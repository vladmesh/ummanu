"""The PO service's turn enters the same scoped lifecycle as dispatcher heads."""

from __future__ import annotations

import functools
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ummanu.po.runner import PoRunner
from ummanu.runtime.head.local_pty import client as client_module
from ummanu.runtime.head.local_pty.client import HeadHandle, LocalPtySpawnError
from ummanu.runtime.head.local_pty.journal import RUN_EXITED, RUN_STARTED, JournalWriter
from ummanu.runtime.head.local_pty.scoped_lifecycle import ScopedHeadLifecycle
from ummanu.runtime.head.memory import scope_unit
from ummanu.runtime.head.spec import HeadSpec
from ummanu.runtime.heads import Registry


class PoScopedLaunchTests(unittest.TestCase):
    def test_service_runner_uses_its_installation_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            instance = Path(temp) / "instance"
            instance.mkdir()
            (instance / "instance.yaml").write_text(
                f"version: 1\nname: po\ndata_dir: {Path(temp) / 'data'}\n"
                "offsite:\n  instance_remote: git@example.invalid:x/y.git\n",
                encoding="utf-8",
            )
            snapshot = Path(temp) / "data" / "heads" / "heads.yaml"
            snapshot.parent.mkdir(parents=True)
            snapshot.touch()
            registry = Registry({}, {"po-claude": {
                "adapter": "claude", "model": "opus", "effort": "high", "memory_limit_mib": 3072,
            }})
            with (mock.patch("ummanu.po.runner.PoStore.for_instance", return_value=SimpleNamespace()),
                  mock.patch("ummanu.po.runner.load_registry", return_value=registry) as load):
                runner = PoRunner.for_instance(instance, Path(temp) / "data")
            # The generated pair in the instance's data directory, not the live root's legacy copy.
            load.assert_called_once_with(snapshot)
            spec = runner._head_spec(SimpleNamespace(cli="claude", model="opus", effort="high"))
            self.assertEqual((spec.profile_id, spec.memory_limit_mib), ("po-claude", 3072))

    def test_po_turn_launches_supervised_scope_and_immediate_exit_is_waitable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = SimpleNamespace(turn_request_id=lambda *_: None, record_process=lambda *_: True)
            spec = HeadSpec.from_profile("po-codex", {
                "adapter": "codex", "model": "gpt-6-sol", "effort": "high", "memory_limit_mib": 4096,
            })
            runner = PoRunner(store, root, head_specs={spec.profile_id: spec})
            session = SimpleNamespace(
                session_id="session", cli="codex", model="gpt-6-sol", effort="high", cwd=str(root),
            )
            files = runner.files("session", 1)
            files.directory.mkdir(parents=True)
            files.prompt.write_text("hello", encoding="utf-8")

            def spawn(**kwargs):
                self.assertEqual(kwargs["role"], "po")
                self.assertEqual(kwargs["memory_limit_mib"], 4096)
                self.assertEqual(kwargs["owner_unit"], "ummanu-po.service")
                self.assertIn("po-codex", kwargs["task"])
                self.assertIn("< ", kwargs["command"])
                run_dir = root / "head"
                run_dir.mkdir()
                journal = run_dir / "journal.jsonl"
                with JournalWriter(journal, kwargs["run_id"]) as writer:
                    writer.append(RUN_EXITED, head_pid=123, exit_code=None, signal=9,
                                  head_loss_reason="memory_limit")
                return HeadHandle(
                    run_dir=run_dir, run_id=kwargs["run_id"], role="po", task=kwargs["task"],
                    socket_path=run_dir / "socket", journal_path=journal, pid_file=run_dir / "head.pid",
                    supervisor_pid=456, head_pid=123,
                )

            with mock.patch("ummanu.po.runner.spawn_head", side_effect=spawn) as spawned:
                process = runner._launch(session, 1, ["/bin/true"], files)
            self.assertEqual(process.pid, 123)
            self.assertEqual(process.wait(), -9)
            self.assertEqual(process.head_loss_reason, "memory_limit")
            self.assertEqual(spawned.call_count, 1)

    def test_delayed_heartbeat_cancels_started_scope_before_turn_is_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cancelled = []
            settled = []
            store = SimpleNamespace(
                turn_request_id=lambda *_: None,
                finish_turn=lambda *_args, **_kwargs: settled.append(bool(cancelled)) or True,
                turn=lambda *_: SimpleNamespace(state="running"),
            )
            spec = HeadSpec.from_profile("po-codex", {"adapter": "codex", "memory_limit_mib": 96})
            runner = PoRunner(store, root, head_specs={spec.profile_id: spec})
            session = SimpleNamespace(
                session_id="session", cli="codex", model=None, effort="default", cwd=str(root),
            )
            files = runner.files("session", 1)
            files.directory.mkdir(parents=True)
            files.prompt.write_text("hello", encoding="utf-8")

            def start(_argv, **kwargs):
                run_dir = next((root / "po-heads").iterdir())
                with JournalWriter(run_dir / "journal.jsonl", run_dir.name) as writer:
                    writer.append(RUN_STARTED, head_pid=123, supervisor_pid=456)
                cgroup = root / "system.slice" / scope_unit(run_dir.name)
                cgroup.mkdir(parents=True)
                (cgroup / "cgroup.events").write_text("populated 1\n", encoding="ascii")
                return SimpleNamespace(wait=lambda: 0)

            def stop_scope(*args, **kwargs):
                run_dir = next((root / "po-heads").iterdir())
                cgroup = root / "system.slice" / scope_unit(run_dir.name)
                (cgroup / "cgroup.events").write_text("populated 0\n", encoding="ascii")
                cancelled.append(True)
                return SimpleNamespace(returncode=0, stderr=b"")

            def stop_head(**_kwargs):
                run_dir = next((root / "po-heads").iterdir())
                with JournalWriter(run_dir / "journal.jsonl", run_dir.name) as writer:
                    writer.append(RUN_EXITED, head_pid=123, signal=signal.SIGKILL)

            client = mock.MagicMock()
            client.__enter__.return_value.stop.side_effect = stop_head

            with (
                mock.patch("ummanu.runtime.head.local_pty.client.subprocess.Popen", side_effect=start),
                mock.patch("ummanu.runtime.head.local_pty.client._identity_written", return_value=False),
                # `spawn_head` binds SPAWN_TIMEOUT_SECONDS as its default when it is defined, so
                # patching the constant left the real 20 s wait in place. Shorten the call instead.
                mock.patch(
                    "ummanu.po.runner.spawn_head", functools.partial(client_module.spawn_head, timeout=0.02)
                ),
                mock.patch("ummanu.runtime.head.local_pty.client.SupervisorClient.connect", return_value=client),
                mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.CGROUP_ROOT", root),
                mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.subprocess.run", side_effect=stop_scope),
                self.assertRaisesRegex(RuntimeError, "did not answer"),
            ):
                runner._launch(session, 1, ["/bin/true"], files)
            self.assertEqual(settled, [True])

    def test_failed_scope_cleanup_does_not_settle_a_live_po_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = SimpleNamespace(
                turn_request_id=lambda *_: None,
                finish_turn=mock.Mock(),
            )
            runner = PoRunner(store, Path(temp))
            session = SimpleNamespace(
                session_id="session", cli="codex", model=None, effort="default", cwd=temp,
            )
            files = runner.files("session", 1)
            with (
                mock.patch(
                    "ummanu.po.runner.spawn_head",
                    side_effect=LocalPtySpawnError(
                        "cleanup_failed", "scope is still alive", cleanup_complete=False,
                    ),
                ),
                self.assertRaisesRegex(RuntimeError, "scope is still alive"),
            ):
                runner._launch(session, 1, ["/bin/true"], files)
            store.finish_turn.assert_not_called()

    def test_po_failure_stops_detached_descendants_before_settling(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = SimpleNamespace(finish_turn=mock.Mock(return_value=True), turn=lambda *_: SimpleNamespace(state="running"))
            runner = PoRunner(store, root)
            files = runner.files("session", 1)
            files.directory.mkdir(parents=True)
            owner = ScopedHeadLifecycle("detached-turn", 96)
            scope_dir = runner._scope_dir("session", 1)
            scope_dir.mkdir()
            owner.persist(scope_dir)
            cgroup = root / "system.slice" / scope_unit(owner.run_id)
            cgroup.mkdir(parents=True)
            (cgroup / "cgroup.events").write_text("populated 1\n", encoding="ascii")
            children = [subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                         start_new_session=True) for _ in range(2)]
            self.addCleanup(lambda: [child.kill() if child.poll() is None else None for child in children])
            self.addCleanup(lambda: [child.wait(timeout=5) for child in children])
            self.assertNotEqual(os.getpgid(children[0].pid), os.getpgid(children[1].pid))

            def stop(*_args, **_kwargs):
                for child in children:
                    if child.poll() is None:
                        os.kill(child.pid, signal.SIGKILL)
                for child in children:
                    child.wait(timeout=5)
                (cgroup / "cgroup.events").write_text("populated 0\n", encoding="ascii")
                return SimpleNamespace(returncode=0, stderr=b"")

            def finish(*_args, **_kwargs):
                self.assertTrue(all(child.poll() is not None for child in children))
                self.assertEqual((cgroup / "cgroup.events").read_text(), "populated 0\n")
                return True

            store.finish_turn.side_effect = finish
            with (
                mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.CGROUP_ROOT", root),
                mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.subprocess.run", side_effect=stop),
            ):
                runner._abandon("session", 1, None, "launch failed")
            store.finish_turn.assert_called_once()

    def test_po_cleanup_failure_leaves_turn_running_for_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = SimpleNamespace(finish_turn=mock.Mock(), turn=lambda *_: SimpleNamespace(state="running"))
            runner = PoRunner(store, root)
            files = runner.files("session", 1)
            files.directory.mkdir(parents=True)
            owner = ScopedHeadLifecycle("failed-cleanup", 96)
            scope_dir = runner._scope_dir("session", 1)
            scope_dir.mkdir()
            owner.persist(scope_dir)
            cgroup = root / "system.slice" / scope_unit(owner.run_id)
            cgroup.mkdir(parents=True)
            (cgroup / "cgroup.events").write_text("populated 1\n", encoding="ascii")
            with (
                mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.CGROUP_ROOT", root),
                mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.subprocess.run",
                           return_value=SimpleNamespace(returncode=1, stderr=b"failed")),
                self.assertRaisesRegex(RuntimeError, "could not stop head scope"),
            ):
                runner._abandon("session", 1, None, "launch failed")
            store.finish_turn.assert_not_called()
            self.assertEqual(ScopedHeadLifecycle.from_run_dir(scope_dir).run_id, owner.run_id)

    def test_po_stop_and_recovery_settle_only_after_scope_empty(self) -> None:
        for action in ("stop", "recovery"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                turn = SimpleNamespace(
                    session_id="session", seq=1, pid=None, process_identity=None,
                    stdout_path=str(root / "stdout"), reason=None,
                    state="running",
                )
                store = SimpleNamespace(
                    session=lambda *_: SimpleNamespace(cli="claude"),
                    running_turns=lambda *_, turn=turn: [turn], turn=lambda *_, turn=turn: turn,
                    finish_turn=mock.Mock(return_value=True),
                )
                runner = PoRunner(store, root)
                scope_dir = runner._scope_dir("session", 1)
                scope_dir.mkdir(parents=True)
                owner = ScopedHeadLifecycle(f"{action}-turn", 96)
                owner.persist(scope_dir)
                cgroup = root / "system.slice" / scope_unit(owner.run_id)
                cgroup.mkdir(parents=True)
                (cgroup / "cgroup.events").write_text("populated 1\n", encoding="ascii")

                def stop(*_args, cgroup=cgroup, **_kwargs):
                    (cgroup / "cgroup.events").write_text("populated 0\n", encoding="ascii")
                    return SimpleNamespace(returncode=0, stderr=b"")

                def finish(*_args, cgroup=cgroup, **_kwargs):
                    self.assertEqual((cgroup / "cgroup.events").read_text(), "populated 0\n")
                    return True

                store.finish_turn.side_effect = finish
                with (
                    mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.CGROUP_ROOT", root),
                    mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.subprocess.run", side_effect=stop),
                ):
                    if action == "stop":
                        self.assertIs(runner.stop_turn("session", 1), turn)
                    else:
                        self.assertTrue(runner._recover_one(turn, rerun=False))
                self.assertEqual(store.finish_turn.call_count, 1)


if __name__ == "__main__":
    unittest.main()
