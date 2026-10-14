"""Production host lifecycle implementation."""

from __future__ import annotations

import os
import signal
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from ummanu.dispatch.host_runtime.contracts import _durable_head_run
from ummanu.dispatch.state import DispatcherRecord, GatePrAuthorship, GatePublishedRef
from ummanu.dispatch.types import STOPPED_BY_DISPATCHER, STOPPED_BY_REVIEW_FREEZE, HostError
from ummanu.dispatch.watchdog import (
    HeadRunIdentityMismatch as _HeadRunIdentityMismatch,
    clear_head_heartbeat as _clear_head_heartbeat,
    guard_head_run_identity as _guard_head_run_identity,
    head_process_status as _head_process_status,
    head_run_process_status as _head_run_process_status,
    heartbeat_is_dead as _heartbeat_is_dead,
    heartbeat_is_live_match as _heartbeat_is_live_match,
    heartbeat_is_mismatch as _heartbeat_is_mismatch,
)
from ummanu.runtime import head as head_ops
from ummanu.runtime.head import CODEX_TUI_MODE, HeadSpec

HEAD_STOP_GRACE_SECONDS = 5.0

HEAD_STOP_POLL_SECONDS = 0.1


class HeadLifecycleMixin:

    def stop(self, record: DispatcherRecord) -> None:
        if self.mode == "noop" or not record.workspace:
            return
        try:
            self.stop_workspace(record)
        except HostError:
            pass

    def stop_workspace(self, record: DispatcherRecord) -> None:
        """Stop every head of this workspace and let a refusal reach the caller.

        Propagate stop refusal before any replacement.
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
        """Take one workspace's heads down, each by its own run's stop on the backend holding it."""
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
                             runs: Sequence[head_ops.HeadRun], *, recorded_only: bool = False,
                             remaining: Callable[[], float] | None = None) -> None:
        """Read scope ownership through the runtime's supported boundary, its native observations cut
        to a caller's `remaining`."""
        from ummanu.runtime.local_pty_head import fence_cleanup_scopes
        try:
            fence_cleanup_scopes(Path(self._local_pty_root()), workspace, task, runs,
                                 recorded_only=recorded_only, remaining=remaining)
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

        Reuse confirmed-stop evidence; never mint a replacement identity.
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

    def _freeze_worker(self, record: DispatcherRecord) -> None:
        """Shut the worker head down now that the reviewer is up, leaving the workspace untouched."""
        if self.mode == "noop" or not (record.handle or record.worker_leaf or record.worker_pid_file):
            return
        self.stop_head(record, "worker", STOPPED_BY_REVIEW_FREEZE)

    def retain_worker(self, record: DispatcherRecord) -> None:
        """Suspend a completed worker without throwing its provider conversation away."""
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
        """Whether this card's retained worker is provably gone, so nothing is left to freeze."""
        if self.mode == "noop" or not record.worker_continuation.retained:
            return False
        status = self._record_heartbeat_status(record, "worker")
        return _heartbeat_is_dead(status)

    def worker_addressable(self, record: DispatcherRecord) -> bool:
        """Whether this card's worker is a live conversation a prompt could be typed into."""
        if self.mode == "noop":
            return False
        return self._continuation_addressable(record)
