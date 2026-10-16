"""Host I/O and catalog boundary for the production dispatcher."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import signal
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ummanu import _proc
from ummanu.board.completion_evidence import has_candidate
from ummanu.board.protocol_artifacts import (
    ArtifactOwnershipViolation,
    ProtocolArtifact,
    validate_rework_prerequisites,
)
from ummanu.codex_provider_events import CodexProviderEventIngress
from ummanu.dispatch import production_checkout
from ummanu.dispatch.card_roles import card_kind, head_role
from ummanu.dispatch.cleanup import lifecycle
from ummanu.dispatch.gate import (
    GateResult,
    gate_check as _gate_check,
    validation_ci as _validation_ci,
)
from ummanu.dispatch.gate_receipt import is_exact_sha as _is_exact_sha
from ummanu.dispatch.git_workspace import (
    WORKSPACES_DIR as GIT_WORKSPACES_DIR,
    GitWorkspaceManager,
    _resolved as _resolved_path,
    orca_workspaces_root,
)
from ummanu.dispatch.github_ci import rerun_failed_ci as _rerun_failed_ci
from ummanu.dispatch.head_vitality_episode import VitalityVerdict as VitalityVerdict
from ummanu.dispatch.heartbeat import heartbeat_identity
from ummanu.dispatch.helpers import (
    _legacy_worker_branch,
    _round_report_ids,
    _round_worker_check,
    _tail,
    scrub_host_output,
)
from ummanu.dispatch.host_runtime.catalog import InstanceCatalog, _same_repo, live_root_project_refusal
from ummanu.dispatch.host_runtime.contracts import (
    _delivery_evidence_json,
    _durable_head_run,
    _launch_command,
    _record_worker_delivery_evidence,
    _runtime_writes_launch_identity,
)
from ummanu.dispatch.host_runtime.lifecycle import HeadLifecycleMixin
from ummanu.dispatch.host_runtime.observer_io import (
    OBSERVER_REPO_BRANCH as OBSERVER_REPO_BRANCH,
    ObserverHostMixin,
)
from ummanu.dispatch.host_runtime.packets import (
    PromptPacketMixin,
    _body_file_path as _body_file_path,
    _gate_attestation_for_prompt as _gate_attestation_for_prompt,
    _private_body_dir as _private_body_dir,
)
from ummanu.dispatch.host_runtime.workspace import WorkspaceEnvironmentMixin
from ummanu.dispatch.launch import (
    CAUSE_WORKSPACE_CONTRACT,
    LAUNCH_DELIVERY_WORKER_FENCED,
    REVIEW_ROLE,
    WORKER_ROLE,
    infrastructure_action as _infrastructure_action,
    launch_delivery,
)
from ummanu.dispatch.observer import ObserverLaunchAborted as ObserverLaunchAborted
from ummanu.dispatch.post_merge import pr_merge_commit
from ummanu.dispatch.production_checkout import ProductionActivationRefused
from ummanu.dispatch.provider_failure import (
    provider_failure_for_persisted_run as _provider_failure_for_persisted_run,
)
from ummanu.dispatch.review import command_terminal_status as _command_terminal_status
from ummanu.dispatch.runtime_provenance import ProductionRuntime, RuntimeProvenance
from ummanu.dispatch.stale_round import WorkspaceReading, read_round_workspace
from ummanu.dispatch.state import DispatcherRecord
from ummanu.dispatch.tick_telemetry import tick_stage
from ummanu.dispatch.tui import (
    READINESS_BUSY,
    TuiDeliveryError,
    bind_claude_provider_progress_source as _bind_claude_provider_progress_source,
    prepare_claude_provider_progress_source as _prepare_claude_provider_progress_source,
    provider_progress_for_run as _provider_progress_for_run,
    provider_turn_started as _provider_turn_started,
)
from ummanu.dispatch.types import (
    STOPPED_BY_DISPATCHER,
    HeadLaunchAborted,
    HostError,
    LegacyDispatcherRecord,
    MergeLanding,
    ProjectGitAccessError,
    ReviewLaunch,
    review_pane_label,
)
from ummanu.dispatch.watchdog import (
    bind_head_heartbeat as _bind_head_heartbeat,
    clear_head_heartbeat as _clear_head_heartbeat,
    head_process_status as _head_process_status,
    heartbeat_is_live_match as _heartbeat_is_live_match,
    pid_file_path as _pid_file_path,
)
from ummanu.dispatch.worker_comments import worker_comments_note
from ummanu.infra.github_credential import (
    PROJECT_ACCESS_REFUSALS,
    CredentialError,
    ProjectGitAccess,
    RemoteExecution,
    project_access_failure_code,
    project_remote_execution,
)
from ummanu.memory import access as memory_access
from ummanu.projects.availability import ProjectAvailability
from ummanu.routing_journal import MODEL_UNKNOWN, HeadRun
from ummanu.runtime import head as head_ops
from ummanu.runtime.codex_preflight import CodexFanoutPolicyError, preflight_codex_launch
from ummanu.runtime.head import HeadCommand, HeadSpec, HeadSpecError
from ummanu.runtime.head.children import read_head_children
from ummanu.runtime.head.handoff import (
    HANDOFF_DEFERRED,
    HANDOFF_NOT_STARTED,
    HANDOFF_SETTLE,
    HANDOFF_STAGE_KEY,
    HANDOFF_UNKNOWN,
    PromptHandoff,
    active_budget,
    handoff_pending_stage,
)
from ummanu.runtime.head_runtime_backends import (
    LegacyHeadRecordError,
    UnknownHeadRuntimeError,
    build_head_runtime,
    head_runtime_name,
    is_legacy_record,
)
from ummanu.runtime.head_runtimes import LOCAL_PTY_RUNTIME
from ummanu.runtime.local_pty_head import RunEnd, head_run_end, head_run_turn_reading
from ummanu.runtime.paths import configured_product_root
from ummanu.runtime.prompt_document import PromptDocumentError
from ummanu.runtime.role_env import BOARD_ACTOR_ENV
from ummanu.tasks import durability_dirt, specification_revision

DESTRUCTIVE_VERDICTS = frozenset(
    {
        VitalityVerdict.CONFIRMED_STALL,
        VitalityVerdict.DEAD,
    }
)


_PREPARED_SOURCE_IDENTITY_KEYS = (
    "version",
    "kind",
    "run_id",
    "head_run_fingerprint",
    "workspace",
    "role",
    "task_ref",
    "root",
)


def _blocked_actions_and_their_infrastructure_twins(*actions: str) -> tuple[str, ...]:
    """Each blocked action and the token its infrastructure-classified form writes.

    Pair blocked outcomes with their infrastructure-classified tokens for replay.
    """
    seen: dict[str, None] = {}
    for action in actions:
        seen.setdefault(action, None)
        seen.setdefault(_infrastructure_action(action), None)
    return tuple(seen)


@dataclass(frozen=True)
class LaunchedHead:
    """One head bring-up as it happened: the pane it runs in and the configuration it runs with."""

    handle: str
    head: str = ""
    run: dict[str, Any] = field(default_factory=dict)
    leaf: str = ""
    delivery_evidence: dict[str, Any] = field(default_factory=dict)
    head_run: dict[str, Any] = field(default_factory=dict)
    fallback_reason: str = ""


@dataclass(frozen=True)
class DispatcherHeadTransport:
    """What this dispatcher hands a head's backend beside a prompt: who the head is and its hook."""

    runtime: CommandHostRuntime
    workspace: str = ""
    prompt_file: str = ""
    adapter: str = ""
    role: str = ""
    before_send: Callable[[], head_ops.HeadRun | None] | None = None
    # The caller's acknowledgement arrives somewhere other than this delivery. Only the observer
    # wake sets it: it holds both proofs a delivery can have and quotes the delivery id in the
    # resume written from the turn it starts, so the weaker of the two must not refuse it.
    ack_out_of_band: bool = False


_head_runtime_name = head_runtime_name


class CommandHostRuntime(ObserverHostMixin, WorkspaceEnvironmentMixin, HeadLifecycleMixin, PromptPacketMixin):
    def __init__(
        self,
        catalog: InstanceCatalog,
        data_dir: Path,
        *,
        mode: str = "real",
        production_runtime: ProductionRuntime | None = None,
        audit: Any | None = None,
        sprint_reader: Any | None = None,
    ) -> None:
        self.catalog = catalog
        self.data_dir = data_dir
        # TASK.md is a durable projection, so its feedback selector reads the same card audit as
        # the dispatcher rather than depending on a live record or wall-clock ordering. The
        # dispatcher hands its own in (`dispatch.bootstrap.runtime_from_args`), which is the only
        # production construction of this host. There is no default: built from the data dir alone
        # it would be the file journal, which nobody writes, and its silence would render a TASK.md
        # without the review the round was bound to. A host standing on its own -- what a test
        # builds -- refuses the read by name instead (`_card_audit`).
        self.audit = audit
        self.sprint_reader = sprint_reader
        self.mode = mode
        # Fixed once for this dispatcher process. Every lifecycle fence asks this same value rather
        # than independently guessing an interpreter, checkout or workspace namespace.
        self.production_runtime = production_runtime or ProductionRuntime.current(
            configured_product_root(), git_workspaces_root=Path(data_dir) / GIT_WORKSPACES_DIR
        )
        # Where a head run is flushed the moment an operation commits it, ahead of the tick's own
        # save. Its durable-state owner installs this only while it holds the record's file: this
        # host has a record, not that file. Unset, a run reaches disk with the tick's records.
        self.commit_state: Callable[[], None] | None = None
        self.cleanup_owner: Any | None = None
        # The first preflight fixes a provider source's baseline before its pane exists. A launch
        # may attest the same run again immediately before opening that pane, but that second
        # read must not replace the durable baseline with a listing taken later in bring-up.
        # Codex also has a live ingress below; Claude has no ingress because its source is bound
        # by the retained HeadRun's read-only progress probe.
        self._prepared_provider_runs: dict[str, head_ops.HeadRun] = {}
        # The owner installs one entry before it asks this host to open a Codex pane, keyed by the
        # HeadRun id in that intent so a same-workspace respawn cannot inherit a predecessor's source.
        self._codex_provider_ingresses: dict[str, CodexProviderEventIngress] = {}
        # The boundaries a head's life is lived through, one per backend a profile can name. Held
        # rather than rebuilt per access, because the turn leases and the activity epoch are what
        # they hold: a runtime rebuilt on every access would have no memory of the turns it handed
        # out. Built lazily and cached by name.
        self._head_runtimes: dict[str, Any] = {}

    def configure_codex_provider_ingress(
        self,
        run: head_ops.HeadRun,
        *,
        persist: Callable[[head_ops.HeadRun], None],
        stop: Callable[[head_ops.HeadRun, str], None],
        block: Callable[[dict[str, Any]], None],
    ) -> None:
        """Install the launch owner's exact-run provider-event ingress."""
        source = run.fanout_policy.get("provider_source")
        if run.spec.adapter != "codex" or not isinstance(source, dict):
            return
        self._codex_provider_ingresses[run.run_id] = CodexProviderEventIngress(
            run,
            persist,
            stop=stop,
            block=block,
        )

    def poll_codex_provider_ingress(self, run: head_ops.HeadRun) -> None:
        """Best-effort read new provider events through the run-bound launch ingress."""
        ingress = self._codex_provider_ingresses.get(run.run_id)
        if ingress is None:
            # The collector is process-local: missing fan-out telemetry is not a lifecycle decision.
            return
        ingress.commit_run(run)
        ingress.poll()

    def _codex_provider_ingress(self, run: head_ops.HeadRun) -> CodexProviderEventIngress | None:
        ingress = self._codex_provider_ingresses.get(run.run_id)
        if ingress is not None:
            return ingress
        return None

    @contextlib.contextmanager
    def committing(self, flush: Callable[[], None]):
        """Lend this runtime a way to flush the durable state, for as long as the caller holds it."""
        previous = self.commit_state
        self.commit_state = flush
        try:
            yield
        finally:
            self.commit_state = previous

    def preflight_codex_run(
        self,
        head: str,
        *,
        role: str,
        workspace: str,
        task_ref: head_ops.TaskRef,
        pid_file: str,
        run_id: str,
    ) -> head_ops.HeadRun:
        """Create and attest the durable run that exists before this Codex pane does."""
        profile = self.catalog.head_profile(head)
        spec = self._head_spec(head, str(profile.get("adapter") or "unknown"))
        run = head_ops.HeadRun(
            run_id=run_id,
            spec=spec,
            workspace=workspace,
            task_ref=task_ref,
            role=role,
            pid_file=pid_file,
            # The launch intent allocates a fresh run ID before either preflight call.
            scope_generation=run_id if spec.memory_limit_mib is not None else "",
        )
        if spec.adapter == "claude":
            prepared = _prepare_claude_provider_progress_source(run)
            self._prepared_provider_runs.setdefault(run_id, prepared)
            return prepared
        if spec.adapter != "codex":
            return run
        return preflight_codex_launch(profile, workspace, run)

    def _preflight_launch_run(
        self,
        head: str,
        *,
        role: str,
        workspace: str,
        task_ref: head_ops.TaskRef,
        pid_file: str,
        run_id: str,
    ) -> head_ops.HeadRun:
        """Attest this bring-up's run, then hand it the source its launch was prepared with."""
        return self._retain_prepared_provider_source(
            self.preflight_codex_run(
                head,
                role=role,
                workspace=workspace,
                task_ref=task_ref,
                pid_file=pid_file,
                run_id=run_id,
            )
        )

    def _retain_prepared_provider_source(self, attested: head_ops.HeadRun) -> head_ops.HeadRun:
        """Keep the pre-pane provider source this exact run was already prepared with.

        Retain only exact run/spec/workspace/role/task/pid/source identity; preserve bound sources.
        """
        ingress = self._codex_provider_ingresses.get(attested.run_id)
        prepared = ingress.run if ingress is not None else self._prepared_provider_runs.get(attested.run_id)
        if prepared is None:
            return attested
        if (
            not prepared.same_run(attested)
            or prepared.spec != attested.spec
            or prepared.workspace != attested.workspace
            or prepared.task_ref != attested.task_ref
            or prepared.role != attested.role
            or prepared.pid_file != attested.pid_file
        ):
            return attested
        source_key = "provider_source" if attested.spec.adapter == "codex" else "provider_progress_source"
        prepared_source = prepared.fanout_policy.get(source_key)
        fresh_source = attested.fanout_policy.get(source_key)
        if not isinstance(prepared_source, dict) or not isinstance(fresh_source, dict):
            # A fresh attestation that established no source of its own has nothing to hand over,
            # and never erases the durable one; the handoff merge keeps what is already on disk.
            return attested
        if any(prepared_source.get(key) != fresh_source.get(key) for key in _PREPARED_SOURCE_IDENTITY_KEYS):
            return attested
        policy = dict(attested.fanout_policy)
        policy[source_key] = dict(prepared_source)
        return attested.with_fanout_policy(policy)

    def _prompt_adapter(self, run: Any, head: str) -> str:
        """The provider whose framing a prompt for this pane is delivered in."""
        if isinstance(run, dict):
            adapter = str(run.get("adapter") or "").lower()
            if adapter:
                return adapter
        try:
            profile = self.catalog.head_profile(head)
        except (AttributeError, HostError) as exc:
            raise HostError(
                f"cannot resolve the prompt adapter for head {head!r}: {exc or 'unknown head'}"
            ) from None
        try:
            return HeadSpec.from_profile(head, profile).adapter.lower()
        except HeadSpecError as exc:
            raise HostError(f"cannot resolve the prompt adapter for head {head!r}: {exc}") from None

    @lifecycle
    def prepare_worker(
        self,
        task: dict[str, Any],
        worker_id: str,
        head: str,
        *,
        attempt_id: str = "",
        require_existing_workspace: bool = False,
        generation: int = 0,
        failover: bool = False,
        heartbeat_run_id: str = "",
        local_run_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._require_production_runtime("worker-prepare")
        self._require_cleanup_admission(task["ref"])
        project = task["project"]
        self._require_project_available(project)
        base = self.catalog.integration_base(project, task.get("workspace", {}).get("base_branch"))
        seed = self.catalog.workspace_seed(project, task)
        workspace = self.restore_workspace(task, worker_id)
        reused = Path(workspace).exists()
        if reused:
            self._validate_resumable_workspace(task, workspace)
            missing_environment = not self._workspace_environment_ready(workspace)
            self._prepare_workspace_environment(workspace, project=project)
            if missing_environment:
                self._run_setup(project, workspace)
        else:
            if require_existing_workspace:
                # The card was requeued onto the checkout its own last attempt preserved, and that
                # checkout is gone. No host repairs it and no later tick finds it: this is the one
                # bring-up family that is about the card rather than the host.
                raise HostError("resume workspace is missing", bring_up_cause=CAUSE_WORKSPACE_CONTRACT)
            if self.mode == "noop":
                Path(workspace).mkdir(parents=True, exist_ok=True)
            else:
                workspace = self._git_workspaces.create(task, worker_id, seed, expected=workspace)
            self._prepare_workspace_environment(workspace, project=project)
            self._run_setup(project, workspace)
        self._require_workspace_environment(workspace)
        self._install_workspace_test_guards(Path(workspace), project=project)
        self._clear_report_bodies(task["ref"])
        snapshot = local_run_snapshot or self.local_run_snapshot_for_round(
            task, WORKER_ROLE, generation, {}
        )
        local_run_policy = self._frozen_local_run_policy(task, WORKER_ROLE, generation, snapshot)
        self._write_prompt(
            Path(workspace) / "TASK.md",
            self._worker_task_doc(
                task, base, attempt_id, generation, local_run_policy=local_run_policy
            ),
        )
        launched = self._launch(
            workspace,
            f"{task['ref']} worker",
            head,
            "TASK.md",
            role="worker",
            env_name="UMMANU_DISPATCHER_WORKER_COMMAND",
            launch_prompt=self._worker_launch_prompt(),
            prompt_document=str(Path(workspace) / "TASK.md"),
            task=task,
            failover=failover,
            heartbeat_run_id=heartbeat_run_id,
            local_run_policy=local_run_policy,
        )
        return {
            "workspace": workspace,
            "handle": launched.handle,
            "leaf": launched.leaf,
            "base_branch": base,
            # Recorded instead of re-reading the registry, which a later edit would answer differently.
            "run": launched.run,
            "delivery_evidence": dict(launched.delivery_evidence),
            "head_run": dict(launched.head_run),
        }

    @lifecycle
    def restart_worker(
        self, task: dict[str, Any], record: DispatcherRecord, *, heartbeat_run_id: str = ""
    ) -> LaunchedHead:
        """Launch rework in the existing workspace without recreating its branch."""
        self._require_cleanup_admission(task["ref"])
        self._require_project_available(str(task.get("project") or ""))
        self._refuse_legacy_record(record, "relaunch the worker of")
        workspace = Path(record.workspace)
        if self.mode == "noop":
            workspace.mkdir(parents=True, exist_ok=True)
        elif not workspace.is_dir():
            # Same family as the missing resume workspace above: the checkout this card's rework
            # continues in is not there, which is this card's own bring-up contract, not the host's.
            raise HostError("rework workspace is missing", bring_up_cause=CAUSE_WORKSPACE_CONTRACT)
        self._prepare_workspace_environment(str(workspace), project=str(task["project"]))
        self._require_workspace_environment(str(workspace))
        base = self.catalog.integration_base(task["project"], task.get("workspace", {}).get("base_branch"))
        self._clear_report_bodies(task["ref"])
        local_run_policy = self._frozen_local_run_policy(
            task, WORKER_ROLE, record.report_generation, record.worker_local_run_snapshot
        )
        self._write_prompt(
            workspace / "TASK.md",
            self._worker_task_doc(
                task,
                base,
                record.attempt_id,
                record.report_generation,
                record.report_decision,
                record.report_protocol_prerequisites,
                record=record,
                local_run_policy=local_run_policy,
            ),
        )
        return self._launch(
            str(workspace),
            f"{task['ref']} worker rework",
            record.head,
            "TASK.md",
            role="worker",
            env_name="UMMANU_DISPATCHER_WORKER_COMMAND",
            launch_prompt=self._worker_launch_prompt(),
            prompt_document=str(workspace / "TASK.md"),
            task=task,
            failover=bool(record.preferred_head),
            heartbeat_run_id=heartbeat_run_id,
            local_run_policy=local_run_policy,
        )

    def provider_progress(self, task: dict[str, Any], record: DispatcherRecord, kind: str) -> dict[str, str]:
        """Read an opaque provider cursor for this exact role's persisted HeadRun."""
        kind = card_kind(kind)
        run = record.review_head_run if kind == "review" else record.worker_head_run
        expected_workspace = record.workspace
        try:
            lifecycle_run = head_ops.HeadRun.from_json(run)
        except (head_ops.HeadRunError, TypeError, ValueError):
            return {"state": "unavailable", "reason": "persisted HeadRun is unavailable"}
        expected_role = head_role(kind)
        if (
            lifecycle_run.workspace != expected_workspace
            or lifecycle_run.task_ref.kind != "card"
            or lifecycle_run.task_ref.ref != str(task.get("ref") or "")
            or (lifecycle_run.role and lifecycle_run.role != expected_role)
        ):
            return {"state": "identity_mismatch", "reason": "persisted HeadRun binding mismatches role"}
        # The provider creates its transcript asynchronously, so binding uses only the durable
        # pre-pane baseline on the HeadRun; ambiguity stays typed unavailable, never a guess.
        if lifecycle_run.spec.adapter == "claude":
            updated = _bind_claude_provider_progress_source(lifecycle_run)
            if updated != lifecycle_run:
                if kind == "review":
                    self._commit_review_run(record, updated)
                else:
                    self._commit_worker_run(record, updated)
                lifecycle_run = updated
        return _provider_progress_for_run(lifecycle_run)

    def provider_failure(self, task: dict[str, Any], record: DispatcherRecord, kind: str) -> dict[str, Any]:
        """Whether this role's exact HeadRun ended its last turn on a provider error (secretary-1799, ummanu-108)."""
        if self.mode == "noop":
            return {"state": "unavailable", "reason": "noop host"}
        kind = card_kind(kind)
        run = record.review_head_run if kind == "review" else record.worker_head_run
        try:
            lifecycle_run = head_ops.HeadRun.from_json(run)
        except (head_ops.HeadRunError, TypeError, ValueError):
            return {"state": "unavailable", "reason": "persisted HeadRun is unavailable"}
        expected_role = head_role(kind)
        if (
            lifecycle_run.workspace != record.workspace
            or lifecycle_run.task_ref.kind != "card"
            or lifecycle_run.task_ref.ref != str(task.get("ref") or "")
            or (lifecycle_run.role and lifecycle_run.role != expected_role)
        ):
            return {"state": "identity_mismatch", "reason": "persisted HeadRun binding mismatches role"}
        return _provider_failure_for_persisted_run(run, local_pty_root=self._local_pty_root())

    def head_children(self, head_pid: int) -> dict[str, Any]:
        """The live descendants of a head's heartbeat-proven pid, with their movement counters."""
        return read_head_children(head_pid)

    def supervisor_journal(
        self, _task: dict[str, Any], record: DispatcherRecord, kind: str
    ) -> dict[str, Any]:
        """This role's head's turn and progress, read from its own supervisor's journal."""
        run = record.review_head_run if kind == "review" else record.worker_head_run
        return head_run_turn_reading(self._local_pty_root(), str((run or {}).get("run_id") or ""))

    def head_loss_reason(self, run: Any) -> str | None:
        """Typed loss recorded by this run's own supervisor, with no process inference."""
        from ummanu.runtime.local_pty_head import head_run_loss_reason

        run_id = str((run or {}).get("run_id") or "") if isinstance(run, dict) else ""
        return head_run_loss_reason(self._local_pty_root(), run_id) if run_id else None

    def head_run_end(
        self, run: Mapping[str, Any], *, heartbeat_state: str, launched_boot: str
    ) -> RunEnd | None:
        """Whether this run has ended, read from its own run directory under this host's root."""
        return head_run_end(
            self._local_pty_root(),
            str(run.get("run_id") or ""),
            heartbeat_state=heartbeat_state,
            launched_boot=launched_boot,
        )

    def safe_recover_worker_continuation(
        self,
        _task: dict[str, Any],
        _record: DispatcherRecord,
        _liveness: dict[str, Any],
    ) -> dict[str, str]:
        """Advertise that this host has no safe provider recovery primitive."""
        return {
            "state": "unavailable",
            "reason": "no provider/terminal-safe continuation recovery capability is available",
        }

    @lifecycle
    def start_review(self, task: dict[str, Any], record: DispatcherRecord) -> ReviewLaunch:
        """Bring the reviewer up as a second head inside the worker's own worktree."""
        self._require_project_available(str(task.get("project") or ""))
        self._refuse_legacy_record(record, "launch the reviewer of")
        if not record.workspace:
            raise HostError("review workspace is unavailable")
        workspace = Path(record.workspace)
        if self.mode == "noop":
            workspace.mkdir(parents=True, exist_ok=True)
        elif not workspace.is_dir():
            raise HostError("review workspace is missing")
        self._require_worker_admission(task, record)
        self._prepare_workspace_environment(record.workspace, project=str(task["project"]))
        self._require_workspace_environment(record.workspace)
        self._clear_body_file("verdict", task["ref"], record.review_baseline)
        local_run_policy = self._frozen_local_run_policy(
            task, REVIEW_ROLE, record.review_baseline, record.review_local_run_snapshot
        )
        document, nudge = self._review_document(task, record, local_run_policy=local_run_policy)
        handoff = _review_launch_handoff(record.launch_intent) if self.head_handoffs() else None
        try:
            launched = self._launch(
                record.workspace,
                review_pane_label(task["ref"]),
                record.review_head,
                str(document),
                role="reviewer",
                env_name="UMMANU_DISPATCHER_REVIEW_COMMAND",
                launch_prompt=nudge,
                prompt_document=str(document),
                task=task,
                failover=bool(record.preferred_review_head),
                heartbeat_run_id=str((record.launch_intent or {}).get("run_id") or ""),
                local_run_policy=local_run_policy,
                handoff=handoff,
            )
        except HeadLaunchAborted as aborted:
            if handoff is None or not handoff_pending_stage(aborted.evidence):
                raise
            # A production launch: the reviewer is up and nothing has been typed into it. The writer
            # fence is made now, before any of its prompt, and every later pass that types into it
            # checks the fence again first (`nudge_review_delivery`).
            try:
                self._fence_worker_for_reviewer(record)
            except HostError as exc:
                raise HeadLaunchAborted(
                    f"worker freeze failed: {exc}",
                    handle=aborted.handle,
                    leaf=aborted.leaf,
                    workspace=aborted.workspace,
                    pid_file=aborted.pid_file,
                    evidence={
                        key: value for key, value in aborted.evidence.items() if key != HANDOFF_STAGE_KEY
                    },
                    head_run=dict(aborted.head_run),
                ) from None
            raise
        try:
            self._fence_worker_for_reviewer(record)
        except HostError as exc:
            # The reviewer pane is up and the worker would not stop: neither head can be reported as
            # absent, so the pane goes back with the failure and the caller keeps its launch intent.
            raise HeadLaunchAborted(
                f"worker freeze failed: {exc}",
                handle=launched.handle,
                leaf=launched.leaf,
                workspace=record.workspace,
                pid_file=_pid_file_path("review", task["ref"]),
                evidence=dict(launched.delivery_evidence),
                head_run=dict(launched.head_run),
            ) from None
        return ReviewLaunch(
            handle=launched.handle,
            leaf=launched.leaf,
            commit=self.head_commit(record),
            run=launched.run,
            head_run=dict(launched.head_run),
            delivery_evidence=dict(launched.delivery_evidence),
            fallback_reason=launched.fallback_reason,
        )

    def _fence_worker_for_reviewer(self, record: DispatcherRecord) -> None:
        """Make sure the worker cannot write the checkout the reviewer judges, or raise.

        A reviewer may judge only after worker writes are provably fenced.
        """
        if record.worker_continuation.retained and self.worker_retained_vanished(record):
            # The retained worker is provably gone (its pid heartbeat resolves to a dead pid), so
            # there is no second writer to freeze. The freeze path here would loop
            # `review-launch-aborted` over a head that can never confirm suspended.
            return
        if record.worker_continuation.retained:
            # A retained worker is already SIGSTOPed and its conversation is what a red verdict
            # continues. Confirm that suspension rather than trusting the record.
            self.confirm_worker_retained(record)
            return
        self._freeze_worker(record)

    def nudge_review_delivery(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        intent: dict[str, Any],
    ) -> dict[str, Any]:
        """Retry the document nudge for the exact reviewer a busy launch intent retained.

        Deliver to the exact retained reviewer only behind the worker fence.
        """
        self._refuse_legacy_record(record, "deliver to the reviewer of")
        if not record.workspace:
            raise HostError("review workspace is unavailable for a retained launch")
        self._require_worker_admission(task, record)
        self._prepare_workspace_environment(record.workspace, project=str(task["project"]))
        stored_run = intent.get("head_run")
        if not isinstance(stored_run, dict) or not stored_run.get("run_id"):
            raise HostError("retained reviewer launch has no durable head run")
        try:
            run = head_ops.HeadRun.from_json(stored_run)
        except (head_ops.HeadRunError, head_ops.TaskRefError) as exc:
            raise HostError(f"retained reviewer launch has an unreadable head run: {exc}") from None
        workspace = str(intent.get("workspace") or record.workspace)
        handle = str(intent.get("handle") or run.handle)
        leaf = str(intent.get("leaf") or run.leaf)
        pid_file = str(intent.get("pid_file") or run.pid_file)
        run = replace(run, workspace=workspace, handle=handle, leaf=leaf, pid_file=pid_file)
        ingress = self._codex_provider_ingress(run)
        if ingress is not None:
            ingress.commit_run(run)
            # A recovered, already-bound source is consumed before another delivery can act. An
            # unbound source belongs to a prior busy pre-send and is bound by the same boundary.
            if ingress.source.get("state") == "bound":
                ingress.poll()
        local_run_policy = self._frozen_local_run_policy(
            task, REVIEW_ROLE, record.review_baseline, record.review_local_run_snapshot
        )
        backend = self.head_runtime_for(run)
        handoff = (
            _review_launch_handoff(intent) if self.head_handoffs() and _takes_handoffs(backend) else None
        )
        if handoff is not None and (
            record.worker_continuation.retained or not launch_delivery(intent).get(LAUNCH_DELIVERY_WORKER_FENCED)
        ):
            try:
                self._fence_worker_for_reviewer(record)
            except HostError as exc:
                failure = HostError(f"the worker could not be fenced before the reviewer's prompt: {exc}")
                failure.evidence = {"subject": "reviewer-launch", "reason": scrub_host_output(str(exc))[:400]}
                raise failure from None
        document, nudge = self._review_document(task, record, local_run_policy=local_run_policy)
        try:
            receipt = backend.deliver(
                run,
                head_ops.NudgePointer(text=nudge, document=str(document)),
                transport=self._head_transport(
                    workspace,
                    str(document),
                    self._prompt_adapter(intent.get("run"), record.review_head),
                    "reviewer",
                    before_send=ingress.bind_before_delivery if ingress is not None else None,
                ),
                subject="reviewer-launch",
                **({"handoff": handoff} if handoff is not None else {}),
            )
        except LegacyDispatcherRecord:
            raise
        except (TuiDeliveryError, HostError) as exc:
            failure = HostError(f"retained reviewer document nudge was not delivered: {exc}")
            failure.evidence = _delivery_evidence_json(exc, "reviewer-launch")
            raise failure from None
        if receipt.handoff_stage:
            pending = HostError(f"retained reviewer document nudge is pending: {receipt.reason}")
            pending.evidence = _delivery_evidence_json(receipt, "reviewer-launch")
            raise pending from None
        if not receipt.ok:
            failure = HostError(f"retained reviewer document nudge was not delivered: {receipt.reason}")
            failure.evidence = _delivery_evidence_json(receipt.failure, "reviewer-launch")
            raise failure from None
        return {
            "handle": receipt.run.handle,
            "leaf": receipt.run.leaf,
            "head_run": receipt.run.to_json(),
            "delivery_evidence": _delivery_evidence_json(receipt.delivery, "reviewer-launch"),
        }

    def worker_status(self, task: dict[str, Any], record: DispatcherRecord) -> dict[str, Any]:
        return _command_terminal_status(self, task, record, kind="worker")

    def review_status(self, task: dict[str, Any], record: DispatcherRecord) -> dict[str, Any]:
        return _command_terminal_status(self, task, record, kind="review")

    def stop_review(self, record: DispatcherRecord, initiator: str = STOPPED_BY_DISPATCHER) -> None:
        """End the reviewer's lifecycle alone."""
        if self.mode == "noop" or not (record.review_handle or record.review_leaf or record.review_pid_file):
            return
        self.stop_head(record, "review", initiator)

    def head_commit(self, record: DispatcherRecord) -> str:
        """Commit the workspace checkout currently sits on, or "" when it cannot be read. Pinned
        at review start and re-read at merge time so a verdict can be tied to a code state."""
        if self.mode == "noop" or not record.workspace:
            return ""
        try:
            completed = self._run(["git", "-C", record.workspace, "rev-parse", "HEAD"], "review head sha")
        except HostError:
            return ""
        return completed.stdout.strip()

    def round_workspace(self, task: dict[str, Any], record: DispatcherRecord) -> WorkspaceReading:
        """The worker checkout's commits, diff and newest change since its integration base."""
        if self.mode == "noop" or not record.workspace:
            return WorkspaceReading(unknown="no workspace to read")
        try:
            base = self.catalog.integration_base(task["project"], task.get("workspace", {}).get("base_branch"))
        except HostError as exc:
            return WorkspaceReading(unknown=str(exc))
        return read_round_workspace(self._run, record.workspace, f"origin/{base}")

    def reconcile_reviewed_base_move(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        reviewed_commit: str,
        current_commit: str,
    ) -> dict[str, str | int] | None:
        """Accept only base-owned history that leaves every reviewed path unchanged.

        All refs are local objects already fetched by the gate. Any missing or unreadable
        answer denies reconciliation.
        """
        if self.mode == "noop" or not record.workspace:
            return None
        workspace = record.workspace
        try:
            base = self.catalog.integration_base(
                task["project"], task.get("workspace", {}).get("base_branch")
            )
            base_sha = self._run(
                ["git", "-C", workspace, "rev-parse", "--verify", f"origin/{base}^{{commit}}"],
                "review reconciliation base",
            ).stdout.strip()
            if not all(_is_exact_sha(sha) for sha in (reviewed_commit, current_commit, base_sha)):
                return None
            self._run(
                ["git", "-C", workspace, "merge-base", "--is-ancestor", reviewed_commit, current_commit],
                "review reconciliation ancestry",
            )
            unreviewed = self._run(
                ["git", "-C", workspace, "rev-list", "--no-merges", f"{reviewed_commit}..{current_commit}", "--not", base_sha],
                "review reconciliation candidate commits",
            ).stdout.strip()
            if unreviewed:
                return None
            fork = self._run(
                ["git", "-C", workspace, "merge-base", reviewed_commit, base_sha],
                "review reconciliation fork",
            ).stdout.strip()
            if not _is_exact_sha(fork):
                return None
            changed = self._run(
                ["git", "-C", workspace, "diff", "--name-only", "-z", "--no-renames", fork, reviewed_commit],
                "review reconciliation reviewed paths",
            ).stdout
            if changed and not changed.endswith("\0"):
                return None
            paths = changed.split("\0")[:-1] if changed else []
            if paths:
                self._run(
                    ["git", "-C", workspace, "diff", "--quiet", reviewed_commit, current_commit, "--", *paths],
                    "review reconciliation path contents",
                )
        except (HostError, KeyError, TypeError, UnicodeError):
            return None
        return {
            "reviewed_sha": reviewed_commit,
            "head_sha": current_commit,
            "base_sha": base_sha,
            "reviewed_paths": len(paths),
        }

    def gate_check(self, task: dict[str, Any], record: DispatcherRecord) -> GateResult:
        self._require_production_runtime("candidate-gate-before")
        self._require_worker_admission(task, record)
        if record.workspace:
            self._decide_workspace_environment_ownership(record.workspace)
        result = _gate_check(self, task, record)
        self._require_production_runtime("candidate-gate-after")
        return result

    def rerun_failed_ci(self, task: dict[str, Any], record: DispatcherRecord, result: GateResult) -> None:
        """The gate owns the GitHub Actions write needed to retry its classified failed run."""
        _rerun_failed_ci(self, result)

    def verify_worker_result(self, task: dict[str, Any], record: DispatcherRecord) -> None:
        if self.mode == "noop":
            return
        workspace = Path(record.workspace)
        if not workspace.is_dir():
            raise HostError("worker workspace is missing")
        if not has_candidate(task):
            # No candidate is published for a research/infra card, and its report artifacts may sit
            # uncommitted in the checkout until the release moves them.
            return
        completed = self._run(
            ["git", "-C", str(workspace), "status", "--porcelain"],
            "git status",
        )
        if durability_dirt(completed.stdout):
            raise HostError("worker reported done with uncommitted changes")
        from ummanu.broad_check import BroadCheckError, admitted_check
        from ummanu.check_commands import completion_check
        from ummanu.config import ConfigError

        try:
            project = str(task.get("project") or "")
            evidence = completion_check(project, workspace, self.catalog.instance_dir) if project else None
            if evidence is not None:
                accepted = admitted_check(_round_worker_check(
                    self._card_audit(), task["ref"],
                    _round_report_ids(record.workspace, record.attempt_id, task["ref"], record.report_generation),
                ))
                if accepted is None:
                    raise HostError("accepted report has no worker admission evidence; drain pre-policy reports before activation")
                if accepted["snapshot_digest"] != evidence["snapshot_digest"]:
                    raise HostError("candidate/receipt differs from the immutable accepted report")
        except (BroadCheckError, ConfigError) as exc:
            raise HostError(f"worker completion evidence unavailable: {exc}") from exc

    def _require_worker_admission(self, task: dict[str, Any], record: DispatcherRecord) -> None:
        """Recovered Validate/review needs the original admission, not a new worker run.

        Retain original admission; do not upgrade recovery to gate attestation.
        """
        if self.mode == "noop" or not has_candidate(task):
            return
        project = str(task.get("project") or "")
        if not project or "broad_check" not in self.catalog.adapter(project):
            return
        from ummanu.broad_check import admitted_check

        accepted = admitted_check(_round_worker_check(
            self._card_audit(), task["ref"],
            _round_report_ids(record.workspace, record.attempt_id, task["ref"], record.report_generation),
        ))
        if accepted is None:
            raise HostError("required immutable worker admission evidence is missing or damaged; drain pre-policy records before activation")
        sha = str(accepted.get("candidate_sha") or "")
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
            raise HostError("worker admission has no exact candidate SHA")
        tree = self._run(["git", "-C", record.workspace, "rev-parse", f"{sha}^{{tree}}"],
                         "worker admitted tree").stdout.strip()
        if tree != accepted.get("tree_sha"):
            raise HostError("worker admitted commit does not match its receipt tree")
        self._run(["git", "-C", record.workspace, "merge-base", "--is-ancestor", sha, "HEAD"],
                  "worker admission ancestry")

    def retained_workspace_state(self, task: dict[str, Any], record: DispatcherRecord) -> dict[str, Any]:
        """Describe the retained worker checkout of a card recovered without a live head."""
        expected_branch = _legacy_worker_branch(task["ref"])
        workspace = record.workspace or self.restore_workspace(task, record.worker)
        state: dict[str, Any] = {
            "workspace": workspace,
            "expected_branch": expected_branch,
            "branch": "",
            "dirty": None,
            "sha": "",
            "bound": False,
            "reason": "",
            "detail": "",
        }
        if self.mode == "noop":
            state.update(bound=True, branch=expected_branch, dirty=False)
            return state
        if not workspace or not Path(workspace).is_dir():
            state["reason"] = "workspace_missing"
            return state
        try:
            self._validate_resumable_workspace(task, workspace)
        except HostError as exc:
            state["reason"] = "workspace_unbindable"
            state["detail"] = scrub_host_output(str(exc))
            return state
        state["branch"] = expected_branch
        try:
            tree = self._run(
                ["git", "-C", workspace, "status", "--porcelain"], "retained workspace tree"
            ).stdout
            sha = self._run(
                ["git", "-C", workspace, "rev-parse", "HEAD"], "retained workspace candidate"
            ).stdout.strip()
        except HostError as exc:
            state["reason"] = "workspace_unreadable"
            state["detail"] = scrub_host_output(str(exc))
            return state
        state["dirty"] = bool(durability_dirt(tree))
        state["sha"] = sha
        if not sha:
            state["reason"] = "candidate_unknown"
            return state
        state["bound"] = True
        return state

    def restore_workspace(self, task: dict[str, Any], worker: str) -> str:
        """Where this card's worker checkout lives, new or already cut.

        Always `<data_dir>/workspaces/<project id>/<worker>`, the git worktree `GitWorkspaceManager`
        places, whatever the card's heads are: placement asks no runtime.
        """
        if self.mode == "noop":
            return str(self.data_dir / "dispatcher" / "workspaces" / worker)
        return str(self._git_workspaces.path(str(task.get("project") or ""), worker))

    @property
    def _git_workspaces(self) -> GitWorkspaceManager:
        return GitWorkspaceManager(self)

    def _is_git_workspace(self, workspace: str) -> bool:
        """Whether an existing or placed workspace is git-managed, read from its path alone."""
        if self.mode == "noop" or not workspace:
            return False
        return self._git_workspaces.owns(workspace)

    def _legacy_workspace(self, workspace: str) -> bool:
        """Whether a recorded card workspace is an Orca worktree: under the Orca workspaces root and
        not a git worktree this host owns. The root is read to recognise such a record, never to
        place one."""
        if self.mode == "noop" or not workspace or self._is_git_workspace(workspace):
            return False
        return _resolved_path(Path(workspace)).is_relative_to(_resolved_path(orca_workspaces_root()))

    def _refuse_legacy_record(self, record: DispatcherRecord, verb: str) -> None:
        """Raise `LegacyDispatcherRecord` when this card's record was written on Orca."""
        if self.mode == "noop":
            return
        subject = f"the dispatcher record of {record.worker or record.head or 'an unnamed card'}"
        if self._legacy_workspace(record.workspace):
            raise LegacyDispatcherRecord(
                subject, f"its workspace {record.workspace} is an Orca worktree", verb=verb
            )
        for role, stored in (("worker", record.worker_head_run), ("reviewer", record.review_head_run)):
            run = _durable_head_run(stored)
            if run is not None and is_legacy_record(run):
                raise LegacyDispatcherRecord(
                    subject, f"its {role} head run {run.run_id} is a legacy record", verb=verb
                )

    def _require_project_available(self, project: str) -> None:
        """Refuse before any project-dependent workspace or head activation."""
        if self.mode == "noop":
            return
        refusal = live_root_project_refusal(self.catalog, project)
        if refusal:
            raise HostError(refusal)
        availability_probe = getattr(self.catalog, "project_availability", None)
        if callable(availability_probe):
            availability = availability_probe(project)
        else:
            binding = dict(self.catalog.binding(project))
            binding["id"] = project
            availability = ProjectAvailability.inspect([binding])
        if not isinstance(availability, ProjectAvailability):
            raise HostError(f"project availability for {project!r} is invalid")
        if not availability.allows(project):
            raise HostError(f"project repo for {project!r} is unavailable")

    def complete_green(self, task: dict[str, Any], record: DispatcherRecord) -> MergeLanding | None:
        """Land the reviewed branch on the card's integration base and say what landed.

        Return MergeLanding only for a real integration-base merge; otherwise None.
        """
        with tick_stage("release_runtime"):
            self._require_production_runtime("release-before")
        if self.mode == "noop" or not record.workspace:
            return None
        self._decide_workspace_environment_ownership(record.workspace)
        if os.environ.get("UMMANU_DISPATCHER_AUTOMERGE", "on").strip().lower() == "off":
            return None
        # Admission refuses such a card; one claimed before that refusal existed lands nowhere.
        refusal = live_root_project_refusal(self.catalog, str(task.get("project") or ""))
        if refusal:
            raise HostError(f"nothing was merged: {refusal}")
        branch = _legacy_worker_branch(task["ref"])
        base = self.catalog.integration_base(task["project"], task.get("workspace", {}).get("base_branch"))
        ci = _validation_ci(self, task)
        if ci == "github":
            try:
                self._merge_github_pr(task, record, branch, base)
            except ProductionActivationRefused as exc:
                # The pull request is merged; only the production activation was refused.
                exc.landing = MergeLanding(
                    sha=self._landed_pr_commit(record, branch),
                    base=base,
                    path="github-pr",
                    ci=ci,
                    branch=branch,
                )
                raise
            with tick_stage("release_runtime"):
                self._require_production_runtime("release-after")
            return MergeLanding(
                sha=self._landed_pr_commit(record, branch),
                base=base,
                path="github-pr",
                ci=ci,
                branch=branch,
            )
        repo = Path(str(self.catalog.binding(task["project"])["repo"])).expanduser()
        # Publish onto the card's integration base (a non-fast-forward push is rejected, never
        # force-landed), then fast-forward the checkout: that is how a merged self-modification
        # reaches the next oneshot tick. The base is read from the card rather than hard-coded to
        # `main`: `integration_bases` makes a non-default base sanctioned for the first time
        # (secretary-1541), and pushing a card that declared one onto `main` anyway would land an
        # increment on a branch it was never validated against.
        project = task["project"]
        with tick_stage("release_merge"):
            self._remote_git_checked(
                project, record.workspace, ["push", "origin", f"{branch}:{base}"], "merge push"
            )
        try:
            with tick_stage("release_refresh"):
                self._remote_git_checked(project, repo, ["fetch", "origin", base], "post-merge fetch")
                self._advance_checkout(repo, f"origin/{base}")
        except ProductionActivationRefused as exc:
            # The push landed; only the production activation was refused.
            exc.landing = MergeLanding(
                sha=self._pushed_branch_head(record, branch), base=base, path="push", ci=ci
            )
            raise
        except HostError:
            # The push already landed. Only the production checkout's refresh activates the merged
            # code, so only its failure stays a release failure for the next tick to replay.
            if self._is_production_checkout(repo):
                raise
        with tick_stage("release_runtime"):
            self._require_production_runtime("release-after")
        return MergeLanding(sha=self._pushed_branch_head(record, branch), base=base, path="push", ci=ci)

    def _advance_checkout(self, repo: Path, ref: str) -> None:
        """Fast-forward a project checkout to `ref` after a merge."""
        if self._is_production_checkout(repo):
            production_checkout.advance(self._run, repo, ref, instance_dir=self.catalog.instance_dir)
            return
        self._run(["git", "-C", str(repo), "merge", "--ff-only", ref], "post-merge fast-forward")

    def _is_production_checkout(self, repo: Path) -> bool:
        """Whether `repo` is the checkout the dispatcher itself runs from."""
        return self.mode != "noop" and _same_repo(repo, Path(self.production_runtime.product_root))

    def _pushed_branch_head(self, record: DispatcherRecord, branch: str) -> str:
        """The commit a `branch:base` push just landed. A non-fast-forward push is rejected, so after
        a successful one the base is exactly this branch head."""
        with tick_stage("release_landed"):
            try:
                return self._run(
                    ["git", "-C", record.workspace, "rev-parse", branch], "post-merge landed commit"
                ).stdout.strip()
            except HostError:
                return ""

    def _landed_pr_commit(self, record: DispatcherRecord, branch: str) -> str:
        """The merge commit a pull request merge just landed, or "" when it cannot be read yet."""
        with tick_stage("release_landed"):
            return pr_merge_commit(self._run, branch, Path(record.workspace))

    def _prepare_pr_merge(self, record: DispatcherRecord, branch: str, base: str) -> None:
        """Refuse the merge unless the open pull request for `branch` targets `base`.

        A draft pull request is marked ready: the pipeline owns it, and GitHub refuses to merge a draft.
        """
        workspace = Path(record.workspace)
        completed = self._run(
            ["gh", "pr", "view", branch, "--json", "baseRefName,isDraft"], "merge pr base", cwd=workspace
        )
        try:
            view = json.loads(completed.stdout or "")
            observed, draft = view["baseRefName"], view["isDraft"]
        except (ValueError, TypeError, KeyError) as exc:
            raise HostError(
                f"pull request for {branch!r} is unreadable ({completed.stdout!r}); nothing was merged"
            ) from exc
        if observed != base:
            raise HostError(
                f"pull request for {branch!r} targets {observed or '(unreadable)'!r}, not the "
                f"integration base {base!r}; nothing was merged"
            )
        if draft:
            self._run(["gh", "pr", "ready", branch], "merge pr ready", cwd=workspace)

    def _merge_github_pr(
        self, task: dict[str, Any], record: DispatcherRecord, branch: str, base: str
    ) -> None:
        """Land a github-CI project through its PR, then fast-forward the project's own checkout so the next
        worktree bases on the merged tree.

        Verify PR base before merge; local refresh is best-effort except production activation refusal.
        """
        with tick_stage("release_merge"):
            self._prepare_pr_merge(record, branch, base)
            self._run(["gh", "pr", "merge", branch, "--merge"], "merge pr", cwd=Path(record.workspace))
        repo = Path(str(self.catalog.binding(task["project"])["repo"])).expanduser()
        default_branch = self.catalog.project_default_branch(task["project"])
        # `gh pr merge` is the irreversible delivery boundary. Refreshing this checkout afterwards is
        # only a convenience for future worktree bases, and a preserved local commit can make
        # ff-only impossible: never report an already-merged card as failed because it did not apply.
        try:
            refresh_branch = base if base == default_branch else default_branch
            with tick_stage("release_refresh"):
                self._remote_git_checked(
                    task["project"], repo, ["fetch", "origin", refresh_branch], "post-merge fetch"
                )
                self._advance_checkout(repo, f"origin/{refresh_branch}")
        except ProductionActivationRefused:
            raise
        except HostError:
            pass

    def teardown(self, record: DispatcherRecord) -> dict[str, Any] | None:
        """Request owned Done cleanup and return its actual durable disposition.

        Code delivery and cleanup settlement are separate facts. Pending stop or
        Git failures retain every owner in the journal for dispatcher retry.
        """
        if self.mode == "noop" or not record.workspace:
            return
        self._refuse_legacy_record(record, "tear down")
        self._require_production_runtime("cleanup-before-stop")
        owner = getattr(self, "cleanup_owner", None)
        if owner is None:
            raise HostError("teardown has no durable cleanup owner")
        # Release owns the subsequent Done transition and claim settlement. A
        # failed cleanup remains replayable even after that record is discarded.
        subject = f"the dispatcher record of {record.worker or record.head or 'an unnamed card'}"
        task_ref = ""
        for role, raw in (("worker", record.worker_head_run), ("reviewer", record.review_head_run)):
            stored = raw.get("task_ref", {})
            if not isinstance(stored, dict):
                # A null or scalar task_ref names no card; refuse by name instead of crashing.
                raise HostError(f"cleanup refuses {subject}: its {role} head run carries task_ref {stored!r}")
            if stored.get("ref"):
                task_ref = stored["ref"]
                break
        if not task_ref:
            task_ref = next((ref for ref, raw in owner._state().get("records", {}).items()
                             if raw.get("attempt_id") == record.attempt_id and raw.get("worker") == record.worker), "")
        if not task_ref:
            raise HostError("cleanup cannot identify the exact card")
        result = owner.cleanup(owner.runtime.reader.show(task_ref), record, "done")
        return {"status": result["status"], "reason": result["reason"], "progress": result["progress"]}

    def production_runtime_provenance(self, within: Callable[[], float] | None = None) -> RuntimeProvenance:
        """Structured, secret-free observation used by every production runtime fence; its probe
        child waits only within a caller's deadline, when one is given."""
        return self.production_runtime.probe() if within is None else self.production_runtime.probe(within)

    def _require_production_runtime(self, boundary: str,
                                    within: Callable[[], float] | None = None) -> RuntimeProvenance:
        if self.mode == "noop":
            return RuntimeProvenance(
                "valid",
                self.production_runtime.interpreter,
                self.production_runtime.product_root,
                "noop",
                (),
            )
        result = self.production_runtime_provenance() if within is None else self.production_runtime_provenance(within)
        if not result.valid:
            error = HostError(result.refusal(boundary))
            error.evidence = result.as_dict()
            raise error
        return result

    def _launch_admission(self, task: dict[str, Any] | None) -> contextlib.AbstractContextManager[Any]:
        owner = self.cleanup_owner
        if owner is None or task is None:
            return contextlib.nullcontext()
        return owner.admission(task, launch=True)

    def _launch(
        self,
        workspace: str,
        title: str,
        head: str,
        prompt_file: str,
        *,
        role: str,
        env_name: str,
        launch_prompt: str | None = None,
        prompt_document: str = "",
        task: dict[str, Any] | None = None,
        failover: bool = False,
        heartbeat_run_id: str = "",
        local_run_policy: tuple[dict[str, Any] | None, bool] | None = None,
        handoff: PromptHandoff | None = None,
    ) -> LaunchedHead:
        """Bring one head up and hand back the pane together with the configuration it started with.

        A failed bring-up after opening a pane carries its stage evidence.
        """
        if role in {WORKER_ROLE, REVIEW_ROLE, "reviewer"}:
            self._require_production_runtime(f"{role}-launch")
            self._require_workspace_environment(workspace)
        role = head_role(role)
        pid_file = _pid_file_path(_watchdog_kind(role), task["ref"]) if task else ""
        task_ref = self._task_ref(task, role, prompt_document)
        run_id = heartbeat_run_id or head_ops.new_run_id()
        # `preflight_codex_run` is deliberately reached even by noop: a fake transport is not an
        # exemption from the policy boundary. A refused attestation opens no pane and clears no
        # predecessor state.
        if self.mode == "noop":
            try:
                preflight_run = self._preflight_launch_run(
                    head,
                    role=role,
                    workspace=workspace,
                    task_ref=task_ref,
                    pid_file=pid_file,
                    run_id=run_id,
                )
            except CodexFanoutPolicyError as exc:
                raise HostError(str(exc)) from None
            preflight_run = self._capture_launch_prompt_identity(
                preflight_run, role=role, document=prompt_document
            )
            return self._launched(
                f"noop:{head}:{Path(workspace).name}:{Path(prompt_file).name}",
                head,
                task,
                role,
                workspace,
                failover,
                head_run=preflight_run.to_json(),
            )
        heartbeat = heartbeat_identity(
            run_id=run_id,
            role=role,
            task_ref=task_ref.to_json(),
        )
        try:
            preflight_run = self._preflight_launch_run(
                head,
                role=role,
                workspace=workspace,
                task_ref=task_ref,
                pid_file=pid_file,
                run_id=run_id,
            )
        except CodexFanoutPolicyError as exc:
            raise HostError(str(exc)) from None
        preflight_run = self._capture_launch_prompt_identity(
            preflight_run, role=role, document=prompt_document
        )
        memory_identity: dict[str, str] | None = None
        project = str((task or {}).get("project") or "")
        if task is not None and role in {"worker", "reviewer"} and project:
            try:
                grant = memory_access.issue_grant(
                    preflight_run,
                    memory_access.card_subject(str(task.get("ref") or ""), project),
                    data_dir=self.data_dir,
                )
            except memory_access.MemoryAccessError as exc:
                raise HostError(f"memory access binding could not be issued: {exc}") from None
            memory_identity = grant.launch_identity
        # A worker or reviewer head writes the board as the profile it runs (`BOARD_ACTOR`). The
        # memory bearer is not part of the command: sudo logs the command line of a scoped launch to
        # the system journal. It reaches the head through the environment the backend is handed.
        launch_identity: dict[str, str] = {}
        if role in {"worker", "reviewer"}:
            launch_identity[BOARD_ACTOR_ENV] = head
        # The backend is the profile's, whatever adapter the command turns out to run.
        heartbeat_owner = self.head_runtime_for(self._head_spec(head, ""))
        if pid_file and not _runtime_writes_launch_identity(heartbeat_owner):
            # Drop any pid a previous launch in this workspace left behind, so a respawn cannot read
            # a dead predecessor's pid as this launch's liveness before the new head overwrites it.
            # A runtime that writes the identity itself reads this file first, to refuse a bring-up
            # over a live head of the same run, and the head it raises then replaces it.
            _clear_head_heartbeat(pid_file)
        command = os.environ.get(env_name)
        launch = HeadCommand(command) if command else None
        if command:
            # A raw command override bypasses the catalog launcher and its pid heartbeat wrapper:
            # deliberate for tests and manual overrides, with the inactivity ceiling as the fallback.
            self.catalog.prepare_head_workspace(head, workspace, role=role)
        else:
            policy_binding: dict[str, str] = {}
            if role in {"worker", "reviewer"}:
                policy, _ = local_run_policy if local_run_policy is not None else (None, bool((task or {}).get("sprint")))
                if policy is not None:
                    policy_binding["local_run_policy"] = json.dumps(policy, ensure_ascii=True)
            launch = self.catalog.head_launch(
                head,
                prompt_file,
                workspace=workspace,
                role=role,
                launch_prompt=launch_prompt,
                identity=launch_identity or None,
                **policy_binding,
            )
            command = launch.command
            if pid_file:
                command = _launch_command(heartbeat_owner, command, pid_file, heartbeat)
        adapter = (getattr(launch, "adapter", "") or "codex") if launch else "codex"
        ingress = self._codex_provider_ingress(preflight_run)
        subject = f"{role or 'head'}-launch"
        pointer = None
        if launch and launch.prompt_after_start:
            # Which of the two prompt shapes this head is in is decided by the rendered command, not
            # the profile: a raw command override runs a provider no profile describes. An empty
            # pointer text is the legacy shape, not an empty prompt — that head is sent the prompt
            # file's own contents; a caller with a task document passes the bounded line naming it.
            pointer = head_ops.NudgePointer(text=launch_prompt or "", document=prompt_document)
        spec = self._head_spec(head, adapter)
        backend = self.head_runtime_for(spec)
        if not _takes_handoffs(backend):
            handoff = None
        receipt = backend.start(
            spec,
            workspace,
            task_ref,
            command=command,
            title=title,
            pointer=pointer,
            env=dict(memory_identity or {}),
            pid_file=pid_file,
            transport=self._head_transport(
                workspace,
                prompt_file,
                adapter,
                role,
                before_send=ingress.bind_before_delivery if ingress is not None else None,
            ),
            subject=subject,
            run_id=run_id,
            role=role,
            run=preflight_run,
            scope_generation=preflight_run.scope_generation,
            launch_admission=lambda: self._launch_admission(task),
            commit=ingress.commit_run if ingress is not None else None,
            **({"handoff": handoff} if handoff is not None and pointer is not None else {}),
        )
        if not receipt.ok:
            failed_run = receipt.run
            if pid_file and failed_run is not None and failed_run.leaf:
                # Delivery can refuse with a live pane before the bring-up returns normally. Bind
                # that pane to the written heartbeat before persisting the failed launch intent.
                _bind_head_heartbeat(pid_file, expected=heartbeat, leaf=failed_run.leaf)
            if receipt.handoff_stage and failed_run is not None:
                # Up, and its prompt is not taken yet: the intent keeps this exact head and the next
                # tick continues the same handoff (`retry_busy_reviewer_launch_delivery`).
                raise HeadLaunchAborted(
                    f"{subject} is pending: {receipt.reason}",
                    handle=failed_run.handle,
                    leaf=failed_run.leaf,
                    workspace=workspace,
                    pid_file=pid_file,
                    evidence=_delivery_evidence_json(receipt, subject),
                    head_run=failed_run.to_json(),
                ) from None
            if receipt.failure is None:
                # A refusal the boundary made on its own terms, before any operation ran: a
                # bring-up over a turn this runtime is still holding is the one that exists. It
                # carries no `HeadOperationError` to translate, so the reason is the message —
                # `_launch_failure(None, ...)` would have raised `HostError("None")`.
                raise HostError(receipt.reason) from None
            raise self._launch_failure(receipt.failure, workspace, pid_file, subject) from None
        if pid_file:
            # Pane create gives the leaf after the head wrote its base identity. A best-effort bind
            # is enough: the reader still requires the run, role and task binding to match.
            _bind_head_heartbeat(pid_file, expected=heartbeat, leaf=receipt.run.leaf)
        lifecycle_run = receipt.run
        if lifecycle_run.spec.adapter == "claude":
            # Claude creates its jsonl after its pane starts.  Capture the one transcript that the
            # pre-pane baseline identifies before routing records this bring-up, so the journal has
            # the provider's session id rather than asking analytics to reconstruct it from cwd.
            lifecycle_run = _bind_claude_provider_progress_source(lifecycle_run)
        delivery = receipt.delivery
        return self._launched(
            receipt.run.handle,
            head,
            task,
            role,
            workspace,
            failover,
            leaf=receipt.run.leaf,
            delivery_evidence=(_delivery_evidence_json(delivery, subject) if delivery is not None else {}),
            head_run=lifecycle_run.to_json(),
            fallback_reason=receipt.fallback_reason,
        )

    def _head_spec(self, head: str, adapter: str) -> HeadSpec:
        """The launch shape the run is recorded with, degrading rather than failing a live bring-up."""
        try:
            return HeadSpec.from_profile(head, self.catalog.head_profile(head))
        except (HeadSpecError, HostError, AttributeError, KeyError, TypeError):
            from ummanu.runtime.head.memory import DEFAULT_MEMORY_LIMIT_MIB

            return HeadSpec(
                profile_id=head, adapter=adapter or "unknown", runtime=LOCAL_PTY_RUNTIME,
                memory_limit_mib=DEFAULT_MEMORY_LIMIT_MIB,
            )

    @staticmethod
    def _task_ref(task: dict[str, Any] | None, role: str, document: str) -> head_ops.TaskRef:
        """What this head is being pointed at."""
        pointer = document if document and os.path.isabs(document) else ""
        if task and task.get("ref"):
            return head_ops.TaskRef.card(str(task["ref"]), document=pointer)
        return head_ops.TaskRef.standing(role or "head", document=pointer)

    def _capture_launch_prompt_identity(
        self, run: head_ops.HeadRun, *, role: str, document: str
    ) -> head_ops.HeadRun:
        """Attach the exact worker/reviewer document before a pane can observe it."""
        if role not in {WORKER_ROLE, REVIEW_ROLE, "reviewer"} or not document:
            return run
        path = Path(document)
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise HostError(f"launch prompt {path} could not be captured: {exc}") from None
        policy = dict(run.fanout_policy)
        policy["prompt_identity"] = {
            "path": str(path.resolve(strict=False)),
            "version": f"sha256:{hashlib.sha256(content).hexdigest()}",
        }
        captured = run.with_fanout_policy(policy)
        # The Codex ingress owns the exact run it hands back immediately before delivery. Keep
        # that handoff aligned with the launch-time prompt fact, or its later provider binding
        # would otherwise return the pre-capture record and erase this identity.
        ingress = self._codex_provider_ingresses.get(run.run_id)
        if ingress is not None:
            ingress.run = captured
        if self._prepared_provider_runs.get(run.run_id) is not None:
            self._prepared_provider_runs[run.run_id] = captured
        return captured

    def _head_transport(
        self,
        workspace: str,
        prompt_file: str = "",
        adapter: str = "",
        role: str = "",
        before_send: Callable[[], head_ops.HeadRun | None] | None = None,
        ack_out_of_band: bool = False,
    ) -> DispatcherHeadTransport:
        """This product's delivery and close semantics, for the operation to perform through
        the host it is running on.
        """
        return DispatcherHeadTransport(
            self,
            workspace,
            prompt_file,
            adapter,
            role,
            before_send,
            ack_out_of_band,
        )

    @staticmethod
    def _run_heartbeat_identity(run: head_ops.HeadRun, role: str) -> dict[str, str]:
        return heartbeat_identity(
            run_id=run.run_id,
            role=role,
            task_ref=run.task_ref.to_json(),
            leaf=run.leaf,
        )

    def _record_heartbeat_status(self, record: DispatcherRecord, kind: str) -> dict[str, Any]:
        field = "review_head_run" if kind == "review" else "worker_head_run"
        pid_file = record.review_pid_file if kind == "review" else record.worker_pid_file
        leaf = record.review_leaf if kind == "review" else record.worker_leaf
        return self._head_status(
            pid_file,
            run=getattr(record, field, {}),
            role=kind,
            leaf=leaf,
        )

    def _launch_failure(
        self, exc: head_ops.HeadOperationError, workspace: str, pid_file: str, subject: str
    ) -> Exception:
        """Translate one operation's refusal into the failure the dispatcher's callers already read."""
        evidence = _delivery_evidence_json(exc, subject)
        if isinstance(exc, head_ops.HeadSpawnAborted):
            return HeadLaunchAborted(
                str(exc),
                handle=exc.run.handle,
                leaf=exc.run.leaf,
                workspace=workspace or exc.run.workspace,
                pid_file=pid_file or exc.run.pid_file,
                evidence=evidence,
                head_run=exc.run.to_json(),
            )
        failure = HostError(str(exc))
        failure.evidence = evidence
        return failure

    def _launched(
        self,
        handle: str,
        head: str,
        task: dict[str, Any] | None,
        role: str,
        workspace: str = "",
        failover: bool = False,
        leaf: str = "",
        delivery_evidence: dict[str, Any] | None = None,
        head_run: dict[str, Any] | None = None,
        fallback_reason: str = "",
    ) -> LaunchedHead:
        """Pair the pane with the launch snapshot of the head running in it."""
        if task is None:
            return LaunchedHead(
                handle=handle,
                head=head,
                leaf=leaf,
                delivery_evidence=dict(delivery_evidence or {}),
                head_run=dict(head_run or {}),
                fallback_reason=fallback_reason,
            )
        try:
            run = self.catalog.head_run(
                task, role=role, head=head, workspace=workspace, failover=failover
            ).to_json()
        except (HostError, AttributeError, KeyError, TypeError):
            run = HeadRun(role=role, head=head, adapter="unknown", model_source=MODEL_UNKNOWN).to_json()
        return LaunchedHead(
            handle=handle,
            head=head,
            run=run,
            leaf=leaf,
            delivery_evidence=dict(delivery_evidence or {}),
            head_run=dict(head_run or {}),
            fallback_reason=fallback_reason,
        )

    def head_runtime_for(self, subject: Any = None) -> Any:
        """The backend this head is held by — the one place a `runtime` value becomes an object.

        Observe, deliver and stop through its persisted backend.
        """
        return self._head_runtime_named(_head_runtime_name(subject))

    def _head_runtime_named(self, name: str) -> Any:
        """Build — or hand back — the one instance of the backend called `name`."""
        held = self._head_runtimes.get(name)
        if held is not None:
            return held
        try:
            built = build_head_runtime(
                name,
                local_pty_root=self._local_pty_root,
                head_process_status=_head_process_status,
            )
        except LegacyHeadRecordError as exc:
            raise LegacyDispatcherRecord("a head run", str(exc), verb="drive") from exc
        except UnknownHeadRuntimeError as exc:
            raise HostError(str(exc)) from exc
        self._head_runtimes[name] = built
        return built

    def _local_pty_root(self) -> Path:
        """Where this dispatcher's supervised heads keep their run directories."""
        return self.data_dir / "heads"

    def _open_head_pane(
        self,
        run: head_ops.HeadRun,
        title: str,
        command: str,
        *,
        env: Mapping[str, str] | None = None,
        launch_task: dict[str, Any] | None = None,
    ) -> head_ops.HeadRun:
        """Bring a head up in a pane of its own, with no prompt delivered by the bring-up."""
        receipt = self.head_runtime_for(run).start(
            run.spec,
            run.workspace,
            run.task_ref,
            command=command,
            title=title,
            env=dict(env or {}),
            pid_file=run.pid_file,
            run_id=run.run_id,
            role=run.role,
            run=run,
            scope_generation=run.scope_generation,
            launch_admission=lambda: self._launch_admission(launch_task),
        )
        if not receipt.ok:
            raise HostError(receipt.reason)
        return receipt.run

    def prompt_worker_report(self, task: dict[str, Any], record: DispatcherRecord) -> None:
        """Ask a live worker to run the open round's ordinary report command. Nothing else."""
        self._refuse_legacy_record(record, "deliver to the worker of")
        status = self._head_status(
            record.worker_pid_file,
            run=record.worker_head_run,
            role="worker",
            leaf=record.worker_leaf,
        )
        if not _heartbeat_is_live_match(status):
            raise HostError("worker session exited")
        if status.get("stopped"):
            raise HostError("worker session is suspended and cannot take a report prompt")
        if not self._continuation_addressable(record):
            raise HostError("worker session cannot accept a report prompt")
        workspace = Path(record.workspace)
        if not workspace.is_dir():
            raise HostError("worker workspace is missing")
        prompt = _report_nudge_prompt(record.report_generation, task["ref"])
        self._nudge_worker(
            record,
            head_ops.NudgePointer.line(prompt),
            "worker report prompt",
            subject="worker-report",
        )

    def worker_takes_comments(self, record: DispatcherRecord) -> bool:
        """Whether this card's worker is live, running and addressable, so a comment pointer can reach it now."""
        if self.mode == "noop" or not self._continuation_addressable(record):
            return False
        if not record.workspace or not Path(record.workspace).is_dir():
            return False
        status = self._head_status(
            record.worker_pid_file,
            run=record.worker_head_run,
            role="worker",
            leaf=record.worker_leaf,
        )
        return _heartbeat_is_live_match(status) and not status.get("stopped")

    def deliver_worker_comments(self, task: dict[str, Any], record: DispatcherRecord) -> None:
        """Rewrite the live worker's TASK.md with the card's comments, then point it there."""
        self._refuse_legacy_record(record, "deliver to the worker of")
        workspace = Path(record.workspace)
        if not workspace.is_dir():
            raise HostError("worker workspace is missing")
        base = self.catalog.integration_base(task["project"], task.get("workspace", {}).get("base_branch"))
        local_run_policy = self._frozen_local_run_policy(
            task, WORKER_ROLE, record.report_generation, record.worker_local_run_snapshot
        )
        self._write_prompt(
            workspace / "TASK.md",
            self._worker_task_doc(
                task,
                base,
                record.attempt_id,
                record.report_generation,
                record.report_decision,
                record.report_protocol_prerequisites,
                record=record,
                local_run_policy=local_run_policy,
            ),
        )
        try:
            pointer = head_ops.NudgePointer.at_document(
                str(workspace / "TASK.md"), worker_comments_note(record.report_generation)
            )
        except PromptDocumentError as exc:
            raise HostError(f"the comment pointer could not be built: {exc}") from None
        self._nudge_worker(record, pointer, "worker comment continuation", subject="worker-comments")

    def resume_worker(
        self, task: dict[str, Any], record: DispatcherRecord, *, handoff_started: str = ""
    ) -> None:
        """Resume an addressable retained worker and deliver its updated rework task.

        An uncertain handoff retains the bound run and pending stage.
        """
        self._refuse_legacy_record(record, "resume the worker of")
        status = self._head_status(
            record.worker_pid_file,
            run=record.worker_head_run,
            role="worker",
            leaf=record.worker_leaf,
        )
        if not _heartbeat_is_live_match(status):
            raise HostError("retained worker session exited")
        if not self._continuation_addressable(record):
            raise HostError("retained worker session cannot accept a continuation")
        adapter = self._prompt_adapter(record.worker_run, record.head)
        workspace = Path(record.workspace)
        if not workspace.is_dir():
            raise HostError("retained worker workspace is missing")
        continuation = record.worker_continuation
        if continuation.delivery_confirmed:
            if status.get("stopped"):
                raise HostError("confirmed retained continuation is no longer running")
            return
        handoff = continuation.handoff
        if handoff is not None and not handoff_started:
            handoff_started = self.worker_handoff_started(record)
        if handoff is not None and handoff_started == HANDOFF_DEFERRED:
            pending = HostError("retained worker continuation is pending: its supervisor was not read this pass")
            pending.evidence = {"subject": "worker-continuation", HANDOFF_STAGE_KEY: HANDOFF_SETTLE}
            raise pending
        if handoff is not None and handoff_started != HANDOFF_NOT_STARTED:
            if status.get("stopped"):
                # It was woken for this line; suspended again since, it is not this handoff's head.
                raise HostError("retained worker was suspended again while its continuation was pending")
            try:
                pointer = head_ops.NudgePointer.at_document(
                    str(workspace / "TASK.md"),
                    _continuation_note(record.report_generation, record.report_decision),
                )
            except PromptDocumentError as exc:
                raise HostError(f"the continuation pointer could not be built: {exc}") from None
            self._nudge_worker(
                record, pointer, "retained worker continuation", subject="worker-continuation", handoff=handoff
            )
            return
        if not status.get("stopped") and (
            _provider_turn_started(str(workspace), continuation.sent_at, adapter=adapter) is True
        ):
            # The dispatcher may have died after the provider started but before it recorded that
            # confirmation. Returning lets recovery checkpoint it without touching TASK.md.
            return
        base = self.catalog.integration_base(task["project"], task.get("workspace", {}).get("base_branch"))
        # One generation and one decision, read once: the document the worker is sent back to and
        # the prompt that sends it there name the same round and adjudication because they share
        # these values, not because separate call sites happen to agree.
        generation = record.report_generation
        decision = record.report_decision
        protocol_prerequisites = record.report_protocol_prerequisites
        local_run_policy = self._frozen_local_run_policy(
            task, WORKER_ROLE, generation, record.worker_local_run_snapshot
        )
        self._clear_report_bodies(task["ref"])
        self._write_prompt(
            workspace / "TASK.md",
            self._worker_task_doc(
                task,
                base,
                record.attempt_id,
                generation,
                decision,
                protocol_prerequisites,
                record=record,
                local_run_policy=local_run_policy,
            ),
        )
        # The continuation travels as a pointer at the document just written, not as the round typed
        # into the composer: that is the delivery shape that has never lost a prompt. It is built
        # before the wake-up, because finding out after SIGCONT leaves a woken head nothing to read.
        try:
            pointer = head_ops.NudgePointer.at_document(
                str(workspace / "TASK.md"), _continuation_note(generation, decision)
            )
        except PromptDocumentError as exc:
            raise HostError(f"the continuation pointer could not be built: {exc}") from None
        activate = None
        if status.get("stopped"):
            # The delivery transport waits for `tui-idle` before this callback: a readiness timeout
            # saying the pane is busy leaves the retained HeadRun frozen, never SIGCONT'd.
            activate = lambda: self._signal_head(
                record.worker_pid_file,
                signal.SIGCONT,
                run=record.worker_head_run,
                role="worker",
                leaf=record.worker_leaf,
            )
        self._nudge_worker(
            record,
            pointer,
            "retained worker continuation",
            subject="worker-continuation",
            before_send=activate,
            handoff=handoff,
        )

    def head_handoffs(self) -> bool:
        """Whether this host's worker and reviewer prompts are production handoffs inside a pass.

        True wherever it raises real heads. A noop host raises none, so it has no head to hand a
        prompt to and keeps the flow it always had.
        """
        return self.mode != "noop"

    def worker_handoffs(self, record: DispatcherRecord) -> bool:
        """Whether this card's retained worker takes its continuation as a production handoff."""
        if not self.head_handoffs():
            return False
        try:
            backend = self.head_runtime_for(self.worker_lifecycle_run(record))
        except (HostError, LegacyDispatcherRecord, LegacyHeadRecordError, UnknownHeadRuntimeError):
            return True
        return _takes_handoffs(backend)

    def worker_handoff_floor(self, record: DispatcherRecord) -> int | None:
        """The worker journal's sequence now: where a production continuation handoff opens.

        None when the worker's own supervisor does not answer, or this host raises no heads.
        """
        if self.mode == "noop":
            return None
        try:
            self._refuse_legacy_record(record, "open a handoff to the worker of")
            run = self.worker_lifecycle_run(record)
            runtime = self.head_runtime_for(run)
        except (HostError, LegacyDispatcherRecord, LegacyHeadRecordError, UnknownHeadRuntimeError):
            return None
        floor = getattr(runtime, "handoff_floor", None)
        return floor(run) if callable(floor) else None

    def worker_handoff_started(self, record: DispatcherRecord) -> str:
        """Whether this continuation's handoff has written to the worker: none, started, unknown, deferred.

        Use bounded supervisor evidence: none, started, unknown or deferred.
        """
        handoff = record.worker_continuation.handoff
        if handoff is None or self.mode == "noop":
            return HANDOFF_NOT_STARTED
        try:
            run = self.worker_lifecycle_run(record)
            runtime = self.head_runtime_for(run)
        except (HostError, LegacyDispatcherRecord, LegacyHeadRecordError, UnknownHeadRuntimeError):
            return HANDOFF_UNKNOWN
        started = getattr(runtime, "handoff_started", None)
        if not callable(started):
            return HANDOFF_UNKNOWN
        return str(started(run, "worker-continuation", handoff))

    def _nudge_worker(
        self,
        record: DispatcherRecord,
        pointer: head_ops.NudgePointer,
        what: str,
        *,
        subject: str,
        before_send: Callable[[], None] | None = None,
        handoff: PromptHandoff | None = None,
    ) -> None:
        """Point this card's live worker at one thing, through the head operation (secretary-1412).

        Keep uncertain handoff bound to its original run.
        """
        self._refuse_legacy_record(record, "deliver to the worker of")
        run = self.worker_lifecycle_run(record)
        ingress = self._codex_provider_ingress(run)
        if run.spec.adapter == "codex" and isinstance(run.fanout_policy.get("provider_source"), dict) and ingress:
            def activate_and_bind() -> head_ops.HeadRun:
                if before_send is not None:
                    before_send()
                try:
                    return ingress.bind_before_delivery()
                except Exception:  # noqa: BLE001 — provider telemetry cannot refuse a delivery
                    return ingress.run

            delivery_hook: Callable[[], head_ops.HeadRun | None] | None = activate_and_bind
        else:
            delivery_hook = before_send
        try:
            receipt = self.head_runtime_for(run).deliver(
                run,
                pointer,
                transport=self._head_transport(
                    record.workspace,
                    "TASK.md",
                    self._prompt_adapter(record.worker_run, record.head),
                    before_send=delivery_hook,
                ),
                subject=subject,
                **({"handoff": handoff} if handoff is not None else {}),
            )
        except LegacyDispatcherRecord:
            raise
        except (TuiDeliveryError, HostError) as exc:
            failure = HostError(f"{what} was not delivered: {exc}")
            failure.evidence = getattr(exc, "evidence", None)
            raise failure from None
        if receipt.handoff_stage:
            if receipt.run is not None:
                # What the delivery bound before typing (a Codex provider source) is this head's now.
                record.worker_head_run = receipt.run.to_json()
            pending = HostError(f"{what} is pending: {receipt.reason}")
            pending.evidence = _delivery_evidence_json(receipt, subject)
            raise pending from None
        if not receipt.ok:
            failure = HostError(f"{what} was not delivered: {receipt.reason}")
            failure.evidence = receipt.evidence
            if receipt.status == head_ops.HEAD_BUSY:
                # The head is in a turn and nothing was typed: a delivery to make once it ends.
                failure.evidence = {"readiness_state": READINESS_BUSY, "reason": receipt.reason}
            raise failure from None
        record.worker_head_run = receipt.run.to_json()
        evidence = _delivery_evidence_json(receipt.delivery, subject)
        source = receipt.run.fanout_policy.get("provider_source")
        if receipt.run.spec.adapter == "codex" and isinstance(source, dict):
            evidence["provider_bound"] = source.get("state") == "bound"
            evidence["provider_source_state"] = str(source.get("state") or "unknown")
        _record_worker_delivery_evidence(record, evidence)

    def _validated_worker_prerequisites(
        self,
        task: dict[str, Any],
        decision: str,
        expected: tuple[str, ...],
        *,
        decision_id: str,
    ) -> tuple[ProtocolArtifact, ...]:
        """Read only the structured declaration bound to the decision rendered for this round."""
        if not decision:
            return ()
        events = self._card_audit().events(str(task.get("ref") or ""))
        description = str(task.get("description") or "")
        revision = specification_revision(events, description)
        digest = hashlib.sha256(description.encode("utf-8")).hexdigest()
        for event in reversed(events):
            if event.get("event_id") != decision_id:
                continue
            data = event.get("data") if isinstance(event.get("data"), dict) else event.get("payload")
            if not isinstance(data, dict) or data.get("marker") != "decision:rework":
                continue
            body = data.get("body")
            if (
                not isinstance(body, str)
                or CommandHostRuntime._canonical_decision_binding(body)
                != CommandHostRuntime._canonical_decision_binding(decision)
                or data.get("specification_revision") != revision
                or data.get("description_sha256") != digest
                or data.get("protocol_prerequisites") != list(expected)
            ):
                continue
            try:
                return validate_rework_prerequisites(expected, specification_revision=revision or None)
            except (ValueError, ArtifactOwnershipViolation):
                return ()
        return ()

    @staticmethod
    def _canonical_decision_binding(body: str) -> str:
        """The one comparison form for raw observer decision bodies.

        Preserve raw submitted bodies; normalize only for comparisons.
        """
        return body.strip()

    @staticmethod
    def _bound_marker_body(
        task: dict[str, Any],
        events: list[dict[str, Any]],
        marker: str,
        revision: str,
        description_digest: str,
        required_body: str = "",
    ) -> str | None:
        """Return the latest uniquely located marker body with an exact spec binding."""
        comments = task.get("comments") or []
        for event in reversed(events):
            data = event.get("data") if isinstance(event.get("data"), dict) else event.get("payload")
            if not isinstance(data, dict) or data.get("marker") != marker:
                continue
            if (
                data.get("specification_revision") != revision
                or data.get("description_sha256") != description_digest
            ):
                continue
            body = data.get("body")
            occurrence = data.get("marker_occurrence")
            if (
                not isinstance(body, str)
                or not body.strip()
                or not isinstance(occurrence, int)
                or occurrence < 1
            ):
                return None
            if required_body and (
                CommandHostRuntime._canonical_decision_binding(body)
                != CommandHostRuntime._canonical_decision_binding(required_body)
            ):
                continue
            rendered = f"[{marker}]\n{body}"
            matches = [
                comment
                for comment in comments
                if comment.get("marker") == marker and comment.get("body") == rendered
            ]
            if len(matches) < occurrence:
                return None
            return CommandHostRuntime._canonical_decision_binding(body)
        return None

    def project_git_access(self, project: str) -> ProjectGitAccess:
        """Bounded, non-mutating proof of a registered project's remote Git access, before a claim."""
        if self.mode == "noop":
            return ProjectGitAccess("not-applicable", "unknown", "noop")
        try:
            repo = Path(str(self.catalog.binding(project)["repo"])).expanduser()
        except (KeyError, HostError) as exc:
            return ProjectGitAccess(
                "not-applicable", "unknown", "none", "project-unavailable", scrub_host_output(str(exc))[:240]
            )
        if not (repo / ".git").exists():
            return ProjectGitAccess(
                "not-applicable",
                "unknown",
                "none",
                "checkout-unavailable",
                "project checkout is not provisioned",
            )
        try:
            execution = project_remote_execution(
                repo, instance_dir=getattr(self.catalog, "instance_dir", None)
            )
        except CredentialError as exc:
            return ProjectGitAccess("refused", "unknown", "none", exc.code, str(exc))
        return execution.preflight_project(repo)

    def remote_git(
        self,
        project: str,
        checkout: str | Path,
        args: list[str],
        label: str,
        *,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run one remote Git operation of a registered project's checkout and return Git's answer."""
        checkout = Path(checkout)
        execution = self._project_remote(project, checkout)
        if execution.transport in {"local", "ssh", "unmanaged"}:
            return self.run_capture(["git", "-C", str(checkout), *args], label, extra_env=extra_env)
        return self._managed_remote_git(project, execution, checkout, args, label, extra_env=extra_env)

    def _remote_git_checked(
        self, project: str, checkout: str | Path, args: list[str], label: str
    ) -> subprocess.CompletedProcess[str]:
        """`remote_git` under `_run`'s contract: a non-zero exit is a HostError."""
        checkout = Path(checkout)
        execution = self._project_remote(project, checkout)
        if execution.transport in {"local", "ssh", "unmanaged"}:
            return self._run(["git", "-C", str(checkout), *args], label)
        completed = self._managed_remote_git(project, execution, checkout, args, label)
        if completed.returncode != 0:
            raise HostError(f"{label} failed: {_tail((completed.stderr or completed.stdout or '').strip())}")
        return completed

    def _project_remote(self, project: str, checkout: Path) -> RemoteExecution:
        try:
            return project_remote_execution(
                checkout, instance_dir=getattr(self.catalog, "instance_dir", None)
            )
        except CredentialError as exc:
            raise ProjectGitAccessError(project, exc.code, str(exc)) from None

    def _managed_remote_git(
        self,
        project: str,
        execution: RemoteExecution,
        checkout: Path,
        args: list[str],
        label: str,
        *,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            completed = execution.run_project(checkout, args, label=label, extra_env=extra_env)
        except CredentialError as exc:
            if exc.code in PROJECT_ACCESS_REFUSALS:
                raise ProjectGitAccessError(project, exc.code, str(exc)) from None
            raise HostError(f"{label} failed: {exc}") from None
        if completed.returncode != 0 and project_access_failure_code(
            completed.stderr or completed.stdout or ""
        ):
            raise ProjectGitAccessError(
                project,
                "credential-rejected",
                f"{label}: managed GitHub credential was refused by the remote: authentication failed",
            )
        return completed

    def _run_shell(self, command: str, cwd: Path, label: str) -> None:
        self._run(["bash", "-lc", command], label, cwd=cwd)

    def _run(
        self, args: list[str], label: str, *, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        try:
            completed = _run_bounded(args, cwd)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HostError(f"{label} failed: {exc}") from None
        if completed.returncode != 0:
            text = (completed.stderr or completed.stdout or "").strip()
            raise HostError(f"{label} failed: {_tail(text)}")
        return completed

    def run_capture(
        self,
        args: list[str],
        label: str,
        *,
        cwd: Path | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Like _run but returns the CompletedProcess regardless of exit status (the gate reads a
        non-zero code as a red verdict, not a host failure). Still raises HostError when the process
        can't run at all. `extra_env` is added to this process's environment for the child."""
        try:
            return _run_bounded(args, cwd, extra_env)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HostError(f"{label} failed: {exc}") from None


HOST_COMMAND_TIMEOUT_SECONDS = 900


def _run_bounded(
    args: list[str], cwd: Path | None, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run one host child to completion, and on a timeout take its descendants down with it.

    On timeout terminate the process group, not just its leader.
    """
    env = {**os.environ, **extra_env} if extra_env else None
    return _proc.run_isolated(args, cwd=cwd, env=env, timeout=HOST_COMMAND_TIMEOUT_SECONDS)


def _continuation_note(generation: int = 0, decision: str = "") -> str:
    """The discriminating tail of the continuation pointer: what a pointer cannot delegate."""
    note = f"Generation {generation}: use its report command, not an earlier turn's."
    if decision.strip():
        note += " Its observer decision outranks the findings below it."
    return note


def _takes_handoffs(backend: Any) -> bool:
    """Whether a head runtime can carry a production handoff: it reads one's state off the head."""
    return callable(getattr(backend, "handoff_floor", None)) and callable(getattr(backend, "handoff_started", None))


def _review_launch_handoff(intent: Any) -> PromptHandoff | None:
    """The production handoff of a reviewer launch prompt, or None outside a production pass."""
    if active_budget() is None:
        return None
    opened = intent.get("at") if isinstance(intent, dict) else None
    began_at = float(opened) if isinstance(opened, (int, float)) and opened > 0 else time.time()
    return PromptHandoff(floor=0, began_at=began_at)


def _report_nudge_prompt(generation: int, reference: str) -> str:
    """What a worker that stopped working without reporting is told, once per round.

    It names actions only: a reply typed into the pane reaches nobody, so the nudge must not invite one.
    """
    card = f" of {reference}" if reference else ""
    return (
        f"Dispatcher watchdog: this head is idle at its prompt and round {generation}{card} has no "
        "report. Do not reply to this message: text typed here is read by nobody and moves nothing. "
        "Take one action now. Work done: run the done report command in TASK.md at the workspace "
        "root. Cannot proceed: run the matching blocked report command in TASK.md. Otherwise: "
        "resume the work itself and run the done report command when it is finished. Each command's "
        f"--request-id and body file end in {generation}. Commits, pushes, a green test run or an "
        "earlier round's report command do not count: the card moves only when this round's report "
        "command runs."
    )


def _watchdog_kind(role: str) -> str:
    """`_launch`'s `role` ("worker"/"reviewer") to the `kind` the wait watchdog and
    `command_terminal_status` key their pid-heartbeat file on ("worker"/"review")."""
    return card_kind(role)
