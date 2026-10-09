"""Real interpreter/role/native-runner proof, executed only by dispatcher CI."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import sysconfig
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests.support.local_check_fixture import LocalCheckFixture
from tests.support.managed_venv import guarded_product_env
from ummanu.dispatch.host import CommandHostRuntime
from ummanu.runtime import role_env, test_guard


class GuardInterpreterTests(LocalCheckFixture, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        import pytest

        self.assertEqual(pytest.__version__.split(".")[0], "9")
        self.adapter["broad_check"]["interpreter"] = ".venv/bin/python"
        self.write_adapter()
        with (self.root / ".gitignore").open("a") as handle:
            handle.write(".venv/\n")
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(self.root / ".venv")], check=True)
        self.host = CommandHostRuntime(
            SimpleNamespace(adapter=lambda project: self.adapter), self.scratch / "host-data",
            mode="real", production_runtime=SimpleNamespace(interpreter=sys.executable),
        )
        self.host._prepare_workspace_environment(str(self.root), project="fixture")
        self.default = self.root / role_env.WORKSPACE_ENV_DIR / "bin/python3"
        self.declared = self.root / ".venv/bin/python"
        # CI's pytest is installed in the test process's prefix. Expose those library
        # files to bounded venv fixtures; no installer, downloads or live venv writes.
        for environment in (self.default.parent.parent, self.declared.parent.parent):
            site = next(environment.glob("lib/python3*/site-packages"))
            (site / "fixture-libraries.pth").write_text(sysconfig.get_path("purelib") + "\n")
            console = environment / "bin/pytest"
            console.write_text(f"#!{environment / 'bin/python3'}\nimport pytest\nraise SystemExit(pytest.console_main())\n")
            console.chmod(0o755)
        self.product_env = guarded_product_env(self.scratch)
        self.env = {**os.environ, **self.product_env, "UMMANU_INSTANCE": str(self.instance),
                    "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
        self.imported = self.scratch / "test-imported"
        local = self.root / "tests/test_local.py"
        local.write_text(f"from pathlib import Path\nPath({str(self.imported)!r}).touch()\n" + local.read_text())

    def launch(self, role: str, argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-P", "-m", "ummanu.runtime.role_env", "exec", "--role", role,
             "--workspace", str(self.root), "--env-file", str(self.scratch / "absent.env"), "--", *argv],
            cwd=self.root, env=self.env, capture_output=True, text=True, timeout=30, check=False,
        )

    def refused(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 125, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
        self.assertIn("ummanu check", result.stderr)
        self.assertFalse(self.imported.exists())
        self.assertFalse(self.log.exists())
        self.assertFalse((self.scratch / "docker-calls").exists())
        print(f"negative argv={result.args!r} rc=125 test_imported=false: {result.stderr.strip()}")

    def test_both_roles_aliases_absolute_interpreters_and_console_refuse_before_import(self) -> None:
        for role in ("worker", "reviewer"):
            for python in ("python", "python3", str(self.default), str(self.declared)):
                for runner, selector in (("unittest", "tests.test_local.Cases.test_one"),
                                         ("pytest", "tests/test_local.py::Cases::test_one")):
                    with self.subTest(role=role, python=python, runner=runner):
                        result = self.launch(role, [python, "-m", runner, selector])
                        self.refused(result)
                        self.assertIn(f"ummanu check {selector}", result.stderr)
            for console in ("pytest", str(self.declared.parent / "pytest")):
                self.refused(self.launch(role, [console, "tests/test_local.py::Cases::test_one"]))
            result = self.launch(role, ["python", "-c", "import unittest, pytest; print('libraries-ok')"])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "libraries-ok\n")
            result = self.launch(role, ["/bin/sh", "-lc", role_env.role_shell_command(
                role, "python3 -m unittest tests.test_local.Cases.test_one", workspace=self.root)])
            self.refused(result)

    def test_retained_materialization_repairs_guard_files_without_rebuilding_venvs(self) -> None:
        inodes = [python.stat().st_ino for python in (self.default, self.declared)]
        for python in (self.default, self.declared):
            site = next(python.parent.parent.glob("lib/python3*/site-packages"))
            (site / test_guard.STARTUP_FILE).unlink()
            (site / test_guard.MODULE_FILE).unlink()
        self.host._prepare_workspace_environment(str(self.root), project="fixture")
        self.assertEqual([python.stat().st_ino for python in (self.default, self.declared)], inodes)
        self.refused(self.launch("worker", [str(self.default), "-m", "unittest", "tests.test_local"]))
        self.refused(self.launch("reviewer", [str(self.declared), "-m", "pytest", "tests/test_local.py"]))

    def test_installed_cli_wrapper_full_reuse_node_and_following_direct_refusal(self) -> None:
        # Exercise the installed CLI as a separate role command, not an ambient marker
        # and not direct unittest. The same bootstrap is used by wrapper169.
        wrapper = [sys.executable, "-P", "-m", "ummanu", "check", "--root", str(self.root),
                   "--instance", str(self.instance)]
        # role_env strips PYTHONPATH; the product CLI interpreter's installed package
        # supplies Ummanu while the wrapper chooses this fixture's declared interpreter.
        result = self.launch("worker", wrapper)
        self.assertEqual(result.returncode, 0, result.stderr)
        import json
        full = json.loads(result.stdout)
        self.assertEqual(full["receipt"]["parsed"]["tests"], 2)
        self.assertEqual(full["receipt"]["project_provenance"]["python"], str(self.declared))
        self.assertTrue(full["receipt"]["project_provenance"]["inside_workspace"])
        receipt = Path(full["path"])
        before = receipt.read_bytes()
        result = self.launch("reviewer", wrapper)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["reused"])
        result = self.launch("reviewer", [*wrapper, "tests/test_local.py::Cases::test_one", "--reuse"])
        self.assertEqual(result.returncode, 0, result.stderr)
        subset = json.loads(result.stdout)
        self.assertIn("Ran 1 test", result.stderr)
        self.assertNotIn("receipt", subset)
        self.assertEqual(self.log.read_text().splitlines(), ["one", "two", "one"])
        self.assertEqual(receipt.read_bytes(), before)
        self.imported.unlink()
        self.log.unlink()
        self.refused(self.launch("reviewer", [str(self.declared), "-m", "unittest", "tests.test_local"]))
        self.assertEqual(receipt.read_bytes(), before)

    def test_native_shared_whitelist_absolute_child_pytest_and_ci_families(self) -> None:
        shared = self.root / "shared"
        shared.mkdir()
        (shared / "__init__.py").touch()
        local = self.root / "checks/test_local.py"
        local.parent.mkdir()
        local.write_text(
            "import os\nfrom pathlib import Path\n"
            "def test_one():\n"
            "    assert os.environ['FIXTURE_ENV'] == 'host'\n"
            "    assert os.environ['PYTHONPATH'] == ''\n"
            "    assert 'UMMANU_WRAPPER_MARKER' not in os.environ\n"
            f"    Path({str(self.log)!r}).open('a').write('one\\n')\n"
            "def test_two():\n    pass\n"
        )
        # Model the native runner's whitelist, absolute interpreter, fixture env and
        # budgets. This is compatibility evidence, never a live codegen head proof.
        shutil.copy(Path(__file__).parent / "support/native_shared_guard_fixture.py", shared / "__main__.py")
        # The fixture preserves native timeout/budget options; the lightweight
        # budget plugin checks setup+call+teardown without a deliberate slow test.
        (self.root / "conftest.py").write_text(
            "import time, pytest\n"
            "def pytest_addoption(parser):\n"
            "    parser.addoption('--timeout', type=float)\n"
            "    parser.addoption('--timeout-method')\n"
            "    parser.addoption('--unit-test-budget', type=float)\n"
            "@pytest.hookimpl(wrapper=True)\n"
            "def pytest_runtest_protocol(item):\n"
            "    assert item.config.getoption('--timeout') == 90\n"
            "    assert item.config.getoption('--timeout-method') == 'thread'\n"
            "    started = time.perf_counter()\n"
            "    yield\n"
            "    assert time.perf_counter() - started <= item.config.getoption('--unit-test-budget')\n"
        )
        self.adapter["broad_check"].update(module="shared", import_package="shared",
                                          local={"membership": "runner", "selector_args": ["--"]})
        self.write_adapter()
        status, full, output = self.invoke()
        self.assertEqual(status, 0, output)
        self.assertIn("2 passed", output)
        receipt = Path(full["path"])
        before = receipt.read_bytes()
        status, subset, output = self.invoke("checks/test_local.py::test_one", "--reuse")
        self.assertEqual(status, 0, output)
        self.assertIn("1 passed", output)
        self.assertEqual(subset["argv"][0], str(self.declared))
        self.assertEqual(subset["argv"][7:], ["--", "checks/test_local.py::test_one"])
        self.assertNotIn("receipt", subset)
        for family in ("docker", "ansible", "privileged", "slow"):
            status, subset, output = self.invoke(f"checks/test_ci.py::test_{family}")
            self.assertEqual(status, 23, output)
            self.assertIn(f"ci_only ({family})", output)
            self.assertEqual(receipt.read_bytes(), before)
        result = subprocess.run(
            ["/usr/bin/env", "-i", "PATH=/absent", "HOME=" + str(self.scratch), "FIXTURE_ENV=host",
             "PYTHONPATH=", str(self.declared), "-m", "pytest", "checks/test_local.py::test_one"],
            cwd=self.root, capture_output=True, text=True, timeout=30, check=False,
        )
        self.assertEqual(result.returncode, 125, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertEqual(self.log.read_text().splitlines(), ["one", "one"])
        self.assertEqual(receipt.read_bytes(), before)
