"""Round facts from a real checkout, the two stale rules over them, and the one typed record."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from ummanu.board.estimate import DEFAULT_ESTIMATE_MINUTES
from ummanu.board.task_protocol.rows import is_significant_card_event, stale_round_fact
from ummanu.dispatch.head_vitality_episode import VitalityVerdict
from ummanu.dispatch.observer import render_observer_wake_context
from ummanu.dispatch.stale_round import (
    DEFAULT_QUIET_MINUTES,
    ESTIMATE_FACTOR,
    QUIET_MINUTES_ENV,
    RULE_ESTIMATE,
    RULE_QUIET,
    WorkspaceReading,
    quiet_seconds,
    read_round_workspace,
    record_stale_round,
    round_facts,
    stale_rule,
)
from ummanu.dispatch.state import DispatcherRecord
from ummanu.dispatch.types import HostError
from ummanu.dispatch.worker_lifecycle import StaleRound

REF = "ummanu-9001"
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


    def test_a_quiet_minutes_value_that_is_not_a_positive_whole_number_reads_as_the_default(self) -> None:
        for value in ("0", "-3", "abc", ""):
            with self.subTest(value=value), mock.patch.dict(os.environ, {QUIET_MINUTES_ENV: value}):
                self.assertEqual(quiet_seconds(), DEFAULT_QUIET_MINUTES * 60)

class _Writer:
    """The card writer's `stale_round`, idempotent by request id like the audit it stands for."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.request_ids: set[str] = set()

    def stale_round(self, *, actor: str, reference: str, body: str, fact: dict, request_id: str) -> dict:
        if request_id not in self.request_ids:
            self.request_ids.add(request_id)
            self.events.append(
                {
                    "event_id": f"evt-{len(self.events)}",
                    "kind": "commented",
                    "outcome": "success",
                    "ref": reference,
                    "actor": {"role": "dispatcher", "id": actor},
                    "payload": {"marker": "dispatcher", "body_sha256": "x", "stale_round": dict(fact)},
                    "body": body,
                }
            )
        return {}


class StaleRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.start = 1_700_000_000.0
        self.record = _record(self.start)
        self.record.report_generation = 1
        self.record.worker_started_at = self.start
        self.records = {REF: self.record}
        self.runtime = SimpleNamespace(
            owner="dispatcher",
            writer=_Writer(),
            save_records=lambda payload, records: None,
            host=SimpleNamespace(
                round_workspace=lambda task, record: WorkspaceReading(
                    commits=2,
                    newest_commit_at=self.start + 600,
                    files_changed=3,
                    lines_added=40,
                    lines_removed=7,
                    newest_file_at=self.start + 900,
                )
            ),
        )

    def tick(self, now: float) -> None:
        task = {"ref": REF, "type": "code"}
        record_stale_round(
            self.runtime, task, self.record, self.records, {}, VitalityVerdict.HEALTHY_ACTIVE, now=now
        )

    def test_a_stale_round_is_recorded_once_with_its_facts_in_the_payload(self) -> None:
        past = self.start + ESTIMATE_FACTOR * DEFAULT_ESTIMATE_MINUTES * 60 + 1
        for step in range(4):
            self.tick(past + step * 60)

        [event] = self.runtime.writer.events
        self.assertTrue(event["body"].startswith("Stale round: rule estimate"))
        self.assertEqual(
            stale_round_fact(event),
            {
                "card": REF,
                "rule": RULE_ESTIMATE,
                "generation": 1,
                "elapsed_seconds": round(past - self.start),
                "estimate_minutes": DEFAULT_ESTIMATE_MINUTES,
                "commits": 2,
                "files_changed": 3,
                "lines_added": 40,
                "lines_removed": 7,
                "last_change_at": "2023-11-14T22:28:20Z",
                "workspace_unknown": "",
            },
        )

    def test_the_stale_record_wakes_the_observer_and_a_plain_dispatcher_comment_does_not(self) -> None:
        self.tick(self.start + ESTIMATE_FACTOR * DEFAULT_ESTIMATE_MINUTES * 60 + 1)
        [event] = self.runtime.writer.events
        plain = {**event, "payload": {"marker": "dispatcher", "body_sha256": "x"}}
        by_worker = {**event, "actor": {"role": "worker", "id": "worker"}}

        self.assertTrue(is_significant_card_event(event, linked_refs={REF}))
        self.assertFalse(is_significant_card_event(event, linked_refs={"ummanu-1"}))
        self.assertFalse(is_significant_card_event(plain, linked_refs={REF}))
        self.assertIsNone(stale_round_fact(plain))
        self.assertIsNone(stale_round_fact(by_worker))

    def test_the_wake_context_names_the_stale_card_and_its_facts(self) -> None:
        self.tick(self.start + ESTIMATE_FACTOR * DEFAULT_ESTIMATE_MINUTES * 60 + 1)
        [event] = self.runtime.writer.events

        text = render_observer_wake_context(
            {"ref": "sprint:77", "comments": []}, change="linked-card", stale_round=[stale_round_fact(event)]
        )

        self.assertIn("Stale round decision:", text)
        self.assertIn(
            f"Stale round on {REF}: rule estimate, report generation 1."
            f" Elapsed {ESTIMATE_FACTOR * DEFAULT_ESTIMATE_MINUTES} min; estimate {DEFAULT_ESTIMATE_MINUTES} min;"
            " 2 commits, 3 files, +40/-7 lines, last change 2023-11-14T22:28:20Z.",
            text,
        )


if __name__ == "__main__":
    unittest.main()
