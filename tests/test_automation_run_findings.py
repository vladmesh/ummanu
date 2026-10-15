"""Doctor findings read from the standing agents' own runs.jsonl."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ummanu import cli as product_cli
from ummanu.automations.runtime import run_findings


class RunFindingsTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name)
        self.now = datetime.now(UTC)

    def _log(self, agent: str, *events: tuple[timedelta, dict]) -> None:
        runs = self.data / "automation-state" / agent / "runs.jsonl"
        runs.parent.mkdir(parents=True, exist_ok=True)
        runs.write_text(
            "".join(json.dumps({"ts": (self.now - ago).isoformat(), **event}) + "\n" for ago, event in events),
            encoding="utf-8",
        )

    @staticmethod
    def _skips(hours: int, event: str = "dispatch") -> list[tuple[timedelta, dict]]:
        return [
            (timedelta(hours=hour), {"event": event, "action": "supervised-busy-skip"})
            for hour in range(hours, 0, -1)
        ]

    def test_an_eight_day_curator_busy_skip_streak_is_a_doctor_finding(self) -> None:
        self._log(
            "curator",
            (timedelta(days=8, hours=2), {"event": "advance"}),
            (timedelta(days=8, hours=1), {"event": "dispatch", "action": "supervised-started"}),
            *self._skips(8 * 24),
        )
        [finding] = product_cli.automation_run_findings(self.data)
        self.assertEqual(finding["code"], "automation_busy_without_advance")
        self.assertEqual(finding["agent"], "curator")
        self.assertEqual(finding["busy_skips"], 8 * 24)
        self.assertEqual(finding["busy_since"], (self.now - timedelta(days=8)).isoformat())

    def test_a_recent_advance_or_memory_write_gives_no_finding(self) -> None:
        for progress in ({"event": "advance"}, {"event": "memory_write", "result": "ok"}):
            with self.subTest(progress=progress):
                self._log("curator", *self._skips(8 * 24), (timedelta(minutes=30), progress))
                self.assertEqual(product_cli.automation_run_findings(self.data), [])

    def test_a_failed_memory_write_is_not_progress(self) -> None:
        self._log(
            "curator",
            *self._skips(24),
            (timedelta(minutes=30), {"event": "memory_write", "result": "error"}),
        )
        self.assertEqual(len(product_cli.automation_run_findings(self.data)), 1)

    def test_below_the_curator_threshold_or_after_a_fresh_head_there_is_no_finding(self) -> None:
        self._log("curator", *self._skips(run_findings.BUSY_WITHOUT_ADVANCE_HOURS[("curator", "dispatch")] - 1))
        self.assertEqual(product_cli.automation_run_findings(self.data), [])
        self._log(
            "curator",
            *self._skips(72),
            (timedelta(minutes=30), {"event": "dispatch", "action": "supervised-started"}),
        )
        self.assertEqual(product_cli.automation_run_findings(self.data), [])

    def test_one_missed_retro_run_is_a_finding(self) -> None:
        self._log("retro", (timedelta(minutes=5), {"event": "dispatch", "action": "supervised-busy-skip"}))
        [finding] = product_cli.automation_run_findings(self.data)
        self.assertEqual((finding["agent"], finding["dispatch"]), ("retro", "dispatch"))

    def test_one_missed_deep_sweep_is_a_finding_that_its_advance_clears(self) -> None:
        skipped = (timedelta(hours=1), {"event": "deep-sweep", "action": "supervised-busy-skip"})
        self._log("steward", skipped)
        [finding] = product_cli.automation_run_findings(self.data)
        self.assertEqual((finding["agent"], finding["dispatch"]), ("steward", "deep-sweep"))
        self._log("steward", skipped, (timedelta(minutes=5), {"event": "deep-sweep-advance"}))
        self.assertEqual(product_cli.automation_run_findings(self.data), [])

    def test_a_short_steward_busy_skip_is_no_finding(self) -> None:
        self._log("steward", *self._skips(2))
        self.assertEqual(product_cli.automation_run_findings(self.data), [])

    def test_no_runs_file_gives_no_finding(self) -> None:
        self.assertEqual(product_cli.automation_run_findings(self.data / "missing"), [])
        self.assertEqual(product_cli.automation_run_findings(None), [])

    def test_three_lock_refusals_in_a_row_are_a_red_finding_naming_the_holder(self) -> None:
        refused = {"event": "lock-refused", "holder_pid": 4242}
        self._log("curator", *[(timedelta(minutes=minutes), refused) for minutes in (180, 120, 60)])
        [finding] = product_cli.automation_run_findings(self.data)
        self.assertEqual(finding["code"], "automation_lock_refused")
        self.assertEqual((finding["agent"], finding["holder_pid"]), ("curator", 4242))
        self.assertEqual(finding["refused_since"], (self.now - timedelta(minutes=180)).isoformat())

    def test_a_successful_tick_clears_the_lock_refusal_streak(self) -> None:
        refused = {"event": "lock-refused", "holder_pid": 4242}
        self._log(
            "curator",
            *[(timedelta(minutes=minutes), refused) for minutes in (180, 120, 60)],
            (timedelta(minutes=5), {"event": "precheck", "result": "no-change"}),
        )
        self.assertEqual(product_cli.automation_run_findings(self.data), [])
        self._log("curator", *[(timedelta(minutes=minutes), refused) for minutes in (120, 60)])
        self.assertEqual(product_cli.automation_run_findings(self.data), [])


if __name__ == "__main__":
    unittest.main()
