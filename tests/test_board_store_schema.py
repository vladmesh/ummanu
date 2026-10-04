"""The initial revision against a real PostgreSQL, in a throwaway container.

Schema defects of the kind PostgreSQL refuses are found by executing the migration, not by reading
it, so the Alembic revisions are executed here and the result is counted against the head catalogue
in ``docs/BOARD_STORE.md`` §3.13: 24 tables, 39 `CHECK`, 40 foreign-key, 24 primary-key and 17
unique constraints and 4 partial unique indexes. The last table and the last primary key are
Alembic's `alembic_version` (§7.4).

The counting is not the strongest thing here. `test_the_migrated_database_still_matches_the_models`
asks Alembic to autogenerate a diff between the database this revision built and the models, and
requires it to be empty: the models are the schema, and a revision that drifts from them is a red
test rather than a surprise on the next installation.

**There is no skip in this module.** A missing Docker, a missing dependency or a container that
never becomes ready is an error, not an absence: a schema test that quietly passes because it never
reached a database is worth less than no test at all. The suite is `integration-board`, so this
runs in the `test / integration-board` job of the exact-SHA gate, where the dependencies (core
since this card) and Docker are both present.

The container is verification, never delivery. It publishes on loopback, holds no volume, is
removed in `tearDownClass`, and nothing here reads or writes the live installation.
"""

from __future__ import annotations

import importlib
import json
import os
import socket
import stat
import subprocess
import threading
import time
import unittest
import warnings
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from ummanu import upgrade
from ummanu.board import migrate, provision, schema, schema_gate
from ummanu.board.backend import record_key
from ummanu.board.store import BoardStoreConfig
from ummanu.runtime.container_labels import TEST_BOARD_LABEL
from ummanu.host_apply import SystemdUnitInstaller
from tests.container_cleanup import cleanup_test_project, remove_test_container

IMAGE = "postgres:16"
DATABASE = "board_store_test"
OWNER = "ummanu_owner"
OWNER_PASSWORD = "throwaway-owner-password"
#: Deliberately awkward: it carries the three characters that break a URL, a `text()` construct
#: and a naive SQL literal respectively.
APP_PASSWORD = "throwaway@app/pass:word"
READ_PASSWORD = "throwaway-read-password"
READY_TIMEOUT_SECONDS = 90

COUNTS = """
SELECT
  (SELECT count(*) FROM information_schema.tables
     WHERE table_schema = 'public' AND table_type = 'BASE TABLE'),
  (SELECT count(*) FROM pg_constraint WHERE contype = 'c' AND connamespace = 'public'::regnamespace),
  (SELECT count(*) FROM pg_constraint WHERE contype = 'f' AND connamespace = 'public'::regnamespace),
  (SELECT count(*) FROM pg_constraint WHERE contype = 'p' AND connamespace = 'public'::regnamespace),
  (SELECT count(*) FROM pg_constraint WHERE contype = 'u' AND connamespace = 'public'::regnamespace),
  (SELECT count(*) FROM pg_index i
     JOIN pg_class c ON c.oid = i.indexrelid
     JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND i.indisunique AND i.indpred IS NOT NULL)
"""

#: What the same schema makes of a `postgres:16` at the head revision, and what §3.13 records
#: beside `0001`'s own numbers. A disagreement here is a defect of the transcription into models,
#: not of the document. Unchanged by `0003`, which trades one `CHECK` for one `CHECK`. `0008` adds
#: the three PO tables: six `CHECK`, two foreign keys, three primary keys, one partial unique index.
#: `0009` adds `po_requests`: two `CHECK`, two foreign keys, one primary key. `0010` adds one `CHECK`
#: on `po_sessions` (closed exactly when audited). `0011` restates the `task_type` CHECK (one for one)
#: and adds two on `tasks`: the review choice vocabulary and live impact being research-only.
#: `0012` and `0013` add non-unique indexes on `requests` only, so no number here moves. `0017`
#: restates the `task_type` CHECK again, one for one. `0018` adds `owner_events`: three `CHECK`, one
#: primary key, one `UNIQUE`. `0022` adds `origin_returns`: three `CHECK`, one primary key, one
#: `UNIQUE` (its partial index is not unique). `0023` adds one `CHECK` on `sprints` (the e2e counts are
#: not negative), `sprint_e2e_charges` (one foreign key, one primary key) and restates the two
#: owner-event CHECKs, one for one. `0026` adds the local-run array CHECK; `0027` adds the owner-decision array CHECK.
DOCUMENTED_COUNTS = (31, 59, 45, 31, 19, 5)

#: Every revision this build ships, oldest first: what an empty database owes.
REVISIONS = (
    "0001_initial",
    "0002_board_gaps",
    "0003_task_type_optional",
    "0004_product_issue_sql",
    "0005_sprint_sql",
    "0006_sprint_transport_key",
    "0007_card_transport_key",
    "0008_po_sessions",
    "0009_po_requests",
    "0010_po_session_close",
    "0011_card_kinds",
    "0012_request_read_indexes",
    "0013_budget_candidates",
    "0014_neutral_extension_bag",
    "0015_po_effort_resolved_model",
    "0016_sprint_po_session",
    "0017_po_card_kinds",
    "0018_owner_events",
    "0019_po_session_title",
    "0020_wait_card_kind",
    "0021_delegated_card_settled",
    "0022_origin_returns",
    "0023_sprint_e2e_budget",
    "0024_e2e_after_merge_kind",
    "0025_card_waits_for_person",
    "0026_sprint_local_runs",
    "0027_sprint_owner_decisions",
)


def docker(*arguments: str) -> str:
    completed = subprocess.run(
        ["docker", *arguments], capture_output=True, text=True, timeout=180, check=False
    )
    if completed.returncode != 0:
        raise RuntimeError(f"docker {' '.join(arguments)} failed: {completed.stderr.strip()}")
    return completed.stdout.strip()


class ProductionLayoutUpgradeTests(unittest.TestCase):
    def test_upgrade_replaces_dev_owned_compose_in_root_owned_installation(self) -> None:
        """The real upgrade step uses sudo for its atomic write under a root-owned 0755 dir."""
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            installation = root / "installation"
            installation.mkdir(mode=0o755)
            compose = installation / "postgres-compose.yml"
            compose.write_text(provision.LEGACY_COMPOSE_TEXT, encoding="utf-8")
            compose.chmod(0o600)
            original = compose.stat()
            config = root / "board-store.env"
            config.write_text("\n".join((
                "UMMANU_DB_HOST=127.0.0.1", "UMMANU_DB_PORT=5432",
                f"UMMANU_DB_NAME={DATABASE}", f"UMMANU_DB_OWNER_USER={OWNER}",
                f"UMMANU_DB_OWNER_PASSWORD={OWNER_PASSWORD}",
                f"UMMANU_DB_APP_USER={schema.APP_ROLE}",
                f"UMMANU_DB_APP_PASSWORD={APP_PASSWORD}",
                f"UMMANU_DB_READ_USER={schema.READ_ROLE}",
                f"UMMANU_DB_READ_PASSWORD={READ_PASSWORD}", "",
            )), encoding="utf-8")
            config.chmod(0o600)
            elevated = [] if os.geteuid() == 0 else ["sudo", "-n"]
            subprocess.run([*elevated, "chown", "0:0", str(installation)], check=True)
            try:
                defaults = dict(provision.provision.__kwdefaults__ or {})
                defaults["compose_path"] = compose
                context = upgrade.UpgradeContext(
                    instance_path=root, product_root=upgrade.running_product_root(),
                    base_branch="main", dry_run=False, units=SystemdUnitInstaller(),
                    report=SimpleNamespace(data_dir=root),
                )
                with (
                    mock.patch.object(provision.provision, "__kwdefaults__", defaults),
                    mock.patch("ummanu.board.provision._run", return_value="container-id"),
                    mock.patch("ummanu.board.provision._inspect_container"),
                    mock.patch("ummanu.board.provision._wait_ready"),
                ):
                    result = upgrade.step_board_store_provision(context)
                self.assertEqual(result.status, "changed", result.detail)
                self.assertIn("production ownership marker", result.detail)
                updated = compose.stat()
                self.assertNotEqual(updated.st_ino, original.st_ino)
                self.assertEqual((updated.st_uid, updated.st_gid), (original.st_uid, original.st_gid))
                self.assertEqual(stat.S_IMODE(updated.st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(installation.stat().st_mode), 0o755)
                self.assertEqual((installation.stat().st_uid, installation.stat().st_gid), (0, 0))
                self.assertEqual(compose.read_text(encoding="utf-8"), provision.COMPOSE_TEXT)
                self.assertEqual(list(installation.iterdir()), [compose])
            finally:
                subprocess.run(
                    [*elevated, "chown", f"{os.getuid()}:{os.getgid()}", str(installation)],
                    check=True,
                )


class BoardStoreSchemaTests(unittest.TestCase):
    container = ""
    port = 0

    @classmethod
    def setUpClass(cls) -> None:
        for module, why in (
            ("psycopg", "the board store driver, docs/BOARD_STORE.md §5.8"),
            ("sqlalchemy", "the schema's source of truth since 2026-09-07"),
            ("alembic", "the migration tool since 2026-09-07"),
        ):
            try:
                __import__(module)
            except ImportError as exc:  # pragma: no cover - a venv without a core dependency
                raise RuntimeError(
                    f"{module} is a core dependency ({why}); reinstall the product "
                    "(`pip install -e .`) rather than skipping the schema proof"
                ) from exc
        cls.container = docker(
            "run",
            "--rm",
            "-d",
            "--label",
            f"{TEST_BOARD_LABEL}={os.getpid()}",
            "-e",
            f"POSTGRES_DB={DATABASE}",
            "-e",
            f"POSTGRES_USER={OWNER}",
            "-e",
            f"POSTGRES_PASSWORD={OWNER_PASSWORD}",
            "-p",
            "127.0.0.1::5432",
            IMAGE,
        )
        cls.addClassCleanup(lambda: remove_test_container(cls.container))
        published = json.loads(docker("inspect", "-f", "{{json .NetworkSettings.Ports}}", cls.container))
        cls.port = int(published["5432/tcp"][0]["HostPort"])
        cls._await_server()

    @classmethod
    def _await_server(cls) -> None:
        """Wait for the cluster the volume keeps, not the bootstrap one the entrypoint stops.

        `pg_isready` answers yes to the temporary initialisation server, which then shuts down;
        a real connection to the published port is the only readiness signal that cannot lie.
        """
        import psycopg

        deadline = time.monotonic() + READY_TIMEOUT_SECONDS
        last = ""
        while time.monotonic() < deadline:
            try:
                with psycopg.connect(cls.credentials("owner").conninfo(), connect_timeout=3):
                    return
            except psycopg.Error as exc:
                last = str(exc).strip()
                time.sleep(0.5)
        raise RuntimeError(f"the throwaway {IMAGE} never accepted a connection: {last}")

    @classmethod
    def config(cls, dbname: str = DATABASE) -> BoardStoreConfig:
        return BoardStoreConfig(
            host="127.0.0.1",
            port=cls.port,
            dbname=dbname,
            owner_user=OWNER,
            owner_password=OWNER_PASSWORD,
            app_user=schema.APP_ROLE,
            app_password=APP_PASSWORD,
            read_user=schema.READ_ROLE,
            read_password=READ_PASSWORD,
        )

    @classmethod
    def credentials(cls, role: str, dbname: str = DATABASE):
        return cls.config(dbname).for_role(role)

    def setUp(self) -> None:
        """One empty database and no leftover roles per test: the initial revision only ever runs
        on an empty database, and its `CREATE ROLE` statements are cluster-wide."""
        import psycopg

        with psycopg.connect(
            self.credentials("owner", "postgres").conninfo(), autocommit=True
        ) as maintenance:
            maintenance.execute(f"DROP DATABASE IF EXISTS {DATABASE} WITH (FORCE)")
            maintenance.execute(f"DROP ROLE IF EXISTS {schema.APP_ROLE}")
            maintenance.execute(f"DROP ROLE IF EXISTS {schema.READ_ROLE}")
            maintenance.execute(f"CREATE DATABASE {DATABASE} OWNER {OWNER}")

    def engine(self, role: str = "owner"):
        import sqlalchemy as sa

        engine = sa.create_engine(migrate.sqlalchemy_url(self.credentials(role)))
        self.addCleanup(engine.dispose)
        return engine

    def owner_connection(self):
        connection = self.engine().connect()
        self.addCleanup(connection.close)
        return connection

    @property
    def passwords(self) -> dict[str, str]:
        return migrate.passwords_for(self.config())

    def run_migrations(self, connection, **kwargs) -> tuple[str, ...]:
        return migrate.apply(connection, passwords=self.passwords, **kwargs)

    def counts(self, connection) -> tuple[int, ...]:
        return tuple(connection.exec_driver_sql(COUNTS).fetchone())

    # --- the schema itself -------------------------------------------------------------

    def test_the_revisions_reproduce_the_numbers_the_document_records(self) -> None:
        connection = self.owner_connection()

        self.assertEqual(self.run_migrations(connection), REVISIONS)

        self.assertEqual(
            self.counts(connection),
            DOCUMENTED_COUNTS,
            "tables, CHECK, FK, PK, UNIQUE and partial unique indexes must match §3.13's numbers",
        )

    def test_0006_frozen_backfill_agrees_with_the_runtime_sprint_mapping(self) -> None:
        revision = importlib.import_module("ummanu.board.migrations.versions.0006_sprint_transport_key")
        self.assertFalse(hasattr(revision, "record_key"))
        for reference in ("sprint:0", "sprint:1596", "sprint:canary", "sprint:١"):
            with self.subTest(reference=reference):
                self.assertEqual(
                    revision._sprint_transport_key(reference),
                    record_key("sprint", reference),
                )

    def test_the_migrated_database_still_matches_the_models(self) -> None:
        """The models are the schema, so a revision that drifts from them is a defect here.

        This is Alembic's own autogenerate comparison, run backwards: anything it would emit to
        make the database match `board.schema` is a difference the revision failed to build.
        """
        from alembic.autogenerate import compare_metadata
        from alembic.migration import MigrationContext

        connection = self.owner_connection()
        self.run_migrations(connection)

        with warnings.catch_warnings():
            # A persisted generated column cannot be altered, which autogenerate says out loud
            # every time it compares one; it is not a difference.
            warnings.filterwarnings("ignore", message="Computed default on")
            context = MigrationContext.configure(
                connection, opts={"compare_type": True, "compare_server_default": True}
            )
            difference = compare_metadata(context, schema.metadata)

        self.assertEqual(difference, [], "the built schema and the models disagree")

    def test_section_9s_sprint_number_sequence_exists(self) -> None:
        connection = self.owner_connection()
        self.run_migrations(connection)

        found = connection.exec_driver_sql(
            "SELECT count(*) FROM pg_sequences WHERE schemaname='public' AND sequencename=%s",
            ("sprint_number_seq",),
        ).fetchone()[0]

        self.assertEqual(found, 1)

    def test_the_version_lives_in_alembics_table_and_nowhere_else(self) -> None:
        connection = self.owner_connection()
        self.run_migrations(connection)

        stamped = connection.exec_driver_sql("SELECT version_num FROM alembic_version").fetchall()

        self.assertEqual(stamped, [(REVISIONS[-1],)])
        self.assertIsNone(
            connection.exec_driver_sql("SELECT to_regclass('public.schema_migrations')").fetchone()[0],
            "the hand-rolled version table is gone; Alembic's is the version",
        )
        self.assertEqual(schema_gate.classify(tuple(row[0] for row in stamped)).state, schema_gate.CURRENT)

    def test_a_second_run_applies_nothing_and_leaves_the_schema_alone(self) -> None:
        connection = self.owner_connection()
        self.run_migrations(connection)
        before = self.counts(connection)

        self.assertEqual(self.run_migrations(connection), ())

        self.assertEqual(self.counts(connection), before)
        self.assertEqual(connection.exec_driver_sql("SELECT count(*) FROM alembic_version").fetchone()[0], 1)

    def test_a_database_at_the_previous_head_advances_to_the_current_head(self) -> None:
        from alembic import command

        connection = self.owner_connection()
        command.upgrade(
            migrate.alembic_config(connection=connection, passwords=self.passwords),
            REVISIONS[-2],
        )
        connection.commit()

        self.assertEqual(self.run_migrations(connection), (REVISIONS[-1],))
        self.assertEqual(migrate.current_revision(connection), REVISIONS[-1])

    def test_0026_release_migration_defaults_existing_sprints_and_accepts_exact_vectors(self) -> None:
        from alembic import command

        from ummanu.board.release_migrations import admit_additive

        connection = self.owner_connection()
        command.upgrade(migrate.alembic_config(connection=connection, passwords=self.passwords), "0025_card_waits_for_person")
        connection.exec_driver_sql(
            "INSERT INTO sprints (ref,board_key,sprint_number,goal,definition_of_done,status,created_at,updated_at) "
            "VALUES ('sprint:7',2000000007,7,'old','old','open',now(),now())"
        )
        connection.commit()
        self.assertEqual(self.run_migrations(connection, admit=admit_additive), ("0026_sprint_local_runs", "0027_sprint_owner_decisions"))
        self.assertEqual(connection.exec_driver_sql("SELECT local_run_exceptions FROM sprints WHERE ref='sprint:7'").scalar(), [])
        entries = [{"project": "ummanu", "argv": ["docker", "run", "two words", ""], "rationale": "owner's exact probe"}]
        connection.exec_driver_sql("UPDATE sprints SET local_run_exceptions=%s::jsonb WHERE ref='sprint:7'", (json.dumps(entries),))
        connection.commit()
        self.assertEqual(connection.exec_driver_sql("SELECT local_run_exceptions FROM sprints WHERE ref='sprint:7'").scalar(), entries)
        self.assertEqual(self.run_migrations(connection, admit=admit_additive), ())
        with self.assertRaisesRegex(RuntimeError, "declares local-run authority"):
            command.downgrade(migrate.alembic_config(connection=connection, passwords=self.passwords), "0025_card_waits_for_person")
        connection.rollback()
        connection.exec_driver_sql("UPDATE sprints SET local_run_exceptions='[]'::jsonb")
        connection.commit()
        command.downgrade(migrate.alembic_config(connection=connection, passwords=self.passwords), "0025_card_waits_for_person")
        connection.commit()
        self.assertEqual(migrate.current_revision(connection), "0025_card_waits_for_person")

    def test_0027_preserves_released_permissions_budget_and_paid_runs_without_inventing_quotes(self) -> None:
        from alembic import command
        from ummanu.board.release_migrations import admit_additive

        connection = self.owner_connection()
        command.upgrade(migrate.alembic_config(connection=connection, passwords=self.passwords), "0026_sprint_local_runs")
        connection.exec_driver_sql(
            "INSERT INTO sprints (ref,board_key,sprint_number,goal,definition_of_done,status,allowed_productions,e2e_budget,e2e_used,created_at,updated_at) "
            "VALUES ('sprint:7',2000000007,7,'old','old','open',ARRAY['ummanu'],7,2,now(),now())"
        )
        connection.exec_driver_sql("INSERT INTO sprint_e2e_charges (dispatch_id,sprint_ref,task_ref,charged_at) VALUES ('paid','sprint:7','ummanu-1',now())")
        connection.commit()
        before = connection.exec_driver_sql("SELECT dispatch_id,sprint_ref,task_ref,charged_at FROM sprint_e2e_charges").all()
        self.assertEqual(self.run_migrations(connection, admit=admit_additive), ("0027_sprint_owner_decisions",))
        self.assertEqual(connection.exec_driver_sql("SELECT allowed_productions,e2e_budget,e2e_used,owner_decisions FROM sprints WHERE ref='sprint:7'").one(), (["ummanu"], 7, 2, []))
        self.assertEqual(connection.exec_driver_sql("SELECT dispatch_id,sprint_ref,task_ref,charged_at FROM sprint_e2e_charges").all(), before)
        connection.exec_driver_sql("UPDATE sprints SET owner_decisions='[{\"id\":\"quoted\"}]'::jsonb")
        connection.commit()
        with self.assertRaisesRegex(RuntimeError, "quoted owner decisions"):
            command.downgrade(migrate.alembic_config(connection=connection, passwords=self.passwords), "0026_sprint_local_runs")
        connection.rollback()

    def test_a_dry_run_reads_the_version_and_writes_nothing(self) -> None:
        connection = self.owner_connection()

        owed = self.run_migrations(connection, dry_run=True)

        self.assertEqual(owed, REVISIONS)
        self.assertIsNone(connection.exec_driver_sql("SELECT to_regclass('public.products')").fetchone()[0])
        self.assertIsNone(
            connection.exec_driver_sql("SELECT to_regclass('public.alembic_version')").fetchone()[0]
        )

    def test_a_failing_revision_leaves_no_half_applied_schema(self) -> None:
        """PostgreSQL's transactional DDL, which is why §7.4 needs no down migration.

        The failure is a real one rather than a fabricated statement: §5.5's `CREATE ROLE` is the
        last thing the initial revision does, so a cluster that already has `ummanu_app` fails it
        after all 21 tables have been created. Nothing may survive that.
        """
        import psycopg

        with psycopg.connect(
            self.credentials("owner", "postgres").conninfo(), autocommit=True
        ) as maintenance:
            maintenance.execute(f"CREATE ROLE {schema.APP_ROLE}")
        self.addCleanup(self._drop_roles)
        connection = self.owner_connection()

        with self.assertRaises(Exception):  # noqa: B017 - whatever the server raises, nothing survives
            self.run_migrations(connection)

        connection.rollback()
        self.assertIsNone(
            connection.exec_driver_sql("SELECT to_regclass('public.products')").fetchone()[0],
            "a failed revision must leave the schema it was moving from",
        )

    def _drop_roles(self) -> None:
        import psycopg

        with psycopg.connect(
            self.credentials("owner", "postgres").conninfo(), autocommit=True
        ) as maintenance:
            maintenance.execute(f"DROP ROLE IF EXISTS {schema.APP_ROLE}")

    def test_the_revision_refuses_to_run_without_the_generated_passwords(self) -> None:
        """§5.5's two passwords are parameters of the run, and an absent one is not a default."""
        connection = self.owner_connection()

        with self.assertRaisesRegex(Exception, "app_password"):
            migrate.apply(connection, passwords={})

        connection.rollback()

    # --- the records `secretary-1583` found the schema could not carry ------------------
    #
    # Each of these is a row the live board holds today and `0001_initial` refused. They are
    # written here as inserts rather than as a reading of the model, because "the schema can
    # represent it" is only true if PostgreSQL accepts it.

    def prepared(self):
        """A migrated database carrying the few rows the cases below reference."""
        connection = self.owner_connection()
        self.run_migrations(connection)
        connection.exec_driver_sql(
            "INSERT INTO products (product_id, board_key, title, created_at, updated_at) "
            "VALUES ('ummanu', %s, 'Ummanu', now(), now())",
            (record_key("product", "ummanu"),),
        )
        connection.exec_driver_sql("INSERT INTO projects (project_id) VALUES ('ummanu')")
        connection.exec_driver_sql(
            "INSERT INTO issues (issue_id, board_key, product_id, title, issue_kind, priority, "
            "created_at, updated_at) VALUES ('2fdac531', %s, 'ummanu', 'An issue', 'bug', "
            "'P1', now(), now())",
            (record_key("issue", "2fdac531"),),
        )
        return connection

    def sprint(self, connection, ref: str, number: int | None) -> None:
        connection.exec_driver_sql(
            "INSERT INTO sprints (ref, board_key, sprint_number, goal, definition_of_done, created_at, "
            "updated_at) VALUES (%s, %s, %s, 'g', 'd', now(), now())",
            (ref, record_key("sprint", ref), number),
        )

    def card(
        self,
        connection,
        ref: str,
        *,
        project: str | None = "ummanu",
        sprint=None,
        task_type: str | None = "code",
        extensions: str = "{}",
    ) -> None:
        connection.exec_driver_sql(
            "INSERT INTO tasks (task_ref, project_id, task_number, title, task_type, state, "
            "sprint_ref, extensions, created_at, updated_at) VALUES (%s, %s, %s, 'A card', %s, "
            "'ready', %s, %s, now(), now())",
            (ref, project, int(ref.rsplit("-", 1)[1]), task_type, sprint, extensions),
        )

    def test_0007_upgrades_a_populated_collision_without_renumbering(self) -> None:
        """The retained imported target: same suffix, with dependent rows, upgraded in place."""
        from alembic import command

        connection = self.owner_connection()
        command.upgrade(
            migrate.alembic_config(connection=connection, passwords=self.passwords),
            "0006_sprint_transport_key",
        )
        connection.exec_driver_sql(
            "INSERT INTO projects (project_id) VALUES ('butler'), ('codegen-product-kit')"
        )
        for ref, project in (("butler-1", "butler"), ("codegen-product-kit-1", "codegen-product-kit")):
            connection.exec_driver_sql(
                "INSERT INTO tasks (task_ref, project_id, task_number, title, task_type, state, "
                "created_at, updated_at) VALUES (%s, %s, 1, %s, 'code', 'ready', now(), now())",
                (ref, project, ref),
            )
        connection.exec_driver_sql(
            "INSERT INTO task_comments (task_ref, marker, body, created_at) "
            "VALUES ('butler-1', 'note', 'butler comment', now())"
        )
        connection.exec_driver_sql(
            "INSERT INTO task_dependencies (task_ref, depends_on, depends_on_task) "
            "VALUES ('codegen-product-kit-1', 'butler-1', 'butler-1')"
        )
        connection.exec_driver_sql(
            "INSERT INTO requests (request_id, operation, intent, status, protocol, entity_kind, "
            "ref, created_at, settled_at) VALUES ('collision-audit', 'card.comment', '{}'::jsonb, "
            "'committed', true, 'card', 'butler-1', now(), now())"
        )
        connection.exec_driver_sql(
            "INSERT INTO board_events (event_id, request_id, kind, entity_kind, ref, actor_role, "
            "actor_id, reason, occurred_at, committed, committed_at) VALUES "
            "('collision-event', 'collision-audit', 'entity.updated', 'card', 'butler-1', "
            "'worker', 'fixture', 'commented', now(), true, now())"
        )
        connection.commit()

        self.assertEqual(
            self.run_migrations(connection),
            (
                "0007_card_transport_key",
                "0008_po_sessions",
                "0009_po_requests",
                "0010_po_session_close",
                "0011_card_kinds",
                "0012_request_read_indexes",
                "0013_budget_candidates",
                "0014_neutral_extension_bag",
                "0015_po_effort_resolved_model",
                "0016_sprint_po_session",
                "0017_po_card_kinds",
                "0018_owner_events",
                "0019_po_session_title",
                "0020_wait_card_kind",
                "0021_delegated_card_settled",
                "0022_origin_returns",
                "0023_sprint_e2e_budget",
                "0024_e2e_after_merge_kind",
                "0025_card_waits_for_person",
                "0026_sprint_local_runs",
                "0027_sprint_owner_decisions",
            ),
        )
        rows = connection.exec_driver_sql(
            "SELECT task_ref, project_id, task_number, board_key FROM tasks ORDER BY task_ref"
        ).fetchall()
        self.assertEqual(
            [(row[0], row[1], row[2]) for row in rows],
            [
                ("butler-1", "butler", 1),
                ("codegen-product-kit-1", "codegen-product-kit", 1),
            ],
        )
        self.assertEqual(len({row[3] for row in rows}), 2)
        self.assertEqual(
            connection.exec_driver_sql("SELECT task_ref, marker, body FROM task_comments").fetchall(),
            [("butler-1", "note", "butler comment")],
        )
        self.assertEqual(
            connection.exec_driver_sql(
                "SELECT task_ref, depends_on, depends_on_task FROM task_dependencies"
            ).fetchall(),
            [("codegen-product-kit-1", "butler-1", "butler-1")],
        )
        self.assertEqual(
            connection.exec_driver_sql(
                "SELECT request_id, requests.ref FROM requests JOIN board_events USING (request_id)"
            ).fetchall(),
            [("collision-audit", "butler-1")],
        )
        fresh = connection.exec_driver_sql(
            "INSERT INTO tasks (task_ref, project_id, task_number, title, task_type, state, "
            "created_at, updated_at) VALUES ('butler-2', 'butler', 2, 'fresh', 'code', 'ready', "
            "now(), now()) RETURNING board_key"
        ).scalar_one()
        self.assertNotIn(fresh, {row[3] for row in rows})

    def test_issue_and_product_comments_have_entity_scoped_tables(self) -> None:
        """Product writes now need the same durable comment shape already used by Issues."""
        connection = self.prepared()

        connection.exec_driver_sql(
            "INSERT INTO issue_comments (issue_id, marker, body, actor_role, created_at) "
            "VALUES ('2fdac531', 'issue:closed', 'closed as resolved', 'po', now())"
        )

        self.assertEqual(
            connection.exec_driver_sql(
                "SELECT issue_id, marker, body, issue_ref FROM issue_comments"
            ).fetchall(),
            [("2fdac531", "issue:closed", "closed as resolved", "issue:2fdac531")],
        )
        connection.exec_driver_sql(
            "INSERT INTO product_comments (product_id, marker, body, actor_role, created_at) "
            "VALUES ('ummanu', 'product:note', 'product note', 'po', now())"
        )
        self.assertEqual(
            connection.exec_driver_sql(
                "SELECT product_id, marker, body, product_ref FROM product_comments"
            ).fetchall(),
            [("ummanu", "product:note", "product note", "product:ummanu")],
        )

    def test_an_issue_keeps_the_metadata_keys_the_model_does_not_name(self) -> None:
        """AC 2: nine metadata keys ride on 158 Issue rows, and a lane that is not the product's."""
        connection = self.prepared()

        connection.exec_driver_sql(
            "UPDATE issues SET extensions = %s::jsonb WHERE issue_id = '2fdac531'",
            ('{"extra": {"slug": "an-issue", "swimlane": "Codegen"}}',),
        )

        stored, default = connection.exec_driver_sql(
            "SELECT (SELECT extensions FROM issues), "
            "(SELECT column_default FROM information_schema.columns "
            " WHERE table_name = 'issues' AND column_name = 'extensions')"
        ).fetchone()
        self.assertEqual(stored["extra"]["swimlane"], "Codegen")
        self.assertIn("'{}'::jsonb", default)

    def test_a_sprint_reference_that_carries_no_number_is_a_row_and_keeps_its_cards(self) -> None:
        """AC 3: both canary sprints, and the two cards that lost their link to them."""
        connection = self.prepared()

        self.sprint(connection, "sprint:canary-terra-20260813", None)
        self.sprint(connection, "sprint:canary-terra-final-20260813", None)
        self.sprint(connection, "sprint:1037", 1037)
        self.card(connection, "ummanu-1438", sprint="sprint:canary-terra-20260813")
        self.card(connection, "ummanu-1439", sprint="sprint:canary-terra-final-20260813")

        self.assertEqual(
            connection.exec_driver_sql("SELECT task_ref, sprint_ref FROM tasks ORDER BY task_ref").fetchall(),
            [
                ("ummanu-1438", "sprint:canary-terra-20260813"),
                ("ummanu-1439", "sprint:canary-terra-final-20260813"),
            ],
        )
        self.assertEqual(
            connection.exec_driver_sql(
                "SELECT ref FROM sprints WHERE sprint_number IS NULL ORDER BY ref"
            ).fetchall(),
            [("sprint:canary-terra-20260813",), ("sprint:canary-terra-final-20260813",)],
        )

    def test_a_product_keeps_metadata_the_relational_model_does_not_name(self) -> None:
        connection = self.prepared()
        connection.exec_driver_sql(
            "UPDATE products SET extensions = %s::jsonb WHERE product_id = 'ummanu'",
            ('{"extra": {"future_product_field": "kept"}}',),
        )
        self.assertEqual(
            connection.exec_driver_sql(
                "SELECT extensions->'extra'->>'future_product_field' FROM products "
                "WHERE product_id = 'ummanu'"
            ).fetchone()[0],
            "kept",
        )

    def test_the_number_and_the_reference_may_not_disagree(self) -> None:
        """§9's spelling, as a constraint: a numbered reference carries exactly its number."""
        import sqlalchemy as sa

        connection = self.prepared()

        for ref, number in (("sprint:1037", 42), ("sprint:1037", None), ("sprint:x", 7)):
            with self.subTest(ref=ref, number=number):
                with self.assertRaises(sa.exc.IntegrityError):
                    self.sprint(connection, ref, number)
                connection.rollback()

    def test_one_reference_is_still_one_sprint(self) -> None:
        """The other half of §9: `sprints.ref` is the key, so a duplicate is refused, not stored.

        This is the finding the report names — `sprint:1037` is on the board twice, a live row and
        an archived one — recorded here as what the schema actually does with it.
        """
        import sqlalchemy as sa

        connection = self.prepared()
        self.sprint(connection, "sprint:1037", 1037)

        with self.assertRaises(sa.exc.IntegrityError):
            self.sprint(connection, "sprint:1037", 1037)
        connection.rollback()

    def test_the_sprint_cursor_is_still_scoped_to_its_own_sprint(self) -> None:
        """AC 3: the composite keys move to the reference without losing their reach."""
        import sqlalchemy as sa

        connection = self.prepared()
        self.sprint(connection, "sprint:1", 1)
        self.sprint(connection, "sprint:2", 2)
        self.card(connection, "ummanu-1", sprint="sprint:2")
        connection.commit()

        with self.assertRaises(sa.exc.IntegrityError):
            connection.exec_driver_sql(
                "UPDATE sprints SET current_task_ref = 'ummanu-1' WHERE ref = 'sprint:1'"
            )
            connection.commit()
        connection.rollback()

        connection.exec_driver_sql(
            "UPDATE sprints SET current_task_ref = 'ummanu-1' WHERE ref = 'sprint:2'"
        )
        connection.commit()
        self.assertEqual(
            connection.exec_driver_sql(
                "SELECT current_task_ref FROM sprints WHERE ref = 'sprint:2'"
            ).fetchone()[0],
            "ummanu-1",
        )

    def test_section_9s_allocator_still_hands_out_numbers(self) -> None:
        connection = self.prepared()

        connection.exec_driver_sql("SELECT setval('sprint_number_seq', 1037)")
        number = connection.exec_driver_sql("SELECT nextval('sprint_number_seq')").fetchone()[0]
        self.sprint(connection, f"sprint:{number}", number)

        self.assertEqual(number, 1038)
        self.assertEqual(
            connection.exec_driver_sql("SELECT ref FROM sprints WHERE sprint_number = 1038").fetchone()[0],
            "sprint:1038",
        )

    def test_a_card_whose_metadata_names_no_project_is_still_a_row(self) -> None:
        """AC 5: `secretary-583` carries no `project`, and the board holds it anyway."""
        connection = self.prepared()

        self.card(connection, "ummanu-583", project=None)

        self.assertEqual(
            connection.exec_driver_sql(
                "SELECT project_id FROM tasks WHERE task_ref = 'ummanu-583'"
            ).fetchone()[0],
            None,
        )

    def test_a_card_the_board_never_gave_a_type_is_still_a_row(self) -> None:
        """AC 1: `secretary-583` carries no `task_type` either, and 0003 keeps it (§8.6)."""
        import sqlalchemy as sa

        connection = self.prepared()

        self.card(
            connection,
            "ummanu-583",
            project=None,
            task_type=None,
            extensions='{"board_never_named": ["task_type"]}',
        )
        connection.commit()

        self.assertEqual(
            connection.exec_driver_sql(
                "SELECT task_type, extensions FROM tasks WHERE task_ref = 'ummanu-583'"
            ).fetchone(),
            (None, {"board_never_named": ["task_type"]}),
        )
        # The vocabulary is still closed: NULL was added to what the column admits, not "any text".
        with self.assertRaises(sa.exc.IntegrityError):
            self.card(connection, "ummanu-584", task_type="chore")
        connection.rollback()

    def test_0011_keeps_existing_cards_and_admits_the_new_kind_and_fields(self) -> None:
        """secretary-1638: `code`, `research` and typeless rows survive; `infra` and the two fields land."""
        import sqlalchemy as sa
        from alembic import command

        connection = self.owner_connection()
        command.upgrade(
            migrate.alembic_config(connection=connection, passwords=self.passwords), "0010_po_session_close"
        )
        connection.commit()
        connection.exec_driver_sql("INSERT INTO projects (project_id) VALUES ('ummanu')")
        self.card(connection, "ummanu-1", task_type="code")
        self.card(connection, "ummanu-2", task_type="research")
        self.card(connection, "ummanu-3", task_type=None)
        connection.commit()
        with self.assertRaises(sa.exc.IntegrityError):
            self.card(connection, "ummanu-4", task_type="infra")
        connection.rollback()

        self.assertEqual(
            self.run_migrations(connection),
            (
                "0011_card_kinds",
                "0012_request_read_indexes",
                "0013_budget_candidates",
                "0014_neutral_extension_bag",
                "0015_po_effort_resolved_model",
                "0016_sprint_po_session",
                "0017_po_card_kinds",
                "0018_owner_events",
                "0019_po_session_title",
                "0020_wait_card_kind",
                "0021_delegated_card_settled",
                "0022_origin_returns",
                "0023_sprint_e2e_budget",
                "0024_e2e_after_merge_kind",
                "0025_card_waits_for_person",
                "0026_sprint_local_runs",
                "0027_sprint_owner_decisions",
            ),
        )

        self.assertEqual(
            connection.exec_driver_sql(
                "SELECT task_ref, task_type, review, live_impact FROM tasks ORDER BY task_ref"
            ).fetchall(),
            [
                ("ummanu-1", "code", None, False),
                ("ummanu-2", "research", None, False),
                ("ummanu-3", None, None, False),
            ],
        )
        self.card(connection, "ummanu-4", task_type="infra")
        connection.exec_driver_sql(
            "UPDATE tasks SET review = 'skipped', live_impact = true WHERE task_ref = 'ummanu-2'"
        )
        connection.commit()
        for statement in (
            "UPDATE tasks SET review = 'sometimes' WHERE task_ref = 'ummanu-1'",
            "UPDATE tasks SET live_impact = true WHERE task_ref = 'ummanu-4'",
            "UPDATE tasks SET live_impact = true WHERE task_ref = 'ummanu-3'",
        ):
            with self.subTest(statement=statement), self.assertRaises(sa.exc.IntegrityError):
                connection.exec_driver_sql(statement)
            connection.rollback()

    def test_a_dependency_on_a_card_the_board_does_not_hold_is_kept(self) -> None:
        """AC 5: nine `blocked_by` values name cards that are not on this board."""
        import sqlalchemy as sa

        connection = self.prepared()
        self.card(connection, "ummanu-1584")
        self.card(connection, "ummanu-1583")

        connection.exec_driver_sql(
            "INSERT INTO task_dependencies (task_ref, depends_on, depends_on_task) VALUES "
            "('ummanu-1584', 'memory-mcp-12', NULL), "
            "('ummanu-1584', 'ummanu-1583', 'ummanu-1583')"
        )
        connection.commit()

        self.assertEqual(
            connection.exec_driver_sql(
                "SELECT depends_on, depends_on_task FROM task_dependencies ORDER BY depends_on"
            ).fetchall(),
            [("memory-mcp-12", None), ("ummanu-1583", "ummanu-1583")],
        )
        # The foreign key still means what it meant: a resolution that names another card, or a
        # card that is not there, is refused rather than silently kept.
        for depends_on, resolved in (
            ("ummanu-1583", "ummanu-1582"),
            ("triggered-agents-9", "triggered-agents-9"),
        ):
            with self.subTest(depends_on=depends_on):
                with self.assertRaises(sa.exc.IntegrityError):
                    connection.exec_driver_sql(
                        "INSERT INTO task_dependencies (task_ref, depends_on, depends_on_task) "
                        "VALUES ('ummanu-1583', %s, %s)",
                        (depends_on, resolved),
                    )
                connection.rollback()

    # --- §7.4's lock -------------------------------------------------------------------

    def test_the_advisory_lock_makes_a_second_runner_wait_for_the_first(self) -> None:
        import psycopg

        holder = psycopg.connect(self.credentials("owner").conninfo(), autocommit=True)
        self.addCleanup(holder.close)
        holder.execute("SELECT pg_advisory_lock(%s)", (migrate.ADVISORY_LOCK_KEY,))
        connection = self.owner_connection()
        finished = threading.Event()

        def run() -> None:
            self.run_migrations(connection)
            finished.set()

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        try:
            self.assertFalse(
                finished.wait(1.5),
                "the run must contend on the fixed advisory key, not migrate concurrently",
            )
        finally:
            holder.execute("SELECT pg_advisory_unlock(%s)", (migrate.ADVISORY_LOCK_KEY,))
        self.assertTrue(finished.wait(60), "the runner never acquired the released lock")
        worker.join(timeout=5)
        self.assertEqual(connection.exec_driver_sql("SELECT count(*) FROM alembic_version").fetchone()[0], 1)

    def test_the_lock_is_released_once_the_run_is_done(self) -> None:
        connection = self.owner_connection()
        self.run_migrations(connection)

        held = connection.exec_driver_sql(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted"
        ).fetchone()[0]

        self.assertEqual(held, 0)

    # --- §5.5's roles ------------------------------------------------------------------

    def test_the_revision_creates_the_two_roles_and_the_boundary_between_them(self) -> None:
        """§5.5: the writer writes, the reader cannot, and neither of them owns any DDL."""
        import sqlalchemy as sa

        connection = self.owner_connection()
        self.run_migrations(connection)
        connection.exec_driver_sql(
            "INSERT INTO products (product_id, board_key, title, created_at, updated_at) "
            "VALUES ('ummanu', %s, 'Ummanu', now(), now())",
            (record_key("product", "ummanu"),),
        )
        connection.commit()

        with self.engine("app").connect() as app:
            app.exec_driver_sql(
                "INSERT INTO products (product_id, board_key, title, created_at, updated_at) "
                "VALUES ('written-by-app', %s, 'App', now(), now())",
                (record_key("product", "written-by-app"),),
            )
            app.commit()
            with self.assertRaises(sa.exc.ProgrammingError):
                app.exec_driver_sql("CREATE TABLE forbidden (a int)")

        with self.engine("read").connect() as reader:
            self.assertEqual(reader.exec_driver_sql("SELECT count(*) FROM products").fetchone()[0], 2)
            with self.assertRaises(sa.exc.ProgrammingError):
                reader.exec_driver_sql(
                    "INSERT INTO products (product_id, board_key, title, created_at, updated_at) "
                    "VALUES ('written-by-read', %s, 'Read', now(), now())",
                    (record_key("product", "written-by-read"),),
                )

    def test_a_table_a_later_revision_adds_is_reachable_without_a_further_grant(self) -> None:
        """The `ALTER DEFAULT PRIVILEGES` half of §5.5, which is the half that fails late."""
        connection = self.owner_connection()
        self.run_migrations(connection)

        connection.exec_driver_sql("CREATE TABLE later_table AS SELECT 1 AS a")
        connection.commit()

        with self.engine("app").connect() as app:
            app.exec_driver_sql("INSERT INTO later_table (a) VALUES (2)")
            app.commit()
        with self.engine("read").connect() as reader:
            self.assertEqual(reader.exec_driver_sql("SELECT count(*) FROM later_table").fetchone()[0], 2)

    def test_the_late_product_comment_table_has_app_and_read_role_grants(self) -> None:
        connection = self.owner_connection()
        self.run_migrations(connection)
        connection.exec_driver_sql(
            "INSERT INTO products (product_id, board_key, title, created_at, updated_at) "
            "VALUES ('ummanu', %s, 'Ummanu', now(), now())",
            (record_key("product", "ummanu"),),
        )
        connection.commit()

        with self.engine("app").connect() as app:
            app.exec_driver_sql(
                "INSERT INTO product_comments (product_id, body, created_at) "
                "VALUES ('ummanu', 'from app', now())"
            )
            app.commit()
        with self.engine("read").connect() as reader:
            self.assertEqual(
                reader.exec_driver_sql("SELECT body FROM product_comments").fetchall(),
                [("from app",)],
            )

    # --- `step_board_store` end to end -------------------------------------------------
    #
    # The unit suite proves the step's outcomes over a stubbed runner. These prove the wire
    # between them is real: that a complete `board-store.env` in an instance directory is what the
    # step resolves, connects with and migrates through, and that a second upgrade changes
    # nothing. They live in this class so one container serves the whole module.

    def write_store(self, directory: Path) -> Path:
        path = directory / "board-store.env"
        path.write_text(
            "".join(f"{key}={value}\n" for key, value in self.config().as_environ().items()),
            encoding="utf-8",
        )
        path.chmod(0o600)
        return path

    def context(self, instance: Path, *, dry_run: bool = False):
        return upgrade.UpgradeContext(
            instance_path=instance,
            product_root=instance,
            base_branch="main",
            dry_run=dry_run,
            units=None,
        )

    def test_an_upgrade_migrates_a_configured_store_and_then_leaves_it_alone(self) -> None:
        with TemporaryDirectory() as tmp:
            instance = Path(tmp)
            self.write_store(instance)

            preview = upgrade.step_board_store(self.context(instance, dry_run=True))
            self.assertEqual(preview.status, "would-change")
            self.assertIn("0001", preview.detail)
            self.assertIn("0002", preview.detail)

            connection = self.owner_connection()
            self.assertIsNone(
                connection.exec_driver_sql("SELECT to_regclass('public.alembic_version')").fetchone()[0],
                "a dry run must read and write nothing",
            )
            connection.close()

            applied = upgrade.step_board_store(self.context(instance))
            self.assertEqual(applied.status, "changed")
            self.assertIn("0001", applied.detail)
            self.assertIn("0002", applied.detail)

            again = upgrade.step_board_store(self.context(instance))
            self.assertEqual(again.status, "unchanged")

        self.assertEqual(self.counts(self.owner_connection()), DOCUMENTED_COUNTS)

    def test_a_store_that_will_not_answer_fails_the_step_with_its_reason(self) -> None:
        with TemporaryDirectory() as tmp:
            instance = Path(tmp)
            path = self.write_store(instance)
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    f"UMMANU_DB_OWNER_PASSWORD={OWNER_PASSWORD}",
                    "UMMANU_DB_OWNER_PASSWORD=not-the-password",
                ),
                encoding="utf-8",
            )
            path.chmod(0o600)

            result = upgrade.step_board_store(self.context(instance))

        self.assertTrue(result.failed)
        self.assertIn("board store", result.detail)

    def test_compose_provision_migrate_roles_and_rerun_on_disposable_volume(self) -> None:
        """The delivery boundary itself, not a hand-built equivalent container."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            compose = root / "postgres-compose.yml"
            project = f"ummanu-provision-{root.name.lower()}"
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                host_port = listener.getsockname()[1]
            config_path = root / "board-store.env"
            config_path.write_text(
                "\n".join(
                    (
                        "UMMANU_DB_HOST=127.0.0.1",
                        f"UMMANU_DB_PORT={host_port}",
                        f"UMMANU_DB_NAME={DATABASE}",
                        f"UMMANU_DB_OWNER_USER={OWNER}",
                        f"UMMANU_DB_OWNER_PASSWORD={OWNER_PASSWORD}",
                        f"UMMANU_DB_APP_USER={schema.APP_ROLE}",
                        f"UMMANU_DB_APP_PASSWORD={APP_PASSWORD}",
                        f"UMMANU_DB_READ_USER={schema.READ_ROLE}",
                        f"UMMANU_DB_READ_PASSWORD={READ_PASSWORD}",
                        "",
                    )
                ),
                encoding="utf-8",
            )
            config_path.chmod(0o600)

            completed = False

            def cleanup() -> None:
                cleanup_test_project(project, container_expected=completed)

            try:
                first = provision.provision(
                    root,
                    compose_path=compose,
                    project=project,
                    test_owner_pid=os.getpid(),
                )
                completed = True
                self.assertIsNotNone(first)
                self.assertTrue(first.changed)
                secret_values = list(provision.store.resolve(root).as_environ().values())[4::2]
                report = first.render()
                self.assertTrue(all(secret not in report for secret in secret_values))

                self.assertEqual(migrate.migrate_instance(root), REVISIONS)
                provision.verify_roles(root)
                import sqlalchemy as sa

                engine = sa.create_engine(migrate.sqlalchemy_url(provision.store.resolve_role(root, "owner")))
                try:
                    with engine.connect() as connection:
                        self.assertEqual(migrate.current_revision(connection), migrate.head_revision())
                finally:
                    engine.dispose()

                second = provision.provision(root, compose_path=compose, project=project,
                                             test_owner_pid=os.getpid())
                self.assertIsNotNone(second)
                self.assertFalse(second.changed)
                self.assertEqual(migrate.migrate_instance(root), ())
                self.assertTrue(docker("volume", "inspect", f"{project}_board-db"))
            finally:
                cleanup()

    # --- 0014: the extension bag under its neutral key ------------------------------------------
    #
    # The revision never spells the key it moves: it moves whichever single top-level key besides
    # `extra` and the markers the rows carry.  So the seeded pre-migration shape names its bag
    # `retired_board`, which stands for the retired board's own name exactly as the revision sees it.

    def at_0013(self):
        """A store at `0013_budget_candidates` with a Product, an Issue and a project."""
        from alembic import command

        connection = self.owner_connection()
        command.upgrade(
            migrate.alembic_config(connection=connection, passwords=self.passwords),
            "0013_budget_candidates",
        )
        connection.commit()
        connection.exec_driver_sql("INSERT INTO projects (project_id) VALUES ('ummanu')")
        connection.exec_driver_sql(
            "INSERT INTO products (product_id, board_key, title, extensions, created_at, updated_at) "
            "VALUES ('ummanu', %s, 'Ummanu', %s::jsonb, now(), now())",
            (record_key("product", "ummanu"), '{"retired_board": {"future_product_field": "kept"}}'),
        )
        connection.exec_driver_sql(
            "INSERT INTO issues (issue_id, board_key, product_id, title, issue_kind, priority, "
            "extensions, created_at, updated_at) VALUES ('2fdac531', %s, 'ummanu', 'An issue', "
            "'bug', 'P1', %s::jsonb, now(), now())",
            (record_key("issue", "2fdac531"), '{"retired_board": {"slug": "an-issue", "swimlane": "Codegen"}}'),
        )
        connection.commit()
        return connection

    def request(self, connection, request_id: str, status: str) -> None:
        settled = "now()" if status != "staged" else "NULL"
        connection.exec_driver_sql(
            "INSERT INTO requests (request_id, operation, intent, status, protocol, entity_kind, "
            f"ref, created_at, settled_at) VALUES (%s, 'card.retire', '{{}}'::jsonb, %s, true, "
            f"'card', 'ummanu-1', now(), {settled})",
            (request_id, status),
        )

    def extensions_of(self, connection) -> dict[str, object]:
        rows = connection.exec_driver_sql(
            "SELECT 'task:' || task_ref, extensions FROM tasks "
            "UNION ALL SELECT 'product:' || product_id, extensions FROM products "
            "UNION ALL SELECT 'issue:' || issue_id, extensions FROM issues"
        ).fetchall()
        return {row[0]: row[1] for row in rows}

    def test_0014_is_what_a_dry_run_of_a_0013_store_owes(self) -> None:
        connection = self.at_0013()

        self.assertEqual(
            self.run_migrations(connection, dry_run=True),
            (
                "0014_neutral_extension_bag",
                "0015_po_effort_resolved_model",
                "0016_sprint_po_session",
                "0017_po_card_kinds",
                "0018_owner_events",
                "0019_po_session_title",
                "0020_wait_card_kind",
                "0021_delegated_card_settled",
                "0022_origin_returns",
                "0023_sprint_e2e_budget",
                "0024_e2e_after_merge_kind",
                "0025_card_waits_for_person",
                "0026_sprint_local_runs",
                "0027_sprint_owner_decisions",
            ),
        )
        self.assertEqual(migrate.current_revision(connection), "0013_budget_candidates")

    def test_0014_moves_every_current_bag_onto_the_neutral_key_and_loses_nothing(self) -> None:
        connection = self.at_0013()
        self.card(
            connection,
            "ummanu-1",
            extensions='{"retired_board": {"swimlane": "ummanu", "steward_report": "1"}}',
        )
        # `secretary-583`'s live shape: the bag and the importer's marker beside it.
        self.card(
            connection,
            "ummanu-583",
            task_type=None,
            extensions='{"retired_board": {"swimlane": "ummanu"}, "board_never_named": ["task_type"]}',
        )
        self.card(connection, "ummanu-2")
        # Both keys present: the two bags merge, and a field both name with one value is kept once.
        self.card(
            connection,
            "ummanu-3",
            extensions='{"retired_board": {"note": "old", "same": "1"}, "extra": {"fresh": "new", "same": "1"}}',
        )
        # A committed done-retention record is history; it does not stop the revision.
        self.request(connection, "done-retention-committed", "committed")
        connection.commit()

        self.assertEqual(
            self.run_migrations(connection),
            (
                "0014_neutral_extension_bag",
                "0015_po_effort_resolved_model",
                "0016_sprint_po_session",
                "0017_po_card_kinds",
                "0018_owner_events",
                "0019_po_session_title",
                "0020_wait_card_kind",
                "0021_delegated_card_settled",
                "0022_origin_returns",
                "0023_sprint_e2e_budget",
                "0024_e2e_after_merge_kind",
                "0025_card_waits_for_person",
                "0026_sprint_local_runs",
                "0027_sprint_owner_decisions",
            ),
        )

        self.assertEqual(
            self.extensions_of(connection),
            {
                "task:ummanu-1": {"extra": {"swimlane": "ummanu", "steward_report": "1"}},
                "task:ummanu-583": {
                    "extra": {"swimlane": "ummanu"},
                    "board_never_named": ["task_type"],
                },
                "task:ummanu-2": {},
                "task:ummanu-3": {"extra": {"note": "old", "fresh": "new", "same": "1"}},
                "product:ummanu": {"extra": {"future_product_field": "kept"}},
                "issue:2fdac531": {"extra": {"slug": "an-issue", "swimlane": "Codegen"}},
            },
        )
        # History is not rewritten.
        self.assertEqual(
            connection.exec_driver_sql("SELECT status FROM requests").fetchall(), [("committed",)]
        )

    def assertRefused(self, connection, *fragments: str) -> None:
        before = self.extensions_of(connection)
        with self.assertRaises(Exception) as caught:
            self.run_migrations(connection)
        connection.rollback()
        for fragment in fragments:
            self.assertIn(fragment, str(caught.exception))
        self.assertEqual(migrate.current_revision(connection), "0013_budget_candidates")
        self.assertEqual(self.extensions_of(connection), before)

    def test_0014_refuses_a_store_with_two_keys_besides_the_neutral_one(self) -> None:
        connection = self.at_0013()
        self.card(connection, "ummanu-1", extensions='{"unexpected": {"a": "1"}}')
        connection.commit()

        self.assertRefused(connection, "'retired_board'", "'unexpected'")

    def test_0014_refuses_a_field_both_bags_name_with_different_values(self) -> None:
        connection = self.at_0013()
        self.card(
            connection,
            "ummanu-1",
            extensions='{"retired_board": {"note": "old"}, "extra": {"note": "new"}}',
        )
        connection.commit()

        self.assertRefused(connection, "tasks:ummanu-1:note")

    def test_0014_refuses_a_bag_that_is_not_an_object(self) -> None:
        connection = self.at_0013()
        self.card(connection, "ummanu-1", extensions='{"retired_board": "flat"}')
        connection.commit()

        self.assertRefused(connection, "tasks:ummanu-1")

    def test_0014_refuses_a_non_committed_done_retention_request_and_names_it(self) -> None:
        connection = self.at_0013()
        self.request(connection, "done-retention-staged", "staged")
        connection.commit()

        self.assertRefused(connection, "done-retention-staged:staged")

    def test_0014_admits_a_store_with_no_done_retention_request_at_all(self) -> None:
        connection = self.at_0013()
        self.request(connection, "card-move-staged", "staged")
        connection.commit()

        self.assertEqual(
            self.run_migrations(connection),
            (
                "0014_neutral_extension_bag",
                "0015_po_effort_resolved_model",
                "0016_sprint_po_session",
                "0017_po_card_kinds",
                "0018_owner_events",
                "0019_po_session_title",
                "0020_wait_card_kind",
                "0021_delegated_card_settled",
                "0022_origin_returns",
                "0023_sprint_e2e_budget",
                "0024_e2e_after_merge_kind",
                "0025_card_waits_for_person",
                "0026_sprint_local_runs",
                "0027_sprint_owner_decisions",
            ),
        )

    def test_0014_has_no_downgrade(self) -> None:
        from alembic import command

        connection = self.at_0013()
        self.assertEqual(
            self.run_migrations(connection),
            (
                "0014_neutral_extension_bag",
                "0015_po_effort_resolved_model",
                "0016_sprint_po_session",
                "0017_po_card_kinds",
                "0018_owner_events",
                "0019_po_session_title",
                "0020_wait_card_kind",
                "0021_delegated_card_settled",
                "0022_origin_returns",
                "0023_sprint_e2e_budget",
                "0024_e2e_after_merge_kind",
                "0025_card_waits_for_person",
                "0026_sprint_local_runs",
                "0027_sprint_owner_decisions",
            ),
        )

        with self.assertRaisesRegex(NotImplementedError, "forward-only"):
            command.downgrade(
                migrate.alembic_config(connection=connection, passwords=self.passwords),
                "0013_budget_candidates",
            )
        connection.rollback()
        self.assertEqual(migrate.current_revision(connection), "0018_owner_events")

    # --- 0015: a PO session's effort and each turn's resolved model -----------------------------

    def test_0015_opens_every_existing_session_at_the_default_effort(self) -> None:
        connection = self.at_0013()
        connection.exec_driver_sql(
            "INSERT INTO po_sessions (session_id, cli, model, cwd, created_at, state) "
            "VALUES ('s-1', 'claude', 'opus', '/po', now(), 'open')"
        )
        connection.exec_driver_sql(
            "INSERT INTO po_turns (session_id, seq, started_at, finished_at, state, stdout_path) "
            "VALUES ('s-1', 1, now(), now(), 'completed', '/runs/turn-0001.stdout')"
        )
        connection.commit()

        self.run_migrations(connection)

        self.assertEqual(
            connection.exec_driver_sql("SELECT effort FROM po_sessions").fetchall(), [("default",)]
        )
        self.assertEqual(
            connection.exec_driver_sql("SELECT resolved_model FROM po_turns").fetchall(), [(None,)]
        )

    # --- 0016: a sprint's PO session and the productions it may touch ---------------------------

    def test_0016_loads_every_existing_sprint_with_no_po_session_and_no_production(self) -> None:
        connection = self.at_0013()
        connection.exec_driver_sql(
            "INSERT INTO sprints (ref, board_key, sprint_number, goal, definition_of_done, product_id, "
            "status, created_at, updated_at) "
            "VALUES ('sprint:7', %s, 7, 'goal', 'done', 'ummanu', 'open', now(), now())",
            (record_key("sprint", "sprint:7"),),
        )
        connection.commit()

        self.run_migrations(connection)

        self.assertEqual(
            connection.exec_driver_sql("SELECT po_session, allowed_productions FROM sprints").fetchall(),
            [(None, [])],
        )

    def test_0016_admits_the_sprint_session_operation_and_nothing_else_new(self) -> None:
        connection = self.at_0013()
        self.run_migrations(connection)
        connection.exec_driver_sql(
            "INSERT INTO po_sessions (session_id, cli, model, cwd, created_at, state) "
            "VALUES ('s-1', 'claude', 'opus', '/po', now(), 'open')"
        )
        connection.exec_driver_sql(
            "INSERT INTO po_requests (request_id, operation, fingerprint, session_id, seq, created_at) "
            "VALUES ('r-1', 'po_sprint_session', 'f', 's-1', NULL, now())"
        )
        connection.commit()
        with self.assertRaisesRegex(Exception, "po_request_operation_in_vocabulary"):
            connection.exec_driver_sql(
                "INSERT INTO po_requests (request_id, operation, fingerprint, session_id, seq, created_at) "
                "VALUES ('r-2', 'po_something_else', 'f', 's-1', NULL, now())"
            )
        connection.rollback()
        # A sprint-session request records no turn: `po_request_seq_only_for_a_send` is unchanged.
        with self.assertRaisesRegex(Exception, "po_request_seq_only_for_a_send"):
            connection.exec_driver_sql(
                "INSERT INTO po_requests (request_id, operation, fingerprint, session_id, seq, created_at) "
                "VALUES ('r-3', 'po_sprint_session', 'f', 's-1', 1, now())"
            )
        connection.rollback()

    # --- 0017: the PO-executed card kinds ----------------------------------------------------

    def test_0017_keeps_every_existing_card_and_admits_decision_and_operation(self) -> None:
        """secretary-1758: rows of the three old kinds and the typeless row load unchanged."""
        import sqlalchemy as sa
        from alembic import command

        connection = self.owner_connection()
        command.upgrade(
            migrate.alembic_config(connection=connection, passwords=self.passwords), "0016_sprint_po_session"
        )
        connection.commit()
        connection.exec_driver_sql("INSERT INTO projects (project_id) VALUES ('ummanu')")
        for number, kind in enumerate(("code", "research", "infra", None), start=1):
            self.card(connection, f"ummanu-{number}", task_type=kind)
        connection.exec_driver_sql("UPDATE tasks SET review = 'skipped' WHERE task_ref = 'ummanu-3'")
        connection.commit()
        with self.assertRaises(sa.exc.IntegrityError):
            self.card(connection, "ummanu-5", task_type="decision")
        connection.rollback()

        self.assertEqual(
            self.run_migrations(connection),
            ("0017_po_card_kinds", "0018_owner_events", "0019_po_session_title", "0020_wait_card_kind", "0021_delegated_card_settled", "0022_origin_returns", "0023_sprint_e2e_budget", "0024_e2e_after_merge_kind", "0025_card_waits_for_person", "0026_sprint_local_runs", "0027_sprint_owner_decisions"),
        )

        self.assertEqual(
            connection.exec_driver_sql(
                "SELECT task_ref, task_type, review, live_impact FROM tasks ORDER BY task_ref"
            ).fetchall(),
            [
                ("ummanu-1", "code", None, False),
                ("ummanu-2", "research", None, False),
                ("ummanu-3", "infra", "skipped", False),
                ("ummanu-4", None, None, False),
            ],
        )
        self.card(connection, "ummanu-5", task_type="decision")
        self.card(connection, "ummanu-6", task_type="operation")
        connection.commit()
        with self.assertRaisesRegex(Exception, "task_type_is_a_known_type_or_nothing"):
            self.card(connection, "ummanu-7", task_type="chore")
        connection.rollback()
        # Live impact stays research-only for the new kinds too.
        with self.assertRaisesRegex(Exception, "task_live_impact_is_research_only"):
            connection.exec_driver_sql("UPDATE tasks SET live_impact = true WHERE task_ref = 'ummanu-5'")
        connection.rollback()

    # --- 0018: owner events ---------------------------------------------------------------------

    def test_0018_adds_owner_events_and_every_pre_0018_row_loads_unchanged(self) -> None:
        """secretary-1770: a store at 0017 keeps its sprints and cards; the writer, dedup and mark-read work."""
        import sqlalchemy as sa
        from alembic import command

        from ummanu.board.owner_events import OwnerEventStore, ReadRefused, record, settle

        connection = self.owner_connection()
        command.upgrade(
            migrate.alembic_config(connection=connection, passwords=self.passwords), "0017_po_card_kinds"
        )
        connection.commit()
        connection.exec_driver_sql("INSERT INTO projects (project_id) VALUES ('ummanu')")
        self.sprint(connection, "sprint:5", 5)
        mark = '{"extra": {"waiting_owner": "2026-09-26T15:00:00Z", "waiting_owner_reason": "pay", "waiting_owner_by": "po"}}'
        self.card(connection, "ummanu-1", sprint="sprint:5", task_type="decision", extensions=mark)
        self.card(connection, "ummanu-2", task_type="code")
        connection.commit()
        before = (
            connection.exec_driver_sql("SELECT * FROM sprints ORDER BY ref").fetchall(),
            connection.exec_driver_sql("SELECT * FROM tasks ORDER BY task_ref").fetchall(),
        )
        store = OwnerEventStore(self.credentials("app"))
        with self.assertLogs("ummanu.board.owner_events", level="WARNING"):
            self.assertFalse(record("sprint_closed", "sprint:5", "closed", "early", to=store))

        self.assertEqual(self.run_migrations(connection), ("0018_owner_events", "0019_po_session_title", "0020_wait_card_kind", "0021_delegated_card_settled", "0022_origin_returns", "0023_sprint_e2e_budget", "0024_e2e_after_merge_kind", "0025_card_waits_for_person", "0026_sprint_local_runs", "0027_sprint_owner_decisions"))

        # Every old column loads unchanged; 0023 adds budget (3 and 0), 0026 no exceptions, 0027 no quotes.
        sprints = connection.exec_driver_sql("SELECT * FROM sprints ORDER BY ref").fetchall()
        self.assertEqual(
            (
                [row[: len(before[0][0])] for row in sprints],
                connection.exec_driver_sql("SELECT * FROM tasks ORDER BY task_ref").fetchall(),
            ),
            before,
        )
        self.assertEqual([tuple(row[len(before[0][0]) :]) for row in sprints], [(3, 0, [], [])])
        self.assertTrue(record("card_handed_to_owner", "ummanu-1", "handed", "h-1", to=store))
        self.assertFalse(record("card_handed_to_owner", "ummanu-1", "again", "h-1", to=store))
        self.assertTrue(record("sprint_closed", "sprint:5", "closed", "c-1", to=store))
        self.assertTrue(record("budget_signal", "sprint:5", "signal", "b-1", to=store))
        self.assertEqual(store.unread_count(), 3)
        events = store.events()
        self.assertEqual([event.kind for event in events], ["card_handed_to_owner", "budget_signal", "sprint_closed"])
        self.assertTrue(events[0].held and events[0].pinned)
        self.assertEqual(events[0].text, "handed")

        with self.assertRaises(ReadRefused):
            store.mark_read(events[0].id)
        self.assertEqual(store.mark_all_read(), 2)
        self.assertIsNone(store.events()[0].read_at)
        self.assertEqual(store.unread_count(), 1)

        connection.exec_driver_sql("UPDATE tasks SET extensions = '{\"extra\": {}}' WHERE task_ref = 'ummanu-1'")
        connection.commit()
        self.assertEqual(settle("ummanu-1", to=store), 1)
        self.assertEqual(store.unread_count(), 0)
        self.assertEqual([event.kind for event in store.events(unread_only=True)], [])

        # The CHECKs hold the vocabularies and the kind-to-class rule; the read role only reads.
        for kind, event_class in (("card_moved", "notice"), ("sprint_closed", "needs_owner"), ("sprint_closed", "urgent")):
            with self.subTest(kind=kind, event_class=event_class), self.assertRaises(sa.exc.IntegrityError):
                connection.exec_driver_sql(
                    'INSERT INTO owner_events (kind, "class", text, created_at, dedup_key) '
                    "VALUES (%s, %s, 'x', now(), %s)",
                    (kind, event_class, f"{kind}-{event_class}"),
                )
            connection.rollback()
        reader = OwnerEventStore(self.credentials("read"))
        self.assertEqual(len(reader.events()), 3)
        from ummanu.board.owner_events import OwnerEventsUnavailable

        with self.assertRaises(OwnerEventsUnavailable):
            reader.mark_all_read()


    # --- 0019: a PO session's title -------------------------------------------------------------

    def test_0019_titles_each_sprint_linked_session_once_and_drops_the_column_on_downgrade(self) -> None:
        """secretary-1782: existing sessions load untitled; a sprint's session takes its sprint's ref."""
        from alembic import command

        connection = self.owner_connection()
        config = migrate.alembic_config(connection=connection, passwords=self.passwords)
        command.upgrade(config, "0018_owner_events")
        connection.commit()
        for session_id in ("linked", "unlinked", "twice"):
            connection.exec_driver_sql(
                "INSERT INTO po_sessions (session_id, cli, model, cwd, created_at, state, effort) "
                "VALUES (%s, 'claude', 'opus', '/po', now(), 'open', 'high')",
                (session_id,),
            )
        for ref, number, session, created in (
            ("sprint:1467", 1467, "linked", "2026-09-20T10:00:00Z"),
            ("sprint:12", 12, None, "2026-09-20T10:00:00Z"),
            ("sprint:5", 5, "twice", "2026-09-21T10:00:00Z"),
            ("sprint:40", 40, "twice", "2026-09-20T10:00:00Z"),
        ):
            self.sprint(connection, ref, number)
            connection.exec_driver_sql(
                "UPDATE sprints SET po_session = %s, created_at = %s WHERE ref = %s", (session, created, ref)
            )
        connection.commit()
        before = connection.exec_driver_sql("SELECT * FROM po_sessions ORDER BY session_id").fetchall()

        self.assertEqual(self.run_migrations(connection), ("0019_po_session_title", "0020_wait_card_kind", "0021_delegated_card_settled", "0022_origin_returns", "0023_sprint_e2e_budget", "0024_e2e_after_merge_kind", "0025_card_waits_for_person", "0026_sprint_local_runs", "0027_sprint_owner_decisions"))

        rows = connection.exec_driver_sql(
            "SELECT session_id, title FROM po_sessions ORDER BY session_id"
        ).fetchall()
        # A session that opened two sprints: the first it opened, whatever the scan order.
        self.assertEqual(rows, [("linked", "sprint:1467"), ("twice", "sprint:40"), ("unlinked", None)])
        # Every other column is what it was.
        after = connection.exec_driver_sql(
            "SELECT session_id, cli, model, cwd, created_at, state, cli_session_id, closed_at, closed_by, "
            "effort FROM po_sessions ORDER BY session_id"
        ).fetchall()
        self.assertEqual(after, before)
        # A title a person set is never overwritten: the backfill takes only null titles.
        connection.exec_driver_sql("UPDATE po_sessions SET title = 'mine' WHERE session_id = 'linked'")
        connection.commit()

        command.downgrade(config, "0018_owner_events")
        connection.commit()
        self.assertEqual(migrate.current_revision(connection), "0018_owner_events")
        self.assertEqual(
            connection.exec_driver_sql(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'po_sessions' AND column_name = 'title'"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            connection.exec_driver_sql("SELECT * FROM po_sessions ORDER BY session_id").fetchall(), before
        )

        # Up again: the backfill runs on what is there, the null titles.
        self.assertEqual(self.run_migrations(connection), ("0019_po_session_title", "0020_wait_card_kind", "0021_delegated_card_settled", "0022_origin_returns", "0023_sprint_e2e_budget", "0024_e2e_after_merge_kind", "0025_card_waits_for_person", "0026_sprint_local_runs", "0027_sprint_owner_decisions"))
        self.assertEqual(
            connection.exec_driver_sql("SELECT title FROM po_sessions ORDER BY session_id").fetchall(),
            [("sprint:1467",), ("sprint:40",), (None,)],
        )

    # --- 0020: the wait card kind ----------------------------------------------------------------

    def test_0020_keeps_every_existing_card_and_admits_wait(self) -> None:
        """secretary-1790: every existing kind loads unchanged; `wait` is admitted, and nothing else new."""
        import sqlalchemy as sa
        from alembic import command

        connection = self.owner_connection()
        config = migrate.alembic_config(connection=connection, passwords=self.passwords)
        command.upgrade(config, "0019_po_session_title")
        connection.commit()
        connection.exec_driver_sql("INSERT INTO projects (project_id) VALUES ('ummanu')")
        kinds = ("code", "research", "infra", "decision", "operation", None)
        for number, kind in enumerate(kinds, start=1):
            self.card(connection, f"ummanu-{number}", task_type=kind)
        connection.commit()
        before = connection.exec_driver_sql("SELECT * FROM tasks ORDER BY task_ref").fetchall()
        with self.assertRaises(sa.exc.IntegrityError):
            self.card(connection, "ummanu-7", task_type="wait")
        connection.rollback()

        self.assertEqual(self.run_migrations(connection), ("0020_wait_card_kind", "0021_delegated_card_settled", "0022_origin_returns", "0023_sprint_e2e_budget", "0024_e2e_after_merge_kind", "0025_card_waits_for_person", "0026_sprint_local_runs", "0027_sprint_owner_decisions"))

        self.assertEqual(connection.exec_driver_sql("SELECT * FROM tasks ORDER BY task_ref").fetchall(), before)
        self.card(connection, "ummanu-7", task_type="wait", extensions='{"extra": {"wait": "{}"}}')
        connection.commit()
        with self.assertRaisesRegex(Exception, "task_type_is_a_known_type_or_nothing"):
            self.card(connection, "ummanu-8", task_type="chore")
        connection.rollback()

        # The downgrade restores 0017's vocabulary, which a wait card does not fit.
        with self.assertRaises(sa.exc.IntegrityError):
            command.downgrade(config, "0019_po_session_title")
        connection.rollback()
        connection.exec_driver_sql("DELETE FROM tasks WHERE task_ref = 'ummanu-7'")
        connection.commit()
        command.downgrade(config, "0019_po_session_title")
        connection.commit()
        self.assertEqual(migrate.current_revision(connection), "0019_po_session_title")
        with self.assertRaises(sa.exc.IntegrityError):
            self.card(connection, "ummanu-7", task_type="wait")
        connection.rollback()

    # --- 0021: the owner event kind of a delegated card's returned result -----------------------

    def test_0021_keeps_every_event_and_admits_delegated_card_settled_as_a_notice(self) -> None:
        """secretary-1792: every existing event loads unchanged; the new kind is a notice, nothing else new."""
        import sqlalchemy as sa
        from alembic import command

        from ummanu.board.owner_events import DELEGATED_CARD_SETTLED, OwnerEventStore, record

        def insert(kind: str, event_class: str, key: str) -> None:
            connection.exec_driver_sql(
                'INSERT INTO owner_events (kind, "class", subject_ref, text, created_at, dedup_key) '
                f"VALUES ('{kind}', '{event_class}', 'ummanu-1', 'text', now(), '{key}')"
            )

        connection = self.owner_connection()
        config = migrate.alembic_config(connection=connection, passwords=self.passwords)
        command.upgrade(config, "0020_wait_card_kind")
        connection.commit()
        insert("sprint_closed", "notice", "k-1")
        insert("card_handed_to_owner", "needs_owner", "k-2")
        connection.commit()
        before = connection.exec_driver_sql("SELECT * FROM owner_events ORDER BY id").fetchall()
        with self.assertRaises(sa.exc.IntegrityError):
            insert(DELEGATED_CARD_SETTLED, "notice", "k-3")
        connection.rollback()

        self.assertEqual(self.run_migrations(connection), ("0021_delegated_card_settled", "0022_origin_returns", "0023_sprint_e2e_budget", "0024_e2e_after_merge_kind", "0025_card_waits_for_person", "0026_sprint_local_runs", "0027_sprint_owner_decisions"))

        self.assertEqual(connection.exec_driver_sql("SELECT * FROM owner_events ORDER BY id").fetchall(), before)
        store = OwnerEventStore(self.credentials("app"))
        self.assertTrue(record(DELEGATED_CARD_SETTLED, "ummanu-1", "settled Done", "d-1", to=store))
        self.assertFalse(record(DELEGATED_CARD_SETTLED, "ummanu-1", "settled Done", "d-1", to=store))
        [event] = [event for event in store.events() if event.kind == DELEGATED_CARD_SETTLED]
        self.assertEqual((event.event_class, event.subject_ref, event.dedup_key), ("notice", "ummanu-1", "d-1"))
        # The class still follows the kind, and an unknown kind is still refused.
        for kind, event_class in ((DELEGATED_CARD_SETTLED, "needs_owner"), ("card_settled", "notice")):
            with self.subTest(kind=kind, event_class=event_class), self.assertRaises(sa.exc.IntegrityError):
                insert(kind, event_class, f"x-{kind}-{event_class}")
            connection.rollback()

        # The downgrade restores 0018's vocabulary, which the new event does not fit.
        with self.assertRaises(sa.exc.IntegrityError):
            command.downgrade(config, "0020_wait_card_kind")
        connection.rollback()
        connection.exec_driver_sql(f"DELETE FROM owner_events WHERE kind = '{DELEGATED_CARD_SETTLED}'")
        connection.commit()
        command.downgrade(config, "0020_wait_card_kind")
        connection.commit()
        self.assertEqual(migrate.current_revision(connection), "0020_wait_card_kind")
        with self.assertRaises(sa.exc.IntegrityError):
            insert(DELEGATED_CARD_SETTLED, "notice", "k-4")
        connection.rollback()


    # --- 0023: a sprint's e2e run budget ---------------------------------------------------------

    def test_0023_gives_every_existing_sprint_a_budget_of_3_and_nothing_used(self) -> None:
        """secretary-1796: open and closed sprints alike read 3 and 0, and the counts are not negative."""
        import sqlalchemy as sa
        from alembic import command

        from ummanu.board.owner_events import OwnerEventStore, record

        connection = self.owner_connection()
        config = migrate.alembic_config(connection=connection, passwords=self.passwords)
        command.upgrade(config, "0022_origin_returns")
        connection.commit()
        self.sprint(connection, "sprint:1469", 1469)
        self.sprint(connection, "sprint:12", 12)
        connection.exec_driver_sql("UPDATE sprints SET status = 'closed', closed_at = now() WHERE ref = 'sprint:12'")
        connection.commit()

        self.assertEqual(self.run_migrations(connection), ("0023_sprint_e2e_budget", "0024_e2e_after_merge_kind", "0025_card_waits_for_person", "0026_sprint_local_runs", "0027_sprint_owner_decisions"))

        self.assertEqual(
            connection.exec_driver_sql("SELECT ref, e2e_budget, e2e_used FROM sprints ORDER BY ref").fetchall(),
            [("sprint:12", 3, 0), ("sprint:1469", 3, 0)],
        )
        self.assertEqual(connection.exec_driver_sql("SELECT count(*) FROM sprint_e2e_charges").scalar(), 0)
        with self.assertRaises(sa.exc.IntegrityError):
            connection.exec_driver_sql("UPDATE sprints SET e2e_used = -1 WHERE ref = 'sprint:1469'")
        connection.rollback()
        # The bell takes the new `needs_owner` kind, and only as `needs_owner`.
        store = OwnerEventStore(self.credentials("app"))
        self.assertTrue(record("e2e_budget_spent", "ummanu-1", "cap spent", "e-1", to=store))
        with self.assertRaises(sa.exc.IntegrityError):
            connection.exec_driver_sql(
                "INSERT INTO owner_events (kind, class, subject_ref, text, created_at, dedup_key) "
                "VALUES ('e2e_budget_spent', 'notice', NULL, 'x', now(), 'e-2')"
            )
        connection.rollback()
        # The downgrade refuses while such an event exists, and drops the budget once none does.
        with self.assertRaises(RuntimeError):
            command.downgrade(config, "0022_origin_returns")
        connection.rollback()
        connection.exec_driver_sql("DELETE FROM owner_events WHERE kind = 'e2e_budget_spent'")
        connection.commit()
        command.downgrade(config, "0022_origin_returns")
        connection.commit()
        self.assertEqual(migrate.current_revision(connection), "0022_origin_returns")
        columns = {
            name
            for (name,) in connection.exec_driver_sql(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'sprints'"
            ).fetchall()
        }
        self.assertFalse({"e2e_budget", "e2e_used"} & columns)


    # --- 0024: the bell kind of an after-merge e2e run --------------------------------------------

    def test_0024_admits_e2e_after_merge_as_a_needs_owner_kind_and_downgrades_only_without_one(self) -> None:
        """secretary-1807: the kind is accepted only as `needs_owner`, and every earlier event loads unchanged."""
        import sqlalchemy as sa
        from alembic import command

        from ummanu.board.owner_events import OwnerEventStore, record

        connection = self.owner_connection()
        config = migrate.alembic_config(connection=connection, passwords=self.passwords)
        command.upgrade(config, "0023_sprint_e2e_budget")
        connection.commit()
        store = OwnerEventStore(self.credentials("app"))
        # The operational store refuses a store at 0023 (`board.schema_gate`), so the earlier event
        # is seeded as that revision's own row.
        with self.assertLogs("ummanu.board.owner_events", level="WARNING"):
            self.assertFalse(record("e2e_budget_spent", "ummanu-1", "cap spent", "e-0", to=store))
        connection.exec_driver_sql(
            "INSERT INTO owner_events (kind, class, subject_ref, text, created_at, dedup_key) "
            "VALUES ('e2e_budget_spent', 'needs_owner', 'ummanu-1', 'cap spent', now(), 'e-0')"
        )
        connection.commit()

        self.assertEqual(self.run_migrations(connection), ("0024_e2e_after_merge_kind", "0025_card_waits_for_person", "0026_sprint_local_runs", "0027_sprint_owner_decisions"))

        self.assertEqual(
            connection.exec_driver_sql("SELECT kind, class FROM owner_events").fetchall(),
            [("e2e_budget_spent", "needs_owner")],
        )
        self.assertTrue(record("e2e_after_merge", "ummanu-2", "after-merge run cancelled", "e-1", to=store))
        with self.assertRaises(sa.exc.IntegrityError):
            connection.exec_driver_sql(
                "INSERT INTO owner_events (kind, class, subject_ref, text, created_at, dedup_key) "
                "VALUES ('e2e_after_merge', 'notice', NULL, 'x', now(), 'e-2')"
            )
        connection.rollback()
        with self.assertRaises(RuntimeError):
            command.downgrade(config, "0023_sprint_e2e_budget")
        connection.rollback()
        connection.exec_driver_sql("DELETE FROM owner_events WHERE kind = 'e2e_after_merge'")
        connection.commit()
        command.downgrade(config, "0023_sprint_e2e_budget")
        connection.commit()
        self.assertEqual(migrate.current_revision(connection), "0023_sprint_e2e_budget")
        with self.assertRaises(sa.exc.IntegrityError):
            connection.exec_driver_sql(
                "INSERT INTO owner_events (kind, class, subject_ref, text, created_at, dedup_key) "
                "VALUES ('e2e_after_merge', 'needs_owner', NULL, 'x', now(), 'e-3')"
            )
        connection.rollback()


    def test_0025_real_wait_backfill_constraints_and_the_previous_runtime(self) -> None:
        import io
        import os
        import sys
        import tarfile
        import tempfile

        import sqlalchemy as sa
        from alembic import command

        from ummanu.board.owner_events import OwnerEventStore

        connection = self.owner_connection()
        config = migrate.alembic_config(connection=connection, passwords=self.passwords)
        command.upgrade(config, "0024_e2e_after_merge_kind")
        connection.exec_driver_sql(
            "INSERT INTO sprints (ref, board_key, goal, definition_of_done, created_at, updated_at) "
            "VALUES ('sprint:fixture', 800, 'wait', 'decision', now(), now())"
        )
        connection.exec_driver_sql(
            "INSERT INTO tasks (task_ref, task_number, title, task_type, state, sprint_ref, created_at, updated_at) "
            "VALUES ('ummanu-800', 800, 'PO decision', 'decision', 'in_progress', 'sprint:fixture', now(), now())"
        )
        connection.exec_driver_sql(
            "INSERT INTO owner_events (kind, class, subject_ref, text, created_at, dedup_key) "
            "VALUES ('provider_red','notice',NULL,'prior notice',now(),'prior-notice')"
        )
        prior = connection.exec_driver_sql("SELECT * FROM owner_events").fetchall()
        connection.commit()
        self.assertEqual(self.run_migrations(connection), ("0025_card_waits_for_person", "0026_sprint_local_runs", "0027_sprint_owner_decisions"))
        self.assertEqual(connection.exec_driver_sql("SELECT * FROM owner_events WHERE dedup_key='prior-notice'").fetchall(), prior)
        self.assertEqual(self.run_migrations(connection), ())
        store = OwnerEventStore(self.credentials("app"))
        snapshot = store.snapshot()
        self.assertEqual(snapshot["unread"], 2)
        self.assertEqual(snapshot["human_waits"][0]["sprint_ref"], "sprint:fixture")
        self.assertTrue(next(event for event in snapshot["events"] if event["kind"] == "card_waits_for_person")["held"])
        with self.assertRaises(sa.exc.IntegrityError):
            connection.exec_driver_sql(
                "INSERT INTO owner_events (kind, class, text, created_at, dedup_key) "
                "VALUES ('card_waits_for_person','notice','invalid',now(),'invalid')"
            )
        connection.rollback()

        # Run the actual previous src tree, not a mock of its schema assessment. CI
        # fetches full history. During development the added revision is still uncommitted.
        repository = Path(__file__).resolve().parents[1]
        self.assertEqual(
            subprocess.check_output(
                ["git", "rev-parse", "--is-shallow-repository"], cwd=repository, text=True
            ).strip(),
            "false",
            "the prior-runtime proof requires full Git history (actions/checkout fetch-depth: 0)",
        )
        # The revision first landed under the package's earlier name, and a later rename adds the
        # same file again under the current one: the earliest addition, by its place in the package,
        # is the one whose parent is the previous runtime.
        revision_path = "src/*/board/migrations/versions/0025_card_waits_for_person.py"
        additions = subprocess.check_output(
            ["git", "log", "--no-renames", "--diff-filter=A", "--reverse", "--format=%P", "--", revision_path],
            cwd=repository, text=True,
        ).split("\n")
        parents = additions[0].split() if additions else []
        previous = parents[0] if parents else "HEAD"
        package = next(
            name.split("/")[1]
            for name in subprocess.check_output(
                ["git", "ls-tree", "-r", "--name-only", previous, "src"], cwd=repository, text=True
            ).splitlines()
            if name.endswith("/board/migrate.py") and name.count("/") == 3
        )
        archive = subprocess.check_output(["git", "archive", previous, "src"], cwd=repository)
        prior_root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with tarfile.open(fileobj=io.BytesIO(archive)) as tree:
            tree.extractall(prior_root, filter="data")
        script = """import json, sys
from types import SimpleNamespace
from PACKAGE.board import migrate
from PACKAGE.board.owner_events import OwnerEventStore
from PACKAGE.board.sql_cards import SqlCardClient
from PACKAGE.tasks import TaskReader
credentials = SimpleNamespace(conninfo=lambda: sys.argv[1])
store = OwnerEventStore(credentials)
events = store.events()
assert any(event.kind == 'card_waits_for_person' and event.event_class == 'needs_owner' for event in events)
assert store.unread_count() == 2
assert store.insert('provider_red', None, 'prior runtime still writes', 'old-runtime')
client = SqlCardClient(credentials, sys.argv[2])
assert TaskReader(client).show('ummanu-800')['state'] == 'in_progress'
client.call('createComment', task_id=client.call('getTaskByReference', project_id=1, reference='ummanu-800')['id'], user_id=0, content='old runtime still writes cards')
client.close()
print(json.dumps({'head': migrate.EXPECTED_SCHEMA_REVISION, 'unread': store.unread_count()}))
""".replace("PACKAGE", package)
        result = subprocess.run(
            [sys.executable, "-P", "-c", script, self.credentials("app").conninfo(), str(prior_root)],
            env={**os.environ, "PYTHONPATH": str(prior_root / "src")}, capture_output=True, text=True, timeout=30, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"head": "0024_e2e_after_merge_kind", "unread": 3})


if __name__ == "__main__":
    unittest.main()
