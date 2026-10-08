"""secretary-1700, secretary-1722: every card gets a plain `git worktree`, never Orca's.

Placement asks no runtime: whatever a card's worker and reviewer profiles are, its checkout is cut,
resumed, discarded, stopped and torn down with no `orca` argv at all. A record whose workspace is an
Orca checkout under the Orca root was written before this was the only placement: it is refused as a
legacy record, never resumed, re-placed or torn down through Orca.

The host runs in real mode over a real project checkout cloned from a real bare remote; only the
parts of a bring-up that are not about the workspace (the Python environment, the task document,
the head launch itself) are stood in for.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from types import SimpleNamespace

from ummanu.dispatch.cleanup import CleanupOwner
from unittest import mock

from ummanu.dispatch.host import CommandHostRuntime, LaunchedHead
from ummanu.dispatch.launch import CAUSE_WORKSPACE_CONTRACT
from ummanu.dispatch.state import DispatcherRecord
from ummanu.dispatch.types import HostError, LegacyDispatcherRecord
from ummanu.runtime.head_runtimes import LOCAL_PTY_RUNTIME
from tests.fakes.dispatcher import FakeCatalog
from tests.production_runtime_fixtures import registered_production_runtime

PROJECT = "sample"
ORCA_BINDING = "sample_orca"
REF = "sample-7"
WORKER = "sample-7-worker"
BRANCH = f"pipeline/{REF}"
WORKER_HEAD = "codex-local-pty"
REVIEW_HEAD = "claude-local-pty"


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)
    return result.stdout.strip()


class _Catalog(FakeCatalog):
    """The fake registry plus one supervised profile per role, bound to a real project checkout."""

    def __init__(self, repo: Path) -> None:
        super().__init__()
        self.repo = repo
        # A project onboarded before secretary-1704 carries a legacy `orca_binding`; a new one has none.
        self.orca_binding: str | None = ORCA_BINDING
        self.profiles[WORKER_HEAD] = {
            "adapter": "codex",
            "model": "gpt-5.6-terra",
            "effort": "default",
            "resource": "openai-sub",
            "runtime": LOCAL_PTY_RUNTIME,
        }
        self.profiles[REVIEW_HEAD] = {
            "adapter": "claude",
            "model": "opus",
            "resource": "claude-sub",
            "runtime": LOCAL_PTY_RUNTIME,
        }

    def binding(self, project: str) -> dict:
        binding = {"repo": str(self.repo), "default_branch": "main"}
        if self.orca_binding is not None:
            binding["orca_binding"] = self.orca_binding
        return binding


class _RecordingHost(CommandHostRuntime):
    """Every child the host runs is recorded; an `orca` argv fails the test."""

    def __init__(self, catalog: _Catalog, data_dir: Path, root: Path) -> None:
        super().__init__(  # type: ignore[arg-type]
            catalog, data_dir, mode="real", production_runtime=registered_production_runtime(root)
        )
        self.argvs: list[list[str]] = []
        self.launches: list[dict[str, Any]] = []

    def _record(self, args: list[str]) -> None:
        self.argvs.append(list(args))
        if args and args[0] == "orca":
            raise AssertionError(f"orca was called: {args}")

    def _run(self, args, label, *, cwd=None):  # type: ignore[override]
        self._record(args)
        return super()._run(args, label, cwd=cwd)

    def run_capture(self, args, label, *, cwd=None):  # type: ignore[override]
        self._record(args)
        return super().run_capture(args, label, cwd=cwd)

    # What a bring-up does besides the workspace is not what these tests are about.
    def _prepare_workspace_environment(self, workspace: str, *, project: str = "") -> None:
        return None

    def _require_workspace_environment(self, workspace: str) -> None:
        return None

    def _run_setup(self, project: str, workspace: str) -> None:
        return None

    def _clear_report_bodies(self, reference: str) -> None:
        return None

    def _clear_body_file(self, kind: str, reference: str, review_round: int) -> None:
        return None

    def _worker_task_doc(self, *args: Any, **kwargs: Any) -> str:
        return "task\n"

    def _review_document(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        *,
        local_run_policy: tuple[dict[str, Any] | None, bool] | None = None,
    ) -> tuple[Path, str]:
        return Path(record.workspace).parent / "review.md", "review it"

    def _launch(self, workspace: str, title: str, head: str, prompt_file: str, **kwargs: Any) -> LaunchedHead:
        self.launches.append({"workspace": workspace, "head": head, **kwargs})
        return LaunchedHead(handle=f"run:{head}", head=head)


class _Fixture:
    """A bare remote with `main` and a published predecessor branch, and a clone to cut from."""

    def __init__(self, root: Path) -> None:
        self.remote = root / "remote.git"
        self.repo = root / "project"
        author = root / "author"
        git(root, "init", "--quiet", "--bare", "--initial-branch", "main", str(self.remote))
        git(root, "init", "--quiet", "--initial-branch", "main", str(author))
        for checkout in (author,):
            git(checkout, "config", "user.name", "Test User")
            git(checkout, "config", "user.email", "test@example.invalid")
        git(author, "remote", "add", "origin", str(self.remote))
        (author / "README.md").write_text("seed\n", encoding="utf-8")
        git(author, "add", "-A")
        git(author, "commit", "--quiet", "-m", "seed")
        git(author, "push", "--quiet", "origin", "main")
        self.main_sha = git(author, "rev-parse", "HEAD")
        git(root, "clone", "--quiet", str(self.remote), str(self.repo))
        git(author, "checkout", "--quiet", "-b", "pipeline/sample-6")
        (author / "predecessor.txt").write_text("unreleased\n", encoding="utf-8")
        git(author, "add", "-A")
        git(author, "commit", "--quiet", "-m", "predecessor")
        git(author, "push", "--quiet", "origin", "pipeline/sample-6")
        self.candidate_sha = git(author, "rev-parse", "HEAD")


def _task(
    *, seed: str = "", worker_head: str = WORKER_HEAD, review_head: str = REVIEW_HEAD
) -> dict[str, Any]:
    workspace: dict[str, Any] = {"seed_ref": seed} if seed else {}
    return {
        "ref": REF,
        "project": PROJECT,
        "workspace": workspace,
        "routing": {"head_override": worker_head, "review_head_override": review_head},
    }


def _record(workspace: str, *, review_head: str = REVIEW_HEAD) -> DispatcherRecord:
    return DispatcherRecord(
        worker=WORKER,
        workspace=workspace,
        handle="",
        head=WORKER_HEAD,
        review_head=review_head,
        attempt_id="attempt-1",
        comment_baseline=0,
        review_baseline=0,
        state="review_starting",
        claimed_at=0.0,
    )


class GitWorkspaceManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name).resolve()
        self.fixture = _Fixture(self.root)
        self.orca_root = self.root / "orca-workspaces"
        env = mock.patch.dict(os.environ, {"UMMANU_DISPATCHER_WORKSPACES_ROOT": str(self.orca_root)})
        env.start()
        self.addCleanup(env.stop)
        self.data_dir = self.root / "data"
        self.catalog = _Catalog(self.fixture.repo)
        self.host = _RecordingHost(self.catalog, self.data_dir, self.root)
        self.git_path = self.data_dir / "workspaces" / PROJECT / WORKER
        self.orca_path = self.orca_root / ORCA_BINDING / WORKER
        cleanup_task = {**_task(), "id": "task-sample-7", "state": "done", "claim": {"worker": None}}
        runtime = SimpleNamespace(data_dir=self.data_dir, catalog=self.catalog, host=self.host,
                                  reader=SimpleNamespace(show=lambda ref: cleanup_task))
        self.host.cleanup_owner = CleanupOwner(runtime)

    def _prepare(self, task: dict[str, Any], **kwargs: Any) -> Path:
        prepared = self.host.prepare_worker(task, WORKER, WORKER_HEAD, attempt_id="attempt-1", **kwargs)
        return Path(prepared["workspace"])

    def _registered(self) -> set[Path]:
        listing = git(self.fixture.repo, "worktree", "list", "--porcelain")
        return {
            Path(line.removeprefix("worktree ")).resolve()
            for line in listing.splitlines()
            if line.startswith("worktree ")
        }

    def _no_orca(self) -> None:
        self.assertFalse([argv for argv in self.host.argvs if argv and argv[0] == "orca"])

    # -- selection -----------------------------------------------------------------------------

    def test_a_supervised_card_is_placed_under_the_data_dir_by_project_id(self) -> None:
        self.assertEqual(self.host.restore_workspace(_task(), WORKER), str(self.git_path))
        self._no_orca()

    def test_every_card_is_placed_by_git_whatever_its_heads(self) -> None:
        # The fake registry's own profiles, a supervised pair, and a head the registry cannot
        # resolve: placement reads none of them.
        for task in (
            _task(worker_head="codex", review_head="codex-reviewer"),
            _task(worker_head="claude-opus", review_head=REVIEW_HEAD),
            _task(worker_head="no-such-head", review_head="no-such-head"),
        ):
            with self.subTest(routing=task["routing"]):
                self.assertEqual(self.host.restore_workspace(task, WORKER), str(self.git_path))
        self.catalog.orca_binding = None
        self.assertEqual(self.host.restore_workspace(_task(worker_head="codex"), WORKER), str(self.git_path))
        self._no_orca()

    def test_a_card_on_the_registrys_own_profiles_is_cut_with_no_orca_call(self) -> None:
        workspace = self._prepare(_task(worker_head="codex", review_head="codex-reviewer"))

        self.assertEqual(workspace, self.git_path)
        self.assertEqual(git(workspace, "branch", "--show-current"), BRANCH)
        self.assertIn(workspace.resolve(), self._registered())
        self._no_orca()

    # -- create --------------------------------------------------------------------------------

    def test_a_branch_seed_is_cut_on_the_card_branch_with_no_orca_call(self) -> None:
        workspace = self._prepare(_task(seed="pipeline/sample-6"))

        self.assertEqual(workspace, self.git_path)
        self.assertEqual(git(workspace, "branch", "--show-current"), BRANCH)
        self.assertEqual(git(workspace, "rev-parse", "HEAD"), self.fixture.candidate_sha)
        self.assertIn(workspace.resolve(), self._registered())
        self._no_orca()

    def test_an_exact_sha_seed_is_cut_on_the_card_branch_with_no_orca_call(self) -> None:
        workspace = self._prepare(_task(seed=self.fixture.candidate_sha))

        self.assertEqual(git(workspace, "branch", "--show-current"), BRANCH)
        self.assertEqual(git(workspace, "rev-parse", "HEAD"), self.fixture.candidate_sha)
        self._no_orca()

    def test_an_ordinary_card_is_cut_from_its_integration_base(self) -> None:
        workspace = self._prepare(_task())

        self.assertEqual(git(workspace, "rev-parse", "HEAD"), self.fixture.main_sha)
        self.assertEqual(git(workspace, "branch", "--show-current"), BRANCH)

    def test_a_resumed_card_is_validated_in_place_with_no_orca_call(self) -> None:
        first = self._prepare(_task())
        (first / "work.txt").write_text("in progress\n", encoding="utf-8")
        self.host.argvs.clear()

        again = self._prepare(_task(), require_existing_workspace=True)

        self.assertEqual(again, first)
        self.assertEqual((again / "work.txt").read_text(encoding="utf-8"), "in progress\n")
        self.assertIn(["git", "-C", str(first), "branch", "--show-current"], self.host.argvs)
        self._no_orca()

    def test_a_worktree_the_check_rejects_preserves_the_unadmitted_workspace_and_ref(self) -> None:
        refusal = HostError(
            "resume workspace is on the wrong branch", bring_up_cause=CAUSE_WORKSPACE_CONTRACT
        )
        with (
            mock.patch.object(_RecordingHost, "_validate_resumable_workspace", side_effect=refusal),
            self.assertRaises(HostError) as refused,
        ):
            self._prepare(_task())

        self.assertIn("wrong branch", str(refused.exception))
        self.assertEqual(refused.exception.bring_up_cause, CAUSE_WORKSPACE_CONTRACT)
        self.assertTrue(self.git_path.exists())
        self.assertIn(self.git_path.resolve(), self._registered())
        self.assertIn(BRANCH, git(self.fixture.repo, "branch", "--list", BRANCH))
        self._no_orca()

    def test_a_workspace_the_launch_intent_names_elsewhere_is_refused_before_anything_is_cut(self) -> None:
        """The launch intent recorded the path first, and a tick that dies can only find the head
        through it: a placement anywhere else is refused rather than adopted (secretary-820)."""
        with self.assertRaisesRegex(HostError, f"belongs at {self.git_path}, not"):
            self.host._git_workspaces.create(_task(), WORKER, "main", expected=str(self.root / "elsewhere"))

        self.assertFalse(self.git_path.exists())
        self.assertEqual(self.host.argvs, [], "neither fetched nor cut")

    # -- stop and teardown ---------------------------------------------------------------------

    def test_stop_and_teardown_remove_the_worktree_with_no_orca_call(self) -> None:
        workspace = self._prepare(_task())
        self.host.argvs.clear()

        self.host.stop(_record(str(workspace)))
        record = _record(str(workspace))
        self.host.cleanup_owner._state = lambda: {"records": {REF: record.to_json()}}
        receipt = self.host.teardown(record)
        self.assertIsNotNone(receipt)

        self.assertFalse(workspace.exists())
        self.assertNotIn(workspace.resolve(), self._registered())
        self.assertEqual(receipt["status"], "completed")
        self.assertFalse(any("prune" in argv or "--force" in argv for argv in self.host.argvs))
        self._no_orca()

    def test_a_refused_stop_leaves_the_git_worktree_in_place(self) -> None:
        workspace = self._prepare(_task())
        self.host.argvs.clear()

        with mock.patch.object(
            self.host.cleanup_owner, "_stop", side_effect=HostError("the worker head was not stopped")
        ):
            record = _record(str(workspace))
            self.host.cleanup_owner._state = lambda: {"records": {REF: record.to_json()}}
            self.assertEqual(self.host.teardown(record)["status"], "pending")

        self.assertTrue(workspace.is_dir())
        self.assertIn(workspace.resolve(), self._registered())
        self.assertFalse([argv for argv in self.host.argvs if "worktree" in argv])

    # -- a project with no orca_binding (secretary-1704) ---------------------------------------

    def test_a_supervised_card_on_a_project_without_orca_binding_gets_its_git_workspace(self) -> None:
        self.catalog.orca_binding = None

        workspace = self._prepare(_task())

        self.assertEqual(workspace, self.git_path)
        self.assertEqual(git(workspace, "branch", "--show-current"), BRANCH)
        self.assertIn(workspace.resolve(), self._registered())
        self._no_orca()

    # -- a card checked out on Orca is a legacy record --------------------------------------------

    def test_an_orca_checkout_is_never_resumed_or_torn_down_and_its_record_is_refused(self) -> None:
        # Cut the way Orca cut it: under the Orca root, named by the binding's registration, on the
        # card branch.
        self.orca_path.parent.mkdir(parents=True)
        git(self.fixture.repo, "worktree", "add", "--quiet", "-b", BRANCH, str(self.orca_path), "origin/main")

        # Placement never finds it: the card's place is its git workspace, which is not there.
        self.assertEqual(self.host.restore_workspace(_task(), WORKER), str(self.git_path))
        with self.assertRaisesRegex(HostError, "resume workspace is missing") as missing:
            self._prepare(_task(), require_existing_workspace=True)
        self.assertEqual(missing.exception.bring_up_cause, CAUSE_WORKSPACE_CONTRACT)
        self.assertFalse(self.git_path.exists())

        # The record that names it is refused by every verb, and nothing is removed.
        record = _record(str(self.orca_path))
        self.host.argvs.clear()
        for verb in (
            lambda: self.host.teardown(record),
            lambda: self.host.stop_workspace(record),
            lambda: self.host.restart_worker(_task(), record),
            lambda: self.host.start_review(_task(), record),
        ):
            with self.assertRaises(LegacyDispatcherRecord) as refused:
                verb()
            self.assertIn(str(self.orca_path), str(refused.exception))
            self.assertEqual(refused.exception.bring_up_cause, CAUSE_WORKSPACE_CONTRACT)
        self.assertEqual(self.host.argvs, [])
        self.assertEqual(self.host.launches, [])
        self.assertIn(self.orca_path.resolve(), self._registered())

    def test_a_git_workspace_is_found_again_whatever_its_profiles_say(self) -> None:
        workspace = self._prepare(_task())

        self.assertEqual(self.host.restore_workspace(_task(worker_head="codex"), WORKER), str(workspace))

    # -- the reviewer --------------------------------------------------------------------------

    def test_the_reviewer_starts_in_the_workers_workspace_with_no_pane_to_split(self) -> None:
        workspace = self._prepare(_task())
        for review_head in (REVIEW_HEAD, "codex-reviewer"):
            with self.subTest(review_head=review_head):
                self.host.launches.clear()

                launched = self.host.start_review(_task(), _record(str(workspace), review_head=review_head))

                [review] = self.host.launches
                self.assertEqual(review["workspace"], str(workspace))
                self.assertEqual(review["head"], review_head)
                self.assertNotIn("split_from", review)
                self.assertEqual(launched.commit, self.fixture.main_sha)
        self._no_orca()


class GitWorkspaceRootTests(unittest.TestCase):
    """The git root is read from its shape alone; the Orca root only names a legacy record."""

    def test_ownership_is_the_git_shape_and_an_orca_path_is_a_legacy_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            host = CommandHostRuntime(FakeCatalog(), data, mode="real")  # type: ignore[arg-type]
            git_path = str(data / "workspaces" / PROJECT / WORKER)
            orca_path = str(data / "orca" / ORCA_BINDING / WORKER)
            with mock.patch.dict(os.environ, {"UMMANU_DISPATCHER_WORKSPACES_ROOT": str(data / "orca")}):
                self.assertTrue(host._is_git_workspace(git_path))
                self.assertFalse(host._legacy_workspace(git_path))
                self.assertFalse(host._is_git_workspace(orca_path))
                self.assertTrue(host._legacy_workspace(orca_path))
                # Neither: a path this host did not place and Orca's root does not hold.
                self.assertFalse(host._legacy_workspace(str(data / "elsewhere" / WORKER)))

    def test_only_the_project_and_worker_shape_under_the_git_root_is_git_managed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            host = CommandHostRuntime(FakeCatalog(), data, mode="real")  # type: ignore[arg-type]
            root = data / "workspaces"
            with mock.patch.dict(os.environ, {"UMMANU_DISPATCHER_WORKSPACES_ROOT": str(data / "orca")}):
                self.assertTrue(host._is_git_workspace(str(root / PROJECT / WORKER)))
                for other in (root / WORKER, root / PROJECT / WORKER / "nested", root, data / "elsewhere"):
                    with self.subTest(path=other):
                        self.assertFalse(host._is_git_workspace(str(other)))

    def test_a_name_that_is_not_one_path_component_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            host = CommandHostRuntime(FakeCatalog(), Path(tmp), mode="real")  # type: ignore[arg-type]
            for project, worker in (("..", WORKER), (PROJECT, "a/b"), ("", WORKER)):
                with self.subTest(project=project, worker=worker), self.assertRaises(HostError):
                    host._git_workspaces.path(project, worker)


if __name__ == "__main__":
    unittest.main()
