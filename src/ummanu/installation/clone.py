"""Legacy checkpoint clones and exporter snapshot checkout/materialization."""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ummanu import state_repo
from ummanu.checkpoint import SNAPSHOT_BASE_REF, SNAPSHOT_BRANCH, SNAPSHOT_REF
from ummanu.config import DataDirError, instance_offsite_remote, recovered_instance_locations
from ummanu.data import init_layout
from ummanu.infra import snapshot_tree
from ummanu.infra.export_allowlist import is_exported
from ummanu.infra.github_credential import CredentialError, RemoteExecution
from ummanu.installation.models import InstallError
from ummanu.installation.support import (
    _checked_data_target,
    _data_target_entries,
    _run,
    _set_installation_owner,
)
from ummanu.state_repo import StateRepoError


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
            detail = state_repo.git_failure_detail(fetch, fallback=f"exited {fetch.returncode}")
            raise InstallError(f"fetch instance remote: {detail}")
        merge = remote_git.run_instance(
            target, ["merge", "--ff-only", "@{u}"], label="fast-forward instance checkout"
        )
        if merge.returncode:
            detail = state_repo.git_failure_detail(merge, fallback=f"exited {merge.returncode}")
            raise InstallError(f"fast-forward instance checkout: {detail}")
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
        completed = state_repo.run_git(
            repository, ["--git-dir", str(repository), *args], label=label, input=input
        )
    except StateRepoError as exc:
        raise InstallError(str(exc)) from None
    if completed.returncode:
        detail = state_repo.git_failure_detail(completed, fallback=f"exited {completed.returncode}")
        raise InstallError(f"{label}: {detail}")
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
        raise InstallError(
            f"snapshot repository {repository} changed during recovery; no files were overwritten"
        )
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
    remote_git = RemoteExecution(
        remote, "recovery-reuse", instance_dir=target, bootstrap_file=bootstrap_credential
    )
    fetched = remote_git.run_instance(
        repository,
        ["--git-dir", str(repository), "fetch", "--quiet", "--no-tags", remote, SNAPSHOT_REF],
        label="fetch snapshot remote",
        timeout=300,
    )
    if fetched.returncode:
        detail = state_repo.git_failure_detail(fetched, fallback=f"exited {fetched.returncode}")
        raise InstallError(f"fetch snapshot remote: {detail}")
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
        raise InstallError(f"snapshot ancestry: {state_repo.git_failure_detail(ancestry, stdout=False)}")
    _snapshot_git(
        repository,
        ["update-ref", "-m", "recover: fast-forward to the remote tip", SNAPSHOT_REF, tip, existing],
        "fast-forward snapshot repository",
    )



def _point_snapshot_remote(repository: Path, remote: str) -> None:
    """Set `origin` to `offsite.instance_remote`, the remote the snapshot pusher publishes to."""
    if remote:
        _snapshot_git(
            repository, ["config", "--replace-all", "remote.origin.url", remote], "set snapshot remote"
        )



def _mark_recovered_tip(repository: Path, tip: str) -> None:
    """Make the recovered tip the takeover base: doctor's foreign-commit walk starts after it."""
    try:
        marker = _snapshot_git(repository, ["cat-file", "blob", SNAPSHOT_BASE_REF], "read takeover marker")
    except InstallError:
        marker = ""
    if marker == tip:
        return
    blob = _snapshot_git(
        repository, ["hash-object", "-w", "--stdin"], "write takeover marker", input=f"{tip}\n"
    )
    _snapshot_git(
        repository,
        ["update-ref", "-m", "recover: takeover base", SNAPSHOT_BASE_REF, blob],
        "write takeover marker",
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
