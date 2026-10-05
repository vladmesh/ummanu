"""The board store's connection file, its models and its Alembic scripts: everything without a server.

The resolver is a parse over a local file, the schema is a `MetaData` object and the script
directory is a directory. What genuinely needs a server — running the revision, counting what
PostgreSQL made of it, and asking Alembic whether the result still matches the models — is
`tests/test_board_store_schema.py`, which raises a throwaway `postgres:16` container.
"""

from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import ummanu.board
from ummanu import upgrade
from ummanu.board import migrate, provision, schema, store
from ummanu.board.store import (
    ROLES,
    STORE_ENV,
    STORE_FILE,
    BoardStoreError,
    ensure_ignored,
    findings,
    materialize_fresh,
    resolve,
    resolve_role,
    resolve_with_lifecycle,
    store_path,
)
from ummanu.infra import export_allowlist
from ummanu.infra.export_allowlist import SNAPSHOT_ALLOWLIST, is_exported
from ummanu.runtime.container_labels import PRODUCTION_BOARD_LABEL, TEST_BOARD_LABEL
from tests import container_cleanup
from tests import sql_backend_fixtures

COMPLETE = {
    "UMMANU_DB_HOST": "127.0.0.1",
    "UMMANU_DB_PORT": "5432",
    "UMMANU_DB_NAME": "ummanu",
    "UMMANU_DB_OWNER_USER": "ummanu_owner",
    "UMMANU_DB_OWNER_PASSWORD": "owner-secret",
    "UMMANU_DB_APP_USER": "ummanu_app",
    "UMMANU_DB_APP_PASSWORD": "app-secret",
    "UMMANU_DB_READ_USER": "ummanu_read",
    "UMMANU_DB_READ_PASSWORD": "read-secret",
}


def write_store(directory: Path, values: dict[str, str] | None = None, *, mode: int = 0o600) -> Path:
    path = store_path(directory)
    body = "".join(f"{key}={value}\n" for key, value in (COMPLETE if values is None else values).items())
    path.write_text(body, encoding="utf-8")
    path.chmod(mode)
    return path


class ConnectionFileTests(unittest.TestCase):
    """§5.4: nine keys, all required, all-or-nothing, and mode 0600."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.instance = Path(self.tmp.name)

    def test_it_declares_exactly_the_nine_keys_of_the_document(self) -> None:
        self.assertEqual(
            STORE_ENV,
            (
                "UMMANU_DB_HOST",
                "UMMANU_DB_PORT",
                "UMMANU_DB_NAME",
                "UMMANU_DB_OWNER_USER",
                "UMMANU_DB_OWNER_PASSWORD",
                "UMMANU_DB_APP_USER",
                "UMMANU_DB_APP_PASSWORD",
                "UMMANU_DB_READ_USER",
                "UMMANU_DB_READ_PASSWORD",
            ),
        )

    def test_a_complete_file_resolves_one_credential_per_role(self) -> None:
        write_store(self.instance)

        config = resolve(self.instance)

        self.assertEqual((config.host, config.port, config.dbname), ("127.0.0.1", 5432, "ummanu"))
        self.assertEqual(
            [(role, resolve_role(self.instance, role).user) for role in ROLES],
            [("owner", "ummanu_owner"), ("app", "ummanu_app"), ("read", "ummanu_read")],
        )
        self.assertNotEqual(
            resolve_role(self.instance, "app").password,
            resolve_role(self.instance, "read").password,
            "a single credential would make §5.5's three-role boundary unreachable",
        )

    def test_the_conninfo_escapes_a_password_that_carries_a_space_or_a_quote(self) -> None:
        values = dict(COMPLETE, UMMANU_DB_APP_PASSWORD="a b'c\\d")
        write_store(self.instance, values)

        conninfo = resolve_role(self.instance, "app").conninfo()

        self.assertIn("password='a b\\'c\\\\d'", conninfo)
        self.assertIn("user='ummanu_app'", conninfo)

    def test_a_missing_file_refuses_with_a_reason(self) -> None:
        with self.assertRaisesRegex(BoardStoreError, "board store configuration is missing"):
            resolve(self.instance)

    def test_a_partial_file_refuses_and_names_what_is_absent(self) -> None:
        for absent in STORE_ENV:
            with self.subTest(absent=absent):
                write_store(self.instance, {k: v for k, v in COMPLETE.items() if k != absent})
                with self.assertRaisesRegex(BoardStoreError, absent):
                    resolve(self.instance)

    def test_an_empty_value_is_a_partial_file_and_not_an_empty_password(self) -> None:
        write_store(self.instance, dict(COMPLETE, UMMANU_DB_APP_PASSWORD=""))

        with self.assertRaisesRegex(BoardStoreError, "line 7 is invalid"):
            resolve(self.instance)

    def test_an_unknown_or_repeated_key_refuses_the_whole_file(self) -> None:
        path = store_path(self.instance)
        for body, line in (
            ("".join(f"{k}={v}\n" for k, v in COMPLETE.items()) + "UMMANU_DB_EXTRA=x\n", 10),
            ("UMMANU_DB_HOST=a\n" + "".join(f"{k}={v}\n" for k, v in COMPLETE.items()), 2),
        ):
            with self.subTest(line=line):
                path.write_text(body, encoding="utf-8")
                path.chmod(0o600)
                with self.assertRaisesRegex(BoardStoreError, f"line {line} is invalid"):
                    resolve(self.instance)

    def test_a_line_without_an_equals_sign_refuses(self) -> None:
        path = write_store(self.instance)
        path.write_text(path.read_text(encoding="utf-8") + "nonsense\n", encoding="utf-8")

        with self.assertRaisesRegex(BoardStoreError, "line 10 must use KEY=VALUE"):
            resolve(self.instance)

    def test_a_readable_by_others_file_refuses_until_it_is_chmodded(self) -> None:
        write_store(self.instance, mode=0o644)

        with self.assertRaisesRegex(BoardStoreError, "permissions are too broad"):
            resolve(self.instance)

    def test_a_symlink_is_refused_even_when_its_target_is_private(self) -> None:
        real = self.instance / "elsewhere.env"
        real.write_text("".join(f"{k}={v}\n" for k, v in COMPLETE.items()), encoding="utf-8")
        real.chmod(0o600)
        store_path(self.instance).symlink_to(real)

        with self.assertRaisesRegex(BoardStoreError, "regular file, not a symlink"):
            resolve(self.instance)

    def test_a_port_that_is_not_a_tcp_port_refuses(self) -> None:
        for port in ("0", "70000", "5432a"):
            with self.subTest(port=port):
                write_store(self.instance, dict(COMPLETE, UMMANU_DB_PORT=port))
                with self.assertRaisesRegex(BoardStoreError, "not a TCP port"):
                    resolve(self.instance)

    def test_an_unknown_role_refuses_rather_than_falling_back_to_the_owner(self) -> None:
        write_store(self.instance)

        with self.assertRaisesRegex(BoardStoreError, "role must be one of"):
            resolve_role(self.instance, "admin")

    def test_a_checkout_with_no_store_and_no_ignore_entry_reports_nothing(self) -> None:
        """A pre-store installation is not an unhealthy one; only a lifecycle marker makes
        absence a finding."""
        self.assertEqual(findings(self.instance), [])

    def test_findings_reports_a_broken_file_without_disclosing_it(self) -> None:
        write_store(self.instance, mode=0o644)

        reported = findings(self.instance)

        self.assertEqual(len(reported), 1)
        self.assertIn("permissions are too broad", reported[0])
        self.assertNotIn("owner-secret", reported[0])

    def test_resolving_never_writes_anything_into_the_installation(self) -> None:
        before = sorted(os.listdir(self.instance))

        with self.assertRaises(BoardStoreError):
            resolve(self.instance)
        findings(self.instance)

        self.assertEqual(sorted(os.listdir(self.instance)), before)

    def test_fresh_materialization_is_complete_private_and_never_rotates(self) -> None:
        config = materialize_fresh(self.instance)
        path = store_path(self.instance)

        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(set(config.as_environ()), set(STORE_ENV))
        self.assertEqual(len({config.owner_password, config.app_password, config.read_password}), 3)
        # Excluded because the export allowlist does not match it; nothing writes a `.gitignore`.
        self.assertFalse(is_exported(STORE_FILE))
        self.assertFalse((self.instance / ".gitignore").exists())
        before = path.read_bytes()
        with self.assertRaisesRegex(BoardStoreError, "rotation"):
            materialize_fresh(self.instance)
        self.assertEqual(path.read_bytes(), before)


class ProvisionDefinitionTests(unittest.TestCase):
    def test_compose_contract_is_pinned_loopback_and_persistent(self) -> None:
        self.assertEqual(provision.IMAGE, f"postgres:{provision.POSTGRES_MAJOR}")
        self.assertIn("image: postgres:16", provision.COMPOSE_TEXT)
        self.assertIn("restart: unless-stopped", provision.COMPOSE_TEXT)
        self.assertIn("127.0.0.1:${UMMANU_DB_PORT}:5432", provision.COMPOSE_TEXT)
        self.assertIn("board-db:/var/lib/postgresql/data", provision.COMPOSE_TEXT)
        self.assertIn(f"{PRODUCTION_BOARD_LABEL}: 'true'", provision.COMPOSE_TEXT)
        self.assertNotIn(TEST_BOARD_LABEL, provision.COMPOSE_TEXT)
        self.assertNotIn("UMMANU_DB_APP_PASSWORD", provision.COMPOSE_TEXT)
        self.assertNotIn("UMMANU_DB_READ_PASSWORD", provision.COMPOSE_TEXT)

    def test_absent_config_is_an_upgrade_noop_before_touching_docker(self) -> None:
        with TemporaryDirectory() as temporary, mock.patch.object(provision, "_exists") as inspect:
            outcome = provision.provision(Path(temporary))

        self.assertIsNone(outcome)
        inspect.assert_not_called()

    def test_existing_volume_without_config_refuses_new_credentials(self) -> None:
        with (
            TemporaryDirectory() as temporary,
            mock.patch.object(provision, "_exists", return_value=True),
            mock.patch.object(store, "materialize_fresh") as materialize,
            self.assertRaisesRegex(BoardStoreError, "exists without board-store.env"),
        ):
            provision.provision(
                Path(temporary),
                allow_create=True,
                compose_path=Path(temporary) / "compose.yml",
            )

        materialize.assert_not_called()

    def test_compose_drift_is_refused_without_replacing_the_file(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_store(root)
            compose = root / "compose.yml"
            compose.write_text("services: {}\n", encoding="utf-8")
            compose.chmod(0o600)

            with self.assertRaisesRegex(BoardStoreError, "definition drift"):
                provision.provision(root, compose_path=compose)

            self.assertEqual(compose.read_text(encoding="utf-8"), "services: {}\n")

    def test_legacy_definition_dry_run_reports_upgrade_without_writing_or_docker(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_store(root)
            compose = root / "compose.yml"
            compose.write_text(provision.LEGACY_COMPOSE_TEXT, encoding="utf-8")
            compose.chmod(0o600)
            with mock.patch.object(provision, "_run") as run:
                outcome = provision.provision(root, compose_path=compose, dry_run=True)
            self.assertIn("would upgrade legacy", outcome.render(dry_run=True))
            self.assertEqual(compose.read_text(encoding="utf-8"), provision.LEGACY_COMPOSE_TEXT)
            run.assert_not_called()

    def test_legacy_container_is_checked_before_definition_changes(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_store(root)
            compose = root / "compose.yml"
            compose.write_text(provision.LEGACY_COMPOSE_TEXT, encoding="utf-8")
            compose.chmod(0o600)
            with (mock.patch.object(provision, "_run", return_value="container-id"),
                  mock.patch.object(provision, "_inspect_container", side_effect=BoardStoreError("drift")),
                  self.assertRaisesRegex(BoardStoreError, "drift")):
                provision.provision(root, compose_path=compose)
            self.assertEqual(compose.read_text(encoding="utf-8"), provision.LEGACY_COMPOSE_TEXT)

    def test_legacy_upgrade_and_labelled_rerun(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_store(root)
            compose = root / "compose.yml"
            compose.write_text(provision.LEGACY_COMPOSE_TEXT, encoding="utf-8")
            compose.chmod(0o600)
            inspection = []
            def inspect(_container, **kwargs):
                inspection.append(kwargs)
            with (mock.patch.object(provision, "_run", side_effect=lambda args, **kw: "container-id" if "ps" in args else ""),
                  mock.patch.object(provision, "_inspect_container", side_effect=inspect),
                  mock.patch.object(provision, "_wait_ready")):
                first = provision.provision(root, compose_path=compose)
                second = provision.provision(root, compose_path=compose)
            self.assertTrue(first.changed)
            self.assertFalse(second.changed)
            self.assertEqual(compose.read_text(encoding="utf-8"), provision.COMPOSE_TEXT)
            self.assertTrue(inspection[0]["legacy"])
            self.assertFalse(inspection[-1].get("legacy", False))

    def test_private_compose_requires_current_pid_and_never_uses_production_marker(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_store(root)
            path = root / "test-compose.yml"
            with self.assertRaisesRegex(BoardStoreError, "explicit test ownership"):
                provision.provision(root, compose_path=path, project="private")
            with self.assertRaisesRegex(BoardStoreError, "current process"):
                provision.provision(root, compose_path=path, project="private", test_owner_pid=os.getpid() + 1)
            text = provision.test_compose_text(os.getpid())
            self.assertIn(f"{TEST_BOARD_LABEL}: '{os.getpid()}'", text)
            self.assertNotIn(PRODUCTION_BOARD_LABEL, text)
            with (mock.patch.object(provision, "_run", side_effect=lambda args, **kw: "container-id" if "ps" in args else ""),
                  mock.patch.object(provision, "_inspect_container") as inspect,
                  mock.patch.object(provision, "_wait_ready")):
                provision.provision(root, compose_path=path, project="private", test_owner_pid=os.getpid())
            self.assertEqual(path.read_text(encoding="utf-8"), text)
            self.assertEqual(inspect.call_args.kwargs["owner_pid"], os.getpid())

    def test_container_inspection_requires_matching_production_marker_and_compose_identity(self) -> None:
        payload = {"Config": {"Image": provision.IMAGE, "Labels": {
            "com.docker.compose.project": provision.PROJECT, "com.docker.compose.service": "postgres"}},
            "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}, "PortBindings": {
                "5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "5432"}]}},
            "Mounts": [{"Type": "volume", "Name": "ummanu-board-store_board-db",
                        "Destination": "/var/lib/postgresql/data"}]}
        with mock.patch.object(provision, "_run", return_value=json.dumps([payload])):
            with self.assertRaisesRegex(BoardStoreError, "production ownership"):
                provision._inspect_container("id", volume_name="ummanu-board-store_board-db")
        payload["Config"]["Labels"][PRODUCTION_BOARD_LABEL] = "true"
        with mock.patch.object(provision, "_run", return_value=json.dumps([payload])):
            provision._inspect_container("id", volume_name="ummanu-board-store_board-db")

    def test_container_inspection_rejects_malformed_labels_fail_closed(self) -> None:
        payload = {"Config": {"Image": provision.IMAGE, "Labels": ["not-a-map"]},
                   "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}, "PortBindings": {
                       "5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "5432"}]}},
                   "Mounts": [{"Type": "volume", "Name": "ummanu-board-store_board-db",
                               "Destination": "/var/lib/postgresql/data"}]}
        with mock.patch.object(provision, "_run", return_value=json.dumps([payload])):
            with self.assertRaisesRegex(BoardStoreError, "ownership label"):
                provision._inspect_container("id", volume_name="ummanu-board-store_board-db")

    def test_reconcile_passes_only_the_private_file_path_not_credentials_on_argv(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = write_store(root)
            compose = root / "compose.yml"
            compose.write_text(provision.COMPOSE_TEXT, encoding="utf-8")
            compose.chmod(0o600)
            calls = []

            def command(arguments, **_kwargs):
                calls.append(arguments)
                if "inspect" in arguments:
                    return "[{}]"
                if "ps" in arguments:
                    return "container-id"
                return ""

            with (
                mock.patch.object(provision, "_run", side_effect=command),
                mock.patch.object(provision, "_inspect_container"),
                mock.patch.object(provision, "_wait_ready"),
            ):
                outcome = provision.provision(root, compose_path=compose)

        self.assertIsNotNone(outcome)
        arguments = " ".join(part for call in calls for part in call)
        self.assertIn(str(config_path), arguments)
        for secret in ("owner-secret", "app-secret", "read-secret"):
            self.assertNotIn(secret, arguments)


class TestContainerCleanupTests(unittest.TestCase):
    def test_direct_fixture_registers_cleanup_before_port_inspection(self) -> None:
        with (
            mock.patch.object(sql_backend_fixtures.atexit, "register") as register,
            mock.patch.object(
                sql_backend_fixtures,
                "docker",
                side_effect=["exact-id", RuntimeError("port inspection failed")],
            ),
            mock.patch.object(sql_backend_fixtures, "remove_test_container") as remove,
        ):
            with self.assertRaisesRegex(RuntimeError, "port inspection failed"):
                sql_backend_fixtures.PostgresBoard()
            register.assert_called_once()
            register.call_args.args[0]()
            remove.assert_called_once_with("exact-id")

    def test_direct_container_requires_exact_id_and_current_pid(self) -> None:
        for labels in ({}, {TEST_BOARD_LABEL: str(os.getpid() + 1)},
                       {TEST_BOARD_LABEL: str(os.getpid()), PRODUCTION_BOARD_LABEL: "true"}):
            with self.subTest(labels=labels):
                calls = []
                def docker(*args):
                    calls.append(args)
                    return json.dumps([{"Id": "exact-id", "Config": {"Labels": labels}}])
                with mock.patch.object(container_cleanup, "_docker", side_effect=docker):
                    with self.assertRaisesRegex(RuntimeError, "ownership mismatch"):
                        container_cleanup.remove_test_container("exact-id")
                self.assertEqual(calls, [("container", "inspect", "exact-id")])

        calls = []
        def docker(*args):
            calls.append(args)
            return json.dumps([{"Id": "exact-id", "Config": {"Labels": {
                TEST_BOARD_LABEL: str(os.getpid())}}}]) if args[0] == "container" else ""
        with mock.patch.object(container_cleanup, "_docker", side_effect=docker):
            container_cleanup.remove_test_container("exact-id")
        self.assertEqual(calls[-1], ("rm", "-f", "exact-id"))

    def test_missing_or_ambiguous_compose_target_does_not_sweep_preexisting_same_name_volume(self) -> None:
        with mock.patch.object(container_cleanup, "_docker", return_value="one\ntwo") as docker:
            with self.assertRaisesRegex(RuntimeError, "ambiguous"):
                container_cleanup.cleanup_test_project("private")
            self.assertEqual(docker.call_count, 1)

        with mock.patch.object(container_cleanup, "_docker", return_value="") as docker:
            # The existing `reused_board-db` is deliberately not inspected or removed: this
            # setup attempt never positively observed an owned container.
            container_cleanup.cleanup_test_project("reused", container_expected=False)
            docker.assert_called_once_with(
                "ps", "--all", "--quiet", "--no-trunc", "--filter", "label=com.docker.compose.project=reused"
            )

        with mock.patch.object(container_cleanup, "_docker", side_effect=RuntimeError("No such container")):
            with self.assertRaisesRegex(RuntimeError, "No such container"):
                container_cleanup.remove_test_container("missing-id")

    def test_foreign_compose_container_leaves_resources_alone(self) -> None:
        calls = []
        def docker(*args):
            calls.append(args)
            if args[0] == "ps":
                return "exact-id"
            return json.dumps([{"Id": "exact-id", "Config": {"Labels": {
                TEST_BOARD_LABEL: str(os.getpid() + 1),
                "com.docker.compose.project": "private",
                "com.docker.compose.service": "postgres"}}}])
        with mock.patch.object(container_cleanup, "_docker", side_effect=docker):
            with self.assertRaisesRegex(RuntimeError, "ownership mismatch"):
                container_cleanup.cleanup_test_project("private")
        self.assertEqual([call[0] for call in calls], ["ps", "container"])

    def test_setup_failure_after_owned_container_cleans_its_resources(self) -> None:
        import json

        calls = []
        def docker(*args):
            calls.append(args)
            if args[0] == "ps":
                return "exact-id"
            if args[:2] == ("container", "inspect"):
                return json.dumps([{"Id": "exact-id", "Config": {"Labels": {
                    TEST_BOARD_LABEL: str(os.getpid()),
                    "com.docker.compose.project": "fresh",
                    "com.docker.compose.service": "postgres"}}}])
            if args[1] == "inspect":
                kind = args[0]
                discriminator = f"com.docker.compose.{kind}"
                value = "default" if kind == "network" else "board-db"
                return json.dumps([{"Name": args[2], "Labels": {
                    "com.docker.compose.project": "fresh", discriminator: value}}])
            return ""

        with mock.patch.object(container_cleanup, "_docker", side_effect=docker):
            container_cleanup.cleanup_test_project("fresh", container_expected=False)
        self.assertIn(("rm", "-f", "exact-id"), calls)
        self.assertIn(("network", "rm", "fresh_default"), calls)
        self.assertIn(("volume", "rm", "fresh_board-db"), calls)

    def test_compose_cleanup_removes_only_verified_container_and_disposable_resources(self) -> None:
        calls = []
        def docker(*args):
            calls.append(args)
            if args[0] == "ps":
                return "exact-id"
            if args[:2] == ("container", "inspect"):
                return json.dumps([{"Id": "exact-id", "Config": {"Labels": {
                    TEST_BOARD_LABEL: str(os.getpid()),
                    "com.docker.compose.project": "private",
                    "com.docker.compose.service": "postgres"}}}])
            if len(args) > 1 and args[1] == "inspect":
                key = "network" if args[0] == "network" else "volume"
                return json.dumps([{"Name": args[2], "Labels": {
                    "com.docker.compose.project": "private",
                    f"com.docker.compose.{key}": "default" if key == "network" else "board-db"}}])
            return ""
        with mock.patch.object(container_cleanup, "_docker", side_effect=docker):
            container_cleanup.cleanup_test_project("private")
        self.assertEqual([call for call in calls if "rm" in call], [
            ("rm", "-f", "exact-id"),
            ("network", "rm", "private_default"),
            ("volume", "rm", "private_board-db"),
        ])


class SchemaModelTests(unittest.TestCase):
    """The models are the schema (§3), so what §3 constrains has to be *in* them.

    None of this needs a server: it reads `MetaData`. What needs one — running the revision and
    counting what PostgreSQL made of it — is `tests/test_board_store_schema.py`.
    """

    def test_it_declares_every_table_of_section_3_and_no_version_table(self) -> None:
        """23 tables; the 24th §3.13 counts is Alembic's own `alembic_version`.

        `product_comments` is the 23rd, added by `0004_product_issue_sql` so the SQL backend can
        serve the released Product comment vocabulary.
        """
        self.assertEqual(
            sorted(schema.metadata.tables),
            [
                "board_events",
                "issue_comments",
                "issues",
                "origin_returns",
                "owner_events",
                "po_feed",
                "po_requests",
                "po_sessions",
                "po_turns",
                "product_comments",
                "product_projects",
                "products",
                "projects",
                "repositories",
                "requests",
                "sprint_budget_events",
                "sprint_comments",
                "sprint_decisions",
                "sprint_e2e_charges",
                "sprint_issues",
                "sprint_projects",
                "sprint_repositories",
                "sprint_resumes",
                "sprints",
                "task_comments",
                "task_dependencies",
                "task_issues",
                "task_retry_heads",
                "task_supersessions",
                "tasks",
            ],
        )
        self.assertNotIn("schema_migrations", schema.metadata.tables)
        self.assertNotIn("alembic_version", schema.metadata.tables)

    def test_jsonb_is_exactly_the_columns_section_3_10_names(self) -> None:
        """Ten with the quoted authority (J9) and typed PO wait (J10)."""
        from sqlalchemy.dialects.postgresql import JSONB

        found = {
            (name, column.name)
            for name, table in schema.metadata.tables.items()
            for column in table.columns
            if isinstance(column.type, JSONB)
        }

        self.assertEqual(found, set(schema.JSONB_COLUMNS))

    def test_every_closed_vocabulary_is_a_check_constraint(self) -> None:
        """§3.12's rule: a closed vocabulary is a CHECK, never a reference table."""
        import sqlalchemy as sa

        checks = [
            str(constraint.sqltext)
            for table in schema.metadata.tables.values()
            for constraint in table.constraints
            if isinstance(constraint, sa.CheckConstraint)
        ]

        # 56 since `0022` added `origin_returns` with three (secretary-1792); 57 since `0023` added the
        # sprint's e2e counts (secretary-1796); 58 since `0026` added the local-run array shape;
        # 59 since `0027` added quoted decisions; 60 with `0029`'s typed PO wait shape.
        self.assertEqual(len(checks), 60, "§3.13 counts 60 CHECK constraints at the head revision")
        for vocabulary in (
            "state IN ('active','archived')",
            "priority IN ('P0','P1','P2','P3')",
            "task_type IN ('code','research','infra','decision','operation','wait')",
            "review IN ('required','skipped')",
            "status IN ('staged','committed','discarded')",
            "target_state IN ('done','blocked')",
        ):
            self.assertTrue(
                any(vocabulary in text for text in checks), f"{vocabulary} is not a CHECK anywhere"
            )

    def test_the_four_partial_unique_indexes_carry_their_predicate(self) -> None:
        partial = sorted(
            index.name
            for table in schema.metadata.tables.values()
            for index in table.indexes
            if index.unique and index.dialect_options["postgresql"]["where"] is not None
        )

        self.assertEqual(
            partial,
            [
                "po_turns_one_running_per_session",
                "repositories_one_primary",
                "sprint_decisions_one_per_card",
                "sprint_decisions_one_per_issue",
                "sprint_projects_one_live_reservation",
            ],
        )

    def test_the_generated_ref_columns_are_postgresql_generated_columns(self) -> None:
        """Four since `0004` added the Product comment request fence.

        `sprints.ref` and the three generated `sprint_ref` columns stopped being computed from
        `sprint_number`: the reference is now the stored identity, and it is the scoping column of
        every sprint child. `issue_comments.issue_ref` is the one new generated column, and it
        exists for the reason the sprint ones did — §3.9's claim key joins `requests.ref`, which
        spells an Issue `issue:<id>`.
        """
        for table, column, expression in (
            ("products", "ref", "'product:' || product_id"),
            ("issues", "ref", "'issue:' || issue_id"),
            ("issue_comments", "issue_ref", "'issue:' || issue_id"),
            ("product_comments", "product_ref", "'product:' || product_id"),
        ):
            with self.subTest(table=table):
                computed = schema.metadata.tables[table].columns[column].computed
                self.assertIsNotNone(computed)
                self.assertTrue(computed.persisted)
                self.assertEqual(str(computed.sqltext), expression)
        self.assertIsNone(
            schema.metadata.tables["sprints"].columns["ref"].computed,
            "the sprint's reference is stored, not derived: a reference with no number is a row",
        )

    def test_section_3_13_step_two_constraints_are_emitted_as_alter_table(self) -> None:
        """`use_alter` is what makes a forward or mutual reference expressible at all."""
        import sqlalchemy as sa

        altered = {
            constraint.name
            for table in schema.metadata.tables.values()
            for constraint in table.constraints
            if isinstance(constraint, sa.ForeignKeyConstraint) and constraint.use_alter
        }

        self.assertEqual(altered, set(schema.DEFERRED_CONSTRAINTS))

    def test_the_two_sprint_cursors_are_deferrable(self) -> None:
        import sqlalchemy as sa

        deferred = {
            constraint.name: constraint.initially
            for constraint in schema.metadata.tables["sprints"].constraints
            if isinstance(constraint, sa.ForeignKeyConstraint) and constraint.deferrable
        }

        self.assertEqual(
            deferred,
            {
                "sprint_current_task_is_in_this_sprint": "DEFERRED",
                "sprint_resume_is_of_this_sprint": "DEFERRED",
            },
        )


class MigrationScriptTests(unittest.TestCase):
    """Alembic's script directory as this product ships it — no server needed."""

    def test_the_po_request_vocabulary_is_one_list_in_code_schema_and_head_migration(self) -> None:
        """A `po_requests` operation the CHECK does not admit rolls back every write that records it.

        `ummanu.po.store.REQUEST_OPERATIONS` is what the code (and the unit tests' fake store)
        records; `board/schema.py` and the revision that last widened the CHECK must say the same.
        """
        import re

        from ummanu.po.store import REQUEST_OPERATIONS

        [check] = [
            constraint
            for constraint in schema.Base.metadata.tables["po_requests"].constraints
            if constraint.name == "po_request_operation_in_vocabulary"
        ]
        in_schema = re.findall(r"'([a-z_]+)'", str(check.sqltext))
        revision = (migrate.SCRIPT_LOCATION / "versions" / "0016_sprint_po_session.py").read_text(
            encoding="utf-8"
        )
        in_revision = re.findall(r"'([a-z_]+)'", re.search(r"operation IN \(([^)]*)\)", revision).group(1))

        self.assertEqual(sorted(in_schema), sorted(REQUEST_OPERATIONS))
        self.assertEqual(sorted(in_revision), sorted(REQUEST_OPERATIONS))

    def test_the_tree_ships_exactly_the_revisions_this_build_expects(self) -> None:
        """Newest first, as `walk_revisions` returns them: each revision sits on the one before.

        The list grows by one whenever a revision ships, which is the point: a revision file
        added to the tree and not chained onto the head is exactly the mistake this catches.
        """
        revisions = [script.revision for script in migrate.script_directory().walk_revisions()]

        self.assertEqual(
            revisions,
            [
                "0029_po_channel",
                "0028_owner_turns",
                "0027_sprint_owner_decisions",
                "0026_sprint_local_runs",
                "0025_card_waits_for_person",
                "0024_e2e_after_merge_kind",
                "0023_sprint_e2e_budget",
                "0022_origin_returns",
                "0021_delegated_card_settled",
                "0020_wait_card_kind",
                "0019_po_session_title",
                "0018_owner_events",
                "0017_po_card_kinds",
                "0016_sprint_po_session",
                "0015_po_effort_resolved_model",
                "0014_neutral_extension_bag",
                "0013_budget_candidates",
                "0012_request_read_indexes",
                "0011_card_kinds",
                "0010_po_session_close",
                "0009_po_requests",
                "0008_po_sessions",
                "0007_card_transport_key",
                "0006_sprint_transport_key",
                "0005_sprint_sql",
                "0004_product_issue_sql",
                "0003_task_type_optional",
                "0002_board_gaps",
                "0001_initial",
            ],
        )
        self.assertEqual(migrate.head_revision(), migrate.EXPECTED_SCHEMA_REVISION)

    def test_the_script_directory_ships_inside_the_installed_package(self) -> None:
        self.assertTrue((migrate.SCRIPT_LOCATION / "env.py").is_file())
        self.assertTrue((migrate.SCRIPT_LOCATION / "script.py.mako").is_file())
        self.assertEqual(migrate.SCRIPT_LOCATION.parent, Path(ummanu.board.__file__).resolve().parent)

    def test_the_configuration_carries_no_connection_string_of_its_own(self) -> None:
        """§5.4 is the only place an installation's URL lives; an `alembic.ini` literal is not."""
        config = migrate.alembic_config()

        self.assertIsNone(config.get_main_option("sqlalchemy.url", None))
        self.assertIsNone(config.config_file_name)
        self.assertEqual(config.get_main_option("script_location"), str(migrate.SCRIPT_LOCATION))
        self.assertNotIn("connection", config.attributes)

    def test_the_connection_and_the_passwords_travel_in_attributes(self) -> None:
        sentinel = object()

        config = migrate.alembic_config(connection=sentinel, passwords={"app_password": "a"})

        self.assertIs(config.attributes["connection"], sentinel)
        self.assertEqual(config.attributes["passwords"], {"app_password": "a"})

    def test_no_password_is_a_literal_in_any_revision(self) -> None:
        """A revision that creates a role takes its passwords as parameters of the run (§5.5).

        Both halves are kept: no revision may carry a password literal at all, and a revision that
        issues `CREATE ROLE` has to reach its passwords through `PASSWORD_PARAMETERS`. Only
        `0001_initial` creates roles — `0002_board_gaps` adds tables to a store whose roles already
        exist, and `0001`'s `ALTER DEFAULT PRIVILEGES` is what makes them reachable — so the second
        assertion is asked of the revisions it is actually about.
        """
        revisions = sorted((migrate.SCRIPT_LOCATION / "versions").glob("*.py"))
        self.assertTrue(revisions)
        creating_roles = 0
        for path in revisions:
            with self.subTest(revision=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("PASSWORD '", text)
                if "CREATE ROLE" in text:
                    creating_roles += 1
                    self.assertIn("PASSWORD_PARAMETERS", text)
        self.assertEqual(creating_roles, 1, "§5.5's fence is built once, by the initial revision")

    def test_the_url_survives_a_password_a_url_would_otherwise_break(self) -> None:
        with TemporaryDirectory() as tmp:
            write_store(Path(tmp), dict(COMPLETE, UMMANU_DB_APP_PASSWORD="p@ss/w:rd"))
            credentials = resolve_role(Path(tmp), "app")

        url = migrate.sqlalchemy_url(credentials)

        self.assertEqual(url.drivername, "postgresql+psycopg")
        self.assertEqual(url.password, "p@ss/w:rd")
        self.assertEqual(url.database, "ummanu")
        self.assertNotIn("p@ss/w:rd", str(url))  # never rendered, and never split into two fields

    def test_an_invocation_with_no_injected_connection_refuses_and_names_the_entry_point(self) -> None:
        """There is one supported way to run these migrations, and `env.py` says which.

        The runner holds `pg_advisory_lock` on the session it migrates on (§7.4), so `env.py`
        opening a connection of its own would migrate outside the lock — and would then reach the
        initial revision with none of §5.5's generated passwords. It refuses here instead, before
        anything connects, naming `ummanu.board.migrate`.
        """
        from alembic import command

        with self.assertRaisesRegex(RuntimeError, "migrations run only through ummanu"):
            command.upgrade(migrate.alembic_config(), "heads")

    def test_the_advisory_key_is_a_fixed_literal(self) -> None:
        """Two upgrades of one installation contend only if every checkout uses one key."""
        self.assertEqual(migrate.ADVISORY_LOCK_KEY, 0x2C5B1F4A6E9D0713)


class LiveRoot(unittest.TestCase):
    """A throwaway live root, a plain directory, for the exclusion tests."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.instance = Path(self.tmp.name)
        (self.instance / "README.md").write_text("instance\n", encoding="utf-8")

    def exported(self):
        """An export allowlist that would copy `board-store.env`, the one thing exclusion refuses."""
        return mock.patch.object(export_allowlist, "SNAPSHOT_ALLOWLIST", (*SNAPSHOT_ALLOWLIST, STORE_FILE))

    def listing(self) -> list[str]:
        return sorted(os.listdir(self.instance))


class IgnoreLifecycleTests(LiveRoot):
    """The exclusion of `board-store.env` (criterion 2, §5.4): the export allowlist never matches it.

    Exclusion is no longer an entry this product writes; it is a property of the allowlist that
    decides what leaves the host. So the lifecycle takes no action and refuses a file the export
    would copy. The bootstrap path that generates the three passwords calls it before it writes.
    """

    def test_the_exclusion_takes_no_action_and_writes_nothing(self) -> None:
        before = self.listing()

        first = ensure_ignored(self.instance)
        dry = ensure_ignored(self.instance, dry_run=True)

        for outcome in (first, dry):
            self.assertFalse(outcome.changed)
            self.assertEqual(outcome.render(), "unchanged")
        self.assertEqual(self.listing(), before)

    def test_it_refuses_a_configuration_anyone_could_read(self) -> None:
        write_store(self.instance, mode=0o644)

        with self.assertRaisesRegex(BoardStoreError, "permissions are too broad"):
            ensure_ignored(self.instance)

        self.assertEqual(store_path(self.instance).stat().st_mode & 0o777, 0o644)

    def test_a_dry_run_also_refuses_a_permissive_file(self) -> None:
        write_store(self.instance, mode=0o644)

        with self.assertRaisesRegex(BoardStoreError, "permissions are too broad"):
            ensure_ignored(self.instance, dry_run=True)

        self.assertEqual(store_path(self.instance).stat().st_mode & 0o777, 0o644)

    def test_an_already_private_configuration_needs_no_repair(self) -> None:
        write_store(self.instance)

        outcome = ensure_ignored(self.instance)

        self.assertFalse(outcome.changed)

    def test_an_exported_configuration_refuses_rather_than_pretending_to_hide_it(self) -> None:
        write_store(self.instance)

        with self.exported():
            for dry_run in (False, True):
                with self.subTest(dry_run=dry_run), self.assertRaisesRegex(BoardStoreError, "snapshot export copies"):
                    ensure_ignored(self.instance, dry_run=dry_run)

    def test_a_symlink_refuses_for_the_reason_the_parse_refuses_one(self) -> None:
        store_path(self.instance).symlink_to(self.instance / "elsewhere.env")

        with self.assertRaisesRegex(BoardStoreError, "regular file, not a symlink"):
            ensure_ignored(self.instance)

    def test_it_never_creates_the_configuration_itself(self) -> None:
        ensure_ignored(self.instance)

        self.assertFalse(store_path(self.instance).exists())

    def test_a_git_work_tree_is_neither_asked_nor_written(self) -> None:
        subprocess.run(["git", "-C", str(self.instance), "init", "--quiet"], check=True)
        write_store(self.instance)

        with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("a child process was started")):
            outcome = ensure_ignored(self.instance)
            resolve(self.instance)

        self.assertFalse(outcome.changed)
        self.assertFalse((self.instance / ".gitignore").exists())


class ExclusionEnforcementTests(LiveRoot):
    """The exclusion stands *in front of* every read of a configured store, not beside it.

    `resolve` is the one door — `resolve_role`, `migrate_instance` and `env.py` all go through it —
    so a `board-store.env` the snapshot export would copy is refused there, and these tests are what
    says so.
    """

    def test_resolving_an_exported_configuration_refuses_with_its_reason(self) -> None:
        write_store(self.instance)

        with self.exported():
            with self.assertRaisesRegex(BoardStoreError, "snapshot export copies"):
                resolve(self.instance)
            with self.assertRaisesRegex(BoardStoreError, "snapshot export copies"):
                resolve_role(self.instance, "owner")

    def test_a_configured_store_is_read_without_writing_anything(self) -> None:
        write_store(self.instance)
        before = self.listing()

        config = resolve(self.instance)

        self.assertEqual(config.owner_user, "ummanu_owner")
        self.assertEqual(self.listing(), before)

    def test_an_absent_store_refuses_without_writing_the_live_root(self) -> None:
        """A read of an installation that has no store -- `status`, `doctor` -- leaves it as it was."""
        before = self.listing()

        with self.assertRaisesRegex(BoardStoreError, "configuration is missing"):
            resolve_role(self.instance, "app")

        self.assertEqual(self.listing(), before)

    def test_the_lifecycle_outcome_is_visible_to_a_caller(self) -> None:
        write_store(self.instance)

        _, first = resolve_with_lifecycle(self.instance)

        self.assertFalse(first.changed)
        self.assertEqual(first.render(), "unchanged")

    def test_the_read_path_refuses_a_broad_mode_rather_than_repairing_it(self) -> None:
        """`enforce_exclusion` is deliberately not a mode repair: a credential file anyone could read
        has already been exposed, so `parse` refuses it instead of quietly chmodding it in the
        middle of a read."""
        path = write_store(self.instance, mode=0o644)

        with self.assertRaisesRegex(BoardStoreError, "permissions are too broad"):
            resolve(self.instance)

        self.assertEqual(path.stat().st_mode & 0o777, 0o644)

    def test_migrating_an_exported_configuration_refuses_before_it_connects(self) -> None:
        from ummanu.board import migrate as board_migrate

        write_store(self.instance)

        with (
            self.exported(),
            mock.patch.object(board_migrate.board_store, "resolve", wraps=resolve) as door,
            self.assertRaisesRegex(BoardStoreError, "snapshot export copies"),
        ):
            board_migrate.migrate_instance(self.instance)

        door.assert_called_once_with(self.instance)

    def test_the_upgrade_step_fails_rather_than_migrating_over_exported_credentials(self) -> None:
        write_store(self.instance)
        context = upgrade.UpgradeContext(
            instance_path=self.instance,
            product_root=self.instance,
            base_branch="main",
            dry_run=False,
            units=None,
        )

        with self.exported(), mock.patch.object(upgrade, "migrate_instance") as migrated:
            result = upgrade.step_board_store(context)

        migrated.assert_not_called()
        self.assertTrue(result.failed)
        self.assertIn("snapshot export copies", result.detail)

    def test_findings_name_an_exported_configuration(self) -> None:
        write_store(self.instance)

        self.assertEqual(findings(self.instance), [])
        with self.exported():
            reported = findings(self.instance)

        self.assertEqual(len(reported), 1)
        self.assertIn("snapshot export copies", reported[0])

    def test_enforcement_is_the_only_door_to_a_configured_store(self) -> None:
        """The claim `resolve` is a chokepoint, checked against the source rather than asserted.

        Everything that opens a configured store reads it through `board_store.resolve`; the only
        callers of the underlying `parse` are `resolve_with_lifecycle` itself and the read-only
        `findings`, which does its own export check and never connects.
        """
        source = (Path(store.__file__)).read_text(encoding="utf-8")
        callers = [line.strip() for line in source.splitlines() if "parse(" in line and "def " not in line]

        self.assertEqual(callers, ["return parse(store_path(instance_dir)), outcome", "parse(path)"])
        self.assertIn("outcome = enforce_exclusion(instance_dir)", source)


class HeldExclusionTests(LiveRoot):
    """`hold_exclusion`: the same guard, run once per process instead of on every read.

    A long-lived reader (`web-serve`) holds it at start-up. Its reads then skip the guard, and a
    refusal found at start-up keeps refusing.
    """

    def setUp(self) -> None:
        super().setUp()
        self.enterContext(mock.patch.dict(store._HELD, clear=True))

    def test_a_held_read_does_not_run_the_guard_again(self) -> None:
        write_store(self.instance)
        outcome = store.hold_exclusion(self.instance)
        self.assertFalse(outcome.changed)

        with mock.patch.object(store, "enforce_exclusion", side_effect=AssertionError("a read ran the guard")):
            config, held = resolve_with_lifecycle(self.instance)
            resolve_role(self.instance, "app")

        self.assertEqual(config.owner_user, "ummanu_owner")
        self.assertFalse(held.changed)

    def test_an_exported_file_found_at_hold_keeps_refusing_every_read(self) -> None:
        write_store(self.instance)

        with self.exported(), self.assertRaisesRegex(BoardStoreError, "snapshot export copies"):
            store.hold_exclusion(self.instance)
        # The refusal is held: a read does not retry the guard, even once the allowlist changed.
        with self.assertRaisesRegex(BoardStoreError, "snapshot export copies"):
            resolve(self.instance)

    def test_a_store_missing_at_hold_is_refused_without_touching_the_live_root(self) -> None:
        before = self.listing()
        with self.assertRaisesRegex(BoardStoreError, "missing"):
            store.hold_exclusion(self.instance)
        self.assertEqual(self.listing(), before)
        write_store(self.instance)

        with self.assertRaisesRegex(BoardStoreError, "missing"):
            resolve(self.instance)

    def test_an_unheld_process_still_guards_every_read(self) -> None:
        write_store(self.instance)

        with self.exported(), self.assertRaisesRegex(BoardStoreError, "snapshot export copies"):
            resolve(self.instance)


class UpgradeStepTests(unittest.TestCase):
    """`step_board_store`: the three outcomes the observer decision defines for it."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.instance = Path(self.tmp.name)

    def context(self, *, dry_run: bool = False):
        return upgrade.UpgradeContext(
            instance_path=self.instance,
            product_root=Path(self.tmp.name),
            base_branch="main",
            dry_run=dry_run,
            units=None,
        )

    def test_it_runs_immediately_after_the_step_that_installs_the_driver(self) -> None:
        """§7.4's placement: the driver has to exist before the step can connect."""
        names = [step.__name__ for step in upgrade.STEPS]

        dependency = names.index("step_dependencies")
        self.assertEqual(
            names[dependency : dependency + 4],
            [
                "step_dependencies",
                "step_dependency_provenance",
                "step_board_store_provision",
                "step_board_store",
            ],
        )

    def test_an_installation_with_no_store_is_a_no_op_that_never_connects(self) -> None:
        """Every installation until the store is provisioned, including the live one today."""
        with mock.patch.object(upgrade, "migrate_instance") as migrate:
            result = upgrade.step_board_store(self.context())

        migrate.assert_not_called()
        self.assertEqual((result.name, result.status), ("board-store", "skipped"))
        self.assertIn("not configured", result.detail)
        self.assertFalse(result.failed)

    def test_a_configured_store_is_migrated_and_the_versions_are_named(self) -> None:
        write_store(self.instance)

        with mock.patch.object(upgrade, "migrate_instance", return_value=("0001_initial",)) as migrate:
            result = upgrade.step_board_store(self.context())

        migrate.assert_called_once_with(self.instance, dry_run=False)
        self.assertEqual(result.status, "changed")
        self.assertIn("0001", result.detail)

    def test_a_current_store_reports_unchanged(self) -> None:
        write_store(self.instance)

        with mock.patch.object(upgrade, "migrate_instance", return_value=()):
            result = upgrade.step_board_store(self.context())

        self.assertEqual(result.status, "unchanged")

    def test_a_dry_run_says_what_it_would_apply_and_applies_nothing(self) -> None:
        write_store(self.instance)

        with mock.patch.object(upgrade, "migrate_instance", return_value=("0001_initial",)) as migrate:
            result = upgrade.step_board_store(self.context(dry_run=True))

        migrate.assert_called_once_with(self.instance, dry_run=True)
        self.assertEqual(result.status, "would-change")
        self.assertIn("would apply", result.detail)

    def test_a_store_that_is_configured_and_broken_fails_the_step(self) -> None:
        """Never walk past it: the next thing the upgrade would do is restart services."""
        with mock.patch.object(
            upgrade, "migrate_instance", side_effect=BoardStoreError("connection refused")
        ):
            write_store(self.instance)
            result = upgrade.step_board_store(self.context())

        self.assertTrue(result.failed)
        self.assertIn("connection refused", result.detail)

    def test_a_partial_configuration_fails_before_any_driver_is_reached(self) -> None:
        write_store(self.instance, {k: v for k, v in COMPLETE.items() if k != "UMMANU_DB_NAME"})

        result = upgrade.step_board_store(self.context())

        self.assertTrue(result.failed)
        self.assertIn("UMMANU_DB_NAME", result.detail)


if __name__ == "__main__":
    unittest.main()
