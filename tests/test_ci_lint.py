"""Candidate lint never expands an empty diff into a repository-wide check."""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.ci_lint import changed_python
from tests.support.git import git


class CandidateLintTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        git(self.root, "init", "-b", "main")
        git(self.root, "config", "user.name", "Test")
        git(self.root, "config", "user.email", "test@example.invalid")
        (self.root / "scripts").mkdir()
        shutil.copyfile(
            Path(__file__).resolve().parents[1] / "scripts/ci_lint.py", self.root / "scripts/ci_lint.py"
        )
        (self.root / "unchanged.py").write_text("unrelated_debt\n")
        (self.root / "removed.py").write_text("pass\n")
        (self.root / "renamed.py").write_text("pass\n")
        self.base = self.commit()

    def commit(self):
        git(self.root, "add", ".")
        git(self.root, "commit", "-m", "fixture")
        return git(self.root, "rev-parse", "HEAD")

    def run_lint(self, head, *, base=None):
        return subprocess.run(
            [
                sys.executable,
                str(self.root / "scripts/ci_lint.py"),
                "--base-sha",
                self.base if base is None else base,
                "--candidate-sha",
                head,
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_deleted_files_and_unchanged_debt_are_excluded_and_renames_are_included(self):
        (self.root / "removed.py").unlink()
        (self.root / "renamed.py").rename(self.root / "renamed with space.py")
        (self.root / "new file.py").write_text("pass\n")
        (self.root / "README.md").write_text("documentation\n")
        head = self.commit()
        self.assertEqual(changed_python(self.root, self.base, head), ["new file.py", "renamed with space.py"])

    def test_no_python_change_succeeds_without_starting_ruff(self):
        (self.root / "README.md").write_text("documentation\n")
        result = self.run_lint(self.commit())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No changed Python files", result.stdout)

    def test_missing_base_and_mismatched_checkout_fail_closed(self):
        with self.assertRaises(subprocess.CalledProcessError):
            changed_python(self.root, "a" * 40, self.base)
        (self.root / "README.md").write_text("new head\n")
        self.commit()
        with self.assertRaisesRegex(ValueError, "candidate SHA"):
            changed_python(self.root, self.base, self.base)

    def test_ruff_failure_is_preserved_for_an_added_file(self):
        (self.root / "bad.py").write_text("missing_name\n")
        result = self.run_lint(self.commit())
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("F821", result.stdout)
        self.assertNotIn("unchanged.py", result.stdout)

    def test_manual_run_and_zero_push_base_compare_to_candidate_parent(self):
        (self.root / "new.py").write_text("pass\n")
        head = self.commit()
        for base in ("", "0" * 40):
            with self.subTest(base=base):
                result = self.run_lint(head, base=base)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Ruff checks 1 changed Python files", result.stdout)
