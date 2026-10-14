"""File-only membership, recovery and ordered overlap contracts. No process or backend probes."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from tests.support.card_run_fixtures import record
from ummanu.webproto import admission, runs, store_io
from ummanu.webproto.errors import OwnerConflict
from ummanu.webproto.ref_lookup import digest
from ummanu.webproto.runs import RunStore, RunStoreError


class RefLookupTests(unittest.TestCase):
    def setUp(self):
        self.data = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = RunStore(self.data)

    def legacy(self, *records):
        self.store.root.mkdir(parents=True, exist_ok=True)
        for run in records:
            self.store._run_path(run.run_id).write_text(json.dumps(run.to_json()), encoding="utf-8")

    def test_constructors_absent_reads_and_preparation_leave_no_store(self):
        with mock.patch.object(Path, "mkdir", side_effect=AssertionError("read made a directory")):
            self.assertEqual(self.store.read_for_ref("none"), [])
            self.store.prepare_ref_lookup()
        self.assertFalse(self.store.root.exists())
        claimed, _ = self.store.claim("request/雪", lambda rid: record(rid))
        self.assertEqual(self.store.read_for_ref("ummanu-1"), [claimed])

    def test_legacy_bootstrap_repeated_preparation_and_new_claim_preserve_canonical_bytes(self):
        old = record(phase="settled", ended=True, settled_state="finished", settled_reason="done")
        self.legacy(old, record("pr-two", "other"))
        self.store.requests.mkdir()
        request_path = self.store.index.path(old.request_id)
        request_path.write_text(
            json.dumps(
                {"request_id": old.request_id, "run_id": old.run_id, "operation": "op", "fingerprint": "fp"},
                indent=3,
            ),
            encoding="utf-8",
        )
        request_bytes = request_path.read_bytes()
        before = {p: p.read_bytes() for p in self.store.root.glob("*.json")}
        with self.assertRaises(RunStoreError):
            self.store.read_for_ref(old.ref)
        for _ in range(2):
            self.store.prepare_ref_lookup()
            self.assertEqual(self.store.read_for_ref(old.ref), [old])
            self.assertEqual(self.store.read_for_ref("missing"), [])
            self.assertEqual({p: p.read_bytes() for p in before}, before)
            self.assertEqual(request_path.read_bytes(), request_bytes)
            self.assertEqual(
                self.store.claim(
                    old.request_id,
                    lambda _: self.fail("legacy replay built"),
                    operation="op",
                    fingerprint="fp",
                ),
                (old, False),
            )
        new, fresh = self.store.claim("new", lambda rid: replace(record(rid), request_id="new"))
        self.assertTrue(fresh)
        self.assertEqual({r.run_id for r in self.store.read_for_ref(old.ref)}, {old.run_id, new.run_id})

    def test_missing_dirty_corrupt_marker_and_membership_pairs_require_explicit_recovery(self):
        self.legacy(record())
        self.store.prepare_ref_lookup()
        for path, content in (
            (self.store.refs.marker, None),
            (self.store.refs.marker, '{"schema_version":1,"complete":false}'),
            (self.store.refs.marker, "{"),
            (self.store.refs._witness(digest("ummanu-1")), None),
            (self.store.refs._witness(digest("ummanu-1")), '{"ref":"foreign"}'),
            (self.store.refs._path(digest("ummanu-1")), None),
            (self.store.refs._path(digest("ummanu-1")), "[]"),
        ):
            with self.subTest(path=path.name, content=content):
                if content is None:
                    path.unlink()
                else:
                    path.write_text(content, encoding="utf-8")
                with (
                    mock.patch.object(
                        store_io, "write_document", side_effect=AssertionError("read repaired")
                    ),
                    self.assertRaises(RunStoreError),
                ):
                    self.store.read_for_ref("ummanu-1")
                self.store.prepare_ref_lookup()
                self.assertEqual(self.store.read_for_ref("ummanu-1"), [record()])

    def test_leaf_and_canonical_ref_id_path_are_checked(self):
        self.legacy(record())
        self.store.prepare_ref_lookup()
        leaf = self.store.refs._path(digest("ummanu-1"))
        for value in (
            {"ref": "foreign", "run_ids": ["pr-one"]},
            {"ref": "ummanu-1", "run_ids": ["../escape"]},
            {"ref": "ummanu-1", "run_ids": ["pr-one", "pr-one"]},
        ):
            leaf.write_text(json.dumps(value), encoding="utf-8")
            self.store.refs._witness(digest("ummanu-1")).write_text(
                json.dumps(
                    {
                        "ref": "ummanu-1",
                        "sha256": self.store.refs._checksum(value),
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(RunStoreError):
                self.store.read_for_ref("ummanu-1")
            self.store.prepare_ref_lookup()
        for changed in (record("different"), record(ref="foreign")):
            self.store._run_path("pr-one").write_text(json.dumps(changed.to_json()), encoding="utf-8")
            with self.assertRaises(RunStoreError):
                self.store.read_for_ref("ummanu-1")
        self.store._run_path("pr-one").unlink()
        with self.assertRaises(RunStoreError):
            self.store.read_for_ref("ummanu-1")

    def test_failed_bootstrap_leaves_dirty_and_does_not_rewrite_history(self):
        self.legacy(record())
        good = self.store._run_path("pr-one").read_bytes()
        broken = self.store.root / "pr-broken.json"
        broken.write_text("{", encoding="utf-8")
        with self.assertRaises(RunStoreError):
            self.store.prepare_ref_lookup()
        self.assertEqual(self.store._run_path("pr-one").read_bytes(), good)
        with self.assertRaises(RunStoreError):
            self.store.read_for_ref("missing")
        broken.write_text(json.dumps(record("pr-broken", "other").to_json()), encoding="utf-8")
        self.store.prepare_ref_lookup()
        self.assertEqual(self.store.read_for_ref("other"), [record("pr-broken", "other")])

    def test_missing_lookup_directory_and_corrupt_membership_checksum_are_unavailable(self):
        self.legacy(record())
        self.store.prepare_ref_lookup()
        self.store.refs.cards.rename(self.store.refs.root / "lost-cards")
        with self.assertRaises(RunStoreError):
            self.store.read_for_ref("ummanu-1")
        self.store.prepare_ref_lookup()
        path = self.store.refs._path(digest("ummanu-1"))
        path.write_text('{"ref":"ummanu-1","run_ids":[]}', encoding="utf-8")
        with self.assertRaisesRegex(RunStoreError, "corrupt"):
            self.store.read_for_ref("ummanu-1")
        self.store.prepare_ref_lookup()
        self.assertEqual(self.store.read_for_ref("ummanu-1"), [record()])

    def test_preparation_rebuilds_ref_changes_made_by_a_stopped_released_writer(self):
        self.legacy(record())
        self.store.prepare_ref_lookup()
        # A released writer does not change the new marker. It must be stopped before recovery.
        self.legacy(record(ref="changed-before-restart"))
        self.store.prepare_ref_lookup()
        self.assertEqual(self.store.read_for_ref("ummanu-1"), [])
        self.assertEqual(
            self.store.read_for_ref("changed-before-restart"), [record(ref="changed-before-restart")]
        )

    def test_every_writer_holds_one_lock_and_orders_dirty_canonical_lookup_complete(self):
        run, _ = self.store.claim("request/雪", lambda rid: record(rid), operation="op", fingerprint="fp")
        locked = False
        events = []
        real_write = store_io.write_text_atomic

        def write(path, body):
            self.assertTrue(locked)
            payload = json.loads(body)
            if path == self.store.refs.marker:
                events.append("complete" if payload["complete"] else "dirty")
            elif path.parent == self.store.root:
                events.append("canonical")
            elif path.parent == self.store.requests:
                events.append("request")
            else:
                events.append("lookup")
            real_write(path, body)

        from contextlib import contextmanager

        @contextmanager
        def lock(path):
            nonlocal locked
            self.assertEqual(path, self.store.lock_path)
            self.assertFalse(locked)
            locked = True
            try:
                yield
            finally:
                locked = False

        for action in (
            lambda: self.store.claim("second", lambda rid: replace(record(rid), request_id="second")),
            lambda: self.store.save(replace(run, started_at=5)),
            lambda: self.store.settle(run.run_id, "finished", "done", now=8),
        ):
            events.clear()
            with (
                mock.patch.object(runs, "file_lock", lock),
                mock.patch.object(store_io, "write_text_atomic", write),
            ):
                action()
            self.assertEqual(events[0:2], ["dirty", "canonical"])
            complete = events.index("complete")
            self.assertGreater(complete, 2)
            self.assertEqual(set(events[2:complete]), {"lookup"})
        settled = self.store.get(run.run_id)
        self.assertTrue(settled.ended)
        self.assertEqual(self.store.settle(run.run_id, "process_failed", "later", now=99), (settled, False))
        self.assertEqual(
            self.store.claim(
                "request/雪", lambda _: self.fail("replay built"), operation="op", fingerprint="fp"
            ),
            (settled, False),
        )

    def test_interrupted_writes_refuse_until_recovery_and_never_lose_canonical_state(self):
        real_write = store_io.write_text_atomic
        for phase in ("canonical", "membership", "presence", "complete"):
            with self.subTest(phase=phase):
                self.legacy(record())
                self.store.prepare_ref_lookup()

                def interrupt(path, body, phase=phase):
                    match = (
                        path == self.store._run_path("pr-new")
                        if phase == "canonical"
                        else path.parent == self.store.refs.cards
                        if phase == "membership"
                        else path.parent == self.store.refs.present
                        if phase == "presence"
                        else path == self.store.refs.marker and json.loads(body)["complete"]
                    )
                    if match:
                        raise RuntimeError("interrupted")
                    real_write(path, body)

                with (
                    mock.patch.object(store_io, "write_text_atomic", interrupt),
                    self.assertRaises(RunStoreError),
                ):
                    self.store.save(record("pr-new", "new-card"))
                for ref in ("ummanu-1", "new-card", "missing"):
                    with self.assertRaises(RunStoreError):
                        self.store.read_for_ref(ref)
                expected = self.store.get("pr-new")
                self.store.prepare_ref_lookup()
                self.assertEqual(self.store.read_for_ref("new-card"), [expected] if expected else [])

    def test_writer_overlap_during_canonical_read_or_absence_proof_is_unavailable(self):
        self.legacy(record())
        self.store.prepare_ref_lookup()
        get = self.store.get

        # An ordered interleaving, no thread timing or process liveness claim.
        def overlap(run_id):
            result = get(run_id)
            RunStore(self.data).save(record("pr-new"))
            return result

        with (
            mock.patch.object(self.store, "get", side_effect=overlap),
            self.assertRaisesRegex(RunStoreError, "changed during"),
        ):
            self.store.read_for_ref("ummanu-1")
        ids = self.store.refs.ids

        def absent_then_write(ref):
            result = ids(ref)
            RunStore(self.data).save(record("pr-absent", ref))
            return result

        with (
            mock.patch.object(self.store.refs, "ids", side_effect=absent_then_write),
            self.assertRaisesRegex(RunStoreError, "changed during"),
        ):
            self.store.read_for_ref("absent")
        self.assertEqual(self.store.read_for_ref("absent"), [record("pr-absent", "absent")])

    def test_reassignment_updates_both_memberships_and_admission_keeps_canonical_refusal(self):
        self.legacy(record())
        self.store.prepare_ref_lookup()
        self.store.save(record(ref="moved"))
        self.assertEqual(self.store.read_for_ref("ummanu-1"), [])
        self.assertEqual(self.store.read_for_ref("moved"), [record(ref="moved")])
        self.store.refs.marker.unlink()
        with (
            mock.patch.object(admission, "_card", return_value={"project": "ummanu", "state": "issues"}),
            mock.patch.object(admission, "_binding", return_value={}),
            mock.patch.object(admission, "_refuse_reserved_project"),
            mock.patch.object(
                self.store, "read_for_ref", side_effect=AssertionError("admission used lookup")
            ),
            self.assertRaises(OwnerConflict),
        ):
            admission.admit(
                "moved",
                report=None,
                data_dir=self.data,
                board=None,
                store=self.store,
                production_state=self.data / "absent-state",
            )
