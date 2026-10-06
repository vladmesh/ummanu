"""Profile memory limits, scoped launch, and typed head-loss recovery."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ummanu.dispatch import review, wait_vitality
from ummanu.dispatch.head_vitality_episode import VitalityVerdict
from ummanu.runtime.head.command import with_pid_heartbeat
from ummanu.runtime.head.local_pty import protocol, scope_bootstrap, scope_launcher
from ummanu.runtime.head.local_pty.client import LocalPtySpawnError, spawn_head
from ummanu.runtime.head.local_pty.journal import RUN_EXITED, RUN_STARTED, JournalWriter, read_events
from ummanu.runtime.head.local_pty.scoped_lifecycle import ScopedHeadLifecycle
from ummanu.runtime.head.local_pty.supervisor import Supervisor, SupervisorStartupError
from ummanu.runtime.head.memory import (
    DEFAULT_MEMORY_LIMIT_MIB,
    OOM_STREAM_ENV,
    MemoryScopeError,
    ScopeEvidence,
    memory_events,
    read_oom_victim,
    scope_argv,
    scope_unit,
)
from ummanu.runtime.head.run import HeadRun, StopInitiator
from ummanu.runtime.head.spec import HeadSpec, HeadSpecError
from ummanu.runtime.head.task_ref import TaskRef
from ummanu.runtime.local_pty_head import LocalPtyHeadRuntime, head_run_loss_reason
from ummanu.webproto.run_state import _exit_status


class HeadMemoryTests(unittest.TestCase):
    def test_kernel_victim_records_are_required_and_bound_to_the_reserved_pid(self) -> None:
        for message, expected in (
            ("Memory cgroup out of memory: Killed process 111 (head) total-vm:100", True),
            ("Memory cgroup out of memory: Killed process 222 (child) total-vm:100", False),
            ("Out of memory: Killed process 111 (head) total-vm:100", False),
            ("oom_reaper: reaped process 111 (head)", False),
            ("oom-kill:constraint=CONSTRAINT_MEMCG,task=head,pid=111", False),
        ):
            with self.subTest(message=message), mock.patch(
                "ummanu.runtime.head.memory.os.read",
                side_effect=[f"3,22,1000,-;{message}\n".encode(), BlockingIOError()],
            ):
                self.assertEqual(read_oom_victim(99, 111) is not None, expected)
        for failure in (OSError("lost records"), BlockingIOError()):
            with mock.patch("ummanu.runtime.head.memory.os.read", side_effect=[
                b"3,22,1000,-;Memory cgroup out of memory: Killed process 111 (head) total-vm:100\n",
                failure,
            ]):
                self.assertEqual(read_oom_victim(99, 111) is None, not isinstance(failure, BlockingIOError))
        with mock.patch("ummanu.runtime.head.memory.os.read", side_effect=[
            b"11,22,1000,-;Memory cgroup out of memory: Killed process 111 (spoof) total-vm:100\n",
            BlockingIOError(),
        ]):
            self.assertIsNone(read_oom_victim(99, 111))

    def test_delayed_reap_reads_child_oom_before_releasing_head_pid(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            supervisor = Supervisor(run_dir=Path(temp), run_id="race", role="worker",
                                    task="card:1", command="true", memory_limit_mib=1)
            supervisor._head_pid = 111
            supervisor._oom_stream = 99
            supervisor._memory_evidence = ScopeEvidence(Path(temp), {"oom_kill": 0})
            (Path(temp) / "memory.events.local").write_text("max 1\noom_kill 1\noom_group_kill 1\n")
            order = []
            def read(_fd, _size):
                order.append("kernel")
                if order.count("kernel") == 1:
                    return b"3,22,1000,-;Memory cgroup out of memory: Killed process 222 (child) total-vm:100\n"
                raise BlockingIOError()
            with (mock.patch("ummanu.runtime.head.local_pty.supervisor.os.waitid",
                             side_effect=lambda *_: order.append("reserve") or SimpleNamespace(si_pid=111)),
                  mock.patch("ummanu.runtime.head.memory.os.read", side_effect=read),
                  mock.patch("ummanu.runtime.head.local_pty.supervisor.os.waitpid",
                             side_effect=lambda *_: order.append("release") or (111, 9))):
                supervisor._reap()
            self.assertEqual(order, ["reserve", "kernel", "kernel", "release"])
            self.assertIsNone(supervisor._oom_victim)
            self.assertNotIn("head_loss_reason", ScopedHeadLifecycle.exit_fields(
                supervisor._head_status, supervisor._memory_evidence, oom_victim=supervisor._oom_victim,
            ))

    def test_stop_requires_scope_empty_for_each_dispatcher_role_even_after_head_exit(self) -> None:
        for role in ("observer", "worker", "review"):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                run = HeadRun(run_id=f"stop-{role}", role=role, spec=HeadSpec.from_profile("test", {"adapter": "codex"}),
                              workspace=temp, task_ref=TaskRef.card("card:1"))
                directory = protocol.run_dir_for(root, run.run_id)
                directory.mkdir()
                owner = ScopedHeadLifecycle(run.run_id, 96)
                owner.persist(directory)
                run = HeadRun.from_json({**run.to_json(), "scope_generation": owner.generation})
                cgroup = root / "system.slice" / scope_unit(run.run_id)
                cgroup.mkdir(parents=True)
                (cgroup / "cgroup.events").write_text("populated 1\n")
                runtime = LocalPtyHeadRuntime(root, head_process_status=lambda *_args, **_kwargs: {"state": "dead"})
                with (mock.patch.object(runtime, "_ask_to_stop", return_value={"ok": True}),
                      mock.patch.object(runtime, "_await_head_gone", return_value=True),
                      mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.CGROUP_ROOT", root),
                      mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.subprocess.run",
                                 return_value=SimpleNamespace(returncode=1, stderr=b"temporary failure"))):
                    refused = runtime.stop(run, StopInitiator(actor="owner"))
                self.assertFalse(refused.ok)
                self.assertFalse(refused.rotation_ready)
                self.assertFalse(refused.run.settled)
                self.assertFalse(json.loads((directory / "scope-owner.json").read_text())["cleanup_complete"])
                (cgroup / "cgroup.events").write_text("populated 0\n")
                with (mock.patch.object(runtime, "_ask_to_stop", return_value={"ok": True}),
                      mock.patch.object(runtime, "_await_head_gone", return_value=True),
                      mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.CGROUP_ROOT", root),
                      mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.subprocess.run",
                                 return_value=SimpleNamespace(returncode=0, stderr=b""))):
                    self.assertTrue(runtime.stop(run, StopInitiator(actor="owner")).ok)

    def test_cancelled_prestart_owner_bars_late_launch_and_stale_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original = ScopedHeadLifecycle("pending", 96, directory=root)
            original.persist(root)
            with mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.CGROUP_ROOT", root / "cgroups"):
                original.stop_and_prove_empty()
            replacement = ScopedHeadLifecycle("pending", 96, directory=root)
            replacement.persist(root)
            with mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.subprocess.Popen") as popen:
                self.assertEqual(scope_launcher.main([
                    temp, str(root / "scope.log"), "1", original.generation, "systemd-run",
                ]), 1)
                popen.assert_not_called()
            with self.assertRaisesRegex(MemoryScopeError, "stale cleanup"):
                original.stop_and_prove_empty()
            self.assertTrue(json.loads((root / "scope-owner.json").read_text())["launch_allowed"])

    def test_new_incarnation_cannot_reuse_an_earlier_memory_exit_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = protocol.run_dir_for(temp, "reused")
            directory.mkdir()
            with JournalWriter(directory / protocol.JOURNAL_NAME, "reused") as journal:
                journal.append(RUN_STARTED, head_pid=111)
                journal.append(RUN_EXITED, head_pid=111, signal=9, head_loss_reason="memory_limit")
                self.assertEqual(head_run_loss_reason(temp, "reused"), "memory_limit")
                journal.append(RUN_STARTED, head_pid=111)
            self.assertIsNone(head_run_loss_reason(temp, "reused"))

    def test_missing_membership_in_existing_cgroup_cannot_dispose_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            owner = ScopedHeadLifecycle("unreadable-membership", 96)
            owner.persist(root)
            (root / "system.slice" / scope_unit(owner.run_id)).mkdir(parents=True)
            with (mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.CGROUP_ROOT", root),
                  mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.subprocess.run",
                             return_value=SimpleNamespace(returncode=0, stderr=b"")),
                  self.assertRaisesRegex(MemoryScopeError, "no membership evidence")):
                owner.stop_and_prove_empty()
            self.assertFalse(json.loads((root / "scope-owner.json").read_text())["cleanup_complete"])

    def test_scoped_heartbeat_writes_identity_in_head_process_before_exec(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pid_file = Path(temp) / "head.pid"
            wrapped = with_pid_heartbeat(
                "/bin/true", str(pid_file), identity={"run_id": "scoped"}, in_process=True,
            )
            self.assertTrue(wrapped.startswith("exec python3 -P -c "))
            process = subprocess.Popen(["/bin/sh", "-c", wrapped])
            self.assertEqual(process.wait(timeout=5), 0)
            self.assertEqual(json.loads(pid_file.read_text())["pid"], process.pid)

    def test_default_and_profile_limits_materialize_as_own_scope_property(self) -> None:
        for role in ("observer", "worker", "review", "po"):
            with self.subTest(role=role):
                default = HeadSpec.from_profile("default", {"adapter": "codex"})
                explicit = HeadSpec.from_profile(
                    "explicit", {"adapter": "codex", "memory_limit_mib": 12288}
                )
                self.assertEqual(default.memory_limit_mib, DEFAULT_MEMORY_LIMIT_MIB)
                self.assertEqual(explicit.memory_limit_mib, 12288)
                run_id = f"{role}-run"
                argv = scope_argv(run_id, explicit.memory_limit_mib, ["/bin/true"])
                self.assertEqual(argv[:5], ["sudo", "-n", "systemd-run", "--system", "--scope"])
                self.assertIn(f"--property=MemoryMax={12288 * 1024 * 1024}", argv)
                self.assertIn("--property=MemorySwapMax=0", argv)
                self.assertIn(scope_unit(run_id), argv)
                self.assertNotEqual(scope_unit(run_id), scope_unit(f"{role}-other"))

    def test_invalid_profile_limit_is_refused(self) -> None:
        for bad in (0, -1, True, "4096", 1.5):
            with self.subTest(bad=bad), self.assertRaisesRegex(HeadSpecError, "memory_limit_mib"):
                HeadSpec.from_profile("bad", {"adapter": "codex", "memory_limit_mib": bad})

    def test_scope_bootstrap_protects_only_the_supervisor_before_head_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cgroup = Path(temp)
            protection = cgroup / "oom_score_adj"
            scope_bootstrap.install_oom_contract(cgroup, protection)
            self.assertEqual((cgroup / "memory.oom.group").read_text(), "1\n")
            self.assertEqual(protection.read_text(), "-1000\n")
            argv = scope_argv("run", 96, ["/bin/true"])
            self.assertIn(str(Path(scope_bootstrap.__file__).resolve()), argv)
            self.assertIn("-I", argv)
            self.assertIn("--property=Delegate=yes", argv)
            owned = scope_argv("po-run", 96, ["/bin/true"], owner_unit="ummanu-po.service")
            self.assertIn("--property=BindsTo=ummanu-po.service", owned)
            self.assertIn("--property=After=ummanu-po.service", owned)

    def test_supervisor_refuses_until_its_own_scope_has_the_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_id = "scope-preflight"
            cgroup = Path(temp) / scope_unit(run_id)
            cgroup.mkdir()
            (cgroup / "memory.max").write_text("1048576\n", encoding="ascii")
            (cgroup / "memory.swap.max").write_text("0\n", encoding="ascii")
            (cgroup / "memory.oom.group").write_text("1\n", encoding="ascii")
            (cgroup / "memory.events.local").write_text("max 0\noom_kill 0\noom_group_kill 0\n", encoding="ascii")
            (cgroup / "pids.peak").write_text("1\n", encoding="ascii")
            supervisor = Supervisor(run_dir=Path(temp), run_id=run_id, role="worker",
                                    task="card:1", command="true", memory_limit_mib=1)
            with (mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.own_cgroup", return_value=cgroup),
                  mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.supervisor_oom_protected", return_value=True),
                  mock.patch.dict(os.environ, {OOM_STREAM_ENV: "99"}),
                  mock.patch("ummanu.runtime.head.local_pty.supervisor.os.fstat"),
                  mock.patch("ummanu.runtime.head.local_pty.supervisor.os.set_inheritable")):
                supervisor._prepare_memory_scope()
                self.assertEqual(supervisor._memory_evidence, ScopeEvidence(cgroup, {"max": 0, "oom_kill": 0, "oom_group_kill": 0}))
                (cgroup / "memory.max").write_text("2097152\n", encoding="ascii")
                with self.assertRaisesRegex(SupervisorStartupError, "MemoryMax=1048576"):
                    supervisor._prepare_memory_scope()
                (cgroup / "memory.max").write_text("1048576\n", encoding="ascii")
                (cgroup / "memory.swap.max").write_text("max\n", encoding="ascii")
                with self.assertRaisesRegex(SupervisorStartupError, "MemorySwapMax=0"):
                    supervisor._prepare_memory_scope()

    def test_synthetic_tiny_limit_kill_with_children_or_threads_persists_reason_for_every_role(self) -> None:
        # A victim-specific kernel record is the supervisor's causal witness.
        # Group counters alone no longer establish a cause. No production cgroup is touched.
        for role in ("observer", "worker", "review", "po"):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as temp:
                run_id = f"tiny-{role}"
                self.assertIn("--property=MemoryMax=1048576", scope_argv(run_id, 1, ["/bin/true"]))
                self.assertIn("--property=MemorySwapMax=0", scope_argv(run_id, 1, ["/bin/true"]))
                run_dir = protocol.run_dir_for(temp, run_id)
                run_dir.mkdir(parents=True)
                supervisor = Supervisor(
                    run_dir=run_dir, run_id=run_id, role=role, task="synthetic", command="true",
                    memory_limit_mib=1,
                )
                supervisor._head_pid = 12345
                supervisor._head_status = signal.SIGKILL
                supervisor._memory_evidence = ScopeEvidence(Path(temp), {"max": 0, "oom_kill": 0, "oom_group_kill": 0})
                (Path(temp) / "memory.events.local").write_text("max 1\noom_kill 1\noom_group_kill 1\n", encoding="ascii")
                # The peak includes the head's children or threads and has no role in attribution.
                (Path(temp) / "pids.peak").write_text("8\n", encoding="ascii")
                supervisor._oom_victim = {"pid": 12345, "kernel_seq": 10, "kernel_usec": 2000}
                supervisor._journal = JournalWriter(run_dir / "journal.jsonl", run_id).open()
                try:
                    with (
                        mock.patch.object(supervisor, "_finish_delivery"),
                        mock.patch.object(supervisor, "_flush_progress"),
                    ):
                        supervisor._finish()
                finally:
                    supervisor._journal.close()
                exit_record = read_events(run_dir / "journal.jsonl").of_kind(RUN_EXITED)[0]
                self.assertEqual(exit_record["head_loss_reason"], "memory_limit")
                self.assertEqual(exit_record["signal"], signal.SIGKILL)
                self.assertEqual(head_run_loss_reason(temp, run_id), "memory_limit")
                self.assertEqual(_exit_status((exit_record,))["head_loss_reason"], "memory_limit")

    def test_other_deaths_are_not_called_memory_exhaustion(self) -> None:
        before = {"max": 0, "oom_kill": 0, "oom_group_kill": 0}
        for number in (None, signal.SIGTERM, signal.SIGKILL):
            self.assertNotIn("head_loss_reason", ScopedHeadLifecycle.exit_fields(
                number or 0, ScopeEvidence(Path("unused"), before),
            ))
        with tempfile.TemporaryDirectory() as temp:
            cgroup = Path(temp)
            (cgroup / "memory.events.local").write_text(
                "max 1\noom_kill 1\noom_group_kill 1\n", encoding="ascii"
            )
            self.assertNotIn("head_loss_reason", ScopedHeadLifecycle.exit_fields(
                signal.SIGKILL, ScopeEvidence(cgroup, before), stopping=True,
            ))
        self.assertIsNone(memory_events(None))
        self.assertNotIn("head_loss_reason", _exit_status(({
            "kind": RUN_EXITED, "signal": signal.SIGKILL, "head_loss_reason": "other"
        },)))

    def test_child_oom_then_head_stop_does_not_persist_memory_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_id = "child-oom-stop"
            run_dir = protocol.run_dir_for(temp, run_id)
            run_dir.mkdir()
            before = {"max": 0, "oom_kill": 0, "oom_group_kill": 0}
            (run_dir / "memory.events.local").write_text(
                "max 1\noom_kill 1\noom_group_kill 0\n", encoding="ascii"
            )
            supervisor = Supervisor(
                run_dir=run_dir, run_id=run_id, role="worker", task="card:1",
                command="true", memory_limit_mib=1,
            )
            supervisor._head_pid = 12345
            supervisor._head_status = signal.SIGKILL
            supervisor._stopping = True
            supervisor._oom_victim = {"pid": 12345, "kernel_seq": 22, "kernel_usec": 1000}
            supervisor._memory_evidence = ScopeEvidence(run_dir, before)
            supervisor._journal = JournalWriter(run_dir / "journal.jsonl", run_id).open()
            try:
                with (mock.patch.object(supervisor, "_finish_delivery"),
                      mock.patch.object(supervisor, "_flush_progress")):
                    supervisor._finish()
            finally:
                supervisor._journal.close()
            exited = read_events(run_dir / "journal.jsonl").of_kind(RUN_EXITED)[0]
            self.assertEqual(exited["signal"], signal.SIGKILL)
            self.assertTrue(exited["stopping"])
            self.assertNotIn("head_loss_reason", exited)

    def test_child_oom_after_unrelated_head_sigkill_is_not_attributed_to_head(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cgroup = Path(temp)
            run_id = "child-oom-after-sigkill"
            run_dir = protocol.run_dir_for(temp, run_id)
            run_dir.mkdir()
            before = {"max": 0, "oom_kill": 0, "oom_group_kill": 0}
            # The head died from an external SIGKILL. Its surviving child then exhausted
            # the group before the supervisor read the counters. The peak records the child.
            (cgroup / "memory.events.local").write_text(
                "max 1\noom_kill 1\noom_group_kill 1\n", encoding="ascii"
            )
            (cgroup / "pids.peak").write_text("3\n", encoding="ascii")
            supervisor = Supervisor(
                run_dir=run_dir, run_id=run_id, role="worker", task="card:1",
                command="true", memory_limit_mib=1,
            )
            supervisor._head_pid = 12345
            supervisor._head_status = signal.SIGKILL
            supervisor._memory_evidence = ScopeEvidence(cgroup, before)
            # No kernel record names the head; observation is after the child OOM.
            supervisor._oom_victim = None
            supervisor._journal = JournalWriter(run_dir / "journal.jsonl", run_id).open()
            try:
                with (mock.patch.object(supervisor, "_finish_delivery"),
                      mock.patch.object(supervisor, "_flush_progress")):
                    supervisor._finish()
            finally:
                supervisor._journal.close()
            exited = read_events(run_dir / "journal.jsonl").of_kind(RUN_EXITED)[0]
            self.assertEqual(exited["signal"], signal.SIGKILL)
            self.assertNotIn("head_loss_reason", exited)

    def test_child_oom_then_unrelated_head_kill_is_not_attributed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cgroup = Path(temp)
            evidence = ScopeEvidence(cgroup, {"max": 0, "oom_kill": 0, "oom_group_kill": 0})
            (cgroup / "memory.events.local").write_text("max 1\noom_kill 1\noom_group_kill 0\n")
            with mock.patch("ummanu.runtime.head.memory.os.read", side_effect=[
                b"3,22,1000,-;Memory cgroup out of memory: Killed process 222 (child) total-vm:1\n",
                BlockingIOError(),
            ]):
                victim = read_oom_victim(99, 111)
            self.assertIsNone(victim)
            fields = ScopedHeadLifecycle.exit_fields(signal.SIGKILL, evidence, oom_victim=victim)
            self.assertEqual(fields["signal"], signal.SIGKILL)
            self.assertNotIn("head_loss_reason", fields)
            self.assertNotIn("oom_victim", fields)

    def test_prestart_timeout_stops_scope_and_proves_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_id = "prestart-timeout"
            cgroup = root / "system.slice" / scope_unit(run_id)
            cgroup.mkdir(parents=True)
            (cgroup / "cgroup.events").write_text("populated 1\n", encoding="ascii")

            def stop(*_args, **_kwargs):
                (cgroup / "cgroup.events").write_text("populated 0\n", encoding="ascii")
                return SimpleNamespace(returncode=0, stderr=b"")

            with (
                mock.patch("ummanu.runtime.head.local_pty.client.subprocess.Popen",
                           return_value=SimpleNamespace(wait=lambda: 0)),
                mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.CGROUP_ROOT", root),
                mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.subprocess.run", side_effect=stop) as systemctl,
                self.assertRaises(LocalPtySpawnError) as failure,
            ):
                spawn_head(root=root / "runs", run_id=run_id, role="worker", task="card:1",
                           command="true", memory_limit_mib=1, timeout=0)
            self.assertEqual(failure.exception.reason, "timeout")
            self.assertTrue(failure.exception.cleanup_complete)
            self.assertEqual(systemctl.call_count, 1)
            self.assertEqual(ScopedHeadLifecycle.from_run_dir(protocol.run_dir_for(root / "runs", run_id)).run_id, run_id)

    def test_systemd_stop_failure_keeps_durable_scope_for_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_id = "stop-failed"
            cgroup = root / "system.slice" / scope_unit(run_id)
            cgroup.mkdir(parents=True)
            (cgroup / "cgroup.events").write_text("populated 1\n", encoding="ascii")
            run_dir = protocol.run_dir_for(root / "runs", run_id)
            with (
                mock.patch("ummanu.runtime.head.local_pty.client.subprocess.Popen",
                           return_value=SimpleNamespace(wait=lambda: 0)),
                mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.CGROUP_ROOT", root),
                mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.subprocess.run",
                           return_value=SimpleNamespace(returncode=1, stderr=b"failed")),
                self.assertRaises(LocalPtySpawnError) as failure,
            ):
                spawn_head(root=root / "runs", run_id=run_id, role="po", task="turn:1",
                           command="true", memory_limit_mib=1, timeout=0)
            self.assertEqual(failure.exception.reason, "cleanup_failed")
            self.assertFalse(failure.exception.cleanup_complete)
            owner = ScopedHeadLifecycle.from_run_dir(run_dir)
            self.assertIsNotNone(owner)
            with (
                mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.CGROUP_ROOT", root),
                mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.subprocess.run",
                           side_effect=lambda *_args, **_kwargs: (
                               (cgroup / "cgroup.events").write_text("populated 0\n", encoding="ascii")
                               and SimpleNamespace(returncode=0, stderr=b"")
                           )),
            ):
                owner.stop_and_prove_empty()

    def test_successful_stop_without_empty_membership_is_not_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            owner = ScopedHeadLifecycle("still-populated", 96)
            cgroup = root / "system.slice" / scope_unit(owner.run_id)
            cgroup.mkdir(parents=True)
            (cgroup / "cgroup.events").write_text("populated 1\n", encoding="ascii")
            with (
                mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.CGROUP_ROOT", root),
                mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.subprocess.run",
                           return_value=SimpleNamespace(returncode=0, stderr=b"")),
                mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.time.monotonic",
                           side_effect=[0, 11]),
                self.assertRaisesRegex(RuntimeError, "still has members"),
            ):
                owner.stop_and_prove_empty()

    def test_scoped_startup_cancellation_waits_for_supervisor_reap(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            journal = run_dir / "journal.jsonl"
            lifecycle = ScopedHeadLifecycle("cancel-run", 96)
            with JournalWriter(journal, "cancel-run") as writer:
                started = writer.append(RUN_STARTED, head_pid=123, supervisor_pid=456)

            def stop(**_kwargs):
                with JournalWriter(journal, "cancel-run") as writer:
                    writer.append(RUN_EXITED, head_pid=123, signal=signal.SIGKILL)

            client = mock.MagicMock()
            client.__enter__.return_value.stop.side_effect = stop
            with mock.patch(
                "ummanu.runtime.head.local_pty.client.SupervisorClient.connect",
                return_value=client,
            ) as connect:
                lifecycle.cancel_started(
                    socket_path=run_dir / "socket", journal_path=journal,
                    started_seq=started["seq"],
                )
            connect.assert_called_once()
            self.assertTrue(read_events(journal).of_kind(RUN_EXITED))

    def test_spawn_materializes_scope_before_supervisor_starts(self) -> None:
        for role in ("observer", "worker", "review", "po"):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as temp:
                run_id = f"launch-materialization-{role}"
                run_dir = protocol.run_dir_for(temp, run_id)
                run_dir.mkdir(parents=True)
                protocol.socket_path_for(run_dir).touch()
                started = {"kind": "run.started", "head_pid": 12, "supervisor_pid": 11}
                fake_process = SimpleNamespace(wait=lambda: 0)
                with (
                    mock.patch("ummanu.runtime.head.local_pty.client.subprocess.Popen", return_value=fake_process) as popen,
                    mock.patch("ummanu.runtime.head.local_pty.client.read_events", side_effect=[
                        SimpleNamespace(events=()), SimpleNamespace(events=(started,))
                    ]),
                    mock.patch("ummanu.runtime.head.local_pty.client._identity_written", return_value=True),
                    mock.patch("ummanu.runtime.head.local_pty.client._answers", return_value=True),
                ):
                    handle = spawn_head(root=temp, run_id=run_id, role=role, task="card:1",
                                        command="true", memory_limit_mib=1)
                argv = popen.call_args.args[0]
                self.assertIn("ummanu.runtime.head.local_pty.scope_launcher", argv)
                self.assertIn("--property=MemoryMax=1048576", argv)
                self.assertIn("--property=MemorySwapMax=0", argv)
                self.assertEqual(argv[argv.index("--memory-limit-mib") + 1], "1")
                self.assertIn("--reuid=", " ".join(argv))
                self.assertNotIn("--daemonize", argv)
                self.assertEqual(handle.head_pid, 12)

    def test_scope_launcher_releases_its_caller_only_after_the_scope_starts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            owner = ScopedHeadLifecycle("launcher", 1)
            owner.persist(Path(temp))
            scope = SimpleNamespace(pid=123, poll=mock.Mock(side_effect=AssertionError("started scope was polled")))
            started = SimpleNamespace(events=({"kind": RUN_STARTED},))
            with (
                mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.subprocess.Popen", return_value=scope) as popen,
                mock.patch("ummanu.runtime.head.local_pty.journal.read_events", side_effect=[SimpleNamespace(events=()), started]),
                mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.launch_identity", return_value="boot:123"),
                mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.os.write"),
            ):
                result = scope_launcher.main([temp, str(Path(temp) / "scope.log"), "1", owner.generation, "systemd-run"])
            self.assertEqual(result, 0)
            self.assertTrue(popen.call_args.kwargs["start_new_session"])
            self.assertEqual(popen.call_args.args[0][-1], "systemd-run")
            self.assertIn("--exec-gated", popen.call_args.args[0])

    def test_scope_launcher_propagates_registration_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            owner = ScopedHeadLifecycle("launcher", 1)
            owner.persist(Path(temp))
            scope = SimpleNamespace(pid=123, poll=lambda: 7)
            with (mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.subprocess.Popen", return_value=scope),
                  mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.launch_identity", return_value="boot:123"),
                  mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.os.write")):
                result = scope_launcher.main([temp, str(Path(temp) / "scope.log"), "1", owner.generation, "systemd-run"])
            self.assertEqual(result, 7)

    def test_scope_launcher_reaps_prestart_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            (run_dir / protocol.STARTUP_ERROR_NAME).write_text("{}", encoding="utf-8")
            owner = ScopedHeadLifecycle("launcher", 1)
            owner.persist(run_dir)
            scope = SimpleNamespace(pid=123, wait=mock.Mock(return_value=0))
            with (mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.subprocess.Popen",
                            return_value=scope),
                  mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.launch_identity", return_value="boot:123"),
                  mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.os.write")):
                result = scope_launcher.main([temp, str(run_dir / "scope.log"), "1", owner.generation, "systemd-run"])
            self.assertEqual(result, 0)
            scope.wait.assert_called_once_with(timeout=5)

    def test_immediate_scoped_exit_returns_durable_head_handle(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_id = "immediate-exit"
            started = {"kind": RUN_STARTED, "seq": 1, "head_pid": 12, "supervisor_pid": 11}
            exited = {"kind": RUN_EXITED, "seq": 2, "head_pid": 12, "exit_code": 7}
            with (
                mock.patch("ummanu.runtime.head.local_pty.client.subprocess.Popen", return_value=SimpleNamespace(wait=lambda: 0)),
                mock.patch("ummanu.runtime.head.local_pty.client.read_events", side_effect=[
                    SimpleNamespace(events=()), SimpleNamespace(events=(started, exited)),
                ]),
                mock.patch("ummanu.runtime.head.local_pty.client._identity_written", return_value=True),
                mock.patch("ummanu.runtime.head.local_pty.client._answers", side_effect=AssertionError("dead socket probed")),
            ):
                handle = spawn_head(root=temp, run_id=run_id, role="worker", task="card:1",
                                    command="exit 7", memory_limit_mib=1)
            self.assertEqual(handle.head_pid, 12)
            self.assertEqual(ScopedHeadLifecycle.started_or_exited((started, exited), 0), (started, True))

    def test_dead_status_admits_typed_journal_reason_for_both_card_roles(self) -> None:
        for kind in ("worker", "review"):
            with (
                self.subTest(kind=kind),
                mock.patch.object(review, "_head_run_process_status", return_value={"state": "dead"}),
                mock.patch.object(review, "_heartbeat_is_dead", return_value=True),
                mock.patch.object(review, "_supervised", return_value=True),
            ):
                run = {"run_id": f"{kind}-run"}
                record = SimpleNamespace(workspace="/tmp/work", worker_head_run=run,
                                         review_head_run=run, worker_leaf="", review_leaf="")
                host = SimpleNamespace(mode="real", head_loss_reason=lambda _run: "memory_limit")
                status = review.command_terminal_status(host, {"ref": "card:1"}, record, kind=kind)
                self.assertEqual(status["head_loss_reason"], "memory_limit")

    def test_memory_loss_enters_the_existing_dead_head_recovery(self) -> None:
        for kind in ("worker", "review"):
            with self.subTest(kind=kind), mock.patch.object(
                wait_vitality, "_trigger_wait_watchdog", return_value={"action": "normal-recovery"}
            ) as recover:
                result = wait_vitality._decide_wait_by_verdict(
                    SimpleNamespace(), {"ref": "card:1"}, SimpleNamespace(report_generation=1),
                    {}, {}, "attempt", kind=kind,
                    status={"head_loss_reason": "memory_limit"},
                    episode=SimpleNamespace(verdict=VitalityVerdict.DEAD), now=1,
                    runtime_reason="", activity=None, progress_at=0,
                )
                self.assertEqual(result, {"action": "normal-recovery"})
                self.assertIn("memory_limit", recover.call_args.kwargs["trigger"])
