"""Shared dispatcher wait, watchdog and head-vitality policy.

This module owns the common worker/reviewer wait state machine: vitality reduction,
recovery-policy rungs, guarded respawn/escalation, suspension recovery and bounded
unobservable-head escalation. Worker/reviewer lifecycle owners remain collaborators;
gate/review/Assessment/merge decisions stay outside this boundary.
"""

from __future__ import annotations

import os
import signal
import time
from collections.abc import Callable
from typing import Any

from ummanu.board import owner_events
from ummanu.dispatch import attempt_accounting
from ummanu.dispatch.head_vitality import SnapshotSource as _SnapshotSource
from ummanu.dispatch.head_vitality import snapshots_from_status as _snapshots_from_status
from ummanu.dispatch.head_vitality_episode import (
    DEFAULT_VITALITY_THRESHOLDS as _DEFAULT_VITALITY_THRESHOLDS,
)
from ummanu.dispatch.head_vitality_episode import VitalityVerdict
from ummanu.dispatch.head_vitality_episode import interrupted_command_note as _interrupted_command_note
from ummanu.dispatch.head_vitality_episode import reduce_vitality as _reduce_vitality
from ummanu.dispatch.head_vitality_guard import (
    assert_destructive_allowed as _assert_destructive_allowed,
)
from ummanu.dispatch.head_vitality_policy import (
    DEFAULT_RECOVERY_THRESHOLDS as _DEFAULT_RECOVERY_THRESHOLDS,
)
from ummanu.dispatch.head_vitality_policy import RUNG_ESCALATED as _RUNG_ESCALATED
from ummanu.dispatch.head_vitality_policy import RecoveryIntent as _RecoveryIntent
from ummanu.dispatch.head_vitality_policy import RecoveryThresholds as _RecoveryThresholds
from ummanu.dispatch.head_vitality_policy import apply_rung_state as _apply_rung_state
from ummanu.dispatch.head_vitality_policy import decide_recovery as _decide_recovery
from ummanu.dispatch.helpers import scrub_host_output
from ummanu.dispatch.host import DESTRUCTIVE_VERDICTS
from ummanu.dispatch.launch import STAGE_RESPAWN, WORKER_ROLE
from ummanu.dispatch.launch import clear_launch_intent as _clear_launch_intent
from ummanu.dispatch.launch import launch_intent_unwritable as _launch_intent_unwritable
from ummanu.dispatch.provider_failure import provider_failure_outcome as _provider_failure_outcome
from ummanu.dispatch.review import start_review as _start_review
from ummanu.dispatch.state import DispatcherRecord
from ummanu.dispatch.state import attempt_request_id as _attempt_request_id
from ummanu.dispatch.state import request_token as _request_token
from ummanu.dispatch.types import STOPPED_BY_WATCHDOG, HostError
from ummanu.dispatch.watchdog import HeadRunIdentityMismatch as _HeadRunIdentityMismatch
from ummanu.dispatch.watchdog import guard_head_run_identity as _guard_head_run_identity
from ummanu.dispatch.watchdog import heartbeat_is_live_match as _heartbeat_is_live_match
from ummanu.dispatch.watchdog import initial_output_stall_seconds as _initial_output_stall_seconds
from ummanu.dispatch.watchdog import reset_idle as _reset_idle
from ummanu.dispatch.watchdog import stall_seconds as _stall_seconds
from ummanu.dispatch.watchdog import (
    suspension_response_window_seconds as _suspension_response_window_seconds,
)
from ummanu.dispatch.watchdog import wait_cycle_token as _wait_cycle_token
from ummanu.dispatch.worker_launch import bring_up_worker_head as _bring_up_worker_head
from ummanu.dispatch.worker_launch import (
    write_worker_relaunch_intent as _write_worker_relaunch_intent,
)
from ummanu.dispatch.worker_report import prompt_worker_report as _prompt_worker_report
from ummanu.runtime.head import HeadRun as _HeadRun


def wait_watchdog(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    *,
    kind: str,
) -> dict[str, Any] | None:
    """Watch an open-ended wait without confusing a bad Orca inventory for a dead head."""
    if getattr(record, f"paused_{'reviewer' if kind == 'review' else 'worker'}_at"):
        return {
            "status": "ok",
            "step": "review" if kind == "review" else "advance",
            "pilot_ref": task["ref"],
            "attempt_id": attempt_id,
            "action": f"{kind}-paused",
        }
    runtime_reason = ""
    try:
        status = (
            runtime.host.review_status(task, record)
            if kind == "review"
            else runtime.host.worker_status(task, record)
        )
    except Exception as exc:  # noqa: BLE001 - provider status has no narrower exception contract.
        # Orca may be down or between reconnects. That is no evidence this head died, so do not
        # restart it; it also cannot prove progress, so the ordinary wait ceiling stays.
        status = {"known": False, "live": True, "reason": "runtime-unavailable"}
        runtime_reason = scrub_host_output(str(exc))
    if status.get("identity_mismatch"):
        return {
            "status": "degraded",
            "step": "review" if kind == "review" else "advance",
            "pilot_ref": task["ref"],
            "attempt_id": attempt_id,
            "action": f"{kind}-heartbeat-identity-mismatch",
            "reason": "the heartbeat names a live process with a mismatching launch identity",
        }
    # A turn that ended on a provider error is a provider verdict, and it outranks every
    # stall reading below (secretary-1799): a head idle at its prompt after a 401 is not late, it
    # was refused, and the answer is the next head of its chain, not a nudge, a respawn into the
    # same provider or Blocked. Decided on the tick that first sees the turn's end.
    provider_outcome = _provider_failure_outcome(
        runtime, task, record, records, payload, attempt_id, kind=kind
    )
    if provider_outcome is not None:
        return provider_outcome
    activity = status.get("last_activity")
    progress_at = float(getattr(record, f"{kind}_progress_at") or 0.0)
    if activity:
        updated = max(progress_at, float(activity))
        if updated != progress_at:
            progress_at = updated
            setattr(record, f"{kind}_progress_at", progress_at)
            runtime.save_records(payload, records)
    now = time.time()
    episode = reduce_and_store_vitality_episode(runtime,
        task,
        record,
        records,
        payload,
        status,
        kind=kind,
        now=now,
        answer_owed_since=answer_owed_since_for_wait(record, kind),
    )
    # THE DECISION IS THE VERDICT (S1-4): the persisted episode -- reduced from this
    # very tick's observations on every shape the status carries, including the
    # not-live ones -- chooses between waiting, nudging and recovering. The old
    # not-live shortcut is gone: what used to be an unconditional reclaim is now
    # taken only when the reduction actually saw death (``Dead``), and a terminal
    # that vanished while the heartbeat stays live is decided by evidence, not by
    # the inventory.
    return _decide_wait_by_verdict(runtime,
        task,
        record,
        records,
        payload,
        attempt_id,
        kind=kind,
        status=status,
        episode=episode,
        now=now,
        runtime_reason=runtime_reason,
        activity=activity,
        progress_at=progress_at,
    )


def _decide_wait_by_verdict(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    *,
    kind: str,
    status: dict[str, Any],
    episode: Any,
    now: float,
    runtime_reason: str,
    activity: Any,
    progress_at: float,
) -> dict[str, Any] | None:
    """Turn this tick's vitality verdict into the wait tick's one decision.

    Verdict -> action, per the plan's recovery policy (nudge before destruction,
    ``wait`` whenever the evidence does not earn intervention):

    * ``HealthyActive`` / ``HealthyQuiet`` / ``Unverifiable`` / ``Suspended`` ->
      ``wait``. A quiet-below-threshold head is between turns; an unverifiable head
      has no strong witness; a suspended head is SIGCONT territory (S1-5). None of
      them may be nudged into a respawn by a clock.
    * ``Retained`` -> ``wait``, and nothing else at all: this dispatcher is holding
      that process on a stop signal itself, so not even the SIGCONT rung applies
      (secretary-1539). The wait clock is renewed because the head is not late.
    * ``SuspectedStall`` -> at most one idempotent report nudge per round, keyed on
      the round generation like every nudge; a suspicion never destroys.
    * ``ConfirmedStall`` / ``Dead`` -> the ordinary recovery path
      (``_trigger_wait_watchdog``), whose every destructive step re-checks the guard.
    * No episode at all (nothing was ever observed for this run), or ``Unverifiable``
      -> ``wait`` while the ceiling has not elapsed, then an OPERATOR escalation
      (``_escalate_unobservable_wait``): one idempotent durable comment plus a
      degraded outcome naming the evidence gap. Such a run is never destroyed -- a
      run nobody could observe is also a run nobody can prove dead -- so its wait is
      bounded by escalation, not by replacement. This differs from main before
      S1-4, which reclaimed such heads on the clock alone; that behaviour is what
      the guard now refuses.

    Evidence-shaped legacy branches inside this fallback (no output since launch; no
    terminal progress) keep their pre-vitality triggers because they act on what a
    source actually said, and their destructive steps are still fenced by the guard.
    """
    ref = task["ref"]
    expectation = _wait_expectation(kind)
    if kind == "worker":
        expectation = f"{expectation} for generation {record.report_generation}"
    verdict = episode.verdict if episode is not None else None

    def plain_wait() -> dict[str, Any] | None:
        if runtime_reason:
            return {
                "status": "degraded",
                "step": "review" if kind == "review" else "advance",
                "pilot_ref": ref,
                "attempt_id": attempt_id,
                "action": f"{kind}-runtime-unavailable",
                "reason": runtime_reason,
            }
        return None

    if verdict is VitalityVerdict.DEAD:
        # The heartbeat names a gone process: the existing not-live handling, from
        # the same evidence the reduction used.
        loss_reason = status.get("head_loss_reason")
        return _trigger_wait_watchdog(runtime,
            task,
            record,
            records,
            payload,
            attempt_id,
            kind=kind,
            trigger=(
                "memory_limit: the supervisor recorded a head memory-limit kill"
                if loss_reason == "memory_limit"
                else "the pid heartbeat names a gone or unreaped process"
            ),
        )
    if verdict is VitalityVerdict.CONFIRMED_STALL:
        reason = (
            f"the {kind} head's vitality episode confirms a stall "
            f"({episode.reason or 'strong quiet past both thresholds'})"
            f" with no {expectation}"
        )
        if kind == "worker":
            # The confirmed boundary: ask once for the report before anything
            # destructive, exactly as the idle ladder did -- but only when the
            # episode itself says the head is stalled.
            prompted, reason = _prompt_worker_report(
                runtime,
                task, record, records, payload, attempt_id, trigger=reason
            )
            if prompted is not None:
                return prompted
        # Degraded, not ok: an `ok` bounce would write healthy telemetry over the
        # one signal that says this card needs looking at before it reaches Blocked.
        return _trigger_wait_watchdog(runtime,
            task,
            record,
            records,
            payload,
            attempt_id,
            kind=kind,
            trigger=reason,
            degraded=True,
        )
    if verdict is VitalityVerdict.SUSPECTED_STALL:
        # One idempotent nudge, then wait: the suspicion phase exists so a single
        # lost turn recovers conversationally instead of destructively.
        suspicion_basis = episode.reason or "strong quiet past the suspect threshold"
        if kind == "worker":
            prompted, trigger = _prompt_worker_report(
                runtime,
                task,
                record,
                records,
                payload,
                attempt_id,
                trigger=(
                    f"the {kind} head's vitality episode suspects a stall "
                    f"({suspicion_basis}) with no {expectation}"
                ),
            )
            if prompted is not None:
                return prompted
            # The prompt was already spent this round (or the head cannot take one):
            # carry the suspicion as visible degradation without escalating.
            return {
                "status": "degraded",
                "step": "review" if kind == "review" else "advance",
                "pilot_ref": ref,
                "attempt_id": attempt_id,
                "action": f"{kind}-stall-suspected",
                "reason": trigger,
            }
        return {
            "status": "degraded",
            "step": "review" if kind == "review" else "advance",
            "pilot_ref": ref,
            "attempt_id": attempt_id,
            "action": f"{kind}-stall-suspected",
            "reason": (
                f"the review head's vitality episode suspects a stall "
                f"({suspicion_basis}) with no {expectation}"
            ),
        }
    if verdict is VitalityVerdict.RETAINED:
        # The dispatcher's own retention is holding this head still, and it is the only
        # thing that ends it. Waking it would be this tick fighting the tick that parked
        # it (secretary-1539), so nothing is signalled, nudged or escalated here. The
        # policy still rides along to clear any rung a past suspension span left behind,
        # and the role's wait clock is renewed because a retained head is not late: it is
        # not being waited on at all.
        _run_recovery_policy(runtime,
            task,
            record,
            records,
            payload,
            episode=episode,
            kind=kind,
            now=now,
        )
        setattr(record, f"{kind}_waiting_since", now)
        runtime.save_records(payload, records)
        if runtime_reason:
            return plain_wait()
        return {
            "status": "ok",
            "step": "review" if kind == "review" else "advance",
            "pilot_ref": ref,
            "attempt_id": attempt_id,
            "action": f"{kind}-retained",
            "reason": "the head is held suspended by this card's confirmed retention",
        }
    if verdict is VitalityVerdict.SUSPENDED:
        # The recovery policy owns this arm (S1-5): one identity-fenced SIGCONT per
        # suspension span, then a bounded response window, then operator escalation --
        # never a stop. The comment is keyed per span so it cannot flood.
        return execute_recovery_intent(runtime,
            task,
            record,
            records,
            payload,
            attempt_id,
            episode=episode,
            kind=kind,
            now=now,
        )
    if verdict in (VitalityVerdict.HEALTHY_ACTIVE, VitalityVerdict.HEALTHY_QUIET):
        # Fresh evidence of life: renew the outer window and wait. No clock on this
        # path may act against what the evidence calls alive. A recovered suspension
        # lands here too; the policy's rung reset rides the same recovery decision,
        # persisted back onto this same role's episode slot.
        _run_recovery_policy(runtime,
            task,
            record,
            records,
            payload,
            episode=episode,
            kind=kind,
            now=now,
        )
        setattr(record, f"{kind}_waiting_since", now)
        runtime.save_records(payload, records)
        return plain_wait()
    # Unverifiable, or no episode at all: nothing strong answered, so the honest
    # answer is that nobody knows. The plan forbids KILLING such a run -- and the
    # guard enforces exactly that, refusing every destructive step on this path --
    # but an unobservable wait is still bounded: once the role's outer ceiling has
    # elapsed with no verdict earned, the tick escalates to the OPERATOR (one
    # idempotent durable comment per wait cycle plus a degraded outcome). Escalation
    # is not replacement: the head is never touched here. The two legacy
    # evidence-shaped branches below keep their pre-vitality meaning because they
    # act only on what a source actually said (no output since launch; no terminal
    # progress), and even they are fenced by the guard.
    #
    # Before the ceilings speak, the policy gets its say: an authoritative
    # deterministic refusal riding this tick's unavailable snapshot (the 1194 class)
    # escalates after N identical sightings instead of waiting out any ceiling.
    policy_outcome = recovery_policy_outcome(runtime,
        task,
        record,
        records,
        payload,
        attempt_id,
        episode=episode,
        kind=kind,
        now=now,
    )
    if policy_outcome is not None:
        return policy_outcome
    stall = _stall_seconds(kind)
    waiting_since = float(getattr(record, f"{kind}_waiting_since") or 0.0)
    started_at = float(getattr(record, f"{kind}_started_at") or 0.0)
    pid_confirmed = bool(status.get("pid_confirmed"))
    if (
        not pid_confirmed
        and activity
        and started_at
        and float(activity) <= started_at
        and now - started_at > _initial_output_stall_seconds()
    ):
        return _trigger_wait_watchdog(runtime,
            task,
            record,
            records,
            payload,
            attempt_id,
            kind=kind,
            trigger=f"no terminal output since launch for {int(now - started_at)}s",
        )
    if progress_at and now - progress_at > stall:
        return _trigger_wait_watchdog(runtime,
            task,
            record,
            records,
            payload,
            attempt_id,
            kind=kind,
            trigger=f"no terminal output for {int(now - progress_at)}s",
        )
    if not waiting_since:
        setattr(record, f"{kind}_waiting_since", now)
        runtime.save_records(payload, records)
        return plain_wait()
    unobserved_for = now - waiting_since
    if unobserved_for >= stall:
        return _escalate_unobservable_wait(runtime,
            task,
            record,
            attempt_id,
            kind=kind,
            seconds=int(unobserved_for),
            ceiling=stall,
            runtime_reason=runtime_reason,
        )
    return plain_wait()


def _escalate_unobservable_wait(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    attempt_id: str,
    *,
    kind: str,
    seconds: int,
    ceiling: int,
    runtime_reason: str = "",
) -> dict[str, Any]:
    """Escalate an unobservable head to the operator WITHOUT touching it.

    Reached only from the no-episode/Unverifiable fallback of
    ``_decide_wait_by_verdict`` once the role's outer ceiling has elapsed on a wait
    nobody could observe. The plan's asymmetry forbids killing what nothing could
    read (the guard refuses it), but an operator must not inherit an unbounded silent
    wait either, so this is the bound: one durable comment per wait cycle (keyed like
    every watchdog comment, so it cannot flood) naming the evidence gap and the
    elapsed span, plus a degraded tick outcome. The head is not signalled, stopped or
    replaced; if it starts answering again the reduction earns a verdict and the
    ordinary table takes over.
    """
    ref = task["ref"]
    detail = f"; {runtime_reason}" if runtime_reason else ""
    runtime.writer.comment(
        role="dispatcher",
        actor=runtime.owner,
        reference=ref,
        body=(
            f"Dispatcher wait watchdog ({kind}): nothing could observe this head for "
            f"{seconds}s (outer ceiling {ceiling}s) -- no readable heartbeat and no "
            f"provider answer{detail}. The head was NOT stopped or replaced: no "
            "evidence earned that. Escalating to the operator; the card keeps "
            "waiting until someone looks or the head becomes observable again."
        ),
        request_id=_attempt_request_id(
            record.attempt_id or attempt_id,
            f"{kind}-unobserved-wait",
            ref,
            _wait_cycle_token(record),
        ),
    )
    return {
        "status": "degraded",
        "step": "review" if kind == "review" else "advance",
        "pilot_ref": ref,
        "attempt_id": attempt_id,
        "action": f"{kind}-unobserved-wait-escalated",
        "reason": (
            f"nothing could observe the head for {seconds}s "
            f"(ceiling {ceiling}s); escalated to the operator, head untouched"
        ),
    }


def _recovery_thresholds(runtime: Any) -> Any:
    """This installation's recovery-policy thresholds, read per call.

    The response window comes from the watchdog's env knob so operations can tighten it
    without a release; the deterministic-refusal limit stays at its small default.
    """
    return _RecoveryThresholds(
        response_window_seconds=float(_suspension_response_window_seconds()),
        deterministic_refusal_limit=_DEFAULT_RECOVERY_THRESHOLDS.deterministic_refusal_limit,
    )


def _recovery_policy_decision(
    runtime: Any,
    episode: Any,
    *,
    kind: str,
    now: float,
) -> tuple[Any, Any] | None:
    """Ask the policy what this tick should intend, and persist its rung state.

    Returns ``(decision, updated_episode)`` or ``None`` when there is nothing to decide
    (no episode). The rung write happens here, once, so every caller of the policy -- the
    wait tick and the gate phase alike -- persists exactly the same shape.
    """
    if episode is None:
        return None
    decision = _decide_recovery(episode, episode, now, _recovery_thresholds(runtime))
    # An observe with unchanged state rewrites nothing: persisting per tick would churn
    # the record for zero information. Only a rung/refusal change is worth a save.
    if decision.intent is _RecoveryIntent.OBSERVE and (
        episode.recovery_rung == decision.rung and episode.deterministic_refusals == decision.refusals
    ):
        return decision, episode
    updated = _apply_rung_state(episode, decision)
    return decision, updated


def _store_recovery_episode(
    runtime: Any,
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    ref: str,
    *,
    kind: str,
    episode: Any,
) -> None:
    field_name = f"{kind}_vitality_episode"
    setattr(record, field_name, episode)
    records[ref] = record
    runtime.save_records(payload, records)


def recovery_policy_outcome(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    *,
    episode: Any,
    kind: str,
    now: float,
) -> dict[str, Any] | None:
    """Turn a policy decision into a tick outcome where the caller needs one.

    Used on arms that only escalate (the deterministic-refusal fast path): ``None``
    means "no escalation earned, carry on with the caller's own logic".
    """
    asked = _recovery_policy_decision(runtime, episode=episode, kind=kind, now=now)
    if asked is None:
        return None
    decision, updated = asked
    if updated is not episode:
        _store_recovery_episode(runtime,
            record,
            records,
            payload,
            task["ref"],
            kind=kind,
            episode=updated,
        )
    if decision.intent is not _RecoveryIntent.ESCALATE_OPERATOR:
        return None
    return _escalate_recovery_to_operator(runtime,
        task,
        record,
        attempt_id,
        kind=kind,
        decision=decision,
        now=now,
    )


def _run_recovery_policy(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    *,
    episode: Any,
    kind: str,
    now: float,
) -> None:
    """Let the policy observe a non-suspended verdict (rung reset after recovery).

    The wait-tick table already handled this verdict; the policy call exists purely to
    clear the persisted ladder when a suspension has resolved: the cleared episode comes
    back at ``RUNG_NONE``, and a later fresh suspension span climbs
    ``RUNG_SIGCONT_SENT`` -> ``RUNG_RESPONSE_WINDOW`` again instead of inheriting an old
    escalation.

    ``kind`` routes the persistence, exactly as everywhere else on this path: the reset
    is stored back onto the SAME role's episode slot the reduction read it from
    (``review_vitality_episode`` for the review head, the worker slot for the worker).
    Persisting across roles would park one run's episode in the other role's field --
    a foreign-run episode the destructive guard refuses as FOREIGN_RUN until some later
    ordinary reduction overwrites it, and a rung that never resets on the real subject.
    """
    asked = _recovery_policy_decision(runtime, episode=episode, kind=kind, now=now)
    if asked is None:
        return
    _, updated = asked
    if updated is not episode:
        _store_recovery_episode(runtime,
            record,
            records,
            payload,
            task["ref"],
            kind=kind,
            episode=updated,
        )


def execute_recovery_intent(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    *,
    episode: Any,
    kind: str,
    now: float,
) -> dict[str, Any]:
    """Execute one recovery-policy intent for a suspended head, safely.

    The Suspended arm's whole surface: ask the policy, persist its rung state, then act:

    * ``sigcont``  -> one identity-fenced SIGCONT (never SIGTERM/SIGKILL from this path)
      plus one durable comment naming the span; idempotent because the policy keys the
      intent on the freeze stamp and only returns it for a fresh span.
    * ``observe`` inside the window -> the plain degraded wait outcome, visible in
      telemetry but touching nothing.
    * ``escalate_operator`` -> one durable comment per span asking a human to look;
      the head is never signalled, stopped or replaced. The guard would refuse any
      destructive step on a Suspended verdict regardless; this path simply never asks.
    """
    ref = task["ref"]
    asked = _recovery_policy_decision(runtime, episode=episode, kind=kind, now=now)
    if asked is None:
        return {
            "status": "degraded",
            "step": "review" if kind == "review" else "advance",
            "pilot_ref": ref,
            "attempt_id": attempt_id,
            "action": f"{kind}-suspension-observed",
            "reason": "no vitality episode on file",
        }
    decision, updated = asked
    _store_recovery_episode(runtime, record, records, payload, ref, kind=kind, episode=updated)
    step = "review" if kind == "review" else "advance"
    if decision.intent is _RecoveryIntent.SIGCONT:
        sent = _sigcont_head(runtime, task, record, kind=kind, now=now)
        body = (
            f"Vitality ({kind}): the head's process is parked on a stop signal "
            f"(suspended since {time.strftime('%H:%M:%S', time.gmtime(decision.detail['span_started_at']))}). "
            + (
                f"Sent one identity-fenced SIGCONT; holding a "
                f"{int(decision.detail['response_window_seconds'])}s response window before escalating."
                if sent
                else "Could NOT verify the process identity, so nothing was signalled; "
                "holding the response window and watching."
            )
            + " The head was not stopped."
        )
        runtime.writer.comment(
            role="dispatcher",
            actor=runtime.owner,
            reference=ref,
            body=body,
            request_id=_attempt_request_id(
                record.attempt_id or attempt_id,
                f"{kind}-vitality-sigcont",
                ref,
                suffix=_request_token(f"sigcont@{decision.detail['span_started_at']:.0f}"),
            ),
        )
        return {
            "status": "degraded",
            "step": step,
            "pilot_ref": ref,
            "attempt_id": attempt_id,
            "action": f"{kind}-sigcont-sent" if sent else f"{kind}-sigcont-fenced",
            "reason": decision.reason,
            "recovery": decision.to_json(),
        }
    if decision.intent is _RecoveryIntent.ESCALATE_OPERATOR:
        _escalate_suspended_head(runtime,
            task,
            record,
            attempt_id,
            kind=kind,
            decision=decision,
        )
        return {
            "status": "degraded",
            "step": step,
            "pilot_ref": ref,
            "attempt_id": attempt_id,
            "action": f"{kind}-suspension-escalated",
            "reason": decision.reason,
            "recovery": decision.to_json(),
        }
    # Observe: inside the response window (or already escalated and holding).
    return {
        "status": "ok" if decision.rung < _RUNG_ESCALATED else "degraded",
        "step": step,
        "pilot_ref": ref,
        "attempt_id": attempt_id,
        "action": f"{kind}-suspension-observed",
        "reason": decision.reason,
        "recovery": decision.to_json(),
    }


def _sigcont_head(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    *,
    kind: str,
    now: float = 0.0,
) -> bool:
    """Send SIGCONT to this role's head process group, identity-fenced at send time.

    The ONLY signal this recovery path may send, and only after re-verifying through
    the heartbeat that the live process behind the pid file is still this exact HeadRun
    (`guard_head_run_identity` raises on a foreign live process, the same fence
    `_confirm_head_process_gone` uses before its signals). A mismatched, unreadable or
    vanished identity sends nothing: resuming somebody else's process group is worse
    than leaving our own parked one parked one more tick. Never SIGTERM/SIGKILL here --
    suspension is recoverable by definition, and the destructive paths keep their own
    guarded entries.
    """
    if getattr(runtime.host, "mode", "real") == "noop":
        return False
    pid_file = record.review_pid_file if kind == "review" else record.worker_pid_file
    if not pid_file:
        return False
    run = record.review_head_run if kind == "review" else record.worker_head_run
    leaf = record.review_leaf if kind == "review" else record.worker_leaf
    try:
        status = _guard_head_run_identity(
            pid_file,
            run=run,
            role=kind,
            task=f"card:{task['ref']}",
            leaf=leaf,
        )
    except _HeadRunIdentityMismatch:
        return False
    if not _heartbeat_is_live_match(status):
        return False
    pid = int(status["pid"])
    try:
        # Same group rule as `_signal_head`: the terminal gives an interactive head
        # its own foreground process group, so the CONT reaches its helpers too; old
        # launches and focused tests may share OUR group, and signalling that would
        # wake the dispatcher itself rather than the head.
        group = os.getpgid(pid)
        if group != os.getpgrp():
            os.killpg(group, signal.SIGCONT)
        else:
            os.kill(pid, signal.SIGCONT)
    except ProcessLookupError:
        return False
    except OSError as exc:
        raise HostError(f"head process {pid} could not be resumed: {exc}") from None
    return True


def _escalate_suspended_head(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    attempt_id: str,
    *,
    kind: str,
    decision: Any,
) -> None:
    """One durable comment per suspension span: the response window expired."""
    detail = decision.detail or {}
    suspended_for = int(detail.get("suspended_for_seconds") or 0)
    window = int(detail.get("span_started_at") or 0)
    runtime.writer.comment(
        role="dispatcher",
        actor=runtime.owner,
        reference=task["ref"],
        body=(
            f"Dispatcher recovery policy ({kind}): the head stayed suspended for "
            f"{suspended_for}s -- past the response window even after SIGCONT. The "
            "process was NOT stopped or replaced: a suspended process is alive by "
            "the kernel's own word. Escalating to the operator; please look at the "
            f"head (pane handle {record.review_handle if kind == 'review' else record.handle}"
            f", heartbeat {record.review_pid_file if kind == 'review' else record.worker_pid_file})."
        ),
        request_id=_attempt_request_id(
            record.attempt_id or attempt_id,
            f"{kind}-suspension-window-expired",
            task["ref"],
            suffix=_request_token(f"suspended@{window:.0f}"),
        ),
    )


def _escalate_recovery_to_operator(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    attempt_id: str,
    *,
    kind: str,
    decision: Any,
    now: float = 0.0,
) -> dict[str, Any]:
    """Escalate a deterministic terminal refusal class to the operator, touching nothing.

    Reached from the no-conclusion fallback once N identical authoritative refusals are
    on file (the 1194 contract): the attempt cannot succeed by retrying, so the ladder
    is skipped entirely. One comment per refusal count (idempotent via the request id),
    plus a degraded outcome. The head is never signalled or replaced from here.
    """
    detail = decision.detail or {}
    ref = task["ref"]
    runtime.writer.comment(
        role="dispatcher",
        actor=runtime.owner,
        reference=ref,
        body=(
            f"Dispatcher recovery policy ({kind}): the same authoritative refusal "
            f"({detail.get('deterministic_class', 'deterministic')}) arrived "
            f"{detail.get('identical_refusals', '?')}x. Retrying cannot change it: the "
            "reason names a property of this launch (configuration, executable, "
            "credentials, quota), not a transient outage. Escalating to the operator "
            "instead of re-sending; nothing was stopped or replaced."
        ),
        request_id=_attempt_request_id(
            record.attempt_id or attempt_id,
            f"{kind}-deterministic-refusal",
            ref,
            str(detail.get("identical_refusals") or 0),
        ),
    )
    return {
        "status": "degraded",
        "step": "review" if kind == "review" else "advance",
        "pilot_ref": ref,
        "attempt_id": attempt_id,
        "action": f"{kind}-deterministic-refusal-escalated",
        "reason": decision.reason,
        "recovery": decision.to_json(),
    }


def _vitality_guard_decision(
    runtime: Any,
    record: DispatcherRecord,
    *,
    kind: str,
    action: str,
    current_run_id: str = "",
) -> Any:
    """Ask the vitality guard whether this watchdog-driven step may proceed."""
    field_name = f"{kind}_vitality_episode"
    return _assert_destructive_allowed(
        getattr(record, field_name),
        action,
        time.time(),
        current_run_id=current_run_id
        or str(
            (record.review_head_run if kind == "review" else record.worker_head_run).get("run_id")
            or ""
        ),
        pid_only_outer_ceiling_seconds=float(_stall_seconds(kind)),
    )


def _guard_or_wait(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    *,
    kind: str,
    now: float,
    action: str,
    proceed: Callable[[], dict[str, Any]],
    current_run_id: str = "",
) -> dict[str, Any]:
    """Run one watchdog-driven destructive step through the vitality guard.

    Allowed -> the step runs as before. Refused -> the tick degrades to a visible,
    idempotent ``{kind}-guard-refused`` wait outcome naming the refusal class, and
    the destructive step does not happen. A refusal is never a silent no-op: the
    outcome carries it, and the durable comment is written once per cycle (keyed on
    the wait-cycle token, like every other watchdog comment).
    """
    decision = _vitality_guard_decision(runtime,
        record,
        kind=kind,
        action=action,
        current_run_id=current_run_id,
    )
    if decision.allowed:
        return proceed()
    request_id = _attempt_request_id(
        record.attempt_id or attempt_id,
        f"{kind}-vitality-guard-refused",
        task["ref"],
        # The refusal CLASS rides the key beside the cycle token, because the body below is a
        # function of exactly what the key names (secretary-1477). Two refusals of different
        # classes inside one wait cycle are two different claims and get two ids; two of the
        # same class are one claim and replay idempotently.
        f"{_wait_cycle_token(record)}-{decision.refusal.value}",
    )
    runtime.writer.comment(
        role="dispatcher",
        actor=runtime.owner,
        reference=task["ref"],
        body=(
            f"Dispatcher wait watchdog refused a destructive step ({decision.refusal.value}). "
            "Nothing was stopped or replaced; the card keeps waiting. The refusal's live "
            "measurement -- the quiet, the dark sources and the next deadline -- stays on the "
            "durable vitality episode; read it with `ummanu head-status`."
        ),
        request_id=request_id,
    )
    return {
        "status": "degraded",
        "step": "review" if kind == "review" else "advance",
        "pilot_ref": task["ref"],
        "attempt_id": attempt_id,
        "action": f"{kind}-guard-refused",
        "reason": f"{decision.refusal.value}: {decision.reason}",
        "guard": decision.to_json(),
    }


def answer_owed_since_for_wait(record: DispatcherRecord, kind: str) -> float:
    """When the head this wait tick watches was put the question it has not answered yet.

    The wait tick runs only while the dispatcher waits on this head's answer -- a worker before
    its report is accepted, a reviewer before its verdict -- so the question stands from the
    bring-up or delivery that opened the phase (``{kind}_started_at``, stamped by every launch,
    continuation, rework and respawn), or, for a worker, from a done report bounced back to rework
    (``worker_answer_owed_since``, secretary-1543), whichever is later. This is the one notion of
    an owed answer the reducer reads (``answer_owed_since``); the gate phase, where the worker's
    report is already accepted, does not come through here and owes nothing.
    """
    if kind == "review":
        return float(record.review_started_at or 0.0)
    return max(float(record.worker_started_at or 0.0), float(record.worker_answer_owed_since or 0.0))


def _run_adapter(run_payload: Any) -> str:
    """The run's ``HeadRun.spec.adapter``, the reducer's declared input for the idle-turn rule.

    An unreadable run is no adapter: the rule then stays off and nothing else changes.
    """
    try:
        return str(_HeadRun.from_json(run_payload).spec.adapter or "")
    except Exception:  # noqa: BLE001 - a damaged run declares nothing; the rule stays off
        return ""


def reduce_and_store_vitality_episode(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    status: dict[str, Any],
    *,
    kind: str,
    now: float,
    answer_owed_since: float | None = None,
) -> Any:
    """Reduce and persist one vitality episode for this role's head run; return it.

    This is S1-2's shadow reduction, promoted (card S1-4) into the wait tick's
    decision input. The contract grows by exactly one clause: the method now returns
    the episode it stored (``None`` when nothing was observed or the reduction
    failed) so the caller can decide from it -- every other property is unchanged.
    Every early return here leaves ``record`` untouched or unchanged, so a caller
    that gets ``None`` decides as it would have with no episode at all: a reduction
    failure degrades to "no episode" plus one comment and must never break the tick
    hosting it.

    Sources actually observed on this path, without any new host call:

    * ``pid_heartbeat`` -- only when the status carries ``pid_status`` (the raw
      ``head_process_status`` classification). The wait tick itself consumes derived booleans
      (`pid_confirmed`, `identity_mismatch`), so the classification is passed through by
      ``command_terminal_status`` alongside them; where it is absent the source stays
      Unavailable rather than being reconstructed from a boolean.
    * ``provider_cursor`` -- from ``status["provider_progress"]``, the already-admitted
      exact-HeadRun evidence ``command_terminal_status`` fetched; compared against the
      previous cursor persisted on the episode.
    * ``pane_advisory`` -- from the same status's ``idle`` flag, advisory by construction.
    * ``execution_child`` -- from ``status["child_activity"]``, the head's descendants read
      from ``/proc`` by ``command_terminal_status`` for a pid the heartbeat proved; compared
      against the previous child cursor and described child kept on the episode.
    * ``supervisor_journal`` -- from ``status["supervisor_journal"]``, the local-pty head's own
      journal reading (Turn, and Progress against the previous journal cursor on the episode).

    ``answer_owed_since`` is the wait tick's declaration (``answer_owed_since_for_wait``);
    a caller that waits on no answer (the gate phase) leaves it ``None`` and only a bounced report
    counts, as before.
    """
    field_name = f"{kind}_vitality_episode"
    previous = getattr(record, field_name)
    run_payload = record.review_head_run if kind == "review" else record.worker_head_run
    run_id = str(run_payload.get("run_id") or "")
    if not run_id:
        # Without a durable run identity there is nothing an episode may bind to. Leaving any
        # stale episode in place would misattribute it to a head nobody can name, so it is
        # dropped explicitly.
        if previous is not None:
            setattr(record, field_name, None)
        return None
    pid_status = status.get("pid_status")
    provider_progress = status.get("provider_progress")
    if (
        not isinstance(pid_status, dict)
        and not isinstance(provider_progress, dict)
        and "idle" not in status
        and not isinstance(status.get("child_activity"), dict)
        and "supervisor_journal" not in status
    ):
        # Nothing was observed at all (the noop host, a runtime-unavailable tick): there is
        # no reduction to run and no episode to write, so return before saving anything.
        # Writing one would both rewrite the state file every such tick and stamp an
        # "observation" nobody made -- the same lie Unverifiable exists to avoid. The
        # caller decides this tick from ``None`` -- i.e. from the outer ceilings, the
        # pre-vitality behaviour for an unobservable head -- while the guard below any
        # destructive step still reads whatever verdict the record already carries.
        return None
    snapshots = _snapshots_from_status(
        status,
        run_id=run_id,
        previous_cursor=(
            (previous.evidence_cursors or {}).get(_SnapshotSource.PROVIDER_CURSOR.value, "")
            if previous is not None
            else ""
        ),
        previous_child_cursor=(
            (previous.evidence_cursors or {}).get(_SnapshotSource.EXECUTION_CHILD.value, "")
            if previous is not None and previous.run_id == run_id
            else ""
        ),
        previous_child_key=(
            previous.last_child_key if previous is not None and previous.run_id == run_id else ""
        ),
        previous_journal_cursor=(
            (previous.evidence_cursors or {}).get(_SnapshotSource.SUPERVISOR_JOURNAL.value, "")
            if previous is not None and previous.run_id == run_id
            else ""
        ),
        observed_at=now,
    )
    # The one fact the reduction cannot observe: whether THIS dispatcher is the one holding
    # the process on a stop signal. `worker_continuation.retained` is that intent, written
    # only after `host.retain_worker` confirmed the suspension, so a parked process reduces
    # to `Retained` rather than to `Suspended` and no recovery rung is ever earned over it
    # (secretary-1539). The review head has no retention of its own, so it always passes
    # False and its ladder is untouched.
    retained = kind == "worker" and bool(record.worker_continuation.retained)
    # The other declared input (secretary-1543, widened by secretary-1739): the instant the
    # dispatcher put the question this head has not answered. The wait tick passes it for both
    # roles; any other caller gets the bounced-report stamp alone, which the gate phase has
    # cleared, so its ladder is untouched.
    if answer_owed_since is None:
        answer_owed_since = float(record.worker_answer_owed_since or 0.0) if kind == "worker" else 0.0
    try:
        episode = _reduce_vitality(
            previous,
            snapshots,
            now,
            _DEFAULT_VITALITY_THRESHOLDS,
            retained=retained,
            answer_owed_since=answer_owed_since,
            adapter=_run_adapter(run_payload),
        )
    except Exception as exc:  # noqa: BLE001 - shadow mode must never break the hosting tick
        # Shadow mode may never break the tick that hosts it. A reduction failure is recorded
        # as no episode so the next tick starts clean, and nothing downstream changes --
        # including the decision, which then falls back to the outer ceilings.
        runtime.writer.comment(
            role="dispatcher",
            actor=runtime.owner,
            reference=task["ref"],
            body=f"Vitality shadow reduction failed and was skipped: {scrub_host_output(str(exc))[:160]}",
            request_id=_attempt_request_id(
                record.attempt_id or "",
                f"{kind}-vitality-error",
                task["ref"],
                suffix=_request_token(str(now)),
            ),
        )
        return None
    changed = previous is None or previous.verdict is not episode.verdict
    setattr(record, field_name, episode)
    records[task["ref"]] = record
    runtime.save_records(payload, records)
    if not changed:
        return episode
    # The request id names only what the comment claims -- the verdict transition itself --
    # so a flapping verdict cannot mint a fresh idempotency key every tick and turn shadow
    # logging into an unbounded comment stream.
    #
    # THE BODY IS A FUNCTION OF EXACTLY WHAT THE KEY NAMES (secretary-1477). The board's
    # identity for a comment is the digest of its body, so a stable key over a body that
    # moved is not an idempotent replay: it is
    # `validation: request id belongs to another operation or payload`, raised out of the
    # wait tick before it ever decides, costing the card its whole per-card advance for
    # that tick. This body therefore says only what `(kind, prev->cur)` already fixes.
    # The live measurement is NOT dropped, only unquoted here: the reduction's `basis` --
    # `quiet:<n>s@<source>`, `advisory:<active|idle>@pane_advisory` and the rest -- is
    # persisted on the episode this method just saved (`record.{kind}_vitality_episode`
    # in `dispatcher/production-state.json`) and is reported verbatim by
    # `ummanu head-status` (`dispatch/head_status.py`, the row's `episode.basis`).
    # Anything a future edit wants to add here has to enter the suffix above with it.
    request_id = _attempt_request_id(
        record.attempt_id or "",
        f"{kind}-vitality-verdict",
        task["ref"],
        suffix=_request_token(
            f"{previous.verdict.value if previous else 'none'}->{episode.verdict.value}"
        ),
    )
    runtime.writer.comment(
        role="dispatcher",
        actor=runtime.owner,
        reference=task["ref"],
        body=(
            f"Vitality ({kind}): {episode.verdict.value}"
            + (f" (was {previous.verdict.value})" if previous is not None else " (first observation)")
            + ". The basis and the live measurement behind this verdict stay on the"
            + " durable vitality episode; read them with `ummanu head-status`."
            + (
                ""
                if episode.verdict in DESTRUCTIVE_VERDICTS
                else " Recorded only - does not authorise destruction."
            )
        ),
        request_id=request_id,
    )
    return episode


def _trigger_wait_watchdog(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    *,
    kind: str,
    trigger: str,
    stall: int | None = None,
    degraded: bool = False,
) -> dict[str, Any]:
    """The verdict-driven recovery entry point (S1-4): respawn once, then escalate.

    Reached ONLY from decisions the persisted vitality episode drove -- a ``Dead``
    or ``ConfirmedStall`` verdict in the wait tick. The destructive step itself is
    fenced by the vitality guard inside ``_guard_or_wait`` before
    ``_respawn_wait``/``_escalate_wait`` run, so a stale episode, a foreign run or a
    verdict that does not authorise destruction turns into a visible wait instead of
    a stop.
    """
    action = (
        f"{kind}-escalate" if int(getattr(record, f"{kind}_respawns") or 0) >= 1 else f"{kind}-respawn"
    )
    return _guard_or_wait(runtime,
        task,
        record,
        records,
        payload,
        attempt_id,
        kind=kind,
        now=time.time(),
        action=action,
        proceed=lambda: (
            _respawn_wait(runtime,
                task,
                record,
                records,
                payload,
                attempt_id,
                kind=kind,
                now=time.time(),
                trigger=trigger,
                degraded=degraded,
            )
            if action == f"{kind}-respawn"
            else _escalate_wait(runtime,
                task,
                record,
                records,
                payload,
                attempt_id,
                kind=kind,
                stall=_stall_seconds(kind) if stall is None else stall,
                trigger=trigger,
            )
        ),
    )


def _respawn_wait(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    *,
    kind: str,
    now: float,
    trigger: str,
    degraded: bool = False,
) -> dict[str, Any]:
    # The successor is told which command its predecessor was stopped in (secretary-1692), read
    # from the stopped run's own episode before the bring-up replaces that run. The note rides
    # the record only for the bring-up below and is never persisted.
    record.respawn_interrupted_command = _interrupted_command_note(
        getattr(record, f"{kind}_vitality_episode"),
        str((record.review_head_run if kind == "review" else record.worker_head_run).get("run_id") or ""),
    )
    try:
        return _respawn_wait_bring_up(
            runtime,
            task,
            record,
            records,
            payload,
            attempt_id,
            kind=kind,
            now=now,
            trigger=trigger,
            degraded=degraded,
        )
    finally:
        record.respawn_interrupted_command = ""


def _respawn_wait_bring_up(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    *,
    kind: str,
    now: float,
    trigger: str,
    degraded: bool = False,
) -> dict[str, Any]:
    ref = task["ref"]
    step = "review" if kind == "review" else "advance"
    if kind == "review":
        # Only the reviewer is stalled; its pane goes and the workspace stays. A stall is not a
        # death, so an unconfirmed stop ends the tick rather than adding a second reviewer.
        unconfirmed = runtime._end_review_pane_confirmed(
            record,
            records,
            payload,
            ref,
            step=step,
            attempt_id=attempt_id,
            initiator=STOPPED_BY_WATCHDOG,
        )
        if unconfirmed is not None:
            return unconfirmed
        # One bring-up path for the reviewer, shared with the normal launch and the recovery path.
        outcome = _start_review(
            runtime, task, records, record, attempt_id, action="review-respawned", payload=payload
        )
        if outcome.get("status") != "ok":
            runtime.save_records(payload, records)
            return outcome
    else:
        # Same as the reviewer above: a silent worker is not a dead one.
        unconfirmed = runtime._stop_worker_confirmed(record, ref, step=step, attempt_id=attempt_id)
        if unconfirmed is not None:
            return unconfirmed
        failure = _write_worker_relaunch_intent(
            runtime, payload, records, ref, record, action="worker-respawn", task=task
        )
        if failure is not None:
            return _launch_intent_unwritable(
                step=step,
                ref=ref,
                attempt_id=record.attempt_id or attempt_id,
                role=WORKER_ROLE,
                reason=failure,
            )
        launched, failed = _bring_up_worker_head(runtime,
            task,
            record,
            records,
            payload,
            attempt_id,
            step=step,
            stage=STAGE_RESPAWN,
            blocked_reason="worker respawn failed",
            blocked_action="worker-respawn-blocked",
            blocked_request_suffix=_wait_cycle_token(record),
        )
        if launched is None:
            assert failed is not None
            if failed.get("status") == "blocked":
                # The respawn itself failed and the card went Blocked: the dead head stays dead.
                owner_events.record(
                    owner_events.HEAD_DEAD,
                    ref,
                    f"The worker head of {ref} was not relaunched: its respawn failed ({trigger}); "
                    f"the card is Blocked: {failed.get('reason') or 'worker respawn failed'}",
                    f"{owner_events.HEAD_DEAD}:{ref}:{record.attempt_id or attempt_id}:respawn:"
                    f"{_wait_cycle_token(record)}",
                    to=getattr(getattr(runtime, "reader", None), "client", None),
                )
            return failed
        record.state = "claimed"
        # The replacement head never saw the bounced report, so it owes no answer for it.
        record.worker_answer_owed_since = 0.0
        # A respawn is a real bring-up: a repinned profile lands a different configuration.
        runtime.record_worker_routing(task, record, launched.run)
        _clear_launch_intent(record)
        record.worker_started_at = record.worker_progress_at = now
    if kind == "review":
        record.review_started_at = record.review_progress_at = now
    # Persist the restart before commenting: there is no try/except here, so a raising comment
    # would escape with the head respawned and respawns still 0, and the escalation never comes.
    setattr(record, f"{kind}_waiting_since", now)
    # The replacement head owns its own readiness; it is not charged with what it replaces.
    _reset_idle(record, kind)
    respawns = int(getattr(record, f"{kind}_respawns") or 0) + 1
    setattr(record, f"{kind}_respawns", respawns)
    records[ref] = record
    runtime.save_records(payload, records)
    # Leave a trace, or the operator cannot tell a first stall from an already-restarted head.
    runtime.writer.comment(
        role="dispatcher",
        actor=runtime.owner,
        reference=ref,
        body=(
            f"Dispatcher wait watchdog: {trigger}, "
            f"respawned the {kind} head (respawn {respawns})."
            + (
                " The report round did not move: the same TASK.md is back in the checkout, "
                f"with the report commands for generation {record.report_generation}."
                if kind == "worker"
                else ""
            )
            + " Another stall escalates to Blocked."
        ),
        request_id=_attempt_request_id(
            record.attempt_id or attempt_id,
            f"{kind}-respawn",
            ref,
            f"{_wait_cycle_token(record)}-{respawns}",
        ),
    )
    return {
        # A head that is alive, idle and has delivered nothing is the pipeline failing to move a
        # card: `degraded` is what puts it in the telemetry an operator and the steward read.
        "status": "degraded" if degraded else "ok",
        "step": step,
        "pilot_ref": ref,
        "attempt_id": attempt_id,
        "action": f"{kind}-respawned",
        **({"reason": trigger} if degraded else {}),
    }


def _escalate_wait(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    *,
    kind: str,
    stall: int,
    trigger: str,
) -> dict[str, Any]:
    ref = task["ref"]
    step = "review" if kind == "review" else "advance"
    if kind == "review":
        # The reviewer may still hold the checkout when its second stall escalates. End it
        # through the same confirmed boundary; a refused stop leaves the record for the retry.
        unconfirmed = runtime._end_review_pane_confirmed(
            record,
            records,
            payload,
            ref,
            step=step,
            attempt_id=attempt_id,
            initiator=STOPPED_BY_WATCHDOG,
        )
        if unconfirmed is not None:
            return unconfirmed
        # Review starts over a retained worker: settle that role before dropping the record.
        unconfirmed = runtime._stop_worker_confirmed(record, ref, step=step, attempt_id=attempt_id)
    else:
        unconfirmed = runtime._stop_worker_confirmed(record, ref, step=step, attempt_id=attempt_id)
    if unconfirmed is not None:
        return unconfirmed
    request_id = _attempt_request_id(
        record.attempt_id or attempt_id, f"{kind}-wait-stall", ref, _wait_cycle_token(record)
    )
    attempt_accounting.terminal_effect(runtime, 
        task,
        record,
        target="blocked",
        reason=(f"wait watchdog: {trigger} after respawn (ceiling {stall}s), blocked for the operator"),
        request_id=request_id,
        terminal_state="blocked",
        disposition="blocked",
        blocked_reason="operator",
    )
    # The one place a worker or reviewer head is given up on: it already had its one respawn, so
    # nothing relaunches it and the card waits in Blocked for the operator.
    owner_events.record(
        owner_events.HEAD_DEAD,
        ref,
        f"The {'reviewer' if kind == 'review' else 'worker'} head of {ref} was not relaunched after its "
        f"respawn ({trigger}); the card is Blocked for the operator",
        f"{owner_events.HEAD_DEAD}:{request_id}",
        to=getattr(getattr(runtime, "reader", None), "client", None),
    )
    records.pop(ref, None)
    runtime.save_records(payload, records)
    return {"status": "ok", "step": step, "pilot_ref": ref, "attempt_id": attempt_id, "to": "blocked"}


def _wait_expectation(kind: str) -> str:
    return "review verdict" if kind == "review" else "worker report"
