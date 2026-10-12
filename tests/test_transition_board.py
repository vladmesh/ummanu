"""The transition's board steps against PostgreSQL: dump, restore, translate, count (RENAME.md §T3).

CI integration shard only: two isolated Compose stores, the source seeded through the product's own
writers, the target restored from the transition's dump and translated. Every product name comes
from the transition's own table, so the rename card can rewrite this file mechanically; the only
literal old names here are card refs (class R).
"""

from __future__ import annotations

import os
import socket
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Any
from unittest import mock

from tests.container_cleanup import cleanup_test_project
from ummanu.board import migrate, provision, schema
from ummanu.board.sql_cards import SqlCardClient
from ummanu.board.store import BoardStoreConfig
from ummanu.data import init_layout
from ummanu.sprint_observer import none_choice
from ummanu.sprints import SprintWriter, sprint_client
from ummanu.tasks import TaskReader, TaskWriter
from ummanu.transition import board as transition_board
from ummanu.transition.names import BASELINE_MARKER, COUNTS_MARKER, INSTANCE_PROJECT, NEW, OLD

OPEN_SPRINT = "sprint:1475"
CLOSED_SPRINT = "sprint:1400"
CARD = f"{OLD.project_id}-1915"


class TransitionBoardTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.old_root = self.home / OLD.product_dir
        self.old_root.mkdir(parents=True)
        self.projects: list[tuple[str, bool]] = []
        self.addCleanup(self._cleanup)
        self.environment = mock.patch.dict(os.environ, {"BOARD_ROLE": ""})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.source_instance, self.source = self._store("source", OLD.db_name)
        self.target_instance, self.target = self._store("target", NEW.db_name)

    def _store(self, name: str, dbname: str) -> tuple[Path, BoardStoreConfig]:
        instance = self.root / name
        (instance / "projects").mkdir(parents=True)
        (instance / "adapters").mkdir()
        (instance / "instance.yaml").write_text(
            f"version: 1\nname: transition-{name}\ndata_dir: {self.root / (name + '-data')}\noffsite:\n"
            "  instance_remote: git@example.invalid:test/transition.git\n",
            encoding="utf-8",
        )
        (instance / "projects" / f"{OLD.project_id}.yaml").write_text(
            f"id: {OLD.project_id}\nrepo: {self.old_root}\nenabled: false\nadapter: {OLD.project_id}\n"
            "default_branch: main\n",
            encoding="utf-8",
        )
        (instance / "adapters" / f"{OLD.project_id}.yaml").write_text(
            "setup:\n  commands: ['true']\nsmoke:\n  command: 'true'\n"
            "validation:\n  ci: local\n  command: 'true'\nartifact_policy:\n  write_project_files: false\n",
            encoding="utf-8",
        )
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = int(listener.getsockname()[1])
        config = BoardStoreConfig(
            host="127.0.0.1", port=port, dbname=dbname,
            # The roles are the running tree's: migration 0001 grants to its own owner role name.
            owner_user=schema.OWNER_ROLE, owner_password=f"{name}-owner-secret",
            app_user=schema.APP_ROLE, app_password=f"{name}-app-secret",
            read_user=schema.READ_ROLE, read_password=f"{name}-read-secret",
        )
        path = instance / "board-store.env"
        path.write_text("".join(f"{key}={value}\n" for key, value in config.as_environ().items()), encoding="utf-8")
        path.chmod(0o600)
        project = f"transition-board-{name}-{uuid.uuid4().hex}"
        self.projects.append((project, False))
        provision.provision(instance, compose_path=instance / "postgres-compose.yml", project=project,
                            test_owner_pid=os.getpid())
        self.projects[-1] = (project, True)
        migrate.migrate_instance(instance)
        provision.verify_roles(instance)
        return instance, config

    def _cleanup(self) -> None:
        errors = []
        for project, completed in reversed(self.projects):
            try:
                cleanup_test_project(project, container_expected=completed)
            except RuntimeError as exc:
                errors.append(f"{project}: {exc}")
        if errors:
            raise RuntimeError("test Compose cleanup refused: " + "; ".join(errors))

    # -- seed ------------------------------------------------------------------------------------

    def _sql(self, config: BoardStoreConfig, statement: str, parameters: tuple[Any, ...] = ()) -> list[tuple]:
        import psycopg

        with psycopg.connect(config.for_role("owner").conninfo()) as connection:
            cursor = connection.execute(statement, parameters)
            return list(cursor.fetchall()) if cursor.description else []

    def _seed(self) -> None:
        data_dir = self.root / "source-data"
        init_layout(data_dir)
        client = SqlCardClient(self.source.for_role("owner"), self.source_instance)
        self.addCleanup(client.close)
        with client.transaction():
            product = client.call("createTask", project_id=1, title=OLD.product_title,
                                  reference=f"product:{OLD.product_id}")
            client.call("saveTaskMetadata", task_id=product, values={
                "record_type": "product", "product_id": OLD.product_id,
                "product_projects": f'["{OLD.project_id}"]',
            })
            for issue in ("open-issue", "closed-issue"):
                key = client.call("createTask", project_id=1, title=issue, reference=f"issue:{issue}")
                client.call("saveTaskMetadata", task_id=key, values={
                    "record_type": "issue", "issue_product": OLD.product_id,
                    "issue_kind": "feature", "issue_priority": "P2",
                })
        self._sql(self.source, "INSERT INTO projects (project_id, enabled, registry_present) VALUES (%s, true, true)",
                  (INSTANCE_PROJECT,))
        board = sprint_client(self.source_instance)
        self.addCleanup(board.close)
        sprints = SprintWriter(board, data_dir=data_dir, instance=self.source_instance)
        for reference, issue in ((CLOSED_SPRINT, "closed-issue"), (OPEN_SPRINT, "open-issue")):
            sprints.create(
                role="po", actor="test", goal=f"goal of {reference}", repositories=[str(self.old_root)],
                product=OLD.product_id, issues=[f"issue:{issue}"], projects=[OLD.project_id],
                observer=none_choice(), reference=reference, request_id=f"create-{reference}",
            )
            self._sql(self.source, "UPDATE sprints SET allowed_productions = ARRAY[%s] WHERE ref = %s",
                      (OLD.project_id, reference))
            if reference == CLOSED_SPRINT:
                self._sql(self.source, "UPDATE sprints SET status = 'closed', closed_at = now(), "
                                       "close_reason = 'done' WHERE ref = %s", (reference,))
                self._sql(self.source, "UPDATE sprint_projects SET reserved = false, released_at = now() "
                                       "WHERE sprint_ref = %s", (reference,))
        self._sql(self.source, "UPDATE issues SET state = 'closed', close_reason = 'resolved' "
                               "WHERE issue_id = 'closed-issue'")
        sprints.comment(role="po", actor="test", reference=OPEN_SPRINT, body=f"{BASELINE_MARKER} doctor ok",
                        request_id="baseline")
        writer = TaskWriter(client, data_dir=data_dir)
        writer.create(
            role="po", actor="test", project=OLD.project_id, task_type="code", title="a parked card",
            target="ready", reference=CARD, sprint=OPEN_SPRINT, sprint_override=True,
            sprint_override_reason="transition fixture", request_id="create-card",
        )
        po = self.home / OLD.data_dir / "po"
        for session, cwd, state in (("open-po", po, "open"), ("closed-po", po, "closed"),
                                    ("other-po", self.root / "elsewhere", "open")):
            closed = state == "closed"
            self._sql(
                self.source,
                "INSERT INTO po_sessions (session_id, cli, model, cwd, created_at, state, closed_at, closed_by) "
                "VALUES (%s, 'claude', 'model', %s, now(), %s, CASE WHEN %s THEN now() END, "
                "CASE WHEN %s THEN 'test' END)",
                (session, str(cwd), state, closed, closed),
            )

    def _rows(self, config: BoardStoreConfig, table: str, where: str = "true", order: str = "1") -> list[tuple]:
        return self._sql(config, f"SELECT * FROM {table} WHERE {where} ORDER BY {order}")

    # -- the steps -------------------------------------------------------------------------------

    def test_dump_restore_translate_counts(self) -> None:
        self._seed()
        markers = transition_board.sprint_markers(self.source, OPEN_SPRINT, (BASELINE_MARKER, COUNTS_MARKER))
        self.assertEqual(sorted(markers), [BASELINE_MARKER])
        self.assertEqual(transition_board.prepared_sprints(self.source, BASELINE_MARKER), [OPEN_SPRINT])

        untouched = {
            "tasks": self._rows(self.source, "tasks", order="board_key"),
            "closed sprints": self._rows(self.source, "sprints", f"ref = '{CLOSED_SPRINT}'"),
            "closed sprint projects": self._rows(self.source, "sprint_projects", f"sprint_ref = '{CLOSED_SPRINT}'"),
            "closed sprint repositories": self._rows(self.source, "sprint_repositories",
                                                     f"sprint_ref = '{CLOSED_SPRINT}'"),
            "closed issues": self._rows(self.source, "issues", "state = 'closed'"),
            "closed and other PO sessions": self._rows(self.source, "po_sessions", "session_id <> 'open-po'"),
            "board_events": self._rows(self.source, "board_events", order="1"),
            "requests": self._rows(self.source, "requests", order="1"),
            "sprint_comments": self._rows(self.source, "sprint_comments", order="comment_id"),
        }
        reservation = self._rows(self.source, "sprint_projects", f"sprint_ref = '{OPEN_SPRINT}'")

        metadata = transition_board.dump(self.source, self.root / "dump")
        self.assertEqual(transition_board.dump(self.source, self.root / "dump"), metadata, "a rerun dumps again")
        self.assertEqual(metadata["table_counts"]["tasks"], 1)
        archive = self.root / "dump" / "postgres.dump"
        self.assertEqual(
            transition_board.restore(archive, self.target_instance, metadata, self.target), "restored"
        )
        self.assertEqual(
            transition_board.restore(archive, self.target_instance, metadata, self.target), "already restored"
        )

        old_po, new_po = self.home / OLD.data_dir / "po", self.home / NEW.data_dir / "po"
        translated = transition_board.translate(
            self.target, OLD, NEW, old_root=self.old_root, new_root=self.home / NEW.product_dir,
            old_po=old_po, new_po=new_po,
        )
        self.assertTrue(translated["translated"])
        self.assertEqual(translated["sprints"], [OPEN_SPRINT])
        self.assertEqual((translated["issues"], translated["po_sessions"]), (1, 1))
        self.assertFalse(transition_board.translate(
            self.target, OLD, NEW, old_root=self.old_root, new_root=self.home / NEW.product_dir,
            old_po=old_po, new_po=new_po,
        )["translated"])

        after = transition_board.table_counts(self.target)
        self.assertEqual(transition_board.verify_counts(metadata["table_counts"], after), [])
        self.assertEqual(
            self._sql(self.target, "SELECT product_id, state, title FROM products ORDER BY product_id"),
            sorted([(OLD.product_id, "archived", OLD.product_title), (NEW.product_id, "active", NEW.product_title)]),
        )
        self.assertEqual(
            self._sql(self.target, "SELECT project_id, enabled, registry_present FROM projects "
                                   "WHERE project_id IN (%s, %s) ORDER BY 1", (OLD.project_id, NEW.project_id)),
            sorted([(OLD.project_id, False, False), (NEW.project_id, True, True)]),
        )
        self.assertEqual(
            self._sql(self.target, "SELECT adapter, orca_binding FROM projects WHERE project_id = %s",
                      (NEW.project_id,)),
            [(NEW.project_id, NEW.project_id)],
        )
        self.assertEqual(
            self._sql(self.target, "SELECT project_id FROM product_projects WHERE product_id = %s ORDER BY 1",
                      (NEW.product_id,)),
            sorted([(NEW.project_id,), (INSTANCE_PROJECT,)]),
        )
        self.assertEqual(
            self._sql(self.target, "SELECT product_id, allowed_productions FROM sprints WHERE ref = %s",
                      (OPEN_SPRINT,)),
            [(NEW.product_id, [NEW.project_id])],
        )
        moved = self._rows(self.target, "sprint_projects", f"sprint_ref = '{OPEN_SPRINT}'")
        self.assertEqual([row[1:] for row in moved], [row[1:] for row in reservation], "same reservation")
        self.assertEqual([row[0] for row in moved], [NEW.project_id])
        self.assertEqual(
            self._sql(self.target, "SELECT r.path FROM sprint_repositories s JOIN repositories r USING (repository_id) "
                                   "WHERE s.sprint_ref = %s", (OPEN_SPRINT,)),
            [(str(self.home / NEW.product_dir),)],
        )
        self.assertEqual(
            self._sql(self.target, "SELECT issue_id, product_id FROM issues ORDER BY 1"),
            [("closed-issue", OLD.product_id), ("open-issue", NEW.product_id)],
        )
        self.assertEqual(
            self._sql(self.target, "SELECT cwd FROM po_sessions WHERE session_id = 'open-po'"), [(str(new_po),)]
        )
        self.assertEqual(
            {
                "tasks": self._rows(self.target, "tasks", order="board_key"),
                "closed sprints": self._rows(self.target, "sprints", f"ref = '{CLOSED_SPRINT}'"),
                "closed sprint projects": self._rows(self.target, "sprint_projects",
                                                     f"sprint_ref = '{CLOSED_SPRINT}'"),
                "closed sprint repositories": self._rows(self.target, "sprint_repositories",
                                                         f"sprint_ref = '{CLOSED_SPRINT}'"),
                "closed issues": self._rows(self.target, "issues", "state = 'closed'"),
                "closed and other PO sessions": self._rows(self.target, "po_sessions", "session_id <> 'open-po'"),
                "board_events": self._rows(self.target, "board_events", order="1"),
                "requests": self._rows(self.target, "requests", order="1"),
                "sprint_comments": self._rows(self.target, "sprint_comments", order="comment_id"),
            },
            untouched,
        )
        reader = SqlCardClient(self.target.for_role("read"), self.target_instance)
        self.addCleanup(reader.close)
        card = TaskReader(reader).show(CARD)
        self.assertEqual((card["ref"], card["project"]), (CARD, OLD.project_id))


if __name__ == "__main__":
    unittest.main()
