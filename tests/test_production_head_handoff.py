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
import socket
import tempfile
import threading
import time
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar
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
from ummanu.runtime.head.local_pty import JournalReadResult, client as client_module, protocol
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
        # How the answers travel back: in how many pieces, after which stale or pushed frames, and
        # after how long a drain is answered and a request is taken off the socket.
        self.fragments = 1
        self.stale: list[dict[str, Any]] = []
        self.drain_delay = 0.002
        self.send_delay = 0.0
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

    def handle(self, request: dict[str, Any]) -> tuple[dict[str, Any], float]:
        """Act on one request as it is read, and say how long its answer takes to travel back."""
        op = request.get("op")
        if op == protocol.OP_STATUS:
            return self.status(), self.status_delay
        if op == protocol.OP_INPUT:
            return self.input(protocol.decode_payload(request.get("data")), str(request.get("subject") or "")), (
                self.admission_delay
            )
        if op == protocol.OP_DRAIN:
            self.drained = True
            return {"ok": True}, self.drain_delay
        return {"ok": False, "error": protocol.ERROR_UNKNOWN_OP}, 0.0

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


class ScriptedSocket:
    """The socket under a real `SupervisorClient`, on the fake clock, wired to one scripted supervisor.

    The client does its own framing over it: what `sendall` writes is split into request frames, the
    supervisor acts on each one as it reads it (before its answer travels back, as the real
    supervisor's loop does), and the encoded answer arrives `delay` seconds later in `fragments`
    pieces, after any `stale` frames queued ahead of it. A `recv` whose next piece is further away
    than the timeout the client set for that call advances the clock by that timeout and raises
    `TimeoutError`, as a socket does; one already due returns at once.
    """

    def __init__(self, sockets: SupervisorSockets) -> None:
        self.sockets = sockets
        self.supervisor: ScriptedSupervisor | None = None
        self.timeout: float | None = None
        self.inbox = bytearray()
        self.arriving: list[tuple[float, bytes]] = []
        self.timeouts: list[float] = []

    def settimeout(self, timeout: float | None) -> None:
        self.timeout = timeout

    def gettimeout(self) -> float | None:
        return self.timeout

    def _wait(self, seconds: float) -> None:
        assert self.supervisor is not None
        clock = self.supervisor.clock
        self.timeouts.append(-1.0 if self.timeout is None else self.timeout)
        if self.timeout is not None and seconds > self.timeout:
            clock.advance(self.timeout)
            raise TimeoutError("timed out")
        clock.advance(max(0.0, seconds))

    def connect(self, path: str) -> None:
        supervisor = self.sockets.supervisors[str(path)]
        self.supervisor = supervisor
        supervisor.connects += 1
        if not supervisor.reachable:
            raise ConnectionRefusedError(f"no supervisor at {path}")
        self._wait(supervisor.connect_delay)

    def sendall(self, data: bytes) -> None:
        supervisor = self.supervisor
        assert supervisor is not None
        self._wait(supervisor.send_delay)
        self.inbox += data
        while (index := self.inbox.find(b"\n")) >= 0:
            request = protocol.decode_frame(bytes(self.inbox[:index]))
            del self.inbox[: index + 1]
            answer, delay = supervisor.handle(request)
            answer = {**answer, protocol.REQUEST_ID: request.get(protocol.REQUEST_ID)}
            now = supervisor.clock.now
            frames = [protocol.encode_frame(frame) for frame in supervisor.stale]
            supervisor.stale = []
            frame = protocol.encode_frame(answer)
            pieces = max(1, supervisor.fragments)
            size = -(-len(frame) // pieces)
            parts = [frame[at : at + size] for at in range(0, len(frame), size)]
            for stale in frames:
                self.arriving.append((now, stale))
            for number, part in enumerate(parts, start=1):
                self.arriving.append((now + delay * number / len(parts), part))

    def recv(self, size: int) -> bytes:
        assert self.supervisor is not None
        if not self.arriving:
            self._wait(float("inf"))
        at, chunk = self.arriving[0]
        self._wait(at - self.supervisor.clock.now)
        self.arriving.pop(0)
        return chunk

    def close(self) -> None:
        return None


class SupervisorSockets:
    """The socket module `SupervisorClient.connect` uses, answered by the scripted supervisors."""

    AF_UNIX = socket.AF_UNIX
    SOCK_STREAM = socket.SOCK_STREAM

    def __init__(self) -> None:
        self.supervisors: dict[str, ScriptedSupervisor] = {}
        self.opened: list[ScriptedSocket] = []

    def add(self, supervisor: ScriptedSupervisor) -> ScriptedSupervisor:
        self.supervisors[str(supervisor.run_dir / protocol.SOCKET_NAME)] = supervisor
        return supervisor

    def socket(self, *_args: Any) -> ScriptedSocket:
        opened = ScriptedSocket(self)
        self.opened.append(opened)
        return opened


@dataclass
class Transport:
    before_send: Any = None


class HandoffTestCase(unittest.TestCase):
    def setUp(self) -> None:
        # A short root: a Unix socket address is about 100 bytes.
        self.root = Path(tempfile.mkdtemp(prefix="h", dir="/tmp"))
        self.clock = FakeClock()
        self.sockets = SupervisorSockets()
        patcher = mock.patch.object(client_module, "socket", self.sockets)
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


class TheDeadlineBoundsTheWholeFramedExchangeTests(HandoffTestCase):
    """review-11 BLOCKER-budget-escape: the deadline reaches every syscall of the real client.

    A socket timeout bounds one blocking call, not a framed request. The operation's deadline is
    given to `SupervisorClient` itself, which recomputes what is left before `connect`, `sendall`
    and each `recv`, and attempts nothing once it is gone; so a reply that arrives in pieces, after
    stale or pushed frames, or after a slow send cannot outlast it.
    """

    def test_a_status_answered_in_three_pieces_over_9s_costs_the_4s_allowance(self) -> None:
        supervisor, run = self.head(status_delay=9.0)
        supervisor.fragments = 3
        receipt, budget, took = self.tick(run, self.handoff(supervisor))
        self.assertEqual(receipt.handoff_stage, HANDOFF_SETTLE)
        self.assertIn("did not answer within this pass's allowance", receipt.reason)
        self.assertAlmostEqual(took, 4.0, places=6, msg="the review's repro took and charged 9 s here")
        self.assertAlmostEqual(budget.spent, 4.0, places=6)
        [opened] = self.sockets.opened
        receive_bounds = opened.timeouts[-2:]
        self.assertAlmostEqual(receive_bounds[0], 4.0 - 0.001, places=6, msg="the first recv got what was left")
        self.assertAlmostEqual(receive_bounds[1], 1.0 - 0.001, places=6, msg="the second only what was left then")

    def test_stale_and_pushed_frames_ahead_of_the_answer_spend_the_same_deadline(self) -> None:
        supervisor, run = self.head(status_delay=3.5)
        supervisor.stale = [{"event": protocol.EVENT_OUTPUT, "data": ""}, {protocol.REQUEST_ID: 99, "ok": True}]
        supervisor.fragments = 2
        receipt, _, took = self.tick(run, self.handoff(supervisor), budget=2.0)
        self.assertEqual(receipt.handoff_stage, HANDOFF_SETTLE)
        self.assertAlmostEqual(took, 2.0, places=6)

    def test_an_admission_answer_in_pieces_is_pending_at_the_deadline_and_never_offered_twice(self) -> None:
        supervisor, run = self.head(admission_delay=9.0)
        supervisor.fragments = 3
        handoff = self.handoff(supervisor)
        first, _, took = self.tick(run, handoff)
        self.assertAlmostEqual(took, 4.0, places=6)
        self.assertEqual(first.status, HEAD_BUSY)
        self.assertIsNone(first.failure)
        self.assertEqual(supervisor.offered, [SUBJECT])
        supervisor.admission_delay, supervisor.fragments = 0.002, 1
        self.clock.advance(60.0)
        self.assertEqual(self.tick(run, handoff)[0].status, HEAD_OK)
        self.assertEqual(supervisor.offered, [SUBJECT, f"{SUBJECT}:submit"])

    def test_a_follower_whose_status_comes_in_pieces_stops_at_the_deadline(self) -> None:
        supervisor, run = self.head(write_seconds=9.0)
        handoff = self.handoff(supervisor)
        supervisor.status_delay, supervisor.fragments = 0.9, 3
        receipt, _, took = self.tick(run, handoff)
        self.assertLessEqual(took, 4.0 + 1e-6)
        self.assertEqual(receipt.handoff_stage, HANDOFF_TYPED)
        self.assertEqual(supervisor.offered, [SUBJECT])

    def test_a_deadline_that_passes_during_the_send_offers_nothing(self) -> None:
        supervisor, run = self.head()
        handoff = self.handoff(supervisor)

        class SlowSecondSend(ScriptedSocket):
            """The status goes out at once; the line's own send would need 5 s, past the allowance."""

            sent = 0

            def sendall(inner, data: bytes) -> None:
                SlowSecondSend.sent += 1
                supervisor.send_delay = 0.0 if SlowSecondSend.sent == 1 else 5.0
                ScriptedSocket.sendall(inner, data)

        with mock.patch.object(self.sockets, "socket", lambda *_a: SlowSecondSend(self.sockets)):
            receipt, _, took = self.tick(run, handoff)
        self.assertLessEqual(took, 4.0 + 1e-6)
        self.assertEqual(receipt.status, HEAD_BUSY)
        self.assertEqual(receipt.handoff_stage, HANDOFF_SETTLE, "the line never reached the supervisor")
        self.assertEqual(supervisor.offered, [])
        supervisor.send_delay = 0.0
        self.clock.advance(60.0)
        self.assertEqual(self.tick(run, handoff)[0].status, HEAD_OK)
        self.assertEqual(supervisor.offered, [SUBJECT, f"{SUBJECT}:submit"], "typed once, after nothing was taken")

    def test_a_native_socket_fragmenting_its_answer_cannot_outlast_the_operation(self) -> None:
        """The review's socketpair repro: 0.12 s allowed against a reply spread over 0.24 s."""
        _, run = self.head()
        client_end, server_end = socket.socketpair()
        self.addCleanup(client_end.close)
        frame = protocol.encode_frame({"ok": True, "alive": True, "journal_seq": 2, protocol.REQUEST_ID: 1})
        cut = len(frame) // 3

        def respond() -> None:
            try:
                server_end.recv(65536)
                for piece in (frame[:cut], frame[cut : 2 * cut], frame[2 * cut :]):
                    time.sleep(0.08)
                    server_end.sendall(piece)
            except OSError:
                pass
            finally:
                server_end.close()

        class Connected:
            """A real connected socket whose `connect` was already made by `socketpair`."""

            def __init__(self, *_args: Any) -> None:
                self.real = client_end

            def connect(self, _path: str) -> None:
                return None

            def __getattr__(self, name: str) -> Any:
                return getattr(self.real, name)

        peer = threading.Thread(target=respond)
        peer.start()
        runtime = self.runtime()
        native = mock.Mock(AF_UNIX=socket.AF_UNIX, SOCK_STREAM=socket.SOCK_STREAM, socket=Connected)
        with (
            mock.patch.object(client_module, "socket", native),
            handoff_budget(0.12, clock=time.monotonic) as budget,
            budget.operation(SUBJECT) as operation,
        ):
            probe = runtime._probe(runtime._address(run), operation)
        peer.join(timeout=2.0)
        self.assertIsNone(probe.status, "the reply was not complete within the deadline")
        self.assertTrue(probe.timed_out)
        # Real scheduling: the deadline is the bound, give or take a scheduler's slice.
        self.assertLess(budget.spent, 0.12 + 0.05, "each recv got a fresh socket timeout")
        self.assertGreaterEqual(budget.spent, 0.11)


class AFatalPrefixIsClosedWithinTheAllowanceTests(HandoffTestCase):
    """The partial line stays a fatal refusal at once; the remote drain spends only real allowance."""

    def journal_a_prefix(self, supervisor: ScriptedSupervisor) -> None:
        supervisor.append(
            "input.accepted", 0.0, subject=SUBJECT, bytes=7, offered_bytes=60, complete=False, state="stalled", delivery=1
        )

    def test_with_no_allowance_the_prefix_is_refused_without_a_single_connection(self) -> None:
        supervisor, run = self.head(connect_delay=3.0)
        handoff = self.handoff(supervisor)
        self.journal_a_prefix(supervisor)
        receipt, budget, took = self.tick(run, handoff, budget=0.0)
        self.assertEqual(receipt.status, HEAD_ALIVE)
        self.assertFalse(receipt.ok)
        self.assertEqual((supervisor.connects, took, budget.spent), (0, 0.0, 0.0), "no 0.5 s is invented")
        self.assertFalse(supervisor.drained, "and no drain is claimed")

    def test_a_drain_the_allowance_cannot_finish_is_owed_and_paid_by_a_later_pass(self) -> None:
        supervisor, run = self.head()
        handoff = self.handoff(supervisor)
        self.journal_a_prefix(supervisor)
        supervisor.connect_delay = 9.0
        receipt, _, took = self.tick(run, handoff)
        self.assertFalse(receipt.ok)
        self.assertAlmostEqual(took, 4.0, places=6)
        self.assertFalse(supervisor.drained)
        # A restarted dispatcher: the journal still holds the prefix, so it is refused again, never
        # retyped, and this pass's allowance tells the supervisor and reads it back.
        supervisor.connect_delay = 0.001
        self.clock.advance(60.0)
        again, _, _ = self.tick(run, handoff)
        self.assertFalse(again.ok)
        self.assertTrue(supervisor.drained)
        self.assertEqual(supervisor.offered, [])

    def test_a_slow_drain_readback_ends_at_the_deadline_without_a_claim(self) -> None:
        supervisor, run = self.head()
        handoff = self.handoff(supervisor)
        self.journal_a_prefix(supervisor)
        supervisor.status_delay, supervisor.fragments = 1.0, 1
        supervisor.drain_delay = 9.0
        receipt, _, took = self.tick(run, handoff)
        self.assertFalse(receipt.ok)
        self.assertLessEqual(took, 4.0 + 1e-6)


class ThreeSlowPeersAndAQuietOneTests(HandoffTestCase):
    def test_three_peers_with_fragmented_slow_status_fit_and_a_quiet_fourth_still_moves(self) -> None:
        slow = [self.head(status_delay=9.0) for _ in range(3)]
        for supervisor, _ in slow:
            supervisor.fragments = 3
        quiet = self.head()
        heads = [*slow, quiet]
        handoffs = [self.handoff(supervisor) for supervisor, _ in heads]
        for tick in range(3):
            started = self.clock.now
            with handoff_budget(4.0, cards=4, clock=self.clock.monotonic) as budget:
                for (_, run), handoff in zip(heads, handoffs, strict=True):
                    with budget.card():
                        self.deliver(run, handoff)
            self.assertLessEqual(self.clock.now - started, 4.0 + 1e-6, f"tick {tick}")
            self.assertAlmostEqual(budget.spent, self.clock.now - started, places=6)
            self.clock.advance(10.0)
        self.assertEqual(quiet[0].offered, [SUBJECT, f"{SUBJECT}:submit"])
        self.assertTrue(all(supervisor.offered == [] for supervisor, _ in slow))


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


class AReviewerPendingHandoffAnswersToTheSharedLivenessRuleTests(HandoffTestCase):
    """review-11 BLOCKER-reviewer-pending-liveness, through the real `resolve_launch_intent`.

    The reviewer's head is behind the scripted supervisor and its exact heartbeat is live. Its
    pending handoff reaches `review.reviewer_pending_liveness` before the retry, the backoff or
    adoption can return: exact-source provider looks on the shared schedule, the existing confirmed
    stop and relaunch when they are exhausted, and nothing typed again.
    """

    REVIEW_RUN: ClassVar[dict[str, Any]] = {
        "run_id": "r1",
        "workspace": "/unused",
        "task_ref": {"kind": "card", "ref": "sample-1"},
        "role": "reviewer",
        "spec": {"profile_id": "claude", "adapter": "claude"},
    }

    def setUp(self) -> None:
        super().setUp()
        self.task = {"ref": "sample-1", "comments": []}
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
        self.record.review_head_run = dict(self.REVIEW_RUN)
        self.record.launch_intent = {
            "role": "review",
            "head": "claude",
            "at": EPOCH,
            "run_id": "r1",
            "leaf": "leaf-r1",
            "pid_file": "/tmp/r1.pid",
            "task": "card:sample-1",
            "aborted": True,
            "head_run": dict(self.REVIEW_RUN),
        }
        launch.defer_pending_launch_delivery(
            self.record, {"subject": "reviewer-launch", "handoff_stage": HANDOFF_SETTLE}, worker_fenced=True
        )
        self.records = {"sample-1": self.record}
        self.payload: dict[str, Any] = {}
        self.runtime = mock.Mock()
        self.cursor = "cursor-1"
        self.source_state = "unavailable"
        self.runtime.host.provider_progress.side_effect = self.provider
        alive = mock.patch.object(launch, "launch_intent_liveness", return_value={"alive": True, "pid_known": True})
        alive.start()
        self.addCleanup(alive.stop)
        wall = mock.patch.object(dispatcher_review.time, "time", side_effect=lambda: self.clock.wall())
        wall.start()
        self.addCleanup(wall.stop)

    def provider(self, _task, record, kind):
        self.assertEqual(kind, "review")
        if self.source_state != "observed":
            return {"state": self.source_state, "reason": "exact source says so"}
        from ummanu.runtime.head_run_binding import head_run_binding

        run_id, fingerprint = head_run_binding(record.review_head_run)
        return {
            "state": "observed",
            "admission": "accepted",
            "head_run_id": run_id,
            "head_run_fingerprint": fingerprint,
            "source": "claude-transcript",
            "source_fingerprint": "b" * 32,
            "cursor": self.cursor,
        }

    def pending_nudge(self, stage: str) -> None:
        def nudge(*_args):
            error = HostError(f"retained reviewer document nudge is pending: production handoff pending at {stage}")
            error.evidence = {"subject": "reviewer-launch", "handoff_stage": stage}
            raise error

        self.runtime.host.nudge_review_delivery.side_effect = nudge

    def resolve(self) -> dict[str, Any] | None:
        with handoff_budget(4.0, clock=self.clock.monotonic):
            return launch.resolve_launch_intent(self.runtime, self.task, self.records, self.payload)

    def test_twelve_recoveries_two_hours_apart_reach_the_confirmed_stop_with_nothing_retyped(self) -> None:
        # The review's repro: a live exact heartbeat, a supervisor that never answers in time.
        supervisor, run = self.head(status_delay=6.0)
        handoff = self.handoff(supervisor)
        backend = self.runtime_for_reviewer = self.runtime_()

        def nudge(*_args):
            receipt = backend.deliver(
                run, POINTER, subject="reviewer-launch", transport=Transport(), handoff=handoff
            )
            error = HostError(receipt.reason)
            error.evidence = receipt.evidence.to_json() if receipt.evidence is not None else {}
            raise error

        self.runtime.host.nudge_review_delivery.side_effect = nudge
        outcomes = []
        for _ in range(12):
            outcomes.append(self.resolve())
            if outcomes[-1] and outcomes[-1]["action"] == "review-launch-undeliverable":
                break
            self.clock.advance(7200.0)
        self.assertEqual(outcomes[-1]["action"], "review-launch-undeliverable")
        self.assertLessEqual(len(outcomes), 4, "three scheduled looks, then the existing end")
        self.assertEqual(outcomes[0]["action"], "review-launch-handoff-pending")
        self.runtime.host.stop_review.assert_called_once()
        self.assertGreaterEqual(self.runtime.host.provider_progress.call_count, 3, "exact-source looks were made")
        self.assertEqual(supervisor.offered, [], "nothing typed, nothing replayed")
        delivery = launch.launch_delivery(self.record.launch_intent or {}) if self.record.launch_intent else {}
        self.assertEqual(int(delivery.get("attempts") or 0), 0, "no delivery attempt was spent by expiry")

    def runtime_(self) -> LocalPtyHeadRuntime:
        return self.runtime_factory()

    def runtime_factory(self) -> LocalPtyHeadRuntime:
        return HandoffTestCase.runtime(self)

    def test_every_pending_stage_with_a_stalled_cursor_ends_the_same_way(self) -> None:
        for stage in (HANDOFF_SETTLE, HANDOFF_TYPED, HANDOFF_SUBMITTED):
            with self.subTest(stage=stage):
                self.setUp()
                self.source_state = "observed"
                self.pending_nudge(stage)
                actions = []
                for _ in range(8):
                    result = self.resolve()
                    actions.append(result["action"] if result else None)
                    if actions[-1] == "review-launch-undeliverable":
                        break
                    self.clock.advance(120.0)
                self.assertEqual(actions[-1], "review-launch-undeliverable")
                self.runtime.host.stop_review.assert_called_once()

    def test_provider_progress_keeps_it_alive_and_is_not_a_delivery(self) -> None:
        self.source_state = "observed"
        self.pending_nudge(HANDOFF_SUBMITTED)
        for step in range(8):
            self.cursor = f"cursor-{step}"
            result = self.resolve()
            self.assertEqual(result["action"], "review-launch-handoff-pending")
            self.clock.advance(120.0)
        self.runtime.host.stop_review.assert_not_called()
        self.assertEqual(launch.launch_delivery(self.record.launch_intent)["state"], LAUNCH_DELIVERY_HANDOFF_PENDING)

    def test_a_source_naming_another_head_run_ends_it_at_once(self) -> None:
        self.source_state = "identity_mismatch"
        self.pending_nudge(HANDOFF_TYPED)
        result = self.resolve()
        self.assertEqual(result["action"], "review-launch-undeliverable")
        self.runtime.host.stop_review.assert_called_once()
        self.runtime.host.nudge_review_delivery.assert_not_called()

    def test_a_source_unavailable_before_its_first_prompt_can_still_baseline_later(self) -> None:
        self.pending_nudge(HANDOFF_SETTLE)
        self.resolve()
        self.clock.advance(20.0)
        self.source_state = "observed"
        self.assertEqual(self.resolve()["action"], "review-launch-handoff-pending")
        episode = launch.launch_delivery(self.record.launch_intent)["liveness"]
        self.assertTrue(episode["baseline_established"])
        self.assertFalse(episode["source_rejected"])

    def test_a_verdict_already_on_the_card_wins_over_a_deferred_stage(self) -> None:
        self.pending_nudge(HANDOFF_SUBMITTED)
        self.task["comments"] = [{"marker": "review:green", "body": "[review:green] fine"}]
        self.resolve()
        self.runtime.host.nudge_review_delivery.assert_not_called()
        self.runtime.host.provider_progress.assert_not_called()
        self.assertEqual(self.record.state, "reviewing", "adopted as the launch it was")

    def test_an_unchanged_pending_pass_writes_nothing(self) -> None:
        self.source_state = "observed"
        self.pending_nudge(HANDOFF_TYPED)
        self.resolve()
        self.clock.advance(1.0)
        self.resolve()  # the first look at the unmoved cursor is new liveness evidence
        self.runtime.save_records.reset_mock()
        self.clock.advance(5.0)
        self.assertEqual(self.resolve()["action"], "review-launch-handoff-pending")
        self.runtime.save_records.assert_not_called()


class TheSharedLivenessRuleTests(unittest.TestCase):
    """`handoff_liveness`: the one rule both callers apply, read back from what they persist."""

    RUN: ClassVar[dict[str, Any]] = AReviewerPendingHandoffAnswersToTheSharedLivenessRuleTests.REVIEW_RUN

    def evidence(self, cursor: str = "c1", state: str = "observed") -> dict[str, Any]:
        from ummanu.runtime.head_run_binding import head_run_binding

        run_id, fingerprint = head_run_binding(self.RUN)
        return {
            "state": state,
            "admission": "accepted",
            "head_run_id": run_id,
            "head_run_fingerprint": fingerprint,
            "source": "s",
            "source_fingerprint": "c" * 32,
            "cursor": cursor,
        }

    def test_the_schedule_survives_a_restart_and_spends_one_attempt_a_look(self) -> None:
        from ummanu.dispatch import handoff_liveness as rule
        from ummanu.dispatch.worker_lifecycle import WorkerContinuationLiveness

        episode = WorkerContinuationLiveness.begin(self.RUN)
        self.assertEqual(rule.observe_pending_handoff(episode, self.evidence(), now=500.0, head_run=self.RUN).verdict,
                         rule.PENDING_QUIET)
        verdicts = []
        for now in (510.0, 540.0, 1500.0, 1501.0, 1502.0):
            episode = WorkerContinuationLiveness.from_json(episode.to_json())  # a new process every look
            verdicts.append(rule.observe_pending_handoff(episode, self.evidence(), now=now, head_run=self.RUN).verdict)
        self.assertEqual(verdicts, ["quiet", "attempt", "attempt", "attempt", "quiet"])
        self.assertTrue(rule.no_progress_exhausted(episode))

    def test_heartbeats_and_receipts_are_not_inputs_only_the_cursor_is(self) -> None:
        from ummanu.dispatch import handoff_liveness as rule
        from ummanu.dispatch.worker_lifecycle import WorkerContinuationLiveness

        episode = WorkerContinuationLiveness.begin(self.RUN)
        rule.observe_pending_handoff(episode, self.evidence(), now=500.0, head_run=self.RUN)
        self.assertEqual(
            rule.observe_pending_handoff(episode, self.evidence("c2"), now=1500.0, head_run=self.RUN).verdict,
            rule.PENDING_PROGRESSED,
        )
        self.assertEqual(episode.busy_attempts, 0)

    def test_an_unavailable_source_is_unprovable_unless_the_caller_counts_it(self) -> None:
        from ummanu.dispatch import handoff_liveness as rule
        from ummanu.dispatch.worker_lifecycle import WorkerContinuationLiveness

        unavailable = {"state": "unavailable", "reason": "no transcript"}
        worker = WorkerContinuationLiveness.begin(self.RUN)
        self.assertEqual(
            rule.observe_pending_handoff(worker, unavailable, now=0.0, head_run=self.RUN).verdict, rule.PENDING_UNPROVABLE
        )
        reviewer = WorkerContinuationLiveness.begin(self.RUN)
        unproven = rule.UnprovenSchedule()
        verdicts = []
        for now in (1000.0, 1031.0, 1091.0, 1211.0):
            unproven = rule.UnprovenSchedule.from_json(unproven.to_json())
            verdicts.append(
                rule.observe_pending_handoff(reviewer, unavailable, now=now, head_run=self.RUN, unproven=unproven).verdict
            )
            # The episode itself stays a clean, unbaselined one a reload accepts.
            self.assertEqual(WorkerContinuationLiveness.from_json(reviewer.to_json()), reviewer)
        self.assertEqual(verdicts, ["quiet", "attempt", "attempt", "attempt"])
        self.assertTrue(rule.no_progress_exhausted(reviewer, unproven))


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
