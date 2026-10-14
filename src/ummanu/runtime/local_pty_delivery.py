"""Delivery admission and its socket/journal witnesses; one outcome classifier."""

from __future__ import annotations

import time
from collections.abc import (
    Callable,
    Mapping,
)
from dataclasses import (
    dataclass,
    replace,
)
from typing import Any

from ummanu.runtime.head import local_pty
from ummanu.runtime.head.handoff import HandoffOperation
from ummanu.runtime.head.local_pty import protocol
from ummanu.runtime.head.operations import (
    HeadNudgeFailed,
    NudgePointer,
)
from ummanu.runtime.head.run import (
    HeadRun,
    StopInitiator,
)
from ummanu.runtime.head.runtime import (
    HEAD_ALIVE,
    HEAD_GONE,
    DeliverReceipt,
)
from ummanu.runtime.local_pty_values import (
    _UNREACHABLE,
    DELIVER_FAILED,
    DELIVER_PREFIX_IS_FATAL,
    DELIVER_STALLED,
    DELIVER_UNESTABLISHED,
    DELIVER_UNKNOWN_IS_FATAL,
    DELIVERY_ARRIVED,
    DELIVERY_LANDED_NOTHING,
    DELIVERY_LEFT_A_PREFIX,
    DELIVERY_PENDING,
    DELIVERY_STATE_UNKNOWN,
    DELIVERY_UNESTABLISHED,
    DRAIN_AFTER_PARTIAL_DELIVERY,
    DRAIN_AFTER_UNESTABLISHED_DELIVERY,
    FATAL_DELIVERY_OUTCOMES,
    OBSERVE_SUPERVISOR_UNREACHABLE,
    _Address,
    _admission_refusal,
    _allowance_refusal,
    _declared_bound,
    _is_transient_bound,
    _payload_of,
    _Probe,
    _Refusal,
    _refusal_detail,
    _RuntimeState,
    _stated_refusal,
    _timed_out,
)
from ummanu.runtime.prompt_document import NUDGE_FILE_MODE
from ummanu.runtime.tui_delivery import (
    DELIVERY_CONFIRMED,
    STAGE_ENTER_ACCEPTED,
    STAGE_PAYLOAD_WRITTEN,
    STAGE_TURN_OBSERVED,
    DeliveryEvidence,
    DeliveryOutcome,
    payload_fingerprint,
)


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
                else STAGE_ENTER_ACCEPTED
                if submits
                else STAGE_PAYLOAD_WRITTEN
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


class DeliveryMixin(_RuntimeState):
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
                    return _pending_report(
                        len(payload),
                        floor,
                        "the admission answer did not come back within this pass's allowance",
                    ), None
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
                return _pending_report(
                    offered,
                    floor,
                    "admitted, and still being written when this pass's allowance ran out",
                    last,
                    seq,
                )
            try:
                status = client.status()
            except _UNREACHABLE as exc:
                if _timed_out(exc):
                    return _pending_report(
                        offered,
                        floor,
                        "admitted, and the supervisor's answer did "
                        "not come back within this pass's allowance",
                        last,
                        seq,
                    )
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
            reading = local_pty.read_tail(address.journal_path)
        except OSError:
            return ()
        if not reading.covers_since(floor):
            return ()
        events = reading.of_kind(local_pty.INPUT_ACCEPTED)
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
