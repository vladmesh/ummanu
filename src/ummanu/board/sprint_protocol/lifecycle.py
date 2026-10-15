"""Cohesive staged close/reopen capability of the public SprintWriter."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ummanu._fsutil import held_lock
from ummanu.board import owner_events
from ummanu.board.card_transitions import CardTransitionForbidden, card_transition
from ummanu.board.roles import Role
from ummanu.board.sprint_close import (
    SprintCloseConflict,
    SprintCloseDecisions,
    SprintCloseIntent,
    SprintCloseoutPlan,
    SprintCloseSnapshot,
    SprintCloseTargets,
)
from ummanu.board.sprint_protocol.reservations import sprint_admission_lock, update_active_sprint_projects
from ummanu.board.sprint_protocol.shared import (
    SPRINT_CLOSED,
    SPRINT_CLOSEOUT,
    SPRINT_REOPENED,
    _close_archive_request_id,
    _close_step_request_id,
    _sql_atomic,
)
from ummanu.board.sprint_write import SprintMutationReceipt, SprintReopenIntent, SprintWriteSnapshot
from ummanu.board.task_protocol.rows import _digest, _now
from ummanu.board.tick_snapshot import select_cards
from ummanu.dispatch.cleanup_journal import CleanupJournal
from ummanu.kernel.errors import TaskError
from ummanu.sprint_observer import encode_observer
from ummanu.tasks import TaskReader, TaskWriter


class SprintLifecycleMixin:
    """Close/reopen steps use self so instance and public-class patches remain effective."""

    def close(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        decisions: SprintCloseDecisions | Mapping[str, Any] | None = None,
        request_id: str | None = None,
        reason: str = "",
        closeout: str = "",
    ) -> dict[str, Any]:
        """Close a sprint on explicit typed decisions while preserving the durable JSON
        contract. The PO closes any sprint; the observer closes the one it was launched
        for, and is refused any other by the identity guard, outside the transaction so
        the refusal stays audited.
        """
        self._role(role, {"po", "observer"}, actor=actor)
        request_id = request_id or str(uuid.uuid4())
        self._guard_observer_identity(role=role, actor=actor, reference=reference, request_id=request_id)
        # Admission before the SQL transaction and its advisory lock, the order create takes them in.
        with sprint_admission_lock(self.data_dir):
            closed = self._close_atomic(
                role=role,
                actor=actor,
                reference=reference,
                decisions=decisions,
                request_id=request_id,
                reason=reason,
                closeout=closeout,
            )
        # After the close committed, once per close record: a repeat of this request id answers the
        # same record and writes nothing new, and a reopened sprint's next close is a new record.
        owner_events.record(
            owner_events.SPRINT_CLOSED,
            reference,
            f"{reference} was closed by the {role}" + (f": {reason.strip()}" if str(reason or "").strip() else ""),
            f"{owner_events.SPRINT_CLOSED}:{reference}:{closed.get('event_id') or request_id}",
            to=self.client,
        )
        return closed

    @_sql_atomic
    def _close_atomic(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        decisions: SprintCloseDecisions | Mapping[str, Any] | None,
        request_id: str,
        reason: str,
        closeout: str,
    ) -> dict[str, Any]:
        try:
            offered = SprintCloseDecisions.from_document(decisions) if decisions is not None else None
        except ValueError as exc:
            raise TaskError("validation", str(exc), 2) from None
        intent = SprintCloseIntent(Role(role), actor, reference)
        intent_document = intent.to_document()
        with self.transactions.reference_lock(reference) as lock, held_lock(lock):
            document, committed = self.transactions.existing(
                request_id,
                kind=SPRINT_CLOSED,
                intent=intent_document,
            )
            if committed is not None:
                self._check_completed_close(committed, offered, reason=reason, closeout=closeout)
                return self._close_result(committed)
            if document is not None:
                self._check_staged_decisions(document, offered)
                self._check_staged_closeout(document, reason=reason, closeout=closeout)
            if document is None:
                from ummanu.sprint_close import plan_close_decisions

                sprint_document = self.reader.show(reference, include_cards=False)
                sprint = SprintCloseSnapshot.from_document(sprint_document)
                targets = self._close_targets(sprint)
                plan = plan_close_decisions(
                    offered,
                    declared_issues=sprint.issues,
                    remaining=targets.remaining,
                    states=targets.remaining_state_map,
                    issue_states=self._declared_issue_states(list(sprint.issues)),
                )
                self._check_close_decisions_are_writable(
                    plan, role=role, states=targets.remaining_state_map
                )
                closeout_plan = self._plan_closeout(
                    sprint, plan, actor=actor, reason=reason, closeout=closeout
                )
                event = self._event(
                    SPRINT_CLOSED,
                    role,
                    actor,
                    reference,
                    request_id,
                    {
                        "intent": intent_document,
                        "reason": str(reason or ""),
                        "closeout": closeout_plan.to_document() if closeout_plan else None,
                        "targets": targets.to_document(),
                        "archived_tasks": [],
                        "remaining_tasks": list(targets.remaining),
                        "decisions": plan.to_document(),
                        "closed_issues": [],
                        "moved_tasks": [],
                        "disposed_tasks": [],
                        "conflicts": [],
                    },
                    sprint_document,
                )
                document, committed = self.transactions.begin(
                    request_id,
                    kind=SPRINT_CLOSED,
                    intent=intent_document,
                    event=event,
                )
                if committed is not None:
                    return self._close_result(committed)
                if document is None:
                    raise TaskError("audit_pending", "sprint close transaction claim is unavailable", 4)
            close_payload = document["event"].get("payload", {})
            close_plan = SprintCloseDecisions.from_document(close_payload.get("decisions"))
            closeout_plan = SprintCloseoutPlan.from_document(close_payload.get("closeout"))
            self.client.sprints.save_close(
                reference,
                request_id,
                close_plan.to_document(),
                reason=str(close_payload.get("reason") or ""),
                closeout_document=closeout_plan.document if closeout_plan else None,
            )
            return self._run_close(document)

    def _issue_store(self) -> Any:
        from ummanu.product_issues import ProductIssueStore

        return ProductIssueStore(self.client, data_dir=self.data_dir, instance=self.instance)

    def _declared_issue_states(self, declared: list[str]) -> dict[str, dict[str, Any]]:
        """What every declared issue actually is, for the decisions to be matched
        against. An installation without its instance directory cannot read
        Product/Issue records at all; a close that needs to write one is refused for
        that separately.
        """
        if self.instance is None:
            return {}
        store = self._issue_store()
        states: dict[str, dict[str, Any]] = {}
        for reference in declared:
            try:
                issue = store.show_issue(reference)
            except TaskError:
                continue
            states[reference] = {
                "closed": bool(issue.get("closed")),
                "close_reason": str(issue.get("close_reason") or ""),
            }
        return states

    def _close_step_status(self, request_id: str) -> str:
        """Whether one step of a close is done, half-written, or still to do. The only
        proof that a step happened is its own derived request id carrying a committed
        event.
        """
        if self.audit.committed_event(request_id) is not None:
            return "done"
        if self.audit.pending_event(request_id) is not None:
            return "pending"
        return "todo"

    def _require_close_step_settled(self, request_id: str) -> None:
        """A step is only over once its own event is committed, whatever the backend shows."""
        if self._close_step_status(request_id) != "done":
            raise TaskError(
                "audit_pending",
                "sprint close step is pending repair; retry with the same request id",
                4,
            )

    def _close_conflict(
        self,
        document: dict[str, Any],
        payload: dict[str, Any],
        *,
        section: str,
        reference: str,
        verdict: str,
        actual: str,
        message: str,
    ) -> None:
        """Stop on somebody else's change, recording a typed recoverable conflict."""
        try:
            conflict = SprintCloseConflict.from_document(
                {"section": section, "ref": reference, "verdict": verdict, "actual": actual}
            )
        except ValueError as exc:
            raise TaskError("audit_pending", str(exc), 4) from None
        conflicts = payload.setdefault("conflicts", [])
        if isinstance(conflicts, list):
            existing = []
            for item in conflicts:
                if isinstance(item, Mapping):
                    try:
                        existing.append(SprintCloseConflict.from_document(item))
                    except ValueError:
                        raise TaskError(
                            "audit_pending", "sprint close transaction has invalid conflicts", 4
                        ) from None
            if not any(item.ref == reference for item in existing):
                conflicts.append(conflict.to_document())
                self.transactions.save(document)
        raise TaskError("close_conflict", message, 3)

    def _close_targets(self, sprint: SprintCloseSnapshot) -> SprintCloseTargets:
        """Freeze this close's task set before any archival write."""
        if not sprint.has_reservations:
            return SprintCloseTargets()
        cards = select_cards(TaskReader(self.client), sprint=sprint.ref)
        return SprintCloseTargets.from_cards(cards)

    def _check_staged_decisions(
        self,
        document: dict[str, Any],
        decisions: SprintCloseDecisions | None,
    ) -> None:
        """A retry may repeat the staged plan, or amend exactly a recorded conflict."""
        if decisions is None:
            return
        payload = (document.get("event") or {}).get("payload") or {}
        try:
            staged = SprintCloseDecisions.from_document(payload.get("decisions"))
        except ValueError:
            return
        offered = SprintCloseDecisions(
            issues=tuple(sorted(decisions.issues, key=lambda entry: entry.ref)),
            cards=tuple(sorted(decisions.cards, key=lambda entry: entry.ref)),
        )
        current = SprintCloseDecisions(
            issues=tuple(sorted(staged.issues, key=lambda entry: entry.ref)),
            cards=tuple(sorted(staged.cards, key=lambda entry: entry.ref)),
        )
        if offered == current:
            return
        from ummanu.sprint_close import ALREADY_CLOSED, ALREADY_MOVED

        confirmation = {"issues": ALREADY_CLOSED, "cards": ALREADY_MOVED}
        conflicts: dict[str, SprintCloseConflict] = {}
        for item in payload.get("conflicts") or []:
            if not isinstance(item, Mapping):
                continue
            try:
                conflict = SprintCloseConflict.from_document(item)
            except ValueError:
                continue
            conflicts[conflict.ref] = conflict
        amended: list[SprintCloseConflict] = []
        for section, current_entries, offered_entries in (
            ("issues", current.issues, offered.issues),
            ("cards", current.cards, offered.cards),
        ):
            if len(offered_entries) != len(current_entries):
                self._refuse_restated_decisions()
            for was, now in zip(current_entries, offered_entries):
                if was == now:
                    continue
                conflict = conflicts.get(now.ref)
                if (
                    conflict is None
                    or was.ref != now.ref
                    or conflict.section != section
                    or now.verdict != confirmation[section]
                    or now.actual != conflict.actual
                ):
                    self._refuse_restated_decisions()
                amended.append(conflict)
        if not amended:
            self._refuse_restated_decisions()
        payload["decisions"] = offered.to_document()
        payload["conflicts"] = [
            item
            for item in (payload.get("conflicts") or [])
            if not (
                isinstance(item, Mapping)
                and any(
                    item.get("ref") == conflict.ref and item.get("section") == conflict.section
                    for conflict in amended
                )
            )
        ]
        self.transactions.save(document)

    def _check_completed_close(
        self,
        committed: dict[str, Any],
        decisions: SprintCloseDecisions | None,
        *,
        reason: str,
        closeout: str,
    ) -> None:
        """A repeat of a finished close carries the same canonical typed plan, or is refused."""
        payload = committed.get("payload") if isinstance(committed.get("payload"), dict) else {}
        try:
            staged = SprintCloseDecisions.from_document(payload.get("decisions"))
        except ValueError as exc:
            raise TaskError("audit_pending", "committed sprint close has invalid decisions", 4) from exc
        if decisions is not None:
            offered = SprintCloseDecisions(
                issues=tuple(sorted(decisions.issues, key=lambda entry: entry.ref)),
                cards=tuple(sorted(decisions.cards, key=lambda entry: entry.ref)),
            )
            current = SprintCloseDecisions(
                issues=tuple(sorted(staged.issues, key=lambda entry: entry.ref)),
                cards=tuple(sorted(staged.cards, key=lambda entry: entry.ref)),
            )
            if offered != current:
                self._refuse_restated_decisions()
        self._check_staged_closeout({"event": committed}, reason=reason, closeout=closeout)

    def _refuse_restated_decisions(self) -> None:
        raise TaskError(
            "validation",
            "this request id was staged with other decisions; retry it with the same file",
            2,
        )

    def _plan_closeout(
        self,
        sprint: SprintCloseSnapshot,
        plan: SprintCloseDecisions,
        *,
        actor: str,
        reason: str,
        closeout: str,
    ) -> SprintCloseoutPlan | None:
        """Freeze the closeout this close will write, before the transaction opens."""
        if not str(closeout or "").strip():
            return None
        from ummanu.sprint_close import closeout_document, closeout_path

        document = closeout_path(sprint.ref, day=_now()[:10])
        text = closeout_document(
            reference=sprint.ref,
            goal=sprint.goal,
            actor=actor,
            reason=str(reason or ""),
            body=str(closeout),
            decisions=plan,
        )
        self._check_closeout_is_writable(document, text, actor=actor)
        return SprintCloseoutPlan(
            document=document,
            text=text,
            body_sha256=_digest(str(closeout)),
        )

    def _check_closeout_is_writable(self, document: str, text: str, *, actor: str) -> None:
        """Refuse a closeout this installation cannot write, before anything is
        written. Asked here it names what is missing and leaves the sprint open; asked
        in the terminal phase, the same refusal would be a transaction to repair.
        """
        from ummanu.knowledge_write import KnowledgeError, check_knowledge_document
        from ummanu.state_repo import StateRepoError

        if self.instance is None:
            raise TaskError(
                "validation",
                "writing the sprint's closeout needs the instance directory; pass --instance",
                2,
            )
        try:
            check_knowledge_document(self._instance_dir(), document=document, actor=actor, text=text)
        except (KnowledgeError, StateRepoError) as exc:
            raise TaskError("validation", f"sprint close cannot write its closeout: {exc}", 2) from None

    def _instance_dir(self) -> Path:
        """The installation directory the closeout is written into, however the caller named it."""
        instance = Path(str(self.instance))
        return instance.parent if instance.is_file() else instance

    def _check_staged_closeout(self, document: dict[str, Any], *, reason: str, closeout: str) -> None:
        """A retry of a staged close carries the same closeout body and reason."""
        payload = (document.get("event") or {}).get("payload") or {}
        if "closeout" not in payload and "reason" not in payload:
            return
        staged = SprintCloseoutPlan.from_document(payload.get("closeout"))
        body = str(closeout or "").strip()
        if str(reason or "") and str(reason) != str(payload.get("reason") or ""):
            self._refuse_restated_closeout()
        if not body:
            return
        if staged is None or staged.body_sha256 != _digest(str(closeout)):
            self._refuse_restated_closeout()

    def _refuse_restated_closeout(self) -> None:
        raise TaskError(
            "validation",
            "this request id was staged with another closeout or reason; retry it with the same one",
            2,
        )

    def _check_close_decisions_are_writable(
        self, plan: SprintCloseDecisions, *, role: str, states: Mapping[str, str]
    ) -> None:
        """Preflight each planned close disposition against `card_transition` for the
        closing role before any writes. Reject the whole plan if any edge is forbidden;
        PO uses sprint override, observer cannot move cards out of Assessment.
        """
        from ummanu.sprint_close import ALREADY_CLOSED, ALREADY_MOVED, DISPOSITION_TARGETS, KEEP_OPEN

        closing = [entry for entry in plan.issues if entry.verdict not in {KEEP_OPEN, ALREADY_CLOSED}]
        if closing and self.instance is None:
            raise TaskError(
                "validation",
                "closing an issue with the sprint needs the instance directory; pass --instance",
                2,
            )
        if not plan.cards:
            return
        forbidden = []
        for entry in plan.cards:
            target = "" if entry.verdict == ALREADY_MOVED else DISPOSITION_TARGETS[entry.verdict]
            source = str(states.get(entry.ref) or "")
            if not target or source == target:
                continue
            try:
                card_transition(role, source, target)
            except (CardTransitionForbidden, ValueError):
                forbidden.append(f"{entry.ref} (in {source or 'an unknown column'}, {entry.verdict})")
        if forbidden:
            raise TaskError(
                "close_plan_forbidden",
                f"a {role} close cannot move " + ", ".join(forbidden)
                + "; decide it with `task decide` (or move it) before closing",
                3,
            )
        writer = TaskWriter(self.client, data_dir=self.data_dir)
        live = []
        for entry in plan.cards:
            try:
                writer._check_dispatcher_archivable(entry.ref)
            except TaskError as exc:
                if exc.code != "live_work":
                    raise
                live.append(entry.ref)
        if live:
            raise TaskError(
                "live_work",
                "sprint close cannot dispose of card(s) whose dispatcher work is still live; "
                "settle them first: " + ", ".join(live),
                3,
            )

    def _run_close(self, document: dict[str, Any]) -> dict[str, Any]:
        event = document.get("event")
        if not isinstance(event, dict):
            raise TaskError("audit_pending", "sprint close transaction has no audit event", 4)
        payload = event.get("payload")
        if not isinstance(payload, dict):
            raise TaskError("audit_pending", "sprint close transaction has no payload", 4)
        try:
            targets = SprintCloseTargets.from_document(payload.get("targets"))
            decisions = SprintCloseDecisions.from_document(payload.get("decisions"))
        except ValueError as exc:
            raise TaskError("audit_pending", f"sprint close transaction is invalid: {exc}", 4) from None
        archive = list(targets.archive)
        archived = payload.setdefault("archived_tasks", [])
        if not isinstance(archived, list) or not all(isinstance(ref, str) for ref in archived):
            raise TaskError("audit_pending", "sprint close transaction has invalid archival progress", 4)
        try:
            self._close_declared_issues(document, event, payload, decisions)
            writer = TaskWriter(self.client, data_dir=self.data_dir) if archive else None
            for task_ref in archive:
                step_request_id = _close_archive_request_id(str(document["request_id"]), task_ref)
                if self._close_step_status(step_request_id) != "done":
                    assert writer is not None
                    writer.archive(
                        role=str(document["intent"]["role"]),
                        actor=str(document["intent"]["actor"]),
                        reference=task_ref,
                        reason=f"archived when sprint {event['ref']} closed",
                        request_id=step_request_id,
                        sprint_close=str(event["ref"]),
                    )
                    self._require_close_step_settled(step_request_id)
                if task_ref not in archived:
                    archived.append(task_ref)
                    self.transactions.save(document)
            self._dispose_remaining_cards(document, event, payload, decisions, targets)
            payload["observer_cleanup"] = CleanupJournal(self.data_dir).observer_handoff(
                self.reader.show(str(event["ref"]), include_cards=False))
            self.transactions.save(document)
            self._write_closeout(document, event, payload)
            sprint = self.reader.show(str(event["ref"]), include_cards=False)
            typed_request_id = str(document["request_id"]) + ":typed-close"
            typed_pending = self.audit.pending_event(typed_request_id)
            if sprint["status"] != "closed" or (
                typed_pending is not None and typed_pending.get("record_type") == "board.protocol_event"
            ):
                document.setdefault("progress", {})["status_started"] = True
                self.transactions.save(document)
                self._transition_host(
                    role=str(document["intent"]["role"]),
                    actor=str(document["intent"]["actor"]),
                    reference=str(event["ref"]),
                    target="closed",
                    reason="Sprint closed",
                    request_id=typed_request_id,
                )
            document.setdefault("progress", {})["status_done"] = True
            self.transactions.save(document)
            self.transactions.complete(document)
        except TaskError:
            raise
        except (OSError, KeyError, TypeError, ValueError):
            raise TaskError(
                "audit_pending", "sprint close is pending repair; retry with the same request id", 4
            ) from None
        update_active_sprint_projects(self.data_dir, self.reader.show(str(event["ref"]), include_cards=False))
        return self._close_result(event)

    def _close_declared_issues(
        self,
        document: dict[str, Any],
        event: dict[str, Any],
        payload: dict[str, Any],
        decisions: SprintCloseDecisions,
    ) -> None:
        """Perform the typed closing verdicts, one issue at a time."""
        from ummanu.sprint_close import ALREADY_CLOSED, KEEP_OPEN

        closed = payload.setdefault("closed_issues", [])
        if not isinstance(closed, list):
            raise TaskError("audit_pending", "sprint close transaction has invalid issue progress", 4)
        pending = [
            entry
            for entry in decisions.issues
            if entry.verdict not in {KEEP_OPEN, ALREADY_CLOSED} and entry.ref not in closed
        ]
        if not pending:
            return
        if self.instance is None:
            raise TaskError(
                "validation",
                "closing an issue with the sprint needs the instance directory; pass --instance",
                2,
            )
        store = self._issue_store()
        document.setdefault("progress", {})["issues_started"] = True
        self.transactions.save(document)
        for entry in pending:
            reference = entry.ref
            step_request_id = _close_step_request_id(str(document["request_id"]), "issue", reference)
            status = self._close_step_status(step_request_id)
            if status != "done":
                current = store.show_issue(reference)
                if status == "todo" and current.get("closed"):
                    carried = str(current.get("close_reason") or "unknown")
                    self._close_conflict(
                        document,
                        payload,
                        section="issues",
                        reference=reference,
                        verdict=entry.verdict,
                        actual=carried,
                        message=(
                            f"issue {reference} was closed as {carried} by somebody else, and this "
                            f"close states {entry.verdict}; retry with that decision amended to "
                            f"already_closed naming {carried}"
                        ),
                    )
                store.close_issue(
                    reference=reference,
                    reason=entry.verdict,
                    actor=str(document["intent"]["actor"]),
                    request_id=step_request_id,
                    role=str(document["intent"]["role"]),
                    sprint_close=str(event["ref"]),
                )
                self._require_close_step_settled(step_request_id)
            closed.append(reference)
            self.transactions.save(document)

    def _dispose_remaining_cards(
        self,
        document: dict[str, Any],
        event: dict[str, Any],
        payload: dict[str, Any],
        decisions: SprintCloseDecisions,
        targets: SprintCloseTargets,
    ) -> None:
        """Take every remaining card into the recorded end its typed disposition names."""
        from ummanu.sprint_close import ALREADY_MOVED, DISPOSITION_TARGETS

        if not decisions.cards:
            return
        moved = payload.setdefault("moved_tasks", [])
        disposed = payload.setdefault("disposed_tasks", [])
        if not isinstance(moved, list) or not isinstance(disposed, list):
            raise TaskError("audit_pending", "sprint close transaction has invalid disposition progress", 4)
        planned = targets.remaining_state_map
        writer = TaskWriter(self.client, data_dir=self.data_dir)
        reader = TaskReader(self.client)
        # Every step of a close is written in the closing caller's own name. A PO close disposes a
        # card through the sprint override; an observer's close needs none, the card is its sprint's.
        role = str(document["intent"]["role"])
        actor = str(document["intent"]["actor"])
        for entry in decisions.cards:
            reference = entry.ref
            verdict = entry.verdict
            reason = entry.reason
            target = "" if verdict == ALREADY_MOVED else DISPOSITION_TARGETS[verdict]
            if target and str(planned.get(reference) or "") != target:
                move_request_id = _close_step_request_id(
                    str(document["request_id"]), "dispose-move", reference
                )
                status = self._close_step_status(move_request_id)
                if status != "done":
                    if status == "todo" and reader.show(reference)["state"] == target:
                        self._close_conflict(
                            document,
                            payload,
                            section="cards",
                            reference=reference,
                            verdict=verdict,
                            actual=target,
                            message=(
                                f"card {reference} was moved to {target} by somebody else, not by "
                                f"this close; retry with that disposition amended to already_moved "
                                f"naming {target}"
                            ),
                        )
                    writer.move(
                        role=role,
                        actor=actor,
                        reference=reference,
                        target=target,
                        reason=f"{verdict} when sprint {event['ref']} closed: {reason}",
                        sprint_override=role == "po",
                        sprint_override_reason=(
                            f"disposed by the close of {event['ref']}: {reason}" if role == "po" else ""
                        ),
                        request_id=move_request_id,
                    )
                    self._require_close_step_settled(move_request_id)
            if reference not in moved:
                moved.append(reference)
                self.transactions.save(document)
            archive_request_id = _close_step_request_id(
                str(document["request_id"]), "dispose-archive", reference
            )
            if self._close_step_status(archive_request_id) != "done":
                writer.archive(
                    role=role,
                    actor=actor,
                    reference=reference,
                    reason=f"archived when sprint {event['ref']} closed: {reason}",
                    request_id=archive_request_id,
                    sprint_close=str(event["ref"]),
                )
                self._require_close_step_settled(archive_request_id)
            if reference not in disposed:
                disposed.append(reference)
                self.transactions.save(document)

    def _write_closeout(
        self, document: dict[str, Any], event: dict[str, Any], payload: dict[str, Any]
    ) -> None:
        """Write this close's typed knowledge-closeout plan exactly once."""
        plan = SprintCloseoutPlan.from_document(payload.get("closeout"))
        if plan is None:
            return
        reference = str(event["ref"])
        step_request_id = _close_step_request_id(str(document["request_id"]), "closeout", reference)
        if self._close_step_status(step_request_id) == "done":
            return
        from ummanu.knowledge_write import KnowledgeError, write_knowledge_document

        actor = str(document["intent"]["actor"])
        self._check_closeout_is_writable(plan.document, plan.text, actor=actor)
        document.setdefault("progress", {})["closeout_started"] = True
        self.transactions.save(document)
        step = self._event(
            SPRINT_CLOSEOUT,
            str(document["intent"]["role"]),
            actor,
            reference,
            step_request_id,
            {"close_request_id": str(document["request_id"]), "document": plan.document, "commit": ""},
            self.reader.show(reference, include_cards=False),
        )
        self.audit.stage(step_request_id, step)
        try:
            written = write_knowledge_document(
                self._instance_dir(), document=plan.document, actor=actor, text=plan.text
            )
        except KnowledgeError as exc:
            raise TaskError("backend_error", f"sprint close could not write its closeout: {exc}", 1) from None
        # The knowledge writer's content revision of the closeout, where a Git commit id used to be.
        step["payload"]["commit"] = written.commit
        step["payload"]["changed"] = bool(written.changed)
        self.audit.stage(step_request_id, step)
        self.audit.append(step_request_id, step)
        self._require_close_step_settled(step_request_id)
        payload["closeout"] = plan.mark_written(written.commit).to_document()
        self.transactions.save(document)

    def _close_result(self, event: dict[str, Any]) -> dict[str, Any]:
        from ummanu.sprint_close import CLOSE_NOT_DONE

        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        try:
            decisions = SprintCloseDecisions.from_document(payload.get("decisions"))
        except ValueError as exc:
            raise TaskError("audit_pending", "sprint close has invalid decisions", 4) from exc
        closeout_plan = SprintCloseoutPlan.from_document(payload.get("closeout"))
        return {
            "action": SPRINT_CLOSED,
            "sprint": self.reader.show(str(event["ref"])),
            "event_id": str(event["event_id"]),
            "archived_tasks": list(payload.get("archived_tasks") or []),
            "remaining_tasks": list(payload.get("remaining_tasks") or []),
            "issue_decisions": [entry.to_document() for entry in decisions.issues],
            "closed_issues": list(payload.get("closed_issues") or []),
            "card_dispositions": [entry.to_document() for entry in decisions.cards],
            "disposed_tasks": list(payload.get("disposed_tasks") or []),
            "reason": str(payload.get("reason") or ""),
            "closeout": closeout_plan.to_result() if closeout_plan else None,
            "definition_of_done": {"satisfied": False, "reason": CLOSE_NOT_DONE},
            "cleanup": CleanupJournal(self.data_dir).summary(sprint=str(event["ref"])),
        }

    def reopen(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        request_id: str | None = None,
        observer: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Reopen through the same admission/staged-intent path as create. Require a
        fresh explicit observer selection rather than inheriting a closed or migrated
        value; write it before transitioning to open.
        """
        # Admission before the SQL transaction and its advisory lock, the order create takes them in.
        with sprint_admission_lock(self.data_dir):
            return self._reopen_atomic(
                role=role, actor=actor, reference=reference, request_id=request_id, observer=observer
            )

    @_sql_atomic
    def _reopen_atomic(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        request_id: str | None,
        observer: dict[str, Any] | None,
    ) -> dict[str, Any]:
        self._role(role, {"po"}, actor=actor)
        request_id = request_id or str(uuid.uuid4())
        intent = SprintReopenIntent(
            role=Role(role),
            actor=actor,
            reference=reference,
            observer=self._observer_intent(observer, executable=True),
        )
        intent_document = intent.to_document()
        document, committed = self.transactions.existing(
            request_id, kind=SPRINT_REOPENED, intent=intent_document
        )
        if committed is not None:
            return self._committed_result(SPRINT_REOPENED, committed)
        if document is None:
            sprint_document = self.reader.show(reference, include_cards=False)
            sprint = SprintWriteSnapshot.from_document(sprint_document, thresholds=self.thresholds)
            self._check_reopen(sprint, reference)
            event = self._event(
                SPRINT_REOPENED,
                role,
                actor,
                reference,
                request_id,
                {"intent": intent_document},
                sprint,
            )
            document, committed = self.transactions.begin(
                request_id, kind=SPRINT_REOPENED, intent=intent_document, event=event
            )
            if committed is not None:
                return self._committed_result(SPRINT_REOPENED, committed)
            if document is None:
                raise TaskError("audit_pending", "sprint transaction claim is unavailable", 4)
        return self._run_reopen(document)

    def _check_reopen(self, sprint: SprintWriteSnapshot, reference: str) -> None:
        """Every rule an open sprint has to satisfy, read live before any write."""
        missing = [
            name
            for name, value in (
                ("product", sprint.product),
                ("issues", sprint.issues),
                ("reservations", sprint.reservations),
            )
            if not value
        ]
        if missing:
            raise TaskError(
                "validation",
                f"sprint {reference} predates sprint ownership and has no "
                + ", ".join(missing)
                + "; open a new sprint that owns its issues instead of reopening it",
                2,
            )
        self._check_ownership(sprint.product, list(sprint.issues), list(sprint.reservations))
        self._check_conflicts(sprint.admission(), excluding=reference)

    def _run_reopen(self, document: dict[str, Any]) -> dict[str, Any]:
        """Drive the staged reopen to its single audit event, or leave it repairable."""
        intent = SprintReopenIntent.from_document(document["intent"])
        reference = intent.reference
        try:
            sprint_document = self.reader.show(reference, include_cards=False)
            sprint = SprintWriteSnapshot.from_document(sprint_document, thresholds=self.thresholds)
            if not (document.get("progress") or {}).get("opened_done"):
                # A staged reopen held nothing while it waited for its repeat, so the
                # installation is measured again before this sprint becomes the open one.
                self._check_conflicts(sprint.admission(), excluding=reference)
            # The value the row carries now, recorded durably before the write that replaces it.
            self._record_observer_preimage(document, sprint)
            document.setdefault("progress", {})["observer_started"] = True
            self.transactions.save(document)
            if intent.observer is None:
                raise TaskError("validation", "sprint reopen intent lacks its observer", 2)
            self._transition_host(
                role=intent.role.value,
                actor=intent.actor,
                reference=reference,
                target="open",
                reason="Sprint reopened",
                request_id=str(document["request_id"]) + ":typed-reopen",
                observer=encode_observer(intent.observer),
            )
            document.setdefault("progress", {})["observer_done"] = True
            document.setdefault("progress", {})["opened_done"] = True
            self.transactions.save(document)
            sprint_document = self.reader.show(reference)
            sprint = SprintWriteSnapshot.from_document(sprint_document, thresholds=self.thresholds)
            event = document["event"]
            event["task_id"] = sprint.entity_id
            event["backend"]["revision"] = "updated_at:" + (sprint.updated_at or "unknown")
            self.transactions.save(document)
            self.transactions.complete(document)
            update_active_sprint_projects(self.data_dir, sprint.admission())
            return SprintMutationReceipt(SPRINT_REOPENED, str(event["event_id"])).to_document(sprint_document)
        except TaskError:
            raise
        except (OSError, KeyError, TypeError, ValueError):
            raise TaskError(
                "audit_pending",
                "sprint reopen is pending repair; retry with the same request id",
                4,
            ) from None

    def _record_observer_preimage(self, document: dict[str, Any], sprint: SprintWriteSnapshot) -> None:
        """Record what the row's observer was, once, before this reopen writes over it."""
        progress = document.setdefault("progress", {})
        if "observer_preimage" in progress:
            return
        try:
            progress["observer_preimage"] = encode_observer(sprint.observer) if sprint.observer else None
        except ValueError:
            progress["observer_preimage"] = None
        self.transactions.save(document)
