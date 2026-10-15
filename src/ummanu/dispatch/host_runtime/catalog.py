"""Production host catalog implementation."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from ummanu.config import validate_instance
from ummanu.dispatch.e2e import parse_e2e
from ummanu.dispatch.launch import CAUSE_BASE_BRANCH_CONTRACT, CAUSE_PROJECT_NOT_ENABLED
from ummanu.dispatch.launcher import (
    HeadLaunchError,
    claude_launch_model as _claude_launch_model,
    ensure_claude_workspace_ready as _ensure_claude_workspace_ready,
    ensure_codex_workspace_trusted as _ensure_codex_workspace_trusted,
    role_launch_env as _role_launch_env,
)
from ummanu.dispatch.observer import OBSERVER_ROLE
from ummanu.dispatch.types import DispatcherError, HostError
from ummanu.head_registry import HeadRegistryConfigError, installed_heads
from ummanu.kernel.capacity_policy import code_concurrency, head_capacity
from ummanu.projects.availability import ProjectAvailability
from ummanu.projects.contract import ContractVerdict, decide as _decide_broad_check_contract
from ummanu.projects.integration_base import IntegrationBaseError, resolve_integration_base, seed_ref_refusal
from ummanu.routing_journal import (
    HEAD_FROM_CARD,
    HEAD_FROM_FALLBACK,
    HEAD_FROM_RECORD,
    HEAD_FROM_ROLE_DEFAULT,
    HeadRun,
    head_run_from_profile,
)
from ummanu.runtime.head import HeadCommand, HeadCommandError, render_head_command as _render_head_command
from ummanu.runtime.heads import (
    HeadRegistryError,
    required_role_default as _required_role_default,
    resolve_head_id as _resolve_head_id,
)


def _same_repo(first: Path, second: Path) -> bool:
    try:
        return first.expanduser().resolve() == second.expanduser().resolve()
    except OSError:
        return first.expanduser().absolute() == second.expanduser().absolute()

def live_root_project_refusal(catalog: Any, project: str) -> str:
    """Why `project` may not run a card because its repository is the live root, or "" when it may."""
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
                raise HostError(
                    f"project {project!r} is registered but not enabled for workloads",
                    bring_up_cause=CAUSE_PROJECT_NOT_ENABLED,
                )
            raise HostError(
                f"project {project!r} is not registered in the instance and is not enabled for workloads",
                bring_up_cause=CAUSE_PROJECT_NOT_ENABLED,
            )
        repo = binding.get("repo")
        if not isinstance(repo, str) or not repo:
            raise HostError(f"project {project!r} has no repo path")
        return binding

    def head_capacity(self) -> int:
        """How many cards with a head the installation runs at once (`head_capacity`)."""
        return head_capacity(self.instance)

    def code_concurrency(self, project: str) -> int:
        """How many code cards of `project` may be active at once (`policy.code_concurrency`).

        A project with no registered binding declares no policy and has the default; the claim's
        contract preflight refuses its card anyway.
        """
        return code_concurrency(self.registered_bindings.get(project) or {})

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

        Use projects.contract as the single authority; unknown contracts are not approvals.
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

        Apply one integration base to PR, receipt, history and merge.
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
        """The git ref this card's checkout is cut from."""
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
        """The profile in this snapshot that serves a head id somebody else wrote down: that id."""
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

        Retain claim-time head identity; never substitute today's default.
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
        """Pre-answer the first-run questions a head's CLI would otherwise put to an operator."""
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
