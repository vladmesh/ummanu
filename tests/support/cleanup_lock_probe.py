"""The dispatcher's long operations run through their work with the global cleanup lock observed.

`<data>/dispatcher/cleanup.lock` serializes short journal and ownership mutations only. A long
operation that held it over its host work (workspace preparation, setup, launch, a board pass) would
stall every ownership publication of the installation behind one card. Each operation here runs to
its end on a real host or tick with every slow collaborator replaced by a recorder that first tries
the real flock (`cleanup_lock_free`): a lock taken anywhere before one of them, by a decorator or a
`with` in the body, is seen held there. `LockProbes.names` proves the operation reached each one.
"""

from __future__ import annotations

import contextlib
import fcntl
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from tests.fakes.dispatcher import FakeCatalog
from tests.support.call_log import CallLog
from ummanu.dispatch import attempt_accounting, production
from ummanu.dispatch.host import CommandHostRuntime, LaunchedHead
from ummanu.dispatch.state import DispatcherRecord


def cleanup_lock_free(data_dir: Path) -> bool:
    """Whether another open file description can take the cleanup flock right now."""
    path = Path(data_dir) / "dispatcher" / "cleanup.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(handle, fcntl.LOCK_UN)
        return True


class LockProbes(CallLog):
    """A call log whose every recorder first reads the cleanup flock; `held` names where it was taken."""

    def __init__(self, data_dir: Path) -> None:
        super().__init__()
        self.data_dir = data_dir
        self.held: list[str] = []
        self.patched: list[str] = []

    def recorder(self, name: str, result: Any = None, *, wraps: Callable[..., Any] | None = None) -> Callable[..., Any]:
        inner = super().recorder(name, result, wraps=wraps)

        def record(*args: Any, **kwargs: Any) -> Any:
            if not cleanup_lock_free(self.data_dir):
                self.held.append(name)
            return inner(*args, **kwargs)

        return record

    def patch(self, owner: Any, attribute: str, result: Any = None, *, name: str = "", wraps: bool = False) -> Any:
        self.patched.append(name or attribute)
        return super().patch(owner, attribute, result, name=name, wraps=wraps)

    def unreached(self) -> set[str]:
        return set(self.patched) - set(self.names)


def _record(workspace: Path) -> DispatcherRecord:
    record = DispatcherRecord(
        worker="sample-1-worker",
        workspace=str(workspace),
        handle="",
        head="codex",
        review_head="claude",
        attempt_id="attempt-1",
        comment_baseline=0,
        review_baseline=0,
        state="claimed",
        claimed_at=1.0,
    )
    record.worker_head_run = {"task_ref": {"kind": "card", "ref": "sample-1"}}
    return record


def run_host_operations(data_dir: Path) -> dict[str, tuple[Any, LockProbes]]:
    """prepare_worker (resumed and fresh), restart_worker, start_review and teardown, to their end."""
    workspace = data_dir / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    task = {"ref": "sample-1", "project": "sample", "workspace": {"base_branch": "main"}}
    launched = LaunchedHead(handle="pane")
    launch = {
        "catalog.integration_base": "main",
        "_clear_report_bodies": None,
        "_frozen_local_run_policy": None,
        "_worker_task_doc": "document",
        "_write_prompt": None,
        "_worker_launch_prompt": "pointer",
        "_launch": launched,
    }
    prepare = {
        "_require_production_runtime": None,
        "_require_cleanup_admission": None,
        "_require_project_available": None,
        "_prepare_workspace_environment": None,
        "_run_setup": None,
        "_require_workspace_environment": None,
        "_install_workspace_test_guards": None,
        "catalog.workspace_seed": "main",
        "local_run_snapshot_for_round": {},
        **launch,
    }
    operations: dict[str, tuple[dict[str, Any], Callable[[CommandHostRuntime], Any]]] = {
        "prepare_worker": (
            {**prepare, "restore_workspace": str(workspace), "_validate_resumable_workspace": None,
             "_workspace_environment_ready": False},
            lambda host: host.prepare_worker(task, "sample-1-worker", "codex"),
        ),
        "prepare_worker (fresh workspace)": (
            {**prepare, "restore_workspace": str(data_dir / "fresh")},
            lambda host: host.prepare_worker(task, "sample-1-worker", "codex"),
        ),
        "restart_worker": (
            {"_require_cleanup_admission": None, "_require_project_available": None,
             "_refuse_legacy_record": None, "_prepare_workspace_environment": None,
             "_require_workspace_environment": None, **launch},
            lambda host: host.restart_worker(task, _record(workspace)),
        ),
        "start_review": (
            {"_require_project_available": None, "_refuse_legacy_record": None, "_require_worker_admission": None,
             "_prepare_workspace_environment": None, "_require_workspace_environment": None,
             "_clear_body_file": None, "_frozen_local_run_policy": None,
             "_review_document": (workspace / "REVIEW.md", "pointer"), "head_handoffs": False,
             "_launch": launched, "_fence_worker_for_reviewer": None, "head_commit": "abc123"},
            lambda host: host.start_review(task, _record(workspace)),
        ),
        "teardown": (
            {"_refuse_legacy_record": None, "_require_production_runtime": None},
            lambda host: host.teardown(_record(workspace)),
        ),
    }
    results: dict[str, tuple[Any, LockProbes]] = {}
    for name, (collaborators, run) in operations.items():
        probes = LockProbes(data_dir)
        host = CommandHostRuntime(FakeCatalog(), data_dir, mode="real")  # type: ignore[arg-type]
        reader = SimpleNamespace(show=probes.recorder("reader.show", task))
        host.cleanup_owner = SimpleNamespace(
            runtime=SimpleNamespace(reader=reader),
            cleanup=probes.recorder("cleanup_owner.cleanup", {"status": "completed", "reason": "", "progress": {}}),
        )
        with contextlib.ExitStack() as stack:
            for collaborator, result in collaborators.items():
                owner, _, attribute = collaborator.rpartition(".")
                target = host.catalog if owner == "catalog" else CommandHostRuntime
                stack.enter_context(probes.patch(target, attribute, result, name=collaborator))
            workspaces = SimpleNamespace(create=probes.recorder("_git_workspaces.create", str(workspace)))
            stack.enter_context(mock.patch.object(CommandHostRuntime, "_git_workspaces", workspaces))
            results[name] = (run(host), probes)
    return results


#: The tick's work, each seam with the answer of an idle installation.
TICK_WORK: dict[str, tuple[Any, Any]] = {
    "publish_pending_attempt_usage": (attempt_accounting, []),
    "publish_pending_attempt_outcomes": (attempt_accounting, []),
    "observer_fence": (production, {}),
    "_reconcile_production": (production, []),
    "_advance_active": (production, ([], [], {})),
    "reconcile_post_merge_watches": (production, []),
    "reconcile_after_merge": (production, []),
    "_reconcile_sprint_budget": (production, []),
    "reconcile_observers": (production, []),
    "_production_claim_ready": (production, None),
    "reconcile_origin_returns": (production, []),
}
#: The probe's work: one active card advanced and the claim path, each through its abort-only runtime.
PROBE_WORK: dict[str, tuple[Any, Any]] = {
    "_probe_runtime": (production, None),
    "_production_tasks": (production, [{"ref": "sample-1"}]),
    "_production_tick_active": (production, {"status": "ok", "step": "advance"}),
    "_production_claim_ready": (production, None),
}


def run_production_operations(data_dir: Path) -> dict[str, tuple[Any, LockProbes]]:
    """production_tick and production_probe on an idle installation, to their end."""
    results: dict[str, tuple[Any, LockProbes]] = {}
    for name, tick, work in (
        ("production_tick", production.production_tick, TICK_WORK),
        ("production_probe", production.production_probe, PROBE_WORK),
    ):
        probes = LockProbes(data_dir)
        host = mock.Mock(spec=CommandHostRuntime)
        host.mode = "real"
        host.committing.side_effect = lambda flush: contextlib.nullcontext()
        runtime = SimpleNamespace(
            data_dir=data_dir,
            owner="unit-test",
            production_state=production.ProductionState(data_dir),
            host=host,
            reader=mock.Mock(**{"list.side_effect": probes.recorder("reader.list", [])}),
            cleanup=SimpleNamespace(replay=probes.recorder("cleanup.replay", [])),
            pause=SimpleNamespace(summary=probes.recorder("pause.summary", {"mode": "running"})),
        )
        with contextlib.ExitStack() as stack:
            for seam, (owner, result) in work.items():
                # The abort-only runtime of the probe is this fixture runtime itself.
                stack.enter_context(probes.patch(owner, seam, runtime if seam == "_probe_runtime" else result))
            results[name] = (tick(runtime), probes)
    return results
