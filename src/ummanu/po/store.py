"""PO head sessions, turns, feed and request ids in the board store (revisions `0008_po_sessions`, `0009_po_requests`,
`0010_po_session_close`, `0015_po_effort_resolved_model`, `0019_po_session_title`).

One short connection per operation: the runner's waiter threads settle turns concurrently, and a
connection shared between them would serialize exactly what must not be serialized. Every state
change of a turn is conditional on the turn still being `running`, so a stop, a recovery and a
process exit racing each other settle a turn once, by whichever came first.

**Request ids.** A /po form's request id belongs to exactly one operation with fixed inputs,
installation-wide, and `po_requests` is the one place that says so. :meth:`PoStore.claim_session` and
:meth:`PoStore.claim_turn` decide it inside the transaction that creates the session or the turn, in a
fixed order: a known id with the same operation and inputs answers the recorded outcome and writes
nothing; a known id with anything else is :class:`RequestConflict`; an unknown id for a send while
another turn runs is :class:`TurnInProgress` and writes nothing; otherwise the session or the turn is
created together with its request row. Same-id transactions are serialized by an advisory lock taken
first, and the primary key on `request_id` backs it.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ummanu.board.schema_gate import SchemaAssessment, SchemaOwed, require

CLIS = ("claude", "codex")
# The reasoning effort that passes the CLI no effort flag: it runs with its own configured one.
DEFAULT_EFFORT = "default"
SESSION_OPEN = "open"
SESSION_CLOSED = "closed"

RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
INTERRUPTED = "interrupted"

OWNER = "owner"
AGENT = "agent"

SESSION_CREATE = "po_session_create"
SEND = "po_send"
# The PO service's resolver opening a fresh session for a sprint (`PoService.sprint_session`).
SPRINT_SESSION = "po_sprint_session"
# Every operation a `po_requests` row may record: the CHECK `po_request_operation_in_vocabulary`
# (board/schema.py, widened in 0016). A new operation joins it here and in a migration together.
REQUEST_OPERATIONS = (SESSION_CREATE, SEND, SPRINT_SESSION)

# The partial unique index that holds "at most one running turn per session".
ONE_RUNNING_INDEX = "po_turns_one_running_per_session"
# The two-key advisory lock namespace for PO request ids ("POR" + "Q"); the second key is the id's hash.
REQUEST_LOCK_CLASS = 0x504F5251


class PoStoreError(RuntimeError):
    """The PO session store refused or could not answer."""


class PoSchemaOwed(SchemaOwed, PoStoreError):
    """The board store owes migrations this build reads PO sessions through (`board.schema_gate`)."""

    def __init__(self, assessment: SchemaAssessment) -> None:
        self.assessment = assessment
        PoStoreError.__init__(self, assessment.describe())


class SessionNotFound(PoStoreError):
    pass


class TurnInProgress(PoStoreError):
    """A turn is already running in this session; nothing was written."""


class SessionClosed(PoStoreError):
    """The session is closed and takes no new turn; nothing was written."""


class RequestConflict(PoStoreError):
    """The request id already belongs to another operation or other inputs; nothing was written."""


class TitleRefused(PoStoreError):
    """The title is not one a session may carry (:func:`session_title`); nothing was written."""


@dataclass(frozen=True)
class Session:
    session_id: str
    cli: str
    model: str
    cwd: str
    created_at: datetime
    state: str
    cli_session_id: str | None
    # Set together by :meth:`PoStore.close_session`, exactly when `state` is closed.
    closed_at: datetime | None = None
    closed_by: str | None = None
    # Chosen at creation (0015); every session opened before it is `default`.
    effort: str = DEFAULT_EFFORT
    # Set by the owner or the PO (0019, :meth:`PoStore.set_title`); None is untitled.
    title: str | None = None
    # Only :meth:`PoStore.sessions` fills these two; a single-session read leaves them None.
    first_message: str | None = None
    last_activity_at: datetime | None = None
    # The model the session's latest turn that reported one ran, filled by the two session reads.
    resolved_model: str | None = None
    # Earliest owner input's native display metadata, filled only by sessions().
    first_message_metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class Turn:
    session_id: str
    seq: int
    started_at: datetime
    finished_at: datetime | None
    state: str
    stdout_path: str
    pid: int | None
    process_identity: str | None
    reason: str | None
    # The model the CLI reported it ran for this turn, null when it reported none (0015).
    resolved_model: str | None = None


@dataclass(frozen=True)
class FeedEntry:
    entry_id: int
    session_id: str
    turn_seq: int
    role: str
    text: str
    created_at: datetime
    # NULL for released inputs and agent answers; new service inputs retain queue classification.
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class PoRequest:
    request_id: str
    operation: str
    fingerprint: str
    session_id: str
    seq: int | None
    created_at: datetime


_SESSION_COLUMNS = (
    "session_id, cli, model, cwd, created_at, state, cli_session_id, closed_at, closed_by, effort, title"
)
_TURN_COLUMNS = "session_id, seq, started_at, finished_at, state, stdout_path, pid, process_identity, reason, resolved_model"
# The latest model a turn of session `s` reported, for the two session reads.
_RESOLVED_MODEL = (
    "(SELECT r.resolved_model FROM po_turns r WHERE r.session_id = s.session_id "
    "AND r.resolved_model IS NOT NULL ORDER BY r.seq DESC LIMIT 1)"
)
_FEED_COLUMNS = "entry_id, session_id, turn_seq, role, text, created_at, metadata"
_REQUEST_COLUMNS = "request_id, operation, fingerprint, session_id, seq, created_at"


# The longest title a session carries, in characters after trimming.
MAX_TITLE_LENGTH = 120


def session_title(title: str | None) -> str | None:
    """The title as stored: trimmed, None when nothing is left; refused with control characters or too long.

    The one rule for a session's title, whoever sets it. Control characters are C0 and C1 (so a
    newline, a tab and DEL too): a title is one line.
    """
    text = (title or "").strip()
    if not text:
        return None
    bad = next((c for c in text if ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F), None)
    if bad is not None:
        raise TitleRefused(f"a PO session title is one line of text; it may not contain {bad!r}")
    if len(text) > MAX_TITLE_LENGTH:
        raise TitleRefused(f"a PO session title is at most {MAX_TITLE_LENGTH} characters, not {len(text)}")
    return text


def session_fingerprint(cli: str, model: str, effort: str = DEFAULT_EFFORT) -> str:
    """What a session-create request id is bound to: the operation, the CLI, the model and the effort.

    A `default` effort is left out, so an id recorded before efforts existed binds the same inputs.
    """
    return _digest([SESSION_CREATE, cli, model] + ([] if effort == DEFAULT_EFFORT else [effort]))


def sprint_session_fingerprint(sprint_ref: str) -> str:
    """What a sprint-session request id is bound to: the operation and the sprint."""
    return _digest([SPRINT_SESSION, sprint_ref])


def send_fingerprint(session_id: str, text: str, card: Mapping[str, Any] | None = None) -> str:
    """What a send request id is bound to: the operation, the session, the exact text and the card facts.

    `card` is the structured facts a dispatcher input carries beside its text (`PoService.submit`,
    secretary-1764). A send without them binds exactly what it bound before they existed.
    """
    parts = [SEND, session_id, hashlib.sha256(text.encode("utf-8")).hexdigest()]
    if card is not None:
        parts.append(json.dumps(dict(card), sort_keys=True, separators=(",", ":")))
    return _digest(parts)


def _digest(parts: list[str]) -> str:
    return hashlib.sha256(json.dumps(parts).encode("utf-8")).hexdigest()


class PoStore:
    def __init__(self, credentials: Any) -> None:
        self.credentials = credentials

    @classmethod
    def for_instance(cls, instance_dir: Path | str, *, role: str = "app") -> PoStore:
        from ummanu.board.store import resolve_role

        return cls(resolve_role(instance_dir, role))

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[Any]:
        """One connection and one transaction: committed on success, rolled back on any error.

        The schema gate reads first, inside the same transaction, so a store that owes migrations
        is refused as :class:`PoSchemaOwed` before any PO table is touched.
        """
        import psycopg

        try:
            with psycopg.connect(self.credentials.conninfo()) as connection:
                require(connection, PoSchemaOwed)
                yield connection
        except psycopg.errors.UniqueViolation as exc:
            if exc.diag.constraint_name == ONE_RUNNING_INDEX:
                raise TurnInProgress("a turn is already running in this session") from None
            raise PoStoreError(f"the board store refused a PO session write: {exc}") from exc
        except psycopg.Error as exc:
            raise PoStoreError(f"the board store did not answer a PO session operation: {exc}") from exc

    # --- request ids ------------------------------------------------------------------------

    def request(self, request_id: str) -> PoRequest | None:
        with self._transaction() as connection:
            row = connection.execute(
                f"SELECT {_REQUEST_COLUMNS} FROM po_requests WHERE request_id = %s", (request_id,)
            ).fetchone()
        return PoRequest(*row) if row is not None else None

    def turn_request_id(self, session_id: str, seq: int) -> str | None:
        """The request id of the send that started turn `seq`, or None when its input carried none."""
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT request_id FROM po_requests WHERE session_id = %s AND seq = %s AND operation = %s",
                (session_id, seq, SEND),
            ).fetchone()
        return str(row[0]) if row is not None else None

    def bind_sprint_session_request(self, request_id: str, sprint_ref: str, session_id: str) -> None:
        """Bind a caller to the resolver's canonical successor using the released request vocabulary."""
        fingerprint = sprint_session_fingerprint(sprint_ref)
        with self._transaction() as connection:
            known = self._known_request(connection, request_id, SPRINT_SESSION, fingerprint)
            if known is not None:
                if known[0] != session_id:
                    raise RequestConflict("sprint resolve already names another session")
                return
            if connection.execute(
                "SELECT session_id FROM po_sessions WHERE session_id = %s", (session_id,)
            ).fetchone() is None:
                raise SessionNotFound(f"there is no PO session {session_id}")
            self._record_request(connection, request_id, SPRINT_SESSION, fingerprint, session_id, None)

    @staticmethod
    def _known_request(
        connection: Any, request_id: str, operation: str, fingerprint: str
    ) -> tuple[str, int | None] | None:
        """Steps 1-3: lock the id, then its recorded (session, seq), a conflict, or None when unknown."""
        connection.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (REQUEST_LOCK_CLASS, request_id))
        row = connection.execute(
            "SELECT operation, fingerprint, session_id, seq FROM po_requests WHERE request_id = %s",
            (request_id,),
        ).fetchone()
        if row is None:
            return None
        if (row[0], row[1]) != (operation, fingerprint):
            raise RequestConflict(
                f"request id {request_id!r} already belongs to another {row[0]} request; "
                "a request id is repeated only with the same operation and inputs"
            )
        return row[2], row[3]

    @staticmethod
    def _record_request(
        connection: Any, request_id: str, operation: str, fingerprint: str, session_id: str, seq: int | None
    ) -> None:
        connection.execute(
            f"INSERT INTO po_requests ({_REQUEST_COLUMNS}) VALUES (%s, %s, %s, %s, %s, now())",
            (request_id, operation, fingerprint, session_id, seq),
        )

    # --- sessions ---------------------------------------------------------------------------

    def create_session(
        self,
        *,
        session_id: str,
        cli: str,
        model: str,
        cwd: str,
        cli_session_id: str | None,
        effort: str = DEFAULT_EFFORT,
    ) -> Session:
        return self.claim_session(
            session_id=session_id, cli=cli, model=model, cwd=cwd, cli_session_id=cli_session_id, effort=effort
        )[0]

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
        """The new session, or the one `request_id` already created; the flag is True when this call did.

        `operation` and `fingerprint` bind the request id to something other than a plain create
        (the resolver's `SPRINT_SESSION`); by default they are a create of this CLI, model and effort.
        `title` is the new session's (:func:`session_title`); it is not part of the fingerprint.
        """
        fingerprint = fingerprint or session_fingerprint(cli, model, effort)
        title = session_title(title)
        with self._transaction() as connection:
            if request_id is not None:
                known = self._known_request(connection, request_id, operation, fingerprint)
                if known is not None:
                    row = connection.execute(
                        f"SELECT {_SESSION_COLUMNS} FROM po_sessions WHERE session_id = %s", (known[0],)
                    ).fetchone()
                    return Session(*row), False
            row = connection.execute(
                "INSERT INTO po_sessions "
                "(session_id, cli, model, cwd, created_at, state, cli_session_id, effort, title) "
                f"VALUES (%s, %s, %s, %s, now(), %s, %s, %s, %s) RETURNING {_SESSION_COLUMNS}",
                (session_id, cli, model, cwd, SESSION_OPEN, cli_session_id, effort, title),
            ).fetchone()
            if request_id is not None:
                self._record_request(connection, request_id, operation, fingerprint, session_id, None)
        return Session(*row), True

    def session(self, session_id: str) -> Session:
        columns = ", ".join(f"s.{column}" for column in _SESSION_COLUMNS.split(", "))
        with self._transaction() as connection:
            row = connection.execute(
                f"SELECT {columns}, {_RESOLVED_MODEL} FROM po_sessions s WHERE s.session_id = %s",
                (session_id,),
            ).fetchone()
        if row is None:
            raise SessionNotFound(f"there is no PO session {session_id}")
        return Session(*row[:-1], resolved_model=row[-1])

    def sessions(self, state: str = SESSION_OPEN) -> list[Session]:
        """Every session in `state` (open by default) with its first owner message and last activity.

        One statement for the whole list: the first message is the earliest owner feed entry by
        `entry_id`; the last activity is the latest of the session's creation, any turn's start or
        finish, and any feed entry. Newest activity first; ties fall back to the newest creation, then
        the session id.
        """
        if state not in (SESSION_OPEN, SESSION_CLOSED):
            raise ValueError(f"a PO session is open or closed, not {state}")
        columns = ", ".join(f"s.{column}" for column in _SESSION_COLUMNS.split(", "))
        with self._transaction() as connection:
            rows = connection.execute(
                f"SELECT {columns}, o.text, GREATEST(s.created_at, t.at, f.at) AS last_activity_at, "
                f"{_RESOLVED_MODEL}, o.metadata "
                "FROM po_sessions s "
                "LEFT JOIN (SELECT session_id, max(GREATEST(started_at, finished_at)) AS at "
                "FROM po_turns GROUP BY session_id) t ON t.session_id = s.session_id "
                "LEFT JOIN (SELECT session_id, max(created_at) AS at "
                "FROM po_feed GROUP BY session_id) f ON f.session_id = s.session_id "
                "LEFT JOIN (SELECT DISTINCT ON (session_id) session_id, text, metadata FROM po_feed "
                "WHERE role = %s ORDER BY session_id, entry_id) o ON o.session_id = s.session_id "
                "WHERE s.state = %s "
                "ORDER BY last_activity_at DESC, s.created_at DESC, s.session_id",
                (OWNER, state),
            ).fetchall()
        return [Session(*row) for row in rows]

    def session_count(self, state: str) -> int:
        with self._transaction() as connection:
            return connection.execute(
                "SELECT count(*) FROM po_sessions WHERE state = %s", (state,)
            ).fetchone()[0]

    def close_session(self, session_id: str, actor: str) -> Session:
        """The owner's close, in one transaction holding the session row.

        Already closed answers the session unchanged, with its original `closed_at` and `closed_by`. A
        running turn refuses with :class:`TurnInProgress` and writes nothing; a send holds the same row
        lock while it creates its turn (:meth:`claim_turn`), so a close and a send never interleave.
        """
        with self._transaction() as connection:
            row = connection.execute(
                f"SELECT {_SESSION_COLUMNS} FROM po_sessions WHERE session_id = %s FOR UPDATE", (session_id,)
            ).fetchone()
            if row is None:
                raise SessionNotFound(f"there is no PO session {session_id}")
            session = Session(*row)
            if session.state == SESSION_CLOSED:
                return session
            busy = connection.execute(
                "SELECT seq FROM po_turns WHERE session_id = %s AND state = %s", (session_id, RUNNING)
            ).fetchone()
            if busy is not None:
                raise TurnInProgress(
                    f"turn {busy[0]} is still running in PO session {session_id}; "
                    "wait for its answer or stop it, then close"
                )
            row = connection.execute(
                "UPDATE po_sessions SET state = %s, closed_at = now(), closed_by = %s "
                f"WHERE session_id = %s RETURNING {_SESSION_COLUMNS}",
                (SESSION_CLOSED, actor, session_id),
            ).fetchone()
        return Session(*row)

    def set_title(self, session_id: str, title: str | None) -> Session:
        """Set the session's title (:func:`session_title`; nothing left clears it), open or closed.

        A repeat with the same title writes the same value: the answer is the same session.
        """
        title = session_title(title)
        with self._transaction() as connection:
            row = connection.execute(
                f"UPDATE po_sessions SET title = %s WHERE session_id = %s RETURNING {_SESSION_COLUMNS}",
                (title, session_id),
            ).fetchone()
        if row is None:
            raise SessionNotFound(f"there is no PO session {session_id}")
        return Session(*row)

    def set_cli_session_id(self, session_id: str, cli_session_id: str) -> bool:
        """Record the CLI's own id once; an id already recorded is never replaced."""
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE po_sessions SET cli_session_id = %s WHERE session_id = %s AND cli_session_id IS NULL",
                (cli_session_id, session_id),
            )
            return cursor.rowcount == 1

    def switch_cli(
        self, session_id: str, *, cli: str, model: str, effort: str, cli_session_id: str | None
    ) -> Session:
        """Move an open session onto another CLI after its own provider refused it (ummanu-108).

        The CLI's conversation id is replaced (a new conversation: Claude's id is chosen up front,
        Codex's is recorded by its first turn), and the session keeps its feed, its turns and its
        requests: the PO's durable state is what the new conversation is resumed from.
        """
        if cli not in CLIS:
            raise PoStoreError(f"a PO session runs {' or '.join(CLIS)}, not {cli!r}")
        with self._transaction() as connection:
            row = connection.execute(
                "UPDATE po_sessions SET cli = %s, model = %s, effort = %s, cli_session_id = %s "
                f"WHERE session_id = %s RETURNING {_SESSION_COLUMNS}",
                (cli, model, effort, cli_session_id, session_id),
            ).fetchone()
        if row is None:
            raise SessionNotFound(f"there is no PO session {session_id}")
        return Session(*row)

    # --- turns ------------------------------------------------------------------------------

    def begin_turn(self, session_id: str, text: str, stdout_path: Callable[[int], Path]) -> Turn:
        """Atomically: the next turn as `running` and the owner's message in the feed."""
        return self.claim_turn(session_id, text, stdout_path)[0]

    def claim_turn(
        self,
        session_id: str,
        text: str,
        stdout_path: Callable[[int], Path],
        *,
        request_id: str | None = None,
        card: Mapping[str, Any] | None = None,
        prompt: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[Turn, bool]:
        """The new turn, or the one `request_id` already started; the flag is True when this call did.

        The id is bound to `text` and `card`; the feed records `prompt`, the text the turn is started
        with, when it differs (the PO service's note after the text, `PoRunner.send_request`).

        A replay answers with that turn in its current state — running, completed, failed or
        interrupted — before "a turn is running" is asked, so a replay during its own turn is that turn,
        and a replay of a send made before the session was closed is still its turn. Any other send
        into a closed session is :class:`SessionClosed` and writes nothing.
        """
        fingerprint = send_fingerprint(session_id, text, card)
        with self._transaction() as connection:
            if request_id is not None:
                known = self._known_request(connection, request_id, SEND, fingerprint)
                if known is not None:
                    row = connection.execute(
                        f"SELECT {_TURN_COLUMNS} FROM po_turns WHERE session_id = %s AND seq = %s", known
                    ).fetchone()
                    return Turn(*row), False
            found = connection.execute(
                "SELECT state FROM po_sessions WHERE session_id = %s FOR UPDATE", (session_id,)
            ).fetchone()
            if found is None:
                raise SessionNotFound(f"there is no PO session {session_id}")
            if found[0] == SESSION_CLOSED:
                raise SessionClosed(f"PO session {session_id} is closed; open a new session to continue")
            busy = connection.execute(
                "SELECT seq FROM po_turns WHERE session_id = %s AND state = %s",
                (session_id, RUNNING),
            ).fetchone()
            if busy is not None:
                raise TurnInProgress(
                    f"turn {busy[0]} is still running in PO session {session_id}; "
                    "wait for its answer or stop it first"
                )
            seq = connection.execute(
                "SELECT coalesce(max(seq), 0) + 1 FROM po_turns WHERE session_id = %s", (session_id,)
            ).fetchone()[0]
            row = connection.execute(
                f"INSERT INTO po_turns (session_id, seq, started_at, state, stdout_path) "
                f"VALUES (%s, %s, now(), %s, %s) RETURNING {_TURN_COLUMNS}",
                (session_id, seq, RUNNING, str(stdout_path(seq))),
            ).fetchone()
            connection.execute(
                "INSERT INTO po_feed (session_id, turn_seq, role, text, created_at, metadata) "
                "VALUES (%s, %s, %s, %s, now(), %s::jsonb)",
                (session_id, seq, OWNER, text if prompt is None else prompt,
                 json.dumps(dict(metadata)) if metadata is not None else None),
            )
            if request_id is not None:
                self._record_request(connection, request_id, SEND, fingerprint, session_id, seq)
        return Turn(*row), True

    def record_process(self, session_id: str, seq: int, pid: int, identity: str | None) -> bool:
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE po_turns SET pid = %s, process_identity = %s "
                "WHERE session_id = %s AND seq = %s AND state = %s",
                (pid, identity, session_id, seq, RUNNING),
            )
            return cursor.rowcount == 1

    def mark_rerun(self, session_id: str, seq: int, reason: str) -> bool:
        """Record on a `running` turn that it is being re-run and why; False when it already was.

        A running turn carries no reason otherwise, so a set reason on a running row is the record
        that this turn is a re-run: the PO service re-runs a turn once and settles a second
        interruption. The recorded process is cleared with it; the relaunch records its own.
        """
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE po_turns SET reason = %s, pid = NULL, process_identity = NULL "
                "WHERE session_id = %s AND seq = %s AND state = %s AND reason IS NULL",
                (reason, session_id, seq, RUNNING),
            )
            return cursor.rowcount == 1

    def complete_turn(
        self, session_id: str, seq: int, answer: str, *, resolved_model: str | None = None
    ) -> bool:
        """The agent's final answer into the feed and the turn `completed`, or nothing at all."""
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE po_turns SET state = %s, finished_at = now(), resolved_model = %s "
                "WHERE session_id = %s AND seq = %s AND state = %s",
                (COMPLETED, resolved_model, session_id, seq, RUNNING),
            )
            if cursor.rowcount != 1:
                return False
            connection.execute(
                "INSERT INTO po_feed (session_id, turn_seq, role, text, created_at) "
                "VALUES (%s, %s, %s, %s, now())",
                (session_id, seq, AGENT, answer),
            )
            return True

    def finish_turn(
        self, session_id: str, seq: int, state: str, reason: str, *, resolved_model: str | None = None
    ) -> bool:
        """The turn `failed` or `interrupted` with `reason`, replacing a re-run's; False once settled."""
        if state not in (FAILED, INTERRUPTED):
            raise ValueError(f"a turn is finished as failed or interrupted, not {state}")
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE po_turns SET state = %s, finished_at = now(), reason = %s, resolved_model = %s "
                "WHERE session_id = %s AND seq = %s AND state = %s",
                (state, reason, resolved_model, session_id, seq, RUNNING),
            )
            return cursor.rowcount == 1

    def turn(self, session_id: str, seq: int) -> Turn:
        with self._transaction() as connection:
            row = connection.execute(
                f"SELECT {_TURN_COLUMNS} FROM po_turns WHERE session_id = %s AND seq = %s",
                (session_id, seq),
            ).fetchone()
        if row is None:
            raise PoStoreError(f"there is no turn {seq} in PO session {session_id}")
        return Turn(*row)

    def turns(self, session_id: str) -> list[Turn]:
        with self._transaction() as connection:
            rows = connection.execute(
                f"SELECT {_TURN_COLUMNS} FROM po_turns WHERE session_id = %s ORDER BY seq",
                (session_id,),
            ).fetchall()
        return [Turn(*row) for row in rows]

    def running_turns(self, session_id: str | None = None) -> list[Turn]:
        query = f"SELECT {_TURN_COLUMNS} FROM po_turns WHERE state = %s"
        parameters: tuple[Any, ...] = (RUNNING,)
        if session_id is not None:
            query += " AND session_id = %s"
            parameters += (session_id,)
        with self._transaction() as connection:
            rows = connection.execute(query + " ORDER BY started_at, session_id", parameters).fetchall()
        return [Turn(*row) for row in rows]

    # --- feed -------------------------------------------------------------------------------

    def feed(self, session_id: str) -> list[FeedEntry]:
        with self._transaction() as connection:
            rows = connection.execute(
                f"SELECT {_FEED_COLUMNS} FROM po_feed WHERE session_id = %s ORDER BY entry_id",
                (session_id,),
            ).fetchall()
        return [FeedEntry(*row) for row in rows]


__all__ = [
    "AGENT",
    "CLIS",
    "COMPLETED",
    "DEFAULT_EFFORT",
    "FAILED",
    "INTERRUPTED",
    "OWNER",
    "RUNNING",
    "SEND",
    "SESSION_CLOSED",
    "SESSION_CREATE",
    "SESSION_OPEN",
    "FeedEntry",
    "PoRequest",
    "PoSchemaOwed",
    "PoStore",
    "PoStoreError",
    "RequestConflict",
    "Session",
    "SessionClosed",
    "SessionNotFound",
    "Turn",
    "TurnInProgress",
    "send_fingerprint",
    "session_fingerprint",
]
