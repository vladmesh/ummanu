"""The PO notes file travels as exact bytes, refuses unsafe sources and never overwrites a target."""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.po_checkpoint_fixtures import write_notes
from ummanu._fsutil import write_json
from ummanu.board.checkpoint_layout import publish_split_board
from ummanu.board.po_notes_checkpoint import (
    NOTES_CHECKPOINT_FILE,
    NotesCapture,
    PoNotesError,
    capture_notes,
    publish_notes,
    read_checkpoint_notes,
    scan_notes_secrets,
)
from ummanu.memory import client_config
from ummanu.po import workspace
from ummanu.runtime.redact import redact

NOTES = b"\xef\xbb\xbf# Decisions\r\nkeep option B\rno trailing newline \xd1\x8f\x00 end"
SECRET = 'known\n"private\tsecret'


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.data = self.root / "data"
        self.data.mkdir()
        self.notes = workspace.workspace_dir(self.data) / workspace.NOTES_FILE

    def write_source(self, content: bytes) -> None:
        self.notes.parent.mkdir(exist_ok=True)
        self.notes.write_bytes(content)

    def board(self, name: str = "board") -> Path:
        board = self.root / name
        board.mkdir()
        for logical in ("cards.ndjson", "sprints.ndjson", "audit.ndjson", "events.ndjson"):
            (board / logical).write_bytes(b"")
        write_json(board / "export.json", {"version": 1, "card_count": 0, "sprint_count": 0})
        return board


class CaptureTests(_Case):
    def test_capture_keeps_exact_bytes_and_tells_an_empty_file_from_an_absent_one(self) -> None:
        self.assertIsNone(capture_notes(self.data))
        self.notes.parent.mkdir()
        self.assertIsNone(capture_notes(self.data))
        self.write_source(b"")
        self.assertEqual(capture_notes(self.data), b"")
        self.write_source(NOTES)
        self.assertEqual(capture_notes(self.data), NOTES)

    def test_unsafe_or_unsupported_sources_are_refused_without_their_content(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (outside / workspace.NOTES_FILE).write_bytes(b"private outside notes")

        def file_link() -> None:
            self.notes.parent.mkdir()
            self.notes.symlink_to(outside / workspace.NOTES_FILE)

        def workspace_link() -> None:
            self.notes.parent.symlink_to(outside, target_is_directory=True)

        def fifo() -> None:
            self.notes.parent.mkdir()
            os.mkfifo(self.notes)

        def directory() -> None:
            self.notes.mkdir(parents=True)

        def workspace_file() -> None:
            self.notes.parent.write_bytes(b"private")

        def not_utf8() -> None:
            self.write_source(b"private \xff\xfe")

        cases = [file_link, workspace_link, fifo, directory, workspace_file, not_utf8]
        if os.geteuid() != 0:

            def unreadable() -> None:
                self.write_source(b"private")
                self.notes.chmod(0)

            cases.append(unreadable)
        for make in cases:
            with self.subTest(source=make.__name__):
                make()
                with self.assertRaises(PoNotesError) as refused:
                    capture_notes(self.data)
                self.assertNotIn("private", str(refused.exception))
                po = self.notes.parent
                if po.is_symlink() or po.is_file():
                    po.unlink()
                else:
                    shutil.rmtree(po)

    def test_a_file_that_changes_while_it_is_read_is_refused(self) -> None:
        self.write_source(b"private first")
        real_read = os.read
        grown = []

        def read(descriptor: int, size: int) -> bytes:
            if not grown:
                grown.append(True)
                with self.notes.open("ab") as handle:
                    handle.write(b" and more")
            return real_read(descriptor, size)

        with mock.patch("ummanu.board.po_notes_checkpoint.os.read", side_effect=read), self.assertRaisesRegex(
            PoNotesError, "changed during capture"
        ):
            capture_notes(self.data)


class TransportTests(_Case):
    def test_present_empty_and_absent_captures_read_back_exactly_in_both_layouts(self) -> None:
        for name, content in (("present", NOTES), ("empty", b""), ("absent", None)):
            with self.subTest(capture=name):
                flat = self.board(f"flat-{name}")
                write_notes(flat, content)
                split = self.root / f"split-{name}"
                split.mkdir()
                publish_split_board(flat, split)
                for board in (flat, split):
                    self.assertEqual(read_checkpoint_notes(board), NotesCapture(content))
                self.assertEqual((split / NOTES_CHECKPOINT_FILE).exists(), content is not None)

    def test_a_board_without_the_declaration_is_legacy_and_an_undeclared_payload_is_refused(self) -> None:
        board = self.board()
        self.assertIsNone(read_checkpoint_notes(board))
        write_json(board / NOTES_CHECKPOINT_FILE, {"version": 1, "text": "private"})
        with self.assertRaisesRegex(PoNotesError, "undeclared PO notes coverage"):
            read_checkpoint_notes(board)

    def test_damaged_declared_captures_are_refused_without_their_content(self) -> None:
        def payload(board: Path, value: object) -> None:
            (board / NOTES_CHECKPOINT_FILE).write_text(json.dumps(value), encoding="utf-8")

        def coverage(board: Path, **changes: object) -> None:
            summary = json.loads((board / "export.json").read_text())
            summary["po_notes"].update(changes)
            write_json(board / "export.json", summary)

        damages = {
            "missing payload": lambda board: (board / NOTES_CHECKPOINT_FILE).unlink(),
            "changed text": lambda board: payload(board, {"version": 1, "text": "private changed"}),
            "extra field": lambda board: payload(board, {"version": 1, "text": "private", "mode": 1}),
            "payload version": lambda board: payload(board, {"version": 2, "text": "private"}),
            "duplicate field": lambda board: (board / NOTES_CHECKPOINT_FILE).write_text(
                '{"version": 1, "text": "private", "text": "private"}', encoding="utf-8"
            ),
            "not json": lambda board: (board / NOTES_CHECKPOINT_FILE).write_bytes(b"private \xff"),
            "digest": lambda board: coverage(board, sha256="0" * 64),
            "boolean size": lambda board: coverage(board, bytes=True),
            "coverage version": lambda board: coverage(board, version=2),
            "unknown state": lambda board: coverage(board, state="private"),
            "absence beside a payload": lambda board: coverage(board, state="absent"),
        }
        for name, damage in damages.items():
            with self.subTest(damage=name):
                board = self.board(name.replace(" ", "-"))
                write_notes(board, b"private")
                damage(board)
                with self.assertRaises(PoNotesError) as refused:
                    read_checkpoint_notes(board)
                self.assertNotIn("private", str(refused.exception))

    def test_the_secret_scan_reads_the_decoded_notes_and_names_no_content(self) -> None:
        board = self.board()
        write_notes(board, f"before {SECRET} after".encode())
        escaped = (board / NOTES_CHECKPOINT_FILE).read_text(encoding="utf-8")
        self.assertNotIn(SECRET, escaped)
        with self.assertRaises(PoNotesError) as refused:
            scan_notes_secrets(escaped, lambda text: redact(text, secret_values=(SECRET,)))
        self.assertEqual(str(refused.exception), "secret detected in PO notes")
        write_notes(board, NOTES)
        scan_notes_secrets(
            (board / NOTES_CHECKPOINT_FILE).read_text(encoding="utf-8"),
            lambda text: redact(text, secret_values=(SECRET,)),
        )


class PublishTests(_Case):
    def staged(self) -> Path:
        return self.notes.parent / f".{workspace.NOTES_FILE}.restore.tmp"

    def test_publication_creates_a_private_file_and_an_exact_retry_is_parity(self) -> None:
        self.assertTrue(publish_notes(self.data, NOTES))
        self.assertEqual(self.notes.read_bytes(), NOTES)
        info = self.notes.stat()
        self.assertEqual((info.st_mode & 0o777, info.st_uid), (0o600, os.geteuid()))
        self.assertEqual(self.notes.parent.stat().st_mode & 0o777, 0o700)
        self.assertFalse(publish_notes(self.data, NOTES))
        self.assertEqual(self.notes.read_bytes(), NOTES)
        self.assertFalse(self.staged().exists())
        empty = self.root / "empty-data"
        empty.mkdir()
        self.assertTrue(publish_notes(empty, b""))
        self.assertEqual((workspace.workspace_dir(empty) / workspace.NOTES_FILE).read_bytes(), b"")

    def test_an_existing_other_file_is_a_conflict_and_keeps_its_bytes(self) -> None:
        for existing in (workspace.NOTES_SEED.encode(), b"private newer notes", b""):
            with self.subTest(existing=existing):
                self.write_source(existing)
                with self.assertRaises(PoNotesError) as refused:
                    publish_notes(self.data, NOTES)
                self.assertIn("target conflict", str(refused.exception))
                self.assertNotIn("private", str(refused.exception))
                self.assertEqual(self.notes.read_bytes(), existing)

    def test_a_symlinked_target_or_workspace_is_refused_and_left_in_place(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (outside / workspace.NOTES_FILE).write_bytes(b"outside")
        self.notes.parent.mkdir()
        self.notes.symlink_to(outside / workspace.NOTES_FILE)
        with self.assertRaisesRegex(PoNotesError, "symlink"):
            publish_notes(self.data, NOTES)
        self.assertTrue(self.notes.is_symlink())
        self.notes.unlink()
        self.notes.parent.rmdir()
        self.notes.parent.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(PoNotesError, "symlink"):
            publish_notes(self.data, NOTES)
        self.assertEqual((outside / workspace.NOTES_FILE).read_bytes(), b"outside")
        self.assertEqual(sorted(path.name for path in outside.iterdir()), [workspace.NOTES_FILE])

    def test_an_interruption_before_or_after_the_link_repeats_without_loss(self) -> None:
        self.notes.parent.mkdir()
        self.staged().write_bytes(b"half written")
        self.assertTrue(publish_notes(self.data, NOTES))
        self.assertEqual(self.notes.read_bytes(), NOTES)
        self.assertFalse(self.staged().exists())
        os.link(self.notes, self.staged())
        self.assertFalse(publish_notes(self.data, NOTES))
        self.assertEqual(self.notes.read_bytes(), NOTES)
        self.assertFalse(self.staged().exists())

    def test_workspace_materialize_after_publication_keeps_the_restored_bytes(self) -> None:
        product = self.root / "product"
        bridge = client_config.bridge_executable(product)
        bridge.parent.mkdir(parents=True)
        bridge.write_text("#!/bin/sh\n", encoding="utf-8")
        source = workspace.agents_source(product)
        source.parent.mkdir(parents=True)
        source.write_text("# PO\n", encoding="utf-8")
        publish_notes(self.data, NOTES)
        result = workspace.materialize(product, self.data)
        self.assertNotIn(workspace.NOTES_FILE, result.changed)
        self.assertEqual(self.notes.read_bytes(), NOTES)
        self.assertFalse(publish_notes(self.data, NOTES))


if __name__ == "__main__":
    unittest.main()
