"""Resolve whether a card's current sprint supplies an observer decision owner."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from ummanu.kernel.errors import TaskError


def observer_managed(task: Mapping[str, Any], show: Callable[[str], dict[str, Any]]) -> bool:
    """Unreadable sprint or observer context refuses routing rather than authorizing release."""
    reference = str(task.get("sprint") or "")
    if not reference:
        return False
    sprint = show(reference)
    status = sprint.get("status")
    if status in {"closed", "stopped"}:
        return False
    if status == "open":
        if "observer" not in sprint or sprint["observer"] == {"kind": "none"}:
            return False
        observer = sprint["observer"]
        if isinstance(observer, dict) and observer.get("kind") == "head" and observer.get("profile"):
            return True
    raise TaskError("routing_context_unknown", "current sprint/observer routing context is unreadable", 3)
