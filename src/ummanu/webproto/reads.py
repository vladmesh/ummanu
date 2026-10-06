"""The read operations every transport of this installation answers from. Nothing here writes.

`system_snapshot`, `task_snapshot` and `task_events` are the shared surface; `head_view` shows one
local-pty head's terminal tail and journal without Orca (:mod:`ummanu.webproto.head_view`).
Everything is assembled from existing owners (`collect_status`, the validated project bindings,
`TaskReader`, the card client's audit owner, the dispatcher production state plus launch
heartbeats); no second collector, no cache file. Each section carries its own availability
(:mod:`ummanu.webproto.sources`), so one failed source never blanks another.
See docs/PROTOCOLS.md, "Reading the pipeline".
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from ummanu.board.backend import CARD, board_client
from ummanu.board.owner_handover import waiting_owner
from ummanu.board.production_rights import touches_production
from ummanu.checkpoint import rpo_problem
from ummanu.config import InstanceReport, validate_instance
from ummanu.dispatch.state import DispatcherRecord

# Public row disposition for transports, shared with the native doctor evaluator.
from ummanu.infra.doctor_findings import accepted as accepted
from ummanu.status import collect_status
from ummanu.tasks import TaskError, TaskReader, task_audit_for
from ummanu.webproto import agents as agent_reads
from ummanu.webproto import head_view as head_reads
from ummanu.webproto import sources
from ummanu.webproto.boundary import ProtocolBoundary
from ummanu.webproto.cursor import Cursor, decode
from ummanu.webproto.errors import (
    HeadRunNotFound,
    InstallationUnavailable,
    InvalidCursor,
    ReadError,
    TaskNotFound,
)
from ummanu.webproto.journal import DEFAULT_LIMIT, CommittedAudit, EventPage
from ummanu.webproto.section import read_source

SCHEMA_VERSION = 1

#: The states a card is "current" in: what the pipeline carries now (not `issues` backlog or `done`).
CURRENT_TASK_STATES = ("ready", "in_progress", "validate", "assessment", "blocked")

#: How many of a card's most recent events its task snapshot opens with.
TASK_SNAPSHOT_EVENTS = 20

def hold_store_exclusion(instance: str | Path) -> str | None:
    """Run the board store's exclusion guard once for this process; the refusal, if it refused.

    Called by a long-lived reader before it serves (:func:`ummanu.board.store.hold_exclusion`). A
    refusal is held too, so later reads answer with it.
    """
    from ummanu.board.store import BoardStoreError, hold_exclusion

    try:
        hold_exclusion(instance)
    except BoardStoreError as refused:
        return str(refused)
    return None


class ReadLayer(ProtocolBoundary):
    """One installation, read three ways, with no knowledge of who is asking.

    Construction does no I/O; every operation reads at call time, so nothing is cached from start-up.
    ``board_client`` and ``status_reader`` inject those sources (tests, transports); not a mode.
    ``health_reader`` supplies a cached :meth:`health_snapshot` reading (the web doctor lamp) for
    :meth:`system_snapshot`, so panel and lamp are one collection; without it health is collected per call.
    """

    def __init__(
        self,
        instance: str | Path,
        *,
        data_dir: str | Path | None = None,
        board_client: Any | None = None,
        status_reader: Callable[[], dict[str, Any]] | None = None,
        health_reader: Callable[[], dict[str, Any]] | None = None,
        offline: bool = False,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.instance = Path(instance)
        self._data_dir = Path(data_dir) if data_dir is not None else None
        self._board_client = board_client
        self._resolved_client: Any | None = board_client
        self._status_reader = status_reader
        self._health_reader = health_reader
        self.offline = offline
        self._clock = clock

    # -- shared plumbing -------------------------------------------------------------------

    def report(self) -> InstanceReport:
        """The validated instance, or `InstallationUnavailable`: without it there is no data plane to read."""
        report = validate_instance(self.instance)
        if not report.ok or report.data_dir is None:
            raise InstallationUnavailable(
                "this instance config does not validate: "
                + "; ".join(str(error) for error in report.errors[:5])
            )
        return report

    def data_dir(self, report: InstanceReport | None = None) -> Path:
        """The data plane, overridden or from config. Resolved once per operation and threaded through."""
        if self._data_dir is not None:
            return self._data_dir
        report = report if report is not None else self.report()
        assert report.data_dir is not None
        return report.data_dir

    def _client(self) -> Any:
        """The board client: injected, or the switch's (§2.2). Kept once built; a failed build is retried."""
        if self._resolved_client is None:
            self._resolved_client = self._board_client or board_client(
                self.instance.parent if self.instance.is_file() else self.instance, serves=(CARD,)
            )
        return self._resolved_client

    def _events(self, data_dir: Path) -> CommittedAudit:
        """The reader of a card's history: the client's audit owner via :func:`ummanu.tasks.task_audit_for`.

        It pages the committed `requests`/`board_events` traversal (`docs/BOARD_STORE.md` §7.3); the
        file projection under `<data>/board` is never consulted.
        """
        return CommittedAudit(task_audit_for(self._client(), data_dir))

    def _unselected(
        self, ref: str, cursor: str | None, exc: Exception, data_dir: Path, *, now: float
    ) -> EventPage:
        """A card backend that could not be established, as an unavailable source (never an empty page).

        A cursor that parses is handed back untouched, so a polling client resumes once the backend answers.
        """
        position: Cursor | None = None
        if cursor not in (None, ""):
            try:
                position = decode(str(cursor), ref=ref)
            except InvalidCursor:
                position = None
        return EventPage(
            items=(),
            next_cursor=position or Cursor(ref=ref, offset=0),
            has_more=False,
            source=sources.unavailable(
                f"the card backend that owns this card's history could not be established: "
                f"{_reason(exc)}",
                now=now,
                evidence=data_dir / "board",
            ),
        )

    def _production_path(self, data_dir: Path) -> Path:
        return data_dir / "dispatcher" / "production-state.json"

    # -- operations ------------------------------------------------------------------------

    def system_snapshot(self) -> dict[str, Any]:
        """Installation health, registered projects, current cards and the agents running now."""
        now = self._clock()
        report = self.report()
        data_dir = self.data_dir(report)
        health = self._system_health(report, data_dir, now=now)
        projects = self._projects(report, now=now)
        tasks = self._tasks(data_dir, now=now)
        agents = self._agents(data_dir, now=now, projects_by_ref=_projects_by_ref(tasks["items"]))
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "system",
            "observed_at": sources.isoformat(now),
            "installation": {
                "instance": str(report.instance_path),
                "name": report.name or None,
                "data_dir": str(data_dir),
                "health": health,
            },
            "projects": projects,
            "tasks": tasks,
            "agents": agents,
        }

    def health_snapshot(self) -> dict[str, Any]:
        """Installation health alone: the same `_health` `system_snapshot` carries, without the rest."""
        now = self._clock()
        report = self.report()
        from ummanu.infra.doctor_record import read_latest

        data_dir = self.data_dir(report)
        health = self._health(report, data_dir, now=now)
        health["doctor"] = read_latest(report.instance_path, data_dir, now=now, offline=self.offline)
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "health",
            "observed_at": sources.isoformat(now),
            "health": health,
        }

    def task_snapshot(self, ref: str, *, events: int = TASK_SNAPSHOT_EVENTS) -> dict[str, Any]:
        """One card: its state, its project, its recent history, its heads and its result."""
        now = self._clock()
        reference = str(ref or "")
        if not reference:
            raise TaskNotFound("a task reference is required")
        report = self.report()
        data_dir = self.data_dir(report)
        card, card_source = self._card(reference, data_dir, now=now)
        page, history = self._tail(reference, data_dir, limit=events, now=now)
        attempt, record, attempt_source = self._attempt(reference, data_dir, now=now)
        rows = agent_reads.agent_rows(record, reference) if record is not None else []
        heads = self._heads(reference, data_dir, record, attempt_source, page.source, history, now=now)
        project = _text(card.get("project")) if card else None
        for row in rows:
            row["project"] = project
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "task",
            "observed_at": sources.isoformat(now),
            "ref": reference,
            "card": {"source": card_source.to_json(), "value": _card_value(card)},
            "project": self._project_of(report, project),
            "attempt": {"source": attempt_source.to_json(), "value": attempt},
            "agents": {"source": attempt_source.to_json(), "items": rows},
            "heads": heads,
            "work": _work(card),
            "events": {
                "source": page.source.to_json(),
                "items": list(page.items),
                "next_cursor": page.next_cursor.encode(),
            },
        }

    def po_delegated(self, session_id: str) -> dict[str, Any]:
        """The cards a PO session delegated, or whose results now go to it, from one board listing.

        One batched `TaskReader.list`, nothing per card. A card counts when its `origin.po_session`
        is this session (`delegated`), its `po_execution.executor` is (`assigned`), or its results now
        go here (`origin.current_session`, `inherited`). `items` is null, never `[]`, when the board
        could not be read.
        """
        now = self._clock()
        session = str(session_id or "")
        report = self.report()
        data_dir = self.data_dir(report)
        try:
            cards = TaskReader(self._client()).list()
        except Exception as exc:  # noqa: BLE001 -- confined to this source read
            return {
                "kind": "po_delegated",
                "session": session,
                "source": sources.unavailable(
                    f"the board could not be read: {_reason(exc)}",
                    now=now,
                    evidence=data_dir / "board" / "cards.ndjson",
                ).to_json(),
                "items": None,
            }
        items = []
        for card in cards:
            origin = _object(card.get("origin"))
            delegated = _text(origin.get("po_session")) == session
            assigned = _text(_object(card.get("po_execution")).get("executor")) == session
            if not session or not (delegated or assigned or _text(origin.get("current_session")) == session):
                continue
            returns = [row for row in origin.get("returns") or [] if isinstance(row, dict)]
            items.append(
                {
                    "ref": _text(card.get("ref")),
                    "title": _text(card.get("title")),
                    "type": _text(card.get("type")) or None,
                    "state": _text(card.get("state")) or None,
                    "relation": "assigned" if assigned else "delegated" if delegated else "inherited",
                    "last_return": returns[-1] if returns else None,
                }
            )
        return {
            "kind": "po_delegated",
            "session": session,
            "source": sources.available(now).to_json(),
            "items": items,
        }

    def task_events(
        self, ref: str, cursor: str | None = None, *, limit: int = DEFAULT_LIMIT
    ) -> dict[str, Any]:
        """One page of a card's history and the cursor that continues it.

        Reading a page's ``next_cursor`` returns exactly what was appended after it; the same cursor
        twice returns the same page (:mod:`ummanu.webproto.cursor`). Positions are ordinals in the
        committed `requests` traversal; a byte-offset cursor from the old file journal is refused by
        name, and the caller restarts from :meth:`task_snapshot`.
        """
        now = self._clock()
        reference = str(ref or "")
        if not reference:
            raise TaskNotFound("a task reference is required")
        data_dir = self.data_dir()
        try:
            reader = self._events(data_dir)
        except Exception as exc:  # noqa: BLE001 -- confined to this source read
            page = self._unselected(reference, cursor, exc, data_dir, now=now)
            return _events_document(reference, page, now=now, cursor=cursor)
        position: Cursor | None = (
            None
            if cursor in (None, "")
            else decode(str(cursor), ref=reference)
        )
        page = reader.page(reference, cursor=position, limit=limit, now=now)
        return _events_document(reference, page, now=now, cursor=cursor)

    def head_view(self, ref: str, run_id: str) -> dict[str, Any]:
        """A read-only view of one of the card's local-pty heads: its terminal's tail and its journal.

        `run_id` must be one the card recorded (current worker or reviewer run, or a launch its history
        names); any other is not found. Each source is then read under a guard
        (:mod:`ummanu.webproto.head_view`).
        """
        now = self._clock()
        reference = str(ref or "")
        if not reference:
            raise TaskNotFound("a task reference is required")
        data_dir = self.data_dir()
        self._card_exists(reference)
        try:
            history: tuple[dict[str, Any], ...] | None = self._events(data_dir).history(reference)
        except Exception as exc:  # noqa: BLE001 -- confined to this source read
            raise InstallationUnavailable(
                f"this card's history could not be read, so its head runs are not known: {_reason(exc)}"
            ) from None
        try:
            record = self._records(data_dir).get(reference)
        except Exception as exc:  # noqa: BLE001 -- confined to this source read
            raise InstallationUnavailable(
                f"the dispatcher production state could not be read: {_reason(exc)}"
            ) from None
        heads = head_reads.recorded_heads(record, history)
        document = head_reads.head_view(
            reference, str(run_id or ""), heads, self._heads_root(data_dir), observed_at=sources.isoformat(now)
        )
        if document is None:
            raise HeadRunNotFound(f"card {reference} recorded no head run {str(run_id or '')[:80]!r}")
        return document

    def _tail(
        self, ref: str, data_dir: Path, *, limit: int, now: float
    ) -> tuple[EventPage, tuple[dict[str, Any], ...] | None]:
        """The opening page of a card's history, plus the whole history (for head runs), from one traversal.

        A backend that cannot be established blanks only this section.
        """
        try:
            reader = self._events(data_dir)
        except Exception as exc:  # noqa: BLE001 -- confined to this source read
            return self._unselected(ref, None, exc, data_dir, now=now), None
        return reader.tail_with_history(ref, limit=limit, now=now)

    def _heads_root(self, data_dir: Path) -> Path:
        """Where local-pty run directories live: the dispatcher's own `local_pty_root`."""
        return data_dir / "heads"

    def _heads(
        self,
        ref: str,
        data_dir: Path,
        record: DispatcherRecord | None,
        record_source: sources.Source,
        history_source: sources.Source,
        history: tuple[dict[str, Any], ...] | None,
        *,
        now: float,
    ) -> dict[str, Any]:
        """The card's head runs, each with its state and whether it has a view.

        Read from the dispatcher record and the card's history; if either is unreadable the section
        says so and still lists what the other named.
        """
        missing = [
            source.reason or "unreadable"
            for source in (record_source, history_source)
            if source.state != sources.AVAILABLE
        ]
        heads = head_reads.recorded_heads(record, history)
        items = head_reads.head_rows(ref, heads, self._heads_root(data_dir))
        source = (
            sources.unavailable(
                "this card's head runs may be incomplete: " + "; ".join(str(reason) for reason in missing),
                now=now,
            )
            if missing
            else sources.available(now)
        )
        return {"source": source.to_json(), "items": items}

    def _card_exists(self, ref: str) -> None:
        """Refuse a card the board does not hold, so a view is never answered for a card that is not."""
        try:
            TaskReader(self._client()).show(ref)
        except TaskError as exc:
            if exc.code == "not_found":
                raise TaskNotFound(f"the board holds no card {ref!r}") from None
            raise InstallationUnavailable(f"the board could not be read: {exc.message}") from None
        except Exception as exc:  # noqa: BLE001 -- confined to this source read
            raise InstallationUnavailable(f"the board could not be read: {_reason(exc)}") from None

    # -- sections --------------------------------------------------------------------------

    def _system_health(self, report: InstanceReport, data_dir: Path, *, now: float) -> dict[str, Any]:
        """The health section: the shared :meth:`health_snapshot` reading if there is one, else collected."""
        if self._health_reader is None:
            return self._health(report, data_dir, now=now)
        try:
            section = self._health_reader().get("health")
        except Exception as exc:  # noqa: BLE001 -- confined to this source read
            reason = exc.message if isinstance(exc, ReadError) else str(exc)
            section = {
                "source": sources.unavailable(
                    f"installation health could not be collected: {reason}",
                    now=now,
                    evidence=self._production_path(data_dir),
                ).to_json(),
                "status": None,
            }
        return section

    def _health(self, report: InstanceReport, data_dir: Path, *, now: float) -> dict[str, Any]:
        """Installation health from `ummanu status`'s own collector; never a second health model."""
        try:
            status = self._read_status(report)
        except Exception as exc:  # noqa: BLE001 -- confined to this source read
            return {
                "source": sources.unavailable(
                    f"installation health could not be collected: {exc}",
                    now=now,
                    evidence=self._production_path(data_dir),
                ).to_json(),
                "status": None,
            }
        return {"source": sources.available(now).to_json(), "status": health_summary(status)}

    def _read_status(self, report: InstanceReport) -> dict[str, Any]:
        """`ummanu status`'s collector for the host only: sprints and runtime panels are read elsewhere.

        Liveness comes from the `agents` section and sprints from `sprint_reads.sprint_list`; reading
        them here made a dashboard read slow.
        """
        if self._status_reader is not None:
            return self._status_reader()
        return collect_status(
            report,
            offline=self.offline,
            sprint_client=self._board_client,
            sprints=False,
            probe_panels=False,
        )

    def _projects(self, report: InstanceReport, *, now: float) -> dict[str, Any]:
        """The registered projects, from the bindings the instance already validates."""
        items = [
            {
                "id": _text(binding.get("id")),
                "repo": _text(binding.get("repo")) or None,
                "remote": _text(binding.get("remote")) or None,
                "adapter": _text(binding.get("adapter")) or None,
                "default_branch": _text(binding.get("default_branch")) or None,
                "plane": _text(binding.get("plane")) or None,
                "enabled": bool(binding.get("enabled", True)),
            }
            for binding in report.bindings
            if isinstance(binding, dict) and _text(binding.get("id"))
        ]
        return {
            "source": sources.available(now).to_json(),
            "items": sorted(items, key=lambda project: project["id"]),
        }

    def _project_of(self, report: InstanceReport, project: str | None) -> dict[str, Any]:
        """A card's project, and whether this installation has it registered."""
        if not project:
            return {"id": None, "registered": False, "binding": None}
        for binding in report.bindings:
            if isinstance(binding, dict) and _text(binding.get("id")) == project:
                return {
                    "id": project,
                    "registered": True,
                    "binding": {
                        "repo": _text(binding.get("repo")) or None,
                        "adapter": _text(binding.get("adapter")) or None,
                        "default_branch": _text(binding.get("default_branch")) or None,
                        "enabled": bool(binding.get("enabled", True)),
                    },
                }
        return {"id": project, "registered": False, "binding": None}

    def _tasks(self, data_dir: Path, *, now: float) -> dict[str, Any]:
        """The cards the pipeline is carrying, as `ummanu task list` reads them."""
        try:
            rows = TaskReader(self._client()).list(states=set(CURRENT_TASK_STATES))
        except Exception as exc:  # noqa: BLE001 -- confined to this source read
            return {
                "source": sources.unavailable(
                    f"the board could not be read: {_reason(exc)}",
                    now=now,
                    evidence=data_dir / "board" / "cards.ndjson",
                ).to_json(),
                "items": [],
            }
        return {"source": sources.available(now).to_json(), "items": [_task_row(row) for row in rows]}

    def _records(self, data_dir: Path) -> dict[str, DispatcherRecord]:
        payload = json.loads(self._production_path(data_dir).read_text(encoding="utf-8"))
        records = payload.get("records") if isinstance(payload, dict) else None
        if not isinstance(records, dict):
            # An unrecognised state file proves nothing about the agents: report the source unavailable.
            raise TypeError("the dispatcher production state carries no records object")
        return {
            reference: DispatcherRecord.from_json(record)
            for reference, record in sorted(records.items())
            if isinstance(reference, str) and isinstance(record, dict)
        }

    def _agents(self, data_dir: Path, *, now: float, projects_by_ref: dict[str, str]) -> dict[str, Any]:
        """Every head the dispatcher holds, with liveness from process state.

        An unreadable production state is an unavailable source, never an empty list.
        """
        reading = read_source(
            "agents",
            lambda: self._records(data_dir),
            refusal=lambda exc: f"the dispatcher production state could not be read: {exc}",
            now=now,
            evidence=self._production_path(data_dir),
        )
        if not reading.answered:
            return {
                "source": reading.source.to_json(),
                "items": [],
            }
        items: list[dict[str, Any]] = []
        for reference, record in reading.value.items():
            for row in agent_reads.agent_rows(record, reference):
                row["project"] = projects_by_ref.get(reference)
                items.append(row)
        return {"source": sources.available(now).to_json(), "items": items}

    def _card(self, ref: str, data_dir: Path, *, now: float) -> tuple[dict[str, Any] | None, sources.Source]:
        try:
            return TaskReader(self._client()).show(ref), sources.available(now)
        except TaskError as exc:
            if exc.code == "not_found":
                raise TaskNotFound(f"the board holds no card {ref!r}") from None
            return None, sources.unavailable(
                f"the board could not be read: {exc.message}",
                now=now,
                evidence=data_dir / "board" / "cards.ndjson",
            )
        except Exception as exc:  # noqa: BLE001 -- confined to this source read
            return None, sources.unavailable(
                f"the board could not be read: {_reason(exc)}",
                now=now,
                evidence=data_dir / "board" / "cards.ndjson",
            )

    def _attempt(
        self, ref: str, data_dir: Path, *, now: float
    ) -> tuple[dict[str, Any] | None, DispatcherRecord | None, sources.Source]:
        """What the dispatcher durably holds for this card, if it holds anything."""
        reading = read_source(
            "attempt",
            lambda: self._records(data_dir).get(ref),
            refusal=lambda exc: f"the dispatcher production state could not be read: {exc}",
            now=now,
            evidence=self._production_path(data_dir),
        )
        if not reading.answered:
            return None, None, reading.source
        record = reading.value
        if record is None:
            return None, None, sources.available(now)
        return (
            {
                "state": record.state or None,
                "attempt_id": record.attempt_id or None,
                "attempt_round": record.attempt_round,
                "report_generation": record.report_generation,
                "gate_state": record.gate_state or None,
                "workspace": record.workspace or None,
                "head": record.head or None,
                "review_head": record.review_head or None,
                "paused": {
                    "worker": record.paused_worker_at > 0,
                    "reviewer": record.paused_reviewer_at > 0,
                },
            },
            record,
            sources.available(now),
        )


def _events_document(ref: str, page: EventPage, *, now: float, cursor: str | None) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "task_events",
        "observed_at": sources.isoformat(now),
        "ref": ref,
        "source": page.source.to_json(),
        "cursor": cursor or None,
        "next_cursor": page.next_cursor.encode(),
        "has_more": page.has_more,
        "items": list(page.items),
    }


def _projects_by_ref(rows: Iterable[dict[str, Any]]) -> dict[str, str]:
    return {row["ref"]: row["project"] for row in rows if row.get("ref") and row.get("project")}


def _task_row(row: dict[str, Any]) -> dict[str, Any]:
    claim = row.get("claim") if isinstance(row.get("claim"), dict) else {}
    audit = row.get("audit") if isinstance(row.get("audit"), dict) else {}
    return {
        "ref": _text(row.get("ref")),
        "title": _text(row.get("title")),
        "state": _text(row.get("state")),
        "project": _text(row.get("project")) or None,
        "sprint": row.get("sprint"),
        "type": _text(row.get("type")) or None,
        "blocked_by": row.get("blocked_by"),
        "claimed_by": claim.get("worker"),
        "created_at": audit.get("created_at"),
        "updated_at": audit.get("updated_at"),
    }


def _card_value(card: dict[str, Any] | None) -> dict[str, Any] | None:
    if card is None:
        return None
    value = _task_row(card)
    value["description"] = _text(card.get("description"))
    value["closed"] = bool(card.get("closed"))
    value["workspace"] = card.get("workspace")
    value["routing"] = card.get("routing")
    # A decision/operation card the PO handed to the owner: `{since, reason, by}`, else null.
    value["waiting_owner"] = waiting_owner(card)
    # The production an operation card touches (`none` included), else null.
    value["touches_production"] = touches_production(card)
    # The `task show` blocks: PO delegation origin and returns, wait target/outcome, e2e runs,
    # PO execution. Null when absent.
    for block in ("origin", "wait", "e2e", "po_execution"):
        value[block] = card.get(block) if isinstance(card.get(block), dict) else None
    return value


def _work(card: dict[str, Any] | None) -> dict[str, Any]:
    """The worker's report, the reviewer's verdict, the observer's decision, and the result.

    All from the card's marker comments (`ummanu.board.events.render_marker_comment`), never a
    transcript, pane or log. Null means the round has not produced that answer yet.
    """
    empty = {"worker_report": None, "review_verdict": None, "decision": None, "outcome": None}
    if card is None:
        return empty
    comments = card.get("comments")
    if not isinstance(comments, list):
        return empty
    found: dict[str, dict[str, Any]] = {}
    latest: dict[str, Any] | None = None
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        marker = comment.get("marker")
        if not isinstance(marker, str) or ":" not in marker:
            continue
        family, _, value = marker.partition(":")
        slot = {"report": "worker_report", "review": "review_verdict", "decision": "decision"}.get(family)
        if slot is None:
            continue
        entry = {
            "marker": marker,
            "value": value,
            "at": comment.get("created_at"),
            "body": _text(comment.get("body")),
            "classification": _classification(_text(comment.get("body"))),
        }
        found[slot] = entry
        latest = {"kind": family, "value": value, "at": entry["at"]}
    result = dict(empty)
    result.update(found)
    # The outcome is the card's latest answer of any kind; only `terminal` (state `done`) says it is finished.
    if latest is not None:
        latest["terminal"] = _text(card.get("state")) == "done"
        result["outcome"] = latest
    return result


def _classification(body: str) -> str | None:
    """The blocked classification a report marker carries on its own line, when it carries one."""
    for line in body.splitlines():
        if line.startswith("classification:"):
            return line.partition(":")[2].strip() or None
    return None


def _reason(exc: Exception) -> str:
    return getattr(exc, "message", None) or str(exc) or type(exc).__name__


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


# -- installation health, summarized ---------------------------------------------------------------

#: The severity of every problem code, and the whole colour rule: red if any red problem, else yellow
#: if any yellow, else green. Codes are classified here, beside :func:`health_summary`, never by wording.
PROBLEM_SEVERITY: dict[str, str] = {
    # Red: the installation cannot be trusted to run work, or its health is unknown.
    "unit.failed": "red",
    "unit.missing": "red",
    "checkpoint.blocked": "red",
    "checkpoint.last_failed": "red",
    # No checkpoint has reached the remote for longer than the 30-minute RPO (`rpo_problem`).
    "checkpoint.rpo_exceeded": "red",
    # The snapshot branch holds a commit the exporter did not make (`snapshot_foreign_commits`).
    "snapshot.foreign_commit": "red",
    "secret_store.key_unusable": "red",
    # Minted by the reader of this summary: unreadable health is not an absence of problems.
    "health.unreadable": "red",
    "doctor.collection_stuck": "red",
    # Yellow: the installation is running, but a person should look.
    "pipeline.paused": "yellow",
    "dispatcher.divergences_open": "yellow",
    "host.inventory_unreadable": "yellow",
    "memory.index_missing": "yellow",
    # A subscription resource is red (ummanu-108): its roles run on the other family meanwhile.
    "provider.red": "yellow",
}

#: The code `ummanu doctor` reports a checkpoint past its RPO under, classified above.
CHECKPOINT_RPO_EXCEEDED = "checkpoint.rpo_exceeded"

#: The code `ummanu doctor` reports foreign history on the snapshot branch under, classified above.
SNAPSHOT_FOREIGN_COMMIT = "snapshot.foreign_commit"

#: Deliberately not green: a code nobody classified must still show as something to look at.
UNCLASSIFIED_SEVERITY = "yellow"


def problem_severity(code: str) -> str:
    """The severity of one problem code, keyed on the code and never on its sentence."""
    return PROBLEM_SEVERITY.get(str(code), UNCLASSIFIED_SEVERITY)


def lamp_colour(findings: Iterable[dict[str, Any]]) -> str:
    """Red if anything red is present, else yellow if anything yellow is, else green."""
    severities = {problem_severity(_text(finding.get("code"))) for finding in findings
                  if finding.get("source") == "status" or not accepted(finding)}
    if "red" in severities:
        return "red"
    if severities:
        return "yellow"
    return "green"


def health_summary(status: dict[str, Any]) -> dict[str, Any]:
    """The operator's view of `collect_status`: what is wrong, by name, over the same facts.

    Not a second health model: every problem is a field the collector already marks as a failure,
    restated as a sentence plus a stable code (:data:`PROBLEM_SEVERITY`) that decides the colour.
    Unjudged values (disk, load, lag) are carried as data, never turned into problems here.
    `state` and `problems` keep their shape.
    """
    installation = _object(status.get("installation"))
    host = _object(status.get("host"))
    dispatcher = _object(status.get("dispatcher"))
    checkpoint = _object(status.get("checkpoint"))
    pause = _object(dispatcher.get("pause"))
    findings: list[dict[str, str]] = []
    units: list[dict[str, Any]] = []

    def found(code: str, message: str) -> None:
        """One problem: the sentence a person reads and the code the colour rule keys on."""
        findings.append({"code": code, "message": message, "severity": problem_severity(code)})

    for unit in host.get("units") or []:
        if not isinstance(unit, dict):
            continue
        name = _text(unit.get("name"))
        units.append(
            {
                "name": name,
                "kind": _text(unit.get("kind")) or None,
                "present": unit.get("present"),
                "enabled": unit.get("enabled"),
                "active": unit.get("active"),
            }
        )
        if unit.get("present") is False:
            found("unit.missing", f"{name} is not installed on this host")
        elif _text(unit.get("active")) == "failed":
            found("unit.failed", f"{name} is failed")
    for name, error in sorted(_object(host.get("inventory_errors")).items()):
        found("host.inventory_unreadable", f"the host inventory could not read {name}: {error}")
    if pause.get("paused"):
        found(
            "pipeline.paused",
            f"the pipeline is paused ({_text(pause.get('mode')) or 'unknown mode'})",
        )
    divergences = _object(dispatcher.get("divergences"))
    if int(divergences.get("open_count") or 0) > 0:
        found(
            "dispatcher.divergences_open",
            f"{int(divergences.get('open_count') or 0)} dispatcher divergence(s) are open",
        )
    if _text(checkpoint.get("blocked_reason")):
        found("checkpoint.blocked", f"the checkpoint is blocked: {_text(checkpoint.get('blocked_reason'))}")
    if _text(checkpoint.get("checkpoint_status")) == "failed":
        found(
            "checkpoint.last_failed",
            "the last checkpoint failed"
            + (f": {_text(checkpoint.get('checkpoint_last_failure_reason'))}" if _text(checkpoint.get("checkpoint_last_failure_reason")) else ""),
        )
    if checkpoint.get("rpo_exceeded"):
        found(CHECKPOINT_RPO_EXCEEDED, rpo_problem(checkpoint))
    key = _object(_object(status.get("secret_store")).get("installation_key"))
    if key and not key.get("usable"):
        found("secret_store.key_unusable", "the secret store's installation key is not usable")
    memory = _object(status.get("memory"))
    if memory and memory.get("index_present") is False:
        found("memory.index_missing", "the memory index is missing")
    providers = _providers(status)
    for provider in providers:
        if provider["state"] not in ("ready", "unknown", "stale"):
            found(
                "provider.red",
                f"provider {provider['resource']} is {provider['state']}"
                + (f" until {provider['until']}" if provider["until"] else "")
                + (f": {provider['reason']}" if provider["reason"] else ""),
            )
    problems = [finding["message"] for finding in findings]
    return {
        "state": "ok" if not problems else "attention",
        "problems": problems,
        # The same sentences as `problems`, in order, each with its code (see :data:`PROBLEM_SEVERITY`).
        "findings": findings,
        "colour": lamp_colour(findings),
        "dispatcher": {
            "phase": _text(dispatcher.get("phase")) or None,
            "paused": bool(pause.get("paused")),
            "pause_mode": _text(pause.get("mode")) or None,
            "active_attempts": len(dispatcher.get("active_attempts") or []),
            "observers": len(dispatcher.get("observers") or []),
            "last_tick_finished_at": _text(_object(dispatcher.get("reconciliation")).get("last_tick_finished_at")) or None,
        },
        "units": units,
        "checkpoint": {
            "status": _text(checkpoint.get("checkpoint_status")) or None,
            "lag_minutes": checkpoint.get("lag_minutes"),
            "lag_commits": checkpoint.get("lag_commits"),
            "blocked_reason": _text(checkpoint.get("blocked_reason")) or None,
            "next_due_at": _text(checkpoint.get("checkpoint_next_due_at")) or None,
        },
        "resources": _object(host.get("resources")),
        # Each subscription resource's state and when a red one comes back (ummanu-108).
        "providers": providers,
        "cards": _object(installation.get("cards")),
        "memory": {
            "fact_count": memory.get("fact_count"),
            "last_reindex_at": _text(memory.get("last_reindex_at")) or None,
        },
    }


def _providers(status: dict[str, Any]) -> list[dict[str, Any]]:
    """The head resources `collect_status` read from the dispatcher's health cache, with expiry."""
    rows = _object(status.get("recovery")).get("resources")
    return [
        {
            "resource": _text(row.get("resource")),
            "state": _text(row.get("state")) or "unknown",
            "until": _text(row.get("until")) or None,
            "reason": _text(row.get("reason")),
        }
        for row in (rows if isinstance(rows, list) else [])
        if isinstance(row, dict) and _text(row.get("resource"))
    ]


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
