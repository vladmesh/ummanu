"""The product's one reader of a head's launch identity, beside the one writer of it.

`command.with_pid_heartbeat` is what puts the record on disk: the head's own shell writes `pid`,
`boot_id`, `proc_starttime_ticks` beside the run, role and task it was launched for, and then
`exec`s, so the pid stays the head's own for its whole life. This module is the other half of that
one scheme — the classification of such a record into "this launch is running", "it ended" and
"this pid is somebody else's now".

It lives here rather than in the control plane that grew it because both of the things that need
it live under this package: `local_pty_head.LocalPtyHeadRuntime` is handed this reader by whoever
builds it, and the mechanical-role driver in `runtime/dispatch.py` builds
one too. `ummanu.dispatch.watchdog` re-exports every name below, so the control plane keeps
the spelling it has always used and there is still exactly one implementation.

A record survives a reboot and a pid can be handed out again, which is why a bare "does this
integer name a process?" is not the question any of these answer: `boot_id` and
`proc_starttime_ticks` are what make a stale record read as the dead head it describes, and
`expected` is what makes a live process that is not this launch read as a mismatch rather than as
a match. Missing, half-written, malformed and legacy pid-only files keep their own inconclusive
states: a reader that cannot tell must never be read as one that said no.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

#: The record layout `with_pid_heartbeat` writes and this module reads. A record of any other
#: version is inconclusive rather than dead: it was written by a scheme this reader does not know.
HEARTBEAT_VERSION = 1

HEARTBEAT_LIVE_MATCH = "live-match"
HEARTBEAT_DEAD = "dead"
HEARTBEAT_IDENTITY_MISMATCH = "identity-mismatch"
HEARTBEAT_NOT_YET_WRITTEN = "not-yet-written"
HEARTBEAT_UNREADABLE = "unreadable"


def _proc_starttime_ticks(pid: int) -> str:
    """Linux's process-creation discriminator for a live PID.

    ``comm`` may contain spaces and parentheses, so splitting the complete ``stat`` line on
    whitespace is not safe. The final closing parenthesis ends it; field 22 is then token 19 of the
    remaining fields (which begin at field 3).
    """
    stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    close = stat.rfind(")")
    fields = stat[close + 2 :].split()
    if close < 0 or len(fields) <= 19:
        raise ValueError("/proc stat has no process start time")
    return fields[19]


def _boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()


#: What `/proc/<pid>/status` says about a pid that answered `kill(pid, 0)`: a process that is there
#: to run, one the kernel has not reaped yet, one that has gone since the signal, and one whose
#: status could not be read at all. The last is inconclusive; the two middle ones are ended.
_PROCESS_RUNNING = "running"
_PROCESS_ZOMBIE = "zombie"
_PROCESS_GONE = "gone"
_PROCESS_UNREADABLE = "unreadable"


def _process_state(pid: int) -> str:
    """What the process itself says it is, read from its own `/proc` status.

    `kill(pid, 0)` answers for a zombie exactly as it does for a running process, so a check made
    right at exit reads one tick stale as still alive; this read is what closes that gap. It closes
    a second one too: a pid reaped between that signal and this read has no `/proc` entry left, and
    that absence is `gone` and never "not a zombie". Classified as running it made a head that had
    already exited read as `live-match` -- the one answer a watchdog may not give about a dead head,
    since it is what the control plane treats as "this launch is still running". CI caught it as an
    intermittent `'live-match' != 'dead'` a moment after a head was stopped (2026-09-12).

    A status that exists but cannot be read is neither: that is `unreadable`, and the caller keeps
    it inconclusive rather than deciding anything on it.
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

    A sprint's reference already reads `sprint:<ID>`, so prefixing it again produced
    `sprint:sprint:<ID>` (secretary-1698); a reference that already carries its kind is kept as it is.
    Every writer and every reader of the record spells the task through this one function.
    """
    kind = str(kind or "")
    ref = str(ref or "")
    if not kind or not ref:
        return ""
    return ref if ref.startswith(f"{kind}:") else f"{kind}:{ref}"


def _task_matches(recorded: str, expected: str) -> bool:
    """Whether a record's `task` is the binding expected, in its spelling or a pre-1698 one.

    Heads launched before secretary-1698 wrote two other spellings and may still be running: an
    Orca-launched observer says `sprint:sprint:<ID>`, and a local-pty head says its bare reference
    (`steward`, `secretary-1463`). Reading those as foreign would declare a running head someone
    else's and replace it. The alias is safe because the run id beside it is still compared exactly.
    """
    if recorded == expected:
        return True
    kind, separator, ref = expected.partition(":")
    return bool(separator) and recorded in (f"{kind}:{expected}", ref)


def _record_matches_expected(record: Mapping[str, Any], expected: Mapping[str, Any] | None) -> bool:
    if expected is None:
        return True
    # An empty expected run is deliberately not a wildcard.  A caller that has no durable HeadRun
    # cannot prove a process belongs to it, even when its pid file happens to be well formed.
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
    """Publish the versioned launch identity for the current process atomically.

    Head shells use an equivalent tiny stdlib writer before ``exec``.  Short-lived
    launch-bound helpers use this function so the reader sees the exact same
    heartbeat contract, rather than a probe-only liveness convention.
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

    A readable record has one of ``live-match``, ``dead`` or ``identity-mismatch``. Missing,
    partially written, malformed and legacy PID-only files retain their distinct inconclusive states.

    A pid that answered ``kill(pid, 0)`` is then asked what it is (:func:`_process_state`): a zombie
    and a pid reaped between the two are both ``dead``, because neither is a launch that is running.
    So is a pid reaped before its start time is read: its `/proc/<pid>/stat` is gone, which is the
    same absence `_process_state` calls `gone`, not an unreadable one (CI, 2026-10-04: a stopped
    head read `dead`, was reaped, and the next read said `unreadable`).
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
        # A normal dispatcher head is owned by us.  Treat an uninspectable process as inconclusive:
        # a weak permission answer cannot authorize a signal or a replacement.
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
