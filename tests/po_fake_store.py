"""An in-memory `PoStore` for unit tests of the PO service: no PostgreSQL, no Docker.

It keeps the store's contract where the service and the runner rely on it: one `running` turn per
session (`TurnInProgress`), request ids bound to one operation and its inputs (`RequestConflict`,
replay before anything else), closed sessions taking no turn (`SessionClosed`), and every settle
conditional on the turn still running. `FakeBoard` is the database; each `FakePoStore` is one process's
connection to it, and `crash()` makes that connection refuse everything, the way a killed service stops
writing, while the board stays for the next process.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from ummanu.po.sprints import SprintRecord, WhyDocument
from ummanu.po.store import (
    AGENT,
    COMPLETED,
    DEFAULT_EFFORT,
    FAILED,
    INTERRUPTED,
    OWNER,
    REQUEST_OPERATIONS,
    RUNNING,
    SEND,
    SESSION_CLOSED,
    SESSION_CREATE,
    SESSION_OPEN,
    FeedEntry,
    PoRequest,
    PoStoreError,
    RequestConflict,
    Session,
    SessionClosed,
    SessionNotFound,
    Turn,
    TurnInProgress,
    send_fingerprint,
    session_fingerprint,
    session_title,
)
from ummanu.tasks import admit_role


class FakeBoard:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.sessions: dict[str, Session] = {}
        self.turns: dict[tuple[str, int], Turn] = {}
        self.feed: list[FeedEntry] = []
        self.requests: dict[str, PoRequest] = {}
        self._clock = datetime(2026, 9, 26, tzinfo=UTC)

    def now(self) -> datetime:
        # Strictly increasing, so "finished before started" is a comparison a test can make.
        self._clock += timedelta(milliseconds=1)
        return self._clock


class FakePoStore:
    def __init__(self, board: FakeBoard | None = None) -> None:
        self.board = board or FakeBoard()
        self.dead = False

    def crash(self) -> None:
        self.dead = True

    def _open(self) -> FakeBoard:
        if self.dead:
            raise PoStoreError("this process is gone")
        return self.board

    # --- request ids ------------------------------------------------------------------------

    def request(self, request_id: str) -> PoRequest | None:
        board = self._open()
        with board.lock:
            return board.requests.get(request_id)

    def turn_request_id(self, session_id: str, seq: int) -> str | None:
        board = self._open()
        with board.lock:
            return next(
                (
                    known.request_id
                    for known in board.requests.values()
                    if (known.session_id, known.seq, known.operation) == (session_id, seq, SEND)
                ),
                None,
            )

    def _known(self, board: FakeBoard, request_id: str | None, operation: str, fingerprint: str):
        if request_id is None or request_id not in board.requests:
            return None
        known = board.requests[request_id]
        if (known.operation, known.fingerprint) != (operation, fingerprint):
            raise RequestConflict(
                f"request id {request_id!r} already belongs to another {known.operation} request"
            )
        return known

    @staticmethod
    def _check_operation(request_id: str | None, operation: str) -> None:
        """The CHECK `po_request_operation_in_vocabulary`, before anything is written.

        PostgreSQL refuses the request row and rolls back the whole transaction with it, so an
        operation missing from the vocabulary creates nothing here either.
        """
        if request_id is not None and operation not in REQUEST_OPERATIONS:
            raise PoStoreError(
                f"the board store refused a PO session write: po_requests.operation {operation!r} "
                "violates check constraint po_request_operation_in_vocabulary"
            )

    # --- sessions ---------------------------------------------------------------------------

    def claim_session(
        self,
        *,
        session_id: str,
        cli: str,
        model: str,
        cwd: str,
        cli_session_id: str | None,
        request_id: str | None = None,
        effort: str = DEFAULT_EFFORT,
        operation: str = SESSION_CREATE,
        fingerprint: str | None = None,
        title: str | None = None,
    ) -> tuple[Session, bool]:
        board = self._open()
        fingerprint = fingerprint or session_fingerprint(cli, model, effort)
        title = session_title(title)
        with board.lock:
            known = self._known(board, request_id, operation, fingerprint)
            if known is not None:
                return board.sessions[known.session_id], False
            self._check_operation(request_id, operation)
            session = Session(
                session_id,
                cli,
                model,
                cwd,
                board.now(),
                SESSION_OPEN,
                cli_session_id,
                effort=effort,
                title=title,
            )
            board.sessions[session_id] = session
            if request_id is not None:
                board.requests[request_id] = PoRequest(
                    request_id, operation, fingerprint, session_id, None, board.now()
                )
            return session, True

    def session(self, session_id: str) -> Session:
        board = self._open()
        with board.lock:
            if session_id not in board.sessions:
                raise SessionNotFound(f"there is no PO session {session_id}")
            return board.sessions[session_id]

    def sessions(self, state: str = SESSION_OPEN) -> list[Session]:
        board = self._open()
        with board.lock:
            found = []
            for session in board.sessions.values():
                if session.state != state:
                    continue
                first = next(
                    (e.text for e in board.feed if e.session_id == session.session_id and e.role == OWNER),
                    None,
                )
                found.append(replace(session, first_message=first, last_activity_at=session.created_at))
            return found

    def session_count(self, state: str) -> int:
        return len(self.sessions(state))

    def close_session(self, session_id: str, actor: str) -> Session:
        board = self._open()
        with board.lock:
            session = self.session(session_id)
            if session.state == SESSION_CLOSED:
                return session
            if any(t.session_id == session_id and t.state == RUNNING for t in board.turns.values()):
                raise TurnInProgress(f"a turn is still running in PO session {session_id}")
            closed = replace(session, state=SESSION_CLOSED, closed_at=board.now(), closed_by=actor)
            board.sessions[session_id] = closed
            return closed

    def set_title(self, session_id: str, title: str | None) -> Session:
        board = self._open()
        title = session_title(title)
        with board.lock:
            session = replace(self.session(session_id), title=title)
            board.sessions[session_id] = session
            return session

    def set_cli_session_id(self, session_id: str, cli_session_id: str) -> bool:
        board = self._open()
        with board.lock:
            session = board.sessions[session_id]
            if session.cli_session_id:
                return False
            board.sessions[session_id] = replace(session, cli_session_id=cli_session_id)
            return True

    # --- turns ------------------------------------------------------------------------------

    def claim_turn(
        self,
        session_id: str,
        text: str,
        stdout_path,
        *,
        request_id: str | None = None,
        card: Any = None,
        prompt: str | None = None,
        metadata: Any = None,
    ) -> tuple[Turn, bool]:
        board = self._open()
        with board.lock:
            known = self._known(board, request_id, SEND, send_fingerprint(session_id, text, card))
            if known is not None:
                return board.turns[(known.session_id, known.seq)], False
            self._check_operation(request_id, SEND)
            session = self.session(session_id)
            if session.state == SESSION_CLOSED:
                raise SessionClosed(f"PO session {session_id} is closed; open a new session to continue")
            turns = [t for t in board.turns.values() if t.session_id == session_id]
            if any(t.state == RUNNING for t in turns):
                raise TurnInProgress(f"a turn is still running in PO session {session_id}")
            seq = max((t.seq for t in turns), default=0) + 1
            turn = Turn(session_id, seq, board.now(), None, RUNNING, str(stdout_path(seq)), None, None, None)
            board.turns[(session_id, seq)] = turn
            board.feed.append(
                FeedEntry(len(board.feed) + 1, session_id, seq, OWNER, text if prompt is None else prompt, board.now(), metadata)
            )
            if request_id is not None:
                board.requests[request_id] = PoRequest(
                    request_id, SEND, send_fingerprint(session_id, text, card), session_id, seq, board.now()
                )
            return turn, True

    def _update_running(self, session_id: str, seq: int, **changes: Any) -> bool:
        board = self._open()
        with board.lock:
            turn = board.turns.get((session_id, seq))
            if turn is None or turn.state != RUNNING:
                return False
            board.turns[(session_id, seq)] = replace(turn, **changes)
            return True

    def record_process(self, session_id: str, seq: int, pid: int, identity: str | None) -> bool:
        return self._update_running(session_id, seq, pid=pid, process_identity=identity)

    def mark_rerun(self, session_id: str, seq: int, reason: str) -> bool:
        board = self._open()
        with board.lock:
            turn = board.turns.get((session_id, seq))
            if turn is None or turn.state != RUNNING or turn.reason is not None:
                return False
            board.turns[(session_id, seq)] = replace(turn, reason=reason, pid=None, process_identity=None)
            return True

    def complete_turn(
        self, session_id: str, seq: int, answer: str, *, resolved_model: str | None = None
    ) -> bool:
        board = self._open()
        with board.lock:
            done = self._update_running(
                session_id, seq, state=COMPLETED, finished_at=board.now(), resolved_model=resolved_model
            )
            if done:
                board.feed.append(FeedEntry(len(board.feed) + 1, session_id, seq, AGENT, answer, board.now()))
            return done

    def finish_turn(
        self, session_id: str, seq: int, state: str, reason: str, *, resolved_model: str | None = None
    ) -> bool:
        if state not in (FAILED, INTERRUPTED):
            raise ValueError(state)
        board = self._open()
        with board.lock:
            return self._update_running(
                session_id,
                seq,
                state=state,
                finished_at=board.now(),
                reason=reason,
                resolved_model=resolved_model,
            )

    def turn(self, session_id: str, seq: int) -> Turn:
        board = self._open()
        with board.lock:
            if (session_id, seq) not in board.turns:
                raise PoStoreError(f"there is no turn {seq} in PO session {session_id}")
            return board.turns[(session_id, seq)]

    def turns(self, session_id: str) -> list[Turn]:
        board = self._open()
        with board.lock:
            return sorted(
                (t for t in board.turns.values() if t.session_id == session_id), key=lambda t: t.seq
            )

    def running_turns(self, session_id: str | None = None) -> list[Turn]:
        board = self._open()
        with board.lock:
            return sorted(
                (
                    t
                    for t in board.turns.values()
                    if t.state == RUNNING and (session_id is None or t.session_id == session_id)
                ),
                key=lambda t: (t.started_at, t.session_id),
            )

    def feed(self, session_id: str) -> list[FeedEntry]:
        board = self._open()
        with board.lock:
            return [entry for entry in board.feed if entry.session_id == session_id]


class FakeSprints:
    """The resolver's view of the sprints (`ummanu.po.sprints.SprintSessions`), in memory.

    Every sprint is open unless `status` says otherwise, and allows the productions `allowed` names
    (none by default). A comment and a session record are kept by their request id and a repeat of one
    changes nothing, as the sprint audit does. `fail` makes the next call of a method raise, as a board
    that dropped the connection would. It also stands in for the sprint writer of `sprint
    allow-production` (`allow_production`), so a test can play the PO recording an allowance.
    """

    def __init__(
        self,
        sessions: dict[str, str | None],
        *,
        status: dict[str, str] | None = None,
        documents: dict[str, list[WhyDocument]] | None = None,
        allowed: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        self.records = {
            ref: SprintRecord(ref, (status or {}).get(ref, "open"), session, (allowed or {}).get(ref, ()))
            for ref, session in sessions.items()
        }
        # The sprint audit's `production_allowed` events (`allow_production`), in order.
        self.events: list[dict[str, Any]] = []
        self.documents = dict(documents or {})
        self.comments: dict[str, tuple[str, str]] = {}
        self.recorded: dict[str, tuple[str, str]] = {}
        self.fail: dict[str, int] = {}
        self.lock = threading.Lock()

    def _maybe_fail(self, method: str) -> None:
        if self.fail.get(method):
            self.fail[method] -= 1
            raise RuntimeError(f"the board dropped the connection during {method}")

    def sprint(self, sprint_ref: str) -> SprintRecord | None:
        with self.lock:
            self._maybe_fail("sprint")
            return self.records.get(sprint_ref)

    def allow_production(
        self, *, role: str, actor: str, reference: str, project: str, reason: str, request_id: str | None
    ) -> dict[str, Any]:
        """What `SprintWriter.allow_production` leaves, as `sprint allow-production` reaches it.

        The writer's own rules (role, registry, status, request id) are its unit and PostgreSQL tests'
        subject; this keeps the result: the project appended once and one `production_allowed` event.
        """
        with self.lock:
            admit_role(role, actor, {"po"})
            record = self.records[reference]
            known = next((event for event in self.events if event["request_id"] == request_id), None)
            if known is not None:
                return {"action": "production_allowed", "event_id": known["event_id"]}
            if project in record.allowed_productions:
                return {"action": "already_allowed", "event_id": None}
            self.records[reference] = replace(record, allowed_productions=(*record.allowed_productions, project))
            event = {
                "event_id": f"evt-allow-{len(self.events) + 1}",
                "request_id": request_id,
                "kind": "production_allowed",
                "ref": reference,
                "actor": {"role": role, "id": actor},
                "payload": {"project": project, "reason": reason},
            }
            self.events.append(event)
            return {"action": "production_allowed", "event_id": event["event_id"]}

    def why_documents(self, sprint_ref: str) -> list[WhyDocument]:
        return list(self.documents.get(sprint_ref, []))

    def comment(self, sprint_ref: str, body: str, *, request_id: str) -> None:
        with self.lock:
            self._maybe_fail("comment")
            self.comments.setdefault(request_id, (sprint_ref, body))

    def record_po_session(self, sprint_ref: str, session_id: str, *, request_id: str) -> None:
        with self.lock:
            self._maybe_fail("record_po_session")
            if request_id in self.recorded:
                return
            self.recorded[request_id] = (sprint_ref, session_id)
            self.records[sprint_ref] = replace(self.records[sprint_ref], po_session=session_id)
