"""`LocalPtyHeadRuntime`: the six `HeadRuntime` verbs over a head this product owns the process of.

Stands on the local-pty substrate (`head.local_pty`): a supervisor in its own session holding the
head's pty, a Unix socket that answers without waiting for the head, and a versioned journal.
`HEAD_BUSY`, `HEAD_DRAINING`, `HEAD_ALIVE` and `HEAD_GONE` mean what they mean on every backend; the
shared contract suite runs against this one. See docs/HEAD_RUNTIME.md ("The runtime default",
"Supervisor progress journal") and docs/HEAD_SCOPES.md.

Invariants:

- `attach` hands out a supervisor stream; detaching is closing the socket and loses no output.
- `request_drain` makes the supervisor close admission, journal `drain.requested` and refuse later
  `input`; the receipt says `head_signalled` only once `status` confirms it. The agent process is
  told nothing; `DRAIN_HEAD_NOT_SIGNALLED` marks a drain the supervisor could not be told of.
- `ok` from `input` means admitted, not arrived. `deliver` follows the payload to its end and
  `_delivery_report` decides its outcome once, from `status`'s delivery record and the journal's
  `input.accepted`; `_follow` is the only place that asks those witnesses. Exactly four outcomes:
  - `DELIVERY_ARRIVED`: the whole payload, final newline included, landed: `HEAD_OK`, `complete`.
  - `DELIVERY_LEFT_A_PREFIX`: ended part-way: `HEAD_ALIVE` (or `HEAD_GONE`), `stalled` or `failed`.
    Fatal.
  - `DELIVERY_LANDED_NOTHING`: ended with no byte taken; the terminal is unchanged and the head is
    still worth delivering to.
  - `DELIVERY_UNESTABLISHED`: the fate cannot be established (the supervisor stopped answering
    after or while the payload was offered, or the substrate overran its own bound, and the
    journal has no record): `delivery_state` `unknown`. Fatal.
  An outcome is never inferred from a byte-count predicate or from which exception arrived. A
  production handoff's operation (`runtime.head.handoff`) adds one non-ending: `DELIVERY_PENDING`,
  a payload whose admission answer or end did not come back within the caller's allowance. It is
  not fatal and not retried: the substrate goes on writing it, and the next pass reads its end from
  `status` and the journal.
- Inside a production handoff every supervisor request is cut to the operation's deadline: the
  client is given it (`_connect`) and recomputes what is left before every `sendall` and `recv` of
  the framed exchange, so `_probe`, `_put`, `_follow_within` and a fatal close cannot outlast it, and
  nothing is attempted once it has passed. Everywhere else the bounds are what they always were.
- A fatal outcome closes admission here and on the substrate, and every later `deliver` is
  `HEAD_DRAINING` naming the reason: a prefix cannot be taken back (`TCIFLUSH` drops only unread
  input) and the next payload would be read as one line with it. An unknown fate may hide one.
- The delivery wait is derived: the substrate's bound for this delivery (`delivery_seconds` at
  `start`) plus `delivery_grace`, which only extends it. This runtime never stops watching first.
- Every frame reader (`status`, `input`, `drain`, `attach`) tests `ok` before contents. A refusal
  stated before a payload was offered is a refusal, not an unknown fate. The substrate's
  connection and attach bounds are transient: `HEAD_BUSY`, never fatal, never a drain. Only
  `_ask_to_stop` ignores `ok`; a stop's outcome is decided by the launch identity.
- A fatal delivery's drain is journalled after its `input.accepted`, except after
  `DELIVERY_UNESTABLISHED`, where a recovering supervisor may write `drain.requested` first.
- Turn, epoch and admission are recovered, never remembered, since every dispatcher tick is a new
  process. `_rehydrate` runs once per critical section, under the verb's lock: from the
  supervisor's `status` (one request per section, passed around as `_Probe`), else from a bounded
  journal tail (`local_pty.JOURNAL_TAIL_BYTES`, replayed in sequence order; a `run.started` in the
  window cuts off earlier incarnations). It only moves forward: the epoch is raised, a turn is
  adopted, admission is closed.
- The activity epoch is the journal sequence, never synthesised; a verb with no witnessed sequence
  leaves it unchanged. `ticks` is diagnostic only.
- Unknown state (debris neither witness can read; a torn, unreadable, out-of-order or mid-history
  journal tail) closes admission and grants no lease. A positively ended head also has admission
  closed, which makes it `rotation_ready`; `start` is decided by the launch identity alone and drops
  what was concluded about the previous incarnation.
- Liveness is the launch-identity reader (`head.identity.head_process_status`) passed in by the
  builder; there is no second pid-file reader.
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
from collections.abc import (
    Callable,
    Iterator,
    Mapping,
)
from contextlib import AbstractContextManager
from dataclasses import replace
from pathlib import Path
from typing import Any

from ummanu.runtime.head import local_pty
from ummanu.runtime.head.handoff import (
    HANDOFF_SETTLE,
    HandoffOperation,
    PromptHandoff,
)
from ummanu.runtime.head.local_pty import protocol
from ummanu.runtime.head.local_pty.scoped_lifecycle import ScopedHeadLifecycle
from ummanu.runtime.head.memory import MemoryScopeError as MemoryScopeError
from ummanu.runtime.head.operations import (
    HeadSpawnAborted,
    HeadSpawnFailed,
    HeadStopFailed,
    NudgePointer,
)
from ummanu.runtime.head.run import (
    HeadRun,
    StopInitiator,
    new_run_id,
)
from ummanu.runtime.head.runtime import (
    HEAD_ALIVE,
    HEAD_BUSY,
    HEAD_GONE,
    HEAD_OK,
    OBSERVE_NO_ADDRESS,
    AttachReceipt,
    DrainReceipt,
    HeadActivity,
    ObserveReceipt,
    StartReceipt,
    StopReceipt,
    TurnLease,
)
from ummanu.runtime.head.spec import HeadSpec
from ummanu.runtime.head.task_ref import TaskRef
from ummanu.runtime.local_pty_delivery import (
    DeliveryReport,
    _outcome_of,
)
from ummanu.runtime.local_pty_durable import (
    _DurableHead,
    _journal_state,
    _supervisor_state,
)
from ummanu.runtime.local_pty_handoff import (
    PromptDeliveryMixin,
    _handoff_progress as _handoff_progress,
)
from ummanu.runtime.local_pty_inspection import (
    fence_cleanup_scopes as fence_cleanup_scopes,
    head_run_directory as head_run_directory,
    head_run_pid_file as head_run_pid_file,
    head_run_supervisor_lease as head_run_supervisor_lease,
    runtime_scope_inventory as runtime_scope_inventory,
)
from ummanu.runtime.local_pty_journal import (
    _has_exited as _has_exited,
    _last_event_at,
    head_run_first_record as head_run_first_record,
    head_run_journal as head_run_journal,
    head_run_journal_read as head_run_journal_read,
    head_run_journal_tail as head_run_journal_tail,
    head_run_loss_reason as head_run_loss_reason,
    head_run_screen_lines as head_run_screen_lines,
    head_run_turn_reading as head_run_turn_reading,
)
from ummanu.runtime.local_pty_values import (
    _CONFIRM_POLL_SECONDS,
    _UNREACHABLE,
    ADOPTED_TURN_SUBJECT as ADOPTED_TURN_SUBJECT,
    DELIVER_DRAINED_BEFORE_THIS_RUNTIME as DELIVER_DRAINED_BEFORE_THIS_RUNTIME,
    DELIVER_DRAINED_BY_THIS_RUNTIME,
    DELIVER_HANDOFF_UNESTABLISHED as DELIVER_HANDOFF_UNESTABLISHED,
    DELIVER_HEAD_ENDED as DELIVER_HEAD_ENDED,
    DELIVER_NOT_SUBMITTED as DELIVER_NOT_SUBMITTED,
    DELIVER_STATE_UNKNOWN as DELIVER_STATE_UNKNOWN,
    DELIVERY_ARRIVED as DELIVERY_ARRIVED,
    DELIVERY_GRACE_SECONDS as DELIVERY_GRACE_SECONDS,
    DELIVERY_LANDED_NOTHING as DELIVERY_LANDED_NOTHING,
    DELIVERY_LEFT_A_PREFIX as DELIVERY_LEFT_A_PREFIX,
    DELIVERY_PENDING as DELIVERY_PENDING,
    DELIVERY_STATE_UNKNOWN as DELIVERY_STATE_UNKNOWN,
    DELIVERY_UNESTABLISHED as DELIVERY_UNESTABLISHED,
    DRAIN_AFTER_PARTIAL_DELIVERY as DRAIN_AFTER_PARTIAL_DELIVERY,
    DRAIN_AFTER_UNESTABLISHED_DELIVERY as DRAIN_AFTER_UNESTABLISHED_DELIVERY,
    DRAIN_HEAD_NOT_SIGNALLED,
    DRAIN_HEAD_SIGNALLED,
    HANDOFF_ALLOWANCE_SPENT,
    JOURNAL_NAME as JOURNAL_NAME,
    OBSERVE_HEAD_EXITED as OBSERVE_HEAD_EXITED,
    OBSERVE_NO_RUN_DIRECTORY as OBSERVE_NO_RUN_DIRECTORY,
    OBSERVE_STATUS_UNREADABLE as OBSERVE_STATUS_UNREADABLE,
    OBSERVE_SUPERVISOR_UNREACHABLE as OBSERVE_SUPERVISOR_UNREACHABLE,
    REHYDRATED_ABSENT,
    REHYDRATED_FROM_SUPERVISOR,
    REHYDRATED_TRANSIENT,
    REHYDRATED_UNKNOWN,
    RUN_EXITED as RUN_EXITED,
    START_HEAD_ALREADY_UP as START_HEAD_ALREADY_UP,
    START_TURN_IN_FLIGHT as START_TURN_IN_FLIGHT,
    STOP_ACTIVITY_SINCE as STOP_ACTIVITY_SINCE,
    STOP_CONFIRM_SECONDS as STOP_CONFIRM_SECONDS,
    STOP_TURN_IN_FLIGHT as STOP_TURN_IN_FLIGHT,
    SUBMIT_KEY as SUBMIT_KEY,
    UNDECLARED_DELIVERY_BOUND as UNDECLARED_DELIVERY_BOUND,
    AttachedStream as AttachedStream,
    IdentityReader,
    LocalPtyRuntimeError as LocalPtyRuntimeError,
    _Address,
    _binding_of,
    _declared_bound as _declared_bound,
    _in_flight,
    _Probe,
    _Refusal,
    _refusal_status,
    _spawn_failure,
    _spawn_status,
    _task_of,
    _unobservable,
    _wake_hook,
    _with_pid_file,
)
from ummanu.runtime.prompt_document import NUDGE_FILE_MODE
from ummanu.runtime.tui_delivery import (
    READINESS_BUSY,
    READINESS_READY,
    STAGE_NONE,
    DeliveryEvidence,
    payload_fingerprint,
)

#: How long a prompt waits for an agent's TUI to settle before it is typed, how much silence counts
#: as settled, and how often that is asked.
PROMPT_SETTLE_SECONDS = 90.0
PROMPT_QUIET_SECONDS = 4.0
PROMPT_POLL_SECONDS = 0.25
#: A head that has printed nothing at all is waited on this long before its silence counts as
#: settled: the process may not have drawn its first frame yet.
PROMPT_FIRST_OUTPUT_SECONDS = 20.0


SUBMIT_CONFIRM_SECONDS = 20.0


class LocalPtyHeadRuntime(PromptDeliveryMixin):
    """The six verbs over a head whose process, terminal and journal this product owns.

    `root` holds run directories and must be short: a Unix socket address is about 100 bytes and
    `protocol.socket_path_for` refuses a longer one. `head_process_status` is the required
    launch-identity reader. `connect_timeout` bounds reaching a supervisor and each non-delivery
    question; once a delivery is admitted, `_put` rebounds the connection to `delivery_wait_for`.

    One lock serialises `deliver`, `request_drain`, `stop` and `stop_if_quiescent` across all heads
    of this runtime, and `deliver` holds it for the whole reception (at most
    `protocol.INPUT_DELIVERY_SECONDS` + `DELIVERY_GRACE_SECONDS`, as no profile can raise
    `delivery_seconds`). That is fine while each tick drives every verb from one thread. Once a
    runtime has a second concurrent caller or a configurable `delivery_seconds`, use a per-head lock
    and keep this one only for the shared `HeadActivity` and `_fatal` bookkeeping.
    """

    #: The supervisor wraps the head command in the launch-identity heartbeat, so callers pass a
    #: bare command; a pre-wrapped one would `exec` the inner writer and never run the head.
    writes_launch_identity = True

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        head_process_status: IdentityReader,
        activity: HeadActivity | None = None,
        spawn: Callable[..., local_pty.HeadHandle] = local_pty.spawn_head,
        connect_timeout: float = 5.0,
        delivery_grace: float = DELIVERY_GRACE_SECONDS,
        delivery_poll: float = 0.02,
        stop_timeout: float = STOP_CONFIRM_SECONDS,
        prompt_settle: float | None = None,
        prompt_quiet: float | None = None,
        prompt_poll: float | None = None,
        prompt_first_output: float | None = None,
        submit_confirm: float | None = None,
        monotonic: Callable[[], float] | None = None,
        wall: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if not callable(head_process_status):
            raise LocalPtyRuntimeError(
                "this runtime is given the product's launch-identity reader; it does not invent a "
                "second way to ask whether a head's process is alive"
            )
        if delivery_grace < 0:
            raise LocalPtyRuntimeError(
                "the grace over the substrate's delivery bound only ever extends the wait; a "
                "negative one would be the independent second knob this backend does not have"
            )
        self.root = Path(root)
        self.activity = activity or HeadActivity()
        self._identity = head_process_status
        self._spawn = spawn
        self._connect_timeout = connect_timeout
        self._delivery_grace = float(delivery_grace)
        self._delivery_poll = delivery_poll
        self._stop_timeout = stop_timeout
        # The prompt waits default to the module's numbers as they are when this runtime is built,
        # so a test can shorten them for a runtime the dispatcher builds for itself.
        self._prompt_settle = float(PROMPT_SETTLE_SECONDS if prompt_settle is None else prompt_settle)
        self._prompt_quiet = float(PROMPT_QUIET_SECONDS if prompt_quiet is None else prompt_quiet)
        self._prompt_poll = float(PROMPT_POLL_SECONDS if prompt_poll is None else prompt_poll)
        self._prompt_first_output = float(
            PROMPT_FIRST_OUTPUT_SECONDS if prompt_first_output is None else prompt_first_output
        )
        self._submit_confirm = float(SUBMIT_CONFIRM_SECONDS if submit_confirm is None else submit_confirm)
        # The clocks a production handoff reads (`_handoff_prompt`), injectable so its stages can be
        # driven by a fake clock. The blocking flow keeps reading `time` directly.
        self._monotonic = monotonic or time.monotonic
        self._wall = wall or time.time
        self._sleep = sleep or time.sleep
        # Reentrant, so `stop_if_quiescent` can perform `stop`.
        self._lock = threading.RLock()
        # This backend alone tracks terminals left with an unfinished payload prefix.
        self._fatal: dict[str, str] = {}
        # A terminal prefix takes precedence over a rehydrated closure reason.
        self._admission_notes: dict[str, str] = {}
        # What this runtime saw of each head's output before, for supervisors that predate
        # `output_idle_seconds` (`_quiet_since`): the incarnation-and-count key and since when.
        self._quiet_marks: dict[str, tuple[tuple[Any, ...], float]] = {}

    def delivery_wait_for(self, substrate_bound: float) -> float:
        """How long this runtime watches a delivery the substrate bounded at `substrate_bound`.

        Always the substrate's bound (its default when undeclared) plus the non-negative grace.
        """
        bound = float(substrate_bound) if substrate_bound > 0 else UNDECLARED_DELIVERY_BOUND
        return bound + self._delivery_grace

    # -- the six verbs ------------------------------------------------------------------------

    def start(
        self,
        spec: HeadSpec,
        workspace: str,
        task_ref: TaskRef,
        *,
        command: str,
        title: str,
        pointer: NudgePointer | None = None,
        run_id: str = "",
        role: str = "",
        run: HeadRun | None = None,
        subject: str = "",
        rows: int = 24,
        cols: int = 80,
        quiet_seconds: float | None = None,
        delivery_seconds: float | None = None,
        env: Mapping[str, str] | None = None,
        pid_file: str = "",
        scope_generation: str = "",
        transport: Any = None,
        launch_admission: Callable[[], AbstractContextManager[Any]] | None = None,
        handoff: PromptHandoff | None = None,
        **ignored: Any,
    ) -> StartReceipt:
        """Bring one head up under its own supervisor and point it at its task.

        `title` is accepted and unused (there is no pane). A run this runtime holds a turn for, or
        whose launch identity on disk says it is up (`_already_up`; each tick is a new process), is
        refused before anything is spawned. `pid_file` is where the caller reads liveness when it is
        not the run directory's `head.pid`; the supervisor writes the launch identity there.

        `scope_generation` names a caller's durable write-ahead admission: the launch preserves it
        and cannot replace an existing scope owner. Without it, a fresh generation is acquired after
        proving the previous owner empty.

        A `pointer` with a `transport` is an agent's prompt, delivered as `deliver` does, after the
        spawn and outside the lock. With a `handoff` that delivery is the resumable production one,
        and none of it happens here: the head is kept up and answers `HEAD_BUSY` at
        `HANDOFF_SETTLE`, and `deliver` with the same `handoff` hands the prompt off on a later pass.
        """
        del title, ignored
        receipt = self._start_locked(
            spec,
            workspace,
            task_ref,
            command=command,
            pointer=None if transport is not None else pointer,
            run_id=run_id,
            role=role,
            run=run,
            subject=subject,
            rows=rows,
            cols=cols,
            quiet_seconds=quiet_seconds,
            delivery_seconds=delivery_seconds,
            env=env,
            pid_file=pid_file,
            scope_generation=scope_generation,
            launch_admission=launch_admission,
        )
        live = receipt.run
        if pointer is None or transport is None or live is None or not receipt.ok:
            return receipt
        if handoff is not None:
            # A head this call raised has not been quiet for anything yet, and its caller still has a
            # writer fence to make before anything is typed (`CommandHostRuntime.start_review`): the
            # prompt is handed off by `deliver` on a later pass, and the head is kept up for it.
            payload_bytes, payload_hash = payload_fingerprint(pointer.text)
            why = "the head is up and its prompt is handed off on a later pass"
            return StartReceipt(
                status=HEAD_BUSY,
                run=live,
                reason=f"production handoff pending at {HANDOFF_SETTLE}: {why}",
                evidence=DeliveryEvidence(
                    handle=live.handle,
                    subject=subject or "head-launch",
                    stage=STAGE_NONE,
                    payload_bytes=payload_bytes,
                    payload_sha256=payload_hash,
                    delivery_mode=NUDGE_FILE_MODE if pointer.document else "",
                    document_path=pointer.document,
                    adapter=live.spec.adapter,
                    reason=why,
                    handoff_stage=HANDOFF_SETTLE,
                ),
                epoch=receipt.epoch,
                lease=receipt.lease,
                rotation_ready=receipt.rotation_ready,
                handoff_stage=HANDOFF_SETTLE,
            )
        delivered = self._deliver_prompt(live, pointer, subject or "head-launch", _wake_hook(transport))
        if delivered.ok:
            return StartReceipt(
                status=HEAD_OK,
                run=delivered.run or live.working(),
                delivery=delivered.delivery,
                epoch=delivered.epoch,
                lease=delivered.lease,
                rotation_ready=delivered.rotation_ready,
            )
        with self._lock:
            # Stop a head whose bring-up prompt did not start its turn.
            self.activity.release(live.run_id)
            report = delivered.evidence if isinstance(delivered.evidence, DeliveryReport) else None
            refusal = None
            if report is None or not report.fatal:
                refusal = _Refusal(
                    status=delivered.status,
                    reason=delivered.reason,
                    failure=delivered.failure,
                    evidence=delivered.evidence,
                )
            return self._abandon_bring_up(live, report, refusal, delivered.epoch)

    def _start_locked(
        self,
        spec: HeadSpec,
        workspace: str,
        task_ref: TaskRef,
        *,
        command: str,
        pointer: NudgePointer | None,
        run_id: str,
        role: str,
        run: HeadRun | None,
        subject: str,
        rows: int,
        cols: int,
        quiet_seconds: float | None,
        delivery_seconds: float | None,
        env: Mapping[str, str] | None,
        pid_file: str,
        scope_generation: str,
        launch_admission: Callable[[], AbstractContextManager[Any]] | None = None,
    ) -> StartReceipt:
        """`start` under the lock: the refusals, the spawn and a bare pointer's one delivery."""
        with self._lock:
            claimed = run.run_id if run is not None else (run_id or "")
            if claimed:
                held = self.activity.lease(claimed)
                if held is not None:
                    return StartReceipt(
                        status=HEAD_BUSY,
                        run=run,
                        reason=(
                            f"this runtime is already running turn {held.lease_id} for "
                            f"{held.subject or 'a caller'} on run {claimed}: a bring-up over it "
                            "would claim a head that is already up"
                        ),
                        evidence={"refusal": START_TURN_IN_FLIGHT, "run_id": claimed},
                        epoch=self.activity.epoch(claimed),
                        lease=held,
                    )
                already_up = self._already_up(
                    claimed,
                    run,
                    spec=spec,
                    workspace=workspace,
                    task_ref=task_ref,
                    role=role,
                    pid_file=pid_file,
                )
                if already_up is not None:
                    return already_up
            identity = claimed or new_run_id()
            candidate = run or HeadRun(
                run_id=identity, spec=spec, workspace=workspace, task_ref=task_ref, role=role
            )
            designated = {"pid_file": pid_file} if pid_file else {}
            try:
                handle = self._spawn(
                    root=self.root,
                    run_id=identity,
                    role=role or (run.role if run is not None else ""),
                    task=_binding_of(task_ref),
                    command=command,
                    cwd=workspace,
                    rows=rows,
                    cols=cols,
                    quiet_seconds=quiet_seconds,
                    delivery_seconds=delivery_seconds,
                    env=env,
                    **(
                        {"memory_limit_mib": spec.memory_limit_mib}
                        if spec.memory_limit_mib is not None
                        else {}
                    ),
                    **({"scope_generation": scope_generation} if scope_generation else {}),
                    **designated,
                    **({"launch_admission": launch_admission} if launch_admission is not None else {}),
                )
            except local_pty.LocalPtySpawnError as exc:
                retained = candidate if not exc.cleanup_complete else run
                if retained is not None and exc.scope_generation:
                    retained = replace(retained, scope_generation=exc.scope_generation)
                    retained = _with_pid_file(retained, pid_file)
                return StartReceipt(
                    status=_spawn_status(exc),
                    run=retained,
                    reason=str(exc),
                    failure=_spawn_failure(exc, retained),
                    evidence={"reason": exc.reason, "detail": exc.detail},
                    epoch=self.activity.epoch(identity),
                )
            live = (
                run
                or HeadRun(
                    run_id=identity,
                    spec=spec,
                    workspace=workspace,
                    task_ref=task_ref,
                    role=role,
                )
            ).rebound(str(handle.socket_path), leaf=identity)
            live = _with_pid_file(live, str(handle.pid_file))
            live = replace(live, scope_generation=getattr(handle, "scope_generation", ""))
            # A new supervisor incarnation drops prior terminal state and reuses its journal scale.
            self.activity.forget(identity)
            self._fatal.pop(identity, None)
            self._admission_notes.pop(identity, None)
            self.activity.noted(identity)
            epoch = self._durable_epoch(live)
            if pointer is None:
                return StartReceipt(
                    status=HEAD_OK,
                    run=live,
                    epoch=epoch,
                    rotation_ready=self.activity.rotatable(identity),
                )
            lease = self.activity.grant(identity, subject or "head-launch")
            try:
                report, refusal = self._put(live, pointer, subject or "head-launch")
            except BaseException:
                # Failed bring-up must not retain a lease that blocks later delivery.
                self.activity.release(identity)
                raise
            if refusal is not None or report is None or report.outcome != DELIVERY_ARRIVED:
                # Stop a head whose bring-up prompt did not land.
                self.activity.release(identity)
                return self._abandon_bring_up(live, report, refusal, epoch)
            self.activity.noted(identity)
            return StartReceipt(
                status=HEAD_OK,
                run=live.working(),
                delivery=_outcome_of(live, pointer, report, subject or "head-launch"),
                epoch=self.activity.advance_to(identity, report.seq),
                lease=lease,
                rotation_ready=self.activity.rotatable(identity),
            )

    def observe(self, run: HeadRun) -> ObserveReceipt:
        """What the substrate can say about this head now: its status, its journal, its process.

        `busy` comes from the supervisor's turn state and this runtime's lease, and stays `None`
        without an answer. The epoch is the journal sequence, so a quiet head reads the same number
        twice. The supervisor is asked once, and that answer both rehydrates and is reported.
        """
        with self._lock:
            address = self._address(run)
            probe = self._probe(address) if address is not None else None
            # Report epoch, turn, and admission from one authoritative observation.
            self._rehydrate(run, probe)
            epoch = self.activity.epoch(run.run_id)
            lease = self.activity.lease(run.run_id)
            rotatable = self.activity.rotatable(run.run_id)
            if address is None or probe is None:
                return _unobservable(run, OBSERVE_NO_ADDRESS, epoch, lease, rotatable)
            if not address.journal_path.exists() and not address.socket_path.exists():
                return _unobservable(run, OBSERVE_NO_RUN_DIRECTORY, epoch, lease, rotatable)
            if probe.error is not None:
                return self._unreachable(run, address, epoch, lease, rotatable, probe.error)
            status = probe.status
            if status is None:
                return _unobservable(
                    run,
                    OBSERVE_STATUS_UNREADABLE,
                    epoch,
                    lease,
                    rotatable,
                    evidence=probe.answer,
                )
            if not status.get("alive"):
                # A dead head cannot retain a running turn lease.
                self.activity.release(run.run_id)
                return ObserveReceipt(
                    status=HEAD_GONE,
                    run=run,
                    reason=OBSERVE_HEAD_EXITED,
                    evidence=status,
                    epoch=epoch,
                    lease=None,
                    rotation_ready=self.activity.rotatable(run.run_id),
                    handle=str(address.socket_path),
                    leaf=run.leaf or run.run_id,
                    connected=False,
                    busy=False,
                )
            turn_open = bool(status.get("turn_open"))
            delivering = _in_flight(status)
            if lease is not None and not turn_open and not delivering:
                # A completed turn makes a drained head rotatable.
                self.activity.release(run.run_id)
                lease = None
                rotatable = self.activity.rotatable(run.run_id)
            return ObserveReceipt(
                status=HEAD_OK,
                run=run,
                evidence=status,
                epoch=epoch,
                lease=lease,
                rotation_ready=rotatable,
                handle=str(address.socket_path),
                leaf=run.leaf or run.run_id,
                connected=True,
                readiness=READINESS_BUSY if (turn_open or delivering) else READINESS_READY,
                last_output_at=_last_event_at(address),
                busy=turn_open or delivering or lease is not None,
            )

    def request_drain(self, run: HeadRun, initiator: StopInitiator) -> DrainReceipt:
        """Take this head out of service, here and at the supervisor that owns it.

        Admission closes locally, and on the supervisor when the socket answers; `head_signalled` is
        claimed only once `status` reads it back. A drain closes admission, never the turn: a
        mid-turn head keeps its turn and lease, and is `rotation_ready` once that turn closes.
        """
        if not isinstance(initiator, StopInitiator):
            raise TypeError("a drain names who requested it")
        with self._lock:
            self.activity.close_admission(run.run_id)
            self._admission_notes.setdefault(run.run_id, DELIVER_DRAINED_BY_THIS_RUNTIME)
            signalled, evidence, seq, probe = self._close_substrate_admission(run, initiator)
            # Rehydrate from the post-drain read-back, the section's one status request: a drain
            # never closes a turn, so it still states the turn `rotation_ready` depends on.
            self._rehydrate(run, probe)
            # The epoch counts the `drain.requested` record this verb just caused.
            self.activity.noted(run.run_id)
            return DrainReceipt(
                status=HEAD_OK if signalled else HEAD_ALIVE,
                run=run,
                reason=DRAIN_HEAD_SIGNALLED if signalled else DRAIN_HEAD_NOT_SIGNALLED,
                evidence=evidence,
                draining=True,
                head_signalled=signalled,
                epoch=self.activity.advance_to(run.run_id, seq),
                lease=self.activity.lease(run.run_id),
                rotation_ready=self.activity.rotatable(run.run_id),
            )

    def stop(
        self,
        run: HeadRun,
        initiator: StopInitiator,
        *,
        signal_name: str = "TERM",
        **ignored: Any,
    ) -> StopReceipt:
        """End this head unconditionally, and confirm it against the head's launch identity.

        `stop_if_quiescent` is the conditional form. The initiator is recorded on the run before the
        signal, so a stop that outlives this process names who began it. A scoped head also needs
        the durable owner's recursive empty proof; the identity going dead only confirms the exit.

        A caller's `remaining` (seconds left of its deadline, as a callable) cuts every wait of the
        stop: this runtime's lock, the supervisor exchange, the scope's termination and the exit
        confirmation. What is unconfirmed when it runs out is an unsettled receipt, never a stop.
        """
        remaining: Callable[[], float] | None = ignored.get("remaining")
        with self._locked_within(remaining) as locked:
            if not locked:
                finishing = run.finishing(initiator)
                reason = "the caller's deadline passed while another operation of this runtime ran"
                return StopReceipt(
                    status=HEAD_ALIVE,
                    run=finishing,
                    reason=reason,
                    failure=HeadStopFailed(reason, run=finishing),
                    epoch=self.activity.epoch(run.run_id),
                    lease=self.activity.lease(run.run_id),
                )
            preflight = ignored.get("preflight")
            if callable(preflight):
                preflight(run)
            finishing = run.finishing(initiator)
            commit = ignored.get("commit")
            if callable(commit):
                commit(finishing)
            address = self._address(run)
            if address is None:
                return StopReceipt(
                    status=HEAD_ALIVE,
                    run=finishing,
                    reason="this head has no run directory to address, so nothing could be stopped",
                    failure=HeadStopFailed("a head with no address cannot be stopped", run=finishing),
                    epoch=self.activity.epoch(run.run_id),
                    lease=self.activity.lease(run.run_id),
                )
            asked = None
            try:
                owner = ScopedHeadLifecycle.from_run_dir(address.run_dir)
                if owner is not None:
                    if owner.run_id != run.run_id or owner.generation != run.scope_generation:
                        raise MemoryScopeError("scope owner does not match the stop's run")
                    with owner.ownership() as record:
                        if address.pid_file.exists():
                            status = self._identity(
                                str(address.pid_file),
                                expected={
                                    "run_id": run.run_id,
                                    "role": run.role,
                                    "task": _task_of(run),
                                },
                            )
                            if (
                                status.get("state") not in ("dead", "live-match")
                                or (status.get("record") or {}).get("run_id") != run.run_id
                            ):
                                raise MemoryScopeError("head identity does not match the scoped stop")
                        record["stop_initiator"] = initiator.to_json()
                        owner.update_owner(address.run_dir, record)
                        asked = self._ask(address, initiator, signal_name, remaining)
                        if remaining is None:
                            owner.stop_owned(record)
                        else:
                            owner.stop_owned(record, remaining=remaining)
                        # A launch can fail before any head identity or journal exists.
                        # Only this generation's durable recursive proof settles that case.
                        gone = record["cleanup_complete"] and (
                            not address.pid_file.exists() or self._await_gone(address, run, remaining)
                        )
                else:
                    if run.scope_generation:
                        raise MemoryScopeError("the scoped run has lost its owner")
                    asked = self._ask(address, initiator, signal_name, remaining)
                    gone = self._await_gone(address, run, remaining)
            except (MemoryScopeError, OSError, ValueError) as exc:
                return StopReceipt(
                    status=HEAD_ALIVE,
                    run=finishing,
                    reason=str(exc),
                    failure=HeadStopFailed(str(exc), run=finishing),
                    evidence=asked,
                    epoch=self.activity.epoch(run.run_id),
                    lease=self.activity.lease(run.run_id),
                    rotation_ready=False,
                )
            if not gone:
                return StopReceipt(
                    status=HEAD_ALIVE,
                    run=finishing,
                    reason=(
                        f"this head was asked to stop and its process was still there "
                        f"{self._stop_timeout:g}s later"
                        if remaining is None
                        else "this head was asked to stop and its process was still there when the "
                        "caller's deadline passed"
                    ),
                    failure=HeadStopFailed("the head's process outlived the stop it was sent", run=finishing),
                    evidence=asked,
                    epoch=self.activity.epoch(run.run_id),
                    lease=self.activity.lease(run.run_id),
                    rotation_ready=self.activity.rotatable(run.run_id),
                )
            # Read the journal's sequence before forgetting the head, so the receipt's epoch is one
            # the next tick can compare.
            self.activity.noted(run.run_id)
            epoch = self._durable_epoch(run)
            self.activity.forget(run.run_id)
            self._fatal.pop(run.run_id, None)
            self._admission_notes.pop(run.run_id, None)
            return StopReceipt(
                status=HEAD_OK,
                run=finishing if finishing.settled else finishing.exited(),
                evidence=asked,
                epoch=epoch,
            )

    def attach(self, run: HeadRun) -> AttachReceipt:
        """Join a caller to this head's live stream through the substrate's bounded attach.

        The stream travels on `evidence` as an `AttachedStream`. `handle` is the socket, returned
        even when the stream is refused. Detaching is closing the client.
        """
        with self._lock:
            self._rehydrate(run)
            epoch = self.activity.epoch(run.run_id)
            lease = self.activity.lease(run.run_id)
            rotatable = self.activity.rotatable(run.run_id)
            address = self._address(run)
            if address is None or not address.socket_path.exists():
                return AttachReceipt(
                    status=HEAD_GONE,
                    run=run,
                    reason=OBSERVE_NO_RUN_DIRECTORY if address is not None else OBSERVE_NO_ADDRESS,
                    epoch=epoch,
                    lease=lease,
                    rotation_ready=rotatable,
                    handle=run.handle,
                    leaf=run.leaf or run.run_id,
                )
            try:
                client = self._connect(address)
            except _UNREACHABLE as exc:
                return AttachReceipt(
                    status=HEAD_ALIVE if self._process_alive(address, run) else HEAD_GONE,
                    run=run,
                    reason=OBSERVE_SUPERVISOR_UNREACHABLE,
                    evidence=str(exc),
                    epoch=epoch,
                    lease=lease,
                    rotation_ready=rotatable,
                    handle=str(address.socket_path),
                    leaf=run.leaf or run.run_id,
                )
            try:
                answer = client.attach()
            except _UNREACHABLE as exc:
                # Connected, then nothing came back: classify by the head's process, not the socket.
                client.close()
                return AttachReceipt(
                    status=HEAD_ALIVE if self._process_alive(address, run) else HEAD_GONE,
                    run=run,
                    reason=OBSERVE_SUPERVISOR_UNREACHABLE,
                    evidence=str(exc),
                    epoch=epoch,
                    lease=lease,
                    rotation_ready=rotatable,
                    handle=str(address.socket_path),
                    leaf=run.leaf or run.run_id,
                )
            if not answer.get("ok"):
                client.close()
                error = str(answer.get("error") or "")
                return AttachReceipt(
                    status=_refusal_status(error),
                    run=run,
                    reason=error or "the supervisor refused the attachment",
                    evidence=answer,
                    epoch=epoch,
                    lease=lease,
                    rotation_ready=rotatable,
                    handle=str(address.socket_path),
                    leaf=run.leaf or run.run_id,
                )
            return AttachReceipt(
                status=HEAD_OK,
                run=run,
                evidence=AttachedStream(
                    client=client,
                    socket_path=str(address.socket_path),
                    backlog=bytes(answer.get("bytes_data") or b""),
                    dropped_bytes=int(answer.get("dropped_bytes") or 0),
                    total_bytes=int(answer.get("total_bytes") or 0),
                ),
                epoch=epoch,
                lease=lease,
                rotation_ready=rotatable,
                handle=str(address.socket_path),
                leaf=run.leaf or run.run_id,
            )

    # -- not a verb ---------------------------------------------------------------------------

    def stop_if_quiescent(
        self,
        run: HeadRun,
        initiator: StopInitiator,
        *,
        expected_activity_epoch: int,
        head_process_alive: bool,
        signal_name: str = "TERM",
    ) -> StopReceipt:
        """End this head only while it is still quiet, with the check and the stop indivisible.

        Order, as on the legacy backend:

          1. the head's epoch against `expected_activity_epoch`, before anything is probed;
          2. `head_process_alive`, the caller's own launch-identity evidence: a dead process makes
             any lease stale, so it is released and the supervisor is not asked;
          3. only for a live process, the end of the turn, read from the supervisor;
          4. admission closed, then the stop; a refusal restores admission as it found it.
        """
        if not isinstance(initiator, StopInitiator):
            raise TypeError("a stop names who ended the head")
        with self._lock:
            # Rehydrate unconditionally inside this section, so step 1 compares against what the
            # head's witnesses say now, not against an empty or stale memory. One status frame
            # serves both the rehydration and step 3.
            _, probe = self._section_probe(run)
            self._rehydrate(run, probe)
            epoch = self.activity.epoch(run.run_id)
            if epoch != expected_activity_epoch:
                return StopReceipt(
                    status=HEAD_ALIVE,
                    run=run,
                    reason=STOP_ACTIVITY_SINCE,
                    evidence={"expected_epoch": expected_activity_epoch, "epoch": epoch},
                    epoch=epoch,
                )
            held = self.activity.lease(run.run_id)
            if held is not None and not head_process_alive:
                self.activity.release(run.run_id)
                held = None
            if held is not None:
                running = self._turn_still_running(run, probe)
                if running:
                    return StopReceipt(
                        status=HEAD_BUSY,
                        run=run,
                        reason=STOP_TURN_IN_FLIGHT,
                        evidence=running,
                        epoch=epoch,
                        lease=held,
                    )
                self.activity.release(run.run_id)
            admitted = self.activity.admits(run.run_id)
            self.activity.close_admission(run.run_id)
            try:
                receipt = self.stop(run, initiator, signal_name=signal_name)
            except BaseException:
                if admitted:
                    self.activity.open_admission(run.run_id)
                raise
            if not receipt.ok and admitted:
                # Nothing was stopped, so nothing was taken out of service either.
                self.activity.open_admission(run.run_id)
            return receipt

    def forget_head(self, run_id: str) -> None:
        """Drop what this runtime remembers about a head somebody else's stop has ended."""
        if not run_id:
            return
        with self._lock:
            owner = ScopedHeadLifecycle.from_run_dir(protocol.run_dir_for(self.root, run_id))
            if owner is not None:
                owner.stop_and_prove_empty()
            self.activity.forget(run_id)
            self._fatal.pop(run_id, None)
            self._admission_notes.pop(run_id, None)

    def activity_epoch(self, run: HeadRun) -> int:
        """This head's activity epoch, rehydrated under the lock `stop_if_quiescent` takes.

        For a caller that will hand the epoch back to `stop_if_quiescent` without having granted it.
        """
        with self._lock:
            self._rehydrate(run)
            return self.activity.epoch(run.run_id)

    # -- what outlived the process that knew it -------------------------------------------------

    def _rehydrate(self, run: HeadRun, probe: _Probe | None = None) -> None:
        """Recover this head's turn, epoch and admission from the supervisor, else its journal.

        Runs once in every critical section, never cached; `_Probe` keeps that to one status request
        per section. It only moves the way the head went: the epoch is raised, a turn is adopted
        (never over a held one), admission is closed (never re-opened). A self-clearing refusal
        (`connection_limit`, `attach_limit`) leaves the head as it was. An unknown state closes
        admission and adopts no lease. A positively ended head (the supervisor says it exited, or
        the launch identity says dead) closes admission and adopts no lease, so it is rotatable.
        """
        run_id = run.run_id
        if not run_id:
            return
        address = self._address(run)
        if address is None:
            return
        state = self._durable_state(address, run_id, probe)
        if state.source in (REHYDRATED_ABSENT, REHYDRATED_TRANSIENT):
            # Nothing to recover from, or a self-clearing bound: leave the head exactly as it was.
            return
        # The sequence first: even a window that cannot account for its shape states it truthfully.
        self.activity.advance_to(run_id, state.seq)
        if state.source == REHYDRATED_UNKNOWN:
            self.activity.close_admission(run_id)
            self._admission_notes.setdefault(run_id, DELIVER_STATE_UNKNOWN)
            return
        identity_ended = state.source != REHYDRATED_FROM_SUPERVISOR and self._identity_says_dead(address)
        if state.exited or identity_ended:
            # A live supervisor is the authoritative witness for its incarnation. A reused run
            # directory can still hold the previous incarnation's dead launch identity until the
            # new head writes its own; that stale record must not end the head that just answered.
            # Without a supervisor answer, the launch identity remains the positive liveness proof.
            self.activity.close_admission(run_id)
            self._admission_notes.setdefault(run_id, DELIVER_HEAD_ENDED)
            return
        if state.draining:
            self.activity.close_admission(run_id)
            self._admission_notes.setdefault(run_id, DELIVER_DRAINED_BEFORE_THIS_RUNTIME)
        if state.turn_open:
            self.activity.adopt(
                run_id,
                TurnLease(
                    lease_id=f"{run_id}:turn-{state.turn}",
                    run_id=run_id,
                    subject=ADOPTED_TURN_SUBJECT,
                    granted_at_epoch=self.activity.epoch(run_id),
                ),
            )

    def _durable_epoch(self, run: HeadRun, probe: _Probe | None = None) -> int:
        """This head's epoch, raised to the journal sequence its witnesses state.

        The only way an epoch is produced on this backend; a witness that states none leaves it
        unchanged (`advance_to` never lowers it).
        """
        address = self._address(run)
        if address is None:
            return self.activity.epoch(run.run_id)
        return self.activity.advance_to(run.run_id, self._durable_state(address, run.run_id, probe).seq)

    def _section_probe(
        self, run: HeadRun, operation: HandoffOperation | None = None
    ) -> tuple[_Address | None, _Probe | None]:
        """The section's one status request, taken at its top; `None` when there is no socket.

        A failed attempt carries one consumable recovery request into `_put` (`_Probe.spend_retry`),
        so no section makes a third. Inside a handoff `operation` the request is cut to its deadline.
        """
        address = self._address(run)
        if address is None or not address.socket_path.exists():
            return address, None
        return address, self._probe(address, operation)

    def _probe(self, address: _Address, operation: HandoffOperation | None = None) -> _Probe:
        """Ask the supervisor once for `status`: an `ok` frame, another frame, or the transport error.

        A transport error grants the section one recovery attempt (`_Probe.spend_retry`). Inside a
        handoff `operation` the request gets only what is left of its deadline and no recovery
        attempt; with nothing left it is not made at all, and the probe says so (`timed_out`).
        """
        if operation is not None and operation.remaining() <= 0:
            return _Probe(error=TimeoutError(HANDOFF_ALLOWANCE_SPENT))
        try:
            with self._connect(address, operation) as client:
                answer = client.status()
        except _UNREACHABLE as exc:
            return _Probe(error=exc, retry_available=operation is None)
        if isinstance(answer, dict) and answer.get("ok"):
            return _Probe(status=answer)
        return _Probe(answer=answer)

    def _durable_state(self, address: _Address, run_id: str, probe: _Probe | None = None) -> _DurableHead:
        """What the head itself still says about its turn, its admission and its epoch.

        The supervisor first. Its frame acts; a self-clearing refusal is `REHYDRATED_TRANSIENT` and
        the journal is not consulted behind it. Silence or an unreadable frame falls to the journal,
        where a tail that cannot account for its shape is `REHYDRATED_UNKNOWN`. `probe` reuses the
        section's status request.
        """
        if not address.socket_path.exists() and not address.journal_path.exists():
            return _DurableHead(source=REHYDRATED_ABSENT)
        if probe is None and address.socket_path.exists():
            probe = self._probe(address)
        if probe is not None:
            if probe.status is not None:
                return _supervisor_state(probe.status)
            if probe.transient:
                return _DurableHead(source=REHYDRATED_TRANSIENT)
        return _journal_state(address, run_id)

    # -- the substrate ------------------------------------------------------------------------

    def _already_up(
        self,
        claimed: str,
        run: HeadRun | None,
        *,
        spec: HeadSpec,
        workspace: str,
        task_ref: TaskRef,
        role: str,
        pid_file: str = "",
    ) -> StartReceipt | None:
        """Refuse a bring-up over a head whose launch identity is a live match; `None` otherwise.

        Decided before `_spawn`. Only a positive live match refuses: a missing, malformed or
        unreadable record proceeds, since refusing would fence the run forever, and a second
        supervisor is still caught by the run-directory lock and `_refuse_a_second_head`. The run
        directory, socket and journal are debris and never refuse. The canonical
        `root/run_id/head.pid` is read, not the run's `pid_file` (the dispatcher's watchdog
        heartbeat, cleared before each launch); a `pid_file` designated to `start` is read too, and
        a live match in either refuses.
        """
        subject = _with_pid_file(
            run
            if run is not None
            else HeadRun(
                run_id=claimed,
                spec=spec,
                workspace=workspace,
                task_ref=task_ref,
                role=role,
            ),
            "",
        )
        address = self._address(subject)
        if address is None:
            return None
        if not self._process_alive(address, subject):
            if not pid_file:
                return None
            address = replace(address, pid_file=Path(pid_file))
            if not self._process_alive(address, subject):
                return None
        return StartReceipt(
            status=HEAD_BUSY,
            run=run,
            reason=(
                f"run {claimed} already has a head up: its launch identity at "
                f"{address.pid_file} is a live match, so a bring-up here would put a second "
                "supervisor over a head that is already running"
            ),
            evidence={
                "refusal": START_HEAD_ALREADY_UP,
                "run_id": claimed,
                "pid_file": str(address.pid_file),
            },
            epoch=self.activity.epoch(claimed),
        )

    def _address(self, run: HeadRun) -> _Address | None:
        """Where this head is addressed, derived from the run id so any later tick can reach it."""
        if not run.run_id:
            return None
        try:
            run_dir = protocol.run_dir_for(self.root, run.run_id)
            socket_path = protocol.socket_path_for(run_dir)
        except protocol.ProtocolError:
            return None
        return _Address(
            run_dir=run_dir,
            socket_path=socket_path,
            journal_path=run_dir / protocol.JOURNAL_NAME,
            pid_file=Path(run.pid_file) if run.pid_file else run_dir / protocol.PID_FILE_NAME,
        )

    def _connect(
        self, address: _Address, operation: HandoffOperation | None = None
    ) -> local_pty.SupervisorClient:
        """A connection bounded by `connect_timeout`, and inside a handoff `operation` by its deadline.

        The operation's deadline goes to the client itself (`SupervisorClient(remaining=...)`), so it
        bounds the connect and then every `sendall` and every `recv` of every framed exchange on the
        connection by what is left of it at that moment; with nothing left nothing is attempted.
        """
        if operation is None:
            return local_pty.SupervisorClient.connect(address.socket_path, timeout=self._connect_timeout)
        client = local_pty.SupervisorClient.connect(
            address.socket_path, timeout=self._connect_timeout, remaining=operation.remaining
        )
        # Whatever handed the connection over, the operation's deadline is the one that bounds it.
        client.bound_by(operation.remaining)
        return client

    def _process_alive(self, address: _Address, run: HeadRun) -> bool:
        """Whether the head's process is alive, by the launch identity alone.

        The full expectation is passed when the run has one (the reader treats a partial one as
        unprovable); otherwise the record must be a live match naming this run.
        """
        expected = {"run_id": run.run_id, "role": run.role, "task": _task_of(run)}
        if all(expected.values()):
            status = self._identity(str(address.pid_file), expected=expected)
            return bool(status.get("alive")) and bool(status.get("match"))
        status = self._identity(str(address.pid_file))
        record = status.get("record") or {}
        return (
            bool(status.get("alive"))
            and bool(status.get("match"))
            and str(record.get("run_id") or "") == run.run_id
        )

    def _identity_says_dead(self, address: _Address) -> bool:
        """Whether the launch identity positively says the process has ended.

        An unreadable record is not a death, and no expectation is passed: a mismatch is not one
        either.
        """
        return bool(self._identity(str(address.pid_file)).get("state") == "dead")

    def _abandon_bring_up(
        self,
        run: HeadRun,
        report: DeliveryReport | None,
        refusal: _Refusal | None,
        epoch: int,
    ) -> StartReceipt:
        """End a head whose prompt never arrived, and say what the ending left behind.

        A fatal launch-prompt outcome closes the head first (`_close_head`), which matters when the
        stop does not confirm: the head may hold a prefix and must not be delivered to again.
        """
        detail = refusal.reason if refusal is not None else (report.detail if report else "")
        if report is not None and report.fatal:
            self._close_head(run, report)
        stopped = self.stop(
            run, StopInitiator(actor="head-launch", reason="the head was never given its task")
        )
        message = f"this head came up and its prompt did not reach it: {detail}"
        if stopped.ok:
            return StartReceipt(
                status=HEAD_GONE,
                run=stopped.run,
                reason=message,
                failure=HeadSpawnFailed(message),
                evidence=report or (refusal.evidence if refusal is not None else None),
                epoch=epoch,
            )
        return StartReceipt(
            status=HEAD_ALIVE,
            run=stopped.run,
            reason=f"{message}; the head it left behind could not be stopped",
            failure=HeadSpawnAborted(message, run=stopped.run),  # type: ignore[arg-type]  # unconfirmed stop receipts carry the run in practice; moved as-is
            evidence=report or (refusal.evidence if refusal is not None else None),
            epoch=epoch,
        )

    def _close_substrate_admission(
        self, run: HeadRun, initiator: StopInitiator, *, operation: HandoffOperation | None = None
    ) -> tuple[bool, Any, int, _Probe | None]:
        """Tell the supervisor to take no more input, and read the answer back.

        Returns `(head_signalled, evidence, journal_seq, probe)`. `head_signalled` comes from the
        read-back `status`, not from the request being sent; `journal_seq` is that frame's (0 when
        none); `probe` is that frame for the caller's rehydration, `None` when no status was read.
        """
        address = self._address(run)
        if address is None or not address.socket_path.exists():
            return False, OBSERVE_NO_RUN_DIRECTORY, 0, None
        if operation is not None and operation.remaining() <= 0:
            # Nothing is left of the handoff's allowance: no request is made, and nothing is claimed.
            return False, HANDOFF_ALLOWANCE_SPENT, 0, None
        try:
            with self._connect(address, operation) as client:
                answer = client.drain(initiator.actor or "dispatcher")
                if not answer.get("ok"):
                    return False, answer, 0, None
                status = client.status()
                if not status.get("ok"):
                    # Drain accepted, read-back declined: `head_signalled` cannot be claimed, and
                    # the refusal is the evidence.
                    return False, status, 0, _Probe(answer=status)
                return (
                    bool(status.get("draining")),
                    answer,
                    int(status.get("journal_seq") or 0),
                    _Probe(status=status),
                )
        except _UNREACHABLE as exc:
            return False, str(exc), 0, None

    @contextlib.contextmanager
    def _locked_within(self, remaining: Callable[[], float] | None) -> Iterator[bool]:
        """This runtime's lock: awaited as ever, or for at most a caller's `remaining`."""
        if remaining is None:
            with self._lock:
                yield True
            return
        locked = self._lock.acquire(timeout=max(0.0, remaining()))
        try:
            yield locked
        finally:
            if locked:
                self._lock.release()

    def _ask(
        self,
        address: _Address,
        initiator: StopInitiator,
        signal_name: str,
        remaining: Callable[[], float] | None,
    ) -> Any:
        """`_ask_to_stop`, handed a caller's deadline only when there is one."""
        if remaining is None:
            return self._ask_to_stop(address, initiator, signal_name)
        return self._ask_to_stop(address, initiator, signal_name, remaining)

    def _await_gone(self, address: _Address, run: HeadRun, remaining: Callable[[], float] | None) -> bool:
        """`_await_head_gone`, handed a caller's deadline only when there is one."""
        if remaining is None:
            return self._await_head_gone(address, run)
        return self._await_head_gone(address, run, remaining)

    def _ask_to_stop(
        self,
        address: _Address,
        initiator: StopInitiator,
        signal_name: str,
        remaining: Callable[[], float] | None = None,
    ) -> Any:
        """Ask the supervisor to end its head; a supervisor that is gone is not a failure here.

        The one reader that does not test `ok`: the answer is evidence only, and `_await_head_gone`
        decides the outcome from the launch identity. A caller's `remaining` bounds the connect and
        every framed exchange (`SupervisorClient`), so a fragmented answer cannot outlive it.
        """
        try:
            with (
                self._connect(address)
                if remaining is None
                else local_pty.SupervisorClient.connect(
                    address.socket_path, timeout=self._connect_timeout, remaining=remaining
                )
            ) as client:
                return client.stop(initiator.actor or "dispatcher", signal_name)
        except _UNREACHABLE as exc:
            return {"ok": False, "error": OBSERVE_SUPERVISOR_UNREACHABLE, "detail": str(exc)}

    def _await_head_gone(
        self, address: _Address, run: HeadRun, remaining: Callable[[], float] | None = None
    ) -> bool:
        """Wait for the head's process to be gone, by its launch identity rather than the socket.

        A head whose identity record was never written is answered by the journal's `run.exited`.
        The wait is `stop_timeout`, cut to a caller's `remaining`.
        """
        deadline = time.monotonic() + (
            self._stop_timeout if remaining is None else min(self._stop_timeout, remaining())
        )
        while True:
            if self._identity_says_dead(address):
                return True
            if not address.pid_file.exists() and _has_exited(address):
                return True
            if time.monotonic() >= deadline:
                return self._identity_says_dead(address) or (
                    not address.pid_file.exists() and _has_exited(address)
                )
            time.sleep(_CONFIRM_POLL_SECONDS)

    def _turn_still_running(self, run: HeadRun, probe: _Probe | None = None) -> str:
        """Why the turn this runtime holds a lease for is still running, or `""` once it ended.

        The supervisor is asked, through the section's `probe` when there is one. A supervisor that
        cannot be asked or read counts as running, never as permission.
        """
        address = self._address(run)
        if address is None or not address.socket_path.exists():
            return "its supervisor can no longer be addressed to ask whether the turn ended"
        if probe is None:
            probe = self._probe(address)
        if probe.error is not None:
            return f"its supervisor could not be asked whether the turn ended ({probe.error})"
        status = probe.status
        if status is None:
            return "its supervisor answered nothing this runtime can read as a turn"
        if not status.get("alive"):
            # The head's process has ended, so the turn it was running ended with it.
            return ""
        if _in_flight(status):
            return "a payload is still being written into its terminal"
        return "its supervisor still shows the turn open" if status.get("turn_open") else ""

    def _unreachable(
        self,
        run: HeadRun,
        address: _Address,
        epoch: int,
        lease: Any,
        rotatable: bool,
        exc: BaseException,
    ) -> ObserveReceipt:
        """A socket that did not answer, classified by the head's process rather than the socket.

        A dead supervisor can leave a live, orphaned head; reporting it gone would open a
        replacement beside it.
        """
        if self._identity_says_dead(address):
            # A lease on a process that no longer exists is stale.
            self.activity.release(run.run_id)
            return ObserveReceipt(
                status=HEAD_GONE,
                run=run,
                reason=OBSERVE_HEAD_EXITED,
                evidence=str(exc),
                epoch=epoch,
                lease=None,
                rotation_ready=self.activity.rotatable(run.run_id),
                handle=str(address.socket_path),
                leaf=run.leaf or run.run_id,
                connected=False,
                busy=False,
            )
        return ObserveReceipt(
            status=HEAD_ALIVE,
            run=run,
            reason=OBSERVE_SUPERVISOR_UNREACHABLE,
            evidence=str(exc),
            epoch=epoch,
            lease=lease,
            rotation_ready=rotatable,
            handle=str(address.socket_path),
            leaf=run.leaf or run.run_id,
            connected=False,
            busy=None,
        )
