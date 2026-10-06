"""Startup noise and shared dispatcher paths cannot leak into test runs."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests import _SUITE_BODY_DIR, _SUITE_HOME, _SUITE_TMP
from tests.support.shell import shell_tools


class HermeticShellTests(unittest.TestCase):
    def test_hostile_shell_startup_files_are_not_executed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for filename in (".bash_profile", ".bashrc", "bash-env"):
                (root / filename).write_text("echo startup-noise\nexit 17\n")
            tools = shell_tools(root / "bin")
            result = subprocess.run(
                [str(tools / "bash"), "-lc", "printf 'native stdout'; exit 3"],
                env={**os.environ, "HOME": str(root)},
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.stdout, "native stdout")
        self.assertEqual(result.returncode, 3)

    def test_suite_replaces_ambient_home_body_and_startup_overrides_in_a_child(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            startup = root / "startup"
            startup.write_text("echo startup-noise\n")
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import tests, os, json, subprocess; "
                        "print(json.dumps({'home': os.environ['HOME'], "
                        "'bodies': os.environ['UMMANU_DISPATCHER_BODY_DIR'], "
                        "'startup': os.environ.get('BASH_ENV'), "
                        "'stdout': subprocess.check_output(['bash', '-lc', 'printf native'], text=True)}))"
                    ),
                ],
                env={
                    **os.environ,
                    "HOME": str(root),
                    "BASH_ENV": str(startup),
                    "ENV": str(startup),
                    "UMMANU_DISPATCHER_BODY_DIR": str(root / "live-bodies"),
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertNotEqual(payload["home"], str(root))
            self.assertNotEqual(payload["bodies"], str(root / "live-bodies"))
            self.assertIsNone(payload["startup"])
            self.assertEqual(payload["stdout"], "native")
            self.assertFalse((root / "live-bodies").exists())
        self.assertTrue(_SUITE_HOME.is_relative_to(_SUITE_TMP))
        self.assertTrue(_SUITE_BODY_DIR.is_relative_to(_SUITE_TMP))
