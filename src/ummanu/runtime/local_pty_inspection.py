"""Supervisor lease inspection and canonical scope-owner cleanup fences."""

from __future__ import annotations

import os
from collections.abc import (
    Callable,
    Iterator,
    Sequence,
)
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ummanu.runtime.head import local_pty
from ummanu.runtime.head.local_pty import protocol
from ummanu.runtime.head.local_pty.scope_inventory import RuntimeScopeInventory
from ummanu.runtime.head.local_pty.scoped_lifecycle import ScopedHeadLifecycle
from ummanu.runtime.head.run import HeadRun
from ummanu.runtime.head.task_ref import TaskRef
from ummanu.runtime.local_pty_values import _binding_of


def head_run_supervisor_files(run_dir: str | os.PathLike[str]) -> tuple[Path, Path]:
    """`(supervisor.lock, supervisor.pid)` under the run directory, for a reader outside it.

    Nothing is opened; a reader must never take the lock itself.
    """
    root = Path(run_dir)
    return root / protocol.SUPERVISOR_LOCK_NAME, root / protocol.SUPERVISOR_PID_NAME


@dataclass(frozen=True)
class SupervisorLease:
    """Who holds one run's supervisor lock, per the kernel's lock table, and what the files say.

    `lock_readable` false: `supervisor.lock` could not be read and nothing else was looked at.
    `table_readable` false: `/proc/locks` could not be read, so the holder is unknown. `error` says
    what failed. With both true, empty `holders` means no process holds the lock. `content_error`
    is set when a file held something other than a pid; that pid is then `None`.
    """

    lock_readable: bool
    table_readable: bool = False
    holders: tuple[int, ...] = ()
    written_pid: int | None = None
    supervisor_pid: int | None = None
    error: str = ""
    content_error: str = ""


def head_run_supervisor_lease(run_dir: str | os.PathLike[str]) -> SupervisorLease:
    """Read who holds this run's supervisor lock without taking it, for a reader outside the run.

    A supervisor holds an exclusive `flock` on `supervisor.lock` for its life, so the holder comes
    from `/proc/locks`; the pids in `supervisor.lock` and `supervisor.pid` are reported beside it.
    An unheld lock means no supervisor owns the run; whether its head still runs is the
    heartbeat's question.
    """
    path, pid_path = head_run_supervisor_files(run_dir)
    try:
        info = path.stat()
        written = path.read_bytes()
    except OSError as exc:
        return SupervisorLease(lock_readable=False, error=str(exc.strerror or exc))
    written_pid, lock_damage = _pid_content(path.name, written)
    try:
        supervisor_pid, pid_damage = _pid_content(pid_path.name, pid_path.read_bytes())
    except OSError:
        supervisor_pid, pid_damage = None, ""
    content_error = "; ".join(damage for damage in (lock_damage, pid_damage) if damage)
    try:
        holders = _flock_holders(info)
    except OSError as exc:
        return SupervisorLease(
            lock_readable=True,
            written_pid=written_pid,
            supervisor_pid=supervisor_pid,
            error=str(exc.strerror or exc),
            content_error=content_error,
        )
    return SupervisorLease(
        lock_readable=True,
        table_readable=True,
        holders=tuple(holders),
        written_pid=written_pid,
        supervisor_pid=supervisor_pid,
        content_error=content_error,
    )


def head_run_directory(root: str | os.PathLike[str], run_id: str) -> Path:
    """The run directory `run_id` names under `root`, refused for anything that is not a run id.

    The substrate's own rule, so a reader outside the backend builds the path the supervisor did
    and cannot be walked out of `root` by a value holding a separator or a dot name.
    """
    try:
        return protocol.run_dir_for(root, run_id)
    except local_pty.ProtocolError as exc:
        raise ValueError(str(exc)) from None


def head_run_pid_file(root: str | os.PathLike[str], run_id: str) -> Path:
    """The heartbeat `run_id`'s own run directory holds: where a stop of that run reads its launch
    identity when the run is handed over without a pid file of its own."""
    return head_run_directory(root, run_id) / protocol.PID_FILE_NAME


def fence_cleanup_scopes(
    root: Path,
    workspace: str,
    task: TaskRef,
    runs: Sequence[HeadRun],
    *,
    recorded_only: bool = False,
    remaining: Callable[[], float] | None = None,
) -> None:
    """Refuse Git settlement while an unrecorded generation owns its target.

    This read uses the same canonical owner as stop. The dispatcher serializes
    admission and Git effects; recorded generations still go through runtime.stop
    for native identity and recursive empty-scope proof, including on replay.

    `recorded_only` reads nothing but the recorded runs' own canonical directories:
    a replaced owner's workspace and task now belong to its successor. A caller's
    `remaining` cuts each native observation of a terminal scope's disappearance.
    """
    if root.absolute() != root.resolve():
        raise ValueError("cleanup scope root is substituted")
    if not root.exists():
        return
    known = {(run.run_id, run.scope_generation): run for run in runs}
    task_identity = _binding_of(task)

    def entries() -> Iterator[Path]:
        # Entry by entry from the native iterator, so a caller's deadline is checked between them.
        with os.scandir(root) as scan:
            for entry in scan:
                yield root / entry.name

    directories = [protocol.run_dir_for(root, run.run_id) for run in runs] if recorded_only else entries()
    for directory in directories:
        if remaining is not None and remaining() <= 0:
            raise ValueError("the caller's deadline passed while cleanup scope ownership was read")
        if directory.is_symlink():
            raise ValueError("cleanup scope directory is substituted")
        if not directory.is_dir():
            continue
        owner = ScopedHeadLifecycle.from_run_dir(directory)
        if owner is None:
            continue
        if (
            directory != protocol.run_dir_for(root, owner.run_id)
            or (directory / "scope-owner.json").is_symlink()
        ):
            raise ValueError("cleanup scope owner path differs from its canonical identity")
        record = ScopedHeadLifecycle.read_owner(directory)
        run = known.get((owner.run_id, owner.generation))
        if recorded_only and run is None:
            raise ValueError("cleanup scope owner run or generation differs from its recorded head")
        if run is not None and (
            record.get("workspace") != run.workspace
            or record.get("task") != _binding_of(run.task_ref)
            or record.get("role") != run.role
        ):
            raise ValueError("cleanup scope owner binding differs from its recorded head")
        if recorded_only:
            continue
        if record.get("workspace") == workspace or record.get("task") == task_identity:  # noqa: SIM102
            if (owner.run_id, owner.generation) not in known:
                if record.get("cleanup_complete") and not record.get("launch_allowed"):
                    # A retained terminal flag cannot bless a reused live unit.
                    inventory = (
                        runtime_scope_inventory(root.parent, {record["unit"]})
                        if remaining is None
                        else runtime_scope_inventory(root.parent, {record["unit"]}, remaining=remaining)
                    )
                    if inventory.errors or record["unit"] not in inventory.disappeared:
                        raise ValueError("cleanup terminal scope lacks current disappearance proof")
                    continue
                raise ValueError("cleanup workspace has an unrecorded or newer scope owner")


def runtime_scope_inventory(
    data_dir: Path, units: set[str], *, remaining: Callable[[], float] | None = None
) -> RuntimeScopeInventory:
    """Read canonical lifecycle ownership for host preservation and diagnostics.

    Consumers use this runtime boundary, never the private PTY owner format.
    The projection grants no launch, adoption or cleanup authority. A caller's
    `remaining` cuts each native scope observation to what is left of its deadline.
    """
    from ummanu.runtime.head.local_pty.scope_inventory import read_runtime_scopes

    return read_runtime_scopes(data_dir, units, remaining=remaining)


def head_scope_owner_lock(run_dir: str | os.PathLike[str]) -> AbstractContextManager[None]:
    """The run's own scope-owner flock: every admission, cleanup and owner rewrite holds it.

    Contention raises `MemoryScopeError`, the lifecycle's retryable refusal.
    """
    return ScopedHeadLifecycle.owner_lock(Path(run_dir))


def head_scope_owner_valid(record: Any) -> dict[str, Any]:
    """The lifecycle's own validation of a scope-owner record; raises `MemoryScopeError`."""
    return ScopedHeadLifecycle.validate_owner(record)


def _flock_holders(info: os.stat_result) -> list[int]:
    """The pids `/proc/locks` names as holding an `flock` on this file."""
    holders = []
    wanted = (os.major(info.st_dev), os.minor(info.st_dev), info.st_ino)
    with open("/proc/locks", encoding="utf-8") as table:
        for line in table:
            # `N: FLOCK ADVISORY WRITE <pid> <major>:<minor>:<inode> 0 EOF`; a waiter reads `N: ->`.
            fields = line.split()
            if len(fields) < 6 or fields[1] != "FLOCK":
                continue
            try:
                major, minor, inode = fields[5].split(":")
                if (int(major, 16), int(minor, 16), int(inode)) == wanted:
                    holders.append(int(fields[4]))
            except ValueError:
                continue
    return holders


def _pid_content(name: str, raw: bytes) -> tuple[int | None, str]:
    """The pid a lock or pid file holds, or `None` and why its content is not one; never raises.

    An empty file is a supervisor that has not written its pid yet, not damage. Anything else that
    is not a plain ASCII decimal -- bytes that are not UTF-8, `1e999`, a sign -- is unreadable
    content rather than an exception.
    """
    text = raw.strip()
    if not text:
        return None, ""
    if text.isdigit():  # bytes.isdigit is ASCII-only, so int() below cannot refuse it
        return int(text), ""
    return None, f"{name} holds no pid ({text[:40]!r})"
