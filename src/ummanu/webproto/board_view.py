"""The web board: every work card in its column as one tile, from one board store statement.

Each tile value has one normaliser, so a malformed row is a tile with an unknown value, never a
failed read. See docs/PROTOCOLS.md, "Reading the pipeline".
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from ummanu.board.card_transitions import CARD_TRANSITIONS
from ummanu.board.legacy_codec import TASK_STATE_BY_COLUMN
from ummanu.board.models import CardState
from ummanu.board.owner_handover import waiting_owner
from ummanu.board.sql_rows import comment_marker
from ummanu.webproto import sources

SCHEMA_VERSION = 1

#: How far back Done reaches for a card of no open sprint.
DONE_WINDOW_HOURS = 48

#: The columns in board order, as `(state, title)`.
COLUMNS: tuple[tuple[str, str], ...] = tuple((state, title) for title, state in TASK_STATE_BY_COLUMN.items())

#: A move writes its reason as a comment marked with the moving role, so the comment that blocked a
#: card carries the marker of a role allowed to move a card into Blocked.
BLOCKING_MARKERS: tuple[str, ...] = tuple(
    sorted(
        role.value
        for role, edges in CARD_TRANSITIONS.items()
        if any(target is CardState.BLOCKED for _source, target in edges)
    )
)

#: Whose head a tile names in a column: the chosen head first, then the resolved one.
HEAD_FIELDS: dict[str, tuple[str, str]] = {
    "in_progress": ("head_override", "worker_head"),
    "validate": ("review_head_override", "review_head"),
}

LINKED = "linked"
NO_SPRINT = "none"
UNREADABLE = "unreadable"


def board_document(client: Any, *, now: float) -> dict[str, Any]:
    """The board read from `client.board_cards`, every card placed in its column, oldest move first."""
    rows = client.board_cards(
        done_since=datetime.fromtimestamp(now - DONE_WINDOW_HOURS * 3600, UTC),
        blocking_markers=BLOCKING_MARKERS,
    )
    placed: dict[str, list[dict[str, Any]]] = {state: [] for state, _title in COLUMNS}
    for row in rows:
        state = row.get("state")
        if state not in placed:
            raise ValueError(f"card {row.get('ref')!r} is in state {state!r}, which no board column shows")
        placed[state].append(_tile(row, state, now=now))
    return _document(
        now,
        source=sources.available(now),
        columns=[
            {"state": state, "title": title, "count": len(placed[state]), "cards": sorted(placed[state], key=_oldest_first)}
            for state, title in COLUMNS
        ],
    )


def unreadable(reason: str, *, now: float) -> dict[str, Any]:
    """The board that could not be read: its reason, and no columns rather than empty ones."""
    return _document(now, source=sources.unavailable(reason, now=now), columns=None)


def _document(now: float, *, source: sources.Source, columns: list[dict[str, Any]] | None) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "board",
        "observed_at": sources.isoformat(now),
        "source": source.to_json(),
        "done_window_hours": DONE_WINDOW_HOURS,
        "columns": columns,
    }


def _tile(row: dict[str, Any], state: str, *, now: float) -> dict[str, Any]:
    return {
        "ref": _line(row.get("ref")),
        "title": _line(row.get("title")),
        "type": _line(row.get("type")),
        "project": _line(row.get("project")),
        "sprint": _sprint(row.get("sprint")),
        "head": _head(row, state),
        "moved": _moved(row.get("moved"), now=now),
        "blocked_reason": _blocked_reason(row.get("blocking_comment")) if state == "blocked" else None,
        "waiting_owner": waiting_owner({"extensions": row.get("extensions")}),
    }


def _oldest_first(tile: dict[str, Any]) -> tuple[int, float, str]:
    """An unknown move counts as the oldest; ties keep reference order."""
    moved = tile["moved"]
    return (0, 0.0, tile["ref"] or "") if moved is None else (1, -moved["age_seconds"], tile["ref"] or "")


def _line(value: Any) -> str | None:
    """A text value on one line, or None when it holds no readable text."""
    if not isinstance(value, str):
        return None
    return " ".join(value.split()) or None


def _sprint(value: Any) -> dict[str, Any]:
    """The card's sprint: linked to a reference, no sprint, or a value that names none readably."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return {"state": NO_SPRINT, "ref": None}
    reference = _line(value)
    if reference is None:
        return {"state": UNREADABLE, "ref": None}
    return {"state": LINKED, "ref": reference}


def _head(row: dict[str, Any], state: str) -> str | None:
    """The head working the card in this column, from the card's own routing; none elsewhere."""
    for field in HEAD_FIELDS.get(state, ()):
        head = _line(row.get(field))
        if head is not None:
            return head
    return None


def _moved(value: Any, *, now: float) -> dict[str, Any] | None:
    """When the card entered its column and how long ago, or None when that is not a past moment."""
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    moment = float(value)
    if not math.isfinite(moment) or moment > now:
        return None
    try:
        at = sources.isoformat(moment)
    except (OverflowError, OSError, ValueError):
        return None
    return {"at": at, "age_seconds": now - moment}


def _blocked_reason(body: Any) -> str | None:
    """The first non-empty content line of the blocking comment, below its marker line."""
    if not isinstance(body, str):
        return None
    lines = body.splitlines()
    if comment_marker(body) is not None:
        lines = lines[1:]
    for line in lines:
        text = _line(line)
        if text is not None:
            return text
    return None
