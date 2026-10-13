from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from tests.support.git import git, make_repo
from ummanu.cli import main
from ummanu.config import load_config, load_schema, validate, validate_instance
from ummanu.onboarding import DRAFT_STORAGE, IDENTITY_FIELDS, OnboardingStorage, ScannerError, project_add


def _schema_sample(spec: dict) -> object:
    """One schema-valid value for a binding property, from the schema alone."""
    if "enum" in spec:
        return spec["enum"][0]
    kind = spec.get("type")
    if kind == "string":
        if spec.get("pattern") == "^/":
            return "/carried-over-value"
        return "carried-over-value"
    if kind == "integer":
        return spec.get("minimum", 0) + 1
    if kind == "boolean":
        return True
    if kind == "object":
        return {name: _schema_sample(child) for name, child in spec.get("properties", {}).items()}
    if kind == "array":
        return [_schema_sample(spec["items"])] if "items" in spec else []
    raise AssertionError(f"no sample for schema {spec!r}")


class OnboardingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = make_repo(self.root)
        self.instance = self.root / "instance"
        self.instance.mkdir()
        # Onboarding keeps its drafts, runs and locks in the data directory this file names.
        (self.instance / "instance.yaml").write_text(
            "version: 1\nname: test\n"
            f"data_dir: {self.root / 'data'}\n"
            "offsite:\n  instance_remote: git@example.invalid:instance.git\n",
            encoding="utf-8",
        )
        self.storage = OnboardingStorage(self.root / "data")

    def tearDown(self) -> None:
        self.temp.cleanup()

    @property
    def binding(self) -> Path:
        return self.instance / "projects" / "sample-project.yaml"

    @property
    def draft(self) -> Path:
        return self.storage.draft("sample-project")

    def test_generated_onboarding_state_lives_in_the_data_directory(self):
        code, artifact = project_add(str(self.repo), str(self.instance), dry_run=False)

        self.assertEqual(code, 0, artifact)
        self.assertEqual(self.draft, self.root / "data" / "onboarding" / "adapter-drafts" / "sample-project.yaml")
        self.assertTrue(self.draft.is_file())
        self.assertTrue((self.root / "data" / "locks" / "onboarding" / "sample-project.lock").is_file())
        self.assertEqual(artifact["ownership"]["adapter"]["storage"], DRAFT_STORAGE)
        self.assertNotIn("secretary-instance", DRAFT_STORAGE)
        # The live root keeps the binding (configuration) and nothing onboarding generated.
        self.assertTrue(self.binding.is_file())
        self.assertFalse((self.instance / "adapter-drafts").exists())
        self.assertFalse((self.instance / ".locks").exists())

    def test_an_instance_without_a_usable_data_directory_writes_nothing(self):
        (self.instance / "instance.yaml").write_text("version: 1\n", encoding="utf-8")

        code, artifact = project_add(str(self.repo), str(self.instance), dry_run=False)

        self.assertEqual(code, 1)
        self.assertEqual(artifact["draft"]["findings"][-1]["code"], "draft.invalid")
        self.assertIn("onboarding storage is unavailable", artifact["draft"]["findings"][-1]["message"])
        self.assertFalse(self.binding.exists())
        self.assertFalse((self.root / "data").exists())

    def test_dry_run_is_deterministic_inventory_without_writes(self):
        first = project_add(str(self.repo), str(self.instance), dry_run=True)
        second = project_add(str(self.repo), str(self.instance), dry_run=True)

        self.assertEqual(first, second)
        code, artifact = first
        self.assertEqual(code, 0)
        self.assertEqual(artifact["scanner"]["facts"]["docs_files"], ["README.md"])
        self.assertEqual(artifact["scanner"]["facts"]["setup_files"], ["pyproject.toml"])
        self.assertFalse(self.binding.exists())
        self.assertFalse(self.draft.exists())
        self.assertEqual(validate(artifact, "onboarding-contract", "output"), [])

    def test_cli_dry_run_prints_valid_contract(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(
                [
                    "project",
                    "add",
                    str(self.repo),
                    "--instance",
                    str(self.instance),
                    "--dry-run",
                ]
            )

        artifact = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(validate(artifact, "onboarding-contract", "stdout"), [])
        self.assertFalse(self.binding.exists())

    def test_dirty_worktree_is_only_an_observed_fact(self):
        (self.repo / "untracked.txt").write_text("dirty\n", encoding="utf-8")

        code, artifact = project_add(str(self.repo), str(self.instance), dry_run=True)

        self.assertEqual(code, 0)
        self.assertFalse(artifact["scanner"]["repo"]["worktree_clean"])
        self.assertNotIn("setup", artifact["draft"]["adapter"])

    def test_publish_creates_disabled_binding_and_separate_valid_draft(self):
        code, artifact = project_add(str(self.repo), str(self.instance), dry_run=False)

        self.assertEqual(code, 0)
        binding = load_config(self.binding)
        stored = load_config(self.draft)
        self.assertFalse(binding["enabled"])
        self.assertEqual(stored, artifact)
        self.assertFalse((self.instance / "adapters" / "sample-project.yaml").exists())
        self.assertEqual(validate(binding, "project-binding", self.binding.name), [])
        self.assertEqual(validate(stored, "onboarding-contract", self.draft.name), [])

    def test_doctor_validates_draft_separately_from_canonical_adapters(self):
        data_dir = self.root / "data"
        (self.instance / "instance.yaml").write_text(
            "version: 1\n"
            "name: test\n"
            f"data_dir: {data_dir}\n"
            "offsite:\n"
            "  instance_remote: git@example.invalid:instance.git\n",
            encoding="utf-8",
        )
        project_add(str(self.repo), str(self.instance), dry_run=False)

        report = validate_instance(self.instance)

        self.assertTrue(report.ok, report.errors)
        self.assertEqual(report.adapters, 0)
        self.assertEqual(report.adapter_drafts, 1)

    def test_repeat_is_idempotent_and_preserves_operator_policy(self):
        project_add(str(self.repo), str(self.instance), dry_run=False)
        binding = load_config(self.binding)
        binding["plane"] = "project"
        binding["policy"] = {"code_concurrency": 3}
        self.binding.write_text(yaml.safe_dump(binding), encoding="utf-8")

        second = project_add(str(self.repo), str(self.instance), dry_run=False)
        binding_bytes = self.binding.read_bytes()
        draft_bytes = self.draft.read_bytes()
        third = project_add(str(self.repo), str(self.instance), dry_run=False)

        self.assertEqual(second, third)
        self.assertEqual(binding_bytes, self.binding.read_bytes())
        self.assertEqual(draft_bytes, self.draft.read_bytes())
        self.assertEqual(load_config(self.binding)["policy"], {"code_concurrency": 3})

    def test_new_head_updates_scanner_without_changing_policy(self):
        project_add(str(self.repo), str(self.instance), dry_run=False)
        binding = load_config(self.binding)
        binding["policy"] = {"code_concurrency": 2}
        self.binding.write_text(yaml.safe_dump(binding), encoding="utf-8")
        draft = load_config(self.draft)
        old_head = draft["scanner"]["repo"]["head"]
        (self.repo / "sample.py").write_text("VALUE = 2\n", encoding="utf-8")
        git(self.repo, "add", "sample.py")
        git(self.repo, "commit", "-m", "Change")

        code, artifact = project_add(str(self.repo), str(self.instance), dry_run=False)

        self.assertEqual(code, 0)
        self.assertNotEqual(artifact["scanner"]["repo"]["head"], old_head)
        self.assertNotIn("policy", artifact["identity"])
        self.assertEqual(load_config(self.binding)["policy"], {"code_concurrency": 2})

    def test_binding_field_in_draft_identity_is_rejected_not_migrated(self):
        """`plane` and `policy` belong to the binding. A draft that also declares them is a
        corrupt contract the schema refuses, and the binding keeps the operator's values."""
        project_add(str(self.repo), str(self.instance), dry_run=False)
        binding = load_config(self.binding)
        binding["plane"] = "project"
        binding["policy"] = {"code_concurrency": 1}
        self.binding.write_text(yaml.safe_dump(binding), encoding="utf-8")
        draft = load_config(self.draft)
        draft["identity"]["plane"] = "project"
        draft["identity"]["policy"] = {"code_concurrency": 1}
        self.draft.write_text(yaml.safe_dump(draft), encoding="utf-8")

        code, artifact = project_add(str(self.repo), str(self.instance), dry_run=False)

        self.assertEqual(code, 1)
        self.assertEqual(artifact["draft"]["findings"][-1]["code"], "draft.invalid")
        self.assertEqual(
            sorted(load_config(self.draft)["identity"]),
            ["adapter", "default_branch", "id", "plane", "policy", "repo"],
        )
        stored = load_config(self.binding)
        self.assertEqual(stored["plane"], "project")
        self.assertEqual(stored["policy"], {"code_concurrency": 1})

    def test_unexpected_identity_field_is_rejected_instead_of_migrated(self):
        project_add(str(self.repo), str(self.instance), dry_run=False)
        draft = load_config(self.draft)
        draft["identity"]["unexpected"] = "value"
        self.draft.write_text(yaml.safe_dump(draft), encoding="utf-8")

        code, artifact = project_add(str(self.repo), str(self.instance), dry_run=False)

        self.assertEqual(code, 1)
        self.assertEqual(artifact["draft"]["findings"][-1]["code"], "draft.invalid")
        self.assertIn("unexpected", load_config(self.draft)["identity"])

    def test_missing_and_scanner_failure_are_valid_and_write_nothing(self):
        code, artifact = project_add(str(self.root / "missing"), str(self.instance), dry_run=False)
        self.assertEqual(code, 1)
        self.assertEqual(artifact["scanner"]["findings"][0]["code"], "repo.missing")
        self.assertEqual(validate(artifact, "onboarding-contract", "missing"), [])
        self.assertFalse((self.instance / "projects").exists())

        not_git = self.root / "not-git"
        not_git.mkdir()
        code, artifact = project_add(str(not_git), str(self.instance), dry_run=False)
        self.assertEqual(code, 1)
        self.assertEqual(artifact["scanner"]["findings"][0]["code"], "scanner.failed")
        self.assertEqual(validate(artifact, "onboarding-contract", "failed"), [])
        self.assertFalse(self.storage.drafts.exists())
        self.assertFalse((self.instance / "adapter-drafts").exists())

    def test_repo_without_tests_or_ci_records_findings_not_policy(self):
        other = self.root / "other"
        other.mkdir()
        repo = make_repo(other, coverage=False)
        code, artifact = project_add(str(repo), str(self.instance), dry_run=True)

        self.assertEqual(code, 0)
        self.assertEqual(artifact["scanner"]["facts"]["test_files"], [])
        self.assertEqual(artifact["scanner"]["facts"]["ci_files"], [])
        self.assertEqual(
            [item["code"] for item in artifact["scanner"]["findings"]],
            ["tests.not-observed", "ci.not-observed"],
        )
        self.assertEqual(artifact["draft"]["adapter"]["status"], "unresolved")
        self.assertNotIn("validation", artifact["draft"]["adapter"])

    def test_identity_mismatch_and_enabled_binding_do_not_mutate(self):
        project_add(str(self.repo), str(self.instance), dry_run=False)
        for field, value in (("repo", "/different/repo"), ("enabled", True)):
            binding = load_config(self.binding)
            binding[field] = value
            self.binding.write_text(yaml.safe_dump(binding), encoding="utf-8")
            before = self.binding.read_bytes(), self.draft.read_bytes()

            code, artifact = project_add(str(self.repo), str(self.instance), dry_run=False)

            self.assertEqual(code, 1)
            self.assertEqual(artifact["draft"]["findings"][-1]["code"], "draft.invalid")
            self.assertEqual(validate(artifact, "onboarding-contract", "failure"), [])
            self.assertEqual(before, (self.binding.read_bytes(), self.draft.read_bytes()))
            binding[field] = str(self.repo.resolve()) if field == "repo" else False
            self.binding.write_text(yaml.safe_dump(binding), encoding="utf-8")

    def test_conflicting_draft_does_not_mutate(self):
        project_add(str(self.repo), str(self.instance), dry_run=False)
        self.draft.write_text("identity: [broken\n", encoding="utf-8")
        before = self.binding.read_bytes(), self.draft.read_bytes()

        code, artifact = project_add(str(self.repo), str(self.instance), dry_run=False)

        self.assertEqual(code, 1)
        self.assertEqual(artifact["draft"]["findings"][-1]["code"], "draft.invalid")
        self.assertEqual(validate(artifact, "onboarding-contract", "failure"), [])
        self.assertEqual(before, (self.binding.read_bytes(), self.draft.read_bytes()))

    def test_second_replace_failure_rolls_back_first_file(self):
        real_replace = os.replace
        calls = 0

        def fail_second(source, target):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError(5, "injected")
            return real_replace(source, target)

        with mock.patch("ummanu._fsutil.os.replace", side_effect=fail_second):
            code, artifact = project_add(str(self.repo), str(self.instance), dry_run=False)

        self.assertEqual(code, 1)
        self.assertEqual(artifact["draft"]["findings"][-1]["code"], "draft.invalid")
        self.assertEqual(validate(artifact, "onboarding-contract", "failure"), [])
        self.assertFalse(self.binding.exists())
        self.assertFalse(self.draft.exists())

    def test_schema_failure_writes_nothing(self):
        problem = mock.Mock()
        problem.__str__ = mock.Mock(return_value="injected schema failure")
        with mock.patch("ummanu.onboarding.validate", return_value=[problem]):
            code, artifact = project_add(str(self.repo), str(self.instance), dry_run=False)

        self.assertEqual(code, 1)
        self.assertEqual(artifact["draft"]["findings"][-1]["code"], "draft.invalid")
        self.assertEqual(validate(artifact, "onboarding-contract", "failure"), [])
        self.assertFalse(self.binding.exists())
        self.assertFalse(self.draft.exists())

    def legacy_enabled_project(self) -> Path:
        """A binding as onboarding left it before drafts existed: enabled, with a canonical
        adapter, and with no draft, provision run or gate result to justify the enable."""
        project_add(str(self.repo), str(self.instance), dry_run=False)
        binding = load_config(self.binding)
        binding["enabled"] = True
        binding["plane"] = "project"
        binding["policy"] = {"code_concurrency": 3}
        binding["remote"] = "git@example.invalid:sample.git"
        binding["orca_binding"] = "Sample_Project"
        self.binding.write_text(yaml.safe_dump(binding), encoding="utf-8")
        self.draft.unlink()
        adapter = self.instance / "adapters" / "sample-project.yaml"
        adapter.parent.mkdir(parents=True, exist_ok=True)
        adapter.write_text("version: 1\nid: sample-project\n", encoding="utf-8")
        return adapter

    def test_update_keeps_every_optional_binding_field_the_schema_allows(self):
        """The carry-over is a merge, so a field nobody named here still survives an update."""
        project_add(str(self.repo), str(self.instance), dry_run=False)
        schema = load_schema("project-binding")
        identity_and_reset = set(IDENTITY_FIELDS) | {"enabled"}
        optional = {
            name: spec for name, spec in schema["properties"].items() if name not in identity_and_reset
        }
        self.assertTrue(optional)
        binding = load_config(self.binding)
        for name, spec in optional.items():
            binding[name] = _schema_sample(spec)
        self.assertEqual(validate(binding, "project-binding", self.binding.name), [])
        self.binding.write_text(yaml.safe_dump(binding), encoding="utf-8")

        code, _ = project_add(str(self.repo), str(self.instance), dry_run=False)

        self.assertEqual(code, 0)
        updated = load_config(self.binding)
        for name in optional:
            self.assertEqual(updated[name], binding[name], name)
        self.assertFalse(updated["enabled"])
        for field in IDENTITY_FIELDS:
            self.assertEqual(updated[field], load_config(self.draft)["identity"][field])

    def test_update_keeps_a_binding_field_this_code_never_names(self):
        """The regression guard for a field added to the schema after this code was written."""
        project_add(str(self.repo), str(self.instance), dry_run=False)
        extended = load_schema("project-binding")
        extended["properties"]["future_field"] = {"type": "string", "minLength": 1}
        binding = load_config(self.binding)
        binding["future_field"] = "set-by-the-operator"
        self.binding.write_text(yaml.safe_dump(binding), encoding="utf-8")

        with mock.patch(
            "ummanu.config.load_schema",
            side_effect=lambda name: extended if name == "project-binding" else load_schema(name),
        ):
            code, _ = project_add(str(self.repo), str(self.instance), dry_run=False)

        self.assertEqual(code, 0)
        self.assertEqual(load_config(self.binding)["future_field"], "set-by-the-operator")

    def test_re_onboard_disables_legacy_binding_and_drops_its_adapter(self):
        adapter = self.legacy_enabled_project()

        code, artifact = project_add(str(self.repo), str(self.instance), dry_run=False, re_onboard=True)

        self.assertEqual(code, 0)
        binding = load_config(self.binding)
        self.assertFalse(binding["enabled"])
        self.assertFalse(adapter.exists())
        self.assertEqual(binding["plane"], "project")
        self.assertEqual(binding["policy"], {"code_concurrency": 3})
        self.assertEqual(binding["remote"], "git@example.invalid:sample.git")
        self.assertEqual(binding["orca_binding"], "Sample_Project")
        self.assertEqual(artifact["provision"]["status"], "pending")
        self.assertEqual(artifact["gate"]["status"], "pending")
        self.assertEqual(load_config(self.draft), artifact)
        self.assertEqual(validate(binding, "project-binding", self.binding.name), [])
        self.assertEqual(validate(artifact, "onboarding-contract", self.draft.name), [])

    def test_re_onboard_is_idempotent_and_leaves_a_disabled_binding_alone(self):
        self.legacy_enabled_project()
        project_add(str(self.repo), str(self.instance), dry_run=False, re_onboard=True)
        binding_bytes = self.binding.read_bytes()
        draft_bytes = self.draft.read_bytes()

        code, _ = project_add(str(self.repo), str(self.instance), dry_run=False, re_onboard=True)

        self.assertEqual(code, 0)
        self.assertEqual(binding_bytes, self.binding.read_bytes())
        self.assertEqual(draft_bytes, self.draft.read_bytes())

    def test_re_onboard_fails_closed_on_identity_mismatch(self):
        self.legacy_enabled_project()
        binding = load_config(self.binding)
        binding["repo"] = "/different/repo"
        self.binding.write_text(yaml.safe_dump(binding), encoding="utf-8")
        adapter = self.instance / "adapters" / "sample-project.yaml"
        before = self.binding.read_bytes(), adapter.read_bytes()

        code, artifact = project_add(str(self.repo), str(self.instance), dry_run=False, re_onboard=True)

        self.assertEqual(code, 1)
        self.assertEqual(artifact["draft"]["findings"][-1]["code"], "draft.invalid")
        self.assertEqual(validate(artifact, "onboarding-contract", "failure"), [])
        self.assertEqual(before, (self.binding.read_bytes(), adapter.read_bytes()))
        self.assertFalse(self.draft.exists())

    def test_re_onboard_fails_closed_on_a_schema_invalid_binding(self):
        self.legacy_enabled_project()
        binding = load_config(self.binding)
        binding["unexpected"] = "value"
        self.binding.write_text(yaml.safe_dump(binding), encoding="utf-8")
        adapter = self.instance / "adapters" / "sample-project.yaml"
        before = self.binding.read_bytes(), adapter.read_bytes()

        code, artifact = project_add(str(self.repo), str(self.instance), dry_run=False, re_onboard=True)

        self.assertEqual(code, 1)
        self.assertEqual(artifact["draft"]["findings"][-1]["code"], "draft.invalid")
        self.assertEqual(before, (self.binding.read_bytes(), adapter.read_bytes()))

    def test_re_onboard_on_a_stale_head_publishes_the_current_one(self):
        self.legacy_enabled_project()
        (self.repo / "sample.py").write_text("VALUE = 2\n", encoding="utf-8")
        git(self.repo, "add", "sample.py")
        git(self.repo, "commit", "-m", "Change")
        head = git(self.repo, "rev-parse", "HEAD")

        code, artifact = project_add(str(self.repo), str(self.instance), dry_run=False, re_onboard=True)

        self.assertEqual(code, 0)
        self.assertEqual(artifact["scanner"]["repo"]["head"], head)
        self.assertEqual(artifact["provision"]["status"], "pending")

    def test_re_onboard_scanner_failure_leaves_the_enable_in_place(self):
        adapter = self.legacy_enabled_project()
        before = self.binding.read_bytes(), adapter.read_bytes()

        with mock.patch("ummanu.onboarding.scan_repo", side_effect=ScannerError("injected")):
            code, artifact = project_add(str(self.repo), str(self.instance), dry_run=False, re_onboard=True)

        self.assertEqual(code, 1)
        self.assertEqual(artifact["scanner"]["findings"][0]["code"], "scanner.failed")
        self.assertEqual(before, (self.binding.read_bytes(), adapter.read_bytes()))
        self.assertFalse(self.draft.exists())

    def test_re_onboard_rollback_leaves_the_enabled_binding_untouched(self):
        adapter = self.legacy_enabled_project()
        before = self.binding.read_bytes(), adapter.read_bytes()
        real_replace = os.replace
        calls = 0

        def fail_second(source, target):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError(5, "injected")
            return real_replace(source, target)

        with mock.patch("ummanu._fsutil.os.replace", side_effect=fail_second):
            code, artifact = project_add(str(self.repo), str(self.instance), dry_run=False, re_onboard=True)

        self.assertEqual(code, 1)
        self.assertEqual(artifact["draft"]["findings"][-1]["code"], "draft.invalid")
        self.assertEqual(before, (self.binding.read_bytes(), adapter.read_bytes()))
        self.assertFalse(self.draft.exists())

    def test_re_onboard_interrupted_mid_transition_is_completed_by_a_retry(self):
        """A kill between the two replaces runs no rollback. Because the binding is written last,
        the interrupted state still carries the enable it started from: nothing new is trusted,
        and the retry recognises the takedown as unfinished and carries it through."""
        adapter = self.legacy_enabled_project()
        real_replace = os.replace
        calls = 0

        def crash_after_first(source, target):
            nonlocal calls
            calls += 1
            result = real_replace(source, target)
            if calls == 1:
                raise KeyboardInterrupt("host crash")
            return result

        with (
            mock.patch("ummanu._fsutil.os.replace", side_effect=crash_after_first),
            self.assertRaises(KeyboardInterrupt),
        ):
            project_add(str(self.repo), str(self.instance), dry_run=False, re_onboard=True)

        self.assertTrue(load_config(self.binding)["enabled"])
        self.assertTrue(adapter.exists())

        code, artifact = project_add(str(self.repo), str(self.instance), dry_run=False, re_onboard=True)

        self.assertEqual(code, 0)
        self.assertFalse(load_config(self.binding)["enabled"])
        self.assertFalse(adapter.exists())
        self.assertEqual(artifact["provision"]["status"], "pending")
        self.assertEqual(load_config(self.draft), artifact)

    def test_re_onboard_interrupted_before_the_adapter_is_dropped_recovers(self):
        """The other crash window: both files published, the adapter not yet deleted. The binding
        is disabled by then, so an ordinary project add finishes the cleanup."""
        adapter = self.legacy_enabled_project()
        real_unlink = Path.unlink

        def crash_on_adapter(self_path, *args, **kwargs):
            if self_path == adapter:
                raise KeyboardInterrupt("host crash")
            return real_unlink(self_path, *args, **kwargs)

        with (
            mock.patch.object(Path, "unlink", crash_on_adapter),
            self.assertRaises(KeyboardInterrupt),
        ):
            project_add(str(self.repo), str(self.instance), dry_run=False, re_onboard=True)

        self.assertFalse(load_config(self.binding)["enabled"])
        self.assertTrue(adapter.exists())

        code, artifact = project_add(str(self.repo), str(self.instance), dry_run=False)

        self.assertEqual(code, 0)
        self.assertFalse(adapter.exists())
        self.assertEqual(artifact["provision"]["status"], "pending")

    def test_cli_re_onboard_publishes_a_disabled_binding(self):
        adapter = self.legacy_enabled_project()
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            code = main(
                [
                    "project",
                    "add",
                    str(self.repo),
                    "--instance",
                    str(self.instance),
                    "--re-onboard",
                ]
            )

        artifact = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(validate(artifact, "onboarding-contract", "stdout"), [])
        self.assertFalse(load_config(self.binding)["enabled"])
        self.assertFalse(adapter.exists())

    def test_add_without_the_flag_still_refuses_an_enabled_binding(self):
        adapter = self.legacy_enabled_project()
        before = self.binding.read_bytes(), adapter.read_bytes()

        code, artifact = project_add(str(self.repo), str(self.instance), dry_run=False)

        self.assertEqual(code, 1)
        self.assertEqual(artifact["draft"]["findings"][-1]["message"], "existing binding is enabled")
        self.assertEqual(before, (self.binding.read_bytes(), adapter.read_bytes()))

    def inventory_binding(self) -> Path:
        """A Phase-2 inventory binding: registered and disabled on the shared inventory-only
        adapter, with no draft of its own. Returns the shared adapter's file."""
        project_add(str(self.repo), str(self.instance), dry_run=False)
        binding = load_config(self.binding)
        binding["adapter"] = "inventory-only"
        binding["plane"] = "project"
        self.binding.write_text(yaml.safe_dump(binding), encoding="utf-8")
        self.draft.unlink()
        shared = self.instance / "adapters" / "inventory-only.yaml"
        shared.parent.mkdir(parents=True, exist_ok=True)
        shared.write_text("version: 1\nid: inventory-only\n", encoding="utf-8")
        return shared

    def test_add_moves_a_disabled_binding_onto_the_project_adapter(self):
        shared = self.inventory_binding()
        shared_bytes = shared.read_bytes()

        code, artifact = project_add(str(self.repo), str(self.instance), dry_run=False)

        self.assertEqual(code, 0, artifact)
        binding = load_config(self.binding)
        self.assertEqual(binding["adapter"], "sample-project")
        self.assertFalse(binding["enabled"])
        self.assertEqual(binding["plane"], "project")
        self.assertEqual(artifact["identity"]["adapter"], "sample-project")
        self.assertEqual(artifact["provision"]["status"], "pending")
        self.assertEqual(load_config(self.draft), artifact)
        self.assertEqual(validate(binding, "project-binding", self.binding.name), [])
        self.assertEqual(validate(artifact, "onboarding-contract", self.draft.name), [])
        self.assertEqual(shared.read_bytes(), shared_bytes)

        before = self.binding.read_bytes(), self.draft.read_bytes()
        code, _ = project_add(str(self.repo), str(self.instance), dry_run=False)
        self.assertEqual(code, 0)
        self.assertEqual(before, (self.binding.read_bytes(), self.draft.read_bytes()))

    def test_an_enabled_binding_on_another_adapter_still_refuses(self):
        self.inventory_binding()
        binding = load_config(self.binding)
        binding["enabled"] = True
        self.binding.write_text(yaml.safe_dump(binding), encoding="utf-8")
        before = self.binding.read_bytes()

        for re_onboard, message in (
            (False, "existing binding is enabled"),
            (True, "existing binding has conflicting adapter"),
        ):
            code, artifact = project_add(
                str(self.repo), str(self.instance), dry_run=False, re_onboard=re_onboard
            )

            self.assertEqual(code, 1)
            self.assertEqual(artifact["draft"]["findings"][-1]["message"], message)
            self.assertEqual(before, self.binding.read_bytes())
            self.assertFalse(self.draft.exists())

    def test_a_draft_of_another_identity_still_refuses_the_move(self):
        """Only the draft of the binding's previous adapter is taken over; a draft naming some
        other identity is still refused, and nothing moves."""
        project_add(str(self.repo), str(self.instance), dry_run=False)
        binding = load_config(self.binding)
        binding["adapter"] = "inventory-only"
        self.binding.write_text(yaml.safe_dump(binding), encoding="utf-8")
        draft = load_config(self.draft)
        draft["identity"]["adapter"] = "someone-else"
        self.draft.write_text(yaml.safe_dump(draft), encoding="utf-8")
        before = self.binding.read_bytes(), self.draft.read_bytes()

        code, artifact = project_add(str(self.repo), str(self.instance), dry_run=False)

        self.assertEqual(code, 1)
        self.assertEqual(
            artifact["draft"]["findings"][-1]["message"], "existing draft identity does not match binding"
        )
        self.assertEqual(before, (self.binding.read_bytes(), self.draft.read_bytes()))

    def test_a_move_interrupted_before_the_binding_is_completed_by_a_retry(self):
        """The draft is published first: a kill before the binding leaves the binding on the
        previous adapter and the draft already on the new one, which the retry accepts."""
        self.inventory_binding()
        real_replace = os.replace
        calls = 0

        def crash_after_first(source, target):
            nonlocal calls
            calls += 1
            result = real_replace(source, target)
            if calls == 1:
                raise KeyboardInterrupt("host crash")
            return result

        with (
            mock.patch("ummanu._fsutil.os.replace", side_effect=crash_after_first),
            self.assertRaises(KeyboardInterrupt),
        ):
            project_add(str(self.repo), str(self.instance), dry_run=False)

        self.assertEqual(load_config(self.binding)["adapter"], "inventory-only")
        self.assertEqual(load_config(self.draft)["identity"]["adapter"], "sample-project")

        code, artifact = project_add(str(self.repo), str(self.instance), dry_run=False)

        self.assertEqual(code, 0, artifact)
        self.assertEqual(load_config(self.binding)["adapter"], "sample-project")
        self.assertFalse(load_config(self.binding)["enabled"])
        self.assertEqual(load_config(self.draft), artifact)

    def test_scanner_cannot_add_a_target(self):
        _, artifact = project_add(str(self.repo), str(self.instance), dry_run=True)
        artifact["scanner"]["repo"]["path"] = "/different/repo"
        self.assertTrue(validate(artifact, "onboarding-contract", "foreign-target"))


if __name__ == "__main__":
    unittest.main()


class ProjectWithoutOrcaTests(unittest.TestCase):
    """secretary-1704: a fresh project is added and reconciled with no Orca call at all."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = make_repo(self.root)
        self.instance = self.root / "instance"
        self.instance.mkdir()
        (self.root / "data").mkdir()
        (self.instance / "instance.yaml").write_text(
            "version: 1\nname: fresh\ndata_dir: "
            + str(self.root / "data")
            + "\noffsite:\n  instance_remote: git@example.invalid:x/y\nhost:\n  unit_prefix: ummanu-\n",
            encoding="utf-8",
        )
        self.fixture = self.root / "host"
        self.fixture.mkdir()
        # The host runs this checkout, so reconcile has this checkout's units to install.
        product = Path(__file__).resolve().parents[1]
        env = mock.patch.dict(os.environ, {"UMMANU_REPO": str(product)})
        env.start()
        self.addCleanup(env.stop)

    def run_cli(self, argv: list[str]) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(argv)
        return code, output.getvalue()

    def test_project_add_then_reconcile_apply_binds_the_project_and_installs_its_units(self):
        from tests.fakes.upgrade import FakeUnitInstaller

        units = FakeUnitInstaller()
        with (
            mock.patch("ummanu.host_apply.SystemdUnitInstaller", return_value=units),
            mock.patch(
                "ummanu.host_commands.resolve_runtime_owner", return_value=(None, self.root / "home")
            ),
        ):
            add_code, add_output = self.run_cli(
                ["project", "add", str(self.repo), "--instance", str(self.instance)]
            )
            apply_code, apply_output = self.run_cli(
                ["reconcile", "apply", "--instance", str(self.instance), "--host-fixture", str(self.fixture)]
            )

        self.assertEqual(add_code, 0, add_output)
        self.assertEqual(apply_code, 0, apply_output)
        binding = load_config(self.instance / "projects" / "sample-project.yaml")
        self.assertNotIn("orca_binding", binding)
        self.assertEqual(validate(binding, "project-binding", "sample-project.yaml"), [])
        self.assertTrue(units.calls)
        manifest = json.loads((self.root / "data" / "host-managed.json").read_text(encoding="utf-8"))
        self.assertEqual({row["kind"] for row in manifest["resources"]}, {"unit"})
        self.assertNotIn("orca", apply_output)
