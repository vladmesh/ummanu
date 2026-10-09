"""Real pytest execution belongs to exact-SHA integration-dispatcher CI only."""

from __future__ import annotations

import os
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

from tests.support.local_check_fixture import LocalCheckFixture
from tests.support.runner_owned_fixture import PARAMETER_NODE


class PytestSelectorTests(LocalCheckFixture, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.adapter["broad_check"]["module"] = "pytest"
        self.adapter["broad_check"].update(
            args=["tests/test_local.py", "-m", "not ci_only"],
            local={"membership": "runner", "selector_args": []},
        )
        self.write_adapter()
        (self.root / "tests" / "test_local.py").write_text(
            "import pytest\nfrom pathlib import Path\n"
            "@pytest.mark.parametrize('value', [1, 2], ids=['one space;$(touch injected)', 'two'])\n"
            "def test_value(value):\n"
            f"    Path({str(self.log)!r}).open('a').write(str(value) + '\\n')\n"
            "    assert value > 0\n",
            encoding="utf-8",
        )
        (self.root / "tests" / "test_board.py").write_text(
            "import pytest\n@pytest.mark.ci_only\ndef test_value():\n    raise AssertionError('CI-only test executed')\n"
        )
        # Project plugins do not inherit arbitrary CI runner plugins from the host environment.
        self.enterContext(mock.patch.dict("os.environ", {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}))

    def test_real_full_module_parameter_node_and_ci_refusal_preserve_full_receipt(self) -> None:
        status, full, output = self.invoke()
        self.assertEqual(status, 0, output)
        self.assertIn("2 passed", output)
        receipt = Path(full["path"])
        before = receipt.read_bytes()
        self.assertFalse(self.forbidden.exists())
        self.assertTrue(full["receipt"]["project_provenance"]["inside_workspace"])
        status, reused, _ = self.invoke("broad", "--reuse", "--module", "pytest")
        self.assertEqual(status, 0)
        self.assertTrue(reused["reused"])
        for selector in (
            "tests/test_local.py",
            "tests/test_local.py::test_value[one space;$(touch injected)]",
        ):
            executed_before = len(self.log.read_text().splitlines())
            status, subset, output = self.invoke(selector, "--reuse")
            self.assertEqual(status, 0, output)
            self.assertIn("passed", output)
            self.assertEqual(subset["argv"][7:], ["tests/test_local.py", "-m", "not ci_only", selector])
            # Supported pytest versions differ in duplicate path collection. Every selector
            # must execute its requested test within the declared paths, including on reuse.
            executed = self.log.read_text().splitlines()[executed_before:]
            self.assertIn("1", executed)
            self.assertLessEqual(set(executed), {"1", "2"})
            if selector == "tests/test_local.py":
                self.assertEqual(set(executed), {"1", "2"})
            self.assertNotIn("receipt", subset)
            self.assertEqual(receipt.read_bytes(), before)
        self.assertFalse((self.root / "injected").exists())
        self.assertEqual(self.invoke("show")[0], 0)
        for selector in ("tests/test_board.py", "tests/test_board.py::test_value"):
            status, error, output = self.invoke(selector)
            self.assertEqual(status, 2)
            self.assertIn("outside declared pytest paths", error["error"]["message"])
            self.assertFalse(self.forbidden.exists())
        self.assertEqual(list(receipt.parent.glob("broad-*.json")), [receipt])

    def test_real_pytest_missing_node_and_failing_assertion_return_its_own_status(self) -> None:
        # Isolate native node errors from duplicate collection: pytest 9 can collect a
        # declared file successfully even when its appended node does not exist.
        # The separate full/module/node test proves declared paths remain in argv.
        self.adapter["broad_check"]["args"] = ["-m", "not ci_only"]
        self.write_adapter()
        status, subset, output = self.invoke("tests/test_local.py::missing_node")
        self.assertEqual(status, 4, output)
        self.assertEqual(subset["exit_code"], 4)
        self.assertIn("missing_node", output)
        self.assertEqual(subset["argv"][7:], ["-m", "not ci_only", "tests/test_local.py::missing_node"])
        self.assertFalse(self.log.exists())
        self.assertNotIn("receipt", subset)
        (self.root / "tests" / "test_local.py").write_text("def test_red():\n    assert False\n")
        status, subset, output = self.invoke("tests/test_local.py::test_red")
        self.assertEqual(status, 1, output)
        self.assertEqual(subset["exit_code"], 1)
        self.assertIn("1 failed", output)
        self.assertNotIn("receipt", subset)
        self.assertFalse((self.root / "state" / "checks").exists())

    def test_declared_pytest_marker_deselection_is_preserved_on_subset(self) -> None:
        self.adapter["broad_check"]["args"] = ["tests", "-m", "not ci_only"]
        self.write_adapter()
        (self.root / "tests" / "test_local.py").write_text(
            "import pytest\n@pytest.mark.ci_only\ndef test_value():\n    raise AssertionError('CI-only test executed')\n"
        )
        status, subset, output = self.invoke("tests/test_board.py::test_value")
        self.assertEqual(status, 5, output)
        self.assertIn("deselected", output)
        self.assertNotIn("CI-only test executed", output)
        self.assertNotIn("receipt", subset)


class SharedRunnerSelectorTests(LocalCheckFixture, unittest.TestCase):
    """A real temporary runner process; no live codegen compatibility claim."""

    def setUp(self) -> None:
        super().setUp()
        (self.root / "shared").mkdir()
        (self.root / "shared" / "__init__.py").write_text("")
        shutil.copy(
            Path(__file__).parent / "support" / "runner_owned_fixture.py",
            self.root / "shared" / "__main__.py",
        )
        (self.root / ".venv" / "bin").mkdir(parents=True)
        (self.root / ".venv" / "bin" / "python").symlink_to(sys.executable)
        self.adapter["broad_check"].update(
            module="shared",
            import_package="shared",
            interpreter=".venv/bin/python",
            args=["--fixture-env", "host"],
            local={"membership": "runner", "selector_args": ["--"]},
        )
        self.write_adapter()
        self.enterContext(mock.patch.dict(os.environ, {"FIXTURE_EXECUTION_LOG": str(self.log)}))

    def test_real_runner_full_module_node_ci_only_and_missing_support_preserve_receipt(self) -> None:
        status, full, output = self.invoke()
        self.assertEqual(status, 0, output)
        self.assertIn("ci_only deselected=4", output)
        self.assertEqual(full["receipt"]["argv"][0], str(self.root / ".venv" / "bin" / "python"))
        self.assertEqual(full["receipt"]["project_provenance"]["imported_package"], "shared")
        receipt = Path(full["path"])
        before = receipt.read_bytes()
        for selector, count in (("checks/test_local.py", 2), (PARAMETER_NODE, 1)):
            status, subset, output = self.invoke(selector, "--reuse")
            self.assertEqual(status, 0, output)
            self.assertIn(f"Ran {count} tests", output)
            self.assertIn("fixture-env=host PYTHONPATH=''", output)
            self.assertEqual(subset["argv"][7:], ["--fixture-env", "host", "--", selector])
            self.assertNotIn("receipt", subset)
            self.assertEqual(receipt.read_bytes(), before)
        for marker in ("docker", "ansible", "privileged", "slow"):
            selector = f"checks/test_ci.py::test_{marker}"
            status, subset, output = self.invoke(selector)
            self.assertEqual(status, 23, output)
            self.assertIn(f"ci_only ({marker}); execution only in CI", output)
            self.assertEqual(receipt.read_bytes(), before)
        with mock.patch.dict(os.environ, {"FIXTURE_SELECTOR_UNAVAILABLE": "1"}):
            status, subset, output = self.invoke(PARAMETER_NODE)
        self.assertEqual(status, 2, output)
        self.assertIn("selector support unavailable", output)
        self.assertEqual(receipt.read_bytes(), before)
        self.assertEqual(self.invoke("show")[0], 0)
        self.assertFalse((self.root / "injected").exists())
        self.assertEqual(list(receipt.parent.glob("broad-*.json")), [receipt])
        self.assertEqual(len(self.log.read_text().splitlines()), 5)
