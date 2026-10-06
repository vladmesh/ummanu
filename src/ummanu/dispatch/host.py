"""Host I/O and catalog boundary for the production dispatcher."""

from __future__ import annotations

from ummanu.dispatch.cleanup import CleanupJournal, serialized

import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from ummanu import _proc
from ummanu._fsutil import write_text_atomic
from ummanu.board.completion_evidence import (
    RESEARCH_REPORT_DIR,
    RESEARCH_REPORT_FILE,
    has_candidate,
    no_candidate_report_contract,
    research_report_path,
)
from ummanu.board.protocol_artifacts import (
    ArtifactOwnershipViolation,
    ProtocolArtifact,
    validate_rework_prerequisites,
)
from ummanu.codex_provider_events import (
    CodexProviderEventIngress,
)
from ummanu.config import validate_instance
from ummanu.dispatch import production_checkout
from ummanu.dispatch.e2e import parse_e2e
from ummanu.dispatch.gate import (
    GateResult,
)
from ummanu.dispatch.gate import (
    gate_check as _gate_check,
)
from ummanu.dispatch.gate import (
    rerun_failed_ci as _rerun_failed_ci,
)
from ummanu.dispatch.gate import (
    validation_ci as _validation_ci,
)
from ummanu.dispatch.gate_receipt import (
    accepted_receipt as _accepted_gate_receipt,
)
from ummanu.dispatch.gate_receipt import (
    is_exact_sha as _is_exact_sha,
)
from ummanu.dispatch.gate_receipt import (
    render_receipt,
)
from ummanu.dispatch.git_workspace import WORKSPACES_DIR as GIT_WORKSPACES_DIR
from ummanu.dispatch.git_workspace import GitWorkspaceManager, orca_workspaces_root
from ummanu.dispatch.git_workspace import _resolved as _resolved_path
from ummanu.dispatch.head_vitality_episode import (
    VitalityVerdict as VitalityVerdict,
)
from ummanu.dispatch.heartbeat import heartbeat_identity, sprint_task
from ummanu.dispatch.helpers import (
    _decision_record_line,
    _last_gate_red_body,
    _legacy_worker_branch,
    _protocol_prerequisites_record_line,
    _round_record_line,
    _tail,
    scrub_host_output,
)
from ummanu.dispatch.helpers import (
    safe_one_line as _safe_one_line,
)
from ummanu.dispatch.launch import (
    CAUSE_BASE_BRANCH_CONTRACT,
    CAUSE_WORKSPACE_CONTRACT,
    REVIEW_ROLE,
    WORKER_ROLE,
)
from ummanu.dispatch.launch import (
    infrastructure_action as _infrastructure_action,
)
from ummanu.dispatch.launcher import (
    HeadLaunchError,
)
from ummanu.dispatch.launcher import (
    claude_launch_model as _claude_launch_model,
)
from ummanu.dispatch.launcher import (
    ensure_claude_workspace_ready as _ensure_claude_workspace_ready,
)
from ummanu.dispatch.launcher import (
    ensure_codex_workspace_trusted as _ensure_codex_workspace_trusted,
)
from ummanu.dispatch.launcher import (
    role_launch_env as _role_launch_env,
)
from ummanu.dispatch.observer import (
    OBSERVER_PROMPT_FILE,
    OBSERVER_ROLE,
    ObserverLaunchAborted,
)
from ummanu.dispatch.observer import (
    observer_launch_prompt as _observer_launch_prompt,
)
from ummanu.dispatch.observer import (
    observer_pid_file as _observer_pid_file,
)
from ummanu.dispatch.observer import (
    render_observer_wake_context as _render_observer_wake_context,
)
from ummanu.dispatch.post_merge import pr_merge_commit
from ummanu.dispatch.production_checkout import ProductionActivationRefused
from ummanu.dispatch.provider_failure import (
    provider_failure_for_persisted_run as _provider_failure_for_persisted_run,
)
from ummanu.dispatch.review import (
    command_terminal_status as _command_terminal_status,
)
from ummanu.dispatch.runtime_provenance import ProductionRuntime, RuntimeProvenance
from ummanu.dispatch.state import (
    REVIEW_REJECTION_REASON,
    DispatcherRecord,
    GatePrAuthorship,
    GatePublishedRef,
)
from ummanu.dispatch.state import (
    attempt_request_id as _attempt_request_id,
)
from ummanu.dispatch.state import (
    request_token as _request_token,
)
from ummanu.dispatch.tui import (
    DELIVERY_ACCEPTED,
    READINESS_BUSY,
    READINESS_READY,
    TuiDeliveryError,
)
from ummanu.dispatch.tui import (
    bind_claude_provider_progress_source as _bind_claude_provider_progress_source,
)
from ummanu.dispatch.tui import (
    delivery_readiness_state as _delivery_readiness_state,
)
from ummanu.dispatch.tui import (
    prepare_claude_provider_progress_source as _prepare_claude_provider_progress_source,
)
from ummanu.dispatch.tui import (
    provider_progress_for_run as _provider_progress_for_run,
)
from ummanu.dispatch.tui import (
    provider_turn_started as _provider_turn_started,
)
from ummanu.dispatch.types import (
    STOPPED_BY_DISPATCHER,
    STOPPED_BY_OPERATOR,  # noqa: F401  # Public compatibility re-export.
    STOPPED_BY_RECONCILIATION,  # noqa: F401  # Public compatibility re-export.
    STOPPED_BY_REVIEW_FREEZE,
    DispatcherError,
    HeadLaunchAborted,
    HostError,
    LegacyDispatcherRecord,
    MergeLanding,
    ProjectGitAccessError,
    ReviewLaunch,
    review_pane_label,
)
from ummanu.dispatch.watchdog import (
    HeadRunIdentityMismatch as _HeadRunIdentityMismatch,
)
from ummanu.dispatch.watchdog import (
    bind_head_heartbeat as _bind_head_heartbeat,
)
from ummanu.dispatch.watchdog import (
    clear_head_heartbeat as _clear_head_heartbeat,
)
from ummanu.dispatch.watchdog import (
    guard_head_run_identity as _guard_head_run_identity,
)
from ummanu.dispatch.watchdog import (
    head_process_status as _head_process_status,
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
    pid_file_path as _pid_file_path,
)
from ummanu.dispatch.worker_comments import (
    WORKER_COMMENT_ROLES,
    WorkerComment,
    select_worker_comments,
    worker_comments_note,
    worker_comments_record_line,
    worker_comments_section,
)
from ummanu.head_registry import HeadRegistryConfigError, installed_heads
from ummanu.infra import git_worktree
from ummanu.infra.github_credential import (
    PROJECT_ACCESS_REFUSALS,
    CredentialError,
    ProjectGitAccess,
    RemoteExecution,
    project_access_failure_code,
    project_remote_execution,
)
from ummanu.memory import access as memory_access
from ummanu.observer_root import OBSERVER_REPO_NAME, observer_root_repo
from ummanu.projects.availability import ProjectAvailability
from ummanu.projects.contract import (
    UNDECIDABLE_RELATIVE_INTERPRETER,
    ContractVerdict,
)
from ummanu.projects.contract import decide as _decide_broad_check_contract
from ummanu.projects.integration_base import (
    IntegrationBaseError,
    resolve_integration_base,
    seed_ref_refusal,
)
from ummanu.projects.integration_base import (
    is_exact_sha as _is_exact_ref_sha,
)
from ummanu.routing_journal import (
    HEAD_FROM_CARD,
    HEAD_FROM_FALLBACK,
    HEAD_FROM_RECORD,
    HEAD_FROM_ROLE_DEFAULT,
    MODEL_UNKNOWN,
    HeadRun,
    head_run_from_profile,
)
from ummanu.runtime import head as head_ops
from ummanu.runtime.codex_preflight import (
    CodexFanoutPolicyError,
    preflight_codex_launch,
)
from ummanu.runtime.head import (
    CODEX_TUI_MODE,
    HeadCommand,
    HeadCommandError,
    HeadSpec,
    HeadSpecError,
)
from ummanu.runtime.head import (
    OBSERVE_PANE_DISCONNECTED as _OBSERVE_PANE_DISCONNECTED,
)
from ummanu.runtime.head import (
    OBSERVE_READINESS_UNKNOWN as _OBSERVE_READINESS_UNKNOWN,
)
from ummanu.runtime.head import (
    PYTHON_SAFE_PATH_FLAG as _PYTHON_SAFE_PATH_FLAG,
)
from ummanu.runtime.head import (
    render_head_command as _render_head_command,
)
from ummanu.runtime.head import (
    with_pid_heartbeat as _with_pid_heartbeat,
)
from ummanu.runtime.head.children import read_head_children
from ummanu.runtime.head_runtime_backends import (
    LegacyHeadRecordError,
    UnknownHeadRuntimeError,
    build_head_runtime,
    head_runtime_name,
    is_legacy_record,
)
from ummanu.runtime.head_runtimes import LOCAL_PTY_RUNTIME
from ummanu.runtime.heads import (
    HeadRegistryError,
)
from ummanu.runtime.heads import (
    required_role_default as _required_role_default,
)
from ummanu.runtime.heads import (
    resolve_head_id as _resolve_head_id,
)
from ummanu.runtime.launch_prefix import pythonpath_prefix
from ummanu.runtime.local_pty_head import head_run_turn_reading
from ummanu.runtime.paths import configured_product_root
from ummanu.runtime.prompt_document import (
    PromptDocumentError,
)
from ummanu.runtime.prompt_document import (
    nudge_for as _nudge_for,
)
from ummanu.runtime.prompt_document import (
    write_prompt_document as _write_prompt_document,
)
from ummanu.runtime.role_env import (
    BOARD_ACTOR_ENV,
    WORKSPACE_ENV_DIR,
    WORKSPACE_EXCLUDES,
    WORKSPACE_NAMESPACE,
    WORKSPACE_PYCACHE_PTH,
    workspace_pycache_pth,
)
from ummanu.tasks import (
    durability_dirt,
    specification_revision,
)

_PYTHONPATH_PREFIX = pythonpath_prefix()

OBSERVER_WORKSPACE_DIR = OBSERVER_REPO_NAME
OBSERVER_REPO_BRANCH = "observers"

# How long a confirmed stop waits for a head to leave after each signal, and how often it looks: a
# stop is never called unconfirmed over a process that is in the middle of leaving.
HEAD_STOP_GRACE_SECONDS = 5.0
HEAD_STOP_POLL_SECONDS = 0.1

DESTRUCTIVE_VERDICTS = frozenset(
    {
        VitalityVerdict.CONFIRMED_STALL,
        VitalityVerdict.DEAD,
    }
)

# Preflight identity excludes the mutable baseline and pane-derived binding facts.
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

    A card blocked by a bring-up now names the outcome's class in the action it wrote, so the
    requeue that brings it back to Ready has to recognise both forms as its own block: recognising
    only the historical one would leave a re-run looking like a fresh claim over a live attempt.
    """
    seen: dict[str, None] = {}
    for action in actions:
        seen.setdefault(action, None)
        seen.setdefault(_infrastructure_action(action), None)
    return tuple(seen)


def _same_repo(first: Path, second: Path) -> bool:
    try:
        return first.expanduser().resolve() == second.expanduser().resolve()
    except OSError:
        return first.expanduser().absolute() == second.expanduser().absolute()


def live_root_project_refusal(catalog: Any, project: str) -> str:
    """Why `project` may not run a card because its repository is the live root, or "" when it may.

    The live root is configuration the exporter cuts, not a code project: no card branch lands in
    it (docs/OPERATIONS.md, "Changing installation config"). A project the catalog cannot look up
    is not answered here; the preflights that need the binding fail on it in their own words.
    """
    instance_dir = getattr(catalog, "instance_dir", None)
    if not project or instance_dir is None:
        return ""
    try:
        repo = catalog.binding(project).get("repo")
    except HostError:
        return ""
    if not isinstance(repo, str) or not repo or not _same_repo(Path(repo), Path(instance_dir)):
        return ""
    return (
        f"project {project!r} names the live root {Path(instance_dir).expanduser()} as its repository; "
        "the live root is configuration, not a code project, and no card lands in it. Change it "
        "through an operation card and `ummanu config check`"
    )


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


class InstanceCatalog:
    instance_dir: Path | None = None

    def __init__(self, instance_path: Path) -> None:
        report = validate_instance(instance_path)
        if not report.ok:
            raise DispatcherError("invalid_instance", "instance config is invalid", 2)
        self.instance_path = report.instance_path
        self.instance_dir = report.instance_path.parent
        self.instance = report.instance
        self.registered_bindings = {
            str(binding.get("id")): binding
            for binding in report.bindings
            if isinstance(binding, dict) and isinstance(binding.get("id"), str) and binding.get("id")
        }
        self.bindings = {
            project: binding
            for project, binding in self.registered_bindings.items()
            if binding.get("enabled") is True
        }
        try:
            # The installation's own snapshot, not the checkout this module was imported from:
            # judging the live registry by that tree makes an unmerged commit stop production ticks.
            self._heads = installed_heads(self.instance_path)
        except HeadRegistryConfigError as exc:
            raise DispatcherError("invalid_heads", str(exc), 2) from None

    def binding(self, project: str) -> dict[str, Any]:
        candidates = (project, project.replace("_", "-"))
        binding = next((self.bindings[name] for name in candidates if name in self.bindings), None)
        if not binding:
            registered = getattr(self, "registered_bindings", self.bindings)
            if any(name in registered for name in candidates):
                raise HostError(f"project {project!r} is registered but not enabled for workloads")
            raise HostError(
                f"project {project!r} is not registered in the instance and is not enabled for workloads"
            )
        repo = binding.get("repo")
        if not isinstance(repo, str) or not repo:
            raise HostError(f"project {project!r} has no repo path")
        return binding

    def project_availability(self, project: str) -> ProjectAvailability:
        """Inspect one enabled binding using the recovery checkout rule."""
        binding = dict(self.binding(project))
        binding["id"] = project
        return ProjectAvailability.inspect([binding])

    def adapter(self, project: str) -> dict[str, Any]:
        binding = self.binding(project)
        adapter = binding.get("adapter")
        if not isinstance(adapter, str) or not adapter:
            raise HostError(f"project {project!r} has no adapter")
        path = self.instance_dir / "adapters" / f"{adapter}.yaml"
        loaded = self._load_optional_yaml(path)
        if not loaded:
            raise HostError(f"adapter {adapter!r} is unavailable")
        # A malformed e2e declaration fails the read, typed, rather than reading as no e2e at all.
        parse_e2e(loaded.get("validation"), adapter=adapter)
        return loaded

    def broad_check_verdict(self, project: str) -> ContractVerdict:
        """This project's broad-check contract as one of the three named states (secretary-1458).

        The rules are `projects.contract`'s, the same implementation the worker's own
        `ummanu check broad --module` resolves through, so a card is never handed out on a
        contract the worker would then refuse. Reading the binding and the adapter beside it is
        all this costs: no workspace, no head, no process. There is no candidate workspace at this
        point, so `workspace=None`: a question that needs one comes back as `undecidable` with its
        name on it rather than as an approval this side is not entitled to give.
        """
        return _decide_broad_check_contract(
            self.binding(project), instance=self.instance_dir or Path("."), workspace=None
        )

    def project_default_branch(self, project: str) -> str:
        """The branch this project's binding declares as its own default. No card is consulted."""
        branch = self.binding(project).get("default_branch")
        return str(branch or "main")

    def integration_base(self, project: str, override: str | None) -> str:
        """Where this card's increment lands: its PR base, history range, receipt base and merge.

        A card may override it only with a branch the project declares it integrates into — its
        default branch, or one of the binding's `integration_bases`. Anything else, a predecessor's
        `pipeline/*` card branch above all, is refused here with the reason on it (secretary-1541):
        opening a release pull request into a branch the project's workflows do not trigger for
        produces zero check-runs, which is indistinguishable from checks that have not appeared yet.
        """
        binding = self.binding(project)
        declared = binding.get("integration_bases")
        try:
            return resolve_integration_base(
                default_branch=str(binding.get("default_branch") or "main"),
                declared=list(declared) if isinstance(declared, list) else None,
                override=override,
            )
        except IntegrationBaseError as exc:
            raise HostError(
                f"project {project!r}: {exc}", bring_up_cause=CAUSE_BASE_BRANCH_CONTRACT
            ) from None

    def workspace_seed(self, project: str, task: dict[str, Any]) -> str:
        """The git ref this card's checkout is cut from.

        A reslice successor inherits its predecessor's unreleased content, so it starts from that
        candidate — `workspace.seed_ref`, a card branch or the exact object id that was assessed.
        A card with no seed starts from its integration base, which is what every card did before
        this field existed and what every ordinary card still does.
        """
        workspace = task.get("workspace") if isinstance(task.get("workspace"), dict) else {}
        seed = str((workspace or {}).get("seed_ref") or "").strip()
        if not seed:
            return self.integration_base(project, (workspace or {}).get("base_branch"))
        refusal = seed_ref_refusal(seed)
        if refusal:
            raise HostError(
                f"project {project!r}: {refusal}", bring_up_cause=CAUSE_BASE_BRANCH_CONTRACT
            ) from None
        return seed

    def worker_head(self, task: dict[str, Any]) -> str:
        requested = task.get("routing", {}).get("head_override")
        head = self._resolved_head(
            str(requested)
            if requested
            else self._role_default("new_card")
        )
        self._head_profile(head)
        return head

    def review_head(self, task: dict[str, Any]) -> str:
        requested = task.get("routing", {}).get("review_head_override")
        head = self._resolved_head(
            str(requested)
            if requested
            else self._role_default("reviewer")
        )
        self._head_profile(head)
        return head

    def _role_default(self, role: str) -> str:
        """The head this snapshot's `[role_defaults]` routes `role` to, refused by the missing key."""
        try:
            return _required_role_default(self._heads.get("role_defaults"), role)
        except HeadRegistryError as exc:
            raise HostError(str(exc)) from None

    def _resolved_head(self, head: str) -> str:
        """The profile in this snapshot that serves a head id somebody else wrote down: that id.

        An id this snapshot does not define — one the installation has since retired — is refused
        by name rather than routed to another profile.
        """
        profiles = self._heads.get("profiles")
        try:
            return _resolve_head_id(head, profiles if isinstance(profiles, dict) else {})
        except HeadRegistryError as exc:
            raise HostError(f"head {head!r} is unavailable: {exc}") from None

    def head_fallback(self, head: str) -> list[str]:
        """The ordered fallback chain `head` names in the registry, empty when it names none."""
        profile = self._head_profile(head)
        chain = profile.get("fallback")
        if not isinstance(chain, list):
            return []
        return [str(entry) for entry in chain if isinstance(entry, str) and entry]

    def claimed_worker_head(self, task: dict[str, Any]) -> str:
        return self._claimed_head(task, "resolved_worker_head", self.worker_head)

    def claimed_review_head(self, task: dict[str, Any]) -> str:
        return self._claimed_head(task, "resolved_review_head", self.review_head)

    def _claimed_head(
        self,
        task: dict[str, Any],
        key: str,
        current: Callable[[dict[str, Any]], str],
    ) -> str:
        """The head the card was claimed with, for a card the dispatcher is picking back up.

        The head is decided once, at claim. Re-reading the override or the role default here would
        hand the rest of a running attempt to whatever the board says now, so a head that has since
        left `heads.yaml` stops the attempt instead of substituting today's default.
        """
        claimed = (task.get("routing") or {}).get(key)
        if not claimed:
            return current(task)
        head = self._resolved_head(str(claimed))
        try:
            self._head_profile(head)
        except HostError as exc:
            raise HostError(f"head {head!r} recorded at claim is unavailable: {exc}") from None
        return head

    def head_run(
        self,
        task: dict[str, Any],
        *,
        role: str,
        head: str = "",
        workspace: str = "",
        failover: bool = False,
    ) -> HeadRun:
        """The launch record for one head of `role`: the profile id plus the configuration it is
        launched with, read from the same snapshot the launcher renders its command from.
        """
        routing = task.get("routing") or {}
        if role == "worker":
            override = routing.get("head_override")
            asked = self._resolved_head(
                str(override)
                if override
                else self._role_default("new_card")
            )
        else:
            override = routing.get("review_head_override")
            asked = self._resolved_head(
                str(override)
                if override
                else self._role_default("reviewer")
            )
        launched = str(head) if head else asked
        if launched != asked:
            head_source = HEAD_FROM_FALLBACK if failover else HEAD_FROM_RECORD
        else:
            head_source = HEAD_FROM_CARD if override else HEAD_FROM_ROLE_DEFAULT
        profile = self._head_profile(launched)
        resources = self._heads.get("resources")
        model: str | None = None
        model_source = ""
        if str(profile.get("adapter") or "") == "claude":
            model, model_source = _claude_launch_model(
                profile, workspace=workspace, env=_role_launch_env(role)
            )
        return head_run_from_profile(
            role=role,
            head=launched,
            head_source=head_source,
            profile=profile,
            resources=resources if isinstance(resources, dict) else {},
            model=model,
            model_source=model_source,
        )

    def observer_head(self) -> str:
        """The head profile a sprint observer is launched with."""
        head = self._role_default("observer")
        self._head_profile(head)
        return head

    def observer_profile(self, head: str) -> dict[str, Any]:
        """The registry entry for a head a sprint declares, or `HostError`."""
        return self._head_profile(head)

    def observer_run(self, head: str, *, workspace: str = "") -> HeadRun:
        """The launch record for an observer head, read from the same snapshot as its command."""
        profile = self._head_profile(head)
        resources = self._heads.get("resources")
        model: str | None = None
        model_source = ""
        if str(profile.get("adapter") or "") == "claude":
            model, model_source = _claude_launch_model(
                profile, workspace=workspace, env=_role_launch_env(OBSERVER_ROLE)
            )
        return head_run_from_profile(
            role=OBSERVER_ROLE,
            head=head,
            head_source=HEAD_FROM_ROLE_DEFAULT,
            profile=profile,
            resources=resources if isinstance(resources, dict) else {},
            model=model,
            model_source=model_source,
        )

    def head_launch(
        self,
        head: str,
        prompt_file: str,
        *,
        workspace: str,
        role: str,
        launch_prompt: str | None = None,
        identity: dict[str, str] | None = None,
        local_run_policy: str | None = None,
    ) -> HeadCommand:
        """The command this workspace's pane will run, with its workspace made fit to run it in."""
        profile = self._head_profile(head)
        try:
            self.prepare_head_workspace(head, workspace, role=role)
            return _render_head_command(
                profile if isinstance(profile, dict) else {},
                prompt=None,
                workspace=workspace,
                role=role,
                identity=identity,
                local_run_policy=local_run_policy,
            )
        except (HeadLaunchError, HeadCommandError) as exc:
            raise HostError(str(exc)) from None

    def prepare_head_workspace(self, head: str, workspace: str, *, role: str = "") -> None:
        """Pre-answer the first-run questions a head's CLI would otherwise put to an operator.

        Every codex head runs as a TUI and is asked about directory trust before it will take a prompt,
        whatever its role; a head launched into an untrusted root sits on the dialog, never answers
        the readiness probe and never receives its prompt. This runs before the head is started:
        trust, then head, then readiness, then delivery.
        """
        profile = self._head_profile(head)
        adapter = profile.get("adapter") if isinstance(profile, dict) else ""
        try:
            if adapter == "claude":
                _ensure_claude_workspace_ready(workspace)
            elif adapter == "codex":
                _ensure_codex_workspace_trusted(profile, workspace)
        except HeadLaunchError as exc:
            raise HostError(str(exc)) from None

    def _head_profile(self, head: str) -> dict[str, Any]:
        profiles = self._heads.get("profiles", {})
        profile = profiles.get(head) if isinstance(profiles, dict) else None
        if not isinstance(profile, dict):
            known = ", ".join(sorted(profiles)) if isinstance(profiles, dict) else ""
            raise HostError(f"unknown head {head!r} (known: {known or '(none)'})")
        return profile

    def head_profile(self, head: str) -> dict[str, Any]:
        return self._head_profile(head)

    def resource(self, resource: str) -> dict[str, Any]:
        resources = self._heads.get("resources", {})
        value = resources.get(resource) if isinstance(resources, dict) else None
        if not isinstance(value, dict):
            raise HostError(f"unknown head resource {resource!r}")
        return value

    @staticmethod
    def _load_optional_yaml(path: Path) -> dict[str, Any]:
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError):
            return {}
        return loaded if isinstance(loaded, dict) else {}


@dataclass(frozen=True)
class DispatcherHeadTransport:
    """What this dispatcher hands a head's backend beside a prompt: who the head is and its hook.

    The backend owns delivery itself (`local-pty` types into the pty it supervises); what it reads
    here is `before_send`, the one step it performs on the dispatcher's behalf after admission and
    before the prompt is typed. There is no pane for this dispatcher to deliver into or close.
    """

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


#: Which backend a run, a spec or a name is held by. Shared with the mechanical-role driver rather
#: than kept here: one reader of the key, as there is one mapping from its value to a backend.
_head_runtime_name = head_runtime_name


def _runtime_writes_launch_identity(runtime: Any) -> bool:
    """Whether this backend writes a head's launch-identity heartbeat itself.

    Exactly one layer owns that writer for a head. `local-pty`'s supervisor wraps the command it is
    handed, so a command this dispatcher had wrapped as well `exec`s the inner writer, which exits,
    and the head never runs (secretary-1698). Only a backend that says it does not write it has this
    dispatcher wrap the command instead.
    """
    return bool(getattr(runtime, "writes_launch_identity", False))


def _launch_command(runtime: Any, command: str, pid_file: str, identity: dict[str, str]) -> str:
    """The command a head's backend is handed: wrapped by this dispatcher only when it owns the writer.

    Either way the record lands at `pid_file` with `identity`: `start` is handed that `pid_file`,
    and a backend that writes the identity itself spells it from the same run, role and task.
    """
    if _runtime_writes_launch_identity(runtime):
        return command
    return _with_pid_heartbeat(command, pid_file, identity=identity)


def _durable_head_run(subject: Any) -> head_ops.HeadRun | None:
    """The run a record names at a workspace-scoped stop, or `None` when it names none.

    Workspace cleanup reads the run itself rather than a lifecycle run rebuilt around the record:
    the per-role builders invent a fresh identity for a field that holds nothing, and a stop has to
    act on the head that was actually raised or on nothing at all. So a field that is empty,
    unparseable, or carries no run id is `None` here — that is a workspace with no head of that
    role, not a head to go looking for.
    """
    if subject is None:
        return None
    if isinstance(subject, head_ops.HeadRun):
        return subject if subject.run_id else None
    if not isinstance(subject, dict) or not subject.get("run_id"):
        return None
    try:
        run = head_ops.HeadRun.from_json(subject)
    except (head_ops.HeadRunError, head_ops.TaskRefError, TypeError, ValueError):
        return None
    return run if run.run_id else None


def _interrupted_command_section(record: DispatcherRecord | None) -> list[str]:
    """The one factual line a respawned head gets about the command its predecessor was running.

    ``respawn_interrupted_command`` is set by the wait watchdog's respawn for exactly the bring-up
    it performs (secretary-1692) and is never persisted, so no other launch renders it.
    """
    line = _safe_one_line(getattr(record, "respawn_interrupted_command", "") or "", limit=600)
    if not line:
        return []
    return ["## Interrupted command", "", line, ""]


class CommandHostRuntime:
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
        """Attest this bring-up's run, then hand it the source its launch was prepared with.

        The worker and reviewer launch path only.  The observer bring-up has its own delivery
        contour and keeps calling `preflight_codex_run` directly, so this repair cannot change what
        an observer launch hands its handoff.
        """
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

        One launch preflights twice: once to fix the intent on disk, and once here, inside the host
        call that opens the pane.  Both attestations must run — workspace trust is a hard pre-pane
        requirement and is rechecked by the second — but only the first descriptor is durable. It is
        the one the ingress binds its session against and the one a recovery reads, while the second
        enumerates the session root again and legitimately sees whatever journals other heads opened
        in between.  Handing that second baseline back as this run's source is what made a rework
        relaunch fail its own handoff with two conflicting unbound descriptors for one HeadRun.

        Retention is fenced on the exact run: the prepared descriptor is kept only when it names this
        run id, spec, workspace, role, task ref and pid file, and describes the same source identity
        and root.  Anything else stays the fresh attestation and is refused downstream as the
        identity conflict it is, and a source already bound to a session is preserved rather than
        replaced by a new unbound one.
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

    @serialized
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

    @serialized
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

    def observer_workspace(self, reference: str) -> str:
        """Where one sprint observer runs. Its own directory, never a card workspace and never the
        interactive ummanu session's checkout: the observer reads reports and slices cards, it
        owns no branch of the project it watches.

        A plain detached `git worktree` of the observer repo under `<data_dir>/workspaces/observers/`
        (secretary-1705). The launch intent and `prepare_observer` both ask this, so they name the
        same path; after launch the recorded path decides.
        """
        token = _request_token(reference)
        if self.mode == "noop":
            return str(self.data_dir / "dispatcher" / OBSERVER_WORKSPACE_DIR / token)
        return str(self._git_observer_root / token)

    @property
    def _git_observer_root(self) -> Path:
        return Path(self.data_dir) / GIT_WORKSPACES_DIR / OBSERVER_WORKSPACE_DIR

    def _is_git_observer_workspace(self, workspace: str) -> bool:
        """Whether an observer workspace is one this host made, read from its recorded path alone.

        Exactly `<data_dir>/workspaces/observers/<token>`. A recorded path anywhere else — an Orca
        worktree under `~/orca/workspaces/observers/` — is a legacy record (`_refuse_legacy_observer`).
        """
        if self.mode == "noop" or not workspace:
            return False
        path = _resolved_path(Path(workspace))
        root = _resolved_path(self._git_observer_root)
        return path.is_relative_to(root) and len(path.relative_to(root).parts) == 1

    def _observer_repo(self) -> Path:
        """The repo observer workspaces are cut from: standalone, empty, without a remote.

        Created once and shared by every sprint: the observer needs a worktree to run in, not a
        checkout of the project it watches.
        """
        repo = observer_root_repo(self.data_dir)
        if not (repo / ".git").is_dir():
            repo.mkdir(parents=True, exist_ok=True)
            self._run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "init",
                    "--quiet",
                    "--initial-branch",
                    OBSERVER_REPO_BRANCH,
                ],
                "observer repo init",
            )
            self._run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "-c",
                    "user.name=ummanu-dispatcher",
                    "-c",
                    "user.email=dispatcher@localhost",
                    "commit",
                    "--quiet",
                    "--allow-empty",
                    "-m",
                    "observer root",
                ],
                "observer repo commit",
            )
        return repo

    def _git_observer_worktree_listed(self, workspace: str) -> bool:
        """Whether git lists this path as a worktree of the observer repo, and it is on disk."""
        repo = observer_root_repo(self.data_dir)
        if not (repo / ".git").is_dir() or not Path(workspace).is_dir():
            return False
        listed = self._observer_git(["worktree", "list", "--porcelain"], repo)
        if listed.returncode != 0:
            raise HostError(f"git worktree list failed: {_tail((listed.stderr or listed.stdout or '').strip())}")
        target = _resolved_path(Path(workspace))
        return any(
            line.startswith("worktree ") and _resolved_path(Path(line[len("worktree ") :])) == target
            for line in (listed.stdout or "").splitlines()
        )

    def _create_git_observer_workspace(self, workspace: Path) -> Path:
        """The observer's workspace as a detached `git worktree` of the observer repo, at the path
        the launch intent already names. No branch is created."""
        if self._git_observer_worktree_listed(str(workspace)):
            return workspace
        repo = self._observer_repo()
        if workspace.exists() or workspace.is_symlink():
            raise HostError("observer placement is occupied without matching registration")
        result = git_worktree.add(self._observer_git, repo, workspace, OBSERVER_REPO_BRANCH)
        if result.returncode != 0:
            detail = _tail((result.stderr or result.stdout or "").strip())
            raise HostError(f"git worktree add failed for the observer workspace: {detail}; residue preserved")
        return workspace

    def _observer_git(self, argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        return self.run_capture(["git", "-C", str(cwd), *argv], "observer git workspace")

    def observer_pid_file(self, reference: str) -> str:
        """Where this sprint's observer heartbeat writes its pid."""
        return _observer_pid_file(reference)

    @serialized
    def prepare_observer(
        self,
        sprint: dict[str, Any],
        head: str,
        *,
        prompt: str,
        identity: dict[str, str] | None = None,
        heartbeat_run_id: str = "",
        recorded_workspace: str = "",
    ) -> dict[str, Any]:
        """Bring one observer up in the dedicated observer repository.

        Sprint repositories are canonical source roots and reservations are project ids. Neither
        is the repository authority for this process: the observer worktree is cut from
        ``observer_root_repo``. `recorded_workspace` is the path the launch intent recorded; without one it
        is `observer_workspace(reference)`, which is what the intent computes, and it is cut with
        `git worktree`. A recorded path this host did not make is a legacy record and is refused.
        """
        reference = str(sprint.get("ref") or "")
        self._require_cleanup_admission(reference)
        placed = recorded_workspace or self.observer_workspace(reference)
        if self.mode == "noop":
            workspace = Path(self.observer_workspace(reference))
            workspace.mkdir(parents=True, exist_ok=True)
        else:
            if not self._is_git_observer_workspace(placed):
                raise LegacyDispatcherRecord(
                    f"the observer of {reference or 'an unnamed sprint'}",
                    f"its recorded workspace {placed} is not a git worktree this host owns",
                    verb="launch",
                )
            workspace = self._create_git_observer_workspace(Path(placed))
        self._write_prompt(workspace / OBSERVER_PROMPT_FILE, prompt)
        pid_file = _observer_pid_file(reference)
        run = self._observer_run(head, str(workspace))
        lifecycle_run = head_ops.HeadRun(
            run_id=heartbeat_run_id or head_ops.new_run_id(),
            spec=self._head_spec(head, str(run.get("adapter") or "unknown")),
            workspace=str(workspace),
            task_ref=head_ops.TaskRef.sprint(reference),
            role=OBSERVER_ROLE,
            pid_file=pid_file,
        )
        if lifecycle_run.spec.memory_limit_mib is not None:
            lifecycle_run = replace(lifecycle_run, scope_generation=lifecycle_run.run_id)
        if lifecycle_run.spec.adapter in {"codex", "claude"}:
            try:
                attested = self.preflight_codex_run(
                    head,
                    role=OBSERVER_ROLE,
                    workspace=str(workspace),
                    task_ref=lifecycle_run.task_ref,
                    pid_file=pid_file,
                    run_id=lifecycle_run.run_id,
                )
                # Codex keeps its existing fan-out ingress handoff. Claude has no ingress: its
                # pre-pane descriptor is retained here, then bound by the exact-run cursor probe.
                lifecycle_run = (
                    self._retain_prepared_provider_source(attested)
                    if lifecycle_run.spec.adapter == "claude"
                    else attested
                )
            except CodexFanoutPolicyError as exc:
                raise HostError(str(exc)) from None
        heartbeat = self._run_heartbeat_identity(lifecycle_run, OBSERVER_ROLE)
        if self.mode == "noop":
            return {
                "workspace": str(workspace),
                "handle": f"noop:{head}:{workspace.name}:{OBSERVER_PROMPT_FILE}",
                "leaf": "",
                "prompt_delivered": False,
                "delivery_evidence": {},
                "pid_file": pid_file,
                "run": run,
                "head_run": lifecycle_run.to_json(),
            }
        heartbeat_owner = self.head_runtime_for(lifecycle_run)
        if not _runtime_writes_launch_identity(heartbeat_owner):
            # Drop a predecessor's pid before the new head can be read as this launch's liveness. A
            # runtime that writes the identity itself reads this file first, to refuse a bring-up
            # over a live head of the same run, and the head it raises then replaces it.
            _clear_head_heartbeat(pid_file)
        try:
            grant = memory_access.issue_grant(
                lifecycle_run,
                memory_access.sprint_subject(reference, list(sprint.get("reservations") or [])),
                data_dir=self.data_dir,
            )
        except memory_access.MemoryAccessError as exc:
            raise HostError(f"memory access binding could not be issued: {exc}") from None
        launch = self.catalog.head_launch(
            head,
            OBSERVER_PROMPT_FILE,
            workspace=str(workspace),
            role=OBSERVER_ROLE,
            launch_prompt=_observer_launch_prompt(),
            identity=dict(identity or {}),
        )
        # The bearer goes in the head's environment, not its command: the command is logged by sudo.
        lifecycle_run = self._open_head_pane(
            lifecycle_run,
            f"{reference} observer",
            _launch_command(heartbeat_owner, launch.command, pid_file, heartbeat),
            env=dict(grant.launch_identity),
        )
        ingress = self._codex_provider_ingress(lifecycle_run)
        if ingress is not None:
            # The returned pane leaf is part of the run identity the event source is bound to:
            # persist it before the first provider prompt, not in the post-launch save below.
            ingress.commit_run(lifecycle_run)
        _bind_head_heartbeat(pid_file, expected=heartbeat, leaf=lifecycle_run.leaf)
        delivered = False
        delivery_evidence: dict[str, Any] = {}
        if launch.prompt_after_start:
            # What this bring-up persists is the run its ingress bound, not the lifecycle the
            # delivery moved: the observer's watchdog adopts exactly the record the source binding
            # was made durable under, and a launcher returning a later value would hand adoption a
            # run the crash-era intent never saw.
            bound_run = lifecycle_run

            def bind_before_send() -> head_ops.HeadRun:
                nonlocal bound_run
                if ingress is not None:
                    bound_run = head_ops.post_delivery_run(
                        lifecycle_run,
                        ingress.bind_before_delivery(),
                    )
                return bound_run

            failure: Exception | None = None
            try:
                receipt = self.head_runtime_for(lifecycle_run).deliver(
                    lifecycle_run,
                    head_ops.NudgePointer.at_document(str(workspace / OBSERVER_PROMPT_FILE)),
                    transport=self._head_transport(
                        str(workspace),
                        OBSERVER_PROMPT_FILE,
                        launch.adapter or "codex",
                        OBSERVER_ROLE,
                        before_send=bind_before_send if ingress is not None else None,
                    ),
                    subject="observer-launch",
                )
                if not receipt.ok:
                    # A non-ok receipt does not have to carry a `failure`: `HEAD_DRAINING` is a
                    # refusal with no operation error behind it, and reading success off
                    # `failure is None` would have counted it as a delivered launch prompt.
                    failure = receipt.failure or HostError(
                        f"the observer launch prompt was refused: {receipt.reason}"
                    )
            except (PromptDocumentError, TuiDeliveryError, HostError) as exc:
                failure = exc
            if failure is None:
                lifecycle_run = bound_run
                delivered = True
                delivery_evidence = _delivery_evidence_json(receipt.delivery, "observer-launch")
            else:
                exc = failure
                evidence = _delivery_evidence_json(exc, "observer-launch")
                try:
                    self._stop_observer_terminals(
                        str(workspace),
                        pid_file=pid_file,
                        run=lifecycle_run,
                        role=OBSERVER_ROLE,
                        task=sprint_task(reference),
                        leaf=lifecycle_run.leaf,
                    )
                except Exception as stop_exc:  # noqa: BLE001 - preserve cleanup failure evidence
                    # The pane is still up and this dict is the only pointer to it: reporting a plain
                    # bring-up failure would leave the sprint headless and open a second head beside it.
                    raise ObserverLaunchAborted(
                        f"{exc}; observer terminal stop failed: {stop_exc}",
                        handle=lifecycle_run.handle,
                        leaf=lifecycle_run.leaf,
                        workspace=str(workspace),
                        pid_file=pid_file,
                        run=run,
                        evidence=evidence,
                    ) from None
                raise ObserverLaunchAborted(str(exc), evidence=evidence) from None
        return {
            "workspace": str(workspace),
            "handle": lifecycle_run.handle,
            "leaf": lifecycle_run.leaf,
            "prompt_delivered": delivered,
            "delivery_evidence": delivery_evidence,
            "pid_file": pid_file,
            "run": run,
            # Delivery owns the source handoff; this launcher adds only the pane facts it proved, and
            # returning `lifecycle_run` keeps observer adoption on the run the ingress persisted.
            "head_run": lifecycle_run.to_json(),
        }

    @serialized
    def stop_observer(self, record: Any) -> None:
        """End one observer head and give back what its bring-up took.

        Unconditional: a freeze, a closed sprint, a policy refusal and an emergency replacement all
        mean "end this now", and none of them may be refused because the head happens to be busy.
        What it owes the head runtime afterwards is the forgetting that the `stop` verb does for
        itself — this teardown is the worktree removal, not that verb, and a runtime that lives
        as long as the production loop would otherwise keep one epoch, one output mark and one
        admission entry per head ever launched. A legacy observer record is refused, untouched.
        """
        if self.mode == "noop":
            return
        self._refuse_legacy_observer(record, "stop")
        owner = getattr(self, "cleanup_owner", None)
        if owner is None:
            raise HostError("observer stop has no durable cleanup owner")
        result = owner.cleanup_observer(record)
        # A stopped closeout head whose workspace removal waits only for card cleanup is down;
        # the journal replays the rest.
        progress = result.get("progress") or {}
        if result["status"] == "pending" and not (progress.get("heads_stopped") and progress.get("awaits_cards")):
            raise HostError("observer cleanup pending: " + result["reason"])
        observer_run = self._observer_lifecycle_run(record)
        self.head_runtime_for(observer_run).forget_head(observer_run.run_id)

    def _refuse_legacy_observer(self, record: Any, verb: str) -> None:
        """Raise `LegacyDispatcherRecord` when this observer record was written on Orca.

        Its recorded workspace is not the git worktree this host places, it names a pane handle and
        no workspace at all (a record from before launch intents named one), or its persisted run
        is a legacy record.
        """
        if self.mode == "noop":
            return
        subject = f"the observer of {getattr(record, 'sprint', '') or 'an unnamed sprint'}"
        workspace = str(getattr(record, "workspace", "") or "")
        if workspace and not self._is_git_observer_workspace(workspace):
            raise LegacyDispatcherRecord(
                subject, f"its workspace {workspace} is not a git worktree this host owns", verb=verb
            )
        if not workspace and (getattr(record, "handle", "") or getattr(record, "leaf", "")):
            raise LegacyDispatcherRecord(subject, "it names an Orca pane and no workspace", verb=verb)
        run = _durable_head_run(getattr(record, "head_run", None))
        if run is not None and is_legacy_record(run):
            raise LegacyDispatcherRecord(subject, f"its head run {run.run_id} is a legacy record", verb=verb)

    def observer_activity_epoch(self, record: Any) -> int:
        """This observer head's activity epoch, to hand back to a stop that must only run if quiet.

        Per head, never the runtime's own counter: another sprint's observer doing something must
        not make this one look busy.

        Asked of the backend rather than of its memory where the backend can answer that way. A
        runtime object lives for one tick, so `activity.epoch` alone reads an epoch of zero for
        every head this process did not start; a backend whose head has a durable witness offers
        `activity_epoch`, which recovers the real one before answering (secretary-1479). The
        legacy backend has no such witness and no such method, and there it is `activity.epoch`
        exactly as before — the fallback is the whole of what a backend without one can say.
        """
        if self.mode == "noop":
            return 0
        observer_run = self._observer_lifecycle_run(record)
        runtime = self.head_runtime_for(observer_run)
        durable = getattr(runtime, "activity_epoch", None)
        if callable(durable):
            return int(durable(observer_run))
        return runtime.activity.epoch(observer_run.run_id)

    @serialized
    def stop_observer_if_quiescent(
        self, record: Any, expected_activity_epoch: int, head_process_alive: bool
    ) -> bool:
        """End this observer only while it is still quiet. False means it was not, and nothing ran.

        The check and the teardown are one critical section inside the head runtime: this head's
        epoch still where the caller saw it, the turn settled, admission closed, and only then the
        teardown — so a delivery cannot land between deciding the head is finished and taking it
        away. The worktree teardown is `stop_observer` unchanged, run only once the runtime's stop
        succeeded: the runtime left admission closed, and this method holds the cleanup ownership
        lock across both, so nothing can reopen the head in between.

        Both facts come from the caller and neither is re-read here. `head_process_alive` is the
        pid-heartbeat answer the caller already had.
        """
        if self.mode == "noop":
            return True
        observer_run = self._observer_lifecycle_run(record)
        receipt = self.head_runtime_for(observer_run).stop_if_quiescent(
            observer_run,
            head_ops.StopInitiator(actor=STOPPED_BY_DISPATCHER),
            expected_activity_epoch=expected_activity_epoch,
            head_process_alive=head_process_alive,
        )
        if not receipt.ok:
            return False
        self.stop_observer(record)
        return True

    def observer_status(self, record: Any) -> dict[str, Any]:
        """Read the observer pane's output clock and whether it is ready for a prompt."""
        if self.mode == "noop":
            return {}
        if not record.workspace or not (record.handle or record.leaf):
            raise HostError("observer record names no terminal to read")
        observer_run = self._observer_lifecycle_run(record)
        seen = self.head_runtime_for(observer_run).observe(observer_run)
        if seen.reason == _OBSERVE_PANE_DISCONNECTED:
            raise HostError("observer terminal is not connected")
        if seen.reason == _OBSERVE_READINESS_UNKNOWN:
            # A probe that failed is not a working observer; raising puts it on the bounded failure path.
            raise HostError("observer terminal readiness could not be read")
        if not seen.ok:
            # What is left once the pane's own answers are handled is the pane not being in the
            # inventory at all. An inventory that could not be read is no longer folded in here:
            # the session manager's refusal travels out of the observation as itself, which is what
            # this path did before the boundary existed and what its callers already classify.
            raise HostError("observer terminal is not in the inventory of its workspace")
        status: dict[str, Any] = {"idle": seen.readiness == READINESS_READY}
        if seen.last_output_at:
            status["last_activity"] = seen.last_output_at
        return status

    def _observer_lifecycle_run(self, record: Any) -> head_ops.HeadRun:
        """This observer as the head runtime addresses it: its own run, at the pane it is in.

        The persisted run is preferred, because the turn leases and the activity epoch the runtime
        keeps are per head and a fresh identity every tick would keep losing them. A record written
        before that run existed still has a workspace and a pane, which is all an observation needs.
        """
        workspace = str(getattr(record, "workspace", "") or "")
        handle = str(getattr(record, "handle", "") or "")
        leaf = str(getattr(record, "leaf", "") or "")
        stored = getattr(record, "head_run", {})
        run: head_ops.HeadRun | None = None
        if isinstance(stored, dict) and stored.get("run_id"):
            try:
                run = head_ops.HeadRun.from_json(stored)
            except (head_ops.HeadRunError, head_ops.TaskRefError, TypeError, ValueError):
                run = None
        if run is None:
            # Deliberately not resolved through the catalog: an observation must not fail because
            # a profile was renamed since this observer started, and nothing an observation reads
            # depends on the spec. A delivery that does need the adapter is told it explicitly.
            run = head_ops.HeadRun(
                run_id=head_ops.new_run_id(),
                spec=HeadSpec(
                    profile_id=str(getattr(record, "head", "") or "unknown-observer"),
                    adapter="unknown",
                ),
                workspace=workspace,
                task_ref=head_ops.TaskRef.sprint(str(getattr(record, "sprint", "") or "unknown-sprint")),
                role=OBSERVER_ROLE,
                pid_file=str(getattr(record, "pid_file", "") or ""),
            )
        return replace(run, workspace=workspace or run.workspace, handle=handle, leaf=leaf)

    def observer_provider_progress(self, record: Any) -> dict[str, str]:
        """Read provider progress only from this observer's persisted HeadRun."""
        stored = getattr(record, "head_run", {})
        expected_workspace = str(getattr(record, "workspace", "") or "")
        expected_sprint = str(getattr(record, "sprint", "") or "")
        try:
            run = head_ops.HeadRun.from_json(stored)
        except (head_ops.HeadRunError, TypeError, ValueError):
            return {"state": "unavailable", "reason": "persisted observer HeadRun is unavailable"}
        if (
            run.workspace != expected_workspace
            or run.task_ref.kind != "sprint"
            or run.task_ref.ref != expected_sprint
            or (run.role and run.role != OBSERVER_ROLE)
        ):
            return {
                "state": "identity_mismatch",
                "reason": "persisted observer HeadRun binding mismatches observer record",
            }
        # Claude journals appear asynchronously after the pane starts. Bind only the one source
        # selected from this exact run's persisted pre-pane baseline, then retain that result on
        # the observer record before its wake reducer evaluates the cursor.
        if run.spec.adapter == "claude":
            updated = _bind_claude_provider_progress_source(run)
            if updated != run:
                record.head_run = updated.to_json()
                if self.commit_state is not None:
                    self.commit_state()
                run = updated
        return _provider_progress_for_run(run)

    def nudge_observer(
        self,
        record: Any,
        *,
        sprint: dict[str, Any],
        change: str = "linked-card",
        post_merge: list[dict[str, Any]] | None = None,
    ) -> str:
        """Give an idle observer one event-driven turn without replacing its head."""
        if self.mode == "noop":
            return DELIVERY_ACCEPTED
        workspace = str(getattr(record, "workspace", "") or "")
        handle = str(getattr(record, "handle", "") or "")
        leaf = str(getattr(record, "leaf", "") or "")
        if not workspace or not (handle or leaf):
            raise HostError("observer has no terminal handle for an event wake")
        self._refuse_legacy_observer(record, "wake")
        # A supervised head owns no pane: its backend addresses it by its own run
        # (issue:70562b15a7dc8764437e).
        waking = self._observer_lifecycle_run(record)
        delivery = getattr(record, "delivery", None)
        message = _render_observer_wake_context(
            sprint, change=change, delivery=delivery, post_merge=post_merge
        )
        document = Path(workspace) / OBSERVER_PROMPT_FILE
        try:
            # One boundary for every adapter: atomically replace the complete live document before
            # constructing or sending its bounded, single-line pointer. A redelivery uses the same
            # path and rewrites it only when the rendered snapshot has changed.
            self._write_prompt(document, message)
            pointer = head_ops.NudgePointer.at_document(str(document))
            adapter = self._prompt_adapter(getattr(record, "run", {}), str(getattr(record, "head", "")))
            # A wake carries both proofs a delivery can have, and either one confirms it. The head
            # is live and working, so the screen evidence this used to rely on alone is the weaker
            # of them: a working Codex can read as idle, and the screen the wake is delivered
            # into is precisely the one that is printing. The provider's own record of the turn
            # is what a launch has always been confirmed by, and it says the same thing about a
            # wake. What stays out of band is the causal acknowledgement, not the delivery: the
            # observer still quotes the delivery id in the resume it writes from this turn.
            receipt = self.head_runtime_for(waking).deliver(
                waking,
                pointer,
                transport=self._head_transport(
                    workspace,
                    OBSERVER_PROMPT_FILE,
                    adapter=adapter,
                    role=OBSERVER_ROLE,
                    ack_out_of_band=True,
                ),
                subject="observer-wake",
            )
        except (PromptDocumentError, RuntimeError, TuiDeliveryError) as exc:
            failure = HostError(f"observer wake was not delivered: {exc}")
            # The lifecycle stores this beside the sprint: the delivery boundary's own evidence
            # (terminal identity, payload size and hash, stage, fingerprints) and no prompt text.
            failure.evidence = getattr(exc, "evidence", None)
            raise failure from None
        if not receipt.ok:
            failure = HostError(f"observer wake was not delivered: {receipt.reason}")
            failure.evidence = receipt.evidence
            if (
                receipt.status == head_ops.HEAD_BUSY
                and _delivery_readiness_state(receipt.evidence) != READINESS_BUSY
            ):
                # The head is in a turn and nothing was typed: a wake to make again once it ends,
                # not a wake it refused (which is what spends the retries that replace a head).
                failure.evidence = {"readiness_state": READINESS_BUSY, "reason": receipt.reason}
            raise failure from None
        return receipt.delivery

    def _stop_observer_terminals(
        self,
        workspace: str,
        *,
        pid_file: str = "",
        run: Any = None,
        role: str = "",
        task: str = "",
        leaf: str = "",
    ) -> None:
        """End the observer this workspace holds, by its own run's stop on the backend holding it."""
        if run is not None:
            self._guard_head_run(run, role, pid_file=pid_file, task=task, leaf=leaf)
        self._stop_recorded_heads(workspace, [(run, role or OBSERVER_ROLE)])

    def _observer_run(self, head: str, workspace: str) -> dict[str, Any]:
        try:
            return self.catalog.observer_run(head, workspace=workspace).to_json()
        except (HostError, AttributeError, KeyError, TypeError):
            return HeadRun(
                role=OBSERVER_ROLE, head=head, adapter="unknown", model_source=MODEL_UNKNOWN
            ).to_json()

    def provider_progress(self, task: dict[str, Any], record: DispatcherRecord, kind: str) -> dict[str, str]:
        """Read an opaque provider cursor for this exact role's persisted HeadRun."""
        run = record.review_head_run if kind == "review" else record.worker_head_run
        expected_workspace = record.workspace
        try:
            lifecycle_run = head_ops.HeadRun.from_json(run)
        except (head_ops.HeadRunError, TypeError, ValueError):
            return {"state": "unavailable", "reason": "persisted HeadRun is unavailable"}
        expected_role = "reviewer" if kind == "review" else "worker"
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

    def observer_provider_failure(self, record: Any) -> dict[str, Any]:
        """Whether this observer's exact HeadRun ended its last turn on a provider error (ummanu-108).

        Bound like `observer_provider_progress`: a persisted run naming another workspace or
        sprint answers nothing. The reader is `provider_failure.provider_failure_for_run`.
        """
        if self.mode == "noop":
            return {"state": "unavailable", "reason": "noop host"}
        stored = getattr(record, "head_run", {})
        try:
            run = head_ops.HeadRun.from_json(stored)
        except (head_ops.HeadRunError, TypeError, ValueError):
            return {"state": "unavailable", "reason": "persisted observer HeadRun is unavailable"}
        if (
            run.workspace != str(getattr(record, "workspace", "") or "")
            or run.task_ref.kind != "sprint"
            or run.task_ref.ref != str(getattr(record, "sprint", "") or "")
            or (run.role and run.role != OBSERVER_ROLE)
        ):
            return {"state": "identity_mismatch", "reason": "persisted observer HeadRun binding mismatches"}
        return _provider_failure_for_persisted_run(stored, local_pty_root=self._local_pty_root())

    def provider_failure(self, task: dict[str, Any], record: DispatcherRecord, kind: str) -> dict[str, Any]:
        """Whether this role's exact HeadRun ended its last turn on a provider error (secretary-1799, ummanu-108).

        Read-only, and bound like `provider_progress`: a persisted run that names another workspace,
        card or role answers nothing. The reader is `provider_failure.provider_failure_for_run`.
        """
        if self.mode == "noop":
            return {"state": "unavailable", "reason": "noop host"}
        run = record.review_head_run if kind == "review" else record.worker_head_run
        try:
            lifecycle_run = head_ops.HeadRun.from_json(run)
        except (head_ops.HeadRunError, TypeError, ValueError):
            return {"state": "unavailable", "reason": "persisted HeadRun is unavailable"}
        expected_role = "reviewer" if kind == "review" else "worker"
        if (
            lifecycle_run.workspace != record.workspace
            or lifecycle_run.task_ref.kind != "card"
            or lifecycle_run.task_ref.ref != str(task.get("ref") or "")
            or (lifecycle_run.role and lifecycle_run.role != expected_role)
        ):
            return {"state": "identity_mismatch", "reason": "persisted HeadRun binding mismatches role"}
        return _provider_failure_for_persisted_run(run, local_pty_root=self._local_pty_root())

    def head_children(self, head_pid: int) -> dict[str, Any]:
        """The live descendants of a head's heartbeat-proven pid, with their movement counters.

        Read from ``/proc`` by ``runtime.head.children`` (secretary-1692); the vitality reducer
        counts their CPU/IO movement as the head's activity while it waits on its own command.
        """
        return read_head_children(head_pid)

    def supervisor_journal(
        self, _task: dict[str, Any], record: DispatcherRecord, kind: str
    ) -> dict[str, Any]:
        """This role's head's turn and progress, read from its own supervisor's journal.

        Read-only: a bounded tail of `heads/<run_id>/journal.jsonl` folded by the local-pty backend
        (secretary-1739). The reading names the run it read, and `command_terminal_status` hands it
        on only for the run the heartbeat proved.
        """
        run = record.review_head_run if kind == "review" else record.worker_head_run
        return head_run_turn_reading(self._local_pty_root(), str((run or {}).get("run_id") or ""))

    def head_loss_reason(self, run: Any) -> str | None:
        """Typed loss recorded by this run's own supervisor, with no process inference."""
        from ummanu.runtime.local_pty_head import head_run_loss_reason

        run_id = str((run or {}).get("run_id") or "") if isinstance(run, dict) else ""
        return head_run_loss_reason(self._local_pty_root(), run_id) if run_id else None

    def safe_recover_worker_continuation(
        self,
        _task: dict[str, Any],
        _record: DispatcherRecord,
        _liveness: dict[str, Any],
    ) -> dict[str, str]:
        """Advertise that this host has no safe provider recovery primitive.

        An operator's raw interrupt can terminate a Codex provider session and a generic key chord
        cannot prove which composer it affected, so the only permitted answer is this typed absence.
        The dispatcher then takes its confirmed-stop fence before replacement.
        """
        return {
            "state": "unavailable",
            "reason": "no provider/terminal-safe continuation recovery capability is available",
        }

    @serialized
    def start_review(self, task: dict[str, Any], record: DispatcherRecord) -> ReviewLaunch:
        """Bring the reviewer up as a second head inside the worker's own worktree.

        Once the reviewer is up the worker is shut down and its commit pinned, so the reviewer
        judges a checkout nothing else is still editing. What the reviewer is given is a nudge at a
        document written outside the checkout, not the review itself.
        """
        self._require_project_available(str(task.get("project") or ""))
        self._refuse_legacy_record(record, "launch the reviewer of")
        if not record.workspace:
            raise HostError("review workspace is unavailable")
        workspace = Path(record.workspace)
        if self.mode == "noop":
            workspace.mkdir(parents=True, exist_ok=True)
        elif not workspace.is_dir():
            raise HostError("review workspace is missing")
        self._prepare_workspace_environment(record.workspace, project=str(task["project"]))
        self._require_workspace_environment(record.workspace)
        self._clear_body_file("verdict", task["ref"], record.review_baseline)
        local_run_policy = self._frozen_local_run_policy(
            task, REVIEW_ROLE, record.review_baseline, record.review_local_run_snapshot
        )
        document, nudge = self._review_document(task, record, local_run_policy=local_run_policy)
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
        )
        try:
            if record.worker_continuation.retained and self.worker_retained_vanished(record):
                # The retained worker is provably gone (its pid heartbeat resolves to a dead pid),
                # so there is no second writer to freeze. The freeze path here would loop
                # `review-launch-aborted` over a head that can never confirm suspended.
                pass
            elif record.worker_continuation.retained:
                # A retained worker is already SIGSTOPed and its conversation is what a red verdict
                # continues. Confirm that suspension rather than trusting the record.
                self.confirm_worker_retained(record)
            else:
                self._freeze_worker(record)
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

    def nudge_review_delivery(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        intent: dict[str, Any],
    ) -> dict[str, Any]:
        """Retry the document nudge for the exact reviewer a busy launch intent retained."""
        self._refuse_legacy_record(record, "deliver to the reviewer of")
        if not record.workspace:
            raise HostError("review workspace is unavailable for a retained launch")
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
        document, nudge = self._review_document(task, record, local_run_policy=local_run_policy)
        try:
            receipt = self.head_runtime_for(run).deliver(
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
            )
        except LegacyDispatcherRecord:
            raise
        except (TuiDeliveryError, HostError) as exc:
            failure = HostError(f"retained reviewer document nudge was not delivered: {exc}")
            failure.evidence = _delivery_evidence_json(exc, "reviewer-launch")
            raise failure from None
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
        """End the reviewer's lifecycle alone. `stop` would take the whole worktree down with it,
        which on a red verdict means killing the checkout's terminals the worker is about to get
        back. Closing the reviewer's own split leaf removes that pane and leaves the rest alone.
        """
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

    def retained_workspace_state(self, task: dict[str, Any], record: DispatcherRecord) -> dict[str, Any]:
        """Describe the retained worker checkout of a card recovered without a live head.

        Read-only by construction: nothing here creates, re-seeds, resets or moves a branch. A
        checkout that cannot be bound to this card is reported unbound with a typed reason, so the
        caller refuses rather than recreating the work from base somewhere else (secretary-1544).
        The four facts the recovery is allowed to rest on are the ones returned: the path, the
        branch, whether the tree is dirty, and the exact candidate commit.
        """
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
        """Raise `LegacyDispatcherRecord` when this card's record was written on Orca.

        Its workspace is an Orca worktree (`_legacy_workspace`), or a head run it carries is a legacy
        record. Asked before any launch, delivery, stop or teardown touches the record, so nothing of
        it is ever driven, stopped through a pane or re-placed silently.
        """
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

        Returns the `MergeLanding` of each of the two merge paths, which the release turns into a
        post-merge CI watch, or None when nothing was merged (noop mode, no workspace, automerge off):
        a release that merged nothing wakes the observer on its Done as before.
        """
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
                    sha=pr_merge_commit(self._run, branch, Path(record.workspace)),
                    base=base,
                    path="github-pr",
                    ci=ci,
                    branch=branch,
                )
                raise
            self._require_production_runtime("release-after")
            return MergeLanding(
                sha=pr_merge_commit(self._run, branch, Path(record.workspace)),
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
        self._remote_git_checked(
            project, record.workspace, ["push", "origin", f"{branch}:{base}"], "merge push"
        )
        self._remote_git_checked(project, repo, ["fetch", "origin", base], "post-merge fetch")
        try:
            self._advance_checkout(repo, f"origin/{base}")
        except ProductionActivationRefused as exc:
            # The push landed; only the production activation was refused.
            exc.landing = MergeLanding(
                sha=self._pushed_branch_head(record, branch), base=base, path="push", ci=ci
            )
            raise
        self._require_production_runtime("release-after")
        return MergeLanding(sha=self._pushed_branch_head(record, branch), base=base, path="push", ci=ci)

    def _advance_checkout(self, repo: Path, ref: str) -> None:
        """Fast-forward a project checkout to `ref` after a merge.

        The production checkout this dispatcher runs from moves only through
        `production_checkout.advance`: the target's board schema first, then the code
        (secretary-1824). Every other checkout keeps its plain fast-forward.
        """
        if self.mode != "noop" and _same_repo(repo, Path(self.production_runtime.product_root)):
            production_checkout.advance(self._run, repo, ref, instance_dir=self.catalog.instance_dir)
            return
        self._run(["git", "-C", str(repo), "merge", "--ff-only", ref], "post-merge fast-forward")

    def _pushed_branch_head(self, record: DispatcherRecord, branch: str) -> str:
        """The commit a `branch:base` push just landed. A non-fast-forward push is rejected, so after
        a successful one the base is exactly this branch head."""
        try:
            return self._run(
                ["git", "-C", record.workspace, "rev-parse", branch], "post-merge landed commit"
            ).stdout.strip()
        except HostError:
            return ""

    def _require_pr_base(self, record: DispatcherRecord, branch: str, base: str) -> None:
        """Refuse the merge unless the open pull request for `branch` targets `base`.

        Unreadable is refused too: the delivery boundary is irreversible, and "the backend would not
        say where this lands" is not evidence that it lands on the integration base.
        """
        completed = self._run(
            ["gh", "pr", "view", branch, "--json", "baseRefName", "-q", ".baseRefName"],
            "merge pr base",
            cwd=Path(record.workspace),
        )
        observed = (completed.stdout or "").strip()
        if observed != base:
            raise HostError(
                f"pull request for {branch!r} targets {observed or '(unreadable)'!r}, not the "
                f"integration base {base!r}; nothing was merged"
            )

    def _merge_github_pr(
        self, task: dict[str, Any], record: DispatcherRecord, branch: str, base: str
    ) -> None:
        """Land a github-CI project through its PR, then fast-forward the project's own checkout so the
        next worktree bases on the merged tree.

        gh honours branch protection and refuses to merge while required checks are unsatisfied. The
        checkout tracks the project's default branch, not the card's base, and the refresh stays
        best-effort: the card is already merged by then, so a failed refresh is not the card's failure.
        The one exception is the production checkout's activation refusal (`ProductionActivationRefused`):
        the checkout stayed where it was because the merged code moved its entrypoint or its schema was refused,
        which is the release's to report, never a refresh to forget.

        `gh pr merge` lands the pull request in *its own* base, whatever that is, so the base is read
        back and required to be this card's integration base before the irreversible call is made
        (secretary-1541). A pull request still pointing somewhere else — a card branch a stale PR was
        opened against — is refused here rather than merged into a branch nothing releases from.
        """
        self._require_pr_base(record, branch, base)
        self._run(["gh", "pr", "merge", branch, "--merge"], "merge pr", cwd=Path(record.workspace))
        repo = Path(str(self.catalog.binding(task["project"])["repo"])).expanduser()
        default_branch = self.catalog.project_default_branch(task["project"])
        # `gh pr merge` is the irreversible delivery boundary. Refreshing this checkout afterwards is
        # only a convenience for future worktree bases, and a preserved local commit can make
        # ff-only impossible: never report an already-merged card as failed because it did not apply.
        try:
            refresh_branch = base if base == default_branch else default_branch
            self._remote_git_checked(
                task["project"], repo, ["fetch", "origin", refresh_branch], "post-merge fetch"
            )
            self._advance_checkout(repo, f"origin/{refresh_branch}")
        except ProductionActivationRefused:
            raise
        except HostError:
            pass

    def stop(self, record: DispatcherRecord) -> None:
        if self.mode == "noop" or not record.workspace:
            return
        try:
            self.stop_workspace(record)
        except HostError:
            pass

    def stop_workspace(self, record: DispatcherRecord) -> None:
        """Stop every head of this workspace and let a refusal reach the caller.

        The confirmed twin of `stop`: a refused workspace stop is not evidence the head is gone, so a
        path that opens a replacement afterwards must use this one. A legacy record is refused.
        """
        if self.mode == "noop" or not record.workspace:
            return
        self._refuse_legacy_record(record, "stop the heads of")
        heartbeats: list[tuple[str, Any, str, str]] = []
        for kind in ("worker", "review"):
            field = "worker_head_run" if kind == "worker" else "review_head_run"
            pid_file = record.worker_pid_file if kind == "worker" else record.review_pid_file
            leaf = record.worker_leaf if kind == "worker" else record.review_leaf
            heartbeats.append((pid_file, getattr(record, field, {}), kind, leaf))
        # A workspace stop kills every pane it contains. Fence every recorded head before the first
        # destructive call, not afterwards when a live process may already have lost its pane.
        for pid_file, run, kind, leaf in heartbeats:
            self._guard_head_run(run, kind, pid_file=pid_file, leaf=leaf)
        self._stop_recorded_heads(record.workspace, [(run, kind) for _, run, kind, _ in heartbeats])
        for pid_file, run, kind, leaf in heartbeats:
            self._confirm_head_process_gone(
                pid_file,
                run=run,
                role=kind,
                leaf=leaf,
            )

    def _stop_recorded_heads(self, workspace: str, runs: Sequence[tuple[Any, str]]) -> None:
        """Take one workspace's heads down, each by its own run's stop on the backend holding it.

        The backend is chosen from the durable run the record names, never from the profile a later
        registry would resolve. A supervised head owns no pane, so nothing about a worktree can end
        it: a refusal is raised rather than absorbed, because the callers of this are exactly the
        ones that go on to remove the worktree or open a replacement head. A workspace that names no
        run has no head to stop: a bring-up that never got as far as a durable run raised nothing.
        """
        for run, role in ((_durable_head_run(run), role) for run, role in runs):
            if run is None:
                # Head exit and scope cleanup are separate. Even a settled HeadRun can
                # still have a durable scope owner that must be checked before removal.
                continue
            receipt = self.head_runtime_for(run).stop(
                run,
                head_ops.StopInitiator(actor=STOPPED_BY_DISPATCHER),
            )
            if not receipt.ok:
                raise HostError(f"the {role} head of {workspace} was not stopped: {receipt.reason}")

    def fence_cleanup_scopes(self, workspace: str, task: head_ops.TaskRef,
                             runs: Sequence[head_ops.HeadRun], *, recorded_only: bool = False) -> None:
        """Read scope ownership through the runtime's supported boundary."""
        from ummanu.runtime.local_pty_head import fence_cleanup_scopes
        try:
            fence_cleanup_scopes(Path(self._local_pty_root()), workspace, task, runs,
                                 recorded_only=recorded_only)
        except (OSError, ValueError, RuntimeError) as exc:
            raise HostError(f"cleanup scope evidence unavailable: {exc}") from exc

    def stop_head(self, record: DispatcherRecord, kind: str, initiator: str = STOPPED_BY_DISPATCHER) -> None:
        """Stop one role's head through the head operation, recording who ended it."""
        if self.mode == "noop":
            return
        self._refuse_legacy_record(record, f"stop the {'reviewer' if kind == 'review' else 'worker'} of")
        if kind == "review":
            self.stop_review_head(record, initiator)
            return
        self.stop_worker_head(record, initiator)

    def stop_review_head(self, record: DispatcherRecord, initiator: str = STOPPED_BY_DISPATCHER) -> None:
        """End this card's reviewer through the head operation, recording who ended it."""
        run = self.review_lifecycle_run(record)
        if run.settled:
            self._confirm_settled_head(run, "reviewer")
            return
        receipt = self.head_runtime_for(run).stop(
            run,
            head_ops.StopInitiator(actor=initiator),
            transport=self._head_transport(record.workspace, role="reviewer"),
            commit=lambda finishing: self._commit_review_run(record, finishing),
            preflight=lambda current: self._guard_head_run(current, "reviewer"),
            confirm_gone=lambda path: self._confirm_head_process_gone(
                path,
                run=run,
                role="reviewer",
            ),
        )
        if not receipt.ok:
            # A preflight mismatch is intentionally uncommitted: that foreign process was never this run.
            raise HostError(receipt.reason)
        self._commit_review_run(record, receipt.run)

    def _confirm_settled_head(self, run: head_ops.HeadRun, role: str) -> None:
        """A stop of a head whose end was already confirmed keeps that receipt as the record.

        Nothing is minted or committed: a fresh identity would name a head that was never launched
        (secretary-1918). The pid file is still read through the settled run, so a live foreign
        process there is fenced and this run, should it still be alive, is taken down.
        """
        self._guard_head_run(run, role)
        self._confirm_head_process_gone(run.pid_file, run=run, role=role)

    def _commit_review_run(self, record: DispatcherRecord, run: head_ops.HeadRun) -> None:
        """Write this reviewer's run onto the record, flushing it when the caller lent us the state."""
        record.review_head_run = run.to_json()
        if self.commit_state is not None:
            self.commit_state()

    def review_lifecycle_run(self, record: DispatcherRecord) -> head_ops.HeadRun:
        """This card's reviewer as the head operations see it."""
        stored = record.review_head_run if isinstance(record.review_head_run, dict) else {}
        run: head_ops.HeadRun | None = None
        if stored.get("run_id"):
            try:
                run = head_ops.HeadRun.from_json(stored)
            except (head_ops.HeadRunError, head_ops.TaskRefError):
                run = None
        if run is None:
            # A record with no run at all was written before runs were recorded, and carries the
            # record rule: a legacy one. A settled run is kept: it is the truthful stop receipt.
            run = head_ops.HeadRun(
                run_id=head_ops.new_run_id(),
                spec=HeadSpec(
                    profile_id=record.review_head,
                    adapter=self._prompt_adapter(record.review_run, record.review_head),
                ),
                workspace=record.workspace,
                # The reviewer's own worker id is the card's, as the claim built it: `<ref>-<slug>`.
                # Inventing a card reference this call never received would not be truthful.
                task_ref=head_ops.TaskRef.card(record.worker or record.review_head or "unknown-reviewer"),
            )
        return replace(
            run,
            workspace=record.workspace or run.workspace,
            handle=record.review_handle,
            leaf=record.review_leaf,
            pid_file=record.review_pid_file,
        )

    def stop_worker_head(self, record: DispatcherRecord, initiator: str = STOPPED_BY_DISPATCHER) -> None:
        """End this card's worker through the head operation, recording who ended it."""
        run = self.worker_lifecycle_run(record)
        if run.settled:
            self._confirm_settled_head(run, "worker")
            return
        receipt = self.head_runtime_for(run).stop(
            run,
            head_ops.StopInitiator(actor=initiator),
            transport=self._head_transport(record.workspace, role="worker"),
            commit=lambda finishing: self._commit_worker_run(record, finishing),
            preflight=lambda current: self._guard_head_run(current, "worker"),
            confirm_gone=lambda path: self._confirm_head_process_gone(
                path,
                run=run,
                role="worker",
            ),
        )
        if not receipt.ok:
            # A failed preflight must leave the persisted HeadRun untouched.
            raise HostError(receipt.reason)
        self._commit_worker_run(record, receipt.run)

    def _commit_worker_run(self, record: DispatcherRecord, run: head_ops.HeadRun) -> None:
        """Write this worker's run onto the record, and flush it if the caller gave us the state."""
        record.worker_head_run = run.to_json()
        if self.commit_state is not None:
            self.commit_state()

    def commit_gate_pr_authorship(
        self, record: DispatcherRecord, entry: GatePrAuthorship | dict[str, Any]
    ) -> None:
        """Write down that the github gate wrote a known text on a known pull request."""
        record.gate_pr_authorship = entry.to_json() if isinstance(entry, GatePrAuthorship) else dict(entry)
        if self.commit_state is not None:
            self.commit_state()

    def commit_gate_published_ref(
        self, record: DispatcherRecord, entry: GatePublishedRef | dict[str, Any]
    ) -> None:
        """Persist the branch and object id the gate just published, as the next push's lease."""
        record.gate_published_ref = entry.to_json() if isinstance(entry, GatePublishedRef) else dict(entry)
        if self.commit_state is not None:
            self.commit_state()

    def worker_lifecycle_run(self, record: DispatcherRecord) -> head_ops.HeadRun:
        """This card's worker as the head operations see it."""
        stored = record.worker_head_run if isinstance(record.worker_head_run, dict) else {}
        run: head_ops.HeadRun | None = None
        if stored.get("run_id"):
            try:
                run = head_ops.HeadRun.from_json(stored)
            except (head_ops.HeadRunError, head_ops.TaskRefError):
                run = None
        if run is None:
            # A record with no run at all was written before runs were recorded, and carries the
            # record rule: a legacy one. A settled run is kept: it is the truthful stop receipt.
            run = head_ops.HeadRun(
                run_id=head_ops.new_run_id(),
                spec=HeadSpec(
                    profile_id=record.head,
                    adapter=self._prompt_adapter(record.worker_run, record.head),
                ),
                workspace=record.workspace,
                # No card reference reaches this call, but the worker id carries one: `<ref>-<slug>`
                # is what the claim built it from. Inventing a card reference would not be truthful.
                task_ref=head_ops.TaskRef.card(record.worker or record.head or "unknown-worker"),
            )
        return replace(
            run,
            workspace=record.workspace or run.workspace,
            handle=record.handle,
            leaf=record.worker_leaf,
            pid_file=record.worker_pid_file,
        )

    @staticmethod
    def _head_status(
        pid_file: str,
        *,
        run: Any = None,
        role: str = "",
        task: str = "",
        leaf: str = "",
        expected: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Classify a head through its persisted run whenever one is available."""
        if run is not None:
            return _head_run_process_status(
                pid_file,
                run=run,
                role=role,
                task=task,
                leaf=leaf,
            )
        return _head_process_status(pid_file, expected=expected)

    def _guard_head_run(
        self,
        run: Any,
        role: str,
        *,
        pid_file: str = "",
        task: str = "",
        leaf: str = "",
    ) -> dict[str, Any]:
        """Fence a live foreign process before any lifecycle attribution or destructive call."""
        if not pid_file:
            pid_file = str(getattr(run, "pid_file", "") or "")
            if isinstance(run, dict):
                pid_file = str(run.get("pid_file") or pid_file)
        if not pid_file:
            return {"known": False, "reason": "missing-pid-file"}
        try:
            return _guard_head_run_identity(
                pid_file,
                run=run,
                role=role,
                task=task,
                leaf=leaf,
            )
        except _HeadRunIdentityMismatch:
            raise HostError(f"head heartbeat from {pid_file} has a mismatching launch identity") from None

    def _confirm_head_process_gone(
        self,
        pid_file: str,
        *,
        run: Any = None,
        role: str = "",
        task: str = "",
        leaf: str = "",
        expected: dict[str, str] | None = None,
    ) -> None:
        """Make sure the process behind a heartbeat is not running, escalating if it is."""
        if not pid_file:
            return
        for signal_number in (signal.SIGTERM, signal.SIGKILL):
            status = self._head_status(
                pid_file,
                run=run,
                role=role,
                task=task,
                leaf=leaf,
                expected=expected,
            )
            if _heartbeat_is_mismatch(status):
                raise HostError(f"head heartbeat from {pid_file} has a mismatching launch identity")
            if not status.get("known") or not status.get("alive"):
                if status.get("known"):
                    _clear_head_heartbeat(pid_file)
                return
            # SIGTERM and SIGHUP stay pending for a SIGSTOPed retained worker: wake its group before
            # the graceful signal, or the green handoff waits out the grace period and then kills it.
            if signal_number == signal.SIGTERM:
                self._signal_head(
                    pid_file,
                    signal.SIGCONT,
                    run=run,
                    role=role,
                    task=task,
                    leaf=leaf,
                    expected=expected,
                )
            self._signal_head(
                pid_file,
                signal_number,
                run=run,
                role=role,
                task=task,
                leaf=leaf,
                expected=expected,
            )
            self._await_head_exit(
                pid_file,
                run=run,
                role=role,
                task=task,
                leaf=leaf,
                expected=expected,
            )
        status = self._head_status(
            pid_file,
            run=run,
            role=role,
            task=task,
            leaf=leaf,
            expected=expected,
        )
        if _heartbeat_is_mismatch(status):
            raise HostError(f"head heartbeat from {pid_file} has a mismatching launch identity")
        if status.get("known") and status.get("alive"):
            raise HostError(f"head process from {pid_file} is still running after stop")
        if status.get("known"):
            _clear_head_heartbeat(pid_file)

    def _signal_head(
        self,
        pid_file: str,
        signal_number: int,
        *,
        run: Any = None,
        role: str = "",
        task: str = "",
        leaf: str = "",
        expected: dict[str, str] | None = None,
    ) -> None:
        status = self._head_status(
            pid_file,
            run=run,
            role=role,
            task=task,
            leaf=leaf,
            expected=expected,
        )
        if not _heartbeat_is_live_match(status):
            return
        pid = int(status["pid"])
        try:
            # The terminal gives an interactive head its own foreground process group, so this
            # reaches helpers without detaching the head from its controlling terminal. Old launches
            # and focused tests can share our group: never turn that into a signal to the dispatcher.
            group = os.getpgid(pid)
            if group != os.getpgrp():
                os.killpg(group, signal_number)
            else:
                os.kill(pid, signal_number)
        except ProcessLookupError:
            return
        except OSError as exc:
            raise HostError(f"head process {pid} could not be signalled: {exc}") from None

    def _await_head_exit(
        self,
        pid_file: str,
        *,
        run: Any = None,
        role: str = "",
        task: str = "",
        leaf: str = "",
        expected: dict[str, str] | None = None,
    ) -> None:
        deadline = time.monotonic() + HEAD_STOP_GRACE_SECONDS
        while time.monotonic() < deadline:
            status = self._head_status(
                pid_file,
                run=run,
                role=role,
                task=task,
                leaf=leaf,
                expected=expected,
            )
            if _heartbeat_is_mismatch(status):
                return
            if not status.get("known") or not status.get("alive"):
                return
            time.sleep(HEAD_STOP_POLL_SECONDS)

    def freeze_worker(self, record: DispatcherRecord) -> None:
        """Shut this card's worker head down and confirm it. Raises when it cannot be confirmed."""
        self._freeze_worker(record)

    @serialized
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
        task_ref = next((raw.get("task_ref", {}).get("ref") for raw in
                        (record.worker_head_run, record.review_head_run)
                        if raw.get("task_ref", {}).get("ref")), "")
        if not task_ref:
            task_ref = next((ref for ref, raw in owner._state().get("records", {}).items()
                             if raw.get("attempt_id") == record.attempt_id and raw.get("worker") == record.worker), "")
        if not task_ref:
            raise HostError("cleanup cannot identify the exact card")
        result = owner.cleanup(owner.runtime.reader.show(task_ref), record, "done")
        return {"status": result["status"], "reason": result["reason"], "progress": result["progress"]}

    def _fetch_seed(self, repo: Path, seed: str, *, project: str) -> str:
        """Bring `seed` into the project checkout and return the start point a worktree is cut at.

        A branch seed is fetched by name and cut at its remote-tracking ref, which is what every
        card did before seeds existed. An exact object id — the predecessor candidate a reslice
        successor inherits — is not a ref the remote will serve by name, so the whole remote is
        fetched and the object is then required to be present: a seed that is not there is this
        card's own contract failing, not a checkout to invent.
        """
        if _is_exact_ref_sha(seed):
            self._remote_git_checked(project, repo, ["fetch", "origin"], "git fetch")
            try:
                self._run(["git", "-C", str(repo), "cat-file", "-e", f"{seed}^{{commit}}"], "git seed probe")
            except HostError:
                raise HostError(
                    f"seed commit {seed[:12]} is not on the project remote; the predecessor "
                    "candidate this card inherits was never published or has been removed",
                    bring_up_cause=CAUSE_BASE_BRANCH_CONTRACT,
                ) from None
            return seed
        self._remote_git_checked(project, repo, ["fetch", "origin", seed], "git fetch")
        return f"origin/{seed}"

    def _validate_resumable_workspace(self, task: dict[str, Any], workspace: str) -> None:
        """Accept only the registered project worktree on this card's worker branch."""
        if self.mode == "noop":
            return
        path = Path(workspace)
        if not path.is_dir():
            raise HostError("resume workspace is not a directory", bring_up_cause=CAUSE_WORKSPACE_CONTRACT)
        try:
            top_level = self._run(
                ["git", "-C", workspace, "rev-parse", "--show-toplevel"], "resume workspace git check"
            ).stdout.strip()
        except HostError as exc:
            raise HostError(
                "resume workspace is not a git worktree", bring_up_cause=CAUSE_WORKSPACE_CONTRACT
            ) from exc
        if not _same_repo(Path(top_level), path):
            raise HostError(
                "resume workspace git root does not match its expected path",
                bring_up_cause=CAUSE_WORKSPACE_CONTRACT,
            )
        branch = self._run(
            ["git", "-C", workspace, "branch", "--show-current"], "resume workspace branch check"
        ).stdout.strip()
        expected_branch = _legacy_worker_branch(task["ref"])
        if branch != expected_branch:
            raise HostError(
                f"resume workspace is on branch {branch or '(detached)'}, expected {expected_branch}",
                bring_up_cause=CAUSE_WORKSPACE_CONTRACT,
            )
        repo = Path(str(self.catalog.binding(task["project"])["repo"])).expanduser()
        if not repo.is_dir():
            raise HostError("resume project repo is unavailable")
        listing = self._run(
            ["git", "-C", str(repo), "worktree", "list", "--porcelain"], "resume workspace ownership check"
        ).stdout
        registered = {
            line.removeprefix("worktree ").strip()
            for line in listing.splitlines()
            if line.startswith("worktree ")
        }
        if not any(_same_repo(Path(candidate), path) for candidate in registered):
            raise HostError(
                "resume workspace is not a registered worktree of the project repo",
                bring_up_cause=CAUSE_WORKSPACE_CONTRACT,
            )

    def _run_setup(self, project: str, workspace: str) -> None:
        if self.mode == "noop":
            return
        adapter = self.catalog.adapter(project)
        for command in adapter.get("setup", {}).get("commands", []):
            self._run_adapter_shell(str(command), Path(workspace), "setup command")
        smoke = adapter.get("smoke", {}).get("command")
        if smoke:
            self._run_adapter_shell(str(smoke), Path(workspace), "smoke command")

    @staticmethod
    def _workspace_environment(workspace: str | Path) -> Path:
        """The reserved dispatcher-owned environment; adapter-owned ``.venv`` is disjoint."""
        return Path(workspace) / WORKSPACE_ENV_DIR

    @classmethod
    def _workspace_environment_owner(cls, workspace: str | Path) -> Path:
        return cls._workspace_environment(workspace).parent / "owner.json"

    @classmethod
    def _workspace_environment_ready_file(cls, workspace: str | Path) -> Path:
        return cls._workspace_environment(workspace).parent / "ready"

    @classmethod
    def _workspace_environment_ready(cls, workspace: str | Path) -> bool:
        return cls._workspace_environment_ready_file(workspace).is_file()

    def _claim_workspace_environment(self, workspace: str) -> Path:
        """Determine dispatcher ownership before creating or populating its reserved environment."""
        root = Path(workspace).resolve(strict=True)
        self._exclude_workspace_environment(root)
        environment = self._workspace_environment(root)
        owner = self._workspace_environment_owner(root)
        expected = {"owner": "ummanu-dispatcher", "schema_version": 1, "workspace": str(root)}
        if self._decide_workspace_environment_ownership(workspace) == "dispatcher":
            return environment
        environment.parent.mkdir(parents=True)
        try:
            write_text_atomic(owner, json.dumps(expected, sort_keys=True) + "\n")
        except RuntimeError as exc:
            raise HostError(f"workspace Python environment ownership could not be written: {exc}") from None
        return environment

    def _exclude_workspace_environment(self, root: Path) -> None:
        """Keep everything the pipeline writes here out of this repository's candidate content.

        Only the lines of `WORKSPACE_EXCLUDES` the file lacks are appended, after whatever it already
        holds, so a set a crash left half-written is completed by the next bring-up. For a linked
        worktree Git resolves this to the repository's shared exclude file.
        """
        located = self._run(
            ["git", "-C", str(root), "rev-parse", "--git-path", "info/exclude"],
            "workspace Git exclude",
            cwd=root,
        ).stdout.strip()
        if not located:
            raise HostError("workspace Git exclude path is unavailable")
        exclude = Path(located)
        if not exclude.is_absolute():
            exclude = root / exclude
        try:
            current = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        except (OSError, UnicodeError) as exc:
            raise HostError(f"workspace Git exclude could not be read: {exc}") from None
        present = {line.strip() for line in current.splitlines()}
        missing = [pattern for pattern in WORKSPACE_EXCLUDES if pattern not in present]
        if not missing:
            return
        separator = "" if not current or current.endswith("\n") else "\n"
        try:
            write_text_atomic(exclude, current + separator + "".join(f"{pattern}\n" for pattern in missing))
        except RuntimeError as exc:
            raise HostError(f"workspace Git exclude could not be written: {exc}") from None

    def _candidate_environment_install_required(self, project: str) -> bool:
        """Use the adapter's existing default-interpreter choice, never a project-name heuristic."""
        if not project:
            return False
        broad_check = self.catalog.adapter(project).get("broad_check")
        return isinstance(broad_check, dict) and "interpreter" not in broad_check

    def _prepare_workspace_environment(self, workspace: str, *, project: str = "") -> None:
        """Create and populate only the environment the dispatcher has explicitly claimed."""
        if self.mode == "noop":
            return
        root = Path(workspace)
        environment = self._claim_workspace_environment(workspace)
        if self._workspace_environment_ready(workspace):
            self._require_workspace_environment(workspace)
            # A venv made ready before the startup file existed stays acceptable; it gains the file
            # here when its layout allows, and keeps working without it otherwise.
            self._install_workspace_pycache_prefix(root, environment, required=False)
            return
        self._run(
            [self.production_runtime.interpreter, "-m", "venv", str(environment)],
            "workspace Python environment",
            cwd=root,
        )
        self._install_workspace_pycache_prefix(root, environment, required=True)
        if self._candidate_environment_install_required(project) and (root / "pyproject.toml").is_file():
            before = self._workspace_extra_files(root)
            self._run(
                [str(environment / "bin" / "python3"), "-m", "pip", "install", "-e", ".[dev]"],
                "workspace candidate dependencies",
                cwd=root,
            )
            self._record_install_output(root, before)
        try:
            write_text_atomic(self._workspace_environment_ready_file(workspace), "ready\n")
        except RuntimeError as exc:
            raise HostError(f"workspace Python environment readiness could not be written: {exc}") from None
        self._require_workspace_environment(workspace)

    def _install_workspace_pycache_prefix(self, root: Path, environment: Path, *, required: bool) -> None:
        """Point every interpreter of the owned venv at the owned bytecode cache.

        Tests start child interpreters with environments built from scratch, which drop the head's
        PYTHONPYCACHEPREFIX; the startup file in site-packages applies to them all the same. It is
        written only into the venv's single site-packages resolving inside the owned namespace.
        """
        try:
            resolved = root.resolve(strict=True)
            namespace = (resolved / WORKSPACE_NAMESPACE).resolve(strict=True)
            found = [path for path in environment.glob("lib/python3*/site-packages") if path.is_dir()]
            inside = len(found) == 1 and found[0].resolve(strict=True).is_relative_to(namespace)
        except (OSError, RuntimeError, ValueError):
            inside = False
        if not inside:
            if required:
                raise HostError(f"workspace Python environment site-packages is unavailable at {environment}")
            return
        file = found[0] / WORKSPACE_PYCACHE_PTH
        body = workspace_pycache_pth(resolved)
        with contextlib.suppress(OSError, UnicodeError):
            if not file.is_symlink() and file.read_text(encoding="utf-8") == body:
                return
        try:
            write_text_atomic(file, body)
        except RuntimeError as exc:
            if required:
                raise HostError(f"workspace bytecode redirect could not be written: {exc}") from None

    def _workspace_extra_files(self, root: Path) -> set[str]:
        """Every untracked or ignored path in `root`'s source tree, outside the owned namespace."""
        status = self._run(
            ["git", "-C", str(root), "status", "--porcelain=v1", "--ignored", "--untracked-files=all", "-z",
             "--", ".", f":(exclude){WORKSPACE_NAMESPACE}"],
            "workspace generated files",
            cwd=root,
        ).stdout
        return {row[3:] for row in status.split("\0") if row[:2] in {"??", "!!"}}

    def _record_install_output(self, root: Path, before: set[str]) -> None:
        """Record what the editable install created in the source tree as exact generated bytes.

        Only files that did not exist before the install are recorded, so the cleanup owner can tell
        the dispatcher's own install output from author work. A later rewrite stops matching.
        """
        if self.mode != "real":
            return
        from ummanu.dispatch.cleanup import CleanupJournal
        journal = CleanupJournal(self.data_dir)
        for name in sorted(self._workspace_extra_files(root) - before):
            file = root.resolve() / name
            if file.is_file() and not file.is_symlink():
                journal.generated(file, file.read_bytes())

    @staticmethod
    def _workspace_python(workspace: str | Path) -> Path:
        return Path(workspace) / WORKSPACE_ENV_DIR / "bin" / "python3"

    def _decide_workspace_environment_ownership(self, workspace: str) -> str:
        """Classify the reserved namespace without requiring a pre-upgrade environment."""
        if self.mode == "noop":
            return "absent"
        root = Path(workspace).resolve(strict=False)
        environment = self._workspace_environment(root)
        namespace = environment.parent
        if not namespace.exists():
            return "absent"
        owner = self._workspace_environment_owner(root)
        expected = {"owner": "ummanu-dispatcher", "schema_version": 1, "workspace": str(root)}
        try:
            observed = json.loads(owner.read_text(encoding="utf-8"))
            inside = namespace.resolve(strict=True).is_relative_to(root)
        except (OSError, RuntimeError, UnicodeError, ValueError):
            raise HostError(f"workspace Python environment ownership is unavailable at {namespace}") from None
        if observed != expected or not inside:
            raise HostError(f"workspace Python environment ownership is invalid at {namespace}")
        return "dispatcher"

    def _require_workspace_environment(self, workspace: str) -> None:
        if self.mode == "noop":
            return
        environment = self._workspace_environment(workspace)
        python = self._workspace_python(workspace)
        try:
            root = Path(workspace).resolve(strict=True)
            inside = environment.resolve(strict=True).is_relative_to(root)
        except (OSError, RuntimeError, ValueError):
            inside = False
        if (
            self._decide_workspace_environment_ownership(workspace) != "dispatcher"
            or not inside
            or not self._workspace_environment_ready(workspace)
            or not os.access(python, os.X_OK)
        ):
            raise HostError(f"workspace Python environment is unavailable at {environment}")

    def _run_adapter_shell(self, command: str, cwd: Path, label: str) -> None:
        """Run adapter setup without lending it the production or dispatcher environment."""
        production_bin = Path(self.production_runtime.interpreter).parent.resolve(strict=False)
        safe_path = os.pathsep.join(
            entry
            for entry in os.environ.get("PATH", "").split(os.pathsep)
            if entry and Path(entry).resolve(strict=False) != production_bin
        )
        prefix = f"PATH={shlex.quote(safe_path)}; unset VIRTUAL_ENV; export PATH; "
        self._run_shell(prefix + command, cwd, label)

    def production_runtime_provenance(self) -> RuntimeProvenance:
        """Structured, secret-free observation used by every production runtime fence."""
        return self.production_runtime.probe()

    def _require_production_runtime(self, boundary: str) -> RuntimeProvenance:
        if self.mode == "noop":
            return RuntimeProvenance(
                "valid",
                self.production_runtime.interpreter,
                self.production_runtime.product_root,
                "noop",
                (),
            )
        result = self.production_runtime_provenance()
        if not result.valid:
            error = HostError(result.refusal(boundary))
            error.evidence = result.as_dict()
            raise error
        return result

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
    ) -> LaunchedHead:
        """Bring one head up and hand back the pane together with the configuration it started with."""
        if role in {WORKER_ROLE, REVIEW_ROLE, "reviewer"}:
            self._require_production_runtime(f"{role}-launch")
            self._require_workspace_environment(workspace)
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
        receipt = self.head_runtime_for(spec).start(
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
            commit=ingress.commit_run if ingress is not None else None,
        )
        if not receipt.ok:
            failed_run = receipt.run
            if pid_file and failed_run is not None and failed_run.leaf:
                # Delivery can refuse with a live pane before the bring-up returns normally. Bind
                # that pane to the written heartbeat before persisting the failed launch intent.
                _bind_head_heartbeat(pid_file, expected=heartbeat, leaf=failed_run.leaf)
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
        """The launch shape the run is recorded with, degrading rather than failing a live bring-up.

        A degraded spec is still a head this host raises itself, so it names `local-pty` rather than
        taking the record rule a hand-built spec otherwise carries.
        """
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
        """Attach the exact worker/reviewer document before a pane can observe it.

        TASK.md is a mutable workspace projection, so routing may not read it after delivery.
        The document digest is a required launch fact: a read failure aborts the bring-up before a
        head can receive an instruction whose durable identity the dispatcher cannot record.
        """
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

    @property
    def head_runtime(self) -> Any:
        """The backend a head with no runtime of its own is read on, for a caller with none to name.

        Every operation of this dispatcher that acts on a head — including the workspace-scoped
        cleanups, which choose from the durable run the record names — goes through
        `head_runtime_for` instead. What is left on this property is the reading a caller outside
        the lifecycle does when it wants the default backend as an object and has nothing to
        resolve it from. That is the record rule (`RECORD_RUNTIME_WHEN_ABSENT`), not the profile
        default: nothing here reads a profile.
        """
        return self.head_runtime_for(None)

    def head_runtime_for(self, subject: Any = None) -> Any:
        """The backend this head is held by — the one place a `runtime` value becomes an object.

        One resolver rather than a branch at each caller: every lifecycle site hands over the head
        it is acting on (a `HeadRun`, a `HeadSpec`, or the name itself) and is handed back the
        backend that head's profile named, so no caller has to know that there is more than one.
        `None` is the operation that names no head at all and gets the record rule.

        The value is read off the head rather than re-resolved from the registry, because the
        registry can be repointed while a head is running: a head raised on one backend has to go
        on being observed and stopped through that backend until it ends.
        """
        return self._head_runtime_named(_head_runtime_name(subject))

    def _head_runtime_named(self, name: str) -> Any:
        """Build — or hand back — the one instance of the backend called `name`.

        An unknown name cannot arrive from a validated registry (`validate_launch_shape` refuses it
        when the table loads), so reaching this refusal means a record or a caller invented one,
        and it fails closed by name rather than falling back to a backend the head is not on. A
        legacy Orca record (`orca-legacy`, or no runtime at all) is refused with
        `LegacyDispatcherRecord`: no backend is built for it, so it is never launched, delivered to
        or stopped.
        """
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
        """Where this dispatcher's supervised heads keep their run directories.

        Deliberately short and directly under the data directory: a run directory holds a Unix
        socket, whose address the kernel bounds at about a hundred bytes, and the substrate refuses
        an address it cannot fit rather than failing opaquely later.
        """
        return self.data_dir / "heads"

    def _open_head_pane(
        self,
        run: head_ops.HeadRun,
        title: str,
        command: str,
        *,
        env: Mapping[str, str] | None = None,
    ) -> head_ops.HeadRun:
        """Bring a head up in a pane of its own, with no prompt delivered by the bring-up.

        The observer is the one head whose delivery contour is its own: it opens the pane, then puts
        its launch prompt in front of it through the same boundary, so that the two halves can be
        accounted for separately. `start` with no pointer is exactly that pane and nothing else.
        """
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
        )
        if not receipt.ok:
            raise HostError(receipt.reason)
        return receipt.run

    def _freeze_worker(self, record: DispatcherRecord) -> None:
        """Shut the worker head down now that the reviewer is up, leaving the workspace untouched.

        Nothing else stops the worker from editing the checkout mid-review. A worker adopted from a
        launch intent has no pane handle, so the stop goes by its pid heartbeat instead.
        """
        if self.mode == "noop" or not (record.handle or record.worker_leaf or record.worker_pid_file):
            return
        self.stop_head(record, "worker", STOPPED_BY_REVIEW_FREEZE)

    def retain_worker(self, record: DispatcherRecord) -> None:
        """Suspend a completed worker without throwing its provider conversation away.

        A missing or dead pid heartbeat is not safe to retain, and a head with no pane handle is not
        retained at all: the caller falls back through the confirmed-stop and durable replacement path
        rather than guessing that a pane is idle.
        """
        if self.mode == "noop":
            raise HostError("noop runtime cannot retain a worker session")
        if not record.handle:
            raise HostError("worker session has no addressable pane to retain")
        status = self._head_status(
            record.worker_pid_file,
            run=record.worker_head_run,
            role="worker",
            leaf=record.worker_leaf,
        )
        if not _heartbeat_is_live_match(status):
            raise HostError("worker session is unavailable for retention")
        try:
            self._signal_head(
                record.worker_pid_file,
                signal.SIGSTOP,
                run=record.worker_head_run,
                role="worker",
                leaf=record.worker_leaf,
            )
        except (KeyError, TypeError, ValueError, OSError) as exc:
            raise HostError(f"worker session could not be suspended: {exc}") from None
        deadline = time.monotonic() + HEAD_STOP_GRACE_SECONDS
        while time.monotonic() < deadline:
            retained = self._head_status(
                record.worker_pid_file,
                run=record.worker_head_run,
                role="worker",
                leaf=record.worker_leaf,
            )
            if _heartbeat_is_live_match(retained) and retained.get("stopped"):
                return
            if not retained.get("alive"):
                break
            time.sleep(HEAD_STOP_POLL_SECONDS)
        raise HostError("worker session could not be confirmed suspended")

    def _continuation_addressable(self, record: DispatcherRecord) -> bool:
        """Whether this worker head is a live provider conversation a prompt can be sent into."""
        run = record.worker_run
        adapter = run.get("adapter")
        return bool(record.handle) and (
            adapter == "claude"
            or (adapter == "codex" and str(run.get("codex_mode") or CODEX_TUI_MODE) == CODEX_TUI_MODE)
        )

    def worker_retained_alive(self, record: DispatcherRecord) -> bool:
        """Whether this card's worker session is confirmably alive and still suspended."""
        if self.mode == "noop" or not record.worker_continuation.retained:
            return False
        status = self._record_heartbeat_status(record, "worker")
        return bool(_heartbeat_is_live_match(status) and status.get("stopped"))

    def confirm_worker_retained(self, record: DispatcherRecord) -> None:
        """Assert the retained worker is still suspended, or raise for the caller's stop path."""
        if self.mode == "noop":
            return
        if not self.worker_retained_alive(record):
            raise HostError("retained worker session is no longer confirmably suspended")

    def worker_retained_vanished(self, record: DispatcherRecord) -> bool:
        """Whether this card's retained worker is provably gone, so nothing is left to freeze.

        Only the pid heartbeat's definitive death signal (`known and not alive`) counts; the ambiguous
        `known: False` of a heartbeat that was never written stays on the confirm-or-freeze path.
        """
        if self.mode == "noop" or not record.worker_continuation.retained:
            return False
        status = self._record_heartbeat_status(record, "worker")
        return _heartbeat_is_dead(status)

    def worker_addressable(self, record: DispatcherRecord) -> bool:
        """Whether this card's worker is a live conversation a prompt could be typed into."""
        if self.mode == "noop":
            return False
        return self._continuation_addressable(record)

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
        """Whether this card's worker is live, running and addressable, so a comment pointer can
        reach it now. A suspended, exited or unaddressable worker is sent nothing: the next round's
        TASK.md carries the comment instead (secretary-1768)."""
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
        """Rewrite the live worker's TASK.md with the card's comments, then point it there.

        The document is the round's own, re-rendered with the generation, decision and
        prerequisites the record froze for it, so the only thing that changes under the worker is
        the comments section. Its report bodies are not cleared: the round is not over.
        """
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

    def resume_worker(self, task: dict[str, Any], record: DispatcherRecord) -> None:
        """Resume an addressable retained worker and deliver its updated rework task."""
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
        )

    def _nudge_worker(
        self,
        record: DispatcherRecord,
        pointer: head_ops.NudgePointer,
        what: str,
        *,
        subject: str,
        before_send: Callable[[], None] | None = None,
    ) -> None:
        """Point this card's live worker at one thing, through the head operation (secretary-1412)."""
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
            )
        except LegacyDispatcherRecord:
            raise
        except (TuiDeliveryError, HostError) as exc:
            failure = HostError(f"{what} was not delivered: {exc}")
            failure.evidence = getattr(exc, "evidence", None)
            raise failure from None
        if not receipt.ok:
            failure = HostError(f"{what} was not delivered: {receipt.reason}")
            failure.evidence = receipt.evidence
            raise failure from None
        record.worker_head_run = receipt.run.to_json()
        evidence = _delivery_evidence_json(receipt.delivery, subject)
        source = receipt.run.fanout_policy.get("provider_source")
        if receipt.run.spec.adapter == "codex" and isinstance(source, dict):
            evidence["provider_bound"] = source.get("state") == "bound"
            evidence["provider_source_state"] = str(source.get("state") or "unknown")
        _record_worker_delivery_evidence(record, evidence)

    def _write_prompt(self, path: Path, body: str) -> None:
        write_text_atomic(path, body)
        if self.mode == "real" and path.name in {"TASK.md", OBSERVER_PROMPT_FILE}:
            from ummanu.dispatch.cleanup import CleanupJournal
            CleanupJournal(self.data_dir).generated(path, body)

    def _require_cleanup_admission(self, reference: str) -> None:
        if self.mode == "real":
            refusal = CleanupJournal(self.data_dir).admission_refusal(reference)
            if refusal:
                raise HostError(refusal)

    def _review_document(
        self,
        task: dict[str, Any],
        record: DispatcherRecord,
        *,
        local_run_policy: tuple[dict[str, Any] | None, bool] | None = None,
    ) -> tuple[Path, str]:
        """This round's review task, on disk, and the one line that points a reviewer at it.

        The text never travels through the pane; only a bounded pointer does. A retry re-renders the
        same document at the same path, and a document that cannot be written stops the bring-up before
        a pane is opened. Nothing here writes to or removes anything from the candidate checkout.
        """
        document = self._prompt_document_path(REVIEW_ROLE, task["ref"], record.review_baseline)
        prompt = self._review_prompt(
            task, record.attempt_id, record.review_baseline, record=record,
            local_run_policy=local_run_policy,
        )
        try:
            _write_prompt_document(document, prompt, outside=Path(record.workspace))
            nudge = _nudge_for(document)
        except PromptDocumentError as exc:
            raise HostError(f"the reviewer task document could not be prepared: {exc}") from None
        return document, nudge

    def _prompt_document_path(self, role: str, reference: str, round_number: int) -> Path:
        """Where a head's task document lives: outside every worktree, with the run's artifacts."""
        root = os.environ.get("UMMANU_DISPATCHER_PROMPT_DIR")
        base = Path(root).expanduser() if root else self.data_dir / "artifacts" / "prompts"
        name = f"{_request_token(role)}-{_request_token(str(round_number))}.md"
        return (base / _request_token(reference) / name).resolve()

    def _clear_body_file(self, kind: str, reference: str, review_round: int) -> None:
        """Drop the body file before launching the head that is supposed to write it.

        The path is keyed on ref+round and heads are told to leave the file in place, so a respawned
        head would inherit its predecessor's body; nothing downstream rejects a stale one. A missing
        file at least fails loudly. Reports go through `_clear_report_bodies`.
        """
        try:
            Path(_body_file_path(kind, reference, review_round)).unlink(missing_ok=True)
        except OSError:
            pass

    def _clear_report_bodies(self, reference: str) -> None:
        """Drop every report body file this card has, the round about to start included.

        The rounds already over are cleared too: the task protocol answers an identical retry from its
        committed event, and a leftover body file is how a retained conversation produces one.
        """
        sample = _body_file_path("report", reference, 0)
        directory, name = os.path.split(sample)
        prefix = name[: -len("0.md")]
        try:
            entries = os.listdir(directory)
        except OSError:
            return
        for entry in entries:
            if not entry.startswith(prefix) or not entry.endswith(".md"):
                continue
            if not entry[len(prefix) : -len(".md")].isdigit():
                continue
            try:
                os.unlink(os.path.join(directory, entry))
            except OSError:
                pass

    def _worker_launch_prompt(self) -> str:
        """Short pointer delivered to the worker head at launch. The full spec lives in TASK.md
        (written next to the workspace root); duplicating it into the launch prompt would ship
        the whole task twice. The head opens TASK.md itself and reports with the command there.
        """
        return (
            "The full task is in TASK.md at the workspace root. Read it first and follow it. "
            "Do not spawn, create, delegate to, or manage subagents; perform the work in this "
            "head only. Report done or blocked with the command given in TASK.md. Do not commit "
            "TASK.md."
        )

    def _broad_check_invocation(self, project: str) -> tuple[str, str]:
        """The exact `check broad`/`check show` commands this project's contract resolves to.

        Until issue:8b39e60e4df361c6138e there was nowhere for a project to say which suite its
        broad check runs, so this packet printed the literal placeholder
        `<this project's broad suite module>` and left the worker to guess. What it guessed, because
        that is what every document said, was bare `python3 -m unittest`: repository-wide discovery,
        every CI suite in one process, ~402s for Ummanu. A packet that names a command a worker
        cannot run without inventing part of it is a packet that teaches the expensive habit.

        The contract is read through the same `projects.contract` rules the preflight and the
        worker's own resolution use. A declared relative interpreter remains an open question until
        the worker holds its workspace; its validated suite can still be named here. Rendering is
        no evidence that the interpreter or imports will be usable there. Two empty strings mean
        no usable suite declaration is available, which the caller reports in words.
        """
        if not project:
            return "", ""
        try:
            verdict = self.catalog.broad_check_verdict(project)
        except (HostError, DispatcherError):
            # Rendering a task document never fails over a registry question. A project whose
            # binding or adapter cannot be read reaches the worker with the honest wording below,
            # and the refusal itself is the preflight's to report, not this packet's.
            return "", ""
        contract = verdict.contract if verdict.fit else None
        if verdict.undecidable and verdict.question == UNDECIDABLE_RELATIVE_INTERPRETER:
            contract = verdict.declared_contract
        if contract is None or not contract.module:
            return "", ""
        broad_arguments = ["check", "broad", "--reuse", "--module", contract.module]
        show_arguments = ["check", "show", "--module", contract.module]
        for argument in contract.args:
            # '=' keeps option-looking suite arguments from being parsed as wrapper options.
            broad_arguments.append(f"--module-arg={argument}")
            show_arguments.append(f"--module-arg={argument}")
        if not contract.interpreter_declared:
            candidate = str(Path(WORKSPACE_ENV_DIR) / "bin" / "python3")
            broad_arguments.extend(("--default-interpreter", candidate))
            show_arguments.extend(("--default-interpreter", candidate))
        return (
            self._control_plane_command(*broad_arguments),
            self._control_plane_command(*show_arguments),
        )

    def _control_plane_command(self, *arguments: str) -> str:
        """Render one head-visible Ummanu command on the production control-plane boundary."""
        interpreter = shlex.quote(str(self.production_runtime.interpreter))
        suffix = "".join(f" {shlex.quote(argument)}" for argument in arguments)
        return f"{_PYTHONPATH_PREFIX} {interpreter} {_PYTHON_SAFE_PATH_FLAG} -m ummanu{suffix}"

    def _local_run_policy(self, task: dict[str, Any]) -> tuple[dict[str, Any] | None, bool]:
        """The same creation-only authority read for packets and ordinary head launches.

        The launch snapshot is sufficient: sprint exceptions are immutable after creation.
        Any malformed declaration invalidates the whole list before project filtering.
        """
        from ummanu.board.local_run import parse_local_run_exceptions, parse_local_run_policy

        reference, project = task.get("sprint"), task.get("project")
        if not reference:
            return None, False
        try:
            policy = {"card": task.get("ref"), "sprint": reference, "project": project, "exceptions": []}
            parse_local_run_policy(policy)
            if self.sprint_reader is None:
                raise ValueError("no sprint reader")
            sprint = self.sprint_reader.show(reference, include_cards=False)
            if not isinstance(sprint, dict) or sprint.get("ref") != reference:
                raise ValueError("sprint identity mismatch")
            projects = sprint.get("reservations", [])
            if not isinstance(projects, list) or any(
                not isinstance(item, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", item) for item in projects
            ):
                raise ValueError("malformed sprint scope")
            if project not in projects:
                raise ValueError("card project is not reserved")
            policy["exceptions"] = [
                entry.to_document()
                for entry in parse_local_run_exceptions(
                    sprint.get("local_run_exceptions", []), projects=projects
                )
                if entry.project == project
            ]
            parse_local_run_policy(policy)
            return policy, False
        except Exception:  # noqa: BLE001 - any failed authority read grants no exceptions
            return None, True

    def local_run_snapshot_for_round(
        self,
        task: dict[str, Any],
        role: str,
        round_number: int,
        retained: dict[str, Any],
    ) -> dict[str, Any]:
        """Capture once for a fresh round; keep the same result on retries and recovery."""
        identity = {
            "card": task.get("ref"),
            "sprint": task.get("sprint") or "",
            "project": task.get("project"),
            "role": role,
            "round": round_number,
        }
        if retained.get("role") == role and retained.get("round") == round_number:
            # A corrupt or changed identity in the current round must not trigger a new read that
            # could grant authority to a guard already bound with a different result.
            return dict(retained)
        policy, unavailable = self._local_run_policy(task)
        return {**identity, "policy": policy, "unavailable": unavailable}

    def _frozen_local_run_policy(
        self,
        task: dict[str, Any],
        role: str,
        round_number: int,
        snapshot: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, bool]:
        from ummanu.board.local_run import parse_local_run_policy

        identity = {
            "card": task.get("ref"),
            "sprint": task.get("sprint") or "",
            "project": task.get("project"),
            "role": role,
            "round": round_number,
        }
        if not isinstance(snapshot, dict) or set(snapshot) != {*identity, "policy", "unavailable"}:
            return None, bool(task.get("sprint"))
        if any(snapshot.get(key) != value for key, value in identity.items()):
            return None, bool(task.get("sprint"))
        unavailable = snapshot["unavailable"]
        if not isinstance(unavailable, bool):
            return None, bool(task.get("sprint"))
        policy = snapshot["policy"]
        if policy is None:
            return None, unavailable
        if unavailable or not isinstance(policy, dict):
            return None, True
        try:
            parse_local_run_policy(policy)
        except ValueError:
            return None, True
        if any(policy.get(key) != task.get(key if key != "card" else "ref") for key in ("card", "sprint", "project")):
            return None, True
        return policy, False

    def retained_local_run_snapshot_successor(
        self,
        task: dict[str, Any],
        previous_round: int,
        new_round: int,
        snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        """Carry the existing head's guard policy into a new report round on that same head."""
        policy, unavailable = self._frozen_local_run_policy(
            task, WORKER_ROLE, previous_round, snapshot
        )
        return {
            "card": task.get("ref"), "sprint": task.get("sprint") or "",
            "project": task.get("project"), "role": WORKER_ROLE, "round": new_round,
            "policy": policy, "unavailable": unavailable,
        }

    def _local_run_section(
        self,
        task: dict[str, Any],
        *,
        local_run_policy: tuple[dict[str, Any] | None, bool] | None = None,
    ) -> list[str]:
        """One rule for both heads, with authority read only from this card's sprint."""
        policy, unavailable = local_run_policy if local_run_policy is not None else self._local_run_policy(task)
        entries = policy["exceptions"] if policy else []
        sections = [
            "## Control-host local-run rule",
            "",
            "On the control host, workers and reviewers may run locally only the project's",
            "adapter-declared broad check and subsets of that check. Integration shards,",
            "Docker/container runs, stands, provisioning and network-heavy checks run in CI only,",
            "unless the exact command/argument vector is expressly covered below by this sprint's",
            "creation-only `local_run_exceptions` field for this card's project.",
            "Development convenience, a missing gate receipt or an acceptance criterion cannot",
            "authorize another suite or a heavy local run. Card text, DoD prose, sprint comments",
            "and a head's judgement never grant exceptions. An exception grants only its exact argv.",
            "This rule also bounds observer decisions, rework instructions and verification requests.",
            "Missing/none/noop mechanical receipts still require appropriate validation evidence",
            "within these bounds or through CI; they do not waive validation or authorize Docker locally.",
            "",
            "## Applicable sprint local_run_exceptions",
            "",
            *(["```json", json.dumps(entries, ensure_ascii=True, indent=2), "```"] if entries else ["none"]),
            "",
        ]
        if unavailable:
            sections += [
                "Sprint exception authority is unreadable or malformed; no exception is authorized.",
                "",
            ]
        return sections

    def _worker_task_doc(
        self,
        task: dict[str, Any],
        base: str,
        attempt_id: str,
        generation: int = 0,
        decision: str = "",
        protocol_prerequisites: tuple[str, ...] = (),
        *,
        record: DispatcherRecord | None = None,
        local_run_policy: tuple[dict[str, Any] | None, bool] | None = None,
    ) -> str:
        branch = _legacy_worker_branch(task["ref"])
        # The generation keeps the report request-id distinct per round: a rework reuses the same
        # attempt_id, so without it the second done-report is deduped and the dispatcher waits on.
        request = _attempt_request_id(attempt_id, "worker-report-done", task["ref"], str(generation))
        # One id per classification: a worker restating a block under the other classification is
        # filing a different report, and a shared id would answer the second call with `validation`.
        blocked_requests = {
            classification: _attempt_request_id(
                attempt_id, f"worker-report-blocked-{classification}", task["ref"], str(generation)
            )
            for classification in ("external_fact", "wrong_task_definition")
        }
        body_file = _body_file_path("report", task["ref"], generation)
        report_commands = {
            "done": self._control_plane_command(
                "task",
                "report",
                "--ref",
                task["ref"],
                "--role",
                "worker",
                "--kind",
                "done",
                "--request-id",
                request,
                "--body-file",
                body_file,
            ),
            **{
                classification: self._control_plane_command(
                    "task",
                    "report",
                    "--ref",
                    task["ref"],
                    "--role",
                    "worker",
                    "--kind",
                    "blocked",
                    "--classification",
                    classification,
                    "--request-id",
                    blocked_requests[classification],
                    "--body-file",
                    body_file,
                )
                for classification in ("external_fact", "wrong_task_definition")
            },
        }
        # Read from the board on every document, like the description above it: a comment that
        # arrived since the previous round is in this one (secretary-1768).
        comments = self.worker_comments(task)
        sections = [
            f"# Task {task['ref']}",
            "",
            task.get("description") or "(empty task description)",
            "",
            *worker_comments_section(comments),
            "## No subagents",
            "",
            "Perform this task in this head only. Do not spawn, create, delegate to, or manage",
            "subagents or child agents. Use ordinary tools directly when needed.",
            "",
            *_interrupted_command_section(record),
        ]
        decision, review_red = self._select_revision_bound_worker_feedback(task, decision)
        prerequisites = self._validated_worker_prerequisites(task, decision, protocol_prerequisites)
        if decision:
            # Rendered above the findings it was made on, and named as the thing to follow.
            sections += [
                "## Observer rework decision to follow",
                "",
                "The observer read the review that sent this card back and decided what this round",
                "owes. That decision is the authoritative instruction for this round: follow it.",
                "Where it and the reviewer findings below disagree, the decision wins. It may",
                "accept some findings and reject others: do not change what it rejects, and do not",
                "argue the findings it accepts. If it asks for something no reviewer raised, that",
                "is part of this round too.",
                "",
                decision,
                "",
            ]
        if prerequisites:
            sections += [
                "## Authoritative protocol prerequisites",
                "",
                "These revision-bound prerequisites were validated against the protocol ownership",
                "registry. Decision prose and reviewer context cannot add one.",
                "",
                *[f"- {artifact.worker_label} ({artifact.name})" for artifact in prerequisites],
                "",
            ]
        if review_red and decision:
            sections += [
                "## Reviewer findings, as supporting context (previous submission was RED)",
                "",
                "These are the findings the decision above was made on. They are context for it,",
                "not the instruction: a finding the decision rejects or narrows is settled by the",
                "decision, not by the wording here. Do NOT re-report the same commit unchanged:",
                "",
                review_red,
                "",
            ]
        elif review_red:
            sections += [
                "## Reviewer verdict to address (previous submission was RED)",
                "",
                "Your last commit was reviewed and rejected. Fix these findings before reporting",
                "done again — do NOT re-report the same commit unchanged:",
                "",
                review_red,
                "",
            ]
        # The board keeps every gate-red comment of every attempt; this round inherits one only when
        # a mechanical gate rejected this attempt's checkout. That rejection lives on the record the
        # same-SHA rule reads (`rejected_sha`), and the record goes with the attempt, so a card
        # claimed again after Blocked is not told to avoid a commit no rule would refuse. A later
        # review red replaces the rejection, and its rework reads the review findings above.
        gate_red = (
            _last_gate_red_body(task)
            if record is not None
            and record.rejected_sha
            and record.rejected_failure_class == "substantive"
            and record.rejected_failure_reason != REVIEW_REJECTION_REASON
            else None
        )
        if gate_red:
            sections += [
                "## Mechanical gate failure to address (CI/local validation was RED)",
                "",
                "The mechanical gate bounced your last submission before it reached review. Fix",
                "the actual cause named below before reporting done again — do NOT re-report the",
                "same commit unchanged:",
                "",
                gate_red,
                "",
            ]
        broad_command, show_command = self._broad_check_invocation(str(task.get("project") or ""))
        sections += self._local_run_section(task, local_run_policy=local_run_policy)
        if broad_command:
            broad_invocation = [f"    {broad_command}", ""]
            show_invocation = f"`{show_command}` and quote its summary"
        else:
            broad_invocation = [
                "Configuration gap: this project's adapter supplies no usable declared broad module.",
                "No exact local broad command can be named. Do not select a module yourself, invent",
                "a placeholder invocation or use repository-wide discovery. Report the configuration",
                "gap and the validation evidence available through CI or the declared exceptions.",
                "Do not claim a broad run or receipt for a suite that cannot be named.",
                "",
            ]
            show_invocation = "When the adapter declares a suite, use its matching `check show` command and quote its summary"
        sections += [
            "## Check-cost contract",
            "",
            "During development, run the smallest relevant checks first. Run at most one local broad",
            "suite for this report generation and unchanged content when it is actually useful; name any",
            "additional broad rerun and its reason in the report. A later executed local/GitHub gate is",
            "reusable downstream only if it produces a valid dispatcher-owned exact-SHA gate receipt. A",
            "none/noop gate or a missing dispatcher-owned exact-SHA gate receipt attests no broad suite;",
            "do not call it authoritative, and run the appropriate validation before reporting when this",
            "card's acceptance criteria require it.",
            "",
            "Run that broad suite through the receipt wrapper, so its worker-local broad receipt outlives",
            "the pane:",
            "",
            *broad_invocation,
            "`--reuse` is the default way to invoke it: with a usable worker-local broad receipt it prints",
            "that worker-local broad receipt",
            "and returns the result the run had, and otherwise it runs the suite. So the answer to a",
            "pane that scrolled away is this same command, not a rerun; asking for a rerun over content the",
            "worker-local broad receipt already covers is prohibited. Drop `--reuse` only to force a fresh run you can",
            "name a reason for.",
            "",
            "It streams the combined output while the suite runs and returns the check's own exit",
            "status, and it writes `.ummanu-task-env/checks/broad-<digest>.json` in this workspace: command and",
            "digest, cwd and imported project, start/end/duration, exit code, parsed verdict and",
            "counts where the runner prints them, and a bounded diagnostic tail. Read it back with",
            show_invocation,
            "in the report. While that worker-local broad receipt is usable, you already have the answer. An edit to the",
            "content, or a concrete red result you are fixing, opens a new justified run — name which",
            "one in the report. Committing content a receipt already covers is not one of them: the",
            "identity is the tree, so a commit that changes no byte reuses the worker-local broad receipt.",
            "The worker-local broad receipt is workspace-local and ignored by git; never commit it.",
            "It is never presented as a dispatcher-owned exact-SHA gate receipt.",
            "",
            "A worker-local broad receipt stands in for a run only while it describes this content and the check",
            "process imported the project from this workspace; an import resolved elsewhere is",
            "recorded truthfully and still refused. `check show` and `--reuse` answer that with",
            "one predicate, so they cannot disagree.",
            "",
            "A check that needs a shell runs as `--command '<shell>'` instead, and buys that",
            "generality by attesting less: a shell may change directory or import environment",
            "before any interpreter starts, so its receipt records no import provenance and is",
            "never reused in place of a run. Prefer `--module` for the suite you report on.",
            "A shell receipt does not authorize a command outside the local-run rule.",
            "",
        ]
        sections += [
            "## Scope of a rework",
            "",
            "Address a reviewer finding when its repair is local to this card. Use `report:blocked`",
            "instead only for an obvious wrong cut: the requested fix contradicts this card, crosses",
            "its explicit Out of scope, or requires a new durable protocol, product contract, or trust",
            "boundary. Difficulty or size alone is not a reason to stop. In a blocked report, name the",
            "conflict and the observer decision needed. Do not silently expand the supported boundary.",
            "",
            "A blocked report has to say which kind of blocker it is, and the two are repaired",
            "differently: `--classification external_fact` when the blocker is a fact outside this",
            "card that somebody has to change first, for example missing access, a broken dependency",
            "or an upstream defect; `--classification wrong_task_definition` when the card itself is",
            "wrong, for example a contradiction, a wrong cut, or scope the card cannot carry. Pick",
            "the one that matches and run that command line below; a blocked report without one is",
            "refused.",
            "",
            "Your checks read the live installation; they never write to it. Do not run a command",
            "that deploys, syncs, provisions or reconciles live state from this workspace: it would",
            "publish unmerged work into the homes the running agents read. Where a check has a",
            "candidate-scoped form, use it (for example `--product-root .`). Where it has none, say",
            "in your report what you could not verify rather than running the live-writing form.",
            "",
            "Do not change or weaken an existing test to make a failure go away. If a test really",
            "does encode behaviour this card changes, say so in the report: name the test, what it",
            "asserted, and why the new contract is the right one. A silently rewritten assertion is",
            "treated as a defect of this round.",
            "",
            "Run every check in the foreground and wait for it there. Do not put work in the",
            "background and do not write a loop that waits for it: you have nothing to wait with.",
            "A background job reports back only at the start of your next turn, so receiving it",
            "means ending this one, while any tool call keeps the turn open — including a no-op",
            "call made to pass the time. A head that spent 23 minutes alternating between `true`",
            "and announcing it would stop polling, then waited on a loop whose condition could",
            "never hold, is why this paragraph exists (secretary-1161, 2026-08-06). Run only the",
            "checks the contract above permits, and run each of them in the foreground.",
            "",
            # In the packet every worker gets: a home file reaches only one model family.
            "Do not add AI co-authorship to your commits. No `Co-Authored-By:` trailer naming",
            "Claude, Codex, an assistant or any model or vendor, and no generated-by attribution",
            "line: the dispatcher checks every commit message on your branch before it publishes",
            "anything, and a violation bounces the card back to you as a red gate. Human",
            "co-authors are fine. If the gate does bounce one, repair the message in this checkout",
            "with `git commit --amend` or `git rebase -i` and report done again; nothing is",
            "rewritten or force-pushed for you.",
            "",
            *(
                [
                    "Before reporting done, stage AND commit everything on the worker branch: run",
                    "`git add -A && git commit`, then confirm `git status --porcelain` prints nothing.",
                    "The dispatcher rejects a done report while the workspace has any uncommitted changes,",
                    "so a partial `git add` that misses your fix files will bounce the card.",
                    "",
                ]
                if has_candidate(task)
                else no_candidate_report_contract(str(task.get("type") or ""))
            ),
            "Report through the ummanu task protocol only:",
            (
                f"This document is report generation {generation}. Every request id below ends in "
                f"-{generation}, and so does the body file. A command carrying any other number belongs"
            ),
            "to a round that is over: that id already names that round's report, and its body file",
            "has been removed. Running it does not report this round. It either fails on the missing",
            "body or answers from the old round's record without writing anything to the card, and",
            "either way this round is left waiting. Copy the command from here, never from an",
            "earlier turn of this conversation.",
            *_body_file_instructions(body_file),
            report_commands["done"],
            report_commands["external_fact"],
            report_commands["wrong_task_definition"],
            "",
            f"Base branch: {base}",
            f"Worker branch: {branch}"
            if has_candidate(task)
            else "Worker branch: none (no branch or PR is expected)",
            "",
            # Last, after anything the card description or decision can write into, so
            # `_task_doc_decision` reads the dispatcher's own record. Written on every document,
            # empty body included: a round with no decision has to read back as none.
            _decision_record_line(generation, decision),
            _protocol_prerequisites_record_line(generation, (artifact.name for artifact in prerequisites)),
            # And the round's own ids, on the same terms: the report commands above are prose in a
            # document that also renders the card description, so they cannot be the authority.
            _round_record_line(generation, [request, *blocked_requests.values()]),
            # Which comments this document carries, so the mid-round arm points the live worker
            # only at the ones it has not been handed.
            worker_comments_record_line(comments),
            "",
        ]
        return "\n".join(sections)

    def worker_comments(self, task: dict[str, Any]) -> tuple[WorkerComment, ...]:
        """The PO, owner and observer comments this card's worker is handed, oldest first.

        The card audit is read only when the card has such a comment at all: the keys come from
        it, and most cards on most ticks have nothing to key.
        """
        if not any(
            isinstance(comment, dict) and comment.get("marker") in WORKER_COMMENT_ROLES
            for comment in task.get("comments") or []
        ):
            return ()
        return select_worker_comments(task, self._card_audit().events(str(task.get("ref") or "")))

    def _card_audit(self) -> Any:
        """The card audit this host was handed, or a refusal that names the missing wiring."""
        if self.audit is None:
            raise HostError("this dispatcher host was built without the card audit its TASK.md reads")
        return self.audit

    def _select_revision_bound_worker_feedback(
        self, task: dict[str, Any], decision: str
    ) -> tuple[str, str | None]:
        """Select only review/decision instructions bound to this description revision.

        The board keeps comments forever, while `TASK.md` must only carry instructions for the
        specification it renders. Missing, malformed, or non-unique bindings intentionally
        produce no historical instruction; the current card description remains the work item.
        """
        events = self._card_audit().events(str(task.get("ref") or ""))
        description = str(task.get("description") or "")
        revision = specification_revision(events, description)
        if not revision:
            return "", None
        digest = hashlib.sha256(description.encode("utf-8")).hexdigest()
        decision = CommandHostRuntime._canonical_decision_binding(decision)
        if decision:
            if not self._bound_marker_body(task, events, "decision:rework", revision, digest, decision):
                return "", None
            review = self._bound_marker_body(task, events, "review:red", revision, digest)
            return decision, review
        return "", self._bound_marker_body(task, events, "review:red", revision, digest)

    def _validated_worker_prerequisites(
        self, task: dict[str, Any], decision: str, expected: tuple[str, ...]
    ) -> tuple[ProtocolArtifact, ...]:
        """Read only the structured declaration bound to the decision rendered for this round."""
        if not decision:
            return ()
        events = self._card_audit().events(str(task.get("ref") or ""))
        description = str(task.get("description") or "")
        revision = specification_revision(events, description)
        digest = hashlib.sha256(description.encode("utf-8")).hexdigest()
        for event in reversed(events):
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

        Audit records retain the body exactly as the observer submitted it, including a final
        newline from a supported reason file.  Worker documents deliberately render the concise
        form, so every revision-bound match must use this same form rather than accidentally
        comparing rendered prose to raw audit content.
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

    def _review_prompt(
        self,
        task: dict[str, Any],
        attempt_id: str,
        review_round: int,
        *,
        record: DispatcherRecord | None = None,
        local_run_policy: tuple[dict[str, Any] | None, bool] | None = None,
    ) -> str:
        # The round belongs in the key like it does in the worker report id: a card that goes red
        # twice in one attempt reuses attempt_id, and a round-less id replays the first verdict.
        green_request = _attempt_request_id(attempt_id, "review-green", task["ref"], str(review_round))
        red_request = _attempt_request_id(attempt_id, "review-red", task["ref"], str(review_round))
        body_file = _body_file_path("verdict", task["ref"], review_round)
        verdict_commands = {
            kind: self._control_plane_command(
                "task",
                "verdict",
                "--ref",
                task["ref"],
                "--role",
                "reviewer",
                "--kind",
                kind,
                "--request-id",
                request,
                "--body-file",
                body_file,
            )
            for kind, request in (("green", green_request), ("red", red_request))
        }
        current_sha = self.head_commit(record) if record else ""
        attestation = _gate_attestation_for_prompt(record, current_sha)
        sections = [
            f"# Review {task['ref']}",
            "",
            task.get("description") or "(empty task description)",
            "",
            *self._local_run_section(task, local_run_policy=local_run_policy),
            "An observed excessive local heavy run is a non-blocking observation, never grounds",
            "for RED, even if its tests passed. Exclude its results from validation evidence; CI",
            "or an allowed local check supplies that evidence. Judge the code and valid evidence.",
            "The observer does not order rework or charge the budget for such a run alone.",
            "Preserve historical verdicts in the audit; do not reopen them under this rule.",
            "Apply the same local-run bounds to every verification you perform; obtain evidence",
            "through CI when those bounds require it. Missing required valid evidence or a code",
            "defect can still block release.",
            "",
            "## No subagents",
            "",
            "Perform this review in this head only. Do not spawn, create, delegate to, or manage",
            "subagents or child agents. Use ordinary tools directly when needed.",
            "",
            *_interrupted_command_section(record),
            "A red verdict must list every blocker you have found in this round. Prefix each with a",
            "stable `BLOCKER-<short-slug>` id so a re-review can close it without rediscovering it.",
            "Do not hold blockers back for a later round and do not widen the scope on the next one.",
            "",
            "For every RED blocker, state the concrete reachable scenario, the violated acceptance",
            "criterion or operational invariant, material assumptions, whether this branch introduced",
            "the defect or it was pre-existing, and whether the repair appears local or would change",
            "architecture, a compatibility promise, a product contract, or a trust boundary. Report",
            "evidence; do not silently widen the supported boundary or decide sprint scope.",
            "",
            *(
                [
                    # Deliberately duplicates the gate's own deterministic preflight: a check that only
                    # ever runs in one place has no second opinion.
                    "Read the commit messages on this branch, not only the diff. AI co-authorship is",
                    "forbidden: a `Co-Authored-By:` trailer naming a model or vendor, or a generated-by",
                    "attribution line, is a RED blocker. Ordinary human co-authors are not. Say what you",
                    "found; do not rewrite history yourself.",
                    "",
                ]
                if has_candidate(task)
                else _no_candidate_review_subject(task)
            ),
            "When a change depends on how an external backend behaves, a passing fixture is not",
            "evidence: it can encode the same wrong assumption as the code under review. Say which",
            "real behaviour you verified and how. If no end-to-end check against the real backend",
            "was possible, write plainly that it was not done and which assumption stays unverified.",
            "",
            "Post exactly one review verdict through the ummanu task protocol:",
            *_body_file_instructions(body_file),
            verdict_commands["green"],
            verdict_commands["red"],
            "",
        ]
        if not has_candidate(task):
            # No candidate: no branch, diff or gate to point at. A re-review keeps only the blockers.
            if record and record.previous_blockers:
                sections[4:4] = [
                    "## Re-review packet",
                    "",
                    "Previous blockers (close or explicitly retain these stable IDs):",
                    _safe_one_line(record.previous_blockers, limit=2000),
                    "",
                ]
            return "\n".join(sections)
        if attestation:
            sections[4:4] = [
                "## Mechanical gate attestation",
                "",
                render_receipt(attestation),
                "",
                "Independently inspect the diff, acceptance criteria and invariants. The attested broad",
                "check above already passed on this exact SHA: do not rerun that broad command or suite on",
                "the same SHA unless you record a concrete `rerun_reason`. A focused reproduction is allowed",
                "for a new blocker, an uncovered external behaviour, or a security/data-loss high-risk need.",
                "Mandatory CI and the exact-SHA pre-merge gate remain machinery-owned and are not waived.",
                "",
            ]
        else:
            sections[4:4] = [
                "## Mechanical gate evidence",
                "",
                "No valid SHA-bound mechanical-gate receipt is available. Independently inspect the diff,",
                "acceptance criteria and invariants; mandatory CI and exact-SHA pre-merge checks remain",
                "machinery-owned. Do not claim that a broad suite was attested. This includes none/noop",
                "gates: run appropriate focused or broad validation when the review needs that evidence.",
                "",
            ]
        if record and record.preferred_head:
            sections[4:4] = [
                "## Head failover",
                "",
                (
                    f"This branch was written by `{_safe_one_line(record.head)}`, not by the head this "
                    f"card asks for (`{_safe_one_line(record.preferred_head)}`): that head's resource "
                    "was red or spent when the card was claimed, so the claim walked the registry's "
                    "fallback chain onto another family."
                ),
                "Review the work on its merits. This is here because who wrote it is a fact you are",
                "entitled to have, not an invitation to grade the head.",
                "",
            ]
        if record and record.previous_reviewed_sha:
            sections[4:4] = [
                "## Re-review packet",
                "",
                f"previous_reviewed_sha: {_safe_one_line(record.previous_reviewed_sha)}",
                f"current_sha: {_safe_one_line(current_sha) or '(unavailable)'}",
                "Changed paths / delta from the prior review:",
                self._review_delta(record, record.previous_reviewed_sha, current_sha),
                "Previous blockers (close or explicitly retain these stable IDs):",
                _safe_one_line(record.previous_blockers, limit=2000)
                or "(legacy verdict had no structured blocker IDs)",
                "Review this delta, the closure of prior blockers and collateral impact; do not restart",
                "from the original base unless a concrete suspicion requires the historical diff.",
                "",
            ]
        return "\n".join(sections)

    def _review_delta(self, record: DispatcherRecord, previous: str, current: str) -> str:
        """A small re-review packet; failure to read it is evidence, never a broad test fallback."""
        if (
            self.mode == "noop"
            or not record.workspace
            or not _is_exact_sha(previous)
            or not _is_exact_sha(current)
        ):
            return "(delta unavailable; inspect only the necessary history)"
        try:
            paths = self.run_capture(
                ["git", "-C", record.workspace, "diff", "--name-only", f"{previous}..{current}"],
                "review delta paths",
            )
            stat = self.run_capture(
                ["git", "-C", record.workspace, "diff", "--stat", f"{previous}..{current}"],
                "review delta stat",
            )
        except (HostError, OSError, subprocess.TimeoutExpired):
            return "(delta unavailable; inspect only the necessary history)"
        if paths.returncode or stat.returncode:
            return "(delta unavailable; inspect only the necessary history)"
        names = (paths.stdout or "").strip()
        summary = (stat.stdout or "").strip()
        return (
            "\n".join(_safe_one_line(part, limit=4000) for part in (names, summary) if part)
            or "(no changed paths)"
        )

    def project_git_access(self, project: str) -> ProjectGitAccess:
        """Bounded, non-mutating proof of a registered project's remote Git access, before a claim.

        Asked of the project's own checkout, the one every card workspace is cut from, through the
        same boundary and resolved Git child every later gate and release operation uses. A
        project with no checkout yet is `not-applicable`: the bring-up already names that failure.
        """
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
        self, project: str, checkout: str | Path, args: list[str], label: str
    ) -> subprocess.CompletedProcess[str]:
        """Run one remote Git operation of a registered project's checkout and return Git's answer.

        The one door the dispatcher's gate and release Git traffic walks through. The transport is
        decided from the checkout's effective remote, after URL rewriting: GitHub HTTPS runs as the
        resolved Git child with ambient helpers disabled and the managed credential selected;
        local/file, SSH and non-HTTPS network URLs keep their explicit non-managed command; any
        other HTTPS host is refused by name. A refused credential raises `ProjectGitAccessError`, a child that could not run or
        timed out raises plain `HostError`, and every other non-zero exit is returned.
        """
        checkout = Path(checkout)
        execution = self._project_remote(project, checkout)
        if execution.transport in {"local", "ssh", "unmanaged"}:
            return self.run_capture(["git", "-C", str(checkout), *args], label)
        return self._managed_remote_git(project, execution, checkout, args, label)

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
        self, project: str, execution: RemoteExecution, checkout: Path, args: list[str], label: str
    ) -> subprocess.CompletedProcess[str]:
        try:
            completed = execution.run_project(checkout, args, label=label)
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
        self, args: list[str], label: str, *, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Like _run but returns the CompletedProcess regardless of exit status (the gate reads a
        non-zero code as a red verdict, not a host failure). Still raises HostError when the process
        can't run at all."""
        try:
            return _run_bounded(args, cwd)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HostError(f"{label} failed: {exc}") from None


# How long one host child (Git, a gate or adapter shell) may run before its group is killed.
HOST_COMMAND_TIMEOUT_SECONDS = 900


def _run_bounded(args: list[str], cwd: Path | None) -> subprocess.CompletedProcess[str]:
    """Run one host child to completion, and on a timeout take its descendants down with it.

    A plain ``subprocess.run`` timeout kills only the direct child: the test processes under a
    ``bash -lc`` gate or adapter setup would keep running. The production tick's unit sets
    ``KillMode=process`` so the local-pty heads it launches outlive it (secretary-1699), which means
    the unit's control-group kill no longer sweeps them either. The child's own process group is
    what bounds them now.
    """
    return _proc.run_isolated(args, cwd=cwd, timeout=HOST_COMMAND_TIMEOUT_SECONDS)


def _gate_attestation_for_prompt(
    record: DispatcherRecord | None,
    current_sha: str = "",
    candidate: dict[str, object] | None = None,
) -> dict[str, object]:
    """Return a complete persisted receipt, never a guessed substitute.

    A record carrying only the old boolean ``gate_state`` is unavailable evidence rather than an
    invented SHA: the exact binding is the safety property.
    """
    if isinstance(candidate, dict):
        return _accepted_gate_receipt(candidate, current_sha)
    source = getattr(record, "gate_attestation", {})
    receipt = getattr(source, "receipt", None)
    if receipt is not None:
        return receipt.as_dict() if receipt.validated_sha == current_sha else {}
    return _accepted_gate_receipt(source, current_sha)


def _continuation_note(generation: int = 0, decision: str = "") -> str:
    """The discriminating tail of the continuation pointer: what a pointer cannot delegate.

    The generation, because the retained conversation still holds the previous round's report
    command in its own scrollback and a number is what makes a replayed one visibly wrong; and the
    standing of the decision, because a pointer that only names a document leaves the conversation
    to rank that document's sections itself. This is a tail, not a line: `NudgePointer.at_document`
    builds it into the nudge with the document's absolute path and checks the ceiling over both.
    """
    note = f"Generation {generation}: use its report command, not an earlier turn's."
    if decision.strip():
        note += " Its observer decision outranks the findings below it."
    return note


def _delivery_evidence_json(carrier: Any, subject: str) -> dict[str, Any]:
    """The bounded evidence a delivery verdict or failure carries, ready to persist."""
    evidence = getattr(carrier, "evidence", None)
    if hasattr(evidence, "to_json"):
        evidence = evidence.to_json()
    if not isinstance(evidence, dict) or not evidence:
        if isinstance(carrier, BaseException):
            return {"subject": subject, "reason": scrub_host_output(str(carrier))[:400]}
        return {}
    evidence = dict(evidence)
    evidence["subject"] = subject
    return evidence


def _record_worker_delivery_evidence(
    record: DispatcherRecord, carrier: Any, *, failure: bool = False
) -> None:
    """Persist a worker prompt receipt before recovery can replace its conversation."""
    evidence = (
        dict(carrier)
        if isinstance(carrier, dict) and carrier
        else _delivery_evidence_json(carrier, "worker-prompt")
    )
    if not evidence:
        return
    record.worker_delivery_evidence = evidence
    typed = record.worker_delivery_evidence.evidence
    if failure and typed is not None and _delivery_readiness_state(typed) != READINESS_BUSY:
        record.worker_delivery_failures += 1


def _report_nudge_prompt(generation: int, reference: str) -> str:
    """What a worker that stopped working without reporting is told, once per round.

    It opens no round and carries no instruction about the work. The generation is spelled out
    because the conversation's own scrollback holds earlier rounds' commands. The last sentence is
    the point: a commit, a push or a green test run is not a report.
    """
    card = f" for {reference}" if reference else ""
    return (
        f"The dispatcher is still waiting for the worker report of generation {generation}{card}, "
        "and this head is sitting at its prompt with nothing delivered for that round. If the work "
        "is done, report it now with the report command in TASK.md at the workspace root: its "
        f"--request-id and its body file both end in {generation}. If it is not done, carry on — "
        "this changes nothing about the task, the round or what the round owes. Committing, "
        "pushing, a green test run or an earlier round's report command is not a report of this "
        "round: the card moves only when that command runs."
    )


def _watchdog_kind(role: str) -> str:
    """`_launch`'s `role` ("worker"/"reviewer") to the `kind` the wait watchdog and
    `command_terminal_status` key their pid-heartbeat file on ("worker"/"review")."""
    return "review" if role == "reviewer" else "worker"


def _body_file_path(kind: str, reference: str, review_round: int) -> str:
    """Where a head writes its report/verdict body. Outside the workspace on purpose: a stray file in
    the worktree makes `git status` dirty, and the done-report check rejects that. The round is in
    the name because heads are told to leave the file behind: without it round 2 starts on top of
    round 1's body and a head that skips the write posts a stale verdict.
    """
    root = os.environ.get("UMMANU_DISPATCHER_BODY_DIR", "/tmp").rstrip("/") or "/tmp"
    return f"{root}/ummanu-{kind}-{_request_token(reference)}-{_request_token(str(review_round))}.md"


def _no_candidate_review_subject(task: dict[str, Any]) -> list[str]:
    """What a reviewer of a research/infra card reviews, in place of a branch and its commits."""
    if str(task.get("type") or "") == "research":
        return [
            "This research card has no candidate: there is no branch, diff or pull request to review.",
            f"Review the report directory: `{RESEARCH_REPORT_DIR}/` in the worker's workspace (the",
            f"report is `{RESEARCH_REPORT_DIR}/{RESEARCH_REPORT_FILE}`, with its artifacts beside it). Once",
            f"transferred it lives in the instance repository at `{research_report_path(str(task['ref']))}`.",
            "",
        ]
    return [
        "This infra card has no candidate: there is no branch, diff or pull request to review.",
        "Review the worker's `report:done` body on the card: its `## What was done` section and its",
        "`## How to verify` section, a command or observation you can repeat.",
        "",
    ]


def _body_file_instructions(body_file: str) -> list[str]:
    """Spell out the delivery path: file first with a normal editing tool, then one plain command.
    The codex runtime refuses an inline mktemp/rm assembly.
    """
    return [
        f"Write the body to {body_file} with your file-writing tool,",
        "then run the command below verbatim. Do not assemble the body inside the shell command",
        "(no heredoc, no mktemp, no echo pipeline) and do not add `rm`: the codex runtime refuses",
        "rm-style commands, and quotes or backticks in the body break the call. Leave the file in",
        "place afterwards; the dispatcher does not read it.",
    ]
