"""Journal fold and public read-only journal, screen and turn views."""

from __future__ import annotations

import json
import math
import os
from dataclasses import (
    dataclass,
    replace,
)
from pathlib import Path
from typing import Any

from ummanu.runtime.head import local_pty
from ummanu.runtime.head.local_pty import protocol
from ummanu.runtime.head.local_pty.journal import (
    JOURNAL_MAX_BYTES,
    JOURNAL_RECORD_BYTES,
    SCOPE_BOUND,
    _unique,
)
from ummanu.runtime.head.local_pty.run_end import RunEnd, run_end
from ummanu.runtime.local_pty_values import _Address


@dataclass(frozen=True)
class _JournalReplay:
    """One run's records folded in sequence order: the head's shape as of the last of them.

    The time fields serve the vitality reading (`head_run_turn_reading`) from the same fold. Times
    are the journal's `at`; `times_valid` is false when a needed one was unusable (the admission
    reader ignores it, the vitality reader refuses the window).
    """

    draining: bool = False
    turn_open: bool = False
    turn: int = 0
    exited: bool = False
    # A `turn.started` or `turn.finished` of the current incarnation was seen: a turn exists.
    turn_seen: bool = False
    # Any record that fixes the turn state by itself (`run.started`, `turn.*`, `run.exited`) was
    # seen. A window that began mid-history can say the turn state only if this is true.
    anchored: bool = False
    turn_started_at: float = 0.0
    turn_finished_at: float = 0.0
    input_at: float = 0.0
    progress_seq: int = 0
    progress_at: float = 0.0
    times_valid: bool = True


def _replay_journal(events: Any) -> _JournalReplay:
    """Fold one run's usable records, never raising on a value a record should not hold."""
    replay = _JournalReplay()
    previous = None
    for event in events:
        seq = event.get("seq")
        if previous is not None and seq != previous + 1:
            # A retained old turn/start cannot supply state across an eviction gap.
            replay = _JournalReplay()
        previous = seq if type(seq) is int else None
        kind = str(event.get("kind") or "")
        if kind == local_pty.RUN_STARTED:
            # A new incarnation: nothing an older one wrote, its times included, answers for it.
            replay = _JournalReplay(anchored=True)
        elif kind == local_pty.TURN_STARTED:
            at = _journal_time(event.get("at"))
            replay = replace(
                replay,
                turn_open=True,
                turn=_journal_count(event.get("turn"), replay.turn),
                turn_seen=True,
                anchored=True,
                turn_started_at=at or 0.0,
                times_valid=replay.times_valid and at is not None,
            )
        elif kind == local_pty.TURN_FINISHED:
            at = _journal_time(event.get("at"))
            replay = replace(
                replay,
                turn_open=False,
                turn_seen=True,
                anchored=True,
                turn_finished_at=at or 0.0,
                times_valid=replay.times_valid and at is not None,
            )
        elif kind == local_pty.INPUT_ACCEPTED:
            at = _journal_time(event.get("at"))
            replay = replace(replay, input_at=at or 0.0, times_valid=replay.times_valid and at is not None)
        elif kind == local_pty.PROVIDER_PROGRESSED:
            at = _journal_time(event.get("at"))
            replay = replace(
                replay,
                progress_seq=_journal_count(event.get("seq"), 0),
                progress_at=at or 0.0,
                times_valid=replay.times_valid and at is not None,
            )
        elif kind in (local_pty.DRAIN_REQUESTED, local_pty.RUN_STOPPING):
            replay = replace(replay, draining=True)
        elif kind == local_pty.RUN_EXITED:
            replay = replace(replay, exited=True, turn_open=False, anchored=True)
    return replay


#: The largest sequence or turn number a journal reading hands on. The writer counts from 1 in
#: steps of one, so nothing real comes near it; a record that claims more is damaged, and a bound
#: keeps a hostile `10**400` from reaching a cursor that is compared as text.
_JOURNAL_COUNT_LIMIT = 2**53


def _journal_count(value: Any, fallback: int) -> int:
    """A positive record count (`seq`, `turn`) as the writer wrote it, else `fallback`."""
    if type(value) is not int or not 0 < value <= _JOURNAL_COUNT_LIMIT:
        return fallback
    return value


def _journal_time(value: Any) -> float | None:
    """A record's `at` as epoch seconds, or `None` for anything the writer could not have written."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        at = float(value)
    except OverflowError:
        return None
    if not math.isfinite(at) or at <= 0:
        return None
    return at


def head_run_turn_reading(
    root: str | os.PathLike[str], run_id: str, *, max_bytes: int = local_pty.JOURNAL_TAIL_BYTES
) -> dict[str, Any]:
    """Whether this run's head is in a turn, and since when, from its supervisor's journal.

    The vitality reading, over the same bounded tail and fold as `_journal_state`:

      * `turn` is `active` while the last `turn.started` has no `turn.finished` after it, else
        `idle`;
      * `turn_since` is the open turn's `turn.started`, or for an idle head the latest of
        `turn.finished`, `input.accepted` and `turn.started`;
      * `progress_seq`/`progress_at` are the last `provider.progressed` in the window, 0 if none.

    Never raises: an unreadable journal, no record of this run, a torn last line, any line
    `_strict_record` refuses, an exited head, or no turn yet all return
    `{"state": "unavailable", "reason": ...}`, read by callers as no answer, not a stopped head.
    The window is read raw (`tail_window`) so no coerced value becomes stall evidence. A window
    begun mid-history answers when it holds a turn boundary (`turn.*`, `run.started`,
    `run.exited`), since the turn state depends on nothing older.
    """
    try:
        return _turn_reading(
            protocol.run_dir_for(root, run_id) / protocol.JOURNAL_NAME, str(run_id), max_bytes
        )
    except Exception as exc:  # noqa: BLE001 - the source's one guard: a failed read is no answer
        return _turn_unavailable(f"the supervisor journal could not be read ({type(exc).__name__})")


def head_run_screen_lines(
    root: str | os.PathLike[str], run_id: str, *, timeout: float = 2.0
) -> dict[str, Any]:
    """This run's head screen as text lines, rendered from its supervisor's output buffer.

    Read-only (one `status` and one `output` request), rendered with the supervisor's `ScreenModel`
    at the head's terminal size. Every failure returns `{"state": "unavailable"}`.
    """
    from ummanu.runtime.head.local_pty.screen import ScreenModel

    try:
        socket_path = protocol.run_dir_for(root, run_id) / protocol.SOCKET_NAME
        with local_pty.SupervisorClient.connect(socket_path, timeout=timeout) as client:
            status = client.status()
            output = client.read_output()
    except Exception as exc:  # noqa: BLE001 - a screen nobody can read is no answer, never an error
        return _turn_unavailable(f"the head's screen could not be read ({type(exc).__name__})")
    if not output.get("ok"):
        return _turn_unavailable("the head's supervisor did not hand out its output")
    screen = ScreenModel(int(status.get("rows") or 24), int(status.get("cols") or 80))
    screen.feed(bytes(output.get("bytes_data") or b""))
    return {"state": "observed", "run_id": run_id, "lines": screen.lines()}


def _turn_reading(path: Path, run_id: str, max_bytes: int) -> dict[str, Any]:
    window = local_pty.tail_window(path, max_bytes=max_bytes)
    if window is None:
        return _turn_unavailable("the supervisor journal holds no record of this run")
    raw, partial_head = window
    if raw and not raw.endswith(b"\n"):
        return _turn_unavailable("the supervisor journal's last line is torn")
    events: list[dict[str, Any]] = []
    for number, line in enumerate(raw.split(b"\n")[:-1] if raw else (), start=1):
        refusal = _strict_record(line, run_id, events[-1] if events else None)
        if isinstance(refusal, str):
            return _turn_unavailable(f"supervisor journal line {number} of the window {refusal}")
        events.append(refusal)
    if not events:
        return _turn_unavailable("the supervisor journal holds no record of this run")
    replay = _replay_journal(events)
    if partial_head and not replay.anchored:
        return _turn_unavailable(
            "the supervisor journal window begins mid-history and holds no turn boundary"
        )
    if not replay.times_valid:
        return _turn_unavailable("a supervisor journal record carries no valid time")
    if replay.exited:
        return _turn_unavailable("the supervisor journal says the head exited")
    if not replay.turn_seen:
        return _turn_unavailable("no turn has started in this run yet")
    since = (
        replay.turn_started_at
        if replay.turn_open
        else max(replay.turn_finished_at, replay.input_at, replay.turn_started_at)
    )
    return {
        "state": "observed",
        "run_id": run_id,
        "turn": "active" if replay.turn_open else "idle",
        "turn_since": since,
        "turn_number": replay.turn,
        "progress_seq": replay.progress_seq,
        "progress_at": replay.progress_at,
        "seq": events[-1]["seq"],
    }


#: The epoch-seconds range a record's `at` may hold: 2000-01-01 to 2100-01-01. The writer stamps
#: `time.time()`, so anything outside is damage, not an early or late clock.
_JOURNAL_EPOCH_MIN = 946_684_800.0
_JOURNAL_EPOCH_MAX = 4_102_444_800.0
#: Kinds whose writer always stamps the turn number (`supervisor.py`); on them `turn` is required.
_TURN_NUMBERED = frozenset({local_pty.TURN_STARTED, local_pty.TURN_FINISHED, local_pty.PROVIDER_PROGRESSED})


def _strict_int(value: Any) -> bool:
    """A JSON integer in `(0, 2**63)`: never a bool, float or string that could be coerced to one."""
    return type(value) is int and 0 < value < 2**63


def _strict_record(line: bytes, run_id: str, previous: dict[str, Any] | None) -> dict[str, Any] | str:
    """One window line as the record its writer wrote, or why it is not one.

    Never coerces. Requires `schema_version` exactly 1, a known `kind`, this run's `run_id`, `seq`
    a JSON integer in `(0, 2**63)` above the previous line's, `at` finite in `[2000, 2100)`, and
    `turn` an integer in `(0, 2**63)` wherever present (required on `_TURN_NUMBERED` kinds). Any
    failure refuses the whole window.
    """
    try:
        record = json.loads(line.decode("utf-8"), object_pairs_hook=_unique)
    except (UnicodeDecodeError, ValueError):
        return "is not a JSON record"
    if not isinstance(record, dict):
        return "is not a JSON object"
    if (
        type(record.get("schema_version")) is not int
        or record.get("schema_version") != local_pty.JOURNAL_SCHEMA_VERSION
    ):
        return "has an unsupported schema_version"
    kind = record.get("kind")
    if not isinstance(kind, str) or kind not in local_pty.EVENT_KINDS:
        return "has no known kind"
    if not isinstance(record.get("run_id"), str):
        return "has no run_id string"
    if record["run_id"] != run_id:
        return "names another run"
    seq = record.get("seq")
    if not _strict_int(seq):
        return "has a seq that is not a positive JSON integer"
    if previous is not None and seq <= previous["seq"]:
        return "is out of sequence order"
    at = record.get("at")
    if isinstance(at, bool) or not isinstance(at, (int, float)):
        return "has a time that is not a number"
    try:
        at_seconds = float(at)
    except OverflowError:
        return "has a time that is not finite"
    if not math.isfinite(at_seconds) or not _JOURNAL_EPOCH_MIN <= at_seconds < _JOURNAL_EPOCH_MAX:
        return "has a time outside the epoch range"
    if ("turn" in record or kind in _TURN_NUMBERED) and not _strict_int(record.get("turn")):
        return "has a turn that is not a positive JSON integer"
    return record


def _turn_unavailable(reason: str) -> dict[str, Any]:
    return {"state": "unavailable", "reason": reason}


def _has_exited(address: _Address) -> bool:
    """Whether the current journal incarnation has a complete `run.exited`.

    Run directories and journals are reused. An exit from an earlier incarnation cannot confirm
    that the head started after it is gone, and a damaged tail cannot prove which incarnation its
    final records belong to. The launch identity remains the primary stop witness.
    """
    try:
        reading = local_pty.read_events(address.journal_path)
    except OSError:
        return False
    if reading.truncated_tail or reading.malformed or not reading.ordered:
        return False
    started = max(
        (int(event.get("seq") or 0) for event in reading.of_kind(local_pty.RUN_STARTED)),
        default=0,
    )
    exited = max(
        (int(event.get("seq") or 0) for event in reading.of_kind(local_pty.RUN_EXITED)),
        default=0,
    )
    return bool(started and exited > started)


def head_run_journal(run_dir: str | os.PathLike[str]) -> tuple[dict[str, Any], ...]:
    """Retained records from one head's supervisor, read from outside its lifecycle.

    Includes pinned terminal records; use `head_run_journal_read` for partial-history flags. A missing file is
    an empty journal; `OSError` propagates, so "unreadable" stays distinct from "empty".
    """
    return head_run_journal_read(run_dir).events


def head_run_journal_read(run_dir: str | os.PathLike[str]) -> local_pty.JournalReadResult:
    """`head_run_journal` with what the read left out (`malformed`, `truncated_tail`).

    For a reader that must not present a damaged journal as a clean one. `OSError` propagates.
    """
    return local_pty.read_events(Path(run_dir) / protocol.JOURNAL_NAME)


def head_run_first_record(run_dir: str | os.PathLike[str]) -> dict[str, Any] | None:
    """Whose run this is, from the journal's first record: its `task`, `role`, `run_id` and `at`.

    Only those four fields, never the record itself: `run.started` carries the head's command,
    and the command carries the head's memory token. `None` when there is no journal; the read is
    incremental, at most `JOURNAL_MAX_BYTES` with a per-record `JOURNAL_RECORD_BYTES` cap.
    Multiple pre-head crash bindings may precede the first start. No complete `run.started`
    record raises `ValueError`. `OSError` propagates, as it does from the other readers here.
    """
    path = Path(run_dir) / protocol.JOURNAL_NAME
    try:
        with open(path, "rb") as journal:
            remaining = JOURNAL_MAX_BYTES
            while remaining:
                line = journal.readline(min(JOURNAL_RECORD_BYTES + 1, remaining))
                remaining -= len(line)
                if len(line) > JOURNAL_RECORD_BYTES or not line.endswith(b"\n"):
                    raise ValueError("the journal's first start is outside a complete bounded record")
                if not line.strip():
                    continue
                record = json.loads(line.decode("utf-8"), object_pairs_hook=_unique)
                if isinstance(record, dict) and record.get("kind") == SCOPE_BOUND:
                    continue
                if not isinstance(record, dict) or record.get("kind") != local_pty.RUN_STARTED:
                    raise ValueError("the journal does not begin with scope bindings or run.started")
                return {key: record.get(key) for key in ("task", "role", "run_id", "at")}
    except FileNotFoundError:
        return None
    raise ValueError("the journal's first start is outside the metadata read limit")


def head_run_journal_tail(run_dir: str | os.PathLike[str]) -> local_pty.JournalReadResult:
    """The last `JOURNAL_TAIL_BYTES` of one run's journal, for a reader showing its recent records.

    Bounded where `head_run_journal_read` is not, because a view of what a head did lately needs
    its end and nothing else. `OSError` propagates for the reason it does there.
    """
    return local_pty.read_tail(Path(run_dir) / protocol.JOURNAL_NAME)


def head_run_loss_reason(root: str | os.PathLike[str], run_id: str) -> str | None:
    """Read a supervisor's typed death record for this exact run, if one exists."""
    from ummanu.runtime.head.memory import MEMORY_LIMIT_REASON

    try:
        reading = head_run_journal_read(protocol.run_dir_for(root, run_id))
    except (OSError, ValueError):
        return None
    if reading.truncated_tail or reading.malformed or not reading.ordered:
        return None
    for event in reversed(reading.events):
        if event.get("run_id") != run_id:
            continue
        if event.get("kind") == local_pty.RUN_STARTED:
            return None
        if event.get("kind") != local_pty.RUN_EXITED:
            continue
        if event.get("head_loss_reason") == MEMORY_LIMIT_REASON and event.get("signal") == 9:
            return MEMORY_LIMIT_REASON
        return None
    return None


def head_run_end(
    root: str | os.PathLike[str], run_id: str, *, heartbeat_state: str, launched_boot: str
) -> RunEnd | None:
    """`run_end` for the run `run_id` names, over its own journal under `root`.

    A journal that is missing, unreadable, malformed or out of order contributes no events.
    """
    events: tuple[dict[str, Any], ...] = ()
    if run_id:
        try:
            reading = head_run_journal_read(protocol.run_dir_for(root, run_id))
        except OSError:
            reading = None
        if reading is not None and not reading.malformed and reading.ordered:
            events = tuple(event for event in reading.events if event.get("run_id") == run_id)
    return run_end(events, heartbeat_state=heartbeat_state, launched_boot=launched_boot)


def _last_event_at(address: _Address) -> float:
    """The head's own clock, as the newest thing its journal has to say about it."""
    events = local_pty.read_tail(address.journal_path).events
    return float(events[-1].get("at") or 0.0) if events else 0.0
