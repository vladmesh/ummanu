"""Registered-project Git access through the managed GitHub credential boundary (secretary-1626).

The production failure was an ambient-credential `git push` retried five times as silence after the
ambient helper was cleaned up (issue:880dd4492d39c16ec5c2). These tests pin the replacement: every
remote Git operation of the dispatcher's gate-to-release cycle for a GitHub HTTPS project crosses
`RemoteExecution` as the checkout's resolved Git child, with the encrypted `github.checkpoint-token`
selected and ambient helpers, credential files and askpass disabled.

Nothing here touches the live installation, a real remote or the operator's Git configuration. Each
test runs with a throwaway HOME and global Git config, an isolated instance store, and GitHub played
by a local bare repository behind `state_repo.run_git`, the single place the boundary starts Git.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import secrets
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.dispatcher_fixtures import CARD_REF, DispatcherRuntimeFixture
from tests.head_registry import write_installed_pair
from tests.production_runtime_fixtures import registered_production_runtime
from ummanu import cli, secret_store, state_repo
from ummanu.cli import main
from ummanu.config import validate_instance
from ummanu.dispatch import gate as dispatcher_gate, gate_pr as dispatcher_gate_pr
from ummanu.dispatch.host import CommandHostRuntime
from ummanu.dispatch.launch import FAILURE_CLASS_INFRASTRUCTURE
from ummanu.dispatch.types import GateTransportError, HostError, ProjectGitAccessError
from ummanu.infra import github_credential
from ummanu.infra.github_credential import (
    PROJECT_ACCESS_REFUSALS,
    PROJECT_GIT_PHASE,
    CredentialError,
    CredentialReadiness,
    ProjectGitAccess,
    project_remote_execution,
)
from ummanu.infra.recovery_inventory import collect_recovery_inventory
from ummanu.projects.contract import ADAPTER_INVALID, ContractVerdict
from ummanu.secret_words import RECOVERY_WORDS
from ummanu.state_repo import GitChildIdentity, StateRepoError


def fast_key_params():
    """Cheap scrypt parameters: the store's format is exercised, not its work factor."""
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


REMOTE = "https://github.com/example/sample.git"
PROJECT = "sample"
BRANCH = "pipeline/sample-1"
AMBIENT_SECRET = "ambient-" + "0123456789abcdef"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], text=True, capture_output=True, check=True
    ).stdout.strip()


def commit(repo: Path, name: str) -> str:
    (repo / name).write_text(f"{name}\n", encoding="utf-8")
    git(repo, "add", name)
    git(repo, "commit", "--quiet", "-m", name)
    return git(repo, "rev-parse", "HEAD")


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class HermeticGitTestCase(unittest.TestCase):
    """A throwaway HOME and global Git configuration; the process's own are never read."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.global_config = self.home / ".gitconfig"
        self.global_config.write_text(
            "[user]\n\tname = test\n\temail = test@example.invalid\n[init]\n\tdefaultBranch = main\n",
            encoding="utf-8",
        )
        environment = mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.home),
                "XDG_CONFIG_HOME": str(self.home / ".config"),
                "GIT_CONFIG_GLOBAL": str(self.global_config),
                "GIT_CONFIG_NOSYSTEM": "1",
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("GIT_ASKPASS", "SSH_ASKPASS", "GIT_DIR", "GIT_WORK_TREE"):
            os.environ.pop(name, None)

    def repository(self, name: str, origin: str | None = None) -> Path:
        repo = self.root / name
        repo.mkdir()
        git(repo, "init", "--quiet", "--initial-branch", "main")
        if origin is not None:
            git(repo, "remote", "add", "origin", origin)
        return repo


class EffectiveRemoteClassificationTests(HermeticGitTestCase):
    def test_a_rewrite_to_github_https_cannot_bypass_managed_authentication(self) -> None:
        """AC2: the URL Git will contact decides, not the one the checkout declares."""
        shorthand = self.repository("shorthand", "gh:example/sample.git")
        git(shorthand, "config", "url.https://github.com/.insteadOf", "gh:")
        ssh_looking = self.repository("ssh-looking", "git@github.com:example/sample.git")
        git(ssh_looking, "config", "url.https://github.com/.insteadOf", "git@github.com:")
        push_only = self.repository("push-only", "git@github.com:example/sample.git")
        git(push_only, "config", "url.https://github.com/.pushInsteadOf", "git@github.com:")

        for repo in (shorthand, ssh_looking, push_only):
            with self.subTest(repo=repo.name):
                execution = project_remote_execution(repo, instance_dir=None)
                self.assertEqual(execution.phase, PROJECT_GIT_PHASE)
                self.assertIn(REMOTE, execution.effective_urls)
                self.assertEqual(execution.transport, "github-https")
        # The rewrite lives in each fixture repository; the global configuration is untouched.
        self.assertNotIn("insteadOf", self.global_config.read_text(encoding="utf-8"))

    def test_local_and_ssh_stay_explicit_and_other_https_is_a_named_refusal(self) -> None:
        cases = {
            "local": (str(self.root / "remote.git"), "local", "local"),
            "file": ("file:///fixture/remote.git", "local", "local"),
            "ssh": ("git@github.com:example/sample.git", "ssh", "manual-bypass"),
            # A non-HTTPS network URL keeps the explicit manual-bypass the checkpoint also gives it.
            "http": ("http://127.0.0.1:9/example/sample.git", "unmanaged", "manual-bypass"),
        }
        for name, (origin, transport, source) in cases.items():
            with self.subTest(name=name):
                repo = self.repository(name, origin)
                execution = project_remote_execution(repo, instance_dir=None)
                self.assertEqual(execution.transport, transport)
                access = execution.preflight_project(repo)
                self.assertEqual((access.state, access.source), ("ready", source))
        other = self.repository("other-https", "https://gitlab.example/example/sample.git")
        execution = project_remote_execution(other, instance_dir=None)
        self.assertEqual(execution.transport, "https-unsupported")
        access = execution.preflight_project(other)
        self.assertEqual((access.state, access.code), ("refused", "unsupported-https"))
        with self.assertRaises(CredentialError) as refused:
            execution.run_project(other, ["fetch", "origin"], label="fixture")
        self.assertEqual(refused.exception.code, "unsupported-https")

    def test_a_credential_bearing_origin_is_refused_before_git(self) -> None:
        bearing = self.repository("bearing", "https://user:inline-secret@github.com/example/sample.git")
        access = project_remote_execution(bearing, instance_dir=None).preflight_project(bearing)
        self.assertEqual((access.state, access.code), ("refused", "unsafe-remote"))
        self.assertIn(access.code, PROJECT_ACCESS_REFUSALS)
        self.assertNotIn("inline-secret", json.dumps(access.to_json()))

    def test_no_configured_origin_is_classified_by_what_git_would_contact(self) -> None:
        """Git reads an unconfigured remote name as a URL or path, after URL rewriting."""
        missing = self.repository("missing")
        self.assertEqual(project_remote_execution(missing, instance_dir=None).transport, "local")

        rewritten = self.repository("rewritten")
        git(rewritten, "config", f"url.{REMOTE}.insteadOf", "origin")
        self.assertEqual(project_remote_execution(rewritten, instance_dir=None).transport, "github-https")

        not_a_checkout = self.root / "plain-directory"
        not_a_checkout.mkdir()
        self.assertEqual(project_remote_execution(not_a_checkout, instance_dir=None).transport, "local")


class _Catalog:
    def __init__(self, instance_dir: Path, repo: Path) -> None:
        self.instance_dir = instance_dir
        self._repo = repo

    def binding(self, project: str) -> dict:
        return {"repo": str(self._repo), "default_branch": "main"}

    def project_default_branch(self, project: str) -> str:
        return "main"


class _Host(CommandHostRuntime):
    """The real host boundary; only `gh` is answered in process."""

    def __init__(self, catalog: _Catalog, root: Path) -> None:
        super().__init__(  # type: ignore[arg-type]
            catalog, root, mode="real", production_runtime=registered_production_runtime(root)
        )
        # Focused gate hosts own no dispatcher state file; the gate then records the lease on the record.
        self.commit_gate_published_ref = None  # type: ignore[assignment]
        self.gh: list[list[str]] = []

    def _run(self, args, label, *, cwd=None):  # type: ignore[override]
        if args[:1] == ["gh"]:
            self.gh.append(list(args))
            answer = json.dumps({"baseRefName": "main", "isDraft": False}) if args[:3] == ["gh", "pr", "view"] else ""
            return subprocess.CompletedProcess(args, 0, answer, "")
        return super()._run(args, label, cwd=cwd)


class FakeGithub:
    """GitHub as the managed boundary's Git child reaches it, served from a local bare repository.

    Installed at `state_repo.run_git`. Each remote operation is recorded with the configuration and
    environment the boundary chose; its credential exchange is then performed by Git's own credential
    protocol with exactly that configuration, and only the transport is redirected to the bare
    repository. `script` may answer an operation instead (a refusal, a timeout).
    """

    def __init__(self, bare: Path) -> None:
        self.bare = bare
        self.operations: list[dict] = []
        self.fills: list[subprocess.CompletedProcess[str]] = []
        self.script = None
        self._run_git = state_repo.run_git

    def install(self, test: unittest.TestCase) -> FakeGithub:
        patcher = mock.patch.object(state_repo, "run_git", new=self)
        patcher.start()
        test.addCleanup(patcher.stop)
        return self

    def labels(self) -> list[str]:
        return [operation["label"] for operation in self.operations]

    def __call__(self, checkout, args, *, label, timeout=120, extra_env=None, input=None, child=None):
        if str(label).startswith("resolve project"):
            return self._run_git(
                checkout, args, label=label, timeout=timeout, extra_env=extra_env, input=input, child=child
            )
        environment = dict(extra_env or {})
        self.operations.append({"args": list(args), "env": environment, "child": child, "label": label})
        if self.script is not None:
            scripted = self.script(args)
            if isinstance(scripted, BaseException):
                raise scripted
            if scripted is not None:
                return scripted
        self.fills.append(
            subprocess.run(
                ["git", *args[:4], "credential", "fill"],
                input="protocol=https\nhost=github.com\npath=example/sample.git\n\n",
                text=True,
                capture_output=True,
                env={**state_repo.git_env(), **environment},
                timeout=60,
                check=False,
            )
        )
        return self._run_git(
            checkout,
            ["-c", f"url.{self.bare}.insteadOf={REMOTE}", *args],
            label=label,
            timeout=timeout,
            extra_env=extra_env,
            input=input,
            child=child,
        )


class ManagedProjectGitTests(HermeticGitTestCase):
    phrase = " ".join(RECOVERY_WORDS[:16])

    def setUp(self) -> None:
        super().setUp()
        self.instance = self.repository("instance")
        (self.instance / "instance.yaml").write_text("version: 1\n", encoding="utf-8")
        git(self.instance, "add", "instance.yaml")
        git(self.instance, "commit", "--quiet", "-m", "initial")
        with mock.patch.object(secret_store, "_new_key_params", side_effect=fast_key_params):
            secret_store.initialize_store(self.instance, phrase=self.phrase, actor="test")
        self.token = "fixture-" + secrets.token_hex(16)
        self.bare = self.root / "github" / "sample.git"
        self.bare.parent.mkdir()
        subprocess.run(
            ["git", "init", "--quiet", "--bare", "--initial-branch", "main", str(self.bare)],
            check=True,
            capture_output=True,
        )
        seed = self.repository("seed", str(self.bare))
        commit(seed, "README.md")
        git(seed, "push", "--quiet", "origin", "main")
        self.repo = self.clone("sample")
        self.ws = self.clone("ws")
        git(self.ws, "checkout", "--quiet", "-b", BRANCH)
        commit(self.ws, "work.txt")
        self.ambient_marker = self.root / "ambient-used"

    def clone(self, name: str) -> Path:
        target = self.root / name
        subprocess.run(
            ["git", "clone", "--quiet", str(self.bare), str(target)], check=True, capture_output=True
        )
        git(target, "remote", "set-url", "origin", REMOTE)
        return target

    def set_token(self) -> None:
        secret_store.set_secret(
            self.instance,
            secret_id=github_credential.CHECKPOINT_CREDENTIAL_ID,
            value=self.token.encode("utf-8"),
            scope="installation",
            purpose=github_credential.CHECKPOINT_CREDENTIAL_PURPOSE,
            actor="test",
        )

    def install_hostile_ambient(self) -> None:
        """Every ambient way Git could authenticate: a store file, a URL-scoped helper and askpass."""
        script = self.home / "ambient-credential"
        script.write_text(
            f"#!/bin/sh\necho used >> '{self.ambient_marker}'\n"
            f"printf 'username=ambient\\npassword={AMBIENT_SECRET}\\n'\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        (self.home / ".git-credentials").write_text(
            f"https://ambient:{AMBIENT_SECRET}@github.com\n", encoding="utf-8"
        )
        with self.global_config.open("a", encoding="utf-8") as handle:
            handle.write(
                f"[credential]\n\thelper = store --file {self.home / '.git-credentials'}\n"
                f'[credential "https://github.com"]\n\thelper = !{script}\n'
                f"[core]\n\taskPass = {script}\n"
            )
        os.environ["GIT_ASKPASS"] = str(script)
        os.environ["SSH_ASKPASS"] = str(script)

    def host(self) -> _Host:
        return _Host(_Catalog(self.instance, self.repo), self.root / "data")

    def record(self) -> SimpleNamespace:
        return SimpleNamespace(workspace=str(self.ws), gate_published_ref={})

    def assert_managed(self, operation: dict) -> None:
        args = operation["args"]
        self.assertEqual(args[:3], ["-c", "credential.helper=", "-c"], "ambient helpers are cleared first")
        self.assertTrue(args[3].startswith("credential.helper=!"))
        self.assertIn("ummanu.infra.github_credential", args[3])
        self.assertEqual(operation["env"].get("GIT_ASKPASS"), "", "ambient askpass is disabled")
        self.assertEqual(operation["env"].get("UMMANU_CHECKPOINT_INSTANCE"), str(self.instance))
        self.assertEqual(operation["child"], GitChildIdentity(os.geteuid(), os.getegid()))
        self.assertNotIn(self.token, json.dumps(args) + json.dumps(operation["env"]))

    def assert_managed_fill(self, fill: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(fill.returncode, 0, fill.stderr)
        reply = dict(line.split("=", 1) for line in fill.stdout.splitlines() if "=" in line)
        self.assertEqual(reply.get("username"), "x-access-token")
        self.assertEqual(digest(reply.get("password", "")), digest(self.token))
        self.assertNotIn(AMBIENT_SECRET, fill.stdout + fill.stderr)
        self.assertNotIn(self.token, fill.stderr)

    def test_a_candidate_is_read_published_and_released_with_ambient_credentials_hostile(self) -> None:
        """AC1/AC5: preflight, base fetch, remote ref, leased publish and release refresh, all managed."""
        self.set_token()
        self.install_hostile_ambient()
        network = FakeGithub(self.bare).install(self)
        host = self.host()

        access = host.project_git_access(PROJECT)
        self.assertEqual(
            (access.state, access.transport, access.source), ("ready", "github-https", "managed-store")
        )
        self.assertEqual(dispatcher_gate._recover_base(host, str(self.ws), "main", PROJECT), "clean")
        sha = git(self.ws, "rev-parse", "HEAD")
        record = self.record()
        self.assertIsNone(dispatcher_gate_pr._publish_branch(host, record, str(self.ws), BRANCH, sha, PROJECT))
        self.assertEqual(
            git(self.bare, "rev-parse", f"refs/heads/{BRANCH}"), sha, "exact candidate published"
        )
        self.assertEqual(record.gate_published_ref, {"branch": BRANCH, "sha": sha})

        # GitHub lands the pull request; the project checkout is then refreshed through the boundary.
        git(self.bare, "update-ref", "refs/heads/main", sha)
        host._merge_github_pr({"ref": "sample-1", "project": PROJECT}, record, BRANCH, "main")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), sha)
        self.assertEqual(
            network.labels(),
            [
                "project Git access preflight",
                "gate base fetch",
                "gate remote branch sha",
                "gate publish branch",
                "post-merge fetch",
            ],
        )
        for operation in network.operations:
            self.assert_managed(operation)
        self.assertEqual(len(network.fills), len(network.operations))
        for fill in network.fills:
            self.assert_managed_fill(fill)
        self.assertFalse(self.ambient_marker.exists(), "no ambient helper or askpass was ever run")

    def test_the_managed_helper_declining_is_not_answered_by_an_ambient_credential(self) -> None:
        self.set_token()
        self.install_hostile_ambient()
        execution = project_remote_execution(self.ws, instance_dir=self.instance)
        child = state_repo.git_child_identity(self.ws)
        with execution._authorized(child) as (prefix, environment, source):
            self.assertEqual(source, "managed-store")
            declining = dict(environment, UMMANU_CHECKPOINT_INSTANCE=str(self.root / "no-instance"))
            fill = subprocess.run(
                ["git", *prefix, "credential", "fill"],
                input="protocol=https\nhost=github.com\n\n",
                text=True,
                capture_output=True,
                env={**state_repo.git_env(), **declining},
                timeout=60,
                check=False,
            )
        self.assertNotEqual(fill.returncode, 0)
        self.assertNotIn(AMBIENT_SECRET, fill.stdout + fill.stderr)
        self.assertFalse(self.ambient_marker.exists())
        self.assertEqual(github_credential.project_access_failure_code(fill.stderr), "credential-rejected")

    def test_a_foreign_push_is_still_the_typed_lease_refusal(self) -> None:
        """AC4: the durable lease and remote-moved classification survive the boundary."""
        self.set_token()
        FakeGithub(self.bare).install(self)
        host = self.host()
        record = self.record()
        first = git(self.ws, "rev-parse", "HEAD")
        self.assertIsNone(dispatcher_gate_pr._publish_branch(host, record, str(self.ws), BRANCH, first, PROJECT))
        foreign = self.root / "foreign"
        subprocess.run(
            ["git", "clone", "--quiet", str(self.bare), str(foreign)], check=True, capture_output=True
        )
        git(foreign, "checkout", "--quiet", BRANCH)
        foreign_sha = commit(foreign, "foreign.txt")
        git(foreign, "push", "--quiet", "origin", BRANCH)
        candidate = commit(self.ws, "rework.txt")

        result = dispatcher_gate_pr._publish_branch(host, record, str(self.ws), BRANCH, candidate, PROJECT)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(
            (result.failure_class, result.failure_reason), ("publication", "remote-branch-moved")
        )
        self.assertEqual(
            git(self.bare, "rev-parse", f"refs/heads/{BRANCH}"), foreign_sha, "nothing overwritten"
        )
        self.assertEqual(record.gate_published_ref, {"branch": BRANCH, "sha": first})

    def test_missing_and_locked_credentials_are_named_before_any_network_git(self) -> None:
        """AC3/AC5: no ls-remote, fetch or push starts, and the refusal is not transport silence."""
        network = FakeGithub(self.bare).install(self)
        host = self.host()
        cases = (("missing", "credential-missing"), ("locked", "credential-locked"))
        for name, code in cases:
            with self.subTest(case=name):
                if name == "locked":
                    self.set_token()
                    (self.instance / "secrets" / secret_store.KEY_NAME).unlink()
                network.operations.clear()
                access = host.project_git_access(PROJECT)
                self.assertEqual(
                    (access.state, access.code, access.transport), ("refused", code, "github-https")
                )
                with self.assertRaises(ProjectGitAccessError) as refused:
                    dispatcher_gate_pr._publish_branch(
                        host, self.record(), str(self.ws), BRANCH, git(self.ws, "rev-parse", "HEAD"), PROJECT
                    )
                self.assertNotIsInstance(refused.exception, GateTransportError)
                self.assertEqual((refused.exception.project, refused.exception.code), (PROJECT, code))
                with self.assertRaises(ProjectGitAccessError):
                    host._remote_git_checked(
                        PROJECT, self.repo, ["fetch", "origin", "main"], "post-merge fetch"
                    )
                self.assertEqual(network.operations, [], "no remote Git child was started")
                self.assertNotIn(self.token, str(refused.exception) + json.dumps(access.to_json()))

    def test_a_rejected_credential_is_refused_once_and_never_enters_the_transport_retry(self) -> None:
        """The production shape: Git's prompt fallback after the helper answered nothing usable."""
        self.set_token()
        network = FakeGithub(self.bare).install(self)
        host = self.host()
        answers = (
            (
                "remote: Invalid username or password.\n"
                "fatal: Authentication failed for 'https://github.com/example/sample.git/'\n"
            ),
            "fatal: could not read Username for 'https://github.com': terminal prompts disabled\n",
            "remote: Repository not found.\nfatal: repository 'https://github.com/example/sample.git/' not found\n",
        )
        for text in answers:
            with self.subTest(answer=text.splitlines()[-1]):
                network.operations.clear()
                network.script = lambda args, text=text: subprocess.CompletedProcess(args, 128, "", text)
                access = host.project_git_access(PROJECT)
                self.assertEqual((access.state, access.code), ("refused", "credential-rejected"))
                self.assertNotIn("sample.git", access.reason, "the reason is fixed vocabulary")
                with self.assertRaises(ProjectGitAccessError) as refused:
                    dispatcher_gate_pr._publish_branch(
                        host, self.record(), str(self.ws), BRANCH, git(self.ws, "rev-parse", "HEAD"), PROJECT
                    )
                self.assertNotIsInstance(refused.exception, GateTransportError)
                self.assertEqual(refused.exception.code, "credential-rejected")
                self.assertEqual(
                    network.labels(),
                    ["project Git access preflight", "gate remote branch sha"],
                    "one question each, and no push after a refused read",
                )

    def test_an_unanswered_managed_operation_is_still_transport(self) -> None:
        self.set_token()
        network = FakeGithub(self.bare).install(self)
        network.script = lambda args: StateRepoError("remote Git failed: Command timed out after 900 seconds")
        host = self.host()

        access = host.project_git_access(PROJECT)
        self.assertEqual((access.state, access.code), ("unreachable", "timeout"))
        with self.assertRaises(GateTransportError):
            dispatcher_gate._recover_base(host, str(self.ws), "main", PROJECT)
        with self.assertRaises(HostError) as failed:
            host._remote_git_checked(PROJECT, self.repo, ["fetch", "origin", "main"], "post-merge fetch")
        self.assertNotIsInstance(failed.exception, ProjectGitAccessError)

    def test_the_operation_capability_is_released_on_success_refusal_timeout_and_exception(self) -> None:
        self.set_token()
        network = FakeGithub(self.bare).install(self)
        entered: list[str] = []
        exited: list[str] = []
        real = github_credential._operation_capability

        @contextlib.contextmanager
        def tracked(selection, child):
            entered.append(selection.source)
            try:
                with real(selection, child) as environment:
                    yield environment
            finally:
                exited.append(selection.source)

        execution = project_remote_execution(self.ws, instance_dir=self.instance)
        scripts = {
            "success": None,
            "refusal": lambda args: subprocess.CompletedProcess(
                args, 128, "", "fatal: Authentication failed\n"
            ),
            "timeout": lambda args: StateRepoError("Command timed out after 1 seconds"),
            "exception": lambda args: RuntimeError("fixture failure"),
        }
        with mock.patch.object(github_credential, "_operation_capability", side_effect=tracked):
            for name, script in scripts.items():
                with self.subTest(outcome=name):
                    network.script = script
                    with contextlib.suppress(CredentialError, RuntimeError):
                        execution.run_project(
                            self.ws, ["ls-remote", "origin", "HEAD"], label="fixture", timeout=5
                        )
                    self.assertEqual(len(entered), len(exited), "every capability that opened was closed")
        self.assertEqual(entered, ["managed-store"] * 4)


class GitAccessClaimPreflightTests(DispatcherRuntimeFixture, unittest.TestCase):
    """AC3: project Git access is decided before the claim, beside the broad-check contract."""

    def _watch_the_preflight(self) -> None:
        self.asked_while: list[tuple[str, str, list[str]]] = []
        self.host.git_access_probe = lambda project: self.asked_while.append(
            (project, self.reader.show(CARD_REF)["state"], list(self.host.calls))
        )

    def test_a_refused_credential_blocks_the_card_before_any_workspace_or_head(self) -> None:
        self.host.git_access = ProjectGitAccess(
            "refused",
            "github-https",
            "managed-store",
            "credential-rejected",
            "managed GitHub credential was refused by the remote: authentication failed",
        )
        self._watch_the_preflight()
        self.start_dispatcher()

        blocked = self.tick()

        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["step"], "git-access-preflight")
        self.assertEqual(blocked["failure_class"], FAILURE_CLASS_INFRASTRUCTURE)
        self.assertEqual(
            (blocked["git_access"]["project"], blocked["git_access"]["code"]),
            ("ummanu", "credential-rejected"),
        )
        self.assertEqual(self.asked_while, [("ummanu", "ready", [])], "asked once, before the claim")
        reason = self.reader.show(CARD_REF)["comments"][-1]["body"]
        self.assertEqual(self.reader.show(CARD_REF)["state"], "blocked")
        self.assertIn("refusal=credential-rejected", reason)
        self.assertIn("no workspace and no head were created", reason)
        self.assertEqual(self.host.calls, [], "the host was never asked for card work")
        self.assertEqual(self.host.prepared, [])
        self.assertEqual(self.runtime.production_state.load()["records"], {})
        self.assertFalse((self.data_dir / "workspaces").exists())

        # A determinate refusal, not silence: nothing is retried on the next tick.
        self.tick()
        self.assertEqual(self.host.git_access_checks, ["ummanu"])
        self.assertEqual(self.reader.show(CARD_REF)["state"], "blocked")

    def test_an_unanswered_preflight_leaves_the_card_ready_and_unclaimed(self) -> None:
        self.host.git_access = ProjectGitAccess(
            "unreachable", "github-https", "managed-store", "network", "network access failed"
        )
        self.start_dispatcher()

        self.tick()

        self.assertEqual(self.reader.show(CARD_REF)["state"], "ready")
        self.assertEqual(self.host.calls, [])
        self.assertEqual(self.host.prepared, [])
        self.assertEqual(self.runtime.production_state.load()["records"], {})

    def test_a_contract_refusal_keeps_its_outcome_and_asks_no_git_question(self) -> None:
        self.catalog.broad_check_state = ContractVerdict.as_refused(
            ADAPTER_INVALID, "ummanu", "fixture adapter is invalid"
        )
        self.host.git_access = ProjectGitAccess(
            "refused", "github-https", "managed-store", "credential-missing"
        )
        self.start_dispatcher()

        blocked = self.tick()

        self.assertEqual(blocked["step"], "contract-preflight")
        self.assertNotIn("git_access_checks", self.host.__dict__)

    def test_a_ready_project_is_claimed_as_before(self) -> None:
        self.start_dispatcher()

        self.tick()

        self.assertEqual(self.host.git_access_checks, ["ummanu"])
        self.assertNotEqual(self.reader.show(CARD_REF)["state"], "ready")
        self.assertTrue(self.host.prepared, "the worker workspace was prepared after the preflight")


INVENTORY_REGISTRY = """resources:
  used:
    account: primary
    probe: 'true'
profiles:
  worker:
    resource: used
    adapter: codex
    fallback: []
role_defaults:
  new_card: worker
"""


class ProjectGitInventoryTests(HermeticGitTestCase):
    """AC7: recovery inventory and doctor see every registered project's Git consumer."""

    def fixture(self, projects: dict[str, str], *, token: bool = True) -> tuple[Path, object]:
        instance = self.root / "instance"
        instance.mkdir()
        (instance / "instance.yaml").write_text(
            f"version: 1\nname: test\ndata_dir: {self.root / 'data'}\n"
            "offsite:\n  instance_remote: https://github.com/example/instance.git\n",
            encoding="utf-8",
        )
        git(instance, "init", "--quiet", "--initial-branch", "main")
        git(instance, "add", "instance.yaml")
        git(instance, "commit", "--quiet", "-m", "fixture")
        git(instance, "remote", "add", "origin", "https://github.com/example/instance.git")
        write_installed_pair(instance, INVENTORY_REGISTRY)
        with mock.patch.object(secret_store, "_new_key_params", side_effect=fast_key_params):
            secret_store.initialize_store(instance, phrase=" ".join(RECOVERY_WORDS[:16]), actor="test")
        self.token = "fixture-" + secrets.token_hex(16)
        if token:
            secret_store.set_secret(
                instance,
                secret_id=github_credential.CHECKPOINT_CREDENTIAL_ID,
                value=self.token.encode("utf-8"),
                scope="installation",
                purpose=github_credential.CHECKPOINT_CREDENTIAL_PURPOSE,
                actor="test",
            )
        (instance / "projects").mkdir()
        for name, origin in projects.items():
            repo = self.repository(name, origin)
            if origin.startswith("gh:"):
                git(repo, "config", "url.https://github.com/.insteadOf", "gh:")
            (instance / "projects" / f"{name}.yaml").write_text(
                f"id: {name}\nrepo: {repo}\nenabled: true\nadapter: {name}\ndefault_branch: main\n",
                encoding="utf-8",
            )
        return instance, validate_instance(instance)

    def consumers(self, report) -> dict[str, dict]:
        snapshot = collect_recovery_inventory(report, inspect_live=False, checkpoint={})
        self.assertNotIn(self.token, json.dumps(snapshot))
        self.snapshot = snapshot
        return {row["consumer"]: row for row in snapshot["credential_consumers"]}

    def test_every_registered_project_is_a_consumer_with_its_effective_transport(self) -> None:
        _, report = self.fixture(
            {
                "rewritten": "gh:example/rewritten.git",
                "hermetic": str(self.root / "hermetic.git"),
                "keyed": "git@github.com:example/keyed.git",
                "elsewhere": "https://gitlab.example/example/elsewhere.git",
            }
        )
        rows = self.consumers(report)

        self.assertIn("checkpoint-github", rows)
        expected = {
            "project-git:rewritten": ("github-https", "managed-ready", "managed-store"),
            "project-git:hermetic": ("local", "not-applicable", "local"),
            "project-git:keyed": ("ssh", "ambient/manual-bypass", "manual-bypass"),
            "project-git:elsewhere": ("https-unsupported", "refused", "none"),
        }
        for consumer, (transport, state, source) in expected.items():
            with self.subTest(consumer=consumer):
                row = rows[consumer]
                self.assertEqual((row["transport"], row["state"], row["source"]), (transport, state, source))
                self.assertEqual(
                    row["managed_readiness"], "managed-ready", "store readiness ignores transport"
                )
                self.assertTrue(row["supported_next_action"])
        self.assertEqual(
            rows["project-git:rewritten"]["canonical_source"], "encrypted-store:github.checkpoint-token"
        )
        self.assertEqual(rows["project-git:rewritten"]["verification_source"], "managed-store-readiness")

    def test_a_missing_store_value_is_reported_whatever_the_transport(self) -> None:
        _, report = self.fixture(
            {"rewritten": "gh:example/rewritten.git", "hermetic": str(self.root / "hermetic.git")},
            token=False,
        )
        rows = self.consumers(report)

        self.assertEqual(rows["project-git:rewritten"]["state"], "missing/unavailable")
        self.assertIn("checkpoint-github set", rows["project-git:rewritten"]["supported_next_action"])
        self.assertEqual(rows["project-git:hermetic"]["state"], "not-applicable")
        self.assertEqual(rows["project-git:hermetic"]["managed_readiness"], "missing/unavailable")

    def test_each_project_row_verifies_readiness_as_its_own_git_child(self) -> None:
        """A project checkout owned by another Git user is verified as that user.

        Root doctor with the instance checkout and a project checkout owned by different
        unprivileged users: the dispatcher's preflight reads the store as the project's child,
        so the inventory row must not borrow the instance owner's verdict.
        """
        _, report = self.fixture({"shared": "gh:example/shared.git", "other": "gh:example/other.git"})
        baseline = self.consumers(report)
        instance_owner = GitChildIdentity(os.geteuid(), os.getegid())
        project_owner = GitChildIdentity(os.geteuid() + 4242, os.getegid() + 4242, "project-user")
        other_checkout = (self.root / "other").resolve()
        real_identity = state_repo.git_child_identity
        asked: list[tuple[Path, GitChildIdentity]] = []

        def identity(path):
            if Path(path).expanduser().resolve() == other_checkout:
                return project_owner
            return real_identity(path)

        def readiness(instance_dir, child):
            asked.append((Path(instance_dir), child))
            if child == project_owner:
                return CredentialReadiness("locked/unverifiable", "installation key is unavailable")
            return CredentialReadiness("managed-ready")

        with (
            mock.patch.object(state_repo, "git_child_identity", side_effect=identity),
            mock.patch(
                "ummanu.infra.recovery_inventory.checkpoint_credential_readiness_for_child",
                side_effect=readiness,
            ),
        ):
            rows = self.consumers(report)

        shared, other = rows["project-git:shared"], rows["project-git:other"]
        self.assertEqual((shared["state"], shared["managed_readiness"]), ("managed-ready", "managed-ready"))
        self.assertEqual(shared["supported_next_action"], "none")
        self.assertEqual((other["transport"], other["source"]), ("github-https", "managed-store"))
        self.assertEqual(
            (other["state"], other["managed_readiness"]), ("locked/unverifiable", "locked/unverifiable")
        )
        self.assertIn("checkpoint-github set", other["supported_next_action"])
        self.assertEqual(
            sorted((child.uid for _, child in asked)),
            sorted([instance_owner.uid, project_owner.uid]),
            "readiness is asked once per distinct Git child, as that child",
        )
        self.assertTrue(all(directory == self.root / "instance" for directory, _ in asked))
        self.assertEqual(rows["checkpoint-github"], baseline["checkpoint-github"], "checkpoint row unchanged")

    def test_ambient_credential_advice_depends_on_the_inventoried_consumers(self) -> None:
        (self.home / ".git-credentials").write_text(
            "https://ambient:placeholder@github.com\n", encoding="utf-8"
        )
        with self.global_config.open("a", encoding="utf-8") as handle:
            handle.write(f"[credential]\n\thelper = store --file {self.home / '.git-credentials'}\n")
        cases = {
            "dependent": {"managed": "gh:example/managed.git", "elsewhere": "https://gitlab.example/x/y.git"},
            "managed-only": {"managed": "gh:example/managed.git"},
        }
        for name, projects in cases.items():
            with self.subTest(case=name):
                if (self.root / "instance").exists():
                    self.tearDown_fixture()
                _, report = self.fixture(projects)
                self.consumers(report)
                ambient = [
                    row
                    for row in self.snapshot["bypasses"]
                    if row.get("kind") in {"credential-helper", "ambient-credential-file"}
                ]
                self.assertEqual(
                    {row["kind"] for row in ambient}, {"credential-helper", "ambient-credential-file"}
                )
                for row in ambient:
                    action = row["supported_next_action"]
                    self.assertNotIn("placeholder", json.dumps(row))
                    if name == "dependent":
                        self.assertTrue(action.startswith("keep the ambient"), action)
                        self.assertIn("elsewhere", action)
                    else:
                        self.assertIn("retire it only after confirming", action)
                    self.assertNotIn("safe to remove", action)

    def tearDown_fixture(self) -> None:
        import shutil

        for name in ("instance", "managed", "elsewhere"):
            shutil.rmtree(self.root / name, ignore_errors=True)

    def test_doctor_prints_and_finds_a_refused_project_consumer(self) -> None:
        instance, _ = self.fixture({"elsewhere": "https://gitlab.example/example/elsewhere.git"})
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            main(["doctor", "--offline", "--instance", str(instance)])
        text = output.getvalue()
        line = next(line for line in text.splitlines() if "project-git:elsewhere" in line)
        self.assertIn("refused", line)
        self.assertIn("transport=https-unsupported", line)
        self.assertNotIn(self.token, text)
        findings = cli._recovery_findings(
            collect_recovery_inventory(validate_instance(instance), inspect_live=False, checkpoint={})
        )
        self.assertIn(
            ("credential_consumer", "project-git:elsewhere"),
            {(finding.get("code"), finding.get("consumer")) for finding in findings},
        )


if __name__ == "__main__":
    unittest.main()
