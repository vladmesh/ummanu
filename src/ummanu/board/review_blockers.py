"""Stable blocker identities and the RED verdict's explicit open set."""

from __future__ import annotations

import re

BLOCKER = r"BLOCKER-[A-Za-z0-9][A-Za-z0-9_-]*"


def open_blockers(body: str) -> frozenset[str] | None:
    """Return the single valid open-blocker line's IDs, or None for missing/invalid input."""
    lines = [line for line in body.splitlines() if line.startswith("Open blockers:")]
    if len(lines) != 1:
        return None
    match = re.fullmatch(rf"Open blockers: ({BLOCKER}(?:, {BLOCKER})*)", lines[0])
    return frozenset(match[1].split(", ")) if match else None
