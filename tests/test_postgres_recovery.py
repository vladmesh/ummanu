"""End-to-end PostgreSQL archive recovery on isolated Compose projects."""

from __future__ import annotations

import json
import os
import socket
import tarfile
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from tests.container_cleanup import cleanup_test_project
from ummanu.backup import create_backups
from ummanu.backup_verify import verify_backup
from ummanu.board import migrate, provision, schema
from ummanu.board.postgres_recovery import (
    PostgresRecoveryError,
    restore_dump,
)
from ummanu.board.sql_cards import SqlCardClient
from ummanu.board.store import BoardStoreConfig, BoardStoreError
from ummanu.data import DataExport, export_board, init_layout
from ummanu.restore import restore_postgres_backup
from ummanu.sprint_observer import none_choice
from ummanu.sprints import SprintWriter, sprint_client
from ummanu.tasks import TaskWriter


class PostgresRecoveryFailureTests(unittest.TestCase):
    def test_migration_failure_is_not_masked_by_the_local_psycopg_handler(self) -> None:
        config = BoardStoreConfig(
            host="127.0.0.1",
            port=6543,
            dbname="target",
            owner_user="owner",
            owner_password="owner-secret",
            app_user="app",
            app_password="app-secret",
            read_user="reader",
            read_password="read-secret",
        )
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            mock.patch("ummanu.board.postgres_recovery.resolve", return_value=config),
            mock.patch(
                "ummanu.board.postgres_recovery.migrate.migrate_instance",
                side_effect=BoardStoreError("migration failed before preflight"),
            ),
            self.assertRaisesRegex(
                PostgresRecoveryError,
                "PostgreSQL restore target is not usable: migration failed before preflight",
            ),
        ):
            restore_dump(
                Path(tmpdir) / "postgres.dump",
                Path(tmpdir),
                {"source_endpoint_id": "different"},
            )


class PostgresRecoveryIntegrationTests(unittest.TestCase):
    projects: list[tuple[Path, str, bool]]

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.projects = []
        self.addCleanup(self._cleanup_projects)
        self.source_instance, self.source_config = self._store("source")
        self.target_instance, self.target_config = self._store("target")
        self.environment = mock.patch.dict(os.environ, {"BOARD_ROLE": ""})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def _store(self, name: str) -> tuple[Path, BoardStoreConfig]:
        instance = self.root / name
        data_dir = self.root / f"{name}-data"
        instance.mkdir()
        (instance / "instance.yaml").write_text(
            "version: 1\nname: recovery-test\n"
            f"data_dir: {data_dir}\noffsite:\n"
            "  instance_remote: git@example.invalid:test/recovery.git\n",
            encoding="utf-8",
        )
        (instance / "projects").mkdir()
        repository = self.root / "repository"
        repository.mkdir(exist_ok=True)
        (instance / "projects" / "ummanu.yaml").write_text(
            f"id: ummanu\nrepo: {repository}\nenabled: false\nadapter: ummanu\ndefault_branch: main\n",
            encoding="utf-8",
        )
        (instance / "adapters").mkdir()
        (instance / "adapters" / "ummanu.yaml").write_text(
            "setup:\n  commands: ['true']\nsmoke:\n  command: 'true'\n"
            "validation:\n  ci: local\n  command: 'true'\n"
            "artifact_policy:\n  write_project_files: false\n",
            encoding="utf-8",
        )
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = int(listener.getsockname()[1])
        config = BoardStoreConfig(
            host="127.0.0.1",
            port=port,
            dbname="ummanu",
            owner_user="ummanu_owner",
            owner_password=f"{name}-owner-secret",
            app_user=schema.APP_ROLE,
            app_password=f"{name}-app-secret",
            read_user=schema.READ_ROLE,
            read_password=f"{name}-read-secret",
        )
        path = instance / "board-store.env"
        path.write_text(
            "".join(f"{key}={value}\n" for key, value in config.as_environ().items()),
            encoding="utf-8",
        )
        path.chmod(0o600)
        compose = instance / "postgres-compose.yml"
        project = f"ummanu-recovery-{name}-{uuid.uuid4().hex}"
        self.projects.append((instance, project, False))
        provision.provision(instance, compose_path=compose, project=project,
                            test_owner_pid=os.getpid())
        self.projects[-1] = (instance, project, True)
        migrate.migrate_instance(instance)
        provision.verify_roles(instance)
        return instance, config

    def _cleanup_projects(self) -> None:
        errors = []
        for _instance, project, completed in reversed(self.projects):
            try:
                cleanup_test_project(project, container_expected=completed)
            except RuntimeError as exc:
                errors.append(f"{project}: {exc}")
        if errors:
            raise RuntimeError("test Compose cleanup refused: " + "; ".join(errors))

    def _seed(self) -> None:
        data_dir = self.root / "source-data"
        init_layout(data_dir)
        client = SqlCardClient(self.source_config.for_role("owner"), self.source_instance)
        with client.transaction():
            product_key = client.call(
                "createTask", project_id=1, title="Ummanu", reference="product:ummanu"
            )
            client.call(
                "saveTaskMetadata",
                task_id=product_key,
                values={
                    "record_type": "product",
                    "product_id": "ummanu",
                    "product_projects": '["ummanu"]',
                    "future_product_key": "opaque",
                },
            )
            issue_key = client.call("createTask", project_id=1, title="Recovery", reference="issue:recovery")
            client.call(
                "saveTaskMetadata",
                task_id=issue_key,
                values={
                    "record_type": "issue",
                    "issue_product": "ummanu",
                    "issue_kind": "feature",
                    "issue_priority": "P1",
                },
            )
        writer = TaskWriter(client, data_dir=data_dir)
        sprint_board = sprint_client(self.source_instance)
        self.addCleanup(sprint_board.close)
        sprint_writer = SprintWriter(sprint_board, data_dir=data_dir, instance=self.source_instance)
        sprint_ref = sprint_writer.create(
            role="po",
            actor="test",
            goal="prove recovery",
            repositories=[str(self.root / "repository")],
            product="ummanu",
            issues=["issue:recovery"],
            projects=["ummanu"],
            observer=none_choice(),
            reference="sprint:recovery-custom",
            request_id="create-nullable-number-sprint",
        )["sprint"]["ref"]
        first = writer.create(
            role="po",
            actor="test",
            project="ummanu",
            task_type="code",
            title="Recover me",
            target="ready",
            reference="ummanu-1",
            sprint=sprint_ref,
            sprint_override=True,
            sprint_override_reason="integration fixture",
            request_id="create-recovery-one",
        )["task"]
        second = writer.create(
            role="po",
            actor="test",
            project="ummanu",
            task_type="code",
            title="Dependent",
            target="ready",
            reference="ummanu-2",
            sprint=sprint_ref,
            blocked_by="ummanu-1",
            request_id="create-recovery-two",
            seed_ref="a" * 40,
            supersedes="ummanu-1",
            sprint_override=True,
            sprint_override_reason="integration fixture",
        )["task"]
        client.call(
            "saveTaskMetadata",
            task_id=int(str(first["id"]).rsplit("_", 1)[1]),
            values={"future_task_key": "opaque", "issues": "issue:recovery"},
        )
        self.assertEqual(second["workspace"]["supersedes"], "ummanu-1")
        with client.transaction():
            client._execute(
                "INSERT INTO projects (project_id, enabled, registry_present) VALUES "
                "('butler', true, true), ('codegen-product-kit', true, true)"
            )
        butler_key = client.call(
            "createTask", project_id=1, title="Butler collision", reference="butler-1", column_id=2
        )
        kit_key = client.call(
            "createTask",
            project_id=1,
            title="Kit collision",
            reference="codegen-product-kit-1",
            column_id=2,
        )
        client.call(
            "saveTaskMetadata",
            task_id=butler_key,
            values={"record_type": "task", "project": "butler", "task_type": "code"},
        )
        client.call(
            "saveTaskMetadata",
            task_id=kit_key,
            values={
                "project": "codegen-product-kit",
                "record_type": "task",
                "task_type": "code",
                "blocked_by": "butler-1",
            },
        )
        writer.comment(
            role="worker",
            actor="test",
            reference="butler-1",
            body="butler collision comment",
            request_id="comment-butler-collision",
        )
        writer.comment(
            role="worker",
            actor="test",
            reference="codegen-product-kit-1",
            body="kit collision comment",
            request_id="comment-kit-collision",
        )
        client.call("closeTask", task_id=butler_key)
        self.assertNotEqual(butler_key, kit_key)
        sprint_writer.comment(
            role="po",
            actor="test",
            reference=sprint_ref,
            body="sprint recovery comment",
            request_id="sprint-recovery-comment",
        )
        sprint_writer.resume(
            role="po",
            actor="test",
            reference=sprint_ref,
            entry={
                "selected_step": "continue recovery",
                "selected_why": "the dump is ready",
                "rejected_alternatives": "none",
                "current_task": "ummanu-1",
                "dod_state": "in progress",
                "next_safe_step": "verify restore",
                "recorded_at": "2026-09-08T00:00:00Z",
            },
            request_id="sprint-recovery-resume",
        )
        sprint_writer.record_budget(
            role="po",
            actor="test",
            reference=sprint_ref,
            event_type="red_ci",
            request_id="sprint-recovery-budget",
        )
        writer.move(
            role="po",
            actor="test",
            reference="ummanu-1",
            target="done",
            reason="recovery fixture complete",
            request_id="complete-recovery-one",
            sprint_override=True,
            sprint_override_reason="integration recovery fixture",
        )
        writer.archive(
            role="po",
            actor="test",
            reference="ummanu-1",
            reason="retain archived recovery evidence",
            request_id="archive-recovery-one",
        )
        writer.comment(
            role="worker",
            actor="test",
            reference="ummanu-1",
            body="post-close evidence",
            request_id="post-close-comment",
        )
        sprint_writer.close(
            role="po",
            actor="test",
            reference=sprint_ref,
            decisions={
                "issues": [
                    {
                        "ref": "issue:recovery",
                        "verdict": "open",
                        "reason": "recovery remains supported",
                    }
                ],
                "cards": [
                    {
                        "ref": "ummanu-2",
                        "verdict": "drop",
                        "reason": "fixture closes with dependent work recorded",
                    }
                ],
            },
            reason="recovery fixture closed",
            request_id="close-recovery-sprint",
        )
        client.connection.close()

    def _exports(self, data_dir: Path, instance_dir: Path, **_kwargs) -> dict[str, DataExport]:
        board = export_board(data_dir, instance_dir=instance_dir)
        (data_dir / "memory" / "export.ndjson").write_text("", encoding="utf-8")
        runs = data_dir / "runs"
        for name, body in (
            ("watermarks.json", "{}\n"),
            ("cards.json", "{}\n"),
            ("claims.json", "{}\n"),
            ("runs.ndjson", ""),
        ):
            (runs / name).write_text(body, encoding="utf-8")
        for name in ("transcripts", "artifacts"):
            (data_dir / name / "inventory.json").write_text("{}\n", encoding="utf-8")
        return {
            "board": board,
            "memory": DataExport(data_dir / "memory" / "export.ndjson", 0, "test"),
            "runs": DataExport(runs / "runs.ndjson", 0, "test"),
            "transcripts": DataExport(data_dir / "transcripts" / "inventory.json", 0, "test"),
            "artifacts": DataExport(data_dir / "artifacts" / "inventory.json", 0, "test"),
        }

    def test_full_backup_destroy_source_restore_target_and_rerun(self) -> None:
        self._seed()
        from ummanu.po.store import PoStore

        po = PoStore(self.source_config.for_role("app"))
        session = po.create_session(session_id="recovery-po-session", cli="claude", model="opus",
                                    cwd="/tmp/recovery-po", cli_session_id=None, effort="high")
        metadata = {"source": "dispatcher", "summary": "Which cut?",
                    "sprint_ref": "sprint:recovery-custom", "comment_position": 50}
        turn, created = po.claim_turn(session.session_id, "Which cut?", lambda seq: self.root / f"turn-{seq}",
                                      request_id="recovery-po-input", prompt="Which cut?\n\nFrozen comments",
                                      metadata=metadata)
        self.assertTrue(created)
        self.assertTrue(po.complete_turn(session.session_id, turn.seq, "Answer"))
        original_feed = po.feed(session.session_id)
        # secretary-1770: owner events are a board table, so the engine dump carries them.
        from ummanu.board.owner_events import OwnerEventStore, record

        source_events = OwnerEventStore(self.source_config.for_role("app"))
        self.assertTrue(record("card_handed_to_owner", "ummanu-1", "handed", "recovery-handover", to=source_events))
        with (
            mock.patch("ummanu.backup._claimed_workspace_from_cwd", return_value=None),
            mock.patch("ummanu.backup._pipeline_status", return_value={"paused": False}),
            mock.patch("ummanu.backup._pipeline_action", return_value=None),
            mock.patch("ummanu.backup.export_all", side_effect=self._exports),
            mock.patch("ummanu.sprints.sprint_client", wraps=sprint_client) as sprint_factory,
        ):
            results = create_backups(self.source_instance, backup_kinds=("full", "core"))
        sprint_factory.assert_called_once_with(self.source_instance)
        by_kind = {result.manifest["backup_kind"]: result for result in results}
        result = by_kind["full"]
        core = by_kind["core"]
        verified = verify_backup(result.archive)
        self.assertEqual(verified.code, 0, verified.findings)
        core_verified = verify_backup(core.archive)
        self.assertEqual(core_verified.code, 0, core_verified.findings)
        self.assertEqual(core.manifest["board_backend"], "postgres")
        self.assertIn("board_history", core.manifest["components"])
        self.assertNotIn("postgres_dump", core.manifest["components"])
        with tarfile.open(core.archive) as archive:
            self.assertIn("ummanu-backup/ummanu-data/board/audit.json", archive.getnames())
            self.assertIn("ummanu-backup/ummanu-data/board/audit.ndjson", archive.getnames())
            self.assertNotIn("ummanu-backup/engine/postgres.dump", archive.getnames())
        self.assertNotIn("raw_board", result.manifest["components"])
        counts = result.manifest["components"]["postgres_dump"]["table_counts"]
        self.assertEqual(counts["products"], 1)
        self.assertEqual(counts["issues"], 1)
        self.assertEqual(counts["tasks"], 4)
        self.assertEqual(counts["sprints"], 1)
        self.assertEqual(counts["task_dependencies"], 2)
        self.assertEqual(counts["task_supersessions"], 1)
        self.assertEqual(counts["task_issues"], 1)
        self.assertGreaterEqual(counts["task_comments"], 4)
        self.assertEqual(counts["repositories"], 1)
        self.assertEqual(counts["projects"], 3)
        self.assertEqual(counts["sprint_repositories"], 1)
        self.assertEqual(counts["sprint_projects"], 1)
        self.assertEqual(counts["sprint_issues"], 1)
        self.assertEqual(counts["sprint_comments"], 2)
        self.assertEqual(counts["sprint_resumes"], 1)
        self.assertEqual(counts["sprint_budget_events"], 1)
        self.assertEqual(counts["sprint_decisions"], 2)
        self.assertGreater(counts["board_events"], 0)
        self.assertGreaterEqual(counts["requests"], 12)
        self.assertGreaterEqual(counts["owner_events"], 1)
        self.assertGreater(result.manifest["components"]["postgres_dump"]["bytes"], 0)
        source_probe = SqlCardClient(self.source_config.for_role("read"), self.source_instance)
        self.assertEqual(
            source_probe._query(
                "SELECT request_id FROM sprint_budget_events WHERE sprint_ref = %s",
                ("sprint:recovery-custom",),
            ),
            [("sprint-recovery-budget",)],
        )
        self.assertIn(
            ("complete-recovery-one", True),
            source_probe._query("SELECT request_id, committed FROM board_events ORDER BY request_id"),
        )
        source_probe.close()
        with tarfile.open(result.archive) as archive:
            cards = json.loads(
                archive.extractfile("ummanu-backup/ummanu-data/board/cards.json").read().decode("utf-8")
            )["cards"]
            sprints = json.loads(
                archive.extractfile("ummanu-backup/ummanu-data/board/sprints.json")
                .read()
                .decode("utf-8")
            )["sprints"]
            history = json.loads(
                archive.extractfile("ummanu-backup/ummanu-data/board/audit.json").read().decode("utf-8")
            )["events"]
        cards_by_ref = {card["reference"]: card for card in cards}
        self.assertEqual([card["reference"] for card in cards].count("butler-1"), 1)
        self.assertEqual([card["reference"] for card in cards].count("codegen-product-kit-1"), 1)
        self.assertTrue(cards_by_ref["butler-1"]["closed"])
        self.assertFalse(cards_by_ref["codegen-product-kit-1"]["closed"])
        self.assertEqual(cards_by_ref["codegen-product-kit-1"]["metadata"]["blocked_by"], "butler-1")
        self.assertIn(
            "butler collision comment",
            "\n".join(comment["text"] for comment in cards_by_ref["butler-1"]["comments"]),
        )
        self.assertIn(
            "kit collision comment",
            "\n".join(comment["text"] for comment in cards_by_ref["codegen-product-kit-1"]["comments"]),
        )
        self.assertEqual(cards_by_ref["ummanu-1"]["metadata"]["issues"], "issue:recovery")
        self.assertEqual(cards_by_ref["ummanu-2"]["metadata"]["supersedes"], "ummanu-1")
        self.assertEqual(sprints[0]["repositories"], [str(self.root / "repository")])
        self.assertEqual(sprints[0]["resume"]["selected_step"], "continue recovery")
        self.assertEqual(sprints[0]["budget"]["by_type"]["red_ci"], 1)
        self.assertEqual(len(sprints[0]["comments"]), 2)
        history_requests = {event["request_id"] for event in history}
        self.assertTrue(
            {
                "sprint-recovery-budget",
                "sprint-recovery-comment",
                "sprint-recovery-resume",
                "comment-butler-collision",
                "comment-kit-collision",
                "complete-recovery-one",
                "close-recovery-sprint",
            }
            <= history_requests
        )
        with tarfile.open(result.archive) as archive:
            names = archive.getnames()
            secrets = {
                self.source_config.owner_password.encode(),
                self.source_config.app_password.encode(),
                self.source_config.read_password.encode(),
            }
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                stream = archive.extractfile(member)
                self.assertIsNotNone(stream)
                while chunk := stream.read(1024 * 1024):
                    for secret in secrets:
                        self.assertNotIn(secret, chunk)
        self.assertNotIn("ummanu-backup/instance/board-store.env", names)

        source_project = self.projects[0]
        self._down(*source_project)
        self.projects.pop(0)
        first = restore_postgres_backup(result.archive, self.target_instance)
        second = restore_postgres_backup(result.archive, self.target_instance)
        self.assertEqual(first, second)
        marker = json.loads((self.root / "target-data" / "postgres-restore.json").read_text(encoding="utf-8"))
        self.assertFalse(marker["processes_started"])
        target_probe = SqlCardClient(self.target_config.for_role("read"), self.target_instance)
        self.addCleanup(target_probe.close)
        restored_po = PoStore(self.target_config.for_role("app"))
        self.assertEqual(PoStore(self.target_config.for_role("read")).feed(session.session_id), original_feed)
        replay, created = restored_po.claim_turn(session.session_id, "Which cut?", lambda seq: self.root / f"turn-{seq}",
                                                 request_id="recovery-po-input", metadata={"source": "web"})
        self.assertFalse(created)
        self.assertEqual(replay.seq, turn.seq)
        self.assertEqual(restored_po.feed(session.session_id), original_feed)
        self.assertEqual(
            target_probe._query(
                "SELECT request_id FROM sprint_budget_events WHERE sprint_ref = %s",
                ("sprint:recovery-custom",),
            ),
            [("sprint-recovery-budget",)],
        )
        self.assertEqual(
            target_probe._query("SELECT task_ref, issue_id FROM task_issues ORDER BY task_ref, issue_id"),
            [("ummanu-1", "recovery")],
        )
        self.assertEqual(
            target_probe._query("SELECT task_ref, supersedes FROM task_supersessions ORDER BY task_ref"),
            [("ummanu-2", "ummanu-1")],
        )
        self.assertIn(
            ("complete-recovery-one", True),
            target_probe._query("SELECT request_id, committed FROM board_events ORDER BY request_id"),
        )
        self.assertEqual(
            target_probe._query("SELECT DISTINCT request_id FROM sprint_decisions ORDER BY request_id"),
            [("close-recovery-sprint",)],
        )
        self.assertEqual(
            target_probe._query("SELECT project_id FROM projects ORDER BY project_id"),
            [("butler",), ("codegen-product-kit",), ("ummanu",)],
        )
        collision_rows = target_probe._query(
            "SELECT task_ref, task_number, board_key, archived FROM tasks "
            "WHERE task_ref IN ('butler-1', 'codegen-product-kit-1') ORDER BY task_ref"
        )
        self.assertEqual(
            [(row[0], row[1], row[3]) for row in collision_rows],
            [("butler-1", 1, True), ("codegen-product-kit-1", 1, False)],
        )
        self.assertNotEqual(collision_rows[0][2], collision_rows[1][2])
        self.assertEqual(
            target_probe._query("SELECT path FROM repositories ORDER BY path"),
            [(str(self.root / "repository"),)],
        )
        self.assertEqual(
            target_probe._query(
                "SELECT kind, \"class\", subject_ref, read_at FROM owner_events WHERE dedup_key = %s",
                ("recovery-handover",),
            ),
            [("card_handed_to_owner", "needs_owner", "ummanu-1", None)],
        )
        print(
            "postgres recovery evidence:",
            json.dumps(
                {
                    "archive_bytes": result.archive.stat().st_size,
                    "dump_bytes": result.manifest["components"]["postgres_dump"]["bytes"],
                    "dump_tool": result.manifest["components"]["postgres_dump"]["tool_version"],
                    "source_head": result.manifest["components"]["postgres_dump"]["source_schema"],
                    "target_head": marker["migration_head"],
                    "table_counts": counts,
                    "idempotent_rerun": first == second,
                    "processes_started": marker["processes_started"],
                    "secrets_absent": True,
                },
                sort_keys=True,
            ),
        )

    def _source_comment(self, body: str, request_id: str) -> None:
        client = SqlCardClient(self.source_config.for_role("owner"), self.source_instance)
        try:
            TaskWriter(client, data_dir=self.root / "source-data").comment(
                role="worker", actor="test", reference="ummanu-1", body=body, request_id=request_id
            )
        finally:
            client.connection.close()

    def test_writes_inside_the_freeze_and_during_the_dump_keep_counts_equal_to_the_dump(self) -> None:
        """secretary-1678 D1: the pause writes rows, and counts taken before it described no dump."""
        self._seed()
        from ummanu.board import postgres_recovery

        run_client = postgres_recovery._run_client
        dumps: list[list[str]] = []

        def pipeline(action: str, **_kwargs):
            if action == "pause":
                # The freeze's own records (observer stops) commit after the pause begins.
                self._source_comment("written inside the freeze", "freeze-write")
            return None

        def client(args: list[str], action: str) -> str:
            if action == "pg_dump":
                dumps.append(args)
                # The snapshot is already exported: this commit is in neither the counts nor the dump.
                self._source_comment("written while the dump runs", "dump-window-write")
            return run_client(args, action)

        with (
            mock.patch("ummanu.backup._claimed_workspace_from_cwd", return_value=None),
            mock.patch("ummanu.backup._pipeline_status", return_value={"paused": False}),
            mock.patch("ummanu.backup._pipeline_action", side_effect=pipeline),
            mock.patch("ummanu.backup.export_all", side_effect=self._exports),
            mock.patch("ummanu.board.postgres_recovery._run_client", side_effect=client),
        ):
            (result,) = create_backups(self.source_instance)
        self.assertEqual(len(dumps), 1)
        self.assertTrue(any(arg.startswith("--snapshot=") for arg in dumps[0]))
        counts = result.manifest["components"]["postgres_dump"]["table_counts"]
        source_probe = SqlCardClient(self.source_config.for_role("read"), self.source_instance)
        self.addCleanup(source_probe.close)
        live_comments = source_probe._query("SELECT count(*) FROM task_comments")[0][0]
        self.assertEqual(counts["task_comments"], live_comments - 1)

        restore_postgres_backup(result.archive, self.target_instance)

        target_probe = SqlCardClient(self.target_config.for_role("read"), self.target_instance)
        self.addCleanup(target_probe.close)
        self.assertEqual(postgres_recovery.target_counts(self.target_config), counts)
        bodies = {row[0] for row in target_probe._query("SELECT body FROM task_comments")}
        self.assertIn("written inside the freeze", "\n".join(bodies))
        self.assertNotIn("written while the dump runs", "\n".join(bodies))

    def test_export_states_record_types_and_an_archive_without_them_restores(self) -> None:
        """secretary-1678 D2: older task rows exported no record type; restore reads absent as task."""
        self._seed()
        source = SqlCardClient(self.source_config.for_role("owner"), self.source_instance)
        with source.transaction():
            # Older rows never repeated their kind in the bag.
            source._execute(
                "UPDATE tasks SET extensions = extensions #- '{extra,record_type}' WHERE task_ref = 'ummanu-2'"
            )
        source.connection.close()
        exported: list[dict] = []

        def exports(data_dir: Path, instance_dir: Path, **kwargs) -> dict[str, DataExport]:
            result = self._exports(data_dir, instance_dir, **kwargs)
            board = data_dir / "board"
            cards = json.loads((board / "cards.json").read_text(encoding="utf-8"))["cards"]
            exported.extend(json.loads(json.dumps(cards)))
            # The archive an older producer wrote: task cards carry no record type.
            for card in cards:
                if card["metadata"].get("record_type") == "task":
                    del card["metadata"]["record_type"]
            (board / "cards.json").write_text(json.dumps({"version": 1, "cards": cards}), encoding="utf-8")
            (board / "cards.ndjson").write_text(
                "".join(json.dumps(card) + "\n" for card in cards), encoding="utf-8"
            )
            return result

        with (
            mock.patch("ummanu.backup._claimed_workspace_from_cwd", return_value=None),
            mock.patch("ummanu.backup._pipeline_status", return_value={"paused": False}),
            mock.patch("ummanu.backup._pipeline_action", return_value=None),
            mock.patch("ummanu.backup.export_all", side_effect=exports),
        ):
            (result,) = create_backups(self.source_instance)

        kinds = {card["reference"]: card["metadata"].get("record_type") for card in exported}
        self.assertEqual(kinds["product:ummanu"], "product")
        self.assertEqual(kinds["issue:recovery"], "issue")
        self.assertEqual(kinds["ummanu-2"], "task")
        self.assertEqual(
            {ref: kind for ref, kind in kinds.items() if ":" not in ref},
            dict.fromkeys(("butler-1", "codegen-product-kit-1", "ummanu-1", "ummanu-2"), "task"),
        )
        with tarfile.open(result.archive) as archive:
            archived = json.loads(
                archive.extractfile("ummanu-backup/ummanu-data/board/cards.json").read().decode("utf-8")
            )["cards"]
        self.assertFalse(any("record_type" in card["metadata"] for card in archived if ":" not in card["reference"]))

        # Counts, then normalized parity against the restored store; either refusal raises.
        restore_postgres_backup(result.archive, self.target_instance)
        self.assertTrue((self.root / "target-data" / "postgres-restore.json").is_file())

    def test_tasks_rows_whose_bag_names_another_kind_export_as_tasks_and_restore(self) -> None:
        """secretary-1678 D2: the table is the record type, whatever a stale bag value says."""
        self._seed()
        source = SqlCardClient(self.source_config.for_role("owner"), self.source_instance)
        with source.transaction():
            for reference, stale in (("ummanu-1", "product"), ("codegen-product-kit-1", "not-a-kind")):
                source._execute(
                    "UPDATE tasks SET extensions = jsonb_set(coalesce(extensions, '{}'::jsonb), "
                    "'{extra,record_type}', to_jsonb(%s::text), true) WHERE task_ref = %s",
                    (stale, reference),
                )
        source.connection.close()

        with (
            mock.patch("ummanu.backup._claimed_workspace_from_cwd", return_value=None),
            mock.patch("ummanu.backup._pipeline_status", return_value={"paused": False}),
            mock.patch("ummanu.backup._pipeline_action", return_value=None),
            mock.patch("ummanu.backup.export_all", side_effect=self._exports),
        ):
            (result,) = create_backups(self.source_instance)

        with tarfile.open(result.archive) as archive:
            cards = json.loads(
                archive.extractfile("ummanu-backup/ummanu-data/board/cards.json").read().decode("utf-8")
            )["cards"]
        kinds = {card["reference"]: card["metadata"]["record_type"] for card in cards}
        self.assertEqual(kinds["ummanu-1"], "task")
        self.assertEqual(kinds["codegen-product-kit-1"], "task")
        self.assertEqual(kinds["product:ummanu"], "product")
        self.assertEqual(kinds["issue:recovery"], "issue")

        # Counts, then normalized parity against the restored store; either refusal raises.
        restore_postgres_backup(result.archive, self.target_instance)
        self.assertTrue((self.root / "target-data" / "postgres-restore.json").is_file())

    @staticmethod
    def _down(instance: Path, project: str, completed: bool) -> None:
        cleanup_test_project(project, container_expected=completed)


if __name__ == "__main__":
    unittest.main()
