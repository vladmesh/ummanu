"""The web board: every work card in its column as one tile, from one board store statement.

Each tile value has one normaliser, so a malformed row is a tile with an unknown value, never a
failed read. See docs/PROTOCOLS.md, "Reading the pipeline".
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from ummanu.board.card_transitions import CARD_TRANSITIONS
from ummanu.board.legacy_codec import TASK_STATE_BY_COLUMN
from ummanu.board.models import CardState
from ummanu.board.owner_handover import waiting_owner
from ummanu.board.sql_rows import comment_marker
from ummanu.dispatch.worker_report import BLOCKED_RELAY
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

#: The marker of the worker's own block report, whose body holds the reason a relay only names.
WORKER_BLOCKED_REPORT = "report:blocked"

#: What a reason line may start with that says nothing yet: a heading's marks, a `Blocker:` label
#: and a `classification: <value>` clause.
_HEADING = re.compile(r"#+\s*")
_BLOCKER_LABEL = re.compile(r"\Ablocker\s*:\s*", re.IGNORECASE)
_CLASSIFICATION = re.compile(r"\Aclassification\s*:\s*[^\s.,;]+[.,;]?\s*", re.IGNORECASE)

#: Whose head a tile names in a column: the chosen head first, then the resolved one.
HEAD_FIELDS: dict[str, tuple[str, str]] = {
    "in_progress": ("head_override", "worker_head"),
    "validate": ("review_head_override", "review_head"),
}

LINKED = "linked"
NO_SPRINT = "none"
UNREADABLE = "unreadable"


def board_document(client: Any, *, now: float, project: str | None, sprint: str | None) -> dict[str, Any]:
    """The board read from `client.board_cards`, every card placed in its column, oldest move first.

    `project` and `sprint` narrow it when given; the sprint `none` keeps only cards of no sprint.
    """
    rows = client.board_cards(
        done_since=datetime.fromtimestamp(now - DONE_WINDOW_HOURS * 3600, UTC),
        blocking_markers=BLOCKING_MARKERS,
        report_marker=WORKER_BLOCKED_REPORT,
        project=project,
        sprint=None if sprint == NO_SPRINT else sprint,
        no_sprint=sprint == NO_SPRINT,
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
        project=project,
        sprint=sprint,
        columns=[
            {"state": state, "title": title, "count": len(placed[state]), "cards": sorted(placed[state], key=_oldest_first)}
            for state, title in COLUMNS
        ],
    )


def unreadable(reason: str, *, now: float, project: str | None, sprint: str | None) -> dict[str, Any]:
    """The board that could not be read: its reason, and no columns rather than empty ones."""
    return _document(
        now, source=sources.unavailable(reason, now=now), project=project, sprint=sprint, columns=None
    )


def _document(
    now: float,
    *,
    source: sources.Source,
    project: str | None,
    sprint: str | None,
    columns: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "board",
        "observed_at": sources.isoformat(now),
        "source": source.to_json(),
        "filter": {"project": project, "sprint": sprint},
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
        "blocked_reason": _blocked_reason(row) if state == "blocked" else None,
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


def _blocked_reason(row: dict[str, Any]) -> str | None:
    """The first line that says something of the comment that blocked the card, or of the worker's
    block report when that comment only relays it."""
    lines = _content_lines(row.get("blocking_comment"))
    if lines == [BLOCKED_RELAY]:
        lines = _content_lines(row.get("worker_report"))
    for line in lines:
        reason = _saying(line)
        if reason is not None:
            return reason
    return None


def _content_lines(body: Any) -> list[str]:
    """A comment's non-empty lines on one line each, below its marker line."""
    if not isinstance(body, str):
        return []
    lines = body.splitlines()
    if comment_marker(body) is not None:
        lines = lines[1:]
    return [text for text in map(_line, lines) if text is not None]


def _saying(line: str) -> str | None:
    """A reason line without the marks and labels it may start with; None when nothing is left or
    it is a heading that is only a label."""
    heading = _HEADING.match(line)
    text = line[heading.end():] if heading else line
    if heading and re.fullmatch(r"\w+:?", text):
        return None
    text = _BLOCKER_LABEL.sub("", text, count=1)
    text = _CLASSIFICATION.sub("", text, count=1)
    return text or None
