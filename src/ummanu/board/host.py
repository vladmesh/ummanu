"""Backend-neutral host contract for normalized board entities."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from ummanu.board.marker_payload import MarkerPayload, marker_payload_from_data
from ummanu.board.models import (
    Actor,
    BoardEntity,
    EntityKind,
    EntityRef,
    Event,
    EventKind,
    RelatedRefs,
)
from ummanu.board.transitions import LifecycleState


@dataclass(frozen=True, slots=True)
class Create:
    entity: BoardEntity
    actor: Actor
    reason: str
    related_refs: RelatedRefs = field(default_factory=RelatedRefs)
    request_id: str | None = None


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DESCRIPTION_APPEND_KEYS = ("body_sha256", "description_sha256_was", "description_sha256")


@dataclass(frozen=True, slots=True)
class DescriptionAppend:
    """The evidence of an Issue description append: an added block.

    The old text stays byte for byte and the block goes after it.  The replaced Issue already carries
    the new description; these digests are the identity of the request, because once the block is on
    the board the description alone cannot tell a replay of the same request from a second append.
    """

    body_sha256: str
    description_sha256_was: str
    description_sha256: str

    def __post_init__(self) -> None:
        for name in _DESCRIPTION_APPEND_KEYS:
            value = getattr(self, name)
            if not isinstance(value, str) or not _SHA256.fullmatch(value):
                raise ValueError(f"description append {name} must be a SHA-256 hex digest")
        if self.description_sha256 == self.description_sha256_was:
            raise ValueError("description append must change the description")

    def event_data(self) -> dict[str, str]:
        return {name: getattr(self, name) for name in _DESCRIPTION_APPEND_KEYS}

    @classmethod
    def from_event_data(cls, data: object) -> DescriptionAppend:
        if not isinstance(data, dict) or set(data) != set(_DESCRIPTION_APPEND_KEYS):
            raise ValueError("description append evidence must carry exactly its three digests")
        return cls(*(data[name] for name in _DESCRIPTION_APPEND_KEYS))


@dataclass(frozen=True, slots=True)
class DescriptionEdit:
    """Exact before/after evidence for a full Issue description replacement."""

    description_sha256_was: str
    description_sha256: str

    def __post_init__(self) -> None:
        for value in (self.description_sha256_was, self.description_sha256):
            if not isinstance(value, str) or not _SHA256.fullmatch(value):
                raise ValueError("description edit evidence must be SHA-256 hex digests")

    def event_data(self) -> dict[str, str]:
        return {"description_sha256_was": self.description_sha256_was,
                "description_sha256": self.description_sha256}

    @classmethod
    def from_event_data(cls, data: object) -> DescriptionEdit:
        if not isinstance(data, dict) or set(data) != {"description_sha256_was", "description_sha256"}:
            raise ValueError("description edit evidence must carry exactly its two digests")
        return cls(data["description_sha256_was"], data["description_sha256"])


@dataclass(frozen=True, slots=True)
class Replace:
    entity: BoardEntity
    actor: Actor
    reason: str
    related_refs: RelatedRefs = field(default_factory=RelatedRefs)
    request_id: str | None = None
    # Explicit description operations carry their own evidence; without either, an Issue
    # replace is the released priority change.
    description_append: DescriptionAppend | None = None
    description_edit: DescriptionEdit | None = None


@dataclass(frozen=True, slots=True)
class SprintSupplement:
    """The only non-state values a migrated Sprint edge may persist.

    These are domain values, not a board metadata bag.  The adapter owns
    their storage spelling and rejects combinations that do not belong to an
    edge, so callers cannot tunnel unrelated sprint fields through a lifecycle
    transition.
    """

    observer: str | None = None
    budget_by_type: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        if self.observer is not None and (not isinstance(self.observer, str) or not self.observer):
            raise ValueError("Sprint observer must be a non-empty string")
        seen: set[str] = set()
        for name, count in self.budget_by_type:
            if not isinstance(name, str) or not name or name in seen:
                raise ValueError("Sprint budget types must be unique non-empty strings")
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise ValueError("Sprint budget counts must be non-negative integers")
            seen.add(name)

    def event_data(self) -> dict[str, object]:
        result: dict[str, object] = {}
        if self.observer is not None:
            result["observer"] = self.observer
        if self.budget_by_type:
            result["budget_by_type"] = dict(self.budget_by_type)
        return result


@dataclass(frozen=True, slots=True)
class TransitionRequest:
    kind: EntityKind
    ref: EntityRef
    target: LifecycleState
    actor: Actor
    reason: str
    related_refs: RelatedRefs = field(default_factory=RelatedRefs)
    request_id: str | None = None
    sprint: SprintSupplement | None = None
    # A transition may carry immutable, non-control-plane evidence that is
    # needed to repair observational work after the effect is confirmed.
    # Card outcome obligations are deliberately kept with the effect they
    # describe, rather than in a second dispatcher-state journal.
    data: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.data, dict):
            raise ValueError("transition data must be an object")


@dataclass(frozen=True, slots=True, init=False)
class MarkerComment:
    """One control-plane comment expressed as a typed marker payload.

    Callers may still hand over the released dictionary shape.  The host
    boundary normalizes it immediately to one of the closed marker payload
    values; adapters serialize it back only when they stage the board event.
    """

    ref: EntityRef
    kind: EventKind
    actor: Actor
    reason: str
    payload: MarkerPayload
    related_refs: RelatedRefs
    request_id: str | None
    fresh_admission: Callable[[], None] | None

    def __init__(
        self,
        ref: EntityRef,
        kind: EventKind,
        actor: Actor,
        reason: str,
        data: MarkerPayload | dict[str, object],
        related_refs: RelatedRefs = RelatedRefs(),
        request_id: str | None = None,
        fresh_admission: Callable[[], None] | None = None,
    ) -> None:
        if not isinstance(ref, str) or not ref.strip():
            raise ValueError("marker Card ref must be a non-empty string")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("marker reason must be a non-empty string")
        if kind not in {EventKind.CARD_REPORTED, EventKind.CARD_VERDICTED, EventKind.CARD_DECIDED}:
            raise ValueError("marker comment kind must be a declared control-plane event kind")
        payload = data
        if isinstance(data, dict):
            payload = marker_payload_from_data(kind.value, reason, data)
        object.__setattr__(self, "ref", ref)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "actor", actor)
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "payload", payload)
        object.__setattr__(self, "related_refs", related_refs)
        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "fresh_admission", fresh_admission)

    @property
    def data(self) -> dict[str, object]:
        """Released adapter projection of the typed payload."""
        return self.payload.to_event_data()


@dataclass(frozen=True, slots=True)
class MutationResult:
    entity: BoardEntity
    event: Event
    replayed: bool = False


class BoardHost(Protocol):
    """The protocol seam.  Its values never expose backend row dictionaries."""

    def read(self, kind: EntityKind, ref: EntityRef) -> BoardEntity: ...

    def list(self, kind: EntityKind) -> Sequence[BoardEntity]: ...

    def create(self, operation: Create) -> MutationResult: ...

    def replace(self, operation: Replace) -> MutationResult: ...

    def transition(self, operation: TransitionRequest) -> MutationResult: ...

    def marker_comment(self, operation: MarkerComment) -> MutationResult: ...
