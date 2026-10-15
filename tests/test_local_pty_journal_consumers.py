"""Actual launch, admission, handoff, delivery, terminal and metadata readers after eviction."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.support.local_pty_journal import small_limits
from ummanu.po.runner import RunnerError, ScopedPoProcess
from ummanu.runtime import local_pty_journal as views
from ummanu.runtime.head.identity import (
    HEARTBEAT_LIVE_MATCH,
    HEARTBEAT_NOT_YET_WRITTEN,
    HEARTBEAT_UNREADABLE,
    current_boot_id,
)
from ummanu.runtime.head.local_pty import client, journal as j, protocol
from ummanu.runtime.head.local_pty.run_end import RUN_END_EARLIER_BOOT, RUN_END_EXITED, RunEnd
from ummanu.runtime.head.local_pty.scoped_lifecycle import ScopedHeadLifecycle
from ummanu.runtime.local_pty_delivery import DeliveryMixin
from ummanu.runtime.local_pty_durable import _journal_state, _supervisor_state
from ummanu.runtime.local_pty_handoff import _handoff_progress
from ummanu.runtime.local_pty_values import (
    DELIVERY_UNESTABLISHED,
    REHYDRATED_FROM_SUPERVISOR,
    REHYDRATED_UNKNOWN,
)


class RetainedJournalConsumerTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.directory = self.root / "run"
        self.directory.mkdir()
        self.path = self.directory / protocol.JOURNAL_NAME
        self.address = SimpleNamespace(journal_path=self.path)
        small_limits(self)

    def progress(self, writer, count=120):
        for _ in range(count):
            writer.append(j.PROVIDER_PROGRESSED, turn=1, output_bytes=1, padding="x" * 1100)

    def handle(self, pid=20):
        return client.HeadHandle(self.directory, "run", "po", "task", self.directory / "socket",
                                 self.path, self.directory / "head.pid", 30, pid)

    def test_evicted_floor_and_interior_gap_refuse_unwitnessed_handoff_and_delivery(self):
        with j.JournalWriter(self.path, "run") as writer:
            writer.append(j.RUN_STARTED, head_pid=20)
            writer.append(j.TURN_STARTED, turn=1)
            floor = writer.seq
            writer.append(j.INPUT_ACCEPTED, subject="prompt", delivery=1, bytes=8,
                          offered_bytes=8, complete=True, state="complete")
            writer.append(j.INPUT_ACCEPTED, subject="prompt:submit", delivery=2, bytes=1,
                          offered_bytes=1, complete=True, state="complete")
            self.progress(writer)
        reading = j.read_events(self.path)
        self.assertLess(reading.events[0]["seq"], floor)
        self.assertTrue(reading.partial_head)
        self.assertTrue(_handoff_progress(reading, "prompt", floor).unknown)
        self.assertFalse(DeliveryMixin()._accepted_records(self.address, floor))
        report = DeliveryMixin()._report_of(self.address, {"id": 1}, 8, floor, established=False)
        self.assertEqual(report.outcome, DELIVERY_UNESTABLISHED)
        self.assertFalse(report.journalled)
        self.assertEqual(_journal_state(self.address, "run").source, REHYDRATED_UNKNOWN)
        live = _supervisor_state({"journal_seq": 999, "alive": True, "turn_open": False})
        self.assertEqual(live.source, REHYDRATED_FROM_SUPERVISOR)
        self.assertFalse(live.draining)

    def test_retained_old_turn_does_not_override_newer_unknown_state(self):
        with j.JournalWriter(self.path, "run") as writer:
            writer.append(j.RUN_STARTED)
            writer.append(j.TURN_STARTED, turn=1)
            self.progress(writer)
        result = views.head_run_turn_reading(self.root, "run", max_bytes=j.JOURNAL_MAX_BYTES)
        self.assertEqual(result["state"], "unavailable")
        with j.JournalWriter(self.path, "run") as writer:
            writer.append(j.TURN_FINISHED, turn=1, output_bytes=120)
        result = views.head_run_turn_reading(self.root, "run", max_bytes=j.JOURNAL_MAX_BYTES)
        self.assertEqual((result["state"], result["turn"]), ("observed", "idle"))

    def test_a_complete_suffix_above_floor_can_witness_exactly_one_delivery(self):
        with j.JournalWriter(self.path, "run") as writer:
            writer.append(j.RUN_STARTED)
            self.progress(writer)
            floor = writer.seq
            writer.append(j.INPUT_ACCEPTED, subject="prompt", delivery=1, bytes=8,
                          offered_bytes=8, complete=True, state="complete")
            writer.append(j.TURN_STARTED, turn=1)
            writer.append(j.TURN_FINISHED, turn=1, output_bytes=8)
        reading = j.read_events(self.path)
        self.assertTrue(reading.partial_head)
        self.assertTrue(reading.covers_since(floor))
        self.assertIsNotNone(_handoff_progress(reading, "prompt", floor).typed)
        report = DeliveryMixin()._report_of(self.address, {"id": 1}, 8, floor, established=False)
        self.assertTrue(report.journalled)
        self.assertEqual(report.written, 8)
        self.assertFalse(report.fatal)
        with self.path.open("ab") as stream:
            stream.write(b"{torn")
        self.assertTrue(_handoff_progress(j.read_events(self.path), "prompt", floor).unknown)
        self.assertFalse(DeliveryMixin()._accepted_records(self.address, floor))

    def test_launch_and_failed_launch_use_sequence_floor_across_capture_and_rotation(self):
        with j.JournalWriter(self.path, "run") as writer:
            writer.append(j.RUN_STARTED, head_pid=10, supervisor_pid=11)
            writer.append(j.RUN_EXITED, head_pid=10, exit_code=0)
            self.progress(writer)
            floor = writer.seq
        observed = []

        def launch(*_args, **_kwargs):
            with j.JournalWriter(self.path, "run") as writer:
                start = writer.append(j.RUN_STARTED, head_pid=20, supervisor_pid=30)
                self.progress(writer)
                end = writer.append(j.RUN_EXITED, head_pid=20, exit_code=0)
                observed.extend((start, end))
            (self.directory / protocol.SOCKET_NAME).touch()
            return SimpleNamespace(wait=lambda: 0)

        with (mock.patch.object(client.subprocess, "Popen", side_effect=launch),
              mock.patch.object(client, "_identity_written", return_value=True),
              mock.patch.object(client, "_answers", return_value=True)):
            handle = client.spawn_head(root=self.root, run_id="run", command="true", role="worker", task="task")
        self.assertEqual((handle.head_pid, handle.supervisor_pid), (20, 30))
        result = j.read_events(self.path)
        self.assertLess(len(result.events), floor)
        self.assertEqual(ScopedHeadLifecycle.started_or_exited(result.events, floor), (observed[0], True))
        lifecycle = mock.Mock()
        lifecycle.started_or_exited.side_effect = ScopedHeadLifecycle.started_or_exited
        error = client.LocalPtySpawnError("failed", "failure")
        self.assertIs(client._after_failed_launch(error, lifecycle, self.path,
                                                 self.directory / "socket", floor), error)
        lifecycle.stop_and_prove_empty.assert_called_once()
        lifecycle.cancel_started.assert_not_called()

    def test_failed_launch_cancels_current_started_head_when_old_floor_is_evicted(self):
        with j.JournalWriter(self.path, "run") as writer:
            writer.append(j.RUN_STARTED, head_pid=10)
            self.progress(writer)
            floor = writer.seq
            start = writer.append(j.RUN_STARTED, head_pid=20)
            self.progress(writer)
        lifecycle = mock.Mock()
        lifecycle.started_or_exited.side_effect = ScopedHeadLifecycle.started_or_exited
        error = client.LocalPtySpawnError("failed", "failure")
        client._after_failed_launch(error, lifecycle, self.path, self.directory / "socket", floor)
        lifecycle.cancel_started.assert_called_once_with(socket_path=self.directory / "socket",
                                                        journal_path=self.path, started_seq=start["seq"])

    def test_synchronous_scoped_launcher_uses_real_sequence_floor_after_rotation(self):
        from ummanu.runtime.head.local_pty import scoped_lifecycle as scoped

        owner = ScopedHeadLifecycle("run", 96)
        owner.persist(self.directory, role="worker", task="task", workspace=str(self.root))
        with j.JournalWriter(self.path, "run") as writer:
            writer.append(j.RUN_STARTED, head_pid=10)
            self.progress(writer)
            floor = writer.seq

        def launch(*_args, **_kwargs):
            with j.JournalWriter(self.path, "run") as writer:
                writer.append(j.SCOPE_BOUND, binding={"invocation_id": "new"})
                writer.append(j.RUN_STARTED, head_pid=20)
                writer.append(j.RUN_EXITED, head_pid=20, exit_code=0)
                self.progress(writer)
            return SimpleNamespace(pid=30)

        with (mock.patch.object(scoped.subprocess, "Popen", side_effect=launch),
              mock.patch.object(scoped, "launch_identity", return_value="boot:123"),
              mock.patch.object(scoped.time, "sleep", side_effect=AssertionError("must not wait"))):
            result = ScopedHeadLifecycle.launch_until_started(
                [str(self.directory), str(self.directory / "log"), "1", owner.generation, "fake-scope"])
        self.assertEqual(result, 0)
        self.assertLess(len(j.read_events(self.path).events), floor)

    def test_short_lived_exit_and_memory_reason_remain_waitable_after_eviction(self):
        with j.JournalWriter(self.path, "run") as writer:
            writer.append(j.RUN_STARTED, head_pid=20)
            writer.append(j.RUN_EXITED, head_pid=20, signal=9, head_loss_reason="memory_limit")
            self.progress(writer)
        with mock.patch("ummanu.po.runner.process_identity", return_value="identity"):
            process = ScopedPoProcess(self.handle())
        self.assertEqual(process.wait(timeout=0), -9)
        self.assertEqual(process.head_loss_reason, "memory_limit")
        self.assertTrue(views._has_exited(self.address))
        self.assertEqual(views.head_run_loss_reason(self.root, "run"), "memory_limit")
        with j.JournalWriter(self.path, "run") as writer:
            writer.append(j.RUN_STARTED, head_pid=21)
            self.progress(writer)
        self.assertFalse(views._has_exited(self.address))
        self.assertIsNone(views.head_run_loss_reason(self.root, "run"))
        with (mock.patch("ummanu.po.runner.still_running", return_value=False),
              self.assertRaisesRegex(RunnerError, "without a run.exited")):
            process.wait(timeout=0)

    def test_po_damaged_or_wrong_incarnation_exit_is_not_success(self):
        with j.JournalWriter(self.path, "run") as writer:
            writer.append(j.RUN_STARTED, head_pid=10)
            writer.append(j.RUN_EXITED, head_pid=10, exit_code=0)
        with mock.patch("ummanu.po.runner.process_identity", return_value="identity"):
            process = ScopedPoProcess(self.handle())
        with (mock.patch("ummanu.po.runner.still_running", return_value=True),
              self.assertRaises(subprocess.TimeoutExpired)):
            process.wait(timeout=0)
        with self.path.open("ab") as stream:
            stream.write(b"not-json\n")  # Complete corruption remains a hard refusal.
        with self.assertRaisesRegex(RunnerError, "damaged"):
            process.wait(timeout=0)

    def test_po_transient_torn_tail_retries_before_accepting_exact_matching_exit(self):
        from ummanu.po import runner

        with j.JournalWriter(self.path, "run") as writer:
            start = writer.append(j.RUN_STARTED, head_pid=20)
            writer.append(j.RUN_EXITED, head_pid=20, signal=9, head_loss_reason="memory_limit")
        complete = j.read_events(self.path)
        torn = replace(complete, truncated_tail=True)
        now = [0.0]
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            now[0] += seconds

        clock = SimpleNamespace(monotonic=lambda: now[0], sleep=sleep)
        with (mock.patch.object(runner, "process_identity", return_value="exact-supervisor"),
              mock.patch.object(runner, "time", clock),
              mock.patch.object(runner, "still_running", return_value=True) as alive,
              mock.patch.object(runner, "read_events", side_effect=[torn, complete])):
            process = ScopedPoProcess(replace(self.handle(), journal_started_seq=start["seq"]))
            self.assertEqual(process.wait(timeout=1), -9)
        self.assertEqual(process.head_loss_reason, "memory_limit")
        self.assertEqual(sleeps, [0.05], "the complete exit in the torn reading was not success")
        alive.assert_called_once_with(30, "exact-supervisor")

    def test_po_persistently_torn_live_tail_times_out_at_callers_deadline(self):
        from ummanu.po import runner

        now = [0.0]
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            now[0] += seconds

        clock = SimpleNamespace(monotonic=lambda: now[0], sleep=sleep)
        with (mock.patch.object(runner, "process_identity", return_value="exact-supervisor"),
              mock.patch.object(runner, "time", clock),
              mock.patch.object(runner, "still_running", return_value=True),
              mock.patch.object(runner, "read_events", return_value=j.JournalReadResult(truncated_tail=True))):
            process = ScopedPoProcess(self.handle())
            with self.assertRaises(subprocess.TimeoutExpired):
                process.wait(timeout=0.12)
        self.assertAlmostEqual(now[0], 0.12)
        self.assertAlmostEqual(sleeps[-1], 0.02)
        self.assertIsNone(process.head_loss_reason)

    def test_po_torn_tail_with_dead_supervisor_never_borrows_complete_prior_exit(self):
        from ummanu.po import runner

        prior = {"kind": j.RUN_EXITED, "run_id": "run", "seq": 2, "head_pid": 20, "exit_code": 0}
        with (mock.patch.object(runner, "process_identity", return_value="exact-supervisor"),
              mock.patch.object(runner, "time", SimpleNamespace(monotonic=lambda: 0,
                               sleep=mock.Mock(side_effect=AssertionError("dead supervisor cannot wait")))),
              mock.patch.object(runner, "still_running", return_value=False),
              mock.patch.object(runner, "read_events", return_value=j.JournalReadResult(
                  events=(prior,), truncated_tail=True))):
            process = ScopedPoProcess(self.handle())
            with self.assertRaisesRegex(RunnerError, "without a run.exited"):
                process.wait(timeout=1)
        self.assertIsNone(process.head_loss_reason)

    def test_multiple_prehead_bindings_preserve_failed_launch_cleanup_sequence_floor(self):
        with j.JournalWriter(self.path, "run") as writer:
            for number in range(32):
                writer.append(j.SCOPE_BOUND, binding={"invocation_id": f"failed-{number}"})
            floor = writer.seq
        lifecycle = mock.Mock(generation="new-generation")
        lifecycle.started_or_exited.side_effect = ScopedHeadLifecycle.started_or_exited
        lifecycle.launcher_argv.side_effect = lambda argv, **_kwargs: argv
        started = []

        def launch(*_args, **_kwargs):
            with j.JournalWriter(self.path, "run") as writer:
                writer.append(j.SCOPE_BOUND, binding={"invocation_id": "new"})
                started.append(writer.append(j.RUN_STARTED, head_pid=20, supervisor_pid=30))
                self.progress(writer)
            return SimpleNamespace(wait=lambda: 0)

        with (mock.patch.object(client, "ScopedHeadLifecycle", return_value=lifecycle) as factory,
              mock.patch.object(client.subprocess, "Popen", side_effect=launch),
              mock.patch.object(client, "_identity_written", return_value=False)):
            factory.from_run_dir.return_value = None
            with self.assertRaises(client.LocalPtySpawnError) as error:
                client.spawn_head(root=self.root, run_id="run", command="true", role="worker",
                                  task="task", memory_limit_mib=96, timeout=0)
        self.assertEqual(error.exception.reason, "timeout")
        self.assertTrue(error.exception.cleanup_complete)
        lifecycle.started_or_exited.assert_called_with(mock.ANY, floor)
        lifecycle.cancel_started.assert_called_once_with(socket_path=self.directory / protocol.SOCKET_NAME,
                                                        journal_path=self.path, started_seq=started[0]["seq"])
        self.assertLess(len(j.read_events(self.path).events), floor)

    def test_po_launch_sequence_fences_reused_pid_in_new_incarnation(self):
        with j.JournalWriter(self.path, "run") as writer:
            start = writer.append(j.RUN_STARTED, head_pid=20)
            writer.append(j.RUN_EXITED, head_pid=20, exit_code=0)
            writer.append(j.RUN_STARTED, head_pid=20)
            writer.append(j.RUN_EXITED, head_pid=20, exit_code=0)
        with mock.patch("ummanu.po.runner.process_identity", return_value="identity"):
            process = ScopedPoProcess(replace(self.handle(), journal_started_seq=start["seq"]))
        with self.assertRaisesRegex(RunnerError, "incarnation changed"):
            process.wait(timeout=0)

    def test_first_record_preserves_metadata_for_long_scoped_start_without_command(self):
        with j.JournalWriter(self.path, "run") as writer:
            writer.append(j.SCOPE_BOUND, binding={"invocation_id": "native", "proof": "secret" * 450})
            start = writer.append(j.RUN_STARTED, command="SECRET" * 600, role="worker", task="task")
            self.progress(writer)
        metadata = views.head_run_first_record(self.directory)
        self.assertEqual(metadata, {key: start.get(key) for key in ("task", "role", "run_id", "at")})
        self.assertNotIn("SECRET", json.dumps(metadata))
        self.assertTrue(views.head_run_journal_read(self.directory).partial_head)
        self.assertTrue(self.handle().events().partial_head)


class HeadRunEndTests(unittest.TestCase):
    """`head_run_end` proves a run ended from its own journal or a launch lost with its boot."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.root / "run" / protocol.JOURNAL_NAME
        self.path.parent.mkdir()

    def end(self, *, heartbeat_state=HEARTBEAT_LIVE_MATCH, launched_boot=""):
        return views.head_run_end(
            self.root, "run", heartbeat_state=heartbeat_state, launched_boot=launched_boot
        )

    def test_an_exit_after_the_start_is_the_end_with_its_signal(self):
        with j.JournalWriter(self.path, "run") as writer:
            writer.append(j.RUN_STARTED, head_pid=20)
            writer.append(j.RUN_EXITED, signal=15, exit_code=None)
        self.assertEqual(self.end(launched_boot=current_boot_id()), RunEnd(RUN_END_EXITED, 15))

    def test_a_start_after_the_last_exit_is_a_running_incarnation(self):
        with j.JournalWriter(self.path, "run") as writer:
            writer.append(j.RUN_STARTED, head_pid=20)
            writer.append(j.RUN_EXITED, signal=15, exit_code=None)
            writer.append(j.RUN_STARTED, head_pid=21)
        self.assertIsNone(self.end())

    def test_an_identity_lost_with_an_earlier_boot_is_the_end(self):
        for state in (HEARTBEAT_NOT_YET_WRITTEN, HEARTBEAT_UNREADABLE):
            with self.subTest(state=state):
                self.assertEqual(
                    self.end(heartbeat_state=state, launched_boot="an-earlier-boot"),
                    RunEnd(RUN_END_EARLIER_BOOT),
                )

    def test_a_missing_journal_or_launch_boot_alone_proves_nothing(self):
        for state, boot in (
            (HEARTBEAT_NOT_YET_WRITTEN, ""),
            (HEARTBEAT_UNREADABLE, current_boot_id()),
            (HEARTBEAT_LIVE_MATCH, "an-earlier-boot"),
        ):
            with self.subTest(state=state, boot=boot):
                self.assertIsNone(self.end(heartbeat_state=state, launched_boot=boot))
