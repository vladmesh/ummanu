"""The style check judges only the lines a candidate adds."""

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.check_style import Finding, Report, check, publish
from tests.support.git import git

CORE = """def old():
    # carried over from ummanu-12 on 2026-01-01
    return 1


def _double(value):
    doubled = value * 2
    return doubled
"""

OLD_TEST = """def test_old():
    value = 1
    assert value
"""

READS = """import inspect
from pathlib import Path

from tests import source_trees

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def test_source_of_a_function():
    assert "return" in inspect.getsource(len)


def test_a_document():
    assert (ROOT / "docs" / "guide.md").read_text()


def test_the_package_tree():
    assert list(SRC.rglob("*.py"))


def test_a_parsed_tree():
    assert source_trees.parse("x = 1")


# architecture-guard: the kernel imports nothing from the dispatcher
def test_a_marked_guard():
    assert list(SRC.rglob("*.py"))


def test_a_temporary_document(tmp_path):
    (tmp_path / "notes.md").write_text("x")
    assert (tmp_path / "notes.md").read_text() == "x"
"""


class StyleCheckTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        git(self.root, "init", "-b", "main")
        git(self.root, "config", "user.name", "Test")
        git(self.root, "config", "user.email", "test@example.invalid")
        self.write({"src/pkg/__init__.py": "", "src/pkg/core.py": CORE, "tests/test_old.py": OLD_TEST})
        self.base = self.commit()

    def write(self, files):
        for name, text in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    def commit(self):
        git(self.root, "add", "-A")
        git(self.root, "commit", "-m", "fixture")
        return git(self.root, "rev-parse", "HEAD")

    def check(self):
        return check(self.root, self.base, self.commit())

    def test_card_ids_dates_and_em_dashes_in_added_prose_are_red(self):
        self.write(
            {
                "src/pkg/core.py": CORE
                + "\n\ndef new():\n"
                + '    """Return two — always."""\n'
                + '    note = "ummanu-292 in a string is data"\n'
                + "    # see sprint:1490 and PR #741\n"
                + "    return 2  # added 2026-10-14\n",
                "tests/test_new.py": "def test_ummanu_292_regression():\n    assert True\n",
                "notes — draft.txt": "x\n",
            }
        )
        report = self.check()
        self.assertEqual(
            [(finding.path, finding.line, finding.message.split(":")[0]) for finding in report.errors],
            [
                ("notes — draft.txt", 1, "em dash in a file name"),
                ("src/pkg/core.py", 12, "em dash in prose"),
                ("src/pkg/core.py", 14, "S3 card, sprint, issue or PR id or date in prose"),
                ("src/pkg/core.py", 15, "S3 card, sprint, issue or PR id or date in prose"),
                (
                    "tests/test_new.py",
                    1,
                    "S3 card, sprint, issue id or date in the name test_ummanu_292_regression",
                ),
            ],
        )

    def test_moved_prose_and_a_base_that_moved_on_are_not_added_lines(self):
        git(self.root, "checkout", "-b", "candidate")
        self.write(
            {"src/pkg/core.py": CORE.split("\n\n\n")[1], "src/pkg/moved.py": CORE.split("\n\n\n")[0] + "\n"}
        )
        head = self.commit()
        git(self.root, "checkout", "main")
        self.write({"src/pkg/core.py": CORE.replace("    # carried over from ummanu-12 on 2026-01-01\n", "")})
        main = self.commit()
        git(self.root, "checkout", "candidate")
        self.assertEqual(check(self.root, main, head), Report())

    def test_a_new_test_reading_source_or_docs_is_red_unless_marked_or_the_source_index(self):
        self.write(
            {
                "tests/test_reads.py": READS,
                "tests/source_trees.py": "from pathlib import Path\n\nTEXT = Path(__file__).read_text()\n",
                "tests/test_old.py": OLD_TEST.replace("value = 1", "value = inspect.getsource(len)"),
            }
        )
        self.assertEqual(
            [(finding.path, finding.line, finding.message) for finding in self.check().errors],
            [
                ("tests/test_reads.py", 11, "S1 test reads product source as text"),
                ("tests/test_reads.py", 15, "S1 test reads docs, skills, prompt or Markdown text"),
                ("tests/test_reads.py", 19, "S1 test reads product source as text"),
                ("tests/test_reads.py", 23, "S1 test reads product source as text"),
            ],
        )

    def test_a_reexport_is_red_only_when_nothing_imports_it(self):
        self.write(
            {
                "src/pkg/api.py": (
                    "from pkg.core import old as old\n"
                    "from pkg.core import old as kept\n"
                    "from pkg.core import new as new\n"
                    "from pkg.core import used as used\n"
                    "from pkg.core import relative as relative\n"
                ),
                "tests/test_api.py": "from pkg.api import new\n",
                "src/pkg/use.py": "from pkg import api\n\nVALUE = api.used\n",
                "src/pkg/rel.py": "from .api import relative\n",
            }
        )
        self.assertEqual(
            self.check().errors,
            [Finding("src/pkg/api.py", 1, "S2 re-export old as old that nothing imports")],
        )

    def test_prose_share_copied_helpers_and_absence_shapes_are_warnings(self):
        code = "".join(f"VALUE_{number} = {number}\n" for number in range(20))
        prose = "".join(f"# Why value {number} exists.\n" for number in range(6))
        self.write(
            {
                "src/pkg/values.py": prose + code,
                "src/pkg/other.py": "def _twice(value):\n    doubled = value * 2\n    return doubled\n",
                "tests/test_gone.py": (
                    "import pkg\n\n\ndef test_old_name_is_gone():\n    assert not hasattr(pkg, 'old')\n"
                ),
            }
        )
        report = self.check()
        self.assertEqual(report.errors, [])
        self.assertEqual(
            report.warnings,
            [
                Finding("tests/test_gone.py", 4, "S1 test_old_name_is_gone looks like a test of absence"),
                Finding("src/pkg/other.py", 1, "S4 private helper _twice duplicates src/pkg/core.py:6"),
                Finding("", 0, "S5 prose share of added src lines 0.26 > 0.25 (6 prose, 23 code)"),
            ],
        )

    def test_github_actions_gets_annotations_and_a_step_summary(self):
        summary = self.root / "summary.md"
        report = Report(
            [Finding("src/pkg/core.py", 3, "em dash in prose")],
            [Finding("", 0, "S5 prose share of added src lines 0.30 > 0.25 (9 prose, 30 code)")],
        )
        output = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true", "GITHUB_STEP_SUMMARY": str(summary)}),
            contextlib.redirect_stdout(output),
        ):
            publish(report)
        self.assertEqual(
            output.getvalue().splitlines(),
            [
                "::error file=src/pkg/core.py,line=3,title=style::em dash in prose",
                "::warning title=style::S5 prose share of added src lines 0.30 > 0.25 (9 prose, 30 code)",
                "Style: 1 errors, 1 warnings.",
            ],
        )
        self.assertEqual(
            summary.read_text().splitlines(),
            [
                "### Style of added lines",
                "",
                "- error: `src/pkg/core.py:3: em dash in prose`",
                "- warning: `S5 prose share of added src lines 0.30 > 0.25 (9 prose, 30 code)`",
            ],
        )
