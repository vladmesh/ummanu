"""Durable owner turns on PO cards, written with card audit and owner events.

The released three-field waiting_owner mark remains the unanswered explicit handover.
An answer clears it atomically and stores owner_answer (JSON text) with the handover and
answer audit IDs, verbatim quotation, original mark, session and answer time. Delivery
uses this record even after the initial PO turn ended. owner_escalation (JSON text) names
an unresolved PO ownership/answer episode and its explicit reason. Transitions settle it;
new handovers replace it. No read creates or clears these facts.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from ummanu.board.extension_bag import EXTENSION_BAG

WAITING_OWNER = "waiting_owner"
WAITING_OWNER_REASON = "waiting_owner_reason"
WAITING_OWNER_BY = "waiting_owner_by"
MARK_KEYS = (WAITING_OWNER, WAITING_OWNER_REASON, WAITING_OWNER_BY)
#: The metadata write that removes the mark: an empty value is a removal from the bag.
CLEAR_MARK = {key: "" for key in MARK_KEYS}
OWNER_ANSWER = "owner_answer"
OWNER_ESCALATION = "owner_escalation"
OWNER_ANSWER_RECORDED = "owner_answer_recorded"


def attention_record(task: Mapping[str, Any], key: str) -> dict[str, Any] | None:
    """Read the board's answer or escalation; these records are written with their audit."""
    try:
        value = json.loads(str(_bag(task).get(key) or "null"))
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def current_handover(events: Iterable[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    found = None
    for event in events:
        if event.get("kind") == HANDED_TO_OWNER:
            found = event
    return found


def po_episode(events: Iterable[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """Released generic claims and native card.started claims share their real audit clock."""
    found = None
    for event in events:
        if event.get("kind") in {"claimed", "card.started"}:
            found = event
    return found

#: The one recipient a card is handed to today.
OWNER = "owner"
#: The comment role, and actor, of the owner's answer on a card (`task comment --role owner`).
OWNER_ROLE = "owner"
#: The first content line of the PO comment `task handover` writes.
HANDOVER_MARKER = "handover:owner"
#: The audit kind of a handover; the owner-event card turns it into an owner event.
HANDED_TO_OWNER = "handed_to_owner"


def mark_values(since: str, reason: str, by: str) -> dict[str, str]:
    """The metadata write that sets the mark."""
    return {WAITING_OWNER: since, WAITING_OWNER_REASON: reason, WAITING_OWNER_BY: by}


def _bag(task: Mapping[str, Any]) -> Mapping[str, Any]:
    extensions = task.get("extensions")
    bag = extensions.get(EXTENSION_BAG) if isinstance(extensions, Mapping) else None
    return bag if isinstance(bag, Mapping) else {}


def carries_mark_fields(task: Mapping[str, Any]) -> bool:
    """Whether any of the three fields is on the card, well-formed or not."""
    bag = _bag(task)
    return any(str(bag.get(key) or "") for key in MARK_KEYS)


def waiting_owner(task: Mapping[str, Any]) -> dict[str, str] | None:
    """The card's mark as `{since, reason, by}`, or None when it carries no well-formed one."""
    bag = _bag(task)
    since = str(bag.get(WAITING_OWNER) or "").strip()
    reason = str(bag.get(WAITING_OWNER_REASON) or "").strip()
    by = str(bag.get(WAITING_OWNER_BY) or "").strip()
    if not (since and reason and by):
        return None
    try:
        datetime.fromisoformat(since)
    except ValueError:
        return None
    return {"since": since, "reason": reason, "by": by}


def render_handover_comment(reason: str) -> str:
    """The comment body `task handover` writes as the PO (the role line is added by the writer)."""
    return f"[{HANDOVER_MARKER}]\n\n{reason.strip()}\n"


def _content_lines(comment: Mapping[str, Any]) -> list[str]:
    lines = str(comment.get("body") or "").splitlines()
    marker = comment.get("marker")
    if lines and marker and lines[0].strip() == f"[{marker}]":
        lines = lines[1:]
    return lines


def owner_comments_since_handover(comments: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    """The owner's comments after the card's latest `[handover:owner]` PO comment, oldest first."""
    found: list[dict[str, str]] = []
    for comment in comments:
        if not isinstance(comment, Mapping):
            continue
        lines = _content_lines(comment)
        if comment.get("marker") == "po" and lines and lines[0].strip() == f"[{HANDOVER_MARKER}]":
            found = []
        elif comment.get("marker") == OWNER_ROLE:
            found.append(
                {"created_at": str(comment.get("created_at") or ""), "body": "\n".join(lines).strip()}
            )
    return found


def owner_answer_event_ids(events: Iterable[Mapping[str, Any]]) -> list[str]:
    """Event ids of the owner's comments after the card's latest handover, in audit order."""
    found: list[str] = []
    handed = False
    for event in events:
        kind = str(event.get("kind") or "")
        if kind == HANDED_TO_OWNER:
            found = []
            handed = True
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
        if handed and ((kind == "commented" and payload.get("marker") == OWNER_ROLE)
                       or kind == OWNER_ANSWER_RECORDED) and event.get("event_id"):
            found.append(str(event["event_id"]))
    return found


__all__ = [
    "CLEAR_MARK",
    "HANDED_TO_OWNER",
    "HANDOVER_MARKER",
    "MARK_KEYS",
    "OWNER",
    "OWNER_ANSWER",
    "OWNER_ANSWER_RECORDED",
    "OWNER_ESCALATION",
    "OWNER_ROLE",
    "WAITING_OWNER",
    "WAITING_OWNER_BY",
    "WAITING_OWNER_REASON",
    "attention_record",
    "carries_mark_fields",
    "current_handover",
    "mark_values",
    "owner_answer_event_ids",
    "owner_comments_since_handover",
    "po_episode",
    "render_handover_comment",
    "waiting_owner",
]
