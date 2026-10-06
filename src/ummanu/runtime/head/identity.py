"""Reader of a head's launch identity; `command.with_pid_heartbeat` is the writer.

The head shell writes `pid`, `boot_id`, `proc_starttime_ticks`, run, role and task, then `exec`s.
This module classifies such a record as running, ended, or a reused pid. `boot_id` and start ticks
make a stale record read dead; `expected` makes a foreign live process a mismatch. Missing,
partial, malformed and pid-only files stay inconclusive: "cannot tell" never reads as "no".
`ummanu.dispatch.watchdog` re-exports these names. See docs/PROTOCOLS.md "Head heartbeat identity".
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

#: Any other record version is inconclusive, not dead.
HEARTBEAT_VERSION = 1

HEARTBEAT_LIVE_MATCH = "live-match"
HEARTBEAT_DEAD = "dead"
HEARTBEAT_IDENTITY_MISMATCH = "identity-mismatch"
HEARTBEAT_NOT_YET_WRITTEN = "not-yet-written"
HEARTBEAT_UNREADABLE = "unreadable"


def _proc_starttime_ticks(pid: int) -> str:
    """Linux's process-creation discriminator for a live PID.

    ``comm`` may contain spaces and parentheses: split after the last ``)``; field 22 is then
    token 19.
    """
    stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    close = stat.rfind(")")
    fields = stat[close + 2 :].split()
    if close < 0 or len(fields) <= 19:
        raise ValueError("/proc stat has no process start time")
    return fields[19]


def _boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()


#: `/proc/<pid>/status` verdict for a pid that answered `kill(pid, 0)`. Zombie and gone mean ended;
#: unreadable is inconclusive.
_PROCESS_RUNNING = "running"
_PROCESS_ZOMBIE = "zombie"
_PROCESS_GONE = "gone"
_PROCESS_UNREADABLE = "unreadable"


def _process_state(pid: int) -> str:
    """The process state from its own `/proc` status.

    `kill(pid, 0)` succeeds for a zombie, and a pid reaped after that signal has no `/proc` entry:
    both must read as ended (`zombie`/`gone`), never as running, or a dead head reads `live-match`.
    A status that exists but cannot be read is `unreadable` (inconclusive).
    """
    try:
        status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
    except FileNotFoundError:
        return _PROCESS_GONE
    except OSError:
        return _PROCESS_UNREADABLE
    for line in status.splitlines():
        if line.startswith("State:"):
            return _PROCESS_ZOMBIE if "Z" in line else _PROCESS_RUNNING
    return _PROCESS_RUNNING


def _is_stopped(pid: int) -> bool:
    """Whether a live process is suspended with SIGSTOP/SIGTSTP."""
    try:
        status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
    except OSError:
        return False
    for line in status.splitlines():
        if line.startswith("State:"):
            return "T" in line
    return False


def _unreadable(reason: str) -> dict[str, Any]:
    return {"known": False, "alive": False, "match": False, "state": HEARTBEAT_UNREADABLE, "reason": reason}


def task_binding(kind: str, ref: str) -> str:
    """The `task` a launch identity names: `kind:ref`, with the kind written exactly once.

    A ref already carrying its kind (`sprint:<ID>`) is kept. Every writer and reader spells the task
    through this function.
    """
    kind = str(kind or "")
    ref = str(ref or "")
    if not kind or not ref:
        return ""
    return ref if ref.startswith(f"{kind}:") else f"{kind}:{ref}"


def _task_matches(recorded: str, expected: str) -> bool:
    """Whether a record's `task` is the expected binding, in its spelling or a legacy one.

    Older heads may still run with `sprint:sprint:<ID>` (Orca observer) or a bare ref (local-pty);
    reading those as foreign would replace a running head. Safe because the run id is compared exactly.
    """
    if recorded == expected:
        return True
    kind, separator, ref = expected.partition(":")
    return bool(separator) and recorded in (f"{kind}:{expected}", ref)


def _record_matches_expected(record: Mapping[str, Any], expected: Mapping[str, Any] | None) -> bool:
    if expected is None:
        return True
    # An empty expected field is not a wildcard: without a durable HeadRun nothing can be proven.
    for name in ("run_id", "role", "task"):
        value = str(expected.get(name) or "")
        if not value:
            return False
        recorded = str(record.get(name) or "")
        if not (_task_matches(recorded, value) if name == "task" else recorded == value):
            return False
    leaf = str(expected.get("leaf") or "")
    return not leaf or str(record.get("leaf") or "") == leaf


def _read_record(pid_file: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        raw = Path(pid_file).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, {"known": False, "alive": False, "match": False, "state": HEARTBEAT_NOT_YET_WRITTEN}
    except OSError as exc:
        return None, _unreadable(type(exc).__name__)
    try:
        record = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None, _unreadable("malformed-json")
    if not isinstance(record, dict):
        return None, _unreadable("record-is-not-an-object")
    if record.get("version") != HEARTBEAT_VERSION:
        return None, _unreadable("unknown-version")
    try:
        pid = int(cast(Any, record.get("pid")))
    except (TypeError, ValueError):
        return None, _unreadable("invalid-pid")
    required = ("boot_id", "proc_starttime_ticks", "run_id", "role", "task")
    if pid <= 0 or any(not str(record.get(name) or "") for name in required):
        return None, _unreadable("missing-identity")
    record["pid"] = pid
    record["leaf"] = str(record.get("leaf") or "")
    return record, None


def publish_heartbeat(pid_file: str, identity: Mapping[str, str], *, pid: int | None = None) -> None:
    """Atomically publish the versioned launch identity for the current process.

    Head shells use an equivalent stdlib writer before ``exec``; short-lived launch-bound helpers use
    this so the reader sees the same contract.
    """
    current_pid = os.getpid() if pid is None else pid
    record = {
        "version": HEARTBEAT_VERSION,
        "pid": current_pid,
        "boot_id": _boot_id(),
        "proc_starttime_ticks": _proc_starttime_ticks(current_pid),
        "run_id": str(identity.get("run_id") or ""),
        "role": str(identity.get("role") or ""),
        "task": str(identity.get("task") or ""),
    }
    if not all(record[name] for name in ("run_id", "role", "task")):
        raise ValueError("heartbeat identity is incomplete")
    leaf = str(identity.get("leaf") or "")
    if leaf:
        record["leaf"] = leaf
    path = Path(pid_file)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".ummanu-heartbeat-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            Path(temporary).unlink()
        except OSError:
            pass
        raise


def head_process_status(pid_file: str, *, expected: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Classify a launch-identity heartbeat without trusting PID reuse.

    A readable record is ``live-match``, ``dead`` or ``identity-mismatch``; unreadable files keep
    distinct inconclusive states. A zombie, or a pid reaped before its status or start time is read
    (`/proc` entry gone), is ``dead``.
    """
    record, failure = _read_record(pid_file)
    if failure is not None:
        return failure
    assert record is not None
    pid = int(record["pid"])
    dead: dict[str, Any] = {
        "known": True,
        "alive": False,
        "match": False,
        "state": HEARTBEAT_DEAD,
        "pid": pid,
        "record": record,
    }
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return dead
    except PermissionError:
        # Uninspectable is inconclusive: it cannot authorize a signal or a replacement.
        return _unreadable("process-not-inspectable")
    except OSError as exc:
        return _unreadable(type(exc).__name__)
    try:
        boot_matches = str(record["boot_id"]) == _boot_id()
        start_matches = str(record["proc_starttime_ticks"]) == _proc_starttime_ticks(pid)
    except FileNotFoundError:
        if _process_state(pid) == _PROCESS_GONE:
            return dead
        return _unreadable("FileNotFoundError")
    except (OSError, ValueError) as exc:
        return _unreadable(type(exc).__name__)
    state = _process_state(pid)
    if state == _PROCESS_UNREADABLE:
        return _unreadable("process-status-unreadable")
    if state in (_PROCESS_ZOMBIE, _PROCESS_GONE):
        return dead
    if not boot_matches or not start_matches or not _record_matches_expected(record, expected):
        return {
            "known": True,
            "alive": True,
            "match": False,
            "state": HEARTBEAT_IDENTITY_MISMATCH,
            "pid": pid,
            "record": record,
            "stopped": _is_stopped(pid),
        }
    return {
        "known": True,
        "alive": True,
        "match": True,
        "state": HEARTBEAT_LIVE_MATCH,
        "pid": pid,
        "record": record,
        "stopped": _is_stopped(pid),
    }


def heartbeat_is_live_match(status: Mapping[str, Any]) -> bool:
    return str(status.get("state") or "") == HEARTBEAT_LIVE_MATCH


def heartbeat_is_dead(status: Mapping[str, Any]) -> bool:
    return str(status.get("state") or "") == HEARTBEAT_DEAD


def heartbeat_is_mismatch(status: Mapping[str, Any]) -> bool:
    return str(status.get("state") or "") == HEARTBEAT_IDENTITY_MISMATCH
