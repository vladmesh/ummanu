"""Server-rendered presentation: overview."""

from __future__ import annotations

import json
from html import escape
from typing import Any
from urllib.parse import quote

from ummanu.web.doctor import DOCTOR_NOT_BUILT, unreadable as doctor_unreadable
from ummanu.web.page_assets import _ACTIONS_SCRIPT
from ummanu.web.page_parts import (
    _beside,
    _budget,
    _chip,
    _current_card_cell,
    _feed_table,
    _open_sprints,
    _or_dash,
    _panel,
    _po_indicator,
    _rows,
    _section,
    _short,
    _source_block,
    _sprint_items,
    _sprint_projects,
    _sprint_status_chip,
)
from ummanu.web.page_shell import LAMP_WORDS, _page, error
from ummanu.web.page_values import _mapping
from ummanu.webproto.reads import accepted


def dashboard(
    snapshot: dict[str, Any],
    *,
    pause: dict[str, Any] | None = None,
    sprints: dict[str, Any] | None = None,
    po: dict[str, Any] | None = None,
) -> str:
    """The operator's one screen: pipeline state, open sprints, work in flight, recent history.

    An unavailable side read (``{"available", "reason", "document"}``) is the marked block, not empty.
    Usage windows and health colour are the bottom bar's and the lamp's, read per request, not here.
    """
    installation = snapshot.get("installation") or {}
    open_items = _sprint_items(sprints)
    body = "\n".join(
        part
        for part in [
            '<div class="lead"><h1>Dashboard</h1>',
            f'<span class="age">read at {escape(str(snapshot.get("observed_at") or "an unknown time"))}</span></div>',
            '<section class="panel">',
            _pipeline_strip(pause, installation, po=po),
            '<div class="body" id="pause-feedback-holder"><p id="pause-feedback" class="feedback"></p></div>',
            "</section>",
            _attention(installation),
            '<section class="sprints-block">',
            '<header class="block-head"><h2>Open sprints</h2>'
            + (f'<span class="count">{len(open_items)}</span>' if open_items else "")
            + '<a class="more" href="/sprints">all sprints</a></header>',
            _open_sprints(sprints),
            "</section>",
        ]
        if part
    )
    return _page("Dashboard", body, script=_ACTIONS_SCRIPT, nav="dashboard")


#: The word for each pause mode, and the colour class that is its second reading.
PAUSE_WORDS: dict[str, tuple[str, str]] = {
    "running": ("running — the dispatcher claims cards and raises heads", "running"),
    "drain": ("drained — no new card is claimed; running heads finish their work", "drain"),
    "freeze": ("frozen — no new card is claimed and the heads were stopped", "freeze"),
    "soft": ("drained — no new card is claimed; running heads finish their work", "drain"),
    "hard": ("frozen — no new card is claimed and the heads were stopped", "freeze"),
}


def _pipeline_strip(
    section: dict[str, Any] | None, installation: dict[str, Any], *, po: dict[str, Any] | None = None
) -> str:
    """Is the pipeline running, and how to stop it; `installation` serves only the refused branch."""
    document, refused = _beside(section, what="whether the pipeline is paused")
    if document is None:
        return f'<div class="strip">{refused}{_po_indicator(po)}</div>'
    state = document.get("state") or {}
    paused = bool(state.get("paused"))
    mode = str(state.get("mode") or "") if paused else "running"
    words, colour = PAUSE_WORDS.get(
        mode, (f"{mode or 'unknown'} — a pause mode this page does not know", "unknown")
    )
    facts = []
    if paused:
        facts.append(f"since <b>{_or_dash(state.get('since'))}</b>")
        facts.append(f"by <b>{_or_dash(state.get('actor'))}</b>")
        if state.get("pause_reason"):
            facts.append(f"because {escape(str(state.get('pause_reason')))}")
    dispatcher = document.get("dispatcher") or {}
    facts.append(f"dispatcher <b>{_or_dash(dispatcher.get('phase'))}</b>")
    if dispatcher.get("tracked_cards") is not None:
        facts.append(f"tracking <b>{escape(str(dispatcher.get('tracked_cards')))}</b> card(s)")
    if paused:
        action = (
            '<form class="pause inline" data-action="/api/pause/resume" data-confirm="Resume the pipeline? '
            "A frozen pipeline's heads are relaunched in their workspaces.\">"
            '<button type="submit">Resume</button></form>'
        )
    else:
        # Draining is rare and stops the pipeline, so it is one click further away.
        action = (
            '<details class="drain"><summary>Drain…</summary>'
            '<form class="pause inline" data-action="/api/pause/drain" data-confirm="Drain the pipeline? No '
            'new card is claimed until it is resumed; running heads keep working.">'
            '<input name="reason" id="drain-reason" placeholder="why" required size="26" aria-label="reason">'
            '<button type="submit" class="danger">Drain</button></form></details>'
        )
    return "\n".join(
        [
            '<div class="strip">',
            f'<span class="light light-{escape(colour)}" title="{escape(words)}">{escape(words.split(" — ")[0])}</span>',
            f'<span class="facts">{" · ".join(facts)}</span>',
            f'<span class="push">{_po_indicator(po)}{action}</span>',
            "</div>",
            _source_block(state.get("source"), what="the pause flag"),
        ]
    )


def _attention(installation: dict[str, Any]) -> str:
    """The installation's problem under the strip while there is one; unreadable health is marked."""
    health = installation.get("health") or {}
    status = health.get("status")
    source = _source_block(health.get("source"), what="whether this installation is healthy")
    parts = [source]
    if isinstance(health.get("combined") or status, dict) and (health.get("combined") or status):
        problems = [str(item) for item in (health.get("combined") or status).get("problems") or []]
        if problems:
            more = f' <span class="muted">and {len(problems) - 1} more</span>' if len(problems) > 1 else ""
            parts.append(
                '<section class="attention"><b>Needs attention</b>'
                f"<span>{escape(problems[0])}{more}</span>"
                '<a href="/doctor">open doctor →</a></section>'
            )
    elif (health.get("source") or {}).get("state") == "available":
        parts.append('<p class="empty">the health collector answered with nothing.</p>')
    parts.append(_panel("Installation", _health_panel(installation), open_=False))
    return "\n".join(part for part in parts if part)


def _health_panel(installation: dict[str, Any]) -> str:
    """Health as the read layer summarizes it: problems by name, then facts."""
    health = installation.get("health") or {}
    status = health.get("status")
    parts: list[str] = []
    if not isinstance(status, dict) or not status:
        status = {}
    recorded = health.get("doctor") or {}
    if recorded:
        parts.append(f'<p class="muted">Recorded doctor: {escape(str(recorded.get("state")))}; run at {escape(str(recorded.get("run_at") or "unknown"))}.</p>')
        parts.append(_doctor_progress(recorded))
    if recorded.get("state") == "unknown":
        parts.append('<p class="muted">recorded doctor is unknown / not yet collected.</p>')
    problems = [str(item) for item in (health.get("combined") or status).get("problems") or []]
    combined = health.get("combined") or {}
    if problems or combined.get("findings"):
        parts.append(_doctor_list(combined["findings"]) if combined.get("findings") else
                     '<ul class="problems">' + "".join(f"<li>{escape(item)}</li>" for item in problems) + "</ul>")
    elif recorded.get("state") != "unknown":
        parts.append('<p class="muted">nothing needs attention.</p>')
    checkpoint = status.get("checkpoint") or {}
    resources = status.get("resources") or {}
    cards = status.get("cards") or {}
    dispatcher = status.get("dispatcher") or {}
    rows = [
        ["instance", _or_dash(installation.get("name"))],
        [
            "checkpoint",
            f"{_or_dash(checkpoint.get('status'))}"
            + (
                f' <span class="muted">lag {escape(str(checkpoint.get("lag_minutes")))} min, {escape(str(checkpoint.get("lag_commits")))} commit(s)</span>'
                if checkpoint.get("lag_minutes") is not None
                else ""
            ),
        ],
        ["disk free", _gib(resources.get("disk_free_bytes"))],
        ["memory free", _gib(resources.get("memory_available_bytes"))],
        ["load", _load(resources.get("load_average"))],
        ["cards on the board", _or_dash(cards.get("total"))],
        ["active attempts", _or_dash(dispatcher.get("active_attempts"))],
        ["last tick", _or_dash(dispatcher.get("last_tick_finished_at"))],
    ]
    for provider in status.get("providers") or []:
        if not isinstance(provider, dict):
            continue
        until = provider.get("until")
        rows.append([
            f"provider {escape(str(provider.get('resource')))}",
            escape(str(provider.get("state")))
            + (f' <span class="muted">until {escape(str(until))}</span>' if until else ""),
        ])
    parts.append(_rows(["", ""], rows))
    units = [
        unit
        for unit in status.get("units") or []
        if isinstance(unit, dict)
        and (
            unit.get("active") == "failed"
            or (str(unit.get("kind")) == "timer" and unit.get("active") not in (None, "active"))
        )
    ]
    if units:
        parts.append(
            '<p class="facts">units not active: '
            + ", ".join(
                f"<b>{escape(str(unit.get('name')))}</b> ({escape(str(unit.get('active')))})"
                for unit in units
            )
            + "</p>"
        )
    return "\n".join(part for part in parts if part)


def _gib(value: Any) -> str:
    try:
        return f"{float(value) / (1024**3):.1f} GiB"
    except (TypeError, ValueError):
        return "—"


def _load(value: Any) -> str:
    if not isinstance(value, list) or not value:
        return "—"
    try:
        return " ".join(f"{float(item):.2f}" for item in value[:3])
    except (TypeError, ValueError):
        return "—"


#: Doctor page severity groups, in reading order: what makes the lamp red first.
SEVERITY_GROUPS: tuple[tuple[str, str], ...] = (
    ("red", "Red — the installation cannot be trusted to run work"),
    ("yellow", "Yellow — running, but a person should look"),
    ("neutral", "Accepted findings"),
)


def doctor(section: dict[str, Any] | None) -> str:
    """The page behind the lamp: problems by code and severity, none, or unreadable health and why."""
    document = section.get("document") if isinstance(section, dict) and section.get("available") else None
    if not isinstance(document, dict):
        reason = (
            str(section.get("reason") or "installation health was not read")
            if isinstance(section, dict)
            else DOCTOR_NOT_BUILT
        )
        document = doctor_unreadable(reason)
    colour = str(document.get("colour") or "red")
    colour = colour if colour in LAMP_WORDS else "red"
    problems = [problem for problem in document.get("problems") or [] if isinstance(problem, dict)]
    parts = [
        '<div class="lead"><h1>Doctor</h1>',
        f'<span class="age">read at {escape(str(document.get("observed_at") or "an unknown time"))}</span></div>',
        (
            f'<div class="strip"><span class="light light-{escape(_LIGHT_OF[colour])}" '
            f'title="{escape(LAMP_WORDS[colour])}">{escape(colour)}</span>'
            f'<span class="facts">{escape(LAMP_WORDS[colour])}</span></div>'
        ),
    ]
    if not document.get("readable"):
        parts.append(
            '<p class="unavailable"><b>this installation\'s health could not be read.</b> '
            f"{escape(str(document.get('reason') or 'no reason was recorded'))}<br>"
            "An unread installation is not a healthy one, so this is red and not green.</p>"
        )
    recorded = document.get("doctor") or {}
    parts.append(
        f'<p>Recorded doctor: {escape(str(recorded.get("state") or "unknown"))}; '
        f'mode {escape(str(recorded.get("mode") or "unknown"))}; '
        f'run at {escape(str(recorded.get("run_at") or "unknown"))}; '
        f'completed at {escape(str(recorded.get("completed_at") or "unknown"))}; '
        f'exit {escape(str(recorded.get("exit_code")))}</p>'
    )
    parts.append(_doctor_progress(recorded))
    if recorded.get("state") == "unknown":
        parts.append('<p class="muted">recorded doctor is unknown / not yet collected.</p>')
    if isinstance(document.get("source"), dict):
        parts.append(_source_block(document["source"], what="status health"))
    if problems:
        for severity, heading in SEVERITY_GROUPS:
            group = [problem for problem in problems if problem.get("severity") == severity]
            if group:
                parts.append(_panel(heading, _doctor_list(group), open_=True, count=len(group)))
        other = [
            problem
            for problem in problems
            if problem.get("severity") not in {severity for severity, _ in SEVERITY_GROUPS}
        ]
        if other:
            parts.append(
                _panel(
                    "Classified as neither — and so not green either",
                    _doctor_list(other),
                    open_=True,
                    count=len(other),
                )
            )
    elif document.get("readable") and recorded.get("state") != "unknown":
        parts.append(
            '<p class="empty">no problem is recorded for this installation: '
            "every check this installation records answered, and none of them is a finding.</p>"
        )
    parts.append(
        '<p class="muted">This page combines status health with the latest periodically recorded <code>ummanu doctor</code> findings. The read time above is the web reading time. This page launches no doctor, '
        "opens no SSH and touches no provider.</p>"
    )
    return _page("Doctor", "\n".join(part for part in parts if part), nav="")


#: The lamp's colour, said in the stylesheet's own words for the light on the page.
_LIGHT_OF = {"green": "ok", "yellow": "attention", "red": "bad", "unknown": "unknown"}


def _doctor_progress(recorded: dict[str, Any]) -> str:
    collecting = recorded.get("collecting")
    if not isinstance(collecting, dict):
        return ""
    return f'<p class="muted">run in progress since {escape(str(collecting.get("run_at") or "unknown"))}</p>'


def _doctor_list(problems: list[dict[str, Any]]) -> str:
    """One problem per line: the code it is known by, then the sentence a person reads."""
    rows = [
        [
            f"<code>{escape(str(problem.get('code') or '—'))}</code>",
            escape(str(problem.get("message") or "")) + (
                f"<br>accepted: {escape(str(problem['acceptance_reason']))}"
                if problem.get("source") == "doctor" and accepted(problem) else ""
            ) + (
                "<br><code>" + escape(json.dumps({key: value for key, value in problem.items()
                                              if key not in {"code", "message", "severity", "source", "accepted", "acceptance_reason"}}, sort_keys=True)) + "</code>"
                if any(key not in {"code", "message", "severity", "source", "accepted", "acceptance_reason"} for key in problem) else ""
            ),
        ]
        for problem in problems
    ]
    return _rows(["", ""], rows)


def commands(document: dict[str, Any]) -> str:
    """The whole command history, a page at a time, newest first."""
    listing = document.get("commands") or {}
    older = ""
    if listing.get("has_more") and listing.get("next_cursor"):
        older = f'<a class="more" href="/history?cursor={quote(str(listing["next_cursor"]))}&amp;limit={int(document.get("limit") or 25)}">older →</a>'
    body = "\n".join(
        [
            '<div class="lead"><h1>History</h1>',
            f'<span class="age">read at {escape(str(document.get("observed_at") or "an unknown time"))}</span></div>',
            _panel("Commands, newest first", _feed_table(document), more=older),
        ]
    )
    return _page("History", body, nav="history")


#: How the two owner event classes read on the list: the badge's words and tone.
OWNER_EVENT_CLASSES: dict[str, tuple[str, str]] = {
    "needs_owner": ("needs the owner", "bad"),
    "notice": ("notice", "accent"),
}


def owner_event_subject(subject: str) -> str:
    """A subject ref as a link to its page where it has one: a card, a sprint, a PO session."""
    if not subject:
        return ""
    if subject.startswith("po-session:"):
        session = subject.removeprefix("po-session:")
        return f'<a class="ref" href="/po/sessions/{quote(session)}">{escape(subject)}</a>'
    if subject.startswith("sprint:"):
        return f'<a class="ref" href="/sprints/{quote(subject)}">{escape(subject)}</a>'
    if ":" in subject:
        return f'<span class="ref">{escape(subject)}</span>'
    return f'<a class="ref" href="/tasks/{quote(subject)}">{escape(subject)}</a>'


def owner_events(document: dict[str, Any]) -> str:
    """The bell's list: unread by default or all, newest first, open `needs_owner` pinned.

    "Mark all read" takes notices only; a `needs_owner` event whose card still waits for the owner
    has no button (its card clears it).
    """
    unread_only = bool(document.get("unread_only"))
    events = [event for event in document.get("events") or [] if isinstance(event, dict)]
    back = f'<input type="hidden" name="view" value="{"unread" if unread_only else "all"}">'
    rows = []
    for event in events:
        label, tone = OWNER_EVENT_CLASSES.get(
            str(event.get("class") or ""), (str(event.get("class") or "?"), "")
        )
        unread = bool(event.get("unread"))
        subject = str(event.get("subject_ref") or "")
        state = (
            _chip("unread", "warn")
            if unread
            else (f'<span class="age">read {escape(str(event.get("read_at") or ""))}</span>')
        )
        if not unread:
            action = ""
        elif event.get("class") == "needs_owner" and event.get("held"):
            action = (
                f'<span class="held">stays unread: {escape(subject)}: '
                + ("current handover has no recorded owner answer" if event.get("kind") == "card_handed_to_owner"
                   else "PO escalation remains unresolved: " + escape(str(event.get("text") or ""))) + "</span>"
            )
        else:
            action = (
                f'<form method="post" action="/owner-events/{int(event.get("id") or 0)}/read">{back}'
                '<button type="submit" class="quiet">Mark read</button></form>'
            )
        classes = " ".join(
            name for name, on in (("unread", unread), ("pinned", bool(event.get("pinned")))) if on
        )
        rows.append(
            f'<li class="{classes}" id="owner-event-{int(event.get("id") or 0)}">'
            f"{_chip(label, tone)}"
            f"<code>{escape(str(event.get('kind') or ''))}</code>"
            f"{owner_event_subject(subject)}"
            f"{state}"
            f'<time class="when age">{escape(str(event.get("created_at") or ""))}</time>'
            f'<div class="text">{escape(str(event.get("text") or ""))}</div>'
            f"{action}</li>"
        )
    all_mark = ' aria-current="true"' if not unread_only else ""
    unread_mark = ' aria-current="true"' if unread_only else ""
    disabled = ' disabled title="No unread notices to mark; bulk read marks notices only"' if not document.get("notice_count", 0) else ""
    actions = (
        '<div class="owner-events-actions">'
        f'<div class="filters"><a href="/owner-events"{unread_mark}>Unread</a>'
        f'<a href="/owner-events?all=1"{all_mark}>All</a></div>'
        f'<form method="post" action="/owner-events/read-all">{back}'
        f'<button type="submit" class="quiet"{disabled}>Mark all notices read</button></form></div>'
    )
    actions += '<p>Bulk read marks notices only. Other events can be marked read individually unless held.</p>'
    if not document.get("notice_count", 0):
        actions += '<p>No unread notices to mark.</p>'
    listing = _section(
        document.get("source"),
        events,
        what="the owner events",
        empty="no unread event." if unread_only else "no owner event yet.",
        table=f'<ol class="owner-events">{"".join(rows)}</ol>',
    )
    body = "\n".join(
        [
            '<div class="lead"><h1>Owner events</h1>',
            f'<span class="age">read at {escape(str(document.get("observed_at") or "an unknown time"))}</span></div>',
            f'<p role="status">{escape(str(document.get("read_feedback") or ""))}</p>',
            _panel(
                "What needs you, and what you should know", actions + listing, count=document.get("unread")
            ),
        ]
    )
    return _page("Owner events", body)


def sprints_page(
    document: dict[str, Any], *, view: str = "active", search: str = "", project: str = ""
) -> str:
    """Active work and a searchable archive, without putting history in primary navigation."""
    listing = document.get("sprints") or {}
    items = [item for item in listing.get("items") or [] if isinstance(item, dict)]
    project_choices = sorted({value for item in items for value in _sprint_projects(item)})
    view = "archive" if view == "archive" else "active"
    if view == "archive":
        needle = search.casefold().strip()
        items = [
            item
            for item in items
            if not needle or needle in f"{item.get('ref', '')} {item.get('goal', '')}".casefold()
        ]
        if project:
            items = [item for item in items if project in _sprint_projects(item)]
    active_mark = ' aria-current="true"' if view == "active" else ""
    archive_mark = ' aria-current="true"' if view == "archive" else ""
    filters = (
        f'<a href="/sprints"{active_mark}>Active</a><a href="/sprints?view=archive"{archive_mark}>Archive</a>'
    )
    rows = []
    for item in items:
        ref = str(item.get("ref") or "")
        goal = str(item.get("goal") or "")
        current = _mapping(item.get("current_task"))
        observer = _mapping(item.get("observer"))
        launch = observer.get("launch") or {}
        budget = _mapping(item.get("budget"))
        rows.append(
            [
                f'<a class="ref" href="/sprints/{quote(ref)}">{escape(ref)}</a>',
                _sprint_status_chip(item),
                escape(", ".join(_sprint_projects(item)) or str(item.get("product") or "—")),
                escape(goal if len(goal) <= 110 else goal[:107].rstrip() + "…"),
                _current_card_cell(item, current),
                (
                    f'<span class="state state-{escape(str(launch.get("state") or ""))}">{escape(str(launch.get("state") or "—"))}</span>'
                    if launch
                    else "—"
                ),
                _budget(budget) if budget else "—",
            ]
        )
    table = _section(
        listing.get("source"),
        items,
        what="which sprints exist",
        empty="no sprint matches this filter.",
        table=_rows(["sprint", "status", "product", "goal", "current card", "observer", "budget"], rows),
    )
    archive_form = ""
    if view == "archive":
        options = '<option value="">all projects</option>' + "".join(
            f'<option value="{escape(value)}"{" selected" if value == project else ""}>{escape(value)}</option>'
            for value in project_choices
        )
        archive_form = (
            '<form class="inline" method="get" action="/sprints"><input type="hidden" name="view" value="archive">'
            f'<div><label for="archive-search">search archive</label><input id="archive-search" name="q" value="{escape(search)}" placeholder="name or goal"></div>'
            f'<div><label for="archive-project">project</label><select id="archive-project" name="project">{options}</select></div>'
            '<button type="submit" class="quiet">Filter</button></form>'
        )
    body = "\n".join(
        [
            '<div class="lead"><h1>Sprints</h1>',
            f'<span class="age">read at {escape(str(document.get("observed_at") or "an unknown time"))}</span></div>',
            _panel(
                "Sprints",
                f'<div class="filters">{filters}</div>{archive_form}' + table,
                count=len(items) or None,
            ),
        ]
    )
    return _page("Sprints", body, nav="sprints")


def projects_page(snapshot: dict[str, Any]) -> str:
    projects = snapshot.get("projects") or {}
    items = [item for item in projects.get("items") or [] if isinstance(item, dict)]
    rows = [
        [
            f'<a href="/projects/{quote(str(item.get("id") or ""))}">{escape(str(item.get("id") or ""))}</a>',
            _or_dash(item.get("repo")),
            _or_dash(item.get("adapter")),
            "enabled" if item.get("enabled") else "disabled",
        ]
        for item in items
    ]
    content = _section(
        projects.get("source"),
        items,
        what="which projects are registered",
        empty="this installation has no registered project.",
        table=_rows(["project", "repository", "adapter", "status"], rows),
    )
    return _page(
        "Projects",
        '<div class="lead"><h1>Projects</h1></div>' + _panel("Projects", content, count=len(items) or None),
        nav="projects",
    )


def project_page(snapshot: dict[str, Any], *, project_id: str, sprints: dict[str, Any] | None) -> str:
    projects = snapshot.get("projects") or {}
    item = next(
        (value for value in projects.get("items") or [] if str(value.get("id") or "") == project_id), None
    )
    if not isinstance(item, dict):
        return error(404, "project_not_found", f"project {project_id} is not registered")
    rows = [
        ["repository", _or_dash(item.get("repo"))],
        ["adapter", _or_dash(item.get("adapter"))],
        ["default branch", _or_dash(item.get("default_branch"))],
        ["status", "enabled" if item.get("enabled") else "disabled"],
    ]
    document, refused = _beside(sprints, what="this project's sprints")
    sprint_items = (
        []
        if document is None
        else [
            value
            for value in (document.get("sprints") or {}).get("items") or []
            if isinstance(value, dict)
            and project_id in (_sprint_projects(value) or [str(value.get("product") or "")])
        ]
    )
    sprint_rows = [
        [
            f'<a href="/sprints/{quote(str(value.get("ref") or ""))}">{escape(str(value.get("ref") or ""))}</a>',
            _sprint_status_chip(value),
            escape(_short(value.get("goal"), 120)),
        ]
        for value in sprint_items
    ]
    sprint_body = refused or (
        _rows(["sprint", "status", "goal"], sprint_rows)
        if sprint_rows
        else '<p class="empty">no sprint belongs to this project.</p>'
    )
    body = (
        '<div class="lead"><h1>'
        + escape(project_id)
        + "</h1></div>"
        + _panel("Project", _rows(["", ""], rows))
        + '<div style="margin-top:1rem">'
        + _panel("Sprints", sprint_body, count=len(sprint_items) or None, open_=False)
        + "</div>"
    )
    return _page(project_id, body, nav="projects", crumbs=(("Projects", "/projects"), (project_id, "")))


def _installation(installation: dict[str, Any]) -> str:
    health = installation.get("health") or {}
    status = health.get("status")
    rows = [
        ["instance", _or_dash(installation.get("instance"))],
        ["name", _or_dash(installation.get("name"))],
        ["data dir", _or_dash(installation.get("data_dir"))],
    ]
    parts = [
        _rows(["", ""], rows),
        _source_block(health.get("source"), what="whether this installation is healthy"),
    ]
    if isinstance(status, dict):
        summary = _mapping(status.get("summary"))
        overall = summary.get("state") or status.get("state") or "collected"
        parts.append(f'<p>health: <span class="state">{escape(str(overall))}</span></p>')
    elif (health.get("source") or {}).get("state") == "available":
        parts.append('<p class="empty">the health collector answered with nothing.</p>')
    return "\n".join(part for part in parts if part)

