"""Real flocks and TaskWriter/SqlBoardHost, with hermetic board transport and hosts."""

from __future__ import annotations

import ast
import contextlib
import copy
import fcntl
import inspect
import textwrap
import threading
import time
import unittest
from types import MethodType, SimpleNamespace
from unittest import mock

from tests import test_owned_cleanup as owned_fixtures, test_tick_board_snapshot as tick_fixtures
from tests.fakes.dispatcher import FakeCatalog
from ummanu import restore
from ummanu.board.fake import MemoryAudit
from ummanu.board.host import TransitionRequest
from ummanu.board.models import Actor, CardState, EntityKind
from ummanu.board.tick_snapshot import tick_snapshot
from ummanu.dispatch import assessment_decision, cleanup, production, release_lifecycle
from ummanu.dispatch.host import CommandHostRuntime
from ummanu.dispatch.runtime import DispatcherRuntime
from ummanu.dispatch.types import HostError, OwnershipChanged
from ummanu.dispatch.worker_launch import _worker_launch_failure
from ummanu.runtime.head.local_pty import client as pty_client
from ummanu.tasks import TaskError, TaskWriter


def lock_free(data):
    path = data / "dispatcher" / "cleanup.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(handle, fcntl.LOCK_UN)
        return True


class Audit(MemoryAudit):
    """The generic and typed audit contracts over the same memory owner."""

    def __init__(self, root):
        super().__init__()
        self.board_dir = root / "board"

    def stage(self, request_id, event):
        self._pending[request_id] = copy.deepcopy(event)

    def events(self, reference="", kind=None, **kwargs):
        return [event for event in super().events(reference)
                if kind is None or event["kind"] == kind]

    def pending_marker_owner(self, reference, content, *, request_id):
        return None

    def marker_comment_lock(self, reference):
        return contextlib.nullcontext()


class NarrowCleanupLockTests(unittest.TestCase):
    def setUp(self):
        self.fixture = owned_fixtures.OwnedCleanupTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.data = self.fixture.data
        self.store = tick_fixtures.CountingStore(self.data)
        self.store.rows[1]["reference"] = "sample-1"
        self.store.rows[1]["column_id"] = 7
        self.store.meta[1].update(project="sample", sprint_ref="")
        self.store.add(8, "in_progress", kind="operation")
        for metadata in self.store.meta.values():
            metadata["sprint_ref"] = ""
            if "sprint_status" in metadata:
                metadata["sprint_status"] = "closed"
        self.audit = Audit(self.data)
        self.enterContext(mock.patch("ummanu.board.sql_audit.SqlTaskAudit", return_value=self.audit))
        self.writer = TaskWriter(self.store, data_dir=self.data)
        self.owner = self.fixture.owner
        self.owner.runtime.reader = self.writer.reader
        self.owner.runtime.writer = self.writer
        self.fixture.task = self.writer.reader.show("sample-1")

    def blocked_thread(self, action, entered, release):
        errors = []

        def run():
            try:
                action()
            except Exception as exc:  # noqa: BLE001 - relay thread failures to the test
                errors.append(exc)

        thread = threading.Thread(target=run)
        thread.start()
        self.addCleanup(release.set)
        self.addCleanup(thread.join, 8)
        self.assertTrue(entered.wait(3), errors)
        return thread, errors

    def head_commands(self, suffix=""):
        request = lambda value: value + suffix
        actions = (
            lambda: self.writer.comment(role="po", actor="test-po", reference="sample-1",
                                        body="tick is still running", request_id=request("head-comment")),
            lambda: self.writer.report(role="worker", actor="test-worker", reference="demo-8",
                                       kind="done", body="Operation recorded", request_id=request("head-report")),
            lambda: self.writer.move(role="po", actor="test-po", reference="sample-1", target="ready",
                                     reason="reopen", request_id=request("head-move")),
            lambda: self.writer.complete(role="po", actor="test-po", reference="demo-8", kind="operation",
                                         body="## What was done\nRecorded\n## How to verify\nRead it",
                                         request_id=request("head-complete")),
        )
        for action in actions:
            started = time.monotonic()
            action()
            self.assertLess(time.monotonic() - started, 1)

    def test_head_commands_finish_during_bringup_and_cleanup_replay(self):
        host_entered, host_release = threading.Event(), threading.Event()
        replay_entered, replay_release = threading.Event(), threading.Event()
        host = CommandHostRuntime(FakeCatalog(), self.data, mode="noop", audit=self.audit)
        task = self.writer.reader.show("demo-2")
        key = self.fixture.request()

        def block(entered, release):
            self.assertTrue(lock_free(self.data))
            entered.set()
            self.assertTrue(release.wait(6))

        def tick_body(*args):
            host.prepare_worker(task=task, worker_id="demo-2-worker", head="test-head")
            self.owner.replay_one(key)

        runtime = SimpleNamespace(data_dir=self.data, production_state=production.ProductionState(self.data),
                                  pause=SimpleNamespace(summary=lambda: {"mode": "running"}))
        with mock.patch.object(host, "_run_setup", side_effect=lambda *a, **k: block(host_entered, host_release)), \
                mock.patch.object(host, "_launch", return_value=SimpleNamespace(
                    handle="fake", leaf="", run={}, delivery_evidence={}, head_run={})), \
                mock.patch.object(self.owner, "_scope_fence", side_effect=lambda *a, **k: block(replay_entered, replay_release)), \
                mock.patch.object(production, "_production_mutation_guard", return_value=None), \
                mock.patch.object(production, "_production_tick_body", side_effect=tick_body):
            thread, errors = self.blocked_thread(lambda: production.production_tick(runtime), host_entered, host_release)
            self.addCleanup(replay_release.set)
            self.head_commands("-host")
            thread.join(2)
            self.assertTrue(thread.is_alive())
            # Restore independent fixtures for the second set of commands.
            self.store.rows[1]["column_id"] = 7
            self.store.rows[8]["column_id"] = 3
            host_release.set()
            self.assertTrue(replay_entered.wait(3), errors)
            self.head_commands("-replay")
            thread.join(2)
            self.assertTrue(thread.is_alive())
            replay_release.set()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
        self.assertTrue(self.fixture.workspace.exists())
        self.assertEqual(self.writer.reader.show("sample-1")["state"], "ready")

    def test_move_during_cleanup_proof_refuses_workspace_commit(self):
        key = self.fixture.request()
        entered, release = threading.Event(), threading.Event()
        original = self.owner._dirty

        def proof(*args):
            self.assertTrue(lock_free(self.data))
            entered.set()
            self.assertTrue(release.wait(5))
            return original(*args)

        results = []
        with mock.patch.object(self.owner, "_dirty", side_effect=proof):
            thread, errors = self.blocked_thread(lambda: results.append(self.owner.replay_one(key)), entered, release)
            started = time.monotonic()
            self.writer.move(role="po", actor="test-po", reference="sample-1", target="ready",
                             reason="return to work", request_id="race-move")
            self.assertLess(time.monotonic() - started, 1)
            release.set()
            thread.join(5)
        self.assertEqual(errors, [])
        self.assertTrue(self.fixture.workspace.exists())
        self.assertEqual(results[0]["status"], "pending")
        self.assertNotIn("removal_started", results[0]["progress"])

    def test_original_tick_and_writer_wrappers_block_the_head_command(self):
        """Counterfactual using the two global wrappers present at 93eb9018."""
        entered, release, completed = threading.Event(), threading.Event(), threading.Event()
        runtime = SimpleNamespace(data_dir=self.data, production_state=production.ProductionState(self.data),
                                  pause=SimpleNamespace(summary=lambda: {"mode": "running"}))

        def body(*args):
            entered.set()
            self.assertTrue(release.wait(5))

        def comment():
            self.writer.comment(role="po", actor="test-po", reference="sample-1", body="waiting",
                                request_id="legacy-wait")
            completed.set()

        original_write = TaskWriter._write
        with mock.patch.object(self.writer, "_write", MethodType(cleanup.serialized(original_write), self.writer)), \
                mock.patch.object(production, "_production_mutation_guard", return_value=None), \
                mock.patch.object(production, "_production_tick_body", side_effect=body):
            tick, errors = self.blocked_thread(lambda: cleanup.serialized(production.production_tick)(runtime),
                                               entered, release)
            head = threading.Thread(target=comment)
            head.start()
            self.addCleanup(head.join, 5)
            self.assertFalse(completed.wait(1))
            release.set()
            tick.join(5)
            head.join(5)
            self.assertTrue(completed.is_set())
            self.assertEqual(errors, [])

    def test_external_activation_is_counted_despite_tick_snapshot(self):
        self.store.rows[2]["column_id"] = 6
        self.store.rows[4]["column_id"] = 6
        self.store.add(9, "ready", project="other")
        with tick_snapshot(self.writer.reader) as snapshot:
            snapshot.load()
            # A different process's move is deliberately invisible to board_write.
            self.store.rows[2]["column_id"] = 3
            with self.assertRaisesRegex(TaskError, "capacity"):
                self.writer.claim(role="dispatcher", actor="test-dispatcher", reference="demo-9",
                                  worker="demo-9-worker", cap=1, request_id="capacity-claim")
        self.assertEqual(self.writer.reader.show("demo-9")["state"], "ready")

    def test_journal_updates_survive_replay_and_record_capture(self):
        key = self.fixture.request()
        entered, release = threading.Event(), threading.Event()
        original = self.owner._dirty

        def proof(*args):
            entered.set()
            self.assertTrue(release.wait(5))
            return original(*args)

        with mock.patch.object(self.owner, "_dirty", side_effect=proof):
            thread, errors = self.blocked_thread(lambda: self.owner.replay_one(key), entered, release)
            runtime = SimpleNamespace(host=CommandHostRuntime(FakeCatalog(), self.data, mode="real"),
                                      data_dir=self.data, cleanup=self.owner, reader=self.writer.reader,
                                      production_state=production.ProductionState(self.data))
            DispatcherRuntime.save_records(runtime, {}, {"sample-1": self.fixture.record})
            other = {**self.fixture.task, "id": "other", "ref": "other-1"}
            other_key = self.owner.journal.remember(other, {"attempt_id": "other-attempt"})
            self.owner.journal.generated(self.data / "prompt", "prompt bytes")
            release.set()
            thread.join(5)
        self.assertEqual(errors, [])
        value = cleanup.CleanupJournal(self.data).read()
        self.assertEqual(set(value["intents"]), {key, other_key})
        self.assertIn(str(self.data / "prompt"), value["generated"])
        self.assertTrue(value["intents"][key]["progress"]["workspace_removed"])

    def test_all_cleanup_subprocesses_and_stops_run_without_global_lock(self):
        self.fixture.head()
        key = self.fixture.request()
        original = cleanup._read_git
        stop = self.fixture.stop
        remove = cleanup.git_worktree.remove

        def read_git(*args, **kwargs):
            self.assertTrue(lock_free(self.data))
            return original(*args, **kwargs)

        def stop_head(*args, **kwargs):
            self.assertTrue(lock_free(self.data))
            return stop(*args, **kwargs)

        def remove_workspace(*args, **kwargs):
            self.assertTrue(lock_free(self.data))
            return remove(*args, **kwargs)

        with mock.patch.object(cleanup, "_read_git", side_effect=read_git), \
                mock.patch.object(self.fixture.backend, "stop", side_effect=stop_head), \
                mock.patch.object(cleanup.git_worktree, "remove", side_effect=remove_workspace):
            result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "completed")

    def test_large_journal_mutation_remains_bounded(self):
        journal = cleanup.CleanupJournal(self.data)
        key = journal.remember(self.fixture.task, self.fixture.record.to_json())
        value = journal.read()
        value["intents"][key]["record"]["retained_evidence"] = "x" * 42_000_000
        journal.save(value)
        started = time.monotonic()
        other = {**self.fixture.task, "ref": "large-journal-peer", "id": "peer"}
        journal.remember(other, {"attempt_id": "peer"})
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 5)
        print(f"42 MB journal remember lock hold: {elapsed:.3f}s")

    def test_launch_refuses_changed_claim_and_holds_no_global_lock_at_effect(self):
        task = self.writer.reader.show("demo-2")
        with self.owner.admission(task, launch=True):
            self.assertTrue(lock_free(self.data))
        self.store.meta[2]["claim"] = "replacement-worker"
        with self.assertRaisesRegex(HostError, "ownership changed"), self.owner.admission(task, launch=True):
            self.fail("stale launch admitted")

    def test_launch_admission_covers_only_the_syscall(self):
        task = self.writer.reader.show("demo-2")
        active = False

        @contextlib.contextmanager
        def admission():
            nonlocal active
            with self.owner.admission(task, launch=True):
                active = True
                try:
                    yield
                finally:
                    active = False

        def wait():
            self.assertFalse(active)
            self.assertTrue(lock_free(self.data))
            # End before readiness polling; no real process is launched by this test.
            raise OSError("simulated readiness failure")

        def popen(*args, **kwargs):
            self.assertTrue(active)
            self.assertTrue(lock_free(self.data))
            return SimpleNamespace(wait=wait)

        with mock.patch.object(pty_client.subprocess, "Popen", side_effect=popen), \
                self.assertRaises(pty_client.LocalPtySpawnError):
            pty_client.spawn_head(root=self.data / "heads", run_id="launch-check", role="worker",
                                  task="card:demo-2", command="fake", cwd=self.data,
                                  launch_admission=admission)

    def test_ownership_refusal_preserves_intent_without_blocking_new_owner(self):
        runtime = mock.Mock()
        record = self.fixture.record
        record.launch_intent = {"run_id": "not-yet-launched", "workspace": str(self.fixture.workspace)}
        result = _worker_launch_failure(runtime, {}, {"sample-1": record}, "sample-1", record,
                                        OwnershipChanged("card moved during setup"), step="claim",
                                        attempt_id=record.attempt_id)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(record.launch_intent["run_id"], "not-yet-launched")
        self.assertEqual(runtime.mock_calls, [])

    def test_long_methods_have_no_global_serialization(self):
        methods = (CommandHostRuntime.prepare_worker, CommandHostRuntime.start_review,
                   CommandHostRuntime.restart_worker, CommandHostRuntime.teardown,
                   cleanup.CleanupOwner.replay_one, production.production_tick, production.production_probe)
        for method in methods:
            tree = ast.parse(textwrap.dedent(inspect.getsource(inspect.unwrap(method))))
            self.assertFalse(any(isinstance(node, ast.Name) and node.id in {"serialized", "ownership_lock"}
                                 for node in ast.walk(tree)), method.__name__)

    def assessment_record(self):
        self.store.rows[1]["column_id"] = 5
        self.store.meta[1]["claim"] = self.fixture.record.worker
        self.fixture.task = self.writer.reader.show("sample-1")
        self.fixture.head("worker", generation="")
        self.fixture.head("review", generation="")
        self.fixture.state({"sample-1": self.fixture.record.to_json()})
        return self.fixture.task, self.fixture.record

    def test_assessment_release_disposes_before_done_claim_settlement(self):
        task, record = self.assessment_record()
        result = self.owner.cleanup(task, record, "done")
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["reason"], "cleanup awaits terminal board claim settlement")
        self.assertCountEqual(self.fixture.stops, [("run-worker", ""), ("run-review", "")])
        self.assertFalse(self.fixture.workspace.exists())
        self.assertTrue(result["progress"]["workspace_removed"])
        self.assertFalse(result["progress"].get("claim_settled"))
        self.assertEqual(self.writer.reader.show("sample-1")["claim"]["worker"], record.worker)

    def test_assessment_rework_between_admission_and_removal_preserves_workspace(self):
        task, record = self.assessment_record()
        entered, release = threading.Event(), threading.Event()
        original = self.owner._dirty
        results = []

        def proof(*args):
            entered.set()
            self.assertTrue(release.wait(5))
            return original(*args)

        with mock.patch.object(self.owner, "_dirty", side_effect=proof):
            thread, errors = self.blocked_thread(
                lambda: results.append(self.owner.cleanup(task, record, "done")), entered, release)
            self.writer.move(role="po", actor="test-po", reference="sample-1", target="in_progress",
                             reason="rework", request_id="assessment-race-rework")
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results[0]["status"], "pending")
        self.assertTrue(self.fixture.workspace.exists())
        self.assertNotIn("removal_started", results[0]["progress"])
        self.assertEqual(self.writer.reader.show("sample-1")["state"], "in_progress")

    def test_assessment_decision_release_reaches_real_host_teardown(self):
        task, record = self.assessment_record()
        host = self.fixture.host
        host.mode = "real"
        host.cleanup_owner = self.owner
        host._refuse_legacy_record = lambda *a: None
        host._require_production_runtime = lambda *a: None
        host.teardown = MethodType(CommandHostRuntime.teardown, host)
        host.complete_green = lambda *a: None
        runtime = self.owner.runtime
        runtime.save_records = lambda *a: None
        records = {"sample-1": record}

        def terminal(*args, **kwargs):
            self.assertEqual(self.writer.reader.show("sample-1")["state"], "assessment")
            self.assertFalse(self.fixture.workspace.exists())
            self.assertEqual(len(self.fixture.stops), 2)
            self.store.rows[1]["column_id"] = 7

        with mock.patch.object(assessment_decision, "recorded_decision", return_value=("release", "release", ())), \
                mock.patch.object(release_lifecycle.e2e_stage, "run_stage", return_value=None), \
                mock.patch.object(release_lifecycle, "read_release_gate", return_value=(None, object())), \
                mock.patch("ummanu.dispatch.gate_lifecycle.accept_green_gate", return_value=None), \
                mock.patch.object(release_lifecycle, "decision_pointer", return_value="release"), \
                mock.patch.object(release_lifecycle.attempt_accounting, "terminal_effect", side_effect=terminal):
            result = assessment_decision.advance_assessment(runtime, task, records, {}, "release-attempt")
        self.assertEqual(result["to"], "done")
        self.assertEqual(result["cleanup"]["reason"], "cleanup awaits terminal board claim settlement")
        self.assertTrue(result["cleanup"]["progress"]["workspace_removed"])
        self.assertEqual(records, {})

    def test_final_journal_conflict_is_pending_and_replay_does_not_repeat_effects(self):
        self.fixture.head(generation="")
        key = self.fixture.request()
        original = self.owner.journal.commit_intent
        conflicted = False

        def commit(intent_key, intent):
            nonlocal conflicted
            if intent_key == key and intent["status"] == "completed" and not conflicted:
                conflicted = True
                fresh = self.owner.journal.read()
                fresh["intents"][key]["record"]["concurrent_revision"] = "fresh-owner-evidence"
                self.owner.journal.save(fresh)
            return original(intent_key, intent)

        with mock.patch.object(self.owner.journal, "commit_intent", side_effect=commit), \
                mock.patch.object(cleanup.git_worktree, "remove", wraps=cleanup.git_worktree.remove) as remove:
            results = self.owner.replay()
            self.assertTrue(conflicted)
            self.assertEqual(results[0]["status"], "pending")
            pending = self.owner.journal.read()["intents"][key]
            self.assertEqual(pending["record"]["concurrent_revision"], "fresh-owner-evidence")
            self.assertIn("ownership changed", pending["reason"])
            self.assertEqual(self.owner.replay()[0]["status"], "completed")
            self.assertEqual(remove.call_count, 1)
        self.assertEqual(self.fixture.stops, [("run-worker", "")])

    def test_disposal_runs_without_sql_transaction(self):
        self.fixture.head(generation="")
        key = self.fixture.request()
        namespace = self.fixture.workspace / ".ummanu-task-env"
        namespace.mkdir()
        self.fixture.host._decide_workspace_environment_ownership = (
            lambda path: "dispatcher" if namespace.exists() else "absent")
        active = False

        @contextlib.contextmanager
        def transaction():
            nonlocal active
            self.assertFalse(active)
            active = True
            try:
                yield
            finally:
                active = False

        remove = cleanup.git_worktree.remove

        def dispose(*args, **kwargs):
            self.assertFalse(active)
            self.assertTrue(lock_free(self.data))
            return remove(*args, **kwargs)

        call = self.store.call
        rmtree = cleanup.shutil.rmtree

        def check(method, **kwargs):
            if method == "lockOwnershipReference":
                self.assertTrue(active)
            return call(method, **kwargs)

        def remove_environment(*args, **kwargs):
            self.assertFalse(active)
            return rmtree(*args, **kwargs)

        with mock.patch.object(self.store, "transaction", side_effect=transaction), \
                mock.patch.object(cleanup.git_worktree, "remove", side_effect=dispose) as disposal, \
                mock.patch.object(self.store, "call", side_effect=check), \
                mock.patch.object(cleanup.shutil, "rmtree", side_effect=remove_environment) as environment:
            result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "completed", result["reason"])
        disposal.assert_called_once()
        environment.assert_called_once_with(namespace)
        self.assertFalse(active)

    def test_direct_card_transition_waits_for_disposal_fence_but_comment_finishes(self):
        key = self.fixture.request()
        entered, release = threading.Event(), threading.Event()
        attempted, finished = threading.Event(), threading.Event()
        failures = []
        remove = cleanup.git_worktree.remove

        def dispose(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return remove(*args, **kwargs)

        def move():
            attempted.set()
            try:
                self.writer.board_host.transition(TransitionRequest(
                    EntityKind.CARD, "sample-1", CardState.READY, Actor("po", "test-po"),
                    "reopen after disposal", request_id="direct-transition"))
            except Exception as exc:  # noqa: BLE001 - relay the writer thread's failure
                failures.append(exc)
            finally:
                finished.set()

        with mock.patch.object(cleanup.git_worktree, "remove", side_effect=dispose):
            disposal, errors = self.blocked_thread(lambda: self.owner.replay_one(key), entered, release)
            writer = threading.Thread(target=move)
            writer.start()
            self.addCleanup(writer.join, 5)
            self.assertTrue(attempted.wait(3))
            self.assertFalse(finished.wait(.1))
            started = time.monotonic()
            self.writer.comment(role="po", actor="test-po", reference="sample-1", body="during disposal",
                                request_id="disposal-comment")
            self.assertLess(time.monotonic() - started, 1)
            release.set()
            disposal.join(5)
            writer.join(5)
        self.assertEqual(errors, [])
        self.assertEqual(failures, [])
        self.assertTrue(finished.is_set())
        self.assertEqual(self.writer.reader.show("sample-1")["state"], "ready")

    def test_restore_fences_nonexported_existing_card_before_sql(self):
        attempted, acquired = threading.Event(), threading.Event()

        def existing_card():
            attempted.set()
            with cleanup.reference_lock(self.data, "demo-2"):
                acquired.set()

        thread = threading.Thread(target=existing_card)
        self.addCleanup(thread.join, 5)

        @contextlib.contextmanager
        def transaction():
            # Restore reconciliation can write this existing, nonexported card.
            # Its effect fence must precede SQL/capacity, rather than wait there.
            thread.start()
            self.assertTrue(attempted.wait(3))
            self.assertFalse(acquired.wait(.1))
            yield

        with mock.patch.object(restore, "_normalized_cards", return_value=[{"reference": "sample-1"}]), \
                mock.patch.object(restore, "_import_normalized_board", return_value=0), \
                mock.patch.object(self.store, "transaction", side_effect=transaction):
            self.assertEqual(restore.import_normalized_board(self.data, client=self.store), 0)
        thread.join(5)
        self.assertTrue(acquired.is_set())

    def test_admission_board_reads_run_outside_global_lock(self):
        original = self.writer.reader.show

        def read(*args, **kwargs):
            self.assertTrue(lock_free(self.data))
            return original(*args, **kwargs)

        with mock.patch.object(self.writer.reader, "show", side_effect=read):
            task = read("demo-2")
            with self.owner.admission(task, launch=True):
                self.assertTrue(lock_free(self.data))
            key = self.fixture.request()
            self.owner.replay_one(key)

    def test_observer_cleanup_without_card_reader_stops_and_removes_workspace(self):
        _, path, record = self.fixture.observer()
        owner = cleanup.CleanupOwner(SimpleNamespace(
            data_dir=self.data, host=self.fixture.host, sprints=self.fixture.runtime.sprints))
        result = owner.cleanup_observer(record)
        self.assertEqual(result["status"], "completed", result["reason"])
        self.assertEqual(self.fixture.stops, [("observer-run", "")])
        self.assertFalse(path.exists())
        self.assertTrue(result["progress"]["claim_settled"])

    def test_observer_stop_refusal_without_card_reader_preserves_workspace(self):
        _, path, record = self.fixture.observer()
        self.fixture.stop_failure = True
        owner = cleanup.CleanupOwner(SimpleNamespace(
            data_dir=self.data, host=self.fixture.host, sprints=self.fixture.runtime.sprints))
        result = owner.cleanup_observer(record)
        self.assertEqual(result["status"], "pending")
        self.assertIn("simulated stop failure", result["reason"])
        self.assertEqual(self.fixture.stops, [("observer-run", "")])
        self.assertTrue(path.exists())
        self.assertFalse(result["progress"]["heads_stopped"])

    def test_observer_launch_fences_the_sprint_readers_client(self):
        active = False

        @contextlib.contextmanager
        def transaction():
            nonlocal active
            self.assertTrue(lock_free(self.data))
            active = True
            try:
                yield
            finally:
                active = False

        current = {"id": "sprint-1", "ref": "sprint:1", "status": "open"}

        def show(reference, *, include_cards):
            self.assertTrue(active)
            self.assertTrue(lock_free(self.data))
            self.assertEqual(reference, "sprint:1")
            self.assertFalse(include_cards)
            return copy.deepcopy(current)

        client = mock.Mock(transaction=transaction)
        reader = SimpleNamespace(client=client, show=show)
        owner = cleanup.CleanupOwner(SimpleNamespace(data_dir=self.data, sprints=reader))
        task = {**current, "kind": "observer"}
        with owner.admission(task, launch=True):
            self.assertFalse(active)
            self.assertTrue(lock_free(self.data))
        self.assertFalse(active)
        client.call.assert_called_once_with("lockOwnershipReference", reference="sprint:1", observer=True)
        current["status"] = "closed"
        with self.assertRaisesRegex(OwnershipChanged, "launch ownership changed"), owner.admission(task, launch=True):
            self.fail("changed sprint admitted")


class TickPhaseLockTests(unittest.TestCase):
    def test_cleanup_lock_free_in_every_tick_phase(self):
        fixture = tick_fixtures.TickBoardSnapshotTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.runtime.pause = SimpleNamespace(summary=lambda: {"mode": "running"})
        fixture.runtime.host.committing = lambda commit: contextlib.nullcontext()
        phases = []
        original = production.tick_phase

        @contextlib.contextmanager
        def phase(name):
            self.assertTrue(lock_free(fixture.root), name)
            phases.append(name)
            with original(name):
                yield

        with mock.patch.object(production, "tick_phase", phase), \
                mock.patch.object(production, "_production_mutation_guard", return_value=None):
            result = production.production_tick(fixture.runtime)
        self.assertEqual(result["errors"], [])
        self.assertTrue({"snapshot", "reconcile", "after-merge", "launches"} <= set(phases))
        self.assertNotIn("checkpoint", phases)
