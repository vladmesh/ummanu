"""The pages, rendered from the layer's own documents and from nothing else.

Every value on a page comes out of a `web-read` or `web-run` document. Nothing here recomputes a
state, decides whether an agent is alive, or fills a gap with a plausible value — a section whose
source refused says so, in its own words, where the list would have been.

That is the one rendering rule worth stating twice, because it is criterion 2: **an empty list and
"I could not find out" are different things, and they look different.** An empty section is a quiet
line saying there is nothing; an unavailable one is a marked block carrying the reason the source
gave and the age of whatever is being shown instead. A dashboard that drew both as blank space
would tell an operator that the pipeline is idle at the exact moment it has lost sight of it.

The pages are server-rendered, so what a source said is in the markup rather than assembled later
by a script that may not run. The only thing the script does is tail a card's events from a cursor
the client itself holds, and narrow the issue list to the product a sprint form has selected -- both
of which leave a page that works when it does not run.

The sprint form obeys the same rule twice over. Every choice on it is an entry of `sprint_options`,
so a registry or a board that holds something else offers something else, and nothing about a
product, an issue, a project or a head profile is written into this module. And a refused
submission is rendered from the very object that was submitted, so what a person typed comes back
on the screen exactly as they typed it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from html import escape
from typing import Any
from urllib.parse import quote

from ummanu.web import markdown
from ummanu.web.doctor import DOCTOR_NOT_BUILT
from ummanu.web.doctor import unreadable as doctor_unreadable
from ummanu.web.provider_usage import credit_count, credit_moment, credit_moment_iso

TITLE = "ummanu"

#: Said on every page. This application still has no authentication of any kind of its own, so
#: where it may listen is not a deployment preference; see :mod:`ummanu.web.server`. What
#: changed with DoD 5 is what stands in front of it, not what it is: a request that arrived from
#: off this host passed TLS and a password at the front (:mod:`ummanu.webfront`) before it
#: reached this process, and there is no path here that does not.
LOOPBACK_NOTICE = (
    "local only — this application has no password, no TLS and no authorisation of its own and is "
    "refused a non-loopback address; anything reaching it from outside came through the guarded "
    "front"
)

STYLE = """
:root {
  color-scheme: light dark;
  --ground: #f3f5f8; --surface: #ffffff; --raised: #eef1f5; --line: #d9dfe7; --line-strong: #b9c2cd;
  --ink: #18212c; --muted: #5d6b7a; --faint: #8a95a3;
  --accent: #2456a6; --accent-ink: #ffffff; --accent-soft: #e4ecf9;
  --ok: #1e7f4f; --ok-soft: #dff3e8; --warn: #a8600f; --warn-soft: #fbeedb; --bad: #b3261e; --bad-soft: #fbe3e1;
  --mono: "IBM Plex Mono", ui-monospace, "SFMono-Regular", Menlo, monospace;
  --sans: "IBM Plex Sans", system-ui, -apple-system, "Segoe UI", sans-serif;
}
:root[data-theme="light"] { color-scheme: light; }
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --ground: #11161d; --surface: #1a2028; --raised: #222a34; --line: #2c3541; --line-strong: #40495a;
    --ink: #e7ebf0; --muted: #a0abb8; --faint: #6f7b89;
    --accent: #8db1f0; --accent-ink: #0f1a2e; --accent-soft: #223252;
    --ok: #5fcf8f; --ok-soft: #173627; --warn: #f0b060; --warn-soft: #3a2a12; --bad: #f28b84; --bad-soft: #3d1c1a;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --ground: #11161d; --surface: #1a2028; --raised: #222a34; --line: #2c3541; --line-strong: #40495a;
  --ink: #e7ebf0; --muted: #a0abb8; --faint: #6f7b89;
  --accent: #8db1f0; --accent-ink: #0f1a2e; --accent-soft: #223252;
  --ok: #5fcf8f; --ok-soft: #173627; --warn: #f0b060; --warn-soft: #3a2a12; --bad: #f28b84; --bad-soft: #3d1c1a;
}
* { box-sizing: border-box; }
html { background: var(--ground); }
body { margin: 0; background: var(--ground); color: var(--ink); font: 14px/1.5 var(--sans); }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
h1, h2, h3 { margin: 0; text-wrap: balance; }
h1 { font-size: 1.35rem; font-weight: 600; letter-spacing: -.01em; }
h2 { font-size: .8rem; font-weight: 600; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); }
h3 { font-size: 1rem; font-weight: 600; }
code, .mono, time, .ref { font-family: var(--mono); font-size: .86em; }
pre { white-space: pre-wrap; overflow-x: auto; margin: 0; font: .86rem/1.5 var(--mono); }

/* the top bar: product, navigation, where you are */
.top { position: sticky; top: 0; z-index: 5; background: var(--surface); border-bottom: 1px solid var(--line); }
.top .row { max-width: 1280px; margin: 0 auto; padding: 0 20px; display: flex; align-items: center; gap: 1.5rem; min-height: 3rem; flex-wrap: wrap; }
.brand { font-weight: 600; letter-spacing: -.01em; color: var(--ink); }
.brand:hover { text-decoration: none; }
nav.primary { display: flex; gap: .25rem; }
nav.primary a { padding: .8rem .6rem; color: var(--muted); border-bottom: 2px solid transparent; }
nav.primary a:hover { color: var(--ink); text-decoration: none; }
nav.primary a[aria-current="page"] { color: var(--ink); border-bottom-color: var(--accent); }
.top-action { margin-left: auto; padding: .35rem .7rem; border-radius: 4px; background: var(--accent); color: var(--accent-ink); font-weight: 600; }
.top-action:hover { text-decoration: none; filter: brightness(1.08); }
.crumbs { display: flex; align-items: center; gap: .4rem; color: var(--muted); font-size: .85rem; }
.crumbs .sep { color: var(--faint); }
.crumbs .here { color: var(--ink); font-family: var(--mono); }
.top .notice { margin-left: auto; color: var(--faint); font-size: .72rem; max-width: 22rem; text-align: right; line-height: 1.25; }
.top .theme-toggle { display:inline-flex; align-items:center; gap:.25rem; padding:.3rem .45rem; border:1px solid var(--line-strong); border-radius:999px; background:transparent; color:var(--muted); line-height:1; }
.top .theme-toggle:hover { background:var(--raised); color:var(--ink); filter:none; }
.top .theme-toggle .sun, .top .theme-toggle .moon { opacity:.35; }
.top .theme-toggle[data-theme="light"] .sun, .top .theme-toggle[data-theme="dark"] .moon { opacity:1; color:var(--ink); }

/* the owner's bell (secretary-1770): the unread count on every page, and its list */
.top .bell { display: inline-flex; align-items: center; gap: .3rem; padding: .25rem .55rem; border-radius: 999px; border: 1px solid var(--line-strong); color: var(--muted); font-size: .85rem; line-height: 1; }
.top .bell:hover { text-decoration: none; color: var(--ink); }
.top .bell .bell-count { font-weight: 600; font-variant-numeric: tabular-nums; }
.top .bell.bell-unread { border-color: transparent; background: var(--warn-soft); color: var(--warn); }
.top .bell.bell-unknown { color: var(--faint); }
ol.owner-events { list-style: none; margin: 0; padding: 0; display: grid; gap: .5rem; }
ol.owner-events li { display: flex; flex-wrap: wrap; gap: .4rem .75rem; align-items: baseline; padding: .55rem .75rem; border: 1px solid var(--line); border-radius: 6px; min-width: 0; }
ol.owner-events li.unread { border-left: 3px solid var(--warn); background: var(--warn-soft); }
ol.owner-events li.pinned { border-left-color: var(--bad); }
ol.owner-events .text { flex: 1 1 100%; order: 3; white-space: pre-wrap; overflow-wrap: anywhere; }
ol.owner-events .when { margin-left: auto; }
ol.owner-events form { order: 4; }
ol.owner-events .held { order: 4; color: var(--muted); font-size: .85rem; }
.owner-events-actions { display: flex; flex-wrap: wrap; gap: .5rem; align-items: center; margin-bottom: .75rem; }
.owner-events-actions form { margin: 0; }

/* the page */
main { max-width: 1280px; margin: 0 auto; padding-block: 1.25rem 4rem; padding-inline: 20px; }
.lead { display: flex; align-items: baseline; gap: 1rem; flex-wrap: wrap; margin-bottom: 1rem; }
.lead .age { margin-left: auto; }
.age { color: var(--faint); font-size: .8rem; font-family: var(--mono); }
.grid { display: grid; grid-template-columns: minmax(0, 7fr) minmax(0, 4fr); gap: 1rem; align-items: start; }
.grid > .col { display: grid; gap: 1rem; min-width: 0; }
.push { margin-left: auto; display: inline-flex; gap: .4rem; align-items: center; }
.clamp { display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; line-clamp: 2; overflow: hidden; }
.attention { display: flex; align-items: center; gap: .9rem; flex-wrap: wrap; margin-top: 1rem; padding: .6rem .9rem; border-radius: 6px; background: var(--warn-soft); color: var(--ink); }
.attention > b { color: var(--warn); font-size: .74rem; text-transform: uppercase; letter-spacing: .06em; }
.attention > a { margin-left: auto; color: var(--warn); font-size: .85rem; }
main > details.panel { margin-top: 1rem; }
main > .unavailable { margin-top: 1rem; }
details.drain > summary { list-style: none; cursor: pointer; padding: .3rem .7rem; border: 1px solid var(--line-strong); border-radius: 4px; color: var(--muted); font-size: .85rem; }
details.drain > summary::-webkit-details-marker { display: none; }
details.drain[open] > summary { display: none; }
.sprints-block { margin-top: 1.25rem; }
.block-head { display: flex; align-items: baseline; gap: .6rem; margin-bottom: .6rem; }
.block-head .count { font: .8rem var(--mono); color: var(--muted); }
.block-head .more { margin-left: auto; font-size: .85rem; }
.compact-sprints { display: grid; grid-template-columns: repeat(auto-fill, minmax(26rem, 1fr)); gap: 1rem; }
@media (max-width: 600px) { .compact-sprints { grid-template-columns: minmax(0, 1fr); } }
.compact-sprint { display: grid; gap: .7rem; align-content: start; padding: .9rem 1rem; background: var(--surface); border: 1px solid var(--line); border-radius: 6px; min-width: 0; }
.compact-sprint header { display: flex; gap: .45rem; align-items: center; flex-wrap: wrap; }
.compact-sprint h3 a { font-family: var(--mono); font-size: .95rem; }
.compact-sprint .goal { margin: 0; color: var(--muted); }
.card-box { display: grid; gap: .35rem; padding: .55rem .7rem; border: 1px solid var(--line); border-radius: 6px; background: var(--ground); }
.card-line { display: flex; align-items: center; gap: .45rem; flex-wrap: wrap; }
.card-title { font-weight: 500; }
.heads-line { display: flex; gap: .4rem; flex-wrap: wrap; }
.budget-line { display: flex; align-items: center; gap: .6rem; font-size: .75rem; color: var(--faint); }
.budget-line .track { position: relative; flex-grow: 1; height: 4px; border-radius: 999px; background: var(--raised); }
.budget-line .fill { position: absolute; left: 0; top: 0; bottom: 0; border-radius: 999px; background: var(--ok); }
.budget-line.signal .fill { background: var(--warn); }
.budget-line.hard .fill { background: var(--bad); }
.budget-line .mark { position: absolute; top: -3px; width: 1px; height: 10px; background: var(--warn); }
.budget-line .mono { font-size: .72rem; white-space: nowrap; }
.po-feed { list-style:none; padding:0; margin:0; display:grid; gap:.6rem; min-width:0; max-width:100%; }
.po-entry { border-left: 3px solid var(--line-strong); padding:.35rem .7rem; min-width:0; max-width:100%; }
.po-entry.po-agent { border-left-color: var(--accent); background: var(--raised); }
.po-entry .who { font-size:.8rem; color:var(--muted); }
.po-entry .text { white-space: pre-wrap; overflow-wrap:anywhere; word-break:break-word; min-width:0; max-width:100%; }
.po-entry .md { overflow-wrap:anywhere; word-break:break-word; min-width:0; max-width:100%; }
.po-entry .md > :first-child { margin-top:0; }
.po-entry .md > :last-child { margin-bottom:0; }
.po-entry .md p, .po-entry .md ul, .po-entry .md ol, .po-entry .md blockquote, .po-entry .md pre { margin:.4rem 0; }
.po-entry .md h3, .po-entry .md h4, .po-entry .md h5, .po-entry .md h6 { margin:.6rem 0 .25rem; font-weight:600; }
.po-entry .md h3 { font-size:1rem; } .po-entry .md h4 { font-size:.95rem; }
.po-entry .md h5, .po-entry .md h6 { font-size:.9rem; color:var(--muted); }
.po-entry .md ul, .po-entry .md ol { padding-left:1.4rem; }
.po-entry .md li > ul, .po-entry .md li > ol { margin:.15rem 0; }
.po-entry .md blockquote { border-left:3px solid var(--line-strong); padding-left:.7rem; color:var(--muted); margin-inline:0; }
.po-entry .md hr { border:0; border-top:1px solid var(--line); margin:.6rem 0; }
.po-entry .md code { background:var(--surface); border:1px solid var(--line); border-radius:3px; padding:0 .25em; overflow-wrap:anywhere; word-break:break-word; }
.po-entry .md pre { white-space:pre; max-width:100%; min-width:0; overflow-x:auto; overflow-wrap:normal; word-break:normal; background:var(--surface); border:1px solid var(--line); border-radius:4px; padding:.5rem .7rem; }
.po-entry .md pre code { background:none; border:0; padding:0; font-size:inherit; overflow-wrap:normal; word-break:normal; }
.po-mark { font-size:.85rem; color:var(--muted); }
/* The composer's one row of controls. `send` opens it; everything that is not `send` is pushed to
   the far end, so the hand reaching for `send` never lands on `close` -- which cannot be undone,
   a closed session is never reopened. The row wraps rather than overflows, and it is in the normal
   flow: the bottom bar's reserved height still keeps the composer clear of the bar at phone width. */
.po-bar { display:flex; flex-wrap:wrap; align-items:center; gap:.5rem; padding:.6rem .9rem; margin:0 0 1rem; background:var(--surface); border:1px solid var(--line); border-radius:6px; }
.po-bar h2 { margin-right:.3rem; }
.po-bar select { min-width:7rem; }
.po-bar button { margin-left:auto; }
.po-list { list-style:none; margin:-.75rem -.9rem; padding:0; }
.po-list li { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:.2rem 1rem; align-items:center; padding:.65rem .9rem; }
.po-list li + li { border-top:1px solid var(--line); }
.po-list li:hover { background:var(--raised); }
.po-list .title { font-size:.95rem; color:var(--ink); overflow-wrap:anywhere; display:-webkit-box; -webkit-box-orient:vertical; -webkit-line-clamp:2; line-clamp:2; overflow:hidden; }
.po-list .title:hover { color:var(--accent); text-decoration:none; }
.po-list .meta { grid-column:1; display:flex; flex-wrap:wrap; gap:.15rem .5rem; font-size:.8rem; color:var(--muted); }
.po-list .meta > * + *::before { content:"·"; color:var(--faint); margin-right:.5rem; }
.po-list .meta b { color:var(--ink); font-weight:500; }
.po-list .meta time { font:inherit; }
.po-list .meta .id { font-family:var(--mono); color:var(--faint); }
.po-list .side { grid-column:2; grid-row:1 / span 2; display:flex; align-items:center; gap:.6rem; }
.po-list .side .empty { font-size:.85rem; }
.po-list .po-close button { padding:.2rem .6rem; font-weight:400; border-color:var(--line-strong); color:var(--muted); }
.po-list .po-close button:hover { border-color:var(--warn); color:var(--warn); filter:none; }
.po-list li.titled .title { font-weight:600; }
.po-list li.titled .side { grid-row:1 / span 3; }
.po-list .first { grid-column:1; font-size:.9rem; color:var(--muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.lead h1 .id { font-family:var(--mono); font-size:.8rem; font-weight:400; color:var(--faint); }
.po-title { display:flex; flex-wrap:wrap; align-items:center; gap:.5rem; margin:-.5rem 0 1rem; }
.po-title input { flex:0 1 24rem; min-width:0; }
.po-title button { font-weight:400; }
@media (max-width: 600px) {
  .po-bar select { flex:1 1 7rem; }
  .po-bar button { margin-left:0; flex-basis:100%; }
  .po-list li { grid-template-columns:minmax(0,1fr); }
  .po-list .side, .po-list li.titled .side { grid-column:1; grid-row:auto; }
}
.po-controls { display:flex; flex-wrap:wrap; align-items:center; gap:.6rem; margin-top:.6rem; }
.po-controls .aside { display:flex; flex-wrap:wrap; align-items:center; gap:.6rem; margin-left:auto; }
.po-controls .aside button { font-weight:400; }
.po-controls .aside .slot { display:contents; }
.po-controls .aside .po-close button { border-color:var(--line-strong); color:var(--muted); }
.po-controls .aside .po-close button:hover { border-color:var(--warn); color:var(--warn); filter:none; }
@media (max-width: 900px) { .grid { grid-template-columns: minmax(0, 1fr); } }

/* panels: one surface per subject */
.panel { background: var(--surface); border: 1px solid var(--line); border-radius: 6px; min-width: 0; }
.panel > header { display: flex; align-items: center; gap: .6rem; padding: .6rem .9rem; border-bottom: 1px solid var(--line); }
.panel > header .count { font-family: var(--mono); font-size: .8rem; color: var(--muted); }
.panel > header .more { margin-left: auto; font-size: .85rem; }
.panel > .body { padding: .75rem .9rem; min-width:0; max-width:100%; }
.panel > .body > * + * { margin-top: .6rem; }
.panel table { margin: -.25rem 0; }
.stack > * + * { margin-top: .6rem; }
details.panel > summary { list-style: none; cursor: pointer; padding: .6rem .9rem; font-weight: 600; color: var(--muted); font-size: .8rem; text-transform: uppercase; letter-spacing: .06em; }
details.panel > summary::-webkit-details-marker { display: none; }
details.panel > summary::before { content: "▸ "; color: var(--faint); }
details.panel[open] > summary::before { content: "▾ "; }
details.panel > .body { padding: .25rem .9rem .9rem; }
main > details.po-delegated { margin: 0 0 1rem; }
details.po-delegated > summary { text-transform: none; letter-spacing: normal; font-size: .9rem; }

/* the pipeline strip */
.strip { display: flex; align-items: center; gap: 1rem; flex-wrap: wrap; padding: .7rem .9rem; }
.strip .push { gap: 1rem; }
#pause-feedback-holder:has(> .feedback:empty) { display: none; }
.light { display: inline-flex; align-items: center; gap: .45rem; padding: .3rem .7rem; border-radius: 999px; font-weight: 600; font-size: .85rem; border: 1px solid transparent; }
.light::before { content: ""; width: .55rem; height: .55rem; border-radius: 50%; background: currentColor; }
.light-running, .light-ok { color: var(--ok); background: var(--ok-soft); }
.light-drain, .light-attention { color: var(--warn); background: var(--warn-soft); }
.light-freeze, .light-bad { color: var(--bad); background: var(--bad-soft); }
.light-unknown { color: var(--muted); background: var(--raised); }

/* chips: state in form */
.chip { display: inline-block; padding: .05rem .5rem; border-radius: 999px; font-size: .78rem; font-weight: 600; line-height: 1.5; background: var(--raised); color: var(--muted); white-space: nowrap; }
.chip-ok { background: var(--ok-soft); color: var(--ok); }
.chip-warn { background: var(--warn-soft); color: var(--warn); }
.chip-bad { background: var(--bad-soft); color: var(--bad); }
.chip-accent { background: var(--accent-soft); color: var(--accent); }
.state { font-family: var(--mono); font-size: .85rem; }
.state-running { color: var(--ok); }
.state-process_failed, .state-stopped { color: var(--bad); }
.state-source_unavailable, .state-unknown, .state-not_started { color: var(--warn); }
.reason, .muted { color: var(--muted); }
.reason { font-size: .85rem; }
.empty { color: var(--faint); font-style: italic; }
.facts { color: var(--muted); font-size: .85rem; display: flex; flex-wrap: wrap; gap: .25rem 1rem; }
.facts b { color: var(--ink); font-weight: 600; font-family: var(--mono); font-size: .95em; }
.problems { margin: 0; padding: 0; list-style: none; display: grid; gap: .3rem; }
.problems li { padding-left: .9rem; position: relative; color: var(--ink); }
.problems li::before { content: ""; position: absolute; left: 0; top: .55em; width: .4rem; height: .4rem; border-radius: 50%; background: var(--warn); }
.unavailable { border-left: 3px solid var(--warn); background: var(--warn-soft); padding: .5rem .75rem; border-radius: 0 4px 4px 0; margin: 0; }
.unavailable b { color: var(--warn); }

/* tables */
table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: .4rem .6rem .4rem 0; vertical-align: top; }
th { font-weight: 600; color: var(--muted); font-size: .74rem; text-transform: uppercase; letter-spacing: .05em; border-bottom: 1px solid var(--line); }
tr + tr td { border-top: 1px solid var(--line); }
td:last-child, th:last-child { padding-right: 0; }
.scroll { overflow-x: auto; }
table.kv th { text-transform: none; letter-spacing: 0; font-size: .85rem; border-bottom: 0; width: 11rem; color: var(--muted); font-weight: 500; }
table.kv tr + tr th { border-top: 1px solid var(--line); }
.feed td:first-child { white-space: nowrap; color: var(--muted); }
.feed .actor { color: var(--muted); font-size: .85rem; }
.outcome-success { color: var(--ok); }
.outcome-failure, .outcome-refused, .outcome-error { color: var(--bad); }

/* sprints */
.sprint-card { border: 1px solid var(--line); border-radius: 6px; background: var(--surface); }
.sprint-card > header { display: flex; align-items: center; gap: .6rem; flex-wrap: wrap; padding: .6rem .9rem; border-bottom: 1px solid var(--line); }
.sprint-card > header h3 a { color: var(--ink); font-family: var(--mono); font-size: .95rem; }
.sprint-card > .body { padding: .6rem .9rem .75rem; }
.sprint-card .goal { margin: 0 0 .6rem; max-width: 65ch; }
.budget { display: inline-block; width: 7rem; height: .45rem; border: 1px solid var(--line-strong); border-radius: 999px; vertical-align: middle; overflow: hidden; background: var(--raised); }
.budget i { display: block; height: 100%; background: var(--ok); }
.budget.signal i { background: var(--warn); }
.budget.hard i { background: var(--bad); }
.hero { display: flex; align-items: flex-start; gap: 1rem; flex-wrap: wrap; margin-bottom: 1rem; }
.hero h1 { font-family: var(--mono); font-weight: 600; }
.hero .title { font-size: 1.05rem; color: var(--ink); flex-basis: 100%; max-width: 70ch; }
.hero .chips { display: flex; gap: .4rem; flex-wrap: wrap; align-items: center; }
.hero .age + .chips { flex-basis: 100%; }

/* the sprint page */
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(12rem, 1fr)); gap: .6rem; }
.tile { display: grid; gap: .2rem; align-content: start; padding: .55rem .7rem; border: 1px solid var(--line); border-radius: 6px; background: var(--ground); }
.tile .label, .call .label, .because .label { font-size: .7rem; font-weight: 600; text-transform: uppercase; letter-spacing: .06em; color: var(--faint); }
.tile .reason { font-size: .8rem; }
.tile-ok { background: var(--ok-soft); border-color: transparent; } .tile-ok b { color: var(--ok); }
.tile-warn { background: var(--warn-soft); border-color: transparent; } .tile-warn b { color: var(--warn); }
.tile-bad { background: var(--bad-soft); border-color: transparent; } .tile-bad b { color: var(--bad); }
.call { display: grid; grid-template-columns: 6.5rem minmax(0, 1fr); gap: .9rem; padding: .55rem 0; }
.call + .call { border-top: 1px solid var(--line); }
.call .label { padding-top: .2rem; }
details.reasons > summary { list-style: none; cursor: pointer; margin-top: .3rem; color: var(--accent); font-size: .8rem; font-weight: 500; }
details.reasons > summary::-webkit-details-marker { display: none; }
.because { display: grid; gap: .15rem; margin-top: .5rem; color: var(--muted); }
details.more-actions { margin-top: .75rem; border-top: 1px solid var(--line); padding-top: .6rem; }
details.more-actions > summary { cursor: pointer; color: var(--muted); font-size: .85rem; }
details.more-actions form.act { margin-top: .6rem; }
ul.refs { margin: 0; padding: 0; list-style: none; display: grid; grid-template-columns: repeat(auto-fill, minmax(16rem, 1fr)); gap: .35rem; }

/* the card page */
.doc { overflow-wrap: anywhere; min-width: 0; line-height: 1.6; }
.doc > :first-child { margin-top: 0; }
.doc p, .doc ul, .doc ol, .doc blockquote, .doc pre { margin: .45rem 0; }
.doc h3, .doc h4, .doc h5, .doc h6 { margin: 1rem 0 .3rem; font-size: 1rem; font-weight: 600; }
.doc ul, .doc ol { padding-left: 1.4rem; }
.doc li + li { margin-top: .25rem; }
.doc code { background: var(--raised); border-radius: 3px; padding: 0 .3em; }
.doc pre { white-space: pre; overflow-x: auto; background: var(--ground); border: 1px solid var(--line); border-radius: 4px; padding: .5rem .7rem; }
.doc pre code { background: none; padding: 0; }
.doc blockquote { border-left: 3px solid var(--line-strong); padding-left: .7rem; color: var(--muted); margin-inline: 0; }
.task-text { max-width: 80ch; font-size: .95rem; }
.heads.stacked { grid-template-columns: minmax(0, 1fr); gap: .35rem; }
.head-facts { padding: 0 .1rem .4rem 1.2rem; font-size: .78rem; color: var(--muted); }
.head-runs { margin: 0 0 .5rem; padding: 0 0 0 1.2rem; list-style: none; font-size: .78rem; color: var(--muted); }
.head-runs li { padding: .15rem 0; border-top: 1px dashed var(--line); }

/* forms and actions */
form { margin: 0; }
form.inline { display: flex; flex-wrap: wrap; gap: .5rem; align-items: flex-end; }
label { display: block; font-size: .78rem; color: var(--muted); margin-bottom: .15rem; }
input, select, textarea { font: inherit; color: var(--ink); background: var(--surface); border: 1px solid var(--line-strong); border-radius: 4px; padding: .35rem .5rem; max-width: 100%; }
textarea { width: 100%; min-height: 4rem; resize: vertical; }
button { font: inherit; font-weight: 600; cursor: pointer; border-radius: 4px; padding: .38rem .8rem; border: 1px solid var(--accent); background: var(--accent); color: var(--accent-ink); }
button.quiet { background: transparent; color: var(--accent); }
button.danger { background: transparent; border-color: var(--warn); color: var(--warn); }
button:hover { filter: brightness(1.08); }
form.act { display: block; }
form.act + form.act { border-top: 1px solid var(--line); padding-top: .75rem; margin-top: .75rem; }
form.act .row { display: flex; flex-wrap: wrap; gap: .6rem; align-items: flex-end; margin-top: .5rem; }
form.act .row label { margin: 0; display: flex; align-items: center; gap: .35rem; color: var(--ink); font-size: .85rem; }
form.act .hint { color: var(--faint); font-size: .78rem; margin: .4rem 0 0; }
.feedback:not(:empty) { border-left: 3px solid var(--accent); background: var(--accent-soft); padding: .4rem .75rem; margin-top: .5rem; border-radius: 0 4px 4px 0; font-size: .9rem; }
.feedback.bad { border-left-color: var(--bad); background: var(--bad-soft); }
#feedback:not(:empty) { border-left: 3px solid var(--accent); background: var(--accent-soft); padding: .4rem .75rem; margin-top: .5rem; }
#feedback.bad { border-left-color: var(--bad); background: var(--bad-soft); }
.refresh { display: flex; align-items: center; gap: .35rem; font-size: .8rem; color: var(--muted); margin-left: auto; }
.refresh input { margin: 0; }
.filters { display: flex; gap: .25rem; flex-wrap: wrap; }
.filters a { padding: .2rem .6rem; border-radius: 999px; border: 1px solid var(--line); color: var(--muted); font-size: .85rem; }
.filters a[aria-current="true"] { background: var(--accent-soft); color: var(--accent); border-color: transparent; }
.filters a:hover { text-decoration: none; color: var(--ink); }

/* long text: one copy of it, in the reading face, held to two lines until it is opened */
.prose { white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.55; }
details.text > summary { list-style: none; cursor: pointer; color: var(--ink); }
details.text > summary::-webkit-details-marker { display: none; }
details.text > summary .prose { display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; line-clamp: 2; overflow: hidden; }
details.text[open] > summary .prose { display: block; -webkit-line-clamp: unset; line-clamp: none; }
details.text > summary::after { content: "show more"; display: block; margin-top: .1rem; color: var(--accent); font-size: .8rem; font-weight: 500; }
details.text[open] > summary::after { content: "show less"; }

/* tabs: a radio per tab, so the page works with no script and a reload keeps nothing hidden */
.tabs { position: relative; }
.tabs > input { position: absolute; opacity: 0; pointer-events: none; }
.tabs > .tab-bar { display: flex; gap: 1.4rem; padding: 0 .9rem; border-bottom: 1px solid var(--line); overflow-x: auto; }
.tabs > .tab-bar label { margin: 0; padding: .65rem 0; border-bottom: 2px solid transparent; color: var(--muted); font-size: .9rem; font-weight: 500; white-space: nowrap; cursor: pointer; }
.tabs > .tab-bar label:hover { color: var(--ink); }
.tabs > .tab-bar .count { margin-left: .35rem; padding: 0 .4rem; border-radius: 999px; background: var(--raised); font: .72rem var(--mono); color: var(--muted); }
.tabs > .tab-panel { display: none; padding: .75rem .9rem; min-width: 0; }
.tabs > .tab-panel > * + * { margin-top: .6rem; }
TAB_RULES

/* a head: who runs a role, on which model, how hard it thinks, and whether it is alive */
.heads { display: grid; grid-template-columns: repeat(auto-fit, minmax(13rem, 1fr)); gap: .6rem; }
.head { display: flex; align-items: center; gap: .6rem; padding: .5rem .7rem; border: 1px solid var(--line); border-radius: 6px; background: var(--ground); min-width: 0; }
.head.idle { border-style: dashed; }
.head .who { display: grid; min-width: 0; }
.head .role { font-size: .74rem; color: var(--faint); }
.head .model { font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.head .effort { margin-left: auto; display: grid; justify-items: end; gap: .15rem; font-size: .72rem; color: var(--muted); white-space: nowrap; }
.pulse { width: .5rem; height: .5rem; border-radius: 50%; flex: none; background: var(--faint); }
.pulse.live { background: var(--ok); box-shadow: 0 0 0 3px var(--ok-soft); }
.pulse.lost { background: var(--bad); box-shadow: 0 0 0 3px var(--bad-soft); }
.segs { display: inline-flex; gap: 2px; }
.segs i { width: 5px; height: 9px; border-radius: 2px; background: var(--line-strong); }
.segs i.on { background: var(--accent); }
.segs.unset i { background: transparent; border: 1px solid var(--line-strong); }
.head-chip { display: inline-flex; align-items: center; gap: .4rem; padding: .1rem .5rem; border: 1px solid var(--line); border-radius: 4px; background: var(--ground); font-size: .78rem; white-space: nowrap; }
.head-chip .role { color: var(--faint); }
.lead .head-chip { align-self: center; }
.effort-cell { display: inline-flex; align-items: center; gap: .4rem; font-size: .8rem; color: var(--muted); white-space: nowrap; }

/* the transition timeline */
ol.timeline { list-style: none; margin: 0; padding: 0; }
ol.timeline > li { border-top: 1px solid var(--line); }
ol.timeline > li:first-child { border-top: 0; }
ol.timeline details > summary { list-style: none; cursor: pointer; display: grid; grid-template-columns: 5.2rem 1fr; gap: .6rem; padding: .35rem 0; align-items: baseline; }
ol.timeline details > summary::-webkit-details-marker { display: none; }
ol.timeline details > summary:hover { background: var(--raised); }
ol.timeline .arrow { color: var(--faint); margin: 0 .2rem; }
ol.timeline .why { color: var(--muted); font-size: .85rem; display: block; }
ol.timeline .detail { padding: .3rem 0 .6rem 5.8rem; display: grid; gap: .4rem; }
ol.timeline .record { border-left: 2px solid var(--line-strong); padding-left: .6rem; }
ol.timeline .record .who { font-size: .8rem; color: var(--muted); }
@media (max-width: 600px) { ol.timeline .detail { padding-left: 0; } }

/* events */
ol.events { list-style: none; padding: 0; margin: 0; }
ol.events li { display: grid; grid-template-columns: 11.5rem 1fr; gap: .6rem; padding: .35rem 0; border-top: 1px solid var(--line); }
ol.events li:first-child { border-top: 0; }
ol.events time { color: var(--muted); }
ol.events b { font-weight: 600; }

/* the sprint form */
form.sprint { display: block; max-width: 48rem; }
form.sprint .field { margin: .9rem 0; }
form.sprint textarea { min-height: 4.5rem; }
form.sprint select { min-width: 22rem; max-width: 100%; }
form.sprint .choices { border: 1px solid var(--line); border-radius: 4px; padding: .4rem .6rem; max-height: 14rem; overflow-y: auto; }
form.sprint .choices label { display: block; font-size: inherit; color: inherit; padding: .1rem 0; }
form.sprint .hint { color: var(--muted); font-size: .8rem; }
.bad-field { color: var(--bad); font-size: .85rem; }
.refused, .pending { border-left: 3px solid var(--warn); background: var(--warn-soft); padding: .5rem .75rem; margin: .5rem 0; border-radius: 0 4px 4px 0; }
.launch { font-weight: 600; }

/* the shared bottom bar: what each provider has left, on every page.
   Its height is reserved on the body rather than overlaid, so nothing a page draws -- the /po
   composer least of all -- ends up underneath it. The row never wraps: it scrolls sideways
   instead, which is what keeps the reserved height true at phone width as well as at desktop. */
:root { --bar-height: 2.4rem; }
body { padding-bottom: var(--bar-height); }
.statusbar { position: fixed; left: 0; right: 0; bottom: 0; z-index: 6; height: var(--bar-height); background: var(--surface); border-top: 1px solid var(--line); }
.statusbar .row { max-width: 1280px; margin: 0 auto; padding: 0 20px; height: 100%; display: flex; align-items: center; gap: 1.1rem; white-space: nowrap; overflow-x: auto; font-size: .78rem; color: var(--muted); }
.statusbar .lamp { display: inline-flex; align-items: center; gap: .4rem; padding: .1rem .55rem; border-radius: 999px; font-weight: 600; border: 1px solid transparent; }
.statusbar .lamp::before { content: ""; width: .5rem; height: .5rem; border-radius: 50%; background: currentColor; }
.statusbar .lamp:hover { text-decoration: none; filter: brightness(1.08); }
.lamp-green { color: var(--ok); background: var(--ok-soft); }
.lamp-yellow { color: var(--warn); background: var(--warn-soft); }
.lamp-red { color: var(--bad); background: var(--bad-soft); }
.lamp-unknown { color: var(--muted); background: var(--surface); }
/* One provider is one group: a heading set apart from its windows the way a panel's heading is
   (uppercase and tracked, like h2), a rule between one provider and the next, and each window a
   chip of its own so the eye never has to guess where a window ends and the next one begins. */
.statusbar .provider { display: inline-flex; align-items: baseline; gap: .45rem; }
.statusbar .provider + .provider { border-left: 1px solid var(--line-strong); padding-left: 1.1rem; }
.statusbar .provider > b { color: var(--ink); font-weight: 600; font-size: .72rem; text-transform: uppercase; letter-spacing: .06em; }
.statusbar .window { display: inline-flex; align-items: baseline; gap: .35rem; font-family: var(--mono); color: var(--ink); background: var(--raised); border-radius: 999px; padding: .05rem .5rem; }
.statusbar .window .win-name { color: var(--muted); }
/* The chips sit side by side on one line, so a fixed column for the percentage only opens a hole
   between a window's name and its figure: the figure follows the name at the chip's own gap. */
.statusbar .window > b { font-variant-numeric: tabular-nums; }
.statusbar .window > b.stale { color: var(--warn); font-weight: 500; }
.statusbar .window .dot { color: var(--faint); }
.resets { color: var(--muted); font-variant-numeric: tabular-nums; }
/* The Codex reset credits fold into one chip-sized label; the details (expiry, whether a reset
   applies, the button) open above the bar, so the bar itself keeps one chip per fact. */
.statusbar details.credits { position: relative; display: inline-block; }
.statusbar details.credits > summary { list-style: none; cursor: pointer; font-family: var(--mono); font-size: inherit; color: var(--muted); background: var(--raised); border: 1px dashed var(--line-strong); border-radius: 999px; padding: .05rem .5rem; }
.statusbar details.credits > summary::-webkit-details-marker { display: none; }
.statusbar details.credits > summary:hover, .statusbar details.credits[open] > summary { color: var(--ink); border-style: solid; }
.statusbar .credits-pop { position: absolute; bottom: calc(100% + .5rem); left: 0; z-index: 7; display: grid; gap: .35rem; min-width: 18rem; max-width: 26rem; padding: .6rem .8rem; background: var(--surface); border: 1px solid var(--line-strong); border-radius: 6px; box-shadow: 0 6px 24px rgba(0,0,0,.18); white-space: normal; font-family: var(--sans, inherit); color: var(--ink); }
.statusbar .credits-pop .pop-line { display: block; }
.statusbar .credits-pop .pop-line.expires { color: var(--muted); font-variant-numeric: tabular-nums; }
.statusbar .credits-pop .pop-line.warn { color: var(--warn); }
.statusbar .credits-pop .pop-line.applies { color: var(--ok); }
.statusbar .reason, .statusbar .age { font-size: inherit; }
.statusbar .bar-reset { font: inherit; font-size: .78rem; padding: .15rem .7rem; line-height: 1.4; border-radius: 999px; justify-self: start; }
.statusbar .bar-reset:disabled { opacity: .55; cursor: not-allowed; }
.statusbar .bar-feedback:empty { display: none; }
.statusbar .bar-feedback { color: var(--ink); white-space: normal; }
.statusbar .bar-feedback.bad { color: var(--bad); }
.statusbar .bar-refresh { display: inline-flex; align-items: center; gap: .3rem; margin: 0 0 0 auto; font-size: inherit; color: var(--muted); }
.statusbar .bar-refresh input { margin: 0; }
@media (max-width: 600px) { .statusbar .row { padding: 0 12px; gap: .8rem; } }
@media (prefers-reduced-motion: no-preference) { .light::before { transition: background .2s; } }
"""

#: How many tabs one strip may hold. The stylesheet has no counter for "the n-th radio shows the
#: n-th panel", so the rule is written out once per position.
MAX_TABS = 6

STYLE = STYLE.replace(
    "TAB_RULES",
    "\n".join(
        f".tabs > input:nth-of-type({n}):checked ~ .tab-bar label:nth-of-type({n}) "
        "{ color: var(--ink); border-bottom-color: var(--accent); }\n"
        f".tabs > input:nth-of-type({n}):focus-visible ~ .tab-bar label:nth-of-type({n}) "
        "{ outline: 2px solid var(--accent); outline-offset: 2px; }\n"
        f".tabs > input:nth-of-type({n}):checked ~ .tab-panel:nth-of-type({n}) {{ display: block; }}"
        for n in range(1, MAX_TABS + 1)
    ),
)


# -- the shell ----------------------------------------------------------------------------------


#: The primary navigation: where a person can go from anywhere. Keys are what a page names itself
#: as, so the current one is marked; the order is the order of use.
NAV: tuple[tuple[str, str, str], ...] = (
    ("dashboard", "/", "Dashboard"),
    ("sprints", "/sprints", "Sprints"),
    ("projects", "/projects", "Projects"),
    ("po", "/po", "PO"),
)

FONTS = "https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap"


# -- the bottom status bar ------------------------------------------------------------------------
#
# The bar belongs to the shell, not to any page: it is rendered once, inside :func:`_page`, so a
# page function added tomorrow gets it without knowing it exists and cannot grow a provider read of
# its own. What it draws is handed in by the transport, which is the only thing here that may talk
# to a layer -- and it is handed in *lazily*, as a callable, so a JSON route that never renders a
# page never causes a provider read at all.
#
# The value is a context variable and not module state: it is set for the span of one request and
# reset when that span ends, so two requests answered on two threads never see each other's.

#: The read the bar draws, for the span of one request: a `{"available", "reason", "document"}`
#: section, or `None` when this process was built without the provider usage layer.
_LIMITS_SOURCE: ContextVar[Callable[[], dict[str, Any] | None] | None] = ContextVar(
    "ummanu.web.limits_source", default=None
)

#: The doctor reading the lamp draws, for the span of one request: a `{"available", "reason",
#: "document"}` section, or `None` when this process was built without the doctor layer. Fed the
#: same way and for the same reason as the limits: lazily, per request, and never module state.
_DOCTOR_SOURCE: ContextVar[Callable[[], dict[str, Any] | None] | None] = ContextVar(
    "ummanu.web.doctor_source", default=None
)

#: The bell's reading for the span of one request: a `{"state", "reason", "count"}` document from the
#: owner events layer, or `None` when this process was built without it. Fed like the doctor lamp:
#: lazily, per request, never module state, so the count is the board's at the moment of the render.
_BELL_SOURCE: ContextVar[Callable[[], dict[str, Any] | None] | None] = ContextVar(
    "ummanu.web.bell_source", default=None
)

#: Said when nothing fed the bar: no provider layer was built into this process at all.
LIMITS_NOT_BUILT = "this web process was built without the provider usage layer"
#: Said when the reading came back but carries nothing about this provider.
LIMITS_NOT_IN_READING = "this reading carried nothing about this provider"

#: The providers the bar always keeps a place for, in this order, whatever a reading holds.
BAR_PROVIDERS: tuple[tuple[str, str], ...] = (("claude", "Claude"), ("codex", "Codex"))

#: The clock a countdown on a page is measured against, for the span of one render. Unset -- which
#: is every real request -- means this host's wall clock; a test sets it to render deterministically.
_RENDER_CLOCK: ContextVar[Callable[[], datetime] | None] = ContextVar(
    "ummanu.web.render_clock", default=None
)

#: Whether the page being rendered is the answer to a POST, for the span of one request. A page
#: reached that way must not reload itself: the browser would offer to send the submission again.
_FROM_POST: ContextVar[bool] = ContextVar("ummanu.web.from_post", default=False)


@contextmanager
def limits_source(read: Callable[[], dict[str, Any] | None] | None) -> Iterator[None]:
    """Feed the bottom bar for the span of one request, and stop feeding it when that span ends."""
    token = _LIMITS_SOURCE.set(read)
    try:
        yield
    finally:
        _LIMITS_SOURCE.reset(token)


@contextmanager
def doctor_source(read: Callable[[], dict[str, Any] | None] | None) -> Iterator[None]:
    """Feed the doctor lamp for the span of one request, and stop feeding it when that span ends."""
    token = _DOCTOR_SOURCE.set(read)
    try:
        yield
    finally:
        _DOCTOR_SOURCE.reset(token)


@contextmanager
def bell_source(read: Callable[[], dict[str, Any] | None] | None) -> Iterator[None]:
    """Feed the header bell for the span of one request, and stop feeding it when that span ends."""
    token = _BELL_SOURCE.set(read)
    try:
        yield
    finally:
        _BELL_SOURCE.reset(token)


def _bell() -> str:
    """The bell in the header: the unread owner events, or `?` when the board could not count them."""
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


@contextmanager
def render_clock(now: Callable[[], datetime] | None) -> Iterator[None]:
    """Fix the clock this render measures a countdown against, so a test can assert one exactly."""
    token = _RENDER_CLOCK.set(now)
    try:
        yield
    finally:
        _RENDER_CLOCK.reset(token)


@contextmanager
def from_post(value: bool) -> Iterator[None]:
    """Mark the span of one request as answering a POST, so its pages carry no auto-reload."""
    token = _FROM_POST.set(value)
    try:
        yield
    finally:
        _FROM_POST.reset(token)


def _limits_bar() -> str:
    """The bar, from whatever the transport is feeding it -- which may be nothing at all."""
    read = _LIMITS_SOURCE.get()
    doctor_read = _DOCTOR_SOURCE.get()
    return _limits_bar_of(
        read() if read is not None else None,
        doctor=doctor_read() if doctor_read is not None else None,
    )


def _limits_bar_of(section: dict[str, Any] | None, *, doctor: dict[str, Any] | None = None) -> str:
    """The bar for one section, kept apart from where the section comes from so a test can hand one in.

    Three things are never confused here, in the same way every section of a page keeps them
    apart: a current reading, a reading that is not current, and no reading at all. Only the
    first draws a percentage, because a number on a bar is read as what is left *now*.
    """
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
    """The lamp: one colour out of the recorded health, and a link to the problems behind it.

    It is a link from every page and not a panel on one, so the colour is never a dead end: what
    makes it red is one click away wherever a person happens to be.
    """
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
    """One usage window's chip: its name, what is left, and the time to its next reset.

    A reading whose reset has come and gone describes a window that no longer runs, so its
    percentage is drawn as stale rather than as a figure somebody would read as what is left now.
    """
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
    """The Codex rate-limit reset credits a reading carries, folded into one small label.

    The bar shows only the count (`1 reset`), a `<details>` summary the reader opens for the rest:
    the nearest expiry, whether the provider counts a reset as applicable right now, and the button
    that spends one. Nothing at all when the reading carries no credits or none are available: a label
    saying zero would be read as a limit, and there is no credit to spend. Every value is read through
    the same normalisers the layer wrote it with, so a hand-made document cannot make the bar fail.
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
#: The provider does not (`applicable_available_count` zero or unknown). The rule behind that count is
#: the provider's and undocumented; the button stays, and the provider's own answer decides.
RESET_NOT_APPLICABLE = (
    "the provider does not count a reset as applicable now: a press may answer nothing to reset, "
    "or refill windows that still have usage left"
)


def _bar_reset_button(credits: Any) -> str:
    """The button that spends one Codex reset credit, drawn inside the credits popover.

    No button when the reading carries no credits or none is available -- a fallback reading never
    carries any. Enabled whenever one is available: whether a reset applies is the provider's call,
    made when the consume is sent, and its answer is shown in words. The click asks the person first,
    naming what it spends; the operation behind it re-reads the provider and refuses with no credit
    anyway, so a stale page cannot spend a credit this markup offered.
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
    """The stand-in for a percentage. It is words, never a number: no reading is not a low reading."""
    return f'<span class="reason">no current reading — {escape(reason)}</span>'


def _page(
    title: str,
    body: str,
    *,
    script: str = "",
    nav: str = "",
    crumbs: tuple[tuple[str, str], ...] = (),
) -> str:
    """The shell every page shares: the top bar with the product, the navigation and the crumbs.

    `nav` names the primary entry this page belongs to, and the navigation marks it; `crumbs` is
    what is open under it -- normally one identifier, the card or the sprint -- shown beside the
    navigation and never repeating it.
    """
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
            '<link rel="preconnect" href="https://fonts.googleapis.com">',
            f'<link rel="stylesheet" href="{FONTS}">',
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
            # The refresh is the shell's, like the bar it keeps current, so every page has it and
            # every page obeys the one rule: nothing reloads while a form holds typed text. One
            # page never carries it at all: the answer to a POST, where a reload is the browser
            # re-sending the submission and asking the reader to confirm it.
            f"<script>{script}{'' if _FROM_POST.get() else _REFRESH_SCRIPT}{_RESET_SCRIPT}</script>",
            "</body></html>",
        ]
    )


def _panel(title: str, body: str, *, count: Any = None, more: str = "", open_: bool | None = None) -> str:
    """One surface for one subject: a header naming it, a count when there is one, and the body.

    `open_` turns the panel into a disclosure that starts open or closed; `None` is a plain panel.
    """
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


#: The tone a card state reads in. Semantic colour beside the state's own word, never instead.
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


# -- source rendering ---------------------------------------------------------------------------


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
    """One section: the source's refusal if it refused, then the rows, or the empty line.

    Both are rendered when a source refused and stale rows are still worth showing, and the two are
    never confused: the refusal is above the rows, so nobody reads an old list as a current one.
    """
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


#: Said where a countdown would be when the reading carries no reset moment, or one this process
#: cannot read. It is words, for the same reason a missing percentage is: nothing is not zero.
NO_RESET_RECORDED = "no reset time recorded"


def _reset_moment(value: Any) -> datetime | None:
    """The moment a reading calls a reset, or `None` when it recorded none this process can read."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    # `provider_usage._iso` always writes UTC; a moment without an offset is read as UTC rather
    # than as this host's local time, which would silently shift the countdown by the offset.
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


#: The hover title of a percentage that is not drawn, because the window it measured has reset since.
READING_PREDATES_RESET = "this reading predates the last reset of its window, so what is left now is unknown"

#: The longest window a reset is rolled forward by. A longer one is no usage window this bar knows,
#: and is treated as no window length at all rather than counted down over centuries.
LONGEST_WINDOW_MINUTES = 366 * 24 * 60


def _time_left(seconds: float) -> str:
    """A reset as the time left until it: see :func:`_duration`."""
    return f"{_duration(seconds)} left"


def _duration(seconds: float) -> str:
    """A span still ahead, in the one spelling this module uses for every countdown.

    It is only ever asked about a moment still ahead -- `_reset_reading` rolls a past one forward
    first -- so anything under a minute, a rounded-down zero included, is less than a minute.
    """
    total = int(seconds)
    if total < 60:
        return "less than a minute"
    # A unit belongs to the number in front of it: `1h 6m`, never `1 h 6 m`, where the spaces make
    # four things out of two and the reader has to pair them up again.
    minutes = total // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def _render_now() -> datetime:
    """The clock of this render, as an aware moment: every countdown is measured from it."""
    clock = _RENDER_CLOCK.get()
    now = clock() if clock is not None else datetime.now(UTC)
    return now if now.tzinfo is not None else now.replace(tzinfo=UTC)


def _window_length(value: Any) -> int | None:
    """A window's length in minutes when it is one a reset can be rolled forward by, else `None`."""
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return value if 0 < value <= LONGEST_WINDOW_MINUTES else None


def _reset(resets_at: Any, window_minutes: Any = None) -> str:
    """A usage window's reset as the time left until it; see :func:`_reset_reading`."""
    return _reset_reading(resets_at, window_minutes)[0]


def _reset_reading(resets_at: Any, window_minutes: Any = None) -> tuple[str, bool]:
    """A usage window's reset, and whether the reading predates it: the one place both are decided.

    What is shown is how long is left, because that is what a reader of a usage window wants and
    an ISO moment is not it. The moment is not lost: it is the element's hover title, wherever
    there is one to carry -- a reading with no moment carries no title at all rather than a
    misleading one.

    The countdown is measured against the clock of *this render* and never against the reading's
    `observed_at`. The reading is served from a cache that may be up to `CACHE_SECONDS` old, so
    counting from when it was observed would keep showing the time that was left then and overstate
    what is left now; the page is drawn now, so now is what it counts from.

    A reset at or before now has happened, so the window running now resets whole window lengths
    later: the moment is rolled forward to the first of those still ahead, and the second value is
    true because the reading's percentage belongs to the window before. The number of lengths is
    divided out, never stepped, so a far-past moment costs the same as a recent one. A past reset
    with no usable window length cannot be rolled, and is said as no reset recorded.
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
    """A usage window's percentage, drawn once for both the bar and the dashboard panel.

    The reading is rounded to a tenth by the layer, and a tenth that is zero is noise beside a
    countdown: it is drawn as `73%`. A reading that really is fractional keeps its one digit rather
    than being rounded away here, because the layer's precision is not this module's to drop.
    A value that is no number at all is a dash and never a `0%`, for the reason a missing countdown
    is words: nothing is not zero.
    """
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


def _link(ref: str) -> str:
    return f'<a class="ref" href="/tasks/{quote(ref)}">{escape(ref)}</a>'


def _or_dash(value: Any) -> str:
    return escape(str(value)) if value not in (None, "") else "—"


# -- the dashboard ------------------------------------------------------------------------------


def dashboard(
    snapshot: dict[str, Any],
    *,
    pause: dict[str, Any] | None = None,
    sprints: dict[str, Any] | None = None,
    limits: dict[str, Any] | None = None,
    po: dict[str, Any] | None = None,
) -> str:
    """The operator's one screen: the pipeline's state, the open sprints, what is in flight, and
    what happened last.

    Three of the four sections come from reads beside the snapshot, each handed in as
    ``{"available", "reason", "document"}`` by the transport; one that is not available is drawn
    as the marked block every unreadable source gets, and never as an empty section.
    """
    installation = snapshot.get("installation") or {}
    open_items = _sprint_items(sprints)
    # Each fact is drawn once. The usage windows are the bottom bar's and the lamp is the doctor's
    # colour on every page, so neither has a panel here; what this page adds is the problem itself,
    # and only while there is one. `limits` is still accepted: the transport hands every page the
    # same reads, and a caller is not told to stop reading what the bar draws.
    del limits
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


# -- the pipeline bar -----------------------------------------------------------------------------

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
    """The first thing on the screen: is the pipeline running, and how to stop it.

    Health is not repeated here: the lamp on the bottom bar is its colour on every page, and the
    dashboard names the problem itself under this strip while there is one. `installation` is kept
    for the refused branch, which has nothing else to show.
    """
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
        # Draining is rare and stops the pipeline, so it is one click further away than reading.
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
    """The installation's problem, named under the strip while there is one, and nothing otherwise.

    The lamp on the bottom bar already says the colour on every page; this is the sentence behind
    it, where the operator is looking. Health that could not be read is not silence: it is the
    marked block every unreadable source gets. The facts behind the verdict -- checkpoint, disk,
    memory, load -- stay one click away in the collapsed installation panel.
    """
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
    """Health as the read layer summarizes it: the problems by name, then the facts.

    Whether the source answered at all is said once, above the panel, by :func:`_attention`.
    """
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
    if problems:
        combined = health.get("combined") or {}
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


# -- the open sprints -----------------------------------------------------------------------------


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
    """One open sprint as the dashboard shows it: the goal, the card in hand, who works it, the spend."""
    ref = str(item.get("ref") or "")
    projects = item.get("projects") if isinstance(item.get("projects"), list) else []
    project = ", ".join(str(value) for value in projects) or str(item.get("product") or "—")
    human_wait = item.get("attention") if isinstance(item.get("attention"), dict) else {}
    attention = _chip("attention required", "warn") if human_wait.get("state") == "waiting" and human_wait.get("event_ids") else ""
    budget = item.get("budget") if isinstance(item.get("budget"), dict) else {}
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
            _budget_line(budget) if budget else "",
            "</article>",
        ]
    )


#: The tone a gate state reads in, beside its own word.
GATE_TONES: dict[str, str] = {"green": "ok", "red": "bad", "pending": "", "running": "accent"}


def _current_card_box(item: dict[str, Any]) -> str:
    """The card a sprint is on: where it stands, whether its gate passed, and what it is called."""
    current = item.get("current_task") if isinstance(item.get("current_task"), dict) else {}
    if isinstance(item.get("current_task"), str) and item.get("current_task"):
        current = {"ref": item["current_task"], "live": None}
    ref = str(current.get("ref") or "")
    if not ref:
        said = str(current.get("reason") or "the observer has cut no card for this sprint yet")
        return f'<div class="card-box"><span class="empty">{escape(said)}</span></div>'
    standing = item.get("current_card_state") if isinstance(item.get("current_card_state"), dict) else {}
    checks = item.get("checks") if isinstance(item.get("checks"), dict) else {}
    gate = checks.get("gate") if isinstance(checks.get("gate"), dict) else {}
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
    """The heads a sprint runs on -- observer, worker, reviewer -- each with its model and effort.

    They are the read layer's `head_profiles`, joined against the registry there and not here. A
    role that section leaves unset (a worker or reviewer the dispatcher picks per card) is not drawn:
    its model is a card's fact, on the card page, and not one of the sprint's.
    """
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
    """Each role's profile with its model and effort, and for the observer whether it is up.

    Whether the observer is up is the launch's to say, laid over the profile, never the reverse.
    """
    profiles = item.get("head_profiles") if isinstance(item.get("head_profiles"), dict) else {}
    heads = {
        role: dict(profiles[role])
        for role in ("observer", "worker", "reviewer")
        if isinstance(profiles.get(role), dict) and profiles[role].get("profile")
    }
    observer = item.get("observer") if isinstance(item.get("observer"), dict) else {}
    launch = observer.get("launch") if isinstance(observer.get("launch"), dict) else {}
    if "observer" in heads and launch.get("state"):
        heads["observer"]["state"] = launch["state"]
    return heads


def _sprint_status_chip(item: dict[str, Any]) -> str:
    status = str(item.get("status") or "")
    return _chip(status or "unknown", {"open": "ok", "closed": "", "stopped": "bad"}.get(status, ""))


def _waiting_chip(item: dict[str, Any]) -> str:
    waiting = item.get("waiting") if isinstance(item.get("waiting"), dict) else {}
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
    """What a waiting sprint waits on, in words and with the card it points at.

    The chip carries the same reason only as a hover title; a sprint whose decision or operation card
    is with the PO, or handed to the owner, has to say so where it can be read at a glance.
    """
    waiting = item.get("waiting") if isinstance(item.get("waiting"), dict) else {}
    if waiting.get("state") != "waiting" or not waiting.get("reason"):
        return ""
    card = str(waiting.get("card") or "")
    pointer = f" {_link(card)}" if card else ""
    return f'<div class="reason">waiting: {escape(_short(waiting.get("reason"), 160))}{pointer}</div>'


#: Said where a duration would be when the committed audit dates no transition of the current card.
#: Words, and never a zero age: "0s" beside a board state reads as a card that moved as the page was
#: drawn, which is the opposite of a card nothing has moved at all.
NO_TRANSITION_RECORDED = "no transition recorded"


def _card_standing(item: dict[str, Any]) -> str:
    """Where a sprint's current card stands and how long it has stood there, drawn once.

    Every surface that shows it -- the row of `/sprints`, the dashboard's sprint card and the
    "Now" panel of `/sprints/{ref}` -- is this one function, so they cannot say it differently. The wording follows `_reset`: the
    duration is what a reader wants in the text, and the exact ISO moment is the hover title of the
    element carrying it. An answer with no moment carries no title at all rather than a misleading
    one, and a sprint that has ended carries no duration at all: its card is where the sprint
    stopped, not something that is still ageing.
    """
    carried = item.get("current_card_state")
    section = carried if isinstance(carried, dict) else {}
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


def _budget(budget: dict[str, Any]) -> str:
    total = int(budget.get("total") or 0)
    thresholds = budget.get("thresholds") or {}
    hard = int(thresholds.get("hard") or 0)
    signal = int(thresholds.get("signal") or 0)
    ratio = min(1.0, total / hard) if hard else 0.0
    colour = "hard" if budget.get("hard_reached") else ("signal" if budget.get("signal_reached") else "")
    by_type = budget.get("by_type") or {}
    spent = ", ".join(
        f"{escape(str(kind))} {escape(str(count))}" for kind, count in sorted(by_type.items()) if count
    )
    return (
        f'<span class="budget {colour}"><i style="width:{ratio * 100:.0f}%"></i></span> '
        f"{total} of {hard} (signal at {signal})"
        + (f' <span class="muted">— {spent}</span>' if spent else "")
    )


def _budget_line(budget: dict[str, Any]) -> str:
    """The card budget as a thin secondary line: how much of it is spent, and where the signal is.

    It is a spend, not progress -- a sprint that is going in circles fills it faster than one that
    is going well -- so it is drawn small and quiet, under whatever says how far the work got.
    """
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


# -- the command feed -----------------------------------------------------------------------------


def _feed(section: dict[str, Any] | None, *, compact: bool = False) -> str:
    document, refused = _beside(section, what="the last commands")
    if document is None:
        return refused
    return _feed_table(document, compact=compact)


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
        actor = item.get("actor") if isinstance(item.get("actor"), dict) else {}
        entity = item.get("entity") if isinstance(item.get("entity"), dict) else {}
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
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


#: How a severity is spoken on the doctor page, and the order the groups are read in: what makes
#: the lamp red first, because that is what the page is opened for.
SEVERITY_GROUPS: tuple[tuple[str, str], ...] = (
    ("red", "Red — the installation cannot be trusted to run work"),
    ("yellow", "Yellow — running, but a person should look"),
)


def doctor(section: dict[str, Any] | None) -> str:
    """The page behind the lamp: what is wrong, by code, grouped by what it does to the colour.

    Three answers and never two: problems, no problem at all, or health that could not be read --
    which is said as itself, with the reason, rather than drawn as an empty list. An unreadable
    installation showing "nothing is wrong" is the one failure this page exists to prevent.
    """
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
                "<br><code>" + escape(json.dumps({key: value for key, value in problem.items()
                                              if key not in {"code", "message", "severity", "source"}}, sort_keys=True)) + "</code>"
                if any(key not in {"code", "message", "severity", "source"} for key in problem) else ""
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
    """The bell's list: the unread by default, or every event, newest first, open `needs_owner` pinned.

    A notice is marked read by its own button, and "Mark all read" takes the notices only. A
    `needs_owner` event whose card still waits for the owner has no button: its card clears it.
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
        current = item.get("current_task") if isinstance(item.get("current_task"), dict) else {}
        observer = item.get("observer") if isinstance(item.get("observer"), dict) else {}
        launch = observer.get("launch") or {}
        budget = item.get("budget") if isinstance(item.get("budget"), dict) else {}
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


# -- the owner's actions --------------------------------------------------------------------------


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


#: The states a move may name, in the layer's spelling. Kept beside the form that offers them.
MOVE_TARGETS = ("ready", "in_progress", "done", "blocked", "issues", "validate", "assessment")


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
        summary = status.get("summary") if isinstance(status.get("summary"), dict) else {}
        overall = summary.get("state") or status.get("state") or "collected"
        parts.append(f'<p>health: <span class="state">{escape(str(overall))}</span></p>')
    elif (health.get("source") or {}).get("state") == "available":
        parts.append('<p class="empty">the health collector answered with nothing.</p>')
    return "\n".join(part for part in parts if part)


def _project_table(items: list[dict[str, Any]]) -> str:
    return _rows(
        ["project", "repo", "adapter", "branch", "enabled"],
        [
            [
                escape(str(item.get("id") or "")),
                _or_dash(item.get("repo")),
                _or_dash(item.get("adapter")),
                _or_dash(item.get("default_branch")),
                "yes" if item.get("enabled") else "no",
            ]
            for item in items
        ],
    )


def _task_table(items: list[dict[str, Any]]) -> str:
    return _rows(
        ["card", "state", "project", "title"],
        [
            [
                _link(str(item.get("ref") or "")),
                _state_chip(item.get("state")),
                _or_dash(item.get("project")),
                _or_dash(item.get("title")),
            ]
            for item in items
        ],
    )


def _start_form(projects: list[dict[str, Any]], tasks: list[dict[str, Any]]) -> str:
    if not projects:
        return '<p class="empty">no registered project, so there is nothing to start a run in.</p>'
    options = "".join(
        f'<option value="{escape(str(item.get("id")))}">{escape(str(item.get("id")))}</option>'
        for item in projects
    )
    cards = "".join(
        f'<option value="{escape(str(item.get("ref")))}" data-project="{escape(str(item.get("project") or ""))}">'
        f"{escape(str(item.get('ref')))} — {escape(str(item.get('title') or ''))}</option>"
        for item in tasks
    )
    return (
        '<form id="start-form" class="inline">'
        f'<div><label for="project">project</label><select id="project" name="project">{options}</select></div>'
        f'<div><label for="ref">card</label><select id="ref" name="ref">{cards}</select></div>'
        '<div><label for="profile">head profile</label>'
        '<input id="profile" name="profile" placeholder="a profile from the head registry" required></div>'
        '<div><label for="instruction">extra instruction</label>'
        '<input id="instruction" name="instruction" placeholder="optional"></div>'
        '<button type="submit">Start a worker run</button>'
        "</form>"
        '<p class="empty">a repeated submission of the same card and profile carries the same request '
        "id, and the operation behind it answers it with the run that already exists.</p>"
    )


# -- the task page ------------------------------------------------------------------------------


def task(snapshot: dict[str, Any], *, runs: dict[str, Any], sessions: dict[str, Any] | None = None) -> str:
    """Criterion 3: state, recent events, the worker's and reviewer's output, and the result.

    The card is read as a task first: its title is the heading and its full text -- what the
    observer asked for -- is the first tab, because that is what nobody could see before. Each fact
    is drawn once: the chips under the title are the card's state, and the side panel carries only
    what they do not.
    """
    ref = str(snapshot.get("ref") or "")
    card = snapshot.get("card") or {}
    value = card.get("value") or {}
    project = snapshot.get("project") or {}
    events = snapshot.get("events") or {}
    agents = snapshot.get("agents") or {}
    chips = [_state_chip(value.get("state"))] if value else []
    if project.get("id") or value.get("project"):
        chips.append(_chip(str(project.get("id") or value.get("project"))))
    routing = value.get("routing") if isinstance(value.get("routing"), dict) else {}
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
                # A card handed to the owner is answered here: the comment is written as the owner's
                # and reaches the PO (`card_ops._comment_role`).
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
    """Every head run the card recorded, by role: the latest run large, the runs before it beneath.

    `heads` is the read layer's one row per run: launch configuration, what the run reported, and
    whether a local-pty supervisor still holds it. `agents` is the dispatcher's view of the process
    behind the current run of each role, joined by run id (by role when the run is not named), and
    it decides the pulse: a run the dispatcher no longer holds is drawn from what its row says.
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
        # A process the dispatcher holds under no run the card recorded: drawn from what it says.
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
        # The process decides the pulse; the run's own reason (a legacy runtime, a lock that could
        # not be read) stays beside it, because it is what the run said and the process cannot.
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
    """The line under a head: the exact model id, the profile, the run, and why its state is what it is."""
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
    """One local-pty head, read-only: the tail of its journal.

    There is no form on this page and no script of its own.

    The layer hands over normalised values only, and each section is still drawn under
    `_shown`: whatever a head's run directory held, a section that cannot be drawn says so in its
    own place and the rest of the page is served.
    """
    ref = str(document.get("ref") or "")
    run_id = str(document.get("run_id") or "")
    head = _mapping(document.get("head"))
    journal = _mapping(document.get("journal"))
    card = f"/tasks/{quote(ref, safe='')}"
    tail = journal.get("tail")
    body = "\n".join(
        [
            _shown(lambda: _head_header(document, head, run_id)),
            _panel(
                "Journal",
                _shown(lambda: _head_journal(journal)),
                count=(len(tail) if isinstance(tail, list) else 0) or None,
            ),
            f'<p><a href="{escape(card)}">back to {escape(ref or "the card")}</a></p>',
        ]
    )
    return _page(f"Head {run_id}", body, nav="dashboard", crumbs=((ref, card), (run_id, "")))


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


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


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


# -- delegation, waits and e2e on a card (secretary-1811) -----------------------------------------

#: The prefix a wait's PO return address carries (`board/wait_card.PO_SESSION_PREFIX`).
PO_ADDRESS_PREFIX = "po-session:"
#: The prefix a wait's card return address carries (`board/wait_card.CARD_PREFIX`).
CARD_ADDRESS_PREFIX = "card:"
PO_TITLES_UNAVAILABLE = "PO session titles are unavailable"


def _block(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _entries(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def po_sessions_named(snapshot: dict[str, Any]) -> list[str]:
    """Every PO session a card page links to: the origin, its successor, the returns, the wait's addresses."""
    value = _block(_block(snapshot.get("card")).get("value"))
    origin = _block(value.get("origin"))
    named = [origin.get("po_session"), origin.get("current_session")]
    named += [row.get("session") for row in _entries(origin.get("returns"))]
    for address in _block(_block(value.get("wait")).get("po_sessions")).values():
        named += [_block(address).get("addressed"), _block(address).get("received_by")]
    return list(dict.fromkeys(str(item) for item in named if isinstance(item, str) and item))


def _session_titles(sessions: dict[str, Any] | None) -> tuple[dict[str, Any] | None, str]:
    """The sessions the PO store answered for, or None with the reason when it did not."""
    if sessions is None:
        return None, ""
    if not sessions.get("available"):
        return None, str(sessions.get("reason") or "no reason was recorded")
    return _block(_block(sessions.get("document")).get("sessions")), ""


def _po_session_link(session_id: Any, known: dict[str, Any] | None) -> str:
    """A PO session as its title linked to its page; its short id when untitled, gone or unknown."""
    text = str(session_id or "")
    if not text:
        return "—"
    short = f'<span class="id">{escape(text[:8])}</span>'
    found = _block((known or {}).get(text)) if known is not None else None
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


def _external(url: Any, label: str | None = None) -> str:
    """A link out when the value is a web address, the text otherwise."""
    text = str(url or "")
    if text.startswith(("https://", "http://")):
        return f'<a href="{escape(text)}" rel="noreferrer">{escape(label or text)}</a>'
    return _or_dash(text)


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
        po = _block(_block(wait.get("po_sessions")).get(address))
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
    """What this wait waits for, since when and until when, where it stands and where its result went."""
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
    result = _block(wait.get("result"))
    if result:
        summary = escape(str(result.get("summary") or result.get("outcome") or "no summary recorded"))
        evidence = result.get("evidence")
        rows.append(["result", summary + (f" · {_external(evidence, 'evidence')}" if evidence else "")])
    observed = _block(wait.get("last_observation"))
    if observed.get("text"):
        rows.append(
            [
                "last seen",
                f'{escape(str(observed["text"]))} <span class="age">{_or_dash(observed.get("at"))}</span>',
            ]
        )
    error = _block(wait.get("last_error"))
    if error.get("text"):
        rows.append(["last error", escape(str(error["text"]))])
    parts = [_rows(["", ""], rows)]
    deliveries = _block(wait.get("deliveries"))
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
        result = _block(run.get("result"))
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
    """Each e2e run of this card with its wait card, the budget it spends, and where after-merge stands."""
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
        # Both facts, never one standing in for the other: `state` is what the evidence says this
        # run is — running, finished, failed, its source unreadable, or unknown — and `ended` is
        # whether it is over. A run that reads `unknown` while still open is a run nobody may treat
        # as running, so it must not look like one here.
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
    """What this run produced: the verdict it carries, its result, and the status it exited with.

    The state word beside this says how a run *ended*; this says what came of it, and the two are
    not the same question. A reviewer run that ended normally and a reviewer run that ended
    normally having called the work `red` read identically in the state column, which is the one
    thing a card page is read to find out — so the verdict is drawn here, off `state.result.verdict`
    (the field :func:`ummanu.webproto.run_state.verdict_of` already publishes), and never
    re-derived from the result body by this module.

    The rule of this file applies unchanged: a result that is absent and a result that could not be
    read are different things and say different words. An open run has produced nothing yet and
    says exactly that, rather than borrowing the vocabulary of a run that finished empty.
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


def _review_form(ref: str, worker: dict[str, Any] | None) -> str:
    if worker is None:
        return '<p class="empty">there is no worker run to review yet.</p>'
    return (
        '<form id="review-form">'
        f'<input type="hidden" id="worker-run" value="{escape(str(worker.get("run_id") or ""))}">'
        '<div><label for="review-profile">reviewer profile</label>'
        '<input id="review-profile" placeholder="a profile from the head registry" required></div>'
        f'<button type="submit">review {escape(str(worker.get("run_id") or ""))}</button>'
        "</form>"
        '<p class="empty">a review is refused while its worker run is still open, and a repeated '
        "submission returns the review that already exists.</p>"
    )


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


def _long(text: Any, *, chars: int = 160) -> str:
    """Long text held to two lines, opened in place on a click. Short text is shown as it is.

    The text is in the page exactly once. The fold used to repeat the first line as its summary and
    then print the whole text again below it, in the code face; a reader saw the opening sentence
    twice and the prose as if it were a log. Now the one copy is the summary, clamped by the
    stylesheet until the disclosure opens, so opening it only lets the same block grow.
    """
    value = str(text or "").strip()
    if not value:
        return '<span class="empty">—</span>'
    if len(value) <= chars and "\n" not in value:
        return f'<span class="prose">{escape(value)}</span>'
    return f'<details class="text"><summary><span class="prose">{escape(value)}</span></summary></details>'


def _tabs(name: str, tabs: list[tuple[str, str, Any]]) -> str:
    """A strip of tabs over panels: `(label, body, count)` each, the first one shown.

    Radios and labels rather than a script: every panel is in the markup, so a search, a reader
    with scripts off and a test all see what the page holds. `name` keeps two strips on one page
    apart. A strip holds at most :data:`MAX_TABS` tabs; a longer list is a page to rethink.
    """
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


#: Effort as a count of lit bars. `extra` is Codex's older spelling of `xhigh`; anything else a
#: profile says is shown as its own word beside empty bars rather than guessed onto the scale.
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
#: The efforts that mean "no flag was passed": the CLI's own default decides. A PO session stored
#: with one of them was opened before an effort had to be chosen, and reads "not set".
EFFORT_DEFAULT = {"", "default", "none"}
#: What a PO session stored with no explicit effort says in place of an effort.
PO_EFFORT_UNSET = "not set"


def _model_name(model: Any) -> str:
    """A model id as people say it: `claude-opus-5-5` is Opus 5.5, `gpt-6-sol` is GPT-6 Sol.

    Only ids of a shape it recognises are renamed, and only by moving their own parts around; an
    alias (`opus`) or an id of any other shape is returned as it is, because a name made up here
    would claim a version nobody recorded.
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
    """The effort a run reported, else the one it was configured with -- the same rule as the model."""
    return head.get("resolved_effort") or head.get("effort")


def _head(
    role: str, head: dict[str, Any] | None, *, compact: bool = False, unset_effort: str = "CLI default"
) -> str:
    """One head: the role, the model that runs it, its effort, and whether its process is alive.

    `head` carries what the read layer says about it -- `profile`, `adapter`, `model`,
    `resolved_model`, `effort`, `state` -- and nothing here fills a gap: a head with no model is
    called an unknown model, not the likeliest one. `unset_effort` is what no effort reads as.
    """
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


#: The event kinds whose `data.body` is a record somebody wrote about the card: what a transition
#: is made of, read beside it.
RECORD_KINDS = {
    "card.reported": "worker report",
    "card.verdict": "reviewer verdict",
    "card.decided": "observer decision",
}


def _timeline(items: list[dict[str, Any]]) -> str:
    """Every transition of the card, oldest first, each opening on the records that made it.

    A transition is an event carrying `transition` (source and target). The records between the
    previous transition and this one -- the worker's report before a submit, the verdict before
    a park in Assessment, the decision before a rework -- are what made it, and they are read
    under it rather than found in the flat history.
    """
    ordered = sorted(items, key=lambda item: str(item.get("occurred_at") or ""))
    steps: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    pending: list[dict[str, Any]] = []
    for item in ordered:
        if item.get("kind") in RECORD_KINDS and (item.get("data") or {}).get("body"):
            pending.append(item)
        if isinstance(item.get("transition"), dict) and item["transition"]:
            steps.append((item, pending))
            pending = []
    if not steps:
        return '<p class="empty">no transition has been recorded for this card yet.</p>'
    parts = ['<ol class="timeline">']
    for event, records in steps:
        transition = event["transition"]
        when = str(event.get("occurred_at") or "")
        clock = when[11:19] if len(when) >= 19 else when
        actor = event.get("actor") if isinstance(event.get("actor"), dict) else {}
        reason = str(event.get("reason") or "")
        short = reason if len(reason) <= 120 else reason[:117].rstrip() + "…"
        detail = [
            f'<p class="why">{escape(when)} · {escape(str(actor.get("role") or ""))} {escape(str(actor.get("id") or ""))} · <span class="mono">{escape(str(event.get("kind") or ""))}</span></p>',
            (f"<div>{_long(reason, chars=300)}</div>" if reason else ""),
        ]
        for record in records:
            data = record.get("data") or {}
            who = record.get("actor") if isinstance(record.get("actor"), dict) else {}
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
        parts.append(
            f'<li><details><summary><time title="{escape(when)}">{escape(clock)}</time>'
            f'<span>{_state_chip(transition.get("source"))}<span class="arrow">→</span>{_state_chip(transition.get("target"))}'
            f' <span class="why">{escape(short)}</span></span></summary>'
            f'<div class="detail">{"".join(part for part in detail if part)}</div></details></li>'
        )
    parts.append("</ol>")
    return "\n".join(parts)


def _js(value: str) -> str:
    """A string safe to paste into the script literal: no quote, no backslash, no tag opener."""
    return escape(value).replace("\\", "").replace("'", "").replace('"', "")


_DASHBOARD_SCRIPT = """
const feedback = document.getElementById('feedback');
const form = document.getElementById('start-form');
function say(text, bad) { feedback.textContent = text; feedback.className = bad ? 'bad' : ''; }
function requestId(key) {
  const stored = sessionStorage.getItem(key);
  if (stored) return stored;
  const made = 'web-' + (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random());
  sessionStorage.setItem(key, made);
  return made;
}
const project = document.getElementById('project');
const ref = document.getElementById('ref');
function filter() {
  let first = null;
  for (const option of ref.options) {
    const owned = !option.dataset.project || option.dataset.project === project.value;
    option.hidden = !owned;
    if (owned && first === null) first = option;
  }
  if (first && ref.selectedOptions[0] && ref.selectedOptions[0].hidden) ref.value = first.value;
}
if (project && ref) { project.addEventListener('change', filter); filter(); }
if (form) form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const profile = document.getElementById('profile').value.trim();
  const instruction = document.getElementById('instruction').value;
  const card = ref.value;
  // The request id is the client's and it is kept: a repeated submission, a reload and a
  // reconnection all carry the same one, and the operation answers them with the same run.
  const id = requestId('ummanu.web.start.' + card + '.' + profile);
  say('starting...', false);
  const response = await fetch('/api/runs/start', {
    method: 'POST',
    headers: {'content-type': 'application/json'},
    body: JSON.stringify({ref: card, request_id: id, profile: profile, instruction: instruction}),
  });
  const document_ = await response.json();
  if (!response.ok) { say(document_.error.code + ': ' + document_.error.message, true); return; }
  say('run ' + document_.run.run_id + ' — ' + document_.state.value, false);
  window.location.href = '/tasks/' + encodeURIComponent(card);
});
"""

_ACTIONS_SCRIPT = """
// The owner's actions: every form with a data-action posts a JSON body to that route, and shows
// the answer where the form is. The request id belongs to this browser and is kept until the
// operation answers success: a retry after a network failure repeats the same request, and the
// next comment is a new one.
function freshId() { return 'web-' + (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random()); }
function keptId(key) {
  const name = 'ummanu.web.request.' + key;
  let id = sessionStorage.getItem(name);
  if (!id) { id = freshId(); sessionStorage.setItem(name, id); }
  return id;
}
function forgetId(key) { sessionStorage.removeItem('ummanu.web.request.' + key); }
async function postJson(url, body) {
  const response = await fetch(url, {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify(body)});
  let answer = null;
  try { answer = await response.json(); } catch (error) { answer = {error: {code: 'unreadable', message: 'the answer was not JSON (' + response.status + ')'}}; }
  return {ok: response.ok, status: response.status, answer: answer};
}
function tell(element, text, bad) { if (!element) return; element.textContent = text; element.className = 'feedback' + (bad ? ' bad' : ''); }
for (const form of document.querySelectorAll('form.act')) form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const out = form.querySelector('.feedback');
  const kind = form.dataset.kind;
  const key = form.dataset.key;
  const body = {request_id: keptId(key)};
  for (const field of form.elements) {
    if (!field.name) continue;
    if (field.type === 'checkbox') body[field.name] = field.checked; else body[field.name] = field.value;
  }
  if (kind === 'move' && !body.sprint_override) { delete body.sprint_override; delete body.sprint_override_reason; }
  if (kind === 'close' && !body.decisions.trim()) delete body.decisions;
  if (kind === 'close' && !window.confirm('Close this sprint? Its remaining cards and issues get the decisions stated, and the closeout is written.')) return;
  tell(out, 'sending...', false);
  const result = await postJson(form.dataset.action, body);
  if (!result.ok) { tell(out, result.answer.error.code + ': ' + result.answer.error.message, true); return; }
  forgetId(key);
  const answer = result.answer;
  if (kind === 'comment') tell(out, (answer.saved === false ? 'already saved' : 'saved') + (answer.comment_id ? ' as ' + answer.comment_id : answer.event_id ? ' as ' + answer.event_id : ''), false);
  else if (kind === 'move') tell(out, 'moved (' + (answer.event_id || 'recorded') + '); the card\\'s new state shows on the next page load', false);
  else if (kind === 'close') tell(out, 'closed; ' + ((answer.definition_of_done || {}).reason || ''), false);
  else tell(out, 'done', false);
  form.reset();
});
for (const form of document.querySelectorAll('form.pause')) form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const out = document.getElementById('pause-feedback');
  if (!window.confirm(form.dataset.confirm)) return;
  const body = {};
  for (const field of form.elements) if (field.name) body[field.name] = field.value;
  tell(out, 'sending...', false);
  const result = await postJson(form.dataset.action, body);
  if (!result.ok) { tell(out, result.answer.error.code + ': ' + result.answer.error.message, true); return; }
  const answer = result.answer;
  tell(out, (answer.outcome || answer.kind || 'done') + (answer.state && answer.state.mode ? ' — ' + answer.state.mode : '') + '; reloading', false);
  window.setTimeout(() => window.location.reload(), 800);
});
"""

_REFRESH_SCRIPT = """
// Reload every 30 s while nobody is typing, which is what keeps the bottom bar's numbers current
// on a page nobody touches. Every switch carrying data-refresh-toggle is the same switch -- the
// bar has one on every page, the dashboard keeps its own -- and the choice is this browser's and is
// remembered. The guard is the point and is never loosened: focus in a field, or any field holding
// typed content, cancels the reload, so a half-written /po message is never discarded by it -- and
// a password field counts, because the /po login page's token is typed into one and a tick that
// cleared it would be the same loss with none of the text on screen to retype from.
(() => {
  const boxes = Array.from(document.querySelectorAll('input[data-refresh-toggle]'));
  let on = true;
  try { on = localStorage.getItem('ummanu.web.refresh') !== 'off'; } catch (error) { on = true; }
  for (const box of boxes) {
    box.checked = on;
    box.addEventListener('change', () => {
      on = box.checked;
      for (const other of boxes) other.checked = on;
      try { localStorage.setItem('ummanu.web.refresh', on ? 'on' : 'off'); } catch (error) {}
    });
  }
  function reloadIfIdle() {
    const active = document.activeElement;
    if (active && (active.tagName === 'TEXTAREA' || active.tagName === 'INPUT' || active.tagName === 'SELECT')) return;
    for (const field of document.querySelectorAll('textarea, input[type=text], input[type=password], input[type=search], input[type=email], input[type=url], input[type=number], input:not([type])')) if (field.value) return;
    window.location.reload();
    return true;
  }
  // The one reload rule, published for anything else on the page that wants the bar read again
  // (the Codex reset button): it reloads, or answers false when somebody is typing.
  window.ummanuReloadWhenIdle = () => reloadIfIdle() === true;
  window.setInterval(() => {
    if (!on) return;
    reloadIfIdle();
  }, 30000);
})();
"""

_RESET_SCRIPT = """
// The Codex reset button on the bar. Every click asks first; the request id is made for the click
// and kept only while no answer came back, so pressing again after a network failure repeats the
// same request -- which the operation and the provider both answer once -- and any answer ends it.
(() => {
  const button = document.querySelector('button[data-codex-reset]');
  if (!button) return;
  const out = document.getElementById('codex-reset-feedback');
  const KEY = 'ummanu.web.request.codex-reset';
  const WORDS = {reset: 'reset: the Codex limit is reset', already_redeemed: 'already redeemed: this request was spent before',
    nothing_to_reset: 'nothing to reset: the provider says your usage does not need a reset right now; the credit stays',
    no_credit: 'no credit left', refused: 'refused', error: 'error', unknown: 'unknown'};
  function say(text, bad) { if (!out) return; out.textContent = text; out.className = 'bar-feedback' + (bad ? ' bad' : ''); }
  function kept() { try { return sessionStorage.getItem(KEY); } catch (error) { return null; } }
  function keep(id) { try { if (id) sessionStorage.setItem(KEY, id); else sessionStorage.removeItem(KEY); } catch (error) {} }
  button.addEventListener('click', async () => {
    if (!window.confirm(button.dataset.confirm)) return;
    const id = kept() || ('web-codex-reset-' + (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random()));
    keep(id);
    button.disabled = true;
    say('sending...', false);
    let response = null, answer = null;
    try {
      response = await fetch('/api/providers/codex/reset-limit', {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify({request_id: id})});
    } catch (error) {
      button.disabled = false;
      say('network failure; press again to repeat the same request', true);
      return;
    }
    keep(null);
    try { answer = await response.json(); } catch (error) { answer = null; }
    if (!response.ok || !answer || answer.error) {
      button.disabled = false;
      const failure = answer && answer.error ? answer.error.code + ': ' + answer.error.message : 'the answer was not JSON (' + response.status + ')';
      say(failure, true);
      return;
    }
    const outcome = String(answer.outcome || '');
    const good = outcome === 'reset' || outcome === 'already_redeemed';
    say((WORDS[outcome] || outcome) + (answer.reason ? ' — ' + answer.reason : ''), !good);
    if (!good) { button.disabled = false; return; }
    // Read the bar again through the page's one reload rule: never while a field holds typed text,
    // and never on a page that answers a POST, which carries no reload at all.
    if (outcome === 'reset') window.setTimeout(() => {
      if (!(window.ummanuReloadWhenIdle && window.ummanuReloadWhenIdle())) say(WORDS.reset + '; reload to read the new limits', false);
    }, 1500);
  });
})();
"""

_TASK_SCRIPT = """
const REF = '__REF__';
const KEY = 'ummanu.web.cursor.' + REF;
const notice = document.getElementById('events-notice');
const list = document.getElementById('events');
const seen = new Set(Array.from(list.children).map((li) => li.dataset.eventId));
// The cursor is the client's, and it is the only thing that decides where watching resumes. A
// reload or a reconnection reads the one this browser stored; only a first visit falls back to the
// end of the tail the server rendered. The server keeps nothing.
let cursor = sessionStorage.getItem(KEY) || '__CURSOR__';
let polling = true;
function render(item) {
  if (seen.has(item.event_id)) return;
  seen.add(item.event_id);
  const li = document.createElement('li');
  li.dataset.eventId = item.event_id;
  const time = document.createElement('time');
  time.textContent = item.occurred_at || '';
  const kind = document.createElement('b');
  kind.textContent = item.kind || '';
  li.append(time, kind, ' ' + (item.reason || item.outcome || ''));
  list.append(li);
  const empty = document.getElementById('events-empty');
  if (empty) empty.remove();
}
async function tail() {
  if (!polling) return;
  const url = '/api/tasks/' + encodeURIComponent(REF) + '/events?cursor=' + encodeURIComponent(cursor);
  let response;
  try { response = await fetch(url); } catch (error) { notice.textContent = 'the page could not reach this service: ' + error; return; }
  if (response.status === 400) {
    // A cursor this installation will not honour is not silently reset to the beginning: watching
    // stops, the reason is shown, and starting again from the current tail is the reader's choice.
    polling = false;
    const refused = await response.json();
    notice.className = 'unavailable';
    notice.textContent = 'this browser\\'s position in the history was refused (' + refused.error.message + '). Reload to start from the current tail.';
    sessionStorage.removeItem(KEY);
    return;
  }
  if (!response.ok) { notice.textContent = 'the history could not be read just now (' + response.status + '); retrying.'; return; }
  const page = await response.json();
  notice.className = page.source.state === 'available' ? '' : 'unavailable';
  notice.textContent = page.source.state === 'available' ? '' : 'could not read this card\\'s history: ' + page.source.reason;
  page.items.forEach(render);
  cursor = page.next_cursor;
  sessionStorage.setItem(KEY, cursor);
}
tail();
setInterval(tail, 4000);

const reviewForm = document.getElementById('review-form');
const feedback = document.getElementById('feedback');
if (reviewForm) reviewForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const worker = document.getElementById('worker-run').value;
  const profile = document.getElementById('review-profile').value.trim();
  // Same rule as a start: the id belongs to this browser and is kept, so a repeated submission
  // and a reconnection are answered with the review that already exists.
  const key = 'ummanu.web.review.' + worker + '.' + profile;
  let id = sessionStorage.getItem(key);
  if (!id) {
    id = 'web-' + (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random());
    sessionStorage.setItem(key, id);
  }
  feedback.textContent = 'starting the review...';
  feedback.className = '';
  const response = await fetch('/api/runs/review', {
    method: 'POST',
    headers: {'content-type': 'application/json'},
    body: JSON.stringify({request_id: id, profile: profile, worker_run_id: worker, ref: REF}),
  });
  const answer = await response.json();
  if (!response.ok) {
    feedback.className = 'bad';
    feedback.textContent = answer.error.code + ': ' + answer.error.message;
    return;
  }
  feedback.textContent = 'review run ' + answer.review.run.run_id + ' — ' + answer.review.state.value;
});
"""


# -- the sprint form ------------------------------------------------------------------------------

#: What the observer's launch state is called on the page, in words. The colour is a second reading
#: of the same fact and never the only one: three of these -- saved and not yet raised, really
#: running, and nothing could be established -- are the three an operator opens this page to tell
#: apart, and a page that drew them as three shades would be unreadable to half the people who open
#: it and to every screen reader. The words come from here; the sentence beside them is the layer's
#: own reason and is never rewritten.
LAUNCH_WORDS: dict[str, str] = {
    "not_started": "saved — no observer is up for it yet",
    "running": "running — an observer head is up",
    "unavailable": "not established — this could not be read at all",
    "stopped": "stopped — an observer was raised for it and is not alive",
    "not_declared": "no observer — this sprint declared none, so none is raised",
}

#: Said under the submit button, because it is the one thing about this form that surprises people:
#: there is no second "start" action anywhere. See `ummanu.webproto.sprint_ops`.
START_NOTICE = (
    "starting a sprint is opening it with an observer: there is no separate launch action, and the "
    "production tick raises one observer head for each open sprint that has none"
)

#: The word on the empty option of the two executor selects, and the answer that leaves the role
#: unpinned. Submitting it sends the layer nothing about that role at all.
EXECUTOR_CHOICE = "the observer chooses"


def redirect(location: str, *, what: str = "this sprint is open") -> str:
    """The body of a 303. A browser follows the header; anything that does not gets the link."""
    return _page(
        "opened",
        f'<p>{escape(what)}. <a href="{escape(location)}">{escape(location)}</a></p>',
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
    """The "new sprint" form, on this installation's own catalogue and on what was typed into it.

    `options` is a `sprint_options` document, or `None` when the catalogue could not be read at all
    while a refusal was being shown — the refusal is the thing being answered, so it is rendered
    over a form that says its choices are missing rather than replaced by a page about the
    catalogue.
    """
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

    Two refusals, two opposite instructions, and the page has to give the right one. A part-done
    create is the one that is not simply "no": a sprint exists, and the only safe move is to submit
    this same form again, which carries the same request id and therefore picks that sprint up
    instead of opening a second one.

    Everything else left no sprint behind and has spent its request id below this transport, so the
    form now carries a new one. That is said out loud rather than done quietly, because it is the
    difference between "fix the field and send this again" and "send exactly this again", and a
    person who read the wrong one either opens a second sprint or reaches a dead end.
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
    """The open issues, each carrying the product that owns it.

    The product is on every row rather than only in a script's memory: the list is narrowed to the
    selected product by the page's own script, and when that script does not run the whole list is
    there with each row saying whose it is, which is a usable form rather than a blank one.
    """
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
    """The registered projects, each saying whether an open sprint already holds it.

    `reserved_by` is three answers and not two: a list of sprints, an empty list, and `null` for a
    reservation index nobody could read. The third is said in its own words, because "held by
    nobody" and "nobody could say" would otherwise look identical on the one row where the
    difference decides whether a create is about to be refused.
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
    """The observer, which is the one head an operator must name, chosen and never typed.

    Only the profiles the layer marked as observers are offered, because that flag is
    `check_observer_profile` — the create's own check — asked of each profile rather than a rule
    restated here.

    The one answer that is not a profile is deliberately *not* offered. `none` opens a sprint the
    production tick raises no observer for, so on a page whose button says "start this sprint" it
    would be an option that starts nothing; it stays a legal answer for `ummanu sprint create`
    and for the rows that already carry it, which the sprint page renders unchanged.
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
    """One optional pin, whose first and default answer is that the observer picks the head.

    The empty option is not decoration: it is submitted as the empty string and the transport turns
    it into `None`, which is how the row is written with no field for this role at all. That is a
    different thing from a role pinned to a profile and a different thing again from one pinned to
    nothing, and the three must not be able to look alike here.
    """
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


def _profile_option(item: dict[str, Any], chosen: str) -> str:
    """One head profile as a person picks it: what it is called, its model and its effort.

    None of the three is composed here from a rule about naming: `label`, `model` and `effort` are
    fields of the profile the registry actually holds, and a profile that pins neither says so in
    the words the layer used rather than showing an empty column.
    """
    value = str(item.get("id") or "")
    model = str(item.get("model") or "") or "the adapter's default model"
    effort = str(item.get("effort") or "") or "the adapter's default effort"
    return (
        f'<option value="{escape(value)}"{_selected(value == chosen)}>'
        f"{escape(str(item.get('label') or value))} — model {escape(model)}, effort {escape(effort)}"
        f" ({escape(value)})</option>"
    )


#: Said beside a value that was submitted and that the catalogue no longer offers. It is kept on
#: the form rather than dropped for one reason: a form that quietly changed a submitted choice
#: would then be asking for a repeat of something the person never sent -- which is exactly wrong
#: after a part-done create, where the safe move is to submit *this* form again unchanged.
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


_SPRINT_FORM_SCRIPT = """
// The only thing this does is narrow the issue list to the product that is selected. Every row is
// already on the page with the product that owns it, so a browser that does not run this shows the
// whole list rather than an empty one.
const product = document.getElementById('product');
const issues = document.getElementById('issues');
function narrow() {
  if (!product || !issues) return;
  for (const row of issues.querySelectorAll('label')) {
    const owned = !product.value || row.dataset.product === product.value;
    row.hidden = !owned;
    if (!owned) row.querySelector('input').checked = false;
  }
}
if (product) { product.addEventListener('change', narrow); narrow(); }
"""


# -- the sprint page ------------------------------------------------------------------------------


def sprint(document: dict[str, Any]) -> str:
    """One sprint: what it is after, the card in hand, who works it, and what the observer decided.

    Each fact is drawn once. The status, the product and whether the observer is working are the
    header's chips and are not repeated in a panel; the current card is the "Now" panel's and is not
    listed again among the cards; the resume's decision and next step are the "Observer's call"
    and the resume tab carries only what that panel does not.
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
            _panel("Sprint", _sprint_fields(value, observer)),
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
    checks = work.get("checks") if isinstance(work.get("checks"), dict) else {}
    if checks:
        gate = checks.get("gate") if isinstance(checks.get("gate"), dict) else {}
        state = str(gate.get("state") or checks.get("state") or "unknown")
        tone = GATE_TONES.get(state, "ok" if state == "green" else "warn" if state == "not_green" else "")
        tiles.append(_tile("CI gate", state.replace("_", " "), str(checks.get("reason") or ""), tone))
    waiting = work.get("waiting") if isinstance(work.get("waiting"), dict) else {}
    if waiting:
        tiles.append(
            _tile("Observer is", str(waiting.get("state") or "unknown"), str(waiting.get("reason") or ""))
        )
    if tiles:
        parts.append(f'<div class="tiles">{"".join(tiles)}</div>')
    heads = _sprint_heads({**work, "observer": observer})
    if heads:
        parts.append(heads)
    budget = work.get("budget") if isinstance(work.get("budget"), dict) else {}
    if budget:
        parts.append(_budget_line(budget))
    return "\n".join(parts)


#: How a sprint's `waiting_on` kind reads on its page.
WAITING_ON_LABELS = {"run": "a run", "owner": "the owner", "po": "the PO", "dependency": "a dependency card", "observer": "the observer"}


def _waiting_on(items: Any) -> str:
    """What the sprint waits for, one line per card, each pointing at its card; nothing when nothing is."""
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
    decision = work.get("decision") if isinstance(work.get("decision"), dict) else {}
    entry = decision.get("entry") if isinstance(decision.get("entry"), dict) else {}
    when = str(entry.get("recorded_at") or "")
    return (
        f'<span class="age more" title="{escape(when)}">{escape(when[11:19] or when)}</span>' if when else ""
    )


def _observer_call(work: dict[str, Any]) -> str:
    """The observer's last decision in prose, its reasons one click away, and the next step."""
    decision = work.get("decision") if isinstance(work.get("decision"), dict) else {}
    entry = decision.get("entry") if isinstance(decision.get("entry"), dict) else {}
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


def _sprint_tabs(ref: str, value: dict[str, Any] | None, work: dict[str, Any]) -> str:
    """What is worth a look but not always: the cards, the Definition of Done, the resume, the issues."""
    if value is None:
        return '<div class="body"><p class="empty">no sprint was read, so there is nothing to show here.</p></div>'
    cards = work.get("cards") if isinstance(work.get("cards"), dict) else {}
    states = cards.get("states") if isinstance(cards.get("states"), dict) else {}
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
    dod_body = (
        f'<div class="doc">{markdown.render(dod)}</div>'
        if dod.strip()
        else '<p class="empty">this sprint has no Definition of Done.</p>'
    )
    resume = value.get("resume") if isinstance(value.get("resume"), dict) else {}
    # The call's fields are left out only when the call panel drew them, from the journal's decision.
    decision = work.get("decision") if isinstance(work.get("decision"), dict) else {}
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


def _sprint_fields(value: dict[str, Any] | None, observer: dict[str, Any]) -> str:
    """What the sprint was opened on, and where its observer stands -- the facts no chip carries."""
    if value is None:
        return '<p class="empty">no sprint was read, so there is nothing to show here.</p>'
    return _rows(
        ["", ""],
        [
            ["projects", _listed(value.get("reservations"), "this sprint reserves no project")],
            ["repositories", _listed(value.get("repositories"), "this sprint names no repository")],
            ["observer", _observer_line(observer)],
            *_executor_rows(value.get("executors") or {}),
        ],
    )


def _listed(values: Any, empty: str) -> str:
    items = [str(one) for one in (values or []) if str(one)]
    if not items:
        return f'<span class="empty">{escape(empty)}</span>'
    return ", ".join(escape(one) for one in items)


def _observer_line(observer: dict[str, Any]) -> str:
    """The declared observer, and separately whether one is up. Two sources, said apart.

    The head the dispatcher holds is named only when it is not the declared one: the same profile
    printed three times said nothing the first one had not.
    """
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
    return (
        _source_block(launch.get("source"), what="whether this sprint's observer is up")
        + f'{said} <span class="launch state state-{escape(state)}">{escape(words)}</span>'
        + f'<div class="reason">{escape(str(launch.get("reason") or "no reason was recorded"))}</div>'
        + differs
        + heartbeat
    )


def _executor_rows(executors: dict[str, Any]) -> list[list[str]]:
    """Both roles, always, and each in the state it is really in.

    A role nobody pinned is not a blank cell: it is the observer's to choose, which is a decision
    somebody made, and the page says so in those words.
    """
    rows = []
    for role in ("worker", "reviewer"):
        entry = executors.get(role) if isinstance(executors.get(role), dict) else {}
        state = str(entry.get("state") or "")
        if state == "pinned":
            said = f"pinned to {escape(str(entry.get('profile') or ''))}"
        elif state == "malformed":
            said = "this row carries a pin that is not a profile"
        else:
            said = escape(EXECUTOR_CHOICE)
        rows.append([escape(role), said])
    return rows


# -- the PO head ----------------------------------------------------------------------------------

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


def _po_indicator(section: dict[str, Any] | None) -> str:
    """The dashboard's PO panel: how many turns run, and the way in. Only a number, never a session."""
    if section is None:
        return ""
    if not section.get("available"):
        return '<span class="facts"><a href="/po">PO head</a> <span class="empty">— running turns could not be counted</span></span>'
    count = int((section.get("document") or {}).get("running") or 0)
    return f'<span class="facts"><a href="/po" id="po-indicator">{count} PO turn{"" if count == 1 else "s"} running</a></span>'


def po_login(message: str) -> str:
    body = "\n".join(
        [
            '<div class="lead"><h1>Product owner</h1></div>',
            f'<p class="refused">{escape(message)}</p>',
            '<form class="sprint" method="post" action="/po/login">',
            '<div class="field"><label for="po-token">PO token</label> ',
            '<input id="po-token" name="token" type="password" autocomplete="off" required></div>',
            '<button type="submit">open</button>',
            "</form>",
            (
                '<p class="hint empty">the token is the file <code>po-web-token</code> in the installation\'s '
                "data directory; OPERATIONS.md says how to read and rotate it.</p>"
            ),
        ]
    )
    return _page("Product owner", body, nav="po")


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
    """The PO head: the bar that opens a new session, then open sessions (or, with `closed`, the closed ones).

    A session is a row of two lines, its first message and what it runs on, rather than a table: the
    list owns the page's whole width and never scrolls sideways, whatever the window's width.
    """
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
    """One session: its title when set, its first message, then CLI · model · effort ("not set" when none was chosen) · when · id.

    An untitled session's first line is its first message, as before titles existed.
    """
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
    """A moment as how long ago it was, with the moment itself on hover; a date once it is days old."""
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
    """Open another session from the one being read, with this session's CLI, model and effort.

    It is the `/po` form's own route and its own fields (`POST /po/sessions` with a request id, a CLI,
    a model and an effort), reduced to hidden inputs: there is no second way of creating a session, and
    nothing about the session being read changes. The pair is copied from that session because it is
    the pair the owner chose; an installation that no longer offers it refuses the create the way the
    `/po` form's does, on `/po`, with the list of what it does offer to pick from.

    The effort is copied when the session has an explicit one. A session stored with `default` was
    opened before an effort had to be chosen, and `default` is never sent: the new session opens at
    the first effort `efforts` offers for that CLI, and the button says so beside it. With none
    offered the effort is left out, and the create is refused with the reason.

    The request id is the page's own with a suffix. One page mints one id and an id belongs to one
    operation for good (`po_requests`), so a page whose message was sent must not offer the same id
    again for a create — that would be `request_conflict` rather than a new session.
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
#: The title input's length; the store's `MAX_TITLE_LENGTH` is the rule, this only stops typing past it.
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
    """One session: its newest-first feed, the message box, turn state, stop while running, close otherwise.

    Its title, when set, heads the page, and a small form under the header renames it (an empty one
    clears it), open or closed. `title_draft` is what a refused rename submitted, kept in that form.

    The feed runs newest first and the message box sits above it, so every control the owner needs
    belongs to the box and not to the end of the feed: `send`, and at the far end of the same row
    `stop turn` while a turn runs, `close` while none does, and `new session` always.

    A closed session stays readable: its feed and who closed it when, with no message box and no
    close, but with `new session` — that is what the owner does next, and it touches nothing here.
    """
    session = document.get("session") or {}
    session_id = str(session.get("session_id") or "")
    closed = session.get("state") == "closed"
    turns = document.get("turns") or []
    by_turn: dict[Any, list[dict[str, Any]]] = {}
    for entry in document.get("feed") or []:
        by_turn.setdefault(entry.get("turn_seq"), []).append(entry)
    queued = document.get("queued") or []
    # Messages the PO service holds until the session's running turn ends; newest first, above the turns.
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
    # `send` belongs to the message form and the other three are forms of their own; HTML has no
    # nested form, so the row holds them side by side and `send` reaches its form by `form=`.
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
    # What this page shows, as the session script's polling baseline: read from the page at load and
    # from the page swapped in after that, never from the JSON that only says something changed.
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
    """The cards this session delegated, or whose results now come to it, with their states (secretary-1811).

    A collapsed disclosure at the head of the page, by the title (secretary-1818): its summary alone
    says the count, the columns the cards are in and how many results are not returned yet, and the
    table opens under it. It is outside every block the session script swaps (PO_SESSION_BLOCKS), so
    an in-place update never closes or moves it. Nothing when the document carries no such block (a
    poll that did not ask for it); the reason when the board could not be read; `none` when the
    session delegated nothing.
    """
    if not isinstance(section, dict):
        return ""
    items = section.get("items")
    if not isinstance(items, list):
        source = _block(section.get("source"))
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
        last = _block(item.get("last_return"))
        returned = (
            f"{_state_chip(last.get('state'))} {_or_dash(last.get('status') or 'pending')}" if last else "—"
        )
        state = str(item.get("state") or "unknown")
        tally[state] = tally.get(state, 0) + 1
        if (last and not last.get("status")) or (not last and state in PO_RETURNED_STATES):
            pending += 1
        relation = "" if item.get("relation") != "inherited" else ' <span class="age">(as successor)</span>'
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
    """The delegated-cards disclosure: closed on every render, so the owner opens it when they want it."""
    return (
        f'<details class="panel po-delegated" id="po-delegated"><summary>{escape(summary)}</summary>'
        f'<div class="body">{body}</div></details>'
    )


def _po_entry(entry: dict[str, Any]) -> str:
    """One feed item: the PO head's answer as the safe Markdown subset, the owner's text as typed."""
    role = "agent" if entry.get("role") == "agent" else "owner"
    who = "PO head" if role == "agent" else "owner"
    text = str(entry.get("text") or "")
    shown = (
        f'<div class="md">{markdown.render(text)}</div>'
        if role == "agent"
        else f'<div class="text">{escape(text)}</div>'
    )
    return (
        f'<li class="po-entry po-{role}"><div class="who">{who} · turn {escape(str(entry.get("turn_seq")))}'
        f" · {escape(str(entry.get('created_at') or ''))}</div>{shown}</li>"
    )


def _po_queued_entry(entry: dict[str, Any]) -> str:
    """A message on disk in the PO service's queue, waiting for the session's running turn to end."""
    return (
        f'<li class="po-entry po-owner" data-state="queued"><div class="who">owner · '
        f"{_chip('queued', 'accent')} · {escape(str(entry.get('queued_at') or ''))}</div>"
        f'<div class="text">{escape(str(entry.get("text") or ""))}</div></li>'
    )


def _po_turn_mark(turn: dict[str, Any]) -> str:
    state = str(turn.get("state") or "unknown")
    word, tone = TURN_MARKS.get(state, (state, ""))
    reason = turn.get("reason")
    said = f' <span class="reason">{escape(str(reason))}</span>' if reason else ""
    return f'<li class="po-mark" data-state="{escape(state)}">turn {escape(str(turn.get("seq")))} {_chip(word, tone)}{said}</li>'


_PO_FORM_SCRIPT = """
// Narrow the model and effort selects to the chosen CLI. The page is served with every CLI's options in
// one optgroup each, so it still works with no script; the script keeps them aside and lists only the
// chosen CLI's, flat -- hiding options would leave the other CLIs' group labels showing in the list.
const cli = document.getElementById('po-cli');
const selects = ['po-model', 'po-effort'].map((id) => document.getElementById(id)).filter(Boolean);
const all = new Map(selects.map((select) => [select, [...select.querySelectorAll('option')]]));
function narrow() {
  for (const select of selects) {
    const current = select.value;
    const owned = all.get(select).filter((option) => option.dataset.cli === cli.value);
    select.replaceChildren(...owned);
    const kept = owned.find((option) => option.value === current);
    if (kept) kept.selected = true;
    else if (owned.length) owned[0].selected = true;
  }
}
if (cli) { cli.addEventListener('change', narrow); narrow(); }
"""

#: The ids of the session page's blocks a turn's end changes: the head with its turn state, the stop
#: and close slots of the control row, and the feed with its queued messages. The session script
#: replaces exactly these with the ones of the page read again; the composer is in none of them.
PO_SESSION_BLOCKS = ("po-head", "po-stop", "po-close", "po-feed")

_PO_SESSION_SCRIPT = """
// While a turn runs or a message is queued, poll this session's JSON. When the turn count, the last
// turn's state or the queue changes, read this same page again and swap its changing blocks in place.
// Nothing reloads: the composer, what is typed in it, its selection and its focus are never touched.
const SESSION = '__SESSION__';
const BLOCKS = __BLOCKS__;
const status = document.getElementById('po-status');
const draft = document.getElementById('po-text');
// Enter sends through the form's own submit path, Shift+Enter keeps the newline. A form goes out once:
// a refusal renders a fresh page, and an in-place update after the turn it started lets it send again.
const form = document.getElementById('po-send');
// The send button sits in the composer's control row, outside the form it submits through `form=`,
// so it is looked up by that association and not only inside the form.
const button = document.querySelector('button[form="po-send"]');
let submitted = false;
if (form) {
  form.addEventListener('submit', (event) => {
    if (submitted) { event.preventDefault(); return; }
    submitted = true;
    if (button) button.disabled = true;
  });
}
if (form && draft) {
  draft.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter' || event.shiftKey || event.ctrlKey || event.altKey || event.metaKey) return;
    if (event.isComposing || event.keyCode === 229) return;
    event.preventDefault();
    if (submitted || !draft.value.trim()) return;
    form.requestSubmit();
  });
}
function say(text) { if (status) status.textContent = text; }
// The polling baseline is what a page shows, carried by its #po-turn-state: the turn count, the last
// turn's state, the queue length and whether a turn runs. Null when the page does not carry it whole.
function shown(page) {
  const element = page.getElementById('po-turn-state');
  if (!element) return null;
  const data = element.dataset;
  const turns = Number(data.turns);
  const queued = Number(data.queued);
  if (data.turns === undefined || data.queued === undefined || data.last === undefined) return null;
  if (!Number.isInteger(turns) || !Number.isInteger(queued)) return null;
  if (data.running !== 'true' && data.running !== 'false') return null;
  return { turns: turns, last: data.last, queued: queued, running: data.running === 'true' };
}
// Read this page again and put its fresh blocks where the old ones are. Answers the baseline the
// swapped-in page shows, or the reason it could not, and then the page is left exactly as it was.
async function swapBlocks() {
  let fresh;
  try {
    const response = await fetch(window.location.pathname, { cache: 'no-store' });
    if (!response.ok) return { failed: String(response.status) };
    fresh = new DOMParser().parseFromString(await response.text(), 'text/html');
  } catch (error) { return { failed: (error && error.message) || 'network error' }; }
  const pairs = BLOCKS.map((id) => [document.getElementById(id), fresh.getElementById(id), id]);
  const missing = pairs.find(([here, there]) => !here || !there);
  if (missing) return { failed: 'the page has no #' + missing[2] };
  const state = shown(fresh);
  if (!state) return { failed: 'the page does not say its turn state' };
  for (const [here, there] of pairs) here.outerHTML = there.outerHTML;
  return { state: state };
}
function waitingText(running) {
  return running
    ? 'the turn is running; this page updates when it ends'
    : 'the message is queued; this page updates when its turn starts';
}
// The JSON is only the cheap change detector. Whether to keep polling, and against what, is decided by
// the page on screen alone: a turn that started between the JSON read and the page read is shown
// running by the page, and is followed to its end.
let seen = shown(document);
if (seen && (seen.running || seen.queued > 0)) {
  say(waitingText(seen.running));
  let busy = false;
  const timer = window.setInterval(async () => {
    if (busy) return;
    busy = true;
    try {
      let doc;
      try {
        const response = await fetch('/po/api/sessions/' + encodeURIComponent(SESSION) + '?cards=0', { cache: 'no-store' });
        if (!response.ok) { say('could not refresh (' + response.status + ')'); return; }
        doc = await response.json();
      } catch (error) { say('could not refresh (' + ((error && error.message) || 'network error') + ')'); return; }
      const changed = (doc.turns || []).length !== seen.turns
        || (doc.last_turn ? doc.last_turn.state : '') !== seen.last
        || (doc.queued || []).length !== seen.queued
        || Boolean(doc.running) !== seen.running;
      if (!changed) return;
      const swapped = await swapBlocks();
      // A failed read leaves the old baseline, so the next tick sees the same change and tries again.
      if (swapped.failed) { say('could not refresh the answer (' + swapped.failed + ')'); return; }
      seen = swapped.state;
      // The turn a send started is over or under way, so the composer sends the next message again.
      submitted = false;
      if (button) button.disabled = false;
      if (seen.running || seen.queued > 0) { say('the page is up to date; ' + waitingText(seen.running)); return; }
      window.clearInterval(timer);
      say('the answer arrived');
    } catch (error) {
      say('could not refresh the answer (' + ((error && error.message) || 'unexpected error') + ')');
    } finally {
      busy = false;
    }
  }, 3000);
}
"""
