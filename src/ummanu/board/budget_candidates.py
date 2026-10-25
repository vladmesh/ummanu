"""The budget pass's candidate set: committed card events that may be budget events, not yet charged.

`dispatch.production._budget_event_type` is the classifier; what is here is its **necessary
conditions**, written once in SQL and once in Python for the file journal. Every event the classifier
types satisfies them, over both event shapes (the generic `kind`/`payload` form and the typed
`record_type`/`transition`/`data` form). They are a superset: a transition to `blocked` whose forward
taxonomy owns no budget type satisfies them and classifies to nothing, and the budget pass resolves
such a candidate with a terminal marker rather than making the SQL exact.

An event is *uncharged* while no committed request has the id `sprint-budget-<identity>`, the id its
charge or its terminal marker is written under. That is a primary-key probe, so the set is an
anti-join and not a position: an event that commits late, or whose card cannot be looked up for any
number of ticks, stays in the set until it is resolved. `0037_stage_budget_candidates`
serves the read with a partial index holding only candidate rows.
"""

from __future__ import annotations

from typing import Any

from ummanu.board.task_routing import ACTIVE_STATES

#: `_budget_event_type` spells these; the SQL below repeats them as literals, and the test that
#: pins the predicate to the classifier (`tests/test_budget_candidates.py`) keeps them in step.
_VERDICT_KINDS = ("verdict", "card.verdict")
_CREATED_BUDGET_EVENTS = ("hotfix", "recreated_task")

#: The charge id prefix; a charge and a terminal marker both take `CHARGE_PREFIX + identity`.
CHARGE_PREFIX = "sprint-budget-"

#: Over `intent`, the committed record. Kept textually identical to the partial index predicate of
#: `0037_stage_budget_candidates` (a test compares them), so the planner can prove the index applies. Its
#: `%` are literal: a statement that passes parameters doubles them (`sql_audit`).
CANDIDATE_PREDICATE = (
    "(intent ->> 'ref') <> '' AND (intent ->> 'ref') NOT LIKE 'sprint:%' AND ("
    "((intent ->> 'kind') IN ('verdict', 'card.verdict') "
    "AND 'review:red' IN ((intent -> 'payload' ->> 'marker'), (intent -> 'data' ->> 'marker')))"
    " OR (intent -> 'transition' ->> 'target') = 'blocked'"
    " OR ((intent ->> 'kind') = 'moved' AND (intent -> 'payload' ->> 'to') = 'blocked')"
    " OR ((intent -> 'transition' ->> 'target') = 'ready'"
    " AND (intent -> 'transition' ->> 'source') IN ('assessment', 'in_progress', 'validate', 'ci', 'review'))"
    " OR ((intent ->> 'kind') = 'moved' AND (intent -> 'payload' ->> 'to') = 'ready'"
    " AND (intent -> 'payload' ->> 'from') IN ('assessment', 'in_progress', 'validate', 'ci', 'review'))"
    " OR (((intent -> 'transition' ->> 'target') = 'in_progress'"
    " OR ((intent ->> 'kind') = 'moved' AND (intent -> 'payload' ->> 'to') = 'in_progress'))"
    " AND (intent ->> 'request_id') LIKE '%gate-red%')"
    " OR ((intent ->> 'kind') = 'created'"
    " AND ((intent -> 'payload' ->> 'budget_event') IN ('hotfix', 'recreated_task')"
    " OR (intent -> 'data' ->> 'budget_event') IN ('hotfix', 'recreated_task'))))"
)

#: The event's identity, `event_id or request_id` as the budget pass has always spelled it.
IDENTITY = "COALESCE(NULLIF(intent ->> 'event_id', ''), NULLIF(intent ->> 'request_id', ''))"


def _text(value: Any) -> str | None:
    """What `->>` answers for a JSON member: a string as itself, a non-string rendered, null as None."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _member(document: Any, *path: str) -> str | None:
    for key in path[:-1]:
        document = document.get(key) if isinstance(document, dict) else None
    return _text(document.get(path[-1])) if isinstance(document, dict) else None


def is_candidate(event: dict[str, Any]) -> bool:
    """`CANDIDATE_PREDICATE` over one record, for the file journal, which has no SQL."""
    reference = _member(event, "ref")
    if not reference or reference.startswith("sprint:"):
        return False
    kind = _member(event, "kind")
    target = _member(event, "transition", "target")
    moved_to = _member(event, "payload", "to") if kind == "moved" else None
    if kind in _VERDICT_KINDS and "review:red" in (
        _member(event, "payload", "marker"),
        _member(event, "data", "marker"),
    ):
        return True
    if "blocked" in (target, moved_to):
        return True
    if target == "ready" and _member(event, "transition", "source") in ACTIVE_STATES:
        return True
    if moved_to == "ready" and _member(event, "payload", "from") in ACTIVE_STATES:
        return True
    if "in_progress" in (target, moved_to) and "gate-red" in (_member(event, "request_id") or ""):
        return True
    return kind == "created" and bool(
        {_member(event, "payload", "budget_event"), _member(event, "data", "budget_event")}
        & set(_CREATED_BUDGET_EVENTS)
    )


def identity(event: dict[str, Any]) -> str:
    return _member(event, "event_id") or _member(event, "request_id") or ""


__all__ = ["CANDIDATE_PREDICATE", "CHARGE_PREFIX", "IDENTITY", "identity", "is_candidate"]
