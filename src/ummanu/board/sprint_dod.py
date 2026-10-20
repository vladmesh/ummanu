"""Addressable Definition of Done items of a sprint, their weights and the progress they give.

A sprint carries either an ordered item list (structured) or only its free-text DoD
(unstructured); the list is never derived from the text. An item is open, met or descoped: a met
item names its evidence, a descoped one who took it out and why. Progress is the met share of the
summed weights of the items still in scope.
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
DESCOPED = "descoped"
UNSTRUCTURED = "this sprint's Definition of Done is free text, not addressable items"
UNESTIMATED = "not every item is weighed yet, so every item counts as weight 1"
#: The reason an owner close files each unmet item as a new issue.
OWNER_CLOSE_REASON = "sprint closed early by owner, item not met"

_ID = re.compile(r"d[1-9][0-9]*\Z")
_KEYS = frozenset({"id", "text", "state", "weight", "weight_reason", "evidence", "met_by"})
_MET_BY = frozenset({"role", "actor", "at"})
#: Present on a descoped item only, so an item stored before descoping existed reads unchanged.
_DESCOPED_KEY = "descoped"
_DESCOPED_BY = frozenset({*_MET_BY, "reason"})


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
        if not isinstance(item, dict) or set(item) - {_DESCOPED_KEY} != _KEYS:
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
        descoped = item["state"] == DESCOPED
        if (
            item["state"] not in {OPEN, MET, DESCOPED}
            or met != (item["evidence"] is not None)
            or met != (item["met_by"] is not None)
            or descoped != (_DESCOPED_KEY in item)
        ):
            raise ValueError(f"Definition of Done item {identifier} has an inconsistent state")
        if met and (not str(item["evidence"]).strip() or not _valid_record(item["met_by"], _MET_BY)):
            raise ValueError(f"Definition of Done item {identifier} is met without evidence and author")
        if descoped and not _valid_record(item[_DESCOPED_KEY], _DESCOPED_BY):
            raise ValueError(f"Definition of Done item {identifier} is descoped without author and reason")
    return items


def _valid_record(value: Any, keys: frozenset[str]) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == keys
        and all(isinstance(part, str) and part.strip() for part in value.values())
    )


def encode(items: list[dict[str, Any]]) -> str:
    return json.dumps(items, sort_keys=True, separators=(",", ":"))


def find(items: list[dict[str, Any]], item_id: str) -> dict[str, Any]:
    for item in items:
        if item["id"] == item_id:
            return item
    raise ValueError(f"this sprint has no Definition of Done item {item_id!r}")


def _in_scope(items: list[dict[str, Any]], item_id: str) -> dict[str, Any]:
    item = find(items, item_id)
    if item["state"] == DESCOPED:
        raise ValueError(f"Definition of Done item {item_id} is descoped")
    return item


def weighed(items: list[dict[str, Any]], item_id: str, weight: int, reason: str) -> list[dict[str, Any]]:
    if weight not in WEIGHTS:
        raise ValueError(f"a weight is one of {', '.join(map(str, WEIGHTS))}; not {weight!r}")
    if not reason.strip():
        raise ValueError("a weight needs the reason for it")
    _in_scope(items, item_id)
    return [
        {**item, "weight": weight, "weight_reason": reason.strip()} if item["id"] == item_id else item
        for item in items
    ]


def met(
    items: list[dict[str, Any]], item_id: str, evidence: str, by: Mapping[str, str]
) -> list[dict[str, Any]]:
    if not evidence.strip():
        raise ValueError("a met item names its evidence: a card, a PR or a sprint comment")
    if _in_scope(items, item_id)["state"] == MET:
        raise ValueError(f"Definition of Done item {item_id} is already met; reopen it first")
    return [
        {**item, "state": MET, "evidence": evidence.strip(), "met_by": dict(by)}
        if item["id"] == item_id
        else item
        for item in items
    ]


def reopened(items: list[dict[str, Any]], item_id: str) -> list[dict[str, Any]]:
    if _in_scope(items, item_id)["state"] != MET:
        raise ValueError(f"Definition of Done item {item_id} is not met")
    return [
        {**item, "state": OPEN, "evidence": None, "met_by": None} if item["id"] == item_id else item
        for item in items
    ]


def reworded(items: list[dict[str, Any]], item_id: str, text: str) -> list[dict[str, Any]]:
    """The items with one open or met item's wording replaced; its state, weight and evidence stay."""
    wording = text.strip()
    if not wording:
        raise ValueError("a Definition of Done item needs its wording")
    _in_scope(items, item_id)
    return [{**item, "text": wording} if item["id"] == item_id else item for item in items]


def descoped(
    items: list[dict[str, Any]], item_id: str, reason: str, by: Mapping[str, str]
) -> list[dict[str, Any]]:
    """The items with one open item taken out of scope; there is no way back."""
    if not reason.strip():
        raise ValueError("descoping an item needs the reason for it")
    state = _in_scope(items, item_id)["state"]
    if state != OPEN:
        raise ValueError(f"Definition of Done item {item_id} is {state}; only an open item is descoped")
    return [
        {**item, "state": DESCOPED, _DESCOPED_KEY: {**by, "reason": reason.strip()}}
        if item["id"] == item_id
        else item
        for item in items
    ]


def items_of(view: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    """The stored items behind a `section` view, or None for an unstructured sprint."""
    if not view["structured"]:
        return None
    return [
        {key: value for key, value in item.items() if key in _KEYS or key == _DESCOPED_KEY}
        for item in view["items"]
    ]


def unmet(items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [item for item in items or [] if item["state"] == OPEN]


def section(items: list[dict[str, Any]] | None) -> dict[str, Any]:
    """What every reader shows: the items with their scale share, and the progress they give.

    Until every item in scope is weighed, every item counts as weight 1. A descoped item
    has no share and is left out of `total` and `progress`.
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
    counted = [item for item in items if item["state"] != DESCOPED]
    estimated = bool(counted) and all(item["weight"] is not None for item in counted)
    weights = [0 if item["state"] == DESCOPED else item["weight"] if estimated else 1 for item in items]
    whole = sum(weights)
    done = sum(weight for weight, item in zip(weights, items, strict=True) if item["state"] == MET)
    return {
        "structured": True,
        "reason": None if estimated else UNESTIMATED,
        "items": [
            {**item, "share": weight / whole if whole else 0.0}
            for weight, item in zip(weights, items, strict=True)
        ],
        "estimated": estimated,
        "met": sum(1 for item in items if item["state"] == MET),
        "total": len(counted),
        "progress": done / whole if whole else None,
    }
