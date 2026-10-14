"""Only the `UMMANU_*` environment configures the product, and the old package is gone (docs/RENAME.md, "Guard").

The retired SECRETARY prefix is ignored, never read as a fallback, and the old package does not
import. The old spellings below are negative fixtures, never runtime aliases.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ummanu import session
from ummanu.automations.runtime import production_telemetry
from ummanu.runtime import paths, role_env

ROOT = Path(__file__).resolve().parent.parent

#: The keys under test, by role: (old spelling, new spelling).
KEYS = {
    "data_dir": ("SECRETARY_DATA_DIR", "UMMANU_DATA_DIR"),
    "instance": ("SECRETARY_INSTANCE", paths.INSTANCE_ENV),
    "repo": ("TA_SECRETARY_REPO", paths.PRODUCT_ENV),
    "runtime_env_file": ("SECRETARY_RUNTIME_ENV_FILE", "UMMANU_RUNTIME_ENV_FILE"),
}


def _clean_environ() -> dict[str, str]:
    """The process environment without anything that names an installation."""
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("SECRETARY_", "UMMANU_", "TA_SECRETARY_"))
        and key not in role_env.RUNTIME_ENV_FILE_ENVS
    }


class EnvPrefixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="env-prefix-"))
        self.addCleanup(shutil.rmtree, self.home, True)
        self.default_instance = self.home / "default-instance"
        patcher = mock.patch.object(production_telemetry, "DEFAULT_INSTANCE", self.default_instance)
        patcher.start()
        self.addCleanup(patcher.stop)

    def resolve(self, environ: dict[str, str]) -> dict[str, Path | None]:
        with mock.patch.dict(os.environ, {**_clean_environ(), "HOME": str(self.home), **environ}, clear=True):
            return {
                "data_dir": production_telemetry.data_dir(),
                "instance": production_telemetry.instance_file(),
                "repo": paths.configured_product_root(),
                "runtime_env_file": role_env.runtime_env_path(),
                # With `--workspace` named, an installation that does not resolve leaves no data dir.
                "memory_data_dir": session.resolve_shell_target("/w", None, dict(os.environ)).data_dir,
            }

    def test_the_old_prefix_is_ignored(self) -> None:
        bogus = self.home / "bogus-old-prefix"
        resolved = self.resolve({old: str(bogus / role) for role, (old, _new) in KEYS.items()})
        for role, value in resolved.items():
            with self.subTest(role=role):
                self.assertFalse(
                    str(value).startswith(str(bogus)), f"{role} followed the old prefix: {value}"
                )
        self.assertEqual(resolved["data_dir"], self.home / "ummanu-data")
        self.assertEqual(resolved["instance"], self.default_instance)
        self.assertEqual(resolved["repo"], self.home / "ummanu")
        self.assertEqual(resolved["runtime_env_file"], Path(role_env.RUNTIME_ENV_DEFAULT))
        self.assertIsNone(resolved["memory_data_dir"])

    def test_the_new_prefix_takes_effect(self) -> None:
        configured = self.home / "configured"
        (configured / "instance").mkdir(parents=True)
        resolved = self.resolve({new: str(configured / role) for role, (_old, new) in KEYS.items()})
        self.assertEqual(resolved["data_dir"], configured / "data_dir")
        self.assertEqual(resolved["instance"], configured / "instance" / "instance.yaml")
        self.assertEqual(resolved["repo"], configured / "repo")
        self.assertEqual(resolved["runtime_env_file"], configured / "runtime_env_file")
        self.assertEqual(resolved["memory_data_dir"], configured / "data_dir")

    def test_both_prefixes_set_resolve_to_the_new_one(self) -> None:
        old_dir, new_dir = self.home / "old", self.home / "new"
        environ = {old: str(old_dir / role) for role, (old, _new) in KEYS.items()}
        environ.update({new: str(new_dir / role) for role, (_old, new) in KEYS.items()})
        resolved = self.resolve(environ)
        self.assertEqual(resolved["data_dir"], new_dir / "data_dir")
        self.assertEqual(resolved["repo"], new_dir / "repo")
        self.assertEqual(resolved["runtime_env_file"], new_dir / "runtime_env_file")

    def test_the_old_package_does_not_import_in_a_clean_interpreter(self) -> None:
        environ = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
        old = subprocess.run(
            [sys.executable, "-P", "-c", "import secretary"],
            cwd=self.home,
            env=environ,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(old.returncode, 0, old.stdout)
        self.assertIn("ModuleNotFoundError: No module named 'secretary'", old.stderr)
        # The same interpreter does import the product from this tree, so the refusal above is the
        # missing package and not a broken interpreter.
        new = subprocess.run(
            [sys.executable, "-P", "-c", "import ummanu; print(ummanu.__file__)"],
            cwd=self.home,
            env={**environ, "PYTHONPATH": str(ROOT / "src")},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(new.returncode, 0, new.stderr)
        self.assertEqual(Path(new.stdout.strip()), ROOT / "src" / "ummanu" / "__init__.py")

if __name__ == "__main__":
    unittest.main()
