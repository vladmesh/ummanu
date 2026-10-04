"""Quoted sprint authority. Array order is owner-answer order, never timestamp order.

Latest production answer wins for that project. Latest e2e grant/refusal wins for admission;
each grant adds its finite runs once, without erasing earlier charges or unused budget.
Advance consents are bounded data for the PO/observer, not implicit budget grants.
Released permissions/counters remain the baseline when there is no quoted answer.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

FIELD = "sprint_owner_decisions"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\Z")
_PROJECT = re.compile(r"[a-z][a-z0-9_-]*\Z")
INPUT_FIELDS = frozenset({"id", "scope", "kind", "value", "quotation"})


def parse_decisions(value: Any) -> list[dict[str, Any]]:
    """Validate the complete input before any write; preserve the quotation verbatim."""
    if not isinstance(value, list):
        raise ValueError("owner_decisions must be a JSON list")
    result = []
    seen = set()
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != INPUT_FIELDS:
            raise ValueError("each owner decision needs exactly id, scope, kind, value, quotation")
        identifier, scope, kind, quotation = (entry[key] for key in ("id", "scope", "kind", "quotation"))
        if not isinstance(identifier, str) or not _ID.fullmatch(identifier) or identifier in seen:
            raise ValueError("owner decision id must be a unique addressable token (1..160 characters)")
        if not isinstance(quotation, str) or not quotation.strip():
            raise ValueError("owner decision requires a non-empty verbatim owner quotation")
        setting = entry["value"]
        if kind == "production":
            if not isinstance(scope, str) or not _PROJECT.fullmatch(scope) or type(setting) is not bool:
                raise ValueError("production decision needs a registered project scope and boolean value")
        elif kind == "e2e_grant":
            if scope != "sprint" or type(setting) is not int or not 1 <= setting <= 2_147_483_647:
                raise ValueError("e2e_grant needs scope sprint and a finite positive integer of runs")
        elif kind == "e2e_refusal":
            if scope != "sprint" or setting != "no_more_e2e":
                raise ValueError("e2e_refusal needs scope sprint and value no_more_e2e")
        elif kind == "advance_consent":
            if scope != "sprint" and (not isinstance(scope, str) or not _PROJECT.fullmatch(scope)):
                raise ValueError("advance_consent scope must be sprint or a registered project")
            if (
                not isinstance(setting, dict) or set(setting) != {"action", "max_uses"}
                or not isinstance(setting["action"], str) or not setting["action"].strip()
                or type(setting["max_uses"]) is not int or not 1 <= setting["max_uses"] <= 2_147_483_647
            ):
                raise ValueError("advance_consent needs explicit action and finite positive max_uses")
        else:
            raise ValueError(f"unknown owner decision kind {kind!r}")
        seen.add(identifier)
        result.append({key: entry[key] for key in sorted(INPUT_FIELDS)})
    return result


def stored_decisions(raw: Any) -> list[dict[str, Any]]:
    entries = json.loads(raw or "[]") if isinstance(raw, str) or raw is None else raw
    if not isinstance(entries, list):
        raise ValueError("stored owner decisions must be an array")
    parse_decisions([{key: entry[key] for key in INPUT_FIELDS} for entry in entries])
    for entry in entries:
        attribution = entry.get("recorded_by")
        if set(entry) != INPUT_FIELDS | {"recorded_by"} or not isinstance(attribution, dict):
            raise ValueError("stored owner decision needs audit attribution")
        if set(attribution) != {"role", "actor", "request_id", "event_id", "at"} or any(
            not isinstance(item, str) or not item.strip() for item in attribution.values()
        ) or attribution["role"] != "po":
            raise ValueError("stored owner decision has invalid audit attribution")
    return entries


def attributed(entries: list[dict[str, Any]], event: Mapping[str, Any]) -> list[dict[str, Any]]:
    actor = event["actor"]
    return [
        {**entry, "recorded_by": {"role": actor["role"], "actor": actor["id"],
                                 "request_id": event["request_id"], "event_id": event["event_id"],
                                 "at": event["occurred_at"]}}
        for entry in entries
    ]


def input_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    return {key: entry[key] for key in INPUT_FIELDS}


def productions(baseline: list[str], entries: list[dict[str, Any]]) -> list[str]:
    allowed = list(baseline)
    for entry in entries:
        if entry["kind"] == "production":
            project = entry["scope"]
            if entry["value"] and project not in allowed:
                allowed.append(project)
            elif not entry["value"] and project in allowed:
                allowed.remove(project)
    return allowed


def e2e_refusal(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    for entry in reversed(entries):
        if entry["kind"] in {"e2e_grant", "e2e_refusal"}:
            return entry if entry["kind"] == "e2e_refusal" else None
    return None


def refusal_text(sprint: str, entry: Mapping[str, Any]) -> str:
    return (f"No new e2e run is dispatched. Standing owner decision {sprint}/{entry['id']} "
            f"refuses further e2e in this sprint.\n\nOwner quotation:\n\n{entry['quotation']}")
