"""Codex session rollout lookup: the TUI liveness signal for the watchdog.

Codex TUI paints in an alternate screen, so Orca's terminal lastOutputAt can stay stale
while the head is reasoning and writing rollout JSONL. These helpers map session files
back to a workspace and give the dispatcher's terminal-status check a supplemental activity
signal (ummanu/dispatch/runtime.py, through latest_activity_for).

Two hard rules, both from review 373:

- A malformed session file (broken UTF-8, JSON that is not an object, non-dict payload)
  is skipped, never raised: this code runs inside every dispatcher tick, and one bad
  .jsonl must not abort the tick for every card. Same tolerant posture as the curator's
  session_meta reader (agents/curator/discover.py), kept separate because liveness and
  transcript harvesting have different filtering and failure semantics.
- The scan only visits the freshest dated day dirs (sessions/YYYY/MM/DD/...): a watchdog
  poll cares about heads alive right now, and a long-lived CODEX_HOME accumulates years
  of rollouts that a per-tick rglob would keep re-reading.
"""

from __future__ import annotations

import json
from pathlib import Path

from ummanu.infra.env import env_value
from ummanu.runtime.codex_home import session_roots


def sessions_roots() -> list[Path]:
    """Where Codex heads write their rollouts: `UMMANU_CODEX_SESSIONS` alone when set, else every home
    a live head may be writing into (`codex_home.session_roots`), not only the current one."""
    configured = env_value("UMMANU_CODEX_SESSIONS", nonempty=True)
    return [Path(configured)] if configured else session_roots()


# A TUI head alive right now wrote its rollout today, or yesterday across midnight.
SCAN_DAY_DIRS = 2


def session_cwd(path: Path) -> str | None:
    """Cwd from a session jsonl's session_meta line, or None when unreadable/malformed."""
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= 20:
                    return None
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict) or rec.get("type") != "session_meta":
                    continue
                payload = rec.get("payload")
                if not isinstance(payload, dict):
                    payload = {}
                cwd = payload.get("cwd") or rec.get("cwd")
                return cwd if isinstance(cwd, str) and cwd else None
    except OSError:
        return None
    return None


def _recent_day_dirs(root: Path, limit: int = SCAN_DAY_DIRS) -> list[Path]:
    """Newest date-leaf dirs under sessions/YYYY/MM/DD, newest first, at most `limit`.

    An unexpected layout (flat dir, no digit-named years) degrades to scanning `root`
    itself: a full scan is slower but correct, a skipped tree would be a blind spot."""
    try:
        years = sorted(
            (d for d in root.iterdir() if d.is_dir() and d.name.isdigit()), key=lambda d: d.name, reverse=True
        )
    except OSError:
        return [root]
    if not years:
        return [root]
    days: list[Path] = []
    for year in years:
        try:
            months = sorted((d for d in year.iterdir() if d.is_dir()), key=lambda d: d.name, reverse=True)
        except OSError:
            continue
        for month in months:
            try:
                leaves = sorted(
                    (d for d in month.iterdir() if d.is_dir()), key=lambda d: d.name, reverse=True
                )
            except OSError:
                continue
            for day in leaves:
                days.append(day)
                if len(days) >= limit:
                    return days
    return days or [root]


def _session_paths_for(workspace: str):
    want = str(Path(workspace).resolve(strict=False))
    seen: set[Path] = set()
    for root in sessions_roots():
        if not root.is_dir():
            continue
        for day_dir in _recent_day_dirs(root):
            try:
                files = list(day_dir.glob("*.jsonl"))
            except OSError:
                continue
            for path in files:
                key = path.resolve(strict=False)
                if key in seen or session_cwd(path) != want:
                    continue
                seen.add(key)
                yield path


def latest_activity_for(workspace: str) -> float | None:
    """Latest mtime among recent session files whose session_meta cwd is `workspace`."""
    latest: float | None = None
    for path in _session_paths_for(workspace):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if latest is None or mtime > latest:
            latest = mtime
    return latest
