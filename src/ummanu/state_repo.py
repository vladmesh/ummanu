"""The live root's writer lock and the private instance repository as a commit target.

Contract: docs/RECOVERY.md, sections "Layout" and "Writers". Only one writer still commits to the
instance repository: the legacy tick (`state/board`, `state/runs`, and in legacy mode the live-root
paths the Git-free writers leave uncommitted, `checkpoint.LEGACY_LIVE_PATHS`). It owns its
pathspecs and never `git add -A`. The memory writer, the knowledge writer and the secret store make
no Git call: they write files under `state_repo_lock`, the live-root writer lock, which is all they
take from this module. What leaves the host is decided
by the snapshot export allowlist (`infra.export_allowlist`), not by `.gitignore`, which no writer
here maintains any more.
"""

from __future__ import annotations

import os
import pwd
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ummanu import _proc
from ummanu._fsutil import file_lock

STATE_LOCK_NAME = "ummanu-state-writer.lock"

FALLBACK_IDENTITY = ("ummanu checkpoint", "ummanu-checkpoint@localhost")

# Pathspec each committing writer owns. Disjoint by construction; see the module docstring.
BOARD_RUNS_PATHSPEC = ("state/board", "state/runs")
MEMORY_PATHSPEC = ("state/memory",)

# These are deliberately repository-local controls for the private instance
# checkout. They reduce Git's peak packing appetite; they are not a process RSS
# limit and must never escape into a user's global configuration or a project.
# The last two switch off the implicit `gc --auto` every `git commit` would
# otherwise start inside a checkpoint tick; `ummanu instance-maintenance`,
# fired by its own timer, packs the repository instead.
PACKING_CONTROLS = (
    ("pack.threads", "1"),
    ("pack.windowMemory", "128m"),
    ("pack.deltaCacheSize", "64m"),
    ("gc.auto", "0"),
    ("maintenance.auto", "false"),
)

# Variables with which the caller's environment selects a *different* repository than
# the one named on the command line.  Git honours them ahead of `-C`, so an inherited
# `GIT_DIR` silently redirects an instance write into whatever repository the caller
# happened to be working in.  Every Git child this product starts drops them first.
GIT_SELECTION_VARIABLES = (
    "GIT_DIR",
    "GIT_INDEX_FILE",
    "GIT_WORK_TREE",
    "GIT_OBJECT_DIRECTORY",
)

MEMORY_FACTS_RELATIVE = Path("state") / "memory" / "facts"
KNOWLEDGE_RELATIVE = Path("state") / "knowledge"
SECRETS_RELATIVE = Path("secrets")


class StateRepoError(RuntimeError):
    """A git command against the instance repo did not run or did not succeed."""


@dataclass(frozen=True)
class GitChildIdentity:
    """The identity that will execute an instance-repository Git child."""

    uid: int
    gid: int
    name: str | None = None


def git_child_identity(instance_dir: Path) -> GitChildIdentity:
    """Resolve the actual Git child before handing it an operation capability.

    Root lifecycle commands cross to the checkout owner.  Everybody else runs
    Git as their current effective identity.  Keep this decision shared with
    :func:`run_git`: a credential handoff must not guess from a caller's uid.
    """
    instance_dir = Path(instance_dir).expanduser().resolve()
    if os.getuid() == 0:
        try:
            owner = instance_dir.stat()
            if owner.st_uid != 0:
                account = pwd.getpwuid(owner.st_uid)
                return GitChildIdentity(owner.st_uid, owner.st_gid, account.pw_name)
        except (KeyError, OSError) as exc:
            raise StateRepoError(f"could not select instance runtime user: {exc}") from None
    return GitChildIdentity(os.geteuid(), os.getegid())


def memory_facts_dir(instance_dir: Path) -> Path:
    return Path(instance_dir).expanduser().resolve() / MEMORY_FACTS_RELATIVE


def knowledge_dir(instance_dir: Path) -> Path:
    return Path(instance_dir).expanduser().resolve() / KNOWLEDGE_RELATIVE


def secrets_dir(instance_dir: Path) -> Path:
    return Path(instance_dir).expanduser().resolve() / SECRETS_RELATIVE


@contextmanager
def state_repo_lock(instance_dir: Path) -> Iterator[None]:
    """Hold the index of the instance repo for one writer at a time.

    The lock lives in the git dir rather than the worktree so it never shows up
    as an untracked file in the operator's `git status`.
    """
    instance_dir = Path(instance_dir).expanduser().resolve()
    lock_path = _lock_path(instance_dir)
    with file_lock(lock_path):
        _make_repo_user_owned(lock_path, instance_dir)
        yield


def _lock_path(instance_dir: Path) -> Path:
    git_dir = instance_dir / ".git"
    if git_dir.is_dir():
        return git_dir / STATE_LOCK_NAME
    # A worktree or a not-yet-initialized repo: keep the lock beside the tree.
    return instance_dir / f".{STATE_LOCK_NAME}"


def commit_identity(instance_dir: Path) -> list[str]:
    """Fall back to a writer identity only when the repo declares none."""
    for key in ("user.name", "user.email"):
        try:
            configured = git(instance_dir, ["config", "--get", key], label="inspect commit identity")
        except StateRepoError:
            configured = ""
        if not configured.strip():
            name, email = FALLBACK_IDENTITY
            return ["-c", f"user.name={name}", "-c", f"user.email={email}"]
    return []


def git_command(instance_dir: Path, args: list[str]) -> list[str]:
    """The only Git invocation shape allowed for an instance repository.

    An instance checkout is runtime-user-owned even when install, recovery or
    an operator's repair is root-initiated.  Git reads repository configuration
    before it executes its subcommand, so crossing to that owner must happen
    before every operation, including a harmless-looking reachability probe.
    """
    instance_dir = Path(instance_dir).expanduser().resolve()
    return [
        "git",
        "-c",
        f"safe.directory={instance_dir}",
        "-c",
        "core.hooksPath=/dev/null",
        "-C",
        str(instance_dir),
        *args,
    ]


def git_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """The environment every Git child of this product starts with.

    Two policies, one place.  Noninteractive: no credential prompt and no
    interactive SSH, so an operation is bounded rather than hanging on a
    terminal nobody is watching.  Repository-selecting: `GIT_DIR` and its
    siblings are removed, because Git reads them ahead of the `-C`/path the
    caller asked for, and an inherited one would silently point a state or
    journal write at the caller's own repository.

    This is also the pre-checkout helper: a clone has no instance repository to
    cross into yet, but its child still must not inherit that selection.
    """
    env = dict(os.environ if base is None else base)
    for name in GIT_SELECTION_VARIABLES:
        env.pop(name, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    return env


def run_git(
    instance_dir: Path,
    args: list[str],
    *,
    label: str,
    timeout: float = 120,
    extra_env: dict[str, str] | None = None,
    input: str | None = None,
    child: GitChildIdentity | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one instance-repository Git command through its privilege boundary.

    Unlike :func:`git`, this returns non-zero results for callers such as the
    checkpoint pusher that need to distinguish an expected false predicate
    from a command failure.  It still owns command construction, identity
    crossing, hook suppression and noninteractive execution for all callers.
    """
    instance_dir = Path(instance_dir).expanduser().resolve()
    env = git_env()
    if extra_env:
        env.update(extra_env)
    command = _crossed_git_command(instance_dir, args, env, extra_env=extra_env, child=child, label=label)
    try:
        # Its own process group: a timeout must take Git's remote helper (`git-remote-https`) down
        # with Git, since the production tick's unit no longer kills what it leaves behind.
        return _proc.run_isolated(command, input=input, timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StateRepoError(f"{label} failed: {exc}") from None


def run_git_bytes(
    instance_dir: Path,
    args: list[str],
    *,
    label: str,
    timeout: float = 120,
    input: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """:func:`run_git` for a local read whose output is object bytes, not text.

    The text runner decodes and normalises line ends, which would change the bytes a digest is
    taken over. Same command shape, identity crossing and environment; meant for local plumbing
    such as `cat-file --batch`, never for a remote operation.
    """
    instance_dir = Path(instance_dir).expanduser().resolve()
    env = git_env()
    command = _crossed_git_command(instance_dir, args, env, extra_env=None, child=None, label=label)
    try:
        return subprocess.run(
            command, input=input, capture_output=True, timeout=timeout, env=env, start_new_session=True, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StateRepoError(f"{label} failed: {exc}") from None


def _crossed_git_command(
    instance_dir: Path,
    args: list[str],
    env: dict[str, str],
    *,
    extra_env: dict[str, str] | None,
    child: GitChildIdentity | None,
    label: str,
) -> list[str]:
    """The Git command line for `instance_dir`, crossed to its owner when this process is root."""
    command = git_command(instance_dir, args)
    # The instance checkout is runtime-user-owned.  Root install/upgrade may need to reconcile
    # it, but Git reads repository configuration before a command (including fsmonitor), so a
    # root Git process would execute runtime-user-controlled configuration.  Cross that boundary
    # once, before Git starts, rather than trying to suppress every executable Git feature.
    # `getuid` deliberately tests the process's real privilege.  Some lifecycle tests (and
    # wrappers) only override effective identity for their own preflight, which must not make an
    # unprivileged process attempt `runuser`.
    if os.getuid() == 0:
        try:
            child = child or git_child_identity(instance_dir)
            if child.uid != 0:
                # `runuser` rebuilds the calling environment, and what it keeps
                # depends on its PAM configuration. Restate the whole policy as
                # command arguments, so neither a credential prompt nor an
                # inherited repository selection can come back after the owner
                # crossing.
                command = [
                    "runuser",
                    "--user",
                    child.name or pwd.getpwuid(child.uid).pw_name,
                    "--",
                    "env",
                    *[argument for name in GIT_SELECTION_VARIABLES for argument in ("--unset", name)],
                    f"GIT_TERMINAL_PROMPT={env['GIT_TERMINAL_PROMPT']}",
                    f"GIT_SSH_COMMAND={env['GIT_SSH_COMMAND']}",
                    # PAM decides which parent environment entries survive
                    # runuser.  `extra_env` is the explicit, controlled seam
                    # for non-secret per-command context such as the managed
                    # credential helper's instance location, so restate it
                    # after the privilege crossing as well.
                    *[f"{name}={value}" for name, value in sorted((extra_env or {}).items())],
                    *command,
                ]
        except (KeyError, OSError) as exc:
            raise StateRepoError(f"{label} failed: could not select instance runtime user: {exc}") from None
    return command


def run_as_git_child(
    instance_dir: Path,
    argv: list[str],
    *,
    label: str,
    timeout: float = 120,
    extra_env: dict[str, str] | None = None,
    child: GitChildIdentity | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a non-Git consumer under the identity selected for instance Git.

    A managed credential readiness probe must not inspect a runtime-user-owned
    installation key as root and then speak for the eventual Git child.  This
    narrow runner shares the same identity crossing and controlled environment
    as :func:`run_git`, without accepting repository-controlled Git config.
    """
    instance_dir = Path(instance_dir).expanduser().resolve()
    resolved = child or git_child_identity(instance_dir)
    env = git_env()
    if extra_env:
        env.update(extra_env)
    command = list(argv)
    if os.getuid() == 0 and resolved.uid != 0:
        command = [
            "runuser",
            "--user",
            resolved.name or pwd.getpwuid(resolved.uid).pw_name,
            "--",
            "env",
            *[argument for name in GIT_SELECTION_VARIABLES for argument in ("--unset", name)],
            f"GIT_TERMINAL_PROMPT={env['GIT_TERMINAL_PROMPT']}",
            f"GIT_SSH_COMMAND={env['GIT_SSH_COMMAND']}",
            *[f"{name}={value}" for name, value in sorted((extra_env or {}).items())],
            *command,
        ]
    try:
        return _proc.run_isolated(command, timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StateRepoError(f"{label} failed: {exc}") from None


def git_failure_detail(
    result: subprocess.CompletedProcess[Any], *, fallback: str = "git error", stdout: bool = True
) -> str:
    """The last line Git printed on stderr (else stdout), which every Git failure message quotes."""
    output = result.stderr or (result.stdout if stdout else None) or ""
    if isinstance(output, bytes):
        output = output.decode("utf-8", "replace")
    detail = output.strip().splitlines()
    return detail[-1] if detail else fallback


def run_git_checked(
    instance_dir: Path,
    args: list[str],
    *,
    label: str,
    error: type[Exception] = StateRepoError,
    timeout: float = 120,
    input: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """:func:`run_git` for a command that must succeed, failing as the caller's own `error` class.

    A non-zero exit raises ``error("<label> failed: <Git's last line>")``; a runner failure (Git
    missing, a timeout) raises ``error`` with the runner's own text.
    """
    try:
        result = run_git(instance_dir, args, label=label, timeout=timeout, extra_env=extra_env, input=input)
    except StateRepoError as exc:
        if error is StateRepoError:
            raise
        raise error(str(exc)) from None
    if result.returncode != 0:
        raise error(f"{label} failed: {git_failure_detail(result)}")
    return result


def git(
    instance_dir: Path,
    args: list[str],
    *,
    label: str,
    timeout: float = 120,
    error: type[Exception] = StateRepoError,
    input: str | None = None,
) -> str:
    """Run a required instance-repository Git command and return its stdout."""
    return run_git_checked(instance_dir, args, label=label, error=error, timeout=timeout, input=input).stdout


def read_blobs(
    instance_dir: Path,
    oids: list[str],
    *,
    git_args: list[str],
    error: type[Exception] = StateRepoError,
    label: str = "snapshot blobs",
) -> dict[str, bytes]:
    """The bytes of each blob, read in one `cat-file --batch` (the text runner would alter them).

    `git_args` precede `cat-file` (``--git-dir <repo>`` for a bare repository). Output that ends
    before a header or a body it announced (a truncated or interrupted read) is refused as
    ``error``, never an index error or a short blob.
    """
    try:
        result = run_git_bytes(
            instance_dir,
            [*git_args, "cat-file", "--batch"],
            label=label,
            input="".join(f"{oid}\n" for oid in oids).encode(),
        )
    except StateRepoError as exc:
        if error is StateRepoError:
            raise
        raise error(str(exc)) from None
    if result.returncode != 0:
        raise error(f"{label} failed: {git_failure_detail(result, stdout=False)}")
    payload, offset = result.stdout, 0
    blobs: dict[str, bytes] = {}
    for oid in oids:
        end = payload.find(b"\n", offset)
        header = payload[offset:end].decode("ascii", "replace").split() if end >= 0 else []
        if len(header) != 3 or header[1] != "blob" or not header[2].isdigit():
            raise error(f"{label} failed: {oid} is not a blob")
        size = int(header[2])
        if end + 1 + size > len(payload):
            raise error(f"{label} failed: {oid} is truncated")
        blobs[oid] = payload[end + 1 : end + 1 + size]
        offset = end + 1 + size + 1
    return blobs


def _make_repo_user_owned(path: Path, instance_dir: Path) -> None:
    """Hand root-created lifecycle files back before the runtime user invokes Git."""
    if os.getuid() != 0:
        return
    try:
        repo_owner = instance_dir.stat()
        if repo_owner.st_uid != 0:
            os.chown(path, repo_owner.st_uid, repo_owner.st_gid)
    except OSError as exc:
        raise StateRepoError(f"prepare instance lifecycle file failed: {exc}") from None


def require_repo(instance_dir: Path) -> Path:
    instance_dir = Path(instance_dir).expanduser().resolve()
    if not (instance_dir / ".git").exists():
        raise StateRepoError(f"instance repo is not a git repository: {instance_dir}")
    return instance_dir


def packing_controls(instance_dir: Path) -> dict[str, str | None]:
    """Read only the instance checkout's local packing controls.

    ``--local`` is part of the command rather than an assumption about Git's
    default scope. This makes the boundary mechanically visible in lifecycle
    and doctor paths and leaves global and registered-project configuration out.
    """
    instance = require_repo(instance_dir)
    values: dict[str, str | None] = {}
    for key, _ in PACKING_CONTROLS:
        result = run_git(instance, ["config", "--local", "--get-all", key], label=f"inspect {key}")
        if result.returncode not in {0, 1}:
            raise StateRepoError(f"inspect {key} failed: {git_failure_detail(result)}")
        values[key] = result.stdout.strip() if result.returncode == 0 else None
    return values


def configure_packing_controls(instance_dir: Path, *, dry_run: bool = False) -> tuple[str, ...]:
    """Idempotently set the three supported local instance packing controls."""
    instance = require_repo(instance_dir)
    current = packing_controls(instance)
    drifted = tuple(key for key, expected in PACKING_CONTROLS if current.get(key) != expected)
    if not drifted or dry_run:
        return drifted
    with state_repo_lock(instance):
        # Re-read while holding the repository writer lock. A concurrent local
        # lifecycle run can then converge without one writer undoing another.
        current = packing_controls(instance)
        drifted = tuple(key for key, expected in PACKING_CONTROLS if current.get(key) != expected)
        for key, expected in PACKING_CONTROLS:
            if key in drifted:
                git(instance, ["config", "--local", "--replace-all", key, expected], label=f"set {key}")
    return drifted


def head(instance_dir: Path) -> str | None:
    try:
        return git(instance_dir, ["rev-parse", "--verify", "HEAD"], label="inspect head").strip()
    except StateRepoError:
        return None


def status(instance_dir: Path, pathspec: tuple[str, ...]) -> str:
    return git(
        instance_dir,
        ["status", "--porcelain", "--untracked-files=all", "--", *pathspec],
        label="inspect state status",
    ).strip()


def commit(instance_dir: Path, pathspec: tuple[str, ...], message: str) -> str | None:
    """Stage and commit one writer's pathspec. Returns the new HEAD, or None.

    None means the pathspec held nothing to commit; the caller decides whether
    that is normal (an unchanged tick) or a fault (a write that changed nothing).
    """
    git(instance_dir, ["add", "--", *pathspec], label="stage state")
    if not status(instance_dir, pathspec):
        return None
    git(
        instance_dir,
        [*commit_identity(instance_dir), "commit", "--quiet", "--message", message, "--", *pathspec],
        label="commit state",
    )
    return head(instance_dir)
