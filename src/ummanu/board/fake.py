"""In-memory BoardHost implementation for contract tests."""

from __future__ import annotations

import copy
import threading
import uuid
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from typing import Any, TypeVar

from ummanu.board.audit_contract import is_protocol_event
from ummanu.board.events import BoardEventCanon, MutationEventTransaction, event_id
from ummanu.board.host import Create, MutationResult, Replace, TransitionRequest
from ummanu.board.models import BoardEntity, EntityKind, Event, EventKind
from ummanu.board.transitions import (
    BoardProtocolError,
    InvalidTransition,
    transition,
)

T = TypeVar("T", bound=BoardEntity)

_FOREIGN_CLAIM = "request id belongs to another operation or payload"


def _foreign_claim() -> Exception:
    from ummanu.task_errors import TaskError

    return TaskError("validation", _FOREIGN_CLAIM, 2)


class MemoryAudit:
    """An in-memory audit owner: the small contract :class:`BoardEventCanon` uses, and no file.

    One request id owns one record, staged and then committed, under one lock, and one event id
    belongs to one request -- the claim rules the card audit (`SqlTaskAudit`) keeps in `requests`.
    Several hosts may share one instance, which is how a test rebuilds a host over the same
    history. Nothing here survives the process.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._committed: dict[str, dict[str, Any]] = {}
        self._pending: dict[str, dict[str, Any]] = {}

    def claim(
        self,
        request_id: str,
        event: dict[str, Any],
        *,
        verify: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any] | None:
        """Return the record that already owns `request_id`, or stage `event` as its owner."""
        with self._lock:
            existing = self._committed.get(request_id) or self._pending.get(request_id)
            if existing is not None:
                if existing != event:
                    raise _foreign_claim()
                return copy.deepcopy(existing)
            if verify is not None:
                verify(event)
            self._pending[request_id] = copy.deepcopy(event)
            return None

    def append(self, request_id: str, event: dict[str, Any], *, restoring: bool = False) -> str:
        # `restoring` is `SqlTaskAudit.append`'s: this audit keeps no origin-return outbox.
        with self._lock:
            owner = self._committed.get(request_id) or self._pending.get(request_id)
            if owner is not None and owner != event:
                raise _foreign_claim()
            self._committed.setdefault(request_id, copy.deepcopy(event))
            self._pending.pop(request_id, None)
            return str(event["event_id"])

    def discard(self, request_id: str, event: dict[str, Any] | None = None) -> None:
        """Drop a staged record; a protocol one only when the caller names it exactly."""
        with self._lock:
            if request_id in self._committed:
                return
            pending = self._pending.get(request_id)
            if pending is None:
                return
            if (event is None and is_protocol_event(pending)) or (event is not None and pending != event):
                raise _foreign_claim()
            del self._pending[request_id]

    def committed_event(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            return copy.deepcopy(self._committed.get(request_id))

    def pending_event(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            return copy.deepcopy(self._pending.get(request_id))

    def event(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self.committed_event(request_id) or self.pending_event(request_id)

    def events(self, reference: str = "") -> list[dict[str, Any]]:
        """Committed records in append order, optionally for one ref."""
        with self._lock:
            return [
                copy.deepcopy(record)
                for record in self._committed.values()
                if not reference or record.get("ref") == reference
            ]

    def event_id_owner(self, event_id: str) -> str | None:
        with self._lock:
            for owners in (self._committed, self._pending):
                for request_id, record in owners.items():
                    if record.get("event_id") == event_id:
                        return request_id
            return None

    def _occurrence_projection_records(
        self, kinds: Iterable[str] | None = None, *, outcome_owed: bool = False
    ) -> list[tuple[dict[str, Any], bool]]:
        """Committed then staged records, read under one lock hold, narrowed as the card audit does."""
        from ummanu.tasks import _projection_slice

        with self._lock:
            records = [(copy.deepcopy(record), False) for record in self._committed.values()]
            records += [(copy.deepcopy(record), True) for record in self._pending.values()]
        if kinds is None:
            return records
        return _projection_slice(records, frozenset(str(kind) for kind in kinds), outcome_owed)

    def status(self) -> dict[str, int | bool]:
        with self._lock:
            return {"ok": not self._pending, "pending": len(self._pending)}


class FakeBoardHost:
    """A deterministic host with no board store, container, or network dependency.

    Passing ``audit`` (a :class:`MemoryAudit`, which several hosts may share) routes every mutation
    through the same typed event canon and transaction the real host uses. Without one the host is
    a compact in-memory contract double, but still refuses to complete a mutation without creating
    its event. Neither mode writes a file.
    """

    def __init__(
        self,
        entities: Sequence[BoardEntity] = (),
        *,
        audit: MemoryAudit | None = None,
    ) -> None:
        self._entities = {(entity.kind, entity.ref): entity for entity in entities}
        self.events: list[Event] = []
        self._requests: dict[str, Event] = {}
        self.canon = BoardEventCanon(audit) if audit is not None else None

    def read(self, kind: EntityKind, ref: str) -> BoardEntity:
        try:
            return self._entities[(kind, ref)]
        except KeyError as exc:
            raise BoardProtocolError(f"{kind.value} {ref!r} was not found") from exc

    def list(self, kind: EntityKind) -> Sequence[BoardEntity]:
        return tuple(
            sorted(
                (entity for (entity_kind, _), entity in self._entities.items() if entity_kind == kind),
                key=lambda entity: entity.ref,
            )
        )

    def create(self, operation: Create) -> MutationResult:
        entity = operation.entity

        def effect() -> BoardEntity:
            key = (entity.kind, entity.ref)
            if key in self._entities:
                raise BoardProtocolError(f"{entity.kind.value} {entity.ref!r} already exists")
            self._entities[key] = entity
            return entity

        return self._mutate(entity, EventKind.ENTITY_CREATED, operation, effect)

    def replace(self, operation: Replace) -> MutationResult:
        entity = operation.entity

        def effect() -> BoardEntity:
            key = (entity.kind, entity.ref)
            try:
                current = self._entities[key]
            except KeyError:
                raise BoardProtocolError(f"{entity.kind.value} {entity.ref!r} was not found") from None
            if current.state != entity.state:
                raise InvalidTransition(
                    f"replace cannot change {entity.kind.value} lifecycle state; use transition"
                )
            self._entities[key] = entity
            return entity

        return self._mutate(entity, EventKind.ENTITY_UPDATED, operation, effect)

    def transition(self, operation: TransitionRequest) -> MutationResult:
        entity = self.read(operation.kind, operation.ref)
        if operation.request_id:
            existing = (
                self.canon.event(operation.request_id)
                if self.canon is not None
                else self._requests.get(operation.request_id)
            )
            if existing is not None:
                self._require_same_operation(existing, entity, existing.kind, operation)
                if self.canon is None:
                    return MutationResult(entity, existing)
                result = MutationEventTransaction(
                    self.canon,
                    request_id=operation.request_id,
                    event=existing,
                ).execute(
                    lambda: (_ for _ in ()).throw(
                        BoardProtocolError("pending fake transition must not repeat its effect")
                    ),
                    confirm=lambda: self._confirm_transition(operation),
                )
                if existing not in self.events:
                    self.events.append(existing)
                return MutationResult(result, existing)
        successor, declaration = transition(entity, operation.target)

        def effect() -> BoardEntity:
            # The transition registry was checked before staging.  Re-read at
            # effect time so a concurrent fake mutation cannot be overwritten.
            current = self.read(operation.kind, operation.ref)
            checked, _ = transition(current, operation.target)
            self._entities[(checked.kind, checked.ref)] = checked
            return checked

        return self._mutate(successor, declaration.event_kind, operation, effect)

    def _mutate(
        self,
        entity: T,
        kind: EventKind,
        operation: Create | Replace | TransitionRequest,
        effect: Callable[[], T],
    ) -> MutationResult:
        event, request_id = self._event(entity, kind, operation)
        if self.canon is not None:
            result = MutationEventTransaction(
                self.canon,
                request_id=request_id,
                event=event,
            ).execute(effect, confirm=lambda: self.read(entity.kind, entity.ref))
        else:
            # Keep the lightweight test double deterministic while using the
            # same request-id ownership rule as its durable counterpart.
            existing = self._requests.get(request_id)
            if existing is not None:
                if existing != event:
                    raise ValueError("request id belongs to another operation or payload")
                result = self.read(entity.kind, entity.ref)
            else:
                result = effect()
                self._requests[request_id] = event
        if event not in self.events:
            self.events.append(event)
        return MutationResult(result, event)

    def _event(
        self,
        entity: BoardEntity,
        kind: EventKind,
        operation: Create | Replace | TransitionRequest,
    ) -> tuple[Event, str]:
        request_id = operation.request_id
        if request_id and self.canon is not None:
            existing = self.canon.event(request_id)
            if existing is not None:
                self._require_same_operation(existing, entity, kind, operation)
                return existing, request_id
        if request_id and request_id in self._requests:
            existing = self._requests[request_id]
            self._require_same_operation(existing, entity, kind, operation)
            return existing, request_id
        # A caller that declares no request id still gets its own idempotency key, instead
        # of borrowing one from the generated event id.
        request_id = request_id or f"fake-request-{uuid.uuid4().hex}"
        event = Event(
            self._event_id(request_id, entity, kind, operation),
            kind,
            entity.kind,
            entity.ref,
            operation.actor,
            operation.reason,
            datetime.now(UTC).replace(microsecond=0),
            operation.related_refs,
            self.read(operation.kind, operation.ref).state.value
            if isinstance(operation, TransitionRequest)
            else None,
            operation.target.value if isinstance(operation, TransitionRequest) else None,
            _supplement_data(operation),
        )
        return event, request_id

    def _event_id(
        self,
        request_id: str,
        entity: BoardEntity,
        kind: EventKind,
        operation: Create | Replace | TransitionRequest,
    ) -> str:
        """Derive a collision-resistant id from what the occurrence durably is.

        A per-host counter cannot do this: the audit outlives the host, so a
        recreated host would restart at one and publish a second `board-event-1` for an
        unrelated occurrence.  Digesting the request id together with the operation keeps a
        genuine same-request replay on its original id and separates everything else.
        """
        identity = {
            "request_id": request_id,
            "kind": kind.value,
            "entity_kind": entity.kind.value,
            "ref": entity.ref,
            "actor": [operation.actor.role, operation.actor.id, operation.actor.head_run_ref],
            "reason": operation.reason,
            "related_refs": list(operation.related_refs.refs),
            "source": self.read(operation.kind, operation.ref).state.value
            if isinstance(operation, TransitionRequest)
            else None,
            "target": operation.target.value if isinstance(operation, TransitionRequest) else None,
            "data": _supplement_data(operation),
        }
        return event_id(identity)

    @staticmethod
    def _require_same_operation(
        existing: Event,
        entity: BoardEntity,
        kind: EventKind,
        operation: Create | Replace | TransitionRequest,
    ) -> None:
        if (
            existing.kind is not kind
            or existing.entity_kind is not entity.kind
            or existing.ref != entity.ref
            or existing.actor != operation.actor
            or existing.reason != operation.reason
            or existing.related_refs != operation.related_refs
            or (
                isinstance(operation, TransitionRequest)
                and (
                    existing.target_state != operation.target.value
                    or existing.data != _supplement_data(operation)
                )
            )
        ):
            raise ValueError("request id belongs to another operation or payload")

    def _confirm_transition(self, operation: TransitionRequest) -> BoardEntity:
        entity = self.read(operation.kind, operation.ref)
        if entity.state is not operation.target:
            raise BoardProtocolError("fake transition is not proven")
        return entity


def _supplement_data(operation: Create | Replace | TransitionRequest) -> dict[str, object]:
    if isinstance(operation, TransitionRequest) and operation.sprint is not None:
        data = operation.sprint.event_data()
    else:
        data = {}
    if isinstance(operation, TransitionRequest):
        if not isinstance(operation.data, dict):
            raise ValueError("transition data must be an object")  # noqa: TRY004 - transition payload refusals are ValueError
        overlap = set(data).intersection(operation.data)
        if overlap:
            raise ValueError(f"transition data collides with supplement: {sorted(overlap)!r}")
        data.update(operation.data)
    if isinstance(operation, TransitionRequest) and operation.kind is EntityKind.SPRINT:
        data["request_related_refs"] = list(operation.related_refs.refs)
    return data
