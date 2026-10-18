"""Durable frozen plans survive retry and refuse unsafe state without regeneration."""

import copy
import json
import os
import stat
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from tests.po_checkpoint_fixtures import po_records, write_po
from ummanu.board.po_checkpoint import PoCheckpointError, read_checkpoint_po
from ummanu.board.po_restore import normalized_po_records, prepare_po_restore

MOMENT = datetime(2026, 10, 1, tzinfo=UTC)


class PoRestorePlanTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.data = self.root / "data"
        self.data.mkdir()
        self.path = self.data / ".po-restore" / "plan.json"
        self.records = po_records()
        self.records["po_sessions"][1]["cli"] = "claude"

    def prepare(self, *, records=None, moment=MOMENT, target=None):
        return prepare_po_restore(self.records if records is None else records,
                                  self.data if target is None else target, interrupted_at=moment)

    def assert_refused_unchanged(self, payload):
        self.path.write_bytes(payload)
        with (
            mock.patch("uuid.uuid4", side_effect=AssertionError("generated on refusal")),
            self.assertRaises(PoCheckpointError) as failed,
        ):
            self.prepare()
        self.assertIn("PO restore plan", str(failed.exception))
        for secret in ("private-credential", "repeated text", "native-provider-context", str(self.data)):
            self.assertNotIn(secret, str(failed.exception))
        self.assertEqual(self.path.read_bytes(), payload)

    def test_checkpoint_capture_prepares_private_minimal_plan_and_reloads_both_providers(self):
        board = self.root / "board"
        board.mkdir()
        (board / "export.json").write_text("{}")
        write_po(board, self.records)
        records = read_checkpoint_po(board)
        plan = self.prepare(records=records)
        saved = json.loads(self.path.read_bytes())
        self.assertEqual(set(saved), {"version", "source_sha256", "data_dir", "interrupted_at", "native_bindings"})
        self.assertEqual(saved["native_bindings"], [list(pair) for pair in plan.native_bindings])
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        before = self.path.stat()
        with mock.patch("uuid.uuid4", side_effect=AssertionError("generated on retry")):
            retry = self.prepare(records=records, moment=MOMENT + timedelta(days=10))
        self.assertEqual(retry, plan)
        self.assertEqual(normalized_po_records(records, retry), normalized_po_records(records, plan))
        self.assertIsInstance(retry.native_bindings, tuple)
        self.assertTrue(all(isinstance(pair, tuple) for pair in retry.native_bindings))
        self.assertIsNone(dict(retry.native_bindings)["closed"])
        self.assertIsNotNone(dict(retry.native_bindings)["empty"])
        after = self.path.stat()
        self.assertEqual((after.st_ino, after.st_mtime_ns), (before.st_ino, before.st_mtime_ns))

    def test_strict_json_whitelist_types_and_encoding_refuse_without_replacement(self):
        self.prepare()
        saved = json.loads(self.path.read_bytes())
        invalid = [b"", b"{", b"\xff", b"[]", b"null", b'{"private-credential": NaN}',
                   b'{"version": 1, "version": 1}']
        for field, value in (
            ("version", True), ("version", 2), ("version", "1"),
            ("source_sha256", None), ("source_sha256", "A" * 64), ("source_sha256", "0" * 64),
            ("data_dir", None), ("data_dir", "/private-credential"),
            ("interrupted_at", 1), ("interrupted_at", "private-credential"),
            ("interrupted_at", "2020-01-01T00:00:00Z"),
            ("interrupted_at", "2026-10-01T00:00:00+00:00"),
            ("native_bindings", {}), ("native_bindings", [None]),
            ("native_bindings", [["empty"]]), ("native_bindings", [[1, None]]),
            ("native_bindings", [["empty", {}]]),
        ):
            invalid.append(json.dumps({**saved, field: value}).encode())
        for field in saved:
            invalid.append(json.dumps({key: value for key, value in saved.items() if key != field}).encode())
        invalid.append(json.dumps({**saved, "private-credential": "private-credential"}).encode())
        valid_text = json.dumps(saved)
        invalid.append(valid_text.replace('"version": 1', '"version": 1, "version": 1').encode())
        for payload in invalid:
            with self.subTest(payload_class=type(payload).__name__, size=len(payload)):
                self.assert_refused_unchanged(payload)

    def test_missing_duplicate_reordered_stale_and_invalid_native_bindings_are_refused(self):
        self.records["po_sessions"][2]["cli"] = "claude"
        old_id = "11111111-1111-4111-8111-111111111111"
        self.records["po_sessions"][0]["cli_session_id"] = old_id
        self.prepare()
        saved = json.loads(self.path.read_bytes())
        bindings = saved["native_bindings"]
        for altered in (
            [], bindings[:-1], bindings + [bindings[-1]], list(reversed(bindings)),
            [bindings[0], bindings[0], bindings[2]],
            [["closed", "private-credential"], *bindings[1:]],
            [bindings[0], ["empty", "native-provider-context"], bindings[2]],
            [bindings[0], ["empty", old_id], bindings[2]],
            [bindings[0], ["empty", None], bindings[2]],
            [bindings[0], ["empty", "ABCDEFAB-CDEF-4ABC-8DEF-ABCDEFABCDEF"], bindings[2]],
            [bindings[0], bindings[1], ["open", bindings[1][1]]],
        ):
            with self.subTest(binding_count=len(altered)):
                self.assert_refused_unchanged(json.dumps({**saved, "native_bindings": altered}).encode())

    def test_unfinished_plan_refusal_names_the_safe_field_without_its_value(self):
        self.records["po_turns"][-1]["started_at"] = "2026-09-30T00:00:00Z"
        self.prepare()
        saved = json.loads(self.path.read_bytes())
        other_digest = "f" * 64 if saved["source_sha256"] != "f" * 64 else "e" * 64
        for field, value in (
            ("source_sha256", other_digest),
            ("data_dir", str(self.root / "elsewhere")),
            ("interrupted_at", "2026-10-01 00:00:00"),
            ("interrupted_at", "2026-09-29T00:00:00Z"),
        ):
            payload = json.dumps({**saved, field: value}).encode()
            self.path.write_bytes(payload)
            with self.subTest(field=field, value=value), self.assertRaises(PoCheckpointError) as failed:
                self.prepare()
            self.assertIn(f"PO restore plan field {field} ", str(failed.exception))
            self.assertNotIn(value, str(failed.exception))
            self.assertEqual(self.path.read_bytes(), payload)

    def test_source_digest_ignores_object_key_order_but_binds_unicode_and_array_order(self):
        plan = self.prepare()
        before = self.path.read_bytes()
        reordered = {table: [dict(reversed(list(row.items()))) for row in rows]
                     for table, rows in reversed(list(self.records.items()))}
        self.assertEqual(self.prepare(records=reordered), plan)
        for change in ("unicode", "type", "order"):
            records = copy.deepcopy(self.records)
            if change == "unicode":
                records["po_feed"][0]["text"] += "ё"
            elif change == "type":
                records["po_feed"][0]["metadata"]["nested"][0] = 1
            else:
                records["po_feed"][0]["metadata"]["nested"].reverse()
            with self.subTest(change=change), self.assertRaises(PoCheckpointError):
                self.prepare(records=records)
            self.assertEqual(self.path.read_bytes(), before)

    def test_copied_plan_foreign_target_and_symlink_components_are_refused(self):
        self.prepare()
        before = self.path.read_bytes()
        other = self.root / "other"
        other.mkdir()
        state = other / ".po-restore"
        state.mkdir(mode=0o700)
        copied = state / "plan.json"
        copied.write_bytes(before)
        copied.chmod(0o600)
        link = self.root / "link"
        link.symlink_to(self.data, target_is_directory=True)
        for target in (other, link, link / ".po-restore", Path("relative")):
            with self.subTest(target=target), self.assertRaises(PoCheckpointError):
                self.prepare(target=target)
        with mock.patch("os.geteuid", return_value=os.geteuid() + 1), self.assertRaises(PoCheckpointError):
            self.prepare()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(copied.read_bytes(), before)

    def test_plan_symlink_dangling_link_hardlink_and_directory_never_mean_absence(self):
        self.prepare()
        before = self.path.read_bytes()
        stored = self.root / "stored"
        self.path.rename(stored)
        for target in (stored, self.root / "missing"):
            self.path.symlink_to(target)
            with self.subTest(kind="symlink"), self.assertRaises(PoCheckpointError):
                self.prepare()
            self.assertTrue(self.path.is_symlink())
            self.path.unlink()
        os.link(stored, self.path)
        with self.assertRaises(PoCheckpointError):
            self.prepare()
        self.path.unlink()
        self.path.mkdir()
        with self.assertRaises(PoCheckpointError):
            self.prepare()
        self.path.rmdir()
        self.path.parent.rmdir()
        self.path.parent.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(PoCheckpointError):
            self.prepare()
        self.assertEqual(stored.read_bytes(), before)

    def test_unsafe_permissions_and_read_errors_preserve_existing_plan(self):
        self.prepare()
        before = self.path.read_bytes()
        for path, unsafe, safe in ((self.path, 0o644, 0o600), (self.path.parent, 0o755, 0o700),
                                   (self.data, 0o777, 0o755)):
            path.chmod(unsafe)
            with self.subTest(path=path.name), self.assertRaises(PoCheckpointError):
                self.prepare()
            path.chmod(safe)
            self.assertEqual(self.path.read_bytes(), before)
        original = os.open

        def denied(path, *args, **kwargs):
            if path == self.path:
                raise PermissionError("private-credential")
            return original(path, *args, **kwargs)

        with (
            mock.patch("os.open", side_effect=denied),
            self.assertRaisesRegex(PoCheckpointError, "storage failed \\(PermissionError\\)"),
        ):
            self.prepare()
        self.assertEqual(self.path.read_bytes(), before)

    def test_interruption_before_publication_has_no_accepted_plan_and_retry_succeeds(self):
        fsync = os.fsync

        def file_sync_interrupted(descriptor):
            if stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise OSError("file sync interrupted")
            fsync(descriptor)

        for operation, failure in (("os.replace", OSError("rename interrupted")),
                                   ("os.fsync", file_sync_interrupted)):
            with self.subTest(operation=operation):
                with (
                    mock.patch(operation, side_effect=failure),
                    self.assertRaisesRegex(PoCheckpointError, "storage failed"),
                ):
                    self.prepare()
                self.assertFalse(self.path.exists())
        plan = self.prepare()
        self.assertEqual(self.prepare(), plan)

    def test_interruption_after_rename_retains_published_ids_and_time(self):
        original = os.replace

        def interrupted(source, destination):
            original(source, destination)
            raise OSError("publication acknowledgement interrupted")

        with mock.patch("os.replace", side_effect=interrupted), self.assertRaises(PoCheckpointError):
            self.prepare()
        before = self.path.read_bytes()
        with mock.patch("uuid.uuid4", side_effect=AssertionError("regenerated published plan")):
            plan = self.prepare(moment=MOMENT + timedelta(days=1))
        self.assertEqual(plan.interrupted_at, "2026-10-01T00:00:00Z")
        self.assertEqual(self.path.read_bytes(), before)

    def test_directory_sync_failure_after_publication_retries_sync_without_replacement(self):
        from ummanu._fsutil import fsync_directory

        def interrupted(directory):
            if directory == self.path.parent:
                raise OSError("directory sync interrupted")
            fsync_directory(directory)

        with (
            mock.patch("ummanu.board.po_restore.fsync_directory", side_effect=interrupted),
            self.assertRaises(PoCheckpointError),
        ):
            self.prepare()
        before = self.path.read_bytes()
        with mock.patch("uuid.uuid4", side_effect=AssertionError("regenerated published plan")):
            retry = self.prepare(moment=MOMENT + timedelta(days=1))
        self.assertEqual(retry.interrupted_at, "2026-10-01T00:00:00Z")
        self.assertEqual(self.path.read_bytes(), before)
        with (
            mock.patch("ummanu.board.po_restore.fsync_directory", side_effect=interrupted),
            self.assertRaises(PoCheckpointError),
        ):
            self.prepare()
        self.assertEqual(self.path.read_bytes(), before)

    def test_concurrent_same_target_callers_accept_one_plan(self):
        barrier = threading.Barrier(4)

        def prepare(index):
            barrier.wait(timeout=2)
            return self.prepare(moment=MOMENT + timedelta(days=index))

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(prepare, index) for index in range(4)]
            plans = [future.result(timeout=2) for future in futures]
        self.assertTrue(all(plan == plans[0] for plan in plans))
        self.assertEqual(self.prepare(), plans[0])

    def test_invalid_source_and_naive_time_cannot_create_or_replace_a_plan(self):
        for exists in (False, True):
            if exists:
                self.prepare()
            for records, moment in ((None, MOMENT), (self.records, MOMENT.replace(tzinfo=None))):
                with self.subTest(exists=exists), self.assertRaises(PoCheckpointError):
                    prepare_po_restore(records, self.data, interrupted_at=moment)
            self.assertEqual(self.path.exists(), exists)

    def test_explicit_empty_capture_roundtrips_without_recovering_legacy_absence(self):
        records = {table: [] for table in self.records}
        plan = self.prepare(records=records)
        self.assertEqual(self.prepare(records=records), plan)
        self.assertEqual(normalized_po_records(records, plan), records)
