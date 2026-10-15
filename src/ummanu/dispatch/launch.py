"""Durable launch intents for dispatcher worker and reviewer heads.

Write the role, reserved round/attempt, workspace and pid-file identity before
asking the host to start a process. Resolve the intent before advancing the card:
adopt a confirmed live pid, wait through heartbeat grace if unknown, and clean up
a confirmed-dead or abandoned launch before retrying.

Record the actual pane, launched profile and HeadRun while the intent remains.
A failed state write prevents launch; a post-pane failure preserves the intent.
Never treat a refused stop as complete or start a second head without resolving
its predecessor.
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ummanu.dispatch import attempt_accounting
from ummanu.dispatch.card_roles import REVIEW_ROLE, WORKER_ROLE, card_kind, head_role
from ummanu.dispatch.heartbeat import intent_heartbeat_identity
from ummanu.dispatch.helpers import scrub_host_output
from ummanu.dispatch.state import DispatcherRecord, LaunchIntent
from ummanu.dispatch.tui import (
    DELIVERY_RECEIPT_ACCEPTED,
    DELIVERY_RECEIPT_REFUSED,
    DELIVERY_RECEIPT_UNOBSERVED,
    PRE_DELIVERY_STARTING,
    PRE_DELIVERY_UNKNOWN_DIALOG,
    PRE_DELIVERY_UPDATE_MODAL,
    READINESS_BLOCKED,
    READINESS_BUSY,
    delivery_readiness_state,
    delivery_receipt_state,
)
from ummanu.dispatch.types import (
    STOPPED_BY_LAUNCH_RECOVERY,
    HeadLaunchAborted,
    HostError,
)
from ummanu.dispatch.watchdog import (
    bind_head_heartbeat,
    head_process_status,
    initial_output_stall_seconds,
    pid_file_path,
)
from ummanu.runtime.head import operations as head_ops

PANE_STATE_LABELS = {
    READINESS_BUSY: "busy",
    READINESS_BLOCKED: "held in a dialog",
    PRE_DELIVERY_UPDATE_MODAL: "holding its own update modal",
    PRE_DELIVERY_STARTING: "still starting",
    PRE_DELIVERY_UNKNOWN_DIALOG: "holding a dialog nothing here recognises",
    DELIVERY_RECEIPT_REFUSED: "not accepting the pointer",
}

# A busy review nudge retries through its exact existing run on a bounded schedule.
REVIEW_BUSY_RETRY_INITIAL_SECONDS = 30
REVIEW_BUSY_RETRY_MAX_SECONDS = 5 * 60

# The delivery state of a launch whose pointer the composer took.
LAUNCH_DELIVERY_CONFIRMED = "confirmed"
# The delivery state of a launch whose production handoff is still in progress (`runtime.head.handoff`):
# not a refusal and not busy, so it spends no attempt and waits for no backoff. Its own bounds are the
# handoff's (`prompt_settle`, `SUBMIT_ATTEMPTS`), which end it in a refusal this ladder counts.
LAUNCH_DELIVERY_HANDOFF_PENDING = "handoff_pending"
# Set on a launch's delivery once the worker was shut down, confirmed, before its reviewer was given
# anything (`CommandHostRuntime._fence_worker_for_reviewer`); a retained worker is confirmed anew.
LAUNCH_DELIVERY_WORKER_FENCED = "worker_fenced"
# How many ticks a launch may hold its card while its pointer is still unaccepted. Past this the
# head is stopped and relaunched: a head that cannot be made sendable is replaced, and it never
# sits indefinitely while the card reports progress.
LAUNCH_DELIVERY_MAX_ATTEMPTS = int(os.environ.get("UMMANU_LAUNCH_DELIVERY_MAX_ATTEMPTS", "5") or 5)

# Catch only storage failures: other errors must not masquerade as unwritable state.
STORAGE_ERRORS = (OSError, ValueError, TypeError, UnicodeError)


def launch_pid_file(role: str, reference: str) -> str:
    """The heartbeat file the head of this role writes. Known before the head exists."""
    return pid_file_path(card_kind(role), reference)


def launch_intent(record: DispatcherRecord) -> dict[str, Any]:
    intent = getattr(record, "launch_intent", None)
    return intent if isinstance(intent, dict) and intent.get("role") else {}


def launch_delivery(intent: dict[str, Any]) -> dict[str, Any]:
    """The delivery record this intent carries, whatever it says."""
    delivery = intent.get("delivery") if isinstance(intent, dict) else None
    return dict(delivery) if isinstance(delivery, dict) else {}


def busy_launch_delivery(intent: dict[str, Any]) -> dict[str, Any]:
    """Return a durable, still-undelivered busy launch nudge, if this intent has one."""
    delivery = launch_delivery(intent)
    if delivery.get("state") != READINESS_BUSY:
        return {}
    return delivery


def launch_delivery_state(evidence: Any) -> str:
    """Name what one delivery boundary receipt says, in the words an intent keeps.

    A pane the boundary found busy or held in a dialog keeps that state, because the retry schedule
    reads it; anything else the composer did not accept is simply `refused`.
    """
    receipt = delivery_receipt_state(evidence)
    if receipt == DELIVERY_RECEIPT_ACCEPTED:
        return LAUNCH_DELIVERY_CONFIRMED
    readiness = delivery_readiness_state(evidence)
    if readiness in {READINESS_BUSY, READINESS_BLOCKED}:
        return readiness
    return receipt


def launch_delivery_receipt(evidence: Any) -> dict[str, Any] | None:
    """Return durable delivery evidence, or None when delivery was not observed.

None does not prove delivery: the launch may have failed before a prompt existed
or the head may not have received it.
    """
    if delivery_receipt_state(evidence) == DELIVERY_RECEIPT_UNOBSERVED:
        return None
    return {
        "state": launch_delivery_state(evidence),
        "receipt": delivery_receipt_state(evidence),
        "evidence": dict(evidence),
    }


def undelivered_launch_delivery(intent: dict[str, Any]) -> dict[str, Any]:
    """Evidence of an unaccepted launch delivery, or an empty mapping.

Adoption, recovery and reviewer retry use the same boundary verdict; a live
pid or writable pane never overrides a refused or unconfirmed prompt.
    """
    delivery = launch_delivery(intent)
    if not delivery:
        return {}
    if str(delivery.get("state") or "") == LAUNCH_DELIVERY_CONFIRMED:
        return {}
    if delivery_receipt_state(delivery.get("evidence")) == DELIVERY_RECEIPT_ACCEPTED:
        return {}
    return delivery


def remember_launch_delivery(intent: dict[str, Any], evidence: Any) -> None:
    """Put a delivery boundary receipt onto an intent, keeping any retry schedule already on it."""
    receipt = launch_delivery_receipt(evidence)
    if receipt is None:
        return
    intent["delivery"] = {**launch_delivery(intent), **receipt}


def defer_launch_delivery(
    record: DispatcherRecord | None,
    evidence: dict[str, Any],
    *,
    state: str = READINESS_BUSY,
    now: float | None = None,
) -> int:
    """Persist the next capped retry for a launch whose pointer the composer has not accepted."""
    intent = dict(launch_intent(record))
    if not intent:
        return 0
    previous = launch_delivery(intent)
    attempts = int(previous.get("attempts") or 0) + 1
    delay = min(
        REVIEW_BUSY_RETRY_INITIAL_SECONDS * (2 ** min(attempts - 1, 4)),
        REVIEW_BUSY_RETRY_MAX_SECONDS,
    )
    intent["delivery"] = {
        "state": state,
        "attempts": attempts,
        "next_at": (time.time() if now is None else now) + delay,
        "evidence": dict(evidence),
    }
    record.launch_intent = intent
    return delay


def defer_pending_launch_delivery(
    record: DispatcherRecord | None, evidence: dict[str, Any], *, worker_fenced: bool = False
) -> bool:
    """Keep a pending production handoff for the next tick.

Do not count it as an unsuccessful delivery or pane busy retry.
A persisted worker_fenced value remains set once the host established the fence.
    """
    intent = dict(launch_intent(record))
    if not intent:
        return False
    previous = launch_delivery(intent)
    delivery = {
        **previous,
        "state": LAUNCH_DELIVERY_HANDOFF_PENDING,
        "attempts": int(previous.get("attempts") or 0),
        "next_at": 0.0,
        "evidence": dict(evidence),
        # Kept through the confirmation, so adoption can tell this launch is `start_review` continued.
        "handoff": True,
        **({LAUNCH_DELIVERY_WORKER_FENCED: True} if worker_fenced or previous.get(LAUNCH_DELIVERY_WORKER_FENCED) else {}),
    }
    if delivery == previous:
        return False
    intent["delivery"] = delivery
    record.launch_intent = intent
    return True


def defer_busy_launch_delivery(
    record: DispatcherRecord | None,
    evidence: dict[str, Any],
    *,
    now: float | None = None,
) -> int:
    """Persist the next capped retry for a pre-send reviewer document nudge."""
    return defer_launch_delivery(record, evidence, state=READINESS_BUSY, now=now)


def write_launch_intent(
    runtime: Any,
    payload: dict[str, Any],
    records: dict[str, DispatcherRecord],
    ref: str,
    record: DispatcherRecord,
    *,
    role: str,
    action: str,
    head: str,
    workspace: str,
    document: str = "",
    round_number: int | None = None,
    task: dict[str, Any] | None = None,
) -> str | None:
    """Persist a launch intent before invoking the host.

A failed write leaves the record unchanged and prevents launch. Reserve the
new round here so an interrupted rework cannot resume in its rejected round.
    """
    role = card_kind(role)
    previous = dict(getattr(record, "launch_intent", None) or {})
    previous_workspace_settled = record.workspace_settled
    snapshot_field = "review_local_run_snapshot" if role == REVIEW_ROLE else "worker_local_run_snapshot"
    previous_snapshot = dict(getattr(record, snapshot_field, {}) or {})
    reserved = record.attempt_round if round_number is None else round_number
    run_id = uuid.uuid4().hex
    preflight_run: head_ops.HeadRun | None = None
    attest = getattr(getattr(runtime, "host", None), "preflight_codex_run", None)
    if callable(attest):
        document_name = "REVIEW.md" if role == REVIEW_ROLE else "TASK.md"
        task_document = document or str(Path(workspace) / document_name)
        try:
            candidate = attest(
                head,
                role=head_role(role),
                workspace=workspace,
                task_ref=head_ops.TaskRef.card(ref, document=task_document),
                pid_file=launch_pid_file(role, ref),
                run_id=run_id,
            )
        except Exception as exc:  # noqa: BLE001 — report any host preflight refusal
            return f"codex-fanout-policy: {type(exc).__name__}: {exc}"
        preflight_run = candidate
    snapshot_for_round = getattr(getattr(runtime, "host", None), "local_run_snapshot_for_round", None)
    if task is not None and callable(snapshot_for_round):
        policy_round = record.review_baseline if role == REVIEW_ROLE else record.report_generation
        setattr(record, snapshot_field, snapshot_for_round(task, role, policy_round, previous_snapshot))
    record.workspace_settled = False
    record.launch_intent = LaunchIntent(
        role=role,
        action=action,
        head=head,
        workspace=workspace,
        pid_file=launch_pid_file(role, ref),
        # Fix run id before host interaction to fence crash-era heartbeat recovery.
        run_id=run_id,
        task=f"card:{ref}",
        attempt_id=record.attempt_id,
        round_number=reserved,
        opens_round=bool(reserved) and reserved != record.attempt_round,
        respawns=int(getattr(record, f"{role}_respawns", 0) or 0),
        at=time.time(),
        head_run=preflight_run,
    )
    if preflight_run is not None:
        _remember_head_run(record, role, preflight_run.to_json())
    records[ref] = record
    try:
        runtime.save_records(payload, records)
    except STORAGE_ERRORS as exc:
        record.launch_intent = previous
        record.workspace_settled = previous_workspace_settled
        setattr(record, snapshot_field, previous_snapshot)
        return f"{type(exc).__name__}: {exc}"
    return None


def confirm_launch_intent(
    runtime: Any,
    payload: dict[str, Any],
    records: dict[str, DispatcherRecord],
    ref: str,
    record: DispatcherRecord,
    *,
    handle: str = "",
    leaf: str = "",
    run: dict[str, Any] | None = None,
    head_run: dict[str, Any] | None = None,
    delivery: dict[str, Any] | None = None,
) -> None:
    """Persist the pane, actual launch snapshot and HeadRun after host bring-up.

Save identity in both intent and record before further effects so recovery can
adopt exactly the running head. The pre-launch intent remains sufficient if
this write fails.
    """
    intent = dict(launch_intent(record))
    if not intent:
        return
    if handle:
        intent["handle"] = handle
    if leaf:
        intent["leaf"] = leaf
    if run:
        intent["run"] = dict(run)
    if head_run is not None:
        if head_run:
            # Preserve provider source binding against stale launcher results.
            canonical = _canonical_launch_head_run(record, str(intent.get("role") or ""), intent, head_run)
            intent["head_run"] = canonical
            # Intent, HeadRun, and heartbeat share the pre-pane run identity.
            intent["run_id"] = str(canonical.get("run_id") or intent.get("run_id") or "")
            head_run = canonical
        _remember_head_run(record, str(intent.get("role") or ""), head_run)
    if delivery is not None:
        # Persist delivery confirmation with its run; recovery never infers it from a heartbeat.
        intent["delivery"] = dict(delivery)
    intent["launched"] = True
    record.launch_intent = intent
    records[ref] = record
    _persist_quietly(runtime, payload, records)


def _remember_head_run(record: DispatcherRecord, role: str, head_run: dict[str, Any] | None) -> None:
    """Put one role's head run on the record. The caller's save is what makes it durable."""
    if head_run is None or not role:
        return
    setattr(record, role_field(role, "head_run"), dict(head_run))


def _canonical_launch_head_run(
    record: DispatcherRecord,
    role: str,
    intent: dict[str, Any],
    reported: dict[str, Any],
) -> dict[str, Any]:
    """Return the one post-delivery HeadRun a launch intent is permitted to write.

    The ingress writer and the pane operation own different facts, and both must describe the exact
    same launch identity. A foreign or damaged persistent copy is a fence, never a prompt to
    reconstruct or attribute a different head.
    """
    values: list[dict[str, Any]] = []
    stored = intent.get("head_run")
    if isinstance(stored, dict) and stored.get("run_id"):
        values.append(stored)
    record_run = getattr(record, role_field(role, "head_run"), {}) if role else {}
    if isinstance(record_run, dict) and record_run.get("run_id"):
        values.append(record_run)
    values.append(reported)
    # Apply canonical merge only to source-bearing Codex runs.
    if not any(_has_provider_source(value) for value in values):
        return dict(reported)
    try:
        current = head_ops.HeadRun.from_json(values[0])
        for value in values[1:]:
            current = _merge_launch_head_runs(current, head_ops.HeadRun.from_json(value))
    except HostError:
        raise
    except (TypeError, ValueError) as exc:
        raise HostError(f"launch HeadRun handoff is unreadable or identity-mismatched: {exc}") from None
    return current.to_json()


def _has_provider_source(value: dict[str, Any]) -> bool:
    policy = value.get("fanout_policy") if isinstance(value, dict) else None
    return isinstance(policy, dict) and isinstance(policy.get("provider_source"), dict)


def _merge_launch_head_runs(current: head_ops.HeadRun, later: head_ops.HeadRun) -> head_ops.HeadRun:
    """Merge verified pane/lifecycle evidence without ever discarding a bound source."""
    if (
        not current.same_run(later)
        or current.spec != later.spec
        or current.workspace != later.workspace
        or current.task_ref != later.task_ref
        or current.role != later.role
        or current.pid_file != later.pid_file
        or (current.scope_generation and later.scope_generation
            and current.scope_generation != later.scope_generation)
    ):
        raise HostError("launch HeadRun identity mismatch")
    policy = _newer_provider_policy(current.fanout_policy, later.fanout_policy)
    lifecycle_rank = {"spawned": 0, "working": 1, "finishing": 2, "exited": 3}
    if lifecycle_rank.get(later.lifecycle, -1) < lifecycle_rank.get(current.lifecycle, -1):
        lifecycle = current.lifecycle
        stopped_by = current.stopped_by
    else:
        lifecycle = later.lifecycle
        stopped_by = later.stopped_by
    # Empty newer fields never erase a retained crash-recovery address.
    return replace(
        later,
        handle=later.handle or current.handle,
        leaf=later.leaf or current.leaf,
        lifecycle=lifecycle,
        stopped_by=stopped_by,
        fanout_policy=policy,
        scope_generation=later.scope_generation or current.scope_generation,
    )


def merge_launch_head_run(current: dict[str, Any], later: dict[str, Any]) -> dict[str, Any]:
    """Merge two source-bearing writers for one run, fencing foreign identity at the boundary."""
    try:
        return _merge_launch_head_runs(
            head_ops.HeadRun.from_json(current),
            head_ops.HeadRun.from_json(later),
        ).to_json()
    except HostError:
        raise
    except (TypeError, ValueError) as exc:
        raise HostError(f"launch HeadRun handoff is unreadable: {exc}") from None


def _newer_provider_policy(current: dict[str, Any], later: dict[str, Any]) -> dict[str, Any]:
    """Choose a compatible provider source, preferring a bound cursor over stale launch state."""
    current_source = current.get("provider_source") if isinstance(current, dict) else None
    later_source = later.get("provider_source") if isinstance(later, dict) else None
    if not isinstance(current_source, dict):
        if isinstance(current, dict) and current.get("provider_source_required") is True:
            raise HostError("persisted provider source is incomplete")
        return dict(later)
    if not isinstance(later_source, dict):
        # A pane/lifecycle writer that knows nothing about a source has no authority to erase
        # either the preflight baseline or the source binding it was handed.
        return dict(current)
    current_state = str(current_source.get("state") or "")
    later_state = str(later_source.get("state") or "")
    if current_state == "unbound":
        preflight_keys = (
            "version",
            "kind",
            "run_id",
            "head_run_fingerprint",
            "workspace",
            "role",
            "task_ref",
            "root",
            "baseline",
        )
        if any(current_source.get(key) != later_source.get(key) for key in preflight_keys):
            raise HostError("preflight provider source conflicts for one launch HeadRun")
        if later_state in ("unbound", "bound"):
            return dict(later)
        return dict(current)
    if current_state != "bound":
        # This is a typed unavailable/identity-mismatch record.  It is intentionally not healed
        # by a later local result which happens to carry a well-formed source.
        raise HostError("persisted provider source is unavailable")
    if later_state != "bound":
        return dict(current)
    identity_keys = (
        "version",
        "kind",
        "run_id",
        "head_run_fingerprint",
        "workspace",
        "role",
        "task_ref",
        "root",
        "baseline",
        "path",
        "session_id",
        "parent_thread_id",
        "initial_range",
    )
    if any(current_source.get(key) != later_source.get(key) for key in identity_keys):
        raise HostError("bound provider sources conflict for one launch HeadRun")
    current_cursor = current_source.get("cursor") if isinstance(current_source.get("cursor"), dict) else {}
    later_cursor = later_source.get("cursor") if isinstance(later_source.get("cursor"), dict) else {}
    current_line = current_cursor.get("line")
    later_line = later_cursor.get("line")
    if not isinstance(current_line, int) or not isinstance(later_line, int):
        raise HostError("bound provider source cursor is malformed")
    if current_line == later_line:
        if current_cursor.get("digest") != later_cursor.get("digest"):
            raise HostError("bound provider source cursor conflicts for one launch HeadRun")
        return dict(later)
    return dict(later if later_line > current_line else current)


def launch_left_a_head(record: DispatcherRecord) -> bool:
    """Does this launch's heartbeat prove a head exists, whatever the failure claimed?

    The heartbeat is the one piece of evidence that can contradict a host claiming no head is
    running, and where it does the intent has to survive.
    """
    intent = launch_intent(record)
    if not intent:
        return False
    status = head_process_status(
        str(intent.get("pid_file") or ""), expected=intent_heartbeat_identity(intent)
    )
    return bool(status.get("match"))


def role_field(role: str, suffix: str) -> str:
    return f"{card_kind(role)}_{suffix}"


def clear_launch_intent(record: DispatcherRecord) -> None:
    """Take back an intent whose host call has answered. The caller's own save persists it.

    The heartbeat the intent named stays on the record as that role's head identity: it is what every
    later stop falls back on when the pane handle is missing, and an adopted head never had one.
    """
    intent = launch_intent(record)
    typed = record.launch_intent.intent
    role = typed.role if typed is not None else str(intent.get("role") or "")
    pid_file = typed.pid_file if typed is not None else str(intent.get("pid_file") or "")
    if role and pid_file:
        setattr(record, role_field(role, "pid_file"), pid_file)
    record.launch_intent = {}


def forget_role_head(record: DispatcherRecord, role: str) -> None:
    """Drop every pointer to one role's head, once it is confirmed gone."""
    role = card_kind(role)
    if role == REVIEW_ROLE:
        record.review_handle = ""
        record.review_leaf = ""
        record.review_pid_file = ""
        return
    record.handle = ""
    record.worker_leaf = ""
    record.worker_pid_file = ""
    record.worker_continuation.drop_session()


def mark_launch_aborted(
    runtime: Any,
    payload: dict[str, Any],
    records: dict[str, DispatcherRecord],
    ref: str,
    record: DispatcherRecord,
    exc: HeadLaunchAborted,
) -> None:
    """Keep the intent when bring-up fails after opening a pane.

Preserve observed pane/pid and delivery evidence for the next tick: a live,
writable head may still have received no prompt. An interrupted reviewer
document nudge is retried under its original identity.
    """
    intent = dict(launch_intent(record))
    if not intent:
        return
    remember_launch_delivery(intent, getattr(exc, "evidence", None))
    if exc.handle:
        intent["handle"] = exc.handle
    if exc.leaf:
        intent["leaf"] = exc.leaf
    if exc.head_run:
        # Preserve the live spawned run for adoption rather than reconstructing it.
        intent["head_run"] = dict(exc.head_run)
        _remember_head_run(record, str(intent.get("role") or ""), exc.head_run)
    intent["aborted"] = True
    record.launch_intent = intent
    records[ref] = record
    _persist_quietly(runtime, payload, records)


def launch_aborted(*, step: str, ref: str, attempt_id: str, role: str, reason: str) -> dict[str, Any]:
    """The outcome of a bring-up that may have left a head running and could not confirm it."""
    return {
        "status": "degraded",
        "step": step,
        "pilot_ref": ref,
        "attempt_id": attempt_id,
        "action": f"{role}-launch-aborted",
        "reason": f"launch may have left a head running: {reason}",
    }


def role_label(role: str) -> str:
    """What this role's head is called in a line an operator reads."""
    return head_role(card_kind(role))


def pane_state_label(readiness: str) -> str:
    return PANE_STATE_LABELS.get(readiness, readiness or "not ready")


# Classify shared worker/reviewer bring-up failures as infrastructure unless the card contract failed.
FAILURE_CLASS_INFRASTRUCTURE = "infrastructure"
FAILURE_CLASS_TASK = "task"
# Refused before any bring-up: no host failed and no attempt ran.
FAILURE_CLASS_ADMISSION = "admission"

CAUSE_LAUNCH_ABORTED = "launch_aborted"
CAUSE_HOST_UNAVAILABLE = "host_unavailable"
CAUSE_WORKSPACE_CONTRACT = "workspace_contract"
# The card names an integration base this project cannot integrate into (secretary-1541). Nothing
# about the code was judged, but no host repairs it either: it is the card's own contract.
CAUSE_BASE_BRANCH_CONTRACT = "base_branch_contract"
# The card names a project the instance does not enable for workloads.
CAUSE_PROJECT_NOT_ENABLED = "project_not_enabled"
CAUSE_ADMISSION_REFUSED = "admission_refused"
BRING_UP_CAUSE_CLASSES = {
    CAUSE_LAUNCH_ABORTED: FAILURE_CLASS_INFRASTRUCTURE,
    CAUSE_HOST_UNAVAILABLE: FAILURE_CLASS_INFRASTRUCTURE,
    CAUSE_WORKSPACE_CONTRACT: FAILURE_CLASS_TASK,
    CAUSE_BASE_BRANCH_CONTRACT: FAILURE_CLASS_TASK,
    CAUSE_PROJECT_NOT_ENABLED: FAILURE_CLASS_TASK,
    CAUSE_ADMISSION_REFUSED: FAILURE_CLASS_ADMISSION,
}
STAGE_CLAIM = "claim"
STAGE_RESPAWN = "respawn"
STAGE_REWORK = "rework"
STAGE_REVIEW = "review"
BRING_UP_STAGES = (STAGE_CLAIM, STAGE_RESPAWN, STAGE_REWORK, STAGE_REVIEW)

# Durable action tokens retain task-class spelling and mark infrastructure failures.
INFRASTRUCTURE_ACTION_TOKEN = "infrastructure"


def infrastructure_action(action: str) -> str:
    """`review-blocked` -> `review-infrastructure-blocked`. Idempotent."""
    if f"-{INFRASTRUCTURE_ACTION_TOKEN}-" in f"-{action}-":
        return action
    suffix = "-blocked"
    if action.endswith(suffix):
        return f"{action[: -len(suffix)]}-{INFRASTRUCTURE_ACTION_TOKEN}{suffix}"
    return f"{action}-{INFRASTRUCTURE_ACTION_TOKEN}"


def bring_up_blocked_action(action: str, failure: BringUpFailure) -> str:
    """The action token a blocked bring-up writes into its transition, per its class."""
    return infrastructure_action(action) if failure.infrastructure else action


def bring_up_terminal_reason(failure: BringUpFailure) -> str:
    """The forward taxonomy reason for a classified bring-up failure."""
    return "infrastructure" if failure.infrastructure else "task_contract"


def bring_up_failure_class(request_id: str) -> str:
    """Read a blocked bring-up's class back off the transition it wrote. The consumer's half."""
    return (
        FAILURE_CLASS_INFRASTRUCTURE
        if f"-{INFRASTRUCTURE_ACTION_TOKEN}-blocked" in request_id
        else FAILURE_CLASS_TASK
    )


@dataclass(frozen=True)
class BringUpFailure:
    """What one bring-up that produced no head was, in the words both paths use for it."""

    failure_class: str
    cause: str
    stage: str
    role: str
    attempt_id: str
    detail: str

    @property
    def infrastructure(self) -> bool:
        return self.failure_class == FAILURE_CLASS_INFRASTRUCTURE

    def evidence(self) -> dict[str, Any]:
        """What did not come up, at which step, under which attempt."""
        evidence: dict[str, Any] = {
            "failure_class": self.failure_class,
            "cause": self.cause,
            "stage": self.stage,
            "head": role_label(self.role),
            "attempt_id": self.attempt_id,
            "detail": self.detail,
        }
        return evidence

    def clause(self) -> str:
        """The one sentence that names the class on the card, in the tick, and nowhere differently."""
        if self.failure_class == FAILURE_CLASS_ADMISSION:
            return (
                f"[admission outcome: class={self.failure_class}, cause={self.cause}, "
                f"stage={self.stage}, head={role_label(self.role)}] no head was started, "
                "so this is not a host failure."
            )
        verdict = (
            f"the {role_label(self.role)} head never came up, so this is not a verdict about the card"
            if self.infrastructure
            else f"this card's own {role_label(self.role)} bring-up contract is what failed"
        )
        return (
            f"[bring-up outcome: class={self.failure_class}, cause={self.cause}, "
            f"stage={self.stage}, head={role_label(self.role)}, "
            f"attempt={self.attempt_id or '(unknown)'}] {verdict}."
        )

    def outcome_fields(self, reason: str) -> dict[str, Any]:
        """The tick outcome's half of the same statement, carrying the card's exact reason string."""
        return {
            "failure_class": self.failure_class,
            "failure_cause": self.cause,
            "failure_reason": reason,
            "bring_up": self.evidence(),
        }


def classify_bring_up_failure(
    exc: BaseException | None,
    record: DispatcherRecord | None,
    role: str,
    *,
    stage: str,
    attempt_id: str,
    detail: str = "",
) -> BringUpFailure:
    """Classify bring-up failures with a typed cause and evidence.

An exception or an explicit preflight refusal supplies the details. Accept
only recognized HostError bring_up_cause values; unknown causes are not proof.
    """
    cause = ""
    if isinstance(exc, HeadLaunchAborted):
        cause = CAUSE_LAUNCH_ABORTED
    else:
        declared = str(getattr(exc, "bring_up_cause", "") or "")
        cause = declared if declared in BRING_UP_CAUSE_CLASSES else CAUSE_HOST_UNAVAILABLE
    text = detail or (scrub_host_output(str(exc)) if exc is not None else "")
    return BringUpFailure(
        failure_class=BRING_UP_CAUSE_CLASSES[cause],
        cause=cause,
        stage=stage if stage in BRING_UP_STAGES else STAGE_CLAIM,
        role=role,
        attempt_id=attempt_id,
        detail=text,
    )


def bring_up_blocked_reason(
    default: str,
    exc: Exception,
    *,
    failure: BringUpFailure,
) -> str:
    """The card's Blocked reason for a bring-up that will not be retried.

    `failure` is required rather than derived here, so no caller can write this reason without
    having classified the failure first: the class the observer reads on the card is the same
    object the tick outcome and the request id are built from.
    """
    return f"{default}: {scrub_host_output(str(exc))}\n{failure.clause()}"


def head_stop_unconfirmed(*, step: str, ref: str, attempt_id: str, role: str, reason: str) -> dict[str, Any]:
    """The outcome of a tick that refused to launch because a stop was not confirmed.

    A head the host would not promise is gone may still be editing the checkout, so nothing takes
    its place this tick.
    """
    return {
        "status": "degraded",
        "step": step,
        "pilot_ref": ref,
        "attempt_id": attempt_id,
        "action": f"{role}-stop-unconfirmed",
        "reason": f"the previous head could not be confirmed stopped: {reason}",
    }


def launch_intent_unwritable(
    *, step: str, ref: str, attempt_id: str, role: str, reason: str
) -> dict[str, Any]:
    """The outcome of a tick that refused to launch because it could not record the launch."""
    return {
        "status": "degraded",
        "step": step,
        "pilot_ref": ref,
        "attempt_id": attempt_id,
        "action": f"{role}-launch-intent-unwritable",
        "reason": f"launch intent could not be persisted: {reason}",
    }


def launch_intent_liveness(intent: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
    """Whether the head this intent was written for is running, and on what evidence.

    `pid_known: False` means the heartbeat is not readable yet, which a head launched seconds ago has
    not written, so it reads as alive until the grace window has passed.
    """
    now = time.time() if now is None else now
    typed = LaunchIntent.from_json(intent)
    pid_file = typed.pid_file if typed is not None else str(intent.get("pid_file") or "")
    expected = intent_heartbeat_identity(intent)
    status = head_process_status(pid_file, expected=expected)
    # Heal only empty-leaf pre-bind heartbeats; never overwrite a foreign leaf.
    record = status.get("record") if isinstance(status.get("record"), dict) else {}
    leaf = str(expected.get("leaf") or "")
    if status.get("state") == "identity-mismatch" and leaf and not str(record.get("leaf") or ""):
        base_expected = {name: str(expected.get(name) or "") for name in ("run_id", "role", "task")}
        bind_head_heartbeat(pid_file, expected=base_expected, leaf=leaf)
        status = head_process_status(pid_file, expected=expected)
    if status.get("state") == "identity-mismatch":
        return {"alive": False, "pid_known": True, "identity_mismatch": True}
    if status.get("known"):
        return {"alive": bool(status.get("match")), "pid_known": True}
    started_at = typed.at if typed is not None else float(intent.get("at") or 0.0)
    if started_at and now - started_at <= initial_output_stall_seconds():
        return {"alive": True, "pid_known": False}
    return {"alive": False, "pid_known": False}


def resolve_launch_intent(
    runtime: Any,
    task: dict[str, Any],
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    """Settle an unresolved bring-up before the tick acts on this card.

    Returns the tick's outcome when the intent takes the tick, and None when there is nothing pending
    or the launch left nothing running, in which case the ordinary path relaunches.
    """
    ref = task["ref"]
    record = records.get(ref)
    if record is None:
        return None
    intent = launch_intent(record)
    if not intent:
        return None
    role = str(intent.get("role"))
    step = "review" if role == REVIEW_ROLE else "advance"
    liveness = launch_intent_liveness(intent)
    if liveness.get("identity_mismatch"):
        # The pid is a living process, but not this launch.  It cannot be signalled or attributed
        # to this HeadRun, and a replacement over the still-owned pane would be just as unsafe.
        return {
            "status": "degraded",
            "step": step,
            "pilot_ref": ref,
            "attempt_id": record.attempt_id,
            "action": f"{role}-heartbeat-identity-mismatch",
            "head": str(intent.get("head") or ""),
            "reason": "launch heartbeat names a live process with a mismatching launch identity",
        }
    if role == REVIEW_ROLE and liveness["alive"]:
        # A reviewer whose production handoff is pending answers to the shared liveness rule before
        # anything below (the heartbeat grace, the retry's backoff, a pending receipt, adoption) can
        # return in its place. Delayed import avoids the dispatcher_review launch-intent cycle.
        from ummanu.dispatch.review import reviewer_pending_liveness

        settled = reviewer_pending_liveness(runtime, task, records, payload, record, intent, step)
        if settled is not None:
            return settled
        intent = launch_intent(record)
    if liveness["alive"] and not liveness["pid_known"]:
        # Leave a starting head alone until its heartbeat grace resolves.
        return {
            "status": "skipped",
            "step": step,
            "pilot_ref": ref,
            "attempt_id": record.attempt_id,
            "action": f"{role}-launch-pending",
            "head": str(intent.get("head") or ""),
            "reason": "a launch intent is still within its grace window and has written no pid yet",
        }
    if not liveness["alive"]:
        failure = stop_launch_intent(runtime, record, intent, role)
        if failure is None:
            keep_reserved_round(runtime, record, intent)
        _persist_quietly(runtime, payload, records)
        if failure is not None:
            return head_stop_unconfirmed(
                step=step,
                ref=ref,
                attempt_id=record.attempt_id,
                role=role,
                reason=failure,
            )
        return None
    if role == REVIEW_ROLE and undelivered_launch_delivery(intent):
        # Delayed import avoids the dispatcher_review launch-intent cycle.
        from ummanu.dispatch.review import retry_busy_reviewer_launch_delivery

        deferred = retry_busy_reviewer_launch_delivery(runtime, task, records, payload, record, intent, step)
        if deferred is not None:
            return deferred
        intent = launch_intent(record)
    return _adopt_launch_intent(runtime, task, records, payload, record, intent, role, step)


def _retained_worker_kept_for_review(runtime: Any, record: DispatcherRecord, intent: dict[str, Any]) -> bool:
    """Whether an adopted reviewer keeps its previously retained worker.

Only a completed production handoff qualifies, and only when the worker is
confirmed stopped or still suspended. An inconclusive answer is handled by
the caller's stop path.
    """
    if not launch_delivery(intent).get("handoff") or not record.worker_continuation.retained:
        return False
    try:
        if not runtime.host.worker_retained_vanished(record):
            runtime.host.confirm_worker_retained(record)
    except HostError:
        return False
    return True


def keep_reserved_round(runtime: Any, record: DispatcherRecord, intent: dict[str, Any]) -> None:
    """Apply the round reserved by an abandoned worker launch intent.

A failed rework still consumes its reserved round; do not restart rework
under the round whose red verdict initiated it.
    """
    if str(intent.get("role") or "") == REVIEW_ROLE:
        return
    reserved = int(intent.get("round") or 0)
    if not intent.get("opens_round") or not reserved or record.attempt_round == reserved:
        return
    runtime.open_worker_round(record, round_number=reserved)
    # The state the rework bring-up would have written. The head is not up, so the wait watchdog
    # owns the relaunch from here, and it does it inside the round opened above.
    record.state = "claimed"


def stop_launch_intent(
    runtime: Any, record: DispatcherRecord, intent: dict[str, Any], role: str
) -> str | None:
    """Stop a launch's residual head and remove its intent if confirmed.

Retain identity after a refused stop, avoiding a second process in the same
workspace. Fall back from pane/heartbeat evidence to the owned workspace.
    """
    _remember_launch_identity(record, intent, role)
    handle = getattr(record, "review_handle" if role == REVIEW_ROLE else "handle", "")
    leaf = getattr(record, "review_leaf" if role == REVIEW_ROLE else "worker_leaf", "")
    pid_file = getattr(record, role_field(role, "pid_file"), "")
    # Workspace alone proves no head; a readable heartbeat is required.
    status = head_process_status(pid_file, expected=intent_heartbeat_identity(intent))
    if status.get("state") == "identity-mismatch":
        return "launch heartbeat names a live process with a mismatching launch identity"
    named = bool(handle or leaf) or bool(status.get("known"))
    try:
        if role == REVIEW_ROLE and named:
            # Imported here rather than at module scope: `dispatcher_review` writes the reviewer's
            # intent through this module, and a top-level import either way would be a cycle.
            from ummanu.dispatch.review import end_review_pane

            end_review_pane(runtime.host, record, STOPPED_BY_LAUNCH_RECOVERY)
        elif named:
            runtime.host.stop_head(record, WORKER_ROLE, STOPPED_BY_LAUNCH_RECOVERY)
            forget_role_head(record, WORKER_ROLE)
        else:
            # A legacy launch without a role identity can only be settled by its workspace.  A
            # worker or reviewer that has a leaf is always stopped through that exact pane above.
            runtime.host.stop_workspace(record)
            forget_role_head(record, WORKER_ROLE)
            forget_role_head(record, REVIEW_ROLE)
    except HostError as exc:
        return f"{type(exc).__name__}: {exc}"
    record.launch_intent = {}
    return None


def _remember_launch_identity(record: DispatcherRecord, intent: dict[str, Any], role: str) -> None:
    """Put the launch's own identity on the record, so the stop paths can reach its head."""
    pid_file = str(intent.get("pid_file") or "")
    handle = str(intent.get("handle") or "")
    leaf = str(intent.get("leaf") or "")
    # A first claim writes its intent before the record knows where the head runs, so the
    # workspace comes back off the intent here: without it the stop has no worktree to address.
    record.workspace = record.workspace or str(intent.get("workspace") or "")
    if pid_file:
        setattr(record, role_field(role, "pid_file"), pid_file)
    if role == REVIEW_ROLE:
        record.review_handle = record.review_handle or handle
        record.review_leaf = record.review_leaf or leaf
    else:
        record.handle = record.handle or handle
        record.worker_leaf = record.worker_leaf or leaf
    stored_run = intent.get("head_run")
    if not isinstance(stored_run, dict) or not stored_run.get("run_id"):
        # Rebuild the intent's exact HeadRun before a fenced recovery stop.
        run_id = str(intent.get("run_id") or "")
        task = str(intent.get("task") or "")
        task_kind, separator, task_ref = task.partition(":")
        if run_id and separator and task_kind == "card" and task_ref:
            stored_run = head_ops.HeadRun(
                run_id=run_id,
                spec=head_ops.HeadSpec(profile_id=str(intent.get("head") or "unknown"), adapter="unknown"),
                workspace=record.workspace,
                task_ref=head_ops.TaskRef.card(task_ref),
                handle=handle,
                leaf=leaf,
                pid_file=pid_file,
            ).to_json()
    _remember_head_run(record, role, stored_run if isinstance(stored_run, dict) else None)


def _adopt_launch_intent(
    runtime: Any,
    task: dict[str, Any],
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    record: DispatcherRecord,
    intent: dict[str, Any],
    role: str,
    step: str,
) -> dict[str, Any]:
    """Adopt an interrupted launch as this card's head for its role.

Restore the durable HeadRun first, then re-address pane, leaf and heartbeat.
Do not reconstruct identity or overwrite an existing stop initiator.
    """
    ref = task["ref"]
    undelivered = undelivered_launch_delivery(intent)
    if undelivered:
        return refuse_undelivered_launch(
            runtime, payload, records, ref, record, intent, role, step, undelivered
        )
    launched_at = float(intent.get("at") or 0.0) or time.time()
    record.workspace = record.workspace or str(intent.get("workspace") or "")
    handle = str(intent.get("handle") or "")
    leaf = str(intent.get("leaf") or "")
    stored_run = intent.get("head_run")
    if not isinstance(stored_run, dict) or not stored_run.get("run_id"):
        # Recover a pre-pane failure with its intent's fixed run identity.
        run_id = str(intent.get("run_id") or "")
        if run_id:
            stored_run = head_ops.HeadRun(
                run_id=run_id,
                spec=head_ops.HeadSpec(profile_id=str(intent.get("head") or "unknown"), adapter="unknown"),
                workspace=record.workspace,
                task_ref=head_ops.TaskRef.card(ref),
                handle=handle,
                leaf=leaf,
                pid_file=str(intent.get("pid_file") or ""),
            ).to_json()
    _remember_head_run(record, role, stored_run if isinstance(stored_run, dict) else None)
    if role == REVIEW_ROLE:
        # Stop the worker before reviewer adoption; refusal preserves the intent.
        record.review_handle = handle
        record.review_leaf = leaf
        record.review_pid_file = str(intent.get("pid_file") or "")
        # A launch a production handoff finished over several ticks is `start_review` continued,
        # and it ends the way `start_review` ends: a retained worker whose suspension is confirmed
        # stays suspended for the red verdict to continue. Anything less certain is stopped.
        kept = _retained_worker_kept_for_review(runtime, record, intent)
        if not kept:
            try:
                runtime.host.freeze_worker(record)
            except HostError as exc:
                _persist_quietly(runtime, payload, records)
                return head_stop_unconfirmed(
                    step=step,
                    ref=ref,
                    attempt_id=record.attempt_id,
                    role=WORKER_ROLE,
                    reason=f"{type(exc).__name__}: {exc}",
                )
            forget_role_head(record, WORKER_ROLE)
        record.state = "reviewing"
        record.review_started_at = record.review_progress_at = launched_at
        if not record.review_commit:
            # The worker is down and the reviewer writes no commits, so the checkout still sits
            # where the launch pinned it. The merge gate needs that sha to accept the verdict.
            record.review_commit = runtime.host.head_commit(record)
        deferred = _record_adopted_routing(runtime, task, records, payload, record, intent, role, step)
        if deferred is not None:
            return deferred
        if role == WORKER_ROLE:
            attempt_accounting.persist_outcome_round_context(runtime, task, record, phase="worker")
        clear_launch_intent(record)
    else:
        record.state = "claimed"
        reserved = int(intent.get("round") or 0)
        if intent.get("opens_round") and reserved:
            # The launch was a rework: it reserved the next round before calling the host, and the
            # adopted head belongs to that round. Opening it here is what keeps the rework's
            # routing and its verdict apart from the round the red result closed.
            runtime.open_worker_round(record, round_number=reserved)
        # Whatever pane identity the launch got as far as reporting. Usually none: the tick died
        # before the host answered. The heartbeat `clear_launch_intent` keeps is what the freeze,
        # the respawn and the red-verdict bounce then stop this head by.
        record.handle = handle
        record.worker_leaf = leaf
        record.worker_started_at = record.worker_progress_at = launched_at
        deferred = _record_adopted_routing(runtime, task, records, payload, record, intent, role, step)
        if deferred is not None:
            return deferred
        if role == WORKER_ROLE:
            attempt_accounting.persist_outcome_round_context(runtime, task, record, phase="worker")
        clear_launch_intent(record)
    records[ref] = record
    _persist_quietly(runtime, payload, records)
    return {
        "status": "ok",
        "step": step,
        "pilot_ref": ref,
        "attempt_id": record.attempt_id,
        "action": f"{role}-launch-adopted",
        "head": str(intent.get("head") or ""),
        "reason": "a launch intent was left by a tick that did not finish; its head is alive",
    }


def stop_undeliverable_launch(
    runtime: Any,
    payload: dict[str, Any],
    records: dict[str, DispatcherRecord],
    ref: str,
    record: DispatcherRecord,
    intent: dict[str, Any],
    role: str,
    step: str,
    *,
    readiness: str,
    attempts: int,
    reason: str,
) -> dict[str, Any]:
    """The bounded end of a launch that will not be given its pointer: stopped, then made again.

    The head is stopped through its own intent first, and only a confirmed stop takes the intent
    back for the ordinary path to relaunch; an unconfirmed stop keeps it, so no second head opens
    beside the first.
    """
    failure = stop_launch_intent(runtime, record, intent, role)
    if failure is None:
        keep_reserved_round(runtime, record, intent)
    records[ref] = record
    _persist_quietly(runtime, payload, records)
    if failure is not None:
        return head_stop_unconfirmed(step=step, ref=ref, attempt_id=record.attempt_id, role=role, reason=failure)
    return {
        "status": "degraded",
        "step": step,
        "pilot_ref": ref,
        "attempt_id": record.attempt_id,
        "action": f"{role}-launch-undeliverable",
        "head": str(intent.get("head") or ""),
        "readiness": readiness,
        "attempts": attempts,
        "reason": reason,
    }


def refuse_undelivered_launch(
    runtime: Any,
    payload: dict[str, Any],
    records: dict[str, DispatcherRecord],
    ref: str,
    record: DispatcherRecord,
    intent: dict[str, Any],
    role: str,
    step: str,
    undelivered: dict[str, Any],
) -> dict[str, Any]:
    """Do not adopt a head whose prompt delivery was refused or unconfirmed.

No routing claim, reviewer transition, freeze or intent consumption occurs
until delivery is accepted. After LAUNCH_DELIVERY_MAX_ATTEMPTS, stop through
the intent before allowing a later relaunch; retain it when stop is unconfirmed.
    """
    attempts = int(undelivered.get("attempts") or 0)
    state = str(undelivered.get("state") or "")
    head = str(intent.get("head") or "")
    now = time.time()
    if attempts >= LAUNCH_DELIVERY_MAX_ATTEMPTS:
        return stop_undeliverable_launch(
            runtime,
            payload,
            records,
            ref,
            record,
            intent,
            role,
            step,
            readiness=state,
            attempts=attempts,
            reason=(
                f"the {role_label(role)} head never accepted its pointer in {attempts} attempts "
                f"({pane_state_label(state)}); it has been stopped and the launch is made again"
            ),
        )
    next_at = float(undelivered.get("next_at") or 0.0)
    if next_at and now < next_at:
        return {
            "status": "degraded",
            "step": step,
            "pilot_ref": ref,
            "attempt_id": record.attempt_id,
            "action": f"{role}-launch-undelivered",
            "head": head,
            "readiness": state,
            "attempts": attempts,
            "reason": (
                f"the {role_label(role)} launch is alive but its pointer was never accepted "
                f"({pane_state_label(state)}); the next attempt is due in "
                f"{max(0, int(next_at - now))}s"
            ),
        }
    evidence = undelivered.get("evidence")
    delay = defer_launch_delivery(
        record,
        dict(evidence) if isinstance(evidence, dict) else {},
        state=state or READINESS_BLOCKED,
        now=now,
    )
    records[ref] = record
    _persist_quietly(runtime, payload, records)
    return {
        "status": "degraded",
        "step": step,
        "pilot_ref": ref,
        "attempt_id": record.attempt_id,
        "action": f"{role}-launch-undelivered",
        "head": head,
        "readiness": state,
        "attempts": attempts + 1,
        "reason": (
            f"a live {role_label(role)} head is not a delivered pointer: the composer never "
            f"accepted it ({pane_state_label(state)}), so the launch is not adopted as a claim; "
            f"attempt {attempts + 1} of {LAUNCH_DELIVERY_MAX_ATTEMPTS} waits {delay}s"
        ),
    }


def _record_adopted_routing(
    runtime: Any,
    task: dict[str, Any],
    records: dict[str, DispatcherRecord],
    payload: dict[str, Any],
    record: DispatcherRecord,
    intent: dict[str, Any],
    role: str,
    step: str,
) -> dict[str, Any] | None:
    """Record routing for an adopted head, returning an outcome on failure.

Reconstruct the launch's durable routing identity rather than claiming a new
head or discarding a recovered launch.
    """
    ref = task["ref"]
    run = intent.get("run") if isinstance(intent.get("run"), dict) else None
    try:
        if role == REVIEW_ROLE:
            runtime.record_review_routing(task, record, run)
        else:
            runtime.record_worker_routing(task, record, run)
    except Exception as exc:  # noqa: BLE001 — any journal refusal, whatever the plane called it
        records[ref] = record
        _persist_quietly(runtime, payload, records)
        return {
            "status": "degraded",
            "step": step,
            "pilot_ref": ref,
            "attempt_id": record.attempt_id,
            "action": f"{role}-launch-adopt-deferred",
            "head": str(intent.get("head") or ""),
            "reason": f"the adopted head could not be recorded in the routing journal: {exc}",
        }
    return None


def _persist_quietly(runtime: Any, payload: dict[str, Any], records: dict[str, DispatcherRecord]) -> bool:
    """Flush records during a launch/adoption tick.

When the flush fails, the tick's final save remains responsible; avoid
raising a failure after an already running head was admitted.
    """
    try:
        runtime.save_records(payload, records)
    except STORAGE_ERRORS:
        return False
    return True
