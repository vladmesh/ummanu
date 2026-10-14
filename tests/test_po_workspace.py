"""The PO workspace in the data directory, the skills delivered into it, and retired skill copies."""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ummanu import cli, upgrade
from ummanu.installation.upgrade import types as upgrade_types, workspaces as upgrade_workspaces
from ummanu.memory import client_config
from ummanu.po import workspace
from ummanu.role_skills import (
    BIN_DIR_ENV,
    MANIFEST,
    OWNERSHIP_MARKER,
    RegistryError,
    audit,
    load_registry,
    skill_delivery,
    sync,
)

ROOT = MANIFEST.parent.parent
PO_SKILLS = {"open-sprint", "open-issue", "grilling", "knowledge-doc"}
PO_TARGETS = {"claude-po-workspace": ".claude/skills", "codex-po-workspace": ".agents/skills"}


class InstructionsTests(unittest.TestCase):
    """The PO instructions `upgrade` ships carry the delegation rule (secretary-1792)."""

    def setUp(self) -> None:
        self.agents = workspace.agents_source(ROOT).read_text(encoding="utf-8")
        self.flat = " ".join(self.agents.split())

    def test_it_quotes_the_wait_card_form_of_the_protocols_exactly(self) -> None:
        """Every wait-card command the PO is shown parses under the real `task create` parser.

        Each flag of the documented form is handed to the parser with the first alternative of its
        value, a placeholder kept as its own text, so a flag the CLI does not have fails here.
        """
        forms = [
            line.strip()
            for line in self.agents.splitlines()
            if "ummanu task create" in line and "--type wait" in line
        ]
        self.assertTrue(forms)
        parser = cli.build_parser()
        for form in forms:
            with self.subTest(form=form):
                command = re.sub(r"<([^>]*)>", lambda m: "<" + m.group(1).replace(" ", "-") + ">", form)
                words = command.split(" -m ummanu ", 1)[1].split()[:2]
                argv = list(words)
                for flag, value in re.findall(r"(--[a-z][a-z-]*)(?:\s+(?!-)([^\s\[\]|]+))?", command):
                    argv += [flag, value] if value else [flag]
                self.assertEqual(argv[:2], ["task", "create"])
                stderr = io.StringIO()
                try:
                    with contextlib.redirect_stderr(stderr):
                        args = parser.parse_args(argv)
                except SystemExit:
                    self.fail(f"{argv} is refused by the CLI: {stderr.getvalue()}")
                self.assertEqual((args.role, args.type), ("po", "wait"))
                self.assertTrue(args.wait_deadline and args.wait_return)


class PoWorkspaceStepTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.product = self.root / "product"
        self.bridge = client_config.bridge_executable(self.product)
        self.bridge.parent.mkdir(parents=True)
        self.bridge.write_text("#!/bin/sh\n", encoding="utf-8")
        source = workspace.agents_source(self.product)
        source.parent.mkdir(parents=True)
        source.write_bytes(workspace.agents_source(ROOT).read_bytes())
        self.data = self.root / "data"
        self.po = self.data / workspace.WORKSPACE_NAME

    def run_step(self, *, dry_run: bool = False) -> upgrade_types.StepResult:
        context = upgrade_types.UpgradeContext(
            instance_path=self.root / "instance",
            product_root=self.product,
            base_branch="main",
            dry_run=dry_run,
            units=None,
            report=SimpleNamespace(data_dir=self.data),
        )
        return upgrade_workspaces.step_po_workspace(context)

    def snapshot(self) -> dict[str, tuple[bytes, int]]:
        return {
            str(path.relative_to(self.po)): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in sorted(self.po.rglob("*"))
            if path.is_file()
        }

    def test_the_step_runs_after_the_user_bridge_entries_and_before_the_skills(self) -> None:
        names = [step.__name__ for step in upgrade.STEPS]

        self.assertLess(names.index("step_memory_clients"), names.index("step_po_workspace"))
        self.assertLess(names.index("step_po_workspace"), names.index("step_role_skills"))

    def test_the_workspace_is_handed_over_after_the_skills_are_delivered(self) -> None:
        names = [step.__name__ for step in upgrade.STEPS]

        self.assertLess(names.index("step_role_skills"), names.index("step_po_workspace_owner"))

    def test_a_root_invoker_hands_the_whole_workspace_to_the_runtime_user_on_every_run(self) -> None:
        """Skill roots role-skills creates as root are handed over, and repaired on a later run."""
        context = upgrade_types.UpgradeContext(
            instance_path=self.root / "instance",
            product_root=self.product,
            base_branch="main",
            dry_run=False,
            units=None,
            report=SimpleNamespace(data_dir=self.data),
            runtime_user="po-runtime",
        )
        upgrade_workspaces.step_po_workspace(context)
        (self.root / "instance").mkdir()
        with mock.patch.dict(os.environ, {BIN_DIR_ENV: str(self.root / "bin")}):
            sync(
                instance_path=self.root / "instance",
                product_manifest=MANIFEST,
                home=self.root / "home",
                data_dir=self.data,
                target_filter=set(PO_TARGETS),
            )
        outside = self.root / "outside"
        outside.write_text("not the PO's\n", encoding="utf-8")
        (self.po / "link").symlink_to(outside)
        account = SimpleNamespace(pw_uid=4321, pw_gid=4321)

        runs: list[tuple[upgrade_types.StepResult, set[Path]]] = []
        for _ in range(2):
            with (
                mock.patch('ummanu.installation.upgrade.workspaces.os.geteuid', return_value=0),
                mock.patch('ummanu.installation.upgrade.workspaces.pwd.getpwnam', return_value=account),
                mock.patch('ummanu.installation.upgrade.workspaces.os.chown') as chown,
            ):
                result = upgrade_workspaces.step_po_workspace_owner(context)
            runs.append((result, {Path(call.args[0]) for call in chown.call_args_list}))

        for result, owned in runs:
            with self.subTest(result=result.detail):
                self.assertFalse(result.failed, result.detail)
                for path in (
                    self.po,
                    self.po / "NOTES.md",
                    self.po / ".codex" / "config.toml",
                    self.po / ".claude" / "skills",
                    self.po / ".agents" / "skills",
                    self.po / ".agents" / "skills" / "open-issue" / "SKILL.md",
                ):
                    self.assertIn(path, owned)
                self.assertNotIn(self.po / "link", owned)
                self.assertNotIn(outside, owned)

    def test_a_non_root_invoker_skips_the_handover(self) -> None:
        upgrade_workspaces.step_po_workspace(
            upgrade_types.UpgradeContext(
                instance_path=self.root / "instance",
                product_root=self.product,
                base_branch="main",
                dry_run=False,
                units=None,
                report=SimpleNamespace(data_dir=self.data),
            )
        )
        context = upgrade_types.UpgradeContext(
            instance_path=self.root / "instance",
            product_root=self.product,
            base_branch="main",
            dry_run=False,
            units=None,
            report=SimpleNamespace(data_dir=self.data),
            runtime_user="po-runtime",
        )

        with (
            mock.patch('ummanu.installation.upgrade.workspaces.os.geteuid', return_value=1000),
            mock.patch('ummanu.installation.upgrade.workspaces.os.chown') as chown,
        ):
            result = upgrade_workspaces.step_po_workspace_owner(context)

        self.assertEqual(result.status, "skipped")
        chown.assert_not_called()

    def test_a_first_run_materializes_the_workspace(self) -> None:
        result = self.run_step()

        self.assertEqual(result.status, "changed", result.detail)
        self.assertEqual(
            (self.po / "AGENTS.md").read_bytes(),
            (ROOT / "packaging" / "po-workspace" / "AGENTS.md").read_bytes(),
        )
        claude = (self.po / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertEqual(len(claude.splitlines()), 1)
        self.assertIn("AGENTS.md", claude)
        self.assertTrue((self.po / "NOTES.md").is_file())
        mcp = json.loads((self.po / ".mcp.json").read_text(encoding="utf-8"))
        self.assertEqual(mcp["mcpServers"]["po_memory"], client_config._claude_bridge(self.bridge, self.data))
        codex = tomllib.loads((self.po / ".codex" / "config.toml").read_text(encoding="utf-8"))
        entry = codex["mcp_servers"]["po_memory"]
        self.assertEqual(entry["command"], str(self.bridge))
        self.assertEqual(entry["env"]["MEMORY_ACCESS_BINDINGS"], str(self.data / "memory" / "access-grants"))

    def test_a_second_run_changes_nothing(self) -> None:
        self.run_step()
        before = self.snapshot()

        result = self.run_step()

        self.assertEqual(result.status, "unchanged", result.detail)
        self.assertEqual(self.snapshot(), before)

    def test_upgrade_keeps_the_notes_and_restores_the_instructions(self) -> None:
        self.run_step()
        notes = self.po / "NOTES.md"
        notes.write_text("# mine\nthe owner prefers P2 by default\n", encoding="utf-8")
        (self.po / "AGENTS.md").write_text("edited by hand\n", encoding="utf-8")
        (self.po / "CLAUDE.md").write_text("something else\nand more\n", encoding="utf-8")

        result = self.run_step()

        self.assertEqual(result.status, "changed")
        self.assertIn("AGENTS.md", result.detail)
        self.assertNotIn("NOTES.md", result.detail)
        self.assertEqual(notes.read_text(encoding="utf-8"), "# mine\nthe owner prefers P2 by default\n")
        self.assertEqual((self.po / "AGENTS.md").read_bytes(), workspace.agents_source(ROOT).read_bytes())
        self.assertEqual((self.po / "CLAUDE.md").read_text(encoding="utf-8"), workspace.CLAUDE_POINTER)

    def test_unrelated_mcp_entries_in_the_workspace_survive(self) -> None:
        self.run_step()
        path = self.po / ".mcp.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["mcpServers"]["other"] = {"type": "stdio", "command": "other"}
        path.write_text(json.dumps(payload), encoding="utf-8")

        self.run_step()

        self.assertIn("other", json.loads(path.read_text(encoding="utf-8"))["mcpServers"])

    def test_a_dry_run_writes_nothing(self) -> None:
        result = self.run_step(dry_run=True)

        self.assertEqual(result.status, "changed")
        self.assertFalse(self.data.exists())

    def test_a_checkout_without_a_virtualenv_is_skipped(self) -> None:
        (self.bridge.parent).rename(self.root / "moved")
        (self.product / ".venv").rmdir()

        self.assertEqual(self.run_step().status, "skipped")
        self.assertFalse(self.data.exists())


class PoSkillDeliveryTests(unittest.TestCase):
    """The shipped manifest, delivered into a scratch home and data directory."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.instance = self.root / "instance"
        self.instance.mkdir()
        self.home = self.root / "home"
        self.data = self.root / "data"
        env = mock.patch.dict(os.environ, {BIN_DIR_ENV: str(self.root / "bin")})
        env.start()
        self.addCleanup(env.stop)

    def test_both_workspace_roots_get_exactly_the_four_po_skills(self) -> None:
        result = sync(
            instance_path=self.instance, product_manifest=MANIFEST, home=self.home, data_dir=self.data
        )

        self.assertTrue(result["after"]["ok"], result["after"])
        for target, relative in PO_TARGETS.items():
            with self.subTest(target):
                root = self.data / workspace.WORKSPACE_NAME / relative
                self.assertEqual({path.name for path in root.iterdir()}, PO_SKILLS)
                for skill in PO_SKILLS:
                    role = "po" if skill == "open-issue" else "ummanu"
                    self.assertEqual(
                        (root / skill / "SKILL.md").read_bytes(),
                        (ROOT / "skills" / "roles" / role / skill / "SKILL.md").read_bytes(),
                    )

    def test_the_po_role_declares_the_four_skills_with_one_source_each(self) -> None:
        registry = load_registry(self.instance, product_manifest=MANIFEST)

        self.assertEqual({skill for skill, _ in registry.roles["po"]}, PO_SKILLS)
        for skill in PO_SKILLS:
            role = registry.source_role("po", skill)
            self.assertEqual(role, "po" if skill == "open-issue" else "ummanu")

    def test_without_an_installation_the_workspace_targets_are_named_and_skipped(self) -> None:
        result = audit(instance_path=self.instance, product_manifest=MANIFEST, home=self.home)

        self.assertEqual(set(result["unresolved_targets"]), set(PO_TARGETS))
        self.assertNotIn("claude-po-workspace", result["targets"])

    def test_a_workspace_root_cannot_climb_out_of_the_workspace(self) -> None:
        manifest = self.root / "product" / "skills" / "manifest.toml"
        skill = manifest.parent / "roles" / "po" / "x" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("# x\n", encoding="utf-8")
        manifest.write_text(
            '[roles.po]\nskills = ["x"]\n\n[targets.t]\nshell = "claude"\nroot = "@po/../../escape"\nroles = ["po"]\n',
            encoding="utf-8",
        )

        with self.assertRaises(RegistryError):
            audit(instance_path=self.instance, product_manifest=manifest, home=self.home, data_dir=self.data)

    def test_only_a_question_about_a_workspace_role_reads_the_instance_file(self) -> None:
        """An observer launch gate must not fail on an instance file the observer's targets never use."""
        (self.instance / "instance.yaml").write_text("{}\n", encoding="utf-8")
        env = {"UMMANU_ROLE_SKILLS_MANIFEST": str(MANIFEST), "HOME": str(self.home)}

        with mock.patch.dict(os.environ, env):
            observer = skill_delivery("observer", "observe-sprint", "claude", instance_path=self.instance)
            po = skill_delivery("po", "open-issue", "claude", instance_path=self.instance)

        self.assertTrue(observer["paths"], observer)
        self.assertNotIn("data directory", observer["reason"])
        self.assertIn("data directory cannot be resolved", po["reason"])


class RetiredSkillTests(unittest.TestCase):
    """A skill leaves the manifest; sync removes only copies it can prove it delivered."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.instance = self.root / "instance"
        self.instance.mkdir()
        self.skills = self.root / "product" / "skills"
        self.manifest = self.skills / "manifest.toml"
        self.shell = self.root / "shell"
        env = mock.patch.dict(os.environ, {BIN_DIR_ENV: str(self.root / "bin")})
        env.start()
        self.addCleanup(env.stop)

    def write_manifest(self, *skills: str) -> None:
        self.manifest.parent.mkdir(parents=True, exist_ok=True)
        listed = ", ".join(json.dumps(skill) for skill in skills)
        self.manifest.write_text(
            f"[roles.ummanu]\nskills = [{listed}]\n\n"
            f'[targets.t]\nshell = "claude"\nroot = "{self.shell}"\nroles = ["ummanu"]\n',
            encoding="utf-8",
        )

    def write_skill(self, name: str, text: str) -> Path:
        path = self.skills / "roles" / "ummanu" / name / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def run_sync(self) -> dict:
        return sync(instance_path=self.instance, product_manifest=self.manifest)

    def git(self, *args: str) -> None:
        subprocess.run(
            [
                "git",
                "-C",
                str(self.skills.parent),
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@example.invalid",
                *args,
            ],
            check=True,
            capture_output=True,
        )

    def test_a_copy_sync_delivered_is_removed_once_the_manifest_drops_it(self) -> None:
        self.write_skill("kept", "# kept\n")
        old = self.write_skill("spec-card", "# old\n")
        self.write_manifest("kept", "spec-card")
        self.run_sync()
        self.assertTrue((self.shell / "spec-card" / OWNERSHIP_MARKER).is_file())
        foreign = self.shell / "operators-own" / "SKILL.md"
        foreign.parent.mkdir()
        foreign.write_text("# mine\n", encoding="utf-8")

        old.unlink()
        old.parent.rmdir()
        self.write_manifest("kept")
        before = audit(instance_path=self.instance, product_manifest=self.manifest)
        result = self.run_sync()

        self.assertFalse(before["ok"])
        self.assertEqual([item["skill"] for item in before["retired"]], ["spec-card"])
        self.assertEqual([item["skill"] for item in result["removed"]], ["spec-card"])
        self.assertFalse((self.shell / "spec-card").exists())
        self.assertTrue((self.shell / "kept" / "SKILL.md").is_file())
        self.assertTrue(foreign.is_file())
        self.assertTrue(result["after"]["ok"], result["after"])

    def test_a_copy_from_before_the_marker_is_removed_when_its_repository_shipped_it(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.skills.parent)], check=True, capture_output=True)
        self.write_skill("kept", "# kept\n")
        shipped = self.write_skill("spec-card", "# spec card as shipped\n")
        self.write_manifest("kept", "spec-card")
        self.git("add", "-A")
        self.git("commit", "-qm", "ship")
        text = shipped.read_text(encoding="utf-8")
        shipped.unlink()
        shipped.parent.rmdir()
        self.write_manifest("kept")
        self.git("add", "-A")
        self.git("commit", "-qm", "retire")
        legacy = self.shell / "spec-card" / "SKILL.md"
        legacy.parent.mkdir(parents=True)
        legacy.write_text(text, encoding="utf-8")
        same_name = self.shell / "edited" / "SKILL.md"
        same_name.parent.mkdir()
        same_name.write_text("# not what was shipped\n", encoding="utf-8")

        result = self.run_sync()

        self.assertEqual([item["skill"] for item in result["removed"]], ["spec-card"])
        self.assertFalse(legacy.parent.exists())
        self.assertTrue(same_name.is_file())
        self.assertTrue(result["after"]["ok"], result["after"])

    def test_an_unmarked_copy_that_was_never_shipped_is_left_alone(self) -> None:
        self.write_skill("kept", "# kept\n")
        self.write_manifest("kept")
        stranger = self.shell / "spec-card" / "SKILL.md"
        stranger.parent.mkdir(parents=True)
        stranger.write_text("# somebody else's spec-card\n", encoding="utf-8")

        result = self.run_sync()

        self.assertEqual(result["removed"], [])
        self.assertTrue(stranger.is_file())

    def test_a_reference_to_a_skill_its_role_does_not_declare_is_refused(self) -> None:
        self.write_skill("kept", "# kept\n")
        self.manifest.parent.mkdir(parents=True, exist_ok=True)
        self.manifest.write_text(
            '[roles.ummanu]\nskills = ["kept"]\n\n[roles.po]\nskills = ["ummanu/missing"]\n',
            encoding="utf-8",
        )

        with self.assertRaisesRegex(RegistryError, "ummanu/missing"):
            load_registry(self.instance, product_manifest=self.manifest)


if __name__ == "__main__":
    unittest.main()
