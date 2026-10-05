"""Non-monetary after-merge dispositions, carried by native PO completion records.

One run owns one operation. Completion evidence is historical; only an unresolved
operation or concrete planned follow-up can be the active holder. The dispatcher
applies this rule under card row locks and recovers pending work from durable marks.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping

from ummanu.board import e2e_record, po_execution, po_origin, wait_card
from ummanu.board.audit_contract import card_transition_of, is_protocol_event
from ummanu.board.completion_evidence import (
    E2E_DISPOSITION_SECTION, po_completion_record, render_po_completion_record,
)


def safe_retry(run: e2e_record.E2eRun) -> bool:
    result = run.result or {}
    return not run.closing and (
        result.get("outcome") in {wait_card.DEADLINE_PASSED, wait_card.CANCELLED}
        or (result.get("outcome") == wait_card.TARGET_REACHED
            and run.conclusion in {"cancelled", "timed_out", "skipped"})
    )


def source_obligation(task: Mapping[str, Any], *, superseded: bool) -> bool:
    """Retention is visibility, not disposition of a code card's e2e obligation."""
    return task.get("type", "code") == "code" and not superseded


def pending_mark(task: Mapping[str, Any], *, superseded: bool,
                 merge_sha: str | None = None) -> bool:
    mark = e2e_record.e2e_state(task).after_merge
    return bool(source_obligation(task, superseded=superseded) and mark
                and mark.state in {e2e_record.AM_PENDING, e2e_record.AM_BUDGET_WAIT}
                and (merge_sha is None or mark.merge_sha == merge_sha))


def owns_mark(mark: e2e_record.AfterMergeMark | None, *, run: e2e_record.E2eRun,
              carrier: str, covered: Mapping[str, str], task: Mapping[str, Any] | None = None,
              superseded: bool = False, runs: list[e2e_record.E2eRun] | None = None) -> bool:
    """Current merge/run/holder wins over old recovery, including released 0024 marks."""
    if (superseded or (task is not None and not source_obligation(task, superseded=superseded))
            or mark is None or mark.merge_sha != covered["merge_sha"] or mark.carrier != carrier):
        return False
    if not mark.dispatch_id and runs is not None:
        latest = next((candidate for candidate in reversed(runs) if covered in candidate.covered), None)
        if latest is not run:
            return False
    if mark.dispatch_id and mark.dispatch_id != run.dispatch_id:
        return False
    if mark.decision and mark.decision != run.disposition:
        return False
    if mark.holder not in (None, "", run.disposition, (run.disposition_result or {}).get("holder")):
        return False
    return mark.state in {e2e_record.AM_COVERED, e2e_record.AM_BLOCKED, e2e_record.AM_RED,
                          e2e_record.AM_PENDING}


def hotfix_obligation(task: Mapping[str, Any], *, run: e2e_record.E2eRun,
                      project: str, created: Mapping[str, Any] | None,
                      superseded: bool) -> bool:
    """An actual released hotfix is independent of its covered source marks.

    The caller reads create authority under the same locks as run/card evidence.
    Missing or inconsistent authority is degraded, never a terminal conclusion.
    """
    if (not created or created.get("kind") != "created" or created.get("outcome") != "success"
            or created.get("ref") != run.hotfix or (created.get("actor") or {}).get("role") != "dispatcher"
            or task.get("ref") != run.hotfix or task.get("type") != "code"
            or task.get("project") != project):
        raise ValueError("Hotfix lacks matching committed dispatcher create/run identity")
    receipt = run.disposition_result or {}
    return bool(not superseded and not task.get("closed") and task.get("state") == "blocked"
                and not task.get("sprint") and po_origin.po_origin(task) is None
                and task.get("blocked_by") in (None, "", run.disposition, receipt.get("holder"))
                and receipt.get("status") != "settled")


def completion_identity(operation: str, carrier: str, run: e2e_record.E2eRun) -> dict[str, Any]:
    return {"operation": operation, "carrier": carrier, "run": run.dispatch_id,
            "covered": [dict(item) for item in run.covered]}


def parse_outcome(text: str, *, operation: str, carrier: str,
                  run: e2e_record.E2eRun) -> dict[str, Any]:
    """Strict finite record; no prose, budget grant, or truthy coercion is authority."""
    value = json.loads(text)
    identity = completion_identity(operation, carrier, run)
    if not isinstance(value, dict) or any(value.get(key) != expected for key, expected in identity.items()):
        raise ValueError("E2E disposition must name this operation, carrier, run and exact covered merges")
    action = value.get("action")
    extra = {"prior_effect"} if action == "retry" else {"holder"} if action == "follow_up" else set()
    if action not in {"retry", "decline", "follow_up"} or set(value) != {*identity, "action", "evidence", *extra}:
        raise ValueError("E2E disposition requires retry, decline or follow_up and only its documented fields")
    if not isinstance(value["evidence"], str) or not value["evidence"].strip():
        raise ValueError("E2E disposition needs investigation evidence")
    if action == "retry" and value["prior_effect"] not in {"not_started", "finished"}:
        raise ValueError("Retry needs investigated prior_effect not_started or finished; unresolved effects cannot retry")
    if action == "follow_up" and (not isinstance(value["holder"], str) or not value["holder"].strip()
                                  or value["holder"] in {operation, carrier, *(item['ref'] for item in run.covered)}):
        raise ValueError("Follow-up must name separate real planned work")
    return value


def disposition_effect(*, operation: dict[str, Any], carrier: str, run: e2e_record.E2eRun,
                       events: list[dict[str, Any]], read: Callable[[str], dict[str, Any]],
                       sprint: Callable[[str], dict[str, Any]],
                       superseded: Callable[[str], bool]) -> dict[str, str]:
    """Authoritative outcome rule. Read failures propagate; they are never terminal evidence."""
    ref = str(operation["ref"])
    def neutral(why: str) -> dict[str, str]:
        return {"status": "neutral", "action": "", "holder": "",
                "reason": why + ". PO must repair the disposition/route and record a bound native completion"}
    if (operation.get("type") != "operation" or superseded(ref)
            or (operation.get("closed") and operation.get("state") != "done")):
        return neutral("Disposition operation is missing, closed or superseded; PO must repair its route")
    if operation.get("state") in {"ready", "in_progress", "blocked"}:
        return {"status": "waiting", "action": "", "holder": ref, "reason": "PO disposition is unresolved"}
    if operation.get("state") != "done":
        return neutral("Disposition operation has no actionable or completed state")
    fields = po_completion_record(operation)
    if fields is None or not fields.get(E2E_DISPOSITION_SECTION):
        return neutral("Operation completed without a structured E2E disposition; PO must reopen and natively complete this operation with a bound outcome")
    # A PO comment alone is not a completion. Require the native atomic In progress
    # -> Done transition with this exact rendered record and its actual PO session.
    record = render_po_completion_record("operation", fields)
    completed = next((event for event in reversed(events)
                      if (edge := card_transition_of(event)) and edge[1] == "done"), None)
    if (completed is None or not is_protocol_event(completed)
            or card_transition_of(completed) != ("in_progress", "done")
            or completed.get("ref") != ref or (completed.get("actor") or {}).get("role") != "po"
            or completed.get("reason") != record):
        return neutral("Disposition lacks matching native PO completion authority")
    session = str((completed.get("data") or {}).get("po_session") or "")
    assignment = po_execution.assignment(operation)
    origin = po_origin.po_origin(operation)
    if operation.get("sprint"):
        route = sprint(str(operation["sprint"]))
        authority = session and session in route.get("_po_sessions", {route.get("po_session")})
    elif origin:
        authority = session and po_origin.in_line(session, origin["session"], po_origin.return_state(operation))
    else:
        authority = bool(session and assignment and assignment.request == po_execution.DISPOSITION_PREFIX + run.dispatch_id
                         and assignment.purpose == "e2e_disposition"
                         and set(assignment.sources) == {item["ref"] for item in run.covered}
                         and session in {assignment.initial.get("session"), assignment.executor,
                                         *(row.get("session") for row in assignment.successors.values())})
    if not authority:
        return neutral("Disposition completion's PO session is not the operation's supported execution route")
    try:
        outcome = parse_outcome(fields[E2E_DISPOSITION_SECTION], operation=ref, carrier=carrier, run=run)
    except (ValueError, TypeError) as exc:
        return neutral(str(exc))
    effect = {"status": "settled", "action": outcome["action"], "holder": "",
              "reason": outcome["evidence"], "completion": str(completed.get("event_id") or completed["request_id"])}
    if outcome["action"] == "follow_up":
        holder = read(outcome["holder"])
        if ((holder.get("closed") and holder.get("state") != "done") or superseded(outcome["holder"])
                or holder.get("project") != operation.get("project")
                or holder.get("type") not in {"code", "infra", "research", "decision", "operation"}):
            return neutral("Follow-up holder is closed, superseded, foreign or not real planned work")
        if holder.get("sprint"):
            planned = sprint(str(holder["sprint"])).get("status") == "open" or holder.get("state") == "done"
        else:
            planned = po_origin.po_origin(holder) is not None
        if not planned:
            return neutral("Follow-up needs an open supported sprint or a genuine PO turn")
        if holder.get("state") != "done":
            if holder.get("state") not in {"ready", "in_progress", "validate", "assessment", "blocked"}:
                return neutral("Follow-up has no live planned state")
            effect.update(status="follow_up", holder=outcome["holder"])
        else:
            effect["reason"] += f"; follow-up {outcome['holder']} completed, settling this disposition"
    return effect
