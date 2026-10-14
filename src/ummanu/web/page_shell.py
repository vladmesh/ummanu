"""Server-rendered presentation: shell."""

from __future__ import annotations

from datetime import timedelta
from html import escape
from typing import Any

from ummanu.web.doctor import DOCTOR_NOT_BUILT, unreadable as doctor_unreadable
from ummanu.web.page_assets import _REFRESH_SCRIPT, _RESET_SCRIPT, STYLE
from ummanu.web.page_parts import (
    READING_PREDATES_RESET,
    _age,
    _chip,
    _duration,
    _percent,
    _render_now,
    _reset_reading,
)
from ummanu.web.page_values import _BELL_SOURCE, _DOCTOR_SOURCE, _FROM_POST, _LIMITS_SOURCE
from ummanu.web.provider_usage import credit_count, credit_moment, credit_moment_iso
from ummanu.webproto.reads import accepted

TITLE = "ummanu"


#: Said on every page. The app has no authentication of its own (see :mod:`ummanu.web.server`);
#: off-host requests pass TLS and a password at :mod:`ummanu.webfront` first.
LOOPBACK_NOTICE = (
    "local only — this application has no password, no TLS and no authorisation of its own and is "
    "refused a non-loopback address; anything reaching it from outside came through the guarded "
    "front"
)


#: Primary navigation in order of use; keys are page names, so the current page is marked.
NAV: tuple[tuple[str, str, str], ...] = (
    ("dashboard", "/", "Dashboard"),
    ("sprints", "/sprints", "Sprints"),
    ("projects", "/projects", "Projects"),
    ("po", "/po", "PO"),
)


#: Said when no provider layer was built into this process.
LIMITS_NOT_BUILT = "this web process was built without the provider usage layer"


#: Said when the reading carries nothing about this provider.
LIMITS_NOT_IN_READING = "this reading carried nothing about this provider"


#: Providers the bar always shows, in this order, whatever a reading holds.
BAR_PROVIDERS: tuple[tuple[str, str], ...] = (("claude", "Claude"), ("codex", "Codex"))


def _bell() -> str:
    """The header bell: unread owner events, or `?` when the board could not count them."""
    read = _BELL_SOURCE.get()
    if read is None:
        return ""
    try:
        reading = read()
    except Exception as exc:  # noqa: BLE001 - a bell that cannot count never takes a page down
        reading = {"state": "unavailable", "reason": f"{type(exc).__name__}: {exc}", "count": 0}
    if not isinstance(reading, dict):
        return ""
    if reading.get("state") != "available":
        reason = str(reading.get("reason") or "the owner events could not be read")
        return (
            f'<a class="bell bell-unknown" id="owner-bell" href="/owner-events" title="{escape(reason)}" '
            'aria-label="owner events: unread count unknown">'
            '<span aria-hidden="true">🔔</span><span class="bell-count">?</span></a>'
        )
    count = int(reading.get("count") or 0)
    tone = " bell-unread" if count else ""
    label = f"owner events: {count} unread"
    return (
        f'<a class="bell{tone}" id="owner-bell" href="/owner-events" title="{escape(label)}" '
        f'aria-label="{escape(label)}"><span aria-hidden="true">🔔</span>'
        f'<span class="bell-count">{count}</span></a>'
    )


def _limits_bar() -> str:
    """The bar, from whatever the transport feeds it (possibly nothing)."""
    read = _LIMITS_SOURCE.get()
    doctor_read = _DOCTOR_SOURCE.get()
    return _limits_bar_of(
        read() if read is not None else None,
        doctor=doctor_read() if doctor_read is not None else None,
    )


def _limits_bar_of(section: dict[str, Any] | None, *, doctor: dict[str, Any] | None = None) -> str:
    """The bar for one section (test seam); only a current reading draws a percentage."""
    document = section.get("document") if isinstance(section, dict) and section.get("available") else None
    document = document if isinstance(document, dict) else None
    if document is not None:
        refused = LIMITS_NOT_IN_READING
    elif isinstance(section, dict):
        refused = str(section.get("reason") or "provider usage was not read")
    else:
        refused = LIMITS_NOT_BUILT
    carried: dict[str, dict[str, Any]] = {}
    for provider in (document or {}).get("providers") or []:
        if isinstance(provider, dict) and provider.get("id") is not None:
            carried[str(provider["id"])] = provider
    named = {key for key, _ in BAR_PROVIDERS}
    parts = [_doctor_lamp(doctor)]
    parts += [_bar_provider(label, carried.get(key), refused) for key, label in BAR_PROVIDERS]
    parts += [
        _bar_provider(str(provider.get("label") or key), provider, refused)
        for key, provider in carried.items()
        if key not in named
    ]
    parts.append(
        '<label class="bar-refresh" title="reload this page every 30 s while nobody is typing">'
        '<input type="checkbox" data-refresh-toggle> auto</label>'
    )
    return (
        '<footer class="statusbar" id="status-bar" aria-label="installation health and provider usage limits">'
        f'<div class="row">{"".join(parts)}</div></footer>'
    )


#: Unknown is reserved for the expected initial doctor state, before any result exists.
LAMP_WORDS: dict[str, str] = {
    "green": "no problem is recorded for this installation",
    "yellow": "this installation runs, but something wants a person's eye",
    "red": "this installation cannot be trusted to run work, or its health is unknown",
    "unknown": "recorded doctor is unknown / not yet collected",
}


def _doctor_lamp(section: dict[str, Any] | None) -> str:
    """The lamp: one colour from the recorded health, linking to the problems behind it."""
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
    problems = [problem for problem in document.get("problems") or [] if isinstance(problem, dict)
                and (problem.get("source") == "status" or not accepted(problem))]
    count = f' <span class="lamp-count">{len(problems)}</span>' if problems else ""
    title = LAMP_WORDS[colour]
    if problems:
        title = f"{title}: {problems[0].get('message') or ''}"
    title += f"; doctor run at {document.get('doctor_run_at') or 'unknown'}"
    return (
        f'<a class="lamp lamp-{colour}" href="/doctor" title="{escape(title)}" '
        f'aria-label="{escape("installation health: " + colour)}">doctor{count}</a>'
    )


def _bar_provider(label: str, provider: dict[str, Any] | None, refused: str) -> str:
    """One provider's place on the bar: its windows, or why there is no current reading."""
    if provider is None:
        return f'<span class="provider"><b>{escape(label)}</b>{_bar_no_reading(refused)}</span>'
    shown = escape(str(provider.get("label") or label))
    status = str(provider.get("status") or "unavailable")
    age = provider.get("age_seconds")
    old = "" if age in (None, 0, 0.0) else f' <span class="age">{_age(age)} old</span>'
    if status != "available":
        reason = str(provider.get("reason") or "no reason was recorded")
        mark = _chip(status, "warn")
        return f'<span class="provider"><b>{shown}</b>{mark}{_bar_no_reading(reason)}{old}</span>'
    windows = [window for window in provider.get("windows") or [] if isinstance(window, dict)]
    if not windows:
        return (
            f'<span class="provider"><b>{shown}</b>'
            f"{_bar_no_reading('this reading carried no usage window')}{old}</span>"
        )
    drawn = "".join(_bar_window(window) for window in windows)
    credits = _bar_reset_credits(provider.get("reset_credits")) if provider.get("id") == "codex" else ""
    return f'<span class="provider"><b>{shown}</b>{drawn}{credits}{old}</span>'


def _bar_window(window: dict[str, Any]) -> str:
    """One usage window's chip: name, what is left, time to reset; stale once its window reset."""
    countdown, predates_reset = _reset_reading(window.get("resets_at"), window.get("window_minutes"))
    if predates_reset:
        figure = f'<b class="stale" title="{escape(READING_PREDATES_RESET)}">stale</b>'
    else:
        figure = f"<b>{_percent(window.get('remaining_percent'))}</b>"
    return (
        f'<span class="window"><span class="win-name">{escape(str(window.get("name") or "window"))}</span>'
        f'{figure}<span class="dot">·</span>{countdown}</span>'
    )


def _bar_reset_credits(credits: Any) -> str:
    """The Codex reset credits folded into one `<details>` label (`1 reset`) with the spend button.

    Empty when no credit is available. Values go through the layer's normalisers, so a hand-made
    document cannot break the bar.
    """
    if not isinstance(credits, dict):
        return ""
    available = credit_count(credits.get("available"))
    if not available:
        return ""
    count = f"{available} reset{'' if available == 1 else 's'}"
    lines = [f'<span class="pop-line">{escape(count)} banked</span>']
    moment = credit_moment(credits.get("next_expires_at"))
    if moment is not None:
        try:
            ahead = (moment - _render_now()) // timedelta(microseconds=1)
        except (OverflowError, ValueError):
            ahead = None
        if ahead is not None:
            expiry = f"expires in {_duration(ahead / 1_000_000)}" if ahead > 0 else "expired"
            title = credit_moment_iso(credits.get("next_expires_at")) or ""
            lines.append(f'<span class="pop-line expires" title="{escape(title)}">{escape(expiry)}</span>')
    applicable = credit_count(credits.get("applicable"))
    if applicable:
        lines.append(f'<span class="pop-line applies">{escape(RESET_APPLICABLE)}</span>')
    else:
        lines.append(f'<span class="pop-line warn">{escape(RESET_NOT_APPLICABLE)}</span>')
    return (
        f'<details class="credits"><summary title="{escape(RESET_SUMMARY_TITLE)}">{escape(count)}</summary>'
        f'<div class="credits-pop">{"".join(lines)}{_bar_reset_button(credits)}</div></details>'
    )


#: The hover title of the folded credits label.
RESET_SUMMARY_TITLE = "Codex rate-limit reset credits: open for details and the reset"


#: The provider counts a reset as applicable now (`applicable_available_count` above zero).
RESET_APPLICABLE = "the provider counts a reset as applicable now"


#: The provider does not (count zero or unknown); its rule is undocumented, so its answer decides.
RESET_NOT_APPLICABLE = (
    "the provider does not count a reset as applicable now: a press may answer nothing to reset, "
    "or refill windows that still have usage left"
)


def _bar_reset_button(credits: Any) -> str:
    """The button spending one Codex reset credit; absent when none is available.

    Whether a reset applies is the provider's call at consume time. The operation re-reads the
    provider and refuses without a credit, so a stale page cannot spend one.
    """
    if not isinstance(credits, dict):
        return ""
    available = credit_count(credits.get("available"))
    if not available:
        return ""
    feedback = '<span class="bar-feedback" id="codex-reset-feedback" role="status"></span>'
    question = f"Spend 1 of {available} Codex reset credits?"
    return (
        '<button type="button" class="bar-reset" data-codex-reset '
        f'data-confirm="{escape(question)}" title="spend 1 Codex reset credit">'
        f"reset limit</button>{feedback}"
    )


def _bar_no_reading(reason: str) -> str:
    """The stand-in for a percentage: words, never a number (no reading is not a low reading)."""
    return f'<span class="reason">no current reading — {escape(reason)}</span>'


def _page(
    title: str,
    body: str,
    *,
    script: str = "",
    nav: str = "",
    crumbs: tuple[tuple[str, str], ...] = (),
) -> str:
    """The shared shell: top bar, navigation with `nav` marked, and `crumbs` (what is open under it)."""
    current = ' aria-current="page"'
    links = "".join(
        f'<a href="{escape(href)}"{current if key == nav else ""}>{escape(label)}</a>'
        for key, href, label in NAV
    )
    trail = ""
    if crumbs:
        parts = []
        for index, (label, href) in enumerate(crumbs):
            if index == len(crumbs) - 1:
                parts.append(f'<span class="here">{escape(label)}</span>')
            else:
                parts.append(f'<a href="{escape(href)}">{escape(label)}</a><span class="sep">/</span>')
        trail = f'<div class="crumbs">{"".join(parts)}</div>'
    return "\n".join(
        [
            "<!doctype html>",
            '<html lang="en"><head><meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            f"<title>{escape(title)} · {escape(TITLE)}</title>",
            f"<style>{STYLE}</style>",
            "<script>try{const theme=localStorage.getItem('ummanu.web.theme');if(theme==='light'||theme==='dark')document.documentElement.dataset.theme=theme;}catch(error){}</script>",
            "</head><body>",
            '<header class="top"><div class="row">',
            f'<a class="brand" href="/">{escape(TITLE)}</a>',
            f'<nav class="primary" aria-label="primary">{links}</nav>',
            trail,
            f'<p class="notice" title="{escape(LOOPBACK_NOTICE)}">local only</p>',
            _bell(),
            '<button class="theme-toggle" id="theme-toggle" type="button" aria-label="Toggle color theme" title="Toggle color theme"><span class="sun" aria-hidden="true">☀</span><span class="moon" aria-hidden="true">☾</span></button>',
            '<a class="top-action" href="/sprints/new">New sprint</a>',
            "</div></header>",
            "<main>",
            body,
            "</main>",
            _limits_bar(),
            """<script>(() => {
  const button = document.getElementById('theme-toggle');
  function currentTheme() {
    const selected = document.documentElement.dataset.theme;
    if (selected === 'light' || selected === 'dark') return selected;
    return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }
  function showTheme() {
    if (!button) return;
    const theme = currentTheme();
    button.dataset.theme = theme;
    const next = theme === 'dark' ? 'light' : 'dark';
    button.setAttribute('aria-label', 'Switch to ' + next + ' theme');
    button.title = 'Switch to ' + next + ' theme';
  }
  if (button) button.addEventListener('click', () => {
    const next = currentTheme() === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('ummanu.web.theme', next); } catch (error) {}
    showTheme();
  });
  showTheme();
})();</script>""",
            # Every page refreshes (never while a form holds typed text) except a POST answer,
            # where a reload would make the browser re-send the submission.
            f"<script>{script}{'' if _FROM_POST.get() else _REFRESH_SCRIPT}{_RESET_SCRIPT}</script>",
            "</body></html>",
        ]
    )


def error(status: int, code: str, message: str) -> str:
    """A refusal as a page: the status, the protocol code that produced it, and what it said."""
    body = "\n".join(
        [
            f"<h2>{status} — {escape(code)}</h2>",
            f'<p class="unavailable"><b>this request was refused.</b> {escape(message)}</p>',
            '<p><a href="/">back to the dashboard</a></p>',
        ]
    )
    return _page(f"{status} {code}", body)


def redirect(location: str, *, what: str = "this sprint is open") -> str:
    """The body of a 303. A browser follows the header; anything that does not gets the link."""
    return _page(
        "opened",
        f'<p>{escape(what)}. <a href="{escape(location)}">{escape(location)}</a></p>',
    )

