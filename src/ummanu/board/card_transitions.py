"""Role-aware Card lifecycle authorization.

This leaf deliberately depends only on normalized board values and lifecycle declarations.
Legacy writers can therefore ask it for authority without importing a board adapter.
"""

from __future__ import annotations

from typing import TypeAlias

from ummanu.board.models import CardState, EntityKind
from ummanu.board.roles import Role
from ummanu.board.transitions import BoardProtocolError, Transition, transition_for

CardTransitionKey: TypeAlias = tuple[CardState, CardState]


class CardTransitionForbidden(BoardProtocolError):
    """A role is not authorized for a requested Card lifecycle edge."""


_CARD_STATES = tuple(CardState)

# This is the compatibility contract observed from TaskWriter's former matrix.  It is the one
# role authority registry; lifecycle declarations remain in ``board.transitions``.
CARD_TRANSITIONS: dict[Role, frozenset[CardTransitionKey]] = {
    Role.PO: frozenset(
        (source, target) for source in _CARD_STATES for target in _CARD_STATES if source != target
    ),
    Role.DISPATCHER: frozenset(
        {
            # Claim is a dispatcher-owned Ready-to-In progress lifecycle edge.  It
            # used to bypass this registry through TaskWriter's raw column move.
            (CardState.READY, CardState.IN_PROGRESS),
            # A wait card's `target_reached` (secretary-1790). `TaskWriter.move` admits this edge
            # for the dispatcher on a wait card only.
            (CardState.IN_PROGRESS, CardState.DONE),
            # A wait card's other outcome Blocks the Ready cards it holds (`blocked_by`); admitted
            # by `TaskWriter.move` only for a card whose `blocked_by` names a wait card.
            (CardState.READY, CardState.BLOCKED),
            (CardState.IN_PROGRESS, CardState.VALIDATE),
            (CardState.IN_PROGRESS, CardState.BLOCKED),
            (CardState.IN_PROGRESS, CardState.READY),
            # A worker's partial report parks the card for the observer's decision.
            (CardState.IN_PROGRESS, CardState.ASSESSMENT),
            (CardState.VALIDATE, CardState.IN_PROGRESS),
            (CardState.VALIDATE, CardState.BLOCKED),
            (CardState.VALIDATE, CardState.DONE),
            (CardState.VALIDATE, CardState.ASSESSMENT),
            (CardState.ASSESSMENT, CardState.IN_PROGRESS),
            (CardState.ASSESSMENT, CardState.DONE),
            (CardState.ASSESSMENT, CardState.BLOCKED),
        }
    ),
    Role.OBSERVER: frozenset(
        (source, target)
        for source in _CARD_STATES
        for target in _CARD_STATES
        if source != target and source is not CardState.ASSESSMENT
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
            (CardState.ASSESSMENT, CardState.BLOCKED),
        }
    ),
}


def card_transition(role: Role | str, source: CardState | str, target: CardState | str) -> Transition:
    """Return the declared lifecycle transition when ``role`` owns the Card edge."""
    try:
        normalized_role = Role(role)
        source_state = CardState(source)
        target_state = CardState(target)
    except ValueError as exc:
        raise CardTransitionForbidden(f"{role} cannot transition a Card from {source} to {target}") from exc
    if (source_state, target_state) not in CARD_TRANSITIONS.get(normalized_role, frozenset()):
        raise CardTransitionForbidden(
            f"{role} cannot transition a Card from {source_state.value} to {target_state.value}"
        )
    return transition_for(EntityKind.CARD, source_state, target_state)
