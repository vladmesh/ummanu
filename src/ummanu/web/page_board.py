"""Server-rendered presentation: the board."""

from __future__ import annotations

from html import escape
from typing import Any
from urllib.parse import quote

from ummanu.web.page_assets import _BOARD_SCRIPT
from ummanu.web.page_parts import _age, _link, _or_dash, _pipeline
from ummanu.web.page_shell import _page
from ummanu.web.page_values import _mapping
from ummanu.webproto import board_view

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


def board(document: dict[str, Any], *, filters: dict[str, Any]) -> str:
    """One column per card state with its count, each card a tile; a narrow screen stacks the columns.

    Above them the active filter and a GET form over `filters` that changes or resets it.
    An empty column is marked so it can collapse; the board's scroll position survives a reload.
    """
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
            '<div class="lead board-lead"><h1>Board</h1>',
            _filter_form(_mapping(document.get("filter")), filters),
            f'<p class="board-note"><span class="muted">Done: the last {escape(str(window))} h and every Done card of an open sprint</span>',
            f'<span class="age">read at {escape(str(document.get("observed_at") or "an unknown time"))}</span></p></div>',
            content,
        ]
    )
    return _page("Board", body, script=_BOARD_SCRIPT, nav="board", fit_viewport=True)


def _column(column: dict[str, Any]) -> str:
    title = str(column.get("title") or column.get("state") or "")
    count = column.get("count")
    blocked = column.get("state") == "blocked"
    cards = [_tile(_mapping(card), blocked=blocked) for card in column.get("cards") or []]
    tiles = f'<ol class="tiles">{"".join(cards)}</ol>' if cards else '<p class="empty">no cards</p>'
    kind = "board-column" if cards else "board-column board-column-empty"
    return (
        f'<section class="{kind}" aria-label="{escape(f"{title}: {count} cards")}">'
        f'<header><h2>{escape(title)}</h2><span class="count">{escape(str(count))}</span></header>'
        f"{tiles}</section>"
    )


def _tile(tile: dict[str, Any], *, blocked: bool) -> str:
    ref = tile.get("ref")
    reference = _link(ref, titled=True) if ref else '<span class="ref">no readable ref</span>'
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
        f'<span class="meta-item">{item}</span>'
        for item in (
            escape(str(tile.get("type") or NO_TYPE)),
            escape(str(tile.get("project") or NO_PROJECT)),
            _sprint(_mapping(tile.get("sprint"))),
            f'<span title="current head">{_or_dash(tile.get("head"))}</span>',
        )
    )
    said = escape(str(tile.get("blocked_reason") or NO_REASON))
    reason = f'<p class="tile-reason" title="{said}">{said}</p>' if blocked else ""
    skip = _mapping(tile.get("ready_skip"))
    skipped = (
        f'<p class="tile-skip" title="{escape(str(skip["reason"]))}">not taken since '
        f'{escape(str(skip["since"]))}: {escape(str(skip["reason"]))}</p>'
        if skip
        else ""
    )
    return "".join(
        [
            f'<li class="tile{" tile-owner" if waiting else ""}">',
            owner,
            f'<div class="tile-head">{reference}{age}</div>',
            f'<p class="tile-title" title="{escape(str(title or NO_TITLE))}">{escape(str(title or NO_TITLE))}</p>',
            f'<p class="tile-meta">{meta}</p>',
            _pipeline(_mapping(tile.get("pipeline"))),
            reason,
            skipped,
            "</li>",
        ]
    )


def _sprint(sprint: dict[str, Any]) -> str:
    ref = sprint.get("ref")
    if sprint.get("state") == "linked" and ref:
        return f'<a href="/sprints/{quote(str(ref))}">{escape(str(ref))}</a>'
    return SPRINT_UNKNOWN if sprint.get("state") == "unreadable" else NO_SPRINT


def _filter_form(active: dict[str, Any], filters: dict[str, Any]) -> str:
    project = str(active.get("project") or "")
    sprint = str(active.get("sprint") or "")
    shown = []
    if project:
        shown.append(f"project {escape(project)}")
    if sprint:
        shown.append(f"sprint {escape(NO_SPRINT if sprint == board_view.NO_SPRINT else sprint)}")
    projects = _mapping(filters.get("projects"))
    sprints = _mapping(filters.get("sprints"))
    project_options = _options([("", "all projects")], _choices(projects, project), project)
    sprint_options = _options(
        [("", "all sprints"), (board_view.NO_SPRINT, NO_SPRINT)],
        [value for value in _choices(sprints, sprint) if value != board_view.NO_SPRINT],
        sprint,
    )
    reasons = "".join(
        f'<span class="muted">{escape(str(source.get("reason") or "no reason was recorded"))}</span>'
        for source in (_mapping(projects.get("source")), _mapping(sprints.get("source")))
        if source.get("state") != "available"
    )
    return (
        '<form class="inline board-filter" method="get" action="/board">'
        f'<span class="board-filter-active">showing: {" · ".join(shown) or "the whole board"}</span>'
        f'<div><label for="board-project">project</label><select id="board-project" name="project">{project_options}</select></div>'
        f'<div><label for="board-sprint">sprint</label><select id="board-sprint" name="sprint">{sprint_options}</select></div>'
        '<button type="submit" class="quiet">Filter</button>'
        f'<a href="/board">whole board</a>{reasons}</form>'
    )


def _choices(section: dict[str, Any], active: str) -> list[str]:
    """The section's values, with the active one kept even when the section does not offer it."""
    items = section.get("items")
    values = [item for item in items if isinstance(item, str) and item] if isinstance(items, list) else []
    return values if not active or active in values else [active, *values]


def _options(fixed: list[tuple[str, str]], values: list[str], active: str) -> str:
    return "".join(
        f'<option value="{escape(value)}"{" selected" if value == active else ""}>{escape(label)}</option>'
        for value, label in [*fixed, *((value, value) for value in values)]
    )
