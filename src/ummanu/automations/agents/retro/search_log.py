"""Second source for retro: the memory-mcp search log.

The audit stream contains several memory actions. This reader accepts only records whose
`action` is `memory_search`, and pairs them with the transcript batch so the judge can tell whether a
search actually fired near an answer (the "answered from memory without memory_search" failure).

Path is overridable via UMMANU_SEARCH_LOG (default ~/ummanu-data/memory/search-log.jsonl). The file
may not exist yet — that is not an error, it just means an empty tail.

Timestamps: Claude transcript ts are UTC (trailing Z); the search log writes naive UTC
(memory-mcp uses datetime.utcnow().isoformat()). Both normalize to naive UTC for comparison;
the window slack absorbs minor skew.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ummanu.infra.env import env_value

DEFAULT_SEARCH_LOG = Path.home() / "ummanu-data" / "memory" / "search-log.jsonl"
SEARCH_LOG = Path(env_value("UMMANU_SEARCH_LOG", str(DEFAULT_SEARCH_LOG)))


def _parse(ts):
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def tail(since=None, until=None, slack_s: int = 120) -> list[dict]:
    """search-log entries whose ts falls in [since, until] (ISO strings), widened by slack_s.

    Missing file -> []. Entries with an unparseable ts are dropped from a bounded window.
    """
    if not SEARCH_LOG.is_file():
        return []
    lo, hi = _parse(since), _parse(until)
    if lo:
        lo -= timedelta(seconds=slack_s)
    if hi:
        hi += timedelta(seconds=slack_s)
    out = []
    for line in SEARCH_LOG.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("action") != "memory_search":
            continue
        t = _parse(rec.get("ts"))
        if lo and (t is None or t < lo):
            continue
        if hi and (t is None or t > hi):
            continue
        out.append(rec)
    return out


def render_markdown(entries) -> str:
    """Compact tail for the judge: one line per search with hit count."""
    if not entries:
        return "# search log: no records in the batch window.\n"
    lines = ["# Search-log tail for the batch window", ""]
    for e in entries:
        hits = e.get("hits") or []
        lines.append(f"- `{e.get('ts', '?')}` k={e.get('k', '?')} hits={len(hits)}: {e.get('query', '')}")
    return "\n".join(lines) + "\n"
