"""App-role PO import, rollback, readback, recovery wiring and subsequent writes on real PostgreSQL."""

import contextlib
import copy
import getpass
import io
import json
import shutil
import stat
import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.po_checkpoint_fixtures import po_records, seed_po
from tests.restore_fixtures import _write_instance_to
from tests.sql_backend_fixtures import PostgresBoard
from ummanu.board import store
from ummanu.board.po_checkpoint import (
    PO_COLUMNS,
    RESTORED_PO_COVERAGE,
    PoCheckpointError,
    export_po_records,
    read_checkpoint_po,
)
from ummanu.board.po_notes_checkpoint import RESTORED_NOTES_COVERAGE
from ummanu.board.po_queue_checkpoint import read_checkpoint_queue
from ummanu.board.po_restore import (
    PoRestoreStore,
    normalized_po_records,
    prepare_po_restore,
    require_po_parity,
)
from ummanu.board.sql_cards import SqlCardClient
from ummanu.board.sql_rows import rfc3339
from ummanu.cli import main
from ummanu.data import export_board, init_layout
from ummanu.dispatch.po_delivery import deliver
from ummanu.po.attachments import AttachmentPublicationError, attachment_path, store_image
from ummanu.po.client import PoServiceClient
from ummanu.po.queue import PoQueue, queue_dir
from ummanu.po.recovery_admission import PoRecoveryAdmission, RecoveryAdmissionError
from ummanu.po.restore_context import NOTICE, RestoreContextError, RestoredContexts
from ummanu.po.runner import PoRunner
from ummanu.po.service import PoService, listening
from ummanu.po.store import COMPLETED, PoStore, RequestConflict, SessionClosed
from ummanu.po.workspace import NOTES_FILE, workspace_dir
from ummanu.restore import RestoreError, restore_findings, restore_po, restore_state

NOTES = b"# PO notes\r\nThe owner chose option B; the SQL feed has no record of it.\n"


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
        self.data = self.root / "data"
        self.data.mkdir()
        self.plan = prepare_po_restore(self.records, self.data, interrupted_at=datetime(2026, 10, 1, tzinfo=UTC))
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
        with mock.patch("uuid.uuid4", side_effect=AssertionError("retry generated an id")):
            self.plan = prepare_po_restore(self.records, self.data,
                                           interrupted_at=datetime(2026, 11, 1, tzinfo=UTC))
        self.assertEqual(normalized_po_records(self.records, self.plan), self.expected)
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

    def test_fresh_sequence_advances_before_normal_claim_and_complete_with_request_replays(self):
        with self.client.transaction():
            initial_sequence = self.client._query("SELECT last_value, is_called FROM po_feed_entry_id_seq")[0]
            self.assertEqual(self.client._query("SELECT current_user"), [("ummanu_app",)])
        imported_max = max(row["entry_id"] for row in self.records["po_feed"])
        self.assertEqual(initial_sequence, (1, False))
        self.assertGreater(imported_max, initial_sequence[0])

        self.import_records()
        require_po_parity(self.expected, self.readback())
        with self.client.transaction():
            restored_sequence = self.client._query("SELECT last_value, is_called FROM po_feed_entry_id_seq")[0]
        self.assertGreaterEqual(restored_sequence[0], imported_max)
        self.assertTrue(restored_sequence[1])

        turn, created = self.po.claim_turn("open", "fresh input", lambda seq: self.root / str(seq),
                                           request_id="fresh-send", metadata={"source": "owner"})
        self.assertTrue(created)
        self.assertEqual(turn.seq, 5)
        self.assertTrue(self.po.complete_turn("open", turn.seq, "fresh answer"))
        owner, agent = self.po.feed("open")[-2:]
        self.assertGreater(owner.entry_id, imported_max)
        self.assertGreater(agent.entry_id, imported_max)
        self.assertGreater(agent.entry_id, owner.entry_id)
        self.assertEqual((owner.turn_seq, owner.role, owner.text, owner.metadata),
                         (turn.seq, "owner", "fresh input", {"source": "owner"}))
        self.assertEqual((agent.turn_seq, agent.role, agent.text), (turn.seq, "agent", "fresh answer"))

        preserved = self.readback()
        preserved["po_turns"] = [row for row in preserved["po_turns"]
                                 if (row["session_id"], row["seq"]) != ("open", turn.seq)]
        preserved["po_feed"] = [row for row in preserved["po_feed"]
                                if row["entry_id"] not in (owner.entry_id, agent.entry_id)]
        preserved["po_requests"] = [row for row in preserved["po_requests"] if row["request_id"] != "fresh-send"]
        require_po_parity(self.expected, preserved)

        before = self.readback()
        replayed, created = self.po.claim_turn("open", self.records["po_feed"][0]["text"],
                                               lambda seq: self.root / str(seq), request_id="send")
        self.assertFalse(created)
        self.assertEqual(replayed, self.po.turn("open", 1))
        replayed, created = self.po.claim_turn("open", "fresh input", lambda seq: self.root / str(seq),
                                               request_id="fresh-send", metadata={"source": "owner"})
        self.assertFalse(created)
        self.assertEqual(replayed, self.po.turn("open", turn.seq))
        with self.assertRaises(RequestConflict):
            self.po.claim_turn("open", "different input", lambda seq: self.root / str(seq), request_id="send")
        self.assertEqual(before, self.readback())

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


class _FinishedProcess:
    pid = 999999999

    def wait(self, timeout=None):
        return 0


class PoRecoveryWiringPostgresTests(unittest.TestCase):
    """A deployed capture through the shared PO phase of restore-board into ordinary PO use."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.board = PostgresBoard.shared()
        self.calls = []

    def installation(self, name):
        config = self.board.fresh_database()
        self.addCleanup(self.board.release_database, config.dbname)
        data = self.root / f"{name}-data"
        instance = _write_instance_to(self.root / name, name, data)
        (instance / "projects").mkdir()
        path = store.store_path(instance)
        path.write_text("".join(f"{key}={value}\n" for key, value in config.as_environ().items()),
                        encoding="utf-8")
        path.chmod(0o600)
        init_layout(data)
        client = SqlCardClient(config.for_role("app"), instance)
        self.addCleanup(client.close)
        po = PoStore(config.for_role("app"))
        self.addCleanup(po.close)
        return instance, data, client, po

    def capture(self, records, name, notes=None):
        """The board export a source installation whose PO tables hold `records` and whose queue holds
        one pending and one refused input publishes."""
        instance, data, client, _ = self.installation(name)
        seed_po(client, records)
        queue = PoQueue(data)
        queue.put(session_id="open", text="queued at the cut", request_id="queued-input", source="web")
        queue.refuse(queue.put(session_id="closed", text="gone", request_id="refused-input", source="web"), "closed")
        if notes is not None:
            workspace_dir(data).mkdir()
            (workspace_dir(data) / NOTES_FILE).write_bytes(notes)
        export_board(data, instance_dir=instance)
        return data / "board"

    def target(self, board, name):
        instance, data, client, po = self.installation(name)
        shutil.copytree(board, data / "board", dirs_exist_ok=True)
        (data / "po").mkdir()
        return instance, data, client, po

    @staticmethod
    def readback(client):
        with client.transaction():
            return export_po_records(client)

    @staticmethod
    def restore_board(instance):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["restore-board", "--instance", str(instance)])
        return code, output.getvalue()

    def launch(self, session, seq, argv, files, environment, spec):
        notes = Path(session.cwd) / NOTES_FILE
        self.calls.append({"argv": argv, "prompt": files.prompt.read_text(),
                           "notes": notes.read_bytes() if notes.exists() else None})
        output = ({"type": "result", "result": "fresh answer", "session_id": session.cli_session_id}
                  if session.cli == "claude" else {"type": "thread.started", "thread_id": "new-thread"})
        files.stdout.write_text(json.dumps(output) + "\n")
        files.stderr.write_text("")
        files.last_message.write_text("fresh answer")
        return _FinishedProcess()

    def runner(self, po, data):
        runner = PoRunner(po, data, head_specs={}, env={}, turn_launcher=self.launch)
        self.enterContext(mock.patch.object(runner, "codex_home", return_value=self.root))
        return runner

    def test_restore_board_restores_dialogue_both_providers_resume_and_a_new_export_carries_it(self):
        for cli in ("claude", "codex"):
            with self.subTest(cli=cli):
                self.calls = []
                records = po_records(feed_count=12)
                records["po_sessions"][-1]["cli"] = cli
                board = self.capture(records, f"{cli}-source", notes=NOTES)
                self.assertEqual(read_checkpoint_po(board), records)
                instance, data, client, po = self.target(board, f"{cli}-target")
                code, output = self.restore_board(instance)
                self.assertEqual(code, 0, output)
                self.assertNotIn("repeated text", output)
                self.assertNotIn("password", output.lower())
                self.assertNotIn("option B", output)
                result = json.loads(output)
                self.assertEqual(
                    (result["po_sql_coverage"], result["po_sql_restore"], result["po_sql_diagnostic"]),
                    ("captured", "finished", RESTORED_PO_COVERAGE),
                )
                self.assertEqual(
                    (result["po_notes_coverage"], result["po_notes_parity"], result["po_notes_diagnostic"]),
                    ("captured", "complete", RESTORED_NOTES_COVERAGE),
                )
                self.assertEqual((workspace_dir(data) / NOTES_FILE).read_bytes(), NOTES)
                self.assertEqual(
                    (result["po_queue_coverage"], result["po_queue_pending"], result["po_queue_refused"],
                     result["po_queue_diagnostic"]),
                    ("captured", 1, 1, read_checkpoint_queue(data / "board").describe()),
                )
                self.assertFalse(queue_dir(data).exists())
                plan = prepare_po_restore(records, data, interrupted_at=datetime.now(UTC))
                expected = normalized_po_records(records, plan)
                require_po_parity(expected, self.readback(client))
                self.assertIsNone(PoRecoveryAdmission(data, po).status())

                runner = self.runner(po, data)
                self.assertEqual(runner.recover(rerun=True), [])
                replay, created = runner.send_request("open", records["po_feed"][0]["text"], "send")
                self.assertEqual((replay.seq, created), (1, False))
                first, created = runner.send_request("open", "current-owner-input", "new-request")
                self.assertTrue(created)
                first = runner.wait("open", first.seq, 2)
                self.assertEqual(first.state, COMPLETED, first.reason)
                [call] = self.calls
                self.assertEqual(call["notes"], NOTES)
                self.assertIn(NOTICE, call["prompt"])
                self.assertEqual(call["prompt"].count("current-owner-input"), 1)
                if cli == "claude":
                    self.assertEqual(call["argv"][-2:], ["--session-id", dict(plan.native_bindings)["open"]])
                else:
                    self.assertEqual(call["argv"][1], "exec")
                    self.assertEqual(po.session("open").cli_session_id, "new-thread")
                second, _ = runner.send_request("open", "follow-up", "second-request")
                self.assertEqual(runner.wait("open", second.seq, 2).state, COMPLETED)
                self.assertEqual(self.calls[-1]["prompt"], "follow-up")
                if cli == "claude":
                    self.assertEqual(self.calls[-1]["argv"][-2:], ["--resume", dict(plan.native_bindings)["open"]])
                else:
                    self.assertEqual(self.calls[-1]["argv"][1:3], ["exec", "resume"])

                live = self.readback(client)
                code, output = self.restore_board(instance)
                self.assertEqual(code, 0, output)
                self.assertEqual(json.loads(output)["po_sql_restore"], "finished")
                self.assertEqual(json.loads(output)["po_notes_parity"], "complete")
                self.assertEqual((workspace_dir(data) / NOTES_FILE).read_bytes(), NOTES)
                self.assertEqual(self.readback(client), live)
                self.assertEqual(len(self.calls), 2)
                again, created = runner.send_request("open", "current-owner-input", "new-request")
                self.assertEqual((again.seq, created), (first.seq, False))
                self.assertEqual(len(self.calls), 2)

                export = self.root / f"{cli}-export"
                export_board(export, instance_dir=instance)
                exported = read_checkpoint_po(export / "board")
                self.assertEqual(exported, live)
                self.assertEqual(len(exported["po_turns"]), len(records["po_turns"]) + 2)
                self.assertEqual(exported["po_feed"][:len(expected["po_feed"])], expected["po_feed"])

    def test_each_interrupted_phase_holds_po_input_and_the_retry_resumes_the_same_recovery(self):
        records = po_records()
        board = self.capture(records, "fault-source")
        restore = PoRestoreStore.restore

        def after_import(owner, *args):
            restore(owner, *args)
            raise PoCheckpointError("injected import failure")

        faults = (
            ("import", PoRestoreStore, "restore", after_import, "holding"),
            ("contexts", RestoredContexts, "prepare", RestoreContextError("injected contexts failure"),
             "imported"),
            ("finish", PoRecoveryAdmission, "finish", RecoveryAdmissionError("injected finish failure"),
             "contexts_prepared"),
        )
        for step, owner, name, fault, phase in faults:
            with self.subTest(step=step):
                instance, data, client, po = self.target(board, f"fault-{step}")
                with (
                    mock.patch.object(owner, name, autospec=True, side_effect=fault),
                    self.assertRaisesRegex(RestoreError, f"PO SQL restore failed at {step}: injected"),
                ):
                    restore_po(data, source=data / "board", instance=instance)
                self.assertEqual(restore_state(data)["po_sql_restore"], phase)
                self.assertIn(f"PO SQL restore is incomplete at {phase}", restore_findings(data))
                self.assertEqual(PoRecoveryAdmission(data, po).status()["reason"], "recovery_hold")
                self.assertEqual((data / ".po-restore" / "contexts.json").exists(), step == "finish")
                committed = self.readback(client)
                if step == "import":
                    self.assertEqual(committed, {table: [] for table in PO_COLUMNS})
                with self.assertRaises(RecoveryAdmissionError):
                    self.runner(po, data).send_request("open", "held input", "held-request")
                self.assertEqual(self.readback(client), committed)
                self.assertEqual(self.calls, [])
                plan = (data / ".po-restore" / "plan.json").read_bytes()

                self.assertTrue(restore_po(data, source=data / "board", instance=instance))
                self.assertEqual((data / ".po-restore" / "plan.json").read_bytes(), plan)
                self.assertEqual(restore_state(data)["po_sql_restore"], "finished")
                self.assertIsNone(PoRecoveryAdmission(data, po).status())
                frozen = prepare_po_restore(records, data, interrupted_at=datetime.now(UTC))
                require_po_parity(normalized_po_records(records, frozen), self.readback(client))
                self.assertFalse(restore_po(data, source=data / "board", instance=instance))

    def test_board_commit_before_the_po_import_holds_service_and_dispatcher_and_the_retry_finishes(self):
        board = self.capture(po_records(), "seam-source")
        instance, data, client, po = self.target(board, "seam-target")
        runner = self.runner(po, data)
        service = PoService(runner, models={"codex": ("chosen-model",)})
        service.queue.put(session_id="open", text="queued before recovery", request_id="queued-early", source="web")
        successors = []

        def successor(closed):
            successors.append(closed)
            return "", "no successor here"

        with listening(service):
            runtime = SimpleNamespace(po=PoServiceClient(data))
            with mock.patch("ummanu.board.po_restore.prepare_po_restore",
                            side_effect=PoCheckpointError("injected seam failure")):
                code, output = self.restore_board(instance)
            self.assertEqual(code, 2, output)
            self.assertIn("PO SQL restore failed at plan: injected seam failure", output)
            state = restore_state(data)
            self.assertEqual((state["board"], state["po_sql_restore"]), ("complete", "holding"))
            # The restored board is published; every PO consumer still waits on the admission hold.
            status, detail, received = deliver(runtime, session_id="open", text="card result",
                                               request_id="delivery-1", card={"ref": "ummanu-1"},
                                               successor=successor)
            self.assertEqual((status, received), (None, ""))
            self.assertIn("recovery_hold", detail)
            service.pump()
            self.assertEqual([item.request_id for item in service.queue.pending()], ["queued-early"])
            self.assertIsNone(service.queue.find_refused("queued-early"))
            self.assertEqual((successors, self.calls), ([], []))
            self.assertEqual(self.readback(client), {table: [] for table in PO_COLUMNS})
            self.assertIn("PO SQL restore is incomplete at holding", restore_findings(data))

            code, output = self.restore_board(instance)
            self.assertEqual(code, 0, output)
            self.assertEqual(json.loads(output)["po_sql_restore"], "finished")
            service.pump()
            queued = po.request("queued-early")
            self.assertIsNotNone(queued)
            self.assertEqual(runner.wait("open", queued.seq, 2).state, COMPLETED)
            self.assertEqual(service.queue.pending(), [])
            self.assertEqual(len(self.calls), 1)
            self.assertEqual(successors, [])

    def test_image_failure_after_import_retry_preserves_plan_and_sql_until_native_parity(self):
        from ummanu.po import attachments

        board = self.capture(po_records(), "images-source", notes=NOTES)
        source_data = board.parent
        images = [(session, store_image(source_data, session, content).name, content)
                  for session, content in (("open", b"\x89PNG\r\n\x1a\nimage"),
                                           ("closed", b"\xff\xd8\xffimage"),
                                           ("empty", b"RIFF\x00\x00\x00\x00WEBPimage"))]
        export_board(source_data, instance_dir=self.root / "images-source")
        instance, data, client, po = self.target(board, "images-target")
        (data / "po").chmod(0o700)
        with mock.patch.object(RestoredContexts, "prepare", side_effect=RestoreContextError("context failure")):
            code, output = self.restore_board(instance)
        self.assertEqual(code, 2, output)
        self.assertIn("context failure", output)
        plan_bytes = (data / ".po-restore" / "plan.json").read_bytes()
        imported = self.readback(client)
        session, name, content = images[0]
        attachment_path(data, session, name).unlink()
        with mock.patch.object(attachments, "publish_image", side_effect=AttachmentPublicationError("injected image failure")):
            code, output = self.restore_board(instance)
        self.assertEqual(code, 2, output)
        self.assertIn("injected image failure", output)
        self.assertEqual(restore_state(data)["po_attachments_parity"], "failed")
        self.assertEqual(self.readback(client), imported)
        self.assertEqual(PoRecoveryAdmission(data, po).status()["reason"], "recovery_hold")
        self.assertEqual((data / ".po-restore" / "plan.json").read_bytes(), plan_bytes)
        later = store_image(data, session, content + b"later")
        for _ in range(2):
            code, output = self.restore_board(instance)
            self.assertEqual(code, 0, output)
            self.assertEqual(json.loads(output)["po_attachments_parity"], "complete")
            self.assertEqual(self.readback(client), imported)
            self.assertEqual((data / ".po-restore" / "plan.json").read_bytes(), plan_bytes)
            for session, name, content in images:
                self.assertEqual(attachment_path(data, session, name).read_bytes(), content)
        self.assertEqual(later.read_bytes(), images[0][2] + b"later")
        self.assertIsNone(PoRecoveryAdmission(data, po).status())
        self.assertEqual(self.calls, [])
        self.assertFalse(queue_dir(data).exists())

    def test_unfinished_plan_with_another_source_digest_is_refused_by_field_through_restore_board(self):
        board = self.capture(po_records(), "plan-source")
        instance, data, client, po = self.target(board, "plan-target")
        with mock.patch.object(PoRestoreStore, "restore", autospec=True,
                               side_effect=PoCheckpointError("injected import failure")):
            self.assertEqual(self.restore_board(instance)[0], 2)
        path = data / ".po-restore" / "plan.json"
        saved = json.loads(path.read_bytes())
        other = "f" * 64 if saved["source_sha256"] != "f" * 64 else "e" * 64
        payload = json.dumps({**saved, "source_sha256": other}).encode()
        path.write_bytes(payload)
        code, output = self.restore_board(instance)
        self.assertEqual(code, 2, output)
        self.assertIn("PO SQL restore failed at plan: PO restore plan field source_sha256 differs", output)
        for value in (other, saved["source_sha256"]):
            self.assertNotIn(value, output)
        self.assertEqual(path.read_bytes(), payload)
        self.assertEqual(PoRecoveryAdmission(data, po).status()["reason"], "recovery_hold")
        self.assertEqual(restore_state(data)["po_sql_restore"], "holding")
        self.assertEqual(self.readback(client), {table: [] for table in PO_COLUMNS})

    def test_busy_target_refuses_before_any_recovery_write_and_keeps_its_running_turn(self):
        board = self.capture(po_records(), "busy-source")
        instance, data, client, _ = self.target(board, "busy-target")
        live = po_records()
        live["po_sessions"] = [live["po_sessions"][-1]]
        live["po_turns"] = [live["po_turns"][-1]]
        live["po_feed"] = live["po_requests"] = []
        seed_po(client, live)
        with self.assertRaisesRegex(RestoreError, "failed at admission: .*turns are running"):
            restore_po(data, source=data / "board", instance=instance)
        self.assertEqual(restore_state(data)["po_sql_restore"], "captured")
        self.assertEqual(self.readback(client), live)
        self.assertEqual(sorted(path.name for path in data.iterdir() if path.name.startswith(".po-")), [])

    def test_root_recovery_runs_the_po_phase_in_the_runtime_user_child(self):
        board = self.capture(po_records(), "root-source")
        instance, data, client, po = self.target(board, "root-target")
        root = mock.patch("os.geteuid", return_value=0)
        root.start()
        self.addCleanup(root.stop)
        # The child stops pretending to be root once it has dropped to the account.
        with (
            mock.patch("os.initgroups"),
            mock.patch("os.setgid"),
            mock.patch("os.setuid", side_effect=lambda _uid: root.stop()),
        ):
            for _ in range(2):
                restore_po(data, source=data / "board", instance=instance, runtime_user=getpass.getuser())
        root.stop()
        self.assertEqual(restore_state(data)["po_sql_restore"], "finished")
        self.assertIsNone(PoRecoveryAdmission(data, po).status())
        for path in (data / ".po-recovery-admission.json", data / ".po-restore" / "plan.json",
                     data / ".po-restore" / "contexts.json"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((data / ".po-restore").stat().st_mode), 0o700)
        self.assertEqual(len(self.readback(client)["po_sessions"]), 3)
