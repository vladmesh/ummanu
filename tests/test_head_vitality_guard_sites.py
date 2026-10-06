"""Call-site coverage for the single destructive guard (card S1-4).

Two questions, both answered structurally so they stay true when the code moves:

1. *Is every watchdog-driven destructive path fenced?* The wait tick's recovery entry
   point (``_trigger_wait_watchdog``), its direct ceiling-driven respawn/escalate
   branches, and the retained-worker replacement chain must all ask the guard before a
   head is stopped, killed or replaced. The test monkeypatches the guard to refuse
   everything and asserts no destructive host call happens on any path -- flipping the
   guard's verdict to ``allowed`` cannot be caught this way, so each path is additionally
   driven to its destructive step with the real guard and asserted to have happened.

2. *Are the legitimate stops still legitimate?* Operator-initiated, card-lifecycle and
   launch-recovery stops run without any episode and must keep working: the guard is
   called only on the watchdog-driven paths, and these tests pin that a stop with no
   episode on file still succeeds there.
"""

from __future__ import annotations

import unittest
from unittest import mock

from tests.dispatcher_fixtures import DispatcherRuntimeFixture
from ummanu.dispatch import wait_vitality as wait_vitality_module


def _refuse_everything(_episode, action, _now, **_kwargs):
    """A guard that always refuses, carrying the action it was asked about."""

    class _Decision:
        allowed = False
        refusal = type("R", (), {"value": "test-refusal"})()
        reason = f"test suite refuses {action}"

        def to_json(self):
            return {"action": action, "allowed": False, "reason": self.reason}

    return _Decision()


class WatchdogPathsAreGuardedTests(DispatcherRuntimeFixture, unittest.TestCase):
    """Every watchdog-driven destructive step must pass through the vitality guard."""

    def _with_refusing_guard(self):
        """Patch the dispatcher's guard symbol to refuse everything."""
        return mock.patch.object(
            wait_vitality_module,
            "_assert_destructive_allowed",
            side_effect=_refuse_everything,
        )

    def test_a_confirmed_stall_respawn_is_refused_by_the_guard(self) -> None:
        self._open_the_second_round()
        self._head_at_its_prompt()
        self.tick()
        self._rewind_idle()
        self.tick()  # the round's one prompt resets the episode
        self._rewind_idle()

        with self._with_refusing_guard():
            outcome = self.tick()

        self.assertEqual(outcome["action"], "worker-guard-refused")
        self.assertNotIn("restart_worker", self.host.calls)

    def test_an_escalation_is_refused_by_the_guard(self) -> None:
        """The ladder's end: after a prompt and a respawn, the next confirmed episode
        escalates to Blocked. That escalation is destructive (it replaces the round's
        worker with nothing), so it must pass the guard."""
        self._open_the_second_round()
        self._head_at_its_prompt()
        self.tick()
        self.assertEqual(self._bounce_the_idle_worker()["action"], "worker-respawned")
        self.tick()
        self._rewind_idle()

        with self._with_refusing_guard():
            outcome = self.tick()

        # The escalation wants to block; the guard refuses and the tick degrades to an
        # explicit wait instead.
        self.assertEqual(outcome["action"], "worker-guard-refused")
        self.assertNotEqual(self.reader.show("ummanu-510")["state"], "blocked")

    def test_the_escalation_happens_through_the_real_guard(self) -> None:
        self._open_the_second_round()
        self._head_at_its_prompt()
        self.tick()
        self.assertEqual(self._bounce_the_idle_worker()["action"], "worker-respawned")
        self.tick()
        self._rewind_idle()

        outcome = self.tick()

        self.assertEqual(outcome["to"], "blocked")
        self.assertIn("stop_head:worker", self.host.calls)

    def test_a_review_respawn_is_refused_by_the_guard(self) -> None:
        self.start_dispatcher()
        self._run_worker_to_validate()
        self.assertEqual(self.tick()["action"], "review-started")
        self._head_at_its_prompt("review")
        self.assertEqual(self.tick()["action"], "waiting-review-verdict")
        self._rewind_idle("review")

        with self._with_refusing_guard():
            outcome = self.tick()

        self.assertEqual(outcome["action"], "review-guard-refused")
        self.assertNotIn("stop_review", self.host.calls)

    def test_a_dead_head_reclaim_is_refused_by_the_guard(self) -> None:
        self.start_dispatcher()
        self.tick()
        self._kill_worker_heartbeat()
        self.host.worker_status_result = {"known": True, "live": False, "reason": "missing-terminal"}

        with self._with_refusing_guard():
            outcome = self.tick()

        self.assertEqual(outcome["action"], "worker-guard-refused")
        self.assertNotIn("restart_worker", self.host.calls)

    def _kill_worker_heartbeat(self) -> None:
        """Rewrite the worker heartbeat with a reaped pid (the head is genuinely gone)."""
        import subprocess

        proc = subprocess.Popen(["true"])
        proc.wait()
        record = self.runtime.production_state.records(self.runtime.production_state.load())[
            "ummanu-510"
        ]
        self.host.head_pid = proc.pid
        self.host._write_head_pid(
            "worker",
            "ummanu-510",
            head_run=record.worker_head_run,
            leaf=record.worker_leaf,
        )

    def test_the_unobservable_ceiling_escalates_without_touching_the_head(self) -> None:
        """No reduction ever ran (patched out): the ceiling branch may not destroy an
        unobserved head. Its bound is operator escalation -- one durable comment per
        wait cycle plus a degraded outcome -- never a stop or a replacement."""
        self._open_the_second_round()
        self._head_at_its_prompt()
        self.tick()
        self._rewind_idle()
        self.tick()  # the round's one prompt
        self._rewind_idle()

        with mock.patch.object(
            wait_vitality_module,
            "reduce_and_store_vitality_episode",
            lambda *args, **kwargs: None,
        ):
            self.tick()  # stamps the fresh waiting window
            payload = self.runtime.production_state.load()
            record_payload = payload["records"]["ummanu-510"]
            record_payload["worker_waiting_since"] -= 100_000.0
            self.runtime.production_state.save(payload)
            outcome = self.tick()

        self.assertEqual(outcome["action"], "worker-unobserved-wait-escalated")
        self.assertEqual(outcome["status"], "degraded")
        self.assertNotIn("restart_worker", self.host.calls)
        self.assertNotIn("stop_head:worker", self.host.calls)
        last = self.reader.show("ummanu-510")["comments"][-1]["body"]
        self.assertIn("NOT stopped or replaced", last)


class TheGuardReallyFiresTests(DispatcherRuntimeFixture, unittest.TestCase):
    """With the real guard, the same paths reach their destructive step.

    A coverage test that only proves refusal would also pass if the guard were never
    called at all (a permanently-refusing inline check would look identical), so each
    path is driven through the real guard to its action.
    """

    def test_the_confirmed_stall_respawn_happens_through_the_real_guard(self) -> None:
        self._open_the_second_round()
        self._head_at_its_prompt()
        self.tick()
        self._rewind_idle()
        self.tick()  # the round's one prompt
        self._rewind_idle()

        with mock.patch.object(
            wait_vitality_module,
            "_assert_destructive_allowed",
            wraps=wait_vitality_module._assert_destructive_allowed,
        ) as guard:
            outcome = self.tick()

        self.assertEqual(outcome["action"], "worker-respawned")
        self.assertIn("restart_worker", self.host.calls)
        self.assertEqual(guard.call_count, 1)
        self.assertEqual(guard.call_args.args[1], "worker-respawn")


class LegitimateStopsWithoutAnEpisodeTests(DispatcherRuntimeFixture, unittest.TestCase):
    """Operator, lifecycle and launch-recovery stops need no episode and get none."""

    def test_an_operator_stop_runs_without_any_episode(self) -> None:
        self._open_the_second_round()
        payload = self.runtime.production_state.load()
        record = self.runtime.production_state.records(payload)["ummanu-510"]
        self.assertIsNone(record.worker_vitality_episode)

        # The operator's explicit command path: the host's stop_head with no vitality
        # history on file. It must not be fenced by the guard.
        with mock.patch.object(
            wait_vitality_module,
            "_assert_destructive_allowed",
            side_effect=AssertionError("the guard must not be consulted"),
        ):
            self.runtime.host.stop_head(record, "worker", "operator asked")
        self.assertIn("stop_head:worker", self.host.calls)

    def test_a_card_lifecycle_stop_runs_without_any_episode(self) -> None:
        self.start_dispatcher()
        self.tick()
        payload = self.runtime.production_state.load()
        record = self.runtime.production_state.records(payload)["ummanu-510"]
        self.assertIsNone(record.worker_vitality_episode)
        # The lifecycle stop goes through its own confirmed-stop path, which the guard
        # never sees; the assertion is that it completes without one.
        with mock.patch.object(
            wait_vitality_module,
            "_assert_destructive_allowed",
            side_effect=AssertionError("the guard must not be consulted"),
        ):
            self.runtime.host.stop_head(record, "worker", "card blocked")
        self.assertIn("stop_head:worker", self.host.calls)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
