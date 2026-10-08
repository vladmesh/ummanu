"""ummanu-140: a production prompt handoff costs a tick at most its shared budget, and resumes.

sprint:1484 measured the slow reconcile ticks inside one card's advance: a red-gate continuation
and two reviewer launches, each 19-21 s. The head's own journal puts a large share of that in the
prompt waits of `_deliver_prompt`: at least `PROMPT_QUIET_SECONDS` of watched silence before the
line is typed (even into a head suspended for ten minutes), the echo turn's `TURN_QUIET_SECONDS`
before the submit, and the submit's confirmation; a reviewer adds the head's startup output before
it is quiet. `_handoff_prompt` decides each of those stages by one observation and lets a stage
wait only out of the tick's `HandoffBudget`, continuing next tick from the journal.

The head here is a scripted supervisor on a fake clock: its journal is built the way
`local_pty.supervisor` builds one (`input.accepted`, `turn.started`, `provider.progressed`,
`turn.finished` after two quiet seconds), so the runtime's own journal reader is what decides.
No test sleeps.
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar
from unittest import mock

from ummanu.dispatch import launch, production, review as dispatcher_review
from ummanu.dispatch.host import CommandHostRuntime
from ummanu.dispatch.launch import LAUNCH_DELIVERY_HANDOFF_PENDING, launch_delivery
from ummanu.dispatch.state import DispatcherRecord
from ummanu.dispatch.tick_telemetry import card_details
from ummanu.dispatch.types import HeadLaunchAborted, HostError
from ummanu.runtime.head import HeadRun, HeadSpec, TaskRef
from ummanu.runtime.head.handoff import (
    HANDOFF_SETTLE,
    HANDOFF_SUBMITTED,
    HANDOFF_TYPED,
    PromptHandoff,
    handoff_budget,
    handoff_pending_stage,
)
from ummanu.runtime.head.local_pty import JournalReadResult
from ummanu.runtime.head.operations import NudgePointer
from ummanu.runtime.head.runtime import (
    HEAD_ALIVE,
    HEAD_BUSY,
    HEAD_OK,
    DeliverReceipt,
    ObserveReceipt,
    StartReceipt,
)
from ummanu.runtime.local_pty_head import (
    DELIVER_HANDOFF_UNESTABLISHED,
    DELIVER_NOT_SUBMITTED,
    DELIVERY_ARRIVED,
    SUBMIT_KEY,
    DeliveryReport,
    LocalPtyHeadRuntime,
    _handoff_progress,
)

EPOCH = 1_800_000_000.0
QUIET = 2.0  # local_pty.supervisor.TURN_QUIET_SECONDS
SUBJECT = "worker-continuation"
POINTER = NudgePointer.at_document("/tmp/TASK.md", "Generation 4: read TASK.md again.")


class FakeClock:
    """Monotonic and wall time that only move when somebody waits or a test advances them."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def wall(self) -> float:
        return EPOCH + self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, seconds)

    def time(self) -> float:
        return self.wall()

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class ScriptedHead:
    """A head behind a supervisor, scripted on the fake clock.

    `prints_until`: the head keeps printing (outside any turn) until then, like a TUI starting.
    `takes_enter`: a submit makes it print a taken prompt and keep working; otherwise an Enter
    redraws a cursor and nothing more. `idle_field`: whether its supervisor reports
    `output_idle_seconds` (supervisors started before ummanu-140 do not).
    """

    clock: FakeClock
    idle_field: bool = True
    prints_until: float = 0.0
    takes_enter: bool = True
    printed: int = 4096
    events: list[dict[str, Any]] = field(default_factory=list)
    seq: int = 2
    turn: int = 0
    turn_open: bool = False
    turn_bytes: int = 0
    last_output: float = -600.0
    scheduled: list[tuple[float, int]] = field(default_factory=list)
    accepted: list[str] = field(default_factory=list)
    woken: int = 0

    def __post_init__(self) -> None:
        t = 0.0
        while t < self.prints_until:
            self.scheduled.append((t, 64))
            t += 0.5
        self.events = [
            {"seq": 1, "kind": "scope.bound", "at": EPOCH - 600},
            {"seq": 2, "kind": "run.started", "at": EPOCH - 600},
        ]

    def _append(self, kind: str, at: float, **fields: Any) -> None:
        self.seq += 1
        self.events.append({"seq": self.seq, "kind": kind, "at": EPOCH + at, **fields})

    def _close_turn_by(self, at: float) -> None:
        if self.turn_open and at - self.last_output >= QUIET:
            self._append(
                "turn.finished",
                self.last_output + QUIET,
                turn=self.turn,
                output_bytes=self.turn_bytes,
                reason="quiet",
                quiet_seconds=QUIET,
            )
            self.turn_open = False

    def advance(self) -> None:
        now = self.clock.now
        self.scheduled.sort()
        while self.scheduled and self.scheduled[0][0] <= now:
            at, amount = self.scheduled.pop(0)
            self._close_turn_by(at)
            self.printed += amount
            self.last_output = at
            if self.turn_open:
                self.turn_bytes += amount
                self._append("provider.progressed", at, turn=self.turn, output_bytes=amount)
        self._close_turn_by(now)

    def status(self) -> dict[str, Any]:
        self.advance()
        status = {
            "alive": True,
            "turn_open": self.turn_open,
            "turn": self.turn,
            "output_bytes": self.printed,
            "journal_seq": self.seq,
        }
        if self.idle_field:
            status["output_idle_seconds"] = round(self.clock.now - self.last_output, 3)
        return status

    def deliver(self, subject: str, payload: bytes, wake: Any) -> tuple[str, int]:
        self.advance()
        if self.turn_open:
            return HEAD_BUSY, 0
        if wake is not None:
            wake()
            self.woken += 1
        now = self.clock.now
        self.accepted.append(subject)
        self._append(
            "input.accepted",
            now,
            subject=subject,
            bytes=len(payload),
            offered_bytes=len(payload),
            complete=True,
            state="complete",
            delivery=len(self.accepted),
        )
        self.turn += 1
        self.turn_open, self.turn_bytes, self.last_output = True, 0, now
        self._append("turn.started", now, turn=self.turn, subject=subject)
        if payload == SUBMIT_KEY:
            if self.takes_enter:
                # A taken prompt redraws kilobytes, and the provider keeps printing while it works.
                self.scheduled += [(now + 0.5 + step, 1500) for step in range(30)]
            else:
                self.scheduled.append((now + 0.1, 12))
        else:
            self.scheduled.append((now + 0.1, len(payload)))
        return HEAD_OK, len(payload)

    def read(self) -> JournalReadResult:
        self.advance()
        return JournalReadResult(events=tuple(self.events))


class ScriptedRuntime(LocalPtyHeadRuntime):
    """`LocalPtyHeadRuntime` with its three witnesses (status, input, journal) answered by the script."""

    def __init__(self, root: Path, head: ScriptedHead, clock: FakeClock) -> None:
        super().__init__(
            root,
            head_process_status=lambda *_args, **_kwargs: {},
            monotonic=clock.monotonic,
            wall=clock.wall,
            sleep=clock.sleep,
        )
        self.head = head
        self.observations = 0

    def observe(self, run: HeadRun) -> ObserveReceipt:
        self.observations += 1
        status = self.head.status()
        return ObserveReceipt(status=HEAD_OK, run=run, evidence=status, busy=status["turn_open"])

    def _deliver_payload(self, run, pointer, subject, payload=None, wake=None) -> DeliverReceipt:
        data = payload if payload is not None else (pointer.text + "\n").encode()
        status, written = self.head.deliver(subject, data, wake)
        if status != HEAD_OK:
            return DeliverReceipt(status=status, run=run, reason="this head is running a turn")
        report = DeliveryReport(
            outcome=DELIVERY_ARRIVED,
            state="complete",
            written=written,
            offered=written,
            journalled=True,
            seq=self.head.seq,
        )
        return DeliverReceipt(
            status=HEAD_OK,
            run=run,
            evidence=report,
            delivered_bytes=written,
            offered_bytes=written,
            delivery_state="complete",
        )

    def _handoff_read(self, run, subject, floor):
        return _handoff_progress(self.head.read(), subject, floor)

    def _durable_epoch(self, run, probe=None) -> int:
        return self.head.seq


class HandoffTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.clock = FakeClock()
        self.run_ = HeadRun(
            run_id="5e0942743ede48869b67e848503f0c49",
            spec=HeadSpec(profile_id="codex", adapter="codex"),
            workspace="/tmp/workspace",
            task_ref=TaskRef.card("codegen-orchestrator-1558"),
            role="worker",
        )

    def head(self, **options: Any) -> ScriptedHead:
        return ScriptedHead(self.clock, **options)

    def runtime(self, head: ScriptedHead) -> ScriptedRuntime:
        # Every tick is a new dispatcher process: a new runtime remembers nothing of the last one.
        return ScriptedRuntime(self.root, head, self.clock)

    def handoff(self, head: ScriptedHead) -> PromptHandoff:
        return PromptHandoff(floor=head.seq, began_at=self.clock.wall())

    def tick(self, head: ScriptedHead, handoff: PromptHandoff, *, budget: float = 4.0, wake=None):
        """One production pass over this handoff: the receipt, the budget spent, the clock it took."""
        started = self.clock.now
        with handoff_budget(budget, clock=self.clock.monotonic) as spent:
            receipt = self.runtime(head).deliver(
                self.run_, POINTER, subject=SUBJECT, transport=Transport(wake), handoff=handoff
            )
        self.assertLessEqual(spent.spent, budget + 1e-9, "the shared budget was overdrawn")
        return receipt, spent, self.clock.now - started


@dataclass
class Transport:
    before_send: Any = None


class AQuietHeadIsHandedOffInsideTheBudgetTests(HandoffTestCase):
    """The 06:39:08 case: a retained worker, quiet for ten minutes, takes its continuation."""

    def test_one_tick_types_submits_and_confirms_within_the_budget(self) -> None:
        head = self.head()
        receipt, budget, took = self.tick(head, self.handoff(head))
        self.assertEqual(receipt.status, HEAD_OK, receipt.reason)
        self.assertTrue(receipt.delivery.evidence.turn_confirmed)
        self.assertEqual(head.accepted, [SUBJECT, f"{SUBJECT}:submit"])
        # Settle is one status: no watched `PROMPT_QUIET_SECONDS` over a head that has long been quiet.
        stages = {entry["stage"]: entry for entry in budget.stages}
        self.assertEqual(stages["settle"]["ms"], 0.0)
        self.assertEqual(stages["settle"]["outcome"], "quiet")
        # What is left is the echo turn's own quiet close and the provider's first output.
        self.assertAlmostEqual(stages["echo"]["ms"], 2100.0, delta=260.0)
        self.assertLess(stages["confirm"]["ms"], 760.0)
        self.assertLessEqual(took, 4.0)

    def test_with_no_budget_each_tick_moves_it_one_stage_and_nothing_is_sent_twice(self) -> None:
        head = self.head()
        handoff = self.handoff(head)
        first, _, took = self.tick(head, handoff, budget=0.0, wake=lambda: None)
        self.assertEqual(took, 0.0, "a stage that cannot be decided now is not waited on")
        self.assertEqual(first.status, HEAD_BUSY)
        self.assertEqual(first.handoff_stage, HANDOFF_TYPED)
        self.assertEqual(handoff_pending_stage(first.evidence), HANDOFF_TYPED)
        self.assertIsNone(first.failure)
        self.clock.advance(60.0)
        second, _, took = self.tick(head, handoff, budget=0.0, wake=lambda: None)
        self.assertEqual(took, 0.0)
        self.assertEqual(second.handoff_stage, HANDOFF_SUBMITTED)
        self.clock.advance(60.0)
        third, _, _ = self.tick(head, handoff, budget=0.0, wake=lambda: None)
        self.assertEqual(third.status, HEAD_OK, third.reason)
        self.assertTrue(third.delivery.evidence.turn_confirmed)
        self.assertEqual(
            head.accepted, [SUBJECT, f"{SUBJECT}:submit"], "one line, one Enter, across three ticks"
        )
        self.assertEqual(head.woken, 1, "the wake runs once, before the only typed line")

    def test_a_confirmed_handoff_asked_again_sends_nothing(self) -> None:
        head = self.head()
        handoff = self.handoff(head)
        self.assertEqual(self.tick(head, handoff)[0].status, HEAD_OK)
        self.clock.advance(60.0)
        again, budget, _ = self.tick(head, handoff)
        self.assertEqual(again.status, HEAD_OK, again.reason)
        self.assertEqual(head.accepted, [SUBJECT, f"{SUBJECT}:submit"])
        self.assertEqual(budget.spent, 0.0)


class TheBlockingFlowItReplacesTests(HandoffTestCase):
    """Before: what `_deliver_prompt` costs on the same scripted heads, on the same fake clock.

    These are the stage costs the card measured the change against. The blocking flow watches a
    head that has been quiet for ten minutes for another `PROMPT_QUIET_SECONDS`, waits out the echo
    turn's quiet close, and polls for the provider's output; three such cards in one tick add up.
    """

    def blocking(self, head: ScriptedHead) -> float:
        started = self.clock.now
        with mock.patch("ummanu.runtime.local_pty_head.time", self.clock):
            receipt = self.runtime(head).deliver(self.run_, POINTER, subject=SUBJECT, transport=Transport())
        self.assertEqual(receipt.status, HEAD_OK, receipt.reason)
        return self.clock.now - started

    def test_a_quiet_retained_head_costs_the_settle_echo_and_confirm_waits(self) -> None:
        took = self.blocking(self.head())
        # settle 4.0 (watched quiet) + echo ~2.1 (TURN_QUIET_SECONDS after the echo) + confirm ~0.5
        self.assertGreaterEqual(took, 6.5)
        self.assertLessEqual(took, 7.25)

    def test_a_starting_reviewer_costs_its_startup_output_too(self) -> None:
        took = self.blocking(self.head(prints_until=4.0))
        # Its last startup output at 3.5 s, quiet by 7.5 s (production 06:53: 7.6 s), then echo and confirm.
        self.assertGreaterEqual(took, 10.0)

    def test_three_cards_sum_their_waits_where_the_handoff_shares_one_budget(self) -> None:
        blocking = sum(self.blocking(self.head()) for _ in range(3))
        self.assertGreater(blocking, 19.5)
        started = self.clock.now
        with handoff_budget(4.0, clock=self.clock.monotonic):
            for head in (self.head(), self.head(), self.head()):
                self.runtime(head).deliver(
                    self.run_, POINTER, subject=SUBJECT, transport=Transport(), handoff=self.handoff(head)
                )
        self.assertLessEqual(self.clock.now - started, 4.0)


class AHeadThatIsNotReadyIsNotWaitedOnTests(HandoffTestCase):
    """`PROMPT_FIRST_OUTPUT_SECONDS`/`PROMPT_SETTLE_SECONDS` become ticks, never a 20/90 s wait."""

    def test_a_starting_head_is_typed_into_on_the_tick_it_is_quiet(self) -> None:
        head = self.head(prints_until=30.0)
        handoff = self.handoff(head)
        first, budget, took = self.tick(head, handoff)
        self.assertEqual(first.handoff_stage, HANDOFF_SETTLE)
        self.assertLessEqual(took, 4.0)
        self.assertEqual(head.accepted, [], "nothing is typed into a head still printing")
        self.assertEqual(budget.stages[-1]["outcome"], "pending")
        self.clock.advance(60.0)
        second, _, _ = self.tick(head, handoff)
        self.assertEqual(second.status, HEAD_OK, second.reason)
        self.assertEqual(head.accepted, [SUBJECT, f"{SUBJECT}:submit"])

    def test_a_head_that_never_stops_printing_is_typed_into_past_the_settle_bound(self) -> None:
        head = self.head(prints_until=400.0)
        handoff = self.handoff(head)
        for _ in range(2):
            receipt, _, took = self.tick(head, handoff)
            self.assertEqual(receipt.handoff_stage, HANDOFF_SETTLE)
            self.assertLessEqual(took, 4.0)
            self.clock.advance(60.0)
        # 120 s after the handoff opened: past `PROMPT_SETTLE_SECONDS`, as `_await_settled` gives up.
        receipt, _, _ = self.tick(head, handoff)
        self.assertEqual(head.accepted[:1], [SUBJECT])
        self.assertIn(receipt.handoff_stage, {HANDOFF_TYPED, HANDOFF_SUBMITTED, ""})

    def test_an_older_supervisor_is_watched_for_quiet_only_out_of_the_budget(self) -> None:
        head = self.head(idle_field=False)
        handoff = self.handoff(head)
        starved, _, took = self.tick(head, handoff, budget=1.0)
        self.assertEqual(starved.handoff_stage, HANDOFF_SETTLE)
        self.assertLessEqual(took, 1.0)
        self.assertEqual(head.accepted, [])
        receipt, _, took = self.tick(head, handoff, budget=8.0)
        self.assertIn(receipt.handoff_stage, {"", HANDOFF_TYPED, HANDOFF_SUBMITTED})
        self.assertEqual(head.accepted[:1], [SUBJECT])
        self.assertLessEqual(took, 8.0)

    def test_a_head_in_a_turn_is_refused_busy_as_before_and_nothing_is_typed(self) -> None:
        head = self.head()
        head.turn_open, head.turn, head.last_output = True, 9, 0.0
        head.scheduled += [(step * 0.5, 100) for step in range(1, 40)]
        receipt, _, _ = self.tick(head, self.handoff(head))
        self.assertEqual(receipt.status, HEAD_BUSY)
        self.assertEqual(receipt.handoff_stage, "", "a busy pane is the refusal it always was")
        self.assertEqual(head.accepted, [])


class ASubmitIsSentAtMostTwiceTests(HandoffTestCase):
    def test_an_enter_that_starts_nothing_is_retried_once_then_reported_not_submitted(self) -> None:
        head = self.head(takes_enter=False)
        handoff = self.handoff(head)
        receipts = []
        for _ in range(4):
            receipts.append(self.tick(head, handoff)[0])
            self.clock.advance(60.0)
        final = receipts[-1]
        self.assertEqual(final.status, HEAD_ALIVE)
        self.assertEqual(final.reason, DELIVER_NOT_SUBMITTED)
        self.assertTrue(final.evidence.payload_left_in_composer)
        self.assertEqual(final.evidence.submit_count, 2)
        self.assertEqual(head.accepted, [SUBJECT, f"{SUBJECT}:submit", f"{SUBJECT}:submit"])


class ARestartedDispatcherContinuesTheSameHandoffTests(HandoffTestCase):
    """The journal is the cursor: a new process after the pointer, the submit or the confirmation."""

    def test_after_the_pointer_it_submits_and_does_not_type_again(self) -> None:
        head = self.head()
        handoff = self.handoff(head)
        self.runtime(head)._deliver_payload(self.run_, POINTER, SUBJECT)  # the dying tick's line
        self.clock.advance(60.0)
        receipt, _, _ = self.tick(head, handoff)
        self.assertEqual(receipt.status, HEAD_OK, receipt.reason)
        self.assertEqual(head.accepted, [SUBJECT, f"{SUBJECT}:submit"])

    def test_after_the_submit_it_confirms_from_the_journal_and_sends_no_second_enter(self) -> None:
        head = self.head()
        handoff = self.handoff(head)
        runtime = self.runtime(head)
        runtime._deliver_payload(self.run_, POINTER, SUBJECT)
        self.clock.advance(3.0)
        runtime._deliver_payload(self.run_, POINTER, f"{SUBJECT}:submit", SUBMIT_KEY)
        self.clock.advance(60.0)
        receipt, budget, _ = self.tick(head, handoff)
        self.assertEqual(receipt.status, HEAD_OK, receipt.reason)
        self.assertEqual(head.accepted, [SUBJECT, f"{SUBJECT}:submit"])
        self.assertEqual(budget.spent, 0.0, "a turn the journal already shows taken is not waited on")

    def test_a_previous_rounds_handoff_below_the_floor_is_not_this_one(self) -> None:
        head = self.head()
        old = self.handoff(head)
        self.assertEqual(self.tick(head, old)[0].status, HEAD_OK)
        self.clock.advance(600.0)
        head.advance()
        new = self.handoff(head)
        receipt, _, _ = self.tick(head, new)
        self.assertEqual(receipt.status, HEAD_OK, receipt.reason)
        self.assertEqual(head.accepted, [SUBJECT, f"{SUBJECT}:submit"] * 2)

    def test_a_journal_that_no_longer_reaches_the_floor_is_refused_not_retyped(self) -> None:
        head = self.head()
        handoff = PromptHandoff(floor=0, began_at=self.clock.wall())

        class Cut(ScriptedRuntime):
            def _handoff_read(self, run, subject, floor):
                read = self.head.read()
                return _handoff_progress(
                    JournalReadResult(events=read.events[1:], partial_head=True), subject, floor
                )

        with handoff_budget(4.0, clock=self.clock.monotonic):
            receipt = Cut(self.root, head, self.clock).deliver(
                self.run_, POINTER, subject=SUBJECT, transport=Transport(), handoff=handoff
            )
        self.assertEqual(receipt.status, HEAD_ALIVE)
        self.assertTrue(receipt.reason.startswith(DELIVER_HANDOFF_UNESTABLISHED))
        self.assertEqual(handoff_pending_stage(receipt), "")
        self.assertEqual(head.accepted, [])

    def test_a_line_left_in_part_is_the_fatal_prefix_it_always_was(self) -> None:
        head = self.head()
        handoff = self.handoff(head)
        head.seq += 1
        head.events.append(
            {
                "seq": head.seq,
                "kind": "input.accepted",
                "at": EPOCH,
                "subject": SUBJECT,
                "bytes": 7,
                "offered_bytes": 60,
                "complete": False,
                "state": "stalled",
            }
        )

        class NoSupervisor(ScriptedRuntime):
            def _close_substrate_admission(self, run, initiator):
                return False, None, self.head.seq, None

        receipt = NoSupervisor(self.root, head, self.clock).deliver(
            self.run_, POINTER, subject=SUBJECT, transport=Transport(), handoff=handoff
        )
        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.handoff_stage, "")
        self.assertEqual(receipt.status, HEAD_ALIVE)
        self.assertEqual(head.accepted, [])


class ThreeCardsShareOneBudgetTests(HandoffTestCase):
    """0-3 active records, one slow: the tick's waiting is bounded by structure, not by luck."""

    def test_a_slow_peer_spends_the_budget_and_the_others_still_move_every_tick(self) -> None:
        slow = self.head(prints_until=100.0)
        quick = [self.head(), self.head()]
        handoffs = {id(head): self.handoff(head) for head in (slow, *quick)}
        done: set[int] = set()
        for tick in range(5):
            started = self.clock.now
            with handoff_budget(4.0, clock=self.clock.monotonic) as budget:
                for head in (slow, *quick):  # board order puts the slow one first
                    receipt = self.runtime(head).deliver(
                        self.run_, POINTER, subject=SUBJECT, transport=Transport(), handoff=handoffs[id(head)]
                    )
                    if receipt.ok:
                        done.add(id(head))
            self.assertLessEqual(self.clock.now - started, 4.0 + 1e-9, f"tick {tick} waited past its budget")
            self.assertLessEqual(budget.spent, 4.0 + 1e-9)
            if tick == 0:
                self.assertEqual(slow.accepted, [], "the slow head is not typed into while it prints")
                self.assertTrue(
                    all(head.accepted[:1] == [SUBJECT] for head in quick),
                    "the quick peers are not starved by the slow one",
                )
            self.clock.advance(60.0)
        self.assertEqual(done, {id(head) for head in quick} | {id(slow)})
        for head in (slow, *quick):
            self.assertEqual(head.accepted, [SUBJECT, f"{SUBJECT}:submit"])


class ALaunchWithAPendingHandoffKeepsItsHeadTests(HandoffTestCase):
    """A reviewer raised this tick is not quiet yet: the bring-up is kept, never abandoned for it."""

    def test_start_answers_busy_with_the_stage_and_does_not_stop_the_head(self) -> None:
        head = self.head(prints_until=8.0)
        runtime = self.runtime(head)
        abandoned = []
        with (
            mock.patch.object(
                ScriptedRuntime, "_start_locked", return_value=StartReceipt(status=HEAD_OK, run=self.run_)
            ),
            mock.patch.object(
                ScriptedRuntime, "_abandon_bring_up", side_effect=lambda *a: abandoned.append(a)
            ),
            handoff_budget(4.0, clock=self.clock.monotonic),
        ):
            receipt = runtime.start(
                self.run_.spec,
                self.run_.workspace,
                self.run_.task_ref,
                command="codex",
                title="",
                pointer=POINTER,
                transport=Transport(),
                subject="reviewer-launch",
                handoff=PromptHandoff(floor=0, began_at=self.clock.wall()),
            )
        self.assertEqual(receipt.status, HEAD_BUSY)
        self.assertEqual(receipt.handoff_stage, HANDOFF_SETTLE)
        self.assertEqual(receipt.run, self.run_)
        self.assertEqual(abandoned, [])
        self.assertEqual(head.accepted, [])


class ReviewerLaunchHandoffTests(unittest.TestCase):
    """The intent keeps a pending reviewer handoff: no attempt is spent and no failure is counted."""

    EVIDENCE: ClassVar[dict[str, str]] = {
        "subject": "reviewer-launch",
        "stage": "none",
        "handoff_stage": "settle",
        "reason": "the head has not been seen quiet yet",
    }

    def setUp(self) -> None:
        self.task = {"ref": "sample-1"}
        self.record = DispatcherRecord(
            worker="worker-1",
            workspace="/unused",
            handle="",
            head="codex",
            review_head="claude",
            attempt_id="attempt-1",
            comment_baseline=0,
            review_baseline=0,
            state="review_starting",
            claimed_at=1.0,
        )
        self.record.launch_intent = {
            "role": "review",
            "head": "claude",
            "at": EPOCH,
            "run_id": "r1",
            "pid_file": "/tmp/r1.pid",
            "task": "card:sample-1",
        }
        self.records = {"sample-1": self.record}
        self.payload: dict[str, Any] = {}
        self.runtime = mock.Mock()

    def pending(self, evidence=None) -> HostError:
        error = HostError("retained reviewer document nudge is pending: production handoff pending at settle")
        error.evidence = dict(evidence or self.EVIDENCE)
        return error

    def test_a_pending_bring_up_keeps_the_exact_head_and_spends_no_attempt(self) -> None:
        aborted = HeadLaunchAborted(
            "reviewer-launch is pending",
            handle="",
            leaf="r1",
            workspace="/unused",
            pid_file="/tmp/r1.pid",
            evidence=dict(self.EVIDENCE),
            head_run={
                "run_id": "r1",
                "workspace": "/unused",
                "task_ref": {"kind": "card", "ref": "sample-1"},
                "role": "reviewer",
                "spec": {"profile_id": "claude", "adapter": "claude"},
            },
        )
        dispatcher_review._record_review_delivery_failure(self.record, aborted)
        result = dispatcher_review._reviewer_launch_aborted(
            self.runtime,
            self.task,
            self.records,
            "sample-1",
            self.record,
            "attempt-1",
            aborted,
            payload=self.payload,
        )
        self.assertEqual(result["action"], "review-launch-handoff-pending")
        self.assertEqual(result["status"], "ok")
        delivery = launch_delivery(self.record.launch_intent)
        self.assertEqual(delivery["state"], LAUNCH_DELIVERY_HANDOFF_PENDING)
        self.assertEqual(delivery["attempts"], 0)
        self.assertEqual(delivery["next_at"], 0.0)
        self.assertEqual(self.record.launch_intent["head_run"]["run_id"], "r1")
        self.assertEqual(self.record.review_delivery_failures, 0)
        self.assertEqual(self.record.review_launch_aborts, 0)

    def test_the_retry_continues_it_and_writes_only_when_its_stage_moved(self) -> None:
        self.record.launch_intent = {
            **self.record.launch_intent,
            "aborted": True,
            "delivery": {
                "state": LAUNCH_DELIVERY_HANDOFF_PENDING,
                "attempts": 0,
                "next_at": 0.0,
                "evidence": dict(self.EVIDENCE),
                "handoff": True,
            },
        }
        self.runtime.host.nudge_review_delivery.side_effect = self.pending()
        retry = lambda: dispatcher_review.retry_busy_reviewer_launch_delivery(
            self.runtime,
            self.task,
            self.records,
            self.payload,
            self.record,
            dict(self.record.launch_intent),
            "review",
        )
        result = retry()
        self.assertEqual(result["action"], "review-launch-handoff-pending")
        self.runtime.save_records.assert_not_called()
        self.runtime.host.nudge_review_delivery.side_effect = self.pending(
            {**self.EVIDENCE, "handoff_stage": "typed"}
        )
        self.assertEqual(retry()["handoff_stage"], "typed")
        self.runtime.save_records.assert_called_once()
        self.assertEqual(launch_delivery(self.record.launch_intent)["attempts"], 0)
        self.assertEqual(self.record.review_delivery_failures, 0)


class AFinishedHandoffAdoptsLikeStartReviewTests(unittest.TestCase):
    """A reviewer launch a handoff finished keeps a retained worker suspended, as `start_review` does."""

    def setUp(self) -> None:
        self.record = DispatcherRecord(
            worker="worker-1",
            workspace="/unused",
            handle="worker-1",
            head="codex",
            review_head="claude",
            attempt_id="attempt-1",
            comment_baseline=0,
            review_baseline=0,
            state="review_starting",
            claimed_at=1.0,
        )
        self.record.worker_continuation.begin_retention(1.0)
        self.record.worker_continuation.confirm_validation_move()
        self.runtime = mock.Mock()
        self.runtime.host.worker_retained_vanished.return_value = False

    def kept(self, delivery: dict[str, Any]) -> bool:
        return launch._retained_worker_kept_for_review(
            self.runtime, self.record, {"role": "review", "delivery": delivery}
        )

    def test_only_a_handoff_launch_with_a_confirmed_suspension_keeps_the_worker(self) -> None:
        self.assertTrue(self.kept({"state": "confirmed", "handoff": True}))
        self.runtime.host.confirm_worker_retained.assert_called_once_with(self.record)
        self.assertFalse(self.kept({"state": "confirmed"}), "a crash or busy adoption still stops it")
        self.runtime.host.confirm_worker_retained.side_effect = HostError("not suspended")
        self.assertFalse(self.kept({"state": "confirmed", "handoff": True}), "an unconfirmed one is stopped")


class TheHostCarriesTheHandoffTests(unittest.TestCase):
    """`resume_worker`/`_nudge_worker`: a started handoff is continued, a pending one is typed evidence."""

    def setUp(self) -> None:
        self.workspace = Path(tempfile.mkdtemp())
        self.host = mock.Mock(spec=CommandHostRuntime)
        self.host._head_status.return_value = {"alive": True, "match": True, "stopped": False, "known": True}
        self.host._continuation_addressable.return_value = True
        self.host._prompt_adapter.return_value = "codex"
        self.record = DispatcherRecord(
            worker="worker-1",
            workspace=str(self.workspace),
            handle="worker-1",
            head="codex",
            review_head="claude",
            attempt_id="attempt-1",
            comment_baseline=0,
            review_baseline=0,
            state="validate",
            claimed_at=1.0,
            report_generation=4,
        )
        self.record.worker_continuation.begin_retention(1.0)
        self.record.worker_continuation.confirm_validation_move()
        self.record.worker_continuation.begin_delivery("gate", 2.0)
        self.record.worker_continuation.open_handoff(41, EPOCH)

    def test_a_started_handoff_is_continued_without_rewriting_or_clearing_anything(self) -> None:
        self.host.worker_handoff_started.return_value = "started"
        with mock.patch("ummanu.dispatch.host._heartbeat_is_live_match", return_value=True):
            CommandHostRuntime.resume_worker(self.host, {"ref": "sample-1", "project": "p"}, self.record)
        self.host._write_prompt.assert_not_called()
        self.host._clear_report_bodies.assert_not_called()
        nudge = self.host._nudge_worker.call_args
        self.assertEqual(nudge.kwargs["handoff"], self.record.worker_continuation.handoff)
        self.assertNotIn("before_send", nudge.kwargs, "nothing is woken twice")

    def test_a_pending_receipt_is_raised_with_its_stage_and_the_run_it_bound(self) -> None:
        bound = HeadRun(
            run_id="w1",
            spec=HeadSpec(profile_id="claude", adapter="claude"),
            workspace=str(self.workspace),
            task_ref=TaskRef.card("sample-1"),
            role="worker",
        )
        self.host.worker_lifecycle_run.return_value = bound
        self.host._codex_provider_ingress.return_value = None
        pending = DeliverReceipt(
            status=HEAD_BUSY,
            run=bound,
            reason="production handoff pending at typed",
            handoff_stage=HANDOFF_TYPED,
            evidence=mock.Mock(to_json=lambda: {"stage": "payload_written", "handoff_stage": HANDOFF_TYPED}),
        )
        self.host.head_runtime_for.return_value.deliver.return_value = pending
        with self.assertRaises(HostError) as raised:
            CommandHostRuntime._nudge_worker(
                self.host,
                self.record,
                POINTER,
                "retained worker continuation",
                subject=SUBJECT,
                handoff=self.record.worker_continuation.handoff,
            )
        self.assertEqual(handoff_pending_stage(raised.exception), HANDOFF_TYPED)
        self.assertEqual(self.record.worker_head_run["run_id"], "w1")
        self.assertEqual(
            self.host.head_runtime_for.return_value.deliver.call_args.kwargs["handoff"],
            self.record.worker_continuation.handoff,
        )


class EachCardCarriesItsHandoffStagesTests(unittest.TestCase):
    def test_tick_card_attaches_the_stages_its_advance_ran_and_readers_bound_them(self) -> None:
        clock = FakeClock()
        with production.tick_clock(), handoff_budget(4.0, clock=clock.monotonic) as budget:
            with production.tick_card("quiet-1", 1):
                pass
            with production.tick_card("slow-1", 1):
                budget.note(SUBJECT, "settle", 0.0, "quiet")
                budget.note(SUBJECT, "echo", 2100.0, "closed")
            cards = list(production._TICK_CARDS.get())
        self.assertNotIn("handoffs", cards[0])
        self.assertEqual([stage["stage"] for stage in cards[1]["handoffs"]], ["settle", "echo"])
        read = card_details([{**cards[1], "handoffs": cards[1]["handoffs"] * 10}])
        self.assertEqual(len(read[0]["handoffs"]), 8)
        self.assertEqual(
            read[0]["handoffs"][1], {"stage": "echo", "subject": SUBJECT, "ms": 2100.0, "outcome": "closed"}
        )


class TheJournalReaderTests(unittest.TestCase):
    """`_handoff_progress` is the only place a handoff's stage is recalled."""

    def events(self, *rows: tuple) -> JournalReadResult:
        out = []
        for seq, kind, extra in rows:
            out.append({"seq": seq, "kind": kind, "at": EPOCH + seq, **extra})
        return JournalReadResult(events=tuple(out))

    def test_a_submit_that_joined_an_open_turn_counts_only_what_followed_it(self) -> None:
        read = self.events(
            (5, "input.accepted", {"subject": SUBJECT, "bytes": 60, "complete": True, "state": "complete"}),
            (6, "turn.started", {"turn": 3, "subject": SUBJECT}),
            (7, "provider.progressed", {"turn": 3, "output_bytes": 900}),
            (8, "input.accepted", {"subject": f"{SUBJECT}:submit", "bytes": 1, "complete": True}),
            (9, "turn.finished", {"turn": 3, "output_bytes": 960}),
        )
        progress = _handoff_progress(read, SUBJECT, 4)
        self.assertEqual(progress.submits, 1)
        self.assertTrue(progress.submit_finished)
        self.assertFalse(progress.confirmed, "the echo's own bytes are not the submit's turn")

    def test_a_turn_total_counts_folded_progress(self) -> None:
        read = self.events(
            (5, "input.accepted", {"subject": SUBJECT, "bytes": 60, "complete": True, "state": "complete"}),
            (6, "turn.started", {"turn": 3, "subject": SUBJECT}),
            (7, "turn.finished", {"turn": 3, "output_bytes": 60}),
            (8, "input.accepted", {"subject": f"{SUBJECT}:submit", "bytes": 1, "complete": True}),
            (9, "turn.started", {"turn": 4, "subject": f"{SUBJECT}:submit"}),
            (10, "turn.finished", {"turn": 4, "output_bytes": 4000}),
        )
        self.assertTrue(_handoff_progress(read, SUBJECT, 4).confirmed)

    def test_a_restarted_supervisor_after_the_line_is_unknown(self) -> None:
        read = self.events(
            (5, "input.accepted", {"subject": SUBJECT, "bytes": 60, "complete": True, "state": "complete"}),
            (6, "run.started", {}),
        )
        self.assertTrue(_handoff_progress(read, SUBJECT, 4).unknown)

    def test_a_line_typed_twice_is_unknown(self) -> None:
        line = {"subject": SUBJECT, "bytes": 60, "complete": True, "state": "complete"}
        read = self.events((5, "input.accepted", line), (6, "input.accepted", line))
        self.assertTrue(_handoff_progress(read, SUBJECT, 4).unknown)


class PromptHandoffRecordTests(unittest.TestCase):
    def test_it_round_trips_and_refuses_what_it_cannot_trust(self) -> None:
        handoff = PromptHandoff(floor=250, began_at=EPOCH)
        self.assertEqual(PromptHandoff.from_json(handoff.to_json()), handoff)
        for bad in (
            {},
            {"floor": -1, "began_at": EPOCH},
            {"floor": True, "began_at": EPOCH},
            {"floor": 3, "began_at": 0},
            None,
            "x",
        ):
            self.assertIsNone(PromptHandoff.from_json(bad))


if __name__ == "__main__":
    unittest.main()
