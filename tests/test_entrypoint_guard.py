"""The entrypoint guard: a target without the running entrypoint never becomes the production checkout.

Real Git over a fixture origin and checkout. The release path (`production_checkout.advance`) and
the operator path (`upgrade.fast_forward`) refuse the same targets through the same check, before
the board schema or the checkout is touched (secretary-1929, `docs/OPERATIONS.md` §Entrypoint activation).
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ummanu import upgrade
from ummanu.dispatch import entrypoint_guard, production_checkout
from ummanu.dispatch.production_checkout import ProductionActivationRefused
from ummanu.dispatch.runtime_preflight import PACKAGE
from ummanu.dispatch.types import HostError

PREFLIGHT = f"src/{PACKAGE}/dispatch/runtime_preflight.py"
MANIFEST = f'[project]\nname = "product"\n\n[project.scripts]\n{PACKAGE} = "{PACKAGE}.cli:main"\n'


def git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {completed.stderr.strip()}")
    return completed.stdout.strip()


def run(args: list[str], label: str, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """The host runner's contract: the completed process, or `HostError` on a non-zero exit."""
    completed = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise HostError(f"{label} failed: {completed.stderr.strip()}")
    return completed


class EntrypointFixture:
    """An origin, a production checkout at the old release, and an author clone that pushes targets."""

    def setUp(self) -> None:
        super().setUp()  # type: ignore[misc]
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="entrypoint-guard-")))  # type: ignore[attr-defined]
        origin, self.production, self.author = self.root / "origin.git", self.root / "product", self.root / "author"
        git(self.root, "init", "--quiet", "--bare", "--initial-branch", "main", str(origin))
        git(self.root, "clone", "--quiet", str(origin), str(self.production))
        self.configure(self.production)
        self.write(self.production, {PREFLIGHT: "PACKAGE = 'x'\n", "pyproject.toml": MANIFEST, "README.md": "old\n"})
        git(self.production, "add", "-A")
        git(self.production, "commit", "--quiet", "-m", "old release")
        git(self.production, "push", "--quiet", "origin", "main")
        self.old = git(self.production, "rev-parse", "HEAD")
        git(self.root, "clone", "--quiet", str(origin), str(self.author))
        self.configure(self.author)

    @staticmethod
    def configure(repo: Path) -> None:
        git(repo, "config", "user.name", "Test User")
        git(repo, "config", "user.email", "test@example.invalid")

    @staticmethod
    def write(repo: Path, files: dict[str, str]) -> None:
        for relative, text in files.items():
            path = repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def push(self, message: str, *, files: dict[str, str] | None = None, move: tuple[str, str] | None = None) -> str:
        """Commit onto the author clone and push it to `main`; the production checkout only fetches."""
        if move is not None:
            git(self.author, "mv", *move)
        self.write(self.author, files or {})
        git(self.author, "add", "-A")
        git(self.author, "commit", "--quiet", "-m", message)
        git(self.author, "push", "--quiet", "origin", "main")
        return git(self.author, "rev-parse", "HEAD")

    def renamed(self) -> str:
        """A target that moves the package directory, as the rename does."""
        return self.push("rename the package", move=(f"src/{PACKAGE}", "src/renamed_package"))

    def scriptless(self) -> str:
        """A target that keeps the directory but drops the console script."""
        return self.push("drop the console script", files={"pyproject.toml": '[project]\nname = "product"\n'})

    def fetched(self) -> None:
        git(self.production, "fetch", "--quiet", "origin", "main")

    def assert_untouched(self) -> None:
        self.assertEqual(git(self.production, "rev-parse", "HEAD"), self.old)  # type: ignore[attr-defined]
        self.assertEqual(git(self.production, "status", "--porcelain"), "")  # type: ignore[attr-defined]
        self.assertTrue((self.production / PREFLIGHT).is_file())  # type: ignore[attr-defined]
        self.assertEqual((self.production / "pyproject.toml").read_text(encoding="utf-8"), MANIFEST)  # type: ignore[attr-defined]


class TheCheckTests(EntrypointFixture, unittest.TestCase):
    def probe(self, args: list[str]) -> str | None:
        try:
            return run(["git", "-C", str(self.production), *args], "probe").stdout
        except HostError:
            return None

    def test_a_target_that_keeps_the_entrypoint_passes(self) -> None:
        target = self.push("an ordinary release", files={"README.md": "new\n"})
        self.fetched()
        self.assertIsNone(entrypoint_guard.entrypoint_refusal(self.probe, target))

    def test_the_package_is_the_running_codes_own_not_a_literal(self) -> None:
        target = self.renamed()
        self.fetched()
        self.assertIsNotNone(entrypoint_guard.entrypoint_refusal(self.probe, target))
        # After the rename the running package is the new one, and the same check admits the
        # renamed tree once its console script is named after it.
        renamed = self.push(
            "rename the console script",
            files={"pyproject.toml": '[project]\nname = "product"\n\n[project.scripts]\nrenamed_package = "x:main"\n'},
        )
        self.fetched()
        self.assertIsNone(entrypoint_guard.entrypoint_refusal(self.probe, renamed, package="renamed_package"))

    def test_a_missing_or_unreadable_manifest_is_refused(self) -> None:
        for manifest in ("[project\n", "[project]\nscripts = 1\n"):
            with self.subTest(manifest=manifest):
                target = self.push("break the manifest", files={"pyproject.toml": manifest})
                self.fetched()
                refusal = entrypoint_guard.entrypoint_refusal(self.probe, target)
                self.assertIsNotNone(refusal)
                assert refusal is not None
                self.assertEqual(refusal.reason, "entrypoint_moved")


class AdvanceTests(EntrypointFixture, unittest.TestCase):
    """`production_checkout.advance`: the guard sits after the ancestry check and before the schema."""

    def advance(self) -> tuple[mock.MagicMock, str | Exception]:
        self.fetched()
        with mock.patch.object(production_checkout.release_migrations, "prepare") as prepare:
            try:
                result: str | Exception = production_checkout.advance(
                    run, self.production, "origin/main", instance_dir=self.root / "instance"
                )
            except ProductionActivationRefused as exc:
                result = exc
        return prepare, result

    def test_a_target_that_keeps_the_entrypoint_advances_as_before(self) -> None:
        target = self.push("an ordinary release", files={"README.md": "new\n"})
        prepare, result = self.advance()
        self.assertEqual(result, target)
        prepare.assert_called_once_with(self.production, target, self.root / "instance")
        self.assertEqual(git(self.production, "rev-parse", "HEAD"), target)

    def test_a_moved_package_is_refused_before_the_schema_and_the_checkout(self) -> None:
        target = self.renamed()
        prepare, refused = self.advance()
        assert isinstance(refused, ProductionActivationRefused)
        prepare.assert_not_called()
        self.assert_untouched()
        facts = refused.facts()
        self.assertEqual((facts["code"], facts["reason"]), ("entrypoint_moved", "entrypoint_moved"))
        self.assertEqual((facts["target"], facts["old"]), (target, self.old))
        self.assertEqual(facts["checkout"], str(self.production))
        self.assertIsNone(facts["revision"])
        self.assertIsNone(facts["remote_merge"])
        self.assertIn(PREFLIGHT, facts["message"])
        self.assertIn("docs/OPERATIONS.md §Entrypoint activation", facts["message"])

    def test_a_dropped_console_script_is_refused(self) -> None:
        self.scriptless()
        prepare, refused = self.advance()
        assert isinstance(refused, ProductionActivationRefused)
        prepare.assert_not_called()
        self.assert_untouched()
        self.assertEqual(refused.facts()["reason"], "entrypoint_moved")
        self.assertIn("[project.scripts]", refused.facts()["message"])

    def test_a_checkout_already_at_the_target_is_not_checked(self) -> None:
        with mock.patch.object(entrypoint_guard, "entrypoint_refusal") as check:
            prepare, result = self.advance()
        self.assertEqual(result, self.old)
        check.assert_not_called()
        prepare.assert_called_once()


class UpgradeFastForwardTests(EntrypointFixture, unittest.TestCase):
    """`upgrade.fast_forward` and its dry-run planning refuse through the same check."""

    def test_an_ordinary_target_fast_forwards(self) -> None:
        target = self.push("an ordinary release", files={"README.md": "new\n"})
        self.assertEqual(upgrade.fast_forward(self.production, "main"), (self.old, target))

    def test_a_moved_package_or_dropped_script_leaves_the_checkout_as_found(self) -> None:
        for make in (self.renamed, self.scriptless):
            with self.subTest(target=make.__name__):
                target = make()
                with self.assertRaises(upgrade.EntrypointRefused) as raised:
                    upgrade.fast_forward(self.production, "main")
                self.assertIsInstance(raised.exception, upgrade.GitError)
                self.assertEqual(raised.exception.refusal.reason, "entrypoint_moved")
                self.assertEqual(raised.exception.refusal.target, target)
                self.assertIn("entrypoint_moved", str(raised.exception))
                self.assertIn("docs/OPERATIONS.md §Entrypoint activation", str(raised.exception))
                self.assert_untouched()

    def test_the_pull_step_and_its_dry_run_report_the_refusal(self) -> None:
        self.renamed()
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run):
                context = SimpleNamespace(
                    pull_result=None, pull=True, product_root=self.production, base_branch="main", dry_run=dry_run,
                    changed_paths=(), code_changed=False, schemas_changed=False,
                )
                result = upgrade.step_pull(context)  # type: ignore[arg-type]
                self.assertEqual(result.status, "failed")
                self.assertIn("entrypoint_moved", result.detail)
                self.assertIn("docs/OPERATIONS.md §Entrypoint activation", result.detail)
                self.assertEqual(context.changed_paths, (), "a refused target plans no move")
                self.assert_untouched()

    def test_a_role_worktree_is_refused_the_same_way(self) -> None:
        worktree = self.root / "workspaces" / "role"
        git(self.production, "worktree", "add", "--quiet", "--detach", str(worktree), "HEAD")
        self.renamed()
        with self.assertRaises(upgrade.EntrypointRefused):
            upgrade.fast_forward(worktree, "main")
        self.assertEqual(git(worktree, "rev-parse", "HEAD"), self.old)
        self.assertTrue((worktree / PREFLIGHT).is_file())


if __name__ == "__main__":
    unittest.main()
