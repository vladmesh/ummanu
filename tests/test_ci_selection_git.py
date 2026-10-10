"""Real Git, checkout execution and coverage lifecycle proofs belong only to CI."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from scripts.ci_selection import SelectionError, build_plan, read_plan, select, snapshot, summary, validation_result
from scripts.ci_test_shards import SUITES, aggregate_coverage, aggregate_evidence, load_manifest
from tests.support.git import git


class SelectionGitTests(unittest.TestCase):
    def test_repository_narrow_source_projection_prints_each_selected_reason(self):
        root = Path(__file__).resolve().parents[1]
        candidate = git(root, "rev-parse", "HEAD")
        grouped = load_manifest(root)
        sources = snapshot(root, candidate)
        # A projection over committed sources is not a natural code PR or live gate proof.
        mode, reasons, selected, explanations = select(
            grouped, [("M", ("src/ummanu/board/terminal_taxonomy.py",))], sources, sources)
        self.assertEqual(mode, "affected", reasons)
        self.assertLess(sum(map(len, selected.values())), sum(map(len, grouped.values())))
        self.assertIn("tests/test_terminal_taxonomy.py", selected["unit"])
        plan = {"candidate_sha": candidate, "candidate_tree": git(root, "rev-parse", "HEAD^{tree}"),
                "base_sha": candidate, "merge_base": candidate, "event": "projection", "ref": "fixture",
                "mode": mode, "reasons": reasons, "selected": selected, "explanations": explanations}
        print("Narrow terminal_taxonomy source projection; not live acceptance evidence")
        print(summary(plan))

    def fixture(self, root):
        (root / "tests").mkdir()
        (root / "src/ummanu").mkdir(parents=True)
        (root / "src/ummanu/__init__.py").write_text("")
        (root / "src/ummanu/leaf.py").write_text("VALUE = 1\n")
        (root / "tests/__init__.py").write_text("")
        entries = []
        for index, suite in enumerate(SUITES):
            path = f"tests/test_{index}.py"
            (root / path).write_text(
                "import unittest\n" + ("from ummanu.leaf import VALUE\n" if index == 0 else "")
                + "class Case(unittest.TestCase):\n    def test_pass(self):\n"
                + ("        self.assertIn(VALUE, [1, 2])\n" if index == 0 else "        self.assertTrue(True)\n"))
            entries.append(f"{suite} {path}\n")
        (root / "tests/ci-shards.txt").write_text("".join(entries))
        (root / "README.md").write_text("fixture\n")
        (root / ".coveragerc").write_text("[run]\nbranch = True\nrelative_files = True\nsource = src/ummanu\n")
        git(root, "init", "--quiet")
        git(root, "config", "user.name", "CI fixture")
        git(root, "config", "user.email", "ci@example.invalid")
        return self.commit(root)

    def commit(self, root):
        git(root, "add", "-A")
        git(root, "commit", "--quiet", "--allow-empty", "-m", "fixture")
        return git(root, "rev-parse", "HEAD").strip()

    def plan(self, root, base, candidate, event="pull_request"):
        return build_plan(root, load_manifest(root), candidate, base, event,
                          "refs/pull/1/merge" if event == "pull_request" else "refs/heads/main")

    def test_exact_git_docs_diff_and_full_main_dispatch_empty_missing_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = self.fixture(root)
            (root / "design notes.md").write_text("documentation\n")
            candidate = self.commit(root)
            docs = self.plan(root, base, candidate)
            self.assertEqual(docs["mode"], "docs-only")
            self.assertEqual(docs["merge_base"], base)
            self.assertEqual(docs["selected"], {})
            for event in ("push", "workflow_dispatch"):
                full = self.plan(root, base, candidate, event)
                self.assertEqual(full["mode"], "full")
                self.assertEqual(full["selected"], load_manifest(root))
            for missing in ("", "0" * 40, candidate):
                self.assertEqual(self.plan(root, missing, candidate)["mode"], "full")
            with self.assertRaisesRegex(SelectionError, "checkout mismatch"):
                self.plan(root, base, base)
            results = {"selection": "success", "test_suites": "skipped", "typecheck": "skipped", "lint": "skipped"}
            self.assertTrue(validation_result(docs, results))
            with redirect_stdout(StringIO()):
                self.assertEqual(aggregate_evidence(root / "absent", "skipped", docs), 0)
                self.assertEqual(aggregate_coverage(root, root / "absent", root / "out", candidate, base, docs), 0)

    def test_git_delete_rename_mixed_docs_and_copy_preserve_code_impact(self):
        for action in ("delete", "rename-doc", "rename-code", "mixed", "copy"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                base = self.fixture(root)
                source = root / "src/ummanu/leaf.py"
                if action == "delete":
                    source.unlink()
                elif action.startswith("rename"):
                    target = root / ("was code.md" if action == "rename-doc" else "src/ummanu/renamed.py")
                    git(root, "mv", str(source), str(target))
                elif action == "mixed":
                    source.write_text("VALUE = 2\n")
                    (root / "README.md").write_text("new docs\n")
                else:
                    (root / "src/ummanu/new.py").write_bytes(source.read_bytes())
                candidate = self.commit(root)
                plan = self.plan(root, base, candidate)
                self.assertEqual(plan["mode"], "affected" if action == "mixed" else "full")

    def test_exact_plan_regeneration_rejects_foreign_membership_event_base_and_schema(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as artifacts:
            root = Path(tmp)
            base = self.fixture(root)
            (root / "src/ummanu/leaf.py").write_text("VALUE = 2\n")
            candidate = self.commit(root)
            plan = self.plan(root, base, candidate)
            self.assertEqual(plan["selected"], {"unit": ["tests/test_0.py"]})
            self.assertEqual(plan["explanations"], {"tests/test_0.py": ["consumes ummanu.leaf"]})
            self.assertIn("src/ummanu/leaf.py", snapshot(root, candidate))
            path = Path(artifacts) / "plan.json"
            path.write_text(json.dumps(plan))
            kwargs = {"candidate_sha": candidate, "base_sha": base, "event": "pull_request", "ref": "refs/pull/1/merge"}
            self.assertEqual(read_plan(root, path, load_manifest(root), **kwargs), plan)
            for key, value in (("selected", {}), ("schema_version", 99), ("base_sha", candidate),
                               ("mode", "docs-only"), ("event", "push")):
                path.write_text(json.dumps({**plan, key: value}))
                with self.assertRaises(SelectionError):
                    read_plan(root, path, load_manifest(root), **kwargs)

    def test_real_subset_execution_coverage_and_checkout_evidence(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as artifacts:
            root, output = Path(tmp), Path(artifacts)
            base = self.fixture(root)
            (root / "src/ummanu/leaf.py").write_text("VALUE = 2\n")
            candidate = self.commit(root)
            plan = self.plan(root, base, candidate)
            selection = output / "plan.json"
            selection.write_text(json.dumps(plan))
            report_dir = output / "evidence" / "unit"
            coverage_dir = output / "coverage"
            raw = coverage_dir / f"ci-coverage-unit-{candidate}" / "coverage.unit"
            raw.parent.mkdir(parents=True)
            driver = output / "execute.py"
            driver.write_text(
                "import json\nfrom pathlib import Path\n"
                "from scripts.ci_selection import read_plan\n"
                "from scripts.ci_test_shards import load_manifest, run_suite_with_evidence\n"
                f"root = Path({str(root)!r})\n"
                f"plan = read_plan(root, Path({str(selection)!r}), load_manifest(root), "
                f"candidate_sha={candidate!r}, base_sha={base!r}, event='pull_request', ref='refs/pull/1/merge')\n"
                f"raise SystemExit(run_suite_with_evidence(root, 'unit', Path({str(report_dir)!r}), {candidate!r}, plan))\n")
            repo = Path(__file__).resolve().parents[1]
            result = subprocess.run(
                [sys.executable, "-m", "coverage", "run", "--data-file", str(raw), str(driver)],
                cwd=root, env={**os.environ, "PYTHONPATH": f"{root / 'src'}:{repo}", "PYTHONDONTWRITEBYTECODE": "1"},
                capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads((report_dir / "report.json").read_text())
            self.assertEqual(report["executed_modules"], ["tests.test_0"])
            self.assertEqual(report["counts"]["collected"], 1)
            self.assertFalse(report["checkout_status"]["changed"])
            with redirect_stdout(StringIO()):
                self.assertEqual(aggregate_evidence(output / "evidence", "success", plan), 0)
                self.assertEqual(aggregate_coverage(root, coverage_dir, output / "combined", candidate, base, plan), 0)
            combined = json.loads((output / "combined/combined-coverage.json").read_text())
            self.assertEqual(combined["coverage_scope"], "selected modules")
            self.assertEqual(combined["selected"], plan["selected"])
            changed = json.loads((output / "combined/changed-lines.json").read_text())
            self.assertEqual(changed["lines"][0]["classification"], "covered")
            # Corrupt selected raw data must not become a green coverage result.
            raw.write_bytes(b"corrupt sqlite")
            with redirect_stdout(StringIO()):
                self.assertEqual(aggregate_coverage(root, coverage_dir, output / "bad", candidate, base, plan), 3)
