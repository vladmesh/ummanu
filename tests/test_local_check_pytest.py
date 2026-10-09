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
        import pytest

        self.assertEqual(pytest.__version__.split(".")[0], "9", "exact-SHA proof requires pytest 9.x")
        self.adapter["broad_check"].update(
            module="pytest", local={"membership": "runner", "selector_args": []}
        )
        self.enterContext(mock.patch.dict("os.environ", {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}))

    def fixture(self, directory: str) -> str:
        path = self.root / directory / ("test_selected_" + directory.replace("/", "_").replace("-", "_") + ".py")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "import pytest\nfrom pathlib import Path\n"
            "@pytest.mark.parametrize('value', [1, 2], ids=['one space;$(touch injected)', 'two'])\n"
            "def test_value(value):\n"
            f"    Path({str(self.log)!r}).open('a').write(str(value) + '\\n')\n"
            "    assert value > 0\n"
            "@pytest.mark.ci_only\n"
            "@pytest.mark.integration\n"
            "@pytest.mark.slow\n"
            "def test_ci_only():\n    raise AssertionError('CI-only test executed')\n",
            encoding="utf-8",
        )
        return path.relative_to(self.root).as_posix()

    def test_all_adapter_shapes_file_and_directory_roots_execute_exact_parameter(self) -> None:
        shapes = (
            ("codegen-product-kit", ("tests/unit", "tests/tooling", "tests/copier"),
             ("-m", "not slow")),
            ("codegen-platform-services", ("libs/platform-core/tests",), ("-q",)),
            ("personal-site", ("services/backend/tests",),
             ("-c", "services/backend/pyproject.toml", "--rootdir", "services/backend",
              "-m", "not integration", "-p", "no:cacheprovider")),
            ("dnd-simulator", ("tests/unit",), ("-q",)),
        )
        for name, roots, options in shapes:
            for kind in ("file", "directory"):
                with self.subTest(adapter=name, roots=kind):
                    files = [self.fixture(directory) for directory in roots]
                    if name == "personal-site":
                        (self.root / "services/backend/pyproject.toml").write_text(
                            '[tool.pytest.ini_options]\nmarkers = ["ci_only", "integration", "slow"]\n'
                        )
                    declared_roots = files if kind == "file" else list(roots)
                    # Keep the installed shape's original options and test ci_only with its own
                    # marker expression. Duplicate -m has native pytest last-value semantics.
                    before_options = options[:4] if name == "personal-site" else ()
                    after_options = options[4:] if name == "personal-site" else options
                    declared = [*before_options, *declared_roots, *after_options, "-m", "not ci_only"]
                    self.adapter["broad_check"]["args"] = declared
                    self.write_adapter()
                    self.log.write_text("")
                    status, full, output = self.invoke()
                    self.assertEqual(status, 0, output)
                    self.assertIn(f"{2 * len(roots)} passed", output)
                    self.assertEqual(self.log.read_text().splitlines(), ["1", "2"] * len(roots))
                    self.assertEqual(full["receipt"]["argv"][7:], declared)
                    receipt = Path(full["path"])
                    before = receipt.read_bytes()
                    receipt_files = set(receipt.parent.glob("broad-*.json"))
                    self.assertTrue(full["receipt"]["project_provenance"]["inside_workspace"])
                    status, reused, output = self.invoke(
                        "broad", "--reuse", "--module", "pytest",
                        *[f"--module-arg={arg}" for arg in declared],
                    )
                    self.assertEqual(status, 0, output)
                    self.assertTrue(reused["reused"])
                    node = files[0] + "::test_value[one space;$(touch injected)]"
                    for selectors, expected in (
                        ((files[0],), ["1", "2"]), ((node,), ["1"]),
                        ((node, files[0] + "::test_value[two]"), ["1", "2"]),
                    ):
                        executed_before = len(self.log.read_text().splitlines())
                        status, subset, output = self.invoke(*selectors, "--reuse")
                        self.assertEqual(status, 0, output)
                        self.assertEqual(self.log.read_text().splitlines()[executed_before:], expected)
                        self.assertEqual(subset["argv"][7:], [*before_options, *selectors, *after_options, "-m", "not ci_only"])
                        self.assertNotIn("receipt", subset)
                        self.assertEqual(receipt.read_bytes(), before)
                    # The legacy append input reaches the same production resolver.
                    executed_before = len(self.log.read_text().splitlines())
                    status, subset, output = self.invoke(
                        "broad", "--reuse", "--module", "pytest",
                        *[f"--module-arg={arg}" for arg in declared], f"--module-arg={node}",
                    )
                    self.assertEqual(status, 0, output)
                    self.assertEqual(self.log.read_text().splitlines()[executed_before:], ["1"])
                    executed_before_errors = self.log.read_text()
                    for selector in (files[0] + "::missing_node",):
                        status, subset, output = self.invoke(selector)
                        self.assertEqual(status, 4, output)
                        self.assertEqual(subset["exit_code"], 4)
                        self.assertIn("not found", output)
                        self.assertIn("missing_node", output)
                        self.assertNotIn("receipt", subset)
                        self.assertEqual(self.log.read_text(), executed_before_errors)
                    if kind == "directory":
                        status, subset, output = self.invoke(roots[0] + "/missing_file.py")
                        self.assertEqual(status, 4, output)
                        self.assertIn("file or directory not found", output)
                        self.assertEqual(self.log.read_text(), executed_before_errors)
                    executed_before = self.log.read_text()
                    status, error, output = self.invoke(files[0] + "::test_ci_only")
                    self.assertEqual(status, 2, output)
                    self.assertIn("deselected", output)
                    self.assertIn("not ci_only", error["error"]["message"])
                    self.assertIn("CI", error["error"]["message"])
                    self.assertEqual(self.log.read_text(), executed_before)
                    # This file exists and would fail on import. Refusal must precede execution.
                    status, error, output = self.invoke("tests/test_board.py")
                    self.assertEqual(status, 2, output)
                    self.assertIn("declared pytest roots", error["error"]["message"])
                    self.assertFalse(self.forbidden.exists())
                    self.assertEqual(receipt.read_bytes(), before)
                    self.assertEqual(self.invoke("show")[0], 0)
                    self.assertEqual(set(receipt.parent.glob("broad-*.json")), receipt_files)
                    (self.root / files[0]).write_text("def test_red():\n    assert False\n")
                    status, subset, output = self.invoke(files[0] + "::test_red")
                    self.assertEqual(status, 1, output)
                    self.assertIn("1 failed", output)
                    self.assertNotIn("receipt", subset)
                    self.assertEqual(receipt.read_bytes(), before)
                    self.assertEqual(set(receipt.parent.glob("broad-*.json")), receipt_files)
        self.assertFalse((self.root / "injected").exists())


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
