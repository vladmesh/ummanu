"""Normalized board protocol foundation.

This package is additive in phase one.  Existing task, sprint, and Product/Issue
command paths retain their current writers until their dedicated migration cards.
"""

from ummanu.board.card_transitions import (
    CARD_TRANSITIONS,
    CardTransitionForbidden,
    card_transition,
)
from ummanu.board.events import (
    AnalyticsOutcomeConflict,
    AttemptOutcomeOccurrence,
    AttemptUsageOccurrence,
    BoardEventCanon,
    BoardEventPending,
    MutationEventTransaction,
)
from ummanu.board.fake import FakeBoardHost
from ummanu.board.host import (
    BoardHost,
    Create,
    DescriptionAppend,
    DescriptionEdit,
    MarkerComment,
    MutationResult,
    Replace,
    SprintSupplement,
    TransitionRequest,
)
from ummanu.board.models import (
    Actor,
    BoardEntity,
    Card,
    CardState,
    EntityKind,
    Event,
    EventKind,
    Issue,
    IssueCloseReason,
    IssueKind,
    IssuePriority,
    IssueState,
    Product,
    ProductState,
    RelatedRefs,
    Sprint,
    SprintState,
)
from ummanu.board.roles import Role
from ummanu.board.task_routing import (
    BlockClassification,
    FamilyPreference,
    RoutingPhase,
    TaskComplexity,
    TaskDecision,
    TaskMetadata,
    TaskRouting,
    TaskType,
)
from ummanu.board.transitions import (
    TRANSITIONS,
    BoardProtocolError,
    InvalidTransition,
    transition,
)


def __getattr__(name: str):
    """Keep legacy adapters out of imports of board's protocol leaves."""
    if name == "SqlBoardHost":
        from ummanu.board.sql_host import SqlBoardHost

        return SqlBoardHost
    raise AttributeError(name)


__all__ = [
    "CARD_TRANSITIONS",
    "TRANSITIONS",
    "Actor",
    "AnalyticsOutcomeConflict",
    "AttemptOutcomeOccurrence",
    "AttemptUsageOccurrence",
    "BlockClassification",
    "BoardEntity",
    "BoardEventCanon",
    "BoardEventPending",
    "BoardHost",
    "BoardProtocolError",
    "Card",
    "CardState",
    "CardTransitionForbidden",
    "Create",
    "DescriptionAppend",
    "DescriptionEdit",
    "EntityKind",
    "Event",
    "EventKind",
    "FakeBoardHost",
    "FamilyPreference",
    "InvalidTransition",
    "Issue",
    "IssueCloseReason",
    "IssueKind",
    "IssuePriority",
    "IssueState",
    "MarkerComment",
    "MutationEventTransaction",
    "MutationResult",
    "Product",
    "ProductState",
    "RelatedRefs",
    "Replace",
    "Role",
    "RoutingPhase",
    "Sprint",
    "SprintState",
    "SprintSupplement",
    "SqlBoardHost",
    "TaskComplexity",
    "TaskDecision",
    "TaskMetadata",
    "TaskRouting",
    "TaskType",
    "TransitionRequest",
    "card_transition",
    "transition",
]
