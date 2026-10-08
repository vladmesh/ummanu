"""`LocalPtyHeadRuntime`: the six `HeadRuntime` verbs over a head this product owns the process of.

Stands on the local-pty substrate (`head.local_pty`): a supervisor in its own session holding the
head's pty, a Unix socket that answers without waiting for the head, and a versioned journal.
`HEAD_BUSY`, `HEAD_DRAINING`, `HEAD_ALIVE` and `HEAD_GONE` mean what they mean on every backend; the
shared contract suite runs against this one. See docs/HEAD_RUNTIME.md ("`local-pty` parity
criteria", "Supervisor progress journal") and docs/HEAD_SCOPES.md.

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
import json
import math
import os
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ummanu.runtime.head import local_pty
from ummanu.runtime.head.handoff import (
    HANDOFF_DEFERRED,
    HANDOFF_EFFECT_RESERVE_SECONDS,
    HANDOFF_NOT_STARTED,
    HANDOFF_SETTLE,
    HANDOFF_STARTED,
    HANDOFF_SUBMITTED,
    HANDOFF_TYPED,
    HANDOFF_UNKNOWN,
    HANDOFF_WAIT_BUDGET_SECONDS,
    HandoffBudget,
    HandoffOperation,
    PromptHandoff,
    active_budget,
)
from ummanu.runtime.head.identity import task_binding
from ummanu.runtime.head.local_pty import protocol
from ummanu.runtime.head.local_pty.scope_inventory import RuntimeScopeInventory
from ummanu.runtime.head.local_pty.scoped_lifecycle import ScopedHeadLifecycle
from ummanu.runtime.head.memory import MemoryScopeError
from ummanu.runtime.head.operations import (
    HeadNudgeFailed,
    HeadOperationError,
    HeadSpawnAborted,
    HeadSpawnFailed,
    HeadStopFailed,
    NudgePointer,
    post_delivery_run,
)
from ummanu.runtime.head.run import HeadRun, StopInitiator, new_run_id
from ummanu.runtime.head.runtime import (
    HEAD_ALIVE,
    HEAD_BUSY,
    HEAD_DRAINING,
    HEAD_GONE,
    HEAD_OK,
    HEAD_UNSUPPORTED,
    OBSERVE_NO_ADDRESS,
    AttachReceipt,
    DeliverReceipt,
    DrainReceipt,
    HeadActivity,
    ObserveReceipt,
    StartReceipt,
    StopReceipt,
    TurnLease,
)
from ummanu.runtime.head.spec import HeadSpec
from ummanu.runtime.head.task_ref import TaskRef
from ummanu.runtime.prompt_document import NUDGE_FILE_MODE
from ummanu.runtime.tui_delivery import (
    DELIVERY_CONFIRMED,
    READINESS_BUSY,
    READINESS_READY,
    READINESS_UNKNOWN,
    STAGE_ENTER_ACCEPTED,
    STAGE_NONE,
    STAGE_PAYLOAD_WRITTEN,
    STAGE_TURN_OBSERVED,
    DeliveryEvidence,
    DeliveryOutcome,
    payload_fingerprint,
)

#: Supervisor transport failures become unreachable-head receipts.
_UNREACHABLE = (local_pty.LocalPtyError, OSError)

#: How the launch-identity reader is called: the shape of `head_process_status`.
IdentityReader = Callable[..., Mapping[str, Any]]

#: Stable observation tokens callers route on.
#: The run directory holds nothing addressable — no socket and no journal.
OBSERVE_NO_RUN_DIRECTORY = "no_run_directory"
#: An unreachable live process is not an ended head.
OBSERVE_SUPERVISOR_UNREACHABLE = "supervisor_unreachable"
#: The head's own process has ended, by the supervisor's answer or by the launch identity.
OBSERVE_HEAD_EXITED = "head_exited"
#: The socket answered something this runtime cannot read as a status. Not an answer about the head.
OBSERVE_STATUS_UNREADABLE = "status_unreadable"

#: `_delivery_report` owns the closed delivery-outcome vocabulary.
#: All of it reached the head's terminal.
DELIVERY_ARRIVED = "arrived"
#: It ended part-way. Fatal: the prefix on the terminal cannot be taken back.
DELIVERY_LEFT_A_PREFIX = "left_a_prefix"
#: It ended and the kernel took nothing. The terminal is as it was and the head is still worth
#: delivering to.
DELIVERY_LANDED_NOTHING = "landed_nothing"
#: An unestablished fate is fatal because it may have left a terminal prefix.
DELIVERY_UNESTABLISHED = "unestablished"
#: The outcomes after which this runtime hands the head no more work.
FATAL_DELIVERY_OUTCOMES = frozenset({DELIVERY_LEFT_A_PREFIX, DELIVERY_UNESTABLISHED})
#: Not an ending, and only inside a production handoff's operation: offered or admitted, and its
#: answer or its end had not come back when the caller's allowance ran out. The substrate keeps
#: writing it; nothing is closed, and nothing is offered again until `status` and the journal say
#: what became of it.
DELIVERY_PENDING = "pending"

#: This state means no delivery fate could be established.
DELIVERY_STATE_UNKNOWN = "unknown"

#: Why a `deliver` says what it says, beside the delivery state it carries.
DELIVER_NOT_ADMITTED = "not_admitted"
DELIVER_STALLED = "delivery_stalled"
DELIVER_FAILED = "delivery_failed"
DELIVER_UNESTABLISHED = "delivery_unestablished"
DELIVER_PREFIX_IS_FATAL = "partial_delivery_closed_this_head"
DELIVER_UNKNOWN_IS_FATAL = "unestablished_delivery_closed_this_head"
#: Refusal reason after a partial delivery; it travels on every later refusal.
DRAIN_AFTER_PARTIAL_DELIVERY = (
    "a delivery reached this head's terminal in part and could not be taken back: admission is "
    "closed rather than re-opened over the fragment it left"
)
#: The same gate after a delivery whose fate could not be established.
DRAIN_AFTER_UNESTABLISHED_DELIVERY = (
    "this head was given a payload and what became of it could not be established: admission is "
    "closed rather than re-opened over bytes that may be sitting on its terminal"
)
#: What a drain on this backend really does, said once so that every receipt can carry it.
DRAIN_HEAD_SIGNALLED = (
    "the process that owns this head was told: its supervisor closed admission, wrote "
    "drain.requested into the head's journal, and refuses every later input by name"
)
DRAIN_HEAD_NOT_SIGNALLED = (
    "this runtime hands the head no more work, but its supervisor could not be told, so the head's "
    "own socket would still admit a payload from somebody else"
)

#: Substrate names re-exported so a reader outside a lifecycle (e.g. the exit status of a head whose
#: supervisor is gone) need not import the substrate; `test_local_pty_head_runtime` asserts this.
JOURNAL_NAME = protocol.JOURNAL_NAME
RUN_EXITED = local_pty.RUN_EXITED

#: Runtime grace extends, never shortens, the substrate delivery bound.
DELIVERY_GRACE_SECONDS = 5.0

#: Missing delivery bounds use the substrate default, not a runtime knob.
UNDECLARED_DELIVERY_BOUND = protocol.INPUT_DELIVERY_SECONDS

#: The submit keystroke, sent as a delivery of its own: a line and its carriage return in one burst
#: read as a paste to a TUI, and the line stays unsent in the composer.
SUBMIT_KEY = b"\r"
#: How long a prompt waits for an agent's TUI to settle before it is typed, how much silence counts
#: as settled, and how often that is asked.
PROMPT_SETTLE_SECONDS = 90.0
PROMPT_QUIET_SECONDS = 4.0
PROMPT_POLL_SECONDS = 0.25
#: A head that has printed nothing at all is waited on this long before its silence counts as
#: settled: the process may not have drawn its first frame yet.
PROMPT_FIRST_OUTPUT_SECONDS = 20.0
#: A submit started a turn when this much output follows it within this long (a taken prompt
#: redraws kilobytes; an Enter that sent nothing redraws at most a cursor).
SUBMIT_CONFIRM_BYTES = 256
SUBMIT_CONFIRM_SECONDS = 20.0
#: How many submits one prompt is given before it is reported as typed and not taken.
SUBMIT_ATTEMPTS = 2
#: Why an agent prompt did not start a turn: it is in the composer, and no submit made it go.
DELIVER_NOT_SUBMITTED = "prompt_typed_but_no_turn_started"
#: Why a production handoff was refused rather than continued: its journal cannot say how far it got.
DELIVER_HANDOFF_UNESTABLISHED = "prompt_handoff_unestablished"
#: A delivery inside a handoff operation that offered nothing: the allowance was spent, or the
#: supervisor did not answer within it. `HEAD_BUSY`, not a refusal of the head.
DELIVER_DEFERRED = "handoff_allowance_spent"
#: A delivery inside a handoff operation that was offered and had not ended within its allowance.
DELIVER_PENDING = "delivery_pending"
#: What a probe that was not made, for want of allowance, says.
HANDOFF_ALLOWANCE_SPENT = "this pass's head handoff allowance is spent"

#: Stop-if-quiescent refusal tokens, the same as the legacy backend's.
STOP_TURN_IN_FLIGHT = "turn_in_flight"
STOP_ACTIVITY_SINCE = "activity_since_expected_epoch"

#: Bring-up distinguishes in-process turns from an on-disk live head.
START_TURN_IN_FLIGHT = "turn_in_flight"
START_HEAD_ALREADY_UP = "head_already_up"

#: How long `stop` waits for a signalled head to be gone; above the supervisor's escalation grace,
#: so a head that only dies to `SIGKILL` is still seen to die.
STOP_CONFIRM_SECONDS = 10.0
_CONFIRM_POLL_SECONDS = 0.05

#: Source tokens distinguish answers, unknowns, and self-clearing refusals.
#: The supervisor answered on its socket: the live, authoritative source, and one request.
REHYDRATED_FROM_SUPERVISOR = "supervisor"
#: The supervisor is gone or unreachable and its journal answered instead, from a bounded tail.
REHYDRATED_FROM_JOURNAL = "journal"
#: No socket and no journal: a positive answer that there is no head, not an unknown.
REHYDRATED_ABSENT = "absent"
#: Debris exists and neither witness could read its state. Fail-closed: closes admission.
REHYDRATED_UNKNOWN = "unknown"
#: Self-clearing supervisor bounds are not head state and never close admission.
REHYDRATED_TRANSIENT = "transient"

#: Rehydrated admission closures retain a distinct refusal reason.
DELIVER_DRAINED_BEFORE_THIS_RUNTIME = (
    "a drain was requested for this head before this runtime existed, and the head's own "
    "supervisor or journal still says so: it takes no more work"
)
DELIVER_STATE_UNKNOWN = (
    "this head left a run directory behind and neither its supervisor nor its journal could say "
    "what state it is in: a new turn is refused rather than admitted on an unknown"
)
DELIVER_HEAD_ENDED = (
    "this head's own process has ended, by its supervisor's answer or by its launch identity: it "
    "takes no more work, and it holds no turn, so it is ready to be replaced"
)
#: Record a local drain before rehydration so later refusals name that drain.
DELIVER_DRAINED_BY_THIS_RUNTIME = (
    "a drain was requested for this head, and the head's own supervisor or journal says so: it "
    "takes no more work"
)

#: Adopted cross-tick turns have no inventable caller identity.
ADOPTED_TURN_SUBJECT = "a caller from a previous tick"


class LocalPtyRuntimeError(RuntimeError):
    """This runtime was built in a shape that could not describe a head truthfully."""


@dataclass(frozen=True)
class AttachedStream:
    """A live head's stream and the address it was joined at.

    Closing `client` is the whole of detaching; output arriving meanwhile stays in the supervisor's
    buffer.
    """

    client: local_pty.SupervisorClient
    socket_path: str
    backlog: bytes = b""
    dropped_bytes: int = 0
    total_bytes: int = 0

    def close(self) -> None:
        self.client.close()


@dataclass(frozen=True)
class DeliveryReport:
    """What one delivery did to the head's terminal: the outcome and the numbers behind it.

    `outcome` is decided once by `_delivery_report`; callers branch on it, never on byte counts.
    `state` is the substrate's state verbatim, or `unknown`. `written` is what the kernel took,
    `offered` what the caller handed over. `journalled` says the journal's `input.accepted` was
    found. `floor` is the journal sequence the delivery was offered after: delivery ids restart in
    every supervisor incarnation and the journal does not, so only records above it may answer.
    """

    outcome: str
    state: str
    written: int
    offered: int
    delivery_id: int = 0
    journalled: bool = False
    floor: int = 0
    detail: str = ""
    #: The highest journal sequence the watch saw; the epoch is raised to it. Never below `floor`.
    seq: int = 0

    @property
    def fatal(self) -> bool:
        """Whether this outcome closes the head, read from the outcome and from nothing else."""
        return self.outcome in FATAL_DELIVERY_OUTCOMES


def _delivery_report(
    *,
    state: str,
    written: int,
    offered: int,
    established: bool,
    delivery_id: int = 0,
    journalled: bool = False,
    floor: int = 0,
    detail: str = "",
    seq: int = 0,
) -> DeliveryReport:
    """Decide once what became of one delivery; the only place in this backend that decides it.

    `established` is whether a witness (`status`, or the journal record matched on the delivery id)
    said what the delivery ended as. Unwitnessed, or still `in_flight` after the derived wait (the
    substrate overran its own bound), is `DELIVERY_UNESTABLISHED`, never "nothing landed".
    """
    if not established or state == protocol.DELIVERY_IN_FLIGHT:
        return DeliveryReport(
            outcome=DELIVERY_UNESTABLISHED,
            state=DELIVERY_STATE_UNKNOWN,
            written=written,
            offered=offered,
            delivery_id=delivery_id,
            journalled=journalled,
            floor=floor,
            detail=detail,
            seq=max(seq, floor),
        )
    if state == protocol.DELIVERY_COMPLETE and written >= offered:
        outcome = DELIVERY_ARRIVED
    elif written > 0:
        outcome = DELIVERY_LEFT_A_PREFIX
    else:
        outcome = DELIVERY_LANDED_NOTHING
    return DeliveryReport(
        outcome=outcome,
        state=state,
        written=written,
        offered=offered,
        delivery_id=delivery_id,
        journalled=journalled,
        floor=floor,
        detail=detail,
        seq=max(seq, floor),
    )


class LocalPtyHeadRuntime:
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
            candidate = run or HeadRun(run_id=identity, spec=spec, workspace=workspace,
                                       task_ref=task_ref, role=role)
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
                    **({"memory_limit_mib": spec.memory_limit_mib} if spec.memory_limit_mib is not None else {}),
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

    def deliver(
        self,
        run: HeadRun,
        pointer: NudgePointer,
        *,
        subject: str = "",
        transport: Any = None,
        handoff: PromptHandoff | None = None,
        **ignored: Any,
    ) -> DeliverReceipt:
        """Put one prompt in front of a running head and say what became of the bytes.

        Before writing: `HEAD_DRAINING` (admission closed) or `HEAD_BUSY` (a turn is running);
        neither queues. After admission the payload is followed to its end and the receipt carries
        its outcome; `ok` is only `DELIVERY_ARRIVED`. A `transport` marks the pointer as an agent's
        composer prompt, typed and then submitted separately (`_deliver_prompt`); only its
        `before_send` hook is used (`_before_send`). A `handoff` makes that prompt the resumable
        production handoff (`_handoff_prompt`).
        """
        del ignored
        if transport is not None:
            if handoff is not None:
                return self._handoff_prompt(
                    run, pointer, subject or "head-nudge", _wake_hook(transport), handoff
                )
            return self._deliver_prompt(run, pointer, subject or "head-nudge", _wake_hook(transport))
        return self._deliver_payload(run, pointer, subject or "head-nudge")

    def _deliver_payload(
        self,
        run: HeadRun,
        pointer: NudgePointer,
        subject: str,
        payload: bytes | None = None,
        wake: Callable[[], Any] | None = None,
        operation: HandoffOperation | None = None,
        probe: _Probe | None = None,
    ) -> DeliverReceipt:
        """`deliver` for the pointer's line, or `payload`; `wake` runs after admission (`_before_send`).

        Inside a handoff `operation` (`_handoff_prompt`) the caller passes the status frame it has
        just read as the section's `probe`, every request is cut to the operation's deadline, `wake`
        runs only once the supervisor has answered (right before the offer), and an offer still
        unanswered or in flight when the deadline passes is `HEAD_BUSY` with `DELIVERY_PENDING`
        evidence: the lease is kept, nothing is closed, nothing is offered again.
        """
        with self._lock:
            # Rehydrate under the decision lock from the section's single status frame.
            if probe is None:
                _, probe = self._section_probe(run, operation)
            if operation is not None and probe is not None and probe.timed_out:
                # Not an answer about the head: nothing is concluded from it, nothing is offered.
                return _allowance_receipt(run, self.activity)
            self._rehydrate(run, probe)
            if not self.activity.admits(run.run_id):
                # Closed admission takes precedence over a busy turn.
                return DeliverReceipt(
                    status=HEAD_DRAINING,
                    run=run,
                    reason=(
                        self._fatal.get(run.run_id)
                        or self._admission_notes.get(run.run_id)
                        or DELIVER_NOT_ADMITTED
                    ),
                    epoch=self.activity.epoch(run.run_id),
                    lease=self.activity.lease(run.run_id),
                    rotation_ready=self.activity.rotatable(run.run_id),
                )
            held = self.activity.lease(run.run_id)
            if held is not None:
                running = self._turn_still_running(run, probe)
                if running:
                    return DeliverReceipt(
                        status=HEAD_BUSY,
                        run=run,
                        reason=(
                            f"this head is running turn {held.lease_id} for "
                            f"{held.subject or 'a caller'} ({running}): one head runs one turn, "
                            "and this delivery is not queued behind it"
                        ),
                        epoch=self.activity.epoch(run.run_id),
                        lease=held,
                    )
                self.activity.release(run.run_id)
            lease = self.activity.grant(run.run_id, subject)
            woken = [run]

            def before_offer() -> None:
                if wake is not None:
                    woken[0] = self._before_send(woken[0], wake)

            try:
                if operation is None:
                    before_offer()
                    run = woken[0]
                report, refusal = self._put(
                    run,
                    pointer,
                    subject,
                    probe,
                    payload,
                    operation=operation,
                    before_offer=before_offer if operation is not None else None,
                )
            except BaseException:
                self.activity.release(run.run_id)
                raise
            run = woken[0]
            if refusal is not None:
                # Refused at admission: nothing reached the terminal, so the turn is handed back.
                self.activity.release(run.run_id)
                return DeliverReceipt(
                    status=refusal.status,
                    run=run,
                    reason=refusal.reason,
                    failure=refusal.failure,
                    evidence=refusal.evidence,
                    epoch=self.activity.epoch(run.run_id),
                    lease=None,
                    rotation_ready=self.activity.rotatable(run.run_id),
                )
            assert report is not None
            # Only the head's journal sequence advances the epoch.
            self.activity.noted(run.run_id)
            epoch = self.activity.advance_to(run.run_id, report.seq)
            if report.outcome == DELIVERY_PENDING:
                # Admitted, or perhaps admitted, and not ended within the allowance: the lease stays
                # with the write the substrate is still making, and the caller's next pass reads its end.
                return DeliverReceipt(
                    status=HEAD_BUSY,
                    run=run,
                    reason=f"{DELIVER_PENDING}: {report.detail}",
                    evidence=report,
                    delivery_state=report.state,
                    delivered_bytes=report.written,
                    offered_bytes=report.offered,
                    epoch=epoch,
                    lease=lease,
                    rotation_ready=self.activity.rotatable(run.run_id),
                )
            if report.outcome == DELIVERY_ARRIVED:
                return DeliverReceipt(
                    status=HEAD_OK,
                    run=run.working() if run.running else run,
                    delivery=_outcome_of(run, pointer, report, subject),
                    delivery_state=report.state,
                    delivered_bytes=report.written,
                    offered_bytes=report.offered,
                    evidence=report,
                    epoch=epoch,
                    lease=lease,
                )
            return self._delivery_that_did_not_arrive(run, report, lease, epoch, operation)

    def _deliver_prompt(
        self,
        run: HeadRun,
        pointer: NudgePointer,
        subject: str,
        wake: Callable[[], Any] | None = None,
    ) -> DeliverReceipt:
        """Put an agent's prompt in front of it the way a keyboard does, and see a turn start.

        No step holds the lock across a wait:

        1. wait until the head stops printing (`_await_settled`); a head in a turn is refused
           `HEAD_BUSY` by step 2;
        2. deliver the line, which lands in the composer; `wake` runs after admission and before
           the first byte (`SIGCONT` for a retained worker, a Codex provider bind);
        3. wait until the substrate's turn over that line closes (`_await_idle`), or the submit
           would be refused by the echo's turn;
        4. deliver `SUBMIT_KEY` alone and watch for a turn's output (`_await_turn`), up to
           `SUBMIT_ATTEMPTS` times.

        `ok` and `turn_confirmed` only when a turn was seen to start; otherwise `HEAD_ALIVE` with
        `DELIVER_NOT_SUBMITTED`.
        """
        self._await_settled(run)
        typed = self._deliver_payload(run, pointer, subject, wake=wake)
        if not typed.ok or not isinstance(typed.evidence, DeliveryReport):
            return typed
        report = typed.evidence
        live = typed.run or run
        last: DeliverReceipt = typed
        submits = 0
        submitted = 0
        confirmed = False
        for _ in range(SUBMIT_ATTEMPTS):
            self._await_idle(live)
            before = self._output_bytes(live)
            last = self._deliver_payload(live, pointer, f"{subject}:submit", SUBMIT_KEY)
            if not last.ok:
                break
            submits += 1
            submitted += last.delivered_bytes
            if self._await_turn(live, before):
                confirmed = True
                break
        outcome = _outcome_of(
            live, pointer, report, subject, submits=submits, submitted=submitted, confirmed=confirmed
        )
        if confirmed:
            return DeliverReceipt(
                status=HEAD_OK,
                run=live,
                delivery=outcome,
                delivery_state=typed.delivery_state,
                delivered_bytes=typed.delivered_bytes,
                offered_bytes=typed.offered_bytes,
                evidence=report,
                epoch=last.epoch,
                lease=last.lease,
            )
        evidence = outcome.evidence
        if last.status == HEAD_BUSY:
            evidence.readiness_state = READINESS_BUSY
        reason = DELIVER_NOT_SUBMITTED if last.ok else f"{DELIVER_NOT_SUBMITTED}: {last.reason or last.status}"
        evidence.reason = reason
        return DeliverReceipt(
            status=last.status if not last.ok else HEAD_ALIVE,
            run=live,
            reason=reason,
            failure=HeadNudgeFailed(reason),
            evidence=evidence,
            delivery_state=typed.delivery_state,
            delivered_bytes=typed.delivered_bytes,
            offered_bytes=typed.offered_bytes,
            epoch=last.epoch,
            lease=last.lease,
            rotation_ready=last.rotation_ready,
        )

    def _handoff_prompt(
        self,
        run: HeadRun,
        pointer: NudgePointer,
        subject: str,
        wake: Callable[[], Any] | None,
        handoff: PromptHandoff,
    ) -> DeliverReceipt:
        """`_deliver_prompt` for the production dispatcher: resumable, inside one bounded operation.

        The authoritative boundary of a production handoff. Every supervisor request it makes, and
        every wait, runs inside one `HandoffOperation` cut from the card's share of the tick's
        `HandoffBudget` (a handoff called outside a production pass gets a private budget of the same
        size). Where the handoff stands is read, never remembered: first the supervisor's `status` (a
        line or an Enter of this handoff still being written), then the journal above
        `handoff.floor` (`_handoff_progress`). A typed line is not typed again, an accepted or
        in-flight Enter is not sent again, and a turn that took the prompt confirms it. Whatever this
        pass cannot finish answers `HEAD_BUSY` with `handoff_stage` and leaves the rest to the next.
        The outcomes this flow shares with `_deliver_prompt` are reached on the same evidence, and
        its bounds (`prompt_settle` before typing and before a submit, `SUBMIT_ATTEMPTS`, and the
        blocking flow's `submit_confirm` + `prompt_settle` for one submit's turn) are measured from
        the durable times in the journal and in `handoff`, so they survive a dispatcher restart.
        """
        budget = active_budget() or HandoffBudget(HANDOFF_WAIT_BUDGET_SECONDS, clock=self._monotonic)
        with budget.operation(subject) as operation:
            began = operation.clock()
            receipt = self._handoff_within(run, pointer, subject, wake, handoff, operation)
            operation.note(
                "handoff",
                began,
                receipt.handoff_stage and f"pending:{receipt.handoff_stage}" or receipt.status,
            )
            return receipt

    def _handoff_within(
        self,
        run: HeadRun,
        pointer: NudgePointer,
        subject: str,
        wake: Callable[[], Any] | None,
        handoff: PromptHandoff,
        operation: HandoffOperation,
    ) -> DeliverReceipt:
        live = run
        # The furthest stage this call has seen established, so a look the allowance cut short does
        # not report less than an earlier look in the same call saw.
        reached = HANDOFF_SETTLE
        while True:
            seen = self._handoff_look(live, subject, handoff.floor, operation)
            progress = seen.progress
            if progress.unknown:
                return self._handoff_unestablished(live, pointer, subject, progress.unknown)
            if progress.typed_prefix is not None:
                # A write that left part of the line on the terminal: the same fatal outcome a live
                # delivery reaches (`_delivery_that_did_not_arrive`), never a second line over it.
                report = _journalled_report(progress.typed_prefix, handoff.floor)
                return self._delivery_that_did_not_arrive(
                    live, report, None, seen.epoch(self.activity, live), operation
                )
            stage = _later_stage(_stage_of(progress), reached)
            if seen.deferred:
                return self._handoff_pending(live, pointer, subject, stage, seen.deferred, seen, progress)
            if seen.writing:
                # This handoff's own write is admitted and the substrate is still making it: wait for
                # its end within the allowance, and never offer it again.
                stage = reached = _later_stage(
                    stage, HANDOFF_SUBMITTED if seen.writing == "submit" else HANDOFF_TYPED
                )
                if self._handoff_pause(operation):
                    continue
                return self._handoff_pending(
                    live, pointer, subject, stage, "this handoff's write is still being made", seen, progress
                )
            if progress.typed is None:
                settle = self._handoff_settle(live, handoff, seen, operation)
                if settle == "again":
                    continue
                if settle == "pending":
                    return self._handoff_pending(
                        live, pointer, subject, HANDOFF_SETTLE, "the head has not been seen quiet yet", seen, progress
                    )
                if operation.remaining() < HANDOFF_EFFECT_RESERVE_SECONDS:
                    return self._handoff_pending(
                        live, pointer, subject, HANDOFF_SETTLE, HANDOFF_ALLOWANCE_SPENT, seen, progress
                    )
                began = operation.clock()
                typed = self._deliver_payload(
                    live, pointer, subject, wake=wake, operation=operation, probe=seen.probe
                )
                operation.note("type", began, typed.status if typed.ok else typed.reason.split(":")[0])
                live = typed.run or live
                if typed.reason.startswith(DELIVER_DEFERRED):
                    return self._handoff_pending(
                        live, pointer, subject, HANDOFF_SETTLE, HANDOFF_ALLOWANCE_SPENT, seen, progress
                    )
                if typed.reason.startswith(DELIVER_PENDING):
                    # Admitted and still being written, or offered and its answer lost: either way the
                    # next look reads what became of it, and nothing is offered again before that.
                    admitted = isinstance(typed.evidence, DeliveryReport) and typed.evidence.delivery_id > 0
                    return self._handoff_pending(
                        live,
                        pointer,
                        subject,
                        HANDOFF_TYPED if admitted else HANDOFF_SETTLE,
                        typed.reason.split(": ", 1)[-1],
                        seen,
                        progress,
                    )
                if not typed.ok:
                    return typed
                seen_line = self._handoff_read(live, subject, handoff.floor)
                if not seen_line.unknown and seen_line.typed is None and seen_line.typed_prefix is None:
                    # The supervisor journals `input.accepted` before it reports a delivery ended, so
                    # this is a journal the reader could not follow: say so rather than guess the stage.
                    return self._handoff_unestablished(
                        live, pointer, subject, "the typed line is missing from the head's journal"
                    )
                continue
            report = _journalled_report(progress.typed, handoff.floor)
            if progress.confirmed:
                return self._handoff_done(live, pointer, subject, report, progress, seen)
            now = self._wall()
            # Past what the blocking flow gives one submit (`_await_turn`, then `_await_idle`) the
            # next attempt decides instead, and a turn still open refuses it, as it refuses there.
            if (
                progress.submits
                and not progress.submit_finished
                and now - progress.submit_at < self._submit_confirm + self._prompt_settle
            ):
                # The latest submit's turn has not printed enough yet to say it took the prompt.
                if self._handoff_pause(operation):
                    continue
                return self._handoff_pending(
                    live,
                    pointer,
                    subject,
                    HANDOFF_SUBMITTED,
                    "the submitted prompt's turn has not shown that it took the prompt yet",
                    seen,
                    progress,
                )
            if progress.submits >= SUBMIT_ATTEMPTS:
                return self._handoff_not_submitted(live, pointer, subject, report, progress, None, seen)
            echo_open = bool(seen.status and (seen.status.get("turn_open") or _in_flight(seen.status)))
            if echo_open and now - progress.typed_at < self._prompt_settle:
                # The typed line's echo turn has not closed; an Enter now would be refused by it.
                if self._handoff_pause(operation):
                    continue
                return self._handoff_pending(
                    live, pointer, subject, stage, "the typed line's echo turn has not closed yet", seen, progress
                )
            if operation.remaining() < HANDOFF_EFFECT_RESERVE_SECONDS:
                return self._handoff_pending(live, pointer, subject, stage, HANDOFF_ALLOWANCE_SPENT, seen, progress)
            began = operation.clock()
            last = self._deliver_payload(
                live, pointer, f"{subject}:submit", SUBMIT_KEY, operation=operation, probe=seen.probe
            )
            operation.note("submit", began, last.status if last.ok else last.reason.split(":")[0])
            if last.reason.startswith(DELIVER_DEFERRED):
                return self._handoff_pending(live, pointer, subject, stage, HANDOFF_ALLOWANCE_SPENT, seen, progress)
            if last.reason.startswith(DELIVER_PENDING):
                admitted = isinstance(last.evidence, DeliveryReport) and last.evidence.delivery_id > 0
                return self._handoff_pending(
                    live,
                    pointer,
                    subject,
                    HANDOFF_SUBMITTED if admitted else stage,
                    last.reason.split(": ", 1)[-1],
                    seen,
                    progress,
                )
            if not last.ok:
                return self._handoff_not_submitted(live, pointer, subject, report, progress, last, seen)
            after = self._handoff_read(live, subject, handoff.floor)
            if not after.unknown and after.submits <= progress.submits:
                return self._handoff_unestablished(
                    live, pointer, subject, "the accepted submit is missing from the head's journal"
                )

    def _handoff_look(
        self, run: HeadRun, subject: str, floor: int, operation: HandoffOperation
    ) -> _HandoffLook:
        """One status (cut to the operation's deadline), then the journal above `floor`.

        In that order: the supervisor journals `input.accepted` before its `status` says a write
        ended, so a status that shows no write of this handoff in flight followed by a journal that
        shows no line means none was taken. And because it reads a request before it answers any
        connection made after it, an offer whose answer was lost is visible to this status.
        """
        address = self._address(run)
        probe = (
            self._probe(address, operation)
            if address is not None and address.socket_path.exists()
            else None
        )
        progress = self._handoff_read(run, subject, floor)
        status = probe.status if probe is not None else None
        deferred = ""
        if probe is not None and probe.timed_out:
            deferred = (
                HANDOFF_ALLOWANCE_SPENT
                if str(probe.error) == HANDOFF_ALLOWANCE_SPENT
                else "the head's supervisor did not answer within this pass's allowance"
            )
        elif probe is not None and probe.transient:
            deferred = "the head's supervisor is at a self-clearing bound"
        writing = ""
        if status is not None and _in_flight(status):
            delivery = status.get("delivery")
            written_for = str(delivery.get("subject") or "") if isinstance(delivery, Mapping) else ""
            writing = "line" if written_for == subject else "submit" if written_for == f"{subject}:submit" else ""
        return _HandoffLook(probe=probe, status=status, progress=progress, deferred=deferred, writing=writing)

    def _handoff_settle(
        self, run: HeadRun, handoff: PromptHandoff, seen: _HandoffLook, operation: HandoffOperation
    ) -> str:
        """Whether the head may be typed into now (`type`), after a short wait (`again`), or not yet.

        Quiet as `_await_settled` means it: `prompt_quiet` with no output (after `prompt_first_output`
        for a head that has printed nothing), or past `prompt_settle` from the handoff's opening. A
        supervisor that reports `output_idle_seconds` says how long in one frame; one started before it
        did is answered from what this runtime saw of it before (`_quiet_since`). The wait is taken
        only when the quiet is due within what is left of the allowance, never to watch a noisy head.
        A head that cannot be observed or is in a turn is not waited on: `_deliver_payload` refuses it
        by name, as the blocking flow's delivery does.
        """
        status = seen.status
        if status is None or not status.get("alive") or status.get("turn_open") or _in_flight(status):
            self._quiet_marks.pop(run.run_id, None)
            return "type"
        wall = self._wall()
        if wall - handoff.began_at >= self._prompt_settle:
            return "type"
        now = self._monotonic()
        since = self._quiet_since(run, status, now)
        quiet_at = since + self._prompt_quiet
        if _output_of_status(status) <= 0:
            quiet_at = max(quiet_at, now + (handoff.began_at + self._prompt_first_output - wall))
        if now >= quiet_at:
            return "type"
        if quiet_at - now <= operation.remaining() - HANDOFF_EFFECT_RESERVE_SECONDS:
            self._sleep(quiet_at - now)
            return "again"
        return "pending"

    def _quiet_since(self, run: HeadRun, status: Mapping[str, Any], now: float) -> float:
        """Since when (this runtime's monotonic clock) the head has printed nothing, as far as known.

        `output_idle_seconds` answers it directly. Without it, the supervisor's incarnation (its and
        the head's pids), its journal sequence and its output count are kept from the first frame
        that showed them: while a later frame shows all four unchanged the head printed nothing in
        between, since the count only grows within an incarnation and a new one writes `run.started`.
        Anything else starts the watch again from now; nothing is concluded from what is missing.
        """
        idle = status.get("output_idle_seconds")
        if isinstance(idle, (int, float)) and not isinstance(idle, bool) and math.isfinite(idle) and idle >= 0:
            return now - float(idle)
        key = (
            status.get("supervisor_pid"),
            status.get("head_pid"),
            status.get("journal_seq"),
            status.get("output_bytes"),
        )
        held = self._quiet_marks.get(run.run_id)
        if held is not None and held[0] == key:
            return held[1]
        self._quiet_marks[run.run_id] = (key, now)
        return now

    def _handoff_pause(self, operation: HandoffOperation) -> bool:
        """Wait one poll and look again; False, without waiting, when the allowance has no room for both."""
        if operation.remaining() <= self._prompt_poll:
            return False
        self._sleep(self._prompt_poll)
        return True

    def _handoff_read(self, run: HeadRun, subject: str, floor: int) -> _HandoffProgress:
        address = self._address(run)
        if address is None:
            return _HandoffProgress(unknown="the head has no address to read its journal from")
        try:
            read = local_pty.read_tail(address.journal_path)
        except (local_pty.JournalError, OSError) as exc:
            return _HandoffProgress(unknown=f"the head's journal could not be read: {exc}")
        return _handoff_progress(read, subject, floor)

    def _handoff_pending(
        self,
        run: HeadRun,
        pointer: NudgePointer,
        subject: str,
        stage: str,
        why: str,
        seen: _HandoffLook,
        progress: _HandoffProgress,
    ) -> DeliverReceipt:
        """A handoff this pass leaves for the next: `HEAD_BUSY` naming its stage, no failure.

        The evidence says what is established (a line typed, how many Enters accepted), never what
        is still being written: that is the next pass's to read.
        """
        payload_bytes, payload_hash = payload_fingerprint(pointer.text)
        report = _journalled_report(progress.typed, 0) if progress.typed is not None else None
        submits = progress.submits
        evidence = DeliveryEvidence(
            handle=run.handle,
            subject=subject,
            stage=(
                STAGE_ENTER_ACCEPTED if submits else STAGE_PAYLOAD_WRITTEN if report is not None else STAGE_NONE
            ),
            payload_bytes=payload_bytes,
            payload_sha256=payload_hash,
            delivery_mode=NUDGE_FILE_MODE if pointer.document else "",
            document_path=pointer.document,
            adapter=run.spec.adapter,
            body_write_accepted=report is not None,
            body_bytes_written=report.written if report is not None else 0,
            body_write_count=1 if report is not None else 0,
            submit_write_accepted=submits > 0,
            submit_bytes_written=submits,
            submit_count=submits,
            payload_left_in_composer=report is not None,
            reason=why,
            handoff_stage=stage,
        )
        return DeliverReceipt(
            status=HEAD_BUSY,
            run=run,
            reason=f"production handoff pending at {stage}: {why}",
            evidence=evidence,
            delivery_state=report.state if report is not None else "",
            delivered_bytes=report.written if report is not None else 0,
            offered_bytes=report.offered if report is not None else 0,
            epoch=seen.epoch(self.activity, run),
            lease=self.activity.lease(run.run_id),
            rotation_ready=self.activity.rotatable(run.run_id),
            handoff_stage=stage,
        )

    def _handoff_done(
        self,
        live: HeadRun,
        pointer: NudgePointer,
        subject: str,
        report: DeliveryReport,
        progress: _HandoffProgress,
        seen: _HandoffLook,
    ) -> DeliverReceipt:
        submits = max(progress.submits, 1)
        return DeliverReceipt(
            status=HEAD_OK,
            run=live,
            delivery=_outcome_of(
                live, pointer, report, subject, submits=submits, submitted=submits, confirmed=True
            ),
            delivery_state=report.state,
            delivered_bytes=report.written,
            offered_bytes=report.offered,
            evidence=report,
            epoch=seen.epoch(self.activity, live),
            lease=self.activity.lease(live.run_id),
        )

    def _handoff_not_submitted(
        self,
        live: HeadRun,
        pointer: NudgePointer,
        subject: str,
        report: DeliveryReport,
        progress: _HandoffProgress,
        last: DeliverReceipt | None,
        seen: _HandoffLook,
    ) -> DeliverReceipt:
        """`_deliver_prompt`'s typed-and-not-taken outcome, from the journal's count of submits."""
        outcome = _outcome_of(
            live,
            pointer,
            report,
            subject,
            submits=progress.submits,
            submitted=progress.submits,
            confirmed=False,
        )
        evidence = outcome.evidence
        if last is not None and last.status == HEAD_BUSY:
            evidence.readiness_state = READINESS_BUSY
        refused = last if last is not None and not last.ok else None
        reason = (
            f"{DELIVER_NOT_SUBMITTED}: {refused.reason or refused.status}"
            if refused is not None
            else DELIVER_NOT_SUBMITTED
        )
        evidence.reason = reason
        return DeliverReceipt(
            status=refused.status if refused is not None else HEAD_ALIVE,
            run=live,
            reason=reason,
            failure=HeadNudgeFailed(reason),
            evidence=evidence,
            delivery_state=report.state,
            delivered_bytes=report.written,
            offered_bytes=report.offered,
            epoch=seen.epoch(self.activity, live),
            lease=self.activity.lease(live.run_id),
            rotation_ready=self.activity.rotatable(live.run_id),
        )

    def _handoff_unestablished(
        self, run: HeadRun, pointer: NudgePointer, subject: str, why: str
    ) -> DeliverReceipt:
        """A handoff whose state the journal cannot establish: neither pending nor retried blind.

        Typing again could put a second line over the first, and calling it delivered would be a
        guess, so it is a typed refusal the caller's recovery owns (`HEAD_ALIVE`).
        """
        payload_bytes, payload_hash = payload_fingerprint(pointer.text)
        reason = f"{DELIVER_HANDOFF_UNESTABLISHED}: {why}"
        return DeliverReceipt(
            status=HEAD_ALIVE,
            run=run,
            reason=reason,
            failure=HeadNudgeFailed(reason),
            evidence=DeliveryEvidence(
                handle=run.handle,
                subject=subject,
                payload_bytes=payload_bytes,
                payload_sha256=payload_hash,
                document_path=pointer.document,
                adapter=run.spec.adapter,
                readiness_state=READINESS_UNKNOWN,
                reason=reason,
            ),
            delivery_state=DELIVERY_STATE_UNKNOWN,
            epoch=self.activity.epoch(run.run_id),
            lease=self.activity.lease(run.run_id),
            rotation_ready=self.activity.rotatable(run.run_id),
        )

    def _before_send(self, run: HeadRun, hook: Callable[[], Any]) -> HeadRun:
        """Run the caller's pre-send hook after admission and before `_put`, whatever the stop state.

        Each hook is harmless on a running head: `SIGCONT` for a retained worker (without it the
        bytes go to a stopped process) and a Codex `bind_before_delivery` (advisory telemetry). A
        returned `HeadRun` is merged via `post_delivery_run`; other returns are ignored. Exceptions
        propagate, and `_deliver_payload` hands back the turn.
        """
        handed = hook()
        if isinstance(handed, HeadRun):
            return post_delivery_run(run, handed)
        return run

    def _await_settled(self, run: HeadRun) -> None:
        """Wait until the head has printed nothing new for `prompt_quiet`, within `prompt_settle`.

        A head that has printed nothing yet gets `prompt_first_output` first. A head that cannot be
        observed, or is in a turn, is not waited on.
        """
        began = time.monotonic()
        deadline = began + self._prompt_settle
        printed = -1
        steady_since = began
        while True:
            seen = self.observe(run)
            if not seen.ok or seen.busy:
                return
            now = time.monotonic()
            current = _output_of(seen)
            if current != printed:
                printed, steady_since = current, now
            elif now - steady_since >= self._prompt_quiet and (
                printed > 0 or now - began >= self._prompt_first_output
            ):
                return
            if now >= deadline:
                return
            time.sleep(self._prompt_poll)

    def _await_idle(self, run: HeadRun) -> None:
        """Wait until neither the substrate's turn nor this runtime's lease holds the head."""
        deadline = time.monotonic() + self._prompt_settle
        while time.monotonic() < deadline:
            seen = self.observe(run)
            if not seen.ok or not seen.busy:
                return
            time.sleep(self._prompt_poll)

    def _await_turn(self, run: HeadRun, before: int) -> bool:
        """Whether the head printed `SUBMIT_CONFIRM_BYTES` past `before` within `submit_confirm`."""
        deadline = time.monotonic() + self._submit_confirm
        while True:
            seen = self.observe(run)
            if seen.status == HEAD_GONE:
                return False
            if seen.ok and _output_of(seen) - before >= SUBMIT_CONFIRM_BYTES:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(self._prompt_poll)

    def _output_bytes(self, run: HeadRun) -> int:
        """How much the head has printed in this supervisor's life, or 0 when it cannot be read."""
        seen = self.observe(run)
        return _output_of(seen) if seen.ok else 0

    def handoff_floor(self, run: HeadRun) -> int | None:
        """The journal sequence a production handoff opened now starts above, or None if unknowable.

        Read from the supervisor's own `status` (one request, inside the handoff operation's bound),
        so it is the sequence of a live head. A head that does not answer within the allowance, or
        does not answer at all, has no floor a handoff could safely start from: the caller opens none
        this pass and acts on nothing.
        """
        address = self._address(run)
        if address is None or not address.socket_path.exists():
            return None
        budget = active_budget() or HandoffBudget(HANDOFF_WAIT_BUDGET_SECONDS, clock=self._monotonic)
        with budget.operation("handoff-floor") as operation:
            began = operation.clock()
            probe = self._probe(address, operation)
            status = probe.status if probe.error is None else None
            operation.note("floor", began, "read" if status is not None else "deferred" if probe.timed_out else "none")
        if not isinstance(status, Mapping) or not status.get("alive"):
            return None
        seq = status.get("journal_seq")
        return seq if isinstance(seq, int) and not isinstance(seq, bool) and seq >= 0 else None

    def handoff_started(self, run: HeadRun, subject: str, handoff: PromptHandoff) -> str:
        """Whether this handoff has written to the head: `none`, `started`, `unknown` or `deferred`.

        `started` once any of its line is on the terminal (whole or not) by the journal above the
        floor, or once the supervisor's `status` shows its line or its Enter still being written.
        Asked in that order's reverse, status first (`_handoff_look`). `unknown` when the journal
        cannot say, never read as `none`; `deferred` when the supervisor did not answer within the
        allowance, so nothing may be concluded this pass. A supervisor that is gone has nothing in
        flight, so the journal alone answers for it.
        """
        budget = active_budget() or HandoffBudget(HANDOFF_WAIT_BUDGET_SECONDS, clock=self._monotonic)
        with budget.operation(subject) as operation:
            began = operation.clock()
            seen = self._handoff_look(run, subject, handoff.floor, operation)
            progress = seen.progress
            if progress.unknown:
                answer = HANDOFF_UNKNOWN
            elif progress.typed is not None or progress.typed_prefix is not None or seen.writing:
                answer = HANDOFF_STARTED
            elif seen.deferred:
                answer = HANDOFF_DEFERRED
            elif seen.probe is not None and seen.status is None and seen.probe.error is None:
                # It answered something that is not a status: whether a write is in flight is unknown.
                answer = HANDOFF_UNKNOWN
            else:
                answer = HANDOFF_NOT_STARTED
            operation.note("started", began, answer)
        return answer

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
                    status=HEAD_ALIVE, run=finishing, reason=reason,
                    failure=HeadStopFailed(reason, run=finishing),
                    epoch=self.activity.epoch(run.run_id), lease=self.activity.lease(run.run_id),
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
                            status = self._identity(str(address.pid_file), expected={
                                "run_id": run.run_id, "role": run.role, "task": _task_of(run),
                            })
                            if status.get("state") not in ("dead", "live-match") or (status.get("record") or {}).get("run_id") != run.run_id:
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
                    status=HEAD_ALIVE, run=finishing, reason=str(exc),
                    failure=HeadStopFailed(str(exc), run=finishing), evidence=asked,
                    epoch=self.activity.epoch(run.run_id),
                    lease=self.activity.lease(run.run_id), rotation_ready=False,
                )
            if not gone:
                return StopReceipt(
                    status=HEAD_ALIVE,
                    run=finishing,
                    reason=(
                        f"this head was asked to stop and its process was still there "
                        f"{self._stop_timeout:g}s later"
                        if remaining is None else
                        "this head was asked to stop and its process was still there when the "
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
            return StopReceipt(status=HEAD_OK, run=finishing if finishing.settled else finishing.exited(), evidence=asked, epoch=epoch)

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
        identity_ended = (
            state.source != REHYDRATED_FROM_SUPERVISOR and self._identity_says_dead(address)
        )
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

    def _put(
        self,
        run: HeadRun,
        pointer: NudgePointer,
        subject: str,
        probe: _Probe | None = None,
        payload: bytes | None = None,
        operation: HandoffOperation | None = None,
        before_offer: Callable[[], None] | None = None,
    ) -> tuple[DeliveryReport | None, _Refusal | None]:
        """Offer one payload and follow it until this backend can say what became of it.

        Exactly one of the pair is not `None`. Until the request goes onto the socket, an unreachable
        supervisor or a stated refusal is a `_Refusal` (the terminal was never touched); after that,
        every ending is a `DeliveryReport`. `probe` is the section's status request: an `ok` frame
        is reused, a failed one permits one recovery request. That frame supplies either a stated
        refusal or the journal `floor` for matching this delivery's `input.accepted`.

        Inside a handoff `operation` every request is cut to its deadline. A connection that does not
        answer in time is a `HEAD_BUSY` refusal naming the allowance (nothing was offered); an offer
        whose answer, or an admitted payload whose end, does not come back in time is
        `DELIVERY_PENDING` (`_follow`), never the fatal unestablished outcome. `before_offer` runs
        once the supervisor has answered, right before the offer.
        """
        address = self._address(run)
        if address is None or not address.socket_path.exists():
            return None, _Refusal(
                status=HEAD_GONE,
                reason="this head has no socket to deliver through",
                failure=HeadNudgeFailed("the head's supervisor can no longer be addressed"),
            )
        if probe is not None and probe.status is None and isinstance(probe.answer, Mapping):
            # Refused in this section's status frame, before the offer: a refusal (`HEAD_BUSY` at
            # the connection bound); nothing is closed or remembered as fatal.
            return None, _stated_refusal(probe.answer)
        if payload is None:
            payload = _payload_of(pointer)
        if operation is not None and operation.remaining() <= 0:
            return None, _allowance_refusal()
        try:
            client = self._connect(address, operation)
        except _UNREACHABLE as exc:
            if operation is not None and _timed_out(exc):
                return None, _allowance_refusal()
            return None, self._unreachable_refusal(address, run, exc)
        with client:
            # The pre-offer journal sequence floors `input.accepted` matching, since delivery ids
            # restart per incarnation. The section's frame is reused; failing here is a refusal.
            if probe is not None and probe.status is not None:
                status: Mapping[str, Any] = probe.status
            else:
                if probe is not None and not probe.spend_retry():
                    assert probe.error is not None
                    return None, self._unreachable_refusal(address, run, probe.error)
                try:
                    status = client.status()
                except _UNREACHABLE as exc:
                    if operation is not None and _timed_out(exc):
                        return None, _allowance_refusal()
                    return None, self._unreachable_refusal(address, run, exc)
            if not status.get("ok"):
                # A stated refusal (at the connection bound the supervisor reads no request): the
                # payload was never offered. `ok` is tested before any contents are believed.
                return None, _stated_refusal(status)
            floor = int(status.get("journal_seq") or 0)
            if before_offer is not None:
                before_offer()
            try:
                answer = client.send_input(payload, subject=subject)
            except _UNREACHABLE as exc:
                if operation is not None and _timed_out(exc):
                    # The offer is on the socket and its answer did not come back in time: it may be
                    # admitted. Not fatal and never offered again on this alone: the supervisor reads
                    # a request before it answers any connection made after it, so the next `status`
                    # and the journal say whether it was taken.
                    return _pending_report(len(payload), floor, "the admission answer did not come back "
                                           "within this pass's allowance"), None
                # The request is on the socket and no answer came back: whether it was admitted
                # cannot be established, so this is unestablished, not a refusal. The journal is not
                # asked, since no delivery id came back to match a record on.
                return _delivery_report(
                    state=DELIVERY_STATE_UNKNOWN,
                    written=0,
                    offered=len(payload),
                    established=False,
                    floor=floor,
                    detail=f"the head's supervisor stopped answering as it was offered: {exc}",
                ), None
            if not answer.get("ok"):
                return None, _admission_refusal(answer)
            admitted = dict(answer.get("delivery") or {})
            if operation is not None:
                return self._follow(address, client, admitted, len(payload), floor, operation), None
            # From here the answer bound is the derived delivery wait, not `connect_timeout`: a
            # slow supervisor inside its declared bound must not read as silent, which is fatal.
            client.set_timeout(self.delivery_wait_for(_declared_bound(admitted)))
            return self._follow(address, client, admitted, len(payload), floor), None

    def _follow(
        self,
        address: _Address,
        client: local_pty.SupervisorClient,
        admitted: Mapping[str, Any],
        offered: int,
        floor: int,
        operation: HandoffOperation | None = None,
    ) -> DeliveryReport:
        """Watch an admitted delivery to its end; the only place that asks what a delivery did.

        The deadline is `delivery_wait_for` the bound the supervisor declared on this delivery,
        measured after admission, so it always outlasts the substrate. Polled here rather than via
        `wait_for_delivery` so that every ending is a value, never a state read off an exception.

        Inside a handoff `operation` the watch stops at the operation's deadline instead, and each
        request is cut to it: a delivery still in flight then, or a status answer that did not come
        back in time, is `DELIVERY_PENDING`. The substrate's own bound still ends the delivery.
        """
        if operation is not None:
            return self._follow_within(address, client, admitted, offered, floor, operation)
        delivery_id = int(admitted.get("id") or 0)
        last: Mapping[str, Any] = admitted
        # The highest journal sequence seen, from the pre-offer one; the delivery's epoch.
        seq = floor
        deadline = time.monotonic() + self.delivery_wait_for(_declared_bound(admitted))
        while True:
            try:
                status = client.status()
            except _UNREACHABLE as exc:
                # Admitted, then nobody left to ask: the journal decides, else unestablished.
                return self._report_of(
                    address, last, offered, floor, established=False, detail=str(exc), seq=seq
                )
            if not status.get("ok"):
                if _is_transient_bound(status) and time.monotonic() < deadline:
                    # A self-clearing bound is not an ending: ask again within the deadline.
                    time.sleep(self._delivery_poll)
                    continue
                # A declining frame says nothing about this delivery; the journal is the witness left.
                return self._report_of(
                    address,
                    last,
                    offered,
                    floor,
                    established=False,
                    detail=_refusal_detail(status),
                    seq=seq,
                )
            seq = max(seq, int(status.get("journal_seq") or 0))
            delivery = status.get("delivery")
            if isinstance(delivery, dict) and int(delivery.get("id") or 0) == delivery_id:
                last = delivery
                if delivery.get("state") != protocol.DELIVERY_IN_FLIGHT:
                    return self._report_of(address, last, offered, floor, established=True, seq=seq)
            if time.monotonic() >= deadline:
                # The substrate overran its own bound without ending the delivery: unestablished
                # unless the journal has it, and fatal, since bytes may be on the terminal.
                return self._report_of(
                    address,
                    last,
                    offered,
                    floor,
                    established=False,
                    seq=seq,
                    detail=(
                        f"the substrate bounded this delivery at "
                        f"{_declared_bound(admitted):g}s and had not ended it "
                        f"{self.delivery_wait_for(_declared_bound(admitted)):g}s later"
                    ),
                )
            time.sleep(self._delivery_poll)

    def _follow_within(
        self,
        address: _Address,
        client: local_pty.SupervisorClient,
        admitted: Mapping[str, Any],
        offered: int,
        floor: int,
        operation: HandoffOperation,
    ) -> DeliveryReport:
        """`_follow` inside a handoff operation: the same endings, or pending when its time is up."""
        delivery_id = int(admitted.get("id") or 0)
        last: Mapping[str, Any] = admitted
        seq = floor
        while True:
            left = operation.remaining()
            if left <= 0:
                return _pending_report(offered, floor, "admitted, and still being written when this "
                                       "pass's allowance ran out", last, seq)
            try:
                status = client.status()
            except _UNREACHABLE as exc:
                if _timed_out(exc):
                    return _pending_report(offered, floor, "admitted, and the supervisor's answer did "
                                           "not come back within this pass's allowance", last, seq)
                # Admitted, then nobody left to ask: the journal decides, else unestablished.
                return self._report_of(
                    address, last, offered, floor, established=False, detail=str(exc), seq=seq
                )
            if not status.get("ok"):
                if _is_transient_bound(status):
                    # A self-clearing bound is not an ending: ask again while the allowance lasts.
                    self._sleep(min(self._delivery_poll, operation.remaining()))
                    continue
                return self._report_of(
                    address,
                    last,
                    offered,
                    floor,
                    established=False,
                    detail=_refusal_detail(status),
                    seq=seq,
                )
            seq = max(seq, int(status.get("journal_seq") or 0))
            delivery = status.get("delivery")
            if isinstance(delivery, dict) and int(delivery.get("id") or 0) == delivery_id:
                last = delivery
                if delivery.get("state") != protocol.DELIVERY_IN_FLIGHT:
                    return self._report_of(address, last, offered, floor, established=True, seq=seq)
            self._sleep(min(self._delivery_poll, operation.remaining()))

    def _report_of(
        self,
        address: _Address,
        delivery: Mapping[str, Any],
        offered: int,
        floor: int,
        *,
        established: bool,
        detail: str = "",
        seq: int = 0,
    ) -> DeliveryReport:
        """What reached the head's terminal, from `status`, corroborated by the journal.

        A journalled delivery is established however the socket ended.
        """
        state = str(delivery.get("state") or protocol.DELIVERY_IN_FLIGHT)
        written = int(delivery.get("written_bytes") or 0)
        delivery_id = int(delivery.get("id") or 0)
        journalled = False
        for event in self._accepted_records(address, floor):
            if int(event.get("delivery") or 0) != delivery_id:
                continue
            journalled = True
            seq = max(seq, int(event.get("seq") or 0))
            # The same count `status` reports; the record is written when the delivery ends.
            written = int(event.get("bytes") or written)
            state = str(event.get("state") or state)
        return _delivery_report(
            state=state,
            written=written,
            offered=int(delivery.get("size_bytes") or offered),
            established=established or journalled,
            delivery_id=delivery_id,
            journalled=journalled,
            floor=floor,
            detail=str(delivery.get("detail") or "") or detail,
            seq=seq,
        )

    def _accepted_records(self, address: _Address, floor: int) -> tuple[dict[str, Any], ...]:
        """The journal's `input.accepted` records above `floor`; empty when it cannot be read.

        The floor keeps an earlier incarnation's record with the same delivery id from answering.
        """
        try:
            events = local_pty.read_tail(address.journal_path).of_kind(local_pty.INPUT_ACCEPTED)
        except OSError:
            return ()
        return tuple(event for event in events if int(event.get("seq") or 0) > floor)

    def _unreachable_refusal(self, address: _Address, run: HeadRun, exc: BaseException) -> _Refusal:
        """A payload that was never admitted, because the supervisor could not be spoken to."""
        return _Refusal(
            status=HEAD_ALIVE if self._process_alive(address, run) else HEAD_GONE,
            reason=OBSERVE_SUPERVISOR_UNREACHABLE,
            failure=HeadNudgeFailed(f"the head's supervisor could not be reached: {exc}"),
            evidence=str(exc),
        )

    def _delivery_that_did_not_arrive(
        self,
        run: HeadRun,
        report: DeliveryReport,
        lease: Any,
        epoch: int,
        operation: HandoffOperation | None = None,
    ) -> DeliverReceipt:
        """A delivery that ended without arriving whole, keyed on `report.outcome` alone.

        `DELIVERY_LEFT_A_PREFIX` and `DELIVERY_UNESTABLISHED` close the head (`_close_head`) and
        keep the lease, since the head may be working on what it got. `DELIVERY_LANDED_NOTHING`
        hands back the lease only.
        """
        if report.fatal:
            self._close_head(run, report, operation)
        elif report.outcome == DELIVERY_LANDED_NOTHING:
            self.activity.release(run.run_id)
            lease = None
        return DeliverReceipt(
            status=self._status_of(run, report),
            run=run,
            reason=self._reason_of(report),
            failure=HeadNudgeFailed(report.detail or report.outcome),
            evidence=report,
            delivery_state=report.state,
            delivered_bytes=report.written,
            offered_bytes=report.offered,
            epoch=epoch,
            lease=lease,
            rotation_ready=self.activity.rotatable(run.run_id),
        )

    def _close_head(
        self, run: HeadRun, report: DeliveryReport, operation: HandoffOperation | None = None
    ) -> None:
        """Hand this head no more work, here and at its supervisor, remembering the reason.

        The single place for every fatal outcome, from `deliver` and `_abandon_bring_up` alike.
        Inside a handoff `operation` the supervisor is told only within what is left of it, and not
        at all once nothing is: the head is closed here at once either way, and what the supervisor
        was not told is owed by the durable evidence that made the outcome fatal (a partial line in
        the journal, which every later pass of the handoff refuses again and tells the supervisor
        again from its own allowance) and by the caller's confirmed stop before any replacement.
        """
        self._fatal[run.run_id] = (
            DRAIN_AFTER_PARTIAL_DELIVERY
            if report.outcome == DELIVERY_LEFT_A_PREFIX
            else DRAIN_AFTER_UNESTABLISHED_DELIVERY
        )
        self.activity.close_admission(run.run_id)
        self._close_substrate_admission(
            run,
            StopInitiator(
                actor="local-pty-runtime",
                reason=(
                    DELIVER_PREFIX_IS_FATAL
                    if report.outcome == DELIVERY_LEFT_A_PREFIX
                    else DELIVER_UNKNOWN_IS_FATAL
                ),
            ),
            **({} if operation is None else {"operation": operation}),
        )

    def _status_of(self, run: HeadRun, report: DeliveryReport) -> str:
        """`HEAD_GONE` only for a head established to have ended; `HEAD_ALIVE` for the rest.

        A failed delivery means the terminal closed under it. For an unestablished one the launch
        identity is asked, and anything but a positive death is `HEAD_ALIVE`.
        """
        if report.state == protocol.DELIVERY_FAILED:
            return HEAD_GONE
        if report.outcome == DELIVERY_UNESTABLISHED:
            address = self._address(run)
            if address is not None and self._identity_says_dead(address):
                return HEAD_GONE
        return HEAD_ALIVE

    def _reason_of(self, report: DeliveryReport) -> str:
        """Why this delivery says what it says: the outcome first, the substrate's state after."""
        reason = (
            DELIVER_UNESTABLISHED
            if report.outcome == DELIVERY_UNESTABLISHED
            else {
                protocol.DELIVERY_STALLED: DELIVER_STALLED,
                protocol.DELIVERY_FAILED: DELIVER_FAILED,
            }.get(report.state, DELIVER_STALLED)
        )
        if report.outcome == DELIVERY_LEFT_A_PREFIX:
            return f"{reason}: {DRAIN_AFTER_PARTIAL_DELIVERY}"
        if report.outcome == DELIVERY_UNESTABLISHED:
            return f"{reason}: {DRAIN_AFTER_UNESTABLISHED_DELIVERY}"
        return reason

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

    def _ask(self, address: _Address, initiator: StopInitiator, signal_name: str,
             remaining: Callable[[], float] | None) -> Any:
        """`_ask_to_stop`, handed a caller's deadline only when there is one."""
        if remaining is None:
            return self._ask_to_stop(address, initiator, signal_name)
        return self._ask_to_stop(address, initiator, signal_name, remaining)

    def _await_gone(self, address: _Address, run: HeadRun, remaining: Callable[[], float] | None) -> bool:
        """`_await_head_gone`, handed a caller's deadline only when there is one."""
        if remaining is None:
            return self._await_head_gone(address, run)
        return self._await_head_gone(address, run, remaining)

    def _ask_to_stop(self, address: _Address, initiator: StopInitiator, signal_name: str,
                     remaining: Callable[[], float] | None = None) -> Any:
        """Ask the supervisor to end its head; a supervisor that is gone is not a failure here.

        The one reader that does not test `ok`: the answer is evidence only, and `_await_head_gone`
        decides the outcome from the launch identity. A caller's `remaining` bounds the connect and
        every framed exchange (`SupervisorClient`), so a fragmented answer cannot outlive it.
        """
        try:
            with (self._connect(address) if remaining is None else local_pty.SupervisorClient.connect(
                address.socket_path, timeout=self._connect_timeout, remaining=remaining
            )) as client:
                return client.stop(initiator.actor or "dispatcher", signal_name)
        except _UNREACHABLE as exc:
            return {"ok": False, "error": OBSERVE_SUPERVISOR_UNREACHABLE, "detail": str(exc)}

    def _await_head_gone(self, address: _Address, run: HeadRun,
                         remaining: Callable[[], float] | None = None) -> bool:
        """Wait for the head's process to be gone, by its launch identity rather than the socket.

        A head whose identity record was never written is answered by the journal's `run.exited`.
        The wait is `stop_timeout`, cut to a caller's `remaining`.
        """
        deadline = time.monotonic() + (self._stop_timeout if remaining is None
                                       else min(self._stop_timeout, remaining()))
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


@dataclass(frozen=True)
class _DurableHead:
    """What outlived the process that granted this head's turn, as one value.

    `source` says which witness answered, so "not draining" and "unknown" never look alike. `seq`
    is the journal sequence the activity epoch is raised to.
    """

    source: str
    seq: int = 0
    draining: bool = False
    turn_open: bool = False
    turn: int = 0
    exited: bool = False


def _journal_state(address: _Address, run_id: str) -> _DurableHead:
    """This head's shape from a bounded tail of its journal, replayed in sequence order.

    A replay, not a newest-record search: `turn.started` then `run.exited` is an ended turn, not an
    open one, and `run.started` resets the replay. A window with no record of this run, or one that
    cannot account for itself (torn last record, unreadable records, unordered sequence, begun
    mid-history), is `REHYDRATED_UNKNOWN`, still carrying its last sequence.
    """
    try:
        result = local_pty.read_tail(address.journal_path)
    except OSError:
        return _DurableHead(source=REHYDRATED_UNKNOWN)
    events = [event for event in result.events if str(event.get("run_id") or "") == run_id]
    if not events:
        return _DurableHead(source=REHYDRATED_UNKNOWN)
    seq = int(events[-1].get("seq") or 0)
    if result.truncated_tail or result.malformed or result.partial_head or not result.ordered:
        return _DurableHead(source=REHYDRATED_UNKNOWN, seq=seq)
    replay = _replay_journal(events)
    return _DurableHead(
        source=REHYDRATED_FROM_JOURNAL,
        seq=seq,
        draining=replay.draining,
        turn_open=replay.turn_open,
        turn=replay.turn,
        exited=replay.exited,
    )


@dataclass(frozen=True)
class _JournalReplay:
    """One run's records folded in sequence order: the head's shape as of the last of them.

    The time fields serve the vitality reading (`head_run_turn_reading`) from the same fold. Times
    are the journal's `at`; `times_valid` is false when a needed one was unusable (the admission
    reader ignores it, the vitality reader refuses the window).
    """

    draining: bool = False
    turn_open: bool = False
    turn: int = 0
    exited: bool = False
    # A `turn.started` or `turn.finished` of the current incarnation was seen: a turn exists.
    turn_seen: bool = False
    # Any record that fixes the turn state by itself (`run.started`, `turn.*`, `run.exited`) was
    # seen. A window that began mid-history can say the turn state only if this is true.
    anchored: bool = False
    turn_started_at: float = 0.0
    turn_finished_at: float = 0.0
    input_at: float = 0.0
    progress_seq: int = 0
    progress_at: float = 0.0
    times_valid: bool = True


def _replay_journal(events: Any) -> _JournalReplay:
    """Fold one run's usable records, never raising on a value a record should not hold."""
    replay = _JournalReplay()
    for event in events:
        kind = str(event.get("kind") or "")
        if kind == local_pty.RUN_STARTED:
            # A new incarnation: nothing an older one wrote, its times included, answers for it.
            replay = _JournalReplay(anchored=True)
        elif kind == local_pty.TURN_STARTED:
            at = _journal_time(event.get("at"))
            replay = replace(
                replay,
                turn_open=True,
                turn=_journal_count(event.get("turn"), replay.turn),
                turn_seen=True,
                anchored=True,
                turn_started_at=at or 0.0,
                times_valid=replay.times_valid and at is not None,
            )
        elif kind == local_pty.TURN_FINISHED:
            at = _journal_time(event.get("at"))
            replay = replace(
                replay,
                turn_open=False,
                turn_seen=True,
                anchored=True,
                turn_finished_at=at or 0.0,
                times_valid=replay.times_valid and at is not None,
            )
        elif kind == local_pty.INPUT_ACCEPTED:
            at = _journal_time(event.get("at"))
            replay = replace(replay, input_at=at or 0.0, times_valid=replay.times_valid and at is not None)
        elif kind == local_pty.PROVIDER_PROGRESSED:
            at = _journal_time(event.get("at"))
            replay = replace(
                replay,
                progress_seq=_journal_count(event.get("seq"), 0),
                progress_at=at or 0.0,
                times_valid=replay.times_valid and at is not None,
            )
        elif kind in (local_pty.DRAIN_REQUESTED, local_pty.RUN_STOPPING):
            replay = replace(replay, draining=True)
        elif kind == local_pty.RUN_EXITED:
            replay = replace(replay, exited=True, turn_open=False, anchored=True)
    return replay


#: The largest sequence or turn number a journal reading hands on. The writer counts from 1 in
#: steps of one, so nothing real comes near it; a record that claims more is damaged, and a bound
#: keeps a hostile `10**400` from reaching a cursor that is compared as text.
_JOURNAL_COUNT_LIMIT = 2**53


def _journal_count(value: Any, fallback: int) -> int:
    """A positive record count (`seq`, `turn`) as the writer wrote it, else `fallback`."""
    if type(value) is not int or not 0 < value <= _JOURNAL_COUNT_LIMIT:
        return fallback
    return value


def _journal_time(value: Any) -> float | None:
    """A record's `at` as epoch seconds, or `None` for anything the writer could not have written."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        at = float(value)
    except OverflowError:
        return None
    if not math.isfinite(at) or at <= 0:
        return None
    return at


def head_run_turn_reading(
    root: str | os.PathLike[str], run_id: str, *, max_bytes: int = local_pty.JOURNAL_TAIL_BYTES
) -> dict[str, Any]:
    """Whether this run's head is in a turn, and since when, from its supervisor's journal.

    The vitality reading, over the same bounded tail and fold as `_journal_state`:

      * `turn` is `active` while the last `turn.started` has no `turn.finished` after it, else
        `idle`;
      * `turn_since` is the open turn's `turn.started`, or for an idle head the latest of
        `turn.finished`, `input.accepted` and `turn.started`;
      * `progress_seq`/`progress_at` are the last `provider.progressed` in the window, 0 if none.

    Never raises: an unreadable journal, no record of this run, a torn last line, any line
    `_strict_record` refuses, an exited head, or no turn yet all return
    `{"state": "unavailable", "reason": ...}`, read by callers as no answer, not a stopped head.
    The window is read raw (`tail_window`) so no coerced value becomes stall evidence. A window
    begun mid-history answers when it holds a turn boundary (`turn.*`, `run.started`,
    `run.exited`), since the turn state depends on nothing older.
    """
    try:
        return _turn_reading(
            protocol.run_dir_for(root, run_id) / protocol.JOURNAL_NAME, str(run_id), max_bytes
        )
    except Exception as exc:  # noqa: BLE001 - the source's one guard: a failed read is no answer
        return _turn_unavailable(f"the supervisor journal could not be read ({type(exc).__name__})")


def head_run_screen_lines(
    root: str | os.PathLike[str], run_id: str, *, timeout: float = 2.0
) -> dict[str, Any]:
    """This run's head screen as text lines, rendered from its supervisor's output buffer.

    Read-only (one `status` and one `output` request), rendered with the supervisor's `ScreenModel`
    at the head's terminal size. Every failure returns `{"state": "unavailable"}`.
    """
    from ummanu.runtime.head.local_pty.screen import ScreenModel

    try:
        socket_path = protocol.run_dir_for(root, run_id) / protocol.SOCKET_NAME
        with local_pty.SupervisorClient.connect(socket_path, timeout=timeout) as client:
            status = client.status()
            output = client.read_output()
    except Exception as exc:  # noqa: BLE001 - a screen nobody can read is no answer, never an error
        return _turn_unavailable(f"the head's screen could not be read ({type(exc).__name__})")
    if not output.get("ok"):
        return _turn_unavailable("the head's supervisor did not hand out its output")
    screen = ScreenModel(int(status.get("rows") or 24), int(status.get("cols") or 80))
    screen.feed(bytes(output.get("bytes_data") or b""))
    return {"state": "observed", "run_id": run_id, "lines": screen.lines()}


def _turn_reading(path: Path, run_id: str, max_bytes: int) -> dict[str, Any]:
    window = local_pty.tail_window(path, max_bytes=max_bytes)
    if window is None:
        return _turn_unavailable("the supervisor journal holds no record of this run")
    raw, partial_head = window
    if raw and not raw.endswith(b"\n"):
        return _turn_unavailable("the supervisor journal's last line is torn")
    events: list[dict[str, Any]] = []
    for number, line in enumerate(raw.split(b"\n")[:-1] if raw else (), start=1):
        refusal = _strict_record(line, run_id, events[-1] if events else None)
        if isinstance(refusal, str):
            return _turn_unavailable(f"supervisor journal line {number} of the window {refusal}")
        events.append(refusal)
    if not events:
        return _turn_unavailable("the supervisor journal holds no record of this run")
    replay = _replay_journal(events)
    if partial_head and not replay.anchored:
        return _turn_unavailable(
            "the supervisor journal window begins mid-history and holds no turn boundary"
        )
    if not replay.times_valid:
        return _turn_unavailable("a supervisor journal record carries no valid time")
    if replay.exited:
        return _turn_unavailable("the supervisor journal says the head exited")
    if not replay.turn_seen:
        return _turn_unavailable("no turn has started in this run yet")
    since = (
        replay.turn_started_at
        if replay.turn_open
        else max(replay.turn_finished_at, replay.input_at, replay.turn_started_at)
    )
    return {
        "state": "observed",
        "run_id": run_id,
        "turn": "active" if replay.turn_open else "idle",
        "turn_since": since,
        "turn_number": replay.turn,
        "progress_seq": replay.progress_seq,
        "progress_at": replay.progress_at,
        "seq": events[-1]["seq"],
    }


#: The epoch-seconds range a record's `at` may hold: 2000-01-01 to 2100-01-01. The writer stamps
#: `time.time()`, so anything outside is damage, not an early or late clock.
_JOURNAL_EPOCH_MIN = 946_684_800.0
_JOURNAL_EPOCH_MAX = 4_102_444_800.0
#: Kinds whose writer always stamps the turn number (`supervisor.py`); on them `turn` is required.
_TURN_NUMBERED = frozenset({local_pty.TURN_STARTED, local_pty.TURN_FINISHED, local_pty.PROVIDER_PROGRESSED})


def _strict_int(value: Any) -> bool:
    """A JSON integer in `(0, 2**63)`: never a bool, float or string that could be coerced to one."""
    return type(value) is int and 0 < value < 2**63


def _strict_record(line: bytes, run_id: str, previous: dict[str, Any] | None) -> dict[str, Any] | str:
    """One window line as the record its writer wrote, or why it is not one.

    Never coerces. Requires `schema_version` exactly 1, a known `kind`, this run's `run_id`, `seq`
    a JSON integer in `(0, 2**63)` above the previous line's, `at` finite in `[2000, 2100)`, and
    `turn` an integer in `(0, 2**63)` wherever present (required on `_TURN_NUMBERED` kinds). Any
    failure refuses the whole window.
    """
    try:
        record = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return "is not a JSON record"
    if not isinstance(record, dict):
        return "is not a JSON object"
    if (
        type(record.get("schema_version")) is not int
        or record.get("schema_version") != local_pty.JOURNAL_SCHEMA_VERSION
    ):
        return "has an unsupported schema_version"
    kind = record.get("kind")
    if not isinstance(kind, str) or kind not in local_pty.EVENT_KINDS:
        return "has no known kind"
    if not isinstance(record.get("run_id"), str):
        return "has no run_id string"
    if record["run_id"] != run_id:
        return "names another run"
    seq = record.get("seq")
    if not _strict_int(seq):
        return "has a seq that is not a positive JSON integer"
    if previous is not None and seq <= previous["seq"]:
        return "is out of sequence order"
    at = record.get("at")
    if isinstance(at, bool) or not isinstance(at, (int, float)):
        return "has a time that is not a number"
    try:
        at_seconds = float(at)
    except OverflowError:
        return "has a time that is not finite"
    if not math.isfinite(at_seconds) or not _JOURNAL_EPOCH_MIN <= at_seconds < _JOURNAL_EPOCH_MAX:
        return "has a time outside the epoch range"
    if ("turn" in record or kind in _TURN_NUMBERED) and not _strict_int(record.get("turn")):
        return "has a turn that is not a positive JSON integer"
    return record


def _turn_unavailable(reason: str) -> dict[str, Any]:
    return {"state": "unavailable", "reason": reason}


def _supervisor_state(status: Mapping[str, Any]) -> _DurableHead:
    """What one `status` frame the supervisor answered says about this head's durable shape."""
    return _DurableHead(
        source=REHYDRATED_FROM_SUPERVISOR,
        seq=int(status.get("journal_seq") or 0),
        # `stopping` counts as draining: `drain.requested` is written before `run.stopping`.
        draining=bool(status.get("draining")) or bool(status.get("stopping")),
        turn_open=bool(status.get("turn_open")),
        turn=int(status.get("turn") or 0),
        exited=not bool(status.get("alive")),
    )


@dataclass
class _Probe:
    """A section's bounded status request to the supervisor, and what came of it.

    One of `status`, `answer` (a frame that is not a readable status) or `error` is set. A failed
    request grants one recovery attempt, consumed by `spend_retry` before it is made, so a third
    request is impossible.
    """

    #: The frame the supervisor answered, when it answered one this runtime can read.
    status: Mapping[str, Any] | None = None
    #: What came back instead, when something did and it was not a readable status.
    answer: Any = None
    #: What went wrong on the socket, when nothing came back at all.
    error: BaseException | None = None
    #: The one recovery request a failed initial attempt may still spend.
    retry_available: bool = False

    @property
    def timed_out(self) -> bool:
        """Whether nothing came back because the time it was given ran out (no answer, no refusal)."""
        return _timed_out(self.error)

    def spend_retry(self) -> bool:
        """Consume the failed attempt's sole retry before anybody can make the request."""
        if not self.retry_available:
            return False
        self.retry_available = False
        return True

    @property
    def transient(self) -> bool:
        """Whether the refusal that came back is a bound the substrate clears by itself.

        Decided by the error name in the frame, never by the absence of a state: such a refusal
        must not close admission.
        """
        return isinstance(self.answer, Mapping) and _is_transient_bound(self.answer)


@dataclass(frozen=True)
class _Address:
    """Everything about one head that is derived from its run directory."""

    run_dir: Path
    socket_path: Path
    journal_path: Path
    pid_file: Path


@dataclass(frozen=True)
class _Refusal:
    """A delivery refused before anything reached the terminal."""

    status: str
    reason: str
    failure: HeadOperationError | None = None
    evidence: Any = None


def _timed_out(exc: BaseException | None) -> bool:
    """Whether a transport error is a request that ran out of time, not a supervisor that is gone."""
    while exc is not None:
        if isinstance(exc, TimeoutError):
            return True
        exc = exc.__cause__
    return False


def _allowance_refusal() -> _Refusal:
    """Nothing was offered: the handoff operation's time ran out before the supervisor answered."""
    return _Refusal(status=HEAD_BUSY, reason=f"{DELIVER_DEFERRED}: {HANDOFF_ALLOWANCE_SPENT}")


def _allowance_receipt(run: HeadRun, activity: HeadActivity) -> DeliverReceipt:
    return DeliverReceipt(
        status=HEAD_BUSY,
        run=run,
        reason=f"{DELIVER_DEFERRED}: {HANDOFF_ALLOWANCE_SPENT}",
        epoch=activity.epoch(run.run_id),
        lease=activity.lease(run.run_id),
        rotation_ready=activity.rotatable(run.run_id),
    )


def _pending_report(
    offered: int,
    floor: int,
    detail: str,
    delivery: Mapping[str, Any] | None = None,
    seq: int = 0,
) -> DeliveryReport:
    """A delivery offered inside a handoff operation whose answer or end did not come back in time."""
    delivery = delivery or {}
    return DeliveryReport(
        outcome=DELIVERY_PENDING,
        state=protocol.DELIVERY_IN_FLIGHT,
        written=int(delivery.get("written_bytes") or 0),
        offered=int(delivery.get("size_bytes") or offered),
        delivery_id=int(delivery.get("id") or 0),
        floor=floor,
        detail=detail,
        seq=max(seq, floor),
    )


def _refusal_status(error: str) -> str:
    """The status of a refusal the supervisor stated, by its error name, for every verb.

    A self-clearing bound is `HEAD_BUSY`; only `ERROR_HEAD_GONE` is `HEAD_GONE`; anything else is
    `HEAD_ALIVE`: a live supervisor refused, and the head is still the caller's to account for.
    """
    if _is_transient_bound({"error": error}):
        return HEAD_BUSY
    if error == protocol.ERROR_HEAD_GONE:
        return HEAD_GONE
    return HEAD_ALIVE


def _declared_bound(admitted: Mapping[str, Any]) -> float:
    """The bound the substrate put on the delivery it just admitted, in seconds.

    Read off the admitted delivery record (the head's `delivery_seconds`); a record declaring none
    gets the substrate's default.
    """
    try:
        bound = float(admitted.get("timeout_seconds") or 0.0)
    except (TypeError, ValueError):
        return UNDECLARED_DELIVERY_BOUND
    return bound if bound > 0 else UNDECLARED_DELIVERY_BOUND


def _is_transient_bound(answer: Mapping[str, Any]) -> bool:
    """Whether a stated refusal is one of the bounds the substrate clears by itself.

    The connection and attach bounds refuse a caller, not the head: every reader answers
    `HEAD_BUSY` and closes nothing.
    """
    return str(answer.get("error") or "") in (
        protocol.ERROR_CONNECTION_LIMIT,
        protocol.ERROR_ATTACH_LIMIT,
    )


def _refusal_detail(answer: Mapping[str, Any]) -> str:
    """What a supervisor's refusal frame says, in the order a reader wants it: detail, then name."""
    return (
        str(answer.get("detail") or "")
        or str(answer.get("error") or "")
        or "the head's supervisor answered nothing this runtime can read"
    )


def _stated_refusal(answer: Mapping[str, Any]) -> _Refusal:
    """A refusal the supervisor stated to a question asked before any payload was offered.

    A non-`ok` frame here (in practice the connection bound) is a refusal before the offer, never an
    unknown fate after one.
    """
    error = str(answer.get("error") or "")
    detail = _refusal_detail(answer)
    return _Refusal(
        status=_refusal_status(error),
        reason=detail,
        failure=HeadNudgeFailed(detail),
        evidence=answer,
    )


def _admission_refusal(answer: Mapping[str, Any]) -> _Refusal:
    """The substrate's own refusal of a payload, as a boundary status.

    Each one left the terminal untouched, so none is `HEAD_OK` and the turn is handed back.
    """
    error = str(answer.get("error") or "")
    detail = str(answer.get("detail") or error)
    if _is_transient_bound(answer):
        # Refused at a self-clearing bound without the request being read: nothing was offered,
        # so `HEAD_BUSY`, as in `_refusal_status`.
        return _Refusal(HEAD_BUSY, detail, HeadNudgeFailed(detail), answer)
    if error == protocol.ERROR_DRAINING:
        return _Refusal(HEAD_DRAINING, detail, HeadNudgeFailed(detail), answer)
    if error == protocol.ERROR_HEAD_GONE:
        return _Refusal(HEAD_GONE, detail, HeadNudgeFailed(detail), answer)
    if error == protocol.ERROR_INPUT_IN_FLIGHT:
        # Another payload holds the floor: worth retrying once it lands, never interleaved.
        return _Refusal(HEAD_BUSY, detail, HeadNudgeFailed(detail), answer)
    # An oversized payload and everything else: the head is untouched and still the caller's.
    return _Refusal(HEAD_ALIVE, detail, HeadNudgeFailed(detail), answer)


def _wake_hook(transport: Any) -> Callable[[], Any] | None:
    """The pre-send hook a caller's transport carries (`before_send`), or `None`."""
    hook = getattr(transport, "before_send", None)
    return hook if callable(hook) else None


def _payload_of(pointer: NudgePointer) -> bytes:
    """One pointer as the bytes a head's terminal receives: the line, and the Enter that sends it."""
    return (pointer.text + "\n").encode("utf-8")


def _output_of(seen: ObserveReceipt) -> int:
    """The supervisor's count of what the head printed, off an observation's status frame."""
    evidence = seen.evidence if isinstance(seen.evidence, Mapping) else {}
    value = evidence.get("output_bytes")
    return value if isinstance(value, int) else 0


def _outcome_of(
    run: HeadRun,
    pointer: NudgePointer,
    report: DeliveryReport,
    subject: str,
    *,
    submits: int = 0,
    submitted: int = 0,
    confirmed: bool = True,
) -> DeliveryOutcome:
    """The delivery evidence for a payload that provably reached the head's terminal.

    `DELIVERY_CONFIRMED`: the proof is the supervisor's count of bytes the kernel took, corroborated
    by the journal. For an agent's prompt with submits, `turn_confirmed` says whether a turn was
    seen to start, and `payload_left_in_composer` is set when not.
    """
    payload_bytes, payload_hash = payload_fingerprint(pointer.text)
    evidence = DeliveryEvidence(
        handle=run.handle,
        subject=subject,
        payload_bytes=payload_bytes,
        payload_sha256=payload_hash,
        delivery_mode=NUDGE_FILE_MODE if pointer.document else "",
        document_path=pointer.document,
        adapter=run.spec.adapter,
        body_write_accepted=True,
        body_bytes_written=report.written,
        body_write_count=1,
        send_accepted=True,
        bytes_written=report.written,
        attempts=1,
        turn_confirmed=True,
        reason=report.detail,
    )
    if submits or not confirmed:
        evidence = replace(
            evidence,
            stage=(
                STAGE_TURN_OBSERVED
                if confirmed
                else STAGE_ENTER_ACCEPTED if submits else STAGE_PAYLOAD_WRITTEN
            ),
            submit_write_accepted=submits > 0,
            submit_bytes_written=submitted,
            submit_count=submits,
            attempts=max(submits, 1),
            resends=max(submits - 1, 0),
            turn_confirmed=confirmed,
            payload_left_in_composer=not confirmed,
        )
    return DeliveryOutcome(DELIVERY_CONFIRMED, evidence)


@dataclass(frozen=True)
class _HandoffLook:
    """One look at a handoff's head: a status cut to the operation's deadline, then the journal.

    `deferred` says why nothing may be concluded from the supervisor this pass (its answer did not
    come back in time, or it is at a self-clearing bound). `writing` is `line` or `submit` while the
    supervisor shows that write of this handoff still in flight.
    """

    probe: _Probe | None
    status: Mapping[str, Any] | None
    progress: _HandoffProgress
    deferred: str = ""
    writing: str = ""

    def epoch(self, activity: HeadActivity, run: HeadRun) -> int:
        """The head's epoch, raised to the sequence this look's status stated (no further request)."""
        seq = self.status.get("journal_seq") if self.status is not None else None
        if isinstance(seq, int) and not isinstance(seq, bool):
            return activity.advance_to(run.run_id, seq)
        return activity.epoch(run.run_id)


def _stage_of(progress: _HandoffProgress) -> str:
    """The stage the journal puts a handoff at: nothing typed, typed, or submitted."""
    if progress.typed is None:
        return HANDOFF_SETTLE
    return HANDOFF_SUBMITTED if progress.submits else HANDOFF_TYPED


def _later_stage(one: str, other: str) -> str:
    """The further of two handoff stages."""
    order = (HANDOFF_SETTLE, HANDOFF_TYPED, HANDOFF_SUBMITTED)
    return max(one, other, key=order.index)


def _output_of_status(status: Mapping[str, Any]) -> int:
    value = status.get("output_bytes")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


@dataclass(frozen=True)
class _HandoffProgress:
    """How far one production handoff got, as the head's journal records it above the floor.

    `typed` is the `input.accepted` that put the line on the terminal whole; `typed_prefix` one that
    left only part of it. `submits` counts accepted submit keystrokes after it. `submit_bytes` is
    the output the latest submit's turn has printed as far as the journal says (progress records,
    then the turn's own total when it finishes). `unknown` says why none of this can be established.
    """

    unknown: str = ""
    typed: Mapping[str, Any] | None = None
    typed_prefix: Mapping[str, Any] | None = None
    typed_at: float = 0.0
    submits: int = 0
    submit_at: float = 0.0
    submit_turn: int = 0
    submit_bytes: int = 0
    submit_finished: bool = False

    @property
    def confirmed(self) -> bool:
        """A submit's turn printed what a taken prompt prints (`SUBMIT_CONFIRM_BYTES`)."""
        return self.submits > 0 and self.submit_bytes >= SUBMIT_CONFIRM_BYTES


def _handoff_progress(read: Any, subject: str, floor: int) -> _HandoffProgress:
    """Read one handoff's stage off a journal tail: the only place a production handoff is recalled.

    Records at or below `floor` predate the handoff. A window that no longer reaches the floor, a
    torn or out-of-order tail, a line typed twice, or a new supervisor incarnation after the line
    are all unknown: absence of a record proves nothing there.
    """
    if read.malformed or not read.ordered:
        return _HandoffProgress(unknown="the head's journal tail is not readable in order")
    events = [event for event in read.events if isinstance(event.get("seq"), int)]
    if read.partial_head and (not events or events[0]["seq"] > floor + 1):
        return _HandoffProgress(unknown="the head's journal window no longer reaches this handoff's floor")
    submit = f"{subject}:submit"
    typed: Mapping[str, Any] | None = None
    typed_at = 0.0
    submits = 0
    submit_at = 0.0
    submit_turn = 0
    submit_own_turn = False
    submit_bytes = 0
    submit_finished = False
    open_turn = 0
    for event in events:
        seq = event["seq"]
        kind = event.get("kind")
        turn = event.get("turn") if isinstance(event.get("turn"), int) else 0
        if kind == local_pty.TURN_STARTED:
            open_turn = turn
            if submits and not submit_turn and seq > floor:
                submit_turn, submit_own_turn = turn, True
        elif kind == local_pty.TURN_FINISHED:
            if submits and turn == submit_turn and not submit_finished:
                submit_finished = True
                total = event.get("output_bytes")
                if submit_own_turn and isinstance(total, int):
                    # The turn's own count includes what was folded out of its progress records.
                    submit_bytes = max(submit_bytes, total)
            if turn == open_turn:
                open_turn = 0
        elif kind == local_pty.PROVIDER_PROGRESSED:
            amount = event.get("output_bytes")
            if submits and turn == submit_turn and not submit_finished and isinstance(amount, int):
                submit_bytes += amount
        elif seq <= floor:
            continue
        elif kind == local_pty.RUN_STARTED and typed is not None:
            return _HandoffProgress(unknown="the head's supervisor started again after the line was typed")
        elif kind == local_pty.INPUT_ACCEPTED and event.get("subject") == subject:
            written = event.get("bytes") if isinstance(event.get("bytes"), int) else 0
            if written <= 0:
                continue  # it landed nothing: the terminal is as it was
            if typed is not None:
                return _HandoffProgress(unknown="this handoff's line was typed more than once")
            if not event.get("complete"):
                return _HandoffProgress(typed_prefix=event)
            typed, typed_at = event, float(event.get("at") or 0.0)
        elif kind == local_pty.INPUT_ACCEPTED and event.get("subject") == submit and typed is not None:
            written = event.get("bytes") if isinstance(event.get("bytes"), int) else 0
            if written <= 0:
                continue
            submits += 1
            submit_at = float(event.get("at") or 0.0)
            # An Enter into an open turn joins it; otherwise the supervisor opens one right after.
            submit_turn, submit_own_turn = open_turn, False
            submit_bytes, submit_finished = 0, False
    return _HandoffProgress(
        typed=typed,
        typed_at=typed_at,
        submits=submits,
        submit_at=submit_at,
        submit_turn=submit_turn,
        submit_bytes=submit_bytes,
        submit_finished=submit_finished,
    )


def _journalled_report(event: Mapping[str, Any], floor: int) -> DeliveryReport:
    """The `DeliveryReport` a typed line's `input.accepted` stands for, when no live follow made one."""
    written = _journal_int(event, "bytes", 0)
    return _delivery_report(
        state=str(event.get("state") or ""),
        written=written,
        offered=_journal_int(event, "offered_bytes", written),
        established=True,
        delivery_id=_journal_int(event, "delivery", 0),
        journalled=True,
        floor=floor,
        detail=str(event.get("detail") or ""),
        seq=_journal_int(event, "seq", 0),
    )


def _journal_int(event: Mapping[str, Any], key: str, default: int) -> int:
    """An integer field of a journal record, or `default` when it is missing or not an integer."""
    value = event.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _spawn_status(exc: local_pty.LocalPtySpawnError) -> str:
    """Which status a refused bring-up left behind.

    Incomplete cleanup, a timeout or `already_running` may leave something running: `HEAD_ALIVE`,
    as `HeadSpawnAborted` on the legacy path, so no second head is opened beside it.
    """
    if not exc.cleanup_complete or exc.reason in ("timeout", "already_running"):
        return HEAD_ALIVE
    return HEAD_GONE


def _spawn_failure(exc: local_pty.LocalPtySpawnError, run: HeadRun | None) -> HeadOperationError:
    if _spawn_status(exc) == HEAD_ALIVE and run is not None:
        return HeadSpawnAborted(str(exc), run=run)
    return HeadSpawnFailed(str(exc))


def _with_pid_file(run: HeadRun, pid_file: str) -> HeadRun:
    """The same run, holding the launch-identity record its head writes."""
    if run.pid_file == pid_file:
        return run
    payload = run.to_json()
    payload["pid_file"] = pid_file
    return HeadRun.from_json(payload)


def _task_of(run: HeadRun) -> str:
    return _binding_of(run.task_ref)


def _binding_of(task_ref: TaskRef) -> str:
    """The `task` this backend's heads write and are compared by: the product's one spelling."""
    return task_binding(task_ref.kind, task_ref.ref)


def _in_flight(status: Mapping[str, Any]) -> bool:
    delivery = status.get("delivery")
    return isinstance(delivery, dict) and delivery.get("state") == protocol.DELIVERY_IN_FLIGHT


def _has_exited(address: _Address) -> bool:
    """Whether the current journal incarnation has a complete `run.exited`.

    Run directories and journals are reused. An exit from an earlier incarnation cannot confirm
    that the head started after it is gone, and a damaged tail cannot prove which incarnation its
    final records belong to. The launch identity remains the primary stop witness.
    """
    try:
        reading = local_pty.read_tail(address.journal_path)
    except OSError:
        return False
    if reading.truncated_tail or reading.malformed or not reading.ordered:
        return False
    started = max(
        (int(event.get("seq") or 0) for event in reading.of_kind(local_pty.RUN_STARTED)),
        default=0,
    )
    exited = max(
        (int(event.get("seq") or 0) for event in reading.of_kind(local_pty.RUN_EXITED)),
        default=0,
    )
    return bool(started and exited > started)


def head_run_journal(run_dir: str | os.PathLike[str]) -> tuple[dict[str, Any], ...]:
    """Everything one head's supervisor wrote about it, read whole from outside its lifecycle.

    Whole rather than a tail, so a finished run's `run.exited` is always found. A missing file is
    an empty journal; `OSError` propagates, so "unreadable" stays distinct from "empty".
    """
    return head_run_journal_read(run_dir).events


def head_run_journal_read(run_dir: str | os.PathLike[str]) -> local_pty.JournalReadResult:
    """`head_run_journal` with what the read left out (`malformed`, `truncated_tail`).

    For a reader that must not present a damaged journal as a clean one. `OSError` propagates.
    """
    return local_pty.read_events(Path(run_dir) / protocol.JOURNAL_NAME)


def head_run_supervisor_files(run_dir: str | os.PathLike[str]) -> tuple[Path, Path]:
    """`(supervisor.lock, supervisor.pid)` under the run directory, for a reader outside it.

    Nothing is opened; a reader must never take the lock itself.
    """
    root = Path(run_dir)
    return root / protocol.SUPERVISOR_LOCK_NAME, root / protocol.SUPERVISOR_PID_NAME


@dataclass(frozen=True)
class SupervisorLease:
    """Who holds one run's supervisor lock, per the kernel's lock table, and what the files say.

    `lock_readable` false: `supervisor.lock` could not be read and nothing else was looked at.
    `table_readable` false: `/proc/locks` could not be read, so the holder is unknown. `error` says
    what failed. With both true, empty `holders` means no process holds the lock. `content_error`
    is set when a file held something other than a pid; that pid is then `None`.
    """

    lock_readable: bool
    table_readable: bool = False
    holders: tuple[int, ...] = ()
    written_pid: int | None = None
    supervisor_pid: int | None = None
    error: str = ""
    content_error: str = ""


def head_run_supervisor_lease(run_dir: str | os.PathLike[str]) -> SupervisorLease:
    """Read who holds this run's supervisor lock without taking it, for a reader outside the run.

    A supervisor holds an exclusive `flock` on `supervisor.lock` for its life, so the holder comes
    from `/proc/locks`; the pids in `supervisor.lock` and `supervisor.pid` are reported beside it.
    An unheld lock means no supervisor owns the run; whether its head still runs is the
    heartbeat's question.
    """
    path, pid_path = head_run_supervisor_files(run_dir)
    try:
        info = path.stat()
        written = path.read_bytes()
    except OSError as exc:
        return SupervisorLease(lock_readable=False, error=str(exc.strerror or exc))
    written_pid, lock_damage = _pid_content(path.name, written)
    try:
        supervisor_pid, pid_damage = _pid_content(pid_path.name, pid_path.read_bytes())
    except OSError:
        supervisor_pid, pid_damage = None, ""
    content_error = "; ".join(damage for damage in (lock_damage, pid_damage) if damage)
    try:
        holders = _flock_holders(info)
    except OSError as exc:
        return SupervisorLease(
            lock_readable=True,
            written_pid=written_pid,
            supervisor_pid=supervisor_pid,
            error=str(exc.strerror or exc),
            content_error=content_error,
        )
    return SupervisorLease(
        lock_readable=True,
        table_readable=True,
        holders=tuple(holders),
        written_pid=written_pid,
        supervisor_pid=supervisor_pid,
        content_error=content_error,
    )


def head_run_directory(root: str | os.PathLike[str], run_id: str) -> Path:
    """The run directory `run_id` names under `root`, refused for anything that is not a run id.

    The substrate's own rule, so a reader outside the backend builds the path the supervisor did
    and cannot be walked out of `root` by a value holding a separator or a dot name.
    """
    try:
        return protocol.run_dir_for(root, run_id)
    except local_pty.ProtocolError as exc:
        raise ValueError(str(exc)) from None


def head_run_pid_file(root: str | os.PathLike[str], run_id: str) -> Path:
    """The heartbeat `run_id`'s own run directory holds: where a stop of that run reads its launch
    identity when the run is handed over without a pid file of its own."""
    return head_run_directory(root, run_id) / protocol.PID_FILE_NAME


def fence_cleanup_scopes(root: Path, workspace: str, task: TaskRef,
                         runs: Sequence[HeadRun], *, recorded_only: bool = False,
                         remaining: Callable[[], float] | None = None) -> None:
    """Refuse Git settlement while an unrecorded generation owns its target.

    This read uses the same canonical owner as stop. The dispatcher serializes
    admission and Git effects; recorded generations still go through runtime.stop
    for native identity and recursive empty-scope proof, including on replay.

    `recorded_only` reads nothing but the recorded runs' own canonical directories:
    a replaced owner's workspace and task now belong to its successor. A caller's
    `remaining` cuts each native observation of a terminal scope's disappearance.
    """
    if root.absolute() != root.resolve():
        raise ValueError("cleanup scope root is substituted")
    if not root.exists():
        return
    known = {(run.run_id, run.scope_generation): run for run in runs}
    task_identity = _binding_of(task)
    directories = ([protocol.run_dir_for(root, run.run_id) for run in runs] if recorded_only
                   else list(root.iterdir()))
    for directory in directories:
        if directory.is_symlink():
            raise ValueError("cleanup scope directory is substituted")
        if not directory.is_dir():
            continue
        owner = ScopedHeadLifecycle.from_run_dir(directory)
        if owner is None:
            continue
        if (directory != protocol.run_dir_for(root, owner.run_id)
                or (directory / "scope-owner.json").is_symlink()):
            raise ValueError("cleanup scope owner path differs from its canonical identity")
        record = ScopedHeadLifecycle.read_owner(directory)
        run = known.get((owner.run_id, owner.generation))
        if recorded_only and run is None:
            raise ValueError("cleanup scope owner run or generation differs from its recorded head")
        if run is not None and (record.get("workspace") != run.workspace
                                or record.get("task") != _binding_of(run.task_ref)
                                or record.get("role") != run.role):
            raise ValueError("cleanup scope owner binding differs from its recorded head")
        if recorded_only:
            continue
        if record.get("workspace") == workspace or record.get("task") == task_identity:  # noqa: SIM102
            if (owner.run_id, owner.generation) not in known:
                if record.get("cleanup_complete") and not record.get("launch_allowed"):
                    # A retained terminal flag cannot bless a reused live unit.
                    inventory = (runtime_scope_inventory(root.parent, {record["unit"]}) if remaining is None
                                 else runtime_scope_inventory(root.parent, {record["unit"]}, remaining=remaining))
                    if inventory.errors or record["unit"] not in inventory.disappeared:
                        raise ValueError("cleanup terminal scope lacks current disappearance proof")
                    continue
                raise ValueError("cleanup workspace has an unrecorded or newer scope owner")


def runtime_scope_inventory(data_dir: Path, units: set[str], *,
                            remaining: Callable[[], float] | None = None) -> RuntimeScopeInventory:
    """Read canonical lifecycle ownership for host preservation and diagnostics.

    Consumers use this runtime boundary, never the private PTY owner format.
    The projection grants no launch, adoption or cleanup authority. A caller's
    `remaining` cuts each native scope observation to what is left of its deadline.
    """
    from ummanu.runtime.head.local_pty.scope_inventory import read_runtime_scopes

    return read_runtime_scopes(data_dir, units, remaining=remaining)


def head_scope_owner_lock(run_dir: str | os.PathLike[str]) -> AbstractContextManager[None]:
    """The run's own scope-owner flock: every admission, cleanup and owner rewrite holds it.

    Contention raises `MemoryScopeError`, the lifecycle's retryable refusal.
    """
    return ScopedHeadLifecycle.owner_lock(Path(run_dir))


def head_scope_owner_valid(record: Any) -> dict[str, Any]:
    """The lifecycle's own validation of a scope-owner record; raises `MemoryScopeError`."""
    return ScopedHeadLifecycle.validate_owner(record)


def head_run_first_record(run_dir: str | os.PathLike[str]) -> dict[str, Any] | None:
    """Whose run this is, from the journal's first record: its `task`, `role`, `run_id` and `at`.

    Only those four fields, never the record itself: `run.started` carries the head's command,
    and the command carries the head's memory token. `None` when there is no journal; the read is
    bounded to the first `JOURNAL_TAIL_BYTES`, and a first line that is not a `run.started`
    record raises `ValueError`. `OSError` propagates, as it does from the other readers here.
    """
    path = Path(run_dir) / protocol.JOURNAL_NAME
    try:
        with open(path, "rb") as journal:
            head = journal.read(local_pty.JOURNAL_TAIL_BYTES)
    except FileNotFoundError:
        return None
    line, newline, _rest = head.partition(b"\n")
    if not newline:
        raise ValueError("the journal's first record is not a complete line")
    record = json.loads(line.decode("utf-8"))
    if not isinstance(record, dict) or record.get("kind") != local_pty.RUN_STARTED:
        raise ValueError("the journal does not begin with run.started")
    return {key: record.get(key) for key in ("task", "role", "run_id", "at")}


def head_run_journal_tail(run_dir: str | os.PathLike[str]) -> local_pty.JournalReadResult:
    """The last `JOURNAL_TAIL_BYTES` of one run's journal, for a reader showing its recent records.

    Bounded where `head_run_journal_read` is not, because a view of what a head did lately needs
    its end and nothing else. `OSError` propagates for the reason it does there.
    """
    return local_pty.read_tail(Path(run_dir) / protocol.JOURNAL_NAME)


def head_run_loss_reason(root: str | os.PathLike[str], run_id: str) -> str | None:
    """Read a supervisor's typed death record for this exact run, if one exists."""
    from ummanu.runtime.head.memory import MEMORY_LIMIT_REASON

    try:
        events = head_run_journal_tail(protocol.run_dir_for(root, run_id)).events
    except (OSError, ValueError):
        return None
    for event in reversed(events):
        if event.get("run_id") != run_id:
            continue
        if event.get("kind") == local_pty.RUN_STARTED:
            return None
        if event.get("kind") != local_pty.RUN_EXITED:
            continue
        if event.get("head_loss_reason") == MEMORY_LIMIT_REASON and event.get("signal") == 9:
            return MEMORY_LIMIT_REASON
        return None
    return None


def _flock_holders(info: os.stat_result) -> list[int]:
    """The pids `/proc/locks` names as holding an `flock` on this file."""
    holders = []
    wanted = (os.major(info.st_dev), os.minor(info.st_dev), info.st_ino)
    with open("/proc/locks", encoding="utf-8") as table:
        for line in table:
            # `N: FLOCK ADVISORY WRITE <pid> <major>:<minor>:<inode> 0 EOF`; a waiter reads `N: ->`.
            fields = line.split()
            if len(fields) < 6 or fields[1] != "FLOCK":
                continue
            try:
                major, minor, inode = fields[5].split(":")
                if (int(major, 16), int(minor, 16), int(inode)) == wanted:
                    holders.append(int(fields[4]))
            except ValueError:
                continue
    return holders


def _pid_content(name: str, raw: bytes) -> tuple[int | None, str]:
    """The pid a lock or pid file holds, or `None` and why its content is not one; never raises.

    An empty file is a supervisor that has not written its pid yet, not damage. Anything else that
    is not a plain ASCII decimal -- bytes that are not UTF-8, `1e999`, a sign -- is unreadable
    content rather than an exception.
    """
    text = raw.strip()
    if not text:
        return None, ""
    if text.isdigit():  # bytes.isdigit is ASCII-only, so int() below cannot refuse it
        return int(text), ""
    return None, f"{name} holds no pid ({text[:40]!r})"


def _last_event_at(address: _Address) -> float:
    """The head's own clock, as the newest thing its journal has to say about it."""
    events = local_pty.read_tail(address.journal_path).events
    return float(events[-1].get("at") or 0.0) if events else 0.0


def _unobservable(
    run: HeadRun, reason: str, epoch: int, lease: Any, rotatable: bool, *, evidence: Any = None
) -> ObserveReceipt:
    """An observation this backend could not make, said as that and not as an answer."""
    return ObserveReceipt(
        status=HEAD_UNSUPPORTED,
        run=run,
        reason=reason,
        evidence=evidence,
        epoch=epoch,
        lease=lease,
        rotation_ready=rotatable,
        handle=run.handle,
        leaf=run.leaf or run.run_id,
    )
