"""Shared effective limits and generated draft reads across the config boundary."""

from __future__ import annotations

import json
import tempfile
import unittest
from collections import UserDict
from pathlib import Path

from ummanu import config, onboarding, sprints
from ummanu.board import sprint_read
from ummanu.kernel import sprint_policy
from ummanu.kernel.errors import TaskError
from ummanu.onboarding import OnboardingStorage as LegacyStorage
from ummanu.runtime.onboarding_storage import ADAPTER_STORAGE, DRAFT_STORAGE, OnboardingStorage


class ConfigBoundaryTests(unittest.TestCase):
    def instance(self, root: Path, **settings) -> Path:
        path = root / "instance.yaml"
        document = {
            "version": 1,
            "name": "test",
            "data_dir": "data",
            "offsite": {"instance_remote": "git@example.invalid:x/y.git"},
            **settings,
        }
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def test_budget_consumers_share_resolution_and_error_boundary(self) -> None:
        self.assertIs(sprint_read.budget_thresholds, sprint_policy.budget_thresholds)
        self.assertIs(config.budget_thresholds, sprint_policy.budget_thresholds)
        fixtures = (
            ({}, {"signal": 3, "hard": 6}),
            ({"sprint_budget": None}, {"signal": 3, "hard": 6}),
            ({"sprint_budget": "bad"}, {"signal": 3, "hard": 6}),
            ({"sprint_budget": {"signal": True, "hard": "4"}}, {"signal": 1, "hard": 4}),
            ({"sprint_budget": {"signal": 2.9, "hard": 4.8}}, {"signal": 2, "hard": 4}),
            ({"sprint_budget": {"signal": False, "hard": None}}, {"signal": 3, "hard": 6}),
            ({"sprint_budget": {"signal": "bad", "hard": -2}}, {"signal": 3, "hard": 6}),
        )
        for settings, expected in fixtures:
            with self.subTest(settings=settings), tempfile.TemporaryDirectory() as temporary:
                self.assertEqual(sprints.budget_thresholds(settings), expected)
                self.assertEqual(sprint_read.budget_thresholds(UserDict(settings)), expected)
                self.assertEqual(
                    sprint_read.SprintBudget.from_legacy(
                        thresholds=config.budget_thresholds(settings)
                    ).thresholds,
                    expected,
                )
                path = self.instance(Path(temporary), **settings)
                report = config.validate_instance(path)
                self.assertEqual(report.errors, config.validate(report.instance, "instance", path.name))
        self.assertEqual(
            sprint_policy.budget_thresholds(UserDict(sprint_budget=UserDict(signal="2", hard="4"))),
            {"signal": 2, "hard": 4},
        )
        for budget in ({"hard": 2}, {"signal": 10}, {"signal": 4, "hard": 3}):
            with self.subTest(budget=budget), tempfile.TemporaryDirectory() as temporary:
                settings = {"sprint_budget": budget}
                with self.assertRaisesRegex(
                    ValueError, "sprint budget hard threshold must not be below signal threshold"
                ):
                    sprint_read.budget_thresholds(settings)
                with self.assertRaises(TaskError) as caught:
                    sprints.budget_thresholds(settings)
                self.assertEqual(
                    (caught.exception.code, caught.exception.message, caught.exception.exit_code),
                    ("validation", "sprint budget hard threshold must not be below signal threshold", 2),
                )
                path = self.instance(Path(temporary), **settings)
                report = config.validate_instance(path)
                self.assertEqual(
                    report.errors,
                    config.validate(report.instance, "instance", path.name)
                    + [
                        config.SchemaError(
                            path.name, "sprint_budget", "hard threshold must not be below signal threshold"
                        )
                    ],
                )

    def test_capacity_consumers_share_fail_closed_settings(self) -> None:
        self.assertIs(sprints.open_sprint_limit, sprint_policy.open_sprint_limit)
        self.assertIs(sprints.open_sprint_limit_invalid, config.open_sprint_limit_invalid)
        for value in (1, 2, 3, 0, 4, -1, True, False, "2", "", 1.5, None):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as temporary:
                path = self.instance(Path(temporary), open_sprint_limit=value)
                expected = value if type(value) is int and value in (1, 2, 3) else 1
                invalid = not (type(value) is int and value in (1, 2, 3))
                self.assertEqual(sprints.instance_open_sprint_limit(path), expected)
                report = config.validate_instance(path)
                self.assertEqual(config.open_sprint_limit_invalid(report.instance), invalid)
                self.assertEqual(any("one open sprint" in error.message for error in report.errors), invalid)
        for settings in (None, {}, UserDict(open_sprint_limit=2)):
            self.assertEqual(sprints.open_sprint_limit(settings), 1)
            self.assertFalse(sprints.open_sprint_limit_invalid(settings))

    def test_storage_alias_paths_and_canonical_draft_validation_without_writes(self) -> None:
        self.assertIs(LegacyStorage, OnboardingStorage)
        self.assertIs(onboarding.DRAFT_STORAGE, DRAFT_STORAGE)
        self.assertIs(onboarding.ADAPTER_STORAGE, ADAPTER_STORAGE)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = self.instance(root)
            storage = OnboardingStorage.for_instance(root)
            self.assertEqual(storage, OnboardingStorage.for_instance(path))
            self.assertEqual(storage.data_dir, config.generated_state_dir(path))
            self.assertEqual(storage.root, root / "data/onboarding")
            self.assertEqual(storage.draft("p"), root / "data/onboarding/adapter-drafts/p.yaml")
            self.assertEqual(storage.provision_runs("p"), storage.root / "provision-runs/p")
            self.assertEqual(storage.gate_runs("p"), storage.root / "gate-runs/p")
            self.assertEqual(storage.compatibility_manifests, storage.root / "compatibility-manifests")
            self.assertEqual(storage.lock("p"), root / "data/locks/onboarding/p.lock")
            before = set(root.rglob("*"))
            report = config.validate_instance(path)
            self.assertEqual((report.projects, report.adapters, report.adapter_drafts), (0, 0, 0))
            self.assertEqual(set(root.rglob("*")), before)
            storage.drafts.mkdir(parents=True)
            storage.draft("p").write_text("[broken", encoding="utf-8")
            (storage.drafts / "q.yaml").write_text("{}", encoding="utf-8")
            before = set(root.rglob("*"))
            report = config.validate_instance(path)
            self.assertEqual(report.adapter_drafts, 2)
            self.assertEqual(report.errors[0].source, "p.yaml")
            self.assertEqual(report.errors[0].path, "<file>")
            self.assertTrue(any(error.source == "q.yaml" for error in report.errors))
            self.assertEqual(set(root.rglob("*")), before)

    def test_storage_remains_reachable_with_unrelated_invalid_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for settings in ({"data_dir": None}, {"host": {"units": ["bad"]}}):
                with self.subTest(settings=settings):
                    path = self.instance(root, **settings)
                    if settings.get("data_dir", "data") is None:
                        with self.assertRaises(config.DataDirError):
                            OnboardingStorage.for_instance(path)
                    else:
                        self.assertEqual(OnboardingStorage.for_instance(path).data_dir, root / "data")
                    report = config.validate_instance(path)
                    self.assertIsNone(report.data_dir)
                    self.assertEqual(report.adapter_drafts, 0)
                    self.assertTrue(report.errors)
                    self.assertFalse((root / "data").exists())
