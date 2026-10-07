"""The card reader's and writer's implementation: the PostgreSQL board store.

`docs/BOARD_STORE.md` §2.2 is the reason this module has the shape it has.  Almost every consumer
of the board reaches cards through `TaskReader` and `TaskWriter`, not through the JSON-RPC client,
so the cheapest honest place to put the store is *underneath* those two classes and nowhere else.  `SqlCardClient` therefore answers the same eleven-method board vocabulary
`tasks.py` and `board/sql_host.py` speak — `getAllTasks`, `getTaskMetadata`, `saveTaskMetadata`,
`createTask`, `updateTask`, `moveTaskPosition`, `closeTask`, `createComment`, `getAllComments`,
`getColumns`, `getActiveSwimlanes`, `getProjectByName`, `getTaskByReference`, `addSwimlane` — over
`tasks`, `task_comments` and their satellites (§3.5, §3.7).  The public behaviour of the two
classes above it does not change; where their data comes from and where it lands does.

Three mappings do the whole job:

* **state ↔ column.**  The store keeps `tasks.state`; the board keeps a column id.  §3.5's seven
  states and `_STATE_BY_COLUMN`'s seven column titles are the same seven, so the virtual board
  below numbers them once and both directions read that one table.
* **metadata bag ↔ columns.**  §8.1: the keys the model names are columns, and the keys it does
  not are `tasks.extensions.extra` (§8.2).  `saveTaskMetadata` writes columns for the former
  and the bag for the latter, so a key nobody modelled is still readable rather than dropped.
* **swimlane ↔ nothing.**  The store has no lane: a lane is a legacy board presentation of the
  product a card belongs to.  It is kept exactly where the importer keeps it —
  `extensions.extra.swimlane` — and the lane *table* is virtual, derived from the lanes the
  rows themselves name plus the products the store holds.

The integer board-client identity is `tasks.board_key`. It is immutable and globally unique while
`tasks.task_number` remains the public per-project number parsed from the stable reference.
"""

from __future__ import annotations

import contextlib
import json
import re
import select
import threading
import time
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ummanu.board import schema_gate
from ummanu.board.backend import card_transport_key, record_key_kind
from ummanu.board.extension_bag import EXTENSION_BAG
from ummanu.board.sql_product_issues import ProductIssueRecords
from ummanu.board.sql_sprints import SqlSprintRecords
from ummanu.board.store import BoardStoreCredentials
from ummanu.board.task_routing import live_impact_flag
from ummanu.tasks import TaskError

#: The seven columns of the Pipeline board, in board order.  Their ids are this module's own:
#: nothing outside the client may depend on the number, only on the title.
BOARD_COLUMNS = (
    (1, "Issues"),
    (2, "Ready"),
    (3, "In progress"),
    (4, "Validate"),
    (5, "Assessment"),
    (6, "Blocked"),
    (7, "Done"),
)

#: `_STATE_BY_COLUMN` read the other way, so a store row can name its column.
_COLUMN_ID_BY_STATE = {
    "issues": 1,
    "ready": 2,
    "in_progress": 3,
    "validate": 4,
    "assessment": 5,
    "blocked": 6,
    "done": 7,
}
_STATE_BY_COLUMN_ID = {identifier: state for state, identifier in _COLUMN_ID_BY_STATE.items()}

#: The one virtual board this client serves.  The store has no second board; a name that is not
#: this one (or the sprint board's) is not found.
BOARD_NAME = "Pipeline"
BOARD_ID = 1
SPRINT_BOARD_NAME = "Ummanu sprints"
SPRINT_BOARD_ID = 2

#: §8.1's metadata keys that are `tasks` columns, and the column each one is.  Everything else a
#: caller writes lands in `extensions[EXTENSION_BAG]` (§8.2).
_METADATA_COLUMNS = {
    "project": "project_id",
    "task_type": "task_type",
    "claim": "claim_worker",
    "slug": "slug",
    "base_branch": "base_branch",
    "seed_ref": "seed_ref",
    "complexity": "complexity",
    "family_preference": "family_preference",
    "head": "head_override",
    "review_head": "review_head_override",
    "resolved_head": "resolved_worker_head",
    "resolved_review_head": "resolved_review_head",
    "routing_reason": "routing_reason",
    "codex_launch_mode": "codex_launch_mode",
    "sprint_ref": "sprint_ref",
    "review": "review",
}

#: The most connections one client holds open at once (§5.6).  The web serves each request on its
#: own thread, and a dashboard asked from four places at once should not queue; the dispatcher and
#: a CLI process use one thread and so one connection.
POOL_SIZE = 4
#: How long a call waits for a free connection before it fails as `backend_unavailable`.
POOL_WAIT_SECONDS = 10.0

#: `live_impact` is a boolean column and `"1"` or absence on the board.
_METADATA_FLAG = ("live_impact", "live_impact")

#: The two counters, which are integers in the store and decimal strings on the board.
_METADATA_COUNTERS = {"retry_same": "retry_same", "retry_switch": "retry_switch"}

#: The columns whose closed vocabulary has a default the board spells as absence (§3.12).
_ENUM_DEFAULTS = {"complexity": "standard", "family_preference": "auto"}

#: `quota_snapshot_at` is a `timestamptz` column and an RFC3339 string on the board.
_METADATA_TIMESTAMP = ("quota_snapshot_at", "quota_snapshot_at")

#: Metadata keys with satellite tables rather than columns (§3.5).
_METADATA_LINKS = ("retry_heads", "blocked_by", "supersedes", "issues")


class SqlCardError(TaskError):
    """The store cannot answer this board question without guessing.

    A `TaskError`, not a bare `RuntimeError`: every command above this client renders that one
    vocabulary as a named refusal with an exit status (`task_commands.run_task_command`), and a
    `RuntimeError` reaching a CLI handler is a traceback with a connection string somewhere up
    the stack.  The code is the one every malformed-reply refusal of the board vocabulary uses.
    """

    def __init__(self, message: str) -> None:
        super().__init__("backend_error", message, 1)


class CardSchemaOwed(schema_gate.SchemaOwed, TaskError):
    """The store owes migrations this build's code reads through (`board.schema_gate`).

    Raised when a connection opens, before any statement that depends on the schema, so every
    reader and writer over this client — cards, sprints, products/issues, the SQL audit and
    `SqlBoardHost` — answers it as one named `TaskError`.
    """

    def __init__(self, assessment: schema_gate.SchemaAssessment) -> None:
        self.assessment = assessment
        TaskError.__init__(self, schema_gate.SCHEMA_OWED, assessment.describe(), 1)


def _driver_error(action: str, exc: BaseException) -> TaskError:
    """One driver failure, in the refusal vocabulary the CLI already prints.

    Three classes and each keeps its own name.  A driver that is not installed at all, and a
    server that will not accept or keep a connection, are `backend_unavailable` — the code the
    board vocabulary uses for exactly that, and the one callers treat as "the effect may or may
    not have landed".  Everything else psycopg raises — a constraint, a
    type, a statement the schema refuses — is `backend_error`.  What PostgreSQL said is carried
    through, and only that: psycopg's diagnostics do not contain the connection string, so an
    operator gets the reason without the credentials.
    """
    if isinstance(exc, ModuleNotFoundError):
        return TaskError("backend_unavailable", f"the board store driver is not installed: {exc}", 1)
    import psycopg

    if isinstance(exc, psycopg.OperationalError):
        return TaskError(
            "backend_unavailable", f"the board store is unreachable while it must {action}: {exc}", 1
        )
    return TaskError("backend_error", f"the board store refused to {action}: {exc}", 1)


@contextlib.contextmanager
def _translated(action: str) -> Iterator[None]:
    """`_driver_error`, applied to everything the driver raises inside the block."""
    try:
        import psycopg
    except ModuleNotFoundError as exc:
        raise _driver_error(action, exc) from None
    try:
        yield
    except psycopg.Error as exc:
        raise _driver_error(action, exc) from None


def _unusable(connection: Any) -> bool:
    """psycopg's own verdict that a connection can carry no further statement.

    Compared with `True` so a stand-in without the attributes reads as alive.
    """
    return getattr(connection, "closed", False) is True or getattr(connection, "broken", False) is True


def _hung_up(connection: Any) -> bool:
    """Whether the server has already spoken on an idle connection, which it does only to end it.

    A terminated backend or a restarted server sends its farewell and closes the socket, and
    psycopg learns of it only from the next statement, which then fails.  An idle connection has
    nothing to read, so a readable socket is a dead one.  A stand-in without `fileno` reads as alive.
    """
    fileno = getattr(connection, "fileno", None)
    if fileno is None:
        return False
    try:
        poller = select.poll()
        poller.register(fileno(), select.POLLIN | select.POLLERR | select.POLLHUP)
        return bool(poller.poll(0))
    except Exception:  # noqa: BLE001 - a socket that cannot be asked cannot be trusted either.
        return True


def _transaction_state(connection: Any) -> str | None:
    """psycopg's local name for where the connection's transaction stands, or None for a stand-in."""
    return getattr(getattr(getattr(connection, "info", None), "transaction_status", None), "name", None)


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _epoch(value: datetime | None) -> str:
    if value is None:
        return ""
    return str(int(value.timestamp()))


def _ensure_project_row(client: SqlCardClient, project_id: str) -> None:
    """Insert a missing `projects` row with the id only, inside the caller's transaction (§3.1)."""
    client._execute(
        "INSERT INTO projects (project_id) VALUES (%s) ON CONFLICT (project_id) DO NOTHING",
        (project_id,),
    )


def _now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: Any) -> datetime | None:
    text = _text(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _rfc3339(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


#: The per-record reads `call_batch` answers set-based rather than call by call.
_BULK_READS = ("getTaskMetadata", "getAllComments")


def _batch_key(task_id: Any) -> Any:
    """The identity a batch answer is filed under: the integer key, or the raw value if none."""
    try:
        return int(task_id)
    except (TypeError, ValueError):
        return task_id


def _grouped(rows: Iterable[tuple[Any, ...]]) -> dict[Any, list[Any]]:
    """Rows keyed by their first column, each group in the order the statement returned it.

    A group holds the bare second column when a row has two columns, else the remaining tuple.
    """
    groups: dict[Any, list[Any]] = {}
    for owner, *rest in rows:
        groups.setdefault(owner, []).append(rest[0] if len(rest) == 1 else tuple(rest))
    return groups


def _task_number_of(ref: str) -> int:
    match = re.search(r"(\d+)$", ref)
    if match is None:
        raise SqlCardError(f"a card reference must end in a number: {ref!r}")
    return int(match.group(1))


class _ThreadState(threading.local):
    """What one thread holds of a client: its pinned connection, open sessions, transaction depth,
    and what its open transaction has staged but not committed."""

    connection: Any = None
    sessions = 0
    depth = 0
    #: Staged creates by kind ("records", "sprints"), or None outside a transaction.
    staged: dict[str, dict[int, dict[str, Any]]] | None = None
    #: Lanes this thread's open transaction added, merged into the shared table on commit.
    lanes_added: list[str] | None = None


class SqlCardClient:
    """The board vocabulary of §2.2, answered from PostgreSQL instead of JSON-RPC.

    A bounded pool of connections, opened lazily and kept (§5.6); a client that reconnected per
    call would spend its time on handshakes.  A thread pins one of them for a session — one board
    call, one `transaction()` — so threads read at once and never share a transaction.  Autocommit
    is off, so a mutation issued inside `transaction()` is one transaction (§7.1) and one issued
    outside it still commits on its own.
    """

    def __init__(
        self,
        credentials: BoardStoreCredentials,
        instance_dir: Path | str,
        *,
        pool_size: int = POOL_SIZE,
        pool_wait_seconds: float = POOL_WAIT_SECONDS,
    ) -> None:
        self.credentials = credentials
        self.instance_dir = Path(instance_dir)
        self.pool_size = pool_size
        self.pool_wait_seconds = pool_wait_seconds
        # The pool: idle connections, newest last, and how many are open, idle or pinned.
        self._pool = threading.Condition()
        self._idle: list[Any] = []
        self._open = 0
        self._local = _ThreadState()
        # Transactions of different threads take turns.
        self._transaction_lock = threading.RLock()
        # The virtual lane table as committed: names the rows themselves carry, plus what
        # `addSwimlane` adds outside a transaction.  A transaction's own additions stay in its
        # thread's state until it commits (`_add_lane`).
        self._lanes: list[str] | None = None
        self._lanes_lock = threading.Lock()
        # The Product/Issue half of the same vocabulary, over the same pool (§3.1, §3.2).
        self.records = ProductIssueRecords(self)
        self.sprints = SqlSprintRecords(self)

    # --- connection ------------------------------------------------------------------

    @property
    def _depth(self) -> int:
        """This thread's `transaction()` nesting; another thread's transaction is not this one's."""
        return self._local.depth

    @_depth.setter
    def _depth(self, value: int) -> None:
        self._local.depth = value

    @property
    def _connection(self) -> Any:
        """The connection this thread has pinned, or None."""
        return self._local.connection

    def _staged(self, kind: str) -> dict[int, dict[str, Any]]:
        """The one place staged Product/Issue (`"records"`) and Sprint (`"sprints"`) creates live.

        They belong to the thread that staged them (§5.6): no other thread's read sees them, since
        PostgreSQL has no such row for it either.  Inside `transaction()` they are that
        transaction's, and its end drops them — a rollback discards them, and a commit refuses while
        any is unfinished.  Outside a transaction a create stays its thread's until the thread's own
        `saveTaskMetadata` inserts it, the two-call create the legacy retry path and the fixtures use.
        """
        local = self._local
        if local.staged is None:
            local.staged = {"records": {}, "sprints": {}}
        return local.staged[kind]

    @property
    def connection(self) -> Any:
        """This thread's pinned connection, replaced first when it is dead and no transaction holds
        it (§5.6).

        A board-store restart leaves a kept object `closed` or `broken` for good; without this
        check every later call of a long-lived holder (the web layer) failed with "the connection
        is closed" until the process restarted.  Inside `transaction()` a dead connection is kept,
        so the statement fails and the transaction with it: a reconnect there would run the rest
        of the transaction on a connection that never saw its first half.

        Asked outside every session (a caller closing the client's connection, a test reading its
        backend pid), it answers the connection this thread would use next, borrowed and returned
        at once.
        """
        local = self._local
        if not local.sessions:
            with self._session():
                return self.connection
        if local.connection is not None and not local.depth and _unusable(local.connection):
            self._discard()
        if local.connection is None:
            local.connection = self._borrow()
        return local.connection

    def close(self) -> None:
        """Close the idle connections and this thread's own; one in use elsewhere returns first."""
        with self._pool:
            closing, self._idle = self._idle, []
            pinned, self._local.connection = self._local.connection, None
            if pinned is not None:
                closing.append(pinned)
            self._open -= len(closing)
            self._pool.notify_all()
        for connection in closing:
            with _translated("close its connection"):
                connection.close()

    def _borrow(self) -> Any:
        """An idle live connection, a new one while fewer than `pool_size` are open, or a wait.

        An idle connection that is dead, or whose server has hung up on it, is closed and never
        handed out.  A caller that finds every connection in use waits `pool_wait_seconds`, then
        fails as `backend_unavailable`.
        """
        deadline = time.monotonic() + self.pool_wait_seconds
        with self._pool:
            while True:
                while self._idle:
                    candidate = self._idle.pop()
                    if not (_unusable(candidate) or _hung_up(candidate)):
                        return candidate
                    self._open -= 1
                    with contextlib.suppress(Exception):
                        candidate.close()
                if self._open < self.pool_size:
                    self._open += 1
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TaskError(
                        "backend_unavailable",
                        f"all {self.pool_size} board store connections of this process stayed in use "
                        f"for {self.pool_wait_seconds:g} s",
                        1,
                    )
                self._pool.wait(remaining)
        try:
            with _translated("open a connection"):
                import psycopg

                connection = psycopg.connect(self.credentials.conninfo(), autocommit=False)
            self._admit(connection)
            return connection
        except BaseException:
            self._give_back(None)
            raise

    @staticmethod
    def _admit(connection: Any) -> None:
        """The schema gate, once per new connection: pooled ones were admitted when they opened.

        A refused connection is closed rather than pooled, so nothing remembers the refusal: the
        next borrow opens and reads again, and succeeds once the migrations are applied. The read's
        transaction is ended here, so an admitted connection enters the pool idle.
        """
        try:
            with _translated("read its schema revision"):
                schema_gate.require(connection, CardSchemaOwed)
                if _transaction_state(connection) not in (None, "IDLE"):
                    connection.rollback()
        except BaseException:
            with contextlib.suppress(Exception):
                connection.close()
            raise

    def _give_back(self, connection: Any) -> None:
        """Return a live connection to the pool, or with None free the slot of a dead one."""
        with self._pool:
            if connection is None:
                self._open -= 1
            else:
                self._idle.append(connection)
            self._pool.notify()

    def _discard(self) -> None:
        """Drop this thread's pinned connection so the next statement borrows another; closing it
        may fail."""
        connection, self._local.connection = self._local.connection, None
        if connection is not None:
            with contextlib.suppress(Exception):
                connection.close()
            self._give_back(None)

    @contextlib.contextmanager
    def _session(self) -> Iterator[None]:
        """Pin one pooled connection to this thread for the block, borrowed on first use.

        Re-entrant: an inner session, a `transaction()` and every statement inside join the pin.
        The outermost exit ends what the connection still has open — commits it, or rolls it back
        when the block failed or a statement left it aborted — and returns it to the pool, or
        discards it if it is dead, so an idle pooled connection never holds a transaction.
        """
        local = self._local
        local.sessions += 1
        failed = True
        try:
            yield
            failed = False
        finally:
            local.sessions -= 1
            if not local.sessions:
                self._unpin(failed=failed)

    def _unpin(self, *, failed: bool) -> None:
        connection, self._local.connection = self._local.connection, None
        if connection is None:
            return
        state = _transaction_state(connection)
        committing = False
        try:
            if state == "INERROR":
                # A statement failed and nothing rolled it back.  A session-level advisory lock
                # taken in this session may have outlived its own unlock the same way.
                connection.rollback()
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_unlock_all()")
                connection.rollback()
            elif state == "INTRANS" and failed:
                connection.rollback()
            elif state == "INTRANS":
                committing = True
                connection.commit()
        except Exception as exc:  # noqa: BLE001 - a connection that cannot end its work is not reused.
            self._local.connection = connection
            self._discard()
            if committing:
                raise _driver_error("commit", exc) from None
            return
        if _unusable(connection):
            self._local.connection = connection
            self._discard()
        else:
            self._give_back(connection)

    @contextlib.contextmanager
    def _statement(self, action: str) -> Iterator[None]:
        """`_translated`, plus: a statement outside a transaction that left the connection dead
        discards it, so this call still fails as `backend_unavailable` and the next one reconnects.
        """
        try:
            with _translated(action):
                yield
        except TaskError:
            if not self._depth and self._connection is not None and _unusable(self._connection):
                self._discard()
            raise

    @contextlib.contextmanager
    def read_snapshot(self) -> Iterator[None]:
        """Pin all checkpoint rows, metadata, comments and audit to one read-only cut."""
        if self._depth:
            raise SqlCardError("a read snapshot must start outside a mutation transaction")
        with self.transaction():
            self._execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            yield

    @contextlib.contextmanager
    def transaction(self) -> Iterator[None]:
        """One transaction on this thread's pinned connection; other threads' ones wait their turn.

        The connection is borrowed before the turn is awaited, so the thread holding the turn
        never waits for the pool.
        """
        with self._session():
            if not self._depth:
                self.connection  # noqa: B018 - borrowed here, before the lock.
            with self._transaction_lock, self._transaction():
                yield

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[None]:
        """One transaction per protocol mutation (§7.1), re-entrant for nested effects.

        The writer opens this once around a whole protocol mutation — the request claim, the card
        effect and the event — and every inner call joins it.  A failure anywhere inside rolls the
        whole thing back, which is why the `BoardEventPending` class of half-applied write §7.3
        describes does not exist on this backend.
        """
        if self._depth:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        # Checked before the depth is raised: a transaction may start on a new connection, never
        # continue on one (§5.6).
        if self._connection is not None and _unusable(self._connection):
            self._discard()
        self._depth = 1
        suspect = False
        try:
            yield
        except BaseException as failure:
            # A rollback that itself fails must not hide what it was rolling back, so the
            # original failure stays the cause of the refusal the caller sees.
            try:
                self.connection.rollback()
            except Exception as exc:  # noqa: BLE001 - every driver failure becomes one refusal.
                suspect = True
                raise _driver_error("roll back", exc) from failure
            finally:
                # Two pieces of state are derived from rows this transaction wrote and are wrong
                # the moment those rows are gone: the staged creates and the virtual lane table.
                self._local.staged = self._local.lanes_added = None
                self._lanes = None
            raise
        else:
            if self.records.staged or self.sprints.staged:
                pending = ", ".join(
                    sorted(
                        [row["reference"] for row in self.records.staged.values()]
                        + [row["reference"] for row in self.sprints.staged.values()]
                    )
                )
                self._local.staged = self._local.lanes_added = None
                self._lanes = None
                self.connection.rollback()
                raise SqlCardError(
                    f"a Product/Issue create was never finished and cannot commit: {pending}. "
                    "The row's own table needs the values `saveTaskMetadata` carries (§3.2)"
                )
            with _translated("commit"):
                self.connection.commit()
            # Committed: this transaction's lanes are everyone's now.
            for name in self._local.lanes_added or ():
                self._add_shared_lane(name)
        finally:
            self._depth = 0
            self._local.staged = self._local.lanes_added = None
            # A transaction whose connection died, or whose rollback failed, leaves a connection
            # nothing may reuse: dropped here, so the next operation opens a new one (§5.6).
            if self._connection is not None and (suspect or _unusable(self._connection)):
                self._discard()

    def _commit_unless_nested(self) -> None:
        if not self._depth:
            with self._session(), self._statement("commit"):
                self.connection.commit()

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        with self._session(), self._statement("answer a read"), self.connection.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall()

    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        with self._session(), self._statement("apply a write"), self.connection.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.rowcount

    # --- the board vocabulary --------------------------------------------------------

    def call(self, method: str, **params: Any) -> Any:
        handler = getattr(self, f"_rpc_{method}", None)
        if handler is None:
            raise SqlCardError(f"the board store does not serve {method}")
        # One call is one session: its statements and its commit share a connection.
        try:
            with self._session():
                return handler(**params)
        finally:
            if not method.startswith("get"):
                from ummanu.board.tick_snapshot import board_write

                board_write(self, params)

    def call_batch(self, calls: Iterable[tuple[str, dict[str, Any]]]) -> list[Any]:
        """Run the calls and answer in call order.

        A batch made only of per-record reads (`getTaskMetadata`, `getAllComments`) is answered
        set-based: one statement per table for every record the batch names, whatever their
        number, so a whole-board listing costs what one record costs.  A batch holding anything
        else runs call by call, which keeps its writes in the order the caller gave them.
        """
        prepared = [(method, dict(arguments)) for method, arguments in calls]
        if not all(method in _BULK_READS for method, _ in prepared):
            with self._session():
                return [self.call(method, **arguments) for method, arguments in prepared]
        wanted: dict[str, list[Any]] = {method: [] for method in _BULK_READS}
        for method, arguments in prepared:
            wanted[method].append(arguments["task_id"])
        with self._session():
            answers = {
                "getTaskMetadata": self._metadata_of(wanted["getTaskMetadata"]),
                "getAllComments": self._comments_of(wanted["getAllComments"]),
            }
        return [answers[method][_batch_key(arguments["task_id"])] for method, arguments in prepared]

    def _metadata_of(self, task_ids: list[Any]) -> dict[Any, dict[str, str]]:
        """`getTaskMetadata` for every id, keyed by `_batch_key`, in bounded statements."""
        return self._by_kind(
            task_ids,
            card=self._card_metadata,
            sprint=self.sprints.metadata_of,
            record=self.records.metadata_of,
        )

    def _comments_of(self, task_ids: list[Any]) -> dict[Any, list[dict[str, Any]]]:
        """`getAllComments` for every id, keyed by `_batch_key`, in bounded statements."""
        return self._by_kind(
            task_ids,
            card=self._card_comments,
            sprint=self.sprints.comments_of,
            record=self.records.comments_of,
        )

    @staticmethod
    def _by_kind(task_ids: list[Any], *, card: Any, sprint: Any, record: Any) -> dict[Any, Any]:
        """Route each id to the table family its key names, one set-based read per family."""
        groups: dict[str, list[int]] = {"card": [], "sprint": [], "record": []}
        seen: set[Any] = set()
        for task_id in task_ids:
            key = _batch_key(task_id)
            if key in seen:
                continue
            seen.add(key)
            kind = record_key_kind(task_id)
            if kind == "sprint":
                groups["sprint"].append(int(task_id))
            elif kind is not None:
                groups["record"].append(int(task_id))
            else:
                groups["card"].append(task_id)
        answers: dict[Any, Any] = {}
        for family, read in (("card", card), ("sprint", sprint), ("record", record)):
            if groups[family]:
                answers.update(read(groups[family]))
        return answers

    def restore_card_rows(self) -> list[dict[str, Any]]:
        """Stored Card rows only; Product/Issue ownership is not restore emptiness."""
        return self._rows()

    # --- board shape -----------------------------------------------------------------

    def _rpc_getProjectByName(self, *, name: str) -> dict[str, Any] | None:
        if name == BOARD_NAME:
            return {"id": BOARD_ID, "name": BOARD_NAME}
        if name == SPRINT_BOARD_NAME:
            return {"id": SPRINT_BOARD_ID, "name": SPRINT_BOARD_NAME}
        return None

    def _rpc_createProject(self, *, name: str) -> Any:
        return SPRINT_BOARD_ID if name == SPRINT_BOARD_NAME else False

    def _rpc_getColumns(self, *, project_id: int) -> list[dict[str, Any]]:
        if int(project_id) == SPRINT_BOARD_ID:
            return [{"id": 1, "title": "Sprints"}]
        return [{"id": identifier, "title": title} for identifier, title in BOARD_COLUMNS]

    def _lane_names(self) -> list[str]:
        """The lane table as this thread sees it: the committed one plus its transaction's own."""
        committed = self._lanes
        if committed is None:
            named = {
                row[0]
                for row in self._query(
                    f"SELECT DISTINCT extensions->'{EXTENSION_BAG}'->>'swimlane' FROM tasks "
                    f"WHERE extensions->'{EXTENSION_BAG}'->>'swimlane' IS NOT NULL"
                )
            }
            named |= {row[0] for row in self._query("SELECT product_id FROM products")}
            with self._lanes_lock:
                if self._lanes is None:
                    self._lanes = sorted(named)
                committed = self._lanes
        added = self._local.lanes_added if self._depth else None
        # Without additions of its own, the thread gets the shared table itself, as before.
        return sorted({*committed, *added}) if added else committed

    def _add_lane(self, name: str) -> None:
        """A lane from now on: this thread's transaction's until it commits, else everyone's."""
        if self._depth:
            if self._local.lanes_added is None:
                self._local.lanes_added = []
            self._local.lanes_added.append(name)
        else:
            self._add_shared_lane(name)

    def _add_shared_lane(self, name: str) -> None:
        with self._lanes_lock:
            # Not loaded yet: the next load reads the committed rows, which name it already.
            if self._lanes is not None and name not in self._lanes:
                self._lanes = sorted([*self._lanes, name])

    def _rpc_getActiveSwimlanes(self, *, project_id: int) -> list[dict[str, Any]]:
        return [
            {"id": index, "name": name, "position": index}
            for index, name in enumerate(self._lane_names(), start=1)
        ]

    def _rpc_addSwimlane(self, *, project_id: int, name: str) -> Any:
        lanes = self._lane_names()
        if name in lanes:
            return False  # A duplicate name answers false, not the existing id.
        self._add_lane(name)
        return sorted([*lanes, name]).index(name) + 1

    def _lane_added(self, name: str) -> None:
        """A product this transaction created is a lane from now on (§8.6)."""
        if name not in self._lane_names():
            self._add_lane(name)

    def _lane_id(self, name: str | None) -> int:
        if not name:
            return 0
        lanes = self._lane_names()
        return lanes.index(name) + 1 if name in lanes else 0

    def _lane_name(self, identifier: Any) -> str | None:
        lanes = self._lane_names()
        try:
            index = int(identifier)
        except (TypeError, ValueError):
            return None
        return lanes[index - 1] if 1 <= index <= len(lanes) else None

    # --- cards -----------------------------------------------------------------------

    _CARD_COLUMNS = (
        "task_ref, board_key, title, description, state, archived, position, "
        "extensions, created_at, updated_at, date_moved"
    )

    def _row(self, values: tuple[Any, ...]) -> dict[str, Any]:
        (
            ref,
            board_key,
            title,
            description,
            state,
            archived,
            position,
            extensions,
            created,
            updated,
            moved,
        ) = values
        bag = extensions if isinstance(extensions, dict) else json.loads(extensions or "{}")
        lane = (bag.get(EXTENSION_BAG) or {}).get("swimlane")
        transport_key = card_transport_key(board_key)
        if transport_key is None:
            raise SqlCardError(f"card {ref} carries malformed transport key {board_key!r}")
        return {
            "id": transport_key,
            "reference": ref,
            "title": _text(title),
            "description": _text(description),
            "column_id": _COLUMN_ID_BY_STATE[state],
            "position": position,
            "swimlane_id": self._lane_id(lane),
            "date_creation": _epoch(created),
            "date_modification": _epoch(updated),
            # §3.5's `date_moved`, added by revision `0004`: when this card entered the column it
            # is in.  A row the store cannot date — every row written before that revision —
            # answers no value at all rather than a substitute, which is what keeps Done
            # retention refusing an episode nobody can name (§8.6).
            **({"date_moved": _epoch(moved)} if moved is not None else {}),
            "is_active": 0 if archived else 1,
        }

    def _rows(self, where: str = "", params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        clause = f" WHERE {where}" if where else ""
        rows = [
            self._row(values)
            for values in self._query(
                f"SELECT {self._CARD_COLUMNS} FROM tasks{clause} ORDER BY task_ref", params
            )
        ]
        seen: dict[int, str] = {}
        for row in rows:
            previous = seen.get(row["id"])
            if previous is not None:
                raise SqlCardError(
                    f"two cards carry transport key {row['id']}: "
                    f"{previous} and {row['reference']}"
                )
            seen[row["id"]] = row["reference"]
        return rows

    def _rpc_getAllTasks(self, *, project_id: int, status_id: int = 1) -> list[dict[str, Any]]:
        """Every row of the board, which is three tables here and was one table before them (§8.1).

        `all_project_cards` is how `ProductIssueStore` sees the board at all, so a Product or an
        Issue that is not in this answer is a record the catalogue cannot report.  The status
        filter means the same thing for all three: an archived Product and a closed Issue are
        `is_active = 0`, exactly as an archived card is.
        """
        if int(project_id) == SPRINT_BOARD_ID:
            return self.sprints.rows() if status_id == 1 else []
        if status_id not in {0, 1}:
            return []
        rows = self._rows("archived = %s", (status_id == 0,))
        active = status_id == 1
        rows += [row for row in self.records.rows() if bool(row["is_active"]) is active]
        return rows

    def _rpc_getTaskByReference(self, *, project_id: int, reference: str) -> dict[str, Any] | None:
        if int(project_id) == SPRINT_BOARD_ID:
            return self.sprints.row_by_reference(reference)
        if self.records.kind_of_reference(reference) is not None:
            return self.records.row_by_reference(reference)
        rows = self._rows("task_ref = %s", (reference,))
        return rows[0] if rows else None

    def _rpc_getBoardRows(self, *, project_id: int) -> list[dict[str, Any]]:
        """One complete enumeration for checkpoint exports, including archived records."""
        if int(project_id) == SPRINT_BOARD_ID:
            return self.sprints.rows()
        return self._rows() + self.records.rows()

    def _rpc_getTaskById(self, *, project_id: int, task_id: int) -> dict[str, Any] | None:
        """An exact Card transport key, including an archived Card during recovery."""
        if int(project_id) != BOARD_ID or card_transport_key(task_id) is None:
            return None
        rows = self._rows("board_key = %s", (task_id,))
        return rows[0] if rows else None

    def _rpc_getArchivedAfterMergeTasks(self, *, project_id: int) -> list[dict[str, Any]]:
        if int(project_id) != BOARD_ID:
            return []
        # e2e is JSON text in the extension bag. Match its after_merge keys without
        # casting: a malformed record must reach the normal per-carrier error path.
        return self._rows(
            f"archived AND extensions->'{EXTENSION_BAG}'->>'e2e' LIKE %s",
            ('%"after_merge%',),
        )

    def _rpc_getCapacityReferences(self) -> list[str]:
        """Admission needs fresh keys, including cards activated by another process.

        This filtered primary-key query is not a full board/metadata hydration.
        TaskWriter revalidates each peer while its capacity admission lock is held.
        """
        from ummanu.tasks import ACTIVE_STATES

        return [row[0] for row in self._query(
            "SELECT task_ref FROM tasks WHERE NOT archived AND state = ANY(%s) ORDER BY task_ref",
            (list(ACTIVE_STATES),),
        )]

    def _rpc_lockOwnershipReference(self, *, reference: str, observer: bool = False) -> bool:
        """Fence state/claim updates in the caller's cleanup/launch transaction.

        NO KEY UPDATE still permits a head's comment foreign-key check. Acquire
        this before cleanup.lock, so a contended SQL row never holds the flock.
        """
        table, key = ("sprints", "ref") if observer else ("tasks", "task_ref")
        return bool(self._query(f"SELECT {key} FROM {table} WHERE {key}=%s FOR NO KEY UPDATE",
                                (reference,)))

    def _rpc_getNextTaskReference(self, *, project: str) -> str:
        # Products and issues have product:/issue: references, never project-N.
        # Read the numeric high-water mark, including archives, without returning
        # rows. A punctuation range is unsafe under locale-dependent collations;
        # the literal prefix is the membership test. The caller holds the allocation
        # lock and checks the live key.
        prefix = project + "-"
        rows = self._query(
            "SELECT coalesce(max(substring(task_ref FROM %s)::numeric), 0) FROM tasks "
            "WHERE starts_with(task_ref, %s) AND substring(task_ref FROM %s) ~ '^[0-9]+$'",
            (len(prefix) + 1, prefix, len(prefix) + 1),
        )
        return prefix + str(int(rows[0][0]) + 1)

    def _ref_of(self, task_id: Any) -> str:
        key = card_transport_key(task_id)
        if key is None:
            raise SqlCardError(f"no Card transport key is {task_id!r}")
        rows = self._query("SELECT task_ref FROM tasks WHERE board_key = %s", (key,))
        if not rows:
            raise SqlCardError(f"no card carries transport key {key}")
        if len(rows) > 1:
            raise SqlCardError(f"two cards carry transport key {key}")
        return rows[0][0]

    @staticmethod
    def _card_keys(task_ids: list[Any]) -> list[int]:
        keys = []
        for task_id in task_ids:
            key = card_transport_key(task_id)
            if key is None:
                raise SqlCardError(f"no Card transport key is {task_id!r}")
            keys.append(key)
        return keys

    @staticmethod
    def _missing_card(keys: list[int], found: dict[int, Any]) -> None:
        for key in keys:
            if key not in found:
                raise SqlCardError(f"no card carries transport key {key}")

    def _rpc_createTask(
        self,
        *,
        project_id: int,
        title: str,
        description: str = "",
        column_id: int = 1,
        swimlane_id: int = 0,
        reference: str = "",
    ) -> Any:
        if int(project_id) == SPRINT_BOARD_ID:
            return self.sprints.create(
                title=title, description=description, reference=reference
            )
        if not reference:
            raise SqlCardError("the board store identifies a card by its reference (§9)")
        if self.records.kind_of_reference(reference) is not None:
            return self.records.create(
                title=title, description=description, reference=reference
            )
        number = _task_number_of(reference)
        lane = self._lane_name(swimlane_id)
        extensions: dict[str, Any] = {EXTENSION_BAG: {"swimlane": lane}} if lane else {}
        now = _now()
        rows = self._query(
            "INSERT INTO tasks (task_ref, task_number, title, description, state, archived, "
            "position, extensions, created_at, updated_at, date_moved) "
            "VALUES (%s, %s, %s, %s, %s, false, %s, %s::jsonb, %s, %s, %s) RETURNING board_key",
            (
                reference,
                number,
                title,
                description or "",
                _STATE_BY_COLUMN_ID[int(column_id)],
                self._next_position(_STATE_BY_COLUMN_ID[int(column_id)]),
                json.dumps(extensions),
                now,
                now,
                now,
            ),
        )
        self._commit_unless_nested()
        return int(rows[0][0])

    def _next_position(self, state: str) -> int:
        rows = self._query("SELECT coalesce(max(position), 0) + 1 FROM tasks WHERE state = %s", (state,))
        return int(rows[0][0])

    def _rpc_updateTask(self, *, id: int, **fields: Any) -> bool:
        kind = record_key_kind(id)
        if kind == "sprint":
            result = self.sprints.update(int(id), fields)
            self._commit_unless_nested()
            return result
        if kind is not None:
            result = self.records.update(int(id), fields)
            self._commit_unless_nested()
            return result
        ref = self._ref_of(id)
        assignments = []
        params: list[Any] = []
        for name in ("reference", "title", "description"):
            if name in fields:
                assignments.append(f"{'task_ref' if name == 'reference' else name} = %s")
                params.append(fields[name])
        if not assignments:
            return True
        if "reference" in fields:
            assignments.append("task_number = %s")
            params.append(_task_number_of(str(fields["reference"])))
        assignments.append("updated_at = %s")
        params.append(_now())
        params.append(ref)
        self._execute(f"UPDATE tasks SET {', '.join(assignments)} WHERE task_ref = %s", tuple(params))
        self._commit_unless_nested()
        return True

    def _rpc_moveTaskPosition(
        self, *, project_id: int, task_id: int, column_id: int, position: int, swimlane_id: int = 0
    ) -> bool:
        if record_key_kind(task_id) is not None:
            raise SqlCardError(
                "a Sprint, Product or Issue has no Pipeline card column to move to"
            )
        ref = self._ref_of(task_id)
        state = _STATE_BY_COLUMN_ID[int(column_id)]
        lane = self._lane_name(swimlane_id)
        self._execute(
            "UPDATE tasks SET state = %s, position = %s, updated_at = %s, date_moved = %s, "
            "extensions = CASE WHEN %s::text IS NULL THEN extensions "
            f"ELSE jsonb_set(coalesce(extensions, '{{}}'::jsonb), '{{{EXTENSION_BAG},swimlane}}', "
            "to_jsonb(%s::text), true) END "
            "WHERE task_ref = %s",
            (state, max(1, int(position)), _now(), _now(), lane, lane, ref),
        )
        self._commit_unless_nested()
        return True

    def _rpc_closeTask(self, *, task_id: int) -> bool:
        kind = record_key_kind(task_id)
        if kind == "sprint":
            # Sprint terminal state is a typed column, not archive state.
            return True
        if kind is not None:
            result = self.records.close(int(task_id))
            self._commit_unless_nested()
            return result
        ref = self._ref_of(task_id)
        self._execute(
            "UPDATE tasks SET archived = true, updated_at = %s WHERE task_ref = %s", (_now(), ref)
        )
        self._commit_unless_nested()
        return True

    # --- metadata --------------------------------------------------------------------

    def _rpc_getTaskMetadata(self, *, task_id: int) -> dict[str, str]:
        return self._metadata_of([task_id])[_batch_key(task_id)]

    def _card_metadata(self, task_ids: list[Any]) -> dict[int, dict[str, str]]:
        """Card metadata for every transport key: one `tasks` read and one per satellite."""
        keys = self._card_keys(task_ids)
        rows = {
            int(values[0]): values[1:]
            for values in self._query(
                "SELECT board_key, task_ref, project_id, task_type, claim_worker, slug, base_branch, "
                "seed_ref, complexity, family_preference, head_override, review_head_override, "
                "resolved_worker_head, resolved_review_head, routing_reason, codex_launch_mode, "
                "sprint_ref, retry_same, retry_switch, quota_snapshot_at, extensions, review, "
                "live_impact FROM tasks WHERE board_key = ANY(%s::bigint[])",
                (keys,),
            )
        }
        self._missing_card(keys, rows)
        refs = [str(rows[key][0]) for key in keys]
        heads = _grouped(self._query(
            "SELECT task_ref, head FROM task_retry_heads WHERE task_ref = ANY(%s::text[]) "
            "ORDER BY task_ref, ordinal",
            (refs,),
        ))
        blocked = _grouped(self._query(
            "SELECT task_ref, depends_on FROM task_dependencies WHERE task_ref = ANY(%s::text[]) "
            "ORDER BY task_ref, depends_on",
            (refs,),
        ))
        supersedes = _grouped(self._query(
            "SELECT task_ref, supersedes FROM task_supersessions WHERE task_ref = ANY(%s::text[])",
            (refs,),
        ))
        issues = _grouped(self._query(
            "SELECT task_ref, issue_id FROM task_issues WHERE task_ref = ANY(%s::text[]) "
            "ORDER BY task_ref, issue_id",
            (refs,),
        ))
        names = (
            "project",
            "task_type",
            "claim",
            "slug",
            "base_branch",
            "seed_ref",
            "complexity",
            "family_preference",
            "head",
            "review_head",
            "resolved_head",
            "resolved_review_head",
            "routing_reason",
            "codex_launch_mode",
            "sprint_ref",
        )
        result: dict[int, dict[str, str]] = {}
        for key in keys:
            ref, *values = rows[key]
            meta: dict[str, str] = {}
            for name, value in zip(names, values[: len(names)], strict=True):
                if value is not None and _text(value):
                    meta[name] = _text(value)
            for name, value in (("retry_same", values[15]), ("retry_switch", values[16])):
                if value:
                    meta[name] = str(value)
            if values[17] is not None:
                meta["quota_snapshot_at"] = _rfc3339(values[17])
            if values[19] is not None:
                meta["review"] = _text(values[19])
            if values[20]:
                meta["live_impact"] = "1"
            if heads.get(ref):
                meta["retry_heads"] = ",".join(heads[ref])
            if blocked.get(ref):
                meta["blocked_by"] = ",".join(blocked[ref])
            if supersedes.get(ref):
                meta["supersedes"] = supersedes[ref][0]
            if issues.get(ref):
                meta["issues"] = ",".join(f"issue:{issue_id}" for issue_id in issues[ref])
            bag = values[18] if isinstance(values[18], dict) else json.loads(values[18] or "{}")
            for name, value in (bag.get(EXTENSION_BAG) or {}).items():
                if name != "swimlane":
                    meta[name] = _text(value)
            # The table is the record type (`sql_product_issues.py`): a `tasks` row is a task
            # whatever its bag says; a stale bag value never renames it.
            meta["record_type"] = "task"
            result[key] = meta
        return result

    def _rpc_saveTaskMetadata(self, *, task_id: int, values: dict[str, Any]) -> bool:
        kind = record_key_kind(task_id)
        if kind == "sprint":
            result = self.sprints.save_metadata(int(task_id), values)
            self._commit_unless_nested()
            return result
        if kind is not None:
            result = self.records.save_metadata(int(task_id), values)
            self._commit_unless_nested()
            return result
        ref = self._ref_of(task_id)
        # The read states a `tasks` row's kind from the table; a bag value naming another kind
        # would be a Product or Issue written into the wrong table, so it is refused here.
        declared = _text(values.get("record_type"))
        if declared not in {"", "task"}:
            raise SqlCardError(f"card {ref} is a task; it cannot carry record_type {declared!r}")
        assignments: list[str] = []
        params: list[Any] = []
        bag_updates: dict[str, Any] = {}
        bag_removals: list[str] = []
        for key, raw in values.items():
            text = _text(raw)
            if key == "project" and text:
                # §3.1: `projects` only backs the foreign key; the registry files are canonical.
                # A fresh store has no rows at all, so the write that names an id inserts it,
                # with the id only, as a Product's project-set write does.
                _ensure_project_row(self, text)
            if key in _METADATA_COLUMNS:
                column = _METADATA_COLUMNS[key]
                assignments.append(f"{column} = %s")
                params.append(text or _ENUM_DEFAULTS.get(key))
                if text:
                    bag_removals.append(key)
                else:
                    bag_updates[key] = ""
            elif key == _METADATA_FLAG[0]:
                assignments.append(f"{_METADATA_FLAG[1]} = %s")
                params.append(live_impact_flag(text))
                bag_removals.append(key)
            elif key in _METADATA_COUNTERS:
                assignments.append(f"{_METADATA_COUNTERS[key]} = %s")
                params.append(int(text) if text.isdigit() else 0)
            elif key == _METADATA_TIMESTAMP[0]:
                assignments.append(f"{_METADATA_TIMESTAMP[1]} = %s")
                params.append(_timestamp(text))
                if text:
                    bag_removals.append(key)
                else:
                    bag_updates[key] = ""
            elif key in _METADATA_LINKS:
                self._write_link(ref, key, text)
                if text:
                    bag_removals.append(key)
                else:
                    bag_updates[key] = ""
            elif text:
                bag_updates[key] = text
            else:
                bag_removals.append(key)
        if bag_updates:
            assignments.append(
                f"extensions = jsonb_set(coalesce(extensions, '{{}}'::jsonb), '{{{EXTENSION_BAG}}}', "
                f"coalesce(extensions->'{EXTENSION_BAG}', '{{}}'::jsonb) || %s::jsonb, true)"
            )
            params.append(json.dumps(bag_updates))
        if assignments:
            assignments.append("updated_at = %s")
            params.append(_now())
            params.append(ref)
            self._execute(f"UPDATE tasks SET {', '.join(assignments)} WHERE task_ref = %s", tuple(params))
        for key in bag_removals:
            self._execute(
                "UPDATE tasks SET extensions = jsonb_set(coalesce(extensions, '{}'::jsonb), "
                f"'{{{EXTENSION_BAG}}}', coalesce(extensions->'{EXTENSION_BAG}', '{{}}'::jsonb) - %s, true) "
                "WHERE task_ref = %s",
                (key, ref),
            )
        self._commit_unless_nested()
        return True

    def _write_link(self, ref: str, key: str, text: str) -> None:
        """The three metadata keys that are satellite rows rather than a column (§3.5)."""
        if key == "retry_heads":
            self._execute("DELETE FROM task_retry_heads WHERE task_ref = %s", (ref,))
            for ordinal, head in enumerate(part for part in text.split(",") if part.strip()):
                self._execute(
                    "INSERT INTO task_retry_heads (task_ref, ordinal, head) VALUES (%s, %s, %s)",
                    (ref, ordinal, head.strip()),
                )
        elif key == "blocked_by":
            self._execute("DELETE FROM task_dependencies WHERE task_ref = %s", (ref,))
            for value in (part.strip() for part in text.split(",") if part.strip()):
                if value == ref:
                    continue
                self._execute(
                    "INSERT INTO task_dependencies (task_ref, depends_on, depends_on_task) "
                    "SELECT %s, %s, (SELECT task_ref FROM tasks WHERE task_ref = %s)",
                    (ref, value, value),
                )
        elif key == "supersedes":
            self._execute("DELETE FROM task_supersessions WHERE task_ref = %s", (ref,))
            if text and text != ref and self._query(
                "SELECT 1 FROM tasks WHERE task_ref = %s", (text,)
            ):
                self._execute(
                    "INSERT INTO task_supersessions (task_ref, supersedes, recorded_at) "
                    "VALUES (%s, %s, %s)",
                    (ref, text, _now()),
                )
        elif key == "issues":
            self._execute("DELETE FROM task_issues WHERE task_ref = %s", (ref,))
            for value in (part.strip() for part in text.split(",") if part.strip()):
                issue_id = value.removeprefix("issue:")
                self._execute(
                    "INSERT INTO task_issues (task_ref, issue_id) "
                    "SELECT %s, issue_id FROM issues WHERE issue_id = %s",
                    (ref, issue_id),
                )

    # --- the sprint e2e run budget (0023, secretary-1796) ----------------------------------

    def _rpc_getSprintE2eBudget(self, *, sprint_ref: str) -> dict[str, Any] | None:
        return self.sprints.e2e_budget(sprint_ref)

    def _rpc_chargeSprintE2e(self, *, sprint_ref: str, task_ref: str, dispatch_id: str, at: str) -> dict[str, Any]:
        return self.sprints.charge_e2e(sprint_ref, task_ref=task_ref, dispatch_id=dispatch_id, at=at)

    # --- comments --------------------------------------------------------------------

    def _rpc_getAllComments(self, *, task_id: int) -> list[dict[str, Any]]:
        return self._comments_of([task_id])[_batch_key(task_id)]

    def _card_comments(self, task_ids: list[Any]) -> dict[int, list[dict[str, Any]]]:
        """Card comments for every transport key, in one read after the key resolution."""
        keys = self._card_keys(task_ids)
        refs = {
            int(key): str(ref)
            for key, ref in self._query(
                "SELECT board_key, task_ref FROM tasks WHERE board_key = ANY(%s::bigint[])", (keys,)
            )
        }
        self._missing_card(keys, refs)
        comments = _grouped(self._query(
            "SELECT task_ref, comment_id, body, created_at FROM task_comments "
            "WHERE task_ref = ANY(%s::text[]) ORDER BY task_ref, created_at, comment_id",
            ([refs[key] for key in keys],),
        ))
        return {
            key: [
                {"id": identifier, "date_creation": _epoch(created), "comment": body}
                for identifier, body, created in comments.get(refs[key], [])
            ]
            for key in keys
        }

    def _rpc_createComment(
        self, *, task_id: int, content: str, user_id: int = 0, created_at: Any = None
    ) -> Any:
        kind = record_key_kind(task_id)
        if kind == "sprint":
            comment_id = self.sprints.create_comment(int(task_id), content, created_at=created_at)
            self._commit_unless_nested()
            return comment_id
        if kind is not None:
            comment_id = self.records.create_comment(int(task_id), content)
            self._commit_unless_nested()
            return comment_id
        ref = self._ref_of(task_id)
        first = content.splitlines()[0] if content else ""
        marker = first[1:-1] if first.startswith("[") and first.endswith("]") else None
        rows = self._query(
            "INSERT INTO task_comments (task_ref, marker, body, created_at) "
            "VALUES (%s, %s, %s, %s) RETURNING comment_id",
            (ref, marker, content, _timestamp(created_at) if created_at else _now()),
        )
        self._commit_unless_nested()
        return int(rows[0][0])


    def _rpc_removeTask(self, *, task_id: int) -> bool:
        result = self.sprints.remove(int(task_id))
        self._commit_unless_nested()
        return result


__all__ = ["BOARD_COLUMNS", "BOARD_ID", "BOARD_NAME", "CardSchemaOwed", "SqlCardClient", "SqlCardError"]
