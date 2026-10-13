"""Installation subprocess, runtime ownership and target confinement boundaries."""

from __future__ import annotations

import json
import os
import pwd
import stat
import subprocess
from pathlib import Path

from ummanu import _proc, state_repo
from ummanu.data import manifest_for
from ummanu.installation.models import InstallError
from ummanu.secret_store import key_path
from ummanu.upgrade import GitError, _set_runtime_owner


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
        detail = state_repo.git_failure_detail(completed, fallback=f"exited {completed.returncode}")
        raise InstallError(f"{label}: {detail}")
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
