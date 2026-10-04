"""The schema gate against a real PostgreSQL (`board/schema_gate.py`, docs/BOARD_STORE.md §7.4).

Each test gets databases of its own in the suite's throwaway `postgres:16`
(`tests/sql_backend_fixtures.py`): one migrated only to an earlier packaged revision, one never
migrated, one at the head. Every operational entry point is asked against them through the
connection boundary it really uses — `SqlCardClient` for cards, sprints, products/issues, the SQL
audit and `SqlBoardHost`; `PoStore` and `OwnerEventStore` over connections of their own — and doctor
reads the same assessment. Nothing here reaches a live installation.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tests.sql_backend_fixtures import OWNER, PostgresBoard
from ummanu.board import migrate, owner_events, schema_gate
from ummanu.board.host import EntityKind
from ummanu.board.owner_events import OwnerEventsSchemaOwed, OwnerEventStore, OwnerEventsUnavailable
from ummanu.board.sql_cards import CardSchemaOwed, SqlCardClient
from ummanu.board.sql_host import SqlBoardHost
from ummanu.board.store import STORE_FILE, BoardStoreConfig
from ummanu.cli import main
from ummanu.po.store import PoSchemaOwed, PoStore, PoStoreError
from ummanu.product_issues import ProductIssueStore
from ummanu.sprints import SprintReader
from ummanu.tasks import TaskError, TaskReader, task_audit_for

#: The earlier packaged revision the stale store stops at, and what it therefore owes.
STALE = "0020_wait_card_kind"
OWED = (
    "0021_delegated_card_settled",
    "0022_origin_returns",
    "0023_sprint_e2e_budget",
    "0024_e2e_after_merge_kind",
    "0025_card_waits_for_person",
    "0026_sprint_local_runs",
    "0027_sprint_owner_decisions",
)


class SchemaGateBackendTests(unittest.TestCase):
    board: PostgresBoard

    @classmethod
    def setUpClass(cls) -> None:
        cls.board = PostgresBoard.shared()
        # The template migration is what creates the cluster-wide app and read roles; every
        # database below reuses them.
        current = cls.board.fresh_database()
        cls.board.release_database(current.dbname)

    def setUp(self) -> None:
        self.scratch = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="schema-gate-")))
        self._serial = 0

    # --- fixtures ----------------------------------------------------------------------------

    def database(self, revision: str | None) -> BoardStoreConfig:
        """A database of this test's own, migrated to `revision` (None: never migrated)."""
        import psycopg

        self._serial += 1
        name = f"schema_gate_{os.getpid()}_{id(self) % 100000}_{self._serial}"
        with psycopg.connect(self.board.config("postgres").for_role("owner").conninfo(), autocommit=True) as c:
            c.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
            c.execute(f"CREATE DATABASE {name} OWNER {OWNER}")
        self.addCleanup(self.board.drop_database, name)
        config = self.board.config(name)
        if revision is not None:
            self.upgrade(config, revision)
        return config

    def upgrade(self, config: BoardStoreConfig, revision: str = "heads") -> None:
        import sqlalchemy as sa
        from alembic import command

        engine = sa.create_engine(migrate.sqlalchemy_url(config.for_role("owner")))
        try:
            with engine.connect() as connection:
                command.upgrade(
                    migrate.alembic_config(
                        connection=connection,
                        passwords=migrate.passwords_for(config),
                        reuse_existing_roles=True,
                    ),
                    revision,
                )
                connection.commit()
        finally:
            engine.dispose()

    def client(self, config: BoardStoreConfig, role: str = "app") -> SqlCardClient:
        client = SqlCardClient(config.for_role(role), self.scratch)
        self.addCleanup(client.close)
        return client

    def sessions(self, config: BoardStoreConfig) -> list[tuple[str, str]]:
        """The (user, state) of every other session on the database, as the server lists them."""
        import psycopg

        with psycopg.connect(config.for_role("owner").conninfo(), autocommit=True) as c:
            return [
                (str(user), str(state))
                for user, state in c.execute(
                    "SELECT usename, coalesce(state, '') FROM pg_stat_activity "
                    "WHERE datname = current_database() AND pid <> pg_backend_pid()"
                ).fetchall()
            ]

    def await_no_sessions(self, config: BoardStoreConfig) -> None:
        """A closed client connection's backend exits just after the socket closes."""
        deadline = time.monotonic() + 5
        while self.sessions(config) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.sessions(config), [], "a refused connection left a session behind")

    @contextlib.contextmanager
    def statements(self) -> Any:
        """Every statement any psycopg cursor of this process runs inside the block."""
        import psycopg

        seen: list[str] = []
        original = psycopg.Cursor.execute

        def recording(cursor: Any, query: Any, *args: Any, **kwargs: Any) -> Any:
            seen.append(str(query))
            return original(cursor, query, *args, **kwargs)

        with mock.patch.object(psycopg.Cursor, "execute", recording):
            yield seen

    def assert_refused(self, raised: BaseException, *, actual: str | None, pending: tuple[str, ...]) -> None:
        self.assertIsInstance(raised, schema_gate.SchemaOwed)
        self.assertEqual(raised.code, "schema_owed")  # type: ignore[attr-defined]
        self.assertEqual(raised.actual, actual)  # type: ignore[attr-defined]
        self.assertEqual(raised.expected, migrate.EXPECTED_SCHEMA_REVISION)  # type: ignore[attr-defined]
        self.assertEqual(raised.pending, pending)  # type: ignore[attr-defined]
        for name in pending:
            self.assertIn(name, str(raised))

    def instance(self, config: BoardStoreConfig) -> Path:
        """An installation whose `board-store.env` names `config`, for the CLI and doctor."""
        instance = self.scratch / f"instance-{config.dbname}"
        instance.mkdir()
        (instance / "instance.yaml").write_text(
            "version: 1\nname: schema-gate\n"
            f"data_dir: {self.scratch / ('data-' + config.dbname)}\n"
            "offsite:\n  instance_remote: git@example.invalid:x/y\n",
            encoding="utf-8",
        )
        store = instance / STORE_FILE
        store.write_text("".join(f"{key}={value}\n" for key, value in config.as_environ().items()), encoding="utf-8")
        store.chmod(0o600)
        return instance

    # --- the card client ---------------------------------------------------------------------

    def test_a_stale_store_is_refused_before_any_entity_statement(self) -> None:
        config = self.database(STALE)
        client = self.client(config)

        with self.statements() as seen, self.assertRaises(CardSchemaOwed) as raised:
            TaskReader(client).list()

        self.assert_refused(raised.exception, actual=STALE, pending=OWED)
        self.assertIsInstance(raised.exception, TaskError)
        self.assertEqual(seen, [schema_gate.VERSION_QUERY], "only the version table was read")
        self.assertIn(f"at schema revision {STALE}", raised.exception.message)

    def test_a_stale_store_is_refused_before_it_is_asked_for_a_column_it_lacks(self) -> None:
        # 0023 adds the sprints' e2e budget. Without the gate this read is an UndefinedColumn.
        config = self.database("0022_origin_returns")

        with self.assertRaises(CardSchemaOwed) as raised:
            SprintReader(self.client(config)).list(create=False)

        self.assert_refused(raised.exception, actual="0022_origin_returns", pending=OWED[2:])

    def test_a_store_never_migrated_is_explained_not_an_undefined_table(self) -> None:
        config = self.database(None)

        with self.assertRaises(CardSchemaOwed) as raised:
            TaskReader(self.client(config)).list()

        self.assert_refused(raised.exception, actual=None, pending=migrate.lineage())
        self.assertIn("no schema at all", raised.exception.message)
        self.assertNotIn("UndefinedTable", raised.exception.message)
        self.await_no_sessions(config)

    def test_a_current_store_reads_for_the_app_and_the_read_role(self) -> None:
        config = self.database("heads")
        for role in ("app", "read"):
            with self.subTest(role=role):
                client = self.client(config, role)
                self.assertEqual(TaskReader(client).list(), [])
                self.assertEqual(SprintReader(client).list(create=False), [])
                with client._session():
                    state = client.connection.info.transaction_status.name
                self.assertEqual(state, "IDLE")

    def test_every_entry_point_over_the_client_answers_the_same_refusal(self) -> None:
        config = self.database(STALE)
        client = self.client(config)
        entry_points = {
            "cards": lambda: TaskReader(client).list(),
            "card show": lambda: TaskReader(client).show("ummanu-1"),
            "sprints": lambda: SprintReader(client).list(create=False),
            "products": lambda: ProductIssueStore(client, data_dir=self.scratch, instance=self.scratch).list_products(),
            "issues": lambda: ProductIssueStore(client, data_dir=self.scratch, instance=self.scratch).show_issue(
                "issue:0123456789abcdef0123"
            ),
            "audit": lambda: task_audit_for(client).pending_events(),
            "host cards": lambda: SqlBoardHost(client).list(EntityKind.CARD),
            "host sprint": lambda: SqlBoardHost(client).read(EntityKind.SPRINT, "sprint:1"),
            "transaction": lambda: client.transaction().__enter__(),
        }
        for name, call in entry_points.items():
            with self.subTest(entry_point=name), self.assertRaises(CardSchemaOwed) as raised:
                call()
            self.assert_refused(raised.exception, actual=STALE, pending=OWED)
        self.assertEqual((client._open, client._idle), (0, []), "no connection is held or pooled")
        self.await_no_sessions(config)

    def test_a_refusal_holds_nothing_and_the_same_client_reads_once_migrated(self) -> None:
        config = self.database(STALE)
        client = self.client(config)
        with self.assertRaises(CardSchemaOwed):
            TaskReader(client).list()
        self.assertEqual((client._open, client._idle), (0, []))
        self.await_no_sessions(config)

        self.upgrade(config)

        self.assertEqual(TaskReader(client).list(), [], "a refusal is not remembered")
        self.assertEqual(client._open, 1)
        self.assertEqual(
            [state for user, state in self.sessions(config) if user == config.app_user], ["idle"]
        )

    def test_a_failing_read_rolls_back_and_the_next_read_reuses_the_connection(self) -> None:
        config = self.database("heads")
        client = self.client(config)
        [(pid,)] = client._query("SELECT pg_backend_pid()")

        with self.assertRaises(TaskError) as raised:
            client._query("SELECT * FROM no_such_table")
        self.assertEqual(raised.exception.code, "backend_error")
        self.assertEqual(
            [state for user, state in self.sessions(config) if user == config.app_user], ["idle"]
        )

        self.assertEqual(client._query("SELECT pg_backend_pid()"), [(pid,)])
        self.assertEqual(TaskReader(client).list(), [])
        self.assertEqual(client._open, 1)

    def test_a_revision_this_build_does_not_know_is_read_as_a_later_builds_schema(self) -> None:
        import psycopg

        config = self.database("heads")
        with psycopg.connect(config.for_role("owner").conninfo()) as c:
            c.execute("UPDATE alembic_version SET version_num = '0099_a_later_build'")

        self.assertEqual(TaskReader(self.client(config)).list(), [])
        self.assertEqual(PoStore(config.for_role("app")).sessions(), [])
        self.assertEqual(schema_gate.inspect_instance(self.instance(config))["state"], schema_gate.AHEAD)

    def test_the_cli_answers_the_refusal_as_a_named_error_not_a_traceback(self) -> None:
        instance = self.instance(self.database(STALE))
        output, errors = io.StringIO(), io.StringIO()

        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main(["task", "list", "--instance", str(instance)])

        text = output.getvalue() + errors.getvalue()
        self.assertNotEqual(code, 0)
        self.assertIn("schema_owed", text)
        self.assertIn(OWED[-1], text)
        self.assertNotIn("Traceback", text)

    # --- the stores with connections of their own ----------------------------------------------

    def test_the_po_store_refuses_a_stale_store_in_its_own_family(self) -> None:
        config = self.database(STALE)
        store = PoStore(config.for_role("app"))

        with self.statements() as seen, self.assertRaises(PoSchemaOwed) as raised:
            store.sessions()

        self.assertIsInstance(raised.exception, PoStoreError)
        self.assert_refused(raised.exception, actual=STALE, pending=OWED)
        self.assertEqual(seen, [schema_gate.VERSION_QUERY])
        self.await_no_sessions(config)

        self.upgrade(config)
        self.assertEqual(store.sessions(), [])

    def test_the_owner_event_store_refuses_a_stale_store_and_the_bell_writer_swallows_it(self) -> None:
        config = self.database("0017_po_card_kinds")
        store = OwnerEventStore(config.for_role("app"))
        owed = migrate.lineage()[migrate.lineage().index("0018_owner_events") :]

        with self.assertRaises(OwnerEventsSchemaOwed) as raised:
            store.unread_count()
        self.assertIsInstance(raised.exception, OwnerEventsUnavailable)
        self.assert_refused(raised.exception, actual="0017_po_card_kinds", pending=owed)

        with self.assertLogs("ummanu.board.owner_events", level="WARNING") as logged:
            self.assertFalse(owner_events.record("sprint_closed", "sprint:1", "x", "k", to=store))
        self.assertIn("OwnerEventsSchemaOwed", "\n".join(logged.output))
        self.assertEqual(
            owner_events.record_strict("sprint_closed", "sprint:1", "x", "k2", to=store), owner_events.FAILED
        )
        self.await_no_sessions(config)

    def test_an_owner_event_joined_to_a_client_transaction_is_refused_with_the_client(self) -> None:
        config = self.database(STALE)
        client = self.client(config)
        store = OwnerEventStore(config.for_role("app"), client=client)

        with self.assertRaises(CardSchemaOwed) as raised, client.transaction():
            store.insert("sprint_closed", "sprint:1", "x", "k")
        self.assert_refused(raised.exception, actual=STALE, pending=OWED)

        self.upgrade(config)
        with client.transaction():
            self.assertTrue(store.insert("sprint_closed", "sprint:1", "x", "k"))
        self.assertEqual(OwnerEventStore(config.for_role("read")).unread_count(), 1)

    # --- doctor -----------------------------------------------------------------------------

    def doctor(self, instance: Path, *extra: str) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["doctor", "--dry-run", *extra, "--instance", str(instance)])
        return code, output.getvalue()

    def test_doctor_reports_an_owed_schema_in_text_and_json_alike(self) -> None:
        config = self.database(STALE)
        instance = self.instance(config)

        text_code, text = self.doctor(instance)
        json_code, raw = self.doctor(instance, "--json")
        payload = json.loads(raw)

        self.assertEqual((text_code, json_code), (1, 1), text)
        self.assertIn(f"board schema: owed: the board store is at schema revision {STALE}", text)
        self.assertIn(f"board schema pending: {', '.join(OWED)}", text)
        self.assertIn("status: findings", text)
        [finding] = [f for f in payload["findings"] if f["code"] == "schema_owed"]
        self.assertEqual(finding["actual"], STALE)
        self.assertEqual(finding["expected"], migrate.EXPECTED_SCHEMA_REVISION)
        self.assertEqual(finding["pending"], list(OWED))
        self.assertEqual(payload["board_schema"]["pending"], list(OWED))
        self.assertEqual(payload["board_schema"]["message"], text.split("board schema: owed: ", 1)[1].split("\n")[0])
        self.assertFalse(payload["ok"])
        self.await_no_sessions(config)

    def test_doctor_adds_nothing_for_a_current_schema(self) -> None:
        instance = self.instance(self.database("heads"))

        _code, text = self.doctor(instance)
        payload = json.loads(self.doctor(instance, "--json")[1])

        self.assertIn(f"board schema: current at {migrate.EXPECTED_SCHEMA_REVISION}", text)
        self.assertEqual(payload["board_schema"]["state"], "current")
        codes = {finding["code"] for finding in payload["findings"]}
        self.assertFalse(codes & {"schema_owed", "board_schema_unavailable"}, payload["findings"])

    def test_doctor_reports_a_schema_it_cannot_read(self) -> None:
        config = self.database("heads")
        instance = self.instance(
            dataclasses.replace(config, read_password="not-the-read-password")
        )

        code, text = self.doctor(instance)
        payload = json.loads(self.doctor(instance, "--json")[1])

        self.assertEqual(code, 1)
        self.assertIn("board schema: unavailable: the board store schema could not be read", text)
        [finding] = [f for f in payload["findings"] if f["code"] == "board_schema_unavailable"]
        self.assertIn("could not be read", finding["message"])

    def test_doctor_offline_and_on_a_fixture_reads_no_live_store_in_text_or_json(self) -> None:
        import psycopg

        instance = self.instance(self.database(STALE))
        fixture = self.scratch / "host-fixture"
        fixture.mkdir()
        original = psycopg.connect
        connections: list[str] = []

        def connect(*args: Any, **kwargs: Any) -> Any:
            connections.append(str(args[0] if args else kwargs.get("conninfo", "")))
            return original(*args, **kwargs)

        for flags in (("--offline",), ("--host-fixture", str(fixture))):
            for renderer in ((), ("--json",)):
                with self.subTest(flags=flags, renderer=renderer):
                    connections.clear()
                    with mock.patch("psycopg.connect", side_effect=connect), self.statements() as seen:
                        _, output = self.doctor(instance, *flags, *renderer)
                    self.assertEqual((connections, seen), ([], []), "no board connection or query")
                    self.assertNotIn("schema_owed", output)
                    reason = f"{flags[0]} reads no live board store"
                    if renderer:
                        payload = json.loads(output)
                        self.assertEqual(payload["board_schema"]["state"], "not_inspected")
                        self.assertEqual(payload["board_schema"]["reason"], reason)
                        sprints = payload["status"]["installation"]["sprints"]
                        self.assertEqual((sprints["items"], sprints["error"]), ([], None))
                        self.assertIn("skipped", sprints)
                    else:
                        self.assertIn(f"board schema: not inspected: {reason}", output)

    def test_online_json_doctor_still_reads_sprints_and_reports_the_owed_schema(self) -> None:
        instance = self.instance(self.database(STALE))

        payload = json.loads(self.doctor(instance, "--json")[1])

        sprints = payload["status"]["installation"]["sprints"]
        self.assertNotIn("skipped", sprints)
        self.assertIn("schema_owed", str(sprints["error"]))
        self.assertIn("schema_owed", {finding["code"] for finding in payload["findings"]})

if __name__ == "__main__":
    unittest.main()
