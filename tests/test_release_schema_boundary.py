"""The release's schema boundary: the target's board migrations, applied before the production checkout moves.

Every test builds a real Git history and a real `postgres:16` store of its own
(`tests/sql_backend_fixtures.py`): an origin, a production checkout of it at an old commit carrying the
package's own migration bundle, and a worker branch whose commit adds a genuine revision the old bundle
does not have. `CommandHostRuntime.complete_green` then releases the branch through the push path, and
through the GitHub path with only `gh` faked (its merge is a push of the branch onto `main`), with the
production runtime registered at that checkout (`dispatch.production_checkout`). The refused release is
then taken through `release_lifecycle.release_effect` against a real card board, where the reason and the
operation card are read back through `TaskReader`. Nothing here reaches a live installation, and no
revision written here is ever packaged.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from tests.dispatcher_fixtures import CARD_REF, DispatcherRuntimeFixture, ensure_attempt
from tests.production_runtime_fixtures import RegisteredProductionRuntime
from tests.sql_backend_fixtures import OWNER, PostgresBoard, seed_client
from ummanu.board import migrate, release_migrations, schema_gate
from ummanu.board.production_rights import ACTIVATION_OPERATION_REQUEST_PREFIX, touches_production
from ummanu.board.sql_cards import SqlCardClient
from ummanu.board.store import BoardStoreConfig
from ummanu.dispatch import release_activation, release_lifecycle
from ummanu.dispatch.host import CommandHostRuntime
from ummanu.dispatch.production_checkout import ProductionActivationRefused
from ummanu.dispatch.runtime import DispatcherRuntime
from ummanu.dispatch.runtime_preflight import PACKAGE
from ummanu.dispatch.state import DispatcherRecord
from ummanu.dispatch.types import HostError
from ummanu.tasks import TaskError, TaskReader, TaskWriter

HEAD = migrate.EXPECTED_SCHEMA_REVISION
BRANCH = "pipeline/ummanu-510"
CANARY = "0025_release_canary"
FAILING = "0026_release_canary_fails"
SPRINT = "sprint:1031"
#: The entrypoint the live units execute; a release must keep it to be activated (secretary-1929).
PREFLIGHT = f"src/{PACKAGE}/dispatch/runtime_preflight.py"
MANIFEST = f'[project]\nname = "product"\n\n[project.scripts]\n{PACKAGE} = "{PACKAGE}.cli:main"\n'


def setUpModule() -> None:
    board = PostgresBoard.shared()
    # The template migration creates the cluster-wide app and read roles every database below reuses.
    board.release_database(board.fresh_database().dbname)


def git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {completed.stderr.strip()}")
    return completed.stdout.strip()


def _revision(name: str, down: str, body: str, *, safety: str | None = "additive") -> str:
    """One Alembic revision file, as a later build would ship it."""
    declared = f"release_safety = {safety!r}\n" if safety is not None else ""
    return (
        f'"""Release boundary test revision {name}: never packaged."""\n\n'
        "from __future__ import annotations\n\n"
        "import sqlalchemy as sa\n"
        "from alembic import op\n\n"
        f"revision = {name!r}\n"
        f"down_revision = {down!r}\n"
        "branch_labels = None\n"
        "depends_on = None\n"
        f"{declared}\n\n"
        "def upgrade() -> None:\n"
        + textwrap.indent(textwrap.dedent(body).strip() + "\n", "    ")
        + "\n\ndef downgrade() -> None:\n    raise NotImplementedError\n"
    )


#: A genuine additive revision: a nullable column on the table every card read selects from, and a
#: row that records whether the session running it held the migration advisory lock.
LOCK_PROBE = (
    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted AND pid = pg_backend_pid() "
    f"AND ((classid::bigint << 32) | objid::bigint) = {migrate.ADVISORY_LOCK_KEY}"
)
CANARY_BODY = f"""
op.add_column("tasks", sa.Column("release_canary", sa.Text(), nullable=True))
op.execute("CREATE TABLE release_canary_evidence AS SELECT ({LOCK_PROBE}) AS lock_held, now() AS applied_at")
"""
#: A revision that fails on the server after a statement that would have succeeded.
FAILING_BODY = """
op.add_column("tasks", sa.Column("release_canary_two", sa.Text(), nullable=True))
op.execute("UPDATE release_canary_missing SET x = 1")
"""


class _Catalog:
    """One project, `ummanu`, bound to the production checkout."""

    def __init__(self, repo: Path, instance: Path, ci: str) -> None:
        self.repo = repo
        self.instance_dir = instance
        self.ci = ci

    def binding(self, project: str) -> dict:
        return {"repo": str(self.repo), "default_branch": "main", "orca_binding": project}

    def project_default_branch(self, project: str) -> str:
        return "main"

    def integration_base(self, project: str, override: str | None) -> str:
        return override or "main"

    def adapter(self, project: str) -> dict:
        return {"validation": {"ci": self.ci}}


class _ReleaseHost(CommandHostRuntime):
    """Real Git over real repositories; `gh` answers as GitHub would after a merge that fast-forwards."""

    def __init__(self, catalog: _Catalog, root: Path, workspace: Path, *, product_root: Path) -> None:
        super().__init__(
            catalog,  # type: ignore[arg-type]
            root,
            mode="real",
            production_runtime=RegisteredProductionRuntime(  # type: ignore[arg-type]
                interpreter=str(product_root / ".venv" / "bin" / "python3"),
                product_root=str(product_root),
                import_origin=str(product_root / "src" / "ummanu" / "__init__.py"),
            ),
        )
        self.workspace = workspace
        self.labels: list[str] = []
        self.fail_label = ""

    def _run(self, args, label, *, cwd=None):  # type: ignore[override]
        self.labels.append(label)
        if label == self.fail_label:
            self.fail_label = ""
            raise HostError(f"{label} failed: injected")
        if args and args[0] == "gh":
            if args[:3] == ["gh", "pr", "merge"]:
                git(self.workspace, "push", "--quiet", "origin", f"{BRANCH}:main")
                return subprocess.CompletedProcess(list(args), 0, "", "")
            if "mergeCommit" in args:
                return subprocess.CompletedProcess(list(args), 0, git(self.workspace, "rev-parse", BRANCH) + "\n", "")
            return subprocess.CompletedProcess(list(args), 0, json.dumps({"baseRefName": "main", "isDraft": False}), "")
        return super()._run(args, label, cwd=cwd)


class ReleaseFixture:
    """A store at the packaged head, a production checkout carrying the packaged bundle, a worker branch."""

    postgres: PostgresBoard

    def setUp(self) -> None:
        super().setUp()  # type: ignore[misc]
        self.release_fixture()

    def release_fixture(self) -> None:
        self.postgres = PostgresBoard.shared()
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="release-schema-")))  # type: ignore[attr-defined]
        self._serial = 0

    # --- the store -----------------------------------------------------------------------------

    def database(self) -> BoardStoreConfig:
        import psycopg
        import sqlalchemy as sa

        self._serial += 1
        name = f"release_schema_{os.getpid()}_{id(self) % 100000}_{self._serial}"
        with psycopg.connect(self.postgres.config("postgres").for_role("owner").conninfo(), autocommit=True) as c:
            c.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
            c.execute(f"CREATE DATABASE {name} OWNER {OWNER}")
        self.addCleanup(self.postgres.drop_database, name)  # type: ignore[attr-defined]
        config = self.postgres.config(name)
        engine = sa.create_engine(migrate.sqlalchemy_url(config.for_role("owner")))
        try:
            with engine.connect() as connection:
                migrate.apply(connection, passwords=migrate.passwords_for(config), reuse_existing_roles=True)
        finally:
            engine.dispose()
        return config

    def instance(self, config: BoardStoreConfig, directory: Path | None = None) -> Path:
        instance = directory or self.root / "instance"
        instance.mkdir(parents=True, exist_ok=True)
        path = instance / "board-store.env"
        path.write_text("".join(f"{key}={value}\n" for key, value in config.as_environ().items()), encoding="utf-8")
        path.chmod(0o600)
        registry = instance / "projects"
        registry.mkdir(exist_ok=True)
        (registry / "ummanu.yaml").write_text("id: ummanu\n", encoding="utf-8")
        return instance

    def query(self, config: BoardStoreConfig, sql: str) -> list[tuple]:
        import psycopg

        with psycopg.connect(config.for_role("owner").conninfo(), autocommit=True) as c:
            return [tuple(row) for row in c.execute(sql).fetchall()]

    def version(self, config: BoardStoreConfig) -> list[str]:
        return [str(row[0]) for row in self.query(config, "SELECT version_num FROM alembic_version")]

    def column(self, config: BoardStoreConfig, table: str, column: str) -> bool:
        return bool(
            self.query(
                config,
                "SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' "
                f"AND table_name = '{table}' AND column_name = '{column}'",
            )
        )

    @contextlib.contextmanager
    def applies(self) -> Any:
        """What every `migrate.apply` inside the block returned: the revisions it ran."""
        results: list[tuple[str, ...]] = []
        real = migrate.apply

        def recording(*args: Any, **kwargs: Any) -> tuple[str, ...]:
            results.append(real(*args, **kwargs))
            return results[-1]

        with mock.patch.object(migrate, "apply", side_effect=recording):
            yield results

    def advisory_locks(self, config: BoardStoreConfig) -> int:
        return int(self.query(config, "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'")[0][0])

    # --- the history ---------------------------------------------------------------------------

    def repos(self) -> tuple[Path, Path]:
        """(production checkout, worker workspace): both at the old commit, which ships the packaged bundle."""
        origin, production, workspace = self.root / "origin.git", self.root / "ummanu", self.root / "workspace"
        git(self.root, "init", "--quiet", "--bare", "--initial-branch", "main", str(origin))
        git(self.root, "clone", "--quiet", str(origin), str(production))
        for repo in (production,):
            git(repo, "config", "user.name", "Test User")
            git(repo, "config", "user.email", "test@example.invalid")
        bundle = production / release_migrations.MIGRATIONS_TREE
        shutil.copytree(migrate.SCRIPT_LOCATION, bundle, ignore=shutil.ignore_patterns("__pycache__"))
        (production / "README.md").write_text("old release\n", encoding="utf-8")
        (production / PREFLIGHT).parent.mkdir(parents=True)
        (production / PREFLIGHT).write_text("PACKAGE = 'x'\n", encoding="utf-8")
        (production / "pyproject.toml").write_text(MANIFEST, encoding="utf-8")
        git(production, "add", "-A")
        git(production, "commit", "--quiet", "-m", "old release")
        git(production, "push", "--quiet", "origin", "main")
        git(self.root, "clone", "--quiet", str(origin), str(workspace))
        git(workspace, "config", "user.name", "Test User")
        git(workspace, "config", "user.email", "test@example.invalid")
        git(workspace, "checkout", "--quiet", "-b", BRANCH)
        return production, workspace

    def commit(self, workspace: Path, files: dict[str, str], message: str) -> str:
        for relative, text in files.items():
            path = workspace / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        git(workspace, "add", "-A")
        git(workspace, "commit", "--quiet", "-m", message)
        return git(workspace, "rev-parse", "HEAD")

    def revisions(self, workspace: Path, *revisions: tuple[str, str, str, str | None]) -> str:
        """Commit (name, down_revision, body, release_safety) revision files onto the worker branch."""
        return self.commit(
            workspace,
            {
                f"{release_migrations.MIGRATIONS_TREE}/versions/{name}.py": _revision(name, down, body, safety=safety)
                for name, down, body, safety in revisions
            },
            "a later build's migrations",
        )

    def release_host(self, production: Path, workspace: Path, instance: Path, *, ci: str = "local") -> _ReleaseHost:
        return _ReleaseHost(_Catalog(production, instance, ci), self.root, workspace, product_root=production)

    def release(self, host: _ReleaseHost, workspace: Path) -> Any:
        return host.complete_green({"ref": "ummanu-510", "project": "ummanu"}, SimpleNamespace(workspace=str(workspace)))


class MigrationRunnerCleanupTests(ReleaseFixture, unittest.TestCase):
    """`migrate.apply` ends a failed run with rollback, then unlock, and never lets either replace the failure."""

    def owner_connection(self, config: BoardStoreConfig) -> Any:
        import sqlalchemy as sa

        engine = sa.create_engine(migrate.sqlalchemy_url(config.for_role("owner")))
        self.addCleanup(engine.dispose)
        connection = engine.connect()
        self.addCleanup(connection.close)
        return connection

    def test_a_failed_statement_inside_the_locked_run_is_the_error_the_caller_sees(self) -> None:
        import sqlalchemy as sa

        config = self.database()
        connection = self.owner_connection(config)

        def failing(conn: Any, script: Any = None) -> tuple[str, ...]:
            conn.exec_driver_sql("SELECT * FROM release_no_such_table")
            return ()

        with mock.patch.object(migrate, "pending", side_effect=failing), self.assertRaises(
            sa.exc.ProgrammingError
        ) as raised:
            migrate.apply(connection, passwords=migrate.passwords_for(config))

        self.assertIn('relation "release_no_such_table" does not exist', str(raised.exception))
        self.assertNotIn("current transaction is aborted", str(raised.exception))
        self.assertEqual(self.advisory_locks(config), 0, "unlocked after the rollback")
        self.assertEqual(connection.exec_driver_sql("SELECT 1").scalar(), 1, "the session is usable again")

    def test_a_cleanup_that_cannot_run_discards_the_session_and_keeps_the_failure(self) -> None:
        import psycopg

        config = self.database()
        connection = self.owner_connection(config)
        other = psycopg.connect(config.for_role("owner").conninfo(), autocommit=True)
        self.addCleanup(other.close)

        def dies(conn: Any, script: Any = None) -> tuple[str, ...]:
            pid = conn.exec_driver_sql("SELECT pg_backend_pid()").scalar()
            other.execute("SELECT pg_terminate_backend(%s)", (pid,))
            raise RuntimeError("the run failed first")

        with mock.patch.object(migrate, "pending", side_effect=dies), self.assertRaisesRegex(
            RuntimeError, "the run failed first"
        ):
            migrate.apply(connection, passwords=migrate.passwords_for(config))

        self.assertTrue(connection.invalidated)
        deadline = time.monotonic() + 5
        while self.advisory_locks(config) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.advisory_locks(config), 0, "the lock ended with its session")


class ReleaseAppliesTheTargetSchemaFirstTests(ReleaseFixture, unittest.TestCase):
    def test_push_release_applies_an_owed_additive_revision_under_the_lock_then_moves_the_checkout(self) -> None:
        config = self.database()
        instance = self.instance(config)
        production, workspace = self.repos()
        old = git(production, "rev-parse", "HEAD")
        target = self.revisions(workspace, (CANARY, HEAD, CANARY_BODY, "additive"))
        self.assertFalse((Path(migrate.SCRIPT_LOCATION) / "versions" / f"{CANARY}.py").exists())
        host = self.release_host(production, workspace, instance)
        seen: list[tuple[str, Any]] = []
        real_prepare = release_migrations.prepare

        def observed(repo: Path, pinned: str, instance_dir: Path) -> Any:
            # The migration runs while the checkout is still on the old code, for the pinned target.
            seen.append(("before", git(production, "rev-parse", "HEAD"), pinned, list(host.labels)))
            result = real_prepare(repo, pinned, instance_dir)
            seen.append(("after", git(production, "rev-parse", "HEAD"), self.version(config)))
            return result

        with mock.patch.object(release_migrations, "prepare", side_effect=observed):
            landing = self.release(host, workspace)

        self.assertEqual(seen[0][:3], ("before", old, target))
        self.assertEqual(seen[1], ("after", old, [CANARY]))
        # Pin, then ancestry, then the entrypoint (the preflight file, then the manifest), then the
        # schema, then and only then the fast-forward, read back.
        self.assertEqual(
            host.labels[host.labels.index("post-merge fetch") :],
            [
                "post-merge fetch",
                "post-merge target",
                "post-merge checkout head",
                "post-merge ancestry",
                "post-merge entrypoint guard",
                "post-merge entrypoint guard",
                "post-merge fast-forward",
                "post-merge checkout head",
                "post-merge landed commit",
            ],
        )
        self.assertEqual(
            seen[0][3][-3:],
            ["post-merge ancestry", "post-merge entrypoint guard", "post-merge entrypoint guard"],
            "the schema step follows the ancestry check and the entrypoint guard",
        )
        self.assertEqual(git(production, "rev-parse", "HEAD"), target)
        self.assertEqual((landing.sha, landing.path), (target, "push"))
        self.assertEqual(self.version(config), [CANARY])
        self.assertTrue(self.column(config, "tasks", "release_canary"))
        # The revision ran on the session holding the migration lock, and the lock is gone again.
        self.assertEqual(self.query(config, "SELECT lock_held FROM release_canary_evidence"), [(1,)])
        self.assertEqual(self.advisory_locks(config), 0)

    def test_a_current_store_takes_no_migration_and_the_checkout_still_moves(self) -> None:
        config = self.database()
        instance = self.instance(config)
        production, workspace = self.repos()
        target = self.commit(workspace, {"README.md": "code only\n"}, "a release with no migration")
        host = self.release_host(production, workspace, instance)

        with self.applies() as applied:
            self.release(host, workspace)

        self.assertEqual(applied, [()], "one read under the lock, nothing owed, nothing run")
        self.assertEqual(git(production, "rev-parse", "HEAD"), target)
        self.assertEqual(self.version(config), [HEAD])
        self.assertEqual(self.advisory_locks(config), 0)

    def test_the_old_runtime_keeps_reading_and_writing_after_the_future_additive_revision(self) -> None:
        from tests.fakes.dispatcher import dispatcher_seed

        config = self.database()
        instance = self.instance(config)
        seed_client(config, dispatcher_seed(), instance).close()
        production, workspace = self.repos()
        self.revisions(workspace, (CANARY, HEAD, CANARY_BODY, "additive"))

        self.release(self.release_host(production, workspace, instance), workspace)

        self.assertEqual(self.version(config), [CANARY])
        import psycopg

        with psycopg.connect(config.for_role("read").conninfo()) as connection:
            assessment = schema_gate.assess(connection)
        self.assertEqual(assessment.state, schema_gate.AHEAD, "this build reads a later build's schema as additive")
        for role in ("app", "read"):
            client = SqlCardClient(config.for_role(role), instance)
            self.addCleanup(client.close)
            with self.subTest(role=role):
                shown = TaskReader(client).show(CARD_REF)
                self.assertEqual((shown["ref"], shown["title"]), (CARD_REF, "Pilot"))
                self.assertIn("ummanu-511", [card["ref"] for card in TaskReader(client).list()])
        writer = TaskWriter(SqlCardClient(config.for_role("app"), instance), data_dir=self.root, workspace=self.root)
        self.addCleanup(writer.client.close)
        writer.comment(role="observer", actor="observer", reference=CARD_REF, body="still writes", request_id="after-0025")
        self.assertIn("still writes", TaskReader(writer.client).show(CARD_REF)["comments"][-1]["body"])

    def test_a_github_release_applies_the_revision_before_refreshing_the_production_checkout(self) -> None:
        config = self.database()
        instance = self.instance(config)
        production, workspace = self.repos()
        target = self.revisions(workspace, (CANARY, HEAD, CANARY_BODY, "additive"))
        host = self.release_host(production, workspace, instance, ci="github")

        landing = self.release(host, workspace)

        self.assertEqual((landing.sha, landing.path), (target, "github-pr"))
        self.assertEqual(git(production, "rev-parse", "HEAD"), target)
        self.assertEqual(self.version(config), [CANARY])
        merge = host.labels.index("merge pr")
        self.assertLess(merge, host.labels.index("post-merge target"))
        self.assertLess(host.labels.index("post-merge ancestry"), host.labels.index("post-merge fast-forward"))

    def test_a_checkout_that_is_not_production_fast_forwards_without_touching_any_board(self) -> None:
        production, workspace = self.repos()
        target = self.revisions(workspace, (CANARY, HEAD, CANARY_BODY, "additive"))
        # No board-store.env at all: the boundary would refuse, the plain fast-forward does not ask.
        host = _ReleaseHost(
            _Catalog(production, self.root / "no-instance", "local"),
            self.root,
            workspace,
            product_root=self.root / "elsewhere",
        )
        with mock.patch.object(release_migrations, "prepare", side_effect=AssertionError("not production")):
            self.release(host, workspace)
        self.assertEqual(git(production, "rev-parse", "HEAD"), target)
        self.assertNotIn("post-merge target", host.labels)


class RefusedReleaseKeepsTheCheckoutTests(ReleaseFixture, unittest.TestCase):
    def failing_release(self, *, ci: str = "local") -> tuple[BoardStoreConfig, Path, str, str, ProductionActivationRefused]:
        config = self.database()
        instance = self.instance(config)
        production, workspace = self.repos()
        old = git(production, "rev-parse", "HEAD")
        target = self.revisions(
            workspace, (CANARY, HEAD, CANARY_BODY, "additive"), (FAILING, CANARY, FAILING_BODY, "additive")
        )
        with self.assertRaises(ProductionActivationRefused) as raised:
            self.release(self.release_host(production, workspace, instance, ci=ci), workspace)
        return config, production, old, target, raised.exception

    def test_a_failing_revision_keeps_the_checkout_and_names_the_revision_the_target_and_its_cause(self) -> None:
        config, production, old, target, refused = self.failing_release()

        self.assertEqual(git(production, "rev-parse", "HEAD"), old)
        facts = refused.facts()
        self.assertEqual(
            {key: facts[key] for key in ("code", "reason", "revision", "target", "old", "applied", "pending")},
            {
                "code": "release_schema_refused",
                "reason": "migration_failed",
                "revision": FAILING,
                "target": target,
                "old": old,
                "applied": [CANARY],
                "pending": [CANARY, FAILING],
            },
        )
        # The server's own cause survives the rollback and the unlock.
        self.assertIn('relation "release_canary_missing" does not exist', facts["cause"])
        self.assertNotIn("current transaction is aborted", str(refused))
        self.assertEqual(facts["remote_merge"], {"sha": target, "base": "main", "path": "push", "branch": ""})
        # What succeeded is committed; the failing revision left nothing, and no lock or session stays.
        self.assertEqual(self.version(config), [CANARY])
        self.assertTrue(self.column(config, "tasks", "release_canary"))
        self.assertFalse(self.column(config, "tasks", "release_canary_two"))
        self.assertEqual(self.advisory_locks(config), 0)
        self.assertEqual(
            self.query(config, "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                       "AND pid <> pg_backend_pid()"),
            [(0,)],
        )

    def test_the_github_path_raises_the_refusal_instead_of_swallowing_it_as_a_refresh(self) -> None:
        _config, production, old, target, refused = self.failing_release(ci="github")

        self.assertEqual(git(production, "rev-parse", "HEAD"), old)
        self.assertEqual(refused.facts()["remote_merge"]["path"], "github-pr")
        self.assertEqual(refused.facts()["remote_merge"]["sha"], target)

    def test_an_ordinary_refresh_failure_on_the_github_path_is_still_swallowed_and_migrates_nothing(self) -> None:
        config = self.database()
        instance = self.instance(config)
        production, workspace = self.repos()
        self.revisions(workspace, (CANARY, HEAD, CANARY_BODY, "additive"))
        # A preserved local commit: the production checkout cannot fast-forward.
        local = self.commit(production, {"LOCAL.md": "local\n"}, "a local commit")

        landing = self.release(self.release_host(production, workspace, instance, ci="github"), workspace)

        self.assertEqual(landing.path, "github-pr")
        self.assertEqual(git(production, "rev-parse", "HEAD"), local)
        self.assertEqual(self.version(config), [HEAD], "a checkout that cannot move is not migrated for")

    def test_destructive_and_unclassified_revisions_are_refused_before_any_runs(self) -> None:
        for safety, reason in (("destructive", "destructive"), (None, "unclassified"), ("maybe", "unclassified")):
            with self.subTest(safety=safety):
                config = self.database()
                instance = self.instance(config, self.root / f"instance-{reason}-{safety}")
                shutil.rmtree(self.root / "origin.git", ignore_errors=True)
                shutil.rmtree(self.root / "ummanu", ignore_errors=True)
                shutil.rmtree(self.root / "workspace", ignore_errors=True)
                production, workspace = self.repos()
                old = git(production, "rev-parse", "HEAD")
                # An additive revision first: it must not run either, since the batch is refused whole.
                self.revisions(
                    workspace,
                    (CANARY, HEAD, CANARY_BODY, "additive"),
                    ("0026_release_drop", CANARY, 'op.drop_column("tasks", "title")', safety),
                )

                with self.assertRaises(ProductionActivationRefused) as raised:
                    self.release(self.release_host(production, workspace, instance), workspace)

                facts = raised.exception.facts()
                self.assertEqual((facts["reason"], facts["revision"]), (reason, "0026_release_drop"))
                self.assertIn('release_safety = "additive"', facts["message"])
                self.assertEqual(facts["applied"], [])
                self.assertEqual(git(production, "rev-parse", "HEAD"), old)
                self.assertEqual(self.version(config), [HEAD])
                self.assertTrue(self.column(config, "tasks", "title"))
                self.assertFalse(self.column(config, "tasks", "release_canary"))

    def test_a_held_migration_lock_refuses_within_the_bound(self) -> None:
        import psycopg

        config = self.database()
        instance = self.instance(config)
        production, workspace = self.repos()
        old = git(production, "rev-parse", "HEAD")
        self.revisions(workspace, (CANARY, HEAD, CANARY_BODY, "additive"))
        holder = psycopg.connect(config.for_role("owner").conninfo(), autocommit=True)
        self.addCleanup(holder.close)
        holder.execute("SELECT pg_advisory_lock(%s)", (migrate.ADVISORY_LOCK_KEY,))

        started = time.monotonic()
        with mock.patch.dict(os.environ, {release_migrations.LOCK_TIMEOUT_ENV: "1"}), self.assertRaises(
            ProductionActivationRefused
        ) as raised:
            self.release(self.release_host(production, workspace, instance), workspace)

        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual(raised.exception.refusal.reason, "lock_timeout")
        self.assertEqual(git(production, "rev-parse", "HEAD"), old)
        self.assertEqual(self.version(config), [HEAD])
        holder.execute("SELECT pg_advisory_unlock(%s)", (migrate.ADVISORY_LOCK_KEY,))
        self.assertEqual(self.advisory_locks(config), 0, "the refused run holds nothing")

    def test_an_unreadable_graph_or_store_refuses_before_the_checkout_moves(self) -> None:
        config = self.database()
        production, workspace = self.repos()
        old = git(production, "rev-parse", "HEAD")
        target = self.revisions(workspace, (CANARY, "0099_nowhere", CANARY_BODY, "additive"))
        host = self.release_host(production, workspace, self.instance(config))
        with self.assertRaises(ProductionActivationRefused) as graph:
            self.release(host, workspace)
        self.assertEqual(graph.exception.refusal.reason, "bundle_unreadable")
        self.assertEqual(git(production, "rev-parse", "HEAD"), old)
        with self.assertRaises(release_migrations.ReleaseSchemaRefused) as store:
            release_migrations.prepare(production, target, self.root / "no-store")
        self.assertEqual(store.exception.reason, "bundle_unreadable")
        good = git(production, "rev-parse", "HEAD")
        with self.assertRaises(release_migrations.ReleaseSchemaRefused) as missing:
            release_migrations.prepare(production, good, self.root / "no-store")
        self.assertEqual(missing.exception.reason, "store_unavailable")
        with self.assertRaises(release_migrations.ReleaseSchemaRefused) as moving:
            release_migrations.prepare(production, "origin/main", self.root / "no-store")
        self.assertEqual(moving.exception.reason, "bundle_unreadable", "a ref is not a pinned target")

    def test_refused_owner_credentials_refuse_before_the_checkout_moves(self) -> None:
        config = self.database()
        production, workspace = self.repos()
        old = git(production, "rev-parse", "HEAD")
        self.revisions(workspace, (CANARY, HEAD, CANARY_BODY, "additive"))
        refused_login = self.instance(config, self.root / "wrong-password")
        store = refused_login / "board-store.env"
        store.write_text(
            store.read_text(encoding="utf-8").replace(
                f"UMMANU_DB_OWNER_PASSWORD={config.owner_password}", "UMMANU_DB_OWNER_PASSWORD=not-it"
            ),
            encoding="utf-8",
        )
        self.assertIn("not-it", store.read_text(encoding="utf-8"))
        with self.assertRaises(ProductionActivationRefused) as credentials:
            self.release(self.release_host(production, workspace, refused_login), workspace)
        self.assertEqual(credentials.exception.refusal.reason, "store_unavailable")
        self.assertIn("password authentication failed", credentials.exception.refusal.cause)
        self.assertEqual(git(production, "rev-parse", "HEAD"), old)
        self.assertEqual(self.version(config), [HEAD])


class ReplayTests(ReleaseFixture, unittest.TestCase):
    def test_a_replay_after_the_migration_and_before_the_move_applies_nothing_twice(self) -> None:
        config = self.database()
        instance = self.instance(config)
        production, workspace = self.repos()
        old = git(production, "rev-parse", "HEAD")
        target = self.revisions(workspace, (CANARY, HEAD, CANARY_BODY, "additive"))
        host = self.release_host(production, workspace, instance)
        host.fail_label = "post-merge fast-forward"

        with self.assertRaises(HostError):
            self.release(host, workspace)
        self.assertEqual((git(production, "rev-parse", "HEAD"), self.version(config)), (old, [CANARY]))

        # The next tick replays the release: the push is a no-op, nothing is owed, the checkout moves.
        with self.applies() as applied:
            replayed = self.release(self.release_host(production, workspace, instance), workspace)
        self.assertEqual(applied, [()], "the revision applied before the interruption is not owed again")
        self.assertEqual(replayed.sha, target)
        self.assertEqual(git(production, "rev-parse", "HEAD"), target)
        # Applied once: the revision's own evidence row is the only one, and its table was not recreated.
        self.assertEqual(self.query(config, "SELECT count(*) FROM release_canary_evidence"), [(1,)])

    def test_two_runners_racing_apply_the_revision_once(self) -> None:
        config = self.database()
        instance = self.instance(config)
        production, workspace = self.repos()
        target = self.revisions(workspace, (CANARY, HEAD, CANARY_BODY, "additive"))
        git(workspace, "push", "--quiet", "origin", BRANCH)
        git(production, "fetch", "--quiet", "origin", BRANCH)
        results: list[Any] = []

        def run() -> None:
            try:
                results.append(release_migrations.prepare(production, target, instance))
            except Exception as exc:  # noqa: BLE001 - the assertion below names it
                results.append(exc)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual(sorted(result.applied for result in results), [(), (CANARY,)], results)
        self.assertEqual(self.version(config), [CANARY])


class RefusedActivationOnTheBoardTests(DispatcherRuntimeFixture, ReleaseFixture, unittest.TestCase):
    """The refused release through `release_effect`, on the card board the PO reads."""

    def setUp(self) -> None:
        DispatcherRuntimeFixture.setUp(self)
        self.release_fixture()

    def arrange(
        self, *, ci: str = "local", partial: bool = False, moved: bool = False
    ) -> tuple[DispatcherRecord, dict, dict]:
        self.start_dispatcher()
        self.board.move(self.board.key_of(CARD_REF), "validate")
        self.writer.move(role="dispatcher", actor=self.runtime.owner, reference=CARD_REF,
                         target="assessment", reason="release fixture", request_id="release-fixture-park")
        self._decide("accept")
        # The production board is the card board: the release migrates the store it then writes to.
        self.instance(self.board_config(), self.data_dir)
        production, workspace = self.repos()
        self.old = git(production, "rev-parse", "HEAD")
        revisions = [(FAILING, HEAD, FAILING_BODY, "additive")]
        if partial:
            revisions = [(CANARY, HEAD, CANARY_BODY, "additive"), (FAILING, CANARY, FAILING_BODY, "additive")]
            # This case changes the schema, so discard its database before the ordinary fixture
            # cleanup can return it to the empty-row reuse pool.
            self.addCleanup(self.postgres.drop_database, self.board.credentials.dbname)
        if moved:
            # The rename: the package directory moves, so the live units' entrypoint is gone.
            git(workspace, "mv", f"src/{PACKAGE}", "src/renamed_package")
            self.target = self.commit(workspace, {}, "rename the package")
        else:
            self.target = self.revisions(workspace, *revisions)
        self.production, self.workspace = production, workspace
        releasing = _ReleaseHost(_Catalog(production, self.data_dir, ci), self.root, workspace, product_root=production)
        self.host.complete_green = lambda task, record: self.release(releasing, workspace)  # type: ignore[method-assign]
        record = DispatcherRecord(
            worker="w", workspace=str(workspace), handle="", head="codex", review_head="claude",
            attempt_id="attempt-release", comment_baseline=0, review_baseline=0, state="assessment", claimed_at=0.0,
        )
        payload = self.runtime.production_state.load()
        ensure_attempt(payload, CARD_REF, self.runtime.owner, self.runtime.owner)
        return record, {CARD_REF: record}, payload

    def board_config(self) -> BoardStoreConfig:
        return self.postgres.config(self.board.credentials.dbname)

    def release_card(self, record: DispatcherRecord, records: dict, payload: dict) -> dict:
        return release_lifecycle.release_effect(
            self.runtime, self.reader.show(CARD_REF), record, records, payload, record.attempt_id,
            step="assessment", move_reason="Observer decision: accept.", decision="accept",
        )

    def operations(self) -> list[dict[str, Any]]:
        return [card for card in self.reader.list() if card.get("type") == "operation"]

    def restart(self) -> None:
        """Construct a new runtime; its next tick loads the obligation from the production state."""
        self.runtime = DispatcherRuntime(
            self.reader, self.writer, self.writer.audit, self.data_dir, self.catalog, self.host,
            owner="ummanu-pilot", sprints=self.sprints,
        )

    def obligation(self) -> dict[str, Any]:
        persisted = self.runtime.production_state.load()["records"][CARD_REF]["activation_recovery"]
        restored = self.runtime.production_state.records(self.runtime.production_state.load())[CARD_REF]
        self.assertEqual(restored.activation_recovery.to_json(), persisted)
        facts = persisted["facts"]
        self.assertEqual((facts["old"], facts["target"], facts["revision"]), (self.old, self.target, FAILING))
        self.assertIn('relation "release_canary_missing" does not exist', facts["cause"])
        self.assertNotIn("current transaction is aborted", facts["cause"])
        self.assertEqual(facts["remote_merge"]["sha"], self.target)
        self.assertEqual(git(self.production, "rev-parse", "HEAD"), self.old)
        return persisted

    def settled(self, owed: dict[str, Any]) -> None:
        card = self.reader.show(CARD_REF)
        self.assertEqual(card["state"], "blocked")
        [body] = [c["body"] for c in card["comments"] if release_activation.HEADING in c["body"]]
        facts = json.loads(body.split("```json\n", 1)[1].split("\n```", 1)[0])
        [operation] = self.operations()
        self.assertEqual(facts, {**owed["facts"], "operation": operation["ref"]})
        self.assertEqual((operation["project"], operation["sprint"], touches_production(operation)),
                         ("ummanu", SPRINT, "ummanu"))
        self.assertEqual(operation["description"], owed["operation"]["description"])
        [created] = self.writer.audit.events(operation["ref"], kind="created")
        self.assertEqual(created["request_id"], owed["operation"]["request_id"])
        [blocked] = [e for e in self.writer.audit.events(CARD_REF)
                     if (e.get("transition") or {}).get("target") == "blocked"]
        self.assertEqual(blocked["request_id"], owed["block_request_id"])
        self.assertIn(operation["ref"], str(blocked))
        self.assertEqual(git(self.production, "rev-parse", "HEAD"), self.old)
        self.assertNotIn(CARD_REF, self.runtime.production_state.load()["records"])

    def recovered_tick(self, owed: dict[str, Any], *, full: bool = False) -> dict:
        self.restart()
        with mock.patch.object(self.host, "complete_green", side_effect=AssertionError("activation rerun")):
            outcome = self.runtime.production_tick() if full else self.tick()
        self.settled(owed)
        return outcome

    def test_native_registry_refusal_survives_reload_and_an_ordinary_production_tick(self) -> None:
        record, records, payload = self.arrange(partial=True)
        registry = self.data_dir / "projects" / "ummanu.yaml"
        withheld = registry.with_suffix(".withheld")
        registry.rename(withheld)
        try:
            outcome = self.release_card(record, records, payload)
            self.assertEqual(outcome["status"], "degraded")
            self.assertIn("unknown registered project: ummanu", outcome["reason"])
            owed = self.obligation()
            self.assertEqual(owed["facts"]["applied"], [CANARY])
            self.assertEqual(owed["facts"]["pending"], [CANARY, FAILING])
            self.assertEqual(self.version(self.board_config()), [CANARY])
            self.assertEqual(self.operations(), [])
            self.assertEqual(self.reader.show(CARD_REF)["state"], "assessment")
            self.assertFalse(any(release_activation.HEADING in c["body"]
                                 for c in self.reader.show(CARD_REF)["comments"]))
        finally:
            withheld.rename(registry)
        # Moving the remote and the attempt cannot change the request or its original facts.
        later = self.commit(self.workspace, {"LATER.md": "later\n"}, "a later remote commit")
        git(self.workspace, "push", "--quiet", "origin", f"{BRANCH}:main")
        self.assertNotEqual(later, self.target)
        persisted = self.runtime.production_state.load()
        persisted["records"][CARD_REF]["attempt_id"] = "a-later-tick"
        self.runtime.production_state.save(persisted)
        self.recovered_tick(owed, full=True)
        # Ordinary orphan reconciliation after settlement cannot multiply the writes.
        self.runtime.production_tick()
        self.settled(owed)

    def test_one_shot_sprint_guard_unavailable_retains_the_original_github_delivery(self) -> None:
        from ummanu.tasks import SprintReservationUnverifiable

        record, records, payload = self.arrange(ci="github")
        with mock.patch.object(self.writer, "open_sprints_reserving",
                               side_effect=SprintReservationUnverifiable(SPRINT, TaskError("unavailable", "one-shot outage", 4))):
            outcome = self.release_card(record, records, payload)
        self.assertEqual(outcome["status"], "degraded")
        self.assertIn("sprint_guard_unavailable", outcome["reason"])
        owed = self.obligation()
        self.assertEqual(owed["facts"]["remote_merge"],
                         {"sha": self.target, "base": "main", "path": "github-pr", "branch": BRANCH})
        self.assertEqual(self.operations(), [])
        self.assertEqual(self.reader.show(CARD_REF)["state"], "assessment")
        self.recovered_tick(owed, full=True)

    def test_interruptions_around_each_board_write_recover_from_durable_state(self) -> None:
        for method in ("create", "comment", "move"):
            for after in (False, True):
                # Each case owns a fresh board and native Git history.
                with self.subTest(method=method, after=after), contextlib.ExitStack() as cleanups:
                    case = RefusedActivationOnTheBoardTests()
                    case.setUp()
                    cleanups.callback(case.doCleanups)
                    cleanups.callback(case.tearDown)
                    record, records, payload = case.arrange(partial=True)
                    original = getattr(case.writer, method)

                    def interrupt(*, case=case, after=after, original=original, **kwargs):
                        case.obligation()  # Durable before the first create call.
                        if after:
                            original(**kwargs)
                        raise RuntimeError("tick interrupted")

                    with (
                        mock.patch.object(case.writer, method, side_effect=interrupt),
                        case.assertRaisesRegex(RuntimeError, "tick interrupted"),
                    ):
                        case.release_card(record, records, payload)
                    owed = case.obligation()
                    expected_operations = int(method != "create" or after)
                    case.assertEqual(len(case.operations()), expected_operations)
                    reasons = [c for c in case.reader.show(CARD_REF)["comments"]
                               if release_activation.HEADING in c["body"]]
                    case.assertEqual(len(reasons), int(method == "move" or (method == "comment" and after)))
                    case.assertEqual(case.reader.show(CARD_REF)["state"],
                                     "blocked" if method == "move" and after else "assessment")
                    case.recovered_tick(owed, full=True)

    def test_final_state_save_failure_retains_the_obligation_after_source_block(self) -> None:
        record, records, payload = self.arrange()
        save = self.runtime.save_records

        def fail_removal(payload, records):
            if CARD_REF not in records:
                raise OSError("state save interrupted")
            return save(payload, records)

        with mock.patch.object(self.runtime, "save_records", side_effect=fail_removal):
            outcome = self.release_card(record, records, payload)
        self.assertEqual(outcome["status"], "degraded")
        self.assertIn(CARD_REF, records)
        self.assertEqual(self.reader.show(CARD_REF)["state"], "blocked")
        owed = self.obligation()
        self.recovered_tick(owed, full=True)

    def test_operation_audit_failure_rolls_back_creation_and_retries_the_exact_request(self) -> None:
        record, records, payload = self.arrange()
        append = self.writer.audit.append

        def refuse_operation_audit(request_id, event):
            if request_id.startswith(ACTIVATION_OPERATION_REQUEST_PREFIX):
                raise OSError("operation audit unavailable")
            return append(request_id, event)

        with mock.patch.object(self.writer.audit, "append", side_effect=refuse_operation_audit):
            outcome = self.release_card(record, records, payload)
        self.assertEqual(outcome["status"], "degraded")
        self.assertIn("audit_pending", outcome["reason"])
        owed = self.obligation()
        self.assertEqual(self.operations(), [])
        self.assertIsNone(self.writer.audit.committed_event(owed["operation"]["request_id"]))
        # PostgreSQL owns the claim, card and event in one transaction; an append failure rolls
        # all three back. Only the dispatcher's release obligation remains durable.
        self.assertIsNone(self.writer.audit.pending_event(owed["operation"]["request_id"]))
        self.assertEqual(self.reader.show(CARD_REF)["state"], "assessment")
        self.recovered_tick(owed, full=True)

    def test_a_failed_initial_state_save_cannot_create_or_settle_anything(self) -> None:
        record, records, payload = self.arrange()
        with (
            mock.patch.object(self.runtime, "save_records", side_effect=OSError("disk unavailable")),
            self.assertRaisesRegex(OSError, "disk unavailable"),
        ):
            self.release_card(record, records, payload)
        self.assertEqual(self.operations(), [])
        self.assertEqual(self.reader.show(CARD_REF)["state"], "assessment")
        self.assertEqual(git(self.production, "rev-parse", "HEAD"), self.old)
        # The same process can flush its retained facts before retrying creation.
        self.release_card(record, records, payload)
        self.assertEqual(len(self.operations()), 1)

    def test_an_interruption_after_final_state_save_needs_no_recovery_record(self) -> None:
        record, records, payload = self.arrange()
        save = self.runtime.save_records
        owed = None

        def after_removal(payload, records):
            nonlocal owed
            if CARD_REF in records:
                result = save(payload, records)
                owed = self.obligation()
                return result
            save(payload, records)
            raise RuntimeError("tick died after settlement")

        with (
            mock.patch.object(self.runtime, "save_records", side_effect=after_removal),
            self.assertRaisesRegex(RuntimeError, "tick died after settlement"),
        ):
            self.release_card(record, records, payload)
        self.assertIsNotNone(owed)
        self.recovered_tick(owed, full=True)

    def test_the_card_blocks_with_one_typed_reason_and_one_operation_for_the_sprint_po(self) -> None:
        record, records, payload = self.arrange()

        outcome = self.release_card(record, records, payload)

        self.assertEqual((outcome["status"], outcome["reason"]), ("blocked", "production activation refused"))
        self.assertEqual(outcome["activation_refused"]["revision"], FAILING)
        card = self.reader.show(CARD_REF)
        self.assertEqual(card["state"], "blocked")
        [reason] = [c["body"] for c in card["comments"] if release_activation.HEADING in c["body"]]
        self.assertIn('"code": "release_schema_refused"', reason)
        self.assertIn('"reason": "migration_failed"', reason)
        self.assertIn(f'"revision": "{FAILING}"', reason)
        self.assertIn(f'"target": "{self.target}"', reason)
        self.assertIn(f'"old": "{self.old}"', reason)
        self.assertIn('relation \\"release_canary_missing\\" does not exist', reason)
        blocked = [e for e in self.writer.audit.events(CARD_REF) if (e.get("transition") or {}).get("target") == "blocked"]
        self.assertEqual(len(blocked), 1)
        self.assertIn(f"migration_failed at revision {FAILING}", str(blocked[0]))
        [operation] = self.operations()
        self.assertEqual(
            (operation["project"], operation["sprint"], operation["state"], touches_production(operation)),
            ("ummanu", SPRINT, "ready", "ummanu"),
        )
        self.assertIn(operation["ref"], reason)
        body = operation["description"]
        for needle in (self.old, self.target, FAILING, "Applied by this release (committed, additive): none",
                       "ummanu upgrade", "ummanu doctor --json", "Delivered to the remote"):
            self.assertIn(needle, body)
        [created] = self.writer.audit.events(operation["ref"], kind="created")
        self.assertEqual(created["actor"]["role"], "dispatcher")
        self.assertTrue(created["request_id"].startswith(ACTIVATION_OPERATION_REQUEST_PREFIX))
        # The failed revision left the shared store at its schema: the board kept working throughout.
        self.assertEqual(self.version(self.board_config()), [HEAD])

    def test_a_tick_interrupted_before_the_block_replays_to_one_reason_and_one_operation(self) -> None:
        record, records, payload = self.arrange()
        with (
            mock.patch.object(release_lifecycle, "block_merge_path", side_effect=RuntimeError("tick died")),
            self.assertRaisesRegex(RuntimeError, "tick died"),
        ):
            self.release_card(record, records, payload)
        self.assertEqual(len(self.operations()), 1, "the operation is created before anything can be lost")
        self.assertNotEqual(self.reader.show(CARD_REF)["state"], "blocked")

        self.release_card(record, records, payload)

        card = self.reader.show(CARD_REF)
        self.assertEqual(card["state"], "blocked")
        self.assertEqual(len([c for c in card["comments"] if release_activation.HEADING in c["body"]]), 1)
        self.assertEqual(len(self.operations()), 1)

    def test_an_entrypoint_moved_target_lands_stays_inactive_and_names_the_installation_operation_guidance(self) -> None:
        record, records, payload = self.arrange(moved=True)
        with (
            mock.patch.object(release_migrations, "prepare", side_effect=AssertionError("schema touched")),
            mock.patch.object(release_lifecycle, "block_merge_path", side_effect=RuntimeError("tick died")),
            self.assertRaisesRegex(RuntimeError, "tick died"),
        ):
            self.release_card(record, records, payload)
        self.assertEqual(len(self.operations()), 1)

        # The replay settles the same obligation: one reason, one operation, no second activation.
        with mock.patch.object(self.host, "complete_green", side_effect=AssertionError("activation rerun")):
            outcome = self.release_card(record, records, payload)

        self.assertEqual((outcome["status"], outcome["reason"]), ("blocked", "production activation refused"))
        refused = outcome["activation_refused"]
        self.assertEqual((refused["code"], refused["reason"], refused["revision"]),
                         ("entrypoint_moved", "entrypoint_moved", None))
        self.assertEqual((refused["target"], refused["old"]), (self.target, self.old))
        # The merge is kept on the remote; the checkout, its tree and the board schema are untouched.
        self.assertEqual(git(self.workspace, "ls-remote", "origin", "main").split()[0], self.target)
        self.assertEqual(git(self.production, "rev-parse", "HEAD"), self.old)
        self.assertEqual(git(self.production, "status", "--porcelain"), "")
        self.assertTrue((self.production / PREFLIGHT).is_file())
        self.assertEqual(self.version(self.board_config()), [HEAD])
        card = self.reader.show(CARD_REF)
        self.assertEqual(card["state"], "blocked")
        self.assertEqual(len([c for c in card["comments"] if release_activation.HEADING in c["body"]]), 1)
        [blocked] = [e for e in self.writer.audit.events(CARD_REF)
                     if (e.get("transition") or {}).get("target") == "blocked"]
        for needle in ("entrypoint_moved", "The merge landed on main", "deliberately stays on the old commit",
                       "docs/OPERATIONS.md §Entrypoint activation", "not by retrying the release or running `upgrade`"):
            self.assertIn(needle, str(blocked))
        [operation] = self.operations()
        self.assertIn(operation["ref"], str(blocked))
        self.assertEqual((operation["sprint"], touches_production(operation)), (SPRINT, "ummanu"))
        for needle in ("docs/OPERATIONS.md §Entrypoint activation", "The merge landed on `main`", "Do not retry the release",
                       self.old, self.target):
            self.assertIn(needle, operation["description"])
        self.assertNotIn(CARD_REF, self.runtime.production_state.load()["records"])

        self.runtime.production_tick()
        self.assertEqual(len(self.operations()), 1)
        self.assertEqual(git(self.production, "rev-parse", "HEAD"), self.old)

    def test_the_dispatcher_creates_no_other_operation(self) -> None:
        self.start_dispatcher()
        self.instance(self.board_config(), self.data_dir)
        base = {"role": "dispatcher", "actor": "ummanu-pilot", "project": "ummanu", "task_type": "operation",
                "title": "T", "sprint": SPRINT}
        for extra, code in (
            ({"touches_production": "ummanu"}, "role_forbidden"),
            ({"touches_production": "ummanu", "request_id": "dispatcher-anything-1"}, "role_forbidden"),
            ({"touches_production": "none", "request_id": ACTIVATION_OPERATION_REQUEST_PREFIX + "x"}, "validation"),
        ):
            with self.subTest(extra=extra), self.assertRaises(TaskError) as raised:
                self.writer.create(**base, **extra)
            self.assertEqual(raised.exception.code, code)
        with self.assertRaises(TaskError) as observer:
            self.writer.create(
                **{**base, "role": "observer", "actor": "observer"},
                touches_production="ummanu",
                request_id=ACTIVATION_OPERATION_REQUEST_PREFIX + "y",
                origin={"session": "s", "request": "r"},
            )
        self.assertEqual(observer.exception.code, "validation")
        self.assertEqual(self.operations(), [])


if __name__ == "__main__":
    unittest.main()
