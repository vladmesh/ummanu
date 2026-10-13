"""A fresh installation's board is the PostgreSQL store bootstrap leaves, against a real `postgres:16`.

secretary-1666: bootstrap starts, waits for and shapes no other board, and install recovery asks
for none before it restores a checkpoint. The board is the store bootstrap provisions, migrates and
verifies. These tests prove that store is
enough on its own: a freshly migrated, empty store takes a card and lists it back, and a real install
recovery on a real bootstrap checkout restores a checkpoint's cards into it at parity.

`provision` itself is the one step not exercised: it drives Docker Compose against the installed
`/opt/ummanu` definition on port 5432. A throwaway container stands in for what it starts, and
`board-store.env` is written for that container the way `provision` writes it for its own.

There is no skip, for the reason `tests/sql_backend_fixtures.py` gives: a store proof that passes
because it never reached a database is worth less than none.
"""

from __future__ import annotations

import getpass
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import psycopg

from tests.fakes.installation import CARD as CHECKPOINT_CARD, PRODUCT_ROOT, _checkpoint, _git
from tests.retired_board import STALE_FILE
from tests.sql_backend_fixtures import PostgresBoard
from ummanu import bootstrap as bootstrap_module
from ummanu.board import store
from ummanu.board.backend import CARD, board_client
from ummanu.board.store import BoardStoreConfig
from ummanu.data import init_layout
from ummanu.installation import commands as installation_commands, support as installation_support
from ummanu.tasks import TaskReader, TaskWriter

STORE_DATABASE = "ummanu"


def _write_store_file(instance: Path, config: BoardStoreConfig) -> None:
    """`board-store.env` for the throwaway server, private and ignored, as `provision` leaves it."""
    store.ensure_ignored(instance)
    path = store.store_path(instance)
    path.write_text(
        "".join(f"{key}={value}\n" for key, value in config.as_environ().items()), encoding="utf-8"
    )
    path.chmod(0o600)


class FreshStoreCase(unittest.TestCase):
    """One throwaway server per class: the first migration creates the cluster-wide roles."""

    board: PostgresBoard

    @classmethod
    def setUpClass(cls) -> None:
        cls.board = PostgresBoard()
        cls.addClassCleanup(cls.board.stop)
        with psycopg.connect(cls.board.config("postgres").for_role("owner").conninfo(), autocommit=True) as c:
            c.execute(f"CREATE DATABASE {STORE_DATABASE} OWNER {cls.board.config('postgres').owner_user}")
        cls.config = cls.board.config(STORE_DATABASE)

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="fresh-postgres-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def _bootstrap(self, remote: str, target: Path) -> int:
        """The real `bootstrap`, with only the host edges and Compose provisioning stood in for."""

        def provision(instance: Path, *, allow_create: bool) -> None:
            self.assertTrue(allow_create)
            _write_store_file(instance, self.config)

        args = SimpleNamespace(
            instance_dir=str(target),
            instance_remote=remote,
            installation_user=getpass.getuser(),
            dry_run=False,
        )
        with (
            mock.patch("ummanu.bootstrap.os.geteuid", return_value=0),
            mock.patch("ummanu.bootstrap._host_supported"),
            mock.patch("ummanu.bootstrap._ensure_installation_user"),
            mock.patch("ummanu.bootstrap._set_installation_owner"),
            mock.patch("ummanu.bootstrap._install_platform"),
            mock.patch("ummanu.bootstrap.provision_board_store", side_effect=provision),
            mock.patch("builtins.print"),
        ):
            return bootstrap_module.bootstrap(args)


class EmptyStoreTests(FreshStoreCase):
    def test_a_freshly_migrated_empty_store_accepts_a_card_create_and_a_card_list(self) -> None:
        instance = self.root / "instance"
        instance.mkdir()
        _git(instance, "init")
        _write_store_file(instance, self.config)
        # The two store steps bootstrap runs after `provision`, on a database with no schema at all.
        bootstrap_module.migrate_instance(instance)
        bootstrap_module.verify_board_store_roles(instance)
        data = self.root / "data"
        init_layout(data)

        client = board_client(instance, serves=(CARD,))
        self.addCleanup(client.close)
        self.assertEqual(TaskReader(client).list(), [])
        created = TaskWriter(client, data_dir=data).create(
            role="po",
            actor="po",
            project="ummanu",
            task_type="code",
            title="the first card on a fresh board",
        )["task"]
        listed = TaskReader(client).list()

        self.assertEqual(created["ref"], "ummanu-1")
        self.assertEqual([(card["ref"], card["project"]) for card in listed], [("ummanu-1", "ummanu")])
        self.assertEqual(listed[0]["audit"]["backend"]["kind"], "postgres")


class BootstrapThenRecoveryTests(FreshStoreCase):
    def test_install_recovery_restores_the_checkpoint_into_the_store_bootstrap_left(self) -> None:
        source = self.root / "source"
        remote = self.root / "instance.git"
        target = self.root / "instance"
        data = self.root / "data"
        source.mkdir()
        _checkpoint(source, data)
        # The registry the restored card's project has to be in, shaped as in test_postgres_recovery.
        repository = self.root / "repository"
        repository.mkdir()
        (source / "projects").mkdir()
        (source / "projects" / "ummanu.yaml").write_text(
            f"id: ummanu\nrepo: {repository}\nenabled: false\nadapter: ummanu\ndefault_branch: main\n",
            encoding="utf-8",
        )
        (source / "adapters").mkdir()
        (source / "adapters" / "ummanu.yaml").write_text(
            "setup:\n  commands: ['true']\nsmoke:\n  command: 'true'\n"
            "validation:\n  ci: local\n  command: 'true'\n"
            "artifact_policy:\n  write_project_files: false\n",
            encoding="utf-8",
        )
        _git(source, "init")
        _git(source, "config", "user.name", "Test")
        _git(source, "config", "user.email", "test@example.invalid")
        _git(source, "add", ".")
        _git(source, "commit", "-m", "checkpoint")
        subprocess.run(
            ["git", "clone", "--bare", str(source), str(remote)], check=True, capture_output=True, text=True
        )
        text = (source / "instance.yaml").read_text(encoding="utf-8")
        (source / "instance.yaml").write_text(text.replace("placeholder", str(remote)), encoding="utf-8")
        _git(source, "add", "instance.yaml")
        _git(source, "commit", "-m", "remote identity")
        _git(source, "push", str(remote), "HEAD:master")

        self.assertEqual(self._bootstrap(str(remote), target), 0)
        # Nothing selects the board, so bootstrap records nothing in runtime.env.
        self.assertFalse((target / "runtime.env").exists())
        self.assertFalse((target / STALE_FILE).exists())

        real_run = installation_support._run

        def run(argv: list[str], **kwargs: object) -> object:
            # Recovery needs no Orca (A20 step 9); every command runs for real.
            if "orca" in argv[:1] or argv[:5] == ["runuser", "--user", getpass.getuser(), "--", "orca"]:
                raise AssertionError(f"recovery ran Orca: {argv}")
            return real_run(argv, **kwargs)

        args = SimpleNamespace(
            instance_dir=str(target),
            instance_remote=str(remote),
            installation_user=getpass.getuser(),
            recover=True,
            adopt=False,
            dry_run=False,
            runtime_env=None,
            product_root=str(PRODUCT_ROOT),
            bootstrap_credential_file=None,
            bootstrap_credential_stdin=False,
            recovery_phrase_file=None,
            recovery_phrase_stdin=False,
            host_fixture=None,
        )
        with ExitStack() as stack:
            for patch in (
                mock.patch("ummanu.installation.commands._ensure_installation_user"),
                mock.patch("ummanu.installation.clone._set_installation_owner"),
                mock.patch("ummanu.installation.commands._set_installation_owner"),
                mock.patch("ummanu.installation.materialize._set_installation_owner"),
                mock.patch("ummanu.installation.support._set_installation_owner"),
                mock.patch("ummanu.installation.clone._run", side_effect=run),
                # Project checkouts and CODEX_HOME are host provisioning, not the board.
                mock.patch("ummanu.installation.commands.provision_project_checkouts", return_value=[]),
                mock.patch("ummanu.installation.commands.provision_codex_home", return_value=0),
                mock.patch("ummanu.installation.commands.rebuild_memory_index", return_value=1),
                # The fixture enables the web front; the host's Caddy is not this test's (ummanu-49 P4).
                mock.patch("ummanu.installation.materialize.caddy_installed", return_value=True),
                mock.patch(
                    "ummanu.installation.commands.materialize_host",
                    return_value=SimpleNamespace(steps=[SimpleNamespace(status="changed")]),
                ),
                mock.patch("ummanu.installation.commands.materialize_pipeline_state", return_value=0),
                mock.patch("ummanu.installation.commands.restore_findings", return_value=[]),
            ):
                stack.enter_context(patch)
            result = installation_commands.install(args)

        steps = {step.name: (step.status, step.detail) for step in result.steps}
        self.assertEqual(result.status, "ok", result.steps)
        self.assertFalse([name for name in steps if "transport" in name], steps)
        self.assertEqual(steps["prerequisites"], ("unchanged", "PostgreSQL is reachable"))
        self.assertEqual(steps["board"], ("changed", "1 card(s) at parity"))
        self.assertFalse((target / STALE_FILE).exists())
        self.assertFalse((target / "runtime.env").exists())

        # Read back through the store the units will use, not through the recovery's own client.
        client = board_client(target, serves=(CARD,))
        self.addCleanup(client.close)
        cards = TaskReader(client).list()
        self.assertEqual(
            [(card["ref"], card["title"], card["project"]) for card in cards],
            [(CHECKPOINT_CARD["reference"], CHECKPOINT_CARD["title"], "ummanu")],
        )


if __name__ == "__main__":
    unittest.main()
