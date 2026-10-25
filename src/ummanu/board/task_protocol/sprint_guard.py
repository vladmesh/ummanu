"""Sprint reservations, executor pins and role identity before task effects."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from ummanu.board import sprint_dod
from ummanu.board.backend import BOARD_STORE_KIND
from ummanu.board.models import Actor, EntityKind, Event, EventKind
from ummanu.board.protocol_artifacts import ArtifactOwnershipViolation
from ummanu.board.roles import Role
from ummanu.board.task_protocol.rows import (
    ArtifactOwnershipTaskError,
    SprintReservationUnverifiable,
    _artifact_ownership_refusal_request_id,
    _now,
    _sprint_guard_denial_request_id,
    _sprint_guard_override_request_id,
    _task_number,
)
from ummanu.kernel.errors import TaskError as TaskError


class SprintGuardMixin:

    def bind_dod(
        self, *, role: str, actor: str, reference: str, sprint: str, dod_item: str,
        sprint_override: bool = False, sprint_override_reason: str = "",
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Bind an unlinked card of an open sprint once; a saved request replays its result."""
        role = self._role(role, {Role.OBSERVER, Role.PO}, actor=actor)
        request_id = request_id or str(uuid.uuid4())
        if not sprint or not dod_item:
            raise TaskError("validation", "binding requires --sprint and one --dod-item d<N>", 2)
        reason = self._redact_for_board(sprint_override_reason.strip())
        identity = {"sprint": sprint, "dod_item": dod_item, "sprint_override": sprint_override,
                    "sprint_override_reason": reason}
        self._guard_observer_identity(role=role, actor=actor, project="", card_sprint=sprint,
                                      request_id=request_id, reference=reference)

        def payload(task: dict[str, Any]) -> dict[str, Any]:
            if task["sprint"] != sprint:
                raise TaskError("validation", "the card does not belong to the named sprint", 2)
            entity = self._sprint_entity(sprint)
            if entity["status"] != "open":
                raise TaskError("closed", "binding requires an open sprint", 3)
            try:
                sprint_dod.card_item(entity, dod_item)
            except ValueError as exc:
                raise TaskError("validation", str(exc), 2) from None
            if task["dod_item"] is not None and task["dod_item"] != dod_item:
                raise TaskError("validation", "the card already links another Definition of Done item", 2)
            self._guard_sprint_write(
                role=role, actor=actor, project=task["project"], card_sprint=sprint,
                linked_sprint=entity, sprint_override=sprint_override,
                sprint_override_reason=reason, request_id=request_id, reference=reference,
            )
            return identity

        def mutate(task: dict[str, Any]) -> None:
            if task["dod_item"] is None:
                saved = self.client.call("saveTaskMetadata", task_id=_task_number(task), values={"dod_item": dod_item})
                if saved is not True or self.reader.show(reference)["dod_item"] != dod_item:
                    raise TaskError("backend_error", "board store did not save the Definition of Done link", 1)

        return self._write("dod_item_bound", role, actor, reference, request_id, payload, mutate, identity=identity)

    def _sprint_holds_project(self, project: str) -> bool:
        """Whether an open sprint reserves this card's project."""
        from ummanu.sprints import active_sprint_projects

        return bool(active_sprint_projects(self.data_dir).get(project))

    def _sprint_executor_pins(
        self,
        *,
        sprint_ref: str,
        head: str | None,
        review_head: str | None,
        sprint: dict[str, Any] | None = None,
    ) -> tuple[str | None, str | None]:
        """Enforce a sprint's worker/reviewer profile pins for both create and edit;
        dispatcher launch uses the declared profile. `None` means no field write, empty
        string inherits a pin, and a conflicting profile is refused. Unpinned roles
        remain unchanged; unreadable pin metadata fails closed.
        """
        from ummanu.sprint_observer import EXECUTOR_FIELDS, EXECUTOR_PINNED, EXECUTOR_UNSET

        requested: dict[str, str | None] = {"worker": head, "reviewer": review_head}
        if not sprint_ref or (head is None and review_head is None):
            return requested["worker"], requested["reviewer"]
        entity = sprint if sprint is not None else self._sprint_entity(sprint_ref)
        states = entity.get("executors") or {}
        for role in EXECUTOR_FIELDS:
            asked = requested[role]
            if asked is None:
                continue
            state = states.get(role) or {"state": EXECUTOR_UNSET}
            if state.get("state") == EXECUTOR_UNSET:
                continue
            if state.get("state") != EXECUTOR_PINNED:
                raise TaskError(
                    "sprint_executor_unreadable",
                    f"sprint {sprint_ref} carries a {role} pin that is not a head profile; repair "
                    "the sprint entity before writing its cards",
                    3,
                )
            profile = str(state.get("profile") or "")
            if asked and asked != profile:
                raise TaskError(
                    "sprint_executor_pinned",
                    f"sprint {sprint_ref} pins its {role} to head profile {profile!r}; this card "
                    f"asks for {asked!r}",
                    3,
                )
            requested[role] = profile
        return requested["worker"], requested["reviewer"]

    def _po_session_state(self, session_id: str) -> str:
        """The PO session's state (`open`/`closed`), `""` when the PO store has no such session.

        Raises `TaskError` when the store cannot answer: an unverifiable address is not admitted.
        """
        from ummanu.po.store import PoStore, SessionNotFound

        try:
            return str(PoStore.for_instance(self.instance_dir).session(session_id).state)
        except SessionNotFound:
            return ""
        except Exception as exc:  # noqa: BLE001 - a store error, or credentials that cannot be read
            raise TaskError(
                "po_store_unavailable",
                f"cannot verify PO session {session_id}: the PO store did not answer ({type(exc).__name__})",
                1,
            ) from None

    def _refuse_unknown_po_sessions(self, session_ids: Iterable[str]) -> None:
        """A wait card's `po-session:<id>` must name a session the PO store holds and that is open."""
        from ummanu.po.store import SESSION_CLOSED

        for session_id in session_ids:
            state = self._po_session_state(session_id)
            if not state:
                raise TaskError("validation", f"--wait-return po-session:{session_id} names no PO session", 2)
            if state == SESSION_CLOSED:
                raise TaskError(
                    "validation",
                    f"--wait-return po-session:{session_id} names a closed PO session; it takes no input",
                    2,
                )

    def _refuse_unpinned_reviewer_on_skipped(
        self, *, sprint_ref: str, review_head: str, sprint: dict[str, Any] | None = None
    ) -> None:
        """Refuse a caller-named reviewer on a card whose review is skipped, unless the
        sprint pins it. The review choice decides whether review runs and the sprint
        pin decides who reviews, so a skipped card may carry the pinned reviewer,
        applied by the pin or named explicitly.
        """
        if not review_head:
            return
        _, pinned = self._sprint_executor_pins(
            sprint_ref=sprint_ref, head=None, review_head="", sprint=sprint
        )
        if review_head != (pinned or ""):
            raise TaskError(
                "validation",
                f"--review-head {review_head!r} names a reviewer for a card whose review is skipped",
                2,
            )

    def _sprint_entity(self, reference: str) -> dict[str, Any]:
        """The sprint a card names, read here only to answer what it pins. A sprint
        that cannot be read fails closed: the alternative is writing a profile onto a
        card whose constraint nobody could check, which is the one outcome the pin
        exists to prevent.
        """
        from ummanu.sprints import SprintReader

        try:
            return SprintReader(self.client).show(reference, include_cards=False)
        except TaskError as exc:
            raise TaskError(
                "sprint_executor_unreadable",
                f"sprint {reference} cannot be read, so its executor pins cannot be checked: {exc.message}",
                3,
            ) from None

    def open_sprints_reserving(
        self, project: str, *, linked_sprint: dict[str, Any] | None = None
    ) -> list[str]:
        """Read the local reservation index and verify every named sprint against the
        live board; seed a missing index. Unreadable reservations raise
        `SprintReservationUnverifiable`, failing closed for both write and dispatch
        admission.
        """
        from ummanu.sprints import (
            SprintReader,
            active_sprint_projects,
            refresh_active_sprint_projects,
            sprint_guard_index_initialized,
            update_active_sprint_projects,
        )

        if not sprint_guard_index_initialized(self.data_dir):
            try:
                refresh_active_sprint_projects(self.data_dir, SprintReader(self.client))
            except TaskError as exc:
                raise SprintReservationUnverifiable("", exc) from exc

        refs = set(active_sprint_projects(self.data_dir).get(project, []))
        if linked_sprint is not None and project in linked_sprint.get("reservations", []):
            refs.add(str(linked_sprint["ref"]))
        held: list[str] = []
        for sprint_ref in sorted(refs):
            try:
                sprint = (
                    linked_sprint
                    if linked_sprint and sprint_ref == linked_sprint.get("ref")
                    else SprintReader(self.client).show(sprint_ref, include_cards=False)
                )
            except TaskError as exc:
                raise SprintReservationUnverifiable(sprint_ref, exc) from exc
            update_active_sprint_projects(self.data_dir, sprint)
            if sprint.get("status") == "open" and project in sprint.get("reservations", []):
                held.append(sprint_ref)
        return held

    def _guard_sprint_write(
        self,
        *,
        role: str,
        actor: str,
        project: str,
        card_sprint: str,
        linked_sprint: dict[str, Any] | None,
        sprint_override: bool,
        sprint_override_reason: str,
        request_id: str,
        reference: str,
        steward_report: bool = False,
        po_card: bool = False,
        card_executor: bool = False,
    ) -> dict[str, str]:
        """Authorize a create/move/edit by first verifying the observer's launch-bound
        sprint identity, then the live project reservation. An unbound/mismatched
        observer fails closed. Verified exceptions: unsprinted PO cards, steward
        reports, PO-created delegated decision/operation cards and a `card_executor`
        writing a card of the holding sprint; normal dispatcher admission remains separate.
        """
        self._guard_observer_identity(
            role=role,
            actor=actor,
            project=project,
            card_sprint=card_sprint,
            request_id=request_id,
            reference=reference,
        )
        try:
            held = self.open_sprints_reserving(project, linked_sprint=linked_sprint)
        except SprintReservationUnverifiable as exc:
            message = (
                f"cannot verify sprint {exc.sprint_ref} reserving project {project}; write it through the sprint entity"
                if exc.sprint_ref
                else f"cannot verify open sprints for project {project}; write it through the sprint entity"
            )
            self._deny_sprint_write(
                code="sprint_guard_unavailable",
                message=message,
                role=role,
                actor=actor,
                project=project,
                sprint=exc.sprint_ref,
                request_id=request_id,
                reference=reference,
            )
            raise AssertionError("unreachable") from exc
        if not held:
            return {}
        sprint_ref = card_sprint if card_sprint in held else held[0]
        if role == "po" and sprint_override:
            if not sprint_override_reason:
                self._deny_sprint_write(
                    code="validation",
                    message="sprint override requires a non-empty reason",
                    role=role,
                    actor=actor,
                    project=project,
                    sprint=sprint_ref,
                    request_id=request_id,
                    reference=reference,
                    exit_code=2,
                )
            self._grant_sprint_override(
                role=role,
                actor=actor,
                project=project,
                sprint=sprint_ref,
                reason=sprint_override_reason,
                request_id=request_id,
                reference=reference,
            )
            return {"sprint_override_reason": sprint_override_reason}
        # A PO card linked to no sprint is not the holding sprint's work, whatever its kind: whether
        # it runs on a reserved project is the dispatcher's admission (`open_sprints_reserving`
        # asked before the claim), not this guard. A card linked to a sprint, and a create that
        # links one, keep the override rule above.
        if role == "po" and not card_sprint and linked_sprint is None:
            return {}
        if role == "po" and po_card:
            return {}
        # The observer's own headless card (a wait) touches no branch any sprint owns; its identity
        # as this card's sprint's observer was proven above.
        if role == "observer" and po_card and card_sprint:
            return {}
        # The steward's own report card is its tick's accounting, created In progress as research
        # and linked to no sprint; the dispatcher never claims it. Its proposals and every other
        # card it touches stay the holding sprint's to refuse.
        if role == "steward" and steward_report and not card_sprint and linked_sprint is None:
            return {}
        # The caller was already proven to be this card's sprint's observer above; what is left is
        # that the sprint holding the project is the one the card is linked to.
        if role == "observer" and card_sprint in held:
            return {}
        if role == "dispatcher":
            return {}
        if card_executor and card_sprint in held:
            return {}
        self._deny_sprint_write(
            code="sprint_write_forbidden",
            message=f"project {project} is reserved by open sprint {sprint_ref}; write it through the sprint entity {sprint_ref}",
            role=role,
            actor=actor,
            project=project,
            sprint=sprint_ref,
            request_id=request_id,
            reference=reference,
        )
        raise AssertionError("unreachable")

    def _guard_observer_identity(
        self,
        *,
        role: str,
        actor: str,
        project: str,
        card_sprint: str,
        request_id: str,
        reference: str,
    ) -> None:
        """Require the observer's launch-bound sprint to match the card's sprint.
        Missing binding and mismatched binding are distinct refusals, neither a generic
        role denial; unsprinted cards use the reservation guard.
        """
        if role != "observer":
            return
        from ummanu.runtime.role_env import declared_observer_sprint

        declared = declared_observer_sprint()
        if not declared:
            self._deny_sprint_write(
                code="observer_identity_unbound",
                message="this observer names no sprint, so its writes cannot be authenticated; "
                "it has to be launched by the dispatcher for one sprint",
                role=role,
                actor=actor,
                project=project,
                sprint="",
                request_id=request_id,
                reference=reference,
            )
        if card_sprint and card_sprint != declared:
            self._deny_sprint_write(
                code="observer_sprint_mismatch",
                message=f"this observer belongs to sprint {declared} and the card is linked to "
                f"{card_sprint}; write it as that sprint's observer",
                role=role,
                actor=actor,
                project=project,
                sprint=declared,
                request_id=request_id,
                reference=reference,
            )

    def _grant_sprint_override(
        self,
        *,
        role: str,
        actor: str,
        project: str,
        sprint: str,
        reason: str,
        request_id: str,
        reference: str,
    ) -> None:
        """Record the granted single-writer override before the operation it authorizes
        runs. A generic control-plane record like the denial, not part of the Card's
        typed event: the event describes the lifecycle edge, this describes the
        authority the writer used.
        """
        override_request_id = _sprint_guard_override_request_id(request_id)
        if self.audit.committed_event(override_request_id) is not None:
            return
        event = {
            "event_id": "evt_" + uuid.uuid4().hex,
            "schema_version": 1,
            "occurred_at": _now(),
            "actor": {"role": role, "id": actor},
            "kind": "sprint_guard_override",
            "outcome": "granted",
            "task_id": "",
            "ref": reference,
            "backend": {"kind": BOARD_STORE_KIND, "task_id": None, "revision": "not_written"},
            "request_id": override_request_id,
            "payload": {
                "project": project,
                "sprint": sprint,
                "sprint_override_reason": reason,
                "operation_request_id": request_id,
            },
        }
        self.audit.stage(override_request_id, event)
        try:
            self.audit.append(override_request_id, event)
        except OSError:
            raise TaskError(
                "audit_pending",
                "sprint override was granted but audit repair is required",
                4,
            ) from None

    def _deny_sprint_write(
        self,
        *,
        code: str,
        message: str,
        role: str,
        actor: str,
        project: str,
        sprint: str,
        request_id: str,
        reference: str,
        exit_code: int = 3,
    ) -> None:
        denial_request_id = _sprint_guard_denial_request_id(request_id)
        event = self.audit.committed_event(denial_request_id)
        if event is None:
            event = {
                "event_id": "evt_" + uuid.uuid4().hex,
                "schema_version": 1,
                "occurred_at": _now(),
                "actor": {"role": role, "id": actor},
                "kind": "sprint_guard_denied",
                "outcome": "denied",
                "task_id": "",
                "ref": reference,
                "backend": {"kind": BOARD_STORE_KIND, "task_id": None, "revision": "not_written"},
                "request_id": denial_request_id,
                "payload": {
                    "code": code,
                    "message": message,
                    "project": project,
                    "sprint": sprint,
                    "operation_request_id": request_id,
                },
            }
            self.audit.stage(denial_request_id, event)
            try:
                self.audit.append(denial_request_id, event)
            except OSError:
                raise TaskError(
                    "audit_pending", "sprint write was denied but audit repair is required", 4
                ) from None
        payload = event.get("payload") if isinstance(event, dict) else {}
        raise TaskError(str(payload.get("code") or code), str(payload.get("message") or message), exit_code)

    def _deny_rework_artifact_ownership(
        self,
        *,
        violation: ArtifactOwnershipViolation,
        role: str,
        actor: str,
        reference: str,
        request_id: str,
        protocol_prerequisites: tuple[str, ...],
    ) -> None:
        """Persist the denied rework without mutating its parked card. The derived
        audit key lets the observer correct the instruction and reuse the decision
        request identity: a refusal is evidence, not an authoritative decision or
        worker outcome.
        """
        refusal_request_id = _artifact_ownership_refusal_request_id(request_id)
        data = {
            "decision": "rework",
            "code": "artifact_ownership_violation",
            "artifact": violation.artifact.name,
            "artifact_owner": violation.artifact.owner.value,
            "requested_role": violation.requested_role.value,
            "specification_revision": violation.specification_revision,
            "protocol_prerequisites": list(protocol_prerequisites),
        }
        existing = self.board_host.canon.event(refusal_request_id)
        if existing is None:
            event = Event(
                "evt_artifact_ownership_" + hashlib.sha256(refusal_request_id.encode("utf-8")).hexdigest(),
                EventKind.CARD_DECISION_REFUSED,
                EntityKind.CARD,
                reference,
                Actor(role, actor),
                violation.message,
                datetime.now(UTC),
                data=data,
            )
        else:
            if (
                existing.kind is not EventKind.CARD_DECISION_REFUSED
                or existing.ref != reference
                or existing.data != data
            ):
                raise TaskError("validation", "request id belongs to another operation or payload", 2)
            event = existing
        try:
            self.board_host.canon.commit(refusal_request_id, event)
        except (OSError, ValueError) as exc:
            raise TaskError("audit_pending", "artifact ownership refusal requires audit repair", 4) from exc
        raise ArtifactOwnershipTaskError(violation)
