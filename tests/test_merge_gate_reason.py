from __future__ import annotations

import unittest
from unittest import mock

from ummanu.dispatch import attempt_accounting, gate_lifecycle, release_lifecycle, review_verdict
from ummanu.dispatch.gate import GateResult
from ummanu.dispatch.gate_receipt import mint_gate_receipt
from ummanu.dispatch.state import DispatcherRecord
from ummanu.dispatch.types import GateTransportError, HostError


class MergeGateReasonTests(unittest.TestCase):
    def setUp(self):
        self.task = {"ref": "ummanu-252", "type": "code"}
        self.record = DispatcherRecord(
            worker="worker", workspace="/fixture", handle="worker", head="worker",
            review_head="reviewer", attempt_id="attempt-fixture", comment_baseline=0,
            review_baseline=0, state="reviewing", claimed_at=1,
        )
        self.records = {self.task["ref"]: self.record}
        self.runtime = mock.Mock()
        self.runtime.host.gate_check.side_effect = HostError("gate backend failed")
        self.terminal = self.enterContext(mock.patch.object(attempt_accounting, "terminal_effect"))
        self.events = []
        self.runtime.host.stop.side_effect = lambda *_: self.events.append("stop")
        self.terminal.side_effect = lambda *_args, **_kwargs: self.events.append("terminal")
        self.runtime.save_records.side_effect = lambda *_: self.events.append(tuple(self.records))

    def read_release(self):
        return release_lifecycle.read_release_gate(
            self.runtime, self.task, self.record, self.records, {}, "tick-attempt"
        )

    def test_same_host_error_is_gate_on_review_and_release_with_unchanged_public_identity(self):
        for consumer, step, action, outcome in (
            (review_verdict.read_merge_gate, "review", "merge-gate-blocked", "merge gate failed"),
            (release_lifecycle.read_release_gate, "assessment", "release-failed-blocked", "release failed"),
        ):
            with self.subTest(step=step):
                self.records[self.task["ref"]] = self.record
                result = consumer(self.runtime, self.task, self.record, self.records, {}, "tick-attempt")
                self.assertEqual(result, ({
                    "status": "blocked", "step": step, "pilot_ref": self.task["ref"], "reason": outcome,
                }, None))
                terminal = self.terminal.call_args.kwargs
                self.assertEqual(terminal["request_id"], f"dispatcher-attempt-fixture-{action}-ummanu-252")
                self.assertEqual(terminal["blocked_reason"], "gate")
                self.assertNotIn(self.task["ref"], self.records)
                self.assertEqual(self.events[-3:], ["stop", "terminal", ()])
        self.runtime.host.complete_green.assert_not_called()

    def test_release_red_is_gate_and_drift_remains_implementation(self):
        for kind, cause in (("red", "gate"), ("drift", "implementation")):
            with self.subTest(kind=kind), mock.patch.object(
                release_lifecycle, "merge_readiness", return_value=(kind, None, "changed checkout")
            ):
                self.read_release()
                self.assertEqual(self.terminal.call_args.kwargs["blocked_reason"], cause)
                self.assertEqual(self.terminal.call_args.kwargs["request_id"],
                                 f"dispatcher-attempt-fixture-release-{kind}-blocked-ummanu-252")

    def test_missing_result_is_gate_for_each_consumer_and_state(self):
        for consumer in (review_verdict.read_merge_gate, release_lifecycle.read_release_gate):
            for kind in ("pending", "red", "green"):
                with self.subTest(consumer=consumer.__name__, kind=kind), mock.patch.object(
                    release_lifecycle, "merge_readiness", return_value=(kind, None, "")
                ):
                    consumer(self.runtime, self.task, self.record, self.records, {}, "tick-attempt")
                    self.assertEqual(self.terminal.call_args.kwargs["blocked_reason"], "gate")

    def test_transport_retries_until_existing_ceiling_without_merge_or_early_terminal(self):
        self.runtime.host.gate_check.side_effect = GateTransportError("backend unreachable")
        for count in range(1, gate_lifecycle.GATE_TRANSPORT_MAX_ATTEMPTS):
            outcome, result = self.read_release()
            self.assertIsNone(result)
            self.assertEqual(outcome["action"], "gate-transport-retry")
            self.assertEqual(outcome["attempts"], count)
            self.terminal.assert_not_called()
            self.runtime.host.stop.assert_not_called()
            self.assertIn(self.task["ref"], self.records)
        outcome, _ = self.read_release()
        self.assertEqual(outcome["reason"], "gate transport unavailable")
        self.assertEqual(self.terminal.call_args.kwargs["blocked_reason"], "gate")
        self.runtime.host.complete_green.assert_not_called()

    def test_pending_waits_for_existing_ceiling(self):
        self.runtime.host.gate_check.side_effect = None
        self.runtime.host.gate_check.return_value = GateResult("pending", "CI running")
        with mock.patch.object(gate_lifecycle.time, "time", return_value=100.0):
            self.assertEqual(self.read_release()[0]["action"], "merge-gate-pending")
        with (
            mock.patch.object(gate_lifecycle, "_worker_vitality_for_gate", return_value=None),
            mock.patch.object(gate_lifecycle.time, "time", return_value=100.0 + gate_lifecycle.GATE_PENDING_STALL_SECONDS),
        ):
            self.assertEqual(self.read_release()[0]["action"], "merge-gate-pending")
            self.terminal.assert_not_called()
        with (
            mock.patch.object(gate_lifecycle, "_worker_vitality_for_gate", return_value=None),
            mock.patch.object(gate_lifecycle.time, "time", return_value=101.0 + gate_lifecycle.GATE_PENDING_STALL_SECONDS),
        ):
            self.assertEqual(self.read_release()[0]["to"], "blocked")
        self.assertEqual(self.terminal.call_args.kwargs["blocked_reason"], "gate")

    def test_receipt_and_topology_failures_are_gate(self):
        self.runtime.host.mode = "real"
        self.runtime.host.head_commit.return_value = "a" * 40
        receipt = mint_gate_receipt(
            validated_sha="b" * 40, base_sha="c" * 40, gate_mode="github",
            required_checks=[{"name": "test", "conclusion": "SUCCESS", "url": ""}],
            check_set_identity="fixture",
        )
        with mock.patch.object(gate_lifecycle, "_validation_ci", return_value="github"):
            for value in (None, {"invalid": True}, receipt):
                with self.subTest(receipt=value):
                    gate_lifecycle.accept_green_gate(
                        self.runtime, self.task, self.record, self.records, {}, "tick-attempt",
                        GateResult("green", "green", attestation=value), stage="release",
                    )
                    self.assertEqual(self.terminal.call_args.kwargs["blocked_reason"], "gate")
        gate_lifecycle.gate_red_to_worker(
            self.runtime, self.task, self.record, self.records, {}, "tick-attempt",
            GateResult("red", "CI unreachable", failure_class="topology"), phase="review-freeze",
        )
        self.assertEqual(self.terminal.call_args.kwargs["blocked_reason"], "gate")

    def test_boundary_requires_cause_and_ignores_action_wording(self):
        kwargs = {"action": "gate-label", "reason": "cause", "step": "review", "outcome": "failed"}
        with self.assertRaises(TypeError):
            release_lifecycle.block_merge_path(
                self.runtime, self.task, self.record, self.records, {}, "tick-attempt", **kwargs
            )
        self.runtime.host.stop.assert_not_called()
        for label, cause in (("unrelated-label", "gate"), ("gate-label", "implementation")):
            release_lifecycle.block_merge_path(
                self.runtime, self.task, self.record, self.records, {}, "tick-attempt",
                **{**kwargs, "action": label}, blocked_reason=cause,
            )
            self.assertEqual(self.terminal.call_args.kwargs["blocked_reason"], cause)
