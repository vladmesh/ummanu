"""Bounded negative PATH fixtures, with no native backend execution."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support.local_check_fixture import role_git_env
from tests.support.managed_venv import managed_product_root
from ummanu.broad_check import content_identity
from ummanu.runtime import docker_guard, role_env


class HeadCommandGuardTests(unittest.TestCase):
    def test_role_receipt_fixture_keeps_git_identity_and_maintenance_controls(self) -> None:
        git = shutil.which("git", path=os.defpath)
        self.assertIsNotNone(git)
        with tempfile.TemporaryDirectory() as scratch:
            parent = Path(scratch)
            product = managed_product_root(parent)
            native = parent / "native-bin"
            native.mkdir()
            (native / "git").symlink_to(git)
            root = parent / "candidate"
            root.mkdir()
            subprocess.run([git, "-C", str(root), "init", "-q"], check=True, capture_output=True)
            (root / "tracked.py").write_text("VALUE = 1\n")
            (root / ".gitignore").write_text(".ummanu-task-env/\n")
            python = root / role_env.WORKSPACE_ENV_DIR / "bin/python3"
            python.parent.mkdir(parents=True)
            python.symlink_to("/usr/bin/python3")  # PATH binding only; never executed here.
            base = {**os.environ, "UMMANU_REPO": str(product),
                    "PATH": str(native) + os.pathsep + str(product / ".venv/bin"),
                    "GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "gc.auto", "GIT_CONFIG_VALUE_0": "0",
                    "GIT_CONFIG_KEY_1": "maintenance.auto", "GIT_CONFIG_VALUE_1": "false"}
            for role in ("worker", "reviewer"):
                with self.subTest(role=role), mock.patch.dict(os.environ, base, clear=True):
                    env = role_env.runtime_env(role, base_env=base, workspace=root, env_file=parent / "absent.env")
                    result = subprocess.run([git, "-C", str(root), "rev-parse", "--git-dir"],
                                            env=env, capture_output=True, text=True, check=False, timeout=5)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("missing config key GIT_CONFIG_KEY_0", result.stderr)
                    repaired = role_git_env(root, base)
                    env = role_env.runtime_env(role, base_env=repaired, workspace=root, env_file=parent / "absent.env")
                    with mock.patch.dict(os.environ, env, clear=True):
                        self.assertTrue(content_identity(root).resolved)
                    for name, expected in (("gc.auto", "0"), ("maintenance.auto", "false")):
                        result = subprocess.run([git, "-C", str(root), "config", "--get", name],
                                                env=env, capture_output=True, text=True, check=True, timeout=5)
                        self.assertEqual(result.stdout.strip(), expected)
                    self.assertNotIn("GIT_CONFIG_COUNT", env)
                    self.assertNotIn("GIT_CONFIG_KEY_0", env)

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
