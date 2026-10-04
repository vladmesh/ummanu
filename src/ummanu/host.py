"""Desired host state and read-only host inventory.

Compares what an instance config describes against what is on the host across two resource
kinds: project repos and systemd units. Orca repo registrations are Orca's own state and no longer
part of the host surface. Nothing here changes the host,
and no source reads config values or secrets — only resource *names*.

Desired state has two declarative inputs: the instance config says which components this
installation runs and which foreign names under its unit prefix it does not own; the product's
``packaging/systemd`` directory says what those components are. A unit file's content is part of
the desired state, so its digest rides in the planned resource's spec.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from ummanu.infra.systemd import (
    ACTIVE_STATES,
    ENABLED_STATES,
    SystemdObservation,
    observation_error,
)
from ummanu.infra.systemd import (
    CommandResult as _CmdResult,
)
from ummanu.projects.availability import ProjectAvailability, binding_disabled
from ummanu.runtime.local_pty_head import runtime_scope_inventory
from ummanu.runtime.paths import component_enabled, configured_product_root, default_instance_path

KINDS = ("projects", "units")
UNIT_SUFFIXES = (".service", ".timer")
# The units this checkout ships. Like the role-skill manifest constant, it is what tests about the
# shipped canon read and never the fallback a host command lands on: the units an installation is
# planned or doctored against belong to the product checkout it was installed from.
SHIPPED_PACKAGING_ROOT = Path(__file__).resolve().parents[2] / "packaging" / "systemd"


@dataclass(frozen=True)
class PlannedResource:
    """One desired host resource and the evidence required to own it."""

    logical_id: str
    kind: str
    name: str
    spec: str
    fingerprint: str


@dataclass(frozen=True)
class PlanChange:
    logical_id: str
    kind: str
    name: str
    action: str


def _resource(logical_id: str, kind: str, name: str, payload: dict[str, str]) -> PlannedResource:
    spec = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    value = json.dumps([logical_id, kind, name, spec], separators=(",", ":"))
    return PlannedResource(logical_id, kind, name, spec, hashlib.sha256(value.encode()).hexdigest())


@dataclass(frozen=True)
class PackagedUnit:
    """One rendered systemd unit and the bytes that define its desired state."""

    component: str
    name: str
    path: Path
    content: bytes
    digest: str
    installable: bool
    oneshot: bool


def packaging_root(product_root: Path | str) -> Path:
    """Where a named product checkout keeps its shipped units."""
    return Path(product_root).expanduser() / "packaging" / "systemd"


def default_packaging_root() -> Path:
    """Where the product this host is configured with keeps its shipped units.

    Not this checkout's own directory: a plan or a doctor run describes an installation, and the
    units that installation runs come from the checkout it was installed from.
    """
    return packaging_root(configured_product_root())


@dataclass(frozen=True)
class SystemdLayout:
    """Installation-specific values substituted into shipped systemd templates."""

    product_root: Path
    instance_path: Path
    data_dir: Path
    runtime_user: str
    runtime_home: Path
    memory_model: str = "intfloat/multilingual-e5-large"
    memory_dim: int = 1024
    memory_threads: int = 1


def default_systemd_layout() -> SystemdLayout:
    """The layout of a host nobody named anything for: the configured product, the running home."""
    root = configured_product_root()
    home = Path.home()
    return SystemdLayout(
        root, default_instance_path(), home / "ummanu-data", os.environ.get("USER", "dev"), home
    )


def render_systemd_unit(template: bytes, layout: SystemdLayout) -> bytes:
    """Compile one shipped unit template into its canonical host bytes."""
    values = {
        b"{{UMMANU_PRODUCT_ROOT}}": os.fsencode(layout.product_root),
        b"{{UMMANU_INSTANCE_PATH}}": os.fsencode(layout.instance_path),
        b"{{UMMANU_DATA_DIR}}": os.fsencode(layout.data_dir),
        b"{{UMMANU_RUNTIME_USER}}": layout.runtime_user.encode(),
        b"{{UMMANU_RUNTIME_HOME}}": os.fsencode(layout.runtime_home),
        b"{{UMMANU_MEMORY_MODEL}}": layout.memory_model.encode(),
        b"{{UMMANU_MEMORY_DIM}}": str(layout.memory_dim).encode(),
        b"{{UMMANU_MEMORY_THREADS}}": str(layout.memory_threads).encode(),
    }
    rendered = template
    for marker, value in values.items():
        rendered = rendered.replace(marker, value)
    if b"{{UMMANU_" in rendered:
        raise ValueError("systemd template has an unknown placeholder")
    return rendered


def load_packaged_units(root: Path, prefix: str, layout: SystemdLayout | None = None) -> list[PackagedUnit]:
    """Read the shipped unit catalogue. A unit outside the prefix is not ours.

    Returns an empty catalogue when the directory is absent or unreadable rather than raising, so a
    read-only doctor run does not crash on a plan built without them.
    """
    if not prefix:
        return []
    try:
        entries = sorted(entry for entry in root.iterdir() if entry.is_file())
    except OSError:
        return []
    units: list[PackagedUnit] = []
    for entry in entries:
        if not entry.name.startswith(prefix) or not entry.name.endswith(UNIT_SUFFIXES):
            continue
        try:
            payload = render_systemd_unit(entry.read_bytes(), layout or default_systemd_layout())
        except (OSError, ValueError):
            continue
        suffix = entry.name[entry.name.rindex(".") :]
        component = entry.name[len(prefix) : -len(suffix)]
        if not component:
            continue
        units.append(
            PackagedUnit(
                component=component,
                name=entry.name,
                path=entry,
                content=payload,
                digest=hashlib.sha256(payload).hexdigest(),
                # Only a unit with [Install] can be enabled; the rest are pulled
                # in by a timer's Unit= and enabling them would fail.
                installable=b"[Install]" in payload,
                oneshot=b"Type=oneshot" in payload,
            )
        )
    return units


def foreign_units(host: dict[str, Any]) -> set[str]:
    """Unit names under our prefix that this installation declares are not ours."""
    if not isinstance(host, dict):
        return set()
    return set(_str_list(host.get("foreign_units")))


def build_plan(
    instance: dict[str, Any],
    bindings: Iterable[dict[str, Any]],
    *,
    packaged: Iterable[PackagedUnit] | None = None,
) -> list[PlannedResource]:
    """Render the supported host surface without consulting the live host.

    Heads produce systemd services and every enabled component of the shipped unit catalogue
    produces its unit. Project bindings produce nothing here: a legacy ``orca_binding`` names an
    Orca registration that reconcile neither creates, checks nor removes.
    """
    host = instance.get("host", {}) if isinstance(instance, dict) else {}
    prefix = host.get("unit_prefix", "") if isinstance(host, dict) else ""
    if packaged is None:
        packaged = load_packaged_units(default_packaging_root(), prefix)
    digests = {unit.name: unit.digest for unit in packaged}
    result: list[PlannedResource] = []
    heads = instance.get("heads", []) if isinstance(instance, dict) else []
    if isinstance(heads, list) and prefix:
        for head in heads:
            if not isinstance(head, dict) or not isinstance(head.get("role"), str):
                continue
            role = head["role"]
            logical_id = f"systemd:head:{role}"
            name = f"{prefix}{role}.service"
            model = head.get("model")
            if not isinstance(model, str):
                continue
            result.append(_resource(logical_id, "unit", name, {"model": model, "role": role}))
    if prefix:
        if component_enabled(host, "dispatcher-production"):
            result.extend(_production_dispatcher_units(prefix, digests))
        result.extend(_packaged_component_units(host, packaged))
    # A declared foreign unit is outside this installation's ownership even
    # when its name overlaps a product-shipped unit. Keep that boundary in the
    # canonical desired state so reconcile and doctor cannot disagree about it.
    foreign = foreign_units(host)
    return sorted(
        (resource for resource in result if resource.kind != "unit" or resource.name not in foreign),
        key=lambda resource: (resource.kind, resource.logical_id),
    )


def _production_dispatcher_units(prefix: str, digests: dict[str, str]) -> list[PlannedResource]:
    """The dispatcher pair keeps its own logical ids and semantic spec."""
    service = f"{prefix}dispatcher-production.service"
    timer = f"{prefix}dispatcher-production.timer"
    service_spec = {
        "component": "dispatcher-production",
        "managed_by": "ummanu",
        "runtime": "production pre-import fence -> ummanu dispatcher production-tick --instance $UMMANU_INSTANCE",
        "env": "UMMANU_INSTANCE,UMMANU_DISPATCHER_OWNER",
    }
    timer_spec = {
        "component": "dispatcher-production",
        "managed_by": "ummanu",
        "service": service,
        "on_boot_sec": "30s",
        "on_unit_active_sec": "60s",
    }
    for name, spec in ((service, service_spec), (timer, timer_spec)):
        if digest := digests.get(name):
            spec["digest"] = digest
    return [
        _resource("systemd:dispatcher:production.service", "unit", service, service_spec),
        _resource("systemd:dispatcher:production.timer", "unit", timer, timer_spec),
    ]


def _packaged_component_units(
    host: dict[str, Any], packaged: Iterable[PackagedUnit]
) -> list[PlannedResource]:
    """Every shipped unit of an enabled component, keyed by its own name."""
    result: list[PlannedResource] = []
    for unit in packaged:
        if unit.component == "dispatcher-production":
            continue  # owned by _production_dispatcher_units, which carries its digest
        if not component_enabled(host, unit.component):
            continue
        result.append(
            _resource(
                f"systemd:unit:{unit.name}",
                "unit",
                unit.name,
                {
                    "component": unit.component,
                    "managed_by": "ummanu",
                    "digest": unit.digest,
                    "installable": "yes" if unit.installable else "no",
                },
            )
        )
    return result


def plan_input_errors(
    instance: dict[str, Any],
    bindings: Iterable[dict[str, Any]],
    *,
    packaged: Iterable[PackagedUnit] | None = None,
) -> list[str]:
    """Reject incomplete desired-state inputs before a plan can fail open."""
    bindings = list(bindings)
    packaged = list(packaged) if packaged is not None else None
    host = instance.get("host", {}) if isinstance(instance, dict) else {}
    prefix = host.get("unit_prefix") if isinstance(host, dict) else None
    heads = instance.get("heads", []) if isinstance(instance, dict) else []
    if isinstance(heads, list) and heads and not isinstance(prefix, str):
        return ["host.unit_prefix is required when heads are configured"]
    errors: list[str] = []
    desired = build_plan(instance, bindings, packaged=packaged)
    logical_ids: set[str] = set()
    names: set[tuple[str, str]] = set()
    for resource in desired:
        if resource.logical_id in logical_ids:
            errors.append(f"duplicate desired logical_id: {resource.logical_id}")
        logical_ids.add(resource.logical_id)
        key = (resource.kind, resource.name)
        if key in names:
            errors.append(f"duplicate desired resource name: {resource.kind} {resource.name}")
        names.add(key)
    return errors


def load_managed_manifest(path: Path) -> tuple[list[PlannedResource], str]:
    """Load applied state for a read-only view.

    Missing or semantically invalid state proves no ownership. A manifest that
    could not be read is different: callers must report that condition rather
    than derive a plan from an empty ownership record.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [], ""
    except UnicodeError:
        return [], "managed manifest is not valid UTF-8"
    except OSError:
        return [], "managed manifest is unreadable"
    except ValueError:
        return [], ""
    values = raw.get("resources", []) if isinstance(raw, dict) else []
    resources: list[PlannedResource] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        fields = (value.get("logical_id"), value.get("kind"), value.get("name"), value.get("fingerprint"))
        if all(isinstance(field, str) and field for field in fields):
            spec = value.get("spec", "")
            resources.append(
                PlannedResource(
                    fields[0], fields[1], fields[2], spec if isinstance(spec, str) else "", fields[3]
                )
            )
    return resources, ""


def strict_manifest(path: Path) -> tuple[list[PlannedResource], str]:
    """Load state for a write path. Unlike plan, a writer must fail closed.

    Returns ``(resources, reason)``; a non-empty reason means the manifest could not be trusted, and
    the caller must refuse to write rather than treat the unreadable state as "we own nothing".
    """
    try:
        info = path.lstat()
    except FileNotFoundError:
        return [], ""
    except OSError:
        return [], "managed manifest is unreadable"
    if stat.S_ISLNK(info.st_mode):
        return [], "managed manifest must not be a symlink"
    if not stat.S_ISREG(info.st_mode):
        return [], "managed manifest is not a regular file"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except UnicodeError:
        return [], "managed manifest is not valid UTF-8"
    except OSError:
        return [], "managed manifest is unreadable"
    except ValueError:
        return [], "managed manifest is not valid JSON"
    if (
        not isinstance(payload, dict)
        or payload.get("version") != 1
        or not isinstance(payload.get("resources"), list)
    ):
        return [], "managed manifest has an unsupported shape"
    resources, error = load_managed_manifest(path)
    if error:
        return [], error
    if len(resources) != len(payload["resources"]):
        return [], "managed manifest contains invalid resource records"
    logical_ids: set[str] = set()
    names: set[tuple[str, str]] = set()
    for resource in resources:
        # An "orca" record is a registration an older reconcile adopted or created. It stays
        # valid state, left alone: reconcile no longer plans, checks or deletes Orca repos.
        if resource.kind not in {"unit", "orca"} or not resource.spec:
            return [], "managed manifest contains non-canonical resource records"
        value = json.dumps(
            [resource.logical_id, resource.kind, resource.name, resource.spec],
            separators=(",", ":"),
        )
        if hashlib.sha256(value.encode()).hexdigest() != resource.fingerprint:
            return [], "managed manifest contains a fingerprint mismatch"
        if resource.logical_id in logical_ids:
            return [], "managed manifest has duplicate logical ids"
        logical_ids.add(resource.logical_id)
        key = (resource.kind, resource.name)
        if key in names:
            return [], "managed manifest has duplicate resource names"
        names.add(key)
    return resources, ""


def manifest_text(resources: Iterable[PlannedResource]) -> str:
    records = [
        {
            "fingerprint": resource.fingerprint,
            "kind": resource.kind,
            "logical_id": resource.logical_id,
            "name": resource.name,
            "spec": resource.spec,
        }
        for resource in sorted(resources, key=lambda item: (item.kind, item.logical_id))
    ]
    return json.dumps({"version": 1, "resources": records}, indent=2, sort_keys=True) + "\n"


def plan_changes(
    desired: Iterable[PlannedResource],
    actual: HostInventory,
    managed: Iterable[PlannedResource],
    unit_prefix: str = "",
    declared_foreign: Iterable[str] = (),
) -> list[PlanChange]:
    """Classify changes. A name match is a conflict unless exact state owns it.

    Only units are planned. A legacy managed ``orca`` record has no host inventory to match, so it
    is never deleted: the registration it names is Orca's own state.
    """
    declared_foreign = set(declared_foreign)
    desired, managed = list(desired), list(managed)
    scope_resources = [resource for resource in [*desired, *managed]
                       if resource.kind == "unit" and resource.name.endswith(".scope")
                       and (resource.name not in declared_foreign
                            or actual.runtime_scopes is not None
                            and resource.name in actual.runtime_scopes.scopes)]
    if scope_resources:
        # A scope is never a packaged resource, even if an old manifest or
        # explicit host configuration attempts to put it in that lifecycle.
        return [PlanChange(resource.logical_id, "unit", resource.name, "conflict")
                for resource in scope_resources]
    actual_names = {"unit": actual.units}
    # Do not let an older manifest record pull a now-declared foreign unit back
    # under management through the deletion pass below.
    managed_by_id = {
        resource.logical_id: resource
        for resource in managed
        if resource.kind != "unit" or resource.name not in declared_foreign
    }
    desired_by_id = {
        resource.logical_id: resource
        for resource in desired
        if resource.kind != "unit" or resource.name not in declared_foreign
    }
    changes: list[PlanChange] = []
    for resource in desired_by_id.values():
        present = resource.name in actual_names[resource.kind]
        owned = managed_by_id.get(resource.logical_id)
        if not present:
            action = "create"
        elif owned and owned.kind == resource.kind and owned.name == resource.name:
            action = "update" if owned.fingerprint != resource.fingerprint else "unchanged"
        else:
            action = "conflict"
        changes.append(PlanChange(resource.logical_id, resource.kind, resource.name, action))
    for logical_id, resource in managed_by_id.items():
        desired_resource = desired_by_id.get(logical_id)
        renamed = desired_resource and (
            resource.kind != desired_resource.kind or resource.name != desired_resource.name
        )
        if (desired_resource is None or renamed) and resource.name in actual_names.get(resource.kind, set()):
            changes.append(PlanChange(logical_id, resource.kind, resource.name, "delete"))
    known_units = {resource.name for resource in desired_by_id.values() if resource.kind == "unit"}
    known_units.update(resource.name for resource in managed_by_id.values() if resource.kind == "unit")
    # A name the instance declares foreign stays out of our namespace: it is not
    # a conflict to resolve, it is somebody else's unit we have agreed to leave
    # alone. Declaring it is the only way to say so, so silence here is still
    # fail-closed.
    known_units.update(declared_foreign)
    if actual.runtime_scopes is not None and not actual.runtime_scopes.errors:
        known_units.update(actual.runtime_scopes.scopes)
        known_units.update(actual.runtime_scopes.disappeared)
    if unit_prefix:
        for name in actual.units:
            if name.startswith(unit_prefix) and name not in known_units:
                changes.append(PlanChange(f"systemd:conflict:{name}", "unit", name, "conflict"))
    return sorted(changes, key=lambda change: (change.kind, change.logical_id))


@dataclass(frozen=True)
class HostInventory:
    """The set of resource names actually present on the host."""

    projects: set[str] = field(default_factory=set)
    units: set[str] = field(default_factory=set)
    unit_states: dict[str, tuple[str, str]] = field(default_factory=dict)
    # systemd's LastTriggerUSec per probed timer ("n/a" when it never fired): the evidence that a
    # schedule ran, which `enabled`/`active` of a waiting timer cannot give.
    timer_triggers: dict[str, str] = field(default_factory=dict)
    runtime_scopes: Any = None


@dataclass(frozen=True)
class Expectations:
    """Resource names an instance config says it owns."""

    projects: set[str] = field(default_factory=set)
    units: set[str] = field(default_factory=set)
    unit_prefix: str = ""
    projects_root: str = ""
    foreign_units: set[str] = field(default_factory=set)
    unit_runtime: dict[str, tuple[bool, bool]] = field(default_factory=dict)
    project_error: str = ""
    runtime_data_dir: Path | None = None
    #: Checkouts of disabled bindings: not required on the host, and not unmanaged when present.
    dormant_projects: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class KindDiff:
    """Three-way comparison for one resource kind. Sorted, name-only."""

    matched: list[str]
    missing_on_host: list[str]
    unmanaged_on_host: list[str]


@dataclass(frozen=True)
class CollectResult:
    """What a source found on the host, plus why any kind could not be read.

    An unreadable kind is never reported as an empty host: doctor marks it unavailable instead of
    comparing against an empty set, so "could not inspect" never masquerades as "nothing there".
    """

    inventory: HostInventory
    errors: dict[str, str] = field(default_factory=dict)


def _project_name(binding: dict[str, Any]) -> str:
    """Pick the host-facing name of a project binding."""
    repo = binding.get("repo")
    if isinstance(repo, str) and "/" in repo:
        name = PurePosixPath(repo).name
        name = name.removesuffix(".git")
        if name:
            return name
    identifier = binding.get("id")
    return identifier if isinstance(identifier, str) else ""


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def build_expectations(
    bindings: Iterable[dict[str, Any]],
    host: dict[str, Any],
    *,
    availability: ProjectAvailability = ProjectAvailability(),
) -> Expectations:
    """Derive expected resource names from bindings and the instance ``host`` block."""
    host = host if isinstance(host, dict) else {}
    bindings = list(bindings)
    projects = {
        name
        for binding in bindings
        if availability.allows(str(binding.get("id") or "")) and not binding_disabled(binding)
        for name in (_project_name(binding),)
        if name
    }
    dormant = {
        name
        for binding in bindings
        if binding_disabled(binding)
        for name in (_project_name(binding),)
        if name
    }
    return Expectations(
        projects=projects,
        dormant_projects=dormant - projects,
        units=set(_str_list(host.get("units"))),
        unit_prefix=host.get("unit_prefix", "") if isinstance(host.get("unit_prefix"), str) else "",
        projects_root=host.get("projects_root", "") if isinstance(host.get("projects_root"), str) else "",
    )


def _normalized_repo_path(repo: str) -> str:
    return str(Path(repo).expanduser().resolve(strict=False))


def unit_runtime_expectations(
    desired: Iterable[PlannedResource], packaged: Iterable[PackagedUnit]
) -> dict[str, tuple[bool, bool]]:
    """Required persistent state of the canonical desired units, from their shipped metadata."""
    packaged_by_name = {unit.name: unit for unit in packaged}
    runtime = {}
    for resource in desired:
        if resource.kind != "unit":
            continue
        unit = packaged_by_name.get(resource.name)
        if unit is not None and unit.oneshot and not unit.installable:
            runtime[resource.name] = (False, False)
        else:
            runtime[resource.name] = (True, True)
    return runtime


def build_doctor_expectations(
    instance: dict[str, Any],
    bindings: Iterable[dict[str, Any]],
    *,
    packaged: Iterable[PackagedUnit] | None = None,
    data_dir: Path | None = None,
) -> Expectations:
    """Derive doctor parity from reconcile's canonical desired state."""
    bindings = list(bindings)
    host = instance.get("host", {}) if isinstance(instance, dict) else {}
    host = host if isinstance(host, dict) else {}
    projects: set[str] = set()
    dormant: set[str] = set()
    project_error = ""
    for binding in bindings:
        if not isinstance(binding, dict) or not isinstance(binding.get("repo"), str):
            continue
        try:
            (dormant if binding_disabled(binding) else projects).add(_normalized_repo_path(binding["repo"]))
        except (OSError, RuntimeError):
            # A symlink loop or unreadable binding path is not evidence that the
            # checkout is absent. Leave this kind unavailable for doctor.
            project_error = "expected project checkout path could not be normalized"
    prefix = host.get("unit_prefix", "") if isinstance(host.get("unit_prefix"), str) else ""
    packaged = (
        list(packaged) if packaged is not None else load_packaged_units(default_packaging_root(), prefix)
    )
    desired = build_plan(instance, bindings, packaged=packaged)
    units = {resource.name for resource in desired if resource.kind == "unit"}
    return Expectations(
        projects=projects,
        units=units,
        unit_prefix=prefix,
        projects_root=host.get("projects_root", "") if isinstance(host.get("projects_root"), str) else "",
        foreign_units=foreign_units(host),
        unit_runtime=unit_runtime_expectations(desired, packaged),
        project_error=project_error,
        runtime_data_dir=data_dir,
        dormant_projects=dormant - projects,
    )


@dataclass(frozen=True)
class UnitRuntimeFinding:
    name: str
    field: str
    actual: str

    def render(self) -> str:
        if self.field == "runtime":
            return f"{self.name}: runtime status unavailable"
        return f"{self.name}: expected {self.field}, got {self.actual}"


def assess_unit_runtime(
    runtime: dict[str, tuple[bool, bool]], collected: CollectResult
) -> list[UnitRuntimeFinding]:
    """Assess observable, present units. File absence and kind unavailability belong to inventory."""
    if "units" in collected.errors:
        return []
    findings = []
    for name, (need_enabled, need_active) in sorted(runtime.items()):
        if name not in collected.inventory.units:
            continue
        state = collected.inventory.unit_states.get(name)
        if state is None:
            if need_enabled or need_active:
                findings.append(UnitRuntimeFinding(name, "runtime", "unavailable"))
            continue
        enabled, active = state
        if need_enabled and enabled != "enabled":
            findings.append(UnitRuntimeFinding(name, "enabled", enabled))
        if need_active and active != "active":
            findings.append(UnitRuntimeFinding(name, "active", active))
    return findings


def _diff(expected: set[str], actual: set[str]) -> KindDiff:
    return KindDiff(
        matched=sorted(expected & actual),
        missing_on_host=sorted(expected - actual),
        unmanaged_on_host=sorted(actual - expected),
    )


def inventory(expected: Expectations, actual: HostInventory) -> dict[str, KindDiff]:
    """Compare expectations against a host inventory, one KindDiff per kind."""
    transient = (set(actual.runtime_scopes.scopes) | set(actual.runtime_scopes.disappeared)
                 if actual.runtime_scopes is not None and not actual.runtime_scopes.errors else set())
    return {
        "projects": _diff(expected.projects, actual.projects - expected.dormant_projects),
        "units": _diff(expected.units, actual.units - expected.foreign_units - transient),
    }


class HostSource(ABC):
    """Something that can enumerate host resources without changing them."""

    @abstractmethod
    def collect(self, expected: Expectations) -> CollectResult: ...


def _with_runtime_scopes(expected: Expectations, collected: CollectResult) -> CollectResult:
    """Keep runtime preservation evidence separate from packaged desired state."""
    from dataclasses import replace

    if expected.runtime_data_dir is None or "units" in collected.errors:
        return collected
    projected = runtime_scope_inventory(expected.runtime_data_dir, collected.inventory.units)
    errors = dict(collected.errors)
    if projected.errors:
        errors["units"] = "runtime ownership unavailable: " + "; ".join(projected.errors.values())
    # Preserve the raw observation for refresh. Absence is a current projection,
    # never a permanent removal from the authority's input names.
    actual = replace(collected.inventory, runtime_scopes=projected)
    return CollectResult(actual, errors)


def _names_from_dir(directory: Path) -> set[str]:
    return {entry.name for entry in directory.iterdir() if entry.is_dir()}


#: A fixture host's directory of installed unit files (`FixtureHostSource`).
FIXTURE_UNIT_FILES_DIR = "unit-files"


class FixtureHostSource(HostSource):
    """A host modelled by a fixture directory. Used by tests and offline checks.

    Layout under ``root``::

            projects/<name>/     one directory per project repo on the fixture host
            units.txt            one systemd unit name per line
            unit-files/          installed unit files, read by doctor's live-root findings

    Reads only. A missing root is an inspection failure, so every kind is marked unavailable; within
    an existing root a missing per-kind file means an empty set.
    """

    def __init__(self, root: Path):
        self.root = root

    def _lines(self, name: str) -> tuple[set[str], str]:
        try:
            path = self.root / name
            if not path.is_file():
                return set(), ""
            names = {
                token
                for line in path.read_text(encoding="utf-8").splitlines()
                if (token := line.strip()) and not token.startswith("#")
            }
            return names, ""
        except UnicodeError:
            return set(), "fixture host file is not valid UTF-8"
        except OSError:
            return set(), "fixture host file is unreadable"

    def _projects(self, expected: Expectations) -> tuple[set[str], str]:
        try:
            paths, error = self._lines("projects.txt")
            if error or paths:
                return {_normalized_repo_path(path) for path in paths}, error
            projects_dir = self.root / "projects"
            if not projects_dir.is_dir():
                return set(), ""
            # Legacy fixtures model checkouts beneath their own root. Their
            # directory names are observed host facts, not aliases for an
            # expected binding with the same basename.
            return {
                _normalized_repo_path(str(projects_dir / name)) for name in _names_from_dir(projects_dir)
            }, ""
        except OSError:
            return set(), "fixture projects directory is unreadable"

    def collect(self, expected: Expectations) -> CollectResult:
        if not self.root.is_dir():
            # The root was never read, so this is "could not inspect", not an
            # empty host. Marking every kind unavailable stops doctor from
            # reporting all expected resources as missing against a phantom host.
            reason = "fixture host directory not found"
            return CollectResult(
                inventory=HostInventory(),
                errors={kind: reason for kind in KINDS},
            )
        projects, project_error = self._projects(expected)
        units, unit_error = self._lines("units.txt")
        states, state_error = self._unit_states()
        triggers, trigger_error = self._timer_triggers()
        errors = {
            kind: reason
            for kind, reason in (
                ("projects", project_error),
                ("units", unit_error or state_error or trigger_error),
            )
            if reason
        }
        return _with_runtime_scopes(expected, CollectResult(
            HostInventory(projects, units, states, timer_triggers=triggers), errors))

    def _timer_triggers(self) -> tuple[dict[str, str], str]:
        """Optional fixture last triggers: ``timer LastTriggerUSec`` per line (the value has spaces)."""
        try:
            path = self.root / "timer-triggers.txt"
            if not path.is_file():
                return {}, ""
            triggers: dict[str, str] = {}
            for line in path.read_text(encoding="utf-8").splitlines():
                name, _, value = line.strip().partition(" ")
                if not name or name.startswith("#"):
                    continue
                if not value.strip():
                    return {}, "fixture timer triggers are invalid"
                triggers[name] = value.strip()
            return triggers, ""
        except UnicodeError:
            return {}, "fixture timer triggers are not valid UTF-8"
        except OSError:
            return {}, "fixture timer triggers are unreadable"

    def _unit_states(self) -> tuple[dict[str, tuple[str, str]], str]:
        """Optional fixture runtime states: ``unit enabled active`` per line."""
        try:
            path = self.root / "unit-states.txt"
            if not path.is_file():
                return {}, ""
            states: dict[str, tuple[str, str]] = {}
            for line in path.read_text(encoding="utf-8").splitlines():
                fields = line.split()
                if not fields or fields[0].startswith("#"):
                    continue
                if len(fields) != 3:
                    return {}, "fixture unit states are invalid"
                states[fields[0]] = (fields[1], fields[2])
            return states, ""
        except UnicodeError:
            return {}, "fixture unit states are not valid UTF-8"
        except OSError:
            return {}, "fixture unit states are unreadable"


class LiveHostSource(HostSource):
    """The real host: the projects directory and systemd, both read-only.

    A failure to inspect a kind is recorded per kind in the CollectResult rather than silently
    turning into an empty set, so doctor can say "could not inspect" instead of a false "nothing".
    """

    # Cap each host probe so a hung systemctl cannot wedge doctor.
    timeout_seconds = 10
    # Where the installed unit files are read from (doctor's live-root findings).
    unit_files_dir = Path("/etc/systemd/system")

    def __init__(self, runtime_user: str | None = None):
        self.runtime_user = runtime_user

    def collect(self, expected: Expectations) -> CollectResult:
        inventory = HostInventory()
        errors: dict[str, str] = {}

        projects, reason = self._projects(expected)
        if reason:
            errors["projects"] = reason
        else:
            inventory = HostInventory(projects, inventory.units, inventory.unit_states)

        units, unit_states, triggers, reason = self._units(expected)
        if reason:
            errors["units"] = reason
        else:
            inventory = HostInventory(inventory.projects, units, unit_states, timer_triggers=triggers)

        return _with_runtime_scopes(expected, CollectResult(inventory=inventory, errors=errors))

    def _projects(self, expected: Expectations) -> tuple[set[str], str]:
        if expected.project_error:
            return set(), expected.project_error
        root = expected.projects_root
        actual: set[str] = set()
        for project in expected.projects:
            try:
                mode = Path(project).stat().st_mode
            except FileNotFoundError:
                continue
            except (OSError, RuntimeError):
                return set(), "expected project checkout path could not be inspected"
            if stat.S_ISDIR(mode):
                actual.add(project)
        if not root:
            return (actual, "host.projects_root not set") if expected.projects else (actual, "")
        path = Path(root).expanduser()
        if not path.is_dir():
            # Never echo the configured value: it comes from private instance
            # config and could carry a secret-like path. Name the field only.
            return set(), "host.projects_root is not a directory"
        try:
            actual.update(str(entry.resolve(strict=False)) for entry in path.iterdir() if entry.is_dir())
            return actual, ""
        except OSError:
            return set(), "host.projects_root is not readable"

    def _units(
        self, expected: Expectations
    ) -> tuple[set[str], dict[str, tuple[str, str]], dict[str, str], str]:
        prefix = expected.unit_prefix
        if not prefix:
            # unmanaged-on-host can only be computed by enumerating a namespace.
            # Declared units with no prefix would let us confirm the declared
            # ones and silently miss every undescribed host unit, so we refuse
            # to emit a diff that cannot include unmanaged-on-host.
            if expected.units:
                return set(), {}, {}, "host.unit_prefix is required to compute unmanaged-on-host"
            return set(), {}, {}, ""
        result = self._run(["systemctl", "list-unit-files", "--no-legend", f"{prefix}*"])
        reason = observation_error(result, allow_empty_match=True)
        if reason:
            return set(), {}, {}, reason
        names: set[str] = set()
        for line in result.stdout.splitlines():
            fields = line.split()
            token = fields[0] if fields else ""
            if token.startswith(prefix):
                names.add(token)
        # Transient scopes have no unit file. Enumerate loaded units as well so
        # an unknown transient unit cannot disappear from ownership comparison.
        loaded = self._run(["systemctl", "list-units", "--all", "--plain", "--no-legend", f"{prefix}*"])
        reason = observation_error(loaded, allow_empty_match=True)
        if reason:
            return set(), {}, {}, reason
        for line in loaded.stdout.splitlines():
            fields = line.split()
            if fields and fields[0].startswith(prefix):
                names.add(fields[0])
        states: dict[str, tuple[str, str]] = {}
        triggers: dict[str, str] = {}
        for name in expected.unit_runtime:
            if name not in names:
                continue
            enabled = self._run(["systemctl", "is-enabled", name])
            active = self._run(["systemctl", "is-active", name])
            reason = observation_error(enabled, states=ENABLED_STATES) or observation_error(
                active, states=ACTIVE_STATES
            )
            if reason:
                return set(), {}, {}, reason
            states[name] = (enabled.stdout.strip(), active.stdout.strip())
            if name.endswith(".timer"):
                # Best effort: a missing trigger reads as unknown, never as an inventory failure.
                shown = self._run(["systemctl", "show", "--property=LastTriggerUSec", "--value", name])
                if shown.ran and shown.returncode == 0 and (value := shown.stdout.strip()):
                    triggers[name] = value
        return names, states, triggers, ""

    def _run(self, cmd: list[str]) -> _CmdResult:
        return SystemdObservation(self.runtime_user, self.timeout_seconds).run(cmd)
