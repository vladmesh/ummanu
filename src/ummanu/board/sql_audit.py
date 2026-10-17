"""The card audit contract, kept in `requests` and `board_events`.

`docs/BOARD_STORE.md` §7.3 is the table this module implements, row by row.  The contract callers
see is unchanged and deliberately so: the same `request_id`, the same answer to a retry with the
same id, and the same refusal — *"request id belongs to another operation or payload"* — when an
id is reused for a different operation.  What changes is where the claim lives.

* **The namespace** is `requests.request_id`, the whole installation's one primary key (§3.9),
  in place of the index the pre-2026-09-10 file journal kept over its lines and pending files.
* **Staged and committed** are `requests.status`, in place of a pending file and a journal line.
  `requests.intent` holds the record itself, frozen at the claim, which is what makes the
  "same operation, same payload" comparison a column comparison rather than a file read.
* **`requests.protocol`** is what makes §3.9's one permitted replacement checkable: a generic
  stage may replace a generic staged record and may never replace a protocol one.
* **`board_events`** carries the typed occurrences — the ones whose `kind` is an `EventKind` —
  so the event stream is queryable as §3.9 describes.  A *generic* audit record (`moved`,
  `edited`, `commented`, `restored_comment`, …) has no `EventKind` and therefore no
  `board_events` row; it lives in its `requests` row alone.  That is not a loss of the record,
  it is the closed vocabulary of §3.12 refusing to be widened by an adapter.
* **The per-card marker lock** is a PostgreSQL advisory lock rather than a file lock, so it is
  released by the connection rather than by a process that might die holding a file.

What is **not** implemented, because §7.3 says it ceases to exist: a card effect and its claim
are statements of one transaction (§7.1), so `reconcile` has nothing to repair and answers `(0, 0)`.

What *can* outlive its writer is a claim staged on its own, outside a transaction: `stage` and
`claim` commit a `staged` row by themselves when no transaction is open, and a process that dies
before `append` or `discard` leaves it staged for good, blocking every checkpoint.
`settle_stale_staged` is the later tick's answer (secretary-1664): a row staged longer than
`STALE_STAGED_GRACE_SECONDS` is committed when its effect is proven present, and otherwise refused
terminally in place (`discarded`, with the reason on the row), never by applying the effect.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import threading
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime
from typing import Any

from ummanu.board import budget_candidates, origin_outbox
from ummanu.board.audit_contract import is_protocol_event, require_claim
from ummanu.board.models import EntityKind, Event, EventKind
from ummanu.board.sql_rows import json_document, now as _now, rfc3339

#: Every `kind` a `board_events` row may carry (§3.12).  A record whose kind is outside it is a
#: generic audit record and stays in its `requests` row.
_TYPED_KINDS = frozenset(kind.value for kind in EventKind)

#: The order `events()` has always answered in, and the key columns of its indexes.
_CLAIM_ORDER = "settled_at, created_at, request_id"
_CLAIM_ORDER_DESC = "settled_at DESC, created_at DESC, request_id DESC"


#: How long a staged row may stay staged before `settle_stale_staged` settles it. A live claim is
#: settled by its own writer in seconds; this is far past that, and short enough that the next
#: checkpoint after it (five minutes at most) still publishes inside the 30-minute RPO.
STALE_STAGED_GRACE_SECONDS = 15 * 60

#: The key a refused stale row carries its refusal under, inside its own `intent`.
REFUSAL_KEY = "stale_refusal"

#: Records whose effect is the record itself: their writer stages and appends with no board write
#: in between, so the frozen intent is all there is to prove. Typed occurrences by `EventKind`,
#: generic records by `kind`; any generic record marked `backend.revision == "not_written"`
#: (guard decisions, product-run events) says the same thing about itself.
_RECORD_ONLY_TYPED = frozenset({EventKind.ATTEMPT_USAGE.value, EventKind.ATTEMPT_OUTCOME.value})
_RECORD_ONLY_GENERIC = frozenset(
    {"sprint_guard_denied", "sprint_guard_override", "outcome_round_context", "routing"}
)

#: The effect tables whose rows claim the request that wrote them (`request_id` column). A row
#: there is the effect, present, and it is written in the same statement as the effect itself.
_CLAIMING_EFFECT_TABLES = (
    "sprint_budget_events",
    "sprint_comments",
    "sprint_decisions",
    "task_comments",
    "issue_comments",
    "product_comments",
)


def _kinds_answering(kind: str) -> set[str]:
    """Every stored `kind` a `kind=` narrowing matches: itself, and each whose action it is."""
    from ummanu.board.task_protocol.rows import _MARKER_EVENT_ACTIONS

    return {kind} | {stored for stored, action in _MARKER_EVENT_ACTIONS.items() if action == kind}


class SqlAuditError(RuntimeError):
    pass


def _advisory_key(text: str) -> int:
    digest = hashlib.sha256(text.encode("utf-8")).digest()[:8]
    return int.from_bytes(digest, "big", signed=True)


def _stamp(value: Any) -> str:
    if isinstance(value, datetime):
        return rfc3339(value)
    return str(value or "")


class SqlTaskAudit:
    """The audit owner of the PostgreSQL card backend.

    It speaks the `TaskError` vocabulary, because every caller above it
    catches those and nothing else; a `psycopg` error escaping from here would be an unhandled
    failure in a command that today has a named one.
    """

    def __init__(self, client: Any) -> None:
        self.client = client
        self._marker_lock_depth = threading.local()

    # --- primitives ------------------------------------------------------------------

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        return self.client._query(sql, params)

    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        return self.client._execute(sql, params)

    def _commit(self) -> None:
        self.client._commit_unless_nested()

    @staticmethod
    def _document(value: Any) -> dict[str, Any]:
        return json_document(value)

    def _record(self, request_id: str, status: str) -> dict[str, Any] | None:
        rows = self._query(
            "SELECT intent FROM requests WHERE request_id = %s AND status = %s",
            (request_id, status),
        )
        return self._document(rows[0][0]) if rows else None

    # --- the audit owner surface -------------------------------------------------------

    def committed_event(self, request_id: str) -> dict[str, Any] | None:
        return self._record(request_id, "committed")

    def pending_event(self, request_id: str) -> dict[str, Any] | None:
        return self._record(request_id, "staged")

    def event(self, request_id: str) -> dict[str, Any] | None:
        committed = self.committed_event(request_id)
        return committed if committed is not None else self.pending_event(request_id)

    def events(
        self,
        reference: str = "",
        *,
        kind: str = "",
        references: Iterable[str] | None = None,
        since: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Committed records in claim order, narrowed in SQL the way the file journal narrows them.

        `reference` and `references` (a set of refs) narrow by `requests.ref`, which is the record's
        own `ref` on every row `_claim_row` writes; `kind` matches the record's kind or its released
        action spelling (`_event_action`); `since` keeps what settled at or after that moment. Each
        filter is served by an index of `0012_request_read_indexes`, so what a read costs follows
        the slice it asks for and not the history beside it. On a store the revision has not reached
        yet the answer is the same and only the plan differs.

        An empty `references` answers no rows and still asks the store, with a statement that reads
        none: a caller whose document needs no events establishes that the audit answers at constant
        cost, and fails exactly where a read would have (secretary-1660).
        """
        clauses, params = self._committed_filter(reference, kind=kind, references=references, since=since)
        if clauses is None:
            self._query("SELECT 1 FROM requests WHERE false")
            return []
        rows = self._query(
            f"SELECT intent FROM requests WHERE {' AND '.join(clauses)} ORDER BY {_CLAIM_ORDER}",
            tuple(params),
        )
        return [self._document(intent) for (intent,) in rows]

    @staticmethod
    def _committed_filter(
        reference: str,
        *,
        kind: str,
        references: Iterable[str] | None,
        since: datetime | None,
    ) -> tuple[list[str] | None, list[Any]]:
        clauses = ["status = 'committed'"]
        params: list[Any] = []
        if reference:
            clauses.append("ref = %s")
            params.append(reference)
        if references is not None:
            wanted = sorted({str(item) for item in references if item})
            if not wanted:
                return None, []
            clauses.append("ref = ANY(%s)")
            params.append(wanted)
        if kind:
            clauses.append("intent->>'kind' = ANY(%s)")
            params.append(sorted(_kinds_answering(kind)))
        if since is not None:
            clauses.append("settled_at >= %s")
            params.append(since)
        return clauses, params

    def events_page(self, *, end: int | None, limit: int) -> tuple[int, list[dict[str, Any]]]:
        """How many records are committed, and the ordinals `[end - limit, end)` of `events()`.

        One statement, so the count and the page come from one snapshot: counted and read in two,
        a command committing in between shifts the ordinals and a traversal skips or repeats a
        row. The count is an index-only scan, and the page is read newest first from the committed
        claim-order index, so the rows fetched are the page and the pages above it rather than the
        whole history. `end` past the count answers no rows; the caller refuses it.
        """
        stop = "COALESCE(%s::bigint, total.n)"
        rows = self._query(
            "WITH total AS (SELECT count(*) AS n FROM requests WHERE status = 'committed') "
            "SELECT total.n, page.intent FROM total LEFT JOIN LATERAL ("
            "SELECT intent, settled_at, created_at, request_id FROM requests "
            f"WHERE status = 'committed' AND {stop} <= total.n "
            f"ORDER BY {_CLAIM_ORDER_DESC} "
            f"LIMIT GREATEST(LEAST(%s::bigint, {stop}), 0) OFFSET GREATEST(total.n - {stop}, 0)"
            ") AS page ON true "
            "ORDER BY page.settled_at, page.created_at, page.request_id",
            (end, limit, end, end),
        )
        total = int(rows[0][0])
        return total, [self._document(intent) for _total, intent in rows if intent is not None]

    def uncharged_budget_candidates(self, *, limit: int) -> list[dict[str, Any]]:
        """The oldest `limit` committed budget candidates with no committed charge, in claim order.

        `budget_candidates` defines both halves: the candidate predicate, which the partial index
        `requests_budget_candidates` of `0013_budget_candidates` holds exactly, and the charge id,
        which the anti-join probes through the primary key. No position is kept, so a record that
        commits late or stays unresolved for any number of passes is answered until it is charged.
        Without the index the answer is the same and the plan reads the committed claim order.
        """
        predicate = budget_candidates.CANDIDATE_PREDICATE.replace("%", "%%")
        identity = budget_candidates.IDENTITY
        rows = self._query(
            f"SELECT intent FROM requests WHERE status = 'committed' AND {predicate} "
            f"AND {identity} IS NOT NULL AND NOT EXISTS (SELECT 1 FROM requests AS charge "
            f"WHERE charge.request_id = %s || {identity.replace('intent', 'requests.intent')} "
            "AND charge.status = 'committed') "
            f"ORDER BY {_CLAIM_ORDER} LIMIT %s",
            (budget_candidates.CHARGE_PREFIX, max(limit, 0)),
        )
        return [self._document(intent) for (intent,) in rows]

    def pending_events(self) -> list[dict[str, Any]]:
        return [
            self._document(intent)
            for (intent,) in self._query(
                "SELECT intent FROM requests WHERE status = 'staged' ORDER BY created_at, request_id"
            )
        ]

    def status(self) -> dict[str, int | bool]:
        pending = int(self._query("SELECT count(*) FROM requests WHERE status = 'staged'")[0][0])
        return {"ok": pending == 0, "pending": pending}

    def event_id_owner(self, event_id: str) -> str | None:
        rows = self._query(
            "SELECT request_id FROM requests WHERE intent->>'event_id' = %s "
            "ORDER BY created_at, request_id LIMIT 1",
            (event_id,),
        )
        return rows[0][0] if rows else None

    @staticmethod
    def require_claim(
        existing: dict[str, Any],
        *,
        kind: str,
        reference: str | None,
        identity: dict[str, Any] | None,
    ) -> None:
        require_claim(existing, kind=kind, reference=reference, identity=identity)

    @staticmethod
    def _require_same_event(existing: dict[str, Any], event: dict[str, Any]) -> None:
        from ummanu.kernel.errors import TaskError

        if existing != event:
            raise TaskError("validation", "request id belongs to another operation or payload", 2)

    # --- ownership -------------------------------------------------------------------

    def _pending_owner(
        self, request_id: str, event: dict[str, Any] | None, *, operation: str
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, bool]:
        """Which record owns `request_id`, over the two statuses of one `requests` row."""
        committed = self.committed_event(request_id)
        if committed is not None:
            if event is not None:
                self._require_same_event(committed, event)
            return committed, None, False
        pending = self.pending_event(request_id)
        if pending is None:
            if self._record(request_id, "discarded") is not None:
                if operation == "discard":
                    return None, None, False
                from ummanu.kernel.errors import TaskError

                refusal = self.refusal(request_id) or {}
                reason = refusal.get("reason") or "no reason recorded"
                raise TaskError(
                    "validation",
                    f"request id was refused when its stale staged record was settled: {reason}",
                    2,
                )
            return None, None, False
        if operation == "reconcile" and is_protocol_event(pending):
            self._require_same_event(pending, {})
        if event is not None and pending == event:
            return None, pending, False
        if (
            operation == "stage"
            and event is not None
            and not is_protocol_event(pending)
            and not is_protocol_event(event)
        ):
            return None, pending, True
        if operation == "discard" and event is None and not is_protocol_event(pending):
            return None, pending, False
        self._require_same_event(pending, event or {})
        raise AssertionError("unreachable")

    def _claim_row(self, request_id: str, event: dict[str, Any], *, status: str) -> None:
        """§7.1 step 1: claim the id in the one global namespace, then own it.

        `ref` is written on the replacement as well as on the insert, and that is the whole of
        the repair this row needed.  A create claims its request id before it knows the reference
        it will allocate — the reference is chosen from the board's high-water mark inside the
        same mutation — so the first statement writes `ref = NULL` and the second, which names it,
        used to update `intent` and leave the column behind.  `requests.ref` then stayed NULL for
        the life of every created card, and the column §3.9 makes the record's own subject index
        answered nothing.  `TaskWriter._create` now runs both statements in one transaction, so
        the column and the record it belongs to become visible together or not at all.
        """
        ref = str(event.get("ref") or "")
        subject = event.get("subject") if isinstance(event.get("subject"), dict) else {}
        entity_kind = str(subject.get("kind") or "card")
        self._execute(
            "INSERT INTO requests (request_id, operation, intent, status, protocol, entity_kind, "
            "ref, created_at, settled_at) VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (request_id) DO UPDATE SET intent = EXCLUDED.intent, "
            # An accepted replacement of a staged record by a different staged record starts that
            # record's age anew: `settle_stale_staged` judges the current record, not the id's first
            # claim. A commit over its own staged row keeps the claim time, as it always has.
            "created_at = CASE WHEN requests.status = 'staged' AND EXCLUDED.status = 'staged' "
            "AND requests.intent IS DISTINCT FROM EXCLUDED.intent "
            "THEN EXCLUDED.created_at ELSE requests.created_at END, "
            "status = EXCLUDED.status, settled_at = EXCLUDED.settled_at, "
            "protocol = EXCLUDED.protocol, operation = EXCLUDED.operation, ref = EXCLUDED.ref",
            (
                request_id,
                str(event.get("kind") or ""),
                json.dumps(event, sort_keys=True),
                status,
                is_protocol_event(event),
                entity_kind,
                ref or None,
                _now(),
                None if status == "staged" else _now(),
            ),
        )

    def stage(self, request_id: str, event: dict[str, Any]) -> None:
        with self._locked():
            committed, pending, replace = self._pending_owner(request_id, event, operation="stage")
            if committed is not None:
                return
            if pending is not None and not replace:
                return
            self._claim_row(request_id, event, status="staged")
            self._commit()

    def claim(
        self,
        request_id: str,
        event: dict[str, Any],
        *,
        verify: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any] | None:
        with self._locked():
            committed, pending, _replace = self._pending_owner(request_id, event, operation="claim")
            if committed is not None:
                return committed
            if pending is not None:
                return pending
            if verify is not None:
                verify(event)
            self._claim_row(request_id, event, status="staged")
            self._commit()
            return None

    def append(self, request_id: str, event: dict[str, Any], *, restoring: bool = False) -> str:
        """Commit a record and its origin obligation in the same transaction.

        Restored history owes no new return; recovery imports the saved outbox separately.
        """
        with self._locked():
            committed, _pending, _replace = self._pending_owner(request_id, event, operation="append")
            if committed is None:
                self._claim_row(request_id, event, status="committed")
                if not restoring:
                    origin_outbox.enqueue(self, request_id, event)
                self._write_board_event(request_id, event)
                self._commit()
            return str(event["event_id"])

    def discard(self, request_id: str, event: dict[str, Any] | None = None) -> None:
        with self._locked():
            committed, pending, _replace = self._pending_owner(request_id, event, operation="discard")
            if committed is not None or pending is None:
                return
            self._execute(
                "DELETE FROM requests WHERE request_id = %s AND status = 'staged'", (request_id,)
            )
            self._commit()

    def reconcile(self) -> tuple[int, int]:
        """Nothing to repair: §7.1 makes the effect and the record one transaction (§7.3)."""
        return 0, 0

    # --- stale staged rows -----------------------------------------------------------

    def oldest_pending(self) -> dict[str, Any] | None:
        """The oldest staged row, for a gate that has to say what it is blocked on and since when."""
        rows = self._query(
            "SELECT request_id, operation, created_at FROM requests WHERE status = 'staged' "
            "ORDER BY created_at, request_id LIMIT 1"
        )
        if not rows:
            return None
        request_id, kind, created_at = rows[0]
        return {"request_id": request_id, "kind": kind, "staged_at": _stamp(created_at)}

    def settle_stale_staged(
        self, *, grace_seconds: float = STALE_STAGED_GRACE_SECONDS, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        """Commit or terminally refuse every row staged longer than `grace_seconds`.

        Each row is settled in its own transaction under the request-namespace lock, re-read there,
        so a writer finishing it concurrently wins and a row younger than the grace is never read
        for settling. Nothing here applies an effect: a row is committed only when its effect is
        proven present (`_stale_verdict`), and every other row -- effect absent, or not provable
        from the store -- is refused. A refusal moves the stale row itself to `discarded` with its
        reason on that row (`_refuse`), which makes its request id terminal (`_pending_owner`).
        Nothing is ever allocated in the request-id namespace and no other `requests` row is
        written. Returns one outcome per settled row.
        """
        cutoff = (now or _now()).timestamp() - float(grace_seconds)
        candidates = [
            request_id
            for (request_id,) in self._query(
                "SELECT request_id FROM requests WHERE status = 'staged' "
                "AND created_at < to_timestamp(%s) ORDER BY created_at, request_id",
                (cutoff,),
            )
        ]
        outcomes: list[dict[str, Any]] = []
        for request_id in candidates:
            with self.client.transaction(), self._locked():
                rows = self._query(
                    "SELECT intent, created_at, ref FROM requests "
                    "WHERE request_id = %s AND status = 'staged' AND created_at < to_timestamp(%s) "
                    "FOR UPDATE",
                    (request_id, cutoff),
                )
                if not rows:
                    continue
                intent, created_at, ref = rows[0]
                record = self._document(intent)
                present, reason = self._stale_verdict(request_id, record)
                outcome = {
                    "request_id": request_id,
                    "kind": str(record.get("kind") or ""),
                    "ref": ref or "",
                    "staged_at": _stamp(created_at),
                    "outcome": "committed" if present else "refused",
                    "reason": reason,
                }
                if present:
                    # Only this staged row, which the lock above holds: never an upsert over an id.
                    self._execute(
                        "UPDATE requests SET status = 'committed', settled_at = %s "
                        "WHERE request_id = %s AND status = 'staged' AND created_at < to_timestamp(%s)",
                        (_now(), request_id, cutoff),
                    )
                    self._write_board_event(request_id, record)
                else:
                    self._refuse(request_id, cutoff, outcome)
            outcomes.append(outcome)
        return outcomes

    def _stale_verdict(self, request_id: str, record: dict[str, Any]) -> tuple[bool, str]:
        """Whether a stale staged record's effect is present, and the sentence that says why."""
        kind = str(record.get("kind") or "")
        backend = record.get("backend") if isinstance(record.get("backend"), dict) else {}
        if is_protocol_event(record):
            if kind in _RECORD_ONLY_TYPED:
                return True, f"{kind} is a record-only occurrence: the staged record is its whole effect"
        elif kind in _RECORD_ONLY_GENERIC or backend.get("revision") == "not_written":
            return True, f"{kind} is a record-only audit record: the staged record is its whole effect"
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        claimed = [
            table
            for table in _CLAIMING_EFFECT_TABLES
            if self._query(f"SELECT 1 FROM {table} WHERE request_id = %s LIMIT 1", (request_id,))
        ]
        compound = kind == "budget_recorded" and bool(payload.get("hard_limit_stop"))
        if claimed and not compound:
            return True, f"its effect is present: {', '.join(claimed)} holds a row claiming {request_id}"
        if kind == "budget_recorded" and not compound:
            return False, (
                "its effect is absent: a budget charge writes exactly one sprint_budget_events row "
                f"claiming its request, and none claims {request_id}"
            )
        if compound:
            return False, (
                "its effect cannot be verified: a hard-limit budget charge also stops the sprint, "
                "and the store holds no row proving that half; refused rather than guessed"
            )
        return False, (
            f"its effect cannot be verified: no row in the store claims {request_id} and a "
            f"{kind or 'record'} effect is not provable from its record; refused rather than guessed"
        )

    def _refuse(self, request_id: str, cutoff: float, outcome: dict[str, Any]) -> None:
        """Move the stale row itself to its terminal state, with the reason on that row.

        One statement, on the one row this settlement owns: guarded by `status = 'staged'` and the
        grace, so a second pass or a concurrent settler matches nothing and writes nothing. No
        request id is allocated and no other row is touched; the row's own id and its `discarded`
        status are how the refusal is found (`refusal`, `refusals`).
        """
        settled = _now()
        refusal = {
            "reason": outcome["reason"],
            "staged_at": outcome["staged_at"],
            "settled_at": rfc3339(settled),
            "settled_by": "stale-audit-settlement",
        }
        self._execute(
            "UPDATE requests SET status = 'discarded', settled_at = %s, "
            "intent = intent || jsonb_build_object(%s::text, %s::jsonb) "
            "WHERE request_id = %s AND status = 'staged' AND created_at < to_timestamp(%s)",
            (settled, REFUSAL_KEY, json.dumps(refusal, sort_keys=True), request_id, cutoff),
        )

    def refusal(self, request_id: str) -> dict[str, Any] | None:
        """The refusal settlement recorded on the stale row `request_id`, if it refused it."""
        record = self._record(request_id, "discarded")
        if record is None or not isinstance(record.get(REFUSAL_KEY), dict):
            return None
        return {**record[REFUSAL_KEY], "request_id": request_id, "kind": str(record.get("kind") or "")}

    def refusals(self) -> list[dict[str, Any]]:
        """Every stale row settlement refused, oldest settlement first."""
        rows = self._query(
            "SELECT request_id FROM requests WHERE status = 'discarded' ORDER BY settled_at, request_id"
        )
        return [refusal for (request_id,) in rows if (refusal := self.refusal(request_id)) is not None]

    def _write_board_event(self, request_id: str, event: dict[str, Any]) -> None:
        """Mirror a typed occurrence into `board_events`; a generic record has no row there."""
        kind = str(event.get("kind") or "")
        if kind not in _TYPED_KINDS or not is_protocol_event(event):
            return
        try:
            typed = Event.from_record(event)
        except (TypeError, ValueError):
            return
        if self._query("SELECT 1 FROM board_events WHERE event_id = %s", (typed.event_id,)):
            return
        self._execute(
            "INSERT INTO board_events (event_id, request_id, kind, entity_kind, ref, actor_role, "
            "actor_id, head_run_ref, reason, source_state, target_state, related_refs, data, "
            "occurred_at, committed, committed_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, true, %s)",
            (
                typed.event_id,
                request_id,
                typed.kind.value,
                typed.entity_kind.value,
                typed.ref,
                typed.actor.role,
                typed.actor.id,
                typed.actor.head_run_ref,
                typed.reason,
                typed.source_state,
                typed.target_state,
                list(typed.related_refs.refs),
                json.dumps(typed.data, sort_keys=True),
                typed.occurred_at,
                _now(),
            ),
        )

    # --- locks -----------------------------------------------------------------------

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        """The successor of `.audit.lock`: one advisory lock over the request namespace.

        Inside a mutation transaction the whole claim is already serialized by the row locks the
        statements take, so this is only what keeps two *separate* claims from interleaving their
        read-then-write of one id.
        """
        key = _advisory_key("ummanu.board.requests")
        if self.client._depth:
            self._execute("SELECT pg_advisory_xact_lock(%s)", (key,))
            yield
            return
        # A session-level lock belongs to one server session: the lock, the claim and the
        # unlock stay on this thread's one pooled connection.
        with self.client._session():
            self._execute("SELECT pg_advisory_lock(%s)", (key,))
            try:
                yield
            finally:
                self._execute("SELECT pg_advisory_unlock(%s)", (key,))
                self._commit()

    @contextlib.contextmanager
    def marker_comment_lock(self, reference: str) -> Iterator[None]:
        """`marker_comment_lock`, as an advisory lock scoped to one card's marker identity."""
        held = getattr(self._marker_lock_depth, "held", None)
        if held is None:
            held = self._marker_lock_depth.held = {}
        depth = held.get(reference, 0)
        if depth:
            held[reference] = depth + 1
            try:
                yield
            finally:
                held[reference] -= 1
            return
        key = _advisory_key(f"ummanu.board.marker:{reference}")
        in_transaction = bool(self.client._depth)
        # As in `_locked`: the session-level lock and its unlock share one pooled connection.
        with self.client._session():
            if in_transaction:
                self._execute("SELECT pg_advisory_xact_lock(%s)", (key,))
            else:
                self._execute("SELECT pg_advisory_lock(%s)", (key,))
                self._commit()
            held[reference] = 1
            try:
                yield
            finally:
                del held[reference]
                if not in_transaction:
                    self._execute("SELECT pg_advisory_unlock(%s)", (key,))
                    self._commit()

    # --- marker reservations ---------------------------------------------------------

    def pending_marker_owner(
        self, reference: str, content: str, *, request_id: str | None = None
    ) -> str | None:
        owners = self.pending_marker_owners([(reference, content, request_id or "")])
        return owners.get(request_id or "")

    def pending_marker_owners(self, candidates: Iterable[tuple[str, str, str]]) -> dict[str, str]:
        from ummanu.board.events import render_marker_comment

        wanted = {(reference, content): request_id for reference, content, request_id in candidates}
        owners: dict[str, str] = {}
        for record in self.pending_events():
            candidate = str(record.get("request_id") or "")
            identity: tuple[str, str] | None = None
            if is_protocol_event(record):
                # A staged Card event that is not a marker (a usage record whose publication was
                # refused, say) renders no marker and so reserves none; it must not refuse the
                # marker write that is asking.
                try:
                    event = Event.from_record(record)
                    if event.entity_kind is EntityKind.CARD:
                        identity = (event.ref, render_marker_comment(event))
                except (TypeError, ValueError):
                    continue
            else:
                payload = record.get("payload")
                if (
                    record.get("kind") == "restored_comment"
                    and isinstance(record.get("ref"), str)
                    and isinstance(payload, dict)
                    and isinstance(payload.get("restore_body"), str)
                ):
                    identity = (record["ref"], payload["restore_body"])
            request_id = wanted.get(identity) if identity is not None else None
            if request_id is not None and candidate != request_id:
                owners[request_id] = candidate
        return owners

    def _occurrence_projection_records(
        self, kinds: Iterable[str] | None = None, *, outcome_owed: bool = False
    ) -> list[tuple[dict[str, Any], bool]]:
        """The fail-closed usage projection's input: committed first, then staged.

        With `kinds`, the slice a projection over those kinds can be decided from, in one statement
        and so one snapshot: every record of those kinds, every record sharing an `event_id` with
        one of them (the only cross-record conflict a projection checks that SQL does not already
        rule out, since `request_id` is the primary key), and with `outcome_owed` every record
        whose data carries an `attempt_outcome_owed` obligation. Without `kinds`, everything.
        """
        if kinds is None:
            return [(record, False) for record in self.events()] + [
                (record, True) for record in self.pending_events()
            ]
        wanted = sorted({str(kind) for kind in kinds})
        columns = "request_id, status, intent, settled_at, created_at"
        owed = (
            f" UNION ALL SELECT {columns} FROM requests WHERE status IN ('committed', 'staged') "
            "AND (intent->'data') ? 'attempt_outcome_owed'"
            if outcome_owed
            else ""
        )
        # Arms the planner can each serve from one index, rather than a semi-join on `request_id`
        # that it prefers to answer with a hash over the whole table: the kind by `requests_by_kind`,
        # the shared event ids as one array by `requests_by_event_id`, the obligations by
        # `requests_owing_outcome`. A record two arms both find is kept once.
        rows = self._query(
            f"WITH own AS (SELECT {columns} FROM requests WHERE status IN ('committed', 'staged') "
            "AND intent->>'kind' = ANY(%s)) "
            "SELECT status, intent FROM ("
            f"SELECT DISTINCT ON (request_id) {columns} FROM ("
            f"SELECT {columns} FROM own"
            f" UNION ALL SELECT {columns} FROM requests WHERE status IN ('committed', 'staged') "
            "AND intent->>'event_id' = ANY(ARRAY(SELECT intent->>'event_id' FROM own))"
            + owed
            + ") AS found ORDER BY request_id) AS slice "
            f"ORDER BY status = 'staged', {_CLAIM_ORDER}",
            (wanted,),
        )
        return [(self._document(intent), status == "staged") for status, intent in rows]


__all__ = ["SqlAuditError", "SqlTaskAudit"]
