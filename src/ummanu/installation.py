"""Supported fresh-install and recovery flow, from a legacy Git checkpoint or an exporter snapshot.

The private instance repository is the only portable input. This module turns its normalized
checkpoint into a new local data plane and then calls the same materializer ``ummanu upgrade``
uses.

The secret store opens before anything reads ``runtime.env``, because on a clean host that file
does not exist yet: it is what the store writes once the recovery phrase rebuilds the
installation key. Without the phrase the recovery still brings back everything that needs no
credentials and reports which secrets stayed locked or went missing.

It deliberately does not install the board store (`bootstrap` does): its supported version is a
product decision gate, so a missing store is reported before any live state is written.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ummanu import _proc, state_repo
from ummanu._fsutil import (
    ndjson_line,
    ndjson_lines,
    publish_component_entries,
    publish_state_atomic,
    write_json,
    write_text_atomic,
)
from ummanu.board.backend import CARD, board_client
from ummanu.board.checkpoint_layout import CheckpointBoard, CheckpointLayoutError, open_checkpoint_board
from ummanu.checkpoint import SNAPSHOT_BASE_REF, SNAPSHOT_BRANCH, SNAPSHOT_REF
from ummanu.config import (
    ConfigError,
    DataDirError,
    instance_offsite_remote,
    load_config,
    recovered_instance_locations,
    validate_instance,
)
from ummanu.data import init_layout, manifest_for
from ummanu.host import foreign_units
from ummanu.host_apply import (
    SystemdUnitInstaller,
    resolve_runtime_owner,
)
from ummanu.infra import snapshot_tree
from ummanu.infra.export_allowlist import is_exported
from ummanu.infra.github_credential import (
    CredentialError,
    RemoteExecution,
    bootstrap_file_owner_is_allowed,
    validate_checkpoint_credential,
)
from ummanu.memory.client_config import (
    ClientConfigError,
    bridge_executable,
    packaged_codex_home,
    seed_codex_home,
)
from ummanu.memory_journal import export_memory_snapshot
from ummanu.projects.availability import ProjectAvailability, binding_disabled
from ummanu.restore import (
    RestoreError,
    import_normalized_board,
    mark_reconcile_applied,
    rebuild_memory_index,
    restore_findings,
    restore_state,
)
from ummanu.runtime.codex_home import managed_codex_homes
from ummanu.runtime.paths import PRODUCT_DIRNAME, PRODUCT_ENV, component_enabled
from ummanu.runtime.shared_state import resolve_pipeline_state_dir
from ummanu.runtime_env import (
    RuntimeEnvError,
    RuntimeEnvMissing,
    instance_runtime_env_path,
    read_runtime_env,
)
from ummanu.secret_recover import SecretRecovery, recover_secrets
from ummanu.secret_store import (
    MATERIALIZE_FILE,
    SecretStoreError,
    is_initialized,
    key_path,
    list_secrets,
    materialize_path,
    normalize_phrase,
)
from ummanu.state_repo import StateRepoError
from ummanu.tasks import TaskError, TaskReader
from ummanu.upgrade import (
    STEPS,
    GitError,
    UpgradeContext,
    UpgradeResult,
    _set_runtime_owner,
    default_product_root,
    run_steps,
    step_head_registry,
    step_host,
    step_pipeline_state,
    workspaces_root,
)
from ummanu.webfront.commands import configured_sites, missing_sites_message

CHECKPOINT_BOARD = ("cards.ndjson", "sprints.ndjson", "events.ndjson", "audit.ndjson", "export.json")
CHECKPOINT_RUNS = ("runs.ndjson", "claims.json", "watermarks.json", "export.json")
# `packaging/systemd/ummanu-web-front.service` runs the distribution's Caddy at this path.
WEB_FRONT_COMPONENT = "web-front"
CADDY_BINARY = Path("/usr/bin/caddy")


class InstallError(RuntimeError):
    """A controlled install/recovery refusal."""


@dataclass
class InstallStep:
    name: str
    status: str
    detail: str = ""


@dataclass(frozen=True)
class ProjectProvisionResult:
    project_id: str
    target: str
    transport: str
    outcome: str
    code: str
    reason: str
    retryable: bool


@dataclass
class InstallResult:
    steps: list[InstallStep] = field(default_factory=list)
    projects: list[ProjectProvisionResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def status(self) -> str:
        if any(step.status == "failed" for step in self.steps):
            return "failed"
        if any(step.status == "degraded" for step in self.steps) or any(
            project.outcome == "failed" for project in self.projects
        ):
            return "degraded"
        return "ok"

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.steps.append(InstallStep(name, status, detail))

    def render(self) -> str:
        lines = ["ummanu install"]
        for step in self.steps:
            suffix = f": {step.detail}" if step.detail else ""
            lines.append(f"  {step.status:9} {step.name}{suffix}")
        if self.projects:
            lines.append("projects:")
            for project in self.projects:
                lines.append(
                    f"  {project.outcome:9} {project.project_id} "
                    f"target={project.target} transport={project.transport} "
                    f"code={project.code} retryable={'yes' if project.retryable else 'no'}: "
                    f"{project.reason}"
                )
        lines.append("status: " + self.status)
        return "\n".join(lines)


@dataclass(frozen=True)
class PipelineStateMaterialization:
    records: int
    changed: bool


RECOVERY_PROGRESS_FILE = "recovery-progress.json"


def _run(
    argv: list[str],
    *,
    label: str,
    timeout: int = 120,
    cwd: Path | None = None,
    environment: dict[str, str] | None = None,
) -> str:
    # Installation clones before there is an instance checkout to cross into, so it
    # takes the environment half of the instance-repository boundary on its own.
    child_environment = state_repo.git_env()
    if environment:
        child_environment.update(environment)
    try:
        completed = _proc.run(argv, timeout=timeout, env=child_environment, cwd=cwd)
    except FileNotFoundError:
        raise InstallError(f"{label}: command not found") from None
    except (OSError, subprocess.TimeoutExpired):
        raise InstallError(f"{label}: command could not run") from None
    if completed.returncode:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        raise InstallError(f"{label}: {detail[-1] if detail else f'exited {completed.returncode}'}")
    return (completed.stdout or "").strip()


def _ensure_installation_user(name: str | None, *, recovery: bool, dry_run: bool) -> None:
    if not name:
        return
    try:
        pwd.getpwnam(name)
    except KeyError:
        if dry_run:
            return
        if os.geteuid() != 0:
            raise InstallError(
                f"installation user {name!r} does not exist; create it as root, then rerun"
            ) from None
        _run(["useradd", "--create-home", "--", name], label="create installation user")
        return
    if not recovery:
        raise InstallError(
            f"installation user {name!r} already exists; choose --recover for a lost-machine "
            "restore or use the separate adopt workflow for a live installation"
        )


def _set_installation_owner(path: Path, name: str | None) -> None:
    """Give root-created installation files safe ownership for the runtime user."""
    try:
        _set_runtime_owner(path, name)
    except GitError as exc:
        raise InstallError(str(exc)) from None


def _establish_recovery_ownership_barrier(
    instance_dir: Path,
    data_dir: Path | None,
    installation_user: str | None,
    *,
    additional_paths: tuple[Path, ...] = (),
) -> None:
    """Hand every restored root-owned path to its declared runtime account.

    This is the single recovery boundary before user-owned Git, remote execution
    or host materialization may consume restored secrets.
    """
    ownership_roots = (
        instance_dir,
        *((data_dir,) if data_dir is not None else ()),
        *additional_paths,
    )
    for path in dict.fromkeys(ownership_roots):
        _set_installation_owner(path, installation_user)
    key = key_path(instance_dir)
    if not key.exists() and not key.is_symlink():
        return
    try:
        info = key.lstat()
    except OSError as exc:
        raise InstallError(f"could not inspect recovered installation key: {exc}") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise InstallError("recovered installation key is not a regular non-symlink file")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise InstallError("recovered installation key must have mode 0600")
    if installation_user and os.geteuid() == 0:
        try:
            account = pwd.getpwnam(installation_user)
        except KeyError:
            raise InstallError(f"installation user {installation_user!r} does not exist") from None
        if (info.st_uid, info.st_gid) != (account.pw_uid, account.pw_gid):
            raise InstallError(
                f"recovered installation key is not owned by installation user {installation_user!r}"
            )


def _clone_or_reuse(
    remote: str,
    target: Path,
    *,
    recovery: bool,
    dry_run: bool,
    bootstrap_credential: Path | None = None,
    installation_user: str | None = None,
) -> str:
    empty_target = target.is_dir() and not any(target.iterdir())
    if not target.exists() or empty_target:
        if dry_run:
            return "would clone private instance remote"
        target.parent.mkdir(parents=True, exist_ok=True)
        _clone_instance(
            remote,
            target,
            bootstrap_credential=bootstrap_credential,
            installation_user=installation_user,
        )
        return "cloned private instance remote"
    if not target.is_dir() or not (target / ".git").exists():
        raise InstallError(
            f"target {target} is not a valid instance checkout; remove a failed partial target "
            "or choose a fresh --instance-dir, no files were overwritten"
        )
    # This checkout belongs to the runtime user.  Do not make a root Git process trust it: Git
    # loads repository configuration before every command, including executable fsmonitor hooks.
    # state_repo crosses to the owner before Git starts.
    try:
        origin = state_repo.git(
            target, ["remote", "get-url", "origin"], label="inspect instance remote"
        ).strip()
    except state_repo.StateRepoError:
        raise InstallError(
            f"existing target {target} is not a usable instance checkout; preserve and inspect it, "
            "then remove a failed partial target or choose a fresh --instance-dir, no files were overwritten"
        ) from None
    if origin != remote:
        raise InstallError("existing target belongs to a different instance remote")
    # Only a checkout explicitly prepared by `bootstrap` may continue into its
    # first install. runtime.env alone is normal state of every live installation.
    if not recovery and not (target / ".ummanu-bootstrap").is_file():
        raise InstallError(
            f"target {target} already contains an installation; choose --recover or use the "
            "separate adopt workflow"
        )
    try:
        dirty = _checkout_changes(target, label="inspect instance checkout")
    except state_repo.StateRepoError as exc:
        raise InstallError(str(exc)) from None
    if dirty:
        raise InstallError("instance checkout has local changes; recovery will not overwrite them")
    if dry_run:
        return "reused checkpoint checkout"
    remote_git = RemoteExecution(
        remote, "recovery-reuse", instance_dir=target, bootstrap_file=bootstrap_credential
    )
    try:
        fetch = remote_git.run_instance(
            target,
            ["fetch", "--quiet", "--no-tags", "origin"],
            label="fetch instance remote",
        )
        if fetch.returncode:
            detail = (fetch.stderr or fetch.stdout or "").strip().splitlines()
            raise InstallError(
                f"fetch instance remote: {detail[-1] if detail else f'exited {fetch.returncode}'}"
            )
        merge = remote_git.run_instance(
            target, ["merge", "--ff-only", "@{u}"], label="fast-forward instance checkout"
        )
        if merge.returncode:
            detail = (merge.stderr or merge.stdout or "").strip().splitlines()
            raise InstallError(
                f"fast-forward instance checkout: {detail[-1] if detail else f'exited {merge.returncode}'}"
            )
    except CredentialError as exc:
        raise InstallError(str(exc)) from None
    return "reused checkpoint checkout"


def _checkout_changes(target: Path, *, label: str) -> str:
    """The instance checkout's local changes, one porcelain entry per line; empty when clean.

    An untracked file the export allowlist does not match (`secrets/installation.key`,
    `runtime.env`, `board-store.env`) is the host's own and never a checkpoint change, so it is not
    one here: no product code keeps a `.gitignore` entry for it any more (docs/RECOVERY.md,
    "Local-file exclusion"). Every other change, tracked or untracked, still counts.
    """
    output = state_repo.git(target, ["status", "--porcelain", "-z", "--untracked-files=all"], label=label)
    return "\n".join(
        entry
        for entry in output.split("\0")
        if entry and not (entry.startswith("?? ") and not is_exported(entry[3:]))
    )


def _clone_instance(
    remote: str,
    target: Path,
    *,
    bootstrap_credential: Path | None,
    installation_user: str | None = None,
) -> None:
    temporary: Path | None = None
    claimed_target = False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.clone-", dir=target.parent))
        staging = temporary / "checkout"
        RemoteExecution(remote, "initial-clone", bootstrap_file=bootstrap_credential).run_clone(
            staging,
            label="clone instance remote",
            timeout=300,
            clone_args=["--depth=1", "--single-branch", "--no-tags", "--no-local"],
        )
        _validate_initial_clone(staging, remote)
        _set_installation_owner(staging, installation_user)
        if not target.exists():
            target.mkdir(mode=0o700)
            claimed_target = True
        elif not target.is_dir() or any(target.iterdir()):
            raise InstallError(
                f"target {target} changed during clone; choose a fresh --instance-dir, "
                "no files were overwritten"
            )
        os.replace(staging, target)
        claimed_target = False
    except CredentialError as exc:
        raise InstallError(str(exc)) from None
    except OSError as exc:
        raise InstallError("adopt cloned instance checkout: atomic replacement failed") from exc
    finally:
        if claimed_target:
            try:
                target.rmdir()
            except OSError:
                pass
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)


def _validate_initial_clone(staging: Path, remote: str) -> None:
    """Prove the bounded clone is the remote's checked-out branch tip before adoption."""

    def inspect(args: list[str], label: str) -> str:
        return _run(["git", "-C", str(staging), *args], label=label)

    if inspect(["rev-parse", "--is-inside-work-tree"], "validate cloned repository") != "true":
        raise InstallError("validate cloned repository: not a Git work tree")
    if inspect(["remote", "get-url", "origin"], "validate cloned origin") != remote:
        raise InstallError("validate cloned origin: remote identity mismatch")
    branch = inspect(["symbolic-ref", "--quiet", "--short", "HEAD"], "validate cloned branch")
    if not branch:
        raise InstallError("validate cloned branch: remote default branch is unavailable")
    try:
        upstream = inspect(
            ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
            "validate cloned upstream",
        )
    except InstallError:
        raise InstallError("validate cloned branch: remote default branch is unavailable") from None
    if upstream != f"origin/{branch}":
        raise InstallError("validate cloned upstream: tracking relationship mismatch")
    head = inspect(["rev-parse", "HEAD"], "validate cloned revision")
    upstream_head = inspect(["rev-parse", "@{u}"], "validate cloned upstream revision")
    if head != upstream_head:
        raise InstallError("validate cloned revision: checkout is not at the remote tip")
    if inspect(["rev-parse", "--is-shallow-repository"], "validate shallow clone") != "true":
        raise InstallError("validate shallow clone: bounded history was not established")


@dataclass(frozen=True)
class SnapshotCheckout:
    """The clone step's result for a remote whose tip is an exporter snapshot."""

    tip: str
    repository: Path
    # The extracted tree: `state/board` and `state/runs` are read here, never from the live root.
    checkpoint_root: Path
    # Private staging beside the live root that holds `checkpoint_root`; the caller removes it.
    scratch: Path
    changed: bool
    detail: str
    data_dir: Path


def _reads_remote_shape(target: Path, *, recovery: bool, dry_run: bool) -> bool:
    """The one shape decision (docs/RECOVERY.md, "Two remote shapes"): whether the clone step reads
    the remote tip before it decides how to clone, for `install`, `recover` and `bootstrap` alike.

    A recovery into an absent or empty target reads it; a dry run there stays offline. A live root an
    earlier snapshot recovery or bootstrap laid out (an `instance.yaml` and no `.git`) is read by a
    recovery and by the first install of a bootstrapped target. A work-tree target, any other
    non-empty target and a fresh install never read it.
    """
    if not target.exists() or (target.is_dir() and not any(target.iterdir())):
        return recovery and not dry_run
    snapshot_live_root = (target / "instance.yaml").is_file() and not (target / ".git").exists()
    return snapshot_live_root and (recovery or (target / ".ummanu-bootstrap").is_file())


def _snapshot_checkout(
    remote: str,
    target: Path,
    *,
    dry_run: bool,
    bootstrap_credential: Path | None = None,
    installation_user: str | None = None,
) -> SnapshotCheckout | None:
    """The clone step for a snapshot remote, or None when the remote tip is a legacy checkpoint.

    The shape decision reads the remote tip from a depth-1 bare clone in private staging: a tree
    with `snapshot-manifest.json` at its root is an exporter snapshot. Its tree is extracted and
    checked against the manifest, and the live root is checked against the tree, before anything
    outside the staging is written. Then the bare clone becomes the snapshot repository (or an
    existing one there is fast-forwarded to the tip), the takeover marker names the tip, and the
    live root is laid out in staging and moved into place. A dry run writes none of that.
    """
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        scratch = Path(tempfile.mkdtemp(prefix=f".{target.name}.snapshot-", dir=target.parent))
    except OSError as exc:
        raise InstallError(f"could not stage the instance remote: {exc}") from None
    keep = False
    try:
        staging = scratch / "instance.git"
        RemoteExecution(remote, "initial-clone", bootstrap_file=bootstrap_credential).run_clone(
            staging,
            label="clone instance remote",
            timeout=300,
            clone_args=["--bare", "--depth=1", "--single-branch", "--no-tags", "--no-local"],
        )
        branch, tip = _validate_snapshot_clone(staging, remote)
        payload = snapshot_tree.manifest_payload(staging, tip)
        if payload is None:
            return None
        manifest = snapshot_tree.parse_manifest(payload)
        if branch != SNAPSHOT_BRANCH:
            raise InstallError(
                f"snapshot remote's default branch is {branch}, but the exporter publishes {SNAPSHOT_BRANCH}"
            )
        tree = snapshot_tree.extract(staging, tip, scratch / "tree", manifest)
        snapshot_tree.require_known_schema(manifest, _board_schema_lineage())
        try:
            data_dir, repository = recovered_instance_locations(tree.root / "instance.yaml", target)
            offsite_remote = instance_offsite_remote(tree.root / "instance.yaml")
        except DataDirError as exc:
            raise InstallError(f"snapshot instance.yaml: {exc}") from None
        live_root_in_data = _snapshot_layout(target, data_dir, repository)
        live = snapshot_tree.live_root_state(target, tree)
        # Shape (b): the live root `<data>/<name>` and this staging beside it, exactly, are not data.
        not_data = (target, scratch) if live_root_in_data else ()
        bootstrap_evidence = _checked_data_target(data_dir, ignore=not_data)
        existing = _existing_snapshot_tip(repository)
        if dry_run:
            keep = True
            return SnapshotCheckout(
                tip,
                repository,
                tree.root,
                scratch,
                True,
                f"would recover exporter snapshot {tip[:12]}",
                data_dir,
            )
        changed = live != "same" or existing != tip
        inside_data = repository.is_relative_to(data_dir) or live_root_in_data
        if inside_data and (not _data_target_entries(data_dir, not_data) or bootstrap_evidence):
            # The checkpoint step accepts a data root only empty or laid out by ummanu, so lay it out
            # before the snapshot repository or the live root becomes one of its entries.
            init_layout(data_dir)
        if not existing:
            _adopt_snapshot_repository(staging, repository, installation_user)
        elif existing != tip:
            _fast_forward_snapshot_repository(
                repository, remote, existing, tip, target=target, bootstrap_credential=bootstrap_credential
            )
        _point_snapshot_remote(repository, offsite_remote)
        _mark_recovered_tip(repository, tip)
        if live != "same":
            _materialize_live_root(tree, target, scratch / "live", installation_user)
        keep = True
        verb = "recovered" if changed else "reused"
        return SnapshotCheckout(
            tip,
            repository,
            tree.root,
            scratch,
            changed,
            f"{verb} exporter snapshot {tip[:12]} into {repository}",
            data_dir,
        )
    except CredentialError as exc:
        raise InstallError(str(exc)) from None
    except snapshot_tree.SnapshotTreeError as exc:
        raise InstallError(f"snapshot recovery refused: {exc}") from None
    except OSError as exc:
        raise InstallError(f"snapshot recovery failed: {exc}") from None
    finally:
        if not keep:
            shutil.rmtree(scratch, ignore_errors=True)


def _snapshot_layout(target: Path, data_dir: Path, repository: Path) -> bool:
    """The one layout check of a snapshot recovery; True for shape (b), False for (a), else refused.

    Accepted (docs/RECOVERY.md, "Snapshot recovery"):
    (a) the live root and the data directory are disjoint, neither containing the other;
    (b) the live root is a direct child of the data directory, `<data>/<name>` (`data_dir: ..`).
    In both the snapshot repository lies outside the live root. Anything inside the live root
    would be written before the live root is laid out, which then could no longer be absent or
    empty; a live root deeper in the data directory would make its parents look like data.
    """
    for location, label in ((data_dir, "data directory"), (repository, "snapshot repository")):
        if location == target or location.is_relative_to(target):
            raise InstallError(
                f"snapshot instance.yaml puts the {label} {location} inside the live root {target}; "
                "this layout is not supported, nothing was written"
            )
    if not target.is_relative_to(data_dir):
        return False
    if target.parent != data_dir:
        raise InstallError(
            f"the live root {target} lies inside the data directory {data_dir} but is not a direct "
            "child of it; this layout is not supported, nothing was written"
        )
    return True


def _validate_snapshot_clone(staging: Path, remote: str) -> tuple[str, str]:
    """Prove the bounded bare clone is the remote's default-branch tip; returns (branch, tip)."""

    def inspect(args: list[str], label: str) -> str:
        return _run(["git", "--git-dir", str(staging), *args], label=label)

    if inspect(["rev-parse", "--is-bare-repository"], "validate cloned repository") != "true":
        raise InstallError("validate cloned repository: not a bare Git repository")
    if inspect(["remote", "get-url", "origin"], "validate cloned origin") != remote:
        raise InstallError("validate cloned origin: remote identity mismatch")
    try:
        branch = inspect(["symbolic-ref", "--quiet", "--short", "HEAD"], "validate cloned branch")
        tip = inspect(["rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}"], "validate cloned branch")
    except InstallError:
        raise InstallError("validate cloned branch: remote default branch is unavailable") from None
    if not branch or inspect(["rev-parse", "HEAD"], "validate cloned revision") != tip:
        raise InstallError("validate cloned branch: remote default branch is unavailable")
    if inspect(["rev-parse", "--is-shallow-repository"], "validate shallow clone") != "true":
        raise InstallError("validate shallow clone: bounded history was not established")
    return branch, tip


def _board_schema_lineage() -> tuple[str, ...]:
    from ummanu.board.migrate import lineage
    from ummanu.board.store import BoardStoreError

    try:
        return lineage()
    except (BoardStoreError, ImportError, OSError) as exc:
        raise InstallError(f"could not read this product's board schema lineage: {exc}") from None


def _snapshot_git(repository: Path, args: list[str], label: str, *, input: str | None = None) -> str:
    try:
        completed = state_repo.run_git(repository, ["--git-dir", str(repository), *args], label=label, input=input)
    except StateRepoError as exc:
        raise InstallError(str(exc)) from None
    if completed.returncode:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        raise InstallError(f"{label}: {detail[-1] if detail else f'exited {completed.returncode}'}")
    return (completed.stdout or "").strip()


def _existing_snapshot_tip(repository: Path) -> str:
    """The snapshot branch tip of a repository already at the location; "" when there is none yet."""
    if repository.is_symlink() or (repository.exists() and not repository.is_dir()):
        raise InstallError(f"snapshot repository {repository} is not a directory; no files were overwritten")
    if not repository.exists() or not any(repository.iterdir()):
        return ""
    try:
        bare = _snapshot_git(repository, ["rev-parse", "--is-bare-repository"], "inspect snapshot repository")
    except InstallError:
        bare = ""
    if bare != "true":
        raise InstallError(
            f"snapshot repository {repository} exists but is not a bare Git repository; no files were overwritten"
        )
    try:
        return _snapshot_git(
            repository, ["rev-parse", "--verify", f"{SNAPSHOT_REF}^{{commit}}"], "inspect snapshot repository"
        )
    except InstallError:
        return ""


def _adopt_snapshot_repository(staging: Path, repository: Path, installation_user: str | None) -> None:
    first_created: Path | None = None
    parent = repository.parent
    while not parent.exists():
        first_created, parent = parent, parent.parent
    repository.parent.mkdir(parents=True, exist_ok=True)
    # The exporter stages its cuts beside the repository, so a parent recovery created is the user's too.
    if first_created is not None:
        _set_installation_owner(first_created, installation_user)
    _set_installation_owner(staging, installation_user)
    if repository.exists() and any(repository.iterdir()):
        raise InstallError(f"snapshot repository {repository} changed during recovery; no files were overwritten")
    os.replace(staging, repository)


def _fast_forward_snapshot_repository(
    repository: Path,
    remote: str,
    existing: str,
    tip: str,
    *,
    target: Path,
    bootstrap_credential: Path | None,
) -> None:
    """Move an existing snapshot repository to the remote tip, fast-forward only."""
    remote_git = RemoteExecution(remote, "recovery-reuse", instance_dir=target, bootstrap_file=bootstrap_credential)
    fetched = remote_git.run_instance(
        repository,
        ["--git-dir", str(repository), "fetch", "--quiet", "--no-tags", remote, SNAPSHOT_REF],
        label="fetch snapshot remote",
        timeout=300,
    )
    if fetched.returncode:
        detail = (fetched.stderr or fetched.stdout or "").strip().splitlines()
        raise InstallError(f"fetch snapshot remote: {detail[-1] if detail else f'exited {fetched.returncode}'}")
    if _snapshot_git(repository, ["rev-parse", "FETCH_HEAD"], "inspect fetched snapshot") != tip:
        raise InstallError("the snapshot remote moved during recovery; rerun the same command")
    try:
        ancestry = state_repo.run_git(
            repository,
            ["--git-dir", str(repository), "merge-base", "--is-ancestor", existing, tip],
            label="snapshot ancestry",
        )
    except StateRepoError as exc:
        raise InstallError(str(exc)) from None
    if ancestry.returncode == 1:
        raise InstallError(
            f"snapshot repository {repository} is at {existing[:12]}, which the remote tip {tip[:12]} does "
            "not extend; preserve and inspect it, no files were overwritten"
        )
    if ancestry.returncode:
        detail = (ancestry.stderr or "").strip().splitlines()
        raise InstallError(f"snapshot ancestry: {detail[-1] if detail else 'git error'}")
    _snapshot_git(
        repository,
        ["update-ref", "-m", "recover: fast-forward to the remote tip", SNAPSHOT_REF, tip, existing],
        "fast-forward snapshot repository",
    )


def _point_snapshot_remote(repository: Path, remote: str) -> None:
    """Set `origin` to `offsite.instance_remote`, the remote the snapshot pusher publishes to."""
    if remote:
        _snapshot_git(repository, ["config", "--replace-all", "remote.origin.url", remote], "set snapshot remote")


def _mark_recovered_tip(repository: Path, tip: str) -> None:
    """Make the recovered tip the takeover base: doctor's foreign-commit walk starts after it."""
    try:
        marker = _snapshot_git(repository, ["cat-file", "blob", SNAPSHOT_BASE_REF], "read takeover marker")
    except InstallError:
        marker = ""
    if marker == tip:
        return
    blob = _snapshot_git(repository, ["hash-object", "-w", "--stdin"], "write takeover marker", input=f"{tip}\n")
    _snapshot_git(
        repository, ["update-ref", "-m", "recover: takeover base", SNAPSHOT_BASE_REF, blob], "write takeover marker"
    )


def _materialize_live_root(
    tree: snapshot_tree.SnapshotTree, target: Path, staging: Path, installation_user: str | None
) -> None:
    """Lay the live root out in staging and move it into place; an absent or empty target only."""
    snapshot_tree.stage_live_root(tree, staging)
    _set_installation_owner(staging, installation_user)
    claimed_target = False
    try:
        if not target.exists():
            target.mkdir(mode=0o700)
            claimed_target = True
        elif not target.is_dir() or any(target.iterdir()):
            raise InstallError(
                f"target {target} changed during recovery; choose a fresh --instance-dir, no files were overwritten"
            )
        os.replace(staging, target)
        claimed_target = False
    except OSError as exc:
        raise InstallError("adopt the recovered live root: atomic replacement failed") from exc
    finally:
        if claimed_target:
            try:
                target.rmdir()
            except OSError:
                pass


def _bootstrap_credential(args: argparse.Namespace, target: Path) -> tuple[Path | None, Path | None]:
    """Return external bootstrap material and a disposable file to remove afterwards."""
    source = getattr(args, "bootstrap_credential_file", None)
    from_stdin = bool(getattr(args, "bootstrap_credential_stdin", False))
    if not source and not from_stdin:
        return None, None
    if source:
        path = Path(source).expanduser()
        try:
            info = path.lstat()
        except OSError as exc:
            raise InstallError("could not inspect bootstrap credential file") from exc
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise InstallError("bootstrap credential file must be a regular mode-0600 file")
        if not bootstrap_file_owner_is_allowed(info):
            raise InstallError("bootstrap credential file belongs to another user")
        try:
            validate_checkpoint_credential(path.read_bytes())
        except OSError as exc:
            raise InstallError("bootstrap credential file is unreadable") from exc
        except CredentialError as exc:
            raise InstallError(f"bootstrap credential content is rejected: {exc}") from None
        return path, None
    try:
        value = sys.stdin.buffer.read()
        validate_checkpoint_credential(value)
    except CredentialError as exc:
        raise InstallError(f"bootstrap credential content is rejected: {exc}") from None
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".ummanu-bootstrap-credential-", dir=target.parent)
    path = Path(temporary)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
    except OSError as exc:
        raise InstallError("could not prepare bootstrap credential") from exc
    return path, path


def _runtime_env_file(instance_dir: Path, override: str | None) -> Path:
    """The env file this installation runs on, override included."""
    return instance_runtime_env_path(instance_dir, override)


def _recovery_phrase(args: argparse.Namespace, instance_dir: Path) -> str | None:
    """Read the phrase the same way a secret value is read: never from argv.

    A phrase on the command line lands in the process table and the shell history, and one in the
    environment is inherited by everything this command starts, so the only inputs are a file,
    standard input and a non-echoing terminal prompt. No flag and no terminal means no phrase.
    """
    path = getattr(args, "recovery_phrase_file", None)
    if path:
        source = Path(path).expanduser()
        try:
            raw = source.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise InstallError(f"could not read the recovery phrase file: {exc}") from None
        return _clean_phrase(raw, f"{source} holds no recovery phrase")
    if getattr(args, "recovery_phrase_stdin", False):
        return _clean_phrase(sys.stdin.read(), "no recovery phrase on standard input")
    if is_initialized(instance_dir) and not key_path(instance_dir).exists() and sys.stdin.isatty():
        answer = getpass.getpass("Recovery phrase (empty to continue without it): ")
        if answer.strip():
            return _clean_phrase(answer, "no recovery phrase entered")
    return None


def _clean_phrase(raw: str, empty: str) -> str:
    try:
        return normalize_phrase(raw)
    except SecretStoreError:
        raise InstallError(empty) from None


def _open_secret_store(
    instance_dir: Path, runtime_env: Path, *, phrase: str | None, dry_run: bool
) -> SecretRecovery:
    """Open the store before anything asks for credentials.

    Which file `runtime-env` materializes into belongs to the installation being recovered, not to
    the host default, so the resolution override is pinned to this target for the duration of the
    write. That is also what keeps a recovery drill off a live installation's env file.
    """
    try:
        with _runtime_environment({"UMMANU_RUNTIME_ENV_FILE": str(runtime_env)}):
            return recover_secrets(instance_dir, phrase=phrase, dry_run=dry_run)
    except (SecretStoreError, StateRepoError) as exc:
        raise InstallError(f"secret store: {exc}") from None


def _checked_file_targets(instance_dir: Path) -> frozenset[Path]:
    """Refuse a catalog file target already in a data directory ummanu has not laid out, before the
    store writes anything; return the targets in the data directory this run may create.

    The store replaces a regular file at a target path, so once it has run nothing tells an operator's
    file there from the one it wrote. A target that is already there while the data directory holds
    no ummanu layout is therefore foreign and is refused by path, with no secret written. Only the
    targets returned here, absent before the store ran, count as this run's own afterwards
    (`_checked_data_target`). A laid-out data directory has its targets refreshed as before.
    """
    if not is_initialized(instance_dir):
        return frozenset()
    report = validate_instance(instance_dir)
    if not report.ok or report.data_dir is None:
        # The instance is refused right after the store step; there is no data directory to guard.
        return frozenset()
    data_dir = Path(report.data_dir).expanduser().resolve()
    try:
        targets = {
            _named_path(materialize_path(instance_dir, entry))
            for entry in list_secrets(instance_dir)
            if (entry.get("materialize") or {}).get("target") == MATERIALIZE_FILE
        }
    except (SecretStoreError, StateRepoError) as exc:
        raise InstallError(f"secret store: {exc}") from None
    inside = {path for path in targets if path.is_relative_to(data_dir)}
    present = sorted(path for path in inside if path.exists() or path.is_symlink())
    if present and not _valid_existing_layout(data_dir):
        raise InstallError(
            f"data target {data_dir} is not an installation created by ummanu, and it already holds "
            f"{', '.join(map(str, present))}, where the secret store would write; nothing was written: "
            "choose adopt or a clean recovery target"
        )
    return frozenset(inside - set(present))


def _secret_store_step(recovery: SecretRecovery) -> tuple[str, str]:
    if not recovery.store_present:
        return "skipped", "no secret store in the instance repo"
    if not recovery.unlocked:
        return "unchanged", recovery.summary()
    return "changed" if recovery.changed else "unchanged", recovery.summary()


def _add_secret_steps(result: InstallResult, recovery: SecretRecovery) -> None:
    """One line per secret that did not come back, ids and targets only."""
    for status, entries in (("locked", recovery.locked), ("missing", recovery.missing)):
        for entry in entries:
            where = entry.get("path") or entry.get("target") or "not materialized"
            result.add(f"secret:{entry['id']}", status, f"{entry.get('environment', '-')} -> {where}")
    for path in recovery.withheld:
        result.add(f"secret-file:{path}", "withheld", "a secret this file needs is not readable")


def _blocked_by_secrets(cause: InstallError, recovery: SecretRecovery, runtime_env: Path) -> InstallError:
    """Say what is still closed instead of asking for a hand-written file."""
    reason = (
        str(cause) if runtime_env.exists() else f"{runtime_env} is not there, and the store is what writes it"
    )
    lines = [f"recovery is incomplete: {recovery.summary()}", f"  cause: {reason}"]
    lines.extend(recovery.render())
    return InstallError("\n".join(lines))


@contextmanager
def _runtime_environment(values: dict[str, str]) -> Iterator[None]:
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _instance_host(instance_dir: Path) -> dict[str, Any] | None:
    """The `host` block of `instance.yaml`; None when the file cannot be read or has none."""
    try:
        instance = load_config(Path(instance_dir) / "instance.yaml")
    except ConfigError:
        return None
    host = instance.get("host") if isinstance(instance, dict) else None
    return host if isinstance(host, dict) else None


def web_front_wanted(instance_dir: Path) -> bool:
    """Whether this installation's desired units include `<prefix>web-front.service`.

    The same answer the host plan gives: units exist only under a unit prefix, for an enabled
    component, and not when the installation declares the name foreign. An instance that cannot be
    read wants nothing here; the install refuses it on its own terms.
    """
    host = _instance_host(instance_dir)
    if host is None:
        return False
    prefix = host.get("unit_prefix")
    if not isinstance(prefix, str) or not prefix:
        return False
    return component_enabled(host, WEB_FRONT_COMPONENT) and (
        f"{prefix}{WEB_FRONT_COMPONENT}.service" not in foreign_units(host)
    )


def caddy_installed() -> bool:
    """Whether the binary `ummanu-web-front.service` executes is on this host."""
    return os.access(CADDY_BINARY, os.X_OK)


def check_prerequisites(instance_dir: Path) -> None:
    # The prerequisite is the board this installation serves cards from: the PostgreSQL store.
    try:
        TaskReader(board_client(instance_dir, serves=(CARD,))).list()
    except TaskError as exc:
        raise InstallError(f"PostgreSQL prerequisite failed: {exc.message}") from None
    # Up front, not at materializer verify: without it the front crash-loops (203/EXEC).
    if web_front_wanted(instance_dir) and not caddy_installed():
        raise InstallError(
            f"caddy prerequisite failed: the web-front component is enabled and {CADDY_BINARY} is "
            "absent; install the distribution's caddy (`ummanu bootstrap` does) or disable the component"
        )
    # The rendered Caddyfile is data-dir state no checkpoint carries; the materializer renders it from
    # instance config (`upgrade.step_web_front_config`), so without sites there the front cannot start.
    if web_front_wanted(instance_dir) and not configured_sites(_instance_host(instance_dir)):
        raise InstallError(f"web-front prerequisite failed: {missing_sites_message(instance_dir)}")


def _valid_existing_layout(data_dir: Path) -> bool:
    try:
        actual = json.loads((data_dir / "data-manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return actual == manifest_for(data_dir)


def _data_target_entries(
    data_dir: Path, ignore: tuple[Path, ...] = (), own: tuple[Path, ...] = ()
) -> set[str]:
    """The data target's entries, less the ones that are exactly a path of `ignore` and the ones
    that hold nothing but files of `own`."""
    if not data_dir.exists():
        return set()
    written = frozenset(_named_path(path) for path in own)
    return {
        entry.name
        for entry in data_dir.iterdir()
        if entry not in ignore and not _holds_only(_named_path(entry), written)
    }


def _named_path(path: Path) -> Path:
    """`path` with its directories resolved and its own name kept, so a link is still seen as one."""
    path = Path(path).expanduser()
    return path.parent.resolve() / path.name


def _holds_only(path: Path, written: frozenset[Path]) -> bool:
    """Whether `path` is one of `written`, a regular file, or a directory holding only such files."""
    if path.is_symlink():
        return False
    if path in written:
        return path.is_file()
    if not path.is_dir():
        return False
    children = list(path.iterdir())
    return bool(children) and all(_holds_only(child, written) for child in children)


def _checked_data_target(
    data_dir: Path, *, ignore: tuple[Path, ...] = (), own: tuple[Path, ...] = ()
) -> bool:
    """Refuse a non-empty data target ummanu did not create; True when it holds bootstrap evidence only.

    `ignore` names direct entries of the data target that are not its contents: a snapshot
    recovery's live root `<data>/<name>` and the staging it created beside it. `own` names the files
    this recovery's own secret store step has just created there, at paths that did not exist before
    it ran (`_checked_file_targets`; a file target such as `<data>/webfront/owner-password.env`, which
    a legacy recovery materializes before this check runs): an entry holding only those is this
    run's, not a foreign installation's. Nothing else is
    left out, siblings and deeper paths included, and a foreign file beside an own one is refused.
    """
    entries = _data_target_entries(data_dir, ignore, own)
    if not entries:
        return False
    # An older bootstrap recorded its host unit in `host-managed.json` before checkpoint
    # materialization so the first full reconcile could prove ownership. That one evidence
    # file is compatible with an otherwise empty data root.
    bootstrap_evidence = entries == {"host-managed.json"}
    if not bootstrap_evidence and not _valid_existing_layout(data_dir):
        raise InstallError(
            f"data target {data_dir} is not an installation created by ummanu "
            f"(it holds {', '.join(sorted(entries))}); choose adopt or a clean recovery target"
        )
    return bootstrap_evidence


def materialize_checkpoint(
    instance_dir: Path,
    data_dir: Path,
    *,
    dry_run: bool = False,
    own: tuple[Path, ...] = (),
) -> tuple[int, int]:
    """Validate the checkpoint and optionally publish it into the local layout.

    `instance_dir` is where `state/board` and `state/runs` are read: the checkout of a legacy
    checkpoint, or the extracted tree of an exporter snapshot. `own` is what this recovery's secret
    store step wrote into the data target before it (`_checked_data_target`).
    """
    bootstrap_evidence = _checked_data_target(data_dir, own=own)
    board_source = instance_dir / "state" / "board"
    runs_source = instance_dir / "state" / "runs"
    try:
        board = open_checkpoint_board(board_source)
    except CheckpointLayoutError as exc:
        raise InstallError(f"private checkpoint has an unreadable board layout: {exc}") from None
    for name in ("cards.ndjson", "export.json"):
        if not board.has(name):
            raise InstallError(f"private checkpoint is missing state/board/{name}")
    for name in CHECKPOINT_RUNS:
        if not (runs_source / name).is_file():
            raise InstallError(f"private checkpoint is missing state/runs/{name}")

    try:
        card_lines = [line for line in ndjson_lines(board.read_text("cards.ndjson")) if line.strip()]
        run_lines = [
            line
            for line in ndjson_lines((runs_source / "runs.ndjson").read_text(encoding="utf-8"))
            if line.strip()
        ]
        # A checkpoint written before sprints joined the board export carries no
        # sprints.ndjson; its export.json declares no sprint count either, and the
        # next tick writes both.
        sprint_lines = [
            line
            for line in ndjson_lines(
                board.read_text("sprints.ndjson") if board.has("sprints.ndjson") else ""
            )
            if line.strip()
        ]
        cards = [json.loads(line) for line in card_lines]
        sprints = [json.loads(line) for line in sprint_lines]
        for line in run_lines:
            json.loads(line)
        board_export = json.loads(board.read_text("export.json"))
        run_export = json.loads((runs_source / "export.json").read_text(encoding="utf-8"))
        claims = json.loads((runs_source / "claims.json").read_text(encoding="utf-8"))
        watermarks = json.loads((runs_source / "watermarks.json").read_text(encoding="utf-8"))
    except CheckpointLayoutError as exc:
        raise InstallError(f"private checkpoint has an unreadable board layout: {exc}") from None
    except (OSError, UnicodeError, ValueError):
        raise InstallError("private checkpoint contains invalid normalized state") from None
    if not isinstance(board_export, dict) or not isinstance(run_export, dict):
        raise InstallError("private checkpoint contains invalid export metadata")
    if any(not isinstance(card, dict) for card in cards) or board_export.get("card_count") != len(cards):
        raise InstallError("private checkpoint board count does not match cards.ndjson")
    declared_sprints = board_export.get("sprint_count")
    if any(not isinstance(sprint, dict) for sprint in sprints) or (
        declared_sprints is not None and declared_sprints != len(sprints)
    ):
        raise InstallError("private checkpoint sprint count does not match sprints.ndjson")
    run_count = len(run_lines)
    declared_runs = run_export.get("run_record_count")
    if not isinstance(declared_runs, int) or declared_runs != run_count:
        raise InstallError("private checkpoint run count does not match runs.ndjson")
    claim_entries = claims.get("claims") if isinstance(claims, dict) else None
    watermark_entries = watermarks.get("files") if isinstance(watermarks, dict) else None
    if not isinstance(claim_entries, dict) or run_export.get("claim_count") != len(claim_entries):
        raise InstallError("private checkpoint claim count does not match claims.json")
    if not isinstance(watermark_entries, list) or run_export.get("watermark_count") != len(watermark_entries):
        raise InstallError("private checkpoint watermark count does not match watermarks.json")

    if dry_run:
        return len(cards), run_count

    if not _data_target_entries(data_dir, own=own) or bootstrap_evidence:
        init_layout(data_dir)

    board_target = data_dir / "board"
    runs_target = data_dir / "runs"
    board_target.mkdir(parents=True, exist_ok=True)
    runs_target.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix=".checkpoint-board-", dir=board_target) as staging_raw:
            staging = Path(staging_raw)
            write_json(staging / "cards.json", {"version": 1, "cards": cards})
            write_json(staging / "sprints.json", {"version": 1, "sprints": sprints})
            for name in CHECKPOINT_BOARD:
                if board.has(name):
                    write_text_atomic(staging / name, board.read_text(name))
            publish_component_entries(
                staging,
                board_target,
                [
                    "cards.json",
                    "sprints.json",
                    *[n for n in CHECKPOINT_BOARD if (staging / n).is_file()],
                ],
                "checkpoint board materialization",
            )
        with tempfile.TemporaryDirectory(prefix=".checkpoint-runs-", dir=runs_target) as staging_raw:
            staging = Path(staging_raw)
            for name in CHECKPOINT_RUNS:
                write_text_atomic(staging / name, (runs_source / name).read_text(encoding="utf-8"))
            publish_component_entries(
                staging,
                runs_target,
                list(CHECKPOINT_RUNS),
                "checkpoint runs materialization",
            )
    except (OSError, RuntimeError, CheckpointLayoutError) as exc:
        raise InstallError(f"could not materialize checkpoint: {exc}") from None
    return len(cards), run_count


def _restored_run_journals(runs_source: Path) -> dict[Path, list[tuple[int, str]]]:
    """Rebuild the JSONL files whose records the checkpoint normalizes."""
    grouped: dict[Path, list[tuple[int, object]]] = {}
    try:
        lines = ndjson_lines((runs_source / "runs.ndjson").read_text(encoding="utf-8"))
        for raw in lines:
            if not raw.strip():
                continue
            entry = json.loads(raw)
            source = entry.get("source") if isinstance(entry, dict) else None
            line = entry.get("line") if isinstance(entry, dict) else None
            if not isinstance(source, str) or not source or not isinstance(line, int) or line < 1:
                raise ValueError
            relative = Path(source)
            if relative.is_absolute() or ".." in relative.parts or relative.suffix != ".jsonl":
                raise ValueError
            grouped.setdefault(relative, []).append((line, entry.get("record")))
    except (OSError, UnicodeError, ValueError, TypeError):
        raise InstallError("private checkpoint contains invalid run journal records") from None

    journals: dict[Path, list[tuple[int, str]]] = {}
    for relative, records in grouped.items():
        ordered = sorted(records, key=lambda item: item[0])
        numbers = [number for number, _ in ordered]
        if len(numbers) != len(set(numbers)):
            raise InstallError(
                f"private checkpoint has duplicate run journal lines for {relative.as_posix()}"
            )
        try:
            journals[relative] = [
                (number, ndjson_line(record))
                for number, record in ordered
            ]
        except (TypeError, ValueError):
            raise InstallError(
                f"private checkpoint contains an unserializable run journal record for {relative.as_posix()}"
            ) from None
    return journals


def _render_restored_journal(records: list[tuple[int, str]]) -> str:
    """Keep original physical line numbers; blank source lines are meaningful offsets."""
    rendered: list[str] = []
    previous = 0
    for number, record in records:
        rendered.append("\n" * (number - previous - 1))
        rendered.append(record)
        previous = number
    return "".join(rendered)


def _live_run_journals(state_dir: Path) -> dict[Path, list[str]]:
    """Parse the current journal into the same canonical record spelling as a checkpoint."""
    journals: dict[Path, list[str]] = {}
    try:
        if not state_dir.is_dir():
            return journals
        for path in state_dir.rglob("*.jsonl"):
            if not path.is_file():
                continue
            relative = path.relative_to(state_dir)
            journals[relative] = [
                ndjson_line(json.loads(line))
                for line in ndjson_lines(path.read_text(encoding="utf-8"))
                if line.strip()
            ]
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise InstallError(f"could not read live pipeline state at {state_dir}: {exc}") from None
    return journals


def materialize_pipeline_state(
    instance_dir: Path,
    state_dir: Path,
    *,
    dry_run: bool = False,
) -> PipelineStateMaterialization:
    """Put canonical run journals back where the dispatcher checkpoint reads them.

    Recovery restores ``state/runs`` into the data plane, but the dispatcher exports from its
    pipeline worktree. This bridge is intentionally narrow: it restores only the JSONL content the
    canonical checkpoint carries, and refuses to overwrite a different non-empty live journal.
    """
    try:
        runs_source = Path(instance_dir).expanduser().resolve() / "state" / "runs"
        state_dir = Path(state_dir).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise InstallError(f"could not resolve pipeline state paths: {exc}") from None
    journals = _restored_run_journals(runs_source)
    existing = _live_run_journals(state_dir)
    for relative, canonical in journals.items():
        live = existing.get(relative, [])
        canonical_records = [record for _, record in canonical]
        if live and live[: len(canonical_records)] != canonical_records:
            raise InstallError(
                f"live pipeline state at {state_dir} does not extend the checkpoint; refusing to overwrite it"
            )
    records = sum(len(journal) for journal in journals.values())
    if dry_run:
        # What a real run would change: a missing directory or an absent/empty journal.
        pending = not state_dir.exists() or any(not existing.get(relative) for relative in journals)
        return PipelineStateMaterialization(records=records, changed=pending)
    try:
        created = not state_dir.exists()
        state_dir.mkdir(parents=True, exist_ok=True)
        # A valid live extension is newer than the checkpoint and must survive a
        # retry. Only absent/empty journals receive the reconstructed prefix.
        writes = [
            (state_dir / relative, _render_restored_journal(records))
            for relative, records in journals.items()
            if not existing.get(relative)
        ]
        if writes:
            publish_state_atomic(writes)
    except (OSError, RuntimeError) as exc:
        raise InstallError(f"could not materialize live pipeline state: {exc}") from None
    return PipelineStateMaterialization(records=records, changed=created or bool(writes))


def pipeline_state_path(runtime_home: Path) -> Path:
    """The dispatcher-owned state source below the installation user's worktree."""
    return resolve_pipeline_state_dir(workspaces_root(runtime_home))


def materialize_host(
    instance: Path,
    product_root: Path,
    host_fixture: Path | None = None,
    installation_user: str | None = None,
    before_host: Callable[[UpgradeContext], None] | None = None,
    project_availability: ProjectAvailability | None = None,
):
    report = validate_instance(instance)
    if not report.ok:
        raise InstallError("invalid instance config: " + "; ".join(map(str, report.errors)))
    try:
        installation_user, runtime_home = resolve_runtime_owner(instance, installation_user)
    except ValueError as exc:
        raise InstallError(str(exc)) from None
    context = UpgradeContext(
        instance_path=instance,
        product_root=product_root,
        base_branch="main",
        dry_run=False,
        units=SystemdUnitInstaller(runtime_user=installation_user),
        host_fixture=host_fixture,
        pull=False,
        report=report,
        runtime_user=installation_user,
        runtime_home=runtime_home,
        project_availability=project_availability or ProjectAvailability(),
    )
    # The steps resolve their own paths against `runtime_home`; HOME is exported for the
    # subprocesses they start, which read the environment and not this context.
    with _runtime_environment({"HOME": str(runtime_home)}):
        if before_host is None:
            result = run_steps(context)
        else:
            # Recovery restores the pipeline state itself, inside its ownership barrier.
            steps = tuple(step for step in STEPS if step is not step_pipeline_state)
            host_index = steps.index(step_host)
            prepared = run_steps(context, steps=steps[:host_index])
            if not prepared.ok:
                failed = prepared.steps[-1]
                raise InstallError(f"materializer {failed.name} failed: {failed.detail}")
            before_host(context)
            finished = run_steps(context, steps=steps[host_index:])
            result = UpgradeResult(steps=[*prepared.steps, *finished.steps])
    if not result.ok:
        failed = result.steps[-1]
        raise InstallError(f"materializer {failed.name} failed: {failed.detail}")
    return result


def materialize_head_registry(
    instance: Path, product_root: Path, report: Any, installation_user: str | None
) -> tuple[str, str]:
    """Generate `<data>/heads/` before the board import, with upgrade's own head-registry step.

    The import validates an open sprint's observer head against the installed pair, and on a clean
    host nothing has written one yet: the pair is generated state, never recovery canon, and the
    data directory it lives in (ummanu-26) is recovered empty. So recovery writes it here, from the
    canon the recovered live root carries, as `ummanu upgrade` would; the host materializer later
    runs the same step and finds it current. It is idempotent, so a retry passes through it again.
    """
    context = UpgradeContext(
        instance_path=instance,
        product_root=product_root,
        base_branch="main",
        dry_run=False,
        units=SystemdUnitInstaller(runtime_user=installation_user),
        pull=False,
        report=report,
        runtime_user=installation_user,
    )
    step = step_head_registry(context)
    if step.status == "failed":
        raise InstallError(f"head registry: {step.detail}")
    return step.status, step.detail


def _publish_recovered_memory_export(data_dir: Path, instance: Path, installation_user: str | None) -> None:
    """Publish `<data>/memory/export.ndjson` from the recovered facts and hand it to the user.

    The index rebuild writes only the index, and the pack step skips a ledger the restore already
    matches, so without this `memory verify` finds no export on a recovered host.
    """
    try:
        export_memory_snapshot(data_dir, instance)
    except (OSError, RuntimeError) as exc:
        raise InstallError(f"could not publish the memory export: {exc}") from None
    _set_installation_owner(data_dir / "memory", installation_user)


def _unchanged_memory_step(data_dir: Path, instance: Path, installation_user: str | None) -> tuple[str, str]:
    """A retry past the rebuild: the index stays, and a missing export is still published."""
    if (data_dir / "memory" / "export.ndjson").is_file():
        return "unchanged", "checkpoint index already rebuilt"
    _publish_recovered_memory_export(data_dir, instance, installation_user)
    return "changed", "checkpoint index already rebuilt; published the memory export"


def provision_project_checkouts(
    bindings: list[dict[str, object]],
    installation_user: str | None,
    *,
    instance_dir: Path | None = None,
    bootstrap_credential: Path | None = None,
    timeout: float = 600,
    progress_path: Path | None = None,
    recovery_identity: str = "",
) -> list[ProjectProvisionResult]:
    """Attempt every registered checkout through the credential and atomic-adoption boundary."""
    results: list[ProjectProvisionResult] = []
    persisted: list[dict[str, object]] = []
    for index, binding in enumerate(bindings):
        result = _provision_project_checkout(
            binding,
            index=index,
            installation_user=installation_user,
            instance_dir=instance_dir,
            bootstrap_credential=bootstrap_credential,
            timeout=timeout,
        )
        results.append(result)
        persisted.append(
            {
                "project_id": result.project_id,
                "transport": result.transport,
                "outcome": result.outcome,
                "code": result.code,
                "retryable": result.retryable,
            }
        )
        if progress_path is not None:
            _write_recovery_progress(progress_path, recovery_identity, projects=persisted)
    return results


def _provision_project_checkout(
    binding: dict[str, object],
    *,
    index: int,
    installation_user: str | None,
    instance_dir: Path | None,
    bootstrap_credential: Path | None,
    timeout: float,
) -> ProjectProvisionResult:
    project_id = binding.get("id")
    display_id = (
        project_id
        if isinstance(project_id, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]*", project_id)
        else f"binding-{index + 1}"
    )
    if binding_disabled(binding):
        # A retired or not-yet-onboarded project: nothing runs against it, so nothing is cloned for
        # it, and its absence is not an unavailable checkout.
        return ProjectProvisionResult(
            display_id, "not-inspected", "not-contacted", "disabled", "disabled", "binding is disabled", False
        )
    raw_target = binding.get("repo")
    if not isinstance(raw_target, str) or not raw_target:
        return _project_failure(display_id, "invalid", "unknown", "invalid-binding", False)
    try:
        target = Path(raw_target).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return _project_failure(display_id, "invalid", "unknown", "invalid-target", False)
    target_state = "existing" if target.exists() else "missing"
    if target.exists():
        if target.is_dir() and (target / ".git").exists():
            return ProjectProvisionResult(
                display_id,
                "existing",
                "not-contacted",
                "unchanged",
                "existing",
                "valid checkout left unchanged",
                False,
            )
        return _project_failure(display_id, target_state, "not-contacted", "target-collision", False)
    remote = binding.get("remote")
    branch = binding.get("default_branch")
    if not isinstance(remote, str) or not remote or not isinstance(branch, str) or not branch:
        return _project_failure(display_id, target_state, "unknown", "invalid-binding", False)
    execution = RemoteExecution(
        remote,
        "project-provision",
        instance_dir=instance_dir,
        bootstrap_file=bootstrap_credential,
    )
    transport = execution.transport
    if instance_dir is None:
        return _project_failure(display_id, target_state, transport, "invalid-context", False)
    temporary: Path | None = None
    claimed_target = False
    try:
        created = [parent for parent in (target.parent, *target.parent.parents) if not parent.exists()]
        target.parent.mkdir(parents=True, exist_ok=True)
        if created:
            # Root creates `~/projects` on a fresh host; the installation user clones into it later.
            _set_installation_owner(created[-1], installation_user)
        temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.clone-", dir=target.parent))
        _set_installation_owner(temporary, installation_user)
        staging = temporary / "checkout"
        execution.run_clone(
            staging,
            label=f"clone project {display_id}",
            timeout=timeout,
            clone_args=["--branch", branch, "--single-branch"],
            child_target=instance_dir,
        )
        _set_installation_owner(staging, installation_user)
        # Claim the final name without overwriting a path created since the
        # initial inspection. Replacing our own empty directory is atomic.
        target.mkdir()
        claimed_target = True
        os.replace(staging, target)
        claimed_target = False
    except KeyboardInterrupt:
        raise
    except CredentialError as exc:
        return _project_failure(
            display_id,
            target_state,
            transport,
            exc.code,
            exc.code not in {"unsupported-https", "invalid-branch"},
        )
    except InstallError:
        return _project_failure(display_id, target_state, transport, "ownership", True)
    except OSError:
        return _project_failure(display_id, target_state, transport, "atomic-replace", True)
    finally:
        if claimed_target:
            try:
                target.rmdir()
            except OSError:
                pass
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)
    return ProjectProvisionResult(
        display_id, "adopted", transport, "cloned", "cloned", "checkout cloned atomically", False
    )


def _project_failure(
    project_id: str, target: str, transport: str, code: str, retryable: bool
) -> ProjectProvisionResult:
    reason = {
        "invalid-binding": "project binding is incomplete",
        "invalid-target": "checkout target is invalid",
        "target-collision": "checkout target exists and is not a Git repository",
        "invalid-context": "installation context is unavailable",
        "unsupported-https": "HTTPS host is outside the managed credential boundary",
        "unsupported-transport": "remote transport is unsupported",
        "unsafe-remote": "credential-bearing remote URLs are refused",
        "authentication": "remote authentication failed",
        "network": "remote network access failed",
        "timeout": "Git clone timed out and staging was removed",
        "invalid-branch": "configured default branch is unavailable",
        "command-not-found": "Git is unavailable",
        "process": "Git process could not run",
        "git": "Git clone failed",
        "identity": "Git child identity could not be resolved",
        "ownership": "staged checkout ownership could not be finalized",
        "atomic-replace": "staged checkout could not be adopted",
        "credential": "managed credential is unavailable",
    }.get(code, "project checkout provisioning failed")
    return ProjectProvisionResult(project_id, target, transport, "failed", code, reason, retryable)


def _recovery_identity_entry(digest: Any, *, path: bytes, entry_type: bytes, content: bytes) -> None:
    """Hash one typed recovery input without allowing adjacent components to alias."""
    for component in (path, entry_type, content):
        digest.update(len(component).to_bytes(8, "big"))
        digest.update(component)


def _recovery_identity(
    instance_dir: Path, bindings: list[dict[str, object]], *, checkpoint_root: Path | None = None
) -> str:
    """The identity of one recovery's inputs: board, runs, memory facts and project bindings.

    Board and runs are read below `checkpoint_root` (the extracted tree of an exporter snapshot),
    by default the instance checkout; the facts always come from the live root.
    """
    digest = hashlib.sha256()
    checkpoint_root = instance_dir if checkpoint_root is None else checkpoint_root
    # The board is hashed by its logical files, read through the checkpoint reader, so the same
    # board has the same identity whether a flat or a split checkpoint carries it.
    try:
        board: CheckpointBoard | None = open_checkpoint_board(checkpoint_root / "state" / "board")
        board_error = b""
    except CheckpointLayoutError as exc:
        board, board_error = None, str(exc).encode()
    checkpoint_inputs = [
        *(f"state/board/{name}" for name in CHECKPOINT_BOARD),
        *(f"state/runs/{name}" for name in CHECKPOINT_RUNS),
    ]
    for relative in checkpoint_inputs:
        path = checkpoint_root / relative
        name = relative.removeprefix("state/board/")
        if name in CHECKPOINT_BOARD:
            if board is None:
                entry_type, content = b"invalid", board_error
            elif not board.has(name):
                entry_type, content = b"absent", b""
            else:
                try:
                    entry_type, content = b"file", board.read_bytes(name)
                except (OSError, CheckpointLayoutError) as exc:
                    entry_type, content = b"invalid", str(exc).encode()
        elif path.is_file():
            entry_type, content = b"file", path.read_bytes()
        else:
            entry_type, content = b"absent", b""
        _recovery_identity_entry(
            digest,
            path=relative.encode(),
            entry_type=entry_type,
            content=content,
        )
    facts = instance_dir / "state" / "memory" / "facts"
    _recovery_identity_entry(
        digest,
        path=b"state/memory/facts",
        entry_type=b"dir" if facts.is_dir() else b"absent",
        content=b"",
    )
    try:
        entries = sorted(facts.rglob("*"), key=lambda path: path.relative_to(facts).as_posix())
        for path in entries:
            relative = path.relative_to(facts).as_posix().encode()
            mode = path.lstat().st_mode
            if stat.S_ISREG(mode):
                entry_type, content = b"file", path.read_bytes()
            elif stat.S_ISDIR(mode):
                entry_type, content = b"dir", b""
            elif stat.S_ISLNK(mode):
                entry_type, content = b"symlink", os.fsencode(os.readlink(path))
            else:
                entry_type, content = b"other", b""
            _recovery_identity_entry(
                digest,
                path=relative,
                entry_type=entry_type,
                content=content,
            )
    except (OSError, RuntimeError) as exc:
        raise InstallError("could not identify memory recovery canon") from exc
    safe_bindings = [
        {key: binding.get(key) for key in ("id", "repo", "remote", "default_branch")} for binding in bindings
    ]
    _recovery_identity_entry(
        digest,
        path=b"project-bindings",
        entry_type=b"json",
        content=json.dumps(safe_bindings, sort_keys=True, separators=(",", ":")).encode(),
    )
    return digest.hexdigest()


def _read_recovery_progress(path: Path, identity: str) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": 1, "identity": identity}
    if not isinstance(payload, dict) or payload.get("identity") != identity:
        return {"version": 1, "identity": identity}
    return payload


def _write_recovery_progress(path: Path, identity: str, **changes: object) -> None:
    payload = _read_recovery_progress(path, identity)
    payload.update(changes)
    payload.update(version=1, identity=identity)
    try:
        write_text_atomic(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    except RuntimeError as exc:
        raise InstallError(f"could not record recovery progress: {exc}") from None


def provision_codex_home(
    product_root: Path,
    installation_user: str | None,
    *,
    data_dir: Path | None = None,
) -> int:
    """Seed non-secret Codex runtime files into `<data_dir>/codex-home`, preserving login state.

    That is the one CODEX_HOME the installation manages (`codex_home.managed_codex_homes`); the
    legacy Orca home is not seeded or reconciled since A20 step 7 (secretary-1723). `auth.json` is
    never written: logging in is the PO's own step, `CODEX_HOME=<data_dir>/codex-home codex login`,
    and without it `codex_preflight.resolve_codex_home` refuses to launch a Codex head.

    A `config.toml` seeded here gets the full PO-bridge entry in the same step
    (`memory.client_config.seed_codex_home`, which `reconcile_clients` seeds through too), so the home
    parses under the `-c mcp_servers.po_memory.enabled=false` every pipeline head is launched with.
    That needs the data dir the bridge reads; without one the seed is the packaged file alone. An
    existing config is the upgrade's `memory-clients` step to reconcile.
    """
    if not installation_user:
        return 0
    targets = managed_codex_homes(None if data_dir is None else Path(data_dir))
    if not targets:
        return 0
    source = packaged_codex_home(product_root)
    bridge = (bridge_executable(product_root), Path(data_dir)) if data_dir is not None else None
    changed = 0
    for target in targets:
        changed += _seed_codex_home(target, source, bridge=bridge)
        _set_installation_owner(target, installation_user)
    return changed


def _seed_codex_home(target: Path, source: Path, *, bridge: tuple[Path, Path] | None = None) -> int:
    """Copy-once `AGENTS.md` and `config.toml` into one CODEX_HOME, reconciling the memory entry.

    `bridge` is the PO-bridge executable and data dir a freshly seeded `config.toml` is reconciled
    with (`memory.client_config.seed_codex_home`); the file still counts once.
    """
    changed = 0
    config = target / "config.toml"
    if config.exists() and _reconcile_managed_memory_mcp(config, source / "config.toml"):
        changed += 1
    try:
        changed += len(seed_codex_home(target, source, bridge))
    except ClientConfigError as exc:
        raise InstallError(f"could not provision managed CODEX_HOME: {exc}") from None
    return changed


def _reconcile_managed_memory_mcp(destination: Path, source: Path) -> bool:
    """Add the bearer setting to the one Memory endpoint Ummanu owns.

    CODEX_HOME also holds user login and preferences, so provisioning remains copy-once.  The
    exception is the managed loopback Memory entry: its URL must still exactly match the packaged
    endpoint and only its missing bearer key is inserted.  Any malformed or user-repointed config
    is left untouched.
    """
    try:
        current_text = destination.read_text(encoding="utf-8")
        source_payload = tomllib.loads(source.read_text(encoding="utf-8"))
        current_payload = tomllib.loads(current_text)
        managed = source_payload["mcp_servers"]["memory"]
        current = current_payload["mcp_servers"]["memory"]
        managed_url = managed["url"]
    except (OSError, UnicodeError, KeyError, TypeError, tomllib.TOMLDecodeError):
        return False
    if not isinstance(managed, dict) or not isinstance(current, dict):
        return False
    if not isinstance(managed_url, str) or current.get("url") != managed_url:
        return False
    bearer_name = managed.get("bearer_token_env_var")
    if not isinstance(bearer_name, str) or current.get("bearer_token_env_var"):
        return False
    lines = current_text.splitlines(keepends=True)
    section = "[mcp_servers.memory]"
    start = next((index for index, line in enumerate(lines) if line.strip() == section), None)
    if start is None:
        return False
    end = next(
        (index for index in range(start + 1, len(lines)) if lines[index].lstrip().startswith("[")), len(lines)
    )
    newline = "\r\n" if "\r\n" in current_text else "\n"
    lines.insert(end, f'bearer_token_env_var = "{bearer_name}"{newline}')
    try:
        write_text_atomic(destination, "".join(lines))
    except RuntimeError:
        return False
    return True


def _validated_instance(instance_dir: Path):
    report = validate_instance(instance_dir)
    if not report.ok:
        raise InstallError("invalid cloned instance: " + "; ".join(map(str, report.errors)))
    return report


# Files no product checkout is without. An install materializes from the checkout it selects, and
# the selection can miss: the operator ran the command out of a candidate tree without naming it,
# or `UMMANU_REPO` still points at a checkout that has since moved.
PRODUCT_MARKERS = (Path("packaging") / "codex-home", Path("skills") / "manifest.toml")


def _product_root(args: argparse.Namespace) -> Path:
    """The checkout this install materializes from, refused here if it is not one.

    Named, else configured, else the home default — never the checkout running this module. A path
    that holds no product would otherwise surface as an ENOENT from whichever step read it first.
    """
    if args.product_root:
        root = Path(args.product_root).expanduser().resolve()
        source = "--product-root"
    else:
        root = default_product_root()
        source = f"{PRODUCT_ENV} or the {PRODUCT_DIRNAME} default under this account's home"
    missing = [str(marker) for marker in PRODUCT_MARKERS if not (root / marker).exists()]
    if missing:
        raise InstallError(
            f"not a product checkout: {root} (from {source}) has no {missing[0]}; "
            "name the checkout to install with --product-root"
        )
    return root


def _restore_without_credentials(
    args: argparse.Namespace,
    target: Path,
    result: InstallResult,
    bootstrap_credential: Path | None,
    checkpoint_root: Path | None = None,
    written: tuple[Path, ...] = (),
) -> None:
    """Recover everything that does not go through the board.

    A locked store costs the operator their credentials, not their installation. What is left undone
    is named as skipped rather than quietly attempted with half a configuration, and the caller then
    fails with the secret report.
    """
    report = _validated_instance(target)
    assert report.data_dir is not None
    data_dir = report.data_dir
    checkpoint_root = target if checkpoint_root is None else checkpoint_root
    identity = _recovery_identity(target, report.bindings, checkpoint_root=checkpoint_root)
    progress_path = data_dir / RECOVERY_PROGRESS_FILE
    progress = _read_recovery_progress(progress_path, identity)
    checkpoint_complete = progress.get("checkpoint") == "complete"
    cards, runs = materialize_checkpoint(
        checkpoint_root, data_dir, dry_run=args.dry_run or checkpoint_complete, own=written
    )
    if args.dry_run:
        result.add(
            "checkpoint",
            "would-change",
            f"would materialize {cards} board card(s), {runs} run record(s)",
        )
        return
    _set_installation_owner(data_dir, args.installation_user)
    result.add(
        "checkpoint",
        "unchanged" if checkpoint_complete else "changed",
        f"{cards} board card(s), {runs} run record(s) verified"
        if checkpoint_complete
        else f"{cards} board card(s), {runs} run record(s)",
    )
    _write_recovery_progress(progress_path, identity, checkpoint="complete")
    host = report.host if isinstance(report.host, dict) else {}
    threads = host.get("memory_threads", 1)
    if progress.get("memory") == "complete" and (data_dir / "memory" / "index.sqlite").is_file():
        result.add("memory", *_unchanged_memory_step(data_dir, target, args.installation_user))
    else:
        _write_recovery_progress(progress_path, identity, memory="started")
        count = rebuild_memory_index(
            data_dir, target, threads=threads if isinstance(threads, int) else None, isolated=True
        )
        _publish_recovered_memory_export(data_dir, target, args.installation_user)
        result.add("memory", "changed", f"rebuilt index for {count} fact(s)")
        _write_recovery_progress(progress_path, identity, memory="complete")
    project_results = provision_project_checkouts(
        report.bindings,
        args.installation_user,
        instance_dir=target,
        bootstrap_credential=bootstrap_credential,
        progress_path=progress_path,
        recovery_identity=identity,
    )
    result.projects.extend(project_results)
    seeded = provision_codex_home(_product_root(args), args.installation_user, data_dir=data_dir)
    cloned = sum(project.outcome == "cloned" for project in project_results)
    failed = sum(project.outcome == "failed" for project in project_results)
    result.add(
        "runtime",
        "degraded" if failed else ("changed" if cloned or seeded else "unchanged"),
        f"{cloned} project checkout(s) cloned, {failed} unavailable, {seeded} CODEX_HOME file(s) seeded",
    )
    result.add("board", "skipped", "requires an available board backend after locked secret recovery")
    result.add("host", "skipped", "requires full recovery before host materialization")


def install(args: argparse.Namespace) -> InstallResult:
    result = InstallResult()
    target = Path(args.instance_dir).expanduser().resolve()
    recovery = bool(args.recover)
    disposable_bootstrap: Path | None = None
    bootstrap: Path | None = None
    recovery_data_dir: Path | None = None
    recovery_runtime_paths: list[Path] = []
    snapshot: SnapshotCheckout | None = None
    if args.adopt:
        result.add("mode", "failed", "full live-host adoption is not supported by this flow")
        return result
    try:
        # bootstrap creates this user before the first install. Its stamp is also
        # checked by _clone_or_reuse, so it is the narrow exception to the usual
        # refusal to touch an existing installation user.
        bootstrap_checkout = (target / ".ummanu-bootstrap").is_file()
        _ensure_installation_user(
            args.installation_user,
            recovery=recovery or bootstrap_checkout,
            dry_run=args.dry_run,
        )
        result.add(
            "installation-user",
            "unchanged"
            if recovery or bootstrap_checkout
            else ("would-change" if args.dry_run else "changed"),
            args.installation_user,
        )
        # A retry may need its already-restored managed credential to update the
        # checkpoint checkout. Repair the previous partial run before that first
        # user-owned consumer; the post-secret call below closes the same barrier
        # for files materialized by this invocation.
        if recovery and not args.dry_run and key_path(target).exists():
            ownership_report = _validated_instance(target)
            assert ownership_report.data_dir is not None
            recovery_data_dir = ownership_report.data_dir
            _establish_recovery_ownership_barrier(
                target,
                recovery_data_dir,
                args.installation_user,
            )
        # A supplied bootstrap capability can authenticate a reused recovery
        # checkout too, so consume it only when this non-dry-run invocation
        # will execute one of those remote paths.
        needs_clone = not target.exists() or (target.is_dir() and not any(target.iterdir()))
        if not args.dry_run and (
            needs_clone
            or getattr(args, "bootstrap_credential_file", None)
            or getattr(args, "bootstrap_credential_stdin", False)
        ):
            bootstrap, disposable_bootstrap = _bootstrap_credential(args, target)
        # The one shape decision (docs/RECOVERY.md, "Two remote shapes"). An exporter snapshot takes
        # the snapshot path; a legacy checkpoint and every target that does not read the shape take
        # the clone step below unchanged.
        if _reads_remote_shape(target, recovery=recovery, dry_run=args.dry_run):
            snapshot = _snapshot_checkout(
                args.instance_remote,
                target,
                dry_run=args.dry_run,
                bootstrap_credential=bootstrap,
                installation_user=args.installation_user,
            )
        if snapshot is not None:
            detail = snapshot.detail
            result.add(
                "instance-checkout",
                "would-change" if args.dry_run else ("changed" if snapshot.changed else "unchanged"),
                detail,
            )
            recovery_runtime_paths.append(snapshot.repository)
        else:
            detail = _clone_or_reuse(
                args.instance_remote,
                target,
                recovery=recovery,
                dry_run=args.dry_run,
                bootstrap_credential=bootstrap,
                installation_user=args.installation_user,
            )
            result.add(
                "instance-checkout",
                "unchanged" if detail.startswith("reused") else ("would-change" if args.dry_run else "changed"),
                detail,
            )
        # Board and runs: the checkout of a legacy checkpoint, or the snapshot's extracted tree.
        checkpoint_root = snapshot.checkpoint_root if snapshot is not None else target
        if args.dry_run and not target.exists():
            result.add("secret-store", "skipped", "available only after clone")
            result.add("runtime-env", "skipped", "available only after clone")
            return result
        # The store opens before anything reads runtime.env, because on a clean
        # host that file is the store's output and does not exist yet.
        runtime_env = _runtime_env_file(target, args.runtime_env)
        # Before the store writes: which of its file targets in the data directory are new.
        created = _checked_file_targets(target)
        secrets = _open_secret_store(
            target,
            runtime_env,
            phrase=_recovery_phrase(args, target),
            dry_run=args.dry_run,
        )
        result.add("secret-store", *_secret_store_step(secrets))
        _add_secret_steps(result, secrets)
        # This run's own files in the data directory: the file targets it created there.
        written = tuple(item.path for item in secrets.materialized if _named_path(item.path) in created)

        # Secret recovery can create a root-owned 0600 installation key. Cross
        # ownership once, before any runtime-user Git or remote consumer starts.
        if recovery and not args.dry_run:
            ownership_report = _validated_instance(target)
            assert ownership_report.data_dir is not None
            recovery_data_dir = ownership_report.data_dir
            _establish_recovery_ownership_barrier(
                target,
                recovery_data_dir,
                args.installation_user,
            )
            result.add(
                "recovery-ownership",
                "unchanged",
                "restored paths handed to the installation user",
            )

        runtime_loaded = True
        try:
            values = read_runtime_env(target, args.runtime_env)
        except RuntimeEnvMissing as exc:
            # A legacy board-only catalog is inert after this migration. It
            # must not force a recovery phrase merely to recreate transport.
            runtime_loaded = False
            runtime_required = any(
                entry.get("target") == "runtime-env" for entry in (*secrets.locked, *secrets.missing)
            )
            if not runtime_required:
                values = {}
            else:
                _restore_without_credentials(args, target, result, bootstrap, checkpoint_root, written)
                raise _blocked_by_secrets(exc, secrets, runtime_env) from None
        except RuntimeEnvError as exc:
            raise InstallError(str(exc)) from None
        try:
            canonical_runtime_env = runtime_env.resolve() == target.resolve() / "runtime.env"
        except OSError:
            canonical_runtime_env = False
        if bootstrap_checkout and runtime_loaded:
            # A bootstrap checkout may hold a runtime.env that carries none of the store's
            # variables, so on it the file being there does not say they arrived.
            unavailable = sorted(
                {
                    str(entry["environment"])
                    for entry in (*secrets.locked, *secrets.missing)
                    if entry.get("target") == "runtime-env" and entry.get("environment")
                }
                - set(values)
            )
            if unavailable:
                _restore_without_credentials(args, target, result, bootstrap, checkpoint_root, written)
                raise _blocked_by_secrets(
                    InstallError(f"runtime.env lacks {', '.join(unavailable)}"), secrets, runtime_env
                ) from None
        if not args.dry_run:
            if canonical_runtime_env:
                _set_installation_owner(runtime_env, args.installation_user)
            _set_installation_owner(target / ".gitignore", args.installation_user)
            _set_installation_owner(target / ".git", args.installation_user)
        result.add(
            "runtime-env",
            "unchanged" if runtime_loaded else "skipped",
            "host-only runtime configuration loaded"
            if runtime_loaded
            else "not required by this installation",
        )
        with _runtime_environment({**values, "UMMANU_INSTANCE": str(target)}):
            check_prerequisites(target)
            result.add("prerequisites", "unchanged", "PostgreSQL is reachable")
            report = _validated_instance(target)
            assert report.data_dir is not None
            data_dir = report.data_dir
            recovery_data_dir = data_dir
            identity = _recovery_identity(target, report.bindings, checkpoint_root=checkpoint_root)
            progress_path = data_dir / RECOVERY_PROGRESS_FILE
            progress = _read_recovery_progress(progress_path, identity)
            checkpoint_complete = progress.get("checkpoint") == "complete"
            cards, runs = materialize_checkpoint(
                checkpoint_root, data_dir, dry_run=args.dry_run or checkpoint_complete, own=written
            )
            if args.dry_run:
                result.add(
                    "checkpoint",
                    "would-change",
                    f"would materialize {cards} board card(s), {runs} run record(s)",
                )
                result.add("board", "would-change", f"would restore {cards} card(s) and verify parity")
                result.add("memory", "would-change", "would rebuild the index from checkpoint facts")
                result.add("host", "would-change", "would run the host materializer")
                result.add("status", "skipped", "preview made no recovery changes")
                return result
            _set_installation_owner(data_dir, args.installation_user)
            result.add(
                "checkpoint",
                "unchanged" if checkpoint_complete else "changed",
                f"{cards} board card(s), {runs} run record(s) verified"
                if checkpoint_complete
                else f"{cards} board card(s), {runs} run record(s)",
            )
            _write_recovery_progress(progress_path, identity, checkpoint="complete")
            product_root = _product_root(args)
            # Before the board: the import checks open sprints' observer heads against this pair.
            result.add(
                "head-registry",
                *materialize_head_registry(target, product_root, report, args.installation_user),
            )
            recovered_board_completion = (
                progress.get("board") == "started"
                and restore_state(data_dir).get("board_parity") == "complete"
            )
            if progress.get("board") == "complete" or recovered_board_completion:
                result.add("board", "unchanged", f"{cards} card(s) already restored at parity")
                if recovered_board_completion:
                    _write_recovery_progress(progress_path, identity, board="complete")
            else:
                _write_recovery_progress(progress_path, identity, board="started")
                restored = import_normalized_board(data_dir, instance=target)
                result.add("board", "changed", f"{restored} card(s) at parity")
                _write_recovery_progress(progress_path, identity, board="complete")
            host = report.host if isinstance(report.host, dict) else {}
            threads = host.get("memory_threads", 1)
            recovered_memory_completion = (
                progress.get("memory") == "started"
                and restore_state(data_dir).get("memory_index") == "complete"
                and (data_dir / "memory" / "index.sqlite").is_file()
            )
            if (
                progress.get("memory") == "complete" and (data_dir / "memory" / "index.sqlite").is_file()
            ) or recovered_memory_completion:
                result.add("memory", *_unchanged_memory_step(data_dir, target, args.installation_user))
                if recovered_memory_completion:
                    _write_recovery_progress(progress_path, identity, memory="complete")
            else:
                _write_recovery_progress(progress_path, identity, memory="started")
                # In a child process: the host step starts `ummanu-memory-mcp`, and this process must
                # not still hold its own copy of the embedding model beside it (ummanu-53 P14).
                count = rebuild_memory_index(
                    data_dir, target, threads=threads if isinstance(threads, int) else None, isolated=True
                )
                _publish_recovered_memory_export(data_dir, target, args.installation_user)
                result.add("memory", "changed", f"rebuilt index for {count} fact(s)")
                _write_recovery_progress(progress_path, identity, memory="complete")
            project_results = provision_project_checkouts(
                report.bindings,
                args.installation_user,
                instance_dir=target,
                bootstrap_credential=bootstrap,
                progress_path=progress_path,
                recovery_identity=identity,
            )
            result.projects.extend(project_results)
            seeded = provision_codex_home(product_root, args.installation_user, data_dir=data_dir)
            cloned = sum(project.outcome == "cloned" for project in project_results)
            failed = sum(project.outcome == "failed" for project in project_results)
            project_availability = ProjectAvailability(
                frozenset(project.project_id for project in project_results if project.outcome == "failed")
            )
            result.add(
                "runtime",
                "degraded" if failed else ("changed" if cloned or seeded else "unchanged"),
                f"{cloned} project checkout(s) cloned, {failed} unavailable, {seeded} CODEX_HOME file(s) seeded",
            )
            restored_runs = 0
            pipeline_state_changed = False

            def restore_pipeline_source(context: UpgradeContext) -> None:
                nonlocal restored_runs, pipeline_state_changed
                state_path = pipeline_state_path(context.runtime_home or Path.home())
                recovery_runtime_paths.append(state_path.parent)
                restored = materialize_pipeline_state(
                    checkpoint_root,
                    state_path,
                    dry_run=False,
                )
                restored_runs = restored.records
                pipeline_state_changed = restored.changed
                _establish_recovery_ownership_barrier(
                    target,
                    data_dir,
                    args.installation_user,
                    additional_paths=(state_path.parent,),
                )

            host_result = materialize_host(
                target,
                product_root,
                Path(args.host_fixture).expanduser().resolve() if args.host_fixture else None,
                args.installation_user,
                before_host=restore_pipeline_source,
                project_availability=project_availability,
            )
            mark_reconcile_applied(data_dir)
            changed = sum(step.status == "changed" for step in host_result.steps)
            result.add(
                "host",
                "changed" if changed else "unchanged",
                f"materializer complete ({changed} changed step(s))",
            )
            result.add(
                "pipeline-state",
                "changed" if pipeline_state_changed else "unchanged",
                f"materialized {restored_runs} run record(s)",
            )
            findings = restore_findings(data_dir)
            if findings:
                raise InstallError("status findings: " + "; ".join(findings))
            if not recovery:
                (target / ".ummanu-bootstrap").unlink(missing_ok=True)
            result.add(
                "status",
                "degraded" if failed else "unchanged",
                f"core ready; {failed} project checkout(s) unavailable; rerun ummanu recover with the same inputs"
                if failed
                else "board, memory and operational configuration are ready"
                if secrets.complete
                else f"board and memory are ready, but {secrets.summary()}",
            )
    except (InstallError, RestoreError, RuntimeError) as exc:
        result.add("install", "failed", str(exc))
    finally:
        if recovery and not args.dry_run:
            try:
                _establish_recovery_ownership_barrier(
                    target,
                    recovery_data_dir,
                    args.installation_user,
                    additional_paths=tuple(recovery_runtime_paths),
                )
            except InstallError as exc:
                if any(step.status == "failed" for step in result.steps):
                    result.add(
                        "recovery-ownership-cleanup",
                        "failed",
                        f"ownership cleanup also failed: {exc}; original failure is retained above",
                    )
                else:
                    result.add("install", "failed", str(exc))
        if disposable_bootstrap is not None:
            disposable_bootstrap.unlink(missing_ok=True)
        if snapshot is not None:
            shutil.rmtree(snapshot.scratch, ignore_errors=True)
    return result


def run_install(args: argparse.Namespace) -> int:
    if args.bootstrap_credential_stdin and args.recovery_phrase_stdin:
        print(
            json.dumps(
                {
                    "error": {
                        "code": "usage",
                        "message": "--bootstrap-credential-stdin and --recovery-phrase-stdin cannot share standard input; use a mode-0600 file for one input",
                    }
                }
            ),
            file=sys.stderr,
        )
        return 2
    result = install(args)
    if args.json:
        print(
            json.dumps(
                {
                    "status": result.status,
                    "steps": [step.__dict__ for step in result.steps],
                    "projects": [project.__dict__ for project in result.projects],
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(result.render())
    return 0 if result.ok else 1


def add_bootstrap_credential_arguments(parser) -> None:
    """The one declaration of the external clone credential, shared by `install`, `recover` and `bootstrap`."""
    bootstrap = parser.add_mutually_exclusive_group()
    bootstrap.add_argument(
        "--bootstrap-credential-file",
        help="mode-0600 external GitHub credential used only to clone a private instance remote",
    )
    bootstrap.add_argument(
        "--bootstrap-credential-stdin",
        action="store_true",
        help="read a one-line external bootstrap credential from standard input before clone",
    )


def add_install_commands(subparsers) -> None:
    def arguments(parser, *, recovery_default: bool) -> None:
        parser.add_argument("--instance-remote", required=True, help="private Git checkpoint remote")
        parser.add_argument("--instance-dir", required=True, help="local checkout destination")
        parser.add_argument("--installation-user", required=True, help="dedicated OS account")
        parser.add_argument(
            "--runtime-env",
            help="host-only credentials file (default: INSTANCE/runtime.env)",
        )
        phrase = parser.add_mutually_exclusive_group()
        phrase.add_argument(
            "--recovery-phrase-file",
            help="read the recovery phrase from this file; never pass it on the command line",
        )
        add_bootstrap_credential_arguments(parser)
        phrase.add_argument(
            "--recovery-phrase-stdin",
            action="store_true",
            help="read the recovery phrase from standard input",
        )
        parser.add_argument("--product-root", help="installed product checkout")
        parser.add_argument(
            "--recover",
            action="store_true",
            default=recovery_default,
            help="resume or recover the same installation without overwriting local changes",
        )
        parser.add_argument(
            "--adopt",
            action="store_true",
            help="select the separate live-host adoption path",
        )
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--json", action="store_true")
        parser.add_argument("--host-fixture", help=argparse.SUPPRESS)
        parser.set_defaults(handler=run_install)

    fresh = subparsers.add_parser("install", help="install or resume from a private instance remote")
    arguments(fresh, recovery_default=False)
    recover = subparsers.add_parser(
        "recover",
        help="recover a lost installation from a private instance remote",
    )
    arguments(recover, recovery_default=True)
