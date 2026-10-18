"""Complete, inert PO SQL records on the board cut's connection and snapshot."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ummanu.board.checkpoint_layout import open_checkpoint_board, read_export_summary
from ummanu.board.sql_rows import parse_rfc3339, rfc3339
from ummanu.po.store import REQUEST_OPERATIONS

PO_FILE = "po_sql.json"
LEGACY_PO_COVERAGE = "checkpoint has no PO SQL coverage; sessions, turns, feed and request bindings are unavailable"
RESTORED_PO_COVERAGE = (
    "PO SQL imported at parity, restored contexts prepared and recovery admission finished; "
    "queue, attachments and provider readiness are not restored"
)

# Explicit SQL columns, including nullable values; no display projections or read limits.
PO_COLUMNS = {
    "po_sessions": (
        "session_id", "cli", "model", "cwd", "created_at", "state", "cli_session_id",
        "closed_at", "closed_by", "effort", "title",
    ),
    "po_turns": (
        "session_id", "seq", "started_at", "finished_at", "state", "stdout_path", "pid",
        "process_identity", "reason", "resolved_model",
    ),
    "po_feed": ("entry_id", "session_id", "turn_seq", "role", "text", "created_at", "metadata"),
    "po_requests": ("request_id", "operation", "fingerprint", "session_id", "seq", "created_at"),
}
_ORDER = {"po_sessions": "session_id COLLATE \"C\"", "po_turns": "session_id COLLATE \"C\", seq",
          "po_feed": "entry_id", "po_requests": "request_id COLLATE \"C\""}
_TIMES = {"created_at", "closed_at", "started_at", "finished_at"}
_NULLABLE = {"cli_session_id", "closed_at", "closed_by", "title", "finished_at", "pid",
             "process_identity", "reason", "resolved_model", "metadata"}


class PoCheckpointError(ValueError):
    """PO records cannot be transported without changing their bindings or state."""


def _fail(table: str, index: int, field: str) -> None:
    raise PoCheckpointError(f"PO SQL {table} record {index}: {field} mismatch or invalid")


def export_po_records(client: Any) -> dict[str, list[dict[str, Any]]]:
    """Read all four tables on the caller's active SQL read snapshot, without writes."""
    result = {}
    for table, columns in PO_COLUMNS.items():
        rows = client._query(f"SELECT {', '.join(columns)} FROM {table} ORDER BY {_ORDER[table]}")
        result[table] = [
            {field: rfc3339(value, microseconds=True) if field in _TIMES and value is not None else value
             for field, value in zip(columns, values, strict=True)}
            for values in rows
        ]
    return result


def validate_po_records(records: Any) -> None:
    """Require exact fields, SQL types, source identity/order and all PO foreign keys and states."""
    if not isinstance(records, dict) or set(records) != set(PO_COLUMNS):
        raise PoCheckpointError("PO SQL table coverage is invalid")
    for table, columns in PO_COLUMNS.items():
        if not isinstance(records[table], list):
            raise PoCheckpointError(f"PO SQL {table} must be a list")
        for index, row in enumerate(records[table]):
            if not isinstance(row, dict) or set(row) != set(columns):
                _fail(table, index, "fields")
            for field, value in row.items():
                if value is None and (field in _NULLABLE or (table == "po_requests" and field == "seq")):
                    continue
                if field in _TIMES:
                    moment = parse_rfc3339(value) if isinstance(value, str) else None
                    if moment is None or moment.tzinfo is None or rfc3339(moment, microseconds=True) != value:
                        _fail(table, index, field)
                elif field in {"seq", "turn_seq", "entry_id", "pid"}:
                    limit = 2**63 if field == "entry_id" else 2**31
                    if type(value) is not int or not 0 < value < limit:
                        _fail(table, index, field)
                elif field == "metadata":
                    if not isinstance(value, dict):
                        _fail(table, index, field)
                    try:
                        json.dumps(value, allow_nan=False)
                    except (TypeError, ValueError):
                        _fail(table, index, field)
                elif not isinstance(value, str) or "\x00" in value:
                    _fail(table, index, field)
                elif field in {"session_id", "request_id"} and not value:
                    _fail(table, index, field)
    sessions = {}
    last = None
    for index, row in enumerate(records["po_sessions"]):
        identity = row["session_id"]
        if last is not None and identity <= last:
            _fail("po_sessions", index, "session_id/order")
        last = identity
        sessions[identity] = row
        if row["cli"] not in {"claude", "codex"}:
            _fail("po_sessions", index, "cli")
        if row["state"] not in {"open", "closed"} or (
            (row["state"] == "closed") != (row["closed_at"] is not None)
            or (row["state"] == "closed") != (row["closed_by"] is not None)
        ):
            _fail("po_sessions", index, "state/close audit")
    turns = set()
    running = set()
    last = None
    for index, row in enumerate(records["po_turns"]):
        identity = (row["session_id"], row["seq"])
        if last is not None and identity <= last:
            _fail("po_turns", index, "session_id/seq/order")
        last = identity
        turns.add(identity)
        if row["session_id"] not in sessions:
            _fail("po_turns", index, "session_id")
        if row["state"] not in {"running", "completed", "failed", "interrupted"} or (
            (row["state"] == "running") != (row["finished_at"] is None)
        ):
            _fail("po_turns", index, "state/finished_at")
        if row["state"] == "running":
            if row["session_id"] in running:
                _fail("po_turns", index, "one running turn")
            running.add(row["session_id"])
    last_id = 0
    for index, row in enumerate(records["po_feed"]):
        if row["entry_id"] <= last_id:
            _fail("po_feed", index, "entry_id/order")
        last_id = row["entry_id"]
        if (row["session_id"], row["turn_seq"]) not in turns:
            _fail("po_feed", index, "session_id/turn_seq")
        if row["role"] not in {"owner", "agent"}:
            _fail("po_feed", index, "role")
    last = None
    for index, row in enumerate(records["po_requests"]):
        identity = row["request_id"]
        if last is not None and identity <= last:
            _fail("po_requests", index, "request_id/order")
        last = identity
        if row["operation"] not in REQUEST_OPERATIONS or (
            (row["operation"] == "po_send") != (row["seq"] is not None)
        ):
            _fail("po_requests", index, "operation/seq")
        if row["session_id"] not in sessions or (
            row["seq"] is not None and (row["session_id"], row["seq"]) not in turns
        ):
            _fail("po_requests", index, "session_id/seq")
        if not re.fullmatch(r"[0-9a-f]{64}", row["fingerprint"]):
            _fail("po_requests", index, "fingerprint")


def po_coverage(records: dict[str, list[dict[str, Any]]], payload: bytes) -> dict[str, Any]:
    """Bind explicit complete table counts to the exact PO file bytes, including empty tables."""
    return {"version": 1, "counts": {table: len(rows) for table, rows in records.items()},
            "sha256": hashlib.sha256(payload).hexdigest()}


def read_checkpoint_po(directory: Path) -> dict[str, list[dict[str, Any]]] | None:
    """Validate declared PO coverage; only an absent declaration and file mean legacy absence."""
    try:
        board = open_checkpoint_board(directory)
        summary = read_export_summary(board)
        if "po_sql" not in summary:
            if board.has(PO_FILE):
                raise PoCheckpointError("undeclared PO SQL coverage")
            return None
        coverage = summary["po_sql"]
        if not isinstance(coverage, dict) or type(coverage.get("version")) is not int or coverage["version"] != 1:
            raise PoCheckpointError("unsupported PO SQL coverage")
        payload = board.read_bytes(PO_FILE)
        records = json.loads(payload)
        validate_po_records(records)
        if coverage != po_coverage(records, payload) or any(
            type(count) is not int for count in coverage["counts"].values()
        ):
            raise PoCheckpointError("PO SQL count/digest mismatch")
        return records
    except PoCheckpointError:
        raise
    except (OSError, UnicodeError, ValueError):
        raise PoCheckpointError("PO SQL checkpoint is unreadable or invalid") from None


def scan_po_secrets(text: str, scrub: Callable[[str], str]) -> None:
    """Scan decoded fields as well as their serialized form, naming only table/ordinal/field."""
    records = json.loads(text)
    validate_po_records(records)
    for table, rows in records.items():
        for index, row in enumerate(rows):
            for field, value in row.items():
                serialized = json.dumps(value, ensure_ascii=False)
                raw = value if isinstance(value, str) else serialized
                if scrub(raw) != raw or scrub(serialized) != serialized or nested_secret(value, scrub):
                    raise PoCheckpointError(f"secret detected in PO SQL {table} record {index} field {field}")
    if scrub(text) != text:
        raise PoCheckpointError("secret detected in PO SQL file structure")


def nested_secret(value: Any, scrub: Callable[[str], str]) -> bool:
    """Whether any decoded string, mapping key or nested item changes under `scrub`."""
    if isinstance(value, str):
        return scrub(value) != value
    if isinstance(value, dict):
        return any(nested_secret(key, scrub) or nested_secret(item, scrub) for key, item in value.items())
    if isinstance(value, list):
        return any(nested_secret(item, scrub) for item in value)
    return False
