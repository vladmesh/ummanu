"""App-role PO import, rollback, readback and subsequent writes on real PostgreSQL."""

import copy
import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from tests.po_checkpoint_fixtures import po_records, seed_po
from tests.sql_backend_fixtures import PostgresBoard
from ummanu.board.po_checkpoint import PO_COLUMNS, PoCheckpointError, export_po_records
from ummanu.board.po_restore import PoRestoreStore, normalized_po_records, plan_po_restore, require_po_parity
from ummanu.board.sql_cards import SqlCardClient
from ummanu.board.sql_rows import rfc3339
from ummanu.po.store import PoStore, RequestConflict, SessionClosed


class PoRestorePostgresTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        board = PostgresBoard.shared()
        self.config = board.fresh_database()
        self.addCleanup(board.release_database, self.config.dbname)
        self.client = SqlCardClient(self.config.for_role("app"), self.root)
        self.addCleanup(self.client.close)
        self.restore = PoRestoreStore(self.client)
        self.po = PoStore(self.config.for_role("app"))
        self.addCleanup(self.po.close)
        records = po_records()
        records["po_sessions"][1]["cli"] = "claude"
        source_config = board.fresh_database()
        self.addCleanup(board.release_database, source_config.dbname)
        source = SqlCardClient(source_config.for_role("app"), self.root)
        self.addCleanup(source.close)
        seed_po(source, records)
        with source.transaction():
            self.records = export_po_records(source)
        self.assertEqual(self.records, records)
        self.plan = plan_po_restore(self.records, self.root / "data", interrupted_at=datetime(2026, 10, 1, tzinfo=UTC))
        self.expected = normalized_po_records(self.records, self.plan)

    def readback(self):
        with self.client.transaction():
            return export_po_records(self.client)

    def import_records(self):
        with self.client.transaction():
            self.restore.restore(self.records, self.plan)

    def test_import_twice_has_complete_parity_through_fresh_sql_and_po_readers_without_effects(self):
        with ExitStack() as stack:
            for target in ("subprocess.Popen", "subprocess.run", "os.kill", "os.killpg",
                           "ummanu.po.runner.PoRunner.recover", "ummanu.po.runner.PoRunner.send_request",
                           "ummanu.po.service.PoService.submit", "ummanu.board.owner_events.OwnerEventStore.insert",
                           "pathlib.Path.read_bytes",
                           "pathlib.Path.read_text"):
                stack.enter_context(mock.patch(target, side_effect=AssertionError(target)))
            self.import_records()
            self.import_records()
        require_po_parity(self.expected, self.readback())
        self.assertEqual(self.po.running_turns(), [])
        for table in PO_COLUMNS:
            for expected in self.expected[table]:
                if table == "po_sessions":
                    row = self.po.session(expected["session_id"])
                elif table == "po_turns":
                    row = self.po.turn(expected["session_id"], expected["seq"])
                elif table == "po_feed":
                    row = next(row for row in self.po.feed(expected["session_id"])
                               if row.entry_id == expected["entry_id"])
                else:
                    row = self.po.request(expected["request_id"])
                values = asdict(row)
                actual = {field: rfc3339(values[field], microseconds=True)
                          if isinstance(values[field], datetime) else values[field] for field in expected}
                self.assertEqual(expected, actual)
        self.assertEqual(self.po.turns("empty"), [])
        self.assertEqual(self.po.session("closed").state, "closed")

    def test_import_does_not_commit_its_callers_work(self):
        with self.assertRaisesRegex(RuntimeError, "owner rollback"), self.client.transaction():
            self.restore.restore(self.records, self.plan)
            raise RuntimeError("owner rollback")
        self.assertEqual(self.readback(), {table: [] for table in PO_COLUMNS})
        self.import_records()
        require_po_parity(self.expected, self.readback())

    def test_interruption_at_each_fk_phase_rolls_back_prefix_and_same_plan_retries(self):
        execute = self.client._execute
        for stop in (1, 4, 9, 20):
            count = 0

            def interrupt(sql, params=(), *, stop=stop, **kwargs):
                nonlocal count
                result = execute(sql, params, **kwargs)
                if sql.startswith("INSERT INTO po_"):
                    count += 1
                    if count == stop:
                        raise RuntimeError("interrupted")
                return result

            with (
                self.subTest(stop=stop),
                mock.patch.object(self.client, "_execute", side_effect=interrupt),
                self.assertRaisesRegex(RuntimeError, "interrupted"),
            ):
                self.import_records()
            self.assertEqual(self.readback(), {table: [] for table in PO_COLUMNS})
        self.import_records()
        self.import_records()
        require_po_parity(self.expected, self.readback())

    def test_conflict_after_missing_parents_were_inserted_rolls_back_every_new_row(self):
        partial = {table: [] for table in PO_COLUMNS}
        partial["po_sessions"] = [copy.deepcopy(self.expected["po_sessions"][-1])]
        partial["po_turns"] = [copy.deepcopy(self.expected["po_turns"][1])]
        partial["po_turns"][0]["reason"] = "private target history"
        seed_po(self.client, partial)
        before = self.readback()
        with self.assertRaises(PoCheckpointError) as failed:
            self.import_records()
        self.assertIn("po_turns", str(failed.exception))
        self.assertNotIn("private target history", str(failed.exception))
        self.assertEqual(before, self.readback())

    def test_confirmed_target_completion_refuses_pending_source_without_reverting_it(self):
        target = copy.deepcopy(self.expected)
        target["po_turns"][-1].update(state="completed", reason="confirmed", resolved_model="confirmed-model")
        seed_po(self.client, target)
        before = self.readback()
        with self.assertRaises(PoCheckpointError):
            self.import_records()
        self.assertEqual(before, self.readback())

    def test_extra_history_and_foreign_request_binding_are_refused_without_overwrite(self):
        seed_po(self.client, self.expected)
        with self.client.transaction():
            self.client._execute("UPDATE po_requests SET seq = 2 WHERE request_id = 'send'")
        before = self.readback()
        with self.assertRaises(PoCheckpointError) as failed:
            self.import_records()
        self.assertIn("po_requests", str(failed.exception))
        self.assertEqual(before, self.readback())
        with self.client.transaction():
            self.client._execute("UPDATE po_requests SET seq = 1 WHERE request_id = 'send'")
            self.client._execute("INSERT INTO po_feed (entry_id, session_id, turn_seq, role, text, created_at) "
                                 "OVERRIDING SYSTEM VALUE VALUES (99, 'open', 1, 'agent', 'confirmed', now())")
        before = self.readback()
        with self.assertRaises(PoCheckpointError):
            self.import_records()
        self.assertEqual(before, self.readback())

    def test_readback_mismatch_on_each_table_rolls_back_and_redacts_values(self):
        execute = self.client._execute
        for table, column, value in (("po_sessions", "title", "private-title"),
                                     ("po_turns", "reason", "private-reason"),
                                     ("po_feed", "text", "private-message"),
                                     ("po_requests", "fingerprint", "f" * 64)):
            def corrupt(sql, params=(), *, table=table, column=column, value=value, **kwargs):
                result = execute(sql, params, **kwargs)
                if sql.startswith(f"INSERT INTO {table} "):
                    execute(f"UPDATE {table} SET {column} = %s", (value,))
                return result

            with (
                self.subTest(table=table),
                mock.patch.object(self.client, "_execute", side_effect=corrupt),
                self.assertRaises(PoCheckpointError) as failed,
            ):
                self.import_records()
            self.assertIn(f"field {column}:", str(failed.exception))
            self.assertNotIn(value, str(failed.exception))
            self.assertEqual(self.readback(), {name: [] for name in PO_COLUMNS})

    def test_app_sequence_privileges_and_normal_claim_append_and_request_replays(self):
        with self.client.transaction():
            self.client._query("SELECT setval('po_feed_entry_id_seq', 1000, true)")
        self.import_records()
        with self.client.transaction():
            privileges = self.client._query(
                "SELECT has_sequence_privilege(current_user, 'po_feed_entry_id_seq', 'SELECT'), "
                "has_sequence_privilege(current_user, 'po_feed_entry_id_seq', 'UPDATE')"
            )
        self.assertEqual(privileges, [(True, True)])
        session, created = self.po.claim_session(session_id="unused", cli="codex", model="chosen-model",
                                                cwd="unused", cli_session_id=None, effort="high", request_id="create")
        self.assertFalse(created)
        self.assertEqual(session.session_id, "open")
        self.po.bind_sprint_session_request("sprint", "sprint:10", "open")
        turn, created = self.po.claim_turn("open", self.records["po_feed"][0]["text"],
                                           lambda seq: self.root / str(seq), request_id="send")
        self.assertFalse(created)
        self.assertEqual(turn.seq, 1)
        with self.assertRaises(RequestConflict):
            self.po.claim_turn("open", "different input", lambda seq: self.root / str(seq), request_id="send")
        with self.assertRaises(SessionClosed):
            self.po.claim_turn("closed", "new input", lambda seq: self.root / str(seq))
        turn, created = self.po.claim_turn("open", "new input", lambda seq: self.root / str(seq),
                                           request_id="new-send", metadata={"source": "owner"})
        self.assertTrue(created)
        self.assertEqual(turn.seq, 5)
        self.assertTrue(self.po.complete_turn("open", turn.seq, "new answer"))
        feed = self.po.feed("open")
        self.assertGreater(feed[-2].entry_id, 1000)
        self.assertGreater(feed[-1].entry_id, feed[-2].entry_id)
        self.assertEqual(feed[-2].metadata, {"source": "owner"})
        before = self.readback()
        with self.assertRaises(PoCheckpointError):
            self.import_records()
        self.assertEqual(before, self.readback())
