"""The PO input queue in the board checkpoint: inert captured documents bound to the PO SQL cut.

A capture is every pending and refused input file of `<data>/po-queue/`, read inside the queue's
mutation boundary in the same hold as the PO SQL snapshot. Checkpoint and materialize carry it as
data under `board/`; nothing here writes a queue file, claims a request or starts a turn.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ummanu._fsutil import ndjson_line, ndjson_lines, write_text_atomic
from ummanu.board.checkpoint_layout import open_checkpoint_board, read_export_summary
from ummanu.board.po_checkpoint import nested_secret
from ummanu.po.queue import (
    PENDING,
    REFUSED,
    CapturedInput,
    QueueCapture,
    QueueError,
    is_input_name,
    parse_input_document,
    validate_input_document,
)

QUEUE_CHECKPOINT_FILE = "po_queue.ndjson"
COVERAGE_FIELD = "po_queue"
LEGACY_QUEUE_COVERAGE = "checkpoint has no PO queue coverage; pending and refused PO inputs are unavailable"
QUEUE_NOT_RESTORED = "inert data in board/po_queue.ndjson; queue execution and reconciliation are not restored"
_RECORD_FIELDS = {"section", "name", "document"}


class PoQueueCheckpointError(ValueError):
    """A captured PO queue cannot be carried without changing or losing an input."""


@dataclass(frozen=True)
class CheckpointQueue:
    """A declared capture and the PO SQL coverage digest it was read with."""

    capture: QueueCapture
    po_sql_sha256: str

    def counts(self) -> tuple[int, int]:
        pending = sum(1 for item in self.capture.inputs if item.section == PENDING)
        return pending, len(self.capture.inputs) - pending

    def describe(self) -> str:
        pending, refused = self.counts()
        if not self.capture.present:
            return f"source host had no PO queue directory; {QUEUE_NOT_RESTORED}"
        return f"{pending} pending and {refused} refused PO input(s) captured as {QUEUE_NOT_RESTORED}"


def queue_payload(capture: QueueCapture) -> bytes:
    return "".join(
        ndjson_line({"section": item.section, "name": item.name, "document": item.document})
        for item in capture.inputs
    ).encode("utf-8")


def queue_coverage(capture: QueueCapture, payload: bytes, po_sql_sha256: str) -> dict[str, Any]:
    """Bind section counts, directory presence and the PO SQL cut to the exact payload bytes."""
    pending = sum(1 for item in capture.inputs if item.section == PENDING)
    return {
        "version": 1,
        "directory": "present" if capture.present else "absent",
        "pending": pending,
        "refused": len(capture.inputs) - pending,
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "po_sql_sha256": po_sql_sha256,
    }


def write_queue_capture(directory: Path, capture: QueueCapture, po_sql_sha256: str) -> dict[str, Any]:
    """Stage the payload, empty for an empty or absent queue, and return the coverage that declares it."""
    payload = queue_payload(capture)
    write_text_atomic(directory / QUEUE_CHECKPOINT_FILE, payload.decode("utf-8"))
    return queue_coverage(capture, payload, po_sql_sha256)


def _records(payload: bytes) -> tuple[CapturedInput, ...]:
    """Validate every record as the owner's document in its captured section and FIFO order."""
    inputs: list[CapturedInput] = []
    for index, line in enumerate(ndjson_lines(payload.decode("utf-8"))):
        try:
            record = parse_input_document(line)
            if type(record) is not dict or set(record) != _RECORD_FIELDS:
                raise QueueError("record fields")
            validate_input_document(record["document"], record["section"])
        except (ValueError, RecursionError, QueueError):
            raise PoQueueCheckpointError(f"PO queue record {index} is not a captured input") from None
        if not is_input_name(record["name"]):
            raise PoQueueCheckpointError(f"PO queue record {index} name is not a queue input name")
        item = CapturedInput(record["section"], record["name"], record["document"])
        if inputs and (
            (inputs[-1].section, item.section) == (REFUSED, PENDING)
            or (inputs[-1].section == item.section and inputs[-1].name >= item.name)
        ):
            raise PoQueueCheckpointError(f"PO queue record {index} is out of section or name order")
        inputs.append(item)
    return tuple(inputs)


def read_checkpoint_queue(directory: Path) -> CheckpointQueue | None:
    """Validate the declared capture before any write; no declaration and no payload is legacy."""
    try:
        board = open_checkpoint_board(directory)
        summary = read_export_summary(board)
        if COVERAGE_FIELD not in summary:
            if board.has(QUEUE_CHECKPOINT_FILE):
                raise PoQueueCheckpointError("undeclared PO queue coverage")
            return None
        coverage = summary[COVERAGE_FIELD]
        if (
            not isinstance(coverage, dict)
            or type(coverage.get("version")) is not int
            or coverage["version"] != 1
            or coverage.get("directory") not in ("present", "absent")
            or any(type(coverage.get(field)) is not int for field in ("pending", "refused", "bytes"))
        ):
            raise PoQueueCheckpointError("unsupported PO queue coverage")
        po_sql = summary.get("po_sql")
        if not isinstance(po_sql, dict) or coverage.get("po_sql_sha256") != po_sql.get("sha256"):
            raise PoQueueCheckpointError("PO queue coverage is not bound to this checkpoint's PO SQL cut")
        if not board.has(QUEUE_CHECKPOINT_FILE):
            raise PoQueueCheckpointError("declared PO queue payload is missing")
        payload = board.read_bytes(QUEUE_CHECKPOINT_FILE)
        capture = QueueCapture(present=coverage["directory"] == "present", inputs=_records(payload))
        if not capture.present and capture.inputs:
            raise PoQueueCheckpointError("PO queue payload contradicts the captured absence")
        if coverage != queue_coverage(capture, payload, po_sql["sha256"]):
            raise PoQueueCheckpointError("PO queue count/digest mismatch")
        return CheckpointQueue(capture, po_sql["sha256"])
    except PoQueueCheckpointError:
        raise
    except (OSError, UnicodeError, ValueError):
        raise PoQueueCheckpointError("PO queue checkpoint is unreadable or invalid") from None


def scan_queue_secrets(text: str, scrub: Callable[[str], str]) -> None:
    """Scan every decoded field and its serialized form, naming only the section ordinal and field."""
    try:
        inputs = _records(text.encode("utf-8"))
    except (PoQueueCheckpointError, UnicodeError):
        raise PoQueueCheckpointError("PO queue payload is unreadable") from None
    for index, item in enumerate(inputs):
        if nested_secret(item.name, scrub):
            raise PoQueueCheckpointError(f"secret detected in PO queue record {index} name")
        for field, value in item.document.items():
            serialized = json.dumps(value, ensure_ascii=False)
            if nested_secret(value, scrub) or scrub(serialized) != serialized:
                raise PoQueueCheckpointError(f"secret detected in PO queue {item.section} record {index} field {field}")
    if scrub(text) != text:
        raise PoQueueCheckpointError("secret detected in PO queue file structure")
