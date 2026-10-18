"""PO images are SQL-bound captures published at exact native parity before recovery admission."""

import hashlib
import json
import os
import shutil
import stat
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.fakes.installation import _checkpoint, split_board
from tests.po_checkpoint_fixtures import po_records, write_po
from tests.po_fake_store import FakeBoard, FakePoStore
from ummanu import data
from ummanu._fsutil import write_json
from ummanu.board import checkpoint_layout
from ummanu.board.checkpoint_layout import open_checkpoint_board, publish_split_board
from ummanu.board.po_attachments_checkpoint import (
    ATTACHMENTS_CHECKPOINT_FILE,
    LEGACY_ATTACHMENTS_COVERAGE,
    PoAttachmentsCheckpointError,
    read_checkpoint_attachments,
    scan_attachment_secrets,
    write_attachments_capture,
)
from ummanu.board.po_checkpoint import PoCheckpointError
from ummanu.data import export_board, init_layout
from ummanu.installation import support as installation_support
from ummanu.installation.materialize import materialize_checkpoint
from ummanu.installation.models import InstallError
from ummanu.memory.client_config import bridge_executable
from ummanu.po import attachments, recovery_admission, workspace as po_workspace
from ummanu.po.attachments import (
    MAX_ATTACHMENT_BYTES,
    AttachmentCaptureError,
    AttachmentInventory,
    AttachmentPublicationError,
    StoredAttachment,
    attachment_path,
    attachments_dir,
    inventory_attachments,
    publish_image,
    store_image,
)
from ummanu.po.queue import QueueCapture
from ummanu.po.recovery_admission import PoRecoveryAdmission
from ummanu.restore import RestoreError, restore_findings, restore_po, restore_state
from ummanu.runtime.redact import redact

SESSIONS = ["closed", "empty", "open"]
SECRET = "ghp_" + "Q7" * 18
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 3
JPEG = b"\xff\xd8\xff\xe0" + bytes(range(255, -1, -1)) * 2
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"\xab\xcd\xef" * 100
METADATA_SECRET = b"\x89PNG\r\n\x1a\n\x00\x00\x00\x2ftEXtComment\x00" + SECRET.encode() + b"\x9c\xff\x00"


def _scrub(value: str) -> str:
    return redact(value, env_files=[])


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.data = self.root / "data"
        self.data.mkdir()

    def seed(self) -> dict[tuple[str, str], bytes]:
        """Images of an open and a closed session, an empty directory, and what is not a session image."""
        stored = {}
        for session, image in (("open", PNG), ("open", JPEG), ("closed", WEBP), ("closed", PNG)):
            stored[(session, store_image(self.data, session, image).name)] = image
        attachments_dir(self.data, "empty").mkdir(parents=True)
        directory = attachments_dir(self.data, "open")
        (directory / f".{hashlib.sha256(b'x').hexdigest()}.png.tmp").write_bytes(b"in flight")
        (directory / "operator-note.txt").write_text("private")
        store_image(self.data, "orphan", PNG)
        return stored

    def board(self, inventory: AttachmentInventory, name: str = "board", *, data_dir: Path | None = None) -> Path:
        board = self.root / name
        board.mkdir()
        for logical in ("cards.ndjson", "sprints.ndjson", "audit.ndjson", "events.ndjson"):
            (board / logical).write_bytes(b"")
        write_json(board / "export.json", {"version": 1, "card_count": 0, "sprint_count": 0})
        write_po(board, po_records())
        self.declare(board, inventory, data_dir=data_dir)
        return board

    def declare(self, board: Path, inventory: AttachmentInventory, *, data_dir: Path | None = None) -> None:
        summary = json.loads((board / "export.json").read_text())
        summary["po_attachments"] = write_attachments_capture(
            board, data_dir or self.data, inventory, summary["po_sql"]["sha256"]
        )
        write_json(board / "export.json", summary)

    def captured_bytes(self, board: Path) -> dict[tuple[str, str], bytes]:
        lines = open_checkpoint_board(board).read_text(ATTACHMENTS_CHECKPOINT_FILE).splitlines()
        return {(record["session_id"], record["name"]): bytes.fromhex(record["data"])
                for record in map(json.loads, lines)}


class InventoryTests(_Case):
    def test_every_image_of_the_cut_sessions_is_captured_exactly_and_nothing_else(self) -> None:
        self.assertEqual(inventory_attachments(self.data, SESSIONS), AttachmentInventory(3, 0, ()))
        stored = self.seed()
        source = {path: (path.read_bytes(), path.stat().st_mtime_ns)
                  for path in (self.data / "po").rglob("*") if path.is_file()}
        inventory = inventory_attachments(self.data, SESSIONS)
        self.assertEqual((inventory.sessions, inventory.directories), (3, 3))
        self.assertEqual([(item.session_id, item.name) for item in inventory.files], sorted(stored))
        self.assertEqual([item.size for item in inventory.files], [len(stored[key]) for key in sorted(stored)])
        board = self.board(inventory)
        self.assertEqual(self.captured_bytes(board), stored)
        captured = read_checkpoint_attachments(board)
        self.assertEqual((captured.coverage(), len(captured.files), captured.image_bytes()),
                         ("captured", 4, sum(map(len, stored.values()))))
        self.assertEqual(source, {path: (path.read_bytes(), path.stat().st_mtime_ns)
                                  for path in (self.data / "po").rglob("*") if path.is_file()})

    def test_unsafe_or_damaged_entries_refuse_the_inventory_without_their_content(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "private.png").write_bytes(PNG)

        def image() -> Path:
            return next(attachments_dir(self.data, "open").glob("*.png"))

        def relink(path: Path) -> None:
            shutil.move(path, self.root / "moved")
            path.symlink_to(outside, target_is_directory=True)

        def oversize() -> None:
            with image().open("r+b") as handle:
                handle.truncate(MAX_ATTACHMENT_BYTES + 1)

        cases = {
            "symlinked image": lambda: (image().unlink(), image_path.symlink_to(outside / "private.png")),
            "fifo entry": lambda: os.mkfifo(attachments_dir(self.data, "open") / "private"),
            "directory entry": lambda: (attachments_dir(self.data, "open") / "private").mkdir(),
            "symlinked attachments": lambda: relink(attachments_dir(self.data, "open")),
            "symlinked session": lambda: relink(attachments_dir(self.data, "open").parent),
            "symlinked sessions": lambda: relink(self.data / "po" / "sessions"),
            "oversize image": oversize,
            "empty image": lambda: image().write_bytes(b""),
        }
        for label, damage in cases.items():
            with self.subTest(damage=label):
                shutil.rmtree(self.data)
                shutil.rmtree(self.root / "moved", ignore_errors=True)
                self.data.mkdir()
                self.seed()
                image_path = image()
                damage()
                with self.assertRaises(AttachmentCaptureError) as refused:
                    inventory_attachments(self.data, SESSIONS)
                self.assertNotIn("private", str(refused.exception))

    def test_a_vanished_changed_or_damaged_image_after_the_inventory_refuses_the_capture(self) -> None:
        self.seed()
        changes = {
            "vanished": lambda path: path.unlink(),
            "rewritten": lambda path: path.write_bytes(path.read_bytes()[:-1] + bytes([path.read_bytes()[-1] ^ 1])),
            "grown": lambda path: path.write_bytes(path.read_bytes() + b"\x00"),
            "replaced by a link": lambda path: (path.unlink(), path.symlink_to(self.root)),
        }
        for label, change in changes.items():
            with self.subTest(change=label):
                inventory = inventory_attachments(self.data, SESSIONS)
                item = inventory.files[-1]
                path = attachments_dir(self.data, item.session_id) / item.name
                original = path.read_bytes() if path.is_file() else None
                change(path)
                board = self.root / label.replace(" ", "-")
                board.mkdir()
                with self.assertRaises(AttachmentCaptureError) as refused:
                    write_attachments_capture(board, self.data, inventory, "a" * 64)
                self.assertIn(item.name, str(refused.exception))
                path.unlink(missing_ok=True)
                store_image(self.data, item.session_id, original)

    def test_an_upload_during_the_inventory_waits_for_the_lock_and_lands_after_the_cut(self) -> None:
        first = store_image(self.data, "open", PNG)
        upload = threading.Thread(target=lambda: store_image(self.data, "open", JPEG), name="upload")
        arrived = threading.Event()
        real_lock = recovery_admission.directory_lock

        def lock(path):
            if threading.current_thread() is upload:
                arrived.set()
            return real_lock(path)

        real_listing = attachments._session_files

        def listing(root, session_id):
            if not arrived.is_set():
                upload.start()
                self.assertTrue(arrived.wait(30))
            return real_listing(root, session_id)

        with (
            mock.patch.object(recovery_admission, "directory_lock", side_effect=lock),
            mock.patch.object(attachments, "_session_files", side_effect=listing),
        ):
            inventory = inventory_attachments(self.data, ["open"])
        upload.join(30)
        self.assertFalse(upload.is_alive())
        self.assertEqual([item.name for item in inventory.files], [first.name])
        self.assertEqual(len(self.captured_bytes(self.board(inventory))), 1)
        self.assertEqual(len(inventory_attachments(self.data, ["open"]).files), 2)


class CheckpointTests(_Case):
    def test_flat_and_split_read_back_and_tell_captured_empty_absent_and_legacy_apart(self) -> None:
        stored = self.seed()
        cases = {
            "captured": inventory_attachments(self.data, SESSIONS),
            "empty": AttachmentInventory(3, 1, ()),
            "absent": AttachmentInventory(3, 0, ()),
        }
        for label, inventory in cases.items():
            with self.subTest(coverage=label):
                flat = self.board(inventory, f"flat-{label}")
                split = self.root / f"split-{label}"
                publish_split_board(flat, split)
                for board in (flat, split):
                    captured = read_checkpoint_attachments(board)
                    self.assertEqual((captured.coverage(), captured.files), (label, inventory.files))
                self.assertEqual((split / "po_attachments").exists(), label == "captured")
        self.assertEqual(self.captured_bytes(self.root / "split-captured"), stored)
        legacy = self.board(AttachmentInventory(3, 0, ()), "legacy")
        summary = json.loads((legacy / "export.json").read_text())
        summary.pop("po_attachments")
        write_json(legacy / "export.json", summary)
        (legacy / ATTACHMENTS_CHECKPOINT_FILE).unlink()
        split = self.root / "split-legacy"
        publish_split_board(legacy, split)
        self.assertIsNone(read_checkpoint_attachments(legacy))
        self.assertIsNone(read_checkpoint_attachments(split))
        self.assertFalse((split / "po_attachments").exists())

    def test_damaged_coverage_or_payload_is_refused_without_content(self) -> None:
        self.seed()
        inventory = inventory_attachments(self.data, SESSIONS)
        lines = (self.board(inventory, "reference") / ATTACHMENTS_CHECKPOINT_FILE).read_text().splitlines(keepends=True)
        first = json.loads(lines[0])

        def coverage(change):
            def apply(board: Path) -> None:
                summary = json.loads((board / "export.json").read_text())
                change(summary)
                write_json(board / "export.json", summary)
            return apply

        def payload(*records: str):
            return lambda board: (board / ATTACHMENTS_CHECKPOINT_FILE).write_text("".join(records))

        def record(**changes) -> str:
            return json.dumps({**first, **changes}) + "\n"

        flipped = first["data"][:-2] + ("00" if first["data"][-2:] != "00" else "01")
        cases = {
            "unknown version": coverage(lambda summary: summary["po_attachments"].update(version=2)),
            "count mismatch": coverage(lambda summary: summary["po_attachments"].update(files=3)),
            "boolean count": coverage(lambda summary: summary["po_attachments"].update(directories=True)),
            "more directories than sessions": coverage(lambda summary: summary["po_attachments"].update(directories=4)),
            "unbound cut": coverage(lambda summary: summary["po_sql"].update(sha256="b" * 64)),
            "no PO SQL": coverage(lambda summary: summary.pop("po_sql")),
            "undeclared payload": coverage(lambda summary: summary.pop("po_attachments")),
            "missing payload": lambda board: (board / ATTACHMENTS_CHECKPOINT_FILE).unlink(),
            "changed PO SQL": lambda board: (board / "po_sql.json").write_text(
                (board / "po_sql.json").read_text().replace("Saved title", "Other title")),
            "truncated": payload(*lines[:-1]),
            "reordered": payload(lines[1], lines[0], *lines[2:]),
            "repeated": payload(lines[0], *lines),
            "duplicate field": payload(lines[0].replace('{"data"', '{"name": "private.png", "data"', 1), *lines[1:]),
            "flipped byte": payload(record(data=flipped), *lines[1:]),
            "uppercase hex": payload(record(data=first["data"].upper()), *lines[1:]),
            "wrong size": payload(record(size=first["size"] - 1, data=first["data"][:-2]), *lines[1:]),
            "wrong digest": payload(record(sha256="c" * 64), *lines[1:]),
            "wrong type": payload(record(name=first["name"].rsplit(".", 1)[0] + ".jpg"), *lines[1:]),
            "unsafe name": payload(record(name="../private.png"), *lines[1:]),
            "unknown session": payload(*lines, record(session_id="private")),
            "unknown field": payload(record(private="x"), *lines[1:]),
            "blank line": payload("\n", *lines),
        }
        for label, damage in cases.items():
            with self.subTest(damage=label):
                board = self.board(inventory, label.replace(" ", "-"))
                damage(board)
                with self.assertRaises(PoAttachmentsCheckpointError) as refused:
                    read_checkpoint_attachments(board)
                self.assertNotIn("private", str(refused.exception))
                self.assertNotIn(first["data"][:32], str(refused.exception))

    def test_split_parts_stay_within_the_budget_through_growth_rewrite_shrink_and_removal(self) -> None:
        budget = 512
        self.enterContext(mock.patch.object(checkpoint_layout, "MAX_SEGMENT_BYTES", budget))
        split = self.root / "split"

        def parts() -> list[Path]:
            return sorted((split / "po_attachments").rglob("*.ndjson"))

        def remove(session: str, image: bytes) -> None:
            (attachments_dir(self.data, session) / store_image(self.data, session, image).name).unlink()

        stages = {
            "grown": lambda: [store_image(self.data, session, image)
                              for session in ("closed", "open") for image in (PNG, JPEG, WEBP)],
            "rewritten at the front": lambda: remove("closed", JPEG),
            "shrunk": lambda: [remove("open", image) for image in (JPEG, WEBP)],
            "emptied": lambda: [path.unlink() for session in ("closed", "open")
                                for path in attachments_dir(self.data, session).iterdir()],
        }
        for label, change in stages.items():
            with self.subTest(stage=label):
                change()
                inventory = inventory_attachments(self.data, SESSIONS)
                board = self.board(inventory, f"flat-{label.replace(' ', '-')}")
                publish_split_board(board, split)
                logical = (board / ATTACHMENTS_CHECKPOINT_FILE).read_bytes()
                self.assertEqual(read_checkpoint_attachments(split).files, inventory.files)
                self.assertTrue(all(0 < part.stat().st_size <= budget for part in parts()))
                self.assertEqual(b"".join(part.read_bytes() for part in parts()), logical)
                self.assertEqual(len(parts()) > 1, len(logical) > budget)
                self.assertEqual((split / "po_attachments").exists(), bool(logical))
        legacy = self.board(AttachmentInventory(3, 0, ()), "flat-legacy")
        summary = json.loads((legacy / "export.json").read_text())
        summary.pop("po_attachments")
        write_json(legacy / "export.json", summary)
        (legacy / ATTACHMENTS_CHECKPOINT_FILE).unlink()
        store_image(self.data, "open", PNG)
        publish_split_board(self.board(inventory_attachments(self.data, SESSIONS), "flat-again"), split)
        publish_split_board(legacy, split)
        self.assertFalse((split / "po_attachments").exists())
        self.assertIsNone(read_checkpoint_attachments(split))

    def test_damaged_split_parts_are_refused(self) -> None:
        self.enterContext(mock.patch.object(checkpoint_layout, "MAX_SEGMENT_BYTES", 256))
        self.seed()
        inventory = inventory_attachments(self.data, SESSIONS)

        def missing(board: Path) -> None:
            sorted((board / "po_attachments").rglob("*.ndjson"))[-1].unlink()

        def gap(board: Path) -> None:
            sorted((board / "po_attachments").rglob("*.ndjson"))[1].unlink()

        def reordered(board: Path) -> None:
            first, second = sorted((board / "po_attachments").rglob("*.ndjson"))[:2]
            payload = first.read_bytes()
            first.write_bytes(second.read_bytes())
            second.write_bytes(payload)

        def stray(board: Path) -> None:
            (board / "po_attachments" / "0000" / "private.ndjson").write_text("private")

        def linked(board: Path) -> None:
            shutil.move(board / "po_attachments", self.root / f"moved-{board.name}")
            (board / "po_attachments").symlink_to(self.root / f"moved-{board.name}", target_is_directory=True)

        def removed(board: Path) -> None:
            shutil.rmtree(board / "po_attachments")

        for damage in (missing, gap, reordered, stray, linked, removed):
            with self.subTest(damage=damage.__name__):
                board = self.root / damage.__name__
                publish_split_board(self.board(inventory, f"flat-{damage.__name__}"), board)
                damage(board)
                with self.assertRaises(PoAttachmentsCheckpointError) as refused:
                    read_checkpoint_attachments(board)
                self.assertNotIn("private", str(refused.exception))

    def test_a_secret_in_image_metadata_is_found_in_the_decoded_bytes_whatever_cuts_its_hex(self) -> None:
        self.enterContext(mock.patch.object(checkpoint_layout, "MAX_SEGMENT_BYTES", 64))
        store_image(self.data, "open", PNG)
        name = store_image(self.data, "open", METADATA_SECRET).name
        board = self.root / "split"
        publish_split_board(self.board(inventory_attachments(self.data, SESSIONS), "flat"), board)
        logical = open_checkpoint_board(board).read_text(ATTACHMENTS_CHECKPOINT_FILE)
        for text in (logical, *(part.read_text() for part in (board / "po_attachments").rglob("*.ndjson"))):
            self.assertEqual(_scrub(text), text)
        with self.assertRaises(PoAttachmentsCheckpointError) as refused:
            scan_attachment_secrets(logical, _scrub)
        self.assertIn(name, str(refused.exception))
        self.assertNotIn(SECRET, str(refused.exception))
        scan_attachment_secrets("", _scrub)


class ExportTests(_Case):
    def export(self, inventory: AttachmentInventory) -> None:
        cut = ([], [], [], [], po_records(), [], QueueCapture(present=False, inputs=()), inventory)
        with mock.patch.object(data, "_read_board_cut", return_value=cut):
            export_board(self.data, instance_dir=self.root, reader=mock.Mock())

    def published(self) -> dict[str, bytes]:
        return {path.name: path.read_bytes() for path in (self.data / "board").iterdir() if path.is_file()}

    def test_a_refused_capture_or_a_known_secret_keeps_the_previous_live_export(self) -> None:
        init_layout(self.data)
        stored = self.seed()
        self.export(inventory_attachments(self.data, SESSIONS))
        self.assertEqual(self.captured_bytes(self.data / "board"), stored)
        before = self.published()
        secret = store_image(self.data, "open", METADATA_SECRET)
        with self.assertRaisesRegex(RuntimeError, "board export blocked: secret detected in PO attachment") as refused:
            self.export(inventory_attachments(self.data, SESSIONS))
        self.assertNotIn(SECRET, str(refused.exception))
        self.assertEqual(self.published(), before)
        secret.unlink()
        phantom = StoredAttachment("open", hashlib.sha256(PNG + b"x").hexdigest() + ".png", len(PNG) + 1)
        inventory = inventory_attachments(self.data, SESSIONS)
        with self.assertRaisesRegex(RuntimeError, "board export is not restorable: .* vanished after the inventory"):
            self.export(AttachmentInventory(inventory.sessions, inventory.directories, (*inventory.files, phantom)))
        self.assertEqual(self.published(), before)
        self.assertEqual(sorted(path.name for path in (self.data / "board").iterdir() if path.name.startswith(".")),
                         [])


class MaterializeTests(_Case):
    def test_flat_and_split_points_materialize_then_restore_native_bytes_before_sql(self) -> None:
        stored = self.seed()
        source_files = sorted(path for path in (self.data / "po").rglob("*") if path.is_file())
        for layout in ("flat", "split"):
            with self.subTest(layout=layout):
                instance, target = self.root / f"instance-{layout}", self.root / f"target-{layout}"
                _checkpoint(instance, target)
                board = instance / "state" / "board"
                write_po(board, po_records())
                self.declare(board, inventory_attachments(self.data, SESSIONS))
                if layout == "split":
                    split_board(board)
                    self.assertFalse((board / ATTACHMENTS_CHECKPOINT_FILE).exists())
                materialize_checkpoint(instance, target)
                self.assertEqual(self.captured_bytes(target / "board"), stored)
                self.assertFalse((target / "po").exists())
                fake = FakeBoard()
                with (
                    mock.patch("ummanu.board.store.resolve_role", return_value=object()),
                    mock.patch("ummanu.po.store.PoStore", side_effect=lambda _credentials, fake=fake: _ClosingStore(fake)),
                    mock.patch("ummanu.board.po_restore.prepare_po_restore", side_effect=PoCheckpointError("stopped")),
                    self.assertRaisesRegex(RestoreError, "failed at plan: stopped"),
                ):
                    restore_po(target, source=target / "board", instance=instance)
                state = restore_state(target)
                captured = read_checkpoint_attachments(target / "board")
                self.assertEqual(
                    (state["po_attachments_coverage"], state["po_attachments_files"], state["po_attachments_bytes"],
                     state["po_attachments_diagnostic"]),
                    ("captured", 4, sum(map(len, stored.values())),
                     captured.describe() + "; native runtime parity complete"),
                )
                self.assertEqual(state["po_attachments_parity"], "complete")
                for (session, name), image in stored.items():
                    self.assertEqual(attachment_path(target, session, name).read_bytes(), image)
                self.assertEqual(fake.requests, {})
        self.assertEqual(source_files, sorted(path for path in (self.data / "po").rglob("*") if path.is_file()))

    def test_a_later_point_without_images_replaces_the_payload_an_earlier_point_left_on_the_target(self) -> None:
        stored = self.seed()
        target = self.root / "target"
        points = iter(range(100))

        def point(inventory: AttachmentInventory | None, layout: str) -> Path:
            instance = self.root / f"instance-{next(points)}"
            _checkpoint(instance, target)
            board = instance / "state" / "board"
            write_po(board, po_records())
            if inventory is not None:
                self.declare(board, inventory)
            if layout == "split":
                split_board(board)
            return instance

        def restored_coverage() -> tuple[object, object, object]:
            with (
                mock.patch("ummanu.board.store.resolve_role", return_value=object()),
                mock.patch("ummanu.po.store.PoStore", side_effect=lambda _credentials: _ClosingStore(FakeBoard())),
                mock.patch("ummanu.board.po_restore.prepare_po_restore", side_effect=PoCheckpointError("stopped")),
                self.assertRaisesRegex(RestoreError, "failed at plan: stopped"),
            ):
                restore_po(target, source=target / "board", instance=self.root / "instance")
            state = restore_state(target)
            self.assertEqual(state["po_attachments_parity"],
                             "unavailable" if state["po_attachments_coverage"] == "unavailable" else "complete")
            return state["po_attachments_coverage"], state["po_attachments_files"], state["po_attachments_bytes"]

        captured = inventory_attachments(self.data, SESSIONS)
        later = {
            "split empty": (AttachmentInventory(3, 1, ()), "split", ("empty", 0, 0)),
            "split absent": (AttachmentInventory(3, 0, ()), "split", ("absent", 0, 0)),
            "flat empty": (AttachmentInventory(3, 1, ()), "flat", ("empty", 0, 0)),
            "legacy": (None, "split", ("unavailable", None, None)),
        }
        for label, (inventory, layout, coverage) in later.items():
            with self.subTest(point=label):
                materialize_checkpoint(point(captured, "split"), target)
                self.assertEqual(self.captured_bytes(target / "board"), stored)
                selected = point(inventory, layout)
                for _ in ("selected", "repeated"):
                    materialize_checkpoint(selected, target)
                    source = open_checkpoint_board(selected / "state" / "board")
                    published = target / "board" / ATTACHMENTS_CHECKPOINT_FILE
                    self.assertEqual(published.read_bytes() if published.exists() else None,
                                     source.read_bytes(ATTACHMENTS_CHECKPOINT_FILE)
                                     if source.has(ATTACHMENTS_CHECKPOINT_FILE) else None)
                    self.assertEqual(json.loads((target / "board" / "export.json").read_text()),
                                     json.loads(open_checkpoint_board(selected / "state" / "board")
                                                .read_text("export.json")))
                    attachments = read_checkpoint_attachments(target / "board")
                    if inventory is None:
                        self.assertIsNone(attachments)
                    else:
                        self.assertEqual((attachments.coverage(), attachments.files), (coverage[0], ()))
                    self.assertEqual(restored_coverage(), coverage)
                    self.assertFalse((target / "po").exists())

    def test_legacy_and_damaged_captures_are_told_apart_before_any_write(self) -> None:
        legacy = self.root / "legacy"
        init_layout(legacy)
        write_json(legacy / "board" / "export.json", {"version": 1})
        with mock.patch("ummanu.board.store.resolve_role", side_effect=AssertionError("credentials read")):
            restore_po(legacy, source=legacy / "board", instance=self.root / "instance")
        state = restore_state(legacy)
        self.assertEqual(
            (state["po_attachments_coverage"], state["po_attachments_files"], state["po_attachments_diagnostic"]),
            ("unavailable", None, LEGACY_ATTACHMENTS_COVERAGE),
        )
        self.seed()
        instance, target = self.root / "instance", self.root / "target"
        _checkpoint(instance, target)
        board = instance / "state" / "board"
        write_po(board, po_records())
        self.declare(board, inventory_attachments(self.data, SESSIONS))
        summary = json.loads((board / "export.json").read_text())
        summary["po_attachments"]["image_bytes"] += 1
        write_json(board / "export.json", summary)
        with self.assertRaisesRegex(InstallError, "PO attachment count/digest mismatch"):
            materialize_checkpoint(instance, target)
        self.assertFalse((target / "board" / ATTACHMENTS_CHECKPOINT_FILE).exists())
        damaged = self.root / "damaged"
        init_layout(damaged)
        shutil.copytree(board, damaged / "board", dirs_exist_ok=True)
        publish = mock.Mock()
        with (
            mock.patch("ummanu.board.store.resolve_role", side_effect=AssertionError("credentials read")),
            self.assertRaisesRegex(RestoreError, "PO attachment count/digest mismatch"),
        ):
            restore_po(damaged, source=damaged / "board", instance=instance, board=publish)
        publish.assert_not_called()
        self.assertEqual(restore_state(damaged), {})


class PublicationTests(_Case):
    def image(self, image=PNG, session="open"):
        name = f"{hashlib.sha256(image).hexdigest()}.{attachments.image_kind(image)}"
        return StoredAttachment(session, name, len(image)), image

    def native(self, item):
        return attachments_dir(self.data, item.session_id) / item.name

    def test_exact_images_retry_and_later_uploads_survive(self):
        images = [self.image(PNG), self.image(JPEG), self.image(WEBP, "closed")]
        for item, image in images:
            publish_image(self.data, item, image)
        later = store_image(self.data, "open", PNG + b"later")
        before = {self.native(item): self.native(item).stat().st_ino for item, _ in images}
        for item, image in images:
            publish_image(self.data, item, image)
            self.assertEqual(attachment_path(self.data, item.session_id, item.name).read_bytes(), image)
            self.assertEqual(self.native(item).stat().st_ino, before[self.native(item)])
            self.assertEqual(stat.S_IMODE(self.native(item).stat().st_mode), 0o600)
            for path in (self.native(item).parent, *list(self.native(item).parents)[1:4]):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        self.assertEqual(later.read_bytes(), PNG + b"later")

    def test_invalid_bytes_and_native_conflicts_never_replace_content(self):
        item, image = self.image()
        for invalid in (image[:-1], JPEG, b"private"):
            with self.assertRaises(AttachmentPublicationError):
                publish_image(self.data, item, invalid)
        self.assertFalse((self.data / "po").exists())
        publish_image(self.data, item, image)
        target = self.native(item)
        target.write_bytes(b"private")
        before = target.stat()
        with self.assertRaisesRegex(AttachmentPublicationError, "session open attachment .*conflict"):
            publish_image(self.data, item, image)
        self.assertEqual(target.read_bytes(), b"private")
        self.assertEqual(target.stat().st_ino, before.st_ino)
        self.assertEqual(target.stat().st_mode, before.st_mode)

    def test_installed_workspace_and_upload_directories_preserve_metadata_through_restore(self):
        product = self.root / "product"
        instructions = po_workspace.agents_source(product)
        instructions.parent.mkdir(parents=True)
        instructions.write_text("Fixture PO instructions\n")
        bridge = bridge_executable(product)
        bridge.parent.mkdir(parents=True)
        bridge.write_text("#!/bin/sh\n")
        target = self.root / "target"
        init_layout(target)
        previous_umask = os.umask(0o022)
        try:
            captured = {(session, store_image(self.data, session, image).name): image
                        for session, image in (("open", PNG), ("open", JPEG), ("closed", WEBP))}
            source = self.board(inventory_attachments(self.data, SESSIONS))
            po_workspace.materialize(product, target)
            existing = store_image(target, "open", PNG)
            later = store_image(target, "open", PNG + b"later upload")
            ancestors = list(existing.parents)[:4]
            for path in ancestors:
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o755)
            preserved = (existing, later, *ancestors)
            before = {path: (path.stat().st_ino, path.stat().st_uid, path.stat().st_gid, path.stat().st_mode)
                      for path in preserved}
            self.assertEqual(list(attachments_dir(target, "open").glob("*.jpg")), [])
            fake = FakeBoard()
            with (
                mock.patch("ummanu.board.store.resolve_role", return_value=object()),
                mock.patch("ummanu.po.store.PoStore", side_effect=lambda _: _ClosingStore(fake)),
                mock.patch("ummanu.board.po_restore.prepare_po_restore", side_effect=PoCheckpointError("stopped")),
            ):
                for _ in range(2):
                    with self.assertRaisesRegex(RestoreError, "failed at plan: stopped"):
                        restore_po(target, source=source, instance=self.root)
                    self.assertEqual(restore_state(target)["po_attachments_parity"], "complete")
                    for (session, name), image in captured.items():
                        path = attachment_path(target, session, name)
                        self.assertEqual(path.read_bytes(), image)
                        self.assertEqual((path.stat().st_uid, stat.S_IMODE(path.stat().st_mode)),
                                         (os.geteuid(), 0o600))
                    for path, identity in before.items():
                        info = path.stat()
                        self.assertEqual((info.st_ino, info.st_uid, info.st_gid, info.st_mode), identity)
                    for path in (attachments_dir(target, "closed"), attachments_dir(target, "closed").parent):
                        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
                    self.assertEqual(later.read_bytes(), PNG + b"later upload")
        finally:
            os.umask(previous_umask)

    def test_symlinks_nonregular_entries_and_unsafe_permissions_refuse_without_changes(self):
        item, image = self.image()
        outside = self.root / "outside"
        outside.write_bytes(b"private")
        for part in ("po", "sessions", "open", "attachments", item.name):
            for kind in ("symlink", "fifo", "permissions", "group-write", "other-write", "owner"):
                with self.subTest(part=part, kind=kind):
                    if (self.data / "po").exists():
                        shutil.rmtree(self.data / "po")
                    publish_image(self.data, item, image)
                    path = self.data / "po"
                    for name in ("sessions", "open", "attachments", item.name):
                        if path.name == part:
                            break
                        path /= name
                    if kind in ("symlink", "fifo"):
                        if path.is_dir():
                            shutil.rmtree(path)
                        else:
                            path.unlink()
                        if kind == "symlink":
                            path.symlink_to(outside)
                        else:
                            os.mkfifo(path)
                    elif kind == "permissions":
                        path.chmod(0o777 if path.is_dir() else 0o644)
                    elif kind in ("group-write", "other-write"):
                        mode = 0o700 if path.is_dir() else 0o600
                        path.chmod(mode | (0o020 if kind == "group-write" else 0o002))
                    before = path.lstat()
                    real_stat = os.fstat

                    def foreign_stat(fd, real_stat=real_stat, before=before):
                        info = real_stat(fd)
                        if (info.st_dev, info.st_ino) == (before.st_dev, before.st_ino):
                            fields = list(info)
                            fields[4] = os.geteuid() + 1
                            return os.stat_result(fields)
                        return info

                    patch = mock.patch.object(attachments.os, "fstat", side_effect=foreign_stat)
                    with ExitStack() as stack:
                        if kind == "owner":
                            stack.enter_context(patch)
                        with self.assertRaises(AttachmentPublicationError):
                            publish_image(self.data, item, image)
                    after = path.lstat()
                    self.assertEqual((after.st_ino, after.st_uid, after.st_mode),
                                     (before.st_ino, before.st_uid, before.st_mode))
                    self.assertEqual(outside.read_bytes(), b"private")
                    if not path.is_dir():
                        path.unlink()
                    if (self.data / "po").exists():
                        shutil.rmtree(self.data / "po")

    def test_link_and_every_directory_sync_failure_retry_checks_and_syncs_existing_bytes(self):
        item, image = self.image()
        real_link, real_sync = os.link, os.fsync

        def interrupted_link(*args, **kwargs):
            real_link(*args, **kwargs)
            raise OSError("interrupted after link")

        with (
            mock.patch.object(attachments.os, "link", side_effect=interrupted_link),
            self.assertRaises(AttachmentPublicationError),
        ):
            publish_image(self.data, item, image)
        self.assertEqual(self.native(item).read_bytes(), image)
        for component in (item.name, "attachments", "open", "sessions", "po", "data"):
            for _ in range(2):
                synced = []

                def sync(fd, synced=synced, component=component):
                    name = Path(os.readlink(f"/proc/self/fd/{fd}")).name
                    synced.append(name)
                    if name == component:
                        raise OSError("injected directory sync failure")
                    return real_sync(fd)

                with (
                    mock.patch.object(attachments.os, "fsync", side_effect=sync),
                    self.assertRaises(AttachmentPublicationError),
                ):
                    publish_image(self.data, item, image)
                self.assertIn(item.name, synced)
                self.assertEqual(self.native(item).read_bytes(), image)
            publish_image(self.data, item, image)
        self.assertEqual(attachment_path(self.data, item.session_id, item.name).read_bytes(), image)

    def test_selected_capture_is_frozen_and_partial_failure_holds_admission_until_retry(self):
        stored = self.seed()
        source = self.board(inventory_attachments(self.data, SESSIONS))
        target = self.root / "target"
        init_layout(target)
        fake = FakeBoard()
        images = read_checkpoint_attachments(source)
        first, second = images.files[:2]
        real_publish = attachments.publish_image
        attempts = []

        def publish(data_dir, item, content):
            attempts.append(item)
            if item == second:
                raise AttachmentPublicationError(f"session {item.session_id} attachment {item.name}: injected failure")
            real_publish(data_dir, item, content)

        def board():
            self.assertEqual(PoRecoveryAdmission(target, FakePoStore(fake)).status()["reason"], "recovery_hold")
            (source / ATTACHMENTS_CHECKPOINT_FILE).write_bytes(b"damaged after validation")

        with (
            mock.patch("ummanu.board.store.resolve_role", return_value=object()),
            mock.patch("ummanu.po.store.PoStore", side_effect=lambda _: _ClosingStore(fake)),
            mock.patch("ummanu.board.po_restore.prepare_po_restore", side_effect=PoCheckpointError("stopped")) as plan,
        ):
            with (
                mock.patch.object(attachments, "publish_image", side_effect=publish),
                self.assertRaisesRegex(RestoreError, "PO images restore failed: .*injected failure"),
            ):
                restore_po(target, source=source, instance=self.root, board=board)
            self.assertEqual(attempts, [first, second])
            self.assertEqual(attachment_path(target, first.session_id, first.name).read_bytes(),
                             stored[first.session_id, first.name])
            self.assertEqual(restore_state(target)["po_attachments_parity"], "failed")
            self.assertIn("PO images restore parity is incomplete", restore_findings(target))
            self.assertEqual(PoRecoveryAdmission(target, FakePoStore(fake)).status()["reason"], "recovery_hold")
            plan.assert_not_called()
            self.declare(source, inventory_attachments(self.data, SESSIONS))
            # Restore the selected source's bytes, not a mutable target export.
            write_json(target / "board" / "export.json", {"version": 1})
            for _ in range(2):
                with self.assertRaisesRegex(RestoreError, "failed at plan: stopped"):
                    restore_po(target, source=source, instance=self.root)
                self.assertEqual(restore_state(target)["po_attachments_parity"], "complete")
                for (session, name), image in stored.items():
                    self.assertEqual(attachment_path(target, session, name).read_bytes(), image)
        self.assertEqual(fake.requests, {})

    def test_recovery_ownership_handoff_leaves_native_content_for_runtime_validation(self):
        item, image = self.image()
        publish_image(self.data, item, image)
        instance = self.root / "instance"
        instance.mkdir()
        progress = self.data / "progress"
        progress.write_text("fixture")
        from ummanu.installation.upgrade import workspaces

        with (
            mock.patch.object(workspaces.os, "geteuid", return_value=0),
            mock.patch.object(workspaces.pwd, "getpwnam", return_value=SimpleNamespace(pw_uid=123, pw_gid=456)),
            mock.patch.object(workspaces.os, "chown") as chown,
        ):
            installation_support._establish_recovery_ownership_barrier(instance, self.data, "runtime")
        changed = {Path(call.args[0]) for call in chown.call_args_list}
        self.assertIn(progress, changed)
        self.assertIn(self.data, changed)
        self.assertFalse(any(path == self.data / "po" or self.data / "po" in path.parents for path in changed))
        self.assertEqual(self.native(item).read_bytes(), image)


class _ClosingStore(FakePoStore):
    def close(self):
        return None


if __name__ == "__main__":
    unittest.main()
