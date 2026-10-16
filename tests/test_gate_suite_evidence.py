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
from tests.support.persisted_mapping import record as dispatcher_record
from ummanu.dispatch import ci_evidence, gate, gate_lifecycle, github_ci, state

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
        long_block = long_block.split("\nTiming violations:", 1)[0].splitlines()
        kept = [line for line in long_block if "lines omitted)" not in line]
        self.assertLessEqual(len(kept), ci_evidence.TRACEBACK_LINES)
        self.assertIn('  File "/home/runner/work/ummanu/ummanu/tests/test_store.py", line 52, in test_reopen', kept)
        self.assertIn('  File "/home/runner/work/ummanu/ummanu/src/ummanu/store.py", line 140, in reopen', kept)
        self.assertEqual(kept[-1], "ummanu.store.StoreClosed: /tmp/store")

    def test_cap_selects_one_set_of_tests_for_locations_and_tracebacks(self):
        # report.json lists the rollup failure first; test-output.log prints the store error first.
        with mock.patch.object(ci_evidence, "TESTS_PER_SUITE", 1):
            text = ci_evidence.render_suite("unit", FIXTURE)
        located = [line for line in text.splitlines() if line.startswith("- tests.")]
        headers = [line for line in text.splitlines() if line.startswith(("FAIL: ", "ERROR: "))]
        self.assertEqual(located, [(
            "- tests.test_rollup.RollupTests.test_missing_check_is_red: "
            "/home/runner/work/ummanu/ummanu/tests/test_rollup.py:88"
        )])
        self.assertEqual(headers, [
            "FAIL: test_missing_check_is_red (tests.test_rollup.RollupTests.test_missing_check_is_red)",
        ])
        self.assertIn("(1 more failing tests omitted)", text)

    def test_subtest_and_fixture_errors_pair_with_their_locations(self):
        sep, dash = "=" * 70, "-" * 70
        report = {"outcome": "product_failure", "counts": {"failed": 1, "error": 1}, "failure_locations": [
            "setUpClass (tests.test_a.ATests): /w/tests/test_a.py:9",
            "tests.test_b.BTests.test_x (case='two'): /w/tests/test_b.py:20",
        ]}
        lines = [
            sep, "FAIL: test_x (tests.test_b.BTests.test_x) (case='two')", dash,
            "Traceback (most recent call last):", "AssertionError: subtest two", "",
            sep, "ERROR: setUpClass (tests.test_a.ATests)", dash,
            "Traceback (most recent call last):", "RuntimeError: fixture", "", dash, "Ran 2 tests",
        ]
        log = "\n".join(lines)
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "report.json").write_text(json.dumps(report), encoding="utf-8")
            (directory / "test-output.log").write_text(log, encoding="utf-8")
            text = ci_evidence.render_suite("unit", directory)
        tracebacks = text.split("Tracebacks:\n", 1)[1].splitlines()
        self.assertEqual(tracebacks, [
            "ERROR: setUpClass (tests.test_a.ATests)", "Traceback (most recent call last):", "RuntimeError: fixture",
            "FAIL: test_x (tests.test_b.BTests.test_x) (case='two')", "Traceback (most recent call last):",
            "AssertionError: subtest two",
        ])


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



class RedEvidencePersistenceTests(unittest.TestCase):
    def test_red_gate_records_the_evidence_reference_before_the_comment_and_across_restart(self):
        sha = github_fixture()[3]
        fake = EvidenceHost(["unit"], {f"ci-evidence-unit-{sha}": FIXTURE})
        result = fake.gate()
        runtime = mock.Mock()
        runtime.host.head_commit.return_value = sha
        runtime._end_review_pane_confirmed.return_value = None
        record = dispatcher_record()
        seen = {}

        def transition(runtime, task, record, records, payload, attempt_id, **kwargs):
            seen["saved"] = json.loads(json.dumps(record.to_json()))
            seen["comment"] = kwargs["move_reason"]
            return {"status": "red"}

        with mock.patch.object(gate_lifecycle, "_begin_red_transition", side_effect=transition):
            gate_lifecycle.gate_red_to_worker(
                runtime, {"ref": "p-1", "comments": []}, record, {}, {}, "attempt-1", result, phase="gate",
            )
        reference = [{"run_id": "7", "suite": "unit", "artifact": f"ci-evidence-unit-{sha}"}]
        restarted = state.DispatcherRecord.from_json(seen["saved"])
        self.assertEqual(restarted.rejected_gate_evidence, reference)
        self.assertIn("Failed suite evidence:", seen["comment"])
        self.assertIn("tests/test_rollup.py:88", seen["comment"])
        self.assertIn(f"<!-- gate-suite-evidence: {json.dumps(reference, separators=(',', ':'))} -->", seen["comment"])


if __name__ == "__main__":
    unittest.main()
