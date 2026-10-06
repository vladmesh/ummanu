"""`ummanu recover` from an exporter snapshot into a real PostgreSQL board store.

The source installation writes its board -- every record kind production holds: a Product, an Issue
whose priority comment claims its request, a closed sprint and an open one with sprint comments, task
cards with comments, a closed card on a retired project id, and the requests and events all of those
leave -- into one database of a throwaway `postgres:16`, and a real `SnapshotExporter` window cuts it into a bare repository that is pushed to a local bare remote. The
recovery target is a second, empty database. The clean-host sequence then runs from its first step
(docs/RECOVERY.md, "Fresh install and recovery"). `bootstrap` runs for real with the host edges and
Compose provisioning stood in for, as in `tests/test_fresh_postgres_install.py`: its clone step lays
the snapshot out, its provisioning writes `board-store.env` for the target database (bootstrap's
provisioning, never the snapshot, is where that host-local file comes from) and the migration runs
against it. Recovery then runs through `install()` for real: the reused live root, the secret store
step, the checkpoint, the board and sprint import with parity, the memory reindex (only the
embedding model is stood in for) and the head registry regeneration. Project checkouts, CODEX_HOME
and the host steps other than the head registry are host provisioning and stay out.

Each kind is compared with its source after recovery. The ummanu-45 drill stopped on the issue
comment: its `[request-id:...]` stamp claims an exported request (`issue_comment_claims_its_request`),
which the import wrote only after every comment, and no seed here held one.
"""

from __future__ import annotations

import getpass
import json
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.fakes.installation import PRODUCT_ROOT
from tests.fakes.snapshot_remote import HEAD, exporter_remote, git
from tests.sql_backend_fixtures import PostgresBoard, insert_card_row
from ummanu import bootstrap as bootstrap_module, installation, upgrade
from ummanu.board import store
from ummanu.board.sql_audit import SqlTaskAudit
from ummanu.board.sql_cards import SqlCardClient
from ummanu.board.store import BoardStoreConfig
from ummanu.checkpoint import SNAPSHOT_BASE_REF, SNAPSHOT_REF, SnapshotExporter, tick_checkpoint_writer
from ummanu.data import init_layout
from ummanu.head_registry import installed_heads, installed_pair
from ummanu.memory_journal import export_memory_snapshot, verify_memory_journal
from ummanu.product_issues import ProductIssueStore
from ummanu.restore import DEFAULT_MEMORY_DIM, restore_state
from ummanu.sprint_observer import none_choice
from ummanu.sprints import SprintReader, SprintWriter, sprint_client
from ummanu.tasks import TaskError, TaskReader, TaskWriter

#: Production's `personal_site-198` (`cards/0001/00001509.json`): closed in Done, in the lane and on
#: the project id `personal_site` that the registry has since retired for `personal-site`, with the
#: empty `model` its extension bag carries. It stopped the ummanu-41 drill's board import.
RETIRED_CARD = "personal_site-198"
RETIRED_METADATA = {
    "claim": "personal_site-198-1782988235",
    "head": "claude-sonnet",
    "task_type": "code",
}
CARDS = ("ummanu-1", "ummanu-2", RETIRED_CARD)
SPRINTS = ("sprint:snapshot", "sprint:snapshot-open")


def _write_store_file(instance: Path, config: BoardStoreConfig) -> None:
    """`board-store.env` for one database, private and outside the export, as `provision` leaves it."""
    store.ensure_ignored(instance)
    path = store.store_path(instance)
    path.write_text(
        "".join(f"{key}={value}\n" for key, value in config.as_environ().items()), encoding="utf-8"
    )
    path.chmod(0o600)


def _exported_history(data_dir: Path) -> list[dict[str, object]]:
    """The committed requests the recovered export carries, as `restore._restore_board_history` reads them."""
    board = data_dir / "board"
    if (board / "audit.json").is_file():
        return json.loads((board / "audit.json").read_text(encoding="utf-8"))["events"]
    lines = (board / "audit.ndjson").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


class _Embedder:
    """A deterministic stand-in for the fastembed model: the index is real, the vectors are not."""

    def embed_many(self, texts: list[str]):
        import numpy as np

        vectors = []
        for text in texts:
            raw = np.arange(1, DEFAULT_MEMORY_DIM + 1, dtype=np.float32) * (1 + len(text) % 7)
            vectors.append(raw / np.linalg.norm(raw))
        return vectors

    def __call__(self, text: str):
        return self.embed_many([text])[0]


class SnapshotRecoveryPostgresTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="snapshot-recovery-postgres-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        board = PostgresBoard.shared()
        self.source_config = board.fresh_database()
        self.target_config = board.fresh_database()
        self.fixture = exporter_remote(self.root, cut=False)
        # The registry knows the current id only.
        (self.fixture.source / "projects" / "personal-site.yaml").write_text(
            (self.fixture.source / "projects" / "ummanu.yaml")
            .read_text(encoding="utf-8")
            .replace("id: ummanu\n", "id: personal-site\n"),
            encoding="utf-8",
        )
        _write_store_file(self.fixture.source, self.source_config)
        init_layout(self.fixture.source_data)
        self._seed_source()
        state_dir = self.root / "source-pipeline-state"
        state_dir.mkdir()
        self.tip = self.fixture.cut(stand_in=False, state_dir=state_dir)

    def _seed_source(self) -> None:
        """Every record kind production holds, each by the writer that makes it there.

        Two task cards with comments; a Product and one Issue whose priority change leaves the
        stamped comment that claims its request; a sprint with a comment, closed, and an open one
        with a comment after it; and production's closed card on a retired project id, as its store
        holds it. Every writer leaves its requests and events behind.

        A sprint cannot exist without an owning Product, an open Issue of it and a reserved project
        (`SprintWriter._check_ownership`), so those are made the way the PO makes them, through
        `ProductIssueStore`. The cards come first and stay outside the sprints. No writer creates a
        card on an id the registry no longer has, nor an empty bag value, so that row is inserted
        the way the PostgreSQL suites seed a row (`insert_card_row`) and given its metadata through
        the client.
        """
        source, data_dir = self.fixture.source, self.fixture.source_data
        client = SqlCardClient(self.source_config.for_role("owner"), source)
        self.addCleanup(client.close)
        writer = TaskWriter(client, data_dir=data_dir)
        for number in (1, 2):
            writer.create(
                role="po",
                actor="test",
                project="ummanu",
                task_type="code",
                title=f"Recovered card {number}",
                target="ready",
                reference=f"ummanu-{number}",
                request_id=f"create-snapshot-card-{number}",
            )
            for index in (1, 2):
                writer.comment(
                    role="po",
                    actor="test",
                    reference=f"ummanu-{number}",
                    body=f"card {number} comment {index}",
                    request_id=f"comment-snapshot-card-{number}-{index}",
                )
        moved = datetime(2026, 8, 4, 9, 13, 38, tzinfo=UTC)
        with client.transaction():
            key = insert_card_row(
                client,
                key=360,
                reference=RETIRED_CARD,
                state="done",
                project="personal_site",
                title="Docker hygiene",
                description="a closed card on a retired project id",
                closed=True,
                position=1,
                created=moved,
                updated=moved,
                moved=moved,
                lane="personal_site",
                bag={"model": ""},
            )
            client._lanes = None
            client.call("saveTaskMetadata", task_id=key, values=RETIRED_METADATA)
        products = ProductIssueStore(client, data_dir=data_dir, instance=source)
        products.create_product(
            product_id="ummanu",
            projects=["ummanu"],
            title="Ummanu",
            description="the recovered product",
            actor="test",
            request_id="create-snapshot-product",
        )
        issue = products.create_issue(
            product="ummanu",
            issue_kind="feature",
            priority="P1",
            title="Recover from the snapshot",
            description="the sprint's issue",
            actor="test",
            request_id="create-snapshot-issue",
        )["ref"]
        self.issue = issue
        # The Issue comment that claims its request: `[issue:priority]` stamped `[request-id:...]`.
        products.update_priority(
            reference=issue,
            priority="P0",
            reason="recovery is the release blocker",
            actor="test",
            request_id="raise-snapshot-issue",
        )
        sprints = sprint_client(source)
        self.addCleanup(sprints.close)
        sprint_writer = SprintWriter(sprints, data_dir=data_dir, instance=source)
        sprint = sprint_writer.create(
            role="po",
            actor="test",
            goal="recover from the snapshot",
            repositories=[str(self.root / "repository")],
            product="ummanu",
            issues=[issue],
            projects=["ummanu"],
            observer=none_choice(),
            reference="sprint:snapshot",
            request_id="create-snapshot-sprint",
        )["sprint"]["ref"]
        sprint_writer.comment(
            role="po",
            actor="test",
            reference=sprint,
            body="closed sprint comment",
            request_id="comment-snapshot-sprint",
        )
        sprint_writer.close(
            role="po",
            actor="test",
            reference=sprint,
            decisions={
                "issues": [{"ref": issue, "verdict": "open", "reason": "recovery stays supported"}],
                "cards": [],
            },
            reason="snapshot fixture closed",
            request_id="close-snapshot-sprint",
        )
        # Production holds an open sprint beside its closed ones; the installation admits one.
        open_sprint = sprint_writer.create(
            role="po",
            actor="test",
            goal="stay open across the recovery",
            repositories=[str(self.root / "repository-open")],
            product="ummanu",
            issues=[issue],
            projects=["ummanu"],
            observer=none_choice(),
            reference=SPRINTS[1],
            request_id="create-snapshot-open-sprint",
        )["sprint"]["ref"]
        sprint_writer.comment(
            role="po",
            actor="test",
            reference=open_sprint,
            body="open sprint comment",
            request_id="comment-snapshot-open-sprint",
        )
        self.source_kinds = self._kinds(source, data_dir, self.source_config)

    def _kinds(self, instance: Path, data_dir: Path, config: BoardStoreConfig) -> dict[str, object]:
        """What each record kind holds, read by its own reader, to compare source and target."""
        client = SqlCardClient(config.for_role("owner"), instance)
        self.addCleanup(client.close)
        products = ProductIssueStore(client, data_dir=data_dir, instance=instance)
        issue = products.show_issue(self.issue)
        history = issue.pop("history")
        sprints = sprint_client(instance)
        self.addCleanup(sprints.close)
        sprint_reader = SprintReader(sprints, data_dir=data_dir)
        tasks = TaskReader(client)
        return {
            "product": products.show_product("ummanu"),
            "issue": issue,
            "issue_comments": [comment["text"] for comment in history["comments"]],
            "issue_requests": {event["request_id"] for event in history["audit"]},
            "sprints": {
                reference: (
                    shown["status"],
                    shown["goal"],
                    [comment["body"] for comment in shown["comments"]],
                )
                for reference in SPRINTS
                for shown in (sprint_reader.show(reference),)
            },
            "cards": {
                reference: (
                    shown["title"],
                    shown["state"],
                    shown["closed"],
                    shown["project"],
                    [comment["body"] for comment in shown["comments"]],
                )
                for reference in CARDS
                for shown in (tasks.show(reference),)
            },
            "requests": {event["request_id"]: event for event in SqlTaskAudit(client).events()},
        }

    def _bootstrap(self) -> tuple[int, list[str]]:
        """The real `bootstrap` against the snapshot remote; returns its exit code and its output."""
        target = self.fixture.target

        def provision(instance: Path, *, allow_create: bool) -> None:
            self.assertTrue(allow_create)
            # The clone step brought no store credential: provisioning is where it comes from.
            self.assertFalse(store.store_path(instance).exists())
            _write_store_file(instance, self.target_config)

        printed = mock.Mock()
        args = SimpleNamespace(
            instance_dir=str(target),
            instance_remote=str(self.fixture.remote),
            installation_user=getpass.getuser(),
            dry_run=False,
        )
        with (
            mock.patch("ummanu.bootstrap.os.geteuid", return_value=0),
            mock.patch("ummanu.bootstrap._host_supported"),
            mock.patch("ummanu.bootstrap._ensure_installation_user"),
            mock.patch("ummanu.bootstrap._set_installation_owner"),
            mock.patch("ummanu.installation._set_installation_owner"),
            mock.patch("ummanu.bootstrap._install_platform"),
            mock.patch("ummanu.bootstrap.provision_board_store", side_effect=provision),
            # The database is a migrated copy, so the migration finds nothing owed. The role contract
            # of a store bootstrap creates is `tests/test_fresh_postgres_install.py`'s.
            mock.patch("ummanu.bootstrap.verify_board_store_roles"),
            mock.patch("builtins.print", printed),
        ):
            code = bootstrap_module.bootstrap(args)
        return code, [str(call.args[0]) for call in printed.call_args_list if call.args]

    def test_a_snapshot_remote_recovers_into_an_empty_store_at_parity(self) -> None:
        fixture = self.fixture
        target, data_dir = fixture.target, fixture.data_dir
        summary = json.loads(
            subprocess.run(
                ["git", "-C", str(fixture.remote), "cat-file", "blob", f"{self.tip}:state/board/export.json"],
                capture_output=True,
                check=True,
                text=True,
            ).stdout
        )
        # Two cards, the closed card on the retired id, the Product and its Issue.
        self.assertEqual(summary["card_count"], 5)
        self.assertEqual(summary["sprint_count"], 2)
        # The seed holds what the ummanu-45 drill stopped on: an Issue comment claiming a request.
        self.assertIn("[request-id:raise-snapshot-issue]", self.source_kinds["issue_comments"][-1])
        code, output = self._bootstrap()
        self.assertEqual(code, 0, output)
        self.assertFalse((target / ".git").exists())
        self.assertTrue((target / ".ummanu-bootstrap").is_file())
        self.assertEqual(store.store_path(target).stat().st_mode & 0o777, 0o600)

        def head_registry_only(context, steps=installation.STEPS):
            return upgrade.run_steps(
                context, steps=tuple(step for step in steps if step is upgrade.step_head_registry)
            )

        args = SimpleNamespace(
            instance_dir=str(target),
            instance_remote=str(fixture.remote),
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
        # A refused batch now names the store's refusal itself; every refusal is kept anyway, so a
        # failure message holds each one the store gave, not only the first.
        refused: list[str] = []
        real_batch = SqlCardClient.call_batch

        def recording_batch(client, calls):
            try:
                return real_batch(client, calls)
            except TaskError as exc:
                refused.append(exc.message)
                raise

        with ExitStack() as stack:
            for patch in (
                mock.patch.object(SqlCardClient, "call_batch", autospec=True, side_effect=recording_batch),
                mock.patch("ummanu.installation._ensure_installation_user"),
                mock.patch("ummanu.installation._set_installation_owner"),
                mock.patch("ummanu.installation.provision_project_checkouts", return_value=[]),
                mock.patch("ummanu.installation.provision_codex_home", return_value=0),
                mock.patch("ummanu.installation.run_steps", side_effect=head_registry_only),
                mock.patch("ummanu.installation.check_product_runtime"),
                # The fixture enables the web front; the host's Caddy is not this test's (ummanu-49 P4).
                mock.patch("ummanu.installation.caddy_installed", return_value=True),
                mock.patch("ummanu.memory_service.build_document_embedder", return_value=_Embedder()),
            ):
                stack.enter_context(patch)
            result = installation.install(args)

        steps = {step.name: (step.status, step.detail) for step in result.steps}
        self.assertEqual(result.status, "ok", f"{result.render()}\nrefused store batches: {refused}")
        # The live root bootstrap laid out is this tip's: recovery reuses it instead of cloning again.
        self.assertEqual(steps["instance-checkout"][0], "unchanged")
        self.assertIn(f"reused exporter snapshot {self.tip[:12]}", steps["instance-checkout"][1])
        self.assertEqual(steps["board"], ("changed", f"{summary['card_count']} card(s) at parity"))
        # Board and sprints arrived with the counts the tree's export.json declares.
        state = restore_state(data_dir)
        self.assertEqual(state["board_parity"], "complete")
        self.assertEqual(state["board_count"], summary["card_count"])
        self.assertEqual(state["sprint_parity"], "complete")
        self.assertEqual(state["sprint_count"], summary["sprint_count"])
        # The closed card on the retired id is back as the tree exported it: placement and project.
        exported = next(
            card
            for card in json.loads((data_dir / "board" / "cards.json").read_text(encoding="utf-8"))["cards"]
            if card["reference"] == RETIRED_CARD
        )
        self.assertEqual(
            (exported["column"], exported["swimlane"], exported["closed"], exported["metadata"]["model"]),
            ("Done", "personal_site", True, ""),
        )
        restored_client = SqlCardClient(self.target_config.for_role("owner"), target)
        self.addCleanup(restored_client.close)
        restored = TaskReader(restored_client).show(RETIRED_CARD)
        self.assertEqual(
            (restored["state"], restored["closed"], restored["project"]),
            ("done", True, "personal_site"),
        )
        self.assertEqual(restored["extensions"]["extra"]["swimlane"], exported["swimlane"])
        # Every record kind at parity with its source.
        restored_kinds = self._kinds(target, data_dir, self.target_config)
        source_kinds = self.source_kinds
        for kind in ("product", "issue", "issue_comments", "sprints", "cards"):
            with self.subTest(kind=kind):
                self.assertEqual(restored_kinds[kind], source_kinds[kind])
        self.assertEqual(restored_kinds["issue"]["priority"], "P0")
        self.assertEqual(
            {
                reference: status
                for reference, (status, _goal, _comments) in restored_kinds["sprints"].items()
            },
            {"sprint:snapshot": "closed", "sprint:snapshot-open": "open"},
        )
        self.assertTrue(all(comments for *_rest, comments in restored_kinds["sprints"].values()))
        for reference in ("ummanu-1", "ummanu-2"):
            self.assertEqual(
                restored_kinds["cards"][reference][-1],
                [f"[po]\ncard {reference[-1]} comment {index}" for index in (1, 2)],
            )
        # Audit and requests: every request the export carries is back with its record, the one the
        # Issue comment claims included, and the target adds only the restore's own.
        self.assertLessEqual(source_kinds["issue_requests"], restored_kinds["issue_requests"])
        self.assertIn("raise-snapshot-issue", restored_kinds["issue_requests"])
        exported = {event["request_id"]: event for event in _exported_history(data_dir)}
        self.assertLessEqual(set(source_kinds["requests"]), set(exported))
        restored_requests = restored_kinds["requests"]
        self.assertEqual({request_id: restored_requests.get(request_id) for request_id in exported}, exported)
        self.assertEqual(
            {request_id for request_id in restored_requests if request_id not in exported},
            {request_id for request_id in restored_requests if request_id.startswith("restore:")},
        )
        # The snapshot repository, its marker and the plain live root.
        repository = data_dir / "backup" / "instance.git"
        self.assertEqual(git(repository, "rev-parse", SNAPSHOT_REF), self.tip)
        self.assertEqual(git(repository, "cat-file", "blob", SNAPSHOT_BASE_REF), self.tip)
        for absent in (".git", "state/board", "state/runs", "snapshot-manifest.json"):
            self.assertFalse((target / absent).exists(), absent)
        # Memory: the index rebuilt from the recovered facts verifies against the canon.
        self.assertEqual(steps["memory"], ("changed", "rebuilt index for 1 fact(s)"))
        export_memory_snapshot(data_dir, target)
        verified = verify_memory_journal(data_dir, target)
        self.assertTrue(verified.ok, verified.findings)
        # Heads regenerated into the data directory; the first tick is the exporter's.
        self.assertEqual(installed_pair(target).snapshot.parent, data_dir / "heads")
        self.assertEqual(installed_heads(target)["role_defaults"]["new_card"], HEAD)
        writer = tick_checkpoint_writer(data_dir, target)
        self.assertIsInstance(writer, SnapshotExporter)
        self.assertEqual(writer.snapshot_repo, repository.resolve())


if __name__ == "__main__":
    unittest.main()
