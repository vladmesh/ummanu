"""`PoStore` keeps its connections between operations (INEF-12), hermetically.

The driver is a stand-in for `psycopg.connect`: each connection answers the schema gate's version
read and records every statement and transaction boundary, so the tests count connections opened,
gate reads made and transactions run without a server. The same store against a real PostgreSQL,
including a backend terminated mid-transaction, is `tests/test_po_reconnect_backend.py`.
"""

from __future__ import annotations

import gc
import socket
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest import mock

import psycopg

from ummanu.board.migrate import EXPECTED_SCHEMA_REVISION
from ummanu.board.schema_gate import VERSION_QUERY
from ummanu.po.store import IDLE_CONNECTIONS, PoSchemaOwed, PoStore, PoStoreError


class FakeConnection:
    """One driver connection: a version table, a transaction state and a socket a server can hang up."""

    def __init__(self, server: FakeServer) -> None:
        self.server = server
        self.closed = False
        self.broken = False
        self.statements: list[str] = []
        self.info = SimpleNamespace(transaction_status=SimpleNamespace(name="IDLE"))
        self._ours, self._theirs = socket.socketpair()

    def fileno(self) -> int:
        return self._ours.fileno()

    def hang_up(self) -> None:
        """What a terminated backend does to an idle connection: the socket becomes readable."""
        self._theirs.close()

    def execute(self, query: str, params: Any = None) -> Any:
        if self.closed or self.broken:
            raise psycopg.OperationalError("the connection is closed")
        self.statements.append(str(query))
        if self.server.fail_next:
            self.server.fail_next = False
            self.broken = True
            raise psycopg.OperationalError("terminating connection due to administrator command")
        rows = [(revision,) for revision in self.server.revisions] if query == VERSION_QUERY else []
        return SimpleNamespace(fetchall=lambda: rows, fetchone=lambda: rows[0] if rows else None)

    @contextmanager
    def transaction(self):
        self.statements.append("BEGIN")
        self.info.transaction_status.name = "INTRANS"
        try:
            yield
        except BaseException:
            if not self.broken:
                self.statements.append("ROLLBACK")
                self.info.transaction_status.name = "IDLE"
            raise
        self.statements.append("COMMIT")
        self.info.transaction_status.name = "IDLE"

    def close(self) -> None:
        self.closed = True
        self._ours.close()
        self._theirs.close()


class FakeServer:
    def __init__(self) -> None:
        self.revisions = [EXPECTED_SCHEMA_REVISION]
        self.connections: list[FakeConnection] = []
        self.fail_next = False

    def connect(self, conninfo: str, **options: Any) -> FakeConnection:
        assert options == {"autocommit": True}, options
        connection = FakeConnection(self)
        self.connections.append(connection)
        return connection

    def gate_reads(self) -> int:
        return sum(connection.statements.count(VERSION_QUERY) for connection in self.connections)


class PoStoreConnectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = FakeServer()
        self.enterContext(mock.patch("psycopg.connect", side_effect=self.server.connect))
        self.store = PoStore(SimpleNamespace(conninfo=lambda: "dbname=po"))
        self.addCleanup(self.store.close)

    def read(self) -> None:
        """One store operation: a request-id lookup, one SELECT in one transaction."""
        self.assertIsNone(self.store.request("rid"))

    def test_operations_one_after_another_share_one_connection_gated_once(self) -> None:
        for _ in range(3):
            self.read()
        [connection] = self.server.connections
        self.assertEqual(self.server.gate_reads(), 1)
        # Each operation is still its own transaction, around exactly its own statements.
        self.assertEqual(connection.statements.count("BEGIN"), 3)
        self.assertEqual(connection.statements.count("COMMIT"), 3)
        self.assertEqual(connection.statements[0], VERSION_QUERY)
        self.assertEqual(connection.statements[1], "BEGIN")

    def test_a_closed_connection_is_replaced_and_the_operation_succeeds(self) -> None:
        self.read()
        self.server.connections[0].closed = True
        self.read()
        self.assertEqual(len(self.server.connections), 2)
        self.assertEqual(self.server.gate_reads(), 2, "the new connection is gated on its own")
        self.assertEqual(self.server.connections[1].statements.count("COMMIT"), 1)

    def test_an_idle_connection_the_server_hung_up_on_is_replaced(self) -> None:
        self.read()
        self.server.connections[0].hang_up()
        self.read()
        self.assertEqual(len(self.server.connections), 2)
        self.assertTrue(self.server.connections[0].closed)
        self.assertEqual(self.server.connections[1].statements.count("COMMIT"), 1)

    def test_a_connection_broken_inside_an_operation_fails_it_and_is_never_kept(self) -> None:
        self.read()
        self.server.fail_next = True
        with self.assertRaises(PoStoreError) as failed:
            self.read()
        self.assertIn("did not answer", str(failed.exception))
        self.assertTrue(self.server.connections[0].closed)
        self.read()
        self.assertEqual(len(self.server.connections), 2)

    def test_a_refusal_inside_an_operation_rolls_back_and_keeps_the_connection(self) -> None:
        with self.assertRaises(LookupError), self.store._transaction() as connection:
            connection.execute("SELECT 1")
            raise LookupError("refused on the rows it read")
        self.read()
        [connection] = self.server.connections
        self.assertEqual(connection.statements.count("ROLLBACK"), 1)
        self.assertEqual(connection.statements.count("COMMIT"), 1)

    def test_a_store_that_owes_migrations_is_refused_and_keeps_nothing(self) -> None:
        self.server.revisions = []
        with self.assertRaises(PoSchemaOwed):
            self.read()
        self.assertTrue(self.server.connections[0].closed)
        self.assertEqual(self.server.connections[0].statements, [VERSION_QUERY], "no PO table is touched")
        self.server.revisions = [EXPECTED_SCHEMA_REVISION]
        self.read()
        self.assertEqual(len(self.server.connections), 2)

    def test_overlapping_operations_each_hold_their_own_connection(self) -> None:
        with self.store._transaction() as first, self.store._transaction() as second:
            self.assertIsNot(first, second)
        self.read()
        self.read()
        self.assertEqual(len(self.server.connections), 2, "both are kept and reused, none opened")

    def test_idle_connections_are_bounded_and_closed_with_the_store(self) -> None:
        with contextlib_nested(self.store, IDLE_CONNECTIONS + 2):
            pass
        kept = [connection for connection in self.server.connections if not connection.closed]
        self.assertEqual(len(kept), IDLE_CONNECTIONS)
        self.store.close()
        self.assertTrue(all(connection.closed for connection in self.server.connections))

    def test_a_dropped_store_closes_what_it_kept(self) -> None:
        store = PoStore(SimpleNamespace(conninfo=lambda: "dbname=po"))
        store.request("rid")
        del store
        gc.collect()
        self.assertTrue(all(connection.closed for connection in self.server.connections))


@contextmanager
def contextlib_nested(store: PoStore, depth: int):
    """`depth` operations open at once, so `depth` connections are borrowed and then given back."""
    if depth == 0:
        yield
        return
    with store._transaction(), contextlib_nested(store, depth - 1):
        yield


if __name__ == "__main__":
    unittest.main()
