from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sys
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

from tests.fakes.tasks import CardSeed
from tests.head_registry import write_installed_pair
from ummanu.checkpoint import PUSH_INTERVAL_SECONDS, CheckpointResult, is_push_due
from ummanu.dispatch.gate import GateResult
from ummanu.dispatch.heartbeat import run_heartbeat_identity
from ummanu.dispatch.host import (
    CommandHostRuntime,
    LaunchedHead,
    _continuation_note,
    _report_nudge_prompt,
)
from ummanu.dispatch.launch import CAUSE_BASE_BRANCH_CONTRACT
from ummanu.dispatch.launcher import claude_launch_model, role_launch_env
from ummanu.dispatch.types import (
    STOPPED_BY_DISPATCHER,
    STOPPED_BY_REVIEW_FREEZE,
    HeadLaunchAborted,
    HostError,
    ReviewLaunch,
)
from ummanu.dispatch.watchdog import head_run_process_status as _head_run_process_status, pid_file_path
from ummanu.dispatch.worker_comments import worker_comments_note
from ummanu.dispatch.worker_lifecycle import head_run_binding
from ummanu.projects.availability import ProjectAvailability
from ummanu.projects.contract import (
    ContractVerdict,
    ModuleContract,
)
from ummanu.projects.integration_base import (
    IntegrationBaseError,
    resolve_integration_base,
    seed_ref_refusal,
)
from ummanu.routing_journal import HeadRun, head_run_from_profile
from ummanu.runtime.head import operations as head_ops
from ummanu.runtime.head_runtimes import LOCAL_PTY_RUNTIME
from ummanu.tasks import TaskError

#: The `command_terminal_status` answers that carry a pid heartbeat and no provider channel at
#: all, because no readable connected pane was matched to probe one from: the two live shapes
#: (`pid`, `disconnected`) and the two not-live ones (`missing-terminal`, `process-exited`).
_PROVIDER_LESS_STATUS_REASONS = frozenset({"pid", "disconnected", "missing-terminal", "process-exited"})


def _legacy_unbound_v1_run(run_json: dict[str, Any], *, root: Path) -> dict[str, Any]:
    """Give a production-shaped Codex HeadRun its exact, still-unbound v1 descriptor."""
    run = head_ops.HeadRun.from_json(run_json)
    # CommandHostRuntime preflights Codex with the profile's resolved model before it writes this
    # source.  The fixture's generic head run omits a model, which cannot be a clean fan-out
    # attestation, so give this isolated production-shaped source the real profile fact.
    run = replace(
        run,
        role="worker",
        spec=replace(run.spec, model="gpt-5.6-terra"),
    )
    run_id, fingerprint = head_run_binding(run.to_json())
    source = {
        "version": 1,
        "kind": "codex_session_event_jsonl",
        "state": "unbound",
        "run_id": run_id,
        "head_run_fingerprint": fingerprint,
        "workspace": str(Path(run.workspace).resolve(strict=False)),
        "role": run.role,
        "task_ref": run.task_ref.to_json(),
        "root": str(root.resolve(strict=False)),
        "baseline": [],
    }
    return run.with_fanout_policy(
        {
            "version": 1,
            "state": "allowed",
            "terminal_state": "clean",
            "run_id": run.run_id,
            "role": run.role,
            "model": run.spec.model or "",
            "binary_path": "/test/codex",
            "binary_digest": "0" * 64,
            "cli_version": "test-codex",
            "tool_schema_digest": "0" * 64,
            "provider_schema_verdict": "no_callable_child_spawn_surface",
            "events": [],
            "provider_source_required": True,
            "provider_source": source,
        }
    ).to_json()


def _configure_production_shaped_codex_relaunch(host: Any, *, root: Path) -> None:
    """Make the fake's next Codex rework retain the real preflight/launch HeadRun handoff."""

    def preflight(
        head: str,
        *,
        role: str,
        workspace: str,
        task_ref: head_ops.TaskRef,
        pid_file: str,
        run_id: str,
    ) -> head_ops.HeadRun:
        run = head_ops.HeadRun(
            run_id=run_id,
            spec=head_ops.HeadSpec(
                profile_id=head,
                adapter="codex",
                model="gpt-5.6-terra",
                runtime=LOCAL_PTY_RUNTIME,
            ),
            workspace=workspace,
            task_ref=task_ref,
            role=role,
            pid_file=pid_file,
        )
        return head_ops.HeadRun.from_json(
            _legacy_unbound_v1_run(
                run.to_json(),
                root=root / run_id,
            )
        )

    real_restart = host.restart_worker

    def restart(task: dict, record, *, heartbeat_run_id: str = "") -> LaunchedHead:
        launched = real_restart(task, record, heartbeat_run_id=heartbeat_run_id)
        preflight_run = head_ops.HeadRun.from_json(record.launch_intent["head_run"])
        reported = preflight_run.rebound(launched.handle, leaf=launched.leaf).working()
        host._write_head_pid("worker", task["ref"], head_run=reported.to_json(), leaf=launched.leaf)
        return replace(launched, head_run=reported.to_json())

    host.preflight_codex_run = preflight
    host.restart_worker = restart


def dispatcher_seed() -> CardSeed:
    """The dispatcher's board: the pilot card and a Ready neighbor it must not claim."""
    tasks = [
        {
            "id": 12,
            "reference": "ummanu-510",
            "title": "Pilot",
            "description": "pilot spec",
            "column_id": 2,
            "position": 1,
            "swimlane_id": 4,
            "date_creation": 1720000000,
            "date_modification": 1720000000,
        },
        {
            "id": 13,
            "reference": "ummanu-511",
            "title": "Neighbor",
            "description": "do not claim",
            "column_id": 2,
            "position": 2,
            "swimlane_id": 4,
            "date_creation": 1720000000,
            "date_modification": 1720000000,
        },
    ]
    metadata = {
        12: {"project": "ummanu", "task_type": "code", "slug": "pilot"},
        13: {"project": "ummanu", "task_type": "code", "slug": "neighbor"},
    }
    return CardSeed(tasks, metadata)


# The head snapshot the sprint entity resolves a declared observer against. It is the
# installation's own registry, not the dispatcher's catalog, and a sprint may not be opened on a
# profile that is missing from it.
SPRINT_HEAD_SNAPSHOT = "\n".join(  # noqa: FLY002 - this fixture is deliberately line-oriented
    [
        "resources:",
        "  openai-sub:",
        "    account: openai-subscription",
        "  claude-sub:",
        "    account: claude-subscription",
        "profiles:",
        "  codex-observer:",
        "    adapter: codex",
        "    resource: openai-sub",
        "  claude-observer:",
        "    adapter: claude",
        "    resource: claude-sub",
        "role_defaults:",
        "  new_card: codex-observer",
        "  reviewer: codex-observer",
        "  observer: codex-observer",
        "",
    ]
)


class TwoOpenSprintAdmission:
    """Open the two sprints the pilot setting admits, through `SprintWriter.create` itself.

    A dispatcher fixture reads sprint rows the way production does, so the rows it reads have to
    be rows admission produced: the setting is written before either create, the products, issues
    and project registry the create validates against are seeded, and the pair is disjoint on
    product, reservation and repository.  Each sprint declares its own observer: `observer` is the
    first sprint's and `second_observer` the second's, which defaults to none for the scenarios
    that only need one head.  A scenario that needs a broken declaration corrupts the persisted
    value afterwards, which is the only way a live installation reaches one.

    Mixed into a fixture that owns `self.board` (a card store, `tests.sql_backend_fixtures`) and
    `self.data_dir`.
    """

    FIRST = "sprint:1"
    SECOND = "sprint:2"
    # Two reserved projects each, so either sprint still has a card to claim once its first one
    # is in flight.
    RESERVATIONS: ClassVar = {FIRST: ["ummanu", "fourth"], SECOND: ["other", "third"]}

    def sprint_instance(self) -> Path:
        """The installation directory the sprint entity validates and reads its limit from."""
        return self.data_dir / "registry" / "instance"

    def admit_two_open_sprints(self, *, observer: dict, second_observer: dict | None = None):
        from ummanu.sprint_observer import none_choice
        from ummanu.sprints import (
            SprintReader,
            SprintWriter,
            instance_open_sprint_limit,
        )

        instance = self.sprint_instance()
        (instance / "projects").mkdir(parents=True, exist_ok=True)
        for project in ("ummanu", "other", "third", "fourth"):
            (instance / "projects" / f"{project}.yaml").write_text(
                f"id: {project}\n",
                encoding="utf-8",
            )
        # The setting is in force before either create runs: it is what the second one is
        # admitted by, and admission reads it live. The data directory it names is where the
        # installed head pair sits.
        (instance / "instance.yaml").write_text(
            "version: 1\nname: two-sprints\n"
            f"data_dir: {self.data_dir}\n"
            "offsite:\n  instance_remote: git@example.invalid:x/y.git\n"
            "open_sprint_limit: 2\n",
            encoding="utf-8",
        )
        write_installed_pair(instance, SPRINT_HEAD_SNAPSHOT)
        self.assertEqual(instance_open_sprint_limit(instance), 2)
        self.board.add_record(
            "product:ummanu",
            "Ummanu",
            {
                "record_type": "product",
                "product_id": "ummanu",
                "product_projects": json.dumps(["ummanu", "fourth"]),
            },
        )
        self.board.add_record(
            "product:other",
            "Other",
            {
                "record_type": "product",
                "product_id": "other",
                "product_projects": json.dumps(["other", "third"]),
            },
        )
        self.board.add_record(
            "issue:ummanu",
            "Ummanu issue",
            {
                "record_type": "issue",
                "issue_product": "ummanu",
                "issue_kind": "feature",
                "issue_priority": "P1",
            },
        )
        self.board.add_record(
            "issue:other",
            "Other issue",
            {
                "record_type": "issue",
                "issue_product": "other",
                "issue_kind": "feature",
                "issue_priority": "P1",
            },
        )
        writer = SprintWriter(self.board, data_dir=self.data_dir, instance=instance)
        roots = self.data_dir / "repos"
        for reference, product, issue, request in (
            (self.FIRST, "ummanu", "issue:ummanu", "admit-first-sprint"),
            (self.SECOND, "other", "issue:other", "admit-second-sprint"),
        ):
            writer.create(
                role="po",
                actor="operator",
                goal=f"goal of {reference}",
                definition_of_done="done when the pair is proven",
                reference=reference,
                product=product,
                issues=[issue],
                projects=self.RESERVATIONS[reference],
                repositories=[str(roots / product)],
                observer=observer
                if reference == self.FIRST
                else (second_observer if second_observer is not None else none_choice()),
                request_id=request,
            )
        self.assertEqual(
            sorted(
                sprint["ref"] for sprint in SprintReader(self.board).list(statuses={"open"}, create=False)
            ),
            [self.FIRST, self.SECOND],
        )
        return writer

    def rewrite_observer(self, reference: str, value: str) -> None:
        """Break the persisted declaration of an already-open sprint, as decay does."""
        self.board.save_sprint_metadata(reference, sprint_observer=value)

    def link_pair_cards(self) -> None:
        """One card of each sprint's two reserved projects, all Ready."""
        self.board.save_metadata(12, sprint_ref=self.FIRST)
        self.board.save_metadata(13, project="other", sprint_ref=self.SECOND)
        # `fourth-1` sits ahead of `third-1` in the claim order, so a tick that holds the first
        # sprint back records the skip and the other sprint's claim in the same pass.
        self.add_pair_card(14, "fourth-1", project="fourth", sprint=self.FIRST)
        self.add_pair_card(15, "third-1", project="third", sprint=self.SECOND)

    def add_pair_card(self, task_id: int, reference: str, *, project: str, sprint: str) -> None:
        self.board.add_card(
            task_id,
            reference,
            project=project,
            state="ready",
            description="spec",
            metadata={"task_type": "code", "slug": reference, "sprint_ref": sprint},
        )


class FakeCatalog:
    def __init__(
        self,
        adapter: dict | None = None,
        *,
        default_branch: str = "",
        instance_dir: Path | None = None,
    ) -> None:
        self._adapter = adapter or {}
        self._default_branch = default_branch
        # Checkpoint freshness reads the instance repo; the default is deliberately
        # not a repo, so tests that do not care read back empty git fields.
        self.instance_dir = instance_dir or Path("/nonexistent-instance")
        # A trimmed stand-in for heads.yaml: enough profiles to tell two families apart in the
        # routing journal, including one that pins no model at all.
        self.profiles = {
            "codex": {
                "adapter": "codex",
                "model": "gpt-5.6-terra",
                "effort": "default",
                "resource": "openai-sub",
            },
            "codex-reviewer": {
                "adapter": "codex",
                "model": "gpt-5.6-terra",
                "effort": "extra",
                "resource": "openai-sub",
            },
            "claude-opus": {"adapter": "claude", "model": "opus", "resource": "claude-sub"},
            "claude-default": {"adapter": "claude", "resource": "claude-sub"},
        }
        self.resources = {
            "openai-sub": {"account": "openai-subscription"},
            "claude-sub": {"account": "claude-subscription"},
        }
        self.profiles["codex-observer"] = {
            "adapter": "codex",
            "model": "gpt-5.6-terra",
            "effort": "extra",
            "resource": "openai-sub",
            "codex_mode": "tui",
        }
        # Like the installed registry, every profile names its runtime, and there is one.
        for profile in self.profiles.values():
            profile["runtime"] = "local-pty"
        # Mutable, like the role_defaults block of heads.yaml: an operator can re-point a role
        # while cards are in flight.
        self.role_defaults = {
            "new_card": "codex",
            "reviewer": "codex-reviewer",
            "observer": "codex-observer",
        }
        # None until a test needs one of the other two contract states (secretary-1458), or needs
        # to watch the moment the preflight asks.
        self.broad_check_state: ContractVerdict | None = None
        self.broad_check_probe: Callable[[str], None] | None = None

    def project_default_branch(self, project: str) -> str:
        return str(self.binding(project).get("default_branch") or "main")

    def integration_base(self, project: str, override: str | None) -> str:
        # Same rule as InstanceCatalog: an override is honoured only when the project declares it
        # as an integration target, and refused with the reason on it otherwise.
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

    def workspace_seed(self, project: str, task: dict) -> str:
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

    def broad_check_verdict(self, project: str) -> ContractVerdict:
        """The card's project can be broad-checked, as every card in this suite always could.

        The pilot project here is Ummanu itself, whose adapter declares its own `broad_check` —
        the live case secretary-1458 must keep working, so `fit` is the default answer. A test that
        needs one of the other two named states assigns it to `broad_check_state`; what produces
        each of them from a real adapter is pinned against `InstanceCatalog` and the worker's own
        resolution, not here.

        `broad_check_probe` is called first, so a test can see what the board looked like at the
        moment the question was asked — which is the whole point of asking it before the claim.
        """
        if self.broad_check_probe is not None:
            self.broad_check_probe(project)
        if self.broad_check_state is not None:
            return self.broad_check_state
        return ContractVerdict.as_fit(
            ModuleContract(sys.executable, "ummanu", module="tests.broad"),
            "ummanu",
        )

    def adapter(self, project: str) -> dict:
        return self._adapter

    def worker_head(self, task: dict) -> str:
        # Routing overrides resolve ahead of the role default, as in InstanceCatalog: the resolved
        # head is written to the board at claim and re-resolved on adoption, so a fake that always
        # answers "codex" would hide an override that never propagates.
        return str(task.get("routing", {}).get("head_override") or self.role_defaults["new_card"])

    def review_head(self, task: dict) -> str:
        return str(task.get("routing", {}).get("review_head_override") or self.role_defaults["reviewer"])

    def head_profile(self, head: str) -> dict:
        # The registry entry behind a head, as InstanceCatalog answers it: prompt delivery resolves
        # the adapter through this, and an unknown head is an error rather than an empty profile.
        if head not in self.profiles:
            raise HostError(f"unknown head {head!r}")
        return self.profiles[head]

    def head_fallback(self, head: str) -> list[str]:
        # Same rule as InstanceCatalog: the chain is whatever the registry writes down, and an
        # unknown head is an error rather than an empty chain, so the claim-time walk can tell
        # "this head names no stand-in" from "this head does not exist".
        if head not in self.profiles:
            raise HostError(f"unknown head {head!r}")
        chain = self.profiles[head].get("fallback")
        return [str(entry) for entry in chain] if isinstance(chain, list) else []

    def claimed_worker_head(self, task: dict) -> str:
        # Same rule as InstanceCatalog: the head the claim wrote onto the card wins over whatever
        # the override and the role default say now, and a claimed head that has left the registry
        # stops the bring-up instead of falling back to the current default.
        return self._claimed_head(task, "resolved_worker_head", self.worker_head)

    def claimed_review_head(self, task: dict) -> str:
        return self._claimed_head(task, "resolved_review_head", self.review_head)

    def _claimed_head(self, task: dict, key: str, current) -> str:
        claimed = task.get("routing", {}).get(key)
        if not claimed:
            return current(task)
        head = str(claimed)
        if head not in self.profiles:
            raise HostError(f"head {head!r} recorded at claim is unavailable")
        return head

    def head_run(
        self,
        task: dict,
        *,
        role: str,
        head: str = "",
        workspace: str = "",
        failover: bool = False,
    ) -> HeadRun:
        """Mirror InstanceCatalog.head_run over a four-profile registry: `codex` for the worker,
        `codex-reviewer` for the reviewer, `claude-opus` as the other family and `claude-default` as
        the profile that pins no model. Same rule as the real catalog: the head comes from the
        bring-up, its configuration from the registry as it reads right now, and only the caller's
        own record can say the claim reached this head by walking a chain."""
        routing = task.get("routing") or {}
        if role == "worker":
            override = routing.get("head_override")
            asked = str(override or self.role_defaults["new_card"])
        else:
            override = routing.get("review_head_override")
            asked = str(override or self.role_defaults["reviewer"])
        launched = str(head) if head else asked
        # Same rule as InstanceCatalog: a head the claim reached by walking a chain says so, and
        # anything else that differs from the asked head is the record's older decision.
        source = (
            ("fallback" if failover else "record")
            if launched != asked
            else ("card" if override else "role_default")
        )
        profile = self.profiles.get(launched, {"adapter": "codex", "resource": "openai-sub"})
        model: str | None = None
        model_source = ""
        if str(profile.get("adapter") or "") == "claude":
            # Same as InstanceCatalog: a claude profile that pins no model leaves the choice to the
            # CLI, and the snapshot names the model that CLI resolves at this bring-up.
            model, model_source = claude_launch_model(profile, workspace=workspace, env=role_launch_env(role))
        return head_run_from_profile(
            role=role,
            head=launched,
            head_source=source,
            profile=profile,
            resources=self.resources,
            model=model,
            model_source=model_source,
        )

    def observer_head(self) -> str:
        # Same rule as InstanceCatalog: the observer's own role_defaults key, refused by that key
        # when the registry has none rather than borrowing the worker's default.
        head = self.role_defaults.get("observer")
        if not head:
            raise HostError("head registry has no role_defaults.observer")
        if head not in self.profiles:
            raise HostError(f"unknown head {head!r}")
        return head

    def observer_profile(self, head: str) -> dict:
        # Same rule as InstanceCatalog: one lookup for a head a sprint declared, no fallback. A
        # profile that has left the registry makes the sprint unrunnable, and the fence says so.
        if head not in self.profiles:
            raise HostError(f"unknown head {head!r}")
        return self.profiles[head]

    def observer_run(self, head: str, *, workspace: str = "") -> HeadRun:
        profile = self.profiles.get(head, {"adapter": "codex", "resource": "openai-sub"})
        return head_run_from_profile(
            role="observer",
            head=head,
            head_source="role_default",
            profile=profile,
            resources=self.resources,
        )

    def binding(self, project: str) -> dict:
        # `orca_binding` is required of every enabled binding, so the double carries one too. Here
        # it spells the project the same way; the projects where it does not have their own tests.
        binding = {"repo": f"/home/dev/{project}", "orca_binding": project}
        if self._default_branch:
            binding["default_branch"] = self._default_branch
        return binding

    def project_availability(self, _project: str) -> ProjectAvailability:
        """The fake's declared projects are available unless a test supplies a stricter catalog."""
        return ProjectAvailability()


class FakeHost:
    def __init__(self, root: Path, catalog: FakeCatalog | None = None) -> None:
        self.root = root
        # Exercise gate attestation and head observation through the production policy.
        # The recording verbs below simulate effects; they are not a noop policy host.
        self.mode = "real"
        # The real task-document selector reads the dispatcher's card audit; the fixture that
        # builds the dispatcher hands its writer's audit in (`tests/dispatcher_fixtures.py`).
        self.audit: Any = None
        # The real host snapshots the head at bring-up and hands the record back; the fake goes
        # through the same catalog so the routing journal sees real configurations here too.
        self.catalog = catalog or FakeCatalog()
        self.production_runtime = SimpleNamespace(interpreter=sys.executable)
        # Ordered log of every host call. The per-method lists below answer "did it happen"; this
        # answers "in what order", which some invariants depend on (complete_green must push from
        # the workspace before teardown removes it).
        self.calls: list[str] = []
        self.prepared: list[str] = []
        self.prepare_requires_existing: list[bool] = []
        # Every launch this fake performs gets its own head run identity, numbered in order.
        self.head_runs = 0
        # The production runtime installs this exact-run ingress immediately after a Codex launch
        # intent is durable.  Most dispatcher fixtures use non-source HeadRuns, so the double
        # records the hand-off without inventing a provider journal event.
        self.codex_provider_ingresses: list[str] = []
        self.reviews: list[str] = []
        self.stopped: list[str] = []
        self.torn_down: list[str] = []
        self.completed: list[str] = []
        self.fail_prepare_reason = ""
        # A bring-up failure the caller has to read for more than its message, the worker twin of
        # `fail_observer_error`: a HeadLaunchAborted carrying the pane that stayed up.
        self.fail_prepare_error: Exception | None = None
        self.fail_result_reason = ""
        self.fail_review_error: Exception | None = None
        # Recovery retries a busy reviewer nudge against the launch intent's existing HeadRun.
        # Keep that operation independently scriptable: it is neither another split nor a worker
        # freeze, and tests use the call log to prove the ordering.
        self.fail_review_delivery_retry_error: Exception | None = None
        self.review_delivery_retry_evidence: dict | None = None
        self.review_delivery_retries: list[str] = []
        # A production reviewer can receive its prompt before a later freeze fails.  Tests that
        # exercise that boundary give the fake the same completed metadata-only receipt.
        self.review_launch_delivery_evidence: dict[str, object] = {}
        # Failure hooks for host calls the real runtime can fail on: a rework workspace removed
        # out of band, a merge push the remote rejects, an orca terminal inventory that errors.
        self.fail_restart_reason = ""
        # The relaunch twin of `fail_prepare_error`: a rework or respawn bring-up whose failure the
        # caller has to read for more than its message, e.g. a head pane that was not ready.
        self.fail_restart_error: Exception | None = None
        self.fail_complete_reason = ""
        self.worker_status_result: dict | None = None
        self.review_status_result: dict | None = None
        self.worker_status_error: Exception | None = None
        self.review_status_error: Exception | None = None
        # The provider cursor the fake's bound progress seam answers with. Tests advance
        # it to model a working transcript; the default is one unchanged value, i.e.
        # admitted Quiet between ticks.
        self.provider_cursor = "fake:unchanged"
        # Mechanical gate results consumed FIFO; empty means the default green (ci: none / passing).
        self.gate_results: list[GateResult] = []
        self.gate_calls: list[str] = []
        self.gate_error: Exception | None = None
        self.gate_reruns: list[tuple[str, str]] = []
        self.gate_rerun_error: Exception | None = None
        # Reviewer pane bookkeeping (secretary-651): which handle each review was split off, which
        # reviewer panes were closed on their own, and the commit the checkout reports. `commit` is
        # what start_review pins; reassign it to model a checkout that moved under a green verdict.
        self.split_from: list[str] = []
        self.stopped_reviews: list[str] = []
        self.review_stop_initiators: list[str] = []
        self.commit = "c0ffee1234567890"
        # The retained checkout a headless recovery binds (secretary-1544): bound and clean on the
        # card's own branch by default; a test that models a lost or foreign checkout sets the
        # refusal reason, and one that models a worker stopped mid-edit sets `dirty`.
        self.retained_workspace_reason = ""
        self.retained_workspace_branch = ""
        self.retained_workspace_dirty = False
        # Observer heads (secretary-793): which sprints got one, which handles were stopped, and
        # the pid the fake heartbeat writes. os.getpid() is a live process, so the default launch
        # reads as alive; point it at a free pid to model a head that died.
        self.observers: list[str] = []
        # The sprint binding each bring-up handed the head, in launch order.
        self.observer_identities: list[dict[str, str]] = []
        self.observer_nudges: list[str] = []
        self.observer_wake_contexts: list[tuple[dict, str]] = []
        self.observer_wake_post_merge: list[list[dict]] = []
        self.stopped_observers: list[str] = []
        # workspace -> live terminal handle, the inventory Orca answers `terminal list` from.
        self.observer_terminals: dict[str, str] = {}
        self.observer_pid = os.getpid()
        # Work liveness is separate from the pid.  Tests can make a live TUI report a completed,
        # stale queue without pretending the process has died.
        self.observer_status_result: dict | None = None
        self.fail_observer_reason = ""
        # A bring-up failure the caller has to read for more than its message, e.g. an
        # ObserverLaunchAborted that carries the handle of a terminal that stayed up.
        self.fail_observer_error: Exception | None = None
        # Orca refusing to close an observer pane: the head must be assumed alive afterwards.
        self.fail_stop_observer_reason = ""
        # sprint -> the head runtime's activity epoch for that sprint's observer head.
        self.observer_activity_epochs: dict[str, int] = {}
        # The head turning out not to be quiet after all, which refuses a conditional stop.
        self.observer_not_quiescent = False
        # Every conditional stop this host was asked for: (sprint, expected epoch, process alive).
        self.observer_quiescent_stops: list[tuple[str, int, bool]] = []
        # The pid a worker/reviewer bring-up writes to its heartbeat file, the way the real
        # launcher's `with_pid_heartbeat` wrapper does. Launch-intent recovery reads it, so a fake
        # that never wrote one would make every intent look like a head that never came up. None
        # models a runtime that writes no heartbeat at all.
        self.head_pid: int | None = os.getpid()
        # Stop refusals (secretary-820). A stop the host will not confirm must never be followed by
        # a replacement head, and these are how a test makes one refuse.
        self.fail_stop_workspace_reason = ""
        self.fail_stop_head_reason = ""
        self.stop_initiators: list[tuple[str, str]] = []
        self.fail_stop_review_reason = ""
        self.fail_freeze_worker_reason = ""
        self.fail_retain_worker_reason = ""
        # Most fixture cards use the ordinary exec profile, which has no conversation to resume.
        # Tests that model a retained Codex TUI clear this explicitly.
        self.fail_resume_worker_reason = "retained worker session cannot accept a continuation"
        # The bounded report prompt (secretary-1172). It goes to the same live conversations a
        # continuation does, so `fail_resume_worker_reason` decides addressability for both; this
        # one fails a delivery into a head that *is* addressable, which is the refused/ambiguous
        # send. Every prompt actually delivered is recorded, so a test can prove there was one.
        self.fail_report_prompt_reason = ""
        self.report_prompts: list[str] = []
        # Mid-round comment pointers (secretary-1768): which live workers take one, and every
        # pointer actually delivered, built the way the real host builds it.
        self.fail_worker_comments_reason = ""
        self.worker_comment_prompts: list[str] = []
        self.retained_workers: list[str] = []
        self.resumed_workers: list[str] = []
        # The prompt each wake carried, built the way the real host builds it.
        self.resumed_continuations: list[str] = []
        # A retained session the heartbeat can no longer confirm as suspended: set False to model
        # the head dying while the reviewer judged its checkout.
        self.retained_worker_alive = True
        # A retained session whose process is *provably* gone (`known and not alive`), not merely
        # unconfirmable: set True to model orca having lost the head entirely, where there is
        # nothing left to freeze before the reviewer takes the checkout.
        self.worker_retained_gone = False
        # A dispatcher death in the gap between the round's document reaching disk and the head
        # being woken or launched. Both bring-ups write the document and then, separately, wake or
        # launch, so both can be interrupted there. Fires once and clears itself, so the tick that
        # recovers runs the same path for real.
        self.crash_after_task_doc: BaseException | None = None

    _card_audit = CommandHostRuntime._card_audit
    _select_revision_bound_worker_feedback = CommandHostRuntime._select_revision_bound_worker_feedback
    _validated_worker_prerequisites = CommandHostRuntime._validated_worker_prerequisites
    _bound_marker_body = staticmethod(CommandHostRuntime._bound_marker_body)
    _control_plane_command = CommandHostRuntime._control_plane_command
    _local_run_policy = CommandHostRuntime._local_run_policy
    _local_run_section = CommandHostRuntime._local_run_section
    # The document's comment selector, borrowed like the rest of the builder (secretary-1768).
    worker_comments = CommandHostRuntime.worker_comments

    def _broad_check_invocation(self, project: str) -> tuple[str, str]:
        """Borrowed from the real host, like the document builder that calls it.

        The packet's broad-check command is resolved from the registered project's contract
        (issue:8b39e60e4df361c6138e), so a fake that answered this itself would let the document
        say something the real host never would. `FakeCatalog.broad_check_verdict` is the seam a
        test moves instead.
        """
        return CommandHostRuntime._broad_check_invocation(self, project)  # type: ignore[arg-type]

    def _write_task_doc(
        self,
        task: dict,
        workspace: Path,
        attempt_id: str,
        generation: int,
        decision: str = "",
        protocol_prerequisites: tuple[str, ...] = (),
        record=None,
    ) -> None:
        """Write the TASK.md this bring-up would hand the worker, from the real builder.

        The fake owns no copy of the document: a test that wants to know which report round the
        worker was actually given reads it out of the checkout, the way the worker does.
        """
        workspace.mkdir(parents=True, exist_ok=True)
        # Same order as the real host, and the real code: the round's body files go before the
        # document that names the new one is written.
        CommandHostRuntime._clear_report_bodies(self, task["ref"])  # type: ignore[arg-type]
        document = CommandHostRuntime._worker_task_doc(
            self,  # type: ignore[arg-type]
            task,
            task.get("workspace", {}).get("base_branch") or "main",
            attempt_id,
            generation,
            decision,
            protocol_prerequisites,
            record=record,
        )
        (workspace / "TASK.md").write_text(document, encoding="utf-8")
        if self.crash_after_task_doc is not None:
            crash, self.crash_after_task_doc = self.crash_after_task_doc, None
            raise crash

    def _write_head_pid(
        self,
        kind: str,
        reference: str,
        *,
        head_run: dict | None = None,
        leaf: str = "",
        run_id: str = "",
    ) -> None:
        path = Path(pid_file_path(kind, reference))
        path.parent.mkdir(parents=True, exist_ok=True)
        if self.head_pid is None:
            path.unlink(missing_ok=True)
            return
        identity = run_heartbeat_identity(
            head_run or {"run_id": run_id},
            role=kind,
            task=f"card:{reference}",
            leaf=leaf,
        )
        if self.head_pid > 0 and Path(f"/proc/{self.head_pid}/stat").exists():
            stat = Path(f"/proc/{self.head_pid}/stat").read_text(encoding="utf-8")
            starttime = stat[stat.rfind(")") + 2 :].split()[19]
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
        else:
            # Death is checked before the kernel identity, so a valid-shaped record can model an
            # exited head without depending on a recycled or still-present /proc directory.
            starttime = "0"
            boot_id = "dead-process"
        identity.update(
            {
                "version": 1,
                "pid": self.head_pid,
                "boot_id": boot_id,
                "proc_starttime_ticks": starttime,
            }
        )
        path.write_text(json.dumps(identity), encoding="utf-8")

    def prepare_worker(
        self,
        task: dict,
        worker_id: str,
        head: str,
        *,
        attempt_id: str = "",
        require_existing_workspace: bool = False,
        generation: int = 0,
        failover: bool = False,
        heartbeat_run_id: str = "",
        local_run_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        self.calls.append("prepare_worker")
        self.prepare_requires_existing.append(require_existing_workspace)
        if self.fail_prepare_error is not None:
            if isinstance(self.fail_prepare_error, HeadLaunchAborted):
                # A bring-up that failed with its terminal already open: the head is running, so
                # its heartbeat is there for recovery to find, exactly as after a real launch.
                self._write_head_pid(
                    "worker",
                    task["ref"],
                    run_id=heartbeat_run_id,
                    leaf=self.fail_prepare_error.leaf,
                )
            raise self.fail_prepare_error
        if self.fail_prepare_reason:
            raise HostError(self.fail_prepare_reason)
        workspace = self.root / worker_id
        workspace.mkdir(parents=True, exist_ok=True)
        self._write_task_doc(task, workspace, attempt_id, generation)
        self.prepared.append(task["ref"])
        launched = self._launched(
            f"term:{worker_id}",
            head,
            task,
            "worker",
            workspace=str(workspace),
            failover=failover,
            run_id=heartbeat_run_id,
        )
        self._write_head_pid("worker", task["ref"], head_run=launched.head_run, leaf=launched.leaf)
        return {
            "workspace": str(workspace),
            "handle": launched.handle,
            "leaf": launched.leaf,
            "base_branch": task.get("workspace", {}).get("base_branch") or "main",
            "run": launched.run,
            # The real host always carries this bounded receipt, even when noop mode has no pane
            # and therefore no delivery facts to record yet.
            "delivery_evidence": {},
            # The head's own run, as `spawn` returns it (secretary-1412).
            "head_run": dict(launched.head_run),
        }

    def observer_workspace(self, reference: str) -> str:
        return str(self.root / "observers" / reference.replace(":", "-"))

    def configure_codex_provider_ingress(self, run, *, persist, stop, block) -> None:
        self.codex_provider_ingresses.append(run.run_id)

    def poll_codex_provider_ingress(self, run) -> None:
        return None

    def provider_failure(self, _task, record, kind) -> dict:
        """A scripted first-turn provider failure, bound to one exact run (secretary-1799).

        A test names the run whose session ended on a provider error in `failed_runs` (run id ->
        `ProviderError`, typically parsed by the real reader from a real-shape rollout); every other
        run -- the fallback head launched in its place included -- answers that nothing failed.
        """
        run = record.review_head_run if kind == "review" else record.worker_head_run
        run_id = str((run or {}).get("run_id") or "")
        error = self.__dict__.get("failed_runs", {}).get(run_id)
        if error is None:
            return {"state": "none"}
        return {
            "state": "failed",
            "run_id": run_id,
            "head": record.review_head if kind == "review" else record.head,
            "resource": "",
            "error": error.to_json(),
        }

    def provider_progress(self, _task, record, kind) -> dict[str, str]:
        """A fake provider's opaque cursor is still explicitly bound to its HeadRun.

        The default answer re-reads one unchanged cursor: an admitted Quiet, which is
        what a real rollout produces between ticks when nothing advanced. Tests model
        advancement or darkness by scripting their own evidence.
        """
        run = record.review_head_run if kind == "review" else record.worker_head_run
        run_id, fingerprint = head_run_binding(run)
        if not run_id:
            return {"state": "unavailable", "reason": "fake has no persisted HeadRun"}
        return {
            "state": "observed",
            "admission": "accepted",
            "source": "fake-bound-session",
            "source_fingerprint": "f" * 32,
            "cursor": self.provider_cursor,
            "head_run_id": run_id,
            "head_run_fingerprint": fingerprint,
        }

    def _synthetic_status(self, task: dict, record, kind: str) -> dict | None:
        """Derive the vitality sources for a scripted status answer.

        A test that scripts ``worker_status_result``/``review_status_result`` spells the
        derived booleans the watchdog consumes. The vitality decision additionally needs
        the sources those booleans were derived from, so the fake derives them here the
        way the real ``command_terminal_status`` does: the raw classification is read
        through ``head_run_process_status`` against this record's pid file, and provider
        progress comes from the same bound-cursor seam. Fixtures that spell
        ``pid_status``/``provider_progress`` themselves (the vitality wiring tests)
        pass through verbatim.
        """
        scripted = getattr(self, f"{kind}_status_result")
        if scripted is None:
            return None
        result = dict(scripted)
        if result.get("identity_mismatch"):
            return result
        pid_file = record.worker_pid_file if kind == "worker" else record.review_pid_file
        run = record.worker_head_run if kind == "worker" else record.review_head_run
        leaf = record.worker_leaf if kind == "worker" else record.review_leaf
        if "pid_status" not in result:
            if pid_file:
                result["pid_status"] = dict(
                    _head_run_process_status(
                        pid_file,
                        run=run,
                        role=kind,
                        task=f"card:{task['ref']}",
                        leaf=leaf,
                    )
                )
            else:
                result["pid_status"] = {
                    "known": False,
                    "alive": False,
                    "match": False,
                    "state": "not-yet-written",
                }
        if result.get("live") is False:
            # A scripted not-live answer models a terminal the inventory lost. The
            # classification is settled before the provider channel is decided, because it is
            # what decides it.
            result.setdefault("reason", "missing-terminal")
        # Only the live-pane branch of the real `command_terminal_status` probes the provider at
        # all, so every other shape carries a `pid_status` and NO provider channel: the two live
        # ones -- `pid` (an exact live heartbeat the worktree inventory has no pane for) and
        # `disconnected` -- and the two not-live ones, `missing-terminal` and `process-exited`,
        # which never reach the probe either. The fake used to attach a provider cursor to every
        # scripted shape, so no in-repo fixture could express the provider-less status the
        # reduction really sees, which is why the absent-channel defect went unseen
        # (secretary-1543) and why a wait test could assert a recovery production would not
        # produce (secretary-1544).
        if (
            "provider_progress" not in result
            and str(result.get("reason") or "") not in _PROVIDER_LESS_STATUS_REASONS
        ):
            result["provider_progress"] = dict(self.provider_progress(task, record, kind))
        return result

    def observer_provider_progress(self, record) -> dict[str, str]:
        """The observer twin of the shared exact-HeadRun progress seam."""
        run_id, fingerprint = head_run_binding(record.head_run)
        if not run_id:
            return {"state": "unavailable", "reason": "fake has no persisted observer HeadRun"}
        return {
            "state": "observed",
            "admission": "accepted",
            "source": "fake-bound-session",
            "source_fingerprint": "f" * 32,
            "cursor": "fake:unchanged",
            "head_run_id": run_id,
            "head_run_fingerprint": fingerprint,
        }

    def observer_pid_file(self, reference: str) -> str:
        return str(self.root / "observers" / f"{reference.replace(':', '-')}.pid")

    def prepare_observer(
        self,
        sprint: dict,
        head: str,
        *,
        prompt: str,
        identity: dict[str, str] | None = None,
        heartbeat_run_id: str = "",
        recorded_workspace: str = "",
    ) -> dict:
        self.calls.append("prepare_observer")
        self.observer_identities.append(dict(identity or {}))
        if self.fail_observer_error is not None:
            raise self.fail_observer_error
        if self.fail_observer_reason:
            raise HostError(self.fail_observer_reason)
        reference = str(sprint["ref"])
        workspace = Path(self.observer_workspace(reference))
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "SPRINT.md").write_text(prompt, encoding="utf-8")
        self.observers.append(reference)
        pid_file = Path(self.observer_pid_file(reference))
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        handle = f"observer:{reference}"
        leaf = f"leaf:{handle}"
        head_run = head_ops.HeadRun(
            run_id=heartbeat_run_id or "fake-observer-run",
            spec=head_ops.HeadSpec(profile_id=head, adapter="codex", model="gpt-5.6-terra", runtime=LOCAL_PTY_RUNTIME),
            workspace=str(workspace),
            task_ref=head_ops.TaskRef.sprint(reference),
            role="observer",
            handle=handle,
            leaf=leaf,
            pid_file=str(pid_file),
        ).to_json()
        observer_identity = run_heartbeat_identity(
            head_run,
            role="observer",
            task=f"sprint:{reference}",
            leaf=leaf,
        )
        if self.observer_pid > 0 and Path(f"/proc/{self.observer_pid}/stat").exists():
            stat = Path(f"/proc/{self.observer_pid}/stat").read_text(encoding="utf-8")
            observer_identity.update(
                {
                    "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip(),
                    "proc_starttime_ticks": stat[stat.rfind(")") + 2 :].split()[19],
                }
            )
        else:
            observer_identity.update({"boot_id": "dead-process", "proc_starttime_ticks": "0"})
        observer_identity.update({"version": 1, "pid": self.observer_pid})
        pid_file.write_text(json.dumps(observer_identity), encoding="utf-8")
        # Like Orca: the terminal is findable by its workspace, which is how a head whose handle
        # was lost with its tick still gets stopped.
        self.observer_terminals[str(workspace)] = handle
        return {
            "workspace": str(workspace),
            "handle": handle,
            "leaf": leaf,
            "pid_file": str(pid_file),
            # Like the real host: a bring-up that puts a prompt in front of the head says so, and
            # hands back what the delivery boundary saw doing it.
            "prompt_delivered": True,
            "delivery_evidence": {
                "subject": "observer-launch",
                "handle": handle,
                "stage": "acknowledged",
                "payload_bytes": len(prompt.encode("utf-8")),
            },
            "run": self.catalog.observer_run(head, workspace=str(workspace)).to_json(),
            "head_run": head_run,
        }

    def observer_status(self, _record) -> dict:
        if self.observer_status_result is not None:
            return dict(self.observer_status_result)
        return {"last_activity": time.time(), "idle": False}

    def nudge_observer(
        self, record, *, sprint: dict, change: str = "linked-card", post_merge: list | None = None
    ) -> str:
        self.calls.append("nudge_observer")
        if self.fail_observer_reason:
            raise HostError(self.fail_observer_reason)
        self.observer_nudges.append(str(record.sprint))
        self.observer_wake_contexts.append((sprint, change))
        self.observer_wake_post_merge.append(list(post_merge or []))
        # Like the real host, this confirms terminal acceptance only. The later durable resume
        # closes the observer delivery during normal reconciliation.
        return "accepted"

    def observer_activity_epoch(self, record) -> int:
        """Like the real host: this head's own epoch, which a conditional stop compares against."""
        return int(self.observer_activity_epochs.get(str(record.sprint), 0))

    def stop_observer_if_quiescent(
        self, record, expected_activity_epoch: int, head_process_alive: bool
    ) -> bool:
        """Like the real host: the head runtime refuses the stop unless the head is still quiet.

        The fake keeps the same two refusals the runtime has — a turn still running, or an epoch
        that moved since the caller looked — so a test can make a rotation lose the race without
        needing a second thread. `observer_not_quiescent` stands for the first of those whatever
        this head's process is doing: which of the runtime's reasons produced it is settled against
        the real runtime in `tests/test_local_pty_head_runtime.py`, and what this layer owes is that a refusal
        parks the relaunch. The liveness fact the caller is now required to hand down is recorded so
        that a test can check it travelled and was read where the judgement was made.
        """
        self.calls.append("stop_observer_if_quiescent")
        self.observer_quiescent_stops.append(
            (str(record.sprint), int(expected_activity_epoch), bool(head_process_alive))
        )
        if self.observer_not_quiescent:
            return False
        if int(expected_activity_epoch) != self.observer_activity_epoch(record):
            return False
        self.stop_observer(record)
        return True

    def stop_observer(self, record) -> None:
        self.calls.append("stop_observer")
        if self.fail_stop_observer_reason:
            raise HostError(self.fail_stop_observer_reason)
        handle = record.handle or self.observer_terminals.get(str(record.workspace) or "", "")
        self.observer_terminals.pop(str(record.workspace) or "", None)
        if handle:
            self.stopped_observers.append(handle)

    def pane_leaf(self, workspace: str, handle: str) -> str:
        return f"leaf:{handle}"

    def start_review(self, task: dict, record) -> ReviewLaunch:
        self.calls.append("start_review")
        if self.fail_review_error is not None:
            raise self.fail_review_error
        self.reviews.append(task["ref"])
        # Mirror the real host: the reviewer gets its own pane and the worker head is shut down,
        # pinning the commit the reviewer judges.
        self.split_from.append(record.handle)
        launched = self._launched(
            f"review:{task['ref']}",
            record.review_head,
            task,
            "reviewer",
            record.workspace,
            failover=bool(record.preferred_review_head),
            delivery_evidence=dict(self.review_launch_delivery_evidence),
            run_id=str((record.launch_intent or {}).get("run_id") or ""),
        )
        self._write_head_pid("review", task["ref"], head_run=launched.head_run, leaf=launched.leaf)
        try:
            if record.worker_continuation.retained and self.worker_retained_vanished(record):
                # Mirror the real host: a retained worker whose session is provably gone leaves
                # nothing to freeze, so the reviewer takes the checkout it left rather than the
                # launch aborting forever over a head that will never confirm suspended.
                pass
            elif record.worker_continuation.retained:
                # Mirror the real host: a retained worker is already suspended, so the reviewer
                # judges a checkout nothing is editing without ending that conversation.
                self.confirm_worker_retained(record)
            else:
                self.freeze_worker(record)
        except HostError as exc:
            # The reviewer pane is up and the worker would not go: the real host hands the pane
            # back with the failure rather than reporting a bring-up that left nothing running.
            raise HeadLaunchAborted(
                f"worker freeze failed: {exc}",
                handle=launched.handle,
                leaf=launched.leaf,
                workspace=record.workspace,
                pid_file=pid_file_path("review", task["ref"]),
                evidence=dict(launched.delivery_evidence),
                # The pane is up, so the run of the head in it travels with the failure: the
                # adoption that follows continues that run rather than opening a new identity
                # for a reviewer this launch did start (secretary-1414).
                head_run=dict(launched.head_run),
            ) from None
        return ReviewLaunch(
            handle=launched.handle,
            leaf=launched.leaf,
            commit=self.commit,
            run=launched.run,
            head_run=dict(launched.head_run),
            delivery_evidence=dict(launched.delivery_evidence),
            fallback_reason=str(getattr(self, "review_launch_fallback_reason", "")),
        )

    def nudge_review_delivery(self, task: dict, record, intent: dict) -> dict:
        """Fake the direct document retry on the one reviewer this intent already owns."""
        self.calls.append("nudge_review_delivery")
        self.review_delivery_retries.append(task["ref"])
        if self.fail_review_delivery_retry_error is not None:
            raise self.fail_review_delivery_retry_error
        run = head_ops.HeadRun.from_json(dict(intent["head_run"])).working()
        return {
            "handle": str(intent.get("handle") or run.handle),
            "leaf": str(intent.get("leaf") or run.leaf),
            "head_run": run.to_json(),
            "delivery_evidence": self.review_delivery_retry_evidence
            or {
                "subject": "reviewer-launch",
                "handle": str(intent.get("handle") or run.handle),
                "stage": "acknowledged",
                "turn_confirmed": True,
                "readiness_state": "ready",
            },
        }

    def restart_worker(self, task: dict, record, *, heartbeat_run_id: str = "") -> LaunchedHead:
        self.calls.append("restart_worker")
        if self.fail_restart_error is not None:
            if isinstance(self.fail_restart_error, HeadLaunchAborted):
                # The pane stayed up, so the head's heartbeat is there for recovery to find.
                self._write_head_pid(
                    "worker",
                    task["ref"],
                    run_id=heartbeat_run_id,
                    leaf=self.fail_restart_error.leaf,
                )
            raise self.fail_restart_error
        if self.fail_restart_reason:
            raise HostError(self.fail_restart_reason)
        self._write_task_doc(
            task,
            Path(record.workspace),
            record.attempt_id,
            record.report_generation,
            record.report_decision,
            record.report_protocol_prerequisites,
            record=record,
        )
        self.prepared.append(task["ref"])
        launched = self._launched(
            f"rework:{task['ref']}",
            record.head,
            task,
            "worker",
            workspace=record.workspace,
            failover=bool(record.preferred_head),
            run_id=heartbeat_run_id,
        )
        self._write_head_pid("worker", task["ref"], head_run=launched.head_run, leaf=launched.leaf)
        return launched

    def _launched(
        self,
        handle: str,
        head: str,
        task: dict,
        role: str,
        workspace: str = "",
        failover: bool = False,
        delivery_evidence: dict[str, object] | None = None,
        run_id: str = "",
    ) -> LaunchedHead:
        leaf = f"leaf:{handle}"
        lifecycle = self._head_run(handle, head, task, role, workspace, leaf, run_id=run_id)
        routing = self.catalog.head_run(
            task, role=role, head=head, workspace=workspace, failover=failover
        ).to_json()
        # A fake launch is still a provider mock: give its routing event an opaque, distinct session
        # id so dispatcher tests exercise the same launch-to-journal connection as real adapters.
        routing["session_id"] = f"mock-{routing['adapter']}-session-{lifecycle['run_id']}"
        routing["session_id_reason"] = ""
        return LaunchedHead(
            handle=handle,
            head=head,
            run=routing,
            leaf=leaf,
            delivery_evidence=dict(delivery_evidence or {}),
            # The head's own run, as `spawn` hands it back on the real host (secretary-1412). The
            # fake opens no pane, but it does report an identity: what a bring-up owes the record
            # is that this head can be named afterwards, and a fake that answered `{}` could not
            # show a recovery continuing the same run.
            head_run=lifecycle,
        )

    def _head_run(
        self, handle: str, head: str, task: dict, role: str, workspace: str, leaf: str, *, run_id: str = ""
    ) -> dict:
        self.head_runs += 1
        profile = self.catalog.profiles.get(head, {"adapter": "codex"})
        adapter = str(profile.get("adapter") or "unknown")
        document = str(Path(workspace) / "TASK.md") if role == "worker" and workspace else ""
        prompt_identity: dict[str, str] = {}
        if document:
            prompt_identity = {
                "path": str(Path(document).resolve(strict=False)),
                "version": f"sha256:{hashlib.sha256(Path(document).read_bytes()).hexdigest()}",
            }
        return head_ops.HeadRun(
            run_id=run_id or f"run-{role}-{self.head_runs}",
            spec=head_ops.HeadSpec(profile_id=head, adapter=adapter, runtime=LOCAL_PTY_RUNTIME),
            workspace=workspace or str(self.root / f"{task['ref']}-pilot"),
            task_ref=head_ops.TaskRef.card(task["ref"], document=document),
            handle=handle,
            leaf=leaf,
            pid_file=pid_file_path("review" if role == "reviewer" else "worker", task["ref"]),
            fanout_policy={
                "version": 1,
                "state": "unknown",
                "terminal_state": "unknown",
                "events": [],
                **({"prompt_identity": prompt_identity} if prompt_identity else {}),
            },
        ).to_json()

    def worker_status(self, task: dict, record) -> dict:
        self.calls.append("worker_status")
        if self.worker_status_error is not None:
            raise self.worker_status_error
        synthetic = self._synthetic_status(task, record, "worker")
        if synthetic is not None:
            return synthetic
        # No scripted answer: derive the same live shape the real status seam produces,
        # so the vitality reduction sees this record's true classification and bound
        # provider cursor instead of an evidence-free "live".
        pid_file = record.worker_pid_file
        run = record.worker_head_run
        leaf = record.worker_leaf
        pid_status = (
            dict(
                _head_run_process_status(
                    pid_file,
                    run=run,
                    role="worker",
                    task=f"card:{task['ref']}",
                    leaf=leaf,
                )
            )
            if pid_file
            else {
                "known": False,
                "alive": False,
                "match": False,
                "state": "not-yet-written",
            }
        )
        reason = (
            "live"
            if pid_status.get("alive")
            else ("process-exited" if pid_status.get("state") == "dead" else "live")
        )
        answer = {
            "known": True,
            "live": bool(pid_status.get("alive")) if pid_status.get("known") else True,
            "reason": reason,
            "pid_confirmed": bool(pid_status.get("match") and pid_status.get("alive")),
            "last_activity": time.time(),
            "pid_status": pid_status,
        }
        if reason not in _PROVIDER_LESS_STATUS_REASONS:
            # Same rule as the scripted shapes: only a live connected pane is ever probed.
            answer["provider_progress"] = dict(self.provider_progress(task, record, "worker"))
        return answer

    def review_status(self, task: dict, record) -> dict:
        self.calls.append("review_status")
        if self.review_status_error is not None:
            raise self.review_status_error
        live = task["ref"] in self.reviews
        synthetic = self._synthetic_status(task, record, "review")
        if synthetic is not None:
            return synthetic
        if not live:
            return {"known": True, "live": False, "reason": "missing-terminal"}
        pid_file = record.review_pid_file
        run = record.review_head_run
        leaf = record.review_leaf
        pid_status = (
            dict(
                _head_run_process_status(
                    pid_file,
                    run=run,
                    role="review",
                    task=f"card:{task['ref']}",
                    leaf=leaf,
                )
            )
            if pid_file
            else {
                "known": False,
                "alive": False,
                "match": False,
                "state": "not-yet-written",
            }
        )
        return {
            "known": True,
            "live": bool(pid_status.get("alive")) if pid_status.get("known") else True,
            "reason": "live",
            "pid_confirmed": bool(pid_status.get("match") and pid_status.get("alive")),
            "last_activity": time.time(),
            "pid_status": pid_status,
            "provider_progress": dict(self.provider_progress(task, record, "review")),
        }

    def verify_worker_result(self, task: dict, record) -> None:
        self.calls.append("verify_worker_result")
        if self.fail_result_reason:
            raise HostError(self.fail_result_reason)

    # The project Git access preflight answer, scriptable per test. It is deliberately kept out of
    # `calls`: that log is the ordering of card work, and this read precedes any claim.
    git_access = None
    git_access_probe = None

    def project_git_access(self, project: str):
        from ummanu.infra.github_credential import ProjectGitAccess

        self.__dict__.setdefault("git_access_checks", []).append(project)
        if callable(self.git_access_probe):
            self.git_access_probe(project)
        if isinstance(self.git_access, Exception):
            raise self.git_access
        return self.git_access or ProjectGitAccess("ready", "local", "local")

    def gate_check(self, task: dict, record) -> GateResult:
        self.calls.append("gate_check")
        self.gate_calls.append(task["ref"])
        if self.gate_error is not None:
            raise self.gate_error
        if self.gate_results:
            scripted = self.gate_results.pop(0)
            # A scripted gate answer may be the absence of one: an exception in the queue is
            # raised where the real gate would have raised it.
            if isinstance(scripted, Exception):
                raise scripted
            return scripted
        return GateResult("green", "gate green")

    def rerun_failed_ci(self, task: dict, record, result: GateResult) -> None:
        self.calls.append("rerun_failed_ci")
        if self.gate_rerun_error is not None:
            raise self.gate_rerun_error
        self.gate_reruns.append((task["ref"], result.failed_run_id))

    def restore_workspace(self, task: dict, worker: str) -> str:
        self.calls.append("restore_workspace")
        return str(self.root / worker)

    def retained_workspace_state(self, task: dict, record) -> dict:
        """The four facts the headless recovery is allowed to rest on, scriptable per test.

        Shaped exactly like the production answer: a checkout that cannot be bound comes back
        unbound with a typed reason and no candidate, never repaired.
        """
        self.calls.append("retained_workspace_state")
        workspace = record.workspace or self.restore_workspace(task, record.worker)
        expected_branch = f"pipeline/{task['ref']}"
        if self.retained_workspace_reason:
            return {
                "workspace": workspace,
                "expected_branch": expected_branch,
                "branch": self.retained_workspace_branch,
                "dirty": None,
                "sha": "",
                "bound": False,
                "reason": self.retained_workspace_reason,
                "detail": "",
            }
        return {
            "workspace": workspace,
            "expected_branch": expected_branch,
            "branch": self.retained_workspace_branch or expected_branch,
            "dirty": self.retained_workspace_dirty,
            "sha": self.commit,
            "bound": True,
            "reason": "",
            "detail": "",
        }

    def complete_green(self, task: dict, record) -> None:
        self.calls.append("complete_green")
        if self.fail_complete_reason:
            raise HostError(self.fail_complete_reason)
        self.completed.append(task["ref"])

    def stop(self, record) -> None:
        self.calls.append("stop")
        self.stopped.append(record.worker)
        self._kill_head("worker", record)
        self._kill_head("review", record)

    def stop_workspace(self, record) -> None:
        """The confirmed twin of `stop`: a refusal reaches the caller (secretary-820)."""
        self.calls.append("stop_workspace")
        if self.fail_stop_workspace_reason:
            raise HostError(self.fail_stop_workspace_reason)
        self.stop(record)

    @contextlib.contextmanager
    def committing(self, flush):
        """The real host's durable-commit seam (secretary-1412), lent for the caller's span.

        The fake performs no host I/O, so it never commits mid-operation; it still has to accept
        the loan, because the tick and the freeze hand it out unconditionally and a host that
        could not take it would be a host the production paths cannot use.
        """
        previous = getattr(self, "commit_state", None)
        self.commit_state = flush
        try:
            yield
        finally:
            self.commit_state = previous

    def stop_head(self, record, kind: str, initiator: str = "dispatcher") -> None:
        # The initiator the real host records on the run (secretary-1412). Kept in the call log so
        # a test can say not only that a head was stopped but who this dispatcher said stopped it.
        self.calls.append(f"stop_head:{kind}")
        self.stop_initiators.append((kind, initiator))
        if self.fail_stop_head_reason:
            raise HostError(self.fail_stop_head_reason)
        handle = record.review_handle if kind == "review" else record.handle
        pid_file = record.review_pid_file if kind == "review" else record.worker_pid_file
        leaf = record.review_leaf if kind == "review" else record.worker_leaf
        if not handle and not leaf and not pid_file:
            raise HostError(f"{kind} head has neither a pane handle nor a pid heartbeat")
        self._kill_head(kind, record)

    def freeze_worker(self, record) -> None:
        self.calls.append("freeze_worker")
        if self.fail_freeze_worker_reason:
            raise HostError(self.fail_freeze_worker_reason)
        if record.handle or record.worker_leaf or record.worker_pid_file:
            self.stop_head(record, "worker", STOPPED_BY_REVIEW_FREEZE)

    def retain_worker(self, record) -> None:
        self.calls.append("retain_worker")
        if self.fail_retain_worker_reason:
            raise HostError(self.fail_retain_worker_reason)
        if not record.handle and not record.worker_pid_file:
            raise HostError("worker session is unavailable for retention")
        if not record.handle:
            # Like the real host: a head with no pane is unaddressable, so there is nothing to
            # retain and the caller stops it instead.
            raise HostError("worker session has no addressable pane to retain")
        self.retained_workers.append(record.handle)

    def worker_retained_alive(self, record) -> bool:
        if not record.worker_continuation.retained:
            return False
        return bool(self.retained_worker_alive and (record.handle or record.worker_pid_file))

    def worker_retained_vanished(self, record) -> bool:
        if not record.worker_continuation.retained:
            return False
        return bool(self.worker_retained_gone)

    def confirm_worker_retained(self, record) -> None:
        self.calls.append("confirm_worker_retained")
        # `fail_freeze_worker_reason` is the knob for "the host cannot vouch that this worker is
        # not writing". Suspending it for the reviewer instead of stopping it does not change what
        # a reviewer launch needs to hear before it takes the checkout.
        if self.fail_freeze_worker_reason:
            raise HostError(self.fail_freeze_worker_reason)
        if not self.worker_retained_alive(record):
            raise HostError("retained worker session is no longer confirmably suspended")

    def worker_addressable(self, record) -> bool:
        # The real host asks whether this head is a live provider conversation: a pane handle plus
        # an adapter that has one. The fixture's exec profile has neither, and that is exactly what
        # `fail_resume_worker_reason` models here.
        return bool(record.handle) and not self.fail_resume_worker_reason

    def prompt_worker_report(self, task: dict, record) -> None:
        self.calls.append("prompt_worker_report")
        if self.fail_report_prompt_reason:
            raise HostError(self.fail_report_prompt_reason)
        if not self.worker_addressable(record):
            raise HostError("worker session cannot accept a report prompt")
        if not record.worker_pid_file and not record.handle:
            raise HostError("worker session exited")
        # Unlike a continuation, this writes no document and clears no body file: the round the
        # head is being pointed back at is the one it already has.
        self.report_prompts.append(_report_nudge_prompt(record.report_generation, task["ref"]))

    def worker_takes_comments(self, record) -> bool:
        return self.worker_addressable(record) and bool(record.worker_pid_file or record.handle)

    def deliver_worker_comments(self, task: dict, record) -> None:
        self.calls.append("deliver_worker_comments")
        if self.fail_worker_comments_reason:
            raise HostError(self.fail_worker_comments_reason)
        # Same order as the real host: the round's document, comments included, before the pointer.
        document = CommandHostRuntime._worker_task_doc(
            self,  # type: ignore[arg-type]
            task,
            task.get("workspace", {}).get("base_branch") or "main",
            record.attempt_id,
            record.report_generation,
            record.report_decision,
            record.report_protocol_prerequisites,
            record=record,
        )
        (Path(record.workspace) / "TASK.md").write_text(document, encoding="utf-8")
        self.worker_comment_prompts.append(
            head_ops.NudgePointer.at_document(
                str(Path(record.workspace) / "TASK.md"), worker_comments_note(record.report_generation)
            ).text
        )

    def worker_handoff_started(self, record) -> str:
        """No production handoff runs on this double (it has no `worker_handoffs`): nothing started."""
        return "none"

    def resume_worker(self, task: dict, record, *, handoff_started: str = "") -> None:
        self.calls.append("resume_worker")
        if self.fail_resume_worker_reason:
            raise HostError(self.fail_resume_worker_reason)
        if not record.handle and not record.worker_pid_file:
            raise HostError("retained worker session exited")
        # Same order as the real host: the round's document is on disk before the suspended
        # conversation is woken, and the prompt that wakes it names that same round.
        self._write_task_doc(
            task,
            Path(record.workspace),
            record.attempt_id,
            record.report_generation,
            record.report_decision,
            record.report_protocol_prerequisites,
            record=record,
        )
        self.resumed_continuations.append(
            head_ops.NudgePointer.at_document(
                str(Path(record.workspace) / "TASK.md"),
                _continuation_note(record.report_generation, record.report_decision),
            ).text
        )
        self.resumed_workers.append(record.handle)

    def _kill_head(self, kind: str, record) -> None:
        """Drop the heartbeat of a stopped head, the way a closed pty tree does.

        Without this a stop would leave a pid file that still names this live test process, and
        every later liveness read would answer that the head the test just stopped is running.
        """
        pid_file = record.review_pid_file if kind == "review" else record.worker_pid_file
        if pid_file:
            Path(pid_file).unlink(missing_ok=True)

    def stop_review(self, record, initiator: str = STOPPED_BY_DISPATCHER) -> None:
        self.calls.append("stop_review")
        # Who ended this reviewer, as the runtime named it. The real host writes it onto the
        # record's run; this double only has to prove the caller passed one, which is what the
        # initiator-per-path assertions read.
        self.review_stop_initiators.append(initiator)
        if not record.review_handle and not record.review_leaf and not record.review_pid_file:
            return
        if self.fail_stop_review_reason:
            raise HostError(self.fail_stop_review_reason)
        if record.review_handle:
            self.stopped_reviews.append(record.review_handle)
        self._kill_head("review", record)

    def head_commit(self, record) -> str:
        self.calls.append("head_commit")
        return self.commit

    def reconcile_reviewed_base_move(
        self, task: dict, record, reviewed_commit: str, current_commit: str
    ) -> dict[str, str | int] | None:
        self.calls.append("reconcile_reviewed_base_move")
        return None

    def teardown(self, record) -> None:
        self.calls.append("teardown")
        self.stop(record)
        self.torn_down.append(record.worker)


class FakeCheckpoint:
    def __init__(self, outcome: CheckpointResult | Exception) -> None:
        self.outcome = outcome
        self.calls = 0

    def write(self) -> CheckpointResult:
        self.calls += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class FakePusher:
    def __init__(self, outcome: dict | Exception) -> None:
        self.outcome = outcome
        self.calls: list[dict] = []

    def due(self, state: dict | None = None, *, now: float | None = None) -> bool:
        """The same public window predicate the production pusher exposes."""
        return is_push_due(
            dict(state or {}),
            float(now or 0),
            interval_seconds=PUSH_INTERVAL_SECONDS,
        )

    def push(self, state: dict | None = None, *, now: float | None = None) -> dict:
        self.calls.append(dict(state or {}))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return {**(state or {}), **self.outcome}


class FakeSprints:
    """The sprint facts the card cycle asks about, and nothing else.

    `show` answers what a card's sprint declares, which is what decides whether a verdict parks.
    `list` stays empty on purpose: the observer *head* lifecycle is reconciled from it, and these
    tests are about the cards, not about the head that watches them.
    """

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    def list(self, *args, **kwargs) -> list[dict]:
        return []

    def show(self, reference: str, **kwargs) -> dict:
        if reference not in self.rows:
            raise TaskError("not_found", f"no sprint {reference}", 3)
        return self.rows[reference]


__all__ = [
    "FakeCatalog",
    "FakeCheckpoint",
    "FakeHost",
    "FakePusher",
    "FakeSprints",
    "TwoOpenSprintAdmission",
    "_configure_production_shaped_codex_relaunch",
    "_legacy_unbound_v1_run",
    "dispatcher_seed",
]
