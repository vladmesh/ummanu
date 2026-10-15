"""Addressable Definition of Done items of a sprint, their weights and the progress they give.

A sprint carries either an ordered item list (structured) or only its free-text DoD
(unstructured); the list is never derived from the text. An item is open or met, and a met
item names its evidence. Progress is the met share of the summed weights.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

FIELD = "sprint_dod_items"
WEIGHTS = (1, 2, 3, 5, 8, 13)
OPEN = "open"
MET = "met"
UNSTRUCTURED = "this sprint's Definition of Done is free text, not addressable items"
UNESTIMATED = "not every item is weighed yet, so every item counts as weight 1"
#: The reason an owner close files each unmet item as a new issue.
OWNER_CLOSE_REASON = "sprint closed early by owner, item not met"

_ID = re.compile(r"d[1-9][0-9]*\Z")
_KEYS = frozenset({"id", "text", "state", "weight", "weight_reason", "evidence", "met_by"})
_MET_BY = frozenset({"role", "actor", "at"})


def new_items(texts: list[str], *, existing: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Open, unweighed items for `texts`, numbered after the highest id in `existing`."""
    items = list(existing or [])
    number = max((int(item["id"][1:]) for item in items), default=0)
    for text in texts:
        wording = str(text).strip()
        if not wording:
            raise ValueError("a Definition of Done item needs its wording")
        number += 1
        items.append(
            {
                "id": f"d{number}",
                "text": wording,
                "state": OPEN,
                "weight": None,
                "weight_reason": None,
                "evidence": None,
                "met_by": None,
            }
        )
    return items


def stored_items(raw: Any) -> list[dict[str, Any]] | None:
    """The stored list, or None for an unstructured sprint; anything malformed is an error."""
    if raw is None or raw == "":
        return None
    items = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(items, list):
        raise TypeError("stored Definition of Done items must be a list")
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != _KEYS:
            raise ValueError("a stored Definition of Done item has exactly " + ", ".join(sorted(_KEYS)))
        identifier = item["id"]
        if not isinstance(identifier, str) or not _ID.fullmatch(identifier) or identifier in seen:
            raise ValueError(f"Definition of Done item id {identifier!r} is not a unique d<N>")
        seen.add(identifier)
        if not isinstance(item["text"], str) or not item["text"].strip():
            raise ValueError(f"Definition of Done item {identifier} has no wording")
        if item["weight"] is not None and item["weight"] not in WEIGHTS:
            raise ValueError(f"Definition of Done item {identifier} has weight {item['weight']!r}")
        if (item["weight"] is None) != (item["weight_reason"] is None):
            raise ValueError(f"Definition of Done item {identifier} has a weight without its reason")
        met = item["state"] == MET
        if (
            item["state"] not in {OPEN, MET}
            or met != (item["evidence"] is not None)
            or met != (item["met_by"] is not None)
        ):
            raise ValueError(f"Definition of Done item {identifier} has an inconsistent state")
        if met and (not str(item["evidence"]).strip() or not _valid_met_by(item["met_by"])):
            raise ValueError(f"Definition of Done item {identifier} is met without evidence and author")
    return items


def _valid_met_by(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == _MET_BY
        and all(isinstance(part, str) and part.strip() for part in value.values())
    )


def encode(items: list[dict[str, Any]]) -> str:
    return json.dumps(items, sort_keys=True, separators=(",", ":"))


def find(items: list[dict[str, Any]], item_id: str) -> dict[str, Any]:
    for item in items:
        if item["id"] == item_id:
            return item
    raise ValueError(f"this sprint has no Definition of Done item {item_id!r}")


def weighed(items: list[dict[str, Any]], item_id: str, weight: int, reason: str) -> list[dict[str, Any]]:
    if weight not in WEIGHTS:
        raise ValueError(f"a weight is one of {', '.join(map(str, WEIGHTS))}; not {weight!r}")
    if not reason.strip():
        raise ValueError("a weight needs the reason for it")
    find(items, item_id)
    return [
        {**item, "weight": weight, "weight_reason": reason.strip()} if item["id"] == item_id else item
        for item in items
    ]


def met(
    items: list[dict[str, Any]], item_id: str, evidence: str, by: Mapping[str, str]
) -> list[dict[str, Any]]:
    if not evidence.strip():
        raise ValueError("a met item names its evidence: a card, a PR or a sprint comment")
    if find(items, item_id)["state"] == MET:
        raise ValueError(f"Definition of Done item {item_id} is already met; reopen it first")
    return [
        {**item, "state": MET, "evidence": evidence.strip(), "met_by": dict(by)}
        if item["id"] == item_id
        else item
        for item in items
    ]


def reopened(items: list[dict[str, Any]], item_id: str) -> list[dict[str, Any]]:
    if find(items, item_id)["state"] != MET:
        raise ValueError(f"Definition of Done item {item_id} is not met")
    return [
        {**item, "state": OPEN, "evidence": None, "met_by": None} if item["id"] == item_id else item
        for item in items
    ]


def items_of(view: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    """The stored items behind a `section` view, or None for an unstructured sprint."""
    if not view["structured"]:
        return None
    return [{key: value for key, value in item.items() if key in _KEYS} for item in view["items"]]


def unmet(items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [item for item in items or [] if item["state"] != MET]


def section(items: list[dict[str, Any]] | None) -> dict[str, Any]:
    """What every reader shows: the items with their scale share, and the progress they give.

    Until every item is weighed, every item counts as weight 1.
    """
    if items is None:
        return {
            "structured": False,
            "reason": UNSTRUCTURED,
            "items": [],
            "estimated": False,
            "met": 0,
            "total": 0,
            "progress": None,
        }
    estimated = bool(items) and all(item["weight"] is not None for item in items)
    weights = [item["weight"] if estimated else 1 for item in items]
    whole = sum(weights)
    done = sum(weight for weight, item in zip(weights, items, strict=True) if item["state"] == MET)
    return {
        "structured": True,
        "reason": None if estimated else UNESTIMATED,
        "items": [{**item, "share": weight / whole} for weight, item in zip(weights, items, strict=True)],
        "estimated": estimated,
        "met": sum(1 for item in items if item["state"] == MET),
        "total": len(items),
        "progress": done / whole if whole else None,
    }
