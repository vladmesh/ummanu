"""The worker wait tick records a stale round once, as one `Stale round:` dispatcher comment."""

from __future__ import annotations

import time
import unittest

from tests.dispatcher_fixtures import CARD_REF, RUNNING_STATUS, STOPPED_STATUS, DispatcherRuntimeFixture
from ummanu.board.estimate import DEFAULT_ESTIMATE_MINUTES
from ummanu.dispatch.stale_round import DEFAULT_QUIET_MINUTES, ESTIMATE_FACTOR, WorkspaceReading

ESTIMATE_SECONDS = ESTIMATE_FACTOR * DEFAULT_ESTIMATE_MINUTES * 60
QUIET_SECONDS = DEFAULT_QUIET_MINUTES * 60


class StaleRoundTests(DispatcherRuntimeFixture, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.start_dispatcher()
        self.tick()  # claim + launch
        self._workspace_changed_now()
        self.assertEqual(self.tick()["action"], "waiting-worker-report")

    def _workspace_changed_now(self) -> None:
        self.host.round_workspace_result = WorkspaceReading(newest_file_at=time.time())

    def _age_round(self, seconds: float) -> None:
        payload = self.runtime.production_state.load()
        payload["records"][CARD_REF]["worker_stale_round"]["started_at"] -= seconds
        self.runtime.production_state.save(payload)

    def _stale_comments(self) -> list[str]:
        return [
            str(comment.get("body") or "")
            for comment in self.reader.show(CARD_REF)["comments"]
            if "Stale round:" in str(comment.get("body") or "")
        ]

    def test_a_round_past_twice_its_estimate_is_recorded_and_nothing_else_changes(self) -> None:
        self._age_round(ESTIMATE_SECONDS + 60)
        self._workspace_changed_now()

        outcome = self.tick()

        comments = self._stale_comments()
        self.assertEqual(len(comments), 1, comments)
        self.assertIn("rule estimate", comments[0])
        self.assertIn(f"estimate {DEFAULT_ESTIMATE_MINUTES} min (default)", comments[0])
        self.assertIn("report generation 1", comments[0])
        self.assertEqual(outcome["action"], "waiting-worker-report")
        self.assertEqual(self.reader.show(CARD_REF)["state"], "in_progress")
        self.assertEqual(self._pilot_record()["worker_respawns"], 0)
        self.assertEqual(self.host.stopped, [])
        self.assertNotIn("restart_worker", self.host.calls)

    def test_a_round_below_both_thresholds_writes_nothing(self) -> None:
        self._age_round(ESTIMATE_SECONDS - 60)
        self._workspace_changed_now()

        self.tick()

        self.assertEqual(self._stale_comments(), [])

    def test_a_live_head_with_a_quiet_workspace_is_recorded_with_its_facts(self) -> None:
        self._age_round(QUIET_SECONDS + 600)
        changed_at = time.time() - QUIET_SECONDS - 60
        self.host.round_workspace_result = WorkspaceReading(
            commits=1,
            newest_commit_at=changed_at,
            files_changed=2,
            lines_added=10,
            lines_removed=3,
            newest_file_at=changed_at,
        )
        self.host.worker_status_result = dict(RUNNING_STATUS)

        self.tick()

        self.assertIn(
            self._pilot_record()["worker_vitality_episode"]["verdict"], {"healthy_active", "healthy_quiet"}
        )

        comments = self._stale_comments()
        self.assertEqual(len(comments), 1, comments)
        self.assertIn("rule quiet", comments[0])
        self.assertIn("Commits since base: 1", comments[0])
        self.assertIn("2 files, +10/-3 lines", comments[0])

    def test_a_quiet_workspace_under_a_head_that_is_not_alive_writes_nothing(self) -> None:
        self._age_round(QUIET_SECONDS + 600)
        self.host.round_workspace_result = WorkspaceReading(newest_file_at=time.time() - QUIET_SECONDS - 60)
        self.host.worker_status_result = dict(STOPPED_STATUS)

        self.tick()

        self.assertEqual(self._pilot_record()["worker_vitality_episode"]["verdict"], "suspended")
        self.assertEqual(self._stale_comments(), [])

    def test_many_stale_ticks_of_one_round_write_one_comment(self) -> None:
        self._age_round(ESTIMATE_SECONDS + 60)
        for _ in range(4):
            self.tick()

        self.assertEqual(len(self._stale_comments()), 1)

    def test_a_rework_round_is_recorded_again(self) -> None:
        self._age_round(ESTIMATE_SECONDS + 60)
        self.tick()
        self.assertEqual(len(self._stale_comments()), 1)

        self._report_done()
        self.assertEqual(self.tick()["to"], "validate")
        self.tick()
        self._review_red()
        self._park_and_decide("rework")
        self._assert_one_generation(2)
        self._workspace_changed_now()
        self.tick()
        self.tick()
        self.assertEqual(self._pilot_record()["worker_stale_round"]["generation"], 2)
        self.assertEqual(len(self._stale_comments()), 1, "a fresh round is not stale")

        self._age_round(ESTIMATE_SECONDS + 60)
        self.tick()

        comments = self._stale_comments()
        self.assertEqual(len(comments), 2, comments)
        self.assertIn("report generation 2", comments[1])


if __name__ == "__main__":
    unittest.main()
