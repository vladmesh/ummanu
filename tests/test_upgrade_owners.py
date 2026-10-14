"""Upgrade owner seams and file-only characterization, with opt-in external base capture."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests import source_trees
from tests.fakes.upgrade import FakeUnitInstaller
from tests.support.upgrade_characterization import capture
from ummanu import upgrade
from ummanu.infra import process_receipts
from ummanu.installation import materialize, support
from ummanu.installation.upgrade import (
    checkout as upgrade_checkout,
    dependencies as upgrade_dependencies,
    host as upgrade_host,
    services as upgrade_services,
    types as upgrade_types,
    workspaces as upgrade_workspaces,
)

OWNERS = (
    upgrade,
    upgrade_checkout,
    upgrade_dependencies,
    upgrade_workspaces,
    upgrade_host,
    upgrade_services,
    upgrade_types,
)
ROOT = Path(__file__).resolve().parents[1]


class UpgradeOwnersTests(unittest.TestCase):
    def test_moved_front_renderer_keeps_the_running_source_for_its_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = upgrade_types.UpgradeContext(
                instance_path=root / "instance",
                product_root=root / "selected-product",
                base_branch="main",
                dry_run=False,
                units=FakeUnitInstaller(),
                report=SimpleNamespace(
                    data_dir=root / "data",
                    host={
                        "unit_prefix": "ummanu-",
                        "web_front": {"sites": ["example.test"]},
                    },
                ),
            )
            with mock.patch.object(
                upgrade_host.state_repo,
                "run_as_git_child",
                return_value=SimpleNamespace(
                    returncode=0,
                    stdout='{"changed": false}',
                ),
            ) as render:
                result = upgrade_host.step_web_front_config(context)
            self.assertEqual(result.status, "unchanged", result.detail)
            render.assert_called_once()
            self.assertEqual(render.call_args.kwargs["extra_env"], {"PYTHONPATH": str(ROOT / "src")})
            self.assertEqual(render.call_args.args[0], context.instance_path)

    def test_shared_types_default_binding_and_actual_install_consumers(self) -> None:
        for owner in OWNERS:
            if hasattr(owner, "UpgradeContext"):
                self.assertIs(owner.UpgradeContext, upgrade_types.UpgradeContext)
        self.assertIs(upgrade.run_steps.__defaults__[0], upgrade.STEPS)
        self.assertIs(materialize.STEPS, upgrade.STEPS)
        self.assertIs(materialize.run_steps, upgrade.run_steps)
        self.assertIs(materialize.step_host, upgrade_host.step_host)
        self.assertIs(materialize.step_pipeline_state, upgrade_host.step_pipeline_state)
        self.assertIs(support._set_runtime_owner, upgrade_workspaces._set_runtime_owner)
        self.assertIs(upgrade_services.ReceiptError, process_receipts.ReceiptError)
        self.assertEqual(upgrade.running_product_root(), ROOT)

    def test_family_is_bounded_and_step_owners_do_not_import_the_entry(self) -> None:
        # Discover the current family instead of pinning its file count as an API.
        for path in [
            ROOT / "src/ummanu/upgrade.py",
            *(ROOT / "src/ummanu/installation/upgrade").glob("*.py"),
        ]:
            self.assertLessEqual(len(path.read_text().splitlines()), 1500, path.name)
            if path.stem == "upgrade":
                continue
            for node in source_trees.imports(source_trees.parse(path.read_text())):
                if isinstance(node, ast.ImportFrom):
                    self.assertNotEqual(node.module, "ummanu.upgrade", path.name)
                    if node.module == "ummanu":
                        self.assertNotIn("upgrade", [a.name for a in node.names], path.name)

    def test_file_only_characterization_and_optional_external_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            packet = capture(*OWNERS, Path(tmp) / "candidate")
            self.assertEqual(packet["steps"]["steps"][-1]["status"], "failed")
            self.assertEqual([row[0] for row in packet["trace"]], ["first", "failed"])
            self.assertEqual([row[1] for row in packet["errors"]], [2, 2, 1, 1, 1, 1])
            self.assertEqual(json.loads(packet["web_bytes"])["process"]["pid"], 123)
            self.assertEqual(packet["install_recover"][1][1][1], ["restore", True])
            base_path = os.environ.get("UMMANU_UPGRADE_BASE_SOURCE")
            if base_path:
                path = Path(base_path).resolve()
                self.assertFalse(path.is_relative_to(ROOT))
                source = path.read_bytes()
                inventory = json.loads((ROOT / "docs" / "upgrade-owner-inventory.json").read_text())
                self.assertEqual(hashlib.sha256(source).hexdigest(), inventory["base_source_sha256"])
                spec = importlib.util.spec_from_file_location("_external_upgrade_base", path)
                baseline = importlib.util.module_from_spec(spec)
                with mock.patch.dict(sys.modules, {spec.name: baseline}):
                    spec.loader.exec_module(baseline)
                    old = capture(*(baseline,) * len(OWNERS), Path(tmp) / "base")
                self.assertEqual(old, packet)
            destination = os.environ.get("UMMANU_UPGRADE_PACKET_OUT")
            if destination:
                Path(destination).write_text(json.dumps(packet, sort_keys=True, indent=2) + "\n")
                if base_path:
                    Path(destination).with_suffix(".base.json").write_text(
                        json.dumps(old, sort_keys=True, indent=2) + "\n"
                    )
