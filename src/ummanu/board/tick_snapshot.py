"""Selection views owned by one production tick. Live reads and writes stay live."""

from __future__ import annotations

import contextlib
import contextvars
import copy
from collections.abc import Iterator
from typing import Any

from ummanu.board.backend import entity_number


class TickSnapshot:
    def __init__(self, reader: Any) -> None:
        self.client = getattr(reader, "client", reader)
        self.reader = reader
        self.cards: dict[str, dict[str, Any]] = {}
        self.sprints: dict[str, dict[str, Any]] | None = None
        self.stale: set[str] = set()
        self.stale_sprints: set[str] = set()
        self.archive_error: Exception | None = None
        self.references_by_id: dict[int, str] = {}

    def remember(self, card: dict[str, Any]) -> None:
        kind = "sprint" if card["ref"].startswith("sprint:") else "task"
        number = entity_number(kind, card.get("id"))
        if number is not None:
            self.references_by_id[number] = card["ref"]

    def load(self) -> None:
        from ummanu.tasks import TaskError

        self.cards = {card["ref"]: card for card in self.reader.list()}
        # Lightweight normalized test readers need only list/show. The production reader
        # supplies the filtered archive query, never a restore of the whole archive.
        if hasattr(type(self.reader), "archived_after_merge_cards"):
            try:
                for card in self.reader.archived_after_merge_cards():
                    self.cards.setdefault(card["ref"], card)
            except (TaskError, ValueError, TypeError, KeyError) as exc:
                self.archive_error = exc
        for card in self.cards.values():
            self.remember(card)

    def select(self, reader: Any, *, sprints: bool = False, **filters: Any) -> list[dict[str, Any]]:
        from ummanu.tasks import TaskError

        if sprints and self.sprints is None:
            self.sprints = {sprint["ref"]: sprint for sprint in reader.list(create=filters.get("create", True))}
            for sprint in self.sprints.values():
                self.remember(sprint)
        entries = self.sprints if sprints else self.cards
        assert entries is not None
        stale = self.stale_sprints if sprints else self.stale
        # Even a newly created entry is refreshed by key. A failed/rolled-back write is
        # invalidated too; an uncertain effect never leaves a trusted selection behind.
        for ref in sorted(stale):
            try:
                entries[ref] = reader.show(ref, include_cards=False) if sprints else reader.show(ref)
                self.remember(entries[ref])
            except TaskError as exc:
                if getattr(exc, "code", None) != "not_found":
                    raise
                entries.pop(ref, None)
            stale.discard(ref)
        result = []
        for card in entries.values():
            if sprints:
                if filters.get("statuses") and card["status"] not in filters["statuses"]:
                    continue
            else:
                if not filters.get("include_archived") and card.get("closed"):
                    continue
                if filters.get("states") and card["state"] not in filters["states"]:
                    continue
                if any(filters.get(key) is not None and card.get(key) != filters[key] for key in ("project", "sprint")):
                    continue
            result.append(copy.deepcopy(card))
        return sorted(result, key=lambda card: (card.get("status", card.get("state", "")), card.get("position", 0), card["ref"], card.get("id", "")))

    def changed(self, params: dict[str, Any]) -> None:
        reference = params.get("reference") or params.get("task_ref")
        sprint_ref = params.get("sprint_ref")
        if reference:
            (self.stale_sprints if str(reference).startswith("sprint:") else self.stale).add(reference)
        if sprint_ref:
            self.stale_sprints.add(sprint_ref)
        number = params.get("task_id", params.get("id"))
        if number in self.references_by_id:
            ref = self.references_by_id[number]
            (self.stale_sprints if ref.startswith("sprint:") else self.stale).add(ref)


_CURRENT: contextvars.ContextVar[TickSnapshot | None] = contextvars.ContextVar("tick_board_snapshot", default=None)


@contextlib.contextmanager
def tick_snapshot(reader: Any) -> Iterator[TickSnapshot]:
    snapshot = TickSnapshot(reader)
    token = _CURRENT.set(snapshot)
    try:
        yield snapshot
    finally:
        _CURRENT.reset(token)


def current_snapshot(reader: Any) -> TickSnapshot | None:
    snapshot = _CURRENT.get()
    return snapshot if snapshot is not None and snapshot.client is getattr(reader, "client", reader) else None


def select_cards(reader: Any, **filters: Any) -> list[dict[str, Any]]:
    snapshot = current_snapshot(reader)
    return snapshot.select(reader, **filters) if snapshot is not None else reader.list(**filters)


def select_sprints(reader: Any, **filters: Any) -> list[dict[str, Any]]:
    snapshot = current_snapshot(reader)
    return snapshot.select(reader, sprints=True, **filters) if snapshot is not None else reader.list(**filters)


def board_write(client: Any, params: dict[str, Any]) -> None:
    """Writer transport boundary; selections are never used to authorize the effect."""
    snapshot = _CURRENT.get()
    if snapshot is not None and snapshot.client is client:
        snapshot.changed(params)
