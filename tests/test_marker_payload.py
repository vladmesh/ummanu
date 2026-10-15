from __future__ import annotations

import unittest

from ummanu.board.host import MarkerComment
from ummanu.board.marker_payload import DecisionPayload, ReportPayload, VerdictPayload
from ummanu.board.models import Actor, EventKind


class MarkerPayloadTests(unittest.TestCase):
    def test_report_dictionary_normalizes_without_changing_wire_shape(self) -> None:
        data = {
            "marker": "report:blocked",
            "body": "dependency is unavailable",
            "status": "blocked",
            "classification": "external_fact",
            "body_sha256": "a" * 64,
        }
        operation = MarkerComment(
            "card:1",
            EventKind.CARD_REPORTED,
            Actor("worker", "worker-1"),
            data["body"],
            data,
        )
        self.assertIsInstance(operation.payload, ReportPayload)
        self.assertEqual(operation.data, data)

    def test_partial_report_dictionary_keeps_its_commit(self) -> None:
        data = {
            "marker": "report:partial",
            "body": "## Done\nparser\n## Not working\nwriter\n## Propose to extract\nwriter",
            "status": "partial",
            "classification": None,
            "partial_commit": "f" * 40,
        }
        operation = MarkerComment(
            "card:1",
            EventKind.CARD_REPORTED,
            Actor("worker", "worker-1"),
            data["body"],
            data,
        )
        self.assertIsInstance(operation.payload, ReportPayload)
        self.assertEqual(operation.payload.partial_commit, "f" * 40)
        self.assertEqual(operation.data, data)

    def test_typed_partial_report_projects_its_commit(self) -> None:
        payload = ReportPayload("partial", "progress", partial_commit="e" * 40)
        self.assertEqual(
            payload.to_event_data(),
            {"marker": "report:partial", "body": "progress", "status": "partial", "partial_commit": "e" * 40},
        )
        with self.assertRaisesRegex(ValueError, "no classification"):
            ReportPayload("partial", "progress", classification="external_fact")

    def test_verdict_dictionary_normalizes_without_changing_wire_shape(self) -> None:
        data = {
            "marker": "review:green",
            "body": "checks passed",
            "status": "green",
            "specification_revision": "specification-1",
        }
        operation = MarkerComment(
            "card:1",
            EventKind.CARD_VERDICTED,
            Actor("reviewer", "reviewer-1"),
            data["body"],
            data,
        )
        self.assertIsInstance(operation.payload, VerdictPayload)
        self.assertEqual(operation.data, data)

    def test_decision_dictionary_keeps_recovery_and_lineage_evidence(self) -> None:
        data = {
            "marker": "decision:rework",
            "body": "keep the repair local",
            "decision": "rework",
            "body_sha256": "a" * 64,
            "assessment_visit": "assessment-1",
            "description_sha256": "b" * 64,
            "specification_revision": "specification-1",
            "protocol_prerequisites": ["worker_local_broad_check_receipt"],
        }
        operation = MarkerComment(
            "card:1",
            EventKind.CARD_DECIDED,
            Actor("observer", "observer-1"),
            data["body"],
            data,
        )
        self.assertIsInstance(operation.payload, DecisionPayload)
        self.assertEqual(operation.data, data)

    def test_typed_payload_projects_the_released_dictionary(self) -> None:
        payload = DecisionPayload("accept", "ship it", ("worker_local_broad_check_receipt",))
        operation = MarkerComment(
            "card:1",
            EventKind.CARD_DECIDED,
            Actor("observer", "observer-1"),
            "ship it",
            payload,
        )
        self.assertEqual(
            operation.data,
            {
                "marker": "decision:accept",
                "body": "ship it",
                "decision": "accept",
                "protocol_prerequisites": ["worker_local_broad_check_receipt"],
            },
        )

    def test_kind_and_marker_must_agree(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported marker payload"):
            MarkerComment(
                "card:1",
                EventKind.CARD_VERDICTED,
                Actor("reviewer", "reviewer-1"),
                "checks passed",
                {"marker": "review:red", "body": "checks passed", "status": "green"},
            )


if __name__ == "__main__":
    unittest.main()
