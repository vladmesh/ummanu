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
