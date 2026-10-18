"""Recovery admission protects public PO mutations and durable queued request ownership."""

import copy
import json
import multiprocessing
import os
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from tests.po_checkpoint_fixtures import po_records
from tests.po_fake_store import FakeBoard, FakePoStore, FakeSprints
from ummanu._fsutil import directory_lock
from ummanu.board.po_restore import normalized_po_records, prepare_po_restore
from ummanu.board.sql_rows import parse_rfc3339
from ummanu.po.client import PoServiceClient, RecoveryRefused
from ummanu.po.recovery_admission import PoRecoveryAdmission, RecoveryAdmissionError
from ummanu.po.restore_context import RestoreContextError
from ummanu.po.runner import PoRunner
from ummanu.po.service import PoService, listening
from ummanu.po.store import COMPLETED, FeedEntry, PoRequest, Session, Turn
from ummanu.web import page_po
from ummanu.webproto.errors import RuntimeUnavailable
from ummanu.webproto.po_ops import PoLayer


def _hold_directory(path, entered, release):
    with directory_lock(Path(path)):
        entered.set()
        release.wait(2)


class _Process:
    pid = 999999999

    def __init__(self, release):
        self.release = release

    def wait(self, timeout=None):
        if self.release is not None and not self.release.wait(2):
            raise RuntimeError("fake process deadline")
        return 0


class RecoveryAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="pra-"))).resolve()
        self.data = self.root / "data"
        (self.data / "po").mkdir(parents=True)
        self.board = FakeBoard()
        self.store = FakePoStore(self.board)
        self.calls = []
        self.release = None
        self.output = True
        self.records = po_records(feed_count=2)
        self.records["po_sessions"][-1]["cli"] = "claude"
        self.runner = self.new_runner()
        self.owner = self.runner.admission
        self.service = PoService(self.runner, models={"claude": ("model",)},
                                 sprints=FakeSprints({"sprint:10": None}))
        self.plan = None

    def new_runner(self):
        runner = PoRunner(FakePoStore(self.board), self.data, head_specs={}, env={}, turn_launcher=self.launch)
        self.enterContext(mock.patch.object(runner, "codex_home", return_value=self.root))
        return runner

    def launch(self, session, seq, argv, files, environment, spec):
        self.calls.append((session.session_id, seq, files.prompt.read_text()))
        result = {"type": "result", "result": "answer"}
        if self.output:
            result["session_id"] = session.cli_session_id
        if session.cli == "codex":
            result = {"type": "thread.started", "thread_id": "replacement-thread"}
        files.stdout.write_text(json.dumps(result))
        files.stderr.write_text("")
        files.last_message.write_text("answer")
        return _Process(self.release)

    def begin_import(self):
        self.assertEqual(self.owner.begin(self.records), "holding")
        self.plan = prepare_po_restore(self.records, self.data, interrupted_at=datetime(2026, 10, 1, tzinfo=UTC))
        normalized = normalized_po_records(self.records, self.plan)

        def row(value, cls):
            return cls(**{key: parse_rfc3339(item) if key.endswith("_at") and item is not None else item
                          for key, item in value.items()})

        self.board.sessions = {value["session_id"]: row(value, Session) for value in normalized["po_sessions"]}
        self.board.turns = {(value["session_id"], value["seq"]): row(value, Turn)
                            for value in normalized["po_turns"]}
        self.board.feed = [row(value, FeedEntry) for value in normalized["po_feed"]]
        self.board.requests = {value["request_id"]: row(value, PoRequest) for value in normalized["po_requests"]}

    def prepare_finish(self):
        self.runner.prepare_restored_context(self.records, self.plan)
        self.owner.finish(self.records, self.plan, import_committed=True, parity_verified=True)

    def snapshot(self):
        return copy.deepcopy((self.board.sessions, self.board.turns, self.board.feed, self.board.requests))

    def test_begin_holds_direct_send_create_and_recovery_before_any_sql_claim(self):
        self.begin_import()
        before = self.snapshot()
        operations = [
            lambda: self.runner.send("open", "new input"),
            lambda: self.runner.send_request("open", "new input", "new-request"),
            lambda: self.runner.create_session("claude", "model", "high"),
            lambda: self.runner.create_session_request("claude", "model", "new-session", "high"),
            lambda: self.runner.stop("open"),
            lambda: self.runner.stop_turn("open", 4),
            lambda: self.runner.recover(rerun=True),
        ]
        with (
            mock.patch.object(self.runner.store, "claim_turn", side_effect=AssertionError("SQL turn claim")),
            mock.patch.object(self.runner.store, "claim_session", side_effect=AssertionError("SQL session claim")),
        ):
            for operation in operations:
                with self.subTest(operation=operation), self.assertRaises(RecoveryAdmissionError) as raised:
                    operation()
                self.assertEqual(raised.exception.code, "recovery_hold")
                self.assertTrue(raised.exception.retryable)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.calls, [])
        self.prepare_finish()
        turn = self.runner.send("open", "safe after preparation")
        self.assertEqual(self.runner.wait("open", turn.seq, 1).state, COMPLETED)

    def test_service_mutations_refuse_before_acceptance_and_leave_read_access(self):
        self.begin_import()
        before = self.snapshot()
        requests = [
            {"op": "submit", "session_id": "open", "text": "input", "request_id": "new-send"},
            {"op": "create_session", "cli": "claude", "model": "model", "effort": "high", "request_id": "new"},
            {"op": "sprint_session", "sprint_ref": "sprint:10", "request_id": "resolve"},
            {"op": "rename_session", "session_id": "open", "title": "changed"},
            {"op": "close_session", "session_id": "open"},
            {"op": "stop_turn", "session_id": "open", "seq": 4},
        ]
        for request in requests:
            with self.subTest(op=request["op"]):
                answer = self.service.handle(request)
                self.assertFalse(answer["ok"])
                self.assertEqual(answer["error"]["code"], "recovery_hold")
                self.assertTrue(answer["error"]["nothing_written"])
                self.assertIn("finish recovery admission", answer["error"]["message"])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.service.queue.pending(), [])
        self.assertEqual(len(self.store.feed("open")), 2)
        status = self.service.handle({"op": "status"})
        self.assertTrue(status["ok"])
        self.assertEqual(status["result"]["recovery_admission"]["reason"], "recovery_hold")

    def test_queue_retains_frozen_item_and_hands_over_once_after_finish(self):
        card = {"kind": "operation", "card_ref": "project:1"}
        metadata = {"summary": "frozen", "nested": [1, True]}
        self.service.queue.put(session_id="open", text="queued input", request_id="queued",
                               source="dispatcher", card=card, note="frozen note", metadata=metadata)
        [item] = self.service.queue.pending()
        path = self.service.queue.directory / item.name
        saved = path.read_bytes()
        self.begin_import()
        before = self.snapshot()
        for _ in range(2):
            self.service.pump()
        self.assertEqual(path.read_bytes(), saved)
        self.assertEqual(self.service.queue.find("queued"), item)
        self.assertIsNone(self.service.queue.find_refused("queued"))
        self.assertIsNone(self.store.request("queued"))
        self.assertEqual(self.snapshot(), before)
        self.prepare_finish()
        self.service.pump()
        known = self.store.request("queued")
        self.assertEqual(self.runner.wait("open", known.seq, 1).state, COMPLETED)
        self.service.pump()
        replay = self.service.submit(session_id="open", text="queued input", request_id="queued",
                                     source="dispatcher", card=card)
        self.assertTrue(replay["repeated"])
        self.assertEqual(self.service.queue.pending(), [])
        self.assertEqual(len(self.calls), 1)
        [owner] = [entry for entry in self.store.feed("open") if entry.turn_seq == known.seq and entry.role == "owner"]
        self.assertEqual(owner.text, "queued input\n\nfrozen note\n")
        self.assertEqual(owner.metadata, metadata)

    def test_finish_requires_caller_commit_parity_and_existing_full_preparation(self):
        self.begin_import()
        with self.assertRaises(RestoreContextError):
            self.owner.finish(self.records, self.plan, import_committed=True, parity_verified=True)
        self.runner.prepare_restored_context(self.records, self.plan)
        for committed, parity in ((False, True), (True, False), (1, True)):
            with self.subTest(committed=committed, parity=parity), self.assertRaises(RecoveryAdmissionError):
                self.owner.finish(self.records, self.plan, import_committed=committed, parity_verified=parity)
        path = self.data / ".po-restore" / "contexts.json"
        saved = path.read_bytes()
        state = json.loads(saved)
        del state["sessions"]["closed"]
        path.write_text(json.dumps(state))
        with self.assertRaises(RestoreContextError):
            self.owner.finish(self.records, self.plan, import_committed=True, parity_verified=True)
        path.write_bytes(saved)
        state = json.loads(saved)
        state["sessions"]["empty"].update(phase="blocked", attempt=1, reason="unknown effects")
        path.write_text(json.dumps(state))
        with self.assertRaises(RestoreContextError):
            self.owner.finish(self.records, self.plan, import_committed=True, parity_verified=True)
        path.write_bytes(saved)
        live = self.board.sessions["empty"]
        self.board.sessions["empty"] = replace(live, title="changed metadata")
        with self.assertRaises(RestoreContextError):
            self.owner.finish(self.records, self.plan, import_committed=True, parity_verified=True)
        self.board.sessions["empty"] = live
        self.assertEqual(self.owner.status()["reason"], "recovery_hold")
        self.owner.finish(self.records, self.plan, import_committed=True, parity_verified=True)
        ordinary = self.runner.create_session("claude", "model", "high")
        turn = self.runner.send(ordinary.session_id, "ordinary input")
        self.assertEqual(self.runner.wait(ordinary.session_id, turn.seq, 1).state, COMPLETED)
        self.assertIsNone(self.owner.status())

    def test_begin_retry_and_restart_preserve_frozen_source_time_and_native_ids(self):
        self.begin_import()
        plan_path = self.data / ".po-restore" / "plan.json"
        saved = plan_path.read_bytes()
        owner = PoRecoveryAdmission(self.data, self.store)
        with mock.patch("uuid.uuid4", side_effect=AssertionError("regenerated native id")):
            self.assertEqual(owner.begin(self.records), "holding")
            repeated = prepare_po_restore(self.records, self.data, interrupted_at=datetime(2026, 10, 2, tzinfo=UTC))
        self.assertEqual(repeated, self.plan)
        self.assertEqual(plan_path.read_bytes(), saved)
        changed = copy.deepcopy(self.records)
        changed["po_feed"][0]["text"] += "different"
        with self.assertRaises(RecoveryAdmissionError):
            owner.begin(changed)
        self.prepare_finish()
        self.assertEqual(owner.begin(self.records), "finished")
        owner.finish(self.records, self.plan, import_committed=True, parity_verified=True)
        self.assertEqual(plan_path.read_bytes(), saved)

    def test_interrupted_directory_and_missing_finished_context_fail_closed(self):
        directory = self.data / ".po-restore"
        directory.mkdir(mode=0o700)
        for operation in (lambda: self.owner.begin(self.records),
                          lambda: self.runner.create_session("claude", "model", "high")):
            with self.assertRaises(RecoveryAdmissionError) as raised:
                operation()
            self.assertEqual(raised.exception.code, "recovery_invalid")
        self.assertEqual(self.snapshot(), ({}, {}, [], {}))
        directory.rmdir()
        self.begin_import()
        self.prepare_finish()
        (directory / "contexts.json").unlink()
        with self.assertRaises(RecoveryAdmissionError):
            self.runner.create_session("claude", "model", "high")
        with self.assertRaises(RecoveryAdmissionError):
            self.owner.begin(self.records)

    def test_hold_precedes_plan_and_survives_interruption_before_plan_publication(self):
        self.assertEqual(self.owner.begin(self.records), "holding")
        with self.assertRaises(RecoveryAdmissionError) as held:
            self.runner.create_session("claude", "model", "high")
        self.assertEqual(held.exception.code, "recovery_hold")
        (self.data / ".po-restore").mkdir(mode=0o700)
        restarted = self.new_runner()
        self.assertEqual(restarted.admission.begin(self.records), "holding")
        with self.assertRaises(RecoveryAdmissionError):
            restarted.send("not-imported", "held before SQL")
        self.assertEqual(self.snapshot(), ({}, {}, [], {}))
        self.begin_import()
        self.prepare_finish()

    def test_finished_shared_plan_context_or_source_damage_has_a_recovery_reason(self):
        self.begin_import()
        self.prepare_finish()
        before = self.snapshot()
        for path in (self.data / ".po-restore" / "plan.json", self.data / ".po-restore" / "contexts.json"):
            saved = path.read_bytes()
            path.write_text("{")
            answer = self.service.handle({"op": "create_session", "cli": "claude", "model": "model",
                                          "effort": "high", "request_id": "held"})
            self.assertEqual(answer["error"]["code"], "recovery_invalid")
            self.assertTrue(answer["error"]["nothing_written"])
            self.assertNotIn(str(self.data), answer["error"]["message"])
            path.write_bytes(saved)
        path = self.owner.path
        state = json.loads(path.read_bytes())
        path.write_text(json.dumps({**state, "source_sha256": "f" * 64}))
        with self.assertRaises(RecoveryAdmissionError):
            self.runner.create_session("claude", "model", "high")
        self.assertEqual(self.snapshot(), before)

    def test_corrupt_foreign_and_unsafe_admission_authority_is_not_replaced(self):
        self.begin_import()
        path = self.data / ".po-recovery-admission.json"
        saved = path.read_bytes()
        state = json.loads(saved)
        bad = [b"{", b"null", b"\xff", b'{"version":1,"version":1}',
               json.dumps({**state, "version": True}).encode(),
               json.dumps({**state, "data_dir": str(self.root)}).encode(),
               json.dumps({**state, "source_sha256": "foreign"}).encode(),
               json.dumps({**state, "phase": "ready"}).encode()]
        for payload in bad:
            path.write_bytes(payload)
            with self.subTest(payload=payload), self.assertRaises(RecoveryAdmissionError):
                self.owner.begin(self.records)
            self.assertEqual(self.service.handle({"op": "rename_session", "session_id": "open", "title": "x"})
                             ["error"]["code"], "recovery_invalid")
            self.assertEqual(path.read_bytes(), payload)
        path.write_bytes(saved)
        for unsafe in ("permissions", "symlink", "hardlink"):
            retained = self.data / "retained"
            if unsafe == "permissions":
                path.chmod(0o644)
            else:
                path.rename(retained)
                if unsafe == "symlink":
                    path.symlink_to(retained)
                else:
                    os.link(retained, path)
            with self.subTest(unsafe=unsafe), self.assertRaises(RecoveryAdmissionError):
                self.owner.begin(self.records)
            if unsafe == "permissions":
                path.chmod(0o600)
            else:
                path.unlink()
                retained.rename(path)
        self.assertEqual(path.read_bytes(), saved)

    def test_begin_refuses_running_work_without_killing_or_claiming_it_recovered(self):
        session = self.runner.create_session("claude", "model", "high")
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        turn = self.runner.send(session.session_id, "already admitted")
        with self.assertRaises(RecoveryAdmissionError) as raised:
            self.owner.begin(self.records)
        self.assertEqual(raised.exception.code, "recovery_busy")
        self.assertEqual(self.store.turn(session.session_id, turn.seq).state, "running")
        self.release.set()
        self.assertEqual(self.runner.wait(session.session_id, turn.seq, 1).state, COMPLETED)
        self.assertEqual(self.owner.begin(self.records), "holding")

    def test_late_waiter_after_stop_cannot_change_session_metadata_after_begin(self):
        session = self.runner.create_session("codex", "model", "high")
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        turn = self.runner.send(session.session_id, "already admitted")
        stopped = threading.Event()
        original = self.runner.store.finish_turn

        def finish(*args, **kwargs):
            changed = original(*args, **kwargs)
            stopped.set()
            return changed

        with mock.patch.object(self.runner.store, "finish_turn", side_effect=finish):
            stop = threading.Thread(target=self.runner.stop, args=(session.session_id,))
            stop.start()
            self.assertTrue(stopped.wait(1))
            self.owner.begin(self.records)
            before = self.snapshot()
            self.release.set()
            stop.join(1)
        self.runner.wait(session.session_id, turn.seq, 1)
        self.assertFalse(stop.is_alive())
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.store.session(session.session_id).cli_session_id, "replacement-thread")

    def test_begin_waits_for_admitted_claim_and_then_refuses_running_work(self):
        session = self.runner.create_session("claude", "model", "high")
        entered, release_claim, finished = threading.Event(), threading.Event(), threading.Event()
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        results = []
        original = self.runner.store.claim_turn

        def claim(*args, **kwargs):
            entered.set()
            if not release_claim.wait(1):
                raise AssertionError("claim deadline")
            return original(*args, **kwargs)

        def begin():
            try:
                self.owner.begin(self.records)
            except RecoveryAdmissionError as exc:
                results.append(exc.code)
            finally:
                finished.set()

        with mock.patch.object(self.runner.store, "claim_turn", side_effect=claim):
            send = threading.Thread(target=self.runner.send, args=(session.session_id, "concurrent"))
            send.start()
            self.assertTrue(entered.wait(1))
            start = threading.Thread(target=begin)
            start.start()
            self.assertFalse(finished.wait(0.03))
            release_claim.set()
            send.join(1)
            start.join(1)
        self.assertEqual(results, ["recovery_busy"])
        self.release.set()
        self.runner.wait(session.session_id, 1, 1)

    def test_finish_serializes_with_session_admission_until_proof_is_published(self):
        self.begin_import()
        self.runner.prepare_restored_context(self.records, self.plan)
        entered, release, admitted = threading.Event(), threading.Event(), threading.Event()
        results = []
        original = self.owner.contexts.prepare

        def prove(*args, **kwargs):
            entered.set()
            if not release.wait(1):
                raise AssertionError("proof deadline")
            return original(*args, **kwargs)

        def create():
            results.append(self.new_runner().create_session("claude", "model", "high"))
            admitted.set()

        with mock.patch.object(self.owner.contexts, "prepare", side_effect=prove):
            finish = threading.Thread(target=self.owner.finish, args=(self.records, self.plan),
                                      kwargs={"import_committed": True, "parity_verified": True})
            finish.start()
            self.assertTrue(entered.wait(1))
            ingress = threading.Thread(target=create)
            ingress.start()
            self.assertFalse(admitted.wait(0.03))
            release.set()
            finish.join(1)
            ingress.join(1)
        self.assertTrue(admitted.is_set())
        self.assertEqual(len(results), 1)

    def test_native_target_lock_serializes_another_process(self):
        context = multiprocessing.get_context("fork")
        entered, release = context.Event(), context.Event()
        child = context.Process(target=_hold_directory, args=(str(self.data), entered, release))
        child.start()
        self.addCleanup(child.join, 2)
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(1))
        completed = threading.Event()
        thread = threading.Thread(target=lambda: (self.owner.begin(self.records), completed.set()))
        thread.start()
        self.assertFalse(completed.wait(0.03))
        release.set()
        thread.join(1)
        child.join(1)
        self.assertTrue(completed.is_set())
        self.assertEqual(child.exitcode, 0)

    def test_publication_failure_before_begin_rename_leaves_no_false_hold(self):
        with (
            mock.patch("ummanu.po.recovery_admission.write_bytes_atomic", side_effect=RuntimeError("disk")),
            self.assertRaises(RecoveryAdmissionError),
        ):
            self.owner.begin(self.records)
        self.assertIsNone(PoRecoveryAdmission(self.data, self.store).status())
        self.assertEqual(self.owner.begin(self.records), "holding")

    def test_restart_after_begin_or_finish_rename_reloads_durable_phase(self):
        with (
            mock.patch("ummanu.po.recovery_admission.fsync_directory", side_effect=OSError("sync")),
            self.assertRaises(RecoveryAdmissionError),
        ):
            self.owner.begin(self.records)
        restarted = PoRecoveryAdmission(self.data, self.store)
        self.assertEqual(restarted.status()["reason"], "recovery_hold")
        self.begin_import()
        self.runner.prepare_restored_context(self.records, self.plan)
        with (
            mock.patch("ummanu.po.recovery_admission.fsync_directory", side_effect=OSError("sync")),
            self.assertRaises(RecoveryAdmissionError),
        ):
            restarted.finish(self.records, self.plan, import_committed=True, parity_verified=True)
        restarted = PoRecoveryAdmission(self.data, self.store)
        restarted.finish(self.records, self.plan, import_committed=True, parity_verified=True)
        self.assertIsNone(restarted.status())

    def test_failure_before_finish_rename_keeps_hold_and_prepared_cohort(self):
        self.begin_import()
        self.runner.prepare_restored_context(self.records, self.plan)
        with (
            mock.patch("ummanu.po.recovery_admission.write_bytes_atomic", side_effect=RuntimeError("disk")),
            self.assertRaises(RecoveryAdmissionError),
        ):
            self.owner.finish(self.records, self.plan, import_committed=True, parity_verified=True)
        self.assertEqual(self.new_runner().admission.status()["reason"], "recovery_hold")
        self.owner.finish(self.records, self.plan, import_committed=True, parity_verified=True)
        self.assertIsNone(self.owner.status())

    def test_known_session_uncertainty_does_not_hold_unrelated_sessions_or_allow_reseed(self):
        self.begin_import()
        self.prepare_finish()
        self.output = False
        turn = self.runner.send("open", "uncertain native establishment")
        self.assertEqual(self.runner.wait("open", turn.seq, 1).state, "interrupted")
        before = self.snapshot()
        with self.assertRaises(RestoreContextError):
            self.runner.send("open", "unsafe repeat")
        self.assertEqual(self.snapshot(), before)
        replay, created = self.runner.send_request("open", self.records["po_feed"][0]["text"], "send")
        self.assertFalse(created)
        self.assertEqual(replay.seq, 1)
        self.assertIsNone(self.owner.status())
        ordinary = self.new_runner().create_session("claude", "model", "high")
        first = self.runner.send(ordinary.session_id, "unrelated")
        self.assertEqual(self.runner.wait(ordinary.session_id, first.seq, 1).state, COMPLETED)
        self.owner.finish(self.records, self.plan, import_committed=True, parity_verified=True)

    def test_client_preserves_retryable_recovery_refusal_and_definite_no_write(self):
        self.begin_import()
        with listening(self.service):
            client = PoServiceClient(self.data)
            with self.assertRaises(RecoveryRefused) as held:
                client.create_session(cli="claude", model="model", effort="high", request_id="socket")
            self.assertTrue(held.exception.retryable)
            self.assertTrue(held.exception.nothing_written)
            self.assertEqual(held.exception.code, "recovery_hold")
            with self.assertRaises(RecoveryRefused):
                client.submit(session_id="open", text="input", request_id="socket-input")
            layer = PoLayer(self.root, data_dir=self.data, store=self.store, client=client,
                            models={"claude": ("model",)})
            with self.assertRaises(RuntimeUnavailable) as refused:
                layer.po_create_session(cli="claude", model="model", effort="high", request_id="web-input")
            self.assertEqual(refused.exception.data["recovery"], "recovery_hold")
            self.assertTrue(refused.exception.data["retryable"])
            self.owner.path.write_text("{")
            with self.assertRaises(RecoveryRefused) as invalid:
                client.rename_session(session_id="open", title="no mutation")
            self.assertFalse(invalid.exception.retryable)
            self.assertEqual(invalid.exception.code, "recovery_invalid")

    def test_dialogue_page_explains_pending_recovery_and_keeps_history_readable(self):
        self.begin_import()
        self.service.queue.put(session_id="open", text="pending", request_id="pending", source="web")
        layer = PoLayer(self.root, data_dir=self.data, store=self.store, models={"claude": ("model",)})
        for invalid in (False, True):
            if invalid:
                self.owner.path.write_text("{")
            document = layer.po_session("open")
            self.assertEqual(len(document["feed"]), 2)
            self.assertEqual(document["queued"][0]["request_id"], "pending")
            page = page_po.po_session(document, request_id="new-form")
            self.assertIn("held: PO recovery", page)
            self.assertIn("repair recovery authority" if invalid else "finish recovery admission", page)
