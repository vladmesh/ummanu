"""Bulk recovery uses constant descriptors and excludes real per-card effects."""

from __future__ import annotations

import contextlib
import copy
import fcntl
import json
import os
import resource
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from tests import test_narrow_cleanup_lock as narrow
from tests.fakes.card_restore import BOARD, COLUMNS, RETIRED, StoreModel
from ummanu import restore, task_restore
from ummanu.dispatch import cleanup
from ummanu.dispatch.types import HostError
from ummanu.tasks import TaskError, TaskWriter


def open_lock_paths() -> list[str]:
    paths = []
    for descriptor in Path("/proc/self/fd").iterdir():
        try:
            path = os.readlink(descriptor)
        except FileNotFoundError:
            continue
        if path.endswith(".lock"):
            paths.append(path)
    return sorted(paths)


class RestoreStore(StoreModel):
    _depth = 0

    def __init__(self, root):
        super().__init__()
        self.instance_dir = root
        self.lock_counts = []

    def transaction(self):
        return contextlib.nullcontext()

    def set_restore_phase(self, phase):
        paths = open_lock_paths()
        assert not any("/effects/" in path or "/admission/" in path for path in paths), paths
        self.lock_counts.append(len(paths))

    def _rpc_getProjectByName(self, *, name):
        return {"id": BOARD, "name": name}

    def _rpc_getColumns(self, *, project_id):
        return [{"id": key, "title": title} for key, title in COLUMNS.items()]

    def _rpc_getAllComments(self, *, task_id):
        return []


class RestoreAudit(narrow.Audit):
    def pending_events(self):
        return copy.deepcopy(list(self._pending.values()))


def restore_under_limit() -> None:
    """Run the complete import, including real normalization, writes and parity."""
    _, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, hard))
    results = []
    for count in (10, 1500):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            board = root / "board"
            board.mkdir()
            cards = []
            for index in range(1, count + 1):
                card = copy.deepcopy(RETIRED)
                card.update(id=index, reference=f"personal_site-{index}", position=index)
                card["metadata"]["claim"] = f"personal_site-{index}-worker"
                card["fields"]["claim"] = card["metadata"]["claim"]
                cards.append(card)
            (board / "cards.json").write_text(json.dumps({"version": 1, "cards": cards}))
            store = RestoreStore(root)
            audit = RestoreAudit(root)
            with mock.patch("ummanu.board.sql_audit.SqlTaskAudit", return_value=audit):
                assert restore.import_normalized_board(root, client=store) == count
            assert len(store.rows) == count
            assert all(row["archived"] for row in store.rows.values())
            assert len(audit.events()) == count
            assert not audit.pending_events()
            assert restore.restore_state(root)["board_parity"] == "complete"
            assert not (root / "dispatcher" / "effects").exists()
            results.append({"cards": count, "max_lock_fds": max(store.lock_counts)})
    assert results[0]["max_lock_fds"] == results[1]["max_lock_fds"] == 3, results
    print(json.dumps({"soft_limit": 256, "restores": results}))


class BulkLaneTests(unittest.TestCase):
    def setUp(self):
        self.fixture = narrow.NarrowCleanupLockTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.data = self.fixture.data
        self.writer = self.fixture.writer
        self.fixture.audit.pending_events = lambda: copy.deepcopy(list(self.fixture.audit._pending.values()))

    def thread(self, action):
        attempted, finished = threading.Event(), threading.Event()
        failures = []

        def run():
            attempted.set()
            try:
                action()
            except Exception as exc:  # noqa: BLE001 - propagate failures from the thread
                failures.append(exc)
            finally:
                finished.set()

        thread = threading.Thread(target=run)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.assertTrue(attempted.wait(3))
        return thread, finished, failures

    def move(self, reference="sample-1", request="bulk-head-move"):
        self.writer.move(role="po", actor="test-po", reference=reference, target="ready",
                         reason="reopen", request_id=request)

    def test_complete_restore_over_soft_limit_has_constant_lock_descriptors(self):
        result = subprocess.run(
            [sys.executable, "-c", "from tests.test_bulk_ownership_lane import restore_under_limit; restore_under_limit()"],
            env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
            capture_output=True, text=True, timeout=90, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt, {"soft_limit": 256, "restores": [
            {"cards": 10, "max_lock_fds": 3}, {"cards": 1500, "max_lock_fds": 3}]})

    def test_move_waits_for_exclusive_bulk_writer(self):
        with cleanup.bulk_lane(self.data, exclusive=True):
            thread, finished, failures = self.thread(self.move)
            self.assertFalse(finished.wait(.1))
            self.assertEqual(self.writer.reader.show("sample-1")["state"], "done")
        thread.join(5)
        self.assertTrue(finished.is_set())
        self.assertEqual(failures, [])
        self.assertEqual(self.writer.reader.show("sample-1")["state"], "ready")

    def test_independent_head_moves_share_the_lane(self):
        entered, release = threading.Event(), threading.Event()
        original = self.writer.board_host._move_card

        def move_card(card, target):
            if card.ref == "sample-1":
                entered.set()
                self.assertTrue(release.wait(5))
            return original(card, target)

        self.addCleanup(release.set)
        with mock.patch.object(self.writer.board_host, "_move_card", side_effect=move_card):
            first, first_done, first_failures = self.thread(self.move)
            self.assertTrue(entered.wait(3), first_failures)
            second, second_done, second_failures = self.thread(
                lambda: self.move("demo-2", "independent-head-move"))
            try:
                self.assertTrue(second_done.wait(1), second_failures)
                self.assertFalse(first_done.is_set())
            finally:
                release.set()
                first.join(5)
                second.join(5)
        self.assertEqual(first_failures + second_failures, [])

    def test_exclusive_bulk_writer_waits_for_disposal(self):
        key = self.fixture.fixture.request()
        entered, release, acquired = threading.Event(), threading.Event(), threading.Event()
        remove = cleanup.git_worktree.remove

        def dispose(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return remove(*args, **kwargs)

        def bulk():
            with cleanup.bulk_lane(self.data, exclusive=True):
                acquired.set()
                self.assertFalse(self.fixture.fixture.workspace.exists())

        self.addCleanup(release.set)
        with mock.patch.object(cleanup.git_worktree, "remove", side_effect=dispose):
            disposal, _, errors = self.thread(lambda: self.fixture.owner.replay_one(key))
            self.assertTrue(entered.wait(3), errors)
            writer, finished, failures = self.thread(bulk)
            self.assertFalse(acquired.wait(.1))
            release.set()
            disposal.join(5)
            writer.join(5)
        self.assertTrue(finished.is_set())
        self.assertEqual(errors + failures, [])

    def test_cleanup_waits_for_bulk_writer_and_revalidates_restored_state(self):
        key = self.fixture.fixture.request()
        with cleanup.bulk_lane(self.data, exclusive=True):
            thread, finished, failures = self.thread(lambda: self.fixture.owner.replay_one(key))
            self.assertFalse(finished.wait(.1))
            self.move()
        thread.join(5)
        self.assertTrue(finished.is_set())
        self.assertEqual(failures, [])
        self.assertTrue(self.fixture.fixture.workspace.exists())

    def test_bulk_change_after_cleanup_admission_refuses_stale_commit(self):
        key = self.fixture.fixture.request()
        entered, release = threading.Event(), threading.Event()
        original = self.fixture.owner._dirty
        results = []

        def proof(*args):
            entered.set()
            self.assertTrue(release.wait(5))
            return original(*args)

        self.addCleanup(release.set)
        with mock.patch.object(self.fixture.owner, "_dirty", side_effect=proof):
            thread, finished, failures = self.thread(lambda: results.append(self.fixture.owner.replay_one(key)))
            self.assertTrue(entered.wait(3), failures)
            with cleanup.bulk_lane(self.data, exclusive=True):
                # Blocked is admissible for cleanup: only the saved admission
                # identity prevents committing a decision made against Done.
                self.writer.move(role="po", actor="test-po", reference="sample-1", target="blocked",
                                 reason="restored ownership", request_id="bulk-changed-ownership")
            release.set()
            thread.join(5)
        self.assertTrue(finished.is_set())
        self.assertEqual(failures, [])
        self.assertTrue(self.fixture.fixture.workspace.exists())
        self.assertEqual(results[0]["status"], "pending")
        self.assertIn("ownership changed since admission", results[0]["reason"])
        self.assertNotIn("removal_started", results[0]["progress"])

    def test_reentrant_bulk_helpers_open_no_card_or_capacity_descriptors(self):
        with mock.patch.object(cleanup.fcntl, "flock", wraps=fcntl.flock) as flock, \
                cleanup.bulk_lane(self.data, exclusive=True):
            for index in range(1500):
                with cleanup.reference_lock(self.data, f"demo-{index}"), \
                        cleanup.reference_lock(self.data, "capacity", lane="admission"), \
                        cleanup.bulk_lane(self.data, exclusive=True):
                    self.assertEqual(len(open_lock_paths()), 1)
        self.assertEqual([call.args[1] for call in flock.call_args_list], [fcntl.LOCK_EX, fcntl.LOCK_UN])

    def test_pending_order_repair_holds_one_lock_for_1500_references(self):
        store = RestoreStore(self.data)
        references = [f"demo-{index}" for index in range(1, 1501)]
        for index, reference in enumerate(references, 1):
            key = store.call("createTask", project_id=BOARD, reference=reference, title=reference)
            store.rows[key]["position"] = index
        writer = TaskWriter(store, data_dir=self.data)
        event = {"payload": {"column": COLUMNS[1], "swimlane": "", "references": references,
                             "references_sha256": task_restore._restore_order_digest(references)}}
        original = task_restore._live_restore_group
        observed = []

        def group(*args):
            observed.append(open_lock_paths())
            return original(*args)

        with mock.patch.object(task_restore, "_live_restore_group", side_effect=group):
            task_restore.finish_pending_restore_order(writer, event)
        self.assertEqual(len(observed), 2)
        self.assertTrue(all(paths == [str(self.data / "dispatcher" / "board-bulk.lock")]
                            for paths in observed))

    def test_shared_reentrancy_keeps_one_descriptor_and_refuses_upgrade(self):
        with cleanup.bulk_lane(self.data):
            with cleanup.bulk_lane(self.data):
                self.assertEqual(len(open_lock_paths()), 1)
            with self.assertRaises(HostError), cleanup.bulk_lane(self.data, exclusive=True):
                self.fail("a shared holder authorized an exclusive writer")

    def test_pre_import_board_read_task_error_is_restore_error(self):
        (self.data / "board").mkdir(parents=True, exist_ok=True)
        (self.data / "board" / "cards.json").write_text(json.dumps({"version": 1, "cards": []}))
        error = TaskError("backend_unavailable", "board read unavailable", 1)
        with mock.patch("ummanu.restore.TaskReader._board", side_effect=error), \
                self.assertRaisesRegex(restore.RestoreError, "board read unavailable"):
            restore.import_normalized_board(self.data, client=self.fixture.store)

    def test_restore_preserves_lock_order_and_error_scope(self):
        entered = []

        @contextlib.contextmanager
        def lock(name):
            entered.append(name)
            yield

        with mock.patch.object(restore, "file_lock", side_effect=lambda *a: lock("restore")), \
                mock.patch("ummanu.sprints.sprint_admission_lock", side_effect=lambda *a: lock("sprint")), \
                mock.patch.object(restore, "bulk_lane", side_effect=lambda *a, **k: lock("bulk")), \
                mock.patch.object(self.fixture.store, "transaction", side_effect=lambda: lock("SQL")), \
                mock.patch.object(restore, "_import_normalized_board", side_effect=TaskError("backend_error", "read failed", 1)), \
                self.assertRaisesRegex(restore.RestoreError, "read failed"):
            restore.import_normalized_board(self.data, client=self.fixture.store)
        self.assertEqual(entered, ["restore", "sprint", "bulk", "SQL"])
