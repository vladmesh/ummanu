"""Pure rendering of observer launch documents and sprint context."""

from __future__ import annotations

import json
from typing import Any

from ummanu.dispatch.observer_state import ObserverDelivery, delivery_evidence_summary
from ummanu.kernel.errors import TaskError
from ummanu.sprint_observer import EXECUTOR_FIELDS, pinned_executor

OBSERVER_PROMPT_FILE = "SPRINT.md"
OBSERVER_SKILL = "observe-sprint"


def render_observer_prompt(
    sprint: dict[str, Any],
    *,
    skill_path: str = "",
    delivery: ObserverDelivery | None = None,
) -> str:
    """The observer's launch document: the sprint entity, and the skill that says what to do.

    Data and one pointer. How a sprint is led belongs to the role skill alone, because two texts
    about the same job drift apart and the head then follows the stale one.
    """
    ref = str(sprint.get("ref") or "")
    repositories = [str(repo) for repo in (sprint.get("repositories") or [])]
    current = str(sprint.get("current_task") or "")
    budget = sprint.get("budget") if isinstance(sprint.get("budget"), dict) else {}
    marker = (
        (delivery.delivery_id, delivery.through_event)
        if delivery is not None and delivery.delivery_id and delivery.through_event
        else ("", "")
    )
    # Counted, never charged: the host failing to bring a card up is not the card spending the
    # sprint's restart budget, and the observer still has to see how often it happened.
    uncharged = budget.get("uncharged") if isinstance(budget.get("uncharged"), dict) else {}
    infrastructure = int(uncharged.get("infrastructure_blocked") or 0)
    sections = [
        f"# Sprint {ref}",
        "",
        "## No subagents",
        "",
        "Run this observer turn in this head only. Do not spawn, create, delegate to, or manage",
        "subagents or child agents. Use ordinary tools directly when needed.",
        "",
        f"You are the observer head of this sprint. Your instructions are the `{OBSERVER_SKILL}`",
        "role skill and nothing else in this file:",
        "",
        f"- {skill_path or OBSERVER_SKILL}",
        "",
        "Read it first. Everything below is the sprint entity as the board holds it right now; the",
        "live copy is `python3 -P -m ummanu sprint show --ref " + ref + "`.",
        "",
        "## Reference",
        "",
        ref or "(unknown sprint reference)",
        "",
        "## Goal",
        "",
        str(sprint.get("goal") or "(empty sprint goal)"),
        "",
        "## Definition of Done",
        "",
        str(sprint.get("definition_of_done") or "(empty definition of done)"),
        "",
        *_dod_item_lines(sprint.get("dod")),
        "## Repositories",
        "",
        "\n".join(f"- {repo}" for repo in repositories) or "- (none recorded)",
        "",
        "## Status",
        "",
        str(sprint.get("status") or "(unknown)"),
        "",
        "## Executors",
        "",
        *_executor_lines(sprint),
        "",
        "## PO session",
        "",
        str(sprint.get("po_session") or "(none recorded)"),
        "",
        "## Allowed productions",
        "",
        *_production_lines(sprint),
        "",
        "## Current card",
        "",
        current or "(none)",
        "",
        "## Budget",
        "",
        f"total {int(budget.get('total') or 0)} restart events recorded so far",
        "Signal threshold reached." if budget.get("signal_reached") else "Signal threshold not reached.",
        "Hard threshold reached." if budget.get("hard_reached") else "Hard threshold not reached.",
        *(
            [f"{infrastructure} infrastructure bring-up outcomes recorded, charged to no threshold."]
            if infrastructure
            else []
        ),
        "",
        *_observer_sprint_context(sprint),
    ]
    if marker[0]:
        sections.extend(
            [
                "## Pending delivery acknowledgement",
                "",
                f"delivery_id: {marker[0]}",
                f"through_event: {marker[1]}",
                "",
            ]
        )
    evidence = delivery_evidence_summary(delivery) if delivery is not None else ""
    if evidence:
        # A replacement head has no memory of the wakes its predecessor never received, and the
        # closing resume is written by whichever head is up at the end. The counts come with the
        # document so that head can report them instead of reporting zero.
        sections.extend(
            [
                "## Delivery evidence",
                "",
                evidence,
                "",
                "Report these counts in your closing resume; do not retry delivery yourself.",
                "",
            ]
        )
    return "\n".join(sections)


def _observer_sprint_context(sprint: dict[str, Any]) -> list[str]:
    """Render all live comments in board order, then the subordinate saved resume."""
    comments = _observer_comments(sprint)
    lines = [
        "## Standing owner decisions",
        "",
        "Apply these explicit entries before the saved resume. Latest production answer per project wins;",
        "the latest e2e grant/refusal controls admission. Grants add once; a refusal stops new runs even with budget room.",
        "Use only the latest advance consent for each action and scope, within max_uses and recorded operation uses.",
        "Free prose grants nothing; advance consent does not increase budget or grant production permissions.",
        "Do not ask the owner again for a covered decision or manufacture a new owner decision.",
        "Read IDs and quotations with `sprint show`; the PO records answers with `sprint record-owner-decisions`.",
        "",
        json.dumps(sprint.get("owner_decisions") or [], ensure_ascii=False, indent=2),
        "",
        "## Live sprint comments",
        "",
        "This is the complete current comment list in board order, with no timestamp cutoff. Apply",
        "every applicable PO/owner decision before evaluating the saved resume or its",
        "`next_safe_step`; the resume is continuity context, not authority over an owner decision.",
        "",
    ]
    if not comments:
        lines.extend(["(none)", ""])
    for index, comment in enumerate(comments, 1):
        row = comment if isinstance(comment, dict) else {}
        body = str(row.get("body") or "")
        owner = body == "[po]" or body.startswith("[po]\n")
        label = " (applicable PO/owner decision)" if owner else ""
        lines.extend(
            [
                f"### Comment {index}{label}",
                "",
                f"created_at: {row.get('created_at') or '(unknown)'!s}",
                "",
                body or "(empty comment body)",
                "",
            ]
        )
    resume = sprint.get("resume") if isinstance(sprint.get("resume"), dict) else None
    lines.extend(
        [
            "## Saved observer resume",
            "",
            "Apply the owner decisions above first. If this resume conflicts with one, revise the plan",
            "and reflect that decision in the next resume instead of following stale `next_safe_step`.",
            "",
            json.dumps(resume, ensure_ascii=False, indent=2) if resume is not None else "(none)",
            "",
        ]
    )
    return lines


def _observer_comments(sprint: dict[str, Any]) -> list[Any]:
    """Distinguish a loaded empty comment list from context that was never loaded."""
    comments = sprint.get("comments")
    if "comments" not in sprint or not isinstance(comments, list):
        raise TaskError("backend_error", "live sprint comments are unavailable", 1)
    return comments

def _dod_item_lines(dod: dict[str, Any] | None) -> list[str]:
    """The addressable DoD items with state and weight, or nothing for a free-text DoD."""
    if not dod or not dod.get("structured"):
        return []
    lines = ["## Definition of Done items", ""]
    for item in dod["items"]:
        weight = item["weight"] if item["weight"] is not None else "not weighed"
        evidence = f"; evidence: {item['evidence']}" if item["evidence"] else ""
        lines.append(f"- {item['id']} [{item['state']}, weight {weight}{evidence}] {item['text']}")
    return [*lines, "", f"{dod['met']} of {dod['total']} met.", ""]


def _production_lines(sprint: dict[str, Any]) -> list[str]:
    """The registered projects whose production this sprint's operations may touch, as created."""
    productions = [str(project) for project in sprint.get("allowed_productions") or []]
    return [f"- {project}" for project in productions] or [
        "- (none: this sprint's operations may touch no production)"
    ]


def _executor_lines(sprint: dict[str, Any]) -> list[str]:
    """What the owner fixed about who runs this sprint's cards, and where the choice is yours.

    Both roles are always printed, and the unpinned one is printed as the freedom it is. A section
    that simply left the role out would read to the head as "there is no reviewer here", which is
    the one thing the absent field never means.
    """
    lines = []
    for role in EXECUTOR_FIELDS:
        profile = pinned_executor(sprint, role)
        lines.append(
            f"- {role}: pinned to head profile `{profile}`. Every card you cut for this sprint runs "
            f"its {role} on it, and a card asking for another profile is refused."
            if profile
            else f"- {role}: not pinned. The owner fixed no {role} profile for this sprint, so you "
            f"choose one per card under the current rules — not a sprint that runs without a {role}."
        )
    # The reviewer head says who reviews, never whether: that is the card's stored review choice.
    lines.append(
        "- review: whether a card is reviewed at all is its `review` choice, not the reviewer head. "
        "It defaults to required for `code` and skipped for `research` and `infra`; "
        "`--review skipped` or `--review required` on the card overrides the default. A skipped "
        "card launches no reviewer; a skipped code card still runs the gate and merges on release."
    )
    return lines


def observer_launch_prompt() -> str:
    """Short pointer handed to the head on its command line; the document is in the workspace."""
    return (
        f"You are the sprint observer. The sprint is in {OBSERVER_PROMPT_FILE} at the workspace "
        f"root. Read it first, then follow the `{OBSERVER_SKILL}` role skill. Do not spawn, "
        "create, delegate to, or manage subagents; perform the observer turn in this head only."
    )
