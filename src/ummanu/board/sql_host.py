"""The normalized BoardHost over the board store client: reads, lifecycle transitions and markers."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ummanu.board.backend import entity_number
from ummanu.board.card_transitions import card_transition
from ummanu.board.events import (
    BoardEventCanon,
    BoardEventPending,
    MutationEventTransaction,
    render_marker_comment,
)
from ummanu.board.host import (
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
    BoardEntity,
    Card,
    CardState,
    EntityKind,
    Event,
    EventKind,
    Issue,
    IssueState,
    Product,
    ProductState,
    RelatedRefs,
    Sprint,
    SprintState,
)
from ummanu.board.tick_snapshot import select_cards, select_sprints
from ummanu.board.transitions import BoardProtocolError, transition, transition_for
from ummanu.product_issues import ProductIssueStore, product_swimlane_id
from ummanu.sprints import SprintReader
from ummanu.tasks import (
    TaskReader,
    _digest,
    _positive_int,
    _target_column_id,
    all_project_cards,
    project_card_by_reference,
    task_audit_for,
)

if TYPE_CHECKING:
    from ummanu.board.sql_cards import SqlCardClient


class SqlBoardHost:
    """Translate the board store's readers and lifecycle edges at the normalized host seam."""

    def __init__(
        self,
        client: SqlCardClient,
        *,
        data_dir: str | None = None,
        instance: str | None = None,
        audit: Any | None = None,
    ) -> None:
        self.client = client
        self.data_dir = data_dir
        self.instance = instance
        # The typed canon follows this host's own client and never the data directory: with no audit
        # handed in it is the card audit, `requests`/`board_events` (`docs/BOARD_STORE.md` §7.3).
        # Sprint and Product/Issue callers hand in the same audit.
        self.canon = (
            BoardEventCanon(audit or task_audit_for(client, data_dir))
            if data_dir is not None
            else None
        )

    def read(self, kind: EntityKind, ref: str) -> BoardEntity:
        if kind is EntityKind.CARD:
            return _card(TaskReader(self.client).show(ref))
        if kind is EntityKind.SPRINT:
            return _sprint(SprintReader(self.client, data_dir=self.data_dir).show(ref))
        store = self._product_issues()
        if kind is EntityKind.PRODUCT:
            return _product(store.show_product(_product_id(ref)))
        if kind is EntityKind.ISSUE:
            return _issue(store.show_issue(ref))
        raise BoardProtocolError(f"unknown entity kind {kind!r}")

    def list(self, kind: EntityKind) -> Sequence[BoardEntity]:
        if kind is EntityKind.CARD:
            return tuple(
                _card(record)
                for record in select_cards(TaskReader(self.client))
                if record.get("record_type") not in {"issue", "product"}
            )
        if kind is EntityKind.SPRINT:
            return tuple(
                _sprint(record)
                for record in select_sprints(SprintReader(self.client, data_dir=self.data_dir), create=False)
            )
        store = self._product_issues()
        if kind is EntityKind.PRODUCT:
            return tuple(_product(record) for record in store.list_products())
        if kind is EntityKind.ISSUE:
            return tuple(_issue(record) for record in store.list_issues(include_closed=True))
        raise BoardProtocolError(f"unknown entity kind {kind!r}")

    def create(self, operation: Create) -> MutationResult:
        if operation.entity.kind not in {EntityKind.PRODUCT, EntityKind.ISSUE}:
            self._migration_pending("create", operation.entity.kind)
        self._require_product_issue_configuration()
        entity = operation.entity
        if not isinstance(entity, (Product, Issue)):
            raise BoardProtocolError("Product/Issue create requires a normalized Product or Issue")
        request_id = self._request_id(operation.request_id, f"{entity.kind.value}-create")
        related = self._related(entity, operation.related_refs)
        existing = self._existing(request_id, entity, operation.actor, operation.reason)
        if existing is not None and self.canon.committed(request_id) is not None:
            return MutationResult(self.read(entity.kind, entity.ref), existing)
        # Pending occurrence is validated recovery evidence, independent of mutable inputs.
        if existing is None:
            self._validate_create(entity)
        event = existing or self._entity_event(
            EventKind.ENTITY_CREATED, entity, operation.actor, operation.reason, related, request_id
        )
        # Resolve or provision the lane before staging; retries only confirm staged effects.
        board_id, column_id = self._issues_board()
        swimlane_id = self._issues_swimlane(board_id, entity)

        def effect() -> None:
            if self._raw_by_ref(entity.ref) is not None:
                raise BoardProtocolError(f"{entity.kind.value} already exists")
            # This is preparatory evidence, not part of the uncertain write
            # window.  A failure here proves that createTask was never issued,
            # so MutationEventTransaction must discard the staged occurrence.
            reply = self.client.call(
                "createTask",
                project_id=board_id,
                title=entity.title,
                description=self._create_marker(request_id),
                column_id=column_id,
                swimlane_id=swimlane_id,
                reference=entity.ref,
            )
            task_id = _positive_int(reply)
            if task_id is None:
                # Discard only after both reads prove absence; otherwise retain pending.
                try:
                    reference_row = self._raw_by_ref(entity.ref)
                    marker_row = self._raw_by_marker(request_id)
                except Exception:  # noqa: BLE001 - failed correlation retains the uncertain occurrence
                    return
                if reference_row is not None or marker_row is not None:
                    return
                raise BoardProtocolError("board store refused the Product/Issue row")

        def confirm() -> dict[str, Any]:
            row = self._row_for_create(entity, request_id)
            if row is None:
                raise BoardProtocolError("Product/Issue create is not proven on the board store")
            return row

        def finish(_created: dict[str, Any]) -> None:
            row = self._row_for_create(entity, request_id)
            if row is None:
                raise BoardProtocolError("created Product/Issue row was not found")
            task_id = self._row_id(row)
            if (row.get("reference") != entity.ref or row.get("description") != entity.description) and not self.client.call(
                "updateTask", id=task_id, reference=entity.ref, description=entity.description
            ):
                raise BoardProtocolError("board store rejected Product/Issue details")
            if (
                self.client.call("saveTaskMetadata", task_id=task_id, values=self._metadata_for(entity))
                is not True
            ):
                raise BoardProtocolError("board store rejected Product/Issue metadata")
            confirmed = self._raw_by_ref(entity.ref)
            if confirmed is None or self._normalized_row(confirmed) != entity:
                raise BoardProtocolError("Product/Issue create remains incomplete")

        MutationEventTransaction(self.canon, request_id=request_id, event=event).execute(
            effect, confirm=confirm, finish=finish
        )
        return MutationResult(self.read(entity.kind, entity.ref), event)

    def replace(self, operation: Replace) -> MutationResult:
        if not isinstance(operation.entity, Issue):
            self._migration_pending("replace", operation.entity.kind)
        self._require_product_issue_configuration()
        entity = operation.entity
        append = operation.description_append
        edit = operation.description_edit
        if append is not None and edit is not None:
            raise BoardProtocolError("Issue replace requires one description operation")
        if edit is not None and (operation.actor.role != "po" or not operation.reason.strip()):
            raise BoardProtocolError("Issue description edit requires the PO and a non-empty reason")
        request_id = self._request_id(operation.request_id, "issue-replace")
        related = self._related(entity, operation.related_refs)
        existing = self._existing(request_id, entity, operation.actor, operation.reason, append=append, edit=edit)
        if existing is not None and self.canon.committed(request_id) is not None:
            return MutationResult(self.read(EntityKind.ISSUE, entity.ref), existing)
        if existing is None:
            current = self.read(EntityKind.ISSUE, entity.ref)
            if not isinstance(current, Issue) or current.state is not IssueState.OPEN:
                raise BoardProtocolError("cannot replace a closed Issue")
            if edit is not None:
                _require_description_edit(current, entity, edit)
            elif append is not None:
                _require_description_append(current, entity, append)
            elif (entity.title, entity.product_ref, entity.state, entity.issue_kind, entity.description) != (
                current.title,
                current.product_ref,
                current.state,
                current.issue_kind,
                current.description,
            ) or entity.priority not in {"P0", "P1", "P2", "P3"}:
                raise BoardProtocolError("Issue replace only supports a non-empty priority change")
        event = existing or self._entity_event(
            EventKind.ENTITY_UPDATED,
            entity,
            operation.actor,
            operation.reason,
            related,
            request_id,
            append=append,
            edit=edit,
        )
        if edit is not None:
            return self._edit_description(entity, edit, event, request_id)
        if append is not None:
            return self._append_description(entity, append, event, request_id)
        content = f"[issue:priority]\n{operation.reason}\n[request-id:{request_id}]"

        def effect() -> None:
            row = self._raw_by_ref(entity.ref)
            if row is None:
                raise BoardProtocolError("Issue was not found")
            task_id = self._row_id(row)
            comments = self.client.call("getAllComments", task_id=task_id) or []
            if not any(
                isinstance(comment, dict) and comment.get("comment") == content for comment in comments
            ):
                saved = self.client.call("createComment", task_id=task_id, user_id=0, content=content)
                if not _comment_saved(saved):
                    raise BoardProtocolError("board store rejected issue priority comment")

        def confirm() -> BoardEntity:
            row = self._raw_by_ref(entity.ref)
            if row is None:
                raise BoardProtocolError("Issue priority change is not proven")
            comments = self.client.call("getAllComments", task_id=self._row_id(row)) or []
            if not any(
                isinstance(comment, dict) and comment.get("comment") == content for comment in comments
            ):
                raise BoardProtocolError("Issue priority comment is not proven")
            return self._normalized_row(row)

        def finish(_confirmed: BoardEntity) -> None:
            row = self._raw_by_ref(entity.ref)
            if (
                row is None
                or self.client.call(
                    "saveTaskMetadata", task_id=self._row_id(row), values={"issue_priority": entity.priority}
                )
                is not True
            ):
                raise BoardProtocolError("board store rejected issue priority")
            row = self._raw_by_ref(entity.ref)
            confirmed = self._normalized_row(row) if row is not None else None
            if not isinstance(confirmed, Issue) or confirmed.priority != entity.priority:
                raise BoardProtocolError("Issue priority remains incomplete")

        MutationEventTransaction(self.canon, request_id=request_id, event=event).execute(
            effect, confirm=confirm, finish=finish
        )
        return MutationResult(self.read(EntityKind.ISSUE, entity.ref), event)

    def _append_description(
        self, entity: Issue, append: DescriptionAppend, event: Event, request_id: str
    ) -> MutationResult:
        """Write the appended description over exactly the text its block was computed from."""

        def effect() -> None:
            row = self._raw_by_ref(entity.ref)
            if row is None:
                raise BoardProtocolError("Issue was not found")
            if _digest(str(row.get("description") or "")) != append.description_sha256_was:
                # Nothing is written over a description that changed since the block was computed.
                raise BoardProtocolError("Issue description changed before the block was appended")
            saved = self.client.call("updateTask", id=self._row_id(row), description=entity.description)
            if not saved:
                raise BoardProtocolError("board store rejected the issue description")

        def confirm() -> BoardEntity:
            row = self._raw_by_ref(entity.ref)
            if row is None or str(row.get("description") or "") != entity.description:
                raise BoardProtocolError("Issue description append is not proven")
            return self._normalized_row(row)

        MutationEventTransaction(self.canon, request_id=request_id, event=event).execute(
            effect, confirm=confirm
        )
        return MutationResult(self.read(EntityKind.ISSUE, entity.ref), event)

    def _edit_description(
        self, entity: Issue, edit: DescriptionEdit, event: Event, request_id: str
    ) -> MutationResult:
        """Replace the description over exactly the text named in the edit evidence."""

        def effect() -> None:
            row = self._raw_by_ref(entity.ref)
            if row is None:
                raise BoardProtocolError("Issue was not found")
            if _digest(str(row.get("description") or "")) != edit.description_sha256_was:
                # Refuse to overwrite a description that changed since this edit was prepared.
                raise BoardProtocolError("Issue description changed before the edit")
            saved = self.client.call("updateTask", id=self._row_id(row), description=entity.description)
            if not saved:
                raise BoardProtocolError("board store rejected the issue description")

        def confirm() -> BoardEntity:
            row = self._raw_by_ref(entity.ref)
            if row is None or str(row.get("description") or "") != entity.description:
                raise BoardProtocolError("Issue description edit is not proven")
            return self._normalized_row(row)

        MutationEventTransaction(self.canon, request_id=request_id, event=event).execute(
            effect, confirm=confirm
        )
        return MutationResult(self.read(EntityKind.ISSUE, entity.ref), event)

    def transition(
        self,
        operation: TransitionRequest,
        *,
        finish: Callable[[Card], None] | None = None,
    ) -> MutationResult:
        """Move one Card along a declared, role-authorized lifecycle edge.

        The order is the contract: validate the live Card and the caller's authority for its edge, stage
        the exact event this occurrence will publish, perform the single column operation, confirm it on
        the board, then commit that event. Only a failure before the column operation owes the journal
        nothing; once it has returned, every later failure owes the caller a repair and keeps the pending
        event that names it.

        ``finish`` is the caller's own idempotent board work for this same edge. It runs once the target
        is proven, never on a replay of an already committed occurrence, and never before the column
        effect.
        """
        if operation.kind is EntityKind.SPRINT:
            return self._transition_sprint(operation)
        if operation.kind is EntityKind.ISSUE:
            return self._transition_issue(operation)
        if operation.kind is not EntityKind.CARD:
            self._migration_pending("transition", operation.kind)
        if self.canon is None:
            raise BoardProtocolError("Card transitions require a configured data directory")
        if not isinstance(operation.target, CardState):
            raise BoardProtocolError("Card transitions require a CardState target")
        request_id = operation.request_id or f"card-transition-{uuid.uuid4().hex}"
        existing = self.canon.event(request_id)
        if existing is not None:
            if (
                existing.entity_kind is not EntityKind.CARD
                or existing.ref != operation.ref
                or existing.actor != operation.actor
                or existing.reason != operation.reason
                or existing.target_state != operation.target.value
                or existing.data != operation.data
            ):
                raise ValueError("request id belongs to another operation or payload")
            event = existing
        else:
            event = None
        current = self.read(EntityKind.CARD, operation.ref)
        if not isinstance(current, Card):
            raise BoardProtocolError("Card transition resolved a non-Card entity")
        # Committed history is not a lease on current state; do not write again.
        if event is not None and self.canon.committed(request_id) is not None:
            return MutationResult(current, event)
        if event is None:
            declaration = card_transition(operation.actor.role, current.state, operation.target)
            related = operation.related_refs
            if current.sprint_ref and current.sprint_ref not in related.refs:
                # Every Card transition includes its sprint ref.
                related = RelatedRefs(related.refs + (current.sprint_ref,))
            event = self._event(current, declaration.event_kind, operation, related, request_id)

        def confirm() -> Card:
            entity = self.read(EntityKind.CARD, operation.ref)
            if not isinstance(entity, Card) or entity.state is not operation.target:
                raise BoardProtocolError("Card transition is not proven on the board store")
            return entity

        def effect() -> None:
            # Re-read before move; post-move confirmation is outside the discard window.
            entity = self.read(EntityKind.CARD, operation.ref)
            if not isinstance(entity, Card):
                raise BoardProtocolError("Card transition resolved a non-Card entity")
            card_transition(operation.actor.role, entity.state, operation.target)
            self._move_card(entity, operation.target)

        entity = MutationEventTransaction(
            self.canon,
            request_id=request_id,
            event=event,
        ).execute(effect, confirm=confirm, finish=finish)
        return MutationResult(entity, event)

    def marker_comment(self, operation: MarkerComment) -> MutationResult:
        """Render one staged control-plane Card event as its marker comment."""
        if self.canon is None:
            raise BoardProtocolError("Card marker comments require a configured data directory")
        with self.canon.audit.marker_comment_lock(operation.ref):
            request_id = self._request_id(operation.request_id, "card-marker")
            existing = self.canon.event(request_id)
            replayed = existing is not None
            if existing is not None:
                self._require_marker_operation(existing, operation)
                current = self.read(EntityKind.CARD, operation.ref)
                if not isinstance(current, Card):
                    raise BoardProtocolError("Card marker comment resolved a non-Card entity")
                if self.canon.committed(request_id) is not None:
                    return MutationResult(current, existing, replayed=True)
                event = existing
            else:
                if operation.fresh_admission is not None:
                    operation.fresh_admission()
                current = self.read(EntityKind.CARD, operation.ref)
                if not isinstance(current, Card):
                    raise BoardProtocolError("Card marker comment resolved a non-Card entity")
                related = operation.related_refs
                if current.sprint_ref and current.sprint_ref not in related.refs:
                    related = RelatedRefs(related.refs + (current.sprint_ref,))
                # The staged ordinal distinguishes this occurrence from older matching comments.
                preview = self._marker_event(current, operation, related, request_id)
                content = self.render_marker(preview)
                owner = self.canon.audit.pending_marker_owner(operation.ref, content, request_id=request_id)
                if owner is not None:
                    raise BoardEventPending(
                        "an earlier identical Card marker occurrence is pending; "
                        f"reconcile request {owner} first"
                    )
                occurrence = self._marker_occurrences(operation.ref, content) + 1
                event = self._marker_event(
                    current,
                    operation,
                    related,
                    request_id,
                    marker_occurrence=occurrence,
                )

            content = self.render_marker(event)

            def effect() -> None:
                # Re-read before comment; post-write transport failure is uncertain.
                entity = self.read(EntityKind.CARD, operation.ref)
                if not isinstance(entity, Card):
                    raise BoardProtocolError("Card marker comment resolved a non-Card entity")
                try:
                    reply = self.client.call(
                        "createComment",
                        task_id=self._card_task_id(operation.ref),
                        user_id=0,
                        content=content,
                    )
                except Exception as exc:
                    # Only unavailable transport can hide an applied effect.
                    from ummanu.tasks import TaskError

                    if isinstance(exc, TaskError) and exc.code == "backend_unavailable":
                        return
                    raise
                if not _comment_saved(reply):
                    raise BoardProtocolError("board store rejected the Card marker comment")

            def confirm() -> Card:
                task = TaskReader(self.client).show(operation.ref)
                if not self._marker_is_proven(event, task):
                    raise BoardProtocolError("Card marker comment is not proven on the board store")
                entity = self.read(EntityKind.CARD, operation.ref)
                if not isinstance(entity, Card):
                    raise BoardProtocolError("Card marker comment resolved a non-Card entity")
                return entity

            entity = MutationEventTransaction(
                self.canon,
                request_id=request_id,
                event=event,
            ).execute(effect, confirm=confirm)
            return MutationResult(entity, event, replayed=replayed)

    def recover_marker_comment(self, request_id: str) -> MutationResult:
        """Publish a pending marker only after its exact rendered comment exists."""
        if self.canon is None:
            raise BoardProtocolError("Card marker recovery requires a configured data directory")
        event = self.canon.event(request_id)
        if event is None or event.entity_kind is not EntityKind.CARD:
            raise BoardProtocolError("pending event is not a recoverable Card marker occurrence")
        with self.canon.audit.marker_comment_lock(event.ref):
            event = self.canon.event(request_id)
            if event is None or event.entity_kind is not EntityKind.CARD:
                raise BoardProtocolError("pending event is not a recoverable Card marker occurrence")
            if event.kind not in {EventKind.CARD_REPORTED, EventKind.CARD_VERDICTED, EventKind.CARD_DECIDED}:
                raise BoardProtocolError("pending event is not a recoverable Card marker occurrence")
            self.render_marker(event)
            task = TaskReader(self.client).show(event.ref)
            if not self._marker_is_proven(event, task):
                raise BoardProtocolError("pending Card marker comment is not proven on the board store")
            entity = self.read(EntityKind.CARD, event.ref)
            if not isinstance(entity, Card):
                raise BoardProtocolError("pending Card marker comment resolved a non-Card entity")
            self.canon.commit(request_id, event)
            return MutationResult(entity, event)

    def _transition_sprint(self, operation: TransitionRequest) -> MutationResult:
        """Apply one checked Sprint status edge through the typed event canon."""
        if self.canon is None:
            raise BoardProtocolError("Sprint transitions require a configured data directory")
        if not isinstance(operation.target, SprintState):
            raise BoardProtocolError("Sprint transitions require a SprintState target")
        self._validate_sprint_supplement(operation)
        request_id = self._request_id(operation.request_id, "sprint-transition")
        current = self.read(EntityKind.SPRINT, operation.ref)
        if not isinstance(current, Sprint):
            raise BoardProtocolError("Sprint transition resolved a non-Sprint entity")
        existing = self.canon.event(request_id)
        if existing is not None:
            if (
                existing.entity_kind is not EntityKind.SPRINT
                or existing.ref != operation.ref
                or existing.actor != operation.actor
                or existing.reason != operation.reason
                or existing.target_state != operation.target.value
                or existing.data != self._sprint_event_data(operation)
                or not self._declared_sprint_event(existing)
            ):
                raise ValueError("request id belongs to another operation or payload")
            if self.canon.committed(request_id) is not None:
                return MutationResult(current, existing)
            event = existing
        else:
            successor, declaration = transition(current, operation.target)
            if not isinstance(successor, Sprint):
                raise BoardProtocolError("Sprint transition resolved an invalid successor")
            related = operation.related_refs
            required = tuple(
                ref for ref in (current.product_ref, *current.issue_refs, *current.card_refs) if ref
            )
            if any(ref not in related.refs for ref in required):
                related = RelatedRefs(related.refs + required)
            event = self._sprint_event(
                declaration.event_kind,
                successor,
                operation.actor,
                operation.reason,
                related,
                request_id,
                source=current.state.value,
                target=operation.target.value,
                data=self._sprint_event_data(operation),
            )

        def effect() -> None:
            live = self.read(EntityKind.SPRINT, operation.ref)
            if not isinstance(live, Sprint):
                raise BoardProtocolError("Sprint transition resolved a non-Sprint entity")
            transition(live, operation.target)
            task_id = self._sprint_task_id(operation.ref)
            supplement = operation.sprint
            # Persist observer before reopen so a refused status write can compensate.
            if supplement is not None and supplement.observer is not None:
                if (
                    self.client.call(
                        "saveTaskMetadata",
                        task_id=task_id,
                        values={"sprint_observer": supplement.observer},
                    )
                    is not True
                ):
                    raise BoardProtocolError("board store rejected Sprint transition")
                if not self._sprint_metadata_matches(task_id, {"sprint_observer": supplement.observer}):
                    raise BoardProtocolError("Sprint transition observer remains incomplete")
            values = {"sprint_status": operation.target.value}
            if supplement is not None and supplement.budget_by_type:
                values["sprint_budget"] = json.dumps(
                    {"by_type": dict(supplement.budget_by_type)},
                    separators=(",", ":"),
                )
            reply = self.client.call("saveTaskMetadata", task_id=task_id, values=values)
            if reply is not True:
                raise BoardProtocolError("board store rejected Sprint transition")

        def confirm() -> Sprint:
            entity = self.read(EntityKind.SPRINT, operation.ref)
            if not isinstance(entity, Sprint) or entity.state is not operation.target:
                raise BoardProtocolError("Sprint transition is not proven on the board store")
            return entity

        entity = MutationEventTransaction(self.canon, request_id=request_id, event=event).execute(
            effect,
            confirm=confirm,
        )
        return MutationResult(entity, event)

    def recover_sprint(self, request_id: str) -> MutationResult:
        """Commit a pending Sprint occurrence only after its exact state is live."""
        if self.canon is None:
            raise BoardProtocolError("Sprint recovery requires a configured data directory")
        event = self.canon.event(request_id)
        if event is None or event.entity_kind is not EntityKind.SPRINT:
            raise BoardProtocolError("pending event is not a recoverable Sprint occurrence")
        if event.target_state is None:
            raise BoardProtocolError("pending Sprint event has no target state")
        try:
            target = SprintState(event.target_state)
        except ValueError as exc:
            raise BoardProtocolError("pending Sprint event has an invalid target") from exc
        entity = self.read(EntityKind.SPRINT, event.ref)
        if not isinstance(entity, Sprint) or entity.state is not target:
            raise BoardProtocolError("pending Sprint transition is not proven on the board store")
        if not self._declared_sprint_event(event):
            raise BoardProtocolError("pending Sprint event has an unsupported lifecycle edge")
        self.canon.commit(request_id, event)
        return MutationResult(entity, event)

    def recover_transition(self, request_id: str) -> MutationResult:
        """Commit a pending Card event only after its exact target is live.

        This deliberately never calls ``moveTaskPosition``: a pending event is evidence of an attempted
        effect, not authority to attempt it again.
        """
        if self.canon is None:
            raise BoardProtocolError("Card transition recovery requires a configured data directory")
        event = self.canon.event(request_id)
        if event is None:
            raise BoardProtocolError("pending Card transition was not found")
        if event.entity_kind is not EntityKind.CARD or event.target_state is None:
            raise BoardProtocolError("pending event is not a recoverable Card transition")
        try:
            target = CardState(event.target_state)
        except ValueError as exc:
            raise BoardProtocolError("pending Card transition has an invalid target") from exc
        entity = self.read(EntityKind.CARD, event.ref)
        if not isinstance(entity, Card) or entity.state is not target:
            raise BoardProtocolError("pending Card transition is not proven on the board store")
        self.canon.commit(request_id, event)
        return MutationResult(entity, event)

    def recover_product_issue(self, request_id: str) -> MutationResult:
        """Resume one typed Product/Issue occurrence without consulting legacy journals."""
        self._require_product_issue_configuration()
        assert self.canon is not None
        event = self.canon.event(request_id)
        if event is None or event.entity_kind not in {EntityKind.PRODUCT, EntityKind.ISSUE}:
            raise BoardProtocolError("pending event is not a recoverable Product/Issue occurrence")
        if event.entity_kind is EntityKind.PRODUCT:
            entity = _product_from_payload(event.data)
        else:
            entity = _issue_from_payload(event.data)
        if event.kind is EventKind.ENTITY_CREATED:
            return self.create(Create(entity, event.actor, event.reason, event.related_refs, request_id))
        if event.kind is EventKind.ENTITY_UPDATED and isinstance(entity, Issue):
            try:
                append = (
                    DescriptionAppend.from_event_data(event.data["append"])
                    if "append" in event.data
                    else None
                )
                edit = DescriptionEdit.from_event_data(event.data["edit"]) if "edit" in event.data else None
            except ValueError as exc:
                raise BoardProtocolError(
                    "pending Issue event has invalid description evidence"
                ) from exc
            return self.replace(
                Replace(entity, event.actor, event.reason, event.related_refs, request_id, append, edit)
            )
        if event.kind is EventKind.ISSUE_CLOSED and isinstance(entity, Issue):
            return self.transition(
                TransitionRequest(
                    EntityKind.ISSUE,
                    entity.ref,
                    IssueState.CLOSED,
                    event.actor,
                    event.reason,
                    event.related_refs,
                    request_id,
                )
            )
        raise BoardProtocolError("pending event is not a recoverable Product/Issue occurrence")

    def _transition_issue(self, operation: TransitionRequest) -> MutationResult:
        self._require_product_issue_configuration()
        if not isinstance(operation.target, IssueState):
            raise BoardProtocolError("Issue transitions require an IssueState target")
        request_id = self._request_id(operation.request_id, "issue-transition")
        known = self.canon.event(request_id) if self.canon is not None else None
        if known is not None:
            if (
                known.entity_kind is not EntityKind.ISSUE
                or known.ref != operation.ref
                or known.actor != operation.actor
                or known.reason != operation.reason
                or known.target_state != operation.target.value
            ):
                raise ValueError("request id belongs to another operation or payload")
            successor = _issue_from_payload(known.data)
            current = self.read(EntityKind.ISSUE, operation.ref)
            if not isinstance(current, Issue):
                raise BoardProtocolError("Issue transition resolved a non-Issue entity")
        else:
            current = self.read(EntityKind.ISSUE, operation.ref)
            if not isinstance(current, Issue):
                raise BoardProtocolError("Issue transition resolved a non-Issue entity")
            successor, _declaration = transition(current, operation.target)
            if not isinstance(successor, Issue):
                raise BoardProtocolError("Issue transition resolved an invalid successor")
            successor = Issue(
                successor.ref,
                successor.title,
                successor.product_ref,
                successor.state,
                successor.priority,
                successor.issue_kind,
                successor.description,
                operation.reason,
            )
        related = self._related(current, operation.related_refs)
        existing = self._existing(
            request_id, successor, operation.actor, operation.reason, target=operation.target.value
        )
        if existing is not None and self.canon.committed(request_id) is not None:
            return MutationResult(self.read(EntityKind.ISSUE, operation.ref), existing)
        event = existing or self._entity_event(
            EventKind.ISSUE_CLOSED,
            successor,
            operation.actor,
            operation.reason,
            related,
            request_id,
            source=current.state.value,
            target=operation.target.value,
        )
        content = f"[issue:closed]\n{operation.reason}\n[request-id:{request_id}]"

        def effect() -> None:
            row = self._raw_by_ref(current.ref)
            if row is None:
                raise BoardProtocolError("Issue was not found")
            task_id = self._row_id(row)
            comments = self.client.call("getAllComments", task_id=task_id) or []
            if not any(
                isinstance(comment, dict) and comment.get("comment") == content for comment in comments
            ):
                saved = self.client.call("createComment", task_id=task_id, user_id=0, content=content)
                if not _comment_saved(saved):
                    raise BoardProtocolError("board store rejected issue close comment")

        def confirm() -> BoardEntity:
            row = self._raw_by_ref(current.ref)
            if row is None:
                raise BoardProtocolError("Issue close is not proven")
            comments = self.client.call("getAllComments", task_id=self._row_id(row)) or []
            if not any(
                isinstance(comment, dict) and comment.get("comment") == content for comment in comments
            ):
                raise BoardProtocolError("Issue close comment is not proven")
            return self._normalized_row(row)

        def finish(_confirmed: BoardEntity) -> None:
            row = self._raw_by_ref(current.ref)
            if row is None:
                raise BoardProtocolError("Issue was not found")
            task_id = self._row_id(row)
            if (
                self.client.call(
                    "saveTaskMetadata", task_id=task_id, values={"issue_closed_reason": operation.reason}
                )
                is not True
            ):
                raise BoardProtocolError("board store rejected issue close reason")
            row = self._raw_by_ref(current.ref)
            if row is None:
                raise BoardProtocolError("Issue was not found")
            if int(row.get("is_active", 1) or 0) != 0 and not self.client.call("closeTask", task_id=task_id):
                raise BoardProtocolError("board store rejected issue closure")
            row = self._raw_by_ref(current.ref)
            if row is None or self._normalized_row(row) != successor:
                raise BoardProtocolError("Issue closure remains incomplete")

        MutationEventTransaction(self.canon, request_id=request_id, event=event).execute(
            effect, confirm=confirm, finish=finish
        )
        return MutationResult(self.read(EntityKind.ISSUE, operation.ref), event)

    @staticmethod
    def _sprint_data(operation: TransitionRequest) -> dict[str, object]:
        return operation.sprint.event_data() if operation.sprint is not None else {}

    @classmethod
    def _sprint_event_data(cls, operation: TransitionRequest) -> dict[str, object]:
        """Keep the caller's immutable link payload with the occurrence."""
        data = cls._sprint_data(operation)
        data["request_related_refs"] = list(operation.related_refs.refs)
        return data

    @staticmethod
    def _validate_sprint_supplement(operation: TransitionRequest) -> None:
        supplement = operation.sprint
        if supplement is None:
            return
        if not isinstance(supplement, SprintSupplement):
            raise BoardProtocolError("Sprint transition supplement must be normalized")
        if operation.target is SprintState.OPEN:
            if supplement.budget_by_type:
                raise BoardProtocolError("Sprint reopen cannot persist a budget")
            return
        if operation.target is SprintState.STOPPED:
            if supplement.observer is not None or not supplement.budget_by_type:
                raise BoardProtocolError("Sprint hard stop requires its computed budget only")
            return
        raise BoardProtocolError("Sprint close cannot persist supplementary values")

    def _sprint_task_id(self, ref: str) -> int:
        record = SprintReader(self.client, data_dir=self.data_dir).show(ref, include_cards=False)
        task_id = entity_number("sprint", record.get("id"))
        if task_id is None:
            raise BoardProtocolError("board store returned an invalid Sprint")
        return task_id

    def _sprint_metadata_matches(self, task_id: int, values: dict[str, str]) -> bool:
        actual = self.client.call("getTaskMetadata", task_id=task_id)
        if not isinstance(actual, dict):
            raise BoardProtocolError("board store returned invalid Sprint metadata")
        return all(str(actual.get(key) or "") == value for key, value in values.items())

    @staticmethod
    def _declared_sprint_event(event: Event) -> bool:
        if event.source_state is None or event.target_state is None:
            return False
        try:
            source = SprintState(event.source_state)
            target = SprintState(event.target_state)
        except ValueError:
            return False
        try:
            declaration = transition_for(EntityKind.SPRINT, source, target)
        except BoardProtocolError:
            return False
        return event.kind is declaration.event_kind

    @staticmethod
    def _sprint_event(
        kind: EventKind,
        entity: Sprint,
        actor,
        reason: str,
        related: RelatedRefs,
        request_id: str,
        *,
        source: str | None = None,
        target: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> Event:
        payload = json.dumps(
            {
                "request_id": request_id,
                "kind": kind.value,
                "entity": [
                    entity.ref,
                    entity.goal,
                    entity.state.value,
                    entity.product_ref,
                    entity.issue_refs,
                    entity.card_refs,
                ],
                "actor": [actor.role, actor.id, actor.head_run_ref],
                "reason": reason,
                "related_refs": list(related.refs),
                "source": source,
                "target": target,
                "data": data or {},
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return Event(
            "board-event-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32],
            kind,
            EntityKind.SPRINT,
            entity.ref,
            actor,
            reason,
            datetime.now(UTC),
            related,
            source,
            target,
            data or {},
        )

    def _require_product_issue_configuration(self) -> None:
        if self.canon is None or self.data_dir is None or self.instance is None:
            raise BoardProtocolError(
                "Product/Issue mutations require configured data and instance directories"
            )

    @staticmethod
    def _request_id(request_id: str | None, prefix: str) -> str:
        result = request_id or f"{prefix}-{uuid.uuid4().hex}"
        if not isinstance(result, str) or not result.strip():
            raise ValueError("request id must not be empty")
        return result

    def _existing(
        self,
        request_id: str,
        entity: Product | Issue,
        actor,
        reason: str,
        *,
        target: str | None = None,
        append: DescriptionAppend | None = None,
        edit: DescriptionEdit | None = None,
    ) -> Event | None:
        assert self.canon is not None
        event = self.canon.event(request_id)
        if event is None:
            return None
        if (
            event.entity_kind is not entity.kind
            or event.ref != entity.ref
            or event.actor != actor
            or event.reason != reason
            or event.target_state != target
            or event.data != _event_data(entity, append, edit)
        ):
            raise ValueError("request id belongs to another operation or payload")
        return event

    @staticmethod
    def _related(entity: Product | Issue, related: RelatedRefs) -> RelatedRefs:
        if isinstance(entity, Issue) and entity.product_ref not in related.refs:
            return RelatedRefs(related.refs + (entity.product_ref,))
        return related

    def _validate_create(self, entity: Product | Issue) -> None:
        if isinstance(entity, Product):
            if (
                entity.state is not ProductState.ACTIVE
                or not entity.projects
                or not entity.ref.startswith("product:")
                or not entity.ref.removeprefix("product:").strip()
            ):
                raise BoardProtocolError("Product create requires an active Product with projects")
            from ummanu.product_issues import registered_projects

            unknown = sorted(set(entity.projects) - registered_projects(self.instance or ""))
            if unknown:
                raise BoardProtocolError("Product has unknown registered project(s): " + ", ".join(unknown))
            return
        if (
            entity.state is not IssueState.OPEN
            or not entity.ref.startswith("issue:")
            or entity.issue_kind not in {"bug", "feature", "question", "improvement"}
            or entity.priority not in {"P0", "P1", "P2", "P3"}
        ):
            raise BoardProtocolError("Issue create requires an open Issue with a valid kind and priority")
        product = self.read(EntityKind.PRODUCT, entity.product_ref)
        if not isinstance(product, Product) or product.state is not ProductState.ACTIVE:
            raise BoardProtocolError("Issue create requires an active Product")

    def _entity_event(
        self,
        kind: EventKind,
        entity: Product | Issue,
        actor,
        reason: str,
        related: RelatedRefs,
        request_id: str,
        *,
        source: str | None = None,
        target: str | None = None,
        append: DescriptionAppend | None = None,
        edit: DescriptionEdit | None = None,
    ) -> Event:
        identity: dict[str, Any] = {
            "request_id": request_id,
            "kind": kind.value,
            "entity": _entity_payload(entity),
            "actor": [actor.role, actor.id, actor.head_run_ref],
            "reason": reason,
            "related_refs": list(related.refs),
            "source": source,
            "target": target,
        }
        if append is not None:
            identity["append"] = append.event_data()
        if edit is not None:
            identity["edit"] = edit.event_data()
        payload = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        return Event(
            "board-event-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32],
            kind,
            entity.kind,
            entity.ref,
            actor,
            reason,
            datetime.now(UTC),
            related,
            source,
            target,
            _event_data(entity, append, edit),
        )

    def _issues_board(self) -> tuple[int, int]:
        board = self.client.call("getProjectByName", name="Pipeline")
        if not isinstance(board, dict) or not isinstance(board.get("id"), int):
            raise BoardProtocolError("Pipeline board is unavailable")
        columns = self.client.call("getColumns", project_id=board["id"]) or []
        first = columns[0] if isinstance(columns, list) and columns else None
        if (
            not isinstance(first, dict)
            or first.get("title") != "Issues"
            or not isinstance(first.get("id"), int)
        ):
            raise BoardProtocolError("Pipeline first column is not Issues")
        return board["id"], first["id"]

    def _issues_swimlane(self, board_id: int, entity: Product | Issue) -> int:
        """The product lane of this record, by the one rule both secretarial writers share."""
        return product_swimlane_id(
            self.client,
            board_id,
            entity.ref if isinstance(entity, Product) else entity.product_ref,
        )

    def _raw_by_ref(self, ref: str) -> dict[str, Any] | None:
        board_id, _ = self._issues_board()
        row = self.client.call("getTaskByReference", project_id=board_id, reference=ref)
        return row if isinstance(row, dict) else None

    def _raw_by_marker(self, request_id: str) -> dict[str, Any] | None:
        board_id, _ = self._issues_board()
        marker = self._create_marker(request_id)
        matches = [
            row
            for row in all_project_cards(self.client, board_id)
            if isinstance(row, dict) and row.get("description") == marker
        ]
        if len(matches) > 1:
            raise BoardProtocolError("Product/Issue create correlation is ambiguous")
        return matches[0] if matches else None

    def _row_for_create(self, entity: Product | Issue, request_id: str) -> dict[str, Any] | None:
        return self._raw_by_ref(entity.ref) or self._raw_by_marker(request_id)

    @staticmethod
    def _row_id(row: dict[str, Any]) -> int:
        task_id = _positive_int(row.get("id"))
        if task_id is None:
            raise BoardProtocolError("board store returned an invalid Product/Issue row")
        return task_id

    def _normalized_row(self, row: dict[str, Any], *, allow_incomplete: bool = False) -> Product | Issue:
        task_id = self._row_id(row)
        metadata = self.client.call("getTaskMetadata", task_id=task_id) or {}
        if not isinstance(metadata, dict):
            raise BoardProtocolError("board store returned invalid Product/Issue metadata")
        record_type = metadata.get("record_type")
        if record_type == "product":
            projects = json.loads(str(metadata.get("product_projects") or "[]"))
            if not allow_incomplete and (not isinstance(projects, list) or not projects):
                raise BoardProtocolError("Product metadata remains incomplete")
            return Product(
                str(row.get("reference") or ""),
                str(row.get("title") or ""),
                ProductState.ACTIVE,
                tuple(str(value) for value in projects),
                str(row.get("description") or ""),
            )
        if record_type == "issue":
            return Issue(
                str(row.get("reference") or ""),
                str(row.get("title") or ""),
                f"product:{metadata.get('issue_product') or ''}",
                IssueState.CLOSED if int(row.get("is_active", 1) or 0) == 0 else IssueState.OPEN,
                str(metadata.get("issue_priority") or ""),
                str(metadata.get("issue_kind") or ""),
                str(row.get("description") or ""),
                str(metadata.get("issue_closed_reason") or "") or None,
            )
        if allow_incomplete:
            # Staged create proves the unique effect; finish supplies its typed shape.
            ref = str(row.get("reference") or "")
            if ref.startswith("product:"):
                return Product(
                    ref,
                    str(row.get("title") or ""),
                    projects=("pending",),
                    description=str(row.get("description") or ""),
                )
            return Issue(
                ref,
                str(row.get("title") or ""),
                "product:pending",
                priority="pending",
                issue_kind="pending",
                description=str(row.get("description") or ""),
            )
        raise BoardProtocolError("row is not a Product or Issue")

    @staticmethod
    def _metadata_for(entity: Product | Issue) -> dict[str, str]:
        if isinstance(entity, Product):
            return {
                "record_type": "product",
                "product_id": entity.ref.removeprefix("product:"),
                "product_projects": json.dumps(list(entity.projects), separators=(",", ":")),
            }
        return {
            "record_type": "issue",
            "issue_product": entity.product_ref.removeprefix("product:"),
            "issue_kind": entity.issue_kind,
            "issue_priority": entity.priority,
        }

    @staticmethod
    def _create_marker(request_id: str) -> str:
        return (
            "[ummanu-product-issue-transaction:"
            + hashlib.sha256(request_id.encode("utf-8")).hexdigest()
            + "]"
        )

    def _move_card(self, card: Card, target: CardState) -> None:
        reader = TaskReader(self.client)
        board_id, columns, _ = reader._board()
        column_id = _target_column_id(columns, target.value)
        if column_id is None:
            raise BoardProtocolError("board schema is invalid")
        raw = project_card_by_reference(self.client, board_id, card.ref)
        if not isinstance(raw, dict):
            raise BoardProtocolError("Card was not found")
        swimlane_id = _positive_int(raw.get("swimlane_id")) or 0
        task_id = _positive_int(raw.get("id"))
        if task_id is None:
            raise BoardProtocolError("board store returned an invalid Card")
        if not self.client.call(
            "moveTaskPosition",
            project_id=board_id,
            task_id=task_id,
            column_id=column_id,
            position=1,
            swimlane_id=swimlane_id,
        ):
            raise BoardProtocolError("board store rejected the Card transition")

    def _card_task_id(self, ref: str) -> int:
        """The card's number, read through the one identity parser rather than one prefix.

        A literal identity prefix here once answered `None` for every card another store word
        normalized, so `report`, `verdict` and `decide` refused while the reader that produced
        the identity worked (`board/backend.py`).
        """
        task = TaskReader(self.client).show(ref)
        task_id = entity_number("task", task.get("id"))
        if task_id is None:
            raise BoardProtocolError("board store returned an invalid Card")
        return task_id

    @staticmethod
    def _marker_is_proven(event: Event, task: dict[str, Any]) -> bool:
        """Whether the board has the precise marker occurrence event staged.

        Earlier protocol events did not retain an occurrence witness; they stay readable as historical
        records, while every new occurrence requires its staged matching-row ordinal.
        """
        content = SqlBoardHost.render_marker(event)
        matching = sum(
            str(comment.get("body") or "") == content
            for comment in task.get("comments", [])
            if isinstance(comment, dict)
        )
        occurrence = event.data.get("marker_occurrence")
        if occurrence is None:
            return matching > 0
        if not isinstance(occurrence, int) or isinstance(occurrence, bool) or occurrence < 1:
            raise BoardProtocolError("Card marker event has an invalid occurrence witness")
        return matching >= occurrence

    def _marker_occurrences(self, ref: str, content: str) -> int:
        task = TaskReader(self.client).show(ref)
        return sum(
            str(comment.get("body") or "") == content
            for comment in task.get("comments", [])
            if isinstance(comment, dict)
        )

    @staticmethod
    def render_marker(event: Event) -> str:
        """Render the established marker grammar from complete typed data."""
        try:
            return render_marker_comment(event)
        except ValueError as exc:
            raise BoardProtocolError(str(exc)) from None

    @classmethod
    def _marker_event(
        cls,
        card: Card,
        operation: MarkerComment,
        related: RelatedRefs,
        request_id: str,
        *,
        marker_occurrence: int | None = None,
    ) -> Event:
        data = dict(operation.data)
        data["request_related_refs"] = list(operation.related_refs.refs)
        if marker_occurrence is not None:
            data["marker_occurrence"] = marker_occurrence
        payload = json.dumps(
            {
                "request_id": request_id,
                "kind": operation.kind.value,
                "ref": card.ref,
                "actor": [operation.actor.role, operation.actor.id, operation.actor.head_run_ref],
                "reason": operation.reason,
                "related_refs": list(related.refs),
                "data": data,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        event = Event(
            "board-event-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32],
            operation.kind,
            EntityKind.CARD,
            card.ref,
            operation.actor,
            operation.reason,
            datetime.now(UTC),
            related,
            data=data,
        )
        # Validate before staging so malformed payloads cannot consume request ids.
        cls.render_marker(event)
        return event

    @staticmethod
    def _require_marker_operation(existing: Event, operation: MarkerComment) -> None:
        data = dict(operation.data)
        request_related = existing.data.get("request_related_refs")
        if request_related != list(operation.related_refs.refs):
            raise ValueError("request id belongs to another operation or payload")
        if (
            existing.kind is not operation.kind
            or existing.entity_kind is not EntityKind.CARD
            or existing.ref != operation.ref
            or existing.actor != operation.actor
            or existing.reason != operation.reason
        ):
            raise ValueError("request id belongs to another operation or payload")
        expected_data = {
            key: value
            for key, value in existing.data.items()
            if key not in {"request_related_refs", "marker_occurrence"}
        }
        # Admission is mutable; check the stable decision owner first.
        if existing.kind is EventKind.CARD_DECIDED and "assessment_visit" not in data:
            expected_data.pop("assessment_visit", None)
        if expected_data != data:
            raise ValueError("request id belongs to another operation or payload")
        SqlBoardHost.render_marker(existing)

    @staticmethod
    def _event(
        card: Card,
        kind: EventKind,
        operation: TransitionRequest,
        related: RelatedRefs,
        request_id: str,
    ) -> Event:
        """Build the one complete occurrence this request publishes.

        The id is derived from the request and its exact payload, so a retry of the same request names
        the same occurrence and a different payload can never borrow it.
        """
        payload = json.dumps(
            {
                "request_id": request_id,
                "kind": kind.value,
                "ref": card.ref,
                "actor": [operation.actor.role, operation.actor.id, operation.actor.head_run_ref],
                "reason": operation.reason,
                "related_refs": list(related.refs),
                "source": card.state.value,
                "target": operation.target.value,
                "data": operation.data,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return Event(
            "board-event-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32],
            kind,
            EntityKind.CARD,
            card.ref,
            operation.actor,
            operation.reason,
            datetime.now(UTC),
            related,
            card.state.value,
            operation.target.value,
            dict(operation.data),
        )

    def _product_issues(self) -> ProductIssueStore:
        if self.data_dir is None or self.instance is None:
            raise BoardProtocolError(
                "Product/Issue reads require the configured data and instance directories"
            )
        return ProductIssueStore(self.client, data_dir=self.data_dir, instance=self.instance)

    @staticmethod
    def _migration_pending(operation: str, kind: EntityKind) -> None:
        raise BoardProtocolError(
            f"SqlBoardHost {operation} for {kind.value} is not migrated; "
            "use the established writer until its migration card preserves its audit semantics"
        )


def _product_id(ref: str) -> str:
    prefix = "product:"
    if not ref.startswith(prefix) or not ref[len(prefix) :]:
        raise BoardProtocolError(f"invalid Product ref {ref!r}")
    return ref[len(prefix) :]


def _product(record: dict[str, Any]) -> Product:
    return Product(
        ref=str(record["ref"]),
        title=str(record["title"]),
        state=ProductState.ARCHIVED if bool(record.get("closed")) else ProductState.ACTIVE,
        projects=tuple(str(project) for project in record.get("projects") or ()),
        description=str(record.get("description") or ""),
    )


def _issue(record: dict[str, Any]) -> Issue:
    return Issue(
        ref=str(record["ref"]),
        title=str(record["title"]),
        product_ref=f"product:{record['product']}",
        state=IssueState.CLOSED if bool(record.get("closed")) else IssueState.OPEN,
        priority=str(record.get("priority") or ""),
        issue_kind=str(record.get("kind") or ""),
        description=str(record.get("description") or ""),
        close_reason=record.get("close_reason"),
    )


def _sprint(record: dict[str, Any]) -> Sprint:
    status = str(record.get("status") or "open")
    try:
        state = SprintState(status)
    except ValueError as exc:
        raise BoardProtocolError(f"invalid Sprint lifecycle state {status!r}") from exc
    return Sprint(
        ref=str(record["ref"]),
        goal=str(record.get("goal") or ""),
        state=state,
        product_ref=f"product:{record['product']}" if record.get("product") else None,
        issue_refs=tuple(str(ref) for ref in record.get("issues") or ()),
        card_refs=tuple(str(card.get("ref")) for card in record.get("cards") or () if isinstance(card, dict)),
    )


def _card(record: dict[str, Any]) -> Card:
    if record.get("record_type") in {"issue", "product"}:
        raise BoardProtocolError(f"{record.get('ref')!r} is not an execution Card")
    state = str(record.get("state") or "")
    try:
        lifecycle = CardState(state)
    except ValueError as exc:
        raise BoardProtocolError(f"invalid Card lifecycle state {state!r}") from exc
    return Card(
        ref=str(record["ref"]),
        title=str(record.get("title") or ""),
        state=lifecycle,
        sprint_ref=str(record["sprint"]) if record.get("sprint") else None,
        description=str(record.get("description") or ""),
    )


def _comment_saved(result: Any) -> bool:
    return result is True or (isinstance(result, int) and not isinstance(result, bool) and result > 0)


def _entity_payload(entity: Product | Issue) -> dict[str, Any]:
    if isinstance(entity, Product):
        return {
            "ref": entity.ref,
            "title": entity.title,
            "state": entity.state.value,
            "projects": list(entity.projects),
            "description": entity.description,
        }
    return {
        "ref": entity.ref,
        "title": entity.title,
        "product_ref": entity.product_ref,
        "state": entity.state.value,
        "priority": entity.priority,
        "issue_kind": entity.issue_kind,
        "description": entity.description,
        "close_reason": entity.close_reason,
    }


def _event_data(entity: Product | Issue, append: DescriptionAppend | None, edit: DescriptionEdit | None = None) -> dict[str, Any]:
    """The normalized entity plus explicit description-operation evidence, when present."""
    data = _entity_payload(entity)
    if append is not None:
        data["append"] = append.event_data()
    if edit is not None:
        data["edit"] = edit.event_data()
    return data


def _require_description_append(current: Issue, successor: Issue, append: DescriptionAppend) -> None:
    """Admit the old description byte for byte followed by a non-empty block, and nothing else."""
    kept = (
        successor.title,
        successor.product_ref,
        successor.state,
        successor.priority,
        successor.issue_kind,
        successor.close_reason,
    ) == (
        current.title,
        current.product_ref,
        current.state,
        current.priority,
        current.issue_kind,
        current.close_reason,
    )
    if (
        not kept
        or len(successor.description) <= len(current.description)
        or not successor.description.startswith(current.description)
        or _digest(current.description) != append.description_sha256_was
        or _digest(successor.description) != append.description_sha256
    ):
        raise BoardProtocolError("Issue replace appends only a non-empty block after the current description")


def _require_description_edit(current: Issue, successor: Issue, edit: DescriptionEdit) -> None:
    from dataclasses import replace

    if (replace(successor, description=current.description) != current
            or _digest(current.description) != edit.description_sha256_was
            or _digest(successor.description) != edit.description_sha256):
        raise BoardProtocolError("Issue description edit changes only the exact current description")


def _issue_from_payload(data: dict[str, Any]) -> Issue:
    try:
        return Issue(
            str(data["ref"]),
            str(data["title"]),
            str(data["product_ref"]),
            IssueState(str(data["state"])),
            str(data["priority"]),
            str(data["issue_kind"]),
            str(data.get("description") or ""),
            str(data.get("close_reason") or "") or None,
        )
    except (KeyError, ValueError) as exc:
        raise BoardProtocolError("pending Issue event has invalid normalized evidence") from exc


def _product_from_payload(data: dict[str, Any]) -> Product:
    try:
        projects = data["projects"]
        if not isinstance(projects, list):
            raise TypeError("projects")
        return Product(
            str(data["ref"]),
            str(data["title"]),
            ProductState(str(data["state"])),
            tuple(str(project) for project in projects),
            str(data.get("description") or ""),
        )
    except (KeyError, ValueError) as exc:
        raise BoardProtocolError("pending Product event has invalid normalized evidence") from exc
