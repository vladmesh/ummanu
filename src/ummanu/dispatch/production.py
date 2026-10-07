"""Production dispatcher loop for the shared Ready queue."""

from __future__ import annotations

import contextlib
import contextvars
import copy
import json
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ummanu._fsutil import try_file_lock, write_json
from ummanu.board.extension_bag import EXTENSION_BAG
from ummanu.board.models import Event, EventKind
from ummanu.board.terminal_taxonomy import (
    TerminalTaxonomyValidationError,
    budget_event_type,
    read_terminal_taxonomy,
)
from ummanu.checkpoint import checkpoint_snapshot
from ummanu.dispatch import attempt_accounting
from ummanu.dispatch.claim import claim_ready_task
from ummanu.dispatch.cleanup import serialized
from ummanu.dispatch.e2e_after_merge import after_merge_snapshot, reconcile_after_merge
from ummanu.dispatch.host import CommandHostRuntime
from ummanu.dispatch.launch import (
    FAILURE_CLASS_INFRASTRUCTURE,
    REVIEW_ROLE,
    WORKER_ROLE,
    bring_up_failure_class,
    forget_role_head,
    launch_intent,
    stop_launch_intent,
)
from ummanu.dispatch.observer import (
    observer_snapshot,
    reconcile_observers,
    retry_pending_observer_stops,
)
from ummanu.dispatch.observer_fence import fenced_task, observer_fence
from ummanu.dispatch.origin_returns import reconcile_origin_returns
from ummanu.dispatch.pause_ops import auto_resume_expired_freeze
from ummanu.dispatch.po_cards import completion_state
from ummanu.dispatch.post_merge import WATCHES_KEY, reconcile_post_merge_watches
from ummanu.dispatch.provider_failure import is_provider_unavailable_return
from ummanu.dispatch.state import (
    DispatcherRecord,
    attempt_request_id as _attempt_request_id,
    close_divergence,
    divergence_is_open,
    is_claim_skip,
    new_attempt_id,
    now_rfc3339,
    record_divergence,
    request_token,
)
from ummanu.dispatch.tick_telemetry import TICK_TELEMETRY_RECENT_KEPT
from ummanu.dispatch.types import STOPPED_BY_RECONCILIATION, HostError
from ummanu.dispatch.wait_cards import pending_wait_blockers
from ummanu.sprints import SprintWriter, budget_thresholds
from ummanu.tasks import ACTIVE_STATES, WAIT_OUTCOME_KEY, TaskError

# Tick telemetry records terminal health for pipeline and steward readers.
TICK_TELEMETRY_UNHEALTHY_KEPT = 50
TICK_TELEMETRY_ERRORS_KEPT = 5
TICK_TELEMETRY_DEGRADATIONS_KEPT = 5
# Frozen ticks are deliberately healthy, not outages.
HEALTHY_TICK_STATUSES = frozenset({"ok", "skipped"})
# Unfinished actions and observer fences degrade telemetry; ordinary blocked cards do not.
DEGRADED_ACTION_STATUSES = frozenset({"degraded", "failed", "critical"})

# The timer fires every minute, while the normalized board/run projection is a
# recovery artifact rather than the dispatcher's live source of truth.  Its
# cadence deliberately lives here, at the production-state durability boundary.
CHECKPOINT_INTERVAL_SECONDS = 5 * 60


#: Where the clock of the tick currently being served lives. Every terminal record a tick makes is
#: written somewhere down its own call tree — the working tick's own save, the frozen tick's, the
#: fence refusal's, and the recovery write of a tick that died on an exception — and threading a
#: start time through all four would put the same parameter on a dozen signatures that have no other
#: reason to know the time. A context variable is set once, at the top of `production_tick`, and read
#: back by `record_tick_telemetry`, which is the one place every one of those records is built.
_TICK_STARTED: contextvars.ContextVar[float | None] = contextvars.ContextVar("tick_started", default=None)
_TICK_PHASES: contextvars.ContextVar[dict[str, float] | None] = contextvars.ContextVar("tick_phases", default=None)
_TICK_PHASE_STACK: contextvars.ContextVar[list[list[float]] | None] = contextvars.ContextVar(
    "tick_phase_stack", default=None
)


@contextlib.contextmanager
def tick_clock() -> Iterator[None]:
    """Run the wall clock of one tick, for the duration every terminal record carries."""
    token = _TICK_STARTED.set(time.perf_counter())
    phases_token = _TICK_PHASES.set({})
    stack_token = _TICK_PHASE_STACK.set([])
    try:
        yield
    finally:
        _TICK_PHASE_STACK.reset(stack_token)
        _TICK_PHASES.reset(phases_token)
        _TICK_STARTED.reset(token)


@contextlib.contextmanager
def tick_phase(name: str) -> Iterator[None]:
    """Accumulate exclusive elapsed milliseconds, including a phase interrupted by an exception.

    Board snapshots can occur inside launches. Subtract nested spans from their parent so each
    millisecond belongs to one phase. Outside a tick this helper has no clock or side effects.
    """
    phases = _TICK_PHASES.get()
    stack = _TICK_PHASE_STACK.get()
    if phases is None or stack is None:
        yield
        return
    frame = [time.perf_counter(), 0.0]
    stack.append(frame)
    try:
        yield
    finally:
        elapsed = (time.perf_counter() - frame[0]) * 1000.0
        stack.pop()
        phases[name] = phases.get(name, 0.0) + max(0.0, elapsed - frame[1])
        if stack:
            stack[-1][1] += elapsed


def tick_duration_ms() -> float | None:
    """How long the tick being served has run, in milliseconds, or None outside a tick.

    None is the honest answer for a record built outside `production_tick` — a test that folds an
    outcome in by hand has no tick and so has no duration, and a zero there would read as a tick
    that cost nothing.
    """
    started = _TICK_STARTED.get()
    return None if started is None else round((time.perf_counter() - started) * 1000.0, 3)


def degraded_actions(outcomes: Any) -> list[dict[str, Any]]:
    """Action outcomes of a tick that report a failed operation, in the order they happened."""
    return [
        outcome
        for outcome in (outcomes or [])
        if isinstance(outcome, dict) and str(outcome.get("status") or "") in DEGRADED_ACTION_STATUSES
    ]


def _counter(value: Any) -> int:
    """A monotonic counter read back from state, defaulting to 0 for anything unusable."""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def record_tick_telemetry(payload: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Fold one terminal tick outcome into `payload["tick_telemetry"]` and return it.

    Called with the result the tick is about to return, right before the state is saved, so the
    durable record and the returned outcome cannot disagree. A tick that never reaches a save
    deliberately records nothing: taking the state file would write outside the lock or across an
    ownership fence, and both leave their own evidence already.
    """
    telemetry = payload.get("tick_telemetry")
    telemetry = dict(telemetry) if isinstance(telemetry, dict) else {}
    # Generation distinguishes telemetry histories after state replacement.
    if not str(telemetry.get("generation") or ""):
        telemetry["generation"] = uuid.uuid4().hex
    seq = _counter(telemetry.get("tick_seq")) + 1
    status = str(result.get("status") or "")
    errors = [error for error in (result.get("errors") or []) if isinstance(error, dict)]
    # Health includes action outcomes, not only top-level status.
    degradations = degraded_actions(result.get("actions"))
    duration = tick_duration_ms()
    phases = {name: round(elapsed, 3) for name, elapsed in (_TICK_PHASES.get() or {}).items()}
    if duration is not None:
        # Independent rounding can overshoot a very short tick by a few microseconds.
        excess = round(sum(phases.values()) - duration, 3)
        if excess > 0 and phases:
            largest = max(phases, key=lambda name: phases[name])
            phases[largest] = round(max(0.0, phases[largest] - excess), 3)
        phases["other"] = round(max(0.0, duration - sum(phases.values())), 3)
    entry = {
        "seq": seq,
        "at": now_rfc3339(),
        "status": status,
        "step": str(result.get("step") or ""),
        "healthy": status in HEALTHY_TICK_STATUSES and not degradations,
        # How long this tick ran, beside the outcome it ran to. The record is made right before the
        # save, so it covers everything the tick did up to the moment it became durable.
        "duration_ms": duration,
        "phases": phases,
        "reason": str(result.get("reason") or ""),
        "actions": len(result.get("actions") or []),
        "error_count": len(errors),
        "degraded_count": len(degradations),
        "degradations": [
            {
                "ref": str(outcome.get("ref") or outcome.get("pilot_ref") or outcome.get("sprint") or ""),
                "step": str(outcome.get("step") or ""),
                "status": str(outcome.get("status") or ""),
                "action": str(outcome.get("action") or ""),
                "reason": str(outcome.get("reason") or ""),
            }
            for outcome in degradations[:TICK_TELEMETRY_DEGRADATIONS_KEPT]
        ],
        "errors": [
            {
                "ref": str(error.get("ref") or ""),
                "code": str(error.get("code") or ""),
                "message": str(error.get("message") or ""),
            }
            for error in errors[:TICK_TELEMETRY_ERRORS_KEPT]
        ],
    }
    telemetry["tick_seq"] = seq
    telemetry["last"] = entry
    recent = telemetry.get("recent")
    recent = recent[-(TICK_TELEMETRY_RECENT_KEPT - 1):] if isinstance(recent, list) else []
    telemetry["recent"] = [
        *recent,
        {key: entry[key] for key in ("seq", "at", "status", "healthy", "duration_ms", "phases")},
    ]
    unhealthy = [item for item in (telemetry.get("unhealthy") or []) if isinstance(item, dict)]
    if entry["healthy"]:
        telemetry["last_healthy_at"] = entry["at"]
    else:
        unhealthy.append(entry)
        telemetry["unhealthy_total"] = _counter(telemetry.get("unhealthy_total")) + 1
    telemetry["unhealthy"] = unhealthy[-TICK_TELEMETRY_UNHEALTHY_KEPT:]
    telemetry.setdefault("unhealthy_total", _counter(telemetry.get("unhealthy_total")))
    _record_incident(telemetry, entry)
    payload["tick_telemetry"] = telemetry
    return telemetry


def _record_incident(telemetry: dict[str, Any], entry: dict[str, Any]) -> None:
    """Fold one tick into the open incident, closing it when the tick is healthy again.

    An incident is one continuous run of unhealthy ticks: the tick that finds none open opens one and
    bumps `incident_total`, every unhealthy tick after it extends that record, and the first healthy
    tick closes it into `recovery` and bumps `recovery_total`. Both counters are monotonic within a
    generation, because a reader dedupes on them and a counter that can go down would let a later
    event consume an earlier one the reader has not seen.

    The tick that opened the incident is kept whole under `opened`, so its cause survives into the
    recovery record.
    """
    incident = telemetry.get("incident")
    incident = dict(incident) if isinstance(incident, dict) else None
    if entry["healthy"]:
        if incident:
            telemetry["recovery"] = {
                **incident,
                "recovered_seq": entry["seq"],
                "recovered_at": entry["at"],
                "recovered_status": entry["status"],
            }
            telemetry["recovery_total"] = _counter(telemetry.get("recovery_total")) + 1
            incident = None
    elif incident:
        incident["unhealthy_ticks"] = _counter(incident.get("unhealthy_ticks")) + 1
        incident["last_seq"] = entry["seq"]
        incident["last_at"] = entry["at"]
    else:
        incident = {
            "id": uuid.uuid4().hex,
            "opened_seq": entry["seq"],
            "opened_at": entry["at"],
            "last_seq": entry["seq"],
            "last_at": entry["at"],
            "unhealthy_ticks": 1,
            "opened": entry,
        }
        telemetry["incident_total"] = _counter(telemetry.get("incident_total")) + 1
    telemetry["incident"] = incident
    telemetry.setdefault("recovery", None)
    telemetry.setdefault("incident_total", _counter(telemetry.get("incident_total")))
    telemetry.setdefault("recovery_total", _counter(telemetry.get("recovery_total")))


def _record_failed_tick(runtime: Any, exc: BaseException) -> None:
    """Record a tick that died on an exception instead of returning a result.

    The tick that raises never reaches its own save, and a board outage raises on the very first
    read, so without this the pipeline keeps reporting the last healthy tick for the whole freshness
    window while nothing is moving. Written as a terminal unhealthy tick, in the same shape as a
    degraded one.

    The state is re-read rather than reused, because the payload the failed tick was mutating is half
    applied. Recording is best effort: the tick's own exception is the one that must reach the caller.
    """
    try:
        payload = runtime.production_state.load()
        record_tick_telemetry(
            payload,
            {
                "status": "failed",
                "step": "production-tick",
                "reason": f"tick raised {type(exc).__name__}",
                "errors": [
                    {
                        "ref": "",
                        # TaskError carries the backend's own code (backend_unavailable and friends), which
                        # is the one thing that tells an operator a board outage from a product bug.
                        "code": str(getattr(exc, "code", "") or "") or "unexpected_error",
                        "message": type(exc).__name__,
                    }
                ],
            },
        )
        runtime.production_state.save(payload)
    except Exception:  # noqa: BLE001 - one step's failure is recorded, never ends the tick
        return


class ProductionState:
    def __init__(self, data_dir: Path) -> None:
        self.root = data_dir / "dispatcher"
        self.path = self.root / "production-state.json"
        self.tick_lock = self.root / "production-tick.lock"
        self.run_lock = self.root / "production-run.lock"

    def load(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            payload = {"version": 1, "phase": "new"}
        except (OSError, ValueError, UnicodeError):
            payload = {"version": 1, "phase": "unavailable"}
        if not isinstance(payload, dict):
            payload = {"version": 1, "phase": "unavailable"}
        payload.setdefault("version", 1)
        payload.setdefault("mode", "production")
        payload.setdefault("phase", "new")
        return payload

    def save(self, payload: dict[str, Any]) -> None:
        write_json(self.path, payload)

    def records(self, payload: dict[str, Any]) -> dict[str, DispatcherRecord]:
        raw = payload.get("records") or {}
        if not isinstance(raw, dict):
            return {}
        return {
            str(ref): DispatcherRecord.from_json(record)
            for ref, record in raw.items()
            if isinstance(record, dict)
        }

    def put_records(self, payload: dict[str, Any], records: dict[str, DispatcherRecord]) -> None:
        payload["records"] = {ref: record.to_json() for ref, record in sorted(records.items())}


def production_observe(runtime: Any) -> dict[str, Any]:
    payload = runtime.production_state.load()
    return {
        "status": "ok",
        "step": "production-observe",
        "phase": payload.get("phase", "new"),
        "owner": payload.get("owner", ""),
        "pause": runtime.pause.summary(),
        "records": list((payload.get("records") or {}).keys()),
        "post_merge_watches": sorted(
            (payload.get(WATCHES_KEY) or {}).keys() if isinstance(payload.get(WATCHES_KEY), dict) else ()
        ),
        "e2e_after_merge": after_merge_snapshot(payload),
        "observers": observer_snapshot(payload),
        "resource_health": runtime.head_health.snapshot(),
        "divergences": list(payload.get("controlled_divergences") or []),
        "open_divergences": [
            divergence
            for divergence in (payload.get("controlled_divergences") or [])
            if isinstance(divergence, dict) and divergence_is_open(divergence)
        ],
        "checkpoint": checkpoint_snapshot(
            runtime.catalog.instance_dir,
            write_state=payload.get("checkpoint"),
            push_state=payload.get("checkpoint_push"),
            data_dir=runtime.data_dir,
        ),
    }


@serialized
def production_tick(runtime: Any) -> dict[str, Any]:
    with tick_clock(), try_file_lock(runtime.production_state.tick_lock) as acquired:
        if not acquired:
            return {
                "status": "blocked",
                "step": "production-tick",
                "reason": "production dispatcher singleton lock is held",
            }
        pause = runtime.pause.summary()
        auto_resume: dict[str, Any] | None = None
        if pause.get("mode") == "freeze":
            auto_resume = auto_resume_expired_freeze(runtime, source="tick")
            if auto_resume is not None and auto_resume.get("resumed"):
                pause = runtime.pause.summary()
        if pause.get("mode") == "freeze":
            return _frozen_tick(runtime, pause, auto_resume)
        payload = runtime.production_state.load()
        guard = _production_mutation_guard(runtime, payload)
        if guard is not None:
            return guard

        # After this guard every exit, including raises, records durable tick health.
        try:
            return _production_tick_body(runtime, payload, pause, auto_resume)
        except Exception as exc:
            _record_failed_tick(runtime, exc)
            raise


def _committing_records(runtime: Any, payload: dict[str, Any], records: dict[str, DispatcherRecord]):
    """Lend the host a flush of this tick's records, for the span the tick holds them.

    A head lifecycle transition has to be durable *before* the host call it describes, and the host
    is the one object that has the record in hand and not the file it lives in.
    """
    return runtime.host.committing(lambda: runtime.save_records(payload, records))


def _production_tick_body(
    runtime: Any,
    payload: dict[str, Any],
    pause: dict[str, Any],
    auto_resume: dict[str, Any] | None,
) -> dict[str, Any]:
    """The tick proper, from the first board read to the durable record of how it ended."""
    records = runtime.production_state.records(payload)
    with _committing_records(runtime, payload, records):
        return _production_tick_work(runtime, payload, records, pause, auto_resume)


def _production_tick_work(
    runtime: Any,
    payload: dict[str, Any],
    records: dict[str, DispatcherRecord],
    pause: dict[str, Any],
    auto_resume: dict[str, Any] | None,
) -> dict[str, Any]:
    """Everything the tick does with the records it has loaded."""
    payload.update(
        {
            "version": 1,
            "mode": "production",
            "phase": "production",
            "owner": runtime.owner,
        }
    )
    payload.setdefault("owner_acquired_at", now_rfc3339())
    payload["last_tick_started_at"] = now_rfc3339()

    # Nothing in this tick precedes the usage obligations already staged in the audit. Each one is
    # a phase that finished and was accounted for but whose account was never appended, and the card
    # it belongs to may be Blocked or Done by now — outside `ACTIVE_STATES`, outside the records,
    # outside everything below. Publishing them from the pending set is the only pass that reaches
    # those, and it runs before the fence, the cycle, reconciliation and any claim, because all of
    # them read a journal these records belong in.
    usage_outcomes = attempt_accounting.publish_pending_attempt_usage(runtime)
    # Outcome recovery is journal-only and reports its own degradation.  It
    # cannot delay the fence or any lifecycle work below.
    outcome_outcomes = attempt_accounting.publish_pending_attempt_outcomes(runtime)
    cleanup_outcomes = []
    if isinstance(runtime.host, CommandHostRuntime) and runtime.host.mode == "real":
        with tick_phase("cleanup"):
            # A few intents per tick: each replay rereads and rewrites the whole journal under the
            # tick's lock, and `replay_cursor` carries the rest to later ticks.
            cleanup_outcomes = [{"step": "owned-cleanup", "ref": item["task"]["ref"],
                                 "status": item["status"], "reason": item["reason"]}
                                for item in runtime.cleanup.replay(limit=5)]

    observer_errors: list[dict[str, str]] = []
    # Fence unhealthy sprint observers before advancing any reserved cards.
    try:
        with tick_phase("reconcile"):
            fence = observer_fence(runtime, payload, pause_mode=str(pause.get("mode") or ""))
    except Exception as exc:  # noqa: BLE001 - one step's failure is recorded, never ends the tick
        # An unfinished fence authorizes no downstream work.
        return _fence_failed_tick(runtime, payload, exc, usage_outcomes + outcome_outcomes)
    fence_outcomes = list(fence.get("outcomes") or [])

    cycle = _production_tasks(runtime, set(ACTIVE_STATES))
    active_tasks = [task for task in cycle if not fenced_task(fence, task)]
    active_refs = {str(task.get("ref") or "") for task in active_tasks}
    # Preserve fenced records from orphan reconciliation.
    fenced_refs = set(fence.get("refs") or ()) | {
        str(task.get("ref") or "") for task in cycle if fenced_task(fence, task)
    }
    with tick_phase("reconcile"):
        reconcile_outcomes = _reconcile_production(
            runtime, records, payload, active_refs, fenced_refs=fenced_refs, fence=fence
        )
        # Distinct from `last_tick_started_at`/`last_tick_finished_at`, which existed before
        # reconciliation did: those are stamped by every tick regardless of code version, so a
        # pre-deployment host with an old dispatcher would otherwise read as "reconciliation ran"
        # on the strength of a field that predates the reconciliation pass itself.
        payload["last_reconciled_at"] = now_rfc3339()
        outcomes, errors, blocked_scopes = _advance_active(runtime, records, payload, active_tasks)
    outcomes = cleanup_outcomes + usage_outcomes + outcome_outcomes + fence_outcomes + reconcile_outcomes + outcomes
    # After the releases of this tick, before the observers: a merge whose base has no CI resolves
    # `absent` in the tick that merged it, and a result written here is delivered below.
    with tick_phase("after-merge"):
        try:
            outcomes += reconcile_post_merge_watches(runtime, payload, records)
        except Exception as exc:  # noqa: BLE001 - a watch that cannot be read must not stop the tick
            errors.append(_unexpected_error("", exc))
        # Right after the watches that queue them: each project's after-merge e2e run (secretary-1807).
        try:
            outcomes += reconcile_after_merge(runtime, payload, records)
        except Exception as exc:  # noqa: BLE001 - an after-merge queue that cannot advance must not stop the tick
            errors.append(_unexpected_error("", exc))
    try:
        outcomes += _reconcile_sprint_budget(runtime)
    except Exception as exc:  # noqa: BLE001 - one step's failure is recorded, never ends the tick
        errors.append(_unexpected_error("", exc))
    # Reconcile after budget accounting so hard stops prevent replacement launches.
    with tick_phase("launches"):
        try:
            outcomes += reconcile_observers(runtime, payload, pause_mode=str(pause.get("mode") or ""))
        except Exception as exc:  # noqa: BLE001 - one step's failure is recorded, never ends the tick
            observer_errors.append(_unexpected_error("", exc))
        errors = observer_errors + errors
        claims_allowed = pause.get("mode") != "drain"
        if claims_allowed:
            try:
                ready_outcome = _production_claim_ready(
                    runtime, records, payload, fence=fence, blocked_scopes=blocked_scopes
                )
            except Exception as exc:  # noqa: BLE001 - one step's failure is recorded, never ends the tick
                errors.append(_unexpected_error("", exc))
            else:
                if ready_outcome is not None:
                    outcomes.append(ready_outcome)
    # Last, after every move this tick made (a claim can Block a decision/operation card): every
    # return the origin-return outbox holds undelivered goes to the PO session that cut its card.
    try:
        outcomes += reconcile_origin_returns(runtime)
    except Exception as exc:  # noqa: BLE001 - a return that cannot be read must not stop the tick
        errors.append(_unexpected_error("", exc))

    runtime.production_state.put_records(payload, records)
    with tick_phase("checkpoint"):
        checkpoint, push = _coordinate_checkpoint(runtime, payload)
    payload["last_tick_finished_at"] = now_rfc3339()
    # Caught degraded actions and checkpoint failures degrade the terminal tick.
    checkpoint_blocked = bool(checkpoint and checkpoint.get("status") == "blocked")
    if checkpoint_blocked:
        outcomes.append(_checkpoint_degradation(checkpoint))
    result = {
        "status": "ok"
        if not errors and not degraded_actions(outcomes) and not checkpoint_blocked
        else "degraded",
        "step": "production-tick",
        "owner": runtime.owner,
        "actions": outcomes,
        "errors": errors,
    }
    if pause.get("paused"):
        result["pause"] = pause
    if auto_resume is not None:
        result["auto_resume"] = auto_resume
    if checkpoint is not None:
        result["checkpoint"] = checkpoint
    if push is not None:
        result["checkpoint_push"] = push
    # Persist telemetry from the result returned to the caller.
    record_tick_telemetry(payload, result)
    runtime.production_state.save(payload)
    return result


def _frozen_tick(runtime: Any, pause: dict[str, Any], auto_resume: dict[str, Any] | None) -> dict[str, Any]:
    """A frozen tick moves no card and retains the normal checkpoint cadence."""
    result: dict[str, Any] = {
        "status": "skipped",
        "step": "production-tick",
        "reason": "pipeline is frozen by pause",
        "pause": pause,
    }
    if auto_resume is not None:
        result["auto_resume"] = auto_resume
    payload = runtime.production_state.load()
    guard = _production_mutation_guard(runtime, payload)
    if guard is not None:
        # No state to write the snapshot's own bookkeeping into, so the checkpoint is not attempted:
        # the freeze is reported with the guard's reason instead of a snapshot that cannot be recorded.
        result["durability"] = {
            "status": "skipped",
            "reason": str(guard.get("reason") or "production state is not writable"),
        }
        return result
    try:
        return _frozen_tick_body(runtime, payload, result)
    except Exception as exc:
        # Same rule as the working tick: past the guard, a raise still leaves a durable record,
        # or a freeze whose checkpoint machinery is broken would read as a healthy pipeline.
        _record_failed_tick(runtime, exc)
        raise


def _frozen_tick_body(runtime: Any, payload: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    # Reconciliation does not run while frozen, so this is the only place a stop the host refused
    # during the freeze is retried. Nothing else about an observer is touched here.
    try:
        observer_stops = retry_pending_observer_stops(runtime, payload)
    except Exception as exc:  # noqa: BLE001 - one step's failure is recorded, never ends the tick
        observer_stops = []
        # A freeze itself is healthy; a retry that raised inside it is not, and telemetry keys
        # health off the status. Say so in both places rather than reporting the frozen tick as a
        # clean one that happens to carry an error field nobody reads.
        result["observer_stop_error"] = _unexpected_error("", exc)
        result["errors"] = [result["observer_stop_error"]]
        result["status"] = "degraded"
    if observer_stops:
        result["observer_stops"] = observer_stops
        # The retried stops are this tick's action outcomes, so a stop the host refused again is
        # read like any other degraded action: it turns the terminal tick degraded and its reason
        # reaches the durable record. Before that the row was classified by nobody and a freeze
        # sitting on a head it could not take down recorded itself healthy, leaving health OK and
        # the steward without a signal (secretary-833 review, round 4).
        result["actions"] = observer_stops
        if degraded_actions(observer_stops):
            result["status"] = "degraded"
    with tick_phase("checkpoint"):
        checkpoint, push = _coordinate_checkpoint(runtime, payload)
    if checkpoint is not None:
        result["checkpoint"] = checkpoint
        if checkpoint.get("status") == "blocked":
            result["status"] = "degraded"
            actions = list(result.get("actions") or ())
            actions.append(_checkpoint_degradation(checkpoint))
            result["actions"] = actions
    if push is not None:
        result["checkpoint_push"] = push
    payload["last_frozen_tick_at"] = now_rfc3339()
    # A freeze is a deliberate stop, so this tick is recorded as a healthy terminal one — and it
    # is saved even when nothing else about this tick changed the state, or a long freeze would
    # age the last healthy tick out and report the pipeline as dead instead of frozen.
    record_tick_telemetry(payload, result)
    runtime.production_state.save(payload)
    return result


def _checkpoint_degradation(checkpoint: dict[str, Any]) -> dict[str, str]:
    """Expose a durability gate failure as an ordinary degraded action."""
    return {
        "status": "degraded",
        "step": "checkpoint",
        "action": "blocked",
        "reason": str(checkpoint.get("reason") or "checkpoint gate blocked"),
    }


def _coordinate_checkpoint(runtime: Any, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Prepare one current recovery snapshot when its local or remote window is due.

    This is the only periodic caller of ``CheckpointWriter``.  It uses the
    pusher's clock and due semantics so the five-minute preparation and the
    thirty-minute remote window do not stack.  A due push receives a snapshot
    prepared in this invocation or is explicitly withheld.
    """
    writer = getattr(runtime, "checkpoint", None)
    pusher = getattr(runtime, "checkpoint_push", None)
    previous = payload.get("checkpoint")
    write_state = dict(previous) if isinstance(previous, dict) else {}
    previous_push = payload.get("checkpoint_push")
    push_state = dict(previous_push) if isinstance(previous_push, dict) else {}
    now = _checkpoint_now(pusher)
    push_due = _checkpoint_push_due(pusher, push_state, now)
    checkpoint_due = _checkpoint_due(write_state, now)

    # A few constrained runtime tests deliberately have no checkpoint writer.
    # Preserve their former benign push-only behavior rather than fabricating a
    # blocked periodic checkpoint. Production constructs both dependencies.
    if writer is None:
        if pusher is None or not push_due:
            return None, None
        push = _push_checkpoint(runtime, push_state, now)
        payload["checkpoint_push"] = push
        return None, push
    if pusher is None and not checkpoint_due:
        checkpoint = _checkpoint_skipped(write_state, now)
        payload["checkpoint"] = checkpoint
        return checkpoint, None

    # A regular remote deadline needs a current preparation before delivery.
    # A remote already known to have diverged is deliberately rechecked by its
    # pusher on every tick, but cannot make the expensive board/run projection
    # run more often than its own five-minute deadline.
    preparation_due = checkpoint_due or _push_forces_preparation(push_due, push_state)
    if not preparation_due:
        checkpoint = _checkpoint_skipped(write_state, now)
        payload["checkpoint"] = checkpoint
        if not push_due or pusher is None:
            return checkpoint, None
        push = _push_checkpoint(runtime, push_state, now)
        payload["checkpoint_push"] = push
        return checkpoint, push

    checkpoint = _write_checkpoint(runtime, write_state, now)
    payload["checkpoint"] = checkpoint
    prepared = checkpoint.get("status") in {"committed", "unchanged"}
    if not push_due:
        return checkpoint, None
    if not prepared:
        push = _push_withheld_for_checkpoint(push_state, checkpoint, now)
        payload["checkpoint_push"] = push
        return checkpoint, push
    if pusher is None:
        return checkpoint, None
    push = _push_checkpoint(runtime, push_state, now)
    # This one-shot pusher retry means only "the next delivery needs a fresh
    # preparation". The preparation above satisfied that condition even when
    # the remote still fails or remains diverged, so it cannot pin the pusher
    # and this coordinator into a per-minute retry loop.
    if push_state.get("retry_pending"):
        push.pop("retry_pending", None)
    payload["checkpoint_push"] = push
    return checkpoint, push


def _checkpoint_now(pusher: Any) -> float:
    """Use the pusher's established controllable clock for both windows."""
    clock = getattr(pusher, "_clock", None)
    if callable(clock):
        try:
            return float(clock())
        except (TypeError, ValueError):
            pass
    return time.time()


def _checkpoint_due(state: dict[str, Any], now: float) -> bool:
    """Whether a fresh board/run preparation is due, fail-closed on old state."""
    if state.get("retry_pending"):
        return True
    successful = _checkpoint_success_epoch(state)
    # Existing payloads did not carry cadence metadata.  Their first upgraded
    # tick must make one fresh cut, rather than treating an old result as one.
    if successful is None:
        return True
    # A rollback is due now.  Otherwise clamp is not enough: a future marker
    # would park recovery forever.
    return now < successful or now - successful >= CHECKPOINT_INTERVAL_SECONDS


def _checkpoint_push_due(pusher: Any, state: dict[str, Any], now: float) -> bool:
    if pusher is None:
        return False
    due = getattr(pusher, "due", None)
    if not callable(due):
        return False
    try:
        return bool(due(state, now=now))
    except Exception:  # noqa: BLE001 - one step's failure is recorded, never ends the tick
        # A pusher that cannot answer its own public window contract gets a
        # fresh preparation and then records its own delivery failure. It must
        # not turn a failed preflight into permission to send an old snapshot.
        return True


def _push_forces_preparation(push_due: bool, state: dict[str, Any]) -> bool:
    """Whether this due push is an ordinary deadline, not a sticky recheck."""
    return push_due and not bool(state.get("remote_diverged"))


def _checkpoint_skipped(state: dict[str, Any], now: float) -> dict[str, Any]:
    """Record an inexpensive not-yet-due decision without reclassifying success."""
    started = time.perf_counter()
    result = dict(state)
    successful = _checkpoint_success_epoch(result)
    result.update(
        {
            "status": "skipped",
            "reason": "not due",
            # This outcome inherits the previous run's fields, so its duration is restated rather
            # than left behind: a skip that reported the last committed run's milliseconds would
            # read as an expensive checkpoint nobody ran.
            "duration_ms": round((time.perf_counter() - started) * 1000.0, 3),
            "at": _checkpoint_rfc3339(now),
            "skip_epoch": now,
            "skip_at": _checkpoint_rfc3339(now),
            "next_due_epoch": successful + CHECKPOINT_INTERVAL_SECONDS if successful is not None else now,
            "next_due_at": _checkpoint_rfc3339(
                successful + CHECKPOINT_INTERVAL_SECONDS if successful is not None else now
            ),
        }
    )
    return result


def _write_checkpoint(runtime: Any, state: dict[str, Any], now: float) -> dict[str, Any]:
    """Prepare a fresh checkpoint and retain success/failure history separately."""
    started = time.perf_counter()
    writer = getattr(runtime, "checkpoint", None)
    if writer is None:
        raw = {"status": "blocked", "reason": "checkpoint writer is unavailable"}
    else:
        try:
            raw = writer.write().to_json()
        except Exception as exc:  # noqa: BLE001 - one step's failure is recorded, never ends the tick
            raw = {"status": "blocked", "reason": f"{type(exc).__name__}: {exc}"}
    result = dict(state)
    status = str(raw.get("status") or "blocked")
    reason = str(raw.get("reason") or "")
    result.update(raw)
    result.update(
        {
            "status": status,
            "reason": reason,
            # The writer times its own run and reports it in `raw`; a run that never reached the
            # writer, or one that died before it could return a result, is timed from out here so
            # that every outcome this coordinator records carries a duration of its own.
            "duration_ms": _number(raw.get("duration_ms"))
            or round((time.perf_counter() - started) * 1000.0, 3),
            "at": _checkpoint_rfc3339(now),
            "attempted_epoch": now,
            "attempted_at": _checkpoint_rfc3339(now),
        }
    )
    if status in {"committed", "unchanged"}:
        result.update(
            {
                "last_success_epoch": now,
                "last_success_at": _checkpoint_rfc3339(now),
                "last_success_status": status,
                "last_success_commit": str(raw.get("commit") or ""),
                "next_due_epoch": now + CHECKPOINT_INTERVAL_SECONDS,
                "next_due_at": _checkpoint_rfc3339(now + CHECKPOINT_INTERVAL_SECONDS),
                "retry_pending": False,
            }
        )
        result.pop("last_failure_epoch", None)
        result.pop("last_failure_at", None)
        result.pop("last_failure_reason", None)
        result.pop("failing_since_epoch", None)
        result.pop("failing_since_at", None)
    else:
        # `last_failure_*` moves with every failed run; `failing_since_*` keeps the first failure
        # after the last success, so doctor can say since when the checkpoint has not published.
        since = _number(state.get("failing_since_epoch")) if state.get("last_failure_reason") else 0.0
        since = since or now
        result.update(
            {
                "last_failure_epoch": now,
                "last_failure_at": _checkpoint_rfc3339(now),
                "last_failure_reason": reason or "checkpoint preparation failed",
                "failing_since_epoch": since,
                "failing_since_at": _checkpoint_rfc3339(since),
                "retry_pending": True,
            }
        )
    return result


def _push_withheld_for_checkpoint(
    state: dict[str, Any], checkpoint: dict[str, Any], now: float
) -> dict[str, Any]:
    reason = str(checkpoint.get("last_failure_reason") or checkpoint.get("reason") or "preparation failed")
    result = dict(state)
    result.update(
        {
            "status": "skipped",
            "reason": f"fresh checkpoint preparation failed; remote push withheld: {reason}",
            "attempted_epoch": now,
            "attempted_at": _checkpoint_rfc3339(now),
            "retry_pending": True,
        }
    )
    return result


def _checkpoint_rfc3339(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _checkpoint_success_epoch(state: dict[str, Any]) -> float | None:
    """A valid zero timestamp is a real successful preparation, not old state."""
    value = state.get("last_success_epoch")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _push_checkpoint(runtime: Any, state: dict[str, Any], now: float) -> dict[str, Any]:
    """Run a due remote window only after this tick's preparation succeeded."""
    pusher = getattr(runtime, "checkpoint_push", None)
    assert pusher is not None
    try:
        result = pusher.push(state, now=now)
    except Exception as exc:  # noqa: BLE001 - one step's failure is recorded, never ends the tick
        return _failed_push(state, exc, now)
    result = dict(result)
    # ``CheckpointPusher`` records this itself. Keep a pre-cadence compatible
    # pusher from being retried every minute merely because it omitted the
    # durable window marker from an otherwise successful result.
    if _number(result.get("attempted_epoch")) <= 0:
        result["attempted_epoch"] = now
        result["attempted_at"] = _checkpoint_rfc3339(now)
    return result


def _failed_push(state: dict[str, Any], exc: Exception, now: float) -> dict[str, Any]:
    result = dict(state)
    result.update(
        {
            "status": "failed",
            "reason": f"{type(exc).__name__}: {exc}",
            "attempted_epoch": now,
            "attempted_at": _checkpoint_rfc3339(now),
            "failures": int(result.get("failures") or 0) + 1,
        }
    )
    return result


class ProbeAbort(Exception):
    """A dry tick reached the point where the real tick would have written."""

    def __init__(self, operation: str, detail: dict[str, Any]) -> None:
        super().__init__(operation)
        self.operation = operation
        self.detail = detail


class _ProbeWriter:
    """Stands in for the board writer. Every write aborts the task's probe."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def move(self, **kwargs: Any) -> None:
        raise ProbeAbort("move", {"ref": kwargs.get("reference", ""), "to": kwargs.get("target", "")})

    def claim(self, *args: Any, **kwargs: Any) -> None:
        raise ProbeAbort("claim", {"ref": kwargs.get("reference", "") or (args[0] if args else "")})

    def comment(self, *args: Any, **kwargs: Any) -> None:
        raise ProbeAbort("comment", {"ref": kwargs.get("reference", "") or (args[0] if args else "")})

    def routing(self, *args: Any, **kwargs: Any) -> None:
        raise ProbeAbort("routing", {"ref": kwargs.get("reference", "") or (args[0] if args else "")})

    def record_wait_state(self, *args: Any, **kwargs: Any) -> None:
        raise ProbeAbort("wait-state", {"ref": kwargs.get("reference", "") or (args[0] if args else "")})

    def record_po_return(self, *args: Any, **kwargs: Any) -> None:
        raise ProbeAbort("po-return", {"ref": kwargs.get("reference", "") or (args[0] if args else "")})

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _ProbeHost:
    """Stands in for the command host. Path arithmetic passes; effects abort.

    ``gate_check`` is listed as an effect even though it mostly reads: it runs the project's setup
    and test commands, far too expensive for a health probe a timer may call every minute.
    """

    EFFECTS = (
        "prepare_worker",
        "prepare_observer",
        "stop_observer",
        "restart_worker",
        # Settling an unresolved launch intent ends heads (a worker frozen for its adopted reviewer,
        # a workspace stopped because its launch left nothing running). A probe that walked those
        # paths for real would kill live heads while reporting what the tick "would" do.
        "stop_workspace",
        "stop_review",
        "freeze_worker",
        "retain_worker",
        "resume_worker",
        # Typing into a live head is an effect even though it stops nothing: a probe that ran it
        # would interrupt a working worker with a prompt about a round the probe is only modelling.
        "prompt_worker_report",
        # The same for a mid-round comment pointer, which also rewrites the worker's TASK.md.
        "deliver_worker_comments",
        "verify_worker_result",
        "gate_check",
        "rerun_failed_ci",
        "complete_green",
        "teardown",
        "stop",
    )

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def restore_workspace(self, task: dict[str, Any], worker: str) -> str:
        return self._inner.restore_workspace(task, worker)

    def __getattr__(self, name: str) -> Any:
        if name in self.EFFECTS:

            def effect(*args: Any, **kwargs: Any) -> Any:
                raise ProbeAbort(name, {})

            return effect
        return getattr(self._inner, name)


class _ProbePo:
    """Stands in for the PO channel. Reading the PO store passes; a resolve, a create or a submit aborts."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def sprint_session(self, **kwargs: Any) -> Any:
        raise ProbeAbort("po-sprint-session", {"sprint": kwargs.get("sprint_ref", "")})

    def create_session(self, **kwargs: Any) -> Any:
        raise ProbeAbort("po-create-session", {"request": kwargs.get("request_id", "")})

    def submit(self, **kwargs: Any) -> Any:
        raise ProbeAbort("po-submit", {"session": kwargs.get("session_id", "")})

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _ProbeState:
    """Reads through to the real state; a save is an abort, never a write."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def save(self, payload: dict[str, Any]) -> None:
        raise ProbeAbort("save-state", {})

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _ProbeCleanup:
    """The durable owner is an effect even after its active record disappeared."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        if name in {"cleanup", "cleanup_observer", "remember", "replay", "replay_one", "replay_targets"}:
            def effect(*args: Any, **kwargs: Any) -> Any:
                raise ProbeAbort("owned-cleanup", {})
            return effect
        return getattr(self._inner, name)


def _probe_runtime(runtime: Any) -> Any:
    """The real runtime with only its writers swapped out.

    A wrapper object would not work: the tick's own methods are bound to the real runtime, so
    ``self.writer`` inside them would still reach the live board. A shallow copy shares every
    collaborator by reference and rebinds those methods to an object that cannot write.
    """
    probe = copy.copy(runtime)
    probe.writer = _ProbeWriter(runtime.writer)
    probe.host = _ProbeHost(runtime.host)
    probe.production_state = _ProbeState(runtime.production_state)
    probe.po = _ProbePo(runtime.po)
    if hasattr(runtime, "cleanup"):
        probe.cleanup = _ProbeCleanup(runtime.cleanup)
    return probe


@serialized
def production_probe(runtime: Any) -> dict[str, Any]:
    """Run a real tick with every write replaced by an abort.

    This is the health gate, so it fails for the same reasons the real tick fails: same singleton
    lock, same mutation guards, same task states, same ``_tick_task`` decision. The only difference
    is that the first write per task raises instead of landing.
    """
    with try_file_lock(runtime.production_state.tick_lock) as acquired:
        if not acquired:
            return {
                "status": "blocked",
                "step": "production-probe",
                "reason": "production dispatcher singleton lock is held",
            }
        pause = runtime.pause.summary()
        if pause.get("mode") == "freeze":
            # A frozen pipeline is stopped on purpose, so the health gate reports it as such
            # instead of as a dispatcher that cannot move cards.
            return {
                "status": "ok",
                "step": "production-probe",
                "owner": runtime.owner,
                "reason": "pipeline is frozen by pause",
                "pause": pause,
                "would": [],
                "errors": [],
            }
        payload = runtime.production_state.load()
        guard = _production_mutation_guard(runtime, payload)
        if guard is not None:
            guard["step"] = "production-probe"
            return guard

        probe = _probe_runtime(runtime)
        records = runtime.production_state.records(payload)
        would: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []

        active = _production_tasks(runtime, set(ACTIVE_STATES))
        for task in active:
            if is_steward_report(task):
                continue
            would.append(_probe_one(probe, task, dict(records), dict(payload)))

        ready = [task for task in _production_tasks(runtime, {"ready"}) if not is_steward_report(task)]
        # The claim path is the one a health gate most needs to exercise: it is
        # where capacity, predecessors and per-project concurrency are decided,
        # and none of that is visible from the active scan. The real tick skips
        # it when an active task blocks, but an aborted probe cannot know that a
        # task would have blocked, so this is always evaluated and reported as
        # its own entry rather than as a prediction of the next tick's one move.
        # Under a drain the tick would not reach the claim path at all, so probing it would report
        # a move the next tick is not going to make.
        if pause.get("mode") != "drain":
            would.append(_probe_ready(probe, dict(records), dict(payload)))
        for entry in would:
            if entry.get("code"):
                errors.append(
                    {"ref": entry["ref"], "code": entry["code"], "message": entry.get("message", "")}
                )

        return {
            "status": "ok" if not errors else "degraded",
            "step": "production-probe",
            "owner": runtime.owner,
            "active": [str(task.get("ref") or "") for task in active],
            "ready": [str(task.get("ref") or "") for task in ready],
            "pause": pause,
            "would": would,
            "errors": errors,
        }


def _probe_one(
    probe: Any,
    task: dict[str, Any],
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
) -> dict[str, Any]:
    ref = str(task.get("ref") or "")
    try:
        outcome = _production_tick_active(probe, task, records, payload)
    except ProbeAbort as abort:
        return {"ref": ref, "operation": abort.operation, "detail": abort.detail}
    except TaskError as exc:
        return {"ref": ref, "operation": "error", "code": exc.code, "message": exc.message}
    except Exception as exc:  # noqa: BLE001
        return {
            "ref": ref,
            "operation": "error",
            "code": "unexpected_error",
            "message": exc.__class__.__name__,
        }
    return {
        "ref": ref,
        "operation": "none",
        "status": outcome.get("status", ""),
        "step": outcome.get("step", ""),
    }


def _probe_ready(
    probe: Any,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
) -> dict[str, Any]:
    try:
        outcome = _production_claim_ready(probe, records, payload)
    except ProbeAbort as abort:
        return {"ref": abort.detail.get("ref", ""), "operation": abort.operation, "detail": abort.detail}
    except TaskError as exc:
        return {"ref": "", "operation": "error", "code": exc.code, "message": exc.message}
    except Exception as exc:  # noqa: BLE001
        return {
            "ref": "",
            "operation": "error",
            "code": "unexpected_error",
            "message": exc.__class__.__name__,
        }
    if outcome is None:
        return {"ref": "", "operation": "none", "step": "production-claim", "status": "idle"}
    return {
        "ref": str(outcome.get("ref") or ""),
        "operation": "none",
        "step": "production-claim",
        "status": outcome.get("status", ""),
    }


def production_run(
    runtime: Any,
    *,
    interval_seconds: float,
    max_interval_seconds: float,
    max_ticks: int | None = None,
) -> dict[str, Any]:
    interval_seconds = max(1.0, interval_seconds)
    max_interval_seconds = max(interval_seconds, max_interval_seconds)
    with try_file_lock(runtime.production_state.run_lock) as acquired:
        if not acquired:
            return {
                "status": "blocked",
                "step": "production-run",
                "reason": "production dispatcher run loop is already active",
            }
        ticks = 0
        failures = 0
        last: dict[str, Any] = {"status": "ok", "step": "production-run", "action": "start"}
        while max_ticks is None or ticks < max_ticks:
            try:
                last = runtime.production_tick()
                failures = 0 if last.get("status") == "ok" else failures + 1
            except Exception as exc:  # noqa: BLE001 - one step's failure is recorded, never ends the tick
                failures += 1
                last = {
                    "status": "degraded",
                    "step": "production-run",
                    "error": _unexpected_error("", exc),
                }
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                break
            delay = min(max_interval_seconds, interval_seconds * (2 ** min(failures, 5)))
            time.sleep(delay)
        return {"status": "ok", "step": "production-run", "ticks": ticks, "last": last}


def production_adopt_attempt_id(reference: str) -> str:
    return "production-adopt-" + request_token(reference)


def is_steward_report(task: dict[str, Any]) -> bool:
    return task.get("extensions", {}).get(EXTENSION_BAG, {}).get("steward_report") == "1"


def _advance_active(
    runtime: Any,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    active_tasks: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, str]], dict[str, set[str]]]:
    """Advance every active card, and report which scopes a blocked card closed for claims.

    The scopes are the blocked card's sprint and its project: a blocked card says nothing about the
    work of another sprint in another project, so the claim suppression it causes is keyed by those
    two rather than installation-wide.
    """
    outcomes: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    blocked_scopes: dict[str, set[str]] = {"sprints": set(), "projects": set()}
    for task in active_tasks:
        if is_steward_report(task):
            continue
        try:
            outcome = _production_tick_active(runtime, task, records, payload)
        except TaskError as exc:
            errors.append({"ref": str(task.get("ref") or ""), "code": exc.code, "message": exc.message})
            continue
        except Exception as exc:  # noqa: BLE001 - one step's failure is recorded, never ends the tick
            errors.append(_unexpected_error(str(task.get("ref") or ""), exc))
            continue
        if outcome.get("status") == "blocked":
            sprint_ref = str(task.get("sprint") or "")
            project = str(task.get("project") or "")
            if sprint_ref:
                blocked_scopes["sprints"].add(sprint_ref)
            if project:
                blocked_scopes["projects"].add(project)
        outcomes.append(outcome)
    return outcomes, errors, blocked_scopes


def _fence_failed_tick(
    runtime: Any,
    payload: dict[str, Any],
    exc: Exception,
    usage_outcomes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """End the tick without touching a card, and leave a durable record of why.

    The state is still saved: the fence may have opened or cleared fences and refreshed its snapshot
    before it raised, and those are what the next tick reads. The usage obligations settled before
    the fence ran are reported too: they were published, and a tick that reported nothing about them
    would read as if they were still owed.
    """
    result = {
        "status": "critical",
        "step": "production-tick",
        "owner": runtime.owner,
        "action": "observer-fence-unavailable",
        "reason": (
            "the observer fence could not be evaluated, so no card was advanced, reconciled or "
            f"claimed this tick: {type(exc).__name__}: {exc}"
        ),
        "actions": list(usage_outcomes or []),
        "errors": [_unexpected_error("", exc)],
    }
    payload["last_tick_finished_at"] = now_rfc3339()
    record_tick_telemetry(payload, result)
    try:
        runtime.production_state.save(payload)
    except Exception:  # noqa: BLE001 - one step's failure is recorded, never ends the tick
        # Reporting the refusal matters more than recording it: the cards are already untouched.
        result["state_save"] = "failed"
    return result


def _reconcile_production(
    runtime: Any,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    active_refs: set[str],
    fenced_refs: set[str] | None = None,
    fence: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Reconcile records and controlled divergences against the current board.

    `_advance_active` only looks at cards the board currently reports as in_progress/validate, so a
    record whose card left that cycle from outside the dispatcher is invisible to it forever. A record
    owns every head launched for the card, so reconciliation must settle those heads before it can
    remove the record.
    """
    outcomes: list[dict[str, Any]] = []
    state_cache: dict[str, str | None] = {}
    # A fenced sprint's records are left exactly as they are. Reconciliation stops heads and
    # removes records, and both are mutations of work the fence exists to hold still.
    fenced_refs = fenced_refs or set()
    fence = fence or {}
    task_cache: dict[str, dict[str, Any] | None] = {}

    def card(ref: str) -> dict[str, Any] | None:
        if ref not in task_cache:
            task_cache[ref] = _current_card(runtime, ref)
        return task_cache[ref]

    def card_state(ref: str) -> str | None:
        if ref not in state_cache:
            task = card(ref)
            state_cache[ref] = None if task is None else str(task.get("state") or "unknown")
        return state_cache[ref]

    def fenced(ref: str) -> bool:
        """Whether this record's card belongs to a fenced sprint, decided a second way.

        `fenced_refs` is the fence's own inventory. This asks the card the tick has just read, by its
        sprint link and its project, so a card missing from that inventory is still not settled while its
        sprint is fenced.
        """
        if ref in fenced_refs:
            return True
        task = card(ref)
        return task is not None and fenced_task(fence, task)

    for ref in sorted(ref for ref in records if ref not in active_refs):
        if fenced(ref):
            continue
        # `active_refs` is a snapshot taken before this pass; the board can move the card back
        # into the active cycle between that snapshot and this loop (a PO race). The snapshot is
        # only ever a reason to look, never proof of anything: only the live state fetched right
        # here, immediately before removal, decides whether the record is actually orphaned.
        state = card_state(ref)
        if state is None or state in ACTIVE_STATES:
            continue
        record = records[ref]
        if record.activation_recovery is not None:
            # A terminal source can retain the record after its move committed but the tick died
            # before the final state save. Finish the exact board requests before orphan removal.
            task = card(ref)
            assert task is not None
            outcomes.append(runtime._tick_task(task, records, payload, record.attempt_id))
            continue
        if (
            state == "blocked"
            and record.gate_state == "green"
            and record.state == "review_starting"
            and record.review_infra_failures > 0
        ):
            # The infrastructure ceiling deliberately hands this exact candidate to an operator.
            # Stopping its retained worker and dropping its receipt one tick later would make the
            # instruction to relaunch only the reviewer impossible to follow.
            continue
        intent_action = str(launch_intent(record).get("action") or "")
        if (state != "ready" and isinstance(runtime.host, CommandHostRuntime)
                and runtime.host.mode == "real"):
            closed = card(ref)
            if closed is None:
                continue
            cleanup = runtime.cleanup.cleanup(closed, record, "inactive")
            outcomes.append({"step": "owned-cleanup", "ref": ref, "status": cleanup["status"],
                             "reason": cleanup["reason"], "progress": cleanup["progress"]})
            if not cleanup["progress"].get("heads_stopped"):
                continue
            forget_role_head(record, WORKER_ROLE)
            forget_role_head(record, REVIEW_ROLE)
            record.workspace_settled = True
            stopped = None
        else:
            stopped = _stop_record_heads(runtime, record, ref, state)
        if stopped is not None:
            outcomes.append(stopped)
            continue
        # Keep the settled record for `_claim`: its workspace is the checkout the next attempt
        # must reuse, while every head and unresolved intent it used to own is now confirmed gone.
        if state == "ready":
            continue
        records.pop(ref)
        closed = card(ref)
        outcomes.append(
            {
                "status": "ok",
                "step": "production-reconcile",
                "ref": ref,
                "action": "record-removed",
                "reason": "card left the active dispatcher cycle",
                "record_state": record.state,
                "card_state": state,
                **({"stopped_launch": intent_action} if intent_action else {}),
                # A decision/operation card closes here once the PO completed it (or anyone moved it).
                **(
                    {"po_completion": completion_state(closed)}
                    if record.po_submission and closed is not None
                    else {}
                ),
            }
        )

    divergences = payload.get("controlled_divergences")
    open_refs = (
        {
            str(divergence.get("pilot_ref") or "")
            for divergence in divergences
            if isinstance(divergence, dict) and divergence_is_open(divergence)
        }
        if isinstance(divergences, list)
        else set()
    )
    for ref in sorted(open_refs - active_refs):
        if fenced(ref):
            continue
        state = card_state(ref)
        if state is None or state in ACTIVE_STATES:
            continue
        closed_ids = _close_divergences_for_ref(payload, ref, state)
        if closed_ids:
            outcomes.append(
                {
                    "status": "ok",
                    "step": "production-reconcile",
                    "ref": ref,
                    "action": "divergences-closed",
                    "reason": "card left the active dispatcher cycle",
                    "card_state": state,
                    "divergence_ids": closed_ids,
                }
            )
    return outcomes


def _stop_record_heads(
    runtime: Any,
    record: DispatcherRecord,
    ref: str,
    card_state: str,
) -> dict[str, Any] | None:
    """Stop every head owned by one record, or preserve the record and report the refusal."""
    intent = launch_intent(record)
    if intent:
        failure = stop_launch_intent(runtime, record, intent, str(intent.get("role") or ""))
        if failure is not None:
            return {
                "status": "degraded",
                "step": "production-reconcile",
                "ref": ref,
                "action": "launch-intent-stop-unconfirmed",
                "reason": f"the head of an unresolved launch could not be stopped: {failure}",
                "record_state": record.state,
                "card_state": card_state,
            }
        # `stop_launch_intent` either settled the role through its saved identity or used the
        # legacy workspace fallback itself. Do not turn the former back into a workspace-wide
        # stop after it has just closed the one named head.
        record.workspace_settled = True
    if not record.needs_settling():
        return None
    try:
        # A recorded pane identity is narrower than the workspace. In particular a create-time
        # handle can alias to another head, so the host resolves a saved leaf to its current handle
        # before closing it. Keep the workspace-wide stop only for legacy records that never got
        # any role identity at all: there is no other safe way to settle an unnamed possible head.
        if record.owns_head(REVIEW_ROLE):
            runtime.host.stop_head(record, REVIEW_ROLE, STOPPED_BY_RECONCILIATION)
        if record.owns_head(WORKER_ROLE):
            runtime.host.stop_head(record, WORKER_ROLE, STOPPED_BY_RECONCILIATION)
        if not record.owns_head() and record.workspace and not record.workspace_settled:
            runtime.host.stop_workspace(record)
    except HostError as exc:
        return {
            "status": "degraded",
            "step": "production-reconcile",
            "ref": ref,
            "action": "head-stop-unconfirmed",
            "reason": f"the card left the active cycle, but its heads could not be stopped: {exc}",
            "record_state": record.state,
            "card_state": card_state,
        }
    forget_role_head(record, WORKER_ROLE)
    forget_role_head(record, REVIEW_ROLE)
    if record.workspace:
        record.workspace_settled = True
    return None


def _close_divergences_for_ref(payload: dict[str, Any], ref: str, card_state: str) -> list[str]:
    divergences = payload.get("controlled_divergences")
    if not isinstance(divergences, list):
        return []
    closed_ids: list[str] = []
    for divergence in divergences:
        if not isinstance(divergence, dict) or divergence.get("pilot_ref") != ref:
            continue
        if not divergence_is_open(divergence):
            continue
        close_divergence(divergence, f"card left the active dispatcher cycle (state={card_state})")
        closed_ids.append(str(divergence.get("id") or ""))
    return closed_ids


def _current_card(runtime: Any, ref: str) -> dict[str, Any] | None:
    """The card as the board has it right now, or None when it could not be asked.

    A `None` here means "skip this ref this tick", never "treat as gone": a transient backend error
    must not look like the card left the cycle. A card the board says is gone comes back as a
    `not_found` state.
    """
    try:
        return runtime.reader.show(ref)
    except TaskError as exc:
        return {"ref": ref, "state": "not_found"} if exc.code == "not_found" else None
    except Exception:  # noqa: BLE001 - one step's failure is recorded, never ends the tick
        return None


def _production_mutation_guard(runtime: Any, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Whether this tick may write the production state at all.

    Two fences, both read off the production state itself: another owner holds the pipeline, or the
    state is in a phase this dispatcher does not write. An installation with no production state yet
    reads as phase `new` and passes, which is how the first tick takes ownership.
    """
    owner = str(payload.get("owner") or "")
    if owner and owner != runtime.owner:
        return {
            "status": "blocked",
            "step": "production-guard",
            "reason": "production ownership fence is held by another owner",
            "owner": owner,
        }
    phase = str(payload.get("phase") or "new")
    if phase not in {"new", "production"}:
        return {
            "status": "blocked",
            "step": "production-guard",
            "reason": "production state is not writable",
            "phase": phase,
        }
    return None


def _production_tasks(runtime: Any, states: set[str]) -> list[dict[str, Any]]:
    with tick_phase("snapshot"):
        return sorted(runtime.reader.list(states=states), key=_task_sort_key)


def _production_tick_active(
    runtime: Any,
    task: dict[str, Any],
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
) -> dict[str, Any]:
    ref = task["ref"]
    task = runtime.reader.show(ref)
    record = records.get(ref)
    if record is not None and record.activation_recovery is not None:
        return runtime._tick_task(task, records, payload, record.attempt_id)
    mismatch = _production_active_mismatch(runtime, task, record, records, payload)
    if mismatch is not None:
        return mismatch
    attempt_id = (
        record.attempt_id if record is not None and record.attempt_id else production_adopt_attempt_id(ref)
    )
    outcome = runtime._tick_task(task, records, payload, attempt_id)
    return outcome


def _production_active_mismatch(
    runtime: Any,
    task: dict[str, Any],
    record: DispatcherRecord | None,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    if record is None:
        return None
    actual_worker = task.get("claim", {}).get("worker")
    if actual_worker in (None, record.worker):
        return None
    stopped = _stop_record_heads(runtime, record, task["ref"], str(task.get("state") or ""))
    if stopped is not None:
        stopped["step"] = "production-recovery"
        stopped["reason"] = (
            "active task claim no longer matches production record, and its heads could not be "
            f"stopped: {stopped['reason']}"
        )
        return stopped
    attempt_accounting.terminal_effect(runtime, 
        task,
        record,
        target="blocked",
        reason="production recovery blocked: active task claim no longer matches production record",
        request_id=_attempt_request_id(record.attempt_id, "active-mismatch-blocked", task["ref"]),
        terminal_state="blocked",
        disposition="blocked",
        # A durable claim/record disagreement is source evidence in its own
        # right, not an uncharged head bring-up infrastructure failure.
        blocked_reason="other",
    )
    divergence = record_divergence(
        payload,
        record.attempt_id,
        task["ref"],
        "production-recovery",
        "active_claim_mismatch",
        expected={"worker": record.worker, "state": task.get("state")},
        actual={"worker": actual_worker, "state": task.get("state")},
        details=["worker"],
    )
    records.pop(task["ref"], None)
    return {
        "status": "blocked",
        "step": "production-recovery",
        "ref": task["ref"],
        "reason": "active task claim no longer matches production record",
        "divergence_id": divergence["id"],
    }


def _production_claim_ready(
    runtime: Any,
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    fence: dict[str, Any] | None = None,
    blocked_scopes: dict[str, set[str]] | None = None,
) -> dict[str, Any] | None:
    fence = fence or {"sprints": set(), "projects": set(), "refs": set()}
    blocked_sprints = set((blocked_scopes or {}).get("sprints") or ())
    blocked_projects = set((blocked_scopes or {}).get("projects") or ())
    active_code_projects = {
        str(task.get("project") or "")
        for task in _production_tasks(runtime, set(ACTIVE_STATES))
        if task.get("type") == "code" and task.get("project") and not is_steward_report(task)
    }
    skipped: list[dict[str, str]] = []
    sprint_cache: dict[str, dict[str, Any]] = {}
    sprint_errors: dict[str, str] = {}
    blockers: dict[str, Any] = {}
    for task in _production_tasks(runtime, {"ready"}):
        record = records.get(task["ref"])
        if record is not None and record.activation_recovery is not None:
            skipped.append({"ref": task["ref"], "reason": "production activation recovery remains owed"})
            continue
        if is_steward_report(task):
            skipped.append({"ref": task["ref"], "reason": "steward report is not claimable"})
            continue
        # A card held by a wait card stays in Ready until the wait delivers its outcome to it.
        if waits := pending_wait_blockers(runtime, task, blockers):
            skipped.append({"ref": task["ref"], "reason": "blocked by pending wait " + ", ".join(waits)})
            continue
        if fenced_task(fence, task):
            skipped.append(
                {
                    "ref": task["ref"],
                    "reason": "the sprint holding this project has no working declared observer",
                }
            )
            continue
        sprint_ref = str(task.get("sprint") or "")
        project = str(task.get("project") or "")
        # A card that went blocked this cycle closes its own sprint and its own project to new
        # claims, and nothing beyond them: another sprint working on other projects keeps moving.
        if sprint_ref and sprint_ref in blocked_sprints:
            skipped.append(
                {
                    "ref": task["ref"],
                    "reason": "this sprint has a card blocked in this cycle",
                }
            )
            continue
        if project and project in blocked_projects:
            skipped.append(
                {
                    "ref": task["ref"],
                    "reason": "this project has a card blocked in this cycle",
                }
            )
            continue
        if sprint_ref:
            sprint = sprint_cache.get(sprint_ref)
            if sprint is None and sprint_ref not in sprint_errors:
                try:
                    sprint = runtime.sprints.show(sprint_ref, include_cards=False)
                except TaskError as exc:
                    sprint_errors[sprint_ref] = exc.message
                else:
                    sprint_cache[sprint_ref] = sprint
            if sprint_ref in sprint_errors:
                skipped.append(
                    {
                        "ref": task["ref"],
                        "reason": "linked sprint cannot be read: " + sprint_errors[sprint_ref],
                    }
                )
                continue
            assert sprint is not None
            if sprint.get("status") != "open":
                skipped.append({"ref": task["ref"], "reason": "linked sprint is stopped or closed"})
                continue
        if task.get("type") == "code" and task.get("project") in active_code_projects:
            skipped.append(
                {
                    "ref": task["ref"],
                    "reason": "project has an active code task",
                }
            )
            continue
        attempt_id = new_attempt_id()
        resume_workspaces = payload.get("resume_workspaces")
        resume_workspace = isinstance(resume_workspaces, dict) and task["ref"] in resume_workspaces
        try:
            outcome = claim_ready_task(
                runtime,
                task,
                records,
                payload,
                attempt_id,
                resume_workspace=resume_workspace,
            )
        except TaskError as exc:
            if exc.code in {"capacity_reached", "claim_conflict", "predecessor_open"}:
                skipped.append({"ref": task["ref"], "reason": exc.message})
                continue
            raise
        # Every claim-skip, not one of them: the pass moves to the next Ready card whatever made
        # this one unclaimable. A skip the scan does not recognise falls through to the return
        # below and ends the pass, which stops cards that had somewhere to go — see
        # CLAIM_SKIP_ACTIONS for the registry a new skip has to join.
        if is_claim_skip(outcome):
            skipped.append(
                {
                    "ref": task["ref"],
                    "reason": str(outcome.get("reason") or "head resource is not ready"),
                }
            )
            continue
        if skipped:
            outcome["skipped_ready"] = skipped
        return outcome
    if skipped:
        return {
            "status": "skipped",
            "step": "production-claim",
            "reason": "no claimable Ready task",
            "skipped_ready": skipped,
        }
    return None


def _task_sort_key(task: dict[str, Any]) -> tuple[int, str, str]:
    return (int(task.get("position") or 0), str(task.get("ref") or ""), str(task.get("id") or ""))


def _unexpected_error(reference: str, exc: Exception) -> dict[str, str]:
    return {
        "ref": reference,
        "code": "unexpected_error",
        "message": exc.__class__.__name__,
    }


#: How many uncharged budget candidates one tick resolves at most. The set is empty between ticks in
#: steady state; a backlog (the first pass after a deployment, a board outage) drains a page a tick.
BUDGET_CANDIDATE_PAGE = 200


def _reconcile_sprint_budget(runtime: Any) -> list[dict[str, Any]]:
    """Charge each durable card event once, using its audit identity as the budget request id.

    The pass reads a page of the uncharged candidate set (`ummanu.board.budget_candidates`), not
    the audit's history: committed events that may be budget events and whose charge id has no
    committed record. Every candidate it reads leaves the set exactly once, by a charge or by a
    terminal marker under the charge id: unlinked card, no budget type, or an invalid taxonomy. Only a
    failed card lookup leaves it in the set, for a later tick; nothing records a position to pass it.
    """
    instance = getattr(runtime.catalog, "instance", {})
    thresholds = budget_thresholds(instance if isinstance(instance, dict) else None)
    writer = SprintWriter(
        runtime.reader.client,
        data_dir=Path(getattr(runtime, "data_dir", None) or Path(runtime.audit.board_dir).parent),
        thresholds=thresholds,
    )
    outcomes: list[dict[str, Any]] = []
    sprint_cache: dict[str, str | None] = {}
    for event in runtime.audit.uncharged_budget_candidates(limit=BUDGET_CANDIDATE_PAGE):
        reference = str(event.get("ref") or "")
        identity = str(event.get("event_id") or event.get("request_id") or "")
        if not reference or reference.startswith("sprint:") or not identity:
            continue
        request_id = "sprint-budget-" + identity
        try:
            event_type = _budget_event_type(event)
        except TerminalTaxonomyValidationError as exc:
            # A corrupt observation is not a lifecycle concern and must not
            # prevent later durable events from reaching their budget seam.
            # The record cannot change, so it is reported once and marked.
            outcomes.append(
                {
                    "status": "degraded",
                    "step": "sprint-budget",
                    "action": "terminal-taxonomy-invalid",
                    "ref": reference,
                    "reason": str(exc),
                }
            )
            _record_unclassified_budget_event(runtime, event, request_id, identity, str(exc))
            continue
        if event_type is None:
            # The candidate predicate is a superset of the classifier (a forward Blocked record whose
            # taxonomy owns no budget type); the answer is as final as the record it was read from.
            _record_unclassified_budget_event(runtime, event, request_id, identity, "no budget event type")
            continue
        sprint = _event_sprint(runtime, event, sprint_cache)
        if sprint is None:
            # A transient board failure must remain eligible for the next tick.  Only a successful
            # lookup that proves the card is unlinked gets a durable terminal marker below.
            continue
        if not sprint:
            _record_unlinked_budget_event(runtime, event, request_id, identity, event_type)
            continue
        result = writer.record_budget(
            role="dispatcher",
            actor=runtime.owner,
            reference=sprint,
            event_type=event_type,
            request_id=request_id,
            source_event_id=identity,
        )
        outcomes.append(
            {
                "status": "ok",
                "step": "sprint-budget",
                "sprint": sprint,
                "ref": reference,
                "event_type": event_type,
                "hard_stopped": result["sprint"]["status"] == "stopped",
            }
        )
    return outcomes


def _event_sprint(runtime: Any, event: dict[str, Any], cache: dict[str, str | None]) -> str | None:
    reference = str(event.get("ref") or "")
    if reference in cache:
        return cache[reference]
    payload = event.get("payload")
    if event.get("kind") == "created" and isinstance(payload, dict):
        sprint = str(payload.get("sprint") or "")
    else:
        try:
            sprint = str(runtime.reader.show(reference).get("sprint") or "")
        except TaskError:
            sprint = None
    cache[reference] = sprint
    return sprint


def _record_unlinked_budget_event(
    runtime: Any,
    event: dict[str, Any],
    request_id: str,
    source_event_id: str,
    event_type: str,
) -> None:
    """Durably remember that a budget-shaped card event has no sprint to charge.

    The audit request id is deliberately the same one a charge would use. A card's sprint link is
    assigned at creation and cannot appear later, so this is a terminal result.
    """
    runtime.audit.append(
        request_id,
        {
            "event_id": "evt_budget_unlinked_" + source_event_id,
            "schema_version": 1,
            "occurred_at": now_rfc3339(),
            "actor": {"role": "dispatcher", "id": runtime.owner},
            "kind": "budget_unlinked",
            "outcome": "success",
            "task_id": str(event.get("task_id") or ""),
            "ref": str(event.get("ref") or ""),
            "backend": dict(event.get("backend") or {}),
            "request_id": request_id,
            "payload": {"source_event_id": source_event_id, "event_type": event_type},
        },
    )


def _record_unclassified_budget_event(
    runtime: Any,
    event: dict[str, Any],
    request_id: str,
    source_event_id: str,
    reason: str,
) -> None:
    """Durably remember that a budget candidate is no budget event, under its charge id.

    `_budget_event_type` reads only the committed record, so its answer cannot change; the marker
    takes the candidate out of the uncharged set instead of it being classified again every tick.
    """
    runtime.audit.append(
        request_id,
        {
            "event_id": "evt_budget_unclassified_" + source_event_id,
            "schema_version": 1,
            "occurred_at": now_rfc3339(),
            "actor": {"role": "dispatcher", "id": runtime.owner},
            "kind": "budget_unclassified",
            "outcome": "success",
            "task_id": str(event.get("task_id") or ""),
            "ref": str(event.get("ref") or ""),
            "backend": dict(event.get("backend") or {}),
            "request_id": request_id,
            "payload": {"source_event_id": source_event_id, "reason": reason},
        },
    )


def _budget_event_type(event: dict[str, Any]) -> str | None:
    payload = event.get("data") if event.get("record_type") == Event.RECORD_TYPE else event.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    marker = str(payload.get("marker") or "")
    if event.get("kind") in {"verdict", EventKind.CARD_VERDICTED.value} and marker == "review:red":
        return "red_review"
    if event.get("record_type") == Event.RECORD_TYPE:
        transition = event.get("transition") if isinstance(event.get("transition"), dict) else {}
        target = str(transition.get("target") or "")
        source = str(transition.get("source") or "")
    elif event.get("kind") == "moved":
        target = str(payload.get("to") or "")
        source = str(payload.get("from") or "")
    else:
        target = source = ""
    if target and payload.get(WAIT_OUTCOME_KEY):
        # A wait card's outcome, or a dependent it Blocked: not a pipeline restart (secretary-1790).
        return None
    if target:
        request_id = str(event.get("request_id") or "")
        if target == "blocked":
            if payload.get("terminal_taxonomy") is None:
                # Taxonomy did not exist when this transition committed. Keep
                # its durable action-token accounting rather than inventing a
                # forward classification from prose or current state.
                return (
                    "infrastructure_blocked"
                    if bring_up_failure_class(request_id) == FAILURE_CLASS_INFRASTRUCTURE
                    else "blocked"
                )
            # A committed forward record owns its disposition. In particular,
            # an assessment reslice can target Blocked without becoming a
            # blocked taxonomy disposition during budget recovery.
            return budget_event_type(read_terminal_taxonomy(payload, disposition=None))
        if target == "ready" and source in ACTIVE_STATES:
            # A worker whose provider failed with no launchable head in its chain waits in Ready
            # for that provider (secretary-1799): not a restart, so nothing is charged for it.
            return None if is_provider_unavailable_return(request_id) else "preempt"
        if target == "in_progress" and "gate-red" in request_id:
            return "red_ci"
    if event.get("kind") == "created":
        budget_event = str(payload.get("budget_event") or "")
        if budget_event in {"recreated_task", "hotfix"}:
            return budget_event
    return None
