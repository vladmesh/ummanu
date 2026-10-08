"""Seed versus integration base: what a reslice successor inherits, and where it lands.

Two field incidents of one root cause sit behind this module. `codegen-orchestrator-1197`
(issue:a858c044707de792a10f) and `codegen-orchestrator-1236` (issue:2c82ba8f5d1c3bf5b8cc) were both
resliced from a predecessor by pointing `workspace.base_branch` at that predecessor's `pipeline/*`
branch. The release pull request opened into that branch, the project's `pull_request` workflow does
not trigger for it, GitHub created zero check-runs, and an empty rollup reads as `pending` — so each
card sat for the gate's whole six-hour pending ceiling before a person noticed.

The fix separates the two meanings the one field carried, and the tests below hold each half:
admission refuses a card branch as an integration base, the checkout is cut from the seed, the pull
request and the merge use the integration base, and a rollup that nothing can ever fill is a typed
red instead of six hours of ordinary-looking pending.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests.dispatcher_fixtures import CARD_REF, DispatcherRuntimeFixture
from tests.fakes.dispatcher import FakeCatalog
from tests.fakes.tasks import writer_seed
from tests.production_runtime_fixtures import registered_production_runtime
from tests.sql_backend_fixtures import card_store
from ummanu.dispatch.gate import GateResult, _impossible_trigger_reason, gate_check
from ummanu.dispatch.host import CommandHostRuntime
from ummanu.dispatch.launch import BRING_UP_CAUSE_CLASSES, CAUSE_BASE_BRANCH_CONTRACT
from ummanu.dispatch.types import HostError
from ummanu.infra.github_credential import PROJECT_GIT_PHASE, RemoteExecution
from ummanu.projects.integration_base import (
    IntegrationBaseError,
    integration_base_refusal,
    resolve_integration_base,
    seed_ref_refusal,
)
from ummanu.tasks import TaskError, TaskReader, TaskWriter


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)
    return result.stdout.strip()


class IntegrationBaseRuleTests(unittest.TestCase):
    """What a project will and will not integrate into, decided from the binding alone."""

    def test_no_override_is_the_projects_default_branch(self) -> None:
        self.assertEqual(
            resolve_integration_base(default_branch="main", declared=None, override=None), "main"
        )
        self.assertEqual(
            resolve_integration_base(default_branch="trunk", declared=None, override=""), "trunk"
        )

    def test_the_default_branch_and_a_declared_branch_are_accepted(self) -> None:
        self.assertEqual(
            resolve_integration_base(default_branch="main", declared=["develop"], override="main"),
            "main",
        )
        self.assertEqual(
            resolve_integration_base(default_branch="main", declared=["develop"], override="develop"),
            "develop",
        )

    def test_a_card_branch_is_refused_by_name(self) -> None:
        """The incident's exact value, refused with the reason a reader can act on."""
        with self.assertRaises(IntegrationBaseError) as refused:
            resolve_integration_base(
                default_branch="main",
                declared=["pipeline/codegen-orchestrator-1235"],
                override="pipeline/codegen-orchestrator-1235",
            )

        message = str(refused.exception)
        self.assertIn("pipeline/codegen-orchestrator-1235", message)
        self.assertIn("card branch", message)
        self.assertIn("--seed-ref", message)

    def test_an_undeclared_branch_is_refused_and_lists_what_is_declared(self) -> None:
        with self.assertRaises(IntegrationBaseError) as refused:
            resolve_integration_base(default_branch="main", declared=["develop"], override="release-9")

        self.assertIn("release-9", str(refused.exception))
        self.assertIn("develop", str(refused.exception))

    def test_an_object_id_is_a_seed_not_a_base(self) -> None:
        self.assertIn("object id", integration_base_refusal("5" * 40))
        self.assertEqual(seed_ref_refusal("5" * 40), "")

    def test_a_ref_that_is_not_a_ref_is_refused_before_it_reaches_git(self) -> None:
        for value in ("--upload-pack=evil", "a//b", "../etc", "main/"):
            with self.subTest(value=value):
                self.assertNotEqual(seed_ref_refusal(value), "")
                self.assertNotEqual(integration_base_refusal(value), "")

    def test_the_refusal_class_is_the_cards_own_contract_not_the_hosts(self) -> None:
        self.assertEqual(BRING_UP_CAUSE_CLASSES[CAUSE_BASE_BRANCH_CONTRACT], "task")


class WorkflowTriggerTests(unittest.TestCase):
    """ "Nothing can ever post these checks" is only ever answered from positive evidence."""

    def _workspace(self, workflows: dict[str, str]) -> Path:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(root)], check=False))
        directory = root / ".github" / "workflows"
        directory.mkdir(parents=True)
        for name, text in workflows.items():
            (directory / name).write_text(text, encoding="utf-8")
        return root

    CI_YML = """
name: CI
on:
  push:
    branches: [main]
  pull_request:
    branches: [main, develop]
jobs:
  unit:
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
"""

    def test_the_incident_shape_is_named_as_impossible(self) -> None:
        workspace = self._workspace({"ci.yml": self.CI_YML})

        reason = _impossible_trigger_reason(str(workspace), "pipeline/codegen-orchestrator-1235")

        self.assertIn("pipeline/codegen-orchestrator-1235", reason)
        self.assertIn("ci.yml", reason)
        self.assertIn("never create a check-run", reason)

    def test_a_base_the_workflow_admits_is_not_impossible(self) -> None:
        workspace = self._workspace({"ci.yml": self.CI_YML})

        self.assertEqual(_impossible_trigger_reason(str(workspace), "main"), "")
        self.assertEqual(_impossible_trigger_reason(str(workspace), "develop"), "")

    def test_an_unfiltered_pull_request_trigger_admits_every_base(self) -> None:
        workspace = self._workspace({"ci.yml": "on: pull_request\njobs: {}\n"})

        self.assertEqual(_impossible_trigger_reason(str(workspace), "anything"), "")

    def test_branches_ignore_is_honoured(self) -> None:
        workspace = self._workspace(
            {"ci.yml": "on:\n  pull_request:\n    branches-ignore: ['pipeline/**']\njobs: {}\n"}
        )

        self.assertEqual(_impossible_trigger_reason(str(workspace), "main"), "")
        self.assertNotEqual(_impossible_trigger_reason(str(workspace), "pipeline/x-1"), "")

    def test_a_star_does_not_cross_a_slash(self) -> None:
        workspace = self._workspace({"ci.yml": "on:\n  pull_request:\n    branches: ['*']\njobs: {}\n"})

        self.assertEqual(_impossible_trigger_reason(str(workspace), "main"), "")
        self.assertNotEqual(_impossible_trigger_reason(str(workspace), "release/9"), "")

    def test_no_workflow_directory_leaves_the_verdict_alone(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(root)], check=False))

        self.assertEqual(_impossible_trigger_reason(str(root), "main"), "")

    def test_a_workflow_that_will_not_parse_leaves_the_verdict_alone(self) -> None:
        """A file this reader cannot understand is not evidence that CI cannot run."""
        workspace = self._workspace({"ci.yml": "on: [\n  unterminated\n"})

        self.assertEqual(_impossible_trigger_reason(str(workspace), "main"), "")


class CardAdmissionTests(unittest.TestCase):
    """The board refuses a card branch as an integration base before a workspace exists."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.client = card_store(self, writer_seed(), instance_dir=self.tmpdir.name)
        # The predecessor a successor names is a card of the board: the store links a supersession
        # only to a card it holds (§3.5).
        self.client.add_card(
            99, "codegen-orchestrator-1235", project="codegen-orchestrator", state="done"
        )
        self.reader = TaskReader(self.client)  # type: ignore[arg-type]
        self.writer = TaskWriter(self.client, data_dir=self.tmpdir.name)  # type: ignore[arg-type]

    def _create(self, **overrides: object) -> dict:
        # A proposal in Issues: admission needs no open sprint, and the fields under test are
        # written by the same create path an execution card takes.
        arguments: dict[str, object] = {
            "role": "worker",
            "actor": "operator",
            "project": "ummanu",
            "task_type": "code",
            "title": "successor of codegen-orchestrator-1235",
            "target": "issues",
        }
        arguments.update(overrides)
        return self.writer.create(**arguments)  # type: ignore[arg-type]

    def test_a_card_branch_base_is_refused_with_its_own_error_code(self) -> None:
        with self.assertRaises(TaskError) as refused:
            self._create(base_branch="pipeline/codegen-orchestrator-1235")

        self.assertEqual(refused.exception.code, "base_branch_not_integration_target")
        self.assertIn("card branch", str(refused.exception))
        self.assertIn("--seed-ref", str(refused.exception))
        self.assertEqual([call for call in self.client.calls if call[0] == "createTask"], [])

    def test_a_seed_without_its_predecessor_is_refused(self) -> None:
        with self.assertRaisesRegex(TaskError, "--supersedes"):
            self._create(seed_ref="pipeline/codegen-orchestrator-1235")

    def test_a_predecessor_without_a_seed_is_refused(self) -> None:
        with self.assertRaisesRegex(TaskError, "requires --seed-ref"):
            self._create(supersedes="codegen-orchestrator-1235")

    def test_a_successor_carries_its_seed_and_its_provenance(self) -> None:
        created = self._create(
            seed_ref="565ad92f" + "0" * 32,
            supersedes="codegen-orchestrator-1235",
            request_id="successor-create",
        )

        workspace = self.reader.show(created["task"]["ref"])["workspace"]
        self.assertEqual(workspace["seed_ref"], "565ad92f" + "0" * 32)
        self.assertEqual(workspace["supersedes"], "codegen-orchestrator-1235")
        # The successor integrates where every other card does: nothing on the card says otherwise.
        self.assertIsNone(workspace["base_branch"])

    def test_an_ordinary_card_is_admitted_exactly_as_before(self) -> None:
        created = self._create(request_id="ordinary-create")

        workspace = self.reader.show(created["task"]["ref"])["workspace"]
        self.assertIsNone(workspace["base_branch"])
        self.assertIsNone(workspace["seed_ref"])
        self.assertIsNone(workspace["supersedes"])


class CatalogSeedTests(unittest.TestCase):
    """Which ref the checkout starts from, and which one the increment lands on."""

    def setUp(self) -> None:
        self.catalog = FakeCatalog()

    def test_an_ordinary_card_seeds_from_its_integration_base(self) -> None:
        task = {"ref": "ummanu-1", "project": "ummanu", "workspace": {}}

        self.assertEqual(self.catalog.workspace_seed("ummanu", task), "main")
        self.assertEqual(self.catalog.integration_base("ummanu", None), "main")

    def test_a_card_naming_the_default_branch_behaves_identically(self) -> None:
        task = {"ref": "ummanu-1", "project": "ummanu", "workspace": {"base_branch": "main"}}

        self.assertEqual(self.catalog.workspace_seed("ummanu", task), "main")
        self.assertEqual(self.catalog.integration_base("ummanu", "main"), "main")

    def test_a_successor_seeds_from_the_predecessor_and_still_integrates_into_main(self) -> None:
        task = {
            "ref": "codegen-orchestrator-1236",
            "project": "ummanu",
            "workspace": {
                "seed_ref": "pipeline/codegen-orchestrator-1235",
                "supersedes": "codegen-orchestrator-1235",
            },
        }

        self.assertEqual(self.catalog.workspace_seed("ummanu", task), "pipeline/codegen-orchestrator-1235")
        self.assertEqual(
            self.catalog.integration_base("ummanu", task["workspace"].get("base_branch")), "main"
        )

    def test_a_legacy_card_branch_base_is_refused_as_this_cards_own_contract(self) -> None:
        """Nothing repairs a card admitted before the split; it fails fast, typed, and once."""
        with self.assertRaises(HostError) as refused:
            self.catalog.integration_base("ummanu", "pipeline/codegen-orchestrator-1235")

        self.assertEqual(getattr(refused.exception, "bring_up_cause", ""), CAUSE_BASE_BRANCH_CONTRACT)
        self.assertIn("card branch", str(refused.exception))


def _gated_workspace(root: Path, base: str, branch: str, *, workflow: str) -> Path:
    """A worker workspace on `branch`, one commit ahead of `base` on an `origin` remote."""
    bare = root / "origin.git"
    bare.mkdir(parents=True)
    git(root, "init", "--quiet", "--bare", "--initial-branch", base, str(bare))
    ws = root / "ws"
    ws.mkdir()
    git(root, "init", "--quiet", "--initial-branch", base, str(ws))
    git(ws, "config", "user.name", "Test User")
    git(ws, "config", "user.email", "test@example.invalid")
    workflows = ws / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(workflow, encoding="utf-8")
    (ws / "README.md").write_text("seed\n", encoding="utf-8")
    git(ws, "add", "-A")
    git(ws, "commit", "--quiet", "-m", "seed")
    git(ws, "remote", "add", "origin", str(bare))
    git(ws, "push", "--quiet", "origin", base)
    git(ws, "checkout", "--quiet", "-b", branch)
    (ws / "work.txt").write_text("work\n", encoding="utf-8")
    git(ws, "add", "-A")
    git(ws, "commit", "--quiet", "-m", "work")
    return ws


class _Catalog:
    def __init__(self, adapter: dict, *, repo: Path | None = None) -> None:
        self._adapter = adapter
        self._repo = repo
        self.instance_dir = Path("/nonexistent-instance")

    def adapter(self, project: str) -> dict:
        return self._adapter

    def project_default_branch(self, project: str) -> str:
        return "main"

    def integration_base(self, project: str, override: str | None) -> str:
        return resolve_integration_base(default_branch="main", declared=None, override=override)

    def workspace_seed(self, project: str, task: dict) -> str:
        workspace = task.get("workspace") or {}
        return str(workspace.get("seed_ref") or "") or self.integration_base(
            project, workspace.get("base_branch")
        )

    def binding(self, project: str) -> dict:
        repo = str(self._repo) if self._repo is not None else f"/home/dev/{project}"
        return {"repo": repo, "default_branch": "main"}


class _GateHost(CommandHostRuntime):
    """The real gate over a real git workspace, with every `gh` call answered in process."""

    def __init__(self, root: Path, *, pr_base: str = "", check_runs: list | None = None) -> None:
        super().__init__(_Catalog({"validation": {"ci": "github"}}), root, mode="real")  # type: ignore[arg-type]
        # "" is "no pull request is open"; anything else is the base the open one targets.
        self.pr_base = pr_base
        self.check_runs = list(check_runs or [])
        self.gh: list[list[str]] = []

    def _gh(self, args):
        self.gh.append(list(args))
        done = lambda out="": subprocess.CompletedProcess(list(args), 0, out, "")
        if args[1:3] == ["repo", "view"]:
            return done("example-org/sample\n")
        if args[1:3] == ["pr", "list"]:
            if not self.pr_base:
                return done("[]")
            return done(json.dumps([{"number": 42, "baseRefName": self.pr_base}]))
        if args[1:3] == ["pr", "create"]:
            self.pr_base = args[args.index("--base") + 1]
            return done("https://github.com/example-org/sample/pull/42\n")
        if args[1:3] == ["pr", "edit"] and "--base" in args:
            self.pr_base = args[args.index("--base") + 1]
            return done()
        if args[1:3] == ["pr", "view"]:
            return done(json.dumps({"title": "t", "body": "b"}))
        if args[1] == "api":
            if args[2].endswith("/check-runs"):
                return done(json.dumps(self.check_runs))
            return done("[]")
        return done()

    def run_capture(self, args, label, *, cwd=None):  # type: ignore[override]
        return self._gh(args) if args[:1] == ["gh"] else super().run_capture(args, label, cwd=cwd)

    def _run(self, args, label, *, cwd=None):  # type: ignore[override]
        return self._gh(args) if args[:1] == ["gh"] else super()._run(args, label, cwd=cwd)


TRIGGERS_MAIN = """
name: CI
on:
  pull_request:
    branches: [main]
jobs:
  unit:
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
"""


class GateTopologyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.ws = _gated_workspace(
            self.root, "main", "pipeline/codegen-orchestrator-1236", workflow=TRIGGERS_MAIN
        )

    def _record(self):
        return SimpleNamespace(workspace=str(self.ws), gate_pr_authorship={})

    def _task(self, workspace: dict) -> dict:
        return {
            "ref": "codegen-orchestrator-1236",
            "project": "codegen_orchestrator",
            "workspace": workspace,
        }

    def _gh_calls(self, host, *prefix: str) -> list[list[str]]:
        return [call for call in host.gh if call[: len(prefix)] == list(prefix)]

    def test_a_seeded_successor_opens_its_pr_into_the_default_branch(self) -> None:
        """The successor inherits the predecessor's content, and lands on `main` all the same."""
        host = _GateHost(self.root, check_runs=[])
        task = self._task(
            {
                "seed_ref": "pipeline/codegen-orchestrator-1235",
                "supersedes": "codegen-orchestrator-1235",
            }
        )

        result = gate_check(host, task, self._record())

        created = self._gh_calls(host, "gh", "pr", "create")
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0][created[0].index("--base") + 1], "main")
        # CI can run for that base, so an empty rollup is still ordinary pending.
        self.assertEqual(result.status, "pending")

    def test_an_ordinary_card_opens_the_same_pull_request_it_always_did(self) -> None:
        host = _GateHost(self.root, check_runs=[])

        gate_check(host, self._task({}), self._record())

        created = self._gh_calls(host, "gh", "pr", "create")
        self.assertEqual(created[0][created[0].index("--base") + 1], "main")
        self.assertEqual(self._gh_calls(host, "gh", "pr", "edit"), [])
        self.assertEqual(self._gh_calls(host, "gh", "pr", "close"), [])

    def test_a_pull_request_on_a_card_branch_is_retargeted_and_made_to_run(self) -> None:
        """The repair the PO did by hand: base to `main`, CI on the same candidate, no rework."""
        host = _GateHost(self.root, pr_base="pipeline/codegen-orchestrator-1235", check_runs=[])

        result = gate_check(host, self._task({}), self._record())

        edited = self._gh_calls(host, "gh", "pr", "edit")
        self.assertEqual(edited[0][edited[0].index("--base") + 1], "main")
        # A base change is not one of the events a `pull_request` workflow subscribes to, so the
        # pull request is reopened on the same head to produce one that is.
        self.assertEqual(len(self._gh_calls(host, "gh", "pr", "close")), 1)
        self.assertEqual(len(self._gh_calls(host, "gh", "pr", "reopen")), 1)
        self.assertEqual(self._gh_calls(host, "gh", "pr", "create"), [])
        self.assertEqual(result.status, "pending")

    def test_a_rollup_nothing_can_fill_is_a_typed_red_not_six_hours_of_pending(self) -> None:
        """The whole incident in one assertion (issue:a858c044707de792a10f)."""
        workspace = _gated_workspace(
            self.root / "other",
            "main",
            "pipeline/codegen-orchestrator-1197",
            workflow="name: CI\non:\n  pull_request:\n    branches: [develop]\njobs: {}\n",
        )
        host = _GateHost(self.root, check_runs=[])
        record = SimpleNamespace(workspace=str(workspace), gate_pr_authorship={})

        result = gate_check(
            host, {"ref": "codegen-orchestrator-1197", "project": "p", "workspace": {}}, record
        )

        self.assertEqual(result.status, "red")
        self.assertEqual(result.failure_class, "topology")
        self.assertEqual(result.failure_reason, "ci-trigger-impossible")
        self.assertIn("never create a check-run", result.summary)
        # A red gate is not a green one: nothing here may be reused as validation evidence.
        self.assertIsNone(result.attestation)


class _MergeHost(CommandHostRuntime):
    """`complete_green`'s git and gh, recorded rather than run."""

    def __init__(self, root: Path, *, pr_base: str) -> None:
        super().__init__(  # type: ignore[arg-type]
            FakeCatalog({"validation": {"ci": "github"}}),
            root,
            mode="real",
            production_runtime=registered_production_runtime(root),
        )
        self.pr_base = pr_base
        self.runs: list[list[str]] = []

    def _project_remote(self, project, checkout):  # type: ignore[override]
        # No checkout exists behind the stubbed `_run`; its origin is declared local, so remote Git
        # keeps the explicit non-managed command this fixture records.
        return RemoteExecution(str(self.data_dir / "origin.git"), PROJECT_GIT_PHASE)

    def _run(self, args, label, *, cwd=None):  # type: ignore[override]
        self.runs.append(list(args))
        if args[:3] == ["gh", "pr", "view"] and "baseRefName" in args:
            return subprocess.CompletedProcess(args, 0, f"{self.pr_base}\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")


class MergeBaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.task = {
            "ref": "codegen-orchestrator-1236",
            "project": "ummanu",
            "workspace": {
                "seed_ref": "pipeline/codegen-orchestrator-1235",
                "supersedes": "codegen-orchestrator-1235",
            },
        }
        self.record = SimpleNamespace(workspace=str(self.root / "ws"))

    def test_a_seeded_card_merges_on_the_default_branch(self) -> None:
        host = _MergeHost(self.root, pr_base="main")

        host.complete_green(self.task, self.record)

        merged = [" ".join(run) for run in host.runs if run[:3] == ["gh", "pr", "merge"]]
        self.assertEqual(merged, ["gh pr merge pipeline/codegen-orchestrator-1236 --merge"])
        refreshed = [" ".join(run) for run in host.runs if "--ff-only" in run]
        self.assertEqual(refreshed[-1].split()[-1], "origin/main")

    def test_a_pull_request_still_on_a_card_branch_is_never_merged(self) -> None:
        host = _MergeHost(self.root, pr_base="pipeline/codegen-orchestrator-1235")

        with self.assertRaises(HostError) as refused:
            host.complete_green(self.task, self.record)

        self.assertIn("pipeline/codegen-orchestrator-1235", str(refused.exception))
        self.assertIn("nothing was merged", str(refused.exception))
        self.assertEqual([run for run in host.runs if run[:3] == ["gh", "pr", "merge"]], [])

    def test_an_unreadable_base_is_never_merged_either(self) -> None:
        host = _MergeHost(self.root, pr_base="")

        with self.assertRaisesRegex(HostError, "unreadable"):
            host.complete_green(self.task, self.record)

        self.assertEqual([run for run in host.runs if run[:3] == ["gh", "pr", "merge"]], [])


class _SeedRepo:
    """A bare `origin` with `main` and a predecessor card branch, plus the project checkout."""

    def __init__(self, root: Path) -> None:
        self.remote = root / "origin.git"
        self.repo = root / "project"
        author = root / "author"
        git(root, "init", "--quiet", "--bare", "--initial-branch", "main", str(self.remote))
        git(root, "clone", "--quiet", str(self.remote), str(author))
        git(author, "config", "user.name", "Test User")
        git(author, "config", "user.email", "test@example.invalid")
        (author / "README.md").write_text("seed\n", encoding="utf-8")
        git(author, "add", "-A")
        git(author, "commit", "--quiet", "-m", "seed")
        git(author, "push", "--quiet", "origin", "main")
        self.main_sha = git(author, "rev-parse", "HEAD")
        # The project checkout the dispatcher cuts worktrees from, cloned before the predecessor
        # published anything: a seed genuinely has to be fetched rather than found sitting there.
        git(root, "clone", "--quiet", str(self.remote), str(self.repo))
        git(self.repo, "config", "user.name", "Test User")
        git(self.repo, "config", "user.email", "test@example.invalid")
        # The predecessor's candidate: published on its card branch, never merged to main.
        git(author, "checkout", "--quiet", "-b", "pipeline/codegen-orchestrator-1235")
        (author / "predecessor.txt").write_text("unreleased content\n", encoding="utf-8")
        git(author, "add", "-A")
        git(author, "commit", "--quiet", "-m", "predecessor candidate")
        git(author, "push", "--quiet", "origin", "pipeline/codegen-orchestrator-1235")
        self.candidate_sha = git(author, "rev-parse", "HEAD")


class SeedFetchTests(unittest.TestCase):
    """`_fetch_seed`: what is fetched, what the worktree is cut at, and what is refused."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.fixture = _SeedRepo(self.root)
        self.host = CommandHostRuntime(_Catalog({}), self.root, mode="real")  # type: ignore[arg-type]

    def _resolves(self, ref: str) -> str:
        return git(self.fixture.repo, "rev-parse", f"{ref}^{{commit}}")

    def test_a_branch_seed_is_fetched_by_name_and_cut_at_its_tracking_ref(self) -> None:
        start = self.host._fetch_seed(
            self.fixture.repo, "pipeline/codegen-orchestrator-1235", project="codegen-orchestrator"
        )

        self.assertEqual(start, "origin/pipeline/codegen-orchestrator-1235")
        self.assertEqual(self._resolves(start), self.fixture.candidate_sha)

    def test_an_object_id_seed_is_cut_at_the_object_itself(self) -> None:
        """A remote will not serve an object id by name, so the whole remote is fetched instead."""
        start = self.host._fetch_seed(
            self.fixture.repo, self.fixture.candidate_sha, project="codegen-orchestrator"
        )

        self.assertEqual(start, self.fixture.candidate_sha)
        self.assertEqual(self._resolves(start), self.fixture.candidate_sha)

    def test_a_seed_object_the_remote_does_not_carry_is_this_cards_own_contract(self) -> None:
        """Not a host that failed: the predecessor candidate this card inherits is not there."""
        absent = "0" * 39 + "1"

        with self.assertRaises(HostError) as refused:
            self.host._fetch_seed(self.fixture.repo, absent, project="codegen-orchestrator")

        self.assertEqual(getattr(refused.exception, "bring_up_cause", ""), CAUSE_BASE_BRANCH_CONTRACT)
        self.assertIn(absent[:12], str(refused.exception))
        self.assertIn("never published", str(refused.exception))

    def test_a_branch_seed_the_remote_does_not_carry_is_a_determinate_git_refusal(self) -> None:
        with self.assertRaises(HostError) as refused:
            self.host._fetch_seed(self.fixture.repo, "pipeline/never-existed", project="codegen-orchestrator")

        self.assertIn("git fetch", str(refused.exception))


class _WorktreeHost(CommandHostRuntime):
    """The card's git workspace end to end: `GitWorkspaceManager.create` with a real fetch.

    Nothing is stood in for: the fetch, the start point, `git worktree add -b` and the acceptance
    check are real, which is the whole point — this is the test that says a successor's workspace
    really does carry the predecessor's content. The start point is only recorded.
    """

    def __init__(self, catalog, root: Path) -> None:
        super().__init__(catalog, root, mode="real")  # type: ignore[arg-type]
        self.start_points: list[str] = []

    def _fetch_seed(self, repo: Path, seed: str, *, project: str) -> str:
        start = super()._fetch_seed(repo, seed, project=project)
        self.start_points.append(start)
        return start


class SeededWorkspaceTests(unittest.TestCase):
    """The card's central new mechanism: a successor's checkout carries its predecessor's content."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.fixture = _SeedRepo(self.root)
        self.catalog = _Catalog({}, repo=self.fixture.repo)
        self.host = _WorktreeHost(self.catalog, self.root)

    def _cut(self, task: dict, worker_id: str) -> Path:
        """Exactly what `prepare_worker` does: seed for the checkout, base for everything else."""
        seed = self.catalog.workspace_seed("ummanu", task)
        return Path(self.host._git_workspaces.create(task, worker_id, seed))

    def test_a_successor_is_cut_from_the_predecessor_candidate_and_still_integrates_into_main(self) -> None:
        task = {
            "ref": "codegen-orchestrator-1236",
            "project": "ummanu",
            "workspace": {
                "seed_ref": self.fixture.candidate_sha,
                "supersedes": "codegen-orchestrator-1235",
            },
        }

        workspace = self._cut(task, "codegen-orchestrator-1236-successor")

        # The content is genuinely there: the predecessor's unreleased file, at its exact commit.
        self.assertEqual(git(workspace, "rev-parse", "HEAD"), self.fixture.candidate_sha)
        self.assertEqual((workspace / "predecessor.txt").read_text(encoding="utf-8"), "unreleased content\n")
        self.assertEqual(self.host.start_points, [self.fixture.candidate_sha])
        # An object id start point still leaves the worktree on the card's branch, not a detached HEAD.
        self.assertEqual(
            git(workspace, "rev-parse", "--abbrev-ref", "HEAD"),
            "pipeline/codegen-orchestrator-1236",
        )
        # And nothing about the seed moved where the increment lands.
        self.assertEqual(
            self.catalog.integration_base("ummanu", task["workspace"].get("base_branch")), "main"
        )

    def test_a_branch_seed_cuts_the_same_content_through_its_tracking_ref(self) -> None:
        task = {
            "ref": "codegen-orchestrator-1236",
            "project": "ummanu",
            "workspace": {
                "seed_ref": "pipeline/codegen-orchestrator-1235",
                "supersedes": "codegen-orchestrator-1235",
            },
        }

        workspace = self._cut(task, "codegen-orchestrator-1236-branch-seed")

        self.assertEqual(git(workspace, "rev-parse", "HEAD"), self.fixture.candidate_sha)
        self.assertEqual(self.host.start_points, ["origin/pipeline/codegen-orchestrator-1235"])

    def test_an_ordinary_card_is_still_cut_from_its_integration_base(self) -> None:
        """No seed, no change: the checkout starts where every card's checkout always started."""
        task = {"ref": "ummanu-1", "project": "ummanu", "workspace": {}}

        workspace = self._cut(task, "ummanu-1-ordinary")

        self.assertEqual(git(workspace, "rev-parse", "HEAD"), self.fixture.main_sha)
        self.assertEqual(self.host.start_points, ["origin/main"])
        self.assertFalse((workspace / "predecessor.txt").exists())


class _PushCatalog:
    """A non-GitHub project whose repo is not the instance repo: `complete_green`'s plain path."""

    def __init__(self, *, integration_bases: list[str] | None = None) -> None:
        self.instance_dir = Path("/nonexistent-instance")
        self._integration_bases = list(integration_bases or [])

    def adapter(self, project: str) -> dict:
        return {"validation": {"ci": "local", "command": "true"}}

    def binding(self, project: str) -> dict:
        return {"repo": f"/nonexistent-projects/{project}", "default_branch": "main"}

    def project_default_branch(self, project: str) -> str:
        return "main"

    def integration_base(self, project: str, override: str | None) -> str:
        return resolve_integration_base(
            default_branch="main", declared=self._integration_bases, override=override
        )

    def workspace_seed(self, project: str, task: dict) -> str:
        workspace = task.get("workspace") or {}
        return str(workspace.get("seed_ref") or "") or self.integration_base(
            project, workspace.get("base_branch")
        )


class _PushHost(CommandHostRuntime):
    def __init__(self, catalog, root: Path) -> None:
        super().__init__(  # type: ignore[arg-type]
            catalog,
            root,
            mode="real",
            production_runtime=registered_production_runtime(root),
        )
        self.runs: list[list[str]] = []

    def _project_remote(self, project, checkout):  # type: ignore[override]
        # No checkout exists behind the stubbed `_run`; its origin is declared local, so remote Git
        # keeps the explicit non-managed command this fixture records.
        return RemoteExecution(str(self.data_dir / "origin.git"), PROJECT_GIT_PHASE)

    def _run(self, args, label, *, cwd=None):  # type: ignore[override]
        self.runs.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")


class NonGithubPublishTests(unittest.TestCase):
    """The publish path that is not a pull request lands on the card's integration base too.

    It hard-coded `main` while everything around it honoured the base. That was unreachable for any
    sanctioned configuration until `integration_bases` made a non-default base sanctionable, which
    is exactly the kind of latent line a change like this one wakes up.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.record = SimpleNamespace(workspace=str(self.root / "ws"))

    def _publish(self, catalog, workspace: dict) -> list[str]:
        host = _PushHost(catalog, self.root)
        host.complete_green(
            {"ref": "ummanu-770", "project": "ummanu", "workspace": workspace}, self.record
        )
        return [" ".join(run) for run in host.runs]

    def test_an_ordinary_card_still_publishes_onto_the_default_branch(self) -> None:
        commands = self._publish(_PushCatalog(), {})

        self.assertTrue(
            any(command.endswith("push origin pipeline/ummanu-770:main") for command in commands), commands
        )
        self.assertTrue(
            any(command.endswith("merge --ff-only origin/main") for command in commands), commands
        )

    def test_a_declared_integration_base_is_published_onto_and_never_main(self) -> None:
        commands = self._publish(_PushCatalog(integration_bases=["develop"]), {"base_branch": "develop"})

        self.assertTrue(
            any(command.endswith("push origin pipeline/ummanu-770:develop") for command in commands),
            commands,
        )
        self.assertTrue(
            any(command.endswith("merge --ff-only origin/develop") for command in commands), commands
        )
        self.assertFalse(any(command.endswith(":main") for command in commands), commands)
        self.assertFalse(any("origin/main" in command for command in commands), commands)


class TopologyRedRoutingTests(DispatcherRuntimeFixture, unittest.TestCase):
    """A gate that cannot produce CI does not spend a rework round on the worker's code."""

    def test_a_topology_red_blocks_the_card_instead_of_reworking_it(self) -> None:
        self.start_dispatcher()
        self._run_worker_to_validate()
        self.host.gate_results = [
            GateResult(
                "red",
                "CI cannot run for `pipeline/ummanu-510`",
                failure_class="topology",
                failure_reason="ci-trigger-impossible",
            )
        ]
        restarts = self.host.calls.count("restart_worker")

        outcome = self.tick()

        self.assertEqual(outcome["status"], "blocked")
        self.assertEqual(self.reader.show(CARD_REF)["state"], "blocked")
        self.assertEqual(self.host.calls.count("restart_worker"), restarts)
        self.assertEqual(outcome["reason"], "gate ci topology")
        reason = " ".join(str(comment) for comment in self.reader.show(CARD_REF)["comments"])
        self.assertIn("no rework changes that", reason)


if __name__ == "__main__":
    unittest.main()
