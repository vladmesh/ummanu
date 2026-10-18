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
import hashlib
import io
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.fakes.installation import PRODUCT_ROOT
from tests.fakes.snapshot_remote import HEAD, REVISION, exporter_producers, exporter_remote, git
from tests.owner_checkpoint_fixtures import bell_rows
from tests.po_checkpoint_fixtures import po_records, seed_po, write_notes, write_queue
from tests.sql_backend_fixtures import PostgresBoard, insert_card_row
from tests.support.snapshot_recovery import recover_patches
from ummanu import bootstrap as bootstrap_module
from ummanu.board import checkpoint_layout, store
from ummanu.board.checkpoint_layout import (
    FLAT,
    LOGICAL_FILES,
    open_checkpoint_board,
    part_name,
    segment_chunks,
)
from ummanu.board.close_checkpoint import (
    LEGACY_CLOSE_COVERAGE,
    read_checkpoint_close_facts,
    require_close_set_parity,
)
from ummanu.board.migrate import head_revision
from ummanu.board.owner_checkpoint import (
    LEGACY_OWNER_COVERAGE,
    OWNER_FILE,
    read_checkpoint_owner_events,
    require_owner_parity,
)
from ummanu.board.owner_events import OwnerEventStore, ReadRefused
from ummanu.board.po_attachments_checkpoint import (
    ATTACHMENTS_CHECKPOINT_FILE,
    LEGACY_ATTACHMENTS_COVERAGE,
    read_checkpoint_attachments,
)
from ummanu.board.po_checkpoint import RESTORED_PO_COVERAGE, export_po_records, read_checkpoint_po
from ummanu.board.po_notes_checkpoint import (
    LEGACY_NOTES_COVERAGE,
    NOTES_CHECKPOINT_FILE,
    RESTORED_NOTES_COVERAGE,
    NotesCapture,
    read_checkpoint_notes,
)
from ummanu.board.po_queue_checkpoint import (
    LEGACY_QUEUE_COVERAGE,
    QUEUE_CHECKPOINT_FILE,
    queue_payload,
    read_checkpoint_queue,
)
from ummanu.board.sql_audit import SqlTaskAudit
from ummanu.board.sql_cards import SqlCardClient
from ummanu.board.store import BoardStoreConfig
from ummanu.checkpoint import (
    SNAPSHOT_BASE_REF,
    SNAPSHOT_REF,
    SnapshotExporter,
    _write_analytics_manifest,
    tick_checkpoint_writer,
    verify_analytics_checkpoint,
)
from ummanu.cli import main as cli_main
from ummanu.data import export_board, init_layout
from ummanu.head_registry import installed_heads, installed_pair
from ummanu.installation import commands as installation_commands
from ummanu.installation.materialize import materialize_checkpoint
from ummanu.memory import client_config
from ummanu.memory_journal import export_memory_snapshot, verify_memory_journal
from ummanu.po import workspace as po_workspace
from ummanu.po.attachments import attachments_dir, store_image
from ummanu.po.queue import PoQueue, queue_dir
from ummanu.po.recovery_admission import PoRecoveryAdmission
from ummanu.po.runner import PoRunner
from ummanu.po.store import COMPLETED, INTERRUPTED, PoStore
from ummanu.po.workspace import NOTES_FILE, workspace_dir
from ummanu.product_issues import ProductIssueStore
from ummanu.restore import import_normalized_board, restore_state
from ummanu.secret_recover import SecretRecovery
from ummanu.sprint_observer import none_choice
from ummanu.sprints import SprintReader, SprintWriter, sprint_client
from ummanu.tasks import TaskError, TaskReader, TaskWriter
from ummanu.webproto.owner_events import OwnerEventLayer

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
#: The PO's own notes on the source host: a decision the SQL feed does not hold, in CRLF and UTF-8.
SOURCE_IMAGES = (
    ("open", b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4),
    ("open", b"\xff\xd8\xff\xe0" + bytes(range(255, -1, -1)) * 3),
    ("closed", b"RIFF\x20\x00\x00\x00WEBPVP8 " + b"\xab\xcd\xef" * 200),
)
SOURCE_NOTES = "# PO notes\r\nThe owner settled on option B in a call; no PO turn records it. Решено.\n".encode()


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


class _FinishedProcess:
    pid = 999999999

    def wait(self, timeout=None):
        return 0


class _SnapshotRecoveryCase(unittest.TestCase):
    """A seeded source installation cut into a snapshot remote, and an empty target store."""

    def _temporary_base(self) -> str | None:
        """Where the fixture tree lives: by default the suite's private temporary root."""
        return None

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="snapshot-recovery-postgres-", dir=self._temporary_base())
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
        self.source_close_result = sprint_writer.close(
            role="po",
            actor="test",
            reference=sprint,
            decisions={
                "issues": [{"ref": issue, "verdict": "open", "reason": "recovery stays supported"}],
                "cards": [],
            },
            reason="snapshot fixture closed",
            closeout="The issue stays open; recovery preserves the recorded decision and document.",
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
        # The PO history production holds: closed, empty and open sessions, a captured running turn.
        po_client = SqlCardClient(self.source_config.for_role("app"), source)
        self.addCleanup(po_client.close)
        seed_po(po_client, po_records())
        workspace_dir(data_dir).mkdir()
        (workspace_dir(data_dir) / NOTES_FILE).write_bytes(SOURCE_NOTES)
        # The PO queue as its own writer leaves it: an input whose claim committed before its
        # removal, unclaimed inputs from the dispatcher and the service, and a refused one.
        queue = PoQueue(data_dir)
        queue.put(session_id="open", text=po_records()["po_feed"][0]["text"], request_id="send", source="web")
        queue.put(session_id="open", text="operate demo-1", request_id="queued-card", source="dispatcher",
                  card={"card_ref": "ummanu-1", "kind": "operation", "input": "card"},
                  note="Production rights: none.", metadata={"source": "dispatcher", "summary": "ummanu-1 (operation)",
                                                             "sprint_ref": SPRINTS[1], "comment_position": 1})
        queue.put(session_id="empty", text="Sprint context", request_id="queued-seed", source="po-service",
                  metadata={"source": "po-service", "summary": "Sprint session context", "seed_sources": []})
        closed = queue.put(session_id="closed", text="for a closed session", request_id="refused-input",
                           source="web", metadata={"attachments": ["shot.png"]})
        queue.refuse(closed, "PO session closed is closed; open a new session to continue")
        self.source_queue = queue.capture()
        # Images of an open and a closed session as the upload publishes them.
        self.source_images = {(session, store_image(data_dir, session, image).name): image
                              for session, image in SOURCE_IMAGES}
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

    def _bootstrap(self, *, target: Path | None = None, remote: Path | None = None) -> tuple[int, list[str]]:
        """The real `bootstrap` against the snapshot remote (or `remote`); returns its exit code and output."""
        target = self.fixture.target if target is None else target

        def provision(instance: Path, *, allow_create: bool) -> None:
            self.assertTrue(allow_create)
            # The clone step brought no store credential: provisioning is where it comes from.
            self.assertFalse(store.store_path(instance).exists())
            _write_store_file(instance, self.target_config)

        printed = mock.Mock()
        args = SimpleNamespace(
            instance_dir=str(target),
            instance_remote=str(self.fixture.remote if remote is None else remote),
            installation_user=getpass.getuser(),
            dry_run=False,
        )
        with (
            mock.patch("ummanu.bootstrap.os.geteuid", return_value=0),
            mock.patch("ummanu.bootstrap._host_supported"),
            mock.patch("ummanu.bootstrap._ensure_installation_user"),
            mock.patch("ummanu.bootstrap._set_installation_owner"),
            mock.patch("ummanu.installation.clone._set_installation_owner"),
            mock.patch("ummanu.bootstrap._install_platform"),
            mock.patch("ummanu.bootstrap.provision_board_store", side_effect=provision),
            # The database is a migrated copy, so the migration finds nothing owed. The role contract
            # of a store bootstrap creates is `tests/test_fresh_postgres_install.py`'s.
            mock.patch("ummanu.bootstrap.verify_board_store_roles"),
            mock.patch("builtins.print", printed),
        ):
            code = bootstrap_module.bootstrap(args)
        return code, [str(call.args[0]) for call in printed.call_args_list if call.args]

    def _args(self, *, target: Path | None = None, remote: Path | None = None) -> SimpleNamespace:
        return SimpleNamespace(
            instance_dir=str(self.fixture.target if target is None else target),
            instance_remote=str(self.fixture.remote if remote is None else remote),
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

    def _recover(self, *extra, args: SimpleNamespace | None = None) -> tuple[object, list[str]]:
        """One unprivileged `recover` with host provisioning stood in for, and the store refusals it met."""
        # A refused batch names the store's refusal itself; every refusal is kept anyway, so a
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
                *recover_patches(owners=True),
                *extra,
            ):
                stack.enter_context(patch)
            return installation_commands.install(self._args() if args is None else args), refused

    def _target_po(self) -> tuple[PoStore, SqlCardClient]:
        po = PoStore(self.target_config.for_role("app"))
        self.addCleanup(po.close)
        client = SqlCardClient(self.target_config.for_role("app"), self.fixture.target)
        self.addCleanup(client.close)
        return po, client

    @staticmethod
    def _readback(client: SqlCardClient) -> dict[str, list[dict[str, object]]]:
        with client.transaction():
            return export_po_records(client)

    def _materialize_po_workspace(self, data_dir: Path) -> tuple[str, ...]:
        """The install step's workspace materialize over the recovered data dir, from a minimal product."""
        product = self.root / "po-product"
        bridge = client_config.bridge_executable(product)
        bridge.parent.mkdir(parents=True, exist_ok=True)
        bridge.write_text("#!/bin/sh\n", encoding="utf-8")
        source = po_workspace.agents_source(product)
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("# PO workspace instructions\n", encoding="utf-8")
        return po_workspace.materialize(product, data_dir).changed


    def _assert_inert_queue(self, steps: dict[str, tuple[str, str]], data_dir: Path, po: PoStore) -> None:
        """The source queue arrives whole as board data; nothing queued, claimed or run on the target."""
        captured = read_checkpoint_queue(data_dir / "board")
        self.assertEqual(captured.capture, self.source_queue)
        self.assertEqual(captured.counts(), (3, 1))
        self.assertEqual(steps["po-queue"], ("unchanged", captured.describe()))
        state = restore_state(data_dir)
        self.assertEqual((state["po_queue_coverage"], state["po_queue_pending"], state["po_queue_refused"]),
                         ("captured", 3, 1))
        self.assertFalse(queue_dir(data_dir).exists())
        for request_id in ("queued-card", "queued-seed", "refused-input"):
            self.assertIsNone(po.request(request_id))
        self.assertEqual(PoQueue(self.fixture.source_data).capture(), self.source_queue)


    def _assert_inert_attachments(self, steps: dict[str, tuple[str, str]], data_dir: Path) -> None:
        """The source images arrive as exact board bytes; no session attachments directory is written."""
        board = data_dir / "board"
        lines = (board / ATTACHMENTS_CHECKPOINT_FILE).read_text(encoding="utf-8").splitlines()
        self.assertEqual({(record["session_id"], record["name"]): bytes.fromhex(record["data"])
                          for record in map(json.loads, lines)}, self.source_images)
        captured = read_checkpoint_attachments(board)
        self.assertEqual(steps["po-attachments"], ("unchanged", captured.describe()))
        state = restore_state(data_dir)
        self.assertEqual(
            (state["po_attachments_coverage"], state["po_attachments_files"], state["po_attachments_bytes"]),
            ("captured", 3, sum(len(image) for image in self.source_images.values())),
        )
        for session in ("closed", "empty", "open"):
            self.assertFalse(attachments_dir(data_dir, session).exists())
        for (session, name), image in self.source_images.items():
            self.assertEqual((attachments_dir(self.fixture.source_data, session) / name).read_bytes(), image)


class SnapshotRecoveryPostgresTests(_SnapshotRecoveryCase):
    def test_legacy_recovery_and_retry_keep_coverage_descriptive_through_the_status_gate(self):
        board = self.fixture.source_data / "board"
        summary_path = board / "export.json"
        summary = json.loads(summary_path.read_text())
        summary.pop("owner_events")
        summary.pop("sprint_close")
        summary.pop("po_notes")
        (board / NOTES_CHECKPOINT_FILE).unlink()
        summary.pop("po_queue")
        (board / QUEUE_CHECKPOINT_FILE).unlink()
        summary.pop("po_attachments")
        (board / ATTACHMENTS_CHECKPOINT_FILE).unlink()
        for name in ("sprints.json", "sprints.ndjson"):
            path = board / name
            if name.endswith(".ndjson"):
                rows = [json.loads(line) for line in path.read_text().splitlines()]
                for row in rows:
                    row.pop("close_facts")
                path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            else:
                payload = json.loads(path.read_text())
                for row in payload["sprints"]:
                    row.pop("close_facts")
                path.write_text(json.dumps(payload))
        summary_path.write_text(json.dumps(summary))
        (board / OWNER_FILE).unlink()
        self.assertIsNone(read_checkpoint_owner_events(board))
        self.assertIsNone(read_checkpoint_notes(board))
        self.assertIsNone(read_checkpoint_queue(board))
        canon = self.fixture.source / "heads" / "heads.toml"
        canon.write_text(
            canon.read_text().replace("fallback = []", 'fallback = ["legacy-fallback"]')
            + '\n[resources.legacy-fallback]\naccount = "legacy-fallback"\n'
            + '[profiles.legacy-fallback]\nresource = "legacy-fallback"\nadapter = "claude"\n'
            + f'fallback = ["{HEAD}"]\n'
        )
        self.tip = self.fixture.cut()
        code, output = self._bootstrap()
        self.assertEqual(code, 0, output)
        previous = None
        target, data_dir = self.fixture.target, self.fixture.data_dir
        client = SqlCardClient(self.target_config.for_role("app"), target)
        self.addCleanup(client.close)
        for attempt in ("first", "retry"):
            with self.subTest(attempt=attempt):
                result, refused = self._recover()
                self.assertEqual(result.status, "ok", f"{result.render()} {refused}")
                machine = asdict(result)
                coverage = next(step for step in machine["steps"] if step["name"] == "owner-events")
                self.assertEqual(
                    (coverage["status"], coverage["detail"]), ("unchanged", LEGACY_OWNER_COVERAGE)
                )
                self.assertEqual(
                    next(step for step in machine["steps"] if step["name"] == "status")["status"], "unchanged"
                )
                state = restore_state(data_dir)
                self.assertEqual(state["board_parity"], "complete")
                self.assertEqual(state["owner_events_coverage"], "unavailable")
                self.assertEqual(state["owner_events_parity"], "unavailable")
                self.assertEqual(state["owner_events_diagnostic"], LEGACY_OWNER_COVERAGE)
                self.assertEqual(
                    (state["po_notes_coverage"], state["po_notes_parity"], state["po_notes_diagnostic"]),
                    ("unavailable", "unavailable", LEGACY_NOTES_COVERAGE),
                )
                notes_step = next(step for step in machine["steps"] if step["name"] == "po-notes")
                self.assertEqual((notes_step["status"], notes_step["detail"]), ("unchanged", LEGACY_NOTES_COVERAGE))
                self.assertFalse((workspace_dir(data_dir) / NOTES_FILE).exists())
                self.assertEqual((state["po_queue_coverage"], state["po_queue_diagnostic"]),
                                 ("unavailable", LEGACY_QUEUE_COVERAGE))
                queue_step = next(step for step in machine["steps"] if step["name"] == "po-queue")
                self.assertEqual((queue_step["status"], queue_step["detail"]), ("unchanged", LEGACY_QUEUE_COVERAGE))
                self.assertIsNone(read_checkpoint_queue(data_dir / "board"))
                self.assertEqual((state["po_attachments_coverage"], state["po_attachments_diagnostic"]),
                                 ("unavailable", LEGACY_ATTACHMENTS_COVERAGE))
                images_step = next(step for step in machine["steps"] if step["name"] == "po-attachments")
                self.assertEqual((images_step["status"], images_step["detail"]),
                                 ("unchanged", LEGACY_ATTACHMENTS_COVERAGE))
                self.assertIsNone(read_checkpoint_attachments(data_dir / "board"))
                self.assertEqual(state["sprint_close_coverage"], "unavailable")
                self.assertEqual(state["sprint_close_parity"], "unavailable")
                self.assertEqual(state["sprint_close_diagnostic"], LEGACY_CLOSE_COVERAGE)
                self.assertEqual(next(step for step in machine["steps"] if step["name"] == "sprint-close")["detail"], LEGACY_CLOSE_COVERAGE)
                self.assertTrue(all(not value["decisions"] and value["close_reason"] is None for value in client.sprints.export_close_facts().values()))
                self.assertEqual(OwnerEventStore(client.credentials, client=client).export(), [])
                current = self._kinds(target, data_dir, self.target_config)
                if previous is not None:
                    self.assertEqual(current, previous)
                previous = current
                output = io.StringIO()
                with redirect_stdout(output):
                    code = cli_main(["doctor", "--offline", "--json", "--instance", str(target)])
                self.assertEqual(code, 0, output.getvalue())
                self.assertTrue(json.loads(output.getvalue())["ok"])

    def test_native_close_facts_recover_with_audit_result_knowledge_and_new_app_close(self):
        source = SqlCardClient(self.source_config.for_role("app"), self.fixture.source)
        self.addCleanup(source.close)
        expected = source.sprints.export_close_facts()
        document = expected[SPRINTS[0]]["closeout_document"]
        original_bytes = (self.fixture.source / "state" / "knowledge" / document).read_bytes()
        code, output = self._bootstrap()
        self.assertEqual(code, 0, output)
        result, refused = self._recover()
        self.assertEqual(result.status, "ok", f"{result.render()} {refused}")
        target, data_dir = self.fixture.target, self.fixture.data_dir
        client = SqlCardClient(self.target_config.for_role("app"), target)
        self.addCleanup(client.close)
        require_close_set_parity(expected, client.sprints.export_close_facts())
        self.assertEqual((target / "state" / "knowledge" / document).read_bytes(), original_bytes)
        state = restore_state(data_dir)
        self.assertEqual((state["sprint_close_coverage"], state["sprint_close_parity"]), ("restored", "complete"))
        require_close_set_parity(expected, read_checkpoint_close_facts(data_dir / "board"))
        writer = SprintWriter(client, data_dir=data_dir, instance=target)
        before = self._kinds(target, data_dir, self.target_config)
        bell = OwnerEventStore(client.credentials, client=client).export()
        replay = writer.close(role="po", actor="test", reference=SPRINTS[0], request_id="close-snapshot-sprint")
        for field in ("event_id", "issue_decisions", "card_dispositions", "closed_issues", "disposed_tasks", "reason", "closeout"):
            self.assertEqual(replay[field], self.source_close_result[field])
        self.assertEqual(self._kinds(target, data_dir, self.target_config), before)
        self.assertEqual(OwnerEventStore(client.credentials, client=client).export(), bell)
        self.assertEqual((target / "state" / "knowledge" / document).read_bytes(), original_bytes)
        result, refused = self._recover()
        self.assertEqual(result.status, "ok", f"{result.render()} {refused}")
        require_close_set_parity(expected, client.sprints.export_close_facts())
        written = writer.close(
            role="po", actor="test", reference=SPRINTS[1], request_id="new-close-after-recovery",
            decisions={"issues": [{"ref": self.issue, "verdict": "open", "reason": "retained for the next sprint"}], "cards": []},
        )
        self.assertEqual(written["sprint"]["status"], "closed")
        actual = client.sprints.export_close_facts()
        self.assertEqual(actual[SPRINTS[1]]["decisions"][0]["request_id"], "new-close-after-recovery")
        self.assertGreater(actual[SPRINTS[1]]["decisions"][0]["decision_id"], actual[SPRINTS[0]]["decisions"][0]["decision_id"])

    def test_committed_native_close_facts_materialize_flat_and_split_before_normal_import(self):
        installation_commands.materialize_head_registry(
            self.fixture.source, PRODUCT_ROOT,
            installation_commands._validated_instance(self.fixture.source), None,
        )
        checkout = self.root / "selected-checkpoint"
        git(self.root, "clone", "--branch", SNAPSHOT_REF.removeprefix("refs/heads/"), str(self.fixture.remote), str(checkout))
        board = open_checkpoint_board(checkout / "state" / "board")
        expected = read_checkpoint_close_facts(board.directory)
        flat = self.root / "flat-checkpoint"
        flat_board = flat / "state" / "board"
        flat_board.mkdir(parents=True)
        for name in LOGICAL_FILES:
            if board.has(name):
                (flat_board / name).write_bytes(board.read_bytes(name))
        _write_analytics_manifest(flat_board)
        shutil.copytree(checkout / "state" / "runs", flat / "state" / "runs")
        git(flat, "init", "--quiet")
        git(flat, "add", "state")
        git(flat, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--quiet", "-m", "Flat checkpoint")
        for layout, instance in (("flat", flat), ("split", checkout)):
            with self.subTest(layout=layout):
                verify_analytics_checkpoint(instance / "state" / "board")
                data = self.root / f"materialized-{layout}"
                materialize_checkpoint(instance, data)
                config = PostgresBoard.shared().fresh_database()
                client = SqlCardClient(config.for_role("app"), self.fixture.source)
                self.addCleanup(client.close)
                for _ in range(2):
                    import_normalized_board(data, client=client, instance=self.fixture.source)
                    require_close_set_parity(expected, client.sprints.export_close_facts())

    def test_normal_recovery_preserves_held_settled_and_read_bell_facts_and_accepts_new_answer(self):
        source, source_data = self.fixture.source, self.fixture.source_data
        client = SqlCardClient(self.source_config.for_role("app"), source)
        self.addCleanup(client.close)
        writer = TaskWriter(client, data_dir=source_data)
        self.handovers = {}
        for number in (3, 4):
            ref = f"ummanu-{number}"
            writer.create(
                role="po",
                actor="po",
                project="ummanu",
                task_type="decision",
                title="Owner decision",
                target="ready",
                sprint=SPRINTS[1],
                reference=ref,
                request_id=f"create-owner-{number}",
            )
            writer.claim(
                role="dispatcher",
                actor="test",
                reference=ref,
                worker="fixture",
                request_id=f"claim-owner-{number}",
            )
            self.handovers[number] = writer.handover(
                role="po",
                actor="po",
                reference=ref,
                to="owner",
                reason="Choose a recovery option",
                request_id=f"handover-owner-{number}",
            )
        answer = writer.record_owner_answer(
            role="po",
            actor="po",
            reference="ummanu-3",
            handover_event=self.handovers[3]["event_id"],
            quotation="Use option A",
            request_id="source-owner-answer",
        )
        writer.escalate_po_card(
            actor="test", reference="ummanu-3", episode=answer["event_id"], reason="Owner help required"
        )
        events = OwnerEventStore(client.credentials, client=client)
        rows = events.export()
        start = max(row["id"] for row in rows)
        extra = [dict(row, id=start + index + 1) for index, row in enumerate(bell_rows(513))]
        with client.transaction():
            events.restore(rows + extra)
        source_bell = events.export()
        source_attention = events.snapshot()
        state_dir = self.root / "owner-pipeline-state"
        state_dir.mkdir()
        self.tip = self.fixture.cut(stand_in=False, state_dir=state_dir)
        code, output = self._bootstrap()
        self.assertEqual(code, 0, output)
        result, refused = self._recover()
        self.assertEqual(result.status, "ok", f"{result.render()} {refused}")
        target, data_dir = self.fixture.target, self.fixture.data_dir
        state = restore_state(data_dir)
        client = SqlCardClient(self.target_config.for_role("app"), target)
        self.addCleanup(client.close)
        events = OwnerEventStore(client.credentials, client=client)
        require_owner_parity(source_bell, read_checkpoint_owner_events(data_dir / "board"))
        require_owner_parity(source_bell, events.export())
        self.assertEqual(state["owner_events_coverage"], "restored")
        self.assertEqual(state["owner_events_parity"], "complete")
        attention = OwnerEventLayer(target, store=events).snapshot()
        for field in ("unread", "needs_owner_count", "notice_count", "held_count"):
            self.assertEqual(attention[field], source_attention[field])
        held = [event for event in events.events() if event.held]
        self.assertEqual({event.kind for event in held}, {"card_handed_to_owner", "po_card_escalated"})
        for event in held:
            with self.assertRaises(ReadRefused):
                events.mark_read(event.id)
        settled = next(
            event
            for event in events.events()
            if event.kind == "card_handed_to_owner" and event.subject_ref == "ummanu-3"
        )
        self.assertFalse(settled.pinned)
        self.assertFalse(settled.held)
        writer = TaskWriter(client, data_dir=data_dir)
        writer.record_owner_answer(
            role="po",
            actor="po",
            reference="ummanu-4",
            handover_event=self.handovers[4]["event_id"],
            quotation="Use option B",
            request_id="recovered-owner-answer",
        )
        self.assertFalse(next(event for event in events.events() if event.subject_ref == "ummanu-4").held)
        saved = source_bell[-1]
        self.assertFalse(
            events.insert(saved["kind"], saved["subject_ref"], saved["text"], saved["dedup_key"])
        )

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

        result, refused = self._recover()

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
        # The PO dialogue is back in SQL through the app role, rebound to this host, before the host
        # step, and admission is open for new owner input.
        self.assertEqual(steps["po-sql"], ("changed", RESTORED_PO_COVERAGE))
        self.assertEqual((state["po_sql_coverage"], state["po_sql_restore"]), ("captured", "finished"))
        po = PoStore(self.target_config.for_role("app"))
        self.addCleanup(po.close)
        self.assertEqual(
            {session.session_id for status in ("open", "closed") for session in po.sessions(status)},
            {"closed", "empty", "open"},
        )
        self.assertEqual(po.session("open").cwd, str(workspace_dir(data_dir.resolve())))
        self.assertEqual([turn.state for turn in po.turns("open")][-1], INTERRUPTED)
        self.assertEqual(len(po.feed("open")), len(po_records()["po_feed"]))
        self.assertIsNotNone(po.request("send"))
        self.assertIsNone(PoRecoveryAdmission(data_dir.resolve(), po).status())
        # The PO's notes are back at the ordinary workspace path with their exact bytes, private and
        # the runtime user's, and the install step's workspace materialize leaves them.
        self.assertEqual(read_checkpoint_notes(data_dir / "board"), NotesCapture(SOURCE_NOTES))
        notes = workspace_dir(data_dir.resolve()) / NOTES_FILE
        self.assertEqual(steps["po-notes"], ("unchanged", RESTORED_NOTES_COVERAGE))
        self.assertEqual((state["po_notes_coverage"], state["po_notes_parity"]), ("captured", "complete"))
        self.assertEqual(notes.read_bytes(), SOURCE_NOTES)
        self.assertEqual((notes.stat().st_mode & 0o777, notes.stat().st_uid), (0o600, os.getuid()))
        self.assertNotIn(NOTES_FILE, self._materialize_po_workspace(data_dir.resolve()))
        self.assertEqual(notes.read_bytes(), SOURCE_NOTES)
        self._assert_inert_queue(steps, data_dir, po)
        self._assert_inert_attachments(steps, data_dir)
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


    def test_retry_after_new_po_activity_and_a_live_export_reads_the_selected_checkpoint(self) -> None:
        self.assertEqual(self._bootstrap()[0], 0)
        result, refused = self._recover()
        self.assertEqual(result.status, "ok", f"{result.render()}\nrefused store batches: {refused}")
        data_dir = self.fixture.data_dir.resolve()
        po, client = self._target_po()
        notes = workspace_dir(data_dir) / NOTES_FILE
        launches = []
        seen_notes = []

        def launch(session, seq, argv, files, environment, spec):
            launches.append(seq)
            seen_notes.append((Path(session.cwd) / NOTES_FILE).read_bytes())
            files.stdout.write_text(json.dumps({"type": "thread.started", "thread_id": "new-thread"}) + "\n")
            files.stderr.write_text("")
            files.last_message.write_text("fresh answer")
            return _FinishedProcess()

        runner = PoRunner(po, data_dir, head_specs={}, env={}, turn_launcher=launch)
        self.enterContext(mock.patch.object(runner, "codex_home", return_value=self.root))
        turn, created = runner.send_request("open", "input after recovery", "after-recovery")
        self.assertTrue(created)
        self.assertEqual(runner.wait("open", turn.seq, 2).state, COMPLETED)
        self.assertEqual(po.session("open").cli_session_id, "new-thread")
        self.assertEqual(seen_notes, [SOURCE_NOTES])
        # The installation's ordinary export rewrites `<data>/board` with the new history; a capture
        # there holding other notes is not what the retry restores.
        export_board(data_dir, instance_dir=self.fixture.target)
        self.assertNotEqual(read_checkpoint_po(data_dir / "board"), po_records())
        self.assertEqual(read_checkpoint_notes(data_dir / "board"), NotesCapture(SOURCE_NOTES))
        write_notes(data_dir / "board", b"notes of a later live export")
        live = self._readback(client)
        plan = (data_dir / ".po-restore" / "plan.json").read_bytes()

        again, refused = self._recover()
        steps = {step.name: (step.status, step.detail) for step in again.steps}
        self.assertEqual(again.status, "ok", f"{again.render()}\nrefused store batches: {refused}")
        self.assertEqual(steps["board"][0], "unchanged")
        self.assertEqual(steps["po-sql"], ("unchanged", RESTORED_PO_COVERAGE))
        self.assertEqual(self._readback(client), live)
        self.assertEqual((data_dir / ".po-restore" / "plan.json").read_bytes(), plan)
        self.assertIsNone(PoRecoveryAdmission(data_dir, po).status())
        replay, created = runner.send_request("open", "input after recovery", "after-recovery")
        self.assertEqual((replay.seq, created), (turn.seq, False))
        self.assertEqual(launches, [turn.seq])
        self.assertEqual(steps["po-notes"], ("unchanged", RESTORED_NOTES_COVERAGE))
        self.assertEqual(notes.read_bytes(), SOURCE_NOTES)
        # Notes the PO changed after recovery are its work: a retry refuses the conflict, keeps them
        # and names no content.
        edited = SOURCE_NOTES + b"Decided after recovery: keep the drill weekly.\n"
        notes.write_bytes(edited)
        conflicted, refused = self._recover()
        self.assertEqual(conflicted.status, "failed", f"{conflicted.render()}\nrefused store batches: {refused}")
        self.assertIn("PO notes target conflict", conflicted.render())
        self.assertNotIn("Decided after recovery", conflicted.render())
        self.assertEqual(notes.read_bytes(), edited)
        self.assertEqual(restore_state(data_dir)["po_notes_parity"], "failed")

    def test_a_committed_flat_checkpoint_recovers_notes_for_the_po_consumer_and_a_retry(self) -> None:
        """A legacy flat Git checkpoint through `recover`: the PO consumer reads its notes, which stay."""
        flat = self.root / "flat-source"
        git(self.root, "clone", "--quiet", "--branch", "main", str(self.fixture.remote), str(flat))
        board = flat / "state" / "board"
        reader = open_checkpoint_board(board)
        logical = {name: reader.read_bytes(name) for name in LOGICAL_FILES if reader.has(name)}
        ignore = (board / ".gitignore").read_bytes()
        shutil.rmtree(board)
        board.mkdir()
        for name, payload in logical.items():
            (board / name).write_bytes(payload)
        (board / ".gitignore").write_bytes(ignore)
        _write_analytics_manifest(board)
        verify_analytics_checkpoint(board)
        self.assertEqual(open_checkpoint_board(board).layout, FLAT)
        self.assertEqual(read_checkpoint_notes(board), NotesCapture(SOURCE_NOTES))
        # Without the snapshot manifest the tip is a legacy checkpoint, recovered as a Git checkout.
        (flat / "snapshot-manifest.json").unlink()
        legacy = self.root / "legacy.git"
        git(self.root, "init", "--quiet", "--bare", "--initial-branch", "main", str(legacy))
        config = flat / "instance.yaml"
        config.write_text(config.read_text(encoding="utf-8").replace(str(self.fixture.remote), str(legacy)), encoding="utf-8")
        store.ensure_ignored(flat)
        git(flat, "add", "-A")
        git(flat, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--quiet", "-m", "Flat checkpoint")
        git(flat, "push", "--quiet", str(legacy), "HEAD:refs/heads/main")
        target = self.root / "flat-target"
        code, output = self._bootstrap(target=target, remote=legacy)
        self.assertEqual(code, 0, output)
        args = self._args(target=target, remote=legacy)
        data_dir = self.fixture.data_dir.resolve()
        notes = workspace_dir(data_dir) / NOTES_FILE

        result, refused = self._recover(args=args)
        steps = {step.name: (step.status, step.detail) for step in result.steps}
        self.assertEqual(result.status, "ok", f"{result.render()}\nrefused store batches: {refused}")
        self.assertTrue((target / ".git").is_dir())
        self.assertEqual(open_checkpoint_board(target / "state" / "board").layout, FLAT)
        self.assertEqual(steps["po-notes"], ("unchanged", RESTORED_NOTES_COVERAGE))
        self.assertEqual(notes.read_bytes(), SOURCE_NOTES)
        self.assertEqual(notes.stat().st_mode & 0o777, 0o600)
        po = PoStore(self.target_config.for_role("app"))
        self.addCleanup(po.close)
        self._assert_inert_queue(steps, data_dir, po)
        self._assert_inert_attachments(steps, data_dir)
        seen_notes = []

        def launch(session, seq, argv, files, environment, spec):
            seen_notes.append((Path(session.cwd) / NOTES_FILE).read_bytes())
            files.stdout.write_text(json.dumps({"type": "thread.started", "thread_id": "flat-thread"}) + "\n")
            files.stderr.write_text("")
            files.last_message.write_text("answer after the flat recovery")
            return _FinishedProcess()

        runner = PoRunner(po, data_dir, head_specs={}, env={}, turn_launcher=launch)
        self.enterContext(mock.patch.object(runner, "codex_home", return_value=self.root))
        turn, created = runner.send_request("open", "input after the flat recovery", "after-flat-recovery")
        self.assertTrue(created)
        self.assertEqual(runner.wait("open", turn.seq, 2).state, COMPLETED)
        self.assertEqual(seen_notes, [SOURCE_NOTES])
        self.assertNotIn(NOTES_FILE, self._materialize_po_workspace(data_dir))
        self.assertEqual(notes.read_bytes(), SOURCE_NOTES)

        again, refused = self._recover(args=args)
        steps = {step.name: (step.status, step.detail) for step in again.steps}
        self.assertEqual(again.status, "ok", f"{again.render()}\nrefused store batches: {refused}")
        self.assertEqual(steps["po-notes"], ("unchanged", RESTORED_NOTES_COVERAGE))
        self._assert_inert_queue(steps, data_dir, po)
        self._assert_inert_attachments(steps, data_dir)
        self.assertEqual(notes.read_bytes(), SOURCE_NOTES)
        self.assertEqual(notes.stat().st_mode & 0o777, 0o600)

    def test_a_known_secret_in_the_notes_keeps_the_live_export_and_the_committed_cut(self) -> None:
        """The real export refuses decoded notes holding a runtime secret before replacing its last cut."""
        source, source_data = self.fixture.source, self.fixture.source_data
        board = source_data / "board"
        before = {path.name: path.read_bytes() for path in board.iterdir() if path.is_file()}
        repository = self.fixture.source_snapshot
        tip = git(repository, "rev-parse", SNAPSHOT_REF)
        committed = git(repository, "cat-file", "blob", f"{tip}:state/board/{NOTES_CHECKPOINT_FILE}")
        # JSON escapes the quote and the backslash, so only the decoded notes contain the value.
        secret = 'private"known\\secret-value'
        with (source / "runtime.env").open("a", encoding="utf-8") as runtime_env:
            runtime_env.write(f"EXAMPLE_TOKEN={secret}\n")
        (workspace_dir(source_data) / NOTES_FILE).write_bytes(SOURCE_NOTES + f"pasted {secret}\n".encode())

        result = SnapshotExporter(
            source_data,
            source,
            snapshot_repo=repository,
            product_revision=REVISION,
            board_schema_head=head_revision(),
            state_dir=self.root / "source-pipeline-state",
        ).write()

        self.assertEqual(result.status, "blocked", result.reason)
        self.assertIn("secret detected in PO notes", result.reason)
        self.assertNotIn("private", result.reason)
        self.assertEqual({path.name: path.read_bytes() for path in board.iterdir() if path.is_file()}, before)
        self.assertEqual(git(repository, "rev-parse", SNAPSHOT_REF), tip)
        self.assertEqual(git(repository, "cat-file", "blob", f"{tip}:state/board/{NOTES_CHECKPOINT_FILE}"), committed)

    def test_a_secret_or_unsafe_queue_input_keeps_the_live_export_and_the_committed_cut(self) -> None:
        """Escaped secrets in a queued input and a symlinked input both refuse before any replacement."""
        source, source_data = self.fixture.source, self.fixture.source_data
        board = source_data / "board"
        before = {path.name: path.read_bytes() for path in board.iterdir() if path.is_file()}
        repository = self.fixture.source_snapshot
        tip = git(repository, "rev-parse", SNAPSHOT_REF)
        committed = git(repository, "ls-tree", "-r", tip, "--", "state/board/po_queue")
        self.assertTrue(committed)
        secret = 'private"known\\secret-value'
        with (source / "runtime.env").open("a", encoding="utf-8") as runtime_env:
            runtime_env.write(f"EXAMPLE_TOKEN={secret}\n")
        queue = PoQueue(source_data)
        pasted = queue.put(session_id="open", text="clean text", request_id="pasted", source="web",
                           metadata={"attachments": [f"pasted {secret}"]})
        outside = self.root / "outside-input.json"
        outside.write_text(json.dumps({**pasted.document(), "text": "private outside"}), encoding="utf-8")

        def unsafe() -> None:
            queue.remove(pasted)
            (queue_dir(source_data) / pasted.name).symlink_to(outside)

        for label, damage, reason in (
            ("secret", lambda: None, "secret detected in PO queue pending record 3 field metadata"),
            ("symlink", unsafe, "PO queue capture refuses pending entry"),
        ):
            with self.subTest(source=label):
                damage()
                result = SnapshotExporter(
                    source_data,
                    source,
                    snapshot_repo=repository,
                    product_revision=REVISION,
                    board_schema_head=head_revision(),
                    state_dir=self.root / "source-pipeline-state",
                ).write()
                self.assertEqual(result.status, "blocked", result.reason)
                self.assertIn(reason, result.reason)
                self.assertNotIn("private", result.reason)
                self.assertEqual({path.name: path.read_bytes() for path in board.iterdir() if path.is_file()}, before)
                self.assertEqual(git(repository, "rev-parse", SNAPSHOT_REF), tip)
                self.assertEqual(git(repository, "ls-tree", "-r", tip, "--", "state/board/po_queue"), committed)

    def test_a_queue_over_the_part_budget_commits_bounded_blobs_that_recover_whole(self) -> None:
        """A backlog and one long refused input exceed the part budget: every committed blob stays
        within it, recovery materializes the whole queue, a shrink leaves no stale part, and a
        secret a part boundary cuts is still refused before the commit."""
        budget = 1024
        self.enterContext(mock.patch.object(checkpoint_layout, "MAX_SEGMENT_BYTES", budget))
        fixture = self.fixture
        queue = PoQueue(fixture.source_data)
        backlog = [
            queue.put(session_id="open", text=f"backlog {index} " + "я" * 200, request_id=f"backlog-{index}",
                      source="web")
            for index in range(12)
        ]
        queue.refuse(queue.put(session_id="closed", text="long " + "я" * 1500, request_id="long-refused",
                               source="web"), "PO session closed is closed")
        state_dir = self.root / "source-pipeline-state"

        def committed(tip: str) -> tuple[list[tuple[str, int]], bytes]:
            listing = git(fixture.remote, "ls-tree", "-r", "-l", "--full-tree", tip, "--", "state/board/po_queue")
            parts = [(line.split("\t")[1], int(line.split()[3])) for line in listing.splitlines()]
            payload = b"".join(
                subprocess.run(["git", "-C", str(fixture.remote), "cat-file", "blob", f"{tip}:{path}"],
                               capture_output=True, check=True).stdout
                for path, _ in parts
            )
            return parts, payload

        for stage in ("grown", "shrunk"):
            with self.subTest(stage=stage):
                if stage == "shrunk":
                    for item in backlog:
                        queue.remove(item)
                tip = fixture.cut(stand_in=False, state_dir=state_dir)
                parts, payload = committed(tip)
                logical = (fixture.source_data / "board" / QUEUE_CHECKPOINT_FILE).read_bytes()
                self.assertEqual(payload, logical)
                self.assertGreater(len(parts), 1)
                self.assertTrue(all(0 < size <= budget for _, size in parts))
                self.assertEqual(
                    [path for path, _ in parts],
                    [f"state/board/po_queue/{part_name(index, '.ndjson').as_posix()}" for index in range(len(parts))],
                )
                self.assertEqual(git(fixture.remote, "ls-tree", "--name-only", tip, "--",
                                     f"state/board/{QUEUE_CHECKPOINT_FILE}"), "")
                if stage == "grown":
                    self.assertGreater(len(logical), 6 * budget)
                    source_queue = queue.capture()
                    self.assertEqual(self._bootstrap()[0], 0)
                    result, refused = self._recover()
                    self.assertEqual(result.status, "ok", f"{result.render()}\nrefused store batches: {refused}")
                    captured = read_checkpoint_queue(fixture.data_dir / "board")
                    self.assertEqual(captured.capture, source_queue)
                    self.assertEqual(captured.counts(), (15, 2))
                    self.assertEqual((fixture.data_dir / "board" / QUEUE_CHECKPOINT_FILE).read_bytes(), logical)
                    self.assertFalse(queue_dir(fixture.data_dir).exists())

        secret = 'private"known\\secret-value'
        with (fixture.source / "runtime.env").open("a", encoding="utf-8") as runtime_env:
            runtime_env.write(f"EXAMPLE_TOKEN={secret}\n")
        escaped = json.dumps(secret)[1:-1]
        board = fixture.source_data / "board"
        # A line longer than the budget is cut every `budget` bytes from its start; padding places the
        # escaped secret across the first cut, so no physical part holds it whole.
        probe = queue.put(session_id="open", text=secret, request_id="pasted", source="web")
        line = next(line for line in queue_payload(queue.capture()).splitlines() if b'"pasted"' in line)
        queue.remove(probe)
        offset = line.index(escaped.encode())
        queue.put(session_id="open", text="p" * (budget - offset - 5) + secret + "q" * budget, request_id="pasted",
                  source="web")
        chunks = segment_chunks(queue_payload(queue.capture()), budget, split_lines=True)
        self.assertFalse(any(escaped.encode() in chunk for chunk in chunks))
        # The stand-in producers stage this live export as it is, so only the cut's own scan can refuse.
        write_queue(board, queue.capture())
        repository = fixture.source_snapshot
        tip = git(repository, "rev-parse", SNAPSHOT_REF)
        before = committed(git(fixture.remote, "rev-parse", SNAPSHOT_REF))
        with exporter_producers():
            result = SnapshotExporter(
                fixture.source_data,
                fixture.source,
                snapshot_repo=repository,
                product_revision=REVISION,
                board_schema_head=head_revision(),
                state_dir=state_dir,
            ).write()
        self.assertEqual(result.status, "blocked", result.reason)
        self.assertIn("secret detected in PO queue pending record", result.reason)
        self.assertNotIn("private", result.reason)
        self.assertEqual(git(repository, "rev-parse", SNAPSHOT_REF), tip)
        self.assertEqual(committed(tip), before)

    def test_a_secret_or_damaged_image_keeps_the_live_export_and_the_committed_cut(self) -> None:
        """A runtime secret in an image's text metadata and a damaged stored image both refuse the cut."""
        source, source_data = self.fixture.source, self.fixture.source_data
        board = source_data / "board"
        before = {path.name: path.read_bytes() for path in board.iterdir() if path.is_file()}
        repository = self.fixture.source_snapshot
        tip = git(repository, "rev-parse", SNAPSHOT_REF)
        committed = git(repository, "ls-tree", "-r", tip, "--", "state/board/po_attachments")
        self.assertTrue(committed)
        secret = "private-known-secret-value"
        with (source / "runtime.env").open("a", encoding="utf-8") as runtime_env:
            runtime_env.write(f"EXAMPLE_TOKEN={secret}\n")
        pasted = store_image(source_data, "open", b"\x89PNG\r\n\x1a\n\x00\x00\x00\x20tEXtComment\x00"
                             + secret.encode() + b"\xff\x00")
        (session, name), image = next(iter(self.source_images.items()))

        def damaged() -> None:
            pasted.unlink()
            (attachments_dir(source_data, session) / name).write_bytes(image[:-1] + b"\x00")

        for label, damage, reason in (
            ("secret", lambda: None, "secret detected in PO attachment record"),
            ("damaged", damaged, f"PO session {session} attachment {name} does not hold the image its name names"),
        ):
            with self.subTest(source=label):
                damage()
                result = SnapshotExporter(
                    source_data,
                    source,
                    snapshot_repo=repository,
                    product_revision=REVISION,
                    board_schema_head=head_revision(),
                    state_dir=self.root / "source-pipeline-state",
                ).write()
                self.assertEqual(result.status, "blocked", result.reason)
                self.assertIn(reason, result.reason)
                self.assertIn(pasted.name if label == "secret" else name, result.reason)
                self.assertNotIn("private", result.reason)
                self.assertEqual({path.name: path.read_bytes() for path in board.iterdir() if path.is_file()}, before)
                self.assertEqual(git(repository, "rev-parse", SNAPSHOT_REF), tip)
                self.assertEqual(git(repository, "ls-tree", "-r", tip, "--", "state/board/po_attachments"), committed)

    def test_images_over_the_part_budget_commit_bounded_blobs_that_recover_whole_and_shrink(self) -> None:
        """Images larger than the part budget commit as bounded blobs, recover with exact bytes, and
        a removal leaves no stale part."""
        budget = 1024
        self.enterContext(mock.patch.object(checkpoint_layout, "MAX_SEGMENT_BYTES", budget))
        fixture = self.fixture
        state_dir = self.root / "source-pipeline-state"
        extra = store_image(fixture.source_data, "closed", b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 9)

        def committed(tip: str) -> tuple[list[tuple[str, int]], bytes]:
            listing = git(fixture.remote, "ls-tree", "-r", "-l", "--full-tree", tip, "--", "state/board/po_attachments")
            parts = [(line.split("\t")[1], int(line.split()[3])) for line in listing.splitlines()]
            payload = b"".join(
                subprocess.run(["git", "-C", str(fixture.remote), "cat-file", "blob", f"{tip}:{path}"],
                               capture_output=True, check=True).stdout
                for path, _ in parts
            )
            return parts, payload

        counts = []
        for stage in ("grown", "shrunk"):
            with self.subTest(stage=stage):
                if stage == "shrunk":
                    extra.unlink()
                tip = fixture.cut(stand_in=False, state_dir=state_dir)
                parts, payload = committed(tip)
                logical = (fixture.source_data / "board" / ATTACHMENTS_CHECKPOINT_FILE).read_bytes()
                self.assertEqual(payload, logical)
                self.assertTrue(all(0 < size <= budget for _, size in parts))
                self.assertEqual(
                    [path for path, _ in parts],
                    [f"state/board/po_attachments/{part_name(index, '.ndjson').as_posix()}"
                     for index in range(len(parts))],
                )
                self.assertEqual(git(fixture.remote, "ls-tree", "--name-only", tip, "--",
                                     f"state/board/{ATTACHMENTS_CHECKPOINT_FILE}"), "")
                counts.append(len(parts))
                if stage == "grown":
                    self.assertEqual(self._bootstrap()[0], 0)
                    result, refused = self._recover()
                    self.assertEqual(result.status, "ok", f"{result.render()}\nrefused store batches: {refused}")
                    self.assertEqual((fixture.data_dir / "board" / ATTACHMENTS_CHECKPOINT_FILE).read_bytes(), logical)
                    self.assertEqual(len(read_checkpoint_attachments(fixture.data_dir / "board").files), 4)
                    self.assertFalse(attachments_dir(fixture.data_dir, "closed").exists())
        self.assertLess(counts[1], counts[0])

    def test_locked_runtime_secrets_restore_board_and_po_sql_but_skip_activation(self) -> None:
        self.assertEqual(self._bootstrap()[0], 0)
        locked = SecretRecovery(
            store_present=True,
            unlocked=False,
            locked=({"id": "example_token", "environment": "EXAMPLE_TOKEN", "target": "runtime-env"},),
        )
        data_dir = self.fixture.data_dir.resolve()
        po, client = self._target_po()
        for attempt in ("first", "retry"):
            with self.subTest(attempt=attempt):
                result, refused = self._recover(
                    mock.patch("ummanu.installation.commands._open_secret_store", return_value=locked)
                )
                steps = {step.name: (step.status, step.detail) for step in result.steps}
                self.assertEqual(result.status, "failed", f"{result.render()}\nrefused store batches: {refused}")
                self.assertIn("recovery is incomplete", result.render())
                changed = "changed" if attempt == "first" else "unchanged"
                self.assertEqual(steps["board"][0], changed)
                self.assertEqual(steps["po-sql"], (changed, RESTORED_PO_COVERAGE))
                self.assertEqual(steps["host"][0], "skipped")
                self.assertEqual(restore_state(data_dir)["po_sql_restore"], "finished")
                self.assertEqual(len(self._readback(client)["po_sessions"]), len(po_records()["po_sessions"]))
                self.assertIsNone(PoRecoveryAdmission(data_dir, po).status())


class RootHandoffSnapshotRecoveryPostgresTests(_SnapshotRecoveryCase):
    """Recovery as real root for a distinct runtime account; CI only (the runner has `sudo -n`)."""

    def _temporary_base(self) -> str:
        # The suite's temporary root is private to the test account, so a runtime account could not
        # reach anything under it; this case owns its own traversable root and its removal.
        base = Path(tempfile.mkdtemp(prefix="ummanu-cross-uid-", dir="/tmp"))
        base.chmod(0o755)
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        return str(base)

    @staticmethod
    def _runtime_view(user: str, repository: Path) -> str:
        """Which ancestors of the snapshot repository the runtime account can traverse, and its probe."""
        lines = []
        for path in (*reversed(repository.parents), repository):
            entered = subprocess.run(["sudo", "-n", "-u", user, "test", "-x", str(path)], check=False)
            lines.append(f"{path}: traversable={entered.returncode == 0}")
        probe = subprocess.run(
            ["sudo", "-n", "-u", user, "git", "--git-dir", str(repository), "rev-parse", "--is-bare-repository"],
            capture_output=True, text=True, cwd="/", check=False,
        )
        lines.append(f"probe rc={probe.returncode} stdout={probe.stdout.strip()!r} stderr={probe.stderr.strip()[-2000:]!r}")
        return "\n".join(lines)

    def test_root_recovery_hands_po_recovery_to_a_distinct_runtime_user_and_retries(self) -> None:
        """Real root, a real distinct runtime account and real ownership changes, through `recover`.

        Bootstrap's handoff gives the live root and data plane to the runtime user; a root `recover`
        then fails inside the PO import and is retried.
        """
        self.assertEqual(self._bootstrap()[0], 0)
        user = f"ummanu-po-{secrets.token_hex(3)}"
        subprocess.run(["sudo", "-n", "useradd", "--create-home", "--user-group", user], check=True)
        self.addCleanup(subprocess.run, ["sudo", "-n", "userdel", "--remove", user], check=False)
        uid = int(subprocess.run(["id", "-u", user], check=True, capture_output=True, text=True).stdout)
        self.addCleanup(subprocess.run, ["sudo", "-n", "chown", "-R", f"{os.getuid()}:{os.getgid()}",
                                         str(self.root)], check=True)
        self.root.chmod(0o755)
        fixture = self.fixture
        # The remote stays the invoking account's, as a remote host's would not be the runtime user's.
        subprocess.run(["sudo", "-n", "chown", "-R", f"{user}:{user}", str(fixture.target),
                        str(fixture.data_dir)], check=True)
        repository = Path(__file__).resolve().parents[1]
        data_dir = fixture.data_dir.resolve()
        runs = []
        for fail_import in (True, False):
            spec = {"args": {**vars(self._args()), "installation_user": user}, "data_dir": str(data_dir),
                    "uid": uid, "fail_import": fail_import}
            # From `/`: runtime-user Git children inherit the working directory, and the runner's
            # checkout is not theirs to traverse.
            completed = subprocess.run(
                ["sudo", "-n", "env", f"PYTHONPATH={repository}:{repository / 'src'}", sys.executable, "-B",
                 "-m", "tests.support.snapshot_recovery"],
                input=json.dumps(spec), capture_output=True, text=True, cwd="/", check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr[-4000:])
            runs.append(json.loads(completed.stdout.splitlines()[-1]))
        failed, retried = runs
        self.assertEqual(failed["status"], "failed", failed["steps"])
        detail = " ".join(step[2] for step in failed["steps"])
        if "PO SQL restore failed at import: injected root recovery import failure" not in detail:
            self.fail(f"{failed['steps']}\n{self._runtime_view(user, data_dir / 'backup' / 'instance.git')}")
        self.assertEqual(retried["status"], "ok", retried["steps"])
        self.assertEqual(retried["po_sql_restore"], "finished")
        for run, phase in ((failed, "holding"), (retried, "finished")):
            # Before the final barrier: whatever root wrote, the PO authority is the runtime user's own.
            facts = run["before_final_barrier"]
            self.assertEqual(facts["data_dir"]["uid"], uid)
            self.assertEqual(facts["data_dir"]["mode"] & 0o022, 0)
            artifacts = facts["artifacts"]
            for name, mode in ((".po-recovery-admission.json", 0o600), (".po-restore", 0o700),
                               (".po-restore/plan.json", 0o600)):
                self.assertEqual(artifacts[name], {"uid": uid, "mode": mode}, name)
            self.assertEqual(facts["admission_phase"], phase)
            self.assertEqual(run["after"]["foreign"], [])
        self.assertIsNone(failed["before_final_barrier"]["artifacts"][".po-restore/contexts.json"])
        self.assertEqual(retried["before_final_barrier"]["artifacts"][".po-restore/contexts.json"],
                         {"uid": uid, "mode": 0o600})
        self.assertEqual(retried["after"]["plan_sha256"], failed["after"]["plan_sha256"])
        # The notes are published before the PO SQL phase, by the runtime user, on both runs.
        for run in (failed, retried):
            facts = run["before_final_barrier"]
            self.assertEqual(facts["artifacts"]["po"], {"uid": uid, "mode": 0o700})
            self.assertEqual(facts["artifacts"]["po/NOTES.md"], {"uid": uid, "mode": 0o600})
            self.assertEqual(facts["notes_sha256"], hashlib.sha256(SOURCE_NOTES).hexdigest())
        self.assertIn(["po-notes", "unchanged", RESTORED_NOTES_COVERAGE], retried["steps"])
        _, client = self._target_po()
        self.assertEqual(len(self._readback(client)["po_sessions"]), len(po_records()["po_sessions"]))


if __name__ == "__main__":
    unittest.main()
