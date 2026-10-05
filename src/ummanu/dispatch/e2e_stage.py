"""The e2e stage: an adapter-declared GitHub workflow run on a code card's candidate (secretary-1795).

Placement. For a `code` card of a project whose adapter declares `validation.e2e` (`dispatch/e2e.py`)
with the default `placement: before_merge`, the stage runs on the exact SHA that the merge gate's green receipt validated (`validated_sha`; the
stage never reads HEAD itself, and the gate read may refresh the base), and before the card becomes
releasable: in `review_verdict.park_green_verdict`, after the green review verdict (or right after
green CI when the card's review is `skipped`) and before the park in Assessment or the release; and
again in the release audit (`release_lifecycle.release_parked`), where a SHA that already has a green
run is not dispatched again. A red review never reaches it, so rework rounds spend no runs. A project
that declares `placement: after_merge` never runs this stage: its cards merge as with no e2e, and
`dispatch/e2e_after_merge.py` runs the workflow on `main` afterwards (secretary-1807).

Dispatch, exactly once per candidate SHA. The card's `e2e` field (`board/e2e_record.py`) is the
stage's record. A run record is written as an intent (card, SHA, dispatch id) before the
`workflow_dispatch` call, which asks GitHub for the run it starts (`return_run_details`); the run id in
the answer is recorded right after it, and the run's `head_sha` is checked against the candidate. A
record that exists for the SHA is continued, never dispatched again: after a crash between the call and
that record the run is looked up by event, branch, SHA and creation time (`e2e.matching_runs`), once
the window has settled: exactly one match is attached as `recovered`, several Block the card with the
candidates listed, and none within the identification window Blocks it too. A dispatch GitHub refuses
Blocks the card with GitHub's answer.

Carrying a green run. A green run authorizes its own SHA, and a SHA that `reconcile_reviewed_base_move`
(the rule that carries a review) reconciles it to: base history only, the card's own paths unchanged.
The reconciliation is recorded on the run and in the attestation. Any other SHA runs the stage again.

Wait. Once the run is identified the dispatcher creates a `wait` card for it (`dispatch/wait_cards.py`),
in the code card's sprint, with the adapter's deadline and the return address `card:<ref>`, under a
request id derived from the dispatch id, so a repeat after a crash is the same card. There is no
other poller: while the wait card waits, this stage only reads its frozen result. The merge gate is
read before the dispatch and accepted once, after the stage is green, by the caller; it is not re-read
while a run is underway.

Outcomes, read off the wait's frozen result:

- conclusion `success`: the stage is green for this SHA; the card proceeds (Assessment or release);
- conclusion `failure`: rework, like a red gate (`gate_lifecycle.gate_red_to_worker`), with the run
  URL, the failed jobs and steps and the gate's bounded `--log-failed` fragment; in the release audit,
  where no rework round is open, Blocked with the same evidence;
- any other conclusion, and every wait outcome other than `target_reached`: Blocked, classified as
  infrastructure (`blocked_reason: infrastructure`), with the outcome and the link.

A run whose result Blocked the card records that move's request id; once it is committed the run's
pass is over, and a card brought back to the same SHA may spend a new run.

Budget (secretary-1796, `board/e2e_budget.py`). A card of a sprint spends the sprint's e2e run budget:
the run is charged by the write of its intent (`TaskWriter.record_e2e_intent`: one conditional UPDATE of
the sprint row in the intent's transaction), so a run whose outcome is unknown is paid for, and a
recovered run, which never writes a second intent, is never charged twice. When the budget has no run
left nothing is dispatched: the dispatcher cuts one `decision` card on the sprint for that budget
generation (request id `dispatcher-e2e-budget-<sprint>-<budget>`), cards reaching the stage later join
it with a comment, and each waiting card records `budget_wait` and stays where it is. Each tick the
stage re-checks the budget first: a raise (the owner's word, applied by the PO with `sprint
e2e-budget`) lets the card dispatch; the decision Done with no raise Blocks it with the decision's text
(`blocked_reason: other`). A card outside every sprint keeps the per-card cap of :data:`E2E_RUN_CAP`
plus the raises on it: a spent cap gets a decision card with the card's PO origin when it has one, and
has explicit PO execution assignment when it has none.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from ummanu.board import e2e_budget, e2e_record, owner_decisions, po_execution, wait_card
from ummanu.board import po_origin as origin_field
from ummanu.board.completion_evidence import has_candidate
from ummanu.board.e2e_record import FAILURE, REFUSED, SENT, SUCCESS, BudgetWait, E2eRun, E2eState
from ummanu.dispatch import attempt_accounting
from ummanu.dispatch.e2e import (
    E2E_CLOCK_MARGIN_SECONDS,
    E2E_IDENTIFY_SECONDS,
    E2E_RECOVERY_SETTLE_SECONDS,
    DispatchRefused,
    E2eDeclaration,
    declared_e2e,
    dispatch_workflow,
    matching_runs,
    red_evidence,
    run_head_sha,
)
from ummanu.dispatch.gate import GateResult, _name_with_owner
from ummanu.dispatch.gate import _fingerprint as _gate_fingerprint
from ummanu.dispatch.gate_receipt import is_exact_sha
from ummanu.dispatch.helpers import _legacy_worker_branch, safe_one_line, scrub_host_output
from ummanu.dispatch.state import DispatcherRecord, request_token
from ummanu.dispatch.state import attempt_request_id as _attempt_request_id
from ummanu.dispatch.types import HostError
from ummanu.tasks import TaskError

#: The phase a red run's rework is opened under: its request id carries `gate-red`, so the sprint
#: charges it as the red CI it is.
E2E_PHASE = "e2e-gate"
E2E_FAILURE_REASON = "e2e-failure"
#: How a run was named: GitHub's dispatch answer, or the lookup after that answer was lost.
IDENTIFIED_BY_ANSWER = "answer"
IDENTIFIED_BY_RECOVERY = "recovery"


def utcnow() -> datetime:
    """The stage's clock; tests replace it."""
    return datetime.now(UTC)


def applies(task: dict[str, Any]) -> bool:
    """A code card with a candidate; a card of unknown kind reads as code, as everywhere else."""
    return has_candidate(task) and str(task.get("type") or "code") == "code"


def stage_request_id(action: str, dispatch_id: str) -> str:
    """A request id bound to one run (its dispatch id names the card): the same after any crash,
    restart or lost dispatcher record."""
    return "-".join(request_token(part) for part in ("dispatcher", action, dispatch_id))


@dataclass(frozen=True)
class E2eProceed:
    """The stage is green for the SHA the gate validated: accept `result` once, and proceed.

    `reconciliation` is the base-only move that carried a green run from the SHA it ran on to this one,
    for the attestation; None when the run ran on this very SHA.
    """

    result: GateResult
    reconciliation: dict[str, Any] | None = None


def run_stage(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    *,
    step: str,
    gate: Callable[[], tuple[dict[str, Any] | None, GateResult | None]],
) -> dict[str, Any] | E2eProceed | None:
    """The e2e stage for one tick: None when the card has no e2e (the caller reads and accepts the gate
    as before), the tick's outcome, or :class:`E2eProceed` with the green gate result to accept.

    The stage never reads HEAD itself. `gate` is the merge gate read (the path that may refresh the
    base), without accepting it: `(outcome, None)` unless green, `(None, result)` on green. The SHA its
    receipt validated is the one the stage dispatches on, records, identifies and judges. A run still
    underway (or a result not yet acted on) is dealt with first, without reading the gate.
    """
    if not applies(task):
        return None
    ref = task["ref"]
    try:
        declaration = declared_e2e(runtime.host, str(task.get("project") or ""))
    except HostError as exc:
        return _block(
            runtime,
            task,
            record,
            records,
            payload,
            attempt_id,
            request_id=_attempt_request_id(record.attempt_id or attempt_id, "e2e-declaration-blocked", ref),
            reason=f"The e2e stage cannot read this project's e2e declaration: {scrub_host_output(str(exc))}",
            step=step,
            outcome="e2e declaration unreadable",
            blocked_reason="gate",
        )
    if declaration is None or declaration.after_merge:
        # An `after_merge` project runs its e2e on main after the merge (`dispatch/e2e_after_merge.py`):
        # the card goes through review, Assessment and release as a project with no e2e.
        return None
    state = e2e_record.e2e_state(task)
    last = state.runs[-1] if state.runs else None
    if last is not None:
        if last.closing and runtime.audit.committed_event(last.closing) is None:
            # Its Blocked move did not commit: repeat it, with the same id and words.
            return _block_run(runtime, task, record, records, payload, attempt_id, last, step=step)
        if not last.closing and not last.acted:
            settled = _settle_run(
                runtime, task, record, records, payload, attempt_id, state, last, declaration, step=step
            )
            if settled is not None:
                return settled
    if state.budget_wait is not None:
        # A spent budget is re-checked first, without reading the gate: a raise lets the card go on.
        held = _budget_recheck(runtime, task, record, records, payload, attempt_id, state, step=step)
        if held is not None:
            return held
    outcome, result = gate()
    if outcome is not None:
        return outcome
    assert result is not None
    sha = _validated_sha(result)
    if not sha:
        # No exact-SHA receipt to bind to: the caller's acceptance refuses the result, so this never
        # proceeds on the stage's account.
        return E2eProceed(result)
    if state.green(sha) is not None:
        return E2eProceed(result)
    latest = state.latest(sha)
    if latest is not None and latest.conclusion == FAILURE and not latest.closing:
        # A red run stands for its SHA: the card goes back to rework again, no run is spent.
        return _act(runtime, task, record, records, payload, attempt_id, state, latest, step=step)
    carried = state.last_green()
    if carried is not None:
        # The rule that carries a review across a base-only move carries the e2e result too.
        reconciliation = runtime.host.reconcile_reviewed_base_move(task, record, carried.sha, sha)
        if reconciliation is not None:
            entry = dict(reconciliation)
            if entry not in carried.reconciled:
                carried.reconciled.append(entry)
                _persist(runtime, ref, state)
            return E2eProceed(result, entry)
    if not str(task.get("sprint") or "") and state.dispatched >= e2e_budget.card_cap(task):
        return _cap_spent(runtime, task, record, records, payload, attempt_id, state, sha, step=step)
    refusal = _standing_refusal(runtime, str(task.get("sprint") or ""))
    if refusal is not None:
        return _standing_decline(runtime, task, record, records, payload, attempt_id, state, refusal, step=step)
    started = _dispatch(runtime, task, record, attempt_id, state, declaration, sha, step=step)
    if isinstance(started, dict):
        return started
    if isinstance(started, _Spent):
        return _budget_spent(runtime, task, record, records, payload, attempt_id, state, sha, started, step=step)
    settled = _settle_run(
        runtime, task, record, records, payload, attempt_id, state, started, declaration, step=step
    )
    return settled or _outcome(ref, attempt_id, "e2e-dispatching", step=step, sha=sha)


def _validated_sha(result: GateResult) -> str:
    """The SHA the green gate's receipt validated, or `""` when it carries no exact one."""
    attestation = result.attestation if isinstance(result.attestation, dict) else {}
    sha = str(attestation.get("validated_sha") or "")
    return sha if is_exact_sha(sha) else ""


# --- dispatch and identification -----------------------------------------------------------------


def _persist(runtime: Any, ref: str, state: E2eState) -> None:
    runtime.writer.record_e2e_state(role="dispatcher", actor=runtime.owner, reference=ref, state=state.text())


@dataclass(frozen=True)
class _Spent:
    """The sprint's e2e budget had no run left when the intent was to be charged: nothing was written."""

    budget: int
    used: int
    charges: tuple[dict[str, Any], ...]


def _dispatch(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    attempt_id: str,
    state: E2eState,
    declaration: E2eDeclaration,
    sha: str,
    *,
    step: str,
) -> E2eRun | dict[str, Any] | _Spent:
    """Charge and write the intent, then dispatch once.

    The run record, the outcome of a transport retry, or :class:`_Spent` when the card's sprint has no
    run left (nothing written, nothing dispatched).
    """
    ref = task["ref"]
    try:
        repo = _name_with_owner(runtime.host, record.workspace)
    except HostError as exc:
        # Nothing is written and nothing dispatched yet: the next tick asks again.
        return {
            **_outcome(ref, attempt_id, "e2e-dispatching", step=step, sha=sha),
            "status": "degraded",
            "reason": f"the e2e stage could not name the repository: {scrub_host_output(str(exc))}",
        }
    run = E2eRun(
        dispatch_id=f"{ref}-e2e-{len(state.runs) + 1}-{secrets.token_hex(4)}",
        sha=sha,
        repo=repo,
        branch=_legacy_worker_branch(ref),
        workflow=declaration.workflow,
        intent_at=wait_card.utc_text(utcnow()),
        deadline=declaration.deadline,
    )
    state.runs.append(run)
    waiting, state.budget_wait = state.budget_wait, None
    # The intent is on the card, and the run charged to the sprint, before anything reaches GitHub.
    charged = runtime.writer.record_e2e_intent(
        role="dispatcher",
        actor=runtime.owner,
        reference=ref,
        state=state.text(),
        sprint=str(task.get("sprint") or ""),
        dispatch_id=run.dispatch_id,
    )
    if not charged.get("charged"):
        state.runs.pop()
        state.budget_wait = waiting
        return _Spent(
            int(charged.get("budget") or 0),
            int(charged.get("used") or 0),
            tuple(item for item in charged.get("charges") or [] if isinstance(item, dict)),
        )
    try:
        dispatched = dispatch_workflow(
            runtime.host, repo, declaration, branch=run.branch, dispatch_id=run.dispatch_id, sha=sha
        )
    except DispatchRefused as exc:
        run.dispatch = REFUSED
        run.dispatch_detail = safe_one_line(scrub_host_output(str(exc)), limit=1000)
        _close(
            run,
            f"The e2e workflow `{run.workflow}` could not be dispatched on `{run.branch}` @ `{sha[:12]}`: "
            f"{run.dispatch_detail}. No run started and none was charged to a worker round; the "
            "workflow, its `workflow_dispatch` trigger or the dispatcher's access has to be repaired.",
        )
    except HostError as exc:
        # No answer, or a rate limit: GitHub may or may not have taken it. The run is looked up from
        # here on, exactly as after a crash; it is never dispatched again.
        run.dispatch_detail = f"unconfirmed: {safe_one_line(scrub_host_output(str(exc)), limit=500)}"
    else:
        run.dispatch = SENT
        # GitHub's own answer names the run; an answer without one is looked up like a crash.
        if dispatched.run_id:
            run.run_id = dispatched.run_id
            run.run_url = f"https://github.com/{repo}/actions/runs/{dispatched.run_id}"
            run.identified_by = IDENTIFIED_BY_ANSWER
    _persist(runtime, ref, state)
    return run


def _identify(
    runtime: Any,
    task: dict[str, Any],
    attempt_id: str,
    state: E2eState,
    run: E2eRun,
    declaration: E2eDeclaration,
    *,
    step: str,
) -> dict[str, Any] | None:
    """Name the run and check its SHA; None once both are on the record, else the tick's outcome.

    A run GitHub's dispatch answer named is only checked (`head_sha`). One whose answer was lost is
    recovered by event, branch, SHA and creation time (plus the dispatch id in its title when the
    adapter declares `dispatch_id_input`), and only once the window has settled: then exactly one match
    is attached as `recovered`, several Block the card with every candidate listed, and none keeps the
    lookup going until the identification window Blocks it.
    """
    ref = task["ref"]
    intent = wait_card.parse_utc(run.intent_at, "intent_at")
    error = ""
    head_sha = ""
    try:
        if run.run_id:
            head_sha = run_head_sha(runtime.host, run.repo, run.run_id)
        else:
            settle_at = intent + timedelta(seconds=E2E_CLOCK_MARGIN_SECONDS + E2E_RECOVERY_SETTLE_SECONDS)
            if utcnow() < settle_at:
                return {
                    **_outcome(ref, attempt_id, "e2e-identifying", step=step, sha=run.sha),
                    "dispatch_id": run.dispatch_id,
                    "recovery_settles_at": wait_card.utc_text(settle_at),
                }
            titled = bool(declaration.dispatch_id_input)
            found = matching_runs(
                runtime.host,
                run.repo,
                run.workflow,
                branch=run.branch,
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
                    f"The e2e run dispatched at {run.intent_at} for `{run.sha[:12]}` cannot be told apart: "
                    f"{len(found)} `{run.workflow}` workflow_dispatch runs on `{run.branch}` at that SHA "
                    f"were created since: {listed}. GitHub's answer naming the run was lost, so none is "
                    "taken as this candidate's e2e result, and nothing is dispatched again.",
                )
                _persist(runtime, ref, state)
                return None
            if found:
                run.run_id = int(found[0]["id"])
                run.run_url = f"https://github.com/{run.repo}/actions/runs/{run.run_id}"
                run.identified_by = IDENTIFIED_BY_RECOVERY
                run.recovery_rule = (
                    f"the only workflow_dispatch run of {run.workflow} on {run.branch} at {run.sha} created "
                    f"at or after {wait_card.utc_text(intent - timedelta(seconds=E2E_CLOCK_MARGIN_SECONDS))}"
                    + (f", its title carrying {run.dispatch_id}" if titled else "")
                    + f", looked up at {wait_card.utc_text(utcnow())}"
                )
                head_sha = str(found[0].get("head_sha") or "")
                _persist(runtime, ref, state)
                runtime.writer.comment(
                    role="dispatcher",
                    actor=runtime.owner,
                    reference=ref,
                    body=(
                        f"E2E run {run.run_url} was identified by recovery, not by GitHub's dispatch answer "
                        f"(that answer was lost): {run.recovery_rule}."
                    ),
                    request_id=stage_request_id("e2e-recovered", run.dispatch_id),
                )
    except HostError as exc:
        head_sha, error = "", safe_one_line(scrub_host_output(str(exc)), limit=500)
    if not head_sha:
        window_end = intent + timedelta(seconds=E2E_IDENTIFY_SECONDS)
        if utcnow() < window_end:
            return {
                **_outcome(ref, attempt_id, "e2e-identifying", step=step, sha=run.sha),
                "dispatch_id": run.dispatch_id,
                **({"run": run.run_url} if run.run_url else {}),
                **({"error": error} if error else {}),
            }
        what = (
            f"its run {run.run_url} could not be read"
            if run.run_id
            else f"no `{run.workflow}` workflow_dispatch run on `{run.branch}` at that SHA was found"
        )
        _close(
            run,
            f"The e2e run dispatched at {run.intent_at} for `{run.sha[:12]}` (dispatch id `{run.dispatch_id}`) "
            f"could not be identified: {what} within {E2E_IDENTIFY_SECONDS // 60} minutes"
            + (f" ({run.dispatch_detail})" if run.dispatch_detail else "")
            + (f"; last error: {error}" if error else "")
            + ". Nothing was dispatched a second time.",
        )
        _persist(runtime, ref, state)
        return None
    run.head_sha = head_sha
    if head_sha != run.sha:
        _close(
            run,
            f"The e2e run {run.run_url} ran on `{head_sha[:12]}`, not on the candidate `{run.sha[:12]}`: "
            f"`{run.branch}` moved between the dispatch and the run. It is not accepted as this "
            "candidate's e2e result.",
        )
    _persist(runtime, ref, state)
    return None


def _create_wait(runtime: Any, task: dict[str, Any], state: E2eState, run: E2eRun) -> None:
    """The wait card for this run, created once: its request id is derived from the dispatch id."""
    ref = task["ref"]
    created = runtime.writer.create(
        role="dispatcher",
        actor=runtime.owner,
        project=str(task.get("project") or ""),
        task_type="wait",
        title=f"E2E run {run.workflow} for {ref} @ {run.sha[:12]}",
        description=(
            f"The dispatcher's e2e stage waits here for the `{run.workflow}` run it dispatched on "
            f"`{run.branch}` @ `{run.sha}` for {ref} (dispatch id `{run.dispatch_id}`). Its result "
            f"returns to {ref}, whose e2e record names this card."
        ),
        target="ready",
        sprint=str(task.get("sprint") or ""),
        wait={
            "run": run.run_url,
            "deadline": run.deadline,
            "returns": [wait_card.CARD_PREFIX + ref],
        },
        request_id=stage_request_id("e2e-wait", run.dispatch_id),
    )
    run.wait_ref = str(created["task"]["ref"])
    _persist(runtime, ref, state)


def _settle_run(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    state: E2eState,
    run: E2eRun,
    declaration: E2eDeclaration,
    *,
    step: str,
) -> dict[str, Any] | None:
    """Take a run no result was acted on as far as it goes this tick: identify it, wait for it, act on
    its result. None once it is green (the stage goes on to the gate), else the tick's outcome."""
    ref = task["ref"]
    if not run.closing and run.result is None:
        if not run.run_id or not run.head_sha:
            pending = _identify(runtime, task, attempt_id, state, run, declaration, step=step)
            if pending is not None:
                return pending
        if not run.closing and not run.wait_ref:
            try:
                _create_wait(runtime, task, state, run)
            except TaskError as exc:
                _close(
                    run,
                    f"The e2e run {run.run_url} was dispatched, but its wait card could not be created: "
                    f"{exc.code}: {exc.message}",
                )
                _persist(runtime, ref, state)
        if not run.closing:
            try:
                wait = runtime.reader.show(run.wait_ref)
            except TaskError as exc:
                if exc.code != "not_found":
                    raise
                _close(run, f"The wait card {run.wait_ref} of the e2e run {run.run_url} no longer exists.")
                _persist(runtime, ref, state)
            else:
                result = wait_card.wait_state(wait).result
                if result is None:
                    return _waiting(task, run, attempt_id, step=step, state=state, wait=wait)
                fact = result.get("fact") if isinstance(result.get("fact"), dict) else {}
                run.result = {
                    "outcome": str(result.get("outcome") or ""),
                    "conclusion": str(fact.get("conclusion") or ""),
                    "summary": str(result.get("summary") or ""),
                    "evidence": str(result.get("evidence") or run.run_url),
                    "key": str(result.get("key") or ""),
                }
                _persist(runtime, ref, state)
    if run.closing:
        return _block_run(runtime, task, record, records, payload, attempt_id, run, step=step)
    return _act(runtime, task, record, records, payload, attempt_id, state, run, step=step)


# --- outcomes ------------------------------------------------------------------------------------


def _act(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    state: E2eState,
    run: E2eRun,
    *,
    step: str,
) -> dict[str, Any] | None:
    """What the wait's frozen result does to the card; the result is marked acted on first."""
    ref = task["ref"]
    result = run.result or {}
    outcome = result.get("outcome") or ""
    conclusion = run.conclusion
    if outcome == wait_card.TARGET_REACHED and conclusion == SUCCESS:
        _comment_green(runtime, task, state, run)
        if not run.acted:
            run.acted = True
            _persist(runtime, ref, state)
        return None
    if outcome == wait_card.TARGET_REACHED and conclusion == FAILURE:
        summary, log, fingerprint = _red_evidence(runtime, run)
        if step == "assessment":
            # A release decision opens no rework round: the card stops with the evidence on it.
            _close(
                run,
                f"Observer decision: release. The e2e stage is red: {summary}. The card is Blocked "
                f"rather than released.\nTail:\n```\n{log}\n```",
            )
            _persist(runtime, ref, state)
            return _block_run(runtime, task, record, records, payload, attempt_id, run, step=step)
        if not run.acted:
            run.acted = True
            _persist(runtime, ref, state)
        from ummanu.dispatch.gate_lifecycle import gate_red_to_worker

        return gate_red_to_worker(
            runtime,
            task,
            record,
            records,
            payload,
            attempt_id,
            GateResult(
                "red",
                summary,
                log,
                fingerprint=fingerprint,
                failure_class="substantive",
                failure_reason=E2E_FAILURE_REASON,
            ),
            phase=E2E_PHASE,
        )
    if outcome == wait_card.TARGET_REACHED:
        what = f"concluded {conclusion or 'with no conclusion'}"
    else:
        what = f"was not waited out: its wait card ended {outcome} ({result.get('summary') or ''})"
    _close(
        run,
        f"The e2e run {run.run_url or run.dispatch_id} on `{run.sha[:12]}` {what}. That is the e2e "
        f"infrastructure, not the candidate's code: the card is Blocked, and no worker round is "
        f"charged. Wait card: {run.wait_ref}. Evidence: {result.get('evidence') or run.run_url}.",
    )
    _persist(runtime, ref, state)
    return _block_run(runtime, task, record, records, payload, attempt_id, run, step=step)


def _red_evidence(runtime: Any, run: E2eRun) -> tuple[str, str, str]:
    """The red run as a gate verdict: `(summary, log fragment, fingerprint)`."""
    evidence = red_evidence(runtime.host, run.repo, run.run_id, run.run_url)
    failed = "; ".join(
        f"job «{safe_one_line(job) or '?'}»"
        + (", step " + ", ".join(f'"{safe_one_line(step)}"' for step in steps) if steps else "")
        for job, steps in evidence.jobs
    )
    summary = (
        f"e2e workflow `{run.workflow}` run {run.run_url} concluded failure on `{run.branch}` @ "
        f"`{run.sha[:12]}`; failed: {failed or 'no failed job was listed'}"
    )
    if evidence.note:
        summary += f" ({evidence.note})"
    fragment = evidence.fragment
    log = fragment.text if fragment.available else f"log unavailable: {fragment.reason}"
    first_job, first_steps = evidence.jobs[0] if evidence.jobs else ("", ())
    fingerprint = _gate_fingerprint(
        "e2e",
        run.workflow,
        first_job,
        fragment.step or ",".join(first_steps),
        fragment.text or fragment.reason,
    )
    return scrub_host_output(summary), scrub_host_output(log).strip(), fingerprint


def _comment_green(runtime: Any, task: dict[str, Any], state: E2eState, run: E2eRun) -> None:
    """The green result on the card, once per run: the park or the release that follows carries it."""
    ref = task["ref"]
    runtime.writer.comment(
        role="dispatcher",
        actor=runtime.owner,
        reference=ref,
        body=(
            "## E2E — green\n\n"
            f"The e2e workflow `{run.workflow}` run {run.run_url} concluded success on the candidate "
            f"`{run.sha}` (`{run.branch}`, dispatch id `{run.dispatch_id}`, wait card {run.wait_ref}). "
            f"Runs dispatched for this card: {state.dispatched}"
            + (
                f", charged to the e2e budget of {task['sprint']}. "
                if task.get("sprint")
                else f" of {e2e_budget.card_cap(task)}. "
            )
            + "The run was identified "
            + (
                "by recovery, not by GitHub's dispatch answer: " + run.recovery_rule + "."
                if run.identified_by == IDENTIFIED_BY_RECOVERY
                else "by GitHub's dispatch answer."
            )
        ),
        request_id=stage_request_id("e2e-green", run.dispatch_id),
    )


def _close(run: E2eRun, reason: str) -> None:
    run.closing = stage_request_id("e2e-blocked", run.dispatch_id)
    run.closing_reason = reason
    run.acted = True


def _block_run(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    run: E2eRun,
    *,
    step: str,
) -> dict[str, Any]:
    """The Blocked move this run's record already names, with its recorded reason.

    A red run Blocked in the release audit is the candidate failing its check (`gate`); every other end
    of a run is the e2e infrastructure's.
    """
    return _block(
        runtime,
        task,
        record,
        records,
        payload,
        attempt_id,
        request_id=run.closing,
        reason=run.closing_reason,
        step=step,
        outcome="e2e " + (run.status() if run.result or run.dispatch == REFUSED else "run unavailable"),
        blocked_reason="gate" if run.result is not None and run.conclusion == FAILURE else "infrastructure",
    )


def _block(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    *,
    request_id: str,
    reason: str,
    step: str,
    outcome: str,
    blocked_reason: str = "infrastructure",
) -> dict[str, Any]:
    """Blocked with the heads down, like every other merge-path block; a run's end is infrastructure.

    The cap (`other`) and an unreadable declaration (`gate`) are not the e2e infrastructure failing.
    """
    ref = task["ref"]
    runtime.host.stop(record)
    verdict = record.worker_continuation.verdict_outcome
    attempt_accounting.terminal_effect(
        runtime,
        task,
        record,
        target="blocked",
        reason=reason,
        request_id=request_id,
        terminal_state="blocked",
        disposition="blocked",
        verdict=verdict if verdict in {"green", "red", "blocked"} else "missing",
        blocked_reason=blocked_reason,
    )
    records.pop(ref, None)
    runtime.save_records(payload, records)
    return {"status": "blocked", "step": step, "pilot_ref": ref, "reason": outcome}


# --- the e2e run budget (secretary-1796) -----------------------------------------------------------


#: The owner's answer is applied with one of these; the decision card's body names the exact command.
_RAISE_COMMANDS = {
    "sprint": "python3 -P -m ummanu sprint e2e-budget --ref {scope} --role po --authorized-by <event id>",
    "card": "python3 -P -m ummanu task e2e-budget --ref {scope} --role po --authorized-by <event id>",
}


def _standing_refusal(runtime: Any, sprint: str) -> dict[str, Any] | None:
    if not sprint:
        return None
    current = runtime.reader.sprint_e2e_budget(sprint)
    return current.get("refusal") if current else None


def _standing_decline(
    runtime: Any, task: dict[str, Any], record: DispatcherRecord, records: dict[str, DispatcherRecord],
    payload: dict[str, Any], attempt_id: str, state: E2eState, refusal: dict[str, Any], *, step: str,
) -> dict[str, Any]:
    sprint, ref = str(task["sprint"]), str(task["ref"])
    decision = f"{sprint}/{refusal['id']}"
    request_id = _attempt_request_id(record.attempt_id or attempt_id, "e2e-standing-declined", ref, decision)
    state.budget_wait = None
    state.budget_decline = {"decision": decision, "request_id": request_id}
    _persist(runtime, ref, state)
    return _block(runtime, task, record, records, payload, attempt_id, request_id=request_id,
                  reason=owner_decisions.refusal_text(sprint, refusal), step=step,
                  outcome="standing owner decision refuses e2e", blocked_reason="other")


def _budget_spent(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    state: E2eState,
    sha: str,
    spent: _Spent,
    *,
    step: str,
) -> dict[str, Any]:
    """The sprint's budget has no run left: wait on the decision for this budget generation."""
    sprint = str(task.get("sprint") or "")
    refusal = _standing_refusal(runtime, sprint)
    if refusal is not None:
        return _standing_decline(runtime, task, record, records, payload, attempt_id, state, refusal, step=step)
    return _await_decision(
        runtime,
        task,
        record,
        records,
        payload,
        attempt_id,
        state,
        sha,
        scope="sprint",
        scope_ref=sprint,
        generation=spent.budget,
        spent_line=f"The e2e run budget of {sprint} is spent: {spent.used} of {spent.budget} runs.",
        charges=list(spent.charges),
        origin=None,
        step=step,
    )


def _cap_spent(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    state: E2eState,
    sha: str,
    *,
    step: str,
) -> dict[str, Any]:
    """A spent standalone cap has one PO decision, using origin or explicit assignment."""
    ref = task["ref"]
    cap = e2e_budget.card_cap(task)
    origin = origin_field.po_origin(task)
    charges = [{"card": ref, "dispatch_id": run.dispatch_id, "at": run.intent_at} for run in state.runs]
    return _await_decision(
        runtime,
        task,
        record,
        records,
        payload,
        attempt_id,
        state,
        sha,
        scope="card",
        scope_ref=ref,
        generation=cap,
        spent_line=(
            f"The e2e run cap of {ref}, a card outside every sprint, is spent: {state.dispatched} of {cap} runs."
        ),
        charges=charges,
        origin=origin,
        step=step,
    )


def _await_decision(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    state: E2eState,
    sha: str,
    *,
    scope: str,
    scope_ref: str,
    generation: int,
    spent_line: str,
    charges: list[dict[str, Any]],
    origin: dict[str, str] | None,
    step: str,
) -> dict[str, Any]:
    """Cut, or join, the one decision of this budget generation, and wait on it where the card is."""
    ref = task["ref"]
    try:
        decision = _decision_card(
            runtime,
            task,
            state,
            sha,
            scope=scope,
            scope_ref=scope_ref,
            generation=generation,
            spent_line=spent_line,
            charges=charges,
            origin=origin,
        )
    except TaskError as exc:
        return _block(
            runtime,
            task,
            record,
            records,
            payload,
            attempt_id,
            request_id=_attempt_request_id(record.attempt_id or attempt_id, "e2e-budget-blocked", ref, sha),
            reason=(
                f"{spent_line} The PO decision card could not be cut: "
                f"{exc.code}: {exc.message}. Nothing was dispatched for `{sha[:12]}`."
            ),
            step=step,
            outcome="e2e budget spent",
            blocked_reason="other",
        )
    waiting = state.budget_wait
    if waiting is None or (waiting.decision, waiting.generation) != (decision, generation):
        state.budget_wait = BudgetWait(decision, generation, scope, wait_card.utc_text(utcnow()))
        _persist(runtime, ref, state)
    return _budget_waiting(task, state, attempt_id, step=step, sha=sha)


def _decision_card(
    runtime: Any,
    task: dict[str, Any],
    state: E2eState,
    sha: str,
    *,
    scope: str,
    scope_ref: str,
    generation: int,
    spent_line: str,
    charges: list[dict[str, Any]],
    origin: dict[str, str] | None,
    waiting: list[tuple[str, str, str]] | None = None,
) -> str:
    """The decision card of this budget generation: cut once, under a request id derived from it.

    `waiting` is every card that waits on it, `(ref, title, the SHA its run is for)`; by default the
    one card and `sha`. A card reaching a generation whose decision already exists joins it with one
    comment.
    """
    waiting = waiting or [(task["ref"], str(task.get("title") or ""), f"`{sha}`")]
    request_id = e2e_budget.decision_request_id(scope_ref, generation)
    known = runtime.audit.committed_event(request_id)
    if known is not None and known.get("ref"):
        decision = str(known["ref"])
        shown = runtime.reader.show(decision)
        for ref, title, where in waiting:
            if f"- {ref} waits " in str(shown.get("description") or ""):
                continue
            runtime.writer.comment(
                role="dispatcher",
                actor=runtime.owner,
                reference=decision,
                body=(
                    f"{ref} ({title}) also waits for its e2e run on {where}, and joins this decision: the "
                    "same answer applies to it."
                ),
                request_id="-".join(
                    request_token(part) for part in ("dispatcher", "e2e-budget-join", decision, ref)
                ),
            )
        return decision
    sprint_route = scope_ref if scope == "sprint" else ""
    if sprint_route:
        try:
            supported = runtime.sprints.show(sprint_route, include_cards=False).get("status") == "open"
        except TaskError as exc:
            if exc.code != "not_found":
                raise
            supported = False
        if not supported:
            sprint_route = ""
            origin = origin_field.po_origin(task)
    try:
        created = runtime.writer.create(
            role="dispatcher",
            actor=runtime.owner,
            project=str(task.get("project") or ""),
            task_type="decision",
            title=f"E2E budget spent: {scope_ref}: PO disposition",
            description=_decision_description(
                runtime,
                task,
                state,
                scope=scope,
                scope_ref=scope_ref,
                spent_line=spent_line,
                charges=charges,
                waiting=waiting,
            ),
            target="ready",
            sprint=sprint_route,
            origin=origin,
            **({"po_execution": po_execution.create_assignment(request_id, "e2e_budget", [ref for ref, _, _ in waiting])}
               if not sprint_route and origin is None else {}),
            request_id=request_id,
        )
    except TaskError:
        # Another tick may have committed the same generation with a different first join.
        if runtime.audit.committed_event(request_id) is None:
            raise
        return _decision_card(runtime, task, state, sha, scope=scope, scope_ref=scope_ref,
                              generation=generation, spent_line=spent_line, charges=charges,
                              origin=origin, waiting=waiting)
    return str(created["task"]["ref"])


def _decision_description(
    runtime: Any,
    task: dict[str, Any],
    state: E2eState,
    *,
    scope: str,
    scope_ref: str,
    spent_line: str,
    charges: list[dict[str, Any]],
    waiting: list[tuple[str, str, str]],
) -> str:
    """What the PO and the owner read: who waits, what was spent with its links and results, the question."""
    ref = task["ref"]
    states: dict[str, E2eState] = {ref: state}
    lines = []
    for charge in charges:
        card = str(charge.get("card") or "")
        if card not in states:
            try:
                states[card] = e2e_record.e2e_state(runtime.reader.show(card))
            except TaskError:
                states[card] = E2eState()
        run = next(
            (
                item
                for item in [*states[card].runs, *states[card].after_merge_runs]
                if item.dispatch_id == charge.get("dispatch_id")
            ),
            None,
        )
        if run is None:
            lines.append(f"- {card}: dispatch `{charge.get('dispatch_id')}` at {charge.get('at')}: no run record")
            continue
        result = run.result or {}
        lines.append(
            f"- {card} @ `{run.sha[:12]}`: {run.run_url or 'run not identified'} ({run.status()}"
            + (f": {result.get('summary')}" if result.get("summary") else "")
            + f"), dispatched {run.intent_at}"
        )
    command = _RAISE_COMMANDS[scope].format(scope=scope_ref)
    return "\n".join(
        [
            (
                f"{spent_line} Every e2e run pays for BitLaunch stands, so more runs are a money decision: "
                "the PO first applies effective standing decisions and decides a safe disposition within "
                "its authority. Complete without a raise to decline further runs. Only a new uncovered "
                "owner decision calls for explicit `task handover --to owner`; this question grants no money."
            ),
            "",
            "## Waiting for e2e",
            "",
            *(f"- {card} waits ({title}) on {where}" for card, title, where in waiting),
            "",
            "Cards that reach the stage later while the budget is spent join this decision with a comment.",
            "",
            "## Runs spent",
            "",
            *(lines or ["- (none recorded)"]),
            "",
            "## PO decision",
            "",
            (
                f"Disposition for {scope_ref}: apply existing authority or decline further runs. "
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
            *([
                "For an answer from the owner conversation in the PO session, record the verbatim quotation",
                "as an entry with a stable ID, scope sprint, kind e2e_grant (positive runs) or e2e_refusal",
                "(value no_more_e2e) in a JSON list. No owner-role card comment is required:",
                "",
                f"      python3 -P -m ummanu sprint record-owner-decisions --ref {scope_ref} --role po --decisions-file <JSON> --request-id <request>",
                "",
                f"Read back with `sprint show --ref {scope_ref}`, then complete this card.",
                "The genuine owner-comment commands below remain supported.",
                "",
            ] if scope == "sprint" else []),
            (
                f"- `{e2e_budget.ANSWER_RAISE_LINE}`: run this with the event id of that comment (the owner's "
                "answer input names it); the raise is the owner's N, and nothing else (`--add`, if given, has "
                "to equal it). Then complete this card; the waiting cards dispatch on the next tick:"
            ),
            "",
            f"      {command}",
            "",
            (
                f"- `{e2e_budget.ANSWER_NO_LINE}`: complete this card without a raise; every card waiting on it "
                "goes to Blocked with your completion text."
            ),
        ]
    )


def _budget_recheck(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    state: E2eState,
    *,
    step: str,
) -> dict[str, Any] | None:
    """A card waiting on a budget decision: None once a run is available again, else the tick's outcome."""
    ref = task["ref"]
    waiting = state.budget_wait
    assert waiting is not None
    if waiting.scope == "sprint":
        refusal = _standing_refusal(runtime, str(task.get("sprint") or ""))
        if refusal is not None:
            return _standing_decline(runtime, task, record, records, payload, attempt_id, state, refusal, step=step)
        current = runtime.reader.sprint_e2e_budget(str(task.get("sprint") or ""))
        budget = int(current["budget"]) if current else waiting.generation
        room = current is None or int(current["used"]) < budget
    else:
        budget = e2e_budget.card_cap(task)
        room = state.dispatched < budget
    raised = budget > waiting.generation
    if room or raised:
        # A run is there, or the owner raised the budget and others spent it first: the stage goes on,
        # and a budget spent again gets the decision of its new generation.
        state.budget_wait = None
        _persist(runtime, ref, state)
        return None
    try:
        decision = runtime.reader.show(waiting.decision)
    except TaskError as exc:
        if exc.code != "not_found":
            raise
        decision = None
    if decision is not None and decision.get("state") != "done":
        return _budget_waiting(task, state, attempt_id, step=step, sha="")
    said = _decision_text(runtime, waiting.decision) if decision is not None else ""
    state.budget_wait = None
    _persist(runtime, ref, state)
    return _block(
        runtime,
        task,
        record,
        records,
        payload,
        attempt_id,
        request_id="-".join(
            request_token(part) for part in ("dispatcher", "e2e-budget-declined", ref, waiting.decision)
        ),
        reason=(
            f"No e2e run is dispatched: the e2e budget ({budget} runs) is spent, and the decision "
            f"{waiting.decision} "
            + ("was completed without a raise" if decision is not None else "no longer exists")
            + ". This is the owner's money decision, not a defect of the card's code."
            + (f"\n\nThe decision:\n\n{said}" if said else "")
        ),
        step=step,
        outcome="e2e budget not raised",
        blocked_reason="other",
    )


def _decision_text(runtime: Any, decision: str) -> str:
    """The reason of the decision card's move into Done: the PO's completion record."""
    for event in reversed(runtime.audit.events(decision)):
        transition = event.get("transition") if isinstance(event.get("transition"), dict) else {}
        if transition.get("target") == "done":
            return str(event.get("reason") or "").strip()
    return ""


def _budget_waiting(
    task: dict[str, Any], state: E2eState, attempt_id: str, *, step: str, sha: str
) -> dict[str, Any]:
    waiting = state.budget_wait
    assert waiting is not None
    return {
        **_outcome(task["ref"], attempt_id, "e2e-budget-waiting", step=step, sha=sha),
        "decision": waiting.decision,
        "mark": waiting.mark,
        "runs_dispatched": state.dispatched,
    }


def _waiting(
    task: dict[str, Any],
    run: E2eRun,
    attempt_id: str,
    *,
    step: str,
    state: E2eState,
    wait: dict[str, Any],
) -> dict[str, Any]:
    view = wait_card.wait_view(wait) or {}
    return {
        **_outcome(task["ref"], attempt_id, "e2e-waiting", step=step, sha=run.sha),
        "run": run.run_url,
        "wait_card": run.wait_ref,
        "deadline": view.get("deadline"),
        "observation": (view.get("last_observation") or {}).get("text"),
        "runs_dispatched": state.dispatched,
    }


def _outcome(ref: str, attempt_id: str, action: str, *, step: str, sha: str) -> dict[str, Any]:
    return {
        "status": "ok",
        "step": step,
        "pilot_ref": ref,
        "attempt_id": attempt_id,
        "action": action,
        "sha": sha,
    }


__all__ = [
    "E2E_FAILURE_REASON",
    "E2E_PHASE",
    "E2eProceed",
    "applies",
    "run_stage",
    "stage_request_id",
    "utcnow",
]
