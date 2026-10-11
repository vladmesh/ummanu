"""Pure recovery-intent policy over a persisted VitalityEpisode.

A suspected stall may earn an idempotent nudge; an intentional Retained state
never earns recovery. Suspension uses one identity-fenced SIGCONT per span,
then a response window and operator escalation. Deterministic refusals can
escalate early. This policy never stops/kills a head or performs I/O."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from ummanu.dispatch.head_vitality_episode import VitalityEpisode, VitalityVerdict

# Serialisation version of RecoveryDecision telemetry payloads. Kept beside the snapshot and
# episode versions so a schema change answers for old payloads explicitly.
DECISION_VERSION = 1


class RecoveryIntent(StrEnum):
"""What the dispatcher may intend for one head, as of one tick."""

    #: Nothing earned: keep watching. Wired.
    OBSERVE = "observe"
    #: One conversational nudge into a suspected-stalled head. Wired (spent through the
    #: existing ``_prompt_worker_report`` machinery at the call site).
    NUDGE = "nudge"
    #: Send SIGCONT to the head's process group, identity-fenced at send time. Wired.
    SIGCONT = "sigcont"
    #: Ask the current head to finish and hand over cleanly. Unwired.
    REQUEST_DRAIN = "request_drain"
    #: Escalate to a human, touching nothing. Wired.
    ESCALATE_OPERATOR = "escalate_operator"
    #: Start a fresh head of the same profile. Unwired.
    RESPAWN = "respawn"
    #: Stop admitting work to this head entirely. Unwired.
    BLOCK = "block"


#: The rungs :func:`decide_recovery` actually climbs this card. Named constants so tests,
#: docs and telemetry all say the same numbers.
RUNG_NONE = 0
RUNG_SUSPICION_NOTED = 1
RUNG_SIGCONT_SENT = 2
RUNG_RESPONSE_WINDOW = 3
RUNG_ESCALATED = 4


class DeterministicReasonClass(StrEnum):
    """Why a refusal reason landed on the deterministic allowlist, in operator vocabulary."""

    INVALID_CONFIGURATION = "invalid_configuration"
    MISSING_EXECUTABLE = "missing_executable"
    AUTHENTICATION_REJECTED = "authentication_rejected"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    SPLIT_SOURCE_NOT_FOUND = "split_source_not_found"


#: The authoritative deterministic terminal-reason allowlist.
#:
#: Entries classify immutable launch refusals, not timing, availability or transport failures.
#:
#: Matching is token-in-bounded-string, not prose scraping: producers put machine tokens on
#: the wire (see ``VitalitySnapshot.from_pane_readiness``, which carries the producer's
#: diagnostic verbatim inside the bounded reason), and this table matches the token wherever
#: it appears in that string.
DETERMINISTIC_TERMINAL_REASONS: dict[str, DeterministicReasonClass] = {
    "terminal_split_source_not_found": DeterministicReasonClass.SPLIT_SOURCE_NOT_FOUND,
    # Invalid launch configuration: the requested profile/split/adaptor combination does not
    # exist, so every retry of the same request refuses identically.
    "terminal_split_invalid_request": DeterministicReasonClass.INVALID_CONFIGURATION,
    # Missing executable: the head's own binary is absent -- a packaging or PATH fact.
    "head_executable_not_found": DeterministicReasonClass.MISSING_EXECUTABLE,
    # Authentication rejected: credentials refused authoritatively, not timed out.
    "authentication_rejected": DeterministicReasonClass.AUTHENTICATION_REJECTED,
    # Resource exhausted: a hard quota/capacity answer (disk, seats, budget). Transient
    # pressure instead arrives as source unavailability, which the reducer freezes rather
    # than spends -- only a terminal refusal lands here.
    "resource_exhausted": DeterministicReasonClass.RESOURCE_EXHAUSTED,
}


def deterministic_reason_class(reason: str) -> DeterministicReasonClass | None:
    """The authoritative class a bounded refusal reason belongs to, or ``None``.

    ``None`` means heuristic-or-unknown: however often such a reason repeats, it is NOT
    evidence, and this function is the single place that distinction is made.
    """
    text = str(reason or "")
    for token, cls in DETERMINISTIC_TERMINAL_REASONS.items():
        if token in text:
            return cls
    return None


@dataclass(frozen=True)
class RecoveryThresholds:
"""Response window and deterministic-refusal count thresholds.

    The response window spans multiple ticks; repeated authoritative refusals
    trigger operator escalation after the configured bound."""

    response_window_seconds: float
    deterministic_refusal_limit: int

    def __post_init__(self) -> None:
        value = self.response_window_seconds
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) <= 0
        ):
            raise ValueError("recovery threshold response_window_seconds is positive and finite")
        limit = self.deterministic_refusal_limit
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("recovery threshold deterministic_refusal_limit is a positive int")
        object.__setattr__(self, "response_window_seconds", float(value))


#: Five minutes: see the docstring above for why this scale. Read per call site through
#: UMMANU_HEAD_SUSPENSION_RESPONSE_SECONDS so operations can tune it without a release.
DEFAULT_RESPONSE_WINDOW_SECONDS = 5 * 60
DEFAULT_DETERMINISTIC_REFUSAL_LIMIT = 3

DEFAULT_RECOVERY_THRESHOLDS = RecoveryThresholds(
    response_window_seconds=float(DEFAULT_RESPONSE_WINDOW_SECONDS),
    deterministic_refusal_limit=DEFAULT_DETERMINISTIC_REFUSAL_LIMIT,
)


@dataclass(frozen=True)
class RecoveryDecision:
"""One policy ruling: the intent, the rung to persist, and the audit trail."""

    intent: RecoveryIntent
    rung: int
    refusals: int = 0
    reason: str = ""
    detail: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        """The bounded form that travels in tick outcomes and telemetry comments."""
        return {
            "version": DECISION_VERSION,
            "intent": self.intent.value,
            "rung": self.rung,
            "refusals": self.refusals,
            "reason": self.reason,
            "detail": dict(self.detail or {}),
        }


def apply_rung_state(episode: VitalityEpisode, decision: RecoveryDecision) -> VitalityEpisode:
"""Write a decision's rung state back onto the episode, purely."""
    return replace(
        episode,
        recovery_rung=decision.rung,
        recovery_span_started_at=float(episode.stall_frozen_since or 0.0),
        deterministic_refusals=int(decision.refusals),
    )


def decide_recovery(
    episode: Any,
    previous_rung_state: Any,
    now: float,
    thresholds: RecoveryThresholds = DEFAULT_RECOVERY_THRESHOLDS,
) -> RecoveryDecision:
"""Choose a pure, idempotent recovery intent for this tick.

    Retained and ordinary healthy/unknown/dead verdicts observe; SuspectedStall
    may nudge once; Suspended advances a single-span SIGCONT, response-window,
    operator-escalation ladder. Identical deterministic refusals can escalate
    independently. Rung history comes from the previously persisted episode."""
    if not isinstance(episode, VitalityEpisode):
        return _previous_or_observe(previous_rung_state, "no persisted episode to decide from")
    if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(float(now)):
        return _previous_or_observe(previous_rung_state, "the policy was handed a clock it cannot read")
    if not isinstance(thresholds, RecoveryThresholds):
        return _previous_or_observe(previous_rung_state, "the policy was handed thresholds it cannot read")
    now = float(now)

    if episode.verdict is VitalityVerdict.RETAINED:
        # The dispatcher itself is holding this process on a stop signal, and it is the caller
        # who told the reducer so. There is no rung to climb: not SIGCONT (which would wake a
        # session the gate is deliberately holding still -- secretary-1539), not the nudge, and
        # not the operator escalation either, since nothing here is unexpected. The ladder is
        # reset to zero exactly as health resets it, so the retention's own end -- a resume, or a
        # stop signal nobody intended -- starts a fresh span.
        #
        # Deliberately BEFORE the deterministic-refusal arm: this arm is about who owns the stop
        # signal, and no reason string may promote a retention into a rung.
        return RecoveryDecision(
            intent=RecoveryIntent.OBSERVE,
            rung=RUNG_NONE,
            refusals=0,
            reason="",
            detail={"verdict": episode.verdict.value, "retained": True},
        )

    cls = deterministic_reason_class(episode.reason)
    if cls is not None:
        return _decide_deterministic(episode, previous_rung_state, thresholds, cls)

    if episode.verdict is VitalityVerdict.SUSPECTED_STALL:
        # The nudge itself stays at the call site (it needs the host); the policy records
        # that this suspicion was consumed, completing the rung table. Idempotency per
        # round generation remains the nudge machinery's contract, unchanged from S1-4.
        return RecoveryDecision(
            intent=RecoveryIntent.NUDGE,
            rung=RUNG_SUSPICION_NOTED,
            refusals=episode.deterministic_refusals,
            reason=episode.reason or "strong quiet past the suspect threshold",
            detail={"suspected_since": episode.suspected_since},
        )

    if episode.verdict is VitalityVerdict.SUSPENDED:
        decision = _decide_suspended(episode, previous_rung_state, now, thresholds)

        return decision

    # Healthy*, Dead, Unverifiable: observe, and clear the ladder. A past span's escalation
    # does not survive into health -- the operator comment already written stands, but the
    # next suspension must earn its own rungs from scratch.
    return RecoveryDecision(
        intent=RecoveryIntent.OBSERVE,
        rung=RUNG_NONE,
        refusals=0,
        reason="",
        detail={"verdict": episode.verdict.value},
    )


def _previous_or_observe(previous_rung_state: Any, reason: str) -> RecoveryDecision:
    """Degrade to observing while keeping whatever rung was already persisted.

    Hostile input must not rewind rung state (that could re-fire an already-spent SIGCONT
    on the next sane tick), so the previously persisted values carry through untouched.
    """
    if isinstance(previous_rung_state, VitalityEpisode):
        return RecoveryDecision(
            intent=RecoveryIntent.OBSERVE,
            rung=previous_rung_state.recovery_rung,
            refusals=previous_rung_state.deterministic_refusals,
            reason=reason,
        )
    return RecoveryDecision(intent=RecoveryIntent.OBSERVE, rung=RUNG_NONE, reason=reason)


def _decide_deterministic(
    episode: VitalityEpisode,
    previous_rung_state: Any,
    thresholds: RecoveryThresholds,
    cls: DeterministicReasonClass,
) -> RecoveryDecision:
"""Count identical authoritative refusals and escalate fast past the limit."""
    seen = int(episode.deterministic_refusals or 0)
    if isinstance(previous_rung_state, VitalityEpisode):
        # The caller may pass the fresher episode as both arguments (reduce-then-decide on
        # one object); the persisted count is whichever the record actually carried.
        seen = max(seen, int(previous_rung_state.deterministic_refusals or 0))
    refusals = seen + 1
    detail: dict[str, Any] = {
        "deterministic_class": cls.value,
        "identical_refusals": refusals,
        "limit": thresholds.deterministic_refusal_limit,
    }
    if refusals >= thresholds.deterministic_refusal_limit:
        return RecoveryDecision(
            intent=RecoveryIntent.ESCALATE_OPERATOR,
            rung=max(int(episode.recovery_rung or 0), RUNG_ESCALATED),
            refusals=refusals,
            reason=(
                f"deterministic refusal '{cls.value}' repeated {refusals}x: retrying "
                "cannot change it, escalating to the operator instead of re-sending"
            ),
            detail=detail,
        )
    return RecoveryDecision(
        intent=RecoveryIntent.OBSERVE,
        rung=int(episode.recovery_rung or 0),
        refusals=refusals,
        reason=(
            f"deterministic refusal '{cls.value}' ({refusals}x of "
            f"{thresholds.deterministic_refusal_limit}): counting toward fast escalation"
        ),
        detail=detail,
    )


def _decide_suspended(
    episode: VitalityEpisode,
    previous_rung_state: Any,
    now: float,
    thresholds: RecoveryThresholds,
) -> RecoveryDecision:
    """The SIGCONT rungs for one suspension span, keyed on the reducer's freeze stamp."""
    span = float(episode.stall_frozen_since or 0.0)
    previous_rung = (
        int(previous_rung_state.recovery_rung or 0) if isinstance(previous_rung_state, VitalityEpisode) else 0
    )
    previous_span = (
        float(previous_rung_state.recovery_span_started_at or 0.0)
        if isinstance(previous_rung_state, VitalityEpisode)
        else 0.0
    )
    same_span = previous_rung >= RUNG_SIGCONT_SENT and previous_span > 0.0 and previous_span == span
    detail: dict[str, Any] = {"span_started_at": span}
    if not same_span:
        # Fresh span (or the first policy sighting of it): one identity-fenced SIGCONT.
        # The rung lands at RESPONSE_WINDOW because sending and waiting are one step from
        # the caller's perspective; SIGCONT_SENT exists as vocabulary for a caller that had
        # to persist "sent" before its window bookkeeping could start.
        return RecoveryDecision(
            intent=RecoveryIntent.SIGCONT,
            rung=RUNG_RESPONSE_WINDOW,
            refusals=episode.deterministic_refusals,
            reason=(
                "process parked on a stop signal: send one identity-fenced SIGCONT, "
                f"then hold a {int(thresholds.response_window_seconds)}s response window"
            ),
            detail={
                **detail,
                "response_window_seconds": thresholds.response_window_seconds,
                "window_expires_at": now + thresholds.response_window_seconds,
            },
        )
    if previous_rung >= RUNG_ESCALATED:
        # Already escalated for this span: stay observable but do not re-fire. The comment
        # was written once; repeating it per tick would flood the card.
        return RecoveryDecision(
            intent=RecoveryIntent.OBSERVE,
            rung=RUNG_ESCALATED,
            refusals=episode.deterministic_refusals,
            reason=(
                "still suspended past the response window; the operator has been "
                "escalated for this span and the head remains untouched"
            ),
            detail=detail,
        )
    suspended_for = max(0.0, now - span)
    if suspended_for <= thresholds.response_window_seconds:
        return RecoveryDecision(
            intent=RecoveryIntent.OBSERVE,
            rung=RUNG_RESPONSE_WINDOW,
            refusals=episode.deterministic_refusals,
            reason=(
                "SIGCONT sent this span; inside the response window "
                f"({int(suspended_for)}s of {int(thresholds.response_window_seconds)}s)"
            ),
            detail={
                **detail,
                "suspended_for_seconds": suspended_for,
                "window_expires_at": span + thresholds.response_window_seconds,
            },
        )
    return RecoveryDecision(
        intent=RecoveryIntent.ESCALATE_OPERATOR,
        rung=RUNG_ESCALATED,
        refusals=episode.deterministic_refusals,
        reason=(
            f"suspended for {int(suspended_for)}s -- past the "
            f"{int(thresholds.response_window_seconds)}s response window even after "
            "SIGCONT; escalating to the operator, NOT stopping the process"
        ),
        detail={**detail, "suspended_for_seconds": suspended_for},
    )
