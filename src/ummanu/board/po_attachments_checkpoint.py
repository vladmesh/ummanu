"""The PO session images in the board checkpoint: inert exact bytes bound to the PO SQL cut.

A capture is every stored image of every session the captured PO SQL holds, inventoried inside the
publication lock in the same hold as the PO SQL snapshot and read after it. Checkpoint and
materialize carry it as data under `board/`; nothing here writes a session's attachments directory.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ummanu._fsutil import ndjson_line, ndjson_lines
from ummanu.board.checkpoint_layout import open_checkpoint_board, read_export_summary, unique_json_fields
from ummanu.board.po_checkpoint import PO_FILE
from ummanu.po.attachments import (
    ATTACHMENT_NAME,
    AttachmentCaptureError,
    AttachmentInventory,
    AttachmentRefused,
    StoredAttachment,
    attachments_dir,
    read_inventoried,
    stored_image_matches,
)

ATTACHMENTS_CHECKPOINT_FILE = "po_attachments.ndjson"
COVERAGE_FIELD = "po_attachments"
LEGACY_ATTACHMENTS_COVERAGE = "checkpoint has no PO attachment coverage; PO session images are unavailable"
_RECORD_FIELDS = {"session_id", "name", "size", "sha256", "data"}
_COUNTS = ("sessions", "directories", "files", "image_bytes", "bytes")
_HEX = re.compile(r"[0-9a-f]*")


class PoAttachmentsCheckpointError(ValueError):
    """Captured PO images cannot be carried without changing, losing or misbinding one."""


@dataclass(frozen=True)
class CheckpointAttachments:
    """A fully validated, SQL-bound capture retaining the selected source's immutable payload."""

    files: tuple[StoredAttachment, ...]
    sessions: int
    directories: int
    po_sql_sha256: str
    payload: bytes = field(repr=False)

    def images(self) -> Iterator[tuple[StoredAttachment, bytes]]:
        return _records(self.payload)

    def coverage(self) -> str:
        if self.files:
            return "captured"
        return "empty" if self.directories else "absent"

    def image_bytes(self) -> int:
        return sum(item.size for item in self.files)

    def describe(self) -> str:
        if self.files:
            return (f"{len(self.files)} PO image(s), {self.image_bytes()} byte(s) of "
                    f"{len({item.session_id for item in self.files})} session(s) captured")
        if self.directories:
            return f"the attachments directories of {self.directories} PO session(s) held no image"
        return "no PO session of the captured cut had an attachments directory"


def attachments_coverage(inventory: AttachmentInventory, payload_bytes: int, sha256: str,
                         po_sql_sha256: str) -> dict[str, Any]:
    """Bind the inventory's counts and the PO SQL cut to the exact payload bytes."""
    return {
        "version": 1,
        "sessions": inventory.sessions,
        "directories": inventory.directories,
        "files": len(inventory.files),
        "image_bytes": sum(item.size for item in inventory.files),
        "bytes": payload_bytes,
        "sha256": sha256,
        "po_sql_sha256": po_sql_sha256,
    }


def write_attachments_capture(directory: Path, data_dir: Path, inventory: AttachmentInventory,
                              po_sql_sha256: str) -> dict[str, Any]:
    """Stage one line per inventoried image, read one at a time, and return the coverage that declares them.

    The bytes are hex, not base64, so the escaped file itself cannot match a credential shape.
    """
    digest = hashlib.sha256()
    size = 0
    try:
        with (directory / ATTACHMENTS_CHECKPOINT_FILE).open("wb") as handle:
            for item in inventory.files:
                data = read_inventoried(data_dir, item)
                line = ndjson_line({"session_id": item.session_id, "name": item.name, "size": item.size,
                                    "sha256": hashlib.sha256(data).hexdigest(), "data": data.hex()}).encode("utf-8")
                handle.write(line)
                digest.update(line)
                size += len(line)
    except OSError as exc:
        raise AttachmentCaptureError(f"could not stage the PO attachments: {exc.strerror}") from None
    return attachments_coverage(inventory, size, digest.hexdigest(), po_sql_sha256)


def _records(payload: bytes) -> Iterator[tuple[StoredAttachment, bytes]]:
    """Validate every record as one stored image in strict (session, name) order, one at a time."""
    previous: StoredAttachment | None = None
    for index, line in enumerate(ndjson_lines(payload.decode("utf-8"))):
        try:
            record = json.loads(line, object_pairs_hook=unique_json_fields)
        except (ValueError, RecursionError):
            raise PoAttachmentsCheckpointError(f"PO attachment record {index} is not a captured image") from None
        if (
            type(record) is not dict
            or set(record) != _RECORD_FIELDS
            or any(type(record[field]) is not str for field in ("session_id", "name", "sha256", "data"))
            or type(record["size"]) is not int
            or not ATTACHMENT_NAME.match(record["name"])
            or not _HEX.fullmatch(record["data"])
            or len(record["data"]) != 2 * record["size"]
        ):
            raise PoAttachmentsCheckpointError(f"PO attachment record {index} is not a captured image")
        data = bytes.fromhex(record["data"])
        try:
            attachments_dir(Path("/"), record["session_id"])
        except AttachmentRefused:
            raise PoAttachmentsCheckpointError(f"PO attachment record {index} names an unsafe session") from None
        if record["sha256"] != hashlib.sha256(data).hexdigest() or not stored_image_matches(record["name"], data):
            raise PoAttachmentsCheckpointError(f"PO attachment record {index} bytes do not match its name or digest")
        item = StoredAttachment(record["session_id"], record["name"], record["size"])
        if previous is not None and (previous.session_id, previous.name) >= (item.session_id, item.name):
            raise PoAttachmentsCheckpointError(f"PO attachment record {index} is out of order or repeated")
        previous = item
        yield item, data


def read_checkpoint_attachments(directory: Path) -> CheckpointAttachments | None:
    """Validate the declared capture before any write; no declaration and no payload is legacy."""
    try:
        board = open_checkpoint_board(directory)
        summary = read_export_summary(board)
        if COVERAGE_FIELD not in summary:
            if board.has(ATTACHMENTS_CHECKPOINT_FILE):
                raise PoAttachmentsCheckpointError("undeclared PO attachment coverage")
            return None
        coverage = summary[COVERAGE_FIELD]
        if (
            not isinstance(coverage, dict)
            or type(coverage.get("version")) is not int
            or coverage["version"] != 1
            or any(type(coverage.get(field)) is not int for field in _COUNTS)
        ):
            raise PoAttachmentsCheckpointError("unsupported PO attachment coverage")
        po_sql = summary.get("po_sql")
        if not isinstance(po_sql, dict) or coverage.get("po_sql_sha256") != po_sql.get("sha256"):
            raise PoAttachmentsCheckpointError("PO attachment coverage is not bound to this checkpoint's PO SQL cut")
        po_payload = board.read_bytes(PO_FILE)
        if hashlib.sha256(po_payload).hexdigest() != po_sql["sha256"]:
            raise PoAttachmentsCheckpointError("PO attachment coverage is not bound to this checkpoint's PO SQL cut")
        sessions = {row["session_id"] for row in json.loads(po_payload)["po_sessions"]}
        # A split checkpoint stores an empty payload as no parts; the digest proves which was declared.
        payload = board.read_bytes(ATTACHMENTS_CHECKPOINT_FILE) if board.has(ATTACHMENTS_CHECKPOINT_FILE) else b""
        files = tuple(item for item, _ in _records(payload))
        if any(item.session_id not in sessions for item in files):
            raise PoAttachmentsCheckpointError("PO attachment record names a session the PO SQL cut does not hold")
        inventory = AttachmentInventory(len(sessions), coverage["directories"], files)
        if (
            coverage != attachments_coverage(inventory, len(payload), hashlib.sha256(payload).hexdigest(),
                                             po_sql["sha256"])
            or coverage["directories"] > len(sessions)
            or len({item.session_id for item in files}) > coverage["directories"]
        ):
            raise PoAttachmentsCheckpointError("PO attachment count/digest mismatch")
        return CheckpointAttachments(files, len(sessions), coverage["directories"], po_sql["sha256"], payload)
    except PoAttachmentsCheckpointError:
        raise
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        raise PoAttachmentsCheckpointError("PO attachment checkpoint is unreadable or invalid") from None


def scan_attachment_secrets(text: str, scrub: Callable[[str], str]) -> None:
    """Scan each image's decoded bytes as text, naming only the record ordinal and image name."""
    try:
        for index, (item, data) in enumerate(_records(text.encode("utf-8"))):
            # Text metadata an image carries (PNG text chunks, EXIF) is scanned where its bytes hold it.
            decoded = data.decode("utf-8", errors="replace")
            if scrub(item.session_id) != item.session_id or scrub(decoded) != decoded:
                raise PoAttachmentsCheckpointError(f"secret detected in PO attachment record {index} {item.name}")
    except UnicodeError:
        raise PoAttachmentsCheckpointError("PO attachment payload is unreadable") from None
    if scrub(text) != text:
        raise PoAttachmentsCheckpointError("secret detected in PO attachment file structure")
