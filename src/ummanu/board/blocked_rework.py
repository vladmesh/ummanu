"""A rework decision recorded on a Blocked card, and the visit it belongs to.

The decision carries the event id of the card's move into Blocked under `BLOCKED_VISIT_KEY`, where an
Assessment decision carries `assessment_visit`. The same write returns the card to Ready.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from ummanu.board.audit_contract import card_transition_of
from ummanu.board.task_protocol.rows import _event_action, _event_payload, assessment_resolution
from ummanu.dispatch.ownership import reference_lock

BLOCKED_VISIT_KEY = "blocked_visit"


def blocked_rework(events: Iterable[dict[str, Any]]) -> tuple[str, dict[str, Any] | None]:
    """The card's current Blocked visit and the rework decision recorded during it.

    The visit is the last move into Blocked, until the card is parked in Assessment or starts work.
    """
    visit, decided = "", None
    for event in events:
        transition = card_transition_of(event)
        if transition is not None:
            if transition[1] == "blocked":
                visit, decided = str(event.get("event_id") or event.get("request_id") or ""), None
            elif transition[1] in {"assessment", "in_progress"}:
                visit, decided = "", None
            continue
        payload = _event_payload(event)
        if (
            visit
            and decided is None
            and _event_action(event) == "decided"
            and payload.get("decision") == "rework"
            and payload.get(BLOCKED_VISIT_KEY) == visit
        ):
            decided = event
    return visit, decided


def decision_resolution(events: Iterable[dict[str, Any]]) -> tuple[str, dict[str, Any] | None]:
    """The visit a frozen decision is read from: a Blocked visit holding a rework, else the Assessment park."""
    ordered = list(events)
    visit, decided = blocked_rework(ordered)
    return (visit, decided) if decided is not None else assessment_resolution(ordered)


def decision_visit(data: dict[str, Any]) -> str:
    """The visit one decision payload names."""
    return str(data.get(BLOCKED_VISIT_KEY) or data.get("assessment_visit") or "")


def blocked_decision_refusal(state: str, decision: str, visit: str) -> str:
    """Why `decision` may not be recorded on a card in `state` during `visit`, or ""."""
    if state == "assessment":
        return ""
    if state != "blocked":
        return "a decision is only recorded on a card in Assessment, or a rework on a Blocked card"
    if decision != "rework":
        return f"a Blocked card takes only a rework decision; {decision} is decided on a card in Assessment"
    if not visit:
        return "the card's move into Blocked is not in its audit, so no rework can be bound to it"
    return ""


def return_blocked_rework(
    writer: Any,
    *,
    blocked: bool,
    role: str,
    actor: str,
    reference: str,
    body: str,
    request_id: str,
    write: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    """Run a decision's marker `write`; for a Blocked card, move it to Ready in the same transaction."""
    if not blocked:
        return write()
    with reference_lock(writer.data_dir, reference), writer._mutation():
        decided = write()
        writer.move(
            role=role,
            actor=actor,
            reference=reference,
            target="ready",
            reason=body,
            request_id=f"{request_id}-return-ready",
        )
    return {**decided, "task": writer.reader.show(reference)}
