"""Regression coverage for the shared role-scoped runtime environment."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from tests.retired_board import LEGACY_ENV, STALE_FILE, legacy_runtime_lines, write_stale_leftovers
from tests.support.managed_venv import managed_product_root
from ummanu.runtime import role_env
from ummanu.runtime.head.command import wrap_role_command


class RuntimeEnvPathTests(unittest.TestCase):
    def test_both_documented_override_names_resolve_the_runtime_env_file(self) -> None:
        with mock.patch.dict(
            os.environ, {"UMMANU_RUNTIME_ENV_FILE": "/tmp/ummanu-runtime.env"}, clear=True
        ):
            self.assertEqual(role_env.runtime_env_path(), Path("/tmp/ummanu-runtime.env"))
        with mock.patch.dict(os.environ, {"TA_RUNTIME_ENV_FILE": "/tmp/ta-runtime.env"}, clear=True):
            self.assertEqual(role_env.runtime_env_path(), Path("/tmp/ta-runtime.env"))
        with mock.patch.dict(
            os.environ,
            {
                "TA_RUNTIME_ENV_FILE": "/tmp/ta-runtime.env",
                "UMMANU_RUNTIME_ENV_FILE": "/tmp/ummanu-runtime.env",
            },
            clear=True,
        ):
            self.assertEqual(role_env.runtime_env_path(), Path("/tmp/ummanu-runtime.env"))


class RuntimeEnvRoleTests(unittest.TestCase):
    @staticmethod
    def _ruff_version(root: Path) -> str:
        pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        dependency = next(
            item for item in pyproject["project"]["optional-dependencies"]["dev"] if item.startswith("ruff==")
        )
        return dependency.removeprefix("ruff==")

    def test_worker_and_reviewer_role_paths_expose_the_workspace_pinned_ruff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            expected = self._ruff_version(Path(__file__).resolve().parents[1])
            (root / "src").symlink_to(Path(__file__).resolve().parents[1] / "src", target_is_directory=True)
            ruff = root / role_env.WORKSPACE_ENV_DIR / "bin" / "ruff"
            ruff.parent.mkdir(parents=True)
            ruff.write_text(
                "#!/bin/sh\n"
                'case "$1" in\n'
                f"  --version) echo 'ruff {expected}' ;;\n"
                '  check) test "$2" = changed.py ;;\n'
                '  format) test "$2" = --check && test "$3" = changed.py ;;\n'
                "  *) exit 2 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            ruff.chmod(0o755)
            python = root / role_env.WORKSPACE_ENV_DIR / "bin" / "python3"
            python.symlink_to("/usr/bin/python3")
            (root / ".venv").symlink_to(Path(sys.prefix), target_is_directory=True)
            base_env = {
                "PATH": os.environ["PATH"],
                "UMMANU_INSTANCE": str(root),
                "UMMANU_RUNTIME_ENV_FILE": str(root / "runtime.env"),
                "UMMANU_REPO": str(root),
            }
            for role in ("worker", "reviewer"):
                with self.subTest(role=role):
                    with mock.patch.dict(os.environ, base_env, clear=True):
                        version_command = wrap_role_command(role, "ruff --version", workspace=str(root))
                        lint_commands = (
                            wrap_role_command(
                                role,
                                "printf '%s\\0' changed.py | xargs -0r ruff check",
                                workspace=str(root),
                            ),
                            wrap_role_command(
                                role,
                                "printf '%s\\0' changed.py | xargs -0r ruff format --check",
                                workspace=str(root),
                            ),
                        )
                    version = subprocess.run(
                        ["/bin/sh", "-c", version_command],
                        cwd=root,
                        env=base_env,
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(version.returncode, 0, version.stderr)
                    self.assertEqual(version.stdout.strip(), f"ruff {expected}")
                    for command in lint_commands:
                        completed = subprocess.run(
                            ["/bin/sh", "-c", command],
                            cwd=root,
                            env=base_env,
                            check=False,
                            capture_output=True,
                            text=True,
                        )
                        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_launcher_only_observer_identity_cannot_come_from_runtime_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "runtime.env"
            env_file.write_text(
                legacy_runtime_lines() + "UMMANU_OBSERVER_SPRINT=sprint:forged\n"
                "UMMANU_OBSERVER_GENERATION=forged\n",
                encoding="utf-8",
            )
            env = role_env.runtime_env("observer", base_env={"PATH": "/usr/bin"}, env_file=env_file)

        self.assertNotIn(role_env.OBSERVER_SPRINT_ENV, env)
        self.assertNotIn(role_env.OBSERVER_GENERATION_ENV, env)

    def test_memory_bearer_capability_can_only_come_from_the_launch_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "runtime.env"
            env_file.write_text("UMMANU_MEMORY_ACCESS_TOKEN=forged\n", encoding="utf-8")
            env = role_env.runtime_env("worker", base_env={"PATH": "/usr/bin"}, env_file=env_file)
            launched = role_env.runtime_env(
                "worker",
                base_env={"PATH": "/usr/bin", role_env.MEMORY_ACCESS_TOKEN_ENV: "launch-bound"},
                env_file=env_file,
            )

        self.assertNotIn(role_env.MEMORY_ACCESS_TOKEN_ENV, env)
        self.assertEqual(launched[role_env.MEMORY_ACCESS_TOKEN_ENV], "launch-bound")

    def test_scheduled_memory_roles_accept_only_a_launch_bound_bearer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "runtime.env"
            env_file.write_text("UMMANU_MEMORY_ACCESS_TOKEN=forged\n", encoding="utf-8")
            for role in ("curator", "retro", "steward"):
                with self.subTest(role=role):
                    env = role_env.runtime_env(role, base_env={"PATH": "/usr/bin"}, env_file=env_file)
                    launched = role_env.runtime_env(
                        role,
                        base_env={"PATH": "/usr/bin", role_env.MEMORY_ACCESS_TOKEN_ENV: "launch-bound"},
                        env_file=env_file,
                    )
                    self.assertNotIn(role_env.MEMORY_ACCESS_TOKEN_ENV, env)
                    self.assertEqual(launched[role_env.MEMORY_ACCESS_TOKEN_ENV], "launch-bound")

    def test_every_merged_role_builds_an_environment_with_no_transport_requirement(self) -> None:
        """No role needs, reads or passes on the retired transport, even where its leftovers remain."""
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            env_file = instance / "runtime.env"
            env_file.write_text("EXAMPLE_API_TOKEN=secret\n" + legacy_runtime_lines(), encoding="utf-8")
            write_stale_leftovers(instance)
            for role in ("worker", "reviewer", "observer", "pipeline", "steward", "retro", "curator"):
                with self.subTest(role=role):
                    env = role_env.runtime_env(
                        role,
                        base_env={"PATH": "/usr/bin", "UMMANU_INSTANCE": str(instance)},
                        env_file=env_file,
                    )
                    self.assertEqual(env["UMMANU_INSTANCE"], str(instance))
                    for name in LEGACY_ENV:
                        self.assertNotIn(name, env)
                    self.assertFalse(any(STALE_FILE in value for value in env.values()))

    def test_a_role_builds_its_environment_with_no_transport_file_and_no_runtime_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            env = role_env.runtime_env(
                "worker",
                base_env={"PATH": "/usr/bin", "UMMANU_INSTANCE": str(instance)},
                env_file=instance / "runtime.env",
            )
        self.assertEqual(env["UMMANU_INSTANCE"], str(instance))


class NoBoardTransportGateTests(unittest.TestCase):
    """The board is the PostgreSQL store, so no role's launch demands a transport."""

    def test_every_board_role_execs_without_a_transport(self) -> None:
        for role in ("pipeline", "observer", "steward", "retro"):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as tmp:
                instance = Path(tmp)
                env_file = instance / "runtime.env"
                env_file.write_text("OTHER=value\n", encoding="utf-8")
                launched: list[str] = []
                # Every role here but `pipeline` starts only under the product's managed venv.
                product = managed_product_root(instance)
                with (
                    mock.patch.dict(
                        os.environ,
                        {
                            "PATH": "/usr/bin",
                            "UMMANU_INSTANCE": str(instance),
                            "UMMANU_REPO": str(product),
                        },
                        clear=True,
                    ),
                    mock.patch.object(
                        role_env.os, "execvpe", side_effect=lambda file, *_, sink=launched: sink.append(file)
                    ),
                ):
                    code = role_env.main(
                        ["exec", "--role", role, "--env-file", str(env_file), "--", "/bin/true"]
                    )
                self.assertIsNone(code)
                self.assertEqual(launched, ["/bin/true"])


def _dispatcher_unit_path(home: Path) -> str:
    """The PATH the production dispatcher unit hands the processes it starts."""
    unit = role_env.REPO_ROOT / "packaging" / "systemd" / "ummanu-dispatcher-production.service"
    for line in unit.read_text(encoding="utf-8").splitlines():
        if line.startswith("Environment=PATH="):
            return line[len("Environment=PATH=") :].replace("{{UMMANU_RUNTIME_HOME}}", str(home))
    raise AssertionError(f"{unit} sets no PATH")


class ManagedInterpreterTests(unittest.TestCase):
    """secretary-1708: a head of a role that runs the product's own CLI finds the product's venv."""

    ROLES = ("observer", "steward", "retro", "curator")
    PROBE = "command -v python3; python3 -P -m ummanu automations --help >/dev/null; echo rc=$?"

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.instance = self.root / "instance"
        self.instance.mkdir()
        (self.instance / "runtime.env").write_text("OTHER=value\n", encoding="utf-8")
        self.product = managed_product_root(self.root)
        self.launcher_env = {
            "HOME": str(self.home),
            "UMMANU_INSTANCE": str(self.instance),
            "UMMANU_RUNTIME_ENV_FILE": str(self.instance / "runtime.env"),
            "UMMANU_REPO": str(self.product),
        }

    def run_clean(self, command: str) -> subprocess.CompletedProcess[str]:
        """`command` under a clean environment carrying only a home and the dispatcher unit's PATH."""
        return subprocess.run(
            [
                "env",
                "-i",
                f"HOME={self.home}",
                f"PATH={_dispatcher_unit_path(self.home)}",
                "/bin/sh",
                "-c",
                command,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def assert_product_python(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.split(), [str(self.product / ".venv" / "bin" / "python3"), "rc=0"], result.stderr
        )

    def test_the_helper_resolves_the_venv_of_the_checkout_the_role_imports(self) -> None:
        cases = (
            ({"UMMANU_REPO": "/srv/repo"}, "/srv/repo"),
            ({"UMMANU_RUNTIME_PYTHONPATH": "/srv/runtime", "UMMANU_REPO": "/srv/repo"}, "/srv/runtime"),
            ({}, str(role_env.REPO_ROOT)),
        )
        for environ, root in cases:
            with self.subTest(environ=environ), mock.patch.dict(os.environ, environ, clear=True):
                self.assertEqual(role_env.managed_venv_bin(), Path(root) / ".venv" / "bin")
                self.assertEqual(role_env.runtime_pythonpath(), str(Path(root) / "src"))

    def test_a_standing_role_head_runs_the_product_interpreter(self) -> None:
        """The rendered wrapper, run for real: login shell, `python3`, the product CLI."""
        for role in self.ROLES:
            with self.subTest(role=role):
                with mock.patch.dict(os.environ, self.launcher_env, clear=True):
                    command = role_env.wrap_shell_command(role, self.PROBE)
                self.assert_product_python(self.run_clean(command))

    def test_an_observer_head_under_the_dispatcher_binding_runs_the_product_interpreter(self) -> None:
        with mock.patch.dict(os.environ, self.launcher_env, clear=True):
            command = wrap_role_command(
                "observer", self.PROBE, identity=role_env.observer_binding("sprint:1459", "1")
            )
        self.assert_product_python(self.run_clean(command))

    def test_the_dispatcher_renders_an_observer_head_through_the_helper(self) -> None:
        """The command the dispatcher hands whichever backend holds an observer head."""
        from ummanu.dispatch.host import InstanceCatalog

        catalog = InstanceCatalog.__new__(InstanceCatalog)
        catalog._heads = {"profiles": {"head": {"adapter": "claude"}}}
        sentinel = Path("/sentinel/product/.venv/bin")
        with (
            mock.patch.object(catalog, "prepare_head_workspace"),
            mock.patch.object(role_env, "managed_venv_bin", return_value=sentinel),
            mock.patch.dict(os.environ, self.launcher_env, clear=True),
        ):
            launch = catalog.head_launch(
                "head",
                "prompt.md",
                workspace=str(self.root),
                role="observer",
                identity=role_env.observer_binding("sprint:1459", "1"),
            )
        prefix = f"PATH={sentinel}${{PATH:+:$PATH}}; export PATH; "
        self.assertIn(f" -- /bin/sh -lc {shlex.quote(prefix)[:-1]}", launch.command)

    def test_a_worker_head_keeps_its_workspace_venv(self) -> None:
        """Worker and reviewer run the candidate's code, never the product's installed one."""
        workspace = self.root / "workspace"
        (workspace / role_env.WORKSPACE_ENV_DIR).parent.mkdir(parents=True)
        (workspace / role_env.WORKSPACE_ENV_DIR).symlink_to(Path(sys.prefix), target_is_directory=True)
        for role in sorted(role_env.RUFF_ROLES):
            with self.subTest(role=role):
                with mock.patch.dict(os.environ, self.launcher_env, clear=True):
                    command = role_env.wrap_shell_command(role, "command -v python3", workspace=workspace)
                    env = role_env.runtime_env(role, base_env={"PATH": "/usr/bin"}, workspace=workspace)
                result = self.run_clean(command)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    result.stdout.split(), [str(workspace / role_env.WORKSPACE_ENV_DIR / "bin" / "python3")]
                )
                self.assertEqual(env["VIRTUAL_ENV"], str(workspace / role_env.WORKSPACE_ENV_DIR))
                # The product Docker guard is now first; Python still comes from the workspace.
                self.assertEqual(
                    env["PATH"].split(os.pathsep)[:2],
                    [
                        str(self.product / "src/ummanu/runtime/docker-bin"),
                        str(workspace / role_env.WORKSPACE_ENV_DIR / "bin"),
                    ],
                )
                self.assertNotIn(str(self.product / ".venv/bin"), env["PATH"])

    def test_worker_and_reviewer_launches_redirect_tool_caches_into_the_owned_namespace(self) -> None:
        """secretary-1920: test and lint caches land where owned cleanup removes them as a whole."""
        workspace = self.root / "workspace"
        (workspace / role_env.WORKSPACE_ENV_DIR).parent.mkdir(parents=True)
        (workspace / role_env.WORKSPACE_ENV_DIR).symlink_to(Path(sys.prefix), target_is_directory=True)
        namespace = workspace / ".ummanu-task-env"
        expected = {
            "PYTHONPYCACHEPREFIX": str(namespace / "pycache"),
            "RUFF_CACHE_DIR": str(namespace / "ruff-cache"),
            "MYPY_CACHE_DIR": str(namespace / "mypy-cache"),
        }
        probe = "; ".join(f'printf "%s\\n" "${name}"' for name in expected)
        for role in sorted(role_env.RUFF_ROLES):
            with self.subTest(role=role):
                with mock.patch.dict(os.environ, self.launcher_env, clear=True):
                    # The dispatcher's head binding, the command `InstanceCatalog.head_launch` renders.
                    command = wrap_role_command(role, probe, workspace=str(workspace))
                    env = role_env.runtime_env(
                        role,
                        base_env={"PATH": "/usr/bin", "PYTHONPYCACHEPREFIX": "/ambient"},
                        workspace=workspace,
                    )
                result = self.run_clean(command)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.splitlines(), list(expected.values()))
                self.assertEqual({name: env[name] for name in expected}, expected)

    def test_the_role_env_puts_the_product_venv_first(self) -> None:
        for role in self.ROLES:
            with self.subTest(role=role), mock.patch.dict(os.environ, self.launcher_env, clear=True):
                env = role_env.runtime_env(
                    role, base_env={"PATH": "/usr/bin"}, env_file=self.instance / "runtime.env"
                )
                self.assertEqual(env["PATH"], f"{self.product / '.venv' / 'bin'}{os.pathsep}/usr/bin")
                self.assertEqual(env["VIRTUAL_ENV"], str(self.product / ".venv"))
        with mock.patch.dict(os.environ, self.launcher_env, clear=True):
            env = role_env.runtime_env(
                "pipeline", base_env={"PATH": "/usr/bin"}, env_file=self.instance / "runtime.env"
            )
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertNotIn("VIRTUAL_ENV", env)

    def test_a_missing_or_unexecutable_interpreter_fails_the_launch_by_name(self) -> None:
        broken = self.root / "broken"
        (broken / ".venv" / "bin").mkdir(parents=True)
        (broken / "src").symlink_to(self.product / "src", target_is_directory=True)
        python = broken / ".venv" / "bin" / "python3"
        for state in ("missing", "not executable"):
            if state == "not executable":
                python.write_text("#!/bin/sh\n", encoding="utf-8")
                python.chmod(0o644)
            for role in self.ROLES:
                with self.subTest(state=state, role=role):
                    with mock.patch.dict(
                        os.environ, {**self.launcher_env, "UMMANU_REPO": str(broken)}, clear=True
                    ):
                        with self.assertRaisesRegex(
                            role_env.RoleEnvError, "no executable managed interpreter"
                        ):
                            role_env.require_managed_interpreter()
                        command = role_env.wrap_shell_command(role, "echo started")
                    result = self.run_clean(command)
                    self.assertEqual(result.returncode, 125, result.stderr)
                    self.assertNotIn("started", result.stdout)
                    self.assertIn(f"no executable managed interpreter at '{python}'", result.stderr)
                    self.assertIn(f"ummanu upgrade --no-pull --product-root {broken}", result.stderr)


if __name__ == "__main__":
    unittest.main()
