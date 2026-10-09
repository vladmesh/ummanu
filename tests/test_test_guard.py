"""Fast startup-policy regressions; interpreter execution is tested in CI."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ummanu.broad_check import _PROVENANCE_BOOTSTRAP, CheckSpec
from ummanu.dispatch.host import CommandHostRuntime
from ummanu.dispatch.types import HostError
from ummanu.runtime import role_env, test_guard


class TestGuardTests(unittest.TestCase):
    def test_authority_requires_exact_bootstrap_and_workspace_roots(self) -> None:
        root = Path("/candidate")
        digest = hashlib.sha256(_PROVENANCE_BOOTSTRAP.encode()).hexdigest()
        argv = CheckSpec.for_module("shared").argv(Path("/tmp/provenance"), root)
        self.assertTrue(test_guard.bootstrap_authorizes(argv, str(root), digest))
        for index, value in ((1, "-m"), (2, "# wrapper marker"), (6, "/other:/other/src")):
            changed = list(argv)
            changed[index] = value
            self.assertFalse(test_guard.bootstrap_authorizes(changed, str(root), digest))
        self.assertFalse(test_guard.bootstrap_authorizes(argv, "/other", digest))
        self.assertFalse(test_guard.bootstrap_authorizes(argv[:6], str(root), digest))

    def test_ancestry_is_live_and_does_not_consult_environment_markers(self) -> None:
        digest = hashlib.sha256(_PROVENANCE_BOOTSTRAP.encode()).hexdigest()
        runner = CheckSpec.for_module("shared").argv(Path("/tmp/provenance"), Path("/candidate"))
        records = {"/proc/30/cmdline": b"python\0-m\0pytest\0", "/proc/20/cmdline": "\0".join(runner).encode()}
        with (
            mock.patch.object(test_guard.os, "getpid", return_value=30),
            mock.patch.object(Path, "read_bytes", autospec=True, side_effect=lambda path: records[str(path)]),
            mock.patch.object(Path, "read_text", return_value="30 (name with ) parentheses) S 20 0"),
            mock.patch.dict("os.environ", {}, clear=True),
        ):
            self.assertTrue(test_guard.authorized("/candidate", digest))
            records["/proc/20/cmdline"] = b"head\0"
            with mock.patch.object(Path, "read_text", return_value="20 (head) S 1 0"):
                self.assertFalse(test_guard.authorized("/candidate", digest))
            with mock.patch.object(Path, "read_bytes", side_effect=FileNotFoundError):
                self.assertFalse(test_guard.authorized("/candidate", digest))

    def test_hook_refuses_only_cli_events_and_prints_one_applicable_command(self) -> None:
        with mock.patch.object(test_guard.sys, "addaudithook") as add:
            test_guard.install("/candidate", "digest")
        audit = add.call_args.args[0]
        for event, args in (("import", ("pytest",)), ("cpython.run_module", ("ummanu",)),
                            ("cpython.run_file", ("ordinary.py",))):
            with mock.patch.object(test_guard.os, "_exit") as leave:
                audit(event, args)
                leave.assert_not_called()
        for event, args in (("cpython.run_module", ("unittest",)),
                            ("cpython.run_module", ("pytest",)),
                            ("cpython.run_file", ("/venv/bin/pytest",))):
            with (
                mock.patch.object(test_guard, "authorized", return_value=False),
                mock.patch.object(test_guard.sys, "argv", ["-m", "tests/test_one.py::test_one"]),
                mock.patch.object(test_guard.os, "write") as write,
                mock.patch.object(test_guard.os, "_exit", side_effect=SystemExit(125)),
                self.assertRaises(SystemExit),
            ):
                audit(event, args)
            output = write.call_args.args[1].decode()
            self.assertEqual(output.count("\n"), 1)
            self.assertIn("ummanu check tests/test_one.py::test_one", output)
        with mock.patch.object(test_guard, "authorized", return_value=True), mock.patch.object(test_guard.os, "_exit") as leave:
            audit("cpython.run_module", ("pytest",))
            leave.assert_not_called()

    def test_guidance_quotes_selectors_and_does_not_forward_runner_options(self) -> None:
        self.assertEqual(
            test_guard.guidance("pytest", ["-m", "not ci_only", "-s", "test_x.py::test_one[space]"]),
            "test-guard: direct pytest refused; use ummanu check 'test_x.py::test_one[space]'\n",
        )
        self.assertEqual(test_guard.guidance("unittest", ["-v", "tests.test_x.Case.test_one"]),
                         "test-guard: direct unittest refused; use ummanu check tests.test_x.Case.test_one\n")

    def test_installer_refuses_shared_or_foreign_sites_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch) / "candidate"
            root.mkdir()
            outside = Path(scratch) / "live"
            site = outside / "lib/python3.12/site-packages"
            site.mkdir(parents=True)
            (outside / "pyvenv.cfg").touch()
            (root / ".venv").symlink_to(outside)
            with self.assertRaises(ValueError):
                test_guard.install_environment(root, root / ".venv")
            self.assertEqual(list(site.iterdir()), [])
            local = root / "local"
            local.mkdir()
            (local / "pyvenv.cfg").touch()
            (local / "lib").symlink_to(outside / "lib")
            with self.assertRaises(ValueError):
                test_guard.install_environment(root, local)
            self.assertEqual(list(site.iterdir()), [])


class HostGuardOwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name)
        self.root = self.scratch / "candidate"
        self.root.mkdir()
        self.default = self.venv(self.root / role_env.WORKSPACE_ENV_DIR)
        self.external = self.venv(self.scratch / "shared")
        (self.external / "sentinel").write_text("untouched")
        self.adapter = {"broad_check": {}}
        self.host = CommandHostRuntime(SimpleNamespace(adapter=lambda _: self.adapter), self.scratch / "data",
                                       mode="real", production_runtime=SimpleNamespace(interpreter="/unused"))

    def venv(self, prefix: Path) -> Path:
        site = prefix / "lib/python3.12/site-packages"
        site.mkdir(parents=True)
        (prefix / "pyvenv.cfg").touch()
        (prefix / "bin").mkdir()
        (prefix / "bin/python").symlink_to("/usr/bin/python3")  # Never execute an interpreter here.
        return prefix

    def snapshot(self, prefix: Path) -> dict[str, bytes]:
        return {str(path.relative_to(prefix)): path.read_bytes() for path in prefix.rglob("*")
                if path.is_file() and not path.is_symlink()}

    def test_external_system_shared_and_escaped_optional_prefixes_are_skipped(self) -> None:
        (self.root / ".shared").symlink_to(self.external)
        bin_alias = self.root / "alias"
        bin_alias.mkdir()
        (bin_alias / "bin").symlink_to(self.external / "bin")
        before = self.snapshot(self.external)
        for interpreter in ("/usr/bin/python3", str(self.external / "bin/python"),
                            ".shared/bin/python", "alias/bin/python", "missing/bin/python"):
            with self.subTest(interpreter=interpreter):
                self.adapter["broad_check"]["interpreter"] = interpreter
                self.host._install_workspace_test_guards(self.root, project="fixture")
                site = next(self.default.glob("lib/python3*/site-packages"))
                self.assertTrue((site / test_guard.STARTUP_FILE).is_file())
                self.assertTrue((site / test_guard.MODULE_FILE).is_file())
                self.assertEqual(self.snapshot(self.external), before)

    def test_relative_and_absolute_local_venvs_guard_the_prefix_despite_executable_symlink(self) -> None:
        local = self.venv(self.root / ".venv")
        for interpreter in (".venv/bin/python", str(local / "bin/python")):
            with self.subTest(interpreter=interpreter):
                self.adapter["broad_check"]["interpreter"] = interpreter
                self.host._install_workspace_test_guards(self.root, project="fixture")
                site = next(local.glob("lib/python3*/site-packages"))
                self.assertTrue((site / test_guard.STARTUP_FILE).is_file())
                (site / test_guard.STARTUP_FILE).unlink()

    def test_escaped_optional_site_is_skipped_but_required_failure_is_closed(self) -> None:
        local = self.root / "escaped"
        local.mkdir()
        (local / "pyvenv.cfg").touch()
        (local / "bin").mkdir()
        (local / "bin/python").symlink_to("/usr/bin/python3")
        (local / "lib").symlink_to(self.external / "lib")
        before = self.snapshot(self.external)
        self.adapter["broad_check"]["interpreter"] = str(local / "bin/python")
        self.host._install_workspace_test_guards(self.root, project="fixture")
        self.assertEqual(self.snapshot(self.external), before)
        (self.default / "pyvenv.cfg").unlink()
        with self.assertRaises(HostError):
            self.host._install_workspace_test_guards(self.root, project="fixture")
        self.assertEqual(self.snapshot(self.external), before)
