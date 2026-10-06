"""Read-only command handlers for Phase 7 host planning."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from ummanu._fsutil import directory_lock, write_text_atomic
from ummanu.config import validate_instance
from ummanu.host import (
    KINDS,
    FixtureHostSource,
    LiveHostSource,
    build_doctor_expectations,
    build_plan,
    foreign_units,
    load_managed_manifest,
    plan_changes,
    plan_input_errors,
)
from ummanu.host import (
    manifest_text as _manifest_text,
)
from ummanu.host import (
    strict_manifest as _strict_manifest,
)
from ummanu.host_apply import resolve_installed_packaged, resolve_runtime_owner
from ummanu.runtime.paths import add_instance_argument


def run_reconcile_plan(args) -> int:
    report = validate_instance(Path(args.instance))
    if not report.ok:
        print("ummanu reconcile plan: invalid instance config")
        return 2
    assert report.data_dir is not None
    packaged = resolve_installed_packaged(
        report.instance,
        instance_path=report.instance_path.parent,
        data_dir=report.data_dir,
    )
    errors = plan_input_errors(report.instance, report.bindings, packaged=packaged)
    if errors:
        print("ummanu reconcile plan: " + errors[0])
        return 2
    if args.offline:
        print("ummanu reconcile plan: --offline cannot produce a plan; use --host-fixture instead")
        return 2
    expected = build_doctor_expectations(report.instance, report.bindings, packaged=packaged,
                                         data_dir=report.data_dir)
    source = FixtureHostSource(Path(args.host_fixture)) if args.host_fixture else LiveHostSource()
    collected = source.collect(expected)
    if collected.errors:
        print("ummanu reconcile plan: host inventory unavailable")
        for kind in KINDS:
            if reason := collected.errors.get(kind):
                print(f"  {kind}: unavailable: {reason}")
        return 2
    manifest = _manifest_path(args, report)
    prefix = report.host.get("unit_prefix", "")
    managed, error = load_managed_manifest(manifest)
    if error:
        print("ummanu reconcile plan: " + error)
        return 2
    changes = plan_changes(
        build_plan(report.instance, report.bindings, packaged=packaged),
        collected.inventory,
        managed,
        prefix if isinstance(prefix, str) else "",
        foreign_units(report.host),
    )
    for change in changes:
        print(f"{change.action} {change.logical_id} {change.kind} {change.name}")
    return 1 if any(change.action == "conflict" for change in changes) else 0


def _manifest_path(args, report) -> Path:
    if getattr(args, "managed_manifest", None):
        return Path(args.managed_manifest)
    assert report.data_dir is not None
    return report.data_dir / "host-managed.json"


def _merge_adoption(managed, resource):
    by_id = {item.logical_id: item for item in managed}
    current = by_id.get(resource.logical_id)
    if current is not None and current != resource:
        return [], current, "existing managed record has drifted"
    for item in managed:
        if (
            item.logical_id != resource.logical_id
            and item.kind == resource.kind
            and item.name == resource.name
        ):
            return [], current, "resource name is already owned by another logical id"
    updated = [item for item in managed if item.logical_id != resource.logical_id] + [resource]
    return updated, current, ""


def run_reconcile_adopt(args) -> int:
    report = validate_instance(Path(args.instance))
    if not report.ok:
        print("ummanu reconcile adopt: invalid instance config")
        return 2
    assert report.data_dir is not None
    packaged = resolve_installed_packaged(
        report.instance,
        instance_path=report.instance_path.parent,
        data_dir=report.data_dir,
    )
    errors = plan_input_errors(report.instance, report.bindings, packaged=packaged)
    if errors:
        print("ummanu reconcile adopt: " + errors[0])
        return 2
    desired = {
        resource.logical_id: resource
        for resource in build_plan(report.instance, report.bindings, packaged=packaged)
    }
    resource = desired.get(args.logical_id)
    if resource is None:
        print("ummanu reconcile adopt: logical id is not in desired state")
        return 2
    if resource.kind == "unit":
        problem = _verify_unit_identity(resource, Path(args.unit_dir))
        if problem:
            print("ummanu reconcile adopt: " + problem)
            return 2
        return _record_adoption(args, report, resource)
    print("ummanu reconcile adopt: resource kind has no verifiable adoption identity")
    return 2


def _verify_unit_identity(resource, unit_dir: Path) -> str:
    """Adopt a unit only when the installed file is what this product ships.

    The desired spec carries the shipped file's digest, so a byte-for-byte match
    is proof that the unit on the host is the one we would have written. A
    hand-edited or third-party unit under our prefix never matches, and stays a
    conflict for the operator to resolve deliberately.
    """
    try:
        expected = json.loads(resource.spec)["digest"]
    except (ValueError, KeyError, TypeError):
        return "desired unit carries no shipped file digest to verify against"
    path = unit_dir / resource.name
    if path.is_symlink():
        return "installed unit must not be a symlink"
    try:
        installed = path.read_bytes()
    except OSError:
        return "installed unit is missing or unreadable"
    if hashlib.sha256(installed).hexdigest() != expected:
        return "installed unit does not match the shipped file"
    return ""


def _record_adoption(args, report, resource) -> int:
    manifest = _manifest_path(args, report)
    managed, error = _strict_manifest(manifest)
    if error:
        print("ummanu reconcile adopt: " + error)
        return 2
    updated, current, error = _merge_adoption(managed, resource)
    if error:
        print("ummanu reconcile adopt: " + error)
        return 2
    print(f"adopt {resource.logical_id} {resource.kind} {resource.name} {resource.fingerprint}")
    if not args.yes:
        print("preview only; pass --yes to write the managed manifest")
        return 0

    with directory_lock(manifest.parent):
        managed, error = _strict_manifest(manifest)
        if error:
            print("ummanu reconcile adopt: " + error)
            return 2
        updated, current, error = _merge_adoption(managed, resource)
        if error:
            print("ummanu reconcile adopt: " + error)
            return 2
        if current == resource:
            print("managed manifest already records this resource")
            return 0
        try:
            if manifest.is_symlink():
                raise RuntimeError("managed manifest must not be a symlink")
            write_text_atomic(manifest, _manifest_text(updated))
        except RuntimeError as exc:
            print("ummanu reconcile adopt: " + str(exc))
            return 2
    print("managed manifest updated")
    return 0


def run_reconcile_apply(args) -> int:
    """Bring the host to the instance config. This is the write half of plan."""
    from ummanu.host_apply import (
        ApplyInputs,
        SystemdUnitInstaller,
        apply_host,
    )

    report = validate_instance(Path(args.instance))
    if not report.ok:
        print("ummanu reconcile apply: invalid instance config")
        return 2
    assert report.data_dir is not None
    packaged = resolve_installed_packaged(
        report.instance,
        instance_path=report.instance_path.parent,
        data_dir=report.data_dir,
    )
    try:
        runtime_user, _ = resolve_runtime_owner(report.instance_path.parent)
    except ValueError as exc:
        print("ummanu reconcile apply: " + str(exc))
        return 2
    expected = build_doctor_expectations(report.instance, report.bindings, packaged=packaged,
                                         data_dir=report.data_dir)
    source = FixtureHostSource(Path(args.host_fixture)) if args.host_fixture else LiveHostSource(runtime_user)
    collected = source.collect(expected)
    if collected.errors:
        # Reconciling against a half-read host would read a missing unit as
        # "absent" and reinstall over whatever is really there.
        print("ummanu reconcile apply: host inventory unavailable")
        for kind in KINDS:
            if reason := collected.errors.get(kind):
                print(f"  {kind}: unavailable: {reason}")
        return 2
    manifest = _manifest_path(args, report)
    managed, error = _strict_manifest(manifest)
    if error:
        print("ummanu reconcile apply: " + error)
        return 2
    result = apply_host(
        ApplyInputs(
            instance=report.instance,
            bindings=report.bindings,
            inventory=collected.inventory,
            managed=managed,
            manifest_path=manifest,
            packaged=packaged,
            runtime_user=runtime_user,
        ),
        units=SystemdUnitInstaller(runtime_user=runtime_user),
        dry_run=args.dry_run,
    )
    for line in result.render():
        print(line)
    if result.conflicts:
        print("ummanu reconcile apply: refusing to write while the host holds unowned names")
        if any(change.name.endswith(".scope") for change in result.conflicts):
            print("  inspect canonical runtime ownership and native scope identity; retain uncertain scopes for their lifecycle")
        else:
            print("  adopt them with `ummanu reconcile adopt`, or declare them in host.foreign_units")
        return 1
    if result.errors:
        return 2
    print("applied" if result.changed else "already reconciled")
    return 0


def add_reconcile_subcommands(subcommands) -> None:
    """Register the Phase 7 host commands outside the already busy CLI module."""
    plan = subcommands.add_parser("plan", help="show the read-only desired host plan")
    add_instance_argument(plan)
    plan.add_argument("--dry-run", action="store_true", help=argparse.SUPPRESS)
    source = plan.add_mutually_exclusive_group()
    source.add_argument(
        "--host-fixture",
        metavar="DIR",
        help="read a deterministic fixture inventory instead of the live host",
    )
    source.add_argument(
        "--offline",
        action="store_true",
        help="reject live inventory; use --host-fixture for an offline plan",
    )
    plan.add_argument("--managed-manifest", metavar="FILE")
    plan.set_defaults(handler=run_reconcile_plan)

    apply_command = subcommands.add_parser(
        "apply", help="bring the host to the instance config (the write half of plan)"
    )
    add_instance_argument(apply_command)
    apply_command.add_argument(
        "--dry-run", action="store_true", help="show the changes without touching the host"
    )
    apply_command.add_argument(
        "--host-fixture",
        metavar="DIR",
        help="read a deterministic fixture inventory instead of the live host",
    )
    apply_command.add_argument("--managed-manifest", metavar="FILE")
    apply_command.set_defaults(handler=run_reconcile_apply)

    adopt = subcommands.add_parser("adopt", help="record one verified existing desired resource as managed")
    add_instance_argument(adopt)
    adopt.add_argument("--logical-id", required=True)
    adopt.add_argument("--managed-manifest", metavar="FILE")
    adopt.add_argument("--unit-dir", default="/etc/systemd/system", metavar="DIR")
    adopt.add_argument(
        "--yes", action="store_true", help="write the managed manifest after showing the record"
    )
    adopt.set_defaults(handler=run_reconcile_adopt)
