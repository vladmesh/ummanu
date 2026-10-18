"""Portable PO state rejects invalid bindings without exposing conversation contents."""

import copy
import json
import unittest

from tests.po_checkpoint_fixtures import po_records
from ummanu.board.po_checkpoint import PoCheckpointError, scan_po_secrets, validate_po_records
from ummanu.runtime.redact import redact


class PoCheckpointTests(unittest.TestCase):
    def test_complete_states_empty_session_and_all_request_operations_are_valid(self):
        validate_po_records(po_records())
        validate_po_records({table: [] for table in po_records()})

    def test_invalid_types_references_and_state_invariants_name_only_record_and_field(self):
        cases = (
            ("po_sessions", 0, "closed_at", None), ("po_sessions", 1, "closed_by", "private"),
            ("po_sessions", 0, "cli", "unknown"), ("po_sessions", 0, "effort", None),
            ("po_turns", 0, "session_id", "private"), ("po_turns", 0, "seq", True),
            ("po_turns", 0, "finished_at", None), ("po_turns", 4, "finished_at", "private"),
            ("po_turns", 0, "state", "private"), ("po_turns", 0, "pid", "private"),
            ("po_feed", 0, "turn_seq", 88), ("po_feed", 0, "text", None),
            ("po_feed", 0, "metadata", []), ("po_feed", 0, "role", "private"),
            ("po_requests", 0, "operation", "private"), ("po_requests", 0, "seq", 1),
            ("po_requests", 1, "seq", None), ("po_requests", 1, "session_id", "private"),
            ("po_requests", 1, "seq", 88), ("po_requests", 1, "fingerprint", "private"),
            ("po_requests", 1, "created_at", "2026-01-01"),
        )
        for table, index, field, value in cases:
            with self.subTest(table=table, field=field):
                records = po_records()
                records[table][index][field] = value
                with self.assertRaises(PoCheckpointError) as failure:
                    validate_po_records(records)
                self.assertIn(f"{table} record {index}", str(failure.exception))
                self.assertNotIn("private", str(failure.exception))

    def test_missing_duplicate_conflicting_and_reordered_identities_are_refused(self):
        for table in po_records():
            for action in ("missing-field", "duplicate", "conflict", "reorder"):
                with self.subTest(table=table, action=action):
                    records = po_records()
                    rows = records[table]
                    if action == "missing-field":
                        rows[0].pop(next(iter(rows[0])))
                    elif action in {"duplicate", "conflict"}:
                        duplicate = copy.deepcopy(rows[0])
                        if action == "conflict":
                            duplicate["state" if "state" in duplicate else "session_id"] = "conflicting"
                        rows.insert(1, duplicate)
                    else:
                        rows.reverse()
                    with self.assertRaises(PoCheckpointError):
                        validate_po_records(records)

    def test_two_running_turns_and_incomplete_table_coverage_are_refused(self):
        records = po_records()
        records["po_turns"][1].update(state="running", finished_at=None)
        with self.assertRaisesRegex(PoCheckpointError, "one running turn"):
            validate_po_records(records)
        records.pop("po_requests")
        with self.assertRaisesRegex(PoCheckpointError, "table coverage"):
            validate_po_records(records)

    def test_secret_scan_covers_messages_metadata_and_host_bindings_without_printing_values(self):
        token = "sk-ant-api03-" + "A" * 40
        for table, field, value in (
            ("po_feed", "text", token), ("po_feed", "metadata", {"nested": [token]}),
            ("po_feed", "metadata", {"nested": ["literal\nsecret"]}),
            ("po_sessions", "cli_session_id", token), ("po_turns", "reason", token),
            ("po_requests", "fingerprint", "a" * 64),
        ):
            with self.subTest(table=table, field=field):
                records = po_records()
                records[table][0][field] = value
                with self.assertRaises(PoCheckpointError) as failure:
                    scan_po_secrets(json.dumps(records), lambda text: redact(text, secret_values=("literal\nsecret", "a" * 64)))
                self.assertEqual(str(failure.exception), f"secret detected in PO SQL {table} record 0 field {field}")

    def test_nonsecret_text_and_metadata_pass_secret_scan_unchanged(self):
        records = po_records()
        before = copy.deepcopy(records)
        scan_po_secrets(json.dumps(records), redact)
        self.assertEqual(records, before)
