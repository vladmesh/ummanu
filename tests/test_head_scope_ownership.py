"""Scope cleanup and terminal settlement share the canonical owner's serialization."""

from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ummanu.po import store as po_store
from ummanu.dispatch.host import CommandHostRuntime, HostError
from ummanu.dispatch.launch import merge_launch_head_run
from ummanu.po.runner import PoRunner
from ummanu.runtime.head.identity import head_process_status, publish_heartbeat
from ummanu.runtime.head.local_pty import protocol
from ummanu.runtime.head.local_pty.client import LocalPtySpawnError, spawn_head
from ummanu.runtime.head.local_pty.journal import JournalWriter, RUN_STARTED, RUN_EXITED
from ummanu.runtime.head.local_pty.scoped_lifecycle import ScopedHeadLifecycle
from ummanu.runtime.head.memory import MemoryScopeError, scope_unit
from ummanu.runtime.head.run import HeadRun, StopInitiator
from ummanu.runtime.head.spec import HeadSpec
from ummanu.runtime.head.task_ref import TaskRef
from ummanu.runtime.local_pty_head import LocalPtyHeadRuntime
from ummanu.runtime.head.command import HeadCommand
from ummanu.webproto.errors import RuntimeUnavailable
from ummanu.webproto.lifecycle import RunLifecycle
from ummanu.webproto.runs import ProductRun, RunStore, RAISING, RAISED, SETTLED, UNRESOLVED
from tests.po_fake_store import FakeBoard, FakePoStore


class ProductOwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.cgroups = self.root / "cgroups"
        self.enterContext(mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.CGROUP_ROOT", self.cgroups))
        self.spec = HeadSpec.from_profile("fixture", {"adapter": "claude", "memory_limit_mib": 96})
        self.store = RunStore(self.root)
        self.runtime = LocalPtyHeadRuntime(self.root / "heads", head_process_status=head_process_status, stop_timeout=0)
        self.lifecycle = RunLifecycle(self.store, self.runtime)
        self.enterContext(mock.patch.object(self.lifecycle, "_preflight"))
        self.enterContext(mock.patch("ummanu.webproto.lifecycle.render_head_command", return_value=HeadCommand(command="true", adapter="claude")))
        self.document = self.root / "task.md"
        self.document.write_text("fixture")
        directory = self.root / "heads" / "product"
        run = ProductRun(run_id="product", request_id="fixture", ref="card:fixture", project="fixture",
                         role="worker", profile="fixture", workspace=str(self.root), run_dir=str(directory),
                         pid_file=str(directory / "head.pid"), journal_path=str(directory / "journal.jsonl"))
        self.store.save(run)
        self.ahead = self.lifecycle.advance(run, RAISING, now=1, spec=self.spec, profile={}, document=self.document)

    def raise_head(self) -> None:
        self.lifecycle.advance(self.ahead, RAISED, now=1, spec=self.spec, profile={},
                               document=self.document, note="fixture", env={})

    def test_prestart_failure_retains_write_ahead_generation_until_scope_proof(self) -> None:
        # Exercise the actual product -> runtime -> launcher path without creating a process.
        membership = self.cgroups / "system.slice" / scope_unit(self.ahead.run_id) / "cgroup.events"
        membership.parent.mkdir(parents=True)
        membership.write_text("populated 1\n")
        saved_head = HeadRun.from_json(self.store.get(self.ahead.run_id).head_run)
        self.assertTrue(saved_head.scope_generation)
        self.assertFalse(saved_head.handle)
        with mock.patch("ummanu.runtime.head.local_pty.client.subprocess.Popen", return_value=SimpleNamespace(wait=lambda: 7)), mock.patch(
            "ummanu.runtime.head.local_pty.scoped_lifecycle.subprocess.run", return_value=subprocess.CompletedProcess([], 1, stderr=b"transient refusal"),
        ):
            with self.assertRaises(RuntimeUnavailable):
                self.raise_head()
        retained = self.store.get(self.ahead.run_id)
        self.assertEqual(retained.phase, UNRESOLVED)
        self.assertFalse(retained.ended)
        directory = Path(self.ahead.run_dir)
        owner = ScopedHeadLifecycle.read_owner(directory)
        self.assertEqual(owner["generation"], saved_head.scope_generation)
        self.assertFalse(owner["cleanup_complete"])
        self.assertFalse(Path(retained.pid_file).exists())
        self.assertFalse(Path(retained.journal_path).exists())
        membership.write_text("populated 0\n")
        with mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stderr=b"")):
            # A fresh service and runtime use only the original durable record.
            recovered = RunLifecycle(self.store, LocalPtyHeadRuntime(self.root / "heads", head_process_status=head_process_status, stop_timeout=0))
            settled = recovered.advance(retained, SETTLED, now=2)
        self.assertTrue(settled.ended)
        self.assertTrue(ScopedHeadLifecycle.read_owner(directory)["cleanup_complete"])
        self.assertEqual(HeadRun.from_json(settled.head_run).scope_generation, saved_head.scope_generation)

    def test_failed_handle_save_stops_scope_using_original_write_ahead_record(self) -> None:
        saved_head = HeadRun.from_json(self.ahead.head_run)
        def spawn(**options):
            self.assertEqual(options["scope_generation"], saved_head.scope_generation)
            owner = ScopedHeadLifecycle(options["run_id"], 96, generation=options["scope_generation"])
            owner.persist(Path(self.ahead.run_dir))
            return SimpleNamespace(socket_path=Path(self.ahead.run_dir) / "head.sock", pid_file=Path(self.ahead.pid_file),
                                   scope_generation=owner.generation)
        real_save = self.store.save
        def save(run):
            if run.phase == RAISED:
                raise OSError("handle save interrupted")
            return real_save(run)
        with mock.patch.object(self.runtime, "_spawn", side_effect=spawn), mock.patch.object(
            self.lifecycle, "_point_at_the_task", side_effect=lambda live, *_: live,
        ), mock.patch.object(self.store, "save", side_effect=save):
            with self.assertRaisesRegex(OSError, "handle save interrupted"):
                self.raise_head()
        settled = self.store.get(self.ahead.run_id)
        self.assertTrue(settled.ended)
        self.assertEqual(settled.phase, SETTLED)
        self.assertEqual(HeadRun.from_json(settled.head_run), saved_head)
        self.assertTrue(ScopedHeadLifecycle.read_owner(Path(self.ahead.run_dir))["cleanup_complete"])

    def test_write_ahead_start_cannot_replace_or_stop_an_existing_generation(self) -> None:
        directory = Path(self.ahead.run_dir)
        for generation in (HeadRun.from_json(self.ahead.head_run).scope_generation, "replacement"):
            with self.subTest(generation=generation):
                owner = ScopedHeadLifecycle(self.ahead.run_id, 96, generation=generation)
                owner.persist(directory)
                before = ScopedHeadLifecycle.read_owner(directory)
                with mock.patch("ummanu.runtime.head.local_pty.client.subprocess.Popen") as launch, mock.patch(
                    "ummanu.runtime.head.local_pty.scoped_lifecycle.subprocess.run",
                ) as stop:
                    with self.assertRaisesRegex(LocalPtySpawnError, "cannot replace") as refused:
                        spawn_head(root=self.root / "heads", run_id=self.ahead.run_id, role="worker", task="card:fixture",
                                   command="true", memory_limit_mib=96,
                                   scope_generation=HeadRun.from_json(self.ahead.head_run).scope_generation)
                self.assertEqual(refused.exception.scope_generation, HeadRun.from_json(self.ahead.head_run).scope_generation)
                self.assertEqual(ScopedHeadLifecycle.read_owner(directory), before)
                launch.assert_not_called()
                stop.assert_not_called()
                owner.stop_and_prove_empty()

    def test_write_ahead_admission_checks_for_competing_owner_under_the_lock(self) -> None:
        directory = Path(self.ahead.run_dir)
        owner = ScopedHeadLifecycle(self.ahead.run_id, 96)
        owner.persist(directory)
        owner.stop_and_prove_empty()
        before = ScopedHeadLifecycle.read_owner(directory)
        # The unlocked first read precedes another caller's registration and cleanup.
        with mock.patch("ummanu.runtime.head.local_pty.client.ScopedHeadLifecycle.from_run_dir", return_value=None), mock.patch(
            "ummanu.runtime.head.local_pty.client.subprocess.Popen",
        ) as launch:
            with self.assertRaisesRegex(LocalPtySpawnError, "cannot replace"):
                spawn_head(root=self.root / "heads", run_id=self.ahead.run_id, role="worker", task="card:fixture",
                           command="true", memory_limit_mib=96,
                           scope_generation=HeadRun.from_json(self.ahead.head_run).scope_generation)
        self.assertEqual(ScopedHeadLifecycle.read_owner(directory), before)
        launch.assert_not_called()

    def test_dispatcher_intent_generation_survives_repeated_preflight_and_host_restart(self) -> None:
        catalog = SimpleNamespace(head_profile=lambda _head: {"adapter": "claude"})
        for role in ("observer", "worker", "reviewer"):
            with self.subTest(role=role), mock.patch(
                "ummanu.dispatch.host._prepare_claude_provider_progress_source", side_effect=lambda run: run,
            ):
                host = CommandHostRuntime(catalog, self.root, production_runtime=SimpleNamespace())
                identity = dict(role=role, workspace=str(self.root), task_ref=TaskRef.card("card:fixture"),
                                pid_file=str(self.root / f"{role}.pid"), run_id=f"fresh-{role}")
                with mock.patch.object(host, "_head_spec", return_value=self.spec):
                    ahead = host.preflight_codex_run("fixture", **identity)
                    again = host.preflight_codex_run("fixture", **identity)
                    replacement = host.preflight_codex_run("fixture", **{**identity, "run_id": f"new-{role}"})
                self.assertTrue(ahead.scope_generation)
                self.assertEqual(again.scope_generation, ahead.scope_generation)
                self.assertNotEqual(replacement.scope_generation, ahead.scope_generation)
                # The next host has no prepared-source cache and reads the saved intent.
                next_host = CommandHostRuntime(catalog, self.root, production_runtime=SimpleNamespace())
                restored = HeadRun.from_json(ahead.to_json())
                with mock.patch.object(next_host, "_head_spec", return_value=self.spec):
                    self.assertEqual(next_host.preflight_codex_run("fixture", **identity).scope_generation, restored.scope_generation)
                substrate = SimpleNamespace(start=mock.Mock(return_value=SimpleNamespace(ok=True, run=restored)))
                with mock.patch.object(next_host, "head_runtime_for", return_value=substrate):
                    self.assertEqual(next_host._open_head_pane(restored, "fixture", "true"), restored)
                self.assertEqual(substrate.start.call_args.kwargs["scope_generation"], restored.scope_generation)

    def test_dispatcher_handoff_retains_generation_and_refuses_conflicting_incarnations(self) -> None:
        ahead = HeadRun.from_json(self.ahead.head_run)
        late = replace(ahead, handle="fixture.sock", scope_generation="")
        merged = HeadRun.from_json(merge_launch_head_run(ahead.to_json(), late.to_json()))
        self.assertEqual(merged.scope_generation, ahead.scope_generation)
        self.assertEqual(merged.handle, late.handle)
        with self.assertRaisesRegex(HostError, "identity mismatch"):
            merge_launch_head_run(ahead.to_json(), replace(late, scope_generation="replacement").to_json())

    def test_ordinary_runtime_replacement_gets_fresh_generation_after_old_scope_proof(self) -> None:
        original = HeadRun.from_json(self.ahead.head_run)
        directory = Path(self.ahead.run_dir)
        owner = ScopedHeadLifecycle(original.run_id, 96, generation=original.scope_generation)
        owner.persist(directory)
        def launched(*_args, **_kwargs):
            with JournalWriter(directory / protocol.JOURNAL_NAME, original.run_id) as journal:
                journal.append(RUN_STARTED, head_pid=222, supervisor_pid=333)
                journal.append(RUN_EXITED, head_pid=222, exit_code=0)
            return SimpleNamespace(wait=lambda: 0)
        with mock.patch("ummanu.runtime.head.local_pty.client.subprocess.Popen", side_effect=launched), mock.patch(
            "ummanu.runtime.head.local_pty.client._identity_written", return_value=True,
        ):
            receipt = self.runtime.start(self.spec, str(self.root), original.task_ref, command="true",
                                         title="fixture", run=original, role="worker")
        self.assertTrue(receipt.ok, receipt.reason)
        current = ScopedHeadLifecycle.read_owner(directory)
        self.assertNotEqual(current["generation"], original.scope_generation)
        self.assertEqual(receipt.run.scope_generation, current["generation"])
        self.assertFalse(current["cleanup_complete"])
        self.assertTrue(self.runtime.stop(receipt.run, StopInitiator(actor="fixture")).ok)


class OwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.CGROUP_ROOT", self.root / "cgroups"))

    def owner(self, run_id: str = "reused") -> ScopedHeadLifecycle:
        directory = self.root / run_id
        directory.mkdir()
        owner = ScopedHeadLifecycle(run_id, 96)
        owner.persist(directory)
        return owner

    def membership(self, owner: ScopedHeadLifecycle) -> Path:
        path = self.root / "cgroups" / "system.slice" / scope_unit(owner.run_id) / "cgroup.events"
        path.parent.mkdir(parents=True)
        path.write_text("populated 1\n")
        return path

    def test_delayed_cleanup_serializes_other_cleanup_replacement_and_stale_retry(self) -> None:
        owner = self.owner()
        membership = self.membership(owner)
        other = ScopedHeadLifecycle.from_run_dir(owner.directory)
        delayed, release = threading.Event(), threading.Event()
        errors = []
        def backend(argv, **_kwargs):
            delayed.set()
            if not release.wait(5):
                raise AssertionError("backend fixture was not released")
            membership.write_text("populated 0\n")
            return subprocess.CompletedProcess(argv, 0, stderr=b"")
        def cleanup():
            try:
                owner.stop_and_prove_empty()
            except Exception as exc:
                errors.append(exc)
        with mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.subprocess.run", side_effect=backend) as stop:
            thread = threading.Thread(target=cleanup)
            thread.start()
            try:
                self.assertTrue(delayed.wait(5))
                with self.assertRaisesRegex(MemoryScopeError, "retry cleanup"):
                    other.stop_and_prove_empty()
                replacement = ScopedHeadLifecycle(owner.run_id, 96)
                with self.assertRaisesRegex(MemoryScopeError, "retry cleanup"):
                    replacement.persist(owner.directory)
                self.assertEqual(stop.call_count, 1)
                self.assertEqual(membership.read_text(), "populated 1\n")
            finally:
                release.set()
                thread.join(5)
            self.assertFalse(thread.is_alive(), "empty proof nested the owner lock")
            self.assertEqual(errors, [])
            other.stop_and_prove_empty()  # a contended cleanup can retry after A completes
            replacement.persist(owner.directory)
            membership.write_text("populated 1\n")
            before = stop.call_count
            for stale in (owner, other):
                with self.assertRaisesRegex(MemoryScopeError, "stale cleanup"):
                    stale.stop_and_prove_empty()
            self.assertEqual(stop.call_count, before)
            self.assertEqual(membership.read_text(), "populated 1\n")
            self.assertTrue(ScopedHeadLifecycle.read_owner(owner.directory)["launch_allowed"])

    def test_interrupted_backend_retains_closed_admission_and_releases_lock_for_retry(self) -> None:
        owner = self.owner()
        membership = self.membership(owner)
        with mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.subprocess.run", side_effect=OSError("interrupted")):
            with self.assertRaises(MemoryScopeError):
                owner.stop_and_prove_empty()
        record = ScopedHeadLifecycle.read_owner(owner.directory)
        self.assertFalse(record["launch_allowed"])
        self.assertFalse(record["cleanup_complete"])
        membership.write_text("populated 0\n")
        with mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stderr=b"")):
            ScopedHeadLifecycle.from_run_dir(owner.directory).stop_and_prove_empty()
        self.assertTrue(ScopedHeadLifecycle.read_owner(owner.directory)["cleanup_complete"])

    def test_actual_retained_prestart_launch_settles_from_matching_proof_without_head_records(self) -> None:
        root = self.root / "heads"
        owner = ScopedHeadLifecycle("prestart", 96)
        membership = self.membership(owner)
        runtime = LocalPtyHeadRuntime(root, head_process_status=head_process_status, stop_timeout=0)
        spec = HeadSpec.from_profile("fixture", {"adapter": "codex", "memory_limit_mib": 96})
        pid_file = self.root / "designated.pid"
        with mock.patch("ummanu.runtime.head.local_pty.client.subprocess.Popen", return_value=SimpleNamespace(wait=lambda: 7)), mock.patch(
            "ummanu.runtime.head.local_pty.scoped_lifecycle.subprocess.run", return_value=subprocess.CompletedProcess([], 1, stderr=b"transient refusal"),
        ):
            started = runtime.start(spec, str(self.root), TaskRef.card("card:fixture"), command="true",
                                    run_id=owner.run_id, role="worker", pid_file=str(pid_file), title="fixture")
            self.assertFalse(started.ok)
            self.assertIsNotNone(started.run)
            self.assertFalse(runtime.stop(started.run, StopInitiator(actor="fixture")).ok)
        directory = protocol.run_dir_for(root, owner.run_id)
        run = HeadRun.from_json(started.run.to_json())
        self.assertEqual(run.scope_generation, ScopedHeadLifecycle.read_owner(directory)["generation"])
        self.assertEqual(run.pid_file, str(pid_file))
        self.assertFalse(pid_file.exists())
        self.assertFalse((directory / protocol.PID_FILE_NAME).exists())
        self.assertFalse((directory / protocol.JOURNAL_NAME).exists())
        membership.write_text("populated 0\n")
        with mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stderr=b"")):
            stopped = runtime.stop(run, StopInitiator(actor="fixture"))
        self.assertTrue(stopped.ok, stopped.reason)
        self.assertTrue(stopped.run.settled)
        self.assertTrue(ScopedHeadLifecycle.read_owner(directory)["cleanup_complete"])
        self.assertFalse(pid_file.exists())
        self.assertFalse((directory / protocol.JOURNAL_NAME).exists())

    def test_runtime_rejects_stale_missing_malformed_and_foreign_owner_before_any_stop(self) -> None:
        owner = self.owner()
        runtime = LocalPtyHeadRuntime(self.root, head_process_status=head_process_status, stop_timeout=0)
        run = HeadRun(run_id=owner.run_id, spec=HeadSpec.from_profile("fixture", {"adapter": "codex"}),
                      workspace=str(self.root), task_ref=TaskRef.card("card:fixture"), role="worker", scope_generation=owner.generation)
        with mock.patch.object(runtime, "_ask_to_stop") as socket, mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.subprocess.run") as backend:
            with owner.ownership():
                self.assertFalse(runtime.stop(run, StopInitiator(actor="fixture")).ok)
            pid_file = owner.directory / protocol.PID_FILE_NAME
            publish_heartbeat(str(pid_file), {"run_id": "foreign", "role": "worker", "task": "card:fixture"})
            self.assertFalse(runtime.stop(run, StopInitiator(actor="fixture")).ok)
            record = json.loads(pid_file.read_text())
            record.update(run_id=run.run_id, proc_starttime_ticks="1")  # reused PID, currently alive
            pid_file.write_text(json.dumps(record))
            self.assertFalse(runtime.stop(run, StopInitiator(actor="fixture")).ok)
            pid_file.unlink()
            owner.stop_and_prove_empty()
            replacement = ScopedHeadLifecycle(owner.run_id, 96)
            replacement.persist(owner.directory)
            self.assertFalse(runtime.stop(run, StopInitiator(actor="fixture")).ok)
            path = owner.directory / "scope-owner.json"
            path.write_text("{}")
            self.assertFalse(runtime.stop(run, StopInitiator(actor="fixture")).ok)
            path.unlink()
            self.assertFalse(runtime.stop(run, StopInitiator(actor="fixture")).ok)
            socket.assert_not_called()
            backend.assert_not_called()

    def test_settled_run_still_requires_scope_proof_and_successful_stop_is_idempotent(self) -> None:
        owner = self.owner()
        membership = self.membership(owner)
        initiator = StopInitiator(actor="original-owner")
        run = HeadRun(run_id=owner.run_id, spec=HeadSpec.from_profile("fixture", {"adapter": "codex"}),
                      workspace=str(self.root), task_ref=TaskRef.card("card:fixture"), role="worker",
                      scope_generation=owner.generation).finishing(initiator).exited()
        runtime = LocalPtyHeadRuntime(self.root, head_process_status=head_process_status, stop_timeout=0)
        with mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.subprocess.run", return_value=subprocess.CompletedProcess([], 1, stderr=b"transient refusal")):
            self.assertFalse(runtime.stop(run, StopInitiator(actor="workspace-cleanup")).ok)
        membership.write_text("populated 0\n")
        with mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stderr=b"")):
            for _ in range(2):
                stopped = runtime.stop(run, StopInitiator(actor="workspace-cleanup"))
                self.assertTrue(stopped.ok, stopped.reason)
                self.assertEqual(stopped.run, run)
                self.assertEqual(stopped.run.stopped_by, initiator)

    def test_absent_cgroup_settles_exact_reviewer_generation_without_pid_or_loaded_unit(self) -> None:
        owner = self.owner("absent-reviewer")
        initiator = StopInitiator(actor="dispatcher", reason="green review")
        run = HeadRun(run_id=owner.run_id, spec=HeadSpec.from_profile("fixture", {"adapter": "codex"}),
                      workspace=str(self.root), task_ref=TaskRef.card("card:fixture"), role="reviewer",
                      scope_generation=owner.generation).finishing(initiator)
        runtime = LocalPtyHeadRuntime(self.root, head_process_status=head_process_status, stop_timeout=0)
        with mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.subprocess.run") as backend:
            stopped = runtime.stop(run, initiator)
        self.assertTrue(stopped.ok, stopped.reason)
        self.assertTrue(stopped.run.settled)
        self.assertEqual(stopped.run.scope_generation, owner.generation)
        proof = ScopedHeadLifecycle.read_owner(owner.directory)
        self.assertFalse(proof["launch_allowed"])
        self.assertTrue(proof["cleanup_complete"])
        backend.assert_not_called()

    def test_absent_cgroup_does_not_waive_a_live_launch_group(self) -> None:
        owner = self.owner("live-launch")
        record = ScopedHeadLifecycle.read_owner(owner.directory)
        record.update(launch_pid=123456, launch_identity="fixture-start:1")
        ScopedHeadLifecycle.update_owner(owner.directory, record)
        with mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.launch_group_present", return_value=True), mock.patch(
            "ummanu.runtime.head.local_pty.scoped_lifecycle.os.killpg", side_effect=PermissionError("fixture"),
        ), mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.subprocess.run",
                      return_value=subprocess.CompletedProcess([], 1, stderr=b"refused")):
            with self.assertRaisesRegex(MemoryScopeError, "launch group"):
                owner.stop_and_prove_empty()
        self.assertFalse(ScopedHeadLifecycle.read_owner(owner.directory)["cleanup_complete"])

    def test_unreadable_membership_cannot_settle_exact_generation(self) -> None:
        owner = self.owner("unreadable-membership")
        membership = self.membership(owner)
        read = Path.read_text

        def unreadable(path, *args, **kwargs):
            if path == membership:
                raise PermissionError("membership unavailable")
            return read(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", unreadable), mock.patch(
            "ummanu.runtime.head.local_pty.scoped_lifecycle.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, stderr=b""),
        ):
            with self.assertRaisesRegex(MemoryScopeError, "verify empty head scope"):
                owner.stop_and_prove_empty()
        self.assertFalse(ScopedHeadLifecycle.read_owner(owner.directory)["cleanup_complete"])


class PoTerminalOwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.CGROUP_ROOT", self.root / "cgroups"))
        self.store = FakePoStore(FakeBoard())
        self.session, _ = self.store.claim_session(session_id="session", cli="claude", model="opus", cwd=str(self.root), cli_session_id=None, effort="high")
        self.failed, self.settled = [], []
        self.runner = PoRunner(self.store, self.root, on_failed=lambda *args: self.failed.append(args), on_settled=lambda *args: self.settled.append(args))
        self.files = self.runner.files("session", 1)
        self.files.directory.mkdir(parents=True)
        self.store.claim_turn("session", "prompt", lambda _seq: self.files.stdout)
        canonical = self.root / "canonical"
        canonical.mkdir()
        self.owner = ScopedHeadLifecycle("po-owned", 96)
        self.owner.persist(canonical)
        self.runner._scope_dir("session", 1).symlink_to(canonical, target_is_directory=True)
        self.files.stdout.write_text(json.dumps({"type": "result", "subtype": "success", "result": "late answer"}))

    def state(self):
        return self.store.turn("session", 1).state

    def answers(self):
        return [item.text for item in self.store.feed("session") if item.role == po_store.AGENT]

    def refuse(self):
        return mock.patch.object(ScopedHeadLifecycle, "stop_owned", side_effect=MemoryScopeError("transient refusal"))

    def test_failed_owner_stop_then_late_successful_waiter_and_recovery_remain_interrupted(self) -> None:
        with self.refuse(), self.assertRaises(MemoryScopeError):
            self.runner.stop_turn("session", 1)
        self.assertEqual(self.state(), po_store.RUNNING)
        self.assertEqual(self.runner._pending_outcome("session", 1)["state"], po_store.INTERRUPTED)
        self.runner._wait(self.session, 1, SimpleNamespace(wait=lambda: 0), [], self.files)
        self.assertEqual(self.state(), po_store.INTERRUPTED)
        self.assertEqual(self.answers(), [])
        self.assertEqual(self.failed, [])
        self.assertEqual(self.settled, [("session", 1)])
        self.assertEqual(self.runner.recover(rerun=True), [])
        self.assertEqual(self.runner._pending_outcome("session", 1)["state"], po_store.INTERRUPTED)

    def test_late_failure_also_consumes_owner_interruption_and_is_idempotent(self) -> None:
        with self.refuse(), self.assertRaises(MemoryScopeError):
            self.runner.stop_turn("session", 1)
        self.assertTrue(self.runner._finish("session", 1, po_store.FAILED, "late failure"))
        self.assertFalse(self.runner._finish("session", 1, po_store.FAILED, "repeated failure"))
        self.assertEqual(self.state(), po_store.INTERRUPTED)
        self.assertEqual(self.failed, [])
        self.assertEqual(self.answers(), [])

    def test_late_waiter_with_cleanup_still_unavailable_leaves_interruption_for_recovery(self) -> None:
        with self.refuse():
            with self.assertRaises(MemoryScopeError):
                self.runner.stop_turn("session", 1)
            self.runner._wait(self.session, 1, SimpleNamespace(wait=lambda: 0), [], self.files)
        self.assertEqual(self.state(), po_store.RUNNING)
        self.assertEqual(self.settled, [("session", 1)])  # waiter exit wakes bounded service recovery
        self.assertEqual(self.answers(), [])
        self.assertEqual(len(self.runner.recover(rerun=True)), 1)
        self.assertEqual(self.state(), po_store.INTERRUPTED)
        self.assertEqual(self.failed, [])
        self.assertEqual(self.answers(), [])

    def test_owner_stop_overrides_uncommitted_completion_and_recovery_publishes_no_answer(self) -> None:
        with self.refuse():
            with self.assertRaises(MemoryScopeError):
                self.runner._settle(self.session, 1, 0, self.files)
            self.assertEqual(self.runner._pending_outcome("session", 1)["state"], po_store.COMPLETED)
            with self.assertRaises(MemoryScopeError):
                self.runner.stop_turn("session", 1)
        self.assertEqual(self.state(), po_store.RUNNING)
        self.assertEqual(len(self.runner.recover(rerun=True)), 1)
        self.assertEqual(self.state(), po_store.INTERRUPTED)
        self.assertEqual(self.answers(), [])
        self.assertEqual(self.failed, [])

    def test_committed_completion_makes_a_later_stop_a_noop_and_late_failure_cannot_publish(self) -> None:
        self.runner._settle(self.session, 1, 0, self.files)
        self.assertIsNone(self.runner.stop_turn("session", 1))
        self.assertFalse(self.runner._finish("session", 1, po_store.FAILED, "late failure"))
        self.assertEqual(self.state(), po_store.COMPLETED)
        self.assertEqual(self.answers(), ["late answer"])
        self.assertEqual(self.failed, [])
        self.assertEqual(self.runner._pending_outcome("session", 1)["state"], po_store.COMPLETED)

    def test_retained_failure_beats_late_completion_and_failure_callback_names_selected_intent_once(self) -> None:
        with self.refuse(), self.assertRaises(MemoryScopeError):
            self.runner._finish("session", 1, po_store.FAILED, "original failure")
        self.runner._settle(self.session, 1, 0, self.files)
        self.runner._settle(self.session, 1, 0, self.files)
        self.assertEqual(self.state(), po_store.FAILED)
        self.assertEqual(self.failed, [("session", 1, "original failure")])
        self.assertEqual(self.answers(), [])

    def test_database_failure_keeps_intent_and_running_row_until_stop_or_recovery_commits(self) -> None:
        with mock.patch.object(self.store, "complete_turn", side_effect=po_store.PoStoreError("lost database")), self.assertRaises(po_store.PoStoreError):
            self.runner._settle(self.session, 1, 0, self.files)
        self.assertEqual(self.state(), po_store.RUNNING)
        self.assertTrue(ScopedHeadLifecycle.read_owner(self.owner.directory)["cleanup_complete"])
        self.runner.stop_turn("session", 1)
        self.assertEqual(self.state(), po_store.INTERRUPTED)
        self.assertEqual(self.answers(), [])

    def test_owner_lock_contention_retains_row_and_stop_retry_selects_interruption(self) -> None:
        with self.owner.ownership(), self.assertRaises(MemoryScopeError):
            self.runner.stop_turn("session", 1)
        self.assertEqual(self.state(), po_store.RUNNING)
        self.assertIsNone(self.runner._pending_outcome("session", 1))
        self.runner.stop_turn("session", 1)
        self.runner._settle(self.session, 1, 0, self.files)
        self.assertEqual(self.state(), po_store.INTERRUPTED)
        self.assertEqual(self.answers(), [])

    def test_relaunch_cannot_replace_the_pointer_to_retained_terminal_intent(self) -> None:
        self.files.prompt.write_text("prompt")
        with self.refuse(), self.assertRaises(MemoryScopeError):
            self.runner.stop_turn("session", 1)
        with mock.patch("ummanu.po.runner.spawn_head") as spawn:
            with self.assertRaisesRegex(RuntimeError, "retained terminal intent"):
                self.runner._scoped_launch(self.session, 1, ["true"], self.files, {},
                                          HeadSpec.from_profile("fixture", {"adapter": "claude"}))
            spawn.assert_not_called()
        self.assertEqual(self.runner._scope_dir("session", 1).resolve(), self.owner.directory)
        self.runner.recover(rerun=True)
        self.assertEqual(self.state(), po_store.INTERRUPTED)

    def test_concurrent_completion_holds_owner_through_commit_and_later_stop_is_noop(self) -> None:
        self.concurrent_settlers("completion")

    def test_concurrent_stop_holds_owner_through_commit_and_late_waiter_cannot_complete(self) -> None:
        self.concurrent_settlers("stop")

    def concurrent_settlers(self, first: str) -> None:
        other = PoRunner(self.store, self.runner.data_dir)
        entered, release = threading.Event(), threading.Event()
        errors = []
        method = "finish_turn" if first == "stop" else "complete_turn"
        original = getattr(self.store, method)
        def delayed(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError("settlement fixture was not released")
            return original(*args, **kwargs)
        def settle_first():
            try:
                if first == "stop":
                    self.runner.stop_turn("session", 1)
                else:
                    self.runner._settle(self.session, 1, 0, self.files)
            except Exception as exc:
                errors.append(exc)
        with mock.patch.object(self.store, method, delayed):
            thread = threading.Thread(target=settle_first)
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(self.state(), po_store.RUNNING)
                self.assertTrue(ScopedHeadLifecycle.read_owner(self.owner.directory)["cleanup_complete"])
                with self.assertRaisesRegex(MemoryScopeError, "retry cleanup"):
                    if first == "stop":
                        other._settle(self.session, 1, 0, self.files)
                    else:
                        other.stop_turn("session", 1)
            finally:
                release.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        if first == "stop":
            other._settle(self.session, 1, 0, self.files)
            self.assertEqual(self.state(), po_store.INTERRUPTED)
            self.assertEqual(self.answers(), [])
        else:
            self.assertIsNone(other.stop_turn("session", 1))
            self.assertEqual(self.state(), po_store.COMPLETED)
            self.assertEqual(self.answers(), ["late answer"])
        self.assertEqual(ScopedHeadLifecycle.read_owner(self.owner.directory)["outcome"]["state"], self.state())
