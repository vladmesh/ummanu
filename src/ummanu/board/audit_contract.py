"""The small, storage-free rules every audit record reader and writer shares.

A card audit record is one of two shapes: a typed board protocol event, whose ``record_type`` is
:data:`PROTOCOL_EVENT_RECORD_TYPE`, or a released generic record (``moved``, ``edited``,
``commented`` and the rest) that carries no discriminator. Recovery, replay and history readers
all have to tell them apart, and a request id is an ownership claim that a replay has to match.
Neither rule depends on where the records are kept, so they live here rather than on the store
that keeps them (:class:`ummanu.board.sql_audit.SqlTaskAudit`).
"""

from __future__ import annotations

from typing import Any

from ummanu.board.models import Event

#: The ``record_type`` of a typed board protocol event; a generic audit record carries none.
PROTOCOL_EVENT_RECORD_TYPE = Event.RECORD_TYPE

_FOREIGN_CLAIM = "request id belongs to another operation or payload"


def is_protocol_event(record: dict[str, Any]) -> bool:
    """Whether an audit record is a typed board protocol event rather than a generic one."""
    return record.get("record_type") == PROTOCOL_EVENT_RECORD_TYPE


def card_transition_of(event: dict[str, Any]) -> tuple[str, str] | None:
    """The board-state transition one audit event records, or `None` when it records none.

    History holds two shapes of the same fact and a reader of the journal has to know both: a typed
    protocol event carries `transition.source` and `transition.target`, and a legacy one is
    `moved` with `payload.from` and `payload.to`. They are read in one place so that a caller asking
    "did this event move the card, and where to" cannot learn only one of them.

    Nothing about *who* moved the card or *why* is decided here: this is the shape and no filter.
    The one thing it does refuse is a legacy event whose outcome is not `success`, because an event
    recording a move that did not happen is not a transition at all -- which is the same reason
    :func:`ummanu.tasks.is_significant_card_event`, the caller this shape was lifted out of, has always asked.
    """
    if is_protocol_event(event):
        typed = event.get("transition") if isinstance(event.get("transition"), dict) else {}
        target = str(typed.get("target") or "")
        return (str(typed.get("source") or ""), target) if target else None
    if str(event.get("kind") or "") != "moved" or str(event.get("outcome") or "") != "success":
        return None
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    target = str(payload.get("to") or "")
    return (str(payload.get("from") or ""), target) if target else None

def require_claim(
    existing: dict[str, Any],
    *,
    kind: str,
    reference: str | None,
    identity: dict[str, Any] | None,
) -> None:
    """Refuse a replay whose caller meant an operation other than the recorded one.

    The event kind, the card ref and the `identity` every write declares all have to match. The only
    fields left out are the ones a retry cannot recompute after the write went through, because they
    describe the state the write replaced: `moved`, `edited`'s digests, and `restored_comment`'s
    body once the comment is known to be on the card. Comparing those would turn a retry into a
    conflict.
    """
    from ummanu.task_errors import TaskError

    if str(existing.get("kind") or "") != kind:
        raise TaskError("validation", _FOREIGN_CLAIM, 2)
    if reference is not None and str(existing.get("ref") or "") != reference:
        raise TaskError("validation", _FOREIGN_CLAIM, 2)
    if not identity:
        return
    payload = existing.get("payload")
    if not isinstance(payload, dict):
        raise TaskError("validation", _FOREIGN_CLAIM, 2)
    for key, value in identity.items():
        if payload.get(key) != value:
            raise TaskError("validation", _FOREIGN_CLAIM, 2)
