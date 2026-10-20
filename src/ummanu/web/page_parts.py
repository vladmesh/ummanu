"""Server-rendered presentation: parts."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from html import escape
from typing import Any
from urllib.parse import quote, urlencode

from ummanu.web.page_assets import MAX_TABS
from ummanu.web.page_values import _RENDER_CLOCK, _mapping


def _panel(title: str, body: str, *, count: Any = None, more: str = "", open_: bool | None = None) -> str:
    """One titled panel with an optional count; `open_` makes it a disclosure (`None`: plain)."""
    counted = "" if count is None else f'<span class="count">{escape(str(count))}</span>'
    if open_ is None:
        return (
            f'<section class="panel"><header><h2>{escape(title)}</h2>{counted}{more}</header>'
            f'<div class="body">{body}</div></section>'
        )
    return (
        f'<details class="panel"{" open" if open_ else ""}><summary>{escape(title)}{" " + counted if counted else ""}</summary>'
        f'<div class="body">{body}</div></details>'
    )


def _chip(text: str, tone: str = "") -> str:
    tone_class = f" chip-{tone}" if tone else ""
    return f'<span class="chip{tone_class}">{escape(text)}</span>'


#: The tone of each card state, shown beside the state's word, never instead of it.
STATE_TONES: dict[str, str] = {
    "in_progress": "accent",
    "validate": "accent",
    "reviewing": "accent",
    "assessment": "warn",
    "blocked": "bad",
    "done": "ok",
    "ready": "",
    "issues": "",
}


def _state_chip(state: Any) -> str:
    word = str(state or "unknown")
    return _chip(word.replace("_", " "), STATE_TONES.get(word, ""))


def _source_block(source: dict[str, Any] | None, *, what: str) -> str:
    """The marked block an unavailable source gets, or nothing when it answered."""
    source = source or {}
    if source.get("state") == "available":
        return ""
    age = source.get("data_age_seconds")
    stale = "" if age is None else f' <span class="age">showing evidence {_age(age)} old</span>'
    reason = escape(str(source.get("reason") or "no reason was recorded"))
    return f'<p class="unavailable"><b>could not find out {escape(what)}:</b> {reason}{stale}</p>'


def _section(source: dict[str, Any] | None, items: list[Any], *, what: str, empty: str, table: str) -> str:
    """One section: the source's refusal above any stale rows, then the rows or the empty line."""
    parts = [_source_block(source, what=what)]
    if items:
        parts.append(table)
    elif (source or {}).get("state") == "available":
        parts.append(f'<p class="empty">{escape(empty)}</p>')
    return "\n".join(part for part in parts if part)


def _age(seconds: Any) -> str:
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return "an unknown time"
    if value < 90:
        return f"{int(value)}s"
    if value < 5400:
        return f"{int(value // 60)}m"
    return f"{int(value // 3600)}h"


#: Said in place of a countdown with no readable reset moment: nothing is not zero.
NO_RESET_RECORDED = "no reset time recorded"


def _reset_moment(value: Any) -> datetime | None:
    """The moment a reading calls a reset, or `None` when it recorded none this process can read."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    # `provider_usage._iso` writes UTC; an offset-less moment is UTC, not host local time.
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


#: Hover title of a percentage not drawn because its window has reset since.
READING_PREDATES_RESET = "this reading predates the last reset of its window, so what is left now is unknown"


#: The longest window a reset is rolled forward by; a longer one counts as no window length.
LONGEST_WINDOW_MINUTES = 366 * 24 * 60


def _time_left(seconds: float) -> str:
    """A reset as the time left until it: see :func:`_duration`."""
    return f"{_duration(seconds)} left"


def _duration(seconds: float) -> str:
    """A future span in this module's one countdown spelling; under a minute is "less than a minute"."""
    total = int(seconds)
    if total < 60:
        return "less than a minute"
    # Units attach to their number: `1h 6m`, never `1 h 6 m`.
    minutes = total // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def _render_now() -> datetime:
    """This render's clock as an aware moment; every countdown is measured from it."""
    clock = _RENDER_CLOCK.get()
    now = clock() if clock is not None else datetime.now(UTC)
    return now if now.tzinfo is not None else now.replace(tzinfo=UTC)


def _window_length(value: Any) -> int | None:
    """A window length in minutes usable for rolling a reset forward, else `None`."""
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return value if 0 < value <= LONGEST_WINDOW_MINUTES else None


def _reset(resets_at: Any, window_minutes: Any = None) -> str:
    """A usage window's reset as the time left until it; see :func:`_reset_reading`."""
    return _reset_reading(resets_at, window_minutes)[0]


def _reset_reading(resets_at: Any, window_minutes: Any = None) -> tuple[str, bool]:
    """A usage window's reset as time left, and whether the reading predates it.

    The ISO moment is the hover title (none without a moment). The countdown runs from this render's
    clock, never the reading's `observed_at` (the cache may be `CACHE_SECONDS` old). A past reset is
    rolled forward by whole window lengths (divided out, not stepped) and flags the percentage as the
    previous window's; a past reset without a usable window length reads as no reset recorded.
    """
    moment = _reset_moment(resets_at)
    if moment is None:
        return f'<span class="resets">{escape(NO_RESET_RECORDED)}</span>', False
    try:
        ahead = (moment - _render_now()) // timedelta(microseconds=1)
    except (OverflowError, ValueError):
        return f'<span class="resets">{escape(NO_RESET_RECORDED)}</span>', False
    if ahead > 0:
        left = _time_left(ahead / 1_000_000)
        return f'<span class="resets" title="{escape(str(resets_at))}">{escape(left)}</span>', False
    minutes = _window_length(window_minutes)
    if minutes is None:
        title = f"the recorded reset {resets_at} has come, and no window length says when the next one is"
        return f'<span class="resets" title="{escape(title)}">{escape(NO_RESET_RECORDED)}</span>', True
    period = minutes * 60_000_000
    left_us = period - (-ahead) % period
    title = f"rolled forward by whole {minutes}-minute windows from the recorded reset {resets_at}"
    left = _time_left(left_us / 1_000_000)
    return f'<span class="resets" title="{escape(title)}">{escape(left)}</span>', True


def _percent(remaining: Any) -> str:
    """A usage window's percentage (`73%`; a real tenth kept); a non-number is a dash, never `0%`."""
    if not isinstance(remaining, (int, float)) or isinstance(remaining, bool):
        return "—"
    return f"{remaining:.1f}".removesuffix(".0") + "%"


def _rows(headers: list[str], rows: list[list[str]]) -> str:
    """A table. Headerless two-column rows are a key/value list and are drawn as one."""
    if headers and not any(headers):
        body = "".join(f"<tr><th>{row[0]}</th><td>{row[1]}</td></tr>" for row in rows)
        return f'<table class="kv"><tbody>{body}</tbody></table>'
    head = "".join(f"<th>{escape(name)}</th>" for name in headers)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _state_cell(state: str, reason: str) -> str:
    return (
        f'<span class="state state-{escape(state)}">{escape(state)}</span>'
        f'<div class="reason">{escape(reason)}</div>'
    )


def _link(ref: str, *, titled: bool = False) -> str:
    """A card's ref linked to its page; `titled` also names the ref in `title=`."""
    title = f' title="{escape(ref)}"' if titled else ""
    return f'<a class="ref" href="/tasks/{quote(ref)}"{title}>{escape(ref)}</a>'


def _board_link(**preset: str) -> str:
    """The board's address with its filter preset, each value URL-encoded."""
    return f"/board?{urlencode(preset)}"


def _or_dash(value: Any) -> str:
    return escape(str(value)) if value not in (None, "") else "—"


def _sprint_items(section: dict[str, Any] | None) -> list[dict[str, Any]]:
    document = (section or {}).get("document") if (section or {}).get("available") else None
    if not isinstance(document, dict):
        return []
    return [item for item in (document.get("sprints") or {}).get("items") or [] if isinstance(item, dict)]


def _beside(section: dict[str, Any] | None, *, what: str) -> tuple[dict[str, Any] | None, str]:
    """A read made beside the page's own: its document, or the marked block saying why none."""
    if not section or not section.get("available") or not isinstance(section.get("document"), dict):
        reason = str((section or {}).get("reason") or "this section was not read")
        return None, (
            f'<div class="unavailable"><b>could not read {escape(what)}.</b> {escape(reason)}</div>'
        )
    return section["document"], ""


def _open_sprints(section: dict[str, Any] | None) -> str:
    document, refused = _beside(section, what="the open sprints")
    if document is None:
        return refused
    listing = document.get("sprints") or {}
    items = [item for item in listing.get("items") or [] if isinstance(item, dict)]
    parts = [_source_block(listing.get("source"), what="which sprints are open")]
    if not items and (listing.get("source") or {}).get("state") == "available":
        parts.append('<p class="empty">no sprint is open. <a href="/sprints/new">Open one</a>.</p>')
    parts.append(
        '<div class="compact-sprints">' + "\n".join(_compact_sprint_card(item) for item in items) + "</div>"
    )
    return "\n".join(part for part in parts if part)


def _compact_sprint_card(item: dict[str, Any]) -> str:
    """One open sprint on the dashboard: goal, current card, heads, spend."""
    ref = str(item.get("ref") or "")
    projects = item.get("projects") if isinstance(item.get("projects"), list) else []
    project = ", ".join(str(value) for value in projects) or str(item.get("product") or "—")
    human_wait = _mapping(item.get("attention"))
    attention = _chip("attention required", "warn") if human_wait.get("state") == "waiting" and human_wait.get("event_ids") else ""
    budget = _mapping(item.get("budget"))
    heads = _sprint_heads(item, compact=True)
    return "".join(
        [
            '<article class="compact-sprint"><header>',
            f'<h3><a href="/sprints/{quote(ref)}">{escape(ref)}</a></h3>{_chip(project)}',
            f'<span class="push">{_waiting_chip(item)}{attention}</span></header>',
            f'<p class="goal clamp">{escape(str(item.get("goal") or ""))}</p>',
            _current_card_box(item),
            _waiting_line(item),
            f'<div class="heads-line">{heads}</div>' if heads else "",
            _dod_scale(item["dod"]) if "dod" in item else "",
            _budget_line(budget) if budget else "",
            "</article>",
        ]
    )


#: The tone a gate state reads in, beside its own word.
GATE_TONES: dict[str, str] = {"green": "ok", "red": "bad", "pending": "", "running": "accent"}


def _current_card_box(item: dict[str, Any]) -> str:
    """The card a sprint is on: where it stands, whether its gate passed, and what it is called."""
    current = _mapping(item.get("current_task"))
    if isinstance(item.get("current_task"), str) and item.get("current_task"):
        current = {"ref": item["current_task"], "live": None}
    ref = str(current.get("ref") or "")
    if not ref:
        said = str(current.get("reason") or "the observer has cut no card for this sprint yet")
        return f'<div class="card-box"><span class="empty">{escape(said)}</span></div>'
    standing = _mapping(item.get("current_card_state"))
    checks = _mapping(item.get("checks"))
    gate = _mapping(checks.get("gate"))
    gate_state = str(gate.get("state") or "")
    gate_chip = (
        f'<span title="{escape(str(checks.get("reason") or ""))}">{_chip("CI " + gate_state, GATE_TONES.get(gate_state, ""))}</span>'
        if gate_state
        else ""
    )
    title = str(standing.get("title") or "")
    return (
        f'<div class="card-box"><div class="card-line">{_card_standing(item)}{gate_chip}'
        f'<span class="push">{_link(ref)}</span></div>'
        + (f'<div class="card-title">{escape(title)}</div>' if title else "")
        + "</div>"
    )


def _sprint_heads(item: dict[str, Any], *, compact: bool = False) -> str:
    """The sprint's heads from `head_profiles`, with model and effort; unset roles are not drawn."""
    heads = _heads_of(item)
    drawn = [
        _head(role, heads[role], compact=compact)
        for role in ("observer", "worker", "reviewer")
        if role in heads
    ]
    if not drawn:
        return ""
    return "".join(drawn) if compact else f'<div class="heads">{"".join(drawn)}</div>'


def _heads_of(item: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Each role's profile with model and effort; the observer's up-state comes from its launch."""
    profiles = _mapping(item.get("head_profiles"))
    heads = {
        role: dict(profiles[role])
        for role in ("observer", "worker", "reviewer")
        if isinstance(profiles.get(role), dict) and profiles[role].get("profile")
    }
    observer = _mapping(item.get("observer"))
    launch = _mapping(observer.get("launch"))
    if "observer" in heads and launch.get("state"):
        heads["observer"]["state"] = launch["state"]
    return heads


def _sprint_status_chip(item: dict[str, Any]) -> str:
    status = str(item.get("status") or "")
    return _chip(status or "unknown", {"open": "ok", "closed": "", "stopped": "bad"}.get(status, ""))


def _waiting_chip(item: dict[str, Any]) -> str:
    waiting = _mapping(item.get("waiting"))
    state = str(waiting.get("state") or "")
    if not state:
        return ""
    tone = {"working": "accent", "waiting": "", "blocked": "", "ended": "", "unknown": ""}.get(
        state, ""
    )
    return (
        f'<span title="{escape(str(waiting.get("reason") or ""))}">{_chip("observer " + state, tone)}</span>'
    )


def _waiting_line(item: dict[str, Any]) -> str:
    """What a waiting sprint waits on, in words, with its card (the chip has only a hover title)."""
    waiting = _mapping(item.get("waiting"))
    if waiting.get("state") != "waiting" or not waiting.get("reason"):
        return ""
    card = str(waiting.get("card") or "")
    pointer = f" {_link(card)}" if card else ""
    return f'<div class="reason">waiting: {escape(_short(waiting.get("reason"), 160))}{pointer}</div>'


#: Said instead of a duration when no transition of the current card is dated; never a zero age.
NO_TRANSITION_RECORDED = "no transition recorded"


def _card_standing(item: dict[str, Any]) -> str:
    """Where a sprint's current card stands and since when; the one renderer for every surface.

    Duration in the text, ISO moment as hover title (none without one); an ended sprint has none.
    """
    carried = item.get("current_card_state")
    section = _mapping(carried)
    if not section:
        return ""
    transition = str(section.get("transition") or "")
    state = str(section.get("state") or "")
    column = _state_chip(state) if state else ""
    if transition == "recorded":
        since = str(section.get("since") or "")
        age = _age(section.get("age_seconds"))
        return f'{column} <span class="age" title="{escape(since)}">{escape(age)} in this state</span>'
    if transition == "absent":
        return f'{column} <span class="empty">{escape(NO_TRANSITION_RECORDED)}</span>'
    said = _short(section.get("reason"), 140) or "nothing said where this card stands"
    return f'{column} <span class="empty">{escape(said)}</span>'


def _short(text: Any, chars: int) -> str:
    value = str(text or "")
    return value if len(value) <= chars else value[: chars - 1].rstrip() + "…"


def _dod_scale(dod: dict[str, Any]) -> str:
    """The sprint's progress: one segment per DoD item in scope, as wide as the reader's share, met ones filled."""
    if not dod.get("structured"):
        return (
            '<div class="dod-scale none"><span class="label">progress</span>'
            f'<span class="muted">no scale: {escape(str(dod.get("reason") or ""))}</span></div>'
        )
    segments = []
    items = _entries(dod.get("items"))
    for item in items:
        if item.get("state") == "descoped":
            continue
        weight = item.get("weight") if item.get("weight") is not None else "not weighed"
        said = f"{item.get('id')} · weight {weight} · {item.get('text')}"
        if item.get("evidence"):
            said += f" · evidence: {item['evidence']}"
        state = "met" if item.get("state") == "met" else "open"
        segments.append(
            f'<i class="seg {state}" style="flex:{float(item.get("share") or 0):.4f} 1 0" '
            f'title="{escape(said)}"></i>'
        )
    progress = dod.get("progress")
    descoped = sum(1 for item in items if item.get("state") == "descoped")
    words = (
        f"{dod.get('met')} of {dod.get('total')} items met"
        + (f" · {float(progress) * 100:.0f}%" if progress is not None else "")
        + (f" · {descoped} descoped" if descoped else "")
    )
    unweighed = "" if dod.get("estimated") else _chip("not estimated")
    return (
        '<div class="dod-scale"><span class="label">progress</span>'
        f'<span class="track">{"".join(segments)}</span>'
        f'<span class="mono">{words}</span>{unweighed}</div>'
    )


def _budget_line(budget: dict[str, Any]) -> str:
    """The card budget as a thin, quiet line: a spend, not progress."""
    total = int(budget.get("total") or 0)
    thresholds = budget.get("thresholds") or {}
    hard = int(thresholds.get("hard") or 0)
    signal = int(thresholds.get("signal") or 0)
    ratio = min(1.0, total / hard) if hard else 0.0
    colour = "hard" if budget.get("hard_reached") else ("signal" if budget.get("signal_reached") else "")
    mark = f'<i class="mark" style="left:{signal / hard * 100:.0f}%"></i>' if hard and signal else ""
    by_type = budget.get("by_type") or {}
    spent = ", ".join(
        f"{escape(str(kind))} {escape(str(count))}" for kind, count in sorted(by_type.items()) if count
    )
    words = f"{total} / {hard} cards · signal {signal}" + (f" · {spent}" if spent else "")
    return (
        f'<div class="budget-line {colour}" title="card budget: a spend, not progress">'
        '<span class="label">budget</span>'
        f'<span class="track"><i class="fill" style="width:{ratio * 100:.1f}%"></i>{mark}</span>'
        f'<span class="mono">{words}</span></div>'
    )


def _feed_table(document: dict[str, Any], *, compact: bool = False) -> str:
    commands = document.get("commands") or {}
    items = [item for item in commands.get("items") or [] if isinstance(item, dict)]
    parts = [_source_block(commands.get("source"), what="the command history")]
    if not items:
        if (commands.get("source") or {}).get("state") == "available":
            parts.append('<p class="empty">nothing has been recorded.</p>')
        return "\n".join(part for part in parts if part)
    rows = []
    for item in items:
        actor = _mapping(item.get("actor"))
        entity = _mapping(item.get("entity"))
        result = _mapping(item.get("result"))
        outcome = str(result.get("outcome") or "")
        reason = str(result.get("reason") or "")
        cut = 90 if compact else 200
        shown = reason if len(reason) <= cut else reason[: cut - 3] + "…"
        when = str(item.get("occurred_at") or "")
        when_shown = when[11:19] if compact and len(when) >= 19 else when
        cells = [
            f'<time title="{escape(when)}">{escape(when_shown)}</time>',
            escape(str(item.get("action") or "")),
            _entity_link(entity),
            (
                f'<span class="outcome-{escape(outcome)}">{escape(outcome)}</span> '
                f'<span class="reason" title="{escape(reason)}">{escape(shown)}</span>'
            ),
        ]
        if not compact:
            cells.insert(
                1,
                f'<span class="actor">{escape(str(actor.get("role") or ""))} {escape(str(actor.get("id") or ""))}</span>',
            )
        rows.append(cells)
    headers = ["when", "action", "on", "result"] if compact else ["when", "who", "action", "on", "result"]
    parts.append('<div class="feed">' + _rows(headers, rows) + "</div>")
    return "\n".join(part for part in parts if part)


def _entity_link(entity: dict[str, Any]) -> str:
    ref = str(entity.get("ref") or "")
    if not ref:
        return "—"
    if ref.startswith("sprint:"):
        return f'<a class="ref" href="/sprints/{quote(ref)}">{escape(ref)}</a>'
    if ref.startswith(("issue:", "product:")):
        return escape(ref)
    return _link(ref)


def _current_card_cell(item: dict[str, Any], current: dict[str, Any]) -> str:
    """The listing's `current card` column: which card, and where it has been standing since when."""
    standing = _card_standing(item)
    if not current.get("ref"):
        return f'<div class="reason">{standing}</div>' if standing else "—"
    said = _link(str(current["ref"]))
    return said + (f'<div class="reason">{standing}</div>' if standing else "")


def _sprint_projects(item: dict[str, Any]) -> list[str]:
    values = item.get("projects")
    if not isinstance(values, list):
        values = item.get("reservations")
    if isinstance(values, list):
        return [str(value) for value in values if value]
    value = item.get("project")
    return [str(value)] if value else []


def _comment_form(action: str, key: str, label: str) -> str:
    return (
        f'<form class="act" data-action="{escape(action)}" data-key="{escape(key)}" data-kind="comment">'
        f'<label for="comment-{escape(key)}">{escape(label)}</label>'
        f'<textarea id="comment-{escape(key)}" name="body" required placeholder="a comment the head working this will read"></textarea>'
        '<div class="row"><button type="submit">Comment</button></div>'
        '<p class="feedback"></p>'
        "</form>"
    )


def _move_form(ref: str) -> str:
    options = "".join(
        f'<option value="{escape(target)}">{escape(target.replace("_", " "))}</option>'
        for target in MOVE_TARGETS
    )
    return (
        f'<form class="act" data-action="/api/tasks/{quote(ref)}/move" data-key="move.{escape(ref)}" data-kind="move">'
        '<label for="move-target">Move this card to</label>'
        f'<select id="move-target" name="target">{options}</select>'
        '<div class="row" style="display:block"><label for="move-reason" style="display:block">why</label>'
        '<textarea id="move-reason" name="reason" required placeholder="why the owner moves it"></textarea></div>'
        '<div class="row"><label><input type="checkbox" name="sprint_override" id="move-override"> past its sprint\'s reservation</label>'
        '<input name="sprint_override_reason" id="move-override-reason" placeholder="why the sprint is overridden" size="34"></div>'
        '<div class="row"><button type="submit" class="danger">Move</button></div>'
        '<p class="feedback"></p>'
        "<p class=\"hint\">a decision on a parked card is the observer's; the owner's intervention is a move "
        "with a reason, and the audit says so.</p>"
        "</form>"
    )


def _close_form(ref: str) -> str:
    return (
        f'<form class="act" data-action="/api/sprints/{quote(ref)}/close" data-key="close.{escape(ref)}" data-kind="close">'
        '<label for="close-reason">Close this sprint</label>'
        '<input name="reason" id="close-reason" required placeholder="why the owner closes it" style="width:100%">'
        '<div class="row" style="display:block"><label for="close-closeout">closeout — what became of the work, written into state/knowledge</label>'
        '<textarea id="close-closeout" name="closeout" required></textarea></div>'
        '<div class="row" style="display:block"><label for="close-decisions">decisions, optional, as the CLI\'s decisions file</label>'
        '<textarea id="close-decisions" name="decisions" class="mono" placeholder="issues:\n  - {ref: issue:…, verdict: …, reason: …}\ncards:\n  - {ref: …, verdict: done|drop, reason: …}"></textarea></div>'
        '<div class="row"><button type="submit" class="danger">Close sprint</button></div>'
        '<p class="feedback"></p>'
        '<p class="hint">a close is not a completed Definition of Done; the sprint\'s own document says so.</p>'
        "</form>"
    )


#: The states a move may name, in the layer's spelling.
MOVE_TARGETS = ("ready", "in_progress", "done", "blocked", "issues", "validate", "assessment")


def _entries(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _external(url: Any, label: str | None = None) -> str:
    """A link out when the value is a web address, the text otherwise."""
    text = str(url or "")
    if text.startswith(("https://", "http://")):
        return f'<a href="{escape(text)}" rel="noreferrer">{escape(label or text)}</a>'
    return _or_dash(text)


def _long(text: Any, *, chars: int = 160) -> str:
    """Long text clamped to two lines, opened in place; the text is in the page once."""
    value = str(text or "").strip()
    if not value:
        return '<span class="empty">—</span>'
    if len(value) <= chars and "\n" not in value:
        return f'<span class="prose">{escape(value)}</span>'
    return f'<details class="text"><summary><span class="prose">{escape(value)}</span></summary></details>'


def _tabs(name: str, tabs: list[tuple[str, str, Any]]) -> str:
    """A no-script radio tab strip of `(label, body, count)`; `name` keeps strips apart."""
    if len(tabs) > MAX_TABS:
        raise ValueError(f"a tab strip holds at most {MAX_TABS} tabs, not {len(tabs)}")
    radios = "".join(
        f'<input type="radio" name="tabs-{escape(name)}" id="tab-{escape(name)}-{index}"'
        f"{' checked' if index == 0 else ''}>"
        for index in range(len(tabs))
    )
    labels = "".join(
        f'<label for="tab-{escape(name)}-{index}">{escape(label)}'
        + ("" if count is None else f'<span class="count">{escape(str(count))}</span>')
        + "</label>"
        for index, (label, _body, count) in enumerate(tabs)
    )
    panels = "".join(f'<section class="tab-panel">{body}</section>' for _label, body, _count in tabs)
    return f'<div class="tabs">{radios}<div class="tab-bar" role="presentation">{labels}</div>{panels}</div>'


#: Effort as lit bars (`extra` is Codex's old `xhigh`); an unknown word gets empty bars.
EFFORT_BARS: dict[str, int] = {
    "minimal": 1,
    "low": 1,
    "medium": 2,
    "high": 3,
    "xhigh": 4,
    "extra": 4,
    "max": 5,
}


#: How many bars the scale has.
EFFORT_SCALE = 5


#: Efforts meaning "no flag passed" (the CLI default decides); a PO session stored so reads "not set".
EFFORT_DEFAULT = {"", "default", "none"}


#: What a PO session stored with no explicit effort says in place of an effort.
PO_EFFORT_UNSET = "not set"


def _model_name(model: Any) -> str:
    """A model id as people say it (`claude-opus-5-5` is Opus 5.5, `gpt-6-sol` is GPT-6 Sol).

    Only recognised shapes are renamed, from their own parts; anything else is returned as is.
    """
    value = str(model or "").strip()
    claude = re.fullmatch(r"claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?", value)
    if claude:
        family, major, minor = claude.groups()
        return f"{family.title()} {major}{'.' + minor if minor else ''}"
    gpt = re.fullmatch(r"gpt-(\d+(?:\.\d+)?)(?:-([a-z]+))?", value)
    if gpt:
        version, variant = gpt.groups()
        return f"GPT-{version}{' ' + variant.title() if variant else ''}"
    return value


def _effort(effort: Any, *, unset: str = "CLI default") -> str:
    """The effort as bars and a word. No flag passed is hollow bars and `unset`, never zero."""
    word = str(effort or "").strip().lower()
    if word in EFFORT_DEFAULT:
        bars = "".join("<i></i>" for _ in range(EFFORT_SCALE))
        return f'<span class="segs unset" aria-hidden="true">{bars}</span><span>{escape(unset)}</span>'
    lit = EFFORT_BARS.get(word, 0)
    bars = "".join(f"<i{' class="on"' if index < lit else ''}></i>" for index in range(EFFORT_SCALE))
    shown = "xhigh" if word == "extra" else word
    return f'<span class="segs" aria-hidden="true">{bars}</span><span>{escape(shown)}</span>'


def _head_model(head: dict[str, Any]) -> tuple[str, str]:
    """What to call a head's model, and the hover title saying where that name came from."""
    resolved = str(head.get("resolved_model") or "")
    configured = str(head.get("model") or "")
    if resolved:
        said = f"{resolved} (the model that answered)"
        if configured and configured != resolved:
            said += f"; configured as {configured}"
        return _model_name(resolved), said
    if configured:
        return _model_name(configured), f"{configured} (configured; no run has reported its model yet)"
    return "unknown model", "neither the profile nor a run recorded a model"


#: A head's liveness as the pulse beside it: a live process, one that was lost, or neither known.
PULSE_OF: dict[str, str] = {
    "running": "live",
    "live": "live",
    "process_failed": "lost",
    "stopped": "lost",
    "lost": "lost",
}


def _head_effort(head: dict[str, Any]) -> Any:
    """The effort a run reported, else the configured one (same rule as the model)."""
    return head.get("resolved_effort") or head.get("effort")


def _head(
    role: str, head: dict[str, Any] | None, *, compact: bool = False, unset_effort: str = "CLI default"
) -> str:
    """One head: role, model, effort and process liveness; a missing model reads as unknown."""
    head = head or {}
    name, said = _head_model(head)
    profile = str(head.get("profile") or head.get("head") or "")
    adapter = str(head.get("adapter") or "")
    state = str(head.get("state") or "")
    title = " · ".join(
        part for part in (profile and f"profile {profile}", said, state and f"state {state}") if part
    )
    if compact:
        return (
            f'<span class="head-chip" title="{escape(title)}"><span class="role">{escape(role)}</span>'
            f"<b>{escape(name)}</b>{_effort(_head_effort(head), unset=unset_effort)}</span>"
        )
    pulse = PULSE_OF.get(state, "")
    idle = "" if pulse == "live" else " idle"
    who = " · ".join(part for part in (role, adapter, "" if pulse else state) if part)
    return (
        f'<div class="head{idle}" title="{escape(title)}">'
        f'<span class="pulse {pulse}" aria-label="{escape(state or "state unknown")}"></span>'
        f'<span class="who"><span class="role">{escape(who)}</span><span class="model">{escape(name)}</span></span>'
        f'<span class="effort">{_effort(_head_effort(head), unset=unset_effort)}</span></div>'
    )


def _js(value: str) -> str:
    """A string safe to paste into the script literal: no quote, no backslash, no tag opener."""
    return escape(value).replace("\\", "").replace("'", "").replace('"', "")


#: The empty option of the executor selects: leaves the role unpinned (nothing sent for it).
EXECUTOR_CHOICE = "the observer chooses"


def _field(name: str, label: str, control: str, errors: dict[str, str], hint: str = "") -> str:
    said = f'<p class="bad-field">{escape(errors[name])}</p>' if name in errors else ""
    note = f'<p class="hint">{escape(hint)}</p>' if hint else ""
    return (
        f'<div class="field"><label for="{escape(name)}">{escape(label)}</label>{note}{control}{said}</div>'
    )


def _text_field(
    name: str, label: str, submitted: dict[str, Any], errors: dict[str, str], *, hint: str
) -> str:
    value = escape(str(submitted.get(name) or ""))
    control = f'<textarea id="{escape(name)}" name="{escape(name)}">{value}</textarea>'
    return _field(name, label, control, errors, hint)


def _profile_option(item: dict[str, Any], chosen: str) -> str:
    """One head profile option: the registry's own `label`, `model` and `effort`."""
    value = str(item.get("id") or "")
    model = str(item.get("model") or "") or "the adapter's default model"
    effort = str(item.get("effort") or "") or "the adapter's default effort"
    return (
        f'<option value="{escape(value)}"{_selected(value == chosen)}>'
        f"{escape(str(item.get('label') or value))} — model {escape(model)}, effort {escape(effort)}"
        f" ({escape(value)})</option>"
    )


#: Said beside a submitted value no longer offered; kept so a resubmission is never silently changed.
NO_LONGER_OFFERED = "this installation no longer offers this choice"


def _kept_option(chosen: str, offered: list[str]) -> list[str]:
    """The submitted choice as an option of its own, when the catalogue stopped offering it."""
    if not chosen or chosen in offered:
        return []
    return [
        f'<option value="{escape(chosen)}" selected>{escape(chosen)} — {escape(NO_LONGER_OFFERED)}</option>'
    ]


def _kept_rows(name: str, chosen: set[str], offered: list[str]) -> list[str]:
    """The same, for the fields a person ticks rather than picks."""
    return [
        f'<label><input type="checkbox" name="{escape(name)}" value="{escape(value)}" checked> '
        f'{escape(value)} <span class="age">({escape(NO_LONGER_OFFERED)})</span></label>'
        for value in sorted(chosen)
        if value not in offered
    ]


def _selected(is_selected: bool) -> str:
    return " selected" if is_selected else ""


def _checked(is_checked: bool) -> str:
    return " checked" if is_checked else ""


def _po_indicator(section: dict[str, Any] | None) -> str:
    """The dashboard's PO panel: how many turns run, and the way in; a count, never a session."""
    if section is None:
        return ""
    if not section.get("available"):
        return '<span class="facts"><a href="/po">PO head</a> <span class="empty">— running turns could not be counted</span></span>'
    count = int((section.get("document") or {}).get("running") or 0)
    return f'<span class="facts"><a href="/po" id="po-indicator">{count} PO turn{"" if count == 1 else "s"} running</a></span>'

