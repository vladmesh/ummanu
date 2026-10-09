"""Local selectors enforce declared membership and never borrow full-round evidence."""

from __future__ import annotations

import json
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

from tests import broad
from tests.support.local_check_fixture import LocalCheckFixture
from ummanu import broad_check, check_commands
from ummanu.broad_check import BroadCheckError
from ummanu.projects.contract import module_contract
from ummanu.projects.local_check import LocalProfile


class LocalSelectorTests(LocalCheckFixture, unittest.TestCase):
    def test_full_module_and_node_keep_the_full_receipt_and_execute_each_subset(self) -> None:
        status, full, output = self.invoke()
        self.assertEqual(status, 0, output)
        self.assertIn("Ran 2 tests", output)
        receipt = Path(full["path"])
        before = receipt.read_bytes()
        self.assertTrue(full["receipt"]["project_provenance"]["inside_workspace"])
        status, reused, _ = self.invoke("broad", "--reuse", "--module", "tests.broad")
        self.assertEqual(status, 0)
        self.assertTrue(reused["reused"])
        self.assertEqual(self.log.read_text().splitlines(), ["one", "two"])
        for selector, count in (
            ("tests/test_local.py", 2),
            ("tests/test_local.py::Cases::test_one", 1),
            ("tests.test_local.Cases.test_one", 1),
        ):
            with self.subTest(selector=selector):
                status, subset, output = self.invoke(selector, "--reuse")
                self.assertEqual(status, 0, output)
                self.assertIn(f"Ran {count} test", output)
                self.assertIn("fixture-one", output)
                self.assertEqual(subset["selector"], selector)
                self.assertNotIn("receipt", subset)
                self.assertNotIn("reused", subset)
                self.assertEqual(receipt.read_bytes(), before)
        self.assertEqual(self.log.read_text().splitlines(), ["one", "two", "one", "two", "one", "one"])
        for argv in (
            ("tests/test_board.py",),
            ("tests/test_board.py::Cases::test_one",),
            ("broad", "--module", "tests.broad", "--module-arg=tests.test_board"),
            ("broad", "--module", "unittest", "--module-arg=tests.test_board"),
            ("broad", "--module", "tests.test_board"),
        ):
            with mock.patch.object(check_commands, "run_broad_check") as run:
                status, error, _ = self.invoke(*argv)
                self.assertEqual(status, 2)
                self.assertEqual(
                    error["error"]["message"],
                    "tests/test_board.py: shard integration-board; execution only in CI",
                )
                run.assert_not_called()
            self.assertEqual(receipt.read_bytes(), before)
            self.assertFalse(self.forbidden.exists())
        self.assertEqual(list(receipt.parent.glob("broad-*.json")), [receipt])
        status, shown, _ = self.invoke("show", "--module", "tests.broad")
        self.assertEqual(status, 0)
        self.assertTrue(shown["usable"])

    def test_subset_without_full_receipt_and_failed_test_preserves_runner_status(self) -> None:
        path = self.root / "tests" / "test_local.py"
        path.write_text(path.read_text() + "    def test_red(self):\n        self.fail('fixture-red')\n")
        status, subset, output = self.invoke("tests/test_local.py::Cases::test_red")
        self.assertEqual(status, 1)
        self.assertEqual(subset["exit_code"], 1)
        self.assertIn("fixture-red", output)
        self.assertFalse(broad_check.receipt_dir(self.root).exists())

    def test_known_ci_only_and_unknown_selectors_refuse_before_runner_or_import(self) -> None:
        for selector in (
            "tests/test_board.py",
            "tests/test_board.py::Cases::test_one",
            "tests.test_board.Cases.test_one",
            "checks/missing.py",
        ):
            with self.subTest(selector=selector), mock.patch.object(check_commands, "run_broad_check") as run:
                status, error, _ = self.invoke(selector)
                self.assertEqual(status, 2)
                run.assert_not_called()
                message = error["error"]["message"]
                if "missing" in selector:
                    self.assertIn("unknown module", message)
                    self.assertNotIn("integration", message)
                else:
                    self.assertEqual(
                        message, "tests/test_board.py: shard integration-board; execution only in CI"
                    )
                self.assertFalse(self.forbidden.exists())

    def test_legacy_flags_cannot_override_membership_or_attest_a_subset(self) -> None:
        for argv in (
            ("broad", "--module", "os"),
            ("broad", "--command", "true"),
            ("broad", "--module", "tests.broad", "--module-arg=tests.test_board"),
            ("broad", "--module", "tests.broad", "--module-arg=-k", "--module-arg=one"),
            ("--module", "os"),
            ("tests/test_local.py", "--command", "true"),
        ):
            with self.subTest(argv=argv), mock.patch.object(check_commands, "run_broad_check") as run:
                status, _error, _ = self.invoke(*argv)
                self.assertEqual(status, 2)
                run.assert_not_called()
        status, subset, output = self.invoke(
            "broad", "--reuse", "--module", "tests.broad", "--module-arg=tests.test_local.Cases.test_one"
        )
        self.assertEqual(status, 0, output)
        self.assertNotIn("receipt", subset)
        self.assertFalse(broad_check.receipt_dir(self.root).exists())
        status, error, _ = self.invoke("show", "--module", "tests.broad", "--module-arg=tests.test_local")
        self.assertEqual(status, 2)
        self.assertEqual(error["error"]["code"], "subset_has_no_receipt")

    def test_old_declared_full_profile_works_but_new_forms_and_subsets_need_local(self) -> None:
        configured = self.adapter["broad_check"]
        del configured["local"]
        configured["args"] = ["tests.test_local"]
        self.write_adapter()
        status, full, output = self.invoke(
            "broad", "--reuse", "--module", "tests.broad", "--module-arg=tests.test_local"
        )
        self.assertEqual(status, 0, output)
        self.assertIn("receipt", full)
        self.assertEqual(self.invoke("show")[0], 0)
        for argv in (
            (),
            ("tests/test_local.py",),
            ("broad", "--module", "tests.broad", "--module-arg=tests.test_board"),
        ):
            with self.subTest(argv=argv), mock.patch.object(check_commands, "run_broad_check") as run:
                status, error, _ = self.invoke(*argv)
                self.assertEqual(status, 2)
                self.assertEqual(error["error"]["code"], "local_check_not_declared")
                run.assert_not_called()

    def test_empty_contradictory_and_unreadable_declarations_never_run(self) -> None:
        originals = json.loads(json.dumps(self.adapter))
        mutations = (
            lambda local: local.update(modules={}),
            lambda local: local.update(shards=["nonexistent"]),
            lambda local: local.update(ci_manifest="missing.txt"),
            lambda local: local.update(modules={"missing.py": "unit"}),
            lambda local: local.update(shards=[]),
        )
        for mutate in mutations:
            self.adapter = json.loads(json.dumps(originals))
            mutate(self.adapter["broad_check"]["local"])
            self.write_adapter()
            with mock.patch.object(check_commands, "run_broad_check") as run:
                self.assertEqual(self.invoke("broad", "--module", "tests.broad")[0], 2)
                run.assert_not_called()
        self.adapter = originals
        self.adapter["broad_check"]["args"] = ["tests.test_local"]
        self.write_adapter()
        self.assertEqual(self.invoke()[0], 2)

    def test_candidate_interpreter_and_provenance_are_shared_with_full_run(self) -> None:
        contract = module_contract(
            {"adapter": "fixture"},
            instance=self.instance,
            project_root=self.root,
            default_interpreter=sys.executable,
        )
        self.assertEqual(contract.local, self.adapter["broad_check"]["local"])
        status, subset, output = self.invoke("tests/test_local.py::Cases::test_one")
        self.assertEqual(status, 0, output)
        self.assertEqual(subset["argv"][0], sys.executable)
        self.assertEqual(
            subset["project_provenance"]["imported_project"], str(self.root / "app" / "__init__.py")
        )

    def test_pytest_parameter_node_is_one_argv_argument_without_shell_interpolation(self) -> None:
        self.adapter["broad_check"]["module"] = "pytest"
        self.adapter["broad_check"]["args"] = ["tests"]
        self.adapter["broad_check"]["local"] = {"membership": "runner", "selector_args": []}
        self.write_adapter()
        selector = "tests/test_local.py::test_one[param with spaces;$(touch injected)::value]"
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
        with mock.patch.object(check_commands, "run_broad_check", return_value=(5, observation)) as run:
            status, subset, _ = self.invoke(selector)
            self.assertEqual(status, 2)
            self.assertEqual(subset["error"]["code"], "pytest_selection_empty")
            spec = run.call_args.args[0]
            self.assertEqual(spec.module_args, (selector,))
            self.assertEqual(spec.argv(self.scratch / "provenance", self.root)[-1], selector)
            self.assertFalse(run.call_args.kwargs["record_receipt"])
            self.assertNotIn("receipt", subset)
        self.assertFalse((self.root / "injected").exists())


class ManifestSelectorTests(LocalCheckFixture, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        (self.root / "scripts").mkdir(exist_ok=True)
        (self.root / "tests").mkdir(exist_ok=True)
        source = Path(__file__).resolve().parents[1]
        shutil.copy(source / "scripts" / "ci_test_shards.py", self.root / "scripts")
        (self.root / "src/ummanu").mkdir(parents=True, exist_ok=True)
        shutil.copy(source / "src/ummanu/test_timing.py", self.root / "src/ummanu")
        from scripts.ci_test_shards import SUITES

        entries = ["unit tests/test_local.py", "integration-board tests/test_board.py"]
        for shard in SUITES:
            relative = f"tests/test_{shard.replace('-', '_')}.py"
            (self.root / relative).write_text("", encoding="utf-8")
            entries.append(f"{shard} {relative}")
        (self.root / "tests" / "ci-shards.txt").write_text("\n".join(entries) + "\n")
        self.adapter["broad_check"].update(
            module="tests.broad",
            local={
                "runner": "unittest",
                "shards": ["unit", "component"],
                "ci_manifest": "tests/ci-shards.txt",
            },
        )
        self.write_adapter()

    def test_full_and_selectors_use_validated_unit_plus_component(self) -> None:
        profile = LocalProfile.load(self.root, self.adapter["broad_check"]["local"])
        self.assertEqual(
            profile.select("tests/test_component.py::Cases::test_one"),
            ("tests/test_component.py", "tests.test_component.Cases.test_one"),
        )
        with mock.patch.object(check_commands, "run_broad_check") as run:
            status, error, _ = self.invoke("tests/test_integration_board.py::Cases::test_one")
            self.assertEqual(status, 2)
            self.assertIn("integration-board", error["error"]["message"])
            run.assert_not_called()

    def test_manifest_preserves_declared_reporting_args_on_full_and_subset(self) -> None:
        self.adapter["broad_check"]["args"] = ["-v"]
        self.write_adapter()
        for argv, expected in (
            ((), ("-v",)),
            (("tests/test_unit.py::Cases::test_one",), ("-v", "tests.test_unit.Cases.test_one")),
            (
                (
                    "broad",
                    "--module",
                    "tests.broad",
                    "--module-arg=-v",
                    "--module-arg=tests.test_unit.Cases.test_one",
                ),
                ("-v", "tests.test_unit.Cases.test_one"),
            ),
        ):
            # Stop at the runner boundary; these new unit fixtures start no check processes.
            with mock.patch.object(
                check_commands, "run_broad_check", side_effect=BroadCheckError("fixture", "observed")
            ) as run:
                self.assertEqual(self.invoke(*argv)[0], 2)
                self.assertEqual(run.call_args.args[0].module_args, expected)

    def test_stale_missing_unclaimed_or_invalid_manifest_stops_before_runner(self) -> None:
        manifest = self.root / "tests" / "ci-shards.txt"
        original = manifest.read_text()
        for text in (
            original + "unit tests/test_missing.py\n",
            original.replace("unit ", "unknown ", 1),
            original.replace("component tests/test_component.py\n", ""),
        ):
            manifest.write_text(text)
            with mock.patch.object(check_commands, "run_broad_check") as run:
                self.assertEqual(self.invoke("tests/test_unit.py")[0], 2)
                run.assert_not_called()
        manifest.unlink()
        with mock.patch.object(check_commands, "run_broad_check") as run:
            self.assertEqual(self.invoke("broad", "--module", "tests.broad")[0], 2)
            run.assert_not_called()

    def test_broad_runner_rejects_explicit_ci_names_before_unittest_imports(self) -> None:
        for selector in ("tests.test_board", "tests.test_board.SomeCase.test_one", "tests.test_unknown"):
            with mock.patch.object(broad.unittest, "main") as run, mock.patch("sys.stderr"):
                self.assertEqual(broad.main([selector]), 2)
                run.assert_not_called()

    def test_unittest_native_and_path_node_ids_have_the_same_selection(self) -> None:
        profile = LocalProfile.load(self.root, self.adapter["broad_check"]["local"])
        self.assertEqual(
            profile.select("tests.test_unit.Cases.test_one"),
            profile.select("tests/test_unit.py::Cases::test_one"),
        )
        for selector in ("discover", "tests/test_unit.py::", "tests/test_unit.py::Cases[test]"):
            with self.assertRaises(BroadCheckError):
                profile.select(selector)
