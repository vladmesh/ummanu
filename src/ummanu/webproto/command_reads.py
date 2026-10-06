"""What was commanded on this installation, and what became of one request id.

`command_history` pages the committed audit across every entity, newest first (the journal's append
order reversed, never a sort by `occurred_at`); each row carries `actor`, `action`, `entity` and
`result`. `command_request` answers `not_found`, `pending` (with the safe continuation), `committed`
or `unknown` for one request id, from the audit's own lookups, without re-sending anything.

The audit is the card audit via :func:`ummanu.tasks.task_audit_for` (`requests`/`board_events`,
`docs/BOARD_STORE.md` §7.3); the file journal is never consulted. No second store or index: history
is `SqlTaskAudit.events_page`, lookups are `committed_event`/`pending_event` (the pair the writers
use), paging is the frozen-offset cursor of :mod:`ummanu.webproto.cursor`.

An unreadable audit is an unavailable source, never an empty history and never `not_found`: it is
`unknown`. An entity kind the record does not carry stays `null`. Neither read writes, locks, retries
or repairs. See docs/PROTOCOLS.md, "What has been commanded, and what became of a request".
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ummanu.board.models import Event
from ummanu.config import InstanceReport, validate_instance
from ummanu.tasks import _event_action, task_audit_for
from ummanu.webproto import sources
from ummanu.webproto.boundary import ProtocolBoundary
from ummanu.webproto.cursor import Cursor, decode
from ummanu.webproto.errors import InvalidCursor, ValidationRefused
from ummanu.webproto.journal import DEFAULT_LIMIT, MAX_LIMIT
from ummanu.webproto.section import Reading, Section, SectionSet, SourceSet, render, rule
from ummanu.webproto.section import read_source as _source

SCHEMA_VERSION = 1

#: The sources of these documents, in the precedence a refusal is attributed in.
SOURCE_INSTALLATION = "installation"
SOURCE_AUDIT = "audit"

#: The reference a history cursor is bound to: none, so card cursors and history cursors are never
#: interchangeable.
HISTORY_SCOPE = ""

#: The four states :meth:`CommandReadLayer.command_request` answers with; `unknown` is never folded
#: into another.
STATE_COMMITTED = "committed"
STATE_PENDING = "pending"
STATE_NOT_FOUND = "not_found"
STATE_UNKNOWN = "unknown"

#: What the history covers, stated on the document; read from no source.
HISTORY_EXTENT = {
    "entities": "all",
    "records": "committed",
    "order": "journal_append_reversed",
    "statement": (
        "This is the committed board audit of the whole installation, newest first, across every "
        "entity it holds -- cards, sprints, products and issues alike. Newest means the journal's "
        "append order reversed and not a sort by `occurred_at`, which the writer stamps and which "
        "two commands can share. A staged operation that has not committed is not in it: it is not "
        "an event yet, and `command_request` is where one is read. A record the released traversal "
        "cannot parse is not in it either, and neither is anything the audit does not hold."
    ),
}

#: The operation-identity contract: which operations take a `request_id`, what a repeat means, and
#: what part-done failures promise. `tests/test_web_command_protocol.py` derives both sets from the
#: operation layers' signatures and holds `docs/PROTOCOLS.md` to this table.
OPERATION_IDENTITY: dict[str, Any] = {
    "with_request_id": {
        "run_start": {
            "layer": "OperationLayer",
            "repeat": (
                "returns the same run. It raises no second head and cuts no second workspace: the "
                "request id owns the run, and a reconnect is the same call."
            ),
        },
        "run_review": {
            "layer": "OperationLayer",
            "repeat": (
                "returns the same reviewer run over the same worker result, and raises no second "
                "reviewer head."
            ),
        },
        "sprint_create": {
            "layer": "SprintOperationLayer",
            "repeat": (
                "resumes the sprint this request already opened, finishing whatever step was owed. "
                "A new request id would open a second sprint beside the half-written one, which is "
                "why a part-done create answers with this id rather than with a plain failure."
            ),
        },
        "sprint_comment": {
            "layer": "SprintOperationLayer",
            "repeat": (
                "returns the comment this request already saved. It never writes a second comment "
                "on the sprint."
            ),
        },
        "sprint_close": {
            "layer": "SprintOperationLayer",
            "repeat": (
                "resumes the staged close, keeps the plan it was staged with and repeats no step it "
                "already committed. A new request id would open a second close beside a "
                "half-finished one."
            ),
        },
    },
    "without_request_id": {
        "pause_drain": {
            "layer": "PauseOperationLayer",
            "reason": (
                "the pause is idempotent in its own mode by its own rule: a drain over a pipeline "
                "already draining changes nothing and says so, and a drain over a freeze is refused "
                "as a conflict. A repeat therefore needs no key, and adding one for uniformity "
                "would buy an operation-id ceremony over a command that completes in one call."
            ),
        },
        "pause_resume": {
            "layer": "PauseOperationLayer",
            "reason": (
                "a resume over a pipeline that is not paused is a no-op that reports itself as one. "
                "Its idempotence is the state of the flag, not a recorded request."
            ),
        },
    },
    "errors": {
        "OperationPending": {
            "code": "backend_unavailable",
            "promises": (
                "the operation is durably part-done and repairable, so 'it did not finish' is not "
                "'it did not happen'. What it already did is staged, and the safe move is to repeat "
                "this same request id."
            ),
            "action": ["operation", "repeat_request", "request_id", "reference"],
        },
        "audit_pending": {
            "exit_status": 4,
            "promises": (
                "the writer's own spelling of the same fact, and the exit status the sprint close "
                "has always answered with. The staged record is kept, never discarded, and a repeat "
                "with the same request id resumes it."
            ),
        },
        "close_conflict": {
            "exit_status": 3,
            "promises": (
                "the close was refused on the state of the world rather than on its arguments -- "
                "the sprint moved under it -- and nothing was written. It is not a part-done "
                "operation and repeating it unchanged will be refused again."
            ),
        },
        "PauseCommandCompleted": {
            "promises": (
                "the pause or resume itself completed and only the report of it could not be "
                "rendered. The command answers with what it did rather than failing, because a "
                "failure would invite a retry of a command that already succeeded."
            ),
        },
    },
}

#: The codes each read can refuse with, checked against `docs/PROTOCOLS.md` by a test. The layer-wide
#: `backend_unavailable` from :mod:`ummanu.webproto.boundary` is not listed per read.
COMMAND_ERRORS: dict[str, tuple[str, ...]] = {
    "command_history": ("validation",),
    "command_request": ("validation",),
}


class _Unreadable(Exception):
    """Inside one source read: the durable document could not be read at all."""


@dataclass(frozen=True, slots=True)
class _History:
    """One page of the committed audit: `total` committed records, and `records` at ordinals
    `[end - limit, end)`.
    """

    total: int
    records: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class _Lookup:
    """The audit's two lookups for one request id, both read inside the source span."""

    request_id: str
    committed: dict[str, Any] | None = None
    pending: dict[str, Any] | None = None


def _page(history: _History, cursor: Cursor | None, limit: int) -> dict[str, Any]:
    """The newest `limit` records at or before `cursor`, newest first, and where reading continues.

    `offset` counts committed records before the oldest row handed out; it is frozen because the
    journal only grows.
    """
    end = history.total if cursor is None else cursor.offset
    if end > history.total:
        raise InvalidCursor(
            "this cursor is past the end of the committed audit, which only ever grows; "
            "the journal it was issued for is not the journal being read"
        )
    start = max(0, end - limit)
    return {
        "items": [_row(record) for record in reversed(history.records)],
        "next_cursor": Cursor(ref=HISTORY_SCOPE, offset=start).encode(),
        # True only when the limit cut the page short.
        "has_more": start > 0,
    }


def _row(record: dict[str, Any]) -> dict[str, Any]:
    """One committed audit record as a command row: initiator, action, target entity, result.

    Unlike :func:`ummanu.webproto.journal._item` it carries only these fields, but shares its rule:
    a typed event's `reason` and a generic record's `outcome` are never renamed into each other.
    """
    actor = record.get("actor")
    # The journal's discriminator (as in `EventJournal` and `BoardEventCanon`): only typed protocol
    # events declare this record type.
    typed = record.get("record_type") == Event.RECORD_TYPE
    return {
        "occurred_at": _text(record.get("occurred_at")) or None,
        "request_id": _text(record.get("request_id")) or None,
        "event_id": _text(record.get("event_id")) or None,
        "actor": (
            {"id": _text(actor.get("id")), "role": _text(actor.get("role"))}
            if isinstance(actor, dict)
            else None
        ),
        "action": _event_action(record),
        "kind": _text(record.get("kind")),
        "entity": _entity(record, typed),
        "result": {
            "outcome": _text(record.get("outcome")) or None,
            "reason": _text(record.get("reason")) or None,
        },
        "typed": typed,
    }


def _entity(record: dict[str, Any], typed: bool) -> dict[str, Any]:
    """The target entity of one record; `kind` is `null` unless a typed event's `subject` states it."""
    reference = _text(record.get("ref"))
    subject = record.get("subject") if typed else None
    kind: str | None = None
    if isinstance(subject, dict):
        kind = _text(subject.get("kind")) or None
        reference = _text(subject.get("ref")) or reference
    return {"ref": reference, "kind": kind}


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


class CommandSections(SectionSet):
    """Every section of both documents, and the only place a source is attributed to one."""

    def commands(self, read: SourceSet) -> Section:
        """One page of the committed history, newest first.

        `items` is `null`, never `[]`, when the audit did not answer.
        """
        return read.decide(
            rule(SOURCE_AUDIT, lambda page: dict(page), needs=(SOURCE_AUDIT,)),
            blank={"items": None, "next_cursor": None, "has_more": None},
            narrates=(),
        )

    def operation(self, read: SourceSet) -> Section:
        """What became of one request id, from the audit's two lookups.

        Committed wins over staged (as `SqlTaskAudit.event` decides); a stale staged record beside it
        is reported as `staged`. The blank is `unknown`, so a refused audit can never read as
        `not_found`; the seam enforces this.
        """
        return read.decide(
            rule(SOURCE_AUDIT, _outcome, needs=(SOURCE_AUDIT,)),
            blank={
                "state": STATE_UNKNOWN,
                "action": None,
                "kind": None,
                "entity": None,
                "actor": None,
                "occurred_at": None,
                "event_id": None,
                "result": None,
                "staged": None,
                "continuation": None,
            },
            narrates=(),
        )


def _outcome(lookup: _Lookup) -> dict[str, Any]:
    """The three answers when the audit answered: committed, pending or not found."""
    record = lookup.committed
    if record is not None:
        found = _row(record)
        return {
            "state": STATE_COMMITTED,
            "action": found["action"],
            "kind": found["kind"],
            "entity": found["entity"],
            "actor": found["actor"],
            "occurred_at": found["occurred_at"],
            "event_id": found["event_id"],
            "result": found["result"],
            # An uncleared staged record beside a committed one is an owed repair, said as one.
            "staged": lookup.pending is not None,
            "continuation": None,
        }
    staged = lookup.pending
    if staged is not None:
        found = _row(staged)
        return {
            "state": STATE_PENDING,
            "action": found["action"],
            "kind": found["kind"],
            "entity": found["entity"],
            "actor": found["actor"],
            "occurred_at": found["occurred_at"],
            "event_id": found["event_id"],
            # A staged effect may still fail, so its result is not established.
            "result": None,
            "staged": True,
            "continuation": _continuation(lookup.request_id, found["action"]),
        }
    return {
        "state": STATE_NOT_FOUND,
        "action": None,
        "kind": None,
        "entity": None,
        "actor": None,
        "occurred_at": None,
        "event_id": None,
        "result": None,
        "staged": False,
        "continuation": None,
    }


def _continuation(request_id: str, action: str) -> dict[str, Any]:
    """How a part-done operation is continued: repeat exactly this request id.

    Same shape as `OperationPending`'s `data.action.repeat_request`. Description only; nothing is
    repeated here.
    """
    return {
        "repeat_request": True,
        "request_id": request_id,
        "action": action,
        "statement": (
            "This request is staged and not committed, so what it did is durably recorded and may "
            "be part-done. The safe continuation is to repeat the operation with this same request "
            "id, which resumes what exists; a new request id would start a second operation beside "
            "it. This read performs no part of it."
        ),
    }


#: Stateless; one instance serves every document.
SECTIONS = CommandSections()


class CommandReadLayer(ProtocolBoundary):
    """One installation's committed commands, read with no knowledge of who is asking.

    Construction does no I/O. `clock` and `board_client` are seams for tests or transports, not modes.
    """

    def __init__(
        self,
        instance: str | Path,
        *,
        data_dir: str | Path | None = None,
        board_client: Any | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.instance = Path(instance)
        self._data_dir = Path(data_dir) if data_dir is not None else None
        # The audit both reads consult is the one this client names.
        self._board_client = board_client
        self._clock = clock

    # -- operations ---------------------------------------------------------------------------

    def command_history(self, cursor: str | None = None, *, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
        """A page of the last commands across every entity, newest first.

        `next_cursor` continues into older commands; `has_more` says the limit cut this page short.
        Writes nothing and takes no lock.
        """
        now = self._clock()
        bounded = max(1, min(int(limit), MAX_LIMIT))
        position = None if not cursor else decode(cursor, ref=HISTORY_SCOPE)
        report, installation = self._installation(now=now)
        audit = self._history(
            self.data_dir(report), end=None if position is None else position.offset, limit=bounded, now=now
        )
        if audit.answered:
            # Paged outside the source span: a foreign cursor is the caller being refused, and a
            # paging defect is this layer's own, not the audit failing to answer.
            audit = Reading(SOURCE_AUDIT, audit.source, _page(audit.value, position, bounded))
        read = SourceSet([installation, audit])
        return render(
            {
                "schema_version": SCHEMA_VERSION,
                "kind": "command_history",
                "observed_at": sources.isoformat(now),
                "limit": bounded,
                "extent": dict(HISTORY_EXTENT),
                "commands": SECTIONS.commands(read),
                "sources": self._marks(read),
            }
        )

    def command_request(self, request_id: str) -> dict[str, Any]:
        """What became of one request id: not found, pending, committed -- or unknown.

        Re-decides nothing and never performs, retries or repairs; a pending answer describes the
        safe continuation. :data:`OPERATION_IDENTITY` travels on the document.
        """
        now = self._clock()
        identifier = str(request_id or "")
        if not identifier:
            raise ValidationRefused(
                "reading what became of an operation needs the request id it was sent with"
            )
        report, installation = self._installation(now=now)
        read = SourceSet([installation, self._request(self.data_dir(report), identifier, now=now)])
        return render(
            {
                "schema_version": SCHEMA_VERSION,
                "kind": "command_request",
                "observed_at": sources.isoformat(now),
                "request_id": identifier,
                "operation": SECTIONS.operation(read),
                # Not a section: read from no source, so stated whatever every source did.
                "identity": _identity(),
                "sources": self._marks(read),
            }
        )

    # -- shared plumbing ----------------------------------------------------------------------

    def report(self) -> InstanceReport:
        report, refused = self._installation(now=self._clock())
        if report is None:
            raise ValidationRefused(str(refused.source.reason))
        return report

    def data_dir(self, report: InstanceReport | None = None) -> Path:
        if self._data_dir is not None:
            return self._data_dir
        report = report if report is not None else self.report()
        assert report.data_dir is not None
        return report.data_dir

    def _installation(self, *, now: float) -> tuple[InstanceReport | None, Reading]:
        """The installation config as a source, and the refusal only it can force.

        With an explicit data directory, an invalid config removes only what it owns. Without one,
        the read is refused as `validation`.
        """

        def produce() -> InstanceReport:
            report = validate_instance(self.instance)
            if not report.ok or report.data_dir is None:
                raise _Unreadable(
                    "this instance config does not validate: "
                    + "; ".join(str(error) for error in report.errors[:5])
                    if report.errors
                    else "this instance config names no data directory"
                )
            return report

        reading = _source(
            SOURCE_INSTALLATION,
            produce,
            refusal=lambda exc: (
                str(exc)
                if isinstance(exc, _Unreadable)
                else f"this instance config could not be read: {_reason(exc)}"
            ),
            now=now,
            evidence=self._instance_file(),
        )
        if reading.answered:
            return reading.value, Reading(SOURCE_INSTALLATION, reading.source, None)
        if self._data_dir is None:
            raise ValidationRefused(str(reading.source.reason))
        return None, reading

    # -- the source ---------------------------------------------------------------------------

    def _history(self, data_dir: Path, *, end: int | None, limit: int, now: float) -> Reading:
        """One page of the committed audit via `events_page`, read once for the document.

        Any failure refuses the source. Records the traversal cannot parse are skipped by it, as
        :data:`HISTORY_EXTENT` states. On PostgreSQL the cost is the page and the pages above it, not
        the whole history. A cursor past the end reads no rows and is refused by `_page`.
        """

        def produce(audit: Any) -> _History:
            total, records = audit.events_page(end=end, limit=limit)
            return _History(total, tuple(records))

        return self._audit(data_dir, produce, now=now)

    def _request(self, data_dir: Path, request_id: str, *, now: float) -> Reading:
        """`committed_event` then `pending_event` for one request id, inside one source span.

        Both are always read, so a stale staged record beside a committed one is reported. Nothing
        is written or repaired.
        """

        def produce(audit: Any) -> _Lookup:
            return _Lookup(request_id, audit.committed_event(request_id), audit.pending_event(request_id))

        return self._audit(data_dir, produce, now=now)

    def _audit(self, data_dir: Path, produce: Callable[[Any], Any], *, now: float) -> Reading:
        """One read of the card audit (`requests`/`board_events`), the sole source of both documents.

        Never the file journal, which a PostgreSQL backend never writes. An empty `requests` table is
        a real answer; an unreachable store or an unbuildable client refuses the source.
        """

        def read() -> Any:
            return produce(task_audit_for(self._client(), data_dir))

        return _source(
            SOURCE_AUDIT,
            read,
            refusal=lambda exc: f"the committed board audit could not be read: {_reason(exc)}",
            now=now,
            evidence=None,
        )

    def _client(self) -> Any:
        """The card client of this installation: an injected one, or the switch's (§2.2)."""
        from ummanu.board.backend import CARD, board_client

        return self._board_client or board_client(
            self.instance.parent if self.instance.is_file() else self.instance, serves=(CARD,)
        )

    @staticmethod
    def _marks(read: SourceSet) -> dict[str, Any]:
        """The availability of every source, stated once for the document."""
        return {key: read.mark(key) for key in (SOURCE_AUDIT, SOURCE_INSTALLATION)}

    def _instance_file(self) -> Path:
        """The config file itself, so a refusal can be dated by it even when reading it failed."""
        return self.instance if self.instance.is_file() else self.instance / "instance.yaml"


def _identity() -> dict[str, Any]:
    """:data:`OPERATION_IDENTITY` as a document field, copied so a caller cannot edit the contract."""
    return {
        "with_request_id": {
            name: dict(entry) for name, entry in OPERATION_IDENTITY["with_request_id"].items()
        },
        "without_request_id": {
            name: dict(entry) for name, entry in OPERATION_IDENTITY["without_request_id"].items()
        },
        "errors": {name: dict(entry) for name, entry in OPERATION_IDENTITY["errors"].items()},
    }


def _reason(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


__all__ = [
    "COMMAND_ERRORS",
    "HISTORY_EXTENT",
    "HISTORY_SCOPE",
    "OPERATION_IDENTITY",
    "SCHEMA_VERSION",
    "SOURCE_AUDIT",
    "SOURCE_INSTALLATION",
    "STATE_COMMITTED",
    "STATE_NOT_FOUND",
    "STATE_PENDING",
    "STATE_UNKNOWN",
    "CommandReadLayer",
    "CommandSections",
]
