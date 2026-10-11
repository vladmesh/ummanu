"""Assessment decision intake, replay, rework and reslice execution."""

from __future__ import annotations

from typing import Any

from ummanu.board.protocol_artifacts import (
    ArtifactOwnershipViolation,
    validate_rework_prerequisites,
)
from ummanu.dispatch import attempt_accounting, release_lifecycle
from ummanu.dispatch.decision_pointer import decision_pointer
from ummanu.dispatch.helpers import _last_marker_body
from ummanu.dispatch.review_packet import retain_rework_review
from ummanu.dispatch.review_verdict import complete_park as _complete_park
from ummanu.dispatch.state import DispatcherRecord, attempt_request_id as _attempt_request_id
from ummanu.dispatch.tick_telemetry import tick_stage
from ummanu.dispatch.types import STOPPED_BY_REVIEW_VERDICT, HostError
from ummanu.dispatch.worker_continuation import (
    begin_red_transition as _begin_red_transition,
    complete_red_transition as _complete_red_transition,
)
from ummanu.tasks import TaskError, _event_payload, assessment_resolution, specification_revision


def advance_assessment(
    runtime: Any,
    task: dict[str, Any],
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
) -> dict[str, Any]:
    """Advance a parked card through its recorded Assessment decision.

    Decision intake/replay and the rework/reslice effects live here. A release delegates to the
    package-owned release/merge/completion lifecycle.
    """
    # The parent of the release stages: what they leave of the advance is its own measured cost.
    with tick_stage("assessment"):
        return _advance_assessment(runtime, task, records, payload, attempt_id)


def _advance_assessment(
    runtime: Any,
    task: dict[str, Any],
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
) -> dict[str, Any]:
    ref = task["ref"]
    record = records.get(ref)
    if record is None:
        try:
            record = runtime._adopt(task, attempt_id)
        except HostError as exc:
            return runtime._block_unresumable(
                task, records, payload, attempt_id, "assessment", exc
            )
        records[ref] = record

    continuation = record.worker_continuation
    if continuation.red_transition_pending:
        # A rework decision whose move did not commit: finish it before any decision is read.
        return _complete_red_transition(
            runtime, task, record, records, payload, attempt_id, ref=ref
        )
    if continuation.assessment_pending:
        # The move landed but the checkpoint did not; re-issuing is a no-op by request id.
        return _complete_park(runtime, record, records, payload, attempt_id, ref=ref)
    if not continuation.parked:
        # A record lost while parked, or a card an operator parked by hand: the board is the fact.
        # A session this record cannot prove is held is not held, so it owns no worker.
        continuation.begin_park(
            "review", len(task.get("comments") or []), "adopted parked card", "unknown"
        )
        continuation.confirm_park()
        record.state = "assessment"
        records[ref] = record
        runtime.save_records(payload, records)

    decision, reason, prerequisites = recorded_decision(runtime, task)
    if not decision:
        return {
            "status": "ok",
            "step": "assessment",
            "pilot_ref": ref,
            "attempt_id": attempt_id,
            "action": "waiting-observer-decision",
        }

    visit, recorded = assessment_resolution(runtime.audit.events(ref))
    decision_request = (
        str(recorded.get("request_id") or "") if isinstance(recorded, dict) else ""
    )
    decision_event_id = (
        str(recorded.get("event_id") or "") if isinstance(recorded, dict) else ""
    )
    if visit and decision_request and decision_event_id:
        try:
            attempt_accounting.persist_outcome_round_context(runtime, 
                task,
                record,
                phase="decision",
                assessment_visit=visit,
                request_ids={decision_request},
                source_event_id=decision_event_id,
                marker=f"decision:{decision}",
            )
        except (OSError, TaskError, ValueError):
            # The decision has already committed. Do not turn a journal outage into a new
            # lifecycle authority; terminal projection retains the missing-decision diagnostic.
            pass

    if decision == "rework":
        return rework_parked(
            runtime,
            task,
            record,
            records,
            payload,
            attempt_id,
            reason=reason,
            protocol_prerequisites=prerequisites,
        )
    if decision == "reslice":
        return reslice_parked(
            runtime, task, record, records, payload, attempt_id, reason=reason
        )

    return release_lifecycle.release_parked(
        runtime, task, record, records, payload, attempt_id, reason=reason
    )


def recorded_decision(
    runtime: Any, task: dict[str, Any]
) -> tuple[str, str, tuple[str, ...]]:
    """Return the current canonical Assessment decision and admitted prerequisites."""
    events = runtime.audit.events(task["ref"])
    _visit, event = assessment_resolution(events)
    data = _event_payload(event) if isinstance(event, dict) else {}
    decision = str(data.get("decision") or "")
    body = data.get("body")
    if not isinstance(body, str) or not body.strip():
        body = _last_marker_body(task, f"decision:{decision}") or ""
    if (
        decision not in {"release", "rework", "reslice"}
        or not isinstance(body, str)
        or not body.strip()
    ):
        return "", "", ()

    # A missing field is the released empty declaration; a present malformed value is never
    # allowed to turn into an authoritative worker instruction.
    declared = data.get("protocol_prerequisites", [])
    if not isinstance(declared, list):
        return "", "", ()
    if decision != "rework":
        return decision, body, ()
    try:
        prerequisites = validate_rework_prerequisites(
            declared,
            specification_revision=specification_revision(
                events, str(task.get("description") or "")
            )
            or None,
        )
    except (ValueError, ArtifactOwnershipViolation):
        # An invalid declaration is never a worker instruction. The writer rejects it before
        # commit; this is the recovery fence for a malformed historical audit record.
        return "", "", ()
    return decision, body, tuple(artifact.name for artifact in prerequisites)


def rework_parked(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    *,
    reason: str,
    protocol_prerequisites: tuple[str, ...],
) -> dict[str, Any]:
    """Release the retained worker round for an accepted rework decision."""
    ref = task["ref"]
    # Before stopping an adopted reviewer or opening a generation: the accepted
    # park's pin and this visit's canonical evidence travel with the rework intent.
    retain_rework_review(task, runtime.audit.events(ref), record, reason)
    verdict = record.worker_continuation.verdict_outcome
    # A parked card should have no reviewer left; an adopted one may still name a pane nobody
    # stopped. Either way nothing is woken beside a head the host will not confirm gone.
    if record.owns_head("review"):
        unconfirmed = runtime._end_review_pane_confirmed(
            record,
            records,
            payload,
            ref,
            step="assessment",
            attempt_id=attempt_id,
            initiator=STOPPED_BY_REVIEW_VERDICT,
        )
        if unconfirmed is not None:
            return unconfirmed

    # Findings are not repeated in the move: packets use the frozen predecessor.
    # The decision is frozen with the round so recovery cannot substitute a later instruction.
    return _begin_red_transition(
        runtime,
        task,
        record,
        records,
        payload,
        attempt_id,
        phase="review",
        move_reason=decision_pointer(runtime, task, "rework"),
        verdict_outcome=verdict,
        decision="rework",
        decision_body=reason,
        decision_protocol_prerequisites=protocol_prerequisites,
    )


def reslice_parked(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    *,
    reason: str,
) -> dict[str, Any]:
    """End the attempt and leave a resliced card Blocked for a fresh cut."""
    ref = task["ref"]
    unconfirmed = runtime._stop_worker_confirmed(
        record, ref, step="assessment", attempt_id=attempt_id
    )
    if unconfirmed is not None:
        records[ref] = record
        runtime.save_records(payload, records)
        return unconfirmed

    runtime.host.stop(record)
    attempt_accounting.terminal_effect(runtime, 
        task,
        record,
        target="blocked",
        reason=decision_pointer(runtime, task, "reslice"),
        decision="reslice",
        request_id=_attempt_request_id(
            record.attempt_id or attempt_id, "assessment-reslice", ref
        ),
        terminal_state="blocked",
        disposition="reslice",
        verdict=(
            record.worker_continuation.verdict_outcome
            if record.worker_continuation.verdict_outcome in {"green", "red", "blocked"}
            else "missing"
        ),
        blocked_reason=None,
    )
    resume_workspaces = payload.setdefault("resume_workspaces", {})
    if isinstance(resume_workspaces, dict):
        resume_workspaces[ref] = record.attempt_id or attempt_id
    records.pop(ref, None)
    runtime.save_records(payload, records)
    return {
        "status": "ok",
        "step": "assessment",
        "pilot_ref": ref,
        "attempt_id": attempt_id,
        "to": "blocked",
        "decision": "reslice",
    }
