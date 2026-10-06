"""The mutation half of the transport-independent layer: product runs without Orca or a transport.

:meth:`OperationLayer.run_start` raises a worker head for one card in a product-cut workspace;
:meth:`OperationLayer.run_review` raises a reviewer over an ended worker run's workspace and result;
:meth:`OperationLayer.run_state` reads one run and is the one place its ending is settled. Documents
follow the `web-run` schema; refusals are typed :class:`ummanu.webproto.errors.ReadError`.

Heads run only under `LocalPtyHeadRuntime` via `head_runtime_backends.build_head_runtime`; no Orca
pane, CLI or RPC is touched (`tests/test_web_run_protocol.py` enforces this). The profile comes from
the head registry and must name `local-pty`. Both start paths pass
:func:`ummanu.webproto.admission.admit` first; the process/record ordering lives in
:mod:`ummanu.webproto.lifecycle`. A request id keys one operation with one input fingerprint, and
every path returning an existing run republishes its events (:meth:`OperationLayer._republish`).
See docs/PROTOCOLS.md, "Running the pipeline".
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ummanu.board.backend import CARD, board_client
from ummanu.config import InstanceReport, validate_instance
from ummanu.runtime.head.command import HeadCommandError
from ummanu.runtime.head.spec import HeadSpec, HeadSpecError
from ummanu.runtime.head_runtimes import LOCAL_PTY_RUNTIME
from ummanu.runtime.heads import HeadRegistryError, load_registry
from ummanu.tasks import task_audit_for
from ummanu.webproto import run_events, sources
from ummanu.webproto import run_state as run_state_reads
from ummanu.webproto.admission import Admission, admit
from ummanu.webproto.boundary import ProtocolBoundary
from ummanu.webproto.errors import (
    InstallationUnavailable,
    OwnerConflict,
    ReadError,
    RunNotFound,
    RuntimeUnavailable,
    ValidationRefused,
)
from ummanu.webproto.lifecycle import (
    CLAUDE_JSON,
    INITIATOR,
    SETTLE_POLL_SECONDS,
    SETTLE_QUIET_SECONDS,
    SETTLE_SECONDS,
    STOP_BRING_UP_FAILED,
    STOP_DEADLINE,
    STOP_ENDED,
    STOP_RESULT_IN,
    SUBMIT_KEY,
    RunLifecycle,
)
from ummanu.webproto.runs import (
    DEFAULT_DEADLINE_SECONDS,
    HEADS_RELATIVE,
    RAISED,
    RAISING,
    RESULT_NAME,
    REVIEW_OPERATION,
    REVIEWER,
    SETTLED,
    START_OPERATION,
    WORKER,
    ProductRun,
    RequestMismatch,
    RunStore,
    RunStoreError,
    request_fingerprint,
)
from ummanu.webproto.store_io import write_document
from ummanu.webproto.workspaces import provision, workspace_path
from ummanu.runtime.head_runtime_backends import build_head_runtime

#: Re-exported names; the transitions using them live in :mod:`ummanu.webproto.lifecycle`.
__all__ = [
    "CLAUDE_JSON",
    "INITIATOR",
    "OperationLayer",
    "STOP_BRING_UP_FAILED",
    "STOP_DEADLINE",
    "STOP_ENDED",
    "STOP_RESULT_IN",
    "SUBMIT_KEY",
]

SCHEMA_VERSION = 1

#: The head's environment: where it must write its result (the one place a result can appear) and
#: which run it is.
RESULT_ENV = "UMMANU_RUN_RESULT"
RUN_ENV = "UMMANU_RUN_ID"
ROLE_ENV = "UMMANU_RUN_ROLE"
REF_ENV = "UMMANU_RUN_REF"
WORKSPACE_ENV = "UMMANU_RUN_WORKSPACE"


class OperationLayer(ProtocolBoundary):
    """One installation's product runtime, with no knowledge of who is asking.

    Construction does no I/O; every operation resolves instance, store and backend when called, so
    a long-lived transport never acts on stale configuration. The injectable seams are not modes.
    """

    def __init__(
        self,
        instance: str | Path,
        *,
        data_dir: str | Path | None = None,
        board_client: Any | None = None,
        registry_path: str | Path | None = None,
        registry: Any | None = None,
        runtime_factory: Callable[[Path], Any] | None = None,
        clock: Callable[[], float] = time.time,
        deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
        settle_seconds: float = SETTLE_SECONDS,
        settle_quiet_seconds: float = SETTLE_QUIET_SECONDS,
        settle_poll_seconds: float = SETTLE_POLL_SECONDS,
    ) -> None:
        self.instance = Path(instance)
        self._data_dir = Path(data_dir) if data_dir is not None else None
        self._board_client = board_client
        self._registry_path = Path(registry_path) if registry_path is not None else None
        self._registry = registry
        self._runtime_factory = runtime_factory
        self._clock = clock
        self.deadline_seconds = float(deadline_seconds)
        self.settle_seconds = float(settle_seconds)
        self.settle_quiet_seconds = float(settle_quiet_seconds)
        self.settle_poll_seconds = float(settle_poll_seconds)

    # -- shared plumbing -------------------------------------------------------------------

    def report(self) -> InstanceReport:
        report = validate_instance(self.instance)
        if not report.ok or report.data_dir is None:
            raise InstallationUnavailable(
                "this instance config does not validate: "
                + "; ".join(str(error) for error in report.errors[:5])
            )
        return report

    def data_dir(self, report: InstanceReport | None = None) -> Path:
        if self._data_dir is not None:
            return self._data_dir
        report = report if report is not None else self.report()
        assert report.data_dir is not None
        return report.data_dir

    def store(self, data_dir: Path | None = None) -> RunStore:
        return RunStore(data_dir if data_dir is not None else self.data_dir())

    def _client(self) -> Any:
        """The board client of this installation: an injected one, or the switch's (§2.2)."""
        return self._board_client or board_client(
            self.instance.parent if self.instance.is_file() else self.instance, serves=(CARD,)
        )

    def _registry_table(self) -> Any:
        if self._registry is not None:
            return self._registry
        try:
            return load_registry(self._registry_path)
        except HeadRegistryError as exc:
            raise ValidationRefused(f"the head registry could not be read: {exc}") from None

    def _profile(self, profile_id: str) -> tuple[HeadSpec, dict[str, Any]]:
        """One registry profile as a launchable spec; a non-`local-pty` profile is refused."""
        if not profile_id:
            raise ValidationRefused("a product run names the head profile it runs on")
        registry = self._registry_table()
        try:
            resolved = registry.resolve(profile_id)
            profile = dict(registry.profile(resolved))
            spec = HeadSpec.from_profile(resolved, profile)
        except (HeadRegistryError, HeadSpecError, HeadCommandError) as exc:
            raise ValidationRefused(f"head profile {profile_id!r} is not launchable: {exc}") from None
        return spec, profile

    def _runtime(self, data_dir: Path) -> Any:
        """This layer's backend via the product's name-to-backend mapping; `runtime_factory` is the
        test seam.
        """
        if self._runtime_factory is not None:
            return self._runtime_factory(data_dir)
        from ummanu.dispatch.watchdog import head_process_status

        return build_head_runtime(
            LOCAL_PTY_RUNTIME,
            local_pty_root=lambda: data_dir / HEADS_RELATIVE,
            head_process_status=head_process_status,
        )

    def _audit(self, data_dir: Path) -> Any:
        """The audit owner of the card backend (`task_audit_for`), where run events are published.

        Generic records must land in the PostgreSQL `requests` table (`docs/BOARD_STORE.md` §7.3),
        never in a data-dir file that backend's readers do not read.
        """
        return task_audit_for(self._client(), data_dir)

    # -- operations ------------------------------------------------------------------------

    def run_start(
        self,
        ref: str,
        *,
        request_id: str,
        profile: str,
        instruction: str = "",
    ) -> dict[str, Any]:
        """Raise a worker head for one card, or hand back the run this request id already owns.

        Order: claim the request id (with run id and paths decided), pass admission, then cut the
        workspace and raise the head. A repeat with the same id and inputs returns the existing run
        and republishes `product_run.started`; different inputs or operation are refused.
        """
        now = self._clock()
        report = self.report()
        data_dir = self.data_dir(report)
        store = self.store(data_dir)
        fingerprint = request_fingerprint(
            START_OPERATION, {"ref": ref, "profile": profile, "instruction": instruction}
        )
        existing = self._existing(
            store, request_id, operation=START_OPERATION, fingerprint=fingerprint
        )
        if existing is not None:
            return self._document(existing, now=now, state=self._republish(data_dir, existing, now=now))

        admission = admit(
            ref,
            report=report,
            data_dir=data_dir,
            board=self._client(),
            store=store,
            production_state=data_dir / "dispatcher" / "production-state.json",
        )
        spec, profile_table = self._profile(profile)
        run, created = self._claim(
            store,
            request_id,
            ref=admission.ref,
            project=admission.project,
            role=WORKER,
            spec=spec,
            data_dir=data_dir,
            now=now,
            operation=START_OPERATION,
            fingerprint=fingerprint,
        )
        if not created:
            return self._document(run, now=now, state=self._republish(data_dir, run, now=now))
        lifecycle = self._lifecycle(data_dir, store)
        with self._closing_before_any_spawn(lifecycle, run, now=now) as prepared:
            workspace = provision(admission.repo, Path(run.workspace), base=admission.default_branch)
            document = self._worker_document(run, admission, instruction=instruction, base=workspace)
            run = prepared(
                lifecycle.advance(
                    run, RAISING, now=now, spec=spec, profile=profile_table, document=document
                )
            )
        run = lifecycle.advance(
            run,
            RAISED,
            now=now,
            spec=spec,
            profile=profile_table,
            document=document,
            note=f"product run {run.run_id} for {run.ref}",
            env=self._environment(run),
        )
        run_events.publish_started(self._audit(data_dir), run)
        return self._document(run, now=now)

    def run_review(
        self,
        *,
        request_id: str,
        profile: str,
        ref: str = "",
        worker_run_id: str = "",
    ) -> dict[str, Any]:
        """Raise a reviewer head for a worker run that has ended, by that run's own result.

        The worker is settled via :meth:`run_state` first and refused while still running; the
        reviewer gets the worker's workspace and outcome. The request id is fingerprinted under this
        operation, so reusing the worker's start id is a validation refusal.
        """
        now = self._clock()
        report = self.report()
        data_dir = self.data_dir(report)
        store = self.store(data_dir)
        fingerprint = request_fingerprint(
            REVIEW_OPERATION, {"ref": ref, "worker_run_id": worker_run_id, "profile": profile}
        )
        existing = self._existing(
            store, request_id, operation=REVIEW_OPERATION, fingerprint=fingerprint
        )
        if existing is not None:
            self._republish(data_dir, existing, now=now)
            return self._review_document(existing, store, now=now)

        worker = self._worker_run(store, ref=ref, worker_run_id=worker_run_id)
        worker_state = self.run_state(worker.run_id)
        # `ended`, not the outcome value: a worker whose ending could not be established is still
        # over, and must stay reviewable.
        if not worker_state["state"]["ended"]:
            raise OwnerConflict(
                f"the worker run {worker.run_id} is {worker_state['state']['value']}: a review is "
                "raised by a worker's result, so it waits until that run has ended"
            )
        worker = store.get(worker.run_id) or worker

        admission = admit(
            worker.ref,
            report=report,
            data_dir=data_dir,
            board=self._client(),
            store=store,
            production_state=data_dir / "dispatcher" / "production-state.json",
        )
        spec, profile_table = self._profile(profile)
        run, created = self._claim(
            store,
            request_id,
            ref=admission.ref,
            project=admission.project,
            role=REVIEWER,
            spec=spec,
            data_dir=data_dir,
            now=now,
            parent_run_id=worker.run_id,
            workspace=worker.workspace,
            operation=REVIEW_OPERATION,
            fingerprint=fingerprint,
        )
        if not created:
            self._republish(data_dir, run, now=now)
            return self._review_document(run, store, now=now)
        lifecycle = self._lifecycle(data_dir, store)
        with self._closing_before_any_spawn(lifecycle, run, now=now) as prepared:
            document = self._review_prompt(run, worker, worker_state)
            run = prepared(
                lifecycle.advance(
                    run, RAISING, now=now, spec=spec, profile=profile_table, document=document
                )
            )
        run = lifecycle.advance(
            run,
            RAISED,
            now=now,
            spec=spec,
            profile=profile_table,
            document=document,
            note=f"review of product run {worker.run_id} for {run.ref}",
            env=self._environment(run),
        )
        run_events.publish_started(self._audit(data_dir), run)
        return self._review_document(run, store, now=now)

    def run_state(self, run_id: str) -> dict[str, Any]:
        """One run's state, and the one place a run's ending becomes durable.

        The first observation of an ended run settles it (`RunStore.settle`, once) and publishes
        `product_run.finished`; every later terminal read republishes, since the event is a pure
        function of the record and publication can fail after the settle. A run whose result is in,
        or that is past its deadline, has its head ended here.
        """
        now = self._clock()
        data_dir = self.data_dir()
        store = self.store(data_dir)
        run = store.get(run_id)
        if run is None:
            raise RunNotFound(f"there is no product run {run_id!r} on this installation")
        state = run_state_reads.observe(run, now=now)
        closing = self._reason_to_close(run, state, now=now)
        if closing:
            run = self._lifecycle(data_dir, store).advance(run, SETTLED, now=now, reason=closing)
            state = run_state_reads.observe(run, now=now)
        if run.ended:
            # Republish on every terminal read: publication may have failed after the settle.
            state = self._republish(data_dir, run, now=now, state=state) or state
        return self._document(run, now=now, state=state)

    def run_list(self, ref: str) -> dict[str, Any]:
        """Every product run of one card, each as the full :meth:`run_state` document.

        Settles nothing a single read would not. Full state is kept so an open run reading `unknown`
        or `source_unavailable` is distinguishable from one running. No runs is an empty list.
        """
        layer_now = self._clock()
        store = self.store()
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "product_runs",
            "observed_at": sources.isoformat(layer_now),
            "ref": ref,
            "items": [self.run_state(run.run_id) for run in store.for_ref(ref)],
        }

    # -- the pieces the operations are made of ----------------------------------------------

    def _lifecycle(self, data_dir: Path, store: RunStore) -> RunLifecycle:
        """This layer's one lifecycle, built per call exactly as the store and the backend are."""
        return RunLifecycle(
            store,
            self._runtime(data_dir),
            settle_seconds=self.settle_seconds,
            settle_quiet_seconds=self.settle_quiet_seconds,
            settle_poll_seconds=self.settle_poll_seconds,
        )

    @contextlib.contextmanager
    def _closing_before_any_spawn(self, lifecycle: RunLifecycle, run: ProductRun, *, now: float):
        """Close a run whose preparation failed, while it provably still holds no process.

        The request id is claimed before the workspace is cut, so an unsettled record would fence the
        card forever. The block reports each phase via `prepared`, and the close (through
        :meth:`RunLifecycle.advance`, which decides whether it may settle) uses the newest record.
        """
        latest = [run]

        def prepared(current: ProductRun) -> ProductRun:
            latest[0] = current
            return current

        try:
            yield prepared
        except BaseException as exc:
            with contextlib.suppress(ReadError, RunStoreError):
                lifecycle.advance(
                    latest[0],
                    SETTLED,
                    now=now,
                    reason=STOP_BRING_UP_FAILED,
                    failure=f"this run's head could not be raised: {exc}",
                )
            raise

    def _existing(
        self, store: RunStore, request_id: str, *, operation: str, fingerprint: str
    ) -> ProductRun | None:
        """The run this exact request already owns, or nothing, or a typed refusal.

        A request id keys one operation with one input fingerprint; a mismatch is a validation
        refusal, never another operation's run.
        """
        if not request_id:
            raise ValidationRefused("a product run operation names the request it is made under")
        try:
            return store.by_request(request_id, operation=operation, fingerprint=fingerprint)
        except RequestMismatch as exc:
            raise ValidationRefused(str(exc)) from None
        except RunStoreError as exc:
            raise RuntimeUnavailable(str(exc)) from None

    def _republish(
        self,
        data_dir: Path,
        run: ProductRun,
        *,
        now: float,
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Ensure this run's `started`/`finished` events are on the card's history; idempotent.

        Every path returning a recovered run calls this, since an audit failure after the head is up
        or after the settle would otherwise lose the event forever. Events are derived entirely from
        the run (`occurred_at` included), so a replay is byte-identical and never duplicates.
        """
        if not run.raised and not run.ended:
            return state
        observed = state if state is not None else run_state_reads.observe(run, now=now)
        audit = self._audit(data_dir)
        if run.raised:
            run_events.publish_started(audit, run)
        if run.ended:
            run_events.publish_finished(audit, run, observed)
        return observed

    def _claim(
        self,
        store: RunStore,
        request_id: str,
        *,
        ref: str,
        project: str,
        role: str,
        spec: HeadSpec,
        data_dir: Path,
        now: float,
        operation: str,
        fingerprint: str,
        parent_run_id: str = "",
        workspace: str = "",
    ) -> tuple[ProductRun, bool]:
        def build(run_id: str) -> ProductRun:
            run_dir = data_dir / HEADS_RELATIVE / run_id
            return ProductRun(
                run_id=run_id,
                request_id=request_id,
                ref=ref,
                project=project,
                role=role,
                profile=spec.profile_id,
                adapter=spec.adapter,
                runtime=spec.runtime,
                parent_run_id=parent_run_id,
                workspace=workspace or str(workspace_path(data_dir, run_id)),
                run_dir=str(run_dir),
                pid_file=str(run_dir / "head.pid"),
                journal_path=str(run_dir / run_state_reads.JOURNAL_NAME),
                log_path=str(run_dir / "supervisor.log"),
                result_path=str(run_dir / RESULT_NAME),
                started_at=now,
                deadline_at=now + self.deadline_seconds,
            )

        try:
            return store.claim(request_id, build, operation=operation, fingerprint=fingerprint)
        except RequestMismatch as exc:
            raise ValidationRefused(str(exc)) from None
        except RunStoreError as exc:
            raise RuntimeUnavailable(str(exc)) from None

    def _environment(self, run: ProductRun) -> dict[str, str]:
        return {
            RESULT_ENV: run.result_path,
            RUN_ENV: run.run_id,
            ROLE_ENV: run.role,
            REF_ENV: run.ref,
            WORKSPACE_ENV: run.workspace,
            "UMMANU_INSTANCE": str(self.instance),
        }

    def _reason_to_close(self, run: ProductRun, state: dict[str, Any], *, now: float) -> str:
        """Why this read should close the run, or `""`. The policy half of a close.

        * settled: never closed again;
        * unresolved: closed on every read (the stop is retried until the ending is confirmed);
        * running: closed when its result is in or its deadline passes;
        * otherwise: closed only when `state["ended"]`, never by the outcome value, which can read
          `source_unavailable` both for a gone head and for an unreadable launch identity.

        The deadline is the earlier of the run's own and this caller's, so it can only shorten.
        """
        if run.ended:
            return ""
        if run.unresolved:
            return STOP_ENDED
        if state["value"] == "running":
            if state["result"]["present"]:
                return STOP_RESULT_IN
            deadlines = [
                moment for moment in (run.deadline_at, run.started_at + self.deadline_seconds) if moment
            ]
            return STOP_DEADLINE if deadlines and now >= min(deadlines) else ""
        return STOP_ENDED if state["ended"] else ""

    def _worker_run(self, store: RunStore, *, ref: str, worker_run_id: str) -> ProductRun:
        if worker_run_id:
            run = store.get(worker_run_id)
            if run is None:
                raise RunNotFound(f"there is no product run {worker_run_id!r} on this installation")
            if run.role != WORKER:
                raise ValidationRefused(f"run {worker_run_id!r} is a {run.role} run, not a worker run")
            return run
        if not ref:
            raise ValidationRefused("a review names the worker run it answers, or the card it is on")
        workers = [run for run in store.for_ref(ref) if run.role == WORKER]
        if not workers:
            raise RunNotFound(f"card {ref} carries no product worker run to review")
        return workers[-1]

    # -- the documents a head is pointed at ---------------------------------------------------

    def _worker_document(self, run: ProductRun, admission: Admission, *, instruction: str, base: str) -> Path:
        """The worker's task document, written outside the workspace so it is not part of the diff."""
        card = admission.card
        body = "\n".join(
            [
                f"# {card.get('title') or run.ref}",
                "",
                f"Card: {run.ref}   Project: {run.project}   Run: {run.run_id}",
                f"Workspace: {run.workspace} (detached at {base or 'HEAD'})",
                "",
                "## What to do",
                "",
                str(card.get("description") or "").strip() or "(this card carries no description)",
                "",
                *([instruction.strip(), ""] if instruction.strip() else []),
                "## How this run ends",
                "",
                "Work only inside the workspace above. Commit nothing, push nothing and open no",
                "pull request: this run is a run, not a pipeline attempt.",
                "",
                f"When you are done, write your result as JSON to the path in ${RESULT_ENV}:",
                "",
                '    {"status": "done", "summary": "<one or two sentences>", "changed": ["<path>", ...]}',
                "",
                "Then stop working and wait. The product that owns this head reads that file, ends",
                "the process and records the run's outcome; you do not need to exit yourself.",
                "",
            ]
        )
        return self._write_document(run, "TASK.md", body)

    def _review_prompt(self, run: ProductRun, worker: ProductRun, worker_state: dict[str, Any]) -> Path:
        result = worker_state["state"]["result"]
        body = "\n".join(
            [
                f"# Review of product run {worker.run_id}",
                "",
                f"Card: {run.ref}   Project: {run.project}   Review run: {run.run_id}",
                f"Worker run: {worker.run_id} on {worker.profile}",
                f"Worker outcome: {worker_state['state']['value']} — {worker_state['state']['reason']}",
                f"Worker result file: {worker.result_path}",
                f"Worker journal: {worker.journal_path}",
                "",
                "## What to review",
                "",
                f"The worker's workspace is {run.workspace}. Read what it changed with",
                "`git status` and `git diff` there, read the worker's result file above, and judge",
                "whether the work the card asked for was actually done.",
                "",
                "## How this run ends",
                "",
                f"Write your verdict as JSON to the path in ${RESULT_ENV}:",
                "",
                '    {"verdict": "green"|"red", "summary": "<why>", "findings": ["<finding>", ...]}',
                "",
                "`green` means the work stands as it is. `red` means it does not, and the findings",
                "say what is wrong. Change nothing in the workspace; a review reads.",
                "",
                "Then stop working and wait. The product ends this head and records the verdict.",
                "",
                "## What the worker was asked to do",
                "",
                _read_text(Path(worker.run_dir) / "TASK.md"),
                "",
            ]
        )
        if not result["present"]:
            body += "\nThe worker published no result file. Say so in your verdict.\n"
        return self._write_document(run, "REVIEW.md", body)

    def _write_document(self, run: ProductRun, name: str, body: str) -> Path:
        """One head document, written through :mod:`ummanu.webproto.store_io`, with a local refusal
        naming which write failed.
        """
        path = Path(run.run_dir) / name
        try:
            write_document(path, body)
        except RunStoreError as exc:
            raise RuntimeUnavailable(f"this run's task document could not be written: {exc}") from None
        return path

    # -- documents -----------------------------------------------------------------------------

    def _document(
        self, run: ProductRun, *, now: float, state: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        observed = state if state is not None else run_state_reads.observe(run, now=now)
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "product_run",
            "observed_at": sources.isoformat(now),
            "run": _run_json(run),
            "state": observed,
            "reads": _reads(run),
        }

    def _review_document(self, run: ProductRun, store: RunStore, *, now: float) -> dict[str, Any]:
        worker = store.get(run.parent_run_id) if run.parent_run_id else None
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "product_review",
            "observed_at": sources.isoformat(now),
            "review": self._document(run, now=now),
            "worker": self._document(worker, now=now) if worker is not None else None,
        }


def _run_json(run: ProductRun) -> dict[str, Any]:
    payload = run.to_json()
    payload["started_at"] = sources.isoformat(run.started_at) if run.started_at else None
    payload["deadline_at"] = sources.isoformat(run.deadline_at) if run.deadline_at else None
    payload["settled_at"] = sources.isoformat(run.settled_at) if run.settled_at else None
    return payload


def _reads(run: ProductRun) -> dict[str, str]:
    """How this run is read back: the card's own `task_events` and `task_snapshot` (no run store)."""
    return {
        "task_events": f"ummanu web-read events --ref {run.ref}",
        "task_snapshot": f"ummanu web-read task --ref {run.ref}",
        "events_kind_started": run_events.STARTED,
        "events_kind_finished": run_events.FINISHED,
    }


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return "(the worker's task document could not be read)"
