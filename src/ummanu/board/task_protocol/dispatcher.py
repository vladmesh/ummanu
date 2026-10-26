"""Dispatcher control writes for wait, PO, e2e, routing and accounting."""

from __future__ import annotations

import contextlib
import hashlib
import json
import uuid
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from typing import Any

from ummanu.board import (
    e2e_budget,
    e2e_record,
    po_execution as execution_field,
    po_origin as origin_field,
    wait_card,
)
from ummanu.board.completion_evidence import is_wait
from ummanu.board.events import AnalyticsOutcomeConflict, BoardEventCanon
from ummanu.board.head_changes import HEAD_CHANGED_KIND, head_change_payload
from ummanu.board.legacy_codec import text as _text
from ummanu.board.models import Actor, EntityKind, Event, EventKind
from ummanu.board.outcome_round_context import OutcomeRoundContext
from ummanu.board.roles import Role
from ummanu.board.task_protocol.rows import (
    _charge_time,
    _check_execution_record,
    _now,
    _task_number,
    post_merge_ci_fact,
)
from ummanu.board.task_routing import ROUTING_PHASE_VALUES, RoutingPhase, TaskType
from ummanu.kernel.errors import TaskError as TaskError


class DispatcherTaskWrites:

    def record_wait_state(self, *, role: str, actor: str, reference: str, state: str) -> None:
        """The dispatcher's one write of a wait card's `wait_state`. A state field, not
        an event: the dispatcher rewrites it only when what it knows changed (a new
        observation or error, the frozen result, a delivery record), and it carries no
        audit record of its own.
        """
        self._role(role, {Role.DISPATCHER}, actor=actor)
        task = self.reader.show(reference)
        if not is_wait(task):
            raise TaskError("validation", f"{reference} is not a wait card; it carries no wait state", 2)
        with self._mutation():
            self.client.call("saveTaskMetadata", task_id=_task_number(task), values={wait_card.WAIT_STATE: state})

    def record_po_return(self, *, role: str, actor: str, reference: str, state: str) -> None:
        """The dispatcher's one write of a delegated card's `po_return`. A state field
        like `wait_state`: the session it handed a decision/operation card to, the
        successors of the origin's line, and one record per terminal transition whose
        result it returned.
        """
        self._role(role, {Role.DISPATCHER}, actor=actor)
        task = self.reader.show(reference)
        if origin_field.po_origin(task) is None:
            raise TaskError("validation", f"{reference} names no PO origin; it carries no return state", 2)
        with self._mutation():
            self.client.call(
                "saveTaskMetadata", task_id=_task_number(task), values={origin_field.PO_RETURN: state}
            )

    def record_po_execution(self, *, role: str, actor: str, reference: str, state: str) -> None:
        """Persist service resolution on an explicitly assigned PO card; preserve create identity."""
        self._role(role, {Role.DISPATCHER}, actor=actor)
        offered = execution_field.PoExecution.from_document(json.loads(state))
        with self._mutation():
            if getattr(self.client, "credentials", None):
                self.client._query("SELECT task_ref FROM tasks WHERE task_ref=%s FOR UPDATE", (reference,))
            task = self.reader.show(reference)
            current = execution_field.assignment(task)
            if current is None or (current.request, current.purpose, current.sources) != (
                offered.request, offered.purpose, offered.sources
            ):
                raise TaskError("validation", "PO execution resolution cannot change assignment identity", 2)
            # Resolution is monotone: a stale tick cannot lose another tick's committed
            # session/choice or a successor already accepted under its stable service ID.
            def resolved(old: dict[str, str], new: dict[str, str]) -> dict[str, str]:
                return {**new, **{key: value for key, value in old.items() if value or key not in new}}
            offered.initial = resolved(current.initial, offered.initial)
            for closed, row in current.successors.items():
                offered.successors[closed] = resolved(row, offered.successors.get(closed, {}))
            if current.executor and current.executor != offered.executor:
                # Preserve a newer executor if the offered session precedes it in this line.
                from ummanu.board.po_origin import ReturnState, line_head
                offered.executor = line_head(offered.executor or current.executor,
                                             ReturnState(successors=offered.successors))
            self.client.call("saveTaskMetadata", task_id=_task_number(task),
                             values={execution_field.PO_EXECUTION: offered.text()})

    def record_e2e_state(self, *, role: str, actor: str, reference: str, state: str) -> None:
        """The dispatcher's one write of a code card's `e2e` run records. A state field
        like `wait_state`: the intent of each dispatch (written before the call), the
        run it identified, its wait card and the wait's frozen result.
        """
        self._role(role, {Role.DISPATCHER}, actor=actor)
        task = self.reader.show(reference)
        # A card of unknown kind reads as code, as the stage reads it.
        if str(task.get("type") or TaskType.CODE.value) != TaskType.CODE.value:
            raise TaskError("validation", f"{reference} is not a code card; it carries no e2e runs", 2)
        with self._mutation():
            self.client.call(
                "saveTaskMetadata", task_id=_task_number(task), values={e2e_record.E2E_FIELD: state}
            )

    def record_e2e_intent(
        self, *, role: str, actor: str, reference: str, state: str, sprint: str, dispatch_id: str
    ) -> dict[str, Any]:
        """Write e2e intent and conditionally charge a sprint's remaining run budget in
        one transaction; when exhausted, return `charged: false` without writes. An
        unsprinted card is governed by its own cap, not a sprint charge.
        """
        self._role(role, {Role.DISPATCHER}, actor=actor)
        task = self.reader.show(reference)
        if str(task.get("type") or TaskType.CODE.value) != TaskType.CODE.value:
            raise TaskError("validation", f"{reference} is not a code card; it carries no e2e runs", 2)
        with self._mutation():
            charge: dict[str, Any] = {"charged": True}
            if sprint:
                charge = self.client.call(
                    "chargeSprintE2e",
                    sprint_ref=sprint,
                    task_ref=reference,
                    dispatch_id=dispatch_id,
                    at=_charge_time(),
                )
                if not charge.get("charged"):
                    return charge
            self.client.call(
                "saveTaskMetadata", task_id=_task_number(task), values={e2e_record.E2E_FIELD: state}
            )
            return charge

    def record_after_merge_projection(self, *, role: str, actor: str, reference: str,
                                       expected: e2e_record.AfterMergeMark | None,
                                       proposed: e2e_record.AfterMergeMark,
                                       published_merge: bool = False) -> e2e_record.E2eState | None:
        """Compare the current obligation before publishing a queue/budget mark.

        No run records or charges are replaced by a stale queue snapshot.
        """
        from ummanu.board.e2e_disposition import source_obligation

        self._role(role, {Role.DISPATCHER}, actor=actor)
        with self._mutation():
            self.client._query("SELECT task_ref FROM tasks WHERE task_ref=%s FOR UPDATE", (reference,))
            task = self.reader.show(reference)
            current = e2e_record.e2e_state(task)
            if (not source_obligation(task, superseded=self._card_superseded(reference))
                    or current.after_merge != expected):
                return None
            if published_merge:
                fact = next((fact for event in reversed(self.audit.events(reference))
                             if (fact := post_merge_ci_fact(event)) is not None), None)
                if fact is None:
                    raise TaskError("backend_error", "Queued merge has no readable committed post-merge CI", 1)
                if fact.get("result") != "green" or fact.get("merge_sha") != proposed.merge_sha:
                    return None
            current.after_merge = proposed
            self.client.call("saveTaskMetadata", task_id=_task_number(task),
                             values={e2e_record.E2E_FIELD: current.text()})
            return current

    def record_after_merge_intent(
        self,
        *,
        role: str,
        actor: str,
        states: dict[str, str],
        sprint: str,
        dispatch_id: str,
        carrier: str,
        expected: dict[str, e2e_record.AfterMergeMark | None] | None = None,
    ) -> dict[str, Any]:
        """Atomically charge an after-merge e2e run and write intent to all covered
        cards. Charge the sprint budget when present; otherwise enforce every covered
        card's own cap. If any cap is exhausted, write nothing and return `charged:
        false` with `spent`.
        """
        self._role(role, {Role.DISPATCHER}, actor=actor)
        if carrier not in states:
            raise TaskError("validation", f"the carrier {carrier} is not among the covered cards", 2)
        from ummanu.board.e2e_disposition import pending_mark

        tasks = {reference: self.reader.show(reference) for reference in states}
        for reference, task in tasks.items():
            if str(task.get("type") or TaskType.CODE.value) != TaskType.CODE.value:
                raise TaskError("validation", f"{reference} is not a code card; it carries no e2e runs", 2)
        with self._mutation():
            for reference in sorted(states):
                self.client._query("SELECT task_ref FROM tasks WHERE task_ref=%s FOR UPDATE", (reference,))
            tasks = {reference: self.reader.show(reference) for reference in states}
            for reference, task in tasks.items():
                proposed = e2e_record.E2eState.from_json(json.loads(states[reference])).after_merge
                mark = e2e_record.e2e_state(task).after_merge
                if (proposed is None or not pending_mark(task,
                        superseded=self._card_superseded(reference), merge_sha=proposed.merge_sha)
                        or (expected is not None and mark != expected.get(reference))):
                    return {"charged": False, "stale": True}
            offered = {ref: e2e_record.E2eState.from_json(json.loads(value)) for ref, value in states.items()}
            new_run = offered[carrier].after_merge_run(dispatch_id)
            if (new_run is None or new_run.placement != e2e_record.AFTER_MERGE
                    or new_run.sha != offered[carrier].after_merge.merge_sha
                    or {item["ref"] for item in new_run.covered} != set(states)
                    or len(new_run.covered) != len(states)
                    or any(item["merge_sha"] != offered[item["ref"]].after_merge.merge_sha for item in new_run.covered)
                    or any(state.after_merge.dispatch_id != dispatch_id or state.after_merge.carrier != carrier
                           for state in offered.values())
                    or new_run.charged_to != (sprint or "cards")
                    or (sprint and tasks[carrier].get("sprint") != sprint)):
                raise TaskError("validation", "after-merge intent must bind the actual carrier/covered merges and payer", 2)
            charge: dict[str, Any] = {"charged": True}
            if sprint:
                charge = self.client.call(
                    "chargeSprintE2e",
                    sprint_ref=sprint,
                    task_ref=carrier,
                    dispatch_id=dispatch_id,
                    at=_charge_time(),
                )
                if not charge.get("charged"):
                    return charge
            else:
                spent = [
                    reference
                    for reference, task in tasks.items()
                    if e2e_record.e2e_state(task).dispatched >= e2e_budget.card_cap(task)
                ]
                if spent:
                    return {"charged": False, "spent": spent}
            for reference, proposed in offered.items():
                current = e2e_record.e2e_state(tasks[reference])
                proposed.after_merge.charged = [*current.after_merge.charged, *([] if sprint else [dispatch_id])]
                current.after_merge = proposed.after_merge
                if reference == carrier:
                    current.after_merge_runs.append(new_run)
                self.client.call(
                    "saveTaskMetadata", task_id=_task_number(tasks[reference]), values={e2e_record.E2E_FIELD: current.text()}
                )
            return charge

    def record_after_merge_run(self, *, role: str, actor: str, reference: str,
                               run: e2e_record.E2eRun) -> e2e_record.E2eState:
        """Update this run under the carrier lock, retaining newer marks and other runs."""
        self._role(role, {Role.DISPATCHER}, actor=actor)
        with self._mutation():
            self.client._query("SELECT task_ref FROM tasks WHERE task_ref=%s FOR UPDATE", (reference,))
            task = self.reader.show(reference)
            current = e2e_record.e2e_state(task)
            stored = current.after_merge_run(run.dispatch_id)
            if stored is None or any(getattr(stored, key) != getattr(run, key) for key in (
                    "sha", "covered", "repo", "branch", "workflow", "intent_at", "deadline",
                    "placement", "git_ref", "charged_to")):
                raise TaskError("validation", "after-merge run identity changed", 2)
            # A later completion receipt is durable and cannot be lost by a stale
            # closing/cleanup save. Intent identity and paid records stay intact.
            run.disposition_result = stored.disposition_result or run.disposition_result
            run.disposition = stored.disposition or run.disposition
            run.hotfix = stored.hotfix or run.hotfix
            run.result = stored.result if stored.result is not None else run.result
            run.resolution = stored.resolution or run.resolution
            if stored.git_ref_state == "deleted":
                run.git_ref_state = "deleted"
            run.acted = stored.acted or run.acted
            current.after_merge_runs[current.after_merge_runs.index(stored)] = run
            self.client.call("saveTaskMetadata", task_id=_task_number(task),
                             values={e2e_record.E2E_FIELD: current.text()})
            return current

    def record_after_merge_mark(self, *, role: str, actor: str, reference: str,
                                carrier: str, run: e2e_record.E2eRun,
                                changes: dict[str, Any]) -> e2e_record.E2eState:
        """Only this run's current covered merge may gain its outcome/holder."""
        from ummanu.board.e2e_disposition import owns_mark

        self._role(role, {Role.DISPATCHER}, actor=actor)
        with self._mutation():
            refs = {reference, carrier} | ({run.hotfix} if run.hotfix else set()) | ({run.disposition} if run.disposition else set())
            for ref in sorted(refs):
                self.client._query("SELECT task_ref FROM tasks WHERE task_ref=%s FOR UPDATE", (ref,))
            task = self.reader.show(reference)
            current = e2e_record.e2e_state(task)
            carried = e2e_record.e2e_state(self.reader.show(carrier))
            stored = carried.after_merge_run(run.dispatch_id)
            covered = next((item for item in run.covered if item["ref"] == reference), None)
            if (covered and stored and stored.sha == run.sha and stored.covered == run.covered
                    and owns_mark(current.after_merge, run=stored, carrier=carrier, covered=covered,
                                  task=task, superseded=self._card_superseded(reference),
                                  runs=carried.after_merge_runs)
                    and not stored.disposition_result):
                if (changes.get("hotfix", stored.hotfix) != stored.hotfix
                        or changes.get("decision", stored.disposition) != stored.disposition):
                    raise TaskError("validation", "mark holder must belong to the actual carrier run", 2)
                if stored.hotfix:
                    if stored.hotfix not in refs:
                        raise TaskError("backend_error", "hotfix changed during mark admission", 1)
                    self._after_merge_hotfix_obligation(self.reader.show(carrier), stored)
                if all(getattr(current.after_merge, name) == value for name, value in changes.items()):
                    return current
                for name, value in changes.items():
                    setattr(current.after_merge, name, value)
                self.client.call("saveTaskMetadata", task_id=_task_number(task),
                                 values={e2e_record.E2E_FIELD: current.text()})
            return current

    @contextlib.contextmanager
    def after_merge_disposition_admission(self, *, role: str, actor: str, carrier: str,
                                         run: e2e_record.E2eRun) -> Iterator[bool]:
        """Keep current obligation ownership locked through operation create/recovery.
        Historical runs retain their evidence and cleanup without a new question.
        """
        from ummanu.board.e2e_disposition import owns_mark

        self._role(role, {Role.DISPATCHER}, actor=actor)
        with self._mutation():
            # Read only identity before row locks; actual evidence is reread below.
            initial = e2e_record.e2e_state(self.reader.show(carrier)).after_merge_run(run.dispatch_id)
            refs = {carrier, *(item["ref"] for item in run.covered)}
            known = self.audit.committed_event(execution_field.DISPOSITION_PREFIX + run.dispatch_id)
            if known and known.get("ref"):
                refs.add(known["ref"])
            if initial and initial.disposition:
                refs.add(initial.disposition)
            if initial and initial.hotfix:
                refs.add(initial.hotfix)
            for ref in sorted(refs):
                self.client._query("SELECT task_ref FROM tasks WHERE task_ref=%s FOR UPDATE", (ref,))
            carrier_task = self.reader.show(carrier)
            carried = e2e_record.e2e_state(carrier_task)
            stored = carried.after_merge_run(run.dispatch_id)
            if (stored is None or stored.sha != run.sha or stored.covered != run.covered
                    or stored.hotfix != (initial.hotfix if initial else "")):
                raise TaskError("validation", "disposition must belong to the actual carrier/run", 2)
            if stored.disposition_result and stored.disposition_result.get("status") == "settled":
                yield False
                return
            known = self.audit.committed_event(execution_field.DISPOSITION_PREFIX + run.dispatch_id)
            if known:
                if (known.get("kind") != "created" or known.get("outcome") != "success"
                        or (known.get("actor") or {}).get("role") != "dispatcher"
                        or known.get("ref") not in refs
                        or (stored.disposition and known.get("ref") != stored.disposition)):
                    raise TaskError("backend_error", "Disposition create authority changed during admission", 1)
                operation = self.reader.show(known["ref"])
                if operation.get("type") != "operation" or operation.get("project") != carrier_task.get("project"):
                    raise TaskError("validation", "Disposition create is not this project's operation", 2)
            owned = False
            for item in stored.covered:
                task = self.reader.show(item["ref"])
                if task.get("project") != carrier_task.get("project"):
                    raise TaskError("validation", "disposition source is foreign", 2)
                owned |= owns_mark(e2e_record.e2e_state(task).after_merge, run=stored,
                    carrier=carrier, covered=item, task=task,
                    superseded=self._card_superseded(item["ref"]), runs=carried.after_merge_runs)
            # The run, not a caller's guessed hotfix, binds released create authority.
            if stored.hotfix:
                owned |= self._after_merge_hotfix_obligation(carrier_task, stored)
            run.hotfix, run.disposition = stored.hotfix, stored.disposition
            yield owned

    def _after_merge_hotfix_obligation(self, carrier: dict[str, Any], run: e2e_record.E2eRun) -> bool:
        from ummanu.board.e2e_disposition import hotfix_obligation

        try:
            return hotfix_obligation(self.reader.show(run.hotfix), run=run,
                project=carrier["project"], superseded=self._card_superseded(run.hotfix),
                created=self.audit.committed_event(e2e_record.AFTER_MERGE_HOTFIX_REQUEST_PREFIX + run.dispatch_id))
        except ValueError as exc:
            raise TaskError("backend_error", str(exc), 1) from None

    def reconcile_after_merge_disposition(self, *, role: str, actor: str, carrier: str,
                                          dispatch_id: str) -> dict[str, Any]:
        """Read native PO completion and atomically persist receipt plus covered
        obligations. Queue contents are a recoverable projection, never completion
        authority.
        """
        from ummanu.board.e2e_disposition import disposition_effect, owns_mark
        from ummanu.sprints import SprintReader

        self._role(role, {Role.DISPATCHER}, actor=actor)
        initial = e2e_record.e2e_state(self.reader.show(carrier)).after_merge_run(dispatch_id)
        if initial is None or not initial.disposition:
            raise TaskError("validation", "a disposition must belong to an actual after-merge run", 2)
        if not initial.covered or carrier not in {item["ref"] for item in initial.covered}:
            raise TaskError("validation", "disposition needs the actual covered carrier/sources", 2)
        created = self.audit.committed_event(execution_field.DISPOSITION_PREFIX + dispatch_id)
        if (not created or created.get("kind") != "created" or created.get("outcome") != "success"
                or created.get("ref") != initial.disposition
                or (created.get("actor") or {}).get("role") != "dispatcher"):
            raise TaskError("validation", "disposition operation has no matching committed run create", 2)
        refs = {carrier, initial.disposition, *(item["ref"] for item in initial.covered)}
        if initial.hotfix:
            refs.add(initial.hotfix)
        # Lock a possible follow-up along with the operation, then read it again
        # under the lock. Malformed completion cannot authorize a guessed holder.
        from ummanu.board.completion_evidence import E2E_DISPOSITION_SECTION, po_completion_record
        try:
            fields = po_completion_record(self.reader.show(initial.disposition)) or {}
            document = json.loads(fields.get(E2E_DISPOSITION_SECTION) or "null")
            if isinstance(document, dict) and isinstance(document.get("holder"), str):
                refs.add(document["holder"])
        except (ValueError, TaskError) as exc:
            if isinstance(exc, TaskError) and exc.code != "not_found":
                raise
        with self._mutation():
            for reference in sorted(refs):
                self.client._query("SELECT task_ref FROM tasks WHERE task_ref=%s FOR UPDATE", (reference,))
            tasks, missing = {}, []
            for ref in {carrier, *(item["ref"] for item in initial.covered)}:
                try:
                    tasks[ref] = self.reader.show(ref)
                except TaskError as exc:
                    if exc.code != "not_found" or ref == carrier:
                        raise
                    missing.append(ref)
            current = {ref: e2e_record.e2e_state(task) for ref, task in tasks.items()}
            run = current[carrier].after_merge_run(dispatch_id)
            if run is None or run.disposition != initial.disposition or run.covered != initial.covered or run.sha != initial.sha:
                raise TaskError("validation", "disposition run identity changed", 2)
            created = self.audit.committed_event(execution_field.DISPOSITION_PREFIX + dispatch_id)
            if (not created or created.get("kind") != "created" or created.get("outcome") != "success"
                    or created.get("ref") != run.disposition
                    or (created.get("actor") or {}).get("role") != "dispatcher"):
                raise TaskError("validation", "disposition operation has no matching committed run create", 2)
            if run.hotfix != initial.hotfix:
                raise TaskError("backend_error", "hotfix identity changed during admission", 1)
            if any(task.get("project") != tasks[carrier].get("project") or task.get("type") != "code" for task in tasks.values()):
                raise TaskError("validation", "disposition sources must be actual project code cards", 2)
            if run.disposition_result and run.disposition_result.get("status") == "settled":
                # Applied retry/decline and completed follow-up are historical.
                # Changing the old operation cannot enqueue or spend twice.
                return {**run.disposition_result, "covered": [], "changed": False}

            def read_locked(reference: str) -> dict[str, Any]:
                if reference not in refs:
                    raise TaskError("backend_error", "disposition holder changed during admission; retry the read", 1)
                return self.reader.show(reference)

            def read_sprint(reference: str) -> dict[str, Any]:
                value = SprintReader(self.client).show(reference, include_cards=False)
                sessions = {value.get("po_session")}
                # Resolver successors do not invalidate a completion made in the
                # recorded sprint session before it closed. Read durable route
                # history, never an unrecorded session or monetary grant.
                for event in self.audit.events(reference):
                    if event.get("kind") == "po_session_set" and (event.get("actor") or {}).get("role") == "po":
                        sessions.add((event.get("payload") or {}).get("po_session"))
                    elif event.get("kind") == "created":
                        sessions.add((event.get("payload") or {}).get("po_session"))
                return {**value, "_po_sessions": sessions - {None, ""}}

            hotfix_owned = self._after_merge_hotfix_obligation(tasks[carrier], run) if run.hotfix else False
            try:
                if missing:
                    raise TaskError("not_found", "Covered source(s): " + ", ".join(sorted(missing)), 2)
                operation = read_locked(run.disposition)
                if operation.get("project") != tasks[carrier].get("project"):
                    raise TaskError("validation", "disposition operation is foreign", 2)
                effect = disposition_effect(operation=operation, carrier=carrier, run=run,
                    events=self.audit.events(run.disposition), read=read_locked,
                    sprint=read_sprint,
                    superseded=self._card_superseded)
                if hotfix_owned and effect["action"] == "retry":
                    effect = {"status": "neutral", "action": "", "holder": "",
                        "reason": "Unowned hotfix needs planned follow_up or explicit decline to resolve its return route"}
            except TaskError as exc:
                if exc.code != "not_found":
                    raise
                effect = {"status": "neutral", "action": "", "holder": "",
                          "reason": f"Disposition evidence is missing: {exc.message}; PO must repair the route"}
            linked = []
            receipt = {"operation": run.disposition, **effect}
            changed = run.disposition_result != receipt
            for item in run.covered:
                if item["ref"] not in current:
                    continue
                mark = current[item["ref"]].after_merge
                if (run.disposition_result == receipt
                        or not owns_mark(mark, run=run, carrier=carrier, covered=item, task=tasks[item["ref"]],
                                        superseded=self._card_superseded(item["ref"]),
                                        runs=current[carrier].after_merge_runs)):
                    continue
                # A spent budget or newer holder/run is already an independent
                # obligation and was excluded by owns_mark above.
                mark.decision = run.disposition
                mark.dispatch_id = dispatch_id
                mark.holder = effect["holder"]
                mark.note = effect["reason"]
                mark.state = (e2e_record.AM_PENDING if effect["action"] == "retry" and effect["status"] == "settled"
                              else e2e_record.AM_DECLINED if effect["status"] == "settled"
                              else e2e_record.AM_RED if run.resolution == e2e_record.AM_RED and effect["status"] == "waiting"
                              else e2e_record.AM_BLOCKED)
                linked.append(item["ref"])
            if hotfix_owned and changed:
                hotfix = read_locked(run.hotfix)
                hotfix_state = e2e_record.e2e_state(hotfix)
                hotfix_state.hotfix_route = e2e_record.HotfixRoute(carrier, dispatch_id, receipt)
                self.client.call("saveTaskMetadata", task_id=_task_number(hotfix),
                                 values={"blocked_by": effect["holder"], e2e_record.E2E_FIELD: hotfix_state.text()})
                # This native publication is in the receipt transaction: a failed
                # comment or metadata write rolls back the whole disposition.
                self.comment(role="dispatcher", actor=actor, reference=run.hotfix,
                    body=f"PO operation {run.disposition}: {effect['status']}; {effect['reason']}. "
                         + (f"Return-route holder: {effect['holder']}." if effect["holder"]
                            else "No live return-route holder; " + ("explicit terminal disposition recorded."
                                if effect["status"] == "settled" else "PO must repair the same operation.")),
                    request_id="dispatcher-e2e-am-hotfix-disposition-" + dispatch_id + "-"
                        + hashlib.sha256(json.dumps(receipt, sort_keys=True).encode()).hexdigest()[:16])
            run.disposition_result = receipt
            for ref in {carrier, *linked}:
                self.client.call("saveTaskMetadata", task_id=_task_number(tasks[ref]),
                                 values={e2e_record.E2E_FIELD: current[ref].text()})
            return {**effect, "operation": run.disposition, "covered": linked, "changed": changed}

    def _sprint_open(self, sprint: str) -> bool:
        """Whether a card's sprint is open: a card of a closed sprint spends its own e2e cap after the
        merge (secretary-1807)."""
        from ummanu.sprints import SprintReader

        try:
            return str(SprintReader(self.client).show(sprint, include_cards=False).get("status") or "") == "open"
        except TaskError as exc:
            if exc.code == "not_found":
                return False
            raise

    def raise_e2e_cap(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        authorized_by: str,
        add: int | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Raise an unsprinted code card's e2e cap as PO only, with `authorized_by`
        pointing to an eligible post-handover owner comment containing `e2e budget:
        raise <N>`. The requested increment must equal N; commit cap and audit
        atomically. Each authorizing event can be consumed once, with refusals before
        writes.
        """
        role = self._role(role, {Role.PO}, actor=actor)
        if add is not None and (isinstance(add, bool) or not isinstance(add, int) or add < 1):
            raise TaskError("validation", f"--add is a whole number of runs, 1 or more; not {add!r}", 2)
        authorized_by = str(authorized_by or "").strip()
        current = self.reader.show(reference)
        if str(current.get("type") or TaskType.CODE.value) != TaskType.CODE.value:
            raise TaskError("validation", f"{reference} is not a code card; it has no e2e cap", 2)
        if str(current.get("sprint") or "") and self._sprint_open(str(current["sprint"])):
            raise TaskError(
                "validation",
                f"{reference} belongs to {current['sprint']}, whose e2e budget it spends: raise that with "
                "`sprint e2e-budget`",
                2,
            )
        decision, add = e2e_budget.authorized_raise(self.audit, self.reader, authorized_by, reference, add)
        request_id = request_id or f"e2e-cap-raise-{authorized_by}"
        identity = {"add": add, "authorized_by": authorized_by}
        if self.audit.event(request_id) is None:
            for event in self.audit.events(reference, kind=e2e_budget.CARD_CAP_RAISED):
                if (event.get("payload") or {}).get("authorized_by") == authorized_by:
                    raise TaskError(
                        "authorization_refused",
                        f"the owner's comment {authorized_by} already authorized a raise of {reference}'s e2e "
                        f"cap ({event.get('request_id')}); each answer raises once",
                        3,
                    )
        at = _now()

        def payload(task: dict[str, Any]) -> dict[str, Any]:
            _check_execution_record(task)
            return {**identity, "decision": decision, "cap": e2e_budget.card_cap(task) + add}

        def mutation(task: dict[str, Any]) -> None:
            raises = [
                *e2e_budget.cap_raises(task),
                {"add": add, "authorized_by": authorized_by, "decision": decision, "at": at},
            ]
            self.client.call(
                "saveTaskMetadata",
                task_id=_task_number(task),
                values={e2e_budget.E2E_CAP_FIELD: e2e_budget.cap_text(raises)},
            )

        return self._write(
            e2e_budget.CARD_CAP_RAISED, role, actor, reference, request_id, payload, mutation, identity=identity
        )

    def routing(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        payload: dict[str, Any],
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Append one routing telemetry record for the card. Journal-only: the board
        holds no per-attempt routing history, so this write has no backend mutation.
        """
        role = self._role(role, {Role.DISPATCHER}, actor=actor)
        phase = _text(payload.get("phase"))
        if phase not in ROUTING_PHASE_VALUES:
            known = ", ".join(sorted(ROUTING_PHASE_VALUES))
            raise TaskError("validation", f"unknown routing phase {phase!r} (known: {known})", 2)
        routing_phase = RoutingPhase(phase)
        normalized_payload = dict(payload)
        normalized_payload["phase"] = routing_phase.value
        heads = payload.get("heads")
        if not isinstance(heads, list) or not heads:
            raise TaskError("validation", "routing requires at least one head record", 2)
        return self._write(
            "routing",
            role,
            actor,
            reference,
            request_id,
            normalized_payload,
            lambda task: None,
            identity=normalized_payload,
        )

    def head_changed(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        payload: Mapping[str, Any],
        request_id: str,
    ) -> dict[str, Any]:
        """Append one head lifecycle record for the card, keyed by the stopped run's request id.
        Journal-only: the card's state and metadata are not touched.
        """
        role = self._role(role, {Role.DISPATCHER}, actor=actor)
        if not request_id.strip():
            raise TaskError("validation", "a head change needs the request id of its operation", 2)
        try:
            normalized = head_change_payload(
                role=str(payload.get("role") or ""),
                cause=str(payload.get("cause") or ""),
                reason=str(payload.get("reason") or ""),
                before=payload.get("before"),
                after=payload.get("after"),
                requested=str(payload.get("requested") or ""),
            )
        except ValueError as exc:
            raise TaskError("validation", str(exc), 2) from None
        return self._write(
            HEAD_CHANGED_KIND,
            role,
            actor,
            reference,
            request_id,
            normalized,
            lambda task: None,
            identity=normalized,
        )

    def outcome_round_context(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        data: OutcomeRoundContext | Mapping[str, object],
        request_id: str,
    ) -> dict[str, Any]:
        """Persist one exact forward source identity before its consumer runs. This is
        journal-only.
        """
        role = self._role(role, {Role.DISPATCHER}, actor=actor)
        try:
            context = (
                data
                if isinstance(data, OutcomeRoundContext)
                else OutcomeRoundContext.from_data(data)
            )
        except ValueError as exc:
            raise TaskError("validation", str(exc), 2) from None
        if not request_id.strip():
            raise TaskError("validation", "outcome round context needs the request id it owns", 2)
        payload = context.to_data()
        return self._write(
            "outcome_round_context",
            role,
            actor,
            reference,
            request_id,
            payload,
            lambda task: None,
            identity=payload,
        )

    def attempt_usage(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        data: dict[str, Any],
        reason: str,
        request_id: str,
    ) -> dict[str, Any]:
        """Stage then append one immutable, schema-validated `attempt.usage` audit
        occurrence, keyed by request id. Retries reuse the staged event rather than
        recomputing changed session data. Failed append remains staged for
        `finish_attempt_usage`; failed staging is an audit error. This is journal-only,
        not a board mutation.
        """
        role = self._role(role, {Role.DISPATCHER}, actor=actor)
        if not request_id.strip():
            raise TaskError("validation", "an attempt usage event needs the request id it owns", 2)
        canon = self.board_host.canon
        if canon is None:
            raise TaskError("backend_unavailable", "board event canon is unavailable", 1)
        try:
            existing = canon.event(request_id)
        except (OSError, ValueError) as exc:
            raise TaskError(
                "audit_unavailable", f"attempt usage occurrence is unreadable: {exc}", 4
            ) from None
        if existing is not None:
            return self._commit_attempt_usage(canon, request_id, existing, replayed=True)
        try:
            event = Event(
                event_id="evt_" + uuid.uuid4().hex,
                kind=EventKind.ATTEMPT_USAGE,
                entity_kind=EntityKind.CARD,
                ref=reference,
                actor=Actor(role, actor),
                reason=reason,
                occurred_at=datetime.now(UTC),
                data=dict(data),
            )
            canon.stage(request_id, event)
        except ValueError as exc:
            raise TaskError("validation", str(exc), 2) from None
        except OSError:
            raise TaskError("audit_unavailable", "attempt usage occurrence could not be staged", 4) from None
        return self._commit_attempt_usage(canon, request_id, event, replayed=False)

    def _commit_attempt_usage(
        self,
        canon: BoardEventCanon,
        request_id: str,
        event: Event,
        *,
        replayed: bool,
    ) -> dict[str, Any]:
        """Publish the staged occurrence, or report that its obligation is still owed."""
        try:
            canon.commit(request_id, event)
        except ValueError as exc:
            raise TaskError("validation", str(exc), 2) from None
        except OSError:
            raise TaskError(
                "audit_pending",
                "attempt usage occurrence is staged and awaits its journal append",
                4,
            ) from None
        return {"action": "attempt_usage", "event_id": event.event_id, "replayed": replayed}

    def finish_attempt_usage(self, *, role: str, reference: str = "") -> int:
        """Publish exact staged `attempt.usage` records, never recomputing them from
        session files. Without `reference`, process all pending cards, including
        Blocked/Done. Publishing is idempotent; failed records stay pending.
        """
        role = self._role(role, {Role.DISPATCHER}, actor="")
        canon = self.board_host.canon
        if canon is None:
            return 0
        finished = 0
        for occurrence in canon.attempt_usage_occurrences(ref=reference):
            if not occurrence.pending:
                continue
            try:
                canon.commit(occurrence.request_id, occurrence.event)
            except (OSError, TypeError, ValueError, TaskError):
                # The obligation stays exactly where it is: still staged, still owed, still exact.
                continue
            finished += 1
        return finished

    def attempt_outcome(
        self,
        *,
        role: str,
        actor: str,
        reference: str,
        data: dict[str, Any],
        reason: str,
        request_id: str,
    ) -> dict[str, Any]:
        """Stage then append one immutable observational terminal occurrence. This
        writer intentionally has no board effect.
        """
        role = self._role(role, {Role.DISPATCHER}, actor=actor)
        if not request_id.strip():
            raise TaskError("validation", "an attempt outcome needs the request id it owns", 2)
        canon = self.board_host.canon
        if canon is None:
            raise TaskError("backend_unavailable", "board event canon is unavailable", 1)
        try:
            occurrences = canon.attempt_outcome_occurrences(ref=reference)
            key = (reference, data.get("attempt_id"), data.get("report_generation"))
            existing_occurrence = next(
                (
                    occurrence
                    for occurrence in occurrences
                    if (
                        occurrence.event.ref,
                        occurrence.event.data.get("attempt_id"),
                        occurrence.event.data.get("report_generation"),
                    )
                    == key
                ),
                None,
            )
            if existing_occurrence is not None:
                if existing_occurrence.event.data != data:
                    raise AnalyticsOutcomeConflict(
                        f"attempt outcome natural key {key!r} has conflicting payloads"
                    )
                return self._commit_attempt_outcome(
                    canon, existing_occurrence.request_id, existing_occurrence.event, replayed=True
                )
            event = Event(
                event_id="evt_" + uuid.uuid4().hex,
                kind=EventKind.ATTEMPT_OUTCOME,
                entity_kind=EntityKind.CARD,
                ref=reference,
                actor=Actor(role, actor),
                reason=reason,
                occurred_at=datetime.now(UTC),
                data=dict(data),
            )
            canon.stage(request_id, event)
        except AnalyticsOutcomeConflict as exc:
            raise TaskError("analytics_outcome_conflict", str(exc), 3) from None
        except ValueError as exc:
            raise TaskError("validation", str(exc), 2) from None
        except OSError:
            raise TaskError("audit_unavailable", "attempt outcome could not be staged", 4) from None
        return self._commit_attempt_outcome(canon, request_id, event, replayed=False)

    def _commit_attempt_outcome(
        self, canon: BoardEventCanon, request_id: str, event: Event, *, replayed: bool
    ) -> dict[str, Any]:
        try:
            canon.commit(request_id, event)
        except ValueError as exc:
            raise TaskError("validation", str(exc), 2) from None
        except OSError:
            raise TaskError(
                "audit_pending", "attempt outcome is staged and awaits its journal append", 4
            ) from None
        return {"action": "attempt_outcome", "event_id": event.event_id, "replayed": replayed}

    def finish_attempt_outcomes(self, *, role: str, reference: str = "") -> int:
        """Append staged outcomes only; it never derives or changes lifecycle facts."""
        role = self._role(role, {Role.DISPATCHER}, actor="")
        canon = self.board_host.canon
        if canon is None:
            return 0
        finished = 0
        for occurrence in canon.attempt_outcome_occurrences(ref=reference):
            if not occurrence.pending:
                continue
            try:
                canon.commit(occurrence.request_id, occurrence.event)
            except (OSError, TypeError, ValueError, TaskError):
                continue
            finished += 1
        return finished
