"""Persistent observer records, delivery cursors and serialization."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ummanu.dispatch.worker_lifecycle import WorkerContinuationLiveness


class DeliveryStage(str, Enum):
    """The durable state of one observer event-delivery batch."""

    IDLE = "idle"
    WAITING_FOR_IDLE = "waiting_for_idle"
    DELIVERY_INTENT = "delivery_intent"
    AWAITING_ACK = "awaiting_ack"
    RETRY_DEFERRED = "retry_deferred"


class ObserverWakeLiveness(WorkerContinuationLiveness):
    """The exact-run provider-progress episode for one observer event batch.

    Records no provider text and is never rebound from a workspace scan: a new observer HeadRun
    opens a new episode, while a recovered episode accepts only the binding it already names.
    """


@dataclass(frozen=True)
class QuietStopJudgement:
    """Evidence for a conditional observer stop at the tick's decision point.

Capture both the head's activity epoch and pid-heartbeat liveness together.
A later read could miss intervening activity; a pane's busy state cannot
distinguish a working head from a dead head's leftover shell.
    """

    activity_epoch: int
    head_process_alive: bool


@dataclass
class ObserverDelivery:
    """One causal delivery cursor, independent of observer process lifecycle.

    `through_event` is fixed before a terminal send or replacement launch, and events appended after
    the intent are deliberately left for the next batch.
    """

    stage: DeliveryStage = DeliveryStage.IDLE
    acknowledged_through: str = ""
    acknowledged_delivery_id: str = ""
    acknowledged_resume_id: str = ""
    pending_from: str = ""
    delivery_id: str = ""
    method: str = ""
    through_event: str = ""
    # Legacy resume cursors fail closed until the acknowledgement deadline.
    resume_cursor: str = ""
    resume_cursor_known: bool = True
    sent_at: float = 0.0
    # Busy-head holds are bounded even before a prompt is delivered.
    held_since: float = 0.0
    deadline: float = 0.0
    attempts: int = 0
    # Heads replaced for refusing this batch's wakes; only an acknowledgement clears it.
    replacements: int = 0
    next_at: float = 0.0
    reason: str = ""
    # Cumulative observer-wake evidence is distinct from per-batch retries and card-head counters.
    wake_attempts: int = 0
    wake_failures: int = 0
    launch_delivery_attempts: int = 0
    launch_delivery_failures: int = 0
    last_failure_reason: str = ""
    last_failure_at: float = 0.0
    last_failure_method: str = ""
    # Store content-free evidence for the last attempted wake, never its prompt.
    last_evidence: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "stage": self.stage.value,
            "acknowledged_through": self.acknowledged_through,
            "acknowledged_delivery_id": self.acknowledged_delivery_id,
            "acknowledged_resume_id": self.acknowledged_resume_id,
            "pending_from": self.pending_from,
            "delivery_id": self.delivery_id,
            "method": self.method,
            "through_event": self.through_event,
            "resume_cursor": self.resume_cursor,
            "resume_cursor_known": self.resume_cursor_known,
            "sent_at": self.sent_at,
            "held_since": self.held_since,
            "deadline": self.deadline,
            "attempts": self.attempts,
            "replacements": self.replacements,
            "next_at": self.next_at,
            "reason": self.reason,
            "wake_attempts": self.wake_attempts,
            "wake_failures": self.wake_failures,
            "launch_delivery_attempts": self.launch_delivery_attempts,
            "launch_delivery_failures": self.launch_delivery_failures,
            "last_failure_reason": self.last_failure_reason,
            "last_failure_at": self.last_failure_at,
            "last_failure_method": self.last_failure_method,
            "last_evidence": dict(self.last_evidence),
        }

    @classmethod
    def from_json(cls, payload: Any) -> ObserverDelivery:
        if not isinstance(payload, dict):
            return cls()
        try:
            stage = DeliveryStage(str(payload.get("stage") or DeliveryStage.IDLE.value))
        except ValueError:
            stage = DeliveryStage.IDLE
        return cls(
            stage=stage,
            acknowledged_through=str(payload.get("acknowledged_through") or ""),
            acknowledged_delivery_id=str(payload.get("acknowledged_delivery_id") or ""),
            acknowledged_resume_id=str(payload.get("acknowledged_resume_id") or ""),
            pending_from=str(payload.get("pending_from") or ""),
            delivery_id=str(payload.get("delivery_id") or ""),
            method=str(payload.get("method") or ""),
            through_event=str(payload.get("through_event") or ""),
            resume_cursor=str(payload.get("resume_cursor") or ""),
            resume_cursor_known=bool(payload.get("resume_cursor_known", True)),
            sent_at=_float(payload.get("sent_at")),
            held_since=_float(payload.get("held_since")),
            deadline=_float(payload.get("deadline")),
            attempts=_int(payload.get("attempts")),
            replacements=_int(payload.get("replacements")),
            next_at=_float(payload.get("next_at")),
            reason=str(payload.get("reason") or ""),
            wake_attempts=_int(payload.get("wake_attempts")),
            wake_failures=_int(payload.get("wake_failures")),
            launch_delivery_attempts=_int(payload.get("launch_delivery_attempts")),
            launch_delivery_failures=_int(payload.get("launch_delivery_failures")),
            last_failure_reason=str(payload.get("last_failure_reason") or ""),
            last_failure_at=_float(payload.get("last_failure_at")),
            last_failure_method=str(payload.get("last_failure_method") or ""),
            last_evidence=(
                dict(payload["last_evidence"]) if isinstance(payload.get("last_evidence"), dict) else {}
            ),
        )


@dataclass
class ObserverRecord:
    """One observer head as the dispatcher last left it."""

    sprint: str
    # Per-record identity prevents a recreated sprint from replaying its predecessor's requests.
    generation: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    head: str = ""
    workspace: str = ""
    handle: str = ""
    # Orca may reissue handles; leafId remains the stable PTY identity.
    leaf: str = ""
    pid_file: str = ""
    launches: int = 0
    # A nonzero launch attempt marks a bring-up whose tick did not survive.
    pending_launch: int = 0
    # Keep possible terminals until a stop confirms they are gone.
    head_possible: bool = False
    # Workspace registration can outlive a failed bring-up without a head.
    workspace_live: bool = False
    # An aborted launch terminal is not a working observer and must be closed.
    abandoned_handle: bool = False
    # Only launch-time binding proves an observer may write for its sprint.
    bound: bool = False
    state: str = "pending"
    launched_at: float = 0.0
    last_action: str = ""
    last_action_at: float = 0.0
    # Work-state explanation, independent of idle/deferred/stop lifecycle reasons.
    state_reason: str = ""
    deferred_reason: str = ""
    stopped_reason: str = ""
    paused_at: float = 0.0
    # The last time the pane was confirmed ready explains why a live head is waiting for a card
    # event.
    idle_since: float = 0.0
    idle_reason: str = ""
    # The event cursor is dispatcher-owned state. A resume entry stays its established six-field
    # document; this delivery machine carries the causal acknowledgement for its fixed batch.
    delivery: ObserverDelivery = field(default_factory=ObserverDelivery)
    # Launch failures are retried on the same bounded schedule as terminal nudges.  They are
    # separate because a sprint can be headless without having a linked-card event to wake for.
    launch_attempts: int = 0
    launch_next_at: float = 0.0
    run: dict[str, Any] = field(default_factory=dict)
    # `run` is routing telemetry.  This is the actual runtime HeadRun used to bind the heartbeat.
    head_run: dict[str, Any] = field(default_factory=dict)
    # Exact-HeadRun provider progress for an event delivery held by this observer.  A missing or
    # malformed value is historical/unknown, never permission to bind today's workspace journal.
    wake_liveness: ObserverWakeLiveness = field(default_factory=ObserverWakeLiveness)
    # The terminal episode of the immediately retired HeadRun.  This is audit-only: all decisions
    # use ``wake_liveness``, which is always bound to the current HeadRun after a replacement.
    retired_wake_liveness: dict[str, Any] = field(default_factory=dict)
    # The profile actually running when a provider fallback put the observer on another head of the
    # declared profile's chain (ummanu-108); empty when it runs on the declared `head` itself.
    fallback_head: str = ""

    def to_json(self) -> dict[str, Any]:
        value = self._json()
        if self.fallback_head:
            value["fallback_head"] = self.fallback_head
        return value

    def _json(self) -> dict[str, Any]:
        return {
            "sprint": self.sprint,
            "generation": self.generation,
            "head": self.head,
            "workspace": self.workspace,
            "handle": self.handle,
            "leaf": self.leaf,
            "pid_file": self.pid_file,
            "launches": self.launches,
            "pending_launch": self.pending_launch,
            "head_possible": self.head_possible,
            "workspace_live": self.workspace_live,
            "abandoned_handle": self.abandoned_handle,
            "bound": self.bound,
            "state": self.state,
            "launched_at": self.launched_at,
            "last_action": self.last_action,
            "last_action_at": self.last_action_at,
            "state_reason": self.state_reason,
            "deferred_reason": self.deferred_reason,
            "stopped_reason": self.stopped_reason,
            "paused_at": self.paused_at,
            "idle_since": self.idle_since,
            "idle_reason": self.idle_reason,
            "delivery": self.delivery.to_json(),
            "launch_attempts": self.launch_attempts,
            "launch_next_at": self.launch_next_at,
            "run": dict(self.run),
            "head_run": dict(self.head_run),
            "wake_liveness": self.wake_liveness.to_json(),
            "retired_wake_liveness": dict(self.retired_wake_liveness),
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> ObserverRecord:
        run = payload.get("run")
        delivery = ObserverDelivery.from_json(payload.get("delivery"))
        return cls(
            sprint=str(payload.get("sprint") or ""),
            generation=str(payload.get("generation") or "") or uuid.uuid4().hex[:12],
            head=str(payload.get("head") or ""),
            workspace=str(payload.get("workspace") or ""),
            handle=str(payload.get("handle") or ""),
            leaf=str(payload.get("leaf") or ""),
            pid_file=str(payload.get("pid_file") or ""),
            launches=_int(payload.get("launches")),
            pending_launch=_int(payload.get("pending_launch")),
            # A record written before this field existed carries a handle when a head is up, so
            # the handle is what it falls back to rather than a blanket "nothing is running".
            head_possible=bool(payload.get("head_possible", bool(payload.get("handle")))),
            # Likewise: a record written before this field existed names a workspace only once a
            # bring-up has been through it, and the host reads Orca to tell a registered one from
            # a path it never learned about.
            workspace_live=bool(payload.get("workspace_live", bool(payload.get("workspace")))),
            abandoned_handle=bool(payload.get("abandoned_handle")),
            # No default beyond False: a record written before this field existed describes a head
            # launched without a binding, and reading it as bound is exactly the head that would be
            # left running and refused on every write.
            bound=bool(payload.get("bound")),
            state=str(payload.get("state") or "pending"),
            launched_at=_float(payload.get("launched_at")),
            last_action=str(payload.get("last_action") or ""),
            last_action_at=_float(payload.get("last_action_at")),
            state_reason=str(payload.get("state_reason") or ""),
            deferred_reason=str(payload.get("deferred_reason") or ""),
            stopped_reason=str(payload.get("stopped_reason") or ""),
            paused_at=_float(payload.get("paused_at")),
            idle_since=_float(payload.get("idle_since")),
            idle_reason=str(payload.get("idle_reason") or ""),
            delivery=delivery,
            launch_attempts=_int(payload.get("launch_attempts")),
            launch_next_at=_float(payload.get("launch_next_at")),
            run=dict(run) if isinstance(run, dict) else {},
            head_run=(dict(payload["head_run"]) if isinstance(payload.get("head_run"), dict) else {}),
            wake_liveness=ObserverWakeLiveness.from_json(payload.get("wake_liveness")),
            retired_wake_liveness=(
                dict(payload["retired_wake_liveness"])
                if isinstance(payload.get("retired_wake_liveness"), dict)
                else {}
            ),
            fallback_head=str(payload.get("fallback_head") or ""),
        )


def load_observers(payload: dict[str, Any]) -> dict[str, ObserverRecord]:
    raw = payload.get("observers")
    if not isinstance(raw, dict):
        return {}
    return {
        str(ref): ObserverRecord.from_json(record) for ref, record in raw.items() if isinstance(record, dict)
    }


def put_observers(payload: dict[str, Any], observers: dict[str, ObserverRecord]) -> None:
    payload["observers"] = {ref: record.to_json() for ref, record in sorted(observers.items())}

def delivery_evidence_summary(delivery: Any) -> str:
    """One line of delivery evidence for the head that has to report it, or an empty string.

    Deliberately counts and a reason, never a retry instruction: redelivery is the dispatcher's, and
    a head reading this is reporting history. It reads its numbers defensively because a host seam
    may hand it any record shape.
    """
    wake_failures = _int(getattr(delivery, "wake_failures", 0))
    launch_failures = _int(getattr(delivery, "launch_delivery_failures", 0))
    attempts = _int(getattr(delivery, "wake_attempts", 0)) + _int(
        getattr(delivery, "launch_delivery_attempts", 0)
    )
    failures = wake_failures + launch_failures
    if not attempts and not failures:
        return ""
    line = (
        f"observer delivery so far: {attempts} attempt(s), {failures} failed "
        f"({wake_failures} wake, {launch_failures} launch)"
    )
    reason = str(getattr(delivery, "last_failure_reason", "") or "")
    if reason:
        method = str(getattr(delivery, "last_failure_method", "") or "observer-wake")
        line += f"; last failure ({method}): {reason}"
    return line

def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0
