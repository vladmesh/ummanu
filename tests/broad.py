"""The Ummanu project's local broad suite: the `unit` and `component` CI suites, and nothing else.

`python3 -m tests.broad` is the answer to the question the broad-check contract could not ask before
issue:8b39e60e4df361c6138e — which suite IS this project's broad suite. Until it existed, the worker
task packet printed the placeholder ``<this project's broad suite module>`` and every document
answered it with bare ``python3 -m unittest``: repository-wide discovery, all CI suites in one
process, 3782 tests and about 402 seconds. That is not a check a worker runs between edits, so in
practice it was either skipped or paid for once and reused past the point where it meant anything.

The composition here is the owner's decision, measured: `unit` (~58s) plus `component` (~19s), about
1440 tests in ~77s. The other seven suites — `runtime-component`, `integration-board`, `integration-dispatcher`,
`integration-heads`, `packaging`, `integration-recovery` and `integration-memory` — stay in dispatcher-owned exact-SHA GitHub CI,
which remains the complete gate and is not weakened by anything here. A green local broad receipt
has never been, and still is not, a substitute for it.

The module list is derived from ``tests/ci-shards.txt`` at run time rather than written out here.
The manifest owns the taxonomy: it is validated before any CI suite starts, and every top-level
``tests/test_*.py`` must occur in it exactly once. A second hand-maintained list of the same modules
would drift the first time somebody added a test file, and it would drift silently — a broad suite
that quietly stopped running a module is worse than one that fails. For the same reason the parsing
is not copied either: ``scripts/ci_test_shards.py`` already validates and groups the manifest, and
this module loads that one implementation by path (``scripts/`` is not an importable package) so
the two can never disagree about what a suite contains. A manifest that is invalid or unreadable
raises out of `broad_modules` and the run fails loudly; it never falls back to a smaller set.

One property is load-bearing and holds by construction rather than by an assertion here: this file
lives inside the ``tests`` package, so ``python -m tests.broad`` imports ``tests/__init__.py`` — and
with it every hermetic default the suite depends on (the throwaway temporary root, the throwaway
Codex home, the throwaway pipeline state dir) — before this module's body runs, and therefore before any
``tests.test_*`` module can be imported. That is exactly the invariant secretary-748 was about, and
``tests/test_health_suite_command.py`` proves it for this entry point.
"""

from __future__ import annotations

import importlib.util
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
    arguments = list(sys.argv[1:] if argv is None else argv)
    # `unittest.main` gives this the same `-v`, `-f` and explicit-test-name handling every other
    # invocation in this repository has; `python -m tests.broad tests.test_broad_check` therefore
    # still runs just that module, and only a vector that names nothing gets the manifest's set.
    selected = [] if _names_tests(arguments) else broad_modules()
    program = unittest.main(
        module=None,
        argv=["python -m tests.broad", *arguments, *selected],
        exit=False,
    )
    return 0 if program.result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
