"""What a card of each kind must leave behind before it may be Done.

A `code` card's evidence is its merged candidate and is produced by the merge itself. A `research`
or `infra` card has no candidate: no branch is published, no pull request is opened and no CI runs
for it, so its completion is proved by a marked dispatcher comment on the card instead.

- `infra`: the worker's `report:done` body carries `## What was done` and `## How to verify`; the
  dispatcher copies both into one `[completion:infra]` comment when it accepts the report.
- `research`: the worker leaves its report in `.ummanu-report/` of its workspace, with a non-empty
  `report.md`. After the report is accepted and any review is done, and before the card parks in
  Assessment or is released, the dispatcher copies that directory to
  `state/knowledge/reports/<card ref>/` through the knowledge directory writer and writes one
  `[completion:research]` comment naming it. A refused or failed transfer Blocks the card.

Both markers are read only from comments the dispatcher wrote, so a worker or reviewer comment that
happens to contain the marker line proves nothing.

A `decision` or `operation` card has no candidate and no head either: the dispatcher submits it to
its sprint's PO session, and the PO completes it inside that turn with `task complete`, which writes
one `[completion:decision]` (`## Decision`, `## How to verify`) or `[completion:operation]`
(`## What was done`, `## How to verify`) comment and moves the card to Done in one transition. That
record is read only from comments the PO wrote, the same way.

A `wait` card (secretary-1790, `board/wait_card.py`) has no candidate, no head and no completion
record to prove: the dispatcher advances it, and its terminal move carries its outcome.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from ummanu.board.task_routing import PO_EXECUTED_TYPES, TaskReview, TaskType

#: The kinds the PO service executes (`decision`, `operation`); they are no-candidate kinds too.
PO_EXECUTED_KINDS = frozenset(kind.value for kind in PO_EXECUTED_TYPES)
WAIT_KIND = TaskType.WAIT.value
NO_CANDIDATE_KINDS = frozenset({TaskType.RESEARCH.value, TaskType.INFRA.value, WAIT_KIND}) | PO_EXECUTED_KINDS

INFRA_COMPLETION_MARKER = "completion:infra"
RESEARCH_COMPLETION_MARKER = "completion:research"
INFRA_REPORT_SECTIONS = ("What was done", "How to verify")
#: A PO-executed card's completion record: its marker and its two sections, by kind.
PO_COMPLETION_MARKERS = {
    TaskType.DECISION.value: "completion:decision",
    TaskType.OPERATION.value: "completion:operation",
}
PO_COMPLETION_SECTIONS = {
    TaskType.DECISION.value: ("Decision", "How to verify"),
    TaskType.OPERATION.value: ("What was done", "How to verify"),
}
#: The role whose comments carry a PO-executed card's completion record.
PO_COMPLETION_ROLE = "po"
# Optional machine-readable disposition, preserved by native task complete. Missing
# or malformed records may still complete the operation but never permit paid retry.
E2E_DISPOSITION_SECTION = "E2E disposition"
# The research report directory, relative to the worker's workspace, and the file it must hold.
RESEARCH_REPORT_DIR = ".ummanu-report"
RESEARCH_REPORT_FILE = "report.md"

_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t#]*$")


def is_po_executed(task: Mapping[str, Any]) -> bool:
    """Whether the PO service executes this card (`decision`, `operation`) instead of a head."""
    return str(task.get("type") or "") in PO_EXECUTED_KINDS


def is_wait(task: Mapping[str, Any]) -> bool:
    """Whether this is a `wait` card, which the dispatcher advances itself with no head."""
    return str(task.get("type") or "") == WAIT_KIND


def is_headless(task: Mapping[str, Any]) -> bool:
    """Whether no head ever runs this card: it takes no claim capacity and no head or reviewer."""
    return is_po_executed(task) or is_wait(task)


def has_candidate(task: Mapping[str, Any]) -> bool:
    """Whether this card delivers a candidate branch. A card of unknown kind is treated as code."""
    return str(task.get("type") or "") not in NO_CANDIDATE_KINDS


def review_required(task: Mapping[str, Any]) -> bool:
    """The stored review choice. A card written before the choice was stored reads as required."""
    return str(task.get("review") or "") != TaskReview.SKIPPED.value


def research_report_path(reference: str) -> str:
    return f"state/knowledge/reports/{reference}/"


def research_report_refusal(workspace: Path) -> str:
    """Why this workspace holds no research report (`""` when `.ummanu-report/report.md` is non-empty)."""
    report = Path(workspace) / RESEARCH_REPORT_DIR / RESEARCH_REPORT_FILE
    try:
        empty = report.is_symlink() or not report.is_file() or not report.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        empty = True
    if not empty:
        return ""
    return (
        f"a research done report needs a non-empty `{RESEARCH_REPORT_DIR}/{RESEARCH_REPORT_FILE}` in the "
        f"workspace ({workspace}); write the report there, with any artifacts beside it in "
        f"`{RESEARCH_REPORT_DIR}/`, and report again"
    )


def _sections(body: str) -> dict[str, str]:
    """Level-2 sections of a Markdown body, by heading text; deeper headings stay in their section."""
    found: dict[str, list[str]] = {}
    current: list[str] | None = None
    fence = False
    for line in body.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
        match = None if fence else _HEADING_RE.match(line.strip())
        if match and len(match.group(1)) <= 2:
            current = found.setdefault(match.group(2).strip(), []) if len(match.group(1)) == 2 else None
            continue
        if current is not None:
            current.append(line)
    return {name: "\n".join(lines).strip() for name, lines in found.items()}


def _required_sections(body: str, names: tuple[str, ...], what: str) -> tuple[dict[str, str], str]:
    """The named non-empty level-2 sections of `body`, and why it lacks some (`""` when it has all)."""
    sections = _sections(body)
    fields = {name: sections.get(name, "") for name in names}
    missing = [f"`## {name}`" for name, text in fields.items() if not text]
    if not missing:
        return fields, ""
    return fields, (
        f"{what} must carry two non-empty sections, `## {names[0]}` and `## {names[1]}` (a command "
        "or an observation); missing or empty: " + ", ".join(missing)
    )


def _render_completion_record(marker: str, names: tuple[str, ...], fields: Mapping[str, str]) -> str:
    parts = [f"[{marker}]", ""]
    for name in names:
        parts += [f"## {name}", "", str(fields.get(name) or "").strip(), ""]
    return "\n".join(parts).rstrip() + "\n"


def infra_report_fields(body: str) -> tuple[dict[str, str], str]:
    """The two infra report fields, and why the body lacks them (`""` when it has both)."""
    return _required_sections(body, INFRA_REPORT_SECTIONS, "an infra done report")


def render_infra_completion_record(fields: Mapping[str, str]) -> str:
    """The comment body the dispatcher writes; `infra_completion_record` reads it back."""
    return _render_completion_record(INFRA_COMPLETION_MARKER, INFRA_REPORT_SECTIONS, fields)


def po_completion_fields(kind: str, body: str) -> tuple[dict[str, str], str]:
    """The two sections a `task complete` body of this kind carries, and why it lacks them."""
    fields, refusal = _required_sections(body, PO_COMPLETION_SECTIONS[kind], f"a {kind} completion body")
    sections = _sections(body)
    if kind == TaskType.OPERATION.value and E2E_DISPOSITION_SECTION in sections:
        fields[E2E_DISPOSITION_SECTION] = sections[E2E_DISPOSITION_SECTION]
    return fields, refusal


def render_po_completion_record(kind: str, fields: Mapping[str, str]) -> str:
    """The comment body `task complete` writes; `po_completion_record` reads it back."""
    names = PO_COMPLETION_SECTIONS[kind]
    if kind == TaskType.OPERATION.value and E2E_DISPOSITION_SECTION in fields:
        names = (*names, E2E_DISPOSITION_SECTION)
    return _render_completion_record(PO_COMPLETION_MARKERS[kind], names, fields)


def render_research_completion_link(reference: str) -> str:
    return f"[{RESEARCH_COMPLETION_MARKER}]\n\n{research_report_path(reference)}\n"


def _marked(
    comments: Iterable[Mapping[str, Any]], marker: str, *, role: str = "dispatcher"
) -> list[str]:
    """Bodies of `role` comments (the dispatcher's by default) whose first content line is `[marker]`."""
    bodies: list[str] = []
    for comment in comments:
        if comment.get("marker") != role:
            continue
        lines = str(comment.get("body") or "").splitlines()
        if lines and lines[0].strip() == f"[{role}]":
            lines = lines[1:]
        while lines and not lines[0].strip():
            lines = lines[1:]
        if lines and lines[0].strip() == f"[{marker}]":
            bodies.append("\n".join(lines[1:]))
    return bodies


def infra_completion_record(task: Mapping[str, Any]) -> dict[str, str] | None:
    """The latest well-formed infra completion record on the card, or None."""
    for body in reversed(_marked(task.get("comments") or [], INFRA_COMPLETION_MARKER)):
        fields, refusal = infra_report_fields(body)
        if not refusal:
            return fields
    return None


def po_completion_record(task: Mapping[str, Any]) -> dict[str, str] | None:
    """The latest well-formed completion record the PO wrote on this decision/operation card, or None."""
    kind = str(task.get("type") or "")
    if kind not in PO_EXECUTED_KINDS:
        return None
    marked = _marked(task.get("comments") or [], PO_COMPLETION_MARKERS[kind], role=PO_COMPLETION_ROLE)
    for body in reversed(marked):
        fields, refusal = po_completion_fields(kind, body)
        if not refusal:
            return fields
    return None


def research_completion_link(task: Mapping[str, Any]) -> str | None:
    """The report directory the latest research completion link names, or None."""
    expected = research_report_path(str(task.get("ref") or ""))
    for body in reversed(_marked(task.get("comments") or [], RESEARCH_COMPLETION_MARKER)):
        if any(line.strip().strip("`") == expected for line in body.splitlines()):
            return expected
    return None


def missing_completion_evidence(task: Mapping[str, Any]) -> str:
    """Name the completion evidence a no-candidate card lacks, or `""` when it has it.

    A `code` card answers `""`: its evidence is the merge, which the release performs itself.
    """
    kind = str(task.get("type") or "")
    if kind == TaskType.INFRA.value:
        return "" if infra_completion_record(task) is not None else INFRA_COMPLETION_MARKER
    if kind == TaskType.RESEARCH.value:
        return "" if research_completion_link(task) is not None else RESEARCH_COMPLETION_MARKER
    if kind in PO_EXECUTED_KINDS:
        return "" if po_completion_record(task) is not None else PO_COMPLETION_MARKERS[kind]
    return ""


def no_candidate_report_contract(kind: str) -> list[str]:
    """The report contract a research/infra worker is handed in its task document."""
    if kind == TaskType.INFRA.value:
        return [
            "## Report contract for an infra card",
            "",
            "This card has no candidate: no branch is published, no pull request is opened and no",
            "CI runs for it, and nothing needs to be committed. Your done report body must carry two",
            "non-empty sections, which the dispatcher copies into the card's completion record:",
            "",
            "    ## What was done",
            "    ## How to verify",
            "",
            "`How to verify` is a command someone can run or an observation someone can repeat. A",
            "done report without both is refused.",
            "",
        ]
    if kind == TaskType.RESEARCH.value:
        return [
            "## Report contract for a research card",
            "",
            "This card has no candidate: no branch is published, no pull request is opened and no",
            "CI runs for it, and nothing needs to be committed. Put the report and every artifact",
            f"(markdown, scripts, data) in `{RESEARCH_REPORT_DIR}/` at the root of this workspace, with",
            f"the report itself in `{RESEARCH_REPORT_DIR}/{RESEARCH_REPORT_FILE}` (non-empty); subdirectories",
            "are fine. Do not commit that directory. A done report without the file is refused.",
            "",
            "After the report is accepted and any review is done, the dispatcher copies the whole",
            f"directory to `{research_report_path('<card ref>')}` in the instance repository and links",
            "it on the card; that link is the completion evidence. The copy is refused, and the card",
            "Blocked, for a symlink or special file, a secret in any text file, or more than 20 MiB in",
            "total. A rework round's next report replaces the directory.",
            "",
        ]
    return []
