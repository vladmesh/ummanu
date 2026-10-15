from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from ummanu.dispatch import ready_skips
from ummanu.dispatch.ready_skips import READY_SKIP_NOTICE_SECONDS, notice_long_ready_skips, skip_until

T0 = 1_790_000_000.0
RED = "no launchable head for codex-sol61-high: codex-sol61-high on openai-sub is exhausted"


class _Writer:
    def __init__(self) -> None:
        self.comments: list[dict] = []

    def comment(self, **kwargs) -> dict:
        self.comments.append(kwargs)
        return {}


def _skipped(*skips: dict) -> dict:
    return {"status": "skipped", "step": "production-claim", "skipped_ready": list(skips)}


class ReadySkipNoticeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = SimpleNamespace(writer=_Writer(), owner="dispatcher")
        self.payload: dict = {}

    def tick(self, at: float, outcome: dict | None) -> list[dict]:
        with mock.patch.object(ready_skips.time, "time", return_value=at):
            return notice_long_ready_skips(self.runtime, self.payload, outcome)

    def test_one_comment_names_the_reason_and_when_the_resource_returns(self) -> None:
        skip = {"ref": "kit-39", "reason": RED, "until": T0 + 86400}
        self.tick(T0, _skipped(skip))
        self.assertEqual(self.tick(T0 + READY_SKIP_NOTICE_SECONDS - 1, _skipped(skip)), [])

        notices = self.tick(T0 + READY_SKIP_NOTICE_SECONDS, _skipped(skip))
        self.tick(T0 + 2 * READY_SKIP_NOTICE_SECONDS, _skipped(skip))

        self.assertEqual([notice["pilot_ref"] for notice in notices], ["kit-39"])
        [comment] = self.runtime.writer.comments
        self.assertEqual(comment["reference"], "kit-39")
        self.assertIn(RED, comment["body"])
        self.assertIn("Expected back 2026-09-22T14:13Z.", comment["body"])
        self.assertEqual(comment["request_id"], f"ready-skip-notice-kit-39-{int(T0)}")

    def test_a_changed_reason_starts_a_new_streak(self) -> None:
        self.tick(T0, _skipped({"ref": "kit-39", "reason": RED}))
        self.tick(T0 + READY_SKIP_NOTICE_SECONDS, _skipped({"ref": "kit-39", "reason": "waiting for a slot"}))

        self.assertEqual(self.runtime.writer.comments, [])

    def test_a_full_pass_forgets_a_card_it_did_not_skip(self) -> None:
        self.tick(T0, _skipped({"ref": "kit-39", "reason": RED}))
        self.tick(T0 + 60, None)
        self.tick(T0 + READY_SKIP_NOTICE_SECONDS, _skipped({"ref": "kit-39", "reason": RED}))

        self.assertEqual(self.runtime.writer.comments, [])

    def test_a_pass_that_ended_on_a_claim_keeps_the_streaks_it_did_not_reach(self) -> None:
        self.tick(T0, _skipped({"ref": "kit-39", "reason": RED}, {"ref": "kit-40", "reason": RED}))
        self.tick(T0 + 60, {"status": "ok", "step": "claim", "pilot_ref": "kit-40"})
        self.tick(T0 + READY_SKIP_NOTICE_SECONDS, _skipped({"ref": "kit-39", "reason": RED}))

        self.assertEqual([comment["reference"] for comment in self.runtime.writer.comments], ["kit-39"])
        self.assertNotIn("kit-40", self.payload[ready_skips.READY_SKIPS_KEY])

    def test_skip_until_reads_the_red_verdicts_a_failover_rejected(self) -> None:
        outcome = {
            "readiness": {"status": "ready", "until": 0.0},
            "failover": {
                "worker": {"rejected": [{"head": "codex", "status": "exhausted", "until": T0 + 5}]},
                "review": {"rejected": []},
            },
        }
        self.assertEqual(skip_until(outcome), T0 + 5)


if __name__ == "__main__":
    unittest.main()
