"""Run the manifest's unit and component suites with package-owned hermetic defaults.

The manifest parser is shared with CI; invalid or incomplete membership fails loudly.
The other seven suites stay in exact-SHA CI. See docs/TESTING.md for the profile contract.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / "tests" / "ci-shards.txt"
SHARD_RUNNER = REPO_ROOT / "scripts" / "ci_test_shards.py"

# The suites this project's local broad profile is composed of, in the order they run.
BROAD_SUITES = ("unit", "component")


def _shard_runner() -> ModuleType:
    """The CI shard runner, imported by path so the manifest has exactly one parser.

    ``scripts/`` has no ``__init__.py`` and is not on ``sys.path``, so an ordinary import is not
    available. Loading the file directly is deliberate: the alternative is a second copy of the
    manifest parsing here, which is the one thing this module exists to avoid. The runner is
    ``__main__``-guarded, so importing it defines constants and functions and runs nothing.
    """
    spec = importlib.util.spec_from_file_location("ummanu_ci_test_shards", SHARD_RUNNER)
    if spec is None or spec.loader is None:  # pragma: no cover - a missing runner is a broken tree
        raise RuntimeError(f"the CI shard runner is unavailable at {SHARD_RUNNER}")
    cached = sys.modules.get(spec.name)
    if cached is not None:
        return cached
    module = importlib.util.module_from_spec(spec)
    # Registered before execution because the runner defines dataclasses, and `dataclasses` looks
    # its own module up in `sys.modules` while processing a class.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[spec.name]
        raise
    return module


def broad_modules() -> list[str]:
    """The dotted test modules of `BROAD_SUITES`, straight from the validated manifest.

    Any manifest problem — unknown suite name, an unclaimed or stale test file, a duplicate, an
    empty suite, an unreadable file — comes out of here as an exception. Running a silently smaller
    set would make the receipt this suite writes a claim about tests that were never executed.
    """
    runner = _shard_runner()
    grouped = runner.load_manifest(REPO_ROOT, MANIFEST)
    missing = [suite for suite in BROAD_SUITES if suite not in grouped]
    if missing:
        raise runner.ManifestError(
            f"{MANIFEST}: the local broad profile names suites the manifest does not define: "
            f"{', '.join(missing)}"
        )
    selected: list[str] = []
    for suite in BROAD_SUITES:
        selected.extend(runner.modules(grouped[suite]))
    return selected


#: Options whose value is a separate argument, so that value is not a test name.
_OPTIONS_TAKING_A_VALUE = frozenset({"-k", "--testNamePatterns", "-p", "--pattern", "-s", "-t"})


def _names_tests(arguments: list[str]) -> bool:
    """Whether the argument vector already names tests to run.

    `unittest.main(module=None, ...)` cannot be trusted with `defaultTest` here: given no test
    names it ignores it and falls into repository-wide discovery, which is the 402-second run this
    module exists to replace — and it would do it while printing nothing to say it had. So the
    module names are put into the argument vector explicitly, and only when the caller supplied
    none of their own.
    """
    expecting_value = False
    for argument in arguments:
        if expecting_value:
            expecting_value = False
            continue
        if argument.startswith("-"):
            expecting_value = argument in _OPTIONS_TAKING_A_VALUE
            continue
        return True
    return False


def main(argv: list[str] | None = None) -> int:
    from ummanu.broad_check import BroadCheckError
    from ummanu.projects.local_check import LocalProfile
    from ummanu.test_timing import TimingRunner, summary

    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        grouped = _shard_runner().load_manifest(REPO_ROOT, MANIFEST)
        profile = LocalProfile(
            "unittest", {path: shard for shard, paths in grouped.items() for path in paths}, BROAD_SUITES
        )
        expecting_value = False
        for index, argument in enumerate(arguments):
            if expecting_value:
                expecting_value = False
            elif argument.startswith("-"):
                expecting_value = argument in _OPTIONS_TAKING_A_VALUE
            else:
                _path, arguments[index] = profile.select(argument)
        selected = [] if _names_tests(arguments) else profile.full_args()
    except (BroadCheckError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    program = unittest.main(
        module=None,
        argv=["python -m tests.broad", *arguments, *selected],
        exit=False,
        testRunner=TimingRunner,
    )
    observation = program.result.observation()
    if timing_path := os.environ.get("UMMANU_TEST_TIMING_RECORD"):
        Path(timing_path).write_text(json.dumps(observation), encoding="utf-8")
    print(summary(observation), file=sys.stderr)
    return 0 if program.result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
