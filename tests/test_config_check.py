"""`ummanu config check`: schema validation and the old-name guard over the live root, with no Git.

It replaces the instance repository's `tests/` (`test_instance_config.py` and `test_old_name_guard.py`),
whose allowlist is ported verbatim into `ummanu.infra.old_name_guard.LIVE_ROOT_ALLOWLIST`. The class
tables below are that suite's own.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from ummanu.cli import main
from ummanu.config import validate_instance
from ummanu.infra import config_check
from ummanu.infra.export_allowlist import is_exported
from ummanu.infra.fallback import fallback_errors
from ummanu.infra.old_name_guard import live_root_violations

OLD = "secret" + "ary"


class LiveRootCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.live = self.root / "instance"
        self.write(
            "instance.yaml",
            f"version: 1\nname: test\ndata_dir: {self.root / 'data'}\n"
            "offsite:\n  instance_remote: git@example.invalid:owner/instance.git\n",
        )
        self.write(
            "projects/sample.yaml",
            f"id: sample\nrepo: {self.root / 'sample'}\nenabled: false\nadapter: sample\ndefault_branch: main\n",
        )
        self.write(
            "adapters/sample.yaml",
            "setup:\n  commands: ['true']\nsmoke:\n  command: 'true'\n"
            "validation:\n  ci: local\n  command: 'true'\n"
            "artifact_policy:\n  write_project_files: false\n",
        )
        self.write("heads/heads.toml", "[role_defaults]\n")
        self.write("persona/rules.md", "Be brief.\n")
        self.write("state/knowledge/decisions/one.md", "# One\n")
        self.write("state/memory/facts/global/fact.md", "---\nsource: curator\n---\nA fact.\n")

    def write(self, relative: str, text: str) -> Path:
        path = self.live / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def check(self) -> tuple[int, list[str], str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["config", "check", "--instance", str(self.live)])
        return code, out.getvalue().splitlines(), err.getvalue()


class ConfigCheckTests(LiveRootCase):
    def test_native_check_preserves_fallback_diagnostics_without_blocking_validation(self) -> None:
        registry = (
            '[resources.openai-sub]\nprovider = "openai"\nkind = "subscription"\n'
            '[profiles.codex-terra-high]\nadapter = "codex"\nresource = "openai-sub"\n'
            'effort = "high"\nruntime = "local-pty"\nfallback = []\n'
        )
        for source in ("[broken", registry):
            with self.subTest(source=source):
                self.write("heads/heads.toml", source)
                report = validate_instance(self.live)
                self.assertTrue(report.ok, report.errors)
                expected = fallback_errors(self.live, report.instance)
                self.assertEqual(len(expected), 1)
                code, findings, _ = self.check()
                self.assertEqual(code, 1)
                self.assertEqual([line for line in findings if line.startswith("fallback:")],
                                 [f"fallback: {error}" for error in expected])
                self.assertEqual(expected[0].source, "heads/heads.toml")
                self.assertEqual(expected[0].path, "<file>" if source == "[broken" else "profiles.fallback")
                if source == "[broken":
                    self.assertTrue(expected[0].message.startswith("head registry is invalid:"))

    def test_native_check_reports_po_one_family(self) -> None:
        path = self.live / "instance.yaml"
        path.write_text(path.read_text() + "po:\n  models:\n    claude: [fable]\n    codex: []\n")
        report = validate_instance(self.live)
        self.assertTrue(report.ok, report.errors)
        expected = fallback_errors(self.live, report.instance)
        self.assertEqual([(error.source, error.path, error.message) for error in expected], [
            ("instance.yaml", "po.models", "a claude PO session has no model on codex to fall over to when its subscription is down")])
        code, findings, _ = self.check()
        self.assertEqual(code, 1)
        self.assertEqual([line for line in findings if line.startswith("fallback:")], [f"fallback: {expected[0]}"])

    def test_native_doctor_reports_fallback_errors_that_instance_validation_allows(self):
        heads = self.live / "heads"
        heads.mkdir(exist_ok=True)
        registry = (
            '[resources.openai-sub]\nprovider = "openai"\nkind = "subscription"\n'
            '[profiles.codex-terra-high]\nadapter = "codex"\nresource = "openai-sub"\n'
            'effort = "high"\nruntime = "local-pty"\nfallback = []\n'
        )
        for source in ("[broken", registry, "[role_defaults]\n"):
            with self.subTest(source=source):
                (heads / "heads.toml").write_text(source)
                path = self.live / "instance.yaml"
                document = yaml.safe_load(path.read_text())
                document["po"] = {"models": {"claude": ["fable"], "codex": []}}
                path.write_text(yaml.safe_dump(document))
                report = validate_instance(self.live)
                self.assertTrue(report.ok, report.errors)
                expected = [{"code": "head_fallback", "message": f"{error.path}: {error.message}"}
                            for error in fallback_errors(self.live, report.instance)]
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = main(["doctor", "--instance", str(self.live), "--dry-run", "--offline", "--json"])
                payload = json.loads(out.getvalue())
                self.assertEqual(code, 1)
                self.assertEqual([row for row in payload["findings"] if row["code"] == "head_fallback"], expected)

    def test_a_clean_live_root_passes(self) -> None:
        code, findings, summary = self.check()

        self.assertEqual((code, findings), (0, []))
        self.assertIn("ummanu config check: ok (7 exported file(s)", summary)
        self.assertFalse((self.live / ".git").exists())

    def test_a_schema_error_is_one_finding_naming_the_file_and_the_field(self) -> None:
        self.write("projects/sample.yaml", "id: sample\nrepo: relative/path\nenabled: maybe\n")

        code, findings, summary = self.check()

        self.assertEqual(code, 1)
        self.assertTrue(findings)
        self.assertTrue(all(line.startswith("schema: sample.yaml: ") for line in findings), findings)
        self.assertTrue(any("enabled" in line for line in findings), findings)
        self.assertIn(f"{len(findings)} finding(s)", summary)

    def test_an_infinite_sprint_budget_is_a_schema_finding(self) -> None:
        path = self.live / "instance.yaml"
        path.write_text(path.read_text() + "sprint_budget:\n  signal: .inf\n  hard: .inf\n")

        code, findings, _ = self.check()

        self.assertEqual(code, 1)
        self.assertTrue(findings)
        self.assertTrue(all(line.startswith("schema: instance.yaml: sprint_budget") for line in findings), findings)

    def test_a_memory_fact_quoting_the_old_name_is_a_record(self) -> None:
        self.write(
            "state/memory/facts/ummanu/old.md",
            f"---\nsource: {OLD}:memory-refresh\nsupersedes: {OLD}-merge-deployment-boundary\n---\n"
            f"The checkout was `/home/dev/{OLD}`; do not revive `{OLD}`.\n",
        )

        self.assertEqual(self.check()[:2], (0, []))

    def test_a_live_knowledge_runbook_mention_is_refused_and_a_dated_one_is_history(self) -> None:
        self.write("state/knowledge/runbooks/2026-07-25-drill.md", f"Run `{OLD} upgrade`.\n")
        self.assertEqual(self.check()[:2], (0, []))

        self.write("state/knowledge/runbooks/drill.md", f"Run `{OLD} upgrade`.\n")

        code, findings, _ = self.check()
        self.assertEqual(code, 1)
        self.assertEqual(len(findings), 1, findings)
        self.assertTrue(findings[0].startswith(f"state/knowledge/runbooks/drill.md:1: '{OLD}' in "))

    def test_a_non_exported_file_holding_the_old_name_is_never_read(self) -> None:
        local = {
            "runtime.env": f"{OLD.upper()}_DATA_DIR=/srv\n",
            "board-store.env": f"PGUSER={OLD}\n",
            "secrets/installation.key": OLD,
            "tests/test_old_name_guard.py": f'OLD = "{OLD}"\n',
            "heads/heads.yaml": f"generated: {OLD}\n",
            "CONTEXT.md": f"The product lives in ~/{OLD}.\n",
            f"{OLD}-notes.md": "local notes\n",
        }
        for relative, text in local.items():
            self.write(relative, text)
        opened: list[str] = []
        real_open = os.open

        def recording_open(path, *args, **kwargs):
            opened.append(os.fsdecode(path))
            return real_open(path, *args, **kwargs)

        with mock.patch.object(config_check.os, "open", side_effect=recording_open):
            code, findings, summary = self.check()

        self.assertEqual((code, findings), (0, []))
        self.assertIn("(7 exported file(s)", summary)
        for relative in local:
            self.assertNotIn(str(self.live / relative), opened)
        self.assertIn(str(self.live / "persona/rules.md"), opened)

    def test_a_work_tree_live_root_is_checked_the_same_and_its_git_is_never_read(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.live)], check=True)
        (self.live / ".git" / "description").write_text(f"{OLD} instance\n", encoding="utf-8")

        self.assertEqual(self.check()[:2], (0, []))
        self.write("persona/rules.md", f"Ask `{OLD}`.\n")
        code, findings, _ = self.check()
        self.assertEqual(code, 1)
        self.assertEqual([line.split(":")[0] for line in findings], ["persona/rules.md"])

    def test_an_exported_path_carrying_the_old_name_is_a_finding(self) -> None:
        self.write(f"persona/{OLD}.md", "plain\n")

        code, findings, _ = self.check()

        self.assertEqual((code, findings), (1, [f"persona/{OLD}.md: path carries '{OLD}'"]))

    def test_a_symlink_at_an_exported_path_is_a_finding_and_is_not_followed(self) -> None:
        self.write("runtime.env", f"{OLD.upper()}=1\n")
        (self.live / "persona" / "env.md").symlink_to(self.live / "runtime.env")

        code, findings, _ = self.check()

        self.assertEqual(code, 1)
        self.assertEqual(len(findings), 1, findings)
        self.assertTrue(findings[0].startswith("export: "), findings[0])
        self.assertIn("persona/env.md", findings[0])

    def test_a_missing_live_root_fails(self) -> None:
        self.live.rename(self.root / "gone")

        code, findings, _ = self.check()

        self.assertEqual(code, 1)
        self.assertTrue(findings)


class PortedAllowlistTests(unittest.TestCase):
    """The instance guard's class tables, against the ported allowlist (docs/RENAME.md, Guard)."""

    def test_each_class_covers_its_own_spelling_only(self) -> None:
        allowed = {
            # H
            "persona/AGENTS.md": f"The Hermes agent under `~/.hermes/skills/{OLD}`, target hermes-{OLD}-roles.\n",
            # I
            "README.md": f"# {OLD}-instance\n\nRemote `vladmesh/{OLD}-instance`, card {OLD}-instance-12.\n",
            f"projects/{OLD}-instance.yaml": f"id: {OLD}-instance\n",
            # R: card refs, history paths, memory facts
            "heads/heads.toml": f'provider = "no-such-provider-{OLD}-1566"\n',
            "state/memory/facts/ummanu/x.md": f"---\nsource: curator\n---\nSee `/home/dev/{OLD}/.venv`.\n",
            f"state/memory/facts/{OLD}-instance/z.md": f"Product code belongs to `{OLD}`.\n",
            f"state/board/cards/{OLD}-1.json": f'{{"title": "{OLD} doctor"}}\n',
            "state/knowledge/decisions/x.md": f"Run `{OLD} upgrade`.\n",
            "state/knowledge/runbooks/2026-07-25-x.md": f"Run `{OLD} upgrade`.\n",
            "state/knowledge/plans/2026-08-21-x.md": f"Run `{OLD} upgrade`.\n",
            "state/knowledge/plans/current-x.md": f"See `projects/{OLD}/reports/x.md`.\n",
            "state/runs/runs.ndjson": f'{{"cwd": "/home/dev/{OLD}"}}\n',
        }
        for path, text in allowed.items():
            with self.subTest(path=path):
                self.assertEqual(live_root_violations(path, text), [])
        refused = {
            # H spellings are the agent's own; the product's CLI is not.
            "persona/AGENTS.md": f"Память пишет `{OLD} memory`.\n",
            # The maintenance unit was a product unit, not the instance repository.
            "CONTEXT.md": f"{OLD}-instance-maintenance.timer\n",
            # A version is not a card ref.
            "tests/test_instance_config.py": f'dist = "{OLD}-0.1.0.dist-info"\n',
            # The manifest's old role table.
            "skills/manifest.toml": f"[roles.{OLD}]\n",
            # A `source:` line outside a fact is a live statement.
            "heads/README.md": f"source: {OLD}\n",
            "instance.yaml": f"unit_prefix: {OLD}-\n",
            # Undated runbooks and current plans are live instructions, not history.
            "state/knowledge/runbooks/x.md": f"Run `{OLD} upgrade`.\n",
            "state/knowledge/plans/current-x.md": f"Run `{OLD} shell`.\n",
            # The retired supervisor project (D2) left live config; its id is no longer allowed there.
            "policies/concurrency.yaml": f"  {OLD}-supervisor: 1\n",
            f"adapters/{OLD}-supervisor.yaml": "smoke:\n",
            # A whole-file history path is a directory, not a name prefix.
            "state/boards.md": f"{OLD}\n",
        }
        for path, text in refused.items():
            with self.subTest(path=path):
                self.assertEqual(len(live_root_violations(path, text)), 1, live_root_violations(path, text))

    def test_the_unported_receipt_rows_name_paths_the_check_never_reads(self) -> None:
        """The instance guard allowed gate and provision receipts at the root; they live in the data
        directory now and no cut carries them, so the check cannot meet them."""
        for path in ("gate-runs/x/result.json", "provision-runs/x/result.yaml"):
            with self.subTest(path=path):
                self.assertFalse(is_exported(path))

    def test_a_failure_names_the_file_line_and_match(self) -> None:
        found = live_root_violations("CONTEXT.md", f"# Контекст\n\nПродукт лежит в `~/{OLD.title()}`.\n")
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].startswith(f"CONTEXT.md:3: '{OLD.title()}' in "), found[0])
        self.assertEqual(
            live_root_violations(f"persona/{OLD}-helper.md", ""),
            [f"persona/{OLD}-helper.md: path carries '{OLD}'"],
        )
        self.assertEqual(len(live_root_violations("persona/AGENTS.md", f"{OLD.upper()}_DATA_DIR=1\n")), 1)

    def test_only_history_rows_admit_a_stray_old_name(self) -> None:
        for path in (
            "state/board/cards.ndjson",
            "state/knowledge/decisions/x.md",
            "state/runs/runs.ndjson",
            "state/memory/facts/global/x.md",
        ):
            with self.subTest(path=path):
                self.assertEqual(live_root_violations(path, OLD), [])
        for path in (
            "instance.yaml",
            "persona/AGENTS.md",
            "skills/manifest.toml",
            "state/knowledge/runbooks/destructive-recovery-drill.md",
            "state/knowledge/plans/current-product-work-plan.md",
        ):
            with self.subTest(path=path):
                self.assertEqual(len(live_root_violations(path, OLD)), 1)


if __name__ == "__main__":
    unittest.main()
