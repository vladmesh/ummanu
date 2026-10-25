"""Task archive/restore, audited writes and pending-effect recovery."""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import Callable, Collection, Iterator
from typing import Any

from ummanu.board import owner_events
from ummanu.board.audit_contract import is_protocol_event
from ummanu.board.backend import BOARD_STORE_KIND, entity_id
from ummanu.board.events import BoardEventPending
from ummanu.board.host import MarkerComment
from ummanu.board.legacy_codec import positive_int as _positive_int, text as _text
from ummanu.board.models import Actor, EventKind
from ummanu.board.roles import Role
from ummanu.board.task_protocol.rows import (
    _CODEX_LAUNCH_MODES,
    _MARKER_EVENT_ACTIONS,
    _READY_RESET_METADATA,
    _TYPED_RECORD_TYPES,
    _audit_repair_required,
    _check_execution_record,
    _CommittedWriteError,
    _create_metadata_values,
    _digest,
    _dispatcher_record_has_live_work,
    _done_retention_request_id,
    _has_archive_reason,
    _is_steward_report,
    _is_steward_report_card,
    _matches_optional,
    _normalize_comment,
    _now,
    _ready_cleanup_incomplete,
    _revision,
    _target_column_id,
    _task_is_active,
    _task_metadata,
    _task_number,
    admit_role,
    all_project_cards,
    project_card_by_reference,
)
from ummanu.board.task_routing import ACTIVE_STATES
from ummanu.board.transitions import BoardProtocolError
from ummanu.dispatch.ownership import bulk_lane, ownership_recovery, reference_lock
from ummanu.kernel.errors import TaskError as TaskError

_CANCEL_HINT = "`task cancel --ref <card> --reason <text>` stops its heads, releases it and archives it"


class RecoveryMixin:

    def _legacy_move(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        target: str,
        reason: str,
        decision: str,
        sprint_override: bool,
        sprint_override_reason: str,
        request_id: str,
        task: dict[str, Any],
    ) -> dict[str, Any]:
        """Replay a move this request id already recorded as a released generic operation."""
        override_payload = self._guard_sprint_write(
            role=role,
            actor=actor,
            project=task["project"],
            card_sprint=str(task.get("sprint") or ""),
            linked_sprint=None,
            sprint_override=sprint_override,
            sprint_override_reason=sprint_override_reason.strip(),
            request_id=request_id,
            reference=reference,
            steward_report=_is_steward_report_card(task),
        )
        return self._write(
            "moved",
            role,
            actor,
            reference,
            request_id,
            lambda task: {
                "from": task["state"],
                "to": target,
                "reason_sha256": _digest(reason) if reason else None,
                **({"decision": decision} if decision else {}),
                **override_payload,
            },
            lambda task: None,
            # `from` is the column the move already left, so it is the one field a retry cannot
            # recompute. Everything the caller asked for is compared.
            identity={
                "to": target,
                "reason_sha256": _digest(reason) if reason else None,
                "decision": decision or None,
                "sprint_override_reason": override_payload.get("sprint_override_reason"),
            },
        )

    def _legacy_record(self, request_id: str) -> dict[str, Any] | None:
        """The released generic audit record this request id owns, if it owns one.

        The two representations are told apart by the record's own discriminator, never by guessing from
        a payload.
        """
        record = self.audit.committed_event(request_id) or self.audit.pending_event(request_id)
        if record is None or is_protocol_event(record):
            return None
        return record

    def archive(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        reason: str,
        request_id: str | None = None,
        sprint_close: str = "",
    ) -> dict[str, Any]:
        """Archive one card: the PO's write, and a step of a sprint close in the
        closer's name. `sprint_close` names the sprint whose close this archive is a
        step of.
        """
        role = self._role(role, {Role.PO, Role.OBSERVER} if sprint_close else {Role.PO}, actor=actor)
        reason = self._redact_for_board(reason)
        if not reason.strip():
            raise TaskError("validation", "archive requires a non-empty reason", 2)
        request_id = request_id or str(uuid.uuid4())
        if role == "observer":
            self._guard_observer_identity(
                role=role,
                actor=actor,
                project="",
                card_sprint=sprint_close,
                request_id=request_id,
                reference=reference,
            )

        def mutation(task: dict[str, Any]) -> Any:
            if task.get("record_type") in {"issue", "product"}:
                raise TaskError(
                    "transition_forbidden",
                    "Product issues must be closed with ummanu issue close; products cannot be archived",
                    3,
                )
            self._check_archivable(task)
            self._check_dispatcher_archivable(reference)
            self._request_workspace_cleanup(task, "close" if sprint_close else "archive")
            try:
                self.client.call(
                    "createComment",
                    task_id=_task_number(task),
                    user_id=0,
                    content=f"[archive]\n{reason}",
                )
                if not self.client.call("closeTask", task_id=_task_number(task)):
                    raise TaskError("backend_error", "board store rejected the archive", 1)
            except Exception as exc:
                raise _CommittedWriteError() from exc

        return self._write(
            "archived",
            role,
            actor,
            reference,
            request_id,
            {"reason_sha256": _digest(reason)},
            mutation,
            retry_payload={"reason": reason},
            identity={"reason_sha256": _digest(reason)},
        )

    def retire_done(
        self,
        *,
        reference: str,
        expected_date_moved: int,
        cutoff: float,
        retention_days: int,
        actor: str = "retro-retention",
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Close one proven-old Done episode without using the PO archive path.
        ``date_moved`` identifies the episode, rather than merely the card.
        """
        expected_date_moved = _positive_int(expected_date_moved) or 0
        if not expected_date_moved:
            return {"action": "retired", "reference": reference, "retired": False, "skipped": True}
        try:
            cutoff_value = float(cutoff)
        except (TypeError, ValueError):
            raise TaskError("validation", "Done retention requires a numeric cutoff", 2) from None
        if retention_days < 0:
            raise TaskError("validation", "Done retention days cannot be negative", 2)

        initial = self._retention_card(reference, task_id=None)
        if initial is None:
            return {"action": "retired", "reference": reference, "retired": False, "skipped": True}
        task_id, raw, metadata, done_id = initial
        self._check_retention_record(metadata)
        request_id = request_id or _done_retention_request_id(task_id, expected_date_moved)
        identity = {
            "expected_date_moved": expected_date_moved,
            "cutoff": cutoff_value,
            "retention_days": retention_days,
            "task_id": task_id,
        }
        committed = self.audit.committed_event(request_id)
        if committed is not None:
            self.audit.require_claim(committed, kind="retired", reference=reference, identity=identity)
            return {"action": "retired", "reference": reference, "retired": True, "replayed": True}
        pending = self.audit.pending_event(request_id)
        if pending is not None:
            self.audit.require_claim(pending, kind="retired", reference=reference, identity=identity)
            try:
                self._finish_pending_retired(pending)
                self._prove_retired_closed(pending)
                self.audit.append(request_id, pending)
            except (TaskError, OSError, KeyError, TypeError, ValueError):
                raise _audit_repair_required() from None
            return {"action": "retired", "reference": reference, "retired": True, "replayed": True}

        # Do not stage a successful-looking occurrence for a candidate that has
        # already aged out of eligibility between the list and this write.
        if not self._retention_matches(raw, metadata, expected_date_moved, cutoff_value, done_id):
            return {"action": "retired", "reference": reference, "retired": False, "skipped": True}
        event = {
            "event_id": "evt_" + uuid.uuid4().hex,
            "schema_version": 1,
            "occurred_at": _now(),
            "actor": {"role": "retro", "id": actor},
            "kind": "retired",
            "outcome": "success",
            "task_id": entity_id("task", task_id),
            "ref": reference,
            "backend": {"kind": BOARD_STORE_KIND, "task_id": task_id, "revision": "pending"},
            "request_id": request_id,
            "payload": identity,
        }
        # The same boundary the card protocol's other mutations stand on (§7.1): the freshness
        # guard, the destructive close, its proof and the record are one transaction where the
        # backend has one.  Retention is a Card protocol mutation with a board effect, not an
        # effect outside the board, so leaving it at `_depth == 0` left exactly the half-applied
        # state §7.3 says this backend does not have: a closed card beside a staged request.  For a
        # client without transactions `_mutation()` is nothing at all, so the ambiguity below — and the pending
        # record `reconcile` settles from it — is untouched.
        with reference_lock(self.data_dir, reference), self._mutation():
            self.audit.stage(request_id, event)
            try:
                # This is the final guard immediately before the destructive call.
                guarded = self._retention_card(reference, task_id=task_id)
                if guarded is None:
                    self.audit.discard(request_id, event)
                    return {"action": "retired", "reference": reference, "retired": False, "skipped": True}
                _guarded_id, latest, latest_metadata, latest_done_id = guarded
                self._check_retention_record(latest_metadata)
                if not self._retention_matches(
                    latest, latest_metadata, expected_date_moved, cutoff_value, latest_done_id
                ):
                    self.audit.discard(request_id, event)
                    return {"action": "retired", "reference": reference, "retired": False, "skipped": True}
                if not self.client.call("closeTask", task_id=task_id):
                    raise TaskError("backend_error", "board store rejected Done retention", 1)
            except _CommittedWriteError:
                raise self._post_effect_refusal("the Done retention close") from None
            except TaskError as exc:
                # A transport error after close is ambiguous; leave its pending
                # evidence.  A definite local guard/validation failure is not.
                if exc.code == "backend_unavailable":
                    raise self._post_effect_refusal("the Done retention close") from None
                current = self.audit.pending_event(request_id)
                if current == event:
                    try:
                        self.audit.discard(request_id, event)
                    except (OSError, TaskError):
                        pass
                raise
            except Exception:  # noqa: BLE001 - an unknown close reply is deliberately ambiguous.
                # A lost reply can arrive after the board applied the
                # close, so reconciliation must prove or safely retry this episode.
                raise self._post_effect_refusal("the Done retention close") from None
            try:
                self._finish_pending_retired(event)
                self._prove_retired_closed(event)
                self.audit.append(request_id, event)
            except (TaskError, OSError, KeyError, TypeError, ValueError):
                raise self._post_effect_refusal("the Done retention close") from None
            return {"action": "retired", "reference": reference, "retired": True, "replayed": False}

    def restore_card(
        self,
        *,
        reference: str,
        metadata: dict[str, str],
        target: str,
        position: int | None = None,
        swimlane: str = "",
        request_id: str | None = None,
    ) -> dict[str, Any]:
        from ummanu.task_restore import restore_card

        return restore_card(self, reference, metadata, target, position, swimlane, request_id)

    def restore_comment(
        self, *, reference: str, body: str, occurrence: int, request_id: str | None = None
    ) -> dict[str, Any]:
        from ummanu.task_restore import restore_comment

        return restore_comment(self, reference, body, occurrence, request_id)

    def reconcile_restore_order(
        self,
        *,
        column: str,
        swimlane: str,
        references: list[str],
        request_id: str,
    ) -> None:
        from ummanu.task_restore import reconcile_restore_order

        reconcile_restore_order(self, column, swimlane, references, request_id)

    def _move_raw(self, task: dict[str, Any], target: str, *, position: int = 1, swimlane_id: int) -> None:
        board_id, columns, _ = self.reader._board()
        column_id = _target_column_id(columns, target)
        if column_id is None:
            raise TaskError("backend_error", "board schema is invalid", 1)
        ok = self.client.call(
            "moveTaskPosition",
            project_id=board_id,
            task_id=_task_number(task),
            column_id=column_id,
            position=position,
            swimlane_id=swimlane_id,
        )
        if not ok:
            raise TaskError("backend_error", "board store rejected the write", 1)

    def _current_swimlane_id(self, task: dict[str, Any]) -> int:
        board_id, _, _ = self.reader._board()
        raw = project_card_by_reference(self.client, board_id, task["ref"])
        if not isinstance(raw, dict):
            raise TaskError("not_found", "task was not found", 2)
        return _positive_int(raw.get("swimlane_id")) or 0

    def _marker_write(
        self,
        *,
        action: str,
        event_kind: EventKind,
        role: str,
        actor: str,
        reference: str,
        reason: str,
        request_id: str | None,
        data: dict[str, Any],
        require_assessment: bool = False,
        fresh_admission: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Send a control-plane marker through the typed host transaction. The command
        supplies its semantic fields once; the host stages them as an immutable event
        and derives the board comment from that event, so a retry never has a second
        representation to drift from.
        """
        request_id = request_id or str(uuid.uuid4())
        if require_assessment:
            current = self.reader.show(reference)
            if current["state"] != "assessment":
                raise TaskError(
                    "transition_forbidden", "a decision is only recorded on a card in Assessment", 3
                )
        try:
            with self._mutation():
                result = self.board_host.marker_comment(
                    MarkerComment(
                        reference,
                        event_kind,
                        Actor(role, actor),
                        reason,
                        data,
                        request_id=request_id,
                        fresh_admission=fresh_admission,
                    )
                )
        except BoardEventPending:
            raise _audit_repair_required() from None
        except ValueError as exc:
            message = str(exc)
            if "released generic audit record" in message:
                message = "request id belongs to another operation or payload"
            raise TaskError("validation", message, 2) from None
        except BoardProtocolError as exc:
            raise TaskError("backend_error", str(exc), 1) from None
        return {
            "action": action,
            "task": self.reader.show(reference),
            "event_id": result.event.event_id,
            "replayed": result.replayed,
        }

    def _write(
        self,
        kind: str,
        role: str,
        actor: str,
        reference: str,
        request_id: str | None,
        payload: dict[str, Any] | Callable[[dict[str, Any]], dict[str, Any]],
        mutation: Any,
        *,
        identity: dict[str, Any],
        retry_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Every request id explicitly declares the operation identity it owns.
        request_id = request_id or str(uuid.uuid4())
        committed = self.audit.committed_event(request_id)
        if committed is not None:
            self.audit.require_claim(committed, kind=kind, reference=reference, identity=identity)
            try:
                event_id = self.audit.append(request_id, committed)
            except OSError:
                raise _audit_repair_required() from None
            return {
                "action": kind,
                "task": self.reader.show(reference),
                "event_id": event_id,
                "replayed": True,
            }
        pending = self.audit.pending_event(request_id)
        if pending is not None:
            self.audit.require_claim(pending, kind=kind, reference=reference, identity=identity)
            try:
                self._finish_pending_cleanup(pending, retry_payload)
                task = self.reader.show(str(pending["ref"]))
                pending["task_id"] = task["id"]
                pending["backend"]["revision"] = _revision(task)
                self.audit.stage(request_id, pending)
                event_id = self.audit.append(request_id, pending)
            except (TaskError, OSError, KeyError, TypeError):
                raise _audit_repair_required() from None
            return {
                "action": kind,
                "task": self.reader.show(reference),
                "event_id": event_id,
                "replayed": True,
            }
        ownership_change = kind in {"claimed", "moved", "archived", "completed", "restored", "retired", "dod_item_bound"}
        with (reference_lock(self.data_dir, reference) if ownership_change else contextlib.nullcontext()), (
            reference_lock(self.data_dir, "capacity", lane="admission")
            if kind in {"claimed", "moved", "restored"} else contextlib.nullcontext()
        ), self._mutation():
            return self._write_effect(
                kind, role, actor, reference, request_id, payload, mutation, identity=identity
            )

    @property
    def _transactional(self) -> bool:
        """Whether `_mutation()` is a real transaction on this backend, asked in one
        place. The boundary and the refusal it produces have to agree about this, so
        they read the same predicate rather than each deciding for itself.
        """
        return getattr(self.client, "transaction", None) is not None

    @contextlib.contextmanager
    def _mutation(self) -> Iterator[None]:
        """One transaction per protocol mutation, where the backend has transactions
        (§7.1). For a client without transactions this is nothing at all: stage a
        record, apply one effect, confirm it, commit the record.
        """
        scope = getattr(self.client, "transaction", None)
        try:
            # Creates and after-merge admission can open SQL before reaching a
            # card fence. Order their transaction against bulk recovery first.
            with bulk_lane(self.data_dir), scope() if scope is not None else contextlib.nullcontext():
                yield
        except (owner_events.OwnerEventError, BoardEventPending) as exc:
            cause = exc.__cause__ if isinstance(exc, BoardEventPending) else exc
            if not isinstance(cause, owner_events.OwnerEventError):
                raise
            outcome = "mutation rolled back" if scope is not None else "mutation refused"
            raise TaskError(
                "backend_error",
                f"{cause}; {outcome}; restore owner-event availability and retry the same request ID",
                1,
            ) from cause

    def _post_effect_refusal(self, subject: str) -> TaskError:
        """Distinguish rollback from partial write on mutation failure.
        Nontransactional backends may commit a board effect without its audit and must
        report repair-required status 4. A transactional backend rolls back both
        effects and staged requests, so it returns an ordinary refusal without a
        phantom repair obligation.
        """
        if not self._transactional:
            return _audit_repair_required()
        return TaskError(
            "backend_error",
            f"{subject} did not happen: it was rolled back together with its record, "
            "so nothing was written and no repair is owed",
            1,
        )

    def _write_effect(
        self,
        kind: str,
        role: str,
        actor: str,
        reference: str,
        request_id: str,
        payload: dict[str, Any] | Callable[[dict[str, Any]], dict[str, Any]],
        mutation: Any,
        *,
        identity: dict[str, Any],
    ) -> dict[str, Any]:
        task = self.reader.show(reference)
        event_payload = payload(task) if callable(payload) else payload
        event = {
            "event_id": "evt_" + uuid.uuid4().hex,
            "schema_version": 1,
            "occurred_at": _now(),
            "actor": {"role": role, "id": actor},
            "kind": kind,
            "outcome": "success",
            "task_id": task["id"],
            "ref": reference,
            "backend": {
                "kind": BOARD_STORE_KIND,
                "task_id": _task_number(task),
                "revision": _revision(task),
            },
            "request_id": request_id,
            "payload": event_payload,
        }
        self.audit.stage(request_id, event)
        try:
            mutation(task)
        except _CommittedWriteError:
            raise _audit_repair_required() from None
        except Exception:
            self.audit.discard(request_id)
            raise
        try:
            task = self.reader.show(reference)
        except Exception:  # noqa: BLE001 - any post-write read failure is an ambiguous commit.
            raise _audit_repair_required() from None
        event["backend"]["revision"] = _revision(task)
        self.audit.stage(request_id, event)
        try:
            event_id = self.audit.append(request_id, event)
        except OSError:
            raise _audit_repair_required() from None
        return {"action": kind, "task": task, "event_id": event_id, "replayed": False}

    def reconcile(
        self, *, defer_restore_comments: bool = False, defer_bulk_restore: bool = False
    ) -> tuple[int, int]:
        repaired = 0
        unresolved = 0
        for event in self.audit.pending_events():
            try:
                if defer_restore_comments and event.get("kind") == "restored_comment":
                    continue
                if defer_bulk_restore and event.get("kind") == "restored_bulk":
                    continue
                if event.get("kind") == "restored_bulk":
                    # Normalized restore owns the canon and the whole-board evidence needed to
                    # finish this obligation. Generic per-card reconciliation must not publish it.
                    unresolved += 1
                    continue
                if is_protocol_event(event):
                    subject = event.get("subject") if isinstance(event.get("subject"), dict) else {}
                    if str(event.get("kind") or "") in _MARKER_EVENT_ACTIONS:
                        self.board_host.recover_marker_comment(str(event["request_id"]))
                        repaired += 1
                        continue
                    if subject.get("kind") == "sprint":
                        self.board_host.recover_sprint(str(event["request_id"]))
                        repaired += 1
                        continue
                    self._finish_pending_transition(event)
                    repaired += 1
                    continue
                if str(event.get("backend", {}).get("kind") or "") == "dispatcher":
                    # An observer lifecycle event describes a head, not a backend row: there is
                    # nothing to re-read and enrich, and it must repair even when the sprint it
                    # names has already left the board.
                    self.audit.append(str(event["request_id"]), event)
                    repaired += 1
                    continue
                if event.get("kind") in {
                    "sprint_guard_denied",
                    "sprint_guard_override",
                    "outcome_round_context",
                }:
                    # A guard decision records itself, not a backend row: there is nothing to
                    # re-read, and the decision it names was made whether or not the operation
                    # it authorized went on to succeed.
                    self.audit.append(str(event["request_id"]), event)
                    repaired += 1
                    continue
                if event.get("kind") == "reference_repaired":
                    from ummanu.board.reference_repair import finish_pending_reference_repair

                    finish_pending_reference_repair(self, event)
                    self.audit.stage(str(event["request_id"]), event)
                    self.audit.append(str(event["request_id"]), event)
                    repaired += 1
                    continue
                if event.get("kind") in {"product_created", "issue_created", "issue_closed"}:
                    # Product/Issue writes have ordered backend cleanup.  Only their supported
                    # command, retried with the original request id, can prove that cleanup.
                    unresolved += 1
                    continue
                if event.get("kind") == "restored_comment":
                    from ummanu.task_restore import finish_pending_restore_comment

                    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                    finish_pending_restore_comment(self, event, payload)
                    self.audit.stage(str(event["request_id"]), event)
                    self.audit.append(str(event["request_id"]), event)
                    repaired += 1
                    continue
                if event.get("kind") == "restored_order":
                    from ummanu.task_restore import finish_pending_restore_order

                    finish_pending_restore_order(self, event)
                    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                    event["backend"]["revision"] = f"order:{payload.get('references_sha256', '')}"
                    self.audit.stage(str(event["request_id"]), event)
                    self.audit.append(str(event["request_id"]), event)
                    repaired += 1
                    continue
                if str(event.get("ref") or "").startswith("sprint:"):
                    from ummanu.sprints import SprintWriter

                    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                    if event.get("kind") == "budget_recorded" and payload.get("hard_limit_stop") is True:
                        SprintWriter(self.client, data_dir=self.data_dir).record_budget(
                            role=str(event.get("actor", {}).get("role") or ""),
                            actor=str(event.get("actor", {}).get("id") or ""),
                            reference=str(event["ref"]),
                            event_type=str(payload.get("event_type") or ""),
                            request_id=str(event["request_id"]),
                            source_event_id=str(payload.get("source_event_id") or ""),
                        )
                        repaired += 1
                        continue
                    SprintWriter(self.client, data_dir=self.data_dir)._pending(
                        str(event.get("kind") or "updated"), event
                    )
                    repaired += 1
                    continue
                if event.get("kind") == "retired":
                    self._finish_pending_retired(event)
                    self._prove_retired_closed(event)
                    self.audit.append(str(event["request_id"]), event)
                    repaired += 1
                    continue
                self._finish_pending_cleanup(event, None)
                task = (
                    self._pending_create_task(event)
                    if event.get("kind") == "created"
                    else self.reader.show(str(event["ref"]))
                )
                event["task_id"] = task["id"]
                event["backend"]["revision"] = _revision(task)
                self.audit.stage(str(event["request_id"]), event)
                self.audit.append(str(event["request_id"]), event)
                repaired += 1
            except (TaskError, BoardProtocolError, OSError, KeyError, TypeError, ValueError):
                unresolved += 1
        return repaired, unresolved

    @ownership_recovery
    def _finish_pending_transition(self, event: dict[str, Any]) -> None:
        """Finish one typed pending Card transition: prove it, clean up, then commit
        it. A typed pending record is read as the transition it declares rather than
        guessed from a payload.
        """
        transition = event.get("transition") if isinstance(event.get("transition"), dict) else {}
        target = str(transition.get("target") or "")
        ref = str(event["ref"])
        card = self.reader.show(ref)
        if card["state"] == target:
            self._reset_transition_metadata(
                card,
                source=str(transition.get("source") or ""),
                target=target,
            )
            if target in {"ready", "done"} and _ready_cleanup_incomplete(self.reader.show(ref)):
                raise TaskError("backend_error", "pending Ready cleanup remains incomplete", 1)
        self.board_host.recover_transition(str(event["request_id"]))

    @ownership_recovery
    def _finish_pending_cleanup(
        self,
        event: dict[str, Any],
        retry_payload: dict[str, Any] | None,
    ) -> None:
        """Complete idempotent backend cleanup before recording a pending event."""
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return
        if event.get("kind") == "dod_item_bound":
            task = self.reader.show(str(event["ref"]))
            if task["sprint"] != payload["sprint"] or task["dod_item"] != payload["dod_item"]:
                raise TaskError("backend_error", "pending binding does not match the stored card", 1)
            return
        if event.get("kind") == "created":
            self._finish_pending_create(event, payload)
            return
        if event.get("kind") == "claimed":
            self._finish_pending_claim(event, payload)
            return
        if event.get("kind") == "restored":
            self._finish_pending_restore(event, payload)
            return
        if event.get("kind") == "archived":
            self._finish_pending_archive(event, retry_payload)
            return
        if event.get("kind") in {"cancelled", "superseded"}:
            from ummanu.dispatch.card_cancel import finish_pending_cancel

            finish_pending_cancel(self, event, retry_payload)
            return
        if event.get("kind") == "retired":
            self._finish_pending_retired(event)
            return
        if event.get("kind") == "decided":
            self._finish_pending_decided(event, payload, retry_payload)
            return
        if event.get("kind") == "restored_comment":
            from ummanu.task_restore import finish_pending_restore_comment

            finish_pending_restore_comment(self, event, payload)
            return
        if event.get("kind") != "moved" or payload.get("to") != "ready":
            return
        task = self.reader.show(str(event["ref"]))
        if task["state"] != "ready":
            raise TaskError("backend_error", "pending move no longer matches task state", 1)
        self.client.call(
            "saveTaskMetadata",
            task_id=_task_number(task),
            values=_READY_RESET_METADATA,
        )
        if _ready_cleanup_incomplete(self.reader.show(str(event["ref"]))):
            raise TaskError("backend_error", "pending Ready cleanup remains incomplete", 1)

    def _retention_card(
        self, reference: str, *, task_id: int | None
    ) -> tuple[int, dict[str, Any], dict[str, str], int] | None:
        """Read an exact retention target, including archived rows when recovering."""
        board_id, columns, _swimlanes = self.reader._board()
        done_id = next((identifier for identifier, title in columns.items() if title == "Done"), None)
        if done_id is None:
            raise TaskError("backend_error", "board schema is invalid", 1)
        from ummanu.board.sql_cards import SqlCardClient

        if isinstance(self.client, SqlCardClient):
            row = project_card_by_reference(self.client, board_id, reference)
            rows = [row] if row is not None else []
        else:
            rows = all_project_cards(self.client, board_id)
        matches = [
            row
            for row in rows
            if isinstance(row, dict)
            and _text(row.get("reference")) == reference
            and (task_id is None or _positive_int(row.get("id")) == task_id)
        ]
        if not matches:
            return None
        if task_id is None:
            active = [row for row in matches if _task_is_active(row)]
            if not active:
                return None
            matches = active
        if len(matches) != 1:
            raise TaskError("backend_error", "Done retention target is ambiguous", 1)
        raw = matches[0]
        number = _task_number(raw)
        return number, raw, _task_metadata(self.client.call("getTaskMetadata", task_id=number)), done_id

    @staticmethod
    def _check_retention_record(metadata: dict[str, str]) -> None:
        if metadata.get("record_type") in _TYPED_RECORD_TYPES:
            raise TaskError(
                "transition_forbidden", "Product issues and products cannot be retired as Done tasks", 3
            )

    def _retention_matches(
        self,
        raw: dict[str, Any],
        metadata: dict[str, str],
        expected_date_moved: int,
        cutoff: float,
        done_id: int,
    ) -> bool:
        return (
            _task_is_active(raw)
            and _positive_int(raw.get("column_id")) == done_id
            and _positive_int(raw.get("date_moved")) == expected_date_moved
            and expected_date_moved < cutoff
            and metadata.get("record_type") not in _TYPED_RECORD_TYPES
        )

    @ownership_recovery
    def _finish_pending_retired(self, event: dict[str, Any]) -> None:
        """Prove a retained close or repeat it only for its original Done episode."""
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        expected = _positive_int(payload.get("expected_date_moved"))
        task_id = _positive_int(payload.get("task_id"))
        try:
            cutoff = float(payload.get("cutoff"))
        except (TypeError, ValueError):
            cutoff = float("nan")
        ref = _text(event.get("ref"))
        if not ref or expected is None or task_id is None or cutoff != cutoff:
            raise TaskError("backend_error", "pending Done retention is incomplete", 1)
        target = self._retention_card(ref, task_id=task_id)
        if target is None:
            raise TaskError("backend_error", "pending Done retention target disappeared", 1)
        _number, raw, metadata, done_id = target
        self._check_retention_record(metadata)
        if _task_is_active(raw):
            if not self._retention_matches(raw, metadata, expected, cutoff, done_id):
                raise TaskError("backend_error", "pending Done retention no longer matches its episode", 1)
            if not self.client.call("closeTask", task_id=task_id):
                raise TaskError("backend_error", "pending Done retention remains incomplete", 1)
            target = self._retention_card(ref, task_id=task_id)
            if target is None:
                raise TaskError("backend_error", "pending Done retention target disappeared", 1)
            _number, raw, _metadata, _done_id = target
        if _task_is_active(raw):
            raise TaskError("backend_error", "pending Done retention remains incomplete", 1)

    def _prove_retired_closed(self, event: dict[str, Any]) -> None:
        """Last audit gate: a success event never names a live replacement episode."""
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        task_id = _positive_int(payload.get("task_id"))
        ref = _text(event.get("ref"))
        if task_id is None or not ref:
            raise TaskError("backend_error", "pending Done retention is incomplete", 1)
        target = self._retention_card(ref, task_id=task_id)
        if target is None or _task_is_active(target[1]):
            raise TaskError("backend_error", "pending Done retention is no longer closed", 1)

    def _finish_pending_claim(self, event: dict[str, Any], payload: dict[str, Any]) -> None:
        """Complete a claim whose metadata committed before the column move failed."""
        ref = str(event["ref"])
        worker = str(payload.get("worker") or "")
        if not worker:
            raise TaskError("backend_error", "pending claim is missing its worker id", 1)
        task = self.reader.show(ref)
        # A pending claim on a typed record can only come from before the claim guard existed (or
        # from a card typed after the claim). Finishing it would move a Product or an Issue into
        # In progress, so cleanup fails closed here as well and leaves the event for a PO.
        _check_execution_record(task)
        if task["claim"]["worker"] != worker:
            raise TaskError("backend_error", "pending claim no longer matches task claim", 1)
        if not _matches_optional(payload.get("resolved_head"), task["routing"]["resolved_worker_head"]):
            raise TaskError("backend_error", "pending claim worker head remains incomplete", 1)
        if not _matches_optional(
            payload.get("resolved_review_head"), task["routing"]["resolved_review_head"]
        ):
            raise TaskError("backend_error", "pending claim review head remains incomplete", 1)
        if task["state"] == "ready":
            self._move_raw(task, "in_progress", swimlane_id=self._current_swimlane_id(task))
        elif task["state"] != "in_progress":
            raise TaskError("backend_error", "pending claim no longer matches task state", 1)
        normalized = self.reader.show(ref)
        if normalized["state"] != "in_progress" or normalized["claim"]["worker"] != worker:
            raise TaskError("backend_error", "pending claim cleanup remains incomplete", 1)

    def _finish_pending_decided(
        self,
        event: dict[str, Any],
        payload: dict[str, Any],
        retry_payload: dict[str, Any] | None,
    ) -> None:
        """Commit a decision only after its canonical marker and exact body exist on the card."""
        ref = str(event.get("ref") or "")
        marker = str(payload.get("marker") or "")
        expected = str(payload.get("body_sha256") or "")

        def matches(comment: dict[str, Any]) -> bool:
            if comment.get("marker") != marker:
                return False
            rendered = str(comment.get("body") or "")
            prefix = f"[{marker}]\n"
            body = rendered.removeprefix(prefix)
            return _digest(body) == expected

        task = self.reader.show(ref)
        matching = [comment for comment in task.get("comments", []) if matches(comment)]
        if matching:
            return
        body = str((retry_payload or {}).get("decision_body") or "")
        if not body or _digest(body) != expected:
            raise TaskError(
                "audit_pending",
                "pending decision has no verified board comment; retry its original request and body",
                4,
            )
        if task.get("state") != "assessment":
            raise TaskError("backend_error", "pending decision no longer matches Assessment", 1)
        self.client.call(
            "createComment",
            task_id=_task_number(task),
            user_id=0,
            content=f"[{marker}]\n{body}",
        )
        verified = self.reader.show(ref)
        if not any(matches(comment) for comment in verified.get("comments", [])):
            raise TaskError("backend_error", "pending decision comment could not be verified", 1)

    def _finish_pending_archive(
        self,
        event: dict[str, Any],
        retry_payload: dict[str, Any] | None,
    ) -> None:
        """Complete an archive whose comment or close committed before the reply was lost."""
        ref = str(event.get("ref") or "")
        if not ref:
            raise TaskError("backend_error", "pending archive is missing its task ref", 1)
        event_payload = event.get("payload")
        expected_digest = _text(event_payload.get("reason_sha256")) if isinstance(event_payload, dict) else ""
        retry_reason = _text((retry_payload or {}).get("reason"))
        safe_retry_reason = self._redact_for_board(retry_reason)
        if retry_reason and safe_retry_reason != retry_reason:
            # The old pending record predates the board-boundary redactor.  Its
            # digest names the raw text, so silently replacing it would make
            # reconciliation believe a different archive reason was committed.
            # Leave it pending for an operator rather than publish a secret or
            # falsify the append-only history.
            raise TaskError(
                "audit_pending",
                "pending archive reason contains credential material; reissue the archive safely",
                4,
            )
        if retry_reason and _digest(retry_reason) != expected_digest:
            raise TaskError("validation", "archive retry reason does not match the pending request", 2)
        board_id, _, _ = self.reader._board()
        raw = project_card_by_reference(self.client, board_id, ref)
        if not isinstance(raw, dict):
            raise TaskError("not_found", "task was not found", 2)
        if _task_is_active(raw):
            task = self.reader.show(ref)
            self._check_archivable(task)
            self._check_dispatcher_archivable(ref)
            self._request_workspace_cleanup(task, "archive")
            if not _has_archive_reason(task, expected_digest):
                if not retry_reason:
                    raise TaskError("backend_error", "pending archive reason comment is missing", 1)
                self.client.call(
                    "createComment",
                    task_id=_task_number(task),
                    user_id=0,
                    content=f"[archive]\n{retry_reason}",
                )
                task = self.reader.show(ref)
                if not _has_archive_reason(task, expected_digest):
                    raise TaskError("backend_error", "pending archive reason comment remains incomplete", 1)
            if not self.client.call("closeTask", task_id=_task_number(task)):
                raise TaskError("backend_error", "pending archive remains incomplete", 1)
        elif expected_digest:
            task_id = _positive_int(raw.get("id"))
            if task_id is None:
                raise TaskError("backend_error", "board store returned an invalid task", 1)
            raw_comments = self.client.call("getAllComments", task_id=task_id) or []
            comments = [_normalize_comment(comment) for comment in raw_comments if isinstance(comment, dict)]
            if not _has_archive_reason({"comments": comments}, expected_digest):
                raise TaskError("backend_error", "pending archive reason comment is missing", 1)
        raw = project_card_by_reference(self.client, board_id, ref)
        if isinstance(raw, dict) and _task_is_active(raw):
            raise TaskError("backend_error", "pending archive remains incomplete", 1)
        self._request_workspace_cleanup(self.reader.show(ref), "archive")

    def _finish_pending_restore(self, event: dict[str, Any], payload: dict[str, Any]) -> None:
        from ummanu.task_restore import finish_pending_restore

        finish_pending_restore(self, event, payload)

    def _finish_pending_create(self, event: dict[str, Any], payload: dict[str, Any]) -> None:
        ref = str(event.get("ref") or "")
        if not ref:
            raise TaskError("backend_error", "pending create is missing its task ref", 1)
        task = self._pending_create_task(event)
        if task["ref"] != ref:
            backend = event.get("backend")
            if isinstance(backend, dict) and backend.get("reference_assignment") == "atomic":
                raise TaskError("backend_error", "pending atomic create reference remains incomplete", 1)
            # Repair a pre-atomic create only if no other row acquired its reference.
            if task["ref"]:
                raise TaskError("backend_error", "pending create task reference does not match", 1)
            board_id, _, _ = self.reader._board()
            current = project_card_by_reference(self.client, board_id, ref)
            if current is not None:
                raise TaskError("backend_error", "pending create reference belongs to another task", 1)
            if not self.client.call("updateTask", id=_task_number(task), reference=ref):
                raise TaskError("backend_error", "pending create reference remains incomplete", 1)
            task = self._pending_create_task(event)
            if task["ref"] != ref:
                raise TaskError("backend_error", "pending create reference remains incomplete", 1)
        self.client.call(
            "saveTaskMetadata",
            task_id=_task_number(task),
            values=_create_metadata_values(payload),
        )
        normalized = self._pending_create_task(event)
        expected_mode = _text(payload.get("codex_launch_mode"))
        if expected_mode not in _CODEX_LAUNCH_MODES:
            expected_mode = ""
        if expected_mode and normalized["routing"]["codex_launch_mode"] != expected_mode:
            raise TaskError("backend_error", "pending create metadata remains incomplete", 1)
        if payload.get("steward_report") is True and not (
            normalized["state"] == "in_progress"
            and normalized.get("record_type") == "task"
            and normalized["type"] == "research"
            and normalized["claim"]["worker"] == _text(payload.get("slug"))
            and _is_steward_report(normalized)
        ):
            raise TaskError("backend_error", "pending steward report metadata remains incomplete", 1)

    def _pending_create_task(self, event: dict[str, Any]) -> dict[str, Any]:
        backend = event.get("backend")
        task_id = _positive_int(backend.get("task_id")) if isinstance(backend, dict) else None
        if task_id is not None:
            return self.reader.show_id(task_id)
        raise TaskError(
            "backend_error",
            "pending create is missing its backend task id; reconcile it manually",
            1,
        )

    @staticmethod
    def _role(role: Role | str, allowed: Collection[Role], *, actor: str = "") -> Role:
        """`admit_role`, the one role check; every write entry point passes the actor it writes as."""
        return admit_role(role, actor, allowed)

    @staticmethod
    def _check_archivable(task: dict[str, Any]) -> None:
        state = task["state"]
        if state in ACTIVE_STATES:
            raise TaskError(
                "live_work",
                "archive refuses a card with live worker or reviewer work, or one parked in "
                f"Assessment holding a retained worker; {_CANCEL_HINT}",
                3,
            )
        if task["claim"]["worker"] is not None:
            raise TaskError("live_work", f"archive refuses a card with an active claim; {_CANCEL_HINT}", 3)

    def _check_dispatcher_archivable(self, reference: str) -> None:
        state_path = self.data_dir / "dispatcher" / "production-state.json"
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError, UnicodeError):
            raise TaskError("live_work", "archive cannot prove dispatcher state is clear", 3) from None
        if not isinstance(payload, dict):
            raise TaskError("live_work", "archive cannot prove dispatcher state is clear", 3)
        records = payload.get("records") or {}
        if not isinstance(records, dict):
            raise TaskError("live_work", "archive cannot prove dispatcher state is clear", 3)
        record = records.get(reference)
        if not isinstance(record, dict):
            return
        if _dispatcher_record_has_live_work(record):
            raise TaskError("live_work", f"archive refuses a card with live dispatcher work; {_CANCEL_HINT}", 3)

    def _request_workspace_cleanup(self, task: dict[str, Any], disposition: str) -> None:
        from ummanu.dispatch.cleanup_journal import CleanupJournal
        from ummanu.dispatch.types import HostError
        try:
            CleanupJournal(self.data_dir).request(task, disposition)
        except HostError as exc:
            raise TaskError("live_work", str(exc), 3) from exc

    def settle_cleanup_claim(self, expected: dict[str, Any], worker: str) -> None:
        """Called only by the cleanup owner after verified head and Git settlement."""
        with reference_lock(self.data_dir, expected["ref"]), self._mutation():
            task = self.reader.show(expected["ref"])
            if (task["id"] != expected["id"] or task.get("claim") != expected.get("claim")
                    or task.get("claim", {}).get("worker") != worker
                    or (not task.get("closed") and task["state"] != "done")):
                raise TaskError("live_work", "cleanup claim changed or remains admitted", 3)
            self.client.call("saveTaskMetadata", task_id=_task_number(task), values={"claim": ""})
            if self.reader.show(expected["ref"]).get("claim", {}).get("worker"):
                raise TaskError("backend_error", "cleanup claim settlement remains pending", 1)
