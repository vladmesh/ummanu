"""issue:8b39e60e4df361c6138e: the local broad suite is exactly the manifest's `unit` + `component`.

`tests/broad.py` exists so the broad-check contract has a suite to name. Its whole value depends on
two properties that are easy to lose quietly, so both are pinned here:

* it runs the modules the manifest assigns to `unit` and `component`, all of them and nothing else —
  a second hand-written list would have drifted the first time a test file was added; and
* an unusable manifest raises rather than producing a smaller set. A broad receipt is a claim about
  which tests ran, so a silently shrunken run is worse than a failed one.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest import mock

from tests import broad

REPO_ROOT = Path(__file__).resolve().parents[1]

OTHER_CI_SUITES = (
    "runtime-component",
    "integration-board",
    "integration-dispatcher",
    "integration-heads",
    "packaging",
    "integration-recovery",
    "integration-memory",
)


def _declared(manifest_text: str) -> list[tuple[str, str]]:
    """Every (suite, path) the manifest declares, read the plainest way rather than through the
    same parser the module under test uses."""
    entries = []
    for raw in manifest_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        suite, relative = line.split()
        entries.append((suite, relative))
    return entries


def _modules_of(manifest_text: str, suites: tuple[str, ...]) -> list[str]:
    grouped: dict[str, list[str]] = {suite: [] for suite in suites}
    for suite, relative in _declared(manifest_text):
        if suite in grouped:
            grouped[suite].append(relative.removesuffix(".py").replace("/", "."))
    return [module for suite in suites for module in grouped[suite]]


class BroadSuiteCompositionTests(unittest.TestCase):
    def test_the_local_broad_profile_is_unit_plus_component(self) -> None:
        self.assertEqual(broad.BROAD_SUITES, ("unit", "component"))

    def test_it_runs_exactly_the_modules_the_manifest_assigns_to_those_suites(self) -> None:
        expected = _modules_of(broad.MANIFEST.read_text(encoding="utf-8"), broad.BROAD_SUITES)

        self.assertTrue(expected, "the manifest defines no unit/component modules at all")
        self.assertEqual(broad.broad_modules(), expected)

    def test_it_leaves_the_other_five_ci_suites_to_the_exact_sha_gate(self) -> None:
        """The composition is a local profile, never a replacement for the seven-suite taxonomy."""
        manifest = broad.MANIFEST.read_text(encoding="utf-8")
        others = set(_modules_of(manifest, OTHER_CI_SUITES))

        self.assertTrue(others)
        self.assertEqual(set(broad.broad_modules()) & others, set())


class BroadSuiteInvocationTests(unittest.TestCase):
    """What `python -m tests.broad` hands to unittest, and what it must never hand it.

    This is not pedantry about an argument vector. `unittest.main(module=None, ...)` ignores
    `defaultTest` when the argument vector names no tests and silently falls back to
    repository-wide discovery — the 402-second, all-seven-suites run this profile replaces. The
    first version of this module did exactly that, ran 3812 tests, and printed nothing to say it
    had. The names go into the vector explicitly, and these pin that they do.
    """

    def _argv_for(self, arguments: list[str]) -> list[str]:
        with mock.patch.object(broad.unittest, "main") as fake:
            fake.return_value.result.wasSuccessful.return_value = True
            fake.return_value.result.observation.return_value = {"status": "complete", "tests": [], "modules": {}}
            broad.main(arguments)
        return fake.call_args.kwargs["argv"]

    def test_a_bare_invocation_names_the_manifest_modules_explicitly(self) -> None:
        argv = self._argv_for([])

        self.assertEqual(argv[1:], broad.broad_modules())

    def test_an_option_only_invocation_still_names_them(self) -> None:
        for arguments in (["-v"], ["-k", "a pattern"]):
            with self.subTest(arguments=arguments):
                argv = self._argv_for(list(arguments))

                self.assertEqual(argv[1 + len(arguments) :], broad.broad_modules())

    def test_an_explicit_test_name_replaces_the_profile_rather_than_joining_it(self) -> None:
        argv = self._argv_for(["tests.test_broad_suite"])

        self.assertEqual(argv[1:], ["tests.test_broad_suite"])


class BroadSuiteManifestFailureTests(unittest.TestCase):
    """A manifest this profile cannot trust has to stop the run, not shrink it."""

    def _in_a_copy(self, mutate: Callable[[Path], None]) -> subprocess.CompletedProcess:
        """Run `broad_modules()` in a throwaway checkout whose manifest `mutate` has broken.

        The copy holds only what the parser reads — the runner, the manifest and an empty file for
        every declared test — so the failure being asserted is the manifest's and not some import.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "checkout"
            (root / "scripts").mkdir(parents=True)
            (root / "tests").mkdir()
            (root / "src/ummanu/projects").mkdir(parents=True)
            shutil.copy(REPO_ROOT / "src/ummanu/projects/test_timing.py", root / "src/ummanu/projects")
            shutil.copy(REPO_ROOT / "scripts" / "ci_test_shards.py", root / "scripts")
            shutil.copy(REPO_ROOT / "tests" / "broad.py", root / "tests")
            (root / "tests" / "__init__.py").write_text("", encoding="utf-8")
            manifest = root / "tests" / "ci-shards.txt"
            text = broad.MANIFEST.read_text(encoding="utf-8")
            manifest.write_text(text, encoding="utf-8")
            for _, relative in _declared(text):
                (root / relative).write_text("", encoding="utf-8")
            mutate(manifest)
            probe = (
                "import sys; sys.path.insert(0, sys.argv[1]);"
                " import tests.broad as module; module.broad_modules()"
            )
            return subprocess.run(
                [sys.executable, "-c", probe, str(root)],
                check=False,
                capture_output=True,
                text=True,
            )

    def test_a_healthy_copy_is_the_control(self) -> None:
        outcome = self._in_a_copy(lambda manifest: None)

        self.assertEqual(outcome.returncode, 0, outcome.stderr)

    def test_an_unknown_suite_name_stops_the_run(self) -> None:
        def mutate(manifest: Path) -> None:
            text = manifest.read_text(encoding="utf-8")
            manifest.write_text(text.replace("unit tests/", "invented tests/", 1), encoding="utf-8")

        outcome = self._in_a_copy(mutate)

        self.assertNotEqual(outcome.returncode, 0)
        self.assertIn("unknown suite", outcome.stderr)

    def test_a_stale_entry_stops_the_run(self) -> None:
        def mutate(manifest: Path) -> None:
            text = manifest.read_text(encoding="utf-8")
            manifest.write_text(text + "unit tests/test_never_written.py\n", encoding="utf-8")

        outcome = self._in_a_copy(mutate)

        self.assertNotEqual(outcome.returncode, 0)
        self.assertIn("missing declared tests", outcome.stderr)

    def test_an_unclaimed_test_file_stops_the_run(self) -> None:
        def mutate(manifest: Path) -> None:
            (manifest.parent / "test_added_but_unclaimed.py").write_text("", encoding="utf-8")

        outcome = self._in_a_copy(mutate)

        self.assertNotEqual(outcome.returncode, 0)
        self.assertIn("unclaimed tests", outcome.stderr)

    def test_an_unreadable_manifest_stops_the_run(self) -> None:
        outcome = self._in_a_copy(lambda manifest: manifest.unlink())

        self.assertNotEqual(outcome.returncode, 0)
        self.assertIn("ci-shards.txt", outcome.stderr)


if __name__ == "__main__":
    unittest.main()
