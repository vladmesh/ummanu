"""One head's versioned event journal: bounded retention, readable while being written.

Every record carries `schema_version` (a running older supervisor may have written it), `seq`
(strictly increasing per run, recovered from the file on open so a takeover continues it) and
`run_id`. Records are single-line JSON, one `O_APPEND` `write()` plus `fsync` each, so `SIGKILL`
leaves at most one partial trailing line, which readers report as a truncated tail.
See docs/HEAD_RUNTIME.md "Supervisor progress journal".
"""

from __future__ import annotations

import json
import os
import stat
import time
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

JOURNAL_SCHEMA_VERSION = 1

#: Current-state read window. Commands and scope proofs can be much larger than this window.
JOURNAL_TAIL_BYTES = 64 * 1024
#: Encoded JSON including newline; refusal happens before any sequence or disk mutation.
JOURNAL_RECORD_BYTES = 1024 * 1024
#: Six original anchors plus a recent suffix; no archives or independently trusted sidecar.
JOURNAL_ANCHOR_BYTES = 6 * JOURNAL_RECORD_BYTES
JOURNAL_RECENT_BYTES = 1024 * 1024
JOURNAL_RETAIN_BYTES = JOURNAL_ANCHOR_BYTES + JOURNAL_RECENT_BYTES
JOURNAL_MAX_BYTES = JOURNAL_RETAIN_BYTES + JOURNAL_RECORD_BYTES
JOURNAL_TRANSIENT_BYTES = JOURNAL_MAX_BYTES + JOURNAL_RETAIN_BYTES

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
    `partial_head`: a bounded read or retention left prefix/interior gaps; absence proves nothing.
    """

    events: tuple[dict[str, Any], ...] = ()
    truncated_tail: bool = False
    malformed: int = 0
    ordered: bool = True
    partial_head: bool = False

    def covers_since(self, seq: int) -> bool:
        """No missing sequence above a delivery floor, including interior retention gaps."""
        if self.truncated_tail or self.malformed or not self.ordered:
            return False
        fresh = [event["seq"] for event in self.events if event["seq"] > seq]
        reaches_floor = bool(self.events and self.events[-1]["seq"] == seq)
        return bool(fresh or reaches_floor or not self.partial_head) and all(
            value == seq + index for index, value in enumerate(fresh, start=1)
        )

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
    _directory: int = field(default=-1, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.run_id:
            raise JournalError("a journal names the run it belongs to")
        self.path = Path(self.path)

    def open(self) -> Self:
        """Open for append, continuing the sequence already in the file."""
        if self._fd >= 0:
            return self
        if self.path.parent.resolve() != self.path.parent.absolute():
            raise JournalError("journal directory is not canonical")
        self._directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            self._fd = self._open_file(self.path.name, os.O_RDWR | os.O_CREAT | os.O_APPEND)
            os.set_inheritable(self._fd, False)
            retained, self._seq = _retained(self._fd, self.run_id)
            self._recover_staging()
            if os.fstat(self._fd).st_size > JOURNAL_MAX_BYTES:
                self._publish(retained)
            os.fsync(self._directory)
        except BaseException:
            self.close()
            raise
        return self

    def _open_file(self, name: str, flags: int) -> int:
        descriptor = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600,
                             dir_fd=self._directory)
        try:
            _owned(os.fstat(descriptor))
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    def _canonical(self) -> None:
        actual = os.stat(self.path.name, dir_fd=self._directory, follow_symlinks=False)
        opened = os.fstat(self._fd)
        if (actual.st_dev, actual.st_ino) != (opened.st_dev, opened.st_ino):
            raise JournalError("journal pathname was substituted")
        _owned(actual)

    @property
    def _staging(self) -> str:
        return self.path.name + ".retaining"

    def _recover_staging(self) -> None:
        """The exclusive supervisor owner may discard its interrupted, unpublished staging file."""
        try:
            info = os.stat(self._staging, dir_fd=self._directory, follow_symlinks=False)
        except FileNotFoundError:
            return
        _owned(info)
        if info.st_size > JOURNAL_RETAIN_BYTES:
            raise JournalError("journal staging exceeds its owned bound")
        os.unlink(self._staging, dir_fd=self._directory)
        os.fsync(self._directory)

    def _publish(self, retained: bytes) -> None:
        """Sync a bounded replacement, publish, switch descriptors, then sync the directory.

        On any failure the caller closes this writer. A reader holding the old descriptor still
        sees its original snapshot; readers opening the name see one complete retained set.
        """
        staging = self._open_file(self._staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        try:
            if os.write(staging, retained) != len(retained):
                raise JournalError("journal staging short write")
            os.fsync(staging)
            self._canonical()
            staged = os.stat(self._staging, dir_fd=self._directory, follow_symlinks=False)
            opened = os.fstat(staging)
            if (staged.st_dev, staged.st_ino) != (opened.st_dev, opened.st_ino):
                raise JournalError("journal staging pathname was substituted")
            _owned(staged)
            os.replace(self._staging, self.path.name,
                       src_dir_fd=self._directory, dst_dir_fd=self._directory)
        finally:
            os.close(staging)
        os.close(self._fd)
        self._fd = -1
        self._fd = self._open_file(self.path.name, os.O_RDWR | os.O_APPEND)
        os.set_inheritable(self._fd, False)
        os.fsync(self._directory)

    def fileno(self) -> int:
        """The append descriptor, for a caller that has to reason about inheritance."""
        return self._fd

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1
        if self._directory >= 0:
            os.close(self._directory)
            self._directory = -1

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
        record: dict[str, Any] = {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "seq": self._seq + 1,
            "run_id": self.run_id,
            "kind": kind,
            "at": round(time.time(), 6),
        }
        record.update(fields)
        line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        encoded = line.encode("utf-8")
        if len(encoded) > JOURNAL_RECORD_BYTES:
            raise JournalError("journal_record_too_large: encoded record exceeds journal limit")
        try:
            self._canonical()
            if os.fstat(self._fd).st_size + len(encoded) > JOURNAL_MAX_BYTES:
                retained, _seq = _retained(self._fd, self.run_id)
                self._publish(retained)
            if os.write(self._fd, encoded) != len(encoded):
                raise JournalError("journal record short write")
            os.fsync(self._fd)
        except BaseException:
            self.close()
            raise
        self._seq = record["seq"]
        return record

    def __enter__(self) -> Self:
        return self.open()

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _owned(info: os.stat_result) -> None:
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
        raise JournalError("journal file is not an owner-only regular file")


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    record: dict[str, Any] = {}
    for key, value in pairs:
        if key in record:
            raise ValueError("duplicate journal field")
        record[key] = value
    return record


def _retained(descriptor: int, run_id: str) -> tuple[bytes, int]:
    """Stream legacy input with bounded lines/anchors/suffix; never repair damaged input.

    Keep the first start for UI metadata, and the current binding, start, drain, stopping and
    exit verbatim. A new binding begins the next scoped incarnation before its start exists.
    """
    anchors: dict[str, tuple[int, bytes]] = {}
    recent: deque[tuple[int, bytes]] = deque()
    recent_bytes = 0
    previous = 0
    scope_pending = False
    os.lseek(descriptor, 0, os.SEEK_SET)
    with os.fdopen(os.dup(descriptor), "rb") as stream:
        while line := stream.readline(JOURNAL_RECORD_BYTES + 1):
            if len(line) > JOURNAL_RECORD_BYTES:
                raise JournalError("journal_record_too_large: existing record exceeds journal limit")
            if not line.endswith(b"\n"):
                raise JournalError("existing journal has a torn tail")
            if not line.strip():
                continue  # Released schema-1 readers allow complete blank separators.
            try:
                raw = json.loads(line, object_pairs_hook=_unique)
            except (ValueError, UnicodeDecodeError) as exc:
                raise JournalError("existing journal is damaged") from exc
            if (not isinstance(raw, dict)
                    or type(raw.get("seq")) is not int
                    or type(raw.get("schema_version")) is not int or _usable(raw) is None
                    or raw["seq"] <= previous
                    or raw["run_id"] != run_id):
                raise JournalError("existing journal is damaged or belongs to a different run")
            seq, kind = raw["seq"], raw["kind"]
            previous = seq
            if kind == SCOPE_BOUND:
                proof = raw.get("binding")
                if (not isinstance(proof, dict) or not isinstance(proof.get("invocation_id"), str)
                        or not proof["invocation_id"]):
                    raise JournalError("existing scope binding is malformed")
                old = anchors.get(SCOPE_BOUND)
                if (old is not None and json.loads(old[1])["binding"].get("invocation_id")
                        == proof.get("invocation_id")):
                    raise JournalError("duplicate current scope invocation")
                anchors = {key: value for key, value in anchors.items() if key == "first"}
                # A new native invocation supersedes an unstarted pre-head crash's binding too.
                # Preserve its original record as the current anchor, never synthesize proof.
                scope_pending = True
            elif kind == RUN_STARTED:
                if not scope_pending and SCOPE_BOUND in anchors:
                    raise JournalError("multiple starts borrow one scope binding")
                binding = anchors.get(SCOPE_BOUND) if scope_pending else None
                anchors = {key: value for key, value in anchors.items() if key == "first"}
                if binding is not None:
                    anchors[SCOPE_BOUND] = binding
                scope_pending = False
                anchors.setdefault("first", (seq, line))
            if kind in (SCOPE_BOUND, RUN_STARTED, DRAIN_REQUESTED, RUN_STOPPING, RUN_EXITED):
                anchors[kind] = (seq, line)
            recent.append((seq, line))
            recent_bytes += len(line)
            while recent_bytes > JOURNAL_RECENT_BYTES:
                recent_bytes -= len(recent.popleft()[1])
    kept = dict(recent)
    kept.update(anchors.values())
    binding = anchors.get(SCOPE_BOUND)
    if binding is not None:
        # Pending invocations can interleave: A, B, A is still a duplicate of the current A.
        # Count only that final invocation in a second bounded-memory scan, rather than keeping
        # an unbounded set of all legacy invocations or letting compaction erase the duplicate.
        invocation = json.loads(binding[1])["binding"]["invocation_id"]
        matches = 0
        os.lseek(descriptor, 0, os.SEEK_SET)
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            while line := stream.readline(JOURNAL_RECORD_BYTES + 1):
                if line.strip():
                    event = json.loads(line)
                    if (event["kind"] == SCOPE_BOUND
                            and event["binding"]["invocation_id"] == invocation):
                        matches += 1
                        if matches > 1:
                            raise JournalError("duplicate current scope invocation")
    return b"".join(kept[seq] for seq in sorted(kept)), previous


def _usable(record: Any) -> dict[str, Any] | None:
    if not isinstance(record, dict):
        return None
    if (type(record.get("schema_version")) is not int
            or record.get("schema_version") != JOURNAL_SCHEMA_VERSION):
        return None
    if record.get("kind") not in EVENT_KINDS:
        return None
    if not isinstance(record.get("run_id"), str) or not record["run_id"]:
        return None
    seq = record.get("seq")
    if type(seq) is not int or seq <= 0:
        return None
    return record


def read_events(path: str | os.PathLike[str]) -> JournalReadResult:
    """Read the retained set (or a released unrotated journal) without trusting its last line.

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
    readers that must validate additional raw typed fields (vitality and scope ownership).
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
        return raw, _parsed(raw, partial_head=False).partial_head
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
            parsed = json.loads(line.decode("utf-8"), object_pairs_hook=_unique)
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
        partial_head=partial_head or bool(events and events[0]["seq"] != 1) or any(
            later["seq"] != earlier["seq"] + 1
            for earlier, later in zip(events, events[1:], strict=False)
        ),
    )


def events_since(events: Sequence[Mapping[str, Any]], seq: int) -> tuple[dict[str, Any], ...]:
    """The records a reader holding `seq` has not seen yet."""
    return tuple(dict(event) for event in events if int(event.get("seq") or 0) > seq)
