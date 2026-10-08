"""`SqlCardClient`'s bounded connection pool (docs/BOARD_STORE.md §5.6).

The driver is a stand-in at `psycopg.connect`, as in `tests/test_sql_card_reconnect.py`: every
connection it hands out records its statements and keeps psycopg's local transaction state, so
the pool's rules are proven without a server.  The dropped-connection rule against a real
PostgreSQL is `tests/test_sql_card_reconnect_backend.py`.
"""

from __future__ import annotations

import socket
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from typing import Any, Self
from unittest import mock

import psycopg

from ummanu.board.migrate import EXPECTED_SCHEMA_REVISION
from ummanu.board.sql_cards import POOL_SIZE, SqlCardClient
from ummanu.tasks import TaskError

#: Long enough that a test waiting on it has certainly failed, short enough to fail fast.
_WAIT = 5.0


class _Credentials:
    def conninfo(self) -> str:
        return "host=board dbname=board user=app password=secret"


class _Cursor:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection
        self.rowcount = 0

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        connection = self.connection
        if connection.closed or connection.broken:
            raise psycopg.OperationalError("the connection is closed")
        if connection.info.transaction_status.name == "INERROR":
            raise psycopg.errors.InFailedSqlTransaction("current transaction is aborted")
        hook = connection.on_execute
        connection.statements.append(sql)
        connection.info.transaction_status = SimpleNamespace(name="INTRANS")
        if hook is not None:
            hook(sql)
        if sql == "FAIL":
            connection.info.transaction_status = SimpleNamespace(name="INERROR")
            raise psycopg.errors.UndefinedTable("no such table")
        self.rowcount = 1

    def fetchall(self) -> list[tuple[Any, ...]]:
        return [(self.connection.number,)]


class _Connection:
    def __init__(self, number: int, on_execute: Any = None) -> None:
        self.number = number
        self.on_execute = on_execute
        self.closed = False
        self.broken = False
        self.statements: list[str] = []
        self.commits = 0
        self.rollbacks = 0
        self.info = SimpleNamespace(transaction_status=SimpleNamespace(name="IDLE"))
        #: The schema gate's reads (`board.schema_gate`), kept apart from the board calls' statements.
        self.probes: list[str] = []

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        """The schema gate's version read: this stand-in is a store at this build's head."""
        self._alive()
        self.probes.append(sql)
        return SimpleNamespace(fetchall=lambda: [(EXPECTED_SCHEMA_REVISION,)])

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def _alive(self) -> None:
        if self.closed or self.broken:
            raise psycopg.OperationalError("the connection is lost")

    def commit(self) -> None:
        self._alive()
        self.commits += 1
        self.info.transaction_status = SimpleNamespace(name="IDLE")

    def rollback(self) -> None:
        self._alive()
        self.rollbacks += 1
        self.info.transaction_status = SimpleNamespace(name="IDLE")

    def close(self) -> None:
        self.closed = True


class PoolCase(unittest.TestCase):
    pool_size = POOL_SIZE
    pool_wait_seconds = _WAIT

    def setUp(self) -> None:
        self.opened: list[_Connection] = []
        #: Called with each statement's text on the thread running it; a test blocks reads here.
        self.on_execute: Any = None

        def connect(conninfo: str, **options: Any) -> _Connection:
            connection = _Connection(len(self.opened), lambda sql: self.on_execute and self.on_execute(sql))
            self.opened.append(connection)
            return connection

        self.enterContext(mock.patch("psycopg.connect", side_effect=connect))
        scratch = self.enterContext(tempfile.TemporaryDirectory())
        self.client = SqlCardClient(
            _Credentials(),  # type: ignore[arg-type]
            scratch,
            pool_size=self.pool_size,
            pool_wait_seconds=self.pool_wait_seconds,
        )

    def read(self, sql: str = "SELECT 1") -> int:
        """One read; answers the number of the connection that carried it."""
        return int(self.client._query(sql)[0][0])

    def threads(self, target: Any, count: int) -> list[BaseException]:
        """Run `target(index)` on `count` threads at once; answers what they raised."""
        failures: list[BaseException] = []

        def run(index: int) -> None:
            try:
                target(index)
            except BaseException as exc:  # noqa: BLE001 - reported to the test.
                failures.append(exc)

        workers = [threading.Thread(target=run, args=(index,)) for index in range(count)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=_WAIT * 2)
        self.assertFalse(any(worker.is_alive() for worker in workers), "a thread never finished")
        return failures


class ConcurrencyTests(PoolCase):
    def test_four_threads_read_at_once_and_none_waits_for_another(self) -> None:
        # Each read holds its connection until all four are inside a statement together, which
        # can only happen if none of them is queued behind another.
        together = threading.Barrier(4, timeout=_WAIT)
        self.on_execute = lambda _sql: together.wait()
        carried: list[int] = []
        failures = self.threads(lambda _index: carried.append(self.read()), 4)
        self.assertEqual(failures, [])
        self.assertEqual(sorted(carried), [0, 1, 2, 3])
        self.assertEqual(len(self.opened), 4)

    def test_a_single_thread_uses_one_connection(self) -> None:
        for _ in range(5):
            self.assertEqual(self.read(), 0)
        with self.client.transaction():
            self.client._execute("UPDATE tasks SET title = title")
        self.client.call("getColumns", project_id=1)
        self.assertEqual(len(self.opened), 1)

    def test_a_read_returns_its_connection_with_no_transaction_open(self) -> None:
        self.read()
        connection = self.opened[0]
        self.assertEqual(connection.info.transaction_status.name, "IDLE")
        self.assertEqual(connection.commits, 1)
        self.assertEqual(self.client._idle, [connection])

    def test_a_failed_statement_is_rolled_back_before_the_connection_is_reused(self) -> None:
        with self.assertRaises(TaskError) as refused:
            self.read("FAIL")
        self.assertEqual(refused.exception.code, "backend_error")
        # Aborted, not dead: rolled back and returned, and the next read works on it.
        self.assertEqual(self.read(), 0)
        self.assertEqual(len(self.opened), 1)
        self.assertGreaterEqual(self.opened[0].rollbacks, 1)

    def test_a_write_and_its_commit_share_one_connection_while_others_read(self) -> None:
        # A board call is one session: its statements and its commit stay on one connection even
        # when another thread reads in between.
        inside = threading.Event()
        resume = threading.Event()

        def pause(sql: str) -> None:
            if sql.startswith("UPDATE") and threading.current_thread().name == "writer":
                inside.set()
                resume.wait(_WAIT)

        self.on_execute = pause

        def write() -> None:
            with self.client._session():
                self.client._execute("UPDATE tasks SET title = 'a'")
                self.client._commit_unless_nested()

        writer = threading.Thread(target=write, name="writer")
        writer.start()
        self.assertTrue(inside.wait(_WAIT))
        self.assertEqual(self.read(), 1, "the reader does not wait for the writer's connection")
        resume.set()
        writer.join(_WAIT)
        self.assertEqual(self.opened[0].statements, ["UPDATE tasks SET title = 'a'"])
        self.assertEqual(self.opened[0].commits, 1)


class ExhaustionTests(PoolCase):
    pool_size = 2
    pool_wait_seconds = 0.3

    def hold(self, count: int) -> tuple[threading.Event, list[threading.Thread]]:
        """`count` threads, each holding a pooled connection until the event is set."""
        release = threading.Event()
        holding = threading.Barrier(count + 1, timeout=_WAIT)

        def keep() -> None:
            with self.client._session():
                self.client._query("SELECT 1")
                holding.wait()
                release.wait(_WAIT)

        holders = [threading.Thread(target=keep) for _ in range(count)]
        for holder in holders:
            holder.start()
        holding.wait()
        return release, holders

    def test_an_exhausted_pool_waits_then_fails_as_backend_unavailable(self) -> None:
        release, holders = self.hold(2)
        try:
            started = time.monotonic()
            with self.assertRaises(TaskError) as refused:
                self.read()
            waited = time.monotonic() - started
        finally:
            release.set()
            for holder in holders:
                holder.join(_WAIT)
        self.assertEqual(refused.exception.code, "backend_unavailable")
        self.assertIn("2 board store connections", refused.exception.message)
        self.assertGreaterEqual(waited, self.pool_wait_seconds)
        self.assertLess(waited, _WAIT)
        self.assertEqual(len(self.opened), 2, "the bound is never exceeded")
        # Once the holders are done, the pool serves again from what it has.
        self.read()
        self.assertEqual(len(self.opened), 2)

    def test_a_waiting_caller_gets_the_first_connection_returned(self) -> None:
        self.pool_wait_seconds = _WAIT
        self.client.pool_wait_seconds = _WAIT
        release, holders = self.hold(2)
        carried: list[int] = []
        waiter = threading.Thread(target=lambda: carried.append(self.read()))
        waiter.start()
        time.sleep(0.1)
        self.assertEqual(carried, [], "the waiter is waiting")
        release.set()
        waiter.join(_WAIT)
        for holder in holders:
            holder.join(_WAIT)
        self.assertEqual(len(carried), 1)
        self.assertEqual(len(self.opened), 2)


class TransactionPinningTests(PoolCase):
    def test_checkpoint_read_snapshot_sets_isolation_before_every_projection_read(self) -> None:
        with self.client.read_snapshot():
            connection = self.client._connection
            self.read("SELECT cards")
            self.read("SELECT metadata")
            self.read("SELECT comments")
            self.read("SELECT audit")
            self.read("SELECT sprints")
            self.assertIs(self.client._connection, connection)
            self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.statements, [
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY", "SELECT cards",
            "SELECT metadata", "SELECT comments", "SELECT audit", "SELECT sprints",
        ])
        self.assertEqual(connection.commits, 1)
        self.assertIsNone(self.client._connection)

    def test_checkpoint_read_snapshot_refuses_a_mutation_transaction_and_rolls_back_errors(self) -> None:
        with self.client.transaction(), self.assertRaisesRegex(TaskError, "outside a mutation"), self.client.read_snapshot():
            self.fail("snapshot entered a mutation")
        with self.assertRaisesRegex(ValueError, "cut failed"), self.client.read_snapshot():
            connection = self.client._connection
            self.read()
            raise ValueError("cut failed")
        self.assertEqual(connection.rollbacks, 1)
        self.assertIsNone(self.client._connection)

    def test_a_transaction_holds_one_connection_across_nested_calls(self) -> None:
        self.read()
        with self.client.transaction():
            first = self.client._connection
            self.client._execute("UPDATE tasks SET title = 'a'")
            with self.client.transaction():
                self.assertIs(self.client._connection, first)
                self.client._execute("UPDATE tasks SET title = 'b'")
                self.client.call("getColumns", project_id=1)
                self.read()
            self.client._commit_unless_nested()
            self.assertIs(self.client._connection, first)
            self.assertEqual(first.commits, 1, "the nested commit is the transaction's")
        self.assertEqual(len(self.opened), 1)
        self.assertEqual(first.commits, 2)
        self.assertEqual(first.statements[1:3], ["UPDATE tasks SET title = 'a'", "UPDATE tasks SET title = 'b'"])
        self.assertIsNone(self.client._connection, "unpinned when the transaction ends")

    def test_a_read_on_another_thread_does_not_join_or_wait_for_a_transaction(self) -> None:
        carried: list[int] = []
        depth_there: list[int] = []
        with self.client.transaction():
            self.client._execute("UPDATE tasks SET title = 'a'")

            def elsewhere() -> None:
                depth_there.append(self.client._depth)
                carried.append(self.read())

            reader = threading.Thread(target=elsewhere)
            reader.start()
            reader.join(_WAIT)
        self.assertEqual(depth_there, [0])
        self.assertEqual(carried, [1])
        self.assertEqual(self.opened[1].statements, ["SELECT 1"])

    def test_transactions_of_two_threads_take_turns(self) -> None:
        order: list[str] = []

        def second() -> None:
            with self.client.transaction():
                order.append("second")

        with self.client.transaction():
            other = threading.Thread(target=second)
            other.start()
            # Time for the second thread to take its connection and queue behind this transaction.
            time.sleep(0.2)
            order.append("first")
        other.join(_WAIT)
        self.assertEqual(order, ["first", "second"])
        self.assertEqual(len(self.opened), 2)


class DeadConnectionTests(PoolCase):
    def test_a_connection_closed_between_two_reads_is_replaced_by_the_next_read(self) -> None:
        self.assertEqual(self.read(), 0)
        self.opened[0].closed = True
        self.assertEqual(self.read(), 1)
        self.assertEqual(self.opened[1].statements, ["SELECT 1"])

    def test_a_connection_reported_broken_between_two_reads_is_replaced_by_the_next_read(self) -> None:
        self.assertEqual(self.read(), 0)
        self.opened[0].broken = True
        self.assertEqual(self.read(), 1)
        self.assertEqual(self.client._open, 1)

    def test_a_connection_whose_server_hung_up_is_replaced_by_the_next_read(self) -> None:
        # psycopg does not know yet: the socket has the server's farewell waiting on it.
        client_end, server_end = socket.socketpair()
        self.addCleanup(client_end.close)
        self.read()
        self.opened[0].fileno = client_end.fileno  # type: ignore[attr-defined]
        self.assertEqual(self.read(), 0, "a quiet socket is a live connection")
        server_end.close()
        self.assertEqual(self.read(), 1)
        self.assertTrue(self.opened[0].closed)

    def test_a_dead_connection_is_never_handed_out_twice(self) -> None:
        # Three connections idle in the pool, two of them dead.
        together = threading.Barrier(3, timeout=_WAIT)
        self.on_execute = lambda _sql: together.wait()
        self.assertEqual(self.threads(lambda _index: self.read(), 3), [])
        self.opened[0].closed = True
        self.opened[2].broken = True
        # Three reads at once take every idle connection there is, dead ones included.
        carried: list[int] = []
        self.assertEqual(self.threads(lambda _index: carried.append(self.read()), 3), [])
        self.on_execute = None
        carried += [self.read() for _ in range(6)]
        self.assertNotIn(0, carried)
        self.assertNotIn(2, carried)
        self.assertEqual(len(self.opened), 5, "each dead one was replaced once")
        self.assertEqual(self.client._open, 3)
        self.assertNotIn(self.opened[0], self.client._idle)
        self.assertNotIn(self.opened[2], self.client._idle)
        self.assertTrue(self.opened[2].closed)

    def test_a_connection_that_dies_under_a_read_fails_it_and_is_not_returned(self) -> None:
        self.read()

        def die(_sql: str) -> None:
            self.opened[0].broken = True
            raise psycopg.OperationalError("server closed the connection unexpectedly")

        self.on_execute = die
        with self.assertRaises(TaskError) as refused:
            self.read()
        self.on_execute = None
        self.assertEqual(refused.exception.code, "backend_unavailable")
        self.assertEqual(self.client._idle, [])
        self.assertEqual(self.client._open, 0)
        self.assertEqual(self.read(), 1)

    def test_close_closes_the_idle_connections(self) -> None:
        together = threading.Barrier(2, timeout=_WAIT)
        self.on_execute = lambda _sql: together.wait()
        self.assertEqual(self.threads(lambda _index: self.read(), 2), [])
        self.client.close()
        self.assertTrue(all(connection.closed for connection in self.opened))
        self.assertEqual((self.client._idle, self.client._open), ([], 0))


class StagedStateTests(PoolCase):
    """Staged creates and added lanes belong to the transaction of the thread that made them.

    The store here is empty, so every row a read returns comes from staged client state; the
    rows a transaction has not committed must never reach another thread's read.
    """

    def setUp(self) -> None:
        super().setUp()
        self.enterContext(mock.patch.object(_Cursor, "fetchall", return_value=[]))

    def refs(self, project_id: int) -> list[str]:
        return [row["reference"] for row in self.client.call("getAllTasks", project_id=project_id)]

    def on_another_thread(self, read: Any) -> Any:
        answers: list[Any] = []
        failures = self.threads(lambda _index: answers.append(read()), 1)
        self.assertEqual(failures, [])
        return answers[0]

    def check_isolated(self, project_id: int, reference: str) -> None:
        def by_reference() -> Any:
            return self.client.call("getTaskByReference", project_id=project_id, reference=reference)

        with self.assertRaises(RuntimeError), self.client.transaction():
            self.client.call("createTask", project_id=project_id, title="probe", reference=reference)
            # The writer sees its own staged create.
            self.assertIn(reference, self.refs(project_id))
            self.assertIsNotNone(by_reference())
            # A reader on another thread, on its own pooled connection, does not.
            self.assertNotIn(reference, self.on_another_thread(lambda: self.refs(project_id)))
            self.assertIsNone(self.on_another_thread(by_reference))
            raise RuntimeError("the writer rolls back")
        # Rolled back: neither the writer's thread nor another one sees it.
        self.assertNotIn(reference, self.refs(project_id))
        self.assertIsNone(by_reference())
        self.assertNotIn(reference, self.on_another_thread(lambda: self.refs(project_id)))

    def test_a_staged_sprint_is_invisible_to_another_thread_and_gone_after_rollback(self) -> None:
        self.check_isolated(2, "sprint:review-probe")

    def test_a_staged_product_and_issue_are_invisible_to_another_thread(self) -> None:
        self.check_isolated(1, "product:probe")
        self.check_isolated(1, "issue:probe")

    def test_a_create_outside_a_transaction_stays_its_own_threads(self) -> None:
        # The two-call create (createTask, then saveTaskMetadata) outside a transaction: the
        # staging thread keeps seeing its create between the calls, and no other thread does.
        for project_id, reference in ((2, "sprint:loose"), (1, "product:loose")):
            with self.subTest(reference=reference):
                self.client.call("createTask", project_id=project_id, title="loose", reference=reference)
                self.assertIn(reference, self.refs(project_id))
                elsewhere = self.on_another_thread(lambda board=project_id: self.refs(board))
                self.assertNotIn(reference, elsewhere)

    def test_an_unfinished_create_still_refuses_the_commit_and_leaves_nothing_staged(self) -> None:
        with self.assertRaises(TaskError) as refused, self.client.transaction():
            self.client.call("createTask", project_id=2, title="probe", reference="sprint:unfinished")
        self.assertIn("sprint:unfinished", refused.exception.message)
        self.assertEqual(self.refs(2), [])
        self.assertEqual((self.client.records.staged, self.client.sprints.staged), ({}, {}))

    def lanes(self) -> list[str]:
        return [lane["name"] for lane in self.client.call("getActiveSwimlanes", project_id=1)]

    def test_a_lane_a_transaction_adds_is_its_own_until_it_commits(self) -> None:
        with self.client.transaction():
            self.client.call("addSwimlane", project_id=1, name="lane-a")
            self.assertEqual(self.lanes(), ["lane-a"])
            self.assertEqual(self.on_another_thread(self.lanes), [])
        self.assertEqual(self.on_another_thread(self.lanes), ["lane-a"])
        self.assertEqual(self.lanes(), ["lane-a"])

    def test_a_lane_a_rolled_back_transaction_added_is_seen_by_no_one(self) -> None:
        self.assertEqual(self.lanes(), [])
        with self.assertRaises(RuntimeError), self.client.transaction():
            self.client._lane_added("product-b")
            self.assertEqual(self.on_another_thread(self.lanes), [])
            raise RuntimeError("rolled back")
        self.assertEqual(self.lanes(), [])
        self.assertEqual(self.on_another_thread(self.lanes), [])


if __name__ == "__main__":
    unittest.main()
