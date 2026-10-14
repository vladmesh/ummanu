"""Prompt delivery and bounded production handoff, with interactive waits unchanged."""

from __future__ import annotations

import math
import time
from collections.abc import (
    Callable,
    Mapping,
)
from dataclasses import dataclass
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
from ummanu.runtime.head.operations import (
    HeadNudgeFailed,
    NudgePointer,
    post_delivery_run,
)
from ummanu.runtime.head.run import HeadRun
from ummanu.runtime.head.runtime import (
    HEAD_ALIVE,
    HEAD_BUSY,
    HEAD_DRAINING,
    HEAD_GONE,
    HEAD_OK,
    DeliverReceipt,
    HeadActivity,
)
from ummanu.runtime.local_pty_delivery import (
    DeliveryMixin,
    DeliveryReport,
    _journalled_report,
    _outcome_of,
)
from ummanu.runtime.local_pty_values import (
    DELIVER_DEFERRED,
    DELIVER_HANDOFF_UNESTABLISHED,
    DELIVER_NOT_ADMITTED,
    DELIVER_NOT_SUBMITTED,
    DELIVER_PENDING,
    DELIVERY_ARRIVED,
    DELIVERY_PENDING,
    DELIVERY_STATE_UNKNOWN,
    HANDOFF_ALLOWANCE_SPENT,
    SUBMIT_ATTEMPTS,
    SUBMIT_CONFIRM_BYTES,
    SUBMIT_KEY,
    _allowance_receipt,
    _in_flight,
    _output_of,
    _Probe,
    _wake_hook,
)
from ummanu.runtime.prompt_document import NUDGE_FILE_MODE
from ummanu.runtime.tui_delivery import (
    READINESS_BUSY,
    READINESS_UNKNOWN,
    STAGE_ENTER_ACCEPTED,
    STAGE_NONE,
    STAGE_PAYLOAD_WRITTEN,
    DeliveryEvidence,
    payload_fingerprint,
)


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
    if not read.covers_since(floor):
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


class PromptDeliveryMixin(DeliveryMixin):
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
        reason = (
            DELIVER_NOT_SUBMITTED if last.ok else f"{DELIVER_NOT_SUBMITTED}: {last.reason or last.status}"
        )
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
                        live,
                        pointer,
                        subject,
                        HANDOFF_SETTLE,
                        "the head has not been seen quiet yet",
                        seen,
                        progress,
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
                    live,
                    pointer,
                    subject,
                    stage,
                    "the typed line's echo turn has not closed yet",
                    seen,
                    progress,
                )
            if operation.remaining() < HANDOFF_EFFECT_RESERVE_SECONDS:
                return self._handoff_pending(
                    live, pointer, subject, stage, HANDOFF_ALLOWANCE_SPENT, seen, progress
                )
            began = operation.clock()
            last = self._deliver_payload(
                live, pointer, f"{subject}:submit", SUBMIT_KEY, operation=operation, probe=seen.probe
            )
            operation.note("submit", began, last.status if last.ok else last.reason.split(":")[0])
            if last.reason.startswith(DELIVER_DEFERRED):
                return self._handoff_pending(
                    live, pointer, subject, stage, HANDOFF_ALLOWANCE_SPENT, seen, progress
                )
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
            self._probe(address, operation) if address is not None and address.socket_path.exists() else None
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
            writing = (
                "line" if written_for == subject else "submit" if written_for == f"{subject}:submit" else ""
            )
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
        if (
            isinstance(idle, (int, float))
            and not isinstance(idle, bool)
            and math.isfinite(idle)
            and idle >= 0
        ):
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
                STAGE_ENTER_ACCEPTED
                if submits
                else STAGE_PAYLOAD_WRITTEN
                if report is not None
                else STAGE_NONE
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
            operation.note(
                "floor", began, "read" if status is not None else "deferred" if probe.timed_out else "none"
            )
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
