"""The memory bearer reaches a head through its environment, never through its launch command.

The command text of a head launch is not private: the scoped launcher runs it under `sudo`, which
writes the full command line to the system journal, and it is visible in process argv to every
process of the same user. The token is handed to the backend as the head's environment instead,
and `role_env exec` passes it on to the head from there.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.dispatcher_fixtures import FakeCatalog
from tests.support.managed_venv import guarded_product_env
from ummanu.dispatch.host import CommandHostRuntime, InstanceCatalog
from ummanu.runtime import role_env
from ummanu.runtime.head import HeadCommand, HeadRun, HeadSpec, TaskRef
from ummanu.runtime.head import command as head_command

TOKEN = "grant-id.not-a-real-secret-0123456789"
TOKEN_ENV = role_env.MEMORY_ACCESS_TOKEN_ENV


class _LaunchFinished(Exception):
    pass


class WorkerLaunchTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.base = {
            **guarded_product_env(self.root),
            "UMMANU_RUNTIME_ENV_FILE": str(self.root / "absent.env"),
        }
        self.workspace = self.root / "candidate"
        venv_bin = self.workspace / role_env.WORKSPACE_ENV_DIR / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "python3").symlink_to(sys.executable)

    def test_the_token_is_in_the_head_environment_and_not_in_its_command(self) -> None:
        catalog = object.__new__(InstanceCatalog)
        catalog._head_profile = mock.Mock(return_value={"adapter": "hermes"})
        catalog.prepare_head_workspace = mock.Mock()
        host = CommandHostRuntime(catalog, self.root / "data", mode="real", sprint_reader=mock.Mock())
        task = {"ref": "ummanu-1", "project": "ummanu"}
        runtime = mock.Mock(writes_launch_identity=True)
        started: list[dict] = []

        def start(*_args, command, env=None, **_kwargs):
            seen = subprocess.run(
                ["/bin/sh", "-c", command],
                env={**self.base, **(env or {})},
                cwd=self.workspace,
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
            started.append({"command": command, "env": dict(env or {}), "seen": seen})
            raise _LaunchFinished

        runtime.start.side_effect = start
        with (
            mock.patch.dict(os.environ, self.base, clear=True),
            mock.patch.dict(
                head_command._ADAPTERS,
                {"hermes": lambda *_args, **_kwargs: f"printenv {TOKEN_ENV} BOARD_ACTOR"},
            ),
            mock.patch.object(host, "_require_production_runtime"),
            mock.patch.object(host, "_require_workspace_environment"),
            mock.patch.object(
                host,
                "_preflight_launch_run",
                return_value=HeadRun(
                    run_id="fake-run",
                    spec=HeadSpec(profile_id="fake-head", adapter="hermes", runtime="local-pty"),
                    workspace=str(self.workspace),
                    task_ref=TaskRef.card(task["ref"]),
                ),
            ),
            mock.patch.object(host, "head_runtime_for", return_value=runtime),
            mock.patch.object(host, "_codex_provider_ingress", return_value=None),
            mock.patch.object(host, "_head_transport"),
            mock.patch(
                "ummanu.dispatch.host.memory_access.issue_grant",
                return_value=SimpleNamespace(launch_identity={TOKEN_ENV: TOKEN}),
            ),
        ):
            for role in ("worker", "reviewer"):
                with self.subTest(role=role), self.assertRaises(_LaunchFinished):
                    host._launch(
                        str(self.workspace),
                        "fake",
                        "fake-head",
                        "TASK.md",
                        role=role,
                        env_name="FAKE_HEAD_OVERRIDE",
                        task=task,
                        local_run_policy=(None, False),
                    )
                launched = started[-1]
                self.assertNotIn(TOKEN, launched["command"])
                self.assertNotIn(f"{TOKEN_ENV}=", launched["command"])
                self.assertEqual(launched["env"], {TOKEN_ENV: TOKEN})
                self.assertEqual(launched["seen"].returncode, 0, launched["seen"].stderr)
                # The head still gets the bearer, and the board actor still comes from the command.
                self.assertEqual(launched["seen"].stdout.split(), [TOKEN, "fake-head"])


class ObserverLaunchTests(unittest.TestCase):
    def test_the_observer_token_travels_beside_its_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "observer-workspace"
            workspace.mkdir()
            catalog = FakeCatalog()
            identities: list[dict[str, str]] = []

            def head_launch(*_args, identity=None, **_kwargs):
                identities.append(dict(identity or {}))
                return HeadCommand("run-observer", prompt_after_start=False, adapter="codex")

            catalog.head_launch = head_launch  # type: ignore[method-assign]
            host = CommandHostRuntime(catalog, root / "data", mode="real")  # type: ignore[arg-type]
            host._create_git_observer_workspace = lambda _placed: workspace  # type: ignore[method-assign]
            opened: list[tuple[str, dict[str, str]]] = []

            def open_pane(run, _title, command, *, env=None):
                opened.append((command, dict(env or {})))
                return dataclasses.replace(run, handle="run:observer", leaf="")

            host._open_head_pane = open_pane  # type: ignore[method-assign]
            host._stop_observer_terminals = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
            # Only the bring-up is under test; what the fixture host does after it is not.
            with (
                mock.patch(
                    "ummanu.dispatch.host.memory_access.issue_grant",
                    return_value=SimpleNamespace(launch_identity={TOKEN_ENV: TOKEN}),
                ),
                contextlib.suppress(Exception),
            ):
                host.prepare_observer({"ref": "sprint:1"}, "codex-observer", prompt="# Sprint")

        self.assertTrue(opened, "the observer pane was never opened")
        command, env = opened[0]
        self.assertNotIn(TOKEN, command)
        self.assertEqual(env, {TOKEN_ENV: TOKEN})
        self.assertTrue(identities)
        self.assertNotIn(TOKEN_ENV, identities[0])
        self.assertNotIn(TOKEN, json.dumps(identities))


if __name__ == "__main__":
    unittest.main()
