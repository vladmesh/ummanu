from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ummanu.backup_verify as backup_verify_module
from tests.restore_fixtures import (
    _core_archive,
    _full_archive,
    _git_history,
    _prepare_producer_data,
    _producer_exports,
    _write_checksums,
    _write_instance,
    _write_instance_to,
    create_backup,
    fake_engine_dump,
)
from ummanu.backup import verify_backup
from ummanu.backup_policy import ARCHIVE_ROOT
from ummanu.cli import main
from ummanu.restore import (
    RestoreError,
    bootstrap_empty,
    restore_backup,
)


class RestoreArchiveTests(unittest.TestCase):
    def test_restore_validates_then_publishes_staged_core_archive(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = _write_instance(root, "test")
            archive = _core_archive(root, "test")

            plan = restore_backup(
                archive,
                instance,
            )

            data_dir = root / "ummanu-data"
            self.assertEqual(plan.backup_kind, "core")
            self.assertEqual(
                plan.components,
                (
                    {"name": "board", "action": "restore"},
                    {"name": "memory", "action": "restore"},
                    {"name": "runs_state", "action": "restore"},
                    {"name": "board_history", "action": "restore"},
                    {"name": "memory_index", "action": "rebuild"},
                    {"name": "board_restore", "action": "handoff"},
                    {"name": "host_reconcile", "action": "handoff"},
                ),
            )
            self.assertTrue((data_dir / "board" / "cards.json").is_file())
            self.assertTrue((data_dir / "memory" / "facts").is_dir())

    def test_restore_runs_checksum_preflight_once(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = _write_instance(root, "test")
            archive = _core_archive(root, "test")
            original = backup_verify_module.verify_restore_payload

            with mock.patch.object(
                backup_verify_module,
                "verify_restore_payload",
                wraps=original,
            ) as verify:
                restore_backup(
                    archive,
                    instance,
                )

        verify.assert_called_once()

    def test_restore_cli_publishes_and_returns_stable_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = _write_instance(root, "test")
            archive = _core_archive(root, "test")
            output = io.StringIO()

            def restoring_archive(archive_arg, instance_arg, **kwargs):
                return restore_backup(
                    archive_arg,
                    instance_arg,
                    **kwargs,
                )

            with (
                mock.patch("ummanu.restore.restore_backup", side_effect=restoring_archive),
                mock.patch("sys.stdout", output),
            ):
                code = main(["restore", str(archive), "--instance", str(instance)])

            payload = json.loads(output.getvalue())
            self.assertTrue((root / "ummanu-data").is_dir())

        self.assertEqual(code, 0)
        self.assertEqual(
            payload,
            {
                "action": "restore",
                "archive": str(archive),
                "backup_kind": "core",
                "backup_version": 2,
                "components": [
                    {"name": "board", "action": "restore"},
                    {"name": "memory", "action": "restore"},
                    {"name": "runs_state", "action": "restore"},
                    {"name": "board_history", "action": "restore"},
                    {"name": "memory_index", "action": "rebuild"},
                    {"name": "board_restore", "action": "handoff"},
                    {"name": "host_reconcile", "action": "handoff"},
                ],
                "data_dir": str(root / "ummanu-data"),
                "dry_run": False,
                "instance_identity": {
                    "instance_remote": "git@example.invalid:test/instance.git",
                    "name": "test",
                },
                "next_steps": ["memory index rebuild", "board restore", "reconcile"],
                "ok": True,
            },
        )

    def test_restore_rejects_identity_before_creating_target(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = _write_instance(root, "target")
            archive = _core_archive(root, "source")

            with self.assertRaisesRegex(RestoreError, "identity"):
                restore_backup(
                    archive,
                    instance,
                )

            self.assertFalse((root / "ummanu-data").exists())

    def test_restore_never_overwrites_existing_target(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = _write_instance(root, "test")
            archive = _core_archive(root, "test")
            data_dir = root / "ummanu-data"
            data_dir.mkdir()
            (data_dir / "keep").write_text("keep", encoding="utf-8")

            with self.assertRaisesRegex(RestoreError, "already exists"):
                restore_backup(
                    archive,
                    instance,
                )
            self.assertEqual((data_dir / "keep").read_text(encoding="utf-8"), "keep")

    def test_bootstrap_anchors_relative_data_root_at_the_instance(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            instance.mkdir()
            (instance / "instance.yaml").write_text(
                "version: 1\nname: test\ndata_dir: relative-data\n"
                "offsite:\n  instance_remote: git@example.invalid:test/instance.git\n",
                encoding="utf-8",
            )

            bootstrap_empty(instance)

            self.assertTrue((instance / "relative-data" / "data-manifest.json").is_file())
            self.assertFalse((root / "relative-data").exists())

    def test_restore_rejects_archive_without_the_memory_component_before_staging(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = _write_instance(root, "test")
            _core_archive(root, "test")
            # Facts are canon in the private repo, so the derived export is the
            # whole memory component an archive carries.
            payload = root / ARCHIVE_ROOT
            (payload / "ummanu-data" / "memory" / "export.ndjson").unlink()
            manifest = json.loads((payload / "versions.json").read_text(encoding="utf-8"))
            _write_checksums(payload, manifest)
            (payload / "versions.json").write_text(json.dumps(manifest), encoding="utf-8")
            stripped = root / "without-memory.tar"
            with tarfile.open(stripped, "w") as destination:
                destination.add(payload, arcname=ARCHIVE_ROOT)

            with self.assertRaisesRegex(RestoreError, "memory/export.ndjson"):
                restore_backup(
                    stripped,
                    instance,
                )
            self.assertFalse((root / "ummanu-data").exists())

    def test_verify_rejects_archive_with_missing_memory_journal_object(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            archive = _core_archive(root, "test")
            damaged = root / "without-journal-object.tar"
            with tarfile.open(archive, "r") as source, tarfile.open(damaged, "w") as destination:
                for member in source.getmembers():
                    if member.isfile() and ".git/objects/" in member.name:
                        continue
                    handle = source.extractfile(member) if member.isfile() else None
                    destination.addfile(member, handle)

            result = verify_backup(
                damaged,
            )

        self.assertEqual(result.code, 1)
        self.assertTrue(
            any("checksum manifest does not match archive" in finding for finding in result.findings)
        )

    def test_full_restore_restores_data_components_and_plans_no_orca_component(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = _write_instance(root, "test")
            archive = _full_archive(root, "test")

            # The data half of `restore-postgres`; the engine half is tests/test_postgres_recovery.py's.
            plan = restore_backup(archive, instance, _allow_postgres_engine=True)

            actions = {component["name"]: component["action"] for component in plan.components}
            data_dir = root / "ummanu-data"
            self.assertNotIn("debug_orca_state", actions)
            self.assertEqual(actions["memory_index"], "rebuild")
            self.assertTrue((data_dir / "transcripts" / "inventory.json").is_file())
            self.assertTrue((data_dir / "artifacts" / "inventory.json").is_file())
            self.assertFalse((data_dir / "debug").exists())

    def test_a_full_archive_from_before_a20_step_9_still_verifies_and_restores(self):
        """An archive carrying `debug/orca-state/inventory.json` and its `debug_orca_state` manifest
        component (secretary-1726): optional debug material, read if present, never required and
        never restored as data."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = _write_instance(root, "test")
            archive = _full_archive(root, "test", legacy_orca_debug=True)
            with tarfile.open(archive) as bundle:
                self.assertIn(f"{ARCHIVE_ROOT}/debug/orca-state/inventory.json", bundle.getnames())

            verified = verify_backup(archive)
            self.assertEqual((verified.code, verified.findings), (0, []))
            self.assertIn("debug_orca_state", verified.manifest["components"])

            plan = restore_backup(archive, instance, _allow_postgres_engine=True)

            actions = {component["name"]: component["action"] for component in plan.components}
            data_dir = root / "ummanu-data"
            self.assertEqual(actions["memory_index"], "rebuild")
            self.assertTrue((data_dir / "transcripts" / "inventory.json").is_file())
            self.assertFalse((data_dir / "debug").exists())
            self.assertFalse(list(data_dir.rglob("*orca*")))

    def test_plain_restore_refuses_a_full_archive_it_cannot_restore_the_engine_of(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = _write_instance(root, "test")
            archive = _full_archive(root, "test")

            with self.assertRaisesRegex(RestoreError, "require ummanu restore-postgres"):
                restore_backup(archive, instance)
            self.assertFalse((root / "ummanu-data").exists())

    def test_restore_refuses_a_version_1_archive_as_an_unsupported_version(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = _write_instance(root, "test")
            archive = _core_archive(root, "test")
            payload = root / ARCHIVE_ROOT
            manifest = json.loads((payload / "versions.json").read_text(encoding="utf-8"))
            manifest["version"] = 1
            (payload / "versions.json").write_text(json.dumps(manifest), encoding="utf-8")
            with tarfile.open(archive, "w") as bundle:
                bundle.add(payload, arcname=ARCHIVE_ROOT)
            output = io.StringIO()

            with self.assertRaises(RestoreError) as refused:
                restore_backup(archive, instance)
            with mock.patch("sys.stdout", output), mock.patch("sys.stderr", output):
                code = main(["restore", str(archive), "--instance", str(instance)])

            self.assertEqual(
                str(refused.exception), "unsupported backup version: 1; this build reads version 2"
            )
            self.assertNotEqual(code, 0)
            self.assertIn("unsupported backup version: 1", output.getvalue())
            self.assertFalse((root / "ummanu-data").exists())

    def test_restore_rejects_checksum_mismatch_without_publishing_target(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = _write_instance(root, "test")
            archive = _core_archive(root, "test")
            payload = root / ARCHIVE_ROOT
            (payload / "ummanu-data" / "board" / "cards.json").write_text(
                '{"cards": ["changed"]}', encoding="utf-8"
            )
            with tarfile.open(archive, "w") as bundle:
                bundle.add(payload, arcname=ARCHIVE_ROOT)

            with self.assertRaisesRegex(RestoreError, "checksum mismatch"):
                restore_backup(
                    archive,
                    instance,
                )
            self.assertFalse((root / "ummanu-data").exists())

    def test_restore_rejects_unexpected_data_component_before_publishing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = _write_instance(root, "test")
            archive = _core_archive(root, "test")
            payload = root / ARCHIVE_ROOT
            extra = payload / "ummanu-data" / "runs" / "untrusted.json"
            extra.write_text("{}", encoding="utf-8")
            manifest = json.loads((payload / "versions.json").read_text(encoding="utf-8"))
            _write_checksums(payload, manifest)
            (payload / "versions.json").write_text(json.dumps(manifest), encoding="utf-8")
            with tarfile.open(archive, "w") as bundle:
                bundle.add(payload, arcname=ARCHIVE_ROOT)

            with self.assertRaisesRegex(RestoreError, "unexpected data component"):
                restore_backup(
                    archive,
                    instance,
                )
            self.assertFalse((root / "ummanu-data").exists())

    def test_restore_rejects_a_data_manifest_below_the_top_level(self):
        # The archive carries the file members only: without a directory member
        # for "evil" the entry is judged on its own path, which used to pass the
        # gate on its basename alone.
        for smuggled in ("evil/data-manifest.json", "evil/deep/nested/data-manifest.json"):
            with self.subTest(smuggled=smuggled), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                instance = _write_instance(root, "test")
                archive = _core_archive(root, "test")
                payload = root / ARCHIVE_ROOT
                entry = payload / "ummanu-data" / smuggled
                entry.parent.mkdir(parents=True, exist_ok=True)
                entry.write_text("{}", encoding="utf-8")
                manifest = json.loads((payload / "versions.json").read_text(encoding="utf-8"))
                _write_checksums(payload, manifest)
                (payload / "versions.json").write_text(json.dumps(manifest), encoding="utf-8")
                top = Path(smuggled).parts[0]
                with tarfile.open(archive, "w") as bundle:
                    bundle.add(
                        payload,
                        arcname=ARCHIVE_ROOT,
                        filter=lambda info, top=top: None if top in Path(info.name).parts else info,
                    )
                    bundle.add(
                        entry,
                        arcname=f"{ARCHIVE_ROOT}/ummanu-data/{smuggled}",
                    )

                with self.assertRaisesRegex(RestoreError, "unexpected data component"):
                    restore_backup(
                        archive,
                        instance,
                    )
                self.assertFalse((root / "ummanu-data").exists())

    def test_restore_discards_memory_journal_runtime_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = _write_instance(root, "test")
            archive = _core_archive(root, "test")
            payload = root / ARCHIVE_ROOT
            git_dir = payload / "ummanu-data" / "memory" / "facts" / ".git"
            (git_dir / "hooks").mkdir(parents=True)
            (git_dir / "hooks" / "post-checkout").write_text("exit 1\n", encoding="utf-8")
            (git_dir / "config").write_text("[core]\nfsmonitor = bad\n", encoding="utf-8")
            (git_dir / "modules" / "nested").mkdir(parents=True)
            (git_dir / "modules" / "nested" / "config").write_text("[core]\npager = bad\n", encoding="utf-8")
            manifest = json.loads((payload / "versions.json").read_text(encoding="utf-8"))
            _write_checksums(payload, manifest)
            (payload / "versions.json").write_text(json.dumps(manifest), encoding="utf-8")
            with tarfile.open(archive, "w") as bundle:
                bundle.add(payload, arcname=ARCHIVE_ROOT)

            restore_backup(
                archive,
                instance,
            )
            restored_git = root / "ummanu-data" / "memory" / "facts" / ".git"
            self.assertFalse((restored_git / "hooks").exists())
            self.assertFalse((restored_git / "config").exists())
            self.assertFalse((restored_git / "modules").exists())
            # The index is runtime state too, and nothing rebuilds it on the way
            # in now that the journal is no longer canon.
            self.assertFalse((restored_git / "index").exists())
            self.assertEqual(len(_git_history(restored_git.parent)), 2)
            # Both facts still reach the target: on disk, and in the commit the
            # history points at. Read the tree, not the discarded index.
            self.assertTrue((restored_git.parent / "fact.md").is_file())
            self.assertTrue((restored_git.parent / "second-fact.md").is_file())
            committed = subprocess.run(
                ["git", "ls-tree", "-r", "--name-only", "HEAD"],
                cwd=restored_git.parent,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(committed.stdout.splitlines(), ["fact.md", "second-fact.md"])

    def test_verify_and_restore_ignore_memory_journal_runtime_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = _write_instance(root, "test")
            archive = _core_archive(root, "test")
            payload = root / ARCHIVE_ROOT
            git_dir = payload / "ummanu-data" / "memory" / "facts" / ".git"
            marker = root / "runtime-hook-ran"
            hooks = git_dir / "hooks"
            hooks.mkdir()
            hook = hooks / "post-checkout"
            hook.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
            hook.chmod(0o755)
            (git_dir / "config").write_text(
                "[core]\n\thooksPath = hooks\n",
                encoding="utf-8",
            )
            manifest = json.loads((payload / "versions.json").read_text(encoding="utf-8"))
            _write_checksums(payload, manifest)
            (payload / "versions.json").write_text(json.dumps(manifest), encoding="utf-8")
            with tarfile.open(archive, "w") as bundle:
                bundle.add(payload, arcname=ARCHIVE_ROOT)

            verified = verify_backup(
                archive,
            )
            restore_backup(
                archive,
                instance,
            )

            restored_git = root / "ummanu-data" / "memory" / "facts" / ".git"
            self.assertEqual(verified.code, 0, verified.findings)
            self.assertFalse(marker.exists())
            self.assertFalse((restored_git / "config").exists())
            self.assertFalse((restored_git / "hooks").exists())

    def test_verify_and_restore_reject_journal_object_alternates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = _write_instance(root, "test")
            archive = _core_archive(root, "test")
            payload = root / ARCHIVE_ROOT
            objects = payload / "ummanu-data" / "memory" / "facts" / ".git" / "objects"
            alternate_store = root / "alternate-objects"
            shutil.copytree(objects, alternate_store)
            shutil.rmtree(objects)
            (objects / "info").mkdir(parents=True)
            (objects / "info" / "alternates").write_text(
                f"{alternate_store}\n",
                encoding="utf-8",
            )
            manifest = json.loads((payload / "versions.json").read_text(encoding="utf-8"))
            _write_checksums(payload, manifest)
            (payload / "versions.json").write_text(json.dumps(manifest), encoding="utf-8")
            with tarfile.open(archive, "w") as bundle:
                bundle.add(payload, arcname=ARCHIVE_ROOT)

            verified = verify_backup(
                archive,
            )
            with self.assertRaisesRegex(RestoreError, "unexpected data component"):
                restore_backup(
                    archive,
                    instance,
                )

            self.assertEqual(verified.code, 1)
            self.assertIn(
                "unexpected data component: memory/facts/.git/objects/info/alternates",
                verified.findings,
            )
            self.assertFalse((root / "ummanu-data").exists())

    def test_restore_publish_failure_leaves_target_unpublished(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = _write_instance(root, "test")
            archive = _core_archive(root, "test")
            from unittest import mock

            real_replace = os.replace

            def fail_publish(source, destination):
                if Path(destination) == root / "ummanu-data":
                    raise OSError("disk error")
                return real_replace(source, destination)

            with (mock.patch("ummanu.restore.os.replace", side_effect=fail_publish),
                  self.assertRaisesRegex(RestoreError, "restore staging failed")):
                restore_backup(
                    archive,
                    instance,
                )
            self.assertFalse((root / "ummanu-data").exists())

    def test_restore_state_write_failure_leaves_target_unpublished(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = _write_instance(root, "test")
            archive = _core_archive(root, "test")

            with (
                mock.patch(
                    "ummanu.restore.write_text_atomic",
                    side_effect=RuntimeError("disk error"),
                ),
                self.assertRaisesRegex(RestoreError, "could not record restore progress"),
            ):
                restore_backup(
                    archive,
                    instance,
                )

            self.assertFalse((root / "ummanu-data").exists())

    def test_restore_preserves_memory_history_and_manifest_counts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = _write_instance(root, "test")
            archive = _core_archive(root, "test")

            restore_backup(
                archive,
                instance,
            )

            data_dir = root / "ummanu-data"
            manifest = json.loads((root / ARCHIVE_ROOT / "versions.json").read_text(encoding="utf-8"))
            source_journal = root / ARCHIVE_ROOT / "ummanu-data" / "memory" / "facts"
            expected_history = _git_history(source_journal)
            cards = json.loads((data_dir / "board" / "cards.json").read_text())["cards"]
            self.assertEqual(len(cards), manifest["components"]["board"]["count"])
            facts = (data_dir / "memory" / "export.ndjson").read_text().splitlines()
            self.assertEqual(len(facts), manifest["components"]["memory"]["count"])
            self.assertEqual(_git_history(data_dir / "memory" / "facts"), expected_history)

    def test_create_backups_round_trip_through_restore(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source_data = root / "source-data"
            source_instance = _write_instance_to(root / "source-instance", "test", source_data)
            _prepare_producer_data(source_data, source_instance)
            source_export = (source_data / "memory" / "export.ndjson").read_text()

            for kind in ("core", "full"):
                target_data = root / f"target-{kind}-data"
                target_instance = _write_instance_to(root / f"target-{kind}-instance", "test", target_data)
                with (
                    mock.patch("ummanu.backup._reject_claimed_worker_context"),
                    mock.patch("ummanu.backup._pipeline_status", return_value={"paused": False}),
                    mock.patch("ummanu.backup._pipeline_action", return_value=None),
                    fake_engine_dump(),
                    mock.patch(
                        "ummanu.backup.export_all",
                        return_value=_producer_exports(source_data),
                    ),
                ):
                    backup = create_backup(
                        source_instance,
                        backup_kind=kind,
                    )

                # A full archive's data half is what `restore-postgres` publishes after the engine.
                restore_backup(backup.archive, target_instance, _allow_postgres_engine=kind == "full")
                manifest = backup.manifest
                cards = json.loads((target_data / "board" / "cards.json").read_text())["cards"]
                facts = (target_data / "memory" / "export.ndjson").read_text().splitlines()
                self.assertEqual(len(cards), manifest["components"]["board"]["count"])
                self.assertEqual(len(facts), manifest["components"]["memory"]["count"])
                # A post-flatten archive carries the derived export byte for
                # byte and no journal at all; canon travels in the private repo.
                self.assertEqual((target_data / "memory" / "export.ndjson").read_text(), source_export)
                self.assertFalse((target_data / "memory" / "facts").exists())
                with tarfile.open(backup.archive) as bundle:
                    names = bundle.getnames()
                data_prefix = f"{ARCHIVE_ROOT}/ummanu-data/"
                self.assertEqual(
                    [name for name in names if name.startswith(data_prefix) and "memory/facts" in name],
                    [],
                )
                # Canon rides along with the instance config, which is where the
                # private repo keeps it.
                self.assertIn(f"{ARCHIVE_ROOT}/instance/state/memory/facts/fact.md", names)
