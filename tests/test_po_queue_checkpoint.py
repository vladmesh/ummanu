"""The PO queue is captured whole and strictly, and its checkpoint payload is validated before use."""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ummanu._fsutil import write_json
from ummanu.board import checkpoint_layout
from ummanu.board.checkpoint_layout import open_checkpoint_board, publish_split_board
from ummanu.board.po_queue_checkpoint import (
    QUEUE_CHECKPOINT_FILE,
    PoQueueCheckpointError,
    read_checkpoint_queue,
    scan_queue_secrets,
    write_queue_capture,
)
from ummanu.po.queue import REFUSED_DIR_NAME, PoQueue, QueueCapture, QueueError, queue_dir
from ummanu.runtime.redact import redact

PO_SQL_SHA256 = "a" * 64
SECRET = 'known\n"private\tsecret'
CARD = {"card_ref": "demo-1", "kind": "operation", "future": {"nested": [1, None, True]}}
METADATA = {"source": "dispatcher", "summary": "demo-1 (operation)", "unknown": {"kept": ["as", "data"]}}


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.data = self.root / "data"
        self.data.mkdir()
        self.queue = PoQueue(self.data)

    def seed(self) -> QueueCapture:
        """Every source the service accepts, frozen card facts, and an interrupted refusal."""
        self.queue.put(session_id="a", text="from the web", request_id="web", source="web",
                       metadata={"attachments": ["one.png"]})
        dispatched = self.queue.put(session_id="a", text="operate", request_id="card", source="dispatcher",
                                    card=CARD, note="production rights: none", metadata=METADATA)
        self.queue.put(session_id="b", text="seed", request_id="seed", source="po-service",
                       metadata={"source": "po-service", "summary": "Sprint session context"})
        refused = self.queue.put(session_id="c", text="gone", request_id="gone", source="web")
        self.queue.refuse(refused, "PO session c no longer exists")
        # A refusal interrupted after its set-aside copy is durable leaves the input in both sections.
        (queue_dir(self.data) / REFUSED_DIR_NAME / dispatched.name).write_text(
            json.dumps({**dispatched.document(), "reason": "interrupted"}), encoding="utf-8"
        )
        return self.queue.capture()

    def board(self, capture: QueueCapture, name: str = "board") -> Path:
        board = self.root / name
        board.mkdir()
        for logical in ("cards.ndjson", "sprints.ndjson", "audit.ndjson", "events.ndjson"):
            (board / logical).write_bytes(b"")
        summary = {"version": 1, "card_count": 0, "sprint_count": 0, "po_sql": {"sha256": PO_SQL_SHA256}}
        summary["po_queue"] = write_queue_capture(board, capture, PO_SQL_SHA256)
        write_json(board / "export.json", summary)
        return board


class CaptureTests(_Case):
    def test_capture_keeps_every_document_field_section_and_fifo_name(self) -> None:
        self.assertEqual(self.queue.capture(), QueueCapture(present=False, inputs=()))
        self.queue.ensure()
        self.assertEqual(self.queue.capture(), QueueCapture(present=True, inputs=()))
        capture = self.seed()
        (queue_dir(self.data) / ".00000000000000000001-1-000001.json.tmp").write_text("in flight")
        (queue_dir(self.data) / "operator-note.txt").write_text("not an input")
        names = sorted(path.name for path in queue_dir(self.data).glob("*.json"))
        self.assertEqual(self.queue.capture(), capture)
        pending = [item for item in capture.inputs if item.section == "pending"]
        refused = [item for item in capture.inputs if item.section == "refused"]
        self.assertEqual([item.name for item in pending], names)
        self.assertEqual([item.document["request_id"] for item in pending], ["web", "card", "seed"])
        self.assertEqual([item.document["source"] for item in pending], ["web", "dispatcher", "po-service"])
        self.assertEqual(pending[1].document["card"], CARD)
        self.assertEqual(pending[1].document["metadata"], METADATA)
        self.assertEqual(pending[1].document["note"], "production rights: none")
        self.assertEqual([item.document["reason"] for item in refused], ["interrupted", "PO session c no longer exists"])
        self.assertEqual(refused[0].name, pending[1].name)
        for item in capture.inputs:
            on_disk = json.loads((queue_dir(self.data) / ("" if item.section == "pending" else REFUSED_DIR_NAME)
                                  / item.name).read_text(encoding="utf-8"))
            self.assertEqual(item.document, on_disk)

    def test_unsafe_or_damaged_inputs_refuse_the_capture_without_their_content(self) -> None:
        self.seed()
        directory = queue_dir(self.data)
        first = sorted(directory.glob("*.json"))[0]
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "private.json").write_text(json.dumps({"text": "private"}))
        original = first.read_bytes()
        document = json.loads(original)

        def rewrite(value: object) -> None:
            first.write_text(json.dumps(value), encoding="utf-8")

        cases = {
            "symlinked input": lambda: (first.unlink(), first.symlink_to(outside / "private.json")),
            "fifo input": lambda: os.mkfifo(directory / "00000000000000000000-1-000001.json"),
            "directory input": lambda: (directory / "00000000000000000000-1-000002.json").mkdir(),
            "symlinked queue": lambda: (shutil.move(directory, self.root / "moved"), directory.symlink_to(outside)),
            "symlinked refused": lambda: (shutil.rmtree(directory / REFUSED_DIR_NAME),
                                          (directory / REFUSED_DIR_NAME).symlink_to(outside)),
            "not utf-8": lambda: first.write_bytes(b'{"text": "private \xff"}'),
            "duplicate field": lambda: first.write_text('{"text": "private", ' + original.decode()[1:]),
            "not finite": lambda: first.write_text(original.decode()[:-1] + ', "metadata": NaN}'),
            "unknown field": lambda: rewrite({**document, "private": "x"}),
            "missing field": lambda: rewrite({key: value for key, value in document.items() if key != "queued_at"}),
            "wrong type": lambda: rewrite({**document, "text": ["private"]}),
            "unknown source": lambda: rewrite({**document, "source": "private"}),
            "reason on pending": lambda: rewrite({**document, "reason": "private"}),
        }
        for label, damage in cases.items():
            with self.subTest(damage=label):
                damage()
                with self.assertRaises(QueueError) as refused:
                    self.queue.capture()
                self.assertNotIn("private", str(refused.exception))
                shutil.rmtree(self.data)
                self.data.mkdir()
                self.seed()
                first = sorted(directory.glob("*.json"))[0]
                original = first.read_bytes()
                document = json.loads(original)


class CheckpointTests(_Case):
    def test_flat_and_split_payloads_read_back_the_capture_and_keep_an_absence_absent(self) -> None:
        capture = self.seed()
        for label, value in (("seeded", capture), ("empty", QueueCapture(True, ())), ("absent", QueueCapture(False, ()))):
            with self.subTest(capture=label):
                board = self.board(value, f"flat-{label}")
                self.assertEqual(read_checkpoint_queue(board).capture, value)
                split = self.root / f"split-{label}"
                publish_split_board(board, split)
                self.assertEqual(read_checkpoint_queue(split).capture, value)
        legacy = self.board(capture, "legacy")
        summary = json.loads((legacy / "export.json").read_text())
        summary.pop("po_queue")
        write_json(legacy / "export.json", summary)
        (legacy / QUEUE_CHECKPOINT_FILE).unlink()
        split = self.root / "split-legacy"
        publish_split_board(legacy, split)
        self.assertIsNone(read_checkpoint_queue(legacy))
        self.assertIsNone(read_checkpoint_queue(split))
        self.assertFalse((split / QUEUE_CHECKPOINT_FILE).exists())
        self.assertFalse((split / "po_queue").exists())

    def test_damaged_coverage_or_payload_is_refused_without_content(self) -> None:
        capture = self.seed()
        payload = (self.board(capture, "reference") / QUEUE_CHECKPOINT_FILE).read_text(encoding="utf-8")
        lines = payload.splitlines(keepends=True)

        def coverage(change):
            def apply(board: Path) -> None:
                summary = json.loads((board / "export.json").read_text())
                change(summary)
                write_json(board / "export.json", summary)
            return apply

        def payload_text(text: str):
            return lambda board: (board / QUEUE_CHECKPOINT_FILE).write_text(text, encoding="utf-8")

        cases = {
            "unknown version": coverage(lambda summary: summary["po_queue"].update(version=2)),
            "count mismatch": coverage(lambda summary: summary["po_queue"].update(pending=1)),
            "boolean count": coverage(lambda summary: summary["po_queue"].update(refused=True)),
            "unbound cut": coverage(lambda summary: summary["po_sql"].update(sha256="b" * 64)),
            "no PO SQL": coverage(lambda summary: summary.pop("po_sql")),
            "absent with inputs": coverage(lambda summary: summary["po_queue"].update(directory="absent")),
            "undeclared payload": coverage(lambda summary: summary.pop("po_queue")),
            "missing payload": lambda board: (board / QUEUE_CHECKPOINT_FILE).unlink(),
            "truncated": payload_text("".join(lines[:-1])),
            "reordered": payload_text("".join([lines[1], lines[0], *lines[2:]])),
            "duplicate field": payload_text(lines[0].replace('{"document"', '{"name": "private.json", "document"', 1)
                                            + "".join(lines[1:])),
            "pending after refused": payload_text("".join([lines[0], lines[1], lines[3], lines[2], lines[4]])),
            "unsafe name": payload_text(lines[0].replace('"name": "', '"name": "../', 1) + "".join(lines[1:])),
            "blank line": payload_text("\n" + payload),
        }
        for label, damage in cases.items():
            with self.subTest(damage=label):
                board = self.board(capture, label.replace(" ", "-"))
                damage(board)
                with self.assertRaises(PoQueueCheckpointError) as refused:
                    read_checkpoint_queue(board)
                self.assertNotIn("private", str(refused.exception))
                self.assertNotIn("operate", str(refused.exception))

    def test_split_parts_stay_within_the_budget_and_follow_growth_rewrite_and_shrink(self) -> None:
        budget = 256
        self.enterContext(mock.patch.object(checkpoint_layout, "MAX_SEGMENT_BYTES", budget))
        split = self.root / "split"

        def parts() -> list[Path]:
            return sorted((split / "po_queue").rglob("*.ndjson"))

        long_text = "я" * 400 + SECRET
        stages = {
            "grown": lambda: [self.queue.put(session_id="a", text=f"input {index} " + "x" * 90, request_id=f"r{index}",
                                             source="web") for index in range(8)],
            "one line over the budget": lambda: self.queue.put(session_id="a", text=long_text, request_id="long",
                                                               source="web"),
            "rewritten at the front": lambda: self.queue.remove(self.queue.pending()[0]),
            "shrunk": lambda: [self.queue.remove(item) for item in self.queue.pending()[1:]],
            "emptied": lambda: [self.queue.remove(item) for item in self.queue.pending()],
        }
        for label, change in stages.items():
            with self.subTest(stage=label):
                change()
                capture = self.queue.capture()
                board = self.board(capture, f"flat-{label.replace(' ', '-')}")
                publish_split_board(board, split)
                logical = (board / QUEUE_CHECKPOINT_FILE).read_bytes()
                self.assertEqual(open_checkpoint_board(split).read_bytes(QUEUE_CHECKPOINT_FILE) if logical else b"",
                                 logical)
                self.assertEqual(read_checkpoint_queue(split).capture, capture)
                self.assertTrue(all(0 < part.stat().st_size <= budget for part in parts()))
                self.assertEqual(b"".join(part.read_bytes() for part in parts()), logical)
                self.assertEqual(len(parts()) > 1, len(logical) > budget)
                self.assertEqual((split / "po_queue").exists(), bool(logical))
                self.assertFalse((split / QUEUE_CHECKPOINT_FILE).exists())

    def test_split_absence_zero_and_damaged_parts_are_told_apart(self) -> None:
        self.enterContext(mock.patch.object(checkpoint_layout, "MAX_SEGMENT_BYTES", 128))
        for index in range(4):
            self.queue.put(session_id="a", text=f"input {index} " + "x" * 100, request_id=f"r{index}", source="web")
        capture = self.queue.capture()

        def split(name: str, value: QueueCapture) -> Path:
            target = self.root / name
            publish_split_board(self.board(value, f"flat-{name}"), target)
            return target

        zero = split("zero", QueueCapture(True, ()))
        absent = split("absent", QueueCapture(False, ()))
        self.assertFalse((zero / "po_queue").exists())
        self.assertEqual(read_checkpoint_queue(zero).capture, QueueCapture(True, ()))
        self.assertEqual(read_checkpoint_queue(absent).capture, QueueCapture(False, ()))

        def missing(board: Path) -> None:
            sorted((board / "po_queue").rglob("*.ndjson"))[-1].unlink()

        def gap(board: Path) -> None:
            sorted((board / "po_queue").rglob("*.ndjson"))[1].unlink()

        def reordered(board: Path) -> None:
            first, second = sorted((board / "po_queue").rglob("*.ndjson"))[:2]
            payload = first.read_bytes()
            first.write_bytes(second.read_bytes())
            second.write_bytes(payload)

        def stray(board: Path) -> None:
            (board / "po_queue" / "0000" / "private.ndjson").write_text("private")

        def linked(board: Path) -> None:
            shutil.move(board / "po_queue", self.root / f"moved-{board.name}")
            (board / "po_queue").symlink_to(self.root / f"moved-{board.name}", target_is_directory=True)

        def undeclared(board: Path) -> None:
            summary = json.loads((board / "export.json").read_text())
            summary.pop("po_queue")
            write_json(board / "export.json", summary)

        for damage in (missing, gap, reordered, stray, linked, undeclared):
            with self.subTest(damage=damage.__name__):
                board = split(damage.__name__, capture)
                damage(board)
                with self.assertRaises(PoQueueCheckpointError) as refused:
                    read_checkpoint_queue(board)
                self.assertNotIn("private", str(refused.exception))

    def test_a_secret_cut_by_a_part_boundary_is_found_in_the_logical_file(self) -> None:
        self.enterContext(mock.patch.object(checkpoint_layout, "MAX_SEGMENT_BYTES", 64))
        escaped = json.dumps(SECRET)[1:-1]

        def scrub(value: str) -> str:
            return redact(value, secret_values=(SECRET,))

        # Padding moves the escaped secret until a part boundary cuts it.
        for padding in range(64):
            shutil.rmtree(self.data)
            self.data.mkdir()
            self.queue.put(session_id="a", text="p" * padding + SECRET, request_id="r", source="web")
            board = self.root / f"split-{padding}"
            publish_split_board(self.board(self.queue.capture(), f"flat-{padding}"), board)
            parts = [part.read_text(encoding="utf-8", errors="replace")
                     for part in sorted((board / "po_queue").rglob("*.ndjson"))]
            if not any(escaped in part for part in parts):
                break
        self.assertFalse(any(escaped in part or scrub(part) != part for part in parts))
        with self.assertRaisesRegex(PoQueueCheckpointError, "secret detected in PO queue pending record 0 field text"):
            scan_queue_secrets(open_checkpoint_board(board).read_text(QUEUE_CHECKPOINT_FILE), scrub)

    def test_secret_scan_reads_decoded_fields_through_json_escapes(self) -> None:
        def scrub(value: str) -> str:
            return redact(value, secret_values=(SECRET,))

        for field in ("text", "note", "metadata", "card", "reason"):
            with self.subTest(field=field):
                shutil.rmtree(self.data)
                self.data.mkdir()
                self.queue.put(session_id="a", text="clean", request_id="clean", source="web")
                hidden = {"text": SECRET, "note": SECRET, "metadata": {"nested": [SECRET]},
                          "card": {SECRET: "key"}}
                item = self.queue.put(session_id="a", request_id="r", source="dispatcher",
                                      **{"text": "clean", **({field: hidden[field]} if field in hidden else {})})
                if field == "reason":
                    self.queue.refuse(item, SECRET)
                board = self.board(self.queue.capture(), f"secret-{field}")
                text = (board / QUEUE_CHECKPOINT_FILE).read_text(encoding="utf-8")
                self.assertNotIn(SECRET, text)
                with self.assertRaises(PoQueueCheckpointError) as refused:
                    scan_queue_secrets(text, scrub)
                self.assertNotIn("private", str(refused.exception))
        scan_queue_secrets("", scrub)


if __name__ == "__main__":
    unittest.main()
