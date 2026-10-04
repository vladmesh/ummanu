"""``ummanu upgrade``: pull a new product version and re-materialize the host.

One materializer and three entry points into it: ``upgrade`` pulls the new version and then runs
it, a fresh install runs it against an empty host, recovery runs it against a half-built one.
Nothing about a step knows which of the three called it, which is what makes them stay identical.

Every step is idempotent and reports one of ``changed``/``unchanged``/``skipped``/``failed``. A
failed step stops the run: later steps assume the earlier ones landed. ``--dry-run`` runs the
same decisions and performs no writes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pwd
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ummanu import _proc, role_skills, state_repo
from ummanu.board.migrate import migrate_instance
from ummanu.board.provision import provision as provision_board_store
from ummanu.board.provision import verify_roles as verify_board_store_roles
from ummanu.board.store import BoardStoreError, ensure_ignored, store_path
from ummanu.config import DataDirError, validate_instance
from ummanu.dispatch import entrypoint_guard
from ummanu.dispatch.entrypoint_guard import EntrypointMoved
from ummanu.head_registry import (
    HeadRegistryConfigError,
    assert_snapshot_current,
    canonical_heads,
    canonical_path,
    generated_pair,
    installed_heads,
    installed_pair,
    materialize_snapshot,
    record_source,
)
from ummanu.host import (
    FixtureHostSource,
    LiveHostSource,
    build_doctor_expectations,
    build_expectations,
    foreign_units,
    strict_manifest,
)
from ummanu.host_apply import (
    ApplyInputs,
    HostCommandError,
    SystemdUnitInstaller,
    UnitInstaller,
    UnitProcessIdentity,
    _process_start_ticks,
    apply_host,
    resolve_packaged,
    resolve_runtime_owner,
)
from ummanu.memory import DEFAULT_MODEL as DEFAULT_MEMORY_MODEL
from ummanu.memory.client_config import (
    CODEX_HOME_SEEDED_FILES,
    ClientConfigError,
    reconcile_clients,
    reconciled_codex_homes,
)
from ummanu.memory.health import MemoryProbeError, probe_memory
from ummanu.memory.pack import MemoryPackError, load_product_pack, materialize_product_pack
from ummanu.po import client as po_client
from ummanu.po import token as po_token
from ummanu.po import workspace as po_workspace
from ummanu.projects.availability import ProjectAvailability
from ummanu.runtime import interactive_workspace
from ummanu.runtime.paths import add_instance_argument, component_enabled, configured_product_root
from ummanu.runtime_env import RuntimeEnvError, RuntimeEnvMissing, read_runtime_env
from ummanu.web.health import WebProbeError, probe_web, target_from_unit
from ummanu.web.server import LoopbackOnly
from ummanu.webfront.commands import (
    CONFIG_NAME as WEB_FRONT_CONFIG_NAME,
)
from ummanu.webfront.commands import (
    FRONT_DIRNAME as WEB_FRONT_DIRNAME,
)
from ummanu.webfront.commands import (
    SITES_SETTING as WEB_FRONT_SITES_SETTING,
)
from ummanu.webfront.commands import (
    configured_sites,
    missing_sites_message,
)

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
RUFF_VERSION_RE = re.compile(r"^ruff==([^;\s]+)$")


@dataclass
class StepResult:
    name: str
    status: str
    detail: str = ""

    @property
    def failed(self) -> bool:
        return self.status == "failed"


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


def _git(root: Path, args: list[str], timeout: int = 120) -> str:
    try:
        # Scrub inherited Git state so `-C root` selects this checkout.
        result = _proc.run(
            ["git", "-c", f"safe.directory={root}", "-C", str(root), *args],
            timeout=timeout,
            env=state_repo.git_env(),
        )
    except FileNotFoundError:
        raise GitError("git not found") from None
    except subprocess.TimeoutExpired:
        raise GitError(f"git {args[0]} timed out") from None
    except OSError:
        raise GitError(f"git {args[0]} could not run") from None
    if result.returncode != 0:
        raise GitError(f"git {args[0]}: {_git_failure_reason(result.stderr or result.stdout)}")
    return (result.stdout or "").strip()


def _git_failure_reason(output: str) -> str:
    """Git's `fatal:` line, else its last line: the first is often progress (`Preparing worktree`)."""
    lines = [line.strip() for line in (output or "").splitlines() if line.strip()]
    fatal = next((line for line in lines if line.startswith("fatal:")), None)
    return fatal or (lines[-1] if lines else "failed")


def require_entrypoint(root: Path, target: str) -> None:
    """Raise `EntrypointRefused` when moving `root` to `target` would remove the running entrypoint.

    The same check the release path runs (`dispatch.entrypoint_guard`), through this module's Git.
    """

    def probe(args: list[str]) -> str | None:
        try:
            return _git(root, args)
        except GitError:
            return None

    try:
        entrypoint_guard.require_entrypoint(probe, target)
    except EntrypointMoved as exc:
        raise EntrypointRefused(root, exc) from exc


def upstream_target(root: Path, base_branch: str) -> tuple[str, str]:
    """Fetch `base_branch` and pin it. Returns ``(head, target)``; refuses a target that moved the entrypoint."""
    head = _git(root, ["rev-parse", "HEAD"])
    _git(root, ["fetch", "--quiet", "origin", base_branch])
    target = _git(root, ["rev-parse", "--verify", f"origin/{base_branch}^{{commit}}"])
    if target != head:
        require_entrypoint(root, target)
    return head, target


def fast_forward(root: Path, base_branch: str) -> tuple[str, str]:
    """Fetch and fast-forward one checkout. Returns ``(before, after)``.

    Strictly ``--ff-only``: a checkout with local commits or a diverged history is left exactly as
    found and the caller hears why. Nothing in an upgrade may discard work that is only on this host.
    The upstream commit is pinned and checked first: a target without the entrypoint the live units
    execute raises `EntrypointRefused` before anything moves (`docs/RENAME.md` §T1).
    """
    before, target = upstream_target(root, base_branch)
    _git(root, ["merge", "--ff-only", target])
    return before, _git(root, ["rev-parse", "HEAD"])


def _changed_paths(root: Path, before: str, after: str) -> tuple[str, ...]:
    if before == after:
        return ()
    return tuple(_git(root, ["diff", "--name-only", f"{before}..{after}"]).splitlines())


def _touches(changed: tuple[str, ...], prefixes: tuple[str, ...]) -> bool:
    return any(path.startswith(prefix) for path in changed for prefix in prefixes)


def planned_unit_names(changed: tuple[str, ...], name_prefix: str) -> tuple[str, ...]:
    """The shipped units under ``name_prefix`` a revision move edits, by unit file name.

    `step_host` answers the same question from the live host, and on the apply path the two agree,
    because by then the checkout has moved and reconcile has seen the new bytes. Under `--dry-run`
    only this one can answer it: the files are still the old ones on disk, and the change exists
    only in the diff against the target revision.
    """
    return tuple(
        name
        for path in changed
        if path.startswith(PACKAGED_UNIT_ROOT)
        for name in (path[len(PACKAGED_UNIT_ROOT) :],)
        if "/" not in name and name.startswith(name_prefix)
    )


def record_change_plan(context: UpgradeContext, changed: tuple[str, ...]) -> None:
    """Turn one revision move into the facts every later step reads. The only place that happens.

    Three entry points reach the same schedule — a fresh `pull`, the re-executed pulled schedule
    arriving through its handoff marker, and `--dry-run` planning against the upstream target — and
    each of them used to derive its own subset of these facts. `--dry-run` derived none at all, so
    it returned before it knew anything and no later step could name a pending action; the handoff
    derived `code_changed` and silently left `schemas_changed` false. Both were one defect with
    three spellings, so there is now one recorder and the entry points only supply the path set.
    """
    context.changed_paths = changed
    context.code_changed = _touches(changed, MEMORY_CODE_PATHS)
    context.schemas_changed = _touches(changed, SCHEMA_PATHS)


def step_pull(context: UpgradeContext) -> StepResult:
    if context.pull_result is not None:
        return context.pull_result
    if not context.pull:
        if context.handoff_before and context.handoff_after:
            return StepResult(
                "pull",
                "changed",
                f"{context.handoff_before[:12]} -> {context.handoff_after[:12]}; running pulled schedule",
            )
        return StepResult("pull", "skipped", "--no-pull")
    try:
        dirty = _git(context.product_root, ["status", "--porcelain"])
        if dirty:
            return StepResult("pull", "failed", "product checkout has uncommitted changes")
        if context.dry_run:
            # Plan against the target revision and write nothing: `fetch` and `diff` read, and the
            # checkout is left exactly where it was. This is what makes every later step's
            # `would-change` line about the revision an operator is about to apply rather than
            # about the one already installed.
            head, target = upstream_target(context.product_root, context.base_branch)
            record_change_plan(context, _changed_paths(context.product_root, head, target))
            if head == target:
                return StepResult("pull", "unchanged", head[:12])
            return StepResult("pull", "changed", f"{head[:12]} -> {target[:12]} (not applied)")
        before, after = fast_forward(context.product_root, context.base_branch)
        changed = _changed_paths(context.product_root, before, after)
    except GitError as exc:
        return StepResult("pull", "failed", str(exc))
    if before == after:
        return StepResult("pull", "unchanged", after[:12])
    record_change_plan(context, changed)
    context.pulled_before = before
    context.pulled_after = after
    return StepResult("pull", "changed", f"{before[:12]} -> {after[:12]}")


def _snapshot_install(venv_python: Path) -> bool:
    """Is the product installed into this venv as a copy rather than as the checkout itself?

    A snapshot install is a silent liability: nothing that follows moves it, so the venv keeps
    answering with whatever the code looked like when it was taken. An editable install cannot drift
    that way, so finding a snapshot is itself a reason to reinstall, whether or not a dependency
    manifest moved.
    """
    for dist_info in (venv_python.parent.parent / "lib").glob("python*/site-packages/ummanu-*.dist-info"):
        try:
            direct_url = json.loads((dist_info / "direct_url.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return True
        return not bool(direct_url.get("dir_info", {}).get("editable"))
    return True


def _declared_ruff_version(product_root: Path) -> str:
    """Read the exact Ruff version the product's ``dev`` extra promises."""
    try:
        pyproject = tomllib.loads((product_root / "pyproject.toml").read_text(encoding="utf-8"))
        dependencies = pyproject["project"]["optional-dependencies"]["dev"]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read the dev Ruff pin: {exc}") from None
    if not isinstance(dependencies, list):
        raise TypeError("the dev extra is not a dependency list")
    for dependency in dependencies:
        if isinstance(dependency, str) and (match := RUFF_VERSION_RE.fullmatch(dependency)):
            return match.group(1)
    raise ValueError("the dev extra declares no exact Ruff pin")


def _ruff_runtime_problem(product_root: Path, venv_python: Path) -> str:
    """Why this venv cannot run the Ruff version its checkout declares, if any."""
    try:
        expected = _declared_ruff_version(product_root)
    except (TypeError, ValueError) as exc:
        return str(exc)
    executable = venv_python.parent / "ruff"
    if not executable.is_file():
        return f"the pinned Ruff {expected} is missing"
    try:
        result = _proc.run([str(executable), "--version"], timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return f"the pinned Ruff {expected} cannot run"
    if result.returncode != 0:
        return f"the pinned Ruff {expected} cannot run"
    actual = (result.stdout or result.stderr).strip()
    if actual != f"ruff {expected}":
        return f"the installed Ruff reports {actual or 'no version'}, not {expected}"
    return ""


# Receipts an upgrade writes after it has done a step's work, so the next run can compare the
# installed state with the checkout instead of trusting its own pull delta (secretary-1743). They
# live under `DATA_DIR/upgrade/`, which no backup carries: like the web receipt, each one describes
# this host's venv or process generation and is recreated by the next upgrade.
UPGRADE_RECEIPT_ROOT = Path("upgrade")
DEPENDENCY_RECEIPT_RELATIVE = UPGRADE_RECEIPT_ROOT / "dependency-receipt.json"
DEPENDENCY_RECEIPT_VERSION = 1
# A receipt is a few hundred bytes; anything much larger is not one of ours.
RECEIPT_MAX_BYTES = 64 * 1024
SHA256_RE = re.compile(r"[0-9a-f]{64}")
EXTRA_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
REQUIREMENT_NAME_RE = re.compile(r"\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)")


class ReceiptError(RuntimeError):
    """A receipt or one of the inputs it binds cannot be read or written."""


def _upgrade_receipt_path(context: UpgradeContext, relative: Path) -> Path:
    data_dir = _data_dir(context)
    if not isinstance(data_dir, Path):
        raise ReceiptError("instance has no resolved data directory")
    return data_dir / relative


def _load_receipt(path: Path, label: str) -> tuple[dict[str, Any] | None, str]:
    """Read one receipt as a JSON object, totally: every way it can be wrong is "no receipt"."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None, f"the {label} is missing"
    except OSError as exc:
        return None, f"the {label} cannot be read: {exc}"
    if not stat.S_ISREG(info.st_mode) or info.st_size > RECEIPT_MAX_BYTES:
        return None, f"the {label} is malformed"
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as source:
            raw = source.read(RECEIPT_MAX_BYTES + 1)
        payload = json.loads(raw.decode("utf-8")) if len(raw) <= RECEIPT_MAX_BYTES else None
    except (OSError, UnicodeError, ValueError, RecursionError):
        return None, f"the {label} is malformed"
    if not isinstance(payload, dict):
        return None, f"the {label} is malformed"
    return payload, ""


def _normalized_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _declared_extras(product_root: Path) -> dict[str, frozenset[str]]:
    """The checkout's optional extras, each as the distribution names it requires."""
    try:
        pyproject = tomllib.loads((product_root / "pyproject.toml").read_text(encoding="utf-8"))
        declared = pyproject["project"]["optional-dependencies"]
    except (OSError, UnicodeError, KeyError, TypeError, tomllib.TOMLDecodeError):
        return {}
    if not isinstance(declared, dict):
        return {}
    extras = {}
    for name, requirements in declared.items():
        if not isinstance(name, str) or not isinstance(requirements, list):
            continue
        extras[_normalized_name(name)] = frozenset(
            _normalized_name(match.group(1))
            for requirement in requirements
            if isinstance(requirement, str) and (match := REQUIREMENT_NAME_RE.match(requirement))
        )
    return extras


def _venv_distributions(venv: Path) -> frozenset[str]:
    """The distribution names installed into ``venv``, read from its ``*.dist-info`` directories."""
    return frozenset(
        _normalized_name(dist_info.name.split("-", 1)[0])
        for dist_info in (venv / "lib").glob("python*/site-packages/*.dist-info")
    )


def required_extras(context: UpgradeContext, venv: Path) -> tuple[str, ...]:
    """The extras `dependencies` installs: ``dev`` plus every extra this installation uses.

    Which extras an installation uses is a property of the installation, not of the manifest, and
    this is the one place that rule lives. ``memory`` is used when the memory unit is installed or
    active, because `ummanu-memory-mcp` runs from this venv and its pins are that extra's. Any
    declared extra is used when the venv already carries all of its distributions, so a reinstall
    never drops what an operator added. An extra the checkout does not declare is never asked for.
    """
    declared = _declared_extras(context.product_root)
    extras = {"dev"}
    unit = f"{_memory_unit_prefix(context.report)}service"
    if MEMORY_COMPONENT in declared and (
        context.units.installed(unit) is not None or context.units.is_active(unit)
    ):
        extras.add(MEMORY_COMPONENT)
    carried = _venv_distributions(venv)
    extras.update(
        name for name, distributions in declared.items() if distributions and distributions <= carried
    )
    return tuple(sorted(extras))


def _read_dependency_receipt(context: UpgradeContext, venv: Path) -> tuple[dict[str, Any] | None, str]:
    try:
        path = _upgrade_receipt_path(context, DEPENDENCY_RECEIPT_RELATIVE)
    except ReceiptError as exc:
        return None, f"the dependency receipt cannot be located: {exc}"
    payload, reason = _load_receipt(path, "dependency receipt")
    if payload is None:
        return None, reason
    malformed = "the dependency receipt is malformed"
    if set(payload) != {"version", "venv", "dependency_sha256", "extras"}:
        return None, malformed
    version = payload["version"]
    digest = payload["dependency_sha256"]
    extras = payload["extras"]
    if type(version) is not int or version != DEPENDENCY_RECEIPT_VERSION:
        return None, malformed
    if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
        return None, malformed
    if (
        not isinstance(extras, list)
        or len(extras) > 64
        or not all(isinstance(extra, str) and EXTRA_NAME_RE.fullmatch(extra) for extra in extras)
        or len(set(extras)) != len(extras)
    ):
        return None, malformed
    if not isinstance(payload["venv"], str):
        return None, malformed
    if payload["venv"] != str(venv.resolve()):
        return None, "the dependency receipt names another venv"
    return {"dependency_sha256": digest, "extras": tuple(sorted(extras))}, ""


def _write_dependency_receipt(
    context: UpgradeContext, venv: Path, digest: str, extras: tuple[str, ...]
) -> None:
    path = _upgrade_receipt_path(context, DEPENDENCY_RECEIPT_RELATIVE)
    payload = {
        "version": DEPENDENCY_RECEIPT_VERSION,
        "venv": str(venv.resolve()),
        "dependency_sha256": digest,
        "extras": list(extras),
    }
    _write_private_receipt(path, payload, context.runtime_user, ReceiptError, "dependency receipt")


def step_dependencies(context: UpgradeContext) -> StepResult:
    """Install the product into its venv when the venv does not match the checkout.

    The question is the venv's state, not this run's pull delta: a checkout moved by hand, by the
    dispatcher or by `--no-pull` has an empty delta and a stale venv all the same. So the step
    compares the dependency receipt the last successful install wrote — the tracked dependency
    manifests' digest, the extras and the venv — with the checkout and this installation's extras.
    A missing or unreadable receipt is a reason to install, never `unchanged`.
    """
    venv = context.product_root / ".venv"
    venv_python = venv / "bin" / "python"
    if not venv_python.is_file():
        return StepResult("dependencies", "skipped", "no .venv in the product checkout")
    try:
        digest = _git_tracked_digest(context.product_root, DEPENDENCY_PATHS)
    except ReceiptError as exc:
        return StepResult("dependencies", "failed", f"cannot compare the venv with the checkout: {exc}")
    extras = required_extras(context, venv)
    listed = ",".join(extras)
    compared = f"deps sha256 {digest[:12]}, extras {listed}"
    reasons = []
    receipt, receipt_reason = _read_dependency_receipt(context, venv)
    if receipt is None:
        reasons.append(receipt_reason)
    else:
        if receipt["dependency_sha256"] != digest:
            reasons.append(f"deps sha256 {receipt['dependency_sha256'][:12]} -> {digest[:12]}")
        if receipt["extras"] != extras:
            reasons.append(f"extras {','.join(receipt['extras']) or 'none'} -> {listed}")
    if _snapshot_install(venv_python):
        reasons.append("the venv holds a snapshot install")
    # Under `--dry-run` the checkout has not moved yet, so the digest cannot see a pending manifest
    # change; the plan can.
    if _touches(context.changed_paths, DEPENDENCY_PATHS):
        reasons.append("a dependency manifest moved")
    ruff_problem = _ruff_runtime_problem(context.product_root, venv_python)
    if ruff_problem:
        reasons.append(ruff_problem)
    if not reasons:
        return StepResult("dependencies", "unchanged", f"venv matches checkout ({compared})")
    reason = "; ".join(reasons)
    if context.dry_run:
        return StepResult(
            "dependencies", "changed", f"would reinstall the product into .venv ({compared}): {reason}"
        )
    try:
        _proc.run(
            [
                str(venv_python),
                "-m",
                "pip",
                "install",
                "--quiet",
                "-e",
                f"{context.product_root}[{listed}]",
            ],
            timeout=900,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        return StepResult("dependencies", "failed", f"pip install exited {exc.returncode}")
    except (OSError, subprocess.TimeoutExpired):
        return StepResult("dependencies", "failed", "pip install could not run")
    context.code_changed = True
    try:
        _write_dependency_receipt(context, venv, digest, extras)
    except ReceiptError as exc:
        return StepResult("dependencies", "failed", f"installed the product into .venv but {exc}")
    return StepResult(
        "dependencies",
        "changed",
        f"installed the product into .venv ({compared}) and wrote the dependency receipt: {reason}",
    )


def step_dependency_provenance(context: UpgradeContext) -> StepResult:
    """Prove core imports come from the selected production installation environment."""
    venv = context.product_root / ".venv"
    python = venv / "bin" / "python"
    if not python.is_file():
        path = store_path(context.instance_path)
        if path.exists() or path.is_symlink():
            return StepResult(
                "dependency-provenance",
                "failed",
                "a configured board store requires the selected product checkout's production venv",
            )
        return StepResult("dependency-provenance", "skipped", "no .venv in the product checkout")
    program = (
        "import importlib.util,json,pathlib,sys;"
        "names=('ummanu','psycopg','sqlalchemy','alembic');"
        "print(json.dumps({'prefix':sys.prefix,'origins':{n:str(pathlib.Path(importlib.util.find_spec(n).origin).resolve()) for n in names}}))"
    )
    try:
        completed = _proc.run([str(python), "-P", "-c", program], timeout=60)
        if completed.returncode:
            raise ValueError("the import probe failed")
        evidence = json.loads(completed.stdout)
        origins = evidence["origins"]
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return StepResult(
            "dependency-provenance",
            "failed",
            "could not import ummanu, psycopg, SQLAlchemy and Alembic from the production venv",
        )
    root = context.product_root.resolve()
    try:
        Path(origins["ummanu"]).resolve().relative_to(root)
        for name in ("psycopg", "sqlalchemy", "alembic"):
            Path(origins[name]).resolve().relative_to(venv.resolve())
    except (ValueError, TypeError):
        return StepResult(
            "dependency-provenance",
            "failed",
            "production imports escaped the selected product root or its venv",
        )
    detail = ", ".join(f"{name}={origins[name]}" for name in origins)
    return StepResult("dependency-provenance", "unchanged", detail)


def step_memory_clients(context: UpgradeContext) -> StepResult:
    """Materialize PO bridge entries without changing provider login state."""
    if not (context.product_root / ".venv").is_dir():
        return StepResult("memory-clients", "skipped", "no .venv in the product checkout")
    if context.runtime_home is None:
        return StepResult("memory-clients", "failed", "installation runtime home is unresolved")
    data_dir = context.report.data_dir
    if data_dir is None:
        return StepResult("memory-clients", "failed", "instance data directory is unresolved")
    try:
        result = reconcile_clients(
            context.product_root,
            context.runtime_home,
            data_dir,
            dry_run=context.dry_run,
        )
    except ClientConfigError as exc:
        return StepResult("memory-clients", "failed", str(exc))
    if not context.dry_run:
        data_homes = reconciled_codex_homes(data_dir)
        # A data-dir home may have been seeded here; the home itself and its login are left as found.
        seeded = (home / name for home in data_homes for name in CODEX_HOME_SEEDED_FILES)
        user_codex = context.runtime_home / ".codex" / "config.toml"
        claude = context.runtime_home / ".claude.json"
        try:
            _set_runtime_directory_owner(user_codex.parent, context.runtime_user)
            for path in (*seeded, user_codex, claude):
                _set_runtime_owner(path, context.runtime_user)
        except GitError as exc:
            return StepResult("memory-clients", "failed", str(exc))
    if not result.changed:
        return StepResult("memory-clients", "unchanged", "Claude and Codex PO bridge entries current")
    action = "would reconcile" if context.dry_run else "reconciled"
    return StepResult("memory-clients", "changed", f"{action} {result.changed} client config(s)")


def step_codex_home(context: UpgradeContext) -> StepResult:
    """Seed the non-secret Codex runtime files into `<data_dir>/codex-home`, the one CODEX_HOME the
    installation manages; the PO's `codex login` there is the only other step it needs."""
    if not (context.product_root / "packaging" / "codex-home").is_dir():
        return StepResult("codex-home", "skipped", "no packaging/codex-home in the product checkout")
    data_dir = _data_dir(context)
    if data_dir is None:
        return StepResult("codex-home", "failed", "instance data directory is unresolved")
    if context.dry_run:
        return StepResult("codex-home", "skipped", "--dry-run does not seed CODEX_HOME")
    # installation imports this module; the seeding lives there with install's own call of it.
    from ummanu.installation import InstallError, provision_codex_home

    try:
        seeded = provision_codex_home(
            context.product_root,
            context.runtime_user,
            data_dir=data_dir,
        )
    except InstallError as exc:
        return StepResult("codex-home", "failed", str(exc))
    if not seeded:
        return StepResult("codex-home", "unchanged", "CODEX_HOME files current")
    return StepResult("codex-home", "changed", f"seeded {seeded} CODEX_HOME file(s)")


def step_po_workspace(context: UpgradeContext) -> StepResult:
    """Materialize the PO head's working directory; its notes file is never rewritten.

    Ownership is handed over by `step_po_workspace_owner`, after the skills are delivered into it.
    """
    if not (context.product_root / ".venv").is_dir():
        return StepResult("po-workspace", "skipped", "no .venv in the product checkout")
    data_dir = _data_dir(context)
    if data_dir is None:
        return StepResult("po-workspace", "failed", "instance data directory is unresolved")
    try:
        result = po_workspace.materialize(context.product_root, data_dir, dry_run=context.dry_run)
    except po_workspace.WorkspaceError as exc:
        return StepResult("po-workspace", "failed", str(exc))
    if not result.changed:
        return StepResult("po-workspace", "unchanged", f"{result.path} current")
    action = "would write" if context.dry_run else "wrote"
    return StepResult("po-workspace", "changed", f"{action} {', '.join(result.changed)} in {result.path}")


def step_po_workspace_owner(context: UpgradeContext) -> StepResult:
    """Hand the whole PO workspace to the runtime user once role-skills has delivered into it.

    Runs on every upgrade rather than on change: a root invoker creates the skill roots and their
    copies after `po-workspace`, and a tree left root-owned by an earlier run is repaired here.
    """
    data_dir = _data_dir(context)
    if data_dir is None:
        return StepResult("po-workspace-owner", "skipped", "instance data directory is unresolved")
    workspace = po_workspace.workspace_dir(data_dir)
    if context.dry_run or not workspace.is_dir():
        return StepResult("po-workspace-owner", "skipped", f"no {workspace} to hand over")
    if not context.runtime_user or os.geteuid() != 0:
        return StepResult(
            "po-workspace-owner", "skipped", "not a root invoker; files already belong to the caller"
        )
    try:
        _set_runtime_owner(workspace, context.runtime_user)
    except GitError as exc:
        return StepResult("po-workspace-owner", "failed", str(exc))
    return StepResult("po-workspace-owner", "unchanged", f"{workspace} owned by {context.runtime_user}")


def step_interactive_workspace(context: UpgradeContext) -> StepResult:
    """Materialize the interactive head's working directory and hand it to the runtime user.

    `AGENTS.md` is the product's shared part followed by the live root's `persona/AGENTS.md`; recover
    runs this same step against the live root it extracted. Nothing else receives the persona.
    """
    source = interactive_workspace.shared_source(context.product_root)
    if not source.exists() and not source.is_symlink():
        return StepResult(
            "interactive-workspace", "skipped", f"no {interactive_workspace.SHARED_SOURCE_RELATIVE} in the product checkout"
        )
    data_dir = _data_dir(context)
    if data_dir is None:
        return StepResult("interactive-workspace", "failed", "instance data directory is unresolved")
    try:
        result = interactive_workspace.materialize(
            context.product_root, context.instance_path, data_dir, dry_run=context.dry_run
        )
    except interactive_workspace.WorkspaceError as exc:
        return StepResult("interactive-workspace", "failed", str(exc))
    if not context.dry_run:
        try:
            _set_runtime_owner(result.path, context.runtime_user)
        except GitError as exc:
            return StepResult("interactive-workspace", "failed", str(exc))
    sources = f"shared {result.shared}, personal {result.personal or 'absent'}"
    if not result.changed:
        return StepResult("interactive-workspace", "unchanged", f"{result.path} current ({sources})")
    action = "would write" if context.dry_run else "wrote"
    return StepResult(
        "interactive-workspace",
        "changed",
        f"{action} {', '.join(result.changed)} in {result.path} ({sources})",
    )


def step_po_token(context: UpgradeContext) -> StepResult:
    """Create `DATA_DIR/po-web-token` (0600, runtime user) if absent; an existing token is never rewritten.

    Rotation is deleting the file and running this step again.
    """
    data_dir = _data_dir(context)
    if data_dir is None:
        return StepResult("po-token", "skipped", "instance data directory is unresolved")
    path = po_token.token_path(data_dir)
    try:
        created = po_token.ensure_token(data_dir, dry_run=context.dry_run)
        if not context.dry_run:
            _set_runtime_owner(path, context.runtime_user)
    except (po_token.TokenError, GitError) as exc:
        return StepResult("po-token", "failed", str(exc))
    if created:
        action = "would create" if context.dry_run else "created"
        return StepResult("po-token", "changed", f"{action} {path} (mode 0600)")
    return StepResult("po-token", "unchanged", f"{path} exists; never rewritten")


def _data_dir(context: UpgradeContext) -> Path | None:
    return getattr(context.report, "data_dir", None)


def _role_skills_manifest(context: UpgradeContext) -> Path:
    """The skill registry of the checkout being installed, which is not always the running one."""
    return role_skills.product_manifest_path(context.product_root)


def step_registries(context: UpgradeContext) -> StepResult:
    """Read every registry this upgrade materializes from, before anything is written.

    The steps that follow write in order — head snapshot, role worktrees, skills and entry points,
    host — and each reads operator-written configuration that can be malformed, so finding that out
    at the third write leaves a host half-moved. Parsing is not enough for the skill registry: a
    manifest whose declared ``SKILL.md`` is absent, whose target roots overlap, or whose entry point
    collides parses cleanly and is refused by `sync` after the head snapshot has been written, so the
    whole plan is decided here against the same manifests and home the later steps use.
    """
    manifest = _role_skills_manifest(context)
    try:
        registry = role_skills.load_registry(context.instance_path, product_manifest=manifest)
        data_dir = role_skills.resolve_data_dir(registry, context.instance_path, _data_dir(context))
        problems = role_skills.unmaterializable(registry, context.runtime_home, data_dir=data_dir)
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        return StepResult("registries", "failed", f"skill registry: {exc}")
    if problems:
        return StepResult(
            "registries",
            "failed",
            f"skill registry: {problems[0]}",
        )
    try:
        canonical, _ = canonical_path(context.product_root, context.instance_path)
        canonical_heads(context.product_root, context.instance_path)
    except HeadRegistryConfigError as exc:
        return StepResult("registries", "failed", str(exc))
    try:
        context.memory_pack = load_product_pack(context.product_root)
    except MemoryPackError as exc:
        return StepResult("registries", "failed", f"memory pack: {exc}")
    sources = ", ".join(str(source.path) for source in registry.sources)
    return StepResult("registries", "unchanged", f"{sources}, {canonical}, and memory pack are readable")


def step_memory_pack(context: UpgradeContext) -> StepResult:
    """Materialize the shipped pack only after `registries` validated its source."""
    pack = context.memory_pack
    try:
        if pack is None:
            pack = load_product_pack(context.product_root)
        data_dir = context.report.data_dir
        if data_dir is None:
            return StepResult("memory-pack", "failed", "instance has no resolved data directory")
        result = materialize_product_pack(
            pack,
            instance_dir=context.instance_path,
            data_dir=data_dir,
            dry_run=context.dry_run,
            runtime_handoff=lambda path: _set_runtime_owner(path, context.runtime_user),
            runtime_export_check=lambda memory_dir: _assert_memory_export_readable(
                memory_dir, context.runtime_user
            ),
        )
    except MemoryPackError as exc:
        return StepResult("memory-pack", "failed", str(exc))
    context.memory_pack_changed = result.changed
    context.memory_pack_digest = pack.digest
    if not result.changed:
        return StepResult("memory-pack", "unchanged", "installed digest matches product pack")
    verb = "would reconcile" if context.dry_run else "reconciled"
    return StepResult(
        "memory-pack",
        "changed",
        f"{verb} {result.added} added, {result.updated} updated, {result.deleted} deleted, {result.retained} retained",
    )


def step_role_skills(context: UpgradeContext) -> StepResult:
    """Materialize the product skills of the checkout being installed, plus this installation's."""
    manifest = _role_skills_manifest(context)
    try:
        before = role_skills.audit(
            instance_path=context.instance_path,
            product_manifest=manifest,
            home=context.runtime_home,
            data_dir=_data_dir(context),
        )
    except (OSError, ValueError) as exc:
        return StepResult("role-skills", "failed", str(exc))
    if before["ok"]:
        if not context.dry_run:
            try:
                _hand_role_skill_dirs_to_runtime_user(context, manifest)
            except (GitError, OSError, ValueError) as exc:
                return StepResult("role-skills", "failed", str(exc))
        return StepResult("role-skills", "unchanged", f"{len(before['targets'])} targets in sync")
    pending = len(before["missing"]) + len(before["drift"]) + len(before["entry_points"])
    pending += len(before.get("retired", []))
    if before["config_errors"] or before["source_missing"]:
        return StepResult(
            "role-skills", "failed", "manifest is not usable: overlapping roots or a missing source skill"
        )
    if context.dry_run:
        return StepResult("role-skills", "changed", f"would sync {pending} skill copies")
    try:
        after = role_skills.sync(
            instance_path=context.instance_path,
            product_manifest=manifest,
            home=context.runtime_home,
            data_dir=_data_dir(context),
        )
    except (OSError, ValueError) as exc:
        return StepResult("role-skills", "failed", str(exc))
    if not after["after"]["ok"]:
        return StepResult("role-skills", "failed", "sync ran but the audit is still red")
    try:
        _hand_role_skill_dirs_to_runtime_user(context, manifest)
    except (GitError, OSError, ValueError) as exc:
        return StepResult("role-skills", "failed", str(exc))
    return StepResult("role-skills", "changed", f"synced {pending} skill copies")


def _hand_role_skill_dirs_to_runtime_user(context: UpgradeContext, manifest: Path) -> None:
    """Give the runtime user every skill root, and the directories above it a root sync created.

    Under sudo, `sync` creates `~/.claude/skills`, `~/.hermes/skills`, the Codex runtime home and the
    entry points' directory as root. Each root is handed over whole; its ancestors one by one, up to
    the home or data directory that contains them and never that directory itself.
    """
    if not context.runtime_user or os.geteuid() != 0:
        return
    registry = role_skills.load_registry(context.instance_path, product_manifest=manifest)
    data_dir = role_skills.resolve_data_dir(registry, context.instance_path, _data_dir(context))
    home = context.runtime_home or Path.home()
    bounds = tuple(Path(bound) for bound in (home, data_dir) if bound is not None)
    roots = {
        root for root in role_skills.target_roots(registry, context.runtime_home, data_dir).values() if root
    }
    for root in sorted(roots):
        _set_runtime_owner(root, context.runtime_user)
        _set_runtime_ancestors_owner(root, bounds, context.runtime_user)
    for command in role_skills.iter_expected_commands(registry, context.runtime_home):
        _set_runtime_ancestors_owner(command.dest, bounds, context.runtime_user)


def _set_runtime_ancestors_owner(path: Path, bounds: tuple[Path, ...], runtime_user: str) -> None:
    """Assign the real directories between `path` and the innermost bound containing it."""
    bound = max(
        (bound for bound in bounds if bound in path.parents), key=lambda b: len(b.parts), default=None
    )
    if bound is None:
        return
    for parent in path.parents:
        if parent == bound:
            return
        if parent.is_symlink() or not parent.is_dir():
            continue
        _set_runtime_directory_owner(parent, runtime_user)


def step_head_registry(context: UpgradeContext) -> StepResult:
    """Keep the installation snapshot derived from whichever registry is this host's canon.

    The installation's own ``heads/heads.toml`` when it owns one, else the product's portable
    default. The pin next to the snapshot records which of the two won, plus the checkout and
    revision, and the live tick validates the pin against the snapshot. Both are generated state in
    ``<data>/heads/``: written here, never committed or pushed (docs/RECOVERY.md).
    """
    try:
        pair = generated_pair(context.instance_path, _data_dir(context))
        canonical, _ = canonical_path(context.product_root, context.instance_path)
        changed = materialize_snapshot(
            context.instance_path,
            context.product_root,
            dry_run=context.dry_run,
            data_dir=_data_dir(context),
        )
        repinned = record_source(
            context.instance_path,
            context.product_root,
            dry_run=context.dry_run,
            data_dir=_data_dir(context),
        )
        if not context.dry_run:
            # A root-run upgrade may have created the directory: the installation account reads it.
            _set_runtime_owner(pair.snapshot.parent, context.runtime_user)
    except (HeadRegistryConfigError, GitError) as exc:
        return StepResult("head-registry", "failed", str(exc))
    target = pair.snapshot
    # The snapshot, not the pin: the pin records which canon won and where the checkout is, while
    # `heads.yaml` is the file a running process actually read and cached.
    context.head_registry_changed = bool(changed)
    if not changed and not repinned:
        return StepResult("head-registry", "unchanged", f"{target} matches {canonical}")
    verb = "would regenerate" if context.dry_run else "regenerated"
    what = target if changed else pair.source
    if changed and repinned:
        what = f"{target} and {pair.source}"
    return StepResult("head-registry", "changed", f"{verb} {what}")


def step_instance_packing(context: UpgradeContext) -> StepResult:
    """Keep the private instance repo's packing controls bounded and local."""
    from ummanu.checkpoint import live_root_is_work_tree

    if not live_root_is_work_tree(context.instance_path):
        # An exporter-mode live root has no repository to pack; the snapshot repository is bare.
        return StepResult("instance-packing", "skipped", "the live root is not a Git work tree")
    try:
        drifted = state_repo.configure_packing_controls(context.instance_path, dry_run=context.dry_run)
    except state_repo.StateRepoError as exc:
        return StepResult("instance-packing", "failed", str(exc))
    if not drifted:
        return StepResult("instance-packing", "unchanged", "local Git packing controls match")
    action = "would set" if context.dry_run else "set"
    return StepResult(
        "instance-packing",
        "changed",
        f"{action} local Git packing controls: {', '.join(drifted)}",
    )


class AgentSpecsError(RuntimeError):
    """The product's declaration of its background agents' specs is broken."""


# The product declares where its background agents' specs live in its own `pyproject.toml`:
# the agents are a package on top of `ummanu`, so `ummanu` reads their location from the
# product's manifest instead of naming the package (sprint:1455, secretary-1689).
AGENT_SPECS_KEY = "agent-specs"


def agents_root(product_root: Path) -> Path | None:
    """The directory of ``<agent>/automation.toml`` specs this product declares, or None.

    A product tree without a manifest, or whose manifest declares no specs, ships no agents —
    as a tree without the directory always did. A manifest that cannot be parsed, or declares a
    path outside the product, is a broken product rather than an empty one.
    """
    manifest = product_root / "pyproject.toml"
    try:
        raw = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise AgentSpecsError(
            f"{manifest}: product manifest is unreadable: {exc.__class__.__name__}"
        ) from None
    tool = raw.get("tool")
    table = tool.get("ummanu") if isinstance(tool, dict) else None
    declared = table.get(AGENT_SPECS_KEY) if isinstance(table, dict) else None
    if declared is None:
        return None
    relative = Path(declared) if isinstance(declared, str) else None
    if relative is None or not declared or relative.is_absolute() or ".." in relative.parts:
        raise AgentSpecsError(
            f"{manifest}: [tool.ummanu] {AGENT_SPECS_KEY} must be a path inside the product"
        )
    return product_root / relative


def workspaces_root(home: Path | str | None = None) -> Path:
    """Where role workspaces live: the configured root, else under the named home.

    ``home`` is the installation owner's, which is not the invoking process's when a repair runs
    as root or against another account's installation. Materializing root's workspace paths for
    units the owner then runs is how a workspace ends up somewhere nothing materialized.
    """
    configured = os.environ.get("TA_WORKSPACES_ROOT")
    if configured:
        return Path(configured)
    return Path(home if home is not None else Path.home()) / "orca" / "workspaces"


def desired_role_worktrees(product_root: Path, home: Path | None = None) -> list[Path]:
    """Every derived role worktree shipped by this product, present or absent."""
    root = workspaces_root(home) / "ummanu"
    agents = agents_root(product_root)
    if agents is None:
        return []
    try:
        names = sorted(entry.name for entry in agents.iterdir() if (entry / "automation.toml").is_file())
    except OSError:
        return []
    return [root / name for name in names]


def _set_runtime_owner(path: Path, runtime_user: str | None) -> None:
    """Repair root-created runtime files without traversing links or hardlinks."""
    if not runtime_user or os.geteuid() != 0 or not path.exists():
        return
    try:
        account = pwd.getpwnam(runtime_user)
    except KeyError:
        raise GitError(f"runtime user {runtime_user!r} does not exist") from None

    def assign(candidate: Path) -> None:
        info = candidate.lstat()
        if stat.S_ISLNK(info.st_mode) or (stat.S_ISREG(info.st_mode) and info.st_nlink > 1):
            return
        os.chown(candidate, account.pw_uid, account.pw_gid, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode):
            return
        with os.scandir(candidate) as children:
            for child in children:
                assign(Path(child.path))

    try:
        assign(path)
    except OSError as exc:
        raise GitError(f"could not assign {path} to runtime user {runtime_user}: {exc}") from None


def _assert_memory_export_readable(memory_dir: Path, runtime_user: str | None) -> None:
    """Require the daemon account to own readable export files before activation."""
    # Root publication must prove runtime-account ownership.
    if not runtime_user or os.geteuid() != 0:
        return
    try:
        account = pwd.getpwnam(runtime_user)
    except KeyError:
        raise GitError(f"runtime user {runtime_user!r} does not exist") from None
    for name in ("export.ndjson", "export.json", "manifest.json"):
        path = memory_dir / name
        try:
            info = path.lstat()
        except OSError as exc:
            raise GitError(f"could not inspect memory export {path}: {exc}") from None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise GitError(f"memory export is not a regular file: {path}")
        if info.st_uid != account.pw_uid or not info.st_mode & stat.S_IRUSR:
            raise GitError(f"memory export is not readable by runtime user {runtime_user}: {path}")


def _set_runtime_directory_owner(path: Path, runtime_user: str | None) -> None:
    """Make a created workspace ancestor traversable and writable without walking siblings."""
    if not runtime_user or os.geteuid() != 0 or not path.exists():
        return
    try:
        account = pwd.getpwnam(runtime_user)
    except KeyError:
        raise GitError(f"runtime user {runtime_user!r} does not exist") from None
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise GitError(f"workspace ancestor is not a real directory: {path}")
        os.chown(path, account.pw_uid, account.pw_gid, follow_symlinks=False)
    except OSError as exc:
        raise GitError(f"could not assign {path} to runtime user {runtime_user}: {exc}") from None


def _workspace_owner_dirs(worktree: Path) -> tuple[Path, ...]:
    """The exact parents this materializer can create for a role worktree."""
    ummanu_root = worktree.parent
    workspace_root = ummanu_root.parent
    roots = [workspace_root, ummanu_root]
    if workspace_root.name == "workspaces" and workspace_root.parent.name == "orca":
        roots.insert(0, workspace_root.parent)
    return tuple(roots)


def _registered_worktree(product_root: Path, worktree: Path) -> bool:
    """Whether the product's Git already has a linked worktree registered at `worktree`."""
    listing = _git(product_root, ["worktree", "list", "--porcelain"])
    wanted = worktree.resolve(strict=False)
    return any(
        Path(line.removeprefix("worktree ")).resolve(strict=False) == wanted
        for line in listing.splitlines()
        if line.startswith("worktree ")
    )


def _worktree_git_dir(worktree: Path) -> Path | None:
    """Return the linked-worktree administrative directory named by its .git file."""
    try:
        line = (worktree / ".git").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    if not line.startswith("gitdir: "):
        return None
    return Path(line.removeprefix("gitdir: ")).expanduser().resolve()


def step_worktrees(context: UpgradeContext) -> StepResult:
    try:
        worktrees = desired_role_worktrees(context.product_root, context.runtime_home)
    except AgentSpecsError as exc:
        return StepResult("role-worktrees", "failed", str(exc))
    if not worktrees:
        return StepResult("role-worktrees", "skipped", "the product ships no role worktrees")
    created: list[str] = []
    moved: list[str] = []
    stuck: list[str] = []
    # Sudo may create worktree metadata the runtime user must own.
    try:
        _set_runtime_owner(worktrees[0].parent, context.runtime_user)
        for parent in _workspace_owner_dirs(worktrees[0]):
            _set_runtime_directory_owner(parent, context.runtime_user)
    except GitError as exc:
        return StepResult("role-worktrees", "failed", str(exc))
    for worktree in worktrees:
        try:
            _set_runtime_owner(worktree, context.runtime_user)
            admin = _worktree_git_dir(worktree)
            if admin is not None:
                _set_runtime_owner(admin.parent, context.runtime_user)
                _set_runtime_owner(admin, context.runtime_user)
        except GitError as exc:
            stuck.append(f"{worktree.name}: {exc}")
            continue
        if not (worktree / ".git").exists():
            if worktree.exists() and any(worktree.iterdir()):
                stuck.append(f"{worktree.name}: target exists and is not a managed worktree")
                continue
            if context.dry_run:
                created.append(worktree.name)
                continue
            try:
                worktree.parent.mkdir(parents=True, exist_ok=True)
                for parent in _workspace_owner_dirs(worktree):
                    _set_runtime_directory_owner(parent, context.runtime_user)
                # A registration whose directory is gone (a lost workspace root, a host rebuilt
                # from a backup) makes a plain add refuse; one `--force` replaces exactly that
                # registration. A locked one still refuses, and git's `fatal:` line says why.
                force = ["--force"] if _registered_worktree(context.product_root, worktree) else []
                _git(
                    context.product_root,
                    ["worktree", "add", *force, "--detach", str(worktree), "HEAD"],
                    timeout=300,
                )
                _set_runtime_owner(worktree, context.runtime_user)
                admin = _worktree_git_dir(worktree)
                if admin is None:
                    raise GitError(f"could not locate Git administration for {worktree}")
                _set_runtime_owner(worktree.parent, context.runtime_user)
                _set_runtime_owner(admin.parent, context.runtime_user)
                _set_runtime_owner(admin, context.runtime_user)
            except (GitError, OSError) as exc:
                stuck.append(f"{worktree.name}: {exc}")
                continue
            created.append(worktree.name)
            continue
        try:
            if context.dry_run:
                head, target = upstream_target(worktree, context.base_branch)
                if head != target:
                    moved.append(worktree.name)
                continue
            before, after = fast_forward(worktree, context.base_branch)
        except GitError as exc:
            stuck.append(f"{worktree.name}: {exc}")
            continue
        if before != after:
            moved.append(worktree.name)
    if stuck:
        return StepResult("role-worktrees", "failed", "; ".join(stuck))
    if not moved and not created:
        return StepResult("role-worktrees", "unchanged", f"{len(worktrees)} worktrees current")
    details = []
    if created:
        details.append("created " + ", ".join(created))
    if moved:
        details.append("updated " + ", ".join(moved))
    return StepResult("role-worktrees", "changed", "; ".join(details))


def step_pipeline_state(context: UpgradeContext) -> StepResult:
    """Put the dispatcher's untracked run journals back from the instance checkpoint.

    `state/pipeline/` lives in the pipeline worktree but is not tracked, so a worktree the previous
    step recreated comes back without the journals every checkpoint exports (ummanu-1). The restore
    is install's own and keeps its refusal: a live journal that does not extend the checkpoint fails
    the step and is never overwritten.
    """
    # installation imports this module; the restore lives there with install's own call of it.
    from ummanu.installation import InstallError, materialize_pipeline_state, pipeline_state_path

    if not (context.instance_path / "state" / "runs" / "runs.ndjson").is_file():
        return StepResult("pipeline-state", "skipped", "the instance checkpoint carries no run journal")
    state_dir = pipeline_state_path(context.runtime_home or Path.home())
    try:
        plan = materialize_pipeline_state(context.instance_path, state_dir, dry_run=True)
    except InstallError as exc:
        return StepResult("pipeline-state", "failed", str(exc))
    if not plan.changed:
        return StepResult(
            "pipeline-state", "unchanged", f"{state_dir} extends the checkpoint's {plan.records} run record(s)"
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
            "pipeline-state", "unchanged", f"{state_dir} extends the checkpoint's {restored.records} run record(s)"
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
        sys.executable, "-P", "-m", "ummanu", "web-front", "render",
        "--instance", str(context.instance_path), "--data-dir", str(data_dir),
    ]
    for site in sites:
        argv += ["--site", site]
    try:
        # The product this process runs, whoever the child runs as.
        completed = state_repo.run_as_git_child(
            context.instance_path,
            argv,
            label="web-front render",
            extra_env={"PYTHONPATH": str(Path(__file__).resolve().parents[1])},
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
    canonical = build_doctor_expectations(report.instance, report.bindings, packaged=packaged,
                                          data_dir=report.data_dir)
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
    preserved = ("; preserved runtime scopes: " + ", ".join(result.preserved_runtime_scopes)
                 if result.preserved_runtime_scopes else "")
    if not pending and not runtime_pending:
        return StepResult("host", "unchanged", f"{len(result.changes)} resources reconciled" + preserved)
    detail = ", ".join(f"{change.action} {change.name}" for change in [*pending, *runtime_pending])
    if result.runtime_findings:
        detail += "; " + "; ".join(result.runtime_findings)
    detail += preserved
    return StepResult("host", "changed", detail)


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


MEMORY_PROCESS_RECEIPT_RELATIVE = UPGRADE_RECEIPT_ROOT / "memory-process-receipt.json"
MEMORY_PROCESS_RECEIPT_VERSION = 1
MEMORY_PROCESS_INPUT_KEYS = (
    "product_revision",
    "product_sha256",
    "dependency_sha256",
    "memory_model",
    "memory_pack_sha256",
)
MEMORY_MODEL_RE = re.compile(r"[\x21-\x7e]{1,256}")


def _unit_environment(unit_text: bytes | None, key: str) -> str | None:
    """The last ``Environment=`` assignment of ``key`` in a unit file, if it makes one."""
    if unit_text is None:
        return None
    try:
        lines = unit_text.decode("utf-8").splitlines()
    except UnicodeError:
        return None
    value = None
    for line in lines:
        line = line.strip()
        if not line.startswith("Environment="):
            continue
        try:
            words = shlex.split(line[len("Environment=") :])
        except ValueError:
            continue
        for word in words:
            name, separator, item = word.partition("=")
            if separator and name == key:
                value = item
    return value


def memory_process_inputs(context: UpgradeContext, unit: str) -> dict[str, str | None]:
    """The checkout and installed state an active memory process has to be bound to."""
    model = _unit_environment(context.units.installed(unit), "MEMORY_MODEL") or DEFAULT_MEMORY_MODEL
    return {
        "product_revision": _product_revision(context.product_root, ReceiptError),
        "product_sha256": _git_tracked_digest(context.product_root, PRODUCT_SOURCE_PATHS),
        "dependency_sha256": _git_tracked_digest(context.product_root, DEPENDENCY_PATHS),
        "memory_model": model,
        "memory_pack_sha256": context.memory_pack_digest,
    }


def _valid_memory_inputs(value: object) -> dict[str, str | None] | None:
    if not isinstance(value, dict) or set(value) != set(MEMORY_PROCESS_INPUT_KEYS):
        return None
    revision = value["product_revision"]
    model = value["memory_model"]
    pack = value["memory_pack_sha256"]
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40,64}", revision):
        return None
    for key in ("product_sha256", "dependency_sha256"):
        if not isinstance(value[key], str) or not SHA256_RE.fullmatch(value[key]):
            return None
    if not isinstance(model, str) or not MEMORY_MODEL_RE.fullmatch(model):
        return None
    if pack is not None and (not isinstance(pack, str) or not SHA256_RE.fullmatch(pack)):
        return None
    return {key: value[key] for key in MEMORY_PROCESS_INPUT_KEYS}


def _read_memory_process_receipt(
    context: UpgradeContext, unit: str
) -> tuple[tuple[UnitProcessIdentity, dict[str, str | None]] | None, str]:
    try:
        path = _upgrade_receipt_path(context, MEMORY_PROCESS_RECEIPT_RELATIVE)
    except ReceiptError as exc:
        return None, f"the memory process receipt cannot be located: {exc}"
    payload, reason = _load_receipt(path, "memory process receipt")
    if payload is None:
        return None, reason
    malformed = "the memory process receipt is malformed"
    if set(payload) != {"version", "unit", "process", "inputs"}:
        return None, malformed
    version = payload["version"]
    if type(version) is not int or version != MEMORY_PROCESS_RECEIPT_VERSION or payload["unit"] != unit:
        return None, malformed
    identity = _receipt_identity(payload["process"])
    inputs = _valid_memory_inputs(payload["inputs"])
    if identity is None or inputs is None:
        return None, malformed
    return (identity, inputs), ""


def _short(value: str | None) -> str:
    if value is None:
        return "none"
    return value[:12] if re.fullmatch(r"[0-9a-f]{40,64}", value) else value


def _memory_receipt_evidence(
    context: UpgradeContext,
    unit: str,
    identity: UnitProcessIdentity | None,
    inputs: dict[str, str | None],
) -> tuple[bool, str]:
    """Whether the active memory process is the one the receipt bound to these inputs, and why."""
    if identity is None:
        return False, "the active memory process identity is unavailable"
    receipt, reason = _read_memory_process_receipt(context, unit)
    if receipt is None:
        return False, reason
    recorded_identity, recorded = receipt
    if recorded_identity != identity:
        return False, "the memory process receipt belongs to a different process generation"
    moved = [
        f"{key.replace('_', ' ')} {_short(recorded[key])} -> {_short(inputs[key])}"
        for key in MEMORY_PROCESS_INPUT_KEYS
        if recorded[key] != inputs[key]
    ]
    if moved:
        return False, "; ".join(moved)
    return True, f"memory process receipt verified: pid {identity.pid}; {_memory_inputs_summary(inputs)}"


def _memory_inputs_summary(inputs: dict[str, str | None]) -> str:
    return (
        f"product revision {_short(inputs['product_revision'])}, "
        f"product sha256 {_short(inputs['product_sha256'])}, "
        f"deps sha256 {_short(inputs['dependency_sha256'])}, "
        f"model {inputs['memory_model']}, pack sha256 {_short(inputs['memory_pack_sha256'])}"
    )


def step_memory(context: UpgradeContext) -> StepResult:
    """Keep the memory service running the checkout's code, dependencies, model and pack.

    Like `step_web`, the service is bound to what it was started on by a process receipt, so a
    service started before the checkout moved outside this upgrade is restarted, not reported as
    current. Unlike the web, a stopped memory service is started: the pipeline cannot run without it.
    """
    report = context.report
    unit = f"{_memory_unit_prefix(report)}service"
    if not _process_unit_enabled(context, MEMORY_COMPONENT, unit):
        return StepResult("memory", "skipped", f"{unit} is outside this installation's desired units")
    try:
        inputs = memory_process_inputs(context, unit)
    except ReceiptError as exc:
        return StepResult("memory", "failed", f"cannot compare the memory service with the checkout: {exc}")
    reasons = []
    try:
        active = context.units.is_active(unit)
    except HostCommandError as exc:
        return StepResult("memory", "failed", str(exc))
    if not active:
        reasons.append("service is not active")
    if context.unit_changed:
        reasons.append("unit file changed")
    if context.code_changed:
        reasons.append("product code or dependencies changed")
    if context.memory_pack_changed:
        reasons.append("memory pack export changed")
    if active:
        try:
            identity = context.units.process_identity(unit)
        except HostCommandError as exc:
            reasons.append(f"the active memory process identity could not be observed: {exc}")
        else:
            verified, evidence = _memory_receipt_evidence(context, unit, identity, inputs)
            if verified and not reasons:
                return StepResult("memory", "unchanged", evidence)
            if not verified:
                reasons.append(evidence)
    reason = "; ".join(reasons)
    if context.dry_run:
        return StepResult("memory", "changed", f"would restart {unit}: {reason}")
    try:
        context.units.restart(unit)
    except HostCommandError as exc:
        return StepResult("memory", "failed", str(exc))
    try:
        before_probe = context.units.process_identity(unit)
    except HostCommandError as exc:
        return StepResult(
            "memory", "failed", f"{unit} restarted but its process identity is unavailable: {exc}"
        )
    if before_probe is None:
        return StepResult("memory", "failed", f"{unit} restarted but its process identity is unavailable")
    try:
        data_dir = report.data_dir
        if data_dir is None:
            return StepResult(
                "memory", "failed", "authenticated probe: instance has no resolved data directory"
            )
        probe_memory(
            data_dir,
            runtime_handoff=lambda path: _set_runtime_owner(path, context.runtime_user),
            runtime_user=context.runtime_user,
        )
    except (MemoryProbeError, GitError) as exc:
        return StepResult("memory", "failed", f"authenticated probe failed: {exc}")
    try:
        after_probe = context.units.process_identity(unit)
        final_inputs = memory_process_inputs(context, unit)
    except (HostCommandError, ReceiptError) as exc:
        return StepResult("memory", "failed", f"{unit} probe passed but its receipt cannot be bound: {exc}")
    if after_probe != before_probe:
        return StepResult(
            "memory", "failed", f"{unit} changed process generation during the probe; no receipt was written"
        )
    if final_inputs != inputs:
        return StepResult(
            "memory", "failed", f"{unit} inputs changed during the restart; no receipt was written"
        )
    payload = {
        "version": MEMORY_PROCESS_RECEIPT_VERSION,
        "unit": unit,
        "process": _receipt_process(after_probe),
        "inputs": final_inputs,
    }
    try:
        _write_private_receipt(
            _upgrade_receipt_path(context, MEMORY_PROCESS_RECEIPT_RELATIVE),
            payload,
            context.runtime_user,
            ReceiptError,
            "memory process receipt",
        )
    except ReceiptError as exc:
        return StepResult("memory", "failed", f"restarted and authenticated {unit} but {exc}")
    return StepResult(
        "memory",
        "changed",
        f"restarted and authenticated {unit} ({_memory_inputs_summary(final_inputs)}) and wrote the "
        f"memory process receipt: {reason}",
    )


WEB_PROCESS_RECEIPT_RELATIVE = Path("web") / "process-receipt.json"
WEB_PROCESS_RECEIPT_VERSION = 1
WEB_PROCESS_INPUT_KEYS = (
    "product_revision",
    "product_sha256",
    "dependency_sha256",
    "schemas_sha256",
    "web_units_sha256",
    "head_registry_sha256",
)


class WebProcessReceiptError(ReceiptError):
    """The small, private receipt that binds a successful web generation to its inputs."""


def web_process_receipt_path(context: UpgradeContext) -> Path:
    data_dir = getattr(context.report, "data_dir", None)
    if not isinstance(data_dir, Path):
        raise WebProcessReceiptError("web process receipt: instance has no resolved data directory")
    return data_dir / WEB_PROCESS_RECEIPT_RELATIVE


def _git_tracked_digest(product_root: Path, paths: tuple[str, ...]) -> str:
    """Hash selected tracked product inputs, including their checkout-relative names.

    A working process may create bytecode below ``src/``.  Git's tracked list intentionally leaves
    that runtime by-product out while still including local edits to a tracked input.  The revision
    is recorded separately, so a moved checkout and a dirty tracked file are both visible evidence.
    """
    try:
        listed = _git(product_root, ["ls-files", "--", *paths])
    except GitError as exc:
        raise WebProcessReceiptError(f"cannot list product inputs: {exc}") from None
    digest = hashlib.sha256()
    digest.update(b"ummanu-web-input-v1\0")
    digest.update("\0".join(paths).encode("utf-8"))
    for relative in filter(None, listed.splitlines()):
        path = product_root / relative
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise OSError("not a regular file")
            contents = path.read_bytes()
        except OSError as exc:
            raise WebProcessReceiptError(f"cannot read product input {relative}: {exc}") from None
        digest.update(b"\0file\0")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(contents)
    return digest.hexdigest()


def _digest_web_units(context: UpgradeContext, name_prefix: str) -> str:
    """Hash both shipped and installed web unit bytes without retaining their host-specific text."""
    digest = hashlib.sha256()
    digest.update(b"ummanu-web-units-v1\0")
    for name in (f"{name_prefix}.service", f"{name_prefix}-front.service"):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0installed\0")
        installed = context.units.installed(name)
        digest.update(installed if installed is not None else b"<absent>")
        shipped = context.product_root / PACKAGED_UNIT_ROOT / name
        digest.update(b"\0shipped\0")
        try:
            info = shipped.lstat()
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise OSError("not a regular file")
            digest.update(shipped.read_bytes())
        except FileNotFoundError:
            digest.update(b"<absent>")
        except OSError as exc:
            raise WebProcessReceiptError(f"cannot read shipped web unit {shipped}: {exc}") from None
    return digest.hexdigest()


def _product_revision(product_root: Path, error: type[ReceiptError]) -> str:
    try:
        revision = _git(product_root, ["rev-parse", "HEAD"])
    except GitError as exc:
        raise error(f"cannot read product revision: {exc}") from None
    if not re.fullmatch(r"[0-9a-f]{40,64}", revision):
        raise error("cannot read an exact product revision")
    return revision


def web_process_inputs(context: UpgradeContext, name_prefix: str) -> dict[str, str]:
    """The product and materialized state an active web process has to be bound to."""
    revision = _product_revision(context.product_root, WebProcessReceiptError)
    try:
        snapshot = installed_pair(context.instance_path, _data_dir(context)).snapshot
    except HeadRegistryConfigError as exc:
        raise WebProcessReceiptError(str(exc)) from None
    try:
        snapshot_bytes = snapshot.read_bytes()
    except FileNotFoundError:
        snapshot_bytes = b"<missing>"
    except OSError as exc:
        raise WebProcessReceiptError(f"cannot read head registry snapshot {snapshot}: {exc}") from None
    return {
        "product_revision": revision,
        "product_sha256": _git_tracked_digest(context.product_root, PRODUCT_SOURCE_PATHS),
        "dependency_sha256": _git_tracked_digest(context.product_root, DEPENDENCY_PATHS),
        "schemas_sha256": _git_tracked_digest(context.product_root, SCHEMA_PATHS),
        "web_units_sha256": _digest_web_units(context, name_prefix),
        "head_registry_sha256": hashlib.sha256(snapshot_bytes).hexdigest(),
    }


def _receipt_process(identity: UnitProcessIdentity) -> dict[str, int | str]:
    return {
        "pid": identity.pid,
        "start_ticks": identity.start_ticks,
        "invocation_id": identity.invocation_id,
    }


def _receipt_identity(value: object) -> UnitProcessIdentity | None:
    if not isinstance(value, dict) or set(value) != {"pid", "start_ticks", "invocation_id"}:
        return None
    pid = value.get("pid")
    start_ticks = value.get("start_ticks")
    invocation_id = value.get("invocation_id")
    if (
        type(pid) is not int
        or pid <= 0
        or type(start_ticks) is not int
        or start_ticks <= 0
        or not isinstance(invocation_id, str)
        or not invocation_id
    ):
        return None
    return UnitProcessIdentity(pid, start_ticks, invocation_id)


def _valid_receipt_inputs(value: object) -> dict[str, str] | None:
    if not isinstance(value, dict) or set(value) != set(WEB_PROCESS_INPUT_KEYS):
        return None
    result = {}
    for key in WEB_PROCESS_INPUT_KEYS:
        item = value.get(key)
        if not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{64}", item):
            if key == "product_revision" and isinstance(item, str) and re.fullmatch(r"[0-9a-f]{40,64}", item):
                result[key] = item
                continue
            return None
        result[key] = item
    return result


def _read_web_process_receipt(context: UpgradeContext, unit: str) -> tuple[dict[str, Any] | None, str]:
    try:
        path = web_process_receipt_path(context)
        info = path.lstat()
    except FileNotFoundError:
        return None, "the web process receipt is missing"
    except OSError as exc:
        return None, f"the web process receipt cannot be read: {exc}"
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        return None, "the web process receipt is malformed"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, "the web process receipt is malformed"
    if not isinstance(payload, dict) or set(payload) != {"version", "unit", "process", "inputs"}:
        return None, "the web process receipt is malformed"
    if payload.get("version") != WEB_PROCESS_RECEIPT_VERSION or payload.get("unit") != unit:
        return None, "the web process receipt is malformed"
    if (
        _receipt_identity(payload.get("process")) is None
        or _valid_receipt_inputs(payload.get("inputs")) is None
    ):
        return None, "the web process receipt is malformed"
    return payload, ""


def _receipt_evidence(
    context: UpgradeContext,
    unit: str,
    identity: UnitProcessIdentity | None,
    inputs: dict[str, str],
) -> tuple[bool, str]:
    if identity is None:
        return False, "the active web process identity is unavailable"
    receipt, reason = _read_web_process_receipt(context, unit)
    if receipt is None:
        return False, reason
    if _receipt_identity(receipt["process"]) != identity:
        return False, "the web process receipt belongs to a different process generation"
    if _valid_receipt_inputs(receipt["inputs"]) != inputs:
        return False, "the web process receipt inputs do not match the materialized state"
    evidence = (
        f"web process receipt verified: pid {identity.pid}, start ticks {identity.start_ticks}, "
        f"invocation {identity.invocation_id}; product revision {inputs['product_revision']}; "
        f"product {inputs['product_sha256']}; dependencies {inputs['dependency_sha256']}; "
        f"schemas {inputs['schemas_sha256']}; web units {inputs['web_units_sha256']}; "
        f"head registry {inputs['head_registry_sha256']}"
    )
    return True, evidence


def _receipt_owner(runtime_user: str | None, error: type[ReceiptError]) -> tuple[int, int] | None:
    if not runtime_user or os.geteuid() != 0:
        return None
    try:
        account = pwd.getpwnam(runtime_user)
    except KeyError:
        raise error(f"runtime user {runtime_user!r} does not exist") from None
    return account.pw_uid, account.pw_gid


def _write_web_process_receipt(
    context: UpgradeContext,
    unit: str,
    identity: UnitProcessIdentity,
    inputs: dict[str, str],
) -> None:
    """Publish the receipt only after a proved generation, as one private atomic replacement."""
    payload = {
        "version": WEB_PROCESS_RECEIPT_VERSION,
        "unit": unit,
        "process": _receipt_process(identity),
        "inputs": inputs,
    }
    _write_private_receipt(
        web_process_receipt_path(context),
        payload,
        context.runtime_user,
        WebProcessReceiptError,
        "web process receipt",
    )


def _write_private_receipt(
    path: Path,
    payload: dict[str, Any],
    runtime_user: str | None,
    error: type[ReceiptError],
    label: str,
) -> None:
    """Write one receipt as a private (0600, runtime-owned) atomic replacement."""
    temporary: Path | None = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        owner = _receipt_owner(runtime_user, error)
        parent_info = path.parent.lstat()
        if not stat.S_ISDIR(parent_info.st_mode) or stat.S_ISLNK(parent_info.st_mode):
            raise OSError("receipt directory is not a real directory")
        if owner is not None:
            os.chown(path.parent, *owner, follow_symlinks=False)
            os.chmod(path.parent, 0o700, follow_symlinks=False)
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        if owner is not None:
            os.fchown(descriptor, *owner)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, sort_keys=True, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        temporary = None
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        raise error(f"could not write {label} {path}: {exc}") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _po_unit(report: Any) -> str:
    return f"{_component_unit_prefix(report, PO_COMPONENT)}.service"


# The PO service writes its own process receipt when it starts (`ummanu po-serve`), unlike the web
# and memory receipts, which the upgrade writes after its own restart: an upgrade only asks the PO
# service to restart, and a busy service exits long after the upgrade that asked has ended
# (secretary-1759). It lives in the service's own directory, which the runtime user owns, and no
# backup carries it (`po-service/` is outside the backed-up roots).
PO_PROCESS_RECEIPT_VERSION = 1
PO_PROCESS_INPUT_KEYS = ("product_revision", "product_sha256", "dependency_sha256", "schemas_sha256")


def po_process_inputs(product_root: Path) -> dict[str, str]:
    """The checkout a PO process has to be bound to: its code, dependencies and bundled schemas."""
    return {
        "product_revision": _product_revision(product_root, ReceiptError),
        "product_sha256": _git_tracked_digest(product_root, PRODUCT_SOURCE_PATHS),
        "dependency_sha256": _git_tracked_digest(product_root, DEPENDENCY_PATHS),
        "schemas_sha256": _git_tracked_digest(product_root, SCHEMA_PATHS),
    }


def _valid_po_inputs(value: object) -> dict[str, str] | None:
    if not isinstance(value, dict) or set(value) != set(PO_PROCESS_INPUT_KEYS):
        return None
    revision = value["product_revision"]
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40,64}", revision):
        return None
    for key in PO_PROCESS_INPUT_KEYS[1:]:
        if not isinstance(value[key], str) or not SHA256_RE.fullmatch(value[key]):
            return None
    return {key: value[key] for key in PO_PROCESS_INPUT_KEYS}


def write_po_process_receipt(
    data_dir: Path, product_root: Path, *, environ: dict[str, str] | None = None
) -> str:
    """Bind the calling PO service process to the checkout it was started on; the journal line.

    The identity is the one `SystemdUnitInstaller.process_identity` observes from outside: this pid,
    its kernel start ticks and systemd's ``INVOCATION_ID``. A process systemd did not start has no
    invocation id and gets no receipt, so an upgrade asks it to restart. The receipt is one private
    atomic replacement, so a new process generation replaces the previous one's.
    """
    env = os.environ if environ is None else environ
    pid = os.getpid()
    invocation_id = env.get("INVOCATION_ID", "")
    if not invocation_id:
        raise ReceiptError("no INVOCATION_ID: the process was not started by systemd")
    start_ticks = _process_start_ticks(pid)
    if start_ticks is None:
        raise ReceiptError(f"cannot read the start ticks of pid {pid}")
    identity = UnitProcessIdentity(pid, start_ticks, invocation_id)
    inputs = po_process_inputs(product_root)
    payload = {"version": PO_PROCESS_RECEIPT_VERSION, "process": _receipt_process(identity), "inputs": inputs}
    path = po_client.process_receipt_path(data_dir)
    _write_private_receipt(path, payload, None, ReceiptError, "PO process receipt")
    return f"wrote the PO process receipt {path}: pid {pid}; {_po_inputs_summary(inputs)}"


def _read_po_process_receipt(
    data_dir: Path,
) -> tuple[tuple[UnitProcessIdentity, dict[str, str]] | None, str]:
    payload, reason = _load_receipt(po_client.process_receipt_path(data_dir), "PO process receipt")
    if payload is None:
        return None, reason
    malformed = "the PO process receipt is malformed"
    if set(payload) != {"version", "process", "inputs"}:
        return None, malformed
    version = payload["version"]
    if type(version) is not int or version != PO_PROCESS_RECEIPT_VERSION:
        return None, malformed
    identity = _receipt_identity(payload["process"])
    inputs = _valid_po_inputs(payload["inputs"])
    if identity is None or inputs is None:
        return None, malformed
    return (identity, inputs), ""


def _po_receipt_evidence(
    data_dir: Path | None, identity: UnitProcessIdentity | None, inputs: dict[str, str]
) -> tuple[bool, str]:
    """Whether the active PO process is the one its receipt bound to these inputs, and why."""
    if data_dir is None:
        return False, "the PO process receipt cannot be located: instance has no resolved data directory"
    if identity is None:
        return False, "the active PO process identity is unavailable"
    receipt, reason = _read_po_process_receipt(data_dir)
    if receipt is None:
        return False, reason
    recorded_identity, recorded = receipt
    if recorded_identity != identity:
        return False, "the PO process receipt belongs to a different process generation"
    moved = [
        f"{key.replace('_', ' ')} {_short(recorded[key])} -> {_short(inputs[key])}"
        for key in PO_PROCESS_INPUT_KEYS
        if recorded[key] != inputs[key]
    ]
    if moved:
        return False, f"the PO process receipt is stale: {'; '.join(moved)}"
    return True, f"PO process receipt verified: pid {identity.pid}, {_po_inputs_summary(inputs)}"


def _po_inputs_summary(inputs: dict[str, str]) -> str:
    return (
        f"revision {_short(inputs['product_revision'])}, "
        f"product sha256 {_short(inputs['product_sha256'])}, "
        f"deps sha256 {_short(inputs['dependency_sha256'])}, "
        f"schemas sha256 {_short(inputs['schemas_sha256'])}"
    )


def step_po(context: UpgradeContext) -> StepResult:
    """Put the PO service on this upgrade's code without killing a running PO turn.

    Every PO turn is a child of `ummanu-po.service`, and the PO itself runs `ummanu upgrade`
    inside a turn, so restarting the unit here would kill and re-run this very upgrade's caller. The
    one rule is the service's (`PoService.request_restart`, asked through
    `ummanu.po.client.request_restart`): idle, it exits now and `Restart=always` starts the new
    code, which this step waits for; busy, it takes no new turn and exits as soon as its running turns
    settle, and this step reports the restart as deferred rather than failing.

    The reasons are the service's process inputs: the product source and dependencies, the bundled
    schemas and the unit file (an update; a unit reconcile just created was started on this code).
    This run's pull delta is not enough: on an installation whose dispatcher release fast-forwards the
    checkout before the PO runs `upgrade`, the pull is empty while the service still runs the old code
    (secretary-1759). So, like the web and memory services, the running process is bound to what it
    was started on by a process receipt the service writes at start (`write_po_process_receipt`): a
    missing one, one of another process generation, or one whose revision or digests differ from the
    checkout is a reason too. Like
    the memory service and unlike the web, a stopped PO service is started: nothing runs in a stopped
    unit, and without it no PO turn runs at all. An installation that opted the component out has no
    unit, and the step is skipped.
    """
    report = context.report
    unit = _po_unit(report)
    if not _process_unit_enabled(context, PO_COMPONENT, unit):
        return StepResult("po", "skipped", f"{unit} is outside this installation's desired units")
    if context.units.installed(unit) is None:
        return StepResult("po", "skipped", f"{unit} is not installed; this host runs no PO service")
    try:
        active = context.units.is_active(unit)
    except HostCommandError as exc:
        return StepResult("po", "failed", str(exc))
    if not active:
        if context.dry_run:
            return StepResult("po", "changed", f"would start {unit}: service is not active")
        try:
            context.units.restart(unit)
        except HostCommandError as exc:
            return StepResult("po", "failed", f"starting {unit} failed: {exc}")
        return StepResult("po", "changed", f"started {unit}: service was not active")
    try:
        inputs = po_process_inputs(context.product_root)
    except ReceiptError as exc:
        return StepResult("po", "failed", f"cannot compare the PO service with the checkout: {exc}")
    reasons = []
    if context.po_unit_changed or planned_unit_names(
        context.changed_paths, f"{_component_unit_prefix(report, PO_COMPONENT)}."
    ):
        reasons.append("the PO unit file changed")
    if context.schemas_changed:
        reasons.append("bundled schemas changed")
    if context.code_changed:
        reasons.append("product code or dependencies changed")
    try:
        identity = context.units.process_identity(unit)
    except HostCommandError as exc:
        reasons.append(f"the active PO process identity could not be observed: {exc}")
    else:
        verified, evidence = _po_receipt_evidence(report.data_dir, identity, inputs)
        if verified and not reasons:
            return StepResult("po", "unchanged", evidence)
        if not verified:
            reasons.append(evidence)
    reason = "; ".join(reasons)
    if context.dry_run:
        return StepResult("po", "changed", f"would ask {unit} to restart once no PO turn runs: {reason}")
    data_dir = report.data_dir
    if data_dir is None:
        return StepResult("po", "failed", "the instance has no resolved data directory for the PO service")
    try:
        before = context.units.process_identity(unit)
        directory = po_client.service_dir(data_dir)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        _set_runtime_owner(directory, context.runtime_user)
        answer = po_client.request_restart(data_dir, reason)
        _set_runtime_owner(po_client.restart_marker_path(data_dir), context.runtime_user)
    except (HostCommandError, GitError, OSError) as exc:
        return StepResult("po", "failed", f"could not ask {unit} to restart: {exc}")
    if answer.outcome == "deferred":
        return StepResult(
            "po",
            "changed",
            f"PO service restart deferred: {answer.running} turn(s) running; "
            f"{unit} restarts itself once they end: {reason}",
        )
    if answer.outcome == "unanswered":
        return StepResult(
            "po",
            "changed",
            f"PO service restart requested but not acknowledged ({answer.detail}); "
            f"{unit} restarts itself at its next idle check: {reason}",
        )
    deadline = _monotonic() + PO_RESTART_WAIT_SECONDS
    while True:
        try:
            after = context.units.process_identity(unit)
        except HostCommandError:
            after = None
        if after is not None and after != before and context.units.is_active(unit):
            return StepResult("po", "changed", f"restarted {unit} while idle (pid {after.pid}): {reason}")
        if _monotonic() >= deadline:
            return StepResult(
                "po",
                "failed",
                f"{unit} exited for the restart but no new process came up within {PO_RESTART_WAIT_SECONDS:g}s",
            )
        _sleep(PO_RESTART_POLL_SECONDS)


def _monotonic() -> float:
    return time.monotonic()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def step_web(context: UpgradeContext) -> StepResult:
    """Make the long-lived web process coherent with what this upgrade just materialized.

    The web transport was the one supported long-lived process an upgrade did not own. That is not
    a gap you can see from a step list: `ummanu upgrade` reported `ok`, the checkout was current,
    the unit was current, and `ummanu-web.service` went on running the callables it had imported
    a day earlier. On 2026-09-11 that process answered every route with an empty reply for nineteen
    hours, because `importlib.resources` reads the bundled schemas from the checkout *now* while the
    validator that reads them was loaded *then*: a new relative `$ref` against an old registry-less
    validator (secretary-1624). Timestamps could not fix that and were never the defect; the process
    was. So the reconciliation is a restart, and it is here, after everything the new process needs.

    Three properties are load-bearing:

    **It is last of the materializing steps.** A restart is the moment the old code stops being the
    code that answers, so everything the new process reads has to be in place first: the checkout
    (`step_pull`), the installed dependencies and the bundled schemas that came with them
    (`step_dependencies`), and the unit itself (`step_host`). `run_steps` stops at the first failed
    step, so a failed prerequisite means this step does not run at all and the run is failed — it
    can never be the case that something reported the web as serving current code over a failed
    prerequisite.

    **A restart without a read is not evidence.** systemd accepting `restart` says nothing about
    whether the new process bound its socket or whether it can answer. So a restart is followed by
    one bounded loopback GET against the address the *installed unit* names, and a probe that does
    not come back 200 fails the step: an upgrade that cannot read the transport it just restarted
    does not get to say the transport is current.

    **The unit is optional and this step never starts it.** The web is not on every installation,
    and a stopped transport is a state an operator chose — an upgrade that quietly published a
    dashboard because it found a unit file would be making that choice for them. So an uninstalled
    or inactive unit is reported as skipped, with which of the two it was, and nothing is started.
    That is the one place this step deliberately differs from `step_memory`, which starts a stopped
    memory service because the pipeline cannot run without it.

    Only `ummanu-web.service` is restarted. `ummanu-web-front.service` is `PartOf=` it and
    comes along, which is also why a change to either unit file is a reason to restart this one.

    The restart reasons are every process-local input this upgrade materializes: the product source
    and its dependencies, the bundled schemas, the shipped web unit files, and the head registry
    snapshot — `load_registry` caches per process, so a profile added to the canon by a `--no-pull`
    run is invisible to the running transport until it is replaced, and that made the documented
    `heads.toml` procedure need a manual restart nobody was reminded of. A unit change is read from
    both sides: `web_unit_changed` is what reconcile actually did, and `planned_unit_names` is what
    the target revision will do, which is the only one of the two that can answer under `--dry-run`.
    """
    report = context.report
    name_prefix = _component_unit_prefix(report, WEB_COMPONENT)
    unit = f"{name_prefix}.service"
    if not _process_unit_enabled(context, WEB_COMPONENT, unit):
        return StepResult("web", "skipped", f"{unit} is outside this installation's desired units")
    installed = context.units.installed(unit)
    if installed is None:
        return StepResult("web", "skipped", f"{unit} is not installed; this host serves no web transport")
    try:
        active = context.units.is_active(unit)
    except HostCommandError as exc:
        return StepResult("web", "failed", str(exc))
    if not active:
        return StepResult(
            "web", "skipped", f"{unit} is installed but not active; an upgrade does not start it"
        )
    try:
        inputs = web_process_inputs(context, name_prefix)
    except WebProcessReceiptError as exc:
        return StepResult("web", "failed", str(exc))
    reasons = []
    if context.web_unit_changed or planned_unit_names(context.changed_paths, name_prefix):
        reasons.append("a web unit file changed")
    if context.web_front_config_changed:
        reasons.append("the web front configuration was rendered")
    if context.schemas_changed:
        reasons.append("bundled schemas changed")
    if context.code_changed:
        reasons.append("product code or dependencies changed")
    if context.head_registry_changed:
        reasons.append("the head registry snapshot changed")
    try:
        identity = context.units.process_identity(unit)
    except HostCommandError as exc:
        identity = None
        receipt_reason = f"the active web process identity could not be observed: {exc}"
    else:
        verified, receipt_reason = _receipt_evidence(context, unit, identity, inputs)
        if verified and not reasons:
            return StepResult("web", "unchanged", receipt_reason)
    reasons.append(receipt_reason)
    try:
        target = target_from_unit(installed)
    except LoopbackOnly as refused:
        return StepResult("web", "failed", f"{unit} does not serve a loopback address: {refused}")
    reason = "; ".join(reasons)
    if context.dry_run:
        return StepResult("web", "changed", f"would restart {unit} and probe {target.url}: {reason}")
    try:
        context.units.restart(unit)
    except HostCommandError as exc:
        return StepResult("web", "failed", f"restarting {unit} failed: {exc}")
    try:
        before_probe = context.units.process_identity(unit)
    except HostCommandError as exc:
        return StepResult("web", "failed", f"{unit} restarted but its process identity is unavailable: {exc}")
    if before_probe is None:
        return StepResult("web", "failed", f"{unit} restarted but its process identity is unavailable")
    try:
        status = probe_web(target, timeout_seconds=WEB_PROBE_TIMEOUT_SECONDS)
    except WebProbeError as exc:
        return StepResult("web", "failed", f"{unit} restarted but the loopback probe failed: {exc}")
    try:
        after_probe = context.units.process_identity(unit)
    except HostCommandError as exc:
        return StepResult(
            "web", "failed", f"{unit} probe passed but its process identity is unavailable: {exc}"
        )
    if after_probe != before_probe:
        return StepResult(
            "web",
            "failed",
            f"{unit} changed process generation during the loopback probe; no receipt was written",
        )
    try:
        final_inputs = web_process_inputs(context, name_prefix)
    except WebProcessReceiptError as exc:
        return StepResult("web", "failed", f"{unit} probe passed but receipt inputs could not be read: {exc}")
    if final_inputs != inputs:
        return StepResult(
            "web", "failed", f"{unit} materialized inputs changed during restart; no receipt was written"
        )
    try:
        _write_web_process_receipt(context, unit, after_probe, final_inputs)
    except WebProcessReceiptError as exc:
        return StepResult("web", "failed", str(exc))
    return StepResult(
        "web",
        "changed",
        f"restarted {unit} and probed {target.url} -> {status}; wrote web process receipt: {reason}",
    )


def step_verify(context: UpgradeContext) -> StepResult:
    """Re-plan against the host we just wrote. A second pass must be a no-op."""
    if context.dry_run:
        return StepResult("verify", "skipped", "--dry-run made no changes to verify")
    probe = replace(context, dry_run=True, pull=False)
    result = step_host(probe)
    if result.failed:
        return StepResult("verify", "failed", f"host is still not reconciled: {result.detail}")
    if result.status == "changed":
        return StepResult("verify", "failed", f"reconcile is not idempotent: {result.detail}")
    name_prefix = _component_unit_prefix(context.report, WEB_COMPONENT)
    web_unit = f"{name_prefix}.service"
    web_evidence = ""
    po_unit = _po_unit(context.report)
    try:
        web_active = (
            _process_unit_enabled(context, WEB_COMPONENT, web_unit)
            and context.units.installed(web_unit) is not None
            and context.units.is_active(web_unit)
        )
        po_installed = (
            _process_unit_enabled(context, PO_COMPONENT, po_unit)
            and context.units.installed(po_unit) is not None
        )
        po_active = po_installed and context.units.is_active(po_unit)
    except HostCommandError as exc:
        return StepResult("verify", "failed", str(exc))
    if web_active:
        try:
            inputs = web_process_inputs(context, name_prefix)
            identity = context.units.process_identity(web_unit)
        except (HostCommandError, WebProcessReceiptError) as exc:
            return StepResult("verify", "failed", f"active web process receipt cannot be verified: {exc}")
        verified, web_evidence = _receipt_evidence(context, web_unit, identity, inputs)
        if not verified:
            return StepResult(
                "verify", "failed", f"active web process receipt is not current: {web_evidence}"
            )
    if not po_installed:
        po_evidence = f"PO process receipt not checked: {po_unit} is not installed"
    elif not po_active:
        po_evidence = f"PO process receipt not checked: {po_unit} is not active"
    else:
        data_dir = getattr(context.report, "data_dir", None)
        try:
            po_inputs = po_process_inputs(context.product_root)
            po_identity = context.units.process_identity(po_unit)
        except (HostCommandError, ReceiptError) as exc:
            return StepResult("verify", "failed", f"active PO process receipt cannot be verified: {exc}")
        verified, po_evidence = _po_receipt_evidence(data_dir, po_identity, po_inputs)
        if not verified:
            # A busy service restarts itself once its turns end (`step_po` deferred it): the PO runs
            # `ummanu upgrade` inside a turn, so its own upgrade always ends here.
            if data_dir is None or not po_client.restart_marker_path(data_dir).exists():
                return StepResult(
                    "verify", "failed", f"active PO process receipt is not current: {po_evidence}"
                )
            po_evidence = f"PO service restart pending: {po_evidence}"
    try:
        audit = role_skills.audit(
            instance_path=context.instance_path,
            product_manifest=_role_skills_manifest(context),
            home=context.runtime_home,
            data_dir=_data_dir(context),
        )
    except (OSError, ValueError) as exc:
        return StepResult("verify", "failed", str(exc))
    if not audit["ok"]:
        return StepResult("verify", "failed", "role skills are still out of sync")
    try:
        assert_snapshot_current(context.instance_path, context.product_root, _data_dir(context))
        installed_heads(context.instance_path, generated_pair(context.instance_path, _data_dir(context)))
    except HeadRegistryConfigError as exc:
        return StepResult("verify", "failed", str(exc))
    detail = "host reconciled and role skills in sync"
    if web_evidence:
        detail += f"; {web_evidence}"
    detail += f"; {po_evidence}"
    return StepResult("verify", "unchanged", detail)


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
            privileged_argv=(
                context.units.argv
                if callable(getattr(context.units, "argv", None))
                else None
            ),
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


# Validate registries before any mutating materialization step.
STEPS: tuple[Callable[[UpgradeContext], StepResult], ...] = (
    step_pull,
    step_registries,
    step_memory_pack,
    step_runtime_owner,
    step_dependencies,
    step_dependency_provenance,
    step_board_store_provision,
    step_board_store,
    step_board_store_roles,
    step_memory_clients,
    step_codex_home,
    step_po_workspace,
    step_interactive_workspace,
    step_head_registry,
    step_instance_packing,
    step_worktrees,
    # Right after the worktrees: a recreated pipeline worktree has no untracked run journals.
    step_pipeline_state,
    step_role_skills,
    step_po_workspace_owner,
    step_po_token,
    # Before the host step: it enables and starts the front, whose ExecStartPre validates this file.
    step_web_front_config,
    step_host,
    step_memory,
    # Never kills a running PO turn: the service restarts itself once idle.
    step_po,
    # Last of the materializing steps: a restart is the moment the new code becomes the code that
    # answers, so it follows the checkout, the dependencies, the schemas and the unit.
    step_web,
    step_verify,
)


def run_steps(context: UpgradeContext, steps=STEPS) -> UpgradeResult:
    result = UpgradeResult()
    for step in steps:
        try:
            outcome = step(context)
        except HostCommandError as exc:
            outcome = StepResult(step.__name__.removeprefix("step_"), "failed", str(exc))
        result.steps.append(outcome)
        if outcome.failed:
            break
    return result


def running_product_root() -> Path:
    """The checkout this module was imported from.

    For reading what this process itself ships. Never for deciding what to install: that is
    `default_product_root`.
    """
    return Path(__file__).resolve().parents[2]


def default_product_root() -> Path:
    """The checkout an install or upgrade materializes when nothing names one.

    The configured one, or ``~/ummanu`` — never the checkout the running module happens to sit in.
    A candidate checkout is a normal place to run ``ummanu upgrade`` from, and installing whatever
    executed the command would make the caller's working directory decide the product version.
    ``--product-root`` and ``UMMANU_REPO`` still win, in that order.
    """
    return configured_product_root()


def run_upgrade(args) -> int:
    report = validate_instance(Path(args.instance))
    if not report.ok:
        print(f"ummanu upgrade: {len(report.errors)} config problem(s):")
        for error in report.errors:
            print(f"  {error}")
        return 2
    product_root = Path(args.product_root).expanduser() if args.product_root else default_product_root()
    # Resolve the checkout owner before materializing home-relative paths.
    instance_path = report.instance_path.parent
    try:
        runtime_user, runtime_home = resolve_runtime_owner(instance_path, getattr(args, "runtime_user", None))
    except ValueError as exc:
        print(f"ummanu upgrade: {exc}")
        return 2
    context = UpgradeContext(
        instance_path=instance_path,
        product_root=product_root,
        base_branch=args.base_branch,
        dry_run=args.dry_run,
        units=SystemdUnitInstaller(runtime_user=runtime_user),
        host_fixture=Path(args.host_fixture) if args.host_fixture else None,
        pull=not args.no_pull,
        report=report,
        runtime_user=runtime_user,
        runtime_home=runtime_home,
    )
    handoff = os.environ.get("UMMANU_UPGRADE_HANDOFF")
    if handoff:
        try:
            marker = json.loads(handoff)
            before = str(marker["before"])
            after = str(marker["after"])
            if _git(product_root, ["rev-parse", "HEAD"]) != after:
                raise ValueError("checkout no longer matches the pulled revision")
            if _git(product_root, ["status", "--porcelain"]):
                raise ValueError("checkout became dirty during pulled-code handoff")
            changed_paths = marker.get("changed_paths")
            if not isinstance(changed_paths, list) or not all(
                isinstance(path, str) for path in changed_paths
            ):
                raise ValueError("handoff has no valid changed-path set")
        except (GitError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            print(f"ummanu upgrade: invalid pulled-code handoff: {exc}")
            return 1
        context.pull = False
        context.handoff_before = before
        context.handoff_after = after
        record_change_plan(context, tuple(changed_paths))
    elif context.pull and not context.dry_run:
        pulled = step_pull(context)
        if pulled.failed:
            result = UpgradeResult([pulled])
            print(result.render())
            return 1
        if pulled.status == "changed":
            before = context.pulled_before
            after = context.pulled_after
            if before is None or after is None:
                print("ummanu upgrade: pull did not record the before/after revisions")
                return 1
            try:
                _exec_pulled_upgrade(
                    args,
                    product_root,
                    before=before,
                    after=after,
                    changed_paths=context.changed_paths,
                )
            except OSError as exc:
                print(f"ummanu upgrade: could not run pulled revision {after[:12]}: {exc}")
                return 1
            raise AssertionError("exec returned unexpectedly")
        context.pull_result = pulled
    result = run_steps(context)
    if args.json:
        print(
            json.dumps(
                {
                    "status": "ok" if result.ok else "failed",
                    "dry_run": context.dry_run,
                    "steps": [
                        {"name": step.name, "status": step.status, "detail": step.detail}
                        for step in result.steps
                    ],
                },
                sort_keys=True,
                indent=2,
            )
        )
    else:
        print(result.render())
    return 0 if result.ok else 1


def _exec_pulled_upgrade(
    args: Any,
    product_root: Path,
    *,
    before: str,
    after: str,
    changed_paths: tuple[str, ...],
) -> None:
    """Replace the import-bound process with the exact checkout revision it just pulled."""
    python = product_root / ".venv" / "bin" / "python"
    if not python.is_file():
        raise FileNotFoundError(f"the pulled checkout has no production interpreter at {python}")
    argv = [
        str(python),
        "-P",
        "-m",
        "ummanu",
        "upgrade",
        "--instance",
        str(args.instance),
        "--no-pull",
        "--base-branch",
        str(args.base_branch),
        "--product-root",
        str(product_root),
    ]
    if args.dry_run:
        argv.append("--dry-run")
    if getattr(args, "runtime_user", None):
        argv.extend(("--runtime-user", str(args.runtime_user)))
    if getattr(args, "host_fixture", None):
        argv.extend(("--host-fixture", str(args.host_fixture)))
    if args.json:
        argv.append("--json")
    environment = dict(os.environ)
    environment["UMMANU_UPGRADE_HANDOFF"] = json.dumps(
        {"before": before, "after": after, "changed_paths": list(changed_paths)}
    )
    os.execve(python, argv, environment)


def add_upgrade_command(subparsers) -> None:
    upgrade = subparsers.add_parser(
        "upgrade",
        help="pull the current product version and re-materialize this installation",
    )
    add_instance_argument(upgrade)
    upgrade.add_argument("--dry-run", action="store_true", help="decide every step but write nothing")
    upgrade.add_argument("--no-pull", action="store_true", help="re-materialize without moving the checkout")
    upgrade.add_argument("--base-branch", default="main")
    upgrade.add_argument(
        "--product-root",
        help="product checkout to upgrade (defaults to UMMANU_REPO, else ~/ummanu)",
    )
    upgrade.add_argument(
        "--runtime-user",
        help="account this installation belongs to, whose home every home-relative path is "
        "materialized under (default: the owner of the instance checkout)",
    )
    upgrade.add_argument("--host-fixture", metavar="DIR", help=argparse.SUPPRESS)
    upgrade.add_argument("--json", action="store_true", help="emit the step report as JSON")
    upgrade.set_defaults(handler=run_upgrade)
