"""Doctor findings read from a standing agent's own `runs.jsonl`.

Busy-without-advance: a supervised head that keeps a role's ticks answered `supervised-busy-skip`
past the role's threshold while nothing advances is stuck, and doctor names it instead of waiting.
Lock-refused: ticks that keep losing the run lock with no record from any tick between them mean
the lock's holder is stuck.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

#: Standing agents whose ticks raise a supervised head.
STANDING_AGENTS = ("curator", "steward", "retro")
#: Hours a dispatch stream may stay busy-skipped before doctor names it, per (agent, dispatch event).
#: A daily run (retro, the steward's deep sweep) missed once is already a finding.
BUSY_WITHOUT_ADVANCE_HOURS = {
    ("curator", "dispatch"): 6,
    ("steward", "dispatch"): 6,
    ("steward", "deep-sweep"): 0,
    ("retro", "dispatch"): 0,
}
#: The progress record that answers each dispatch event; `memory_write` with result ok also counts.
_PROGRESS = {"dispatch": "advance", "deep-sweep": "deep-sweep-advance"}
#: Consecutive `lock-refused` records that make a red finding.
LOCK_REFUSED_STREAK = 3
BUSY_SKIP = "supervised-busy-skip"
_BUSY = frozenset({BUSY_SKIP, "busy-skip"})
# A fresh head starts the clock again: its predecessor's stall is no longer what blocks ticks.
_STARTED = frozenset({"supervised-started", "created", "ephemeral-restart", "watchdog-restart"})


def read_runs(runs: Path) -> list[dict]:
    """The decodable records of `runs.jsonl`, in order; a torn or foreign line is skipped alone."""
    try:
        lines = runs.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return []
    records = []
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def busy_without_advance(
    agent: str, records: list[dict], *, now: datetime | None = None
) -> list[dict[str, object]]:
    """One finding per dispatch stream of `agent` busy-skipped past its threshold with no progress."""
    findings: list[dict[str, object]] = []
    for (owner, stream), hours in BUSY_WITHOUT_ADVANCE_HOURS.items():
        if owner != agent:
            continue
        since: datetime | None = None
        skips = 0
        for record in records:
            kind, action = record.get("event"), record.get("action")
            progress = kind == _PROGRESS[stream] or (kind == "memory_write" and record.get("result") == "ok")
            if progress or (kind == stream and action in _STARTED):
                since, skips = None, 0
            elif kind == stream and action in _BUSY:
                skips += 1
                since = since or _time(record.get("ts"))
        if since is None:
            continue
        busy = (now or datetime.now(UTC)) - since
        if busy < timedelta(hours=hours):
            continue
        findings.append(
            {
                "code": "automation_busy_without_advance",
                "agent": agent,
                "dispatch": stream,
                "busy_since": since.isoformat(),
                "busy_skips": skips,
                "threshold_hours": hours,
                "message": (
                    f"{stream}: supervised head busy since {since.isoformat()} "
                    f"({int(busy.total_seconds() // 3600)}h, {skips} busy skip(s)) without "
                    f"{_PROGRESS[stream]}; threshold {hours}h"
                ),
            }
        )
    return findings


def trailing_busy_skips(records: list[dict]) -> int:
    """How many of the latest head dispatches in a row were busy skips."""
    count = 0
    for record in reversed(records):
        action = record.get("action")
        if action in _BUSY:
            count += 1
        elif action in _STARTED or action == "supervised-start-failed":
            break
    return count


def lock_refused(agent: str, records: list[dict]) -> dict[str, object] | None:
    """The finding when the latest LOCK_REFUSED_STREAK or more records of `agent` are all lock refusals."""
    streak: list[dict] = []
    for record in reversed(records):
        if record.get("event") != "lock-refused":
            break
        streak.append(record)
    if len(streak) < LOCK_REFUSED_STREAK:
        return None
    first = streak[-1]
    holder = streak[0].get("holder_pid")
    return {
        "code": "automation_lock_refused",
        "agent": agent,
        "holder_pid": holder,
        "refused_since": first.get("ts"),
        "refusals": len(streak),
        "message": (
            f"{len(streak)} ticks in a row refused the run lock since {first.get('ts')}; "
            f"holder pid {holder}"
        ),
    }
