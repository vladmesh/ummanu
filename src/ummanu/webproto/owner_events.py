"""The owner's bell: owner events read from the board, and marked read (secretary-1770).

Every answer comes from one statement snapshot of the board store's `owner_events` table through
:class:`ummanu.board.owner_events.OwnerEventStore`. A response pins the list, count and scoped
human waits together; nothing is held between requests.

A board without the table (migration `0018` not applied yet) or a store that does not answer reads as
no events, with the source state `unavailable` and the reason: the bell says it cannot count, and no
page fails over it. The two writes refuse instead, since writing to a table that is not there is not
something a page can pretend to have done.

The rules are the store's (`board.owner_events`): a click marks one event read, and refuses a
`needs_owner` event whose card still carries `waiting_owner`; "mark all read" takes notices only.
"""

from __future__ import annotations

import copy
import time
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from ummanu.board.owner_events import (
    OwnerEventError,
    OwnerEventNotFound,
    OwnerEventStore,
    OwnerEventsUnavailable,
    ReadRefused,
)
from ummanu.webproto import sources
from ummanu.webproto.errors import OwnerConflict, OwnerEventMissing, RuntimeUnavailable

SCHEMA_VERSION = 1
AVAILABLE = "available"
UNAVAILABLE = "unavailable"


class OwnerEventLayer:
    """One installation's owner events. Construction does no I/O; `store` is the seam a test supplies."""

    def __init__(
        self,
        instance: str | Path,
        *,
        store: Any | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.instance = Path(instance)
        self._store = store
        self._clock = clock
        self._pin: ContextVar[dict[str, Any] | None] = ContextVar(f"owner-events-{id(self)}", default=None)

    def _events(self) -> Any:
        if self._store is not None:
            return self._store
        instance = self.instance.parent if self.instance.is_file() else self.instance
        try:
            return OwnerEventStore.for_instance(instance)
        except Exception as exc:  # noqa: BLE001 - an unusable store configuration is an unavailable source
            raise OwnerEventsUnavailable(f"the board store is not usable: {exc}") from None

    # -- reads -----------------------------------------------------------------------------

    @contextmanager
    def one_reading(self):
        token = self._pin.set({})
        try:
            yield
        finally:
            self._pin.reset(token)

    def snapshot(self) -> dict[str, Any]:
        """One board statement for count/list/scoped waits, pinned for one rendered response."""
        pin = self._pin.get()
        if pin is not None and "reading" in pin:
            return copy.deepcopy(pin["reading"])
        try:
            document = {"state": AVAILABLE, "reason": None, **self._events().snapshot()}
        except OwnerEventError as exc:
            document = {"state": UNAVAILABLE, "reason": str(exc), "events": [], "unread_events": [], "unread": 0, "human_waits": []}
        if pin is not None:
            pin["reading"] = document
        return copy.deepcopy(document)

    def owner_event_list(self, *, unread_only: bool = False) -> dict[str, Any]:
        """Every event, open `needs_owner` ones first then newest first; `unread_only` keeps the unread."""
        now = self._clock()
        snapshot = self.snapshot()
        return self._document(
            now,
            state=snapshot["state"],
            reason=snapshot["reason"],
            events=snapshot["unread_events"] if unread_only else snapshot["events"],
            unread=snapshot["unread"],
            unread_only=unread_only,
            held_count=snapshot.get("held_count", sum(e.get("held", False) and e.get("class") == "needs_owner" for e in snapshot["unread_events"])),
            needs_owner_count=snapshot.get("needs_owner_count", sum(e.get("class") == "needs_owner" for e in snapshot["unread_events"])),
            notice_count=snapshot.get("notice_count", sum(e.get("class") == "notice" for e in snapshot["unread_events"])),
        )

    def unread_count(self) -> dict[str, Any]:
        """The bell: how many events are unread, or why that cannot be said."""
        snapshot = self.snapshot()
        return {"state": snapshot["state"], "reason": snapshot["reason"], "count": snapshot["unread"]}

    # -- writes ----------------------------------------------------------------------------

    def mark_read(self, event_id: int | str) -> dict[str, Any]:
        """One click: this event read, unless it needs the owner and its card still waits for them."""
        try:
            identifier = int(str(event_id))
        except ValueError:
            raise OwnerEventMissing(f"there is no owner event {event_id!r}") from None
        try:
            event = self._events().mark_read(identifier)
        except OwnerEventNotFound as exc:
            raise OwnerEventMissing(str(exc)) from None
        except ReadRefused as exc:
            raise OwnerConflict(str(exc)) from None
        except OwnerEventError as exc:
            raise RuntimeUnavailable(str(exc)) from None
        return {"schema_version": SCHEMA_VERSION, "kind": "owner_event_read", "event": event.to_json()}

    def mark_all_read(self) -> dict[str, Any]:
        """Every unread notice read; a `needs_owner` event is never touched by this."""
        try:
            marked = int(self._events().mark_all_read())
        except OwnerEventError as exc:
            raise RuntimeUnavailable(str(exc)) from None
        remaining = self._events().snapshot()
        held = [e for e in remaining["unread_events"] if e.get("held") and e.get("class") == "needs_owner"]
        return {"schema_version": SCHEMA_VERSION, "kind": "owner_events_read", "marked": marked,
                "remaining": remaining["unread"],
                "needs_owner_count": remaining.get("needs_owner_count", sum(e.get("class") == "needs_owner" for e in remaining["unread_events"])),
                "held": held, "held_count": remaining.get("held_count", len(held))}

    @staticmethod
    def _document(
        now: float,
        *,
        state: str,
        reason: str | None,
        events: list[dict[str, Any]],
        unread: int,
        unread_only: bool,
        notice_count: int,
        held_count: int,
        needs_owner_count: int,
    ) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "owner_events",
            "observed_at": sources.isoformat(now),
            "source": {"state": state, "reason": reason},
            "unread_only": unread_only,
            "unread": unread,
            "notice_count": notice_count,
            "held_count": held_count,
            "needs_owner_count": needs_owner_count,
            "events": events,
        }


__all__ = ["OwnerEventLayer"]
