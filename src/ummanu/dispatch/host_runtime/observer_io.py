"""Production host observer io implementation."""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

from ummanu.dispatch.cleanup import lifecycle
from ummanu.dispatch.git_workspace import WORKSPACES_DIR as GIT_WORKSPACES_DIR, _resolved as _resolved_path
from ummanu.dispatch.heartbeat import sprint_task
from ummanu.dispatch.helpers import _tail
from ummanu.dispatch.host_runtime.contracts import (
    _delivery_evidence_json,
    _durable_head_run,
    _launch_command,
    _runtime_writes_launch_identity,
)
from ummanu.dispatch.observer import (
    OBSERVER_PROMPT_FILE,
    OBSERVER_ROLE,
    ObserverLaunchAborted,
    observer_launch_prompt as _observer_launch_prompt,
    observer_pid_file as _observer_pid_file,
    render_observer_wake_context as _render_observer_wake_context,
)
from ummanu.dispatch.provider_failure import (
    provider_failure_for_persisted_run as _provider_failure_for_persisted_run,
)
from ummanu.dispatch.state import request_token as _request_token
from ummanu.dispatch.tui import (
    DELIVERY_ACCEPTED,
    READINESS_BUSY,
    READINESS_READY,
    TuiDeliveryError,
    bind_claude_provider_progress_source as _bind_claude_provider_progress_source,
    delivery_readiness_state as _delivery_readiness_state,
    provider_progress_for_run as _provider_progress_for_run,
)
from ummanu.dispatch.types import STOPPED_BY_DISPATCHER, HostError, LegacyDispatcherRecord
from ummanu.dispatch.watchdog import (
    bind_head_heartbeat as _bind_head_heartbeat,
    clear_head_heartbeat as _clear_head_heartbeat,
)
from ummanu.infra import git_worktree
from ummanu.memory import access as memory_access
from ummanu.observer_root import OBSERVER_REPO_NAME, observer_root_repo
from ummanu.routing_journal import MODEL_UNKNOWN, HeadRun
from ummanu.runtime import head as head_ops
from ummanu.runtime.codex_preflight import CodexFanoutPolicyError
from ummanu.runtime.head import (
    OBSERVE_PANE_DISCONNECTED as _OBSERVE_PANE_DISCONNECTED,
    OBSERVE_READINESS_UNKNOWN as _OBSERVE_READINESS_UNKNOWN,
    HeadSpec,
)
from ummanu.runtime.head_runtime_backends import is_legacy_record
from ummanu.runtime.prompt_document import PromptDocumentError

OBSERVER_WORKSPACE_DIR = OBSERVER_REPO_NAME

OBSERVER_REPO_BRANCH = "observers"


class ObserverHostMixin:

    def observer_workspace(self, reference: str) -> str:
        """Where one sprint observer runs.

        Use the dedicated managed observer worktree, never the card checkout.
        """
        token = _request_token(reference)
        if self.mode == "noop":
            return str(self.data_dir / "dispatcher" / OBSERVER_WORKSPACE_DIR / token)
        return str(self._git_observer_root / token)

    @property
    def _git_observer_root(self) -> Path:
        return Path(self.data_dir) / GIT_WORKSPACES_DIR / OBSERVER_WORKSPACE_DIR

    def _is_git_observer_workspace(self, workspace: str) -> bool:
        """Whether an observer workspace is one this host made, read from its recorded path alone."""
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
            "worktree" in row and _resolved_path(Path(row["worktree"])) == target
            for row in git_worktree.parse_list(listed.stdout or "")
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

    @lifecycle
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
        """Bring one observer up in the dedicated observer repository."""
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
            **({"launch_task": {**sprint, "kind": "observer"}} if self.cleanup_owner is not None else {}),
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

    def stop_observer(self, record: Any) -> None:
        """End one observer head and give back what its bring-up took."""
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
        """Raise `LegacyDispatcherRecord` when this observer record was written on Orca."""
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

        Use the head's own activity epoch, not the runtime counter.
        """
        if self.mode == "noop":
            return 0
        observer_run = self._observer_lifecycle_run(record)
        runtime = self.head_runtime_for(observer_run)
        durable = getattr(runtime, "activity_epoch", None)
        if callable(durable):
            return int(durable(observer_run))
        return runtime.activity.epoch(observer_run.run_id)

    def stop_observer_if_quiescent(
        self, record: Any, expected_activity_epoch: int, head_process_alive: bool
    ) -> bool:
        """End this observer only while it is still quiet. False means it was not, and nothing ran."""
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
        """This observer as the head runtime addresses it: its own run, at the pane it is in."""
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

    def observer_provider_failure(self, record: Any) -> dict[str, Any]:
        """Whether this observer's exact HeadRun ended its last turn on a provider error (ummanu-108).

        Read provider failure only for the attested HeadRun.
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
