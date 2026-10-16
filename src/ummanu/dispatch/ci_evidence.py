"""Render one CI suite's evidence directory (report.json, test-output.log) for a red gate comment.

The directory is what `scripts/ci_test_shards.py` uploads as `ci-evidence-<suite>-<sha>`.
Output is bounded by `TESTS_PER_SUITE` and `TRACEBACK_LINES`, omitted counts stated.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ummanu.projects.test_timing import diagnostic

TESTS_PER_SUITE = 5
TRACEBACK_LINES = 20
_LINE_CHARS = 300
_SEPARATOR_RE = re.compile(r"^(?:={20,}|-{20,})$")
_HEADER_RE = re.compile(r"^(?:FAIL|ERROR): \S+ \(.+\)")
_FRAME_RE = re.compile(r'^\s*File "([^"]+)", line \d+')
# Frames outside the checkout (stdlib, installed packages) are what the bound drops first.
_FOREIGN_FRAME_RE = re.compile(r"(?:/site-packages/|/dist-packages/|/lib/python\d)")


class EvidenceUnreadable(Exception):
    """The evidence directory lacks a readable report.json or test-output.log."""


def render_suite(suite: str, directory: Path) -> str:
    """Suite outcome, counts, failure locations, timing violations and bounded tracebacks."""
    try:
        report = json.loads((directory / "report.json").read_text(encoding="utf-8"))
        log = (directory / "test-output.log").read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError) as exc:
        raise EvidenceUnreadable(f"{type(exc).__name__}: {exc}") from None
    if not isinstance(report, dict) or not isinstance(report.get("counts"), dict):
        raise EvidenceUnreadable("report.json carries no counts")
    counts = ", ".join(f"{name} {value}" for name, value in report["counts"].items())
    lines = [f"Suite {suite}: outcome {report.get('outcome')}; {counts}"]
    violations = [diagnostic(item) for item in report.get("timing_violations") or []]
    locations = [entry for entry in report.get("failure_locations") or [] if entry not in violations]
    lines += _bounded("Failures:", [f"- {_clip(entry)}" for entry in locations], "failures")
    lines += _bounded("Timing violations:", [f"- {_clip(entry)}" for entry in violations], "timing violations")
    tracebacks = [_bounded_traceback(block) for block in _failure_blocks(log)]
    lines += _bounded("Tracebacks:", [line for block in tracebacks for line in block], "tracebacks",
                      groups=[len(block) for block in tracebacks])
    return "\n".join(lines)


def _bounded(title: str, entries: list[str], noun: str, *, groups: list[int] | None = None) -> list[str]:
    """`title` and the first `TESTS_PER_SUITE` entries (or line groups), naming the omitted count."""
    sizes = groups if groups is not None else [1] * len(entries)
    if not sizes:
        return []
    kept = sum(sizes[:TESTS_PER_SUITE])
    out = [title, *entries[:kept]]
    if len(sizes) > TESTS_PER_SUITE:
        out.append(f"({len(sizes) - TESTS_PER_SUITE} more {noun} omitted)")
    return out


def _failure_blocks(log: str) -> list[list[str]]:
    """Each unittest FAIL/ERROR header with the lines up to the next separator."""
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in log.splitlines():
        if _HEADER_RE.match(line):
            current = [line]
            blocks.append(current)
        elif current is not None and _SEPARATOR_RE.match(line.strip()):
            # The dash rule right under the header opens the traceback; any later rule closes it.
            if len(current) > 1:
                current = None
        elif current is not None:
            current.append(line)
    for block in blocks:
        while len(block) > 1 and not block[-1].strip():
            block.pop()
    return blocks


def _bounded_traceback(block: list[str]) -> list[str]:
    """Header plus at most `TRACEBACK_LINES` lines: in-checkout frames and the closing lines."""
    header, body = block[0], block[1:]
    if len(body) <= TRACEBACK_LINES:
        return [_clip(header), *(_clip(line) for line in body)]
    kept: set[int] = set()
    for index in range(len(body) - 1, -1, -1):
        match = _FRAME_RE.match(body[index])
        if not match or _FOREIGN_FRAME_RE.search(match.group(1)):
            continue
        frame = {index, index + 1} if index + 1 < len(body) and not _FRAME_RE.match(body[index + 1]) else {index}
        if len(kept | frame) > TRACEBACK_LINES // 2:
            break
        kept |= frame
    index = len(body) - 1
    while len(kept) < TRACEBACK_LINES:
        kept.add(index)
        index -= 1
    out = [_clip(header)]
    previous = -1
    for index in sorted(kept):
        if index > previous + 1:
            out.append(f"  ... ({index - previous - 1} lines omitted)")
        out.append(_clip(body[index]))
        previous = index
    return out


def _clip(line: str) -> str:
    return line if len(line) <= _LINE_CHARS else line[:_LINE_CHARS] + " ..."
