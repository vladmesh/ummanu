"""Real pytest execution belongs to exact-SHA integration-dispatcher CI only."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from tests.support.local_check_fixture import LocalCheckFixture
from ummanu import check_commands


class PytestSelectorTests(LocalCheckFixture, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.adapter["broad_check"]["module"] = "pytest"
        self.adapter["broad_check"]["local"]["runner"] = "pytest"
        self.write_adapter()
        (self.root / "checks" / "test_local.py").write_text(
            "import pytest\nfrom pathlib import Path\n"
            "@pytest.mark.parametrize('value', [1, 2], ids=['one space;$(touch injected)', 'two'])\n"
            "def test_value(value):\n"
            f"    Path({str(self.log)!r}).open('a').write(str(value) + '\\n')\n"
            "    assert value > 0\n",
            encoding="utf-8",
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
        for selector, count in (
            ("checks/test_local.py", 2),
            ("checks/test_local.py::test_value[one space;$(touch injected)]", 1),
        ):
            status, subset, output = self.invoke(selector, "--reuse")
            self.assertEqual(status, 0, output)
            self.assertIn(f"{count} passed", output)
            self.assertNotIn("receipt", subset)
            self.assertEqual(receipt.read_bytes(), before)
        self.assertEqual(self.log.read_text().splitlines(), ["1", "2", "1", "2", "1"])
        self.assertFalse((self.root / "injected").exists())
        self.assertEqual(self.invoke("show")[0], 0)
        for selector in ("checks/test_board.py", "checks/test_board.py::test_value"):
            with mock.patch.object(check_commands, "run_broad_check") as run:
                status, error, _ = self.invoke(selector)
                self.assertEqual(status, 2)
                self.assertIn("integration-board", error["error"]["message"])
                run.assert_not_called()
                self.assertFalse(self.forbidden.exists())
        self.assertEqual(list(receipt.parent.glob("broad-*.json")), [receipt])

    def test_real_pytest_missing_node_and_failing_assertion_return_its_own_status(self) -> None:
        status, subset, output = self.invoke("checks/test_local.py::missing_node")
        self.assertEqual(status, 4, output)
        self.assertEqual(subset["exit_code"], 4)
        (self.root / "checks" / "test_local.py").write_text("def test_red():\n    assert False\n")
        status, subset, output = self.invoke("checks/test_local.py::test_red")
        self.assertEqual(status, 1, output)
        self.assertEqual(subset["exit_code"], 1)
