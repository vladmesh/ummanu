"""secretary-1406: a broad check must leave structured evidence behind.

The failure these tests pin is cheap to describe and expensive to repeat: a worker runs the broad
suite, the pane scrolls, and the only way back to "did it pass, and how many tests" is another
ninety-second run over code that did not change. The receipt is that answer, so the interesting
cases are all the ways it could quietly lie — a truncated artifact, a killed run, a receipt written
before the last edit — and every one of them has to read as "not usable" rather than as a summary.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from io import StringIO
from pathlib import Path
from signal import NSIG
from unittest import mock

from tests.support.broad_check_fixture import (
    BroadCheckTestCase,
    _documents,
    _run_main,
    _status,
)
from ummanu import broad_check
from ummanu.broad_check import (
    BroadCheckError,
    CheckSpec,
    RunResult,
    load_receipt,
    parse_unittest_summary,
    receipt_path,
    run_broad_check,
    usable_receipt,
)
from ummanu.cli import main


class TimingReceiptTests(BroadCheckTestCase):
    def test_native_load_tests_failure_or_error_retains_before_after_timings_in_receipt_and_show(self):
        from types import SimpleNamespace

        from tests.support.native_timing import native_outcome_modules
        from ummanu.check_commands import run_check_show
        from ummanu.projects.test_timing import TimingRunner, valid_observation

        for kind, outcome in (("load_failure", "failed"), ("load_error", "error")):
            with self.subTest(kind=kind):
                now = [0.0]
                selected = native_outcome_modules(now, kind)
                sources = [module.__name__ for module in selected]
                output = StringIO()
                with (mock.patch.dict(sys.modules, {m.__name__: m for m in selected}),
                      mock.patch("time.monotonic", side_effect=lambda clock=now: clock[0])):
                    loader = unittest.TestLoader()
                    suites = [loader.loadTestsFromModule(module) for module in selected]
                    self.assertIsInstance(next(iter(suites[1])), unittest.loader._FailedTest)
                    result = TimingRunner(stream=output, source_modules=sources).run(unittest.TestSuite(suites))
                observation = json.loads(json.dumps(result.observation()))
                self.assertTrue(valid_observation(observation))
                self.assertFalse(result.wasSuccessful())
                self.assertEqual(observation["native_outcomes"][0]["outcome"], outcome)
                self.assertEqual([r["duration_seconds"] for r in observation["tests"]], [1, 2])
                # Project the real producer's sidecar and native output through the
                # existing pure receipt fixture, keeping its original rc1.
                suite = self._suite(kind,
                    "import json, os, sys\nfrom pathlib import Path\n"
                    f"Path(os.environ['UMMANU_TEST_TIMING_RECORD']).write_text(json.dumps({observation!r}))\n"
                    f"print({output.getvalue()!r})\nsys.exit(1)\n")
                code, receipt = self._run(suite)
                self.assertEqual(code, 1)
                self.assertEqual(receipt["verdict"], "failed")
                self.assertEqual(receipt["parsed"]["timing"], observation)
                self.assertEqual(receipt["parsed"]["tests"], 3)
                self.assertEqual(receipt["parsed"]["failures" if outcome == "failed" else "errors"], 1)
                path = receipt_path(self.root, suite)
                original_bytes = path.read_bytes()
                shown = load_receipt(path)
                self.assertEqual(shown["parsed"]["timing"], observation)
                self.assertTrue(usable_receipt(self.root, suite).usable)
                # check show uses the same validated receipt and summary; the CLI
                # dispatch and reuse predicate have existing independent regressions.
                rendered = broad_check.summarize(shown)
                for record in observation["tests"]:
                    self.assertIn(record["identifier"], rendered)
                    self.assertIn(f"{record['duration_seconds']:.6f}s", rendered)
                self.assertIn(f"native {outcome} {sources[1]} load", rendered)
                shown_output = StringIO()
                with (mock.patch("ummanu.check_commands._spec",
                                 return_value=SimpleNamespace(spec=suite, module_contract=None)),
                      mock.patch("sys.stdout", shown_output)):
                    self.assertEqual(run_check_show(SimpleNamespace(root=str(self.root))), 0)
                shown_payload = json.loads(shown_output.getvalue())
                self.assertEqual(shown_payload["receipt"]["exit_code"], 1)
                self.assertEqual(shown_payload["receipt"]["parsed"]["timing"], observation)
                self.assertEqual(shown_payload["summary"], rendered)
                self.assertEqual(path.read_bytes(), original_bytes)

    def test_native_fixture_and_loader_observations_retain_receipt_verdict_and_measurements(self):
        from types import ModuleType

        from ummanu.projects.test_timing import TimingRunner, valid_observation

        for phase in ("setUpClass", "setUpModule", "load"):
            for kind in ("skip", "error"):
                with self.subTest(phase=phase, kind=kind):
                    now = [0.0]
                    module = ModuleType("tests.receipt_fixture")

                    class Case(unittest.TestCase):
                        def test_body(self, clock=now):
                            clock[0] += 1

                    Case.__module__, Case.__qualname__ = module.__name__, "Case"
                    module.Case = Case

                    def fixture(clock=now, outcome=kind):
                        clock[0] += 2
                        raise unittest.SkipTest("fixture skip") if outcome == "skip" else ValueError("fixture error")

                    if phase == "setUpClass":
                        Case.setUpClass = classmethod(lambda cls, fixture=fixture: fixture())
                    elif phase == "setUpModule":
                        module.setUpModule = fixture
                    else:
                        # Loader errors retain prior valid timings; import skips use the
                        # stdlib loader's own synthetic skipped class.
                        exc = unittest.SkipTest("loader skip") if kind == "skip" else ValueError("loader error")
                        if kind == "skip":
                            failed = unittest.loader._make_skipped_test("broken", exc, unittest.TestSuite)
                        else:
                            failed = unittest.TestSuite([unittest.loader._FailedTest("broken", exc)])
                    output = StringIO()
                    with (mock.patch.dict(sys.modules, {module.__name__: module}),
                          mock.patch("time.monotonic", side_effect=lambda clock=now: clock[0])):
                        suites = [unittest.TestLoader().loadTestsFromModule(module)]
                        sources = [module.__name__]
                        if phase == "load":
                            suites.append(failed)
                            sources.append("tests.broken_fixture")
                        result = TimingRunner(stream=output, source_modules=sources).run(unittest.TestSuite(suites))
                    observation = result.observation()
                    self.assertTrue(valid_observation(observation))
                    self.assertEqual(observation["status"], "complete")
                    self.assertEqual(len(observation["tests"]), int(phase == "load"))
                    self.assertEqual(observation["native_outcomes"][0]["phase"], phase)
                    code = 0 if result.wasSuccessful() else 1
                    suite = self._suite(f"native{phase}{kind}",
                        "import json, os, sys\nfrom pathlib import Path\n"
                        f"Path(os.environ['UMMANU_TEST_TIMING_RECORD']).write_text(json.dumps({observation!r}))\n"
                        f"print({output.getvalue()!r})\nsys.exit({code})\n")
                    actual, receipt = self._run(suite)
                    self.assertEqual(actual, code)
                    self.assertEqual(receipt["verdict"], "passed" if code == 0 else "failed")
                    self.assertEqual(receipt["parsed"]["tests"], result.testsRun)
                    self.assertEqual(receipt["parsed"]["timing"], observation)
                    self.assertTrue(usable_receipt(self.root, suite).usable)
                    self.assertIn(observation["native_outcomes"][0]["identifier"], broad_check.summarize(receipt))

    def test_partial_native_interrupt_is_written_by_local_main_and_retained_in_receipt(self):
        from tests import broad
        from ummanu.projects.test_timing import valid_observation

        now = [0.0]

        class Case(unittest.TestCase):
            def test_a(self):
                now[0] += 1

            def test_b(self):
                now[0] += 2
                raise KeyboardInterrupt()

        def native_main(**kwargs):
            kwargs["testRunner"]().run(unittest.TestLoader().loadTestsFromTestCase(Case))

        timing_path = self.root / "native-interrupted.json"
        with (mock.patch.dict(os.environ, {"UMMANU_TEST_TIMING_RECORD": str(timing_path)}),
              mock.patch("time.monotonic", side_effect=lambda: now[0]),
              mock.patch("tests.broad.unittest.main", side_effect=native_main),
              mock.patch("sys.stderr", StringIO()), self.assertRaises(KeyboardInterrupt)):
            broad.main(["tests.test_broad_check"])
        observation = json.loads(timing_path.read_text())
        self.assertTrue(valid_observation(observation))
        self.assertEqual(observation["status"], "incomplete")
        self.assertEqual([r["duration_seconds"] for r in observation["tests"]], [1, 2])
        # Feed the actual producer's sidecar through the existing receipt fixture;
        # no installed full-profile activation or delivery is simulated here.
        suite = self._suite("partialnative",
            "import json, os, sys\nfrom pathlib import Path\n"
            f"Path(os.environ['UMMANU_TEST_TIMING_RECORD']).write_text(json.dumps({observation!r}))\n"
            "sys.exit(130)\n")
        code, receipt = self._run(suite)
        self.assertEqual(code, 130)
        self.assertEqual(receipt["verdict"], "failed")
        self.assertEqual(receipt["parsed"]["timing"], observation)
        self.assertTrue(usable_receipt(self.root, suite).usable)

    def test_source_hints_preserve_native_verbosity_failfast_and_buffer_options(self):
        from tests import broad

        class Case(unittest.TestCase):
            def test_a(self):
                print("native buffered output")
                self.fail("native failure")

            def test_b(self):
                self.fail("failfast must stop before this test")

        timing_path = self.root / "flags.json"
        output = StringIO()
        with (mock.patch.dict(os.environ, {"UMMANU_TEST_TIMING_RECORD": str(timing_path)}),
              mock.patch.object(unittest.defaultTestLoader, "loadTestsFromNames",
                                return_value=unittest.TestLoader().loadTestsFromTestCase(Case)),
              mock.patch("sys.stderr", output), mock.patch("sys.stdout", StringIO())):
            code = broad.main(["-q", "-f", "-b", "tests.test_broad_check"])
        observation = json.loads(timing_path.read_text())
        self.assertEqual(code, 1)
        self.assertEqual(len(observation["tests"]), 1)
        self.assertEqual(observation["status"], "incomplete")
        self.assertIn("Stdout:\nnative buffered output", output.getvalue())
        self.assertNotIn("failfast must stop", output.getvalue())
        self.assertFalse(output.getvalue().startswith("F"))

    def test_timings_survive_truncation_reuse_original_status_and_digest(self):
        observation = {"status": "complete", "tests": [
            {"identifier": "tests.fixture.Case.test_a", "module": "tests.fixture",
             "duration_seconds": 6.0, "outcome": "passed"},
            {"identifier": "tests.fixture.Case.test_b", "module": "tests.fixture",
             "duration_seconds": 7.0, "outcome": "failed"},
        ], "modules": {"tests.fixture": 13.0}}
        for code in (0, 1):
            suite = self._suite(f"timed{code}",
                "import json, os, sys\nfrom pathlib import Path\n"
                f"Path(os.environ['UMMANU_TEST_TIMING_RECORD']).write_text(json.dumps({observation!r}))\n"
                "print('x' * 20000)\n" + f"sys.exit({code})\n")
            actual, receipt = self._run(suite)
            self.assertEqual(actual, code)
            self.assertEqual(receipt["verdict"], "passed" if code == 0 else "failed")
            self.assertTrue(receipt["tail_truncated"])
            self.assertEqual(receipt["parsed"]["timing"], observation)
            lookup = usable_receipt(self.root, suite)
            self.assertTrue(lookup.usable)
            rendered = broad_check.summarize(lookup.receipt)
            self.assertIn("WARNING: timing budget test tests.fixture.Case.test_a: 6.000000s > 5s", rendered)
            self.assertIn("WARNING: timing budget test tests.fixture.Case.test_b: 7.000000s > 5s", rendered)
            receipt["parsed"]["timing"]["tests"][0]["duration_seconds"] = 0
            receipt_path(self.root, suite).write_text(json.dumps(receipt))
            self.assertFalse(usable_receipt(self.root, suite).usable)

    def test_old_receipt_and_missing_or_interrupted_timing(self):
        suite = self._suite("oldtiming", "print('no timing')\n")
        _, receipt = self._run(suite)
        self.assertEqual(receipt["parsed"]["timing"]["status"], "unavailable")
        del receipt["parsed"]["timing"]
        receipt["receipt_digest"] = broad_check._receipt_digest(receipt)
        path = receipt_path(self.root, suite)
        path.write_text(json.dumps(receipt))
        before = path.read_bytes()
        self.assertTrue(usable_receipt(self.root, suite).usable)
        self.assertIn("timing: unavailable", broad_check.summarize(receipt))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(broad_check._with_timing({}, None, incomplete=True)["timing"],
                         {"status": "incomplete", "tests": [], "modules": {}})
        record = self.root / "provenance.json"
        record.with_name("timing.json").write_text('{"status":')
        self.assertEqual(broad_check._with_timing({}, record, incomplete=False)["timing"]["status"], "unavailable")

    def test_subset_observation_does_not_write_or_change_full_receipt(self):
        suite = self._suite("subsettiming", "print('subset')\n")
        self._run(suite)
        path = receipt_path(self.root, suite)
        before = path.read_bytes()
        code, observed = run_broad_check(suite, root=self.root, stream=self.stream, record_receipt=False)
        self.assertEqual(code, 0)
        self.assertEqual(observed["parsed"]["timing"]["status"], "unavailable")
        self.assertEqual(path.read_bytes(), before)



class ParsedVerdictTests(unittest.TestCase):
    def test_a_green_unittest_summary_yields_counts_and_skips(self) -> None:
        parsed = parse_unittest_summary("....s\nRan 1421 tests in 94.512s\n\nOK (skipped=3)\n")

        self.assertEqual(parsed["tests"], 1421)
        self.assertEqual(parsed["runner_duration_seconds"], 94.512)
        self.assertEqual(parsed["summary"], "OK")
        self.assertEqual(parsed["skipped"], 3)

    def test_a_red_unittest_summary_yields_failure_and_error_counts(self) -> None:
        parsed = parse_unittest_summary(
            "Ran 12 tests in 1.5s\n\nFAILED (failures=2, errors=1, skipped=4, expected failures=1)\n"
        )

        self.assertEqual(parsed["summary"], "FAILED")
        self.assertEqual(parsed["failures"], 2)
        self.assertEqual(parsed["errors"], 1)
        self.assertEqual(parsed["skipped"], 4)
        self.assertEqual(parsed["expected_failures"], 1)

    def test_a_runner_that_prints_no_summary_parses_to_nothing_rather_than_to_green(self) -> None:
        self.assertEqual(parse_unittest_summary("built 4 targets\n"), {})



class ReceiptIntegrityTests(BroadCheckTestCase):
    def test_a_truncated_or_edited_receipt_fails_closed(self) -> None:
        self._run("echo one")
        path = receipt_path(self.root, "echo one")
        good = path.read_text(encoding="utf-8")

        path.write_text(good[: len(good) // 2], encoding="utf-8")
        self.assertIsNone(load_receipt(path))
        self.assertFalse(usable_receipt(self.root, "echo one").usable)

        payload = json.loads(good)
        payload["verdict"] = "passed"
        payload["exit_code"] = 0
        payload["status"] = "complete"
        payload["tail"] = "OK"
        path.write_text(json.dumps(payload), encoding="utf-8")
        # Every field is individually plausible; the receipt is still refused because its own
        # digest no longer covers them.
        self.assertIsNone(load_receipt(path))
        lookup = usable_receipt(self.root, "echo one")
        self.assertFalse(lookup.usable)
        self.assertIn("no intact receipt", lookup.reason)

    def test_a_receipt_from_another_schema_is_not_read_as_this_one(self) -> None:
        self._run("echo one")
        path = receipt_path(self.root, "echo one")
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["schema_version"] = broad_check.SCHEMA_VERSION + 1
        payload["receipt_digest"] = broad_check._receipt_digest(payload)
        path.write_text(json.dumps(payload), encoding="utf-8")

        self.assertIsNone(load_receipt(path))

    def test_a_failed_publish_leaves_the_previous_receipt_intact(self) -> None:
        self._run("echo first; exit 0")
        path = receipt_path(self.root, "echo first; exit 0")
        before = load_receipt(path)
        self.assertIsNotNone(before)

        real_replace = os.replace

        def refuse(source, target, *args, **kwargs):
            if str(target) == str(path):
                raise OSError("disk full")
            return real_replace(source, target, *args, **kwargs)

        with (
            mock.patch("ummanu._fsutil.os.replace", side_effect=refuse),
            self.assertRaises(BroadCheckError) as caught,
        ):
            self._run("echo first; exit 0")
        self.assertEqual(caught.exception.code, "receipt_unwritable")

        # The reader still sees the whole previous receipt, never a partial new one, and no
        # staged temporary is left behind to be mistaken for one.
        self.assertEqual(load_receipt(path), before)
        self.assertEqual([entry.name for entry in path.parent.iterdir()], [path.name])

    def test_a_receipt_is_refused_where_git_would_offer_to_commit_it(self) -> None:
        (self.root / ".gitignore").write_text("nothing-here\n", encoding="utf-8")

        with self.assertRaises(BroadCheckError) as caught:
            self._run("echo one")
        self.assertEqual(caught.exception.code, "receipt_not_ignored")
        self.assertFalse(receipt_path(self.root, "echo one").exists())

    def test_the_committed_ignore_rules_cover_the_receipt_directory(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        target = broad_check.receipt_path(repo_root, "python3 -m unittest")
        result = subprocess.run(
            ["git", "-C", str(repo_root), "check-ignore", "-q", str(target)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

        self.assertEqual(result.returncode, 0, f"{target} must stay git-ignored")



class DocumentedCommandTests(unittest.TestCase):
    """One documented form, or roles invent their own and the evidence stops being comparable."""

    def test_the_wrapper_is_documented_where_a_role_looks_for_it(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        for name in ("CONTRIBUTING.md", "docs/OPERATIONS.md", "docs/PROTOCOLS.md"):
            text = (repo_root / name).read_text(encoding="utf-8")
            self.assertIn("check broad", text, name)

    def test_the_operator_reference_documents_reading_a_receipt_back(self) -> None:
        text = (Path(__file__).resolve().parents[1] / "docs" / "OPERATIONS.md").read_text(encoding="utf-8")

        self.assertIn("python3 -m ummanu check show --module", text)
        self.assertIn("state/checks/", text)

    def test_the_documented_shapes_say_which_one_attests_an_import(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        operations = (repo_root / "docs" / "OPERATIONS.md").read_text(encoding="utf-8")
        protocols = (repo_root / "docs" / "PROTOCOLS.md").read_text(encoding="utf-8")

        self.assertIn("origin: unobservable", operations)
        self.assertIn("--module", operations)
        self.assertIn("attests no import", protocols)
        # Both documents have to state the trust boundary reuse actually enforces.
        self.assertIn("outside the candidate", operations)
        self.assertIn("resolved inside the candidate workspace", protocols)



class ResultInvariantTests(BroadCheckTestCase):
    """secretary-1406 review: the writer's result invariants, enforced where readers come in.

    A digest proves nobody edited a payload after something computed it. It says nothing about
    whether the numbers describe a run that happened, so the combinations no run can produce are
    refused at the same boundary, before anything is authorized or any status is handed back.
    """

    def _stored(self, **changes: object) -> Path:
        """A real receipt from a real run, then damaged and re-digested the way a buggy tool would."""
        suite = self._suite("invariantsuite", "print('ran')\n")
        self._run(suite)
        path = receipt_path(self.root, suite)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.update(changes)
        payload["receipt_digest"] = broad_check._receipt_digest(payload)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def _damaged(self, marker: Path, **changes: object) -> tuple[CheckSpec, Path, list[str]]:
        """A real receipt for a red suite, damaged and re-digested the way a buggy writer would."""
        suite = self._suite(
            "reddish",
            f"open({str(marker)!r}, 'a', encoding='utf-8').write('ran\\n')\nraise SystemExit(2)\n",
        )
        argv = ["check", "broad", "--root", str(self.root), "--reuse", "--module", "reddish"]
        self.assertEqual(_status(argv), 2)
        path = receipt_path(self.root, suite)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.update(changes)
        payload["receipt_digest"] = broad_check._receipt_digest(payload)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return suite, path, argv

    def test_a_complete_receipt_with_a_whitespace_reason_is_refused_and_the_check_runs(self) -> None:
        """secretary-1406 review, BLOCKER-RECEIPT-WHITESPACE-REASON: a blank-after-strip reason
        used to read as no reason at all, so a complete receipt could carry one and still be
        reused. No run writes anything but `""` there."""
        marker = self.scripts / "whitespace-runs.txt"
        suite, path, argv = self._damaged(marker, incomplete_reason=" ")

        self.assertIsNone(load_receipt(path))
        lookup = usable_receipt(self.root, suite)
        self.assertFalse(lookup.usable)
        self.assertIsNone(lookup.authorized())
        self.assertIsNone(lookup.authorized_result())

        reused = _run_main(argv)
        self.assertFalse(reused["reused"])
        self.assertEqual(marker.read_text(encoding="utf-8").count("ran"), 2)

    def test_a_receipt_recording_exit_256_is_refused_and_cannot_mask_a_failure(self) -> None:
        """secretary-1406 review, BLOCKER-RECEIPT-EXIT-RANGE: 256 is not a status this POSIX
        wrapper can observe, and reusing it returned shell 0 because the value is masked."""
        marker = self.scripts / "range-runs.txt"
        suite, path, argv = self._damaged(marker, exit_code=256, verdict="failed")

        self.assertIsNone(load_receipt(path))
        self.assertFalse(usable_receipt(self.root, suite).usable)

        # At the shell, through a real process: the check runs again and its own 2 comes back,
        # rather than the stored 256 being masked to a successful 0.
        completed = subprocess.run(
            [sys.executable, "-m", "ummanu", *argv],
            cwd=Path(__file__).resolve().parents[1],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 2)
        self.assertFalse(json.loads(completed.stdout)["reused"])
        self.assertEqual(marker.read_text(encoding="utf-8").count("ran"), 2)

    def test_a_valid_red_receipt_still_preserves_its_status_at_the_shell(self) -> None:
        """The other half of the same behaviour: an intact exit-2 receipt is reused, and reuse
        still answers 2 through a real process."""
        marker = self.scripts / "valid-runs.txt"
        suite, _, argv = self._damaged(marker)  # no damage: the receipt stays valid

        self.assertTrue(usable_receipt(self.root, suite).usable)
        completed = subprocess.run(
            [sys.executable, "-m", "ummanu", *argv],
            cwd=Path(__file__).resolve().parents[1],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 2)
        self.assertTrue(json.loads(completed.stdout)["reused"])
        self.assertEqual(marker.read_text(encoding="utf-8").count("ran"), 1, "the check was skipped")

    def test_the_representable_edges_are_exactly_what_a_posix_process_can_return(self) -> None:
        representable = {
            "the widest normal status": (255, ""),
            "a green status": (0, ""),
            "the lowest signal": (-1, ""),
            "the highest signal this platform defines": (-(NSIG - 1), ""),
            "a normal exit the runner still calls unfinished": (0, "timed out after 0.5s"),
        }
        for name, (code, reason) in representable.items():
            with self.subTest(case=name):
                self.assertIsNotNone(RunResult.observe(code, reason), name)

        unrepresentable = {
            "a status one past the widest": (256, ""),
            "a wildly out-of-range status": (4096, ""),
            "a signal this platform does not define": (-NSIG, ""),
            "a code that is not a number": ("2", ""),
            "a boolean": (True, ""),
            "a reason that is not a string": (0, None),
        }
        for name, (code, reason) in unrepresentable.items():
            with self.subTest(case=name):
                self.assertIsNone(RunResult.observe(code, reason), name)

    def test_a_signalled_result_keeps_a_canonical_reason_and_an_unknown_verdict(self) -> None:
        bare = RunResult.observe(-15, "")
        given = RunResult.observe(-15, "timed out after 0.5s")

        self.assertEqual(bare.incomplete_reason, "killed by signal 15")
        self.assertEqual(given.incomplete_reason, "timed out after 0.5s")
        for result in (bare, given):
            self.assertEqual(result.signal, 15)
            self.assertEqual(result.status, "incomplete")
            self.assertEqual(result.verdict, "unknown")
            self.assertEqual(result.shell_status, 143)

    def test_the_boundary_refuses_every_result_no_run_could_have_written(self) -> None:
        cases = {
            "a complete run that died on a signal": {
                "exit_code": -9,
                "signal": 9,
                "verdict": "failed",
                "status": "complete",
            },
            "an exit code and signal that disagree": {
                "exit_code": -9,
                "signal": 2,
                "status": "incomplete",
                "verdict": "unknown",
                "incomplete_reason": "killed by signal 9",
            },
            "a signal on an ordinary exit": {"exit_code": 1, "signal": 9, "verdict": "failed"},
            "an incomplete run with no reason": {
                "status": "incomplete",
                "verdict": "unknown",
                "incomplete_reason": "",
            },
            "an incomplete run that claims a verdict": {
                "status": "incomplete",
                "verdict": "passed",
                "incomplete_reason": "timed out",
            },
            "a complete run carrying an incomplete reason": {"incomplete_reason": "timed out"},
            "a green exit reported as failed": {"exit_code": 0, "verdict": "failed"},
            "a red exit reported as passed": {"exit_code": 3, "verdict": "passed"},
            "a complete run with an unknown verdict": {"verdict": "unknown"},
            "an exit code that is not a number": {"exit_code": "0"},
            "a missing signal": {"signal": None},
            "a boolean masquerading as an exit code": {"exit_code": True, "verdict": "failed"},
            "a normal status outside the POSIX range": {"exit_code": 256, "verdict": "failed"},
            "a signal this platform does not define": {
                "exit_code": -NSIG,
                "signal": NSIG,
                "status": "incomplete",
                "verdict": "unknown",
                "incomplete_reason": f"killed by signal {NSIG}",
            },
            "a complete run whose reason is only whitespace": {"incomplete_reason": " "},
            "an incomplete reason that is not canonical": {
                "status": "incomplete",
                "verdict": "unknown",
                "incomplete_reason": "timed out  ",
            },
        }
        for name, changes in cases.items():
            with self.subTest(case=name):
                path = self._stored(**changes)
                self.assertIsNone(load_receipt(path), name)
                spec = CheckSpec.for_module("invariantsuite")
                self.assertFalse(usable_receipt(self.root, spec).usable, name)

    def test_the_results_a_run_does_write_are_still_accepted(self) -> None:
        cases = {
            "a green complete run": {},
            "a red complete run": {"exit_code": 2, "verdict": "failed"},
            "a killed run": {
                "exit_code": -15,
                "signal": 15,
                "status": "incomplete",
                "verdict": "unknown",
                "incomplete_reason": "killed by signal 15",
            },
            "a timeout that still exited normally": {
                "exit_code": 0,
                "signal": 0,
                "status": "incomplete",
                "verdict": "unknown",
                "incomplete_reason": "timed out after 0.5s",
            },
        }
        for name, changes in cases.items():
            with self.subTest(case=name):
                path = self._stored(**changes)
                self.assertIsNotNone(load_receipt(path), name)

    def test_a_signalled_run_this_wrapper_wrote_satisfies_its_own_invariants(self) -> None:
        # The rules are the writer's, so the writer's own output has to pass them — a killed run,
        # a timeout, an ordinary red and an ordinary green.
        _, killed = self._run("kill -9 $$")
        _, timed_out = self._run(
            self._script("sleeper.py", "import time\ntime.sleep(30)\n"), timeout_seconds=0.5
        )
        _, red = self._run("exit 4")
        _, green = self._run("true")

        for receipt in (killed, timed_out, red, green):
            self.assertEqual(broad_check.result_refusal(receipt), "")

    def test_no_reader_reaches_a_receipt_except_through_the_boundary(self) -> None:
        """The CLI has no way to a receipt that goes around `load_receipt`."""
        source = Path(broad_check.__file__).with_name("check_commands.py").read_text(encoding="utf-8")

        for bypass in ("json.load", "read_text", "read_bytes", "open("):
            self.assertNotIn(bypass, source, "check_commands must not read a receipt itself")
        self.assertNotIn("receipt_dir", source)
        # Nor may it take a result out of a receipt by hand: the status it returns comes from the
        # canonical model the boundary reconstructed.
        for raw in ('["exit_code"]', '"exit_code"', '["verdict"]', '["signal"]'):
            self.assertNotIn(raw, source, "check_commands must not read raw result fields")
        self.assertIn("shell_status", source)
        # And the only producer of a lookup is the function that starts by loading.
        self.assertIn("receipt = load_receipt(path)", Path(broad_check.__file__).read_text(encoding="utf-8"))



class CheckSetIdentityTests(BroadCheckTestCase):
    """secretary-1406 review, BLOCKER-CHECK-SET-IDENTITY-COLLISION.

    Identity used to be the rendered command line, and a rendering cannot carry an argument vector:
    `--module-arg 'one two'` and `--module-arg one --module-arg two` render identically, so the
    second invocation was handed the first one's receipt. The digest covers the structured check
    set instead, and the receipt stores it so a reader validates rather than trusts the filename.
    """

    def _record(self) -> Path:
        """A suite that logs its own argv — outside the checkout, so running it is not an edit."""
        log = self.scripts / "argv.log"
        (self.root / "argsuite.py").write_text(
            f"import sys\nopen({str(log)!r}, 'a', encoding='utf-8').write(repr(sys.argv[1:]) + '\\n')\n",
            encoding="utf-8",
        )
        return log

    def test_one_argument_with_a_space_is_not_the_same_check_as_two_arguments(self) -> None:
        log = self._record()
        joined = CheckSpec.for_module("argsuite", ("one two",))
        split = CheckSpec.for_module("argsuite", ("one", "two"))

        # They still *read* the same, which is exactly why the rendering cannot be the identity.
        self.assertEqual(joined.identity, split.identity)
        self.assertNotEqual(joined.digest, split.digest)
        self.assertNotEqual(receipt_path(self.root, joined), receipt_path(self.root, split))

        self._run(joined)
        # The second invocation must run: nothing here has evidence about it.
        self.assertFalse(usable_receipt(self.root, split).usable)
        self.assertIsNone(usable_receipt(self.root, split).receipt)
        self._run(split)

        logged = log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(logged, ["['one two']", "['one', 'two']"])

    def test_the_cli_runs_the_second_invocation_instead_of_reusing_the_first(self) -> None:
        log = self._record()
        base = ["check", "broad", "--root", str(self.root), "--reuse", "--module", "argsuite"]

        first = _run_main(base + ["--module-arg", "one two"])
        second = _run_main(base + ["--module-arg", "one", "--module-arg", "two"])
        # The same invocation again is the case reuse exists for, and it must still work.
        third = _run_main(base + ["--module-arg", "one", "--module-arg", "two"])

        self.assertFalse(first["reused"])
        self.assertFalse(second["reused"], "a different argument vector must run, not reuse")
        self.assertTrue(third["reused"])
        self.assertEqual(log.read_text(encoding="utf-8").splitlines(), ["['one two']", "['one', 'two']"])

    def test_a_receipt_whose_stored_check_set_was_edited_is_refused(self) -> None:
        self._record()
        spec = CheckSpec.for_module("argsuite", ("one", "two"))
        self._run(spec)
        path = receipt_path(self.root, spec)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["check_set"]["args"] = ["one two"]
        payload["receipt_digest"] = broad_check._receipt_digest(payload)
        path.write_text(json.dumps(payload), encoding="utf-8")

        # The receipt's own digest is intact, so only the check set's digest catches this.
        self.assertIsNone(load_receipt(path))
        self.assertFalse(usable_receipt(self.root, spec).usable)

    def test_a_receipt_found_under_another_check_s_name_is_refused(self) -> None:
        self._record()
        joined = CheckSpec.for_module("argsuite", ("one two",))
        split = CheckSpec.for_module("argsuite", ("one", "two"))
        self._run(joined)
        # Move the first receipt to where a lookup for the second would find it.
        receipt_path(self.root, joined).rename(receipt_path(self.root, split))

        lookup = usable_receipt(self.root, split)
        self.assertFalse(lookup.usable)
        self.assertEqual(lookup.reason, "receipt is for a different check")

    def test_shell_and_module_checks_never_share_an_identity(self) -> None:
        shell = CheckSpec.for_shell("python -m unittest")
        module = CheckSpec.for_module("unittest")

        self.assertEqual(shell.identity, module.identity)
        self.assertNotEqual(shell.digest, module.digest)



class StreamingSummaryTests(BroadCheckTestCase):
    """secretary-1406 review, BLOCKER-PARSED-SUMMARY-LOST-BEYOND-TAIL.

    The parsed verdict used to be read back out of the bounded tail, so a runner that printed its
    summary and then more than the tail's worth of cleanup output produced a passing receipt with
    no parsed fields at all. The verdict is scanned off the stream instead, in constant memory.
    """

    def test_a_summary_followed_by_more_than_the_tail_still_parses(self) -> None:
        command = self._script(
            "noisy.py",
            "import sys\n"
            "sys.stdout.write('Ran 5 tests in 0.100s\\n\\nOK (skipped=2)\\n')\n"
            "sys.stdout.write('cleanup\\n' * 4000)\n",
        )

        _, receipt = self._run(command)

        self.assertGreater(receipt["output_bytes"], broad_check.TAIL_BYTES)
        self.assertNotIn("OK (skipped=2)", receipt["tail"])
        self.assertEqual(
            receipt["parsed"],
            {"tests": 5, "runner_duration_seconds": 0.1, "summary": "OK", "skipped": 2,
             "timing": {"status": "unavailable", "tests": [], "modules": {}}},
        )
        # Parsing kept the verdict without keeping the logs.
        self.assertLessEqual(len(receipt["tail"].encode("utf-8")), broad_check.TAIL_BYTES)

    def test_a_red_summary_buried_under_cleanup_output_is_not_lost_either(self) -> None:
        command = self._script(
            "noisyred.py",
            "import sys\n"
            "sys.stdout.write('Ran 9 tests in 2.000s\\n\\nFAILED (failures=1, skipped=3)\\n')\n"
            "sys.stdout.write('x' * 40000 + '\\n')\n"
            "sys.exit(1)\n",
        )

        exit_code, receipt = self._run(command)

        self.assertEqual(exit_code, 1)
        self.assertEqual(receipt["parsed"]["summary"], "FAILED")
        self.assertEqual(receipt["parsed"]["failures"], 1)
        self.assertEqual(receipt["parsed"]["skipped"], 3)
        self.assertEqual(receipt["parsed"]["tests"], 9)

    def test_a_summary_split_across_reads_is_still_seen(self) -> None:
        scanner = broad_check._SummaryScanner()
        for chunk in (b"Ran 7 te", b"sts in 1.5s\n\nOK (ski", b"pped=1)\n"):
            scanner.feed(chunk)

        self.assertEqual(
            scanner.finish(),
            {"tests": 7, "runner_duration_seconds": 1.5, "summary": "OK", "skipped": 1},
        )

    def test_the_scanner_keeps_constant_state_across_an_enormous_line(self) -> None:
        scanner = broad_check._SummaryScanner()
        for _ in range(64):
            scanner.feed(b"y" * 65536)
        scanner.feed(b"\nRan 2 tests in 0.5s\nOK\n")

        self.assertEqual(len(scanner._carry), 0)
        self.assertEqual(scanner.finish(), {"tests": 2, "runner_duration_seconds": 0.5, "summary": "OK"})

    def test_the_last_summary_wins_when_a_runner_prints_several(self) -> None:
        parsed = parse_unittest_summary("Ran 1 test in 0.1s\nOK\nRan 4 tests in 0.4s\nFAILED (errors=2)\n")

        self.assertEqual(
            parsed,
            {"tests": 4, "runner_duration_seconds": 0.4, "summary": "FAILED", "errors": 2},
        )



class CheckCommandTests(BroadCheckTestCase):
    def _main(self, argv: list[str]) -> tuple[int, dict]:
        stdout = StringIO()
        with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", StringIO()):
            code = main(argv)
        return code, json.loads(stdout.getvalue())

    def test_the_command_exit_status_survives_the_wrapper(self) -> None:
        code, payload = self._main(["check", "broad", "--root", str(self.root), "--command", "exit 7"])

        self.assertEqual(code, 7)
        self.assertEqual(payload["receipt"]["exit_code"], 7)
        self.assertFalse(payload["reused"])
        self.assertIn("exit_code: 7", payload["summary"])

    def test_a_signal_death_is_reported_as_a_shell_status_not_flattened_to_one(self) -> None:
        code, payload = self._main(["check", "broad", "--root", str(self.root), "--command", "kill -9 $$"])

        self.assertEqual(code, 137)
        self.assertEqual(payload["receipt"]["status"], "incomplete")

    def test_show_reports_usability_without_running_the_check_again(self) -> None:
        # The marker lives outside the checkout: a check that edited the workspace would
        # legitimately change its content identity, which is a different test than this one.
        marker = self.scripts / "ran.txt"
        self._suite(
            "marksuite",
            f"open({str(marker)!r}, 'a', encoding='utf-8').write('ran\\n')\n",
        )
        self._main(["check", "broad", "--root", str(self.root), "--module", "marksuite"])
        self.assertEqual(marker.read_text(encoding="utf-8").count("ran"), 1)

        code, payload = self._main(["check", "show", "--root", str(self.root), "--module", "marksuite"])

        self.assertEqual(code, 0)
        self.assertTrue(payload["usable"])
        self.assertEqual(payload["receipt"]["verdict"], "passed")
        self.assertIn("- command:", payload["summary"])
        self.assertEqual(marker.read_text(encoding="utf-8").count("ran"), 1)

    def test_show_refuses_a_shell_receipt_because_it_attests_no_import(self) -> None:
        self._main(["check", "broad", "--root", str(self.root), "--command", "echo suite"])

        code, payload = self._main(["check", "show", "--root", str(self.root), "--command", "echo suite"])

        self.assertEqual(code, 1)
        self.assertFalse(payload["usable"])
        self.assertIn("provenance was not observed", payload["reason"])
        # The summary is still there to read; it just cannot replace a run.
        self.assertIn("attests no import", payload["summary"])

    def test_show_refuses_a_receipt_that_no_longer_describes_the_checkout(self) -> None:
        self._suite("showsuite", "print('ran')\n")
        self._main(["check", "broad", "--root", str(self.root), "--module", "showsuite"])
        (self.root / "app.py").write_text("VALUE = 9\n", encoding="utf-8")

        code, payload = self._main(["check", "show", "--root", str(self.root), "--module", "showsuite"])

        self.assertEqual(code, 1)
        self.assertFalse(payload["usable"])

    def test_reuse_skips_the_run_only_while_the_content_is_unchanged(self) -> None:
        marker = self.scripts / "runs.txt"
        self._suite(
            "reruns",
            f"open({str(marker)!r}, 'a', encoding='utf-8').write('ran\\n')\n",
        )
        argv = ["check", "broad", "--root", str(self.root), "--module", "reruns", "--reuse"]
        self._main(argv)

        code, payload = self._main(argv)
        self.assertEqual(code, 0)
        self.assertTrue(payload["reused"])
        self.assertEqual(marker.read_text(encoding="utf-8").count("ran"), 1)

        (self.root / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
        code, payload = self._main(argv)
        self.assertEqual(code, 0)
        self.assertFalse(payload["reused"])
        self.assertEqual(marker.read_text(encoding="utf-8").count("ran"), 2)

    def test_a_reused_receipt_returns_the_status_of_the_run_it_replaces(self) -> None:
        """secretary-1406 review, BLOCKER-REUSED-EXIT-STATUS: reuse used to answer 0-or-1, so a
        check that failed with 2 came back as 1 the moment its receipt stood in for it — losing
        exactly the fact the caller ran the check to learn."""
        self._suite("usagesuite", "raise SystemExit(2)\n")
        argv = ["check", "broad", "--root", str(self.root), "--reuse", "--module", "usagesuite"]

        stdout = StringIO()
        with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", StringIO()):
            fresh = main(argv)
            reused = main(argv)
        payloads = [json.loads(part) for part in _documents(stdout.getvalue())]

        self.assertEqual(fresh, 2)
        self.assertEqual(reused, 2, "a receipt standing in for the run answers what the run did")
        self.assertFalse(payloads[0]["reused"])
        self.assertTrue(payloads[1]["reused"])
        self.assertEqual(payloads[1]["receipt"]["exit_code"], 2)

    def test_a_complete_receipt_recording_a_signal_is_refused_and_the_check_runs(self) -> None:
        """secretary-1406 review, BLOCKER-RECEIPT-RESULT-INCONSISTENCY.

        This test used to manufacture exactly this artifact and assert that reuse honoured it. A
        signalled run is written `incomplete` and is never evidence that a suite finished, so a
        receipt claiming both is corrupt however neatly its own digest was recomputed — and
        corruption outranks status preservation and reuse.
        """
        marker = self.scripts / "signal-runs.txt"
        suite = self._suite(
            "signalsuite",
            f"open({str(marker)!r}, 'a', encoding='utf-8').write('ran\\n')\n",
        )
        argv = ["check", "broad", "--root", str(self.root), "--reuse", "--module", "signalsuite"]
        self.assertEqual(_status(argv), 0)
        self.assertEqual(marker.read_text(encoding="utf-8").count("ran"), 1)

        path = receipt_path(self.root, suite)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["exit_code"] = -9
        payload["signal"] = 9
        payload["verdict"] = "failed"
        payload["receipt_digest"] = broad_check._receipt_digest(payload)
        path.write_text(json.dumps(payload), encoding="utf-8")

        # The boundary refuses it, so no reader downstream ever sees it...
        self.assertIsNone(load_receipt(path))
        lookup = usable_receipt(self.root, suite)
        self.assertFalse(lookup.usable)
        self.assertIsNone(lookup.receipt)
        self.assertIsNone(lookup.authorized())
        # ...and the command runs instead of standing on it, which the suite's own marker proves.
        reused = _run_main(argv)
        self.assertFalse(reused["reused"])
        self.assertEqual(marker.read_text(encoding="utf-8").count("ran"), 2)

    def test_a_shell_receipt_is_never_reused_in_place_of_a_run(self) -> None:
        marker = self.scripts / "shell-runs.txt"
        command = f"echo ran >> {marker}"
        argv = ["check", "broad", "--root", str(self.root), "--command", command, "--reuse"]
        self._main(argv)
        code, payload = self._main(argv)

        self.assertEqual(code, 0)
        self.assertFalse(payload["reused"])
        self.assertEqual(marker.read_text(encoding="utf-8").count("ran"), 2)

    def test_a_check_needs_exactly_one_shape(self) -> None:
        stdout, stderr = StringIO(), StringIO()
        with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
            neither = main(["check", "broad", "--root", str(self.root)])
            both = main(
                [
                    "check",
                    "broad",
                    "--root",
                    str(self.root),
                    "--module",
                    "unittest",
                    "--command",
                    "true",
                ]
            )

        self.assertEqual(neither, 2)
        self.assertEqual(both, 2)

    def test_an_empty_command_is_a_usage_error_and_writes_nothing(self) -> None:
        stdout, stderr = StringIO(), StringIO()
        with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
            code = main(["check", "broad", "--root", str(self.root), "--command", "   "])

        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stderr.getvalue())["error"]["code"], "empty_command")
        self.assertFalse(broad_check.receipt_dir(self.root).exists())



if __name__ == "__main__":
    unittest.main()
