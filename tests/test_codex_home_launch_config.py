"""Every Codex home the product manages parses under Codex exactly as a pipeline head launches it.

issue:ccd29d777e3e6d86caa1: the packaged `config.toml` carried a `[mcp_servers.po_memory]` stub with
`enabled = false` and nothing else, and Codex refuses any MCP table without `command` or `url`
("invalid transport"). Removing the stub was not enough either: every pipeline head is launched with
`-c mcp_servers.po_memory.enabled=false`, and on a home without the entry that override creates the
same transport-less table. The home parses as launched only once it holds the full PO-bridge entry.

Two checks, paired. The structural one always runs: every `[mcp_servers.*]` table of every seeded or
reconciled home has a transport, before and after the head's `-c` overrides are applied to it. The
other runs the real `codex` binary with the launch argv `head/command.py` renders, on the read-only
`login status`, and skips where no `codex` is installed (CI).
"""

from __future__ import annotations

import getpass
import os
import shlex
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tests.fakes.upgrade import FakeUnitInstaller
from ummanu import upgrade
from ummanu.installation.provision import provision_codex_home
from ummanu.memory.client_config import bridge_executable, reconcile_clients
from ummanu.runtime.codex_home import managed_codex_homes
from ummanu.runtime.codex_preflight import CodexHomeLoginMissing, resolve_codex_home
from ummanu.runtime.head.command import render_head_command

REPO = Path(__file__).resolve().parents[1]
PACKAGED_HOME = REPO / "packaging" / "codex-home"
OLD_STUB = "[mcp_servers.po_memory]\nenabled = false\n"
# A login Codex reads without the network: `login status` answers from it alone.
API_KEY_LOGIN = '{"OPENAI_API_KEY": "sk-fixture-not-a-key"}\n'
CODEX = shutil.which("codex")


def launch_argv(home: Path, workspace: Path) -> list[str]:
    """The argv a pipeline Codex head is launched with, without its `CODEX_HOME=` prefix."""
    command = render_head_command(
        {"adapter": "codex", "codex_home": str(home), "model": "gpt-5.6-sol"}, workspace=str(workspace)
    ).command
    assignment, *argv = shlex.split(command)
    assert assignment == f"CODEX_HOME={home}", command
    return argv


def launch_overrides(argv: list[str]) -> list[str]:
    return [argv[index + 1] for index, token in enumerate(argv) if token == "-c"]


def _merge(target: dict[str, Any], patch: dict[str, Any]) -> None:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = value


def apply_overrides(payload: dict[str, Any], overrides: list[str]) -> dict[str, Any]:
    """`-c key.path=value` the way Codex applies it: a TOML dotted-key assignment merged in."""
    for override in overrides:
        _merge(payload, tomllib.loads(override))
    return payload


def transportless_mcp_tables(payload: dict[str, Any]) -> list[str]:
    servers = payload.get("mcp_servers") or {}
    return sorted(
        name
        for name, table in servers.items()
        if not isinstance(table, dict) or not ({"command", "url"} & table.keys())
    )


def launch_config_defects(config: Path, workspace: Path) -> list[str]:
    """Why Codex would refuse `config` as a pipeline head reads it; empty when it would not."""
    payload = tomllib.loads(config.read_text(encoding="utf-8"))
    defects = [f"as written: {name}" for name in transportless_mcp_tables(payload)]
    overrides = launch_overrides(launch_argv(config.parent, workspace))
    defects += [
        f"as launched: {name}" for name in transportless_mcp_tables(apply_overrides(payload, overrides))
    ]
    return defects


def run_codex_login_status(home: Path, workspace: Path) -> str:
    """`codex <launch flags> login status` against `home`: read-only, no network, no login."""
    assert CODEX is not None
    argv = launch_argv(home, workspace)
    completed = subprocess.run(
        [CODEX, *argv[1:], "login", "status"],
        env={**os.environ, "CODEX_HOME": str(home)},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=60,
        check=False,
    )
    return completed.stdout


class _Report:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir


class _ManagedHomes(unittest.TestCase):
    """A temp product checkout with the packaged Codex home, a runtime home and a data dir."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="ummanu-codex-home-launch.")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.runtime_home = self.root / "home"
        self.data_dir = self.root / "data"
        self.product = self.root / "product"
        shutil.copytree(PACKAGED_HOME, self.product / "packaging" / "codex-home")
        bridge = bridge_executable(self.product)
        bridge.parent.mkdir(parents=True)
        bridge.write_text("#!/bin/sh\n", encoding="utf-8")
        self.user = getpass.getuser()

    def homes(self) -> tuple[Path, ...]:
        return managed_codex_homes(self.data_dir)

    def seed_and_reconcile(self) -> None:
        provision_codex_home(self.product, self.user, data_dir=self.data_dir)
        reconcile_clients(self.product, self.runtime_home, self.data_dir)

    def old_stub_home(self) -> Path:
        home = self.root / "old-stub-home"
        home.mkdir()
        (home / "config.toml").write_text('model = "gpt-5.6-sol"\n\n' + OLD_STUB, encoding="utf-8")
        return home

    def logged_in_data_home_without_config(self) -> Path:
        """The reviewer's case: a login in `<data_dir>/codex-home` and nothing else. The resolver
        selects it on the login alone."""
        home = self.homes()[0]
        home.mkdir(parents=True)
        (home / "auth.json").write_text(API_KEY_LOGIN, encoding="utf-8")
        return home

    def selectable_home(self) -> Path:
        """The home `resolve_codex_home` picks for a head with no profile or env override."""
        environment = {key: value for key, value in os.environ.items() if key != "TA_CODEX_HOME"}
        with mock.patch.dict(os.environ, {**environment, "HOME": str(self.runtime_home)}, clear=True):
            return Path(resolve_codex_home({}, data_dir=self.data_dir).path)

    def assert_completed_home(self, home: Path) -> None:
        """The packaged defaults, the full bridge entry, the login untouched, and parseable as launched."""
        packaged = tomllib.loads((PACKAGED_HOME / "config.toml").read_text(encoding="utf-8"))
        payload = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
        self.assertEqual({key: payload.get(key) for key in packaged}, packaged)
        self.assertEqual(
            (home / "AGENTS.md").read_text(encoding="utf-8"),
            (PACKAGED_HOME / "AGENTS.md").read_text(encoding="utf-8"),
        )
        self.assertEqual(payload["mcp_servers"]["po_memory"]["command"], str(bridge_executable(self.product)))
        self.assertEqual((home / "auth.json").read_text(encoding="utf-8"), API_KEY_LOGIN)
        self.assertEqual(launch_config_defects(home / "config.toml", self.workspace), [])

    def run_upgrade_steps(self, *only: Any) -> list[upgrade.StepResult]:
        """`memory-clients` and `codex-home` in the order the upgrade runs them, or only those named."""
        steps = [
            step for step in upgrade.STEPS if step in (upgrade.step_memory_clients, upgrade.step_codex_home)
        ]
        self.assertEqual(len(steps), 2)
        if only:
            steps = [step for step in steps if step in only]
        context = upgrade.UpgradeContext(
            instance_path=self.root / "instance",
            product_root=self.product,
            base_branch="main",
            dry_run=False,
            units=FakeUnitInstaller(),
            report=_Report(self.data_dir),
            runtime_user=self.user,
            runtime_home=self.runtime_home,
        )
        return [step(context) for step in steps]


class CodexHomeLaunchConfigTests(_ManagedHomes):
    def test_nothing_packaged_holds_an_mcp_table_without_a_transport(self) -> None:
        for path in sorted((REPO / "packaging").rglob("*.toml")):
            with self.subTest(path=path.relative_to(REPO)):
                self.assertEqual(
                    transportless_mcp_tables(tomllib.loads(path.read_text(encoding="utf-8"))), []
                )

    def test_a_home_seeded_alone_already_parses_as_launched(self) -> None:
        provision_codex_home(self.product, self.user, data_dir=self.data_dir)

        for home in self.homes():
            with self.subTest(home=home.name):
                self.assertEqual(launch_config_defects(home / "config.toml", self.workspace), [])
                bridge = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))["mcp_servers"][
                    "po_memory"
                ]
                self.assertEqual(bridge["command"], str(bridge_executable(self.product)))
                self.assertEqual(
                    bridge["env"]["MEMORY_ACCESS_BINDINGS"], str(self.data_dir / "memory" / "access-grants")
                )

    def test_every_seeded_and_reconciled_config_parses_as_launched(self) -> None:
        self.seed_and_reconcile()

        configs = [home / "config.toml" for home in self.homes()]
        configs.append(self.runtime_home / ".codex" / "config.toml")
        for config in configs:
            with self.subTest(config=str(config.relative_to(self.root))):
                self.assertEqual(launch_config_defects(config, self.workspace), [])

    def test_the_structural_check_refuses_the_old_stub_and_a_home_without_the_entry(self) -> None:
        stub = self.old_stub_home() / "config.toml"
        self.assertEqual(
            launch_config_defects(stub, self.workspace), ["as written: po_memory", "as launched: po_memory"]
        )
        # The PO's hand edit on 2026-09-24: the stub gone, no entry in its place.
        stub.write_text('model = "gpt-5.6-sol"\n', encoding="utf-8")
        self.assertEqual(launch_config_defects(stub, self.workspace), ["as launched: po_memory"])

    def test_reconcile_writes_the_entry_into_a_seeded_data_dir_home_that_lacks_it(self) -> None:
        data_home = self.homes()[0]
        data_home.mkdir(parents=True)
        (data_home / "config.toml").write_text('model = "operator-choice"\n\n' + OLD_STUB, encoding="utf-8")

        result = reconcile_clients(self.product, self.runtime_home, self.data_dir)

        self.assertTrue(result.codex_data_dir)
        self.assertEqual(launch_config_defects(data_home / "config.toml", self.workspace), [])
        self.assertEqual(tomllib.loads((data_home / "config.toml").read_text())["model"], "operator-choice")

    def test_upgrade_steps_leave_a_fresh_data_dir_home_parseable_as_launched(self) -> None:
        self.assertFalse(self.data_dir.exists())

        outcomes = self.run_upgrade_steps()

        self.assertEqual([outcome.status for outcome in outcomes], ["changed", "changed"], outcomes)
        for home in self.homes():
            with self.subTest(home=home.name):
                self.assertEqual(launch_config_defects(home / "config.toml", self.workspace), [])
        # A second upgrade finds both homes current.
        self.assertEqual([outcome.status for outcome in self.run_upgrade_steps()], ["unchanged", "unchanged"])

    def test_reconcile_alone_completes_a_logged_in_home_without_a_config(self) -> None:
        home = self.logged_in_data_home_without_config()
        self.assertEqual(self.selectable_home(), home)

        result = reconcile_clients(self.product, self.runtime_home, self.data_dir)

        self.assertTrue(result.codex_data_dir)
        self.assert_completed_home(home)
        self.assertFalse(reconcile_clients(self.product, self.runtime_home, self.data_dir).codex_data_dir)

    def test_either_upgrade_step_alone_completes_a_logged_in_home_without_a_config(self) -> None:
        for step in (upgrade.step_codex_home, upgrade.step_memory_clients):
            with self.subTest(step=step.__name__):
                shutil.rmtree(self.data_dir, ignore_errors=True)
                shutil.rmtree(self.runtime_home, ignore_errors=True)
                home = self.logged_in_data_home_without_config()

                (outcome,) = self.run_upgrade_steps(step)

                self.assertEqual(outcome.status, "changed", outcome)
                self.assert_completed_home(home)

    def test_an_upgrade_interrupted_after_memory_clients_leaves_the_selected_home_parseable(self) -> None:
        for logged_in in (False, True):
            with self.subTest(data_dir_home_logged_in=logged_in):
                shutil.rmtree(self.data_dir, ignore_errors=True)
                shutil.rmtree(self.runtime_home, ignore_errors=True)
                if logged_in:
                    self.logged_in_data_home_without_config()

                # `codex-home` never runs: the upgrade stopped after `memory-clients`.
                (outcome,) = self.run_upgrade_steps(upgrade.step_memory_clients)

                self.assertEqual(outcome.status, "changed", outcome)
                if not logged_in:
                    # No login, so no home a head could be launched in (secretary-1723).
                    with self.assertRaises(CodexHomeLoginMissing):
                        self.selectable_home()
                    continue
                selected = self.selectable_home()
                self.assertEqual(selected, self.homes()[0])
                self.assertEqual(launch_config_defects(selected / "config.toml", self.workspace), [])


@unittest.skipIf(CODEX is None, "the codex binary is not on PATH; the structural checks above still run")
class RealCodexLaunchConfigTests(_ManagedHomes):
    """The same homes, read by the real `codex` binary with the pipeline head's launch flags."""

    def assert_codex_loads(self, home: Path) -> None:
        output = run_codex_login_status(home, self.workspace)
        self.assertNotIn("Error loading configuration", output)
        self.assertNotIn("invalid transport", output)

    def test_real_codex_loads_every_seeded_and_reconciled_home(self) -> None:
        self.seed_and_reconcile()
        for home in self.homes():
            with self.subTest(home=home.name):
                self.assert_codex_loads(home)

    def test_real_codex_loads_a_home_the_upgrade_steps_created(self) -> None:
        self.run_upgrade_steps()
        for home in self.homes():
            with self.subTest(home=home.name):
                self.assert_codex_loads(home)

    def test_real_codex_loads_a_logged_in_home_completed_by_any_one_path(self) -> None:
        paths = {
            "reconcile_clients": lambda: reconcile_clients(self.product, self.runtime_home, self.data_dir),
            "codex-home": lambda: self.run_upgrade_steps(upgrade.step_codex_home),
            "memory-clients": lambda: self.run_upgrade_steps(upgrade.step_memory_clients),
        }
        for name, complete in paths.items():
            with self.subTest(path=name):
                shutil.rmtree(self.data_dir, ignore_errors=True)
                shutil.rmtree(self.runtime_home, ignore_errors=True)
                home = self.logged_in_data_home_without_config()
                complete()
                self.assert_codex_loads(home)
                # The login was read, so the config got past loading to the auth it gates.
                self.assertIn("Logged in using an API key", run_codex_login_status(home, self.workspace))

    def test_real_codex_refuses_a_logged_in_home_without_a_config(self) -> None:
        # The reviewer's reproduction, before any path completes the home.
        output = run_codex_login_status(self.logged_in_data_home_without_config(), self.workspace)
        self.assertIn("invalid transport", output)

    def test_real_codex_refuses_the_old_stub_under_the_launch_flags(self) -> None:
        # The control: without it a probe that never reached config loading would pass too.
        self.assertIn(
            "Error loading configuration", run_codex_login_status(self.old_stub_home(), self.workspace)
        )


if __name__ == "__main__":
    unittest.main()
