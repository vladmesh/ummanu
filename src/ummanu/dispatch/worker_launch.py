"""Worker bring-up and headless-worker recovery for the production dispatcher.

The Ready-card claim boundary ends with a durable DispatcherRecord.  This module owns the next
bounded responsibility: turn that durable claim into one running worker, recover the same bring-up
after a crash, and replace a provably headless worker on its retained checkout.

Later worker/report, red-continuation, gate, review and merge state machines deliberately remain on
DispatcherRuntime.  The runtime is passed as a collaborator until those adjacent responsibilities
are extracted; launch persistence, host operations and durable state stay on their existing narrow
dispatch package boundaries.
"""

from __future__ import annotations

import time
from typing import Any

from ummanu.dispatch import attempt_accounting
from ummanu.dispatch.helpers import (
    _spent_report_generations,
    _task_doc_report_generation,
    scrub_host_output,
)
from ummanu.dispatch.host import (
    LaunchedHead,
    _delivery_evidence_json,
    _record_worker_delivery_evidence,
)
from ummanu.dispatch.launch import (
    STAGE_CLAIM,
    STAGE_RESPAWN,
    WORKER_ROLE,
    bring_up_blocked_action as _bring_up_blocked_action,
    bring_up_blocked_reason as _bring_up_blocked_reason,
    bring_up_terminal_reason as _bring_up_terminal_reason,
    classify_bring_up_failure as _classify_bring_up_failure,
    clear_launch_intent as _clear_launch_intent,
    confirm_launch_intent as _confirm_launch_intent,
    launch_aborted as _launch_aborted,
    launch_delivery_receipt as _launch_delivery_receipt,
    launch_intent_unwritable as _launch_intent_unwritable,
    launch_left_a_head as _launch_left_a_head,
    launch_pid_file as _launch_pid_file,
    mark_launch_aborted as _mark_launch_aborted,
    write_launch_intent as _write_launch_intent,
)
from ummanu.dispatch.state import (
    DispatcherRecord,
    HeadlessRecoveryEpisode,
    PersistedHeadlessRecoveryEpisode,
    attempt_request_id as _attempt_request_id,
    claim_actual as _claim_actual,
    claim_mismatch as _claim_mismatch,
    record_divergence as _record_divergence,
)
from ummanu.dispatch.types import HeadLaunchAborted, HostError, OwnershipChanged
from ummanu.dispatch.watchdog import (
    head_process_status as _head_process_status,
    reset_idle as _reset_idle,
    reset_wait as _reset_wait,
)

#: Why a card standing in an active execution state without a live worker was refused a
#: replacement launch (secretary-1544). The key is the durable recovery error: it goes on the
#: record, into the tick outcome and into the card's own comment, so "In progress" is never the
#: only description of a card nobody is working on.
HEADLESS_RECOVERY_REASONS: dict[str, str] = {
    "workspace_missing": "the retained worker checkout is gone, so no candidate can be bound",
    "workspace_unbindable": (
        "the retained checkout is not this card's registered worktree on its own branch"
    ),
    "workspace_unreadable": "the retained checkout could not be read",
    "candidate_unknown": "the retained checkout names no candidate commit",
    "round_already_answered": (
        "the retained candidate's round already has an accepted worker report, so an active "
        "execution state owes no worker work; the continuation is a dispatcher-owned exact-SHA "
        "validation, which this path does not perform"
    ),
}
#: The blocked-reason taxonomy each recovery error is charged to.
_HEADLESS_RECOVERY_BLOCKED_REASON: dict[str, str] = {
    "round_already_answered": "operator",
}


def _tree_state_label(dirty: Any) -> str:
    """Say `unknown` for a tree nothing read, rather than calling it clean."""
    if dirty is None:
        return "unknown"
    return "dirty" if dirty else "clean"


def _headless_episode_token(record: DispatcherRecord) -> str:
    """The id discriminator that separates one headless episode of a card from the next.

    A card with no dispatcher record does not get a minted attempt id: production ticks it under the
    constant `production_adopt_attempt_id(ref)`, the same string for that card forever
    (`dispatch/production.py`). So an attempt-scoped request id is a *card*-scoped one here, and a
    second episode would replay the first episode's committed event instead of moving the board —
    the tick reporting a transition that did not happen (secretary-1544 round 5).

    The stamp this returns is dispatcher-owned and episode-scoped in both directions. It is written
    once, when the episode is first observed, and it survives every tick of that episode, so a tick
    that died between the board move and its own bookkeeping replays onto the same id and moves
    nothing twice. It does not survive the episode, because the refusal drops the record with it, so
    the next return of the same card mints a new one and gets its own move.

    Two parts, because neither alone is enough. The wall clock is what a replay must not disturb,
    but it has a resolution and two episodes of a fast-moving card could land inside it. The card's
    comment count cannot: a refusal writes its own move and reason onto the card, so the next
    episode is stamped strictly higher whatever the clock says.
    """
    episode = record.worker_headless.episode
    since = episode.since if episode is not None else 0.0
    comments = episode.comment_baseline if episode is not None else 0
    return f"episode-{since:.6f}-c{comments}" if since else "episode-unstamped"


def _headless_worker(record: DispatcherRecord) -> bool:
    """Whether this card owes a worker that nothing on the record can name or reach.

    Not an absence of health: an absence of *identity*. A record here has no pane, no leaf, no
    heartbeat path and no launch intent, so there is nothing for a watchdog to observe, nudge or
    replace, and nothing that could ever answer the report the tick would otherwise wait for.
    """
    continuation = record.worker_continuation
    return not (
        # `owns_head` is the project's own question — does anything here still have to be settled
        # before a replacement opens — and it is the right one: a HeadRun left on the record by a
        # stop that already forgot the pane and the heartbeat names a head nobody can reach.
        record.owns_head(WORKER_ROLE)
        or record.launch_intent
        or record.paused_worker_at
        or continuation.delivery_pending
        or continuation.delivery_confirmed
        or continuation.red_transition_pending
    )


def launch_worker_after_claim(
    runtime: Any,
    claimed: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    *,
    require_existing_workspace: bool = False,
) -> dict[str, Any]:
    ref = claimed["ref"]
    attempt_accounting.persist_outcome_round_context(runtime, claimed, record, phase="worker")
    mismatch = _claim_mismatch(claimed, record.worker, record.head, record.review_head)
    if mismatch:
        divergence = _record_divergence(
            payload,
            record.attempt_id,
            ref,
            "claim",
            "claim_live_mismatch",
            expected={
                "state": "in_progress",
                "worker": record.worker,
                "resolved_head": record.head,
                "resolved_review_head": record.review_head,
            },
            actual=_claim_actual(claimed),
            details=mismatch,
        )
        return {
            "status": "blocked",
            "step": "claim",
            "pilot_ref": ref,
            "attempt_id": record.attempt_id,
            "reason": "claim live board mismatch",
            "divergence_id": divergence["id"],
        }
    live_head = _head_process_status(_launch_pid_file(WORKER_ROLE, ref))
    if live_head.get("known") and live_head.get("alive"):
        # This record belongs to the claim being opened now, so it has no HeadRun that can prove
        # the pre-existing heartbeat is its own. Signalling that workspace would turn an absence
        # of ownership into permission to stop it: keep the claim and make the ambiguity visible.
        return {
            "status": "degraded",
            "step": "claim",
            "action": "orphan-worker-heartbeat-unbound",
            "pilot_ref": ref,
            "attempt_id": record.attempt_id,
            "reason": "a live worker heartbeat has no durable HeadRun binding for this claim",
        }
    # The workspace is asked of the host rather than taken from its answer: with it and the pid
    # file the next tick can stop a head whose handle a tick dying mid-launch never recorded.
    intent_failure = _write_launch_intent(
        runtime,
        payload,
        records,
        ref,
        record,
        role=WORKER_ROLE,
        action="claim",
        head=record.head,
        workspace=runtime.host.restore_workspace(claimed, record.worker),
        task=claimed,
    )
    if intent_failure is not None:
        if intent_failure.startswith("codex-fanout-policy:"):
            # No terminal was created. This is policy evidence, not a transient failure worth
            # retrying: a later tick with the same schema is the same prohibited launch.
            attempt_accounting.terminal_effect(runtime, 
                claimed,
                record,
                target="blocked",
                reason=f"Codex provider fan-out policy refused worker preflight: {intent_failure}",
                request_id=_attempt_request_id(record.attempt_id, "codex-fanout-blocked", ref),
                terminal_state="blocked",
                disposition="blocked",
                blocked_reason="provider",
            )
            records.pop(ref, None)
            runtime.save_records(payload, records)
            return {
                "status": "blocked",
                "step": "claim",
                "pilot_ref": ref,
                "attempt_id": record.attempt_id,
                "policy_evidence": {"kind": "codex_provider_fanout", "state": "unknown"},
                "reason": intent_failure,
            }
        return _launch_intent_unwritable(
            step="claim",
            ref=ref,
            attempt_id=record.attempt_id,
            role=WORKER_ROLE,
            reason=intent_failure,
        )
    # The launch intent already contains the exact preflight HeadRun. Bind its provider source
    # before `prepare_worker` can create a pane, not after TASK.md has been delivered.
    runtime.bind_codex_provider_ingress(
        record,
        records,
        payload,
        role=WORKER_ROLE,
        reference=ref,
    )
    try:
        snapshot_kwargs = (
            {"local_run_snapshot": record.worker_local_run_snapshot}
            if callable(getattr(runtime.host, "local_run_snapshot_for_round", None))
            else {}
        )
        prepared = runtime.host.prepare_worker(
            claimed,
            record.worker,
            record.head,
            attempt_id=record.attempt_id,
            require_existing_workspace=require_existing_workspace,
            generation=record.report_generation,
            failover=bool(record.preferred_head),
            heartbeat_run_id=str(dict(record.launch_intent).get("run_id") or ""),
            **snapshot_kwargs,
        )
    except (HeadLaunchAborted, HostError) as exc:
        aborted = _worker_launch_failure(runtime,
            payload, records, ref, record, exc, step="claim", attempt_id=record.attempt_id
        )
        if aborted is not None:
            return aborted
        _clear_launch_intent(record)
        # An infrastructure outcome blocks for a person; it is not a new attempt.
        failure = _classify_bring_up_failure(
            exc, record, WORKER_ROLE, stage=STAGE_CLAIM, attempt_id=record.attempt_id
        )
        reason = _bring_up_blocked_reason(
            "dispatcher bring-up failed", exc, failure=failure
        )
        attempt_accounting.terminal_effect(runtime, 
            claimed,
            record,
            target="blocked",
            reason=reason,
            request_id=_attempt_request_id(
                record.attempt_id, _bring_up_blocked_action("bringup-blocked", failure), ref
            ),
            terminal_state="blocked",
            disposition="blocked",
            blocked_reason=_bring_up_terminal_reason(failure),
        )
        records.pop(ref, None)
        runtime.save_records(payload, records)
        return {
            "status": "blocked",
            "step": "claim",
            "pilot_ref": ref,
            "reason": "host bring-up failed",
            **failure.outcome_fields(reason),
        }
    record.workspace = prepared["workspace"]
    _record_worker_delivery_evidence(record, prepared.get("delivery_evidence"))
    # The intent carries the pane, the launch snapshot and this head's own run before the record
    # is told anything else: from here every failure is one over a worker that is already running.
    # The delivery receipt goes with them, because a recovery of this launch has to be able to
    # tell a worker that received its TASK pointer from one whose composer swallowed it.
    _confirm_launch_intent(
        runtime,
        payload,
        records,
        ref,
        record,
        handle=str(prepared.get("handle") or ""),
        leaf=str(prepared.get("leaf") or ""),
        run=prepared.get("run"),
        head_run=dict(prepared.get("head_run") or {}),
        delivery=_launch_delivery_receipt(prepared.get("delivery_evidence")),
    )
    try:
        _settle_worker_pane(runtime,
            ref,
            record,
            str(prepared.get("handle") or ""),
            str(prepared.get("leaf") or ""),
        )
    except HeadLaunchAborted as exc:
        return _worker_launch_aborted(runtime,
            payload, records, ref, record, exc, step="claim", attempt_id=record.attempt_id
        )
    record.worker_started_at = record.worker_progress_at = time.time()
    record.state = "claimed"
    resume_workspaces = payload.get("resume_workspaces")
    if isinstance(resume_workspaces, dict):
        resume_workspaces.pop(ref, None)
    records[ref] = record
    runtime.save_records(payload, records)
    # The worker is up: record the head running it from the launcher's own snapshot. An adopted
    # claim predating routing telemetry has no round, so this opens one from the journal. Spend
    # the intent only once that lands: a refusal leaves the head adoptable and its routing owed.
    runtime.record_worker_routing(claimed, record, prepared.get("run"))
    _clear_launch_intent(record)
    runtime.save_records(payload, records)
    runtime.writer.comment(
        role="dispatcher",
        actor=runtime.owner,
        reference=ref,
        body=(
            f"Production dispatcher claimed {ref}, attempt {record.attempt_id}, "
            f"worker {record.worker}, workspace {prepared['workspace']}."
        ),
        request_id=_attempt_request_id(record.attempt_id, "claimed-comment", ref),
    )
    outcome = {
        "status": "ok",
        "step": "claim",
        "pilot_ref": ref,
        "attempt_id": record.attempt_id,
        "worker": record.worker,
        "workspace": prepared["workspace"],
        "head": record.head,
        "review_head": record.review_head,
    }
    if record.preferred_head or record.preferred_review_head:
        # The tick says a head was substituted in the same line that says the card was claimed:
        # an operator must not have to open the card to see the work runs elsewhere.
        outcome["preferred_head"] = record.preferred_head
        outcome["preferred_review_head"] = record.preferred_review_head
    return outcome


def _worker_launch_aborted(
    runtime: Any,
    payload: dict[str, Any],
    records: dict[str, DispatcherRecord],
    ref: str,
    record: DispatcherRecord,
    exc: HeadLaunchAborted,
    *,
    step: str,
    attempt_id: str,
) -> dict[str, Any]:
    """A worker bring-up that failed with its terminal already open."""
    _mark_launch_aborted(runtime, payload, records, ref, record, exc)
    return _launch_aborted(
        step=step,
        ref=ref,
        attempt_id=record.attempt_id or attempt_id,
        role=WORKER_ROLE,
        reason=scrub_host_output(str(exc)),
    )


def _worker_launch_failure(
    runtime: Any,
    payload: dict[str, Any],
    records: dict[str, DispatcherRecord],
    ref: str,
    record: DispatcherRecord,
    exc: Exception,
    *,
    step: str,
    attempt_id: str,
) -> dict[str, Any] | None:
    """The aborted-launch outcome when this failure may have left a worker running, else None."""
    if isinstance(exc, OwnershipChanged):
        # The write-ahead intent remains evidence for normal reconciliation.
        # A preparation refusal cannot block the card its new owner just moved.
        return {"status": "skipped", "step": step, "pilot_ref": ref, "reason": str(exc)}
    _record_worker_delivery_evidence(record, exc, failure=True)
    if not isinstance(exc, HeadLaunchAborted):
        if not _launch_left_a_head(record):
            return None
        exc = HeadLaunchAborted(
            str(exc),
            workspace=record.workspace,
            pid_file=_launch_pid_file(WORKER_ROLE, ref),
            evidence=_delivery_evidence_json(exc, "worker-launch"),
        )
    return _worker_launch_aborted(runtime,
        payload, records, ref, record, exc, step=step, attempt_id=attempt_id
    )


def _settle_worker_pane(runtime: Any, ref: str, record: DispatcherRecord, handle: str, leaf: str) -> None:
    """Put the pane identity of a worker head that is already up onto its record."""
    record.handle = handle
    record.worker_leaf = leaf


def bring_up_worker_head(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    *,
    step: str,
    stage: str,
    blocked_reason: str,
    blocked_action: str,
    blocked_request_suffix: str = "",
) -> tuple[LaunchedHead | None, dict[str, Any] | None]:
    """Relaunch this card's worker in its own workspace, under the intent already on disk.

    The blocked transition is named rather than handed in whole, because the action token is
    where the outcome's class becomes durable: only the shared classifier below decides which
    of the two tokens this relaunch writes.
    """
    ref = task["ref"]
    try:
        runtime._require_head_ready(record.head)
        runtime.bind_codex_provider_ingress(
            record,
            records,
            payload,
            role=WORKER_ROLE,
            reference=ref,
        )
        launched = runtime.host.restart_worker(
            task, record, heartbeat_run_id=str(dict(record.launch_intent).get("run_id") or "")
        )
    except Exception as exc:  # noqa: BLE001 — classified by what it left running, not by type
        aborted = _worker_launch_failure(runtime,
            payload, records, ref, record, exc, step=step, attempt_id=attempt_id
        )
        if aborted is not None:
            return None, aborted
        _clear_launch_intent(record)
        return None, runtime._block_failed_worker_restart(
            ref=ref,
            record=record,
            records=records,
            payload=payload,
            attempt_id=attempt_id,
            step=step,
            stage=stage,
            reason=blocked_reason,
            action=blocked_action,
            request_suffix=blocked_request_suffix,
            error=exc,
        )
    # The head is up. Its pane, launch configuration and own run go into the intent before
    # anything else, so an adoption gets the run that launched rather than a fresh identity.
    _confirm_launch_intent(
        runtime,
        payload,
        records,
        ref,
        record,
        handle=launched.handle,
        leaf=launched.leaf,
        run=launched.run,
        head_run=dict(launched.head_run),
        delivery=_launch_delivery_receipt(launched.delivery_evidence),
    )
    _record_worker_delivery_evidence(record, launched.delivery_evidence)
    try:
        _settle_worker_pane(runtime,ref, record, launched.handle, launched.leaf)
    except HeadLaunchAborted as exc:
        return None, _worker_launch_aborted(runtime,
            payload, records, ref, record, exc, step=step, attempt_id=attempt_id
        )
    return launched, None


def write_worker_relaunch_intent(
    runtime: Any,
    payload: dict[str, Any],
    records: dict[str, DispatcherRecord],
    ref: str,
    record: DispatcherRecord,
    *,
    action: str,
    round_number: int | None = None,
    task: dict[str, Any] | None = None,
) -> str | None:
    """Fix a rework or respawn bring-up on disk before `restart_worker` is called."""
    return _write_launch_intent(
        runtime,
        payload,
        records,
        ref,
        record,
        role=WORKER_ROLE,
        action=action,
        head=record.head,
        workspace=record.workspace,
        round_number=round_number,
        task=task,
    )


def resolve_headless_worker(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
) -> dict[str, Any] | None:
    """Settle a card standing in an active execution state with no worker at all.

    The situation this closes (secretary-1544, field evidence codegen-orchestrator-1232): a card
    is returned from Blocked straight into In progress. `_adopt` rebuilds a record from the
    board, finds no heartbeat to bind, and — because the board claim is the old one — the audit
    holds no claim event for the freshly synthesised attempt either, so the record settled
    ``adopted`` with an empty handle and the tick waited for a report from a worker that was
    stopped hours ago. Returns None when this card is not in that situation.

    Exactly one of three things is durably established before the card is left active:

    * a verified live worker identity — bound by `_adopt` from the worker's own heartbeat, in
      which case this is never reached;
    * a replacement launch intent bound to the retained workspace and its exact candidate,
      written to disk before the host is called and adopted by the next tick if this one dies;
    * a refusal that puts the card back in Blocked with a named recovery error.

    This path has no launch intent and no delivery receipt to consult — an adopted record is
    rebuilt from the board, and there is no evidence of what any head was ever handed. What it
    rests on instead is the board's own consumed report markers and the retained checkout's
    TASK.md round record: a checkout whose document belongs to a round the board has already
    consumed a report for owes no worker work, and relaunching over it would either redo an
    answered round or invent a document for one nobody opened.
    """
    ref = task["ref"]
    if not _headless_worker(record):
        if record.worker_headless:
            record.worker_headless = PersistedHeadlessRecoveryEpisode()
            records[ref] = record
            runtime.save_records(payload, records)
        return None
    # A living heartbeat this record cannot prove is its own is ambiguity, not permission: the
    # same refusal `_launch_worker_after_claim` makes over an unbound orphan. Never relaunch
    # beside it and never signal it.
    live = _head_process_status(_launch_pid_file(WORKER_ROLE, ref))
    state = runtime.host.retained_workspace_state(task, record)
    prior_headless = record.worker_headless.episode
    record.worker_headless = PersistedHeadlessRecoveryEpisode(
        HeadlessRecoveryEpisode(
            since=(prior_headless.since if prior_headless is not None else 0.0) or time.time(),
        # Where the card stood when this episode opened. Read once and carried, so it is the
        # episode's own discriminator rather than whatever the board says on a later tick.
        comment_baseline=(
            prior_headless.comment_baseline
            if prior_headless is not None and prior_headless.comment_baseline
            else len(task.get("comments") or [])
        ),
        record_state=record.state,
        handle_known=False,
        heartbeat=str(live.get("state") or "") or "absent",
        workspace=str(state.get("workspace") or ""),
        branch=str(state.get("branch") or ""),
        expected_branch=str(state.get("expected_branch") or ""),
        dirty=state.get("dirty") if isinstance(state.get("dirty"), bool) else None,
        candidate_sha=str(state.get("sha") or ""),
            report_generation=record.report_generation,
            recovery_error="",
        )
    )
    if live.get("known") and live.get("alive"):
        record.worker_headless["recovery_error"] = "orphan_heartbeat_unbound"
        records[ref] = record
        runtime.save_records(payload, records)
        return {
            "status": "degraded",
            "step": "advance",
            "pilot_ref": ref,
            "attempt_id": record.attempt_id or attempt_id,
            "action": "orphan-worker-heartbeat-unbound",
            "recovery_error": "orphan_heartbeat_unbound",
            "reason": "a live worker heartbeat has no durable HeadRun binding for this card",
        }
    refusal = _headless_recovery_refusal(runtime,task, record, state)
    record.worker_headless["recovery_error"] = refusal
    records[ref] = record
    runtime.save_records(payload, records)
    if refusal:
        return _refuse_headless_worker(runtime,task, record, records, payload, attempt_id, refusal)
    return _relaunch_headless_worker(runtime,task, record, records, payload, attempt_id, state)


def _headless_recovery_refusal(
    runtime: Any, task: dict[str, Any], record: DispatcherRecord, state: dict[str, Any]
) -> str:
    """The recovery error that forbids a replacement launch here, or "" when one is owed."""
    if not state.get("bound"):
        return str(state.get("reason") or "workspace_unbindable")
    if record.state != "adopted":
        # A record that lived through its own launch knows the round it is in; only a record
        # rebuilt from the board has to read the round off the checkout and the markers.
        return ""
    document_generation = _task_doc_report_generation(str(state.get("workspace") or ""))
    if document_generation and document_generation <= _spent_report_generations(task):
        # The checkout holds the document of a round whose report the board has already
        # consumed. Out of this card's scope: the continuation for an unchanged candidate with
        # no worker work left is dispatcher-owned exact-SHA validation (issue:3a0b263f), and
        # inventing a worker round here would be the same fiction from the other side.
        return "round_already_answered"
    return ""


def _relaunch_headless_worker(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    state: dict[str, Any],
) -> dict[str, Any]:
    """Put a replacement worker on the retained checkout, on its branch and its exact SHA."""
    ref = task["ref"]
    episode_token = _headless_episode_token(record)
    # The intent is bound to the checkout that was verified above, not to a fresh one: the
    # workspace is passed through rather than re-derived, so a replacement can never land in a
    # different worktree than the candidate it was decided on.
    record.workspace = str(state.get("workspace") or record.workspace)
    failure = write_worker_relaunch_intent(runtime,
        payload, records, ref, record, action="headless-worker-recovery", task=task
    )
    if failure is not None:
        return _launch_intent_unwritable(
            step="advance",
            ref=ref,
            attempt_id=record.attempt_id or attempt_id,
            role=WORKER_ROLE,
            reason=failure,
        )
    launched, failed = bring_up_worker_head(runtime,
        task,
        record,
        records,
        payload,
        attempt_id,
        step="advance",
        stage=STAGE_RESPAWN,
        blocked_reason="headless worker recovery bring-up failed",
        blocked_action="headless-worker-recovery-blocked",
    )
    if launched is None:
        assert failed is not None
        return failed
    now = time.time()
    record.state = "claimed"
    # The replacement never saw whatever the stopped head was asked for, so it owes no answer.
    record.worker_answer_owed_since = 0.0
    record.worker_started_at = record.worker_progress_at = now
    _reset_wait(record, "worker")
    _reset_idle(record, "worker")
    runtime.record_worker_routing(task, record, launched.run)
    _clear_launch_intent(record)
    headless = dict(record.worker_headless)
    record.worker_headless = PersistedHeadlessRecoveryEpisode()
    records[ref] = record
    runtime.save_records(payload, records)
    runtime.writer.comment(
        role="dispatcher",
        actor=runtime.owner,
        reference=ref,
        body=(
            f"Dispatcher headless recovery: {ref} stood in an active state with no worker "
            f"identity. Relaunched on the retained checkout {record.workspace} "
            f"(branch {state.get('branch') or '(unknown)'}, candidate "
            f"{state.get('sha') or '(unknown)'}, "
            f"tree {_tree_state_label(state.get('dirty'))}) for report generation "
            f"{record.report_generation}. Nothing was recreated, reset or re-seeded."
        ),
        request_id=_attempt_request_id(
            record.attempt_id or attempt_id,
            "headless-worker-recovery",
            ref,
            # The episode, not the generation: an adopted record's attempt id is a constant and
            # two episodes can share a generation, which would suppress the second comment.
            f"{record.report_generation}-{episode_token}",
        ),
    )
    return {
        "status": "ok",
        "step": "advance",
        "pilot_ref": ref,
        "attempt_id": record.attempt_id or attempt_id,
        "action": "headless-worker-replacement-launched",
        "workspace": record.workspace,
        "branch": state.get("branch") or "",
        "candidate_sha": state.get("sha") or "",
        "dirty": state.get("dirty"),
        "headless_since": headless.get("since"),
    }


def _refuse_headless_worker(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    attempt_id: str,
    recovery_error: str,
) -> dict[str, Any]:
    """Refuse the active-state transition and put the card back in Blocked, saying why."""
    ref = task["ref"]
    headless = dict(record.worker_headless)
    explanation = HEADLESS_RECOVERY_REASONS.get(recovery_error, recovery_error)
    unconfirmed = runtime._stop_worker_confirmed(record, ref, step="advance", attempt_id=attempt_id)
    if unconfirmed is not None:
        records[ref] = record
        runtime.save_records(payload, records)
        return unconfirmed
    detail = ""
    if headless.get("workspace"):
        detail = (
            f" Retained checkout {headless['workspace']}"
            f" (branch {headless.get('branch') or '(unbound)'},"
            f" candidate {headless.get('candidate_sha') or '(unreadable)'})."
        )
    attempt_accounting.terminal_effect(runtime, 
        task,
        record,
        target="blocked",
        reason=(
            f"headless worker recovery refused ({recovery_error}): {explanation}.{detail}"
            " The card is returned to Blocked rather than left in an active state with no"
            " worker."
        ),
        # Episode-scoped, or a card returned twice would replay the first refusal's committed
        # event: no board move, no comment, and a tick still reporting `blocked`.
        request_id=_attempt_request_id(
            record.attempt_id or attempt_id,
            f"headless-recovery-{recovery_error}",
            ref,
            _headless_episode_token(record),
        ),
        terminal_state="blocked",
        disposition="blocked",
        blocked_reason=_HEADLESS_RECOVERY_BLOCKED_REASON.get(recovery_error, "infrastructure"),
    )
    records.pop(ref, None)
    runtime.save_records(payload, records)
    return {
        "status": "blocked",
        "step": "advance",
        "pilot_ref": ref,
        "attempt_id": record.attempt_id or attempt_id,
        "action": "headless-worker-recovery-refused",
        "recovery_error": recovery_error,
        "reason": explanation,
        "workspace": headless.get("workspace") or "",
        "candidate_sha": headless.get("candidate_sha") or "",
        "to": "blocked",
    }
