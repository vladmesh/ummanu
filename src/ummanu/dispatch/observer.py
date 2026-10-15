"""Observer lifecycle: one head per open sprint, reconciled each production tick.

Open sprints launch or retain their observer; confirmed-dead heads may be replaced,
and closed sprints are stopped. Observers never claim cards.

Persist launch intent before calling the host, and stage lifecycle events before
their effects. Keep ambiguous or refused stops and failed post-pane launches
addressable; do not equate an unreadable heartbeat with death.

Sent deliveries use an acknowledgement deadline. Bound provider evidence wins
over pane idleness; the legacy turn ceiling applies only without attested source.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from ummanu.board import owner_events
from ummanu.board.tick_snapshot import select_sprints
from ummanu.codex_provider_events import CodexProviderSourceError
from ummanu.dispatch.heartbeat import sprint_task
from ummanu.dispatch.launch import merge_launch_head_run
from ummanu.dispatch.observer_audit import (
    _OBSERVER_SLICE_ATTEMPTS as _OBSERVER_SLICE_ATTEMPTS,
    _cursor_refs as _cursor_refs,
    _event_age_seconds as _event_age_seconds,
    _event_id as _event_id,
    _event_index as _event_index,
    _linked_refs as _linked_refs,
    _timestamp as _timestamp,
    _with_audit as _with_audit,
    commit_event as commit_event,
    commit_staged_event as commit_staged_event,
    discard_event as discard_event,
    observer_request_id as observer_request_id,
    record_event as record_event,
    stage_event as stage_event,
)
from ummanu.dispatch.observer_prompt import (
    OBSERVER_PROMPT_FILE as OBSERVER_PROMPT_FILE,
    OBSERVER_SKILL,
    _executor_lines as _executor_lines,
    _observer_comments,
    _observer_sprint_context as _observer_sprint_context,
    _production_lines as _production_lines,
    observer_launch_prompt as observer_launch_prompt,
    render_observer_prompt,
)
from ummanu.dispatch.observer_state import (
    DeliveryStage,
    ObserverDelivery,
    ObserverRecord,
    ObserverWakeLiveness,
    QuietStopJudgement,
    _float as _float,
    _int as _int,
    delivery_evidence_summary as delivery_evidence_summary,
    load_observers,
    put_observers,
)
from ummanu.dispatch.post_merge import render_fact, wake_facts
from ummanu.dispatch.tui import (
    READINESS_BUSY,
    delivery_readiness_state,
)
from ummanu.dispatch.types import HostError
from ummanu.dispatch.watchdog import (
    head_run_process_status,
    heartbeat_is_dead,
    heartbeat_is_live_match,
    heartbeat_is_mismatch,
    initial_output_stall_seconds,
    pid_file_path,
)
from ummanu.dispatch.worker_lifecycle import (
    CONTINUATION_NO_PROGRESS_BUSY_ATTEMPTS,
    ContinuationLivenessState,
    head_run_binding,
    residual_composer_class,
)
from ummanu.infra.env import positive_int
from ummanu.kernel.errors import TaskError
from ummanu.role_skills import skill_delivery
from ummanu.runtime import head as head_ops
from ummanu.runtime.codex_preflight import CodexFanoutRecordingError
from ummanu.runtime.role_env import observer_binding
from ummanu.sprint_observer import (
    KIND_HEAD,
    KIND_NONE,
    REASON_UNKNOWN_PROFILE,
    ObserverMetadataError,
    executable_observer,
)
from ummanu.tasks import is_significant_observer_event

OBSERVER_ROLE = "observer"
OBSERVER_PID_KIND = "observer"

# Pane readiness is the positive signal for the next durable event delivery.
OBSERVER_ACK_DEADLINE_DEFAULT_SECONDS = 30 * 60
# Compatibility ceiling for records without an attested provider-progress source.
OBSERVER_TURN_CEILING_DEFAULT_SECONDS = 3 * 60 * 60
# Unadmitted provider sources use a separate ceiling from no-source compatibility records.
OBSERVER_UNPROVEN_TURN_CEILING_DEFAULT_SECONDS = 15 * 60
OBSERVER_WAKE_RETRY_INITIAL_SECONDS = 30
OBSERVER_WAKE_RETRY_MAX_SECONDS = 5 * 60
# Bounded refused deliveries prevent an unreachable head from retrying forever.
OBSERVER_WAKE_MAX_ATTEMPTS_DEFAULT = 3

EVENT_LAUNCHED = "observer_launched"
EVENT_RELAUNCHED = "observer_relaunched"
EVENT_STOPPED = "observer_stopped"
EVENT_DEFERRED = "observer_launch_deferred"
EVENT_PROVIDER_FANOUT_BLOCKED = "observer_codex_provider_fanout_blocked"

# A refused stop preserves the record until the terminal is confirmed gone.
STATE_STOP_PENDING = "stop-pending"
STATE_PAUSE_STOP_PENDING = "pause-stop-pending"
STATE_STOPPED_BY_PAUSE = "stopped-by-pause"
PENDING_STOP_STATES = (STATE_STOP_PENDING, STATE_PAUSE_STOP_PENDING)
# The deferral a drain writes on an open sprint's record. Nothing failed: the launch waits for the
# resume, and the fence reads exactly this reason to tell that apart from a failed bring-up.
DRAIN_DEFERRED_REASON = "pipeline is draining"


def observer_pid_file(reference: str) -> str:
    return pid_file_path(OBSERVER_PID_KIND, reference)


class ObserverLaunchAborted(HostError):
    """A bring-up that failed after the head's terminal had already been created.

    An empty `handle` means nothing of that head is left running. A non-empty one means the host
    could not close the terminal it opened, so the head is assumed alive: the caller keeps the
    handle in the record and retries the stop.
    """

    def __init__(
        self,
        message: str,
        *,
        handle: str = "",
        leaf: str = "",
        workspace: str = "",
        pid_file: str = "",
        run: dict[str, Any] | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.handle = handle
        self.leaf = leaf
        self.workspace = workspace
        self.pid_file = pid_file
        self.run = dict(run or {})
        self.evidence = dict(evidence or {})



def observer_head_status(record: ObserverRecord) -> dict[str, Any]:
    """Read this observer's launch identity once, retaining its liveness classification.

A missing or unreadable identity is not positive evidence of death; only a
confirmed-dead process permits a replacement.
    """
    return head_run_process_status(
        record.pid_file or observer_pid_file(record.sprint),
        run=record.head_run,
        role="observer",
        task=sprint_task(record.sprint),
        leaf=record.leaf,
    )


def observer_head_is_dead(status: dict[str, Any]) -> bool:
    """Whether the observer's recorded process is positively dead.

Require a readable identity naming a pid that is gone or unreaped on this boot.
Missing, partial, unreadable, legacy pid-only and foreign live identities do
not authorize a replacement.
    """
    return heartbeat_is_dead(status)


def observer_alive(
    record: ObserverRecord, *, now: float | None = None, status: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Observer liveness with its evidence.

An unreadable pid file is inconclusive, not death; a new head is treated as
alive during its grace period. Reuse a supplied status when available.
    """
    now = time.time() if now is None else now
    status = observer_head_status(record) if status is None else status
    if heartbeat_is_mismatch(status):
        return {
            "alive": False,
            "reason": "heartbeat-identity-mismatch",
            "pid_known": True,
            "identity_mismatch": True,
        }
    if status.get("known"):
        return {"alive": heartbeat_is_live_match(status), "reason": "pid", "pid_known": True}
    grace = initial_output_stall_seconds()
    if record.launched_at and now - record.launched_at <= grace:
        return {"alive": True, "reason": "pid-not-written-yet", "pid_known": False}
    return {"alive": False, "reason": "pid-file-is-unreadable", "pid_known": False}


def observer_snapshot(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Observer state as an operator reads it, without opening a transcript."""
    rows = []
    now = time.time()
    for ref, record in sorted(load_observers(payload).items()):
        liveness = observer_alive(record, now=now)
        idle_reason = _idle_reason_at(record, now)
        rows.append(
            {
                "sprint": ref,
                "head": record.head,
                "state": record.state,
                "state_reason": idle_reason or record.state_reason,
                "workspace": record.workspace,
                "handle": record.handle,
                "leaf": record.leaf,
                "launches": record.launches,
                "alive": liveness["alive"],
                "pid_known": liveness["pid_known"],
                "heartbeat_state": liveness["reason"],
                # A head adopted from a launch intent is watching its sprint, but its terminal handle
                # died with the tick that started it: it is stopped by workspace, not by handle.
                "handle_known": bool(record.handle),
                "abandoned_handle": record.abandoned_handle,
                # Whether the head that is up was launched with its sprint binding. False on a head
                # from before the binding: it is alive and every write of its role is refused, which
                # reads as a watched sprint from every other field.
                "bound": record.bound,
                "last_action": record.last_action,
                "last_action_at": record.last_action_at,
                "deferred_reason": record.deferred_reason,
                "stopped_reason": record.stopped_reason,
                "paused": record.paused_at > 0,
                "idle_since": record.idle_since if idle_reason else 0.0,
                "idle_reason": idle_reason,
                "delivery": record.delivery.to_json(),
                "wake_liveness": record.wake_liveness.to_json(),
                "launch_attempts": record.launch_attempts,
                "launch_next_at": record.launch_next_at,
            }
        )
    return rows


def _idle_reason_at(record: ObserverRecord, now: float) -> str:
    """The idle explanation of a head idle right now, its quiet age read at `now`; empty otherwise."""
    if record.state != "idle-grace":
        return ""
    if not record.idle_since:
        return record.idle_reason
    return f"{record.idle_reason}; its terminal has been quiet for {int(now - record.idle_since)}s"


def observer_ack_deadline_seconds() -> int:
    """How long one delivery may stay unacknowledged before it is sent again."""
    return positive_int("UMMANU_OBSERVER_ACK_DEADLINE_SECONDS", OBSERVER_ACK_DEADLINE_DEFAULT_SECONDS)


def observer_turn_ceiling_seconds() -> int:
    """How long one head may hold a batch while never being seen ready for input."""
    return positive_int("UMMANU_OBSERVER_TURN_CEILING_SECONDS", OBSERVER_TURN_CEILING_DEFAULT_SECONDS)


def observer_unproven_turn_ceiling_seconds() -> int:
    """How long a head whose provider source was never admitted may hold a batch."""
    return positive_int(
        "UMMANU_OBSERVER_UNPROVEN_TURN_CEILING_SECONDS",
        OBSERVER_UNPROVEN_TURN_CEILING_DEFAULT_SECONDS,
    )


def observer_wake_max_attempts() -> int:
    """How many refused wake deliveries of one batch are retried before the head is replaced."""
    return positive_int("UMMANU_OBSERVER_WAKE_MAX_ATTEMPTS", OBSERVER_WAKE_MAX_ATTEMPTS_DEFAULT)


def reconcile_observers(
    runtime: Any, payload: dict[str, Any], *, pause_mode: str = ""
) -> list[dict[str, Any]]:
    """Reconcile observer heads with open sprints.

Keep fenced sprints in reconciliation: their card fence cannot clear until
their observer has been adopted. No tracked heads and no open sprints is a no-op.
    """
    observers = load_observers(payload)
    try:
        open_sprints = {
            str(sprint.get("ref") or ""): sprint
            for sprint in select_sprints(runtime.sprints, statuses={"open"})
            if str(sprint.get("ref") or "")
        }
    except (TaskError, HostError) as exc:
        if not observers:
            return []
        # The board could not be asked. A record is never dropped on that evidence: an unreachable
        # sprint board must not read as a closed sprint and take a live head down with it.
        return [
            {
                "status": "degraded",
                "step": "observer-reconcile",
                "action": "sprint-board-unavailable",
                "reason": getattr(exc, "message", str(exc)),
            }
        ]
    if not open_sprints and not observers:
        return []

    outcomes: list[dict[str, Any]] = []
    try:
        for ref in sorted(set(observers) - set(open_sprints)):
            outcomes.append(
                _stop_observer(runtime, payload, observers, ref, reason="sprint is no longer open")
            )
        for ref in sorted(open_sprints):
            dead = _dead_launch(observers.get(ref))
            outcome = _reconcile_open_sprint(
                runtime,
                payload,
                observers,
                ref,
                pause_mode=pause_mode,
                sprint=open_sprints[ref],
            )
            if dead is not None and outcome.get("action") not in _BROUGHT_UP:
                _record_observer_dead(runtime, ref, dead, outcome)
            outcomes.append(outcome)
    finally:
        # Whatever went wrong above, the heads that were started or stopped before it are already
        # real. The records go back into the payload so the caller saves them: a lost record means
        # an unattended terminal and a second head on the same sprint next tick.
        put_observers(payload, observers)
    return outcomes


#: The outcomes of a tick that brought an observer head up.
_BROUGHT_UP = frozenset({"observer-launched", "observer-relaunched"})


def _dead_launch(record: ObserverRecord | None) -> int | None:
    """Return the launch count if this observer is confirmed dead at tick start.

Use the same liveness classifier as reconciliation, before it mutates records.
Deliberate stops and pending stops are not deaths.
    """
    if (
        record is None
        or record.launches <= 0
        or record.state in PENDING_STOP_STATES
        or record.state == STATE_STOPPED_BY_PAUSE
        or not _head_may_be_running(record)
    ):
        return None
    try:
        return record.launches if observer_head_is_dead(observer_head_status(record)) else None
    except Exception:  # noqa: BLE001 - the bell's reading never decides a tick
        return None


def _record_observer_dead(runtime: Any, ref: str, launches: int, outcome: dict[str, Any]) -> None:
    """One `observer_dead` notice per dead head generation: repeated deferrals of it write nothing new."""
    why = str(outcome.get("reason") or outcome.get("action") or "no reason recorded")
    owner_events.record(
        owner_events.OBSERVER_DEAD,
        ref,
        f"The observer head of {ref} is dead and was not relaunched this tick: {why}",
        f"{owner_events.OBSERVER_DEAD}:{ref}:{launches}",
        to=getattr(getattr(runtime, "reader", None), "client", None),
    )


def _reconcile_open_sprint(
    runtime: Any,
    payload: dict[str, Any],
    observers: dict[str, ObserverRecord],
    ref: str,
    *,
    pause_mode: str,
    sprint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    record = observers.get(ref)
    sprint = sprint if sprint is not None else {}
    try:
        decision = observer_decision(runtime, sprint)
    except ObserverMetadataError as exc:
        # The fence has already stopped this sprint's cards and said so durably.  Nothing is
        # launched, nothing is probed and no live head is taken down on the strength of a value
        # nobody can read: a corrupt declaration is repaired by an operator, not guessed at here.
        return {
            "status": "degraded",
            "step": "observer-reconcile",
            "sprint": ref,
            "action": "observer-declaration-invalid",
            "reason": exc.message,
            "observer_reason": exc.reason,
        }
    if decision["kind"] == KIND_NONE:
        # A sprint that declares no observer gets no head and no probe.  A record left from an
        # earlier declaration is stopped, or the sprint would keep paying for a head it no longer
        # declares.
        if record is not None:
            return _stop_observer(runtime, payload, observers, ref, reason="sprint declares no observer")
        return {
            "status": "ok",
            "step": "observer-reconcile",
            "sprint": ref,
            "action": "observer-none",
            "reason": "sprint declares no observer",
        }
    declared = str(decision["head"])
    if record is not None and record.head != declared and record.state != "launching":
        # The declaration moved to another profile while this record was kept (a `sprint reopen
        # --observer` over a live record). The fence holds the sprint on `observer_head_mismatch`
        # for as long as the record names the old head, and nothing else retires it, so the sprint
        # stayed fenced for good (issue:c61deb40b2d2b0745f37). The old head is stopped and its
        # record dropped, exactly as a close would; the next tick launches the declared head. A
        # bring-up still in flight is resolved first, by the branch below, and meets this on the
        # tick after.
        return _stop_observer(
            runtime,
            payload,
            observers,
            ref,
            reason=f"sprint declares observer {declared}, not {record.head or '(none recorded)'}",
        )
    if record is not None and record.state == "blocked":
        # A provider-policy refusal has no retry condition inside the dispatcher.  Keep this
        # sprint-visible record and typed evidence until an independently attested launch replaces
        # it; treating it as a normal deferred launch would quietly keep probing forbidden panes.
        return {
            "status": "blocked",
            "step": "observer-reconcile",
            "sprint": ref,
            "action": "codex-fanout-policy-blocked",
            "head": record.head,
            "policy_evidence": {"kind": "codex_provider_fanout", "state": "unknown"},
            "reason": record.deferred_reason,
        }
    if record is not None and _head_may_be_running(record):
        # Recovery has to re-install the exact launch binding before it looks at any new provider
        # event.  The source verifier rejects a missing/mismatched cursor rather than attributing
        # a same-workspace journal to this observer.
        failure = _poll_codex_provider_ingress(runtime, payload, observers, ref, record)
        if failure is not None:
            return failure
    # A record still in `launching` is a bring-up whose tick did not live to record the outcome.
    # It is resolved before anything else, because until it is, neither "a head is running here"
    # nor "this sprint is headless" is known.
    unresolved_intent = record is not None and record.state == "launching"
    if record is not None and unresolved_intent:
        adopted = _adopt_launch_intent(runtime, payload, observers, ref, record)
        if adopted is not None:
            return adopted
        _abandon_launch_intent(runtime, ref, record)
        # Abandoning is a state transition, not a reason for the rest of this tick to keep
        # treating the record as unresolved. In particular, the now-pending dead head must pass
        # the same durable-cursor gate as every other relaunch before the host is touched.
        unresolved_intent = record.state == "launching"
    # A head from before the sprint binding is retired here, whatever its pid says. Its writes are
    # all refused as `observer_identity_unbound`, so it is a live process that cannot work the
    # sprint, and nothing it does can bind it: the binding is in the environment it started with.
    # Stopping it is the changeover — this tick closes the terminal, the next one brings the head
    # back up bound. It comes before the drain check on purpose: a drained pipeline is a reason to
    # start nothing, not a reason to keep a head that can only be refused.
    if (
        record is not None
        and not unresolved_intent
        and not record.bound
        and record.launches > 0
        and _head_may_be_running(record)
    ):
        return _stop_observer(
            runtime,
            payload,
            observers,
            ref,
            reason="observer head predates the sprint binding and cannot authenticate its writes",
        )
    # A terminal left over from an aborted bring-up is skipped here whatever its pid says: that
    # head never received its sprint, and reading it as the live observer would park the sprint
    # forever on a head that is doing nothing.
    pending_event: dict[str, Any] | None = None
    event_state: dict[str, Any] | None = None
    # Whether this tick's launch, if it reaches one, is the replacement of a head that is
    # positively dead with nothing owed to it. Only that launch leaves a cooldown behind.
    dead_head_bringup = False
    # A durable cursor is a prerequisite for every existing head path, including a legacy record
    # left in `pending`. Otherwise a dead/missing head can be relaunched from an unknowable point
    # and replay or skip board work before the normal recovery branch gets a chance to validate it.
    if (
        record is not None
        and not unresolved_intent
        and (
            record.delivery.acknowledged_through
            or (record.delivery.stage != DeliveryStage.IDLE and record.delivery.through_event)
        )
    ):
        event_state = _observer_event_state(runtime, ref, record)
        if not event_state.get("known", True):
            _set_observer_state(record, "degraded", reason=event_state["reason"])
            return {
                "status": "degraded",
                "step": "observer-reconcile",
                "sprint": ref,
                "action": "observer-cursor-unavailable",
                "head": record.head,
                "reason": event_state["reason"],
            }
    # Whether a head of this record may still be up, and whether it is the live observer of this
    # sprint. The two are separated because the answer to the second is the tick's judgement that
    # this head is finished, and everything a rotation further down hands to the conditional stop
    # is that judgement: the head's epoch, and the pid-heartbeat fact read right here.
    head_may_be_running = record is not None and _head_may_be_running(record)
    # The pid-heartbeat answer about this head's process, read once for both readers of it: the
    # liveness judgement here, and the conditional stop further down, which is owed the fact rather
    # than allowed to re-derive it from a pane. `head_is_live` is narrower — an unresolved bring-up
    # intent and an abandoned handle both mean "not the live observer of this sprint" whatever the
    # process is doing — so the two are kept apart instead of one standing in for the other.
    head_status = observer_head_status(record) if head_may_be_running else {}
    head_process_alive = head_may_be_running and bool(observer_alive(record, status=head_status)["alive"])
    # Positive death of this record's launch identity, from the same reading. It is what makes a
    # bring-up over a headless open sprint conditional on the head rather than on the queue: a
    # process that is provably gone cannot be woken and cannot be the second head a launch would
    # put beside a live one.
    head_is_dead = head_may_be_running and observer_head_is_dead(head_status)
    head_is_live = (
        head_may_be_running and not unresolved_intent and not record.abandoned_handle and head_process_alive
    )
    if head_is_live:
        if record.state in PENDING_STOP_STATES:
            # The sprint is open again and the head that was to be stopped is still the head of
            # this sprint: the pending stop is moot, so the record reads as running once more.
            record.state = "running"
            record.state_reason = ""
            record.stopped_reason = ""
            record.paused_at = 0.0
        if record.launch_next_at and record.state not in {"deferred", "launching", "pending"}:
            # A replacement that stuck: this head was seen alive on a tick after its bring-up, so
            # the cooldown that bring-up left behind has done its job and the next dead head of
            # this sprint starts its own series rather than inheriting this one's. A record whose
            # state says a launch is still owed keeps its deadline: that one is a launch this tick
            # has yet to make, not a bound on the head it is looking at.
            record.launch_attempts = 0
            record.launch_next_at = 0.0
            record.deferred_reason = ""
        event = event_state or _observer_event_state(runtime, ref, record)
        if not event.get("known", True):
            _set_observer_state(record, "degraded", reason=event["reason"])
            return {
                "status": "degraded",
                "step": "observer-reconcile",
                "sprint": ref,
                "action": "observer-cursor-unavailable",
                "head": record.head,
                "reason": event["reason"],
            }
        if event["pending"]:
            return _wake_for_event(runtime, payload, observers, ref, record, event)
        work = _observer_work_state(runtime, ref, record)
        if work["state"] == "idle":
            _mark_idle_grace(record, since=work["since"], reason=work["reason"])
            return {
                "status": "ok",
                "step": "observer-reconcile",
                "sprint": ref,
                "action": "observer-idle",
                "head": record.head,
                "launches": record.launches,
                "reason": _idle_reason_at(record, time.time()),
            }
        if work["state"] == "waiting":
            _set_observer_state(record, "waiting", reason=work["reason"])
            return {
                "status": "ok",
                "step": "observer-reconcile",
                "sprint": ref,
                "action": "observer-waiting",
                "head": record.head,
                "launches": record.launches,
            }
        if record.state in {"waiting", "idle-grace"}:
            _set_observer_state(record, "running")
        return {
            "status": "ok",
            "step": "observer-reconcile",
            "sprint": ref,
            "action": "observer-live",
            "head": record.head,
            "launches": record.launches,
        }
    # A process recovery happens for work an observer has not durably acknowledged, and — since
    # secretary-1478 — for a head whose own launch identity says it is dead, queue or no queue.
    # A quiet queue is a reason not to *wake* a head that is there; it is not a reason to leave an
    # open sprint without one. The fence over this sprint's cards asks the same pid heartbeat and
    # holds until a head is adopted, so leaving a positively dead head alone here fenced the sprint
    # until an operator commented on one of its cards (issue:5733409d4a74ad3ce8a8).
    if (
        record is not None
        and not unresolved_intent
        and record.launches > 0
        and record.state != STATE_STOPPED_BY_PAUSE
        and record.state != "pending"
    ):
        event = event_state or _observer_event_state(runtime, ref, record)
        if not event.get("known", True):
            _set_observer_state(record, "degraded", reason=event["reason"])
            return {
                "status": "degraded",
                "step": "observer-reconcile",
                "sprint": ref,
                "action": "observer-cursor-unavailable",
                "head": record.head,
                "reason": event["reason"],
            }
        if event["pending"]:
            pending_event = event
        elif record.state == "deferred":
            # The quiet queue answers only for a record no later branch of this tick still owns.
            # `deferred` is owned below: it is a bring-up this tick owes, held to the backoff its
            # last attempt persisted, and the deferral branch is what decides whether the backoff
            # has run out. Answering `idle` over it dropped that launch and its counter, so a
            # bring-up whose replacement the host refused was never attempted again — this card's
            # own deadlock, reached by another route (secretary-1478 round 3).
            dead_head_bringup = head_is_dead
        elif not head_is_dead:
            _set_observer_state(record, "idle", reason="no unacknowledged significant card event")
            return {
                "status": "ok",
                "step": "observer-reconcile",
                "sprint": ref,
                "action": "observer-idle",
                "head": record.head,
                "launches": record.launches,
                "reason": event["reason"],
            }
        elif time.time() < record.launch_next_at:
            # A dead head with nothing owed to it, replaced recently enough that the cooldown that
            # replacement left has not run out. It is what keeps a head that dies the moment it
            # comes up to one attempt per backoff window instead of one per tick.
            return {
                "status": "degraded",
                "step": "observer-reconcile",
                "sprint": ref,
                "action": "observer-launch-deferred",
                "head": record.head,
                "reason": record.deferred_reason
                or "the replacement of this dead observer head is waiting out its backoff",
            }
        else:
            # The bring-up over a dead head with nothing owed to it. It carries no batch: there is
            # no unacknowledged event to hand the replacement, and inventing one would replay work
            # the previous head already acknowledged. Everything below it is the ordinary launch
            # path — the drain, the deferral backoff, the conditional stop — because a dead head is
            # not a reason to skip any of them.
            dead_head_bringup = True
    if pause_mode == "drain":
        # A drain claims nothing new. A dead observer is a bring-up, so it waits for the resume
        # exactly like a Ready card does. The record is still written: an open sprint has to be
        # readable from outside with its head profile and the reason nothing is running on it,
        # and the resume then relaunches from that same record. Neither the readiness gate nor
        # the host is touched here — only the head profile is resolved, to fill the record.
        return _defer(
            runtime,
            payload,
            observers,
            ref,
            record,
            head=_observer_head_or_blank(runtime, sprint),
            reason=DRAIN_DEFERRED_REASON,
            action="observer-launch-skipped",
            # An intent nobody could resolve stays an intent through the drain: the resume then
            # closes what it may have started instead of opening a head beside it.
            keep_state=unresolved_intent,
            retry=False,
        )
    if (
        record is not None
        and record.state == "deferred"
        and not record.abandoned_handle
        and time.time() < record.launch_next_at
    ):
        return {
            "status": "degraded",
            "step": "observer-reconcile",
            "sprint": ref,
            "action": "observer-launch-deferred",
            "head": record.head,
            "reason": record.deferred_reason,
        }
    if record is None:
        # The first observer launch renders the current sprint board. Card history from before
        # that head existed is therefore its baseline, not a second turn immediately after its
        # own initial queue. Later transitions still enter the delivery machine normally.
        record = ObserverRecord(sprint=ref)
        baseline = _observer_event_state(runtime, ref, record)
        if baseline.get("pending"):
            record.delivery.acknowledged_through = str(baseline.get("event_id") or "")
    # The judgement this tick made about the previous head, assembled where its consumer is rather
    # than at the branch that made it: every path between the two returns without reaching a
    # launch, and none of them touches the head runtime for this run — `_observer_event_state` and
    # `_observer_head_or_blank` read the board and the sprint, not the head. What must not happen
    # is the read sliding *past* here, into the stop itself, where it would already contain
    # whatever the head did in between; everything `_launch_observer` does before the stop —
    # `head_readiness`, `observer_skill_delivery`, the wake-liveness write — happens after it.
    judgement = QuietStopJudgement(
        activity_epoch=(
            _observer_activity_epoch(runtime, record) if head_may_be_running and not head_is_live else 0
        ),
        head_process_alive=head_process_alive,
    )
    attempted = record.launch_attempts
    outcome = _launch_observer(
        runtime,
        payload,
        observers,
        ref,
        record,
        pending_event=pending_event,
        head=str(decision["head"]),
        # The rotation, and the one stop that is conditional: this tick judged the previous head
        # finished, and it hands that judgement down rather than letting the stop re-read it.
        conditional_stop=True,
        judgement=judgement,
    )
    if dead_head_bringup and outcome.get("action") in {"observer-launched", "observer-relaunched"}:
        # A launch that fails is already on the persisted backoff `_defer` keeps, and its counter
        # is the one this attempt continued. A launch that *succeeds* clears that counter, which
        # is right for every other caller and wrong for exactly this one: the head this tick
        # replaced was dead with no work owed to it, so nothing but the passage of time separates
        # this bring-up from the next one over a head that dies the same way. The cooldown is what
        # that time is, and a head seen alive on a later tick clears it again.
        _hold_dead_head_bringup(record, attempts=attempted + 1)
        observers[ref] = record
        _persist_quietly(runtime, payload, observers)
    return outcome


def _observer_event_state(runtime: Any, ref: str, record: ObserverRecord) -> dict[str, Any]:
    """Read semantic sprint work and acknowledge only the active delivery batch.

    A resume acknowledges only when its audit payload carries the active delivery's immutable
    marker, so an older turn cannot credit work it never received.
    """
    try:
        # Observer reconciliation needs cards and the event stream, but not the independently
        # rendered resume-freshness field. The event boundary comes before the entity snapshot:
        # every committed comment event follows its board mutation, so the subsequent snapshot
        # contains everything in this batch. A comment committed after the audit read belongs to
        # the next batch.
        #
        # The read is narrowed to this sprint and its cards (secretary-1658), and nothing under
        # the tick reads the whole stream: the cards are listed once to name the slice before it
        # is read, and a card linked between that listing and the snapshot widens the slice and
        # the read is taken again. Links still moving after that leave this tick's state
        # unestablished, and the next tick reads again. A cursor the slice does not hold is looked
        # up by its own id and its ref joins the slice.
        refs = _linked_refs(runtime.sprints.show(ref, include_resume_freshness=False))
        for _attempt in range(_OBSERVER_SLICE_ATTEMPTS):
            events = runtime.audit.events(references=refs | {ref})
            sprint = runtime.sprints.show(ref, include_resume_freshness=False)
            linked = _linked_refs(sprint)
            if linked <= refs:
                break
            refs |= linked
        else:
            return {"known": False, "pending": False, "reason": "linked cards changed while the audit was read"}
        delivery = record.delivery
        wanted = {delivery.acknowledged_through}
        if delivery.stage != DeliveryStage.IDLE:
            wanted.add(delivery.through_event)
        missing = {event_id for event_id in wanted if event_id} - {_event_id(event) for event in events}
        if missing:
            events = runtime.audit.events(references=refs | {ref} | _cursor_refs(runtime.audit, missing))
        refs = linked
    except (TaskError, HostError, OSError, ValueError, TypeError):
        return {"known": False, "pending": False, "reason": "linked card audit is unavailable"}
    try:
        _observer_comments(sprint)
    except TaskError as exc:
        return {"known": False, "pending": False, "reason": exc.message}
    resumes: list[dict[str, Any]] = []
    for event in events:
        if str(event.get("ref") or "") == ref and str(event.get("kind") or "") == "resume_recorded":
            resumes.append(event)
    _acknowledge_delivery_from_resume(record.delivery, resumes)
    # Cursors written before semantic wakes were narrowed may name claim/report/routing telemetry.
    # Resolve that id in the complete stream first, then filter only later events; filtering first
    # makes the cursor disappear and replays the whole sprint forever.
    cursor = record.delivery.acknowledged_through
    cursor_at = _event_index(events, cursor)
    if cursor and cursor_at < 0:
        return {"known": False, "pending": False, "reason": "acknowledged observer cursor is unavailable"}
    active_legacy_at = -1
    if record.delivery.stage != DeliveryStage.IDLE and record.delivery.through_event:
        active_at = _event_index(events, record.delivery.through_event)
        if active_at < 0:
            return {"known": False, "pending": False, "reason": "active observer cursor is unavailable"}
        if not is_significant_observer_event(events[active_at], linked_refs=refs, sprint_ref=ref):
            active_legacy_at = active_at
    following = events[cursor_at + 1 :] if cursor_at >= 0 else events
    significant = [
        event for event in following if is_significant_observer_event(event, linked_refs=refs, sprint_ref=ref)
    ]
    if active_legacy_at >= 0:
        # Retire the obsolete batch without skipping semantic work which arrived after the prior
        # acknowledged cursor. Only an empty semantic range permits advancing across the noise.
        preserved_cursor = cursor if significant else record.delivery.through_event
        _reset_delivery_to_idle(
            record.delivery,
            acknowledged_through=preserved_cursor,
            acknowledged_delivery_id=record.delivery.delivery_id,
            acknowledged_resume_id="",
        )
    if not significant:
        return {"known": True, "pending": False, "reason": "no significant linked-card event"}
    latest = significant[-1]
    latest_id = _event_id(latest)
    if not latest_id:
        return {"known": False, "pending": False, "reason": "latest card event has no durable id"}
    return {
        "known": True,
        "pending": True,
        "event_id": latest_id,
        "change": "sprint-entity" if str(latest.get("ref") or "") == ref else "linked-card",
        "sprint": sprint,
        # The post-merge CI results in this batch, stated in the wake itself (secretary-1736).
        "post_merge": wake_facts(significant),
        "pending_from": _event_id(significant[0]),
        "occurred_at": str(latest.get("occurred_at") or ""),
        "age_seconds": _event_age_seconds(str(latest.get("occurred_at") or "")),
        "latest_resume_id": _event_id(resumes[-1]) if resumes else "",
        "reason": "latest significant linked-card event is not acknowledged",
    }


def _reset_delivery_to_idle(
    delivery: ObserverDelivery,
    *,
    acknowledged_through: str,
    acknowledged_delivery_id: str,
    acknowledged_resume_id: str,
) -> None:
    delivery.stage = DeliveryStage.IDLE
    delivery.acknowledged_through = acknowledged_through
    delivery.acknowledged_delivery_id = acknowledged_delivery_id
    delivery.acknowledged_resume_id = acknowledged_resume_id
    delivery.pending_from = ""
    delivery.delivery_id = ""
    delivery.method = ""
    delivery.through_event = ""
    delivery.resume_cursor = ""
    delivery.resume_cursor_known = True
    delivery.sent_at = 0.0
    delivery.held_since = 0.0
    delivery.deadline = 0.0
    delivery.attempts = 0
    delivery.next_at = 0.0
    delivery.reason = ""


def _acknowledge_delivery_from_resume(delivery: ObserverDelivery, resumes: list[dict[str, Any]]) -> None:
    """Advance only when the active batch's marker was written by its observer.

    A refused delivery counts here too: the marker only exists in a prompt that reached the head, so
    a resume naming it is proof of the turn whatever the dispatcher observed of the send.
    """

    if delivery.stage not in {
        DeliveryStage.DELIVERY_INTENT,
        DeliveryStage.AWAITING_ACK,
        DeliveryStage.RETRY_DEFERRED,
    }:
        return
    if not delivery.delivery_id or not delivery.through_event:
        return
    for resume in reversed(resumes):
        payload = resume.get("payload")
        actor = resume.get("actor")
        if not isinstance(payload, dict) or not isinstance(actor, dict):
            continue
        if str(actor.get("role") or "") != OBSERVER_ROLE:
            continue
        if (
            str(payload.get("delivery_id") or "") != delivery.delivery_id
            or str(payload.get("through_event") or "") != delivery.through_event
        ):
            continue
        resume_id = _event_id(resume)
        if not resume_id:
            continue
        _reset_delivery_to_idle(
            delivery,
            acknowledged_through=delivery.through_event,
            acknowledged_delivery_id=delivery.delivery_id,
            acknowledged_resume_id=resume_id,
        )
        return


def _new_delivery_intent(
    delivery: ObserverDelivery,
    *,
    method: str,
    through_event: str,
    resume_cursor: str,
    now: float,
    delivery_id: str = "",
) -> None:
    """Fix a high-water mark before an external observer action."""

    delivery.stage = DeliveryStage.DELIVERY_INTENT
    delivery.pending_from = ""
    delivery.delivery_id = delivery_id or "delivery-" + uuid.uuid4().hex
    delivery.method = method
    delivery.through_event = through_event
    delivery.resume_cursor = resume_cursor
    delivery.resume_cursor_known = True
    delivery.sent_at = now
    # A fresh send is a fresh turn: the head is being asked again, so the ceiling on how long it
    # may hold this batch without going idle starts over here too.
    delivery.held_since = now
    delivery.deadline = now + observer_ack_deadline_seconds()
    delivery.next_at = delivery.deadline
    delivery.reason = f"{method} delivery intent is persisted before external action"


def _prepare_launch_delivery(record: ObserverRecord, event: dict[str, Any]) -> str:
    """Put a replacement launch through the same state machine as a terminal nudge."""

    delivery = record.delivery
    now = time.time()
    active = delivery.stage in {DeliveryStage.DELIVERY_INTENT, DeliveryStage.AWAITING_ACK}
    retrying = delivery.stage == DeliveryStage.RETRY_DEFERRED
    _new_delivery_intent(
        delivery,
        method="launch",
        through_event=(delivery.through_event if active or retrying else str(event["event_id"])),
        resume_cursor=str(event.get("latest_resume_id") or ""),
        now=now,
        delivery_id=delivery.delivery_id if active or retrying else "",
    )
    return delivery.through_event


def _set_delivery_waiting(delivery: ObserverDelivery, event: dict[str, Any], *, reason: str) -> None:
    if delivery.stage == DeliveryStage.IDLE:
        delivery.stage = DeliveryStage.WAITING_FOR_IDLE
        delivery.pending_from = str(event.get("pending_from") or event.get("event_id") or "")
        # A batch waiting for a busy head is held by it, so the turn ceiling runs from here even
        # though no prompt has been delivered yet.
        delivery.held_since = time.time()
    delivery.reason = reason


def _evidence_of(carrier: Any) -> dict[str, Any]:
    """The bounded delivery evidence a host answer or failure carries, or nothing."""
    evidence = getattr(carrier, "evidence", None)
    if hasattr(evidence, "to_json"):
        evidence = evidence.to_json()
    return dict(evidence) if isinstance(evidence, dict) else {}


def _delivery_subject(method: str) -> str:
    """Which delivery this is, in the words the sprint's evidence keeps it under.

    A wake is a prompt sent into an observer head already up; a launch delivery is the prompt a
    replacement head is brought up with. Neither is a reviewer bring-up, which belongs to a card.
    """
    return "observer-launch" if method == "launch" else "observer-wake"


def _count_delivery_attempt(delivery: ObserverDelivery, method: str) -> None:
    """Count one attempt to put a batch in front of the observer, however it ends."""
    if method == "launch":
        delivery.launch_delivery_attempts += 1
    else:
        delivery.wake_attempts += 1


def _count_delivery_failure(
    delivery: ObserverDelivery,
    method: str,
    reason: str,
    evidence: dict[str, Any] | None = None,
) -> None:
    """Record one delivery that did not reach the head, as sprint evidence rather than as state.

    An attempt is counted with it: a delivery that failed before the pane was touched at all was
    still a turn the observer was owed and did not get.
    """
    _count_delivery_attempt(delivery, method)
    if method == "launch":
        delivery.launch_delivery_failures += 1
    else:
        delivery.wake_failures += 1
    delivery.last_failure_reason = reason
    delivery.last_failure_at = time.time()
    delivery.last_failure_method = _delivery_subject(method)
    if evidence:
        delivery.last_evidence = dict(evidence)


def _defer_delivery(
    record: ObserverRecord,
    ref: str,
    reason: str,
    *,
    method: str = "",
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist bounded external-delivery retry state without creating a busy loop."""

    delivery = record.delivery
    _count_delivery_failure(delivery, method or delivery.method or "nudge", reason, evidence)
    delivery.stage = DeliveryStage.RETRY_DEFERRED
    delivery.attempts += 1
    delay = min(
        OBSERVER_WAKE_RETRY_INITIAL_SECONDS * (2 ** (delivery.attempts - 1)),
        OBSERVER_WAKE_RETRY_MAX_SECONDS,
    )
    delivery.next_at = time.time() + delay
    delivery.deadline = 0.0
    delivery.reason = f"{reason}; retry in {delay}s"
    _set_observer_state(record, "wake-deferred", reason=delivery.reason)
    return {
        "status": "degraded",
        "step": "observer-reconcile",
        "sprint": ref,
        "action": "observer-wake-deferred",
        "head": record.head,
        "delivery_id": delivery.delivery_id,
        "event_id": delivery.through_event,
        "reason": delivery.reason,
        "wake_failures": delivery.wake_failures,
        "launch_delivery_failures": delivery.launch_delivery_failures,
    }


def _wait_for_busy_delivery(
    record: ObserverRecord,
    ref: str,
    reason: str,
    *,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Keep an observed-working observer and its exact undelivered batch intact.

    The intent was already durable before the nudge. A `tui-idle` timeout with a busy body says the
    prompt was not sent, so it must not consume a failed-wake retry or arm an acknowledgement
    deadline; `WAITING_FOR_IDLE` puts it under the turn ceiling instead.
    """
    delivery = record.delivery
    delivery.stage = DeliveryStage.WAITING_FOR_IDLE
    delivery.deadline = 0.0
    delivery.next_at = 0.0
    if not delivery.held_since:
        delivery.held_since = time.time()
    delivery.reason = reason
    if evidence:
        delivery.last_evidence = dict(evidence)
    _set_observer_state(record, "waiting", reason=reason)
    return {
        "status": "degraded",
        "step": "observer-reconcile",
        "sprint": ref,
        "action": "observer-wake-busy",
        "head": record.head,
        "delivery_id": delivery.delivery_id,
        "event_id": delivery.through_event,
        "reason": reason,
    }


def _fail_delivery(
    runtime: Any,
    payload: dict[str, Any],
    observers: dict[str, ObserverRecord],
    ref: str,
    record: ObserverRecord,
    event: dict[str, Any],
    reason: str,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Refuse one wake, retry it a bounded number of times, then replace the head.

    Once the retries are spent the delivery takes the replacement path: it stops this head before it
    opens the next one, and carries the same `delivery_id` and `through_event` into the new launch,
    so a resume still acknowledges exactly the batch that was owed.
    """
    outcome = _defer_delivery(record, ref, reason, method="nudge", evidence=evidence)
    if record.delivery.attempts < observer_wake_max_attempts():
        return outcome
    attempts = record.delivery.attempts
    # The replacement opens its own delivery attempt count: the next failing batch gets the same
    # bounded retries before it costs the sprint another head.
    record.delivery.attempts = 0
    # An emergency replacement. The head has refused every wake it was given, so it is stuck or
    # gone; a stop that refused because it looked busy would leave the sprint on the head that is
    # not working and park the replacement behind a bounded backoff for as long as it stays stuck.
    replaced = _launch_observer(
        runtime,
        payload,
        observers,
        ref,
        record,
        pending_event=event,
        conditional_stop=False,
    )
    unlaunched = str(replaced.get("reason") or "")
    replaced["reason"] = f"{reason}; the observer head was replaced after {attempts} failed wakes"
    if unlaunched:
        replaced["reason"] += f"; that replacement did not come up: {unlaunched}"
    return replaced


def _wake_pending(ref: str, record: ObserverRecord) -> dict[str, Any]:
    """A delivery that is on the head and still within its acknowledgement deadline."""
    return {
        "status": "ok",
        "step": "observer-reconcile",
        "sprint": ref,
        "action": "observer-wake-pending",
        "head": record.head,
        "delivery_id": record.delivery.delivery_id,
        "event_id": record.delivery.through_event,
    }


def _redelivery_reason(status: dict[str, Any], delivery: ObserverDelivery, *, now: float) -> str:
    """Return the reason to redeliver, or empty text while awaiting acknowledgement.

An observed idle head with readable activity may receive a known-sent batch
again immediately. A pending DELIVERY_INTENT may never have reached the pane;
its deadline, not idle state alone, controls retry.
    """
    if now >= delivery.deadline:
        return (
            "the acknowledgement deadline expired with this batch unacknowledged after "
            f"{int(now - delivery.sent_at)}s"
        )
    if delivery.stage != DeliveryStage.AWAITING_ACK:
        return ""
    try:
        # Read for its readability alone: the value is not compared against anything.
        float(status.get("last_activity"))
    except (TypeError, ValueError):
        return ""
    return (
        "the observer became idle without acknowledging this batch "
        f"{int(now - delivery.sent_at)}s after it was sent"
    )


def _observer_provider_source(
    record: ObserverRecord,
) -> tuple[head_ops.HeadRun | None, dict[str, Any], bool]:
    """Return this run's persisted provider source, never one found from the workspace."""
    stored = record.head_run if isinstance(record.head_run, dict) else {}
    try:
        run = head_ops.HeadRun.from_json(stored)
    except (head_ops.HeadRunError, head_ops.TaskRefError, TypeError, ValueError):
        return None, {}, False
    source_key = (
        "provider_source"
        if run.spec.adapter == "codex"
        else "provider_progress_source"
        if run.spec.adapter == "claude"
        else ""
    )
    if not source_key:
        return run, {}, False
    raw_source = run.fanout_policy.get(source_key)
    return run, dict(raw_source) if isinstance(raw_source, dict) else {}, source_key in run.fanout_policy


def _observer_has_provider_liveness_contract(record: ObserverRecord) -> bool:
    """Whether this wake must use run-bound provider progress.

Current bound runs retain this contract even when their telemetry breaks;
never fall back to screen liveness after losing a previously bound source.
Legacy runs without a provider binding take the separate unbound path.
    """
    _run, _source, source_present = _observer_provider_source(record)
    if source_present:
        return True
    return False


def _precontract_unbound_observer_source(record: ObserverRecord) -> bool:
    """Recognise, but never repair, the pre-rollout unbound Codex source shape.

    A current preflight source carries the run descriptor and the exact pre-pane baseline that makes
    later binding safe. Accepting a journal from the workspace now would be a retroactive identity
    claim.
    """
    run, source, _source_present = _observer_provider_source(record)
    if run is None or not source or source.get("state") != "unbound":
        return False
    run_id, fingerprint = head_run_binding(record.head_run)
    return not (
        run_id
        and str(source.get("run_id") or "") == run_id
        and str(source.get("head_run_fingerprint") or "") == fingerprint
        and str(source.get("workspace") or "") == run.workspace
        and str(source.get("role") or "") == OBSERVER_ROLE
        and source.get("task_ref") == run.task_ref.to_json()
        and str(source.get("root") or "")
        and isinstance(source.get("baseline"), list)
    )


def _begin_observer_wake_liveness(record: ObserverRecord) -> None:
    """Open one episode only at a new event-delivery boundary.

    An old terminal episode is replaced only after a later acknowledged batch opens a new delivery
    on the new HeadRun, never on an observation from an arbitrary same-workspace source.
    """
    if not _observer_has_provider_liveness_contract(record):
        return
    run_id, _ = head_run_binding(record.head_run)
    if not run_id:
        return
    if record.wake_liveness.bound or record.wake_liveness.terminal:
        record.wake_liveness = ObserverWakeLiveness.begin(record.head_run)
        return
    if (
        record.wake_liveness.state == ContinuationLivenessState.UNKNOWN
        and record.wake_liveness.reason == "missing"
    ):
        record.wake_liveness = ObserverWakeLiveness.begin(record.head_run)


def _observe_observer_wake_progress(record: ObserverRecord, runtime: Any, *, now: float) -> str:
    """Record one exact-run provider observation before any TUI readiness interpretation."""
    if not _observer_has_provider_liveness_contract(record):
        return "disabled"
    liveness = record.wake_liveness
    if not liveness.bound:
        # A malformed or historical episode cannot acquire a source halfway through a delivery.
        return "unknown"
    try:
        probe = getattr(runtime.host, "observer_provider_progress", None)
        evidence = (
            probe(record)
            if callable(probe)
            else {"state": "unavailable", "reason": "host has no observer provider-progress probe"}
        )
    except Exception as exc:  # noqa: BLE001 - observer bookkeeping never fails the tick
        evidence = {"state": "unavailable", "reason": f"provider-progress probe failed: {exc}"}
    return liveness.observe_provider(evidence, now, head_run=record.head_run)


def _observer_no_progress_evidence(record: ObserverRecord, status: dict[str, Any]) -> str:
    """Classify a stale exact cursor without making screen text liveness authority."""
    evidence = record.delivery.last_evidence if isinstance(record.delivery.last_evidence, dict) else {}
    evidence = (
        {**evidence, **(status.get("delivery_evidence") or {})}
        if isinstance(status.get("delivery_evidence"), dict)
        else evidence
    )
    if str(evidence.get("reason") or "") == "payload-left-in-composer":
        return "completed_turn_residual_composer"
    return residual_composer_class(evidence)


def _replace_observer_for_no_progress(
    runtime: Any,
    payload: dict[str, Any],
    observers: dict[str, ObserverRecord],
    ref: str,
    record: ObserverRecord,
    event: dict[str, Any],
    *,
    reason: str,
) -> dict[str, Any]:
    """Persist the terminal exact-run outcome before the fenced stop/relaunch path."""
    record.wake_liveness.terminalize("replacement", reason)
    observers[ref] = record
    if not _persist_quietly(runtime, payload, observers):
        return {
            "status": "degraded",
            "step": "observer-reconcile",
            "sprint": ref,
            "action": "observer-wake-liveness-persist-failed",
            "head": record.head,
            "reason": "observer provider-progress terminal outcome could not be persisted",
        }
    # An emergency replacement too: the head is up and showing no provider progress, so it is
    # being taken down for being stuck rather than for being finished.
    replaced = _launch_observer(
        runtime,
        payload,
        observers,
        ref,
        record,
        pending_event=event,
        conditional_stop=False,
    )
    original = str(replaced.get("reason") or "")
    replaced["reason"] = reason + (f"; {original}" if original else "")
    return replaced


def _observer_provider_failure(runtime: Any, record: ObserverRecord) -> dict[str, Any] | None:
    """The host's reading of this observer's own provider refusal, or None when it shows none."""
    probe = getattr(runtime.host, "observer_provider_failure", None)
    if not callable(probe) or not record.head_run.get("run_id"):
        return None
    try:
        reading = probe(record)
    except Exception:  # noqa: BLE001 - an unreadable source is no verdict; the wake decides
        return None
    if not isinstance(reading, dict) or reading.get("state") != "failed":
        return None
    if str(reading.get("run_id") or record.head_run.get("run_id")) != str(record.head_run.get("run_id")):
        return None
    return reading


def _replace_observer_for_provider_failure(
    runtime: Any,
    payload: dict[str, Any],
    observers: dict[str, ObserverRecord],
    ref: str,
    record: ObserverRecord,
    event: dict[str, Any],
    reading: dict[str, Any],
) -> dict[str, Any]:
    """Record the refusing resource red, then replace the observer through the emergency path.

    The replacement walks the declared profile's chain (`_launch_observer`), so it lands on the
    other family while this resource is red, and it carries the same pending batch.
    """
    from ummanu.head_health import failure_status, failure_until, until_text

    error = reading.get("error") if isinstance(reading.get("error"), dict) else {}
    kind = str(error.get("kind") or "")
    summary = " ".join(str(error.get("summary") or kind or "provider error").split())[:200]
    resource = str(reading.get("resource") or "")
    now = time.time()
    until = failure_until(kind, float(error.get("reset_at") or 0.0), now)
    if resource:
        try:
            runtime.head_health.record(
                resource,
                failure_status(kind),
                f"provider error in a turn of observer head {reading.get('head') or record.head}: {summary}",
                now=now,
                until=until,
            )
        except Exception as exc:  # noqa: BLE001 - the cache writer has no narrower contract
            return {
                "status": "degraded",
                "step": "observer-reconcile",
                "sprint": ref,
                "action": "observer-provider-failure-unrecorded",
                "head": record.head,
                "reason": f"resource health could not be written ({type(exc).__name__}); retried next tick",
            }
    replaced = _replace_observer_for_no_progress(
        runtime,
        payload,
        observers,
        ref,
        record,
        event,
        reason=(
            f"provider failure: observer head {reading.get('head') or record.head} was refused by "
            f"{resource or '(unnamed resource)'} ({summary}); the resource is red until {until_text(until)}"
        ),
    )
    replaced["provider_failure"] = {"resource": resource, "error": summary, "until": until}
    if record.fallback_head:
        replaced["switched_to"] = record.fallback_head
    return replaced


def _adopt_precontract_unbound_observer(
    runtime: Any,
    payload: dict[str, Any],
    observers: dict[str, ObserverRecord],
    ref: str,
    record: ObserverRecord,
    event: dict[str, Any],
    *,
    now: float,
) -> dict[str, Any]:
    """Replace an old unbound Codex observer without treating a workspace journal as its own."""
    if not record.wake_liveness.bound:
        record.wake_liveness = ObserverWakeLiveness.begin(record.head_run)
    record.wake_liveness.observe_provider(
        {
            "state": "unavailable",
            "reason": "pre-contract Codex observer source is unbound and has no exact baseline",
        },
        now,
        head_run=record.head_run,
    )
    _new_delivery_intent(
        record.delivery,
        method="replacement",
        through_event=str(event["event_id"]),
        resume_cursor=str(event.get("latest_resume_id") or ""),
        now=now,
        delivery_id=record.delivery.delivery_id,
    )
    return _replace_observer_for_no_progress(
        runtime,
        payload,
        observers,
        ref,
        record,
        event,
        reason=(
            "pre-contract Codex observer provider source is unbound; it cannot be rebound from "
            "workspace journal discovery and is being identity-fenced for replacement"
        ),
    )


def _observer_wake_ceiling_seconds(record: ObserverRecord) -> int:
    """Allowed hold time for a head never observed ready for input.

An admitted provider cursor uses the bounded no-progress policy instead.
Other heads use the appropriate clock for unavailable or unbound telemetry.
    """
    if record.wake_liveness.admitted:
        return 0
    if _observer_has_provider_liveness_contract(record):
        return observer_unproven_turn_ceiling_seconds()
    return observer_turn_ceiling_seconds()


def _turn_ceiling_overrun(delivery: ObserverDelivery, ceiling: int, *, now: float) -> str:
    """Return why an unready head has held a batch too long, or empty text.

This ceiling covers a busy-looking head; it is longer than the acknowledgement
deadline for a head standing at its prompt.
    """
    if delivery.stage == DeliveryStage.IDLE:
        return ""
    if not delivery.held_since:
        # A record written before this ceiling existed. It runs from the first tick that sees the
        # head busy, never retroactively over a hold nobody was measuring.
        delivery.held_since = now
        return ""
    held = now - delivery.held_since
    if held < ceiling:
        return ""
    return (
        f"the observer head held this delivery for {int(held)}s without ever being ready for "
        f"input, past the {ceiling}s turn ceiling"
    )


def _wake_for_event(
    runtime: Any,
    payload: dict[str, Any],
    observers: dict[str, ObserverRecord],
    ref: str,
    record: ObserverRecord,
    event: dict[str, Any],
) -> dict[str, Any]:
    """Deliver an immutable event batch to the observer queue.

Repeat a sent delivery only when the head is demonstrably idle without an
acknowledgement or its acknowledgement deadline expired; event age is not a trigger.
    """
    now = time.time()
    delivery = record.delivery
    event_id = str(event["event_id"])
    if delivery.stage == DeliveryStage.IDLE:
        _set_delivery_waiting(delivery, event, reason="observer has unacknowledged significant card work")
        _begin_observer_wake_liveness(record)
    if _precontract_unbound_observer_source(record):
        return _adopt_precontract_unbound_observer(runtime, payload, observers, ref, record, event, now=now)
    provider_observation = _observe_observer_wake_progress(record, runtime, now=now)
    if provider_observation != "disabled":
        # The cursor and its admission are durable before a pane read can cause delivery,
        # replacement or a return that leaves this process.  A fresh cursor is stronger than an
        # idle screen and means this exact observer is already making progress on the same batch.
        observers[ref] = record
        if not _persist_quietly(runtime, payload, observers):
            return {
                "status": "degraded",
                "step": "observer-reconcile",
                "sprint": ref,
                "action": "observer-wake-liveness-persist-failed",
                "head": record.head,
                "reason": "observer provider-progress evidence could not be persisted",
            }
        if provider_observation == "progressed":
            return {
                "status": "ok",
                "step": "observer-reconcile",
                "sprint": ref,
                "action": "observer-wake-progressing",
                "head": record.head,
                "event_id": delivery.through_event or event_id,
                "reason": "an admitted exact-HeadRun provider cursor advanced",
            }
        # Every other observation — a baseline, a stall, an unbound, rejected or unreadable
        # source — carries on to the pane read.  An observation which cannot prove progress is
        # not a verdict about the head: it decides which of the two bounded no-progress ladders
        # below ends the wait, never whether the wait ends at all.
    # A last turn that ended on a provider refusal is not a busy or quiet head (ummanu-108): the
    # resource goes red until its reset and the observer is replaced on the next head of its chain,
    # with the same pending batch, before any nudge or ceiling reads the silence.
    refused = _observer_provider_failure(runtime, record)
    if refused is not None:
        return _replace_observer_for_provider_failure(runtime, payload, observers, ref, record, event, refused)
    if delivery.stage == DeliveryStage.RETRY_DEFERRED and now < delivery.next_at:
        return {
            "status": "degraded",
            "step": "observer-reconcile",
            "sprint": ref,
            "action": "observer-wake-deferred",
            "head": record.head,
            "delivery_id": delivery.delivery_id,
            "event_id": delivery.through_event,
            "reason": delivery.reason,
        }
    active = delivery.stage in {DeliveryStage.DELIVERY_INTENT, DeliveryStage.AWAITING_ACK}
    try:
        status = getattr(runtime.host, "observer_status", lambda _record: {})(record)
    except (HostError, OSError, TypeError, ValueError) as exc:
        if active and now < delivery.deadline:
            # A terminal that could not be read says nothing about the head either way, and this
            # delivery's acknowledgement deadline is still running: waiting costs it nothing.
            return _wake_pending(ref, record)
        if delivery.stage == DeliveryStage.WAITING_FOR_IDLE:
            _new_delivery_intent(
                delivery,
                method="nudge",
                through_event=event_id,
                resume_cursor=str(event.get("latest_resume_id") or ""),
                now=now,
            )
        return _fail_delivery(
            runtime,
            payload,
            observers,
            ref,
            record,
            event,
            f"observer terminal could not be read: {exc}",
        )
    if not isinstance(status, dict) or not status.get("idle"):
        if provider_observation == "stalled" and _observer_has_provider_liveness_contract(record):
            liveness = record.wake_liveness
            liveness.no_progress_evidence = _observer_no_progress_evidence(record, status)
            attempts = liveness.note_busy(now)
            if attempts >= CONTINUATION_NO_PROGRESS_BUSY_ATTEMPTS:
                return _replace_observer_for_no_progress(
                    runtime,
                    payload,
                    observers,
                    ref,
                    record,
                    event,
                    reason=(
                        "the exact observer provider cursor remained unchanged through "
                        f"{attempts} bounded no-progress observations "
                        f"({liveness.no_progress_evidence})"
                    ),
                )
            _set_observer_state(
                record,
                "waiting",
                reason=(
                    "observer terminal is not ready and its exact provider cursor has not advanced "
                    f"({attempts}/{CONTINUATION_NO_PROGRESS_BUSY_ATTEMPTS})"
                ),
            )
            return {
                "status": "ok",
                "step": "observer-reconcile",
                "sprint": ref,
                "action": "observer-wake-no-progress",
                "head": record.head,
                "event_id": delivery.through_event or event_id,
                "attempts": attempts,
                "reason": delivery.reason,
            }
        ceiling = _observer_wake_ceiling_seconds(record)
        if ceiling:
            overrun = _turn_ceiling_overrun(delivery, ceiling, now=now)
            if overrun:
                return _fail_delivery(runtime, payload, observers, ref, record, event, overrun)
        if active and now < delivery.deadline:
            return _wake_pending(ref, record)
        if delivery.stage == DeliveryStage.IDLE:
            _set_delivery_waiting(delivery, event, reason="observer terminal is not ready for a prompt")
        elif delivery.stage == DeliveryStage.WAITING_FOR_IDLE:
            delivery.reason = "observer terminal is not ready for a prompt"
        elif active:
            delivery.reason = (
                "the acknowledgement deadline expired and the observer terminal is still not ready "
                "for a prompt"
            )
        _set_observer_state(record, "waiting", reason=delivery.reason)
        return {
            "status": "ok",
            "step": "observer-reconcile",
            "sprint": ref,
            "action": "observer-wake-waiting",
            "head": record.head,
            "event_id": delivery.through_event or event_id,
            # What the provider source answered for this wait, so a head held by unprovable
            # telemetry is not read back as an ordinary busy observer.
            "admission": provider_observation,
            "reason": delivery.reason,
        }
    redelivery = _redelivery_reason(status, delivery, now=now) if active else ""
    if active and not redelivery:
        return _wake_pending(ref, record)
    if active:
        # A repeat keeps the original high-water mark. A newer B may be visible to the observer,
        # but a resume for this turn still acknowledges only the unconfirmed A batch.
        _new_delivery_intent(
            delivery,
            method="nudge",
            through_event=delivery.through_event,
            resume_cursor=str(event.get("latest_resume_id") or ""),
            now=now,
        )
    elif delivery.stage == DeliveryStage.RETRY_DEFERRED:
        _new_delivery_intent(
            delivery,
            method="nudge",
            through_event=delivery.through_event or event_id,
            resume_cursor=str(event.get("latest_resume_id") or ""),
            now=now,
            delivery_id=delivery.delivery_id,
        )
    elif delivery.stage == DeliveryStage.WAITING_FOR_IDLE and delivery.delivery_id:
        # A direct delivery wait saw this same live pane busy before any send. Keep its marker and
        # high-water mark when it eventually becomes idle; a fresh id would make a later resume
        # unable to acknowledge the intent that was already written before that busy observation.
        _new_delivery_intent(
            delivery,
            method="nudge",
            through_event=delivery.through_event or event_id,
            resume_cursor=str(event.get("latest_resume_id") or ""),
            now=now,
            delivery_id=delivery.delivery_id,
        )
    else:
        _new_delivery_intent(
            delivery,
            method="nudge",
            through_event=event_id,
            resume_cursor=str(event.get("latest_resume_id") or ""),
            now=now,
        )
    observers[ref] = record
    if not _persist_quietly(runtime, payload, observers):
        return _defer_delivery(record, ref, "observer wake intent could not be persisted")
    try:
        # Delivery acceptance is a terminal concern. The causal acknowledgement is deliberately
        # out of band: the next normal reconciliation reads the observer's durable resume once,
        # instead of polling the complete audit stream while the prompt-delivery loop runs.
        accepted = runtime.host.nudge_observer(
            record,
            # `_observer_event_state` made this authoritative full read before delivery intent was
            # entered. Carry that exact snapshot to the document boundary: a second board read here
            # could fail after intent persistence and needlessly list the whole Pipeline again.
            sprint=event["sprint"],
            change=str(event.get("change") or "linked-card"),
            post_merge=list(event.get("post_merge") or []),
        )
    except (AttributeError, HostError, OSError, TypeError, ValueError) as exc:
        evidence = _evidence_of(exc)
        if delivery_readiness_state(evidence) == READINESS_BUSY:
            return _wait_for_busy_delivery(
                record,
                ref,
                "observer terminal became busy before its event wake could be delivered",
                evidence=evidence,
            )
        return _fail_delivery(
            runtime,
            payload,
            observers,
            ref,
            record,
            event,
            f"observer wake failed: {exc}",
            evidence=evidence,
        )
    _count_delivery_attempt(delivery, "nudge")
    delivery.last_evidence = _evidence_of(accepted) or delivery.last_evidence
    delivery.stage = DeliveryStage.AWAITING_ACK
    delivery.sent_at = now
    delivery.held_since = now
    delivery.deadline = now + observer_ack_deadline_seconds()
    delivery.next_at = delivery.deadline
    delivery.reason = redelivery or "observer was nudged for an unacknowledged card event"
    _set_observer_state(record, "running")
    outcome = {
        "status": "ok",
        "step": "observer-reconcile",
        "sprint": ref,
        "action": "observer-redelivered" if redelivery else "observer-nudged",
        "head": record.head,
        "delivery_id": delivery.delivery_id,
        "event_id": delivery.through_event,
    }
    if redelivery:
        outcome["reason"] = redelivery
    return outcome


def _observer_work_state(runtime: Any, ref: str, record: ObserverRecord) -> dict[str, Any]:
    """Classify the live observer without turning an ordinary card wait into a restart.

    A pane Orca reports ready for input means the observer is idle even while a card is in flight:
    the card's next durable transition, not a periodic idle check, provides its next model turn.
    """
    try:
        runtime.sprints.show(ref, include_resume_freshness=False)
    except (HostError, TaskError):
        return {"state": "unknown", "reason": "the sprint could not be read for idle recovery"}
    try:
        status = getattr(runtime.host, "observer_status", lambda _record: {})(record)
    except (HostError, OSError, TypeError, ValueError):
        return {"state": "unknown", "reason": "the observer terminal could not be read for idle recovery"}
    if not isinstance(status, dict) or not status.get("idle"):
        return {"state": "unknown", "reason": "the observer terminal is not ready for a prompt"}
    try:
        last_activity = float(status.get("last_activity"))
    except (TypeError, ValueError):
        return {"state": "unknown", "reason": "the observer terminal has no activity timestamp"}
    return {
        "state": "idle",
        "since": last_activity,
        "reason": "the observer head finished its turn with no unacknowledged significant card event",
    }


def _set_observer_state(record: ObserverRecord, state: str, *, reason: str = "") -> None:
    """Keep work state and reason without turning repeated ticks into new transitions."""
    if record.state == state and record.state_reason == reason:
        return
    changed_state = record.state != state
    record.state = state
    record.state_reason = reason
    if changed_state:
        record.last_action = state
        record.last_action_at = time.time()


def _mark_idle_grace(record: ObserverRecord, *, since: float, reason: str) -> None:
    """Persist a completed queue while it waits for a new linked-card event."""
    if record.state == "idle-grace" and record.idle_since == since and record.idle_reason == reason:
        return
    now = time.time()
    record.state = "idle-grace"
    record.state_reason = reason
    record.idle_since = since
    record.idle_reason = reason
    record.last_action = "idle-grace"
    record.last_action_at = now


def _head_may_be_running(record: ObserverRecord) -> bool:
    """Whether a terminal of this record's head may still be up.

    A launch intent counts: it is written before the host is called, so a record that carries one
    may well have a head behind it, and the workspace is where that head is, handle or no handle.
    """
    return bool(record.handle) or (record.head_possible and bool(record.workspace))


def _needs_teardown(record: ObserverRecord) -> bool:
    """Whether the stop still has something of this record to give back to Orca.

    A head that may be up is one such thing, a workspace the bring-up may have registered is the
    other, and they do not arrive or leave together.
    """
    return _head_may_be_running(record) or (record.workspace_live and bool(record.workspace))


def _bring_up_request_id(ref: str, generation: str, attempt: int) -> str:
    """The id `_launch_observer` gave attempt number `attempt`. The first one is a launch."""
    return observer_request_id("relaunch" if attempt > 1 else "launch", ref, generation, attempt)


def _abandon_launch_intent(runtime: Any, ref: str, record: ObserverRecord) -> None:
    """Settle an intent whose head cannot be found alive.

An already committed attempt is spent; a pending-only attempt retries under
its original identity. Close possible workspace residue before relaunch.
    """
    attempt = record.pending_launch
    record.state = "pending"
    record.state_reason = ""
    record.pending_launch = 0
    if not attempt:
        return
    request_id = _bring_up_request_id(ref, record.generation, attempt)
    audit = getattr(runtime, "audit", None)
    if audit is None:
        return
    try:
        committed = audit.committed_event(request_id) is not None
    except Exception:  # noqa: BLE001 - observer bookkeeping never fails the tick
        committed = False
    if committed:
        record.launches = max(record.launches, attempt)


def _adopt_launch_intent(
    runtime: Any,
    payload: dict[str, Any],
    observers: dict[str, ObserverRecord],
    ref: str,
    record: ObserverRecord,
) -> dict[str, Any] | None:
    """Recover an interrupted observer launch.

Adopt a readable live pid with the reserved attempt. Wait through the initial
heartbeat grace period; otherwise clean up intent residue before relaunch.
    """
    liveness = observer_alive(record)
    if liveness.get("identity_mismatch"):
        return {
            "status": "degraded",
            "step": "observer-reconcile",
            "sprint": ref,
            "action": "observer-heartbeat-identity-mismatch",
            "head": record.head,
            "reason": "observer heartbeat names a live process with a mismatching launch identity",
        }
    if not liveness["alive"]:
        return None
    if not liveness["pid_known"]:
        # Left exactly as it is, intent and all: the next tick resolves it once the grace window
        # has either produced a pid or run out.
        return {
            "status": "skipped",
            "step": "observer-reconcile",
            "sprint": ref,
            "action": "observer-launch-pending",
            "head": record.head,
            "reason": "a launch intent is still within its grace window and has written no pid yet",
            "launches": record.launches,
        }
    attempt = record.pending_launch or record.launches or 1
    now = time.time()
    record.launches = max(record.launches, attempt)
    record.pending_launch = 0
    record.head_possible = True
    record.abandoned_handle = False
    record.state = "running"
    record.state_reason = ""
    record.last_action = "adopted"
    record.last_action_at = now
    record.deferred_reason = ""
    observers[ref] = record
    audited = commit_staged_event(runtime, _bring_up_request_id(ref, record.generation, attempt))
    _persist_quietly(runtime, payload, observers)
    return _with_audit(
        {
            "status": "ok",
            "step": "observer-reconcile",
            "sprint": ref,
            "action": "observer-adopted",
            "reason": "a launch intent was left by a tick that did not finish; its head is alive",
            "head": record.head,
            "launches": record.launches,
        },
        audited,
    )


def observer_decision(runtime: Any, sprint: dict[str, Any]) -> dict[str, Any]:
    """Resolve the observer profile declared by this sprint.

Accept an existing registered head or an explicit 'none'. Never fall back to
role defaults; reject absent, unreadable, provenance-only or unknown metadata.
    """
    value = executable_observer(sprint)
    if value["kind"] == KIND_NONE:
        return {"kind": KIND_NONE, "head": "", "value": value}
    head = str(value["profile"])
    try:
        runtime.catalog.observer_profile(head)
    except (HostError, TaskError) as exc:
        raise ObserverMetadataError(
            REASON_UNKNOWN_PROFILE,
            f"sprint {sprint.get('ref') or '?'} declares observer head {head!r}, which this "
            f"installation cannot resolve: {getattr(exc, 'message', str(exc))}",
        ) from None
    return {"kind": KIND_HEAD, "head": head, "value": value}


def _role_default_observer_head(runtime: Any) -> str:
    """The registry's observer default, for a record filled in without a sprint to read.

    Empty when the registry cannot answer; the launch path reports the same failure properly.
    """
    try:
        return runtime.catalog.observer_head()
    except HostError:
        return ""


def _observer_head_or_blank(runtime: Any, sprint: dict[str, Any] | None = None) -> str:
    """The head to fill a record with, without deciding anything the launch has to decide again.

    Corruption is not reported here: the fence and the launch path are where an unusable observer
    is answered for.
    """
    if sprint is not None:
        try:
            decision = observer_decision(runtime, sprint)
        except ObserverMetadataError:
            return ""
        return str(decision.get("head") or "")
    return _role_default_observer_head(runtime)


def observer_skill_delivery(runtime: Any, head: str) -> dict[str, Any]:
    """Whether the observer skill is in the shell this head runs in.

    The shell is the head's own adapter, read from the same registry the launch command is rendered
    from. A registry that cannot name the adapter reads as undelivered — a head brought up without
    its instructions is worse than a sprint that waits for the next tick.
    """
    try:
        shell = str(runtime.catalog.observer_run(head).adapter or "")
    except (HostError, TaskError) as exc:
        return {
            "delivered": False,
            "paths": [],
            "reason": f"the shell of head {head!r} could not be resolved: "
            f"{getattr(exc, 'message', str(exc))}",
        }
    if not shell:
        return {
            "delivered": False,
            "paths": [],
            "reason": f"head {head!r} names no adapter, so its shell is unknown",
        }
    return skill_delivery(OBSERVER_ROLE, OBSERVER_SKILL, shell)


def _declared_head(runtime: Any, ref: str) -> str:
    """The profile one open sprint declares, read live. Raises rather than substituting one."""
    sprint = runtime.sprints.show(ref, include_cards=False, include_resume_freshness=False)
    decision = observer_decision(runtime, sprint)
    if decision["kind"] == KIND_NONE:
        raise ObserverMetadataError(
            "observer_none", f"sprint {ref} declares no observer, so there is no head to launch"
        )
    head = str(decision.get("head") or "")
    if not head:
        raise HostError(f"no observer head could be resolved for sprint {ref}")
    return head


def _launch_observer(
    runtime: Any,
    payload: dict[str, Any],
    observers: dict[str, ObserverRecord],
    ref: str,
    record: ObserverRecord | None,
    *,
    conditional_stop: bool,
    judgement: QuietStopJudgement | None = None,
    pending_event: dict[str, Any] | None = None,
    head: str = "",
) -> dict[str, Any]:
    """Launch the sprint's declared observer profile.

For ordinary rotation use the captured activity-epoch/liveness judgement to
conditionally stop the predecessor; any new work defers replacement.
Emergency replacement stops unconditionally. A conditional stop without its
judgement is a programming error.
    """
    if conditional_stop and judgement is None:
        raise TypeError("a conditional stop is made on the judgement that asked for it")
    record = record or ObserverRecord(sprint=ref)
    relaunch = record.launches > 0
    if not head:
        # The replacement path after a batch of refused wakes gets here without one. It resolves
        # the sprint's declaration again rather than reusing `record.head`: a re-pointed sprint
        # must move its head at the replacement, not keep the profile the first launch picked.
        try:
            head = _declared_head(runtime, ref)
        except ObserverMetadataError as exc:
            return _defer(runtime, payload, observers, ref, record, head="", reason=exc.message)
        except HostError as exc:
            return _defer(runtime, payload, observers, ref, record, head="", reason=str(exc))
    # The declared profile is only the first one to try (ummanu-108): a red resource walks the
    # profile's fallback chain to the other family. `head` stays the declared profile on the record
    # (the fence and the declaration compare against it); `launch_head` is what actually runs.
    choice = runtime.resolve_head(head)
    readiness = choice.readiness
    if not choice.resolved:
        return _defer(
            runtime,
            payload,
            observers,
            ref,
            record,
            head=head,
            reason=(
                f"head resource {readiness.resource} is {readiness.status}: {readiness.reason}"
                if len(choice.rejected) < 2
                else choice.reason
            ),
            readiness=readiness.to_json(),
        )
    launch_head = choice.head
    # A head whose shell has no observer skill would come up with a prompt pointing at a file it
    # cannot open, and would improvise a sprint from the entity alone. The launch waits instead,
    # and the record says exactly which file is missing.
    delivery = observer_skill_delivery(runtime, launch_head)
    if not delivery["delivered"]:
        return _defer(
            runtime,
            payload,
            observers,
            ref,
            record,
            head=head,
            reason=f"observer role skill is not available to this head: {delivery['reason']}",
        )
    if pending_event is not None and record.wake_liveness.bound and not record.wake_liveness.terminal:
        # Every replacement closes the old exact-run episode before the old process is touched.
        # The launch intent below will move this terminal value to audit-only storage and open a
        # fresh episode for the replacement HeadRun in the same durable write as that new binding.
        record.wake_liveness.terminalize(
            "replacement", "observer HeadRun was replaced while its event delivery was pending"
        )
        observers[ref] = record
        if not _persist_quietly(runtime, payload, observers):
            return {
                "status": "degraded",
                "step": "observer-reconcile",
                "sprint": ref,
                "action": "observer-wake-liveness-persist-failed",
                "head": record.head,
                "reason": "retiring observer wake-liveness outcome could not be persisted",
            }
    if _head_may_be_running(record):  # noqa: SIM102 - the inner stop is commented on its own
        # The pid is dead but the pane it ran in can still be there, the shell left behind that
        # `with_pid_heartbeat` exists to tell apart from a live head. Close it before opening the
        # next one, or every respawn leaves a ghost pane in the observer's workspace. A pane that
        # refuses to close parks the relaunch: two heads on one sprint is worse than none.
        #
        # Which stop that is belongs to the caller. The rotation's is conditional: the head is
        # being replaced because it was judged finished, and a delivery that reached it after that
        # judgement would be typed into a pane this line is about to take away, so the head runtime
        # compares the judgement's epoch, and — only for a head the judgement found alive — the turn
        # it may still be holding, under its own lock, and a head that is not quiet after all parks
        # the relaunch instead. An emergency replacement's is not: it is here because the head is
        # stuck, which is exactly the state a conditional stop refuses in.
        if not stop_observer_head(
            runtime,
            record,
            judgement=judgement if conditional_stop else None,
        ):
            return _defer(
                runtime,
                payload,
                observers,
                ref,
                record,
                head=head,
                reason=(
                    "the previous observer head was not quiet, or its terminal could not be stopped"
                    if conditional_stop
                    else "the previous observer head's terminal could not be stopped"
                ),
            )
    try:
        # The prompt is rendered from the sprint as it reads right now, never from a copy taken
        # when the sprint was created: goal, DoD, repositories and current card all move.
        sprint = runtime.sprints.show(ref, include_resume_freshness=False)
        _observer_comments(sprint)
    except (HostError, TaskError) as exc:
        return _defer(
            runtime,
            payload,
            observers,
            ref,
            record,
            head=head,
            reason=f"observer bring-up failed: {getattr(exc, 'message', str(exc))}",
        )
    kind = EVENT_RELAUNCHED if relaunch else EVENT_LAUNCHED
    attempt = record.launches + 1
    request_id = _bring_up_request_id(ref, record.generation, attempt)
    delivery_event_id = ""
    if pending_event is not None:
        # The lifecycle launch is also this event's only permitted observer turn. The delivery
        # intent is saved by `_write_launch_intent` before `prepare_observer` reaches the host.
        delivery_event_id = _prepare_launch_delivery(record, pending_event)
    try:
        # Staged before the host is asked for anything, so a head that comes up while the process
        # dies mid-launch still has its event staged for the stale-staged settlement to pick up.
        event = stage_event(
            runtime,
            kind,
            ref,
            request_id,
            {
                "head": head,
                **({"launched_head": launch_head} if launch_head != head else {}),
                "launches": attempt,
                **(
                    {
                        "delivery_id": record.delivery.delivery_id,
                        "delivery_method": record.delivery.method,
                        "through_event": delivery_event_id,
                    }
                    if delivery_event_id
                    else {}
                ),
            },
        )
    except OSError as exc:
        if delivery_event_id:
            _defer_delivery(record, ref, f"observer launch event could not be staged: {exc}")
        return _defer(
            runtime,
            payload,
            observers,
            ref,
            record,
            head=head,
            reason=f"observer lifecycle event could not be staged: {exc}",
        )
    intent = _write_launch_intent(
        runtime, payload, observers, ref, record, head, attempt, launch_head=launch_head
    )
    if intent is not None:
        # State that cannot be written means no head: a launch nobody can record is exactly how a
        # sprint ends up with two of them.
        discard_event(runtime, request_id)
        if delivery_event_id:
            _defer_delivery(record, ref, "observer launch delivery intent could not be persisted")
        return _defer(
            runtime,
            payload,
            observers,
            ref,
            record,
            head=head,
            reason=f"observer launch intent could not be persisted: {intent}",
        )
    _bind_codex_provider_ingress(runtime, payload, observers, ref, record)
    try:
        launched = runtime.host.prepare_observer(
            sprint,
            launch_head,
            prompt=render_observer_prompt(
                sprint,
                skill_path=_first_path(delivery),
                delivery=record.delivery,
            ),
            # The identity of the head being launched, read off its record: the sprint it is the
            # observer of and the generation that tells this lifecycle of that reference from the
            # previous one. Not `ref` and not the sprint document, so nothing between the record
            # and the head can hand it another sprint's name.
            identity=observer_binding(record.sprint or ref, record.generation),
            heartbeat_run_id=str(record.head_run.get("run_id") or ""),
            # The path the intent above fixed on disk: a record that already names one keeps it.
            recorded_workspace=record.workspace,
        )
    except ObserverLaunchAborted as exc:
        # The bring-up failed with its terminal still up. The staged event is dropped, because no
        # observer was launched, but the handle is kept so the next tick closes that terminal
        # first and only then opens the replacement.
        discard_event(runtime, request_id)
        _clear_launch_intent(record, head_possible=bool(exc.handle))
        if delivery_event_id:
            _defer_delivery(
                record,
                ref,
                f"observer replacement launch failed: {exc}",
                method="launch",
                evidence=dict(exc.evidence),
            )
        elif exc.evidence:
            # No batch was owed, so there is no retry state to write — but a prompt was delivered
            # to that head and did not land, and that is the sprint's evidence either way.
            _count_delivery_failure(
                record.delivery,
                "launch",
                f"observer launch prompt failed: {exc}",
                dict(exc.evidence),
            )
        if exc.handle:
            record.handle = exc.handle
            record.leaf = exc.leaf
            record.workspace = exc.workspace or record.workspace
            record.pid_file = exc.pid_file or record.pid_file
            record.run = exc.run or record.run
            record.abandoned_handle = True
        return _defer(
            runtime,
            payload,
            observers,
            ref,
            record,
            head=head,
            reason=f"observer bring-up failed: {exc}",
        )
    except (HostError, TaskError) as exc:
        # Nothing came up, so the staged event describes a launch that never happened. A host that
        # got as far as opening a terminal reports it as `ObserverLaunchAborted` above, so there is
        # nothing left to close here either.
        discard_event(runtime, request_id)
        _clear_launch_intent(record, head_possible=False)
        if delivery_event_id:
            _defer_delivery(
                record,
                ref,
                f"observer replacement launch failed: {getattr(exc, 'message', str(exc))}",
                method="launch",
                evidence=_evidence_of(exc),
            )
        return _defer(
            runtime,
            payload,
            observers,
            ref,
            record,
            head=head,
            reason=f"observer bring-up failed: {getattr(exc, 'message', str(exc))}",
        )
    now = time.time()
    record.head = head
    record.fallback_head = launch_head if launch_head != head else ""
    record.workspace = str(launched.get("workspace") or "")
    record.handle = str(launched.get("handle") or "")
    record.leaf = str(launched.get("leaf") or "")
    record.pid_file = str(launched.get("pid_file") or observer_pid_file(ref))
    record.run = launched.get("run") if isinstance(launched.get("run"), dict) else {}
    record.head_run = (
        dict(launched["head_run"]) if isinstance(launched.get("head_run"), dict) else record.head_run
    )
    if delivery_event_id and not record.wake_liveness.bound:
        # Non-Codex/fake hosts may only return the complete immutable HeadRun after launch.  Bind
        # the pending delivery now, before this result can be persisted or any next-tick probe can
        # run. Production Codex normally did this in the preflight launch intent above.
        record.wake_liveness = ObserverWakeLiveness.begin(record.head_run)
    # The host has answered, so the create-time pane identity belongs on the durable launch intent
    # before the ordinary state commit below.  A crash in the rest of this branch then adopts this
    # exact pane rather than falling back to the create-time handle or another inventory lookup.
    observers[ref] = record
    _persist_quietly(runtime, payload, observers)
    record.abandoned_handle = False
    record.launches = attempt
    record.pending_launch = 0
    record.bound = True
    record.head_possible = True
    record.workspace_live = True
    record.state = "running"
    record.state_reason = ""
    record.launched_at = now
    record.last_action = "relaunched" if relaunch else "launched"
    record.last_action_at = now
    record.deferred_reason = ""
    record.launch_attempts = 0
    record.launch_next_at = 0.0
    record.stopped_reason = ""
    record.paused_at = 0.0
    if launched.get("prompt_delivered"):
        # Every launch prompt is one launch delivery, batch or no batch. The first launch of a
        # sprint carries no pending event and still puts a prompt in front of a head; a sprint
        # that lost that prompt has to be able to say so at closeout.
        _count_delivery_attempt(record.delivery, "launch")
        evidence = launched.get("delivery_evidence")
        if isinstance(evidence, dict) and evidence:
            record.delivery.last_evidence = dict(evidence)
    if delivery_event_id:
        record.delivery.stage = DeliveryStage.AWAITING_ACK
        record.delivery.sent_at = now
        record.delivery.held_since = now
        record.delivery.deadline = now + observer_ack_deadline_seconds()
        record.delivery.next_at = record.delivery.deadline
        record.delivery.reason = "replacement launch is pending confirmation for an unacknowledged card event"
    observers[ref] = record
    if event is not None:
        event["payload"]["workspace"] = record.workspace
    outcome = {
        "status": "ok",
        "step": "observer-reconcile",
        "sprint": ref,
        "action": "observer-relaunched" if relaunch else "observer-launched",
        "head": head,
        "workspace": record.workspace,
        "launches": record.launches,
    }
    if delivery_event_id:
        outcome["delivery_id"] = record.delivery.delivery_id
        outcome["event_id"] = delivery_event_id
    # A failure here is not a lost head: the intent on disk still names this launch, and the next
    # tick adopts the head it started instead of opening a second one.
    if not _persist_quietly(runtime, payload, observers):
        outcome["state"] = "pending"
        outcome["status"] = "degraded"
    return _with_audit(outcome, commit_event(runtime, event))


def _bind_codex_provider_ingress(
    runtime: Any,
    payload: dict[str, Any],
    observers: dict[str, ObserverRecord],
    ref: str,
    record: ObserverRecord,
) -> None:
    """Bind the observer's persisted HeadRun to the only provider-event ingress."""
    stored = record.head_run if isinstance(record.head_run, dict) else {}
    if not stored.get("run_id"):
        return
    try:
        run = head_ops.HeadRun.from_json(stored)
    except (head_ops.HeadRunError, head_ops.TaskRefError):
        return
    if run.spec.adapter != "codex" or not isinstance(run.fanout_policy.get("provider_source"), dict):
        return

    def persist(updated: head_ops.HeadRun) -> None:
        if not updated.same_run(run):
            raise HostError("observer provider event writer was handed another HeadRun")
        updated_json = updated.to_json()
        existing = record.head_run if isinstance(record.head_run, dict) else {}
        if existing.get("run_id"):
            updated_json = merge_launch_head_run(existing, updated_json)
        record.head_run = updated_json
        record.workspace = updated.workspace or record.workspace
        record.handle = updated.handle or record.handle
        record.leaf = updated.leaf or record.leaf
        record.pid_file = updated.pid_file or record.pid_file
        observers[ref] = record
        if not _persist_quietly(runtime, payload, observers):
            raise OSError("observer provider event state could not be durably saved")

    def stop(updated: head_ops.HeadRun, reason: str) -> None:
        # ``stop_observer_head`` asks the host to identity-fence the run before it signals or
        # removes the worktree.  A foreign heartbeat is kept on the record and never signalled.
        stop_observer_head(runtime, record)

    def block(evidence: dict[str, Any]) -> None:
        record.state = "blocked"
        record.state_reason = ""
        record.deferred_reason = (
            "Codex provider fan-out policy blocked observer: "
            f"{evidence.get('state') or 'unknown'}; {evidence.get('reason') or 'provider event observed'}"
        )
        record.last_action = "codex-provider-fanout-blocked"
        record.last_action_at = time.time()
        observers[ref] = record
        _persist_quietly(runtime, payload, observers)
        record_event(
            runtime,
            EVENT_PROVIDER_FANOUT_BLOCKED,
            ref,
            observer_request_id("codex-provider-event-blocked", ref, record.generation, record.launches),
            {"policy_evidence": dict(evidence), "head": record.head},
        )

    runtime.host.configure_codex_provider_ingress(run, persist=persist, stop=stop, block=block)


def _poll_codex_provider_ingress(
    runtime: Any,
    payload: dict[str, Any],
    observers: dict[str, ObserverRecord],
    ref: str,
    record: ObserverRecord,
) -> dict[str, Any] | None:
    if _precontract_unbound_observer_source(record):
        # This record predates the descriptor/baseline contract.  In particular, do not install
        # an ingress which would scan the workspace and bind a journal retroactively; the pending
        # event path replaces this exact old HeadRun under its normal identity fence.
        return None
    _bind_codex_provider_ingress(runtime, payload, observers, ref, record)
    if not isinstance(record.head_run, dict) or not record.head_run.get("run_id"):
        # A record from before runtime HeadRuns cannot be identified as Codex from this field.
        # It is not promoted to an allowed policy state, but it remains on its established
        # non-provider lifecycle path rather than being misclassified as a Codex source failure.
        return None
    try:
        run = head_ops.HeadRun.from_json(record.head_run)
    except (head_ops.HeadRunError, head_ops.TaskRefError):
        # Older launch intents had a run id before the full HeadRun was returned by the host.
        # They cannot be identified as an attested Codex source, so leave their existing launch
        # recovery path intact.  Current Codex preflight records a complete source-bearing run.
        return None
    if run.spec.adapter != "codex" or not isinstance(run.fanout_policy.get("provider_source"), dict):
        return None
    try:
        runtime.host.poll_codex_provider_ingress(run)
    except (CodexProviderSourceError, CodexFanoutRecordingError) as exc:
        return {
            "status": "blocked",
            "step": "observer-reconcile",
            "sprint": ref,
            "action": "codex-provider-fanout-blocked",
            "policy_evidence": {"kind": "codex_provider_fanout", "state": "unknown"},
            "reason": str(exc),
        }
    return None


def _write_launch_intent(
    runtime: Any,
    payload: dict[str, Any],
    observers: dict[str, ObserverRecord],
    ref: str,
    record: ObserverRecord,
    head: str,
    attempt: int,
    *,
    launch_head: str = "",
) -> str | None:
    """Persist the observer launch intent before calling the host.

Keep the declared profile and the actual fallback launch profile separate.
Bind the provider episode to the profile that will run; compute workspace/pid
identity before the host responds so interrupted launches remain recoverable.
    """
    previous = record.to_json()
    now = time.time()
    try:
        workspace = record.workspace or str(runtime.host.observer_workspace(ref))
        pid_file = record.pid_file or str(runtime.host.observer_pid_file(ref))
    except Exception as exc:  # noqa: BLE001 - observer bookkeeping never fails the tick
        # Without the workspace the head could not be found again, and without the pid file its
        # liveness could not be read: an intent that names neither is not worth launching against.
        return f"{type(exc).__name__}: {exc}"
    run_id = uuid.uuid4().hex
    preflight_run: dict[str, Any] | None = None
    attest = getattr(runtime.host, "preflight_codex_run", None)
    if callable(attest):
        try:
            candidate = attest(
                launch_head or head,
                role=OBSERVER_ROLE,
                workspace=workspace,
                task_ref=head_ops.TaskRef.sprint(ref),
                pid_file=pid_file,
                run_id=run_id,
            )
        except Exception as exc:  # noqa: BLE001 - observer bookkeeping never fails the tick
            return f"codex-fanout-policy: {type(exc).__name__}: {exc}"
        preflight_run = candidate.to_json()
    record.head = head
    record.fallback_head = launch_head if launch_head and launch_head != head else ""
    record.workspace = workspace
    record.pid_file = pid_file
    record.head_run = preflight_run or {"run_id": run_id}
    if record.delivery.stage != DeliveryStage.IDLE and record.wake_liveness.terminal:
        # The old episode remains readable as evidence, but never participates in decisions for
        # the replacement.  Opening the new episode in this launch-intent write means a crash
        # cannot expose the new HeadRun with the retired run's baseline or recovery ladder.
        record.retired_wake_liveness = record.wake_liveness.to_json()
        record.wake_liveness = ObserverWakeLiveness.begin(record.head_run)
    record.pending_launch = attempt
    record.head_possible = True
    record.workspace_live = True
    # The identity is rendered into the command this intent is written for, so a head adopted from
    # the intent is a bound head: the flag belongs to the launch, not to its confirmation.
    record.bound = True
    record.state = "launching"
    record.state_reason = ""
    record.launched_at = now
    record.last_action = "launching"
    record.last_action_at = now
    record.deferred_reason = ""
    record.stopped_reason = ""
    record.paused_at = 0.0
    observers[ref] = record
    try:
        put_observers(payload, observers)
        runtime.production_state.save(payload)
    except Exception as exc:  # noqa: BLE001 - observer bookkeeping never fails the tick
        for name, value in previous.items():
            if name == "delivery":
                record.delivery = ObserverDelivery.from_json(value)
            elif name == "wake_liveness":
                record.wake_liveness = ObserverWakeLiveness.from_json(value)
            else:
                setattr(record, name, value)
        # Written only when set, so a previous record without it had none.
        record.fallback_head = str(previous.get("fallback_head") or "")
        return f"{type(exc).__name__}: {exc}"
    return None


def _clear_launch_intent(record: ObserverRecord, *, head_possible: bool) -> None:
    """Take back a launch intent whose host call answered. `head_possible` is what it answered."""
    record.pending_launch = 0
    record.head_possible = head_possible
    record.state = "pending"
    record.state_reason = ""


def _observer_launch_backoff(attempts: int) -> int:
    """The persisted delay one bring-up attempt of an observer head buys. One series, one place."""
    return min(
        OBSERVER_WAKE_RETRY_INITIAL_SECONDS * (2 ** (max(attempts, 1) - 1)),
        OBSERVER_WAKE_RETRY_MAX_SECONDS,
    )


def _hold_dead_head_bringup(record: ObserverRecord, *, attempts: int) -> None:
    """Stamp the cooldown a successful replacement of a dead head leaves on its record.

    The same two fields every other deferral uses, so there is one bound and one clock, but the
    state is left alone: this head *is* up, and calling it deferred would fence its own sprint's
    cards behind `REASON_DEFERRED` for as long as the cooldown lasts.
    """
    delay = _observer_launch_backoff(attempts)
    record.launch_attempts = attempts
    record.launch_next_at = time.time() + delay
    record.deferred_reason = (
        "the observer head was replaced after its launch identity said it had died; "
        f"another replacement no sooner than {delay}s"
    )


def _defer(
    runtime: Any,
    payload: dict[str, Any],
    observers: dict[str, ObserverRecord],
    ref: str,
    record: ObserverRecord | None,
    *,
    head: str,
    reason: str,
    action: str = "observer-launch-deferred",
    readiness: dict[str, Any] | None = None,
    keep_state: bool = False,
    retry: bool = True,
) -> dict[str, Any]:
    """Defer launch while preserving the current sprint and intent.

Change only deferral fields (or preserve state entirely with keep_state).
Failed launches back off exponentially; deliberate drains retry after drain.
    """
    record = record or ObserverRecord(sprint=ref)
    policy_refusal = reason.startswith("codex-fanout-policy:")
    record.head = record.head or head
    if policy_refusal:
        record.state = "blocked"
        record.state_reason = ""
        retry = False
        action = "codex-fanout-policy-blocked"
    elif not keep_state:
        record.state = "deferred"
        record.state_reason = ""
    if retry:
        record.launch_attempts += 1
        delay = _observer_launch_backoff(record.launch_attempts)
        record.launch_next_at = time.time() + delay
        record.deferred_reason = f"{reason}; retry in {delay}s"
    else:
        record.launch_attempts = 0
        record.launch_next_at = 0.0
        record.deferred_reason = reason
    record.last_action = "launch-deferred"
    record.last_action_at = time.time()
    observers[ref] = record
    audited = record_event(
        runtime,
        EVENT_DEFERRED,
        ref,
        observer_request_id("deferred", ref, record.generation, record.launches),
        {"head": record.head, "reason": record.deferred_reason, "launches": record.launches},
    )
    outcome = {
        "status": "blocked" if policy_refusal else "skipped",
        "step": "observer-reconcile",
        "sprint": ref,
        "action": action,
        "head": record.head,
        "reason": record.deferred_reason,
    }
    if policy_refusal:
        outcome["policy_evidence"] = {"kind": "codex_provider_fanout", "state": "unknown"}
    if readiness is not None:
        outcome["readiness"] = readiness
    _persist_quietly(runtime, payload, observers)
    return _with_audit(outcome, audited)


def _stop_observer(
    runtime: Any,
    payload: dict[str, Any],
    observers: dict[str, ObserverRecord],
    ref: str,
    *,
    reason: str,
) -> dict[str, Any]:
    record = observers[ref]
    request_id = observer_request_id("stop", ref, record.generation, record.launches)
    try:
        event = stage_event(
            runtime,
            EVENT_STOPPED,
            ref,
            request_id,
            {"head": record.head, "reason": reason, "launches": record.launches},
        )
    except OSError as exc:
        # The event has to be on disk before the terminal is gone, so an unwritable audit parks
        # the stop rather than performing an unrecordable one. The head stays up and is retried.
        _mark_stop_pending(record, STATE_STOP_PENDING, reason)
        return {
            "status": "degraded",
            "step": "observer-reconcile",
            "sprint": ref,
            "action": "observer-stop-failed",
            "head": record.head,
            "reason": f"{reason}, and the stop could not be staged in the audit log: {exc}",
        }
    if not stop_observer_head(runtime, record):
        discard_event(runtime, request_id)
        _mark_stop_pending(record, STATE_STOP_PENDING, reason)
        return {
            "status": "degraded",
            "step": "observer-reconcile",
            "sprint": ref,
            "action": "observer-stop-failed",
            "head": record.head,
            "reason": f"{reason}, and the observer terminal could not be stopped",
        }
    observers.pop(ref)
    outcome = {
        "status": "ok",
        "step": "observer-reconcile",
        "sprint": ref,
        "action": "observer-stopped",
        "head": record.head,
        "reason": reason,
    }
    _persist_quietly(runtime, payload, observers)
    return _with_audit(outcome, commit_event(runtime, event))


def _observer_activity_epoch(runtime: Any, record: ObserverRecord) -> int:
    """Read the runtime's activity epoch for a conditional stop.

Use zero if unavailable, making the conditional stop fail closed rather than
turning it into an unconditional stop.
    """
    try:
        return int(runtime.host.observer_activity_epoch(record))
    except (HostError, AttributeError, TypeError, ValueError):
        return 0


def _mark_stop_pending(record: ObserverRecord, state: str, reason: str) -> None:
    """Keep the head on the books after a refused stop, handle included, so the retry can find it."""
    now = time.time()
    record.state = state
    record.state_reason = reason
    record.stopped_reason = reason
    record.last_action = "stop-failed"
    record.last_action_at = now
    if state == STATE_PAUSE_STOP_PENDING:
        record.paused_at = now


def stop_observer_head(
    runtime: Any, record: ObserverRecord, *, judgement: QuietStopJudgement | None = None
) -> bool:
    """Stop an observer; return false when the host cannot confirm removal.

Ordinary rotation supplies a judgement with activity epoch and pid liveness:
the runtime checks the same head is still quiet under its lock. Emergency,
operator, freeze and policy stops are unconditional. Preserve the record for
any refused stop, including a live or indeterminate head.
    """
    if not _needs_teardown(record):
        return True
    if observer_alive(record).get("identity_mismatch"):
        # The recorded PID is a live foreign process.  Keep the record and let an operator resolve
        # the owned pane rather than signalling or replacing over it.
        return False
    try:
        if judgement is None:
            runtime.host.stop_observer(record)
        elif not runtime.host.stop_observer_if_quiescent(
            record, judgement.activity_epoch, judgement.head_process_alive
        ):
            return False
    except HostError:
        return False
    record.handle = ""
    record.leaf = ""
    record.head_possible = False
    record.workspace_live = False
    record.abandoned_handle = False
    return True


def freeze_observers(runtime: Any, payload: dict[str, Any], *, reason: str) -> dict[str, list[str]]:
    """Stop every observer head and say in the record why it is gone.

    Returns the sprints whose head is down under `stopped` and those whose stop the host refused
    under `failed`. A refused stop keeps its handle and is retried by the frozen tick.
    """
    observers = load_observers(payload)
    if not observers:
        return {"stopped": [], "failed": []}
    stopped: list[str] = []
    failed: list[str] = []
    try:
        for ref, record in sorted(observers.items()):
            if not _head_may_be_running(record):
                continue
            if _stop_for_pause(runtime, ref, record, reason):
                stopped.append(ref)
            else:
                failed.append(ref)
    finally:
        put_observers(payload, observers)
    return {"stopped": stopped, "failed": failed}


def _stop_for_pause(runtime: Any, ref: str, record: ObserverRecord, reason: str) -> bool:
    """Take one head down for a freeze. False leaves it on the books as a pending stop."""
    request_id = observer_request_id("freeze-stop", ref, record.generation, record.launches)
    try:
        event = stage_event(
            runtime,
            EVENT_STOPPED,
            ref,
            request_id,
            {"head": record.head, "reason": reason, "launches": record.launches},
        )
    except OSError:
        _mark_stop_pending(record, STATE_PAUSE_STOP_PENDING, reason)
        return False
    if not stop_observer_head(runtime, record):
        discard_event(runtime, request_id)
        _mark_stop_pending(record, STATE_PAUSE_STOP_PENDING, reason)
        return False
    _mark_stopped_by_pause(record, reason)
    commit_event(runtime, event)
    return True


def _mark_stopped_by_pause(record: ObserverRecord, reason: str) -> None:
    now = time.time()
    record.handle = ""
    record.leaf = ""
    record.abandoned_handle = False
    record.state = STATE_STOPPED_BY_PAUSE
    record.state_reason = reason
    record.stopped_reason = reason
    record.paused_at = now
    record.last_action = "stopped-by-pause"
    record.last_action_at = now


def retry_pending_observer_stops(runtime: Any, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Retry pending observer stops, returning one classified result per attempt.

Run this path even when production reconciliation is frozen. Repeated refusal
must be visible as degraded instead of silently leaving a head running.
    """
    observers = load_observers(payload)
    pending = {
        ref: record
        for ref, record in sorted(observers.items())
        if record.state in PENDING_STOP_STATES and _needs_teardown(record)
    }
    if not pending:
        return []
    rows: list[dict[str, Any]] = []
    try:
        for ref, record in pending.items():
            reason = record.stopped_reason
            if record.state == STATE_PAUSE_STOP_PENDING:
                if _stop_for_pause(runtime, ref, record, reason):
                    rows.append(_retry_row(ref, "observer-stopped-by-pause", reason, ok=True))
                else:
                    rows.append(_retry_row(ref, "observer-stop-failed", reason))
                continue
            request_id = observer_request_id("stop", ref, record.generation, record.launches)
            try:
                event = stage_event(
                    runtime,
                    EVENT_STOPPED,
                    ref,
                    request_id,
                    {"head": record.head, "reason": reason, "launches": record.launches},
                )
            except OSError as exc:
                rows.append(
                    _retry_row(
                        ref,
                        "observer-stop-failed",
                        f"{reason}, and the stop could not be staged in the audit log: {exc}",
                    )
                )
                continue
            if not stop_observer_head(runtime, record):
                discard_event(runtime, request_id)
                rows.append(
                    _retry_row(
                        ref,
                        "observer-stop-failed",
                        f"{reason}, and the observer terminal could not be stopped",
                    )
                )
                continue
            observers.pop(ref)
            commit_event(runtime, event)
            rows.append(_retry_row(ref, "observer-stopped", reason, ok=True))
    finally:
        put_observers(payload, observers)
    return rows


def _retry_row(ref: str, action: str, reason: str, *, ok: bool = False) -> dict[str, Any]:
    return {
        "status": "ok" if ok else "degraded",
        "step": "observer-stop-retry",
        "sprint": ref,
        "action": action,
        "reason": reason,
    }


def resume_observers(payload: dict[str, Any]) -> list[str]:
    """Clear the freeze marks so the next tick brings the observers back.

    Resume launches nothing itself: the tick's own reconciliation is the single bring-up path.
    """
    observers = load_observers(payload)
    if not observers:
        return []
    resumed: list[str] = []
    for ref, record in sorted(observers.items()):
        # A freeze-stop the host refused is cleared too: the sprint is running again, so the head
        # that survived the freeze is the head of this sprint and the tick can find it alive.
        if record.state not in (STATE_STOPPED_BY_PAUSE, STATE_PAUSE_STOP_PENDING):
            continue
        record.state = "pending"
        record.state_reason = ""
        record.paused_at = 0.0
        record.stopped_reason = ""
        resumed.append(ref)
    put_observers(payload, observers)
    return resumed


def _persist_quietly(runtime: Any, payload: dict[str, Any], observers: dict[str, ObserverRecord]) -> bool:
    """Flush the observer records mid-tick. False means only the tick's own save carries them now.

    Never raised at the caller: the head this record describes is already up or already gone, and
    the launch intent that precedes every bring-up keeps that decision recoverable.
    """
    put_observers(payload, observers)
    try:
        runtime.production_state.save(payload)
    except Exception:  # noqa: BLE001 - observer bookkeeping never fails the tick
        return False
    return True


def _first_path(delivery: dict[str, Any]) -> str:
    paths = delivery.get("paths") or []
    return str(paths[0]) if paths else ""


def render_observer_wake_context(
    sprint: dict[str, Any],
    *,
    change: str,
    delivery: ObserverDelivery | None = None,
    post_merge: list[dict[str, Any]] | None = None,
) -> str:
    """Wake text with the same live owner-decision context a replacement launch receives.

    A post-merge CI result in the batch is stated first, as the fact it is: `green`, `red`,
    `absent` or `timeout`, with the run and, for a red one, the failed checks and classification.
    """
    changed = "The sprint entity changed." if change == "sprint-entity" else "A linked card changed."
    facts = [render_fact(fact) for fact in post_merge or [] if isinstance(fact, dict)]
    if facts:
        changed = (
            "A release's post-merge CI on the integration base has answered. Decide the next step "
            "on this result, not on the card's Done:\n\n" + "\n\n".join(facts) + "\n\n" + changed
        )
    lead = (
        changed + " Read its worker report, reviewer verdict and any valid executed dispatcher-owned "
        "exact-SHA gate receipt first. Suppress a routine broad rerun only when that receipt "
        "exists; none/noop/missing evidence proves no broad suite, so run or request appropriate "
        "validation when the decision needs it. Keep a worker-local broad receipt with the worker. "
        "It does not suppress that rerun. The delivery marker is only the immutable audit batch "
        "boundary; the live sprint context below may be newer. Apply all owner decisions before "
        "taking the next semantic step, then record resume."
    )
    return lead + "\n\n" + render_observer_prompt(sprint, delivery=delivery)


