"""Worker report acceptance, stale-result handling and one-shot report prompting.

Claim/launch, retained continuation, shared wait/vitality policy, and the gate/review/merge
state machines keep their existing owners. The runtime is a collaborator, as in worker_launch;
board documents and tick results remain compatibility boundaries. This module never imports
the dispatcher facade, and the durable save/effect ordering is unchanged.
"""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Any

from ummanu.board.completion_evidence import (
    has_candidate,
    infra_report_fields,
    render_infra_completion_record,
)
from ummanu.dispatch import attempt_accounting
from ummanu.dispatch.gate import reset_infrastructure_reruns as _reset_infrastructure_reruns
from ummanu.dispatch.head_vitality_episode import VitalityVerdict
from ummanu.dispatch.helpers import (
    _last_marker_body,
    _round_blocked_report_classification,
    _round_done_report_body,
    _round_report_ids,
    _round_report_marker,
    scrub_host_output,
)
from ummanu.dispatch.host import _record_worker_delivery_evidence
from ummanu.dispatch.launch import STAGE_REWORK, WORKER_ROLE
from ummanu.dispatch.launch import clear_launch_intent as _clear_launch_intent
from ummanu.dispatch.launch import launch_intent_unwritable as _launch_intent_unwritable
from ummanu.dispatch.state import DispatcherRecord, OutcomeTerminalPath
from ummanu.dispatch.state import attempt_request_id as _attempt_request_id
from ummanu.dispatch.types import HostError
from ummanu.dispatch.watchdog import reset_idle as _reset_idle
from ummanu.dispatch.watchdog import reset_wait as _reset_wait
from ummanu.dispatch.worker_comments import task_doc_comment_keys as _task_doc_comment_keys
from ummanu.dispatch.worker_launch import bring_up_worker_head as _bring_up_worker_head
from ummanu.dispatch.worker_launch import write_worker_relaunch_intent as _write_worker_relaunch_intent
from ummanu.dispatch.worker_lifecycle import WorkerContinuationStage


def worker_report_marker(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
) -> str | None:
    """Find this round's report and durably bind its terminal path before recovery."""
    ref = task["ref"]
    # The round the dispatcher is holding, not merely the card's last report marker: a marker is
    # attributed to a round through the request id its command carried, which the audit keeps.
    marker = _round_report_marker(
        runtime.audit,
        ref,
        _round_report_ids(record.workspace, record.attempt_id or attempt_id, ref, record.report_generation),
    )
    if marker in {"report:done", "report:blocked"}:
        # Persist the terminal path before looking up the source handoff.
        # A failed lookup must not turn every later Validate, gate or
        # reviewer effect into a path that claims no report was consumed.
        record.outcome_terminal_path = OutcomeTerminalPath.FOLLOWS_ACCEPTED_REPORT
        records[ref] = record
        runtime.save_records(payload, records)
        attempt_accounting.capture_outcome_source(runtime, task, record, phase="report", kind="card.reported", marker=marker)
    return marker


def handle_worker_report(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    *,
    marker: str | None,
) -> dict[str, Any] | None:
    """Consume a done/blocked report after continuation recovery, or leave the wait open."""
    ref = task["ref"]
    continuation = record.worker_continuation
    if marker == "report:done":
        if continuation.validation_move_pending:
            # Frozen and recorded before a tick died mid-move; the replay never wakes the worker.
            # The phase this report closed is the same one the dying tick accepted, so its
            # usage occurrence is finished here rather than lost with that tick.
            attempt_accounting.record_attempt_usage(runtime, ref, record, role=WORKER_ROLE, attempt_id=attempt_id)
            runtime.writer.move(
                role="dispatcher",
                actor=runtime.owner,
                reference=ref,
                target="validate",
                reason="worker report:done",
                request_id=_attempt_request_id(
                    record.attempt_id or attempt_id,
                    "worker-done",
                    ref,
                    str(record.report_generation),
                ),
            )
            record.state = "validate"
            runtime.save_records(payload, records)
            return {
                "status": "ok",
                "step": "advance",
                "pilot_ref": ref,
                "attempt_id": attempt_id,
                "to": "validate",
            }
        try:
            runtime.host.verify_worker_result(task, record)
        except HostError as exc:
            unconfirmed = runtime._stop_worker_confirmed(record, ref, step="advance", attempt_id=attempt_id)
            if unconfirmed is not None:
                return unconfirmed
            attempt_accounting.terminal_effect(runtime, 
                task,
                record,
                target="blocked",
                reason=f"worker result is not durable: {scrub_host_output(str(exc))}",
                request_id=_attempt_request_id(record.attempt_id or attempt_id, "worker-result-blocked", ref),
                terminal_state="blocked",
                disposition="blocked",
                blocked_reason="implementation",
            )
            records.pop(ref, None)
            return {
                "status": "blocked",
                "step": "advance",
                "pilot_ref": ref,
                "attempt_id": attempt_id,
                "reason": "worker result is not durable",
            }
        if has_candidate(task):
            current_sha = runtime.host.head_commit(record)
            if current_sha and current_sha == record.rejected_sha:
                if record.rejected_failure_class == "infrastructure":
                    return _accept_stale_infrastructure_done(
                        runtime,
                        task,
                        record,
                        records,
                        payload,
                        attempt_id,
                        current_sha,
                    )
                return _reject_stale_done(runtime, task, record, records, payload, attempt_id, current_sha)
        else:
            # No candidate: an unchanged HEAD is not a stale result, and every done report of a
            # new round is a fresh one. An infra report leaves its completion record here.
            recorded = _record_infra_completion(runtime, task, record, records, payload, attempt_id)
            if recorded is not None:
                return recorded
        record.rejected_done_reports = 0
        # The report is accepted: whatever the head owed, it has answered.
        record.worker_answer_owed_since = 0.0
        # The report is accepted from here on. Account the worker phase it closes while the
        # head that wrote it is still on the record with its bound provider session.
        attempt_accounting.record_attempt_usage(runtime, ref, record, role=WORKER_ROLE, attempt_id=attempt_id)
        record.review_baseline = len(task.get("comments") or [])
        # Freeze before moving the board. A later tick may finish the idempotent move, but it
        # never leaves a completed worker writing while CI or a reviewer owns this checkout.
        try:
            runtime.host.retain_worker(record)
            continuation.begin_retention(time.time())
        except HostError:
            # A worker with no reusable conversation is still made safe by a confirmed stop.
            unconfirmed = runtime._stop_worker_confirmed(record, ref, step="advance", attempt_id=attempt_id)
            if unconfirmed is not None:
                return unconfirmed
        # Fresh code state: the mechanical gate must re-run before this report reaches review.
        record.gate_state = ""
        record.gate_pending_since = 0.0
        record.gate_transport_failures = 0
        record.gate_transport_error = ""
        _reset_infrastructure_reruns(record)
        _reset_wait(record, "worker")
        _reset_wait(record, "review")
        records[ref] = record
        runtime.save_records(payload, records)
        runtime.writer.move(
            role="dispatcher",
            actor=runtime.owner,
            reference=ref,
            target="validate",
            reason="worker report:done",
            # Keyed on the generation the report closes, so this move and its replay after a
            # crash carry one id whatever the card's comment count has done since.
            request_id=_attempt_request_id(
                record.attempt_id or attempt_id, "worker-done", ref, str(record.report_generation)
            ),
        )
        if continuation.validation_move_pending:
            continuation.confirm_validation_move()
        record.state = "validate"
        runtime.save_records(payload, records)
        return {
            "status": "ok",
            "step": "advance",
            "pilot_ref": ref,
            "attempt_id": attempt_id,
            "to": "validate",
        }
    if marker == "report:blocked":
        # Before the stop, so the phase is accounted while its head is still described here.
        attempt_accounting.record_attempt_usage(runtime, ref, record, role=WORKER_ROLE, attempt_id=attempt_id)
        unconfirmed = runtime._stop_worker_confirmed(record, ref, step="advance", attempt_id=attempt_id)
        if unconfirmed is not None:
            return unconfirmed
        attempt_accounting.terminal_effect(runtime, 
            task,
            record,
            target="blocked",
            reason="worker report:blocked",
            request_id=_attempt_request_id(record.attempt_id or attempt_id, "worker-blocked", ref),
            terminal_state="blocked",
            disposition="blocked",
            verdict="blocked",
            blocked_reason=(
                _round_blocked_report_classification(
                    runtime.audit,
                    ref,
                    _round_report_ids(
                        record.workspace,
                        record.attempt_id or attempt_id,
                        ref,
                        record.report_generation,
                    ),
                )
                or "other"
            ),
        )
        records.pop(ref, None)
        return {
            "status": "ok",
            "step": "advance",
            "pilot_ref": ref,
            "attempt_id": attempt_id,
            "to": "blocked",
        }
    return None


def _record_infra_completion(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
) -> dict[str, Any] | None:
    """Copy an accepted infra done report into the card's completion record.

    The report writer refuses a body without both sections, so a malformed body here got past
    it (a replayed legacy record, a hand-written marker). It is not accepted into Validate: the
    card is Blocked with the reason the worker would have been given.
    """
    if task.get("type") != "infra":
        return None
    ref = task["ref"]
    round_ids = _round_report_ids(
        record.workspace, record.attempt_id or attempt_id, ref, record.report_generation
    )
    body = (
        _round_done_report_body(runtime.audit, ref, round_ids) or _last_marker_body(task, "report:done") or ""
    )
    fields, refusal = infra_report_fields(body)
    if refusal:
        unconfirmed = runtime._stop_worker_confirmed(record, ref, step="advance", attempt_id=attempt_id)
        if unconfirmed is not None:
            return unconfirmed
        attempt_accounting.terminal_effect(runtime, 
            task,
            record,
            target="blocked",
            reason=f"infra report rejected: {refusal}",
            request_id=_attempt_request_id(
                record.attempt_id or attempt_id,
                "infra-report-rejected",
                ref,
                str(record.report_generation),
            ),
            terminal_state="blocked",
            disposition="blocked",
            blocked_reason="implementation",
        )
        records.pop(ref, None)
        runtime.save_records(payload, records)
        return {
            "status": "blocked",
            "step": "advance",
            "pilot_ref": ref,
            "attempt_id": attempt_id,
            "reason": "infra report lacks its completion sections",
        }
    runtime.writer.comment(
        role="dispatcher",
        actor=runtime.owner,
        reference=ref,
        body=render_infra_completion_record(fields),
        request_id=_attempt_request_id(
            record.attempt_id or attempt_id, "completion-infra", ref, str(record.report_generation)
        ),
    )
    return None


def _accept_stale_infrastructure_done(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    sha: str,
) -> dict[str, Any]:
    """Let an infra-red SHA retry the gate without opening a no-op worker round.

    This is deliberately beside ``_reject_stale_done``: that safeguard remains intact for a
    red review and a substantive gate.  The class was persisted from the gate result, so this
    branch neither parses a card comment nor trusts a manual flag.
    """
    ref = task["ref"]
    if record.rejected_done_reports:
        return _block_repeated_infrastructure_done(
            runtime,
            task,
            record,
            records,
            payload,
            attempt_id,
            sha,
        )
    # The report is accepted here, for the same round the first one opened: the occurrence
    # that round already owns is what a repeated report returns, not a second account.
    attempt_accounting.record_attempt_usage(runtime, ref, record, role=WORKER_ROLE, attempt_id=attempt_id)
    try:
        runtime.host.retain_worker(record)
        record.worker_continuation.begin_retention(time.time())
    except HostError:
        unconfirmed = runtime._stop_worker_confirmed(record, ref, step="advance", attempt_id=attempt_id)
        if unconfirmed is not None:
            return unconfirmed
    # A legacy/recovered record can still hand the worker an infra-classified stale SHA.  One
    # report is enough to return it to the real gate rerun path; another identical report has
    # no new evidence and must not reuse the same request id as a silent no-op tick.
    record.rejected_done_reports = 1
    record.gate_state = ""
    record.gate_pending_since = 0.0
    record.gate_transport_failures = 0
    record.gate_transport_error = ""
    _reset_infrastructure_reruns(record)
    _reset_wait(record, "worker")
    _reset_wait(record, "review")
    runtime.writer.comment(
        role="dispatcher",
        actor=runtime.owner,
        reference=ref,
        body=(
            f"The repeated done report for HEAD {sha} was accepted for automatic mechanical "
            f"gate retry: the previous red was classified from its CI step as infrastructure "
            f"({record.rejected_failure_reason or 'enumerated infrastructure signature'}). "
            "No worker rework round was opened."
        ),
        request_id=_attempt_request_id(
            record.attempt_id or attempt_id,
            "stale-done-infrastructure-retry",
            ref,
            str(record.report_generation),
        ),
    )
    record.comment_baseline = len(runtime.reader.show(ref).get("comments") or [])
    record.review_baseline = record.comment_baseline
    records[ref] = record
    runtime.save_records(payload, records)
    runtime.writer.move(
        role="dispatcher",
        actor=runtime.owner,
        reference=ref,
        target="validate",
        reason=("worker report:done retries an infrastructure-classified mechanical gate on the same SHA"),
        request_id=_attempt_request_id(
            record.attempt_id or attempt_id,
            "stale-done-infrastructure-validate",
            ref,
            str(record.report_generation),
        ),
    )
    record.state = "validate"
    runtime.save_records(payload, records)
    return {
        "status": "ok",
        "step": "advance",
        "pilot_ref": ref,
        "attempt_id": attempt_id,
        "to": "validate",
        "action": "stale-done-infrastructure-retry",
    }


def _block_repeated_infrastructure_done(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    sha: str,
) -> dict[str, Any]:
    """A second stale infra report cannot add evidence after the accepted gate retry."""
    ref = task["ref"]
    unconfirmed = runtime._stop_worker_confirmed(record, ref, step="advance", attempt_id=attempt_id)
    if unconfirmed is not None:
        return unconfirmed
    reports = record.rejected_done_reports + 1
    attempt_accounting.terminal_effect(runtime, 
        task,
        record,
        target="blocked",
        reason=(
            f"The worker reported done {reports} times on unchanged infrastructure-classified "
            f"HEAD {sha} ({record.rejected_failure_reason or 'enumerated CI-service signature'}). "
            "One report already returned the SHA to the bounded Actions rerun path; a further "
            "identical report has no new gate evidence."
        ),
        request_id=_attempt_request_id(
            record.attempt_id or attempt_id,
            "stale-done-infrastructure-blocked",
            ref,
            str(reports),
        ),
        terminal_state="blocked",
        disposition="blocked",
        blocked_reason="infrastructure",
    )
    records.pop(ref, None)
    runtime.save_records(payload, records)
    return {
        "status": "blocked",
        "step": "advance",
        "pilot_ref": ref,
        "attempt_id": attempt_id,
        "action": "stale-done-infrastructure-blocked",
    }


def _reject_stale_done(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    sha: str,
) -> dict[str, Any]:
    """Bounce one repeated rejected result, then leave the diagnosis to a human."""
    ref = task["ref"]
    rejected = record.rejected_done_reports + 1
    if rejected >= 2:
        unconfirmed = runtime._stop_worker_confirmed(record, ref, step="advance", attempt_id=attempt_id)
        if unconfirmed is not None:
            return unconfirmed
        record.rejected_done_reports = rejected
        attempt_accounting.terminal_effect(runtime, 
            task,
            record,
            target="blocked",
            reason=(
                f"The worker reported done twice with no new work: HEAD {sha} was already "
                "rejected by the mechanical gate or by a red review. A human needs to look at "
                "this."
            ),
            request_id=_attempt_request_id(
                record.attempt_id or attempt_id,
                "stale-done-blocked",
                ref,
                str(record.rejected_done_reports),
            ),
            terminal_state="blocked",
            disposition="blocked",
            blocked_reason="implementation",
        )
        records.pop(ref, None)
        runtime.save_records(payload, records)
        return {
            "status": "blocked",
            "step": "advance",
            "pilot_ref": ref,
            "attempt_id": attempt_id,
            "reason": "worker repeatedly reported rejected SHA",
        }

    # The rework worker opens in this same checkout, so the head that reported the stale done has
    # to be confirmed gone first; a refusal ends the tick before the comment and the relaunch.
    unconfirmed = runtime._stop_worker_confirmed(record, ref, step="advance", attempt_id=attempt_id)
    if unconfirmed is not None:
        return unconfirmed
    # Counted only once the bounce happens; a tick stopped at the refusal rejected nothing.
    record.rejected_done_reports = rejected
    # The head now owes the dispatcher an answer. Stamped here so the vitality reduction can
    # read the pair "rejected report, then a turn that ended" as an explicit stall signal
    # rather than leaving it to the outer report ceiling (secretary-1543).
    record.worker_answer_owed_since = time.time()
    runtime.writer.comment(
        role="dispatcher",
        actor=runtime.owner,
        reference=ref,
        body=(
            f"The done report was rejected: HEAD {sha} was already rejected by the mechanical "
            "gate or by a red review. Do and commit new work, then report again. If the cause "
            "is a test or the gate itself and the code should not change, use "
            "report --kind blocked; another done on this SHA moves the card to Blocked."
        ),
        request_id=_attempt_request_id(
            record.attempt_id or attempt_id, "stale-done-rework", ref, str(record.rejected_done_reports)
        ),
    )
    record.comment_baseline = len(runtime.reader.show(ref).get("comments") or [])
    record.review_baseline = record.comment_baseline
    # The bounce restarts this attempt with a new TASK.md, so it is a new report round: without a
    # new generation the next done report would be deduped against the stale one just rejected.
    # The routing round does not move here, so this generation cannot be `attempt_round`.
    record.report_generation += 1
    # Nobody adjudicated this round: it was opened by the bounce, not an observer. The decision
    # that opened the previous one goes with it, or the document names a review this is not about.
    record.report_decision = ""
    record.report_decision_id = ""
    record.report_protocol_prerequisites = ()
    _reset_wait(record, "worker")
    _reset_wait(record, "review")
    moved = runtime.reader.show(ref)
    failure = _write_worker_relaunch_intent(
        runtime, payload, records, ref, record, action="stale-done-rework", task=moved
    )
    if failure is not None:
        return _launch_intent_unwritable(
            step="advance",
            ref=ref,
            attempt_id=record.attempt_id or attempt_id,
            role=WORKER_ROLE,
            reason=failure,
        )
    launched, failed = _bring_up_worker_head(
        runtime,
        moved,
        record,
        records,
        payload,
        attempt_id,
        step="advance",
        stage=STAGE_REWORK,
        blocked_reason="stale-result rework bring-up failed",
        blocked_action="stale-done-rework-blocked",
    )
    if launched is None:
        assert failed is not None
        return failed
    _record_worker_delivery_evidence(record, launched.delivery_evidence)
    record.state = "claimed"
    # A rejected done report earns no verdict, so this stays the same round.
    runtime.record_worker_routing(moved, record, launched.run)
    _clear_launch_intent(record)
    record.worker_started_at = record.worker_progress_at = time.time()
    records[ref] = record
    runtime.save_records(payload, records)
    return {
        "status": "ok",
        "step": "advance",
        "pilot_ref": ref,
        "attempt_id": attempt_id,
        "action": "stale-done-rework",
    }


def prompt_worker_report(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    *,
    trigger: str,
) -> tuple[dict[str, Any] | None, str]:
    """Spend this round's one report prompt on a confirmed-idle worker, or decline to.

    Hands back the tick's outcome and the trigger the caller carries on with. A `None` outcome
    means the watchdog carries on into its stop-and-replace path.

    The order is the durability contract: intent on disk, then the send, then the confirmation. A
    tick that dies in the middle leaves an intent that reads as spent, which is what stops a
    restart from typing the same prompt twice.
    """
    ref = task["ref"]
    nudge = record.worker_report_nudge
    generation = record.report_generation
    if nudge.spent(generation):
        return None, trigger
    if not runtime.host.worker_addressable(record):
        return None, trigger
    nudge.begin(generation, time.time())
    records[ref] = record
    runtime.save_records(payload, records)
    try:
        runtime.bind_codex_provider_ingress(
            record, records, payload, role="worker", reference=ref
        )
        runtime.host.prompt_worker_report(task, record)
    except HostError as exc:
        _record_worker_delivery_evidence(record, exc, failure=True)
        records[ref] = record
        runtime.save_records(payload, records)
        return None, f"{trigger}, and the report prompt was refused: {scrub_host_output(str(exc))}"
    nudge.confirm()
    # The prompted head owns a fresh idle window AND a fresh stall episode: charging it
    # with the episode that produced the prompt would escalate on the next tick before
    # the worker could have answered. The episode restarts its quiet reference at now,
    # keeping the run identity and history, so the ladder must re-earn suspicion from
    # the moment the worker was actually asked. That restart is ``quiet_since``, not
    # ``started_at`` alone: the reducer measures quiet from the LATER of the last
    # observed progress and the last restart, so for an episode that ever saw the
    # provider advance, rewriting only ``started_at`` bought the head no grace at all
    # and the next tick re-confirmed immediately -- removing the one conversational
    # rung that stands between a quiet head and a respawn (secretary-1543). The
    # progress history itself is left alone: an operator still reads when this head
    # last actually moved.
    _reset_idle(record, "worker")
    episode = record.worker_vitality_episode
    if episode is not None:
        record.worker_vitality_episode = replace(
            episode,
            verdict=VitalityVerdict.HEALTHY_QUIET,
            suspected_since=0.0,
            confirmed_since=0.0,
            started_at=time.time(),
            quiet_since=time.time(),
            updated_at=time.time(),
            reason="report prompt delivered; the quiet clock restarts here",
        )
    records[ref] = record
    runtime.save_records(payload, records)
    # Persisted before the comment: a raising writer must not leave the prompt unrecorded.
    runtime.writer.comment(
        role="dispatcher",
        actor=runtime.owner,
        reference=ref,
        body=(
            f"Dispatcher wait watchdog: {trigger}. The worker head was asked once to run the "
            f"report command for generation {generation}. The round, its TASK.md and its owner "
            "are unchanged. Another idle episode in this round stops the head instead."
            + (
                f" Provider bound: {bool(record.worker_delivery_evidence.get('provider_bound'))}; "
                f"source state: {record.worker_delivery_evidence.get('provider_source_state')}."
                if record.worker_delivery_evidence.get("provider_source_state")
                else ""
            )
        ),
        request_id=_attempt_request_id(
            record.attempt_id or attempt_id, "worker-report-prompt", ref, str(generation)
        ),
    )
    return {
        # Degraded, not ok: a card whose worker had to be reminded is not moving on its own.
        "status": "degraded",
        "step": "advance",
        "pilot_ref": ref,
        "attempt_id": attempt_id,
        "action": "worker-report-prompted",
        "reason": trigger,
    }, trigger


def deliver_worker_comments(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
) -> dict[str, Any] | None:
    """Point a live worker at PO, owner and observer comments that landed during its round.

    A comment is pending when neither the worker's TASK.md was rendered with it nor the record says
    it was already pointed at. Nothing is sent to a worker that is not running a round right now —
    paused, suspended, retained for validation, between rounds or gone — and nothing is recorded
    for it either: the next round's TASK.md carries every comment (secretary-1768).

    The order is `prompt_worker_report`'s durability contract: the keys go on the record and to
    disk before the send, so a tick that dies in the middle reads them as delivered rather than
    typing the same comment twice. The TASK.md is rewritten before the pointer goes out, so the
    section it points at already holds the new comment.
    """
    if (
        record.state != "claimed"
        or record.paused_worker_at
        or record.worker_continuation.stage
        not in (WorkerContinuationStage.NONE, WorkerContinuationStage.DELIVERY_CONFIRMED)
    ):
        return None
    comments = runtime.host.worker_comments(task)
    if not comments:
        return None
    handed = _task_doc_comment_keys(record.workspace) | set(record.worker_comment_deliveries)
    pending = [comment.key for comment in comments if comment.key not in handed]
    if not pending or not runtime.host.worker_takes_comments(record):
        return None
    ref = task["ref"]
    record.worker_comment_deliveries = (*record.worker_comment_deliveries, *pending)
    records[ref] = record
    runtime.save_records(payload, records)
    try:
        runtime.bind_codex_provider_ingress(record, records, payload, role="worker", reference=ref)
        runtime.host.deliver_worker_comments(task, record)
    except HostError as exc:
        _record_worker_delivery_evidence(record, exc, failure=True)
        records[ref] = record
        runtime.save_records(payload, records)
        return {
            "status": "degraded",
            "step": "advance",
            "pilot_ref": ref,
            "attempt_id": attempt_id,
            "action": "worker-comments-refused",
            "comments": pending,
            "reason": scrub_host_output(str(exc)),
        }
    records[ref] = record
    runtime.save_records(payload, records)
    return {
        "status": "ok",
        "step": "advance",
        "pilot_ref": ref,
        "attempt_id": attempt_id,
        "action": "worker-comments-delivered",
        "comments": pending,
    }
