"""Stable parsers for the legacy flat task wire format.

Legacy readers, restore code, and the one-shot board importer all have to interpret
old board rows exactly the same way.  Keep those small, pure normalization rules
here instead of making feature packages import private helpers from ``tasks.py``.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

TASK_STATE_BY_COLUMN = {
    "Issues": "issues",
    "Ready": "ready",
    "In progress": "in_progress",
    "Validate": "validate",
    "CI": "ci",
    "Review": "review",
    "Assessment": "assessment",
    "Blocked": "blocked",
    "Done": "done",
}

TASK_KNOWN_METADATA = {
    "task_type",
    "project",
    "blocked_by",
    "claim",
    "slug",
    "base_branch",
    "seed_ref",
    "supersedes",
    "issues",
    "head",
    "resolved_head",
    "review_head",
    "resolved_review_head",
    "retry_same",
    "retry_switch",
    "retry_heads",
    "complexity",
    "family_preference",
    "routing_reason",
    "quota_snapshot_at",
    "codex_launch_mode",
    "sprint_ref",
    "dod_item",
    "review",
    "live_impact",
}


def text(value: Any) -> str:
    return value if isinstance(value, str) else "" if value is None else str(value)


def null_if_empty(value: Any) -> str | None:
    value_text = text(value)
    return value_text or None


def positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number > 0 else None


def nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def split_heads(value: Any) -> list[str]:
    return [head for head in text(value).split(",") if head]


def enum_or_default(value: Any, allowed: Collection[str], default: str) -> str:
    candidate = text(value)
    return candidate if candidate in allowed else default


def enum_or_none(value: Any, allowed: Collection[str]) -> str | None:
    candidate = text(value)
    return candidate if candidate in allowed else None


__all__ = [
    "TASK_KNOWN_METADATA",
    "TASK_STATE_BY_COLUMN",
    "enum_or_default",
    "enum_or_none",
    "nonnegative_int",
    "null_if_empty",
    "positive_int",
    "split_heads",
    "text",
]
