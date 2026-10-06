"""Reconcile core: bring the host to the instance's desired state.

``plan`` renders the desired host surface and ``adopt`` records one already
correct resource as ours. This module is the write half, and it is the only one:
self-deploy, fresh install and recovery all reach the host through
``apply_host`` so there is a single materializer to reason about.

Two rules make that safe to run unattended.

Ownership. Every write is authorised by ``plan_changes`` against the managed
manifest, not by the desired plan alone. A name that exists on the host without
a matching managed record is a ``conflict``, and a conflict anywhere aborts the
whole run before the first write. Overwriting a unit we never installed is
exactly the failure mode that makes an unattended reconcile unsafe, so it fails
closed and asks the operator to adopt or declare the name instead.

Atomicity of record. The manifest is rewritten after each resource settles, so
an interrupted run leaves the manifest describing what is really installed. A
resource that failed to install is never recorded as managed.
"""

from __future__ import annotations

import json
import os
import pwd
import stat
import subprocess
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ummanu import _proc
from ummanu._fsutil import directory_lock, write_text_atomic
from ummanu.config import instance_data_dir
from ummanu.head_registry import pinned_product_root
from ummanu.host import (
    CollectResult,
    HostInventory,
    PackagedUnit,
    PlanChange,
    PlannedResource,
    SystemdLayout,
    assess_unit_runtime,
    build_plan,
    default_packaging_root,
    foreign_units,
    load_packaged_units,
    manifest_text,
    packaging_root,
    plan_changes,
    plan_input_errors,
    strict_manifest,
    unit_runtime_expectations,
)
from ummanu.infra.systemd import ACTIVE_STATES, SystemdObservation, observation_error
from ummanu.memory.config import memory_config

SYSTEM_UNIT_DIR = Path("/etc/systemd/system")


@dataclass
class ApplyResult:
    """What one reconcile run changed, refused, or could not do."""

    changes: list[PlanChange] = field(default_factory=list)
    applied: list[str] = field(default_factory=list)
    conflicts: list[PlanChange] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False
    runtime_changes: list[PlanChange] = field(default_factory=list)
    runtime_findings: list[str] = field(default_factory=list)
    preserved_runtime_scopes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.conflicts and not self.errors

    @property
    def changed(self) -> bool:
        return bool(self.applied)

    def render(self) -> list[str]:
        # Only what moves: an apply run that lists every unchanged resource buries
        # the two lines an operator actually has to read.
        lines = [
            f"{change.action} {change.logical_id} {change.kind} {change.name}"
            for change in [*self.changes, *self.runtime_changes]
            if change.action != "unchanged"
        ]
        for conflict in self.conflicts:
            lines.append(f"conflict: {conflict.kind} {conflict.name} is not owned by this instance")
        lines.extend(f"error: {message}" for message in self.errors)
        lines.extend(f"preserved runtime scope: {unit}" for unit in self.preserved_runtime_scopes)
        return lines


class UnitInstaller(ABC):
    """The systemd side of a reconcile. Only this talks to the host."""

    @abstractmethod
    def installed(self, name: str) -> bytes | None: ...

    @abstractmethod
    def install(self, unit: PackagedUnit) -> None: ...

    @abstractmethod
    def remove(self, name: str) -> None: ...

    @abstractmethod
    def daemon_reload(self) -> None: ...

    @abstractmethod
    def enable(self, name: str) -> None: ...

    @abstractmethod
    def disable(self, name: str) -> None: ...

    @abstractmethod
    def restart(self, name: str) -> None: ...

    @abstractmethod
    def start(self, name: str) -> None: ...

    @abstractmethod
    def is_active(self, name: str) -> bool: ...

    @abstractmethod
    def process_identity(self, name: str) -> UnitProcessIdentity | None: ...


@dataclass(frozen=True)
class UnitProcessIdentity:
    """A systemd main process, including a kernel start identity against PID reuse."""

    pid: int
    start_ticks: int
    invocation_id: str


class HostCommandError(RuntimeError):
    """A host command failed. The message names the command, never its output."""


class SystemdUnitInstaller(UnitInstaller):
    """The real host. Unit files are root-owned, so writes go through sudo."""

    timeout_seconds = 60

    def __init__(
        self, unit_dir: Path = SYSTEM_UNIT_DIR, sudo: bool = True, runtime_user: str | None = None
    ) -> None:
        self.unit_dir = unit_dir
        self.sudo = sudo
        self.observation = SystemdObservation(runtime_user)

    def argv(self, cmd: list[str]) -> list[str]:
        """The privileged invocation contour: root work goes through non-interactive sudo."""
        if cmd[0] == "systemctl":
            cmd = [cmd[0], "--system", *cmd[1:]]
        return (["sudo", "-n"] if self.sudo else []) + cmd

    def _run(self, cmd: list[str], label: str) -> subprocess.CompletedProcess[str]:
        argv = self.argv(cmd)
        try:
            result = _proc.run(argv, timeout=self.timeout_seconds)
        except FileNotFoundError:
            raise HostCommandError(f"{label}: {cmd[0]} not found") from None
        except subprocess.TimeoutExpired:
            raise HostCommandError(f"{label}: {cmd[0]} timed out") from None
        except OSError:
            raise HostCommandError(f"{label}: {cmd[0]} could not run") from None
        if result.returncode != 0:
            raise HostCommandError(f"{label}: {cmd[0]} exited {result.returncode}")
        return result

    def installed(self, name: str) -> bytes | None:
        try:
            return (self.unit_dir / name).read_bytes()
        except OSError:
            return None

    def install(self, unit: PackagedUnit) -> None:
        argv = self.argv(
            [
                "install",
                "-m",
                "0644",
                "-o",
                "root",
                "-g",
                "root",
                "/dev/stdin",
                str(self.unit_dir / unit.name),
            ]
        )
        try:
            result = _proc.run(argv, input=unit.content, text=False, timeout=self.timeout_seconds)
        except FileNotFoundError:
            raise HostCommandError(f"install {unit.name}: install not found") from None
        except subprocess.TimeoutExpired:
            raise HostCommandError(f"install {unit.name}: install timed out") from None
        except OSError:
            raise HostCommandError(f"install {unit.name}: install could not run") from None
        if result.returncode != 0:
            raise HostCommandError(f"install {unit.name}: install exited {result.returncode}")

    def remove(self, name: str) -> None:
        self._run(["rm", "-f", str(self.unit_dir / name)], f"remove {name}")

    def daemon_reload(self) -> None:
        self._run(["systemctl", "daemon-reload"], "daemon-reload")

    def enable(self, name: str) -> None:
        self._run(["systemctl", "enable", "--now", name], f"enable {name}")

    def disable(self, name: str) -> None:
        self._run(["systemctl", "disable", "--now", name], f"disable {name}")

    def restart(self, name: str) -> None:
        self._run(["systemctl", "restart", name], f"restart {name}")

    def start(self, name: str) -> None:
        self._run(["systemctl", "start", name], f"start {name}")

    def is_active(self, name: str) -> bool:
        result = self.observation.run(["systemctl", "is-active", name])
        if reason := observation_error(result, states=ACTIVE_STATES):
            raise HostCommandError(f"observe {name}: {reason}")
        return result.stdout.strip() == "active"

    def process_identity(self, name: str) -> UnitProcessIdentity | None:
        """Read systemd's current main PID and bind it to the kernel's start tick.

        ``MainPID`` alone is not a process identity: Linux may recycle it. The unit invocation
        identifies systemd's generation and ``/proc/<pid>/stat`` identifies the kernel process that
        generation currently points at. Read systemd on both sides of ``/proc`` so a replacement
        while observing is unknown rather than accidentally attested.
        """
        first = self._unit_process_properties(name)
        if first is None:
            return None
        start_ticks = _process_start_ticks(first[0])
        if start_ticks is None:
            return None
        second = self._unit_process_properties(name)
        if second != first:
            return None
        return UnitProcessIdentity(first[0], start_ticks, first[1])

    def _unit_process_properties(self, name: str) -> tuple[int, str] | None:
        argv = [
            "systemctl",
            "show",
            name,
            "--property=MainPID",
            "--property=InvocationID",
        ]
        result = self.observation.run(argv)
        if reason := observation_error(result):
            raise HostCommandError(f"observe {name}: {reason}")
        values = {}
        for line in (result.stdout or "").splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key] = value
        try:
            pid = int(values.get("MainPID", "0"))
        except ValueError:
            return None
        invocation_id = values.get("InvocationID", "")
        if pid <= 0 or not invocation_id:
            return None
        return pid, invocation_id


def _process_start_ticks(pid: int) -> int | None:
    """Return Linux ``/proc`` field 22 without confusing a process name's parentheses."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    closing = text.rfind(")")
    fields = text[closing + 1 :].split() if closing >= 0 else []
    # Field 3 is the first item after ``)``; starttime is field 22.
    if len(fields) <= 19:
        return None
    try:
        start_ticks = int(fields[19])
    except ValueError:
        return None
    return start_ticks if start_ticks > 0 else None


@dataclass(frozen=True)
class ApplyInputs:
    """Everything a reconcile needs, resolved once by the caller."""

    instance: dict[str, Any]
    bindings: list[dict[str, Any]]
    inventory: HostInventory
    managed: list[PlannedResource]
    manifest_path: Path
    packaged: list[PackagedUnit]
    runtime_user: str | None = None


def resolve_packaged(
    instance: dict[str, Any],
    packaging_root: Path | None = None,
    *,
    product_root: Path | None = None,
    instance_path: Path,
    data_dir: Path | None = None,
    runtime_user: str | None = None,
) -> list[PackagedUnit]:
    """Compile shipped templates for this installation's user and filesystem layout."""
    layout = resolve_systemd_layout(
        instance,
        packaging_root=packaging_root,
        product_root=product_root,
        instance_path=instance_path,
        data_dir=data_dir,
        runtime_user=runtime_user,
    )
    host = instance.get("host", {}) if isinstance(instance, dict) else {}
    prefix = host.get("unit_prefix", "") if isinstance(host, dict) else ""
    root = packaging_root or default_packaging_root()
    return load_packaged_units(root, prefix if isinstance(prefix, str) else "", layout)


def resolve_installed_packaged(
    instance: dict[str, Any],
    *,
    instance_path: Path,
    data_dir: Path | None = None,
) -> list[PackagedUnit]:
    """Compile the units of the checkout this installation was installed from.

    Every read-only view of a host — doctor, status, the production findings — describes an
    installation, so the desired unit content has to come from the checkout the last upgrade
    recorded rather than from whichever copy of the product is executing the command. Reading the
    running module's `packaging/systemd` would report a portable installation as drifted against a
    catalogue it was never installed with.
    """
    product_root = pinned_product_root(instance_path)
    return resolve_packaged(
        instance,
        packaging_root(product_root),
        product_root=product_root,
        instance_path=instance_path,
        data_dir=data_dir,
    )


def resolve_runtime_owner(instance_path: Path, runtime_user: str | None = None) -> tuple[str, Path]:
    """The account that owns an installation, and the home its paths hang off.

    The instance checkout is durable installation state. When a command runs
    as root after recovery, its owner identifies the runtime account. Failure
    to resolve that owner is an error: falling back to the invoking account
    would turn a read or repair command into a different desired state, and a
    repair run as root would materialize skills, entry points and worktrees
    under ``/root`` while rendering units that name the owner's home.
    """
    target = instance_path.expanduser().resolve(strict=False)
    if runtime_user is None:
        try:
            runtime_user = pwd.getpwuid(target.stat().st_uid).pw_name
        except (KeyError, OSError):
            raise ValueError(f"could not resolve installation user from {target}") from None
    try:
        home = Path(pwd.getpwnam(runtime_user).pw_dir).expanduser().resolve(strict=False)
    except KeyError:
        raise ValueError(f"installation user does not exist: {runtime_user}") from None
    return runtime_user, home


def resolve_systemd_layout(
    instance: dict[str, Any],
    packaging_root: Path | None = None,
    *,
    product_root: Path | None = None,
    instance_path: Path,
    data_dir: Path | None = None,
    runtime_user: str | None = None,
) -> SystemdLayout:
    """Resolve the one systemd layout used for an installation command."""
    root = (packaging_root or default_packaging_root()).resolve(strict=False)
    # Units run with the product checkout as their working directory. Keep every
    # rendered filesystem value absolute so a caller's relative spelling cannot
    # change the service's interpretation of its own layout.
    target = instance_path.expanduser().resolve(strict=False)
    user, home = resolve_runtime_owner(target, runtime_user)
    host = instance.get("host", {}) if isinstance(instance.get("host"), dict) else {}
    memory = memory_config(host)
    configured_data_dir = data_dir if data_dir is not None else instance_data_dir(target)
    return SystemdLayout(
        product_root=(product_root or root.parents[1]).expanduser().resolve(strict=False),
        instance_path=target,
        data_dir=configured_data_dir.expanduser().resolve(strict=False),
        runtime_user=user,
        runtime_home=home,
        memory_model=memory.model,
        memory_dim=memory.dim,
        memory_threads=memory.threads,
    )


def apply_host(
    inputs: ApplyInputs,
    *,
    units: UnitInstaller,
    dry_run: bool = False,
) -> ApplyResult:
    """Reconcile the host to the instance. Fails closed on any conflict."""
    if inputs.inventory.runtime_scopes is not None:
        from dataclasses import replace

        fresh = inputs.inventory.runtime_scopes.revalidate()
        if fresh.errors:
            return ApplyResult(
                errors=["runtime ownership unavailable: " + "; ".join(fresh.errors.values())], dry_run=dry_run
            )
        inputs = replace(
            inputs,
            inventory=replace(
                inputs.inventory,
                units=(inputs.inventory.units | set(fresh.observed)) - fresh.disappeared,
                runtime_scopes=fresh,
            ),
        )
    host = inputs.instance.get("host", {}) if isinstance(inputs.instance, dict) else {}
    prefix = host.get("unit_prefix", "") if isinstance(host, dict) else ""
    errors = plan_input_errors(inputs.instance, inputs.bindings, packaged=inputs.packaged)
    if errors:
        return ApplyResult(errors=list(errors), dry_run=dry_run)
    if not dry_run:
        _, error = strict_manifest(inputs.manifest_path)
        if error:
            return ApplyResult(errors=[error], dry_run=dry_run)

    desired = build_plan(inputs.instance, inputs.bindings, packaged=inputs.packaged)
    changes = plan_changes(
        desired,
        inputs.inventory,
        inputs.managed,
        prefix if isinstance(prefix, str) else "",
        foreign_units(host),
    )
    result = ApplyResult(changes=changes, dry_run=dry_run)
    if inputs.inventory.runtime_scopes is not None:
        result.preserved_runtime_scopes = sorted(inputs.inventory.runtime_scopes.scopes)
    result.conflicts = [change for change in changes if change.action == "conflict"]
    if result.conflicts:
        # Nothing is written: a conflict means at least one name in our namespace
        # is not provably ours, and a partial reconcile around it would leave the
        # host in a state neither the plan nor the manifest describes.
        return result
    if inputs.inventory.runtime_scopes is not None:
        deleting = {change.name for change in changes if change.kind == "unit" and change.action == "delete"}
        for scope_name, scope in inputs.inventory.runtime_scopes.scopes.items():
            dependencies = deleting.intersection(scope["binds_to"])
            if dependencies:
                result.errors.append(
                    f"preserved runtime scope {scope_name} is bound to {', '.join(sorted(dependencies))}; "
                    "settle its runtime lifecycle before removing the bound service"
                )
        if result.errors:
            return result
    desired_by_id = {resource.logical_id: resource for resource in desired}
    packaged_by_name = {unit.name: unit for unit in inputs.packaged}
    # Check the whole batch before the first write. A unit the plan wants but the
    # product does not ship would otherwise fail halfway through, leaving some
    # units installed and the rest not.
    unshipped = sorted(
        change.name
        for change in changes
        if change.kind == "unit"
        and change.action in {"create", "update"}
        and change.name not in packaged_by_name
    )
    if unshipped:
        result.errors.append("no unit file is shipped for: " + ", ".join(unshipped))
        return result
    runtime = unit_runtime_expectations(desired, inputs.packaged)
    findings = assess_unit_runtime(runtime, CollectResult(inputs.inventory))
    # File creates/updates already settle installable units after daemon-reload. Existing owned
    # units need runtime repair even when their manifest and template fingerprints are unchanged.
    unchanged = {
        change.name: change for change in changes if change.kind == "unit" and change.action == "unchanged"
    }
    by_name = {}
    for finding in findings:
        if finding.name not in unchanged:
            continue
        result.runtime_findings.append(finding.render())
        if finding.field == "runtime":
            result.errors.append(finding.render())
            continue
        previous = by_name.get(finding.name)
        action = "enable" if finding.field == "enabled" else "start"
        if previous is None or action == "enable":
            change = unchanged[finding.name]
            by_name[finding.name] = PlanChange(change.logical_id, "unit", change.name, action)
    result.runtime_changes = list(by_name.values())
    if result.errors:
        return result
    if dry_run:
        return result

    managed_by_id = {resource.logical_id: resource for resource in inputs.managed}
    reload_needed = False

    for change in changes:
        if change.action == "unchanged":
            continue
        try:
            touched_units = _apply_change(
                change, managed_by_id.get(change.logical_id), packaged_by_name, units
            )
        except HostCommandError as exc:
            result.errors.append(str(exc))
            break
        reload_needed = reload_needed or touched_units
        if change.action == "delete":
            managed_by_id.pop(change.logical_id, None)
        else:
            managed_by_id[change.logical_id] = desired_by_id[change.logical_id]
        result.applied.append(f"{change.action} {change.logical_id} {change.kind} {change.name}")
        try:
            _write_manifest(
                inputs.manifest_path,
                managed_by_id.values(),
                runtime_user=inputs.runtime_user,
            )
        except (HostCommandError, RuntimeError) as exc:
            result.errors.append(str(exc))
            break

    if not result.applied and not result.errors:
        try:
            _repair_manifest_access(inputs.manifest_path, inputs.runtime_user)
        except HostCommandError as exc:
            result.errors.append(str(exc))

    if reload_needed:
        try:
            units.daemon_reload()
            _settle_units(changes, desired_by_id, packaged_by_name, units)
        except HostCommandError as exc:
            result.errors.append(str(exc))
    if not result.errors:
        for change in result.runtime_changes:
            try:
                if change.action == "enable":
                    units.enable(change.name)
                else:
                    units.start(change.name)
                result.applied.append(f"{change.action} {change.name}")
            except HostCommandError as exc:
                result.errors.append(str(exc))
                break
    return result


def _apply_change(
    change: PlanChange,
    owned: PlannedResource | None,
    packaged_by_name: dict[str, PackagedUnit],
    units: UnitInstaller,
) -> bool:
    """Materialize one change. Returns True when systemd needs a reload."""
    if change.kind == "unit":
        if change.action == "delete":
            # Only a unit with [Install] was ever enabled; `disable` on one
            # without it fails, which would turn a clean removal into an error.
            if _was_installable(owned):
                units.disable(change.name)
            units.remove(change.name)
            return True
        unit = packaged_by_name.get(change.name)
        if unit is None:
            raise HostCommandError(f"install {change.name}: no unit of that name is shipped by this product")
        units.install(unit)
        return True
    raise HostCommandError(f"{change.kind} {change.name}: unsupported resource kind")


def _was_installable(owned: PlannedResource | None) -> bool:
    """Whether the record we are deleting says the unit had an [Install] section."""
    if owned is None:
        return False
    try:
        return json.loads(owned.spec).get("installable") == "yes"
    except (ValueError, TypeError):
        return False


def _settle_units(
    changes: Iterable[PlanChange],
    desired_by_id: dict[str, PlannedResource],
    packaged_by_name: dict[str, PackagedUnit],
    units: UnitInstaller,
) -> None:
    """Enable what we just installed. Idempotent: enable --now is a no-op twice."""
    for change in changes:
        if change.kind != "unit" or change.action in {"delete", "unchanged"}:
            continue
        unit = packaged_by_name.get(change.name)
        if unit is None or not unit.installable:
            continue
        units.enable(change.name)


def _write_manifest(
    path: Path,
    resources: Iterable[PlannedResource],
    *,
    runtime_user: str | None = None,
) -> None:
    """Publish trusted state and hand a root-created file to its reader."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with directory_lock(path.parent):
        _, error = strict_manifest(path)
        if error:
            raise HostCommandError(error)
        write_text_atomic(path, manifest_text(resources))
        _set_manifest_access(path, runtime_user)


def _set_manifest_access(path: Path, runtime_user: str | None) -> None:
    """Make a root-published manifest private to the installation account."""
    if not runtime_user or os.geteuid() != 0:
        return
    try:
        account = pwd.getpwnam(runtime_user)
    except KeyError:
        raise HostCommandError(f"installation user {runtime_user!r} does not exist") from None
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
            raise HostCommandError("managed manifest is not a private regular file")
        os.chown(path, account.pw_uid, account.pw_gid, follow_symlinks=False)
        os.chmod(path, 0o600, follow_symlinks=False)
    except OSError as exc:
        raise HostCommandError(f"could not assign managed manifest to {runtime_user}: {exc}") from None


def _repair_manifest_access(path: Path, runtime_user: str | None) -> None:
    """Repair a valid root-created manifest even when reconciliation is unchanged."""
    if not runtime_user or os.geteuid() != 0:
        return
    try:
        path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise HostCommandError(f"could not inspect managed manifest for {runtime_user}: {exc}") from None
    _set_manifest_access(path, runtime_user)


__all__ = [
    "ApplyInputs",
    "ApplyResult",
    "HostCommandError",
    "SystemdUnitInstaller",
    "UnitInstaller",
    "UnitProcessIdentity",
    "apply_host",
    "resolve_packaged",
    "resolve_systemd_layout",
]
