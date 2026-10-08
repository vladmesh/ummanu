"""One liveness rule for a pending production head handoff, whichever caller owns it (ummanu-142).

A production handoff (`runtime.head.handoff`) may stay pending across ticks: at settle, typed or
submitted, or with its supervisor not answering within the pass's allowance (`deferred`). Whether
the handoff has delivered is the head's own status and journal; whether the head is alive at all is
a separate question, and this module is the one place it is asked, for the retained worker's
continuation (`worker_continuation._production_liveness_step`) and the reviewer's launch
(`review.reviewer_pending_liveness`) alike:

- The only evidence is the exact-source provider cursor of the exact HeadRun the handoff is for
  (`WorkerContinuationLiveness.observe_provider`). A heartbeat, a valid advisory ingress event, an
  echo, a spinner, an accepted Enter or a pending receipt is not provider progress.
- A cursor that does not move spends one no-progress attempt each time the stall outlasts the next
  step of the busy schedule (30 s, then 90 s, then 210 s since the last progress or the episode's
  first observation), at most one per pass. The schedule is read off durable times, so a restart
  neither resets nor repeats it, and a pass that spends nothing writes nothing.
- Nothing about the handoff can stop this: its stage, its deferral, its allowance. Each caller maps
  the verdict onto its own existing terminal machinery, always behind a confirmed stop.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ummanu.dispatch.helpers import scrub_host_output
from ummanu.dispatch.worker_lifecycle import (
    BUSY_RETRY_INITIAL_SECONDS,
    CONTINUATION_NO_PROGRESS_BUSY_ATTEMPTS,
    ContinuationLivenessState,
    WorkerContinuationLiveness,
)

#: The provider moved since the last look: the ladder starts again.
PENDING_PROGRESSED = "progressed"
#: Nothing is due: a baseline, or a stall the schedule has already counted.
PENDING_QUIET = "quiet"
#: One no-progress attempt was spent by this look.
PENDING_ATTEMPT = "attempt"
#: The source cannot be trusted for this HeadRun (identity, legacy binding, or, where the caller
#: does not count it, unavailability): the caller's terminal outcome for an unprovable source.
PENDING_UNPROVABLE = "unprovable"

#: Where the episode is kept on a reviewer launch's delivery record, and its pre-baseline schedule.
LAUNCH_LIVENESS_KEY = "liveness"
LAUNCH_UNPROVEN_KEY = "unproven"


@dataclass(frozen=True)
class PendingLiveness:
    """What one look found: the verdict, and the provider observation it was made from."""

    verdict: str
    observation: str

    @property
    def progressed(self) -> bool:
        return self.verdict == PENDING_PROGRESSED


def no_progress_attempts_due(stalled_for: float) -> int:
    """How many no-progress attempts a stall this long has earned on the busy schedule."""
    due, threshold, step = 0, float(BUSY_RETRY_INITIAL_SECONDS), float(BUSY_RETRY_INITIAL_SECONDS)
    while stalled_for >= threshold and due < CONTINUATION_NO_PROGRESS_BUSY_ATTEMPTS:
        due += 1
        step *= 2
        threshold += step
    return due


@dataclass
class UnprovenSchedule:
    """The no-progress schedule of a head whose provider record does not exist yet.

    A head raised for this handoff (a reviewer) has no provider record until it takes its prompt,
    so its source may be unavailable before its episode can be baselined. Its episode must stay a
    clean, unbaselined one until then (`WorkerContinuationLiveness.from_json` refuses anything else),
    so the looks it gets meanwhile are counted here, on the same schedule, from the first of them.
    Once the episode is baselined its own schedule takes over.
    """

    since: float = 0.0
    attempts: int = 0

    def to_json(self) -> dict[str, Any]:
        return {"since": self.since, "attempts": self.attempts}

    @classmethod
    def from_json(cls, value: Any) -> UnprovenSchedule:
        if not isinstance(value, dict):
            return cls()
        try:
            since, attempts = float(value.get("since") or 0.0), int(value.get("attempts") or 0)
        except (TypeError, ValueError):
            return cls()
        return cls(since=max(0.0, since), attempts=max(0, attempts))


def no_progress_exhausted(liveness: WorkerContinuationLiveness, unproven: UnprovenSchedule | None = None) -> bool:
    if unproven is not None and unproven.attempts >= CONTINUATION_NO_PROGRESS_BUSY_ATTEMPTS:
        return True
    return liveness.busy_attempts >= CONTINUATION_NO_PROGRESS_BUSY_ATTEMPTS


def observe_pending_handoff(
    liveness: WorkerContinuationLiveness,
    evidence: Any,
    *,
    now: float,
    head_run: Any,
    unproven: UnprovenSchedule | None = None,
) -> PendingLiveness:
    """Apply the rule to one exact-source answer; the caller persists what it passed in, if changed.

    `unproven`: the caller counts an unavailable source as no progress on the same schedule rather
    than taking an immediate terminal outcome (a head raised for this handoff, see
    `UnprovenSchedule`). A retained worker was baselined before it was woken, passes none, and keeps
    the immediate outcome. A source that names another HeadRun is never counted: it is unprovable.
    """
    if unproven is not None and not liveness.baseline_established and not _admissible(evidence):
        if isinstance(evidence, dict) and str(evidence.get("state") or "") == "identity_mismatch":
            liveness.observe_provider(evidence, now, head_run=head_run)
            return PendingLiveness(PENDING_UNPROVABLE, "unknown")
        if not unproven.since:
            unproven.since = now
        if unproven.attempts >= no_progress_attempts_due(now - unproven.since):
            return PendingLiveness(PENDING_QUIET, "unavailable")
        unproven.attempts += 1
        return PendingLiveness(PENDING_ATTEMPT, "unavailable")
    observation = liveness.observe_provider(evidence, now, head_run=head_run)
    if observation == "progressed":
        return PendingLiveness(PENDING_PROGRESSED, observation)
    admitted = liveness.admitted and observation in {"baseline", "stalled"}
    if not admitted:
        if unproven is not None and observation == "unavailable" and liveness.baseline_established:
            # An episode already baselined, whose source has gone: no progress, on its own schedule.
            return _schedule(liveness, now, observation)
        return PendingLiveness(PENDING_UNPROVABLE, observation)
    if liveness.state != ContinuationLivenessState.STALLED:
        return PendingLiveness(PENDING_QUIET, observation)
    return _schedule(liveness, now, observation)


def _schedule(liveness: WorkerContinuationLiveness, now: float, observation: str) -> PendingLiveness:
    stalled_since = max(liveness.last_provider_progress_at, liveness.first_observed_at)
    if liveness.busy_attempts >= no_progress_attempts_due(now - stalled_since):
        return PendingLiveness(PENDING_QUIET, observation)
    if not liveness.first_busy_at:
        liveness.first_busy_at = now
    liveness.busy_attempts += 1
    return PendingLiveness(PENDING_ATTEMPT, observation)


def _admissible(evidence: Any) -> bool:
    return (
        isinstance(evidence, dict)
        and str(evidence.get("state") or "") == "observed"
        and str(evidence.get("admission") or "") == "accepted"
    )


def provider_evidence(runtime: Any, task: dict[str, Any], record: Any, kind: str) -> Any:
    """The exact-source provider answer for one role's persisted HeadRun; a refusal is evidence too."""
    try:
        return getattr(
            runtime.host,
            "provider_progress",
            lambda _task, _record, _kind: {
                "state": "unavailable",
                "reason": "host has no provider-progress probe",
            },
        )(task, record, kind)
    except Exception as exc:  # noqa: BLE001 - evidence must retain any host refusal.
        return {
            "state": "unavailable",
            "reason": f"provider-progress probe failed: {scrub_host_output(str(exc))}",
        }


def durable_view(record: Any) -> dict[str, Any]:
    """The record as written, less the one stamp every provider read refreshes.

    `last_provider_observed_at` says only when a cursor was last read, on the worker's episode and
    on a reviewer launch's alike; a pass whose read changed nothing else has nothing to write.
    """
    view = record.to_json()
    liveness = view.get("worker_continuation_liveness")
    if isinstance(liveness, dict):
        view["worker_continuation_liveness"] = _without_read_stamp(liveness)
    intent = view.get("launch_intent")
    delivery = intent.get("delivery") if isinstance(intent, dict) else None
    if isinstance(delivery, dict) and isinstance(delivery.get(LAUNCH_LIVENESS_KEY), dict):
        view["launch_intent"] = {
            **intent,
            "delivery": {**delivery, LAUNCH_LIVENESS_KEY: _without_read_stamp(delivery[LAUNCH_LIVENESS_KEY])},
        }
    return view


def _without_read_stamp(liveness: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in liveness.items() if key != "last_provider_observed_at"}
