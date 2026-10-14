from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

from tests.fakes.upgrade import FakeUnitInstaller
from ummanu.installation.upgrade import (
    services as upgrade_services,
    types as upgrade_types,
    workspaces as upgrade_workspaces,
)
from ummanu.memory import pack as memory_pack
from ummanu.memory.pack import (
    MemoryPackDegradedError,
    MemoryPackError,
    load_product_pack,
    materialize_product_pack,
)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, text=True, capture_output=True
    ).stdout.strip()


def instance_repo(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "--initial-branch=main", "--quiet")
    git(path, "config", "user.name", "Test")
    git(path, "config", "user.email", "test@example.invalid")
    (path / "instance.yaml").write_text("version: 1\nname: example\n", encoding="utf-8")
    git(path, "add", "instance.yaml")
    git(path, "commit", "-m", "initial", "--quiet")
    return path


def write_pack(root: Path, facts: dict[str, str]) -> Path:
    pack = root / "packaging" / "memory" / "product-ummanu"
    pack.mkdir(parents=True, exist_ok=True)
    entries = []
    for fact_id, text in facts.items():
        filename = f"{fact_id}.md"
        raw = f"---\nsource: product:ummanu\n---\n{text}\n".encode()
        (pack / filename).write_bytes(raw)
        entries.append({"id": fact_id, "path": filename, "sha256": hashlib.sha256(raw).hexdigest()})
    manifest = {
        "schema": 1,
        "product": "ummanu",
        "namespace": "product:ummanu",
        "status": "active",
        "ownership": "shipped",
        "fact_format": "markdown-frontmatter-v1",
        "reconciliation": {
            "identity": "id",
            "digest": "sha256",
            "manifest_is_complete": True,
            "absent_id": "delete",
            "unchanged_digest": "retain_embedding",
        },
        "overlay_policy": {"local_overlay_allowed": True, "shipped_id_collision": "reject"},
        "facts": entries,
    }
    (pack / "manifest.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    return root


class ProductMemoryPackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.product = write_pack(self.root / "product", {"one": "one", "two": "two"})
        self.instance = instance_repo(self.root / "instance")
        self.data = self.root / "data"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def materialize(self):
        return materialize_product_pack(
            load_product_pack(self.product), instance_dir=self.instance, data_dir=self.data
        )

    def commit_memory(self, message: str) -> None:
        git(self.instance, "add", "state/memory")
        git(self.instance, "commit", "-m", message, "--quiet")

    def test_portable_materialization_owns_product_scope_and_exports_it(self):
        result = self.materialize()
        export = self.data / "memory" / "export.ndjson"
        rows = [json.loads(line) for line in export.read_text(encoding="utf-8").splitlines()]
        ledger = json.loads(
            (self.instance / "state/memory/packs/product-ummanu.json").read_text(encoding="utf-8")
        )

        self.assertTrue(result.changed)
        self.assertEqual({row["id"] for row in rows}, {"product-ummanu/one", "product-ummanu/two"})
        self.assertEqual(ledger["namespace"], "product:ummanu")

    def test_overlay_collision_is_refused_before_any_instance_mutation(self):
        local = self.instance / "state/memory/facts/product-ummanu/one.md"
        local.parent.mkdir(parents=True)
        local.write_text("local\n", encoding="utf-8")
        self.commit_memory("local fact")
        before = local.read_text(encoding="utf-8")

        with self.assertRaisesRegex(MemoryPackError, "collides"):
            self.materialize()

        self.assertEqual(local.read_text(encoding="utf-8"), before)
        self.assertFalse((self.instance / "state/memory/packs/product-ummanu.json").exists())

    def test_complete_manifest_deletes_only_previously_owned_ids_and_is_noop_when_unchanged(self):
        self.materialize()
        overlay = self.instance / "state/memory/facts/product-ummanu/local.md"
        overlay.write_text("local overlay\n", encoding="utf-8")
        project = self.instance / "state/memory/facts/project/local.md"
        project.parent.mkdir(parents=True)
        project.write_text("project fact\n", encoding="utf-8")
        self.commit_memory("local overlays")
        write_pack(self.product, {"one": "one changed", "three": "three"})

        changed = self.materialize()
        unchanged = self.materialize()

        facts = self.instance / "state/memory/facts/product-ummanu"
        self.assertTrue((facts / "one.md").read_text(encoding="utf-8").endswith("one changed\n"))
        self.assertFalse((facts / "two.md").exists())
        self.assertTrue((facts / "three.md").exists())
        self.assertEqual(overlay.read_text(encoding="utf-8"), "local overlay\n")
        self.assertEqual(project.read_text(encoding="utf-8"), "project fact\n")
        self.assertEqual((changed.added, changed.updated, changed.deleted), (1, 1, 1))
        self.assertFalse(unchanged.changed)

    def test_invalid_shipped_digest_fails_before_writing_the_instance(self):
        fact = self.product / "packaging/memory/product-ummanu/one.md"
        fact.write_text("tampered\n", encoding="utf-8")

        with self.assertRaisesRegex(MemoryPackError, "digest mismatch"):
            load_product_pack(self.product)

        self.assertFalse((self.instance / "state").exists())

    def test_malformed_missing_escaping_and_symlinked_sources_fail_before_instance_mutation(self):
        pack = self.product / "packaging/memory/product-ummanu"
        cases = {
            "malformed": lambda: (pack / "manifest.yaml").write_text("facts: [\n", encoding="utf-8"),
            "missing": lambda: self._rewrite_manifest_path("one", "missing.md"),
            "escaping": lambda: self._rewrite_manifest_path("one", "../outside.md"),
            "symlinked": self._symlink_pack_fact,
        }
        for name, corrupt in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                product = write_pack(root / "product", {"one": "one"})
                instance = instance_repo(root / "instance")
                self.product = product
                self.instance = instance
                pack = product / "packaging/memory/product-ummanu"
                corrupt()
                with self.assertRaises(MemoryPackError):
                    load_product_pack(product)
                self.assertFalse((instance / "state").exists())
        self.product = write_pack(self.root / "product", {"one": "one", "two": "two"})
        self.instance = instance_repo(self.root / "instance-replacement")

    def test_canon_write_failure_is_a_typed_pack_error_and_restores_the_canon(self):
        with mock.patch(
            "ummanu.memory.pack.canon_revision",
            side_effect=RuntimeError("canon disk unavailable"),
        ), self.assertRaisesRegex(MemoryPackError, "canon disk unavailable"):
            self.materialize()
        self.assertFalse(git(self.instance, "status", "--porcelain", "--", "state/memory"))
        self.assertEqual([path for path in (self.instance / "state/memory").rglob("*") if path.is_file()], [])
        self.assertFalse((self.data / "memory" / ".undo").exists())

    def test_failed_export_leaves_pending_ledger_and_no_pull_retry_converges(self):
        with mock.patch(
            "ummanu.memory.pack._publish_memory_export", side_effect=RuntimeError("data directory read-only")
        ), self.assertRaisesRegex(MemoryPackDegradedError, "data directory read-only"):
            self.materialize()

        ledger_path = self.instance / "state/memory/packs/product-ummanu.json"
        self.assertEqual(json.loads(ledger_path.read_text(encoding="utf-8"))["state"], "pending")
        retried = self.materialize()
        self.assertTrue(retried.changed)
        self.assertEqual(json.loads(ledger_path.read_text(encoding="utf-8"))["state"], "ready")
        self.assertTrue((self.data / "memory/export.ndjson").is_file())

    def test_root_handoff_precedes_export_and_makes_export_runtime_owned(self):
        report = SimpleNamespace(data_dir=self.data, host={"unit_prefix": "ummanu-"})
        context = upgrade_types.UpgradeContext(
            instance_path=self.instance,
            product_root=self.product,
            base_branch="main",
            dry_run=False,
            units=FakeUnitInstaller(active={"ummanu-memory.service"}),
            report=report,
            runtime_user="operator",
        )
        account = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())
        owned: set[Path] = set()
        actual_publish = memory_pack._publish_memory_export

        def chown(path, *_args, **_kwargs):
            owned.add(Path(path))

        def publish(*args, **kwargs):
            # The canon write is finished and handed to the runtime user before its export.
            self.assertIn(self.instance / "state/memory/facts/product-ummanu/one.md", owned)
            self.assertIn(self.instance / "state/memory/packs/product-ummanu.json", owned)
            return actual_publish(*args, **kwargs)

        with (
            mock.patch('ummanu.installation.upgrade.workspaces.os.geteuid', return_value=0),
            mock.patch('ummanu.installation.upgrade.workspaces.pwd.getpwnam', return_value=account),
            mock.patch('ummanu.installation.upgrade.workspaces.os.chown', side_effect=chown),
            mock.patch("ummanu.memory.pack._publish_memory_export", side_effect=publish),
        ):
            result = upgrade_workspaces.step_memory_pack(context)

        self.assertEqual(result.status, "changed")
        self.assertTrue(
            {self.data / "memory" / name for name in ("export.ndjson", "export.json", "manifest.json")}
            <= owned
        )

    def _rewrite_manifest_path(self, fact_id: str, path: str) -> None:
        manifest_path = self.product / "packaging/memory/product-ummanu/manifest.yaml"
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        next(entry for entry in manifest["facts"] if entry["id"] == fact_id)["path"] = path
        manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")

    def _symlink_pack_fact(self) -> None:
        pack = self.product / "packaging/memory/product-ummanu"
        fact = pack / "one.md"
        outside = self.root / "outside.md"
        outside.write_text("outside\n", encoding="utf-8")
        fact.unlink()
        fact.symlink_to(outside)

    def test_no_pull_memory_pack_step_detects_ledger_drift_and_requests_restart(self):
        # `memory` binds its receipt to the product checkout's revision, so the product is a Git one.
        git(self.product, "init", "--initial-branch=main", "--quiet")
        git(self.product, "add", "-A")
        git(self.product, "-c", "user.name=T", "-c", "user.email=t@example.invalid", "commit", "-qm", "p")
        report = SimpleNamespace(data_dir=self.data, host={"unit_prefix": "ummanu-"})
        context = upgrade_types.UpgradeContext(
            instance_path=self.instance,
            product_root=self.product,
            base_branch="main",
            dry_run=False,
            units=FakeUnitInstaller(active={"ummanu-memory.service"}),
            pull=False,
            report=report,
        )
        with mock.patch('ummanu.installation.upgrade.services.probe_memory') as probe:
            first = upgrade_workspaces.step_memory_pack(context)
            restarted = upgrade_services.step_memory(context)
            second = upgrade_workspaces.step_memory_pack(context)
            no_op = upgrade_services.step_memory(context)

        self.assertEqual(first.status, "changed")
        self.assertEqual(restarted.status, "changed")
        self.assertIn(("restart", "ummanu-memory.service"), context.units.calls)
        probe.assert_called_once()
        self.assertEqual(second.status, "unchanged")
        self.assertEqual(no_op.status, "unchanged")
