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
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from errno import ENOEXEC
from io import StringIO
from pathlib import Path
from signal import NSIG
from unittest import mock

from ummanu import broad_check
from ummanu.broad_check import (
    BroadCheckError,
    CheckSpec,
    RunResult,
    content_identity,
    load_receipt,
    parse_unittest_summary,
    receipt_path,
    run_broad_check,
    usable_receipt,
)
from ummanu.cli import main
from ummanu.runtime.role_env import workspace_tool_cache_env


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _git_out(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    ).stdout.strip()


def _documents(text: str) -> list[str]:
    """Split the concatenated JSON documents one capture may hold."""
    return [part for part in text.replace("}\n{", "}\n\x00{").split("\x00") if part.strip()]


def _status(argv: list[str]) -> int:
    """Run one CLI command for its exit status alone."""
    with mock.patch("sys.stdout", StringIO()), mock.patch("sys.stderr", StringIO()):
        return main(argv)


def _run_main(argv: list[str]) -> dict:
    """Run one CLI command, capturing the JSON document it prints on stdout."""
    stdout = StringIO()
    with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", StringIO()):
        main(argv)
    return json.loads(stdout.getvalue())


class BroadCheckTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name) / "workspace"
        self.scripts = Path(self.tmpdir.name) / "scripts"
        self.scripts.mkdir()
        self._init_workspace(self.root)
        self.stream = StringIO()
        # A live root with no registered project, unless a case names its own with `--instance`: the
        # CLI neither reads this host's installation nor falls back to the default live root, which
        # it refuses when absent (ummanu-39). An empty one answers `no_project_binding`, as before.
        empty_live_root = Path(self.tmpdir.name) / "live-root"
        empty_live_root.mkdir()
        self.enterContext(mock.patch.dict(os.environ, {"UMMANU_INSTANCE": str(empty_live_root)}))

    def _init_workspace(self, root: Path, *, project_package: str = "ummanu") -> Path:
        """A committed candidate checkout, optionally without any importable project package.

        `project_package=""` is not a curiosity: since issue:8b39e60e4df361c6138e the wrapper puts
        the candidate's own import roots at the front of the check process's `sys.path`, so a
        candidate that *does* carry the package can no longer be made to import someone else's copy
        of it. A test about the candidate boundary therefore needs a candidate with nothing of its
        own to import, which is also a real shape: a checkout mid-rename, or a project whose
        adapter names a package this checkout does not contain.
        """
        root.mkdir(parents=True)
        _git(root, "init", "-q")
        _git(root, "config", "user.email", "worker@example.invalid")
        _git(root, "config", "user.name", "worker")
        # `__pycache__/` is ignored here for the same reason every real checkout ignores it:
        # a Python check writes bytecode as it runs, and that is not a change to the code.
        (root / ".gitignore").write_text("/state/\n__pycache__/\n", encoding="utf-8")
        (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        # A candidate workspace is a checkout of the project under check, and reuse is only ever
        # authorized for a check process that imported the project from here.
        if project_package:
            (root / project_package).mkdir()
            (root / project_package / "__init__.py").write_text("", encoding="utf-8")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "base")
        return root

    def _script(self, name: str, body: str) -> str:
        path = self.scripts / name
        path.write_text(body, encoding="utf-8")
        return f"{sys.executable} {path}"

    def _run(self, command, **kwargs):
        return run_broad_check(command, root=self.root, stream=self.stream, **kwargs)

    def _suite(
        self, name: str, body: str, args: tuple[str, ...] = (), *, root: Path | None = None
    ) -> CheckSpec:
        """A module-shaped check: the standard shape, and the only one that attests an import."""
        ((root or self.root) / f"{name}.py").write_text(body, encoding="utf-8")
        return CheckSpec.for_module(name, args)


class RunAndCaptureTests(BroadCheckTestCase):
    def test_large_interleaved_output_keeps_its_order_and_bounds_the_artifact(self) -> None:
        # One pipe for both streams is the whole point: a failing test's traceback on stderr has
        # to stay next to the dots on stdout that say which test it was.
        command = self._script(
            "interleave.py",
            "import sys\n"
            "for index in range(4000):\n"
            "    sys.stdout.write('out-%d\\n' % index); sys.stdout.flush()\n"
            "    sys.stderr.write('err-%d\\n' % index); sys.stderr.flush()\n",
        )
        exit_code, receipt = self._run(command)

        self.assertEqual(exit_code, 0)
        streamed = self.stream.getvalue().splitlines()
        self.assertEqual(len(streamed), 8000)
        self.assertEqual(streamed[:4], ["out-0", "err-0", "out-1", "err-1"])
        self.assertEqual(streamed[-2:], ["out-3999", "err-3999"])
        self.assertEqual(receipt["output_lines"], 8000)
        self.assertGreater(receipt["output_bytes"], 60000)

        # The receipt itself stays small whatever the suite printed, and the tail it does keep is
        # the end of the run, in order, with no half line pretending to be a whole one.
        self.assertTrue(receipt["tail_truncated"])
        self.assertLessEqual(len(receipt["tail"].encode("utf-8")), broad_check.TAIL_BYTES)
        tail_lines = receipt["tail"].splitlines()
        self.assertLessEqual(len(tail_lines), broad_check.TAIL_LINES)
        self.assertEqual(tail_lines[-2:], ["out-3999", "err-3999"])
        self.assertTrue(all(line.startswith(("out-", "err-")) for line in tail_lines))
        self.assertLess(receipt_path(self.root, command).stat().st_size, 32768)

    def test_one_unbroken_line_cannot_grow_the_receipt(self) -> None:
        command = self._script("onebigline.py", "import sys\nsys.stdout.write('x' * 500000)\n")
        _, receipt = self._run(command)

        self.assertEqual(receipt["output_bytes"], 500000)
        self.assertLessEqual(len(receipt["tail"].encode("utf-8")), broad_check.TAIL_BYTES)

    def test_nonzero_exit_is_preserved_and_remains_usable_evidence(self) -> None:
        suite = self._suite(
            "redsuite",
            "import sys\nsys.stderr.write('boom\\n')\nsys.exit(3)\n",
        )
        exit_code, receipt = self._run(suite)

        self.assertEqual(exit_code, 3)
        self.assertEqual(receipt["exit_code"], 3)
        self.assertEqual(receipt["status"], "complete")
        self.assertEqual(receipt["verdict"], "failed")
        self.assertIn("boom", receipt["tail"])
        # A red answer is a concrete answer: the point of the receipt is that nobody reruns the
        # suite to rediscover it.
        lookup = usable_receipt(self.root, suite)
        self.assertTrue(lookup.usable)
        self.assertEqual(lookup.receipt["verdict"], "failed")

    def test_a_shell_shape_keeps_its_exit_status_while_attesting_no_import(self) -> None:
        exit_code, receipt = self._run("echo boom 1>&2; exit 3")

        self.assertEqual(exit_code, 3)
        self.assertEqual(receipt["command_shape"], "shell")
        self.assertIn("boom", receipt["tail"])
        self.assertEqual(receipt["project_provenance"]["origin"], "unobservable")

    def test_a_killed_command_is_incomplete_and_never_usable(self) -> None:
        exit_code, receipt = self._run("echo starting; kill -9 $$")

        self.assertEqual(exit_code, -9)
        self.assertEqual(receipt["signal"], 9)
        self.assertEqual(receipt["status"], "incomplete")
        self.assertEqual(receipt["verdict"], "unknown")
        self.assertIn("killed by signal 9", receipt["incomplete_reason"])
        self.assertIn("starting", receipt["tail"])

        lookup = usable_receipt(self.root, "echo starting; kill -9 $$")
        self.assertFalse(lookup.usable)
        self.assertIn("did not finish", lookup.reason)

    def test_a_command_that_hangs_silently_is_stopped_and_recorded_as_incomplete(self) -> None:
        command = self._script("hang.py", "import time\ntime.sleep(30)\n")
        exit_code, receipt = self._run(command, timeout_seconds=0.5)

        self.assertNotEqual(exit_code, 0)
        self.assertEqual(receipt["status"], "incomplete")
        self.assertIn("timed out", receipt["incomplete_reason"])
        self.assertFalse(usable_receipt(self.root, command).usable)

    def test_a_module_check_reports_the_import_its_own_process_made(self) -> None:
        suite = self._suite("quietsuite", "print('done')\n")

        _, receipt = self._run(suite)
        provenance = receipt["project_provenance"]

        self.assertEqual(provenance["origin"], "check-process")
        self.assertEqual(provenance["cwd"], str(self.root.resolve()))
        self.assertTrue(provenance["inside_workspace"])
        self.assertTrue(
            provenance["imported_project"].startswith(str(self.root.resolve())),
            provenance["imported_project"],
        )

    def test_the_receipt_records_where_and_when_the_run_happened(self) -> None:
        _, receipt = self._run("echo timed")

        self.assertEqual(receipt["cwd"], str(self.root.resolve()))
        self.assertEqual(receipt["command_or_check_set_digest"], CheckSpec.for_shell("echo timed").digest)
        self.assertLessEqual(receipt["started_at"], receipt["ended_at"])
        self.assertGreaterEqual(receipt["duration_seconds"], 0.0)
        self.assertEqual(receipt["content_identity"]["tree_sha"], content_identity(self.root).tree_sha)


class TimingReceiptTests(BroadCheckTestCase):
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


class UnchangedContentReuseTests(BroadCheckTestCase):
    def setUp(self) -> None:
        super().setUp()
        # Reuse is only ever offered for the standard module shape, so these tests use it.
        self.suite = self._suite("reusesuite", "print('suite ran')\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "suite")

    def test_an_unchanged_checkout_reuses_the_receipt_and_any_edit_invalidates_it(self) -> None:
        self._run(self.suite)
        lookup = usable_receipt(self.root, self.suite)
        self.assertTrue(lookup.usable)
        self.assertEqual(lookup.reason, "receipt describes this exact content")

        # Writing the receipt is itself a change to the directory; it must not invalidate the
        # receipt it just wrote, or reuse would never be possible at all.
        self.assertTrue(usable_receipt(self.root, self.suite).usable)

        (self.root / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        stale = usable_receipt(self.root, self.suite)
        self.assertFalse(stale.usable)
        self.assertIn("content changed", stale.reason)

        (self.root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        self.assertTrue(usable_receipt(self.root, self.suite).usable)

    def test_committing_the_same_content_keeps_the_identity(self) -> None:
        """secretary-1442: one suite, four runs per generation; this was two of them.

        A worker checks a dirty worktree, then commits exactly what it just checked. The commit
        moves HEAD but not a byte of content, so the receipt still describes this checkout.
        """
        (self.root / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        self._run(self.suite)
        self.assertTrue(usable_receipt(self.root, self.suite).usable)
        dirty = content_identity(self.root)

        _git(self.root, "commit", "-q", "-a", "-m", "second")

        committed = content_identity(self.root)
        self.assertTrue(committed.resolved)
        self.assertEqual(committed, dirty)
        # On a clean checkout the identity is nothing more exotic than HEAD's own tree.
        self.assertEqual(committed.tree_sha, _git_out(self.root, "rev-parse", "HEAD^{tree}"))
        lookup = usable_receipt(self.root, self.suite)
        self.assertTrue(lookup.usable, lookup.reason)

    def test_same_size_edit_in_the_index_timestamp_window_is_hashed(self) -> None:
        """The scratch index must retain Git's racy-clean detection boundary.

        Give the tracked file, its index entry and the index itself one timestamp, then change only
        the file's bytes while retaining its size and stat data. Git hashes that deliberately racy
        path when the copied index keeps its original mtime. Rewriting the index bytes gives the
        copy a newer mtime and incorrectly reuses the old blob.
        """
        app = self.root / "app.py"
        index = self.root / _git_out(self.root, "rev-parse", "--git-path", "index")
        stamp = time.time_ns() - 2_000_000_000
        os.utime(app, ns=(stamp, stamp))
        _git(self.root, "add", "app.py")
        os.utime(index, ns=(stamp, stamp))

        app.write_text("VALUE = 2\n", encoding="utf-8")
        os.utime(app, ns=(stamp, stamp))
        observed = content_identity(self.root)

        _git(self.root, "add", "-A")
        expected = _git_out(self.root, "write-tree")
        self.assertTrue(observed.resolved)
        self.assertEqual(observed.tree_sha, expected)

    def test_committing_an_untracked_file_keeps_the_identity(self) -> None:
        (self.root / "extra.py").write_text("HELPER = True\n", encoding="utf-8")
        self._run(self.suite)
        untracked = content_identity(self.root)

        # The same content, now tracked: a change of bookkeeping, not of what a suite would read.
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "adopt the helper")

        self.assertEqual(content_identity(self.root), untracked)
        self.assertTrue(usable_receipt(self.root, self.suite).usable)

    def test_committing_different_content_changes_the_identity(self) -> None:
        self._run(self.suite)
        (self.root / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        _git(self.root, "commit", "-q", "-a", "-m", "a real edit")

        stale = usable_receipt(self.root, self.suite)
        self.assertFalse(stale.usable)
        self.assertIn("content changed", stale.reason)

    def test_a_tracked_file_that_matches_an_ignore_rule_still_counts(self) -> None:
        """Ignore rules govern untracked paths only, and the identity must agree with git.

        The identity is taken through a copy of the real index for exactly this case: from an empty
        index a tracked-and-ignored path would silently drop out, and edits to it would be invisible.
        """
        (self.root / ".gitignore").write_text("/state/\n__pycache__/\nlogged.txt\n", encoding="utf-8")
        (self.root / "logged.txt").write_text("one\n", encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "add", "-f", "logged.txt")
        _git(self.root, "commit", "-q", "-m", "track a path that is also ignored")
        self._run(self.suite)
        self.assertTrue(usable_receipt(self.root, self.suite).usable)

        (self.root / "logged.txt").write_text("two\n", encoding="utf-8")

        stale = usable_receipt(self.root, self.suite)
        self.assertFalse(stale.usable)
        self.assertIn("content changed", stale.reason)

    def test_a_receipt_from_before_the_tree_identity_is_never_reused(self) -> None:
        """An old receipt records `head_sha`; nothing here can compare that with a tree id.

        It is re-signed here so the reader cannot dismiss it as merely corrupt: the point is that a
        well-formed receipt in the old format reads as unresolved rather than as a match.
        """
        self._run(self.suite)
        path = receipt_path(self.root, self.suite)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["content_identity"] = {"head_sha": "0" * 40, "worktree_digest": "1" * 64}
        payload["receipt_digest"] = broad_check._receipt_digest(payload)
        path.write_text(json.dumps(payload), encoding="utf-8")

        lookup = usable_receipt(self.root, self.suite)
        self.assertIsNotNone(load_receipt(path))
        self.assertFalse(lookup.usable)
        self.assertIn("content changed", lookup.reason)

    def test_a_new_untracked_file_changes_the_identity(self) -> None:
        self._run(self.suite)
        (self.root / "extra.py").write_text("HELPER = True\n", encoding="utf-8")

        self.assertFalse(usable_receipt(self.root, self.suite).usable)

    def test_a_check_that_writes_into_the_checkout_invalidates_its_own_receipt(self) -> None:
        writer = self._suite("writersuite", "open('artefact.txt', 'w', encoding='utf-8').write('made\\n')\n")
        self._run(writer)

        # Not a defect: the content the receipt described is not the content now on disk, and
        # saying so is the whole contract.
        lookup = usable_receipt(self.root, writer)
        self.assertFalse(lookup.usable)
        self.assertIn("content changed", lookup.reason)

    def test_a_receipt_for_another_command_is_never_offered_for_this_one(self) -> None:
        self._run(self.suite)

        other = usable_receipt(self.root, CheckSpec.for_module("othersuite"))
        self.assertFalse(other.usable)
        self.assertIsNone(other.receipt)

    def test_a_checkout_without_a_resolvable_identity_never_reuses_anything(self) -> None:
        bare = Path(self.tmpdir.name) / "not-a-repo"
        bare.mkdir()
        (bare / "baresuite.py").write_text("print('suite ran')\n", encoding="utf-8")
        (bare / "ummanu").mkdir()
        (bare / "ummanu" / "__init__.py").write_text("", encoding="utf-8")
        spec = CheckSpec.for_module("baresuite")
        exit_code, receipt = run_broad_check(spec, root=bare, stream=self.stream)

        self.assertEqual(exit_code, 0)
        self.assertEqual(receipt["content_identity"], {"tree_sha": ""})
        lookup = usable_receipt(bare, spec)
        self.assertFalse(lookup.usable)
        self.assertIn("no resolvable content identity", lookup.reason)


class ProvenanceHonestyTests(BroadCheckTestCase):
    """secretary-1406 review, BLOCKER-ACTUAL-IMPORT-PROVENANCE.

    Provenance used to come from a preflight probe the wrapper ran in the workspace before it
    started the check. That probe answers a different question than the one asked: the check may
    `cd` elsewhere, or reach a different interpreter or import path, and the receipt would still
    report the candidate worktree as the imported project — on a complete, passing, reusable
    receipt. Nothing may claim an import it did not observe.
    """

    def _outside_project(self) -> Path:
        outside = Path(self.tmpdir.name) / "elsewhere"
        (outside / "ummanu").mkdir(parents=True)
        (outside / "ummanu" / "__init__.py").write_text("", encoding="utf-8")
        return outside

    def test_a_shell_check_that_changes_directory_cannot_claim_the_candidate_checkout(self) -> None:
        outside = self._outside_project()
        command = f'cd {outside}; {sys.executable} -c "import ummanu, sys; sys.stdout.write(ummanu.__file__)"'

        exit_code, receipt = self._run(command)

        self.assertEqual(exit_code, 0)
        # The check really did import the other checkout, and it really did pass.
        self.assertIn(str(outside), receipt["tail"])
        self.assertEqual(receipt["status"], "complete")
        self.assertEqual(receipt["verdict"], "passed")
        # The receipt claims no import at all, and above all not this workspace's.
        provenance = receipt["project_provenance"]
        self.assertEqual(provenance["origin"], "unobservable")
        self.assertEqual(provenance["imported_project"], "")
        self.assertFalse(provenance["inside_workspace"])
        self.assertNotIn(str(self.root.resolve()), json.dumps(provenance))
        # And it can never stand in for running the check again.
        lookup = usable_receipt(self.root, command)
        self.assertFalse(lookup.usable)
        self.assertIn("provenance was not observed", lookup.reason)

    def test_no_shell_check_is_ever_reusable_however_ordinary_it_looks(self) -> None:
        for command in ("true", f"cd / && {sys.executable} -c 'pass'", "echo ok"):
            with self.subTest(command=command):
                self._run(command)
                lookup = usable_receipt(self.root, command)
                self.assertFalse(lookup.usable)
                self.assertEqual(lookup.receipt["project_provenance"]["origin"], "unobservable")

    def test_an_import_from_outside_the_candidate_is_recorded_and_refused(self) -> None:
        """The end-to-end case the second review reproduced: a real subprocess, a real safe-path
        import environment, and an alternate checkout ordered before the candidate.

        The candidate here deliberately carries no project package of its own. Since
        issue:8b39e60e4df361c6138e the wrapper prepends the candidate's import roots, so a
        candidate that has the package *cannot* be talked into importing another copy -- which is
        the fix, not a loss of coverage. The invariant this test exists for is the other half: when
        the check process really did import something else, the receipt says so and refuses.
        """
        outside = self._outside_project()
        elsewhere = self._init_workspace(Path(self.tmpdir.name) / "no-package", project_package="")
        suite = self._suite("outsidesuite", "import ummanu\nprint(ummanu.__file__)\n", root=elsewhere)
        env = dict(
            os.environ,
            PYTHONSAFEPATH="1",
            PYTHONPATH=os.pathsep.join([str(outside), str(elsewhere)]),
        )

        _, receipt = run_broad_check(suite, root=elsewhere, stream=self.stream, env=env)

        # The receipt tells the truth about what the check process imported...
        provenance = receipt["project_provenance"]
        self.assertEqual(provenance["origin"], "check-process")
        self.assertTrue(
            provenance["imported_project"].startswith(str(outside.resolve())),
            provenance["imported_project"],
        )
        self.assertFalse(provenance["inside_workspace"])
        self.assertIn(str(outside), receipt["tail"])
        # ...and precisely because that import was not this candidate, it cannot stand in for a
        # run of this candidate.
        lookup = usable_receipt(elsewhere, suite)
        self.assertFalse(lookup.usable)
        self.assertIn("outside this candidate workspace", lookup.reason)
        self.assertIsNone(lookup.authorized())

        # The ordinary candidate-inside run of the same standard shape stays reusable -- and stays
        # reusable in the very same import environment, because the candidate's roots come first.
        inside_suite = self._suite("outsidesuite", "import ummanu\nprint(ummanu.__file__)\n")
        _, inside_receipt = self._run(inside_suite, env=env)
        self.assertTrue(inside_receipt["project_provenance"]["inside_workspace"])
        self.assertTrue(usable_receipt(self.root, inside_suite).usable)

    def test_a_check_process_outside_the_candidate_authorizes_nothing_even_when_it_fails(
        self,
    ) -> None:
        # The suite lives outside the candidate and the candidate carries no project package of its
        # own, so whatever this check process imported, it was not this checkout. Whether the
        # project is importable at all from there depends on the machine — an installed copy in
        # site-packages is exactly the case CI runs — so the assertion is about the candidate
        # boundary, which is the invariant, and not about that machine's site configuration.
        # (The candidate's own import roots now come first, so a candidate that *had* the package
        # would import it; the package's absence is what keeps this case reachable at all.)
        elsewhere = self._init_workspace(Path(self.tmpdir.name) / "no-package", project_package="")
        (self.scripts / "crashsuite.py").write_text("raise SystemExit(4)\n", encoding="utf-8")
        suite = CheckSpec.for_module("crashsuite")
        env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONSAFEPATH": "1",
            "PYTHONPATH": str(self.scripts),
        }

        _, receipt = run_broad_check(suite, root=elsewhere, stream=self.stream, env=env)

        self.assertEqual(receipt["exit_code"], 4)
        provenance = receipt["project_provenance"]
        self.assertEqual(provenance["origin"], "check-process")
        self.assertNotIn(str(elsewhere.resolve()), provenance["imported_project"])
        self.assertFalse(provenance["inside_workspace"])
        lookup = usable_receipt(elsewhere, suite)
        self.assertFalse(lookup.usable)
        self.assertIsNone(lookup.authorized())

    def test_every_untrusted_import_is_refused_by_the_one_predicate(self) -> None:
        """The refusal table itself, without an interpreter's site configuration in the way."""
        cases = {
            "no provenance at all": {},
            "an unobserved shape": {"project_provenance": dict(broad_check._UNOBSERVED_PROVENANCE)},
            "a check process that imported nothing": {
                "project_provenance": {"origin": "check-process", "imported_project": ""}
            },
            "an import from outside the candidate": {
                "project_provenance": {
                    "origin": "check-process",
                    "imported_project": str(self.scripts / "ummanu" / "__init__.py"),
                }
            },
            "missing interpreter environment provenance": {
                "project_provenance": {
                    "origin": "check-process",
                    "imported_project": str(self.root / "ummanu" / "__init__.py"),
                }
            },
            "an unresolvable import path": {
                "project_provenance": {"origin": "check-process", "imported_project": "\x00"}
            },
        }
        for name, receipt in cases.items():
            with self.subTest(case=name):
                self.assertTrue(broad_check.candidate_import_refusal(receipt, self.root))

        trusted = {
            "project_provenance": {
                "origin": "check-process",
                "imported_project": str(self.root / "ummanu" / "__init__.py"),
                "environment_prefix": str(self.scripts),
            }
        }
        self.assertEqual(broad_check.candidate_import_refusal(trusted, self.root), "")
        self.assertIn(
            "configured project package",
            broad_check.candidate_import_refusal(
                {"project_provenance": {**trusted["project_provenance"], "imported_package": "other"}},
                self.root,
                expected_package="ummanu",
            ),
        )

    def test_every_route_refuses_the_same_receipts_for_the_same_reasons(self) -> None:
        """No route may authorize reuse the other would refuse: `check show` and `--reuse` read
        one predicate, so their verdicts cannot drift apart."""
        outside = self._outside_project()
        # The out-of-candidate case needs a candidate with no project package of its own: the
        # wrapper prepends the candidate's import roots (issue:8b39e60e4df361c6138e), so a
        # candidate carrying the package imports it whatever else the environment offers.
        elsewhere = self._init_workspace(Path(self.tmpdir.name) / "no-package", project_package="")
        cases = {
            "inside": (self.root, self._suite("insidesuite", "print('in')\n"), None),
            "outside": (
                elsewhere,
                self._suite("outsidecase", "import ummanu\nprint('out')\n", root=elsewhere),
                dict(
                    os.environ,
                    PYTHONSAFEPATH="1",
                    PYTHONPATH=os.pathsep.join([str(outside), str(elsewhere)]),
                ),
            ),
            "shell": (self.root, CheckSpec.for_shell("echo shell"), None),
        }
        for name, (root, spec, env) in cases.items():
            with self.subTest(case=name):
                run_broad_check(spec, root=root, stream=self.stream, **({"env": env} if env else {}))
                lookup = usable_receipt(root, spec)
                argv = ["check", "broad", "--root", str(root), "--reuse"]
                argv += ["--module", spec.module] if spec.shape == "module" else ["--command", spec.command]
                stdout = StringIO()
                with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", StringIO()):
                    main(argv)
                reused = json.loads(stdout.getvalue())["reused"]

                self.assertEqual(reused, lookup.usable, f"{name}: --reuse and the predicate disagree")
                self.assertEqual(lookup.usable, lookup.authorized() is not None)
                if name == "outside":
                    self.assertFalse(lookup.usable, "the outside case must still be refused")
                    self.assertIn("outside this candidate workspace", lookup.reason)


class CandidateImportPrecedenceTests(BroadCheckTestCase):
    """issue:8b39e60e4df361c6138e: the standard shape must import the candidate, by construction.

    This reproduces the live shape exactly, because nothing weaker reproduced the defect. A head's
    shell carries `PYTHONPATH=$UMMANU_REPO/src` (`ummanu/runtime/launch_prefix.py`)
    and every worktree runs on one shared venv that holds an editable install of the production
    checkout, so a src-layout candidate -- which has nothing importable at its own root -- was
    checked by a process that imported *production* sources and ran the candidate's test files
    against them. It printed OK and exited 0. `candidate_import_refusal()` then honestly refused
    the receipt, so `--reuse` was dead for this project and every round paid for the whole suite
    again: the wrapper attested the wrong tree and the worker got no reuse out of it either.

    The bootstrap used to import the configured package first and only afterwards *append* the
    workspace root to `sys.path` -- an append, of a root that a src-layout project has nothing
    under, after the import had already bound the module. Now the candidate's import roots go to
    the front, before the import.
    """

    def _src_layout_checkout(self, path: Path, name: str) -> Path:
        """A src-layout checkout of "the project": nothing importable at its root."""
        (path / "src" / "ummanu").mkdir(parents=True)
        (path / "src" / "ummanu" / "__init__.py").write_text(f"NAME = {name!r}\n", encoding="utf-8")
        return path

    def test_the_live_head_shape_checks_the_candidate_and_not_the_production_checkout(self) -> None:
        production = self._src_layout_checkout(Path(self.tmpdir.name) / "production", "production")
        candidate = self._init_workspace(Path(self.tmpdir.name) / "candidate", project_package="")
        self._src_layout_checkout(candidate, "candidate")
        # The log lives outside the candidate: writing into the checkout would change the very
        # content identity the receipt is about, and reuse would be refused for that instead.
        log = self.scripts / "runs.log"
        suite = self._suite(
            "livesuite",
            "import ummanu\n"
            f"open({str(log)!r}, 'a', encoding='utf-8').write(ummanu.__file__ + '\\n')\n"
            "print(ummanu.NAME)\n",
            root=candidate,
        )
        _git(candidate, "add", "-A")
        _git(candidate, "commit", "-q", "-m", "a src-layout candidate")
        # Exactly what a head hands to the check: the control plane's own sources on PYTHONPATH.
        env = dict(os.environ, PYTHONPATH=str(production / "src"))

        exit_code, receipt = run_broad_check(suite, root=candidate, stream=self.stream, env=env)

        provenance = receipt["project_provenance"]
        self.assertEqual(exit_code, 0)
        self.assertEqual(receipt["tail"].strip(), "candidate")
        self.assertEqual(
            provenance["imported_project"],
            str((candidate / "src" / "ummanu" / "__init__.py").resolve()),
        )
        self.assertTrue(provenance["inside_workspace"])
        self.assertEqual(
            provenance["import_roots"],
            [str(candidate.resolve()), str((candidate / "src").resolve())],
        )
        # The receipt therefore attests this checkout, and reuse is alive again.
        lookup = usable_receipt(candidate, suite)
        self.assertTrue(lookup.usable, lookup.reason)
        self.assertIsNotNone(lookup.authorized())

        reused = _run_main(["check", "broad", "--root", str(candidate), "--reuse", "--module", "livesuite"])
        self.assertTrue(reused["reused"])
        self.assertEqual(
            log.read_text(encoding="utf-8").splitlines(),
            [str((candidate / "src" / "ummanu" / "__init__.py").resolve())],
            "the suite must have run exactly once: the second call reused the receipt",
        )

    def test_existing_candidate_roots_are_promoted_once_ahead_of_production(self) -> None:
        production = self._src_layout_checkout(Path(self.tmpdir.name) / "production", "production")
        candidate = self._init_workspace(Path(self.tmpdir.name) / "candidate", project_package="")
        self._src_layout_checkout(candidate, "candidate")
        log = self.scripts / "normalized-path.json"
        suite = self._suite(
            "pathsuite",
            "import json, os, ummanu, sys\n"
            f"open({str(log)!r}, 'w', encoding='utf-8').write(json.dumps({{\n"
            "    'imported': ummanu.__file__,\n"
            "    'pythonpath': os.environ.get('PYTHONPATH'),\n"
            "    'sys_path': sys.path,\n"
            "}))\n",
            root=candidate,
        )
        _git(candidate, "add", "-A")
        _git(candidate, "commit", "-q", "-m", "a src-layout candidate")
        candidate_root = str(candidate.resolve())
        candidate_src = str((candidate / "src").resolve())
        production_src = str((production / "src").resolve())
        inherited_pythonpath = os.pathsep.join([production_src, candidate_src, candidate_src, candidate_root])

        exit_code, receipt = run_broad_check(
            suite,
            root=candidate,
            stream=self.stream,
            env={**os.environ, "PYTHONPATH": inherited_pythonpath},
        )

        self.assertEqual(exit_code, 0, receipt["tail"])
        observed = json.loads(log.read_text(encoding="utf-8"))
        self.assertEqual(observed["pythonpath"], inherited_pythonpath)
        self.assertEqual(observed["sys_path"][:2], [candidate_root, candidate_src])
        self.assertEqual(observed["sys_path"].count(candidate_root), 1)
        self.assertEqual(observed["sys_path"].count(candidate_src), 1)
        self.assertIn(production_src, observed["sys_path"][2:])
        self.assertEqual(
            observed["imported"],
            str((candidate / "src" / "ummanu" / "__init__.py").resolve()),
        )
        self.assertEqual(receipt["project_provenance"]["imported_project"], observed["imported"])
        self.assertTrue(receipt["project_provenance"]["inside_workspace"])

    def test_the_recorded_roots_are_the_candidate_s_own_root_and_src(self) -> None:
        """The argv slot and the receipt field say the same thing, and neither is a workspace path
        dressed up as evidence: `inside_workspace` is still computed from the observed import."""
        spec = self._suite("rootsuite", "print('ran')\n")

        _, receipt = self._run(spec)

        self.assertEqual(
            receipt["argv"][6],
            "<candidate import roots>",
            "the displayed argv must keep the same shape as the real one",
        )
        self.assertEqual(
            receipt["project_provenance"]["import_roots"],
            broad_check.candidate_import_roots(self.root),
        )
        self.assertEqual(
            broad_check.candidate_import_roots(self.root),
            [str(self.root.resolve()), str((self.root / "src").resolve())],
        )

    def test_a_shell_check_records_no_import_roots_at_all(self) -> None:
        _, receipt = self._run("echo shell")

        self.assertEqual(receipt["project_provenance"]["import_roots"], [])
        self.assertEqual(receipt["project_provenance"]["origin"], "unobservable")


class RegisteredProjectContractTests(BroadCheckTestCase):
    """A non-Ummanu project owns both sides of reusable module provenance."""

    def _register(self, *, interpreter: str, import_package: str, module: str = "project_suite") -> Path:
        instance = Path(self.tmpdir.name) / "instance"
        (instance / "projects").mkdir(parents=True)
        (instance / "adapters").mkdir()
        (instance / "projects" / "example.yaml").write_text(
            f"id: example\nrepo: {self.root}\nadapter: example\nenabled: true\n",
            encoding="utf-8",
        )
        (instance / "adapters" / "example.yaml").write_text(
            "setup:\n  commands: ['true']\n"
            "smoke:\n  command: 'true'\n"
            "validation:\n  ci: github\n"
            "artifact_policy:\n  write_project_files: false\n"
            "broad_check:\n"
            f"  interpreter: {interpreter}\n"
            f"  import_package: {import_package}\n"
            f"  module: {module}\n",
            encoding="utf-8",
        )
        return instance

    def test_registered_non_ummanu_project_reuses_its_green_module_receipt(self) -> None:
        # The legacy fixture has a Ummanu package only for the old default contract. This
        # candidate deliberately has none: its adapter names the project package instead.
        shutil.rmtree(self.root / "ummanu")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "remove Ummanu package")
        (self.root / "codegen_orchestrator").mkdir()
        (self.root / "codegen_orchestrator" / "__init__.py").write_text(
            "NAME = 'candidate'\n", encoding="utf-8"
        )
        (self.root / "project_suite.py").write_text(
            "import codegen_orchestrator\nprint(codegen_orchestrator.NAME)\n", encoding="utf-8"
        )
        interpreter = self.root / ".venv" / "bin" / "python"
        interpreter.parent.mkdir(parents=True)
        interpreter.symlink_to(sys.executable)
        instance = self._register(interpreter=".venv/bin/python", import_package="codegen_orchestrator")
        argv = [
            "check",
            "broad",
            "--root",
            str(self.root),
            "--instance",
            str(instance),
            "--reuse",
            "--module",
            "project_suite",
        ]

        first = _run_main(argv)
        receipt = first["receipt"]
        provenance = receipt["project_provenance"]
        self.assertFalse(first["reused"])
        self.assertEqual(receipt["check_set"]["interpreter"], str(interpreter))
        self.assertEqual(receipt["check_set"]["import_package"], "codegen_orchestrator")
        self.assertEqual(provenance["imported_package"], "codegen_orchestrator")
        self.assertTrue(provenance["imported_project"].startswith(str(self.root.resolve())))
        self.assertTrue(provenance["inside_workspace"])

        self.assertEqual(
            _status(
                [
                    "check",
                    "show",
                    "--root",
                    str(self.root),
                    "--instance",
                    str(instance),
                    "--module",
                    "project_suite",
                ]
            ),
            0,
        )
        second = _run_main(argv)
        self.assertTrue(second["reused"])

    def test_missing_configured_interpreter_is_a_structured_cli_error(self) -> None:
        instance = self._register(interpreter=".venv/bin/python", import_package="codegen_orchestrator")
        source_root = Path(__file__).resolve().parents[1]
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "ummanu",
                "check",
                "broad",
                "--root",
                str(self.root),
                "--instance",
                str(instance),
                "--module",
                "project_suite",
            ],
            cwd=source_root,
            env={**os.environ, "PYTHONPATH": str(source_root / "src")},
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout, "")
        error = json.loads(completed.stderr)
        self.assertEqual(error["error"]["code"], "interpreter_start_failed")
        self.assertIn(".venv/bin/python", error["error"]["message"])
        spec = CheckSpec.for_module(
            "project_suite",
            interpreter=str(self.root / ".venv" / "bin" / "python"),
            import_package="codegen_orchestrator",
        )
        self.assertFalse(receipt_path(self.root, spec).exists())

    def test_other_interpreter_os_errors_follow_the_same_cli_contract(self) -> None:
        instance = self._register(interpreter=".venv/bin/python", import_package="codegen_orchestrator")
        argv = [
            "check",
            "broad",
            "--root",
            str(self.root),
            "--instance",
            str(instance),
            "--module",
            "project_suite",
        ]
        stdout = StringIO()
        stderr = StringIO()
        with (
            mock.patch(
                "ummanu.broad_check.subprocess.Popen", side_effect=OSError(ENOEXEC, "Exec format error")
            ),
            mock.patch("sys.stdout", stdout),
            mock.patch("sys.stderr", stderr),
        ):
            status = main(argv)

        self.assertEqual(status, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(json.loads(stderr.getvalue())["error"]["code"], "interpreter_start_failed")

    def test_the_cli_default_for_an_unregistered_checkout_still_runs_and_says_so(self) -> None:
        """The path this issue deliberately left working, pinned so it cannot fall out by accident.

        A workspace that matches NO registered project has no adapter to have declared anything:
        it is somebody running `ummanu check broad --module ...` in a plain clone by hand, with
        no card, no workspace and no round at stake. That keeps the CLI's own default
        (`sys.executable` importing `ummanu`), and the response names the fallback rather than
        letting it pass for a project contract - `source: cli_default`, not `adapter`. The third
        case this test used to carry, a REGISTERED project whose adapter declared no `broad_check`,
        is no longer a fallback at all; it is the `broad_check_not_declared` refusal below.
        """
        (self.root / "legacysuite.py").write_text("print('legacy')\n", encoding="utf-8")
        disabled = self._register(interpreter=".venv/bin/python", import_package="codegen_orchestrator")
        binding = disabled / "projects" / "example.yaml"
        binding.write_text(
            binding.read_text(encoding="utf-8").replace("enabled: true", "enabled: false"), encoding="utf-8"
        )
        no_contract = Path(self.tmpdir.name) / "no-contract"
        (no_contract / "projects").mkdir(parents=True)
        (no_contract / "adapters").mkdir()
        (no_contract / "projects" / "example.yaml").write_text(
            f"id: example\nrepo: {self.root}\nadapter: example\nenabled: true\n",
            encoding="utf-8",
        )
        (no_contract / "adapters" / "example.yaml").write_text(
            "setup:\n  commands: ['true']\n"
            "smoke:\n  command: 'true'\n"
            "validation:\n  ci: github\n"
            "artifact_policy:\n  write_project_files: false\n",
            encoding="utf-8",
        )
        cases = {
            "no_project_binding": Path(self.tmpdir.name) / "missing-instance",
            "project_binding_disabled": disabled,
        }
        for reason, instance in cases.items():
            with self.subTest(reason=reason):
                payload = _run_main(
                    [
                        "check",
                        "broad",
                        "--root",
                        str(self.root),
                        "--instance",
                        str(instance),
                        "--module",
                        "legacysuite",
                    ]
                )
                self.assertEqual(
                    payload["module_contract"],
                    {
                        "source": "cli_default",
                        "reason": reason,
                    },
                )

    def test_a_registered_project_that_declares_no_contract_is_refused_before_any_run(self) -> None:
        """The new shape on the CLI's own error path, which behaves like every other refusal.

        This supersedes the test that asserted `cannot_attest_project` here. That refusal was
        reached through Ummanu's default lent to a project that never declared it, and its
        message named the substituted package instead of the real cause. The refusal is now
        `broad_check_not_declared`, read off the same enumeration the dispatcher's preflight reads,
        and the CLI contract around it is unchanged: exit 2, the structured
        `invalid_project_adapter` code on stderr, no receipt document and no run.
        """
        (self.root / "project_suite.py").write_text("print('suite')\n", encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "a suite to name")
        no_contract = Path(self.tmpdir.name) / "no-contract"
        (no_contract / "projects").mkdir(parents=True)
        (no_contract / "adapters").mkdir()
        (no_contract / "projects" / "example.yaml").write_text(
            f"id: example\nrepo: {self.root}\nadapter: example\nenabled: true\n", encoding="utf-8"
        )
        (no_contract / "adapters" / "example.yaml").write_text(
            "setup:\n  commands: ['true']\n"
            "smoke:\n  command: 'true'\n"
            "validation:\n  ci: github\n"
            "artifact_policy:\n  write_project_files: false\n",
            encoding="utf-8",
        )
        argv = [
            "check",
            "broad",
            "--root",
            str(self.root),
            "--instance",
            str(no_contract),
            "--module",
            "project_suite",
        ]
        stdout, stderr = StringIO(), StringIO()

        with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
            status = main(argv)

        self.assertEqual(status, 2)
        self.assertEqual(stdout.getvalue(), "", "no receipt document: nothing ran")
        error = json.loads(stderr.getvalue())["error"]
        self.assertEqual(error["code"], "invalid_project_adapter")
        self.assertIn("declares no broad-check contract", error["message"])
        self.assertIn("example", error["message"], "the project and the adapter are both named")
        self.assertFalse(broad_check.receipt_dir(self.root).exists())

    def test_installed_copy_inside_configured_venv_is_not_candidate_provenance(self) -> None:
        # A candidate whose sources sit at neither of its import roots -- here a `lib/` layout --
        # has nothing for the wrapper's prepended roots to find, so its configured venv imports an
        # installed copy that happens to live under the candidate. That must not become reusable:
        # an import out of the interpreter environment attests the environment, not this checkout.
        #
        # The fixture used to put the package in `src/` and prove the same thing about a src-layout
        # project. Since issue:8b39e60e4df361c6138e that shape cannot arise: `src/` is one of the
        # candidate import roots the wrapper prepends, so a src-layout candidate imports itself and
        # the installed copy never wins. The invariant under test is unchanged; only the layout
        # that can still reach it is.
        (self.root / ".gitignore").write_text(
            (self.root / ".gitignore").read_text(encoding="utf-8") + ".venv/\n",
            encoding="utf-8",
        )
        venv = self.root / ".venv"
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
        interpreter = venv / "bin" / "python"
        site_packages = Path(
            subprocess.check_output(
                [str(interpreter), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                text=True,
            ).strip()
        )
        package = site_packages / "codegen_orchestrator"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("NAME = 'installed'\n", encoding="utf-8")
        (self.root / "lib").mkdir()
        (self.root / "lib" / "codegen_orchestrator").mkdir()
        (self.root / "lib" / "codegen_orchestrator" / "__init__.py").write_text(
            "NAME = 'candidate'\n", encoding="utf-8"
        )
        (self.root / "installed_suite.py").write_text(
            "import codegen_orchestrator\nprint(codegen_orchestrator.NAME)\n", encoding="utf-8"
        )
        spec = CheckSpec.for_module(
            "installed_suite", interpreter=str(interpreter), import_package="codegen_orchestrator"
        )

        # This is how the supported source-checkout invocation reaches the wrapper.  Without an
        # environment boundary, the child reinterprets its inherited relative `lib` entry from
        # this fixture's cwd and imports the candidate instead of the configured installed copy.
        _, receipt = self._run(spec, env={**os.environ, "PYTHONPATH": "lib"})

        provenance = receipt["project_provenance"]
        self.assertEqual(provenance["environment_prefix"], str(venv))
        self.assertTrue(provenance["imported_project"].startswith(str(site_packages)))
        self.assertEqual(receipt["tail"], "installed")
        lookup = usable_receipt(self.root, spec)
        self.assertFalse(lookup.usable)
        self.assertIn("interpreter environment", lookup.reason)


class DeclaredBroadSuiteTests(BroadCheckTestCase):
    """issue:8b39e60e4df361c6138e: the adapter names the suite, so the flag does not have to.

    Before this, `--module` was mandatory and nothing in the registry could say what the project's
    broad suite was. The worker task packet printed the literal placeholder
    `<this project's broad suite module>` and every document answered it with repository-wide
    discovery — every CI suite in one process. A contract that names the suite is what lets
    `check broad --reuse` and `check show` mean one exact thing per project.
    """

    def _register(self, block: str) -> Path:
        instance = Path(self.tmpdir.name) / "instance"
        (instance / "projects").mkdir(parents=True)
        (instance / "adapters").mkdir()
        (instance / "projects" / "example.yaml").write_text(
            f"id: example\nrepo: {self.root}\nadapter: example\nenabled: true\n",
            encoding="utf-8",
        )
        (instance / "adapters" / "example.yaml").write_text(
            "setup:\n  commands: ['true']\n"
            "smoke:\n  command: 'true'\n"
            "validation:\n  ci: github\n"
            "artifact_policy:\n  write_project_files: false\n" + block,
            encoding="utf-8",
        )
        return instance

    def _suite_file(self, name: str) -> None:
        (self.root / f"{name}.py").write_text(
            "import sys\nimport ummanu\nprint('ran ' + ' '.join(sys.argv[1:]))\n",
            encoding="utf-8",
        )
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", f"add {name}")

    def test_a_declared_module_and_args_resolve_to_the_exact_argument_vector(self) -> None:
        self._suite_file("project_suite")
        instance = self._register(
            "broad_check:\n"
            "  import_package: ummanu\n"
            "  module: project_suite\n"
            "  args: ['--only', 'fast lane']\n"
        )

        payload = _run_main(["check", "broad", "--root", str(self.root), "--instance", str(instance)])

        self.assertEqual(payload["receipt"]["check_set"]["module"], "project_suite")
        # The vector is a list, never a rendered string: `'fast lane'` is one argument.
        self.assertEqual(payload["receipt"]["check_set"]["args"], ["--only", "fast lane"])
        self.assertEqual(payload["receipt"]["tail"], "ran --only fast lane")
        self.assertEqual(payload["module_contract"], {"source": "adapter"})

    def test_a_declared_contract_with_no_interpreter_accepts_the_dispatcher_candidate_runtime(
        self,
    ) -> None:
        """The production wrapper may select a workspace runtime for the inner project suite."""
        self._suite_file("project_suite")
        instance = self._register("broad_check:\n  import_package: ummanu\n  module: project_suite\n")
        candidate = self.root / ".ummanu-task-env" / "venv"
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(candidate)], check=True)

        payload = _run_main(
            [
                "check",
                "broad",
                "--root",
                str(self.root),
                "--instance",
                str(instance),
                "--default-interpreter",
                ".ummanu-task-env/venv/bin/python3",
            ]
        )

        candidate_python = str(candidate / "bin" / "python3")
        self.assertEqual(payload["receipt"]["check_set"]["interpreter"], candidate_python)
        self.assertEqual(payload["receipt"]["project_provenance"]["python"], candidate_python)
        self.assertEqual(payload["receipt"]["check_set"]["module"], "project_suite")
        # And it really did import the candidate, not whatever this interpreter's environment holds.
        self.assertTrue(
            payload["receipt"]["project_provenance"]["inside_workspace"],
            payload["receipt"]["project_provenance"],
        )

    def test_a_direct_check_with_no_interpreter_still_uses_the_callers_runtime(self) -> None:
        self._suite_file("project_suite")
        instance = self._register("broad_check:\n  import_package: ummanu\n  module: project_suite\n")

        payload = _run_main(["check", "broad", "--root", str(self.root), "--instance", str(instance)])

        self.assertEqual(payload["receipt"]["check_set"]["interpreter"], sys.executable)

    def test_the_cli_refuses_a_subprocess_status_that_disagrees_with_its_receipt(self) -> None:
        spec = self._suite("project_suite", "print('OK')\n")
        _, receipt = self._run(spec)
        instance = self._register("broad_check:\n  import_package: ummanu\n  module: project_suite\n")
        stdout, stderr = StringIO(), StringIO()

        with (
            mock.patch("sys.stdout", stdout),
            mock.patch("sys.stderr", stderr),
            mock.patch("ummanu.check_commands.run_broad_check", return_value=(7, receipt)),
        ):
            status = main(["check", "broad", "--root", str(self.root), "--instance", str(instance)])

        self.assertEqual(status, 2)
        self.assertEqual(stdout.getvalue(), "")
        error = json.loads(stderr.getvalue())["error"]
        self.assertEqual(error["code"], "receipt_status_mismatch")
        self.assertIn("exit code 7", error["message"])

    def test_check_show_reads_back_the_receipt_the_declared_suite_wrote(self) -> None:
        self._suite_file("project_suite")
        instance = self._register("broad_check:\n  import_package: ummanu\n  module: project_suite\n")
        common = ["--root", str(self.root), "--instance", str(instance)]

        _run_main(["check", "broad", *common])
        shown = _run_main(["check", "show", *common])

        self.assertTrue(shown["usable"], shown)
        reused = _run_main(["check", "broad", "--reuse", *common])
        self.assertTrue(reused["reused"])

    def test_an_explicit_module_cannot_replace_the_declared_profile(self) -> None:
        # ummanu-161 changes this contract: an arbitrary module must no longer earn a receipt
        # for a registered profile. Subsets need an explicit local membership declaration.
        self._suite_file("project_suite")
        self._suite_file("other_suite")
        instance = self._register(
            "broad_check:\n"
            "  import_package: ummanu\n"
            "  module: project_suite\n"
            "  args: ['--only', 'fast lane']\n"
        )
        stdout, stderr = StringIO(), StringIO()
        with (
            mock.patch("sys.stdout", stdout),
            mock.patch("sys.stderr", stderr),
            mock.patch("ummanu.check_commands.run_broad_check") as run,
        ):
            status = main(
                [
                    "check",
                    "broad",
                    "--root",
                    str(self.root),
                    "--instance",
                    str(instance),
                    "--module",
                    "other_suite",
                ]
            )
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(stderr.getvalue())["error"]["code"], "local_check_override")
        self.assertEqual(stdout.getvalue(), "")
        run.assert_not_called()
        self.assertFalse(broad_check.receipt_dir(self.root).exists())

    def test_a_project_that_declares_no_module_and_is_given_none_is_refused_by_name(self) -> None:
        # A declared contract that names no suite - the shape every adapter was written in before
        # `module` existed. The fixture used to declare no `broad_check` at all, which is now a
        # refusal of its own (`broad_check_not_declared`) and would never reach this branch.
        instance = self._register("broad_check:\n  import_package: ummanu\n")

        stderr = StringIO()
        with mock.patch("sys.stdout", StringIO()), mock.patch("sys.stderr", stderr):
            status = main(["check", "broad", "--root", str(self.root), "--instance", str(instance)])

        self.assertEqual(status, 2)
        error = json.loads(stderr.getvalue())["error"]
        self.assertEqual(error["code"], "no_broad_check_module")
        self.assertIn("--module", error["message"])

    def test_module_and_command_are_still_two_different_promises(self) -> None:
        instance = self._register("broad_check:\n  import_package: ummanu\n  module: project_suite\n")

        stderr = StringIO()
        with mock.patch("sys.stdout", StringIO()), mock.patch("sys.stderr", stderr):
            status = main(
                [
                    "check",
                    "broad",
                    "--root",
                    str(self.root),
                    "--instance",
                    str(instance),
                    "--module",
                    "a",
                    "--command",
                    "true",
                ]
            )

        self.assertEqual(status, 2)
        self.assertEqual(json.loads(stderr.getvalue())["error"]["code"], "usage")

    def test_module_args_without_a_module_flag_still_names_that_error(self) -> None:
        """The declared suite owns its argument vector, so `--module-arg` still needs `--module`.

        Silently appending a worker's ad-hoc argument to the contract's vector would run a check
        nobody declared while the receipt claimed the declared one.
        """
        instance = self._register("broad_check:\n  import_package: ummanu\n  module: project_suite\n")

        stderr = StringIO()
        with mock.patch("sys.stdout", StringIO()), mock.patch("sys.stderr", stderr):
            status = main(
                [
                    "check",
                    "broad",
                    "--root",
                    str(self.root),
                    "--instance",
                    str(instance),
                    "--module-arg",
                    "extra",
                ]
            )

        self.assertEqual(status, 2)
        self.assertEqual(json.loads(stderr.getvalue())["error"]["code"], "module_arg_without_module")

    def test_a_declared_contract_without_a_module_is_a_usage_error_not_a_refusal(self) -> None:
        """The CLI half of the #330 regression: no module declared, none passed, no refusal.

        The adapter here is the shape every project on the installation was written in before
        `module` existed. The contract itself is fine — it names a runtime and an import package —
        so nothing is refused; what is missing is only the suite, and only this invocation needs
        it. The error therefore names itself and says both ways out.
        """
        instance = self._register("broad_check:\n  import_package: ummanu\n")

        stderr = StringIO()
        with mock.patch("sys.stdout", StringIO()), mock.patch("sys.stderr", stderr):
            status = main(["check", "broad", "--root", str(self.root), "--instance", str(instance)])

        self.assertEqual(status, 2)
        error = json.loads(stderr.getvalue())["error"]
        self.assertEqual(error["code"], "no_broad_check_module")
        self.assertIn("--module", error["message"])
        self.assertIn("broad_check.module", error["message"])

    def test_check_show_without_a_module_says_the_same_thing(self) -> None:
        instance = self._register("broad_check:\n  import_package: ummanu\n")

        stderr = StringIO()
        with mock.patch("sys.stdout", StringIO()), mock.patch("sys.stderr", stderr):
            status = main(["check", "show", "--root", str(self.root), "--instance", str(instance)])

        self.assertEqual(status, 2)
        self.assertEqual(json.loads(stderr.getvalue())["error"]["code"], "no_broad_check_module")

    def test_a_module_flag_still_runs_a_contract_that_declares_no_suite(self) -> None:
        """And naming the suite is all it takes: the adapter's runtime is used, as it always was."""
        self._suite_file("project_suite")
        instance = self._register("broad_check:\n  import_package: ummanu\n")

        payload = _run_main(
            [
                "check",
                "broad",
                "--root",
                str(self.root),
                "--instance",
                str(instance),
                "--module",
                "project_suite",
            ]
        )

        self.assertEqual(payload["receipt"]["check_set"]["module"], "project_suite")
        self.assertEqual(payload["module_contract"], {"source": "adapter"})

    def test_a_declared_block_with_a_blank_interpreter_is_broad_check_incomplete(self) -> None:
        """Present but blank names nothing runnable — a typo, and a refusal since before #330."""
        instance = self._register("broad_check:\n  interpreter: '   '\n  import_package: ummanu\n")

        stderr = StringIO()
        with mock.patch("sys.stdout", StringIO()), mock.patch("sys.stderr", stderr):
            status = main(
                [
                    "check",
                    "broad",
                    "--root",
                    str(self.root),
                    "--instance",
                    str(instance),
                    "--module",
                    "project_suite",
                ]
            )

        self.assertEqual(status, 2)
        # The adapter is unusable, which the CLI has always reported under this code.
        self.assertEqual(json.loads(stderr.getvalue())["error"]["code"], "invalid_project_adapter")


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


class DispatcherWorkspaceReceiptTests(BroadCheckTestCase):
    """secretary-1920: in a dispatcher workspace the receipt is generated output in the owned namespace."""

    def setUp(self) -> None:
        super().setUp()
        self.namespace = self.root / ".ummanu-task-env"
        self.namespace.mkdir()
        self._claim(str(self.root.resolve()))
        exclude = self.root / ".git" / "info" / "exclude"
        exclude.write_text(".ummanu-task-env/\n", encoding="utf-8")
        self.suite = self._suite("ownedsuite", "print('ran')\n")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-q", "-m", "suite")

    def _claim(self, workspace: str) -> None:
        owner = {"owner": "ummanu-dispatcher", "schema_version": 1, "workspace": workspace}
        (self.namespace / "owner.json").write_text(json.dumps(owner) + "\n", encoding="utf-8")

    def _main(self, argv: list[str]) -> tuple[int, dict]:
        stdout = StringIO()
        # The cache redirections every dispatcher-launched worker and reviewer runs with.
        caches = workspace_tool_cache_env(self.root)
        with (
            mock.patch("sys.stdout", stdout),
            mock.patch("sys.stderr", StringIO()),
            mock.patch.dict(os.environ, caches),
        ):
            code = main(argv)
        return code, json.loads(stdout.getvalue())

    def test_writer_show_and_reuse_all_use_the_owned_namespace(self) -> None:
        expected = self.namespace.resolve() / "checks" / f"broad-{self.suite.digest[:16]}.json"
        argv = ["--root", str(self.root), "--module", "ownedsuite"]

        code, written = self._main(["check", "broad", *argv])
        self.assertEqual(code, 0)
        self.assertFalse(written["reused"])
        self.assertEqual(written["path"], str(expected))
        self.assertTrue(expected.is_file())
        self.assertFalse((self.root / "state").exists())

        code, shown = self._main(["check", "show", *argv])
        self.assertEqual(code, 0, shown)
        self.assertTrue(shown["usable"])
        self.assertEqual(shown["path"], str(expected))

        code, reused = self._main(["check", "broad", "--reuse", *argv])
        self.assertEqual(code, 0)
        self.assertTrue(reused["reused"])
        self.assertEqual(reused["path"], str(expected))
        self.assertEqual(usable_receipt(self.root, self.suite).path, expected)

        # Nothing outside the owned namespace is left behind for cleanup to read as work.
        status = _git_out(self.root, "status", "--porcelain=v1", "--ignored", "--untracked-files=all")
        written = sorted({line[3:].split("/")[0] for line in status.splitlines()})
        self.assertEqual(written, [".ummanu-task-env"])

    def test_a_namespace_the_dispatcher_does_not_own_keeps_the_ordinary_location(self) -> None:
        self._claim(str(self.root.resolve() / "elsewhere"))

        code, payload = self._main(["check", "broad", "--root", str(self.root), "--module", "ownedsuite"])

        self.assertEqual(code, 0)
        ordinary = self.root / "state" / "checks" / f"broad-{self.suite.digest[:16]}.json"
        self.assertEqual(payload["path"], str(ordinary))
        self.assertFalse((self.namespace / "checks").exists())
