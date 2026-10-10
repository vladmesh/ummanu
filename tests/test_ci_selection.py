from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from scripts.ci_selection import (
    SelectionError,
    changed_paths,
    import_graph,
    plan_digest,
    read_plan,
    select,
    validation_result,
)
from scripts.ci_test_shards import (
    BoundedTee,
    CoverageError,
    ManifestError,
    _changed_line_report,
    _checkout_status,
    _read_evidence,
    _suite_coverage_data,
    _write_evidence,
    aggregate_coverage,
    aggregate_evidence,
    load_manifest,
    run_reported_suite,
)

SHA = "a" * 40


class SelectionTests(unittest.TestCase):
    def sources(self):
        return {
            "src/ummanu/__init__.py": "",
            "src/ummanu/leaf.py": "VALUE = 1\n",
            "src/ummanu/middle.py": "from .leaf import VALUE\n",
            "src/ummanu/unrelated.py": "OTHER = 1\n",
            "tests/__init__.py": "",
            "tests/helper.py": "from ummanu.middle import VALUE\n",
            "tests/test_direct.py": "from ummanu import leaf\n",
            "tests/test_transitive.py": "from .helper import VALUE\n",
            "tests/test_other.py": "import ummanu.unrelated\n",
        }

    def grouped(self):
        return {"unit": ["tests/test_direct.py", "tests/test_other.py"],
                "component": ["tests/test_transitive.py"]}

    def choose(self, changes, before=None, after=None):
        return select(self.grouped(), changes, before or self.sources(), after or self.sources())

    def test_transitive_relative_imports_select_modules_under_original_owners(self):
        mode, reasons, selected, why = self.choose([("M", ("src/ummanu/leaf.py",))])
        self.assertEqual(mode, "affected")
        self.assertTrue(reasons)
        self.assertEqual(selected, {"unit": ["tests/test_direct.py"],
                                    "component": ["tests/test_transitive.py"]})
        self.assertEqual(why["tests/test_direct.py"], ["consumes ummanu.leaf"])
        self.assertEqual(why["tests/test_transitive.py"], ["consumes tests.helper"])

    def test_changed_test_helper_and_added_test(self):
        self.assertEqual(self.choose([("M", ("tests/helper.py",))])[2],
                         {"component": ["tests/test_transitive.py"]})
        for status in ("M", "A"):
            self.assertEqual(self.choose([(status, ("tests/test_other.py",))])[2],
                             {"unit": ["tests/test_other.py"]})

    def test_package_documentation_and_field_reflection_do_not_import_modules(self):
        sources = self.sources()
        sources["tests/__init__.py"] = '"""Examples use ummanu.leaf and ummanu.middle."""\n'
        sources["src/ummanu/__init__.py"] = 'def __getattr__(name):\n    return "metadata"\n'
        sources["src/ummanu/unrelated.py"] += 'def field(obj):\n    return getattr(obj, "field", None)\n'
        mode, _, selected, _ = self.choose([("M", ("src/ummanu/leaf.py",))], before=sources, after=sources)
        self.assertEqual(mode, "affected")
        self.assertEqual(selected, {"unit": ["tests/test_direct.py"],
                                    "component": ["tests/test_transitive.py"]})
        self.assertNotIn("tests/test_other.py", selected["unit"])

    def test_both_snapshots_preserve_removed_import_edges(self):
        after = self.sources()
        after["tests/helper.py"] = "VALUE = 0\n"
        self.assertIn("component", self.choose([("M", ("src/ummanu/leaf.py",))], after=after)[2])

    def test_docs_mixed_deleted_renamed_and_space_paths(self):
        for changes in ([('M', ('README.md',))], [('A', ('docs/design notes.rst',))],
                        [('D', ('docs/old.md',))], [('R100', ('old.md', 'docs/new.md'))]):
            self.assertEqual(self.choose(changes)[:3],
                             ("docs-only", ["all old and new paths are documentation"], {}))
        self.assertEqual(self.choose([("M", ("README.md",)), ("M", ("src/ummanu/leaf.py",))])[0],
                         "affected")
        for changes in ([('D', ('src/ummanu/leaf.py',))],
                        [('R100', ('src/ummanu/leaf.py', 'docs/leaf.md'))],
                        [('R90', ('docs/old.md', 'src/ummanu/leaf.py'))]):
            self.assertEqual(self.choose(changes)[0], "full")
        decoded = changed_paths(b"R100\0old file.py\0docs/new file.md\0M\0README.md\0")
        self.assertEqual(decoded[0], ("R100", ("old file.py", "docs/new file.md")))
        self.assertEqual(self.choose(decoded)[0], "full")

    def test_empty_unknown_yaml_infrastructure_unsafe_and_no_consumer_fall_back(self):
        for path in ("LICENSE", "config.yaml", "src/ummanu/agents/spec.toml", "scripts/runner.py",
                     "tests/ci-shards.txt", ".github/workflows/ci.yml", "pyproject.toml"):
            self.assertEqual(self.choose([("M", (path,))])[0], "full")
        self.assertEqual(self.choose([])[0], "full")
        bad = self.sources()
        bad["src/ummanu/leaf.py"] = "def invalid(:\n"
        self.assertEqual(self.choose([("M", ("src/ummanu/leaf.py",))], after=bad)[0], "full")
        bad["src/ummanu/leaf.py"] = "import importlib\nimportlib.import_module(name)\n"
        self.assertEqual(self.choose([("M", ("src/ummanu/leaf.py",))], after=bad)[0], "full")
        self.assertEqual(self.choose([("M", ("src/ummanu/not_in_graph.py",))])[0], "full")

    def test_opaque_consumers_are_universal_and_literal_dynamic_import_is_resolved(self):
        source = self.sources()
        source["tests/test_direct.py"] = "import importlib\nimportlib.import_module('ummanu.leaf')\n"
        source["tests/test_other.py"] = "import importlib\nimportlib.import_module(variable)\n"
        mode, _, selected, why = self.choose([("M", ("src/ummanu/leaf.py",))], after=source)
        self.assertEqual(mode, "affected")
        self.assertIn("tests/test_other.py", selected["unit"])
        self.assertEqual(why["tests/test_other.py"], ["conservative opaque consumer"])
        self.assertEqual(self.choose([("M", ("tests/test_other.py",))], after=source)[0], "full")
        source["tests/test_other.py"] = "from ummanu.leaf import *\n"
        self.assertEqual(self.choose([("M", ("tests/test_other.py",))], after=source)[0], "full")

    def test_selection_never_executes_module_body_and_refuses_ambiguous_import(self):
        sources = self.sources()
        sources["src/ummanu/leaf.py"] = "raise RuntimeError('must never execute')\n"
        self.assertEqual(self.choose([("M", ("src/ummanu/leaf.py",))], after=sources)[0], "affected")
        sources["tests/helper.py"] = "from ummanu.missing import x\n"
        self.assertEqual(self.choose([("M", ("src/ummanu/leaf.py",))], after=sources)[0], "full")
        with self.assertRaises(SelectionError):
            import_graph({"tests/foo.py": "", "tests/foo/__init__.py": ""})

    def test_invalid_diff_contract_refuses(self):
        for data in (b"M\0README.md", b"R100\0a\0", b"U\0a\0", b"M\0../escape.md\0"):
            with self.assertRaises(SelectionError):
                changed_paths(data)

    def test_invalid_plan_is_recomputed_and_refused(self):
        expected = {"schema_version": 1, "candidate_sha": SHA, "mode": "full",
                    "selected": self.grouped(), "event": "push"}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plan.json"
            for key, value in (("mode", "docs-only"), ("candidate_sha", "b" * 40),
                               ("selected", {}), ("event", "pull_request"), ("schema_version", 99)):
                invalid = {**expected, key: value}
                path.write_text(json.dumps(invalid))
                with (patch("scripts.ci_selection.build_plan", return_value=expected),
                      self.assertRaisesRegex(SelectionError, "differs")):
                    read_plan(Path(tmp), path, self.grouped(), candidate_sha=SHA,
                              base_sha="", event="push", ref="refs/heads/main")

    def test_full_manifest_cannot_be_hidden_by_selection(self):
        from scripts.ci_test_shards import SUITES

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            entries = []
            for index, suite in enumerate(SUITES):
                path = f"tests/test_{index}.py"
                (root / path).write_text("")
                entries.append(f"{suite} {path}")
            manifest = root / "tests/ci-shards.txt"
            valid = "\n".join(entries) + "\n"
            manifest.write_text(valid)
            self.assertEqual(len(load_manifest(root)), 9)
            for invalid in (valid + entries[0] + "\n", valid + "unit tests/test_stale.py\n",
                            "\n".join(entries[1:]), valid + "alien tests/test_0.py\n"):
                manifest.write_text(invalid)
                with self.assertRaises(ManifestError):
                    load_manifest(root)
            manifest.write_text(valid)
            (root / "tests/test_unclaimed.py").write_text("")
            with self.assertRaisesRegex(ManifestError, "unclaimed"):
                load_manifest(root)


class ExecutionSelectionTests(unittest.TestCase):
    def plan(self):
        return {"candidate_sha": SHA, "candidate_tree": "c" * 40, "base_sha": "b" * 40,
                "merge_base": "b" * 40, "event": "pull_request", "ref": "refs/pull/1/merge",
                "mode": "affected", "reasons": ["fixture import closure"],
                "selected": {"unit": ["tests/test_selected_fixture.py"]}, "explanations": {},
                "validation": {"typecheck": True, "lint": True}}

    def produce(self, root, *, fail=False):
        executed = []
        modules = {}
        for module_name in ("tests.test_selected_fixture", "tests.test_unselected_fixture"):
            module = ModuleType(module_name)
            def body(case, name=module_name):
                executed.append(name)
                if fail:
                    case.fail("selected product failure")
            case = type("ActualTest", (unittest.TestCase,), {"test_actual": body, "__module__": module_name})
            module.ActualTest = case
            modules[module_name] = module
        log = BoundedTee(StringIO(), root / "test-output.log")
        with (patch.dict(sys.modules, modules),
              patch.multiple(sys.modules["tests"], create=True,
                             **{name.rpartition(".")[2]: module for name, module in modules.items()})):
            evidence = run_reported_suite("unit", self.plan()["selected"]["unit"], SHA, log)
        self.assertEqual(executed, ["tests.test_selected_fixture"])
        self.assertEqual(evidence.counts["collected"], 1)
        self.assertEqual(evidence.executed_modules, ["tests.test_selected_fixture"])
        evidence.selection_digest = plan_digest(self.plan())
        evidence.checkout_status = _checkout_status("", "")
        _write_evidence(root, evidence, log)
        return evidence

    def test_subset_executes_only_selected_module_and_aggregate_requires_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evidence = self.produce(root)
            self.assertEqual(evidence.outcome, "success")
            with redirect_stdout(StringIO()) as output:
                self.assertEqual(aggregate_evidence(root, "success", self.plan()), 0)
            self.assertIn("`component`: `not_applicable`", output.getvalue())
            self.assertEqual(_read_evidence(root).executed_modules, evidence.executed_modules)
            for result in ("failure", "cancelled", "skipped"):
                with redirect_stdout(StringIO()):
                    self.assertNotEqual(aggregate_evidence(root, result, self.plan()), 0)

    def test_actual_failure_is_red_and_missing_foreign_duplicate_corrupt_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(self.produce(root, fail=True).outcome, "product_failure")
            with redirect_stdout(StringIO()):
                self.assertEqual(aggregate_evidence(root, "failure", self.plan()), 1)
            self.produce(root)
            report = root / "report.json"
            original = json.loads(report.read_text())
            for key, value in (("candidate_sha", "b" * 40), ("suite", "component"),
                               ("schema_version", 1),
                               ("duration_seconds", -1),
                               ("counts", {**original["counts"], "passed": 999}),
                               ("selected_modules", ["tests.foreign"]), ("executed_modules", []),
                               ("executed_modules", ["tests.test_selected_fixture"] * 2),
                               ("selection_digest", "d" * 64), ("outcome", "not_applicable"),
                               ("checkout_status", None), ("timing", {"status": "unavailable"})):
                report.write_text(json.dumps({**original, key: value}))
                with redirect_stdout(StringIO()):
                    self.assertEqual(aggregate_evidence(root, "success", self.plan()), 3, key)
            report.write_text(json.dumps(original))
            report.write_text(report.read_text().replace('"outcome": "success"', '"outcome": "success", "outcome": "success"'))
            with redirect_stdout(StringIO()):
                self.assertEqual(aggregate_evidence(root, "success", self.plan()), 3)
            report.write_text(json.dumps(original))
            duplicate = root / "duplicate"
            duplicate.mkdir()
            for filename in ("report.json", "junit.xml", "test-output.log"):
                (duplicate / filename).write_bytes((root / filename).read_bytes())
            with redirect_stdout(StringIO()):
                self.assertEqual(aggregate_evidence(root, "success", self.plan()), 3)
            for filename in duplicate.iterdir():
                filename.unlink()
            (root / "junit.xml").write_text('<testsuite name="unit" hostname="wrong" tests="0"/>')
            with redirect_stdout(StringIO()):
                self.assertEqual(aggregate_evidence(root, "success", self.plan()), 3)
            (root / "junit.xml").write_text("malformed")
            with redirect_stdout(StringIO()):
                self.assertEqual(aggregate_evidence(root, "success", self.plan()), 3)
            for filename in root.glob("*.json"):
                filename.unlink()
            with redirect_stdout(StringIO()):
                self.assertEqual(aggregate_evidence(root, "success", self.plan()), 3)

    def test_docs_only_requires_plan_and_allowed_skipped_results_without_coverage(self):
        plan = {**self.plan(), "mode": "docs-only", "selected": {},
                "validation": {"typecheck": False, "lint": False}}
        results = {"selection": "success", "test_suites": "skipped", "typecheck": "skipped", "lint": "skipped"}
        self.assertTrue(validation_result(plan, results))
        for job in results:
            for result in ("failure", "cancelled"):
                self.assertFalse(validation_result(plan, {**results, job: result}))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with redirect_stdout(StringIO()):
                self.assertEqual(aggregate_evidence(root, "skipped", plan), 0)
                self.assertEqual(aggregate_evidence(root, "skipped"), 3)
                with patch("scripts.ci_test_shards._candidate_checkout"):
                    self.assertEqual(aggregate_coverage(root, root, root / "out", SHA, "b" * 40, plan), 0)
            self.assertFalse((root / "out").exists())

    def test_coverage_requires_exact_selection_rejects_foreign_and_exposes_unmeasured(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(CoverageError):
                _suite_coverage_data(root, SHA, ["unit"])
            selected = root / f"ci-coverage-unit-{SHA}" / "coverage.unit"
            selected.parent.mkdir()
            selected.write_bytes(b"fixture; native database validation is CI-only")
            self.assertEqual(_suite_coverage_data(root, SHA, ["unit"]), [selected])
            (root / "coverage.foreign").write_bytes(b"foreign")
            with self.assertRaisesRegex(CoverageError, "foreign"):
                _suite_coverage_data(root, SHA, ["unit"])
            with self.assertRaises(CoverageError):
                _suite_coverage_data(root, "b" * 40, ["unit"])
        report = _changed_line_report({"files": {}}, {"src/ummanu/unexecuted.py": [1]},
                                      base_sha="b" * 40, candidate_sha=SHA)
        self.assertEqual(report["lines"][0]["classification"], "unmeasured")

    def test_foreign_observed_module_and_empty_success_refuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.produce(root)
            report = root / "report.json"
            original = json.loads(report.read_text())
            invalid = copy.deepcopy(original)
            invalid["timing"]["modules"]["tests.foreign"] = 0.1
            for data in (invalid, {**original, "counts": dict.fromkeys(original["counts"], 0)}):
                report.write_text(json.dumps(data))
                with redirect_stdout(StringIO()):
                    self.assertEqual(aggregate_evidence(root, "success", self.plan()), 3)
