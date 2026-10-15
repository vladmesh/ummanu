from __future__ import annotations

import errno
import fcntl
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.restore_fixtures import (
    ENGINE_DUMP_BYTES,
    create_backup,
    engine_dump_component,
    fake_engine_dump,
)
from ummanu.backup import create_backups, verify_backup
from ummanu.backup_policy import POLICIES, should_skip_data_entry
from ummanu.data import DataExport


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.env_patch = mock.patch.dict(os.environ, {"BOARD_ROLE": ""})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.workspace_patch = mock.patch("ummanu.backup._claimed_workspace_from_cwd", return_value=None)
        self.workspace_patch.start()
        self.addCleanup(self.workspace_patch.stop)
        self.engine_dump = self.enterContext(fake_engine_dump())

    def test_create_writes_archive_with_expected_structure_and_verify_is_ok(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            data_dir = root / "ummanu-data"
            _write_instance(instance, data_dir)
            (instance / "runtime.env").write_text("BOARD_API_TOKEN=do-not-archive\n", encoding="utf-8")

            pipeline_calls: list[str] = []

            def fake_pipeline(action, **_kwargs):
                pipeline_calls.append(action)


            def fake_export_all(data_dir_arg, instance_dir_arg, *, copy_transcripts):
                self.assertFalse(copy_transcripts)
                # Canon hangs off the repo root, so the export needs it.
                self.assertEqual(Path(instance_dir_arg).resolve(), instance.resolve())
                _write_export_surface(data_dir_arg)
                return {
                    "board": DataExport(data_dir_arg / "board" / "cards.json", 1, "board"),
                    "memory": DataExport(data_dir_arg / "memory" / "export.ndjson", 1, "memory"),
                    "runs": DataExport(data_dir_arg / "runs" / "runs.ndjson", 1, "runs"),
                    "transcripts": DataExport(
                        data_dir_arg / "transcripts" / "inventory.json",
                        1,
                        "transcripts",
                    ),
                    "artifacts": DataExport(
                        data_dir_arg / "artifacts" / "inventory.json",
                        1,
                        "artifacts",
                    ),
                }

            with (
                mock.patch("ummanu.backup._pipeline_status", return_value={"paused": False}),
                mock.patch("ummanu.backup._pipeline_action", side_effect=fake_pipeline),
                mock.patch("ummanu.backup.export_all", side_effect=fake_export_all),
            ):
                result = create_backup(
                    instance,
                )

            self.assertEqual(pipeline_calls, ["pause", "resume"])
            self.assertTrue(result.archive.is_file())
            verified = verify_backup(
                result.archive,
            )

            self.assertEqual(verified.code, 0, verified.findings)
            self.assertEqual(verified.manifest["version"], 2)
            self.assertEqual(verified.manifest["board_backend"], "postgres")
            with tarfile.open(result.archive, "r") as archive:
                names = set(archive.getnames())
            self.assertIn("ummanu-backup/versions.json", names)
            self.assertIn("ummanu-backup/instance/instance.yaml", names)
            self.assertIn("ummanu-backup/ummanu-data/board/cards.json", names)
            self.assertIn("ummanu-backup/engine/postgres.dump", names)
            self.assertIn("ummanu-backup/ummanu-data/board/audit.json", names)
            self.assertIn("ummanu-backup/ummanu-data/runs/runs.ndjson", names)
            self.assertIn("ummanu-backup/ummanu-data/runs/cards.json", names)
            self.assertIn("ummanu-backup/ummanu-data/artifacts/inventory.json", names)
            # A20 step 9 (secretary-1726): a new backup carries no Orca state, debug or otherwise.
            self.assertFalse([name for name in names if "orca" in name or "/debug" in name], names)
            self.assertNotIn("debug_orca_state", verified.manifest["components"])
            self.assertNotIn("ummanu-backup/ummanu-data/memory/index.sqlite", names)
            self.assertNotIn("ummanu-backup/ummanu-data/backups/old.tar", names)
            self.assertNotIn("ummanu-backup/instance/runtime.env", names)
            self.assertNotIn("ummanu-backup/instance/.env", names)

    def test_create_excludes_memory_journal_hooks_and_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            data_dir = root / "ummanu-data"
            _write_instance(instance, data_dir)
            _write_export_surface(data_dir)
            git_dir = data_dir / "memory" / "facts" / ".git"
            subprocess.run(["git", "init", "--quiet", "--initial-branch", "main", str(git_dir.parent)], check=True)
            (git_dir / "hooks").mkdir(exist_ok=True)
            (git_dir / "hooks" / "post-checkout").write_text("exit 1\n", encoding="utf-8")
            (git_dir / "config").write_text("[core]\nfsmonitor = bad\n", encoding="utf-8")
            (git_dir / "modules" / "nested").mkdir(parents=True)
            (git_dir / "modules" / "nested" / "config").write_text("[core]\npager = bad\n", encoding="utf-8")
            (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
            cache = data_dir / "memory" / "fastembed-cache" / "models--bge-m3"
            cache.mkdir(parents=True)
            (cache / "model.onnx").write_bytes(b"onnx")

            exports = {
                "board": DataExport(data_dir / "board" / "cards.json", 1, "test"),
                "memory": DataExport(data_dir / "memory" / "export.ndjson", 1, "test"),
                "runs": DataExport(data_dir / "runs" / "runs.ndjson", 1, "test"),
                "transcripts": DataExport(data_dir / "transcripts" / "inventory.json", 1, "test"),
                "artifacts": DataExport(data_dir / "artifacts" / "inventory.json", 1, "test"),
            }

            with (
                mock.patch("ummanu.backup._pipeline_status", return_value={"paused": False}),
                mock.patch("ummanu.backup._pipeline_action", return_value=None),
                mock.patch("ummanu.backup.export_all", return_value=exports),
            ):
                result = create_backup(
                    instance,
                )

            with tarfile.open(result.archive, "r") as archive:
                names = set(archive.getnames())
            self.assertIn("ummanu-backup/ummanu-data/memory/facts/.git/HEAD", names)
            self.assertNotIn("ummanu-backup/ummanu-data/memory/facts/.git/config", names)
            self.assertNotIn("ummanu-backup/ummanu-data/memory/facts/.git/hooks/post-checkout", names)
            self.assertNotIn("ummanu-backup/ummanu-data/memory/facts/.git/modules/nested/config", names)
            self.assertIn("ummanu-backup/ummanu-data/memory/export.ndjson", names)
            self.assertEqual(
                [name for name in names if "ummanu-data/memory/fastembed-cache" in name], []
            )
            self.assertEqual(
                verify_backup(
                    result.archive,
                ).code,
                0,
            )

    def _full_backup_with_model_cache(self, root: Path, *, cache_bytes: int = 4) -> tuple[Path, Path]:
        instance = root / "instance"
        data_dir = root / "ummanu-data"
        _write_instance(instance, data_dir)
        _write_export_surface(data_dir)
        (data_dir / "memory" / "export.ndjson").write_text("{}\n" * 20000, encoding="utf-8")
        cache = data_dir / "memory" / "fastembed-cache" / "models--bge-m3"
        cache.mkdir(parents=True)
        (cache / "model.onnx").write_bytes(b"\0" * cache_bytes)
        orca = root / "orca"
        orca.mkdir()
        (orca / "state.json").write_text("{}", encoding="utf-8")
        return instance, data_dir

    def _create_full(self, instance: Path, data_dir: Path):
        exports = {
            "board": DataExport(data_dir / "board" / "cards.json", 1, "test"),
            "memory": DataExport(data_dir / "memory" / "export.ndjson", 1, "test"),
            "runs": DataExport(data_dir / "runs" / "runs.ndjson", 1, "test"),
            "transcripts": DataExport(data_dir / "transcripts" / "inventory.json", 1, "test"),
            "artifacts": DataExport(data_dir / "artifacts" / "inventory.json", 1, "test"),
        }
        with (
            mock.patch("ummanu.backup._pipeline_status", return_value={"paused": False}),
            mock.patch("ummanu.backup._pipeline_action", return_value=None),
            mock.patch("ummanu.backup.export_all", return_value=exports),
        ):
            return create_backup(instance)

    def test_a_nested_versions_json_gets_a_checksum_and_the_archive_verifies(self):
        # Only the root manifest skips checksumming (ummanu-8).
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            data_dir = root / "ummanu-data"
            _write_instance(instance, data_dir)
            _write_export_surface(data_dir)
            nested = instance / "state" / "knowledge" / "reports" / "x" / "versions.json"
            nested.parent.mkdir(parents=True)
            nested.write_text('{"nested": true}\n', encoding="utf-8")

            result = self._create_full(instance, data_dir)

            checksums = result.manifest["checksums"]
            self.assertIn("instance/state/knowledge/reports/x/versions.json", checksums)
            self.assertNotIn("versions.json", checksums)
            verified = verify_backup(result.archive)
            self.assertEqual(verified.code, 0, verified.findings)
            self.assertEqual(verified.findings, [])

    def test_a_full_archive_written_with_the_model_cache_still_verifies_and_restores_without_it(self):
        from ummanu.backup_policy import is_memory_model_cache_entry
        from ummanu.restore import restore_backup

        def before_the_exclusion(relative, *, policy):
            if is_memory_model_cache_entry(relative):
                return False
            return should_skip_data_entry(relative, policy=policy)

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance, data_dir = self._full_backup_with_model_cache(root)
            with mock.patch("ummanu.backup.should_skip_data_entry", side_effect=before_the_exclusion):
                result = self._create_full(instance, data_dir)
            with tarfile.open(result.archive, "r") as archive:
                names = set(archive.getnames())
            self.assertIn(
                "ummanu-backup/ummanu-data/memory/fastembed-cache/models--bge-m3/model.onnx", names
            )

            self.assertEqual(verify_backup(result.archive).code, 0)
            target = root / "target-instance"
            restored = root / "restored-data"
            _write_instance(target, restored)
            # The data half of `restore-postgres`; the engine half is tests/test_postgres_recovery.py's.
            restore_backup(result.archive, target, _allow_postgres_engine=True)

            self.assertEqual(
                (restored / "memory" / "export.ndjson").read_text(encoding="utf-8"), "{}\n" * 20000
            )
            self.assertFalse((restored / "memory" / "fastembed-cache").exists())

    def test_the_po_queue_round_trips_through_a_full_and_a_core_archive(self):
        """secretary-1770: pending and set-aside PO inputs come back; an in-flight temporary file does not."""
        from ummanu.po.queue import PoQueue
        from ummanu.restore import restore_backup

        for kind in ("full", "core"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                instance, data_dir = self._full_backup_with_model_cache(root)
                queue = PoQueue(data_dir)
                queue.put(session_id="s-1", text="the owner's message", request_id="web-po-1", source="web")
                refused = queue.put(session_id="s-2", text="for a closed session", request_id="web-po-2", source="web")
                queue.refuse(refused, "PO session s-2 is closed")
                (queue.directory / ".0001.json.tmp").write_text("half-written", encoding="utf-8")
                exports = {
                    "board": DataExport(data_dir / "board" / "cards.json", 1, "test"),
                    "memory": DataExport(data_dir / "memory" / "export.ndjson", 1, "test"),
                    "runs": DataExport(data_dir / "runs" / "runs.ndjson", 1, "test"),
                    "transcripts": DataExport(data_dir / "transcripts" / "inventory.json", 1, "test"),
                    "artifacts": DataExport(data_dir / "artifacts" / "inventory.json", 1, "test"),
                }
                with (
                    mock.patch("ummanu.backup._pipeline_status", return_value={"paused": False}),
                    mock.patch("ummanu.backup._pipeline_action", return_value=None),
                    mock.patch("ummanu.backup.export_all", return_value=exports),
                ):
                    result = create_backup(instance, backup_kind=kind)
                self.assertEqual(verify_backup(result.archive).code, 0)
                target = root / "target-instance"
                restored = root / "restored-data"
                _write_instance(target, restored)
                restore_backup(result.archive, target, _allow_postgres_engine=True)

                back = PoQueue(restored)
                self.assertEqual(
                    [(item.session_id, item.text, item.request_id) for item in back.pending()],
                    [("s-1", "the owner's message", "web-po-1")],
                )
                self.assertEqual(back.find_refused("web-po-2")["reason"], "PO session s-2 is closed")
                self.assertFalse((back.directory / ".0001.json.tmp").exists())

    def test_create_rejects_claimed_worker_context(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            data_dir = root / "ummanu-data"
            _write_instance(instance, data_dir)

            with (
                mock.patch.dict(os.environ, {"BOARD_ROLE": "worker"}),
                self.assertRaisesRegex(RuntimeError, "claimed worker"),
            ):
                create_backup(instance)

    def test_create_anchors_relative_instance_data_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            _write_instance(instance, Path("ummanu-data"))

            with (
                mock.patch("ummanu.backup._pipeline_status", return_value={"paused": False}),
                mock.patch("ummanu.backup._pipeline_action", return_value=None),
                mock.patch(
                    "ummanu.backup.export_all",
                    side_effect=lambda data_dir, *_args, **_kwargs: _fake_exports(data_dir),
                ),
            ):
                create_backup(instance)

            self.assertTrue((instance / "ummanu-data" / "backups").exists())
            self.assertFalse((root / "ummanu-data").exists())

    def test_create_ignores_invalid_project_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            data_dir = root / "ummanu-data"
            _write_instance(instance, data_dir)
            projects = instance / "projects"
            projects.mkdir()
            (projects / "broken.yaml").write_text("defualt_branch: main\n", encoding="utf-8")

            with (
                mock.patch("ummanu.backup._pipeline_status", return_value={"paused": False}),
                mock.patch("ummanu.backup._pipeline_action", return_value=None),
                mock.patch(
                    "ummanu.backup.export_all",
                    side_effect=RuntimeError("snapshot reached"),
                ),
                self.assertRaisesRegex(RuntimeError, "snapshot reached"),
            ):
                create_backup(instance)

    def test_create_rejects_claimed_workspace_when_board_role_is_removed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "orca" / "workspaces" / "ummanu" / "380-backup"
            workspace.mkdir(parents=True)
            (workspace / "TASK.md").write_text("task\n", encoding="utf-8")
            instance = root / "instance"
            data_dir = root / "ummanu-data"
            _write_instance(instance, data_dir)

            with (
                mock.patch.dict(os.environ, {"BOARD_ROLE": ""}),
                mock.patch("ummanu.backup._claimed_workspace_from_cwd", return_value=workspace),
                self.assertRaisesRegex(RuntimeError, "claimed worker"),
            ):
                create_backup(instance)

    def test_create_stages_beside_the_archives_when_the_system_temp_is_another_filesystem(self):
        """ummanu-241: a tmpfs `/tmp` or a unit's `PrivateTmp` is another mount, and a rename from it
        into `<data>/backups` is `EXDEV`. Here the system temp directory is a separate directory and
        `os.replace` refuses every rename that crosses its boundary, as the kernel would."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            data_dir = root / "ummanu-data"
            system_tmp = root / "system-tmp"
            system_tmp.mkdir()
            _write_instance(instance, data_dir)
            real_replace = os.replace

            def other_mount(path: object) -> bool:
                return Path(os.fspath(path)).resolve().is_relative_to(system_tmp.resolve())

            def replace(source, destination, *args, **kwargs):
                if other_mount(source) != other_mount(destination):
                    raise OSError(errno.EXDEV, os.strerror(errno.EXDEV), str(source), None, str(destination))
                return real_replace(source, destination, *args, **kwargs)

            def fake_export_all(data_dir_arg, _instance_dir, *, copy_transcripts):
                _write_export_surface(data_dir_arg)
                return {
                    name: DataExport(data_dir_arg / relative, 1, name)
                    for name, relative in (
                        ("board", "board/cards.json"),
                        ("memory", "memory/export.ndjson"),
                        ("runs", "runs/runs.ndjson"),
                        ("transcripts", "transcripts/inventory.json"),
                        ("artifacts", "artifacts/inventory.json"),
                    )
                }

            with (
                mock.patch.object(tempfile, "tempdir", str(system_tmp)),
                mock.patch("os.replace", side_effect=replace),
                mock.patch("ummanu.backup._pipeline_status", return_value={"paused": False}),
                mock.patch("ummanu.backup._pipeline_action", return_value=None),
                mock.patch("ummanu.backup.export_all", side_effect=fake_export_all),
            ):
                result = create_backup(instance)

            self.assertTrue(result.archive.is_file())
            self.assertEqual(result.archive.parent, data_dir / "backups")
            self.assertEqual(verify_backup(result.archive).code, 0)
            self.assertEqual(list(system_tmp.iterdir()), [], "backup create staged in the system temp")
            self.assertEqual([path.name for path in (data_dir / "backups").glob(".ummanu-*")], [])

    def test_create_resumes_pipeline_when_snapshot_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            data_dir = root / "ummanu-data"
            _write_instance(instance, data_dir)
            pipeline_calls: list[str] = []

            def fake_pipeline(action, **_kwargs):
                pipeline_calls.append(action)

            with (
                mock.patch("ummanu.backup._pipeline_status", return_value={"paused": False}),
                mock.patch("ummanu.backup._pipeline_action", side_effect=fake_pipeline),
                mock.patch("ummanu.backup.export_all", side_effect=RuntimeError("boom")),
                self.assertRaisesRegex(RuntimeError, "boom"),
            ):
                create_backup(instance)

            self.assertEqual(pipeline_calls, ["pause", "resume"])

    def test_create_uses_pipeline_freeze_pause_contract(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            data_dir = root / "ummanu-data"
            _write_instance(instance, data_dir)
            calls: list[list[str]] = []

            def fake_run(args, **_kwargs):
                calls.append(list(args))
                return SimpleNamespace(
                    stdout=json.dumps(
                        {
                            "paused": True,
                            "mode": "freeze",
                            "internal_mode": "hard",
                            "reason": "ummanu backup create",
                            "actor": "ummanu-backup",
                        }
                    ),
                    stderr="",
                )

            with mock.patch("ummanu.backup.subprocess.run", side_effect=fake_run):
                from ummanu.backup import _pipeline_action, _pipeline_status

                instance_file = instance / "instance.yaml"
                _pipeline_status(instance_file=instance_file, command=["ummanu"])
                _pipeline_action("pause", instance_file=instance_file, command=["ummanu"])
                _pipeline_action("resume", instance_file=instance_file, command=["ummanu"])

            self.assertEqual(
                calls[0],
                ["ummanu", "pause-status", "--instance", str(instance_file)],
            )
            self.assertEqual(
                calls[1],
                [
                    "ummanu",
                    "pause",
                    "freeze",
                    "--instance",
                    str(instance_file),
                    "--reason",
                    "ummanu backup create",
                    "--actor",
                    "ummanu-backup",
                ],
            )
            self.assertEqual(
                calls[2],
                ["ummanu", "resume", "--instance", str(instance_file), "--actor", "ummanu-backup"],
            )
            self.assertNotIn("--exclude-workspace", calls[1])

    def test_pipeline_status_reads_the_pause_protocol_document(self):
        """`pause-status` answers with the layer's `pause_state` document (secretary-1576)."""
        from ummanu.backup import _pause_summary

        self.assertEqual(
            _pause_summary(
                {
                    "kind": "pause_state",
                    "state": {
                        "paused": True,
                        "mode": "freeze",
                        "actor": "ummanu-backup",
                        "pause_reason": "ummanu backup create",
                    },
                }
            ),
            {
                "paused": True,
                "mode": "freeze",
                "actor": "ummanu-backup",
                "reason": "ummanu backup create",
            },
        )
        self.assertFalse(_pause_summary({"kind": "pause_state", "state": {"paused": False}})["paused"])

    def test_a_pause_state_nobody_could_read_counts_as_paused(self):
        """A backup owns the freeze it takes, so an unestablished pause state is not "free".

        The `state` section of the document is null in every field when the pause flag could not be
        read, and the production tick treats such a flag as a freeze. Reading that as "not paused"
        would have a backup freeze on top of a state it cannot see.
        """
        from ummanu.backup import _pause_summary

        for document in ({}, {"state": {"paused": None, "mode": None}}, {"state": None}):
            with self.subTest(document=document):
                self.assertTrue(_pause_summary(document)["paused"])

    def test_pipeline_pause_appends_exclude_workspace(self):
        calls: list[list[str]] = []

        def fake_run(args, **_kwargs):
            calls.append(list(args))
            return SimpleNamespace(stdout="{}", stderr="")

        with mock.patch("ummanu.backup.subprocess.run", side_effect=fake_run):
            from ummanu.backup import _pipeline_action

            _pipeline_action(
                "pause",
                instance_file=Path("/tmp/instance.yaml"),
                command=["ummanu"],
                exclude_workspace=Path("/ws/backup"),
            )

        self.assertEqual(
            calls[0],
            [
                "ummanu",
                "pause",
                "freeze",
                "--instance",
                "/tmp/instance.yaml",
                "--reason",
                "ummanu backup create",
                "--actor",
                "ummanu-backup",
                "--exclude-workspace",
                "/ws/backup",
            ],
        )

    def test_pipeline_pause_reports_dispatcher_without_flag(self):
        import subprocess

        def fake_run(args, **_kwargs):
            raise subprocess.CalledProcessError(
                returncode=2,
                cmd=args,
                stderr="ummanu pause: error: unrecognized arguments: --exclude-workspace /ws/backup\n",
            )

        with mock.patch("ummanu.backup.subprocess.run", side_effect=fake_run):
            from ummanu.backup import _pipeline_action

            with self.assertRaisesRegex(RuntimeError, "refusing to pause"):
                _pipeline_action(
                    "pause",
                    instance_file=Path("/tmp/instance.yaml"),
                    command=["ummanu"],
                    exclude_workspace=Path("/ws/backup"),
                )

    def test_create_claimed_worker_excludes_caller_workspace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            data_dir = root / "ummanu-data"
            _write_instance(instance, data_dir)
            calls: list[tuple[str, Path | None]] = []

            def fake_pipeline(action, *, exclude_workspace=None, **_kwargs):
                calls.append((action, exclude_workspace))

            with (
                mock.patch.dict(os.environ, {"BOARD_ROLE": "worker"}),
                mock.patch("ummanu.backup._pipeline_status", return_value={"paused": False}),
                mock.patch("ummanu.backup._pipeline_action", side_effect=fake_pipeline),
                mock.patch("ummanu.backup.export_all", side_effect=RuntimeError("stop")),
                self.assertRaisesRegex(RuntimeError, "stop"),
            ):
                create_backup(
                    instance,
                    allow_claimed_worker=True,
                    caller_workspace=Path("/ws/backup"),
                )

        self.assertEqual(calls[0], ("pause", Path("/ws/backup")))
        self.assertEqual(calls[1], ("resume", None))

    def test_create_claimed_worker_requires_caller_workspace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            data_dir = root / "ummanu-data"
            _write_instance(instance, data_dir)

            with self.assertRaisesRegex(RuntimeError, "caller_workspace"):
                create_backup(
                    instance,
                    allow_claimed_worker=True,
                )

    def test_create_rejects_preexisting_freeze(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            data_dir = root / "ummanu-data"
            _write_instance(instance, data_dir)

            with (
                mock.patch(
                    "ummanu.backup._pipeline_status",
                    return_value={
                        "paused": True,
                        "mode": "freeze",
                        "internal_mode": "hard",
                        "reason": "maintenance",
                        "actor": "steward",
                    },
                ),
                mock.patch(
                    "ummanu.backup._pipeline_action",
                    side_effect=AssertionError("pause action should not run"),
                ),
                self.assertRaisesRegex(RuntimeError, "already paused"),
            ):
                create_backup(instance)

    def test_create_releases_lock_when_preexisting_freeze_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            data_dir = root / "ummanu-data"
            _write_instance(instance, data_dir)

            with (
                mock.patch(
                    "ummanu.backup._pipeline_status",
                    return_value={
                        "paused": True,
                        "mode": "freeze",
                        "internal_mode": "hard",
                        "reason": "maintenance",
                        "actor": "steward",
                    },
                ),
                mock.patch("ummanu.backup._pipeline_action") as pipeline_action,
                self.assertRaisesRegex(RuntimeError, "already paused"),
            ):
                create_backup(instance)

            pipeline_action.assert_not_called()
            fd = os.open(data_dir / "backups" / ".create.lock", os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def test_create_rejects_preexisting_drain_pause(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            data_dir = root / "ummanu-data"
            _write_instance(instance, data_dir)

            with (
                mock.patch(
                    "ummanu.backup._pipeline_status",
                    return_value={"paused": True, "mode": "drain", "internal_mode": "soft"},
                ),
                mock.patch("ummanu.backup._pipeline_action") as pipeline_action,
                self.assertRaisesRegex(RuntimeError, "already paused"),
            ):
                create_backup(instance)

            pipeline_action.assert_not_called()

    def test_create_rejects_concurrent_create(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            data_dir = root / "ummanu-data"
            _write_instance(instance, data_dir)
            lock_path = data_dir / "backups" / ".create.lock"
            lock_path.parent.mkdir(parents=True)
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    create_backup(instance)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
            self.assertEqual(sorted(path.name for path in lock_path.parent.iterdir()), [".create.lock"])

    def test_create_publishes_archive_without_clobbering_existing_name(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            data_dir = root / "ummanu-data"
            _write_instance(instance, data_dir)
            backups = data_dir / "backups"
            backups.mkdir(parents=True)
            existing = backups / "ummanu-backup-full-20260710T000000Z.tar"
            existing.write_bytes(b"keep")


            with (
                mock.patch("ummanu.backup.datetime") as fake_datetime,
                mock.patch("ummanu.backup._pipeline_status", return_value={"paused": False}),
                mock.patch("ummanu.backup._pipeline_action", return_value=None),
                mock.patch(
                    "ummanu.backup.export_all",
                    side_effect=lambda data_dir_arg, _instance_dir, **_kwargs: _fake_exports(data_dir_arg),
                ),
            ):
                from datetime import UTC, datetime

                fake_datetime.now.return_value = datetime(2026, 7, 10, tzinfo=UTC)
                fake_datetime.strptime.side_effect = datetime.strptime
                fake_datetime.fromtimestamp.side_effect = datetime.fromtimestamp
                result = create_backup(
                    instance,
                )

            self.assertEqual(existing.read_bytes(), b"keep")
            self.assertNotEqual(result.archive, existing)
            self.assertEqual(result.archive.name, "ummanu-backup-full-20260710T000000Z-2.tar")
            self.assertTrue(result.archive.is_file())

    def test_create_both_uses_one_pause_and_writes_core_and_full_archives(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            data_dir = root / "ummanu-data"
            _write_instance(instance, data_dir)
            pipeline_calls: list[str] = []

            def fake_pipeline(action, **_kwargs):
                pipeline_calls.append(action)


            with (
                mock.patch("ummanu.backup._pipeline_status", return_value={"paused": False}),
                mock.patch("ummanu.backup._pipeline_action", side_effect=fake_pipeline),
                mock.patch(
                    "ummanu.backup.export_all",
                    side_effect=lambda data_dir_arg, _instance_dir, **_kwargs: _fake_exports(data_dir_arg),
                ),
            ):
                results = create_backups(
                    instance,
                    backup_kinds=("core", "full"),
                )

            self.assertEqual(pipeline_calls, ["pause", "resume"])
            self.assertEqual([result.manifest["backup_kind"] for result in results], ["core", "full"])
            for result in results:
                verified = verify_backup(
                    result.archive,
                )
                self.assertEqual(verified.code, 0, verified.findings)

            core = results[0].archive
            full = results[1].archive
            with tarfile.open(core, "r") as archive:
                core_names = set(archive.getnames())
            with tarfile.open(full, "r") as archive:
                full_names = set(archive.getnames())
            # One engine dump serves the pass, and only the full archive carries it.
            self.engine_dump.assert_called_once()
            self.assertNotIn("ummanu-backup/engine/postgres.dump", core_names)
            self.assertIn("ummanu-backup/engine/postgres.dump", full_names)
            self.assertNotIn("ummanu-backup/ummanu-data/runs/runs.ndjson", core_names)
            self.assertIn("ummanu-backup/ummanu-data/runs/runs.ndjson", full_names)

    def test_failed_create_does_not_leave_zero_length_final_archive(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            data_dir = root / "ummanu-data"
            _write_instance(instance, data_dir)

            with (
                mock.patch("ummanu.backup._pipeline_status", return_value={"paused": False}),
                mock.patch("ummanu.backup._pipeline_action", side_effect=RuntimeError("pause failed")),
                self.assertRaisesRegex(RuntimeError, "pause failed"),
            ):
                create_backup(instance)

            self.assertEqual(list((data_dir / "backups").glob("*.tar")), [])

    def test_core_filters_done_cards(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            data_dir = root / "ummanu-data"
            _write_instance(instance, data_dir)


            with (
                mock.patch("ummanu.backup._pipeline_status", return_value={"paused": False}),
                mock.patch("ummanu.backup._pipeline_action", return_value=None),
                mock.patch(
                    "ummanu.backup.export_all",
                    side_effect=lambda data_dir_arg, _instance_dir, **_kwargs: _fake_exports(
                        data_dir_arg,
                        include_done=True,
                    ),
                ),
            ):
                result = create_backup(
                    instance,
                    backup_kind="core",
                )

            with tarfile.open(result.archive, "r") as archive:
                names = archive.getnames()
                cards = json.loads(
                    archive.extractfile("ummanu-backup/ummanu-data/board/cards.json")
                    .read()
                    .decode("utf-8")
                )
                manifest = json.loads(
                    archive.extractfile("ummanu-backup/versions.json").read().decode("utf-8")
                )
            self.assertEqual([card["reference"] for card in cards["cards"]], ["active-1"])
            self.assertEqual(manifest["components"]["board"]["count"], 1)
            self.assertIn("ummanu-backup/ummanu-data/board/audit.json", names)
            self.assertIn("ummanu-backup/ummanu-data/board/audit.ndjson", names)

    def test_retention_keeps_one_core_and_removes_old_full(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            data_dir = root / "ummanu-data"
            _write_instance(instance, data_dir)
            backups = data_dir / "backups"
            backups.mkdir(parents=True)
            old_core = backups / "ummanu-backup-core-20260708T000000Z.tar"
            old_full = backups / "ummanu-backup-full-20260708T000000Z.tar"
            recent_full = backups / "ummanu-backup-full-20260710T230000Z.tar"
            for path in (old_core, old_full, recent_full):
                path.write_bytes(b"old")
            fresh_mtime = 2_000_000_000
            os.utime(old_core, (fresh_mtime, fresh_mtime))
            os.utime(old_full, (fresh_mtime, fresh_mtime))


            with (
                mock.patch("ummanu.backup.datetime") as fake_datetime,
                mock.patch("ummanu.backup._pipeline_status", return_value={"paused": False}),
                mock.patch("ummanu.backup._pipeline_action", return_value=None),
                mock.patch(
                    "ummanu.backup.export_all",
                    side_effect=lambda data_dir_arg, _instance_dir, **_kwargs: _fake_exports(data_dir_arg),
                ),
            ):
                fake_datetime.now.return_value = datetime(2026, 7, 11, tzinfo=UTC)
                fake_datetime.strptime.side_effect = datetime.strptime
                fake_datetime.fromtimestamp.side_effect = datetime.fromtimestamp
                result = create_backup(
                    instance,
                    backup_kind="core",
                )

            self.assertTrue(result.archive.exists())
            self.assertFalse(old_core.exists())
            self.assertFalse(old_full.exists())
            self.assertTrue(recent_full.exists())

    def test_verify_reports_missing_runs_state_sidecars(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            archive = root / "core.tar"
            payload = root / "payload" / "ummanu-backup"
            _write_core_payload(payload)
            (payload / "ummanu-data" / "runs" / "claims.json").unlink()
            with tarfile.open(archive, "w") as tar:
                tar.add(payload, arcname="ummanu-backup")

            result = verify_backup(
                archive,
            )

        self.assertEqual(result.code, 1)
        self.assertTrue(
            any("runs_state component path missing from archive: claims" in item for item in result.findings)
        )

    def test_verify_reports_missing_normalized_board_export_sidecars(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            archive = root / "core.tar"
            payload = root / "payload" / "ummanu-backup"
            _write_core_payload(payload)
            (payload / "ummanu-data" / "board" / "cards.ndjson").unlink()
            (payload / "ummanu-data" / "board" / "export.json").unlink()
            with tarfile.open(archive, "w") as tar:
                tar.add(payload, arcname="ummanu-backup")

            result = verify_backup(
                archive,
            )

        self.assertEqual(result.code, 1)
        self.assertIn(
            "missing required archive entry: ummanu-backup/ummanu-data/board/cards.ndjson",
            result.findings,
        )
        self.assertIn(
            "missing required archive entry: ummanu-backup/ummanu-data/board/export.json",
            result.findings,
        )

    def test_verify_rejects_archive_without_the_memory_component(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            archive = root / "core.tar"
            payload = root / "payload" / "ummanu-backup"
            _write_core_payload(payload)
            # Facts are canon in the private repo, so the archive's whole memory
            # component is the derived export. Without it there is no memory.
            (payload / "ummanu-data" / "memory" / "export.ndjson").unlink()
            with tarfile.open(archive, "w") as tar:
                tar.add(payload, arcname="ummanu-backup")

            result = verify_backup(
                archive,
            )

        self.assertEqual(result.code, 1)
        self.assertIn(
            "missing required archive entry: ummanu-backup/ummanu-data/memory/export.ndjson",
            result.findings,
        )

    def test_verify_reports_unsupported_non_string_backup_kind(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            archive = root / "invalid-kind.tar"
            payload = root / "payload" / "ummanu-backup"
            _write_complete_payload(payload)
            manifest_path = payload / "versions.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["backup_kind"] = []
            manifest_path.write_text(
                json.dumps(manifest, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            with tarfile.open(archive, "w") as tar:
                tar.add(payload, arcname="ummanu-backup")

            result = verify_backup(
                archive,
            )

        self.assertEqual(result.code, 1)
        self.assertIn("unsupported backup kind", result.findings)

    def test_verify_returns_2_when_archive_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            missing = verify_backup(root / "missing.tar")
            archive = root / "archive.tar"
            archive.write_bytes(b"not checked")
            invalid = verify_backup(archive)

        self.assertEqual(missing.code, 2)
        self.assertIn("archive not found", missing.findings[0])
        self.assertEqual(invalid.code, 1)
        self.assertIn("invalid archive", invalid.findings[0])

    def test_verify_returns_1_for_incomplete_plain_archive(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            archive = root / "broken.tar"
            payload = root / "payload"
            (payload / "ummanu-backup").mkdir(parents=True)
            with tarfile.open(archive, "w") as tar:
                tar.add(payload / "ummanu-backup", arcname="ummanu-backup")

            result = verify_backup(
                archive,
            )

        self.assertEqual(result.code, 1)
        self.assertTrue(any("missing required archive entry" in item for item in result.findings))

    def test_verify_returns_1_when_component_paths_are_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            archive = root / "incomplete.tar"
            payload = root / "payload" / "ummanu-backup"
            (payload / "instance").mkdir(parents=True)
            (payload / "ummanu-data" / "board").mkdir(parents=True)
            (payload / "ummanu-data" / "memory").mkdir(parents=True)
            (payload / "ummanu-data" / "runs").mkdir(parents=True)
            (payload / "ummanu-data" / "transcripts").mkdir(parents=True)
            (payload / "debug" / "orca-state").mkdir(parents=True)
            (payload / "instance" / "instance.yaml").write_text("version: 1\n", encoding="utf-8")
            (payload / "ummanu-data" / "data-manifest.json").write_text("{}", encoding="utf-8")
            (payload / "ummanu-data" / "board" / "cards.json").write_text("{}", encoding="utf-8")
            (payload / "ummanu-data" / "memory" / "export.ndjson").write_text("", encoding="utf-8")
            (payload / "ummanu-data" / "runs" / "watermarks.json").write_text("{}", encoding="utf-8")
            (payload / "ummanu-data" / "transcripts" / "inventory.json").write_text(
                "{}",
                encoding="utf-8",
            )
            (payload / "debug" / "orca-state" / "inventory.json").write_text("{}", encoding="utf-8")
            (payload / "versions.json").write_text(
                json.dumps(
                    {
                        "version": 2,
                        "board_backend": "postgres",
                        "components": {
                            "board": {"path": "board/cards.json"},
                            "memory": {"path": "memory/export.ndjson"},
                            "runs": {"path": "runs/runs.ndjson"},
                            "transcripts": {"path": "transcripts/inventory.json"},
                            "artifacts": {"path": "artifacts/inventory.json"},
                            "debug_orca_state": {"path": "debug/orca-state/inventory.json"},
                        },
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            with tarfile.open(archive, "w") as tar:
                tar.add(payload, arcname="ummanu-backup")

            result = verify_backup(
                archive,
            )

        self.assertEqual(result.code, 1)
        self.assertTrue(any("runs/runs.ndjson" in item for item in result.findings))
        self.assertTrue(any("artifacts/inventory.json" in item for item in result.findings))

    def test_verify_refuses_a_version_1_manifest_as_an_unsupported_version(self):
        for version in (1, 3, None):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                archive = root / "old.tar"
                payload = root / "payload" / "ummanu-backup"
                _write_complete_payload(payload)
                manifest_path = payload / "versions.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["version"] = version
                manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
                with tarfile.open(archive, "w") as tar:
                    tar.add(payload, arcname="ummanu-backup")

                result = verify_backup(archive)

            # No reading path for another format: the version is the whole answer.
            self.assertEqual(result.code, 1)
            self.assertEqual(
                result.findings, [f"unsupported backup version: {version!r}; this build reads version 2"]
            )

    def test_verify_rejects_an_engine_dump_that_is_not_custom_format(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            archive = root / "plain-sql.tar"
            payload = root / "payload" / "ummanu-backup"
            _write_complete_payload(payload)
            (payload / "engine" / "postgres.dump").write_bytes(b"-- plain SQL dump\n")
            with tarfile.open(archive, "w") as tar:
                tar.add(payload, arcname="ummanu-backup")

            result = verify_backup(archive)

        self.assertEqual(result.code, 1)
        self.assertIn("PostgreSQL dump is not in pg_restore custom format", result.findings)

    def test_verify_returns_1_when_transcript_payload_copies_are_present(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            archive = root / "with-transcript-copies.tar"
            payload = root / "payload" / "ummanu-backup"
            _write_complete_payload(payload)
            copy_path = payload / "ummanu-data" / "transcripts" / "copies" / "session.jsonl"
            copy_path.parent.mkdir(parents=True)
            copy_path.write_text("{}\n", encoding="utf-8")
            with tarfile.open(archive, "w") as tar:
                tar.add(payload, arcname="ummanu-backup")

            result = verify_backup(
                archive,
            )

        self.assertEqual(result.code, 1)
        self.assertTrue(any("unexpected transcript payload copy" in item for item in result.findings))

    def test_git_commit_uses_product_repo_root(self):
        from ummanu.backup import _git_commit

        calls = []

        def fake_run(*args, **kwargs):
            calls.append((args, kwargs))
            return SimpleNamespace(stdout="abc123\n", stderr="")

        with mock.patch("ummanu.backup.subprocess.run", side_effect=fake_run):
            self.assertEqual(_git_commit(Path("/product")), "abc123")

        self.assertEqual(calls[0][1]["cwd"], Path("/product"))


class ShouldSkipDataEntryTests(unittest.TestCase):
    def test_data_manifest_is_admitted_only_at_the_top_level(self):
        for policy in POLICIES.values():
            with self.subTest(kind=policy.kind):
                self.assertFalse(should_skip_data_entry(Path("data-manifest.json"), policy=policy))
                for smuggled in (
                    "evil/data-manifest.json",
                    "evil/deep/nested/data-manifest.json",
                ):
                    self.assertTrue(
                        should_skip_data_entry(Path(smuggled), policy=policy),
                        smuggled,
                    )

    def test_allowed_roots_keep_their_outcomes(self):
        admitted = {
            "core": (
                "board/cards.json",
                "board/data-manifest.json",
                "memory/export.ndjson",
                "runs/watermarks.json",
                "po-queue/1-0.json",
                "po-queue/refused/1-0.json",
            ),
            "full": (
                "board/cards.json",
                "board/data-manifest.json",
                "memory/export.ndjson",
                "runs/runs.ndjson",
                "transcripts/inventory.json",
                "artifacts/inventory.json",
                "artifacts/data-manifest.json",
                "po-queue/1-0.json",
                "po-queue/refused/1-0.json",
            ),
        }
        for kind, relatives in admitted.items():
            policy = POLICIES[kind]
            for relative in relatives:
                with self.subTest(kind=kind, relative=relative):
                    self.assertFalse(should_skip_data_entry(Path(relative), policy=policy))

    def test_full_archives_leave_out_only_the_memory_model_cache(self):
        policy = POLICIES["full"]
        for relative in (
            "memory/fastembed-cache",
            "memory/fastembed-cache/models--bge-m3/snapshots/model.onnx",
        ):
            with self.subTest(relative=relative):
                self.assertTrue(should_skip_data_entry(Path(relative), policy=policy))
        for relative in (
            "memory/export.ndjson",
            "memory/fastembed-cache-notes/readme.md",
            "memory/facts/global/one.md",
        ):
            with self.subTest(relative=relative):
                self.assertFalse(should_skip_data_entry(Path(relative), policy=policy))


def _write_instance(instance: Path, data_dir: Path) -> None:
    instance.mkdir()
    (instance / ".env").write_text("TOKEN=do-not-copy\n", encoding="utf-8")
    (instance / "instance.yaml").write_text(
        "version: 1\n"
        "name: test\n"
        f"data_dir: {data_dir}\n"
        "offsite:\n"
        "  instance_remote: git@example.invalid:test/secretary-instance.git\n",
        encoding="utf-8",
    )


def _write_export_surface(data_dir: Path, *, include_done: bool = False) -> None:
    cards = [{"reference": "active-1", "column": "Ready"}]
    if include_done:
        cards.append({"reference": "done-1", "column": "Done"})
    (data_dir / "board").mkdir(parents=True, exist_ok=True)
    (data_dir / "board" / "cards.json").write_text(
        json.dumps({"version": 1, "cards": cards}) + "\n",
        encoding="utf-8",
    )
    (data_dir / "board" / "cards.ndjson").write_text(
        "".join(json.dumps(card) + "\n" for card in cards),
        encoding="utf-8",
    )
    (data_dir / "board" / "export.json").write_text('{"version":1}\n', encoding="utf-8")
    (data_dir / "board" / "audit.json").write_text(
        '{"version":1,"events":[]}\n', encoding="utf-8"
    )
    (data_dir / "board" / "audit.ndjson").write_text("", encoding="utf-8")
    (data_dir / "memory").mkdir(parents=True, exist_ok=True)
    (data_dir / "memory" / "export.ndjson").write_text("{}\n", encoding="utf-8")
    (data_dir / "memory" / "index.sqlite").write_bytes(b"index")
    (data_dir / "runs").mkdir(parents=True, exist_ok=True)
    (data_dir / "runs" / "runs.ndjson").write_text("{}\n", encoding="utf-8")
    (data_dir / "runs" / "watermarks.json").write_text('{"version":1,"files":[]}\n', encoding="utf-8")
    (data_dir / "runs" / "cards.json").write_text('{"version":1,"cards":{}}\n', encoding="utf-8")
    (data_dir / "runs" / "claims.json").write_text('{"version":1,"claims":{}}\n', encoding="utf-8")
    (data_dir / "transcripts").mkdir(parents=True, exist_ok=True)
    (data_dir / "transcripts" / "inventory.json").write_text(
        '{"version":1,"transcripts":[]}\n',
        encoding="utf-8",
    )
    (data_dir / "artifacts").mkdir(parents=True, exist_ok=True)
    (data_dir / "artifacts" / "inventory.json").write_text(
        '{"version":1,"artifacts":[]}\n',
        encoding="utf-8",
    )
    (data_dir / "backups").mkdir(parents=True, exist_ok=True)
    (data_dir / "backups" / "old.tar").write_bytes(b"old")


def _write_complete_payload(payload: Path) -> None:
    (payload / "instance").mkdir(parents=True)
    (payload / "ummanu-data" / "board").mkdir(parents=True)
    (payload / "ummanu-data" / "memory").mkdir(parents=True)
    (payload / "ummanu-data" / "runs").mkdir(parents=True)
    (payload / "ummanu-data" / "transcripts").mkdir(parents=True)
    (payload / "ummanu-data" / "artifacts").mkdir(parents=True)
    (payload / "debug" / "orca-state").mkdir(parents=True)
    (payload / "engine").mkdir(parents=True)
    (payload / "engine" / "postgres.dump").write_bytes(ENGINE_DUMP_BYTES)
    (payload / "instance" / "instance.yaml").write_text("version: 1\n", encoding="utf-8")
    (payload / "ummanu-data" / "data-manifest.json").write_text("{}", encoding="utf-8")
    (payload / "ummanu-data" / "board" / "cards.json").write_text("{}", encoding="utf-8")
    (payload / "ummanu-data" / "board" / "cards.ndjson").write_text("", encoding="utf-8")
    (payload / "ummanu-data" / "board" / "export.json").write_text("{}", encoding="utf-8")
    (payload / "ummanu-data" / "board" / "audit.json").write_text('{"events":[]}', encoding="utf-8")
    (payload / "ummanu-data" / "board" / "audit.ndjson").write_text("", encoding="utf-8")
    (payload / "ummanu-data" / "memory" / "export.ndjson").write_text("{}\n", encoding="utf-8")
    (payload / "ummanu-data" / "runs" / "runs.ndjson").write_text("{}\n", encoding="utf-8")
    (payload / "ummanu-data" / "runs" / "watermarks.json").write_text("{}", encoding="utf-8")
    (payload / "ummanu-data" / "runs" / "cards.json").write_text("{}", encoding="utf-8")
    (payload / "ummanu-data" / "runs" / "claims.json").write_text("{}", encoding="utf-8")
    (payload / "ummanu-data" / "transcripts" / "inventory.json").write_text(
        "{}",
        encoding="utf-8",
    )
    (payload / "ummanu-data" / "artifacts" / "inventory.json").write_text(
        "{}",
        encoding="utf-8",
    )
    (payload / "debug" / "orca-state" / "inventory.json").write_text("{}", encoding="utf-8")
    (payload / "versions.json").write_text(
        json.dumps(
            {
                "version": 2,
                "board_backend": "postgres",
                "components": {
                    "postgres_dump": engine_dump_component(),
                    "board": {"path": "board/cards.json"},
                    "memory": {"path": "memory/export.ndjson"},
                    "runs": {"path": "runs/runs.ndjson"},
                    "transcripts": {"path": "transcripts/inventory.json"},
                    "artifacts": {"path": "artifacts/inventory.json"},
                    "debug_orca_state": {"path": "debug/orca-state/inventory.json"},
                    "board_history": {"path": "board/audit.json"},
                },
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _write_core_payload(payload: Path) -> None:
    (payload / "instance").mkdir(parents=True)
    (payload / "ummanu-data" / "board").mkdir(parents=True)
    (payload / "ummanu-data" / "memory").mkdir(parents=True)
    (payload / "ummanu-data" / "runs").mkdir(parents=True)
    (payload / "instance" / "instance.yaml").write_text("version: 1\n", encoding="utf-8")
    (payload / "ummanu-data" / "data-manifest.json").write_text("{}", encoding="utf-8")
    (payload / "ummanu-data" / "board" / "cards.json").write_text(
        '{"version":1,"cards":[]}\n',
        encoding="utf-8",
    )
    (payload / "ummanu-data" / "board" / "cards.ndjson").write_text("", encoding="utf-8")
    (payload / "ummanu-data" / "board" / "export.json").write_text("{}", encoding="utf-8")
    (payload / "ummanu-data" / "board" / "audit.json").write_text('{"events":[]}', encoding="utf-8")
    (payload / "ummanu-data" / "board" / "audit.ndjson").write_text("", encoding="utf-8")
    (payload / "ummanu-data" / "memory" / "export.ndjson").write_text("{}\n", encoding="utf-8")
    (payload / "ummanu-data" / "runs" / "watermarks.json").write_text("{}", encoding="utf-8")
    (payload / "ummanu-data" / "runs" / "cards.json").write_text("{}", encoding="utf-8")
    (payload / "ummanu-data" / "runs" / "claims.json").write_text("{}", encoding="utf-8")
    (payload / "versions.json").write_text(
        json.dumps(
            {
                "version": 2,
                "backup_kind": "core",
                "board_backend": "postgres",
                "components": {
                    "board_history": {"path": "board/audit.json"},
                    "board": {"path": "board/cards.json"},
                    "memory": {"path": "memory/export.ndjson"},
                    "runs_state": {
                        "path": "runs/watermarks.json",
                        "cards": "runs/cards.json",
                        "claims": "runs/claims.json",
                    },
                },
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


class BackupMemoryCanonTests(unittest.TestCase):
    """`backup create` archives the live facts, never the seed source.

    Contract: docs/RECOVERY.md, "Layout". The memory component of an archive is
    just `memory/export.ndjson` now that the facts live in the private repo, so
    an export built from the seed would put somebody else's memory in the
    archive and in the live data dir the reindex reads.
    """

    def setUp(self):
        self.env_patch = mock.patch.dict(os.environ, {"BOARD_ROLE": ""})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.workspace_patch = mock.patch("ummanu.backup._claimed_workspace_from_cwd", return_value=None)
        self.workspace_patch.start()
        self.addCleanup(self.workspace_patch.stop)
        self.engine_dump = self.enterContext(fake_engine_dump())

    def test_create_exports_facts_from_the_private_repo(self):
        from ummanu import state_repo

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            data_dir = root / "ummanu-data"
            _write_instance(instance, data_dir)
            _write_export_surface(data_dir)
            for command in (
                ["git", "init", "--quiet", "--initial-branch", "main"],
                ["git", "config", "user.name", "operator"],
                ["git", "config", "user.email", "operator@example.invalid"],
            ):
                subprocess.run(command, cwd=instance, check=True)
            facts = state_repo.memory_facts_dir(instance)
            (facts / "global").mkdir(parents=True)
            (facts / "global" / "live.md").write_text("live fact from the private repo\n", encoding="utf-8")
            subprocess.run(["git", "add", "-A", "."], cwd=instance, check=True)
            subprocess.run(["git", "commit", "--quiet", "-m", "facts"], cwd=instance, check=True)


            def stub(name):
                return lambda data_dir_arg, **_kwargs: DataExport(data_dir_arg / name, 1, name)

            with (
                mock.patch("ummanu.backup._pipeline_status", return_value={"paused": False}),
                mock.patch("ummanu.backup._pipeline_action", return_value=None),
                mock.patch("ummanu.data.export_board", side_effect=stub("board")),
                mock.patch("ummanu.data.export_runs", side_effect=stub("runs")),
                mock.patch("ummanu.data.export_transcripts", side_effect=stub("transcripts")),
                mock.patch("ummanu.data.export_artifacts", side_effect=stub("artifacts")),
            ):
                result = create_backup(instance, backup_kind="core")

            with tarfile.open(result.archive, "r") as archive:
                member = archive.extractfile("ummanu-backup/ummanu-data/memory/export.ndjson")
                exported = member.read().decode("utf-8")

            self.assertIn("live fact from the private repo", exported)
            # The export is published into the live data dir too; the reindex
            # and memory-mcp read it from there.
            self.assertIn(
                "live fact from the private repo",
                (data_dir / "memory" / "export.ndjson").read_text(encoding="utf-8"),
            )


def _fake_exports(data_dir: Path, *, include_done: bool = False) -> dict[str, DataExport]:
    _write_export_surface(data_dir, include_done=include_done)
    return {
        "board": DataExport(data_dir / "board" / "cards.json", 1, "board"),
        "memory": DataExport(data_dir / "memory" / "export.ndjson", 1, "memory"),
        "runs": DataExport(data_dir / "runs" / "runs.ndjson", 1, "runs"),
        "transcripts": DataExport(data_dir / "transcripts" / "inventory.json", 1, "transcripts"),
        "artifacts": DataExport(data_dir / "artifacts" / "inventory.json", 1, "artifacts"),
    }


class PostgresBackupPolicyTests(unittest.TestCase):
    def setUp(self):
        self.env_patch = mock.patch.dict(os.environ, {"BOARD_ROLE": ""})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def test_full_archive_carries_the_engine_dump_and_excludes_store_credentials(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            data_dir = root / "ummanu-data"
            _write_instance(instance, data_dir)
            secret = "not-in-the-archive"
            (instance / "board-store.env").write_text(secret, encoding="utf-8")
            (instance / "board-store.env").chmod(0o600)

            def exports(path, *_args, **_kwargs):
                result = _fake_exports(path)
                (path / "board" / "audit.json").write_text(
                    '{"version":1,"events":[]}\n', encoding="utf-8"
                )
                (path / "board" / "audit.ndjson").write_text("", encoding="utf-8")
                (path / "memory" / "fastembed-cache").mkdir(exist_ok=True)
                (path / "memory" / "fastembed-cache" / "model.onnx").write_bytes(b"onnx")
                return result

            metadata = {
                "engine": "postgresql", "format": "custom", "dump_version": 1,
                "image": "postgres:16", "server_major": 16,
                "source_schema": "0007_card_transport_key",
                "alembic_head": "0007_card_transport_key",
                "restore_purpose": "local recovery", "source_endpoint_id": "a" * 64,
                "table_counts": {"tasks": 1},
            }

            def dump(_config, destination, details):
                destination.write_bytes(b"PGDMPtest")
                return {**details, "tool_version": "(PostgreSQL) 16.10", "bytes": 9}

            with (
                mock.patch("ummanu.backup._claimed_workspace_from_cwd", return_value=None),
                mock.patch("ummanu.backup._pipeline_status", return_value={"paused": False}),
                mock.patch("ummanu.backup._pipeline_action", return_value=None),
                mock.patch("ummanu.backup.export_all", side_effect=exports),
                mock.patch(
                    "ummanu.board.postgres_recovery.inspect_source",
                    return_value=(object(), metadata),
                ),
                mock.patch("ummanu.board.postgres_recovery.create_dump", side_effect=dump),
            ):
                result = create_backup(instance)

            verified = verify_backup(result.archive)
            self.assertEqual(verified.code, 0, verified.findings)
            self.assertEqual(result.manifest["version"], 2)
            self.assertEqual(result.manifest["board_backend"], "postgres")
            self.assertIn("postgres_dump", result.manifest["components"])
            with tarfile.open(result.archive) as archive:
                names = archive.getnames()
                body = b"".join(
                    archive.extractfile(name).read()
                    for name in names
                    if archive.getmember(name).isfile()
                )
            self.assertNotIn("board-store.env", names)
            self.assertNotIn(secret.encode(), body)
            self.assertIn("ummanu-backup/ummanu-data/memory/export.ndjson", names)
            self.assertEqual([name for name in names if "memory/fastembed-cache" in name], [])

    def test_unusable_postgres_fails_before_pause_or_archive(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            data_dir = root / "ummanu-data"
            _write_instance(instance, data_dir)
            previous = data_dir / "backups" / "previous.tar"
            previous.parent.mkdir(parents=True)
            previous.write_bytes(b"previous")
            with (
                mock.patch("ummanu.backup._claimed_workspace_from_cwd", return_value=None),
                mock.patch(
                    "ummanu.board.postgres_recovery.inspect_source",
                    side_effect=__import__(
                        "ummanu.board.postgres_recovery", fromlist=["PostgresRecoveryError"]
                    ).PostgresRecoveryError("unreachable"),
                ),
                mock.patch("ummanu.backup._pipeline_action") as pipeline,
                self.assertRaisesRegex(RuntimeError, "unreachable"),
            ):
                create_backup(instance)
            pipeline.assert_not_called()
            self.assertEqual(previous.read_bytes(), b"previous")


if __name__ == "__main__":
    unittest.main()
