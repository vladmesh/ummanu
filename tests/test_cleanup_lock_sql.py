"""CI-only PostgreSQL proofs of capacity and the ownership row fence."""

from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.fakes.tasks import CardSeed
from tests.sql_backend_fixtures import CardStoreCase
from ummanu.board.sql_cards import SqlCardClient
from ummanu.board.tick_snapshot import tick_snapshot
from ummanu.dispatch.cleanup import CleanupOwner
from ummanu.tasks import TaskError, TaskWriter


class CleanupLockSqlTests(CardStoreCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        seed = CardSeed([
            {"id": 12, "reference": "alpha-1", "title": "Claim", "column_id": 2},
            {"id": 13, "reference": "beta-1", "title": "External", "column_id": 6},
            {"id": 14, "reference": "gamma-1", "title": "Operation", "column_id": 3},
        ], {
            12: {"project": "alpha", "task_type": "code"},
            13: {"project": "beta", "task_type": "code"},
            14: {"project": "gamma", "task_type": "operation"},
        })
        self.client = self.card_store(seed, instance_dir=self.root)
        self.writer = TaskWriter(self.client, data_dir=self.root)
        self.external_client = SqlCardClient(self.client.credentials, self.root)
        self.addCleanup(self.external_client.close)
        self.external = TaskWriter(self.external_client, data_dir=self.root)

    def test_mid_tick_external_activation_prevents_claim_over_capacity(self):
        with tick_snapshot(self.writer.reader) as snapshot:
            snapshot.load()
            moved = threading.Event()
            failures = []

            def external_move():
                try:
                    self.external.move(role="po", actor="test-po", reference="beta-1",
                                       target="in_progress", reason="external admission", request_id="external-move")
                except Exception as exc:  # noqa: BLE001 - propagate the independent connection's failure
                    failures.append(exc)
                finally:
                    moved.set()

            thread = threading.Thread(target=external_move)
            thread.start()
            thread.join(5)
            self.assertTrue(moved.is_set())
            self.assertEqual(failures, [])
            with self.assertRaises(TaskError) as raised:
                self.writer.claim(role="dispatcher", actor="test-dispatcher", reference="alpha-1",
                                  worker="alpha-worker", cap=1, request_id="tick-claim")
            self.assertEqual(raised.exception.code, "capacity_reached")
        self.assertEqual(self.writer.reader.show("alpha-1")["state"], "ready")

    def test_concurrent_claims_admit_only_one_head(self):
        self.external.move(role="po", actor="test-po", reference="beta-1", target="ready",
                           reason="claim candidate", request_id="ready-beta")
        barrier = threading.Barrier(2)
        results = []

        def claim(writer, reference):
            barrier.wait(5)
            try:
                writer.claim(role="dispatcher", actor="test-dispatcher", reference=reference,
                             worker=reference + "-worker", cap=1, request_id="claim-" + reference)
                results.append("claimed")
            except TaskError as exc:
                results.append(exc.code)

        threads = [threading.Thread(target=claim, args=pair)
                   for pair in ((self.writer, "alpha-1"), (self.external, "beta-1"))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertCountEqual(results, ["claimed", "capacity_reached"])

    def test_ownership_row_fences_raw_state_write_but_allows_head_comment(self):
        owner = CleanupOwner(SimpleNamespace(data_dir=self.root, reader=self.writer.reader))
        task = self.writer.reader.show("alpha-1")
        attempted, finished = threading.Event(), threading.Event()
        failures = []

        def raw_move():
            attempted.set()
            try:
                self.external_client.call("moveTaskPosition", project_id=1, task_id=12,
                                          column_id=3, position=1)
            except Exception as exc:  # noqa: BLE001 - relay the raw SQL update's failure
                failures.append(exc)
            finally:
                finished.set()

        comment_client = SqlCardClient(self.client.credentials, self.root)
        self.addCleanup(comment_client.close)
        comment_writer = TaskWriter(comment_client, data_dir=self.root)
        show = self.writer.reader.show

        def admission_read(*args, **kwargs):
            self.assertGreater(self.client._depth, 0)
            thread = threading.Thread(target=raw_move)
            thread.start()
            self.addCleanup(thread.join, 5)
            self.assertTrue(attempted.wait(3))
            self.assertFalse(finished.wait(.1))
            started = time.monotonic()
            comment_writer.comment(role="po", actor="test-po", reference="alpha-1", body="during admission",
                                   request_id="admission-comment")
            self.assertLess(time.monotonic() - started, 1)
            return show(*args, **kwargs)

        with mock.patch.object(self.writer.reader, "show", side_effect=admission_read), \
                owner.admission(task, launch=True):
            self.assertEqual(self.client._depth, 0)
            self.assertTrue(finished.wait(5))
        self.assertTrue(finished.is_set())
        self.assertEqual(failures, [])
        self.assertEqual(self.writer.reader.show("alpha-1")["state"], "in_progress")

    def deadline(self, seconds):
        end = time.monotonic() + seconds
        return lambda: end - time.monotonic()

    def settings(self):
        with self.client.transaction():
            return self.client._query("SHOW lock_timeout"), self.client._query("SHOW statement_timeout")

    def test_cleanup_deadline_bounds_a_held_row_slow_statements_and_a_cold_connection(self):
        """ummanu-145: one caller deadline over the real driver; nothing renews it, nothing outlives it."""
        unbounded = self.settings()
        holding, release = threading.Event(), threading.Event()

        def hold():
            with self.external_client.transaction():
                self.external_client.call("lockOwnershipReference", reference="alpha-1")
                holding.set()
                release.wait(10)

        thread = threading.Thread(target=hold)
        thread.start()
        self.addCleanup(thread.join, 10)
        self.addCleanup(release.set)
        self.assertTrue(holding.wait(5))
        # A held row: the lock wait is the deadline's, and the refusal rolls the transaction back.
        started = time.monotonic()
        with self.client.within(self.deadline(0.5)), self.assertRaises(TaskError), self.client.transaction():
            self.client.call("lockOwnershipReference", reference="alpha-1")
        self.assertLess(time.monotonic() - started, 1.5)
        release.set()
        thread.join(10)
        # Successive slow statements in one transaction: the second gets only what is left.
        started = time.monotonic()
        with self.client.within(self.deadline(1.0)), self.assertRaises(TaskError), self.client.transaction():
            self.client._query("SELECT pg_sleep(0.6)")
            self.client._query("SELECT pg_sleep(0.6)")
        self.assertLess(time.monotonic() - started, 1.5)
        # Standalone reads too, each a transaction of its own.
        started = time.monotonic()
        with self.client.within(self.deadline(1.0)), self.assertRaises(TaskError):
            self.client._query("SELECT pg_sleep(0.6)")
            self.client._query("SELECT pg_sleep(0.6)")
        self.assertLess(time.monotonic() - started, 1.5)
        # A cold client opens a connection only with the two seconds libpq can count, then works.
        cold = SqlCardClient(self.client.credentials, self.root)
        self.addCleanup(cold.close)
        with cold.within(self.deadline(1.5)), self.assertRaises(TaskError):
            cold._query("SELECT 1")
        with cold.within(self.deadline(5.0)), cold.transaction():
            self.assertTrue(cold.call("lockOwnershipReference", reference="alpha-1"))
        # The bound ended with each transaction and each block: the client's own policy is back.
        self.assertEqual(self.settings(), unbounded)
        with cold.transaction():
            self.assertEqual((cold._query("SHOW lock_timeout"), cold._query("SHOW statement_timeout")), unbounded)

    def test_cleanup_deadline_cuts_the_driver_wait_itself_and_discards_the_connection(self):
        """ummanu-145: a raw statement with no server-side bound is cut by the driver wait seam."""
        stalled = SqlCardClient(self.client.credentials, self.root)
        self.addCleanup(stalled.close)
        stalled._query("SELECT 1")  # pooled before the deadline, as the dispatcher's connection is
        started = time.monotonic()
        with stalled.within(self.deadline(1.0)), self.assertRaises(TaskError), stalled._session(), \
                stalled.connection.cursor() as cursor:
            cursor.execute("SELECT pg_sleep(3)")
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(stalled._open, 0)  # the cut connection was closed, never pooled again
        self.assertEqual(stalled._query("SELECT 1"), [(1,)])
