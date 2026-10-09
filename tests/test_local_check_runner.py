"""Runner-owned selectors preserve argv, profile environment, refusals and receipt boundaries."""

from __future__ import annotations

import json
import sys
import unittest
from io import BytesIO
from pathlib import Path
from unittest import mock

from tests.support.local_check_fixture import LocalCheckFixture
from tests.support.runner_owned_fixture import PARAMETER_NODE, host_profile
from ummanu import broad_check, check_commands


class RunnerOwnedSelectorTests(LocalCheckFixture, unittest.TestCase):
    def init_repository(self) -> None:
        # These unit fixtures inject both the runner and clock; they start no processes.
        pass

    def setUp(self) -> None:
        super().setUp()
        self.adapter["broad_check"].update(
            module="shared",
            args=["--fixture-env", "host"],
            local={"membership": "runner", "selector_args": ["--"]},
        )
        self.write_adapter()
        self.calls = []
        self.executed = []
        self.enterContext(mock.patch.object(check_commands, "_git_common_dir", return_value=None))
        self.enterContext(
            mock.patch.object(
                broad_check, "content_identity", return_value=broad_check.ContentIdentity("abc")
            )
        )
        self.enterContext(mock.patch.object(broad_check, "_assert_ignored"))
        selector = mock.MagicMock()
        selector.__enter__.return_value.select.return_value = [True]
        self.enterContext(mock.patch.object(broad_check.selectors, "DefaultSelector", return_value=selector))
        self.process = self.enterContext(
            mock.patch.object(broad_check.subprocess, "Popen", side_effect=self.launch)
        )

    def launch(self, argv, *, env, **kwargs):
        self.assertEqual(argv[4], "shared")
        self.calls.append(argv[7:])
        output = []
        status = host_profile(
            argv[7:], env, output.append, lambda node, environment: self.executed.append((node, environment))
        )
        Path(argv[3]).write_text(
            json.dumps(
                {
                    "python": argv[0],
                    "environment_prefix": sys.prefix,
                    "cwd": str(self.root),
                    "imported_package": "app",
                    "imported_project": str(self.root / "app" / "__init__.py"),
                    "import_roots": [str(self.root)],
                }
            )
        )
        return mock.Mock(stdout=BytesIO("\n".join(output).encode()), wait=mock.Mock(return_value=status))

    def test_full_module_node_and_runner_refusals_preserve_receipt(self) -> None:
        status, full, output = self.invoke()
        self.assertEqual(status, 0, output)
        self.assertIn("ci_only deselected=4", output)
        self.assertEqual(self.calls, [["--fixture-env", "host"]])
        receipt = Path(full["path"])
        before = receipt.read_bytes()
        status, reused, _ = self.invoke(
            "broad", "--reuse", "--module", "shared", "--module-arg=--fixture-env", "--module-arg=host"
        )
        self.assertEqual(status, 0)
        self.assertTrue(reused["reused"])
        self.assertEqual(len(self.calls), 1)
        for selector, count in (("checks/test_local.py", 2), (PARAMETER_NODE, 1)):
            status, subset, output = self.invoke(selector, "--reuse")
            self.assertEqual(status, 0, output)
            self.assertIn(f"Ran {count} tests", output)
            self.assertEqual(self.calls[-1], ["--fixture-env", "host", "--", selector])
            self.assertNotIn("receipt", subset)
            self.assertNotIn("reused", subset)
            self.assertEqual(receipt.read_bytes(), before)
        for marker in ("docker", "ansible", "privileged", "slow"):
            selector = f"checks/test_ci.py::test_{marker}"
            status, subset, output = self.invoke(selector)
            self.assertEqual(status, 23, output)
            self.assertIn(f"{selector}: ci_only ({marker}); execution only in CI", output)
            self.assertEqual(self.calls[-1][-1], selector)
            self.assertNotIn("receipt", subset)
            self.assertEqual(receipt.read_bytes(), before)
        self.assertEqual(list(receipt.parent.glob("broad-*.json")), [receipt])
        self.assertEqual(self.invoke("show")[0], 0)
        self.assertTrue(all(env == {"FIXTURE_ENV": "host", "PYTHONPATH": ""} for _, env in self.executed))
        self.assertTrue(all("test_ci" not in node for node, _ in self.executed))

    def test_legacy_subset_retains_declared_args_and_show_refuses(self) -> None:
        argv = (
            "--module",
            "shared",
            "--module-arg=--fixture-env",
            "--module-arg=host",
            "--module-arg=--",
            f"--module-arg={PARAMETER_NODE}",
        )
        status, subset, output = self.invoke("broad", "--reuse", *argv)
        self.assertEqual(status, 0, output)
        self.assertEqual(self.calls[-1], ["--fixture-env", "host", "--", PARAMETER_NODE])
        self.assertNotIn("receipt", subset)
        self.assertFalse(broad_check.receipt_dir(self.root).exists())
        self.assertEqual(self.invoke("show", *argv)[1]["error"]["code"], "subset_has_no_receipt")

    def test_missing_runner_selector_support_cannot_become_empty_success(self) -> None:
        with mock.patch.dict("os.environ", {"FIXTURE_SELECTOR_UNAVAILABLE": "1"}):
            status, subset, output = self.invoke(PARAMETER_NODE)
        self.assertEqual(status, 2, output)
        self.assertIn("selector support unavailable", output)
        self.assertEqual(subset["exit_code"], 2)
        self.assertEqual(self.executed, [])
        self.assertFalse(broad_check.receipt_dir(self.root).exists())

    def test_declared_candidate_interpreter_wins_over_default(self) -> None:
        interpreter = self.root / ".venv" / "bin" / "python"
        interpreter.parent.mkdir(parents=True)
        interpreter.symlink_to(sys.executable)
        self.adapter["broad_check"]["interpreter"] = ".venv/bin/python"
        self.write_adapter()
        status, subset, output = self.invoke(PARAMETER_NODE)
        self.assertEqual(status, 0, output)
        self.assertEqual(subset["argv"][0], str(interpreter))
        self.assertEqual(subset["project_provenance"]["python"], str(interpreter))

    def test_malformed_runner_declaration_refuses_without_launch(self) -> None:
        for declaration in (
            {"membership": "runner"},
            {"membership": "runner", "selector_args": ["-k"]},
            {"membership": "runner", "selector_args": [], "ci_manifest": "tests/ci-shards.txt"},
            {"runner": "pytest", "shards": ["unit"], "modules": {"checks/test_local.py": "unit"}},
        ):
            self.adapter["broad_check"]["local"] = declaration
            self.write_adapter()
            self.assertEqual(self.invoke()[0], 2)
            self.process.assert_not_called()

    def test_pytest_keeps_paths_and_markers_and_rejects_expansion(self) -> None:
        declared = [
            "tests",
            "-m",
            "not slow",
            "-c",
            "project.ini",
            "--rootdir",
            ".",
            "-p",
            "no:cacheprovider",
        ]
        self.adapter["broad_check"].update(
            module="pytest", args=declared, local={"membership": "runner", "selector_args": []}
        )
        self.write_adapter()
        observation = {
            "exit_code": 5,
            "incomplete_reason": "",
            "signal": 0,
            "status": "complete",
            "verdict": "failed",
            "project_provenance": {
                "origin": "check-process",
                "imported_package": "app",
                "imported_project": str(self.root / "app" / "__init__.py"),
                "environment_prefix": sys.prefix,
            },
        }
        selector = "tests/test_local.py::test_one[param with spaces;$(touch injected)]"
        with mock.patch.object(check_commands, "run_broad_check", return_value=(5, observation)) as run:
            status, subset, _ = self.invoke(selector)
            self.assertEqual(status, 5)
            self.assertEqual(run.call_args.args[0].module_args, (*declared, selector))
            self.assertFalse(run.call_args.kwargs["record_receipt"])
            self.assertNotIn("receipt", subset)
        with mock.patch.object(check_commands, "run_broad_check") as run:
            self.assertEqual(
                self.invoke("outside/test_other.py")[1]["error"]["code"], "outside_local_profile"
            )
            run.assert_not_called()
