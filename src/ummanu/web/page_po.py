"""Server-rendered presentation: po."""

from __future__ import annotations

from datetime import UTC, datetime
from html import escape
from typing import Any
from urllib.parse import quote

from ummanu.web import markdown
from ummanu.web.page_assets import _PO_FORM_SCRIPT, _PO_SESSION_SCRIPT
from ummanu.web.page_parts import (
    EFFORT_DEFAULT,
    PO_EFFORT_UNSET,
    _age,
    _chip,
    _effort,
    _entries,
    _head,
    _head_model,
    _js,
    _link,
    _model_name,
    _or_dash,
    _panel,
    _rows,
    _selected,
    _state_chip,
)
from ummanu.web.page_shell import _page
from ummanu.web.page_values import _mapping

PO_NOTICE = (
    "the PO head runs Claude or Codex with full permissions on this host: what is sent here is carried "
    "out as if it were typed into a shell"
)


PO_SEND_HINT = "Enter to send, Shift+Enter for a new line"


#: The words a turn's state is shown in, and their tone.
TURN_MARKS: dict[str, tuple[str, str]] = {
    "running": ("running", "accent"),
    "completed": ("completed", "ok"),
    "failed": ("failed", "bad"),
    "interrupted": ("interrupted", "warn"),
}


def _po_refusal(refusal: dict[str, Any] | None, refused: str = "send") -> str:
    if not refusal:
        return ""
    code = str(refusal.get("code") or "")
    message = str(refusal.get("message") or "")
    if refused == "title":
        # A rename carries no request id: it sets a value, so saving the same title again is safe.
        return (
            f'<p class="refused"><b>title not saved ({escape(code)}).</b> '
            f'<span class="reason">{escape(message)}</span></p>'
        )
    if (refusal.get("data") or {}).get("reason") == "outcome_unknown":
        return (
            '<p class="refused"><b>no answer from the PO service: it may have done this.</b> '
            "Sending the same form again is safe; it carries the same request id. "
            f'<span class="reason">{escape(message)}</span></p>'
        )
    if code == "owner_conflict" and refused == "close":
        return (
            '<p class="refused"><b>not closed: a turn is still running in this session.</b> '
            "Wait for its answer or stop it, then close; nothing was written. "
            f'<span class="reason">{escape(message)}</span></p>'
        )
    if code == "owner_conflict":
        return (
            '<p class="refused"><b>not sent: a turn is still running in this session.</b> '
            "Wait for its answer or stop it, then send again; nothing was written. "
            f'<span class="reason">{escape(message)}</span></p>'
        )
    if code == "session_closed":
        return (
            '<p class="refused"><b>not sent: this session is closed.</b> '
            "Open a new session to continue; nothing was written. "
            f'<span class="reason">{escape(message)}</span></p>'
        )
    # A form refused without the `nothing_written` marker kept its request id (`web.app._keeps_request_id`).
    kept = refused != "close" and (refusal.get("data") or {}).get("nothing_written") is not True
    hint = " Sending the same form again is safe: it keeps its request id." if kept else ""
    return f'<p class="refused"><b>refused ({escape(code)}).</b> {escape(message)}{hint}</p>'


def po_page(
    overview: dict[str, Any],
    *,
    request_id: str,
    refusal: dict[str, Any] | None = None,
    submitted: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> str:
    """The PO head: new-session bar, then open (or `closed`) sessions as two-line rows, not a table."""
    sessions = overview.get("sessions") or []
    models = overview.get("models") or {}
    submitted = submitted or {}
    closed = bool(overview.get("closed"))
    now = now or datetime.now(UTC)
    items = [_po_session_row(item, closed=closed, now=now) for item in sessions]
    if closed:
        empty = '<p class="empty">no closed PO session</p>'
        title = "Closed sessions"
        more = '<a class="more" href="/po">open sessions</a>'
    else:
        empty = '<p class="empty">no PO session yet</p>'
        title = "Sessions"
        more = (
            f'<a class="more" href="/po?closed=1">closed sessions '
            f"({escape(str(overview.get('closed_count') or 0))})</a>"
        )
    listing = f'<ul class="po-list">{"".join(items)}</ul>' if items else empty
    body = "\n".join(
        [
            '<div class="lead"><h1>Product owner</h1>',
            f'<span class="age">{escape(str(overview.get("running") or 0))} turn(s) running</span></div>',
            _po_refusal(refusal),
            _po_new_session_form(
                models, overview.get("efforts") or {}, request_id=request_id, submitted=submitted
            ),
            _panel(title, listing, count=len(sessions) if sessions else None, more=more),
            f'<p class="hint empty">{escape(PO_NOTICE)}</p>',
        ]
    )
    return _page("Product owner", body, script=_PO_FORM_SCRIPT, nav="po")


def _po_session_row(item: dict[str, Any], *, closed: bool, now: datetime) -> str:
    """One session row: title, first message, then CLI, model, effort ("not set"), when, id."""
    session_id = str(item.get("session_id") or "")
    name, said = _head_model(_po_head(item))
    meta = [
        f'<span title="{escape(said)}">{escape(str(item.get("cli") or ""))} · <b>{escape(name)}</b></span>'
    ]
    meta.append(f'<span class="effort">{_effort(item.get("effort"), unset=PO_EFFORT_UNSET)}</span>')
    if closed:
        meta.append(f"<span>closed {_po_when(item.get('closed_at'), now)}</span>")
    else:
        meta.append(f"<span>{_po_when(item.get('last_activity_at'), now)}</span>")
    meta.append(f'<span class="id">{escape(session_id[:8])}</span>')
    transition = _po_transition(item.get("first_message_metadata"))
    if transition:
        meta.append(f'<span>{transition}</span>')
    if closed:
        side = ""
    else:
        state = _chip("turn running", "accent") if item.get("running") else '<span class="empty">idle</span>'
        side = f'<div class="side">{state}{_po_close_form(session_id)}</div>'
    href = f"/po/sessions/{quote(session_id)}"
    title = str(item.get("title") or "")
    if title:
        return (
            f'<li class="titled"><a class="title" href="{href}">{escape(title)}</a>'
            f'<div class="first">{_po_first_message(item.get("first_message"))}</div>'
            f'<div class="meta">{"".join(meta)}</div>{side}</li>'
        )
    return (
        f'<li><a class="title" href="{href}">'
        f"{_po_first_message(item.get('first_message'))}</a>"
        f'<div class="meta">{"".join(meta)}</div>{side}</li>'
    )


def _po_when(value: Any, now: datetime) -> str:
    """A moment as time ago (the moment on hover); a date once it is days old."""
    if value in (None, ""):
        return "—"
    text = value.isoformat() if isinstance(value, datetime) else str(value)
    try:
        moment = value if isinstance(value, datetime) else datetime.fromisoformat(text)
    except ValueError:
        return escape(text)
    moment = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    seconds = max(0.0, (now - moment).total_seconds())
    said = f"{_age(seconds)} ago" if seconds < 48 * 3600 else moment.date().isoformat()
    return f'<time datetime="{escape(text)}" title="{escape(text)}">{escape(said)}</time>'


def _po_close_form(session_id: str) -> str:
    """The owner's close: a plain form POST, no script."""
    return (
        f'<form class="po-close" method="post" action="/po/sessions/{quote(session_id)}/close">'
        '<button class="quiet" type="submit">close</button></form>'
    )


def _po_new_session_form_for(
    session: dict[str, Any], efforts: dict[str, Any] | None = None, *, request_id: str
) -> str:
    """Open another session with this one's CLI, model and effort via the `/po` form's own route.

    `default` effort is never sent: the first effort `efforts` offers is used and named. The
    request id is the page's own with a suffix, since an id belongs to one operation for good
    (`po_requests`) and reuse would be `request_conflict`.
    """
    cli, model = str(session.get("cli") or "").strip(), str(session.get("model") or "").strip()
    if not cli or not model:
        return ""
    effort = str(session.get("effort") or "").strip()
    note = ""
    if effort.lower() in EFFORT_DEFAULT:
        effort = next(
            (
                str(value).strip()
                for value in (efforts or {}).get(cli) or []
                if str(value).strip().lower() not in EFFORT_DEFAULT
            ),
            "",
        )
        opens = f"the new one opens at {effort}" if effort else "this installation offers no effort for it"
        note = f'<span class="hint">effort not set on this session; {escape(opens)}</span>'
    field = f'<input type="hidden" name="effort" value="{escape(effort)}">' if effort else ""
    return (
        '<form class="po-new" method="post" action="/po/sessions">'
        f'<input type="hidden" name="request_id" value="{escape(request_id)}-new-session">'
        f'<input type="hidden" name="cli" value="{escape(cli)}">'
        f'<input type="hidden" name="model" value="{escape(model)}">'
        f"{field}{note}"
        '<button class="quiet" type="submit">new session</button></form>'
    )


#: How many characters of a session's first owner message its row on `/po` shows, ellipsis included.
PO_FIRST_MESSAGE_CHARS = 80


#: The title input's length; the store's `MAX_TITLE_LENGTH` is the rule, this only stops typing.
PO_TITLE_MAX_CHARS = 120


def _po_first_message(text: Any) -> str:
    """The start of a session's first owner message as escaped plain text, or a muted placeholder."""
    words = " ".join(str(text or "").split())
    if not words:
        return '<span class="empty">no message yet</span>'
    if len(words) > PO_FIRST_MESSAGE_CHARS:
        words = words[: PO_FIRST_MESSAGE_CHARS - 1].rstrip() + "…"
    return escape(words)


def _po_head(session: dict[str, Any]) -> dict[str, Any]:
    """A PO session as a head: the model its last turn reported, else the one it was opened with."""
    return {
        "model": session.get("model"),
        "resolved_model": session.get("resolved_model"),
        "effort": session.get("effort"),
    }


def _po_new_session_form(
    models: dict[str, Any],
    efforts: dict[str, Any] | None = None,
    *,
    request_id: str,
    submitted: dict[str, Any],
) -> str:
    offered = [(cli, list(values or [])) for cli, values in models.items() if values]
    if not offered:
        return '<p class="po-bar empty">this installation offers no model for a PO session</p>'
    chosen_cli = str(submitted.get("cli") or offered[0][0])
    chosen_model = str(submitted.get("model") or "")
    listed = dict(offered).get(chosen_cli) or []
    if chosen_model not in listed and listed:
        # The first model a CLI lists is its preselected one; the script does the same on a CLI change.
        chosen_model = listed[0]
    # `default` (no effort flag) is never offered: a new session's effort is always chosen, and the
    # first one a CLI lists is its preselected one, as with the model.
    effort_lists = {
        cli: [
            str(value).strip()
            for value in (efforts or {}).get(cli) or []
            if str(value).strip().lower() not in EFFORT_DEFAULT
        ]
        for cli, _ in offered
    }
    chosen_effort = str(submitted.get("effort") or "")
    if chosen_effort not in effort_lists.get(chosen_cli, []):
        chosen_effort = next(iter(effort_lists.get(chosen_cli) or []), "")
    cli_options = "".join(
        f'<option value="{escape(cli)}"{_selected(cli == chosen_cli)}>{escape(cli)}</option>'
        for cli, _ in offered
    )
    groups = "".join(
        f'<optgroup label="{escape(cli)}">'
        + "".join(
            f'<option value="{escape(model)}" data-cli="{escape(cli)}"'
            f"{_selected(cli == chosen_cli and model == chosen_model)}>{escape(_model_name(model))}</option>"
            for model in values
        )
        + "</optgroup>"
        for cli, values in offered
    )
    effort_groups = "".join(
        f'<optgroup label="{escape(cli)}">'
        + "".join(
            f'<option value="{escape(effort)}" data-cli="{escape(cli)}"'
            f"{_selected(cli == chosen_cli and effort == chosen_effort)}>{escape(effort)}</option>"
            for effort in effort_lists[cli]
        )
        + "</optgroup>"
        for cli, _ in offered
    )
    return "\n".join(
        [
            '<form class="po-bar" id="po-new" method="post" action="/po/sessions">',
            "<h2>New session</h2>",
            f'<input type="hidden" name="request_id" value="{escape(request_id)}">',
            f'<select id="po-cli" name="cli" aria-label="CLI">{cli_options}</select>',
            f'<select id="po-model" name="model" aria-label="model">{groups}</select>',
            f'<select id="po-effort" name="effort" aria-label="reasoning effort">{effort_groups}</select>',
            '<button type="submit">new session</button>',
            "</form>",
        ]
    )


def po_session(
    document: dict[str, Any],
    *,
    request_id: str,
    draft: str = "",
    refusal: dict[str, Any] | None = None,
    refused: str = "send",
    title_draft: str | None = None,
) -> str:
    """One session: newest-first feed, message box, turn state and controls.

    The control row above the feed holds `send`, `stop turn` or `close`, and `new session`. A title
    heads the page with a rename form (`title_draft` keeps a refused rename). A closed session stays
    readable with only `new session`.
    """
    session = document.get("session") or {}
    session_id = str(session.get("session_id") or "")
    closed = session.get("state") == "closed"
    turns = document.get("turns") or []
    by_turn: dict[Any, list[dict[str, Any]]] = {}
    for entry in document.get("feed") or []:
        by_turn.setdefault(entry.get("turn_seq"), []).append(entry)
    queued = document.get("queued") or []
    # Messages queued until the running turn ends; newest first, above the turns.
    items: list[str] = [_po_queued_entry(entry) for entry in reversed(queued)]
    for turn in reversed(turns):
        items.extend(_po_entry(entry) for entry in reversed(by_turn.get(turn.get("seq"), [])))
        items.append(_po_turn_mark(turn))
    # The blocks a turn's end changes each carry a stable id, and the session script swaps exactly those
    # (PO_SESSION_BLOCKS) for the ones of a freshly read page; the composer is in none of them.
    feed = (
        f'<ol class="po-feed" id="po-feed">{"".join(items)}</ol>'
        if items
        else '<p class="empty" id="po-feed">nothing said yet</p>'
    )
    budget = document.get("context_budget")
    if isinstance(budget, dict):
        feed = (f'<p class="hint">Context budget: {escape(str(budget.get("measured_bytes")))} / '
                f'{escape(str(budget.get("threshold_bytes")))} {escape(str(budget.get("metric")))}. '
                'Deterministic proxy; checked on sprint resolution at idle.</p>' + feed)
    running = bool(document.get("running"))
    base = f"/po/sessions/{quote(session_id)}"
    stop = (
        f'<form method="post" action="{base}/stop">'
        f'<input type="hidden" name="seq" value="{escape(str(document.get("running_seq") or ""))}">'
        '<button class="quiet" type="submit">stop turn</button></form>'
        if running
        else ""
    )
    close = _po_close_form(session_id) if not running and not closed else ""
    # HTML has no nested forms, so the row holds the forms side by side and `send` uses `form=`.
    controls = "".join(
        [
            '<div class="po-controls">',
            "" if closed else '<button type="submit" form="po-send">send</button>',
            (
                f'<div class="aside"><span class="slot" id="po-stop">{stop}</span>'
                f"{_po_new_session_form_for(session, document.get('efforts') or {}, request_id=request_id)}"
                f'<span class="slot" id="po-close">{close}</span></div>'
            ),
            "</div>",
        ]
    )
    message = "\n".join(
        [
            f'<form class="sprint" id="po-send" method="post" action="{base}/messages">',
            f'<input type="hidden" name="request_id" value="{escape(request_id)}">',
            '<div class="field"><label for="po-text">message</label>',
            f'<textarea id="po-text" name="text" required>{escape(draft)}</textarea>',
            f'<p class="hint">{escape(PO_SEND_HINT)}</p></div>',
            "</form>",
        ]
    )
    head = " · ".join(
        escape(str(value)) for value in (session.get("created_at"), session.get("state")) if value
    )
    if running:
        turn_state = _chip("turn running", "accent")
    elif not closed:
        turn_state = '<span class="empty">idle</span>'
    else:
        turn_state = ""
    if queued:
        turn_state += " " + _chip(f"{len(queued)} queued", "accent")
    last = turns[-1] if turns else {}
    # The script's polling baseline is read from the page (initial or swapped in), never the JSON.
    polled = (
        f'data-turns="{len(turns)}" data-last="{escape(str(last.get("state") or ""))}" '
        f'data-queued="{len(queued)}" data-running="{"true" if running else "false"}"'
    )
    title = str(session.get("title") or "")
    heading = (
        f'{escape(title)} <span class="id">{escape(session_id[:8])}</span>'
        if title
        else f"PO session {escape(session_id[:8])}"
    )
    # Outside `po-head`, so a turn's end swapping the header never drops a title being typed.
    rename = (
        f'<form class="po-title" id="po-title" method="post" action="{base}/title">'
        f'<input type="text" name="title" maxlength="{PO_TITLE_MAX_CHARS}" aria-label="session title" '
        f'placeholder="untitled" value="{escape(title if title_draft is None else title_draft)}">'
        '<button class="quiet" type="submit">rename</button></form>'
    )
    body = "\n".join(
        [
            (
                f'<div class="lead" id="po-head"><h1>{heading}</h1>'
                f"{_head('PO · ' + str(session.get('cli') or ''), _po_head(session), compact=True, unset_effort=PO_EFFORT_UNSET)}"
                f'<span class="head-chip" id="po-turn-state" {polled}>{turn_state}</span>'
                f'<span class="age">{head}</span></div>'
            ),
            rename,
            _po_delegated(document.get("delegated")),
            _po_refusal(refusal, refused),
            (
                f'<p class="po-closed">closed {escape(str(session.get("closed_at") or ""))} '
                f"by {escape(str(session.get('closed_by') or ''))}</p>"
                if closed
                else ""
            ),
            controls
            if closed
            else _panel("Send", message + controls + '<p class="feedback" id="po-status"></p>'),
            _panel("Feed", feed, more='<a class="more" href="/po">all sessions</a>'),
            f'<p class="hint empty">{escape(PO_NOTICE)}</p>',
        ]
    )
    script = _PO_SESSION_SCRIPT.replace("__SESSION__", _js(session_id)).replace(
        "__BLOCKS__", "[" + ", ".join(f"'{block}'" for block in PO_SESSION_BLOCKS) + "]"
    )
    return _page(
        title or f"PO session {session_id[:8]}",
        body,
        script=script,
        nav="po",
        crumbs=(("PO", "/po"), (session_id[:8], base)),
    )


def _po_delegated(section: Any) -> str:
    """The cards this session delegated or receives results from, as a collapsed disclosure.

    Outside every `PO_SESSION_BLOCKS` block, so an in-place update never closes it. Empty when the
    document lacks the block; the reason when unreadable; `none` when nothing was delegated.
    """
    if not isinstance(section, dict):
        return ""
    items = section.get("items")
    if not isinstance(items, list):
        source = _mapping(section.get("source"))
        reason = escape(str(source.get("reason") or "no reason was recorded"))
        body = (
            f'<p class="unavailable"><b>could not find out the cards this session delegated:</b> {reason}</p>'
        )
        return _po_delegated_details("Delegated cards: unavailable", body)
    entries = list(_entries(items))
    if not entries:
        return _po_delegated_details(
            "Delegated cards: none", '<p class="empty">this session has delegated no card.</p>'
        )
    rows = []
    tally: dict[str, int] = {}
    pending = 0
    for item in entries:
        last = _mapping(item.get("last_return"))
        returned = (
            f"{_state_chip(last.get('state'))} {_or_dash(last.get('status') or 'pending')}" if last else "—"
        )
        state = str(item.get("state") or "unknown")
        tally[state] = tally.get(state, 0) + 1
        if item.get("relation") != "assigned" and (
            (last and not last.get("status")) or (not last and state in PO_RETURNED_STATES)
        ):
            pending += 1
        relation = (' <span class="age">(PO assignment)</span>' if item.get("relation") == "assigned"
                    else ' <span class="age">(as successor)</span>' if item.get("relation") == "inherited" else "")
        rows.append(
            [
                _link(str(item.get("ref") or "")) + relation,
                _or_dash(item.get("title")),
                _or_dash(item.get("type")),
                _state_chip(item.get("state")),
                returned,
            ]
        )
    summary = f"Delegated cards: {len(rows)} — " + ", ".join(
        f"{count} {_po_delegated_state(state)}" for state, count in tally.items()
    )
    if pending:
        summary += f" · {pending} {'result' if pending == 1 else 'results'} not returned yet"
    return _po_delegated_details(summary, _rows(["card", "title", "kind", "column", "last return"], rows))


#: The columns a delegated card returns its result from (`origin_outbox.TERMINAL_STATES`).
PO_RETURNED_STATES = ("done", "blocked")


def _po_delegated_state(state: str) -> str:
    """A column as the summary's tally says it: `done`, `blocked`, `in progress`, `in validate`."""
    word = state.replace("_", " ")
    return word if state in (*PO_RETURNED_STATES, "in_progress", "unknown") else f"in {word}"


def _po_delegated_details(summary: str, body: str) -> str:
    """The delegated-cards disclosure, closed on every render."""
    return (
        f'<details class="panel po-delegated" id="po-delegated"><summary>{escape(summary)}</summary>'
        f'<div class="body">{body}</div></details>'
    )


def _po_entry(entry: dict[str, Any]) -> str:
    """One feed item: the PO head's answer as the safe Markdown subset, the owner's text as typed."""
    role = "agent" if entry.get("role") == "agent" else "owner"
    who = "PO head" if role == "agent" else "owner"
    text = str(entry.get("text") or "")
    metadata = entry.get("metadata") or {}
    source = metadata.get("source") if isinstance(metadata, dict) else None
    if role == "owner" and source in {"dispatcher", "po-service"}:
        who = source
    shown = (
        f'<div class="md">{markdown.render(text)}</div>'
        if role == "agent"
        else _po_input_text(entry)
    )
    return (
        f'<li class="po-entry po-{role}"><div class="who">{who} · turn {escape(str(entry.get("turn_seq")))}'
        f" · {escape(str(entry.get('created_at') or ''))}</div>{shown}</li>"
    )


def _po_queued_entry(entry: dict[str, Any]) -> str:
    """A message in the PO service's queue, waiting for the running turn to end."""
    metadata = entry.get("metadata") or {}
    source = metadata.get("source") if isinstance(metadata, dict) else None
    who = source if source in {"dispatcher", "po-service"} else "owner"
    return (
        f'<li class="po-entry po-owner" data-state="queued"><div class="who">{who} · '
        f"{_chip('queued', 'accent')} · {escape(str(entry.get('queued_at') or ''))}</div>"
        f'{_po_input_text(entry)}</li>'
    )


def _po_input_text(entry: dict[str, Any]) -> str:
    """Native classification only. Historical absence stays ordinary expanded text."""
    text = escape(str(entry.get("text") or ""))
    metadata = entry.get("metadata")
    if not isinstance(metadata, dict) or metadata.get("source") not in {"dispatcher", "po-service"}:
        return f'<div class="text">{text}</div>'
    summary = escape(str(metadata.get("summary") or "Service input"))
    transition = _po_transition(metadata)
    if transition:
        summary += " · " + transition
    return (f'<div class="text">{summary}</div>'
            '<details class="po-service-input"><summary>Service instructions, comments, rights and full text</summary>'
            f'<div class="text">{text}</div></details>')


def _po_transition(metadata: Any) -> str:
    if not isinstance(metadata, dict) or metadata.get("source") != "po-service":
        return ""
    transition = metadata.get("transition")
    if not isinstance(transition, dict) or transition.get("reason") != "context_budget":
        return ""
    predecessor = str(transition.get("predecessor") or "")
    successor = str(transition.get("successor") or "")
    return (f'Context rollover: {escape(str(transition.get("measured_bytes")))} &gt; '
            f'{escape(str(transition.get("threshold_bytes")))} '
            f'{escape(str(transition.get("metric")))} · '
            f'<a href="/po/sessions/{quote(successor)}">new session</a> · '
            f'<a href="/po/sessions/{quote(predecessor)}">predecessor history</a>')


def _po_turn_mark(turn: dict[str, Any]) -> str:
    state = str(turn.get("state") or "unknown")
    word, tone = TURN_MARKS.get(state, (state, ""))
    reason = turn.get("reason")
    said = f' <span class="reason">{escape(str(reason))}</span>' if reason else ""
    return f'<li class="po-mark" data-state="{escape(state)}">turn {escape(str(turn.get("seq")))} {_chip(word, tone)}{said}</li>'


#: Session page blocks a turn's end changes; the session script swaps exactly these (not the composer).
PO_SESSION_BLOCKS = ("po-head", "po-stop", "po-close", "po-feed")

