"""Typed payloads for the three control-plane Card marker events.

The durable board-event wire format remains a dictionary. This module owns the
closed marker schemas so callers normalize once at the host boundary and only
project back to the released dictionary shape when an event is staged.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, TypeAlias

WireItems: TypeAlias = tuple[tuple[str, object], ...]

REPORT_STATUSES = frozenset({"done", "blocked", "partial"})
_FULL_COMMIT = re.compile(r"^[0-9a-f]{40}$")


def check_report_fields(status: str, classification: object, partial_commit: object) -> None:
    """A report's status-dependent fields: a blocked one is classified, only a partial one names a commit."""
    if status == "blocked":
        if classification not in {"external_fact", "wrong_task_definition"}:
            raise ValueError("blocked Card report requires a supported classification")
    elif classification is not None:
        raise ValueError(f"{status} Card report carries no classification")
    if partial_commit is None:
        return
    if status != "partial":
        raise ValueError(f"{status} Card report carries no partial_commit")
    if not isinstance(partial_commit, str) or not _FULL_COMMIT.match(partial_commit):
        raise ValueError("partial Card report partial_commit must be a full 40-character commit id")


@dataclass(frozen=True, slots=True)
class ReportPayload:
    status: str
    body: str
    classification: str | None = None
    partial_commit: str | None = None
    wire_items: WireItems = ()

    def __post_init__(self) -> None:
        if self.status not in REPORT_STATUSES:
            raise ValueError("Card report status must be done, blocked or partial")
        if not isinstance(self.body, str) or not self.body.strip():
            raise ValueError("Card report body must be non-empty")
        check_report_fields(self.status, self.classification, self.partial_commit)

    @property
    def marker(self) -> str:
        return f"report:{self.status}"

    def to_event_data(self) -> dict[str, object]:
        if self.wire_items:
            return dict(self.wire_items)
        data: dict[str, object] = {"marker": self.marker, "body": self.body, "status": self.status}
        if self.classification is not None:
            data["classification"] = self.classification
        if self.partial_commit is not None:
            data["partial_commit"] = self.partial_commit
        return data


@dataclass(frozen=True, slots=True)
class VerdictPayload:
    status: str
    body: str
    wire_items: WireItems = ()

    def __post_init__(self) -> None:
        if self.status not in {"green", "red"}:
            raise ValueError("Card verdict status must be green or red")
        if not isinstance(self.body, str) or not self.body.strip():
            raise ValueError("Card verdict body must be non-empty")

    @property
    def marker(self) -> str:
        return f"review:{self.status}"

    def to_event_data(self) -> dict[str, object]:
        if self.wire_items:
            return dict(self.wire_items)
        return {"marker": self.marker, "body": self.body, "status": self.status}


@dataclass(frozen=True, slots=True)
class DecisionPayload:
    decision: str
    body: str
    protocol_prerequisites: tuple[str, ...] = ()
    wire_items: WireItems = ()

    def __post_init__(self) -> None:
        if self.decision not in {"accept", "rework", "reslice"}:
            raise ValueError("Card decision must be accept, rework or reslice")
        if not isinstance(self.body, str) or not self.body.strip():
            raise ValueError("Card decision body must be non-empty")
        if any(not isinstance(value, str) or not value for value in self.protocol_prerequisites):
            raise ValueError("Card decision prerequisites must be non-empty strings")
        if len(set(self.protocol_prerequisites)) != len(self.protocol_prerequisites):
            raise ValueError("Card decision prerequisites must be unique")

    @property
    def marker(self) -> str:
        return f"decision:{self.decision}"

    def to_event_data(self) -> dict[str, object]:
        if self.wire_items:
            return dict(self.wire_items)
        return {
            "marker": self.marker,
            "body": self.body,
            "decision": self.decision,
            "protocol_prerequisites": list(self.protocol_prerequisites),
        }


MarkerPayload: TypeAlias = ReportPayload | VerdictPayload | DecisionPayload


def marker_payload_from_data(kind: str, reason: str, data: Mapping[str, Any]) -> MarkerPayload:
    """Normalize one released marker dictionary without changing its wire projection.

    The event may carry additional immutable evidence such as digests,
    ``assessment_visit`` or adapter recovery fields. Those remain byte-for-byte
    equivalent at the dictionary boundary while the closed marker vocabulary is
    represented by a typed value internally.
    """
    body = data.get("body")
    if not isinstance(body, str) or not body.strip():
        raise ValueError("control-plane marker events require a non-empty body")
    if reason != body:
        raise ValueError("control-plane marker event reason must match its body")
    wire_items: WireItems = tuple((str(key), value) for key, value in data.items())

    if kind == "card.reported":
        status = data.get("status")
        classification = data.get("classification")
        if not isinstance(status, str):
            raise ValueError("Card report event has an unsupported marker payload")
        partial_commit = data.get("partial_commit")
        if classification is not None and not isinstance(classification, str):
            raise ValueError("Card report classification must be a string or null")
        if partial_commit is not None and not isinstance(partial_commit, str):
            raise ValueError("partial Card report partial_commit must be a full 40-character commit id")
        payload: MarkerPayload = ReportPayload(status, body, classification, partial_commit, wire_items)
    elif kind == "card.verdict":
        status = data.get("status")
        if not isinstance(status, str):
            raise ValueError("Card verdict event has an unsupported marker payload")
        payload = VerdictPayload(status, body, wire_items)
    elif kind == "card.decided":
        decision = data.get("decision")
        prerequisites = data.get("protocol_prerequisites", [])
        if not isinstance(decision, str) or not isinstance(prerequisites, list):
            raise ValueError("Card decision event has an unsupported marker payload")
        payload = DecisionPayload(decision, body, tuple(prerequisites), wire_items)
    else:
        raise ValueError("event is not a Card marker occurrence")

    if data.get("marker") != payload.marker:
        raise ValueError("Card marker event has an unsupported marker payload")
    return payload


__all__ = [
    "REPORT_STATUSES",
    "DecisionPayload",
    "MarkerPayload",
    "ReportPayload",
    "VerdictPayload",
    "check_report_fields",
    "marker_payload_from_data",
]
