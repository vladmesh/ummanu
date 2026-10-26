"""Journal a card head's confirmed stop, and the successor that replaced it, as one `head_changed`.

A confirmed stop of this card's own run (`TaskRef` card binding, attested role) is owed on the
record with its original facts and request id. The role's next launched run completes it as a
replacement; the round's terminal effect completes it as a stop. Standing, sprint, legacy unbound
and already-ended runs owe nothing. A replay commits nothing new and keeps the recorded facts.
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


def owe_head_change(
    record: DispatcherRecord,
    ref: str,
    *,
    kind: str,
    cause: str,
    reason: str,
    before: dict[str, Any],
    requested: str = "",
    attempt_id: str = "",
) -> None:
    """Keep a confirmed stop's facts on the record until its outcome is known; the caller saves it."""
    role = head_role(card_kind(kind))
    stopped = _card_run(before, ref, role)
    if stopped is None or stopped.settled:
        # A run already confirmed ended before this operation was not stopped by it.
        return
    owed = record.head_changes_owed.get(card_kind(kind))
    if owed is not None:
        if owed["before"]["run_id"] == stopped.run_id:
            return
        raise ValueError(f"the {role} of {ref} already owes the change of run {owed['before']['run_id']}")
    record.head_changes_owed = {
        **record.head_changes_owed,
        card_kind(kind): {
            "request_id": attempt_request_id(
                record.attempt_id or attempt_id, "head-change", ref, f"{role}-{stopped.run_id}"
            ),
            "cause": cause,
            "reason": reason,
            "before": _side(stopped),
            "requested": requested,
        },
    }


def settle_head_change(runtime: Any, ref: str, record: DispatcherRecord, *, kind: str, successor: bool) -> None:
    """Write the owed change of one role, then forget it.

    `successor` reads the role's run on the record as `after`; a run that is not a distinct run of
    this card's role is no successor and leaves the change owed.
    """
    owed = record.head_changes_owed.get(card_kind(kind))
    if owed is None:
        return
    role = head_role(card_kind(kind))
    after = None
    if successor:
        launched = _card_run(role_run(record, kind), ref, role)
        if launched is None or launched.run_id == owed["before"]["run_id"]:
            return
        after = _side(launched)
    if runtime.audit.committed_event(owed["request_id"]) is None:
        runtime.writer.head_changed(
            role="dispatcher",
            actor=runtime.owner,
            reference=ref,
            payload=head_change_payload(
                role=role,
                cause=owed["cause"],
                reason=owed["reason"],
                before=owed["before"],
                after=after,
                requested=owed["requested"],
            ),
            request_id=owed["request_id"],
        )
    record.head_changes_owed = {
        owed_kind: change for owed_kind, change in record.head_changes_owed.items() if owed_kind != card_kind(kind)
    }


def settle_unreplaced(runtime: Any, ref: str, record: DispatcherRecord) -> None:
    """Every change still owed ends without a successor: the round leaves with this record."""
    for kind in tuple(record.head_changes_owed):
        settle_head_change(runtime, ref, record, kind=kind, successor=False)


def record_head_stop(
    runtime: Any,
    ref: str,
    record: DispatcherRecord,
    *,
    kind: str,
    cause: str,
    reason: str,
    before: dict[str, Any],
    attempt_id: str = "",
) -> None:
    """A confirmed stop no successor follows, written at once."""
    owe_head_change(record, ref, kind=kind, cause=cause, reason=reason, before=before, attempt_id=attempt_id)
    settle_head_change(runtime, ref, record, kind=kind, successor=False)


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
