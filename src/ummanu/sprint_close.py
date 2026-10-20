"""The decisions a sprint close carries, and the one file they arrive in."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import yaml

from ummanu.board.sprint_close import SprintCloseDecision, SprintCloseDecisions
from ummanu.kernel.errors import TaskError
from ummanu.product_issues import ISSUE_CLOSE_REASONS

KEEP_OPEN = "open"
ALREADY_CLOSED = "already_closed"
ALREADY_MOVED = "already_moved"
ISSUE_VERDICTS = tuple(sorted(ISSUE_CLOSE_REASONS)) + (KEEP_OPEN, ALREADY_CLOSED)
CARD_DISPOSITIONS = ("done", "drop", ALREADY_MOVED)
DISPOSITION_TARGETS = {"done": "done", "drop": "ready"}
CONFIRMABLE_CARD_STATES = tuple(sorted(set(DISPOSITION_TARGETS.values())))
CONFIRMATIONS = {
    "issue": (ALREADY_CLOSED, tuple(sorted(ISSUE_CLOSE_REASONS))),
    "card": (ALREADY_MOVED, CONFIRMABLE_CARD_STATES),
}

_SECTIONS = ("issues", "cards")
_ENTRY_FIELDS = {"ref", "verdict", "reason", "actual"}
_SHAPE = (
    "sprint close decisions file must be a mapping with the optional keys 'issues' and 'cards', "
    "each a list of {ref, verdict, reason} entries"
)


def _parse_close_decisions_typed(text: str) -> SprintCloseDecisions:
    """Read the decisions file into its normalized typed shape, or refuse it."""
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError:
        raise TaskError("validation", "sprint close decisions file is not valid YAML", 2) from None
    if document is None:
        document = {}
    if not isinstance(document, dict):
        raise TaskError("validation", _SHAPE, 2)
    _check_names(document, "sprint close decisions file")
    unknown = sorted(key for key in document if key not in _SECTIONS)
    if unknown:
        raise TaskError(
            "validation",
            "sprint close decisions file has unknown section(s): " + ", ".join(map(str, unknown)),
            2,
        )
    return SprintCloseDecisions(
        issues=_entries(document.get("issues"), "issue", ISSUE_VERDICTS),
        cards=_entries(document.get("cards"), "card", CARD_DISPOSITIONS),
    )


def parse_close_decisions(text: str) -> dict[str, list[dict[str, str]]]:
    """Released parser API: validate with typed values, project the historical dict."""
    return _parse_close_decisions_typed(text).to_document()


def _check_names(mapping: Mapping[Any, Any], what: str) -> None:
    unnamed = [key for key in mapping if not isinstance(key, str)]
    if unnamed:
        raise TaskError(
            "validation",
            f"{what} has non-string key(s): " + ", ".join(sorted(repr(key) for key in unnamed)),
            2,
        )


def _entries(raw: Any, kind: str, verdicts: tuple[str, ...]) -> tuple[SprintCloseDecision, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise TaskError("validation", _SHAPE, 2)
    seen: set[str] = set()
    entries: list[SprintCloseDecision] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise TaskError("validation", _SHAPE, 2)
        _check_names(entry, f"{kind} decision")
        extra = sorted(key for key in entry if key not in _ENTRY_FIELDS)
        if extra:
            raise TaskError(
                "validation",
                f"{kind} decision has unknown field(s): " + ", ".join(map(str, extra)),
                2,
            )
        reference = entry.get("ref")
        if not isinstance(reference, str) or not reference.strip():
            raise TaskError("validation", f"every {kind} decision needs a ref", 2)
        reference = reference.strip()
        if reference in seen:
            raise TaskError("validation", f"{kind} {reference} has more than one decision", 2)
        seen.add(reference)
        verdict = entry.get("verdict")
        if not isinstance(verdict, str) or verdict not in verdicts:
            raise TaskError(
                "validation",
                f"{kind} decision for {reference} needs a verdict, one of: " + ", ".join(verdicts),
                2,
            )
        reason = entry.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise TaskError(
                "validation",
                f"{kind} decision for {reference} requires a non-empty reason",
                2,
            )
        confirmation, facts = CONFIRMATIONS[kind]
        actual = entry.get("actual")
        if verdict == confirmation:
            if not isinstance(actual, str) or actual not in facts:
                raise TaskError(
                    "validation",
                    f"{kind} decision for {reference} confirms what somebody else did, so it must "
                    f"name it in 'actual', one of: " + ", ".join(facts),
                    2,
                )
        elif actual is not None:
            raise TaskError(
                "validation",
                f"{kind} decision for {reference} states 'actual', which only a {confirmation} "
                "decision carries",
                2,
            )
        entries.append(
            SprintCloseDecision(
                ref=reference,
                verdict=verdict,
                reason=reason.strip(),
                actual=str(actual) if verdict == confirmation else None,
            )
        )
    return tuple(entries)


def plan_close_decisions(
    decisions: SprintCloseDecisions | Mapping[str, Any] | None,
    *,
    declared_issues: Sequence[str],
    remaining: Sequence[str],
    states: Mapping[str, str],
    issue_states: Mapping[str, Mapping[str, Any]] | None = None,
) -> SprintCloseDecisions:
    """Match typed decisions against what this sprint actually declared and holds."""
    try:
        parsed = SprintCloseDecisions.from_document(decisions)
    except ValueError as exc:
        raise TaskError("validation", str(exc), 2) from None
    issues = list(parsed.issues)
    cards = list(parsed.cards)
    declared = list(declared_issues)
    unknown_issues = sorted({entry.ref for entry in issues} - set(declared))
    if unknown_issues:
        raise TaskError(
            "validation",
            "sprint close was given a decision for issue(s) the sprint did not declare: "
            + ", ".join(unknown_issues),
            2,
        )
    missing_issues = [reference for reference in declared if reference not in {entry.ref for entry in issues}]
    if missing_issues:
        raise TaskError(
            "validation",
            "sprint close needs an explicit decision for every declared issue; none was given for: "
            + ", ".join(missing_issues),
            2,
        )
    unknown_cards = sorted({entry.ref for entry in cards} - set(remaining))
    if unknown_cards:
        raise TaskError(
            "validation",
            "sprint close was given a disposition for card(s) that are not open work of this sprint: "
            + ", ".join(unknown_cards),
            2,
        )
    undisposed = [reference for reference in remaining if reference not in {entry.ref for entry in cards}]
    if undisposed:
        raise TaskError(
            "validation",
            "sprint close refuses to leave cards on a closed contract; dispose of each of them: "
            + ", ".join(f"{reference} ({states.get(reference, 'unknown')})" for reference in undisposed),
            2,
        )
    _check_issue_decisions_match_reality(issues, issue_states or {})
    _check_card_confirmations_match_reality(cards, states)
    return SprintCloseDecisions(
        issues=tuple(sorted(issues, key=lambda entry: entry.ref)),
        cards=tuple(sorted(cards, key=lambda entry: entry.ref)),
    )


def _check_issue_decisions_match_reality(
    issues: Sequence[SprintCloseDecision],
    issue_states: Mapping[str, Mapping[str, Any]],
) -> None:
    conflicting: list[str] = []
    for entry in issues:
        state = issue_states.get(entry.ref)
        if not isinstance(state, Mapping):
            continue
        closed = bool(state.get("closed"))
        carried = str(state.get("close_reason") or "")
        if entry.verdict == ALREADY_CLOSED:
            if not closed:
                raise TaskError(
                    "validation",
                    f"issue {entry.ref} is open, so there is nothing to confirm; decide it "
                    "with a closing verdict or leave it open",
                    2,
                )
            if entry.actual != carried:
                raise TaskError(
                    "validation",
                    f"issue {entry.ref} is closed as {carried or 'unknown'}, not as {entry.actual}",
                    2,
                )
        elif closed:
            conflicting.append(f"{entry.ref} ({carried or 'unknown'})")
    if conflicting:
        raise TaskError(
            "validation",
            "sprint close cannot decide issue(s) somebody else has already closed; confirm each "
            "with already_closed naming the reason it carries: " + ", ".join(sorted(conflicting)),
            2,
        )


def _check_card_confirmations_match_reality(
    cards: Sequence[SprintCloseDecision],
    states: Mapping[str, str],
) -> None:
    for entry in cards:
        if entry.verdict != ALREADY_MOVED:
            continue
        carried = states.get(entry.ref, "unknown")
        if entry.actual != carried:
            raise TaskError(
                "validation",
                f"card {entry.ref} is in {carried}, not in {entry.actual}",
                2,
            )


CLOSE_NOT_DONE = (
    "Closing a sprint states what became of its work. It is not a statement that the sprint's "
    "Definition of Done was reached: a closed sprint is not a satisfied contract, and what was and "
    "was not achieved is what the decisions and the closeout below say."
)
CLOSEOUT_DIRECTORY = "closeouts"


def closeout_path(reference: str, *, day: str) -> str:
    slug = "".join(character if character.isalnum() else "-" for character in reference).strip("-")
    return f"{CLOSEOUT_DIRECTORY}/{day}-{slug}.md"


def closeout_document(
    *,
    reference: str,
    goal: str,
    actor: str,
    reason: str,
    body: str,
    decisions: SprintCloseDecisions | Mapping[str, Any] | None,
    dod: Mapping[str, Any],
) -> str:
    """The closeout text: the close, its decisions and, for a sprint with items, each item's end state."""
    plan = SprintCloseDecisions.from_document(decisions)
    lines = [
        f"# Sprint closeout: {reference}",
        "",
        CLOSE_NOT_DONE,
        "",
        f"- Sprint: {reference}",
        f"- Goal: {goal or 'not recorded on the sprint'}",
        f"- Closed by: {actor}",
        f"- Reason for closing: {reason or 'not stated'}",
        "",
        "## What became of the work",
        "",
        body.strip(),
        "",
        "## Declared issues",
        "",
    ]
    lines.extend(_closeout_entries(plan.issues, "This sprint declared no issue."))
    lines.extend(["", "## Cards that were not done", ""])
    lines.extend(_closeout_entries(plan.cards, "No card was left in a working state at the close."))
    if dod["structured"]:
        lines.extend(["", "## Definition of Done items", ""])
        lines.extend(_closeout_dod_item(item) for item in dod["items"])
    return "\n".join(lines).rstrip() + "\n"


def _closeout_dod_item(item: Mapping[str, Any]) -> str:
    if item["state"] == "descoped":
        basis = f"; reason: {item['descoped']['reason']}"
    else:
        basis = f"; evidence: {item['evidence']}" if item["evidence"] else ""
    return f"- {item['id']} [{item['state']}{basis}] {item['text']}"


def _closeout_entries(entries: Sequence[SprintCloseDecision], empty: str) -> list[str]:
    if not entries:
        return [empty]
    return [
        f"- {entry.ref} — {entry.verdict}"
        + (f" ({entry.actual})" if entry.actual else "")
        + f": {entry.reason}"
        for entry in entries
    ]
