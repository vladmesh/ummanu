"""Shared backend values, refusal vocabulary and lifecycle bindings."""

from __future__ import annotations

from collections.abc import (
    Callable,
    Mapping,
)
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import (
    TYPE_CHECKING,
    Any,
)

from ummanu.runtime.head import local_pty
from ummanu.runtime.head.handoff import HandoffOperation
from ummanu.runtime.head.identity import task_binding
from ummanu.runtime.head.local_pty import protocol
from ummanu.runtime.head.operations import (
    HeadNudgeFailed,
    HeadOperationError,
    HeadSpawnAborted,
    HeadSpawnFailed,
    NudgePointer,
)
from ummanu.runtime.head.run import (
    HeadRun,
    StopInitiator,
)
from ummanu.runtime.head.runtime import (
    HEAD_ALIVE,
    HEAD_BUSY,
    HEAD_DRAINING,
    HEAD_GONE,
    HEAD_UNSUPPORTED,
    DeliverReceipt,
    HeadActivity,
    ObserveReceipt,
)
from ummanu.runtime.head.task_ref import TaskRef

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


#: A submit started a turn when this much output follows it within this long (a taken prompt
#: redraws kilobytes; an Enter that sent nothing redraws at most a cursor).
SUBMIT_CONFIRM_BYTES = 256


#: How many submits one prompt is given before it is reported as typed and not taken.
SUBMIT_ATTEMPTS = 2


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


class _RuntimeState:
    """Static lifecycle contracts shared by this backend's two method mixins.

    The concrete runtime owns initialization and implements the lifecycle methods.
    Declarations exist only for type checking, with no runtime forwarding or hooks.
    """

    if TYPE_CHECKING:
        activity: HeadActivity
        _lock: RLock
        _fatal: dict[str, str]
        _admission_notes: dict[str, str]
        _quiet_marks: dict[str, tuple[tuple[Any, ...], float]]
        _delivery_poll: float
        _prompt_first_output: float
        _prompt_poll: float
        _prompt_quiet: float
        _prompt_settle: float
        _submit_confirm: float
        _monotonic: Callable[[], float]
        _wall: Callable[[], float]
        _sleep: Callable[[float], None]

        def delivery_wait_for(self, substrate_bound: float) -> float: ...

        def observe(self, run: HeadRun) -> ObserveReceipt: ...

        def _rehydrate(self, run: HeadRun, probe: _Probe | None = None) -> None: ...

        def _section_probe(
            self, run: HeadRun, operation: HandoffOperation | None = None
        ) -> tuple[_Address | None, _Probe | None]: ...

        def _probe(self, address: _Address, operation: HandoffOperation | None = None) -> _Probe: ...

        def _address(self, run: HeadRun) -> _Address | None: ...

        def _connect(
            self, address: _Address, operation: HandoffOperation | None = None
        ) -> local_pty.SupervisorClient: ...

        def _process_alive(self, address: _Address, run: HeadRun) -> bool: ...

        def _identity_says_dead(self, address: _Address) -> bool: ...

        def _close_substrate_admission(
            self, run: HeadRun, initiator: StopInitiator, *, operation: HandoffOperation | None = None
        ) -> tuple[bool, Any, int, _Probe | None]: ...

        def _turn_still_running(self, run: HeadRun, probe: _Probe | None = None) -> str: ...
