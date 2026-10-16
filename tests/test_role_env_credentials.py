"""Launched roles resolve no GitHub login and no git credential helper from the owner's HOME."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ummanu.infra.head_credential_findings import (
    HEAD_CREDENTIAL_FINDING,
    head_env_credential_findings,
    role_head_credential_findings,
)
from ummanu.runtime import git_hooks, role_env
from ummanu.runtime.paths import PRODUCT_ENV

PRODUCT_ROOT = Path(__file__).resolve().parents[1]

OWNER_NAME = 'Owner "q" \\ name'
OWNER_EMAIL = "owner@example.test"


class OwnerHomeCase(unittest.TestCase):
    """An owner HOME whose own gh and git configuration hold a GitHub login."""

    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="role-credentials-home."))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        (self.home / ".gitconfig").write_text(
            "[user]\n"
            '\tname = "Owner \\"q\\" \\\\ name"\n'
            f"\temail = {OWNER_EMAIL}\n"
            "[credential]\n"
            "\thelper = !gh auth git-credential\n"
            '[credential "https://github.com"]\n'
            "\thelper = !gh auth git-credential\n",
            encoding="utf-8",
        )
        owner_gh = self.home / ".config" / "gh"
        owner_gh.mkdir(parents=True)
        (owner_gh / "hosts.yml").write_text(
            "github.com:\n    user: owner\n    oauth_token: fake-owner-token\n    git_protocol: https\n",
            encoding="utf-8",
        )
        self.repo = self.home / "repo"
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True, env=self._owner_env())

    def _owner_env(self) -> dict[str, str]:
        return {
            "HOME": str(self.home),
            "PATH": os.environ.get("PATH", os.defpath),
            "GH_CONFIG_DIR": str(self.home / ".config" / "gh"),
            "GIT_CONFIG_GLOBAL": str(self.home / ".gitconfig"),
        }

    def _git(self, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args], cwd=self.repo, env=env, capture_output=True, text=True, timeout=30, check=False
        )

    def _role_env(self, role: str) -> dict[str, str]:
        return role_env.runtime_env(role, base_env=self._owner_env(), env_file=self.home / "absent.env")


class RoleCredentialEnvTests(OwnerHomeCase):
    def test_every_role_resolves_no_credential_helper_and_keeps_the_owner_identity(self) -> None:
        self.assertIn("gh auth", self._git(self._owner_env(), "config", "credential.helper").stdout)
        for role in role_env.ROLE_ALLOWLIST:
            with self.subTest(role=role):
                env = self._role_env(role)
                helpers = self._git(env, "config", "--get-regexp", r"^credential\.")
                self.assertEqual((helpers.returncode, helpers.stdout), (1, ""))
                self.assertEqual(self._git(env, "config", "user.name").stdout, f"{OWNER_NAME}\n")
                self.assertEqual(self._git(env, "config", "user.email").stdout, f"{OWNER_EMAIL}\n")
                self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")

    def test_an_inherited_command_scope_git_config_names_no_helper_under_any_role(self) -> None:
        hostile = {
            **self._owner_env(),
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": "!gh auth git-credential",
            "GIT_CONFIG_PARAMETERS": "'credential.helper'='!gh auth git-credential'",
        }
        self.assertIn("gh auth", self._git(hostile, "config", "--get-all", "credential.helper").stdout)
        for role in role_env.ROLE_ALLOWLIST:
            with self.subTest(role=role):
                env = role_env.runtime_env(role, base_env=hostile, env_file=self.home / "absent.env")
                helpers = self._git(env, "config", "--get-all", "credential.helper")
                self.assertEqual((helpers.returncode, helpers.stdout), (1, ""))

    def test_an_inherited_askpass_program_is_never_asked_for_a_login_under_any_role(self) -> None:
        marker = self.home / "askpass-called"
        askpass = self.home / "owner-askpass"
        askpass.write_text(f"#!/bin/sh\necho called >> {marker}\necho owner-secret\n", encoding="utf-8")
        askpass.chmod(0o755)
        hostile = {**self._owner_env(), "GIT_ASKPASS": str(askpass), "SSH_ASKPASS": str(askpass)}
        request = "protocol=https\nhost=github.com\n\n"

        def fill(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
            # The owner's gh helper would reach the network; leave askpass as the only source.
            return subprocess.run(
                ["git", "-c", "credential.helper=", "credential", "fill"],
                cwd=self.repo, env=env, input=request, capture_output=True, text=True, timeout=30, check=False,
            )

        self.assertIn("password=owner-secret", fill(hostile).stdout)
        marker.unlink()
        for role in role_env.ROLE_ALLOWLIST:
            with self.subTest(role=role):
                env = role_env.runtime_env(role, base_env=hostile, env_file=self.home / "absent.env")
                self.assertNotIn("SSH_ASKPASS", env)
                filled = fill(env)
                self.assertNotEqual(filled.returncode, 0)
                self.assertNotIn("owner-secret", filled.stdout)
                self.assertFalse(marker.exists())

    def test_every_role_refuses_a_remote_branch_deletion_and_inherits_no_opt_out(self) -> None:
        bare = self.home / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, env=self._owner_env())
        owner = self._owner_env()
        (self.repo / "file").write_text("x\n", encoding="utf-8")
        for args in (("add", "file"), ("commit", "-qm", "x"), ("remote", "add", "origin", str(bare))):
            self.assertEqual(self._git(owner, *args).returncode, 0)
        self.assertEqual(self._git(owner, "push", "-q", "origin", "HEAD:refs/heads/doomed").returncode, 0)
        inherited = {**owner, git_hooks.REWRITE_OPT_OUT_ENV: "1"}
        product = {PRODUCT_ENV: str(PRODUCT_ROOT)}
        with mock.patch.dict(os.environ, product):
            for name in role_env.RUNTIME_PYTHONPATH_ENVS:
                os.environ.pop(name, None)
            for role in role_env.ROLE_ALLOWLIST:
                with self.subTest(role=role):
                    env = role_env.runtime_env(role, base_env=inherited, env_file=self.home / "absent.env")
                    self.assertNotIn(git_hooks.REWRITE_OPT_OUT_ENV, env)
                    refused = self._git(env, "push", "origin", ":refs/heads/doomed")
                    self.assertNotEqual(refused.returncode, 0)
                    self.assertIn("refusing to delete refs/heads/doomed", refused.stderr)

    def test_every_role_points_gh_at_an_empty_managed_config_directory(self) -> None:
        for role in role_env.ROLE_ALLOWLIST:
            with self.subTest(role=role):
                gh_dir = Path(self._role_env(role)["GH_CONFIG_DIR"])
                self.assertTrue(gh_dir.is_relative_to(self.home / role_env.HEAD_CONFIG_DIR))
                self.assertEqual(list(gh_dir.iterdir()), [])

    @unittest.skipUnless(shutil.which("gh"), "gh is not installed")
    def test_gh_reports_no_login_under_every_role(self) -> None:
        owner = subprocess.run(
            ["gh", "auth", "token"], env=self._owner_env(), capture_output=True, text=True, timeout=30, check=False
        )
        self.assertEqual(owner.stdout.strip(), "fake-owner-token")
        for role in role_env.ROLE_ALLOWLIST:
            with self.subTest(role=role):
                status = subprocess.run(
                    ["gh", "auth", "status"],
                    env=self._role_env(role),
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                self.assertNotEqual(status.returncode, 0)
                self.assertIn("not logged in", status.stdout + status.stderr)



@unittest.skipUnless(shutil.which("gh"), "gh is not installed")
class HeadCredentialDoctorTests(OwnerHomeCase):
    def _findings(self) -> list[tuple[str, str]]:
        return [
            (str(finding["role"]), str(finding["path"]))
            for finding in role_head_credential_findings(
                base_env=self._owner_env(), env_file=self.home / "absent.env"
            )
            if finding["code"] == HEAD_CREDENTIAL_FINDING
        ]

    def test_the_launched_role_envs_reach_no_owner_login(self) -> None:
        self.assertEqual(self._findings(), [])

    def test_a_logged_in_hosts_file_in_the_managed_gh_directory_is_a_finding_per_role(self) -> None:
        hosts = self.home / role_env.HEAD_CONFIG_DIR / "gh" / "hosts.yml"
        hosts.parent.mkdir(parents=True)
        shutil.copy(self.home / ".config" / "gh" / "hosts.yml", hosts)
        self.assertEqual(self._findings(), [("worker", str(hosts)), ("reviewer", str(hosts))])

    def test_a_credential_helper_in_the_git_configuration_the_head_reads_names_its_file(self) -> None:
        env = {**self._role_env("worker"), "GIT_CONFIG_GLOBAL": str(self.home / ".gitconfig")}
        findings = head_env_credential_findings("worker", env)
        self.assertEqual(
            [(finding["role"], finding["path"]) for finding in findings],
            [("worker", str(self.home / ".gitconfig"))] * 2,
        )


if __name__ == "__main__":
    unittest.main()
