"""A head waiting on its own working child process is not a stall (secretary-1692).

Each layer against the thing it claims:

* the ``/proc`` reader and the ``execution_child`` snapshot builder, against real short-lived
  child processes (a busy loop, a sleep) spawned under a stand-in head in a temp dir;
* the reducer, tick by tick with an explicit clock: a moving child holds a quiet head healthy
  inside ``child_activity_ceiling``, and every other shape -- frozen counters, a child past the
  ceiling, an exited child -- runs the ordinary quiet ladder;
* the respawn note's source, ``interrupted_command_note``; the respawn itself is driven through
  the dispatcher in ``test_head_vitality_child_respawn``.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from ummanu.dispatch.head_vitality import (
    CHILD_CPU_ADVANCE_MS,
    ProcessState,
    ProgressState,
    SnapshotSource,
    SourceAvailability,
    VitalitySnapshot,
    snapshots_from_status,
)
from ummanu.dispatch.head_vitality_episode import (
    CHILD_ACTIVITY_CEILING_DEFAULT,
    DEFAULT_VITALITY_THRESHOLDS,
    VitalityEpisode,
    VitalityThresholds,
    VitalityVerdict,
    interrupted_command_note,
    reduce_vitality,
)
from ummanu.dispatch.head_vitality_guard import assert_destructive_allowed
from ummanu.runtime.head.children import COMMAND_LIMIT, read_head_children

RUN_ID = "run-child"
THRESHOLDS = VitalityThresholds(suspect_after=300.0, confirm_after=600.0)
CEILING = THRESHOLDS.child_activity_ceiling
STALLS = (VitalityVerdict.SUSPECTED_STALL, VitalityVerdict.CONFIRMED_STALL)


def heartbeat(observed_at: float) -> VitalitySnapshot:
    return VitalitySnapshot(
        run_id=RUN_ID,
        source=SnapshotSource.PID_HEARTBEAT,
        observed_at=observed_at,
        availability=SourceAvailability.AVAILABLE,
        process=ProcessState.RUNNING,
    )


def quiet_provider(observed_at: float) -> VitalitySnapshot:
    return VitalitySnapshot(
        run_id=RUN_ID,
        source=SnapshotSource.PROVIDER_CURSOR,
        observed_at=observed_at,
        availability=SourceAvailability.AVAILABLE,
        progress=ProgressState.QUIET,
        cursor="12:abc",
    )


def advancing_provider(observed_at: float) -> VitalitySnapshot:
    return VitalitySnapshot(
        run_id=RUN_ID,
        source=SnapshotSource.PROVIDER_CURSOR,
        observed_at=observed_at,
        availability=SourceAvailability.AVAILABLE,
        progress=ProgressState.ADVANCING,
        cursor=f"{int(observed_at)}:moved",
    )


def evidence(*descendants: dict, uptime: int = 1_000_000) -> dict:
    return {"state": "observed", "head_pid": 4242, "uptime_ticks": uptime, "descendants": list(descendants)}


def child(cpu_ms: int, *, pid: int = 5000, start: int = 900_000, io: int = 0) -> dict:
    return {
        "pid": pid,
        "start": start,
        "cpu_ms": cpu_ms,
        "io": io,
        "command": "timeout 580 python -m pytest tests/integration -k shard1",
        "output": "/tmp/shard1.log",
    }


class ChildScenario:
    """Drive the reducer one 60 s tick at a time, building child snapshots with the real builder.

    The previous child cursor and described key are read back from the episode exactly as the
    wait tick does, so the builder and reducer are exercised together.
    """

    def __init__(self, thresholds: VitalityThresholds = THRESHOLDS) -> None:
        self.thresholds = thresholds
        self.episode: VitalityEpisode | None = None
        self.verdicts: list[tuple[float, VitalityVerdict]] = []

    def tick(
        self, now: float, *, child_evidence: dict | None, provider_advances: bool = False
    ) -> VitalityEpisode:
        snapshots = [heartbeat(now), advancing_provider(now) if provider_advances else quiet_provider(now)]
        if child_evidence is not None:
            previous = self.episode
            snapshots.append(
                VitalitySnapshot.from_child_activity(
                    child_evidence,
                    run_id=RUN_ID,
                    previous_cursor=(
                        previous.evidence_cursors.get(SnapshotSource.EXECUTION_CHILD.value, "")
                        if previous is not None
                        else ""
                    ),
                    previous_key=previous.last_child_key if previous is not None else "",
                    observed_at=now,
                )
            )
        self.episode = reduce_vitality(self.episode, snapshots, now, self.thresholds)
        self.verdicts.append((now, self.episode.verdict))
        return self.episode

    def first_at(self, verdict: VitalityVerdict) -> float | None:
        return next((at for at, seen in self.verdicts if seen is verdict), None)


class ReducerTests(unittest.TestCase):
    def test_a_moving_child_holds_a_quiet_head_healthy_until_the_ceiling(self) -> None:
        """The secretary-1665 shape: quiet provider, live child burning CPU, 968 s and beyond."""
        scenario = ChildScenario()
        cpu = 0
        now = 0.0
        while now < CEILING:
            scenario.tick(now, child_evidence=evidence(child(cpu)))
            cpu += 30_000
            now += 60.0
        before_ceiling = [verdict for at, verdict in scenario.verdicts if at < CEILING]
        self.assertFalse(set(before_ceiling) & set(STALLS), scenario.verdicts)
        # The first reading has nothing to compare against; every later one is advancement.
        self.assertTrue(all(verdict is VitalityVerdict.HEALTHY_ACTIVE for verdict in before_ceiling[1:]))
        assert scenario.episode is not None
        self.assertIn("advancing@execution_child", scenario.episode.basis)
        # Child progress is not the head's own work: its progress history is untouched.
        self.assertEqual(scenario.episode.last_progress_at, 0.0)
        self.assertEqual(scenario.episode.activity_epoch, 0)
        # And the destructive guard refuses on it.
        self.assertFalse(assert_destructive_allowed(scenario.episode, "worker-respawn", now).allowed)

    def test_a_child_with_frozen_counters_runs_the_ordinary_ladder(self) -> None:
        with_child = ChildScenario()
        without_child = ChildScenario()
        for step in range(0, 20):
            now = step * 60.0
            with_child.tick(now, child_evidence=evidence(child(12_000)))
            without_child.tick(now, child_evidence=None)
        self.assertEqual(
            [verdict for _at, verdict in with_child.verdicts],
            [verdict for _at, verdict in without_child.verdicts],
        )
        self.assertEqual(with_child.first_at(VitalityVerdict.SUSPECTED_STALL), 300.0)
        self.assertEqual(with_child.first_at(VitalityVerdict.CONFIRMED_STALL), 900.0)
        # A live child is named to a successor even though it never moved (round 2,
        # BLOCKER-OMITTED-INTERRUPTED-COMMAND): with no mover, the youngest live descendant.
        assert with_child.episode is not None
        self.assertEqual(
            interrupted_command_note(with_child.episode, RUN_ID),
            "The previous head was stopped while running: timeout 580 python -m pytest "
            "tests/integration -k shard1 (its output was redirected to /tmp/shard1.log)",
        )

    def test_with_no_mover_the_youngest_live_descendant_is_named(self) -> None:
        helper = dict(child(4000, pid=100, start=10), command="node mcp-server.js", output="")
        tool = dict(child(10, pid=7000, start=950_000), command="sleep 3600", output="")
        scenario = ChildScenario()
        for step in range(0, 3):
            scenario.tick(step * 60.0, child_evidence=evidence(helper, tool))
        assert scenario.episode is not None
        self.assertEqual(scenario.episode.last_child_command, "sleep 3600")
        self.assertTrue(scenario.episode.last_child_key.startswith("y:"))

    def test_a_measured_mover_is_preferred_over_a_younger_idle_process(self) -> None:
        scenario = ChildScenario()
        worker = dict(child(0, pid=5000, start=900_000), command="pytest -n 0")
        idle = dict(child(5, pid=6000, start=950_000), command="sleep 3600", output="")
        scenario.tick(0.0, child_evidence=evidence(worker, idle))
        scenario.tick(60.0, child_evidence=evidence(dict(worker, cpu_ms=30_000), idle))
        scenario.tick(120.0, child_evidence=evidence(dict(worker, cpu_ms=30_000), idle))
        assert scenario.episode is not None
        self.assertEqual(scenario.episode.last_child_command, "pytest -n 0")
        self.assertTrue(scenario.episode.last_child_key.startswith("m:"))

    def test_a_child_that_worked_and_then_froze_stays_named_while_it_lives(self) -> None:
        scenario = ChildScenario()
        scenario.tick(0.0, child_evidence=evidence(child(0)))
        scenario.tick(60.0, child_evidence=evidence(child(30_000)))
        for step in range(2, 20):
            scenario.tick(step * 60.0, child_evidence=evidence(child(30_000)))
        assert scenario.episode is not None
        self.assertEqual(scenario.first_at(VitalityVerdict.CONFIRMED_STALL), 60.0 + 900.0)
        self.assertEqual(
            interrupted_command_note(scenario.episode, RUN_ID),
            "The previous head was stopped while running: timeout 580 python -m pytest "
            "tests/integration -k shard1 (its output was redirected to /tmp/shard1.log)",
        )
        self.assertEqual(interrupted_command_note(scenario.episode, "another-run"), "")

    def test_a_child_spinning_past_the_ceiling_is_still_caught(self) -> None:
        scenario = ChildScenario()
        cpu = 0
        for step in range(0, int((CEILING + 1200) // 60) + 1):
            scenario.tick(step * 60.0, child_evidence=evidence(child(cpu)))
            cpu += 30_000
        suspected = scenario.first_at(VitalityVerdict.SUSPECTED_STALL)
        confirmed = scenario.first_at(VitalityVerdict.CONFIRMED_STALL)
        assert suspected is not None and confirmed is not None
        self.assertGreaterEqual(suspected, CEILING)
        self.assertLessEqual(suspected, CEILING + THRESHOLDS.suspect_after)
        self.assertLessEqual(confirmed, CEILING + THRESHOLDS.suspect_after + THRESHOLDS.confirm_after)
        assert scenario.episode is not None
        self.assertTrue(any(token.startswith("child-ceiling:") for token in scenario.episode.basis))
        self.assertTrue(assert_destructive_allowed(scenario.episode, "worker-respawn", confirmed + 1).allowed)

    def test_an_exited_child_hands_back_to_the_ordinary_quiet_rules(self) -> None:
        scenario = ChildScenario()
        cpu = 0
        for step in range(0, 21):  # a child working 0..1200 s
            scenario.tick(step * 60.0, child_evidence=evidence(child(cpu)))
            cpu += 30_000
        self.assertIs(scenario.verdicts[-1][1], VitalityVerdict.HEALTHY_ACTIVE)
        for step in range(21, 40):  # the child is gone; the head stays silent
            scenario.tick(step * 60.0, child_evidence=evidence())
        # Quiet is measured from the last accepted child advancement, like any other progress.
        self.assertEqual(scenario.first_at(VitalityVerdict.SUSPECTED_STALL), 1200.0 + 300.0)
        self.assertEqual(scenario.first_at(VitalityVerdict.CONFIRMED_STALL), 1200.0 + 900.0)
        assert scenario.episode is not None
        # No live child is described any more, so a respawn names none.
        self.assertEqual(scenario.episode.last_child_command, "")
        self.assertEqual(interrupted_command_note(scenario.episode, RUN_ID), "")

    def test_the_heads_own_progress_restarts_the_ceiling(self) -> None:
        scenario = ChildScenario()
        cpu = 0
        for step in range(0, int((CEILING + 1500) // 60)):
            now = step * 60.0
            scenario.tick(now, child_evidence=evidence(child(cpu)), provider_advances=(now == 1800.0))
            cpu += 30_000
        # The streak restarted at 1800 s, so the ceiling runs to 1800 + CEILING, past this window.
        self.assertFalse({verdict for _at, verdict in scenario.verdicts} & set(STALLS))

    def test_an_unobserved_child_source_changes_nothing(self) -> None:
        """No child reading at all (a host without the probe): the pre-card ladder, exactly."""
        scenario = ChildScenario()
        for step in range(0, 16):
            scenario.tick(step * 60.0, child_evidence=None)
        self.assertEqual(scenario.first_at(VitalityVerdict.SUSPECTED_STALL), 300.0)
        self.assertEqual(scenario.first_at(VitalityVerdict.CONFIRMED_STALL), 900.0)

    def test_the_default_ceiling_is_argued_not_borrowed(self) -> None:
        self.assertEqual(DEFAULT_VITALITY_THRESHOLDS.child_activity_ceiling, CHILD_ACTIVITY_CEILING_DEFAULT)
        self.assertGreaterEqual(CHILD_ACTIVITY_CEILING_DEFAULT, 45 * 60)
        self.assertLessEqual(CHILD_ACTIVITY_CEILING_DEFAULT, 60 * 60)


class DurableFormatTests(unittest.TestCase):
    def _child_episode(self) -> VitalityEpisode:
        scenario = ChildScenario()
        scenario.tick(0.0, child_evidence=evidence(child(0)))
        return scenario.tick(60.0, child_evidence=evidence(child(30_000)))

    def test_an_episode_written_before_the_child_fields_loads(self) -> None:
        payload = self._child_episode().to_json()
        for name in (
            "child_progress_at",
            "child_activity_since",
            "last_child_key",
            "last_child_command",
            "last_child_output",
            "last_child_at",
        ):
            payload.pop(name)
        loaded = VitalityEpisode.from_json(payload)
        self.assertEqual(loaded.child_progress_at, 0.0)
        self.assertEqual(loaded.child_activity_since, 0.0)
        self.assertEqual(loaded.last_child_command, "")
        # And it keeps reducing.
        reduce_vitality(loaded, [heartbeat(120.0), quiet_provider(120.0)], 120.0, THRESHOLDS)

    def test_the_new_fields_are_additive_and_round_trip(self) -> None:
        episode = self._child_episode()
        self.assertEqual(VitalityEpisode.from_json(episode.to_json()), episode)
        self.assertGreater(episode.child_progress_at, 0.0)
        self.assertIn("pytest", episode.last_child_command)
        # Every key an episode carried before this card is still written with its old meaning,
        # so a dispatcher from before it (whose from_json reads named keys only) reads this one.
        self.assertLessEqual(
            {
                "version",
                "run_id",
                "verdict",
                "started_at",
                "suspected_since",
                "confirmed_since",
                "last_progress_at",
                "last_progress_source",
                "quiet_since",
                "evidence_cursors",
                "unavailable_since",
                "basis",
                "reason",
                "recovery_rung",
                "recovery_span_started_at",
                "deterministic_refusals",
                "activity_epoch",
                "updated_at",
                "stall_frozen_since",
                "last_turn",
                "turn_ended_at",
            },
            set(episode.to_json()),
        )
        self.assertEqual(episode.to_json()["version"], 1)

    def test_a_snapshot_without_child_fields_loads(self) -> None:
        payload = quiet_provider(1.0).to_json()
        for name in ("child_key", "command", "output_path"):
            payload.pop(name)
        self.assertEqual(VitalitySnapshot.from_json(payload), quiet_provider(1.0))


class BuilderTests(unittest.TestCase):
    def test_an_unreadable_reading_is_unavailable_not_quiet(self) -> None:
        snapshot = VitalitySnapshot.from_child_activity(
            {"state": "unavailable", "reason": "head pid is not running"}, run_id=RUN_ID, observed_at=1.0
        )
        self.assertIs(snapshot.availability, SourceAvailability.UNAVAILABLE)
        self.assertIs(snapshot.progress, ProgressState.UNKNOWN)

    def test_idle_helpers_below_the_threshold_are_quiet(self) -> None:
        first = VitalitySnapshot.from_child_activity(evidence(child(1000)), run_id=RUN_ID, observed_at=1.0)
        second = VitalitySnapshot.from_child_activity(
            evidence(child(1000 + CHILD_CPU_ADVANCE_MS - 10)),
            run_id=RUN_ID,
            previous_cursor=first.cursor or "",
            observed_at=2.0,
        )
        self.assertIs(second.progress, ProgressState.QUIET)

    def test_a_process_born_after_the_previous_reading_counts_whole(self) -> None:
        first = VitalitySnapshot.from_child_activity(evidence(uptime=1000), run_id=RUN_ID, observed_at=1.0)
        second = VitalitySnapshot.from_child_activity(
            evidence(child(2000, start=1500), uptime=2000),
            run_id=RUN_ID,
            previous_cursor=first.cursor or "",
            observed_at=2.0,
        )
        self.assertIs(second.progress, ProgressState.ADVANCING)
        self.assertIn("pytest", second.command)

    def test_an_aggregate_that_goes_down_is_no_advancement(self) -> None:
        """A descendant that died unreaped takes its CPU out of the tree: quiet, not an error."""
        busy = dict(child(90_000), pid=5000)
        other = dict(child(1_000), pid=6000, start=950_000)
        first = VitalitySnapshot.from_child_activity(evidence(busy, other), run_id=RUN_ID, observed_at=1.0)
        second = VitalitySnapshot.from_child_activity(
            evidence(dict(other, cpu_ms=1_400)),
            run_id=RUN_ID,
            previous_cursor=first.cursor or "",
            observed_at=2.0,
        )
        self.assertIs(second.progress, ProgressState.QUIET)
        # And the next reading measures from the lower aggregate, not from the old one.
        third = VitalitySnapshot.from_child_activity(
            evidence(dict(other, cpu_ms=1_400 + CHILD_CPU_ADVANCE_MS)),
            run_id=RUN_ID,
            previous_cursor=second.cursor or "",
            observed_at=3.0,
        )
        self.assertIs(third.progress, ProgressState.ADVANCING)

    def test_movement_is_the_whole_tree_aggregate_not_the_listed_subset(self) -> None:
        """An aggregate over processes the reading does not describe still counts."""
        idle = dict(child(10), pid=6000, start=950_000)
        first = VitalitySnapshot.from_child_activity(
            dict(evidence(idle), total_cpu_ms=100_000, total_io=0), run_id=RUN_ID, observed_at=1.0
        )
        second = VitalitySnapshot.from_child_activity(
            dict(evidence(idle), total_cpu_ms=100_000 + CHILD_CPU_ADVANCE_MS, total_io=0),
            run_id=RUN_ID,
            previous_cursor=first.cursor or "",
            observed_at=2.0,
        )
        self.assertIs(second.progress, ProgressState.ADVANCING)

    def test_the_status_mapping_carries_the_child_source(self) -> None:
        snapshots = snapshots_from_status(
            {"child_activity": evidence(child(1))}, run_id=RUN_ID, observed_at=1.0
        )
        self.assertEqual([snapshot.source for snapshot in snapshots], [SnapshotSource.EXECUTION_CHILD])


def _stop_group(head: subprocess.Popen) -> None:
    """Kill a stand-in head's whole process group, then reap the head.

    Each stand-in starts in its own session, so the group holds every descendant that did not
    leave it -- more than ``read_head_children`` lists -- and none survives to hold the runner's
    pipe open (secretary-1695)."""
    try:
        os.killpg(head.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    head.wait(timeout=10)


class RealProcessTests(unittest.TestCase):
    """The reader and builder against real processes under a stand-in head."""

    def setUp(self) -> None:
        if not Path("/proc/self/stat").exists():
            self.skipTest("needs Linux /proc")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _head(self, child_code: str, *, output: Path | None = None, extra: str = "") -> subprocess.Popen:
        """A stand-in head process whose one child runs ``child_code``.

        The child's stdout is ``output`` when given and ``/dev/null`` otherwise, never the
        runner's own stdout: a runner writing to a regular file would otherwise be named as the
        child's output file (secretary-1694).
        """
        redirect = f"stdout=open({str(output)!r}, 'w')" if output is not None else "stdout=subprocess.DEVNULL"
        head_code = (
            "import subprocess, sys; "
            f"subprocess.run([sys.executable, '-c', {child_code!r}] + {extra.split()!r}, {redirect})"
        )
        head = subprocess.Popen(
            [sys.executable, "-c", head_code],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=self.tmp.name,
            start_new_session=True,
        )
        self.addCleanup(_stop_group, head)
        deadline = time.time() + 10
        while time.time() < deadline:
            if read_head_children(head.pid).get("descendants"):
                break
            time.sleep(0.05)
        return head

    def _two_readings(self, head: subprocess.Popen, gap: float) -> tuple[VitalitySnapshot, VitalitySnapshot]:
        first = VitalitySnapshot.from_child_activity(
            read_head_children(head.pid), run_id=RUN_ID, observed_at=time.time()
        )
        time.sleep(gap)
        second = VitalitySnapshot.from_child_activity(
            read_head_children(head.pid),
            run_id=RUN_ID,
            previous_cursor=first.cursor or "",
            previous_key=first.child_key,
            observed_at=time.time(),
        )
        return first, second

    def test_a_busy_child_is_advancement_with_its_redacted_command_and_output_file(self) -> None:
        log = Path(self.tmp.name) / "run.log"
        head = self._head(
            "import sys\nwhile True: pass", output=log, extra="API_TOKEN=abcdef0123456789secret"
        )
        reading = read_head_children(head.pid)
        self.assertEqual(reading["state"], "observed")
        (described,) = reading["descendants"]
        self.assertIn("while True", described["command"])
        self.assertNotIn("abcdef0123456789secret", described["command"])
        self.assertLessEqual(len(described["command"]), COMMAND_LIMIT)
        self.assertEqual(described["output"], str(log))

        _first, second = self._two_readings(head, 1.2)
        self.assertIs(second.progress, ProgressState.ADVANCING)
        self.assertIn("while True", second.command)
        self.assertEqual(second.output_path, str(log))

    def test_a_sleeping_child_is_quiet_but_still_described(self) -> None:
        head = self._head("import time; time.sleep(60)")
        first, second = self._two_readings(head, 1.0)
        self.assertIs(first.progress, ProgressState.UNKNOWN)
        self.assertIs(second.progress, ProgressState.QUIET)
        # Quiet, and still described: the youngest live descendant (round 2).
        self.assertTrue(second.child_key.startswith("y:"))
        self.assertIn("time.sleep(60)", second.command)
        self.assertEqual(second.output_path, "")

    def test_an_older_busy_child_under_many_newer_idle_sleepers_is_advancing(self) -> None:
        """Reviewer reproduction for BLOCKER-UNSEEN-WORKING-DESCENDANT: one busy loop started
        first, then 17 idle sleepers; the busy one is older than every sleeper."""
        code = (
            "import subprocess, sys, time\n"
            "busy = subprocess.Popen([sys.executable, '-c', 'while True: pass'])\n"
            "time.sleep(0.3)\n"
            "sleepers = [subprocess.Popen(['sleep', '60']) for _ in range(17)]\n"
            "busy.wait()\n"
        )
        head = subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=self.tmp.name,
            start_new_session=True,
        )
        self.addCleanup(_stop_group, head)
        deadline = time.time() + 15
        while time.time() < deadline and read_head_children(head.pid).get("descendant_count", 0) < 18:
            time.sleep(0.05)
        reading = read_head_children(head.pid)
        self.assertEqual(reading["descendant_count"], 18)
        self.assertLessEqual(len(reading["descendants"]), 16)
        _first, second = self._two_readings(head, 1.2)
        self.assertIs(second.progress, ProgressState.ADVANCING)
        self.assertIn("while True", second.command)

    def _respawn_note_of_a_sleeping_child(self, stdout: Any) -> str:
        """A live ``sleep 3600`` that never crosses the noise floor, reduced to ConfirmedStall at
        900 s; answers the respawn note. ``stdout`` is the child's, set explicitly (secretary-1694)."""
        head = subprocess.Popen(
            ["sh", "-c", "sleep 3600; true"],
            stdout=stdout,
            stderr=subprocess.DEVNULL,
            cwd=self.tmp.name,
            start_new_session=True,
        )
        self.addCleanup(_stop_group, head)
        deadline = time.time() + 10
        while time.time() < deadline and not read_head_children(head.pid).get("descendants"):
            time.sleep(0.05)
        scenario_episode = None
        cursor, key = "", ""
        for now in (0.0, 60.0, 900.0):
            snapshot = VitalitySnapshot.from_child_activity(
                read_head_children(head.pid),
                run_id=RUN_ID,
                previous_cursor=cursor,
                previous_key=key,
                observed_at=now,
            )
            scenario_episode = reduce_vitality(
                scenario_episode, [heartbeat(now), quiet_provider(now), snapshot], now, THRESHOLDS
            )
            cursor = scenario_episode.evidence_cursors.get(SnapshotSource.EXECUTION_CHILD.value, "")
            key = scenario_episode.last_child_key
        assert scenario_episode is not None
        self.assertIs(scenario_episode.verdict, VitalityVerdict.CONFIRMED_STALL)
        return interrupted_command_note(scenario_episode, RUN_ID)

    def test_two_quiet_readings_of_a_sleeping_child_leave_a_respawn_note(self) -> None:
        """Reviewer reproduction for BLOCKER-OMITTED-INTERRUPTED-COMMAND, stdout discarded."""
        self.assertEqual(
            self._respawn_note_of_a_sleeping_child(subprocess.DEVNULL),
            "The previous head was stopped while running: sleep 3600",
        )

    def test_the_respawn_note_names_the_sleeping_childs_output_file(self) -> None:
        log = Path(self.tmp.name) / "sleep.log"
        with log.open("w", encoding="utf-8") as stdout:
            note = self._respawn_note_of_a_sleeping_child(stdout)
        self.assertEqual(
            note,
            f"The previous head was stopped while running: sleep 3600 (its output was redirected to {log})",
        )

    def test_a_head_without_children_and_a_gone_head(self) -> None:
        lonely = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.addCleanup(_stop_group, lonely)
        self.assertEqual(read_head_children(lonely.pid)["descendants"], [])
        gone = subprocess.Popen(
            [sys.executable, "-c", "pass"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        gone.wait(timeout=10)
        self.assertEqual(read_head_children(gone.pid)["state"], "unavailable")


if __name__ == "__main__":
    unittest.main()
