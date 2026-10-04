"""Owner events: what needs the owner, and what the owner should know, on the board (secretary-1770).

One table, `owner_events` (revision `0018_owner_events`), and this module is the only code that
touches it. Producers write through :func:`record`; the web reads and marks through
:class:`OwnerEventStore`; the stay-unread rule runs through :func:`settle`.

**Kinds and classes.** Every kind belongs to one class, and the class is derived from the kind here
(:data:`KIND_CLASS`), never passed by a producer. `needs_owner` is a fact only the owner can move on:
a current unanswered PO handover, an unresolved timed/failed PO episode, or a steward's report with
an explicit non-empty human escalation reason. Routine Blocked, PO and e2e events are notices. `notice` is a fact
the owner should know: a sprint closed or stopped, the budget signal, a dead head nobody relaunched, a
failed PO turn, a red provider, a delegated card's result returned to its PO session. The database holds both vocabularies and the kind-to-class rule as
CHECK constraints (`board/schema.py`), from the same lists.

**Advisory notices never fail their caller.** :func:`record` is idempotent under its dedup key (a unique
column: a repeat inserts nothing) and swallows every failure after logging it: a store that does not
answer, a board that owes migrations (merged code runs before the upgrade applies them; the schema
gate refuses it as `OwnerEventsSchemaOwed`), a store that is not configured at all. A producer's own
notice never depends on the bell. :func:`record_strict`, for a caller whose work
is complete only with its event (a delegated card's returned result, secretary-1792): the same write,
answered as written, already present or failed instead of swallowed, so that caller repeats it.
Required card waits use :func:`record_required_wait` and :func:`settle_required_wait` instead:
failures escape the owner-event savepoint and roll back the enclosing card mutation and occurrence.

**Stay-unread.** A `needs_owner` event whose subject card carries the `waiting_owner` mark
(`board.owner_handover`) is never marked read by a click or by "mark all read": :meth:`mark_read`
refuses it and :meth:`mark_all_read` takes notices only. Its `read_at` is set by :func:`settle_required_wait` when
the owner answer is recorded or the unresolved episode is replaced/ended, inside the card's
transaction. A current unresolved escalation is held by the same rule.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ummanu.board.e2e_record import e2e_state
from ummanu.board.extension_bag import EXTENSION_BAG
from ummanu.board.owner_handover import MARK_KEYS, OWNER_ESCALATION, attention_record, waiting_owner
from ummanu.board.schema_gate import SchemaAssessment, SchemaOwed, require

TABLE = "owner_events"

NEEDS_OWNER = "needs_owner"
NOTICE = "notice"
#: The CHECK `owner_event_class_in_vocabulary`, in this order.
CLASSES = (NEEDS_OWNER, NOTICE)

CARD_HANDED_TO_OWNER = "card_handed_to_owner"
STEWARD_NEEDS_HUMAN = "steward_needs_human"
SPRINT_CLOSED = "sprint_closed"
SPRINT_STOPPED = "sprint_stopped"
BUDGET_SIGNAL = "budget_signal"
OBSERVER_DEAD = "observer_dead"
HEAD_DEAD = "head_dead"
PO_TURN_FAILED = "po_turn_failed"
PROVIDER_RED = "provider_red"
DELEGATED_CARD_SETTLED = "delegated_card_settled"
E2E_BUDGET_SPENT = "e2e_budget_spent"
E2E_AFTER_MERGE = "e2e_after_merge"
CARD_WAITS_FOR_PERSON = "card_waits_for_person"
PO_CARD_ESCALATED = "po_card_escalated"

#: Every kind and the class it belongs to: the CHECKs `owner_event_kind_in_vocabulary` and
#: `owner_event_class_follows_kind` (board/schema.py, 0018, restated by 0021, 0023 and 0024) are these two lists. A
#: new kind joins it here and in a migration together.
KIND_CLASS: dict[str, str] = {
    CARD_WAITS_FOR_PERSON: NOTICE,
    CARD_HANDED_TO_OWNER: NEEDS_OWNER,
    PO_CARD_ESCALATED: NEEDS_OWNER,
    STEWARD_NEEDS_HUMAN: NEEDS_OWNER,
    # Released direct e2e notifications remain visible without claiming owner authority.
    E2E_BUDGET_SPENT: NOTICE,
    # Complete PO routing for uncovered e2e work is a separate cut.
    E2E_AFTER_MERGE: NOTICE,
    SPRINT_CLOSED: NOTICE,
    SPRINT_STOPPED: NOTICE,
    BUDGET_SIGNAL: NOTICE,
    OBSERVER_DEAD: NOTICE,
    HEAD_DEAD: NOTICE,
    PO_TURN_FAILED: NOTICE,
    PROVIDER_RED: NOTICE,
    # A card a PO session delegated settled and its result went back to that session (0021).
    DELEGATED_CARD_SETTLED: NOTICE,
}
KINDS = tuple(KIND_CLASS)
NEEDS_OWNER_KINDS = tuple(kind for kind, value in KIND_CLASS.items() if value == NEEDS_OWNER)

#: The subject of a PO turn that no card input started: its session, `po-session:<session id>`.
PO_SESSION_PREFIX = "po-session:"
#: The longest text an event keeps; a producer's reason is cut, never refused.
TEXT_LIMIT = 2000
#: The most events one list read returns.
LIST_LIMIT = 500

_COLUMNS = 'id, kind, "class", subject_ref, text, created_at, read_at, dedup_key'

logger = logging.getLogger(__name__)


class OwnerEventError(RuntimeError):
    """An owner event could not be read or written."""


class OwnerEventsUnavailable(OwnerEventError):
    """The board store did not answer, or owes the migrations this build reads it through."""


class OwnerEventsSchemaOwed(SchemaOwed, OwnerEventsUnavailable):
    """The board store owes migrations this build reads owner events through (`board.schema_gate`)."""

    def __init__(self, assessment: SchemaAssessment) -> None:
        self.assessment = assessment
        OwnerEventsUnavailable.__init__(self, assessment.describe())


class OwnerEventNotFound(OwnerEventError):
    pass


class ReadRefused(OwnerEventError):
    """A `needs_owner` event whose card still waits for the owner is not marked read by hand."""


@dataclass(frozen=True)
class OwnerEvent:
    id: int
    kind: str
    event_class: str
    subject_ref: str | None
    text: str
    created_at: datetime
    read_at: datetime | None
    dedup_key: str
    #: Whether the subject card carries the `waiting_owner` mark now (filled by the list read).
    held: bool = False

    @property
    def unread(self) -> bool:
        return self.read_at is None

    @property
    def pinned(self) -> bool:
        """An open `needs_owner` event: listed above the notices, whatever its date."""
        return self.event_class == NEEDS_OWNER and self.read_at is None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "class": self.event_class,
            "subject_ref": self.subject_ref,
            "text": self.text,
            "created_at": _iso(self.created_at),
            "read_at": _iso(self.read_at),
            "dedup_key": self.dedup_key,
            "unread": self.unread,
            "pinned": self.pinned,
            "held": self.held,
            "held_reason": _held_fact(self.kind, self.text) if self.held else None,
        }


def class_of(kind: str) -> str:
    """The class a kind belongs to; an unknown kind is a programming error."""
    try:
        return KIND_CLASS[kind]
    except KeyError:
        raise ValueError(f"{kind!r} is not an owner event kind; the kinds are {', '.join(KINDS)}") from None


def po_session_subject(session_id: str) -> str:
    return PO_SESSION_PREFIX + session_id


def list_order_key(event: OwnerEvent) -> tuple[int, float, int]:
    """The list order: open `needs_owner` events first, then newest first (the SQL read's ORDER BY)."""
    return (0 if event.pinned else 1, -event.created_at.timestamp(), -event.id)


#: The heading of the steward report's section for what only a human can do (steward skill, step 5).
NEEDS_HUMAN_HEADING = "needs a human"


def _heading(line: str) -> str:
    return line.strip().lstrip("#").strip().strip("*_").strip().rstrip(":").strip().casefold()


def needs_human_section(text: str) -> str | None:
    """The body of a "Needs a human" section in `text` (a markdown heading or a line of its own), or None.

    The body runs to the next markdown heading. The steward's report always carries the section, with a
    one-line "none" when nothing needs a human (steward skill, report structure): an empty or "none"
    body is no section here.
    """
    lines = str(text or "").splitlines()
    for index, line in enumerate(lines):
        if _heading(line) != NEEDS_HUMAN_HEADING:
            continue
        body: list[str] = []
        for following in lines[index + 1 :]:
            if following.lstrip().startswith("#"):
                break
            body.append(following)
        section = "\n".join(body).strip()
        if section.strip(" .-*_()").casefold() in {"", "none", "nothing"}:
            return None
        return section
    return None


def card_holds_mark(card: Mapping[str, Any] | None) -> bool:
    """Whether a card row's extension bag carries any field of the `waiting_owner` mark."""
    if not isinstance(card, Mapping):
        return False
    extensions = card.get("extensions")
    bag = extensions.get(EXTENSION_BAG) if isinstance(extensions, Mapping) else None
    return isinstance(bag, Mapping) and any(str(bag.get(key) or "") for key in MARK_KEYS)


def person_wait(card: Mapping[str, Any]) -> str | None:
    """The board's actual human waits. Machine wait cards never mean a human decision."""
    if card.get("closed") or card.get("state") == "done":
        return None
    if waiting_owner(card) is not None:
        return "current handover has no recorded owner answer"
    escalation = attention_record(card, OWNER_ESCALATION)
    if escalation:
        return str(escalation["reason"])
    return None


# --- the PostgreSQL store ------------------------------------------------------------------

#: A card carrying the mark, spelled once for the SQL reads and the refusal.
_HELD = (
    "(e.read_at IS NULL AND EXISTS (SELECT 1 FROM tasks t WHERE t.task_ref = e.subject_ref "
    "AND NOT t.archived AND t.state = 'in_progress' "
    "AND NOT EXISTS (SELECT 1 FROM task_supersessions u WHERE u.supersedes = t.task_ref) "
    "AND ((e.kind = 'card_handed_to_owner' "
    "AND COALESCE(t.extensions -> 'extra' ->> 'waiting_owner', '') <> '') "
    "OR (e.kind = 'po_card_escalated' "
    "AND COALESCE(t.extensions -> 'extra' ->> 'owner_escalation', '') <> ''))))"
)


class OwnerEventStore:
    """`owner_events` over the board store: one short connection per operation.

    `client` is a `SqlCardClient` whose open transaction a write on the same thread joins under a
    savepoint (:func:`settle` inside a card transition): the settle then commits or rolls back with
    the transition, and its own failure rolls back only the savepoint.
    """

    def __init__(self, credentials: Any, *, client: Any = None) -> None:
        self.credentials = credentials
        self.client = client

    @classmethod
    def for_instance(cls, instance_dir: Path | str, *, role: str = "app") -> OwnerEventStore:
        from ummanu.board.store import resolve_role

        return cls(resolve_role(instance_dir, role))

    @contextlib.contextmanager
    def _connection(self) -> Iterator[Any]:
        """A savepoint in the client's open transaction, or a short connection of its own.

        The client's connection passed the schema gate when it opened; an own connection reads it
        first, so a store that owes migrations is refused as :class:`OwnerEventsSchemaOwed` before
        `owner_events` is touched.
        """
        import psycopg

        try:
            client = self.client
            if client is not None and getattr(client, "_depth", 0):
                connection = client.connection
                with connection.transaction():
                    yield connection
                return
            with psycopg.connect(self.credentials.conninfo(), connect_timeout=5) as connection:
                require(connection, OwnerEventsSchemaOwed)
                yield connection
        except psycopg.Error as exc:
            raise OwnerEventsUnavailable(f"the board store did not answer an owner event operation: {exc}") from exc

    def insert(self, kind: str, subject_ref: str | None, text: str, dedup_key: str) -> bool:
        """One new event, or nothing when `dedup_key` is already recorded; True when this call wrote it."""
        event_class = class_of(kind)
        with self._connection() as connection:
            row = connection.execute(
                'INSERT INTO owner_events (kind, "class", subject_ref, text, created_at, dedup_key) '
                "VALUES (%s, %s, %s, %s, now(), %s) ON CONFLICT (dedup_key) DO NOTHING RETURNING id",
                (kind, event_class, subject_ref, text, dedup_key),
            ).fetchone()
        return row is not None

    def events(self, *, unread_only: bool = False, limit: int = LIST_LIMIT) -> list[OwnerEvent]:
        """Open `needs_owner` events first, then everything newest first; `unread_only` keeps the unread."""
        where = "WHERE e.read_at IS NULL " if unread_only else ""
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT {', '.join('e.' + column.strip() for column in _COLUMNS.split(','))}, {_HELD} "
                f"FROM owner_events e {where}"
                "ORDER BY (e.\"class\" = 'needs_owner' AND e.read_at IS NULL) DESC, e.created_at DESC, e.id DESC "
                "LIMIT %s",
                (limit,),
            ).fetchall()
        return [OwnerEvent(*row) for row in rows]

    def unread_count(self) -> int:
        with self._connection() as connection:
            return int(connection.execute("SELECT count(*) FROM owner_events WHERE read_at IS NULL").fetchone()[0])

    def snapshot(self) -> dict[str, Any]:
        """List, count and scoped open human waits at one SQL statement snapshot.

        The attention facts are genuine unread rows, never manufactured by a page read.
        A card subject is scoped through its real sprint foreign key; superseded/archived
        subjects cannot make that sprint wait. New card-wait events must still be held.
        """
        with self._connection() as connection:
            document = connection.execute(
                "SELECT jsonb_build_object('unread', (SELECT count(*) FROM owner_events WHERE read_at IS NULL), "
                "'needs_owner_count', (SELECT count(*) FROM owner_events WHERE read_at IS NULL AND class = 'needs_owner'), "
                "'notice_count', (SELECT count(*) FROM owner_events WHERE read_at IS NULL AND class = 'notice'), "
                f"'held_count', (SELECT count(*) FROM owner_events e WHERE e.read_at IS NULL AND e.class = 'needs_owner' AND {_HELD}), "
                "'events', COALESCE((SELECT jsonb_agg(row_to_json(listed)) FROM ("
                f"SELECT e.*, {_HELD} AS held FROM owner_events e "
                "ORDER BY (e.\"class\" = 'needs_owner' AND e.read_at IS NULL) DESC, e.created_at DESC, e.id DESC "
                "LIMIT %s) listed), '[]'::jsonb), "
                "'unread_events', COALESCE((SELECT jsonb_agg(row_to_json(listed)) FROM ("
                f"SELECT e.*, {_HELD} AS held FROM owner_events e WHERE e.read_at IS NULL "
                "ORDER BY (e.\"class\" = 'needs_owner') DESC, e.created_at DESC, e.id DESC "
                "LIMIT %s) listed), '[]'::jsonb), "
                "'human_waits', COALESCE((SELECT jsonb_agg(jsonb_build_object('event_id', e.id, "
                "'subject_ref', e.subject_ref, 'sprint_ref', COALESCE(t.sprint_ref, s.ref))) "
                "FROM owner_events e LEFT JOIN tasks t ON t.task_ref = e.subject_ref "
                "LEFT JOIN sprints s ON s.ref = e.subject_ref "
                "WHERE e.read_at IS NULL AND e.\"class\" = 'needs_owner' "
                "AND (t.sprint_ref IS NOT NULL OR s.ref IS NOT NULL) AND COALESCE(t.archived, false) = false "
                "AND NOT EXISTS (SELECT 1 FROM task_supersessions u WHERE u.supersedes = t.task_ref) "
                f"AND (e.kind = 'steward_needs_human' OR {_HELD})), '[]'::jsonb))",
                (LIST_LIMIT, LIST_LIMIT),
            ).fetchone()[0]
        for event in [*document["events"], *document["unread_events"]]:
            event["unread"] = event["read_at"] is None
            event["pinned"] = event["unread"] and event["class"] == NEEDS_OWNER
            event["held_reason"] = _held_fact(event["kind"], event["text"]) if event["held"] else None
        return document

    def settle_kind(self, subject_ref: str, kind: str) -> int:
        with self._connection() as connection:
            return connection.execute(
                "UPDATE owner_events SET read_at = now() WHERE subject_ref = %s AND kind = %s AND read_at IS NULL",
                (subject_ref, kind),
            ).rowcount

    def mark_read(self, event_id: int) -> OwnerEvent:
        """Mark one event read; an already read one answers as it is. Refuses a held `needs_owner` event."""
        with self._connection() as connection:
            row = connection.execute(
                f"SELECT {', '.join('e.' + column.strip() for column in _COLUMNS.split(','))}, {_HELD} "
                "FROM owner_events e WHERE e.id = %s FOR UPDATE",
                (event_id,),
            ).fetchone()
            if row is None:
                raise OwnerEventNotFound(f"there is no owner event {event_id}")
            event = OwnerEvent(*row)
            if event.read_at is not None:
                return event
            if event.event_class == NEEDS_OWNER and event.held:
                raise ReadRefused(_held_refusal(event))
            row = connection.execute(
                f"UPDATE owner_events SET read_at = now() WHERE id = %s RETURNING {_COLUMNS}",
                (event_id,),
            ).fetchone()
        return OwnerEvent(*row, held=event.held)

    def mark_all_read(self) -> int:
        """Mark every unread notice read; a `needs_owner` event is never touched here. How many were."""
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE owner_events SET read_at = now() WHERE read_at IS NULL AND \"class\" = 'notice'"
            )
            return cursor.rowcount

    def settle_subject(self, subject_ref: str) -> int:
        """The stay-unread rule's end: every unread `needs_owner` event of this card is read now."""
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE owner_events SET read_at = now() "
                "WHERE subject_ref = %s AND \"class\" = 'needs_owner' AND read_at IS NULL",
                (subject_ref,),
            )
            return cursor.rowcount


def _held_fact(kind: str, text: str) -> str:
    return ("current handover has no recorded owner answer" if kind == CARD_HANDED_TO_OWNER
            else "PO escalation remains unresolved: " + text)


def _held_refusal(event: OwnerEvent) -> str:
    fact = _held_fact(event.kind, event.text)
    return f"owner event {event.id} stays unread: {event.subject_ref}: {fact}"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


# --- the writer every producer calls ------------------------------------------------------


def _sink(to: Any) -> Any:
    """Where `to` writes: an event store as given, a board client's, or an installation's; None for nowhere.

    A card client names its own sink when it has an `owner_events` attribute (a test's fake does);
    a PostgreSQL client (`credentials`) writes to its own store, joining its open transaction. A path
    is an instance directory: its board store, or nowhere when it has none configured.
    """
    if to is None:
        return None
    if hasattr(to, "insert") and hasattr(to, "settle_subject"):
        return to
    own = getattr(to, "owner_events", None)
    if own is not None:
        return own
    credentials = getattr(to, "credentials", None)
    if credentials is not None and hasattr(credentials, "conninfo"):
        return OwnerEventStore(credentials, client=to if hasattr(to, "transaction") else None)
    if isinstance(to, (str, Path)):
        from ummanu.board.store import store_path

        if not store_path(to).exists():
            return None
        return OwnerEventStore.for_instance(to)
    return None


def record(kind: str, subject_ref: str | None, text: str, dedup_key: str, *, to: Any) -> bool:
    """Write one owner event, at most once per `dedup_key`; True when this call wrote it.

    The advisory writer. It never raises: an unknown kind is logged as the defect it
    is, and a store that refuses or does not answer is logged and skipped. `to` is where to write
    (see :func:`_sink`); None, or an installation with no board store, writes nowhere.
    """
    try:
        class_of(kind)
        if not str(dedup_key or "").strip():
            raise ValueError("an owner event needs a dedup key")
        sink = _sink(to)
        if sink is None:
            logger.info("owner event %s (%s) not recorded: no board store to record it in", kind, dedup_key)
            return False
        return bool(sink.insert(kind, subject_ref or None, _bounded(text), dedup_key))
    except Exception as exc:  # noqa: BLE001 - the bell never fails the fact it reports
        logger.warning("owner event %s (%s) not recorded: %s: %s", kind, dedup_key, type(exc).__name__, exc)
        return False


#: The answers of :func:`record_strict`.
WRITTEN = "written"
ALREADY_PRESENT = "already_present"
FAILED = "failed"
NOT_APPLICABLE = "not_applicable"


def record_strict(kind: str, subject_ref: str | None, text: str, dedup_key: str, *, to: Any) -> str:
    """Write one owner event for a caller whose own work is not complete without it (secretary-1792).

    The same write as :func:`record`, answered three ways instead of swallowed: :data:`WRITTEN`,
    :data:`ALREADY_PRESENT` under `dedup_key`, or :data:`FAILED` when the store raised or did not
    answer (logged), which the caller repeats under the same key. An installation with no board store
    configured at all answers :data:`NOT_APPLICABLE` (logged): there is no bell to wait for. It never
    raises. An unknown kind or an empty key is the caller's defect and answers :data:`FAILED`.
    """
    try:
        class_of(kind)
        if not str(dedup_key or "").strip():
            raise ValueError("an owner event needs a dedup key")
        sink = _sink(to)
    except Exception as exc:  # noqa: BLE001 - answered, not raised
        logger.warning("owner event %s (%s) not recorded: %s: %s", kind, dedup_key, type(exc).__name__, exc)
        return FAILED
    if sink is None:
        logger.info("owner event %s (%s) not recorded: no board store to record it in", kind, dedup_key)
        return NOT_APPLICABLE
    try:
        written = bool(sink.insert(kind, subject_ref or None, _bounded(text), dedup_key))
    except Exception as exc:  # noqa: BLE001 - the caller repeats it under the same key
        logger.warning("owner event %s (%s) not recorded: %s: %s", kind, dedup_key, type(exc).__name__, exc)
        return FAILED
    return WRITTEN if written else ALREADY_PRESENT


def settle(subject_ref: str, *, to: Any) -> int:
    """Mark read every unread `needs_owner` event of a card whose mark just cleared; never raises."""
    try:
        sink = _sink(to)
        if sink is None:
            return 0
        return int(sink.settle_subject(subject_ref) or 0)
    except Exception as exc:  # noqa: BLE001 - the card's transition never fails on the bell
        logger.warning("owner events of %s not settled: %s: %s", subject_ref, type(exc).__name__, exc)
        return 0


def record_person_wait(card: Mapping[str, Any], occurrence: str, *, to: Any) -> None:
    """Required within the card mutation, never a GET. One event per real wait episode."""
    reference = str(card.get("ref") or "")
    if card.get("closed") or card.get("state") == "done" or card.get("type") == "wait":
        return
    if waiting_owner(card) is not None or not card.get("sprint"):
        return
    routine = (card.get("state") == "blocked" and not e2e_state(card).budget_decline) or (
        card.get("state") == "in_progress" and card.get("type") in {"decision", "operation"})
    if routine:
        record_required_wait(CARD_WAITS_FOR_PERSON, reference, f"{reference}: with the PO/observer",
                             f"{CARD_WAITS_FOR_PERSON}:{reference}:{occurrence}", to=to)


@contextlib.contextmanager
def _required_wait_store(subject_ref: str, *, to: Any) -> Iterator[Any]:
    """Let savepoint failures reach the card transaction with their backend cause intact."""
    try:
        sink = _sink(to)
        if sink is None:
            raise OwnerEventsUnavailable("no board store configured")
        yield sink
    except Exception as exc:
        raise OwnerEventsUnavailable(f"required owner wait for {subject_ref}: {exc}") from exc


def record_required_wait(kind: str, subject_ref: str, text: str, dedup_key: str, *, to: Any) -> None:
    """Create the card's authoritative unread fact inside its mutation transaction."""
    with _required_wait_store(subject_ref, to=to) as sink:
        if kind not in {CARD_HANDED_TO_OWNER, CARD_WAITS_FOR_PERSON, PO_CARD_ESCALATED} or not dedup_key.strip():
            raise ValueError("a required card wait needs its established kind and occurrence key")
        sink.insert(kind, subject_ref, _bounded(text), dedup_key)


def settle_required_wait(subject_ref: str, *, to: Any, kind: str | None = None) -> None:
    """End or replace a human wait atomically with the card effect; never best effort."""
    with _required_wait_store(subject_ref, to=to) as sink:
        if kind is None:
            sink.settle_subject(subject_ref)
        else:
            sink.settle_kind(subject_ref, kind)


def _bounded(text: str) -> str:
    text = str(text or "").strip() or "(no text)"
    return text if len(text) <= TEXT_LIMIT else text[: TEXT_LIMIT - 1] + "…"


__all__ = [
    "ALREADY_PRESENT",
    "BUDGET_SIGNAL",
    "CARD_HANDED_TO_OWNER",
    "CARD_WAITS_FOR_PERSON",
    "CLASSES",
    "DELEGATED_CARD_SETTLED",
    "E2E_AFTER_MERGE",
    "E2E_BUDGET_SPENT",
    "FAILED",
    "HEAD_DEAD",
    "KINDS",
    "KIND_CLASS",
    "NEEDS_OWNER",
    "NEEDS_OWNER_KINDS",
    "NOTICE",
    "NOT_APPLICABLE",
    "OBSERVER_DEAD",
    "PO_CARD_ESCALATED",
    "PO_TURN_FAILED",
    "PROVIDER_RED",
    "SPRINT_CLOSED",
    "SPRINT_STOPPED",
    "STEWARD_NEEDS_HUMAN",
    "WRITTEN",
    "OwnerEvent",
    "OwnerEventError",
    "OwnerEventNotFound",
    "OwnerEventStore",
    "OwnerEventsSchemaOwed",
    "OwnerEventsUnavailable",
    "ReadRefused",
    "card_holds_mark",
    "class_of",
    "list_order_key",
    "needs_human_section",
    "person_wait",
    "po_session_subject",
    "record",
    "record_person_wait",
    "record_required_wait",
    "record_strict",
    "settle",
    "settle_required_wait",
]
