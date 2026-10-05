"""The read operations every transport of this installation answers from.

`system_snapshot`, `task_snapshot` and `task_events` are the surface every transport shares. A
dashboard is one system snapshot; a card page is one task snapshot plus a cursor it polls; a
Telegram head asking "what is running" is the same system snapshot rendered as a message.
`head_view` is the one read added for a need that arrived (secretary-1703): what one of a card's
local-pty heads printed and journalled, without Orca (:mod:`ummanu.webproto.head_view`). No
operation here writes anything at all.

Everything below is assembled from sources that already exist and already own their meaning --
`collect_status` for installation health, the validated project bindings for the registry,
`TaskReader` for cards, the audit owner of the installation's own card client for history, the
dispatcher's durable production state plus the launch heartbeats for agents. Nothing here is a
second collector of a fact somebody already collects, and nothing here writes: no dispatcher tick,
no repair, no board mutation, not even a cache file.

The sources fail independently, so each section of a snapshot carries its own availability record
(:mod:`ummanu.webproto.sources`) instead of the whole read failing. A dead board store must not
blank out the agent list, and an unreadable dispatcher state must not hide the cards.
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
from ummanu.dispatch.types import HostError
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

SCHEMA_VERSION = 1

#: The states a card is "current" in: everything the pipeline is carrying right now. `issues` is
#: the backlog and `done` is history, and a dashboard that mixed either into "current tasks" would
#: report a thousand-card board as a thousand cards in flight.
CURRENT_TASK_STATES = ("ready", "in_progress", "validate", "assessment", "blocked")

#: How many of a card's most recent events its task snapshot opens with.
TASK_SNAPSHOT_EVENTS = 20

#: Failures a source read may answer with instead of a value. They are caught per section, never
#: around the whole snapshot: a section that fails records why, and the rest of the read continues.
_SOURCE_FAILURES = (TaskError, HostError, OSError, ValueError, KeyError, TypeError, AssertionError)


def hold_store_exclusion(instance: str | Path) -> str | None:
    """Run the board store's exclusion guard once for this process; the refusal, if it refused.

    For a long-lived reader, called before it serves: from then on every read of the store in this
    process resolves it without running the guard again (:func:`ummanu.board.store.hold_exclusion`). A refusal is held as well, and is returned here
    so the caller can say it once; the reads that follow answer with it.
    """
    from ummanu.board.store import BoardStoreError, hold_exclusion

    try:
        hold_exclusion(instance)
    except BoardStoreError as refused:
        return str(refused)
    return None


class ReadLayer(ProtocolBoundary):
    """One installation, read three ways, with no knowledge of who is asking.

    Construction is cheap and does no I/O: every operation reads what it needs when it is called,
    so a long-lived transport holding one of these never serves a value it cached at start-up.

    ``board_client`` and ``status_reader`` exist so a test -- or a transport with its own
    connection policy -- can supply those two sources directly. Neither is a mode: the same code
    path runs with the live client as with a fake one.

    ``health_reader`` is where :meth:`system_snapshot` takes its health section from instead of
    collecting it: a transport that already holds a cached reading of :meth:`health_snapshot` --
    the web process's doctor lamp -- hands that reading in, so the dashboard's panel and the lamp
    are one collection in one window rather than two answers to one question. Without it, the
    section is collected on every call, as before.
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
        """The validated instance, or a refusal naming what does not validate.

        An instance whose config is invalid is not a source outage that a snapshot can report
        around: without it there is no data directory to read anything else from, and a snapshot
        built on a guess would be a fabrication.
        """
        report = validate_instance(self.instance)
        if not report.ok or report.data_dir is None:
            raise InstallationUnavailable(
                "this instance config does not validate: "
                + "; ".join(str(error) for error in report.errors[:5])
            )
        return report

    def data_dir(self, report: InstanceReport | None = None) -> Path:
        """The data plane this installation reads from, overridden explicitly or taken from config.

        Every operation resolves the config exactly once and threads the result through its
        sections, so one snapshot cannot be assembled half from a config read before an edit and
        half from one read after it.
        """
        if self._data_dir is not None:
            return self._data_dir
        report = report if report is not None else self.report()
        assert report.data_dir is not None
        return report.data_dir

    def _client(self) -> Any:
        """The board client of this installation: an injected one, or the switch's (§2.2).

        Resolved once per layer and kept, so one operation cannot be assembled half from one
        backend's client and half from another's -- and so a read that consults the card and its
        history asks the switch a single time. A construction that failed is not kept: the next
        operation asks again rather than replaying a refusal.
        """
        if self._resolved_client is None:
            self._resolved_client = self._board_client or board_client(
                self.instance.parent if self.instance.is_file() else self.instance, serves=(CARD,)
            )
        return self._resolved_client

    def _events(self, data_dir: Path) -> CommittedAudit:
        """The reader of this card's history.

        The one place this layer decides where a card's events come from, and it decides it the way
        every other live audit reader of this installation does: resolve the card client, ask
        :func:`ummanu.tasks.task_audit_for` for that client's audit owner, and page that owner's
        ordered traversal (`requests`/`board_events`, `docs/BOARD_STORE.md` §7.3). The file
        projection under `<data>/board` is not consulted at all.

        Until this existed, `task_snapshot` and `task_events` opened `board/events.ndjson` whatever
        the installation was, so a migrated one answered a card's history from a file its writers do
        not touch: unavailable where the projection was swept, and a successful empty or stale page
        where an old one was left behind -- with every committed record, a product run's included,
        invisible.
        """
        return CommittedAudit(task_audit_for(self._client(), data_dir))

    def _unselected(
        self, ref: str, cursor: str | None, exc: Exception, data_dir: Path, *, now: float
    ) -> EventPage:
        """A card backend that could not be established, said as the source fact it is.

        Not an empty history and not a fall back to the file: which store holds this card's events is
        the client's answer, and without a client there is no answer to read. The cursor the caller
        came with is handed back untouched when it parses at all, so a client that keeps polling
        resumes where it stopped once the backend answers again.
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
        """Installation health alone: the section `system_snapshot` carries, without the rest.

        The same `_health` call over the same recorded state, so the dashboard's panel and the
        doctor lamp cannot answer the question differently. It is a separate operation only so that
        a reader who wants health does not pay for the projects, the cards and the agents too.
        """
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

        One `TaskReader.list` for the whole answer (secretary-1811): the listing, the metadata of its
        rows and their outbox returns are each one batched read, and nothing here reads per card. A
        card counts when its `origin.po_session` is this session (`delegated`) or when this session is
        the successor its results go to now (`current_session`, `inherited`). `items` is null, never
        `[]`, when the board could not be read.
        """
        now = self._clock()
        session = str(session_id or "")
        report = self.report()
        data_dir = self.data_dir(report)
        try:
            cards = TaskReader(self._client()).list()
        except _SOURCE_FAILURES as exc:
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

        The cursor is the whole contract: read with the ``next_cursor`` of a page and you get what
        was appended after it, exactly once; read the same cursor twice and you get the same page.
        Both hold because the position is a place in an append-only history rather than a time or a
        recomputed index -- see :mod:`ummanu.webproto.cursor`.

        The history is the card audit's (:meth:`_events`): an ordinal in the committed `requests`
        traversal. A cursor says what it measures, so a byte offset into the pre-2026-09-10 file journal --
        kept by a client across an installation's migration, say -- is refused by name instead of
        read as a position here, and the caller gets its continuation from a fresh
        :meth:`task_snapshot`.
        """
        now = self._clock()
        reference = str(ref or "")
        if not reference:
            raise TaskNotFound("a task reference is required")
        data_dir = self.data_dir()
        try:
            reader = self._events(data_dir)
        except _SOURCE_FAILURES as exc:
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

        `run_id` has to be one the card recorded -- the dispatcher's current worker or reviewer run,
        or a launch its own history names -- and any other is not found, whatever it looks like. Past
        that, nothing refuses: every source is read under a guard and a dead one is said as such
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
        except _SOURCE_FAILURES as exc:
            raise InstallationUnavailable(
                f"this card's history could not be read, so its head runs are not known: {_reason(exc)}"
            ) from None
        try:
            record = self._records(data_dir).get(reference)
        except _SOURCE_FAILURES as exc:
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
        """The opening page of a card's history, and the whole of it briefly, from one traversal.

        A backend that cannot be established takes this section away and nothing else, exactly as an
        unreadable journal does: a snapshot whose events are unavailable still carries the card, the
        project and the attempt. The whole history is what the card's head runs are read from.
        """
        try:
            reader = self._events(data_dir)
        except _SOURCE_FAILURES as exc:
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

        Read from the dispatcher record and the card's history; when either could not be read the
        section says so, and still lists what the other one named.
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
        except _SOURCE_FAILURES as exc:
            raise InstallationUnavailable(f"the board could not be read: {_reason(exc)}") from None

    # -- sections --------------------------------------------------------------------------

    def _system_health(self, report: InstanceReport, data_dir: Path, *, now: float) -> dict[str, Any]:
        """The snapshot's health section: the shared reading when there is one, else collected.

        The shared reading is a :meth:`health_snapshot`, so its section is this same `_health` over
        the same recorded state -- only collected when the holder's window says so, and dated by
        its own `source.observed_at` rather than by this snapshot's.
        """
        if self._health_reader is None:
            return self._health(report, data_dir, now=now)
        try:
            section = self._health_reader().get("health")
        except (ReadError, *_SOURCE_FAILURES) as exc:
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
        """Installation health, straight from `ummanu status`'s own collector.

        Deliberately not a second health model. `collect_status` already owns what "healthy" means
        for units, heads, sprints, checkpoint lag and host resources, and a dashboard that answered
        that question differently from `ummanu status --json` would make an operator debug the
        difference between two answers instead of the installation.
        """
        try:
            status = self._read_status(report)
        except _SOURCE_FAILURES as exc:
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
        """`ummanu status`'s own collector, asked for the host and not for every sprint.

        The sprints are not read here and the runtime panels are not probed: the snapshot's own
        `agents` section answers liveness from process state, and the sprint protocol
        (`sprint_reads.sprint_list`) answers the sprints. Reading them here as well is what made a
        live dashboard read take over ten seconds and carry the full status of every sprint the
        board has ever held.
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
        except _SOURCE_FAILURES as exc:
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
            # Reported as an unavailable source, like every other unreadable input here: a state
            # file whose shape this reader does not recognise proves nothing about the agents.
            raise TypeError("the dispatcher production state carries no records object")
        return {
            reference: DispatcherRecord.from_json(record)
            for reference, record in sorted(records.items())
            if isinstance(reference, str) and isinstance(record, dict)
        }

    def _agents(self, data_dir: Path, *, now: float, projects_by_ref: dict[str, str]) -> dict[str, Any]:
        """Every head the dispatcher holds, with its liveness taken from process state.

        An unreadable or absent production state is reported as an unavailable source rather than
        as an empty list: "the dispatcher is running nothing" and "nobody could tell me what the
        dispatcher is running" are opposite answers for an operator.
        """
        try:
            records = self._records(data_dir)
        except _SOURCE_FAILURES as exc:
            return {
                "source": sources.unavailable(
                    f"the dispatcher production state could not be read: {exc}",
                    now=now,
                    evidence=self._production_path(data_dir),
                ).to_json(),
                "items": [],
            }
        items: list[dict[str, Any]] = []
        for reference, record in records.items():
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
        except _SOURCE_FAILURES as exc:
            return None, sources.unavailable(
                f"the board could not be read: {_reason(exc)}",
                now=now,
                evidence=data_dir / "board" / "cards.ndjson",
            )

    def _attempt(
        self, ref: str, data_dir: Path, *, now: float
    ) -> tuple[dict[str, Any] | None, DispatcherRecord | None, sources.Source]:
        """What the dispatcher durably holds for this card, if it holds anything."""
        try:
            record = self._records(data_dir).get(ref)
        except _SOURCE_FAILURES as exc:
            return (
                None,
                None,
                sources.unavailable(
                    f"the dispatcher production state could not be read: {exc}",
                    now=now,
                    evidence=self._production_path(data_dir),
                ),
            )
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
    # The blocks `task show` carries (secretary-1811): the PO session a card was delegated from and
    # its returns, a wait card's target and outcome, a code card's e2e runs. Null for a card without.
    for block in ("origin", "wait", "e2e", "po_execution"):
        value[block] = card.get(block) if isinstance(card.get(block), dict) else None
    return value


def _work(card: dict[str, Any] | None) -> dict[str, Any]:
    """The worker's report, the reviewer's verdict, the observer's decision, and the result.

    All four come from the card's own marker comments, which are the protocol's public record of
    those events (`ummanu.board.events.render_marker_comment`) -- not from a transcript, a pane
    or a log file. A card with no marker of a kind has null there, which is different from an empty
    body: the round has not produced that answer yet.
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
    # The result of the work is the last answer the card has, whatever kind it is: a decision on an
    # assessed card, a verdict on one in review, a report on one still being validated. `terminal`
    # is the only thing that says the card is finished, and it comes from the card's state.
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

#: The severity every problem code carries, and the whole of the colour rule. A lamp is red if any
#: red problem is present, otherwise yellow if any yellow one is, otherwise green -- so the table
#: below, and not the wording of a sentence, is what decides a colour. A code is classified once,
#: here, beside the place the code is minted, so a problem added to :func:`health_summary` meets
#: this table in the same file rather than landing in a colour by accident.
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
    # Minted by the reader of this summary rather than here: health that could not be read at all
    # is not an absence of problems, so it carries a code of its own and the gravest severity.
    "health.unreadable": "red",
    "doctor.collection_stuck": "red",
    # Yellow: the installation is running, but a person should look.
    "pipeline.paused": "yellow",
    "dispatcher.divergences_open": "yellow",
    "host.inventory_unreadable": "yellow",
    "memory.index_missing": "yellow",
}

#: The code `ummanu doctor` reports a checkpoint past its RPO under, classified above.
CHECKPOINT_RPO_EXCEEDED = "checkpoint.rpo_exceeded"

#: The code `ummanu doctor` reports foreign history on the snapshot branch under, classified above.
SNAPSHOT_FOREIGN_COMMIT = "snapshot.foreign_commit"

#: What an unclassified code is worth. Deliberately not green: a problem somebody adds tomorrow and
#: forgets to classify must show as something to look at, never as a clean installation.
UNCLASSIFIED_SEVERITY = "yellow"


def problem_severity(code: str) -> str:
    """The severity of one problem code, keyed on the code and never on its sentence."""
    return PROBLEM_SEVERITY.get(str(code), UNCLASSIFIED_SEVERITY)


def lamp_colour(findings: Iterable[dict[str, Any]]) -> str:
    """Red if anything red is present, else yellow if anything yellow is, else green."""
    severities = {problem_severity(_text(finding.get("code"))) for finding in findings}
    if "red" in severities:
        return "red"
    if severities:
        return "yellow"
    return "green"


def health_summary(status: dict[str, Any]) -> dict[str, Any]:
    """The operator's view of `collect_status`: what is wrong, said by name, over the same facts.

    Not a second health model. Every problem listed here is a field the collector already marks as
    a failure -- a unit that failed or is missing, a paused pipeline, a checkpoint that is blocked
    or last failed, a store finding -- restated in a sentence, so a dashboard header can say "ok" or
    name what needs attention without a person reading the whole status document. No threshold is
    invented here: a value the collector reports without judging it (free disk, load, lag) is
    carried as data for the page to show, and is not a problem until the collector says so.

    Every problem also carries a stable code (:data:`PROBLEM_SEVERITY`), which is what a colour is
    decided from: a sentence is for a person to read and may be reworded, a code may not. `state`
    and `problems` keep their shape and meaning, so a reader written against them is unaffected.
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
    problems = [finding["message"] for finding in findings]
    return {
        "state": "ok" if not problems else "attention",
        "problems": problems,
        # Added beside `problems` rather than instead of it: the same sentences, in the same order,
        # each with the code a colour is decided from. See :data:`PROBLEM_SEVERITY`.
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
        "cards": _object(installation.get("cards")),
        "memory": {
            "fact_count": memory.get("fact_count"),
            "last_reindex_at": _text(memory.get("last_reindex_at")) or None,
        },
    }


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
