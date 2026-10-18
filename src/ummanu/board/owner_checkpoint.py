"""Complete stored bell facts, with explicit coverage and database-local row identities."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ummanu._fsutil import ndjson_lines
from ummanu.board.checkpoint_layout import open_checkpoint_board, read_export_summary, unique_json_fields
from ummanu.board.owner_events import KIND_CLASS
from ummanu.board.sql_rows import parse_rfc3339, rfc3339

OWNER_FILE = "owner_events.ndjson"
OWNER_FIELDS = ("id", "kind", "class", "subject_ref", "text", "created_at", "read_at", "dedup_key")
LEGACY_OWNER_COVERAGE = "checkpoint has no owner events coverage; unknown bell facts were not reconstructed"


class OwnerCheckpointError(ValueError):
    """Stored bell facts cannot be restored without changing their semantics."""


def _fail(index: int, field: str) -> None:
    raise OwnerCheckpointError(f"owner event record {index}: {field} mismatch or invalid")


def validate_owner_records(rows: list[dict[str, Any]]) -> None:
    """Require canonical stored fields in source identity order, including historical kinds."""
    if not isinstance(rows, list):
        raise OwnerCheckpointError("owner events must be a list")
    last = 0
    seen: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != set(OWNER_FIELDS):
            _fail(index, "fields")
        if type(row["id"]) is not int or not last < row["id"] < 2**63:
            _fail(index, "id/order")
        last = row["id"]
        for field in ("kind", "class", "text", "dedup_key", "subject_ref"):
            value = row[field]
            if value is None and field == "subject_ref":
                continue
            if not isinstance(value, str) or "\x00" in value:
                _fail(index, field)
        if row["kind"] not in KIND_CLASS:
            _fail(index, "kind")
        if row["class"] != KIND_CLASS[row["kind"]]:
            _fail(index, "class")
        if not row["dedup_key"].strip() or row["dedup_key"] in seen:
            _fail(index, "dedup_key")
        seen.add(row["dedup_key"])
        for field in ("created_at", "read_at"):
            value = row[field]
            if value is None and field == "read_at":
                continue
            if not isinstance(value, str):
                _fail(index, field)
            moment = parse_rfc3339(value)
            if moment is None or moment.tzinfo is None or rfc3339(moment, microseconds=True) != value:
                _fail(index, field)


def owner_coverage(rows: list[dict[str, Any]], payload: bytes) -> dict[str, Any]:
    """Bind declared complete coverage, including zero rows, to exact bytes."""
    return {"version": 1, "count": len(rows), "sha256": hashlib.sha256(payload).hexdigest()}


def read_checkpoint_owner_events(directory: Path) -> list[dict[str, Any]] | None:
    """Validate declared bell coverage before writes; absence of both declaration and file is legacy."""
    try:
        board = open_checkpoint_board(directory)
        summary = read_export_summary(board)
        if "owner_events" not in summary:
            if board.has(OWNER_FILE) or board.path(OWNER_FILE).exists():
                raise OwnerCheckpointError("undeclared owner events coverage")
            return None
        coverage = summary["owner_events"]
        if (
            not isinstance(coverage, dict)
            or type(coverage.get("version")) is not int
            or coverage["version"] != 1
        ):
            raise OwnerCheckpointError("unsupported owner events coverage")
        payload = board.read_bytes(OWNER_FILE)
        rows = [
            json.loads(line, object_pairs_hook=unique_json_fields)
            for line in ndjson_lines(payload.decode("utf-8"))
            if line.strip()
        ]
        validate_owner_records(rows)
        if type(coverage.get("count")) is not int or coverage != owner_coverage(rows, payload):
            raise OwnerCheckpointError("owner events count/digest mismatch")
        return rows
    except OwnerCheckpointError:
        raise
    except (OSError, UnicodeError, ValueError):
        raise OwnerCheckpointError("owner events checkpoint is unreadable or invalid") from None


def require_owner_parity(expected: list[dict[str, Any]], actual: list[dict[str, Any]]) -> None:
    """Prove all semantic fields and relative order while allowing local ID remapping."""
    indexed = {row["dedup_key"]: row for row in actual}
    for index, row in enumerate(expected):
        other = indexed.get(row["dedup_key"])
        if other is None:
            _fail(index, "dedup_key")
        for field in OWNER_FIELDS[1:]:
            if row[field] != other[field]:
                _fail(index, field)
    if [row["dedup_key"] for row in expected] != [row["dedup_key"] for row in actual]:
        _fail(0, "dedup_key/order")


def scan_owner_secrets(text: str, scrub: Callable[[str], str]) -> None:
    """Scan decoded stored fields so JSON escaping cannot hide known secret values."""
    rows = [json.loads(line) for line in ndjson_lines(text) if line.strip()]
    validate_owner_records(rows)
    for index, row in enumerate(rows):
        for field, value in row.items():
            if isinstance(value, str) and scrub(value) != value:
                raise OwnerCheckpointError(f"secret detected in owner event record {index} field {field}")
