"""Durability and replay contracts for the package-owned worker report flow."""

from __future__ import annotations

import unittest
from unittest import mock

from ummanu.dispatch import worker_report as dispatcher_worker_report
from ummanu.dispatch.state import DispatcherRecord, OutcomeTerminalPath


class WorkerReportBoundaryTests(unittest.TestCase):
    """The package-owned report flow keeps its durable ordering and crash replay contract."""

    def setUp(self) -> None:
        self.task = {"ref": "sample-1", "type": "code", "comments": []}
        self.record = DispatcherRecord(
            worker="worker-1",
            workspace="/unused",
            handle="",
            head="codex",
            review_head="claude",
            attempt_id="held-attempt",
            comment_baseline=0,
            review_baseline=0,
            state="claimed",
            claimed_at=1.0,
            report_generation=3,
        )
        self.records = {"sample-1": self.record}
        self.payload = {}
        self.runtime = mock.Mock()
        self.runtime.owner = "dispatcher"
        self.runtime._stop_worker_confirmed.return_value = None
        self.runtime.host.worker_addressable.return_value = True
        self.runtime.host.head_commit.return_value = "new-candidate"
        self.accounting = mock.Mock()
        self.accounting_patcher = mock.patch.object(
            dispatcher_worker_report, "attempt_accounting", self.accounting
        )
        self.accounting_patcher.start()
        self.addCleanup(self.accounting_patcher.stop)

    def handle(self, marker):
        return dispatcher_worker_report.handle_worker_report(
            self.runtime, self.task, self.record, self.records, self.payload, "tick-attempt", marker=marker
        )

    def prompt(self):
        return dispatcher_worker_report.prompt_worker_report(
            self.runtime, self.task, self.record, self.records, self.payload, "tick-attempt", trigger="idle"
        )

    def test_no_report_leaves_wait_and_liveness_to_the_runtime(self) -> None:
        self.assertIsNone(self.handle(None))
        self.assertEqual(self.runtime.mock_calls, [])
        self.assertEqual(self.record.state, "claimed")

    def test_terminal_path_is_saved_before_a_failing_source_handoff(self) -> None:
        for marker in ("report:done", "report:blocked"):
            with self.subTest(marker=marker):
                events = []
                self.record.outcome_terminal_path = OutcomeTerminalPath.NO_ACCEPTED_REPORT
                self.runtime.save_records.side_effect = lambda *_, events=events: events.append(
                    ("save", self.record.outcome_terminal_path)
                )

                def fail_source(*args, events=events, **kwargs):
                    events.append(("source", self.record.outcome_terminal_path))
                    raise RuntimeError("source unavailable")

                self.accounting.capture_outcome_source.side_effect = fail_source
                with (
                    mock.patch.object(
                        dispatcher_worker_report, "_round_report_ids", return_value={"report-id"}
                    ),
                    mock.patch.object(dispatcher_worker_report, "_round_report_marker", return_value=marker),
                    self.assertRaisesRegex(RuntimeError, "source unavailable"),
                ):
                    dispatcher_worker_report.worker_report_marker(
                        self.runtime, self.task, self.record, self.records, self.payload, "tick-attempt"
                    )
                self.assertEqual(
                    events,
                    [
                        ("save", OutcomeTerminalPath.FOLLOWS_ACCEPTED_REPORT),
                        ("source", OutcomeTerminalPath.FOLLOWS_ACCEPTED_REPORT),
                    ],
                )
                self.assertIs(self.records["sample-1"], self.record)

    def test_no_terminal_marker_does_not_freeze_an_outcome_path(self) -> None:
        with (
            mock.patch.object(dispatcher_worker_report, "_round_report_ids", return_value={"report-id"}),
            mock.patch.object(dispatcher_worker_report, "_round_report_marker", return_value=None),
        ):
            marker = dispatcher_worker_report.worker_report_marker(
                self.runtime, self.task, self.record, self.records, self.payload, "tick-attempt"
            )
        self.assertIsNone(marker)
        self.assertEqual(self.record.outcome_terminal_path, OutcomeTerminalPath.NO_ACCEPTED_REPORT)
        self.runtime.save_records.assert_not_called()
        self.accounting.capture_outcome_source.assert_not_called()

    def test_done_replay_saves_retention_before_move_and_never_refreezes(self) -> None:
        saved_retention = []
        self.runtime.save_records.side_effect = lambda *_: saved_retention.append(
            self.record.worker_continuation.validation_move_pending
        )
        self.runtime.writer.move.side_effect = [RuntimeError("interrupted move"), None]
        with (
            mock.patch.object(dispatcher_worker_report, "has_candidate", return_value=True),
            self.assertRaisesRegex(RuntimeError, "interrupted move"),
        ):
            self.handle("report:done")
        self.assertEqual(saved_retention, [True])
        self.assertTrue(self.record.worker_continuation.validation_move_pending)
        first_request = self.runtime.writer.move.call_args.kwargs["request_id"]
        outcome = self.handle("report:done")
        self.assertEqual(outcome["to"], "validate")
        self.assertEqual(self.record.state, "validate")
        self.assertEqual(self.record.report_generation, 3)
        self.assertEqual(self.runtime.writer.move.call_args.kwargs["request_id"], first_request)
        self.assertEqual(self.runtime.host.verify_worker_result.call_count, 2)
        self.runtime.host.retain_worker.assert_called_once()
        self.assertEqual(self.accounting.record_attempt_usage.call_count, 2)

    def test_pending_validate_recovery_refuses_lost_completion_evidence(self):
        from ummanu.dispatch.types import HostError

        self.record.worker_continuation.begin_retention(1.0)
        self.runtime.host.verify_worker_result.side_effect = HostError("worker receipt missing")
        outcome = self.handle("report:done")
        self.assertEqual(outcome["status"], "blocked")
        self.runtime.writer.move.assert_not_called()
        self.runtime.host.retain_worker.assert_not_called()
        self.accounting.terminal_effect.assert_called_once()

    def test_unconfirmed_stop_refuses_report_terminal_effects(self) -> None:
        refused = {"status": "degraded", "action": "head-stop-unconfirmed"}
        self.runtime._stop_worker_confirmed.return_value = refused
        self.assertIs(self.handle("report:blocked"), refused)
        self.accounting.record_attempt_usage.assert_called_once()
        self.accounting.terminal_effect.assert_not_called()
        self.runtime.writer.move.assert_not_called()
        self.assertIs(self.records["sample-1"], self.record)

    def test_nudge_is_saved_before_delivery_and_spent_before_a_failed_comment(self) -> None:
        events = []
        self.runtime.save_records.side_effect = lambda *_, events=events: events.append(
            ("save", self.record.worker_report_nudge.stage.value)
        )
        self.runtime.host.prompt_worker_report.side_effect = lambda *_: events.append(
            ("send", self.record.worker_report_nudge.stage.value)
        )

        def fail_comment(**kwargs):
            events.append(("comment", self.record.worker_report_nudge.stage.value))
            raise RuntimeError("comment unavailable")

        self.runtime.writer.comment.side_effect = fail_comment
        with self.assertRaisesRegex(RuntimeError, "comment unavailable"):
            self.prompt()
        self.assertEqual(
            events,
            [
                ("save", "pending"),
                ("send", "pending"),
                ("save", "delivered"),
                ("comment", "delivered"),
            ],
        )
        self.assertEqual(self.prompt(), (None, "idle"))
        self.runtime.host.prompt_worker_report.assert_called_once()

    def test_failed_nudge_intent_write_does_not_send(self) -> None:
        self.runtime.save_records.side_effect = RuntimeError("state unavailable")
        with self.assertRaisesRegex(RuntimeError, "state unavailable"):
            self.prompt()
        self.runtime.host.prompt_worker_report.assert_not_called()
        self.runtime.writer.comment.assert_not_called()


if __name__ == "__main__":
    unittest.main()
