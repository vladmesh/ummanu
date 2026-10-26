"""Server-rendered presentation: card."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from html import escape
from typing import Any
from urllib.parse import quote

from ummanu.web import markdown
from ummanu.web.page_assets import _ACTIONS_SCRIPT, _TASK_SCRIPT
from ummanu.web.page_parts import (
    _chip,
    _comment_form,
    _effort,
    _entries,
    _external,
    _head,
    _head_effort,
    _head_model,
    _js,
    _link,
    _long,
    _move_form,
    _or_dash,
    _panel,
    _rows,
    _source_block,
    _state_cell,
    _state_chip,
    _tabs,
)
from ummanu.web.page_shell import _page
from ummanu.web.page_values import _mapping


def task(snapshot: dict[str, Any], *, runs: dict[str, Any], sessions: dict[str, Any] | None = None) -> str:
    """The card page: task text first, state chips, events, head output and result, each drawn once."""
    ref = str(snapshot.get("ref") or "")
    card = snapshot.get("card") or {}
    value = card.get("value") or {}
    project = snapshot.get("project") or {}
    events = snapshot.get("events") or {}
    agents = snapshot.get("agents") or {}
    chips = [_state_chip(value.get("state"))] if value else []
    if project.get("id") or value.get("project"):
        chips.append(_chip(str(project.get("id") or value.get("project"))))
    routing = _mapping(value.get("routing"))
    kind = " · ".join(str(part) for part in (value.get("type"), routing.get("complexity")) if part)
    if kind:
        chips.append(_chip(kind))
    handed = value.get("waiting_owner") if isinstance(value.get("waiting_owner"), dict) else None
    if handed:
        chips.append(
            f'<span title="{escape(str(handed.get("reason") or ""))}">{_chip("waiting for the owner", "warn")}</span>'
        )
    event_items = list(events.get("items") or [])
    sprint_ref = str(value.get("sprint") or "")
    crumbs: tuple[tuple[str, str], ...] = ((ref, ""),)
    if sprint_ref:
        crumbs = ((sprint_ref, f"/sprints/{quote(sprint_ref)}"), (ref, ""))
    title = str(value.get("title") or "")
    body = "\n".join(
        [
            '<div class="hero">',
            f"<h1>{escape(title)}</h1>" if title else f"<h1>{escape(ref)}</h1>",
            f'<span class="age" style="margin-left:auto">read at {escape(str(snapshot.get("observed_at") or "an unknown time"))}</span>',
            f'<div class="chips">{"".join(chips)}</div>',
            "</div>",
            _source_block(card.get("source"), what="what this card is"),
            '<div class="grid">',
            '<div class="col">',
            '<section class="panel">',
            _tabs(
                f"card-{ref}",
                [
                    ("Task", _task_text(card.get("value")), None),
                    ("Work", _work(snapshot.get("work") or {}), None),
                    (
                        "Timeline",
                        _source_block(events.get("source"), what="this card's history")
                        + _timeline(event_items),
                        None,
                    ),
                    (
                        "Raw events",
                        _events(event_items) + '<p id="events-notice"></p>',
                        len(event_items) or None,
                    ),
                ],
            ),
            "</section>",
            "</div>",
            '<div class="col">',
            _panel("Heads", _card_heads_panel(ref, snapshot.get("heads") or {}, agents)),
            *_card_blocks(value, sessions),
            _panel(
                "Card",
                _card(card.get("value"), project) + _attempt(snapshot.get("attempt") or {}),
            ),
            _panel(
                # A handed card's comment is written as the owner's and reaches the PO
                # (`card_ops._comment_role`).
                "Answer the PO as the owner" if handed else "Tell the head working this card",
                _comment_form(
                    f"/api/tasks/{quote(ref)}/comment",
                    f"card.{ref}",
                    "Your answer, written as the owner; the PO completes the card"
                    if handed
                    else "A comment the head reads on its next turn",
                )
                + f'<details class="more-actions"><summary>Move this card…</summary>{_move_form(ref)}</details>',
            ),
            _panel("Product runs", _runs(ref, runs), open_=False),
            "</div></div>",
        ]
    )
    cursor = escape(str(events.get("next_cursor") or ""))
    script = _TASK_SCRIPT.replace("__REF__", _js(ref)).replace("__CURSOR__", _js(cursor))
    return _page(
        f"Card {ref}",
        body,
        script=script + _ACTIONS_SCRIPT,
        nav="sprints" if sprint_ref else "dashboard",
        crumbs=crumbs,
    )


def _task_text(card: dict[str, Any] | None) -> str:
    """The task as the observer wrote it: the card's description, rendered from its Markdown."""
    if card is None:
        return '<p class="empty">no card was read, so there is no task to show.</p>'
    text = str(card.get("description") or "")
    if not text.strip():
        return '<p class="empty">this card carries no description beyond its title.</p>'
    return f'<article class="doc task-text">{markdown.render(text)}</article>'


def _card_heads_panel(ref: str, heads: dict[str, Any], agents: dict[str, Any]) -> str:
    """Every head run the card recorded, by role: the latest large, earlier ones beneath.

    `agents` (the dispatcher's process per run id, or role) decides the pulse; a run it no longer
    holds is drawn from its own row.
    """
    runs = [item for item in heads.get("items") or [] if isinstance(item, dict)]
    live = {
        str(item.get("run_id") or item.get("role") or ""): item
        for item in agents.get("items") or []
        if isinstance(item, dict)
    }
    by_role: dict[str, list[dict[str, Any]]] = {}
    for item in runs:
        by_role.setdefault(str(item.get("role") or "head"), []).append(item)
    known = {str(item.get("run_id") or "") for item in runs}
    for key, item in live.items():
        # A process held under no run the card recorded is drawn from what it says.
        if key not in known:
            by_role.setdefault(str(item.get("role") or "head"), []).append(item)
    parts = [
        _source_block(heads.get("source"), what="which heads this card has run"),
        _source_block(agents.get("source"), what="which agents are working this card"),
    ]
    if not by_role:
        if (heads.get("source") or {}).get("state") == "available":
            parts.append('<p class="empty">this card has run no head yet.</p>')
        return "\n".join(part for part in parts if part)
    drawn = []
    for role in sorted(by_role, key=lambda role: ROLE_ORDER.get(role, len(ROLE_ORDER))):
        *earlier, latest = by_role[role]
        process = live.get(str(latest.get("run_id") or ""))
        if process is None and latest.get("current"):
            process = live.get(role)
        # The process decides the pulse; the run's own reason (legacy runtime, unreadable lock) stays.
        row = {**latest, "state": process.get("state") if process else latest.get("state")}
        if process and process.get("reason"):
            row["process_reason"] = process["reason"]
        drawn.append(_head(role, row) + _head_facts(ref, row))
        if earlier:
            drawn.append(
                '<ul class="head-runs">'
                + "".join(f"<li>{_head_run(ref, run)}</li>" for run in reversed(earlier))
                + "</ul>"
            )
    parts.append('<div class="heads stacked">' + "".join(drawn) + "</div>")
    return "\n".join(part for part in parts if part)


#: Roles in the order a card page lists them; a role not here comes after.
ROLE_ORDER = {"worker": 0, "reviewer": 1}


def _head_facts(ref: str, item: dict[str, Any]) -> str:
    """The line under a head: exact model id, profile, run, and why its state is what it is."""
    facts = []
    if item.get("resolved_model"):
        facts.append(f'<span class="ref">{escape(str(item["resolved_model"]))}</span>')
    if item.get("head"):
        facts.append(f'profile <span class="ref">{escape(str(item["head"]))}</span>')
    if item.get("run_id"):
        facts.append(_head_run_ref(ref, item))
    reasons = [str(item.get(key) or "") for key in ("process_reason", "reason")]
    return (
        f'<div class="head-facts">{" · ".join(facts)}'
        + "".join(f'<div class="reason">{escape(reason)}</div>' for reason in reasons if reason)
        + "</div>"
    )


def _head_run(ref: str, run: dict[str, Any]) -> str:
    """An earlier run of a role, one line: the model it ran on, its effort, the run and its state."""
    name, said = _head_model(run)
    attempt = run.get("attempt")
    parts = [
        f'<span title="{escape(said)}">{escape(name)}</span>',
        f'<span class="effort-cell">{_effort(_head_effort(run))}</span>',
        _head_run_ref(ref, run),
    ]
    if isinstance(attempt, int):
        parts.insert(0, f'<span class="age">attempt {attempt}</span>')
    state = str(run.get("state") or "")
    if state and state != "unknown":
        parts.append(f'<span class="age">{escape(state)}</span>')
    return " · ".join(parts)


def _head_run_ref(ref: str, run: dict[str, Any]) -> str:
    """The run id, as a link to its journal when a local-pty supervisor kept one."""
    run_id = str(run.get("run_id") or "")
    if run.get("local_pty"):
        return f'<a class="ref" href="{escape(_head_href(ref, run_id))}">{escape(run_id)}</a>'
    return f'<span class="ref">{escape(run_id)}</span>'


def _head_href(ref: str, run_id: str) -> str:
    return f"/tasks/{quote(ref, safe='')}/heads/{quote(run_id, safe='')}"


def head_view(document: dict[str, Any]) -> str:
    """One local-pty head's journal tail, read-only; each section is drawn under `_shown`."""
    ref = str(document.get("ref") or "")
    run_id = str(document.get("run_id") or "")
    head = _mapping(document.get("head"))
    journal = _mapping(document.get("journal"))
    owner = "sprints" if document.get("task_kind") == "sprint" else "tasks"
    back = f"/{owner}/{quote(ref, safe='')}"
    tail = journal.get("tail")
    body = "\n".join(
        [
            _shown(lambda: _head_header(document, head, run_id)),
            _panel(
                "Journal",
                _shown(lambda: _head_journal(journal)),
                count=(len(tail) if isinstance(tail, list) else 0) or None,
            ),
            f'<p><a href="{escape(back)}">back to {escape(ref or "the card")}</a></p>',
        ]
    )
    nav = "sprints" if owner == "sprints" else "dashboard"
    return _page(f"Head {run_id}", body, nav=nav, crumbs=((ref, back), (run_id, "")))


def _shown(draw: Callable[[], str]) -> str:
    """One section of the head view, or the plain statement that it could not be drawn."""
    try:
        return draw()
    except Exception as exc:  # noqa: BLE001 - a head's run directory is untrusted input
        return f'<p class="unavailable"><b>this section could not be shown ({escape(type(exc).__name__)})</b></p>'


def _head_header(document: dict[str, Any], head: dict[str, Any], run_id: str) -> str:
    chips = [_chip(str(head.get("role") or "head"))]
    if head.get("runtime"):
        chips.append(_chip(str(head.get("runtime"))))
    return "\n".join(
        [
            '<div class="hero">',
            f"<h1>{escape(run_id or 'head')}</h1>",
            f'<div class="chips">{"".join(chips)}</div>',
            f'<span class="age" style="margin-left:auto">read at {escape(str(document.get("observed_at") or "an unknown time"))}</span>',
            f'<div class="title">{_state_cell(str(head.get("state") or "unknown"), str(head.get("reason") or ""))}</div>',
            "</div>",
            f'<p class="empty">{escape(str(document.get("read_only") or ""))}</p>',
        ]
    )


def _head_journal(section: dict[str, Any]) -> str:
    """The journal's last records, through the layer's whitelist, and what the read left out."""
    parts = []
    if section.get("state") == "not_applicable":
        return f'<p class="empty">{escape(str(section.get("reason") or ""))}</p>'
    if not section.get("answered"):
        parts.append(
            '<p class="unavailable"><b>the journal is not answering:</b> '
            f"{escape(str(section.get('reason') or 'no reason was recorded'))}</p>"
        )
    elif section.get("reason"):
        parts.append(
            f'<p class="unavailable"><b>the journal answered in part:</b> {escape(str(section["reason"]))}</p>'
        )
    tail = [record for record in section.get("tail") or [] if isinstance(record, dict)]
    rows = []
    for record in tail:
        at = record.get("at")
        when = (
            datetime.fromtimestamp(at, UTC).strftime("%Y-%m-%d %H:%M:%S")
            if isinstance(at, (int, float)) and not isinstance(at, bool)
            else ""
        )
        said = " ".join(str(record.get(key)) for key in ("reason", "subject") if record.get(key))
        rows.append(
            [
                _or_dash(record.get("seq")),
                f"<code>{escape(str(record.get('kind') or ''))}</code>",
                _or_dash(when),
                _or_dash(record.get("turn")),
                _or_dash(record.get("bytes")),
                _or_dash(record.get("output_bytes")),
                _or_dash(record.get("folded_windows")),
                _or_dash(said),
            ]
        )
    if rows:
        parts.append(
            _rows(
                ["seq", "kind", "at (UTC)", "turn", "bytes", "output bytes", "folded windows", "reason"], rows
            )
        )
    elif section.get("answered"):
        parts.append('<p class="empty">the journal holds no record.</p>')
    return "\n".join(parts)


def _card(card: dict[str, Any] | None, project: dict[str, Any]) -> str:
    """What the chips over the title do not say: who claimed it, where it works, when it moved."""
    if card is None:
        return '<p class="empty">no card was read, so there is nothing to show here.</p>'
    registered = "registered" if project.get("registered") else "not registered on this installation"
    rows = [
        [
            "project",
            f'{_or_dash(project.get("id") or card.get("project"))} <span class="age">({escape(registered)})</span>',
        ],
        ["claimed by", _or_dash(card.get("claimed_by"))],
        ["created", _or_dash(card.get("created_at"))],
        ["updated", _or_dash(card.get("updated_at"))],
    ]
    if card.get("blocked_by"):
        rows.append(["blocked by", _or_dash(card.get("blocked_by"))])
    if card.get("touches_production"):
        rows.append(["touches production", _or_dash(card.get("touches_production"))])
    handed = card.get("waiting_owner") if isinstance(card.get("waiting_owner"), dict) else None
    if handed:
        said = (
            f'{_or_dash(handed.get("reason"))} <span class="age">(by {_or_dash(handed.get("by"))} '
            f"at {_or_dash(handed.get('since'))})</span>"
        )
        rows.append(["handed to the owner", said])
    return _rows(["", ""], rows)


#: The prefix a wait's PO return address carries (`board/wait_card.PO_SESSION_PREFIX`).
PO_ADDRESS_PREFIX = "po-session:"


#: The prefix a wait's card return address carries (`board/wait_card.CARD_PREFIX`).
CARD_ADDRESS_PREFIX = "card:"


PO_TITLES_UNAVAILABLE = "PO session titles are unavailable"


def po_sessions_named(snapshot: dict[str, Any]) -> list[str]:
    """Every PO session a card page links to: origin, successor, returns, the wait's addresses."""
    value = _mapping(_mapping(snapshot.get("card")).get("value"))
    origin = _mapping(value.get("origin"))
    named = [origin.get("po_session"), origin.get("current_session")]
    named += [row.get("session") for row in _entries(origin.get("returns"))]
    for address in _mapping(_mapping(value.get("wait")).get("po_sessions")).values():
        named += [_mapping(address).get("addressed"), _mapping(address).get("received_by")]
    return list(dict.fromkeys(str(item) for item in named if isinstance(item, str) and item))


def _session_titles(sessions: dict[str, Any] | None) -> tuple[dict[str, Any] | None, str]:
    """The sessions the PO store answered for, or None with the reason when it did not."""
    if sessions is None:
        return None, ""
    if not sessions.get("available"):
        return None, str(sessions.get("reason") or "no reason was recorded")
    return _mapping(_mapping(sessions.get("document")).get("sessions")), ""


def _po_session_link(session_id: Any, known: dict[str, Any] | None) -> str:
    """A PO session as its title linked to its page; its short id when untitled, gone or unknown."""
    text = str(session_id or "")
    if not text:
        return "—"
    short = f'<span class="id">{escape(text[:8])}</span>'
    found = _mapping((known or {}).get(text)) if known is not None else None
    title = str((found or {}).get("title") or "")
    label = f"{escape(title)} {short}" if title else short
    gone = ' <span class="age">(no such session)</span>' if known is not None and text not in known else ""
    return f'<a href="/po/sessions/{quote(text)}">{label}</a>{gone}'


def _origin_panel(origin: dict[str, Any], sessions: dict[str, Any] | None) -> str:
    """Who delegated this card, where its result goes now, and what was returned to whom."""
    known, refused = _session_titles(sessions)
    origin_session = str(origin.get("po_session") or "")
    current = str(origin.get("current_session") or "")
    parts = [f"<p>Delegated by {_po_session_link(origin_session, known)}</p>"]
    if refused:
        parts.append(f'<p class="unavailable"><b>{escape(PO_TITLES_UNAVAILABLE)}:</b> {escape(refused)}</p>')
    if origin.get("request_id"):
        parts.append(f'<p class="age">request <code>{escape(str(origin["request_id"]))}</code></p>')
    if current and current != origin_session:
        parts.append(
            f"<p>The origin session was succeeded: the result goes to {_po_session_link(current, known)}</p>"
        )
    returns = _entries(origin.get("returns"))
    if not returns:
        parts.append('<p class="empty">nothing has been returned to the PO yet.</p>')
    else:
        rows = [
            [
                _state_chip(row.get("state")),
                _or_dash(row.get("status") or "pending"),
                _or_dash(row.get("delivered_at")),
                _po_session_link(row.get("session"), known) if row.get("session") else "—",
            ]
            for row in returns
        ]
        parts.append(_rows(["returned", "delivery", "delivered at", "received by"], rows))
    return "\n".join(parts)


#: The tone a wait's state reads in.
WAIT_TONES = {
    "waiting": "accent",
    "result_ready": "accent",
    "delivered": "ok",
    "target_reached": "ok",
    "deadline_passed": "bad",
    "source_unreachable": "bad",
    "cancelled": "warn",
    "malformed": "bad",
}


def _wait_target(target: dict[str, Any]) -> str:
    """A run as a link to its page, a card as a link with the states awaited, a time as the time."""
    kind = str(target.get("kind") or "")
    if kind == "github_run":
        url = target.get("link") or target.get("html_url") or target.get("url")
        label = f"GitHub run {target.get('repo') or '?'}#{target.get('run_id') or '?'}"
        return _external(url, label) if url else escape(label)
    if kind == "card":
        states = (
            " or ".join(str(state) for state in target.get("states") or [] if state) or "an unrecorded state"
        )
        ref = str(target.get("ref") or "")
        return f"{_link(ref) if ref else '—'} reaching {escape(states)}"
    if kind == "time":
        return f"the time {_or_dash(target.get('at'))}"
    return '<span class="empty">an unreadable target</span>'


def _wait_address(address: str, status: str, wait: dict[str, Any], known: dict[str, Any] | None) -> list[str]:
    if address.startswith(PO_ADDRESS_PREFIX):
        po = _mapping(_mapping(wait.get("po_sessions")).get(address))
        addressed = po.get("addressed") or address[len(PO_ADDRESS_PREFIX) :]
        where = f"PO session {_po_session_link(addressed, known)}"
        received = po.get("received_by")
        if received and received != addressed:
            where += f" (taken by its successor {_po_session_link(received, known)})"
    elif address.startswith(CARD_ADDRESS_PREFIX):
        where = f"card {_link(address[len(CARD_ADDRESS_PREFIX) :])}"
    else:
        where = escape(address)
    return [where, _chip(status.replace("_", " "), "ok" if status in ("delivered", "accepted") else "")]


def _wait_panel(wait: dict[str, Any], sessions: dict[str, Any] | None) -> str:
    """A wait: its target, since and until when, its state, and where its result went."""
    state = str(wait.get("state") or "unknown")
    if state == "malformed" or not isinstance(wait.get("target"), dict):
        reason = str(wait.get("reason") or "the card carries no well-formed wait spec")
        return f'{_chip(state.replace("_", " "), WAIT_TONES.get(state, ""))} <span class="reason">{escape(reason)}</span>'
    known, refused = _session_titles(sessions)
    rows = [
        ["target", _wait_target(wait["target"])],
        ["waiting since", _or_dash(wait.get("waiting_since"))],
        ["deadline", _or_dash(wait.get("deadline"))],
        ["state", _chip(state.replace("_", " "), WAIT_TONES.get(state, ""))],
    ]
    result = _mapping(wait.get("result"))
    if result:
        summary = escape(str(result.get("summary") or result.get("outcome") or "no summary recorded"))
        evidence = result.get("evidence")
        rows.append(["result", summary + (f" · {_external(evidence, 'evidence')}" if evidence else "")])
    observed = _mapping(wait.get("last_observation"))
    if observed.get("text"):
        rows.append(
            [
                "last seen",
                f'{escape(str(observed["text"]))} <span class="age">{_or_dash(observed.get("at"))}</span>',
            ]
        )
    error = _mapping(wait.get("last_error"))
    if error.get("text"):
        rows.append(["last error", escape(str(error["text"]))])
    parts = [_rows(["", ""], rows)]
    deliveries = _mapping(wait.get("deliveries"))
    addresses = [str(item) for item in wait.get("return_to") or [] if item] or list(deliveries)
    if addresses:
        parts.append(
            _rows(
                ["returns to", "delivery"],
                [
                    _wait_address(address, str(deliveries.get(address) or "pending"), wait, known)
                    for address in addresses
                ],
            )
        )
    if refused:
        parts.append(f'<p class="unavailable"><b>{escape(PO_TITLES_UNAVAILABLE)}:</b> {escape(refused)}</p>')
    return "\n".join(parts)


def _e2e_run_rows(runs: list[dict[str, Any]]) -> list[list[str]]:
    rows = []
    for run in runs:
        result = _mapping(run.get("result"))
        said = str(result.get("summary") or "")
        state = str(run.get("state") or "unknown")
        rows.append(
            [
                f"<code>{escape(str(run.get('sha') or '')[:12]) or '—'}</code>",
                _external(run.get("run"), "run")
                if run.get("run")
                else '<span class="empty">not identified</span>',
                escape(state.replace("_", " "))
                + (f'<div class="reason">{escape(said)}</div>' if said else ""),
                _link(str(run["wait_card"])) if run.get("wait_card") else "—",
            ]
        )
    return rows


def _e2e_panel(e2e: dict[str, Any]) -> str:
    """Each e2e run of the card with its wait card, budget spend, and after-merge state."""
    parts = []
    spent = (
        f"{e2e.get('runs_dispatched') if e2e.get('runs_dispatched') is not None else '?'} run(s) dispatched"
    )
    if e2e.get("budget"):
        sprint = str(e2e["budget"])
        spent += f', charged to <a href="/sprints/{quote(sprint)}">{escape(sprint)}</a>'
    elif e2e.get("run_cap") is not None:
        spent += f" of this card's cap of {escape(str(e2e['run_cap']))}"
    parts.append(f"<p>{spent}</p>")
    if e2e.get("mark"):
        decision = str(e2e.get("waiting_on") or "")
        parts.append(
            f"<p>{_chip('budget spent', 'warn')} {escape(str(e2e['mark']))}"
            + (f" · {_link(decision)}" if decision else "")
            + "</p>"
        )
    runs = _entries(e2e.get("runs"))
    if runs:
        parts.append(_rows(["sha", "run", "state / result", "wait card"], _e2e_run_rows(runs)))
    elif not e2e.get("placement"):
        parts.append('<p class="empty">no e2e run has been dispatched for this card.</p>')
    if e2e.get("placement") == "after_merge":
        rows = [["after merge", _or_dash(e2e.get("state") or "no mark on this card")]]
        if e2e.get("merge_sha"):
            rows.append(["merge sha", f"<code>{escape(str(e2e['merge_sha'])[:12])}</code>"])
        if e2e.get("run"):
            rows.append(["run", _external(e2e["run"])])
        if e2e.get("carrier"):
            rows.append(["carried by", _link(str(e2e["carrier"]))])
        if e2e.get("hotfix"):
            rows.append(["hotfix", _link(str(e2e["hotfix"]))])
        if e2e.get("decision"):
            rows.append(["decision", _link(str(e2e["decision"]))])
        parts.append(_rows(["", ""], rows))
        carried = _entries(e2e.get("after_merge_runs"))
        if carried:
            parts.append(_rows(["sha", "run", "state / result", "wait card"], _e2e_run_rows(carried)))
    return "\n".join(parts)


def _card_blocks(value: dict[str, Any], sessions: dict[str, Any] | None) -> list[str]:
    """The delegation, wait and e2e panels of a card page; none for a card that carries none."""
    panels = []
    if isinstance(value.get("origin"), dict):
        panels.append(_panel("Delegation", _origin_panel(value["origin"], sessions)))
    if isinstance(value.get("wait"), dict):
        panels.append(_panel("Wait", _wait_panel(value["wait"], sessions)))
    if isinstance(value.get("e2e"), dict):
        panels.append(_panel("E2E", _e2e_panel(value["e2e"])))
    return panels


def _attempt(attempt: dict[str, Any]) -> str:
    value = attempt.get("value")
    parts = [_source_block(attempt.get("source"), what="what the dispatcher holds for this card")]
    if value is None:
        if (attempt.get("source") or {}).get("state") == "available":
            parts.append('<p class="empty">the dispatcher holds no attempt for this card.</p>')
        return "\n".join(part for part in parts if part)
    paused = value.get("paused") or {}
    parts.append(
        _rows(
            ["", ""],
            [
                ["dispatcher record", _or_dash(value.get("state"))],
                [
                    "attempt",
                    f"{_or_dash(value.get('attempt_id'))} (round {_or_dash(value.get('attempt_round'))})",
                ],
                ["gate", _or_dash(value.get("gate_state"))],
                ["workspace", _or_dash(value.get("workspace"))],
                [
                    "paused",
                    f"worker {'yes' if paused.get('worker') else 'no'}, reviewer {'yes' if paused.get('reviewer') else 'no'}",
                ],
            ],
        )
    )
    return "\n".join(part for part in parts if part)


def _runs(ref: str, runs: dict[str, Any]) -> str:
    if not runs.get("available"):
        reason = escape(str(runs.get("reason") or "no reason was recorded"))
        return f'<p class="unavailable"><b>could not find out this card\'s product runs:</b> {reason}</p>'
    items = list(runs.get("items") or [])
    if not items:
        return '<p class="empty">this card has no product run.</p>'
    rows = []
    for item in items:
        # `state` (running, finished, failed, unreadable, unknown) and `ended` are both shown; an
        # open `unknown` run must not look like a running one.
        run = item.get("run") or {}
        state = item.get("state") or {}
        value = str(state.get("value") or "unknown")
        over = "over" if state.get("ended") else "open"
        rows.append(
            [
                f"<code>{escape(str(run.get('run_id') or ''))}</code>",
                escape(str(run.get("role") or "")),
                _or_dash(run.get("profile")),
                escape(str(run.get("phase") or "")),
                _state_cell(value, str(state.get("reason") or "no reason was recorded"))
                + f'<div class="age">({escape(over)})</div>',
                _outcome_cell(state),
            ]
        )
    return _rows(["run", "role", "profile", "phase", "state", "outcome"], rows)


def _outcome_cell(state: dict[str, Any]) -> str:
    """What this run produced: its verdict (`state.result.verdict`, never re-derived), result, exit.

    An absent result and an unreadable one say different words; an open run has produced nothing.
    """
    result = state.get("result") or {}
    exit_status = state.get("exit") or {}
    parts: list[str] = []
    verdict = result.get("verdict")
    if verdict:
        parts.append(f'<div><b>verdict</b> <span class="state">{escape(str(verdict))}</span></div>')
    if result.get("present"):
        summary = _summary_of(result.get("value"))
        parts.append(f"<div>the head published a result{escape(summary)}</div>")
    elif result.get("reason"):
        parts.append(f'<div class="reason">{escape(str(result["reason"]))}</div>')
    elif state.get("ended"):
        parts.append('<div class="reason">the head published no result</div>')
    else:
        parts.append('<div class="empty">this run has produced nothing yet.</div>')
    if exit_status.get("code") is not None:
        parts.append(f'<div class="age">exit status {escape(str(exit_status["code"]))}</div>')
    elif exit_status.get("signal") is not None:
        parts.append(f'<div class="age">ended by signal {escape(str(exit_status["signal"]))}</div>')
    return "".join(parts)


def _summary_of(value: Any) -> str:
    """The one line a head's own result offers about itself, when it offers one."""
    if not isinstance(value, dict):
        return ""
    for name in ("summary", "status"):
        text = value.get(name)
        if isinstance(text, str) and text.strip():
            return ": " + text.strip()
    return ""


def _work(work: dict[str, Any]) -> str:
    parts = []
    for slot, title in (
        ("worker_report", "worker report"),
        ("review_verdict", "reviewer verdict"),
        ("decision", "observer decision"),
    ):
        entry = work.get(slot)
        if not isinstance(entry, dict):
            parts.append(f'<h3>{escape(title)}</h3><p class="empty">this round has produced none.</p>')
            continue
        classification = entry.get("classification")
        marked = f' <span class="age">({escape(str(classification))})</span>' if classification else ""
        parts.append(
            f"<h3>{escape(title)}</h3>"
            f'<p><span class="state">{escape(str(entry.get("marker") or ""))}</span>{marked} '
            f'<span class="age">at {_or_dash(entry.get("at"))}</span></p>'
            f"{_long(entry.get('body'), chars=200)}"
        )
    outcome = work.get("outcome")
    if isinstance(outcome, dict):
        terminal = "the card is Done" if outcome.get("terminal") else "the card is not finished"
        parts.append(
            f'<h3>result</h3><p><span class="state">{escape(str(outcome.get("kind")))}:'
            f"{escape(str(outcome.get('value')))}</span> at {_or_dash(outcome.get('at'))} "
            f'<span class="age">({escape(terminal)})</span></p>'
        )
    else:
        parts.append('<h3>result</h3><p class="empty">this card has produced no result yet.</p>')
    return "\n".join(parts)


def _events(items: list[dict[str, Any]]) -> str:
    if not items:
        return '<ol class="events" id="events"></ol><p class="empty" id="events-empty">no event has been recorded for this card.</p>'
    return '<ol class="events" id="events">' + "".join(_event(item) for item in items) + "</ol>"


def _event(item: dict[str, Any]) -> str:
    detail = item.get("reason") or item.get("outcome") or ""
    return (
        f'<li data-event-id="{escape(str(item.get("event_id") or ""))}">'
        f"<time>{escape(str(item.get('occurred_at') or ''))}</time>"
        f"<b>{escape(str(item.get('kind') or ''))}</b> {escape(str(detail))}</li>"
    )


#: Event kinds whose `data.body` is a record about the card, shown under the transition it led to.
RECORD_KINDS = {
    "card.reported": "worker report",
    "card.verdict": "reviewer verdict",
    "card.decided": "observer decision",
}


def _timeline(items: list[dict[str, Any]]) -> str:
    """Every card transition and head change, oldest first; a transition opens on the records since
    the previous transition."""
    ordered = sorted(items, key=lambda item: str(item.get("occurred_at") or ""))
    steps: list[str] = []
    pending: list[dict[str, Any]] = []
    for item in ordered:
        if item.get("kind") == HEAD_CHANGED_KIND:
            steps.append(_head_change_step(item))
            continue
        if item.get("kind") in RECORD_KINDS and (item.get("data") or {}).get("body"):
            pending.append(item)
        if isinstance(item.get("transition"), dict) and item["transition"]:
            steps.append(_transition_step(item, pending))
            pending = []
    if not steps:
        return '<p class="empty">no transition has been recorded for this card yet.</p>'
    return "\n".join(['<ol class="timeline">', *steps, "</ol>"])


def _transition_step(event: dict[str, Any], records: list[dict[str, Any]]) -> str:
    transition = event["transition"]
    when = str(event.get("occurred_at") or "")
    clock = when[11:19] if len(when) >= 19 else when
    actor = _mapping(event.get("actor"))
    reason = str(event.get("reason") or "")
    short = reason if len(reason) <= 120 else reason[:117].rstrip() + "…"
    detail = [
        f'<p class="why">{escape(when)} · {escape(str(actor.get("role") or ""))} {escape(str(actor.get("id") or ""))} · <span class="mono">{escape(str(event.get("kind") or ""))}</span></p>',
        (f"<div>{_long(reason, chars=300)}</div>" if reason else ""),
    ]
    for record in records:
        data = record.get("data") or {}
        who = _mapping(record.get("actor"))
        stamp = str(record.get("occurred_at") or "")
        marker = str(data.get("marker") or data.get("decision") or data.get("status") or "")
        detail.append(
            '<div class="record">'
            f'<div class="who"><b>{escape(RECORD_KINDS[str(record.get("kind"))])}</b>'
            f"{' · ' + escape(marker) if marker else ''} · {escape(stamp[11:19] if len(stamp) >= 19 else stamp)}"
            f" · {escape(str(who.get('id') or who.get('role') or ''))}</div>"
            f"{_long(data.get('body'), chars=200)}"
            "</div>"
        )
    return (
        f'<li><details><summary><time title="{escape(when)}">{escape(clock)}</time>'
        f'<span>{_state_chip(transition.get("source"))}<span class="arrow">→</span>{_state_chip(transition.get("target"))}'
        f' <span class="why">{escape(short)}</span></span></summary>'
        f'<div class="detail">{"".join(part for part in detail if part)}</div></details></li>'
    )


#: The `head_changed` record of a card head's confirmed stop and its launched successor.
HEAD_CHANGED_KIND = "head_changed"

#: How each recorded cause of a head change reads on the timeline.
HEAD_CHANGE_CAUSES = {
    "stall": "confirmed stall",
    "head_exit": "head exited",
    "provider_failover": "provider failover",
    "operator_stop": "manual stop",
}


def _head_change_step(item: dict[str, Any]) -> str:
    """One head change: role, stop or replacement, cause, time and profile before -> after."""
    data = _mapping(item.get("data"))
    before = _mapping(data.get("before"))
    after = _mapping(data.get("after"))
    role = str(data.get("role") or "head")
    replaced = data.get("change") == "replaced"
    cause = str(data.get("cause") or "")
    when = str(item.get("occurred_at") or "")
    clock = when[11:19] if len(when) >= 19 else when
    successor = str(after.get("profile") or "") if replaced else "no successor"
    if replaced and after.get("profile") == before.get("profile"):
        successor += " (same profile, new run)"
    profiles = f"{before.get('profile') or 'unknown'} → {successor}"
    detail = [
        f'<p class="why">{escape(when)} · {escape(HEAD_CHANGE_CAUSES.get(cause, cause))} · <span class="mono">{escape(str(item.get("kind") or ""))}</span></p>',
        f"<div>{_long(data.get('reason'), chars=300)}</div>",
        f'<p class="why">run <span class="mono">{escape(str(before.get("run_id") or ""))}</span>'
        + (f' → <span class="mono">{escape(str(after.get("run_id") or ""))}</span>' if replaced else "")
        + "</p>",
    ]
    requested = str(data.get("requested") or "")
    if requested and requested != before.get("profile"):
        detail.append(f'<p class="why">requested profile {escape(requested)}</p>')
    return (
        f'<li><details><summary><time title="{escape(when)}">{escape(clock)}</time>'
        f'<span>{_chip(f"{role} {"replaced" if replaced else "stopped"}", "warn")}'
        f' <span class="why">{escape(HEAD_CHANGE_CAUSES.get(cause, cause))}: {escape(profiles)}</span></span></summary>'
        f'<div class="detail">{"".join(detail)}</div></details></li>'
    )
