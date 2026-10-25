"""Resolve whether a card's current sprint supplies an observer decision owner."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from ummanu.board.audit_contract import is_protocol_event
from ummanu.board.task_protocol.rows import assessment_resolution
from ummanu.kernel.errors import TaskError

AUTOMATIC_ASSESSMENT_REASON = "green code completion; Assessment automatically releases without an observer"


def automatic_assessment_origin(events: Iterable[dict[str, Any]]) -> bool:
    """Only the current committed dispatcher Assessment entry can prove automatic origin."""
    ordered = list(events)
    visit, _decision = assessment_resolution(ordered)
    if not visit:
        return False
    for event in reversed(ordered):
        if event.get("event_id") != visit:
            continue
        actor = event.get("actor")
        transition = event.get("transition")
        return bool(
            is_protocol_event(event) and event.get("kind") == "card.assessed"
            and event.get("request_id")
            and isinstance(actor, dict) and actor.get("role") == "dispatcher"
            and isinstance(transition, dict) and transition.get("source") in {"ci", "review"}
            and transition.get("target") == "assessment"
            and event.get("reason") == AUTOMATIC_ASSESSMENT_REASON
        )
    return False


def observer_managed(task: Mapping[str, Any], show: Callable[[str], dict[str, Any]]) -> bool:
    """Unreadable sprint or observer context refuses routing rather than authorizing release."""
    reference = str(task.get("sprint") or "")
    if not reference:
        return False
    sprint = show(reference)
    status = sprint.get("status")
    if status in {"closed", "stopped"}:
        return False
    if status == "open":
        if "observer" not in sprint or sprint["observer"] == {"kind": "none"}:
            return False
        observer = sprint["observer"]
        if isinstance(observer, dict) and observer.get("kind") == "head" and observer.get("profile"):
            return True
    raise TaskError("routing_context_unknown", "current sprint/observer routing context is unreadable", 3)
