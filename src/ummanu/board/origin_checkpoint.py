"""Portable origin obligations: complete state bound to the exported cards and audit."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from ummanu._fsutil import ndjson_lines
from ummanu.board.checkpoint_layout import open_checkpoint_board
from ummanu.board.origin_outbox import STATUSES, TERMINAL_STATES, OutboxRow, owed_target
from ummanu.board.sql_rows import parse_rfc3339, rfc3339

OUTBOX_FILE = "origin_returns.ndjson"
LEGACY_COVERAGE = "checkpoint has no origin outbox coverage; unknown deliveries were not reconstructed"


class OriginCheckpointError(ValueError):
    """An origin obligation cannot be transferred without changing delivery semantics."""


def _fail(row: dict[str, Any], field: str) -> None:
    # Diagnostics identify the obligation without printing notice, session or request contents.
    identity = [str(value)[:160] for value in (row.get("event_id", "?"), row.get("task_ref", row.get("ref", "?")))]
    raise OriginCheckpointError(
        f"origin return event/card {json.dumps(identity, ensure_ascii=True)}: {field} mismatch or invalid"
    )


def validate_outbox_records(
    rows: list[dict[str, Any]], cards: list[dict[str, Any]], history: list[dict[str, Any]],
) -> None:
    """Validate delivery state, strict ordering and each obligation's card and committed audit link."""
    if any(
        not isinstance(card, dict) or not isinstance(card.get("metadata", {}), dict)
        or not isinstance(card.get("reference"), str)
        for card in cards
    ):
        raise OriginCheckpointError("origin return cards are invalid")
    card_refs = {
        card.get("reference") for card in cards
        if card.get("metadata", {}).get("record_type", "task") == "task"
    }
    events: dict[str, dict[str, Any]] = {}
    requests: set[str] = set()
    for event in history:
        if not isinstance(event, dict):
            raise OriginCheckpointError("origin return audit contains an invalid event")
        event_id = event.get("event_id")
        if not isinstance(event_id, str):
            raise OriginCheckpointError("origin return audit event identity is invalid")
        if event_id in events:
            _fail(event, "audit.event_id")
        request_id = event.get("request_id")
        if not isinstance(request_id, str) or not request_id or request_id in requests:
            _fail(event, "audit.request_id")
        requests.add(request_id)
        events[event_id] = event
    seen: set[str] = set()
    last = 0
    for row in rows:
        if not isinstance(row, dict):
            raise OriginCheckpointError("origin return record must be an object")
        if set(row) != set(OutboxRow.__dataclass_fields__):
            _fail(row, "fields")
        if type(row["id"]) is not int or not last < row["id"] < 2**63:
            _fail(row, "id/order")
        last = row["id"]
        for field in ("event_id", "task_ref", "request_id"):
            if not isinstance(row[field], str) or not row[field] or "\x00" in row[field]:
                _fail(row, field)
        if row["event_id"] in seen:
            _fail(row, "event_id")
        seen.add(row["event_id"])
        if row["task_ref"] not in card_refs:
            _fail(row, "task_ref")
        if row["target_state"] not in TERMINAL_STATES:
            _fail(row, "target_state")
        for field in ("notice", "session", "po_request_id"):
            if row[field] is not None and (not isinstance(row[field], str) or "\x00" in row[field]):
                _fail(row, field)
        for field in ("created_at", "delivered_at"):
            if row[field] is None and field == "delivered_at":
                continue
            moment = parse_rfc3339(row[field])
            if not isinstance(row[field], str) or moment is None or moment.tzinfo is None:
                _fail(row, field)
            if rfc3339(moment, microseconds=True) != row[field]:
                _fail(row, field)
        if row["delivered_at"] is None:
            if any(row[field] is not None for field in ("status", "session", "po_request_id")):
                _fail(row, "delivery")
        elif row["status"] not in STATUSES:
            _fail(row, "status")
        event = events.get(row["event_id"])
        if event is None:
            _fail(row, "audit.event_id")
        for field, key in (("task_ref", "ref"), ("request_id", "request_id")):
            if row[field] != event.get(key):
                _fail(row, f"audit.{field}")
        if row["target_state"] != owed_target(event):
            _fail(row, "audit.target_state")


def read_checkpoint_outbox(directory: Path) -> list[dict[str, Any]] | None:
    """Read a versioned complete outbox, or None for released checkpoints without coverage."""
    try:
        board = open_checkpoint_board(directory)
        summary = json.loads(board.read_text("export.json")) if board.has("export.json") else {"version": 1}
        if not isinstance(summary, dict):
            raise OriginCheckpointError("invalid board export metadata")
        version = summary.get("version", 1)
        if type(version) is not int or version not in (1, 2):
            raise OriginCheckpointError("unsupported board export version")
        if version == 1:
            if board.has(OUTBOX_FILE) or any(key.startswith("origin_return") for key in summary):
                raise OriginCheckpointError("unversioned origin return coverage")
            return None
        payload = board.read_bytes(OUTBOX_FILE)
        rows = [json.loads(line) for line in ndjson_lines(payload.decode("utf-8")) if line.strip()]
        count = summary.get("origin_return_count")
        digest = summary.get("origin_return_sha256")
        if type(count) is not int or count != len(rows):
            raise OriginCheckpointError("origin return count mismatch")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or (
            digest != hashlib.sha256(payload).hexdigest()
        ):
            raise OriginCheckpointError("origin return digest mismatch")
        cards = [json.loads(line) for line in ndjson_lines(board.read_text("cards.ndjson")) if line.strip()]
        history = [json.loads(line) for line in ndjson_lines(board.read_text("audit.ndjson")) if line.strip()]
        audit_json = directory / "audit.json"
        if audit_json.exists():
            audit_payload = json.loads(audit_json.read_text(encoding="utf-8"))
            if not isinstance(audit_payload, dict) or audit_payload.get("events") != history:
                raise OriginCheckpointError("origin return audit.json/audit.ndjson parity mismatch")
        validate_outbox_records(rows, cards, history)
        return rows
    except (OSError, UnicodeError, ValueError) as exc:
        if isinstance(exc, OriginCheckpointError):
            raise
        raise OriginCheckpointError("origin return checkpoint is unreadable or invalid") from None


def require_outbox_parity(expected: list[dict[str, Any]], actual: list[dict[str, Any]]) -> None:
    """Prove all delivery fields and relative order; database-local identity numbers may differ."""
    indexed = {row["event_id"]: row for row in actual}
    for row in expected:
        other = indexed.get(row["event_id"])
        if other is None:
            _fail(row, "event_id")
        for field in row:
            if field != "id" and row[field] != other[field]:
                _fail(row, field)
    wanted = [row["event_id"] for row in expected]
    found = [row["event_id"] for row in actual]
    if wanted != found:
        wanted_set = set(wanted)
        difference = next((row for row in actual if row["event_id"] not in wanted_set), None)
        _fail(difference or (expected or actual)[0], "event_id/order")
