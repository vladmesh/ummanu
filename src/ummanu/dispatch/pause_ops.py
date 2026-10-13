"""Pause, resume and pause-status for the production dispatcher.

Head transitions live here rather than in the tick because pause and resume are operator commands
that run between ticks: they take the production tick lock, so a tick in flight finishes before a
freeze stops its heads, and the next tick sees a settled flag.

**The action a pause command performed is decided under that lock, and travels out of it.** Inside
the lock `pause` already knows which of the two things it did -- it wrote the flag, or it found the
mode already held -- and `resume` knows which mode it lifted. That knowledge cannot be recovered
afterwards: a flag read taken before the command and another taken after are two moments the lock
separates, and between them another operator command can have set or cleared exactly the mode this
one intended, so the pair proves nothing about which command did it (secretary-1577). Rendering the
state afterwards is a second, independent thing that can fail on its own -- `pause_status` converts
every dispatcher record, and a production state this release cannot convert refuses there, after the
pause has taken. So the decision is a value made under the lock, and the render is applied to it by
:func:`_with_status`, which carries the decision out on :class:`PauseCommandCompleted` when the
render refuses rather than letting the second failure erase the first.

The render deliberately happens *outside* the lock: it walks the whole production state, and holding
the tick lock across it would make every corrupt-state read a contention problem on the dispatcher's
own lock.
"""

from __future__ import annotations

import os
import time
from typing import Any

from ummanu._fsutil import file_lock
from ummanu.dispatch.helpers import _last_marker
from ummanu.dispatch.launch import (
    REVIEW_ROLE,
    WORKER_ROLE,
    clear_launch_intent,
    confirm_launch_intent,
    forget_role_head,
    keep_reserved_round,
    launch_intent,
    launch_left_a_head,
    mark_launch_aborted,
    stop_launch_intent,
    write_launch_intent,
)
from ummanu.dispatch.observer import (
    freeze_observers,
    observer_snapshot,
    resume_observers,
)
from ummanu.dispatch.pause import (
    PAUSE_MODES,
    auto_resume_status,
    clear_legacy_mirror,
    normalize_pause_mode,
    on_resume_text,
    pause_payload,
    write_legacy_mirror,
)
from ummanu.dispatch.review import end_review_pane, start_review
from ummanu.dispatch.state import DispatcherRecord, now_rfc3339
from ummanu.dispatch.types import (
    STOPPED_BY_OPERATOR,
    DispatcherError,
    HeadLaunchAborted,
    HostError,
)
from ummanu.task_errors import TaskError

WORKER_MARKERS = {"report:done", "report:blocked"}
REVIEW_MARKERS = {"review:green", "review:red"}


class PauseCommandCompleted(DispatcherError):
    """A pause command that ran to completion, whose rendering of the state afterwards refused.

    It carries the `decision` the command made under the tick lock -- `step`, `action`, and for a
    resume the mode it lifted -- so a caller reports what happened instead of inferring it from
    observations taken outside that lock, which cannot establish it. That includes a command that
    deliberately changed nothing: `noop` is as much a decision made under the lock as `paused` is,
    and a repeat is exactly what an outside observation mistakes a real pause for. `cause` is the
    failure of the render, unchanged and chained.

    It is a `DispatcherError` spelling the cause's own code, message and exit status, so a caller
    that does not know this class -- `ummanu pause freeze` through `_run_production`, the tick's
    auto-resume -- answers exactly what it answered before this class existed. Only a caller that
    reads `decision` behaves differently, and reading it is the whole point.
    """

    def __init__(self, decision: dict[str, Any], cause: BaseException, *, warnings: list[str]) -> None:
        super().__init__(
            str(getattr(cause, "code", "") or "pause_status_unavailable"),
            str(getattr(cause, "message", "") or f"{type(cause).__name__}: {cause}"),
            int(getattr(cause, "exit_code", 1)),
        )
        self.decision = dict(decision)
        self.cause = cause
        #: What the command itself wanted the operator to hear, decided with the action.
        self.warnings = list(warnings)


def _with_status(
    runtime: Any,
    decision: dict[str, Any],
    *,
    warnings: list[str],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The command's own decision, over the state as `pause_status` reads it now.

    The two are separate on purpose. The decision is what this command did and is already made; the
    status is a description of the pipeline afterwards, assembled from every dispatcher record, and
    it can refuse for reasons that have nothing to do with the command. When it does, the decision
    leaves on :class:`PauseCommandCompleted` rather than being lost with it.
    """
    try:
        status = pause_status(runtime)
    except Exception as exc:  # any refusal of the render, over a command that took
        raise PauseCommandCompleted(decision, exc, warnings=warnings) from exc
    status["warnings"] = [*status.get("warnings", []), *warnings]
    return {**status, **(extra or {}), **decision}


def pause(
    runtime: Any,
    *,
    mode: str,
    actor: str,
    reason: str,
    exclude_workspaces: list[str] | None = None,
) -> dict[str, Any]:
    """Set the pipeline-wide pause. Freeze also stops the live heads of every tracked card.

    Idempotent in the same mode. Pausing in the other mode while already paused is refused: a drain
    quietly upgraded to a freeze would stop a head whose card the caller still expects to be riding
    its cycle. `exclude_workspaces` is the narrow initiator exception a freeze needs when the caller
    is itself a head; an excluded head is neither stopped nor relaunched on resume.
    """
    resolved = normalize_pause_mode(mode)
    if not resolved:
        raise DispatcherError(
            "validation", f"unknown pause mode {mode!r} (one of {', '.join(PAUSE_MODES)})", 2
        )
    actor = (actor or "").strip()
    reason = (reason or "").strip()
    if not actor:
        raise DispatcherError("validation", "pause requires a non-empty actor", 2)
    if not reason:
        raise DispatcherError("validation", "pause requires a non-empty reason", 2)

    warnings: list[str] = []
    with file_lock(runtime.production_state.tick_lock):
        current = runtime.pause.load()
        current_mode = normalize_pause_mode(current.get("mode"))
        if current_mode == resolved:
            # The action, and the only place it can be decided. A repeat and a pause that wrote the
            # flag are told apart by what the flag held at the instant this command was serialised
            # against every other one, which is here; the same two modes observed from outside the
            # lock are two moments another command fits between (secretary-1577).
            action = "noop"
        elif current_mode:
            raise DispatcherError(
                "pause_conflict",
                f"pipeline is already paused ({current_mode}), resume before pausing {resolved}",
                3,
            )
        else:
            action = "paused"
            since = now_rfc3339()
            stopped_worker: list[str] = []
            stopped_reviewer: list[str] = []
            stopped_observer: list[str] = []
            excluded: list[str] = []
            if resolved == "freeze":
                payload = runtime.production_state.load()
                if _state_unreadable(payload):
                    # The records cannot be read, so no head can be identified — and writing the state
                    # back would replace an unreadable file with an empty one. The flag still goes down:
                    # the next tick reads it and advances nothing, which is what a freeze is for.
                    warnings.append("production state is unreadable: the flag is set but no head was stopped")
                else:
                    records = runtime.production_state.records(payload)
                    # The freeze is a stop path like any other, and its stops have to be durable
                    # before the panes are touched (secretary-1412): an operator freeze interrupted
                    # half-way must still leave every head it had begun stopping named on its record,
                    # with `operator` as the initiator, or the next tick opens a second stop of a head
                    # nothing can identify.
                    with _freeze_state_committing(runtime, payload, records):
                        stopped_worker, stopped_reviewer, excluded = _freeze_heads(
                            runtime, records, _excluded_paths(exclude_workspaces)
                        )
                    # Observer heads stop with everything else, with the freeze's own reason on the
                    # record. The next tick after the resume brings them back.
                    observer_stops = freeze_observers(
                        runtime, payload, reason=f"pipeline freeze by {actor}: {reason}"
                    )
                    stopped_observer = observer_stops["stopped"]
                    if observer_stops["failed"]:
                        # The head is still alive and still on the books as a pending stop, which the
                        # frozen tick retries. The operator hears it now rather than from the log.
                        warnings.append(
                            "observer heads could not be stopped and are retried by the next tick: "
                            + ", ".join(observer_stops["failed"])
                        )
                    runtime.production_state.put_records(payload, records)
                    runtime.production_state.save(payload)
            mirror = write_legacy_mirror(mode=resolved, actor=actor, reason=reason, since=since)
            runtime.pause.save(
                pause_payload(
                    mode=resolved,
                    actor=actor,
                    reason=reason,
                    since=since,
                    stopped_worker=stopped_worker,
                    stopped_reviewer=stopped_reviewer,
                    stopped_observer=stopped_observer,
                    excluded_worker=excluded,
                    legacy_mirror=mirror,
                )
            )
    return _with_status(runtime, {"step": "pause", "action": action}, warnings=warnings)


def resume(runtime: Any, *, actor: str) -> dict[str, Any]:
    """Clear the pause and put back what a freeze stopped.

    A drain never stopped anything. A freeze relaunches the stopped worker and reviewer heads in
    their existing workspaces and hands every wait watchdog a fresh window. Report-first: a card
    whose head reported while frozen is left for the next tick to move instead of getting a fresh
    head launched into work that is already finished.
    """
    with file_lock(runtime.production_state.tick_lock):
        decision, report, warnings = _resume_under_lock(runtime, actor=actor)
    # Outside the lock, for the reason the module docstring gives: the render walks every record,
    # and what this resume did is already decided and travels with a render that refuses.
    return _with_status(runtime, decision, warnings=warnings, extra=report)


def auto_resume_expired_freeze(runtime: Any, *, source: str) -> dict[str, Any] | None:
    """Lift an automation-owned freeze that outlived its TTL. None when there is nothing to lift.

    A freeze set by automation expires, a freeze set by a person does not. `ummanu backup create`
    resumes in a `finally`, so a backup that is killed would otherwise leave the dispatcher frozen
    forever. Called by the tick, which already holds the tick lock, so this takes no lock of its own.
    """
    decision = auto_resume_status(runtime.pause.load())
    if not decision.get("eligible"):
        return None
    outcome = {**decision, "source": source}
    try:
        result = resume_locked(runtime, actor="auto-resume")
    except Exception as exc:  # noqa: BLE001 — a failed recovery is reported, it does not kill the tick
        # This caller is outside the pause protocol: it names the failure by its class rather than
        # by the code, message and exit status :class:`PauseCommandCompleted` preserves. So unwrap
        # it here, and the tick's auto-resume reports the render's own failure exactly as it did
        # before that class existed.
        surfaced = exc.cause if isinstance(exc, PauseCommandCompleted) else exc
        return {**outcome, "resumed": False, "error": f"{type(surfaced).__name__}: {surfaced}"}
    return {
        **outcome,
        "resumed": True,
        "relaunched": result.get("relaunched", []),
        "parked": result.get("parked", []),
        "skipped": result.get("skipped", []),
    }


def resume_locked(runtime: Any, *, actor: str) -> dict[str, Any]:
    """`resume` without taking the tick lock, for callers that already hold it.

    The render is inside their lock because they hold it for their own span; what they get back is
    the same document `resume` returns, and the same :class:`PauseCommandCompleted` when the render
    refuses over a resume that happened.
    """
    decision, report, warnings = _resume_under_lock(runtime, actor=actor)
    return _with_status(runtime, decision, warnings=warnings, extra=report)


def _resume_under_lock(runtime: Any, *, actor: str) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Do the resume, and say what it did: the decision, the report, and the warnings.

    The decision -- `resumed` or `noop`, and the mode a resume lifted -- is made here, where the
    caller holds the tick lock, and it is what a caller reports. The report beside it is the rest of
    this resume's own answer: the lists the relaunch produced and the legacy mirror it cleared.
    They are separate because they survive differently: the decision leaves on
    :class:`PauseCommandCompleted` when the state cannot be rendered afterwards, and the lists are
    part of the answer that never arrived.
    """
    state = runtime.pause.load()
    mode = normalize_pause_mode(state.get("mode"))
    if not mode:
        runtime.pause.clear()
        return {"step": "resume", "action": "noop"}, {}, []
    buckets: dict[str, list[str]] = {"relaunched": [], "parked": [], "skipped": []}
    warnings: list[str] = []
    if mode == "freeze":
        payload = runtime.production_state.load()
        if _state_unreadable(payload):
            warnings.append("production state is unreadable: the pause is lifted but no head was relaunched")
        else:
            records = runtime.production_state.records(payload)
            buckets = _resume_heads(runtime, state, records, payload)
            # The observers are not relaunched here: clearing their freeze marks hands them back to
            # the tick's reconciliation, which is the one bring-up path and already tells a dead
            # head from a live one.
            buckets["observers_resumed"] = resume_observers(payload)
            _refresh_watchdog_windows(records)
            runtime.production_state.put_records(payload, records)
            payload["resumed_at"] = now_rfc3339()
            payload["resumed_by"] = actor or runtime.owner
            runtime.production_state.save(payload)
    mirror = clear_legacy_mirror(state)
    runtime.pause.clear()
    return (
        {"step": "resume", "action": "resumed", "resumed_mode": mode},
        {"legacy_mirror": mirror, **buckets},
        warnings,
    )


def pause_status(runtime: Any) -> dict[str, Any]:
    """Pause state of the dispatcher that actually runs, with the flag's path in the live data
    plane. Per-card head lines say whether a head is missing because a freeze stopped it or because
    it is simply not up yet, so a pause never reads as a dead head."""
    state = runtime.pause.load()
    mode = normalize_pause_mode(state.get("mode"))
    payload = runtime.production_state.load()
    records = runtime.production_state.records(payload)
    stopped_worker = list(state.get("stopped_worker") or [])
    stopped_reviewer = list(state.get("stopped_reviewer") or [])
    excluded_worker = list(state.get("excluded_worker") or [])
    warnings: list[str] = []
    if state.get("corrupt"):
        warnings.append(f"pause file is unreadable and read as frozen: {runtime.pause.path}")
    out: dict[str, Any] = {
        "status": "ok",
        "step": "pause-status",
        "paused": bool(mode),
        "mode": mode,
        "pause_file": str(runtime.pause.path),
        "dispatcher": {
            "kind": "production",
            "phase": str(payload.get("phase") or "new"),
            "owner": str(payload.get("owner") or ""),
            "state_file": str(runtime.production_state.path),
        },
        "since": str(state.get("since") or ""),
        "actor": str(state.get("actor") or ""),
        "reason": str(state.get("reason") or ""),
        "stopped_worker": stopped_worker,
        "stopped_reviewer": stopped_reviewer,
        "stopped_observer": list(state.get("stopped_observer") or []),
        "excluded_worker": excluded_worker,
        "on_resume": on_resume_text(mode, stopped_worker, stopped_reviewer),
        # Operator-visible answer to "will this lift itself?": an automation-owned freeze expires,
        # a freeze a person set is a maintenance window and is held until they resume it.
        "auto_resume": auto_resume_status(state),
        "legacy_mirror": state.get("legacy_mirror") if isinstance(state.get("legacy_mirror"), dict) else {},
        "heads": head_lines(records),
        "observers": observer_snapshot(payload),
        "warnings": warnings,
    }
    return out


def head_lines(records: dict[str, DispatcherRecord]) -> list[dict[str, Any]]:
    """One line per tracked card, in reference order: the rule for what a missing head means.

    Public because it is a rule and there is one of it. The protocol layer
    (:mod:`ummanu.webproto.pause_reads`) reports the same lines from the same durable records,
    and a second walk over those records there would be a second answer to "is that head gone
    because a freeze stopped it".
    """
    return [_head_line(ref, record) for ref, record in sorted(records.items())]


def _head_line(ref: str, record: DispatcherRecord) -> dict[str, Any]:
    return {
        "ref": ref,
        "state": record.state,
        "worker": _head_state(record.handle or record.worker_leaf, record.paused_worker_at),
        "reviewer": _head_state(record.review_handle or record.review_leaf, record.paused_reviewer_at),
        "workspace": record.workspace,
    }


def _head_state(handle: str, paused_at: float) -> str:
    if handle:
        return "running"
    return "stopped-by-pause" if paused_at else "not-running"


def _state_unreadable(payload: dict[str, Any]) -> bool:
    return str(payload.get("phase") or "") == "unavailable"


def _excluded_paths(exclude_workspaces: list[str] | None) -> set[str]:
    return {os.path.abspath(os.path.expanduser(path)) for path in (exclude_workspaces or []) if path}


def _freeze_state_committing(runtime: Any, payload: dict[str, Any], records: dict[str, DispatcherRecord]):
    """Lend the host a flush of the freeze's own records, for the span it holds them."""

    def flush() -> None:
        runtime.production_state.put_records(payload, records)
        runtime.production_state.save(payload)

    return runtime.host.committing(flush)


def _freeze_heads(
    runtime: Any, records: dict[str, DispatcherRecord], excluded_paths: set[str]
) -> tuple[list[str], list[str], list[str]]:
    """Stop the live heads a freeze must stop, and mark why they are gone.

    `stop` only, never `teardown`: the worktree and everything uncommitted in it stays exactly as the
    head left it. The reviewer's pane is closed through its own lifecycle helper first.

    A head is here whenever anything still names it, the pid heartbeat included: a head adopted from
    a launch intent has no pane handle. A stop the host will not confirm leaves the record pointing
    at its head and its `paused_*` stamp unset, so resume will not relaunch a second one beside it.

    An unresolved launch intent is stopped first and on its own terms: between the host call and the
    record's save the intent is the only thing that names that head.
    """
    stopped_worker: list[str] = []
    stopped_reviewer: list[str] = []
    excluded: list[str] = []
    now = time.time()
    for ref, record in sorted(records.items()):
        if record.workspace and os.path.abspath(record.workspace) in excluded_paths:
            excluded.append(ref)
            continue
        intent = launch_intent(record)
        unresolved_role = ""
        if intent:
            role = str(intent.get("role") or WORKER_ROLE)
            if stop_launch_intent(runtime, record, intent, role) is not None:
                # An unconfirmed launch still owns its head: never mark that role stopped or
                # stop it a second time by a weaker identity. The other role is independent;
                # a failed reviewer launch must not leave a known worker running under freeze.
                unresolved_role = role
            else:
                # A rework's reserved round survives the head that never started.
                keep_reserved_round(runtime, record, intent)
                if role == REVIEW_ROLE:
                    record.paused_reviewer_at = now
                    stopped_reviewer.append(ref)
                else:
                    record.paused_worker_at = now
                    stopped_worker.append(ref)
        if unresolved_role != REVIEW_ROLE and (
            record.review_handle or record.review_leaf or record.review_pid_file
        ):
            try:
                end_review_pane(runtime.host, record, STOPPED_BY_OPERATOR)
            except HostError:
                # Keep the reviewer's identity and pause stamp untouched. Worker stop is
                # still owed, whether or not the reviewer host answered.
                pass
            else:
                record.paused_reviewer_at = now
                if ref not in stopped_reviewer:
                    stopped_reviewer.append(ref)
        if unresolved_role != WORKER_ROLE and (
            record.handle or record.worker_leaf or record.worker_pid_file
        ):
            try:
                runtime.host.stop_head(record, WORKER_ROLE, STOPPED_BY_OPERATOR)
            except HostError:
                # Keep the worker's identity so resume cannot launch beside it.
                pass
            else:
                forget_role_head(record, WORKER_ROLE)
                record.paused_worker_at = now
                if ref not in stopped_worker:
                    stopped_worker.append(ref)
    return stopped_worker, stopped_reviewer, excluded


def _resume_heads(
    runtime: Any,
    state: dict[str, Any],
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
) -> dict[str, list[str]]:
    relaunched: list[str] = []
    parked: list[str] = []
    skipped: list[str] = []

    for ref in list(state.get("stopped_reviewer") or []):
        record = records.get(ref)
        if record is None:
            skipped.append(f"{ref}:reviewer")
            continue
        if record.review_handle or record.review_leaf or launch_intent(record):
            # A resume that got this far and then failed to drop the flag is retried by the next
            # tick's TTL check; relaunching a head that is already back would double it. An
            # unresolved launch intent says the same thing with less certainty: the tick's own
            # recovery either adopts that head or drops it, and this is not the place to guess.
            parked.append(f"{ref}:reviewer")
            continue
        record.paused_reviewer_at = 0.0
        task = _load_task(runtime, ref)
        if task is None or task.get("state") != "validate":
            skipped.append(f"{ref}:reviewer")
            continue
        if _last_marker(task, record.review_baseline, REVIEW_MARKERS):
            # The reviewer posted its verdict before the freeze stopped it. The next tick acts on
            # that verdict; a fresh reviewer would review a card that is already judged.
            parked.append(f"{ref}:reviewer")
            continue
        outcome = start_review(
            runtime,
            task,
            records,
            record,
            record.attempt_id,
            action="review-resumed",
            payload=payload,
        )
        (relaunched if outcome.get("status") == "ok" else skipped).append(f"{ref}:reviewer")

    for ref in list(state.get("stopped_worker") or []):
        record = records.get(ref)
        if record is None:
            skipped.append(f"{ref}:worker")
            continue
        if record.handle or record.worker_leaf or launch_intent(record):
            parked.append(f"{ref}:worker")
            continue
        record.paused_worker_at = 0.0
        task = _load_task(runtime, ref)
        if task is None or task.get("state") != "in_progress" or not record.workspace:
            # Not a card that wants a worker head any more: moved on during the freeze, or its
            # worker was already frozen for review. Either way the tick owns it from here.
            parked.append(f"{ref}:worker")
            continue
        if _last_marker(task, record.comment_baseline, WORKER_MARKERS):
            parked.append(f"{ref}:worker")
            continue
        # Same durable launch contour as the tick's own bring-ups: the intent is on disk before
        # the head exists, so a resume that dies mid-relaunch is adopted rather than doubled.
        failure = write_launch_intent(
            runtime,
            payload,
            records,
            ref,
            record,
            role=WORKER_ROLE,
            action="worker-resume",
            head=record.head,
            workspace=record.workspace,
            task=task,
        )
        if failure is not None:
            skipped.append(f"{ref}:worker")
            continue
        try:
            launched = runtime.host.restart_worker(
                task,
                record,
                heartbeat_run_id=str((record.launch_intent or {}).get("run_id") or ""),
            )
        except Exception as exc:  # noqa: BLE001 — one failed relaunch must not strand the others
            if isinstance(exc, HeadLaunchAborted) or launch_left_a_head(record):
                # A pane was already open, or the heartbeat says a head of this relaunch is running
                # whatever the failure claimed. Its intent stays on disk with what is known of it,
                # and the next tick adopts or stops that head. Clearing it would hide it from both.
                aborted = (
                    exc
                    if isinstance(exc, HeadLaunchAborted)
                    else HeadLaunchAborted(str(exc), workspace=record.workspace)
                )
                mark_launch_aborted(runtime, payload, records, ref, record, aborted)
                skipped.append(f"{ref}:worker")
                continue
            # Left with no handle and a fresh watchdog window: the card stays In progress under the
            # ordinary wait watchdog, which respawns it once and then escalates to Blocked.
            clear_launch_intent(record)
            record.handle = ""
            record.worker_leaf = ""
            skipped.append(f"{ref}:worker")
            continue
        # The head is up, so its pane and launch snapshot are fixed on disk before the record is
        # told about them: everything left to do here can fail over a worker that already runs.
        confirm_launch_intent(
            runtime,
            payload,
            records,
            ref,
            record,
            handle=launched.handle,
            leaf=launched.leaf,
            run=launched.run,
            head_run=dict(launched.head_run),
        )
        record.handle = launched.handle
        record.worker_leaf = launched.leaf
        clear_launch_intent(record)
        record.state = "claimed"
        # A resume is a real bring-up: the round records the head that came back, which a registry
        # repin during the freeze can configure differently (secretary-716).
        runtime.record_worker_routing(task, record, launched.run)
        record.worker_started_at = record.worker_progress_at = time.time()
        relaunched.append(f"{ref}:worker")

    return {"relaunched": relaunched, "parked": parked, "skipped": skipped}


def _refresh_watchdog_windows(records: dict[str, DispatcherRecord]) -> None:
    """Restart every open wait clock at resume.

    A freeze advances nothing, so the ceilings kept running against a pipeline stopped on purpose.
    Without this the first tick after a long freeze reads the pause as silence and starts respawning
    and blocking cards whose heads were fine.
    """
    now = time.time()
    for record in records.values():
        if record.worker_waiting_since:
            record.worker_waiting_since = now
            record.worker_progress_at = now
            record.worker_idle_since = 0.0
        if record.review_waiting_since:
            record.review_waiting_since = now
            record.review_progress_at = now
            record.review_idle_since = 0.0
        if record.gate_pending_since:
            record.gate_pending_since = now


def _load_task(runtime: Any, ref: str) -> dict[str, Any] | None:
    try:
        return runtime.reader.show(ref)
    except TaskError:
        return None
