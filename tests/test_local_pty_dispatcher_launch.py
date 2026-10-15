"""secretary-1698: a head the dispatcher raises on `local-pty` runs its command, under one heartbeat.

The dispatcher wrapped a head's command in `with_pid_heartbeat` and handed it to a backend whose
supervisor wraps what it is handed a second time. The shell text then read `writer; exec env writer;
exec env head`: the second statement `exec`ed the inner writer, which exited 0, and the head never
ran (issue:c91642ca362fd25bf433, head-run 0dbbaa7eb1524e5fa32630774f6860b8: `head_exited` after 140 ms).

So these tests go through the dispatcher's own launch code — `prepare_observer` for the observer,
`_launch` for a worker and a reviewer — onto a real supervisor, a real pty and a real process. The
head is a harmless stand-in that records its pid and argv and stays up. What is asserted is what a
tick later depends on: the journal says the head command it was given is the whole head command,
that head is the process running, and the pid file the dispatcher reads classifies it as a live match
for the identity the dispatcher expects, with the observer's task spelled `sprint:<ID>` once.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

from tests.fakes.dispatcher import FakeCatalog
from tests.production_runtime_fixtures import registered_production_runtime
from ummanu.dispatch.head_status import head_status
from ummanu.dispatch.heartbeat import heartbeat_identity, sprint_task
from ummanu.dispatch.host import CommandHostRuntime
from ummanu.dispatch.observer import (
    ObserverRecord,
    observer_head_is_dead,
    observer_head_status,
    put_observers,
)
from ummanu.dispatch.state import DispatcherRecord
from ummanu.dispatch.watchdog import (
    HEARTBEAT_IDENTITY_MISMATCH,
    HEARTBEAT_LIVE_MATCH,
    head_process_status,
    head_run_process_status,
)
from ummanu.dispatch.worker_lifecycle import WorkerContinuation, WorkerContinuationStage
from ummanu.runtime.head import HeadCommand, HeadRun, HeadSpec, TaskRef
from ummanu.runtime.head.command import with_pid_heartbeat
from ummanu.runtime.head.identity import publish_heartbeat
from ummanu.runtime.head.local_pty import protocol
from ummanu.runtime.head.local_pty.journal import RUN_STARTED, read_events
from ummanu.runtime.head_runtimes import LOCAL_PTY_RUNTIME
from ummanu.runtime.tui_delivery import READINESS_BUSY, delivery_readiness_state as _delivery_readiness_state

PROFILE = "claude-local-pty"
#: The stand-in head: it writes down which process it is and what it was run with, then stays up.
STAND_IN = """import json, os, sys, time
with open(sys.argv[1] + '.tmp', 'w', encoding='utf-8') as handle:
    json.dump({'pid': os.getpid(), 'argv': sys.argv[1:]}, handle)
os.replace(sys.argv[1] + '.tmp', sys.argv[1])
deadline = time.monotonic() + 120
while time.monotonic() < deadline:
    time.sleep(0.2)
"""
#: One heartbeat writer is one of these in the shell text a head's `/bin/sh -c` runs.
WRITER_MARK = "path, pid, identity = sys.argv[1:]"


class _StandInCatalog(FakeCatalog):
    """A `local-pty` Claude profile whose rendered head command is the stand-in."""

    def __init__(self, command: str) -> None:
        super().__init__()
        self.profiles[PROFILE] = {
            "adapter": "claude",
            "model": "opus",
            "resource": "claude-sub",
            "runtime": LOCAL_PTY_RUNTIME,
        }
        self.command = command

    def head_launch(
        self,
        head: str,
        prompt_file: str,
        *,
        workspace: str,
        role: str,
        launch_prompt: str | None = None,
        identity: dict[str, str] | None = None,
        local_run_policy: str | None = None,
    ) -> HeadCommand:
        return HeadCommand(self.command, adapter="claude")

    def prepare_head_workspace(self, head: str, workspace: str, *, role: str) -> None:
        return None


def _alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return False
    return stat[stat.rfind(")") + 2 :].split()[:1] != ["Z"]


def _kill(pid: int, *, group: bool = False) -> None:
    if pid <= 0:
        return
    try:
        os.killpg(pid, signal.SIGKILL) if group else os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


class LocalPtyDispatcherLaunchTests(unittest.TestCase):
    def setUp(self) -> None:
        # /tmp rather than the workspace: the run directory holds a Unix socket, whose address the
        # kernel bounds at about a hundred bytes.
        self.root = Path(tempfile.mkdtemp(prefix="lp-dispatch-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.addCleanup(self._reap)
        self.record = self.root / "stand-in.json"
        self.command = (
            f"{shlex.quote(sys.executable)} -c {shlex.quote(STAND_IN)} {shlex.quote(str(self.record))}"
        )
        environment = mock.patch.dict(
            os.environ,
            {
                "UMMANU_DISPATCHER_WORKSPACES_ROOT": str(self.root / "orca-workspaces"),
                "UMMANU_DISPATCHER_BODY_DIR": str(self.root / "bodies"),
                "UMMANU_CLAUDE_PROJECTS": str(self.root / "claude-projects"),
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.host = CommandHostRuntime(_StandInCatalog(self.command), self.root / "data", mode="real")  # type: ignore[arg-type]

    # -- processes -----------------------------------------------------------------------------

    def _run_dirs(self) -> list[Path]:
        heads = self.root / "data" / "heads"
        return sorted(path for path in heads.glob("*") if path.is_dir()) if heads.exists() else []

    def _started(self, run_id: str) -> dict[str, Any]:
        events = read_events(self.root / "data" / "heads" / run_id / protocol.JOURNAL_NAME).events
        started = [event for event in events if event.get("kind") == RUN_STARTED]
        self.assertEqual(len(started), 1, "one bring-up is one run.started")
        return started[0]

    def _reap(self) -> None:
        for run_dir in self._run_dirs():
            for event in read_events(run_dir / protocol.JOURNAL_NAME).events:
                if event.get("kind") == RUN_STARTED:
                    _kill(int(event.get("head_pid") or 0), group=True)
                    _kill(int(event.get("head_pid") or 0))
                    _kill(int(event.get("supervisor_pid") or 0))

    def _stand_in(self) -> dict[str, Any]:
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            try:
                return json.loads(self.record.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                time.sleep(0.02)
        self.fail("the head command never ran: the stand-in wrote no record of itself")

    # -- what every launch path owes -----------------------------------------------------------

    def _assert_the_head_command_runs(self, run: HeadRun, pid_file: str) -> int:
        started = self._started(run.run_id)
        self.assertEqual(
            started["command"],
            self.command,
            "the runtime was handed something other than the head command",
        )
        # The supervisor adds the one writer to what it was handed, so this is the shell text.
        self.assertEqual(with_pid_heartbeat(started["command"], pid_file).count(WRITER_MARK), 1)
        stand_in = self._stand_in()
        self.assertEqual(stand_in["argv"], [str(self.record)], "the process running is not the head")
        self.assertEqual(stand_in["pid"], started["head_pid"], "the heartbeat's `$$` is not the head")
        # The failure this card fixes ended the head within 140 ms; this one is still up after that.
        time.sleep(0.5)
        self.assertTrue(_alive(stand_in["pid"]), "the head exited after its bring-up")
        self.assertFalse(
            (self.root / "data" / "heads" / run.run_id / protocol.PID_FILE_NAME).exists(),
            "a second heartbeat record was written beside the one the dispatcher reads",
        )
        return int(stand_in["pid"])

    # -- the observer ----------------------------------------------------------------------------

    def test_an_observer_raised_on_local_pty_runs_its_head_and_reads_as_a_live_match(self) -> None:
        workspace = self.root / "workspaces" / "observer"
        workspace.mkdir(parents=True)
        with mock.patch.object(CommandHostRuntime, "_create_git_observer_workspace", return_value=workspace):
            launched = self.host.prepare_observer({"ref": "sprint:1459"}, PROFILE, prompt="# Sprint\n")

        run = HeadRun.from_json(launched["head_run"])
        self.assertEqual(run.spec.runtime, LOCAL_PTY_RUNTIME)
        self.assertEqual(launched["pid_file"], self.host.observer_pid_file("sprint:1459"))
        head = self._assert_the_head_command_runs(run, launched["pid_file"])

        record = ObserverRecord(
            sprint="sprint:1459",
            workspace=str(workspace),
            leaf=str(launched["leaf"]),
            pid_file=str(launched["pid_file"]),
            head_run=dict(launched["head_run"]),
        )
        status = observer_head_status(record)
        self.assertEqual(status["state"], HEARTBEAT_LIVE_MATCH, status)
        self.assertEqual(status["pid"], head)
        self.assertEqual(status["record"]["task"], "sprint:1459", "the observer's task is prefixed twice")
        self.assertEqual(status["record"]["run_id"], run.run_id)
        self.assertEqual(status["record"]["leaf"], run.leaf)
        handoff = json.loads(Path(f"{launched['pid_file']}.leaf").read_text(encoding="utf-8"))
        self.assertEqual(handoff["expected"]["task"], "sprint:1459")

    # -- a worker and a reviewer -------------------------------------------------------------------

    def _launch_card_head(self, role: str) -> tuple[Any, str]:
        workspace = self.root / "workspaces" / role
        workspace.mkdir(parents=True)
        (workspace / "TASK.md").write_text("# Task\n", encoding="utf-8")
        with (
            mock.patch.object(CommandHostRuntime, "_require_production_runtime"),
            mock.patch.object(CommandHostRuntime, "_require_workspace_environment"),
        ):
            launched = self.host._launch(
                str(workspace),
                f"ummanu-9001 {role}",
                PROFILE,
                str(workspace / "TASK.md"),
                role=role,
                env_name="UMMANU_1698_NO_COMMAND_OVERRIDE",
                task={"ref": "ummanu-9001"},
            )
        kind = "review" if role == "reviewer" else "worker"
        return launched, str(Path(os.environ["UMMANU_DISPATCHER_BODY_DIR"]) / f"ummanu-{kind}-pid-")

    def test_a_worker_and_a_reviewer_raised_on_local_pty_run_their_heads(self) -> None:
        for role in ("worker", "reviewer"):
            with self.subTest(role=role):
                self.record.unlink(missing_ok=True)
                launched, prefix = self._launch_card_head(role)
                run = HeadRun.from_json(launched.head_run)
                self.assertEqual(run.spec.runtime, LOCAL_PTY_RUNTIME)
                self.assertTrue(run.pid_file.startswith(prefix), run.pid_file)
                head = self._assert_the_head_command_runs(run, run.pid_file)

                status = head_run_process_status(
                    run.pid_file,
                    run=launched.head_run,
                    role=role,
                    leaf=launched.leaf,
                )
                self.assertEqual(status["state"], HEARTBEAT_LIVE_MATCH, status)
                self.assertEqual(status["pid"], head)
                self.assertEqual(status["record"]["task"], "card:ummanu-9001")
                self.assertEqual(status["record"]["role"], role)
                self._reap()


    def test_head_status_reads_a_local_pty_worker_and_reviewer_from_their_supervisors(self) -> None:
        """secretary-1701: a card's supervised heads are read like the observer, and Orca is not asked."""
        workspace = self.root / "workspaces" / "card"
        workspace.mkdir(parents=True)
        (workspace / "TASK.md").write_text("# Task\n", encoding="utf-8")
        launched = {}
        for role in ("worker", "reviewer"):
            with (
                mock.patch.object(CommandHostRuntime, "_require_production_runtime"),
                mock.patch.object(CommandHostRuntime, "_require_workspace_environment"),
            ):
                launched[role] = self.host._launch(
                    str(workspace),
                    f"ummanu-9001 {role}",
                    PROFILE,
                    str(workspace / "TASK.md"),
                    role=role,
                    env_name="UMMANU_1698_NO_COMMAND_OVERRIDE",
                    task={"ref": "ummanu-9001"},
                )
        worker, reviewer = (HeadRun.from_json(launched[role].head_run) for role in ("worker", "reviewer"))
        record = DispatcherRecord(
            worker="worker-1",
            workspace=str(workspace),
            handle=launched["worker"].handle,
            head=PROFILE,
            review_head=PROFILE,
            attempt_id="attempt-1",
            comment_baseline=0,
            review_baseline=0,
            claimed_at=0.0,
            worker_leaf=launched["worker"].leaf,
            worker_pid_file=worker.pid_file,
            worker_head_run=dict(launched["worker"].head_run),
            review_handle=launched["reviewer"].handle,
            review_leaf=launched["reviewer"].leaf,
            review_pid_file=reviewer.pid_file,
            review_head_run=dict(launched["reviewer"].head_run),
            state="review",
        )
        runtime = mock.Mock()
        runtime.host = self.host
        runtime.data_dir = self.root / "data"
        runtime.production_state.load.return_value = {}
        runtime.production_state.records.return_value = {"ummanu-9001": record}

        def no_orca(argv: Any, **_kwargs: Any) -> Any:
            self.fail(f"head-status called {argv!r} for a workspace whose heads are all supervised")

        with mock.patch("ummanu._proc.run", no_orca):
            deadline = time.monotonic() + 15.0
            while True:
                answer = head_status(runtime, workspace=str(workspace))
                if all(row["process"]["state"] == HEARTBEAT_LIVE_MATCH for row in answer["heads"]):
                    break
                self.assertLess(time.monotonic(), deadline, answer)
                time.sleep(0.05)

        self.assertNotIn("pane_channel", answer)
        rows = {row["role"]: row for row in answer["heads"]}
        self.assertEqual(sorted(rows), ["reviewer", "worker"])
        for role, run in (("worker", worker), ("reviewer", reviewer)):
            with self.subTest(role=role):
                row = rows[role]
                started = self._started(run.run_id)
                self.assertEqual((row["runtime"], row["run_id"]), (LOCAL_PTY_RUNTIME, run.run_id))
                self.assertEqual(row["head"], "alive")
                self.assertEqual(row["process"]["pid"], started["head_pid"])
                self.assertTrue(row["supervisor"]["answered"], row["supervisor"])
                self.assertTrue(row["supervisor"]["alive"])
                for key in ("turn", "turn_open", "draining", "stopping"):
                    self.assertIn(key, row["supervisor"])
                self.assertEqual(row["lease"]["state"], "held")
                self.assertEqual(row["lease"]["holder_pid"], started["supervisor_pid"])
                self.assertIn(RUN_STARTED, [event["kind"] for event in row["journal"]["tail"]])
                self.assertEqual(row["unavailable_sources"], [])
                self.assertIn(f"local-pty run {run.run_id}", row["summary"])


FAKE_TUI = Path(__file__).resolve().parent / "fixtures" / "local_pty_fake_tui.py"


class _PromptAfterStartCatalog(_StandInCatalog):
    """The same profile, rendered as the dispatcher renders every Claude head: prompt after start."""

    def head_launch(self, head: str, prompt_file: str, **_options: Any) -> HeadCommand:
        return HeadCommand(self.command, prompt_after_start=True, adapter="claude")


class LocalPtyObserverPromptTests(unittest.TestCase):
    """issue:70562b15a7dc8764437e: a local-pty observer is given its launch prompt and its wakes.

    Through the dispatcher's own code — `prepare_observer` and `nudge_observer` — onto a real
    supervisor and a fake agent that keeps a composer. The launch prompt sat unsent in Claude's
    composer, and a wake never reached the head at all, because the wake looked the observer up in
    Orca's pane inventory, where a supervised head is never listed.
    """

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="lp-prompt-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.addCleanup(self._reap)
        self.record = self.root / "submitted.jsonl"
        command = f"{shlex.quote(sys.executable)} -u {shlex.quote(str(FAKE_TUI))} {shlex.quote(str(self.record))}"
        patches = [
            mock.patch.dict(
                os.environ,
                {
                    "UMMANU_DISPATCHER_WORKSPACES_ROOT": str(self.root / "orca-workspaces"),
                    "UMMANU_DISPATCHER_BODY_DIR": str(self.root / "bodies"),
                    "UMMANU_CLAUDE_PROJECTS": str(self.root / "claude-projects"),
                },
            ),
            mock.patch("ummanu.runtime.local_pty_head.PROMPT_QUIET_SECONDS", 0.5),
            mock.patch("ummanu.runtime.local_pty_head.PROMPT_POLL_SECONDS", 0.05),
            mock.patch("ummanu.runtime.local_pty_head.SUBMIT_CONFIRM_SECONDS", 3.0),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.host = CommandHostRuntime(_PromptAfterStartCatalog(command), self.root / "data", mode="real")  # type: ignore[arg-type]

    def _reap(self) -> None:
        heads = self.root / "data" / "heads"
        for run_dir in heads.glob("*") if heads.exists() else ():
            for event in read_events(run_dir / protocol.JOURNAL_NAME).events:
                if event.get("kind") == RUN_STARTED:
                    _kill(int(event.get("head_pid") or 0), group=True)
                    _kill(int(event.get("head_pid") or 0))
                    _kill(int(event.get("supervisor_pid") or 0))

    def _submitted(self) -> list[str]:
        if not self.record.exists():
            return []
        return [json.loads(line)["submitted"].strip() for line in self.record.read_text().splitlines() if line]

    def _launch(self) -> tuple[dict[str, Any], ObserverRecord]:
        workspace = Path(self.host.observer_workspace("sprint:1459"))
        workspace.mkdir(parents=True)
        with mock.patch.object(CommandHostRuntime, "_create_git_observer_workspace", return_value=workspace):
            launched = self.host.prepare_observer({"ref": "sprint:1459"}, PROFILE, prompt="# Sprint\n")
        record = ObserverRecord(
            sprint="sprint:1459",
            head=PROFILE,
            workspace=str(workspace),
            handle=str(launched["handle"]),
            leaf=str(launched["leaf"]),
            pid_file=str(launched["pid_file"]),
            head_run=dict(launched["head_run"]),
        )
        return launched, record

    def _await_turn_end(self, record: ObserverRecord) -> None:
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if self.host.observer_status(record).get("idle"):
                return
            time.sleep(0.05)
        self.fail("the observer's turn never ended")

    def test_the_launch_prompt_is_submitted_and_a_wake_reaches_the_head(self) -> None:
        launched, record = self._launch()
        self.assertTrue(launched["prompt_delivered"])
        self.assertTrue(launched["delivery_evidence"].get("turn_confirmed"), launched["delivery_evidence"])
        workspace = Path(record.workspace)
        self.assertEqual(self._submitted(), [f"Read {workspace}/SPRINT.md and do its task."])

        self._await_turn_end(record)
        delivered = self.host.nudge_observer(record, sprint={"ref": "sprint:1459", "comments": []}, change="sprint-entity")
        self.assertTrue(delivered.evidence.turn_confirmed)
        submitted = self._submitted()
        self.assertEqual(len(submitted), 2, submitted)
        self.assertIn("SPRINT.md", submitted[1])

    def test_head_status_reads_a_local_pty_observer_from_its_own_backend(self) -> None:
        _launched, record = self._launch()
        payload: dict[str, Any] = {}
        put_observers(payload, {"sprint:1459": record})
        runtime = mock.Mock()
        runtime.host.mode = "real"
        runtime.data_dir = self.root / "data"
        runtime.production_state.load.return_value = payload
        runtime.production_state.records.return_value = {}
        answer = head_status(runtime, workspace=record.workspace)

        self.assertEqual(len(answer["heads"]), 1, answer)
        row = answer["heads"][0]
        self.assertEqual((row["kind"], row["runtime"], row["run_id"]), ("observer", LOCAL_PTY_RUNTIME, record.head_run["run_id"]))
        self.assertEqual(row["process"]["state"], HEARTBEAT_LIVE_MATCH)
        self.assertTrue(row["supervisor"]["alive"])
        self.assertIn("input.accepted", [event["kind"] for event in row["journal"]["tail"]])
        self.assertIn("local-pty run", answer["summary"][0])

    def test_a_wake_to_a_working_observer_is_a_busy_wait_not_a_failed_wake(self) -> None:
        _launched, record = self._launch()
        # The head is still printing the turn its launch prompt started.
        with self.assertRaises(Exception) as caught:
            self.host.nudge_observer(record, sprint={"ref": "sprint:1459", "comments": []}, change="sprint-entity")
        self.assertEqual(_delivery_readiness_state(caught.exception), READINESS_BUSY, caught.exception)
        self.assertEqual(len(self._submitted()), 1, "nothing was typed into the working head")


class LocalPtyRetainedWorkerContinuationTests(unittest.TestCase):
    """secretary-1702: a red-verdict continuation into a retained local-pty worker starts a turn.

    Through `_launch`, `retain_worker` and `resume_worker` onto a real supervisor and the fake
    agent. `retain_worker` suspends the head with `SIGSTOP`; `resume_worker` hands the delivery a
    `before_send` that sends the `SIGCONT`, which this backend never performed, so the continuation
    was typed into a stopped process and reported `prompt_typed_but_no_turn_started`.
    """

    # The observer prompt tests' fixture: the same profile, supervisor root and fake agent.
    setUp = LocalPtyObserverPromptTests.setUp
    _reap = LocalPtyObserverPromptTests._reap
    _submitted = LocalPtyObserverPromptTests._submitted

    def test_a_retained_worker_is_resumed_and_its_continuation_starts_a_turn(self) -> None:
        workspace = self.root / "workspaces" / "worker"
        workspace.mkdir(parents=True)
        (workspace / "TASK.md").write_text("# Task\n", encoding="utf-8")
        with (
            mock.patch.object(CommandHostRuntime, "_require_production_runtime"),
            mock.patch.object(CommandHostRuntime, "_require_workspace_environment"),
        ):
            launched = self.host._launch(
                str(workspace),
                "ummanu-9001 worker",
                PROFILE,
                str(workspace / "TASK.md"),
                role="worker",
                env_name="UMMANU_1702_NO_COMMAND_OVERRIDE",
                task={"ref": "ummanu-9001"},
                launch_prompt=f"Read {workspace}/TASK.md and do its task.",
            )
        run = HeadRun.from_json(launched.head_run)
        self.assertEqual(run.spec.runtime, LOCAL_PTY_RUNTIME)
        record = DispatcherRecord(
            worker="ummanu-9001-worker",
            head=PROFILE,
            review_head=PROFILE,
            attempt_id="attempt-1702",
            comment_baseline=0,
            review_baseline=0,
            state="in_progress",
            claimed_at=time.time(),
            workspace=str(workspace),
            handle=launched.handle,
            worker_leaf=launched.leaf,
            worker_pid_file=run.pid_file,
            worker_head_run=dict(launched.head_run),
            worker_run={"adapter": "claude"},
            report_generation=2,
        )
        self.assertEqual(len(self._submitted()), 1, "the launch prompt started the worker's round")
        runtime = self.host.head_runtime_for(run)
        deadline = time.monotonic() + 15.0
        while runtime.observe(run).busy and time.monotonic() < deadline:
            time.sleep(0.05)

        self.host.retain_worker(record)
        # Where `_deliver_red_continuation` stands when it calls `resume_worker`.
        record.worker_continuation = WorkerContinuation(
            stage=WorkerContinuationStage.DELIVERY_PENDING,
            phase="review",
            session_held=True,
            sent_at=time.time(),
        )
        self.assertTrue(self.host.worker_retained_alive(record), "the worker was not suspended")

        with (
            mock.patch.object(CommandHostRuntime, "_worker_task_doc", return_value="# Task, round 2\n"),
            mock.patch.object(self.host.catalog, "integration_base", return_value="main", create=True),
        ):
            self.host.resume_worker({"ref": "ummanu-9001", "project": "ummanu"}, record)

        submitted = self._submitted()
        self.assertEqual(len(submitted), 2, submitted)
        self.assertIn(f"{workspace}/TASK.md", submitted[1])
        self.assertIn("Generation 2", submitted[1])
        self.assertTrue(record.worker_delivery_evidence.get("turn_confirmed"), record.worker_delivery_evidence)
        self.assertFalse(head_process_status(run.pid_file).get("stopped"), "the worker is still suspended")


class ObserverTaskIdentityTests(unittest.TestCase):
    """AC3: an observer's `task` is `sprint:<ID>` once, and a pre-1698 record is still recognised."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="lp-identity-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.run = HeadRun(
            run_id="observer-run-1459",
            spec=HeadSpec(profile_id="claude-opus-high", adapter="claude"),
            workspace=str(self.root / "observer"),
            task_ref=TaskRef.sprint("sprint:1459"),
            role="observer",
            leaf="leaf-1459",
            pid_file=str(self.root / "observer.pid"),
        )

    def _record(self) -> ObserverRecord:
        return ObserverRecord(
            sprint="sprint:1459",
            leaf=self.run.leaf,
            pid_file=self.run.pid_file,
            head_run=self.run.to_json(),
        )

    def test_the_observer_binding_names_the_sprint_once_wherever_it_is_built(self) -> None:
        expected = "sprint:1459"
        self.assertEqual(sprint_task("sprint:1459"), expected)
        self.assertEqual(sprint_task("1459"), expected)
        identity = heartbeat_identity(run_id="r", role="observer", task_ref=self.run.task_ref.to_json())
        self.assertEqual(identity["task"], expected, "the heartbeat record's task")
        self.assertEqual(
            heartbeat_identity(run_id="r", role="observer", task=sprint_task("sprint:1459"))["task"],
            expected,
            "the fallback the stop paths and observer_head_status hand over",
        )
        self.assertEqual(
            heartbeat_identity(run_id="r", role="worker", task_ref=TaskRef.card("ummanu-1698").to_json())[
                "task"
            ],
            "card:ummanu-1698",
        )

    def test_a_new_observer_record_is_a_live_match_with_the_single_prefix(self) -> None:
        publish_heartbeat(
            self.run.pid_file,
            {"run_id": self.run.run_id, "role": "observer", "task": "sprint:1459", "leaf": self.run.leaf},
        )
        status = observer_head_status(self._record())
        self.assertEqual(status["state"], HEARTBEAT_LIVE_MATCH, status)

    def test_a_running_orca_observer_written_before_the_fix_stays_alive(self) -> None:
        """The record sprint:1459's own Orca observer carries: `sprint:sprint:1459`.

        Read as foreign it would be an identity mismatch, and read as dead it would be replaced
        mid-sprint by the upgrade. It is the same run id, so it is the same launch.
        """
        publish_heartbeat(
            self.run.pid_file,
            {
                "run_id": self.run.run_id,
                "role": "observer",
                "task": "sprint:sprint:1459",
                "leaf": self.run.leaf,
            },
        )
        status = observer_head_status(self._record())
        self.assertEqual(status["state"], HEARTBEAT_LIVE_MATCH, status)
        self.assertFalse(observer_head_is_dead(self._record(), status))

    def test_a_legacy_spelling_does_not_excuse_another_run(self) -> None:
        publish_heartbeat(
            self.run.pid_file,
            {
                "run_id": "somebody-else",
                "role": "observer",
                "task": "sprint:sprint:1459",
                "leaf": self.run.leaf,
            },
        )
        self.assertEqual(observer_head_status(self._record())["state"], HEARTBEAT_IDENTITY_MISMATCH)

    def test_a_local_pty_head_written_before_the_fix_keeps_its_bare_reference(self) -> None:
        pid_file = str(self.root / "steward.pid")
        publish_heartbeat(pid_file, {"run_id": "steward-run", "role": "steward", "task": "steward"})
        expected = heartbeat_identity(
            run_id="steward-run", role="steward", task_ref=TaskRef.standing("steward").to_json()
        )
        self.assertEqual(expected["task"], "standing:steward")
        self.assertEqual(head_process_status(pid_file, expected=expected)["state"], HEARTBEAT_LIVE_MATCH)

    def test_the_stop_paths_expect_the_single_prefix(self) -> None:
        from types import SimpleNamespace

        from ummanu.dispatch.cleanup import CleanupOwner
        host = CommandHostRuntime(
            FakeCatalog(), self.root / "data", mode="real",
            production_runtime=registered_production_runtime(self.root),
        )  # type: ignore[arg-type]
        record = self._record()
        record.workspace = host.observer_workspace("sprint:1459")
        run = replace(self.run, workspace=record.workspace,
                      spec=replace(self.run.spec, runtime=LOCAL_PTY_RUNTIME))
        record.head_run = run.to_json()
        record = ObserverRecord.from_json(record.to_json())
        self.assertEqual(HeadRun.from_json(record.head_run), run)
        runtime = SimpleNamespace(data_dir=host.data_dir, host=host,
                                  sprints=SimpleNamespace(show=lambda *a, **k: {
                                      "id": "sprint-1459", "ref": record.sprint, "status": "open"}))
        host.cleanup_owner = CleanupOwner(runtime)
        seen: list[str] = []

        def remember(*_args: object, **kwargs: object) -> None:
            seen.append(str(kwargs.get("task")))

        with (
            mock.patch.object(CommandHostRuntime, "_guard_head_run", side_effect=remember),
            mock.patch.object(host, "head_runtime_for", return_value=SimpleNamespace(
                stop=mock.Mock(side_effect=lambda run, initiator: SimpleNamespace(
                    ok=True, reason="", run=run.finishing(initiator).exited())),
                forget_head=mock.Mock())) as selected,
        ):
            host.stop_observer(record)

        self.assertEqual(seen, ["sprint:1459"])
        stopped = selected.return_value.stop.call_args.args[0]
        self.assertEqual(sprint_task(stopped.task_ref.ref), "sprint:1459")
        self.assertEqual(stopped.run_id, self.run.run_id)
        self.assertEqual(host.cleanup_owner.journal.summary()[0]["status"], "preserved")


if __name__ == "__main__":
    unittest.main()
