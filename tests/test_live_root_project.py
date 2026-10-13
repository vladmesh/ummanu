"""A card whose project repository is the live root has nowhere to land (ummanu-32).

The dispatcher's instance-repo landing is gone. Such a card is refused fail-closed at admission,
naming the live root, and the host refuses it again at workspace preparation and at the merge.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest import mock

from ummanu.dispatch import claim
from ummanu.dispatch.host import CommandHostRuntime, live_root_project_refusal
from ummanu.dispatch.runtime_provenance import RuntimeProvenance
from ummanu.dispatch.types import HostError
from ummanu.head_health import HeadReadiness
from ummanu.projects.contract import CANNOT_ATTEST_PROJECT, ContractVerdict


class _Catalog:
    """One registered project, `instance`, whose repository is `repo`; `live_root` is the live root."""

    def __init__(self, live_root: Path, repo: Path) -> None:
        self.instance_dir = live_root
        self.repo = repo
        self.verdicts: list[str] = []

    def binding(self, project: str) -> dict:
        if project != "instance":
            raise HostError(f"project {project!r} is not registered in the instance")
        return {"repo": str(self.repo), "default_branch": "main"}

    def project_default_branch(self, project: str) -> str:
        return "main"

    def integration_base(self, project: str, override: str | None) -> str:
        return "main"

    def worker_head(self, task: dict) -> str:
        return "codex"

    def review_head(self, task: dict) -> str:
        return "codex-reviewer"

    def head_fallback(self, head: str) -> list[str]:
        return []

    def broad_check_verdict(self, project: str) -> ContractVerdict:
        self.verdicts.append(project)
        return ContractVerdict.as_refused(CANNOT_ATTEST_PROJECT, "instance", "stand-in refusal")


class _ClaimRuntime:
    owner = "dispatcher"

    def __init__(self, catalog: _Catalog) -> None:
        self.catalog = catalog
        self.writer = mock.Mock()
        self.audit = mock.Mock()
        self.audit.committed_event.return_value = None
        self.audit.events.return_value = []
        self.host = mock.Mock()

    def head_readiness(self, head: str) -> HeadReadiness:
        return HeadReadiness("acct", "ready", "", time.time())


class _ValidProduction:
    """The production runtime this host's release fences ask; always the registered one."""

    interpreter = sys.executable
    product_root = "/registered/ummanu"

    def probe(self) -> RuntimeProvenance:
        return RuntimeProvenance("valid", sys.executable, self.product_root, "", ())


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


class LiveRootRefusalTests(unittest.TestCase):
    def test_only_a_repository_that_resolves_to_the_live_root_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            live = Path(tmp) / "instance"
            live.mkdir()
            alias = Path(tmp) / "alias"
            alias.symlink_to(live)

            refusal = live_root_project_refusal(_Catalog(live, live), "instance")
            self.assertIn(f"project 'instance' names the live root {live} as its repository", refusal)
            self.assertIn("ummanu config check", refusal)
            self.assertTrue(live_root_project_refusal(_Catalog(live, alias), "instance"))
            self.assertTrue(live_root_project_refusal(_Catalog(live, Path(f"{live}/")), "instance"))

            self.assertEqual(live_root_project_refusal(_Catalog(live, Path(tmp) / "project"), "instance"), "")
            self.assertEqual(live_root_project_refusal(_Catalog(live, live / "nested"), "instance"), "")
            # Not this check's question: an unregistered project fails on its binding elsewhere.
            self.assertEqual(live_root_project_refusal(_Catalog(live, live), "unregistered"), "")
            self.assertEqual(live_root_project_refusal(_Catalog(live, live), ""), "")
            self.assertEqual(live_root_project_refusal(SimpleNamespace(), "instance"), "")


class AdmissionTests(unittest.TestCase):
    # A sprint card, so sprint admission admits it without reading the board.
    TASK: ClassVar[dict] = {
        "ref": "ummanu-1", "project": "instance", "sprint": "sprint:1", "kind": "code", "workspace": {}
    }

    def _prepare(self, catalog: _Catalog) -> tuple[dict, list[dict], _ClaimRuntime]:
        runtime = _ClaimRuntime(catalog)
        blocks: list[dict] = []
        with mock.patch.object(
            claim, "_write_claim_preflight_block", side_effect=lambda *args, **kwargs: blocks.append(kwargs)
        ):
            result = claim._prepare_claim(runtime, dict(self.TASK), {}, {}, "attempt-1")
        return result, blocks, runtime

    def test_a_card_on_the_live_root_is_blocked_at_admission_before_the_contract_or_the_host(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            live = Path(tmp)
            catalog = _Catalog(live, live)

            result, blocks, runtime = self._prepare(catalog)

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["step"], "live-root-project-refused")
        self.assertIn(f"names the live root {live}", result["failure_reason"])
        self.assertEqual([block["action"] for block in blocks], [claim.LIVE_ROOT_PROJECT_BLOCKED_ACTION])
        self.assertIn("refused at admission", blocks[0]["reason"])
        self.assertIn(f"names the live root {live}", blocks[0]["reason"])
        # The claim is the door to Blocked, and nothing else was asked or done.
        runtime.writer.claim.assert_called_once()
        self.assertEqual(catalog.verdicts, [])
        runtime.host.project_git_access.assert_not_called()
        self.assertEqual(runtime.host.mock_calls, [])

    def test_a_card_on_another_repository_goes_on_to_the_contract_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            catalog = _Catalog(Path(tmp) / "instance", Path(tmp) / "project")

            result, blocks, _ = self._prepare(catalog)

        self.assertEqual(catalog.verdicts, ["instance"])
        self.assertEqual(result["step"], "contract-preflight")
        self.assertEqual([block["action"] for block in blocks], ["contract-preflight-blocked"])


class HostRefusalTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.remote = self.root / "remote.git"
        self.live = self.root / "instance"
        self.workspace = self.root / "workspace"
        subprocess.run(["git", "init", "-q", "--bare", "--initial-branch", "main", str(self.remote)], check=True)
        subprocess.run(["git", "clone", "-q", str(self.remote), str(self.live)], check=True, capture_output=True)
        for repo in (self.live,):
            _git(repo, "config", "user.name", "Fixture")
            _git(repo, "config", "user.email", "fixture@example.invalid")
        (self.live / "instance.yaml").write_text("version: 1\n", encoding="utf-8")
        _git(self.live, "add", "instance.yaml")
        _git(self.live, "commit", "-qm", "seed")
        _git(self.live, "push", "-q", "origin", "main")
        subprocess.run(["git", "clone", "-q", str(self.remote), str(self.workspace)], check=True, capture_output=True)
        _git(self.workspace, "config", "user.name", "Fixture")
        _git(self.workspace, "config", "user.email", "fixture@example.invalid")
        _git(self.workspace, "checkout", "-qb", "pipeline/ummanu-1")
        (self.workspace / "result.txt").write_text("green result\n", encoding="utf-8")
        _git(self.workspace, "add", "result.txt")
        _git(self.workspace, "commit", "-qm", "feature")
        self.host = CommandHostRuntime(
            _Catalog(self.live, self.live),  # type: ignore[arg-type]
            self.root,
            mode="real",
            production_runtime=_ValidProduction(),  # type: ignore[arg-type]
        )

    def test_the_merge_lands_nothing_in_the_live_root_or_its_remote(self) -> None:
        published = _git(self.remote, "rev-parse", "refs/heads/main")
        local = _git(self.live, "rev-parse", "HEAD")

        with self.assertRaisesRegex(HostError, "nothing was merged: .*names the live root"):
            self.host.complete_green(
                {"ref": "ummanu-1", "project": "instance"}, SimpleNamespace(workspace=str(self.workspace))
            )

        self.assertEqual(_git(self.remote, "rev-parse", "refs/heads/main"), published)
        self.assertEqual(_git(self.live, "rev-parse", "HEAD"), local)
        self.assertFalse((self.live / "result.txt").exists())

    def test_no_workspace_is_prepared_on_the_live_root(self) -> None:
        with self.assertRaisesRegex(HostError, f"names the live root {self.live}"):
            self.host._require_project_available("instance")

if __name__ == "__main__":
    unittest.main()
