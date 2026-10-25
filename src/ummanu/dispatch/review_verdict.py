"""Review verdict acceptance: a green or red verdict parks, transitions or blocks the card."""

from __future__ import annotations

from typing import Any

from ummanu.board.completion_evidence import has_candidate
from ummanu.board.observer_routing import observer_managed
from ummanu.board.review_blockers import repeated_open_blockers
from ummanu.dispatch import attempt_accounting, e2e_stage, release_lifecycle
from ummanu.dispatch.assessment_park import begin_park, complete_park
from ummanu.dispatch.candidate_ledger import ledger_entry
from ummanu.dispatch.gate import GateResult
from ummanu.dispatch.gate_lifecycle import (
    accept_green_gate as _accept_green_gate,
    block_gate_transport as _block_gate_transport,
    gate_answered as _gate_answered,
    gate_pending as _gate_pending,
    gate_red_to_worker as _gate_red_to_worker,
    gate_transport_retry as _gate_transport_retry,
)
from ummanu.dispatch.helpers import (
    RED_REVIEW_CEILING,
    _last_marker,
    _last_review_red_body,
    red_review_count as _red_review_count,
)
from ummanu.dispatch.launch import REVIEW_ROLE
from ummanu.dispatch.state import (
    REVIEW_REJECTION_REASON,
    DispatcherRecord,
)
from ummanu.dispatch.types import STOPPED_BY_REVIEW_VERDICT, GateTransportError
from ummanu.dispatch.worker_continuation import (
    begin_red_transition as _begin_red_transition,
    complete_red_transition as _complete_red_transition,
)


def advance_review_verdict(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
) -> dict[str, Any] | None:
    """Consume a durable review verdict or finish an already-open park before review orchestration."""
    ref = task["ref"]
    if record.worker_continuation.parked:
        # The park's move or its checkpoint did not commit: the card is still in Validate with
        # the verdict recorded. Finish the park before the gate or any review marker is read.
        return complete_park(runtime, record, records, payload, attempt_id, ref=ref)
    if record.worker_continuation.red_transition_pending:
        # A red transition whose move did not commit is finished before the gate is read again,
        # before any review marker and before a reviewer starts: a rollup that has turned green
        # since cannot retract a red round this card is already owed.
        return _complete_red_transition(runtime, task, record, records, payload, attempt_id, ref=ref)
    marker = _last_marker(task, record.review_baseline, {"review:green", "review:red"})
    if marker == "review:green":
        attempt_accounting.capture_outcome_source(runtime, 
            task, record, phase="verdict", kind="card.verdict", marker=marker
        )
        return park_green_verdict(runtime, task, record, records, payload, attempt_id)
    if marker != "review:red":
        return None

    attempt_accounting.capture_outcome_source(runtime, task, record, phase="verdict", kind="card.verdict", marker=marker)
    # Only the reviewer's lifecycle ends here: a full `stop` would take the worktree's
    # terminals down, and this checkout is about to be parked and is never re-created from
    # base. An unconfirmed stop ends the tick before the card moves. The commit is read
    # first: ending the reviewer forgets the commit it judged and the park has to keep it.
    reviewed = record.review_commit or runtime.host.head_commit(record)
    unconfirmed = runtime._end_review_pane_confirmed(
        record,
        records,
        payload,
        ref,
        step="review",
        attempt_id=attempt_id,
        initiator=STOPPED_BY_REVIEW_VERDICT,
    )
    if unconfirmed is not None:
        return unconfirmed
    # The verdict is accepted here, whichever of the three red outcomes it takes: the
    # reviewer's pane is closed but its run, and the session it names, are still recorded.
    attempt_accounting.record_attempt_usage(runtime, ref, record, role=REVIEW_ROLE, attempt_id=attempt_id)
    record.rejected_sha = reviewed
    record.rejected_failure_class = "substantive"
    record.rejected_failure_reason = REVIEW_REJECTION_REASON
    record.rejected_gate_evidence = []
    record.rejected_done_reports = 0
    # The only point where both the last review body and the SHA it judged are available.
    # Keep them for the next review packet instead of reconstructing the card from base.
    record.previous_reviewed_sha = reviewed
    record.previous_review_id = ""
    # Preserve the verdict as evidence. The packet renderer supplies its data boundary;
    # flattening/truncating here loses stable IDs and their relationships permanently.
    record.previous_blockers = _last_review_red_body(task) or ""
    repeated = repeated_open_blockers(task)
    if not parks_for_decision(runtime, task):
        # No observer to release it: the verdict acts on its own tick, and the worker that
        # wrote the code is still suspended, so the verdict goes to that conversation.
        # A repeat or the ceiling leaves the decision with the PO that cut the card.
        reds = _red_review_count(task)
        if repeated or reds >= RED_REVIEW_CEILING:
            return block_unobserved_red_review(
                runtime, task, record, records, payload, attempt_id, reds=reds, repeated=repeated
            )
        return _begin_red_transition(
            runtime,
            task,
            record,
            records,
            payload,
            attempt_id,
            phase="review",
            move_reason="review:red",
            verdict_outcome="red",
        )

    # The worker of this round stays suspended through the park: the observer may send the
    # findings back to it, and that conversation is only worth keeping if nothing else writes.
    runtime._record_verdict_routing(ref, record, "red")
    return begin_park(
        runtime,
        task,
        record,
        records,
        payload,
        attempt_id,
        verdict_outcome="red",
        reviewed_commit=reviewed,
        move_reason=(
            "review:red. The card is parked in Assessment: the reviewer is stopped and "
            "the worker of this round is held, waiting for an accept, rework or reslice "
            "decision."
            + (f" Repeated open blockers: {', '.join(repeated)}." if repeated else "")
        ),
    )


def parks_for_decision(runtime: Any, task: dict[str, Any]) -> bool:
    """Whether a substantive verdict on this card waits for a decision, or acts at once."""
    return observer_managed(task, runtime.sprints.show)


def park_green_verdict(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    *,
    reviewed: bool = True,
) -> dict[str, Any]:
    """A green review verdict, or an accepted report with review skipped, parks the card.

    A code card of a project that declares an e2e check runs it first, on the SHA the merge gate has
    just passed (`dispatch/e2e_stage.py`): the park carries its green result, and a red one never
    parks. It does not merge it. A card without a candidate has no gate to re-read and nothing to merge,
    so it goes straight to the park or, with nobody to decide, to the release.
    """
    ref = task["ref"]
    if reviewed:
        # Recorded before the gate: this round's head pair is a fact a red re-check cannot undo.
        runtime._record_verdict_routing(ref, record, "green")
        attempt_accounting.record_attempt_usage(runtime, ref, record, role=REVIEW_ROLE, attempt_id=attempt_id)
    if has_candidate(task):
        # The e2e stage reads the merge gate itself and binds to the SHA that gate validated, before the
        # card may park or release; the green result it read is accepted here, once.
        e2e = e2e_stage.run_stage(
            runtime,
            task,
            record,
            records,
            payload,
            attempt_id,
            step="review",
            gate=lambda: read_merge_gate(runtime, task, record, records, payload, attempt_id),
        )
        if isinstance(e2e, dict):
            return e2e
        if e2e is None:
            gated = merge_ready_for_park(runtime, task, record, records, payload, attempt_id)
        else:
            gated = accept_merge_gate(
                runtime, task, record, records, payload, attempt_id, e2e.result, e2e_reconciliation=e2e.reconciliation
            )
        if gated is not None:
            return gated
    else:
        # Before the park or the release: the observer decides with the report in knowledge.
        refused = release_lifecycle.transfer_research_report(runtime,
            task, record, records, payload, attempt_id, step="review"
        )
        if refused is not None:
            return refused
    parks = parks_for_decision(runtime, task)
    pinned = (record.review_commit or runtime.host.head_commit(record)) if has_candidate(task) else ""
    if reviewed and (parks or task.get("type") == "code"):
        unconfirmed = runtime._end_review_pane_confirmed(
            record, records, payload, ref, step="review", attempt_id=attempt_id,
            initiator=STOPPED_BY_REVIEW_VERDICT,
        )
        if unconfirmed is not None:
            return unconfirmed
    if not parks:
        if task.get("type") == "code":
            begin_park(
                runtime, task, record, records, payload, attempt_id,
                verdict_outcome="green" if reviewed else "missing",
                reviewed_commit=pinned,
                move_reason="green code completion; Assessment automatically releases without an observer",
            )
            task = runtime.reader.show(ref)
        return release_lifecycle.release_effect(runtime,
            task,
            record,
            records,
            payload,
            attempt_id,
            step="review",
            move_reason="review:green" if reviewed else "report:done, review skipped",
            verdict="green" if reviewed else "missing",
        )
    if not has_candidate(task):
        waits = "there is no candidate to merge, and Done waits"
    elif reviewed:
        waits = "the mechanical gate is green and the merge waits"
    else:
        waits = "the mechanical gate is green, no reviewer runs, and the merge waits"
    return begin_park(runtime, 
        task,
        record,
        records,
        payload,
        attempt_id,
        verdict_outcome="green" if reviewed else "missing",
        reviewed_commit=pinned,
        move_reason=(
            f"{'review:green' if reviewed else 'report:done, review skipped'}. The card is parked "
            f"in Assessment: {waits} for an accept, rework or reslice decision."
        ),
    )


def merge_ready_for_park(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
) -> dict[str, Any] | None:
    """Re-read the merge gate before a candidate is parked or released; None when it is green."""
    outcome, result = read_merge_gate(runtime, task, record, records, payload, attempt_id)
    if outcome is not None:
        return outcome
    assert result is not None
    return accept_merge_gate(runtime, task, record, records, payload, attempt_id, result)


def accept_merge_gate(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    result: GateResult,
    *,
    e2e_reconciliation: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Accept a green merge gate result once, for the park or the release that follows it."""
    return _accept_green_gate(
        runtime,
        task,
        record,
        records,
        payload,
        attempt_id,
        result,
        stage="assessment" if parks_for_decision(runtime, task) else "release",
        e2e_reconciliation=e2e_reconciliation,
    )


def read_merge_gate(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
) -> tuple[dict[str, Any] | None, GateResult | None]:
    """The merge gate read before a park or a release: `(outcome, None)` unless it is green, then
    `(None, result)`. Nothing is accepted or attested here."""
    ref = task["ref"]
    kind, result, detail = release_lifecycle.merge_readiness(runtime, task, record)
    if kind == "transport":
        retry = _gate_transport_retry(runtime, 
            task,
            record,
            records,
            payload,
            attempt_id,
            GateTransportError(detail),
            step="review",
        )
        if retry is not None:
            return retry, None
        return _block_gate_transport(runtime, 
            task,
            record,
            records,
            payload,
            attempt_id,
            step="review",
            action="merge-gate-transport-blocked",
        ), None
    if kind == "drift":
        # The bounce clears the record's gate state itself.
        return _gate_red_to_worker(runtime, 
            task, record, records, payload, attempt_id, GateResult("red", detail), phase="review-freeze"
        ), None
    _gate_answered(runtime, ref, record, records, payload)
    if kind == "failed":
        return release_lifecycle.block_merge_path(runtime,
            task,
            record,
            records,
            payload,
            attempt_id,
            blocked_reason="gate",
            action="merge-gate-blocked",
            reason=f"merge gate failed: {detail}",
            step="review",
            outcome="merge gate failed",
        ), None
    if kind == "pending":
        if result is None:
            return release_lifecycle.block_merge_path(runtime,
                task,
                record,
                records,
                payload,
                attempt_id,
                blocked_reason="gate",
                action="merge-gate-result-blocked",
                reason="merge gate returned pending without a result payload",
                step="review",
                outcome="merge gate result unavailable",
            ), None
        return _gate_pending(runtime, 
            task,
            record,
            records,
            payload,
            attempt_id,
            result,
            step="review",
            action="merge-gate-pending",
        ), None
    if kind != "green":
        if result is None:
            return release_lifecycle.block_merge_path(runtime,
                task,
                record,
                records,
                payload,
                attempt_id,
                blocked_reason="gate",
                action="merge-gate-result-blocked",
                reason="merge gate returned a non-green state without a result payload",
                step="review",
                outcome="merge gate result unavailable",
            ), None
        return _gate_red_to_worker(runtime, 
            task, record, records, payload, attempt_id, result, phase="merge-gate"
        ), None
    if result is None:
        return release_lifecycle.block_merge_path(runtime,
            task,
            record,
            records,
            payload,
            attempt_id,
            blocked_reason="gate",
            action="merge-gate-result-blocked",
            reason="merge gate returned green without a result payload",
            step="review",
            outcome="merge gate result unavailable",
        ), None
    return None, result


def block_unobserved_red_review(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    *,
    reds: int,
    repeated: tuple[str, ...],
) -> dict[str, Any]:
    """Stop an unobserved RED at a repeat or the ceiling, keeping its workspace and branch."""
    runtime._record_verdict_routing(task["ref"], record, "red")
    reason = (
        f"review:red. Repeated open blockers: {', '.join(repeated)}. The card is Blocked "
        "instead of opening another worker round. The PO that cut the card decides between "
        "the card's goal and its wording. The workspace and the branch are kept as the last "
        "round left them."
        if repeated else
        f"review:red. This card has now collected {reds} substantive red reviews and its "
        f"sprint has no observer to decide for it, so the no-observer ceiling of "
        f"{RED_REVIEW_CEILING} is reached: the card is Blocked instead of opening another "
        f"worker round. The workspace and the branch are kept as the last round left "
        f"them; unblock the card to continue."
    )
    return release_lifecycle.block_merge_path(runtime,
        task,
        record,
        records,
        payload,
        attempt_id,
        blocked_reason="review",
        action="red-review-repeated-blockers" if repeated else "red-review-ceiling",
        reason=reason,
        step="review",
        outcome="repeated review blockers" if repeated else "red review ceiling reached",
        rejected_candidate=ledger_entry(runtime, task["ref"], record),
    )
