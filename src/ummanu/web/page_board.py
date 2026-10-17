"""Server-rendered presentation: the board."""

from __future__ import annotations

from html import escape
from typing import Any
from urllib.parse import quote

from ummanu.web.page_parts import _age, _link, _or_dash
from ummanu.web.page_shell import _page
from ummanu.web.page_values import _mapping

#: What a tile says for a value its card holds no readable form of.
NO_TITLE = "no readable title"
NO_TYPE = "type unknown"
NO_PROJECT = "project unknown"
NO_SPRINT = "вне спринта"
SPRINT_UNKNOWN = "sprint unknown"
AGE_UNKNOWN = "unknown"
NO_REASON = "reason not recorded"
#: The text label of a tile the PO handed to the owner; its colour is never the only sign.
OWNER_LABEL = "waits for the owner"


def board(document: dict[str, Any]) -> str:
    """One column per card state with its count, each card a tile; a narrow screen stacks the columns."""
    source = _mapping(document.get("source"))
    columns = document.get("columns")
    if source.get("state") != "available" or not isinstance(columns, list):
        reason = str(source.get("reason") or "no reason was recorded")
        content = f'<p class="unavailable"><b>board not readable:</b> {escape(reason)}</p>'
    else:
        content = '<div class="board">' + "".join(_column(_mapping(column)) for column in columns) + "</div>"
    window = document.get("done_window_hours")
    body = "\n".join(
        [
            '<div class="lead"><h1>Board</h1>',
            f'<span class="muted">Done: the last {escape(str(window))} h and every Done card of an open sprint</span>',
            f'<span class="age">read at {escape(str(document.get("observed_at") or "an unknown time"))}</span></div>',
            content,
        ]
    )
    return _page("Board", body, nav="board")


def _column(column: dict[str, Any]) -> str:
    title = str(column.get("title") or column.get("state") or "")
    count = column.get("count")
    blocked = column.get("state") == "blocked"
    cards = [_tile(_mapping(card), blocked=blocked) for card in column.get("cards") or []]
    tiles = f'<ol class="tiles">{"".join(cards)}</ol>' if cards else '<p class="empty">no cards</p>'
    return (
        f'<section class="board-column" aria-label="{escape(f"{title}: {count} cards")}">'
        f'<header><h2>{escape(title)}</h2><span class="count">{escape(str(count))}</span></header>'
        f"{tiles}</section>"
    )


def _tile(tile: dict[str, Any], *, blocked: bool) -> str:
    ref = tile.get("ref")
    reference = _link(ref) if ref else '<span class="ref">no readable ref</span>'
    title = tile.get("title")
    moved = _mapping(tile.get("moved"))
    age = (
        f'<span class="age" title="in this column since {escape(str(moved["at"]))}">{escape(_age(moved["age_seconds"]))}</span>'
        if moved
        else f'<span class="age">{AGE_UNKNOWN}</span>'
    )
    waiting = _mapping(tile.get("waiting_owner"))
    owner = (
        f'<p class="tile-owner-label" title="{escape(str(waiting.get("reason") or ""))}">{OWNER_LABEL}</p>'
        if waiting
        else ""
    )
    meta = " · ".join(
        [
            escape(str(tile.get("type") or NO_TYPE)),
            escape(str(tile.get("project") or NO_PROJECT)),
            _sprint(_mapping(tile.get("sprint"))),
            f'<span title="current head">{_or_dash(tile.get("head"))}</span>',
        ]
    )
    reason = (
        f'<p class="tile-reason">{escape(str(tile.get("blocked_reason") or NO_REASON))}</p>' if blocked else ""
    )
    return "".join(
        [
            f'<li class="tile{" tile-owner" if waiting else ""}">',
            owner,
            f'<div class="tile-head">{reference}{age}</div>',
            f'<p class="tile-title" title="{escape(str(title or NO_TITLE))}">{escape(str(title or NO_TITLE))}</p>',
            f'<p class="tile-meta">{meta}</p>',
            reason,
            "</li>",
        ]
    )


def _sprint(sprint: dict[str, Any]) -> str:
    ref = sprint.get("ref")
    if sprint.get("state") == "linked" and ref:
        return f'<a href="/sprints/{quote(str(ref))}">{escape(str(ref))}</a>'
    return SPRINT_UNKNOWN if sprint.get("state") == "unreadable" else NO_SPRINT
