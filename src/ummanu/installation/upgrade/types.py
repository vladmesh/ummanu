"""Shared upgrade context, results, errors and installation path facts."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ummanu.dispatch.entrypoint_guard import EntrypointMoved
from ummanu.host import (
    foreign_units,
)
from ummanu.host_apply import (
    UnitInstaller,
)
from ummanu.infra.process_receipts import ReceiptError as ReceiptError
from ummanu.projects.availability import ProjectAvailability
from ummanu.runtime.paths import component_enabled

MEMORY_COMPONENT = "memory"

WEB_COMPONENT = "web"

WEB_FRONT_COMPONENT = "web-front"

PO_COMPONENT = "po"

# How long an idle PO service is given to exit and come back under `Restart=always` (RestartSec=3).
PO_RESTART_WAIT_SECONDS = 30.0

PO_RESTART_POLL_SECONDS = 0.5

# Git's own spelling, for this repository. Every path in a `git diff --name-only` here starts with
# `src/`, because that is where the product's packages live; a prefix of `ummanu/` matched none
# of them, so a source-only or schema-only revision — `f9cabc3`, the one that took the web process
# down — moved no flag at all and no long-lived process was ever restarted for it. The prefixes are
# therefore derived from the tree, and `tests/test_web_process_coherence.py` fails if a listed
# prefix stops naming a directory that exists (secretary-1624 rework). Every package under `src/` is
# product code a long-lived process imports — `ummanu` and the background-agent CLI on top of it
# alike — so the one prefix covers them all without this module naming a package it must not
# depend on (secretary-1689).
PRODUCT_SOURCE_PATHS = ("src/",)

DEPENDENCY_PATHS = ("pyproject.toml", "uv.lock", "requirements.txt")

# The bundled JSON Schemas are product data a long-lived process reads from the checkout through
# `importlib.resources` *after* it started, while its validation callables were loaded at start. So
# a schema move is its own named restart reason, not a detail of "code changed": it is the half of
# the split that makes an unrestarted process fail on files its own build shipped (secretary-1624).
SCHEMA_PATHS = ("src/ummanu/schemas/",)

# Where the shipped unit files live in the repository. A revision that edits one of them is a unit
# change `step_host` cannot see yet under `--dry-run`, because the checkout has not moved.
PACKAGED_UNIT_ROOT = "packaging/systemd/"

# Changes here require restarting a long-lived service even if its unit file is unchanged.
MEMORY_CODE_PATHS = (*PRODUCT_SOURCE_PATHS, *DEPENDENCY_PATHS)

# How long a restarted web transport is given to answer one bounded loopback read.
WEB_PROBE_TIMEOUT_SECONDS = 20.0


@dataclass
class StepResult:
    name: str
    status: str
    detail: str = ""

    @property
    def failed(self) -> bool:
        return self.status == "failed"


class _SourceCutOwner(Protocol):
    """Context seam without a dependency from shared types back to acquisition."""

    def check(self, selected: Path) -> None: ...

    def digest(self, selected: Path, paths: tuple[str, ...], *, revision: str | None = None) -> str: ...


@dataclass
class UpgradeContext:
    """Everything the steps share, resolved once before the first of them runs."""

    instance_path: Path
    product_root: Path
    base_branch: str
    dry_run: bool
    units: UnitInstaller
    host_fixture: Path | None = None
    pull: bool = True
    report: Any = None
    changed_paths: tuple[str, ...] = ()
    code_changed: bool = False
    schemas_changed: bool = False
    unit_changed: bool = False
    web_unit_changed: bool = False
    # `step_web_front_config` rewrote the front's Caddyfile; the pair restarts for it like for a unit.
    web_front_config_changed: bool = False
    # `ummanu-po.service` was rewritten by reconcile (an update; a created unit was just started).
    po_unit_changed: bool = False
    # A regenerated head snapshot is process-local state too: `load_registry` caches per process,
    # so a profile added to the canon is invisible to the running transport until it is replaced.
    head_registry_changed: bool = False
    # Product-pack changes require incremental memory reconciliation.
    memory_pack_changed: bool = False
    memory_pack: Any = None
    # The shipped pack's digest once `step_memory_pack` materialized it; the memory receipt binds it.
    memory_pack_digest: str | None = None
    # Resolve home-relative artifacts from the installation owner, not the invoker.
    runtime_user: str | None = None
    runtime_home: Path | None = None
    project_availability: ProjectAvailability = field(default_factory=ProjectAvailability)
    pull_result: StepResult | None = None
    handoff_before: str | None = None
    handoff_after: str | None = None
    pulled_before: str | None = None
    pulled_after: str | None = None
    # Invocation-owned; replace() shares it, and run_steps closes all such copies on exit.
    source_cut: _SourceCutOwner | None = field(default=None, repr=False, compare=False)


@dataclass
class UpgradeResult:
    steps: list[StepResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(step.failed for step in self.steps)

    @property
    def changed(self) -> bool:
        return any(step.status == "changed" for step in self.steps)

    def render(self) -> str:
        lines = ["ummanu upgrade"]
        for step in self.steps:
            suffix = f": {step.detail}" if step.detail else ""
            lines.append(f"  {step.status:9} {step.name}{suffix}")
        lines.append("status: " + ("ok" if self.ok else "failed"))
        return "\n".join(lines)


class GitError(RuntimeError):
    """A git command failed. The message carries git's reason, not a traceback."""


class EntrypointRefused(GitError):
    """The upstream target does not keep the running entrypoint; the checkout was left as found.

    `refusal` is the `entrypoint_guard.EntrypointMoved` the release path raises for the same target.
    """

    def __init__(self, root: Path, refusal: EntrypointMoved) -> None:
        self.refusal = refusal
        super().__init__(f"{refusal.reason}: {root} stays at its commit: {refusal}")


# Receipts an upgrade writes after it has done a step's work, so the next run can compare the
# installed state with the checkout instead of trusting its own pull delta (secretary-1743). They
# live under `DATA_DIR/upgrade/`, which no backup carries: like the web receipt, each one describes
# this host's venv or process generation and is recreated by the next upgrade.
UPGRADE_RECEIPT_ROOT = Path("upgrade")


def _upgrade_receipt_path(context: UpgradeContext, relative: Path) -> Path:
    data_dir = _data_dir(context)
    if not isinstance(data_dir, Path):
        raise ReceiptError("instance has no resolved data directory")
    return data_dir / relative


def _data_dir(context: UpgradeContext) -> Path | None:
    return getattr(context.report, "data_dir", None)


def _component_unit_prefix(report: Any, component: str) -> str:
    """The installation's unit-name prefix for one component's units.

    Deliberately not closed with a `.`: the web component ships `ummanu-web.service` *and*
    `ummanu-web-front.service`, and both belong to it.
    """
    prefix = report.host.get("unit_prefix", "") if isinstance(report.host, dict) else ""
    return f"{prefix}{component}" if isinstance(prefix, str) else component


def _memory_unit_prefix(report: Any) -> str:
    return f"{_component_unit_prefix(report, MEMORY_COMPONENT)}."


def _process_unit_enabled(context: UpgradeContext, component: str, unit: str) -> bool:
    """Process reconciliation follows the catalogue's component and foreign declarations too."""
    return component_enabled(context.report.host, component) and unit not in foreign_units(
        context.report.host
    )


class WebProcessReceiptError(ReceiptError):
    """The small, private receipt that binds a successful web generation to its inputs."""


def _po_unit(report: Any) -> str:
    return f"{_component_unit_prefix(report, PO_COMPONENT)}.service"
