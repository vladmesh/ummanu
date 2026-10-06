"""Review launch recovery helpers for dispatcher runtimes."""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Any

from ummanu.dispatch import attempt_accounting
from ummanu.dispatch.helpers import scrub_host_output
from ummanu.dispatch.launch import (
    LAUNCH_DELIVERY_MAX_ATTEMPTS,
    REVIEW_ROLE,
    STAGE_REVIEW,
    WORKER_ROLE,
    BringUpFailure,
    bring_up_blocked_action,
    bring_up_blocked_reason,
    bring_up_terminal_reason,
    busy_launch_delivery,
    classify_bring_up_failure,
    clear_launch_intent,
    confirm_launch_intent,
    defer_busy_launch_delivery,
    defer_launch_delivery,
    forget_role_head,
    launch_aborted,
    launch_intent_unwritable,
    launch_left_a_head,
    mark_launch_aborted,
    pane_state_label,
    undelivered_launch_delivery,
    write_launch_intent,
)
from ummanu.dispatch.state import DispatcherRecord
from ummanu.dispatch.state import attempt_request_id as _attempt_request_id
from ummanu.dispatch.tui import (
    DELIVERY_RECEIPT_REFUSED,
    READINESS_BLOCKED,
    READINESS_BUSY,
    delivery_readiness_state,
)
from ummanu.dispatch.types import (
    STOPPED_BY_DISPATCHER,
    HeadLaunchAborted,
    HostError,
)
from ummanu.dispatch.watchdog import (
    head_run_process_status as _head_run_process_status,
)
from ummanu.dispatch.watchdog import (
    heartbeat_is_dead as _heartbeat_is_dead,
)
from ummanu.dispatch.watchdog import (
    heartbeat_is_live_match as _heartbeat_is_live_match,
)
from ummanu.dispatch.watchdog import (
    heartbeat_is_mismatch as _heartbeat_is_mismatch,
)
from ummanu.dispatch.watchdog import (
    initial_output_stall_seconds as _initial_output_stall_seconds,
)
from ummanu.dispatch.watchdog import (
    pid_file_path as _pid_file_path,
)
from ummanu.dispatch.watchdog import (
    review_infra_retry_attempts as _review_infra_retry_attempts,
)
from ummanu.dispatch.watchdog import (
    review_launch_abort_stuck_ticks as _review_launch_abort_stuck_ticks,
)
from ummanu.dispatch.watchdog import (
    wait_cycle_token as _wait_cycle_token,
)
from ummanu.dispatch.worker_lifecycle import head_run_binding
from ummanu.runtime.head import HeadRun, HeadRunError
from ummanu.runtime.head_runtime_backends import head_runtime_name
from ummanu.runtime.head_runtimes import LOCAL_PTY_RUNTIME


def candidate_sha(record: DispatcherRecord) -> str:
    """The checkout the green gate attested, as the typed receipt itself recorded it."""
    receipt = record.gate_attestation.receipt
    return receipt.validated_sha if receipt is not None else ""


def review_infrastructure_retry(
    runtime: Any,
    task: dict[str, Any],
    records: dict[str, DispatcherRecord],
    record: DispatcherRecord,
    attempt_id: str,
    *,
    payload: dict[str, Any],
    reason: str,
) -> dict[str, Any] | None:
    """Hold a green candidate over a reviewer that could not be started. None past the ceiling.

    A reviewer pane that will not split says nothing about the code, and the candidate behind it is
    still the one a green exact-SHA gate accepted. Nothing here decides anything about the candidate:
    the record stays exactly as the green gate left it and the state goes back to `review_starting`,
    which is the one state whose recovery launches a reviewer and only a reviewer. Only the count
    moves, and past the ceiling the caller blocks the card for an operator.
    """
    ref = task["ref"]
    if record.gate_state != "green":
        # No green candidate to preserve. Whatever failed here belongs to the ordinary failure
        # path, which is where the caller goes when this answers None.
        return None
    attempts = record.review_infra_failures + 1
    limit = _review_infra_retry_attempts()
    record.review_infra_failures = attempts
    record.review_infra_error = reason
    record.state = "review_starting"
    records[ref] = record
    runtime.save_records(payload, records)
    if attempts >= limit:
        return None
    sha = candidate_sha(record)
    return {
        "status": "degraded",
        "step": "review",
        "pilot_ref": ref,
        "attempt_id": record.attempt_id or attempt_id,
        "action": "review-infrastructure-retry",
        "attempts": attempts,
        "candidate_sha": sha,
        "report_generation": record.report_generation,
        "reason": (
            f"the reviewer could not be started over the green candidate "
            f"{sha[:12] or '(sha unavailable)'}; this is a review-stage infrastructure failure, "
            f"not a verdict, so the gate receipt, the worker report and the held worker session "
            f"stay and retry {attempts} of {limit} launches the reviewer again: {reason}"
        ),
    }


def review_infrastructure_blocked_reason(
    record: DispatcherRecord, reason: str, failure: BringUpFailure
) -> str:
    """What the operator reads when a reviewer bring-up will not be retried.

    The green candidate's own sentence is the reviewer-specific half and stays: the card was never
    reworked and its receipt still stands, so it is the reviewer that gets relaunched. The class,
    the cause and the evidence beneath it come from the shared classifier, in the same words the
    worker path writes and the same words this tick's outcome carries.
    """
    if not failure.infrastructure:
        return f"review bring-up failed: {reason}\n{failure.clause()}"
    sha = candidate_sha(record)
    return (
        f"reviewer infrastructure failed on {record.review_infra_failures} consecutive launch "
        f"attempts over a green candidate; the card was never reworked and its gate receipt for "
        f"{sha or '(sha unavailable)'} still stands, so relaunch the reviewer rather than the "
        f"worker: {reason}\n{failure.clause()}"
    )


def review_infrastructure_failure(
    runtime: Any,
    task: dict[str, Any],
    records: dict[str, DispatcherRecord],
    record: DispatcherRecord,
    attempt_id: str,
    *,
    payload: dict[str, Any],
    reason: str,
    outcome_reason: str,
    exc: BaseException | None = None,
) -> dict[str, Any]:
    """One bounded path for a proven-headless reviewer bring-up failure over a green candidate.

    The classification is the worker path's, made by the same call: `exc` is the failure as the host
    raised it where there is one, and the preflight cases that have no exception (a head resource
    that is not ready, a launch intent that could not be written) pass their `reason` as evidence.
    Only an infrastructure outcome is held for another launch — a card whose own bring-up contract
    is broken is not made whole by relaunching a reviewer over it.
    """
    ref = task["ref"]
    failure = classify_bring_up_failure(
        exc,
        record,
        REVIEW_ROLE,
        stage=STAGE_REVIEW,
        attempt_id=record.attempt_id or attempt_id,
        detail=reason,
    )
    if failure.infrastructure:
        held = review_infrastructure_retry(
            runtime, task, records, record, attempt_id, payload=payload, reason=reason
        )
        if held is not None:
            return dict(held, **failure.outcome_fields(held["reason"]))
    blocked_reason = review_infrastructure_blocked_reason(record, reason, failure)
    attempt_accounting.terminal_effect(runtime, 
        task,
        record,
        target="blocked",
        reason=blocked_reason,
        request_id=_attempt_request_id(
            record.attempt_id or attempt_id,
            bring_up_blocked_action("review-blocked", failure),
            ref,
            _wait_cycle_token(record),
        ),
        terminal_state="blocked",
        disposition="blocked",
        blocked_reason=bring_up_terminal_reason(failure),
    )
    records[ref] = record
    runtime.save_records(payload, records)
    return {
        "status": "blocked",
        "step": "review",
        "pilot_ref": ref,
        "reason": outcome_reason,
        **failure.outcome_fields(blocked_reason),
    }


def command_terminal_status(
    host: Any, task: dict[str, Any], record: DispatcherRecord, *, kind: str
) -> dict[str, Any]:
    """Return one role's liveness, read from its pid heartbeat and its exact-run provider cursor.

    No pane is read: every head the dispatcher raises is supervised and owns none, and a legacy
    Orca record (`head_runtime_backends.is_legacy_record`) is read through the same pid heartbeat
    and never through a pane inventory (A20 step 6, secretary-1723). The reason words are the ones
    the wait watchdog and the vitality reduction already read: `pid` for a proved process,
    `missing-terminal` for one the heartbeat does not prove, whatever the runtime.
    """
    if host.mode == "noop":
        return {"known": True, "live": True, "reason": "noop"}
    if not record.workspace:
        raise HostError(f"{kind} workspace is unavailable")
    run = record.review_head_run if kind == "review" else record.worker_head_run
    leaf = record.review_leaf if kind == "review" else record.worker_leaf
    pid_status = _head_run_process_status(
        _pid_file_path(kind, task["ref"]),
        run=run,
        role=kind,
        task=f"card:{task['ref']}",
        leaf=leaf,
    )
    if _heartbeat_is_mismatch(pid_status):
        return {
            "known": True,
            "live": True,
            "reason": "heartbeat-identity-mismatch",
            "identity_mismatch": True,
            "pid_confirmed": False,
        }
    if _heartbeat_is_live_match(pid_status):
        # The classification rides along (a suspended head must be seen as Suspended, never aged
        # as Unverifiable), and the pid answers the process axis alone: no pane flag exists on
        # this shape.
        status = {
            "known": True,
            "live": True,
            "reason": "pid",
            "pid_confirmed": True,
            "pid_status": dict(pid_status),
        }
        if _supervised(run):
            # A local-pty head's provider cursor is an exact-run file read, and without it the
            # episode had no channel that sees the head work: it aged on the pid alone and was
            # held healthy only by child processes, inside their ceiling (secretary-1703's working
            # worker read `suspected_stall`, then `confirmed_stall`, secretary-1719). A legacy
            # record keeps the provider-less shape, whose darkness the episode records
            # (secretary-1543).
            status["provider_progress"] = _provider_progress_for_status(host, task, record, kind)
            # The head's own supervisor journal answers the Turn axis (secretary-1739): whether a
            # turn is open, since when the head has been at its prompt, and whether new screen
            # content was journalled. Without it a head that ended its turn and sat idle was held
            # healthy by child processes and aged only on the provider's quiet.
            journal = _supervisor_journal_for_status(host, task, record, kind, run)
            if journal is not None:
                status["supervisor_journal"] = journal
        child_activity = _head_child_activity(host, pid_status)
        if child_activity is not None:
            status["child_activity"] = child_activity
        return status
    if _heartbeat_is_dead(pid_status):
        # The heartbeat names a gone process: the reclaim is evidence-backed, so the
        # classification rides along and the reduction sees Dead.
        death = {
            "known": True,
            "live": False,
            "reason": "missing-terminal",
            "pid_status": dict(pid_status),
        }
        if _supervised(run):
            loss_reader = getattr(host, "head_loss_reason", None)
            if callable(loss_reader):
                try:
                    loss_reason = loss_reader(run)
                except Exception:  # noqa: BLE001 - a journal read cannot override the heartbeat
                    loss_reason = None
                if loss_reason == "memory_limit":
                    death["head_loss_reason"] = loss_reason
        return death
    if not pid_status.get("known"):
        # `pid_file_path`'s own contract: the dispatcher clears the pid file before every fresh
        # launch and the new head writes it "the moment it starts", so a respawn opens a window in
        # which the heartbeat has not been written yet. The observer
        # path already grants a launch grace window for exactly this reading (`observer_alive`); the
        # worker/reviewer path did not, so a watchdog tick landing in that window read a live,
        # just-(re)launched head as missing-terminal and, being the second such tick, escalated
        # straight to Blocked (secretary-1158).
        started_at = record.review_started_at if kind == "review" else record.worker_started_at
        if started_at and time.time() - started_at <= _initial_output_stall_seconds():
            return {"known": True, "live": True, "reason": "pid-not-written-yet", "pid_confirmed": False}
    # An unproven heartbeat is observation failure, not death.
    return {
        "known": True,
        "live": bool(pid_status.get("alive")) if pid_status.get("known") else False,
        "reason": "missing-terminal",
        "pid_status": dict(pid_status),
    }


def _head_child_activity(host: Any, pid_status: Any) -> dict[str, Any] | None:
    """The head's child processes, for a pid the heartbeat proved is this run (secretary-1692).

    Asked only of an exact live match -- a foreign or dead pid has no children of this run -- and
    only of a host that can answer. A read that fails is left out of the status entirely: the
    child source is extra evidence of work, and its absence changes no other channel's meaning.
    """
    if not isinstance(pid_status, dict) or not _heartbeat_is_live_match(pid_status):
        return None
    probe = getattr(host, "head_children", None)
    if probe is None:
        return None
    try:
        evidence = probe(int(pid_status.get("pid") or 0))
    except Exception:  # noqa: BLE001 - an observation failure is not evidence about the head
        return None
    if not isinstance(evidence, dict) or str(evidence.get("state") or "") != "observed":
        return None
    return evidence


def _provider_progress_for_status(
    host: Any, task: dict[str, Any], record: DispatcherRecord, kind: str
) -> dict[str, Any]:
    """This role's exact-run provider cursor off the host, inside the status admission fence."""
    try:
        provider_progress = getattr(
            host, "provider_progress", lambda _task, _record, _kind: {"state": "unavailable"}
        )(task, record, kind)
    except Exception:  # noqa: BLE001 — status probes can fail through any provider adapter
        provider_progress = {"state": "unavailable", "reason": "provider-progress probe failed"}
    return _admitted_provider_progress_for_status(
        provider_progress,
        record.review_head_run if kind == "review" else record.worker_head_run,
    )


def _supervisor_journal_for_status(
    host: Any, task: dict[str, Any], record: DispatcherRecord, kind: str, run: Any
) -> dict[str, Any] | None:
    """This role's journal reading, bound to the run on the record; `None` from a host with none.

    A reading that names another run is not this head's: it is handed on as unavailable, never as
    an answer, and a probe that raised is a channel that did not answer.
    """
    probe = getattr(host, "supervisor_journal", None)
    if probe is None:
        return None
    try:
        reading = probe(task, record, kind)
    except Exception:  # noqa: BLE001 - an observation failure is not evidence about the head
        return {"state": "unavailable", "reason": "supervisor journal probe failed"}
    if not isinstance(reading, dict):
        return {"state": "unavailable", "reason": "invalid supervisor journal reading shape"}
    run_id = str((run or {}).get("run_id") or "") if isinstance(run, dict) else ""
    if str(reading.get("state") or "") == "observed" and str(reading.get("run_id") or "") != run_id:
        return {"state": "unavailable", "reason": "supervisor journal reading names another HeadRun"}
    return reading


def _supervised(run: Any) -> bool:
    """Whether a record's persisted HeadRun is held by a local-pty supervisor rather than a pane."""
    try:
        return head_runtime_name(HeadRun.from_json(run)) == LOCAL_PTY_RUNTIME
    except (HeadRunError, AttributeError, KeyError, TypeError, ValueError):
        return False


def _admitted_provider_progress_for_status(value: Any, run: Any) -> dict[str, Any]:
    """Keep shared worker/reviewer liveness inside the same exact-HeadRun admission fence.

    The generic status seam receives a host response as data, so it must verify the response's run
    binding before it lets an opaque cursor renew either role's watchdog.
    """
    if not isinstance(value, dict):
        return {"state": "unavailable", "reason": "invalid provider-progress shape"}
    result: dict[str, Any] = {
        "state": str(value.get("state") or "unavailable")[:40],
        "reason": scrub_host_output(str(value.get("reason") or ""))[:240],
    }
    for name, limit in (
        ("admission", 40),
        ("source", 80),
        ("source_fingerprint", 64),
        ("cursor", 240),
        ("head_run_id", 120),
        ("head_run_fingerprint", 64),
    ):
        if name in value:
            result[name] = str(value.get(name) or "")[:limit]
    if "observed_at" in value:
        result["observed_at"] = str(value.get("observed_at") or "")[:64]
    if result["state"] != "observed" or result.get("admission") != "accepted":
        return result
    run_id, fingerprint = head_run_binding(run)
    if not run_id:
        return {
            "state": "unavailable",
            "reason": "persisted HeadRun is unavailable for provider-progress admission",
        }
    if result.get("head_run_id") != run_id or result.get("head_run_fingerprint") != fingerprint:
        return {
            "state": "identity_mismatch",
            "reason": "provider-progress observation does not name the persisted HeadRun",
        }
    source_fingerprint = str(result.get("source_fingerprint") or "")
    if (
        not result.get("source")
        or not result.get("cursor")
        or len(source_fingerprint) != 32
        or any(character not in "0123456789abcdef" for character in source_fingerprint.lower())
    ):
        return {
            "state": "unavailable",
            "reason": "provider-progress source admission is incomplete",
        }
    return result


def end_review_pane(host: Any, record: DispatcherRecord, initiator: str = STOPPED_BY_DISPATCHER) -> None:
    """Close the reviewer's pane and forget it. Used wherever the reviewer's lifecycle ends on its own
    — a red verdict, a respawn after a silent reviewer — so the next bring-up cannot mistake a stale
    handle for a live pane, and so the worker's workspace survives untouched.

    `initiator` is who is ending it and every caller names one. The pane pointers are dropped
    afterwards; the run itself stays on the record, because the initiator it carries is what makes
    the stop readable after the head is gone.

    A stop the host will not confirm raises, and the record keeps pointing at that reviewer: every
    caller opens something in the same checkout right after.
    """
    host.stop_review(record, initiator)
    record.review_handle = ""
    record.review_leaf = ""
    record.review_pid_file = ""
    record.review_commit = ""


def recover_review_launch(
    runtime: Any,
    task: dict[str, Any],
    records: dict[str, DispatcherRecord],
    record: DispatcherRecord,
    attempt_id: str,
    *,
    payload: dict[str, Any],
) -> dict[str, Any]:
    ref = task["ref"]
    if record.review_provider_hold:
        # The held reviewer was stopped, confirmed, before the hold was written, and nothing has
        # been launched since: there is no head whose liveness could say otherwise.
        return start_review(
            runtime, task, records, record, attempt_id, action="review-restarted", payload=payload
        )
    try:
        status = runtime.host.review_status(task, record)
    except Exception as exc:  # noqa: BLE001 — preserve ambiguous launches after any host failure
        # Inventory silence cannot prove the reviewer is absent. Preserve launch ambiguity and ask
        # the same liveness question next tick; never launch beside a possibly-live head.
        return {
            "status": "degraded",
            "step": "review",
            "pilot_ref": ref,
            "attempt_id": record.attempt_id or attempt_id,
            "action": "review-inventory-unavailable",
            "reason": scrub_host_output(str(exc)),
        }
    if status.get("identity_mismatch"):
        # A readable live heartbeat can still name a foreign process.  Do not adopt it into the
        # review state or reset the launch episode; this record remains the un-attributed run.
        return {
            "status": "degraded",
            "step": "review",
            "pilot_ref": ref,
            "attempt_id": record.attempt_id or attempt_id,
            "action": "review-heartbeat-identity-mismatch",
            "reason": "review heartbeat names a live process with a mismatching launch identity",
        }
    if status.get("live"):
        record.state = "reviewing"
        # A reviewer is on the checkout: whatever stuck launches came before belong to an episode
        # that is over, so the abort ceiling starts fresh for the next one (issue:aa9a8ae4), and so
        # does the infrastructure hold this card may have been retrying under (secretary-1401).
        record.review_launch_aborts = 0
        record.review_infra_failures = 0
        record.review_infra_error = ""
        return {
            "status": "ok",
            "step": "review",
            "pilot_ref": ref,
            "attempt_id": attempt_id,
            "action": "waiting-review-verdict",
        }
    return start_review(
        runtime, task, records, record, attempt_id, action="review-restarted", payload=payload
    )


def _reviewer_launch_aborted(
    runtime: Any,
    task: dict[str, Any],
    records: dict[str, DispatcherRecord],
    ref: str,
    record: DispatcherRecord,
    attempt_id: str,
    exc: HeadLaunchAborted,
    *,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """The ambiguous reviewer bring-up: its pane is open and nothing can say the head is gone.

    "No reviewer exists" is exactly what cannot be claimed here, so the intent stays on disk with
    what the failure knew of that head. Blocking the card and dropping the record instead would leave
    a live reviewer with nothing pointing at it.
    """
    evidence = getattr(exc, "evidence", None)
    if hasattr(evidence, "to_json"):
        evidence = evidence.to_json()
    busy = delivery_readiness_state(exc) == READINESS_BUSY
    if busy:
        # The pane was observed working before any document nudge was sent.  Its live heartbeat is
        # not launch confirmation, so retain the exact run and schedule the delivery retry on the
        # intent before it reaches launch recovery.
        defer_busy_launch_delivery(record, evidence if isinstance(evidence, dict) else {})
    mark_launch_aborted(runtime, payload, records, ref, record, exc)
    record.state = "review_starting"
    if busy:
        records[ref] = record
        runtime.save_records(payload, records)
        delivery = busy_launch_delivery(record.launch_intent)
        delay = max(0, int(float(delivery.get("next_at") or 0.0) - time.time()))
        return {
            "status": "degraded",
            "step": "review",
            "pilot_ref": ref,
            "attempt_id": record.attempt_id or attempt_id,
            "action": "review-launch-busy",
            "head": record.review_head,
            "attempts": int(delivery.get("attempts") or 0),
            "reason": (
                "the reviewer pane was busy before its document nudge was sent; its exact launch "
                f"intent is retained and retry is due in {delay}s"
            ),
        }
    # Count this abort and, once it has repeated past the ceiling, pull an operator in once.
    # The record and its intent are untouched — a head may still be running, so this never
    # blocks or drops the card — but a launch that cannot freeze its worker for this many ticks
    # is no longer a transient the steward's degraded line covers on its own (issue:aa9a8ae4).
    record.review_launch_aborts += 1
    _escalate_stuck_review_launch(runtime, task, record, attempt_id)
    records[ref] = record
    runtime.save_records(payload, records)
    return launch_aborted(
        step="review",
        ref=ref,
        attempt_id=record.attempt_id or attempt_id,
        role=REVIEW_ROLE,
        reason=scrub_host_output(str(exc)),
    )


def retry_busy_reviewer_launch_delivery(
    runtime: Any,
    task: dict[str, Any],
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    record: DispatcherRecord,
    intent: dict[str, Any],
    step: str,
) -> dict[str, Any] | None:
    """Retry an unaccepted reviewer nudge, over its exact live launch, before it can be adopted.

    "Unaccepted" is wider than "busy": a pane that was held in a dialog, one still starting its MCP
    servers and one that left the pointer sitting in its composer are the same fact here — the
    reviewer has not received the document — and they retry the *same* immutable pointer, at the
    same path, over the same run, rather than a rebuilt or duplicated one.

    A launch heartbeat only proves that the pane exists. Until the document nudge is confirmed it
    does not permit the adoption side effects: freezing the worker, recording routing, clearing the
    intent or setting the review lifecycle. The intent is also the retry's durable cursor.
    """
    delivery = undelivered_launch_delivery(intent)
    if not delivery:
        return None
    if int(delivery.get("attempts") or 0) >= LAUNCH_DELIVERY_MAX_ATTEMPTS:
        # The retry is bounded like everything else here. Past the ceiling the launch-recovery path
        # owns the head: it refuses the adoption and replaces the reviewer rather than nudging a
        # pane that has not taken a pointer in five attempts.
        return None
    ref = task["ref"]
    now = time.time()
    next_at = float(delivery.get("next_at") or 0.0)
    if next_at and now < next_at:
        state = str(delivery.get("state") or "")
        return {
            "status": "degraded",
            "step": step,
            "pilot_ref": ref,
            "attempt_id": record.attempt_id,
            # The outcome says which state is holding the pointer. A pane that was working and a
            # pane that never accepted the document are both deferred here and are not the same
            # fact, so they are not reported under the same word.
            "action": ("review-launch-busy" if state == READINESS_BUSY else "review-launch-undelivered"),
            "head": str(intent.get("head") or record.review_head),
            "readiness": state,
            "attempts": int(delivery.get("attempts") or 0),
            "reason": (
                "the reviewer document nudge is still deferred while its exact pane is "
                f"{pane_state_label(state)}; retry is due in {max(0, int(next_at - now))}s"
            ),
        }
    bind_ingress = getattr(runtime, "bind_codex_provider_ingress", None)
    if callable(bind_ingress):
        bind_ingress(record, records, payload, role=REVIEW_ROLE, reference=ref)
    try:
        retried = runtime.host.nudge_review_delivery(task, record, intent)
    except Exception as exc:  # noqa: BLE001 — evidence is the delivery boundary's contract
        evidence = getattr(exc, "evidence", None)
        if hasattr(evidence, "to_json"):
            evidence = evidence.to_json()
        if not isinstance(evidence, dict):
            evidence = {}
        _record_review_delivery_failure(record, exc)
        state = delivery_readiness_state(exc)
        if state == READINESS_BUSY:
            delay = defer_busy_launch_delivery(record, evidence, now=now)
            records[ref] = record
            runtime.save_records(payload, records)
            attempts = int(busy_launch_delivery(record.launch_intent).get("attempts") or 0)
            return {
                "status": "degraded",
                "step": step,
                "pilot_ref": ref,
                "attempt_id": record.attempt_id,
                "action": "review-launch-busy",
                "head": str(intent.get("head") or record.review_head),
                "attempts": attempts,
                "reason": (
                    "the reviewer pane remained busy before its document nudge was sent; its "
                    f"exact launch intent is retained and retry {attempts} waits {delay}s"
                ),
            }
        # Do not silently convert an unavailable, malformed or stale probe into busy.  The intent
        # stays on disk with the typed evidence; on the following tick the existing live-launch
        # recovery path owns its conservative stop/adoption decision.  The attempt is counted and
        # backed off under the same schedule, so an unreachable pane is bounded rather than retried
        # every tick until somebody notices.
        defer_launch_delivery(record, evidence, state=state or READINESS_BLOCKED, now=now)
        records[ref] = record
        runtime.save_records(payload, records)
        return {
            "status": "degraded",
            "step": step,
            "pilot_ref": ref,
            "attempt_id": record.attempt_id,
            "action": "review-launch-delivery-unavailable",
            "head": str(intent.get("head") or record.review_head),
            "readiness": state,
            "reason": "the retained reviewer launch could not be nudged; its typed delivery "
            "evidence is retained for normal launch recovery",
        }
    if not isinstance(retried, dict):
        raise HostError("reviewer delivery retry returned no durable result")
    evidence = retried.get("delivery_evidence")
    head_run = retried.get("head_run")
    # A started turn with the document still in the composer is not a reviewer receipt, and it is
    # not an ambiguity either: the pointer was looked for and found sitting there. It is recorded
    # as the determinate refusal it is rather than as a pane that was busy.
    if (
        isinstance(evidence, dict)
        and bool(evidence.get("turn_confirmed"))
        and bool(evidence.get("payload_left_in_composer"))
    ):
        record.review_delivery_evidence = dict(evidence)
        delay = defer_launch_delivery(record, evidence, state=DELIVERY_RECEIPT_REFUSED, now=now)
        records[ref] = record
        runtime.save_records(payload, records)
        attempts = int(undelivered_launch_delivery(record.launch_intent).get("attempts") or 0)
        return {
            "status": "degraded",
            "step": step,
            "pilot_ref": ref,
            "attempt_id": record.attempt_id,
            "action": "review-launch-undelivered",
            "head": str(intent.get("head") or record.review_head),
            "attempts": attempts,
            "reason": (
                "the reviewer transport observed a turn but the document remained in the "
                f"composer; its exact launch intent is retained and retry waits {delay}s"
            ),
        }
    if not isinstance(evidence, dict) or not bool(evidence.get("turn_confirmed")):
        raise HostError("reviewer delivery retry returned without confirmation evidence")
    if not isinstance(head_run, dict) or not head_run.get("run_id"):
        raise HostError("reviewer delivery retry returned without the launched head run")
    if evidence:
        record.review_delivery_evidence = dict(evidence)
    # Persist accepted delivery with exact run before reviewer adoption.
    confirmed = {
        **delivery,
        "state": "confirmed",
        "next_at": 0.0,
        "confirmed_at": now,
        "evidence": dict(evidence),
    }
    confirm_launch_intent(
        runtime,
        payload,
        records,
        ref,
        record,
        handle=str(retried.get("handle") or intent.get("handle") or ""),
        leaf=str(retried.get("leaf") or intent.get("leaf") or ""),
        head_run=dict(head_run),
        delivery=confirmed,
    )
    return None


def _record_review_delivery_failure(record: DispatcherRecord, exc: Exception) -> None:
    """Keep a reviewer prompt that did not land as durable card telemetry.

    Only a failure the delivery boundary evidenced counts: a split that would not open is a bring-up
    failure, not a prompt that was refused. What is kept is what the boundary saw, and it is never
    reset by a later reviewer, so a card cannot report that every prompt landed once one finally does.
    """
    evidence = getattr(exc, "evidence", None)
    if hasattr(evidence, "to_json"):
        evidence = evidence.to_json()
    if not isinstance(evidence, dict) or not evidence:
        return
    record.review_delivery_evidence = dict(evidence)
    typed = record.review_delivery_evidence.evidence
    # Preserve a successful receipt when a later reviewer-launch step aborts.
    if typed is not None and not typed.turn_confirmed and delivery_readiness_state(typed) != READINESS_BUSY:
        record.review_delivery_failures += 1


def _escalate_stuck_review_launch(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord,
    attempt_id: str,
) -> None:
    """Comment once when a reviewer launch has aborted past the stuck ceiling.

    The abort keeps the record on purpose, which is also what makes the loop silent. Past the ceiling
    this leaves one durable, operator-addressed note on the card. The request id is stable within the
    stuck episode and distinct across episodes, so the board carries one note per episode.
    """
    ceiling = _review_launch_abort_stuck_ticks()
    if record.review_launch_aborts < ceiling:
        return
    ref = task["ref"]
    # Keep idempotent abort-comment bodies stable across ticks.
    runtime.writer.comment(
        role="dispatcher",
        actor=runtime.owner,
        reference=ref,
        body=(
            f"⚠️ Reviewer launch has aborted for at least {ceiling} ticks running and is not "
            "recovering on its own: the reviewer pane came up and the bring-up could not be "
            "finished over it, so the card is stuck before review. An operator should look, and "
            "the dispatcher tick's own reason field carries what each attempt failed on."
        ),
        request_id=_attempt_request_id(
            record.attempt_id or attempt_id,
            "review-launch-stuck",
            ref,
            _wait_cycle_token(record),
        ),
    )


def _walk_review_provider_hold(
    runtime: Any,
    task: dict[str, Any],
    records: dict[str, DispatcherRecord],
    record: DispatcherRecord,
    attempt_id: str,
    *,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    """A reviewer held for a provider: the head it launches on now, or the hold outcome (secretary-1799).

    The card stays in Validate with no reviewer and nothing is counted while no head of the
    reviewer's chain can run; the first tick one can, the hold is released onto that head and the
    ordinary launch below proceeds. None means "launch now".
    """
    from ummanu.dispatch.provider_failure import review_hold_choice

    ref = task["ref"]
    choice = review_hold_choice(runtime, task, record)
    if choice is None:
        record.state = "review_starting"
        records[ref] = record
        runtime.save_records(payload, records)
        return {
            "status": "degraded",
            "step": "review",
            "pilot_ref": ref,
            "attempt_id": record.attempt_id or attempt_id,
            "action": "review-provider-unavailable",
            "head": record.review_head,
            "reason": record.review_provider_hold,
        }
    held = record.review_provider_hold
    record.review_head = choice.head
    record.preferred_review_head = choice.preferred if choice.substituted else ""
    record.review_provider_hold = ""
    runtime.writer.comment(
        role="dispatcher",
        actor=runtime.owner,
        reference=ref,
        body=(
            f"Provider recovered (reviewer): {choice.head} can be launched ({choice.reason}), so the "
            f"reviewer held since `{held}` is launched on it now."
        ),
        request_id=_attempt_request_id(
            record.attempt_id or attempt_id,
            "review-provider-hold-released",
            ref,
            f"{choice.head}-{int(time.time())}",
        ),
    )
    return None


def _review_chain_switch(
    runtime: Any, task: dict[str, Any], record: DispatcherRecord, attempt_id: str, readiness: Any
) -> Any:
    """Move the reviewer onto the head its role's chain resolves to now; the readiness to launch on.

    The walk starts from the role's preferred head (the card's override or the role default), so a
    fallback reviewer returns to the primary when that is launchable again. A walk with nothing
    launchable leaves the record alone and answers with the reason naming every refused resource.
    """
    from ummanu.dispatch.provider_failure import resolve_role_chain, same_family_review_note

    ref = task["ref"]
    try:
        choice = resolve_role_chain(runtime, task, kind="review")
    except HostError:
        return readiness
    if not choice.resolved:
        return replace(readiness, reason=choice.reason) if len(choice.rejected) > 1 else readiness
    if choice.head == record.review_head:
        return choice.readiness
    previous = record.review_head
    record.review_head = choice.head
    record.preferred_review_head = choice.preferred if choice.substituted else ""
    runtime.writer.comment(
        role="dispatcher",
        actor=runtime.owner,
        reference=ref,
        body=(
            f"Reviewer head: {choice.head} instead of {previous} ({choice.reason})."
            + (same_family_review_note(runtime, record, choice.head) if choice.substituted else "")
        ),
        request_id=_attempt_request_id(
            record.attempt_id or attempt_id,
            "review-head-chain",
            ref,
            f"{choice.head}-{record.review_baseline}",
        ),
    )
    return choice.readiness


def start_review(
    runtime: Any,
    task: dict[str, Any],
    records: dict[str, DispatcherRecord],
    record: DispatcherRecord,
    attempt_id: str,
    *,
    action: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    ref = task["ref"]
    if record.review_provider_hold:
        held = _walk_review_provider_hold(runtime, task, records, record, attempt_id, payload=payload)
        if held is not None:
            return held
    try:
        readiness = runtime.head_readiness(record.review_head)
    except HostError as exc:
        if record.gate_state != "green":
            raise
        record.state = "review_starting"
        return review_infrastructure_failure(
            runtime,
            task,
            records,
            record,
            attempt_id,
            payload=payload,
            reason=scrub_host_output(str(exc)),
            outcome_reason="review resource check failed",
            exc=exc,
        )
    if not readiness.launch_allowed or record.preferred_review_head:
        # The claimed reviewer is only the first one to try (ummanu-108): a red resource walks the
        # role's chain to the other family, and a reviewer that was substituted goes back to the
        # primary once its resource is green again. Decided per launch, never mid-run.
        readiness = _review_chain_switch(runtime, task, record, attempt_id, readiness)
    if not readiness.launch_allowed:
        record.state = "review_starting"
        if record.gate_state == "green":
            return review_infrastructure_failure(
                runtime,
                task,
                records,
                record,
                attempt_id,
                payload=payload,
                reason=readiness.reason,
                outcome_reason="review resource unavailable",
            )
        return {
            "status": "skipped",
            "step": "head-preflight",
            "action": "review-resource-not-ready",
            "pilot_ref": ref,
            "head": record.review_head,
            "readiness": readiness.to_json(),
            "reason": readiness.reason,
        }
    # Persist reviewer intent before pane split to prevent a crash-era duplicate.
    intent_kwargs: dict[str, Any] = {}
    prompt_document_path = getattr(runtime.host, "_prompt_document_path", None)
    if callable(prompt_document_path):
        # The real host writes the review packet outside the checkout. Its preflight descriptor
        # must carry that same pointer, not the historical in-worktree placeholder.
        intent_kwargs["document"] = str(prompt_document_path(REVIEW_ROLE, ref, record.review_baseline))
    failure = write_launch_intent(
        runtime,
        payload,
        records,
        ref,
        record,
        role=REVIEW_ROLE,
        action=action,
        head=record.review_head,
        workspace=record.workspace,
        task=task,
        **intent_kwargs,
    )
    if failure is not None:
        if failure.startswith("codex-fanout-policy:"):
            attempt_accounting.terminal_effect(runtime, 
                task,
                record,
                target="blocked",
                reason=f"Codex provider fan-out policy refused reviewer preflight: {failure}",
                request_id=_attempt_request_id(
                    record.attempt_id or attempt_id, "codex-fanout-review-blocked", ref
                ),
                terminal_state="blocked",
                disposition="blocked",
                blocked_reason="provider",
            )
            records.pop(ref, None)
            runtime.save_records(payload, records)
            return {
                "status": "blocked",
                "step": "review",
                "pilot_ref": ref,
                "policy_evidence": {"kind": "codex_provider_fanout", "state": "unknown"},
                "reason": failure,
            }
        record.state = "review_starting"
        if record.gate_state == "green":
            return review_infrastructure_failure(
                runtime,
                task,
                records,
                record,
                attempt_id,
                payload=payload,
                reason=failure,
                outcome_reason="review launch intent unavailable",
            )
        return launch_intent_unwritable(
            step="review",
            ref=ref,
            attempt_id=record.attempt_id or attempt_id,
            role=REVIEW_ROLE,
            reason=failure,
        )
    # The durable intent owns the exact pre-pane HeadRun and provider binding.
    bind_ingress = getattr(runtime, "bind_codex_provider_ingress", None)
    if callable(bind_ingress):
        bind_ingress(record, records, payload, role=REVIEW_ROLE, reference=ref)
    try:
        launch = runtime.host.start_review(task, record)
    except Exception as exc:  # noqa: BLE001 — classify every host launch refusal
        # Normalize and persist prompt evidence once; infrastructure failures carry none.
        _record_review_delivery_failure(record, exc)
        if isinstance(exc, HeadLaunchAborted):
            return _reviewer_launch_aborted(
                runtime, task, records, ref, record, attempt_id, exc, payload=payload
            )
        if launch_left_a_head(record):
            # A live exact heartbeat preserves intent for adoption or fenced stop.
            mark_launch_aborted(
                runtime,
                payload,
                records,
                ref,
                record,
                HeadLaunchAborted(str(exc), workspace=record.workspace),
            )
            record.state = "review_starting"
            return launch_aborted(
                step="review",
                ref=ref,
                attempt_id=record.attempt_id or attempt_id,
                role=REVIEW_ROLE,
                reason=scrub_host_output(str(exc)),
            )
        clear_launch_intent(record)
        if record.gate_state == "green":
            # No-head reviewer failures share one infrastructure transition and counter.
            return review_infrastructure_failure(
                runtime,
                task,
                records,
                record,
                attempt_id,
                payload=payload,
                reason=scrub_host_output(str(exc)),
                outcome_reason="host review failed",
                exc=exc,
            )
        # No green candidate to hold. The classification is the same call the worker path makes,
        # so the card's reason, transition class and tick outcome are one statement.
        failure = classify_bring_up_failure(
            exc,
            record,
            REVIEW_ROLE,
            stage=STAGE_REVIEW,
            attempt_id=record.attempt_id or attempt_id,
        )
        blocked_reason = bring_up_blocked_reason(
            "review bring-up failed", exc, failure=failure
        )
        attempt_accounting.terminal_effect(runtime, 
            task,
            record,
            target="blocked",
            reason=blocked_reason,
            request_id=_attempt_request_id(
                record.attempt_id or attempt_id,
                bring_up_blocked_action("review-blocked", failure),
                ref,
                _wait_cycle_token(record),
            ),
            terminal_state="blocked",
            disposition="blocked",
            blocked_reason=bring_up_terminal_reason(failure),
        )
        records.pop(ref, None)
        return {
            "status": "blocked",
            "step": "review",
            "pilot_ref": ref,
            "reason": "host review failed",
            **failure.outcome_fields(blocked_reason),
        }
    # Persist reviewer pane, launch snapshot, and HeadRun together before record adoption.
    confirm_launch_intent(
        runtime,
        payload,
        records,
        ref,
        record,
        handle=launch.handle,
        leaf=launch.leaf,
        run=launch.run,
        head_run=dict(launch.head_run),
    )
    record.review_handle = launch.handle
    record.review_leaf = launch.leaf
    record.review_commit = launch.commit
    if launch.delivery_evidence:
        # Successful and refused reviewer launches use the same durable, metadata-only receipt.
        # A later recovery therefore sees the actual transport version and submit count instead
        # of assuming the pane received the review because the split succeeded.
        record.review_delivery_evidence = dict(launch.delivery_evidence)
    # The verdict this pane issues belongs to this head, so the round records it now, from the
    # launcher's own snapshot (secretary-716). The intent is spent only once that has landed: a
    # journal that refuses here leaves the reviewer adoptable, and the adoption writes the routing
    # event the round would otherwise never get for it.
    runtime.record_review_routing(task, record, launch.run)
    clear_launch_intent(record)
    record.review_started_at = record.review_progress_at = time.time()
    # The reviewer took the checkout, so any stuck-launch episode before it is over and its abort
    # ceiling starts fresh for the next one (issue:aa9a8ae4), as does any infrastructure hold the
    # card was retrying under (secretary-1401).
    record.review_launch_aborts = 0
    record.review_infra_failures = 0
    record.review_infra_error = ""
    # A retained worker is suspended, not gone: it keeps its pane and its heartbeat so a red
    # verdict can continue that same conversation, and the reviewer still judges a checkout
    # nothing is editing. Without retention the worker head was shut down for the reviewer, and
    # the record must stop naming a pane that no longer exists.
    if not record.worker_continuation.retained:
        forget_role_head(record, WORKER_ROLE)
    record.state = "reviewing"
    outcome = {
        "status": "ok",
        "step": "review",
        "pilot_ref": ref,
        "attempt_id": attempt_id,
        "action": action,
    }
    if launch.fallback_reason:
        outcome["reviewer_fallback_reason"] = launch.fallback_reason
    return outcome
