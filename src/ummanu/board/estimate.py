"""The observer's expected worker time for a card, in minutes: a reference point, never a ceiling.

A worker-executed card (`code`, `research`, `infra`) may carry `task create --estimate-minutes N`, a
positive integer kept as one field of the card's extension bag (docs/BOARD_STORE.md §8.2). Only create
writes it. A card without one reads as :data:`DEFAULT_ESTIMATE_MINUTES`, and :func:`estimate_view`
says which of the two a reader sees. Nothing stops, parks or charges a card by this value.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ummanu.board.extension_bag import card_bag
from ummanu.board.task_routing import TaskType

ESTIMATE_MINUTES = "estimate_minutes"
DEFAULT_ESTIMATE_MINUTES = 120
#: The kinds a worker head executes, the only ones an estimate describes.
ESTIMATED_KINDS = frozenset({TaskType.CODE.value, TaskType.RESEARCH.value, TaskType.INFRA.value})
_MINUTES = re.compile(r"^[0-9]+$")


def parse_minutes(value: str) -> int | None:
    """`value` as a positive whole number of minutes, or None when it is not one."""
    text = value.strip()
    if not _MINUTES.match(text) or int(text) <= 0:
        return None
    return int(text)


def create_refusal(kind: str, value: str) -> str:
    """Why `--estimate-minutes value` cannot go on a new card of `kind`, or `""`."""
    if not value.strip():
        return ""
    if kind not in ESTIMATED_KINDS:
        return f"--estimate-minutes is the expected worker time of a worker card; a {kind} card takes none"
    if parse_minutes(value) is None:
        return f"--estimate-minutes must be a positive whole number of minutes, not {value.strip()!r}"
    return ""


def estimate_view(task: Mapping[str, Any]) -> dict[str, Any] | None:
    """The effective estimate of a worker card and where it comes from, or None for any other kind.

    A stored value that is not a positive whole number reads as absent, so the default applies.
    """
    if str(task.get("type") or "") not in ESTIMATED_KINDS:
        return None
    stored = parse_minutes(str(card_bag(task).get(ESTIMATE_MINUTES) or ""))
    if stored is None:
        return {"minutes": DEFAULT_ESTIMATE_MINUTES, "source": "default"}
    return {"minutes": stored, "source": "card"}
