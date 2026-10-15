"""Effective sprint budgets and admission capacity, shared by validation and runtime."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ummanu.board.legacy_codec import positive_int

DEFAULT_BUDGET_SIGNAL = 3
DEFAULT_BUDGET_HARD = 6
DEFAULT_OPEN_SPRINT_LIMIT = 1
MAX_OPEN_SPRINT_LIMIT = 3


def budget_thresholds(config: Mapping[str, Any] | None = None) -> dict[str, int]:
    """Resolve effective Sprint budget thresholds without depending on the legacy façade."""
    raw = config.get("sprint_budget") if isinstance(config, Mapping) else {}
    raw = raw if isinstance(raw, Mapping) else {}
    signal = positive_int(raw.get("signal")) or DEFAULT_BUDGET_SIGNAL
    hard = positive_int(raw.get("hard")) or DEFAULT_BUDGET_HARD
    if hard < signal:
        raise ValueError("sprint budget hard threshold must not be below signal threshold")
    return {"signal": signal, "hard": hard}


def open_sprint_limit(config: dict[str, Any] | None = None) -> int:
    """How many sprints this installation may hold open, refusing to widen on bad
    input. An absent, unreadable or malformed setting all answer one.
    """
    raw = config.get("open_sprint_limit") if isinstance(config, dict) else None
    if raw is None or isinstance(raw, bool) or not isinstance(raw, int):
        return DEFAULT_OPEN_SPRINT_LIMIT
    if not DEFAULT_OPEN_SPRINT_LIMIT <= raw <= MAX_OPEN_SPRINT_LIMIT:
        return DEFAULT_OPEN_SPRINT_LIMIT
    return raw


def open_sprint_limit_invalid(config: dict[str, Any] | None = None) -> bool:
    """Whether the setting is present and holds something this installation cannot honour."""
    if not isinstance(config, dict) or "open_sprint_limit" not in config:
        return False
    raw = config["open_sprint_limit"]
    # `True` is an int to Python and would compare equal to the limit it falls back to,
    # so the type is judged before the value.
    if isinstance(raw, bool) or not isinstance(raw, int):
        return True
    return not DEFAULT_OPEN_SPRINT_LIMIT <= raw <= MAX_OPEN_SPRINT_LIMIT
