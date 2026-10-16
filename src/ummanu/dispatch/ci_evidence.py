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
_HEADER_RE = re.compile(r"^(?:FAIL|ERROR): (\S+) \(([^()\s]+)\)(.*)$")
_LOCATION_RE = re.compile(r"^(.+): \S+:\d+$")
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
    blocks = _failure_blocks(log)
    located = [_location(entry) for entry in report.get("failure_locations") or [] if entry not in violations]
    known = {identifier for identifier, _entry in located}
    failing = located + [(identifier, identifier) for identifier in blocks if identifier not in known]
    shown = failing[:TESTS_PER_SUITE]
    if shown:
        lines.append("Failures:")
        lines += [f"- {_clip(entry)}" for _identifier, entry in shown]
        tracebacks = [blocks[identifier] for identifier, _entry in shown if identifier in blocks]
        if tracebacks:
            lines.append("Tracebacks:")
            lines += [line for block in tracebacks for line in _bounded_traceback(block)]
        if len(failing) > len(shown):
            lines.append(f"({len(failing) - len(shown)} more failing tests omitted)")
    if violations:
        lines.append("Timing violations:")
        lines += [f"- {_clip(entry)}" for entry in violations[:TESTS_PER_SUITE]]
        if len(violations) > TESTS_PER_SUITE:
            lines.append(f"({len(violations) - TESTS_PER_SUITE} more timing violations omitted)")
    return "\n".join(lines)


def _location(entry: str) -> tuple[str, str]:
    """(test identifier, entry) of a `failure_locations` entry, `<identifier>[: <path>:<line>]`."""
    match = _LOCATION_RE.match(entry)
    return (match.group(1) if match else entry), entry


def _failure_blocks(log: str) -> dict[str, list[str]]:
    """Each unittest FAIL/ERROR block, keyed by the failing test's `test.id()`."""
    blocks: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in log.splitlines():
        header = _HEADER_RE.match(line)
        if header:
            current = [line]
            blocks[_header_identifier(header)] = current
        elif current is not None and _SEPARATOR_RE.match(line.strip()):
            # The dash rule right under the header opens the traceback; any later rule closes it.
            if len(current) > 1:
                current = None
        elif current is not None:
            current.append(line)
    for block in blocks.values():
        while len(block) > 1 and not block[-1].strip():
            block.pop()
    return blocks


def _header_identifier(header: re.Match[str]) -> str:
    """unittest prints `name (id)` for a test and its subtests, and `name (class)` for a fixture error."""
    name, owner, rest = header.group(1), header.group(2), header.group(3)
    if owner.endswith(f".{name}"):
        return owner + rest
    return f"{name} ({owner}){rest}"


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
