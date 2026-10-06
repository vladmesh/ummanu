"""Production dispatcher runtime."""

from __future__ import annotations

from ummanu.dispatch.cleanup import serialized

import time
from pathlib import Path
from typing import Any, cast

from ummanu.board.completion_evidence import has_candidate, is_po_executed, is_wait, review_required
from ummanu.board.sql_audit import SqlTaskAudit
from ummanu.checkpoint import CheckpointPusher, CheckpointWriter
from ummanu.codex_provider_events import (
    CodexProviderSourceError,
)
from ummanu.dispatch import attempt_accounting, release_activation
from ummanu.dispatch.assessment_decision import advance_assessment as _advance_assessment
from ummanu.dispatch.claim import (
    SPRINT_RESERVATION_BLOCKED_ACTION,  # noqa: F401  # Compatibility re-export.
    SPRINT_RESERVATION_RESERVED,  # noqa: F401  # Compatibility re-export.
    SPRINT_RESERVATION_UNVERIFIABLE,  # noqa: F401  # Compatibility re-export.
)
from ummanu.dispatch.claim import (
    claim_ready_task as _claim_ready_task,
)
from ummanu.dispatch.claim import (
    resolve_head as _resolve_claim_head,
)
from ummanu.dispatch.gate_lifecycle import run_gate as _run_gate
from ummanu.dispatch.helpers import (
    _report_adoption_baseline,
    _review_adoption_baseline,
    _spent_report_generations,
    _task_doc_decision,
    _task_doc_protocol_prerequisites,
    _task_doc_report_generation,
    _worker_id,
    scrub_host_output,
)
from ummanu.dispatch.host import (  # noqa: F401  # Compatibility re-exports.
    DESTRUCTIVE_VERDICTS,
    HEAD_STOP_GRACE_SECONDS,
    HEAD_STOP_POLL_SECONDS,
    OBSERVER_REPO_BRANCH,
    OBSERVER_WORKSPACE_DIR,
    CommandHostRuntime,
    DispatcherHeadTransport,
    InstanceCatalog,
    _body_file_instructions,
    _body_file_path,
    _continuation_note,
    _durable_head_run,
    _gate_attestation_for_prompt,
    _head_runtime_name,
    _legacy_worker_branch,
    _record_worker_delivery_evidence,
    _report_nudge_prompt,
    _same_repo,
    _watchdog_kind,
)
from ummanu.dispatch.host import (
    LaunchedHead as LaunchedHead,  # Compatibility re-export.
)
from ummanu.dispatch.launch import (
    REVIEW_ROLE,
    WORKER_ROLE,
)
from ummanu.dispatch.launch import (
    bring_up_blocked_action as _bring_up_blocked_action,
)
from ummanu.dispatch.launch import (
    bring_up_blocked_reason as _bring_up_blocked_reason,
)
from ummanu.dispatch.launch import (
    bring_up_terminal_reason as _bring_up_terminal_reason,
)
from ummanu.dispatch.launch import (
    classify_bring_up_failure as _classify_bring_up_failure,
)
from ummanu.dispatch.launch import (
    forget_role_head as _forget_role_head,
)
from ummanu.dispatch.launch import (
    head_stop_unconfirmed as _head_stop_unconfirmed,
)
from ummanu.dispatch.launch import (
    launch_pid_file as _launch_pid_file,
)
from ummanu.dispatch.launch import (
    merge_launch_head_run as _merge_launch_head_run,
)
from ummanu.dispatch.launch import (
    resolve_launch_intent as _resolve_launch_intent,
)
from ummanu.dispatch.pause import ProductionPause
from ummanu.dispatch.pause_ops import (
    pause as _pause_pipeline,
)
from ummanu.dispatch.pause_ops import (
    pause_status as _pause_status,
)
from ummanu.dispatch.pause_ops import (
    resume as _resume_pipeline,
)
from ummanu.dispatch.po_cards import ServicePoChannel
from ummanu.dispatch.po_cards import advance_po_card as _advance_po_card
from ummanu.dispatch.production import (
    ProductionState,
)
from ummanu.dispatch.production import (
    production_observe as _production_observe,
)
from ummanu.dispatch.production import (
    production_probe as _production_probe,
)
from ummanu.dispatch.production import (
    production_run as _production_run,
)
from ummanu.dispatch.production import (
    production_tick as _production_tick,
)
from ummanu.dispatch.review import (
    end_review_pane as _end_review_pane,
)
from ummanu.dispatch.review import (
    recover_review_launch as _recover_review_launch,
)
from ummanu.dispatch.review import (
    start_review as _start_review,
)
from ummanu.dispatch.review_verdict import (
    advance_review_verdict as _advance_review_verdict,
)
from ummanu.dispatch.review_verdict import (
    park_green_verdict as _park_green_verdict,
)
from ummanu.dispatch.state import (
    DispatcherRecord,
    OutcomeTerminalPath,
    PersistedHeadRun,
    PersistedLaunchIntent,
    PersistedRoutingHeadSnapshot,
    now_rfc3339,
)
from ummanu.dispatch.state import (
    attempt_request_id as _attempt_request_id,
)
from ummanu.dispatch.state import (
    claim_mismatch as _claim_mismatch,
)
from ummanu.dispatch.state import (
    outcome_terminal_path as _outcome_terminal_path,
)
from ummanu.dispatch.types import (
    STOPPED_BY_DISPATCHER,  # noqa: F401  # Public compatibility re-export.
    STOPPED_BY_OPERATOR,  # noqa: F401  # Public compatibility re-export.
    STOPPED_BY_RECONCILIATION,  # noqa: F401  # Public compatibility re-export.
    STOPPED_BY_REPLACEMENT,
    STOPPED_BY_REVIEW_FREEZE,  # noqa: F401  # Public compatibility re-export.
    STOPPED_BY_REVIEW_VERDICT,  # noqa: F401  # Public compatibility re-export.
    STOPPED_BY_WATCHDOG,  # noqa: F401  # Public compatibility re-export.
    HostError,
)
from ummanu.dispatch.types import DispatcherError as DispatcherError
from ummanu.dispatch.wait_cards import advance_wait_card as _advance_wait_card
from ummanu.dispatch.wait_vitality import wait_watchdog as _wait_watchdog
from ummanu.dispatch.watchdog import (
    head_process_status as _head_process_status,
)
from ummanu.dispatch.watchdog import (
    head_run_process_status as _head_run_process_status,
)
from ummanu.dispatch.watchdog import (
    heartbeat_is_live_match as _heartbeat_is_live_match,
)
from ummanu.dispatch.worker_continuation import (
    complete_red_transition as _complete_red_transition,
)
from ummanu.dispatch.worker_continuation import (
    recover_worker_continuation as _recover_worker_continuation,
)
from ummanu.dispatch.worker_launch import (
    launch_worker_after_claim as _launch_worker_after_claim,
)
from ummanu.dispatch.worker_launch import (
    resolve_headless_worker as _resolve_headless_worker,
)
from ummanu.dispatch.worker_report import (
    deliver_worker_comments as _deliver_worker_comments,
)
from ummanu.dispatch.worker_report import (
    handle_worker_report as _handle_worker_report,
)
from ummanu.dispatch.worker_report import (
    worker_report_marker as _worker_report_marker,
)
from ummanu.head_health import (
    HeadChoice,
    HeadHealth,
    HeadReadiness,
)
from ummanu.routing_journal import (
    MODEL_UNKNOWN,
    REVIEWER,
    WORKER,
    HeadRun,
)
from ummanu.routing_journal import (
    attempts as _routing_attempts,
)
from ummanu.routing_journal import (
    routing_head_snapshot_from_launch as _routing_head_snapshot_from_launch,
)
from ummanu.routing_journal import (
    routing_payload as _routing_payload,
)
from ummanu.routing_journal import (
    run_key as _run_key,
)
from ummanu.runtime import head as head_ops
from ummanu.runtime.codex_preflight import (
    CodexFanoutRecordingError,
)
from ummanu.runtime.head import (
    HeadSpec,
)
from ummanu.sprints import SprintReader, budget_thresholds
from ummanu.tasks import (
    TaskReader,
    TaskWriter,
)


class DispatcherRuntime:
    def __init__(
        self,
        reader: TaskReader,
        writer: TaskWriter,
        audit: SqlTaskAudit,
        data_dir: Path,
        catalog: InstanceCatalog,
        host: CommandHostRuntime,
        *,
        owner: str = "ummanu-dispatcher",
        production_state: ProductionState | None = None,
        pause: ProductionPause | None = None,
        checkpoint: CheckpointWriter | None = None,
        checkpoint_push: CheckpointPusher | None = None,
        sprints: Any | None = None,
        po: Any | None = None,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.audit = audit
        self.data_dir = Path(data_dir)
        self.production_state = production_state or ProductionState(data_dir)
        self.pause = pause or ProductionPause(data_dir)
        self.catalog = catalog
        self.host = host
        from ummanu.dispatch.cleanup import CleanupOwner
        self.cleanup = CleanupOwner(self)
        if isinstance(host, CommandHostRuntime):
            host.cleanup_owner = self.cleanup
        self.owner = owner
        self.checkpoint = checkpoint
        self.checkpoint_push = checkpoint_push
        self.head_health = HeadHealth(catalog, data_dir)
        # Sprint entities live on their own board, so they need their own reader, not the card one.
        instance = getattr(catalog, "instance", {})
        limits = budget_thresholds(instance if isinstance(instance, dict) else None)
        self.sprints = (
            sprints
            if sprints is not None
            else SprintReader(reader.client, data_dir=self.data_dir, thresholds=limits)
        )
        # The PO service and its store, for the decision and operation cards the PO executes.
        self.po = (
            po if po is not None else ServicePoChannel(self.data_dir, getattr(catalog, "instance_dir", None))
        )

    def head_readiness(self, head: str) -> HeadReadiness:
        return self.head_health.check(head)

    def resolve_head(self, preferred: str) -> HeadChoice:
        """Compatibility entry point; claim-owned resolution lives in dispatch.claim."""
        return _resolve_claim_head(self, preferred)

    def _require_head_ready(self, head: str) -> None:
        readiness = self.head_readiness(head)
        if not readiness.launch_allowed:
            raise HostError(f"head resource {readiness.resource} is {readiness.status}: {readiness.reason}")

    def bind_codex_provider_ingress(
        self,
        record: DispatcherRecord,
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        *,
        role: str,
        reference: str,
    ) -> None:
        """Give a persisted Codex HeadRun its only provider-event ingress."""
        stored = record.worker_head_run if role == WORKER_ROLE else record.review_head_run
        intent = dict(record.launch_intent or {})
        if not isinstance(stored, dict) or not stored.get("run_id"):
            candidate = intent.get("head_run")
            stored = cast(PersistedHeadRun, candidate if isinstance(candidate, dict) else {})
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
                raise HostError("provider event writer was handed another HeadRun")
            updated_json = updated.to_json()
            if role == WORKER_ROLE:
                existing = record.worker_head_run
                if isinstance(existing, dict) and existing.get("run_id"):
                    updated_json = _merge_launch_head_run(existing, updated_json)
                record.worker_head_run = cast(PersistedHeadRun, updated_json)
                record.workspace = updated.workspace or record.workspace
                record.handle = updated.handle or record.handle
                record.worker_leaf = updated.leaf or record.worker_leaf
                record.worker_pid_file = updated.pid_file or record.worker_pid_file
            else:
                existing = record.review_head_run
                if isinstance(existing, dict) and existing.get("run_id"):
                    updated_json = _merge_launch_head_run(existing, updated_json)
                record.review_head_run = cast(PersistedHeadRun, updated_json)
                record.workspace = updated.workspace or record.workspace
                record.review_handle = updated.handle or record.review_handle
                record.review_leaf = updated.leaf or record.review_leaf
                record.review_pid_file = updated.pid_file or record.review_pid_file
            current_intent = dict(record.launch_intent or {})
            intent_run = current_intent.get("head_run")
            if isinstance(intent_run, dict) and str(intent_run.get("run_id") or "") == updated.run_id:
                current_intent["head_run"] = _merge_launch_head_run(intent_run, updated_json)
                record.launch_intent = cast(PersistedLaunchIntent, current_intent)
            records[reference] = record
            self.save_records(payload, records)

        def stop(updated: head_ops.HeadRun, reason: str) -> None:
            # The head operation re-reads the heartbeat identity before signalling. A mismatch is
            # swallowed here: the block still records the unknown source, no foreign process is hit.
            try:
                with self.host.committing(lambda: self.save_records(payload, records)):
                    self.host.stop_head(record, "worker" if role == WORKER_ROLE else "review")
            except HostError:
                return

        def block(evidence: dict[str, Any]) -> None:
            attempt_accounting.terminal_effect(self, 
                {"ref": reference},
                record,
                target="blocked",
                reason=(
                    "Codex provider fan-out policy blocked this head: "
                    f"{evidence.get('state') or 'unknown'}; {evidence.get('reason') or 'provider event observed'}"
                ),
                request_id=_attempt_request_id(
                    record.attempt_id, "codex-provider-event-blocked", reference, f"{role}-{run.run_id}"
                ),
                terminal_state="blocked",
                disposition="blocked",
                blocked_reason="provider",
            )

        self.host.configure_codex_provider_ingress(run, persist=persist, stop=stop, block=block)

    def poll_codex_provider_ingress(
        self,
        record: DispatcherRecord,
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        *,
        reference: str,
    ) -> dict[str, Any] | None:
        """Refresh advisory fan-out telemetry for recovered worker/reviewer runs."""
        for role, stored in (
            (WORKER_ROLE, record.worker_head_run),
            (REVIEW_ROLE, record.review_head_run),
        ):
            if not isinstance(stored, dict) or not stored.get("run_id"):
                continue
            try:
                run = head_ops.HeadRun.from_json(stored)
            except (head_ops.HeadRunError, head_ops.TaskRefError):
                continue
            if run.spec.adapter != "codex" or not isinstance(run.fanout_policy.get("provider_source"), dict):
                continue
            self.bind_codex_provider_ingress(record, records, payload, role=role, reference=reference)
            try:
                self.host.poll_codex_provider_ingress(run)
            except (CodexProviderSourceError, CodexFanoutRecordingError) as exc:
                return {
                    "status": "blocked",
                    "step": "codex-provider-event",
                    "pilot_ref": reference,
                    "attempt_id": record.attempt_id,
                    "policy_evidence": {"kind": "codex_provider_fanout", "state": "unknown"},
                    "reason": str(exc),
                }
        return None

    def pause_pipeline(
        self,
        *,
        mode: str,
        actor: str,
        reason: str,
        exclude_workspaces: list[str] | None = None,
    ) -> dict[str, Any]:
        return _pause_pipeline(
            self, mode=mode, actor=actor, reason=reason, exclude_workspaces=exclude_workspaces
        )

    def resume_pipeline(self, *, actor: str) -> dict[str, Any]:
        return _resume_pipeline(self, actor=actor)

    def pause_status(self) -> dict[str, Any]:
        return _pause_status(self)

    def production_observe(self) -> dict[str, Any]:
        return _production_observe(self)

    def production_tick(self) -> dict[str, Any]:
        return _production_tick(self)

    def production_probe(self) -> dict[str, Any]:
        return _production_probe(self)

    def production_run(
        self,
        *,
        interval_seconds: float,
        max_interval_seconds: float,
        max_ticks: int | None = None,
    ) -> dict[str, Any]:
        return _production_run(
            self,
            interval_seconds=interval_seconds,
            max_interval_seconds=max_interval_seconds,
            max_ticks=max_ticks,
        )

    def _tick_task(
        self,
        task: dict[str, Any],
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        attempt_id: str,
    ) -> dict[str, Any]:
        ref = task["ref"]
        record = records.get(ref)
        if record is not None and record.activation_recovery is not None:
            return release_activation.resume_refused_activation(
                self, task, record, records, payload, attempt_id
            )
        if is_po_executed(task):
            # A decision/operation card has no head, workspace or launch intent to settle: the PO
            # service executes it, and the dispatcher only submits it and watches the turn.
            return _advance_po_card(self, task, records, payload, attempt_id)
        if is_wait(task):
            # A wait card has no head either: the dispatcher observes its target and delivers its
            # outcome itself (dispatch/wait_cards.py).
            return _advance_wait_card(self, task, records, payload, attempt_id)
        # Staged usage obligations are deliberately not settled here: a card can finish its last
        # phase and leave `ACTIVE_STATES` in the same tick, so no per-card pass can be the site that
        # guarantees publication. `publish_pending_attempt_usage` owns that, over the whole pending
        # set, at the top of the production tick.
        #
        # A launch intent can outlive its tick. Re-establish the exact provider source before
        # adoption reads a heartbeat, not after a mismatched session was attributed to this card.
        record = records.get(ref)
        if record is not None:
            fanout = self.poll_codex_provider_ingress(record, records, payload, reference=ref)
            if fanout is not None:
                return fanout
        # A record carrying a launch intent is a bring-up whose tick did not live to record its
        # outcome. It is settled before anything else: until it is, neither "this card has a head"
        # nor "this card is headless" is known, and the wrong answer gives one workspace two heads.
        pending_launch = _resolve_launch_intent(self, task, records, payload)
        if pending_launch is not None:
            return pending_launch
        if task["state"] == "ready":
            resume_workspaces = payload.get("resume_workspaces")
            resume_workspace = isinstance(resume_workspaces, dict) and ref in resume_workspaces
            return _claim_ready_task(
                self,
                task,
                records,
                payload,
                attempt_id,
                resume_workspace=resume_workspace,
            )
        if task["state"] == "in_progress":
            return self._advance_worker(task, records, payload, attempt_id)
        if task["state"] == "validate":
            return self._advance_review(task, records, payload, attempt_id)
        if task["state"] == "assessment":
            return _advance_assessment(self, task, records, payload, attempt_id)
        records.pop(ref, None)
        return {
            "status": "ok",
            "step": "tick",
            "action": "terminal-state",
            "state": task["state"],
            "pilot_ref": ref,
            "attempt_id": attempt_id,
        }

    def _claim(
        self,
        task: dict[str, Any],
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        attempt_id: str,
        *,
        resume_workspace: bool = False,
    ) -> dict[str, Any]:
        """Compatibility entry point; production claim ownership lives in dispatch.claim."""
        return _claim_ready_task(
            self,
            task,
            records,
            payload,
            attempt_id,
            resume_workspace=resume_workspace,
        )

    def _end_review_pane_confirmed(
        self,
        record: DispatcherRecord,
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        ref: str,
        *,
        step: str,
        attempt_id: str,
        initiator: str,
    ) -> dict[str, Any] | None:
        """End the reviewer before a replacement head opens. Returns the tick's outcome on refusal."""
        try:
            _end_review_pane(self.host, record, initiator)
        except HostError as exc:
            return _head_stop_unconfirmed(
                step=step,
                ref=ref,
                attempt_id=record.attempt_id or attempt_id,
                role="review",
                reason=scrub_host_output(str(exc)),
            )
        return None

    def _stop_worker_confirmed(
        self,
        record: DispatcherRecord,
        ref: str,
        *,
        step: str,
        attempt_id: str,
    ) -> dict[str, Any] | None:
        """Stop this card's worker head before a replacement opens, or answer with the refusal."""
        try:
            if record.handle or record.worker_leaf or record.worker_pid_file:
                self.host.stop_head(record, "worker", STOPPED_BY_REPLACEMENT)
            else:
                # A preempted head can lose its own identity with a dispatcher crash while the
                # workspace is still known. An unnamed writer is ambiguity, never evidence.
                self.host.stop_workspace(record)
        except HostError as exc:
            return _head_stop_unconfirmed(
                step=step,
                ref=ref,
                attempt_id=record.attempt_id or attempt_id,
                role=WORKER_ROLE,
                reason=scrub_host_output(str(exc)),
            )
        _forget_role_head(record, WORKER_ROLE)
        # The session is gone; a red transition already opened over it is not, and is not dropped.
        record.worker_continuation.drop_session()
        return None

    def _advance_worker(
        self,
        task: dict[str, Any],
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        attempt_id: str,
    ) -> dict[str, Any]:
        ref = task["ref"]
        record = records.get(ref)
        if record is None:
            try:
                record = self._adopt(task, attempt_id)
            except HostError as exc:
                return self._block_unresumable(task, records, payload, attempt_id, "advance", exc)
            records[ref] = record
            if record.worker_head_run:
                # A lost record can recover the live worker only from that worker's own launch
                # identity, already bound and re-checked by `_adopt`: this is a continuation of its
                # HeadRun, so a report written before the record was lost still advances the card here.
                record.state = "claimed"
            else:
                current_claim = _attempt_request_id(attempt_id, "claim", ref)
                if self.audit.committed_event(current_claim) is not None:
                    mismatch = _claim_mismatch(task, record.worker, record.head, record.review_head)
                    if not mismatch:
                        record.state = "claim_verified"
                        self.save_records(payload, records)
                        return _launch_worker_after_claim(self, task, record, records, payload)
        if record.worker_continuation.red_transition_pending:
            # An open red transition outranks everything else. The board move may or may not have
            # committed before its tick died, so it is finished against the board as it is now.
            return _complete_red_transition(self, task, record, records, payload, attempt_id, ref=ref)
        if record.state == "claim_verified":
            return _launch_worker_after_claim(self, task, record, records, payload)
        marker = _worker_report_marker(self, task, record, records, payload, attempt_id)
        recovered = _recover_worker_continuation(
            self, task, record, records, payload, attempt_id, marker=marker
        )
        if recovered is not None:
            return recovered
        reported = _handle_worker_report(
            self, task, record, records, payload, attempt_id, marker=marker
        )
        if reported is not None:
            return reported
        # Before any wait: a card cannot wait for a report from a worker no record can name. The
        # watchdog below observes a head; this decides whether there is one to observe at all.
        headless = _resolve_headless_worker(self, task, record, records, payload, attempt_id)
        if headless is not None:
            return headless
        # A comment that landed mid-round reaches the live worker now, not at the next round.
        commented = _deliver_worker_comments(self, task, record, records, payload, attempt_id)
        if commented is not None:
            return commented
        watchdog = _wait_watchdog(self, task, record, records, payload, attempt_id, kind="worker")
        if watchdog is not None:
            return watchdog
        return {
            "status": "ok",
            "step": "advance",
            "pilot_ref": ref,
            "attempt_id": attempt_id,
            "action": "waiting-worker-report",
        }

    def _advance_review(
        self,
        task: dict[str, Any],
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        attempt_id: str,
    ) -> dict[str, Any]:
        ref = task["ref"]
        record = records.get(ref)
        if record is None:
            try:
                record = self._adopt(task, attempt_id)
            except HostError as exc:
                return self._block_unresumable(task, records, payload, attempt_id, "review", exc)
            records[ref] = record
        verdict = _advance_review_verdict(self, task, record, records, payload, attempt_id)
        if verdict is not None:
            return verdict
        # Mechanical gate: a fresh report clears the cheap CI/local gate before the expensive
        # reviewer is spawned. A review already in flight cleared the gate when it launched.
        # A research/infra card has no candidate, so it has no mechanical gate at all.
        if (
            has_candidate(task)
            and record.state not in ("review_starting", "reviewing")
            and record.gate_state != "green"
        ):
            gated = _run_gate(self, task, record, records, payload, attempt_id)
            if gated is not None:
                return gated
        if not review_required(task) and record.state not in ("review_starting", "reviewing"):
            # `review: skipped`: no reviewer for any kind. The accepted report takes the path a green
            # verdict takes, which for a code card still re-reads the gate and merges on release.
            return _park_green_verdict(self, task, record, records, payload, attempt_id, reviewed=False)
        if record.state == "review_starting":
            return _recover_review_launch(self, task, records, record, attempt_id, payload=payload)
        if record.state != "reviewing":
            if record.worker_continuation.retained and not self.host.worker_retained_alive(record):
                # The record remembers a suspended worker the host cannot confirm is frozen.
                # Ambiguous liveness is never permission to leave it beside the reviewer, so the
                # confirmed stop runs before the reviewer launch intent is written.
                unconfirmed = self._stop_worker_confirmed(record, ref, step="review", attempt_id=attempt_id)
                if unconfirmed is not None:
                    return unconfirmed
                records[ref] = record
                self.save_records(payload, records)
            launch_request = _review_launch_request_id(ref, record.review_baseline)
            if self.audit.committed_event(launch_request) is not None:
                record.state = "review_starting"
                return _recover_review_launch(self, task, records, record, attempt_id, payload=payload)
            attempt_accounting.persist_outcome_round_context(self, task, record, phase="review")
            self.writer.comment(
                role="dispatcher",
                actor=self.owner,
                reference=ref,
                body=f"Dispatcher review launch requested for {ref}, review baseline {record.review_baseline}.",
                request_id=launch_request,
            )
            record.state = "review_starting"
            return _start_review(
                self, task, records, record, attempt_id, action="review-started", payload=payload
            )
        watchdog = _wait_watchdog(self, task, record, records, payload, attempt_id, kind="review")
        if watchdog is not None:
            return watchdog
        return {
            "status": "ok",
            "step": "review",
            "pilot_ref": ref,
            "attempt_id": attempt_id,
            "action": "waiting-review-verdict",
        }








    def _block_unresumable(
        self,
        task: dict[str, Any],
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        attempt_id: str,
        step: str,
        error: Exception,
    ) -> dict[str, Any]:
        """A claimed card the dispatcher cannot pick back up on the head it was claimed with."""
        ref = task["ref"]
        record = records.get(ref)
        if record is not None and record.attempt_id == attempt_id:
            attempt_accounting.terminal_effect(self, 
                task,
                record,
                target="blocked",
                reason=f"claimed head is unavailable: {scrub_host_output(str(error))}",
                request_id=_attempt_request_id(attempt_id, "adopt-head-blocked", ref),
                terminal_state="blocked",
                disposition="blocked",
                blocked_reason="other",
            )
        else:
            # A lost record that cannot be adopted has no durable round context.
            # The lifecycle effect still wins, but v1 cannot manufacture its key.
            attempt_accounting.terminal_effect(self, 
                task,
                DispatcherRecord(
                    worker="",
                    workspace="",
                    handle="",
                    head="",
                    review_head="",
                    attempt_id="",
                    comment_baseline=0,
                    review_baseline=0,
                    state="",
                    claimed_at=0.0,
                ),
                target="blocked",
                reason=f"claimed head is unavailable: {scrub_host_output(str(error))}",
                request_id=_attempt_request_id(attempt_id, "adopt-head-blocked", ref),
                terminal_state="blocked",
                disposition="blocked",
                blocked_reason="other",
            )
        records.pop(ref, None)
        self.save_records(payload, records)
        return {
            "status": "blocked",
            "step": step,
            "pilot_ref": ref,
            "attempt_id": attempt_id,
            "reason": "claimed head is unavailable",
        }

    def _block_failed_worker_restart(
        self,
        *,
        ref: str,
        record: DispatcherRecord,
        records: dict[str, DispatcherRecord],
        payload: dict[str, Any],
        attempt_id: str,
        step: str,
        stage: str,
        reason: str,
        action: str,
        request_suffix: str = "",
        error: Exception,
    ) -> dict[str, Any]:
        """Block a failed rework launch while retaining the workspace's resume provenance."""
        failure = _classify_bring_up_failure(
            error, record, WORKER_ROLE, stage=stage, attempt_id=record.attempt_id or attempt_id
        )
        blocked_reason = _bring_up_blocked_reason(reason, error, failure=failure)
        attempt_accounting.terminal_effect(self, 
            {"ref": ref},
            record,
            target="blocked",
            reason=blocked_reason,
            request_id=_attempt_request_id(
                record.attempt_id or attempt_id,
                _bring_up_blocked_action(action, failure),
                ref,
                request_suffix,
            ),
            terminal_state="blocked",
            disposition="blocked",
            blocked_reason=_bring_up_terminal_reason(failure),
        )
        resume_workspaces = payload.setdefault("resume_workspaces", {})
        if isinstance(resume_workspaces, dict):
            resume_workspaces[ref] = record.attempt_id or attempt_id
        records.pop(ref, None)
        self.save_records(payload, records)
        return {
            "status": "blocked",
            "step": step,
            "pilot_ref": ref,
            "reason": reason,
            **failure.outcome_fields(blocked_reason),
        }







    def head_run_snapshot(
        self,
        task: dict[str, Any],
        *,
        role: str,
        head: str = "",
        workspace: str = "",
        failover: bool = False,
    ) -> HeadRun:
        """The typed launch snapshot for a head with no launcher record, or a marked minimal one."""
        try:
            return self.catalog.head_run(
                task, role=role, head=head, workspace=workspace, failover=failover
            )
        except (HostError, AttributeError, KeyError, TypeError):
            return HeadRun(role=role, head=str(head), adapter="unknown", model_source=MODEL_UNKNOWN)

    def _journal_round(self, ref: str) -> int:
        """The last worker round the journal holds for this card. Survives a lost dispatcher record,
        a restore, and a card that went back to Ready and was claimed again."""
        history = _routing_attempts(self.audit.events(ref, kind="routing"))
        return history[-1].attempt if history else 0

    def open_worker_round(self, record: DispatcherRecord, *, round_number: int = 0) -> None:
        """Start the card's next worker round: stamp its number and drop the previous round's heads."""
        record.attempt_round = round_number or (record.attempt_round + 1)
        record.outcome_terminal_path = OutcomeTerminalPath.NO_ACCEPTED_REPORT
        record.worker_run = cast(PersistedRoutingHeadSnapshot, {})
        record.review_run = cast(PersistedRoutingHeadSnapshot, {})

    def record_worker_routing(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        run: HeadRun | dict[str, Any] | None = None,
    ) -> None:
        """Record the worker head this bring-up just put up, as launched."""
        ref = task["ref"]
        if not record.attempt_round:
            record.attempt_round = self._journal_round(ref) + 1
        snapshot = run or self.head_run_snapshot(
            task,
            role="worker",
            head=record.head,
            workspace=record.workspace,
            failover=bool(record.preferred_head),
        )
        snapshot = _routing_head_snapshot_from_launch(snapshot, lifecycle_run=record.worker_head_run)
        if record.worker_run and _run_key(record.worker_run) == _run_key(snapshot):
            snapshot = record.worker_run.snapshot or snapshot
        record.worker_run = cast(PersistedRoutingHeadSnapshot, snapshot)
        self._record_routing(ref, record, phase="worker", heads=[record.worker_run])

    def record_review_routing(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        run: HeadRun | dict[str, Any] | None = None,
    ) -> None:
        """Record the reviewer head this bring-up just put up, as launched."""
        ref = task["ref"]
        if not record.attempt_round:
            record.attempt_round = self._journal_round(ref) + 1
        snapshot = run or self.head_run_snapshot(
            task,
            role="reviewer",
            head=record.review_head,
            workspace=record.workspace,
            failover=bool(record.preferred_review_head),
        )
        snapshot = _routing_head_snapshot_from_launch(snapshot, lifecycle_run=record.review_head_run)
        if record.review_run and _run_key(record.review_run) == _run_key(snapshot):
            snapshot = record.review_run.snapshot or snapshot
        record.review_run = cast(PersistedRoutingHeadSnapshot, snapshot)
        self._record_routing(ref, record, phase="review", heads=[record.review_run])

    def _record_routing(
        self,
        ref: str,
        record: DispatcherRecord,
        *,
        phase: str,
        heads: list[dict[str, Any]],
        outcome: str = "",
    ) -> None:
        heads = [head for head in heads if head]
        if not heads or not record.attempt_round:
            return
        # The request id carries the launched configurations, not just the round: the same head writes
        # the same id and commits once, a different configuration appends. Same for a verdict's pair.
        parts = [str(record.attempt_round)]
        if outcome:
            parts.append(outcome)
        parts.extend(_run_key(head) for head in heads)
        request_id = _attempt_request_id(record.attempt_id, f"routing-{phase}", ref, "-".join(parts))
        # A tick can die after the journal commit but before its launch snapshot reaches dispatcher
        # state. Recovery cannot rediscover that provider conversation from a live workspace, so it
        # must reuse the committed event's exact dynamic facts rather than retry the same request id
        # with a newly-derived null session or a rewritten prompt digest.
        existing = self.audit.committed_event(request_id)
        if existing is not None:
            payload = existing.get("payload") if isinstance(existing, dict) else None
            recorded_heads = payload.get("heads") if isinstance(payload, dict) else None
            if isinstance(recorded_heads, list):
                by_role = {
                    str(head.get("role") or ""): head for head in recorded_heads if isinstance(head, dict)
                }
                for head in heads:
                    role = str(head.get("role") or "")
                    recorded = by_role.get(role)
                    if recorded is None:
                        continue
                    if role == WORKER:
                        record.worker_run = cast(PersistedRoutingHeadSnapshot, recorded)
                    elif role == REVIEWER:
                        record.review_run = cast(PersistedRoutingHeadSnapshot, recorded)
            return
        self.writer.routing(
            role="dispatcher",
            actor=self.owner,
            reference=ref,
            payload=_routing_payload(
                attempt=record.attempt_round,
                attempt_id=record.attempt_id,
                phase=phase,
                heads=heads,
                outcome=outcome,
            ),
            request_id=request_id,
        )

    def _record_verdict_routing(self, ref: str, record: DispatcherRecord, outcome: str) -> None:
        """Tie the round's outcome to the heads that earned it, carrying both so worker-reviewer
        pairs group by outcome without a join against the launch records."""
        self._record_routing(
            ref,
            record,
            phase="verdict",
            heads=[record.worker_run, record.review_run],
            outcome=outcome,
        )

    @serialized
    def save_records(self, payload: dict[str, Any], records: dict[str, DispatcherRecord]) -> None:
        """Flush the dispatcher records into the production state."""
        if isinstance(self.host, CommandHostRuntime) and self.host.mode == "real":
            for ref, record in records.items():
                if record.workspace:
                    self.cleanup.remember(self.reader.show(ref), record)
        self.production_state.put_records(payload, records)
        payload["last_tick_at"] = now_rfc3339()
        self.production_state.save(payload)

    def _adopt(self, task: dict[str, Any], attempt_id: str) -> DispatcherRecord:
        worker = task.get("claim", {}).get("worker") or _worker_id(task)
        review_baseline = _review_adoption_baseline(task)
        launched = self._review_launch_recorded(task, review_baseline)
        state = "review_starting" if launched else "adopted"
        if task.get("state") == "assessment":
            # A parked card has no head to recover: the reviewer was stopped when it parked, and a
            # worker still suspended in the checkout is not something this record can prove.
            state = "assessment"
        # The routing round of a lost record comes back from the journal, heads included:
        # re-reading the registry would report today's `heads.toml` for a head launched hours ago.
        resumed = _routing_attempts(self.audit.events(task["ref"], kind="routing"))
        round_record = resumed[-1] if resumed else None
        workspace = self.host.restore_workspace(task, worker)
        # The report generation is dispatcher state, lost on this path. The TASK.md names the round
        # the live worker is in; the board's reports are the floor with no readable document. Both
        # are lower bounds, so the larger one is taken: a generation may skip, never repeat.
        report_generation = max(_task_doc_report_generation(workspace), _spent_report_generations(task) + 1)
        # And the decision that round was opened on, from the same document. The card's newest
        # decision comment answers "what was decided since", which must not reach a running round.
        report_decision = _task_doc_decision(workspace)
        report_protocol_prerequisites = _task_doc_protocol_prerequisites(workspace)
        # The lost state file took the round's terminal path with it. The card's own lifecycle
        # state is the same dispatcher-owned fact and outlives that file: a card cannot stand in
        # Validate or Assessment without an accepted report. The reconstructed record state is
        # read the same way, and neither reads back a handoff or a marker.
        adopted_path = _outcome_terminal_path(None, state=str(task.get("state") or ""))
        if adopted_path is OutcomeTerminalPath.NO_ACCEPTED_REPORT:
            adopted_path = _outcome_terminal_path(None, state=state)
        record = DispatcherRecord(
            worker=worker,
            workspace=workspace,
            handle="",
            head=self.catalog.claimed_worker_head(task),
            review_head=self.catalog.claimed_review_head(task),
            attempt_id=attempt_id,
            comment_baseline=_report_adoption_baseline(task),
            review_baseline=review_baseline,
            report_generation=report_generation,
            report_decision=report_decision,
            report_protocol_prerequisites=report_protocol_prerequisites,
            state=state,
            claimed_at=time.time(),
            # A reviewer launches only over a green gate, so a card in review inherits a passed gate.
            gate_state="green" if launched else "",
            attempt_round=round_record.attempt if round_record else 0,
            worker_run=cast(
                PersistedRoutingHeadSnapshot,
                round_record.worker.to_json() if round_record and round_record.worker else {},
            ),
            review_run=cast(
                PersistedRoutingHeadSnapshot,
                round_record.reviewer.to_json() if round_record and round_record.reviewer else {},
            ),
            outcome_terminal_path=adopted_path,
        )
        # A lost record may be recovered from the worker's own heartbeat, but only after its
        # self-described run, role and card binding are promoted into a HeadRun and checked again.
        # A legacy pid or another card's process stays unbound and is never signalled.
        pid_file = _launch_pid_file(WORKER_ROLE, task["ref"])
        heartbeat = _head_process_status(pid_file) if task.get("state") == "in_progress" else {}
        raw = cast(
            dict[str, Any], heartbeat.get("record") if isinstance(heartbeat.get("record"), dict) else {}
        )
        if (
            _heartbeat_is_live_match(heartbeat)
            and str(raw.get("role") or "") == WORKER_ROLE
            and str(raw.get("task") or "") == f"card:{task['ref']}"
            and str(raw.get("run_id") or "")
        ):
            recovered = head_ops.HeadRun(
                run_id=str(raw["run_id"]),
                spec=HeadSpec(
                    profile_id=record.head,
                    adapter=str(record.worker_run.get("adapter") or "unknown"),
                ),
                workspace=workspace,
                task_ref=head_ops.TaskRef.card(task["ref"]),
                leaf=str(raw.get("leaf") or ""),
                pid_file=pid_file,
            )
            verified = _head_run_process_status(
                pid_file,
                run=recovered,
                role=WORKER_ROLE,
                leaf=recovered.leaf,
            )
            if _heartbeat_is_live_match(verified):
                record.worker_head_run = cast(PersistedHeadRun, recovered.to_json())
                record.worker_pid_file = pid_file
                record.worker_started_at = record.worker_progress_at = time.time()
        return record

    def _review_launch_recorded(self, task: dict[str, Any], review_baseline: int) -> bool:
        if task.get("state") != "validate":
            return False
        return self.audit.committed_event(_review_launch_request_id(task["ref"], review_baseline)) is not None


def _review_launch_request_id(reference: str, review_baseline: int) -> str:
    return _attempt_request_id("review", "start-intent", reference, str(review_baseline))
