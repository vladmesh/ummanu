"""Validation and parity of portable obligations without a database or recovery filesystem."""

import copy
import unittest

from tests.origin_checkpoint_fixtures import obligations
from ummanu.board.origin_checkpoint import (
    OriginCheckpointError,
    require_outbox_parity,
    validate_outbox_records,
)


class OriginCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.rows, self.history = obligations()
        self.cards = [{"reference": "ummanu-1", "closed": True, "column": "Ready", "metadata": {}}]

    def test_mixed_deliveries_keep_distinct_audit_and_po_requests_on_an_archived_card(self):
        validate_outbox_records(self.rows, self.cards, self.history)
        relocated = [dict(row, id=index + 20) for index, row in enumerate(self.rows)]
        require_outbox_parity(self.rows, relocated)

    def test_invalid_delivery_identity_order_and_audit_links_name_the_field(self):
        for field, value, diagnostic in (
            ("id", 0, "id/order"), ("event_id", "missing", "audit.event_id"),
            ("task_ref", "unknown-card", "task_ref"), ("request_id", "po-request", "audit.request_id"),
            ("target_state", "done", "audit.target_state"), ("created_at", "bad", "created_at"),
            ("status", "delivered", "delivery"), ("session", "pending-session", "delivery"),
            ("po_request_id", [], "po_request_id"),
        ):
            with self.subTest(field=field):
                rows = copy.deepcopy(self.rows)
                rows[0][field] = value
                with self.assertRaisesRegex(OriginCheckpointError, diagnostic):
                    validate_outbox_records(rows, self.cards, self.history)

    def test_duplicate_events_and_completed_delivery_without_status_are_refused(self):
        for field, value in (("event_id", self.rows[0]["event_id"]), ("status", None)):
            with self.subTest(field=field):
                rows = copy.deepcopy(self.rows)
                rows[1][field] = value
                with self.assertRaises(OriginCheckpointError):
                    validate_outbox_records(rows, self.cards, self.history)

    def test_duplicate_audit_request_is_refused_with_event_card_and_field(self):
        self.history[1]["request_id"] = self.history[0]["request_id"]
        self.rows[1]["request_id"] = self.history[0]["request_id"]
        with self.assertRaisesRegex(OriginCheckpointError, "transition-1.*ummanu-1.*audit.request_id"):
            validate_outbox_records(self.rows, self.cards, self.history)

    def test_parity_refuses_every_changed_delivery_field_and_does_not_expose_values(self):
        for field in self.rows[1]:
            if field == "id":
                continue
            with self.subTest(field=field):
                changed = copy.deepcopy(self.rows)
                changed[1][field] = "private-value\nsecret"
                with self.assertRaises(OriginCheckpointError) as failure:
                    require_outbox_parity(self.rows, changed)
                self.assertIn("transition-1", str(failure.exception))
                self.assertNotIn("private-value", str(failure.exception))

    def test_parity_refuses_missing_extra_and_reordered_obligations(self):
        for changed in (self.rows[:2], self.rows + [dict(self.rows[0], event_id="extra")], self.rows[::-1]):
            with self.subTest(changed=changed), self.assertRaises(OriginCheckpointError):
                require_outbox_parity(self.rows, changed)
