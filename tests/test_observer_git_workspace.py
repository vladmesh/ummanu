"""secretary-1705, secretary-1722: every observer gets a plain detached `git worktree`, never Orca's.

The same rule as a card's workspace: the path is fixed when the launch intent is written and read
back from the record ever after. So these tests hold three things: an observer's workspace is cut,
stopped and removed with no `orca` argv at all; the launch intent and the bring-up name the same
path whatever the head; and a record whose workspace Orca made, or whose run is a legacy record, is
refused by every verb and left untouched.

The host runs in real mode over the real observer repo it creates; only the head itself (its pane,
its process, its stop) is stood in for.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from tests.fakes.dispatcher import FakeCatalog
from tests.production_runtime_fixtures import registered_production_runtime
from ummanu.dispatch.cleanup import CleanupOwner
from ummanu.dispatch.host import OBSERVER_REPO_BRANCH, CommandHostRuntime
from ummanu.dispatch.observer import ObserverRecord, _write_launch_intent
from ummanu.dispatch.types import HostError, LegacyDispatcherRecord
from ummanu.observer_root import observer_root_repo
from ummanu.runtime.head import HeadCommand, HeadRun, HeadSpec, TaskRef
from ummanu.runtime.head_runtimes import LOCAL_PTY_RUNTIME, ORCA_LEGACY_RUNTIME
from ummanu.runtime.local_pty_head import LocalPtyHeadRuntime

REF = "sprint:1705"
TOKEN = "sprint-1705"
SUPERVISED_HEAD = "claude-local-pty"
#: The fake registry's own observer profile.
REGISTRY_HEAD = "codex-observer"


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)
    return result.stdout.strip()


class _Catalog(FakeCatalog):
    """The fake registry plus one supervised observer profile whose head command does nothing."""

    def __init__(self) -> None:
        super().__init__()
        self.profiles[SUPERVISED_HEAD] = {
            "adapter": "claude",
            "model": "opus",
            "resource": "claude-sub",
            "runtime": LOCAL_PTY_RUNTIME,
        }

    def head_launch(self, head: str, prompt_file: str, **_kwargs: Any) -> HeadCommand:
        return HeadCommand("true", adapter="claude")

    def prepare_head_workspace(self, head: str, workspace: str, *, role: str) -> None:
        return None


class _RecordingHost(CommandHostRuntime):
    """Every child and every head step is recorded in order; an `orca` argv fails the test."""

    def __init__(self, data_dir: Path, root: Path) -> None:
        super().__init__(  # type: ignore[arg-type]
            _Catalog(), data_dir, mode="real", production_runtime=registered_production_runtime(root)
        )
        self.events: list[Any] = []

    def _record(self, args: list[str]) -> None:
        self.events.append(list(args))
        if args and args[0] == "orca":
            raise AssertionError(f"orca was called: {args}")

    def _run(self, args, label, *, cwd=None):  # type: ignore[override]
        self._record(args)
        return super()._run(args, label, cwd=cwd)

    def run_capture(self, args, label, *, cwd=None):  # type: ignore[override]
        self._record(args)
        return super().run_capture(args, label, cwd=cwd)

    def _open_head_pane(self, run, title, command):  # type: ignore[override]
        self.events.append("head-start")
        return dataclasses.replace(run, handle="run:observer", leaf="leaf:observer")

    def _confirm_head_process_gone(self, pid_file, **_kwargs):  # type: ignore[override]
        self.events.append("head-confirmed-gone")

    def _guard_head_run(self, *_args, **_kwargs):  # type: ignore[override]
        return {"known": False}


class _HeadRuntime:
    """The head's backend, standing in for its stop."""

    def __init__(self, events: list[Any]) -> None:
        self.events = events

    def stop(self, run, initiator):
        self.events.append(f"head-stop:{run.spec.runtime}")
        return SimpleNamespace(ok=True, reason="", run=run.finishing(initiator).exited())

    def forget_head(self, run_id: str) -> None:
        return None


class ObserverGitWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name).resolve()
        self.orca_root = self.root / "orca-workspaces"
        env = mock.patch.dict(
            os.environ,
            {
                "UMMANU_DISPATCHER_WORKSPACES_ROOT": str(self.orca_root),
                "UMMANU_DISPATCHER_BODY_DIR": str(self.root / "bodies"),
                "UMMANU_CLAUDE_PROJECTS": str(self.root / "claude-projects"),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        self.data_dir = self.root / "data"
        self.host = _RecordingHost(self.data_dir, self.root)
        self.git_path = self.data_dir / "workspaces" / "observers" / TOKEN
        self.orca_path = self.orca_root / "observers" / TOKEN
        runtime = SimpleNamespace(data_dir=self.data_dir, host=self.host,
                                  sprints=SimpleNamespace(show=lambda *a, **k: {"id": "sprint-test", "ref": REF, "status": "open"}))
        self.host.cleanup_owner = CleanupOwner(runtime)

    def prepare(self, head: str, recorded: str = "") -> dict[str, Any]:
        return self.host.prepare_observer({"ref": REF}, head, prompt="# Sprint\n", recorded_workspace=recorded)

    def record(self, launched: dict[str, Any], head: str) -> ObserverRecord:
        return ObserverRecord(
            sprint=REF,
            head=head,
            workspace=str(launched["workspace"]),
            handle=str(launched["handle"]),
            leaf=str(launched["leaf"]),
            pid_file=str(launched["pid_file"]),
            head_run=dict(launched["head_run"]),
        )

    def stop(self, record: ObserverRecord) -> None:
        with mock.patch.object(self.host, "head_runtime_for", return_value=_HeadRuntime(self.host.events)):
            self.host.stop_observer(record)

    def orca_argvs(self) -> list[list[str]]:
        return [event for event in self.host.events if isinstance(event, list) and event[:1] == ["orca"]]

    def worktree_argvs(self) -> list[list[str]]:
        return [event[3:] for event in self.host.events if isinstance(event, list) and event[3:4] == ["worktree"]]

    # -- the supervised observer -----------------------------------------------------------------

    def test_a_local_pty_observer_is_a_detached_git_worktree_with_no_orca_argv_from_intent_to_removal(self) -> None:
        intent = self.host.observer_workspace(REF)
        self.assertEqual(intent, str(self.git_path))

        launched = self.prepare(SUPERVISED_HEAD, intent)

        self.assertEqual(launched["workspace"], intent)
        repo = observer_root_repo(self.data_dir)
        self.assertEqual(git(self.git_path, "rev-parse", "--show-toplevel"), str(self.git_path))
        self.assertEqual(git(self.git_path, "rev-parse", "--abbrev-ref", "HEAD"), "HEAD", "not detached")
        self.assertEqual(git(self.git_path, "rev-parse", "HEAD"), git(repo, "rev-parse", OBSERVER_REPO_BRANCH))
        self.assertEqual(git(repo, "branch", "--format=%(refname:short)"), OBSERVER_REPO_BRANCH, "a branch was made")
        self.assertEqual(git(repo, "log", "--format=%s", OBSERVER_REPO_BRANCH), "observer root")
        self.assertTrue((self.git_path / "SPRINT.md").is_file())

        self.stop(self.record(launched, SUPERVISED_HEAD))

        self.assertFalse(self.git_path.exists())
        self.assertNotIn(str(self.git_path), git(repo, "worktree", "list", "--porcelain"))
        self.assertEqual(self.orca_argvs(), [])

    def test_the_durable_stop_receipt_precedes_git_removal_without_force_or_prune(self) -> None:
        launched = self.prepare(SUPERVISED_HEAD, self.host.observer_workspace(REF))
        del self.host.events[:]

        self.stop(self.record(launched, SUPERVISED_HEAD))

        steps = [
            event if isinstance(event, str) else event[3:5]
            for event in self.host.events
            if isinstance(event, str) or event[3:5] in (["worktree", "remove"], ["worktree", "prune"])
        ]
        self.assertEqual(
            steps,
            [f"head-stop:{LOCAL_PTY_RUNTIME}", ["worktree", "remove"]],
        )
        remove = next(event for event in self.host.events if isinstance(event, list) and event[3:5] == ["worktree", "remove"])
        self.assertNotIn("--force", remove)
        self.assertFalse(any("prune" in event for event in self.host.events if isinstance(event, list)))

    def _quiescent_runtime(self, *, ok: bool) -> Any:
        """The head runtime held to `LocalPtyHeadRuntime`'s real signatures (issue:5f3cdb4c).

        An autospec refuses any keyword the real `stop_if_quiescent` does not take, which is how
        the Orca-era `teardown=` slipped past the fake host and failed every production tick.
        """
        runtime = mock.create_autospec(LocalPtyHeadRuntime, instance=True)
        stand_in = _HeadRuntime(self.host.events)
        runtime.stop.side_effect = stand_in.stop

        def stop_if_quiescent(run, initiator, **_kwargs):
            if not ok:
                return SimpleNamespace(ok=False, reason="turn_in_flight", run=run)
            return runtime.stop(run, initiator)

        runtime.stop_if_quiescent.side_effect = stop_if_quiescent
        return runtime

    def test_a_quiet_observer_is_stopped_through_the_real_runtime_signature_and_its_worktree_removed(self) -> None:
        launched = self.prepare(SUPERVISED_HEAD, self.host.observer_workspace(REF))
        record = self.record(launched, SUPERVISED_HEAD)
        runtime = self._quiescent_runtime(ok=True)

        with mock.patch.object(self.host, "head_runtime_for", return_value=runtime):
            self.assertTrue(self.host.stop_observer_if_quiescent(record, 3, False))

        runtime.stop_if_quiescent.assert_called_once()
        kwargs = runtime.stop_if_quiescent.call_args.kwargs
        self.assertEqual(kwargs, {"expected_activity_epoch": 3, "head_process_alive": False})
        self.assertFalse(self.git_path.exists())
        self.assertEqual(self.orca_argvs(), [])

    def test_a_refused_quiescent_stop_keeps_the_observer_worktree(self) -> None:
        launched = self.prepare(SUPERVISED_HEAD, self.host.observer_workspace(REF))
        record = self.record(launched, SUPERVISED_HEAD)
        runtime = self._quiescent_runtime(ok=False)

        with mock.patch.object(self.host, "head_runtime_for", return_value=runtime):
            self.assertFalse(self.host.stop_observer_if_quiescent(record, 0, True))

        self.assertTrue(self.git_path.exists())
        self.assertNotIn(["worktree", "remove"], [argv[:2] for argv in self.worktree_argvs()])
        self.assertNotIn(f"head-stop:{LOCAL_PTY_RUNTIME}", self.host.events)

    def test_a_respawn_reuses_a_live_worktree_and_recuts_a_removed_one(self) -> None:
        workspace = self.host.observer_workspace(REF)
        launched = self.prepare(SUPERVISED_HEAD, workspace)
        (self.git_path / "notes.txt").write_text("kept\n", encoding="utf-8")

        self.prepare(SUPERVISED_HEAD, workspace)
        self.assertTrue((self.git_path / "notes.txt").exists(), "a registered worktree was re-created")
        self.assertEqual([argv[:2] for argv in self.worktree_argvs()].count(["worktree", "add"]), 1)

        self.stop(self.record(launched, SUPERVISED_HEAD))
        self.assertEqual((self.git_path / "notes.txt").read_text(), "kept\n")
        self.assertTrue(self.git_path.exists(), "observer user work must be preserved")
        again = self.prepare(SUPERVISED_HEAD, workspace)

        self.assertEqual(again["workspace"], workspace)
        self.assertEqual(git(self.git_path, "rev-parse", "--abbrev-ref", "HEAD"), "HEAD")
        self.assertEqual(self.orca_argvs(), [])

    def test_a_completed_stop_over_a_worktree_already_gone_reuses_its_durable_proof(self) -> None:
        launched = self.prepare(SUPERVISED_HEAD, self.host.observer_workspace(REF))
        record = self.record(launched, SUPERVISED_HEAD)
        self.stop(record)
        del self.host.events[:]

        self.stop(record)

        self.assertEqual(self.host.cleanup_owner.journal.summary()[0]["status"], "completed")
        self.assertNotIn(f"head-stop:{LOCAL_PTY_RUNTIME}", self.host.events)
        self.assertEqual(self.orca_argvs(), [])
        self.assertNotIn(["worktree", "remove"], [argv[:2] for argv in self.worktree_argvs()])

    def test_a_directory_git_never_registered_is_preserved_and_starts_no_head(self) -> None:
        """A path supplies no destructive ownership proof for its existing contents."""
        self.git_path.mkdir(parents=True)
        (self.git_path / "SPRINT.md").write_text("stale\n", encoding="utf-8")

        with self.assertRaises(HostError):
            self.prepare(SUPERVISED_HEAD, self.host.observer_workspace(REF))

        self.assertEqual((self.git_path / "SPRINT.md").read_text(encoding="utf-8"), "stale\n")
        self.assertNotIn("head-start", self.host.events)
        self.assertEqual(self.orca_argvs(), [])

    def test_a_worktree_git_refuses_to_cut_starts_no_head(self) -> None:
        with (
            mock.patch(
                "ummanu.dispatch.host.git_worktree.add",
                return_value=subprocess.CompletedProcess([], 128, "", "fatal: invalid reference"),
            ),
            self.assertRaisesRegex(HostError, "git worktree add failed for the observer workspace"),
        ):
            self.prepare(SUPERVISED_HEAD, self.host.observer_workspace(REF))

        self.assertNotIn("head-start", self.host.events)
        self.assertEqual(self.orca_argvs(), [])

    # -- the same path from intent and bring-up ---------------------------------------------------

    def test_the_intent_and_the_bring_up_name_the_same_path_whatever_the_head(self) -> None:
        for head in (SUPERVISED_HEAD, REGISTRY_HEAD):
            with self.subTest(head=head):
                intent = self.host.observer_workspace(REF)
                self.assertEqual(intent, str(self.git_path))
                self.assertEqual(self.prepare(head, intent)["workspace"], intent)
                self.assertEqual(self.prepare(head)["workspace"], intent, "without a recorded path")
        self.assertEqual(self.orca_argvs(), [])

    def test_the_launch_intent_fixes_the_path_the_bring_up_then_cuts(self) -> None:
        runtime = SimpleNamespace(host=self.host, production_state=SimpleNamespace(save=lambda _payload: None))
        for head in (SUPERVISED_HEAD, REGISTRY_HEAD):
            with self.subTest(head=head):
                record = ObserverRecord(sprint=REF)
                self.assertIsNone(_write_launch_intent(runtime, {}, {}, REF, record, head, 1))
                self.assertEqual(record.workspace, str(self.git_path))
                self.assertEqual(self.prepare(head, record.workspace)["workspace"], record.workspace)
        self.assertEqual(self.orca_argvs(), [])

    # -- a record written on Orca is a legacy record ------------------------------------------------

    def test_a_record_that_names_an_orca_workspace_keeps_it_and_the_bring_up_refuses_it(self) -> None:
        runtime = SimpleNamespace(host=self.host, production_state=SimpleNamespace(save=lambda _payload: None))
        record = ObserverRecord(sprint=REF, workspace=str(self.orca_path))

        self.assertIsNone(_write_launch_intent(runtime, {}, {}, REF, record, SUPERVISED_HEAD, 2))

        # Never re-placed silently: the intent keeps the recorded path, and the bring-up refuses it.
        self.assertEqual(record.workspace, str(self.orca_path))
        with self.assertRaisesRegex(LegacyDispatcherRecord, "refused to launch the observer of sprint:1705"):
            self.prepare(SUPERVISED_HEAD, record.workspace)
        self.assertFalse(self.orca_path.exists())
        self.assertFalse(self.git_path.exists())
        self.assertEqual(self.orca_argvs(), [])

    def test_a_live_orca_observer_record_is_refused_by_every_verb_and_left_in_place(self) -> None:
        """sprint:1459's own observer ran on Orca: its record still loads, and nothing drives it."""
        self.orca_path.mkdir(parents=True)
        (self.orca_path / "SPRINT.md").write_text("live\n", encoding="utf-8")
        pid_file = self.host.observer_pid_file(REF)

        def legacy(workspace: Path, runtime: str) -> ObserverRecord:
            return ObserverRecord(
                sprint=REF,
                head=SUPERVISED_HEAD,
                workspace=str(workspace),
                handle="term-obs",
                leaf="leaf-obs",
                pid_file=pid_file,
                head_run=HeadRun(
                    run_id="live-observer",
                    spec=HeadSpec(profile_id=SUPERVISED_HEAD, adapter="claude", runtime=runtime),
                    workspace=str(workspace),
                    task_ref=TaskRef.sprint(REF),
                    role="observer",
                    pid_file=pid_file,
                    handle="term-obs",
                    leaf="leaf-obs",
                ).to_json(),
            )

        # An Orca workspace, and a git-placed workspace whose run is a legacy record.
        for record in (legacy(self.orca_path, ORCA_LEGACY_RUNTIME), legacy(self.git_path, ORCA_LEGACY_RUNTIME)):
            with self.subTest(workspace=record.workspace):
                for verb in (
                    lambda held: self.host.stop_observer(held),
                    lambda held: self.host.nudge_observer(held, sprint={"ref": REF}),
                    lambda held: self.host.observer_status(held),
                    lambda held: self.host.stop_observer_if_quiescent(held, 0, True),
                ):
                    with self.assertRaises(LegacyDispatcherRecord):
                        verb(record)
        self.assertEqual((self.orca_path / "SPRINT.md").read_text(encoding="utf-8"), "live\n")
        self.assertEqual(self.orca_argvs(), [])
        self.assertEqual(self.worktree_argvs(), [])
        self.assertNotIn("head-confirmed-gone", self.host.events)
        self.assertFalse(observer_root_repo(self.data_dir).exists(), "the git route was taken")


if __name__ == "__main__":
    unittest.main()
