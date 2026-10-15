"""Round facts from a real checkout, and the two stale rules over them."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from ummanu.dispatch.head_vitality_episode import VitalityVerdict
from ummanu.dispatch.stale_round import (
    RULE_ESTIMATE,
    RULE_QUIET,
    WorkspaceReading,
    read_round_workspace,
    round_facts,
    stale_rule,
)
from ummanu.dispatch.state import DispatcherRecord
from ummanu.dispatch.types import HostError
from ummanu.dispatch.worker_lifecycle import StaleRound

GIT_IDENTITY = ["-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid"]


def _run(args: list[str], label: str) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(args, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise HostError(f"{label} failed: {completed.stderr.strip()}")
    return completed


def _record(started_at: float) -> DispatcherRecord:
    record = DispatcherRecord(
        worker="w", workspace="", handle="", head="", review_head="", attempt_id="a",
        comment_baseline=0, review_baseline=0, state="claimed", claimed_at=0.0,
    )
    record.worker_stale_round = StaleRound(generation=1, started_at=started_at)
    return record


class ReadRoundWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.git("init", "-q", "-b", "main")
        (self.root / "kept.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
        self.git("add", "kept.txt")
        self.git("commit", "-q", "-m", "base")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")

    def git(self, *args: str) -> None:
        _run(["git", *GIT_IDENTITY, "-C", str(self.root), *args], "fixture git")

    def commit_at(self, epoch: int, *args: str) -> None:
        _run(
            ["env", f"GIT_COMMITTER_DATE=@{epoch}", "git", *GIT_IDENTITY, "-C", str(self.root), "commit", "-q", *args],
            "fixture git",
        )

    def test_commits_diff_and_newest_change_are_read_against_the_fork(self) -> None:
        self.git("switch", "-q", "-c", "card")
        (self.root / "kept.txt").write_text("one\nthree\nfour\nfive\n", encoding="utf-8")
        self.commit_at(1700000000, "-am", "edit")
        (self.root / "new.txt").write_text("new\n", encoding="utf-8")
        self.git("add", "new.txt")
        self.commit_at(1700000600, "-m", "add")
        os.utime(self.root / "kept.txt", (1700001000, 1700001000))
        os.utime(self.root / "new.txt", (1700001000, 1700001000))
        namespace = self.root / ".ummanu-task-env"
        namespace.mkdir()
        (namespace / "scratch").write_text("", encoding="utf-8")
        os.utime(namespace / "scratch", (1800000000, 1800000000))

        reading = read_round_workspace(_run, str(self.root), "origin/main")

        self.assertEqual(reading.unknown, "")
        self.assertEqual(reading.commits, 2)
        self.assertEqual(reading.newest_commit_at, 1700000600.0)
        self.assertEqual((reading.files_changed, reading.lines_added, reading.lines_removed), (2, 3, 1))
        self.assertEqual(reading.newest_file_at, 1700001000.0)

    def test_a_linked_worktree_root_git_file_is_not_a_change(self) -> None:
        worktree = self.root / "linked"
        self.git("worktree", "add", "-q", "-b", "card", str(worktree))
        (worktree / "kept.txt").write_text("one\n", encoding="utf-8")
        os.utime(worktree / "kept.txt", (1700001000, 1700001000))
        (worktree / ".ummanu-task-env").write_text("", encoding="utf-8")
        for name in (".git", ".ummanu-task-env"):
            self.assertTrue((worktree / name).is_file())
            os.utime(worktree / name, (1800000000, 1800000000))

        reading = read_round_workspace(_run, str(worktree), "origin/main")

        self.assertEqual(reading.unknown, "")
        self.assertEqual(reading.newest_file_at, 1700001000.0)

    def test_an_unreadable_workspace_is_unknown_with_its_reason(self) -> None:
        reading = read_round_workspace(_run, str(self.root / "missing"), "origin/main")

        self.assertTrue(reading.unknown)
        self.assertIn("fork point", reading.unknown)


class StaleRuleTests(unittest.TestCase):
    def rule(self, reading: WorkspaceReading, *, elapsed: float, verdict: VitalityVerdict | None) -> str:
        now = 100_000.0
        facts = round_facts(_record(now - elapsed), reading, now)
        return stale_rule(facts, estimate_minutes=60, verdict=verdict, quiet_after=1800)

    def test_the_estimate_rule_fires_past_twice_the_estimate(self) -> None:
        busy = WorkspaceReading(newest_file_at=100_000.0)
        self.assertEqual(self.rule(busy, elapsed=7200, verdict=None), "")
        self.assertEqual(self.rule(busy, elapsed=7201, verdict=None), RULE_ESTIMATE)

    def test_the_quiet_rule_needs_a_live_head_and_a_read_workspace(self) -> None:
        quiet = WorkspaceReading(newest_file_at=100_000.0 - 1800)
        alive = VitalityVerdict.HEALTHY_ACTIVE
        self.assertEqual(self.rule(quiet, elapsed=3600, verdict=alive), RULE_QUIET)
        self.assertEqual(self.rule(quiet, elapsed=3600, verdict=VitalityVerdict.DEAD), "")
        self.assertEqual(self.rule(quiet, elapsed=1799, verdict=alive), "", "quiet starts at the round")
        unknown = WorkspaceReading(unknown="checkout gone")
        self.assertEqual(self.rule(unknown, elapsed=3600, verdict=alive), "")


if __name__ == "__main__":
    unittest.main()
