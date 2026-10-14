"""Pipeline state, web front, host ownership and board materialization."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from ummanu import state_repo
from ummanu.board.migrate import migrate_instance
from ummanu.board.provision import (
    provision as provision_board_store,
    verify_roles as verify_board_store_roles,
)
from ummanu.board.store import BoardStoreError, ensure_ignored, store_path
from ummanu.config import DataDirError
from ummanu.host import (
    FixtureHostSource,
    LiveHostSource,
    build_doctor_expectations,
    build_expectations,
    strict_manifest,
)
from ummanu.host_apply import (
    ApplyInputs,
    HostCommandError,
    apply_host,
    resolve_packaged,
)
from ummanu.infra.process_receipts import ReceiptError as ReceiptError
from ummanu.installation.upgrade.types import (
    MEMORY_COMPONENT,
    WEB_COMPONENT,
    WEB_FRONT_COMPONENT,
    GitError,
    StepResult,
    UpgradeContext,
    _component_unit_prefix,
    _data_dir,
    _po_unit,
    _process_unit_enabled,
)
from ummanu.installation.upgrade.workspaces import (
    _set_runtime_directory_owner,
    _set_runtime_owner,
)
from ummanu.runtime_env import RuntimeEnvError, RuntimeEnvMissing, read_runtime_env
from ummanu.webfront.commands import (
    CONFIG_NAME as WEB_FRONT_CONFIG_NAME,
    FRONT_DIRNAME as WEB_FRONT_DIRNAME,
    SITES_SETTING as WEB_FRONT_SITES_SETTING,
    configured_sites,
    missing_sites_message,
)


def step_pipeline_state(context: UpgradeContext) -> StepResult:
    """Put the dispatcher's untracked run journals back from the instance checkpoint.

    `state/pipeline/` lives in the pipeline worktree but is not tracked, so a worktree the previous
    step recreated comes back without the journals every checkpoint exports (ummanu-1). The restore
    is install's own and keeps its refusal: a live journal that does not extend the checkpoint fails
    the step and is never overwritten.
    """
    # installation imports this module; the restore lives there with install's own call of it.
    from ummanu.installation.materialize import materialize_pipeline_state, pipeline_state_path
    from ummanu.installation.models import InstallError

    if not (context.instance_path / "state" / "runs" / "runs.ndjson").is_file():
        return StepResult("pipeline-state", "skipped", "the instance checkpoint carries no run journal")
    state_dir = pipeline_state_path(context.runtime_home or Path.home())
    try:
        plan = materialize_pipeline_state(context.instance_path, state_dir, dry_run=True)
    except InstallError as exc:
        return StepResult("pipeline-state", "failed", str(exc))
    if not plan.changed:
        return StepResult(
            "pipeline-state",
            "unchanged",
            f"{state_dir} extends the checkpoint's {plan.records} run record(s)",
        )
    if not plan.records:
        # An empty source would make the next export replace the checkpoint's runs with nothing.
        return StepResult("pipeline-state", "skipped", "the checkpoint carries no run records to restore")
    if context.dry_run:
        return StepResult(
            "pipeline-state", "changed", f"would restore {plan.records} run record(s) into {state_dir}"
        )
    created: list[Path] = []
    candidate = state_dir
    while not candidate.exists() and candidate != candidate.parent:
        created.append(candidate)
        candidate = candidate.parent
    try:
        restored = materialize_pipeline_state(context.instance_path, state_dir)
        _set_runtime_owner(state_dir, context.runtime_user)
        for directory in created[1:]:
            _set_runtime_directory_owner(directory, context.runtime_user)
    except (InstallError, GitError) as exc:
        return StepResult("pipeline-state", "failed", str(exc))
    if not restored.changed:
        return StepResult(
            "pipeline-state",
            "unchanged",
            f"{state_dir} extends the checkpoint's {restored.records} run record(s)",
        )
    return StepResult(
        "pipeline-state", "changed", f"restored {restored.records} run record(s) into {state_dir}"
    )


def step_web_front_config(context: UpgradeContext) -> StepResult:
    """Render `<data>/webfront/Caddyfile` from `host.web_front.sites`, before `step_host` starts the front.

    The file is data-directory state (it carries the bcrypt hash) and is not in any checkpoint or
    snapshot, so a recovered host had nothing for `caddy validate` to read (ummanu-53 P12). The sites
    are instance config, which recovery brings back; the hash and session secret are in the store.
    The render is `ummanu web-front render` itself, run as the installation key's owner the way
    `state_repo.run_as_git_child` crosses identity: root never reads that user's key, and the file is
    written by the account whose Caddy reads it.

    An installation that has not moved its sites into instance config yet keeps the file it has,
    byte for byte: nothing is rendered, removed or rewritten. Install and recover refuse that state
    up front (`installation.check_prerequisites`); an upgrade over a host with no file and no sites
    only says so, and leaves the front to `step_host` as before.
    """
    name = "web-front-config"
    report = context.report
    host = report.host if isinstance(report.host, dict) else {}
    prefix = host.get("unit_prefix")
    if not isinstance(prefix, str) or not prefix:
        return StepResult(name, "skipped", "no host.unit_prefix; this installation has no front unit")
    unit = f"{prefix}{WEB_FRONT_COMPONENT}.service"
    if not _process_unit_enabled(context, WEB_FRONT_COMPONENT, unit):
        return StepResult(name, "skipped", f"{unit} is outside this installation's desired units")
    data_dir = _data_dir(context)
    if data_dir is None:
        return StepResult(name, "failed", "instance data directory is unresolved")
    path = data_dir / WEB_FRONT_DIRNAME / WEB_FRONT_CONFIG_NAME
    sites = configured_sites(host)
    if not sites:
        if path.exists():
            return StepResult(
                name,
                "unchanged",
                f"{path} kept as it is: {WEB_FRONT_SITES_SETTING} is not set, so nothing renders it",
            )
        return StepResult(name, "skipped", missing_sites_message(context.instance_path))
    if context.dry_run:
        return StepResult(name, "changed", f"would render {path} for {len(sites)} site(s)")
    argv = [
        sys.executable,
        "-P",
        "-m",
        "ummanu",
        "web-front",
        "render",
        "--instance",
        str(context.instance_path),
        "--data-dir",
        str(data_dir),
    ]
    for site in sites:
        argv += ["--site", site]
    try:
        # The product this process runs, whoever the child runs as.
        completed = state_repo.run_as_git_child(
            context.instance_path,
            argv,
            label="web-front render",
            extra_env={"PYTHONPATH": str(Path(__file__).resolve().parents[3])},
        )
    except state_repo.StateRepoError as exc:
        return StepResult(name, "failed", str(exc))
    if completed.returncode:
        return StepResult(name, "failed", f"web-front render: {_render_failure(completed)}")
    try:
        rendered = json.loads(completed.stdout)
        changed = rendered["changed"]
    except (ValueError, TypeError, KeyError):
        return StepResult(name, "failed", "web-front render printed no result")
    if changed:
        context.web_front_config_changed = True
        return StepResult(name, "changed", f"rendered {path} for {len(sites)} site(s)")
    return StepResult(name, "unchanged", f"{path} current for {len(sites)} site(s)")


def _render_failure(completed: subprocess.CompletedProcess[str]) -> str:
    """The verb's own message from its JSON error line, else its exit status."""
    for line in reversed((completed.stderr or "").strip().splitlines()):
        try:
            message = json.loads(line).get("message")
        except (ValueError, AttributeError):
            continue
        if isinstance(message, str) and message:
            return message
    return f"exit {completed.returncode}"


def step_host(context: UpgradeContext) -> StepResult:
    report = context.report
    assert report.data_dir is not None
    manifest = report.data_dir / "host-managed.json"
    try:
        packaged = resolve_packaged(
            report.instance,
            context.product_root / "packaging" / "systemd",
            product_root=context.product_root,
            instance_path=context.instance_path,
            data_dir=report.data_dir,
            runtime_user=context.runtime_user,
        )
    except (DataDirError, HostCommandError, ValueError) as exc:
        return StepResult("host", "failed", str(exc))
    canonical = build_doctor_expectations(
        report.instance, report.bindings, packaged=packaged, data_dir=report.data_dir
    )
    # Upgrade retains its project availability policy; unit requirements come from the same
    # canonical desired state doctor assesses, using this upgrade's explicit target catalogue.
    projects = build_expectations(report.bindings, report.host, availability=context.project_availability)
    expected = replace(
        projects,
        units=canonical.units,
        unit_runtime=canonical.unit_runtime,
        foreign_units=canonical.foreign_units,
        runtime_data_dir=report.data_dir,
    )
    source = (
        FixtureHostSource(context.host_fixture)
        if context.host_fixture
        else LiveHostSource(context.runtime_user)
    )
    collected = source.collect(expected)
    if collected.errors:
        reasons = "; ".join(f"{kind}: {reason}" for kind, reason in sorted(collected.errors.items()))
        return StepResult("host", "failed", f"host inventory unavailable: {reasons}")
    managed, error = strict_manifest(manifest)
    if error:
        return StepResult("host", "failed", error)
    result = apply_host(
        ApplyInputs(
            instance=report.instance,
            bindings=report.bindings,
            inventory=collected.inventory,
            managed=managed,
            manifest_path=manifest,
            packaged=packaged,
            runtime_user=context.runtime_user,
        ),
        units=context.units,
        dry_run=context.dry_run,
    )
    pending = [change for change in result.changes if change.action != "unchanged"]
    if result.conflicts:
        names = ", ".join(change.name for change in result.conflicts)
        return StepResult("host", "failed", f"unowned names in our namespace: {names}")
    if result.errors:
        return StepResult("host", "failed", "; ".join(result.errors))
    context.unit_changed = any(
        change.kind == "unit" and change.name.startswith(_component_unit_prefix(report, MEMORY_COMPONENT))
        for change in pending
    )
    # Both web units count: the front is `PartOf=` the transport, so a change to either is a change
    # the pair has to be restarted for, and restarting the transport is what restarts the pair.
    context.web_unit_changed = any(
        change.kind == "unit" and change.name.startswith(_component_unit_prefix(report, WEB_COMPONENT))
        for change in pending
    )
    context.po_unit_changed = any(
        change.kind == "unit" and change.action == "update" and change.name == _po_unit(report)
        for change in pending
    )
    runtime_pending = result.runtime_changes
    preserved = (
        "; preserved runtime scopes: " + ", ".join(result.preserved_runtime_scopes)
        if result.preserved_runtime_scopes
        else ""
    )
    if not pending and not runtime_pending:
        return StepResult("host", "unchanged", f"{len(result.changes)} resources reconciled" + preserved)
    detail = ", ".join(f"{change.action} {change.name}" for change in [*pending, *runtime_pending])
    if result.runtime_findings:
        detail += "; " + "; ".join(result.runtime_findings)
    detail += preserved
    return StepResult("host", "changed", detail)


def step_runtime_owner(context: UpgradeContext) -> StepResult:
    """Give the runtime user back `runtime.env`, `.gitignore` and `.git` after a root-run upgrade.

    A no-op for absent paths and for a non-root run.  `runtime.env` is read first so a malformed
    file fails here, before any later step relies on it.
    """
    try:
        read_runtime_env(context.instance_path, require_ignored=False)
    except RuntimeEnvMissing:
        pass
    except RuntimeEnvError as exc:
        return StepResult("runtime-owner", "failed", str(exc))
    if not context.dry_run:
        try:
            _set_runtime_owner(context.instance_path / "runtime.env", context.runtime_user)
            _set_runtime_owner(context.instance_path / ".gitignore", context.runtime_user)
            _set_runtime_owner(context.instance_path / ".git", context.runtime_user)
        except GitError as exc:
            return StepResult("runtime-owner", "failed", str(exc))
    return StepResult(
        "runtime-owner", "unchanged", "runtime.env, .gitignore and .git belong to the runtime user"
    )


def step_board_store(context: UpgradeContext) -> StepResult:
    """Bring a configured board store to the schema this build ships (§7.4).

    Three outcomes, and the first of them is the one that matters today. An installation with no
    `board-store.env` has no store to migrate — that is every installation until the card that
    provisions one lands — so the step is an explicit no-op and the upgrade proceeds exactly as it
    did before this step existed. It is not a warning and it never stops the run.

    With a complete file it reconciles the file's git exclusion, connects as `ummanu_owner` and
    applies what Alembic owes, naming both. With a file that is present but partial, unreadable,
    unreachable or **tracked by the instance repository** it fails with that reason: a store that
    is configured and broken is not something an upgrade may walk past, because the next step it
    would walk to is a service restart.

    The exclusion is reconciled here and enforced in `board_store.resolve`, so this step is where
    an operator *sees* it and not the only place it happens.

    It runs after `step_dependencies` because SQLAlchemy, Alembic and the driver have to be
    installed before it can connect, which is exactly where §7.4 places it.
    """
    path = store_path(context.instance_path)
    if not path.exists() and not path.is_symlink():
        return StepResult("board-store", "skipped", "no board-store.env; the board store is not configured")
    try:
        lifecycle = ensure_ignored(context.instance_path, dry_run=context.dry_run)
        revisions = migrate_instance(context.instance_path, dry_run=context.dry_run)
    except BoardStoreError as exc:
        return StepResult("board-store", "failed", str(exc))
    prefix = f"{lifecycle.render(dry_run=context.dry_run)}; " if lifecycle.changed else ""
    if not revisions:
        return StepResult(
            "board-store",
            "would-change"
            if lifecycle.changed and context.dry_run
            else "changed"
            if lifecycle.changed
            else "unchanged",
            f"{prefix}board store schema is already current",
        )
    listed = ", ".join(revisions)
    if context.dry_run:
        return StepResult(
            "board-store", "would-change", f"{prefix}would apply board store migrations {listed}"
        )
    return StepResult("board-store", "changed", f"{prefix}applied board store migrations {listed}")


def step_board_store_provision(context: UpgradeContext) -> StepResult:
    """Reconcile the container only for an installation carrying the lifecycle marker."""
    try:
        outcome = provision_board_store(
            context.instance_path,
            dry_run=context.dry_run,
            privileged_argv=(context.units.argv if callable(getattr(context.units, "argv", None)) else None),
        )
    except (BoardStoreError, OSError, RuntimeError) as exc:
        return StepResult("board-store-provision", "failed", str(exc))
    if outcome is None:
        return StepResult(
            "board-store-provision", "skipped", "no board-store.env; PostgreSQL is not provisioned"
        )
    return StepResult(
        "board-store-provision",
        "would-change"
        if context.dry_run and outcome.changed
        else "changed"
        if outcome.changed
        else "unchanged",
        outcome.render(dry_run=context.dry_run),
    )


def step_board_store_roles(context: UpgradeContext) -> StepResult:
    path = store_path(context.instance_path)
    if not path.exists() and not path.is_symlink():
        return StepResult("board-store-roles", "skipped", "PostgreSQL is not provisioned")
    if context.dry_run:
        return StepResult("board-store-roles", "skipped", "--dry-run does not test role logins")
    try:
        verify_board_store_roles(context.instance_path)
    except BoardStoreError as exc:
        return StepResult("board-store-roles", "failed", str(exc))
    return StepResult("board-store-roles", "unchanged", "owner/app/read role boundary verified")
