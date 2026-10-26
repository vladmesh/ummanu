"""Structured DoD items retain independent delivery and live-verification evidence.
Met progress uses in-scope weights; main delivery uses explicit links and durable facts.
Free-text DoD never supplies inferred items or links.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from ummanu.board.audit_contract import card_transition_of
from ummanu.board.task_protocol.rows import RELEASE_MERGE_KEY, is_merged_release

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
_MAIN_KEY = "in_main_manual"
_MAIN_BY = frozenset({*_MET_BY, "evidence"})


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
        if not isinstance(item, dict) or set(item) - {_DESCOPED_KEY, _MAIN_KEY} != _KEYS:
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
        if _MAIN_KEY in item and not _valid_record(item[_MAIN_KEY], _MAIN_BY):
            raise ValueError(f"Definition of Done item {identifier} is in main without evidence and author")
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


def card_item(sprint: Mapping[str, Any] | None, item_id: str, *, required: bool = True) -> str:
    """Validate one card's explicit item against its sprint; never infer a link."""
    if not isinstance(item_id, str) or (item_id and not _ID.fullmatch(item_id)):
        raise ValueError("a card links exactly one Definition of Done item, as d<N>")
    if sprint is None:
        if item_id:
            raise ValueError("--dod-item requires --sprint")
        return item_id
    dod = sprint["dod"]
    if not dod["structured"]:
        if item_id:
            raise ValueError(UNSTRUCTURED)
        return item_id
    if not item_id:
        if required:
            raise ValueError("a new card of a structured sprint requires --dod-item d<N>")
        return item_id
    _in_scope(dod["items"], item_id)
    return item_id


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


def marked_in_main(
    items: list[dict[str, Any]], item_id: str, evidence: str, by: Mapping[str, str]
) -> list[dict[str, Any]]:
    """Record manual delivery independently of met; the writer must rule out linked cards."""
    if not evidence.strip():
        raise ValueError("an item in main needs its evidence")
    _in_scope(items, item_id)
    return [
        {**item, _MAIN_KEY: {**by, "evidence": evidence.strip()}} if item["id"] == item_id else item
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
        {key: value for key, value in item.items() if key in _KEYS or key in {_DESCOPED_KEY, _MAIN_KEY}}
        for item in view["items"]
    ]


def unmet(items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [item for item in items or [] if item["state"] == OPEN]


def section(
    items: list[dict[str, Any]] | None, *, cards: list[dict[str, Any]] | None = None,
    events: list[dict[str, Any]] | None = None, unavailable: str | None = None,
) -> dict[str, Any]:
    """Project met shares and delivery independently, excluding descoped items from both counts.
    Until every in-scope item is weighed, each counts as weight 1.
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
            "in_main": None,
        }
    counted = [item for item in items if item["state"] != DESCOPED]
    estimated = bool(counted) and all(item["weight"] is not None for item in counted)
    weights = [0 if item["state"] == DESCOPED else item["weight"] if estimated else 1 for item in items]
    whole = sum(weights)
    done = sum(weight for weight, item in zip(weights, items, strict=True) if item["state"] == MET)
    deliveries = [_delivery(item, cards, events, unavailable) for item in items]
    return {
        "structured": True,
        "reason": None if estimated else UNESTIMATED,
        "items": [
            {**item, "share": weight / whole if whole else 0.0, "delivery": delivery}
            for weight, item, delivery in zip(weights, items, deliveries, strict=True)
        ],
        "estimated": estimated,
        "met": sum(1 for item in items if item["state"] == MET),
        "total": len(counted),
        "progress": done / whole if whole else None,
        "in_main": (
            None if any(delivery["state"] == "unknown" for delivery in deliveries)
            else sum(delivery["state"] == "in_main" for delivery in deliveries)
        ),
    }


def _delivery(
    item: dict[str, Any], cards: list[dict[str, Any]] | None,
    events: list[dict[str, Any]] | None, unavailable: str | None,
) -> dict[str, Any]:
    """Only explicit links and committed release or withdrawal facts decide delivery."""
    basis: dict[str, Any] = {
        "state": "pending", "source": None, "reason": None, "cards": None,
        "merged": [], "excluded": [], "evidence": None, "by": None,
    }
    if item["state"] == DESCOPED:
        return {**basis, "state": DESCOPED, "cards": []}
    if unavailable or cards is None or events is None:
        return {**basis, "state": "unknown", "reason": unavailable or "cards and committed audit were not loaded"}
    linked = [card for card in cards if card.get("dod_item") == item["id"]]
    basis["cards"] = sorted(card["ref"] for card in linked)
    if not linked:
        manual = item.get(_MAIN_KEY)
        if manual is not None:
            return {**basis, "state": "in_main", "source": "manual", "evidence": manual["evidence"],
                    "by": {key: manual[key] for key in _MET_BY}}
        return {**basis, "reason": "no linked cards or manual evidence"}
    releases: dict[str, dict[str, Any]] = {}
    withdrawn: dict[str, dict[str, Any]] = {}
    for event in events:
        ref = event.get("ref")
        if ref not in basis["cards"]:
            continue
        if is_merged_release(event) and (card_transition_of(event) or (None, None))[1] in {"done", "post_merge_ci"}:
            fact = event["data"][RELEASE_MERGE_KEY]
            if fact.get("merge_sha") and fact.get("base") == "main":
                releases[ref] = {"ref": ref, "event_id": event["event_id"], **fact}
        if (event.get("kind") in {"cancelled", "superseded", "wait_cancelled"}
                and event.get("outcome") == "success"
                and event.get("actor", {}).get("role") in {"po", "observer"}):
            withdrawn[ref] = {"ref": ref, "event_id": event["event_id"], "kind": event["kind"]}
    considered = [card["ref"] for card in linked if card["ref"] not in withdrawn]
    basis.update(source="release_merge", merged=[releases[ref] for ref in sorted(considered) if ref in releases],
                 excluded=[withdrawn[ref] for ref in sorted(withdrawn)])
    if considered and all(ref in releases for ref in considered):
        return {**basis, "state": "in_main"}
    return {**basis, "reason": "waiting for release merge facts" if considered else "all linked cards withdrawn"}
