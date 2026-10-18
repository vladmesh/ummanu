"""Typed sprint close facts carried by the normalized sprint checkpoint."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from ummanu._fsutil import ndjson_lines
from ummanu.board.checkpoint_layout import open_checkpoint_board, read_export_summary, unique_json_fields
from ummanu.board.sql_rows import parse_rfc3339, rfc3339

DECISION_FIELDS = (
    "decision_id", "sprint_ref", "subject_kind", "issue_id", "task_ref", "verdict", "actual",
    "reason", "request_id", "decided_at",
)
LEGACY_CLOSE_COVERAGE = "checkpoint has no sprint close facts coverage; typed close parity is unavailable"


class CloseCheckpointError(ValueError):
    """Declared close facts cannot be restored without changing stored semantics."""


def _fail(index: int, field: str) -> None:
    raise CloseCheckpointError(f"sprint close record {index}: {field} mismatch or invalid")


def decision_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return row["sprint_ref"], row["subject_kind"], row["issue_id"] or row["task_ref"]


def validate_close_facts(facts: dict[str, Any]) -> None:
    """Validate nullable columns and complete decisions in source identity order."""
    if not isinstance(facts, dict) or set(facts) != {"close_reason", "closeout_document", "decisions"}:
        _fail(0, "close_facts.fields")
    for field in ("close_reason", "closeout_document"):
        value = facts[field]
        if value is not None and (not isinstance(value, str) or "\x00" in value):
            _fail(0, field)
    document = facts["closeout_document"]
    if document is not None:
        path = PurePosixPath(document)
        if not path.parts or path.is_absolute() or ".." in path.parts or str(path) != document or "\\" in document:
            _fail(0, "closeout_document")
    rows = facts["decisions"]
    if not isinstance(rows, list):
        _fail(0, "decisions")
    last = 0
    seen = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != set(DECISION_FIELDS):
            _fail(index, "fields")
        if type(row["decision_id"]) is not int or not last < row["decision_id"] < 2**63:
            _fail(index, "decision_id/order")
        last = row["decision_id"]
        for field in DECISION_FIELDS[1:]:
            value = row[field]
            if value is None and field in {"issue_id", "task_ref", "actual"}:
                continue
            if not isinstance(value, str) or "\x00" in value:
                _fail(index, field)
        kind = row["subject_kind"]
        if kind not in {"issue", "card"}:
            _fail(index, "subject_kind")
        subject, other = ("issue_id", "task_ref") if kind == "issue" else ("task_ref", "issue_id")
        if not row[subject] or row[other] is not None:
            _fail(index, "subject")
        verdicts = {"resolved", "invalid", "duplicate", "wont_do", "open", "already_closed"}
        if row["verdict"] not in (verdicts if kind == "issue" else {"done", "drop", "already_moved"}):
            _fail(index, "verdict")
        if (row["verdict"] in {"already_closed", "already_moved"}) != (row["actual"] is not None):
            _fail(index, "actual")
        if row["actual"] is not None and row["actual"] not in (
            {"resolved", "invalid", "duplicate", "wont_do"} if kind == "issue" else {"ready", "done"}
        ):
            _fail(index, "actual")
        if not row["reason"] or not row["request_id"] or not row["sprint_ref"]:
            _fail(index, "reason/request_id/sprint_ref")
        moment = parse_rfc3339(row["decided_at"])
        if moment is None or moment.tzinfo is None or rfc3339(moment, microseconds=True) != row["decided_at"]:
            _fail(index, "decided_at")
        key = decision_key(row)
        if key in seen:
            _fail(index, "subject/duplicate")
        seen.add(key)


def close_coverage(sprints: list[dict[str, Any]], payload: bytes) -> dict[str, Any]:
    return {"version": 1, "sprint_count": len(sprints),
            "decision_count": sum(len(sprint["close_facts"]["decisions"]) for sprint in sprints),
            "sha256": hashlib.sha256(payload).hexdigest()}


def read_checkpoint_close_facts(directory: Path) -> dict[str, dict[str, Any]] | None:
    """Validate declared coverage and all source bindings before any restore write."""
    try:
        board = open_checkpoint_board(directory)
        summary = read_export_summary(board)
        sprints = [json.loads(line, object_pairs_hook=unique_json_fields)
                   for line in ndjson_lines(board.read_text("sprints.ndjson")) if line.strip()] if board.has("sprints.ndjson") else []
        local_json = directory / "sprints.json"
        if local_json.is_symlink():
            _fail(0, "sprints.json/symlink")
        local = json.loads(local_json.read_text(), object_pairs_hook=unique_json_fields) if local_json.exists() else None
        if "sprint_close" not in summary:
            if any("close_facts" in sprint for sprint in sprints) or (
                local is not None and any("close_facts" in sprint for sprint in local["sprints"])
            ):
                _fail(0, "undeclared coverage")
            return None
        coverage = summary["sprint_close"]
        if not isinstance(coverage, dict) or type(coverage.get("version")) is not int or coverage["version"] != 1:
            _fail(0, "coverage.version")
        if local is not None and local["sprints"] != sprints:
            _fail(0, "sprints.json/sprints.ndjson parity")
        cards = [json.loads(line, object_pairs_hook=unique_json_fields)
                 for line in ndjson_lines(board.read_text("cards.ndjson")) if line.strip()]
        history = [json.loads(line, object_pairs_hook=unique_json_fields)
                   for line in ndjson_lines(board.read_text("audit.ndjson")) if line.strip()]
        audit_json = directory / "audit.json"
        if audit_json.is_symlink():
            _fail(0, "audit.json/symlink")
        if audit_json.exists() and json.loads(audit_json.read_text(), object_pairs_hook=unique_json_fields)["events"] != history:
            _fail(0, "audit.json/audit.ndjson parity")
        if any(not isinstance(card, dict) or not isinstance(card.get("reference"), str)
               or not isinstance(card.get("metadata", {}), dict) for card in cards):
            _fail(0, "cards.fields")
        card_refs = {card["reference"]: card for card in cards}
        cards_json = directory / "cards.json"
        if cards_json.is_symlink():
            _fail(0, "cards.json/symlink")
        if cards_json.exists() and json.loads(cards_json.read_text(), object_pairs_hook=unique_json_fields)["cards"] != cards:
            _fail(0, "cards.json/cards.ndjson parity")
        if len(card_refs) != len(cards):
            _fail(0, "cards.reference/duplicate")
        requests = {}
        for event in history:
            request = event["request_id"]
            if not isinstance(request, str) or not request or not isinstance(event["ref"], str):
                _fail(0, "audit.request_id/ref")
            if request in requests:
                _fail(0, "audit.request_id/duplicate")
            requests[request] = event["ref"]
        result = {}
        ids = set()
        for index, sprint in enumerate(sprints):
            reference = sprint["reference"]
            if not isinstance(reference, str) or not reference.startswith("sprint:") or reference in result:
                _fail(index, "sprint_ref")
            facts = sprint["close_facts"]
            if not isinstance(sprint.get("issues", []), list) or any(not isinstance(issue, str) for issue in sprint.get("issues", [])):
                _fail(index, "issues")
            validate_close_facts(facts)
            result[reference] = facts
            for row in facts["decisions"]:
                if row["decision_id"] in ids:
                    _fail(index, "decision_id/duplicate")
                ids.add(row["decision_id"])
                if row["sprint_ref"] != reference or requests.get(row["request_id"]) != reference:
                    _fail(index, "sprint_ref/request_id binding")
                if row["subject_kind"] == "issue":
                    issue = "issue:" + row["issue_id"]
                    if issue not in sprint.get("issues", []) or card_refs.get(issue, {}).get("metadata", {}).get("record_type") != "issue":
                        _fail(index, "issue_id/membership")
                else:
                    card = card_refs.get(row["task_ref"], {})
                    if card.get("metadata", {}).get("sprint_ref") != reference or card.get("metadata", {}).get("record_type", "task") != "task":
                        _fail(index, "task_ref/membership")
        if any(type(coverage.get(field)) is not int for field in ("sprint_count", "decision_count")) or coverage != close_coverage(sprints, board.read_bytes("sprints.ndjson")):
            _fail(0, "coverage.count/digest")
        return result
    except CloseCheckpointError:
        raise
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        raise CloseCheckpointError("sprint close checkpoint is unreadable or invalid") from None


def require_close_parity(expected: dict[str, Any], actual: dict[str, Any]) -> None:
    """Prove columns, all decision fields, subjects and order while allowing local IDs."""
    for field in ("close_reason", "closeout_document"):
        if expected[field] != actual[field]:
            _fail(0, field)
    canonical = lambda rows: [{key: value for key, value in row.items() if key != "decision_id"} for row in rows]
    if canonical(expected["decisions"]) != canonical(actual["decisions"]):
        _fail(0, "decisions/fields/order")


def scan_close_secrets(text: str, scrub: Callable[[str], str]) -> None:
    """Scan decoded close prose so JSON escaping cannot conceal known secrets."""
    sprints = [json.loads(line) for line in ndjson_lines(text) if line.strip()]
    for index, sprint in enumerate(sprints):
        if "close_facts" not in sprint:
            continue
        facts = sprint["close_facts"]
        validate_close_facts(facts)
        values = [facts["close_reason"], facts["closeout_document"],
                  *(value for row in facts["decisions"] for value in row.values())]
        if any(isinstance(value, str) and scrub(value) != value for value in values):
            _fail(index, "secret detected")


def require_close_set_parity(expected: dict[str, Any], actual: dict[str, Any]) -> None:
    """Prove the complete sprint set and global decision order, including timestamp ties."""
    if set(expected) != set(actual):
        _fail(0, "sprint_ref")
    for reference, facts in expected.items():
        require_close_parity(facts, actual[reference])
    order = lambda facts: [decision_key(row) for row in sorted(
        (row for value in facts.values() for row in value["decisions"]), key=lambda row: row["decision_id"]
    )]
    if order(expected) != order(actual):
        _fail(0, "decisions/global order")


def validate_close_merge(expected: dict[str, Any], existing: dict[str, Any]) -> None:
    """Refuse conflicting columns or extra typed identities before modifying a target."""
    if set(existing) - set(expected):
        _fail(0, "sprint_ref/extra")
    for reference, prior in existing.items():
        value = expected[reference]
        for field in ("close_reason", "closeout_document"):
            if (prior[field] is not None or prior["decisions"]) and prior[field] != value[field]:
                _fail(0, field)
        wanted = {decision_key(row): row for row in value["decisions"]}
        for row in prior["decisions"]:
            target = wanted.get(decision_key(row))
            if target is None:
                _fail(0, "subject/extra")
            require_close_parity({**value, "decisions": [target]}, {**value, "decisions": [row]})
