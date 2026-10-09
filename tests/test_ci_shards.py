from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree

import yaml

from scripts.ci_test_shards import (
    CHANGED_LINES_JSON_NAME,
    COVERAGE_JSON_NAME,
    EVIDENCE_FILES,
    FAST_MODULES,
    MAX_COVERAGE_JSON_BYTES,
    SUITES,
    BoundedTee,
    CheckoutStatusError,
    CoverageError,
    ManifestError,
    SuiteEvidence,
    TestRecord,
    _changed_candidate_lines,
    _changed_line_report,
    _read_evidence,
    _suite_coverage_data,
    _summary,
    _write_evidence,
    aggregate_coverage,
    aggregate_evidence,
    fast_environment,
    load_manifest,
    main,
    modules,
    report_summary,
    run_bounded,
    run_fast,
    run_reported_suite,
    run_suite_with_evidence,
    validate_fast_profile,
)
from tests.support.git import git

CANDIDATE_SHA = "a" * 40


class TimingBudgetTests(unittest.TestCase):
    def test_strict_thresholds_and_actual_module_membership(self):
        from ummanu.test_timing import violations

        observation = {"tests": [
            {"identifier": "tests.real.IntegrationNamed.test_a", "duration_seconds": 5.0},
            {"identifier": "tests.real.Other.test_b", "duration_seconds": 5.0001},
        ], "modules": {"tests.real": 90.0, "tests.other": 90.0001}}
        self.assertEqual([item["identifier"] for item in violations(observation, modules=True)],
                         ["tests.real.Other.test_b", "tests.other"])

    def test_module_clock_includes_fixtures_across_classes(self):
        from ummanu.test_timing import TimingResult, TimingSuite, violations

        now = [0.0]

        class First(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                now[0] += 44

            @classmethod
            def tearDownClass(cls):
                now[0] += 2

            def test_a(self):
                now[0] += 1

        class IntegrationNamed(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                now[0] += 44

            def test_b(self):
                now[0] += 1

        First.__module__ = IntegrationNamed.__module__ = __name__
        with patch("ummanu.test_timing.time.monotonic", side_effect=lambda: now[0]):
            runner = unittest.TextTestRunner(stream=StringIO(), resultclass=TimingResult)
            result = runner.run(TimingSuite([First("test_a"), IntegrationNamed("test_b")]))
        observation = result.observation()
        self.assertEqual(observation["modules"], {__name__: 92.0})
        self.assertEqual([r["duration_seconds"] for r in observation["tests"]], [1, 1])
        self.assertEqual([r["module"] for r in observation["tests"]], [__name__, __name__])
        self.assertEqual(violations(observation, modules=True)[0]["kind"], "module")

    def test_ci_budget_failure_report_junit_summary_and_nonlocal_exemption(self):
        from ummanu.test_timing import TimingSuite

        now = [0.0]

        class Specimen(unittest.TestCase):
            def test_slow_fake_clock(self):
                now[0] += 6

        for suite in ("unit", "component", "runtime-component", "integration-heads", "packaging"):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                log = BoundedTee(StringIO(), root / "test-output.log")
                with (patch("ummanu.test_timing.time.monotonic", side_effect=lambda: now[0]),
                      patch.object(unittest.defaultTestLoader, "loadTestsFromName",
                                   return_value=TimingSuite([Specimen("test_slow_fake_clock")]))):
                    evidence = run_reported_suite(suite, ["tests/test_fake.py"], CANDIDATE_SHA, log)
                limited = suite in {"unit", "component"}
                self.assertEqual(evidence.outcome, "product_failure" if limited else "success")
                self.assertEqual(evidence.counts["passed"], 1)
                _write_evidence(root, evidence, log)
                restored = _read_evidence(root)
                self.assertEqual(restored.timing, evidence.timing)
                self.assertEqual(restored.timing_violations, evidence.timing_violations)
                if limited:
                    self.assertIn("test_slow_fake_clock", _summary(restored))
                    self.assertIn("6.000000s > 5s", ElementTree.parse(root / "junit.xml").find("testcase/failure").text)
                    self.assertIn("timing budget test", (root / "test-output.log").read_text())

    def test_outcomes_and_interrupted_timing_have_no_invented_zero(self):
        from ummanu.test_timing import TimingRunner, TimingResult

        class Cases(unittest.TestCase):
            def test_pass(self):
                pass

            def test_fail(self):
                self.fail("original failure")

            def test_error(self):
                raise ValueError("original error")

            @unittest.skip("explicit skip")
            def test_skip(self):
                pass

        result = TimingRunner(stream=StringIO()).run(unittest.defaultTestLoader.loadTestsFromTestCase(Cases))
        self.assertFalse(result.wasSuccessful())
        self.assertEqual({r.outcome for r in result.records.values()}, {"passed", "failed", "error", "skipped"})
        self.assertEqual(result.observation()["status"], "complete")
        stream = unittest.runner._WritelnDecorator(StringIO())
        interrupted = TimingResult(stream, True, 1)
        interrupted.startTest(Cases("test_pass"))
        self.assertIsNone(interrupted.observation()["tests"][0]["duration_seconds"])
        self.assertEqual(interrupted.observation()["status"], "incomplete")


class CiTestSuiteManifestTests(unittest.TestCase):
    def _commit_checkout(self, root: Path) -> None:
        git(root, "init", "--quiet")
        git(root, "config", "user.name", "CI test")
        git(root, "config", "user.email", "ci-test@example.invalid")
        git(root, "add", ".")
        git(root, "commit", "--quiet", "-m", "fixture")

    def _run_temporary_suite(self, root: Path, report_dir: Path) -> int:
        grouped = {suite: ["tests/test_passing.py"] for suite in SUITES}
        loaded_tests = {
            name: module
            for name, module in list(sys.modules.items())
            if name == "tests" or name.startswith("tests.")
        }
        for name in loaded_tests:
            del sys.modules[name]
        try:
            with (
                patch.object(sys, "dont_write_bytecode", True),
                redirect_stdout(StringIO()),
                patch("scripts.ci_test_shards.load_manifest", return_value=grouped),
            ):
                return run_suite_with_evidence(root, "unit", report_dir, CANDIDATE_SHA)
        finally:
            for name in list(sys.modules):
                if name == "tests" or name.startswith("tests."):
                    del sys.modules[name]
            sys.modules.update(loaded_tests)

    def _run_generated_suite(self, root: Path, suite: str, report_dir: Path, test_source: str) -> int:
        tests = root / "tests"
        tests.mkdir()
        (tests / "__init__.py").write_text("", encoding="utf-8")
        fixture_source = (Path(__file__).parent / "integration_setup.py").read_text(encoding="utf-8")
        (tests / "integration_setup.py").write_text(fixture_source, encoding="utf-8")
        (tests / "test_generated.py").write_text(test_source, encoding="utf-8")
        self._commit_checkout(root)

        grouped = {name: ["tests/test_generated.py"] for name in SUITES}
        loaded_tests = {
            name: module
            for name, module in list(sys.modules.items())
            if name == "tests" or name.startswith("tests.")
        }
        for name in loaded_tests:
            del sys.modules[name]
        try:
            with (
                patch.object(sys, "dont_write_bytecode", True),
                redirect_stdout(StringIO()),
                patch("scripts.ci_test_shards.load_manifest", return_value=grouped),
            ):
                return run_suite_with_evidence(root, suite, report_dir, CANDIDATE_SHA)
        finally:
            for name in list(sys.modules):
                if name == "tests" or name.startswith("tests."):
                    del sys.modules[name]
            sys.modules.update(loaded_tests)

    def _aggregate_generated_suite(self, evidence_dir: Path, suite: str) -> int:
        for name in SUITES:
            if name != suite:
                self._write_report(evidence_dir / name, self._evidence(name))
        with redirect_stdout(StringIO()):
            return aggregate_evidence(evidence_dir, "success")

    def test_live_manifest_partitions_every_top_level_test_once(self) -> None:
        root = Path(__file__).resolve().parents[1]

        grouped = load_manifest(root)

        declared = [path for suite in SUITES for path in grouped[suite]]
        discovered = sorted(path.relative_to(root).as_posix() for path in root.glob("tests/test_*.py"))
        self.assertEqual(sorted(declared), discovered)
        self.assertEqual(len(declared), len(set(declared)))
        self.assertTrue(all(grouped[suite] for suite in SUITES))

    def test_manifest_rejects_an_omission_duplicate_stale_path_and_unknown_suite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tests = root / "tests"
            tests.mkdir()
            manifest = tests / "ci-shards.txt"
            paths = {suite: f"tests/test_{number}.py" for number, suite in enumerate(SUITES)}
            for relative in paths.values():
                (root / relative).write_text("", encoding="utf-8")

            manifest.write_text(
                "\n".join(f"{suite} {relative}" for suite, relative in paths.items()) + "\n",
                encoding="utf-8",
            )
            (tests / "test_omitted.py").write_text("", encoding="utf-8")
            with self.assertRaisesRegex(ManifestError, "unclaimed tests: tests/test_omitted.py"):
                load_manifest(root, manifest)
            (tests / "test_omitted.py").unlink()

            manifest.write_text(
                manifest.read_text(encoding="utf-8") + f"unit {paths['unit']}\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ManifestError, "already belongs"):
                load_manifest(root, manifest)

            manifest.write_text(
                "\n".join(f"{suite} {relative}" for suite, relative in paths.items())
                + "\nunit tests/test_stale.py\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ManifestError, "missing declared tests: tests/test_stale.py"):
                load_manifest(root, manifest)

            manifest.write_text(
                "\n".join(f"{suite} {relative}" for suite, relative in paths.items())
                + "\nunsupported tests/test_unknown.py\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ManifestError, "unknown suite 'unsupported'"):
                load_manifest(root, manifest)

    def test_runner_executes_every_declared_suite(self) -> None:
        grouped = {suite: [f"tests/test_{number}.py"] for number, suite in enumerate(SUITES)}

        with (
            patch("scripts.ci_test_shards.load_manifest", return_value=grouped),
            patch("scripts.ci_test_shards.subprocess.call", return_value=0) as call,
        ):
            for suite in SUITES:
                self.assertEqual(main(["--suite", suite]), 0)
                call.assert_called_once()
                command = call.call_args.args[0]
                self.assertEqual(command[-1], modules(grouped[suite])[0])
                call.reset_mock()

    def test_runner_rejects_an_invalid_manifest_before_starting_a_suite(self) -> None:
        with (
            patch(
                "scripts.ci_test_shards.load_manifest",
                side_effect=ManifestError("unclaimed tests: tests/test_new.py"),
            ),
            patch("scripts.ci_test_shards.subprocess.call") as call,
            redirect_stderr(StringIO()),
        ):
            self.assertEqual(main(["--suite", "unit"]), 2)
            call.assert_not_called()

    def test_runner_rejects_an_undeclared_suite_name(self) -> None:
        with self.assertRaises(SystemExit) as error, redirect_stderr(StringIO()):
            main(["--suite", "unsupported"])
        self.assertEqual(error.exception.code, 2)

    def test_runner_propagates_a_suite_failure(self) -> None:
        grouped = {suite: ["tests/test_ci_shards.py"] for suite in SUITES}
        with (
            patch("scripts.ci_test_shards.load_manifest", return_value=grouped),
            patch("scripts.ci_test_shards.subprocess.call", return_value=1),
        ):
            self.assertEqual(main(["--suite", "unit"]), 1)

    def test_paths_become_unittest_module_names(self) -> None:
        self.assertEqual(modules(["tests/test_ci_shards.py"]), ["tests.test_ci_shards"])

    def test_fast_profile_is_a_fixed_narrow_hermetic_module_list(self) -> None:
        root = Path(__file__).resolve().parents[1]

        validate_fast_profile(root)

        self.assertEqual(
            FAST_MODULES,
            (
                "tests.test_hermetic_board",
                "tests.test_hermetic_pipeline_state",
            ),
        )

        runtime_component_modules = {
            "tests/test_local_pty_supervisor.py",
            "tests/test_local_pty_head_runtime.py",
            "tests/test_automations_dispatch_local_pty.py",
            "tests/test_runtime_deadline_contract.py",
        }
        grouped = load_manifest(root)
        self.assertTrue(runtime_component_modules.issubset(grouped["runtime-component"]))
        self.assertFalse(
            set(FAST_MODULES) & set(modules(sorted(runtime_component_modules))),
            "the fast profile must never run real local-PTY or runtime-deadline proofs",
        )

    def test_real_fast_profile_completes_under_its_own_process_and_network_guard(self) -> None:
        self.assertEqual(run_fast(Path(__file__).resolve().parents[1]), 0)

    def test_aggregate_rejects_each_non_success_typecheck_or_lint_result(self) -> None:
        root = Path(__file__).resolve().parents[1]
        workflow = yaml.safe_load((root / ".github/workflows/ci.yml").read_text())
        aggregate = workflow["jobs"]["test"]
        self.assertEqual(set(aggregate["needs"]), {"test_suites", "typecheck", "lint"})
        command = aggregate["steps"][-1]["run"]
        for typecheck in ("success", "failure", "cancelled", "skipped"):
            for lint in ("success", "failure", "cancelled", "skipped"):
                with self.subTest(typecheck=typecheck, lint=lint):
                    rendered = command.replace("${{ steps.coverage_aggregate.outcome }}", "success")
                    rendered = rendered.replace("${{ steps.suite_aggregate.outcome }}", "success")
                    rendered = rendered.replace("${{ needs.typecheck.result }}", typecheck)
                    rendered = rendered.replace("${{ needs.lint.result }}", lint)
                    result = subprocess.run(["bash", "-c", rendered], check=False)
                    self.assertEqual(result.returncode == 0, typecheck == lint == "success")

    def test_fast_profile_rejects_a_missing_declared_module_before_launch(self) -> None:
        root = Path(__file__).resolve().parents[1]

        with (
            patch("scripts.ci_test_shards.FAST_MODULES", ("tests.test_missing",)),
            self.assertRaisesRegex(ManifestError, "missing test module"),
        ):
            validate_fast_profile(root)

    def test_fast_action_skips_the_suite_manifest(self) -> None:
        with (
            patch("scripts.ci_test_shards.load_manifest") as manifest,
            patch("scripts.ci_test_shards.run_fast", return_value=0) as fast,
        ):
            self.assertEqual(main(["--fast"]), 0)

        manifest.assert_not_called()
        fast.assert_called_once()

    def test_fast_runner_passes_only_the_fixed_modules_to_its_child(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with patch("scripts.ci_test_shards.run_bounded", return_value=0) as bounded:
            self.assertEqual(run_fast(root), 0)

        command = bounded.call_args.args[0]
        self.assertEqual(command, [sys.executable, "-P", "-m", "unittest", "-v", *FAST_MODULES])

    def test_fast_environment_discards_ambient_credentials_and_installs_guards(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            fixture_root = Path(tmp)
            environment = fast_environment(root, fixture_root)
            for name in ("EXAMPLE_API_TOKEN", "OPENAI_API_KEY", "AWS_ACCESS_KEY_ID"):
                self.assertNotIn(name, environment)
            for name in (
                "HOME",
                "TA_CODEX_HOME",
                "TA_PIPELINE_STATE_DIR",
                "TEMP",
                "TMP",
                "TMPDIR",
                "XDG_CACHE_HOME",
                "XDG_CONFIG_HOME",
                "XDG_DATA_HOME",
            ):
                self.assertTrue(Path(environment[name]).is_relative_to(fixture_root), name)
            self.assertEqual(environment["PATH"], os.defpath)

            network = subprocess.run(
                [sys.executable, "-c", "import socket; socket.create_connection(('127.0.0.1', 1))"],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            command = subprocess.run(
                [sys.executable, "-c", "import subprocess; subprocess.run(['docker', 'info'])"],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertNotEqual(network.returncode, 0)
        self.assertIn("fast test profile forbids network access", network.stderr)
        self.assertNotEqual(command.returncode, 0)
        self.assertIn("fast test profile forbids external command execution", command.stderr)

    def test_fast_guard_allows_the_checkpoint_remote_read_but_refuses_config_writes(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            fixture_root = Path(tmp)
            repo = fixture_root / "instance"
            repo.mkdir()
            git(repo, "init", "--quiet")
            git(repo, "config", "remote.origin.url", "git@example.invalid:x/y.git")
            environment = fast_environment(root, fixture_root)
            for arguments, allowed in (
                (["--get", "remote.origin.url"], True),
                (["user.name", "changed"], False),
            ):
                with self.subTest(arguments=arguments):
                    script = (
                        "import subprocess; subprocess.run("
                        + repr(["git", "-C", str(repo), "config", *arguments])
                        + ", check=True)"
                    )
                    result = subprocess.run(
                        [sys.executable, "-c", script],
                        env=environment,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode == 0, allowed, result.stderr)
                    if allowed:
                        self.assertEqual(result.stdout.strip(), "git@example.invalid:x/y.git")
                    else:
                        self.assertIn("fast test profile forbids external command execution", result.stderr)

    def test_bounded_runner_stops_and_reaps_a_timed_out_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "child.pid"
            command = [
                sys.executable,
                "-c",
                (
                    "import os, time; from pathlib import Path; "
                    f"Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(60)"
                ),
            ]
            started = time.monotonic()
            with redirect_stderr(StringIO()) as stderr:
                result = run_bounded(
                    command,
                    root=Path(tmp),
                    environment=dict(os.environ),
                    timeout_seconds=0.5,
                )
            elapsed = time.monotonic() - started

            pid = int(pid_file.read_text(encoding="utf-8"))
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

        self.assertEqual(result, 124)
        self.assertLess(elapsed, 2)
        self.assertIn("timed out after 0.5 seconds; test child was stopped", stderr.getvalue())

    def test_workflow_publishes_and_preserves_reported_suite_evidence(self) -> None:
        workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text(
            encoding="utf-8"
        )

        for suite in SUITES:
            self.assertIn(suite, workflow)
        candidate_sha = "${{ github.event.pull_request.head.sha || github.sha }}"
        # The board migration proof archives the source before the new revision was added.
        suite_job = workflow.split("  typecheck:", 1)[0]
        self.assertIn(f"ref: {candidate_sha}\n", suite_job)
        self.assertIn("fetch-depth: 0", suite_job)
        self.assertIn(f'--candidate-sha "{candidate_sha}"', workflow)
        self.assertNotIn('--candidate-sha "$GITHUB_SHA"', workflow)
        self.assertIn(
            f"name: ci-evidence-${{{{ matrix.suite }}}}-{candidate_sha}",
            workflow,
        )
        self.assertIn(
            f"""      - name: Run reported suite
        id: run_suite
        continue-on-error: true
        run: >-
          mkdir -p "$RUNNER_TEMP/ci-coverage/${{{{ matrix.suite }}}}"
          &&
          python3 -m coverage run
          --data-file "$RUNNER_TEMP/ci-coverage/${{{{ matrix.suite }}}}/coverage.${{{{ matrix.suite }}}}"
          scripts/ci_test_shards.py --suite "${{{{ matrix.suite }}}}"
          --report-dir "$RUNNER_TEMP/ci-evidence/${{{{ matrix.suite }}}}"
          --candidate-sha "{candidate_sha}""",
            workflow,
        )
        self.assertIn(
            """      - name: Write suite evidence summary
        if: ${{ always() }}
        continue-on-error: true
        run: >-
          python3 scripts/ci_test_shards.py --summary
          --report-dir "$RUNNER_TEMP/ci-evidence/${{ matrix.suite }}"
          >> "$GITHUB_STEP_SUMMARY""",
            workflow,
        )
        self.assertIn(
            f"""      - name: Upload suite JUnit and bounded log
        if: ${{{{ always() }}}}
        continue-on-error: true
        uses: actions/upload-artifact@v4
        with:
          name: ci-evidence-${{{{ matrix.suite }}}}-{candidate_sha}
          path: ${{{{ runner.temp }}}}/ci-evidence/${{{{ matrix.suite }}}}
          if-no-files-found: error
          retention-days: 14""",
            workflow,
        )
        self.assertIn(
            """      - name: Preserve suite result
        if: ${{ always() }}
        run: >-
          python3 scripts/ci_test_shards.py --summary
          --report-dir "$RUNNER_TEMP/ci-evidence/${{ matrix.suite }}"
          > /dev/null""",
            workflow,
        )
        self.assertIn(
            f"""      - name: Upload raw suite coverage
        if: ${{{{ always() }}}}
        continue-on-error: true
        uses: actions/upload-artifact@v4
        with:
          name: ci-coverage-${{{{ matrix.suite }}}}-{candidate_sha}
          path: ${{{{ runner.temp }}}}/ci-coverage/${{{{ matrix.suite }}}}/coverage.${{{{ matrix.suite }}}}
          if-no-files-found: error""",
            workflow,
        )

    def test_workflow_aggregate_downloads_and_classifies_all_suite_evidence(self) -> None:
        workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            """  test:
    name: test
    if: ${{ always() }}
    needs: [test_suites, typecheck, lint]""",
            workflow,
        )
        self.assertIn(
            """      - name: Download suite evidence
        if: ${{ always() }}
        continue-on-error: true
        uses: actions/download-artifact@v4
        with:
          pattern: ci-evidence-*
          path: ${{ runner.temp }}/ci-evidence
          merge-multiple: false""",
            workflow,
        )
        self.assertIn(
            """      - name: Require and classify every test suite
        id: suite_aggregate
        if: ${{ always() }}
        continue-on-error: true
        env:
          SUITES_RESULT: ${{ needs.test_suites.result }}
        run: >-
          python3 scripts/ci_test_shards.py --aggregate
          --evidence-dir "$RUNNER_TEMP/ci-evidence"
          --needs-result "$SUITES_RESULT"
          >> "$GITHUB_STEP_SUMMARY""",
            workflow,
        )
        self.assertIn("pattern: ci-coverage-*", workflow)
        self.assertIn("--coverage-aggregate", workflow)
        self.assertIn('--base-sha "$BASE_SHA"', workflow)
        self.assertIn(
            "name: ci-coverage-combined-${{ github.event.pull_request.head.sha || github.sha }}", workflow
        )
        self.assertIn("name: ci-coverage-baseline-${{ github.sha }}", workflow)
        self.assertIn("## Main coverage baseline", workflow)
        self.assertIn("fetch-depth: 0", workflow)

    def test_main_coverage_baseline_summary_preserves_sha_and_artifact(self) -> None:
        workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text(
            encoding="utf-8"
        )
        step = workflow.split("      - name: Write main coverage baseline summary\n", 1)[1].split(
            "      - name:", 1
        )[0]
        self.assertIn("BASELINE_SHA: ${{ github.sha }}", step)
        self.assertIn("BASELINE_ARTIFACT: ci-coverage-baseline-${{ github.sha }}", step)
        command = " ".join(
            line.strip() for line in step.split("        run: >-\n", 1)[1].splitlines() if line.strip()
        ).replace("${{ github.sha }}", CANDIDATE_SHA)
        artifact = f"ci-coverage-baseline-{CANDIDATE_SHA}"

        with tempfile.TemporaryDirectory() as tmp:
            summary = Path(tmp) / "summary.md"
            result = subprocess.run(
                ["bash", "-e", "-c", command],
                env={
                    **os.environ,
                    "GITHUB_STEP_SUMMARY": str(summary),
                    "BASELINE_SHA": CANDIDATE_SHA,
                    "BASELINE_ARTIFACT": artifact,
                },
                check=False,
                text=True,
                capture_output=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                summary.read_text(encoding="utf-8"),
                f"## Main coverage baseline\n\n- Candidate SHA: `{CANDIDATE_SHA}`\n- Artifact: `{artifact}`\n",
            )

    def _coverage_payload(self) -> dict[str, object]:
        return {
            "meta": {"branch_coverage": True},
            "files": {
                "src/ummanu/example.py": {
                    "executed_lines": [2, 4],
                    "missing_lines": [3],
                    "excluded_lines": [5],
                    "executed_branches": [[2, 4]],
                    "missing_branches": [[2, 3]],
                    "summary": {"num_branches": 2},
                }
            },
        }

    def test_the_published_coverage_keeps_lines_and_branches_and_leaves_out_derived_regions(self) -> None:
        """coverage.py's functions/classes regions pushed the full product past the bound (secretary-1631)."""
        native = self._coverage_payload()
        region = {"executed_lines": [2, 4], "missing_lines": [3], "summary": {"num_statements": 3}}
        entry = native["files"]["src/ummanu/example.py"]
        entry["functions"] = {f"f{index}": region for index in range(400)}
        entry["classes"] = {"": region}
        native_text = json.dumps(native)
        bound = len(native_text) // 2
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "raw"
            for suite in SUITES:
                path = raw / f"ci-coverage-{suite}-{CANDIDATE_SHA}" / f"coverage.{suite}"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"coverage data")
            output = root / "combined"

            def write_native_json(command: list[str], _root: Path) -> None:
                if "json" in command:
                    Path(command[command.index("-o") + 1]).write_text(native_text, encoding="utf-8")

            with (
                patch("scripts.ci_test_shards._candidate_checkout"),
                patch("scripts.ci_test_shards._validate_coverage_datum"),
                patch("scripts.ci_test_shards._run_coverage", side_effect=write_native_json),
                patch("scripts.ci_test_shards.MAX_COVERAGE_JSON_BYTES", bound),
                redirect_stdout(StringIO()),
            ):
                self.assertEqual(aggregate_coverage(root, raw, output, CANDIDATE_SHA, None), 0)

            combined_path = output / COVERAGE_JSON_NAME
            published = json.loads(combined_path.read_text(encoding="utf-8"))
            self.assertLessEqual(combined_path.stat().st_size, bound)

        self.assertEqual(
            published["coverage"]["files"]["src/ummanu/example.py"],
            self._coverage_payload()["files"]["src/ummanu/example.py"],
        )

    def test_coverage_aggregate_combines_one_named_datum_per_suite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "raw"
            for suite in SUITES:
                path = raw / f"ci-coverage-{suite}-{CANDIDATE_SHA}" / f"coverage.{suite}"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"coverage data")
            output = root / "combined"

            commands: list[list[str]] = []

            def write_native_json(command: list[str], _root: Path) -> None:
                commands.append(command)
                if "json" in command:
                    Path(command[command.index("-o") + 1]).write_text(
                        json.dumps(self._coverage_payload()), encoding="utf-8"
                    )

            with (
                patch("scripts.ci_test_shards._candidate_checkout"),
                patch("scripts.ci_test_shards._validate_coverage_datum"),
                patch("scripts.ci_test_shards._run_coverage", side_effect=write_native_json),
            ):
                self.assertEqual(aggregate_coverage(root, raw, output, CANDIDATE_SHA, None), 0)

            combined_path = output / COVERAGE_JSON_NAME
            combined_text = combined_path.read_text(encoding="utf-8")
            combined_size = combined_path.stat().st_size
            combined = json.loads(combined_text)
            changed = json.loads((output / CHANGED_LINES_JSON_NAME).read_text(encoding="utf-8"))

        self.assertEqual(combined["candidate_sha"], CANDIDATE_SHA)
        self.assertEqual(combined["source_roots"], ["src/ummanu"])
        self.assertIn("executed_branches", combined["coverage"]["files"]["src/ummanu/example.py"])
        self.assertEqual(
            combined_text,
            json.dumps(combined, sort_keys=True, separators=(",", ":")) + "\n",
        )
        self.assertLessEqual(combined_size, MAX_COVERAGE_JSON_BYTES)
        self.assertFalse(changed["applicable"])
        self.assertIn("no pull-request base SHA", changed["reason"])
        combine = next(command for command in commands if "combine" in command)
        data_file = Path(combine[combine.index("--data-file") + 1])
        self.assertEqual(data_file.name, "coverage")
        raw_paths = [Path(path) for path in combine[-len(SUITES) :]]
        self.assertEqual({path.name for path in raw_paths}, {f"coverage.{suite}" for suite in SUITES})
        self.assertTrue(all(path.name.startswith(f"{data_file.name}.") for path in raw_paths))

    def test_coverage_aggregate_rejects_missing_or_uncombinable_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "raw"
            raw.mkdir()
            with self.assertRaisesRegex(CoverageError, "unit"):
                _suite_coverage_data(raw, CANDIDATE_SHA)
            for suite in SUITES:
                path = raw / f"ci-coverage-{suite}-{CANDIDATE_SHA}" / f"coverage.{suite}"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"coverage data")
            with (
                patch("scripts.ci_test_shards._candidate_checkout"),
                patch("scripts.ci_test_shards._validate_coverage_datum"),
                patch("scripts.ci_test_shards._run_coverage", side_effect=CoverageError("incompatible data")),
            ):
                self.assertEqual(aggregate_coverage(root, raw, root / "combined", CANDIDATE_SHA, None), 3)

    def test_coverage_aggregate_rejects_a_corrupt_datum_before_combine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "raw"
            for suite in SUITES:
                path = raw / f"ci-coverage-{suite}-{CANDIDATE_SHA}" / f"coverage.{suite}"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"coverage data")
            with (
                patch("scripts.ci_test_shards._candidate_checkout"),
                patch(
                    "scripts.ci_test_shards._validate_coverage_datum",
                    side_effect=CoverageError("corrupt coverage data"),
                ),
                patch("scripts.ci_test_shards._run_coverage") as coverage,
            ):
                self.assertEqual(aggregate_coverage(root, raw, root / "combined", CANDIDATE_SHA, None), 3)

        coverage.assert_not_called()

    def test_changed_line_report_classifies_coverage_and_non_executable_lines(self) -> None:
        report = _changed_line_report(
            self._coverage_payload(),
            {"src/ummanu/example.py": [2, 3, 5, 9]},
            base_sha="b" * 40,
            candidate_sha=CANDIDATE_SHA,
        )

        self.assertTrue(report["applicable"])
        self.assertEqual(
            [entry["classification"] for entry in report["lines"]],
            ["covered", "missed", "excluded", "not_executable"],
        )

    def test_a_package_moved_into_the_source_root_reports_only_the_lines_it_changed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            before = root / "src" / "before"
            before.mkdir(parents=True)
            body = "".join(f"VALUE_{number} = {number}\n" for number in range(40))
            (before / "module.py").write_text(body, encoding="utf-8")
            (root / "README.md").write_text("fixture\n", encoding="utf-8")
            self._commit_checkout(root)
            base = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
            ).stdout.strip()
            subprocess.run(["git", "-C", str(root), "mv", "src/before", "src/ummanu"], check=True)
            moved = root / "src" / "ummanu" / "module.py"
            moved.write_text(body.replace("VALUE_7 = 7", "VALUE_7 = 70"), encoding="utf-8")
            (root / "src" / "ummanu" / "added.py").write_text("ADDED = 1\nMORE = 2\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "--quiet", "-m", "move"], check=True)
            candidate = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
            ).stdout.strip()

            changed = _changed_candidate_lines(root, base, candidate)

        self.assertEqual(changed, {"src/ummanu/module.py": [8], "src/ummanu/added.py": [1, 2]})

    def test_coverage_configuration_enables_branch_scope_without_a_threshold(self) -> None:
        config = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")

        self.assertIn("[tool.coverage.run]", config)
        self.assertIn("branch = true", config)
        self.assertIn("relative_files = true", config)
        self.assertIn('source = ["src/ummanu"]', config)
        self.assertNotIn("fail_under", config)

    def _write_report(self, directory: Path, evidence: SuiteEvidence) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        log = BoundedTee(StringIO(), directory / "test-output.log")
        log.write("test output\n")
        _write_evidence(directory, evidence, log)

    def _evidence(self, suite: str, outcome: str = "success") -> SuiteEvidence:
        return SuiteEvidence(
            suite,
            CANDIDATE_SHA,
            outcome,
            {
                "collected": 3,
                "passed": 2,
                "failed": 1 if outcome == "product_failure" else 0,
                "error": 0,
                "skipped": 0,
            },
            1.25,
            [TestRecord("tests.example.Case.test_slow", "tests.example.Case", "test_slow", 1.0)],
            ["tests.example.Case.test_slow: tests/example.py:12"] if outcome == "product_failure" else [],
        )

    def test_reported_runner_writes_exact_paths_and_product_failure_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module = root / "sample_suite.py"
            module.write_text(
                "import unittest\n"
                "class Sample(unittest.TestCase):\n"
                "    def test_pass(self): self.assertTrue(True)\n"
                "    def test_fail(self): self.fail('broken')\n",
                encoding="utf-8",
            )
            report_dir = root / "artifacts" / "unit"
            report_dir.mkdir(parents=True)
            sys.path.insert(0, str(root))
            try:
                with patch("scripts.ci_test_shards.modules", return_value=["sample_suite"]):
                    log = BoundedTee(StringIO(), report_dir / "test-output.log")
                    evidence = run_reported_suite("unit", ["ignored"], CANDIDATE_SHA, log)
                    _write_evidence(report_dir, evidence, log)
            finally:
                sys.path.remove(str(root))
                sys.modules.pop("sample_suite", None)

            self.assertEqual(evidence.outcome, "product_failure")
            self.assertEqual(
                evidence.counts, {"collected": 2, "passed": 1, "failed": 1, "error": 0, "skipped": 0}
            )
            self.assertEqual({path.name for path in report_dir.iterdir()}, set(EVIDENCE_FILES))
            self.assertEqual(_read_evidence(report_dir).candidate_sha, CANDIDATE_SHA)
            junit = ElementTree.parse(report_dir / "junit.xml").getroot()
            self.assertEqual(junit.attrib["failures"], "1")
            self.assertEqual(len(junit.findall("testcase")), 2)
            self.assertIn("sample_suite.Sample.test_fail", evidence.failure_locations[0])

    def test_reported_runner_writes_success_evidence_and_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temporary = Path(tmp)
            root = temporary / "checkout"
            root.mkdir()
            tests = root / "tests"
            tests.mkdir()
            (tests / "__init__.py").write_text("", encoding="utf-8")
            (tests / "test_passing.py").write_text(
                "import unittest\n"
                "class Passing(unittest.TestCase):\n"
                "    def test_pass(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            self._commit_checkout(root)
            report_dir = temporary / "runner-temp" / "unit"
            self.assertEqual(self._run_temporary_suite(root, report_dir), 0)

            summary = StringIO()
            with redirect_stdout(summary):
                self.assertEqual(report_summary(report_dir), 0)
            evidence = _read_evidence(report_dir)

        self.assertEqual(evidence.outcome, "success")
        self.assertIsNotNone(evidence.checkout_status)
        self.assertFalse(evidence.checkout_status.changed)
        self.assertIn("Candidate SHA", summary.getvalue())
        self.assertIn("collected 1, passed 1", summary.getvalue())
        self.assertIn("Checkout status: unchanged", summary.getvalue())

    def test_missing_memory_setup_is_infrastructure_failure_through_aggregate(self) -> None:
        source = (
            "import unittest\n"
            "from tests.integration_setup import require_integration_setup\n"
            "class MemoryAcceptance(unittest.TestCase):\n"
            "    @classmethod\n"
            "    def setUpClass(cls):\n"
            "        require_integration_setup(None, 'ummanu[memory] is not installed')\n"
            "    def test_scope(self): pass\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "checkout"
            root.mkdir()
            evidence_dir = Path(tmp) / "evidence"
            report_dir = evidence_dir / "integration-memory"
            self.assertEqual(self._run_generated_suite(root, "integration-memory", report_dir, source), 3)
            evidence = _read_evidence(report_dir)
            summary = StringIO()
            with redirect_stdout(summary):
                self.assertEqual(report_summary(report_dir), 3)
            self.assertEqual(self._aggregate_generated_suite(evidence_dir, "integration-memory"), 3)

            junit = ElementTree.parse(report_dir / "junit.xml").getroot()
            log = (report_dir / "test-output.log").read_text(encoding="utf-8")

        self.assertEqual(evidence.outcome, "infrastructure_failure")
        self.assertEqual(evidence.counts["skipped"], 0)
        self.assertEqual(evidence.counts["error"], 1)
        self.assertEqual(junit.attrib["errors"], "1")
        self.assertIn("ummanu[memory] is not installed", evidence.detail)
        self.assertIn("required integration setup unavailable", summary.getvalue())
        self.assertIn("ummanu[memory] is not installed", log)

    def test_unavailable_disposable_board_fixture_is_infrastructure_failure_through_aggregate(self) -> None:
        source = (
            "import unittest\n"
            "from tests.integration_setup import require_disposable_board_fixture\n"
            "class BoardIntegration(unittest.TestCase):\n"
            "    @classmethod\n"
            "    def setUpClass(cls):\n"
            "        require_disposable_board_fixture(None)\n"
            "    def test_board(self): pass\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "checkout"
            root.mkdir()
            evidence_dir = Path(tmp) / "evidence"
            report_dir = evidence_dir / "integration-board"
            self.assertEqual(self._run_generated_suite(root, "integration-board", report_dir, source), 3)
            evidence = _read_evidence(report_dir)
            self.assertEqual(self._aggregate_generated_suite(evidence_dir, "integration-board"), 3)

        self.assertEqual(evidence.outcome, "infrastructure_failure")
        self.assertEqual(evidence.counts["skipped"], 0)
        self.assertIn("required disposable-board fixture is unavailable", evidence.detail)

    def test_intentional_skip_remains_a_success_through_aggregate(self) -> None:
        source = (
            "import unittest\n"
            "class IntentionalSkip(unittest.TestCase):\n"
            "    @unittest.skip('intentional control skip')\n"
            "    def test_skip(self): pass\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "checkout"
            root.mkdir()
            evidence_dir = Path(tmp) / "evidence"
            report_dir = evidence_dir / "integration-memory"
            self.assertEqual(self._run_generated_suite(root, "integration-memory", report_dir, source), 0)
            evidence = _read_evidence(report_dir)
            self.assertEqual(self._aggregate_generated_suite(evidence_dir, "integration-memory"), 0)

        self.assertEqual(evidence.outcome, "success")
        self.assertEqual(evidence.counts["skipped"], 1)

    def test_product_failure_remains_a_product_failure_through_aggregate(self) -> None:
        source = (
            "import unittest\n"
            "class ProductFailure(unittest.TestCase):\n"
            "    def test_failure(self): self.fail('product control failure')\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "checkout"
            root.mkdir()
            evidence_dir = Path(tmp) / "evidence"
            report_dir = evidence_dir / "integration-board"
            self.assertEqual(self._run_generated_suite(root, "integration-board", report_dir, source), 1)
            evidence = _read_evidence(report_dir)
            self.assertEqual(self._aggregate_generated_suite(evidence_dir, "integration-board"), 1)

        self.assertEqual(evidence.outcome, "product_failure")
        self.assertEqual(evidence.counts["failed"], 1)

    def test_reported_runner_detects_a_tracked_checkout_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temporary = Path(tmp)
            root = temporary / "checkout"
            root.mkdir()
            tests = root / "tests"
            tests.mkdir()
            (tests / "__init__.py").write_text("", encoding="utf-8")
            (root / "tracked.txt").write_text("before\n", encoding="utf-8")
            (tests / "test_passing.py").write_text(
                "import unittest\n"
                "from pathlib import Path\n"
                "class Passing(unittest.TestCase):\n"
                "    def test_pass(self):\n"
                "        Path('tracked.txt').write_text('after\\n', encoding='utf-8')\n",
                encoding="utf-8",
            )
            self._commit_checkout(root)

            report_dir = temporary / "runner-temp" / "unit"
            self.assertEqual(self._run_temporary_suite(root, report_dir), 3)
            evidence = _read_evidence(report_dir)

        self.assertEqual(evidence.outcome, "infrastructure_failure")
        self.assertIsNotNone(evidence.checkout_status)
        self.assertTrue(evidence.checkout_status.changed)
        self.assertTrue(any("tracked.txt" in entry for entry in evidence.checkout_status.changed_entries))

    def test_reported_runner_detects_an_untracked_product_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temporary = Path(tmp)
            root = temporary / "checkout"
            root.mkdir()
            tests = root / "tests"
            tests.mkdir()
            (tests / "__init__.py").write_text("", encoding="utf-8")
            (tests / "test_passing.py").write_text(
                "import unittest\n"
                "from pathlib import Path\n"
                "class Passing(unittest.TestCase):\n"
                "    def test_pass(self):\n"
                "        Path('product-artifact.txt').write_text('artifact\\n', encoding='utf-8')\n",
                encoding="utf-8",
            )
            self._commit_checkout(root)

            report_dir = temporary / "runner-temp" / "unit"
            self.assertEqual(self._run_temporary_suite(root, report_dir), 3)
            evidence = _read_evidence(report_dir)
            summary = _summary(evidence)

        self.assertEqual(evidence.outcome, "infrastructure_failure")
        self.assertEqual(evidence.counts["passed"], 1)
        self.assertIsNotNone(evidence.checkout_status)
        self.assertTrue(evidence.checkout_status.changed)
        self.assertTrue(
            any("product-artifact.txt" in entry for entry in evidence.checkout_status.changed_entries)
        )
        self.assertIn("Checkout status: changed", summary)
        self.assertIn("product-artifact.txt", summary)

    def test_checkout_contamination_overrides_a_product_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            temporary = Path(tmp)
            root = temporary / "checkout"
            root.mkdir()
            tests = root / "tests"
            tests.mkdir()
            (tests / "__init__.py").write_text("", encoding="utf-8")
            (tests / "test_passing.py").write_text(
                "import unittest\n"
                "from pathlib import Path\n"
                "class Failing(unittest.TestCase):\n"
                "    def test_failure(self):\n"
                "        Path('product-artifact.txt').write_text('artifact\\n', encoding='utf-8')\n"
                "        self.fail('product failure')\n",
                encoding="utf-8",
            )
            self._commit_checkout(root)

            report_dir = temporary / "runner-temp" / "unit"
            self.assertEqual(self._run_temporary_suite(root, report_dir), 3)
            evidence = _read_evidence(report_dir)

        self.assertEqual(evidence.outcome, "infrastructure_failure")
        self.assertIn("test outcome before checkout verification: product_failure", evidence.detail)
        self.assertTrue(any("test_failure" in location for location in evidence.failure_locations))

    def test_unavailable_checkout_status_is_an_infrastructure_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "checkout"
            root.mkdir()
            tests = root / "tests"
            tests.mkdir()
            (tests / "__init__.py").write_text("", encoding="utf-8")
            (tests / "test_passing.py").write_text("", encoding="utf-8")
            self._commit_checkout(root)

            report_dir = Path(tmp) / "runner-temp" / "unit"
            with patch(
                "scripts.ci_test_shards._checkout_snapshot",
                side_effect=CheckoutStatusError("git status command failed"),
            ):
                self.assertEqual(self._run_temporary_suite(root, report_dir), 3)
            evidence = _read_evidence(report_dir)

        self.assertEqual(evidence.outcome, "infrastructure_failure")
        self.assertIn("checkout status unavailable before suite execution", evidence.failure_locations)

    def test_bounded_log_keeps_a_marker_after_its_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test-output.log"
            log = BoundedTee(StringIO(), path, maximum_bytes=5)
            log.write("abcdefgh")
            log.close()
            content = path.read_text(encoding="utf-8")

        self.assertTrue(log.truncated)
        self.assertTrue(content.startswith("abcde"))
        self.assertIn("truncated at 1000000 bytes", content)

    def test_reported_manifest_or_report_failure_is_infrastructure_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report_dir = Path(tmp) / "report"
            with (
                redirect_stdout(StringIO()),
                patch("scripts.ci_test_shards.load_manifest", side_effect=ManifestError("bad manifest")),
            ):
                self.assertEqual(run_suite_with_evidence(Path(tmp), "unit", report_dir, CANDIDATE_SHA), 3)

            evidence = _read_evidence(report_dir)
            self.assertEqual(evidence.outcome, "infrastructure_failure")
            (report_dir / "junit.xml").unlink()
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                self.assertEqual(report_summary(report_dir), 3)

    def test_cancelled_run_is_recorded_as_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = BoundedTee(StringIO(), Path(tmp) / "test-output.log")
            with patch("scripts.ci_test_shards.unittest.TextTestRunner.run", side_effect=KeyboardInterrupt):
                evidence = run_reported_suite("unit", ["tests/test_any.py"], CANDIDATE_SHA, log)
            log.close()

        self.assertEqual(evidence.outcome, "cancelled")

    def test_aggregate_separates_product_infrastructure_cancellation_and_not_applicable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for suite in SUITES:
                self._write_report(root / suite, self._evidence(suite))
            self._write_report(root / "unit", self._evidence("unit", "product_failure"))
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                self.assertEqual(aggregate_evidence(root, "failure"), 1)

            (root / "component" / "report.json").unlink()
            with redirect_stdout(StringIO()):
                self.assertEqual(aggregate_evidence(root, "failure"), 3)
            for suite in SUITES:
                report = root / suite / "report.json"
                if report.exists():
                    report.unlink()
            with redirect_stdout(StringIO()):
                self.assertEqual(aggregate_evidence(root, "cancelled"), 130)

            with redirect_stdout(StringIO()):
                self.assertEqual(aggregate_evidence(root, "skipped"), 0)


if __name__ == "__main__":
    unittest.main()
