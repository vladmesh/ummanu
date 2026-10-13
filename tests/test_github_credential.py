import contextlib
import hashlib
import io
import json
import os
import pwd
import secrets
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ummanu import secret_store
from ummanu.checkpoint import CheckpointPusher, _credential_snapshot
from ummanu.cli import main
from ummanu.infra import github_credential
from ummanu.installation import (
    clone as installation_clone,
    credentials as installation_credentials,
    models as installation_models,
)
from ummanu.secret_words import RECOVERY_WORDS


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, check=True).stdout


def fast_key_params():
    return {
        "format": secret_store.KEY_PARAMS_FORMAT,
        "version": secret_store.KEY_PARAMS_VERSION,
        "kdf": {
            "id": "scrypt",
            "salt": secret_store._b64(b"0123456789abcdef"),
            "length": 32,
            "n": 2**8,
            "r": 8,
            "p": 1,
        },
    }


class ManagedGithubCredentialTests(unittest.TestCase):
    phrase = " ".join(RECOVERY_WORDS[:16])

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.instance = Path(self.temporary.name) / "instance"
        self.instance.mkdir()
        git(self.instance, "init", "--quiet", "--initial-branch", "main")
        git(self.instance, "config", "user.name", "test")
        git(self.instance, "config", "user.email", "test@example.invalid")
        (self.instance / "instance.yaml").write_text("version: 1\n", encoding="utf-8")
        git(self.instance, "add", "instance.yaml")
        git(self.instance, "commit", "--quiet", "-m", "initial")
        with mock.patch.object(secret_store, "_new_key_params", side_effect=fast_key_params):
            secret_store.initialize_store(self.instance, phrase=self.phrase, actor="test")

    def set_token(self, token: bytes = b"github-test-token") -> None:
        secret_store.set_secret(
            self.instance,
            secret_id=github_credential.CHECKPOINT_CREDENTIAL_ID,
            value=token,
            scope="installation",
            purpose=github_credential.CHECKPOINT_CREDENTIAL_PURPOSE,
            actor="test",
        )

    @staticmethod
    def runtime_account() -> pwd.struct_passwd | None:
        """A usable non-root account, without naming a host-specific user."""
        preferred: list[pwd.struct_passwd] = []
        try:
            owner = pwd.getpwuid(Path(__file__).stat().st_uid)
            if owner.pw_uid:
                preferred.append(owner)
        except KeyError:
            pass
        seen: set[int] = set()
        candidates = [*preferred, *pwd.getpwall()]
        environment = github_credential._helper_runtime_environment()
        for account in candidates:
            if not account.pw_uid or account.pw_uid in seen:
                continue
            seen.add(account.pw_uid)
            probe = subprocess.run(
                [
                    "runuser",
                    "--user",
                    account.pw_name,
                    "--",
                    "env",
                    *[f"{name}={value}" for name, value in environment.items()],
                    sys.executable,
                    "-c",
                    "import ummanu.infra.github_credential",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            if probe.returncode == 0:
                return account
        return None

    def run_cli(self, argv: list[str], stdin: bytes = b"") -> tuple[int, str, str]:
        output, error = io.StringIO(), io.StringIO()
        stream = io.TextIOWrapper(io.BytesIO(stdin), encoding="utf-8")
        with (
            mock.patch("sys.stdin", stream),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(error),
        ):
            code = main(argv)
        return code, output.getvalue(), error.getvalue()

    @staticmethod
    def digest(value: str | bytes) -> str:
        raw = value.encode("utf-8") if isinstance(value, str) else value
        return hashlib.sha256(raw).hexdigest()

    def test_checkpoint_cli_normalizes_one_terminal_newline_and_is_idempotent(self) -> None:
        token = "fixture-" + secrets.token_hex(16)
        base = ["secret", "checkpoint-github", "set", "--instance", str(self.instance), "--stdin"]
        first, output, error = self.run_cli(base, (token + "\n").encode())
        self.assertEqual((first, error), (0, ""))
        self.assertNotIn(token, output)
        self.assertIn('"created": true', output)
        second, repeated, error = self.run_cli(base, (token + "\n").encode())
        self.assertEqual((second, error), (0, ""))
        self.assertNotIn(token, repeated)
        self.assertIn('"created": false', repeated)
        stored = secret_store.read_secret(self.instance, github_credential.CHECKPOINT_CREDENTIAL_ID)
        self.assertEqual(self.digest(stored), self.digest(token))
        tracked_contents = b"\n".join(
            (self.instance / path).read_bytes()
            for path in git(self.instance, "ls-files").splitlines()
            if (self.instance / path).is_file()
        )
        self.assertFalse(token.encode() in tracked_contents)

        replacement = "fixture-" + secrets.token_hex(16)
        source = Path(self.temporary.name) / "checkpoint-token"
        source.write_text(replacement + "\r\n", encoding="utf-8")
        source.chmod(0o600)
        code, imported, error = self.run_cli(
            ["secret", "checkpoint-github", "import", "--instance", str(self.instance), "--file", str(source)]
        )
        self.assertEqual((code, error), (0, ""))
        self.assertNotIn(replacement, imported)
        self.assertEqual(
            self.digest(secret_store.read_secret(self.instance, github_credential.CHECKPOINT_CREDENTIAL_ID)),
            self.digest(replacement),
        )

    def test_checkpoint_cli_refuses_insecure_or_padded_file_without_value_output(self) -> None:
        token = "fixture-" + secrets.token_hex(16)
        source = Path(self.temporary.name) / "checkpoint-token"
        source.write_text(token + "\n", encoding="utf-8")
        source.chmod(0o644)
        argv = ["secret", "checkpoint-github", "set", "--instance", str(self.instance), "--file", str(source)]
        code, output, _ = self.run_cli(argv)
        self.assertEqual(code, 2)
        self.assertIn("mode-0600", output)
        self.assertNotIn(token, output)
        source.chmod(0o600)
        source.write_text(" " + token + "\n", encoding="utf-8")
        code, output, _ = self.run_cli(argv)
        self.assertEqual(code, 2)
        self.assertIn("content must", output)
        self.assertNotIn(token, output)
        source.write_text(token + "\n", encoding="utf-8")
        with mock.patch("ummanu.secret_commands.os.geteuid", return_value=os.geteuid() + 1):
            code, output, _ = self.run_cli(argv)
        self.assertEqual(code, 2)
        self.assertIn("belongs to another user", output)
        self.assertNotIn(token, output)

    def test_real_git_helper_ignores_an_ambient_helper_and_uses_the_store(self) -> None:
        token = "fixture-" + secrets.token_hex(16)
        self.set_token(token.encode())
        environment = dict(os.environ)
        environment.update(github_credential.helper_environment(self.instance))
        environment["GIT_CONFIG_NOSYSTEM"] = "1"
        argv = [
            "git",
            "-c",
            "credential.helper=!false",
            *github_credential._helper_config_args(),
            "credential",
            "fill",
        ]
        self.assertNotIn(token, " ".join(argv))
        result = subprocess.run(
            argv,
            input="protocol=https\nhost=github.com\n\n",
            text=True,
            capture_output=True,
            env=environment,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        reply = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        self.assertEqual(reply.get("username"), "x-access-token")
        self.assertEqual(self.digest(reply.get("password", "")), self.digest(token))
        self.assertNotIn(token, result.stderr)

    def test_bootstrap_inputs_normalize_one_terminator_and_name_the_failure_kind(self) -> None:
        token = "fixture-" + secrets.token_hex(16)
        source = Path(self.temporary.name) / "bootstrap"
        source.write_text(token + "\n", encoding="utf-8")
        source.chmod(0o600)
        args = SimpleNamespace(bootstrap_credential_file=str(source), bootstrap_credential_stdin=False)
        path, disposable = installation_credentials._bootstrap_credential(args, self.instance / "target")
        self.assertEqual((path, disposable), (source, None))
        self.assertEqual(self.digest(github_credential._bootstrap_token(source)), self.digest(token))
        source.write_text(" " + token + "\n", encoding="utf-8")
        with self.assertRaisesRegex(installation_models.InstallError, "content is rejected"):
            installation_credentials._bootstrap_credential(args, self.instance / "target")
        source.unlink()
        with self.assertRaisesRegex(installation_models.InstallError, "could not inspect bootstrap credential file"):
            installation_credentials._bootstrap_credential(args, self.instance / "target")

    def test_wrong_recovery_phrase_stays_a_sanitized_recovery_failure(self) -> None:
        self.set_token()
        (self.instance / "secrets" / secret_store.KEY_NAME).unlink()
        with self.assertRaisesRegex(installation_models.InstallError, "secret store") as failure:
            installation_credentials._open_secret_store(
                self.instance, self.instance / "runtime.env", phrase="wrong recovery phrase", dry_run=True
            )
        self.assertNotIn("github-test-token", str(failure.exception))

    def test_run_install_rejects_two_stdin_secrets_before_reading_either(self) -> None:
        code, output, error = self.run_cli(
            [
                "recover",
                "--instance-remote",
                "https://github.com/example/private.git",
                "--instance-dir",
                str(self.instance / "target"),
                "--installation-user",
                "nobody",
                "--bootstrap-credential-stdin",
                "--recovery-phrase-stdin",
            ],
            b"not-consumed\n",
        )
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertIn("cannot share standard input", error)

    def test_private_remote_selector_enforces_clone_reuse_and_checkpoint_sources(self) -> None:
        with self.assertRaisesRegex(github_credential.CredentialError, "bootstrap credential is required"):
            github_credential.select_private_remote_auth("initial-clone")
        bootstrap = Path(self.temporary.name) / "bootstrap"
        bootstrap.write_text("fixture-bootstrap\n", encoding="utf-8")
        bootstrap.chmod(0o600)
        selected = github_credential.select_private_remote_auth("initial-clone", bootstrap_file=bootstrap)
        self.assertEqual(selected.source, "bootstrap")
        self.assertEqual(selected.environment["UMMANU_GITHUB_BOOTSTRAP_FILE"], str(bootstrap))
        self.assertNotIn("credential.helper", " ".join(selected.environment))

        # Selection does not inspect the caller's filesystem identity. The
        # RemoteExecution boundary resolves and checks the eventual Git child.
        unavailable = github_credential.select_private_remote_auth(
            "recovery-reuse", instance_dir=self.instance
        )
        self.assertEqual(unavailable.source, "managed-store")
        self.set_token()
        managed = github_credential.select_private_remote_auth("recovery-reuse", instance_dir=self.instance)
        self.assertEqual(managed.source, "managed-store")
        self.assertEqual(managed.environment["UMMANU_CHECKPOINT_INSTANCE"], str(self.instance))
        checkpoint = github_credential.select_private_remote_auth("checkpoint", instance_dir=self.instance)
        self.assertEqual(checkpoint.source, "managed-store")
        project_managed = github_credential.select_private_remote_auth(
            "project-provision", instance_dir=self.instance
        )
        project_bootstrap = github_credential.select_private_remote_auth(
            "project-provision", instance_dir=self.instance, bootstrap_file=bootstrap
        )
        self.assertEqual(project_managed.source, "managed-store")
        self.assertEqual(project_bootstrap.source, "bootstrap")

    def test_project_clone_uses_bootstrap_then_managed_capabilities_without_secret_argv(self) -> None:
        remote = "https://github.com/example/private.git"
        bootstrap = Path(self.temporary.name) / "project-bootstrap"
        token = "fixture-" + secrets.token_hex(16)
        bootstrap.write_text(token + "\n", encoding="utf-8")
        bootstrap.chmod(0o600)
        calls: list[tuple[list[str], dict[str, str]]] = []

        def run(_instance, argv, **kwargs):
            calls.append((argv, kwargs["extra_env"]))
            return subprocess.CompletedProcess(argv, 0, "", "")

        with mock.patch("ummanu.infra.github_credential.state_repo.run_as_git_child", side_effect=run):
            execution = github_credential.RemoteExecution(
                remote,
                "project-provision",
                instance_dir=self.instance,
                bootstrap_file=bootstrap,
            )
            execution.run_clone(
                self.instance / "staged-bootstrap",
                label="project clone",
                timeout=1,
                child_target=self.instance,
            )
        bootstrap_argv, bootstrap_env = calls.pop()
        self.assertEqual(execution.source, "bootstrap")
        self.assertNotIn(token, " ".join(bootstrap_argv))
        self.assertNotEqual(bootstrap_env["UMMANU_GITHUB_BOOTSTRAP_FILE"], str(bootstrap))
        self.assertFalse(Path(bootstrap_env["UMMANU_GITHUB_BOOTSTRAP_FILE"]).exists())

        self.set_token()
        with mock.patch("ummanu.infra.github_credential.state_repo.run_as_git_child", side_effect=run):
            execution = github_credential.RemoteExecution(
                remote, "project-provision", instance_dir=self.instance
            )
            execution.run_clone(
                self.instance / "staged-managed",
                label="project clone",
                timeout=1,
                child_target=self.instance,
            )
        managed_argv, managed_env = calls.pop()
        self.assertEqual(execution.source, "managed-store")
        self.assertEqual(managed_argv[:3], ["git", "-c", "credential.helper="])
        self.assertEqual(managed_env["UMMANU_CHECKPOINT_INSTANCE"], str(self.instance))
        self.assertNotIn(token, " ".join(managed_argv) + json.dumps(managed_env))

    def test_project_clone_refuses_credential_bearing_url_before_launch(self) -> None:
        token = "secret-in-url"
        remote = github_credential.RemoteExecution(
            f"https://user:{token}@github.com/example/private.git",
            "project-provision",
            instance_dir=self.instance,
        )
        with (
            mock.patch("ummanu.infra.github_credential.state_repo.run_as_git_child") as run,
            self.assertRaisesRegex(github_credential.CredentialError, "credential-bearing"),
        ):
            remote.run_clone(
                self.instance / "staged",
                label="project clone",
                timeout=1,
                child_target=self.instance,
            )
        run.assert_not_called()

    def test_recovery_reuse_selects_bootstrap_or_managed_auth_and_never_ambient(self) -> None:
        remote = "https://github.com/example/private.git"
        commands: list[tuple[list[str], dict[str, str]]] = []

        def inspect(_instance, args, **_kwargs):
            if args == ["remote", "get-url", "origin"]:
                return remote + "\n"
            if args == ["status", "--porcelain", "-z", "--untracked-files=all"]:
                return ""
            self.fail(f"unexpected local operation: {args}")

        def run(_instance, args, **kwargs):
            commands.append((args, kwargs["extra_env"]))
            return subprocess.CompletedProcess(args, 0, "", "")

        self.set_token()
        with (
            mock.patch("ummanu.installation.clone.state_repo.git", side_effect=inspect),
            mock.patch("ummanu.installation.clone.state_repo.run_git", side_effect=run),
        ):
            self.assertEqual(
                installation_clone._clone_or_reuse(remote, self.instance, recovery=True, dry_run=False),
                "reused checkpoint checkout",
            )
        self.assertEqual(len(commands), 2)
        for args, environment in commands:
            self.assertEqual(args[:2], ["-c", "credential.helper="])
            self.assertIn("UMMANU_CHECKPOINT_INSTANCE", environment)

        (self.instance / "secrets" / secret_store.KEY_NAME).unlink()
        commands.clear()
        with (
            mock.patch("ummanu.installation.clone.state_repo.git", side_effect=inspect),
            mock.patch("ummanu.installation.clone.state_repo.run_git", side_effect=run) as remote_run,
            self.assertRaisesRegex(installation_models.InstallError, "needs a bootstrap credential"),
        ):
            installation_clone._clone_or_reuse(remote, self.instance, recovery=True, dry_run=False)
        remote_run.assert_not_called()

        bootstrap = Path(self.temporary.name) / "bootstrap"
        bootstrap.write_text("fixture-bootstrap\n", encoding="utf-8")
        bootstrap.chmod(0o600)
        with (
            mock.patch("ummanu.installation.clone.state_repo.git", side_effect=inspect),
            mock.patch("ummanu.installation.clone.state_repo.run_git", side_effect=run),
        ):
            installation_clone._clone_or_reuse(
                remote, self.instance, recovery=True, dry_run=False, bootstrap_credential=bootstrap
            )
        self.assertNotEqual(commands[0][1]["UMMANU_GITHUB_BOOTSTRAP_FILE"], str(bootstrap))
        self.assertTrue(commands[0][1]["UMMANU_GITHUB_BOOTSTRAP_FILE"].endswith("/credential"))

    def test_non_github_recovery_reuse_does_not_require_a_github_credential(self) -> None:
        remote = "file:///fixture/instance.git"

        with (
            mock.patch(
                "ummanu.installation.clone.state_repo.git",
                side_effect=(remote + "\n", "", "", ""),
            ) as git,
            mock.patch(
                "ummanu.installation.clone.state_repo.run_git",
                side_effect=lambda _instance, args, **_kwargs: subprocess.CompletedProcess(args, 0, "", ""),
            ) as remote_git,
        ):
            self.assertEqual(
                installation_clone._clone_or_reuse(remote, self.instance, recovery=True, dry_run=False),
                "reused checkpoint checkout",
            )

        self.assertEqual(
            [call.args[1] for call in git.call_args_list],
            [
                ["remote", "get-url", "origin"],
                ["status", "--porcelain", "-z", "--untracked-files=all"],
            ],
        )
        self.assertEqual(
            [call.args[1] for call in remote_git.call_args_list],
            [
                ["fetch", "--quiet", "--no-tags", "origin"],
                ["merge", "--ff-only", "@{u}"],
            ],
        )

    def test_remote_execution_classifies_every_transport_before_launch(self) -> None:
        self.assertEqual(
            github_credential.RemoteExecution(
                "https://github.com/example/private.git", "checkpoint"
            ).transport,
            "github-https",
        )
        self.assertEqual(
            github_credential.RemoteExecution("git@github.com:example/private.git", "checkpoint").transport,
            "ssh",
        )
        self.assertEqual(
            github_credential.RemoteExecution(
                "https://github.com.example/private.git", "checkpoint"
            ).transport,
            "https-unsupported",
        )
        self.assertEqual(
            github_credential.RemoteExecution("file:///fixture/instance.git", "checkpoint").transport, "local"
        )

    def test_ssh_is_manual_bypass_and_other_https_is_refused_before_git(self) -> None:
        ssh = github_credential.RemoteExecution("git@github.com:example/private.git", "checkpoint")
        with mock.patch(
            "ummanu.infra.github_credential.state_repo.run_git",
            side_effect=lambda _instance, args, **_kwargs: subprocess.CompletedProcess(args, 0, "", ""),
        ) as run:
            ssh.run_instance(self.instance, ["fetch", "origin"], label="fixture")
        self.assertEqual(ssh.source, "manual-bypass")
        self.assertNotIn("credential.helper", " ".join(run.call_args.args[1]))

        unsupported = github_credential.RemoteExecution(
            "https://github.com.example/private.git", "checkpoint"
        )
        with (
            mock.patch("ummanu.infra.github_credential.state_repo.run_git") as run,
            self.assertRaisesRegex(github_credential.CredentialError, "unsupported"),
        ):
            unsupported.run_instance(self.instance, ["fetch", "origin"], label="fixture")
        run.assert_not_called()

    @unittest.skipUnless(os.geteuid() == 0, "requires a real root-to-runtime-user Git crossing")
    def test_root_uses_the_runtime_identity_for_managed_readiness_and_consumption(self) -> None:
        """Exercise real runuser, readiness, status and Git helper consumption."""
        runtime = self.runtime_account()
        if runtime is None:
            self.skipTest("no usable non-root account can import the product")
        self.set_token()
        git(self.instance, "remote", "add", "origin", "https://github.com/example/private.git")
        for path in sorted(self.instance.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            os.chown(path, runtime.pw_uid, runtime.pw_gid)
        os.chown(self.instance, runtime.pw_uid, runtime.pw_gid)
        bootstrap = Path(self.temporary.name) / "root-bootstrap"
        token = "fixture-" + secrets.token_hex(16)
        bootstrap.write_text(token + "\n", encoding="utf-8")
        bootstrap.chmod(0o600)
        os.chown(Path(self.temporary.name), runtime.pw_uid, runtime.pw_gid)
        os.chmod(Path(self.temporary.name), 0o700)

        self.assertEqual(
            github_credential.checkpoint_credential_readiness(self.instance).state, "locked/unverifiable"
        )
        managed = github_credential.RemoteExecution(
            "https://github.com/example/private.git", "checkpoint", instance_dir=self.instance
        )
        self.assertEqual(managed.credential_state.state, "managed-ready")
        self.assertEqual(_credential_snapshot(self.instance, {}, 1_700_000_000)["state"], "managed-ready")
        self.assertEqual(
            CheckpointPusher(self.instance)._remote_execution(managed.remote).credential_state.state,
            "managed-ready",
        )

        recovery = github_credential.RemoteExecution(
            "https://github.com/example/private.git", "recovery-reuse", instance_dir=self.instance
        )
        managed_result = recovery.run_instance(
            self.instance,
            ["credential", "fill"],
            label="managed recovery reuse fixture",
            input="protocol=https\nhost=github.com\n\n",
        )
        self.assertEqual(managed_result.returncode, 0, managed_result.stderr)
        managed_reply = dict(line.split("=", 1) for line in managed_result.stdout.splitlines() if "=" in line)
        self.assertEqual(self.digest(managed_reply.get("password", "")), self.digest("github-test-token"))

        remote = github_credential.RemoteExecution(
            "https://github.com/example/private.git",
            "recovery-reuse",
            instance_dir=self.instance,
            bootstrap_file=bootstrap,
        )
        restricted_tmp = Path(tempfile.mkdtemp(prefix="ummanu-restricted-tmp-", dir="/tmp"))
        restricted_tmp.chmod(0o700)
        self.addCleanup(restricted_tmp.rmdir)
        capability_directories: list[Path] = []
        real_mkdtemp = tempfile.mkdtemp

        def tracked_mkdtemp(*args, **kwargs):
            directory = Path(real_mkdtemp(*args, **kwargs))
            capability_directories.append(directory)
            return str(directory)

        with (
            mock.patch.dict(os.environ, {"TMPDIR": str(restricted_tmp)}, clear=False),
            mock.patch("ummanu.infra.github_credential.tempfile.mkdtemp", side_effect=tracked_mkdtemp),
        ):
            result = remote.run_instance(
                self.instance,
                ["credential", "fill"],
                label="root crossing fixture",
                input="protocol=https\nhost=github.com\n\n",
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        reply = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        self.assertEqual(self.digest(reply.get("password", "")), self.digest(token))
        self.assertNotIn(
            str(bootstrap), result.args if isinstance(result.args, str) else " ".join(result.args)
        )
        self.assertEqual(len(capability_directories), 1)
        self.assertFalse(capability_directories[0].exists())

    def test_bootstrap_file_accepts_sudo_original_caller_only(self) -> None:
        source = Path(self.temporary.name) / "bootstrap"
        source.write_text("fixture-bootstrap\n", encoding="utf-8")
        source.chmod(0o600)
        info = source.lstat()
        with (
            mock.patch("ummanu.infra.github_credential.os.geteuid", return_value=0),
            mock.patch.dict(os.environ, {"SUDO_UID": str(info.st_uid)}, clear=False),
        ):
            self.assertTrue(github_credential.bootstrap_file_owner_is_allowed(info))
            self.assertEqual(
                self.digest(github_credential._bootstrap_token(source)), self.digest("fixture-bootstrap")
            )
        with (
            mock.patch("ummanu.infra.github_credential.os.geteuid", return_value=0),
            mock.patch.dict(os.environ, {"SUDO_UID": str(info.st_uid + 1)}, clear=False),
        ):
            self.assertFalse(github_credential.bootstrap_file_owner_is_allowed(info))

    def test_helper_emits_only_native_protocol_reply_for_github(self) -> None:
        self.set_token()
        output = io.StringIO()
        with (
            mock.patch.dict(os.environ, github_credential.helper_environment(self.instance), clear=False),
            mock.patch("sys.stdin", io.StringIO("protocol=https\nhost=github.com\n\n")),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(github_credential.run_helper("get"), 0)
        self.assertEqual(output.getvalue(), "username=x-access-token\npassword=github-test-token\n\n")

    def test_helper_rejects_non_github_and_locked_store_without_leaking_token(self) -> None:
        token = b"github-test-token"
        self.set_token(token)
        output = io.StringIO()
        with (
            mock.patch.dict(os.environ, github_credential.helper_environment(self.instance), clear=False),
            mock.patch("sys.stdin", io.StringIO("protocol=https\nhost=example.invalid\n\n")),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(github_credential.run_helper("get"), 0)
        self.assertEqual(output.getvalue(), "")
        (self.instance / "secrets" / secret_store.KEY_NAME).unlink()
        stderr = io.StringIO()
        with (
            mock.patch.dict(os.environ, github_credential.helper_environment(self.instance), clear=False),
            mock.patch("sys.stdin", io.StringIO("protocol=https\nhost=github.com\n\n")),
            contextlib.redirect_stderr(stderr),
        ):
            self.assertEqual(github_credential.run_helper("get"), 1)
        self.assertNotIn(token.decode(), stderr.getvalue())
        self.assertEqual(
            github_credential.checkpoint_credential_readiness(self.instance).state, "locked/unverifiable"
        )

    def test_pusher_clears_ambient_helpers_and_fails_before_github_network_when_unready(self) -> None:
        commands: list[list[str]] = []

        def run_git(_instance, args, **_kwargs):
            commands.append(args)
            operation = args[-3:]
            if operation == ["remote", "get-url", "origin"]:
                return subprocess.CompletedProcess(args, 0, "https://github.com/example/private.git\n", "")
            if args[-4:] == ["symbolic-ref", "--quiet", "--short", "HEAD"]:
                return subprocess.CompletedProcess(args, 0, "main\n", "")
            if args[-1:] == ["remote"]:
                return subprocess.CompletedProcess(args, 0, "origin\n", "")
            self.fail(f"unexpected command {args}")

        with mock.patch("ummanu.checkpoint.state_repo.run_git", side_effect=run_git):
            state = CheckpointPusher(self.instance).push()
        self.assertEqual(state["status"], "failed")
        self.assertIn("missing/unavailable", state["reason"])
        self.assertFalse(any(command[0] in {"ls-remote", "push"} for command in commands))
        self.assertEqual(state["credential"]["state"], "missing/unavailable")

    def test_bootstrap_file_is_mode_checked_and_not_retained(self) -> None:
        source = Path(self.temporary.name) / "bootstrap"
        source.write_bytes(b"bootstrap-token")
        source.chmod(0o600)
        args = SimpleNamespace(bootstrap_credential_file=str(source), bootstrap_credential_stdin=False)
        path, disposable = installation_credentials._bootstrap_credential(args, self.instance / "target")
        self.assertEqual(path, source)
        self.assertIsNone(disposable)
        source.chmod(0o644)
        with self.assertRaisesRegex(installation_models.InstallError, "mode-0600"):
            installation_credentials._bootstrap_credential(args, self.instance / "target")

    def test_clone_timeout_is_a_sanitized_install_error_and_cleans_capability(self) -> None:
        bootstrap = Path(self.temporary.name) / "bootstrap-timeout"
        bootstrap.write_text("fixture-bootstrap\n", encoding="utf-8")
        bootstrap.chmod(0o600)
        capability_directories: list[Path] = []
        real_mkdtemp = tempfile.mkdtemp

        def tracked_mkdtemp(*args, **kwargs):
            directory = Path(real_mkdtemp(*args, **kwargs))
            capability_directories.append(directory)
            return str(directory)

        with (
            mock.patch(
                "ummanu.infra.github_credential._proc.run_isolated",
                side_effect=subprocess.TimeoutExpired(["git", "clone"], 300),
            ),
            mock.patch("ummanu.infra.github_credential.tempfile.mkdtemp", side_effect=tracked_mkdtemp),
            self.assertRaisesRegex(
                installation_models.InstallError, "clone instance remote: command timed out"
            ) as failure,
        ):
            installation_clone._clone_instance(
                "https://github.com/example/private.git",
                self.instance / "clone",
                bootstrap_credential=bootstrap,
            )
        self.assertNotIn("fixture-bootstrap", str(failure.exception))
        self.assertEqual(len(capability_directories), 2)
        self.assertTrue(
            any(path.name.startswith("ummanu-github-bootstrap-") for path in capability_directories)
        )
        self.assertTrue(all(not path.exists() for path in capability_directories))
        self.assertEqual(list(self.instance.glob(".clone.clone-*")), [])

    def test_bypass_status_reports_the_managed_store_condition_independently(self) -> None:
        self.set_token()
        transports = {
            "ssh://git@github.com/example/private.git": ("ambient/manual-bypass", "available"),
            str(self.instance): ("ambient/manual-bypass", "available"),
            "https://gitlab.com/example/private.git": ("missing/unavailable", "available"),
        }
        for remote, expected in transports.items():
            with (
                self.subTest(remote=remote),
                mock.patch("ummanu.checkpoint.state_repo.git", return_value=remote),
            ):
                snapshot = _credential_snapshot(self.instance, {}, 1_700_000_000)
            self.assertEqual((snapshot["state"], snapshot["store"]), expected)

        (self.instance / "secrets" / secret_store.KEY_NAME).unlink()
        with mock.patch(
            "ummanu.checkpoint.state_repo.git",
            return_value="ssh://git@github.com/example/private.git",
        ):
            locked = _credential_snapshot(self.instance, {}, 1_700_000_000)
        self.assertEqual(locked["state"], "ambient/manual-bypass")
        self.assertEqual(locked["store"], "locked")

    def test_failed_credential_attempt_keeps_the_previous_verification_timestamp(self) -> None:
        pusher = CheckpointPusher(self.instance, clock=lambda: 1000)
        pusher._credential = {"state": "locked/unverifiable", "reason": "installation key is unavailable"}
        previous = {
            "credential": {
                "state": "managed-ready",
                "last_verified_epoch": 100,
                "last_verified_at": "1970-01-01T00:01:40Z",
            }
        }
        recorded = pusher._record(previous, pusher._attempt(), 1000)
        self.assertEqual(recorded["credential"]["last_verified_epoch"], 100)
        self.assertEqual(recorded["credential"]["last_verified_at"], "1970-01-01T00:01:40Z")
