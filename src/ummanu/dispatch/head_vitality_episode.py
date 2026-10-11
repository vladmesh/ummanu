"""Pure reducer of run-bound vitality snapshots into a persisted episode.

The episode preserves verdict, source evidence, cursors and quiet-time history
across ticks. The caller supplies time, retention and answer-owed context;
reduction neither performs I/O nor authorizes destructive action."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from ummanu.dispatch.head_vitality import (
    COMMAND_LIMIT,
    CURSOR_LIMIT,
    REASON_LIMIT,
    HeadVitalityError,
    ProcessState,
    ProgressState,
    SnapshotSource,
    SourceAvailability,
    TurnState,
    VitalitySnapshot,
)
from ummanu.dispatch.watchdog import IDLE_STALL_DEFAULT

# Serialisation version of ``VitalityEpisode``. Strictly coupled to SNAPSHOT_VERSION's discipline:
# a schema change bumps this and answers for old payloads explicitly instead of reinterpreting
# them silently.
EPISODE_VERSION = 1

# ``basis`` is structured provenance, not prose: fixed tokens naming the source and the fact that
# drove the verdict, bounded so one noisy batch cannot bloat a durable record.
BASIS_TOKEN_LIMIT = 80
BASIS_ENTRY_LIMIT = 10

# Progress sources distinguish a broken observed channel from pid-only aging.
_PROGRESS_SOURCES = frozenset({SnapshotSource.PROVIDER_CURSOR.value})

# How long a *dark* progress source may freeze an episode's stall clock before the episode ages
# anyway (secretary-1543). See ``VitalityThresholds.dark_ceiling`` for why this number.
DARK_CEILING_DEFAULT = 2.0 * float(IDLE_STALL_DEFAULT)

# How long advancement seen ONLY in the head's child processes may hold off a stall
# (secretary-1692). See ``VitalityThresholds.child_activity_ceiling`` for why this number.
CHILD_ACTIVITY_CEILING_DEFAULT = 45.0 * 60.0

# The idle-turn stall (secretary-1739): how long a head whose supervisor journal shows its turn
# ENDED may sit at its prompt, while it still owes the dispatcher its answer, before it is
# suspected and then confirmed stalled. Both are measured from the same instant, the start of the
# idle turn. See ``VitalityThresholds.idle_turn_suspect_after`` for why these numbers.
IDLE_TURN_SUSPECT_DEFAULT = float(IDLE_STALL_DEFAULT)
IDLE_TURN_CONFIRM_DEFAULT = 2.0 * float(IDLE_STALL_DEFAULT)

# The head adapters (``HeadRun.spec.adapter``) for which a closed journal turn was verified to mean
# "at its prompt" (secretary-1739 round 2). The supervisor closes a turn after two seconds with no
# pty output at all, which proves nothing about a foreground command: a head whose child works
# silently would close its turn too. The Claude Code and Codex TUIs redraw a working indicator
# while a tool runs, and their production journals show turns open through silent broad runs of
# three to five minutes (runs f4b46634…, 74bdb481…, dc3dfbd1…). Any other adapter, or none, is
# outside the idle-turn rule and keeps the child hold and the open-turn ladder.
IDLE_TURN_ADAPTERS = frozenset({"claude", "codex"})


class VitalityVerdict(StrEnum):
    """What the reducer concludes about one run, as of one reduction."""

    HEALTHY_ACTIVE = "healthy_active"
    HEALTHY_QUIET = "healthy_quiet"
    SUSPENDED = "suspended"
    RETAINED = "retained"
    SUSPECTED_STALL = "suspected_stall"
    CONFIRMED_STALL = "confirmed_stall"
    DEAD = "dead"
    UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True)
class VitalityThresholds:
    """Quiet durations for suspected/confirmed stall, dark-source aging and child activity.

    Dark sources cannot postpone suspicion indefinitely; child-only movement
    cannot keep a silent head healthy without a bound. Destructive permission
    belongs to the separate guard."""

    suspect_after: float
    confirm_after: float
    dark_ceiling: float = float(DARK_CEILING_DEFAULT)
    child_activity_ceiling: float = float(CHILD_ACTIVITY_CEILING_DEFAULT)
    idle_turn_suspect_after: float = float(IDLE_TURN_SUSPECT_DEFAULT)
    idle_turn_confirm_after: float = float(IDLE_TURN_CONFIRM_DEFAULT)

    def __post_init__(self) -> None:
        for name in (
            "suspect_after",
            "confirm_after",
            "dark_ceiling",
            "child_activity_ceiling",
            "idle_turn_suspect_after",
            "idle_turn_confirm_after",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise HeadVitalityError(f"vitality threshold {name} is a number")
            if not math.isfinite(float(value)) or float(value) <= 0:
                raise HeadVitalityError(f"vitality threshold {name} is positive and finite")
        object.__setattr__(self, "suspect_after", float(self.suspect_after))
        object.__setattr__(self, "confirm_after", float(self.confirm_after))
        object.__setattr__(self, "dark_ceiling", float(self.dark_ceiling))
        object.__setattr__(self, "child_activity_ceiling", float(self.child_activity_ceiling))
        object.__setattr__(self, "idle_turn_suspect_after", float(self.idle_turn_suspect_after))
        object.__setattr__(self, "idle_turn_confirm_after", float(self.idle_turn_confirm_after))
        if self.idle_turn_confirm_after < self.idle_turn_suspect_after:
            raise HeadVitalityError("the idle-turn confirmation does not come before its suspicion")


DEFAULT_VITALITY_THRESHOLDS = VitalityThresholds(
    suspect_after=float(IDLE_STALL_DEFAULT),
    confirm_after=2.0 * float(IDLE_STALL_DEFAULT),
    dark_ceiling=float(DARK_CEILING_DEFAULT),
    child_activity_ceiling=float(CHILD_ACTIVITY_CEILING_DEFAULT),
    idle_turn_suspect_after=float(IDLE_TURN_SUSPECT_DEFAULT),
    idle_turn_confirm_after=float(IDLE_TURN_CONFIRM_DEFAULT),
)


def _finite_timestamp(value: Any, name: str) -> float:
    """Validate one epoch-seconds field shared by construction and deserialisation."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HeadVitalityError(f"vitality episode {name} is a number")
    stamp = float(value)
    if not math.isfinite(stamp) or stamp < 0:
        raise HeadVitalityError(f"vitality episode {name} is finite and not negative")
    return stamp


@dataclass(frozen=True)
class VitalityEpisode:
    """One run's vitality conclusion, durable enough to survive the dispatcher."""

    run_id: str
    verdict: VitalityVerdict = VitalityVerdict.UNVERIFIABLE
    # When this episode began accumulating evidence for its run; the quiet reference before any
    # progress was ever seen.
    started_at: float = 0.0
    suspected_since: float = 0.0
    confirmed_since: float = 0.0
    # The last instant a non-advisory source showed advancement, with its source's name. 0.0 when
    # no advancement has ever been observed in this episode.
    last_progress_at: float = 0.0
    last_progress_source: str = ""
    # A conversational rung's restart of the quiet clock. The dispatcher's one report nudge asks
    # the head a question it could not have answered before being asked, so the quiet the ladder
    # charges it with begins at the prompt -- but the head's real progress history stays on file,
    # which is why this is its own stamp instead of a rewritten ``last_progress_at``. 0.0 when no
    # rung has restarted anything; the quiet reference is the later of this and last progress.
    quiet_since: float = 0.0
    # Per-source opaque cursors, compared but never parsed: the caller feeds them back as
    # ``previous_cursor`` when building the next provider snapshot, which is how one reduction
    # knows the cursor moved.
    evidence_cursors: dict[str, str] = field(default_factory=dict)
    # Sources that failed to answer, mapped to the instant they were first seen dark in this
    # episode. A source that answers again leaves this map; while it stays, its stale evidence is
    # frozen rather than spent.
    unavailable_since: dict[str, float] = field(default_factory=dict)
    # Structured provenance: which sources and axes drove the verdict, newest reduction last.
    basis: tuple[str, ...] = ()
    reason: str = ""
    # Policy-owned recovery state survives reductions; rungs are keyed by suspension span,
    # while deterministic launch refusals remain consecutive across spans.
    recovery_rung: int = 0
    recovery_span_started_at: float = 0.0
    deterministic_refusals: int = 0
    # Count of distinct advancement boundaries observed for this run. Each real progress moves the
    # epoch, so a future policy can tell "quiet since the same old work" from "quiet since fresh
    # work" without trusting wall-clock arithmetic alone.
    activity_epoch: int = 0
    updated_at: float = 0.0
    # Non-zero while the stall clocks are frozen by a suspended reading: the instant the freeze
    # began. Cleared on the first reduction that no longer sees suspension, whose reference
    # timestamps are shifted forward by the frozen span so suspended time feeds no threshold.
    stall_frozen_since: float = 0.0
    # The last advisory Turn reading this episode saw ("active"/"idle"/""), and the instant a
    # reading went active -> idle. Advisory evidence stays advisory: neither field may raise a
    # verdict on its own, and ``turn_ended_at`` is read only to NARROW a conclusion the caller's
    # own ``answer_owed_since`` already established (secretary-1543). A head that never showed an
    # active turn -- a Codex head still starting, which answers idle throughout (secretary-1542's
    # measurement) -- never stamps it.
    last_turn: str = ""
    turn_ended_at: float = 0.0
    # Child-process activity (secretary-1692). ``child_progress_at`` is the last instant the
    # head's children were accepted as advancement; it feeds the quiet reference like
    # ``quiet_since`` does, and is kept apart from ``last_progress_at`` because it is not the
    # head's own work. ``child_activity_since`` is the head's own quiet reference when the current
    # child-only streak began (0.0 when none runs): the child activity ceiling is measured from
    # it, and the head's own advancement clears it.
    child_progress_at: float = 0.0
    child_activity_since: float = 0.0
    # The last child reading's described descendant: its ``m:``/``y:pid.start`` key (measured
    # mover or youngest live descendant), redacted bounded command line and redirected output
    # file, and when it was read. Cleared by a reading that saw no live descendant; kept while
    # the child source is not observed. A respawn tells
    # the successor about this command.
    last_child_key: str = ""
    last_child_command: str = ""
    last_child_output: str = ""
    last_child_at: float = 0.0
    # The idle-turn stall (secretary-1739): the instant the head's idle turn is charged from --
    # the later of the journal's idle start, the owed answer and a nudge's restart -- when the
    # idle-turn rule decided this reduction, else 0.0. ``recovery_outlook`` reads it for the next
    # deadline, and an operator reads it as "at its prompt since".
    idle_turn_since: float = 0.0

    def __post_init__(self) -> None:
        if not str(self.run_id or "").strip():
            raise HeadVitalityError("a vitality episode is bound to a HeadRun.run_id")
        if not isinstance(self.verdict, VitalityVerdict):
            raise HeadVitalityError("a vitality episode carries a VitalityVerdict")
        for name in (
            "started_at",
            "suspected_since",
            "confirmed_since",
            "last_progress_at",
            "quiet_since",
            "updated_at",
            "stall_frozen_since",
            "turn_ended_at",
            "child_progress_at",
            "child_activity_since",
            "last_child_at",
            "idle_turn_since",
        ):
            object.__setattr__(self, name, _finite_timestamp(getattr(self, name), name))
        object.__setattr__(self, "last_child_key", str(self.last_child_key or "")[:80])
        object.__setattr__(self, "last_child_command", str(self.last_child_command or "")[:COMMAND_LIMIT])
        object.__setattr__(self, "last_child_output", str(self.last_child_output or "")[:COMMAND_LIMIT])
        if isinstance(self.recovery_rung, bool) or not isinstance(self.recovery_rung, int):
            raise HeadVitalityError("a vitality episode recovery rung is an int")
        if self.recovery_rung < 0:
            raise HeadVitalityError("a vitality episode recovery rung is not negative")
        object.__setattr__(
            self,
            "recovery_span_started_at",
            _finite_timestamp(
                getattr(self, "recovery_span_started_at", 0.0),
                "recovery_span_started_at",
            ),
        )
        refusals = getattr(self, "deterministic_refusals", 0)
        if isinstance(refusals, bool) or not isinstance(refusals, int) or refusals < 0:
            raise HeadVitalityError("a vitality episode deterministic refusal count is a non-negative int")
        if self.activity_epoch < 0:
            raise HeadVitalityError("a vitality episode activity epoch is not negative")
        object.__setattr__(
            self,
            "evidence_cursors",
            {str(name)[:80]: str(cursor)[:CURSOR_LIMIT] for name, cursor in self.evidence_cursors.items()},
        )
        object.__setattr__(
            self,
            "unavailable_since",
            {
                str(name)[:80]: _finite_timestamp(since, "unavailable_since")
                for name, since in self.unavailable_since.items()
            },
        )
        object.__setattr__(
            self, "basis", tuple(str(token)[:BASIS_TOKEN_LIMIT] for token in self.basis)[:BASIS_ENTRY_LIMIT]
        )
        object.__setattr__(self, "reason", str(self.reason or "")[:REASON_LIMIT])
        turn = str(self.last_turn or "")
        if turn not in ("", "active", "idle"):
            raise HeadVitalityError("a vitality episode last turn is active, idle or unrecorded")
        object.__setattr__(self, "last_turn", turn)

    def to_json(self) -> dict[str, Any]:
        """The durable form stored on the dispatcher record beside its run."""
        return {
            "version": EPISODE_VERSION,
            "run_id": self.run_id,
            "verdict": self.verdict.value,
            "started_at": self.started_at,
            "suspected_since": self.suspected_since,
            "confirmed_since": self.confirmed_since,
            "last_progress_at": self.last_progress_at,
            "last_progress_source": self.last_progress_source,
            "quiet_since": self.quiet_since,
            "evidence_cursors": dict(self.evidence_cursors),
            "unavailable_since": dict(self.unavailable_since),
            "basis": list(self.basis),
            "reason": self.reason,
            "recovery_rung": self.recovery_rung,
            "recovery_span_started_at": self.recovery_span_started_at,
            "deterministic_refusals": self.deterministic_refusals,
            "activity_epoch": self.activity_epoch,
            "updated_at": self.updated_at,
            "stall_frozen_since": self.stall_frozen_since,
            "last_turn": self.last_turn,
            "turn_ended_at": self.turn_ended_at,
            "child_progress_at": self.child_progress_at,
            "child_activity_since": self.child_activity_since,
            "last_child_key": self.last_child_key,
            "last_child_command": self.last_child_command,
            "last_child_output": self.last_child_output,
            "last_child_at": self.last_child_at,
            "idle_turn_since": self.idle_turn_since,
        }

    @classmethod
    def from_json(cls, payload: Any) -> VitalityEpisode:
        """Read one episode back, refusing damaged evidence instead of normalising it."""
        if not isinstance(payload, dict):
            raise HeadVitalityError("a vitality episode is read from an object")
        if type(payload.get("version")) is not int or payload.get("version") != EPISODE_VERSION:
            raise HeadVitalityError("vitality episode has an unsupported version")
        try:
            verdict = VitalityVerdict(str(payload.get("verdict") or ""))
        except ValueError:
            raise HeadVitalityError("vitality episode names a verdict outside its vocabulary") from None
        cursors = payload.get("evidence_cursors")
        unavailable = payload.get("unavailable_since")
        if not isinstance(cursors, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in cursors.items()
        ):
            raise HeadVitalityError("vitality episode evidence cursors are malformed")
        if not isinstance(unavailable, dict) or not all(isinstance(key, str) for key in unavailable):
            raise HeadVitalityError("vitality episode unavailable sources are malformed")
        basis = payload.get("basis")
        if not isinstance(basis, list) or not all(isinstance(token, str) for token in basis):
            raise HeadVitalityError("vitality episode basis is malformed")
        rung = payload.get("recovery_rung")
        epoch = payload.get("activity_epoch")
        if isinstance(rung, bool) or not isinstance(rung, int) or rung < 0:
            raise HeadVitalityError("vitality episode recovery rung is a non-negative int")
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
            raise HeadVitalityError("vitality episode activity epoch is a non-negative int")
        # Missing recovery fields mean no rung or deterministic refusal has been recorded.
        span = payload.get("recovery_span_started_at", 0.0)
        refusals = payload.get("deterministic_refusals", 0)
        if isinstance(refusals, bool) or not isinstance(refusals, int) or refusals < 0:
            raise HeadVitalityError("vitality episode deterministic refusal count is a non-negative int")
        return cls(
            run_id=str(payload.get("run_id") or ""),
            verdict=verdict,
            started_at=_finite_timestamp(payload.get("started_at"), "started_at"),
            suspected_since=_finite_timestamp(payload.get("suspected_since"), "suspected_since"),
            confirmed_since=_finite_timestamp(payload.get("confirmed_since"), "confirmed_since"),
            last_progress_at=_finite_timestamp(payload.get("last_progress_at"), "last_progress_at"),
            last_progress_source=str(payload.get("last_progress_source") or "")[:80],
            # Absent in records written before secretary-1543's nudge restart, and absent is
            # exactly what "no rung has restarted this episode's quiet clock" says, so the field
            # answers for itself rather than stranding a live dispatcher's records.
            quiet_since=_finite_timestamp(payload.get("quiet_since", 0.0), "quiet_since"),
            evidence_cursors=dict(cursors),
            unavailable_since=dict(unavailable),
            basis=tuple(basis),
            reason=str(payload.get("reason") or ""),
            recovery_rung=rung,
            recovery_span_started_at=_finite_timestamp(span, "recovery_span_started_at"),
            deterministic_refusals=refusals,
            activity_epoch=epoch,
            updated_at=_finite_timestamp(payload.get("updated_at"), "updated_at"),
            stall_frozen_since=_finite_timestamp(payload.get("stall_frozen_since"), "stall_frozen_since"),
            # Written before secretary-1543 added the Turn-transition fields: their absence means
            # no advisory turn was ever recorded for this episode, which is exactly what an
            # unstamped pair says. Refusing such a record would strand every episode a running
            # dispatcher persisted under EPISODE_VERSION 1, so the version stays where it is and
            # the missing fields answer for themselves.
            last_turn=str(payload.get("last_turn") or ""),
            turn_ended_at=_finite_timestamp(payload.get("turn_ended_at", 0.0), "turn_ended_at"),
            # Written before secretary-1692 added child activity: absent means no child was ever
            # read for this episode, which is what the empty values say. Same version, same rule
            # as the Turn fields above; a reader from before this card ignores the extra keys.
            child_progress_at=_finite_timestamp(payload.get("child_progress_at", 0.0), "child_progress_at"),
            child_activity_since=_finite_timestamp(
                payload.get("child_activity_since", 0.0), "child_activity_since"
            ),
            last_child_key=str(payload.get("last_child_key") or ""),
            last_child_command=str(payload.get("last_child_command") or ""),
            last_child_output=str(payload.get("last_child_output") or ""),
            last_child_at=_finite_timestamp(payload.get("last_child_at", 0.0), "last_child_at"),
            # Written before secretary-1739: absent means the idle-turn rule never decided.
            idle_turn_since=_finite_timestamp(payload.get("idle_turn_since", 0.0), "idle_turn_since"),
        )


def reduce_vitality(
    previous: VitalityEpisode | None,
    snapshots: Sequence[VitalitySnapshot],
    now: float,
    thresholds: VitalityThresholds = DEFAULT_VITALITY_THRESHOLDS,
    *,
    retained: bool = False,
    answer_owed_since: float = 0.0,
    adapter: str = "",
) -> VitalityEpisode:
    """Purely fold one tick of source snapshots into one run's episode.

    Duplicate source observations collapse to the latest reading. `retained`
    distinguishes dispatcher-held SIGSTOP; `answer_owed_since` begins a new
    obligation and bounds idle-turn stall evidence. No I/O or implicit clock."""
    if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(float(now)):
        raise HeadVitalityError("reduce_vitality needs a finite now")
    now = float(now)
    if not isinstance(thresholds, VitalityThresholds):
        raise HeadVitalityError("reduce_vitality needs VitalityThresholds")
    if not all(isinstance(snapshot, VitalitySnapshot) for snapshot in snapshots):
        raise HeadVitalityError("reduce_vitality reduces VitalitySnapshot values")
    if previous is not None and not isinstance(previous, VitalityEpisode):
        raise HeadVitalityError("reduce_vitality continues a VitalityEpisode")
    if not isinstance(retained, bool):
        raise HeadVitalityError("reduce_vitality needs a boolean retained")
    if (
        isinstance(answer_owed_since, bool)
        or not isinstance(answer_owed_since, (int, float))
        or not math.isfinite(float(answer_owed_since))
        or float(answer_owed_since) < 0
    ):
        raise HeadVitalityError("reduce_vitality needs a finite answer_owed_since")
    answer_owed_since = float(answer_owed_since)
    if not isinstance(adapter, str):
        raise HeadVitalityError("reduce_vitality needs the adapter as a string")

    if not snapshots:
        # No observation, no movement: an episode must not age on ticks that looked at nothing.
        if previous is None:
            raise HeadVitalityError("a first episode needs at least one snapshot")
        return previous

    target_run_id, foreign = _resolve_run_id(previous, snapshots)
    owned = _latest_per_source([snapshot for snapshot in snapshots if snapshot.run_id == target_run_id])
    # Sort sources so durable provenance is independent of batch order.
    basis = [f"dropped-foreign-run:{name}" for name in sorted(foreign)]
    if previous is not None and previous.run_id != target_run_id:
        basis.append(f"identity-changed-from:{previous.run_id}")

    episode = (
        previous
        if previous is not None and previous.run_id == target_run_id
        else VitalityEpisode(
            run_id=target_run_id,
            started_at=now,
            updated_at=now,
        )
    )
    episode = _unfreeze_if_resumed(episode, owned, now)

    # The child source is strong about one thing only -- that the head's children are advancing
    # -- and is fused separately below, so its quiet or its absence never changes which arm the
    # head's own channels select.
    strong = [
        snapshot
        for snapshot in owned.values()
        if snapshot.availability is SourceAvailability.AVAILABLE
        and not snapshot.advisory
        and snapshot.source is not SnapshotSource.EXECUTION_CHILD
    ]
    progress_evidence = [
        snapshot for snapshot in strong if snapshot.progress in (ProgressState.ADVANCING, ProgressState.QUIET)
    ]
    # ``owned`` is a dict keyed by source, so iterating it directly would let the batch's answer
    # order decide the words a deterministic reduction writes. Every ``basis`` token emitted in
    # this loop names its source, so the loop walks the sorted sources instead: same inputs in any
    # order, same basis out (the order-independence tests pin exactly this for dark sources too).
    for source_value in sorted(snapshot.source.value for snapshot in owned.values()):
        snapshot = owned[SnapshotSource(source_value)]
        if snapshot.availability is SourceAvailability.UNAVAILABLE:
            if snapshot.source.value not in episode.unavailable_since:
                episode = replace(
                    episode,
                    unavailable_since={
                        **episode.unavailable_since,
                        snapshot.source.value: now,
                    },
                )
                basis.append(f"unavailable@{snapshot.source.value}")
        else:
            if snapshot.source.value in episode.unavailable_since:
                episode = replace(
                    episode,
                    unavailable_since={
                        name: stamp
                        for name, stamp in episode.unavailable_since.items()
                        if name != snapshot.source.value
                    },
                )
            if snapshot.cursor:
                episode = replace(
                    episode,
                    evidence_cursors={**episode.evidence_cursors, snapshot.source.value: snapshot.cursor},
                )

    # A progress source this episode has heard from that produced NO snapshot on this tick is
    # dark, exactly like one that answered "unavailable" (secretary-1543 round 2). The status
    # shapes that carry a live heartbeat and no ``provider_progress`` key at all -- an exact live
    # pid with no matching pane, a pane that is not connected -- used to leave ``unavailable_since``
    # empty while the cursor from the tick that did answer stayed on file, so the episode read as
    # "witnessed and not dark": the freeze window this card designed was skipped and the guard
    # counted one channel as two. Darkness is therefore recorded from the absence of an answer,
    # not only from an answer of absence, which is what makes "not dark" mean "answering now".
    answered = {snapshot.source.value for snapshot in owned.values()}
    witnessed_progress = (set(episode.evidence_cursors) | {episode.last_progress_source}) & _PROGRESS_SOURCES
    for source_value in sorted(witnessed_progress - answered):
        if source_value not in episode.unavailable_since:
            episode = replace(
                episode,
                unavailable_since={**episode.unavailable_since, source_value: now},
            )
            basis.append(f"absent@{source_value}")

    # The Turn axis, recorded and nothing more. An advisory reading that goes active -> idle
    # stamps ``turn_ended_at``; one that goes back to active clears it. Recording is not
    # convicting: only ``answer_owed_since`` below ever reads the stamp, and only to narrow.
    advisory_turn = next(
        (
            snapshot
            for snapshot in owned.values()
            if snapshot.advisory
            and snapshot.availability is SourceAvailability.AVAILABLE
            and snapshot.turn is not TurnState.UNKNOWN
        ),
        None,
    )
    if advisory_turn is not None:
        if advisory_turn.turn is TurnState.ACTIVE:
            episode = replace(episode, last_turn="active", turn_ended_at=0.0)
        else:
            episode = replace(
                episode,
                last_turn="idle",
                turn_ended_at=(episode.turn_ended_at if episode.last_turn == "idle" else now)
                if episode.last_turn
                else episode.turn_ended_at,
            )

    # Child processes (secretary-1692). The reading's described descendant is kept for a respawn
    # to name; its advancement holds the head healthy only inside ``child_activity_ceiling``,
    # measured from the head's own quiet reference, so a hung-but-spinning child is still caught.
    child = owned.get(SnapshotSource.EXECUTION_CHILD)
    child_hold = False
    child_since = 0.0
    if child is not None and child.availability is SourceAvailability.AVAILABLE:
        episode = replace(
            episode,
            last_child_key=child.child_key,
            last_child_command=child.command if child.child_key else "",
            last_child_output=child.output_path if child.child_key else "",
            last_child_at=child.observed_at if child.child_key else 0.0,
        )
        if child.progress is ProgressState.ADVANCING:
            child_since = episode.child_activity_since or _head_quiet_reference(episode)
            held = max(0.0, child.observed_at - child_since)
            if held < thresholds.child_activity_ceiling:
                child_hold = True
            else:
                basis.append(f"child-ceiling:{int(held)}s@{child.source.value}")

    # The idle-turn stall is decided by ``_idle_turn_stall`` alone; every other arm leaves its stamp
    # clear, so the stamp always describes the reduction that wrote it.
    episode = replace(episode, idle_turn_since=0.0)
    idle_turn = _idle_turn_stall(
        episode,
        owned.get(SnapshotSource.SUPERVISOR_JOURNAL) if adapter in IDLE_TURN_ADAPTERS else None,
        now,
        thresholds,
        answer_owed_since=answer_owed_since,
        child_advancing=child is not None
        and child.availability is SourceAvailability.AVAILABLE
        and child.progress is ProgressState.ADVANCING,
    )

    verdict = VitalityVerdict.UNVERIFIABLE
    if any(snapshot.process is ProcessState.DEAD for snapshot in strong):
        # Death outranks everything: a gone process cannot also be quietly working.
        verdict = VitalityVerdict.DEAD
        basis.append(
            "dead@"
            + ",".join(
                sorted(snapshot.source.value for snapshot in strong if snapshot.process is ProcessState.DEAD)
            )
        )
    elif any(snapshot.process is ProcessState.SUSPENDED for snapshot in strong):
        # One fact, two possible owners. The kernel says the process is parked; only the caller
        # knows whether the dispatcher is the one holding it there. A declared retention is the
        # dispatcher's own intent, so it may not also be read as a head to revive.
        verdict = VitalityVerdict.RETAINED if retained else VitalityVerdict.SUSPENDED
        by_heartbeat = any(
            snapshot.source is SnapshotSource.PID_HEARTBEAT
            for snapshot in strong
            if snapshot.process is ProcessState.SUSPENDED
        )
        token = verdict.value + ("@pid_heartbeat" if by_heartbeat else "")
        basis.append(token)
        episode = _freeze_stall_clocks(episode, now, retained=retained)
    elif any(snapshot.progress is ProgressState.ADVANCING for snapshot in progress_evidence):
        verdict = VitalityVerdict.HEALTHY_ACTIVE
        advancing = max(
            (snapshot for snapshot in progress_evidence if snapshot.progress is ProgressState.ADVANCING),
            key=lambda snapshot: (snapshot.observed_at, snapshot.source.value),
        )
        basis.append(f"advancing@{advancing.source.value}")
        progressed = float(advancing.observed_at)
        moved_ahead = progressed > episode.last_progress_at
        episode = replace(
            episode,
            suspected_since=0.0,
            confirmed_since=0.0,
            last_progress_at=max(episode.last_progress_at, progressed),
            last_progress_source=advancing.source.value,
            activity_epoch=episode.activity_epoch + (1 if moved_ahead else 0),
            stall_frozen_since=0.0,
            # The head moved by itself: any child-only streak is over.
            child_activity_since=0.0,
            reason="",
        )
    elif idle_turn is not None:
        # The head's own supervisor says its turn is over and the dispatcher is still owed an
        # answer: the one rule for that shape, ranked above the child hold on purpose.
        verdict, episode, tokens = idle_turn
        basis.extend(tokens)
    elif child_hold:
        # The head is silent but a child it is waiting on works: not a stall, inside the ceiling.
        # It does not touch ``last_progress_at`` or the activity epoch -- those are the head's
        # own work -- only the child stamp the quiet reference reads.
        assert child is not None
        verdict = VitalityVerdict.HEALTHY_ACTIVE
        basis.append(f"advancing@{child.source.value}")
        held = max(0.0, child.observed_at - child_since)
        episode = replace(
            episode,
            suspected_since=0.0,
            confirmed_since=0.0,
            child_progress_at=max(episode.child_progress_at, child.observed_at),
            child_activity_since=child_since,
            stall_frozen_since=0.0,
            reason=(
                f"the head is quiet while its child process works ({int(held)}s of the "
                f"{int(thresholds.child_activity_ceiling)}s child activity ceiling)"
            ),
        )
    elif progress_evidence:
        # Only observed quiet from a strong, admitted source accumulates toward a stall, and it
        # is measured from the last real progress (or the episode's start), never from the tick.
        quietest = max(progress_evidence, key=lambda snapshot: snapshot.observed_at)
        reference = _quiet_reference(episode)
        quiet_seconds = max(0.0, now - reference)
        basis.append(f"quiet:{int(quiet_seconds)}s@{quietest.source.value}")
        episode = replace(episode, stall_frozen_since=0.0)
        if episode.verdict is VitalityVerdict.CONFIRMED_STALL and not episode.confirmed_since:
            episode = replace(
                episode, confirmed_since=reference + thresholds.suspect_after + thresholds.confirm_after
            )
        if quiet_seconds >= thresholds.suspect_after + thresholds.confirm_after:
            verdict = VitalityVerdict.CONFIRMED_STALL
            episode = replace(
                episode,
                confirmed_since=(
                    episode.confirmed_since or reference + thresholds.suspect_after + thresholds.confirm_after
                ),
                suspected_since=(episode.suspected_since or reference + thresholds.suspect_after),
                reason=f"strong quiet for {int(quiet_seconds)}s with no advancement",
            )
            basis.append("confirmed-stall")
        elif quiet_seconds >= thresholds.suspect_after:
            verdict = VitalityVerdict.SUSPECTED_STALL
            episode = replace(
                episode,
                suspected_since=(episode.suspected_since or reference + thresholds.suspect_after),
                reason=f"strong quiet for {int(quiet_seconds)}s with no advancement",
            )
            basis.append("suspected-stall")
        else:
            verdict = VitalityVerdict.HEALTHY_QUIET
    elif strong:
        # A progress source that is known to this episode and not answering freezes stall time --
        # but only for ``dark_ceiling`` (secretary-1543). Past that window the episode ages
        # exactly as a pid-only run does, because the two situations are the same situation: a
        # live PID and nothing that can say the work moved. Either path still preserves an earned
        # confirmation, and a genuinely dark channel is never itself read as no-progress; what
        # ends is only its power to hold an episode healthy for as long as the PID lives.
        reference = _quiet_reference(episode)
        quiet_seconds = max(0.0, now - reference)
        basis.append(
            "alive-no-progress-source@" + ",".join(sorted(snapshot.source.value for snapshot in strong))
        )
        episode = replace(episode, stall_frozen_since=0.0)
        dark_progress = {
            name: since for name, since in episode.unavailable_since.items() if name in _PROGRESS_SOURCES
        }
        dark_since = min(dark_progress.values()) if dark_progress else 0.0
        dark_seconds = max(0.0, now - dark_since) if dark_progress else 0.0
        dark_names = ",".join(sorted(dark_progress))
        # Naming the source and its darkness on every tick of this arm: the operator question
        # this card exists to answer is "which channel is missing, and for how long".
        if dark_progress:
            basis.append(f"dark:{int(dark_seconds)}s@{dark_names}")
        within_ceiling = bool(dark_progress) and dark_seconds < thresholds.dark_ceiling
        if episode.verdict is VitalityVerdict.CONFIRMED_STALL:
            # Sticky, and sticky first: a dark source may not launder a confirmation back to
            # health, whatever the ceiling has done.
            verdict = VitalityVerdict.CONFIRMED_STALL
            episode = replace(
                episode,
                confirmed_since=(
                    episode.confirmed_since or reference + thresholds.suspect_after + thresholds.confirm_after
                ),
                reason=(
                    f"{dark_names} not answering for {int(dark_seconds)}s; confirmation stands"
                    if dark_progress
                    else "progress source known but not answering; confirmation stands"
                ),
            )
            basis.append("preserved-confirmation:provider-dark-pid-alive")
        elif within_ceiling and episode.verdict is VitalityVerdict.SUSPECTED_STALL:
            # Inside the freeze window a dark source neither rewinds nor advances a phase.
            verdict = VitalityVerdict.SUSPECTED_STALL
            episode = replace(
                episode,
                suspected_since=(episode.suspected_since or reference + thresholds.suspect_after),
                reason=(
                    f"{dark_names} not answering for {int(dark_seconds)}s; suspicion stands"
                    if dark_progress
                    else "progress source known but not answering; suspicion stands"
                ),
            )
            basis.append("preserved-suspicion:provider-dark-pid-alive")
        elif within_ceiling:
            verdict = VitalityVerdict.HEALTHY_QUIET
            episode = replace(
                episode,
                reason=(
                    # The frozen note is the honest conclusion here, but a dark
                    # channel's own diagnostic wins when it has one: an authoritative
                    # launch refusal is more useful to the operator (and to the
                    # policy's deterministic allowlist) than the generic frozen words.
                    next(
                        (
                            snapshot.reason
                            for name in sorted(owned)
                            if (snapshot := owned[name]).availability is SourceAvailability.UNAVAILABLE
                            and snapshot.reason
                        ),
                        (
                            f"{dark_names} known to this episode but not answering for "
                            f"{int(dark_seconds)}s; frozen for another "
                            f"{int(thresholds.dark_ceiling - dark_seconds)}s"
                        ),
                    )
                ),
            )
        else:
            # Either no progress source ever answered (the issue 656 pid-only arm) or the one
            # that did has been dark past the ceiling. Both age on the pid's sustained answer of
            # "running, and nothing else"; the reason says which, and names the dark source.
            aged = (
                f"{dark_names} has been dark for {int(dark_seconds)}s "
                f"(past the {int(thresholds.dark_ceiling)}s ceiling) and the head has shown no "
                f"progress for {int(quiet_seconds)}s"
                if dark_progress
                else f"running with no progress evidence for {int(quiet_seconds)}s"
            )
            if quiet_seconds >= thresholds.suspect_after + thresholds.confirm_after:
                # A pid-only run ages: existence alone is not liveness.
                verdict = VitalityVerdict.CONFIRMED_STALL
                episode = replace(
                    episode,
                    confirmed_since=(
                        episode.confirmed_since
                        or reference + thresholds.suspect_after + thresholds.confirm_after
                    ),
                    suspected_since=(episode.suspected_since or reference + thresholds.suspect_after),
                    reason=aged,
                )
                basis.append("confirmed-stall")
            elif quiet_seconds >= thresholds.suspect_after:
                verdict = VitalityVerdict.SUSPECTED_STALL
                episode = replace(
                    episode,
                    suspected_since=(episode.suspected_since or reference + thresholds.suspect_after),
                    reason=aged,
                )
                basis.append("suspected-stall")
            else:
                verdict = VitalityVerdict.HEALTHY_QUIET
    else:
        # Every strong source is dark (or only the advisory pane answered). Nothing may be
        # concluded -- and an already-confirmed episode is not laundered back to health by its
        # observers going blind: its confirmation stands frozen until real evidence moves it.
        if episode.verdict is VitalityVerdict.CONFIRMED_STALL:
            verdict = VitalityVerdict.CONFIRMED_STALL
            # The confirmation stands, but its onset must be stamped here too: an episode that
            # arrived confirmed through deserialisation carries the field, yet one reduced into
            # confirmation by a tick whose observers then went dark does not, and a phase without
            # its onset is a claim nobody can audit. The quiet it confirmed is still measured
            # from the same reference every other quiet conclusion uses.
            quiet_reference = _quiet_reference(episode)
            episode = replace(
                episode,
                confirmed_since=(
                    episode.confirmed_since
                    or quiet_reference + thresholds.suspect_after + thresholds.confirm_after
                ),
            )
            basis.append("preserved-confirmation:strong-sources-unavailable")
        else:
            verdict = VitalityVerdict.UNVERIFIABLE
            basis.append("no-strong-source-answered")
    if (
        verdict is VitalityVerdict.HEALTHY_QUIET
        and answer_owed_since > 0.0
        and episode.last_progress_at < answer_owed_since
        and episode.turn_ended_at >= answer_owed_since
    ):
        # The explicit watchdog signal (secretary-1543): the dispatcher rejected this head's
        # report at ``answer_owed_since`` and sent it back to work, the head then ENDED a turn,
        # and no source has shown progress since. That pair is a conclusion on its own -- there is
        # nothing left running to wait for -- so it does not queue behind the quiet thresholds or
        # the outer ceiling. It can only raise HealthyQuiet: an advancing, dead, suspended,
        # retained or already-stalled head is decided by the arms above, and the advisory reading
        # that stamped ``turn_ended_at`` never establishes health here or anywhere else.
        verdict = VitalityVerdict.SUSPECTED_STALL
        owed_seconds = int(max(0.0, now - answer_owed_since))
        episode = replace(
            episode,
            suspected_since=(episode.suspected_since or episode.turn_ended_at),
            reason=(
                f"the dispatcher's rejected report has gone unanswered for {owed_seconds}s and "
                "the head's turn ended with no new work"
            ),
        )
        basis.append("answer-owed:turn-ended")

    for snapshot in owned.values():
        if snapshot.advisory and snapshot.availability is SourceAvailability.AVAILABLE:
            # Corroboration only: recorded so a human reading the basis sees the pane agreed,
            # weightless by construction.
            basis.append(f"advisory:{snapshot.turn.value}@{snapshot.source.value}")

    return replace(
        episode,
        verdict=verdict,
        # Preserve a dark source diagnostic unless a stronger conclusion supplies one.
        reason=episode.reason
        if episode.reason
        else next(
            (
                snapshot.reason
                for name in sorted(owned)
                if (snapshot := owned[name]).availability is SourceAvailability.UNAVAILABLE
                and snapshot.reason
            ),
            "",
        ),
        basis=tuple(basis[:BASIS_ENTRY_LIMIT]),
        updated_at=now,
    )


def _idle_turn_stall(
    episode: VitalityEpisode,
    journal: VitalitySnapshot | None,
    now: float,
    thresholds: VitalityThresholds,
    *,
    answer_owed_since: float,
    child_advancing: bool,
) -> tuple[VitalityVerdict, VitalityEpisode, list[str]] | None:
    """Judge an idle, completed supervisor turn only for supported adapters with
    a journal answer and a caller-declared outstanding answer.

    Charge quiet time from the latest idle start, owed-answer time or nudge.
    Child activity cannot excuse an idle turn; existing stall evidence remains
    unless a newer question/nudge resets that interval."""
    if (
        journal is None
        or journal.availability is not SourceAvailability.AVAILABLE
        or journal.turn is not TurnState.IDLE
        or not journal.turn_since
        or answer_owed_since <= 0.0
    ):
        return None
    reference = max(journal.turn_since, answer_owed_since, episode.quiet_since)
    idle_seconds = max(0.0, now - reference)
    ladder = (VitalityVerdict.HEALTHY_QUIET, VitalityVerdict.SUSPECTED_STALL, VitalityVerdict.CONFIRMED_STALL)
    level = (
        2
        if idle_seconds >= thresholds.idle_turn_confirm_after
        else 1
        if idle_seconds >= thresholds.idle_turn_suspect_after
        else 0
    )
    suspect_at = reference + thresholds.idle_turn_suspect_after
    confirm_at = reference + thresholds.idle_turn_confirm_after
    tokens = [f"idle-turn:{int(idle_seconds)}s@{SnapshotSource.SUPERVISOR_JOURNAL.value}"]
    reason = (
        f"the head's turn ended and it has sat at its prompt for {int(idle_seconds)}s "
        "without answering the dispatcher"
    )
    held = ladder.index(episode.verdict) if episode.verdict in ladder else 0
    onset = episode.suspected_since or episode.confirmed_since or episode.updated_at
    if held > level and max(answer_owed_since, episode.quiet_since) <= onset:
        level = held
        tokens.append("stall-stands")
        reason = f"{episode.verdict.value} stands and the head's turn has ended: {reason}"
    if child_advancing:
        tokens.append("child-not-holding-idle-turn")
    verdict = ladder[level]
    if verdict is VitalityVerdict.CONFIRMED_STALL:
        tokens.append("confirmed-stall")
        stamps = {
            "suspected_since": episode.suspected_since or suspect_at,
            "confirmed_since": episode.confirmed_since or confirm_at,
        }
    elif verdict is VitalityVerdict.SUSPECTED_STALL:
        tokens.append("suspected-stall")
        stamps = {"suspected_since": episode.suspected_since or suspect_at, "confirmed_since": 0.0}
    else:
        stamps = {"suspected_since": 0.0, "confirmed_since": 0.0}
    episode = replace(
        episode,
        **stamps,
        idle_turn_since=reference,
        stall_frozen_since=0.0,
        reason=reason,
    )
    return verdict, episode, tokens


def recovery_outlook(
    episode: Any,
    now: float,
    thresholds: VitalityThresholds = DEFAULT_VITALITY_THRESHOLDS,
) -> dict[str, Any]:
    """Summarize the episode's observed sources, quiet duration and next action."""
    if not isinstance(episode, VitalityEpisode) or not isinstance(thresholds, VitalityThresholds):
        return {"quiet_seconds": 0.0, "dark_sources": [], "next_deadline": None, "note": ""}
    if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(float(now)):
        return {"quiet_seconds": 0.0, "dark_sources": [], "next_deadline": None, "note": ""}
    now = float(now)
    reference = _quiet_reference(episode)
    quiet_seconds = max(0.0, now - reference)
    dark = [
        {
            "source": name,
            "dark_since": since,
            "dark_seconds": max(0.0, now - since),
            "freeze_expires_at": since + thresholds.dark_ceiling,
        }
        for name, since in sorted(episode.unavailable_since.items())
        if name in _PROGRESS_SOURCES
    ]
    frozen_until = max((entry["freeze_expires_at"] for entry in dark), default=0.0)
    note = ""
    at: float | None = None
    becomes = ""
    if episode.idle_turn_since and episode.verdict in (
        VitalityVerdict.HEALTHY_QUIET,
        VitalityVerdict.SUSPECTED_STALL,
    ):
        # The idle-turn rule decided: its deadlines run from the idle turn's own start, and no
        # dark source freezes them.
        if episode.verdict is VitalityVerdict.HEALTHY_QUIET:
            becomes = VitalityVerdict.SUSPECTED_STALL.value
            at = episode.idle_turn_since + thresholds.idle_turn_suspect_after
        else:
            becomes = VitalityVerdict.CONFIRMED_STALL.value
            at = episode.idle_turn_since + thresholds.idle_turn_confirm_after
    elif episode.verdict is VitalityVerdict.HEALTHY_QUIET:
        becomes = VitalityVerdict.SUSPECTED_STALL.value
        at = max(reference + thresholds.suspect_after, frozen_until)
    elif episode.verdict is VitalityVerdict.SUSPECTED_STALL:
        becomes = VitalityVerdict.CONFIRMED_STALL.value
        at = max(reference + thresholds.suspect_after + thresholds.confirm_after, frozen_until)
    elif episode.verdict is VitalityVerdict.CONFIRMED_STALL:
        note = "the stall is confirmed; the recovery path owns this head, not another threshold"
    elif episode.verdict in (VitalityVerdict.SUSPENDED, VitalityVerdict.RETAINED):
        note = "the stall clocks are frozen while the process is parked on a stop signal"
    return {
        "quiet_seconds": quiet_seconds,
        "dark_sources": dark,
        "next_deadline": (
            None if at is None else {"verdict": becomes, "at": at, "in_seconds": max(0.0, at - now)}
        ),
        "note": note,
    }


def _resolve_run_id(
    previous: VitalityEpisode | None, snapshots: Sequence[VitalitySnapshot]
) -> tuple[str, list[str]]:
    """Pick the run this reduction speaks about, and name the foreign runs it drops."""
    run_ids = sorted({snapshot.run_id for snapshot in snapshots})
    chosen = previous.run_id if previous is not None and previous.run_id in run_ids else run_ids[0]
    return chosen, [name for name in run_ids if name != chosen]


def _latest_per_source(snapshots: Sequence[VitalitySnapshot]) -> dict[SnapshotSource, VitalitySnapshot]:
    """Collapse one batch to each source's latest reading, ties resolved by first appearance."""
    latest: dict[SnapshotSource, VitalitySnapshot] = {}
    for snapshot in snapshots:
        known = latest.get(snapshot.source)
        if known is None or snapshot.observed_at > known.observed_at:
            latest[snapshot.source] = snapshot
    return latest


def _freeze_stall_clocks(episode: VitalityEpisode, now: float, *, retained: bool = False) -> VitalityEpisode:
    """Park the quiet clock for a suspended reading: suspension is not stall evidence.

    The freeze itself is identical for both owners of the stop signal -- a parked process is not
    accumulating quiet either way. Only the recorded reason differs, so an operator reading
    ``head-status`` sees which one it is.
    """
    episode = replace(
        episode,
        # Suspension is a distinct, recoverable state -- SIGCONT territory, not stall territory.
        suspected_since=0.0,
        confirmed_since=0.0,
        stall_frozen_since=episode.stall_frozen_since or now,
        reason=(
            "process held on a stop signal by a confirmed retention"
            if retained
            else "process suspended on a stop signal"
        ),
    )
    return episode


def _quiet_reference(episode: VitalityEpisode) -> float:
    """The instant this episode's current quiet began: what every threshold is measured from."""
    return max(episode.last_progress_at, episode.quiet_since, episode.child_progress_at) or episode.started_at


def _head_quiet_reference(episode: VitalityEpisode) -> float:
    """The quiet reference of the head's OWN channels, ignoring child activity.

    The child activity ceiling is measured from here, so the child's movement can never extend
    the window it is itself bounded by.
    """
    return max(episode.last_progress_at, episode.quiet_since) or episode.started_at


def _unfreeze_if_resumed(
    episode: VitalityEpisode, owned: Mapping[SnapshotSource, VitalitySnapshot], now: float
) -> VitalityEpisode:
    """Resume the quiet clock after suspension by shifting references past the frozen span.

    The frozen span belongs to neither work nor stall, so every quiet-derived timestamp moves
    forward by exactly it: the head is charged with the silence it was actually awake for.
    """
    if not episode.stall_frozen_since:
        return episode
    still_suspended = any(
        snapshot.availability is SourceAvailability.AVAILABLE
        and not snapshot.advisory
        and snapshot.process is ProcessState.SUSPENDED
        for snapshot in owned.values()
    )
    if still_suspended:
        return episode
    delta = max(0.0, now - episode.stall_frozen_since)

    def shift(stamp: float) -> float:
        return min(now, stamp + delta) if stamp else stamp

    return replace(
        episode,
        started_at=shift(episode.started_at),
        last_progress_at=shift(episode.last_progress_at),
        quiet_since=shift(episode.quiet_since),
        child_progress_at=shift(episode.child_progress_at),
        child_activity_since=shift(episode.child_activity_since),
        suspected_since=shift(episode.suspected_since),
        confirmed_since=shift(episode.confirmed_since),
        stall_frozen_since=0.0,
    )


def interrupted_command_note(episode: Any, run_id: str) -> str:
    """The factual line a respawned successor gets about its predecessor's running command."""
    if not isinstance(episode, VitalityEpisode) or not run_id or episode.run_id != str(run_id):
        return ""
    command = episode.last_child_command.strip()
    if not command:
        return ""
    output = episode.last_child_output.strip()
    return (
        "The previous head was stopped while running: "
        + command
        + (f" (its output was redirected to {output})" if output else "")
    )
