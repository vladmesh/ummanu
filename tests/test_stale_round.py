"""The worker wait tick records a stale round once, as one `Stale round:` dispatcher comment.

The wait tick reads one fake clock that each test advances; the round starts at the launch's own
`worker_started_at` and every workspace timestamp is a reading of the same clock.
"""

from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from unittest import mock

from tests.dispatcher_fixtures import CARD_REF, STOPPED_STATUS, DispatcherRuntimeFixture
from ummanu.board.estimate import DEFAULT_ESTIMATE_MINUTES
from ummanu.dispatch import wait_vitality
from ummanu.dispatch.stale_round import DEFAULT_QUIET_MINUTES, ESTIMATE_FACTOR, WorkspaceReading

ESTIMATE_SECONDS = ESTIMATE_FACTOR * DEFAULT_ESTIMATE_MINUTES * 60
QUIET_SECONDS = DEFAULT_QUIET_MINUTES * 60
STEP = 1200
RECORDED_ONLY = "Recorded only: nothing was moved, stopped, interrupted or charged."


def _utc(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


class StaleRoundTests(DispatcherRuntimeFixture, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.start_dispatcher()
        self.tick()  # claim + launch
        self.start = float(self._pilot_record()["worker_started_at"])
        self.now = self.start
        clock = mock.patch.object(wait_vitality, "time", SimpleNamespace(time=lambda: self.now))
        clock.start()
        self.addCleanup(clock.stop)
        self.turns = 0
        self.assertEqual(self._tick_at(self.start, busy=True)["action"], "waiting-worker-report")
        self.assertEqual(self._pilot_record()["worker_stale_round"]["started_at"], self.start)

    def _tick_at(self, at: float, *, busy: bool) -> dict:
        """One wait tick at clock time `at` from a head whose transcript advanced since the last.

        `busy` stamps a workspace change at `at`; otherwise the scripted reading stands.
        """
        self.now = at
        self.turns += 1
        self.host.provider_cursor = f"working:{self.turns}"
        if busy:
            self.host.round_workspace_result = WorkspaceReading(newest_file_at=at)
        return self.tick()

    def _walk_to(self, at: float, *, busy: bool) -> dict:
        """Tick every `STEP` seconds from the current clock, ending with a tick at exactly `at`."""
        while self.now + STEP < at:
            self._tick_at(self.now + STEP, busy=busy)
        return self._tick_at(at, busy=busy)

    def _stale_comments(self) -> list[str]:
        return [
            str(comment.get("body") or "").removeprefix("[dispatcher]\n")
            for comment in self.reader.show(CARD_REF)["comments"]
            if "Stale round:" in str(comment.get("body") or "")
        ]

    def test_the_estimate_rule_holds_only_past_twice_the_estimate(self) -> None:
        at_threshold = self._walk_to(self.start + ESTIMATE_SECONDS, busy=True)

        self.assertEqual(self._stale_comments(), [])
        self.assertEqual(at_threshold["action"], "waiting-worker-report")
        self.assertEqual(self._pilot_record()["worker_vitality_episode"]["verdict"], "healthy_active")

        past = self._tick_at(self.start + ESTIMATE_SECONDS + 1, busy=True)

        self.assertEqual(
            self._stale_comments(),
            [
                (
                    "Stale round: rule estimate (elapsed > 2x estimate), report generation 1."
                    f" Elapsed {round((ESTIMATE_SECONDS + 1) / 60)} min;"
                    f" estimate {DEFAULT_ESTIMATE_MINUTES} min (default)."
                    " Commits since base: 0. Diff against base: 0 files, +0/-0 lines."
                    f" Last change: {_utc(self.start + ESTIMATE_SECONDS + 1)}, 0 min ago. {RECORDED_ONLY}"
                )
            ],
        )
        self.assertEqual(past["action"], "waiting-worker-report")
        self.assertEqual(self.reader.show(CARD_REF)["state"], "in_progress")
        self.assertEqual(self._pilot_record()["worker_respawns"], 0)
        self.assertEqual(self.host.stopped, [])
        self.assertNotIn("restart_worker", self.host.calls)

    def test_the_quiet_rule_holds_from_n_minutes_without_change_under_a_live_head(self) -> None:
        self.host.round_workspace_result = WorkspaceReading(
            commits=1,
            newest_commit_at=self.start,
            files_changed=2,
            lines_added=10,
            lines_removed=3,
            newest_file_at=self.start,
        )

        self._walk_to(self.start + QUIET_SECONDS - 1, busy=False)
        self.assertEqual(self._stale_comments(), [])
        self._tick_at(self.start + QUIET_SECONDS, busy=False)
        self._tick_at(self.start + QUIET_SECONDS + 1, busy=False)

        self.assertEqual(self._pilot_record()["worker_vitality_episode"]["verdict"], "healthy_active")
        self.assertEqual(
            self._stale_comments(),
            [
                (
                    "Stale round: rule quiet (no commit or file change for"
                    f" {DEFAULT_QUIET_MINUTES} min while the head is alive), report generation 1."
                    f" Elapsed {DEFAULT_QUIET_MINUTES} min; estimate {DEFAULT_ESTIMATE_MINUTES} min (default)."
                    f" Commits since base: 1, newest {_utc(self.start)}."
                    " Diff against base: 2 files, +10/-3 lines."
                    f" Last change: {_utc(self.start)}, {DEFAULT_QUIET_MINUTES} min ago. {RECORDED_ONLY}"
                )
            ],
        )

    def test_a_quiet_workspace_under_a_head_that_is_not_alive_writes_nothing(self) -> None:
        self.host.round_workspace_result = WorkspaceReading(newest_file_at=self.start)
        self.host.worker_status_result = dict(STOPPED_STATUS)

        self._tick_at(self.start + QUIET_SECONDS + 1, busy=False)

        self.assertEqual(self._pilot_record()["worker_vitality_episode"]["verdict"], "suspended")
        self.assertEqual(self._stale_comments(), [])

    def test_many_stale_ticks_of_one_round_write_one_comment(self) -> None:
        self._walk_to(self.start + ESTIMATE_SECONDS + 1, busy=True)
        for step in range(1, 5):
            self._tick_at(self.start + ESTIMATE_SECONDS + 1 + step * STEP, busy=True)

        self.assertEqual(len(self._stale_comments()), 1)

    def test_a_rework_round_is_recorded_again(self) -> None:
        self.host.round_workspace_result = WorkspaceReading(newest_file_at=self.start)
        self._walk_to(self.start + QUIET_SECONDS, busy=False)
        self.assertEqual(len(self._stale_comments()), 1)

        self._report_done()
        self.assertEqual(self.tick()["to"], "validate")
        self.tick()
        self._review_red()
        self._park_and_decide("rework")
        self._assert_one_generation(2)
        reopened = self.now + 1
        self._tick_at(reopened, busy=True)
        self.assertEqual(self._pilot_record()["worker_stale_round"]["generation"], 2)
        self.assertEqual(len(self._stale_comments()), 1, "a fresh round is not stale")

        self._tick_at(reopened + QUIET_SECONDS - 1, busy=False)
        self.assertEqual(len(self._stale_comments()), 1)
        self._tick_at(reopened + QUIET_SECONDS, busy=False)

        comments = self._stale_comments()
        self.assertEqual(len(comments), 2, comments)
        self.assertIn("rule quiet", comments[1])
        self.assertIn("report generation 2", comments[1])


if __name__ == "__main__":
    unittest.main()
