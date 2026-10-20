"""Hermetic verdict intake and dispatcher routing at the public verdict seams."""

from __future__ import annotations

import tempfile
import unittest
from unittest import mock

from ummanu.dispatch import attempt_accounting, worker_continuation
from ummanu.dispatch.review_verdict import advance_review_verdict
from ummanu.dispatch.state import DispatcherRecord
from ummanu.tasks import TaskError, TaskWriter


class RedVerdictIntakeTests(unittest.TestCase):
    def setUp(self):
        self.writer = object.__new__(TaskWriter)
        self.writer._role = lambda role, *args, **kwargs: role
        self.writer._redact_for_board = lambda body: body
        self.writer.reader = mock.Mock()
        self.writer.reader.show.return_value = {"description": "Repair the behavior"}
        self.writer.audit = mock.Mock()
        self.writer.audit.events.return_value = []
        self.writer._marker_write = mock.Mock(return_value={"action": "verdict"})

    def verdict(self, body, kind="red"):
        return self.writer.verdict(
            role="reviewer", actor="reviewer", reference="sample-1", kind=kind, body=body
        )

    def test_red_refuses_missing_empty_duplicate_and_malformed_open_sets(self):
        for body in (
            "",
            "   ",
            "BLOCKER-a: still broken",
            "Open blockers:",
            "Open blockers: BLOCKER-a\nOpen blockers: BLOCKER-b",
            "Open blockers: BLOCKER-a\nOpen blockers:",
            "Open blockers: BLOCKER-a, other",
            "Open blockers: `BLOCKER-a`",
        ):
            with self.subTest(body=body), self.assertRaises(TaskError) as raised:
                self.verdict(body)
            self.assertEqual((raised.exception.code, raised.exception.exit_code), ("validation", 2))
            self.assertIn("Open blockers: BLOCKER-a, BLOCKER-b", str(raised.exception))
        self.writer._marker_write.assert_not_called()

    def test_valid_red_preserves_body_and_green_needs_no_open_set(self):
        for kind, body in (
            ("red", "BLOCKER-closed: closed\nOpen blockers: BLOCKER-a, BLOCKER-b\nDetails"),
            ("green", "All required behavior is verified."),
        ):
            with self.subTest(kind=kind):
                self.assertEqual(self.verdict(body, kind), {"action": "verdict"})
                data = self.writer._marker_write.call_args.kwargs["data"]
                self.assertEqual((data["marker"], data["body"]), (f"review:{kind}", body))


class RejectedCandidateMoveTests(unittest.TestCase):
    def test_only_the_dispatcher_moves_a_card_with_a_rejected_candidate(self):
        board = mock.MagicMock(name="board")
        data_dir = self.enterContext(tempfile.TemporaryDirectory())
        writer = TaskWriter(board, data_dir=data_dir)
        writer.reader = mock.MagicMock()
        before = list(board.mock_calls)
        for role, actor in (("po", "operator"), ("steward", "steward")):
            with self.subTest(role=role), self.assertRaises(TaskError) as raised:
                writer.move(
                    role=role,
                    actor=actor,
                    reference="sample-1",
                    target="in_progress",
                    reason="moved by hand",
                    rejected_candidate={"sha": "a" * 40, "source": "gate"},
                )
            self.assertEqual((raised.exception.code, raised.exception.exit_code), ("role_forbidden", 3))
        writer.reader.show.assert_not_called()
        self.assertEqual(board.mock_calls, before)


class RedVerdictRoutingTests(unittest.TestCase):
    def setUp(self):
        self.task = {"ref": "sample-1", "state": "validate", "comments": []}
        self.record = DispatcherRecord(
            worker="worker", workspace="/candidate", handle="worker-pane", head="codex",
            review_head="reviewer", attempt_id="attempt", comment_baseline=0,
            review_baseline=0, state="validate", claimed_at=1.0, report_generation=1,
        )
        self.records = {"sample-1": self.record}
        self.events = []
        self.terminal_effects = []
        self.runtime = mock.Mock()
        self.runtime.owner = "dispatcher"
        self.runtime.reader.show.return_value = self.task
        self.runtime.audit.events.side_effect = lambda reference: list(self.events)
        self.runtime.host.head_commit.return_value = "a" * 40
        self.runtime._end_review_pane_confirmed.return_value = None
        self.runtime.writer.move.side_effect = self.move
        self.enterContext(mock.patch.object(attempt_accounting, "capture_outcome_source"))
        self.enterContext(mock.patch.object(attempt_accounting, "record_attempt_usage"))
        self.enterContext(mock.patch.object(
            attempt_accounting, "terminal_effect", side_effect=self.terminal_move
        ))
        self.delivery = self.enterContext(mock.patch.object(
            worker_continuation, "_deliver_red_continuation", return_value={"action": "delivered"}
        ))

    def move(self, *, target, reason, **kwargs):
        self.task["state"] = target
        self.task["comments"].append({"marker": None, "body": reason})

    def terminal_move(self, runtime, task, record, **kwargs):
        self.terminal_effects.append(kwargs)
        self.move(target=kwargs["target"], reason=kwargs["reason"])

    def verdict(self, body, kind="red"):
        self.task["comments"].append({"marker": f"review:{kind}", "body": f"[review:{kind}]\n{body}"})
        self.events.append({
            "record_type": "board.protocol_event",
            "event_id": f"evt_verdict_{len(self.events)}",
            "data": {"marker": f"review:{kind}", "body": body},
        })

    def advance(self):
        return advance_review_verdict(
            self.runtime, self.task, self.record, self.records, {}, "attempt"
        )

    def test_first_red_opens_round_then_repeated_red_blocks_without_delivery(self):
        self.verdict("Open blockers: BLOCKER-b, BLOCKER-a")
        self.assertEqual(self.advance()["action"], "delivered")
        self.assertEqual(self.task["state"], "in_progress")
        self.assertEqual(self.record.report_generation, 2)
        self.delivery.assert_called_once()

        self.record.worker_continuation.clear()
        self.task["state"] = "validate"
        self.verdict("Open blockers: BLOCKER-a, BLOCKER-new, BLOCKER-b")
        outcome = self.advance()
        self.assertEqual(outcome["status"], "blocked")
        self.assertEqual(self.task["state"], "blocked")
        self.assertEqual(self.delivery.call_count, 1)
        self.assertEqual(self.record.report_generation, 2)
        self.runtime.host.stop.assert_called_once_with(self.record)
        self.assertEqual(self.record.workspace, "/candidate")
        self.assertEqual(self.records, {})
        reason = self.task["comments"][-1]["body"]
        self.assertIn("Repeated open blockers: BLOCKER-a, BLOCKER-b", reason)
        self.assertIn("PO that cut the card decides between the card's goal and its wording", reason)

    def test_new_open_ids_ignore_closed_ids_in_prose_and_open_a_round(self):
        self.verdict("Open blockers: BLOCKER-old")
        self.record.review_baseline = len(self.task["comments"])
        self.verdict("BLOCKER-old: closed\n`BLOCKER-old` is repaired\nOpen blockers: BLOCKER-new")
        self.assertEqual(self.advance()["action"], "delivered")
        self.assertEqual(self.task["state"], "in_progress")
        self.delivery.assert_called_once()

    def test_green_predecessor_breaks_repeat_comparison(self):
        self.verdict("Open blockers: BLOCKER-a")
        self.verdict("Resolved", kind="green")
        self.record.review_baseline = len(self.task["comments"])
        self.verdict("Open blockers: BLOCKER-a")
        self.assertEqual(self.advance()["action"], "delivered")
        self.assertEqual(self.task["state"], "in_progress")

    def test_legacy_red_prose_does_not_supply_an_open_set(self):
        self.verdict("BLOCKER-a: still broken")
        self.record.review_baseline = len(self.task["comments"])
        self.verdict("Open blockers: BLOCKER-a")
        self.assertEqual(self.advance()["action"], "delivered")
        self.assertEqual(self.task["state"], "in_progress")

    def test_observer_repeat_parks_with_ids_and_keeps_reviewed_commit(self):
        self.task["sprint"] = "sprint:sample"
        self.runtime.sprints.show.return_value = {
            "status": "open", "observer": {"kind": "head", "profile": "observer"}
        }
        self.verdict("Open blockers: BLOCKER-a")
        self.record.review_baseline = len(self.task["comments"])
        self.verdict("Open blockers: BLOCKER-a, BLOCKER-new")
        self.assertEqual(self.advance()["to"], "assessment")
        self.assertEqual(self.task["state"], "assessment")
        self.assertIn("Repeated open blockers: BLOCKER-a", self.task["comments"][-1]["body"])
        self.assertEqual(self.record.review_commit, "a" * 40)
        self.delivery.assert_not_called()
        self.runtime.host.stop.assert_not_called()

    def test_distinct_reds_still_stop_at_the_no_observer_ceiling(self):
        for blocker in ("first", "second", "third"):
            self.record.worker_continuation.clear()
            self.task["state"] = "validate"
            self.verdict(f"Open blockers: BLOCKER-{blocker}")
            outcome = self.advance()
        self.assertEqual(outcome["reason"], "red review ceiling reached")
        self.assertEqual(self.task["state"], "blocked")
        self.assertEqual(self.delivery.call_count, 2)

    def test_red_review_ledgers_the_reviewed_candidate_and_its_verdict(self):
        self.events.append({
            "record_type": "board.protocol_event",
            "event_id": "evt_report",
            "data": {
                "marker": "report:done",
                "worker_check": {"candidate_sha": "a" * 40, "tree_sha": "b" * 40},
            },
        })
        self.verdict("Open blockers: BLOCKER-a")
        self.advance()
        self.assertEqual(
            self.terminal_effects[-1]["rejected_candidate"],
            {"sha": "a" * 40, "tree": "b" * 40, "source": "review", "verdict_event_id": "evt_verdict_1"},
        )
