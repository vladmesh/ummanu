"""Retained/red worker continuation and its bounded delivery recovery.

This module owns the durable red intent, board transition, continuation delivery and
confirmed-stop replacement handoff. Models remain in worker_lifecycle; launches remain in
worker_launch. Gate/review decisions and shared wait/vitality policy retain their owners.
The runtime is a collaborator, not an implementation facade, as in worker_report.
"""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Any

from ummanu.dispatch import attempt_accounting
from ummanu.dispatch.gate import reset_infrastructure_reruns as _reset_infrastructure_reruns
from ummanu.dispatch.helpers import scrub_host_output
from ummanu.dispatch.host import _record_worker_delivery_evidence
from ummanu.dispatch.launch import STAGE_REWORK, WORKER_ROLE
from ummanu.dispatch.launch import clear_launch_intent as _clear_launch_intent
from ummanu.dispatch.launch import launch_intent_unwritable as _launch_intent_unwritable
from ummanu.dispatch.state import DispatcherRecord, PersistedGateReceipt, now_rfc3339
from ummanu.dispatch.state import attempt_request_id as _attempt_request_id
from ummanu.dispatch.tui import COMPOSER_EMPTY, COMPOSER_UNKNOWN, READINESS_BUSY
from ummanu.dispatch.tui import delivery_readiness_state as _delivery_readiness_state
from ummanu.dispatch.types import HostError
from ummanu.dispatch.watchdog import reset_wait as _reset_wait
from ummanu.dispatch.worker_launch import bring_up_worker_head as _bring_up_worker_head
from ummanu.dispatch.worker_launch import write_worker_relaunch_intent as _write_worker_relaunch_intent
from ummanu.dispatch.worker_lifecycle import (
    BUSY_RETRY_INITIAL_SECONDS,
    CONTINUATION_NO_PROGRESS_BUSY_ATTEMPTS,
    ContinuationLivenessState,
    ContinuationProviderCondition,
    ContinuationRecoveryRung,
    WorkerContinuationLiveness,
)


def recover_worker_continuation(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    *,
    marker: str | None,
) -> dict[str, Any] | None:
    """Recover delivery after report lookup, before report consumption or shared wait policy.

    A terminal marker can prove delivery. Red-transition replay has a separate entry point
    because it must run before looking up a report from the newly reserved generation.
    """
    ref = task["ref"]
    continuation = record.worker_continuation
    if continuation.delivery_pending:
        if marker in {"report:done", "report:blocked"}:
            # A report after the resume phase opened proves the continuation reached the retained
            # conversation: do not rewrite TASK.md or replay the prompt over a completed turn.
            continuation.confirm_delivery()
            records[ref] = record
            runtime.save_records(payload, records)
            return _finish_retained_worker_resume(
                runtime,
                task,
                record,
                records,
                payload,
                attempt_id,
                phase=continuation.phase or "gate",
            )
        # Progress is sampled before the persisted readiness backoff is interpreted. A new
        # provider cursor beats a busy pane and resets only that ladder, never the HeadRun.
        now = time.time()
        provider_observation = _observe_retained_continuation_progress(runtime, task, record, now=now)
        blocked = _block_unadmitted_continuation_liveness(
            runtime,
            task,
            record,
            records,
            payload,
            attempt_id,
            phase=continuation.phase or "gate",
            observation=provider_observation,
        )
        if blocked is not None:
            return blocked
        fresh_provider_progress = provider_observation == "progressed"
        pending = _continuation_recovery_window(
            runtime,
            task,
            record,
            records,
            payload,
            attempt_id,
            phase=continuation.phase or "gate",
            fresh_provider_progress=fresh_provider_progress,
            now=now,
        )
        if pending is not None:
            return pending
        records[ref] = record
        runtime.save_records(payload, records)
        if (
            fresh_provider_progress
            and record.worker_continuation_liveness.recovery_rung
            != ContinuationRecoveryRung.SAFE_RECOVERY_RESUME_ONCE
        ):
            continuation.busy_next_at = now + BUSY_RETRY_INITIAL_SECONDS
            return _retained_worker_busy_deferred(
                ref,
                record,
                attempt_id,
                continuation.phase or "gate",
                delay=BUSY_RETRY_INITIAL_SECONDS,
            )
        if not continuation.busy_retry_due(time.time()):
            return _retained_worker_busy_deferred(ref, record, attempt_id, continuation.phase or "gate")
        # Nothing is woken from here: the suspension is a fact of the tick that died, and
        # re-entering the transition is what asks the heartbeat again before reopening.
        return _deliver_red_continuation(
            runtime, task, record, records, payload, attempt_id, phase=continuation.phase or "gate"
        )
    if continuation.delivery_confirmed:
        # The delivery was checkpointed and the tick died before the round it opened was
        # recorded; finishing it again keeps the rework off the round the verdict closed.
        return _finish_retained_worker_resume(
            runtime, task, record, records, payload, attempt_id, phase=continuation.phase or "gate"
        )
    return None


def begin_red_transition(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    *,
    phase: str,
    move_reason: str,
    verdict_outcome: str,
    decision: str = "",
    decision_body: str = "",
    decision_protocol_prerequisites: tuple[str, ...] = (),
) -> dict[str, Any]:
    """The only way a card goes back to In progress for rework.

    The order lives here and nowhere else: the intent is on disk, with its phase, the report
    baseline it was opened against and the reason the card is moving, before anything observable
    moves; the board moves; and only then is it decided whether the round's own session takes the
    continuation or a replacement does. Holding a session is deliberately not a precondition.
    """
    ref = task["ref"]
    baseline = len(task.get("comments") or [])
    # The round this transition opens is reserved here, with the intent and before the move:
    # completion must read that generation rather than compute it, or a re-entered completion
    # hands one rework round two generations. The observer's instruction is frozen in the same
    # write, so what the round is for cannot be re-read from a newer decision comment.
    record.worker_continuation.begin_red_transition(
        phase,
        baseline,
        move_reason,
        verdict_outcome,
        decision,
        reserved_generation=record.report_generation + 1,
        decision_body=decision_body,
        decision_protocol_prerequisites=decision_protocol_prerequisites,
    )
    records[ref] = record
    runtime.save_records(payload, records)
    return complete_red_transition(runtime, task, record, records, payload, attempt_id, ref=ref)


def complete_red_transition(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    *,
    ref: str,
) -> dict[str, Any]:
    """Finish the open red transition from the board as it is now.

    The move is keyed on the baseline the intent was opened against, so the tick that already moved
    the card and the tick recovering from a crash before that move run the same call and the card
    moves once. Nothing here re-reads the verdict: the transition carries its own reason.
    """
    continuation = record.worker_continuation
    phase = continuation.phase or "gate"
    baseline = continuation.report_baseline
    if not continuation.decision:
        # A transition performing a decision is the second half of a round whose verdict was
        # already recorded at the park; recording it again would overwrite that outcome.
        runtime._record_verdict_routing(ref, record, continuation.verdict_outcome)
    attempt_accounting.terminal_effect(runtime, 
        task,
        record,
        target="in_progress",
        reason=continuation.move_reason,
        # The board refuses to take a card out of Assessment without a decision; a red gate
        # moving out of Validate carries none and is refused nothing.
        decision=continuation.decision,
        request_id=_attempt_request_id(record.attempt_id or attempt_id, f"{phase}-red", ref, str(baseline)),
        terminal_state="in_progress",
        disposition="rework",
        verdict=(
            "red"
            if continuation.verdict_outcome.endswith("_red")
            else continuation.verdict_outcome
            if continuation.verdict_outcome in {"green", "red"}
            else "missing"
        ),
    )
    moved = runtime.reader.show(ref)
    # The previous round's report stays behind this baseline, so no tick reads it as this one's.
    record.comment_baseline = max(len(moved.get("comments") or []), baseline)
    # Where the next verdict is scanned from, so the one just acted on is not read again.
    record.review_baseline = record.comment_baseline
    # The rework's generation is the one this transition reserved before the move: assigned,
    # never advanced. A legacy transition without a reservation falls back to the advance it
    # was written with.
    previous_generation = record.report_generation
    record.report_generation = continuation.reserved_generation or record.report_generation + 1
    if continuation.retained:
        successor = getattr(runtime.host, "retained_local_run_snapshot_successor", None)
        if callable(successor):
            record.worker_local_run_snapshot = successor(
                moved, previous_generation, record.report_generation,
                record.worker_local_run_snapshot,
            )
    # And the instruction that round is opened on, from the same transition. Always assigned,
    # never merged: a red gate has no decision, and inheriting the prior round's would hand a
    # worker an adjudication of review findings its code has already answered.
    record.report_decision = continuation.decision_body
    record.report_protocol_prerequisites = continuation.decision_protocol_prerequisites
    record.gate_state = ""
    record.gate_pending_since = 0.0
    record.gate_attestation = PersistedGateReceipt()
    record.gate_transport_failures = 0
    record.gate_transport_error = ""
    _reset_infrastructure_reruns(record)
    # The judged round ends here: a stale review pin would refuse the rework's merge.
    record.review_commit = ""
    _reset_wait(record, "review")
    _reset_wait(record, "worker")
    records[ref] = record
    runtime.save_records(payload, records)
    return _deliver_red_continuation(runtime, moved, record, records, payload, attempt_id, phase=phase)


def _deliver_red_continuation(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    *,
    phase: str,
) -> dict[str, Any]:
    """Hand a red verdict back to the session that wrote the code, or to one replacement.

    The order is the same for the gate and for the review: the suspension is re-confirmed at the
    moment of use, the delivery boundary is durable before the worker is woken, and every way out
    that cannot reuse the session goes through a confirmed stop first.
    """
    ref = task["ref"]
    continuation = record.worker_continuation
    step = "review" if phase == "review" else "gate"
    opening_delivery = not continuation.delivery_pending
    fresh_provider_progress = False
    if continuation.delivery_pending:
        now = time.time()
        provider_observation = _observe_retained_continuation_progress(runtime, task, record, now=now)
        blocked = _block_unadmitted_continuation_liveness(
            runtime,
            task,
            record,
            records,
            payload,
            attempt_id,
            phase=phase,
            observation=provider_observation,
        )
        if blocked is not None:
            return blocked
        fresh_provider_progress = provider_observation == "progressed"
        pending = _continuation_recovery_window(
            runtime,
            task,
            record,
            records,
            payload,
            attempt_id,
            phase=phase,
            fresh_provider_progress=fresh_provider_progress,
            now=now,
        )
        if pending is not None:
            return pending
        if (
            record.worker_continuation_liveness.recovery_rung
            == ContinuationRecoveryRung.SAFE_RECOVERY_RESUME_ONCE
        ):
            if not record.worker_continuation_liveness.allow_safe_recovery_resume_once():
                record.worker_continuation_liveness.terminalize(
                    "replacement", "safe recovery resume was already spent"
                )
                records[ref] = record
                runtime.save_records(payload, records)
                return _restart_red_worker(
                    runtime,
                    task,
                    record,
                    records,
                    payload,
                    attempt_id,
                    continuation_reason="safe recovery resume was already spent",
                    phase=phase,
                )
            # A once-only capability: persist spending it before delivery touches the pane.
            records[ref] = record
            runtime.save_records(payload, records)
    if continuation.retained:
        try:
            # The suspension was confirmed on a past tick; a SIGCONT from terminal recovery or
            # an operator since makes this a second writer. Ask the heartbeat again here.
            runtime.host.confirm_worker_retained(record)
        except HostError as exc:
            reason = scrub_host_output(str(exc))
            unconfirmed = runtime._stop_worker_confirmed(record, ref, step=step, attempt_id=attempt_id)
            if unconfirmed is not None:
                return unconfirmed
            return _restart_red_worker(
                runtime,
                task,
                record,
                records,
                payload,
                attempt_id,
                continuation_reason=reason,
                phase=phase,
                worker_stopped=True,
            )
        if opening_delivery:
            # Persist the delivery boundary before waking the worker, or a tick that dies after
            # delivery replays with the old done marker read as the new round's completion.
            continuation.begin_delivery(phase, time.time())
            record.worker_continuation_liveness = WorkerContinuationLiveness.begin(record.worker_head_run)
            # Establish the provider cursor before SIGCONT: the first observation is a baseline.
            provider_observation = _observe_retained_continuation_progress(
                runtime, task, record, now=time.time()
            )
            blocked = _block_unadmitted_continuation_liveness(
                runtime,
                task,
                record,
                records,
                payload,
                attempt_id,
                phase=phase,
                observation=provider_observation,
            )
            if blocked is not None:
                return blocked
            fresh_provider_progress = provider_observation == "progressed"
            records[ref] = record
            runtime.save_records(payload, records)
            pending = _continuation_recovery_window(
                runtime,
                task,
                record,
                records,
                payload,
                attempt_id,
                phase=phase,
                fresh_provider_progress=fresh_provider_progress,
                now=time.time(),
            )
            if pending is not None:
                return pending
        else:
            # An already-open boundary: recreating liveness would make no-progress unbounded.
            records[ref] = record
            runtime.save_records(payload, records)
        try:
            # A restarted dispatcher has no process-local ingress. Install from the durable run
            # with the runtime's usual persist/stop/block callbacks before this delivery.
            runtime.bind_codex_provider_ingress(
                record, records, payload, role="worker", reference=ref
            )
            runtime.host.resume_worker(task, record)
        except HostError as exc:
            if _delivery_readiness_state(exc) == READINESS_BUSY:
                # The boundary saw an owned pane working before it sent anything: neither
                # acknowledgement nor a dead-head vote, so keep the continuation and retry.
                _record_worker_delivery_evidence(record, exc)
                liveness = record.worker_continuation_liveness
                # The pre-read provider cursor is the precedence rule: a fresh rollout keeps this
                # HeadRun and only restarts its no-progress ladder. A busy `tui-idle` does not.
                if fresh_provider_progress:
                    continuation.busy_attempts = liveness.busy_attempts
                    continuation.busy_next_at = time.time() + BUSY_RETRY_INITIAL_SECONDS
                    records[ref] = record
                    runtime.save_records(payload, records)
                    return _retained_worker_busy_deferred(
                        ref,
                        record,
                        attempt_id,
                        phase,
                        delay=BUSY_RETRY_INITIAL_SECONDS,
                    )
                if liveness.state != ContinuationLivenessState.STALLED:
                    # The first exact-source cursor is a persisted baseline, not evidence of a
                    # stall: keep the head and schedule, spending no no-progress attempt.
                    continuation.busy_next_at = time.time() + BUSY_RETRY_INITIAL_SECONDS
                    records[ref] = record
                    runtime.save_records(payload, records)
                    return _retained_worker_busy_deferred(
                        ref,
                        record,
                        attempt_id,
                        phase,
                        delay=BUSY_RETRY_INITIAL_SECONDS,
                    )
                liveness.no_progress_evidence = _continuation_no_progress_evidence(record, liveness.state)
                liveness.note_busy(time.time())
                continuation.busy_attempts = max(0, liveness.busy_attempts - 1)
                delay = continuation.defer_busy(time.time())
                # `defer_busy` owns the persisted retry deadline, liveness owns the bounded
                # episode count: keep them in sync, never beyond this HeadRun's evidence.
                continuation.busy_attempts = liveness.busy_attempts
                records[ref] = record
                runtime.save_records(payload, records)
                bounded = _advance_no_progress_continuation(
                    runtime,
                    task,
                    record,
                    records,
                    payload,
                    attempt_id,
                    phase=phase,
                )
                if bounded is not None:
                    return bounded
                return _retained_worker_busy_deferred(ref, record, attempt_id, phase, delay=delay)
            _record_worker_delivery_evidence(record, exc, failure=True)
            records[ref] = record
            runtime.save_records(payload, records)
            return _restart_red_worker(
                runtime,
                task,
                record,
                records,
                payload,
                attempt_id,
                continuation_reason=scrub_host_output(str(exc)),
                phase=phase,
            )
        continuation.confirm_delivery()
        records[ref] = record
        runtime.save_records(payload, records)
        return _finish_retained_worker_resume(
            runtime, task, record, records, payload, attempt_id, phase=phase
        )
    # Same reservation as the retained branch: the rework round is fixed on disk with the
    # intent, so adoption resumes it rather than the round the verdict closed.
    return _restart_red_worker(
        runtime,
        task,
        record,
        records,
        payload,
        attempt_id,
        continuation_reason="no retained worker session was available",
        phase=phase,
    )


def _observe_retained_continuation_progress(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    *,
    now: float,
) -> str:
    """Persist provider progress before a continuation interprets `tui-idle`."""
    try:
        evidence = getattr(
            runtime.host,
            "provider_progress",
            lambda _task, _record, _kind: {
                "state": "unavailable",
                "reason": "host has no provider-progress probe",
            },
        )(task, record, "worker")
    except Exception as exc:  # noqa: BLE001 - evidence must retain any host refusal.
        evidence = {
            "state": "unavailable",
            "reason": f"provider-progress probe failed: {scrub_host_output(str(exc))}",
        }
    liveness = record.worker_continuation_liveness
    if not liveness.bound and record.worker_continuation.busy_attempts:
        # An old busy count is audit data, never an exact-source observation for the ladder.
        liveness.legacy_busy_attempts = max(
            liveness.legacy_busy_attempts,
            record.worker_continuation.busy_attempts,
        )
    observation = liveness.observe_provider(evidence, now, head_run=record.worker_head_run)
    if liveness.admitted:
        record.worker_continuation.busy_attempts = liveness.busy_attempts
    if observation == "progressed":
        record.worker_continuation.busy_next_at = 0.0
    return observation


def _block_unadmitted_continuation_liveness(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    *,
    phase: str,
    observation: str,
) -> dict[str, Any] | None:
    """Take the explicit safe outcome when the liveness trust boundary is unprovable."""
    if observation in {"baseline", "stalled", "progressed"} and record.worker_continuation_liveness.admitted:
        return None
    if observation == ContinuationProviderCondition.LEGACY_UNBOUND_V1.value:
        return _restart_red_worker(
            runtime,
            task,
            record,
            records,
            payload,
            attempt_id,
            continuation_reason="Codex provider source remained legacy-unbound for v1 progress",
            phase=phase,
        )
    ref = task["ref"]
    reason = record.worker_continuation_liveness.reason or "provider source was not admitted"
    attempt_accounting.terminal_effect(runtime, 
        task,
        record,
        target="blocked",
        reason=(
            "retained continuation liveness is unprovable; preserving the exact HeadRun "
            f"without recovery: {reason}"
        ),
        request_id=_attempt_request_id(
            record.attempt_id or attempt_id,
            "continuation-liveness-unavailable",
            ref,
            phase,
        ),
        terminal_state="blocked",
        disposition="blocked",
        blocked_reason="provider",
    )
    records[ref] = record
    runtime.save_records(payload, records)
    return {
        "status": "blocked",
        "step": "review" if phase == "review" else "gate",
        "pilot_ref": ref,
        "attempt_id": attempt_id,
        "action": f"{phase}-red-continuation-liveness-unavailable",
    }


def _continuation_recovery_window(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    *,
    phase: str,
    fresh_provider_progress: bool,
    now: float,
) -> dict[str, Any] | None:
    """Honor the recorded safe-recovery response window before another pane interaction."""
    liveness = record.worker_continuation_liveness
    ref = task["ref"]
    if liveness.terminal_outcome == "identity_fenced":
        # The stop path is the only component allowed to resolve this: it either confirms the
        # old HeadRun stopped and launches one replacement, or refuses. Neither takes a pane.
        return _restart_red_worker(
            runtime,
            task,
            record,
            records,
            payload,
            attempt_id,
            continuation_reason="continuation liveness HeadRun identity is fenced",
            phase=phase,
        )
    if liveness.recovery_rung != ContinuationRecoveryRung.SAFE_RECOVERY_RESPONSE_WINDOW:
        return None
    if now < liveness.recovery_response_deadline:
        records[ref] = record
        runtime.save_records(payload, records)
        return _retained_worker_recovery_window(
            ref,
            record,
            attempt_id,
            phase,
            remaining=max(0, int(liveness.recovery_response_deadline - now)),
        )
    if fresh_provider_progress:
        liveness.recovery_rung = ContinuationRecoveryRung.SAFE_RECOVERY_RESUME_ONCE
        records[ref] = record
        runtime.save_records(payload, records)
        return None
    liveness.terminalize("replacement", "safe recovery response window showed no provider progress")
    records[ref] = record
    runtime.save_records(payload, records)
    return _restart_red_worker(
        runtime,
        task,
        record,
        records,
        payload,
        attempt_id,
        continuation_reason="safe recovery response window showed no provider progress",
        phase=phase,
    )


def _advance_no_progress_continuation(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    *,
    phase: str,
) -> dict[str, Any] | None:
    """Spend the sole safe-recovery rung, then take one identity-fenced terminal outcome."""
    liveness = record.worker_continuation_liveness
    if not liveness.admitted or liveness.state != ContinuationLivenessState.STALLED:
        return _block_unadmitted_continuation_liveness(
            runtime,
            task,
            record,
            records,
            payload,
            attempt_id,
            phase=phase,
            observation=liveness.state.value,
        )
    if liveness.busy_attempts < CONTINUATION_NO_PROGRESS_BUSY_ATTEMPTS:
        return None
    if liveness.recovery_rung == ContinuationRecoveryRung.SAFE_RECOVERY_RESPONSE_WINDOW:
        return None
    if liveness.recovery_rung == ContinuationRecoveryRung.SAFE_RECOVERY_RESUME_ONCE:
        # The recovery's one authorised return to ordinary delivery has already been spent.
        if liveness.recovery_resume_used:
            liveness.terminalize("replacement", "safe recovery resume was already spent")
        else:
            return None
    if liveness.recovery_rung == ContinuationRecoveryRung.SAFE_RECOVERY_PENDING:
        # The intent was durable before the capability was called, and after a crash there we
        # cannot tell whether the provider acted. Spend the safe rung rather than retry it.
        liveness.terminalize(
            "replacement", "safe recovery response was unconfirmed after dispatcher recovery"
        )
        records[task["ref"]] = record
        runtime.save_records(payload, records)
    if not liveness.terminal:
        # Intent first: a death inside the capability must not make the next process retry it.
        liveness.begin_safe_recovery(time.time())
        records[task["ref"]] = record
        runtime.save_records(payload, records)
        try:
            result = getattr(
                runtime.host,
                "safe_recover_worker_continuation",
                lambda *_args: {
                    "state": "unavailable",
                    "reason": "host has no provider/terminal-safe recovery capability",
                },
            )(task, record, liveness.to_json())
        except Exception as exc:  # noqa: BLE001 - evidence must retain any host refusal.
            result = {"state": "unavailable", "reason": scrub_host_output(str(exc))}
        valid_recovery = (
            isinstance(result, dict)
            and str(result.get("state") or "") == "recovered"
            and bool(result.get("safe"))
            and str(result.get("head_run_id") or "") == liveness.head_run_id
        )
        if valid_recovery:
            # The only extension point for a future provider API: its response is recorded
            # before waiting, and it cannot tunnel a raw interrupt through a terminal command.
            liveness.safe_recovery_response_window(time.time(), 30.0)
            records[task["ref"]] = record
            runtime.save_records(payload, records)
            return _retained_worker_recovery_window(
                task["ref"],
                record,
                attempt_id,
                phase,
                remaining=30,
            )
        reason = (
            str(result.get("reason") or "safe recovery capability is unavailable")
            if isinstance(result, dict)
            else "safe recovery capability returned an invalid shape"
        )
        liveness.recovery_rung = ContinuationRecoveryRung.SAFE_RECOVERY_UNAVAILABLE
        liveness.terminalize("replacement", f"safe recovery unavailable: {reason}")
        records[task["ref"]] = record
        runtime.save_records(payload, records)
    return _restart_red_worker(
        runtime,
        task,
        record,
        records,
        payload,
        attempt_id,
        continuation_reason=(
            "provider progress remained absent after bounded continuation recovery: "
            f"{record.worker_continuation_liveness.reason}"
        ),
        phase=phase,
    )


def _finish_retained_worker_resume(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    *,
    phase: str,
) -> dict[str, Any]:
    ref = task["ref"]
    step = "review" if phase == "review" else "gate"
    if record.worker_continuation_liveness.bound:
        record.worker_continuation_liveness.terminalize(
            "reused", "retained continuation delivery was confirmed"
        )
    record.worker_continuation.clear()
    record.state = "claimed"
    rework_round = record.attempt_round + 1
    retained_run = dict(record.worker_run)
    runtime.open_worker_round(record, round_number=rework_round)
    runtime.record_worker_routing(task, record, retained_run)
    attempt_accounting.persist_outcome_round_context(runtime, task, record, phase="worker")
    _record_worker_continuation(runtime, ref, record, "reused", phase, "retained worker resumed")
    record.worker_started_at = record.worker_progress_at = time.time()
    records[ref] = record
    runtime.save_records(payload, records)
    return {
        "status": "ok",
        "step": step,
        "pilot_ref": ref,
        "attempt_id": attempt_id,
        "action": f"{phase}-red-reused-worker",
    }


def _restart_red_worker(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    *,
    continuation_reason: str,
    phase: str,
    worker_stopped: bool = False,
) -> dict[str, Any]:
    """Launch the red-verdict fallback only after its worker was conclusively stopped."""
    ref = task["ref"]
    review = phase == "review"
    step = "review" if review else "gate"
    blocked_kind = "rework-blocked" if review else f"{phase}-red-blocked"
    action = "rework-started" if review else f"{phase}-red-rework"
    continuation = "replacement"
    if record.worker_continuation_liveness.bound and not record.worker_continuation_liveness.terminal:
        record.worker_continuation_liveness.terminalize("replacement", continuation_reason)
    # Unconditional on purpose: a record written by an older dispatcher, or adopted after a
    # crash, may lack the retained timestamp while its worker lives. Ambiguity is no permission.
    if not worker_stopped:
        unconfirmed = runtime._stop_worker_confirmed(record, ref, step=step, attempt_id=attempt_id)
        if unconfirmed is not None:
            return unconfirmed
    rework_round = record.attempt_round + 1
    # The launch intent takes the transition over from here: it is durable, reserves the rework
    # round, and recovery adopts or relaunches exactly one head. Hand it over in the same write,
    # or both can owe this card a worker. The handover is real only on disk: restoring the held
    # transition after a failed intent write keeps In progress from having no durable worker debt.
    held_transition = replace(record.worker_continuation)
    record.worker_continuation.clear()
    failure = _write_worker_relaunch_intent(
        runtime, payload, records, ref, record, action=f"{phase}-red-rework", round_number=rework_round,
        task=task,
    )
    if failure is not None:
        record.worker_continuation = held_transition
        return _launch_intent_unwritable(
            step=step,
            ref=ref,
            attempt_id=record.attempt_id or attempt_id,
            role=WORKER_ROLE,
            reason=failure,
        )
    launched, failed = _bring_up_worker_head(
        runtime,
        task,
        record,
        records,
        payload,
        attempt_id,
        step=step,
        stage=STAGE_REWORK,
        blocked_reason="rework bring-up failed",
        blocked_action=blocked_kind,
    )
    if launched is None:
        assert failed is not None
        return failed
    record.state = "claimed"
    runtime.open_worker_round(record, round_number=rework_round)
    runtime.record_worker_routing(task, record, launched.run)
    attempt_accounting.persist_outcome_round_context(runtime, task, record, phase="worker")
    _record_worker_continuation(runtime, ref, record, continuation, phase, continuation_reason)
    _clear_launch_intent(record)
    record.worker_started_at = record.worker_progress_at = time.time()
    records[ref] = record
    runtime.save_records(payload, records)
    return {"status": "ok", "step": step, "pilot_ref": ref, "attempt_id": attempt_id, "action": action}


def _record_worker_continuation(
    runtime: Any, ref: str, record: DispatcherRecord, mode: str, phase: str, reason: str
) -> None:
    """Leave the red-verdict ownership decision on the card with its frozen launch snapshot."""
    run = record.worker_run
    evidence = record.worker_delivery_evidence
    source_state = str(evidence.get("provider_source_state") or "")
    provider_note = (
        f"provider bound: {bool(evidence.get('provider_bound'))}; source state: {source_state}; "
        if mode == "reused" and source_state
        else ""
    )
    runtime.writer.comment(
        role="dispatcher",
        actor=runtime.owner,
        reference=ref,
        body=(
            f"Dispatcher {phase} red continuation: {mode}; worker profile {run.get('head') or record.head}, "
            f"model {run.get('model') or 'unknown'}, effort {run.get('effort') or 'default'}; "
            f"reason: {reason}; {provider_note}timestamp: {now_rfc3339()}."
        ),
        request_id=_attempt_request_id(
            record.attempt_id, f"{phase}-red-continuation", ref, str(record.attempt_round)
        ),
    )


def _retained_worker_busy_deferred(
    reference: str,
    record: DispatcherRecord,
    attempt_id: str,
    phase: str,
    *,
    delay: int | None = None,
) -> dict[str, Any]:
    """Report a retained continuation held by its own busy pane without changing ownership."""
    continuation = record.worker_continuation
    remaining = max(0, int(continuation.busy_next_at - time.time()))
    wait = delay if delay is not None else remaining
    return {
        "status": "degraded",
        "step": "review" if phase == "review" else "gate",
        "pilot_ref": reference,
        "attempt_id": record.attempt_id or attempt_id,
        "action": f"{phase}-red-worker-busy",
        "attempts": continuation.busy_attempts,
        "reason": (
            "the retained worker pane is busy before its continuation was delivered; its exact "
            f"HeadRun remains owned and the pending delivery retries in {wait}s"
        ),
    }


def _retained_worker_recovery_window(
    reference: str,
    record: DispatcherRecord,
    attempt_id: str,
    phase: str,
    *,
    remaining: int,
) -> dict[str, Any]:
    """Expose a persisted provider-safe recovery wait without pretending it is a busy retry."""
    liveness = record.worker_continuation_liveness
    return {
        "status": "degraded",
        "step": "review" if phase == "review" else "gate",
        "pilot_ref": reference,
        "attempt_id": record.attempt_id or attempt_id,
        "action": f"{phase}-red-worker-recovery-window",
        "attempts": liveness.busy_attempts,
        "reason": (
            "a provider/terminal-safe continuation recovery is awaiting its recorded response "
            f"window for the exact retained HeadRun ({max(0, remaining)}s remaining)"
        ),
    }


def _continuation_no_progress_evidence(
    record: DispatcherRecord,
    state: ContinuationLivenessState,
) -> str:
    """Classify unchanged provider evidence without retaining or interpreting pane text."""
    if state == ContinuationLivenessState.UNAVAILABLE:
        return "provider_unavailable"
    if state == ContinuationLivenessState.UNKNOWN:
        return "provider_or_identity_unknown"
    evidence = record.worker_delivery_evidence if isinstance(record.worker_delivery_evidence, dict) else {}
    composer_before = str(evidence.get("composer_before") or "")
    composer_after = str(evidence.get("composer_after") or "")
    cursor_before = str(evidence.get("cursor_before") or "")
    cursor_after = str(evidence.get("cursor_after") or "")
    if (
        composer_before
        and composer_before == composer_after
        and composer_before not in {COMPOSER_EMPTY, COMPOSER_UNKNOWN}
        and cursor_before
        and cursor_before == cursor_after
    ):
        return "completed_turn_residual_composer"
    return "active_or_unknown_turn"
