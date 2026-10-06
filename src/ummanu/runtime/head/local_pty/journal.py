"""One head's event journal: versioned, append-only, readable while being written.

Every record carries `schema_version` (a running older supervisor may have written it), `seq`
(strictly increasing per run, recovered from the file on open so a takeover continues it) and
`run_id`. Records are single-line JSON, one `O_APPEND` `write()` plus `fsync` each, so `SIGKILL`
leaves at most one partial trailing line, which readers report as a truncated tail.
See docs/HEAD_RUNTIME.md "Supervisor progress journal".
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

JOURNAL_SCHEMA_VERSION = 1

#: Bytes a current-state reader reads off a journal's end. Run directories are reused across
#: incarnations, so the file grows without bound; records are under 256 bytes, so 64 KiB holds
#: several hundred, at a fixed cost per read.
JOURNAL_TAIL_BYTES = 64 * 1024

#: The head's process is up and the supervisor owns it.
RUN_STARTED = "run.started"
#: Sealed admitted identity bound to native scope, fsynced before any head exists.
SCOPE_BOUND = "scope.bound"
#: A delivery ended: `bytes` is what the pty took, `offered_bytes` what the caller handed over,
#: `complete` whether they match. Written on arrival, not admission; a payload refused at admission
#: is not an event.
INPUT_ACCEPTED = "input.accepted"
#: A turn opened — the first accepted input since the head last went quiet.
TURN_STARTED = "turn.started"
#: The head's modeled screen showed a normalized line not yet seen in this turn. Repeated windows are
#: folded into the next record's `output_bytes`, with their number in `folded_windows`.
PROVIDER_PROGRESSED = "provider.progressed"
#: The open turn's head went quiet for the configured settle time. Any still-pending fold count is
#: carried here as `folded_windows`.
TURN_FINISHED = "turn.finished"
#: Admission closed: this supervisor takes no further input for this head.
DRAIN_REQUESTED = "drain.requested"
#: A stop was asked for, by a client or by a signal to the supervisor itself.
RUN_STOPPING = "run.stopping"
#: The head process — not the supervisor — ended, with its exit code or its signal.
RUN_EXITED = "run.exited"

EVENT_KINDS = (
    SCOPE_BOUND,
    RUN_STARTED,
    INPUT_ACCEPTED,
    TURN_STARTED,
    PROVIDER_PROGRESSED,
    TURN_FINISHED,
    DRAIN_REQUESTED,
    RUN_STOPPING,
    RUN_EXITED,
)

_RESERVED_FIELDS = frozenset({"schema_version", "seq", "run_id", "kind", "at"})


class JournalError(RuntimeError):
    """A journal that would say something untrue about a run."""


@dataclass(frozen=True)
class JournalReadResult:
    """What a reader can honestly say about a journal it just read.

    `truncated_tail`: the last line has no newline (writer died mid-write); earlier records are
    intact. `malformed`: complete lines that were not usable records, a distinct failure.
    `partial_head`: a bounded read began mid-file, so absence of a record proves nothing.
    """

    events: tuple[dict[str, Any], ...] = ()
    truncated_tail: bool = False
    malformed: int = 0
    ordered: bool = True
    partial_head: bool = False

    @property
    def kinds(self) -> tuple[str, ...]:
        return tuple(str(event.get("kind") or "") for event in self.events)

    def of_kind(self, kind: str) -> tuple[dict[str, Any], ...]:
        return tuple(event for event in self.events if event.get("kind") == kind)


@dataclass
class JournalWriter:
    """The supervisor's own end of the journal. One process writes; anybody may read."""

    path: Path
    run_id: str
    _fd: int = field(default=-1, init=False, repr=False)
    _seq: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.run_id:
            raise JournalError("a journal names the run it belongs to")
        self.path = Path(self.path)

    def open(self) -> Self:
        """Open for append, continuing the sequence already in the file."""
        self._seq = _last_seq(self.path)
        self._fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        # Close-on-exec: a head inheriting it could hold the journal open after the supervisor dies.
        os.set_inheritable(self._fd, False)
        return self

    def fileno(self) -> int:
        """The append descriptor, for a caller that has to reason about inheritance."""
        return self._fd

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    @property
    def seq(self) -> int:
        """The sequence number of the last record this writer appended."""
        return self._seq

    def append(self, kind: str, **fields: Any) -> dict[str, Any]:
        """Append one record and return exactly what was written.

        Unknown kinds raise: the event vocabulary is closed so readers can route on it.
        """
        if kind not in EVENT_KINDS:
            known = ", ".join(EVENT_KINDS)
            raise JournalError(f"unknown journal event kind {kind!r} (known: {known})")
        collisions = _RESERVED_FIELDS.intersection(fields)
        if collisions:
            raise JournalError(
                f"a journal record's own fields cannot be overwritten: {', '.join(sorted(collisions))}"
            )
        if self._fd < 0:
            raise JournalError("the journal is not open")
        self._seq += 1
        record: dict[str, Any] = {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "seq": self._seq,
            "run_id": self.run_id,
            "kind": kind,
            "at": round(time.time(), 6),
        }
        record.update(fields)
        line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        os.write(self._fd, line.encode("utf-8"))
        os.fsync(self._fd)
        return record

    def __enter__(self) -> Self:
        return self.open()

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _last_seq(path: Path) -> int:
    try:
        result = read_events(path)
    except OSError:
        return 0
    return max((int(event.get("seq") or 0) for event in result.events), default=0)


def _usable(record: Any) -> dict[str, Any] | None:
    if not isinstance(record, dict):
        return None
    if record.get("schema_version") != JOURNAL_SCHEMA_VERSION:
        return None
    if record.get("kind") not in EVENT_KINDS:
        return None
    if not str(record.get("run_id") or ""):
        return None
    try:
        seq = int(record["seq"])
    except (KeyError, TypeError, ValueError, OverflowError):
        # `OverflowError` is `int(inf)` from a bare `Infinity`: a malformed record, not an exception.
        return None
    if seq <= 0:
        return None
    record["seq"] = seq
    return record


def read_events(path: str | os.PathLike[str]) -> JournalReadResult:
    """Read a whole journal (live or dead writer) without trusting its last line.

    For current state use the bounded `read_tail`. A missing file reads as an empty journal.
    """
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        return JournalReadResult()
    return _parsed(raw, partial_head=False)


def read_tail(path: str | os.PathLike[str], *, max_bytes: int = JOURNAL_TAIL_BYTES) -> JournalReadResult:
    """Read at most `max_bytes` off a journal's end, reporting `partial_head` when bounded.

    The first line of a window not starting at the file's beginning is a cut record and is dropped
    (it must not count as `malformed`). The bound is on bytes read: the window is the last
    `min(max_bytes, size)` bytes of the size as measured, so records appended concurrently belong
    to the next read. A missing file reads as an empty journal.
    """
    window = tail_window(path, max_bytes=max_bytes)
    if window is None:
        return JournalReadResult()
    raw, partial_head = window
    return _parsed(raw, partial_head=partial_head)


def tail_window(
    path: str | os.PathLike[str], *, max_bytes: int = JOURNAL_TAIL_BYTES
) -> tuple[bytes, bool] | None:
    """The raw bytes `read_tail` parses, and whether the window began mid-history.

    `None` for a missing file; a mid-history window has its cut first line dropped. Exposed for
    readers that must judge raw record values rather than `read_tail`'s coerced ones (vitality).
    """
    if max_bytes <= 0:
        raise JournalError("a bounded journal read is bounded by a positive number of bytes")
    try:
        with open(path, "rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            start = max(0, size - max_bytes)
            handle.seek(start)
            raw = handle.read(min(max_bytes, max(0, size - start)))
    except FileNotFoundError:
        return None
    if start <= 0:
        return raw, False
    cut = raw.find(b"\n")
    return (b"" if cut < 0 else raw[cut + 1 :]), True


def _parsed(raw: bytes, *, partial_head: bool) -> JournalReadResult:
    """Turn journal bytes into the records a reader may believe, and say what was wrong with them."""
    if not raw:
        return JournalReadResult(partial_head=partial_head)
    truncated = not raw.endswith(b"\n")
    lines = raw.split(b"\n")
    if truncated:
        lines = lines[:-1]
    events: list[dict[str, Any]] = []
    malformed = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            parsed = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            malformed += 1
            continue
        record = _usable(parsed)
        if record is None:
            malformed += 1
            continue
        events.append(record)
    ordered = all(
        int(later["seq"]) > int(earlier["seq"]) for earlier, later in zip(events, events[1:], strict=False)
    )
    return JournalReadResult(
        events=tuple(events),
        truncated_tail=truncated,
        malformed=malformed,
        ordered=ordered,
        partial_head=partial_head,
    )


def events_since(events: Sequence[Mapping[str, Any]], seq: int) -> tuple[dict[str, Any], ...]:
    """The records a reader holding `seq` has not seen yet."""
    return tuple(dict(event) for event in events if int(event.get("seq") or 0) > seq)
