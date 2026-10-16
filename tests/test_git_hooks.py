"""The shipped pre-push hook, its global install, its doctor finding and the dispatcher's leased push."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.production_runtime_fixtures import registered_production_runtime
from ummanu.dispatch import gate_pr
from ummanu.dispatch.host import CommandHostRuntime
from ummanu.installation.upgrade import workspaces as upgrade_workspaces
from ummanu.projects.availability import ProjectAvailability
from ummanu.runtime import git_hooks

PRODUCT_ROOT = Path(__file__).resolve().parents[1]
BRANCH = "pipeline/sample-1"


class HookedRepositoryFixture(unittest.TestCase):
    """A throwaway HOME whose global config names the shipped hooks, a bare remote and a clone."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        (self.home / ".gitconfig").write_text(
            "[user]\n\tname = test\n\temail = test@example.invalid\n", encoding="utf-8"
        )
        environment = mock.patch.dict(
            os.environ,
            {"HOME": str(self.home), "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"},
        )
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("GIT_CONFIG_GLOBAL", "XDG_CONFIG_HOME", "GIT_DIR", "GIT_WORK_TREE", git_hooks.REWRITE_OPT_OUT_ENV):
            os.environ.pop(name, None)
        self.assertTrue(git_hooks.install_hooks_path(self.home, PRODUCT_ROOT, []))
        self.bare = self.root / "remote.git"
        self.git(self.root, "init", "--quiet", "--bare", "--initial-branch", "main", str(self.bare))
        self.work = self.root / "work"
        self.git(self.root, "clone", "--quiet", str(self.bare), str(self.work))
        self.commit("README.md")
        self.git(self.work, "push", "--quiet", "origin", "HEAD:refs/heads/main")
        self.git(self.work, "checkout", "--quiet", "-b", BRANCH)
        self.commit("work.txt")
        self.git(self.work, "push", "--quiet", "origin", BRANCH)

    def git(self, cwd: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        completed = self.attempt(cwd, *args, env=env)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed

    def attempt(self, cwd: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            env={**os.environ, **(env or {})},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def commit(self, name: str) -> str:
        (self.work / name).write_text(f"{name}\n", encoding="utf-8")
        self.git(self.work, "add", name)
        self.git(self.work, "commit", "--quiet", "-m", name)
        return self.head()

    def head(self) -> str:
        return self.git(self.work, "rev-parse", "HEAD").stdout.strip()

    def remote_sha(self, branch: str = BRANCH) -> str:
        completed = self.attempt(self.bare, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
        return completed.stdout.strip()

    def diverge(self) -> str:
        self.git(self.work, "commit", "--quiet", "--amend", "-m", "rewritten")
        return self.head()


class PrePushHookTests(HookedRepositoryFixture):
    def test_a_fast_forward_push_passes(self) -> None:
        sha = self.commit("more.txt")

        self.git(self.work, "push", "--quiet", "origin", BRANCH)

        self.assertEqual(self.remote_sha(), sha)

    def test_a_forced_push_of_a_diverged_branch_is_refused_naming_the_ref_and_the_opt_out(self) -> None:
        published = self.remote_sha()
        self.diverge()

        for force in ("--force", f"--force-with-lease=refs/heads/{BRANCH}:{published}"):
            with self.subTest(force=force):
                refused = self.attempt(self.work, "push", force, "origin", BRANCH)
                self.assertNotEqual(refused.returncode, 0)
                self.assertIn(f"non-fast-forward update of refs/heads/{BRANCH}", refused.stderr)
                self.assertIn(git_hooks.REWRITE_OPT_OUT_ENV, refused.stderr)
                self.assertEqual(self.remote_sha(), published)

    def test_a_branch_deletion_is_refused_naming_the_ref_and_the_opt_out(self) -> None:
        published = self.remote_sha()

        refused = self.attempt(self.work, "push", "origin", f":{BRANCH}")

        self.assertNotEqual(refused.returncode, 0)
        self.assertIn(f"delete refs/heads/{BRANCH}", refused.stderr)
        self.assertIn(git_hooks.REWRITE_OPT_OUT_ENV, refused.stderr)
        self.assertEqual(self.remote_sha(), published)

    def test_the_opt_out_lets_a_forced_push_and_a_deletion_through(self) -> None:
        opt_out = {git_hooks.REWRITE_OPT_OUT_ENV: "1"}
        rewritten = self.diverge()

        self.git(self.work, "push", "--quiet", "--force", "origin", BRANCH, env=opt_out)
        self.assertEqual(self.remote_sha(), rewritten)
        self.git(self.work, "push", "--quiet", "origin", f":{BRANCH}", env=opt_out)
        self.assertEqual(self.remote_sha(), "")


class HooksPathInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()

    def test_install_sets_the_global_hooks_path_once_and_clears_the_finding(self) -> None:
        unset = git_hooks.hooks_path_finding(self.home, PRODUCT_ROOT)
        self.assertIsNotNone(unset)
        assert unset is not None
        self.assertIn("(unset)", str(unset["message"]))

        self.assertTrue(git_hooks.install_hooks_path(self.home, PRODUCT_ROOT, []))
        self.assertFalse(git_hooks.install_hooks_path(self.home, PRODUCT_ROOT, []))

        self.assertEqual(git_hooks.configured_hooks_path(self.home), str(git_hooks.hooks_dir(PRODUCT_ROOT)))
        self.assertIsNone(git_hooks.hooks_path_finding(self.home, PRODUCT_ROOT))

    def test_a_hooks_path_naming_elsewhere_is_a_finding(self) -> None:
        (self.home / ".gitconfig").write_text("[core]\n\thooksPath = /elsewhere\n", encoding="utf-8")

        finding = git_hooks.hooks_path_finding(self.home, PRODUCT_ROOT)

        self.assertIsNotNone(finding)
        assert finding is not None
        self.assertEqual(finding["code"], git_hooks.HOOKS_PATH_FINDING)
        self.assertIn("/elsewhere", str(finding["message"]))

    def test_a_product_without_the_hook_is_a_finding_and_is_not_installed(self) -> None:
        product = self.home / "product"
        product.mkdir()

        finding = git_hooks.hooks_path_finding(self.home, product)

        self.assertIsNotNone(finding)
        assert finding is not None
        self.assertIn("missing", str(finding["message"]))
        with self.assertRaises(git_hooks.GitHooksError):
            git_hooks.install_hooks_path(self.home, product, [])
        self.assertEqual(git_hooks.configured_hooks_path(self.home), "")


class ShadowedCheckoutHookTests(unittest.TestCase):
    """A registered checkout or linked worktree that runs its own hook keeps the global path unset."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.checkout = self.root / "project"
        self.git("init", "--quiet", "--initial-branch", "main", str(self.checkout))
        self.git("-C", str(self.checkout), "commit", "--quiet", "--allow-empty", "-m", "initial")
        self.hooks = self.checkout / ".git" / "hooks"
        self.hooks.mkdir(exist_ok=True)

    def git(self, *args: str) -> None:
        subprocess.run(
            ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", *args],
            env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )

    def hook(self, name: str) -> Path:
        path = self.hooks / name
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def assert_refused_naming(self, hook: Path) -> None:
        with self.assertRaisesRegex(git_hooks.GitHooksError, str(hook)):
            git_hooks.install_hooks_path(self.home, PRODUCT_ROOT, [self.checkout])
        self.assertEqual(git_hooks.configured_hooks_path(self.home), "")

    def test_an_executable_default_hook_of_a_registered_checkout_refuses_the_install(self) -> None:
        self.assert_refused_naming(self.hook("pre-commit"))

    def test_a_linked_worktree_that_runs_the_default_hooks_refuses_the_install(self) -> None:
        hook = self.hook("pre-commit")
        own = self.root / "own-hooks"
        own.mkdir()
        self.git("-C", str(self.checkout), "config", "extensions.worktreeConfig", "true")
        self.git("-C", str(self.checkout), "config", "--worktree", "core.hooksPath", str(own))
        self.assertEqual(git_hooks.shadowed_hooks([self.checkout]), [])

        linked = self.root / "linked"
        self.git("-C", str(self.checkout), "worktree", "add", "--quiet", str(linked))
        self.git("-C", str(linked), "config", "--worktree", "--unset", "core.hooksPath")

        self.assertEqual(git_hooks.shadowed_hooks([self.checkout]), [str(hook)])
        self.assert_refused_naming(hook)

    def test_the_upgrade_step_fails_on_a_registered_binding_hook_and_writes_nothing(self) -> None:
        hook = self.hook("pre-commit")
        context = SimpleNamespace(
            runtime_home=self.home,
            runtime_user=None,
            product_root=PRODUCT_ROOT,
            dry_run=False,
            report=SimpleNamespace(bindings=[{"id": "project", "repo": str(self.checkout)}]),
            project_availability=ProjectAvailability(),
        )

        result = upgrade_workspaces.step_git_hooks(context)  # type: ignore[arg-type]

        self.assertEqual(result.status, "failed")
        self.assertIn(str(hook), result.detail)
        self.assertEqual(git_hooks.configured_hooks_path(self.home), "")

    def test_samples_a_local_hooks_path_and_an_absent_checkout_do_not_refuse(self) -> None:
        (self.hooks / "pre-commit.sample").write_text("#!/bin/sh\n", encoding="utf-8")
        (self.hooks / "pre-commit.sample").chmod(0o755)
        self.hook("pre-push")
        self.git("-C", str(self.checkout), "config", "core.hooksPath", str(self.root / "own-hooks"))

        self.assertTrue(
            git_hooks.install_hooks_path(self.home, PRODUCT_ROOT, [self.checkout, self.root / "absent"])
        )
        self.assertEqual(git_hooks.configured_hooks_path(self.home), str(git_hooks.hooks_dir(PRODUCT_ROOT)))


class DispatcherPublicationTests(HookedRepositoryFixture):
    def test_the_leased_publication_rewrites_a_hooked_branch_it_last_published(self) -> None:
        published = self.remote_sha()
        rewritten = self.diverge()
        self.assertNotEqual(self.attempt(self.work, "push", "--force", "origin", BRANCH).returncode, 0)
        catalog = SimpleNamespace(instance_dir=self.root / "instance")
        host = CommandHostRuntime(  # type: ignore[arg-type]
            catalog, self.root / "data", mode="real", production_runtime=registered_production_runtime(self.root)
        )
        host.commit_gate_published_ref = None  # type: ignore[assignment]
        record = SimpleNamespace(
            workspace=str(self.work), gate_published_ref={"branch": BRANCH, "sha": published}
        )

        refused = gate_pr._publish_branch(host, record, str(self.work), BRANCH, rewritten, "sample")

        self.assertIsNone(refused)
        self.assertEqual(self.remote_sha(), rewritten)


if __name__ == "__main__":
    unittest.main()
