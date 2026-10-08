"""ummanu-142: a production prompt handoff costs a tick at most its shared allowance, and resumes.

sprint:1484 measured the slow reconcile ticks inside one card's advance: a red-gate continuation
and two reviewer launches, each 19-21 s, of which the prompt waits of `_deliver_prompt` were about
7 s and 10.5 s. `_handoff_prompt` is the one boundary a production handoff goes through: every
supervisor request (connect, status, admission, following the write) and every wait is cut to one
`HandoffOperation`, whose deadline is the card's fair share of the tick's `HandoffBudget`, and what
it really took is charged. What it cannot finish is pending at its stage, and the next tick
continues it from the head's own status and journal.

The head here is a scripted supervisor on a fake clock behind the real client seam
(`local_pty.SupervisorClient.connect`): `LocalPtyHeadRuntime`'s own `_connect`, `_probe`, `_put`,
`_follow` and journal reader run against it, and its journal is a real file in the record format
`local_pty.journal` writes. A request whose answer takes longer than the socket bound the runtime
set raises `TimeoutError` after that bound, as a socket does. No test sleeps.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Self
from unittest import mock

from ummanu.dispatch import launch, production, review as dispatcher_review
from ummanu.dispatch.host import CommandHostRuntime
from ummanu.dispatch.launch import (
    LAUNCH_DELIVERY_HANDOFF_PENDING,
    LAUNCH_DELIVERY_WORKER_FENCED,
    launch_delivery,
)
from ummanu.dispatch.state import DispatcherRecord
from ummanu.dispatch.tick_telemetry import card_details
from ummanu.dispatch.types import HeadLaunchAborted, HostError
from ummanu.runtime.head import HeadRun, HeadSpec, TaskRef, local_pty
from ummanu.runtime.head.handoff import (
    HANDOFF_DEFERRED,
    HANDOFF_NOT_STARTED,
    HANDOFF_SETTLE,
    HANDOFF_STARTED,
    HANDOFF_SUBMITTED,
    HANDOFF_TYPED,
    HandoffBudget,
    PromptHandoff,
    handoff_budget,
    handoff_pending_stage,
)
from ummanu.runtime.head.local_pty import JournalReadResult, protocol
from ummanu.runtime.head.local_pty.journal import JOURNAL_SCHEMA_VERSION
from ummanu.runtime.head.local_pty.supervisor import Supervisor
from ummanu.runtime.head.operations import NudgePointer
from ummanu.runtime.head.runtime import HEAD_ALIVE, HEAD_BUSY, HEAD_OK, DeliverReceipt, StartReceipt
from ummanu.runtime.local_pty_head import (
    DELIVER_HANDOFF_UNESTABLISHED,
    DELIVER_NOT_SUBMITTED,
    DELIVERY_PENDING,
    SUBMIT_KEY,
    LocalPtyHeadRuntime,
    _handoff_progress,
)
from ummanu.runtime.tui_delivery import READINESS_BUSY

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


class ScriptedSupervisor:
    """A head behind its supervisor, on the fake clock, answering the protocol's frames.

    It admits one payload at a time and writes it in `write_seconds` (a slow PTY reader), journals
    `input.accepted` when the write ends and opens a turn for it, and closes a turn after `QUIET`
    seconds without output, as `local_pty.supervisor` does. `prints_until`: the head prints
    (outside any turn) until then, like a TUI starting. `takes_enter`: a submit makes it print a
    taken prompt; otherwise an Enter redraws a cursor and nothing more. `idle_field`: whether its
    status reports `output_idle_seconds` (supervisors started before ummanu-140 do not).
    `status_delay`, `admission_delay`, `connect_delay`: how long each answer takes to come back;
    an answer slower than the socket bound is a `TimeoutError`, and an admission whose answer is
    lost was still admitted, since a supervisor acts on a request before it answers it.
    """

    def __init__(
        self,
        clock: FakeClock,
        run_dir: Path,
        run_id: str,
        *,
        idle_field: bool = True,
        prints_until: float = 0.0,
        takes_enter: bool = True,
        write_seconds: float = 0.02,
        status_delay: float = 0.002,
        admission_delay: float = 0.002,
        connect_delay: float = 0.001,
    ) -> None:
        self.clock = clock
        self.run_dir = run_dir
        self.run_id = run_id
        self.idle_field = idle_field
        self.takes_enter = takes_enter
        self.write_seconds = write_seconds
        self.status_delay = status_delay
        self.admission_delay = admission_delay
        self.connect_delay = connect_delay
        self.reachable = True
        # An unchanged screen (a spinner): the real supervisor folds its windows, journalling nothing.
        self.folding = False
        self.supervisor_pid, self.head_pid = 4001, 4002
        self.output_total = 4096
        self.last_output = -600.0
        self.turn = 0
        self.turn_open = False
        self.turn_bytes = 0
        self.delivery: dict[str, Any] | None = None
        self.delivery_seq = 0
        self.offered: list[str] = []
        self.accepted: list[str] = []
        self.connects = 0
        self.drained = False
        self.scheduled: list[tuple[float, int]] = []
        t = 0.0
        while t < prints_until:
            self.scheduled.append((t, 64))
            t += 0.5
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / protocol.SOCKET_NAME).touch()
        self.journal = run_dir / protocol.JOURNAL_NAME
        self.journal.write_text("")
        self.seq = 0
        self.append("scope.bound", -600.0)
        self.append("run.started", -600.0)

    # -- the journal, in `local_pty.journal`'s record format ------------------------------------

    def append(self, kind: str, at: float, **fields: Any) -> dict[str, Any]:
        self.seq += 1
        record = {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "seq": self.seq,
            "run_id": self.run_id,
            "kind": kind,
            "at": round(EPOCH + at, 6),
            **fields,
        }
        with self.journal.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
        return record

    # -- the supervisor's loop, caught up to the clock -----------------------------------------

    def advance(self) -> None:
        now = self.clock.now
        while True:
            moments = []
            if self.delivery is not None and self.delivery["state"] == protocol.DELIVERY_IN_FLIGHT:
                moments.append((self.delivery["admitted_at"] + self.write_seconds, "write"))
            if self.scheduled:
                moments.append((min(self.scheduled)[0], "output"))
            if self.turn_open:
                moments.append((self.last_output + QUIET, "quiet"))
            due = [moment for moment in moments if moment[0] <= now]
            if not due:
                return
            at, what = min(due)
            if what == "write":
                self._finish(at)
            elif what == "output":
                self.scheduled.sort()
                _, amount = self.scheduled.pop(0)
                self._print(at, amount)
            else:
                self.append(
                    "turn.finished",
                    at,
                    turn=self.turn,
                    output_bytes=self.turn_bytes,
                    reason="quiet",
                    quiet_seconds=QUIET,
                )
                self.turn_open = False

    def _print(self, at: float, amount: int) -> None:
        self.output_total += amount
        self.last_output = at
        if self.turn_open:
            self.turn_bytes += amount
            if not self.folding:
                self.append("provider.progressed", at, turn=self.turn, output_bytes=amount)

    def _finish(self, at: float) -> None:
        delivery = self.delivery
        assert delivery is not None
        delivery["state"] = protocol.DELIVERY_COMPLETE
        delivery["written_bytes"] = delivery["size_bytes"]
        self.accepted.append(delivery["subject"])
        self.append(
            "input.accepted",
            at,
            bytes=delivery["size_bytes"],
            offered_bytes=delivery["size_bytes"],
            complete=True,
            delivery=delivery["id"],
            state=protocol.DELIVERY_COMPLETE,
            subject=delivery["subject"],
            detail=f"all {delivery['size_bytes']} bytes reached the head's terminal",
        )
        if not self.turn_open:
            self.turn += 1
            self.turn_open, self.turn_bytes, self.last_output = True, 0, at
            self.append("turn.started", at, turn=self.turn, subject=delivery["subject"])
        if delivery["payload"] == SUBMIT_KEY:
            if self.takes_enter:
                # A taken prompt redraws kilobytes, and the provider keeps printing while it works.
                self.scheduled += [(at + 0.5 + step, 1500) for step in range(30)]
            else:
                self.scheduled.append((at + 0.1, 12))
        else:
            self.scheduled.append((at + 0.1, delivery["size_bytes"]))

    def view(self) -> dict[str, Any] | None:
        if self.delivery is None:
            return None
        return {key: value for key, value in self.delivery.items() if key not in {"payload", "admitted_at"}}

    # -- the verbs -----------------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        self.advance()
        status: dict[str, Any] = {
            "ok": True,
            "run_id": self.run_id,
            "supervisor_pid": self.supervisor_pid,
            "head_pid": self.head_pid,
            "alive": True,
            "draining": self.drained,
            "stopping": False,
            "turn_open": self.turn_open,
            "turn": self.turn,
            "delivery": self.view(),
            "journal_seq": self.seq,
            "output_bytes": self.output_total,
        }
        if self.idle_field:
            status["output_idle_seconds"] = round(max(0.0, self.clock.now - self.last_output), 3)
        return status

    def input(self, payload: bytes, subject: str) -> dict[str, Any]:
        self.advance()
        if self.drained:
            return {"ok": False, "error": protocol.ERROR_DRAINING, "detail": "admission is closed"}
        if self.delivery is not None and self.delivery["state"] == protocol.DELIVERY_IN_FLIGHT:
            return protocol.in_flight_refusal(self.view() or {})
        self.delivery_seq += 1
        self.offered.append(subject)
        self.delivery = {
            "id": self.delivery_seq,
            "state": protocol.DELIVERY_IN_FLIGHT,
            "size_bytes": len(payload),
            "written_bytes": 0,
            "complete": False,
            "subject": subject,
            "timeout_seconds": protocol.INPUT_DELIVERY_SECONDS,
            "detail": "",
            "payload": payload,
            "admitted_at": self.clock.now,
        }
        return {"ok": True, "accepted": True, "accepted_bytes": len(payload), "delivery": self.view(), "turn": self.turn}


class FakeClient:
    """The `SupervisorClient` surface the runtime uses, answering from a `ScriptedSupervisor`."""

    def __init__(self, supervisor: ScriptedSupervisor, timeout: float) -> None:
        self.supervisor = supervisor
        self.timeout = timeout

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def close(self) -> None:
        return None

    def set_timeout(self, timeout: float) -> None:
        self.timeout = timeout

    def _answer_after(self, delay: float) -> None:
        if delay > self.timeout:
            self.supervisor.clock.advance(self.timeout)
            raise TimeoutError("timed out")
        self.supervisor.clock.advance(delay)

    def status(self) -> dict[str, Any]:
        self._answer_after(self.supervisor.status_delay)
        return self.supervisor.status()

    def send_input(self, data: bytes | str, *, subject: str = "") -> dict[str, Any]:
        payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        # The supervisor reads the request and acts on it before its answer travels back.
        answer = self.supervisor.input(payload, subject)
        self._answer_after(self.supervisor.admission_delay)
        return answer

    def drain(self, actor: str) -> dict[str, Any]:
        self.supervisor.drained = True
        return {"ok": True}


class SupervisorSockets:
    """`SupervisorClient.connect`, answered by whichever scripted supervisor owns the socket path."""

    def __init__(self) -> None:
        self.supervisors: dict[str, ScriptedSupervisor] = {}

    def add(self, supervisor: ScriptedSupervisor) -> ScriptedSupervisor:
        self.supervisors[str(supervisor.run_dir / protocol.SOCKET_NAME)] = supervisor
        return supervisor

    def connect(self, socket_path: Any, *, timeout: float = 5.0) -> FakeClient:
        supervisor = self.supervisors[str(socket_path)]
        supervisor.connects += 1
        if not supervisor.reachable:
            raise local_pty.LocalPtyError(f"no supervisor answers at {socket_path}") from ConnectionRefusedError()
        if supervisor.connect_delay > timeout:
            supervisor.clock.advance(timeout)
            raise local_pty.LocalPtyError(f"no supervisor answers at {socket_path}") from TimeoutError()
        supervisor.clock.advance(supervisor.connect_delay)
        return FakeClient(supervisor, timeout)


@dataclass
class Transport:
    before_send: Any = None


class HandoffTestCase(unittest.TestCase):
    def setUp(self) -> None:
        # A short root: a Unix socket address is about 100 bytes.
        self.root = Path(tempfile.mkdtemp(prefix="h", dir="/tmp"))
        self.clock = FakeClock()
        self.sockets = SupervisorSockets()
        patcher = mock.patch.object(local_pty.SupervisorClient, "connect", self.sockets.connect)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.runs = 0

    def head(self, **options: Any) -> tuple[ScriptedSupervisor, HeadRun]:
        self.runs += 1
        run = HeadRun(
            run_id=f"5e0942743ede48869b67e848503f0c{self.runs:02d}",
            spec=HeadSpec(profile_id="codex", adapter="codex"),
            workspace="/tmp/workspace",
            task_ref=TaskRef.card("codegen-orchestrator-1558"),
            role="worker",
        )
        supervisor = ScriptedSupervisor(self.clock, protocol.run_dir_for(self.root, run.run_id), run.run_id, **options)
        return self.sockets.add(supervisor), run

    def runtime(self) -> LocalPtyHeadRuntime:
        return LocalPtyHeadRuntime(
            self.root,
            head_process_status=lambda *_args, **_kwargs: {},
            monotonic=self.clock.monotonic,
            wall=self.clock.wall,
            sleep=self.clock.sleep,
        )

    def handoff(self, supervisor: ScriptedSupervisor) -> PromptHandoff:
        return PromptHandoff(floor=supervisor.seq, began_at=self.clock.wall())

    def deliver(
        self, run: HeadRun, handoff: PromptHandoff, *, runtime: LocalPtyHeadRuntime | None = None, wake: Any = None
    ) -> DeliverReceipt:
        return (runtime or self.runtime()).deliver(
            run, POINTER, subject=SUBJECT, transport=Transport(wake), handoff=handoff
        )

    def tick(
        self,
        run: HeadRun,
        handoff: PromptHandoff,
        *,
        budget: float = 4.0,
        runtime: LocalPtyHeadRuntime | None = None,
        wake: Any = None,
    ) -> tuple[DeliverReceipt, HandoffBudget, float]:
        """One production pass over this handoff: the receipt, the budget, the clock it took.

        Every tick is a new dispatcher pass; unless a runtime is passed it is also a new process
        that remembers nothing of the last one.
        """
        started = self.clock.now
        with handoff_budget(budget, clock=self.clock.monotonic) as spent:
            receipt = self.deliver(run, handoff, runtime=runtime, wake=wake)
        took = self.clock.now - started
        self.assertLessEqual(took, budget + 1e-6, "the tick's handoff outlasted its allowance")
        self.assertAlmostEqual(spent.spent, took, places=6, msg="what the handoff took was not all charged")
        return receipt, spent, took


class AQuietHeadIsHandedOffInsideTheBudgetTests(HandoffTestCase):
    """The 06:39:08 case: a retained worker, quiet for ten minutes, takes its continuation."""

    def test_one_tick_types_submits_and_confirms_within_the_budget(self) -> None:
        supervisor, run = self.head()
        receipt, budget, took = self.tick(run, self.handoff(supervisor))
        self.assertEqual(receipt.status, HEAD_OK, receipt.reason)
        self.assertTrue(receipt.delivery.evidence.turn_confirmed)
        self.assertEqual(supervisor.accepted, [SUBJECT, f"{SUBJECT}:submit"])
        # Settle is one status: no watched `PROMPT_QUIET_SECONDS` over a head that has long been quiet.
        stages = {entry["stage"]: entry for entry in budget.stages}
        self.assertLess(stages["type"]["ms"], 100.0, "typing is the admission and the write, nothing more")
        self.assertEqual(stages["handoff"]["outcome"], HEAD_OK)
        # Each stage says what it was allowed beside what it took.
        self.assertEqual(stages["handoff"]["allowed_ms"], 4000.0)
        # What is left is the echo turn's own quiet close and the provider's first output.
        self.assertGreater(took, 2.0)
        self.assertLessEqual(took, 3.0)

    def test_with_no_allowance_nothing_is_asked_or_sent_and_the_next_tick_continues(self) -> None:
        supervisor, run = self.head()
        handoff = self.handoff(supervisor)
        first, budget, took = self.tick(run, handoff, budget=0.0)
        self.assertEqual((took, budget.spent), (0.0, 0.0))
        self.assertEqual(first.status, HEAD_BUSY)
        self.assertEqual(first.handoff_stage, HANDOFF_SETTLE)
        self.assertIsNone(first.failure)
        self.assertEqual(supervisor.connects, 0, "a spent allowance makes no supervisor request at all")
        self.clock.advance(60.0)
        second, _, _ = self.tick(run, handoff)
        self.assertEqual(second.status, HEAD_OK, second.reason)
        self.assertEqual(supervisor.offered, [SUBJECT, f"{SUBJECT}:submit"])

    def test_a_confirmed_handoff_asked_again_sends_nothing(self) -> None:
        supervisor, run = self.head()
        handoff = self.handoff(supervisor)
        self.assertEqual(self.tick(run, handoff)[0].status, HEAD_OK)
        self.clock.advance(60.0)
        again, _, took = self.tick(run, handoff)
        self.assertEqual(again.status, HEAD_OK, again.reason)
        self.assertEqual(supervisor.offered, [SUBJECT, f"{SUBJECT}:submit"])
        self.assertLess(took, 0.01, "one status and one journal read")


class TheBlockingFlowItReplacesTests(HandoffTestCase):
    """Before: what `_deliver_prompt` costs on the same scripted heads, on the same fake clock.

    These are the stage costs the card measured the change against. The blocking flow watches a
    head that has been quiet for ten minutes for another `PROMPT_QUIET_SECONDS`, waits out the echo
    turn's quiet close, and polls for the provider's output; three such cards in one tick add up.
    """

    def blocking(self, run: HeadRun) -> float:
        started = self.clock.now
        with mock.patch("ummanu.runtime.local_pty_head.time", self.clock):
            receipt = self.runtime().deliver(run, POINTER, subject=SUBJECT, transport=Transport())
        self.assertEqual(receipt.status, HEAD_OK, receipt.reason)
        return self.clock.now - started

    def test_a_quiet_retained_head_costs_the_settle_echo_and_confirm_waits(self) -> None:
        took = self.blocking(self.head()[1])
        # settle 4.0 (watched quiet) + echo ~2.1 (TURN_QUIET_SECONDS after the echo) + confirm ~0.5
        self.assertGreaterEqual(took, 6.5)
        self.assertLessEqual(took, 7.5)

    def test_a_starting_reviewer_costs_its_startup_output_too(self) -> None:
        took = self.blocking(self.head(prints_until=4.0)[1])
        # Its last startup output at 3.5 s, quiet by 7.5 s (production 06:53: 7.6 s), then echo and confirm.
        self.assertGreaterEqual(took, 10.0)

    def test_a_slow_pty_is_followed_to_its_end_outside_any_budget(self) -> None:
        # The repro the review ran: a write the reader takes 9 s over is followed, synchronously.
        took = self.blocking(self.head(write_seconds=9.0)[1])
        self.assertGreater(took, 9.0)

    def test_three_cards_sum_their_waits_where_the_handoff_shares_one_budget(self) -> None:
        blocking = sum(self.blocking(self.head()[1]) for _ in range(3))
        self.assertGreater(blocking, 19.5)
        heads = [self.head() for _ in range(3)]
        started = self.clock.now
        with handoff_budget(4.0, cards=3, clock=self.clock.monotonic) as budget:
            for supervisor, run in heads:
                with budget.card():
                    self.deliver(run, self.handoff(supervisor))
        self.assertLessEqual(self.clock.now - started, 4.0 + 1e-6)
        self.assertAlmostEqual(budget.spent, self.clock.now - started, places=6)


class NoSupervisorRequestEscapesTheAllowanceTests(HandoffTestCase):
    """BLOCKER-budget-escape: admission, following the write and every status share the allowance."""

    def test_a_write_the_pty_takes_9s_over_is_pending_inside_the_allowance_and_never_repeated(self) -> None:
        supervisor, run = self.head(write_seconds=9.0)
        handoff = self.handoff(supervisor)
        first, budget, took = self.tick(run, handoff)
        self.assertEqual(first.status, HEAD_BUSY)
        self.assertEqual(first.handoff_stage, HANDOFF_TYPED, "admitted and still being written")
        self.assertIsNone(first.failure, "a write still in flight is not a failure")
        self.assertFalse(supervisor.drained, "nor the unestablished outcome that closes the head")
        self.assertAlmostEqual(took, 4.0, places=6)
        type_stage = next(entry for entry in budget.stages if entry["stage"] == "type")
        self.assertEqual(type_stage["outcome"], "delivery_pending")
        self.assertAlmostEqual(type_stage["ms"], 4000.0, delta=10.0, msg="the follow is charged, not hidden")
        # Still in flight on the next tick: followed again within its allowance, never offered again.
        self.clock.advance(1.0)
        second, _, _ = self.tick(run, handoff)
        self.assertEqual(second.handoff_stage, HANDOFF_TYPED)
        self.clock.advance(60.0)
        third, _, _ = self.tick(run, handoff)
        self.assertEqual(third.status, HEAD_BUSY, "the Enter is now the slow write")
        self.assertEqual(third.handoff_stage, HANDOFF_SUBMITTED)
        self.clock.advance(60.0)
        done, _, _ = self.tick(run, handoff)
        self.assertEqual(done.status, HEAD_OK, done.reason)
        self.assertEqual(supervisor.offered, [SUBJECT, f"{SUBJECT}:submit"], "one line and one Enter")

    def test_the_follow_itself_stops_at_the_operation_deadline(self) -> None:
        _, run = self.head(write_seconds=9.0)
        runtime = self.runtime()
        with handoff_budget(4.0, clock=self.clock.monotonic) as budget, budget.operation(SUBJECT) as operation:
            typed = runtime._deliver_payload(run, POINTER, SUBJECT, operation=operation)
        self.assertEqual(typed.status, HEAD_BUSY)
        self.assertEqual(typed.evidence.outcome, DELIVERY_PENDING)
        self.assertAlmostEqual(self.clock.now, 4.0, places=6)
        self.assertAlmostEqual(budget.spent, 4.0, places=6, msg="the review's repro charged 0 s for 9 s")
        self.assertIsNotNone(runtime.activity.lease(run.run_id), "the lease stays with the write in flight")

    def test_three_slow_cards_cannot_add_up_their_old_bounds(self) -> None:
        heads = [self.head(write_seconds=9.0) for _ in range(3)]
        handoffs = [self.handoff(supervisor) for supervisor, _ in heads]
        for tick in range(4):
            started = self.clock.now
            with handoff_budget(4.0, cards=3, clock=self.clock.monotonic) as budget:
                for (_, run), handoff in zip(heads, handoffs, strict=True):
                    with budget.card():
                        self.deliver(run, handoff)
            self.assertLessEqual(self.clock.now - started, 4.0 + 1e-6, f"tick {tick}: 3 x 15 s is the old bound")
            self.assertAlmostEqual(budget.spent, self.clock.now - started, places=6)
            if tick == 0:
                self.assertTrue(all(supervisor.offered == [SUBJECT] for supervisor, _ in heads), "each got its share")
            self.clock.advance(30.0)
        for supervisor, _ in heads:
            self.assertEqual(supervisor.offered, [SUBJECT, f"{SUBJECT}:submit"])

    def test_a_status_slower_than_the_allowance_is_pending_not_unreachable(self) -> None:
        supervisor, run = self.head(status_delay=6.0)
        handoff = self.handoff(supervisor)
        receipt, _, took = self.tick(run, handoff)
        self.assertEqual(receipt.status, HEAD_BUSY)
        self.assertEqual(receipt.handoff_stage, HANDOFF_SETTLE)
        self.assertIn("did not answer within this pass's allowance", receipt.reason)
        self.assertAlmostEqual(took, 4.0, places=6)
        self.assertEqual(supervisor.offered, [])
        self.assertFalse(supervisor.drained)

    def test_a_lost_admission_answer_is_pending_and_the_next_status_says_it_was_taken(self) -> None:
        supervisor, run = self.head(admission_delay=30.0, write_seconds=1.0)
        handoff = self.handoff(supervisor)
        first, _, _ = self.tick(run, handoff)
        self.assertEqual(first.status, HEAD_BUSY)
        self.assertEqual(first.handoff_stage, HANDOFF_SETTLE, "nothing is established about the line yet")
        self.assertIsNone(first.failure, "no false fatal, no false acknowledgement")
        self.assertEqual(supervisor.offered, [SUBJECT], "the supervisor took it all the same")
        supervisor.admission_delay = 0.002
        self.clock.advance(60.0)
        done, _, _ = self.tick(run, handoff)
        self.assertEqual(done.status, HEAD_OK, done.reason)
        self.assertEqual(supervisor.offered, [SUBJECT, f"{SUBJECT}:submit"], "the line was not offered twice")

    def test_a_lost_admission_seen_in_flight_is_followed_not_repeated(self) -> None:
        supervisor, run = self.head(admission_delay=30.0, write_seconds=20.0)
        handoff = self.handoff(supervisor)
        self.tick(run, handoff)
        supervisor.admission_delay = 0.002
        self.clock.advance(1.0)
        second, _, _ = self.tick(run, handoff)
        self.assertEqual(second.handoff_stage, HANDOFF_TYPED, "the status shows this handoff's line in flight")
        self.assertEqual(supervisor.offered, [SUBJECT])

    def test_a_supervisor_that_cannot_be_reached_is_not_waited_on(self) -> None:
        supervisor, run = self.head(connect_delay=30.0)
        receipt, _, took = self.tick(run, self.handoff(supervisor))
        self.assertEqual(receipt.handoff_stage, HANDOFF_SETTLE)
        self.assertAlmostEqual(took, 4.0, places=6, msg="the 5 s connect bound is cut to the allowance")


class TheFloorAndTheStartAreReadInsideTheAllowanceTests(HandoffTestCase):
    def test_an_unavailable_floor_is_none_and_a_reachable_one_is_read(self) -> None:
        supervisor, run = self.head()
        supervisor.reachable = False
        with handoff_budget(4.0, clock=self.clock.monotonic):
            self.assertIsNone(self.runtime().handoff_floor(run))
        supervisor.reachable = True
        supervisor.connect_delay = 30.0
        with handoff_budget(4.0, clock=self.clock.monotonic) as budget:
            self.assertIsNone(self.runtime().handoff_floor(run))
        self.assertLessEqual(budget.spent, 4.0 + 1e-6)
        supervisor.connect_delay = 0.001
        with handoff_budget(4.0, clock=self.clock.monotonic):
            self.assertEqual(self.runtime().handoff_floor(run), supervisor.seq)

    def test_started_reads_the_write_in_flight_and_defers_without_an_answer(self) -> None:
        supervisor, run = self.head(write_seconds=20.0)
        handoff = self.handoff(supervisor)
        with handoff_budget(4.0, clock=self.clock.monotonic):
            self.assertEqual(self.runtime().handoff_started(run, SUBJECT, handoff), HANDOFF_NOT_STARTED)
        self.tick(run, handoff)
        with handoff_budget(4.0, clock=self.clock.monotonic):
            self.assertEqual(
                self.runtime().handoff_started(run, SUBJECT, handoff),
                HANDOFF_STARTED,
                "a line still being written has started, though no journal record says so yet",
            )
        other, other_run = self.head(status_delay=9.0)
        with handoff_budget(4.0, clock=self.clock.monotonic):
            self.assertEqual(
                self.runtime().handoff_started(other_run, SUBJECT, self.handoff(other)), HANDOFF_DEFERRED
            )
        with handoff_budget(0.0, clock=self.clock.monotonic):
            self.assertEqual(self.runtime().handoff_started(other_run, SUBJECT, self.handoff(other)), HANDOFF_DEFERRED)


class AHeadThatIsNotReadyIsNotWaitedOnTests(HandoffTestCase):
    """`PROMPT_FIRST_OUTPUT_SECONDS`/`PROMPT_SETTLE_SECONDS` become ticks, never a 20/90 s wait."""

    def test_a_starting_head_is_typed_into_on_the_tick_it_is_quiet(self) -> None:
        supervisor, run = self.head(prints_until=30.0)
        handoff = self.handoff(supervisor)
        first, _, took = self.tick(run, handoff)
        self.assertEqual(first.handoff_stage, HANDOFF_SETTLE)
        self.assertLess(took, 0.01, "a head that cannot be quiet within the allowance is not watched")
        self.assertEqual(supervisor.offered, [], "nothing is typed into a head still printing")
        self.clock.advance(60.0)
        second, _, _ = self.tick(run, handoff)
        self.assertEqual(second.status, HEAD_OK, second.reason)

    def test_a_head_about_to_be_quiet_is_waited_on_only_that_long(self) -> None:
        supervisor, run = self.head(prints_until=1.0)
        self.clock.advance(1.5)
        receipt, _, _ = self.tick(run, self.handoff(supervisor))
        # Its last output at 0.5 s: quiet at 4.5 s, 3 s into this tick, which is then typed into.
        self.assertEqual(supervisor.offered, [SUBJECT], receipt.reason)
        self.assertEqual(receipt.handoff_stage, HANDOFF_TYPED)

    def test_a_head_that_never_stops_printing_is_typed_into_past_the_settle_bound(self) -> None:
        supervisor, run = self.head(prints_until=400.0)
        handoff = self.handoff(supervisor)
        for _ in range(2):
            receipt, _, _ = self.tick(run, handoff)
            self.assertEqual(receipt.handoff_stage, HANDOFF_SETTLE)
            self.clock.advance(60.0)
        # 120 s after the handoff opened: past `PROMPT_SETTLE_SECONDS`, as `_await_settled` gives up.
        self.tick(run, handoff)
        self.assertEqual(supervisor.offered[:1], [SUBJECT])

    def test_a_head_in_a_turn_is_refused_busy_as_before_and_nothing_is_typed(self) -> None:
        supervisor, run = self.head()
        supervisor.turn_open, supervisor.turn, supervisor.last_output = True, 9, 0.0
        supervisor.scheduled += [(step * 0.5, 100) for step in range(1, 40)]
        receipt, _, _ = self.tick(run, self.handoff(supervisor))
        self.assertEqual(receipt.status, HEAD_BUSY)
        self.assertEqual(receipt.handoff_stage, "", "a busy pane is the refusal it always was")
        self.assertEqual(supervisor.offered, [])


class AReleasedSupervisorIsServedFairlyTests(HandoffTestCase):
    """BLOCKER-released-supervisor-fairness: supervisors started before `output_idle_seconds`.

    The production dispatcher keeps one runtime across ticks (`CommandHostRuntime` caches it), so
    what it saw of a quiet head on one tick still counts on the next, checked against the head's
    incarnation, journal sequence and output count. Ticks here are 10 s apart.
    """

    def run_ticks(self, heads, handoffs, runtime, ticks):
        stages = []
        for _ in range(ticks):
            started = self.clock.now
            with handoff_budget(4.0, cards=len(heads), clock=self.clock.monotonic) as budget:
                for (_, run), handoff in zip(heads, handoffs, strict=True):
                    with budget.card():
                        stages.append(self.deliver(run, handoff, runtime=runtime).handoff_stage)
            self.assertLessEqual(self.clock.now - started, 4.0 + 1e-6)
            self.clock.advance(10.0)
        return stages

    def test_a_quiet_peer_behind_a_noisy_one_types_within_two_ticks(self) -> None:
        noisy = self.head(idle_field=False, prints_until=400.0)
        quiet = self.head(idle_field=False)
        handoffs = [self.handoff(supervisor) for supervisor, _ in (noisy, quiet)]
        self.run_ticks([noisy, quiet], handoffs, self.runtime(), 2)
        self.assertEqual(quiet[0].offered[:1], [SUBJECT], "not 90 s of forced settle")
        self.assertEqual(noisy[0].offered, [], "the noisy one is still not typed into")
        self.assertLess(self.clock.now, 25.0)

    def test_a_third_peer_with_the_new_field_moves_on_its_first_tick(self) -> None:
        noisy = self.head(idle_field=False, prints_until=400.0)
        quiet = self.head(idle_field=False)
        fresh = self.head()
        handoffs = [self.handoff(supervisor) for supervisor, _ in (noisy, quiet, fresh)]
        runtime = self.runtime()
        self.run_ticks([noisy, quiet, fresh], handoffs, runtime, 1)
        self.assertEqual(fresh[0].offered[:1], [SUBJECT], "one status answers it, behind a noisy peer too")
        self.run_ticks([noisy, quiet, fresh], handoffs, runtime, 1)
        self.assertEqual(quiet[0].offered[:1], [SUBJECT])

    def test_a_restarted_dispatcher_sees_the_quiet_again_within_two_ticks(self) -> None:
        noisy = self.head(idle_field=False, prints_until=400.0)
        quiet = self.head(idle_field=False)
        handoffs = [self.handoff(supervisor) for supervisor, _ in (noisy, quiet)]
        self.run_ticks([noisy, quiet], handoffs, self.runtime(), 1)
        # The process restarts: what it saw is gone, and the first look is only a baseline again.
        restarted = self.runtime()
        self.run_ticks([noisy, quiet], handoffs, restarted, 1)
        self.assertEqual(quiet[0].offered, [])
        self.run_ticks([noisy, quiet], handoffs, restarted, 1)
        self.assertEqual(quiet[0].offered[:1], [SUBJECT])

    def test_quiet_is_not_manufactured_across_a_new_incarnation(self) -> None:
        quiet, run = self.head(idle_field=False)
        handoff = self.handoff(quiet)
        runtime = self.runtime()
        with handoff_budget(0.6, clock=self.clock.monotonic):
            first = self.deliver(run, handoff, runtime=runtime)
        self.assertEqual(first.handoff_stage, HANDOFF_SETTLE)
        # Same output count and sequence, another supervisor: the head it watched is not this one.
        quiet.supervisor_pid += 1
        self.clock.advance(10.0)
        with handoff_budget(0.6, clock=self.clock.monotonic):
            second = self.deliver(run, handoff, runtime=runtime)
        self.assertEqual(second.handoff_stage, HANDOFF_SETTLE)
        self.assertEqual(quiet.offered, [])
        self.clock.advance(10.0)
        with handoff_budget(4.0, clock=self.clock.monotonic):
            third = self.deliver(run, handoff, runtime=runtime)
        self.assertEqual(quiet.offered[:1], [SUBJECT], third.reason)

    def test_output_between_looks_restarts_the_watch(self) -> None:
        printing, run = self.head(idle_field=False)
        handoff = self.handoff(printing)
        runtime = self.runtime()
        with handoff_budget(0.6, clock=self.clock.monotonic):
            self.deliver(run, handoff, runtime=runtime)
        printing.scheduled.append((self.clock.now + 5.0, 64))
        self.clock.advance(10.0)
        with handoff_budget(0.6, clock=self.clock.monotonic):
            self.assertEqual(self.deliver(run, handoff, runtime=runtime).handoff_stage, HANDOFF_SETTLE)
        self.assertEqual(printing.offered, [])


class ASubmitIsSentAtMostTwiceTests(HandoffTestCase):
    def test_an_enter_that_starts_nothing_is_retried_once_then_reported_not_submitted(self) -> None:
        supervisor, run = self.head(takes_enter=False)
        handoff = self.handoff(supervisor)
        receipts = []
        for _ in range(4):
            receipts.append(self.tick(run, handoff)[0])
            self.clock.advance(60.0)
        final = receipts[-1]
        self.assertEqual(final.status, HEAD_ALIVE)
        self.assertEqual(final.reason, DELIVER_NOT_SUBMITTED)
        self.assertTrue(final.evidence.payload_left_in_composer)
        self.assertEqual(final.evidence.submit_count, 2)
        self.assertEqual(supervisor.offered, [SUBJECT, f"{SUBJECT}:submit", f"{SUBJECT}:submit"])


class ARestartedDispatcherContinuesTheSameHandoffTests(HandoffTestCase):
    """The head is the cursor: a new process after the intent, the line, the Enter, the confirmation."""

    def test_after_opening_the_intent_it_types_once(self) -> None:
        supervisor, run = self.head()
        handoff = self.handoff(supervisor)
        with handoff_budget(0.0, clock=self.clock.monotonic):
            self.deliver(run, handoff)  # the dying tick got no further than its intent
        self.assertEqual(self.tick(run, handoff)[0].status, HEAD_OK)
        self.assertEqual(supervisor.offered, [SUBJECT, f"{SUBJECT}:submit"])

    def test_after_the_line_was_admitted_it_is_never_offered_again(self) -> None:
        supervisor, run = self.head(write_seconds=5.0)
        handoff = self.handoff(supervisor)
        supervisor.input((POINTER.text + "\n").encode(), SUBJECT)  # admitted, then the tick died
        self.clock.advance(1.0)
        self.assertEqual(self.tick(run, handoff)[0].handoff_stage, HANDOFF_TYPED)
        supervisor.write_seconds = 0.02
        self.clock.advance(60.0)
        self.assertEqual(self.tick(run, handoff)[0].status, HEAD_OK)
        self.assertEqual(supervisor.offered, [SUBJECT, f"{SUBJECT}:submit"])

    def test_after_the_pointer_it_submits_and_does_not_type_again(self) -> None:
        supervisor, run = self.head()
        handoff = self.handoff(supervisor)
        supervisor.input((POINTER.text + "\n").encode(), SUBJECT)
        self.clock.advance(60.0)
        receipt, _, _ = self.tick(run, handoff)
        self.assertEqual(receipt.status, HEAD_OK, receipt.reason)
        self.assertEqual(supervisor.offered, [SUBJECT, f"{SUBJECT}:submit"])

    def test_after_the_enter_was_admitted_no_second_enter_is_sent(self) -> None:
        supervisor, run = self.head()
        handoff = self.handoff(supervisor)
        supervisor.input((POINTER.text + "\n").encode(), SUBJECT)
        self.clock.advance(3.0)
        supervisor.write_seconds = 3.0
        supervisor.input(SUBMIT_KEY, f"{SUBJECT}:submit")
        self.clock.advance(1.0)
        pending, _, _ = self.tick(run, handoff, budget=1.0)
        self.assertEqual(pending.handoff_stage, HANDOFF_SUBMITTED)
        self.clock.advance(60.0)
        self.assertEqual(self.tick(run, handoff)[0].status, HEAD_OK)
        self.assertEqual(supervisor.offered, [SUBJECT, f"{SUBJECT}:submit"])

    def test_after_the_confirmation_it_confirms_from_the_journal_and_sends_nothing(self) -> None:
        supervisor, run = self.head()
        handoff = self.handoff(supervisor)
        supervisor.input((POINTER.text + "\n").encode(), SUBJECT)
        self.clock.advance(3.0)
        supervisor.input(SUBMIT_KEY, f"{SUBJECT}:submit")
        self.clock.advance(60.0)
        receipt, _, took = self.tick(run, handoff)
        self.assertEqual(receipt.status, HEAD_OK, receipt.reason)
        self.assertEqual(supervisor.offered, [SUBJECT, f"{SUBJECT}:submit"])
        self.assertLess(took, 0.01, "a turn the journal already shows taken is not waited on")

    def test_a_previous_rounds_handoff_below_the_floor_is_not_this_one(self) -> None:
        supervisor, run = self.head()
        old = self.handoff(supervisor)
        self.assertEqual(self.tick(run, old)[0].status, HEAD_OK)
        self.clock.advance(600.0)
        supervisor.advance()
        new = self.handoff(supervisor)
        receipt, _, _ = self.tick(run, new)
        self.assertEqual(receipt.status, HEAD_OK, receipt.reason)
        self.assertEqual(supervisor.offered, [SUBJECT, f"{SUBJECT}:submit"] * 2)

    def test_a_new_incarnation_after_the_line_is_refused_not_retyped(self) -> None:
        supervisor, run = self.head()
        handoff = self.handoff(supervisor)
        supervisor.input((POINTER.text + "\n").encode(), SUBJECT)
        self.clock.advance(5.0)
        supervisor.advance()
        supervisor.append("run.started", self.clock.now)
        receipt, _, _ = self.tick(run, handoff)
        self.assertEqual(receipt.status, HEAD_ALIVE)
        self.assertTrue(receipt.reason.startswith(DELIVER_HANDOFF_UNESTABLISHED))
        self.assertEqual(supervisor.offered, [SUBJECT])

    def test_a_journal_that_no_longer_reaches_the_floor_is_refused_not_retyped(self) -> None:
        supervisor, run = self.head()
        handoff = PromptHandoff(floor=0, began_at=self.clock.wall())
        cut = JournalReadResult(events=(), partial_head=True)
        with mock.patch.object(local_pty, "read_tail", return_value=cut):
            receipt, _, _ = self.tick(run, handoff)
        self.assertEqual(receipt.status, HEAD_ALIVE)
        self.assertTrue(receipt.reason.startswith(DELIVER_HANDOFF_UNESTABLISHED))
        self.assertEqual(handoff_pending_stage(receipt), "")
        self.assertEqual(supervisor.offered, [])

    def test_a_line_left_in_part_is_the_fatal_prefix_it_always_was(self) -> None:
        supervisor, run = self.head()
        handoff = self.handoff(supervisor)
        supervisor.append(
            "input.accepted",
            0.0,
            subject=SUBJECT,
            bytes=7,
            offered_bytes=60,
            complete=False,
            state="stalled",
            delivery=1,
        )
        receipt, _, _ = self.tick(run, handoff)
        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.handoff_stage, "")
        self.assertEqual(receipt.status, HEAD_ALIVE)
        self.assertTrue(supervisor.drained, "the head is closed, as a live prefix closes it")
        self.assertEqual(supervisor.offered, [])


class AFoldedTurnIsNotTakenForAConfirmationTests(HandoffTestCase):
    """BLOCKER-pending-liveness, the runtime's half: a spinner the supervisor folds is no evidence.

    `Supervisor._flush_progress` itself writes the journal here. A first window of 12 bytes and then
    eight windows of an unchanged screen leave 100 KB of raw output and nothing a reader may count
    as the submit's turn taking the prompt. The handoff stays submitted, sends no second Enter into
    the open turn, and past the blocking flow's own bounds for one submit (`submit_confirm` +
    `prompt_settle`) reports the prompt not submitted with the pane busy: the refusal the
    dispatcher's existing ladders own. Its provider liveness is the dispatcher's
    (`test_dispatcher_worker_continuation.ProductionContinuationHandoffTests`).
    """

    def folding_supervisor(self, supervisor: ScriptedSupervisor) -> Supervisor:
        folding = object.__new__(Supervisor)
        folding._screen = mock.Mock()
        folding._screen.lines.return_value = ["spinner"]
        folding._progress_window_bytes = 12
        folding._progress_bytes = 12
        folding._progress_at = 0
        folding._progress_visible = set()
        folding._progress_seen = {}
        folding._folded_windows = 0
        folding._turn_id = supervisor.turn
        folding._output_total = supervisor.output_total
        folding._output_dropped = 0
        folding._append = lambda kind, **fields: supervisor.append(kind, self.clock.now, **fields)
        return folding

    def test_the_handoff_stays_submitted_and_then_is_not_submitted_without_a_second_enter(self) -> None:
        supervisor, run = self.head(takes_enter=False)
        handoff = self.handoff(supervisor)
        self.tick(run, handoff, budget=1.0)  # typed
        self.clock.advance(60.0)
        self.tick(run, handoff, budget=0.6)  # submitted, and no time left to see it taken
        self.assertEqual(supervisor.offered, [SUBJECT, f"{SUBJECT}:submit"])
        submitted_at = self.clock.now
        # From here the head redraws a spinner every half second: the turn stays open, the raw output
        # grows, and the supervisor folds every window of it.
        supervisor.folding = True
        supervisor.scheduled = [(submitted_at + step * 0.5, 1500) for step in range(400)]
        folding = self.folding_supervisor(supervisor)
        folding._flush_progress()
        stages = []
        for _ in range(8):
            self.clock.advance(8.0)
            supervisor.advance()
            folding._progress_window_bytes = 12000
            folding._progress_bytes += 12000
            folding._output_total = supervisor.output_total
            folding._flush_progress()
            receipt, _, _ = self.tick(run, handoff)
            stages.append(receipt.handoff_stage or receipt.reason)
        self.assertEqual(folding._folded_windows, 8)
        self.assertGreater(supervisor.output_total - 4096, 100_000, "raw output grew past 100 KB")
        self.assertEqual(stages, [HANDOFF_SUBMITTED] * 8)
        self.assertEqual(supervisor.offered, [SUBJECT, f"{SUBJECT}:submit"], "no Enter into the open turn")
        while self.clock.now - submitted_at < 115.0:
            self.clock.advance(8.0)
            receipt, _, _ = self.tick(run, handoff)
        self.assertEqual(receipt.status, HEAD_BUSY)
        self.assertEqual(receipt.handoff_stage, "")
        self.assertTrue(receipt.reason.startswith(DELIVER_NOT_SUBMITTED))
        self.assertEqual(receipt.evidence.readiness_state, READINESS_BUSY)
        self.assertEqual(supervisor.offered, [SUBJECT, f"{SUBJECT}:submit"])


class ALaunchWithAPendingHandoffKeepsItsHeadTests(HandoffTestCase):
    """A reviewer raised this tick is never typed into by the call that raised it."""

    def test_start_answers_busy_at_settle_and_neither_types_nor_stops_the_head(self) -> None:
        supervisor, run = self.head()
        runtime = self.runtime()
        abandoned = []
        with (
            mock.patch.object(
                LocalPtyHeadRuntime, "_start_locked", return_value=StartReceipt(status=HEAD_OK, run=run)
            ),
            mock.patch.object(LocalPtyHeadRuntime, "_abandon_bring_up", side_effect=lambda *a: abandoned.append(a)),
            handoff_budget(4.0, clock=self.clock.monotonic),
        ):
            receipt = runtime.start(
                run.spec,
                run.workspace,
                run.task_ref,
                command="codex",
                title="",
                pointer=POINTER,
                transport=Transport(),
                subject="reviewer-launch",
                handoff=PromptHandoff(floor=0, began_at=self.clock.wall()),
            )
        self.assertEqual(receipt.status, HEAD_BUSY)
        self.assertEqual(receipt.handoff_stage, HANDOFF_SETTLE)
        self.assertEqual(handoff_pending_stage(receipt.evidence), HANDOFF_SETTLE)
        self.assertEqual(receipt.run, run)
        self.assertEqual(abandoned, [])
        self.assertEqual(supervisor.offered, [])
        self.assertEqual(supervisor.connects, 0, "the writer fence comes before any of its prompt")


class TheBudgetIsSharedFairlyTests(unittest.TestCase):
    def test_each_card_gets_what_is_left_over_the_cards_not_yet_served(self) -> None:
        clock = FakeClock()
        budget = HandoffBudget(4.0, cards=3, clock=clock.monotonic)
        with budget.card() as share:
            self.assertAlmostEqual(share, 4.0 / 3)
            with budget.operation("a") as operation:
                self.assertAlmostEqual(operation.remaining(), 4.0 / 3)
                clock.advance(4.0 / 3)
                self.assertEqual(operation.remaining(), 0.0)
        with budget.card() as share:
            self.assertAlmostEqual(share, (4.0 - 4.0 / 3) / 2)
        with budget.card() as share:
            self.assertAlmostEqual(share, 4.0 - 4.0 / 3, msg="a card that spent nothing leaves it to the next")

    def test_a_nested_operation_is_charged_once(self) -> None:
        clock = FakeClock()
        budget = HandoffBudget(4.0, clock=clock.monotonic)
        with budget.operation("a") as outer:
            clock.advance(1.0)
            with budget.operation("b") as inner:
                self.assertIs(inner, outer)
                clock.advance(1.0)
        self.assertEqual(budget.spent, 2.0)


class OnlyHeadsThatCanBeReadTakeHandoffsTests(unittest.TestCase):
    """The production runtime always does; a noop host, or a backend that cannot read one, does not."""

    def test_local_pty_takes_them_and_nothing_else_is_made_to(self) -> None:
        host = mock.Mock(spec=CommandHostRuntime)
        host.head_handoffs.return_value = True
        record = mock.Mock()
        host.head_runtime_for.return_value = LocalPtyHeadRuntime(
            tempfile.mkdtemp(prefix="h", dir="/tmp"), head_process_status=lambda *_a, **_k: {}
        )
        self.assertTrue(CommandHostRuntime.worker_handoffs(host, record))
        host.head_runtime_for.return_value = object()
        self.assertFalse(CommandHostRuntime.worker_handoffs(host, record))
        host.head_runtime_for.side_effect = HostError("no lifecycle run")
        self.assertTrue(CommandHostRuntime.worker_handoffs(host, record), "never a fallback for a production head")
        host.head_handoffs.return_value = False
        self.assertFalse(CommandHostRuntime.worker_handoffs(host, record))


class TheAdvancePassServesEachCardItsShareTests(unittest.TestCase):
    def test_a_card_that_would_spend_everything_leaves_the_next_their_share(self) -> None:
        clock = FakeClock()
        given: list[float] = []

        def greedy(_runtime, task, _records, _payload):
            budget = production.active_budget()
            with budget.operation(task["ref"]) as operation:
                given.append(operation.allowed)
                clock.advance(operation.remaining())
            return {"status": "ok"}

        tasks = [{"ref": f"card-{n}"} for n in range(3)]
        with (
            mock.patch.object(production, "_production_tick_active", side_effect=greedy),
            handoff_budget(4.0, cards=3, clock=clock.monotonic) as budget,
        ):
            production._advance_active(mock.Mock(), {}, {}, tasks)
        self.assertEqual([round(share, 6) for share in given], [round(4.0 / 3, 6)] * 3)
        self.assertAlmostEqual(budget.spent, 4.0)
        self.assertAlmostEqual(clock.now, 4.0)


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
        self.assertTrue(delivery[LAUNCH_DELIVERY_WORKER_FENCED], "the host fenced the worker before raising it")
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
                LAUNCH_DELIVERY_WORKER_FENCED: True,
            },
        }
        self.runtime.host.nudge_review_delivery.side_effect = self.pending()

        def retry():
            return dispatcher_review.retry_busy_reviewer_launch_delivery(
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

    def test_a_fence_that_cannot_be_made_is_a_counted_refusal_not_a_pending_handoff(self) -> None:
        self.record.launch_intent = {
            **self.record.launch_intent,
            "aborted": True,
            "delivery": {"state": LAUNCH_DELIVERY_HANDOFF_PENDING, "attempts": 0, "next_at": 0.0, "handoff": True},
        }
        refused = HostError("the worker could not be fenced before the reviewer's prompt: still running")
        refused.evidence = {"subject": "reviewer-launch", "reason": "still running"}
        self.runtime.host.nudge_review_delivery.side_effect = refused
        result = dispatcher_review.retry_busy_reviewer_launch_delivery(
            self.runtime, self.task, self.records, self.payload, self.record, dict(self.record.launch_intent), "review"
        )
        self.assertEqual(result["action"], "review-launch-delivery-unavailable")
        self.assertEqual(launch_delivery(self.record.launch_intent)["attempts"], 1)


class TheReviewerIsFencedBeforeItsPromptTests(unittest.TestCase):
    """The reviewer pending-submit / non-retained-worker ordering (ummanu-140's disclosed window).

    The worker is shut down (or a retained one confirmed suspended) before anything is typed into
    the reviewer: on the launch tick, where `start` types nothing, and again on every later pass
    that would type, unless the intent already records that a non-retained worker was stopped.
    """

    def setUp(self) -> None:
        self.workspace = Path(tempfile.mkdtemp())
        self.host = mock.Mock(spec=CommandHostRuntime)
        self.host.mode = "local-pty"
        self.host.head_handoffs.return_value = True
        self.host._review_document.return_value = (self.workspace / "review.md", "Review it.")
        self.host.worker_retained_vanished.return_value = False
        self.order: list[str] = []
        self.host._fence_worker_for_reviewer.side_effect = lambda *_: self.order.append("fence")
        self.record = DispatcherRecord(
            worker="worker-1",
            workspace=str(self.workspace),
            handle="worker-1",
            head="codex",
            review_head="claude",
            attempt_id="attempt-1",
            comment_baseline=0,
            review_baseline=0,
            state="review_starting",
            claimed_at=1.0,
        )
        self.record.launch_intent = {"role": "review", "head": "claude", "at": EPOCH, "run_id": "r1"}
        self.task = {"ref": "sample-1", "project": "p"}

    def pending_launch(self, *_args, **kwargs) -> Any:
        self.order.append("launch")
        self.assertIsNotNone(kwargs["handoff"])
        raise HeadLaunchAborted(
            "reviewer-launch is pending",
            leaf="r1",
            workspace=str(self.workspace),
            evidence={"subject": "reviewer-launch", "handoff_stage": HANDOFF_SETTLE},
            head_run={"run_id": "r1"},
        )

    def test_the_launch_tick_fences_the_worker_then_keeps_the_reviewer_pending(self) -> None:
        self.host._launch.side_effect = self.pending_launch
        with handoff_budget(), self.assertRaises(HeadLaunchAborted) as raised:
            CommandHostRuntime.start_review.__wrapped__(self.host, self.task, self.record)
        self.assertEqual(self.order, ["launch", "fence"])
        self.assertEqual(handoff_pending_stage(raised.exception), HANDOFF_SETTLE)

    def test_a_fence_that_fails_on_the_launch_tick_is_the_freeze_failure_it_always_was(self) -> None:
        self.host._launch.side_effect = self.pending_launch
        self.host._fence_worker_for_reviewer.side_effect = HostError("would not stop")
        with handoff_budget(), self.assertRaises(HeadLaunchAborted) as raised:
            CommandHostRuntime.start_review.__wrapped__(self.host, self.task, self.record)
        self.assertIn("worker freeze failed", str(raised.exception))
        self.assertEqual(handoff_pending_stage(raised.exception), "", "not a pending handoff any more")

    def nudge(self, intent: dict[str, Any]) -> Any:
        self.host.head_runtime_for.return_value.deliver.side_effect = lambda *a, **k: (
            self.order.append("deliver"),
            DeliverReceipt(status=HEAD_BUSY, reason="pending", handoff_stage=HANDOFF_TYPED),
        )[1]
        self.host._codex_provider_ingress.return_value = None
        intent = {
            **intent,
            "head_run": {
                "run_id": "r1",
                "workspace": str(self.workspace),
                "task_ref": {"kind": "card", "ref": "sample-1"},
                "role": "reviewer",
                "spec": {"profile_id": "claude", "adapter": "claude"},
            },
        }
        with handoff_budget(), self.assertRaises(HostError):
            CommandHostRuntime.nudge_review_delivery(self.host, self.task, self.record, intent)

    def test_a_later_pass_fences_again_before_it_types(self) -> None:
        self.nudge({**self.record.launch_intent, "delivery": {"state": LAUNCH_DELIVERY_HANDOFF_PENDING}})
        self.assertEqual(self.order, ["fence", "deliver"])

    def test_a_recorded_stop_of_a_non_retained_worker_is_not_repeated(self) -> None:
        self.nudge(
            {
                **self.record.launch_intent,
                "delivery": {"state": LAUNCH_DELIVERY_HANDOFF_PENDING, LAUNCH_DELIVERY_WORKER_FENCED: True},
            }
        )
        self.assertEqual(self.order, ["deliver"])

    def test_a_retained_worker_is_confirmed_suspended_on_every_pass(self) -> None:
        self.record.worker_continuation.begin_retention(1.0)
        self.nudge(
            {
                **self.record.launch_intent,
                "delivery": {"state": LAUNCH_DELIVERY_HANDOFF_PENDING, LAUNCH_DELIVERY_WORKER_FENCED: True},
            }
        )
        self.assertEqual(self.order, ["fence", "deliver"])

    def test_nothing_is_typed_when_the_fence_fails(self) -> None:
        self.host._fence_worker_for_reviewer.side_effect = HostError("still running")
        self.nudge({**self.record.launch_intent, "delivery": {"state": LAUNCH_DELIVERY_HANDOFF_PENDING}})
        self.assertEqual(self.order, [])


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
        with mock.patch("ummanu.dispatch.host._heartbeat_is_live_match", return_value=True):
            CommandHostRuntime.resume_worker(
                self.host, {"ref": "sample-1", "project": "p"}, self.record, handoff_started=HANDOFF_STARTED
            )
        self.host.worker_handoff_started.assert_not_called()
        self.host._write_prompt.assert_not_called()
        self.host._clear_report_bodies.assert_not_called()
        nudge = self.host._nudge_worker.call_args
        self.assertEqual(nudge.kwargs["handoff"], self.record.worker_continuation.handoff)
        self.assertNotIn("before_send", nudge.kwargs, "nothing is woken twice")

    def test_a_deferred_read_is_pending_and_touches_nothing(self) -> None:
        with (
            mock.patch("ummanu.dispatch.host._heartbeat_is_live_match", return_value=True),
            self.assertRaises(HostError) as raised,
        ):
            CommandHostRuntime.resume_worker(
                self.host, {"ref": "sample-1", "project": "p"}, self.record, handoff_started=HANDOFF_DEFERRED
            )
        self.assertEqual(handoff_pending_stage(raised.exception), HANDOFF_SETTLE)
        self.host._write_prompt.assert_not_called()
        self.host._nudge_worker.assert_not_called()

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


class EachCardCarriesItsHandoffCostTests(unittest.TestCase):
    def test_tick_card_attaches_the_stages_and_the_real_cost_and_readers_bound_them(self) -> None:
        clock = FakeClock()
        with production.tick_clock(), handoff_budget(4.0, cards=2, clock=clock.monotonic) as budget:
            with production.tick_card("quiet-1", 1), budget.card():
                pass
            with production.tick_card("slow-1", 1), budget.card(), budget.operation(SUBJECT) as operation:
                began = clock.now
                clock.advance(2.1)
                operation.note("echo", began, "closed")
            cards = list(production._TICK_CARDS.get())
            production.note_tick_handoff(budget)
            handoff = dict(production._TICK_HANDOFF.get())
        self.assertNotIn("handoffs", cards[0])
        self.assertEqual([stage["stage"] for stage in cards[1]["handoffs"]], ["echo"])
        self.assertAlmostEqual(cards[1]["handoff_ms"], 2100.0)
        self.assertEqual(handoff, {"allowance_ms": 4000.0, "spent_ms": 2100.0, "cards": 2, "stages_dropped": 0})
        read = card_details([{**cards[1], "handoffs": cards[1]["handoffs"] * 20}])
        self.assertEqual(len(read[0]["handoffs"]), 12)
        self.assertEqual(
            read[0]["handoffs"][0],
            {"stage": "echo", "subject": SUBJECT, "ms": 2100.0, "outcome": "closed", "allowed_ms": 4000.0},
        )
        self.assertEqual(read[0]["handoff_ms"], 2100.0)


class TheJournalReaderTests(unittest.TestCase):
    """`_handoff_progress` is the only place a handoff's stage is recalled from the journal."""

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
