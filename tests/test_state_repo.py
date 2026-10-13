"""Privilege-boundary regressions for instance repository lifecycle writes."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest import mock

from ummanu import checkpoint, state_repo, upgrade
from ummanu.installation import support as installation_support


def _git(repo: Path, *args: str) -> str:
    """Plain Git for fixture setup, deliberately not the boundary under test."""
    environment = dict(os.environ)
    for name in state_repo.GIT_SELECTION_VARIABLES:
        environment.pop(name, None)
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        check=True,
        env=environment,
    )
    return result.stdout


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "--quiet", "--initial-branch", "main")
    _git(repo, "config", "user.name", "operator")
    _git(repo, "config", "user.email", "operator@example.invalid")
    (repo / "instance.yaml").write_text("version: 1\n", encoding="utf-8")
    _git(repo, "add", "instance.yaml")
    _git(repo, "commit", "--quiet", "-m", "config")


class StateRepoPrivilegeTests(unittest.TestCase):
    def test_root_delegates_git_to_the_instance_owner_before_git_starts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            result = SimpleNamespace(returncode=0, stdout="", stderr="")
            account = SimpleNamespace(pw_name="runtime")
            with (
                mock.patch("ummanu.state_repo.os.getuid", return_value=0),
                mock.patch("ummanu.state_repo.pwd.getpwuid", return_value=account),
                mock.patch("ummanu.state_repo._proc.run_isolated", return_value=result) as run,
            ):
                state_repo.git(instance, ["status", "--porcelain"], label="test")

        command = run.call_args.args[0]
        self.assertEqual(command[:4], ["runuser", "--user", "runtime", "--"])
        self.assertEqual(command[4], "env")
        self.assertIn("GIT_TERMINAL_PROMPT=0", command)
        self.assertIn("GIT_SSH_COMMAND=ssh -o BatchMode=yes", command)
        self.assertIn("git", command)
        self.assertIn(f"safe.directory={instance.resolve()}", command)
        self.assertIn("core.hooksPath=/dev/null", command)
        self.assertEqual(run.call_args.kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")

    def test_root_owner_crossing_restates_controlled_helper_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            result = SimpleNamespace(returncode=0, stdout="", stderr="")
            account = SimpleNamespace(pw_name="runtime")
            with (
                mock.patch("ummanu.state_repo.os.getuid", return_value=0),
                mock.patch("ummanu.state_repo.pwd.getpwuid", return_value=account),
                mock.patch("ummanu.state_repo._proc.run_isolated", return_value=result) as run,
            ):
                state_repo.run_git(
                    instance,
                    ["status", "--porcelain"],
                    label="test",
                    extra_env={"UMMANU_CHECKPOINT_INSTANCE": str(instance)},
                )
        command = run.call_args.args[0]
        self.assertIn(f"UMMANU_CHECKPOINT_INSTANCE={instance}", command)
        self.assertLess(command.index("UMMANU_CHECKPOINT_INSTANCE=" + str(instance)), command.index("git"))

    def test_root_boundary_never_runs_runtime_hooks_or_config_as_root(self) -> None:
        """The pusher shares this boundary before any Git subcommand starts."""
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            result = SimpleNamespace(returncode=0, stdout="", stderr="")
            account = SimpleNamespace(pw_name="runtime")
            with (
                mock.patch("ummanu.state_repo.os.getuid", return_value=0),
                mock.patch("ummanu.state_repo.pwd.getpwuid", return_value=account),
                mock.patch("ummanu.state_repo._proc.run_isolated", return_value=result) as run,
            ):
                state_repo.run_git(instance, ["push", "origin", "HEAD:main"], label="test")

        command = run.call_args.args[0]
        git = command.index("git")
        self.assertEqual(command[:4], ["runuser", "--user", "runtime", "--"])
        self.assertEqual(
            command[git + 1 : git + 5],
            [
                "-c",
                f"safe.directory={instance.resolve()}",
                "-c",
                "core.hooksPath=/dev/null",
            ],
        )

    def test_non_root_calls_git_directly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = SimpleNamespace(returncode=0, stdout="", stderr="")
            with mock.patch("ummanu.state_repo._proc.run_isolated", return_value=result) as run:
                state_repo.git(Path(tmp), ["status", "--porcelain"], label="test")
        self.assertEqual(run.call_args.args[0][0], "git")


class GitEnvironmentTests(unittest.TestCase):
    """The canonical boundary drops the caller's repository selection."""

    CONTAMINATION: ClassVar[dict[str, str]] = {
        "GIT_DIR": "/foreign/.git",
        "GIT_INDEX_FILE": "/foreign/.git/index",
        "GIT_WORK_TREE": "/foreign",
        "GIT_OBJECT_DIRECTORY": "/foreign/.git/objects",
    }

    def test_git_env_removes_every_repository_selection_variable(self) -> None:
        with mock.patch.dict(os.environ, self.CONTAMINATION, clear=False):
            env = state_repo.git_env()
        for name in state_repo.GIT_SELECTION_VARIABLES:
            self.assertNotIn(name, env)
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["GIT_SSH_COMMAND"], "ssh -o BatchMode=yes")

    def test_run_git_starts_the_child_with_a_scrubbed_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = SimpleNamespace(returncode=0, stdout="", stderr="")
            with (
                mock.patch.dict(os.environ, self.CONTAMINATION, clear=False),
                mock.patch("ummanu.state_repo._proc.run_isolated", return_value=result) as run,
            ):
                state_repo.run_git(Path(tmp), ["status", "--porcelain"], label="test")
        env = run.call_args.kwargs["env"]
        for name in state_repo.GIT_SELECTION_VARIABLES:
            self.assertNotIn(name, env)

    def test_root_owner_crossing_unsets_the_selection_variables_too(self) -> None:
        """`runuser` rebuilds the environment; the crossing must not restore it."""
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            result = SimpleNamespace(returncode=0, stdout="", stderr="")
            account = SimpleNamespace(pw_name="runtime")
            with (
                mock.patch.dict(os.environ, self.CONTAMINATION, clear=False),
                mock.patch("ummanu.state_repo.os.getuid", return_value=0),
                mock.patch("ummanu.state_repo.pwd.getpwuid", return_value=account),
                mock.patch("ummanu.state_repo._proc.run_isolated", return_value=result) as run,
            ):
                state_repo.git(instance, ["status", "--porcelain"], label="test")
        command = run.call_args.args[0]
        self.assertEqual(command[4], "env")
        for name in state_repo.GIT_SELECTION_VARIABLES:
            self.assertLess(command.index(name), command.index("git"))
            self.assertEqual(command[command.index(name) - 1], "--unset")

    def test_upgrade_and_installation_children_take_the_same_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = SimpleNamespace(returncode=0, stdout="", stderr="")
            with (
                mock.patch.dict(os.environ, self.CONTAMINATION, clear=False),
                mock.patch("ummanu.upgrade._proc.run", return_value=result) as run,
            ):
                upgrade._git(root, ["rev-parse", "HEAD"])
            upgrade_env = run.call_args.kwargs["env"]
            with (
                mock.patch.dict(os.environ, self.CONTAMINATION, clear=False),
                mock.patch("ummanu.installation.support._proc.run", return_value=result) as run,
            ):
                installation_support._run(["git", "clone", "--", "remote", str(root)], label="clone")
            install_env = run.call_args.kwargs["env"]
        for name in state_repo.GIT_SELECTION_VARIABLES:
            self.assertNotIn(name, upgrade_env)
            self.assertNotIn(name, install_env)


class ForeignRepositorySelectionTests(unittest.TestCase):
    """A contaminated caller environment must not move the write."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        root = Path(self.tmpdir.name)
        self.target = root / "target"
        self.foreign = root / "foreign"
        _init_repo(self.target)
        _init_repo(self.foreign)
        # Give the two histories distinct tips, so "landed in the target" cannot
        # pass by the repositories happening to agree.
        (self.foreign / "foreign.yaml").write_text("foreign: 1\n", encoding="utf-8")
        _git(self.foreign, "add", "foreign.yaml")
        _git(self.foreign, "commit", "--quiet", "-m", "foreign")
        self.foreign_head = _git(self.foreign, "rev-parse", "HEAD").strip()
        self.contamination = {
            "GIT_DIR": str(self.foreign / ".git"),
            "GIT_INDEX_FILE": str(self.foreign / ".git" / "index"),
            "GIT_WORK_TREE": str(self.foreign),
            "GIT_OBJECT_DIRECTORY": str(self.foreign / ".git" / "objects"),
        }

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_memory_journal_write_lands_in_the_target_repository(self) -> None:
        fact = state_repo.memory_facts_dir(self.target) / "fact-1443.md"
        fact.parent.mkdir(parents=True, exist_ok=True)
        fact.write_text("inherited GIT_DIR must not redirect this\n", encoding="utf-8")

        target_head_before = _git(self.target, "rev-parse", "HEAD").strip()
        with mock.patch.dict(os.environ, self.contamination, clear=False):
            commit = state_repo.commit(self.target, state_repo.MEMORY_PATHSPEC, "memory: one fact")

        self.assertIsNotNone(commit)
        self.assertNotEqual(commit, target_head_before)
        self.assertEqual(_git(self.target, "rev-parse", "HEAD").strip(), commit)
        self.assertIn(
            "state/memory/facts/fact-1443.md",
            _git(self.target, "show", "--name-only", "--format=", "HEAD"),
        )
        # The foreign repository is untouched: no commit, no index, no objects.
        self.assertEqual(_git(self.foreign, "rev-parse", "HEAD").strip(), self.foreign_head)
        self.assertEqual(_git(self.foreign, "status", "--porcelain").strip(), "")

    def test_checkpoint_snapshot_reads_the_target_repository(self) -> None:
        with mock.patch.dict(os.environ, self.contamination, clear=False):
            snapshot = checkpoint.checkpoint_snapshot(self.target)
        self.assertEqual(snapshot["last_commit"], _git(self.target, "rev-parse", "HEAD").strip())
        self.assertNotEqual(snapshot["last_commit"], self.foreign_head)


class InstancePackingControlTests(unittest.TestCase):
    """Lifecycle packing controls stay in the one private instance checkout."""

    def test_controls_are_idempotent_local_and_do_not_touch_global_or_project_repositories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = root / "instance"
            project = root / "project"
            global_config = root / "global.gitconfig"
            _init_repo(instance)
            _init_repo(project)
            global_config.write_text("[user]\n\tname = untouched\n", encoding="utf-8")

            with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(global_config)}, clear=False):
                changed = state_repo.configure_packing_controls(instance)
                again = state_repo.configure_packing_controls(instance)

            self.assertEqual(changed, tuple(key for key, _ in state_repo.PACKING_CONTROLS))
            self.assertEqual(again, ())
            self.assertEqual(
                state_repo.packing_controls(instance), dict(state_repo.PACKING_CONTROLS)
            )
            self.assertEqual(global_config.read_text(encoding="utf-8"), "[user]\n\tname = untouched\n")
            project_config = (project / ".git" / "config").read_text(encoding="utf-8")
            self.assertNotIn("[pack]", project_config)

    def test_controls_replace_duplicate_local_values_with_one_expected_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp) / "instance"
            _init_repo(instance)
            state_repo.configure_packing_controls(instance)
            _git(instance, "config", "--local", "--add", "pack.threads", "1")
            _git(instance, "config", "--local", "--add", "pack.threads", "1")

            changed = state_repo.configure_packing_controls(instance)

            self.assertEqual(changed, ("pack.threads",))
            self.assertEqual(state_repo.packing_controls(instance), dict(state_repo.PACKING_CONTROLS))
            self.assertEqual(_git(instance, "config", "--local", "--get-all", "pack.threads"), "1\n")


if __name__ == "__main__":
    unittest.main()
