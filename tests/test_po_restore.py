"""Frozen PO normalization and full parity diagnostics expose no transported values."""

import copy
import unittest
from dataclasses import FrozenInstanceError, asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from tests.po_checkpoint_fixtures import po_records
from ummanu.board.po_checkpoint import PoCheckpointError
from ummanu.board.po_restore import (
    INTERRUPTION_REASON,
    PoRestorePlan,
    PoRestoreStore,
    normalized_po_records,
    plan_po_restore,
    require_po_parity,
)
from ummanu.po.runner import PoRunner

RESTORED = datetime(2026, 10, 1, tzinfo=UTC)


class PoRestoreTests(unittest.TestCase):
    def setUp(self):
        self.records = po_records()
        self.records["po_sessions"][1]["cli"] = "claude"
        self.plan = plan_po_restore(self.records, Path("/target/data"), interrupted_at=RESTORED)

    def test_frozen_plan_round_trip_retries_preserve_exact_normalized_content(self):
        source = copy.deepcopy(self.records)
        saved = asdict(self.plan)
        saved["native_bindings"] = tuple(tuple(binding) for binding in saved["native_bindings"])
        reloaded = PoRestorePlan(**saved)
        with mock.patch("uuid.uuid4", side_effect=AssertionError("retry generated an id")):
            first = normalized_po_records(self.records, reloaded)
            require_po_parity(first, normalized_po_records(self.records, reloaded))
        with self.assertRaises(FrozenInstanceError):
            self.plan.data_dir = "/different"
        self.assertEqual(self.records, source)

    def test_only_declared_binding_and_running_state_fields_change(self):
        expected = copy.deepcopy(self.records)
        for row in expected["po_sessions"]:
            row["cwd"] = "/target/data/po"
            row["cli_session_id"] = dict(self.plan.native_bindings)[row["session_id"]]
        for row in expected["po_turns"]:
            runner = PoRunner(mock.Mock(), Path("/target/data"), head_specs={}, env={})
            row["stdout_path"] = str(runner.files(row["session_id"], row["seq"]).stdout)
            row["pid"] = row["process_identity"] = None
            if row["state"] == "running":
                row.update(state="interrupted", finished_at="2026-10-01T00:00:00Z", reason=INTERRUPTION_REASON)
        actual = normalized_po_records(self.records, self.plan)
        self.assertEqual(actual, expected)
        self.assertIsNone(actual["po_sessions"][0]["cli_session_id"])
        self.assertNotEqual(actual["po_sessions"][1]["cli_session_id"], "native-provider-context")

    def test_running_reason_is_retained_with_explicit_uncertainty(self):
        self.records["po_turns"][-1]["reason"] = "prior rerun reason"
        plan = plan_po_restore(self.records, Path("/target"), interrupted_at=RESTORED)
        self.assertEqual(normalized_po_records(self.records, plan)["po_turns"][-1]["reason"],
                         "prior rerun reason\n" + INTERRUPTION_REASON)

    def test_changed_source_or_invalid_plan_is_refused(self):
        for plan in (
            replace(self.plan, source_sha256="wrong"), replace(self.plan, data_dir="relative"),
            replace(self.plan, data_dir="/target/../data"), replace(self.plan, interrupted_at="invalid"),
            replace(self.plan, interrupted_at="2020-01-01T00:00:00Z"),
            replace(self.plan, native_bindings=()),
            replace(self.plan, native_bindings=(("closed", "old"),) + self.plan.native_bindings[1:]),
            replace(self.plan, native_bindings=(self.plan.native_bindings[0], ("empty", "invalid"),
                                               self.plan.native_bindings[2])),
        ):
            with self.subTest(plan=plan), self.assertRaises(PoCheckpointError):
                normalized_po_records(self.records, plan)
        changed = copy.deepcopy(self.records)
        changed["po_feed"][0]["text"] += "changed"
        with self.assertRaises(PoCheckpointError):
            normalized_po_records(changed, self.plan)

    def test_session_paths_cannot_escape_target(self):
        for identity in ("/absolute", "../escape", ".", ".."):
            records = {table: [] for table in self.records}
            row = copy.deepcopy(self.records["po_sessions"][1])
            row["session_id"] = identity
            records["po_sessions"] = [row]
            with self.subTest(identity=identity), self.assertRaises(PoCheckpointError):
                plan_po_restore(records, Path("/target"), interrupted_at=RESTORED)

    def test_explicit_empty_capture_and_aware_time_are_required(self):
        records = {table: [] for table in self.records}
        plan = plan_po_restore(records, Path("/target"), interrupted_at=RESTORED)
        self.assertEqual(normalized_po_records(records, plan), records)
        with self.assertRaisesRegex(PoCheckpointError, "aware interruption time"):
            plan_po_restore(records, Path("/target"), interrupted_at=RESTORED.replace(tzinfo=None))

    def test_each_field_mismatch_names_safe_identity_and_redacts_both_values(self):
        expected = normalized_po_records(self.records, self.plan)
        for table, rows in expected.items():
            for field in rows[0]:
                actual = copy.deepcopy(expected)
                actual[table][0][field] = "secret-planted-value"
                with self.subTest(table=table, field=field), self.assertRaises(PoCheckpointError) as failed:
                    require_po_parity(expected, actual)
                diagnostic = str(failed.exception)
                self.assertIn(table, diagnostic)
                self.assertIn(f"field {field}:", diagnostic)
                self.assertIn("expected=", diagnostic)
                self.assertIn("actual=", diagnostic)
                for secret in ("secret-planted-value", "native-provider-context", "repeated text",
                               self.records["po_requests"][0]["fingerprint"], "/target"):
                    self.assertNotIn(secret, diagnostic)

    def test_extra_missing_reordered_rows_and_metadata_types_fail_parity(self):
        expected = normalized_po_records(self.records, self.plan)
        for table in expected:
            for change in ("extra", "missing", "order", "fields"):
                actual = copy.deepcopy(expected)
                if change == "extra":
                    actual[table].append(copy.deepcopy(actual[table][0]))
                elif change == "missing":
                    actual[table].pop()
                elif change == "order":
                    actual[table].reverse()
                else:
                    actual[table][0]["unknown"] = None
                with self.subTest(table=table, change=change), self.assertRaises(PoCheckpointError):
                    require_po_parity(expected, actual)
        actual = copy.deepcopy(expected)
        actual["po_feed"][0]["metadata"]["nested"][0] = 1
        with self.assertRaises(PoCheckpointError):
            require_po_parity(expected, actual)

    def test_missing_outer_transaction_refuses_before_any_sql(self):
        client = mock.Mock(_depth=0)
        with self.assertRaisesRegex(PoCheckpointError, "caller-owned transaction"):
            PoRestoreStore(client).restore(self.records, self.plan)
        client._execute.assert_not_called()
        client._query.assert_not_called()
