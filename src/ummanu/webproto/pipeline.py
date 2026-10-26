"""Read-only stage and SHA-bound gate display shared by the board, card and native CLI."""

from __future__ import annotations

from typing import Any

from ummanu.board.legacy_codec import TASK_STATE_BY_COLUMN
from ummanu.dispatch.gate_receipt import is_exact_sha
from ummanu.dispatch.state import REVIEW_REJECTION_REASON, DispatcherRecord
from ummanu.webproto import sources

STAGE_LABELS = {state: title for title, state in TASK_STATE_BY_COLUMN.items()}


def records_from(payload: Any) -> dict[str, DispatcherRecord]:
    """Decode the production records without migration, recovery or runtime probes."""
    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, dict):
        raise TypeError("the dispatcher production state carries no records object")
    if any(not isinstance(ref, str) or not isinstance(record, dict) for ref, record in records.items()):
        raise TypeError("the dispatcher production state carries an unreadable record")
    return {reference: DispatcherRecord.from_json(record) for reference, record in sorted(records.items())}


def pipeline_summary(
    card: dict[str, Any] | None, record: DispatcherRecord | None, source: sources.Source,
) -> dict[str, Any]:
    """Display recorded facts; a receipt describes its SHA and grants no admission authority."""
    card = card or {}
    stage = card.get("state")
    label = STAGE_LABELS.get(stage, "Stage unknown")
    ci = {"status": "unknown", "sha": None, "reason": None}
    result = {
        "stage": stage, "label": label, "tone": "", "ci": ci,
        "previous": None, "source": source.to_json(),
    }
    if card.get("type") and card["type"] != "code":
        ci["status"] = "not_applicable"
        return result
    if card.get("type") != "code":
        ci["reason"] = "The card type is unknown"
    elif source.state != sources.AVAILABLE:
        ci["reason"] = source.reason
    elif record is None:
        ci["reason"] = "No dispatcher attempt is recorded"
    else:
        previous = _rejection(record)
        result["previous"] = previous
        transport = record.gate_transport_error or record.gate_rerun_transport_error
        if transport or record.gate_transport_failures or record.gate_rerun_transport_failures:
            ci["reason"] = transport or "The gate backend did not answer"
        elif (
            previous and stage == "ci" and record.worker_continuation.red_transition_pending
            and record.worker_continuation.verdict_outcome == "gate_red"
        ):
            ci.update({key: previous[key] for key in ci})
            result["previous"] = None
        elif stage == "ci":
            ci["status"] = "pending" if record.gate_pending_since else "waiting"
        elif stage in {"review", "assessment", "done", "post_merge_ci"}:
            receipt = record.gate_attestation.receipt
            if (
                record.gate_state == "green" and receipt is not None
                and (not record.review_commit or record.review_commit == receipt.validated_sha)
            ):
                ci.update(status="green", sha=receipt.validated_sha)
            else:
                ci["reason"] = "No matching executed exact-SHA gate receipt is recorded"
        elif stage == "in_progress" and previous:
            ci["reason"] = "The worker is preparing a new report"

    status = ci["status"]
    if stage == "ci":
        result["label"] = {
            "waiting": "CI waiting", "pending": "CI pending", "red": "CI red; returning to worker",
            "not_run": "Publication failed; CI did not run",
        }.get(status, "CI status unknown")
        result["tone"] = "bad" if status in {"red", "not_run"} else "warn"
    elif stage == "review":
        result["label"] = "Review" + ("; CI passed" if status == "green" else "; CI status unknown")
        result["tone"] = "accent"
    return result


def _rejection(record: DispatcherRecord) -> dict[str, Any] | None:
    if not is_exact_sha(record.rejected_sha) or record.rejected_failure_reason == REVIEW_REJECTION_REASON:
        return None
    failure = record.rejected_failure_class
    if failure not in {"substantive", "infrastructure", "publication"}:
        return None
    return {
        "status": "not_run" if failure == "publication" else "red",
        "sha": record.rejected_sha,
        "failure_class": failure,
        "reason": record.rejected_failure_reason or None,
        "evidence": [dict(entry) for entry in record.rejected_gate_evidence],
    }
