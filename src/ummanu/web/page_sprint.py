"""Server-rendered presentation: sprint."""

from __future__ import annotations

import json
import re
from html import escape
from typing import Any
from urllib.parse import quote

from ummanu.web import markdown
from ummanu.web.page_assets import _ACTIONS_SCRIPT, _SPRINT_FORM_SCRIPT
from ummanu.web.page_parts import (
    EXECUTOR_CHOICE,
    GATE_TONES,
    _board_link,
    _budget_line,
    _checked,
    _chip,
    _close_form,
    _comment_form,
    _current_card_box,
    _dod_scale,
    _entries,
    _external,
    _field,
    _kept_option,
    _kept_rows,
    _link,
    _long,
    _panel,
    _profile_option,
    _rows,
    _selected,
    _source_block,
    _sprint_heads,
    _sprint_status_chip,
    _state_chip,
    _tabs,
    _text_field,
    _waiting_chip,
)
from ummanu.web.page_shell import _page
from ummanu.web.page_values import _mapping

#: The observer's launch state in words; colour is a second reading only. The sentence beside it
#: is the layer's reason, never rewritten.
LAUNCH_WORDS: dict[str, str] = {
    "not_started": "saved — no observer is up for it yet",
    "running": "running — an observer head is up",
    "unavailable": "not established — this could not be read at all",
    "stopped": "stopped — an observer was raised for it and is not alive",
    "not_declared": "no observer — this sprint declared none, so none is raised",
}


#: Said under the submit button: there is no separate "start" action (`ummanu.webproto.sprint_ops`).
START_NOTICE = (
    "starting a sprint is opening it with an observer: there is no separate launch action, and the "
    "production tick raises one observer head for each open sprint that has none"
)


def sprint_form(
    options: dict[str, Any] | None,
    *,
    submitted: dict[str, Any],
    errors: dict[str, str],
    refusal: dict[str, Any] | None = None,
    catalogue: str | None = None,
    reissued: bool = False,
) -> str:
    """The "new sprint" form; `options` is `None` when the catalogue was unreadable during a refusal."""
    options = options or {}
    heads = options.get("heads") or {}
    products = options.get("products") or {}
    issues = options.get("issues") or {}
    projects = options.get("projects") or {}
    body = "\n".join(
        part
        for part in [
            "<h2>new sprint</h2>",
            _refusal_block(refusal, reissued=reissued),
            _errors_block(errors),
            ""
            if catalogue is None
            else f'<p class="unavailable"><b>could not find out what this installation offers:</b> '
            f"{escape(catalogue)}</p>",
            '<form class="sprint" id="sprint-form" method="post" action="/sprints">',
            f'<input type="hidden" name="request_id" value="{escape(str(submitted.get("request_id") or ""))}">',
            _product_field(products, submitted, errors),
            _issue_field(issues, submitted, errors),
            _project_field(projects, submitted, errors),
            _text_field(
                "goal",
                "goal",
                submitted,
                errors,
                hint="what this sprint is for, in the owner's own words",
            ),
            _text_field(
                "definition_of_done",
                "definition of done",
                submitted,
                errors,
                hint="what would make this sprint done",
            ),
            _observer_field(heads, submitted, errors),
            _executor_field("worker", "worker", heads, submitted),
            _executor_field("reviewer", "reviewer", heads, submitted),
            _text_field(
                "local_run_exceptions", "local run exceptions (JSON)", submitted, errors,
                hint="optional list of {project, argv, rationale}; empty means none; each project must be reserved here",
            ),
            '<button type="submit">start this sprint</button>',
            "</form>",
            f'<p class="hint empty">{escape(START_NOTICE)}</p>',
            (
                '<p class="hint empty">this form carries one request id for as long as it is open, '
                "so a double click, a retry and a reconnection all reach the same sprint rather "
                "than opening a second one.</p>"
            ),
        ]
        if part
    )
    return _page(
        "New sprint",
        body,
        script=_SPRINT_FORM_SCRIPT,
        nav="new-sprint",
    )


def _refusal_block(refusal: dict[str, Any] | None, *, reissued: bool = False) -> str:
    """What the layer said about this submission, and what may safely be done about it.

    A part-done create left a sprint: resubmit this form unchanged (same request id). Any other
    refusal left nothing and spent its id, so the form carries a new one and says so.
    """
    if not refusal:
        return ""
    message = escape(str(refusal.get("message") or "this submission was refused"))
    data = refusal.get("data") or {}
    action = data.get("action") or {}
    if not action.get("repeat_request"):
        reissue = (
            " Nothing was created. This form now carries a new request id, so correcting a field "
            "and submitting it again is a new request and is safe."
            if reissued
            else ""
        )
        return f'<p class="refused"><b>this sprint was not opened.</b> {message}{reissue}</p>'
    reference = str(action.get("reference") or "")
    named = f' It is <a href="/sprints/{quote(reference)}">{escape(reference)}</a>.' if reference else ""
    return (
        '<p class="pending"><b>this sprint exists and the request that opened it did not finish.</b> '
        f"{message}{named} Submitting this form again is safe: it carries the same request id, so it "
        "picks up the sprint that already exists rather than opening a second one. Do not start over "
        "with a new form.</p>"
    )


def _errors_block(errors: dict[str, str]) -> str:
    if not errors:
        return ""
    items = "".join(
        f"<li><b>{escape(name.replace('_', ' '))}</b>: {escape(reason)}</li>"
        for name, reason in errors.items()
    )
    return (
        f'<div class="refused"><b>this form is not complete, so nothing was opened.</b><ul>{items}</ul></div>'
    )


def _product_field(products: dict[str, Any], submitted: dict[str, Any], errors: dict[str, str]) -> str:
    items = list(products.get("items") or [])
    chosen = str(submitted.get("product") or "")
    unavailable = _source_block(products.get("source"), what="which products this installation has")
    if not items and not chosen:
        empty = unavailable or '<p class="empty">this installation has no product to open a sprint for.</p>'
        return _field("product", "product", empty, errors)
    options = ['<option value="">choose a product</option>']
    for item in items:
        value = str(item.get("id") or "")
        options.append(
            f'<option value="{escape(value)}"{_selected(value == chosen)}>'
            f"{escape(str(item.get('label') or value))} ({escape(value)})</option>"
        )
    options += _kept_option(chosen, [str(item.get("id") or "") for item in items])
    control = unavailable + f'<select id="product" name="product">{"".join(options)}</select>'
    return _field("product", "product", control, errors, "the product whose issues this sprint serves")


def _issue_field(issues: dict[str, Any], submitted: dict[str, Any], errors: dict[str, str]) -> str:
    """The open issues, each naming its product so the list works without the narrowing script."""
    items = list(issues.get("items") or [])
    chosen = set(submitted.get("issues") or [])
    unavailable = _source_block(issues.get("source"), what="which issues are open")
    if not items and not chosen:
        empty = (
            unavailable or '<p class="empty">no open issue is on this board, so no sprint can serve one.</p>'
        )
        return _field("issues", "issues this sprint serves", empty, errors)
    rows = []
    for item in items:
        ref = str(item.get("ref") or "")
        product = str(item.get("product") or "")
        rows.append(
            f'<label data-product="{escape(product)}">'
            f'<input type="checkbox" name="issues" value="{escape(ref)}"{_checked(ref in chosen)}> '
            f"{escape(ref)} — {escape(str(item.get('label') or ref))} "
            f'<span class="age">({escape(product) or "no product"})</span></label>'
        )
    rows += _kept_rows("issues", chosen, [str(item.get("ref") or "") for item in items])
    control = unavailable + f'<div class="choices" id="issues">{"".join(rows)}</div>'
    return _field(
        "issues", "issues this sprint serves", control, errors, "the open issues of the product above"
    )


def _project_field(projects: dict[str, Any], submitted: dict[str, Any], errors: dict[str, str]) -> str:
    """The registered projects, each saying whether an open sprint holds it.

    `reserved_by` `null` (index unreadable) is said apart from an empty list.
    """
    items = list(projects.get("items") or [])
    chosen = set(submitted.get("projects") or [])
    unavailable = _source_block(projects.get("source"), what="which projects are registered")
    if not items and not chosen:
        empty = unavailable or '<p class="empty">this installation has no registered project to reserve.</p>'
        return _field("projects", "projects this sprint reserves", empty, errors)
    rows = []
    for item in items:
        value = str(item.get("id") or "")
        reserved = item.get("reserved_by")
        if reserved is None:
            held = "whether an open sprint holds it could not be established"
        elif reserved:
            held = "an open sprint already holds it: " + ", ".join(str(one) for one in reserved)
        else:
            held = ""
        note = f' <span class="age">({escape(held)})</span>' if held else ""
        rows.append(
            f'<label><input type="checkbox" name="projects" value="{escape(value)}"'
            f"{_checked(value in chosen)}> {escape(str(item.get('label') or value))}{note}</label>"
        )
    rows += _kept_rows("projects", chosen, [str(item.get("id") or "") for item in items])
    control = unavailable + f'<div class="choices">{"".join(rows)}</div>'
    return _field(
        "projects",
        "projects this sprint reserves",
        control,
        errors,
        "a project an open sprint already holds is refused by the board, not by this page",
    )


def _observer_field(heads: dict[str, Any], submitted: dict[str, Any], errors: dict[str, str]) -> str:
    """The observer select: only profiles passing `check_observer_profile`.

    `none` (no observer, so nothing starts) is not offered; it stays legal for `ummanu sprint create`.
    """
    items = [item for item in (heads.get("items") or []) if item.get("observer")]
    chosen = str(submitted.get("observer") or "")
    unavailable = _source_block(heads.get("source"), what="which head profiles this installation has")
    if not items and not chosen:
        empty = (
            unavailable
            or '<p class="empty">this installation offers no profile that may observe a sprint.</p>'
        )
        return _field("observer", "observer", empty, errors)
    options = ['<option value="">choose an observer</option>']
    options += [_profile_option(item, chosen) for item in items]
    options += _kept_option(chosen, [str(item.get("id") or "") for item in items])
    control = unavailable + f'<select id="observer" name="observer">{"".join(options)}</select>'
    return _field("observer", "observer", control, errors, "the head that runs this sprint; it is required")


def _executor_field(name: str, label: str, heads: dict[str, Any], submitted: dict[str, Any]) -> str:
    """One optional executor pin; the empty default (the observer picks) writes no field for the role."""
    items = list(heads.get("items") or [])
    chosen = str(submitted.get(name) or "")
    options = [f'<option value=""{_selected(not chosen)}>{escape(EXECUTOR_CHOICE)}</option>']
    options += [_profile_option(item, chosen) for item in items]
    options += _kept_option(chosen, [str(item.get("id") or "") for item in items])
    control = f'<select id="{escape(name)}" name="{escape(name)}">{"".join(options)}</select>'
    return _field(
        name,
        f"{label} (optional)",
        control,
        {},
        f"leave this as “{EXECUTOR_CHOICE}” and the role stays unpinned",
    )


def sprint(document: dict[str, Any]) -> str:
    """One sprint: its goal, the card in hand, who works it, and what the observer decided.

    Each fact is drawn once (header chips, "Now" panel, "Observer's call", then the tabs).
    """
    ref = str(document.get("ref") or "")
    sprint_section = document.get("sprint") or {}
    value = sprint_section.get("value")
    observer = document.get("observer") or {}
    work = {**(value or {}), **(document.get("work") or {})}
    chips = []
    if value:
        chips.append(_sprint_status_chip(value))
        if value.get("product"):
            chips.append(_chip(str(value.get("product"))))
        chips.append(_waiting_chip(work))
    is_open = str((value or {}).get("status") or "") == "open"
    body = "\n".join(
        part
        for part in [
            '<div class="hero">',
            f"<h1>{escape(ref)}</h1>",
            f'<div class="chips">{"".join(chips)}</div>',
            f'<a href="{escape(_board_link(sprint=ref))}">board</a>',
            f'<span class="age" style="margin-left:auto">read at {escape(str(document.get("observed_at") or "an unknown time"))}</span>',
            (
                f'<div class="title">{_long((value or {}).get("goal"), chars=200)}</div>'
                if value and value.get("goal")
                else ""
            ),
            "</div>",
            _source_block(sprint_section.get("source"), what="what this sprint is"),
            '<div class="grid">',
            '<div class="col">',
            _panel("Now", _sprint_now(work, observer)),
            _waiting_on(work.get("waiting_on")),
            _panel("Observer's call", _observer_call(work), more=_recorded_at(work)),
            f'<section class="panel">{_sprint_tabs(ref, value, work)}</section>',
            "</div>",
            '<div class="col">',
            _panel("Sprint", _sprint_fields(ref, value, observer)),
            _panel(
                "Tell the observer",
                _comment_form(
                    f"/api/sprints/{quote(ref)}/comment",
                    f"sprint.{ref}",
                    "A comment the observer reads on its next wake",
                )
                + (
                    '<details class="more-actions"><summary>More actions</summary>'
                    f"{_close_form(ref)}</details>"
                    if is_open
                    else ""
                ),
            ),
            "</div></div>",
        ]
        if part
    )
    return _page(
        f"Sprint {ref}",
        body,
        script=_ACTIONS_SCRIPT,
        nav="sprints",
        crumbs=((ref, ""),),
    )


def _sprint_now(work: dict[str, Any], observer: dict[str, Any]) -> str:
    """The card in hand, whether its gate passed, what the dispatcher holds, who works it, the spend."""
    parts = [_current_card_box(work)]
    tiles = []
    checks = _mapping(work.get("checks"))
    if checks:
        gate = _mapping(checks.get("gate"))
        state = str(gate.get("state") or checks.get("state") or "unknown")
        tone = GATE_TONES.get(state, "ok" if state == "green" else "warn" if state == "not_green" else "")
        tiles.append(_tile("CI gate", state.replace("_", " "), str(checks.get("reason") or ""), tone))
    waiting = _mapping(work.get("waiting"))
    if waiting:
        tiles.append(
            _tile("Observer is", str(waiting.get("state") or "unknown"), str(waiting.get("reason") or ""))
        )
    if tiles:
        parts.append(f'<div class="tiles">{"".join(tiles)}</div>')
    heads = _sprint_heads({**work, "observer": observer})
    if heads:
        parts.append(heads)
    if "dod" in work:
        parts.append(_dod_scale(work["dod"]))
    budget = _mapping(work.get("budget"))
    if budget:
        parts.append(_budget_line(budget))
    return "\n".join(parts)


#: How a sprint's `waiting_on` kind reads on its page.
WAITING_ON_LABELS = {"run": "a run", "owner": "the owner", "po": "the PO", "dependency": "a dependency card", "observer": "the observer"}


def _waiting_on(items: Any) -> str:
    """What the sprint waits for, one line per card linking it; nothing when nothing is."""
    entries = _entries(items)
    if not entries:
        return ""
    rows = [
        [
            _chip(
                WAITING_ON_LABELS.get(str(entry.get("kind") or ""), str(entry.get("kind") or "unknown")),
                "warn" if entry.get("kind") == "owner" else "",
            ),
            _link(str(entry.get("card") or "")),
            _linked_text(str(entry.get("detail") or "")) + (" · " + _link(str(entry["holder"])) if entry.get("holder") else ""),
        ]
        for entry in entries
    ]
    return _panel("Waiting on", _rows(["on", "card", "what"], rows), count=len(rows))


_URL_RE = re.compile(r"https?://[^\s<>\"')]+")


def _linked_text(text: str) -> str:
    """Escaped text whose web addresses are links: a run a detail line names is one click away."""
    out, last = [], 0
    for found in _URL_RE.finditer(text):
        out.append(escape(text[last : found.start()]))
        out.append(_external(found.group(0)))
        last = found.end()
    out.append(escape(text[last:]))
    return "".join(out)


def _tile(label: str, value: str, reason: str, tone: str = "") -> str:
    """One fact in a small box: what it is, its value in a word, and the sentence behind it."""
    return (
        f'<div class="tile{" tile-" + tone if tone else ""}"><span class="label">{escape(label)}</span>'
        f'<b>{escape(value)}</b><span class="reason">{escape(reason)}</span></div>'
    )


def _recorded_at(work: dict[str, Any]) -> str:
    decision = _mapping(work.get("decision"))
    entry = _mapping(decision.get("entry"))
    when = str(entry.get("recorded_at") or "")
    return (
        f'<span class="age more" title="{escape(when)}">{escape(when[11:19] or when)}</span>' if when else ""
    )


def _observer_call(work: dict[str, Any]) -> str:
    """The observer's last decision in prose, its reasons one click away, and the next step."""
    decision = _mapping(work.get("decision"))
    entry = _mapping(decision.get("entry"))
    if not entry:
        return '<p class="empty">the observer has recorded no decision for this sprint yet.</p>'
    freshness = (decision.get("freshness") or {}).get("value") or {}
    stale = (
        ""
        if freshness.get("fresh", True)
        else f'<p class="reason">stale: {escape(str(freshness.get("error") or ""))}</p>'
    )
    reasons = "".join(
        f'<div class="because"><span class="label">{escape(label)}</span><span class="prose">{escape(str(entry[key]))}</span></div>'
        for key, label in (("selected_why", "why"), ("rejected_alternatives", "rejected"))
        if entry.get(key)
    )
    rows = [
        '<div class="call"><span class="label">decision</span><div>'
        f'<span class="prose">{escape(str(entry.get("selected_step") or ""))}</span>{stale}'
        + (
            f'<details class="reasons"><summary>why · rejected alternatives</summary>{reasons}</details>'
            if reasons
            else ""
        )
        + "</div></div>"
    ]
    if entry.get("next_safe_step"):
        rows.append(
            f'<div class="call"><span class="label">next step</span><div>{_long(entry.get("next_safe_step"), chars=200)}</div></div>'
        )
    return "".join(rows)


#: The resume fields the "Observer's call" panel already shows, and so the resume tab leaves out.
CALL_FIELDS = {"selected_step", "selected_why", "rejected_alternatives", "next_safe_step", "recorded_at"}


def _dod_items_table(items: list[dict[str, Any]]) -> str:
    """Each addressable item with its state, weight and the evidence that met it."""
    rows = [
        [
            f'<span class="mono">{escape(str(item.get("id") or ""))}</span>',
            _chip(str(item.get("state") or ""), "ok" if item.get("state") == "met" else ""),
            escape(str(item.get("weight") if item.get("weight") is not None else "not weighed")),
            _long(item.get("text"), chars=200),
            _long(item.get("evidence")) if item.get("evidence") else "",
        ]
        for item in items
    ]
    return _rows(["item", "state", "weight", "", "evidence"], rows)


def _sprint_tabs(ref: str, value: dict[str, Any] | None, work: dict[str, Any]) -> str:
    """The secondary tabs: cards, Definition of Done, resume, issues."""
    if value is None:
        return '<div class="body"><p class="empty">no sprint was read, so there is nothing to show here.</p></div>'
    cards = _mapping(work.get("cards"))
    states = _mapping(cards.get("states"))
    listed = [
        (state, str(card))
        for state, refs in sorted(states.items())
        if isinstance(refs, list)
        for card in refs
    ]
    card_rows = (
        _rows(["", ""], [[_state_chip(state), _link(card)] for state, card in listed])
        if listed
        else '<p class="empty">the observer has cut no card for this sprint yet.</p>'
    )
    dod = str(value.get("definition_of_done") or "")
    items = _entries(_mapping(value.get("dod")).get("items"))
    dod_body = (
        (_dod_items_table(items) if items else "")
        + (f'<div class="doc">{markdown.render(dod)}</div>' if dod.strip() else "")
    ) or '<p class="empty">this sprint has no Definition of Done.</p>'
    resume = _mapping(value.get("resume"))
    # The call's fields are left out only when the call panel drew them, from the journal's decision.
    decision = _mapping(work.get("decision"))
    shown = CALL_FIELDS if isinstance(decision.get("entry"), dict) and decision["entry"] else set()
    kept = {name: text for name, text in resume.items() if name not in shown}
    resume_body = (
        _rows(
            ["", ""],
            [[escape(str(name).replace("_", " ")), _long(kept[name], chars=300)] for name in sorted(kept)],
        )
        if kept
        else '<p class="empty">the observer has recorded nothing beyond its decision and next step.</p>'
    )
    issues = [str(one) for one in value.get("issues") or [] if str(one)]
    issue_body = (
        '<ul class="refs">' + "".join(f'<li class="ref">{escape(one)}</li>' for one in issues) + "</ul>"
        if issues
        else '<p class="empty">this sprint declares no issue.</p>'
    )
    return _tabs(
        f"sprint-{ref}",
        [
            ("Cards", card_rows, len(listed) or None),
            ("Definition of done", dod_body, None),
            ("Owner decisions", _rows(["ID", "Scope / kind", "Value", "Owner quotation", "Recorded by"], [
                [escape(str(entry["id"])), escape(f"{entry['scope']} / {entry['kind']}"),
                 escape(json.dumps(entry["value"], ensure_ascii=False)),
                 f'<pre>{escape(entry["quotation"])}</pre>',
                 escape(json.dumps(entry["recorded_by"], ensure_ascii=False))]
                for entry in value.get("owner_decisions") or []
            ]) if value.get("owner_decisions") else '<p class="empty">No quoted standing owner decisions.</p>', len(value.get("owner_decisions") or []) or None),
            ("Last resume", resume_body, None),
            ("Issues", issue_body, len(issues) or None),
        ],
    )


def _sprint_fields(ref: str, value: dict[str, Any] | None, observer: dict[str, Any]) -> str:
    """What the sprint was opened on, and where its observer stands -- the facts no chip carries."""
    if value is None:
        return '<p class="empty">no sprint was read, so there is nothing to show here.</p>'
    return _rows(
        ["", ""],
        [
            ["projects", _listed(value.get("reservations"), "this sprint reserves no project")],
            ["repositories", _listed(value.get("repositories"), "this sprint names no repository")],
            ["observer", _observer_line(ref, observer)],
            *_executor_rows(value.get("executors") or {}),
        ],
    )


def _listed(values: Any, empty: str) -> str:
    items = [str(one) for one in (values or []) if str(one)]
    if not items:
        return f'<span class="empty">{escape(empty)}</span>'
    return ", ".join(escape(one) for one in items)


def _observer_line(ref: str, observer: dict[str, Any]) -> str:
    """The declared observer and, separately, whether one is up; the held head only if different."""
    declared = observer.get("declared") or {}
    launch = observer.get("launch") or {}
    state = str(launch.get("state") or "")
    words = LAUNCH_WORDS.get(state, "this launch state is one this page does not know")
    profile = declared.get("profile")
    if profile:
        said = f'<span class="ref">{escape(str(profile))}</span>'
    elif declared.get("state") == "malformed":
        said = '<span class="empty">this sprint carries an observer value that is not one of the known forms</span>'
    elif (declared.get("value") or {}).get("kind") == "none":
        said = '<span class="empty">this sprint declared no observer</span>'
    else:
        said = '<span class="empty">the row of this sprint carries no observer field</span>'
    record = launch.get("record") or {}
    held = str(record.get("head") or "")
    differs = (
        f'<div class="reason">the dispatcher holds <span class="ref">{escape(held)}</span>, not the declared head</div>'
        if held and profile and held != str(profile)
        else ""
    )
    heartbeat = (
        f'<div class="reason">heartbeat {escape(str(record.get("heartbeat_state")))}</div>'
        if record.get("heartbeat_state")
        else ""
    )
    run_id = str(record.get("run_id") or "")
    head_link = (
        f'<div class="reason"><a href="/sprints/{quote(ref, safe="")}/heads/{quote(run_id, safe="")}">'
        f"observer head {escape(run_id)}</a></div>"
        if run_id and ref
        else ""
    )
    return (
        _source_block(launch.get("source"), what="whether this sprint's observer is up")
        + f'{said} <span class="launch state state-{escape(state)}">{escape(words)}</span>'
        + f'<div class="reason">{escape(str(launch.get("reason") or "no reason was recorded"))}</div>'
        + differs
        + heartbeat
        + head_link
    )


def _executor_rows(executors: dict[str, Any]) -> list[list[str]]:
    """Both roles, always; an unpinned role reads as the observer's choice."""
    rows = []
    for role in ("worker", "reviewer"):
        entry = _mapping(executors.get(role))
        state = str(entry.get("state") or "")
        if state == "pinned":
            said = f"pinned to {escape(str(entry.get('profile') or ''))}"
        elif state == "malformed":
            said = "this row carries a pin that is not a profile"
        else:
            said = escape(EXECUTOR_CHOICE)
        rows.append([escape(role), said])
    return rows

