"""A red aggregate gate renders the failed suites' own CI evidence instead of the aggregate tail."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest import mock

from tests.support.fakes.gate import github_fixture
from ummanu.dispatch import ci_evidence, gate, github_ci

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "ci_evidence" / "unit"
RUN_URL = "https://github.com/org/repo/actions/runs/7"
AGGREGATE_TAIL = (
    "test\tPreserve required aggregate result\ttest_suites: failure\n"
    "test\tPreserve required aggregate result\t##[error]Process completed with exit code 1."
)


class RenderSuiteTests(unittest.TestCase):
    def test_names_the_failing_test_and_the_timing_violation_with_bounded_traceback(self):
        text = ci_evidence.render_suite("unit", FIXTURE)
        self.assertIn("Suite unit: outcome product_failure; collected 41, passed 38, failed 1, error 1", text)
        self.assertIn(
            "- tests.test_rollup.RollupTests.test_missing_check_is_red: "
            "/home/runner/work/ummanu/ummanu/tests/test_rollup.py:88",
            text,
        )
        self.assertIn(
            "- timing budget test tests.test_clock.ClockTests.test_slow_tick: 6.250000s > 5s", text
        )
        self.assertEqual(text.count("timing budget test"), 1)
        self.assertIn("AssertionError: 'PENDING' != 'FAILURE'", text)
        long_block = text.split("ERROR: test_reopen (tests.test_store.StoreTests.test_reopen)\n", 1)[1]
        long_block = long_block.split("\nFAIL: ", 1)[0].splitlines()
        kept = [line for line in long_block if "lines omitted)" not in line]
        self.assertLessEqual(len(kept), ci_evidence.TRACEBACK_LINES)
        self.assertIn('  File "/home/runner/work/ummanu/ummanu/tests/test_store.py", line 52, in test_reopen', kept)
        self.assertIn('  File "/home/runner/work/ummanu/ummanu/src/ummanu/store.py", line 140, in reopen', kept)
        self.assertEqual(kept[-1], "ummanu.store.StoreClosed: /tmp/store")

    def test_caps_tests_per_suite_and_states_the_omitted_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            shutil.copy(FIXTURE / "report.json", directory / "report.json")
            shutil.copy(FIXTURE / "test-output.log", directory / "test-output.log")
            with mock.patch.object(ci_evidence, "TESTS_PER_SUITE", 1):
                text = ci_evidence.render_suite("unit", directory)
        self.assertIn("(1 more failures omitted)", text)
        self.assertIn("(1 more tracebacks omitted)", text)
        headers = [line for line in text.splitlines() if line.startswith(("FAIL: ", "ERROR: "))]
        self.assertEqual(headers, ["ERROR: test_reopen (tests.test_store.StoreTests.test_reopen)"])


class EvidenceHost:
    """github_fixture answers plus a red aggregate run whose suite jobs and artifacts are scripted."""

    def __init__(self, failed_suites, artifacts, *, check="test", log=AGGREGATE_TAIL):
        self.host, self.task, self.record, self.sha = github_fixture()
        self.downloads: list[str] = []
        scripted = self.host._answer
        jobs = [{"name": f"test / {suite}", "conclusion": "failure"} for suite in failed_suites]
        jobs += [{"name": "test / component", "conclusion": "success"}, {"name": check, "conclusion": "failure"}]

        def answer(args, label):
            if label == "gate gh api" and args[2].endswith("check-runs"):
                payload = {"total_count": 1, "check_runs": [{
                    "id": 1, "name": check, "status": "COMPLETED", "conclusion": "FAILURE", "details_url": RUN_URL,
                }]}
                return CompletedProcess(args, 0, json.dumps(payload), "")
            if label == "gate failed log":
                return CompletedProcess(args, 0, log, "")
            if label == "gate run jobs":
                return CompletedProcess(args, 0, json.dumps({"jobs": jobs}), "")
            if label == "gate suite evidence":
                name = args[args.index("-n") + 1]
                self.downloads.append(name)
                source = artifacts.get(name)
                if source is None:
                    return CompletedProcess(args, 1, "", "no valid artifacts found to download")
                shutil.copytree(source, args[args.index("-D") + 1], dirs_exist_ok=True)
                return CompletedProcess(args, 0, "", "")
            return scripted(args, label)

        self.host._answer = answer
        self.check = check

    def gate(self):
        return gate._github_gate(self.host, self.task, self.record, "/fixture", "main", [self.check])


class SuiteLogFallbackTests(unittest.TestCase):
    def test_missing_artifact_keeps_the_aggregate_tail_and_names_the_artifact(self):
        fake = EvidenceHost(["unit"], {})
        result = fake.gate()
        self.assertEqual(result.status, "red")
        self.assertIn("test_suites: failure", result.log)
        self.assertIn(f"suite unit: evidence artifact ci-evidence-unit-{fake.sha} of run 7 unavailable", result.log)
        self.assertIn("no valid artifacts found to download", result.log)
        self.assertEqual(result.suite_evidence, [])


class GateSuiteEvidenceTests(unittest.TestCase):
    def test_two_failed_suites_download_two_artifacts_and_render_both(self):
        sha = github_fixture()[3]
        fake = EvidenceHost(
            ["unit", "integration-board"],
            {f"ci-evidence-unit-{sha}": FIXTURE, f"ci-evidence-integration-board-{sha}": FIXTURE},
        )
        result = fake.gate()
        self.assertEqual(result.status, "red")
        self.assertEqual(fake.downloads, [f"ci-evidence-unit-{sha}", f"ci-evidence-integration-board-{sha}"])
        self.assertIn("Suite unit: outcome product_failure", result.log)
        self.assertIn("Suite integration-board: outcome product_failure", result.log)
        self.assertIn("tests/test_rollup.py:88", result.log)
        self.assertNotIn("test_suites: failure", result.log)
        self.assertEqual(result.suite_evidence, [
            {"run_id": "7", "suite": "unit", "artifact": f"ci-evidence-unit-{sha}"},
            {"run_id": "7", "suite": "integration-board", "artifact": f"ci-evidence-integration-board-{sha}"},
        ])
        self.assertEqual(result.failure_class, "substantive")

    def test_infrastructure_red_downloads_nothing(self):
        fake = EvidenceHost(["unit"], {})
        infra = github_ci._LogFragment(
            True, step="Set up job", text="##[error]Failed to download action HTTP 503",
            failure_class="infrastructure", failure_reason="action-download-http-5xx",
        )
        with mock.patch.object(github_ci, "_failed_log", return_value=infra):
            result = fake.gate()
        self.assertEqual(result.failure_class, "infrastructure")
        self.assertEqual(fake.downloads, [])
        self.assertNotIn("gate run jobs", fake.host.events)
        self.assertEqual(result.log, "##[error]Failed to download action HTTP 503")

    def test_a_red_job_that_is_not_the_aggregate_keeps_its_fragment(self):
        fake = EvidenceHost(["unit"], {}, check="lint", log="lint\tCheck changed Python files\t##[error]E501")
        result = fake.gate()
        self.assertEqual(result.status, "red")
        self.assertNotIn("gate run jobs", fake.host.events)
        self.assertEqual(fake.downloads, [])
        self.assertEqual(result.log, "##[error]E501")


if __name__ == "__main__":
    unittest.main()
