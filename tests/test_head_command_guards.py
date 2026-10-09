"""Bounded negative PATH fixtures, with no native backend execution."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support.managed_venv import managed_product_root
from ummanu.runtime import docker_guard, role_env


class HeadCommandGuardTests(unittest.TestCase):
    def test_both_head_roles_refuse_absent_and_fake_backends(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            product = managed_product_root(root)
            native = root / "native"
            native.mkdir()
            sentinel = root / "native-executed"
            workspace = root / "candidate"
            python = workspace / role_env.WORKSPACE_ENV_DIR / "bin/python3"
            python.parent.mkdir(parents=True)
            python.symlink_to("/usr/bin/python3")
            for present in (False, True):
                if present:
                    for name in ("ansible", "ansible-playbook", "sudo"):
                        path = native / name
                        path.write_text(f"#!/bin/sh\nprintf executed > {sentinel}\nexit 91\n")
                        path.chmod(0o755)
                for role in ("worker", "reviewer"):
                    with mock.patch.dict(os.environ, {"UMMANU_REPO": str(product)}, clear=True):
                        env = role_env.runtime_env(role, base_env={"PATH": str(native)},
                                                   workspace=workspace, env_file=root / "absent.env")
                    self.assertEqual(env["PATH"].split(os.pathsep)[:2],
                                     [str(product / "src/ummanu/runtime/docker-bin"), str(python.parent)])
                    self.assertEqual(env[docker_guard.BACKEND_ENV], "")
                    for name in ("ansible", "ansible-playbook", "sudo"):
                        with self.subTest(role=role, backend=present, command=name):
                            result = subprocess.run([name, "--version"], cwd=workspace, env=env,
                                                    capture_output=True, text=True, timeout=5, check=False)
                            self.assertEqual(result.returncode, 125)
                            self.assertEqual(result.stdout, "")
                            self.assertEqual(result.stderr, f"head-guard: {name} refused; execution only in CI\n")
                            self.assertFalse(sentinel.exists())
                            print(f"negative role={role} backend={present} argv={name} --version "
                                  f"rc={result.returncode} sentinel=false: {result.stderr.strip()}")

    def test_standing_roles_keep_their_native_path_without_head_shims(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            for role in ("pipeline", "observer", "steward", "curator", "retro"):
                env = role_env.runtime_env(role, base_env={"PATH": "/native"},
                                           env_file=Path(scratch) / "absent.env")
                self.assertNotIn("docker-bin", env["PATH"])
                self.assertTrue(env["PATH"].endswith("/native"))
