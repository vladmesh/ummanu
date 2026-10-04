"""Head-vitality observation vocabulary: mappings, identity fencing and serialisation.

The fixtures reuse the producers' own helpers wherever one exists —
``dispatcher_watchdog.head_process_status`` against real heartbeat files,
``head.command.with_pid_heartbeat`` for a live process — so a producer shape that drifts fails
here instead of silently redefining what an observation means.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from ummanu.dispatch.head_vitality import (
    CURSOR_LIMIT,
    REASON_LIMIT,
    HeadVitalityError,
    ProcessState,
    ProgressState,
    SnapshotSource,
    SourceAvailability,
    TurnState,
    VitalitySnapshot,
)
from ummanu.dispatch.watchdog import (
    HEARTBEAT_DEAD,
    HEARTBEAT_LIVE_MATCH,
    head_process_status,
)
from ummanu.runtime.head import identity, with_pid_heartbeat

RUN_ID = "run-1"


def _heartbeat_identity(run_id: str = RUN_ID) -> dict[str, str]:
    """The non-kernel facts a heartbeat writer embeds, as the launcher spells them."""
    return {"run_id": run_id, "role": "worker", "task": "card:1194", "leaf": "leaf-1"}


def _write_live_heartbeat(directory: str, name: str, *, run_id: str = RUN_ID) -> tuple[str, subprocess.Popen]:
    """Launch a real sleeping process behind a real heartbeat file, like a launch does."""
    pid_file = os.path.join(directory, name)
    wrapped = with_pid_heartbeat("sleep 30", pid_file, identity=_heartbeat_identity(run_id))
    proc = subprocess.Popen(["/bin/sh", "-lc", wrapped])
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not (
        os.path.exists(pid_file) and head_process_status(pid_file)["state"] == HEARTBEAT_LIVE_MATCH
    ):
        time.sleep(0.01)
    return pid_file, proc


class SnapshotConstructionTests(unittest.TestCase):
    def test_a_snapshot_is_bound_to_a_named_run(self) -> None:
        with self.assertRaises(HeadVitalityError):
            VitalitySnapshot(
                run_id="",
                source=SnapshotSource.PID_HEARTBEAT,
                observed_at=1000.0,
                availability=SourceAvailability.AVAILABLE,
            )

    def test_the_reason_stays_bounded(self) -> None:
        snapshot = VitalitySnapshot(
            run_id=RUN_ID,
            source=SnapshotSource.PID_HEARTBEAT,
            observed_at=1000.0,
            availability=SourceAvailability.UNAVAILABLE,
            reason="x" * 5000,
        )
        self.assertEqual(len(snapshot.reason), REASON_LIMIT)


class PidHeartbeatTests(unittest.TestCase):
    """Every ``head_process_status`` classification maps onto the Process axis."""

    def setUp(self) -> None:
        self.directory = tempfile.mkdtemp()

    def tearDown(self) -> None:
        # The suite's processes are short sleeps; anything still alive is reaped here.
        for proc in getattr(self, "_processes", []):
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def track(self, proc: subprocess.Popen) -> subprocess.Popen:
        self._processes = getattr(self, "_processes", []) + [proc]
        return proc

    def test_a_live_matching_process_is_running(self) -> None:
        pid_file, proc = _write_live_heartbeat(self.directory, "live.pid")
        self.track(proc)

        snapshot = VitalitySnapshot.from_pid_heartbeat(
            head_process_status(pid_file, expected=_heartbeat_identity()),
            run_id=RUN_ID,
            observed_at=1000.0,
        )

        self.assertEqual(snapshot.process, ProcessState.RUNNING)
        self.assertEqual(snapshot.availability, SourceAvailability.AVAILABLE)
        self.assertEqual(snapshot.source, SnapshotSource.PID_HEARTBEAT)

    def test_a_sigstopped_process_is_suspended_not_dead_and_not_unknown(self) -> None:
        pid_file, proc = _write_live_heartbeat(self.directory, "stopped.pid")
        self.track(proc)
        proc.send_signal(signal.SIGSTOP)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            # The signal is delivered asynchronously; the classification is only honest once
            # /proc itself reports the process parked in `T`.
            with open(f"/proc/{proc.pid}/status", encoding="utf-8") as handle:
                parked = any(line.startswith("State:") and "\tT" in line for line in handle)
            if parked:
                break
            time.sleep(0.01)
        status = head_process_status(pid_file, expected=_heartbeat_identity())
        try:
            self.assertTrue(status["stopped"])
        finally:
            proc.send_signal(signal.SIGCONT)

        snapshot = VitalitySnapshot.from_pid_heartbeat(status, run_id=RUN_ID, observed_at=1000.0)

        # The six-hour-ceiling incident was exactly this state misread: suspended is its own fact.
        self.assertEqual(snapshot.process, ProcessState.SUSPENDED)

    def test_a_gone_process_is_dead(self) -> None:
        pid_file, proc = _write_live_heartbeat(self.directory, "gone.pid")
        proc.kill()
        proc.wait()
        status = head_process_status(pid_file, expected=_heartbeat_identity())
        self.assertEqual(status["state"], HEARTBEAT_DEAD)

        snapshot = VitalitySnapshot.from_pid_heartbeat(status, run_id=RUN_ID, observed_at=1000.0)

        self.assertEqual(snapshot.process, ProcessState.DEAD)
        self.assertEqual(snapshot.availability, SourceAvailability.AVAILABLE)

    def test_a_process_reaped_between_the_two_proc_reads_is_dead_and_never_live(self) -> None:
        """The window CI found: `kill(pid, 0)` answers, and the process is gone a syscall later.

        `head_process_status` signals the pid, reads its start time, then asks what the process is.
        A head reaped between the second and the third of those leaves no `/proc/<pid>/status` at
        all, and reading that absence as "not a zombie" published `live-match` for a launch that had
        already exited -- which is what the control plane reads as "this head is still running". The
        window is a few microseconds wide, so it is staged here rather than waited for: the process
        really is reaped, and the two reads that happened before it are the ones being simulated.
        """
        pid_file, proc = _write_live_heartbeat(self.directory, "reaped.pid")
        with open(pid_file, encoding="utf-8") as handle:
            ticks = json.load(handle)["proc_starttime_ticks"]
        proc.kill()
        proc.wait()
        self.assertEqual(identity._process_state(proc.pid), "gone")

        with (
            mock.patch.object(identity.os, "kill", return_value=None),
            mock.patch.object(identity, "_proc_starttime_ticks", return_value=ticks),
        ):
            status = head_process_status(pid_file, expected=_heartbeat_identity())

        self.assertEqual(status["state"], HEARTBEAT_DEAD, status)
        snapshot = VitalitySnapshot.from_pid_heartbeat(status, run_id=RUN_ID, observed_at=1000.0)
        self.assertEqual(snapshot.process, ProcessState.DEAD)

    def test_a_process_reaped_before_its_start_time_is_read_is_dead_not_unreadable(self) -> None:
        """The same window one read earlier (CI, 2026-10-04): `kill(pid, 0)` answers a zombie, the
        supervisor reaps it, and `/proc/<pid>/stat` is gone by the time the start time is read.
        That absence is the process having ended, not a status nobody could read. Staged as above:
        the process really is reaped, and only the signal that preceded it is simulated.
        """
        pid_file, proc = _write_live_heartbeat(self.directory, "reaped-early.pid")
        proc.kill()
        proc.wait()

        with mock.patch.object(identity.os, "kill", return_value=None):
            status = head_process_status(pid_file, expected=_heartbeat_identity())

        self.assertEqual(status["state"], HEARTBEAT_DEAD, status)

    def test_a_missing_pid_file_is_unavailable_never_dead(self) -> None:
        status = head_process_status(os.path.join(self.directory, "never-written.pid"))

        snapshot = VitalitySnapshot.from_pid_heartbeat(status, run_id=RUN_ID, observed_at=1000.0)

        self.assertEqual(snapshot.process, ProcessState.UNKNOWN)
        self.assertEqual(snapshot.availability, SourceAvailability.UNAVAILABLE)
        self.assertIn("not-yet-written", snapshot.reason)

    def test_an_identity_mismatch_is_unavailable_and_never_dead_or_running(self) -> None:
        pid_file, proc = _write_live_heartbeat(self.directory, "foreign.pid", run_id="run-other")
        self.track(proc)

        snapshot = VitalitySnapshot.from_pid_heartbeat(
            head_process_status(pid_file, expected=_heartbeat_identity(RUN_ID)),
            run_id=RUN_ID,
            observed_at=1000.0,
        )

        # "Some other run's process is alive there" proves nothing about this run.
        self.assertEqual(snapshot.process, ProcessState.UNKNOWN)
        self.assertEqual(snapshot.availability, SourceAvailability.UNAVAILABLE)
        self.assertIn("another", snapshot.reason)

    def test_a_malformed_status_value_cannot_be_read_as_a_fact(self) -> None:
        snapshot = VitalitySnapshot.from_pid_heartbeat(
            ["not", "a", "dict"], run_id=RUN_ID, observed_at=1000.0
        )

        self.assertEqual(
            (snapshot.process, snapshot.availability), (ProcessState.UNKNOWN, SourceAvailability.UNAVAILABLE)
        )


class ProviderCursorTests(unittest.TestCase):
    """Cursor movement maps onto the Progress axis; everything else stays unavailable."""

    def admitted_evidence(self, cursor: str, **overrides: str) -> dict[str, str]:
        evidence = {
            "state": "observed",
            "admission": "accepted",
            "source": "codex-session",
            "source_fingerprint": "f" * 32,
            "cursor": cursor,
            "observed_at": "1000.0",
            "head_run_id": RUN_ID,
            "head_run_fingerprint": "a" * 32,
        }
        evidence.update(overrides)
        return evidence

    def test_a_moved_cursor_is_advancing_with_the_new_cursor_recorded(self) -> None:
        snapshot = VitalitySnapshot.from_provider_cursor(
            self.admitted_evidence("12:abc"),
            run_id=RUN_ID,
            previous_cursor="11:aaa",
            observed_at=1001.0,
        )

        self.assertEqual(snapshot.progress, ProgressState.ADVANCING)
        self.assertEqual(snapshot.cursor, "12:abc")
        self.assertEqual(snapshot.availability, SourceAvailability.AVAILABLE)

    def test_one_unchanged_cursor_is_quiet_and_never_stagnant(self) -> None:
        snapshot = VitalitySnapshot.from_provider_cursor(
            self.admitted_evidence("12:abc"),
            run_id=RUN_ID,
            previous_cursor="12:abc",
            observed_at=1002.0,
        )

        # Stagnation is the reducer's conclusion over time; one reading cannot see it.
        self.assertEqual(snapshot.progress, ProgressState.QUIET)
        self.assertNotEqual(snapshot.progress, ProgressState.STAGNANT)
        self.assertEqual(snapshot.cursor, "12:abc")

    def test_the_first_observation_of_a_source_has_no_progress_opinion_yet(self) -> None:
        snapshot = VitalitySnapshot.from_provider_cursor(
            self.admitted_evidence("12:abc"), run_id=RUN_ID, previous_cursor="", observed_at=1000.0
        )

        # Without an earlier cursor nothing can be said about movement either way.
        self.assertEqual(snapshot.progress, ProgressState.UNKNOWN)
        self.assertEqual(snapshot.cursor, "12:abc")

    def test_an_unadmitted_reading_is_unavailable_and_not_quiet(self) -> None:
        snapshot = VitalitySnapshot.from_provider_cursor(
            {
                "state": "unavailable",
                "source": "codex-session",
                "reason": "Codex provider source has no bound v1 baseline",
            },
            previous_cursor="12:abc",
            observed_at=1003.0,
            run_id=RUN_ID,
        )

        self.assertEqual(snapshot.progress, ProgressState.UNKNOWN)
        self.assertEqual(snapshot.availability, SourceAvailability.UNAVAILABLE)

    def test_observed_but_not_admitted_evidence_is_unavailable_and_not_quiet(self) -> None:
        """The other half of the admission gate: a reading that says `observed` but was never
        admitted (`admission` missing or refused) is a channel problem too. Treating it as an
        answer would let unverified evidence vote Quiet and spend stall time."""
        snapshot = VitalitySnapshot.from_provider_cursor(
            {
                "state": "observed",
                "source": "codex-session",
                "cursor": "12:abc",
                "head_run_id": RUN_ID,
                "reason": "provider-progress source admission is incomplete",
            },
            run_id=RUN_ID,
            previous_cursor="12:abc",
            observed_at=1006.0,
        )

        self.assertEqual(snapshot.progress, ProgressState.UNKNOWN)
        self.assertEqual(snapshot.availability, SourceAvailability.UNAVAILABLE)
        self.assertIn("not admitted", snapshot.reason)

    def test_another_runs_cursor_is_fenced_as_unavailable(self) -> None:
        snapshot = VitalitySnapshot.from_provider_cursor(
            self.admitted_evidence("12:abc", head_run_id="run-2"),
            run_id=RUN_ID,
            previous_cursor="12:abc",
            observed_at=1004.0,
        )

        self.assertEqual(snapshot.progress, ProgressState.UNKNOWN)
        self.assertEqual(snapshot.availability, SourceAvailability.UNAVAILABLE)
        self.assertIn("other than", snapshot.reason)

    def test_an_admitted_answer_without_a_cursor_value_is_unavailable(self) -> None:
        snapshot = VitalitySnapshot.from_provider_cursor(
            self.admitted_evidence(""), run_id=RUN_ID, previous_cursor="12:abc", observed_at=1005.0
        )

        self.assertEqual(snapshot.progress, ProgressState.UNKNOWN)
        self.assertEqual(snapshot.availability, SourceAvailability.UNAVAILABLE)


class PaneReadinessTests(unittest.TestCase):
    """Pane answers fill the Turn axis only, are advisory, and never touch Process/Progress."""

    def test_a_ready_pane_reads_idle(self) -> None:
        snapshot = VitalitySnapshot.from_pane_readiness({"idle": True}, run_id=RUN_ID, observed_at=5.0)

        self.assertEqual(snapshot.turn, TurnState.IDLE)
        self.assertEqual(snapshot.availability, SourceAvailability.AVAILABLE)
        self.assertTrue(snapshot.advisory)
        self.assertEqual(snapshot.source, SnapshotSource.PANE_ADVISORY)

    def test_a_busy_pane_reads_active(self) -> None:
        snapshot = VitalitySnapshot.from_pane_readiness({"idle": False}, run_id=RUN_ID, observed_at=6.0)

        self.assertEqual(snapshot.turn, TurnState.ACTIVE)
        self.assertTrue(snapshot.advisory)

    def test_axes_outside_turn_stay_unknown_on_every_pane_answer(self) -> None:
        for status in ({"idle": True}, {"idle": False}):
            snapshot = VitalitySnapshot.from_pane_readiness(status, run_id=RUN_ID, observed_at=1.0)
            self.assertEqual(snapshot.process, ProcessState.UNKNOWN)
            self.assertEqual(snapshot.progress, ProgressState.UNKNOWN)

    def test_an_unanswerable_probe_is_unknown_and_advisory_still(self) -> None:
        snapshot = VitalitySnapshot.from_pane_readiness({}, run_id=RUN_ID, observed_at=0.0)

        self.assertEqual(snapshot.turn, TurnState.UNKNOWN)
        self.assertEqual(snapshot.availability, SourceAvailability.UNAVAILABLE)
        self.assertTrue(snapshot.advisory)


class AxisIndependenceTests(unittest.TestCase):
    def test_running_with_an_unknown_turn_round_trips_as_itself(self) -> None:
        snapshot = VitalitySnapshot.from_json(
            VitalitySnapshot.from_pid_heartbeat(
                {
                    "known": True,
                    "alive": True,
                    "match": True,
                    "state": HEARTBEAT_LIVE_MATCH,
                    "stopped": False,
                },
                run_id=RUN_ID,
                observed_at=1000.0,
            ).to_json()
        )

        # A pid heartbeat says nothing about turns; independence means that absence survives.
        self.assertEqual(snapshot.process, ProcessState.RUNNING)
        self.assertEqual(snapshot.turn, TurnState.UNKNOWN)
        self.assertEqual(snapshot.progress, ProgressState.UNKNOWN)

    def test_suspended_process_alongside_a_busy_advisory_pane_is_representable(self) -> None:
        pane = VitalitySnapshot.from_pane_readiness({"idle": False}, run_id=RUN_ID, observed_at=7.0)
        heartbeat = VitalitySnapshot.from_pid_heartbeat(
            {"known": True, "alive": True, "match": True, "state": HEARTBEAT_LIVE_MATCH, "stopped": True},
            run_id=RUN_ID,
            observed_at=8.0,
        )

        self.assertEqual(pane.turn, TurnState.ACTIVE)
        self.assertEqual(heartbeat.process, ProcessState.SUSPENDED)
        self.assertNotEqual(pane, heartbeat)


class UnavailableIsNotNoProgressTests(unittest.TestCase):
    """The invariant the sprint exists to prove: a broken channel freezes knowledge."""

    def test_every_failure_shape_keeps_progress_unknown(self) -> None:
        failures = [
            VitalitySnapshot.from_provider_cursor(None, run_id=RUN_ID, observed_at=1.0),
            VitalitySnapshot.from_pid_heartbeat({"state": "unreadable"}, run_id=RUN_ID, observed_at=1.0),
            VitalitySnapshot.from_pane_readiness("garbage", run_id=RUN_ID, observed_at=1.0),
        ]
        for snapshot in failures:
            with self.subTest(source=snapshot.source):
                self.assertEqual(snapshot.progress, ProgressState.UNKNOWN)
                self.assertEqual(snapshot.process, ProcessState.UNKNOWN)
                self.assertEqual(snapshot.turn, TurnState.UNKNOWN)
                self.assertEqual(snapshot.availability, SourceAvailability.UNAVAILABLE)
                self.assertTrue(snapshot.reason)

    def test_unavailable_snapshots_are_distinct_from_a_quiet_observation(self) -> None:
        unavailable = VitalitySnapshot.from_provider_cursor(None, run_id=RUN_ID, observed_at=1.0)
        quiet = VitalitySnapshot.from_provider_cursor(
            {
                "state": "observed",
                "admission": "accepted",
                "cursor": "9:xyz",
                "head_run_id": RUN_ID,
            },
            run_id=RUN_ID,
            previous_cursor="9:xyz",
            observed_at=2.0,
        )

        self.assertEqual(unavailable.progress, ProgressState.UNKNOWN)
        self.assertEqual(quiet.progress, ProgressState.QUIET)
        self.assertNotEqual(unavailable, quiet)


class SerialisationTests(unittest.TestCase):
    def round_trip(self, snapshot: VitalitySnapshot) -> VitalitySnapshot:
        payload = json.loads(json.dumps(snapshot.to_json()))
        return VitalitySnapshot.from_json(payload)

    def test_a_full_snapshot_round_trips_through_json_text(self) -> None:
        snapshot = VitalitySnapshot(
            run_id=RUN_ID,
            source=SnapshotSource.PROVIDER_CURSOR,
            observed_at=1234.5,
            availability=SourceAvailability.AVAILABLE,
            progress=ProgressState.ADVANCING,
            cursor="14:def",
            reason="",
        )

        self.assertEqual(self.round_trip(snapshot), snapshot)

    def test_a_minimal_snapshot_keeps_its_unset_axes_none_cursor_included(self) -> None:
        snapshot = VitalitySnapshot.from_pane_readiness({"idle": True}, run_id=RUN_ID, observed_at=3.0)

        restored = self.round_trip(snapshot)

        self.assertEqual(restored, snapshot)
        self.assertIsNone(restored.cursor)
        self.assertEqual(restored.to_json()["cursor"], None)

    def test_an_overlong_cursor_survives_a_round_trip_bounded(self) -> None:
        snapshot = VitalitySnapshot(
            run_id=RUN_ID,
            source=SnapshotSource.PROVIDER_CURSOR,
            observed_at=10.0,
            availability=SourceAvailability.AVAILABLE,
            progress=ProgressState.QUIET,
            cursor="c" * 2000,
        )

        self.assertEqual(len(self.round_trip(snapshot).cursor or ""), CURSOR_LIMIT)

    def test_from_json_refuses_payloads_that_change_meaning(self) -> None:
        base = VitalitySnapshot.from_pane_readiness({"idle": True}, run_id=RUN_ID, observed_at=1.0).to_json()
        cases = [
            {**base, "version": 99},
            {**base, "version": "1"},
            "not-an-object",
            {**base, "source": "telemetry-from-nowhere"},
            {**base, "availability": "sort-of"},
            {**base, "turn": "vibing"},
            {**base, "observed_at": "yesterday"},
            {**base, "run_id": ""},
        ]
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(HeadVitalityError):
                VitalitySnapshot.from_json(payload)

    def test_from_json_rejects_a_non_number_timestamp_before_coercing_it(self) -> None:
        base = VitalitySnapshot.from_pane_readiness({"idle": True}, run_id=RUN_ID, observed_at=1.0).to_json()
        with self.assertRaises(HeadVitalityError):
            VitalitySnapshot.from_json({**base, "observed_at": None})

    def test_from_json_rejects_a_boolean_version_that_compares_equal_to_one(self) -> None:
        """``True == 1`` in Python: an equality check alone would accept it."""
        base = VitalitySnapshot.from_pane_readiness({"idle": True}, run_id=RUN_ID, observed_at=1.0).to_json()
        for forged in (True, 1.0):
            with self.subTest(version=forged), self.assertRaises(HeadVitalityError):
                VitalitySnapshot.from_json({**base, "version": forged})

    def test_from_json_rejects_non_finite_timestamps(self) -> None:
        base = VitalitySnapshot.from_pane_readiness({"idle": True}, run_id=RUN_ID, observed_at=1.0).to_json()
        for forged in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(observed_at=forged), self.assertRaises(HeadVitalityError):
                VitalitySnapshot.from_json({**base, "observed_at": forged})

    def test_a_constructor_timestamp_must_be_finite_too(self) -> None:
        for forged in (float("nan"), float("inf")):
            with self.subTest(observed_at=forged), self.assertRaises(HeadVitalityError):
                VitalitySnapshot.from_pane_readiness({"idle": True}, run_id=RUN_ID, observed_at=forged)


if __name__ == "__main__":
    unittest.main()
