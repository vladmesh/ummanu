"""The board protocol's one lifecycle transition registry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

from ummanu.board.models import (
    BoardEntity,
    Card,
    CardState,
    EntityKind,
    EventKind,
    Issue,
    IssueState,
    Product,
    ProductState,
    Sprint,
    SprintState,
)


class BoardProtocolError(Exception):
    """A requested board-protocol operation is not valid."""


class InvalidTransition(BoardProtocolError):
    """The lifecycle registry has no edge for the requested move."""


LifecycleState: TypeAlias = ProductState | IssueState | SprintState | CardState
TransitionKey: TypeAlias = tuple[LifecycleState, LifecycleState]


@dataclass(frozen=True, slots=True)
class Transition:
    source: LifecycleState
    target: LifecycleState
    event_kind: EventKind


# This is intentionally a literal registry, not an inferred graph.  Adding an edge
# requires naming the semantic event it emits, which is the phase-one completeness
# check the later event canon will rely on.
TRANSITIONS: dict[EntityKind, dict[TransitionKey, Transition]] = {
    EntityKind.PRODUCT: {
        (ProductState.ACTIVE, ProductState.ARCHIVED): Transition(
            ProductState.ACTIVE, ProductState.ARCHIVED, EventKind.PRODUCT_ARCHIVED
        ),
    },
    EntityKind.ISSUE: {
        (IssueState.OPEN, IssueState.CLOSED): Transition(
            IssueState.OPEN, IssueState.CLOSED, EventKind.ISSUE_CLOSED
        ),
        (IssueState.CLOSED, IssueState.OPEN): Transition(
            IssueState.CLOSED, IssueState.OPEN, EventKind.ISSUE_REOPENED
        ),
    },
    EntityKind.SPRINT: {
        (SprintState.OPEN, SprintState.CLOSED): Transition(
            SprintState.OPEN, SprintState.CLOSED, EventKind.SPRINT_CLOSED
        ),
        # A hard-budget stop blocks ordinary sprint work, but it has always
        # remained closeable.  Closing it retains its terminal status while
        # completing the close facade's archival and reservation work.
        (SprintState.STOPPED, SprintState.CLOSED): Transition(
            SprintState.STOPPED, SprintState.CLOSED, EventKind.SPRINT_CLOSED
        ),
        (SprintState.OPEN, SprintState.STOPPED): Transition(
            SprintState.OPEN, SprintState.STOPPED, EventKind.SPRINT_STOPPED
        ),
        # A live close may be reopened after the command facade rechecks the
        # same admission rules as create.  It is an explicit released edge,
        # not an implicit exception to the registry.
        (SprintState.CLOSED, SprintState.OPEN): Transition(
            SprintState.CLOSED, SprintState.OPEN, EventKind.SPRINT_REOPENED
        ),
        (SprintState.STOPPED, SprintState.OPEN): Transition(
            SprintState.STOPPED, SprintState.OPEN, EventKind.SPRINT_REOPENED
        ),
    },
    EntityKind.CARD: {
        (CardState.ISSUES, CardState.READY): Transition(
            CardState.ISSUES, CardState.READY, EventKind.CARD_READY
        ),
        (CardState.ISSUES, CardState.IN_PROGRESS): Transition(
            CardState.ISSUES, CardState.IN_PROGRESS, EventKind.CARD_MOVED
        ),
        (CardState.ISSUES, CardState.VALIDATE): Transition(
            CardState.ISSUES, CardState.VALIDATE, EventKind.CARD_MOVED
        ),
        (CardState.ISSUES, CardState.ASSESSMENT): Transition(
            CardState.ISSUES, CardState.ASSESSMENT, EventKind.CARD_MOVED
        ),
        (CardState.ISSUES, CardState.BLOCKED): Transition(
            CardState.ISSUES, CardState.BLOCKED, EventKind.CARD_BLOCKED
        ),
        (CardState.ISSUES, CardState.DONE): Transition(
            CardState.ISSUES, CardState.DONE, EventKind.CARD_MOVED
        ),
        (CardState.READY, CardState.ISSUES): Transition(
            CardState.READY, CardState.ISSUES, EventKind.CARD_MOVED
        ),
        (CardState.READY, CardState.IN_PROGRESS): Transition(
            CardState.READY, CardState.IN_PROGRESS, EventKind.CARD_STARTED
        ),
        (CardState.READY, CardState.VALIDATE): Transition(
            CardState.READY, CardState.VALIDATE, EventKind.CARD_MOVED
        ),
        (CardState.READY, CardState.ASSESSMENT): Transition(
            CardState.READY, CardState.ASSESSMENT, EventKind.CARD_MOVED
        ),
        (CardState.READY, CardState.BLOCKED): Transition(
            CardState.READY, CardState.BLOCKED, EventKind.CARD_BLOCKED
        ),
        (CardState.READY, CardState.DONE): Transition(CardState.READY, CardState.DONE, EventKind.CARD_MOVED),
        (CardState.IN_PROGRESS, CardState.ISSUES): Transition(
            CardState.IN_PROGRESS, CardState.ISSUES, EventKind.CARD_MOVED
        ),
        (CardState.IN_PROGRESS, CardState.READY): Transition(
            CardState.IN_PROGRESS, CardState.READY, EventKind.CARD_RETURNED
        ),
        (CardState.IN_PROGRESS, CardState.VALIDATE): Transition(
            CardState.IN_PROGRESS, CardState.VALIDATE, EventKind.CARD_SUBMITTED
        ),
        (CardState.IN_PROGRESS, CardState.ASSESSMENT): Transition(
            CardState.IN_PROGRESS, CardState.ASSESSMENT, EventKind.CARD_MOVED
        ),
        (CardState.IN_PROGRESS, CardState.BLOCKED): Transition(
            CardState.IN_PROGRESS, CardState.BLOCKED, EventKind.CARD_BLOCKED
        ),
        (CardState.IN_PROGRESS, CardState.DONE): Transition(
            CardState.IN_PROGRESS, CardState.DONE, EventKind.CARD_MOVED
        ),
        (CardState.VALIDATE, CardState.ISSUES): Transition(
            CardState.VALIDATE, CardState.ISSUES, EventKind.CARD_MOVED
        ),
        (CardState.VALIDATE, CardState.READY): Transition(
            CardState.VALIDATE, CardState.READY, EventKind.CARD_MOVED
        ),
        (CardState.VALIDATE, CardState.IN_PROGRESS): Transition(
            CardState.VALIDATE, CardState.IN_PROGRESS, EventKind.CARD_RETURNED
        ),
        (CardState.VALIDATE, CardState.ASSESSMENT): Transition(
            CardState.VALIDATE, CardState.ASSESSMENT, EventKind.CARD_ASSESSED
        ),
        (CardState.VALIDATE, CardState.BLOCKED): Transition(
            CardState.VALIDATE, CardState.BLOCKED, EventKind.CARD_BLOCKED
        ),
        (CardState.VALIDATE, CardState.DONE): Transition(
            CardState.VALIDATE, CardState.DONE, EventKind.CARD_MOVED
        ),
        (CardState.ASSESSMENT, CardState.ISSUES): Transition(
            CardState.ASSESSMENT, CardState.ISSUES, EventKind.CARD_MOVED
        ),
        (CardState.ASSESSMENT, CardState.READY): Transition(
            CardState.ASSESSMENT, CardState.READY, EventKind.CARD_MOVED
        ),
        (CardState.ASSESSMENT, CardState.IN_PROGRESS): Transition(
            CardState.ASSESSMENT, CardState.IN_PROGRESS, EventKind.CARD_REWORKED
        ),
        (CardState.ASSESSMENT, CardState.VALIDATE): Transition(
            CardState.ASSESSMENT, CardState.VALIDATE, EventKind.CARD_MOVED
        ),
        (CardState.ASSESSMENT, CardState.DONE): Transition(
            CardState.ASSESSMENT, CardState.DONE, EventKind.CARD_RELEASED
        ),
        (CardState.ASSESSMENT, CardState.BLOCKED): Transition(
            CardState.ASSESSMENT, CardState.BLOCKED, EventKind.CARD_BLOCKED
        ),
        (CardState.BLOCKED, CardState.ISSUES): Transition(
            CardState.BLOCKED, CardState.ISSUES, EventKind.CARD_MOVED
        ),
        (CardState.BLOCKED, CardState.READY): Transition(
            CardState.BLOCKED, CardState.READY, EventKind.CARD_UNBLOCKED
        ),
        (CardState.BLOCKED, CardState.IN_PROGRESS): Transition(
            CardState.BLOCKED, CardState.IN_PROGRESS, EventKind.CARD_MOVED
        ),
        (CardState.BLOCKED, CardState.VALIDATE): Transition(
            CardState.BLOCKED, CardState.VALIDATE, EventKind.CARD_MOVED
        ),
        (CardState.BLOCKED, CardState.ASSESSMENT): Transition(
            CardState.BLOCKED, CardState.ASSESSMENT, EventKind.CARD_MOVED
        ),
        (CardState.BLOCKED, CardState.DONE): Transition(
            CardState.BLOCKED, CardState.DONE, EventKind.CARD_MOVED
        ),
        (CardState.DONE, CardState.ISSUES): Transition(
            CardState.DONE, CardState.ISSUES, EventKind.CARD_MOVED
        ),
        (CardState.DONE, CardState.READY): Transition(CardState.DONE, CardState.READY, EventKind.CARD_MOVED),
        (CardState.DONE, CardState.IN_PROGRESS): Transition(
            CardState.DONE, CardState.IN_PROGRESS, EventKind.CARD_MOVED
        ),
        (CardState.DONE, CardState.VALIDATE): Transition(
            CardState.DONE, CardState.VALIDATE, EventKind.CARD_MOVED
        ),
        (CardState.DONE, CardState.ASSESSMENT): Transition(
            CardState.DONE, CardState.ASSESSMENT, EventKind.CARD_MOVED
        ),
        (CardState.DONE, CardState.BLOCKED): Transition(
            CardState.DONE, CardState.BLOCKED, EventKind.CARD_BLOCKED
        ),
    },
}


def lifecycle_state(entity: BoardEntity) -> LifecycleState:
    return entity.state


def transition_for(kind: EntityKind, source: LifecycleState, target: LifecycleState) -> Transition:
    try:
        return TRANSITIONS[kind][(source, target)]
    except KeyError as exc:
        raise InvalidTransition(
            f"{kind.value} cannot transition from {source.value} to {target.value}"
        ) from exc


def transition(entity: BoardEntity, target: LifecycleState) -> tuple[BoardEntity, Transition]:
    """Return the checked successor and the event declaration for its edge."""
    declaration = transition_for(entity.kind, lifecycle_state(entity), target)
    if isinstance(entity, Product) and isinstance(target, ProductState):
        return Product(entity.ref, entity.title, target, entity.projects, entity.description), declaration
    if isinstance(entity, Issue) and isinstance(target, IssueState):
        return Issue(
            entity.ref,
            entity.title,
            entity.product_ref,
            target,
            entity.priority,
            entity.issue_kind,
            entity.description,
            entity.close_reason,
        ), declaration
    if isinstance(entity, Sprint) and isinstance(target, SprintState):
        return Sprint(
            entity.ref, entity.goal, target, entity.product_ref, entity.issue_refs, entity.card_refs
        ), declaration
    if isinstance(entity, Card) and isinstance(target, CardState):
        return Card(
            entity.ref,
            entity.title,
            target,
            entity.sprint_ref,
            entity.issue_refs,
            entity.product_ref,
            entity.description,
        ), declaration
    raise InvalidTransition(f"{entity.kind.value} cannot transition to {target!r}")
