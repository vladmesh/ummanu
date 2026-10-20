"""Durable Assessment parking: the intent on disk, then the one idempotent board move."""

from __future__ import annotations

from typing import Any

from ummanu.dispatch.candidate_ledger import ledger_entry
from ummanu.dispatch.state import DispatcherRecord, attempt_request_id as _attempt_request_id
from ummanu.dispatch.watchdog import reset_wait as _reset_wait


def begin_park(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    *,
    verdict_outcome: str,
    move_reason: str,
    reviewed_commit: str = "",
    phase: str = "review",
) -> dict[str, Any]:
    """The only way into Assessment: a substantive verdict from Validate, or a partial report.

    The red transition's order, for the same reason: the intent is on disk, with the reason the
    card is moving, before anything observable moves. Nothing comes after the move — the card waits.
    """
    ref = task["ref"]
    # Re-pinned after the reviewer's pane was forgotten: the merge gate refuses a release for
    # a checkout that moved off the reviewed commit, and the park is exactly that window.
    record.review_commit = reviewed_commit or record.review_commit
    record.worker_continuation.begin_park(
        phase, len(task.get("comments") or []), move_reason, verdict_outcome
    )
    records[ref] = record
    runtime.save_records(payload, records)
    return complete_park(runtime, record, records, payload, attempt_id, ref=ref)


def complete_park(
    runtime: Any,
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    *,
    ref: str,
) -> dict[str, Any]:
    """Finish an open park from the board as it is now.

    Keyed on the baseline the intent was opened against, so the tick that already moved the card
    and the tick recovering from a crash before that move run the same call and it moves once.
    """
    continuation = record.worker_continuation
    rejection = (
        ledger_entry(runtime, ref, record)
        if continuation.phase == "review" and continuation.verdict_outcome == "red"
        else None
    )
    runtime.writer.move(
        role="dispatcher",
        actor=runtime.owner,
        reference=ref,
        target="assessment",
        reason=continuation.move_reason,
        request_id=_attempt_request_id(
            record.attempt_id or attempt_id,
            f"{continuation.phase}-assessment",
            ref,
            str(continuation.report_baseline),
        ),
        **({"rejected_candidate": rejection} if rejection is not None else {}),
    )
    continuation.confirm_park()
    record.state = "assessment"
    _reset_wait(record, "review")
    records[ref] = record
    runtime.save_records(payload, records)
    return {
        "status": "ok",
        "step": "advance" if continuation.phase == "worker" else "review",
        "pilot_ref": ref,
        "attempt_id": attempt_id,
        "to": "assessment",
        "verdict": continuation.verdict_outcome,
    }
