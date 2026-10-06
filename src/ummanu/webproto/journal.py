"""Paged, resumable reading of one card's slice of the committed board audit.

Reads through the audit owner :func:`ummanu.tasks.task_audit_for` returns for the card client
(`docs/BOARD_STORE.md` §7.3), never the file projection under `<data>/board`. Both record shapes
are returned (typed protocol events and generic audit records, told apart by ``typed``); a record
whose typed payload does not parse is still returned, never dropped. Uncommitted staged records are
not events and no cursor lands inside them. See docs/PROTOCOLS.md "Continuing a read".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ummanu.board.models import Event
from ummanu.webproto import sources
from ummanu.webproto.cursor import Cursor
from ummanu.webproto.errors import InvalidCursor
from ummanu.webproto.sources import Source

#: What a caller gets when it asks for a page without saying how big.
DEFAULT_LIMIT = 50
#: The ceiling a caller cannot raise; a client pages through longer histories with the cursor.
MAX_LIMIT = 500


@dataclass(frozen=True, slots=True)
class EventPage:
    """One page of a card's history, and where reading continues."""

    items: tuple[dict[str, Any], ...]
    next_cursor: Cursor
    has_more: bool
    source: Source


class CommittedAudit:
    """The read-only reader of a card's committed history in the store its audit owner holds.

    Pages :meth:`~ummanu.board.sql_audit.SqlTaskAudit.events` with no store, index or cache of its
    own. A position is an ordinal (this card's committed records before the next one); the audit
    only grows and claim order never changes, so a cursor always reads back the same page.
    """

    #: `TaskError` is a translated driver failure; the rest are records the traversal cannot
    #: convert. An unreadable audit is a source fact, never an empty history.
    _FAILURES = (OSError, ValueError, KeyError, TypeError)

    def __init__(self, audit: Any, *, backend: str = "postgres") -> None:
        self.audit = audit
        self.backend = backend

    def page(self, ref: str, *, cursor: Cursor | None, limit: int, now: float) -> EventPage:
        """The next ``limit`` committed records for ``ref`` at or after ``cursor``."""
        start = 0 if cursor is None else cursor.offset
        bounded = max(1, min(int(limit), MAX_LIMIT))
        try:
            records = self._records(ref)
        except self._failures() as exc:
            return self._unreadable(ref, cursor, exc, now=now)
        if start > len(records):
            raise InvalidCursor(
                "this cursor is past the end of the committed board audit, which only ever grows; "
                "the audit it was issued for is not the audit being read"
            )
        window = records[start : start + bounded]
        position = start + len(window)
        return EventPage(
            items=tuple(
                _item(record, self._at(ref, start + index + 1))
                for index, record in enumerate(window)
            ),
            next_cursor=self._at(ref, position),
            has_more=len(window) >= bounded and position < len(records),
            source=sources.available(now),
        )

    def tail(self, ref: str, *, limit: int, now: float) -> EventPage:
        """The last ``limit`` committed records for ``ref``, and the cursor that continues them.

        The cursor is the end of the history, so a polling client is never re-handed an event.
        """
        return self.tail_with_history(ref, limit=limit, now=now)[0]

    def tail_with_history(
        self, ref: str, *, limit: int, now: float
    ) -> tuple[EventPage, tuple[dict[str, Any], ...] | None]:
        """`tail`, and every committed record of the card as its `kind` and `data`, from one traversal.

        The history is `None` when the audit did not answer, which the page's source already says.
        """
        bounded = max(1, min(int(limit), MAX_LIMIT))
        try:
            records = self._records(ref)
        except self._failures() as exc:
            return self._unreadable(ref, None, exc, now=now), None
        start = max(0, len(records) - bounded)
        page = EventPage(
            items=tuple(
                _item(record, self._at(ref, start + index + 1))
                for index, record in enumerate(records[start:])
            ),
            next_cursor=self._at(ref, len(records)),
            has_more=False,
            source=sources.available(now),
        )
        return page, tuple(_brief(record) for record in records)

    def history(self, ref: str) -> tuple[dict[str, Any], ...]:
        """Every committed record of the card as its `kind` and `data`; a store failure raises."""
        return tuple(_brief(record) for record in self._records(ref))

    def _records(self, ref: str) -> tuple[dict[str, Any], ...]:
        """This card's committed records in claim order, both record shapes, as the owner traverses them."""
        return tuple(
            record for record in self.audit.events(ref) if isinstance(record, dict)
        )

    def _at(self, ref: str, ordinal: int) -> Cursor:
        return Cursor(ref=ref, offset=ordinal)

    def _failures(self) -> tuple[type[BaseException], ...]:
        from ummanu.tasks import TaskError

        return (TaskError, *self._FAILURES)

    def _unreadable(self, ref: str, cursor: Cursor | None, exc: Exception, *, now: float) -> EventPage:
        """An audit that would not answer, as a source fact, never an empty page.

        Hands back the caller's own cursor, so a polling client resumes where it stopped.
        """
        return EventPage(
            items=(),
            next_cursor=cursor or self._at(ref, 0),
            has_more=False,
            source=sources.unavailable(
                f"the committed {self.backend} board audit could not be read: {_reason(exc)}",
                now=now,
            ),
        )


def _reason(exc: Exception) -> str:
    return getattr(exc, "message", None) or str(exc) or type(exc).__name__


def _payload(record: dict[str, Any]) -> Any:
    """What a record says: a typed event's `data`, a generic audit record's `payload`."""
    return record.get("data") if record.get("record_type") == Event.RECORD_TYPE else record.get("payload")


def _brief(record: dict[str, Any]) -> dict[str, Any]:
    """A record reduced to its `kind` and its `data`, for a reader of the whole history."""
    payload = _payload(record)
    return {"kind": _text(record.get("kind")), "data": payload if isinstance(payload, dict) else {}}


def _item(record: dict[str, Any], cursor: Cursor) -> dict[str, Any]:
    """One journal record in the layer's stable event shape."""
    typed = record.get("record_type") == Event.RECORD_TYPE
    actor = record.get("actor")
    transition = record.get("transition")
    related = record.get("related_refs")
    payload = _payload(record)
    return {
        "cursor": cursor.encode(),
        "typed": typed,
        "kind": _text(record.get("kind")),
        "event_id": _text(record.get("event_id")) or None,
        "request_id": _text(record.get("request_id")) or None,
        "ref": _text(record.get("ref")),
        "occurred_at": _text(record.get("occurred_at")) or None,
        "actor": (
            {"role": _text(actor.get("role")), "id": _text(actor.get("id"))}
            if isinstance(actor, dict)
            else None
        ),
        # Typed events carry the reason their writer gave; generic audit records carry the outcome
        # of the backend effect. Neither is renamed into the other's field.
        "reason": _text(record.get("reason")) or None,
        "outcome": _text(record.get("outcome")) or None,
        "transition": (
            {"source": _text(transition.get("source")), "target": _text(transition.get("target"))}
            if isinstance(transition, dict)
            else None
        ),
        "related_refs": [ref for ref in related if isinstance(ref, str)] if isinstance(related, list) else [],
        "data": payload if isinstance(payload, dict) else {},
    }


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""
