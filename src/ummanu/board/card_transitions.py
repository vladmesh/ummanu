"""Role-aware Card lifecycle authorization.

This leaf deliberately depends only on normalized board values and lifecycle declarations.
Legacy writers can therefore ask it for authority without importing a board adapter.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeAlias

from ummanu.board import wait_card
from ummanu.board.models import CardState, EntityKind
from ummanu.board.roles import Role
from ummanu.board.transitions import TRANSITIONS, BoardProtocolError, Transition, transition_for

CardTransitionKey: TypeAlias = tuple[CardState, CardState]


class CardTransitionForbidden(BoardProtocolError):
    """A role is not authorized for a requested Card lifecycle edge."""


class CardMoveReasonRequired(BoardProtocolError):
    """The role holds the edge only with a stated reason, and none was given."""


_CARD_STATES = tuple(CardState)

#: The stages of a round. Only the dispatcher moves a card into one.
DISPATCHER_STAGES: frozenset[CardState] = frozenset(
    {CardState.IN_PROGRESS, CardState.VALIDATE, CardState.CI, CardState.REVIEW, CardState.ASSESSMENT}
)

#: Roles whose exits out of Assessment require a recorded decision.
DECISION_BOUND_ROLES: frozenset[Role] = frozenset({Role.DISPATCHER})

# The one role authority registry; lifecycle declarations remain in ``board.transitions``.
CARD_TRANSITIONS: dict[Role, frozenset[CardTransitionKey]] = {
    Role.PO: frozenset(
        (source, target)
        for source in _CARD_STATES
        for target in _CARD_STATES
        if (source, target) in TRANSITIONS[EntityKind.CARD] and target not in DISPATCHER_STAGES
    ),
    Role.DISPATCHER: frozenset(
        {
            (CardState.READY, CardState.IN_PROGRESS),
            (CardState.IN_PROGRESS, CardState.DONE),
            (CardState.READY, CardState.BLOCKED),
            (CardState.IN_PROGRESS, CardState.VALIDATE),
            (CardState.IN_PROGRESS, CardState.CI),
            (CardState.CI, CardState.REVIEW),
            (CardState.CI, CardState.ASSESSMENT),
            (CardState.REVIEW, CardState.ASSESSMENT),
            (CardState.CI, CardState.IN_PROGRESS),
            (CardState.REVIEW, CardState.IN_PROGRESS),
            (CardState.CI, CardState.READY),
            (CardState.REVIEW, CardState.READY),
            (CardState.CI, CardState.BLOCKED),
            (CardState.REVIEW, CardState.BLOCKED),
            (CardState.IN_PROGRESS, CardState.BLOCKED),
            (CardState.IN_PROGRESS, CardState.READY),
            (CardState.IN_PROGRESS, CardState.ASSESSMENT),
            (CardState.VALIDATE, CardState.IN_PROGRESS),
            (CardState.VALIDATE, CardState.BLOCKED),
            (CardState.VALIDATE, CardState.DONE),
            (CardState.VALIDATE, CardState.CI),
            (CardState.VALIDATE, CardState.REVIEW),
            (CardState.VALIDATE, CardState.ASSESSMENT),
            (CardState.ASSESSMENT, CardState.IN_PROGRESS),
            (CardState.ASSESSMENT, CardState.DONE),
            (CardState.ASSESSMENT, CardState.BLOCKED),
        }
    ),
    # The observer decides about a parked card and the dispatcher performs the decision.
    Role.OBSERVER: frozenset(
        (source, target)
        for source in _CARD_STATES
        for target in _CARD_STATES
        if (source, target) in TRANSITIONS[EntityKind.CARD]
        and source is not CardState.ASSESSMENT
        and target not in DISPATCHER_STAGES
    ),
    Role.WORKER: frozenset(),
    Role.REVIEWER: frozenset(),
    Role.RETRO: frozenset(),
    Role.STEWARD: frozenset(
        {
            (CardState.BLOCKED, CardState.READY),
            (CardState.BLOCKED, CardState.DONE),
            (CardState.IN_PROGRESS, CardState.DONE),
            (CardState.READY, CardState.BLOCKED),
            (CardState.IN_PROGRESS, CardState.BLOCKED),
            (CardState.VALIDATE, CardState.BLOCKED),
            (CardState.CI, CardState.BLOCKED),
            (CardState.REVIEW, CardState.BLOCKED),
            (CardState.ASSESSMENT, CardState.BLOCKED),
        }
    ),
}


@dataclass(frozen=True, slots=True)
class CardMoveFacts:
    """The card facts a conditional edge reads; a callable fact is read only by the edge that needs it."""

    reason: str
    steward_report: bool
    wait_outcome: Callable[[], str | None]
    held_by_wait_card: Callable[[], bool]
    after_merge_hotfix: Callable[[], bool]


EdgeCondition: TypeAlias = Callable[[CardMoveFacts], None]


def _reason_required(message: str) -> EdgeCondition:
    def condition(facts: CardMoveFacts) -> None:
        if not facts.reason.strip():
            raise CardMoveReasonRequired(message)

    return condition


def _steward_closes_own_report(facts: CardMoveFacts) -> None:
    if not facts.steward_report:
        raise CardTransitionForbidden("steward may close In progress only for its own report card")


def _wait_card_finished(facts: CardMoveFacts) -> None:
    if facts.wait_outcome() not in (wait_card.TARGET_REACHED, wait_card.CANCELLED):
        raise CardTransitionForbidden(
            "the dispatcher moves an In progress card to Done only as a wait card's "
            "target_reached or cancelled"
        )


def _held_by_ended_wait(facts: CardMoveFacts) -> None:
    # The dispatcher's own hotfix card for a red after-merge e2e run has no other owner to Block it.
    if facts.after_merge_hotfix() or facts.held_by_wait_card():
        return
    raise CardTransitionForbidden(
        "the dispatcher Blocks a Ready card only when a wait card it is blocked by ended unreached"
    )


_STEWARD_REASON = _reason_required("this steward transition requires a non-empty reason")
_OBSERVER_REASON = _reason_required("moving a card out of Blocked requires a non-empty reason")

#: The conditions a role's edge carries beyond holding it; an edge absent here is unconditional.
CARD_EDGE_CONDITIONS: dict[tuple[Role, CardTransitionKey], tuple[EdgeCondition, ...]] = {
    **{
        (Role.STEWARD, edge): (_STEWARD_REASON,)
        for edge in CARD_TRANSITIONS[Role.STEWARD]
        if edge[1] is CardState.BLOCKED or edge == (CardState.BLOCKED, CardState.DONE)
    },
    (Role.STEWARD, (CardState.IN_PROGRESS, CardState.DONE)): (_steward_closes_own_report,),
    **{
        (Role.OBSERVER, edge): (_OBSERVER_REASON,)
        for edge in CARD_TRANSITIONS[Role.OBSERVER]
        if edge[0] is CardState.BLOCKED
    },
    (Role.DISPATCHER, (CardState.IN_PROGRESS, CardState.DONE)): (_wait_card_finished,),
    (Role.DISPATCHER, (CardState.READY, CardState.BLOCKED)): (_held_by_ended_wait,),
}


def forbidden_move_message(role: str, source: str, target: str) -> str:
    """Why ``role`` may not take this edge, naming the handle it uses instead."""
    edge = f"{role} may not move {source} to {target}"
    if role == Role.DISPATCHER.value:
        return edge
    if source == CardState.ASSESSMENT.value:
        return f"{edge}: the card is parked; record a decision with `task decide` and the dispatcher performs it"
    if target not in {stage.value for stage in DISPATCHER_STAGES}:
        return edge
    only = f"{edge}: only the dispatcher moves a card into In progress, Validate, CI, Review or Assessment"
    if source == CardState.BLOCKED.value:
        return (
            f"{only}; return the card to Ready and its round continues at the stage it stopped, "
            "or record `task decide --kind rework`"
        )
    return f"{only}; withdraw a card with `task cancel` or `task supersede`"


def card_transition(role: Role | str, source: CardState | str, target: CardState | str) -> Transition:
    """Return the declared lifecycle transition when ``role`` holds the Card edge."""
    try:
        normalized_role = Role(role)
        source_state = CardState(source)
        target_state = CardState(target)
    except ValueError as exc:
        raise CardTransitionForbidden(forbidden_move_message(str(role), str(source), str(target))) from exc
    if (source_state, target_state) not in CARD_TRANSITIONS[normalized_role]:
        raise CardTransitionForbidden(
            forbidden_move_message(normalized_role.value, source_state.value, target_state.value)
        )
    return transition_for(EntityKind.CARD, source_state, target_state)


def admit_card_move(
    role: Role | str, source: CardState | str, target: CardState | str, facts: CardMoveFacts
) -> Transition:
    """Admit one requested move: the role holds the edge and the edge's conditions hold."""
    declaration = card_transition(role, source, target)
    for condition in CARD_EDGE_CONDITIONS.get((Role(role), (CardState(source), CardState(target))), ()):
        condition(facts)
    return declaration
