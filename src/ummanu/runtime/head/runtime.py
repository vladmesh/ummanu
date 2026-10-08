"""`HeadRuntime`: the one typed boundary for a head's lifecycle; `local_pty_head` implements it.

Six verbs: `start`, `deliver`, `observe`, `request_drain`, `stop`, `attach`. Every verb answers
with a receipt (never a bool, dict or exception); a refusal carries the operation's own error
unchanged in `failure`, and a verb the backend cannot honestly perform answers `unsupported`.
Busyness is not a lifecycle state: `HeadRun.working` is durable history, while "a turn is running
now" is a `TurnLease` plus a per-head activity epoch kept here. See docs/HEAD_RUNTIME.md.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from ummanu.runtime.tui_delivery import DeliveryOutcome
from .operations import HeadOperationError, NudgePointer
from .run import HeadRun, StopInitiator
from .spec import HeadSpec
from .task_ref import TaskRef

HEAD_OK = "ok"
# Refused: mid-turn or a pane held in a dialog. Nothing left behind; worth retrying.
HEAD_BUSY = "busy"
# Refused: a drain was requested, so this runtime hands the head no more work.
HEAD_DRAINING = "draining"
# Refused with something still alive (pane not closed, stop unconfirmed); the caller still owns it.
HEAD_ALIVE = "alive"
# Refused, and nothing the verb touched survived.
HEAD_GONE = "gone"
# This backend cannot honestly perform or answer this verb. Never a disguised `no`.
HEAD_UNSUPPORTED = "unsupported"
RECEIPT_STATUSES = (HEAD_OK, HEAD_BUSY, HEAD_DRAINING, HEAD_ALIVE, HEAD_GONE, HEAD_UNSUPPORTED)

# Observation reasons are routing tokens, never free text.
OBSERVE_NO_ADDRESS = "no_address"
OBSERVE_INVENTORY_UNREADABLE = "inventory_unreadable"
OBSERVE_PANE_ABSENT = "pane_absent"
OBSERVE_PANE_DISCONNECTED = "pane_disconnected"
OBSERVE_READINESS_UNKNOWN = "readiness_unknown"


class TurnLeaseError(RuntimeError):
    """A lease that would say something untrue about which turn a head is running."""


@dataclass(frozen=True)
class TurnLease:
    """One running turn of a head, granted on delivery and released when the backend sees it end.

    Not a field of `HeadRun`: a persisted run cannot answer "is a turn running now".
    `granted_at_epoch` lets a holder of an old lease see the head has acted since.
    """

    lease_id: str
    run_id: str
    subject: str = ""
    granted_at_epoch: int = 0

    def __post_init__(self) -> None:
        if not self.lease_id or not self.run_id:
            raise TurnLeaseError("a turn lease names its head and itself")


class HeadActivity:
    """Per-head activity epoch, turn lease and admission, read under the owning runtime's lock.

    The epoch is monotone and per head, moving whenever the backend sees that head act. At most
    one lease per head. Admission (closed by a drain) is independent of the running turn. `ticks`
    is a runtime-wide diagnostic count and must never feed a quiescence decision. Not durable and
    not locked; the owning runtime serialises access. A backend with a durable witness restores
    state via `advance_to` and `adopt`, which only move forward: an epoch is never lowered and an
    adopted turn never replaces a held one.
    """

    def __init__(self) -> None:
        self._ticks = 0
        self._epochs: dict[str, int] = {}
        self._leases: dict[str, TurnLease] = {}
        self._output_marks: dict[str, float] = {}
        self._closed: set[str] = set()

    @property
    def ticks(self) -> int:
        """Runtime-wide activity count; never compare it for quiescence (other heads move it)."""
        return self._ticks

    def epoch(self, run_id: str) -> int:
        """This head's activity epoch, and zero for a head this runtime has never seen."""
        return self._epochs.get(run_id, 0)

    def acted(self, run_id: str) -> int:
        """Record that the runtime made this head act; return the new epoch.

        Pane reads go through `observed` instead, which may report no activity.
        """
        if not run_id:
            return 0
        self._ticks += 1
        epoch = self._epochs.get(run_id, 0) + 1
        self._epochs[run_id] = epoch
        return epoch

    def noted(self, run_id: str = "") -> int:
        """Count one runtime action in `ticks` without moving any head's epoch.

        Used by backends whose epoch is the head's journal sequence, where a local increment would
        be on a different scale. `run_id` is documentary only.
        """
        del run_id
        self._ticks += 1
        return self._ticks

    def observed(self, run_id: str, *, output_at: float = 0.0) -> int:
        """Record a pane read with output clock `output_at`; return the resulting epoch.

        A repeated clock value or a missing clock (`0`) is not activity: moving the epoch on either
        would make "silent" indistinguishable from "unobserved".
        """
        if not run_id or not output_at:
            return self.epoch(run_id)
        if self._output_marks.get(run_id, 0.0) >= output_at:
            return self.epoch(run_id)
        self._output_marks[run_id] = output_at
        return self.acted(run_id)

    def advance_to(self, run_id: str, epoch: int) -> int:
        """Raise this head's epoch to a durable witness's value; never lower it.

        Monotone across processes: a smaller value, zero or unknown is a no-op. Returns the
        resulting epoch.
        """
        if not run_id or epoch <= 0:
            return self.epoch(run_id)
        current = self._epochs.get(run_id, 0)
        if epoch <= current:
            return current
        self._epochs[run_id] = epoch
        return epoch

    def adopt(self, run_id: str, lease: TurnLease) -> TurnLease:
        """Adopt a turn granted by a predecessor process; return the lease now held.

        Unlike `grant`, an outstanding lease is not a conflict: it is returned as the answer.
        """
        if not run_id or lease.run_id != run_id:
            raise TurnLeaseError("an adopted lease names the head it was adopted for")
        held = self._leases.get(run_id)
        if held is not None:
            return held
        self._leases[run_id] = lease
        return lease

    def lease(self, run_id: str) -> TurnLease | None:
        """The turn this head is running, when it is running one this runtime granted."""
        return self._leases.get(run_id)

    def busy(self, run_id: str) -> bool:
        """Whether this runtime has an outstanding turn for this head."""
        return run_id in self._leases

    def grant(self, run_id: str, subject: str = "") -> TurnLease:
        """Hand this head a turn; raises `TurnLeaseError` while one is outstanding.

        There is no `renew`: evicting the running turn for a newer delivery must be refused, and
        the runtime turns the refusal into a receipt.
        """
        if not run_id:
            raise TurnLeaseError("a turn lease names the head it was granted to")
        held = self._leases.get(run_id)
        if held is not None:
            raise TurnLeaseError(f"the head is already running turn {held.lease_id}")
        lease = TurnLease(
            lease_id=uuid.uuid4().hex,
            run_id=run_id,
            subject=subject,
            granted_at_epoch=self.epoch(run_id),
        )
        self._leases[run_id] = lease
        return lease

    def release(self, run_id: str) -> TurnLease | None:
        """Close this head's turn, and hand back the lease that was closed, if there was one."""
        return self._leases.pop(run_id, None)

    def admits(self, run_id: str) -> bool:
        """Whether this runtime will still hand this head work."""
        return run_id not in self._closed

    def close_admission(self, run_id: str) -> None:
        """Take this head out of service. Says nothing about the turn it is running."""
        if run_id:
            self._closed.add(run_id)

    def open_admission(self, run_id: str) -> None:
        """Put this head back in service — the undo a refused stop owes its admission."""
        self._closed.discard(run_id)

    def rotatable(self, run_id: str) -> bool:
        """Whether this head is done: it takes no more work and the last turn it held has closed."""
        return not self.admits(run_id) and not self.busy(run_id)

    def forget(self, run_id: str) -> None:
        """Drop everything this runtime remembers about a head that has ended."""
        self._leases.pop(run_id, None)
        self._output_marks.pop(run_id, None)
        self._epochs.pop(run_id, None)
        self._closed.discard(run_id)


@dataclass(frozen=True)
class HeadReceipt:
    """What one verb did, routable without catching anything.

    `failure` carries the operation's own error unchanged (an aborted bring-up is not a failed
    one). `epoch` is this head's activity epoch, the value a stop-if-quiescent hands back.
    `rotation_ready` means admission is closed and the last turn has ended, observed together.
    """

    status: str
    run: HeadRun | None = None
    reason: str = ""
    failure: HeadOperationError | None = None
    evidence: Any = None
    epoch: int = 0
    lease: TurnLease | None = None
    rotation_ready: bool = False

    def __post_init__(self) -> None:
        if self.status not in RECEIPT_STATUSES:
            raise ValueError(
                f"a head receipt's status is one of {', '.join(RECEIPT_STATUSES)}, not {self.status!r}"
            )

    @property
    def ok(self) -> bool:
        """Whether the verb did what it says it does."""
        return self.status == HEAD_OK

    @property
    def deferred(self) -> bool:
        """Whether this refusal is one to make again rather than one to recover from."""
        return self.status in (HEAD_BUSY, HEAD_DRAINING)

    @property
    def left_alive(self) -> bool:
        """Whether this refusal left something the caller still has to account for."""
        return self.status == HEAD_ALIVE

    @property
    def unsupported(self) -> bool:
        """Whether this backend simply cannot do or answer this."""
        return self.status == HEAD_UNSUPPORTED


@dataclass(frozen=True)
class StartReceipt(HeadReceipt):
    """A bring-up, and the delivery it made when it was given a pointer to deliver."""

    delivery: DeliveryOutcome | None = None
    fallback_reason: str = ""
    #: The head is up and its production handoff is pending at this stage (`HEAD_BUSY`): the bring-up
    #: is kept, and the next tick continues the same handoff (`runtime.head.handoff`).
    handoff_stage: str = ""


@dataclass(frozen=True)
class DeliverReceipt(HeadReceipt):
    """One prompt put in front of a running head, and what the delivery boundary saw.

    `delivery_state` is empty when delivery completes before the verb returns (then `HEAD_OK`
    means arrived); otherwise `complete`, `stalled`, `failed`, or `unknown` (could not establish
    which; never read it as "nothing landed"). In-flight is never reported. `delivered_bytes` and
    `offered_bytes` are always reported together so a partial arrival cannot read as whole.
    """

    delivery: DeliveryOutcome | None = None
    delivery_state: str = ""
    delivered_bytes: int = 0
    offered_bytes: int = 0
    #: A production handoff not finished in this pass stopped at this stage (`HEAD_BUSY`). Whatever it
    #: already wrote is in the head's journal, so retrying with the same `PromptHandoff` continues it.
    handoff_stage: str = ""

    @property
    def arrived(self) -> bool:
        """Whether the whole payload provably reached the head; route on this, not `ok`, for bytes."""
        if not self.ok:
            return False
        return not self.delivery_state or self.delivery_state == "complete"


@dataclass(frozen=True)
class ObserveReceipt(HeadReceipt):
    """What the backend can say about this head right now, and nothing it cannot.

    Unanswerable fields are `None`, so "not busy" differs from "not knowable". `busy` comes from
    the turn lease and pane readiness, never from `HeadRun.working`.
    """

    handle: str = ""
    leaf: str = ""
    connected: bool | None = None
    readiness: str = ""
    last_output_at: float = 0.0
    busy: bool | None = None


@dataclass(frozen=True)
class DrainReceipt(HeadReceipt):
    """A request that a head take no more work.

    `draining`: this runtime hands the head no more work. `head_signalled`: the head itself was
    told to wind down. A backend reports only what it actually did.
    """

    draining: bool = False
    head_signalled: bool = False


@dataclass(frozen=True)
class StopReceipt(HeadReceipt):
    """A head ended, or a stop that could not be confirmed and what it left behind."""


@dataclass(frozen=True)
class AttachReceipt(HeadReceipt):
    """A caller joined to a live head's stream, or why this backend cannot join it.

    `handle` and `leaf` address the head in the backend's session even when no stream is given.
    """

    handle: str = ""
    leaf: str = ""


class HeadRuntime(Protocol):
    """One head backend as seen from above: six verbs, each with its own receipt.

    Implementations may take backend-specific keyword options; nothing above this boundary may
    reach a pane, session manager or pty directly.
    """

    def start(
        self,
        spec: HeadSpec,
        workspace: str,
        task_ref: TaskRef,
        *,
        command: str,
        title: str,
        pointer: NudgePointer | None = None,
        **options: Any,
    ) -> StartReceipt:
        """Bring one head up, and point it at its task when a pointer is given."""

    def deliver(
        self, run: HeadRun, pointer: NudgePointer, *, subject: str = "", **options: Any
    ) -> DeliverReceipt:
        """Put one prompt in front of a head that is already running."""

    def observe(self, run: HeadRun) -> ObserveReceipt:
        """Read what this backend can actually say about the head as it is now."""

    def request_drain(self, run: HeadRun, initiator: StopInitiator) -> DrainReceipt:
        """Ask this head to take no more work, and say how much of that was really done."""

    def stop(self, run: HeadRun, initiator: StopInitiator, **options: Any) -> StopReceipt:
        """End this head, recording who ended it."""

    def attach(self, run: HeadRun) -> AttachReceipt:
        """Join a caller to this head's live stream, or say that this backend has none."""
