"""What a head's own child processes are doing, read from ``/proc`` (Linux, stdlib only).

A head running one long foreground command is silent yet working. This module reports the
descendants of the head's recorded pid with movement counters; interpreting them belongs to
``head_vitality.VitalitySnapshot.from_child_activity``. See docs/HEAD_VITALITY.md "Child processes".

Descendants come from one scan of ``/proc/[0-9]*/stat`` (parent is field 4; works without
``CONFIG_PROC_CHILDREN``). Movement covers every descendant: ``utime+stime+cutime+cstime`` plus the
head's own ``cutime+cstime`` (reaped grandchildren still count), and best-effort ``rchar+wchar``.
Only per-process descriptions are bounded by ``DESCENDANT_LIMIT``. Failures are answers, never
exceptions. Command lines leave already redacted and bounded: readings are persisted and may be
quoted into a successor's TASK.md.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ummanu.runtime.redact import scrub_secrets

#: How many descendants one reading describes; totals still cover the whole tree. Half the slots
#: go to the newest descendants, half to the most cumulative CPU.
DESCENDANT_LIMIT = 16
#: Bound on one command line as read (before redaction) and as reported.
COMMAND_READ_LIMIT = 4096
COMMAND_LIMIT = 300
OUTPUT_PATH_LIMIT = 200


def _clock_ticks() -> int:
    try:
        return int(os.sysconf("SC_CLK_TCK")) or 100
    except (OSError, ValueError):
        return 100


def _stat_fields(pid: int, proc: Path) -> list[str] | None:
    """Fields 3.. of ``/proc/<pid>/stat``; ``comm`` may hold spaces, so split after the last ')'."""
    try:
        stat = (proc / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    close = stat.rfind(")")
    if close < 0:
        return None
    fields = stat[close + 2 :].split()
    return fields if len(fields) > 19 else None


def _io_bytes(pid: int, proc: Path) -> int:
    try:
        text = (proc / str(pid) / "io").read_text(encoding="utf-8")
    except OSError:
        return 0
    total = 0
    for line in text.splitlines():
        name, _, value = line.partition(":")
        if name.strip() in ("rchar", "wchar"):
            try:
                total += int(value.strip())
            except ValueError:
                continue
    return total


def _command(pid: int, proc: Path) -> str:
    try:
        with (proc / str(pid) / "cmdline").open("rb") as handle:
            raw = handle.read(COMMAND_READ_LIMIT)
    except OSError:
        return ""
    text = raw.replace(b"\0", b" ").decode("utf-8", errors="replace").strip()
    return bounded_command(text)


def bounded_command(text: str) -> str:
    """One redacted, single-line, bounded command line."""
    flattened = "".join(ch if ch.isprintable() else " " for ch in str(text or ""))
    flattened = " ".join(flattened.split())
    scrubbed = scrub_secrets(flattened)
    if len(scrubbed) > COMMAND_LIMIT:
        scrubbed = scrubbed[: COMMAND_LIMIT - 3] + "..."
    return scrubbed


def _output_path(pid: int, proc: Path) -> str:
    """The regular file a descendant's stdout is redirected to, if ``/proc/<pid>/fd/1`` shows one."""
    try:
        target = os.readlink(proc / str(pid) / "fd" / "1")
    except OSError:
        return ""
    if not target.startswith("/") or target.startswith(("/dev/", "/proc/")):
        return ""
    if target.endswith(" (deleted)"):
        return ""
    return bounded_command(target)[:OUTPUT_PATH_LIMIT]


def _uptime_ticks(proc: Path, ticks: int) -> int:
    try:
        return int(float((proc / "uptime").read_text(encoding="utf-8").split()[0]) * ticks)
    except (OSError, ValueError, IndexError):
        return 0


def read_head_children(head_pid: Any, *, proc_root: str = "/proc") -> dict[str, Any]:
    """The live descendants of ``head_pid`` with their movement counters.

    Answers ``{"state": "observed", "head_pid", "uptime_ticks", "total_cpu_ms", "total_io",
    "descendant_count", "descendants": [...]}``; totals cover every live descendant (see module
    docstring). ``descendants`` holds at most ``DESCENDANT_LIMIT`` entries
    ``{"pid", "start", "cpu_ms", "io", "command", "output"}``, newest first; zombies are excluded.
    ``uptime_ticks`` shares the clock of ``start``. Otherwise ``{"state": "unavailable", "reason"}``.
    """
    try:
        pid = int(head_pid)
    except (TypeError, ValueError):
        return {"state": "unavailable", "reason": "head pid is not a number"}
    if pid <= 0:
        return {"state": "unavailable", "reason": "head pid is not positive"}
    proc = Path(proc_root)
    try:
        entries = [name for name in os.listdir(proc) if name.isdigit()]
    except OSError as exc:
        return {"state": "unavailable", "reason": f"/proc is not readable: {type(exc).__name__}"}
    if not (proc / str(pid)).exists():
        return {"state": "unavailable", "reason": "head pid is not running"}
    ticks = _clock_ticks()
    uptime = _uptime_ticks(proc, ticks)
    parents: dict[int, int] = {}
    stats: dict[int, list[str]] = {}
    for name in entries:
        child = int(name)
        fields = _stat_fields(child, proc)
        if fields is None:
            continue
        try:
            parents[child] = int(fields[1])
        except ValueError:
            continue
        stats[child] = fields
    children_of: dict[int, list[int]] = {}
    for child, parent in parents.items():
        children_of.setdefault(parent, []).append(child)
    descendants: list[int] = []
    frontier = [pid]
    seen = {pid}
    while frontier:
        current = frontier.pop()
        for child in children_of.get(current, ()):
            if child in seen:
                continue
            seen.add(child)
            descendants.append(child)
            frontier.append(child)
    readings: list[dict[str, Any]] = []
    total_ticks = 0
    head_fields = stats.get(pid)
    if head_fields is not None:
        try:
            total_ticks += int(head_fields[13]) + int(head_fields[14])
        except ValueError:
            pass
    total_io = 0
    for child in descendants:
        fields = stats[child]
        if fields[0] in ("Z", "X", "x"):
            continue
        try:
            cpu_ticks = sum(int(fields[index]) for index in (11, 12, 13, 14))
            start = int(fields[19])
        except ValueError:
            continue
        io = _io_bytes(child, proc)
        total_ticks += cpu_ticks
        total_io += io
        readings.append({"pid": child, "start": start, "cpu_ms": cpu_ticks * 1000 // ticks, "io": io})
    count = len(readings)
    half = DESCENDANT_LIMIT // 2
    newest = sorted(readings, key=lambda item: (item["start"], item["pid"]), reverse=True)
    busiest = sorted(readings, key=lambda item: (item["cpu_ms"], item["start"]), reverse=True)
    chosen: dict[int, dict[str, Any]] = {}
    for item in [*newest[:half], *busiest]:
        if len(chosen) >= DESCENDANT_LIMIT:
            break
        chosen.setdefault(item["pid"], item)
    described = sorted(chosen.values(), key=lambda item: (item["start"], item["pid"]), reverse=True)
    for item in described:
        item["command"] = _command(item["pid"], proc)
        item["output"] = _output_path(item["pid"], proc)
    return {
        "state": "observed",
        "head_pid": pid,
        "uptime_ticks": uptime,
        "total_cpu_ms": total_ticks * 1000 // ticks,
        "total_io": total_io,
        "descendant_count": count,
        "descendants": described,
    }
