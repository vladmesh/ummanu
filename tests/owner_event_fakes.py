"""An in-memory `OwnerEventStore` for unit tests: no PostgreSQL, the same contract.

It keeps what `owner_events` holds as the database's: the CHECKs on `kind`, on `class` and on the class
following the kind (from `owner_events.KINDS`, `CLASSES` and `KIND_CLASS`, the lists the schema is
spelled from), the unique `dedup_key`, and the refusal to mark a `needs_owner` event read by hand while
its card carries the `waiting_owner` mark. `cards` answers a subject ref with the card as the store
holds it (`extensions`), or None. `missing_table` makes every operation refuse the way a board without
migration 0018 does.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from ummanu.board.owner_events import (
    CARD_HANDED_TO_OWNER,
    CLASSES,
    KIND_CLASS,
    KINDS,
    NEEDS_OWNER,
    NOTICE,
    PO_CARD_ESCALATED,
    OwnerEvent,
    OwnerEventNotFound,
    OwnerEventsUnavailable,
    ReadRefused,
    _held_refusal,
    card_holds_mark,
    class_of,
    list_order_key,
    person_wait,
)


class CheckViolation(Exception):
    """What PostgreSQL raises for a row outside a CHECK; the real store maps it to unavailable."""


class FakeOwnerEvents:
    def __init__(self, cards: Callable[[str], Mapping[str, Any] | None] | None = None) -> None:
        self.lock = threading.RLock()
        self.rows: dict[int, OwnerEvent] = {}
        self.cards = cards or (lambda _ref: None)
        self.missing_table = False
        self.failing: Exception | None = None
        self._clock = datetime(2026, 9, 26, 12, tzinfo=UTC)

    def _now(self) -> datetime:
        self._clock += timedelta(seconds=1)
        return self._clock

    def _answer(self) -> None:
        if self.missing_table:
            raise OwnerEventsUnavailable(
                "the board store has no owner_events table yet: migration 0018 is not applied"
            )
        if self.failing is not None:
            raise self.failing

    def _held(self, event: OwnerEvent) -> bool:
        card = self.cards(str(event.subject_ref)) if event.subject_ref else None
        return bool(event.unread and card and card.get("state") == "in_progress" and not card.get("closed")
                    and ((event.kind == CARD_HANDED_TO_OWNER and card_holds_mark(card))
                         or (event.kind == PO_CARD_ESCALATED and person_wait(card))))

    @staticmethod
    def check(kind: str, event_class: str) -> None:
        if kind not in KINDS:
            raise CheckViolation(f"owner_event_kind_in_vocabulary: {kind!r}")
        if event_class not in CLASSES:
            raise CheckViolation(f"owner_event_class_in_vocabulary: {event_class!r}")
        if (event_class == NEEDS_OWNER) != (KIND_CLASS[kind] == NEEDS_OWNER):
            raise CheckViolation(f"owner_event_class_follows_kind: {kind!r} is not {event_class!r}")

    # --- the store's operations ------------------------------------------------------------

    def insert(self, kind: str, subject_ref: str | None, text: str, dedup_key: str) -> bool:
        return self.insert_row(kind, class_of(kind), subject_ref, text, dedup_key)

    def insert_row(self, kind: str, event_class: str, subject_ref: str | None, text: str, dedup_key: str) -> bool:
        """The INSERT itself, with a class the caller chose: what the CHECKs are there to refuse."""
        with self.lock:
            self._answer()
            self.check(kind, event_class)
            if any(row.dedup_key == dedup_key for row in self.rows.values()):
                return False
            identifier = len(self.rows) + 1
            self.rows[identifier] = OwnerEvent(
                identifier, kind, event_class, subject_ref, text, self._now(), None, dedup_key
            )
            return True

    def events(self, *, unread_only: bool = False, limit: int = 500) -> list[OwnerEvent]:
        with self.lock:
            self._answer()
            rows = [replace(row, held=self._held(row)) for row in self.rows.values()]
        if unread_only:
            rows = [row for row in rows if row.read_at is None]
        return sorted(rows, key=list_order_key)[:limit]

    def unread_count(self) -> int:
        with self.lock:
            self._answer()
            return sum(1 for row in self.rows.values() if row.read_at is None)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            events = self.events(limit=max(500, len(self.rows)))
            waits = []
            for event in events:
                card = self.cards(str(event.subject_ref)) if event.subject_ref else None
                if event.pinned and card and card.get("sprint") and not card.get("closed") and (
                    event.kind == "steward_needs_human" or event.held
                ):
                    waits.append({"event_id": event.id, "subject_ref": event.subject_ref, "sprint_ref": card["sprint"]})
            return {"events": [event.to_json() for event in events[:500]],
                    "unread_events": [event.to_json() for event in self.events(unread_only=True)],
                    "unread": self.unread_count(), "human_waits": waits,
                    "needs_owner_count": sum(e.unread and e.event_class == NEEDS_OWNER for e in events),
                    "notice_count": sum(e.unread and e.event_class == NOTICE for e in events),
                    "held_count": sum(e.held for e in events)}

    def settle_kind(self, subject_ref: str, kind: str) -> int:
        with self.lock:
            self._answer()
            settled = 0
            for identifier, row in list(self.rows.items()):
                if row.subject_ref == subject_ref and row.kind == kind and row.read_at is None:
                    self.rows[identifier] = replace(row, read_at=self._now())
                    settled += 1
            return settled

    def mark_read(self, event_id: int) -> OwnerEvent:
        with self.lock:
            self._answer()
            row = self.rows.get(int(event_id))
            if row is None:
                raise OwnerEventNotFound(f"there is no owner event {event_id}")
            held = self._held(row)
            if row.read_at is not None:
                return replace(row, held=held)
            if row.event_class == NEEDS_OWNER and held:
                raise ReadRefused(_held_refusal(row))
            self.rows[row.id] = replace(row, read_at=self._now())
            return replace(self.rows[row.id], held=held)

    def mark_all_read(self) -> int:
        with self.lock:
            self._answer()
            marked = 0
            for identifier, row in list(self.rows.items()):
                if row.read_at is None and row.event_class == NOTICE:
                    self.rows[identifier] = replace(row, read_at=self._now())
                    marked += 1
            return marked

    def settle_subject(self, subject_ref: str) -> int:
        with self.lock:
            self._answer()
            settled = 0
            for identifier, row in list(self.rows.items()):
                if row.subject_ref == subject_ref and row.event_class == NEEDS_OWNER and row.read_at is None:
                    self.rows[identifier] = replace(row, read_at=self._now())
                    settled += 1
            return settled

    # --- test conveniences -----------------------------------------------------------------

    def kinds(self) -> list[str]:
        return [row.kind for row in sorted(self.rows.values(), key=lambda row: row.id)]

    def of_kind(self, kind: str) -> list[OwnerEvent]:
        return [row for row in sorted(self.rows.values(), key=lambda row: row.id) if row.kind == kind]
