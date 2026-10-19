"""Stable blocker identities and the RED verdict's explicit open set."""

from __future__ import annotations

import re
from typing import Any

BLOCKER = r"BLOCKER-[A-Za-z0-9][A-Za-z0-9_-]*"


def open_blockers(body: str) -> frozenset[str] | None:
    """Return the single valid open-blocker line's IDs, or None for missing/invalid input."""
    lines = [line for line in body.splitlines() if line.startswith("Open blockers:")]
    if len(lines) != 1:
        return None
    match = re.fullmatch(rf"Open blockers: ({BLOCKER}(?:, {BLOCKER})*)", lines[0])
    return frozenset(match[1].split(", ")) if match else None


def repeated_open_blockers(task: dict[str, Any]) -> tuple[str, ...]:
    """Intersect the last two verdicts' explicit open sets only when both are RED."""
    verdicts = [
        comment for comment in task.get("comments") or []
        if comment.get("marker") in {"review:red", "review:green"}
    ]
    if len(verdicts) < 2 or any(c["marker"] != "review:red" for c in verdicts[-2:]):
        return ()
    previous, current = (open_blockers(c["body"]) for c in verdicts[-2:])
    if previous is None or current is None:
        return ()
    return tuple(sorted(previous & current))
