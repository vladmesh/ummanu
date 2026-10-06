"""Head-vitality observation vocabulary: three independent axes on one identity-bound snapshot.

The dispatcher's destructive history (secretary-1063, the 2026-08 incidents behind cards
codegen-orchestrator-1194..1197) came from collapsing several different questions into one boolean:
"is the head working?". This module splits that question into the three independent axes of the
head-vitality plan (`state/knowledge/plans/2026-08-21-head-vitality-runtime-orca-migration.md`,
section "Observation") and turns the signals the codebase already produces into pure data:

    Process  = Running | Suspended | Dead | Unknown      does a kernel process exist?
    Turn     = Active | Idle | Unknown                   is a turn in flight?
    Progress = Advancing | Quiet | Stagnant | Unknown    is the work moving?

Nothing here decides anything. There is no threshold, no timer and no recovery ladder in this
module: an observation reports facts, fusion forms suspicion, policy chooses intent, and only the
runtime that owns delivery can make intervention safe. Keeping every producer dumb and pure is what
lets later cards persist episodes and switch the watchdog without re-deriving these meanings at the
call site.

Invariants every consumer may rely on, and every builder below enforces:

  * **A snapshot is bound to one ``HeadRun.run_id``.** A builder refuses to attach a reading to an
    unnamed run, and a source whose own attestation names a *different* live run degrades to
    ``Unknown``/``Unavailable`` -- never to ``Dead``, because "not mine" is not evidence of death.
    A snapshot therefore never combines a new run's pid with an old run's provider cursor: each
    builder stamps exactly the run it proved.
  * **An unavailable source is not evidence of no progress.** Every failure mode maps to
    ``Unknown`` plus ``SourceAvailability.UNAVAILABLE`` with a bounded reason. In particular a
    single unchanged provider cursor is ``Quiet``, never ``Stagnant``: stagnation is a conclusion a
    reducer draws over time, not something one observation can see.
  * **Pane and terminal readings are advisory.** They fill the ``Turn`` axis alone, are stamped
    with the ``pane_advisory`` source, and can never by themselves grant a stop capability --
    readiness answers whether a pane will accept input, not whether the head behind it may be
    killed. The local-pty supervisor journal (``supervisor_journal``, secretary-1739) is not a
    pane reading: the supervisor that owns the head's pty opened and closed the turn it reports,
    so its ``Turn`` is strong evidence. It never speaks to the ``Process`` axis.

Adapters here are pure functions over plain values: the caller reads ``/proc``, the pid file, the
provider journal or the pane inventory and passes the result in. The module performs no I/O and
imports no runtime host, so a snapshot can be built in a unit test from a hand-written status dict
and the producers' shapes can drift only through a failing test.
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
    """Whether the work itself is seen to move, as of one instant.

    ``STAGNANT`` exists in the vocabulary because the reducer needs somewhere to record a
    stagnation conclusion, but no single-snapshot builder may ever produce it: one unchanged
    cursor is ``QUIET``, and calling it stagnation is the six-hour-ceiling mistake this sprint
    exists to remove.
    """

    ADVANCING = "advancing"
    QUIET = "quiet"
    STAGNANT = "stagnant"
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
    EXECUTION_RECEIPT = "execution_receipt"
    # The head's own child processes, read from /proc (secretary-1692): a head blocked on a long
    # foreground command is silent everywhere else while its child works.
    EXECUTION_CHILD = "execution_child"
    # The head's own local-pty supervisor journal (secretary-1739): the Turn axis, and Progress
    # from `provider.progressed`, which since secretary-1738 is written only for new screen content.
    SUPERVISOR_JOURNAL = "supervisor_journal"


@dataclass(frozen=True)
class VitalitySnapshot:
    """One channel's reading of one head run at one instant.

    Frozen so a snapshot can be stored on a dispatcher record, compared structurally in tests and
    handed across tick boundaries without a reader being able to edit history in place. Axes the
    producing channel cannot answer stay ``Unknown``: independence means a pid heartbeat that says
    nothing about turns fills exactly one axis, and a consumer that wanted all three filled from
    one source would be re-fusing the channels this module exists to keep apart.
    """

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
        """Read one snapshot back, refusing shapes that would silently change its meaning.

        Following ``DispatcherRecord``, a malformed payload raises instead of being repaired: a
        snapshot that came back with a renamed axis or an unbound run is damaged evidence, and
        quietly normalising it would let a later consumer act on words nobody wrote.
        """
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
        """Wrap one ``dispatch.watchdog.head_process_status`` answer.

        The heartbeat is authoritative about the ``Process`` axis and about nothing else. Its
        inconclusive states (file not yet written, unreadable, wrong version) are observations of a
        broken channel, so they arrive ``Unknown``/``Unavailable`` with the distinct state named in
        the reason rather than collapsed into one lie about the process. An identity mismatch is
        fenced the same way: some *other* live process owns that heartbeat, which proves nothing
        about this run and must never read as ``Dead``.
        """
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
        """Wrap one ``dispatch.tui.provider_progress_for_run`` answer against the earlier cursor.

        ``previous_cursor`` is the opaque cursor of this exact run's previous snapshot of this
        exact source (empty before the first observation). Only the admitted, bound evidence counts
        as an answer: an unadmitted or foreign-bound reading is a channel problem and lands on
        ``Unknown``/``Unavailable`` -- in particular it never becomes ``Quiet``, so a reducer can
        never spend stall evidence on a source that went away. Movement and stillness are the only
        two things one cursor pair can prove, and they map to ``Advancing`` and ``Quiet``;
        deciding that quiet has lasted too long belongs to the episode reducer.
        """
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
        """Wrap one ``runtime.head.children.read_head_children`` answer against the earlier cursor.

        Movement is measured over the WHOLE descendant tree (secretary-1692 round 2): the cursor
        carries the reading's aggregate ``total_cpu_ms``/``total_io`` and the reading advances
        when the aggregate grew by ``CHILD_CPU_ADVANCE_MS`` or ``CHILD_IO_ADVANCE_BYTES`` since
        the previous reading. An aggregate that went DOWN (a descendant died and nobody in the
        tree reaped it) is no advancement for that reading, never negative progress. Anything
        less, including a live child whose counters are frozen, is ``Quiet``. The cursor is this
        source's own format, parsed only here.

        Only the described metadata is bounded: the cursor also keeps ``pid.start.cpu.io`` for
        as many described descendants as fit, which is how a reading names the busiest mover.
        The described descendant is that mover when this reading advanced (key ``m:...``), else
        the mover already on file while it lives, else the YOUNGEST live descendant (key
        ``y:...``) -- the likeliest foreground tool command, as opposed to helpers started with
        the session. So any reading that saw a live descendant describes one. How long child
        advancement may hold off a stall is the reducer's question
        (``VitalityThresholds.child_activity_ceiling``), not this one's.
        """
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
        """Wrap one ``local_pty_head.head_run_turn_reading`` answer against the earlier cursor.

        Not advisory: the supervisor that owns the head's pty wrote this journal, and a turn it
        opened on delivery and closed on quiet is the Turn axis a local-pty head had no other
        source for. ``Turn`` is ``Active`` for an open turn and ``Idle`` for a closed one, with
        ``turn_since`` saying since when. ``Progress`` compares the last ``provider.progressed``
        sequence with this run's previous reading: a new one is ``Advancing``, the same one
        ``Quiet``, and the first reading records it without an opinion.

        Every value is normalised once, here (``_journal_reading``): a reading that is not
        ``observed``, names another run, or carries a value its producer could not have written
        is ``Unknown``/``Unavailable`` with a bounded reason. None of them is stall evidence, and
        none of them is ``Dead`` -- this source never speaks to the Process axis.
        """
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
        """Wrap one pane readiness answer (a status carrying `{"idle": bool}`).

        No dispatcher status carries one since secretary-1723 removed the pane path from
        ``command_terminal_status``; the snapshot stays readable for a persisted status that does.

        Advisory by construction: the session manager answers whether a pane will take input, which
        speaks to the ``Turn`` axis alone. A busy pane is a head possibly mid-turn, an idle pane is
        a head between turns, and neither reading says anything about the process or the work --
        which is why this source can never, alone, ground a stop decision. An unanswerable probe
        (no status object, no usable flag) is ``Unknown`` and still advisory, carrying the
        producer's own bounded refusal diagnostic in ``reason`` when it gave one -- a
        deterministic launch failure such as ``terminal_split_source_not_found`` must stay
        readable on the snapshot, because a recovery policy keys on exactly that class.
        """
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
    """The one normaliser for a supervisor journal reading: ``(turn, since, progress_seq)``.

    Returns ``(None, reason)`` for anything a caller must not act on: not an object, not
    observed, another run, an unknown turn word, a time that is not a finite positive number no
    later than the observation (plus ``JOURNAL_CLOCK_SKEW``), and a sequence that is not an int in
    ``[0, 2**53]``. ``bool`` is refused wherever a number is expected, because ``True == 1``.
    """
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
    """Every snapshot one ``command_terminal_status`` answer supports, bound to ``run_id``.

    The dispatcher's wait tick and any read-only observer of the same head must see the same
    evidence, so the mapping from that status shape to this vocabulary lives here rather than at
    each call site: a channel the status did not carry produces no snapshot at all, which is not
    the same as a snapshot that reports its channel unavailable. An empty list therefore means
    "nothing was observed" -- the noop host, a runtime that answered nothing -- and a caller may
    not read it as evidence about the head.

    Pure, like every other builder in this module: the caller has already made whatever host call
    produced ``status``.
    """
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
