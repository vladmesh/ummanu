"""The origin-return outbox: what a delegated card owes its PO session, written when it becomes owed (secretary-1792).

One table, `origin_returns` (revision `0022_origin_returns`), one row per transition into Done or
Blocked of a card that carries a PO origin (`board/po_origin.py`) and is not a wait card: the card
ref, the transition's event id (unique) and request id, the column it entered, when, and the delivery
outcome (`delivered_at`, `status` delivered|skipped, the session that took it, the input's request
id). `notice` is read only; a delivery leaves it as it is.

**Written in the transition's own transaction.** :func:`enqueue` is called by
`SqlTaskAudit.append`, the one place a committed audit record is written (`requests`), in the same
statement group and the same transaction as the record: a card transition's typed event and the
column move it records commit together (`TaskWriter._transition_card`), so the move, its audit event
and its outbox row stand or fall together. Every writer of a card transition reaches it, because
every committed record does: `TaskWriter.move` (every role, the dispatcher's edges and the wait
edges), `task complete`, the decide/release path, a legacy `moved` replay. Archive, reopen and any
later move write no row and touch none: a row is never deleted or rewritten except by its delivery.

**Read by the dispatcher only** (`dispatch/origin_returns.py`): :meth:`OriginOutboxStore.pending`
is one indexed query per tick over the undelivered rows, in id order; :meth:`OriginOutboxStore.mark`
records a delivery once. `task show` reads a card's rows for its `origin` block.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from ummanu.board.audit_contract import card_transition_of
from ummanu.board.extension_bag import EXTENSION_BAG
from ummanu.board.sql_rows import rfc3339

TABLE = "origin_returns"
#: The columns a row is owed for.
TERMINAL_STATES = ("done", "blocked")
#: A row's delivery status: the origin session took the input, or it already had the result.
DELIVERED = "delivered"
SKIPPED = "skipped"
STATUSES = (DELIVERED, SKIPPED)
#: The bag key of the origin, spelled here as the store reads it (`po_origin.PO_ORIGIN`).
_ORIGIN_KEY = "po_origin"
#: The most rows one tick reads.
PENDING_LIMIT = 500

_COLUMNS = (
    "id, task_ref, event_id, request_id, target_state, created_at, delivered_at, status, notice, "
    "session, po_request_id"
)


@dataclass(frozen=True)
class OutboxRow:
    id: int
    task_ref: str
    event_id: str
    request_id: str
    target_state: str
    created_at: datetime
    delivered_at: datetime | None = None
    status: str | None = None
    notice: str | None = None
    session: str | None = None
    po_request_id: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "state": self.target_state,
            "created_at": _iso(self.created_at),
            "delivered_at": _iso(self.delivered_at),
            "status": self.status,
            "notice": self.notice,
            "session": self.session,
            "request_id": self.po_request_id,
        }


def _iso(value: datetime | None) -> str | None:
    return rfc3339(value, microseconds=True) if isinstance(value, datetime) else (str(value) if value else None)


def owed_target(event: dict[str, Any]) -> str:
    """The column a committed record's transition entered when that may owe a return, else `""`."""
    moved = card_transition_of(event)
    if moved is None or moved[1] not in TERMINAL_STATES or not str(event.get("event_id") or ""):
        return ""
    return moved[1]


def enqueue(audit: Any, request_id: str, event: dict[str, Any]) -> bool:
    """Write the outbox row a committed record owes, in the caller's transaction; True when written.

    `audit` is the `SqlTaskAudit` committing the record (its `_query`/`_execute` run on the connection
    and transaction of that commit). Nothing is owed by a record that is no transition into Done or
    Blocked, or whose card carries no origin or is a wait card. The row is keyed by the event id, so a
    replayed commit writes nothing. A store without the table refuses it, and with it the transition.
    """
    target = owed_target(event)
    ref = str(event.get("ref") or "")
    if not target or not ref:
        return False
    owes = audit._query(
        "SELECT 1 FROM tasks WHERE task_ref = %s AND task_type IS DISTINCT FROM 'wait' "
        f"AND coalesce(extensions -> '{EXTENSION_BAG}' ->> '{_ORIGIN_KEY}', '') <> '' LIMIT 1",
        (ref,),
    )
    if not owes:
        return False
    return bool(
        audit._execute(
            f"INSERT INTO {TABLE} (task_ref, event_id, request_id, target_state, created_at) "
            "VALUES (%s, %s, %s, %s, now()) ON CONFLICT (event_id) DO NOTHING",
            (ref, str(event["event_id"]), request_id, target),
        )
    )


class OriginOutboxStore:
    """`origin_returns` over a card client (`SqlCardClient`): its connection and transactions."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def available(self) -> bool:
        """Whether the store has the table (revision 0022); asked without an error that would abort a transaction."""
        rows = self.client._query(f"SELECT to_regclass('public.{TABLE}') IS NOT NULL")
        return bool(rows and rows[0][0])

    def export(self) -> list[dict[str, Any]]:
        """Every obligation in delivery order, including completed deliveries, on the caller's snapshot."""
        rows = self.client._query(f"SELECT {_COLUMNS} FROM {TABLE} ORDER BY id")
        result = []
        for values in rows:
            row = asdict(OutboxRow(*values))
            row["created_at"] = _iso(row["created_at"])
            row["delivered_at"] = _iso(row["delivered_at"])
            result.append(row)
        return result

    def restore(self, rows: list[dict[str, Any]]) -> None:
        """Merge by event identity and prove complete parity inside the caller's transaction."""
        from ummanu.board.origin_checkpoint import OriginCheckpointError, require_outbox_parity

        if not self.client._depth:
            raise OriginCheckpointError("origin return restore requires a transaction")
        self.client._execute(f"LOCK TABLE {TABLE} IN SHARE ROW EXCLUSIVE MODE")
        existing = {row["event_id"]: row for row in self.export()}
        fields = tuple(OutboxRow.__dataclass_fields__)[1:]
        for row in rows:
            prior = existing.get(row["event_id"])
            if prior is not None:
                require_outbox_parity([row], [prior])
                continue
            self.client._execute(
                f"INSERT INTO {TABLE} ({', '.join(fields)}) VALUES ({', '.join('%s' for _ in fields)})",
                tuple(row[field] for field in fields),
            )
        require_outbox_parity(rows, self.export())

    def pending(self, *, limit: int = PENDING_LIMIT) -> list[OutboxRow]:
        """The undelivered rows, oldest first: one query over the partial index of undelivered ids.

        A store before 0022 owes nothing: no card could carry an origin before it.
        """
        if not self.available():
            return []
        rows = self.client._query(
            f"SELECT {_COLUMNS} FROM {TABLE} WHERE delivered_at IS NULL ORDER BY id LIMIT %s", (limit,)
        )
        return [OutboxRow(*row) for row in rows]

    def mark(self, row_id: int, *, status: str, session: str, po_request_id: str) -> bool:
        """Record one row's delivery, once: a row already delivered is left as it is. True when written."""
        if status not in STATUSES:
            raise ValueError(f"an origin return is {' or '.join(STATUSES)}, not {status!r}")
        changed = self.client._execute(
            f"UPDATE {TABLE} SET delivered_at = now(), status = %s, session = %s, "
            "po_request_id = %s WHERE id = %s AND delivered_at IS NULL",
            (status, session or None, po_request_id or None, row_id),
        )
        self.client._commit_unless_nested()
        return bool(changed)

    def rows_for(self, refs: Iterable[str]) -> dict[str, list[OutboxRow]]:
        """Every row of each card, oldest first, in one query."""
        wanted = sorted({str(ref) for ref in refs if ref})
        if not wanted or not self.available():
            return {}
        rows = self.client._query(
            f"SELECT {_COLUMNS} FROM {TABLE} WHERE task_ref = ANY(%s) ORDER BY id", (wanted,)
        )
        found: dict[str, list[OutboxRow]] = {}
        for row in rows:
            record = OutboxRow(*row)
            found.setdefault(record.task_ref, []).append(record)
        return found


def outbox_for(client: Any) -> Any:
    """The outbox a card client reads and marks: its own (a test's fake names one), or its store."""
    own = getattr(client, "origin_outbox", None)
    if own is not None:
        return own
    if hasattr(client, "_query") and hasattr(client, "_execute"):
        return OriginOutboxStore(client)
    return None


__all__ = [
    "DELIVERED",
    "PENDING_LIMIT",
    "SKIPPED",
    "STATUSES",
    "TABLE",
    "TERMINAL_STATES",
    "OriginOutboxStore",
    "OutboxRow",
    "enqueue",
    "outbox_for",
    "owed_target",
]
