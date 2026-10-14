"""Regression coverage for secretary-748: the documented health-suite command
must import ``tests`` (and therefore the hermetic defaults it installs)
before any test module can reach a host-facing path.

Prior incident: docs/OPERATIONS.md and the steward skill documented
``python3 -m unittest discover -s tests``. That invocation's top-level
directory defaults to the start directory itself, so unittest's discovery
does not treat ``tests`` as a dotted package rooted one level up; whether the
hermetic default in ``tests/__init__.py`` ends up applied at all then depends
on some *other* test module happening to import ``tests`` explicitly (as
the since-retired ``tests/test_hermetic_orca.py`` did) before any test runs, purely by
incidental module ordering, not by design. The fix was to document
``python3 -m unittest`` instead, whose default discovery (``discover('.')``)
imports ``tests`` as a package on its own, unconditionally.

The documented command has since changed again, and the invariant has not.
Since issue:8b39e60e4df361c6138e the documented local broad run is
``python3 -m tests.broad`` — the manifest's ``unit`` and ``component`` suites,
about 1440 tests in ~77s, rather than repository-wide discovery's 3782 tests in
~402s. It satisfies the same rule, and it satisfies it more directly than the
form it replaces: ``tests/broad.py`` lives *inside* the ``tests`` package, so
``python -m`` imports ``tests/__init__.py`` before that module's own body runs,
and therefore before it can name a single test module. This file pins that,
plus the standing rule that no committed document may recommend the broken
``-s tests`` form again.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Imports the documented entry point and nothing else, then reports three facts about the
#: process: that the ``tests`` package was imported, that its hermetic default is live, and that
#: no test module had to be imported first for either to be true.
_PROBE = """
import os
import sys
from pathlib import Path

import tests.broad

print("tests-imported=%s" % ("tests" in sys.modules))
print("patched=%s" % Path(os.environ.get("UMMANU_PIPELINE_STATE_DIR", "")).name.startswith(
    "ummanu-tests-pipeline-state."
))
print("no-test-module-imported=%s" % (not any(
    name == "tests.test" or name.startswith("tests.test_") for name in sys.modules
)))
print("names-a-suite=%s" % bool(tests.broad.broad_modules()))
"""

def _run_probe() -> dict:
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    lines = dict(line.split("=", 1) for line in result.stdout.strip().splitlines())
    return {key: value == "True" for key, value in lines.items()}


class DocumentedHealthSuiteCommandTests(unittest.TestCase):
    def test_documented_command_imports_tests_and_applies_the_hermetic_default(self):
        # `python3 -m tests.broad` imports the `tests` package before executing `tests/broad.py`,
        # because that is what importing a submodule means. The hermetic default is therefore live
        # before the module can name a test, rather than depending on collection order.
        outcome = _run_probe()

        self.assertTrue(outcome["tests-imported"])
        self.assertTrue(outcome["patched"])
        self.assertTrue(
            outcome["no-test-module-imported"],
            "the default must be installed by the package import itself, not by whichever test "
            "module happens to be imported first — that accident is ummanu-748",
        )
        self.assertTrue(
            outcome["names-a-suite"],
            "an entry point that resolves to no modules would satisfy every assertion above while "
            "running nothing",
        )

if __name__ == "__main__":
    unittest.main()
