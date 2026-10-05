"""The e2e run budget: what a sprint, or a card outside every sprint, may spend on e2e runs (secretary-1796).

Every e2e run the dispatcher dispatches pays for stands, so runs are budgeted.

- **A sprint** carries `e2e_budget` (runs it may dispatch, set at `sprint create --e2e-budget`, default
  :data:`DEFAULT_E2E_BUDGET`), `e2e_used` and `e2e_charges` (one `{card, dispatch_id, at}` per charged
  run): three columns of `sprints` (revision `0023_sprint_e2e_budget`). A run is charged when its
  dispatch intent is written, before the POST, by one conditional UPDATE of the sprint row in the
  transaction that writes the intent (`TaskWriter.record_e2e_intent`), so two cards of one sprint can
  never together exceed the budget.
- **A card outside every sprint** keeps the per-card cap of :data:`CARD_E2E_CAP` runs, counted from its
  own run records, plus every raise recorded on it: the bag field :data:`E2E_CAP_FIELD`, JSON
  `{"raises": [{add, authorized_by, decision, at}]}`, written only by `task e2e-budget`.

A sprint first applies quoted standing decisions (`board.owner_decisions`): the PO can record grants
from its owner conversation directly; a sprint-wide refusal prevents new admission with any counter.
The released genuine owner-comment grant adapter below records the same grant entry.

An uncovered spent budget is a money decision. The dispatcher cuts a `decision` card for it, once per budget
generation (:func:`decision_request_id`; the generation is the budget, or the card's cap, the runs were
spent against). The PO first applies standing authority or declines further runs, handing over only
a new uncovered owner question. The released grant path raises only on the owner's recorded
word: `sprint e2e-budget` / `task e2e-budget` with `--authorized-by`, the event id of an owner comment
on that decision card (for a card's cap, also the after-merge batch decision that names the card spent,
:func:`batch_decision_request_id`) made after its handover whose one answer line is `e2e budget: raise <N>`; the
raise is that N and nothing else (:func:`authorized_raise`, :func:`owner_answer`).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from ummanu.board.extension_bag import EXTENSION_BAG
from ummanu.board.owner_handover import HANDED_TO_OWNER, OWNER_ROLE

#: What a sprint may spend when `sprint create` names no budget, and what 0023 gave every sprint.
DEFAULT_E2E_BUDGET = 3
#: Runs a card outside every sprint may dispatch before a raise.
CARD_E2E_CAP = 3

#: The sprint metadata the board client answers and takes (`sql_sprints`): the budget, the runs used,
#: the charges (JSON list), and `add`, the write that raises the budget by that many in place.
SPRINT_E2E_BUDGET = "sprint_e2e_budget"
SPRINT_E2E_USED = "sprint_e2e_used"
SPRINT_E2E_CHARGES = "sprint_e2e_charges"
SPRINT_E2E_BUDGET_ADD = "sprint_e2e_budget_add"

#: The bag field of a card outside every sprint that records the raises of its own cap.
E2E_CAP_FIELD = "e2e_cap"

#: The audit kinds of a raise: of a sprint's budget, and of one card's cap.
SPRINT_BUDGET_RAISED = "e2e_budget_raised"
CARD_CAP_RAISED = "e2e_cap_raised"

#: The infix of an after-merge run's dispatch id (`<carrier>-e2e-am-<n>-<random>`, secretary-1807): a
#: sprint's charges read it to count its after-merge runs.
AFTER_MERGE_DISPATCH_INFIX = "-e2e-am-"

#: The request-id actions of a budget decision card: of a sprint, and of a card outside every sprint.
SPRINT_DECISION_ACTION = "e2e-budget"
CARD_DECISION_ACTION = "e2e-cap"
#: The request-id action of the one decision an after-merge batch outside every open sprint needs when
#: some of its cards' caps are spent (secretary-1807): `dispatcher-e2e-caps-<generation>-<spent cards>`,
#: the spent cards joined by `.`. It authorizes a raise of each of those cards' caps.
BATCH_DECISION_ACTION = "e2e-caps"
_BATCH_DECISION = re.compile(r"dispatcher-e2e-caps-[0-9]+-(.+)")


def _token(value: str) -> str:
    # The dispatcher's request-id token (`dispatch.state.request_token`), spelled here so the board
    # can recognise a decision card without importing the dispatcher.
    token = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value)).strip("-")
    return token or "empty"


def decision_prefix(scope_ref: str) -> str:
    """The request-id prefix of every budget decision card cut for this sprint or card."""
    action = SPRINT_DECISION_ACTION if scope_ref.startswith("sprint:") else CARD_DECISION_ACTION
    return "-".join(_token(part) for part in ("dispatcher", action, scope_ref)) + "-"


def decision_request_id(scope_ref: str, generation: int) -> str:
    """The create request id of the decision for one budget generation: one card per generation."""
    return decision_prefix(scope_ref) + str(int(generation))


def batch_decision_request_id(spent: Iterable[str], generation: int) -> str:
    """The create request id of an after-merge batch's decision: one card per (spent cards, their caps)."""
    return f"dispatcher-{BATCH_DECISION_ACTION}-{int(generation)}-" + ".".join(_token(ref) for ref in spent)


def batch_decision_cards(request_id: str) -> list[str]:
    """The spent cards an after-merge batch decision's create request id names, or none."""
    matched = _BATCH_DECISION.fullmatch(str(request_id or ""))
    return matched.group(1).split(".") if matched else []


def budget_view(budget: int, used: int, charges: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """The `e2e` block `sprint show` and `sprint status` carry."""
    listed = [
        {"card": str(item.get("card") or ""), "dispatch_id": str(item.get("dispatch_id") or ""), "at": str(item.get("at") or "")}
        for item in charges
        if isinstance(item, Mapping)
    ]
    cards: list[str] = []
    for item in listed:
        if item["card"] and item["card"] not in cards:
            cards.append(item["card"])
    after_merge = sum(1 for item in listed if AFTER_MERGE_DISPATCH_INFIX in item["dispatch_id"])
    return {
        "budget": int(budget),
        "used": int(used),
        "summary": f"e2e: {int(used)} of {int(budget)}" + (f" ({after_merge} after merge)" if after_merge else ""),
        # Of the runs used, those an after-merge run spent (secretary-1807).
        "after_merge": after_merge,
        "cards": cards,
        "charges": listed,
    }


def sprint_budget(meta: Mapping[str, Any]) -> dict[str, Any]:
    """A sprint's budget view from its metadata; a store that names none reads as the default, unspent."""
    try:
        budget = int(str(meta.get(SPRINT_E2E_BUDGET) or DEFAULT_E2E_BUDGET))
        used = int(str(meta.get(SPRINT_E2E_USED) or 0))
    except ValueError:
        budget, used = DEFAULT_E2E_BUDGET, 0
    try:
        charges = json.loads(str(meta.get(SPRINT_E2E_CHARGES) or "[]"))
    except ValueError:
        charges = []
    return budget_view(budget, used, charges if isinstance(charges, list) else [])


def _bag(task: Mapping[str, Any]) -> Mapping[str, Any]:
    extensions = task.get("extensions")
    bag = extensions.get(EXTENSION_BAG) if isinstance(extensions, Mapping) else None
    return bag if isinstance(bag, Mapping) else {}


def cap_raises(task: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The raises recorded on a card's own cap, oldest first."""
    raw = _bag(task).get(E2E_CAP_FIELD)
    try:
        payload = raw if isinstance(raw, Mapping) else json.loads(str(raw or "") or "{}")
    except ValueError:
        return []
    raises = payload.get("raises") if isinstance(payload, Mapping) else None
    return [dict(item) for item in raises if isinstance(item, Mapping)] if isinstance(raises, list) else []


def card_cap(task: Mapping[str, Any]) -> int:
    """Runs a card outside every sprint may dispatch: the cap plus every recorded raise."""
    return CARD_E2E_CAP + sum(int(item.get("add") or 0) for item in cap_raises(task))


def cap_text(raises: list[dict[str, Any]]) -> str:
    return json.dumps({"raises": raises}, sort_keys=True, separators=(",", ":"))


def _refused(message: str) -> Exception:
    from ummanu.tasks import TaskError

    return TaskError("authorization_refused", message, 3)


def owner_answers_after_handover(events: Iterable[Mapping[str, Any]]) -> list[str]:
    """Event ids of the owner's comments on a card made after its first handover, in audit order."""
    handed = False
    found: list[str] = []
    for event in events:
        kind = str(event.get("kind") or "")
        if kind == HANDED_TO_OWNER:
            handed = True
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
        if handed and kind == "commented" and payload.get("marker") == OWNER_ROLE and event.get("event_id"):
            found.append(str(event["event_id"]))
    return found


#: The owner's answer on a budget decision card: exactly one line, `e2e budget: raise <N>` or
#: `e2e budget: no`, case-insensitive, surrounding whitespace ignored; the rest is free prose.
ANSWER_RAISE_LINE = "e2e budget: raise <N>"
ANSWER_NO_LINE = "e2e budget: no"
_RAISE = re.compile(r"e2e budget:\s*raise\s+([0-9]+)", re.IGNORECASE)
_NO = re.compile(r"e2e budget:\s*no", re.IGNORECASE)
#: What :func:`owner_answer` answers for `e2e budget: no`.
NO = "no"


def owner_answer(body: str) -> int | str | None:
    """The owner's recorded answer in one comment: `N` to raise by, :data:`NO`, or None.

    A comment answers only with exactly one answer line: a positive `e2e budget: raise <N>` or
    `e2e budget: no`, the whole line, in any case and with any surrounding whitespace. A comment with
    neither, with both, or with two answer lines answers nothing.
    """
    answers: list[int | str] = []
    for line in str(body or "").splitlines():
        text = line.strip()
        if raised := _RAISE.fullmatch(text):
            answers.append(int(raised.group(1)))
        elif _NO.fullmatch(text):
            answers.append(NO)
    if len(answers) != 1:
        return None
    [answer] = answers
    return answer if answer == NO or int(answer) > 0 else None


def _comment_body(reader: Any, decision: str, digest: str) -> str | None:
    """The body of the owner's comment on `decision` whose recorded digest is `digest`."""
    for comment in reader.show(decision).get("comments") or []:
        if not isinstance(comment, Mapping) or comment.get("marker") != OWNER_ROLE:
            continue
        lines = str(comment.get("body") or "").split("\n", 1)
        body = lines[1] if len(lines) == 2 and lines[0].strip() == f"[{OWNER_ROLE}]" else ""
        if hashlib.sha256(body.encode("utf-8")).hexdigest() == digest:
            return body
    return None


def authorized_raise(audit: Any, reader: Any, event_id: str, scope_ref: str, add: int | None) -> tuple[str, int]:
    """`(decision card, N)`: the raise of `scope_ref` the owner's comment `event_id` authorizes.

    The event has to be a committed owner-role comment on a decision card the dispatcher cut for this
    sprint's (or this card's) spent budget, made after that card was handed to the owner, whose text
    carries exactly one answer line (:func:`owner_answer`) and that line `e2e budget: raise <N>`. `add`,
    when given, has to be that N. Anything else is refused (`authorization_refused`) before anything is
    written.
    """
    event_id = str(event_id or "").strip()
    if not event_id:
        raise _refused(
            "a raise needs --authorized-by: the event id of the owner's comment on the budget decision card"
        )
    request_id = audit.event_id_owner(event_id)
    event = audit.committed_event(request_id) if request_id else None
    if event is None or str(event.get("event_id") or "") != event_id:
        raise _refused(f"--authorized-by {event_id} names no committed event")
    payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
    if event.get("kind") != "commented" or payload.get("marker") != OWNER_ROLE:
        raise _refused(f"--authorized-by {event_id} is not an owner comment; only the owner raises an e2e budget")
    decision = str(event.get("ref") or "")
    events = audit.events(decision)
    created = next((item for item in events if item.get("kind") == "created"), None)
    created_id = str((created or {}).get("request_id") or "")
    pattern = re.escape(decision_prefix(scope_ref)) + r"[0-9]+"
    # A card's cap is raised on its own decision, or on the after-merge batch decision naming it spent.
    batched = not scope_ref.startswith("sprint:") and scope_ref in batch_decision_cards(created_id)
    if created is None or not (re.fullmatch(pattern, created_id) or batched):
        raise _refused(
            f"--authorized-by {event_id} is a comment on {decision or 'no card'}, which is not an e2e budget "
            f"decision card of {scope_ref}"
        )
    if event_id not in owner_answers_after_handover(events):
        raise _refused(
            f"--authorized-by {event_id} was not made after {decision} was handed to the owner (`task handover`)"
        )
    body = _comment_body(reader, decision, str(payload.get("body_sha256") or ""))
    answer = owner_answer(body) if body is not None else None
    if answer is None:
        raise _refused(
            f"the owner's comment {event_id} carries no single answer line (`{ANSWER_RAISE_LINE}` or "
            f"`{ANSWER_NO_LINE}`), so it authorizes nothing"
        )
    if answer == NO:
        raise _refused(f"the owner's comment {event_id} answers `{ANSWER_NO_LINE}`: it authorizes no raise")
    runs = int(answer)
    if add is not None and add != runs:
        raise _refused(
            f"--add {add} is not what the owner recorded: the comment {event_id} says `e2e budget: raise {runs}`"
        )
    return decision, runs


__all__ = [
    "AFTER_MERGE_DISPATCH_INFIX",
    "ANSWER_NO_LINE",
    "ANSWER_RAISE_LINE",
    "BATCH_DECISION_ACTION",
    "CARD_CAP_RAISED",
    "CARD_E2E_CAP",
    "DEFAULT_E2E_BUDGET",
    "E2E_CAP_FIELD",
    "NO",
    "SPRINT_BUDGET_RAISED",
    "SPRINT_E2E_BUDGET",
    "SPRINT_E2E_BUDGET_ADD",
    "SPRINT_E2E_CHARGES",
    "SPRINT_E2E_USED",
    "authorized_raise",
    "batch_decision_cards",
    "batch_decision_request_id",
    "budget_view",
    "cap_raises",
    "cap_text",
    "card_cap",
    "decision_prefix",
    "decision_request_id",
    "owner_answer",
    "owner_answers_after_handover",
    "sprint_budget",
]
