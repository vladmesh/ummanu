"""The e2e stage after the merge: one run on `main` for every card merged since the last (secretary-1807).

A project whose e2e workflow can only run on a commit whose releases the post-merge CI of `main`
published declares `validation.e2e.placement: after_merge` (`dispatch/e2e.py`). Its cards never run the
before-merge stage (`dispatch/e2e_stage.py`): they go through review, Assessment and release as a
project with no e2e. This module runs the workflow afterwards.

Queueing. When the post-merge watch (`dispatch/post_merge.py`) records a card's merge commit green, the
card joins its project's pending set (:func:`enqueue`), with its merge SHA. The watch is dropped only once
the card is queued, or its project is established not to be `after_merge`; while the adapter or its
declaration cannot be read the watch stays, and each pass retries the enqueue alone. A red, absent or timed-out
post-merge CI queues nothing. The pending sets live in the dispatcher's production state
(:data:`AFTER_MERGE_KEY`), beside the post-merge watches, one per project:
`{pending: [...], run: {...} | None, budget_waits: [...], cleanup: [...]}`. Each card also carries its
place on the board (`e2e.after_merge`, `board/e2e_record.py`).

One run in flight per project, coalesced. While a project has a run in flight its pending cards wait.
When none is and the set is not empty, the target is the newest pending merge SHA (every pending card
already has green post-merge CI), and the run covers every pending card whose merge SHA is the target
or one of its ancestors (GitHub's compare). Cards merged later, or off that line, stay pending. The run
record, with the covered cards and the SHA, is the intent, written on every covered card in the
transaction that charges it (`TaskWriter.record_after_merge_intent`), before anything reaches GitHub;
its *carrier* is the newest covered card, which holds the record. Other projects never wait on it.

Exact SHA. `workflow_dispatch` takes a branch, not a SHA, and `main` may have moved past the target. So
the dispatcher creates a branch it owns, `pipeline-e2e/<dispatch id>` (codegen's `ci.yml` runs on a push
to `main` only, so the branch triggers nothing), pointed at the target, and dispatches on it. The run's
`head_sha` is checked against the target; a run on anything else is never attached. The branch is
deleted once the run's result was acted on, and a branch whose delete got no answer stays in the
project's `cleanup` list until it is gone. The dispatch is sent only in the tick that wrote the intent:
a dispatcher that died between the intent (or the branch) and the dispatch finds the run by the
recovery rule of the before-merge stage (event, branch, SHA, creation time, the settle and the
ambiguity rules), and never dispatches it a second time.

Budget. The run is charged at the intent, exactly as a before-merge run: to the carrier's sprint when
that sprint is open, otherwise to every covered card's own cap, all together or none. When nothing is
left nothing is dispatched, and the batch is the unit: one decision card owns every covered card. In a
sprint it is the sprint's budget decision of the before-merge stage, cut or joined; outside one it is the
batch's own decision (`e2e_budget.batch_decision_request_id`), naming every covered card with its cap and
authorizing a raise of each spent one. Each covered card shows `e2e: budget spent, waiting on
<decision>`, and a card queued later joins the same decision. A raise lets the next pass attempt the whole
batch; the decision completed without one declines every card waiting on it. A batch without a real
PO origin has explicit durable execution assignment to a dedicated native PO service session.

Waiting. A `wait` card on the run, with the adapter's deadline, returning to `card:<carrier>`, created
once under a request id derived from the dispatch id. Its frozen result is read each tick.

Outcomes:

- conclusion `success`: one `## E2E after merge — green` comment on every covered card;
- conclusion `failure`: one `code` hotfix card, idempotent per run, with the run, its conclusion, the
  failed jobs and steps, the bounded `--log-failed` fragment, the SHA and every covered card with its
  merge SHA: in the carrier's sprint when it is open (a `hotfix` budget event, and the sprint observer
  wakes on it as on a Blocked card); otherwise outside every sprint with the carrier's PO origin; with
  neither, created and Blocked at once, with a per-run PO operation owning return-route assignment;
- anything else (another conclusion, a wait outcome other than `target_reached`, a refused dispatch, a
  run never identified, several candidates, a run on another SHA): no hotfix, a comment on every
  covered card and a routine notice. Planned cancellation/time-limit outcomes requeue through budget
  admission; unconfirmed dispatch/SHA/source evidence gets one PO operation before any further paid run.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from ummanu.board import e2e_budget, e2e_record, owner_decisions, owner_events, po_execution, wait_card
from ummanu.board import po_origin as origin_field
from ummanu.board.e2e_record import (
    AFTER_MERGE,
    AM_BLOCKED,
    AM_BUDGET_WAIT,
    AM_COVERED,
    AM_DECLINED,
    AM_GREEN,
    AM_PENDING,
    AM_RED,
    AM_REQUEUED,
    FAILURE,
    REFUSED,
    SENT,
    SUCCESS,
    AfterMergeMark,
    E2eRun,
    E2eState,
)
from ummanu.board.terminal_taxonomy import normalize_terminal_taxonomy
from ummanu.dispatch.e2e import (
    AFTER_MERGE_REF_PREFIX,
    E2E_CLOCK_MARGIN_SECONDS,
    E2E_IDENTIFY_SECONDS,
    E2E_RECOVERY_SETTLE_SECONDS,
    DispatchRefused,
    E2eDeclaration,
    create_ref,
    declared_e2e,
    delete_ref,
    dispatch_workflow,
    is_ancestor,
    matching_runs,
    run_head_sha,
)
from ummanu.dispatch.e2e_stage import (
    IDENTIFIED_BY_ANSWER,
    IDENTIFIED_BY_RECOVERY,
    _decision_card,
    _red_evidence,
    stage_request_id,
    utcnow,
)
from ummanu.dispatch.gate import _name_with_owner
from ummanu.dispatch.helpers import safe_one_line, scrub_host_output
from ummanu.dispatch.state import request_token
from ummanu.dispatch.types import HostError
from ummanu.tasks import TaskError

#: The dispatcher production-state key of every project's after-merge queue.
AFTER_MERGE_KEY = "e2e_after_merge"
STEP = "e2e-after-merge"
#: The dispatch-id infix of an after-merge run: `<carrier>-e2e-am-<n>-<random>`.
DISPATCH_INFIX = e2e_budget.AFTER_MERGE_DISPATCH_INFIX
#: The Blocked reason of a hotfix card nobody owns.
UNOWNED_HOTFIX_REASON = "after-merge e2e red, no sprint or origin owns it"
#: How much of the red run's evidence a hotfix card carries at most.
_EVIDENCE_LIMIT = 12000


# --- the queue ------------------------------------------------------------------------------------


def queues(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Every project's after-merge queue in the dispatcher production state, created on first use."""
    raw = payload.get(AFTER_MERGE_KEY)
    if not isinstance(raw, dict):
        raw = {}
        payload[AFTER_MERGE_KEY] = raw
    return raw


def _queue(payload: dict[str, Any], project: str) -> dict[str, Any]:
    queue = queues(payload).setdefault(project, {})
    queue.setdefault("pending", [])
    queue.setdefault("run", None)
    queue.setdefault("budget_waits", [])
    queue.setdefault("cleanup", [])
    return queue


def _outcome(
    project: str, action: str, *, ref: str = "", status: str = "ok", **fields: Any
) -> dict[str, Any]:
    return {"status": status, "step": STEP, "action": action, "project": project, "pilot_ref": ref, **fields}


@dataclass(frozen=True)
class Enqueued:
    """What :func:`enqueue` made of a resolved post-merge watch.

    `settled`: the watch may be dropped, because its card is queued (now or before) or its project is
    established not to run its e2e after the merge. False when that could not be established (the adapter
    or its e2e declaration could not be read, or the card could not): the watch is kept, with its
    published CI fact, and the next pass asks again. `outcome` is what the tick reports, if anything.
    """

    settled: bool
    outcome: dict[str, Any] | None = None


def enqueue(runtime: Any, payload: dict[str, Any], watch: dict[str, Any]) -> Enqueued:
    """Queue the card of a post-merge watch whose merge commit's CI is recorded green, when its project
    declares `placement: after_merge`.

    Idempotent per card and merge SHA: a card already pending, covered by the run in flight, or already
    marked on the board for this merge SHA (it was queued, and has moved on) is not added again, so a
    replay after a save that kept both the queued card and the watch queues it once.
    """
    result = watch.get("result") if isinstance(watch.get("result"), dict) else {}
    if result.get("result") != "green":
        return Enqueued(True)
    project = str(watch.get("project") or "")
    ref = str(watch.get("ref") or "")
    merge_sha = str(result.get("merge_sha") or watch.get("merge_sha") or "")
    try:
        declaration = declared_e2e(runtime.host, project)
    except HostError as exc:
        return Enqueued(
            False,
            _outcome(
                project,
                "e2e-after-merge-not-queued",
                ref=ref,
                status="degraded",
                reason=(
                    f"the e2e declaration cannot be read: {scrub_host_output(str(exc))}; the post-merge watch is "
                    "kept and the card is queued once it can be"
                ),
            ),
        )
    if declaration is None or not declaration.after_merge or not ref or not merge_sha:
        return Enqueued(True)
    queue = _queue(payload, project)
    run = queue.get("run") or {}
    known = {str(entry.get("ref")) for entry in queue["pending"]} | {
        str(entry.get("ref")) for entry in run.get("entries") or []
    }
    if ref in known:
        return Enqueued(True)
    try:
        mark = e2e_record.e2e_state(runtime.reader.show(ref)).after_merge
    except TaskError as exc:
        return Enqueued(
            False,
            _outcome(
                project,
                "e2e-after-merge-not-queued",
                ref=ref,
                status="degraded",
                reason=f"the card cannot be read: {exc.code}: {exc.message}; the post-merge watch is kept",
            ),
        )
    if mark is not None and mark.merge_sha == merge_sha:
        return Enqueued(True)
    queue["pending"].append(
        {
            "ref": ref,
            "merge_sha": merge_sha,
            "sprint": str(watch.get("sprint") or ""),
            "merged_at": float(watch.get("started_at") or time.time()),
            "repo": str(watch.get("repo") or ""),
            "base": str(watch.get("base") or ""),
            "marked": False,
        }
    )
    return Enqueued(True, _outcome(project, "e2e-after-merge-queued", ref=ref, merge_sha=merge_sha))


def reconcile_after_merge(
    runtime: Any, payload: dict[str, Any], records: dict[str, Any]
) -> list[dict[str, Any]]:
    """Advance every project's after-merge queue once: a project never waits on another."""
    outcomes: list[dict[str, Any]] = []
    # The board, including drained queues, is authoritative. Isolate each carrier:
    # one unreadable released run must not starve any other disposition.
    try:
        cards = runtime.reader.list()
    except (TaskError, ValueError, TypeError, KeyError) as exc:
        cards = []
        outcomes.append(_outcome("", "e2e-after-merge-route-unread", status="degraded", reason=str(exc)))
    # Released 0024 hotfix creates can name a carrier outside the active listing.
    carriers = {card["ref"] for card in cards if e2e_record.e2e_state(card).after_merge_runs}
    for hotfix in cards:
        if hotfix.get("type") != "code" or hotfix.get("state") != "blocked" or hotfix.get("sprint") or origin_field.po_origin(hotfix):
            continue
        try:
            for event in runtime.audit.events(hotfix["ref"], kind="created"):
                request = str(event.get("request_id") or "")
                if not request.startswith(e2e_record.AFTER_MERGE_HOTFIX_REQUEST_PREFIX):
                    continue
                dispatch_id = request.removeprefix(e2e_record.AFTER_MERGE_HOTFIX_REQUEST_PREFIX)
                carriers.add(dispatch_id.split(DISPATCH_INFIX, 1)[0])
        except (TaskError, ValueError, TypeError, KeyError) as exc:
            outcomes.append(_outcome(str(hotfix.get("project") or ""), "e2e-after-merge-route-unread",
                                     ref=hotfix["ref"], status="degraded", reason=str(exc)))
    for carrier_ref in sorted(carriers):
        try:
            carrier = runtime.reader.show(carrier_ref)
            run_ids = [run.dispatch_id for run in e2e_record.e2e_state(carrier).after_merge_runs]
        except (TaskError, ValueError, TypeError, KeyError) as exc:
            outcomes.append(_outcome("", "e2e-after-merge-route-unread", ref=carrier_ref,
                                     status="degraded", reason=str(exc)))
            continue
        for dispatch_id in run_ids:
            try:
                outcome = _reconcile_disposition(runtime, payload, records, carrier_ref, dispatch_id)
                if outcome:
                    outcomes.append(outcome)
            except (TaskError, HostError, OSError, ValueError, TypeError, KeyError) as exc:
                outcomes.append(_outcome(str(carrier["project"]), "e2e-after-merge-disposition-unread",
                                         ref=carrier_ref, status="degraded", reason=str(exc)))
    outcomes += _recover_pending(runtime, payload, records, cards)
    for project in sorted(queues(payload)):
        try:
            outcomes += _advance(runtime, payload, records, project)
        except (TaskError, HostError, OSError, ValueError, TypeError, KeyError) as exc:
            outcomes.append(
                _outcome(
                    project,
                    "e2e-after-merge-failed",
                    status="degraded",
                    reason=f"{type(exc).__name__}: {exc}",
                )
            )
        queue = queues(payload).get(project)
        if isinstance(queue, dict) and not any(
            queue.get(key) for key in ("pending", "run", "budget_waits", "cleanup")
        ):
            del queues(payload)[project]
            runtime.save_records(payload, records)
    return outcomes


def _advance(
    runtime: Any, payload: dict[str, Any], records: dict[str, Any], project: str
) -> list[dict[str, Any]]:
    queue = _queue(payload, project)
    outcomes = _progress(runtime, payload, records, project, queue)
    # Last: every dispatcher-owned branch whose run was acted on, this tick's included.
    return outcomes + _cleanup_refs(runtime, payload, records, project, queue)


def _progress(
    runtime: Any, payload: dict[str, Any], records: dict[str, Any], project: str, queue: dict[str, Any]
) -> list[dict[str, Any]]:
    outcomes: list[dict[str, Any]] = []
    _mark_queued(runtime, payload, records, queue)
    if queue["run"]:
        settled, done = _settle(runtime, payload, records, project, queue)
        if settled is not None:
            outcomes.append(settled)
        if not done:
            return outcomes
        _mark_queued(runtime, payload, records, queue)
    declined = _standing_declines(runtime, payload, records, project, queue)
    if declined is not None:
        outcomes.append(declined)
    if queue["budget_waits"]:
        held = _budget_recheck(runtime, payload, records, project, queue)
        if held is not None:
            return [*outcomes, held]
    if queue["pending"]:
        started = _start(runtime, payload, records, project, queue)
        if started is not None:
            outcomes.append(started)
    return outcomes


def _mark_queued(
    runtime: Any, payload: dict[str, Any], records: dict[str, Any], queue: dict[str, Any]
) -> None:
    """Every newly queued card shows it is pending; a card that is not a code card leaves the queue."""
    changed = False
    for entry in list(queue["pending"]):
        if entry.get("marked"):
            # A crash may lose the queue save after the durable disposition link.
            previous = e2e_record.e2e_state(runtime.reader.show(str(entry["ref"]))).after_merge
            if previous is not None and (previous.merge_sha != entry["merge_sha"]
                                         or previous.state not in {AM_PENDING, AM_BUDGET_WAIT}):
                queue["pending"].remove(entry)
                changed = True
            continue
        task = runtime.reader.show(str(entry["ref"]))
        if str(task.get("type") or "code") != "code":
            queue["pending"].remove(entry)
            changed = True
            continue
        state = e2e_record.e2e_state(task)
        previous = state.after_merge
        state.after_merge = AfterMergeMark(
            merge_sha=str(entry["merge_sha"]),
            state=AM_PENDING,
            charged=list(previous.charged) if previous is not None else [],
        )
        _persist(runtime, str(entry["ref"]), state)
        entry["marked"] = True
        changed = True
    if changed:
        runtime.save_records(payload, records)


def _reconcile_disposition(runtime: Any, payload: dict[str, Any], records: dict[str, Any],
                           carrier_ref: str, dispatch_id: str) -> dict[str, Any] | None:
    """All producers converge here: uncertain ends, closing recovery and 0024 routes."""
    carrier = runtime.reader.show(carrier_ref)
    project = str(carrier["project"])
    state = e2e_record.e2e_state(carrier)
    run = state.after_merge_run(dispatch_id)
    if run is None:
        return None
    if not run.acted:
        queue = _queue(payload, project)
        if not queue["run"]:
            queue["run"] = {"carrier": carrier_ref, "dispatch_id": dispatch_id, "sha": run.sha,
                "entries": [{**item, "repo": run.repo, "sprint": str(carrier.get("sprint") or ""),
                             "merged_at": 0, "marked": True} for item in run.covered]}
            runtime.save_records(payload, records)
    elif run.git_ref and run.git_ref_state != "deleted":
        queue = _queue(payload, project)
        if not any(item["dispatch_id"] == dispatch_id for item in queue["cleanup"]):
            queue["cleanup"].append({"carrier": carrier_ref, "dispatch_id": dispatch_id,
                                    "repo": run.repo, "ref": run.git_ref})
            runtime.save_records(payload, records)
    if not run.disposition:
        if run.resolution == AM_RED and run.hotfix:
            hotfix = runtime.reader.show(run.hotfix)
            if (not hotfix.get("sprint") and origin_field.po_origin(hotfix) is None
                    and hotfix.get("state") != "done" and not hotfix.get("closed")):
                _red(runtime, project, carrier_ref, state, run)
        elif (run.acted or run.closing or run.result) and run.resolution in {AM_REQUEUED, AM_BLOCKED} and not _safe_retry(run):
            run.disposition = _disposition(runtime, project, carrier_ref, run,
                "Resolve the unconfirmed after-merge result before any further paid run. "
                "Investigate the prior effect and record retry, decline or concrete planned follow-up.")
            _persist_run(runtime, carrier_ref, state, run)
    if not run.disposition:
        return None
    effect = runtime.writer.reconcile_after_merge_disposition(role="dispatcher", actor=runtime.owner,
        carrier=carrier_ref, dispatch_id=dispatch_id)
    changed = effect.pop("changed")
    # Board receipt and marks commit first. Save failures/deleted queues recover
    # from the same pending marks next pass, without a second completion or charge.
    publication = {key: value for key, value in effect.items() if key != "covered"}
    for item in run.covered:
        ref = item["ref"]
        runtime.writer.comment(role="dispatcher", actor=runtime.owner, reference=ref,
            body=(f"PO operation {run.disposition} for after-merge run {dispatch_id}: {effect['status']}; {effect['reason']}. "
                  + (f"Live holder: {effect['holder']}." if effect["holder"] else "No wait on the completed operation.")
                  + " Retry, if explicitly selected, still requires normal standing/budget admission."),
            request_id=stage_request_id("e2e-am-disposition-" + ref + "-" + effect["status"],
                                         dispatch_id + "-" + hashlib.sha256(json.dumps(publication, sort_keys=True).encode()).hexdigest()[:16]))
    if not changed:
        return None
    return _outcome(project, "e2e-after-merge-disposition-" + effect["status"], ref=carrier_ref,
                    status="degraded" if effect["status"] == "neutral" else "ok",
                    disposition_action=effect["action"], disposition_status=effect["status"],
                    **{key: value for key, value in effect.items() if key not in {"action", "status"}})


def _recover_pending(runtime: Any, payload: dict[str, Any], records: dict[str, Any],
                     cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pending marks survive queue deletion and committed-transition/save crashes."""
    changed = False
    outcomes = []
    for card in cards:
        try:
            task = runtime.reader.show(card["ref"])
            mark = e2e_record.e2e_state(task).after_merge
            if mark is None or mark.state not in {AM_PENDING, AM_BUDGET_WAIT} or task.get("closed"):
                continue
            project = str(task["project"])
            queue = _queue(payload, project)
            if mark.state == AM_BUDGET_WAIT:
                wait = next((wait for wait in queue["budget_waits"] if wait["decision"] == mark.decision), None)
                if wait is None:
                    wait = _recover_budget_wait(runtime, task, mark)
                    queue["budget_waits"].append(wait)
                    changed = True
                if task["ref"] not in wait["cards"]:
                    wait["cards"].append(task["ref"])
                    changed = True
            existing = [*queue["pending"], *((queue.get("run") or {}).get("entries") or [])]
            if any(entry["ref"] == task["ref"] and entry["merge_sha"] == mark.merge_sha for entry in existing):
                continue
            carried = (e2e_record.e2e_state(runtime.reader.show(mark.carrier)).after_merge_run(mark.dispatch_id)
                       if mark.carrier and mark.dispatch_id else None)
            queue["pending"].append({"ref": task["ref"], "merge_sha": mark.merge_sha,
                "sprint": str(task.get("sprint") or ""), "merged_at": time.time(),
                "repo": carried.repo if carried else "", "base": "main", "marked": True})
            changed = True
        except TaskError as exc:
            outcomes.append(_outcome(str(card.get("project") or ""), "e2e-after-merge-pending-unread",
                ref=card["ref"], status="degraded", reason=f"{exc.code}: {exc.message}"))
    if changed:
        runtime.save_records(payload, records)
    return outcomes


def _recover_budget_wait(runtime: Any, task: dict[str, Any], mark: AfterMergeMark) -> dict[str, Any]:
    """Recover the released budget generation from its actual dispatcher create ID."""
    decision = runtime.reader.show(mark.decision)
    for event in runtime.audit.events(mark.decision, kind="created"):
        if (event.get("actor") or {}).get("role") != "dispatcher":
            continue
        request = str(event.get("request_id") or "")
        spent = e2e_budget.batch_decision_cards(request)
        if spent:
            return {"decision": mark.decision, "generation": int(request.split("-")[3]),
                    "scope": "cards", "scope_ref": task["ref"], "request_id": request,
                    "spent": spent, "cards": []}
        sprint = str(decision.get("sprint") or "")
        if sprint and request.startswith(e2e_budget.decision_prefix(sprint)):
            return {"decision": mark.decision,
                    "generation": int(request.removeprefix(e2e_budget.decision_prefix(sprint))),
                    "scope": "sprint", "scope_ref": sprint, "cards": []}
    raise TaskError("backend_error", "after-merge budget holder has no supported committed budget create", 1)


def _persist(runtime: Any, ref: str, state: E2eState) -> None:
    runtime.writer.record_e2e_state(role="dispatcher", actor=runtime.owner, reference=ref, state=state.text())


def _persist_run(runtime: Any, ref: str, state: E2eState, run: E2eRun) -> None:
    current = runtime.writer.record_after_merge_run(role="dispatcher", actor=runtime.owner,
                                                   reference=ref, run=run)
    state.after_merge = current.after_merge


def _remark(runtime: Any, ref: str, carrier_ref: str, carrier_state: E2eState,
            *, run: E2eRun | None = None, **changes: Any) -> None:
    """Rewrite one covered card's mark; the carrier's goes into the state that also holds the run."""
    if run is not None:
        current = runtime.writer.record_after_merge_mark(role="dispatcher", actor=runtime.owner,
            reference=ref, carrier=carrier_ref, run=run, changes=changes)
        if ref == carrier_ref:
            carrier_state.after_merge = current.after_merge
        return
    if ref == carrier_ref:
        state = carrier_state
    else:
        state = e2e_record.e2e_state(runtime.reader.show(ref))
    mark = state.after_merge or AfterMergeMark(merge_sha=str(changes.get("merge_sha") or ""))
    for name, value in changes.items():
        setattr(mark, name, value)
    state.after_merge = mark
    _persist(runtime, ref, state)


def _sprint_open(runtime: Any, sprint: str) -> bool:
    if not sprint:
        return False
    try:
        return str(runtime.sprints.show(sprint, include_cards=False).get("status") or "") == "open"
    except TaskError as exc:
        if exc.code == "not_found":
            return False
        raise


def _repo(runtime: Any, project: str, entries: list[dict[str, Any]]) -> str:
    for entry in entries:
        if entry.get("repo"):
            return str(entry["repo"])
    from ummanu.dispatch.post_merge import _repo_dir

    repo_dir = _repo_dir(runtime, project)
    if repo_dir is None:
        raise HostError(f"project {project} has no registered checkout to name its repository from")
    return _name_with_owner(runtime.host, str(repo_dir))


# --- starting a run ---------------------------------------------------------------------------------


def _start(
    runtime: Any, payload: dict[str, Any], records: dict[str, Any], project: str, queue: dict[str, Any]
) -> dict[str, Any] | None:
    """Pick the target and the covered cards, charge and write the intent, then dispatch once."""
    try:
        declaration = declared_e2e(runtime.host, project)
    except HostError as exc:
        return _outcome(
            project,
            "e2e-after-merge-declaration-unreadable",
            status="degraded",
            reason=scrub_host_output(str(exc)),
        )
    if declaration is None or not declaration.after_merge:
        return _decline_all(
            runtime,
            payload,
            records,
            project,
            queue,
            "the project's adapter no longer declares an after-merge e2e stage, so no run covers it",
        )
    pending = sorted(queue["pending"], key=lambda entry: float(entry.get("merged_at") or 0))
    target = pending[-1]
    try:
        repo = _repo(runtime, project, pending)
        # Reconstructed queues need not retain merge timestamps. Select the
        # newest descendant by current GitHub ancestry, then compute coverage.
        for candidate in pending:
            if is_ancestor(runtime.host, repo, str(target["merge_sha"]), str(candidate["merge_sha"])):
                target = candidate
        covered = [
            entry
            for entry in pending
            if is_ancestor(runtime.host, repo, str(entry["merge_sha"]), str(target["merge_sha"]))
        ]
        # The actual target is also the budget/question carrier. Reconstructed
        # queue timestamps need not be in ancestry order.
        covered = [entry for entry in covered if entry is not target] + [target]
    except HostError as exc:
        # Nothing is written and nothing dispatched yet: the next tick asks again.
        return _outcome(
            project,
            "e2e-after-merge-picking",
            status="degraded",
            reason=f"the target could not be chosen: {scrub_host_output(str(exc))}",
        )
    carrier_ref = str(target["ref"])
    tasks = {str(entry["ref"]): runtime.reader.show(str(entry["ref"])) for entry in covered}
    states = {ref: e2e_record.e2e_state(task) for ref, task in tasks.items()}
    carrier_state = states[carrier_ref]
    sprint = str(tasks[carrier_ref].get("sprint") or "")
    sprint = sprint if _sprint_open(runtime, sprint) else ""
    sha = str(target["merge_sha"])
    dispatch_id = (
        f"{carrier_ref}{DISPATCH_INFIX}{len(carrier_state.after_merge_runs) + 1}-{secrets.token_hex(4)}"
    )
    git_ref = AFTER_MERGE_REF_PREFIX + dispatch_id
    run = E2eRun(
        dispatch_id=dispatch_id,
        sha=sha,
        repo=repo,
        branch=git_ref,
        workflow=declaration.workflow,
        intent_at=wait_card.utc_text(utcnow()),
        deadline=declaration.deadline,
        placement=AFTER_MERGE,
        covered=[{"ref": str(entry["ref"]), "merge_sha": str(entry["merge_sha"])} for entry in covered],
        git_ref=git_ref,
        charged_to=sprint or "cards",
    )
    for entry in covered:
        ref = str(entry["ref"])
        previous = states[ref].after_merge
        charged = list(previous.charged) if previous is not None else []
        states[ref].after_merge = AfterMergeMark(
            merge_sha=str(entry["merge_sha"]),
            state=AM_COVERED,
            dispatch_id=dispatch_id,
            carrier=carrier_ref,
            charged=charged if sprint else [*charged, dispatch_id],
        )
    carrier_state.after_merge_runs.append(run)
    # The run is in flight in the production state before its intent is on the board: a dispatcher
    # that dies in between finds no intent on the carrier and puts the cards back.
    queue["run"] = {"carrier": carrier_ref, "dispatch_id": dispatch_id, "sha": sha, "entries": covered}
    queue["pending"] = [entry for entry in queue["pending"] if entry not in covered]
    runtime.save_records(payload, records)
    charged = runtime.writer.record_after_merge_intent(
        role="dispatcher",
        actor=runtime.owner,
        states={ref: state.text() for ref, state in states.items()},
        sprint=sprint,
        dispatch_id=dispatch_id,
        carrier=carrier_ref,
    )
    if not charged.get("charged"):
        queue["pending"] = [*covered, *queue["pending"]]
        queue["run"] = None
        runtime.save_records(payload, records)
        if charged.get("stale"):
            _mark_queued(runtime, payload, records, queue)
            return _outcome(project, "e2e-after-merge-admission-changed", status="degraded",
                            reason="Covered marks changed before atomic intent; no run was charged or dispatched")
        return _budget_spent(runtime, payload, records, project, queue, tasks, covered, sprint, charged)
    _push_and_dispatch(runtime, carrier_ref, carrier_state, run, declaration)
    settled, _done = _settle(runtime, payload, records, project, queue)
    return settled or _outcome(project, "e2e-after-merge-dispatching", ref=carrier_ref, sha=sha)


def _push_and_dispatch(
    runtime: Any, carrier_ref: str, state: E2eState, run: E2eRun, declaration: E2eDeclaration
) -> None:
    """Point the dispatcher-owned branch at the target, then dispatch on it: once, in the intent's tick."""
    try:
        create_ref(runtime.host, run.repo, run.git_ref, run.sha)
    except HostError as exc:
        run.dispatch = REFUSED
        run.dispatch_detail = safe_one_line(scrub_host_output(str(exc)), limit=1000)
        _close(
            run,
            f"The branch `{run.git_ref}` could not be pointed at `{run.sha[:12]}`: {run.dispatch_detail}. "
            "Nothing was dispatched.",
        )
        _persist_run(runtime, carrier_ref, state, run)
        return
    run.git_ref_state = "created"
    _persist_run(runtime, carrier_ref, state, run)
    try:
        dispatched = dispatch_workflow(
            runtime.host, run.repo, declaration, branch=run.git_ref, dispatch_id=run.dispatch_id, sha=run.sha
        )
    except DispatchRefused as exc:
        run.dispatch = REFUSED
        run.dispatch_detail = safe_one_line(scrub_host_output(str(exc)), limit=1000)
        _close(
            run,
            f"The e2e workflow `{run.workflow}` could not be dispatched on `{run.git_ref}` @ `{run.sha[:12]}`: "
            f"{run.dispatch_detail}. No run started.",
        )
    except HostError as exc:
        # No answer, or a rate limit: the run is looked up from here on; never dispatched again.
        run.dispatch_detail = f"unconfirmed: {safe_one_line(scrub_host_output(str(exc)), limit=500)}"
    else:
        run.dispatch = SENT
        if dispatched.run_id:
            run.run_id = dispatched.run_id
            run.run_url = f"https://github.com/{run.repo}/actions/runs/{dispatched.run_id}"
            run.identified_by = IDENTIFIED_BY_ANSWER
    _persist_run(runtime, carrier_ref, state, run)


def _close(run: E2eRun, reason: str) -> None:
    """The run is not attached to the covered cards: they go back to the pending set."""
    run.closing = stage_request_id("e2e-am-closed", run.dispatch_id)
    run.closing_reason = reason


# --- a run in flight --------------------------------------------------------------------------------


def _settle(
    runtime: Any, payload: dict[str, Any], records: dict[str, Any], project: str, queue: dict[str, Any]
) -> tuple[dict[str, Any] | None, bool]:
    """Take the run in flight as far as it goes this tick: `(outcome, done)`; done once it is acted on
    and the project may start its next run."""
    inflight = queue["run"]
    carrier_ref = str(inflight["carrier"])
    entries = list(inflight.get("entries") or [])
    state = e2e_record.e2e_state(runtime.reader.show(carrier_ref))
    run = state.after_merge_run(str(inflight["dispatch_id"]))
    if run is None:
        # The intent never reached the board: nothing was charged or dispatched; the cards wait again.
        queue["pending"] = [*entries, *queue["pending"]]
        queue["run"] = None
        runtime.save_records(payload, records)
        return None, True
    if not run.acted:
        if not run.closing and run.result is None:
            if not run.run_id or not run.head_sha:
                pending = _identify(runtime, project, carrier_ref, state, run)
                if pending is not None:
                    return pending, False
            if not run.closing and not run.wait_ref:
                try:
                    _create_wait(runtime, project, carrier_ref, state, run)
                except TaskError as exc:
                    _close(
                        run,
                        f"The run {run.run_url} was dispatched, but its wait card could not be created: "
                        f"{exc.code}: {exc.message}",
                    )
                    _persist_run(runtime, carrier_ref, state, run)
            if not run.closing:
                try:
                    wait = runtime.reader.show(run.wait_ref)
                except TaskError as exc:
                    if exc.code != "not_found":
                        raise
                    _close(run, f"The wait card {run.wait_ref} of the run {run.run_url} no longer exists.")
                    _persist_run(runtime, carrier_ref, state, run)
                else:
                    result = wait_card.wait_state(wait).result
                    if result is None:
                        view = wait_card.wait_view(wait) or {}
                        return (
                            _outcome(
                                project,
                                "e2e-after-merge-waiting",
                                ref=carrier_ref,
                                sha=run.sha,
                                run=run.run_url,
                                wait_card=run.wait_ref,
                                deadline=view.get("deadline"),
                                covered=[item["ref"] for item in run.covered],
                            ),
                            False,
                        )
                    fact = result.get("fact") if isinstance(result.get("fact"), dict) else {}
                    run.result = {
                        "outcome": str(result.get("outcome") or ""),
                        "conclusion": str(fact.get("conclusion") or ""),
                        "summary": str(result.get("summary") or ""),
                        "evidence": str(result.get("evidence") or run.run_url),
                        "key": str(result.get("key") or ""),
                    }
                    _persist_run(runtime, carrier_ref, state, run)
        acted = _act(runtime, project, carrier_ref, state, run, entries, queue)
    else:
        # Acted on before a crash lost the queue's save: a requeue puts its cards back once more.
        if run.resolution in {AM_REQUEUED, AM_BLOCKED} and not run.disposition:
            known = {str(entry.get("ref")) for entry in queue["pending"]}
            queue["pending"] = [
                *({**entry, "marked": True} for entry in entries if str(entry.get("ref")) not in known),
                *queue["pending"],
            ]
        acted = _outcome(
            project, "e2e-after-merge-" + (run.resolution or "acted"), ref=carrier_ref, sha=run.sha
        )
    if run.git_ref_state != "deleted" and not any(
        item.get("dispatch_id") == run.dispatch_id for item in queue["cleanup"]
    ):
        queue["cleanup"].append(
            {"repo": run.repo, "ref": run.git_ref, "dispatch_id": run.dispatch_id, "carrier": carrier_ref}
        )
    queue["run"] = None
    runtime.save_records(payload, records)
    return acted, True


def _identify(
    runtime: Any, project: str, carrier_ref: str, state: E2eState, run: E2eRun
) -> dict[str, Any] | None:
    """Name the run and check its SHA, by the before-merge stage's rules; None once both are on record
    (or the run is closed), else the tick's outcome."""
    intent = wait_card.parse_utc(run.intent_at, "intent_at")
    error = ""
    head_sha = ""
    try:
        if run.run_id:
            head_sha = run_head_sha(runtime.host, run.repo, run.run_id)
        else:
            settle_at = intent + timedelta(seconds=E2E_CLOCK_MARGIN_SECONDS + E2E_RECOVERY_SETTLE_SECONDS)
            if utcnow() < settle_at:
                return _outcome(
                    project,
                    "e2e-after-merge-identifying",
                    ref=carrier_ref,
                    sha=run.sha,
                    dispatch_id=run.dispatch_id,
                    recovery_settles_at=wait_card.utc_text(settle_at),
                )
            declaration = declared_e2e(runtime.host, project)
            titled = bool(declaration is not None and declaration.dispatch_id_input)
            found = matching_runs(
                runtime.host,
                run.repo,
                run.workflow,
                branch=run.git_ref,
                sha=run.sha,
                since=intent,
                dispatch_id=run.dispatch_id if titled else "",
            )
            if len(found) > 1:
                listed = ", ".join(
                    f"{candidate.get('html_url') or candidate['id']} (created {candidate.get('created_at')})"
                    for candidate in sorted(found, key=lambda candidate: int(candidate["id"]))
                )
                _close(
                    run,
                    f"The after-merge e2e run dispatched at {run.intent_at} on `{run.git_ref}` cannot be told "
                    f"apart: {len(found)} `{run.workflow}` workflow_dispatch runs at `{run.sha[:12]}` were "
                    f"created since: {listed}. None is taken as its result, and nothing is dispatched again.",
                )
                _persist_run(runtime, carrier_ref, state, run)
                return None
            if found:
                run.run_id = int(found[0]["id"])
                run.run_url = f"https://github.com/{run.repo}/actions/runs/{run.run_id}"
                run.identified_by = IDENTIFIED_BY_RECOVERY
                run.recovery_rule = (
                    f"the only workflow_dispatch run of {run.workflow} on {run.git_ref} at {run.sha} created "
                    f"at or after {wait_card.utc_text(intent - timedelta(seconds=E2E_CLOCK_MARGIN_SECONDS))}"
                    + (f", its title carrying {run.dispatch_id}" if titled else "")
                    + f", looked up at {wait_card.utc_text(utcnow())}"
                )
                head_sha = str(found[0].get("head_sha") or "")
                _persist_run(runtime, carrier_ref, state, run)
                runtime.writer.comment(
                    role="dispatcher",
                    actor=runtime.owner,
                    reference=carrier_ref,
                    body=(
                        f"After-merge e2e run {run.run_url} was identified by recovery, not by GitHub's "
                        f"dispatch answer (that answer was lost): {run.recovery_rule}."
                    ),
                    request_id=stage_request_id("e2e-am-recovered", run.dispatch_id),
                )
    except HostError as exc:
        head_sha, error = "", safe_one_line(scrub_host_output(str(exc)), limit=500)
    if not head_sha:
        window_end = intent + timedelta(seconds=E2E_IDENTIFY_SECONDS)
        if utcnow() < window_end:
            return _outcome(
                project,
                "e2e-after-merge-identifying",
                ref=carrier_ref,
                sha=run.sha,
                dispatch_id=run.dispatch_id,
                **({"run": run.run_url} if run.run_url else {}),
                **({"error": error} if error else {}),
            )
        what = (
            f"its run {run.run_url} could not be read"
            if run.run_id
            else f"no `{run.workflow}` workflow_dispatch run on `{run.git_ref}` at that SHA was found"
        )
        _close(
            run,
            f"The after-merge e2e run dispatched at {run.intent_at} for `{run.sha[:12]}` (dispatch id "
            f"`{run.dispatch_id}`) could not be identified: {what} within {E2E_IDENTIFY_SECONDS // 60} minutes"
            + (f" ({run.dispatch_detail})" if run.dispatch_detail else "")
            + (f"; last error: {error}" if error else "")
            + ". Nothing was dispatched a second time.",
        )
        _persist_run(runtime, carrier_ref, state, run)
        return None
    run.head_sha = head_sha
    if head_sha != run.sha:
        _close(
            run,
            f"The after-merge e2e run {run.run_url} ran on `{head_sha[:12]}`, not on the target `{run.sha[:12]}`: "
            f"`{run.git_ref}` moved between the dispatch and the run. It is not attached to the covered cards.",
        )
    else:
        for item in run.covered:
            _remark(runtime, item["ref"], carrier_ref, state, run=run, run_url=run.run_url)
    _persist_run(runtime, carrier_ref, state, run)
    return None


def _create_wait(runtime: Any, project: str, carrier_ref: str, state: E2eState, run: E2eRun) -> None:
    """The wait card for this run, created once: its request id is derived from the dispatch id."""
    sprint = run.charged_to if run.charged_to != "cards" and _sprint_open(runtime, run.charged_to) else ""
    covered = ", ".join(item["ref"] for item in run.covered)
    created = runtime.writer.create(
        role="dispatcher",
        actor=runtime.owner,
        project=project,
        task_type="wait",
        title=f"E2E after merge: {run.workflow} on {project} @ {run.sha[:12]}",
        description=(
            f"The dispatcher waits here for the after-merge `{run.workflow}` run it dispatched on "
            f"`{run.git_ref}` @ `{run.sha}` (dispatch id `{run.dispatch_id}`), covering {covered}. Its result "
            f"returns to {carrier_ref}, the newest covered card, whose e2e record names this card."
        ),
        target="ready",
        sprint=sprint,
        wait={"run": run.run_url, "deadline": run.deadline, "returns": [wait_card.CARD_PREFIX + carrier_ref]},
        request_id=stage_request_id("e2e-wait", run.dispatch_id),
    )
    run.wait_ref = str(created["task"]["ref"])
    _persist_run(runtime, carrier_ref, state, run)


# --- outcomes -----------------------------------------------------------------------------------------


def _covered_lines(run: E2eRun) -> list[str]:
    return [f"- {item['ref']}: merged as `{item['merge_sha']}`" for item in run.covered]


def _act(
    runtime: Any,
    project: str,
    carrier_ref: str,
    state: E2eState,
    run: E2eRun,
    entries: list[dict[str, Any]],
    queue: dict[str, Any],
) -> dict[str, Any]:
    """What the run's end does to the covered cards; the resolution is recorded before any effect, and
    every effect is idempotent, so a replay after a crash repeats them under the same ids."""
    result = run.result or {}
    if not run.resolution:
        if run.closing:
            run.resolution = AM_BLOCKED
        elif result.get("outcome") == wait_card.TARGET_REACHED and run.conclusion == SUCCESS:
            run.resolution = AM_GREEN
        elif result.get("outcome") == wait_card.TARGET_REACHED and run.conclusion == FAILURE:
            run.resolution = AM_RED
            summary, log, _fingerprint = _red_evidence(runtime, run)
            # The evidence is frozen on the run, so a replayed hotfix create carries the same words.
            run.closing_reason = _hotfix_description(run, summary, log)
        else:
            run.resolution = AM_REQUEUED
        _persist_run(runtime, carrier_ref, state, run)
    if run.resolution == AM_GREEN:
        _green(runtime, carrier_ref, state, run)
    elif run.resolution == AM_RED:
        _red(runtime, project, carrier_ref, state, run)
    else:
        _requeue(runtime, carrier_ref, state, run, entries, queue)
    run.acted = True
    _persist_run(runtime, carrier_ref, state, run)
    return _outcome(
        project,
        "e2e-after-merge-" + run.resolution,
        ref=carrier_ref,
        sha=run.sha,
        run=run.run_url,
        covered=[item["ref"] for item in run.covered],
        **({"hotfix": run.hotfix} if run.hotfix else {}),
    )


def _green(runtime: Any, carrier_ref: str, state: E2eState, run: E2eRun) -> None:
    covered = ", ".join(item["ref"] for item in run.covered)
    for item in run.covered:
        runtime.writer.comment(
            role="dispatcher",
            actor=runtime.owner,
            reference=item["ref"],
            body="\n".join(
                [
                    "## E2E after merge — green",
                    "",
                    (
                        f"The e2e workflow `{run.workflow}` run {run.run_url} concluded success on `main` @ "
                        f"`{run.sha}` (dispatched on `{run.git_ref}`, wait card {run.wait_ref}). It covers "
                        f"{covered}; this card merged as `{item['merge_sha']}`."
                    ),
                    "",
                    "Covered cards:",
                    *_covered_lines(run),
                ]
            ),
            request_id=stage_request_id("e2e-am-green-" + item["ref"], run.dispatch_id),
        )
        _remark(runtime, item["ref"], carrier_ref, state, run=run, state=AM_GREEN, run_url=run.run_url, note="")


def _hotfix_description(run: E2eRun, summary: str, log: str) -> str:
    text = "\n".join(
        [
            f"The after-merge e2e run of `{run.workflow}` on `main` concluded **{run.conclusion}**: {summary}.",
            "",
            f"- run: {run.run_url}",
            f"- conclusion: {run.conclusion}",
            f"- SHA: `{run.sha}` (dispatched on `{run.git_ref}`, dispatch id `{run.dispatch_id}`)",
            f"- wait card: {run.wait_ref}",
            "",
            "## Covered cards",
            "",
            (
                "Every card merged since the last after-merge run, with its merge commit; the failure is "
                "attributed to all of them:"
            ),
            "",
            *_covered_lines(run),
            "",
            "## Log (`gh run view --log-failed`, bounded)",
            "",
            "```",
            log,
            "```",
            "",
            "## What to do",
            "",
            (
                "Find which covered change broke the e2e run and fix it on `main`. The next after-merge run "
                "covers this hotfix once it merges."
            ),
        ]
    )
    return text if len(text) <= _EVIDENCE_LIMIT else text[: _EVIDENCE_LIMIT - 1] + "…"


def _red(runtime: Any, project: str, carrier_ref: str, state: E2eState, run: E2eRun) -> None:
    """One `code` hotfix card per run, then each covered card names it."""
    if not run.hotfix:
        run.hotfix = _hotfix(runtime, project, carrier_ref, run)
        _persist_run(runtime, carrier_ref, state, run)
    hotfix = runtime.reader.show(run.hotfix)
    if (not hotfix.get("sprint") and origin_field.po_origin(hotfix) is None
            and hotfix.get("state") != "done" and not hotfix.get("closed")):
        run.disposition = _disposition(runtime, project, carrier_ref, run,
            f"Assign the return route and disposition of unowned hotfix {run.hotfix}. "
            "Inspect the run evidence, then create planned follow-up work through an appropriate "
            "open sprint or a real PO turn, or document why no correction is needed. "
            "Do not fabricate a PO origin or rewrite a closed sprint.")
        _persist_run(runtime, carrier_ref, state, run)
        runtime.writer.comment(role="dispatcher", actor=runtime.owner, reference=run.hotfix,
            body=f"PO operation {run.disposition} owns this hotfix's return-route assignment and disposition.",
            request_id=stage_request_id("e2e-am-hotfix-route", run.dispatch_id))
    for item in run.covered:
        runtime.writer.comment(
            role="dispatcher",
            actor=runtime.owner,
            reference=item["ref"],
            body=(
                f"## E2E after merge — red\n\nThe e2e workflow `{run.workflow}` run {run.run_url} concluded "
                f"{run.conclusion} on `main` @ `{run.sha}`, a run covering this card (merged as "
                f"`{item['merge_sha']}`). The hotfix card is {run.hotfix}.\n\nCovered cards:\n"
                + "\n".join(_covered_lines(run))
            ),
            request_id=stage_request_id("e2e-am-red-" + item["ref"], run.dispatch_id),
        )
        _remark(
            runtime, item["ref"], carrier_ref, state, run=run, state=AM_RED, hotfix=run.hotfix,
            decision=run.disposition, run_url=run.run_url
        )


def _hotfix(runtime: Any, project: str, carrier_ref: str, run: E2eRun) -> str:
    """Create the run's one hotfix card, owned by the carrier's open sprint, else its PO origin, else
    an explicit PO return-route operation (code stays Blocked). A committed create is read back."""
    request_id = stage_request_id("e2e-am-hotfix", run.dispatch_id)
    known = runtime.audit.committed_event(request_id)
    carrier = runtime.reader.show(carrier_ref)
    sprint = str(carrier.get("sprint") or "")
    if known is not None and known.get("ref"):
        hotfix = str(known["ref"])
        payload = known.get("payload") if isinstance(known.get("payload"), dict) else {}
        owned = bool(payload.get("sprint") or payload.get("po_origin"))
    else:
        sprint = sprint if _sprint_open(runtime, sprint) else ""
        origin = None if sprint else origin_field.po_origin(carrier)
        created = runtime.writer.create(
            role="dispatcher",
            actor=runtime.owner,
            project=project,
            task_type="code",
            title=f"Hotfix: after-merge e2e red on main @ {run.sha[:12]} ({run.workflow})",
            description=run.closing_reason,
            target="ready",
            sprint=sprint,
            budget_event="hotfix" if sprint else "",
            origin=origin,
            request_id=request_id,
        )
        hotfix = str(created["task"]["ref"])
        owned = bool(sprint or origin)
    if not owned:
        text = (
            f"{UNOWNED_HOTFIX_REASON}: the after-merge e2e run {run.run_url} concluded {run.conclusion} on "
            f"`main` @ `{run.sha[:12]}`, covering {', '.join(item['ref'] for item in run.covered)}. The hotfix "
            f"card {hotfix} has no open sprint and no PO session to go to; its return route needs assignment."
        )
        if runtime.reader.show(hotfix).get("state") == "ready":
            runtime.writer.move(
                role="dispatcher",
                actor=runtime.owner,
                reference=hotfix,
                target="blocked",
                reason=text,
                request_id=stage_request_id("e2e-am-hotfix-blocked", run.dispatch_id),
                terminal_taxonomy=normalize_terminal_taxonomy(
                    disposition="blocked", blocked_reason="other"
                ).to_record(),
            )
    return hotfix


def _disposition(runtime: Any, project: str, carrier_ref: str, run: E2eRun, action: str) -> str:
    """One PO operation per run, including committed-but-not-linked recovery."""
    request_id = stage_request_id("e2e-am-disposition", run.dispatch_id)
    known = runtime.audit.committed_event(request_id)
    if known is not None and known.get("ref"):
        return str(known["ref"])
    carrier = runtime.reader.show(carrier_ref)
    sprint = str(carrier.get("sprint") or "")
    sprint = sprint if _sprint_open(runtime, sprint) else ""
    origin = None if sprint else next((found for ref in [carrier_ref, *(item["ref"] for item in run.covered)]
                                      if (found := origin_field.po_origin(runtime.reader.show(ref))) is not None), None)
    created = runtime.writer.create(role="dispatcher", actor=runtime.owner, project=project,
        task_type="operation", title=f"PO disposition: after-merge e2e {run.dispatch_id}",
        description="\n".join([action, "", "## Evidence", "", f"Run: {run.run_url or run.dispatch_id}",
            f"Target SHA: {run.sha}; wait card: {run.wait_ref or 'none'}; hotfix: {run.hotfix or 'none'}",
            *_covered_lines(run), "", run.closing_reason or str(run.result or {}), "",
            "## Completion", "", ("Use native task complete with What was done and How to verify. Add a plain JSON object "
            "under ## E2E disposition: operation (this card ref), carrier, run, covered (the exact ref/merge_sha "
            "objects below), action (retry, decline, follow_up), evidence (investigation and verification). "
            "Retry additionally needs prior_effect: not_started or finished, attesting investigation resolved the "
            "prior uncertain effect. Follow_up additionally needs holder: an actual planned card in an open sprint "
            "or from a genuine PO turn; its Done settles this disposition. Other fields are refused by the consumer. "
            "Bare Done/free prose does not retry. A missing/malformed disposition needs PO reopening and corrected "
            "native completion of this same operation. "
            "Apply effective standing decisions before spending. This assignment grants no monetary or "
            "production authority. Use task handover --to owner only for a new uncovered owner decision."),
            "", "Bound identity (fill operation with this card's actual ref):", "",
            json.dumps({"operation": "<this operation>", "carrier": carrier_ref, "run": run.dispatch_id,
                        "covered": run.covered}, sort_keys=True)]),
        target="ready", sprint=sprint, origin=origin, touches_production="none",
        **({"po_execution": po_execution.create_assignment(request_id, "e2e_disposition",
                                                           [item["ref"] for item in run.covered])}
           if not sprint and origin is None else {}), request_id=request_id)
    return str(created["task"]["ref"])


def _bell(runtime: Any, subject: str, text: str, key: str) -> None:
    owner_events.record(
        owner_events.E2E_AFTER_MERGE,
        subject,
        text,
        key,
        to=getattr(getattr(runtime, "reader", None), "client", None),
    )


def _requeue(
    runtime: Any,
    carrier_ref: str,
    state: E2eState,
    run: E2eRun,
    entries: list[dict[str, Any]],
    queue: dict[str, Any],
) -> None:
    """No hotfix: planned waits retry through budget admission; uncertain outcomes belong to the PO."""
    result = run.result or {}
    if run.closing:
        what = run.closing_reason
    elif result.get("outcome") == wait_card.TARGET_REACHED:
        what = (
            f"The after-merge e2e run {run.run_url} on `main` @ `{run.sha[:12]}` concluded "
            f"{run.conclusion or 'with no conclusion'}."
        )
    else:
        what = (
            f"The after-merge e2e run {run.run_url or run.dispatch_id} on `main` @ `{run.sha[:12]}` was not "
            f"waited out: its wait card {run.wait_ref} ended {result.get('outcome')} "
            f"({result.get('summary') or ''})."
        )
    note = safe_one_line(what, limit=500)
    # Released cancellation/time-limit outcomes safely retry under normal budget admission.
    # Unknown dispatch/SHA/source evidence needs PO disposition before another paid attempt.
    retry = _safe_retry(run)
    if not retry:
        run.disposition = _disposition(runtime, str(runtime.reader.show(carrier_ref)["project"]),
            carrier_ref, run, "Resolve the unconfirmed after-merge result before any further paid run. "
            "Investigate the evidence and record a safe disposition, or create follow-up work.")
        _persist_run(runtime, carrier_ref, state, run)
    for item in run.covered:
        runtime.writer.comment(
            role="dispatcher",
            actor=runtime.owner,
            reference=item["ref"],
            body=(
                f"## E2E after merge — {run.resolution}\n\n{what}\n\nThat is not this change's code: no hotfix "
                "card is cut. " + (
                    "The covered cards are pending again; the next run covers them under normal budget admission."
                    if retry else f"PO operation {run.disposition} owns the unresolved disposition; no automatic paid retry."
                ) + "\n\nCovered cards:\n" + "\n".join(_covered_lines(run))
            ),
            request_id=stage_request_id("e2e-am-requeued-" + item["ref"], run.dispatch_id),
        )
        _remark(
            runtime, item["ref"], carrier_ref, state, run=run, state=AM_PENDING if retry else AM_BLOCKED,
            decision=run.disposition, holder=run.disposition or None, note=note, run_url=""
        )
    _bell(
        runtime,
        carrier_ref,
        f"After-merge e2e run of {', '.join(item['ref'] for item in run.covered)} ended without an actionable verdict: {what}",
        f"{owner_events.E2E_AFTER_MERGE}:{run.dispatch_id}",
    )
    if not retry:
        covered = {item["ref"] for item in run.covered}
        queue["pending"] = [entry for entry in queue["pending"] if entry["ref"] not in covered]
        return
    known = {str(entry.get("ref")) for entry in queue["pending"]}
    queue["pending"] = [
        *({**entry, "marked": True} for entry in entries if str(entry.get("ref")) not in known),
        *queue["pending"],
    ]


def _safe_retry(run: E2eRun) -> bool:
    from ummanu.board.e2e_disposition import safe_retry
    return safe_retry(run)


def _cleanup_refs(
    runtime: Any, payload: dict[str, Any], records: dict[str, Any], project: str, queue: dict[str, Any]
) -> list[dict[str, Any]]:
    """Delete every dispatcher-owned branch whose run is over. An entry leaves `cleanup` only once the
    branch is deleted or confirmed absent (`e2e.delete_ref`); every other answer is asked again next pass."""
    outcomes: list[dict[str, Any]] = []
    for item in list(queue["cleanup"]):
        try:
            delete_ref(runtime.host, str(item["repo"]), str(item["ref"]))
        except HostError as exc:
            outcomes.append(
                _outcome(
                    project,
                    "e2e-after-merge-ref-cleanup",
                    ref=str(item.get("carrier") or ""),
                    status="degraded",
                    reason=f"branch {item['ref']} not deleted yet: {scrub_host_output(str(exc))}",
                )
            )
            continue
        carrier = str(item.get("carrier") or "")
        if carrier:
            state = e2e_record.e2e_state(runtime.reader.show(carrier))
            run = state.after_merge_run(str(item.get("dispatch_id") or ""))
            if run is not None and run.git_ref_state != "deleted":
                run.git_ref_state = "deleted"
                _persist_run(runtime, carrier, state, run)
        queue["cleanup"].remove(item)
        runtime.save_records(payload, records)
    return outcomes


# --- the budget -----------------------------------------------------------------------------------------


def _budget_spent(
    runtime: Any,
    payload: dict[str, Any],
    records: dict[str, Any],
    project: str,
    queue: dict[str, Any],
    tasks: dict[str, dict[str, Any]],
    covered: list[dict[str, Any]],
    sprint: str,
    charged: dict[str, Any],
) -> dict[str, Any]:
    """Nothing left to charge the run to: the decision card(s), the marks, and the cards wait."""
    by_ref = {str(entry["ref"]): entry for entry in covered}
    # `covered` is in merge order: its last card is the target's, the carrier.
    carrier_ref = str(covered[-1]["ref"])
    waiting = [
        (
            ref,
            str(tasks[ref].get("title") or ""),
            f"`{by_ref[ref]['merge_sha']}` (its merge; the after-merge e2e run)",
        )
        for ref in by_ref
    ]
    waits: list[dict[str, Any]] = []
    if sprint:
        refusal = charged.get("refusal")
        if refusal is not None:
            text = owner_decisions.refusal_text(sprint, refusal)
            for ref in by_ref:
                _decline(runtime, queue, ref, text, decision=f"{sprint}/{refusal['id']}")
            runtime.save_records(payload, records)
            return _outcome(project, "e2e-after-merge-declined", covered=list(by_ref), reason=text)
        budget = int(charged.get("budget") or 0)
        decision = _decision_card(
            runtime,
            tasks[carrier_ref],
            e2e_record.e2e_state(tasks[carrier_ref]),
            str(by_ref[carrier_ref]["merge_sha"]),
            scope="sprint",
            scope_ref=sprint,
            generation=budget,
            spent_line=f"The e2e run budget of {sprint} is spent: {int(charged.get('used') or 0)} of {budget} runs.",
            charges=[item for item in charged.get("charges") or [] if isinstance(item, dict)],
            origin=None,
            waiting=waiting,
        )
        waits.append(
            {
                "decision": decision,
                "generation": budget,
                "scope": "sprint",
                "scope_ref": sprint,
                "cards": list(by_ref),
            }
        )
    else:
        spent = [str(item) for item in charged.get("spent") or []]
        origin = next(
            (
                found
                for ref in [*reversed(spent), *reversed(list(by_ref))]
                if (found := origin_field.po_origin(tasks[ref])) is not None
            ),
            None,
        )
        generation = sum(e2e_budget.card_cap(tasks[ref]) for ref in spent)
        request_id = e2e_budget.batch_decision_request_id(spent, generation)
        decision = _batch_decision(runtime, project, tasks, by_ref, spent, request_id, origin)
        waits.append(
            {
                "decision": decision,
                "generation": generation,
                "scope": "cards",
                "scope_ref": carrier_ref,
                "request_id": request_id,
                "spent": spent,
                "cards": list(by_ref),
            }
        )
    queue["budget_waits"] = waits
    runtime.save_records(payload, records)
    [wait] = waits
    for ref in by_ref:
        _remark(
            runtime,
            ref,
            "",
            E2eState(),
            state=AM_BUDGET_WAIT,
            decision=wait["decision"],
            holder=wait["decision"],
            dispatch_id="",
            carrier="",
            run_url="",
        )
    return _outcome(
        project,
        "e2e-after-merge-budget-waiting",
        ref=carrier_ref,
        decisions=[wait["decision"]],
        covered=list(by_ref),
    )


def _cap_line(ref: str, task: dict[str, Any], merge_sha: str, spent: list[str]) -> str:
    """One card of an out-of-sprint batch in its decision: where it merged, and its cap."""
    used, cap = e2e_record.e2e_state(task).dispatched, e2e_budget.card_cap(task)
    state = (
        f"cap spent, {used} of {cap} runs: needs a raise"
        if ref in spent
        else f"cap {used} of {cap} runs, not spent"
    )
    return f"- {ref} waits ({task.get('title') or ''}) on `{merge_sha}` (its merge; the after-merge e2e run): {state}"


def _batch_decision(
    runtime: Any,
    project: str,
    tasks: dict[str, dict[str, Any]],
    by_ref: dict[str, dict[str, Any]],
    spent: list[str],
    request_id: str,
    origin: dict[str, str] | None,
) -> str:
    """The one decision an out-of-sprint batch with spent caps needs: every covered card named with its cap,
    cut once under `request_id`, which authorizes a raise of each spent card's cap."""
    known = runtime.audit.committed_event(request_id)
    if known is not None and known.get("ref"):
        decision = str(known["ref"])
        shown = runtime.reader.show(decision)
        for ref in by_ref:
            if f"- {ref} waits " not in str(shown.get("description") or ""):
                runtime.writer.comment(role="dispatcher", actor=runtime.owner, reference=decision,
                    body=_cap_line(ref, tasks[ref], str(by_ref[ref]["merge_sha"]), [])
                         + "\n\nIt joins the batch: the same disposition applies to it.",
                    request_id="-".join(request_token(part) for part in
                                        ("dispatcher", "e2e-budget-join", decision, ref)))
        return decision
    commands = [
        f"      python3 -P -m ummanu task e2e-budget --ref {ref} --role po --authorized-by <event id>"
        for ref in spent
    ]
    charges: list[str] = []
    for ref in spent:
        state = e2e_record.e2e_state(tasks[ref])
        charges += [f"- {ref}: before-merge run `{run.dispatch_id}` ({run.status()})" for run in state.runs]
        if state.after_merge is not None:
            charges += [
                f"- {ref}: after-merge run `{dispatch_id}`" for dispatch_id in state.after_merge.charged
            ]
    description = "\n".join(
        [
            (
                f"The after-merge e2e run of {project} would cover {len(by_ref)} cards outside every open sprint. "
                "Such a run is charged to every covered card's own e2e cap, all together or none, and "
                f"{', '.join(spent)} {'has' if len(spent) == 1 else 'have'} no run left, so nothing was "
                "dispatched. The PO first decides a safe disposition using actual existing authority. "
                "Complete without raising caps to decline the batch. Only a new uncovered owner "
                "decision calls for explicit `task handover --to owner`. This card grants no money."
            ),
            "",
            "## Waiting for e2e",
            "",
            "Every card of the batch waits on this decision, and the whole batch is run or declined together:",
            "",
            *(_cap_line(ref, tasks[ref], str(by_ref[ref]["merge_sha"]), spent) for ref in by_ref),
            "",
            "Cards queued later while this decision is open join it with a comment.",
            "",
            "## Runs spent",
            "",
            *(charges or ["- (none recorded)"]),
            "",
            "## PO decision",
            "",
            (
                f"Disposition for the spent caps of {', '.join(spent)}: use existing authority or decline. "
                "If a new grant is necessary, hand over the uncovered question explicitly. "
                "Released genuine owner-comment grants remain readable with these answer lines:"
            ),
            "",
            f"    {e2e_budget.ANSWER_RAISE_LINE}",
            f"    {e2e_budget.ANSWER_NO_LINE}",
            "",
            "A comment with neither line, with both, or with two raise lines authorizes nothing.",
            "",
            "## Applying the owner's answer",
            "",
            (
                f"- `{e2e_budget.ANSWER_RAISE_LINE}`: run this for every card whose cap is spent, each with the "
                "event id of that comment; the raise is the owner's N. Then complete this card; the next pass "
                "dispatches one run over the whole batch:"
            ),
            "",
            *commands,
            "",
            (
                f"- `{e2e_budget.ANSWER_NO_LINE}`: complete this card without a raise; every card of the batch is "
                "then declined, with your completion text."
            ),
        ]
    )
    try:
        created = runtime.writer.create(
            role="dispatcher",
            actor=runtime.owner,
            project=project,
            task_type="decision",
            title=f"E2E caps spent: {', '.join(spent)}: PO batch disposition",
            description=description,
            target="ready",
            sprint="",
            origin=origin,
            **({"po_execution": po_execution.create_assignment(request_id, "e2e_budget", list(by_ref))}
               if origin is None else {}),
            request_id=request_id,
        )
    except TaskError:
        known = runtime.audit.committed_event(request_id)
        if known is None or not known.get("ref"):
            raise
        return _batch_decision(runtime, project, tasks, by_ref, spent, request_id, origin)
    return str(created["task"]["ref"])


def _decline(runtime: Any, queue: dict[str, Any], ref: str, text: str, *, decision: str) -> None:
    """The card leaves the pending set: no after-merge run will cover it."""
    runtime.writer.comment(
        role="dispatcher",
        actor=runtime.owner,
        reference=ref,
        body=f"## E2E after merge — declined\n\n{text}",
        request_id="-".join(
            request_token(part) for part in ("dispatcher", "e2e-am-declined", ref, decision or "cap")
        ),
    )
    _remark(
        runtime,
        ref,
        "",
        E2eState(),
        state=AM_DECLINED,
        decision=decision,
        holder="",
        note=safe_one_line(text, limit=500),
    )
    queue["pending"] = [entry for entry in queue["pending"] if str(entry.get("ref")) != ref]


def _decline_all(
    runtime: Any,
    payload: dict[str, Any],
    records: dict[str, Any],
    project: str,
    queue: dict[str, Any],
    text: str,
) -> dict[str, Any]:
    refs = [str(entry["ref"]) for entry in queue["pending"]]
    for ref in refs:
        _decline(runtime, queue, ref, text, decision="")
    runtime.save_records(payload, records)
    return _outcome(project, "e2e-after-merge-declined", covered=refs, reason=text)


def _standing_declines(
    runtime: Any, payload: dict[str, Any], records: dict[str, Any], project: str, queue: dict[str, Any],
) -> dict[str, Any] | None:
    """Decline pending cards before joining/creating a decision or picking a batch. Paid runs settle first."""
    budgets: dict[str, Any] = {}
    declined = []
    for entry in list(queue["pending"]):
        ref = str(entry["ref"])
        sprint = str(runtime.reader.show(ref).get("sprint") or "")
        if not sprint:
            continue
        if sprint not in budgets:
            budgets[sprint] = runtime.reader.sprint_e2e_budget(sprint)
        refusal = (budgets[sprint] or {}).get("refusal")
        if refusal is not None:
            _decline(runtime, queue, ref, owner_decisions.refusal_text(sprint, refusal),
                     decision=f"{sprint}/{refusal['id']}")
            declined.append(ref)
    if not declined:
        return None
    for wait in list(queue["budget_waits"]):
        wait["cards"] = [ref for ref in wait.get("cards") or [] if ref not in declined]
        if not wait["cards"]:
            queue["budget_waits"].remove(wait)
    runtime.save_records(payload, records)
    return _outcome(project, "e2e-after-merge-declined", covered=declined)


def _budget_recheck(
    runtime: Any, payload: dict[str, Any], records: dict[str, Any], project: str, queue: dict[str, Any]
) -> dict[str, Any] | None:
    """Cards waiting on a budget decision: None once every wait is over, else the tick's outcome."""
    for wait in list(queue["budget_waits"]):
        scope_ref = str(wait["scope_ref"])
        generation = int(wait["generation"])
        if wait["scope"] == "sprint":
            current = runtime.reader.sprint_e2e_budget(scope_ref)
            budget = int(current["budget"]) if current else generation
            room = current is None or int(current["used"]) < budget or budget > generation
            what = f"the e2e budget of {scope_ref} ({budget} runs) is spent"
        else:
            # The batch runs again only once every spent cap has room: all together or none.
            spent = {str(ref): runtime.reader.show(str(ref)) for ref in wait.get("spent") or []}
            short = [
                ref
                for ref, task in spent.items()
                if e2e_record.e2e_state(task).dispatched >= e2e_budget.card_cap(task)
            ]
            room = not short
            what = f"the e2e cap of {', '.join(short)} is spent"
        if room:
            queue["budget_waits"].remove(wait)
            runtime.save_records(payload, records)
            continue
        try:
            decision = runtime.reader.show(str(wait["decision"]))
        except TaskError as exc:
            if exc.code != "not_found":
                raise
            decision = None
        if decision is not None and decision.get("state") != "done":
            _join(runtime, payload, records, queue, wait)
            continue
        text = (
            f"No after-merge e2e run is dispatched: {what}, and "
            f"the decision {wait['decision']} "
            + ("was completed without a raise" if decision is not None else "no longer exists")
            + ". No additional budget was granted; the exhausted budget/cap prevents another run."
        )
        for ref in [str(ref) for ref in wait.get("cards") or []]:
            if any(str(entry.get("ref")) == ref for entry in queue["pending"]):
                _decline(runtime, queue, ref, text, decision=str(wait["decision"]))
        queue["budget_waits"].remove(wait)
        runtime.save_records(payload, records)
    if not queue["budget_waits"]:
        return None
    return _outcome(
        project,
        "e2e-after-merge-budget-waiting",
        decisions=[str(wait["decision"]) for wait in queue["budget_waits"]],
        covered=[str(entry["ref"]) for entry in queue["pending"]],
    )


def _join(
    runtime: Any,
    payload: dict[str, Any],
    records: dict[str, Any],
    queue: dict[str, Any],
    wait: dict[str, Any],
) -> None:
    """A card queued while a budget decision is open joins it, is listed on it, and shows the mark."""
    cards = list(wait.get("cards") or [])
    for entry in queue["pending"]:
        ref = str(entry["ref"])
        if ref in cards:
            continue
        task = runtime.reader.show(ref)
        if wait["scope"] == "sprint":
            _decision_card(
                runtime,
                task,
                e2e_record.e2e_state(task),
                str(entry["merge_sha"]),
                scope="sprint",
                scope_ref=str(wait["scope_ref"]),
                generation=int(wait["generation"]),
                spent_line="",
                charges=[],
                origin=None,
                waiting=[
                    (
                        ref,
                        str(task.get("title") or ""),
                        f"`{entry['merge_sha']}` (its merge; the after-merge e2e run)",
                    )
                ],
            )
        else:
            runtime.writer.comment(
                role="dispatcher",
                actor=runtime.owner,
                reference=str(wait["decision"]),
                body=(
                    _cap_line(ref, task, str(entry["merge_sha"]), [])
                    + "\n\nIt merged while this decision is open and joins the batch: the same answer applies "
                    "to it."
                ),
                request_id="-".join(
                    request_token(part)
                    for part in ("dispatcher", "e2e-budget-join", str(wait["decision"]), ref)
                ),
            )
        _remark(runtime, ref, "", E2eState(), state=AM_BUDGET_WAIT, decision=str(wait["decision"]), holder=str(wait["decision"]))
        cards.append(ref)
        wait["cards"] = cards
        runtime.save_records(payload, records)


def after_merge_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    """`production observe`'s view of every project's after-merge queue."""
    raw = payload.get(AFTER_MERGE_KEY)
    return {
        project: {
            "pending": [str(entry.get("ref")) for entry in queue.get("pending") or []],
            "in_flight": (queue.get("run") or {}).get("dispatch_id") or None,
            "covered": [str(entry.get("ref")) for entry in (queue.get("run") or {}).get("entries") or []],
            "budget_waits": [str(wait.get("decision")) for wait in queue.get("budget_waits") or []],
            "refs_to_delete": [str(item.get("ref")) for item in queue.get("cleanup") or []],
        }
        for project, queue in sorted((raw if isinstance(raw, dict) else {}).items())
        if isinstance(queue, dict)
    }


__all__ = [
    "AFTER_MERGE_KEY",
    "DISPATCH_INFIX",
    "STEP",
    "UNOWNED_HOTFIX_REASON",
    "Enqueued",
    "after_merge_snapshot",
    "enqueue",
    "queues",
    "reconcile_after_merge",
]
