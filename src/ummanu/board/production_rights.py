"""Production rights: the production an `operation` card touches, and the sprint's rule (secretary-1764).

An `operation` card names the production it touches at create (`task create --touches-production
<project>|none`); no other kind carries it. The value is one typed field of the card's extension bag
(`extensions.extra`, docs/BOARD_STORE.md §8.2), so no column and no migration: `touches_production`,
a registered project id or `none`. Only create writes it, and it is read only through
:func:`touches_production`, which treats a malformed value as no value rather than a guess.

A sprint allows its operations some productions (`sprint create --allow-production`, and later
`sprint allow-production --role po`, stored as `sprints.allowed_productions`). The PO service
evaluates the rule, in one place, when it queues a dispatcher's input
(`ummanu.po.service.PoService.submit`), and refuses nothing by it: every operation goes to the PO
as a normal turn, and :func:`rights_note` tells the PO what the rule says. A production the sprint
allows (or `none`) runs with no confirmation; any other one the PO decides under the owner's
standing rule, and records with `sprint allow-production` or hands the card to the owner
(secretary-1769).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from ummanu.board.extension_bag import EXTENSION_BAG

TOUCHES_PRODUCTION = "touches_production"
#: The value of an operation card that touches no production.
NO_PRODUCTION = "none"
#: The one kind that names its production.
OPERATION_KIND = "operation"
# A registered project id as the registry writes one; anything else is not a production.
_PRODUCTION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def touches_production(task: Mapping[str, Any]) -> str | None:
    """The production the card names (`none` included), or None when it carries no well-formed one."""
    extensions = task.get("extensions")
    bag = extensions.get(EXTENSION_BAG) if isinstance(extensions, Mapping) else None
    value = bag.get(TOUCHES_PRODUCTION) if isinstance(bag, Mapping) else None
    value = value.strip() if isinstance(value, str) else ""
    return value if _PRODUCTION.match(value) else None


def create_refusal(kind: str, value: str, registered: Iterable[str] | None) -> str:
    """Why `--touches-production value` cannot go on a new card of `kind`, or `""`.

    `registered` is the installation's project registry, read only for an operation card that names
    a project; `sprint create --allow-production` validates its projects against the same registry.
    """
    if kind != OPERATION_KIND:
        return (
            f"--touches-production names the production an operation card touches; a {kind} card takes none"
            if value
            else ""
        )
    if not value:
        return (
            "an operation card needs --touches-production <project>|none: the production it touches, "
            f"or {NO_PRODUCTION}"
        )
    if value == NO_PRODUCTION:
        return ""
    if not _PRODUCTION.match(value) or value not in set(registered or ()):
        return f"--touches-production names unknown registered project: {value}"
    return ""


#: What a dispatcher input is (`card_facts`'s `input`): the card itself, or the owner's answer to a
#: card handed to the owner, which is not checked again (the owner decided).
CARD_INPUT = "card"
OWNER_ANSWER_INPUT = "owner_answer"
INPUTS = (CARD_INPUT, OWNER_ANSWER_INPUT)
#: The one operation card the dispatcher itself cuts, and only under this request-id prefix
#: (secretary-1824): the release whose production activation was refused (the target's board schema
#: could not be applied) hands the PO one operation to recover it. The dispatcher creates no other
#: operation, and this one touches the production of the card it released (`TaskWriter._create`).
ACTIVATION_OPERATION_REQUEST_PREFIX = "dispatcher-release-activation-op-"

#: The kinds a dispatcher input may be about.
PO_CARD_KINDS = ("decision", OPERATION_KIND)
#: A wait card's outcome delivered to a PO session its creator named (secretary-1790). The session
#: may belong to no sprint, and the wait touches no production, so neither is asked of it.
WAIT_KIND = "wait"
WAIT_OUTCOME_INPUT = "wait_outcome"
#: The result of a card a PO session delegated, returned to that session when the card settles in
#: Done or Blocked (secretary-1792). Any kind, in a sprint or not; it is a result, not an operation,
#: so it names no production and gets no rights section.
DELEGATED_RESULT_INPUT = "delegated_result"


def card_facts(
    *, card_ref: str, kind: str, touches_production: str | None, sprint_ref: str, input: str = CARD_INPUT
) -> dict[str, Any]:
    """The structured facts a dispatcher input carries beside its text; a decision card names no production."""
    return {
        "card_ref": card_ref,
        "kind": kind,
        "touches_production": touches_production if kind == OPERATION_KIND else None,
        "sprint_ref": sprint_ref,
        "input": input,
    }


def facts_problem(card: Any) -> str:
    """What is missing or malformed in a dispatcher input's card facts, or `""` when nothing is."""
    if not isinstance(card, Mapping):
        return "the input carries no card facts"
    if card.get("kind") == WAIT_KIND:
        if not isinstance(card.get("card_ref"), str) or not card["card_ref"].strip():
            return "the card facts name no card_ref"
        if card.get("input") != WAIT_OUTCOME_INPUT:
            return f"a wait card's input is its {WAIT_OUTCOME_INPUT}, not {card.get('input')!r}"
        return "" if card.get("touches_production") is None else "a wait card names no production"
    if card.get("input") == DELEGATED_RESULT_INPUT:
        for field in ("card_ref", "kind"):
            if not isinstance(card.get(field), str) or not card[field].strip():
                return f"the card facts name no {field}"
        if not isinstance(card.get("sprint_ref"), str):
            return "the card facts name no sprint_ref (an empty one for a card outside every sprint)"
        return "" if card.get("touches_production") is None else "a delegated card's result names no production"
    if not isinstance(card.get("card_ref"), str) or not card["card_ref"].strip():
        return "the card facts name no card_ref"
    # An empty sprint_ref is a decision/operation card a PO session cut outside every sprint
    # (secretary-1792): that session executes it, and it has no sprint allowance.
    if not isinstance(card.get("sprint_ref"), str):
        return "the card facts name no sprint_ref"
    if card.get("kind") not in PO_CARD_KINDS:
        return f"the card facts name kind {card.get('kind')!r}, not {' or '.join(PO_CARD_KINDS)}"
    if card.get("input") not in INPUTS:
        return f"the card facts name input {card.get('input')!r}, not {' or '.join(INPUTS)}"
    production = card.get("touches_production")
    if card["kind"] != OPERATION_KIND:
        return "" if production is None else f"a {card['kind']} card names no production"
    # The owner's answer is not checked again, so it needs no production to check.
    if production is None and card["input"] == OWNER_ANSWER_INPUT:
        return ""
    if not isinstance(production, str) or not _PRODUCTION.match(production):
        return f"operation card {card['card_ref']} names no production it touches"
    return ""


def is_allowed(production: str, allowed: Iterable[str]) -> bool:
    """The sprint's rule: `none` always, a production only when the sprint allows it."""
    return production == NO_PRODUCTION or production in set(allowed)


def rights_line(production: str, sprint_ref: str, allowed: Iterable[str]) -> str:
    """What the rule was evaluated on: the card's production and what its sprint allows."""
    return f"touches production {production}; sprint {sprint_ref} allows [{', '.join(allowed)}]"


#: The heading of the section the PO service adds to an operation card's input.
RIGHTS_HEADING = "## Production rights (the PO service)"


def allow_production_command(sprint_ref: str, production: str, request_id: str) -> str:
    """The exact command the PO runs to allow a production on its sprint (`--reason` is the PO's)."""
    return (
        f"python3 -P -m ummanu sprint allow-production --ref {sprint_ref} --role po --project {production} "
        f"--reason '<text>' --request-id {request_id}"
    )


def rights_note(
    production: str, sprint_ref: str, allowed: Iterable[str] | None, *, request_id: str, owner_decisions: Iterable[Mapping[str, Any]] = ()
) -> str:
    """The section the PO service adds to an operation card's input: the rule's verdict and what to do.

    `allowed` is the sprint's list, or None for `none`, which is allowed without reading the sprint.
    A production the sprint does not allow is not refused: the PO decides it under the owner's
    standing rule and either records an allowance (`allow_production_command`, `request_id` its id)
    or hands the card to the owner. An operation cut outside every sprint (`sprint_ref` empty) has
    no allowance to read or record: the PO decides it under the same rule, with nothing to record.
    """
    if not sprint_ref and production != NO_PRODUCTION:
        return (
            f"{RIGHTS_HEADING}\n\n"
            f"touches production {production}; no sprint: the card was cut outside every sprint, so "
            "there is no sprint allowance to read or to record.\n\n"
            "This is not a refusal: decide it under the owner's standing rule. Production of ummanu is "
            "allowed by default, because it is the development server; any other production only when "
            "the owner agreed to it (in this session or on the card). If the rule allows it, run the "
            "operation in this turn and touch no other production. If it does not, hand the card to the "
            "owner with `task handover --to owner` (the command under \"Or hand it to the owner\" above) "
            "and end the turn."
        )
    if production == NO_PRODUCTION or allowed is None:
        allows = "the sprint allows it" if sprint_ref else "nothing to allow"
        return (
            f"{RIGHTS_HEADING}\n\n"
            f"touches production {NO_PRODUCTION}: {allows}. Touch no production in this turn."
        )
    for entry in reversed(list(owner_decisions)):
        if entry["kind"] == "production" and entry["scope"] == production:
            disposition = "allows" if entry["value"] else "refuses"
            action = "Run the operation without further confirmation." if entry["value"] else "Do not touch this production. Apply this answer without asking the owner again."
            return (f"{RIGHTS_HEADING}\n\nStanding owner decision {sprint_ref}/{entry['id']} {disposition} "
                    f"production {production}. {action}\n\nOwner quotation:\n\n{entry['quotation']}\n\n"
                    f"Read back with `sprint show --ref {sprint_ref}`. A later quoted owner answer is recorded "
                    "with `sprint record-owner-decisions --role po --decisions-file <file>`.")
    allowed = list(allowed)
    line = rights_line(production, sprint_ref, allowed)
    if is_allowed(production, allowed):
        return (
            f"{RIGHTS_HEADING}\n\n{line}\n\n"
            "The sprint allows it: run the operation with no further confirmation, and touch no other "
            "production in this turn."
        )
    return (
        f"{RIGHTS_HEADING}\n\n{line}\n\n"
        f"The sprint does not allow production {production} yet. This is not a refusal: decide it under "
        "the owner's standing rule. Production of ummanu is allowed by default, because it is the "
        "development server; any other production only as agreed at sprint planning (the sprint's "
        "comments and its why-document say what was agreed).\n\n"
        "- If you may allow it, record the decision first, with the owner's rule it follows as the "
        "reason, then run the operation in this turn:\n\n"
        f"      {allow_production_command(sprint_ref, production, request_id)}\n\n"
        "- If you may not, hand the card to the owner with `task handover --to owner` (the command "
        'under "Or hand it to the owner" above) and end the turn.'
    )


__all__ = [
    "ACTIVATION_OPERATION_REQUEST_PREFIX",
    "CARD_INPUT",
    "DELEGATED_RESULT_INPUT",
    "INPUTS",
    "NO_PRODUCTION",
    "OPERATION_KIND",
    "OWNER_ANSWER_INPUT",
    "PO_CARD_KINDS",
    "RIGHTS_HEADING",
    "TOUCHES_PRODUCTION",
    "WAIT_KIND",
    "WAIT_OUTCOME_INPUT",
    "allow_production_command",
    "card_facts",
    "create_refusal",
    "facts_problem",
    "is_allowed",
    "rights_line",
    "rights_note",
    "touches_production",
]
