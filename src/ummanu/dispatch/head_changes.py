"""Journal a card head's confirmed stop, and its launched successor, as one `head_changed` record.

Called only after the stop was confirmed and, for a replacement, after the successor's bring-up
succeeded. The stopped run must be this card's own (`TaskRef` card binding, attested role); a
standing, sprint or legacy unbound run writes nothing. One record per stopped run: the request id
is that run's identity, and a replay keeps what was first recorded.
"""

from __future__ import annotations

from typing import Any

from ummanu.board.head_changes import head_change_payload
from ummanu.dispatch.card_roles import card_kind, head_role
from ummanu.dispatch.state import DispatcherRecord, attempt_request_id
from ummanu.runtime.head import TASK_CARD, HeadRun, HeadRunError, TaskRefError


def role_run(record: DispatcherRecord, kind: str) -> dict[str, Any]:
    """A copy of the lifecycle run the record holds for one card role."""
    return dict(record.review_head_run if card_kind(kind) == "review" else record.worker_head_run)


def record_head_change(
    runtime: Any,
    ref: str,
    record: DispatcherRecord,
    *,
    kind: str,
    cause: str,
    reason: str,
    before: dict[str, Any],
    successor: bool,
    requested: str = "",
    attempt_id: str = "",
) -> None:
    """Write the change once; `successor` reads the launched run now on the record as `after`."""
    role = head_role(card_kind(kind))
    stopped = _card_run(before, ref, role)
    if stopped is None or stopped.settled:
        # A run already confirmed ended before this operation was not stopped by it.
        return
    after = None
    if successor:
        launched = _card_run(role_run(record, kind), ref, role)
        if launched is None or launched.same_run(stopped):
            raise ValueError(f"the {role} of {ref} was relaunched without a run of its own on the record")
        after = _side(launched)
    request_id = attempt_request_id(
        record.attempt_id or attempt_id, "head-change", ref, f"{role}-{stopped.run_id}"
    )
    if runtime.audit.committed_event(request_id) is not None:
        return
    runtime.writer.head_changed(
        role="dispatcher",
        actor=runtime.owner,
        reference=ref,
        payload=head_change_payload(
            role=role,
            cause=cause,
            reason=reason,
            before=_side(stopped),
            after=after,
            requested=requested,
        ),
        request_id=request_id,
    )


def _card_run(payload: dict[str, Any], ref: str, role: str) -> HeadRun | None:
    """The run when it provably belongs to this card's role; None for any other or unreadable run."""
    if not payload.get("run_id"):
        return None
    try:
        run = HeadRun.from_json(payload)
    except (HeadRunError, TaskRefError):
        return None
    if run.task_ref.kind != TASK_CARD or run.task_ref.ref != ref or run.role not in ("", role):
        return None
    return run


def _side(run: HeadRun) -> dict[str, str]:
    return {
        "run_id": run.run_id,
        "profile": run.spec.profile_id,
        "adapter": run.spec.adapter,
        "model": run.spec.model or "",
        "effort": run.spec.effort,
    }
