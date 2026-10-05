"""Typed Sprint read model and legacy metadata boundary.

Sprint storage remains string/JSON shaped at the PostgreSQL adapter.  This
module owns the stable parsing rules for the closed Sprint vocabularies and compound metadata
so readers and one-shot migration code normalize those values exactly once before consuming them.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ummanu.board.legacy_codec import positive_int, text
from ummanu.board.models import SprintState
from ummanu.board.po_channel import PoRequest

SOURCE_AUDIT_FIELDS = ("created_at", "updated_at", "board")

# Charged restart types contribute to total and thresholds.
BUDGET_EVENT_TYPES = (
    "red_review",
    "blocked",
    "red_ci",
    "preempt",
    "recreated_task",
    "hotfix",
)
# Infrastructure bring-up failures are visible but never spend restart budget.
BUDGET_UNCHARGED_INFRASTRUCTURE = "infrastructure_blocked"
BUDGET_UNCHARGED_EVENT_TYPES = (BUDGET_UNCHARGED_INFRASTRUCTURE,)
BUDGET_RECORDED_EVENT_TYPES = BUDGET_EVENT_TYPES + BUDGET_UNCHARGED_EVENT_TYPES
BUDGET_UNCHARGED_FIELD = "sprint_budget_uncharged"
DEFAULT_BUDGET_SIGNAL = 3
DEFAULT_BUDGET_HARD = 6

RESUME_FIELDS = (
    "selected_step",
    "selected_why",
    "rejected_alternatives",
    "current_task",
    "dod_state",
    "next_safe_step",
)

def _default_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def budget_thresholds(config: Mapping[str, Any] | None = None) -> dict[str, int]:
    """Resolve effective Sprint budget thresholds without depending on the legacy façade."""
    raw = config.get("sprint_budget") if isinstance(config, Mapping) else {}
    raw = raw if isinstance(raw, Mapping) else {}
    signal = positive_int(raw.get("signal")) or DEFAULT_BUDGET_SIGNAL
    hard = positive_int(raw.get("hard")) or DEFAULT_BUDGET_HARD
    if hard < signal:
        raise ValueError("sprint budget hard threshold must not be below signal threshold")
    return {"signal": signal, "hard": hard}


def _unique_strings(values: list[Any]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def sprint_string_list(value: str | None) -> list[str]:
    """Read one legacy JSON string-list with the released de-duplication semantics."""
    try:
        raw = json.loads(value or "[]")
    except ValueError:
        return []
    return list(_unique_strings(raw)) if isinstance(raw, list) else []


def _budget_count(counts: Any, event_type: str) -> int:
    if not isinstance(counts, Mapping):
        return 0
    try:
        return max(0, int(counts.get(event_type, 0)))
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True, slots=True)
class SprintBudget:
    """Normalized charged and uncharged restart accounting for one Sprint."""

    total: int
    by_type: dict[str, int]
    uncharged: dict[str, int]
    thresholds: dict[str, int]
    signal_reached: bool
    hard_reached: bool

    @classmethod
    def from_legacy(
        cls,
        value: Any = None,
        *,
        thresholds: Mapping[str, int] | None = None,
        uncharged: Any = None,
    ) -> SprintBudget:
        source = value if isinstance(value, Mapping) else {}
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
                source = decoded if isinstance(decoded, Mapping) else {}
            except ValueError:
                source = {}
        by_type = source.get("by_type") if isinstance(source, Mapping) else {}
        counts = {event_type: _budget_count(by_type, event_type) for event_type in BUDGET_EVENT_TYPES}
        spare_source = uncharged
        if spare_source is None:
            spare_source = source.get("uncharged") if isinstance(source, Mapping) else {}
        if isinstance(spare_source, str):
            try:
                decoded = json.loads(spare_source)
                spare_source = decoded if isinstance(decoded, Mapping) else {}
            except ValueError:
                spare_source = {}
        spare = {
            event_type: _budget_count(spare_source, event_type) for event_type in BUDGET_UNCHARGED_EVENT_TYPES
        }
        limits = dict(thresholds) if thresholds else budget_thresholds()
        total = sum(counts.values())
        return cls(
            total=total,
            by_type=counts,
            uncharged=spare,
            thresholds=limits,
            signal_reached=total >= limits["signal"],
            hard_reached=total >= limits["hard"],
        )

    def to_document(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "by_type": dict(self.by_type),
            "uncharged": dict(self.uncharged),
            "thresholds": dict(self.thresholds),
            "signal_reached": self.signal_reached,
            "hard_reached": self.hard_reached,
        }


@dataclass(frozen=True, slots=True)
class SprintSourceAudit:
    """The source audit carried by a restored Sprint, when one was recorded."""

    created_at: str
    updated_at: str
    board: str

    @classmethod
    def from_legacy(cls, value: Any) -> SprintSourceAudit | None:
        source = value
        if isinstance(value, str):
            try:
                source = json.loads(value or "null")
            except ValueError:
                return None
        if not isinstance(source, Mapping):
            return None
        values = {field: text(source.get(field)) for field in SOURCE_AUDIT_FIELDS}
        if not any(values.values()):
            return None
        return cls(
            created_at=values["created_at"],
            updated_at=values["updated_at"],
            board=values["board"],
        )

    def to_document(self) -> dict[str, str]:
        return {
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "board": self.board,
        }


@dataclass(frozen=True, slots=True)
class SprintResume:
    """One complete observer resume entry projected from legacy metadata."""

    selected_step: str
    selected_why: str
    rejected_alternatives: str
    current_task: str
    dod_state: str
    next_safe_step: str
    recorded_at: str
    po_request: PoRequest | None = None

    @classmethod
    def from_legacy(
        cls,
        value: Any,
        *,
        required: bool = False,
        now: Callable[[], str] = _default_now,
    ) -> SprintResume | None:
        source = value
        if isinstance(value, str):
            try:
                source = json.loads(value)
            except ValueError:
                source = None
        if not isinstance(source, Mapping):
            if required:
                raise ValueError("resume entry must be a JSON object")
            return None
        missing = [
            field
            for field in RESUME_FIELDS
            if not isinstance(source.get(field), str) or not str(source[field]).strip()
        ]
        if missing:
            if required:
                raise ValueError("resume entry is missing required fields: " + ", ".join(missing))
            return None
        recorded_at = text(source.get("recorded_at")) or now()
        try:
            po_request = PoRequest.from_document(source.get("po_request"))
        except ValueError:
            if required:
                raise
            po_request = None
        return cls(
            selected_step=str(source["selected_step"]).strip(),
            selected_why=str(source["selected_why"]).strip(),
            rejected_alternatives=str(source["rejected_alternatives"]).strip(),
            current_task=str(source["current_task"]).strip(),
            dod_state=str(source["dod_state"]).strip(),
            next_safe_step=str(source["next_safe_step"]).strip(),
            recorded_at=recorded_at,
            po_request=po_request,
        )

    def to_document(self) -> dict[str, Any]:
        return {
            "selected_step": self.selected_step,
            "selected_why": self.selected_why,
            "rejected_alternatives": self.rejected_alternatives,
            "current_task": self.current_task,
            "dod_state": self.dod_state,
            "next_safe_step": self.next_safe_step,
            "recorded_at": self.recorded_at,
            **({"po_request": self.po_request.to_document()} if self.po_request else {}),
        }


@dataclass(frozen=True, slots=True)
class SprintReadMetadata:
    """Typed compound fields read from one Sprint's legacy metadata bag."""

    repositories: tuple[str, ...]
    state: SprintState
    budget: SprintBudget
    resume: SprintResume | None
    source_audit: SprintSourceAudit | None

    @classmethod
    def from_legacy(
        cls,
        meta: Mapping[str, Any],
        *,
        thresholds: Mapping[str, int] | None = None,
        now: Callable[[], str] = _default_now,
    ) -> SprintReadMetadata:
        raw_state = text(meta.get("sprint_status"))
        try:
            state = SprintState(raw_state)
        except ValueError:
            state = SprintState.OPEN
        return cls(
            repositories=tuple(sprint_string_list(text(meta.get("sprint_repositories")))),
            state=state,
            budget=SprintBudget.from_legacy(
                meta.get("sprint_budget"),
                thresholds=thresholds,
                uncharged=meta.get(BUDGET_UNCHARGED_FIELD),
            ),
            resume=SprintResume.from_legacy(meta.get("sprint_resume"), now=now),
            source_audit=SprintSourceAudit.from_legacy(meta.get("sprint_source_audit")),
        )


__all__ = [
    "BUDGET_EVENT_TYPES",
    "BUDGET_RECORDED_EVENT_TYPES",
    "BUDGET_UNCHARGED_EVENT_TYPES",
    "BUDGET_UNCHARGED_FIELD",
    "DEFAULT_BUDGET_HARD",
    "DEFAULT_BUDGET_SIGNAL",
    "RESUME_FIELDS",
    "SOURCE_AUDIT_FIELDS",
    "SprintBudget",
    "SprintReadMetadata",
    "SprintResume",
    "SprintSourceAudit",
    "budget_thresholds",
    "sprint_string_list",
]
