"""An in-memory origin-return outbox for unit tests: no PostgreSQL, the same contract (secretary-1792).

It keeps what `origin_returns` holds as the database's: one row per transition into Done or Blocked of
a card with a PO origin that is not a wait card (the rule `origin_outbox.enqueue` applies inside the
commit), the event id unique, rows read undelivered in id order, and a delivery marked once. A board
double calls :meth:`FakeOriginOutbox.enqueue` where the store's commit would.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from ummanu.board.completion_evidence import is_wait
from ummanu.board.origin_outbox import STATUSES, OutboxRow, owed_target
from ummanu.board.po_origin import po_origin


class SimulatedCrash(Exception):
    """The dispatcher process died here."""


class FakeOriginOutbox:
    def __init__(self) -> None:
        self.rows: list[OutboxRow] = []
        #: When set, the next `mark` for which it answers true raises `SimulatedCrash` before writing.
        self.crash_before: Any = None
        self.pending_reads = 0
        self._clock = datetime(2026, 9, 27, 12, tzinfo=UTC)

    def enqueue(self, card: dict[str, Any], request_id: str, event: dict[str, Any]) -> bool:
        """What the store writes in the commit of `event` on `card`: the row it owes, or nothing."""
        target = owed_target(event)
        if not target or po_origin(card) is None or is_wait(card):
            return False
        if any(row.event_id == event["event_id"] for row in self.rows):
            return False
        self._clock += timedelta(seconds=1)
        self.rows.append(
            OutboxRow(
                len(self.rows) + 1, str(card["ref"]), str(event["event_id"]), request_id, target, self._clock
            )
        )
        return True

    def pending(self, **_: Any) -> list[OutboxRow]:
        self.pending_reads += 1
        return [row for row in self.rows if row.delivered_at is None]

    def mark(self, row_id: int, *, status: str, session: str, po_request_id: str) -> bool:
        assert status in STATUSES, status
        index = next(position for position, row in enumerate(self.rows) if row.id == row_id)
        row = self.rows[index]
        if self.crash_before is not None and self.crash_before(row):
            self.crash_before = None
            raise SimulatedCrash(f"crashed before marking {row.event_id}")
        if row.delivered_at is not None:
            return False
        self._clock += timedelta(seconds=1)
        self.rows[index] = replace(
            row,
            delivered_at=self._clock,
            status=status,
            session=session or None,
            po_request_id=po_request_id or None,
        )
        return True

    def rows_for(self, refs: Any) -> dict[str, list[OutboxRow]]:
        wanted = set(refs)
        found: dict[str, list[OutboxRow]] = {}
        for row in self.rows:
            if row.task_ref in wanted:
                found.setdefault(row.task_ref, []).append(row)
        return found

    def delivered(self, reference: str) -> dict[str, dict[str, Any]]:
        """The card's marked rows, by event id, as `task show` renders them."""
        return {
            row.event_id: row.to_json()
            for row in self.rows
            if row.task_ref == reference and row.delivered_at is not None
        }
