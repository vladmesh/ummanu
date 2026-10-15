"""Freeze role isolation and durable observer work-state transitions (BUG-17/19)."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from ummanu import status
from ummanu.dispatch import observer, pause_ops
from ummanu.dispatch.launch import REVIEW_ROLE, WORKER_ROLE
from ummanu.dispatch.types import STOPPED_BY_OPERATOR, HostError


class FreezeRoleIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.record = SimpleNamespace(
            workspace="/tmp/ummanu-freeze-test",
            review_handle="review-pane",
            review_leaf="",
            review_pid_file="",
            handle="worker-pane",
            worker_leaf="",
            worker_pid_file="",
            paused_reviewer_at=0.0,
            paused_worker_at=0.0,
        )
        self.host = SimpleNamespace(stop_head=mock.Mock())
        self.runtime = SimpleNamespace(host=self.host)

    def freeze(self) -> tuple[list[str], list[str], list[str]]:
        return pause_ops._freeze_heads(self.runtime, {"card-1": self.record}, set())

    def test_reviewer_stop_failure_does_not_skip_worker_stop(self) -> None:
        with (
            mock.patch.object(pause_ops, "launch_intent", return_value=None),
            mock.patch.object(pause_ops, "end_review_pane", side_effect=HostError("reviewer busy")),
            mock.patch.object(pause_ops, "forget_role_head") as forget,
        ):
            worker, reviewer, _ = self.freeze()

        self.host.stop_head.assert_called_once_with(
            self.record, WORKER_ROLE, STOPPED_BY_OPERATOR
        )
        forget.assert_called_once_with(self.record, WORKER_ROLE)
        self.assertEqual(worker, ["card-1"])
        self.assertEqual(reviewer, [])
        self.assertEqual(self.record.review_handle, "review-pane")
        self.assertEqual(self.record.paused_reviewer_at, 0.0)
        self.assertGreater(self.record.paused_worker_at, 0.0)

    def test_worker_stop_failure_keeps_worker_identity_and_stops_reviewer(self) -> None:
        self.host.stop_head.side_effect = HostError("worker busy")
        with (
            mock.patch.object(pause_ops, "launch_intent", return_value=None),
            mock.patch.object(pause_ops, "end_review_pane") as end_review,
            mock.patch.object(pause_ops, "forget_role_head") as forget,
        ):
            worker, reviewer, _ = self.freeze()

        end_review.assert_called_once_with(self.host, self.record, STOPPED_BY_OPERATOR)
        forget.assert_not_called()
        self.assertEqual(worker, [])
        self.assertEqual(reviewer, ["card-1"])
        self.assertEqual(self.record.handle, "worker-pane")
        self.assertEqual(self.record.paused_worker_at, 0.0)
        self.assertGreater(self.record.paused_reviewer_at, 0.0)

    def test_failed_reviewer_launch_intent_still_attempts_worker_stop(self) -> None:
        with (
            mock.patch.object(pause_ops, "launch_intent", return_value={"role": REVIEW_ROLE}),
            mock.patch.object(pause_ops, "stop_launch_intent", return_value="not stopped"),
            mock.patch.object(pause_ops, "end_review_pane") as end_review,
            mock.patch.object(pause_ops, "keep_reserved_round") as keep_round,
            mock.patch.object(pause_ops, "forget_role_head"),
        ):
            worker, reviewer, _ = self.freeze()

        end_review.assert_not_called()
        keep_round.assert_not_called()
        self.host.stop_head.assert_called_once_with(
            self.record, WORKER_ROLE, STOPPED_BY_OPERATOR
        )
        self.assertEqual((worker, reviewer), (["card-1"], []))
        self.assertEqual(self.record.paused_reviewer_at, 0.0)

    def test_failed_worker_launch_intent_still_attempts_reviewer_stop(self) -> None:
        with (
            mock.patch.object(pause_ops, "launch_intent", return_value={"role": "worker"}),
            mock.patch.object(pause_ops, "stop_launch_intent", return_value="not stopped"),
            mock.patch.object(pause_ops, "end_review_pane") as end_review,
            mock.patch.object(pause_ops, "forget_role_head") as forget,
        ):
            worker, reviewer, _ = self.freeze()

        end_review.assert_called_once_with(self.host, self.record, STOPPED_BY_OPERATOR)
        self.host.stop_head.assert_not_called()
        forget.assert_not_called()
        self.assertEqual((worker, reviewer), ([], ["card-1"]))
        self.assertEqual(self.record.paused_worker_at, 0.0)

    def test_a_stopped_launch_is_counted_once_even_if_an_old_handle_remains(self) -> None:
        with (
            mock.patch.object(pause_ops, "launch_intent", return_value={"role": REVIEW_ROLE}),
            mock.patch.object(pause_ops, "stop_launch_intent", return_value=None),
            mock.patch.object(pause_ops, "keep_reserved_round"),
            mock.patch.object(pause_ops, "end_review_pane"),
            mock.patch.object(pause_ops, "forget_role_head"),
        ):
            worker, reviewer, _ = self.freeze()

        self.assertEqual((worker, reviewer), (["card-1"], ["card-1"]))

    def test_excluded_workspace_does_not_stop_either_head(self) -> None:
        with mock.patch.object(pause_ops, "launch_intent") as intent:
            result = pause_ops._freeze_heads(
                self.runtime, {"card-1": self.record}, {"/tmp/ummanu-freeze-test"}
            )
        self.assertEqual(result, ([], [], ["card-1"]))
        intent.assert_not_called()
        self.host.stop_head.assert_not_called()


class ObserverStateReasonTests(unittest.TestCase):
    def test_transition_reason_is_durable_and_repeated_tick_is_noop(self) -> None:
        record = observer.ObserverRecord(sprint="sprint:1", state="running")
        with mock.patch.object(observer.time, "time", return_value=10.0):
            observer._set_observer_state(record, "degraded", reason="sprint unavailable")

        self.assertEqual(record.state_reason, "sprint unavailable")
        self.assertEqual(record.last_action, "degraded")
        self.assertEqual(record.last_action_at, 10.0)
        restored = observer.ObserverRecord.from_json(record.to_json())
        self.assertEqual(restored.state_reason, "sprint unavailable")
        with mock.patch.object(observer.time, "time", side_effect=AssertionError("no transition")):
            observer._set_observer_state(record, "degraded", reason="sprint unavailable")

        self.assertEqual(record.last_action_at, 10.0)

    def test_reason_change_does_not_reset_state_entry_timestamp(self) -> None:
        record = observer.ObserverRecord(sprint="sprint:1", state="waiting")
        record.state_reason = "old"
        record.last_action_at = 30.0
        with mock.patch.object(observer.time, "time", side_effect=AssertionError("not new state")):
            observer._set_observer_state(record, "waiting", reason="new")

        self.assertEqual(record.state_reason, "new")
        self.assertEqual(record.last_action_at, 30.0)
        with mock.patch.object(observer.time, "time", return_value=40.0):
            observer._set_observer_state(record, "running")
        self.assertEqual(record.state_reason, "")
        self.assertEqual(record.last_action_at, 40.0)

    def test_legacy_idle_reason_is_not_work_state_reason(self) -> None:
        record = observer.ObserverRecord.from_json(
            {"sprint": "sprint:1", "state": "waiting", "idle_reason": "old idle"}
        )
        self.assertEqual(record.state_reason, "")
        with mock.patch.object(observer.time, "time", side_effect=AssertionError("same state")):
            observer._set_observer_state(record, "waiting")
        with mock.patch.object(observer.time, "time", side_effect=AssertionError("reason only")):
            observer._set_observer_state(record, "waiting", reason="a fresh wait")
        self.assertEqual(record.idle_reason, "old idle")
        self.assertEqual(record.state_reason, "a fresh wait")

    def test_idle_grace_snapshot_reads_the_quiet_age_at_read_time(self) -> None:
        record = observer.ObserverRecord(sprint="sprint:1", state="waiting")
        observer._mark_idle_grace(record, since=12.0, reason="no pending work")
        self.assertEqual(record.state_reason, "no pending work")
        payload = {"observers": {"sprint:1": record.to_json()}}
        dead = {"alive": False, "pid_known": False, "reason": "not-running"}
        expected = {
            100.0: "no pending work; its terminal has been quiet for 88s",
            700.0: "no pending work; its terminal has been quiet for 688s",
        }
        for now, reason in expected.items():
            with (
                mock.patch.object(observer, "observer_alive", return_value=dead),
                mock.patch.object(observer.time, "time", return_value=now),
            ):
                snapshot = observer.observer_snapshot(payload)
                status_rows = status._observers(payload)
            self.assertEqual(snapshot[0]["state_reason"], reason)
            self.assertEqual(snapshot[0]["idle_reason"], reason)
            self.assertEqual(status_rows[0]["state_reason"], reason)

    def test_a_head_that_left_idle_grace_reports_no_idle_reason(self) -> None:
        record = observer.ObserverRecord(sprint="sprint:1", state="waiting")
        observer._mark_idle_grace(record, since=12.0, reason="no pending work")
        observer._set_observer_state(record, "running")
        payload = {"observers": {"sprint:1": record.to_json()}}
        with mock.patch.object(
            observer, "observer_alive", return_value={"alive": True, "pid_known": True, "reason": "pid"}
        ):
            snapshot = observer.observer_snapshot(payload)
        self.assertEqual(snapshot[0]["idle_reason"], "")
        self.assertEqual(snapshot[0]["idle_since"], 0.0)


if __name__ == "__main__":
    unittest.main()
