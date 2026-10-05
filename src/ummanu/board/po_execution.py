"""Explicit execution assignment for dispatcher e2e questions without a sprint/origin.

This is assignment to the PO, never provenance of a PO turn. The create request,
purpose and sources are immutable; service resolution/successors live on that card.
The extension survives normalized task export without a separate routing store.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ummanu.board.po_origin import _json_field

PO_EXECUTION = "po_execution"
PURPOSES = frozenset({"e2e_budget", "e2e_disposition"})
DISPOSITION_PREFIX = "dispatcher-e2e-am-disposition-"


@dataclass
class PoExecution:
    request: str
    purpose: str
    sources: tuple[str, ...]
    initial: dict[str, str] = field(default_factory=dict)
    executor: str = ""
    successors: dict[str, dict[str, str]] = field(default_factory=dict)

    @classmethod
    def from_document(cls, value: Any) -> PoExecution:
        if not isinstance(value, Mapping):
            raise TypeError("PO execution assignment must be an object")
        request, purpose, sources = value.get("request"), value.get("purpose"), value.get("sources")
        if (not isinstance(request, str) or not request.strip()
                or not isinstance(purpose, str) or purpose not in PURPOSES):
            raise ValueError("PO execution assignment needs a request and supported e2e purpose")
        if not isinstance(sources, list) or not sources or any(
            not isinstance(ref, str) or not ref.strip() for ref in sources
        ):
            raise ValueError("PO execution assignment needs source card references")
        initial = value.get("initial", {})
        successors = value.get("successors", {})
        executor = value.get("executor", "")
        if not isinstance(executor, str):
            raise TypeError("PO execution executor must be a session string")
        if not isinstance(initial, dict) or not isinstance(successors, dict):
            raise TypeError("PO execution session records must be objects")
        if set(value) - {"request", "purpose", "sources", "initial", "executor", "successors"}:
            raise ValueError("unknown PO execution assignment fields")
        records = [initial, *successors.values()]
        if any(not isinstance(row, dict) or set(row) - {"replaces", "via", "session", "cli", "model", "effort"}
               or any(not isinstance(item, str) for item in row.values()) for row in records):
            raise ValueError("malformed PO execution service session record")
        if any(not isinstance(key, str) or not key or row.get("replaces") != key
               for key, row in successors.items()):
            raise ValueError("PO execution successor must name the session it replaces")
        if any(row and row.get("via") != "create_session" for row in records):
            raise ValueError("standalone PO execution uses native create_session resolution")
        if initial and initial.get("replaces") != "":
            raise ValueError("initial PO assignment cannot replace an unrelated session")
        return cls(request.strip(), purpose, tuple(dict.fromkeys(ref.strip() for ref in sources)), dict(initial),
                   executor, {key: dict(row) for key, row in successors.items()})

    def to_document(self) -> dict[str, Any]:
        return {"request": self.request, "purpose": self.purpose, "sources": list(self.sources),
                "initial": self.initial, "executor": self.executor, "successors": self.successors}

    def text(self) -> str:
        return json.dumps(self.to_document(), sort_keys=True, separators=(",", ":"))


def assignment(task: Mapping[str, Any]) -> PoExecution | None:
    raw = _json_field(task, PO_EXECUTION)
    if raw is None:
        return None
    return PoExecution.from_document(raw)


def create_assignment(request: str, purpose: str, sources: list[str]) -> dict[str, Any]:
    return PoExecution.from_document({"request": request, "purpose": purpose, "sources": sources}).to_document()
