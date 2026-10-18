from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import ummanu.data as data_module
from tests.fakes.sprints import _write_project_registry, sprint_store
from tests.fakes.tasks import empty_seed
from tests.origin_checkpoint_fixtures import obligations
from tests.owner_checkpoint_fixtures import bell_rows
from tests.po_checkpoint_fixtures import FINISHED, MOMENT, po_records, seed_po
from tests.sql_backend_fixtures import card_store
from ummanu import memory_journal
from ummanu.board.close_checkpoint import read_checkpoint_close_facts, require_close_set_parity
from ummanu.board.origin_checkpoint import read_checkpoint_outbox, require_outbox_parity
from ummanu.board.origin_outbox import OriginOutboxStore
from ummanu.board.owner_checkpoint import read_checkpoint_owner_events, require_owner_parity
from ummanu.board.owner_events import OwnerEventStore
from ummanu.board.po_checkpoint import PO_FILE, read_checkpoint_po
from ummanu.board.po_notes_checkpoint import NOTES_CHECKPOINT_FILE, NotesCapture, read_checkpoint_notes
from ummanu.board.po_queue_checkpoint import read_checkpoint_queue
from ummanu.board.sql_audit import SqlTaskAudit
from ummanu.board.sql_cards import SqlCardClient
from ummanu.data import (
    export_all,
    export_artifacts,
    export_board,
    export_memory,
    export_runs,
    export_transcripts,
    init_layout,
    manifest_for,
    normalize_board_card,
)
from ummanu.memory.canon import canon_revision, fact_content_hash, parse_fact_text
from ummanu.memory_journal import verify_memory_journal
from ummanu.memory_write import (
    MEMORY_PROPOSAL_ACTIVE_MARKER,
    MemoryExportPublishError,
    MemoryLockError,
    MemoryPermissionError,
    MemoryProtocolError,
    MemoryValidationError,
    _gc_staging_proposals,
    commit_memory_proposal,
    propose_memory_fact,
    supersede_memory_fact,
)
from ummanu.po import recovery_admission
from ummanu.po.queue import PoQueue
from ummanu.po.recovery_admission import PoRecoveryAdmission
from ummanu.po.workspace import NOTES_FILE, workspace_dir
from ummanu.sprint_observer import none_choice
from ummanu.sprints import SPRINT_BOARD_NAME, SprintWriter
from ummanu.tasks import TaskError, TaskReader, TaskWriter


class TaskExportReader:
    """Small seam for the canonical task export; it never starts a subprocess.

    Its `client` is a real card store: the export's publication gate is that client's card audit.
    """

    def __init__(self, cards: list[dict] | None = None, *, client: object = None) -> None:
        self.cards = cards if cards is not None else [{"id": 1, "reference": "ummanu-1", "title": "One"}]
        self.calls = 0
        self.client = client

    def export(self) -> list[dict]:
        self.calls += 1
        return self.cards


class DataLayoutTests(unittest.TestCase):
    def test_init_layout_creates_target_dirs_and_manifest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "ummanu-data"

            layout = init_layout(data_dir)

            for name in ("board", "memory", "runs", "transcripts", "artifacts", "backups"):
                self.assertTrue((data_dir / name).is_dir(), name)
            self.assertFalse((data_dir / "memory" / "facts").exists())
            self.assertEqual(layout.manifest_path, data_dir / "data-manifest.json")
            manifest_text = layout.manifest_path.read_text(encoding="utf-8")
            self.assertIn('"data_dir"', manifest_text)
            manifest = json.loads(manifest_text)
            self.assertEqual(manifest["components"]["memory"]["facts"], "state/memory/facts")

    def test_init_layout_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "ummanu-data"

            first = init_layout(data_dir)
            second = init_layout(data_dir)

            self.assertTrue(first.created_dirs)
            self.assertEqual(second.created_dirs, [])
            self.assertEqual(manifest_for(data_dir), manifest_for(data_dir))

    def test_init_layout_preserves_manifest_when_publish_short_writes(self):
        original_write_text = Path.write_text

        def partial_manifest_write(path, text, *args, **kwargs):
            if path.name.startswith(".data-manifest.json."):
                return original_write_text(path, "{partial", *args, **kwargs)
            return original_write_text(path, text, *args, **kwargs)

        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "ummanu-data"
            init_layout(data_dir)
            manifest_path = data_dir / "data-manifest.json"
            original_manifest = manifest_path.read_text(encoding="utf-8")

            with mock.patch("ummanu.data.Path.write_text", new=partial_manifest_write), self.assertRaises(RuntimeError):
                init_layout(data_dir)

            self.assertEqual(manifest_path.read_text(encoding="utf-8"), original_manifest)
            self.assertEqual(list(data_dir.glob(".data-manifest.json.*.tmp")), [])

    def test_init_layout_preserves_manifest_when_publish_write_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "ummanu-data"
            init_layout(data_dir)
            manifest_path = data_dir / "data-manifest.json"
            original_manifest = manifest_path.read_text(encoding="utf-8")

            with (
                mock.patch("ummanu.data.Path.write_text", side_effect=OSError("full")),
                self.assertRaises(RuntimeError),
            ):
                init_layout(data_dir)

            self.assertEqual(manifest_path.read_text(encoding="utf-8"), original_manifest)
            self.assertEqual(list(data_dir.glob(".data-manifest.json.*.tmp")), [])

    def test_init_layout_wraps_directory_prepare_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "ummanu-data"
            data_dir.write_text("not a directory", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "cannot prepare ummanu-data layout"):
                init_layout(data_dir)

    def test_init_layout_wraps_manifest_tempfile_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "ummanu-data"

            with (
                mock.patch("ummanu.data.tempfile.mkstemp", side_effect=PermissionError("denied")),
                self.assertRaisesRegex(RuntimeError, "could not write data manifest"),
            ):
                init_layout(data_dir)


class ExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = card_store(self, empty_seed())

    def test_board_cut_preserves_complete_po_sql_above_display_limits_without_writes(self):
        records = po_records(603)
        records["po_feed"][0]["text"] = "whole message\n" * 4000
        records["po_feed"][0]["metadata"]["summary"] = "whole metadata\n" * 2000
        seed_po(self.store, records)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reader = TaskExportReader(client=self.store)
            export_board(root, instance_dir=root, reader=reader, sprint_client=self.store)
            self.assertEqual(read_checkpoint_po(root / "board"), records)
            first = (root / "board" / PO_FILE).read_bytes()
            export_board(root, instance_dir=root, reader=reader, sprint_client=self.store)
            self.assertEqual((root / "board" / PO_FILE).read_bytes(), first)
        self.assertEqual(self.store._query("SELECT count(*) FROM po_feed"), [(603,)])
        self.assertEqual(self.store._query("SELECT state FROM po_turns WHERE session_id = 'open' AND seq = 4"), [("running",)])

    def test_turn_completion_and_acceptance_between_po_reads_stay_outside_the_cut(self):
        records = po_records()
        seed_po(self.store, records)
        other = SqlCardClient(self.store.credentials, self.store.instance_dir)
        self.addCleanup(other.close)
        original = self.store._query
        changed = False

        def read_then_accept(query, *args, **kwargs):
            nonlocal changed
            rows = original(query, *args, **kwargs)
            if "FROM po_sessions ORDER BY" in query and not changed:
                changed = True
                with other.transaction():
                    other._execute("UPDATE po_sessions SET title = 'new title', cli_session_id = 'new context' WHERE session_id = 'open'")
                    other._execute("UPDATE po_turns SET state = 'completed', finished_at = %s WHERE session_id = 'open' AND seq = 4", (FINISHED,))
                    other._execute("INSERT INTO po_turns (session_id, seq, started_at, state, stdout_path) VALUES ('open', 5, %s, 'running', '/new/stdout')", (MOMENT,))
                    other._execute("INSERT INTO po_requests (request_id, operation, fingerprint, session_id, seq, created_at) VALUES ('next-send', 'po_send', %s, 'open', 5, %s)", ("a" * 64, MOMENT))
                    other._execute("INSERT INTO po_feed (entry_id, session_id, turn_seq, role, text, created_at) OVERRIDING SYSTEM VALUE VALUES (999, 'open', 5, 'owner', 'accepted after cut', %s)", (MOMENT,))
            return rows

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(self.store, "_query", side_effect=read_then_accept):
            root = Path(tmp)
            export_board(root, instance_dir=root, reader=TaskExportReader(client=self.store), sprint_client=self.store)
            self.assertEqual(read_checkpoint_po(root / "board"), records)
        self.assertTrue(changed)
        self.assertEqual(other._query("SELECT seq FROM po_requests WHERE request_id = 'next-send'"), [(5,)])

    def test_invalid_po_capture_preserves_previous_export_bytes_and_safe_diagnostic(self):
        seed_po(self.store, po_records())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reader = TaskExportReader(client=self.store)
            export_board(root, instance_dir=root, reader=reader, sprint_client=self.store)
            before = {path.name: path.read_bytes() for path in (root / "board").iterdir() if path.is_file()}
            with self.store.transaction():
                self.store._execute("UPDATE po_requests SET fingerprint = 'sensitive-invalid-binding' WHERE request_id = 'send'")
            with self.assertRaisesRegex(RuntimeError, "po_requests record 1: fingerprint") as failure:
                export_board(root, instance_dir=root, reader=reader, sprint_client=self.store)
            self.assertNotIn("sensitive-invalid-binding", str(failure.exception))
            after = {path.name: path.read_bytes() for path in (root / "board").iterdir() if path.is_file()}
            self.assertEqual(before, after)

    def test_board_cut_captures_po_notes_bytes_or_absence_and_an_unsafe_source_keeps_the_previous_cut(self):
        seed_po(self.store, po_records())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            reader = TaskExportReader(client=self.store)
            notes = workspace_dir(root) / NOTES_FILE
            notes.parent.mkdir()
            for content in (None, b"", b"# Decisions\r\nkeep \xd1\x8f\rno newline", None):
                notes.unlink(missing_ok=True)
                if content is not None:
                    notes.write_bytes(content)
                export_board(root, instance_dir=root, reader=reader, sprint_client=self.store)
                self.assertEqual(read_checkpoint_notes(root / "board"), NotesCapture(content))
                self.assertEqual((root / "board" / NOTES_CHECKPOINT_FILE).exists(), content is not None)
            notes.write_bytes(b"current notes")
            export_board(root, instance_dir=root, reader=reader, sprint_client=self.store)
            before = {path.name: path.read_bytes() for path in (root / "board").iterdir() if path.is_file()}
            outside = root / "outside.md"
            outside.write_bytes(b"private outside notes")
            notes.unlink()
            notes.symlink_to(outside)
            with self.assertRaisesRegex(RuntimeError, "PO notes refuse po/NOTES.md") as failure:
                export_board(root, instance_dir=root, reader=reader, sprint_client=self.store)
            self.assertNotIn("private", str(failure.exception))
            after = {path.name: path.read_bytes() for path in (root / "board").iterdir() if path.is_file()}
            self.assertEqual(before, after)

    def test_a_claim_and_dequeue_at_the_boundary_wait_for_the_coherent_po_queue_cut(self):
        """A claim committed by another connection after the cut began is neither in the rows nor
        dequeued in the capture; the next cut holds the claim and no longer the input."""
        seed_po(self.store, po_records())
        other = SqlCardClient(self.store.credentials, self.store.instance_dir)
        self.addCleanup(other.close)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            queue = PoQueue(root)
            queue.put(session_id="open", text="claimed before a crash", request_id="send", source="web")
            waiting = queue.put(session_id="empty", text="next", request_id="next-send", source="web")
            admission = PoRecoveryAdmission(root, mock.Mock())

            def claim_then_dequeue():
                with admission.mutation(), other.transaction():
                    other._execute("INSERT INTO po_turns (session_id, seq, started_at, state, stdout_path) "
                                   "VALUES ('empty', 1, %s, 'running', '/new/stdout')", (MOMENT,))
                    other._execute("INSERT INTO po_requests (request_id, operation, fingerprint, session_id, seq, "
                                   "created_at) VALUES ('next-send', 'po_send', %s, 'empty', 1, %s)", ("a" * 64, MOMENT))
                queue.remove(waiting)

            writer = threading.Thread(target=claim_then_dequeue)
            arrived = threading.Event()
            real_lock = recovery_admission.directory_lock

            def lock(path):
                if threading.current_thread() is writer:
                    arrived.set()
                return real_lock(path)

            original = self.store._query

            def start_writer_in_window(query, *args, **kwargs):
                if "FROM po_sessions" in query and not arrived.is_set():
                    writer.start()
                    self.assertTrue(arrived.wait(30))
                return original(query, *args, **kwargs)

            reader = TaskExportReader(client=self.store)
            with (
                mock.patch.object(recovery_admission, "directory_lock", side_effect=lock),
                mock.patch.object(self.store, "_query", side_effect=start_writer_in_window),
            ):
                export_board(root, instance_dir=root, reader=reader, sprint_client=self.store)
            writer.join(30)
            self.assertFalse(writer.is_alive())
            for expected_inputs, claimed in ((["send", "next-send"], False), (["send"], True)):
                captured = read_checkpoint_queue(root / "board")
                requests = {row["request_id"] for row in read_checkpoint_po(root / "board")["po_requests"]}
                summary = json.loads((root / "board" / "export.json").read_text())
                self.assertEqual([item.document["request_id"] for item in captured.capture.inputs], expected_inputs)
                self.assertIn("send", requests)
                self.assertEqual("next-send" in requests, claimed)
                self.assertEqual(captured.po_sql_sha256, summary["po_sql"]["sha256"])
                export_board(root, instance_dir=root, reader=reader, sprint_client=self.store)

    def test_board_cut_preserves_all_bell_rows_with_stable_tie_order(self):
        rows = bell_rows(513)
        store = OwnerEventStore(self.store.credentials, client=self.store)
        with self.store.transaction():
            store.restore(rows)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reader = TaskExportReader(client=self.store)
            export_board(root, instance_dir=root, reader=reader, sprint_client=self.store)
            require_owner_parity(rows, read_checkpoint_owner_events(root / "board"))
            first = (root / "board" / "owner_events.ndjson").read_bytes()
            export_board(root, instance_dir=root, reader=reader, sprint_client=self.store)
            self.assertEqual(first, (root / "board" / "owner_events.ndjson").read_bytes())

    def test_owner_mark_read_between_card_and_bell_reads_is_outside_the_coherent_cut(self):
        rows = bell_rows(1)
        rows[0].update(kind="sprint_closed", **{"class": "notice"})
        store = OwnerEventStore(self.store.credentials, client=self.store)
        with self.store.transaction():
            store.restore(rows)
        other = SqlCardClient(self.store.credentials, self.store.instance_dir)
        self.addCleanup(other.close)
        reader = TaskExportReader(client=self.store)
        original = reader.export

        def read_then_mark():
            cards = original()
            other_store = OwnerEventStore(other.credentials, client=other)
            other_store.mark_read(other_store.events()[0].id)
            return cards

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(reader, "export", side_effect=read_then_mark),
        ):
            root = Path(tmp)
            export_board(root, instance_dir=root, reader=reader, sprint_client=self.store)
            require_owner_parity(rows, read_checkpoint_owner_events(root / "board"))
        self.assertEqual(store.unread_count(), 0)

    def test_close_and_reopen_between_card_and_sprint_reads_stay_outside_the_cut(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = _write_project_registry(root, "ummanu", "secretary-instance", "other")
            client = sprint_store(self, instance_dir=instance)
            writer = SprintWriter(client, data_dir=root, instance=instance)
            reference = writer.create(
                role="po", actor="po", goal="Coherent close facts", repositories=[str(root / "repo")],
                product="ummanu", issues=["issue:open"], projects=["ummanu"], observer=none_choice(),
                request_id="create-close-cut",
            )["sprint"]["ref"]
            other = SqlCardClient(client.credentials, instance)
            self.addCleanup(other.close)
            reader = TaskReader(client)
            original = reader.export
            expected = client.sprints.export_close_facts()

            def read_then_close():
                cards = original()
                concurrent = SprintWriter(other, data_dir=root, instance=instance)
                concurrent.close(
                    role="po", actor="po", reference=reference, request_id="close-during-cut",
                    decisions={"issues": [{"ref": "issue:open", "verdict": "open", "reason": "retained"}], "cards": []},
                    reason="closed after cut",
                )
                concurrent.reopen(
                    role="po", actor="po", reference=reference, observer=none_choice(),
                    request_id="reopen-during-cut",
                )
                return cards

            with mock.patch.object(reader, "export", side_effect=read_then_close):
                export_board(root, instance_dir=instance, reader=reader, sprint_client=client)
            require_close_set_parity(expected, read_checkpoint_close_facts(root / "board"))
            current = other.sprints.export_close_facts()
            self.assertEqual(current[reference]["close_reason"], "closed after cut")
            self.assertEqual(len(current[reference]["decisions"]), 1)
            export_board(root, instance_dir=instance, reader=reader, sprint_client=client)
            require_close_set_parity(current, read_checkpoint_close_facts(root / "board"))

    def test_owner_answer_between_card_and_bell_reads_cannot_tear_attention(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = _write_project_registry(root, "ummanu", "secretary-instance", "other")
            client = sprint_store(self, instance_dir=instance)
            sprint = SprintWriter(client, data_dir=root, instance=instance).create(
                role="po",
                actor="po",
                goal="Preserve pending owner attention",
                repositories=[str(root / "project-repos" / "ummanu")],
                product="ummanu",
                issues=["issue:open"],
                projects=["ummanu"],
                observer=none_choice(),
                request_id="create-owner-sprint",
            )["sprint"]["ref"]
            writer = TaskWriter(client, data_dir=root)
            writer.create(
                role="po",
                actor="po",
                project="ummanu",
                task_type="decision",
                title="Owner decision",
                target="ready",
                sprint=sprint,
                reference="ummanu-9",
                request_id="create-owner-cut",
            )
            writer.claim(
                role="dispatcher",
                actor="test",
                reference="ummanu-9",
                worker="fixture",
                request_id="claim-owner-cut",
            )
            handover = writer.handover(
                role="po",
                actor="po",
                reference="ummanu-9",
                to="owner",
                reason="Choose an option",
                request_id="handover-owner-cut",
            )
            reader = TaskReader(client)
            original = reader.export
            before = OwnerEventStore(client.credentials, client=client).export()
            other = SqlCardClient(client.credentials, instance)
            self.addCleanup(other.close)

            def read_then_answer():
                cards = original()
                TaskWriter(other, data_dir=root).record_owner_answer(
                    role="po",
                    actor="po",
                    reference="ummanu-9",
                    handover_event=handover["event_id"],
                    quotation="Choose option A",
                    request_id="answer-during-cut",
                )
                return cards

            with mock.patch.object(reader, "export", side_effect=read_then_answer):
                export_board(root, instance_dir=instance, reader=reader, sprint_client=client)
            require_owner_parity(before, read_checkpoint_owner_events(root / "board"))
            cards = json.loads((root / "board" / "cards.json").read_text())["cards"]
            captured = next(card for card in cards if card["reference"] == "ummanu-9")
            self.assertTrue(captured["metadata"]["waiting_owner"])
            self.assertTrue(
                all(
                    row["read_at"] is not None
                    for row in OwnerEventStore(other.credentials, client=other).export()
                )
            )

    def test_invalid_owner_export_keeps_the_previous_good_live_cut(self):
        rows = bell_rows()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reader = TaskExportReader(client=self.store)
            export_board(root, instance_dir=root, reader=reader, sprint_client=self.store)
            before = {path.name: path.read_bytes() for path in (root / "board").iterdir() if path.is_file()}
            rows[0]["class"] = "notice"
            with (
                mock.patch.object(OwnerEventStore, "export", return_value=rows),
                self.assertRaisesRegex(RuntimeError, "class"),
            ):
                export_board(root, instance_dir=root, reader=reader, sprint_client=self.store)
            after = {path.name: path.read_bytes() for path in (root / "board").iterdir() if path.is_file()}
            self.assertEqual(before, after)

    def _seed_obligations(self, count):
        rows, history = obligations(count)
        with self.store.transaction():
            for event in history:
                SqlTaskAudit(self.store).append(event["request_id"], event, restoring=True)
            OriginOutboxStore(self.store).restore(rows)
        return rows

    def test_board_cut_exports_more_than_a_dispatch_tick_including_completed_returns(self):
        rows = self._seed_obligations(503)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reader = TaskExportReader(client=self.store)
            export_board(root, instance_dir=root, reader=reader, sprint_client=self.store)
            require_outbox_parity(rows, read_checkpoint_outbox(root / "board"))
            first = (root / "board" / "origin_returns.ndjson").read_bytes()
            export_board(root, instance_dir=root, reader=reader, sprint_client=self.store)
            self.assertEqual(first, (root / "board" / "origin_returns.ndjson").read_bytes())

    def test_delivery_between_card_and_outbox_reads_stays_outside_the_board_cut(self):
        rows = self._seed_obligations(3)
        other = SqlCardClient(self.store.credentials, self.store.instance_dir)
        self.addCleanup(other.close)
        reader = TaskExportReader(client=self.store)
        original = reader.export

        def read_then_deliver():
            cards = original()
            pending = OriginOutboxStore(other).pending()[0]
            self.assertTrue(OriginOutboxStore(other).mark(
                pending.id, status="delivered", session="new-session", po_request_id="new-delivery",
            ))
            return cards

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(reader, "export", side_effect=read_then_deliver):
            root = Path(tmp)
            export_board(root, instance_dir=root, reader=reader, sprint_client=self.store)
            require_outbox_parity(rows, read_checkpoint_outbox(root / "board"))
        self.assertEqual(OriginOutboxStore(other).pending(), [])

    def test_invalid_outbox_export_preserves_the_previous_cut(self):
        self._seed_obligations(3)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reader = TaskExportReader(client=self.store)
            export_board(root, instance_dir=root, reader=reader, sprint_client=self.store)
            before = {path.name: path.read_bytes() for path in (root / "board").iterdir() if path.is_file()}
            with self.store.transaction():
                self.store._execute("UPDATE origin_returns SET request_id = 'foreign-audit'")
            with self.assertRaisesRegex(RuntimeError, "audit.request_id"):
                export_board(root, instance_dir=root, reader=reader, sprint_client=self.store)
            after = {path.name: path.read_bytes() for path in (root / "board").iterdir() if path.is_file()}
            self.assertEqual(before, after)

    def test_normalize_board_card_keeps_required_surface(self):
        card = {
            "id": "7",
            "reference": "ummanu-353",
            "title": "Export data",
            "column": "In progress",
            "swimlane": "ummanu",
            "position": "2",
            "date_moved": "1783635890",
            "task_type": "code",
            "project": "ummanu",
        }
        shown = {
            "id": 7,
            "reference": "ummanu-353",
            "title": "Export data",
            "description": "body",
            "column": "In progress",
            "metadata": {"project": "ummanu", "task_type": "code"},
            "comments": [{"ts": "1", "text": "[worker]\nok"}],
        }

        normalized = normalize_board_card(card, shown)

        self.assertEqual(normalized["reference"], "ummanu-353")
        self.assertEqual(normalized["swimlane"], "ummanu")
        self.assertEqual(normalized["column"], "In progress")
        self.assertEqual(normalized["metadata"], {"project": "ummanu", "task_type": "code"})
        self.assertEqual(normalized["comments"], [{"ts": "1", "text": "[worker]\nok"}])

    def test_export_board_writes_normalized_cards_and_is_idempotent(self):
        reader = TaskExportReader(
            client=self.store,
            cards=[
                {
                    "id": 1,
                    "reference": "ummanu-353",
                    "title": "Export",
                    "description": "spec",
                    "column": "Ready",
                    "swimlane": "ummanu",
                    "position": 1,
                    "metadata": {"project": "ummanu"},
                    "comments": [{"ts": "10", "text": "[po]\nbody"}],
                }
            ]
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "ummanu-data"
            first = export_board(
                data_dir, instance_dir=Path(tmpdir), reader=reader, sprint_client=self.store
            )
            first_payload = (data_dir / "board" / "cards.json").read_text(encoding="utf-8")
            second = export_board(
                data_dir, instance_dir=Path(tmpdir), reader=reader, sprint_client=self.store
            )
            second_payload = (data_dir / "board" / "cards.json").read_text(encoding="utf-8")

        self.assertEqual(first.count, 1)
        self.assertEqual(second.count, 1)
        self.assertEqual(first_payload, second_payload)
        self.assertEqual(reader.calls, 2)

    def test_export_board_uses_the_reader_not_a_subprocess_or_pipeline_path(self):
        reader = TaskExportReader(client=self.store)
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            mock.patch("subprocess.run", side_effect=AssertionError("no subprocess")),
        ):
            result = export_board(
                Path(tmpdir) / "ummanu-data",
                instance_dir=Path(tmpdir),
                reader=reader,  # type: ignore[arg-type]
                sprint_client=self.store,
            )

        self.assertEqual(result.source, "ummanu task")
        self.assertEqual(reader.calls, 1)

    def test_task_reader_failure_preserves_the_previous_board_snapshot(self):
        reader = TaskExportReader(client=self.store)
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "ummanu-data"
            export_board(
                data_dir,
                instance_dir=Path(tmpdir),
                reader=reader,  # type: ignore[arg-type]
                sprint_client=self.store,
            )
            previous = (data_dir / "board" / "cards.json").read_text(encoding="utf-8")
            failed_reader = mock.Mock(client=self.store)
            failed_reader.export.side_effect = TaskError("backend_error", "backend refused export", 1)

            with self.assertRaisesRegex(RuntimeError, "ummanu task export failed: backend refused export"):
                export_board(
                    data_dir,
                    instance_dir=Path(tmpdir),
                    reader=failed_reader,  # type: ignore[arg-type]
                    sprint_client=self.store,
                )

            current = (data_dir / "board" / "cards.json").read_text(encoding="utf-8")

        self.assertEqual(current, previous)

    def test_export_board_writes_an_empty_sprint_set_without_a_sprint_board(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "ummanu-data"
            export_board(
                data_dir, instance_dir=Path(tmpdir), reader=TaskExportReader(client=self.store), sprint_client=self.store
            )
            board = data_dir / "board"
            sprints = json.loads((board / "sprints.json").read_text(encoding="utf-8"))
            ndjson = (board / "sprints.ndjson").read_text(encoding="utf-8")
            summary = json.loads((board / "export.json").read_text(encoding="utf-8"))

        # The sprints board may not exist yet: the export must write an empty set rather than
        # silently skipping the component, or the checkpoint cannot tell that apart from loss.
        self.assertEqual(sprints, {"version": 1, "sprints": []})
        self.assertEqual(ndjson, "")
        self.assertEqual(summary["sprint_count"], 0)

    def test_export_board_fails_when_the_sprint_read_fails(self):
        original = self.store.call

        def broken(method, **params):
            if method == "getProjectByName" and params.get("name") == SPRINT_BOARD_NAME:
                raise TaskError("backend_error", "sprint board is unreachable", 1)
            return original(method, **params)

        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "ummanu-data"
            with (
                mock.patch.object(self.store, "call", side_effect=broken),
                self.assertRaisesRegex(RuntimeError, "sprint board is unreachable"),
            ):
                export_board(
                    data_dir,
                    instance_dir=Path(tmpdir),
                    reader=TaskExportReader(client=self.store),
                    sprint_client=self.store,
                )
            published = sorted(path.name for path in (data_dir / "board").iterdir())

        # Unreachable sprints must not produce a partial snapshot of cards alone.
        self.assertEqual(published, [])

    def test_export_board_preserves_previous_snapshot_on_publish_error(self):
        cards_by_run = [
            [{"id": 1, "reference": "ummanu-1", "title": "One"}],
            [
                {"id": 1, "reference": "ummanu-1", "title": "One"},
                {"id": 2, "reference": "ummanu-2", "title": "Two"},
            ],
        ]
        reader = TaskExportReader(cards_by_run[0], client=self.store)

        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "ummanu-data"
            board_dir = data_dir / "board"

            export_board(data_dir, instance_dir=Path(tmpdir), reader=reader, sprint_client=self.store)
            old_cards = (board_dir / "cards.json").read_text(encoding="utf-8")
            old_ndjson = (board_dir / "cards.ndjson").read_text(encoding="utf-8")
            old_summary = (board_dir / "export.json").read_text(encoding="utf-8")

            original_replace = os.replace
            failed = False
            reader.cards = cards_by_run[1]

            def fail_on_ndjson_publish(source, destination):
                nonlocal failed
                if not failed and Path(destination) == board_dir / "cards.ndjson":
                    failed = True
                    raise OSError("full")
                return original_replace(source, destination)

            with (
                mock.patch("ummanu.data.os.replace", side_effect=fail_on_ndjson_publish),
                self.assertRaisesRegex(RuntimeError, "could not publish board export"),
            ):
                export_board(
                    data_dir, instance_dir=Path(tmpdir), reader=reader, sprint_client=self.store
                )

            current_cards = (board_dir / "cards.json").read_text(encoding="utf-8")
            current_ndjson = (board_dir / "cards.ndjson").read_text(encoding="utf-8")
            current_summary = (board_dir / "export.json").read_text(encoding="utf-8")

        self.assertEqual(current_cards, old_cards)
        self.assertEqual(current_ndjson, old_ndjson)
        self.assertEqual(current_summary, old_summary)

    def test_export_memory_writes_ndjson_from_the_instance_repo(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance_dir = init_instance_repo(root / "instance")
            source = memory_facts_dir(instance_dir)
            (source / "ummanu").mkdir(parents=True)
            (source / "ummanu" / "one.md").write_text(
                "---\ntags: [ummanu, memory]\nsource: test\ncreated: 2026-07-11\n---\nfact one\n",
                encoding="utf-8",
            )
            (source / "global").mkdir()
            (source / "global" / "two.md").write_text("fact two\n", encoding="utf-8")
            git(instance_dir, "add", "-A", ".")
            git(instance_dir, "commit", "-m", "facts")
            data_dir = root / "ummanu-data"

            first = export_memory(data_dir, instance_dir)
            first_payload = (data_dir / "memory" / "export.ndjson").read_text(encoding="utf-8")
            second = export_memory(data_dir, instance_dir)
            facts_dir_exists = (data_dir / "memory" / "facts").exists()
            manifest = json.loads((data_dir / "memory" / "manifest.json").read_text(encoding="utf-8"))
            revision = canon_revision(source)

        self.assertEqual(first.count, 2)
        self.assertEqual(second.count, 2)
        self.assertIn("ummanu/one.md", first_payload)
        self.assertIn('"metadata": {"created": "2026-07-11"', first_payload)
        self.assertFalse(facts_dir_exists)
        # The manifest names the content revision of the exported facts, not a Git commit.
        self.assertEqual(manifest["source"]["head"], revision)
        self.assertEqual(manifest["journal"]["commit"], revision)
        self.assertTrue(manifest["source"]["readonly_fallback"])

    def test_export_memory_ndjson_comes_from_readonly_snapshot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance_dir = init_instance_repo(root / "instance")
            fact = memory_facts_dir(instance_dir) / "ummanu" / "one.md"
            fact.parent.mkdir(parents=True)
            fact.write_text("fact one\n", encoding="utf-8")
            original_copy_tree = memory_journal._copy_tree

            def copy_then_mutate(source_memory, snapshot):
                original_copy_tree(source_memory, snapshot)
                fact.write_text("changed after snapshot\n", encoding="utf-8")

            with mock.patch("ummanu.memory_journal._copy_tree", side_effect=copy_then_mutate):
                export_memory(root / "ummanu-data", instance_dir)
            exported = (root / "ummanu-data" / "memory" / "export.ndjson").read_text(encoding="utf-8")

        self.assertIn("fact one", exported)
        self.assertNotIn("changed after snapshot", exported)

    def test_export_memory_skips_symlinked_facts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance_dir = init_instance_repo(root / "instance")
            secret = root / "secret.env"
            secret.write_text("TOKEN=do-not-export\n", encoding="utf-8")
            facts = memory_facts_dir(instance_dir) / "ummanu"
            facts.mkdir(parents=True)
            (facts / "one.md").write_text("fact one\n", encoding="utf-8")
            (facts / "secret.md").symlink_to(secret)

            result = export_memory(root / "ummanu-data", instance_dir)
            exported = (root / "ummanu-data" / "memory" / "export.ndjson").read_text(encoding="utf-8")
            mirrored_secret = root / "ummanu-data" / "memory" / "facts" / "ummanu" / "secret.md"

        self.assertEqual(result.count, 1)
        self.assertIn("fact one", exported)
        self.assertNotIn("TOKEN=do-not-export", exported)
        self.assertFalse(mirrored_secret.exists())

    def test_export_memory_wraps_decode_errors(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance_dir = init_instance_repo(root / "instance")
            fact = memory_facts_dir(instance_dir) / "ummanu" / "bad.md"
            fact.parent.mkdir(parents=True)
            fact.write_bytes(b"\xff\xfe")

            with self.assertRaisesRegex(RuntimeError, "could not decode memory fact"):
                export_memory(root / "ummanu-data", instance_dir)

    def test_export_memory_preserves_previous_snapshot_on_decode_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance_dir = init_instance_repo(root / "instance")
            facts = memory_facts_dir(instance_dir) / "ummanu"
            facts.mkdir(parents=True)
            (facts / "good.md").write_text("good fact\n", encoding="utf-8")
            data_dir = root / "ummanu-data"
            export_memory(data_dir, instance_dir)
            old_export = (data_dir / "memory" / "export.ndjson").read_text(encoding="utf-8")

            (facts / "bad.md").write_bytes(b"\xff\xfe")

            with self.assertRaisesRegex(RuntimeError, "could not decode memory fact"):
                export_memory(data_dir, instance_dir)

            current_export = (data_dir / "memory" / "export.ndjson").read_text(encoding="utf-8")
            facts_dir_exists = (data_dir / "memory" / "facts").exists()

        self.assertEqual(current_export, old_export)
        self.assertFalse(facts_dir_exists)

    def test_memory_protocol_commit_writes_the_fact_without_a_git_commit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = root / "ummanu-data"
            instance_dir = init_instance_repo(root / "instance")
            fact = root / "fact.md"
            fact.write_text("new durable fact\n", encoding="utf-8")

            proposal = propose_memory_fact(
                data_dir,
                actor="curator:claude/session",
                scope="project:ummanu",
                slug="new-fact",
                fact_file=fact,
                source="curator:claude/session",
                tags=["ummanu", "memory"],
            )
            result = commit_memory_proposal(
                data_dir,
                instance_dir,
                actor="curator:claude/session",
                propose_id=proposal.propose_id,
            )
            log_count = memory_commit_count(instance_dir)
            status = memory_status(instance_dir)
            fact_text = (memory_facts_dir(instance_dir) / "ummanu" / "new-fact.md").read_text(encoding="utf-8")
            revision = canon_revision(memory_facts_dir(instance_dir))
            exported = (data_dir / "memory" / "export.ndjson").read_text(encoding="utf-8")

        self.assertEqual(result.fact, "ummanu/new-fact")
        # The writer is Git-free: the fact is a file in the canon, committed later by the tick.
        self.assertEqual(log_count, "0")
        self.assertTrue(status.startswith("?? state/"), status)
        self.assertIn("source: curator:claude/session", fact_text)
        self.assertIn("new durable fact", fact_text)
        self.assertEqual(result.commit, revision)
        self.assertIn("new durable fact", exported)

    def test_memory_verify_checks_export_and_index_parity(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = root / "ummanu-data"
            fact = root / "fact.md"
            fact.write_text("verified durable fact\n", encoding="utf-8")
            instance_dir = init_instance_repo(root / "instance")
            proposal = propose_memory_fact(
                data_dir,
                actor="curator:claude/session",
                scope="project:ummanu",
                slug="verified",
                fact_file=fact,
                source="curator:claude/session",
            )
            commit_memory_proposal(
                data_dir,
                instance_dir,
                actor="curator:claude/session",
                propose_id=proposal.propose_id,
            )
            write_index_from_canon(data_dir / "memory" / "index.sqlite", memory_facts_dir(instance_dir))

            result = verify_memory_journal(data_dir, instance_dir)

        self.assertTrue(result.ok, result.findings)
        self.assertEqual(result.fact_count, 1)
        self.assertEqual(result.export_count, 1)
        self.assertEqual(result.index_count, 1)

    def test_memory_protocol_export_failure_after_commit_is_retryable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = root / "ummanu-data"
            fact = root / "fact.md"
            fact.write_text("retryable fact\n", encoding="utf-8")
            instance_dir = init_instance_repo(root / "instance")
            proposal = propose_memory_fact(
                data_dir,
                actor="curator:claude/session",
                scope="project:ummanu",
                slug="retryable",
                fact_file=fact,
                source="curator:claude/session",
            )

            with (
                mock.patch(
                    "ummanu.memory_write._publish_memory_export",
                    side_effect=RuntimeError("disk full"),
                ),
                self.assertRaises(MemoryExportPublishError) as raised,
            ):
                commit_memory_proposal(
                    data_dir,
                    instance_dir,
                    actor="curator:claude/session",
                    propose_id=proposal.propose_id,
                )

            failed_result = raised.exception.result
            after_failure_revision = canon_revision(memory_facts_dir(instance_dir))
            log_count_after_failure = memory_commit_count(instance_dir)
            completed_marker = data_dir / "memory" / ".staging" / proposal.propose_id / "committed.json"
            completed_exists_after_failure = completed_marker.is_file()
            export_exists_after_failure = (data_dir / "memory" / "export.ndjson").exists()

            retry_result = commit_memory_proposal(
                data_dir,
                instance_dir,
                actor="curator:claude/session",
                propose_id=proposal.propose_id,
            )
            retry_revision = canon_revision(memory_facts_dir(instance_dir))
            retry_log_count = memory_commit_count(instance_dir)
            exported = (data_dir / "memory" / "export.ndjson").read_text(encoding="utf-8")
            staging_exists_after_retry = completed_marker.parent.exists()

        self.assertEqual(failed_result.commit, after_failure_revision)
        self.assertEqual(failed_result.fact, "ummanu/retryable")
        self.assertEqual(log_count_after_failure, "0")
        self.assertTrue(completed_exists_after_failure)
        self.assertFalse(export_exists_after_failure)
        self.assertEqual(retry_result.commit, after_failure_revision)
        self.assertEqual(retry_revision, after_failure_revision)
        self.assertEqual(retry_log_count, "0")
        self.assertIn("retryable fact", exported)
        self.assertFalse(staging_exists_after_retry)

    def test_memory_protocol_errors_share_base_class(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = root / "ummanu-data"
            fact = root / "fact.md"
            fact.write_text("fact\n", encoding="utf-8")
            memory_dir = data_dir / "memory"
            memory_dir.mkdir(parents=True)
            (memory_dir / memory_journal.MEMORY_LOCK_NAME).write_text(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "host": memory_journal.socket.gethostname(),
                        "created_at": 1,
                    }
                ),
                encoding="utf-8",
            )

            errors: list[MemoryProtocolError] = []
            for actor, scope, source in (
                ("curator:claude/session", "bad", "curator:claude/session"),
                ("worker:codex/session", "project:ummanu", "worker:codex/session"),
                ("curator:claude/session", "project:ummanu", "curator:claude/session"),
            ):
                try:
                    propose_memory_fact(
                        data_dir,
                        actor=actor,
                        scope=scope,
                        slug="new-fact",
                        fact_file=fact,
                        source=source,
                    )
                except MemoryProtocolError as exc:
                    errors.append(exc)

        self.assertIsInstance(errors[0], MemoryValidationError)
        self.assertIsInstance(errors[1], MemoryPermissionError)
        self.assertIsInstance(errors[2], MemoryLockError)
        self.assertTrue(all(isinstance(error, MemoryProtocolError) for error in errors))

    def test_memory_protocol_gc_removes_only_stale_uncommitted_proposals(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = root / "ummanu-data"
            fact = root / "fact.md"
            fact.write_text("fact\n", encoding="utf-8")
            stale = propose_memory_fact(
                data_dir,
                actor="curator:claude/session",
                scope="project:ummanu",
                slug="stale",
                fact_file=fact,
                source="curator:claude/session",
            )
            fresh = propose_memory_fact(
                data_dir,
                actor="curator:claude/session",
                scope="project:ummanu",
                slug="fresh",
                fact_file=fact,
                source="curator:claude/session",
            )
            active = propose_memory_fact(
                data_dir,
                actor="curator:claude/session",
                scope="project:ummanu",
                slug="active",
                fact_file=fact,
                source="curator:claude/session",
            )
            for proposal in (stale, active):
                proposal_path = proposal.path / "proposal.json"
                payload = json.loads(proposal_path.read_text(encoding="utf-8"))
                payload["created_at"] = 1
                proposal_path.write_text(json.dumps(payload), encoding="utf-8")
            (active.path / MEMORY_PROPOSAL_ACTIVE_MARKER).write_text("{}", encoding="utf-8")

            # The GC has no standalone entry point: it runs inside the journal lock on the
            # write path, so the test drives the same function that path calls.
            result = _gc_staging_proposals(
                (data_dir / "memory").expanduser().resolve(),
                now=int(time.time()),
                max_age_seconds=60,
                active_grace_seconds=3600,
            )
            fresh_exists = fresh.path.is_dir()
            active_exists = active.path.is_dir()
            stale_exists = stale.path.exists()

        self.assertEqual(result.removed, (stale.propose_id,))
        self.assertTrue(fresh_exists)
        self.assertTrue(active_exists)
        self.assertFalse(stale_exists)

    def test_memory_protocol_rejects_invalid_input_and_permissions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            fact = root / "fact.md"
            fact.write_text("fact\n", encoding="utf-8")

            with self.assertRaises(MemoryValidationError):
                propose_memory_fact(
                    root / "ummanu-data",
                    actor="curator:claude/session",
                    scope="bad",
                    slug="new-fact",
                    fact_file=fact,
                    source="curator:claude/session",
                )
            with self.assertRaises(MemoryPermissionError):
                propose_memory_fact(
                    root / "ummanu-data",
                    actor="worker:codex/session",
                    scope="project:ummanu",
                    slug="new-fact",
                    fact_file=fact,
                    source="worker:codex/session",
                )

    def test_memory_protocol_publishes_po_review_bucket_without_project_aliasing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = root / "ummanu-data"
            fact = root / "fact.md"
            fact.write_text(
                "---\ntags: pending-review,multi-project\n---\nNeeds PO ownership review.\n",
                encoding="utf-8",
            )
            instance_dir = init_instance_repo(root / "instance")
            proposal = propose_memory_fact(
                data_dir,
                actor="curator:claude/session",
                scope="review:po",
                slug="ambiguous-conclusion",
                fact_file=fact,
                source="curator:claude/session",
            )

            result = commit_memory_proposal(
                data_dir,
                instance_dir,
                actor="curator:claude/session",
                propose_id=proposal.propose_id,
            )
            facts = canon_facts(instance_dir)

        self.assertEqual(proposal.scope_dir, "po-review")
        self.assertEqual(result.fact, "po-review/ambiguous-conclusion")
        self.assertEqual(facts, ["po-review/ambiguous-conclusion.md"])

    def test_memory_protocol_supersede_unknown_fact_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = root / "ummanu-data"
            fact = root / "fact.md"
            fact.write_text("replacement\n", encoding="utf-8")
            instance_dir = init_instance_repo(root / "instance")

            with self.assertRaisesRegex(MemoryValidationError, "not found"):
                supersede_memory_fact(
                    data_dir,
                    instance_dir,
                    actor="curator:claude/session",
                    scope="project:ummanu",
                    slug="replacement",
                    fact_file=fact,
                    supersedes=["missing"],
                    source="curator:claude/session",
                )
            status = memory_status(instance_dir)

        self.assertEqual(status, "")

    def test_memory_protocol_supersede_removes_old_fact_in_one_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = root / "ummanu-data"
            old_fact = root / "old.md"
            old_fact.write_text("old fact\n", encoding="utf-8")
            instance_dir = init_instance_repo(root / "instance")
            proposal = propose_memory_fact(
                data_dir,
                actor="curator:claude/session",
                scope="project:ummanu",
                slug="old",
                fact_file=old_fact,
                source="curator:claude/session",
            )
            commit_memory_proposal(
                data_dir,
                instance_dir,
                actor="curator:claude/session",
                propose_id=proposal.propose_id,
            )
            new_fact = root / "new.md"
            new_fact.write_text("current fact\n", encoding="utf-8")

            result = supersede_memory_fact(
                data_dir,
                instance_dir,
                actor="curator:claude/session",
                scope="project:ummanu",
                slug="new",
                fact_file=new_fact,
                supersedes=["old"],
                source="curator:claude/session",
            )
            facts = canon_facts(instance_dir)
            log_count = memory_commit_count(instance_dir)
            revision = canon_revision(memory_facts_dir(instance_dir))

        self.assertEqual(result.changed_facts, ("ummanu/new", "ummanu/old"))
        self.assertEqual(facts, ["ummanu/new.md"])
        self.assertEqual(log_count, "0")
        self.assertEqual(result.commit, revision)

    def test_memory_protocol_live_lock_rejects_concurrent_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = root / "ummanu-data"
            fact = root / "fact.md"
            fact.write_text("fact\n", encoding="utf-8")
            memory_dir = data_dir / "memory"
            memory_dir.mkdir(parents=True)
            (memory_dir / memory_journal.MEMORY_LOCK_NAME).write_text(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "host": memory_journal.socket.gethostname(),
                        "created_at": 1,
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(MemoryLockError):
                propose_memory_fact(
                    data_dir,
                    actor="curator:claude/session",
                    scope="project:ummanu",
                    slug="new-fact",
                    fact_file=fact,
                    source="curator:claude/session",
                )

    def test_memory_protocol_never_touches_files_it_did_not_write(self):
        """The canon is the files: an edit made outside the writer is the canon, not residue.

        Rollback restores only the set a write recorded in its undo area, so the next write leaves
        a hand edit and a new file where they are.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = root / "ummanu-data"
            first_fact = root / "first.md"
            first_fact.write_text("first fact\n", encoding="utf-8")
            instance_dir = init_instance_repo(root / "instance")
            first = propose_memory_fact(
                data_dir,
                actor="curator:claude/session",
                scope="project:ummanu",
                slug="first",
                fact_file=first_fact,
                source="curator:claude/session",
            )
            commit_memory_proposal(
                data_dir,
                instance_dir,
                actor="curator:claude/session",
                propose_id=first.propose_id,
            )
            facts_dir = memory_facts_dir(instance_dir)
            (facts_dir / "ummanu" / "first.md").write_text("dirty edit\n", encoding="utf-8")
            (facts_dir / "ummanu" / "residue.md").write_text("residue\n", encoding="utf-8")
            second_fact = root / "second.md"
            second_fact.write_text("second fact\n", encoding="utf-8")

            second = propose_memory_fact(
                data_dir,
                actor="curator:claude/session",
                scope="project:ummanu",
                slug="second",
                fact_file=second_fact,
                source="curator:claude/session",
            )
            commit_memory_proposal(
                data_dir,
                instance_dir,
                actor="curator:claude/session",
                propose_id=second.propose_id,
            )
            first_text = (facts_dir / "ummanu" / "first.md").read_text(encoding="utf-8")
            facts = canon_facts(instance_dir)

        self.assertEqual(first_text, "dirty edit\n")
        self.assertEqual(facts, ["ummanu/first.md", "ummanu/residue.md", "ummanu/second.md"])

    def test_export_memory_after_protocol_commit_is_readonly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = root / "ummanu-data"
            protocol_fact = root / "protocol.md"
            protocol_fact.write_text("protocol fact\n", encoding="utf-8")
            instance_dir = init_instance_repo(root / "instance")
            proposal = propose_memory_fact(
                data_dir,
                actor="curator:claude/session",
                scope="project:ummanu",
                slug="protocol",
                fact_file=protocol_fact,
                source="curator:claude/session",
            )
            commit_memory_proposal(
                data_dir,
                instance_dir,
                actor="curator:claude/session",
                propose_id=proposal.propose_id,
            )
            before_head = memory_head(instance_dir)
            before_count = memory_commit_count(instance_dir)
            before_status = memory_status(instance_dir)
            before_canon = {
                path: path.read_bytes() for path in memory_facts_dir(instance_dir).rglob("*") if path.is_file()
            }

            result = export_memory(data_dir, instance_dir)
            after_head = memory_head(instance_dir)
            after_count = memory_commit_count(instance_dir)
            exported = (data_dir / "memory" / "export.ndjson").read_text(encoding="utf-8")
            status = memory_status(instance_dir)
            after_canon = {
                path: path.read_bytes() for path in memory_facts_dir(instance_dir).rglob("*") if path.is_file()
            }

        self.assertEqual(result.count, 1)
        self.assertEqual(after_head, before_head)
        self.assertEqual(after_count, before_count)
        self.assertIn("protocol fact", exported)
        # The Git-free writer leaves its fact for the tick to commit; the export changes nothing.
        self.assertEqual(status, before_status)
        self.assertEqual(after_canon, before_canon)

    def test_export_memory_respects_live_journal_lock(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = root / "ummanu-data"
            instance_dir = init_instance_repo(root / "instance")
            memory_dir = data_dir / "memory"
            memory_dir.mkdir(parents=True)
            (memory_dir / memory_journal.MEMORY_LOCK_NAME).write_text(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "host": memory_journal.socket.gethostname(),
                        "created_at": 1,
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(MemoryLockError):
                export_memory(data_dir, instance_dir)

    def test_export_runs_writes_records_watermarks_and_card_mapping(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            state = root / "state"
            (state / "pipeline").mkdir(parents=True)
            (state / "pipeline" / "runs.jsonl").write_text(
                '{"event":"claim","reference":"ummanu-353"}\n',
                encoding="utf-8",
            )
            (state / "pipeline" / "cards.json").write_text(
                '{"ummanu-353":{"worker":"353-system-exports"}}\n',
                encoding="utf-8",
            )

            result = export_runs(root / "ummanu-data", state_dir=state)
            runs = (root / "ummanu-data" / "runs" / "runs.ndjson").read_text(encoding="utf-8")
            watermarks = json.loads(
                (root / "ummanu-data" / "runs" / "watermarks.json").read_text(encoding="utf-8")
            )
            cards = json.loads((root / "ummanu-data" / "runs" / "cards.json").read_text(encoding="utf-8"))

        self.assertEqual(result.count, 1)
        self.assertIn('"event": "claim"', runs)
        self.assertEqual(watermarks["files"][0]["path"], "pipeline/cards.json")
        self.assertIn("ummanu-353", cards["cards"])

    def test_export_runs_of_an_absent_state_dir_publishes_zero_runs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            result = export_runs(root / "ummanu-data", state_dir=root / "absent")
            runs_dir = root / "ummanu-data" / "runs"
            runs = (runs_dir / "runs.ndjson").read_text(encoding="utf-8")
            summary = json.loads((runs_dir / "export.json").read_text(encoding="utf-8"))

        self.assertEqual(result.count, 0)
        self.assertEqual(runs, "")
        self.assertEqual((summary["run_record_count"], summary["card_mapping_count"]), (0, 0))

    def test_export_runs_fails_on_invalid_jsonl(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            state = root / "state"
            (state / "pipeline").mkdir(parents=True)
            (state / "pipeline" / "runs.jsonl").write_text('{"event":', encoding="utf-8")
            (state / "pipeline" / "cards.json").write_text("{}", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "invalid JSONL"):
                export_runs(root / "ummanu-data", state_dir=state)

    def test_export_runs_fails_on_invalid_card_mapping(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            state = root / "state"
            (state / "pipeline").mkdir(parents=True)
            (state / "pipeline" / "runs.jsonl").write_text("{}\n", encoding="utf-8")
            (state / "pipeline" / "cards.json").write_text("{not-json", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "invalid JSON"):
                export_runs(root / "ummanu-data", state_dir=state)

    def test_export_runs_preserves_previous_snapshot_on_publish_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            state = root / "state"
            runs_dir = root / "ummanu-data" / "runs"
            (state / "pipeline").mkdir(parents=True)
            (state / "pipeline" / "runs.jsonl").write_text('{"event":"one"}\n', encoding="utf-8")
            (state / "pipeline" / "cards.json").write_text("{}", encoding="utf-8")

            export_runs(root / "ummanu-data", state_dir=state)
            old_runs = (runs_dir / "runs.ndjson").read_text(encoding="utf-8")
            old_watermarks = (runs_dir / "watermarks.json").read_text(encoding="utf-8")
            old_cards = (runs_dir / "cards.json").read_text(encoding="utf-8")
            old_summary = (runs_dir / "export.json").read_text(encoding="utf-8")

            (state / "pipeline" / "runs.jsonl").write_text(
                '{"event":"one"}\n{"event":"two"}\n',
                encoding="utf-8",
            )
            original_replace = os.replace
            failed = False

            def fail_on_watermarks_publish(source, destination):
                nonlocal failed
                if not failed and Path(destination) == runs_dir / "watermarks.json":
                    failed = True
                    raise OSError("full")
                return original_replace(source, destination)

            with (
                mock.patch("ummanu.data.os.replace", side_effect=fail_on_watermarks_publish),
                self.assertRaisesRegex(RuntimeError, "could not publish runs export"),
            ):
                export_runs(root / "ummanu-data", state_dir=state)

            current_runs = (runs_dir / "runs.ndjson").read_text(encoding="utf-8")
            current_watermarks = (runs_dir / "watermarks.json").read_text(encoding="utf-8")
            current_cards = (runs_dir / "cards.json").read_text(encoding="utf-8")
            current_summary = (runs_dir / "export.json").read_text(encoding="utf-8")

        self.assertEqual(current_runs, old_runs)
        self.assertEqual(current_watermarks, old_watermarks)
        self.assertEqual(current_cards, old_cards)
        self.assertEqual(current_summary, old_summary)

    def test_export_transcripts_inventory_and_optional_copy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            transcripts = root / "claude"
            (transcripts / "project").mkdir(parents=True)
            (transcripts / "project" / "session.jsonl").write_text("{}\n", encoding="utf-8")

            inventory = export_transcripts(root / "ummanu-data", roots=[transcripts])
            no_copy = (root / "ummanu-data" / "transcripts" / "copies").exists()
            copied = export_transcripts(root / "ummanu-data", roots=[transcripts], copy=True)
            copied_dir = (root / "ummanu-data" / "transcripts" / "copies").is_dir()

        self.assertEqual(inventory.count, 1)
        self.assertFalse(no_copy)
        self.assertEqual(copied.count, 1)
        self.assertTrue(copied_dir)

    def test_export_transcripts_skips_symlinks_when_copying(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            transcripts = root / "claude"
            transcripts.mkdir()
            secret = root / "secret.txt"
            secret.write_text("TOKEN=do-not-copy\n", encoding="utf-8")
            (transcripts / "session.jsonl").write_text("{}\n", encoding="utf-8")
            (transcripts / "linked.jsonl").symlink_to(secret)

            result = export_transcripts(root / "ummanu-data", roots=[transcripts], copy=True)
            copy_root = root / "ummanu-data" / "transcripts" / "copies"
            copied_files = [path.name for path in copy_root.rglob("*.jsonl")]
            copied_payload = "\n".join(
                path.read_text(encoding="utf-8") for path in copy_root.rglob("*.jsonl")
            )

        self.assertEqual(result.count, 1)
        self.assertEqual(copied_files, ["session.jsonl"])
        self.assertNotIn("TOKEN=do-not-copy", copied_payload)

    def test_export_transcripts_preserves_previous_snapshot_on_copy_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            transcripts = root / "claude"
            transcripts_dir = root / "ummanu-data" / "transcripts"
            (transcripts / "project").mkdir(parents=True)
            (transcripts / "project" / "session.jsonl").write_text("{}\n", encoding="utf-8")

            export_transcripts(root / "ummanu-data", roots=[transcripts], copy=True)
            old_inventory = (transcripts_dir / "inventory.json").read_text(encoding="utf-8")
            old_ndjson = (transcripts_dir / "inventory.ndjson").read_text(encoding="utf-8")
            old_copies = sorted(
                path.relative_to(transcripts_dir / "copies").as_posix()
                for path in (transcripts_dir / "copies").rglob("*.jsonl")
            )
            (transcripts / "project" / "second.jsonl").write_text("{}\n", encoding="utf-8")

            with (
                mock.patch("ummanu.data.shutil.copy2", side_effect=OSError("full")),
                self.assertRaisesRegex(RuntimeError, "could not copy transcripts"),
            ):
                export_transcripts(root / "ummanu-data", roots=[transcripts], copy=True)

            current_inventory = (transcripts_dir / "inventory.json").read_text(encoding="utf-8")
            current_ndjson = (transcripts_dir / "inventory.ndjson").read_text(encoding="utf-8")
            current_copies = sorted(
                path.relative_to(transcripts_dir / "copies").as_posix()
                for path in (transcripts_dir / "copies").rglob("*.jsonl")
            )

        self.assertEqual(current_inventory, old_inventory)
        self.assertEqual(current_ndjson, old_ndjson)
        self.assertEqual(current_copies, old_copies)

    def test_export_artifacts_inventories_existing_files_and_task_docs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = root / "ummanu-data"
            artifacts_dir = data_dir / "artifacts"
            (artifacts_dir / "report.txt").parent.mkdir(parents=True)
            (artifacts_dir / "report.txt").write_text("report\n", encoding="utf-8")
            (artifacts_dir / ".env").write_text("TOKEN=do-not-copy\n", encoding="utf-8")
            workspaces = root / "workspaces"
            workspace = workspaces / "ummanu" / "354-backup-create-verify"
            workspace.mkdir(parents=True)
            (workspace / "TASK.md").write_text("task\n", encoding="utf-8")
            (workspace / "REVIEW.md").write_text("review\n", encoding="utf-8")
            (workspace / ".env").write_text("TOKEN=do-not-copy\n", encoding="utf-8")

            result = export_artifacts(data_dir, workspaces_root=workspaces)
            second = export_artifacts(data_dir, workspaces_root=workspaces)
            inventory = json.loads((artifacts_dir / "inventory.json").read_text(encoding="utf-8"))
            copied_docs = sorted(
                path.relative_to(artifacts_dir / "task-docs").as_posix()
                for path in (artifacts_dir / "task-docs").rglob("*.md")
            )

        self.assertEqual(result.count, 3)
        self.assertEqual(second.count, 3)
        paths = {entry["relative_path"] for entry in inventory["artifacts"]}
        self.assertIn("report.txt", paths)
        self.assertIn("ummanu/354-backup-create-verify/TASK.md", paths)
        self.assertIn("ummanu/354-backup-create-verify/REVIEW.md", paths)
        self.assertNotIn(".env", paths)
        self.assertEqual(
            copied_docs,
            [
                "ummanu/354-backup-create-verify/REVIEW.md",
                "ummanu/354-backup-create-verify/TASK.md",
            ],
        )

    def test_export_all_passes_copy_transcripts_once(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "ummanu-data"
            instance_dir = Path(tmpdir) / "instance"
            calls = []

            with (
                mock.patch(
                    "ummanu.data.export_board",
                    side_effect=lambda data_dir_arg, *, instance_dir: (
                        calls.append("board")
                        or data_module.DataExport(data_dir_arg / "board.json", 1, "board")
                    ),
                ) as board,
                mock.patch(
                    "ummanu.data.export_memory",
                    side_effect=lambda data_dir_arg, instance_dir_arg: (
                        calls.append("memory")
                        or data_module.DataExport(data_dir_arg / "memory.ndjson", 1, "memory")
                    ),
                ) as memory,
                mock.patch(
                    "ummanu.data.export_runs",
                    side_effect=lambda data_dir_arg: (
                        calls.append("runs")
                        or data_module.DataExport(data_dir_arg / "runs.ndjson", 1, "runs")
                    ),
                ),
                mock.patch(
                    "ummanu.data.export_transcripts",
                    side_effect=lambda data_dir_arg, *, copy: (
                        calls.append("transcripts")
                        or data_module.DataExport(
                            data_dir_arg / "inventory.json",
                            1,
                            "transcripts",
                        )
                    ),
                ) as transcripts,
                mock.patch(
                    "ummanu.data.export_artifacts",
                    side_effect=lambda data_dir_arg: (
                        calls.append("artifacts")
                        or data_module.DataExport(data_dir_arg / "artifacts.json", 1, "artifacts")
                    ),
                ) as artifacts,
            ):
                export_all(data_dir, instance_dir, copy_transcripts=True)

        self.assertEqual(calls, ["memory", "board", "runs", "transcripts", "artifacts"])
        board.assert_called_once_with(data_dir, instance_dir=instance_dir)
        transcripts.assert_called_once_with(data_dir, copy=True)
        artifacts.assert_called_once_with(data_dir)
        memory.assert_called_once_with(data_dir, instance_dir)

    def test_default_pipeline_state_uses_ummanu_workspace(self):
        self.assertEqual(
            data_module.PIPELINE_STATE_DIR,
            Path.home() / "orca" / "workspaces" / "ummanu" / "pipeline" / "state" / "pipeline",
        )


def init_instance_repo(path: Path) -> Path:
    """A private instance repo with one commit and the `ummanu` project registered."""
    path.mkdir(parents=True, exist_ok=True)
    (path / "projects").mkdir()
    (path / "projects" / "ummanu.yaml").write_text("id: ummanu\n", encoding="utf-8")
    git(path, "init", "--initial-branch=main")
    git(path, "config", "user.name", "Test User")
    git(path, "config", "user.email", "test@example.invalid")
    git(path, "config", "commit.gpgsign", "false")
    (path / "instance.yaml").write_text("version: 1\nname: example\n", encoding="utf-8")
    git(path, "add", "instance.yaml")
    git(path, "commit", "-m", "config")
    return path


def memory_facts_dir(instance_dir: Path) -> Path:
    return instance_dir / "state" / "memory" / "facts"


def canon_facts(instance_dir: Path) -> list[str]:
    facts = memory_facts_dir(instance_dir)
    return sorted(path.relative_to(facts).as_posix() for path in facts.rglob("*.md"))


def write_index_from_canon(index: Path, facts_dir: Path) -> None:
    """An index in the memory service's schema, built from the canon the way the service builds one."""
    with sqlite3.connect(index) as conn:
        conn.execute(
            "create table memories(id integer primary key, fact_id text unique, content_hash text, "
            "text text, scope text, tags text, source text, created_at text)"
        )
        for path in sorted(facts_dir.rglob("*.md")):
            fact_id = path.relative_to(facts_dir).as_posix().removesuffix(".md")
            fact = parse_fact_text(path.read_text(encoding="utf-8"), f"{fact_id}.md", fact_id=fact_id)
            conn.execute(
                "insert into memories(fact_id, content_hash, text, scope, tags, source, created_at) "
                "values (?,?,?,?,?,?,?)",
                (
                    fact_id,
                    fact_content_hash(fact),
                    *(fact[key] for key in ("text", "scope", "tags", "source", "created_at")),
                ),
            )
        conn.commit()


def memory_commit_count(instance_dir: Path) -> str:
    return git(instance_dir, "rev-list", "--count", "HEAD", "--", "state/memory")


def memory_head(instance_dir: Path) -> str:
    return git(instance_dir, "log", "-1", "--format=%H", "--", "state/memory")


def memory_status(instance_dir: Path) -> str:
    return git(instance_dir, "status", "--porcelain", "--", "state/memory")


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


if __name__ == "__main__":
    unittest.main()
