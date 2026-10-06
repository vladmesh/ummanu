"""The interactive head's workspace `<data>/interactive`: composition, upgrade, recover, shell, reach.

Decision on ummanu-33: the shared part ships in the product, the personal part lives in the live root
(`persona/AGENTS.md`), and only the interactive head receives the composition.
"""

from __future__ import annotations

import argparse
import io
import os
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ummanu import cli, installation, session, upgrade
from ummanu.infra.export_allowlist import is_exported
from ummanu.memory import client_config
from ummanu.po import workspace as po_workspace
from ummanu.role_skills import BIN_DIR_ENV, MANIFEST, sync
from ummanu.runtime import heads as head_registry, interactive_workspace as iw, role_env

ROOT = MANIFEST.parent.parent
SENTINEL = "persona-sentinel-6f1c2a"
PERSONAL = f"# Personal\n\nAnswer tersely. {SENTINEL}\r\nno trailing newline".encode()


def instance_yaml(data_dir: Path) -> str:
    return (
        f"version: 1\nname: interactive\ndata_dir: {data_dir}\n"
        "offsite:\n  instance_remote: git@example.invalid:x/y.git\n"
        "host:\n  unit_prefix: ummanu-\n"
    )


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.product = self.root / "product"
        self.instance = self.root / "instance"
        self.data = self.root / "data"
        self.workspace = self.data / iw.WORKSPACE_NAME
        source = iw.shared_source(self.product)
        source.parent.mkdir(parents=True)
        source.write_bytes(iw.shared_source(ROOT).read_bytes())
        self.instance.mkdir()
        self.write_personal(PERSONAL)

    def write_personal(self, payload: bytes) -> None:
        path = iw.personal_source(self.instance)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)

    def context(self, *, dry_run: bool = False, runtime_user: str | None = None) -> upgrade.UpgradeContext:
        return upgrade.UpgradeContext(
            instance_path=self.instance,
            product_root=self.product,
            base_branch="main",
            dry_run=dry_run,
            units=None,
            report=SimpleNamespace(data_dir=self.data),
            runtime_user=runtime_user,
        )

    def run_step(self, **kwargs) -> upgrade.StepResult:
        return upgrade.step_interactive_workspace(self.context(**kwargs))

    def agents(self) -> bytes:
        return (self.workspace / iw.AGENTS_FILE).read_bytes()


class SharedPartTests(unittest.TestCase):
    """The shared part ships nothing personal and nothing of the retired layout."""

    def setUp(self) -> None:
        self.text = iw.shared_source(ROOT).read_text(encoding="utf-8")

    def test_it_names_no_retired_layout_and_no_owner(self) -> None:
        for word in ("secretary-instance", "orca", "policies", "vladmesh"):
            with self.subTest(word=word):
                self.assertNotIn(word, self.text.lower())

    def test_it_carries_no_owner_preference(self) -> None:
        # The language and the style are the owner's: they belong to the personal part.
        self.assertIsNone(re.search(r"[Ѐ-ӿ]", self.text), "Cyrillic text in the shared part")
        for word in ("russian", "english", "em-dash", "em dash", "panelmem"):
            with self.subTest(word=word):
                self.assertNotIn(word, self.text.lower())

    def test_interactive_role_does_not_claim_po_only_owner_decision_authority(self) -> None:
        self.assertNotIn("--role po", self.text)
        self.assertNotIn("The PO can supply", self.text)
        self.assertIn("PO-only authority", self.text)

    def test_the_personal_part_is_exported_with_the_snapshot(self) -> None:
        self.assertTrue(is_exported(iw.PERSONAL_SOURCE_RELATIVE.as_posix()))


class UpgradeStepTests(Fixture):
    def test_a_first_run_writes_shared_separator_personal_and_the_claude_pointer(self) -> None:
        result = self.run_step()

        self.assertEqual(result.status, "changed", result.detail)
        shared = iw.shared_source(ROOT).read_bytes()
        self.assertEqual(self.agents(), shared + iw.SEPARATOR + PERSONAL)
        self.assertTrue(self.agents().endswith(PERSONAL))
        self.assertEqual((self.workspace / iw.CLAUDE_FILE).read_bytes(), b"@AGENTS.md\n")
        self.assertIn(iw.digest(PERSONAL), result.detail)

    def test_a_second_run_with_unchanged_sources_is_unchanged(self) -> None:
        self.run_step()
        before = {path.name: path.stat().st_mtime_ns for path in self.workspace.iterdir()}

        result = self.run_step()

        self.assertEqual(result.status, "unchanged", result.detail)
        self.assertEqual(before, {path.name: path.stat().st_mtime_ns for path in self.workspace.iterdir()})

    def test_a_changed_shared_part_is_changed(self) -> None:
        self.run_step()
        source = iw.shared_source(self.product)
        source.write_bytes(source.read_bytes() + b"\nA new shared rule.\n")

        result = self.run_step()

        self.assertEqual(result.status, "changed", result.detail)
        self.assertEqual(self.agents(), source.read_bytes() + iw.SEPARATOR + PERSONAL)

    def test_a_changed_personal_part_is_changed(self) -> None:
        self.run_step()
        self.write_personal(PERSONAL + b"\nmore")

        result = self.run_step()

        self.assertEqual(result.status, "changed", result.detail)
        self.assertTrue(self.agents().endswith(PERSONAL + b"\nmore"))

    def test_without_a_personal_part_the_workspace_holds_the_shared_part_alone(self) -> None:
        iw.personal_source(self.instance).unlink()

        result = self.run_step()

        self.assertEqual(result.status, "changed", result.detail)
        self.assertEqual(self.agents(), iw.shared_source(ROOT).read_bytes())
        self.assertIn("personal absent", result.detail)
        self.assertIn("personal absent", iw.status_line(iw.describe(self.data)))

    def test_a_hand_edit_is_restored(self) -> None:
        self.run_step()
        (self.workspace / iw.AGENTS_FILE).write_text("edited\n", encoding="utf-8")

        self.assertEqual(self.run_step().status, "changed")
        self.assertTrue(self.agents().endswith(PERSONAL))

    def test_a_dry_run_writes_nothing(self) -> None:
        result = self.run_step(dry_run=True)

        self.assertEqual(result.status, "changed", result.detail)
        self.assertIn("would write", result.detail)
        self.assertFalse(self.workspace.exists())

    def test_a_checkout_without_the_shared_part_is_skipped_by_name(self) -> None:
        iw.shared_source(self.product).unlink()

        result = self.run_step()

        self.assertEqual(result.status, "skipped")
        self.assertIn(str(iw.SHARED_SOURCE_RELATIVE), result.detail)
        self.assertFalse(self.workspace.exists())

    def test_an_unreadable_shared_part_fails_by_name(self) -> None:
        source = iw.shared_source(self.product)
        source.unlink()
        source.mkdir()

        result = self.run_step()

        self.assertEqual(result.status, "failed")
        self.assertIn(str(source), result.detail)

    def test_a_root_invoker_hands_the_workspace_to_the_runtime_user(self) -> None:
        account = SimpleNamespace(pw_uid=4321, pw_gid=4321)
        with (
            mock.patch("ummanu.upgrade.os.geteuid", return_value=0),
            mock.patch("ummanu.upgrade.pwd.getpwnam", return_value=account),
            mock.patch("ummanu.upgrade.os.chown") as chown,
        ):
            result = self.run_step(runtime_user="runtime")

        self.assertFalse(result.failed, result.detail)
        owned = {Path(call.args[0]) for call in chown.call_args_list}
        for name in ("", iw.AGENTS_FILE, iw.CLAUDE_FILE, iw.SOURCES_FILE):
            self.assertIn(self.workspace / name if name else self.workspace, owned)

    def test_a_non_root_invoker_changes_no_owner(self) -> None:
        with (
            mock.patch("ummanu.upgrade.os.geteuid", return_value=1000),
            mock.patch("ummanu.upgrade.os.chown") as chown,
        ):
            result = self.run_step(runtime_user="runtime")

        self.assertFalse(result.failed, result.detail)
        chown.assert_not_called()

    def test_the_step_is_an_upgrade_step_that_runs_before_the_host(self) -> None:
        names = [step.__name__ for step in upgrade.STEPS]

        self.assertLess(names.index("step_po_workspace"), names.index("step_interactive_workspace"))
        self.assertLess(names.index("step_interactive_workspace"), names.index("step_host"))


class RecoverTests(Fixture):
    """Recover runs the same step against the live root it extracted from the snapshot."""

    def test_recover_produces_the_upgrade_workspace_from_a_snapshot_live_root(self) -> None:
        self.run_step()
        upgraded = {path.name: path.read_bytes() for path in self.workspace.iterdir()}
        recovered_data = self.root / "recovered-data"
        live_root = self.root / "recovered-instance"
        live_root.mkdir()
        # An exporter-shaped live root: the allowlisted files, no Git.
        (live_root / "snapshot-manifest.json").write_text("{}\n", encoding="utf-8")
        (live_root / "instance.yaml").write_text(instance_yaml(recovered_data), encoding="utf-8")
        (live_root / "persona").mkdir()
        (live_root / "persona" / "AGENTS.md").write_bytes(PERSONAL)

        def interactive_only(context, steps=installation.STEPS):
            self.assertIn(upgrade.step_interactive_workspace, steps)
            return upgrade.run_steps(context, steps=(upgrade.step_interactive_workspace,))

        with (
            mock.patch.object(installation, "run_steps", side_effect=interactive_only),
            mock.patch.object(installation, "check_product_runtime"),
        ):
            result = installation.materialize_host(live_root, self.product)

        self.assertTrue(result.ok, result.render())
        recovered = recovered_data / iw.WORKSPACE_NAME
        self.assertEqual(upgraded, {path.name: path.read_bytes() for path in recovered.iterdir()})


class NoLeakTests(Fixture):
    """The persona reaches `<data>/interactive` only: not the PO, observer, worker or reviewer
    workspaces, and not the owner's `~/.claude`."""

    def test_materialisation_writes_the_persona_nowhere_else(self) -> None:
        home = self.root / "home"
        private = home / ".claude" / "CLAUDE.md"
        private.parent.mkdir(parents=True)
        private.write_text("# the owner's own rules\n", encoding="utf-8")
        bridge = client_config.bridge_executable(self.product)
        bridge.parent.mkdir(parents=True)
        bridge.write_text("#!/bin/sh\n", encoding="utf-8")
        po_source = po_workspace.agents_source(self.product)
        po_source.parent.mkdir(parents=True)
        po_source.write_bytes(po_workspace.agents_source(ROOT).read_bytes())
        (self.product / ".venv").mkdir(exist_ok=True)
        role_workspaces = (
            self.data / "workspaces" / "observers" / "sprint-1",
            self.data / "workspaces" / "ummanu" / "ummanu-1-worker",
            self.data / "workspaces" / "ummanu" / "ummanu-1-review",
        )
        for path in role_workspaces:
            path.mkdir(parents=True)
            (path / "TASK.md").write_text("a card\n", encoding="utf-8")

        context = self.context()
        with mock.patch.dict(os.environ, {"HOME": str(home), BIN_DIR_ENV: str(self.root / "bin")}):
            for step in (upgrade.step_po_workspace, upgrade.step_interactive_workspace):
                self.assertFalse(step(context).failed)
            sync(
                instance_path=self.instance,
                product_manifest=MANIFEST,
                home=home,
                data_dir=self.data,
            )

        self.assertIn(SENTINEL.encode(), self.agents())
        # The skill shells were delivered into, so their absence of the persona means something.
        self.assertTrue(any((home / ".claude" / "skills").iterdir()))
        self.assertTrue(any((self.data / "po" / ".claude" / "skills").iterdir()))
        carriers = [
            path
            for path in (*self.data.rglob("*"), *home.rglob("*"))
            if path.is_file() and SENTINEL.encode() in path.read_bytes()
        ]
        self.assertEqual(carriers, [self.workspace / iw.AGENTS_FILE])
        self.assertTrue((self.data / "po" / "AGENTS.md").is_file())
        self.assertEqual(private.read_text(encoding="utf-8"), "# the owner's own rules\n")
        for path in role_workspaces:
            self.assertEqual(sorted(p.name for p in path.iterdir()), ["TASK.md"])

    def test_only_the_interactive_workspace_reads_the_persona(self) -> None:
        # A string literal that starts with the persona directory, in any product module.
        literal = re.compile(r"""["']persona(["'/])""")
        readers = sorted(
            str(path.relative_to(ROOT))
            for path in (ROOT / "src" / "ummanu").rglob("*.py")
            if literal.search(path.read_text(encoding="utf-8"))
        )
        # The allowlist only exports it into the snapshot; the workspace is the one place it is delivered.
        self.assertEqual(
            readers, ["src/ummanu/infra/export_allowlist.py", "src/ummanu/runtime/interactive_workspace.py"]
        )


class StatusLineTests(Fixture):
    def test_the_line_names_the_path_and_both_source_digests(self) -> None:
        self.run_step()

        line = iw.status_line(iw.describe(self.data))

        shared = iw.digest(iw.shared_source(ROOT).read_bytes())
        self.assertEqual(
            line, f"interactive workspace: {self.workspace} (shared {shared}, personal {iw.digest(PERSONAL)})"
        )

    def test_a_missing_workspace_names_upgrade(self) -> None:
        self.assertEqual(
            iw.status_line(iw.describe(self.data)),
            f"interactive workspace: {self.workspace} (absent; run `ummanu upgrade`)",
        )

    def test_a_hand_edited_workspace_is_named_as_drifted(self) -> None:
        self.run_step()
        (self.workspace / iw.AGENTS_FILE).write_text("edited\n", encoding="utf-8")

        self.assertIn("differs from its sources", iw.status_line(iw.describe(self.data)))

    def test_doctor_prints_the_line(self) -> None:
        self.run_step()
        (self.instance / "instance.yaml").write_text(instance_yaml(self.data), encoding="utf-8")
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["doctor", "--dry-run", "--offline", "--instance", str(self.instance)])

        self.assertEqual(code, 0, out.getvalue())
        self.assertIn(iw.status_line(iw.describe(self.data)) + "\n", out.getvalue())

    def test_status_prints_the_line(self) -> None:
        iw.personal_source(self.instance).unlink()
        self.run_step()
        (self.instance / "instance.yaml").write_text(instance_yaml(self.data), encoding="utf-8")
        snapshot = {
            "installation": {
                "name": "interactive",
                "head_registry": {"error": "none here"},
                "interactive_workspace": iw.describe(self.data),
                "sprints": {"error": None, "items": []},
            },
            "dispatcher": {"active_attempts": [], "observers": [], "last_tick": None},
            "memory": {"fact_count": 0},
            "checkpoint": {"lag_minutes": 0},
        }
        out = io.StringIO()
        with mock.patch.object(cli, "collect_status", return_value=snapshot), redirect_stdout(out):
            code = cli.main(["status", "--offline", "--instance", str(self.instance)])

        self.assertEqual(code, 0, out.getvalue())
        shared = iw.digest(iw.shared_source(ROOT).read_bytes())
        self.assertIn(
            f"interactive workspace: {self.workspace} (shared {shared}, personal absent)\n", out.getvalue()
        )


class ShellTests(Fixture):
    """`ummanu shell` defaults its cwd and Codex trust directory to `<data>/interactive`."""

    def setUp(self) -> None:
        super().setUp()
        (self.instance / "instance.yaml").write_text(instance_yaml(self.data), encoding="utf-8")
        self.env_file = self.instance / "runtime.env"
        self.env_file.write_text("", encoding="utf-8")
        patcher = mock.patch.dict(
            os.environ,
            {
                head_registry.REGISTRY_ENV: str(head_registry.HEADS_TOML),
                "UMMANU_INSTANCE": str(self.instance),
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("UMMANU_DATA_DIR", None)

    def shell(self, *argv: str) -> tuple[int, str, str]:
        args = argparse.Namespace(head=None, workspace=None, env_file=str(self.env_file), print_command=True)
        for flag, value in zip(argv[::2], argv[1::2], strict=True):
            setattr(args, flag.removeprefix("--").replace("-", "_"), value)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = session.run_shell(args)
        return code, out.getvalue(), err.getvalue()

    def test_print_shows_the_launch_in_the_interactive_workspace(self) -> None:
        self.run_step()

        code, out, err = self.shell()

        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith(f"cd {self.workspace} && "), out)

    def test_a_codex_head_trusts_the_interactive_workspace(self) -> None:
        self.run_step()
        codex = session.resolve_profile_id("codex")

        code, out, err = self.shell("--head", codex)

        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith(f"cd {self.workspace} && "), out)
        self.assertIn(f'projects."{self.workspace}".trust_level="trusted"', out)

    def test_an_explicit_workspace_wins(self) -> None:
        self.run_step()
        other = self.root / "elsewhere"
        other.mkdir()

        code, out, err = self.shell("--workspace", str(other))

        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith(f"cd {other} && "), out)
        self.assertNotIn(str(self.workspace), out)

    def test_a_missing_workspace_names_upgrade(self) -> None:
        code, out, err = self.shell()

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn(str(self.workspace), err)
        self.assertIn("ummanu upgrade", err)
        self.assertFalse(self.workspace.exists(), "the shell must not materialize the workspace")

    def test_a_missing_workspace_names_how_to_select_the_installation(self) -> None:
        """The workspace hangs off the selected installation, so the refusal says how to select one."""
        code, _out, err = self.shell()

        self.assertEqual(code, 2)
        self.assertIn("ummanu upgrade --instance LIVE_ROOT", err)
        self.assertIn("UMMANU_INSTANCE", err)
        self.assertIn("--workspace", err)

    def test_the_launch_runs_in_the_workspace(self) -> None:
        self.run_step()
        with (
            mock.patch("ummanu.session.memory_access.issue_grant", return_value=SimpleNamespace(token="t")),
            mock.patch("ummanu.session.os.chdir") as chdir,
            mock.patch("ummanu.session.os.execvpe") as execvpe,
        ):
            args = argparse.Namespace(
                head=None, workspace=None, env_file=str(self.env_file), print_command=False
            )
            self.assertEqual(session.run_shell(args), 0)

        chdir.assert_called_once_with(str(self.workspace))
        execvpe.assert_called_once()


class ShellWithoutAnExportedInstallationTests(Fixture):
    """ummanu-34 rework: a shell that names its installation only through `--env-file`, or not at
    all, resolves the data dir once and uses it for the cwd, the Codex trust entry and CODEX_HOME."""

    def setUp(self) -> None:
        super().setUp()
        self.home = self.root / "home"
        # The default live root under this home (`runtime.paths.default_instance_path`).
        self.instance = self.home / "ummanu-data" / "instance"
        self.instance.mkdir(parents=True)
        (self.instance / "instance.yaml").write_text(instance_yaml(self.data), encoding="utf-8")
        self.env_file = self.instance / "runtime.env"
        self.env_file.write_text("", encoding="utf-8")
        self.write_personal(PERSONAL)
        self.run_step()
        login = self.data / "codex-home" / "auth.json"
        login.parent.mkdir(parents=True)
        login.write_text('{"token": "fixture"}\n', encoding="utf-8")
        # Nothing in the process env names an installation, a Codex home or a runtime env file.
        named = ("UMMANU_INSTANCE", "UMMANU_DATA_DIR", "TA_CODEX_HOME", *role_env.RUNTIME_ENV_FILE_ENVS)
        environ = {key: value for key, value in os.environ.items() if key not in named}
        environ.update({"HOME": str(self.home), head_registry.REGISTRY_ENV: str(head_registry.HEADS_TOML)})
        for patcher in (
            mock.patch.dict(os.environ, environ, clear=True),
            mock.patch.object(role_env, "RUNTIME_ENV_DEFAULT", str(self.env_file)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def shell(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["shell", *argv])
        return code, out.getvalue(), err.getvalue()

    def assert_codex_launch_in_the_interactive_workspace(self, code: int, out: str, err: str) -> None:
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith(f"cd {self.workspace} && "), out)
        self.assertIn(f'projects."{self.workspace}".trust_level="trusted"', out)
        self.assertIn(f"CODEX_HOME={self.data / 'codex-home'} ", out)
        self.assertNotIn("UMMANU_DATA_DIR", os.environ, "the binding outlived the launch")

    def test_codex_with_only_an_env_file(self) -> None:
        self.assert_codex_launch_in_the_interactive_workspace(
            *self.shell("--head", "codex", "--env-file", str(self.env_file), "--print")
        )

    def test_codex_with_no_flags_uses_the_default_instance(self) -> None:
        self.assert_codex_launch_in_the_interactive_workspace(*self.shell("--head", "codex", "--print"))

    def test_the_default_head_with_no_flags_uses_the_default_instance(self) -> None:
        code, out, err = self.shell("--print")

        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith(f"cd {self.workspace} && "), out)


if __name__ == "__main__":
    unittest.main()
