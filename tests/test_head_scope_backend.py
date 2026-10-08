"""Required CI evidence from disposable system scopes, never installation heads."""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.po_cli_fakes import FAKE_CLAUDE, eventually
from tests.po_fake_store import FakeBoard, FakePoStore
from tests.scoped_environment_fixtures import deployed_scope_argv
from ummanu.dispatch.watchdog import head_process_status
from ummanu.po import PO_REQUEST_ENV, PO_SESSION_ENV, store as po_store
from ummanu.po.runner import PoRunner, turn_environment
from ummanu.po.service import PoService
from ummanu.runtime.head.local_pty.client import LocalPtySpawnError, spawn_head
from ummanu.runtime.head.local_pty.journal import RUN_EXITED, RUN_STARTED, SCOPE_BOUND
from ummanu.runtime.head.local_pty.scoped_lifecycle import ScopedHeadLifecycle, launch_identity
from ummanu.runtime.head.memory import MemoryScopeError, scope_unit
from ummanu.runtime.head.run import HeadRun, StopInitiator
from ummanu.runtime.head.spec import HeadSpec
from ummanu.runtime.head.task_ref import TaskRef
from ummanu.runtime.local_pty_head import LocalPtyHeadRuntime


def await_fact(predicate, message: str, seconds: float = 15) -> None:
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(message)
        time.sleep(0.05)


@unittest.skipUnless(os.environ.get("GITHUB_ACTIONS") == "true", "real scope evidence is required in CI")
class ScopeBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="scope-ci-")))

    def start(self, program: str):
        run_id = "ci-" + uuid.uuid4().hex[:16]
        directory = self.root / run_id
        def cleanup():
            owner = ScopedHeadLifecycle.from_run_dir(directory)
            if owner is not None:
                owner.stop_and_prove_empty()
        # Register cleanup before even attempting launch. Missing systemd, cgroup v2,
        # sudo or kernel victim records fail this required job instead of skipping it.
        self.addCleanup(cleanup)
        return spawn_head(root=self.root, run_id=run_id, role="worker", task="ci:owned-fixture",
                          command=shlex.join([sys.executable, "-u", "-c", program]),
                          memory_limit_mib=96)

    def test_active_po_self_upgrade_and_doctor_preserve_native_scope_then_lifecycle_settles(self) -> None:
        from ummanu.runtime.local_pty_head import runtime_scope_inventory

        data = self.root / "data"
        data.mkdir()
        run_id = "ci-po-self-" + uuid.uuid4().hex[:12]
        directory = data / "po-heads" / run_id
        def cleanup():
            owner = ScopedHeadLifecycle.from_run_dir(directory)
            if owner is not None:
                owner.stop_and_prove_empty()
        self.addCleanup(cleanup)
        repo = Path(__file__).resolve().parents[1]
        output_path = self.root / "head-output.log"
        command = shlex.join([sys.executable, "-u", str(repo / "tests/fixtures/scope_self_upgrade.py"),
                             str(self.root), str(data), run_id])
        # The persistent released PO emits this exact argv before it adopts new
        # source. Only the new executable owns the prospective attestation.
        with mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.scope_argv", deployed_scope_argv()):
            handle = spawn_head(
                root=data / "po-heads", run_id=run_id, role="po", task="po:ci:disposable-session:1",
                command=command + " >" + shlex.quote(str(output_path)) + " 2>&1", cwd=self.root,
                env={"PYTHONPATH": os.pathsep.join((str(repo / "src"), str(repo)))}, memory_limit_mib=256,
            )
        try:
            await_fact(lambda: (self.root / "proof.json").exists(), "PO self-upgrade did not produce preservation proof")
        except AssertionError:
            output = output_path.read_text(errors="replace")[-8192:] if output_path.exists() else "no head output"
            self.fail(f"PO self-upgrade did not produce preservation proof; head output:\n{output}")
        proof = json.loads((self.root / "proof.json").read_text())
        unit = scope_unit(run_id)
        self.assertEqual(proof["unit"], unit)
        self.assertTrue(all(value != "failed" for value in proof["results"]))
        self.assertTrue(all(name != unit for _, name in proof["effects"]))
        projected = runtime_scope_inventory(data, {unit})
        self.assertFalse(projected.errors, projected.errors)
        self.assertIn(unit, projected.scopes)
        bound = handle.events().of_kind(SCOPE_BOUND)
        self.assertEqual(len(bound), 1)
        self.assertLess(bound[0]["seq"], handle.events().of_kind(RUN_STARTED)[0]["seq"])
        self.assertEqual(bound[0]["binding"]["admitted"]["generation"], handle.scope_generation)
        self.assertEqual(bound[0]["binding"]["admitted"]["root"], str(data / "po-heads"))
        self.assertFalse(ScopedHeadLifecycle.read_owner(directory)["cleanup_complete"])
        (self.root / "finish").touch()
        await_fact(lambda: any(event.get("kind") == RUN_EXITED for event in handle.events().events),
                   "harmless PO fixture did not exit")
        owner = ScopedHeadLifecycle.from_run_dir(directory)
        owner.stop_and_prove_empty()
        self.assertTrue(ScopedHeadLifecycle.read_owner(directory)["cleanup_complete"])
        settled = runtime_scope_inventory(data, {unit})
        self.assertFalse(settled.errors, settled.errors)
        self.assertFalse(settled.scopes)
        self.assertEqual(settled.disappeared, {unit})

    def test_attestation_survives_head_and_launcher_exit_with_detached_descendants(self) -> None:
        from tests.fakes.upgrade import FakeUnitInstaller
        from tests.runtime_scope_fixtures import host_fixture
        from ummanu.host import FixtureHostSource, build_doctor_expectations
        from ummanu.host_apply import ApplyInputs, apply_host
        from ummanu.runtime.local_pty_head import runtime_scope_inventory

        data = self.root / "data"
        data.mkdir()
        run_id = "ci-retained-" + uuid.uuid4().hex[:12]
        directory = data / "heads" / run_id
        def cleanup():
            owner = ScopedHeadLifecycle.from_run_dir(directory)
            if owner is not None:
                owner.stop_and_prove_empty()
        self.addCleanup(cleanup)
        child_file = self.root / "child.pid"
        program = (
            "import os,time,pathlib; pid=os.fork(); "
            f"pathlib.Path({str(child_file)!r}).write_text(str(pid)) if pid else None; "
            "os._exit(0) if pid else None; os.setsid(); time.sleep(60)"
        )
        handle = spawn_head(root=data / "heads", run_id=run_id, role="worker", task="ci:retained",
                            command=shlex.join([sys.executable, "-u", "-c", program]),
                            cwd=self.root, memory_limit_mib=96)
        await_fact(lambda: bool(handle.events().of_kind(RUN_EXITED)), "head did not journal its exit")
        owner = ScopedHeadLifecycle.from_run_dir(directory)
        launch_record = owner.read_owner(directory)
        await_fact(lambda: launch_identity(launch_record["launch_pid"]) is None, "original launcher did not exit")
        self.assertTrue(child_file.exists())
        unit = scope_unit(run_id)
        projected = runtime_scope_inventory(data, {unit})
        self.assertFalse(projected.errors, projected.errors)
        self.assertTrue(projected.scopes[unit]["populated"])
        with owner.ownership() as record:
            record["launch_allowed"] = False
            owner.update_owner(directory, record)
        before = (directory / "scope-owner.json").read_bytes()
        instance, packaged, desired, fixture = host_fixture(self.root, data, unit)
        expected = build_doctor_expectations(instance, [], packaged=packaged, data_dir=data)
        collected = FixtureHostSource(fixture).collect(expected)
        self.assertFalse(collected.errors, collected.errors)
        for dry in (True, False):
            installer = FakeUnitInstaller()
            result = apply_host(ApplyInputs(instance, [], collected.inventory, desired,
                                           data / "host-managed.json", packaged), units=installer, dry_run=dry)
            self.assertFalse(result.errors, result.errors)
            self.assertEqual(result.preserved_runtime_scopes, [unit])
            self.assertTrue(all(name != unit for _, name in installer.calls))
            self.assertEqual((directory / "scope-owner.json").read_bytes(), before)
        for field, value in (("generation", "substituted-generation"),
                             ("workspace", str(self.root / "substituted-workspace"))):
            owner.update_owner(directory, {**record, field: value})
            self.assertTrue(runtime_scope_inventory(data, {unit}).errors, field)
            owner.update_owner(directory, record)
        owner.stop_and_prove_empty()
        self.assertTrue(owner.read_owner(directory)["cleanup_complete"])
        settled = runtime_scope_inventory(data, {unit})
        self.assertFalse(settled.errors, settled.errors)
        self.assertEqual(settled.disappeared, {unit})

    def test_deployed_po_producer_new_launcher_preserves_path_and_runtime_bindings(self) -> None:
        bin_dir = self.root / "prepared cli tools"
        bin_dir.mkdir()
        result = self.root / "environment.json"
        fake = bin_dir / "prepared-only-cli"
        keys = ["PATH", "PYTHONPATH", "HOME", "CODEX_HOME", "BOARD_ACTOR",
                "SCOPED_CONFIG_SENTINEL", "SUPPLIED_EMPTY", PO_SESSION_ENV, PO_REQUEST_ENV]
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json,os,pathlib,subprocess,sys,ummanu\n"
            "control=subprocess.run(['python3','-P','-m','ummanu','--help'],capture_output=True)\n"
            "pathlib.Path(sys.argv[1]).write_text(json.dumps({"
            f"'environment':{{k:os.environ[k] for k in {keys!r} if k in os.environ}},"
            "'python':sys.executable,'source':ummanu.__file__,"
            "'uid':os.getuid(),'gid':os.getgid(),'groups':os.getgroups(),"
            "'control_exit':control.returncode,'args':sys.argv[2:]}))\n"
        )
        fake.chmod(0o700)
        environment = turn_environment({
            "PATH": str(bin_dir) + ":/usr/bin:/bin",
            "HOME": str(self.root / "runtime home"),
            "CODEX_HOME": str(self.root / "codex home"),
            "SCOPED_CONFIG_SENTINEL": "safe spaces ' ; $(touch NEVER) `touch NEVER`",
            "SUPPLIED_EMPTY": "",
        })
        store = SimpleNamespace(turn_request_id=lambda *_: "disposable-request")
        runner = PoRunner(store, self.root / "data", env=environment, scope_owner_unit="")
        session = SimpleNamespace(session_id="disposable-session", cli="codex", model=None,
                                  effort="high", cwd=str(self.root))
        files = runner.files(session.session_id, 1)
        files.directory.mkdir(parents=True)
        files.prompt.write_text("harmless input")
        def cleanup():
            owner = ScopedHeadLifecycle.from_run_dir(runner._scope_dir(session.session_id, 1))
            if owner is not None:
                owner.stop_and_prove_empty()
        self.addCleanup(cleanup)
        # This is the deployed producer's actual function from the 64c42d7 Git
        # object. The launcher process imports only the candidate source. No live
        # PO process/receipt, provider, credential or installed state is touched.
        with mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.scope_argv",
                        deployed_scope_argv()):
            process = runner._scoped_launch(
                session, 1, [fake.name, str(result), "argument ; $(touch NEVER)", ""], files,
                runner.session_environment(session, 1),
                HeadSpec.from_profile("ci-po", {"adapter": "codex", "memory_limit_mib": 256}),
            )
        self.assertEqual(process.wait(timeout=15), 0)
        actual = json.loads(result.read_text())
        for key, value in environment.items():
            self.assertEqual(actual["environment"][key], value, key)
        self.assertEqual(actual["environment"][PO_SESSION_ENV], session.session_id)
        self.assertEqual(actual["environment"][PO_REQUEST_ENV], "disposable-request")
        self.assertNotIn("FOREIGN_INSTALLATION", actual["environment"])
        self.assertEqual(actual["control_exit"], 0)
        self.assertEqual(actual["args"], ["argument ; $(touch NEVER)", ""])
        self.assertEqual(Path(actual["python"]).parent, Path(sys.executable).parent)
        self.assertTrue(actual["source"].startswith(environment["PYTHONPATH"].split(":")[0]))
        self.assertEqual((actual["uid"], actual["gid"], sorted(actual["groups"])),
                         (os.getuid(), os.getgid(), sorted(os.getgroups())))
        self.assertFalse((self.root / "NEVER").exists())
        owner = ScopedHeadLifecycle.from_run_dir(process.handle.run_dir)
        self.assertTrue(owner.read_owner(process.handle.run_dir)["launch_allowed"])
        owner.stop_and_prove_empty()
        self.assertTrue(owner.read_owner(process.handle.run_dir)["cleanup_complete"])
        # No prepared values are allowed into bootstrap argv, scope properties or
        # persistent diagnostics. The result is private disposable test evidence.
        for name in ("scope-owner.json", "supervisor.log", "journal.jsonl"):
            path = process.handle.run_dir / name
            if path.exists():
                self.assertNotIn(environment["SCOPED_CONFIG_SENTINEL"], path.read_text())

    def test_detached_descendant_stop_failure_retains_owner_and_real_retry_empties_scope(self) -> None:
        child_file = self.root / "detached.pid"
        handle = self.start(
            "import subprocess,sys,time,pathlib; "
            "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],start_new_session=True); "
            f"pathlib.Path({str(child_file)!r}).write_text(str(p.pid));time.sleep(30)"
        )
        await_fact(child_file.exists, "detached child was not created")
        child = int(child_file.read_text())
        self.assertNotEqual(os.getpgid(child), os.getpgid(handle.head_pid))
        run = HeadRun(run_id=handle.run_id, spec=HeadSpec.from_profile("fixture", {"adapter": "codex"}),
                      workspace=str(self.root), task_ref=TaskRef.card("ci:owned-fixture"), role="worker",
                      pid_file=str(handle.pid_file), scope_generation=handle.scope_generation)
        runtime = LocalPtyHeadRuntime(self.root, head_process_status=head_process_status, stop_timeout=3)
        real_run = subprocess.run
        def refuse_stop(argv, **kwargs):
            if argv[:4] == ["sudo", "-n", "systemctl", "stop"]:
                return subprocess.CompletedProcess(argv, 1, stderr=b"injected transient stop failure")
            return real_run(argv, **kwargs)
        with mock.patch("ummanu.runtime.head.local_pty.scoped_lifecycle.subprocess.run", side_effect=refuse_stop):
            self.assertFalse(runtime.stop(run, StopInitiator(actor="ci-owner"), signal_name="KILL").ok)
        os.kill(child, 0)
        owner = ScopedHeadLifecycle.from_run_dir(handle.run_dir)
        self.assertIsNotNone(owner)
        self.assertTrue(runtime.stop(run, StopInitiator(actor="ci-owner"), signal_name="KILL").ok)
        cgroup = Path("/sys/fs/cgroup/system.slice") / scope_unit(handle.run_id)
        self.assertTrue(not cgroup.exists() or "populated 0" in (cgroup / "cgroup.events").read_text().splitlines())

    def test_head_sigkill_then_child_group_oom_before_any_supervisor_observation_is_untyped(self) -> None:
        ready, pressure = self.root / "ready", self.root / "pressure"
        program = f'''import os,pathlib,time
pid=os.fork()
if pid:
    pathlib.Path({str(ready)!r}).write_text(str(pid))
    time.sleep(30)
else:
    os.setsid()
    deadline=time.monotonic()+15
    while not pathlib.Path({str(pressure)!r}).exists() and time.monotonic()<deadline:
        time.sleep(.02)
    data=bytearray(192*1024*1024)
    data[::4096]=b'x'*(len(data)//4096)
    time.sleep(5)
'''
        handle = self.start(program)
        await_fact(ready.exists, "child pressure fixture was not ready")
        os.kill(handle.supervisor_pid, signal.SIGSTOP)
        await_fact(lambda: Path(f"/proc/{handle.supervisor_pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "T",
                   "supervisor observation was not paused")
        os.kill(handle.head_pid, signal.SIGKILL)
        await_fact(lambda: Path(f"/proc/{handle.head_pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z",
                   "unrelated head SIGKILL was not complete")
        pressure.touch()
        cgroup = Path("/sys/fs/cgroup/system.slice") / scope_unit(handle.run_id)
        await_fact(lambda: int(dict(line.split() for line in (cgroup / "memory.events.local").read_text().splitlines())["oom_group_kill"]) > 0,
                   "surviving child did not cause a real group OOM")
        self.assertFalse(handle.events().of_kind(RUN_EXITED))
        os.kill(handle.supervisor_pid, signal.SIGCONT)
        await_fact(lambda: bool(handle.events().of_kind(RUN_EXITED)), "delayed supervisor did not journal exit")
        exited = handle.events().of_kind(RUN_EXITED)[-1]
        self.assertEqual(exited["signal"], signal.SIGKILL)
        self.assertNotIn("head_loss_reason", exited)


@unittest.skipUnless(os.environ.get("GITHUB_ACTIONS") == "true", "real PO scope recovery is required in CI")
class PoScopeBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="po-scope-ci-")))
        self.data = self.root / "data"
        executable = self.root / "claude-fixture"
        executable.write_text(FAKE_CLAUDE)
        executable.chmod(0o700)
        self.store = FakePoStore(FakeBoard())
        self.runner = PoRunner(
            self.store, self.data, executables={"claude": str(executable)},
            env={**os.environ, "FAKE_LOG": str(self.root / "fake.log")}, scope_owner_unit="",
        )
        self.runner.workspace.mkdir(parents=True)
        self.service = PoService(self.runner, data_dir=self.data, models={"claude": ("opus",)})
        self.service.start()
        thread = threading.Thread(target=self.service.run, kwargs={"tick": .05, "say": lambda _: None})
        thread.start()
        self.addCleanup(self.cleanup_scopes)
        self.addCleanup(thread.join, 10)
        self.addCleanup(self.service.stop)

    def cleanup_scopes(self) -> None:
        for directory in (self.data / "po-heads").glob("*"):
            owner = ScopedHeadLifecycle.from_run_dir(directory)
            if owner is not None:
                owner.stop_and_prove_empty()
        with self.runner._lock:
            waiters = list(self.runner._live.values())
        for live in waiters:
            live.thread.join(5)

    def session(self, request_id: str) -> str:
        return self.service.create_session(cli="claude", model="opus", effort="high", request_id=request_id)["session_id"]

    def settled(self, session_id: str, seq: int):
        # Recovery settles the orphan before the service pump creates the queued turn.
        await_fact(
            lambda: any(turn.seq == seq and turn.state != po_store.RUNNING
                        for turn in self.store.turns(session_id)),
            "scoped turn did not settle",
        )
        return self.store.turn(session_id, seq)

    def test_new_orphan_retries_real_scope_cleanup_and_releases_session_without_restart(self) -> None:
        service = self.service
        session_id = self.session("scope-session")
        other = self.session("other-scope-session")
        original_cleanup = ScopedHeadLifecycle.stop_owned
        released = False
        failed_runs = []
        attempts = []
        child_file = self.root / "po-detached.pid"
        def stop(owner, record):
            if owner.run_id in failed_runs and not released:
                attempts.append(owner.run_id)
                raise MemoryScopeError("injected transient cleanup failure")
            original_cleanup(owner, record)
        def launch(session, seq, argv, files, environment, _spec):
            spec = HeadSpec.from_profile("ci-po", {"adapter": session.cli, "memory_limit_mib": 96})
            if session.session_id == session_id and not failed_runs:
                wrapper = (
                    "import subprocess,sys,os,pathlib; "
                    "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'], "
                    "start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
                    f"pathlib.Path({str(child_file)!r}).write_text(str(p.pid)); "
                    "os.execvp(sys.argv[1],sys.argv[1:])"
                )
                argv = [sys.executable, "-c", wrapper, *argv]
            process = service.runner._scoped_launch(session, seq, argv, files, environment, spec)
            if session.session_id == session_id and not failed_runs:
                failed_runs.append(process.handle.run_id)
                raise LocalPtySpawnError("cleanup_failed", "fixture launch lost its waiter", cleanup_complete=False)
            return process
        service.runner._turn_launcher = launch
        with mock.patch.object(ScopedHeadLifecycle, "stop_owned", stop):
            service.submit(session_id=session_id, text="GATE orphan", request_id="scope-orphan")
            service.submit(session_id=session_id, text="after cleanup", request_id="scope-after")
            self.assertEqual(self.store.turn(session_id, 1).state, po_store.RUNNING)
            service.submit(session_id=other, text="independent", request_id="scope-independent")
            self.assertEqual(self.settled(other, 1).state, po_store.COMPLETED)
            eventually(lambda: bool(attempts), "new orphan was not automatically revisited")
            await_fact(child_file.exists, "PO detached child was not created")
            os.kill(int(child_file.read_text()), 0)
            released = True
            self.assertEqual(self.settled(session_id, 1).state, po_store.FAILED)
            self.assertEqual(self.settled(session_id, 2).state, po_store.COMPLETED)
        self.assertFalse(service.runner.orphaned_turns())
        cgroup = Path("/sys/fs/cgroup/system.slice") / scope_unit(failed_runs[0])
        self.assertTrue(not cgroup.exists() or "populated 0" in (cgroup / "cgroup.events").read_text().splitlines())
