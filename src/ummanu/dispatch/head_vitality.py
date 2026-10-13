"""Pure, run-bound vitality observation: Process, Turn and Progress axes.

Convert individual channel readings into snapshots; this module does not make
recovery decisions. Unknown, absent or foreign evidence must not imply progress.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ummanu.dispatch.watchdog import (
    HEARTBEAT_DEAD,
    HEARTBEAT_IDENTITY_MISMATCH,
    HEARTBEAT_LIVE_MATCH,
)

# Serialisation version of ``VitalitySnapshot``. The next card persists episodes keyed by this
# vocabulary, so a schema change later must bump this and answer for old payloads explicitly
# rather than reinterpreting them silently.
SNAPSHOT_VERSION = 1

# Reasons travel in dispatcher records and operator output. Longer than any diagnostic this module
# writes, short enough that one snapshot can never smuggle a transcript into durable state.
REASON_LIMIT = 240
CURSOR_LIMIT = 240
# Bound on a child's command line carried on a snapshot or an episode.
COMMAND_LIMIT = 300

# What one ``execution_child`` reading must show, summed over the descendants it can compare, to
# count as advancement rather than noise. An idle helper the head keeps alive (an MCP server's
# timers, a watcher) burns tens of milliseconds a minute and moves no bytes; a working test run
# burns a large fraction of a core or streams output. Half a CPU-second, or a quarter MiB of
# read+written bytes, between two readings sits well between the two.
CHILD_CPU_ADVANCE_MS = 500
CHILD_IO_ADVANCE_BYTES = 256 * 1024
# ``c2`` carries the whole-tree aggregate; a ``c1`` cursor from round 1 reads as no previous reading.
_CHILD_CURSOR_PREFIX = "c2:"


class HeadVitalityError(RuntimeError):
    """A snapshot was asked to say something no observation can truthfully say."""


class ProcessState(StrEnum):
    """Whether the kernel still has a process behind the run's launch identity."""

    RUNNING = "running"
    SUSPENDED = "suspended"
    DEAD = "dead"
    UNKNOWN = "unknown"


class TurnState(StrEnum):
    """Whether a conversational turn is believed to be in flight right now."""

    ACTIVE = "active"
    IDLE = "idle"
    UNKNOWN = "unknown"


class ProgressState(StrEnum):
    """Whether the work itself is seen to move, as of one instant."""

    ADVANCING = "advancing"
    QUIET = "quiet"
    UNKNOWN = "unknown"


class SourceAvailability(StrEnum):
    """Whether the observing channel answered at all.

    ``UNAVAILABLE`` is a statement about the channel, never about the head: a missing pid file, a
    refused pane probe or an unreadable journal freezes the observer's knowledge instead of voting
    for a stall.
    """

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class SnapshotSource(StrEnum):
    """Which channel produced a snapshot.

    One snapshot comes from one channel. Fusing channels is the reducer's job, and keeping the
    source name on the snapshot is what lets the reducer weight them differently (an advisory pane
    reading and a pid heartbeat are not equally authoritative about anything).
    """

    PID_HEARTBEAT = "pid_heartbeat"
    PROVIDER_CURSOR = "provider_cursor"
    PANE_ADVISORY = "pane_advisory"
    # The head's own child processes, read from /proc (secretary-1692): a head blocked on a long
    # foreground command is silent everywhere else while its child works.
    EXECUTION_CHILD = "execution_child"
    # The head's own local-pty supervisor journal (secretary-1739): the Turn axis, and Progress
    # from `provider.progressed`, which since secretary-1738 is written only for new screen content.
    SUPERVISOR_JOURNAL = "supervisor_journal"


# Reducer/guard safety vocabulary, deliberately provider-only. Child and journal
# advancement have separate clocks and must not bypass dark-source or outer ceilings.
PROGRESS_SOURCES = frozenset({SnapshotSource.PROVIDER_CURSOR.value})


@dataclass(frozen=True)
class VitalitySnapshot:
    """One channel's reading of one head run at one instant."""

    run_id: str
    source: SnapshotSource
    observed_at: float
    availability: SourceAvailability
    process: ProcessState = ProcessState.UNKNOWN
    turn: TurnState = TurnState.UNKNOWN
    progress: ProgressState = ProgressState.UNKNOWN
    # Opaque, compared but never parsed: the reducer detects advancement per source by inequality
    # alone. ``None`` when this channel exposed no cursor or could not be read.
    cursor: str | None = None
    reason: str = ""
    # Only the ``execution_child`` source fills these: the one descendant this reading describes
    # (the busiest measured mover, else the mover on file while it lives, else the youngest live
    # descendant), as an ``m:``/``y:`` + ``pid.start`` key, its redacted bounded command line and,
    # best effort, the regular file its stdout goes to. Empty on every other source and when the
    # reading saw no live descendant.
    child_key: str = ""
    command: str = ""
    output_path: str = ""
    # Only the ``supervisor_journal`` source fills it: when the reported Turn state began, in the
    # journal's own epoch seconds (the open turn's ``turn.started``, or for an idle head the later
    # of its last ``turn.finished`` and ``input.accepted``/``turn.started``). 0.0 everywhere else.
    turn_since: float = 0.0

    def __post_init__(self) -> None:
        if not str(self.run_id or "").strip():
            raise HeadVitalityError("a vitality snapshot is bound to a HeadRun.run_id")
        if isinstance(self.observed_at, bool) or not isinstance(self.observed_at, (int, float)):
            raise HeadVitalityError("a vitality snapshot carries an epoch-seconds timestamp")
        observed_at = float(self.observed_at)
        if not math.isfinite(observed_at) or observed_at < 0:
            raise HeadVitalityError("a vitality snapshot timestamp is finite and not negative")
        # Frozen fields are normalised through ``object.__setattr__`` once, at construction, so a
        # snapshot built from a chatty diagnostic and its serialised form compare equal.
        object.__setattr__(self, "run_id", str(self.run_id))
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "cursor", None if self.cursor is None else str(self.cursor)[:CURSOR_LIMIT])
        object.__setattr__(self, "reason", str(self.reason or "")[:REASON_LIMIT])
        object.__setattr__(self, "child_key", str(self.child_key or "")[:80])
        object.__setattr__(self, "command", str(self.command or "")[:COMMAND_LIMIT])
        object.__setattr__(self, "output_path", str(self.output_path or "")[:COMMAND_LIMIT])
        if isinstance(self.turn_since, bool) or not isinstance(self.turn_since, (int, float)):
            raise HeadVitalityError("a vitality snapshot turn_since is epoch seconds")
        if not math.isfinite(float(self.turn_since)) or float(self.turn_since) < 0:
            raise HeadVitalityError("a vitality snapshot turn_since is finite and not negative")
        object.__setattr__(self, "turn_since", float(self.turn_since))

    @property
    def advisory(self) -> bool:
        """Whether this snapshot is advisory-only and can never authorise intervention."""
        return self.source is SnapshotSource.PANE_ADVISORY

    def to_json(self) -> dict[str, Any]:
        """The durable form a later episode write stores beside the run it describes."""
        return {
            "version": SNAPSHOT_VERSION,
            "run_id": self.run_id,
            "source": self.source.value,
            "observed_at": self.observed_at,
            "availability": self.availability.value,
            "process": self.process.value,
            "turn": self.turn.value,
            "progress": self.progress.value,
            "cursor": self.cursor,
            "reason": self.reason,
            "child_key": self.child_key,
            "command": self.command,
            "output_path": self.output_path,
            "turn_since": self.turn_since,
        }

    @classmethod
    def from_json(cls, payload: Any) -> VitalitySnapshot:
        """Read one snapshot back, refusing shapes that would silently change its meaning."""
        if not isinstance(payload, dict):
            raise HeadVitalityError("a vitality snapshot is read from an object")
        # ``True == 1`` in Python, so an equality check alone would read a boolean version as a
        # supported one; the version is either this exact int or the payload is not ours.
        if type(payload.get("version")) is not int or payload.get("version") != SNAPSHOT_VERSION:
            raise HeadVitalityError("vitality snapshot has an unsupported version")
        raw_observed_at = payload.get("observed_at")
        if isinstance(raw_observed_at, bool) or not isinstance(raw_observed_at, (int, float)):
            # Unlike a record that may default its claim time to now, a snapshot has no honest
            # default for "when was this observed": a damaged timestamp is refused, not zeroed.
            raise HeadVitalityError("vitality snapshot timestamp is not a number")
        observed_at = float(raw_observed_at)
        if not math.isfinite(observed_at):
            # NaN sorts neither before nor after anything and inf never ages out, so either value
            # would silently break every "how long has it been quiet" comparison downstream.
            raise HeadVitalityError("vitality snapshot timestamp is not finite")
        try:
            source = SnapshotSource(str(payload.get("source") or ""))
            availability = SourceAvailability(str(payload.get("availability") or ""))
            process = ProcessState(str(payload.get("process") or ""))
            turn = TurnState(str(payload.get("turn") or ""))
            progress = ProgressState(str(payload.get("progress") or ""))
        except ValueError:
            raise HeadVitalityError("vitality snapshot names an axis outside its vocabulary") from None
        cursor = payload.get("cursor")
        return cls(
            run_id=str(payload.get("run_id") or ""),
            source=source,
            observed_at=observed_at,
            availability=availability,
            process=process,
            turn=turn,
            progress=progress,
            cursor=None if cursor is None else str(cursor),
            reason=str(payload.get("reason") or ""),
            # Optional since secretary-1692: a payload written before them names no child.
            child_key=str(payload.get("child_key") or ""),
            command=str(payload.get("command") or ""),
            output_path=str(payload.get("output_path") or ""),
            # Optional since secretary-1739, on the same terms: absent means no journal Turn time.
            turn_since=_payload_time(payload.get("turn_since", 0.0)),
        )

    @classmethod
    def from_pid_heartbeat(cls, status: Any, *, run_id: str, observed_at: float) -> VitalitySnapshot:
        """Wrap one ``dispatch.watchdog.head_process_status`` answer."""
        if not isinstance(status, dict):
            return cls._unavailable(
                run_id=run_id,
                observed_at=observed_at,
                source=SnapshotSource.PID_HEARTBEAT,
                reason="pid heartbeat status is not an object",
            )
        state = str(status.get("state") or "")
        if state == HEARTBEAT_LIVE_MATCH:
            return cls(
                run_id=run_id,
                source=SnapshotSource.PID_HEARTBEAT,
                observed_at=observed_at,
                availability=SourceAvailability.AVAILABLE,
                process=(ProcessState.SUSPENDED if bool(status.get("stopped")) else ProcessState.RUNNING),
                reason="",
            )
        if state == HEARTBEAT_DEAD:
            return cls(
                run_id=run_id,
                source=SnapshotSource.PID_HEARTBEAT,
                observed_at=observed_at,
                availability=SourceAvailability.AVAILABLE,
                process=ProcessState.DEAD,
                reason="heartbeat names a process that is gone or unreaped",
            )
        if state == HEARTBEAT_IDENTITY_MISMATCH:
            return cls._unavailable(
                run_id=run_id,
                observed_at=observed_at,
                source=SnapshotSource.PID_HEARTBEAT,
                reason="pid heartbeat belongs to another live HeadRun",
            )
        # ``not-yet-written``, ``unreadable`` and their kin: the channel said why it cannot answer.
        return cls._unavailable(
            run_id=run_id,
            observed_at=observed_at,
            source=SnapshotSource.PID_HEARTBEAT,
            reason=f"pid heartbeat is inconclusive: {state or 'no classification'}",
        )

    @classmethod
    def from_provider_cursor(
        cls,
        evidence: Any,
        *,
        run_id: str,
        previous_cursor: str = "",
        observed_at: float,
    ) -> VitalitySnapshot:
        """Wrap one ``dispatch.tui.provider_progress_for_run`` answer against the earlier cursor."""
        if not isinstance(evidence, dict):
            return cls._unavailable(
                run_id=run_id,
                observed_at=observed_at,
                source=SnapshotSource.PROVIDER_CURSOR,
                reason="provider cursor evidence is not an object",
            )
        if (
            str(evidence.get("state") or "") != "observed"
            or str(evidence.get("admission") or "") != "accepted"
        ):
            detail = str(evidence.get("reason") or "")
            if str(evidence.get("state") or "") == "identity_mismatch":
                detail = detail or "provider cursor is bound to another HeadRun"
            return cls._unavailable(
                run_id=run_id,
                observed_at=observed_at,
                source=SnapshotSource.PROVIDER_CURSOR,
                reason=f"provider cursor is not admitted: {detail}".strip(": "),
            )
        if str(evidence.get("head_run_id") or "") != str(run_id):
            return cls._unavailable(
                run_id=run_id,
                observed_at=observed_at,
                source=SnapshotSource.PROVIDER_CURSOR,
                reason="provider cursor names a HeadRun other than the snapshot's run",
            )
        cursor = str(evidence.get("cursor") or "")
        if not cursor:
            return cls._unavailable(
                run_id=run_id,
                observed_at=observed_at,
                source=SnapshotSource.PROVIDER_CURSOR,
                reason="admitted provider cursor carries no cursor value",
            )
        if not previous_cursor:
            return cls(
                run_id=run_id,
                source=SnapshotSource.PROVIDER_CURSOR,
                observed_at=observed_at,
                availability=SourceAvailability.AVAILABLE,
                progress=ProgressState.UNKNOWN,
                cursor=cursor,
                reason="first observation of this source: no earlier cursor to compare against",
            )
        advanced = cursor[:CURSOR_LIMIT] != previous_cursor[:CURSOR_LIMIT]
        return cls(
            run_id=run_id,
            source=SnapshotSource.PROVIDER_CURSOR,
            observed_at=observed_at,
            availability=SourceAvailability.AVAILABLE,
            progress=ProgressState.ADVANCING if advanced else ProgressState.QUIET,
            cursor=cursor,
            reason="" if advanced else "provider cursor unchanged since the previous snapshot",
        )

    @classmethod
    def from_child_activity(
        cls,
        evidence: Any,
        *,
        run_id: str,
        previous_cursor: str = "",
        previous_key: str = "",
        observed_at: float,
    ) -> VitalitySnapshot:
        """Wrap one descendant-tree reading against the previous aggregate CPU/I/O cursor.

        A decrease never counts as progress; describe a live descendant even
        when counters are quiet. Child-only liveness is bounded by the reducer."""
        if not isinstance(evidence, dict) or str(evidence.get("state") or "") != "observed":
            detail = str(evidence.get("reason") or "") if isinstance(evidence, dict) else ""
            return cls._unavailable(
                run_id=run_id,
                observed_at=observed_at,
                source=SnapshotSource.EXECUTION_CHILD,
                reason=f"child processes were not read: {detail}".strip(": "),
            )
        descendants: list[dict[str, Any]] = []
        for item in evidence.get("descendants") or ():
            if not isinstance(item, dict):
                continue
            try:
                descendants.append(
                    {
                        "pid": int(item["pid"]),
                        "start": int(item["start"]),
                        "cpu_ms": max(0, int(item.get("cpu_ms") or 0)),
                        "io": max(0, int(item.get("io") or 0)),
                        "command": str(item.get("command") or ""),
                        "output": str(item.get("output") or ""),
                    }
                )
            except (KeyError, TypeError, ValueError):
                continue
        uptime = _non_negative_int(evidence.get("uptime_ticks"))
        # A producer that reports no aggregate (a hand-built reading) is measured over what it
        # listed; ``read_head_children`` always reports one over the whole tree.
        total_cpu = (
            _non_negative_int(evidence.get("total_cpu_ms"))
            if "total_cpu_ms" in evidence
            else sum(item["cpu_ms"] for item in descendants)
        )
        total_io = (
            _non_negative_int(evidence.get("total_io"))
            if "total_io" in evidence
            else sum(item["io"] for item in descendants)
        )
        cursor = _child_cursor(uptime, total_cpu, total_io, descendants)
        previous = _parse_child_cursor(previous_cursor)

        def key(item: dict[str, Any], marker: str) -> str:
            return f"{marker}:{item['pid']}.{item['start']}"

        def reading(
            progress: ProgressState, reason: str, item: dict[str, Any] | None, marker: str
        ) -> VitalitySnapshot:
            return cls(
                run_id=run_id,
                source=SnapshotSource.EXECUTION_CHILD,
                observed_at=observed_at,
                availability=SourceAvailability.AVAILABLE,
                progress=progress,
                cursor=cursor,
                reason=reason,
                child_key="" if item is None else key(item, marker),
                command="" if item is None else item["command"],
                output_path="" if item is None else item["output"],
            )

        def settled() -> tuple[dict[str, Any] | None, str]:
            """No mover this reading: the mover on file while it lives, else the youngest."""
            if previous_key.startswith("m:"):
                kept = next((item for item in descendants if key(item, "m") == previous_key), None)
                if kept is not None:
                    return kept, "m"
            youngest = max(descendants, key=lambda item: (item["start"], item["pid"]), default=None)
            return youngest, "y"

        if not descendants and not evidence.get("descendant_count"):
            return reading(ProgressState.QUIET, "the head has no live child process", None, "y")
        if previous is None:
            item, marker = settled()
            return reading(
                ProgressState.UNKNOWN,
                "first observation of this source: no earlier cursor to compare against",
                item,
                marker,
            )
        previous_uptime, previous_cpu, previous_io, counters = previous
        cpu_delta = max(0, total_cpu - previous_cpu)
        io_delta = max(0, total_io - previous_io)
        advanced = cpu_delta >= CHILD_CPU_ADVANCE_MS or io_delta >= CHILD_IO_ADVANCE_BYTES
        if not advanced:
            item, marker = settled()
            return reading(
                ProgressState.QUIET,
                f"child processes moved {cpu_delta}ms cpu and {io_delta} bytes since the previous reading",
                item,
                marker,
            )
        mover: dict[str, Any] | None = None
        best: tuple[int, int] = (0, 0)
        for item in descendants:
            known = counters.get((item["pid"], item["start"]))
            if known is not None:
                moved = (max(0, item["cpu_ms"] - known[0]), max(0, item["io"] - known[1]))
            elif previous_uptime and item["start"] > previous_uptime:
                moved = (item["cpu_ms"], item["io"])
            else:
                continue
            if moved > best:
                best, mover = moved, item
        if mover is None:
            # The tree moved but no described process can be named as the one that did.
            item, marker = settled()
            return reading(ProgressState.ADVANCING, "", item, marker)
        return reading(ProgressState.ADVANCING, "", mover, "m")

    @classmethod
    def from_supervisor_journal(
        cls,
        evidence: Any,
        *,
        run_id: str,
        previous_cursor: str = "",
        observed_at: float,
    ) -> VitalitySnapshot:
        """Wrap one head-turn journal reading against its previous cursor.

        Preserve run identity and distinguish a completed turn from progress."""
        reading, refusal = _journal_reading(evidence, run_id=run_id, observed_at=observed_at)
        if reading is None:
            return cls._unavailable(
                run_id=run_id,
                observed_at=observed_at,
                source=SnapshotSource.SUPERVISOR_JOURNAL,
                reason=refusal,
            )
        turn, since, progress_seq = reading
        cursor = f"{_JOURNAL_CURSOR_PREFIX}{progress_seq}"
        previous = _parse_journal_cursor(previous_cursor)
        if previous is not None and progress_seq < previous:
            return cls._unavailable(
                run_id=run_id,
                observed_at=observed_at,
                source=SnapshotSource.SUPERVISOR_JOURNAL,
                reason="supervisor journal progress went backwards since the previous reading",
            )
        if previous is None:
            progress = ProgressState.UNKNOWN
            reason = "first observation of this source: no earlier cursor to compare against"
        elif progress_seq > previous:
            progress, reason = ProgressState.ADVANCING, ""
        else:
            progress = ProgressState.QUIET
            reason = "no new provider.progressed since the previous reading"
        return cls(
            run_id=run_id,
            source=SnapshotSource.SUPERVISOR_JOURNAL,
            observed_at=observed_at,
            availability=SourceAvailability.AVAILABLE,
            turn=turn,
            progress=progress,
            cursor=cursor,
            reason=reason,
            turn_since=since,
        )

    @classmethod
    def from_pane_readiness(cls, status: Any, *, run_id: str, observed_at: float) -> VitalitySnapshot:
        """Wrap a legacy pane readiness reading, without treating it as authoritative progress."""
        idle = status.get("idle") if isinstance(status, dict) else None
        if isinstance(idle, bool):
            turn = TurnState.IDLE if idle else TurnState.ACTIVE
            reason = ""
        else:
            turn = TurnState.UNKNOWN
            detail = (
                str(status.get("reason") or "").strip()[:REASON_LIMIT] if isinstance(status, dict) else ""
            )
            reason = f"pane readiness did not answer: {detail}" if detail else "pane readiness did not answer"
        return cls(
            run_id=run_id,
            source=SnapshotSource.PANE_ADVISORY,
            observed_at=observed_at,
            availability=SourceAvailability.AVAILABLE
            if isinstance(idle, bool)
            else SourceAvailability.UNAVAILABLE,
            turn=turn,
            reason=reason,
        )

    @classmethod
    def _unavailable(
        cls, *, run_id: str, observed_at: float, source: SnapshotSource, reason: str
    ) -> VitalitySnapshot:
        """The one shape every failed observation takes, so consumers learn it once."""
        return cls(
            run_id=run_id,
            source=source,
            observed_at=observed_at,
            availability=SourceAvailability.UNAVAILABLE,
            reason=reason[:REASON_LIMIT],
        )


def _payload_time(value: Any) -> float:
    """An optional stored timestamp: the value when it is one, refused like any damaged field."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HeadVitalityError("vitality snapshot turn_since is not a number")
    try:
        stamp = float(value)
    except OverflowError:
        raise HeadVitalityError("vitality snapshot turn_since is not finite") from None
    if not math.isfinite(stamp) or stamp < 0:
        raise HeadVitalityError("vitality snapshot turn_since is not finite")
    return stamp


# ``j1`` names this source's cursor format: the sequence of the last ``provider.progressed``.
_JOURNAL_CURSOR_PREFIX = "j1:"
# How far ahead of the observer's clock a journal time may be and still be believed. The journal
# and the dispatcher share one host clock, so this only absorbs the read landing between two
# ``time.time()`` calls; a record from further in the future is damaged, not early.
JOURNAL_CLOCK_SKEW = 60.0
# The largest sequence a reading may carry (the writer counts from 1 in steps of one).
_JOURNAL_SEQ_LIMIT = 2**53


def _journal_reading(
    evidence: Any, *, run_id: str, observed_at: float
) -> tuple[tuple[TurnState, float, int] | None, str]:
    """The one normaliser for a supervisor journal reading: ``(turn, since, progress_seq)``."""
    if not isinstance(evidence, dict):
        return None, "supervisor journal reading is not an object"
    state = evidence.get("state")
    if state != "observed":
        detail = evidence.get("reason")
        detail = detail if isinstance(detail, str) else ""
        return None, f"supervisor journal did not answer: {detail}".strip(": ")[:REASON_LIMIT]
    if not isinstance(evidence.get("run_id"), str) or evidence.get("run_id") != str(run_id):
        return None, "supervisor journal reading names a HeadRun other than the snapshot's run"
    words = {"active": TurnState.ACTIVE, "idle": TurnState.IDLE}
    raw_turn = evidence.get("turn")
    turn = words.get(raw_turn) if isinstance(raw_turn, str) else None
    if turn is None:
        return None, "supervisor journal reading names no turn state"
    since = evidence.get("turn_since")
    if isinstance(since, bool) or not isinstance(since, (int, float)):
        return None, "supervisor journal turn time is not a number"
    try:
        since = float(since)
    except OverflowError:
        return None, "supervisor journal turn time is not finite"
    if not math.isfinite(since) or since <= 0 or since > float(observed_at) + JOURNAL_CLOCK_SKEW:
        return None, "supervisor journal turn time is outside the observation's clock"
    progress_seq = evidence.get("progress_seq")
    if type(progress_seq) is not int or not 0 <= progress_seq <= _JOURNAL_SEQ_LIMIT:
        return None, "supervisor journal progress sequence is not a count"
    return (turn, since, progress_seq), ""


def _parse_journal_cursor(cursor: str) -> int | None:
    """Read back a cursor this source wrote; anything else is no previous reading."""
    text = str(cursor or "")
    if not text.startswith(_JOURNAL_CURSOR_PREFIX):
        return None
    digits = text[len(_JOURNAL_CURSOR_PREFIX) :]
    if not digits.isascii() or not digits.isdigit() or len(digits) > 20:
        return None
    return int(digits)


def _non_negative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _child_cursor(uptime: int, total_cpu: int, total_io: int, descendants: list[dict[str, Any]]) -> str:
    """This source's cursor: the reading's uptime and whole-tree aggregate, then as many described
    descendants as fit, busiest first (they are what names a mover next time)."""
    cursor = f"{_CHILD_CURSOR_PREFIX}{uptime:x}:{total_cpu:x}:{total_io:x};"
    entries: list[str] = []
    for item in sorted(descendants, key=lambda item: (item["cpu_ms"], item["start"]), reverse=True):
        entry = f"{item['pid']:x}.{item['start']:x}.{item['cpu_ms']:x}.{item['io']:x}"
        if len(cursor) + len(",".join([*entries, entry])) > CURSOR_LIMIT:
            break
        entries.append(entry)
    return cursor + ",".join(entries)


def _parse_child_cursor(
    cursor: str,
) -> tuple[int, int, int, dict[tuple[int, int], tuple[int, int]]] | None:
    """Read back a cursor ``_child_cursor`` wrote; anything else is no previous reading."""
    text = str(cursor or "")
    if not text.startswith(_CHILD_CURSOR_PREFIX):
        return None
    head, _, body = text[len(_CHILD_CURSOR_PREFIX) :].partition(";")
    try:
        uptime, total_cpu, total_io = (int(part, 16) for part in head.split(":"))
        counters: dict[tuple[int, int], tuple[int, int]] = {}
        for entry in filter(None, body.split(",")):
            pid, start, cpu, io = (int(part, 16) for part in entry.split("."))
            counters[(pid, start)] = (cpu, io)
    except ValueError:
        return None
    return uptime, total_cpu, total_io, counters


def status_has_vitality_observation(status: Any) -> bool:
    """Whether any supported channel was observed, including unavailable journal answers."""
    return isinstance(status, dict) and (
        isinstance(status.get("pid_status"), dict)
        or isinstance(status.get("provider_progress"), dict)
        or "idle" in status
        or isinstance(status.get("child_activity"), dict)
        or "supervisor_journal" in status
    )


def snapshots_from_status(
    status: Any,
    *,
    run_id: str,
    previous_cursor: str = "",
    previous_child_cursor: str = "",
    previous_child_key: str = "",
    previous_journal_cursor: str = "",
    observed_at: float,
) -> list[VitalitySnapshot]:
    """Every snapshot one ``command_terminal_status`` answer supports, bound to ``run_id``."""
    if not isinstance(status, dict):
        return []
    snapshots: list[VitalitySnapshot] = []
    pid_status = status.get("pid_status")
    if isinstance(pid_status, dict):
        snapshots.append(
            VitalitySnapshot.from_pid_heartbeat(pid_status, run_id=run_id, observed_at=observed_at)
        )
    provider_progress = status.get("provider_progress")
    if isinstance(provider_progress, dict):
        snapshots.append(
            VitalitySnapshot.from_provider_cursor(
                provider_progress,
                run_id=run_id,
                previous_cursor=previous_cursor,
                observed_at=observed_at,
            )
        )
    if "idle" in status:
        snapshots.append(VitalitySnapshot.from_pane_readiness(status, run_id=run_id, observed_at=observed_at))
    child_activity = status.get("child_activity")
    if isinstance(child_activity, dict):
        snapshots.append(
            VitalitySnapshot.from_child_activity(
                child_activity,
                run_id=run_id,
                previous_cursor=previous_child_cursor,
                previous_key=previous_child_key,
                observed_at=observed_at,
            )
        )
    if "supervisor_journal" in status:
        # Present means the producer asked the journal; any shape it carries answers for itself,
        # a broken one as an unavailable channel.
        snapshots.append(
            VitalitySnapshot.from_supervisor_journal(
                status.get("supervisor_journal"),
                run_id=run_id,
                previous_cursor=previous_journal_cursor,
                observed_at=observed_at,
            )
        )
    return snapshots
