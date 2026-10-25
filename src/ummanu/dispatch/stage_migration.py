"""Reconcile live code validation checkpoints without opening another round."""

from __future__ import annotations

from typing import Any

from ummanu.dispatch.state import DispatcherRecord, attempt_request_id
from ummanu.dispatch.types import HostError


def reconcile_validation(
    runtime: Any,
    task: dict[str, Any],
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
) -> dict[str, Any]:
    """Move the supported Validate producer to CI or its already requested Review stage."""
    if task.get("type") != "code" or task["state"] not in {"validate", "ci", "review"}:
        return task
    ref = task["ref"]
    task = runtime.reader.show(ref)
    if task["state"] not in {"validate", "ci", "review"}:
        return task
    record = records.get(ref)
    if record is None:
        record = runtime._adopt(task, attempt_id)
        records[ref] = record
        runtime.save_records(payload, records)
    if task["state"] == "validate":
        launched = runtime._review_launch_recorded(task, record.review_baseline)
        reviewing = record.state in {"review_starting", "reviewing"} or launched
        if reviewing and record.gate_state != "green":
            raise HostError("Validate review checkpoint has no accepted gate; the round is retained")
        target = "review" if reviewing else "ci"
        runtime.writer.move(
            role="dispatcher", actor=runtime.owner, reference=ref, target=target,
            reason=f"Migrate live code Validate checkpoint to {target}; retain the current round.",
            request_id=attempt_request_id(
                record.attempt_id or attempt_id, "validation-stage-migration", ref,
                str(record.report_generation),
            ),
        )
        task = runtime.reader.show(ref)
    if record.state in {"validate", "adopted"} or (
        record.state == "claimed" and record.worker_continuation.validation_move_pending
    ):
        record.state = "ci" if task["state"] == "ci" else "review_starting"
        runtime.save_records(payload, records)
    return task
