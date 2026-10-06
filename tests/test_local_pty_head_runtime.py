"""secretary-1466: `LocalPtyHeadRuntime`, against real processes, real ptys and a real socket.

Nothing here is faked, for the same reason nothing in `test_local_pty_supervisor` is: the facts
this backend exists to establish — a delivery that is admitted before it arrives, a head that
stopped reading its terminal, an attachment a caller can drop without hurting the head — are facts
about a kernel, and a fake substrate would settle none of them. Every test starts a real
supervisor, which forks a real trivial child onto a real pty, and every test takes back what it
started on success and on failure alike.

`LocalPtyHeadRuntimeContractTests` is the boundary's own suite from `tests.support.
head_runtime_contract`, run here against this backend and in `test_head_runtime` against the
legacy one. What is in this file beside it is what only this backend can be asked:

  * a delivery that stalls half-way, and the next one *not* being glued to the prefix it left —
    checked against the head's own terminal, record by record, because that is the only witness
    that can tell two payloads apart from one sentence;
  * the wait being derived from the substrate's own bound, so that no configuration lets this
    runtime stop watching a delivery the substrate is still writing;
  * `attach` being a stream rather than an address, and detaching being harmless;
  * `request_drain` reaching the process that owns the head, with `head_signalled` read back
    rather than assumed;
  * a supervisor that died being classified by the head's launch identity and not by the socket;
  * `stop_if_quiescent` in the order secretary-1462 fixed, with liveness outranking the terminal.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import shutil
import signal
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

from ummanu.dispatch.watchdog import clear_head_heartbeat, head_process_status
from ummanu.runtime.head import (
    EXITED,
    HEAD_ALIVE,
    HEAD_BUSY,
    HEAD_DRAINING,
    HEAD_GONE,
    HEAD_OK,
    HEAD_UNSUPPORTED,
    HeadRun,
    HeadSpec,
    NudgePointer,
    StopInitiator,
    TaskRef,
    TurnLease,
)
from ummanu.runtime.head.local_pty import protocol
from ummanu.runtime.head.local_pty.client import SupervisorClient
from ummanu.runtime.head.local_pty.journal import (
    DRAIN_REQUESTED,
    INPUT_ACCEPTED,
    JOURNAL_SCHEMA_VERSION,
    JOURNAL_TAIL_BYTES,
    PROVIDER_PROGRESSED,
    RUN_EXITED,
    RUN_STARTED,
    TURN_FINISHED,
    TURN_STARTED,
    JournalWriter,
    read_events,
    read_tail,
)
from ummanu.codex_provider_events import CodexProviderEventIngress
from ummanu.runtime.codex_preflight import codex_provider_source_descriptor
from tests.support.head_runtime_contract import HeadRuntimeContract
from ummanu.runtime.local_pty_head import (
    ADOPTED_TURN_SUBJECT,
    DELIVER_DRAINED_BEFORE_THIS_RUNTIME,
    DELIVER_NOT_SUBMITTED,
    DELIVER_STATE_UNKNOWN,
    DELIVERY_ARRIVED,
    DELIVERY_LANDED_NOTHING,
    DELIVERY_LEFT_A_PREFIX,
    DELIVERY_STATE_UNKNOWN,
    DELIVERY_UNESTABLISHED,
    DRAIN_AFTER_PARTIAL_DELIVERY,
    DRAIN_AFTER_UNESTABLISHED_DELIVERY,
    OBSERVE_HEAD_EXITED,
    OBSERVE_NO_RUN_DIRECTORY,
    OBSERVE_STATUS_UNREADABLE,
    OBSERVE_SUPERVISOR_UNREACHABLE,
    START_HEAD_ALREADY_UP,
    START_TURN_IN_FLIGHT,
    STOP_ACTIVITY_SINCE,
    STOP_TURN_IN_FLIGHT,
    UNDECLARED_DELIVERY_BOUND,
    AttachedStream,
    LocalPtyHeadRuntime,
    LocalPtyRuntimeError,
    _declared_bound,
    _has_exited,
)

REPO = Path(__file__).resolve().parents[1]
CHILD = REPO / "tests" / "fixtures" / "local_pty_child.py"
SLOW_READER = REPO / "tests" / "fixtures" / "local_pty_slow_reader.py"
CHILD_COMMAND = f"{sys.executable} -u {CHILD}"
#: A head that keeps running when its terminal is hung up and its supervisor is gone. It is the
#: orphan case: the one a socket that stopped answering must never be reported as a head that ended.
ORPHAN = REPO / "tests" / "fixtures" / "local_pty_orphan.py"
#: A head that reports what its own terminal handed it, record by record. The only witness that
#: can say whether two payloads reached the head as one sentence; every other one is a receipt.
LINE_READER = REPO / "tests" / "fixtures" / "local_pty_line_reader.py"
DEAF_COMMAND = f"{sys.executable} -u {ORPHAN}"
#: A head that keeps a composer the way an agent's TUI does: a line feed is a line break in it, and
#: only a carriage return sends it (issue:70562b15a7dc8764437e).
FAKE_TUI = REPO / "tests" / "fixtures" / "local_pty_fake_tui.py"
CODEX = HeadSpec(profile_id="codex-worker", adapter="codex", effort="high", codex_mode="tui")
#: Long enough that a mid-turn assertion is never racing the child's own silence, short enough
#: that a test which has to see the turn end waits about two seconds for it.
TURN_SECONDS = 2.0
#: The grace these tests give this runtime over each head's own delivery bound. Deliberately small
#: and deliberately not zero: it is a margin over the substrate's number, not a limit of its own.
TEST_GRACE_SECONDS = 0.5


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return False
    close = stat.rfind(")")
    fields = stat[close + 2 :].split()
    return bool(fields) and fields[0] != "Z"


def _kill(pid: int, number: int = signal.SIGKILL, *, group: bool = False) -> None:
    if pid <= 0:
        return
    try:
        os.killpg(pid, number) if group else os.kill(pid, number)
    except OSError:
        pass


class LocalPtyRuntimeTestCase(unittest.TestCase):
    """A runtime over a temporary run root, and every process it started given back."""

    def setUp(self) -> None:
        # /tmp rather than the workspace: a Unix socket address is bounded at about a hundred
        # bytes and a workspace path with a run id under it does not fit inside one.
        self.root = Path(tempfile.mkdtemp(prefix="lp-runtime-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.workspace = Path(tempfile.mkdtemp(prefix="lp-workspace-"))
        self.addCleanup(shutil.rmtree, self.workspace, ignore_errors=True)
        self.addCleanup(self._reap_everything)
        # The grace is the only thing about waiting this runtime is told, and it only extends the
        # wait past the substrate's own bound. A short one here keeps a test that has to watch a
        # head's delivery bound expire from paying the production margin on top of it; it can
        # never make this runtime stop watching before the substrate stops writing, which is the
        # property every test below relies on.
        self.runtime = LocalPtyHeadRuntime(
            self.root, head_process_status=head_process_status, delivery_grace=TEST_GRACE_SECONDS
        )
        self.task = TaskRef.card("ummanu-1465", document=f"{self.workspace}/TASK.md")

    # -- processes -----------------------------------------------------------------------------

    def _pids(self) -> list[tuple[int, int]]:
        """Every (head, supervisor) pair this test's run root knows about."""
        pairs = []
        for run_dir in sorted(self.root.glob("*")):
            if not run_dir.is_dir():
                continue
            pairs.append((self._head_pid(run_dir), self._supervisor_pid(run_dir)))
        return pairs

    def _head_pid(self, run_dir: Path) -> int:
        try:
            record = json.loads((run_dir / protocol.PID_FILE_NAME).read_text(encoding="utf-8"))
            return int(record.get("pid") or 0)
        except (OSError, ValueError, TypeError):
            return 0

    def _supervisor_pid(self, run_dir: Path) -> int:
        try:
            return int((run_dir / protocol.SUPERVISOR_PID_NAME).read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return 0

    def _reap_everything(self) -> None:
        for head, supervisor in self._pids():
            _kill(head, group=True)
            _kill(head)
            _kill(supervisor)
        for head, _supervisor in self._pids():
            self._await(lambda pid=head: not _alive(pid), timeout=5.0, soft=True)

    def _await(self, predicate, *, timeout: float = 15.0, message: str = "", soft: bool = False):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        if predicate():
            return True
        if not soft:
            self.fail(message or f"condition never held within {timeout:g}s")
        return False

    # -- the runtime ---------------------------------------------------------------------------

    def bring_up(self, *, pointer: NudgePointer | None = None, command: str = CHILD_COMMAND, **options):
        receipt = self.runtime.start(
            CODEX,
            str(self.workspace),
            self.task,
            command=command,
            title="ummanu-1465 worker",
            pointer=pointer,
            role="worker",
            quiet_seconds=options.pop("quiet_seconds", 0.4),
            **options,
        )
        return receipt

    def live_run(self, **options) -> HeadRun:
        receipt = self.bring_up(**options)
        self.assertTrue(receipt.ok, receipt.reason)
        return receipt.run

    def deliver_line(self, run: HeadRun, text: str, *, subject: str = ""):
        return self.runtime.deliver(run, NudgePointer.line(text), subject=subject)

    def begin_turn(self, run: HeadRun, *, subject: str = "") -> TurnLease:
        receipt = self.deliver_line(run, f"busy {TURN_SECONDS}", subject=subject)
        self.assertEqual(receipt.status, HEAD_OK, receipt.reason)
        return receipt.lease

    def end_turn(self, run: HeadRun) -> None:
        """Wait until the supervisor itself says the turn has closed.

        Deliberately read from the substrate rather than through `observe`: the tests that call
        this go on to make their own observation, and an observation made here would be the one
        that closed the lease.
        """
        address = self.runtime._address(run)
        self._await(
            lambda: not self._status(address.socket_path).get("turn_open", True),
            message="the head's turn never closed",
        )

    def _status(self, socket_path: Path) -> dict:
        try:
            with SupervisorClient.connect(socket_path, timeout=5.0) as client:
                return client.status()
        except Exception:  # noqa: BLE001 - a supervisor that cannot be asked answers nothing
            return {}

    def _admitted_status(self, socket_path: Path) -> dict:
        """The substrate's own state, asked over a connection the supervisor actually took.

        A caller letting go frees its slot in the supervisor's loop, not in its own `close()`, so
        a question asked at the connection bound in that gap is answered by the refusal frame of
        the bound instead of by the substrate — an answer that carries no state to read. Only the
        supervisor can say which of the two arrived, and `ok` is where it says it.
        """
        answer: dict = {}

        def taken() -> bool:
            nonlocal answer
            answer = self._status(socket_path)
            return bool(answer.get("ok"))

        self._await(taken, message="the supervisor never took a question about its own state")
        return answer

    def payloads_delivered(self) -> int:
        """Everything every head under this root has actually been given, as its journal counts."""
        total = 0
        for run_dir in sorted(self.root.glob("*")):
            journal = run_dir / protocol.JOURNAL_NAME
            total += len(read_events(journal).of_kind(INPUT_ACCEPTED))
        return total

    def output_of(self, run: HeadRun) -> bytes:
        """What the head has printed, read straight from its supervisor."""
        address = self.runtime._address(run)
        try:
            with SupervisorClient.connect(address.socket_path, timeout=5.0) as client:
                return bytes(client.read_output()["bytes_data"])
        except Exception:  # noqa: BLE001 - a supervisor that cannot be asked has printed nothing
            return b""

    def orphan_head(self) -> HeadRun:
        """A head that will outlive its supervisor, waited for until it really will.

        The waiting is the point. `start` returns as soon as the head's shell has `exec`ed, which
        is before the interpreter behind that `exec` has installed anything: a supervisor killed in
        that window hangs the terminal up while `SIGHUP` is still the kernel's default, and the
        head dies of the very thing this fixture exists not to die of. Its first line is what says
        the disposition is in place.
        """
        run = self.live_run(command=DEAF_COMMAND)
        self._await(
            lambda: b"ORPHAN" in self.output_of(run),
            message="the orphan head never said it was up",
        )
        return run

    def next_tick(self) -> LocalPtyHeadRuntime:
        """The runtime the next dispatcher process would build: same run root, no memory.

        The production dispatcher is a systemd timer, so this — a fresh object over the same root,
        holding nothing about the heads the previous one started — is what a tick actually is.
        """
        return LocalPtyHeadRuntime(
            self.root, head_process_status=head_process_status, delivery_grace=TEST_GRACE_SECONDS
        )

    def adrift_run(self) -> HeadRun:
        return HeadRun(
            run_id="a-head-this-runtime-never-started",
            spec=CODEX,
            workspace=str(self.workspace),
            task_ref=self.task,
            role="worker",
        )

    def events(self, run: HeadRun):
        return read_events(self.root / run.run_id / protocol.JOURNAL_NAME)

    def head_pid_of(self, run: HeadRun) -> int:
        return self._head_pid(self.root / run.run_id)


class LocalPtyHeadRuntimeContractTests(LocalPtyRuntimeTestCase, HeadRuntimeContract):
    """The boundary's own suite, run against a backend made of real processes."""


class LocalPtyStartTests(LocalPtyRuntimeTestCase):
    def test_a_head_comes_up_addressed_by_its_socket_and_carrying_its_launch_identity(self) -> None:
        receipt = self.bring_up()

        self.assertEqual(receipt.status, HEAD_OK, receipt.reason)
        run = receipt.run
        self.assertEqual(run.handle, str(self.root / run.run_id / protocol.SOCKET_NAME))
        self.assertEqual(run.leaf, run.run_id)
        self.assertEqual(run.pid_file, str(self.root / run.run_id / protocol.PID_FILE_NAME))
        identity = json.loads(Path(run.pid_file).read_text(encoding="utf-8"))
        self.assertEqual(identity["run_id"], run.run_id)
        self.assertEqual(head_process_status(run.pid_file)["state"], "live-match", "the head is not running")

    def test_a_bring_up_over_a_run_whose_head_is_up_is_refused_on_its_launch_identity(self) -> None:
        """secretary-1468: the refusal that used to be the supervisor's is made before the spawn.

        This is the same scenario the supervisor's own `_refuse_a_second_head` covers — a run that
        already has a head, brought up again by a runtime holding no lease for it — asked one layer
        higher. It used to reach `_spawn` and come back as the supervisor's `already_running`
        refusal; it is now refused by the head's own launch identity, before anything is started,
        because a refusal that needs a second supervisor process to be forked first is not a
        precondition. The supervisor's check stays where it is and is exercised below, for the case
        this one deliberately declines to answer.
        """
        run = self.live_run()
        self.runtime.activity.forget(run.run_id)

        receipt = self.bring_up(run_id=run.run_id, timeout=5.0)

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.status, HEAD_BUSY, "a head that is already up is a busy refusal")
        self.assertEqual(receipt.evidence["refusal"], START_HEAD_ALREADY_UP)
        self.assertIn(run.run_id, receipt.reason)
        self.assertIsNone(receipt.lease, "no turn was outstanding; the record is what refused")
        self.assertTrue(_alive(self.head_pid_of(run)), "the refused bring-up took the head with it")

    def test_a_bring_up_that_cannot_be_made_is_a_receipt_and_not_an_exception(self) -> None:
        """A supervisor refusing to start a second head for a run that already has one.

        Reached by removing the launch-identity record: with no record to read, the precondition
        above declines to answer and the bring-up goes ahead, exactly as intended — and runs into
        the run directory lock the live supervisor is holding. Both halves are the point. An
        unreadable record does not fence a run out, and the refusal it lets through is still a
        receipt carrying the operation's own type rather than an exception.
        """
        run = self.live_run()
        self.runtime.activity.forget(run.run_id)
        # The record is what the teardown reads to find this head, so it is given the pid first.
        self.addCleanup(_kill, self.head_pid_of(run))
        Path(run.pid_file).unlink()
        self.assertFalse(
            head_process_status(run.pid_file).get("known"), "the record is meant to be unreadable"
        )

        receipt = self.bring_up(run_id=run.run_id, timeout=5.0)

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.status, HEAD_ALIVE, "something is still running under that run id")
        self.assertIsNotNone(receipt.failure, "the refusal travels as the operation's own type")

    def test_this_runtime_is_not_built_without_the_products_own_liveness_reader(self) -> None:
        """A second pid-file reader invented here is the failure this argument exists to prevent."""
        with self.assertRaises(LocalPtyRuntimeError):
            LocalPtyHeadRuntime(self.root, head_process_status=None)  # type: ignore[arg-type]

    def test_a_bring_up_over_a_head_that_is_running_a_turn_is_a_receipt_not_an_exception(self) -> None:
        run = self.live_run()
        self.begin_turn(run)

        second = self.bring_up(run_id=run.run_id)

        self.assertEqual(second.status, HEAD_BUSY)
        self.assertIsNotNone(second.lease)
        self.assertEqual(second.evidence["refusal"], START_TURN_IN_FLIGHT)


class LocalPtyRestartTests(LocalPtyRuntimeTestCase):
    """secretary-1468: one durable head, one run, across the restart of the thing that started it.

    The production dispatcher is a systemd timer: every tick is a new process, so `HeadActivity` —
    which is a field of the runtime object — is empty at the start of every one of them. An
    invariant kept only in that object therefore holds for exactly one tick, which is not what
    "a bring-up over a head that is already up is refused" is supposed to mean. These tests
    construct the second runtime the way the timer constructs it: a fresh object over the same run
    root, holding no memory of the head the first one started.
    """

    def _next_tick(self) -> LocalPtyHeadRuntime:
        """The runtime the next dispatcher process would build: same root, no memory."""
        return LocalPtyHeadRuntime(
            self.root, head_process_status=head_process_status, delivery_grace=TEST_GRACE_SECONDS
        )

    def _supervisors(self) -> int:
        return len([pid for _head, pid in self._pids() if pid])

    def test_a_restarted_control_plane_does_not_bring_a_second_head_up_over_a_live_run(self) -> None:
        run = self.live_run()
        self.assertEqual(self._supervisors(), 1, "the fixture is meant to start exactly one")
        supervisor = self._supervisor_pid(self.root / run.run_id)
        head = self.head_pid_of(run)

        restarted = self._next_tick()
        self.assertIsNone(restarted.activity.lease(run.run_id), "a new tick remembers nothing")
        receipt = restarted.start(
            CODEX,
            str(self.workspace),
            self.task,
            command=CHILD_COMMAND,
            title="ummanu-1468 worker",
            run=run,
            role="worker",
        )

        self.assertEqual(receipt.status, HEAD_BUSY, receipt.reason)
        self.assertEqual(receipt.evidence["refusal"], START_HEAD_ALREADY_UP)
        self.assertIn(run.run_id, receipt.reason)
        self.assertEqual(
            [d.name for d in sorted(self.root.glob("*")) if d.is_dir()],
            [run.run_id],
            "the refused bring-up left a second run behind",
        )
        self.assertEqual(self._supervisors(), 1, "a second supervisor was started over a live head")
        self.assertEqual(self._supervisor_pid(self.root / run.run_id), supervisor)
        self.assertEqual(self.head_pid_of(run), head, "the head was replaced under the same run")

    def _dispatcher_shaped(self, run: HeadRun) -> HeadRun:
        """The `HeadRun` a dispatcher tick really hands `start`, for a head that is already up.

        Not the one the previous bring-up returned. `DispatcherHost._launch` builds its preflight
        run from the launch intent every time — same `run_id`, `role` and task, but with the
        *watchdog* `pid_file` in the workspace rather than the run directory — and the same tick
        clears that path first (`_clear_head_heartbeat`), so it names nothing at the moment `start`
        is called. `_open_head_pane` passes an observer run of the same shape. A precondition that
        asked this file whether a head is up would always be told no.
        """
        heartbeat = self.workspace / "state" / "heads" / f"{run.role}-{run.run_id}.json"
        heartbeat.parent.mkdir(parents=True, exist_ok=True)
        heartbeat.write_text(json.dumps({"pid": 4321}), encoding="utf-8")
        clear_head_heartbeat(str(heartbeat))
        self.assertFalse(heartbeat.exists(), "the tick clears this path before every launch")
        payload = run.to_json()
        payload["pid_file"] = str(heartbeat)
        payload["handle"] = ""
        payload["leaf"] = ""
        return HeadRun.from_json(payload)

    def test_the_refusal_holds_for_the_head_run_the_dispatcher_actually_passes(self) -> None:
        """The production shape: an external watchdog `pid_file` the tick has just emptied.

        The head's own launch identity is at `root/run_id/head.pid` and nowhere else, so this is
        the case that decides whether the precondition reads the head or reads the caller.
        """
        run = self.live_run()
        supervisor = self._supervisor_pid(self.root / run.run_id)
        head = self.head_pid_of(run)
        preflight = self._dispatcher_shaped(run)

        receipt = self._next_tick().start(
            CODEX,
            str(self.workspace),
            self.task,
            command=CHILD_COMMAND,
            title="ummanu-1468 worker",
            pid_file=preflight.pid_file,
            run_id=preflight.run_id,
            run=preflight,
            role="worker",
        )

        self.assertEqual(receipt.status, HEAD_BUSY, receipt.reason)
        self.assertEqual(receipt.evidence["refusal"], START_HEAD_ALREADY_UP)
        self.assertEqual(
            receipt.evidence["pid_file"],
            str(self.root / run.run_id / protocol.PID_FILE_NAME),
            "the refusal has to name the head's own record, not the caller's heartbeat",
        )
        self.assertEqual(self._supervisors(), 1, "a second supervisor was started over a live head")
        self.assertEqual(self._supervisor_pid(self.root / run.run_id), supervisor)
        self.assertEqual(self.head_pid_of(run), head, "the head was replaced under the same run")

    def test_a_dispatcher_shaped_bring_up_over_a_dead_run_still_goes_ahead(self) -> None:
        """The negative side of the same shape: an emptied heartbeat is not a fence either.

        The precondition now ignores the caller's `pid_file` entirely, so the run that answers is
        the head's own — and when that one says the head is dead, the bring-up proceeds.
        """
        run = self.live_run()
        self.runtime.stop(run, StopInitiator(actor="test", reason="the head is gone"))
        preflight = self._dispatcher_shaped(run)

        receipt = self._next_tick().start(
            CODEX,
            str(self.workspace),
            self.task,
            command=CHILD_COMMAND,
            title="ummanu-1468 worker",
            pid_file=preflight.pid_file,
            run_id=preflight.run_id,
            run=preflight,
            role="worker",
        )

        self.assertEqual(receipt.status, HEAD_OK, receipt.reason)
        self.assertEqual(receipt.run.run_id, run.run_id)

    def test_the_refusal_reads_the_record_rather_than_the_run_directory(self) -> None:
        """A run directory and a socket that outlived their host's boot are not a live head.

        The record is rewritten with a `boot_id` from another boot — which is exactly the state a
        reboot leaves behind, a run directory full of files and a pid that means nothing — and the
        bring-up has to go ahead. Refusing here would fence every card that was running when the
        host went down out of every tick that follows.
        """
        run = self.live_run()
        self.runtime.stop(run, StopInitiator(actor="test", reason="make the record historical"))
        record = json.loads(Path(run.pid_file).read_text(encoding="utf-8"))
        record["boot_id"] = "00000000-0000-0000-0000-000000000000"
        Path(run.pid_file).write_text(json.dumps(record), encoding="utf-8")
        self.assertTrue(self.root.joinpath(run.run_id).is_dir(), "the run directory is meant to survive")

        receipt = self._next_tick().start(
            CODEX,
            str(self.workspace),
            self.task,
            command=CHILD_COMMAND,
            title="ummanu-1468 worker",
            role="worker",
        )

        self.assertEqual(receipt.status, HEAD_OK, receipt.reason)
        self.assertNotEqual(receipt.run.run_id, run.run_id)

    def test_a_dead_run_is_brought_up_again_under_its_own_run_id(self) -> None:
        """The negative side, on the run id itself: a dead head does not fence its own run out."""
        run = self.live_run()
        self.runtime.stop(run, StopInitiator(actor="test", reason="the head is gone"))
        self.assertEqual(head_process_status(run.pid_file)["state"], "dead")

        restarted = self._next_tick()
        receipt = restarted.start(
            CODEX,
            str(self.workspace),
            self.task,
            command=CHILD_COMMAND,
            title="ummanu-1468 worker",
            run_id=run.run_id,
            role="worker",
        )

        self.assertEqual(receipt.status, HEAD_OK, receipt.reason)
        self.assertEqual(receipt.run.run_id, run.run_id)
        self._await(
            lambda: head_process_status(receipt.run.pid_file).get("state") == "live-match",
            message="the run this refusal let through never got a head of its own",
        )


class LocalPtyDurableTurnTests(LocalPtyRuntimeTestCase):
    """secretary-1479: the turn, the epoch and the admission across the boundary of a tick.

    `HeadActivity` is a field of the runtime object and the production dispatcher builds a new one
    every tick, so a promise kept only in that object is a promise that holds for one tick. These
    tests are all the same shape as the bring-up tests of secretary-1468 and for the same reason:
    one instance does something, and the instance that has to answer for it is a *different* one,
    built the way a timer builds it and knowing nothing.

    Nothing new is stored to make that work. What answers is the head's own supervisor over its
    socket, and — when that supervisor is gone — a bounded tail of the journal it left behind.
    """

    def _debris_run(self, run_id: str, journal: bytes) -> HeadRun:
        """A run directory with a journal nobody can read anything out of, and no socket.

        The unknown of obligation 2: this head left something behind, so it is not the absence of
        a head, and no witness can say whether it is draining, mid-turn, or dead.
        """
        run_dir = self.root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / protocol.JOURNAL_NAME).write_bytes(journal)
        return HeadRun(
            run_id=run_id,
            spec=CODEX,
            workspace=str(self.workspace),
            task_ref=self.task,
            role="worker",
        )

    # -- criterion 1: a drain outlives the tick that requested it -------------------------------

    def test_a_drain_requested_in_one_tick_refuses_the_next_tick_s_delivery_by_name(self) -> None:
        run = self.live_run()
        self.runtime.request_drain(run, StopInitiator(actor="operator", reason="rotation"))
        delivered = self.payloads_delivered()

        restarted = self.next_tick()
        self.assertTrue(
            restarted.activity.admits(run.run_id),
            "the fixture is meant to start from an object that remembers nothing",
        )
        refused = restarted.deliver(run, NudgePointer.line("more work"), subject="worker-nudge")

        self.assertEqual(refused.status, HEAD_DRAINING, refused.reason)
        self.assertEqual(refused.reason, DELIVER_DRAINED_BEFORE_THIS_RUNTIME)
        self.assertTrue(refused.deferred, "a drained head is refused, never queued behind a turn")
        self.assertEqual(self.payloads_delivered(), delivered, "the refusal delivered it anyway")
        self.assertFalse(restarted.activity.admits(run.run_id), "admission stayed open")

    def test_a_drain_outlives_the_supervisor_that_recorded_it(self) -> None:
        """And this is the half the substrate cannot answer for.

        While the supervisor is alive it refuses a payload after a drain by itself, so the status a
        caller sees is right even when this runtime has forgotten why. Kill the supervisor and that
        second gate goes with it: the drain is then only in the journal it left behind, and a
        runtime that does not read it hands the head's own orphaned process a payload's worth of
        attempt instead of the refusal the drain earned.
        """
        run = self.orphan_head()
        self.runtime.request_drain(run, StopInitiator(actor="operator", reason="rotation"))
        supervisor = self._supervisor_pid(self.root / run.run_id)
        _kill(supervisor, signal.SIGKILL)
        self._await(lambda: not _alive(supervisor), message="the supervisor survived")
        self.assertIn(DRAIN_REQUESTED, self.events(run).kinds, "the drain is in the journal")
        delivered = self.payloads_delivered()

        refused = self.next_tick().deliver(run, NudgePointer.line("more work"))

        self.assertEqual(refused.status, HEAD_DRAINING, refused.reason)
        self.assertEqual(refused.reason, DELIVER_DRAINED_BEFORE_THIS_RUNTIME)
        self.assertEqual(self.payloads_delivered(), delivered)

    # -- criterion 2: an open turn outlives the tick that opened it -----------------------------

    def test_a_turn_open_in_one_tick_is_seen_open_in_the_next_and_is_not_interrupted(self) -> None:
        run = self.live_run()
        lease = self.begin_turn(run, subject="worker-nudge")
        delivered = self.payloads_delivered()
        head = self.head_pid_of(run)

        restarted = self.next_tick()
        second = restarted.deliver(run, NudgePointer.line("and this too"))

        self.assertEqual(second.status, HEAD_BUSY, second.reason)
        self.assertEqual(self.payloads_delivered(), delivered, "the running turn was written over")
        self.assertIsNotNone(second.lease, "the turn a previous tick granted is the one held now")
        self.assertNotEqual(second.lease.lease_id, lease.lease_id, "the lease is adopted, not re-granted")
        self.assertEqual(second.lease.subject, ADOPTED_TURN_SUBJECT)
        self.assertTrue(_alive(head), "the turn that was running was interrupted")
        self._await(lambda: b"DONE" in self.output_of(run), message="the turn never finished on its own")

    # -- criterion 3: the rotation happens where the lease closes -------------------------------

    def test_a_turn_granted_in_one_tick_closes_in_another_and_that_one_says_rotatable(self) -> None:
        run = self.live_run()
        self.begin_turn(run)
        self.runtime.request_drain(run, StopInitiator(actor="dispatcher", reason="rotation"))

        restarted = self.next_tick()
        mid_turn = restarted.observe(run)
        self.assertEqual(mid_turn.status, HEAD_OK, mid_turn.reason)
        self.assertIsNotNone(mid_turn.lease, "the turn the previous tick granted is still running")
        self.assertFalse(mid_turn.rotation_ready, "a head still mid-turn is not ready to rotate")

        self.end_turn(run)
        rotated = restarted.observe(run)

        self.assertIsNone(rotated.lease, "the last turn closed")
        self.assertTrue(
            rotated.rotation_ready,
            "the lease was granted by one instance and closed by another, and this is the one "
            "that has to answer whether the head is done",
        )

    # -- criterion 4: the epoch is comparable between ticks -------------------------------------

    def test_the_epoch_one_tick_reads_is_the_epoch_the_next_tick_compares(self) -> None:
        run = self.live_run()
        self.begin_turn(run)
        self.end_turn(run)
        address = self.runtime._address(run)
        journal_seq = int(self._admitted_status(address.socket_path)["journal_seq"])

        reading = self.next_tick().activity_epoch(run)
        stopping = self.next_tick()
        receipt = stopping.stop_if_quiescent(
            run,
            StopInitiator(actor="dispatcher", reason="rotation"),
            expected_activity_epoch=reading,
            head_process_alive=True,
        )

        self.assertEqual(reading, journal_seq, "the epoch of a head here is its journal sequence")
        self.assertEqual(receipt.status, HEAD_OK, receipt.reason)
        self.assertEqual(receipt.run.lifecycle, EXITED)

    def test_a_head_that_has_merely_printed_hands_out_no_epoch_of_this_process_own(self) -> None:
        """The drift a process-local increment puts into a number two ticks have to compare.

        A head that prints outside a turn writes nothing to its journal — output is a record only
        while a turn is open — so the sequence does not move and neither may the epoch. An
        observation that added one of its own for having looked would hand the caller a number the
        next tick cannot reach: it reads the journal, and the journal is at the number before it.
        """
        run = self.live_run()
        self._await(lambda: b"SIZE" in self.output_of(run), message="the head never printed")
        address = self.runtime._address(run)
        sequence = int(self._admitted_status(address.socket_path)["journal_seq"])

        looking = self.runtime.observe(run)
        next_tick = self.next_tick().activity_epoch(run)

        self.assertEqual(looking.status, HEAD_OK, looking.reason)
        self.assertEqual(
            looking.epoch, sequence, "the epoch a receipt carries is the head's journal sequence"
        )
        self.assertEqual(
            next_tick,
            looking.epoch,
            "the tick that has to compare this number reads the head, and reads it unchanged",
        )
        self.assertGreater(self.runtime.activity.ticks, 0, "`ticks` is still the diagnostic it always was")

    def test_a_turn_opened_after_a_judgement_was_formed_survives_that_judgement_s_stop(self) -> None:
        """The snapshot a conditional stop compares is this critical section's, never an older one.

        The dispatcher forms its judgement — reads the epoch — and stops on it later, so the two
        readings are separated by real time in which another tick can hand the head a turn. A
        runtime that answered the stop out of the snapshot its own earlier `activity_epoch` took
        would compare a number the head had already moved past, find no lease because it had none
        cached, and end a turn that was running.
        """
        run = self.live_run()
        judging = self.next_tick()
        judged = judging.activity_epoch(run)

        working = self.next_tick()
        opened = working.deliver(run, NudgePointer.line(f"busy {TURN_SECONDS}"), subject="nudge")
        self.assertEqual(opened.status, HEAD_OK, opened.reason)
        head = self.head_pid_of(run)

        receipt = judging.stop_if_quiescent(
            run,
            StopInitiator(actor="dispatcher", reason="rotation"),
            expected_activity_epoch=judged,
            head_process_alive=True,
        )

        self.assertNotEqual(receipt.status, HEAD_OK, "a running turn was ended by a stale epoch")
        self.assertIn(receipt.reason, (STOP_ACTIVITY_SINCE, STOP_TURN_IN_FLIGHT), receipt.reason)
        self.assertTrue(_alive(head), "the head another tick was working was killed")
        self._await(lambda: b"DONE" in self.output_of(run), message="the turn never finished on its own")

    def test_an_epoch_from_a_tick_the_head_has_worked_since_refuses_the_stop(self) -> None:
        run = self.live_run()
        self.end_turn(run)
        stale = self.next_tick().activity_epoch(run)

        working = self.next_tick()
        self.assertEqual(working.deliver(run, NudgePointer.line("hello")).status, HEAD_OK)
        stopping = self.next_tick()
        receipt = stopping.stop_if_quiescent(
            run,
            StopInitiator(actor="dispatcher", reason="rotation"),
            expected_activity_epoch=stale,
            head_process_alive=True,
        )

        self.assertEqual(receipt.status, HEAD_ALIVE)
        self.assertEqual(receipt.reason, STOP_ACTIVITY_SINCE)
        self.assertGreater(receipt.epoch, stale, "the head's own sequence moved and says so")
        self.assertTrue(_alive(self.head_pid_of(run)), "a head that had worked since was stopped")

    # -- criterion 5: unknown is not freedom, and it is not a lease either ----------------------

    def test_a_head_nothing_can_state_the_state_of_is_refused_a_new_turn(self) -> None:
        """Fail-closed: debris that answers nothing does not get to admit work by default."""
        run = self._debris_run("debris-nothing-can-read", b"this is not a journal record\n")
        delivered = self.payloads_delivered()

        runtime = self.next_tick()
        refused = runtime.deliver(run, NudgePointer.line("work"), subject="worker-nudge")

        self.assertEqual(refused.status, HEAD_DRAINING, refused.reason)
        self.assertEqual(refused.reason, DELIVER_STATE_UNKNOWN)
        self.assertEqual(self.payloads_delivered(), delivered)
        self.assertIsNone(
            runtime.activity.lease(run.run_id),
            "an unknown must not invent a lease: nothing could ever release it",
        )

    def test_a_head_that_died_without_ever_being_drained_is_rotatable_too(self) -> None:
        """Nobody drained this one, so nothing but its own death can say it takes no more work.

        The other test of this pair kills a head that had been drained first, and a drain closes
        admission by itself — which hides the question. Here the head is killed outright: its
        supervisor reaps it and says `alive` is false, and that has to be enough for the runtime
        to answer that this head is done. Closing admission over a death is not a fence, and the
        proof of that is the bring-up underneath, which is decided by the launch identity alone.
        """
        run = self.live_run()
        head = self.head_pid_of(run)
        _kill(head, group=True)
        self._await(lambda: not _alive(head), message="the head survived")
        self._await(
            lambda: head_process_status(run.pid_file)["state"] == "dead",
            message="the launch identity never said the head had ended",
        )
        self.assertNotIn(DRAIN_REQUESTED, self.events(run).kinds, "this head was never drained")

        restarted = self.next_tick()
        receipt = restarted.observe(run)

        self.assertEqual(receipt.status, HEAD_GONE, receipt.reason)
        self.assertIsNone(receipt.lease, "a dead head runs no turn")
        self.assertTrue(
            receipt.rotation_ready,
            "an undrained dead head is still a head that takes no more work and holds no turn",
        )
        brought_up = restarted.start(
            CODEX,
            str(self.workspace),
            self.task,
            command=CHILD_COMMAND,
            title="ummanu-1479 worker",
            run_id=run.run_id,
            role="worker",
            quiet_seconds=0.4,
        )
        self.assertEqual(brought_up.status, HEAD_OK, brought_up.reason)

    def test_a_head_that_is_positively_dead_is_rotatable_and_its_run_is_not_fenced(self) -> None:
        """The other direction of the same principle, and the one a fabricated lease would break.

        The supervisor is killed mid-turn, so the last thing its journal says about the turn is
        that it started: there is no `turn.finished` and no `run.exited` to close it. What closes
        it is the head's own launch identity, which says the process is dead — the same order
        `stop_if_quiescent` uses, where liveness outranks anything a terminal has to say.
        """
        run = self.live_run()
        self.begin_turn(run)
        self.runtime.request_drain(run, StopInitiator(actor="dispatcher", reason="rotation"))
        supervisor = self._supervisor_pid(self.root / run.run_id)
        head = self.head_pid_of(run)
        _kill(supervisor, signal.SIGKILL)
        self._await(lambda: not _alive(supervisor), message="the supervisor survived")
        _kill(head, signal.SIGKILL, group=True)
        self._await(lambda: not _alive(head), message="the head survived")
        kinds = self.events(run).kinds
        self.assertIn(TURN_STARTED, kinds)
        self.assertIn(DRAIN_REQUESTED, kinds)
        self.assertNotIn(TURN_FINISHED, kinds, "the fixture needs a turn nobody closed")
        self.assertNotIn(RUN_EXITED, kinds, "the fixture needs a head nobody reaped")

        restarted = self.next_tick()
        receipt = restarted.observe(run)

        self.assertEqual(receipt.status, HEAD_GONE, receipt.reason)
        self.assertIsNone(restarted.activity.lease(run.run_id), "a dead head runs no turn")
        self.assertTrue(receipt.rotation_ready, "a drained, dead head is exactly what rotates")
        brought_up = restarted.start(
            CODEX,
            str(self.workspace),
            self.task,
            command=CHILD_COMMAND,
            title="ummanu-1479 worker",
            run_id=run.run_id,
            role="worker",
            quiet_seconds=0.4,
        )
        self.assertEqual(brought_up.status, HEAD_OK, brought_up.reason)

    # -- criterion 6: the cost of the fallback is bounded ---------------------------------------

    def _journal_run(self, run_id: str, write) -> tuple[HeadRun, Path]:
        """A run directory with a journal and no socket, and the journal `write` filled in."""
        run_dir = self.root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / protocol.JOURNAL_NAME
        with JournalWriter(path, run_id) as journal:
            write(journal, path)
        return (
            HeadRun(
                run_id=run_id,
                spec=CODEX,
                workspace=str(self.workspace),
                task_ref=self.task,
                role="worker",
            ),
            path,
        )

    def _record(self, run_id: str, seq: int, kind: str, **fields) -> bytes:
        """One journal line, written by hand so that a test can cut it in half."""
        record = {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "seq": seq,
            "run_id": run_id,
            "kind": kind,
            "at": 1.0,
        }
        record.update(fields)
        return json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def test_a_live_supervisor_wins_over_a_stale_dead_launch_identity(self) -> None:
        """BUG-11: the supervisor that answered is the current incarnation's witness."""
        run = self.live_run()
        runtime = self.next_tick()
        with mock.patch.object(runtime, "_identity", return_value={"state": "dead"}):
            runtime._rehydrate(run)

        self.assertTrue(
            runtime.activity.admits(run.run_id),
            "a stale dead launch identity overruled the live supervisor",
        )
        self.assertNotEqual(runtime._admission_notes.get(run.run_id), DELIVER_HEAD_ENDED)

    def test_an_old_exit_does_not_confirm_the_new_incarnation_stopped(self) -> None:
        """BUG-16: stop acknowledgement is floored by the latest run.started."""

        def write(journal, _path) -> None:
            journal.append(RUN_STARTED, head_pid=1)
            journal.append(RUN_EXITED, exit_code=0)
            journal.append(RUN_STARTED, head_pid=2)

        run, path = self._journal_run("stop-ack-reused-run", write)
        address = self.next_tick()._address(run)
        assert address is not None
        self.assertFalse(_has_exited(address), "an older exit confirmed the current incarnation")

        with JournalWriter(path, run.run_id) as journal:
            journal.append(RUN_EXITED, exit_code=0)
        self.assertTrue(_has_exited(address), "the current incarnation's exit was not accepted")

    def test_a_reused_run_directory_is_answered_by_the_incarnation_that_exists(self) -> None:
        """A whole window replayed in sequence order, and `run.started` resetting what precedes it.

        The journal of a supervised role is reused across incarnations, so a drain and an exit
        from an incarnation that is over sit in the same file as the turn the current one is
        running. The replay is what tells them apart: everything before the last `run.started` is
        about a head that no longer exists.
        """

        def write(journal, path) -> None:
            journal.append(RUN_STARTED, head_pid=1)
            journal.append(DRAIN_REQUESTED, initiator="an incarnation that is over")
            journal.append(RUN_EXITED, code=0)
            journal.append(RUN_STARTED, head_pid=2)
            journal.append(TURN_STARTED, turn=1, subject="worker-nudge")

        run, path = self._journal_run("a-reused-run-directory", write)

        runtime = self.next_tick()
        runtime._rehydrate(run)

        self.assertLess(path.stat().st_size, JOURNAL_TAIL_BYTES, "this window is the whole file")
        self.assertFalse(read_tail(path).partial_head)
        self.assertTrue(
            runtime.activity.busy(run.run_id),
            "the turn at the end of the journal is the one this head is running",
        )
        self.assertTrue(
            runtime.activity.admits(run.run_id),
            "a drain from an incarnation that has exited is not this head's drain",
        )
        self.assertEqual(
            runtime.activity.epoch(run.run_id),
            read_events(path).events[-1]["seq"],
            "the epoch is the journal's own sequence, which the read does not renumber",
        )

    def test_the_journal_a_dead_supervisor_left_is_read_as_a_bounded_tail(self) -> None:
        """The window is the end of the file, and a window that begins mid-history admits nothing.

        The journal of a supervised role is reused across incarnations and grows, so the read that
        answers "what is this head doing" is bounded at `JOURNAL_TAIL_BYTES`. The file here is
        larger than that bound, so the beginning of its history — where a drain or an exit would
        be — is outside the window. The sequence the window ends on is still the head's own and is
        still what the epoch is raised to; the *shape* is not claimed, because "I did not see a
        drain" is not "there was none", and a reader that spells those the same way admits work to
        a head somebody took out of service.
        """

        def write(journal, path) -> None:
            journal.append(RUN_STARTED, head_pid=1)
            while path.stat().st_size <= JOURNAL_TAIL_BYTES:
                journal.append(PROVIDER_PROGRESSED, turn=1, output_bytes=64)
            journal.append(TURN_STARTED, turn=1, subject="worker-nudge")

        run, path = self._journal_run("a-journal-longer-than-the-window", write)

        runtime = self.next_tick()
        runtime._rehydrate(run)

        self.assertTrue(read_tail(path).partial_head, "the read was not bounded at all")
        self.assertGreater(path.stat().st_size, JOURNAL_TAIL_BYTES)
        self.assertFalse(
            runtime.activity.admits(run.run_id),
            "a window that cannot see the beginning of this head's history cannot admit work",
        )
        self.assertIsNone(
            runtime.activity.lease(run.run_id),
            "and it invents no lease either: nothing could ever release one",
        )
        self.assertTrue(
            runtime.activity.rotatable(run.run_id),
            "closing admission over an unknown is not a fence; a lease would have been",
        )
        self.assertEqual(
            runtime.activity.epoch(run.run_id),
            read_events(path).events[-1]["seq"],
            "the epoch is the journal's own sequence, which the bounded read does not renumber",
        )

    def test_a_drain_that_scrolled_out_of_the_window_is_not_a_head_that_admits_work(self) -> None:
        """The edge the bound buys, paid for at admission rather than at the head's terminal.

        The drain really is in this journal — `read_events` finds it — and it really is outside
        the window `read_tail` is allowed to read. A replay of what is left says "no drain here",
        which is exactly the sentence that must not become an open admission.
        """

        def write(journal, path) -> None:
            journal.append(RUN_STARTED, head_pid=1)
            journal.append(DRAIN_REQUESTED, initiator="the tick that took this head out")
            while path.stat().st_size <= JOURNAL_TAIL_BYTES * 2:
                journal.append(PROVIDER_PROGRESSED, turn=1, output_bytes=64)

        run, path = self._journal_run("a-drain-outside-the-window", write)
        delivered = self.payloads_delivered()

        runtime = self.next_tick()
        refused = runtime.deliver(run, NudgePointer.line("work"), subject="worker-nudge")

        self.assertIn(DRAIN_REQUESTED, read_events(path).kinds, "the drain is in the file")
        self.assertNotIn(DRAIN_REQUESTED, read_tail(path).kinds, "and outside the window")
        self.assertEqual(refused.status, HEAD_DRAINING, refused.reason)
        self.assertEqual(refused.reason, DELIVER_STATE_UNKNOWN)
        self.assertEqual(self.payloads_delivered(), delivered)
        self.assertIsNone(runtime.activity.lease(run.run_id))

    def test_a_drain_record_a_sigkill_cut_in_half_is_not_a_head_that_admits_work(self) -> None:
        """The documented `SIGKILL` case: one partial trailing line, and it is the drain.

        `read_tail` reports it as a truncated tail rather than failing, and everything before it
        is complete — which is precisely the shape a replay reads as "this head was started and
        nothing has happened to it since". What was torn off is the record that says otherwise.
        """
        run_id = "a-supervisor-killed-mid-record"
        started = self._record(run_id, 1, RUN_STARTED, head_pid=1)
        torn = self._record(run_id, 2, DRAIN_REQUESTED, initiator="operator")[:-9]
        run = self._debris_run(run_id, started + b"\n" + torn)
        path = self.root / run_id / protocol.JOURNAL_NAME
        delivered = self.payloads_delivered()

        runtime = self.next_tick()
        refused = runtime.deliver(run, NudgePointer.line("work"), subject="worker-nudge")

        self.assertTrue(read_tail(path).truncated_tail, "the fixture is not a torn journal")
        self.assertEqual(read_tail(path).kinds, (RUN_STARTED,), "the drain is what was lost")
        self.assertEqual(refused.status, HEAD_DRAINING, refused.reason)
        self.assertEqual(refused.reason, DELIVER_STATE_UNKNOWN)
        self.assertEqual(self.payloads_delivered(), delivered)
        self.assertIsNone(runtime.activity.lease(run.run_id))


class LocalPtyDeliveryTests(LocalPtyRuntimeTestCase):
    """Criteria 3, 4 and 7: admission is not arrival, and a stall is never glued over.

    One test per name in the backend's delivery vocabulary, and every one of them produced by a
    real supervisor writing into a real pty rather than by handing the backend a report it made
    up: arrived, a prefix left behind, nothing landed at all, and a fate that could not be
    established because the supervisor stopped answering after it had admitted the payload. There
    is deliberately no test for a delivery that is still landing, because there is no longer an
    outcome for one: the wait is the substrate's own bound, so every delivery this verb returns
    about is one the substrate finished with.
    """

    def _stuck_head(self, **options) -> HeadRun:
        """A head that never reads its terminal, so a payload fills the pty and stops."""
        return self.live_run(
            command=f"{sys.executable} -u {SLOW_READER} --chunk 4096 --pause 600",
            delivery_seconds=options.pop("delivery_seconds", 1.0),
            **options,
        )

    def _rebuild_runtime(self, **options) -> None:
        """The same backend over the same run root, with the knobs a test needs.

        There is deliberately no way to spell "watch this delivery for less time than the substrate
        will spend writing it": how long this runtime watches is derived from the bound the head
        was raised with, and `delivery_grace` can only add to it. What a test can change is how
        often it looks, how long it waits for a socket, and — through `delivery_seconds` on the
        head itself — the one bound both layers follow.
        """
        options.setdefault("delivery_grace", TEST_GRACE_SECONDS)
        self.runtime = LocalPtyHeadRuntime(self.root, head_process_status=head_process_status, **options)

    def _fill_the_terminal(self, run: HeadRun) -> int:
        """Fill this head's pty from outside the runtime, and say how much landed.

        Straight at the socket on purpose: it leaves the pty's buffer full and the head's terminal
        carrying a fragment, without the runtime having made the delivery and so without the
        runtime knowing anything about it. A stalled large write is not by itself proof that no
        smaller write can land next: the pty can expose another small pocket of capacity after the
        large write first met ``EAGAIN``. Top it off until one whole delivery bound records zero;
        that is the real-kernel precondition for the runtime delivery below to land nothing.
        """
        address = self.runtime._address(run)
        with SupervisorClient.connect(address.socket_path, timeout=5.0) as client:
            written = 0
            for _attempt in range(4):
                answer = client.send_input(b"x" * (protocol.INPUT_MAX_BYTES - 1))
                self.assertTrue(answer["ok"], answer)
                final = client.wait_for_delivery(answer["delivery"]["id"], timeout=15.0)
                self.assertEqual(final["state"], protocol.DELIVERY_STALLED, final)
                landed = int(final["written_bytes"])
                written += landed
                if not landed:
                    break
            else:
                self.fail("the pty still accepted bytes after four full delivery bounds")
        self.assertGreater(written, 0, "the pty took nothing, so it was never filled")
        # And then until the head has gone quiet again. The payload above was accepted, so the
        # supervisor opened a turn for it; a runtime asking the head what it is doing while that
        # turn is open is answered "a turn is running" — correctly, and this fixture is not the
        # place to assert about that. What it exists to leave behind is a *full pty*, which the
        # head keeps full because it never reads, and that outlives the turn by every second the
        # head stays stopped.
        self._await(
            lambda: not self._status(address.socket_path).get("turn_open", True),
            message="the turn the fill opened never closed",
        )
        return written

    def _stop_supervisor_once(self, run: HeadRun, ready) -> None:
        """Stop this head's supervisor with `SIGSTOP` the moment `ready()` holds, from a thread.

        A supervisor that has admitted a payload and then cannot answer is a real thing — a loaded
        host, an fsync stall on a journal every record of which is fsynced, a process somebody
        stopped — and this makes it happen on purpose instead of waiting for a bad day. What is
        stopped is the supervisor's own process: the socket, the journal, the pty and the head are
        all still exactly what they were.
        """
        supervisor = self._supervisor_pid(self.root / run.run_id)
        self.assertGreater(supervisor, 0, "the run directory names no supervisor")
        self.addCleanup(_kill, supervisor, signal.SIGCONT)

        def watch() -> None:
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline:
                if ready():
                    _kill(supervisor, signal.SIGSTOP)
                    return
                time.sleep(0.005)

        thread = threading.Thread(target=watch, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 10.0)

    def _delivery_in_flight(self, run: HeadRun, size: int):
        """A predicate that holds once the substrate is carrying a payload of exactly this size."""
        address = self.runtime._address(run)

        def ready() -> bool:
            delivery = self._status(address.socket_path).get("delivery") or {}
            return (
                delivery.get("state") == protocol.DELIVERY_IN_FLIGHT
                and int(delivery.get("size_bytes") or 0) == size
            )

        return ready

    def test_a_delivery_that_arrived_says_so_with_the_numbers_that_prove_it(self) -> None:
        run = self.live_run()

        receipt = self.deliver_line(run, "hello", subject="worker-nudge")

        self.assertEqual(receipt.status, HEAD_OK, receipt.reason)
        self.assertTrue(receipt.arrived)
        self.assertEqual(receipt.delivery_state, protocol.DELIVERY_COMPLETE)
        self.assertEqual(receipt.delivered_bytes, receipt.offered_bytes)
        self.assertEqual(receipt.delivered_bytes, len("hello\n"))
        # The journal says the same thing, with the two counts that make a partial arrival
        # impossible to read as a whole one.
        accepted = self.events(run).of_kind(INPUT_ACCEPTED)[-1]
        self.assertTrue(accepted["complete"])
        self.assertEqual(accepted["bytes"], accepted["offered_bytes"])
        self.assertTrue(receipt.evidence.journalled, "status and the journal both saw it")
        self.assertEqual(receipt.evidence.outcome, DELIVERY_ARRIVED)

    def test_a_delivery_that_stalled_is_not_an_ok_receipt_with_a_delivery_on_it(self) -> None:
        """The wound this card exists not to re-open, one layer above where it was closed."""
        run = self._stuck_head()

        receipt = self.deliver_line(run, "x" * (protocol.INPUT_MAX_BYTES - 1))

        self.assertFalse(receipt.ok, "a stalled delivery reported as a success")
        self.assertFalse(receipt.arrived)
        self.assertEqual(receipt.status, HEAD_ALIVE)
        self.assertEqual(receipt.evidence.outcome, DELIVERY_LEFT_A_PREFIX)
        self.assertEqual(receipt.delivery_state, protocol.DELIVERY_STALLED)
        self.assertGreater(receipt.delivered_bytes, 0, "nothing landed, so nothing stalled")
        self.assertLess(receipt.delivered_bytes, receipt.offered_bytes)
        self.assertIsNone(receipt.delivery, "there is no delivery outcome for a payload that stuck")
        accepted = self.events(run).of_kind(INPUT_ACCEPTED)[-1]
        self.assertFalse(accepted["complete"])
        self.assertEqual(accepted["bytes"], receipt.delivered_bytes)

    def test_the_next_payload_is_never_glued_to_the_prefix_a_stall_left(self) -> None:
        run = self._stuck_head()
        stalled = self.deliver_line(run, "x" * (protocol.INPUT_MAX_BYTES - 1))
        self.assertEqual(stalled.delivery_state, protocol.DELIVERY_STALLED)
        landed = self.payloads_delivered()

        second = self.deliver_line(run, "and now this")

        self.assertEqual(second.status, HEAD_DRAINING, second.reason)
        self.assertIn(DRAIN_AFTER_PARTIAL_DELIVERY, second.reason)
        self.assertEqual(self.payloads_delivered(), landed, "a second payload was written anyway")
        # And it is not only this runtime's bookkeeping: the head's own supervisor refuses too, so
        # nothing else that can reach the socket can glue a payload onto the fragment either.
        address = self.runtime._address(run)
        with SupervisorClient.connect(address.socket_path) as client:
            self.assertTrue(client.status()["draining"])
            refused = client.send_input(b"straight at the socket\n")
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["error"], protocol.ERROR_DRAINING)

    def test_a_delivery_of_which_nothing_landed_leaves_the_head_worth_delivering_to(self) -> None:
        """The other side of the same decision, and the reason it is not "any stall is fatal".

        A payload that reached the terminal in part cannot be taken back, so the head is closed.
        A payload of which the kernel took *nothing* left the terminal exactly as it was: there is
        no fragment for a later payload to be glued to, the turn it would have started never
        started, and closing the head over it would be throwing away a working head.

        The zero is produced rather than asserted about: the head's pty is filled from outside
        this runtime first, so the delivery the runtime then makes finds a terminal with no room
        in it and the kernel takes none of it.
        """
        run = self._stuck_head()
        self._fill_the_terminal(run)

        receipt = self.deliver_line(run, "not one byte of this can land")

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.status, HEAD_ALIVE)
        self.assertEqual(receipt.evidence.outcome, DELIVERY_LANDED_NOTHING)
        self.assertEqual(receipt.delivery_state, protocol.DELIVERY_STALLED)
        self.assertEqual(receipt.delivered_bytes, 0, "something landed, so this is not the case")
        self.assertEqual(receipt.offered_bytes, len("not one byte of this can land\n"))
        self.assertIsNone(receipt.lease, "no turn was started, so none is held")
        self.assertTrue(self.runtime.activity.admits(run.run_id), "the head was closed for nothing")
        # The head's own journal carries the delivery that landed nothing, with both counts.
        accepted = self.events(run).of_kind(INPUT_ACCEPTED)[-1]
        self.assertEqual(accepted["bytes"], 0)
        self.assertEqual(accepted["offered_bytes"], receipt.offered_bytes)
        # And the head is still one this runtime will deliver to: the refusal is not permanent.
        self.assertNotEqual(self.deliver_line(run, "and again").status, HEAD_DRAINING)

    def _reporting_head(self, *, pause: float, delivery_seconds: float) -> HeadRun:
        """A head too slow to be delivered to in time, that says what its terminal handed it.

        Both halves are needed. Reading slowly is what makes a large payload stop part-way and
        leave a fragment on the terminal; going on reading is what leaves room in the pty for a
        second payload to be written behind that fragment and taken in with it, in one read. A
        head that stopped reading altogether would keep its terminal full, and nothing could land
        behind the fragment whether this runtime allowed it or not — which would prove nothing.
        """
        run = self.live_run(
            command=f"{sys.executable} -u {LINE_READER} --chunk 4096 --pause {pause:g}",
            delivery_seconds=delivery_seconds,
        )
        self._await(lambda: b"UP" in self.output_of(run), message="the head never said it was up")
        return run

    def _terminal_records(self, run: HeadRun) -> list[bytes]:
        """What the head said its own terminal handed it: one record per line or fragment."""
        return [
            line
            for line in self.output_of(run).splitlines()
            if line.startswith(b"LINE ") or line.startswith(b"FRAG ")
        ]

    def test_a_delivery_nobody_could_account_for_is_not_reported_as_one_that_never_started(self) -> None:
        """Admitted, then unanswerable: the one case where the fate cannot be established.

        `send_input` said `ok`, so the payload is the supervisor's and may be on the terminal
        already. When that supervisor then stops answering and its journal has nothing to say
        about the delivery either, the honest report is `unknown` — not `delivered 0 of 0`, which
        is what "refused before anything reached the terminal" means on this boundary and which
        would let the next payload be written straight behind a fragment.
        """
        self._rebuild_runtime(connect_timeout=1.0)
        # The supervisor is stopped while this injected bound is still in flight. The report is
        # what the witnesses can say then, not what a runtime-owned timeout ran out on.
        run = self._stuck_head(delivery_seconds=2.0)
        text = "x" * (protocol.INPUT_MAX_BYTES - 1)
        self._stop_supervisor_once(run, self._delivery_in_flight(run, len(text) + 1))

        receipt = self.deliver_line(run, text)

        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.evidence.outcome, DELIVERY_UNESTABLISHED)
        self.assertEqual(receipt.delivery_state, DELIVERY_STATE_UNKNOWN)
        self.assertEqual(receipt.offered_bytes, len(text) + 1, "the payload is reported as offered")
        self.assertEqual(receipt.status, HEAD_ALIVE, "an unanswered socket is not a dead head")
        # Admission is closed rather than left open over bytes that may be sitting on the terminal.
        self.assertFalse(self.runtime.activity.admits(run.run_id))
        landed = self.payloads_delivered()
        second = self.deliver_line(run, "and now this")
        self.assertEqual(second.status, HEAD_DRAINING, second.reason)
        self.assertIn(DRAIN_AFTER_UNESTABLISHED_DELIVERY, second.reason)
        self.assertEqual(self.payloads_delivered(), landed, "a second payload was offered anyway")

    def test_what_the_journal_still_knows_is_read_when_the_supervisor_cannot_be_asked(self) -> None:
        """The other witness. A supervisor that stops answering does not take the facts with it.

        Here the delivery really did end — the journal has its `input.accepted`, with a prefix on
        the terminal — and only the socket is gone. The report is that stall, from the journal,
        with the head closed for the prefix it left; reporting `unknown` here would be throwing
        away a fact that is on this host's disk.
        """
        self._rebuild_runtime(connect_timeout=1.0, delivery_poll=5.0)
        run = self._stuck_head(delivery_seconds=1.0)
        journal = self.root / run.run_id / protocol.JOURNAL_NAME
        self._stop_supervisor_once(run, lambda: bool(read_events(journal).of_kind(INPUT_ACCEPTED)))
        text = "x" * (protocol.INPUT_MAX_BYTES - 1)

        receipt = self.deliver_line(run, text)

        accepted = read_events(journal).of_kind(INPUT_ACCEPTED)[-1]
        self.assertGreater(accepted["bytes"], 0, "nothing landed, so this is a different case")
        self.assertEqual(receipt.evidence.outcome, DELIVERY_LEFT_A_PREFIX)
        self.assertTrue(receipt.evidence.journalled, "the journal was not the witness it is")
        self.assertEqual(receipt.delivery_state, protocol.DELIVERY_STALLED)
        self.assertEqual(receipt.delivered_bytes, accepted["bytes"])
        self.assertEqual(receipt.offered_bytes, len(text) + 1)
        self.assertFalse(self.runtime.activity.admits(run.run_id))
        self.assertIn(DRAIN_AFTER_PARTIAL_DELIVERY, self.deliver_line(run, "glue").reason)

    def test_a_live_head_at_the_connection_bound_is_refused_and_not_closed_for_good(self) -> None:
        """A bound the substrate calls normal must not end a head in this runtime's memory.

        At its connection bound the supervisor accepts the socket, writes a refusal frame and lets
        go — without registering the connection, without reading a byte of any request, and having
        said exactly that in the frame. So the payload below is provably never offered and the
        head's terminal is never touched. A `deliver` that read that frame for its contents without
        believing its `ok` went on writing into a socket whose peer had gone, read the `EPIPE` as
        "admitted, then unanswerable" — which is fatal by design — and closed for the rest of this
        runtime's life a head that was alive, idle and merely popular. That is the collapse this
        sprint exists to remove, arriving the other way round: not alive looking dead, but alive
        being *made* dead by a limit that clears itself.
        """
        run = self.live_run()
        head = self.head_pid_of(run)
        address = self.runtime._address(run)
        held = []
        for _ in range(protocol.CONNECTION_MAX_CLIENTS):
            client = SupervisorClient.connect(address.socket_path)
            held.append(client)
            self.addCleanup(client.close)
            # The supervisor counts the bound, so an answer is what proves this connection is part
            # of it; connecting only proves the kernel took it.
            self.assertTrue(client.status()["ok"], "the supervisor never took this connection")

        receipt = self.deliver_line(run, "a nudge nobody was ever offered")

        self.assertEqual(receipt.status, HEAD_BUSY, receipt.reason)
        self.assertTrue(receipt.deferred, "somebody else letting go makes this worth making again")
        self.assertEqual(receipt.evidence["error"], protocol.ERROR_CONNECTION_LIMIT)
        self.assertEqual(receipt.delivered_bytes, 0)
        self.assertEqual(receipt.offered_bytes, 0, "nothing was offered, so nothing was")
        self.assertIsNone(receipt.lease, "a refused delivery started no turn")
        # Nothing about this head was closed: not its admission here, not this runtime's memory of
        # heads it hands no more work, and not its terminal.
        self.assertTrue(self.runtime.activity.admits(run.run_id), "a live head was closed for good")
        self.assertNotIn(run.run_id, self.runtime._fatal)
        self.assertEqual(self.payloads_delivered(), 0, "the terminal was touched after all")
        self.assertTrue(_alive(head), "the head died of a connection limit")
        held.pop().close()
        self.assertFalse(self._admitted_status(address.socket_path)["draining"], "the substrate was drained")

        def delivered() -> bool:
            return self.deliver_line(run, "and now this").status == HEAD_OK

        self._await(delivered, message="the head never took a payload again")

    def test_a_delivery_at_the_declared_limit_arrives_whole_however_slowly_the_head_reads(
        self,
    ) -> None:
        """Criterion 1, as the head experiences it: the wait is the head's own bound, so it fits.

        A payload at the substrate's declared input limit does not fit in a pty's buffer, so it
        can only move as fast as the head reads it — here, seconds. The head is raised with a
        delivery bound long enough for that, and **that is the only number anybody sets**: this
        runtime derives its own wait from it, so there is no second knob that could run out first
        and no configuration in which a payload the substrate was still happily writing comes back
        as anything but the arrival it became. The receipt is `ok`, the two byte counts are equal
        and they are the whole payload, and the head's own journal says the same.
        """
        bound = 2.0
        text = "x" * (protocol.INPUT_MAX_BYTES - 1)
        run = self.live_run(
            command=f"{sys.executable} -u {SLOW_READER} --chunk 4096 --pause 0.05",
            delivery_seconds=bound,
        )
        self.assertGreater(
            self.runtime.delivery_wait_for(bound),
            bound,
            "the derived wait must outlast the bound it is derived from",
        )

        started = time.monotonic()
        receipt = self.deliver_line(run, text)
        took = time.monotonic() - started

        self.assertEqual(receipt.status, HEAD_OK, receipt.reason)
        self.assertTrue(receipt.arrived, "a payload at the declared limit did not arrive whole")
        self.assertEqual(receipt.delivery_state, protocol.DELIVERY_COMPLETE)
        self.assertEqual(receipt.delivered_bytes, len(text) + 1)
        self.assertEqual(receipt.delivered_bytes, receipt.offered_bytes)
        self.assertGreater(took, 0.1, "the head read this instantly, so nothing was waited out")
        accepted = self.events(run).of_kind(INPUT_ACCEPTED)[-1]
        self.assertTrue(accepted["complete"])
        self.assertEqual(accepted["bytes"], len(text) + 1)

    def test_the_next_payload_after_a_stall_meets_a_refusal_and_not_the_head_s_terminal(
        self,
    ) -> None:
        """The head's own terminal, read record by record, is the witness this test believes.

        Every other assertion about a stall is made about a receipt or a journal. This one is made
        about the bytes the head took off its own file descriptor, because the thing criterion 7
        forbids is invisible everywhere else: a fragment finished by the next payload's newline is
        read by the head as *one sentence*, and no receipt about either payload would be wrong.

        The head reads slowly and never stops, which is what makes both halves reachable — slowly,
        so a payload at the input limit stops part-way and leaves a fragment; never stopping, so
        the pty keeps having room and a second payload written behind that fragment really would
        be glued to it. What prevents it is that the stall is an ending this runtime saw, so the
        head is closed and the next `deliver` meets `HEAD_DRAINING` instead of the terminal.
        """
        run = self._reporting_head(pause=0.5, delivery_seconds=1.5)
        address = self.runtime._address(run)

        first = self.deliver_line(run, "A" * (protocol.INPUT_MAX_BYTES - 1))

        self.assertEqual(first.evidence.outcome, DELIVERY_LEFT_A_PREFIX)
        self.assertEqual(first.delivery_state, protocol.DELIVERY_STALLED)
        self.assertGreater(first.delivered_bytes, 0, "nothing landed, so there is no fragment")
        self.assertLess(first.delivered_bytes, first.offered_bytes)
        # The turn those landed bytes opened closes on its own while the head goes on reading, so
        # nothing about a running turn is what refuses the next payload.
        self.end_turn(run)
        landed = self.payloads_delivered()

        second = self.deliver_line(run, "B" * 40)

        self.assertEqual(second.status, HEAD_DRAINING, second.reason)
        self.assertIn(DRAIN_AFTER_PARTIAL_DELIVERY, second.reason)
        self.assertEqual(self.payloads_delivered(), landed, "a second payload was offered anyway")
        self.assertTrue(self._status(address.socket_path)["draining"], "the substrate still admits")
        # And now the head itself, off its own descriptor. A fragment of the first payload is
        # unavoidable — those bytes cannot be taken back — but nothing of the second may be
        # anywhere in it, and the fragment must still be a fragment: a `LINE` here would be that
        # fragment finished by something written behind it.
        self._await(
            lambda: bool(self._terminal_records(run)),
            timeout=20.0,
            message="the head never said what its terminal handed it",
        )
        records = self._terminal_records(run)
        for record in records:
            kinds = record.split(b"kinds=")[1].split(b" ")[0]
            self.assertEqual(kinds, b"A", f"the head was handed something else: {record!r}")
        self.assertTrue(
            all(record.startswith(b"FRAG ") for record in records),
            f"the first payload's line was completed by something: {records!r}",
        )

    def test_a_payload_over_the_declared_limit_is_refused_and_the_head_is_untouched(self) -> None:
        run = self.live_run()

        receipt = self.deliver_line(run, "x" * (protocol.INPUT_MAX_BYTES + 1))

        self.assertEqual(receipt.status, HEAD_ALIVE)
        self.assertEqual(self.payloads_delivered(), 0, "an oversized payload was written anyway")
        self.assertIsNone(receipt.lease, "a refused delivery started no turn")
        self.assertEqual(self.deliver_line(run, "a payload that fits").status, HEAD_OK)


class LocalPtyDrainTests(LocalPtyRuntimeTestCase):
    """Criterion 5: the drain reaches the process that owns the head, and says so truthfully."""

    def test_a_drain_reaches_the_head_s_own_supervisor_and_is_read_back(self) -> None:
        run = self.live_run()

        receipt = self.runtime.request_drain(run, StopInitiator(actor="operator"))

        self.assertEqual(receipt.status, HEAD_OK, receipt.reason)
        self.assertNotEqual(receipt.status, HEAD_UNSUPPORTED, "this backend owns the process")
        self.assertTrue(receipt.draining)
        self.assertTrue(receipt.head_signalled, "the supervisor was told and confirmed it")
        self.assertTrue(self.events(run).of_kind(DRAIN_REQUESTED), "the head's journal says so")

    def test_a_drain_this_runtime_could_not_deliver_says_that_instead_of_claiming_it(self) -> None:
        """`head_signalled` is read back from the supervisor, never assumed from the request."""
        run = self.orphan_head()
        supervisor = self._supervisor_pid(self.root / run.run_id)
        head = self.head_pid_of(run)
        _kill(supervisor)
        self._await(lambda: not _alive(supervisor), message="the supervisor never died")

        receipt = self.runtime.request_drain(run, StopInitiator(actor="operator"))

        self.assertEqual(receipt.status, HEAD_ALIVE)
        self.assertTrue(receipt.draining, "this runtime's own gate is still real")
        self.assertFalse(receipt.head_signalled)
        self.assertEqual(self.deliver_line(run, "more work").status, HEAD_DRAINING, "the local gate holds")
        _kill(head, group=True)

    def test_a_drain_does_not_interrupt_the_turn_the_head_is_running(self) -> None:
        run = self.live_run()
        self.begin_turn(run)
        head = self.head_pid_of(run)

        self.runtime.request_drain(run, StopInitiator(actor="operator"))

        self.assertTrue(_alive(head), "the drain killed the head it was supposed to wind down")
        self.assertIs(self.runtime.observe(run).busy, True, "the running turn was taken away")


class LocalPtyAttachTests(LocalPtyRuntimeTestCase):
    """Criterion 6: a stream rather than an address, bounded, and safe to walk away from."""

    def test_attach_hands_over_the_head_s_own_stream_addressed_by_its_socket(self) -> None:
        run = self.live_run()

        self._await(lambda: b"SIZE" in self.output_of(run), message="the head never printed")

        receipt = self.runtime.attach(run)

        self.assertEqual(receipt.status, HEAD_OK, receipt.reason)
        self.assertNotEqual(receipt.status, HEAD_UNSUPPORTED)
        self.assertIsInstance(receipt.evidence, AttachedStream)
        self.addCleanup(receipt.evidence.close)
        self.assertEqual(receipt.handle, str(self.root / run.run_id / protocol.SOCKET_NAME))
        self.assertEqual(receipt.evidence.socket_path, receipt.handle)
        self.assertIn(b"SIZE", receipt.evidence.backlog, "the head's output so far came with it")

    def test_an_attached_caller_is_pushed_what_the_head_prints(self) -> None:
        run = self.live_run()
        receipt = self.runtime.attach(run)
        self.addCleanup(receipt.evidence.close)

        self.assertEqual(self.deliver_line(run, "hello").status, HEAD_OK)

        seen = b""
        deadline = time.monotonic() + 10.0
        while b"ECHO hello" not in seen and time.monotonic() < deadline:
            frame = receipt.evidence.client.next_event(timeout=1.0)
            if frame is not None:
                seen += bytes(frame.get("bytes_data") or b"")
        self.assertIn(b"ECHO hello", seen)

    def test_detaching_does_not_touch_the_head_and_loses_none_of_its_output(self) -> None:
        run = self.live_run()
        first = self.runtime.attach(run)
        head = self.head_pid_of(run)

        first.evidence.close()

        self.assertTrue(_alive(head), "a caller walking away took the head with it")
        self.assertEqual(self.deliver_line(run, "after the detach").status, HEAD_OK)
        self._await(
            lambda: b"ECHO after the detach" in self.output_of(run),
            message="the head stopped printing once nobody was attached",
        )
        second = self.runtime.attach(run)
        self.addCleanup(second.evidence.close)
        self.assertIn(
            b"ECHO after the detach",
            second.evidence.backlog,
            "output produced while nobody held the stream was lost",
        )

    def test_attach_is_bounded_by_the_substrate_s_own_limit_and_the_refusal_is_deferred(self) -> None:
        run = self.live_run()
        held = []
        for _ in range(protocol.ATTACH_MAX_CLIENTS):
            receipt = self.runtime.attach(run)
            self.assertEqual(receipt.status, HEAD_OK, receipt.reason)
            held.append(receipt.evidence)
            self.addCleanup(receipt.evidence.close)

        refused = self.runtime.attach(run)

        self.assertEqual(refused.status, HEAD_BUSY)
        self.assertTrue(refused.deferred, "somebody else's stream ending makes this worth retrying")
        self.assertEqual(refused.evidence["error"], protocol.ERROR_ATTACH_LIMIT)
        held[0].close()

        def rejoined() -> bool:
            receipt = self.runtime.attach(run)
            if not receipt.ok:
                return False
            self.addCleanup(receipt.evidence.close)
            return True

        self._await(rejoined, message="the freed slot was never reusable")

    def test_a_live_head_at_the_connection_bound_is_not_reported_as_one_that_ended(self) -> None:
        """The collapse this sprint exists to remove, in the one verb that could still make it.

        Both of the substrate's bounds are refusals worth making again the moment somebody lets
        go. Reporting either of them as `HEAD_GONE` tells a caller that a head which is running,
        answering and merely popular has ended — and what a caller does with a head that has ended
        is open another one beside it.
        """
        run = self.live_run()
        head = self.head_pid_of(run)
        held = []
        for _ in range(protocol.CONNECTION_MAX_CLIENTS):
            client = SupervisorClient.connect(self.root / run.run_id / protocol.SOCKET_NAME)
            held.append(client)
            self.addCleanup(client.close)
            # An answer is what proves the supervisor has *registered* this connection. Connecting
            # only proves the kernel took it, and the bound is counted by the supervisor: without
            # this, a loaded host reaches the verb below before the last connection is counted and
            # the test measures the race instead of the bound.
            self.assertTrue(client.status()["ok"], "the supervisor never took this connection")

        refused = self.runtime.attach(run)

        self.assertEqual(refused.evidence["error"], protocol.ERROR_CONNECTION_LIMIT)
        self.assertEqual(refused.status, HEAD_BUSY, "a live head at a bound is not a head that ended")
        self.assertTrue(refused.deferred, "somebody else letting go makes this worth retrying")
        self.assertTrue(_alive(head))
        held.pop().close()

        def rejoined() -> bool:
            receipt = self.runtime.attach(run)
            if not receipt.ok:
                return False
            receipt.evidence.close()
            return True

        self._await(rejoined, message="the freed connection was never reusable")


class LocalPtyObserveTests(LocalPtyRuntimeTestCase):
    """Criterion 7: built on the journal and the substrate's state, never on a guess."""

    def test_a_head_with_no_run_directory_is_not_a_head_that_is_not_busy(self) -> None:
        receipt = self.runtime.observe(self.adrift_run())

        self.assertEqual(receipt.status, HEAD_UNSUPPORTED)
        self.assertEqual(receipt.reason, OBSERVE_NO_RUN_DIRECTORY)
        self.assertIsNone(receipt.busy)

    def test_a_supervisor_that_died_is_not_reported_as_a_head_that_ended(self) -> None:
        """The collapse that opens a replacement beside a head that is still running."""
        run = self.orphan_head()
        supervisor = self._supervisor_pid(self.root / run.run_id)
        head = self.head_pid_of(run)
        _kill(supervisor)
        self._await(lambda: not _alive(supervisor), message="the supervisor never died")
        self.assertTrue(_alive(head), "the head did not outlive its supervisor, so this asks nothing")

        receipt = self.runtime.observe(run)

        self.assertEqual(receipt.status, HEAD_ALIVE)
        self.assertEqual(receipt.reason, OBSERVE_SUPERVISOR_UNREACHABLE)
        self.assertIsNone(receipt.busy, "not knowing is not knowing the head is idle")
        self.assertTrue(_alive(head))
        _kill(head, group=True)

    def test_a_head_whose_process_ended_is_gone_and_its_turn_with_it(self) -> None:
        run = self.live_run()
        self.begin_turn(run)
        self.assertTrue(self.runtime.activity.busy(run.run_id))
        with SupervisorClient.connect(self.root / run.run_id / protocol.SOCKET_NAME) as client:
            client.stop("test")

        self._await(
            lambda: self.runtime.observe(run).status in (HEAD_GONE,),
            message="the head's exit was never observed",
        )
        receipt = self.runtime.observe(run)

        self.assertEqual(receipt.status, HEAD_GONE)
        self.assertEqual(receipt.reason, OBSERVE_HEAD_EXITED)
        self.assertIsNone(receipt.lease, "a dead head runs no turn")
        self.assertIs(receipt.busy, False)

    def test_the_epoch_moves_on_new_output_and_not_on_the_fact_of_looking(self) -> None:
        run = self.live_run()
        self.deliver_line(run, "hello")
        self._await(lambda: b"ECHO hello" in self.output_of(run), message="the head never echoed")
        self.end_turn(run)
        printed = self.runtime.observe(run).epoch

        again = self.runtime.observe(run).epoch

        self.assertEqual(again, printed, "looking twice at a quiet head is not activity")
        self.assertEqual(self.deliver_line(run, "again").status, HEAD_OK)
        self._await(lambda: b"ECHO again" in self.output_of(run), message="the head never echoed")
        self.assertGreater(self.runtime.observe(run).epoch, printed)


class LocalPtyStopTests(LocalPtyRuntimeTestCase):
    """Criterion 8: the stop is confirmed against the launch identity, and nothing else."""

    def test_stop_ends_the_process_and_confirms_it_against_the_launch_identity(self) -> None:
        run = self.live_run()
        head = self.head_pid_of(run)

        receipt = self.runtime.stop(run, StopInitiator(actor="operator", reason="rotation"))

        self.assertEqual(receipt.status, HEAD_OK, receipt.reason)
        self.assertEqual(receipt.run.lifecycle, EXITED)
        self.assertEqual(receipt.run.stopped_by.actor, "operator")
        self.assertFalse(_alive(head))
        self.assertEqual(head_process_status(run.pid_file)["state"], "dead")

    def test_a_head_mid_turn_is_stopped_unconditionally_when_a_stop_says_so(self) -> None:
        run = self.live_run()
        self.begin_turn(run)

        receipt = self.runtime.stop(run, StopInitiator(actor="reviewer-freeze"))

        self.assertEqual(receipt.status, HEAD_OK, receipt.reason)
        self.assertFalse(_alive(self.head_pid_of(run)))

    def test_a_conditional_stop_refuses_a_head_that_is_mid_turn(self) -> None:
        run = self.live_run()
        self.begin_turn(run)
        head = self.head_pid_of(run)

        receipt = self.runtime.stop_if_quiescent(
            run,
            StopInitiator(actor="dispatcher", reason="rotation"),
            expected_activity_epoch=self.runtime.activity.epoch(run.run_id),
            head_process_alive=True,
        )

        self.assertEqual(receipt.status, HEAD_BUSY)
        self.assertEqual(receipt.reason, STOP_TURN_IN_FLIGHT)
        self.assertTrue(_alive(head), "an active turn was interrupted")
        self.assertTrue(self.runtime.activity.admits(run.run_id), "a refusal left it out of service")

    def test_a_conditional_stop_refuses_a_head_that_has_done_something_since(self) -> None:
        run = self.live_run()
        stale = self.runtime.activity.epoch(run.run_id)
        self.deliver_line(run, "hello")

        receipt = self.runtime.stop_if_quiescent(
            run,
            StopInitiator(actor="dispatcher"),
            expected_activity_epoch=stale,
            head_process_alive=True,
        )

        self.assertEqual(receipt.status, HEAD_ALIVE)
        self.assertEqual(receipt.reason, STOP_ACTIVITY_SINCE)
        self.assertTrue(_alive(self.head_pid_of(run)))

    def test_a_dead_head_is_rotated_without_the_terminal_being_asked_about_its_turn(self) -> None:
        """Liveness outranks terminal readiness, exactly as it does on the legacy backend.

        The head is killed mid-turn, which takes its supervisor's socket with it, so the probe
        below would answer "I could not ask" — a refusal — for a head that is provably gone. The
        caller's own launch-identity evidence is what decides instead, and the rotation happens.

        The epoch is read through `activity_epoch` after the head has died rather than out of this
        object's memory before it, because dying is something the head *did*: its supervisor wrote
        it down, and secretary-1479 makes the epoch that sequence. A judgement formed before the
        death and compared after it has expired by definition, and the refusal that follows is the
        conditional stop working, not failing. What this test is about is the step after that one —
        that liveness, and not the terminal, decides the turn.
        """
        run = self.live_run()
        self.begin_turn(run)
        head = self.head_pid_of(run)
        _kill(head, group=True)
        self._await(lambda: not _alive(head), message="the head never died")

        receipt = self.runtime.stop_if_quiescent(
            run,
            StopInitiator(actor="dispatcher", reason="rotation"),
            expected_activity_epoch=self.runtime.activity_epoch(run),
            head_process_alive=False,
        )

        self.assertEqual(receipt.status, HEAD_OK, receipt.reason)
        self.assertEqual(receipt.run.lifecycle, EXITED)
        self.assertFalse(self.runtime.activity.busy(run.run_id))


class TheWaitIsDerivedFromTheSubstrateTests(LocalPtyRuntimeTestCase):
    """Criterion 1 and 2: the wait is the substrate's own number, and there is no second place.

    secretary-1465 gave this runtime a `delivery_timeout` beside the substrate's `delivery_seconds`
    and nothing tied the two together. Every red round of that card grew out of what that permitted
    — a runtime allowed to stop watching a delivery the substrate was still writing — so what is
    pinned here is that the permission no longer exists, in the two ways it can be pinned: the
    shape of the constructor and the arithmetic of the wait, and then the absence of everything
    that was built to carry an unwatched delivery.
    """

    def test_this_runtime_takes_no_wait_of_its_own(self) -> None:
        parameters = inspect.signature(LocalPtyHeadRuntime.__init__).parameters

        self.assertNotIn("delivery_timeout", parameters, "the independent second knob is back")
        with self.assertRaises(TypeError):
            LocalPtyHeadRuntime(self.root, head_process_status=head_process_status, delivery_timeout=0.5)

    def test_the_wait_is_the_substrate_s_bound_and_can_only_be_longer_than_it(self) -> None:
        """Whatever the head was raised with, the wait over it is strictly longer. No exceptions."""
        for bound in (0.5, 1.0, protocol.INPUT_DELIVERY_SECONDS, 60.0, 3600.0):
            with self.subTest(bound=bound):
                self.assertGreater(self.runtime.delivery_wait_for(bound), bound)
        raised = LocalPtyHeadRuntime(self.root, head_process_status=head_process_status, delivery_grace=0.0)
        self.assertGreaterEqual(raised.delivery_wait_for(90.0), 90.0, "a raised bound is followed")
        self.assertGreater(
            raised.delivery_wait_for(90.0),
            raised.delivery_wait_for(30.0),
            "one number is raised and the other follows it",
        )

    def test_the_grace_cannot_be_spelled_as_a_shorter_wait(self) -> None:
        with self.assertRaises(LocalPtyRuntimeError):
            LocalPtyHeadRuntime(self.root, head_process_status=head_process_status, delivery_grace=-1.0)

    def test_the_bound_is_read_off_the_delivery_the_substrate_admitted(self) -> None:
        """Per delivery, from the answer that admitted it — not from anything remembered here."""
        self.assertEqual(_declared_bound({"timeout_seconds": 7.5}), 7.5)
        self.assertEqual(_declared_bound({}), UNDECLARED_DELIVERY_BOUND)
        self.assertEqual(_declared_bound({"timeout_seconds": "not a number"}), UNDECLARED_DELIVERY_BOUND)
        # And what the substrate really puts there is the head's own bound, so a head raised with
        # one number is a head every delivery to it is waited out at.
        run = self.live_run(delivery_seconds=17.0)
        address = self.runtime._address(run)
        with SupervisorClient.connect(address.socket_path, timeout=5.0) as client:
            answer = client.send_input(b"hello\n")
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(_declared_bound(answer["delivery"]), 17.0)

    def test_nothing_asks_a_second_time_what_became_of_a_delivery(self) -> None:
        """One point asks the witnesses, and it is the one that carries the delivery: `_follow`.

        A source guard rather than a behavioural one on purpose. What criterion 2 forbids is not a
        wrong answer — each of these was locally right when it was added — but a second place where
        the question can be asked at all, because that is what let two places answer it differently.
        A name that comes back is a decision somebody has to make again, and this fails when it is
        made silently.
        """
        source = (REPO / "src" / "ummanu" / "runtime" / "local_pty_head.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        defined = {
            node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assigned = {
            target.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        }

        self.assertNotIn("_settle", defined)
        self.assertNotIn("_resolve", defined)
        self.assertNotIn("_remember", defined)
        self.assertNotIn("DELIVERY_STILL_GOING", assigned, "the fifth outcome is back")
        self.assertNotIn("_unfinished", source, "the register of deliveries nobody accounted for is back")
        self.assertNotIn("still_going", source)
        # And the one place that does ask is still there, asking once.
        self.assertIn("_follow", defined)


class TheSubstrateSBoundsNeverEndAHeadTests(LocalPtyRuntimeTestCase):
    """Criterion 6: a bound the substrate clears by itself is a refusal, never a head's ending.

    The connection bound is reachable from every verb, because every verb dials the socket. What
    each of them answers differs — a delivery and an attachment are refused outright and say
    `HEAD_BUSY`, an observation is a question this backend cannot answer while it is refused, and a
    drain and a stop are verbs whose local half happened — but what none of them may do is the
    same in all five: close this head, remember it as fatal, drain it, or report it as ended.
    """

    def _crowd_the_socket(self, run: HeadRun) -> list[SupervisorClient]:
        """Hold every connection the supervisor will hold, so the next caller is refused."""
        address = self.runtime._address(run)
        held = []
        for _ in range(protocol.CONNECTION_MAX_CLIENTS):
            client = SupervisorClient.connect(address.socket_path)
            held.append(client)
            self.addCleanup(client.close)
            # An answer is what proves the supervisor has *registered* this connection. Connecting
            # only proves the kernel took it, and the bound is counted by the supervisor.
            self.assertTrue(client.status()["ok"], "the supervisor never took this connection")
        return held

    def test_every_verb_that_meets_the_connection_bound_leaves_the_head_exactly_as_it_was(
        self,
    ) -> None:
        run = self.live_run()
        head = self.head_pid_of(run)
        address = self.runtime._address(run)
        held = self._crowd_the_socket(run)

        delivered = self.deliver_line(run, "a nudge nobody was ever offered")
        attached = self.runtime.attach(run)
        observed = self.runtime.observe(run)
        drained = self.runtime.request_drain(run, StopInitiator(actor="operator"))
        stopped = self.runtime.stop_if_quiescent(
            run,
            StopInitiator(actor="dispatcher", reason="rotation"),
            expected_activity_epoch=self.runtime.activity.epoch(run.run_id),
            head_process_alive=True,
        )

        # The two verbs the bound refuses outright say the one thing that is true of it: come back.
        self.assertEqual(delivered.status, HEAD_BUSY, delivered.reason)
        self.assertTrue(delivered.deferred)
        self.assertEqual(delivered.evidence["error"], protocol.ERROR_CONNECTION_LIMIT)
        self.assertEqual(attached.status, HEAD_BUSY, attached.reason)
        self.assertTrue(attached.deferred)
        self.assertEqual(attached.evidence["error"], protocol.ERROR_CONNECTION_LIMIT)
        # An observation cannot be made at all while the question is being refused, and says that
        # rather than inventing an answer: `busy` is `None`, exactly as it is on the legacy backend
        # when a pane's readiness cannot be read.
        self.assertEqual(observed.status, HEAD_UNSUPPORTED, observed.reason)
        self.assertEqual(observed.reason, OBSERVE_STATUS_UNREADABLE)
        self.assertIsNone(observed.busy, "a head nobody could ask about is not a head that is idle")
        # The drain's local half really happened, so it is not `HEAD_BUSY` — that would say nothing
        # of this attempt was left behind — and it does not claim the half that did not.
        self.assertTrue(drained.draining)
        self.assertFalse(drained.head_signalled, "the supervisor was never told, and it says so")
        self.assertEqual(drained.status, HEAD_ALIVE, drained.reason)
        # And a stop that could not be taken by the socket is a head still standing, not one ended.
        self.assertEqual(stopped.status, HEAD_ALIVE, stopped.reason)

        # None of the five ended this head, in this runtime's memory or on the host.
        self.assertNotIn(run.run_id, self.runtime._fatal)
        self.assertTrue(_alive(head), "a head died of a limit that clears itself")
        self.assertEqual(self.payloads_delivered(), 0, "the terminal was touched after all")
        self.assertEqual(self.events(run).of_kind(DRAIN_REQUESTED), (), "the substrate was drained")
        for client in held:
            client.close()
        # The drain this runtime did keep is its own gate, and a fresh head is unaffected by any
        # of it: the bound is gone the moment the callers let go.
        self.assertFalse(self._admitted_status(address.socket_path)["draining"])
        second = self.live_run()
        self._await(
            lambda: self.deliver_line(second, "and now this").status == HEAD_OK,
            message="the head never took a payload again once the bound cleared",
        )

    def test_the_attach_bound_is_refused_and_costs_the_head_nothing_either(self) -> None:
        run = self.live_run()
        head = self.head_pid_of(run)
        for _ in range(protocol.ATTACH_MAX_CLIENTS):
            receipt = self.runtime.attach(run)
            self.assertEqual(receipt.status, HEAD_OK, receipt.reason)
            self.addCleanup(receipt.evidence.close)

        refused = self.runtime.attach(run)
        delivered = self.deliver_line(run, "a payload the attach bound has nothing to do with")

        self.assertEqual(refused.status, HEAD_BUSY, refused.reason)
        self.assertEqual(refused.evidence["error"], protocol.ERROR_ATTACH_LIMIT)
        self.assertNotIn(run.run_id, self.runtime._fatal)
        self.assertTrue(self.runtime.activity.admits(run.run_id))
        self.assertEqual(delivered.status, HEAD_OK, delivered.reason)
        self.assertTrue(_alive(head))

    def test_the_bound_is_still_a_bound_when_the_head_s_journal_is_longer_than_the_window(
        self,
    ) -> None:
        """The bound met by a *rehydrating* runtime, over a journal it cannot read the head out of.

        The three classes of evidence secretary-1479 must keep apart, in the one shape that
        collapses two of them. A supervised role reuses its run directory, so the journal is
        append-only across incarnations and grows past `JOURNAL_TAIL_BYTES`; a window that starts
        mid-history is a `partial_head`, and a `partial_head` is the unknown that closes admission.
        Here the supervisor is alive, undrained and answering — it simply answered
        `connection_limit`, which is a refusal of *this caller* that clears the moment somebody
        lets go. A runtime that reads that typed, self-clearing refusal as "nobody could say
        anything" falls through to that tail, closes admission on the unknown, and answers
        `HEAD_DRAINING` for a head nothing has drained — and goes on answering it after the bound
        has cleared, because admission is never re-opened by rehydration.

        The short-journal bound tests above cannot see this: their window is the whole file, so the
        fallback they land on states a shape and the collapse is invisible.
        """
        run_id = "a-popular-head-with-a-reused-journal"
        run_dir = self.root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / protocol.JOURNAL_NAME
        with JournalWriter(path, run_id) as journal:
            journal.append(RUN_STARTED, head_pid=1)
            while path.stat().st_size <= JOURNAL_TAIL_BYTES:
                journal.append(PROVIDER_PROGRESSED, turn=1, output_bytes=64)
            journal.append(RUN_EXITED, code=0)

        run = self.live_run(run_id=run_id)
        head = self.head_pid_of(run)
        address = self.runtime._address(run)
        self.assertTrue(read_tail(path).partial_head, "the window can still see this head's start")
        self.assertGreater(path.stat().st_size, JOURNAL_TAIL_BYTES)
        held = self._crowd_the_socket(run)

        # A tick that never saw this head, which is what makes it rehydrate before it decides.
        tick = self.next_tick()
        refused = tick.deliver(run, NudgePointer.line("a nudge nobody was ever offered"))

        self.assertEqual(refused.status, HEAD_BUSY, refused.reason)
        self.assertTrue(refused.deferred, "somebody else letting go makes this worth making again")
        self.assertEqual(refused.evidence["error"], protocol.ERROR_CONNECTION_LIMIT)
        self.assertTrue(tick.activity.admits(run.run_id), "a live head was drained by a bound")
        self.assertNotIn(run.run_id, tick._fatal)
        self.assertEqual(self.events(run).of_kind(DRAIN_REQUESTED), (), "the substrate was drained")
        self.assertTrue(_alive(head), "the head died of a limit that clears itself")

        held.pop().close()

        answered = self._admitted_status(address.socket_path)
        self.assertTrue(answered["alive"], "the fixture needs a supervisor that is still there")
        self.assertFalse(answered["draining"], "the substrate was drained after all")
        self._await(
            lambda: tick.deliver(run, NudgePointer.line("and now this")).status == HEAD_OK,
            message="the head never took a payload again once the bound cleared",
        )


class OneStatusFramePerCriticalSectionTests(LocalPtyRuntimeTestCase):
    """Obligation 3 and criterion 6: a verb spends one status request on deciding, not two.

    The rehydration of secretary-1479 is a second reader of the supervisor's `status`, and the
    verbs it serves had readers of their own already: `deliver` read a frame before it offered a
    payload, `stop_if_quiescent` read one to ask whether the adopted turn was still open, and
    `request_drain` read one back to claim `head_signalled`. Asking twice is not only twice the
    cost — it is two moments, and a lease adopted at one moment and tested at another is exactly
    the comparison across time that rehydrating inside the lock exists to remove. So the one frame
    is taken at the top of the section and passed to everything in it.

    What is counted is what a verb spends *deciding*: every `status` up to the point where the
    verb acts on the head. The polling that follows an admitted delivery is not that — it is one
    watch of one delivery, bounded by the substrate's own declared bound, and `_follow` is the
    single place this backend asks what became of a payload.
    """

    def setUp(self) -> None:
        super().setUp()
        real_status = SupervisorClient.status
        real_input = SupervisorClient.send_input
        counter = self

        def status(client: SupervisorClient) -> dict:
            if not counter.offered:
                counter.asked += 1
            if counter.status_failures:
                counter.status_failures -= 1
                raise OSError("one transient status failure")
            return real_status(client)

        def send_input(client: SupervisorClient, data, *, subject: str = "") -> dict:
            counter.offered = True
            return real_input(client, data, subject=subject)

        self.asked = 0
        self.offered = False
        self.status_failures = 0
        self.addCleanup(setattr, SupervisorClient, "send_input", real_input)
        self.addCleanup(setattr, SupervisorClient, "status", real_status)
        SupervisorClient.status = status
        SupervisorClient.send_input = send_input

    def counting(self) -> None:
        """Start counting here: what the fixture asked on its way to this point is not the verb."""
        self.asked = 0
        self.offered = False

    def test_a_delivery_decides_on_one_status_frame(self) -> None:
        run = self.live_run()

        tick = self.next_tick()
        self.counting()
        receipt = tick.deliver(run, NudgePointer.line("work"), subject="worker-nudge")

        self.assertEqual(receipt.status, HEAD_OK, receipt.reason)
        self.assertEqual(self.asked, 1, "the frame that rehydrated is the frame that floored")

    def test_a_delivery_may_retry_one_failed_status_once_and_then_succeeds(self) -> None:
        run = self.live_run()

        tick = self.next_tick()
        self.counting()
        self.status_failures = 1
        receipt = tick.deliver(run, NudgePointer.line("work"), subject="worker-nudge")

        self.assertEqual(receipt.status, HEAD_OK, receipt.reason)
        self.assertEqual(self.asked, 2, "one failed attempt earns one retry, never a third")

    def test_a_delivery_refused_for_a_turn_decides_on_one_status_frame(self) -> None:
        run = self.live_run()
        self.begin_turn(run)

        tick = self.next_tick()
        self.counting()
        receipt = tick.deliver(run, NudgePointer.line("work"), subject="worker-nudge")

        self.assertEqual(receipt.status, HEAD_BUSY, receipt.reason)
        self.assertEqual(self.asked, 1, "the frame that adopted the turn is the frame that read it")

    def test_a_drain_decides_and_reads_back_on_one_status_frame(self) -> None:
        run = self.live_run()

        tick = self.next_tick()
        self.counting()
        receipt = tick.request_drain(run, StopInitiator(actor="operator", reason="rotation"))

        self.assertEqual(receipt.status, HEAD_OK, receipt.reason)
        self.assertTrue(receipt.head_signalled, "the read-back is what this claim is made of")
        self.assertEqual(self.asked, 1, "the read-back is also what the rehydration is made of")

    def test_a_conditional_stop_over_a_turn_it_did_not_grant_decides_on_one_status_frame(
        self,
    ) -> None:
        run = self.live_run()
        self.begin_turn(run)

        epoch = self.next_tick().activity_epoch(run)
        tick = self.next_tick()
        self.counting()
        receipt = tick.stop_if_quiescent(
            run,
            StopInitiator(actor="dispatcher", reason="rotation"),
            expected_activity_epoch=epoch,
            head_process_alive=True,
        )

        self.assertEqual(receipt.status, HEAD_BUSY, receipt.reason)
        self.assertEqual(receipt.reason, STOP_TURN_IN_FLIGHT)
        self.assertEqual(self.asked, 1, "the frame that adopted the lease is the frame that held it")


class OnlyTheResolverWiresThisBackendIn(unittest.TestCase):
    """What is left of `TheBackendIsNotWiredInTests` once a profile may name this backend.

    secretary-1466 built this backend and asserted that nothing selected it: the substrate had one
    consumer, no product module imported the backend, the dispatcher built `OrcaLegacyHeadRuntime`
    and only that, and no registry named `local-pty`. secretary-1467 is the card that makes a
    profile able to name it, so the first three of those are **cancelled on purpose** — the
    dispatcher now imports this backend and builds it when a profile asks for it, which is the
    whole point of that card — and they are replaced here by the properties that survive the
    wiring rather than deleted:

      * the dispatcher still reaches the substrate only through its backend. The PO runner is the
        other authorized consumer: each PO turn now uses the same scoped supervisor;
      * the dispatcher builds its backends in exactly one place. Per-profile selection is a
        resolver, not an `if` at each caller, so a second construction site is a defect;
      * **no profile of the registry this product ships runs on it.** That one is unchanged, and it
        is the half that still says what secretary-1467 deliberately did not do: the canary is a
        change to the installation's own canon, not to the product.
    """

    def test_the_substrate_is_reached_only_through_its_scoped_consumers(self) -> None:
        package = REPO / "src" / "ummanu" / "runtime" / "head" / "local_pty"
        backend = REPO / "src" / "ummanu" / "runtime" / "local_pty_head.py"
        po_runner = REPO / "src" / "ummanu" / "po" / "runner.py"
        substrate = "ummanu.runtime.head.local_pty"
        offenders = []
        for path in (REPO / "src").rglob("*.py"):
            if package in path.parents or path in (backend, po_runner):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                    names = [node.module]
                if any(name == substrate or name.startswith(substrate + ".") for name in names):
                    offenders.append(str(path.relative_to(REPO)))
        self.assertEqual(offenders, [], "the substrate is reached outside its runtime and PO consumers")

    def test_the_product_builds_its_backends_in_exactly_one_place(self) -> None:
        """Criterion 4 of secretary-1467, criterion 1 of secretary-1474: one build site.

        It moved out of the dispatcher when a second reader appeared — the mechanical-role driver
        in `triggered_agents.runtime.dispatch`, which cannot import the control plane — so the
        assertion is now over the whole product rather than over one module of it: whoever names a
        backend, exactly one function anywhere turns that name into an object.
        """
        sites = {}
        for path in sorted((REPO / "src").rglob("*.py")):
            if path == REPO / "src" / "ummanu" / "runtime" / "local_pty_head.py":
                continue  # the class's own module, where it is defined rather than built
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for holder in ast.walk(tree):
                if not isinstance(holder, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for node in ast.walk(holder):
                    if (
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id.endswith("HeadRuntime")
                    ):
                        sites.setdefault(holder.name, set()).add(node.func.id)

        self.assertEqual(sorted(sites), ["build_head_runtime"], "a second place builds a backend")
        # secretary-1722: the Orca backend is no longer built anywhere.
        self.assertEqual(sites["build_head_runtime"], {"LocalPtyHeadRuntime"})

    def test_no_profile_and_no_registry_names_this_backend(self) -> None:
        offenders = []
        for path in (REPO / "src").rglob("*"):
            if path.is_dir() or path.suffix not in (".yaml", ".yml", ".json", ".toml"):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if "local-pty" in text or "local_pty" in text:
                offenders.append(str(path.relative_to(REPO)))
        self.assertEqual(offenders, [], "a profile selects a backend this card does not wire in")


class AnAgentPromptIsSubmittedTests(LocalPtyRuntimeTestCase):
    """issue:70562b15a7dc8764437e: a prompt for an agent's composer is typed, then sent, then seen taken.

    Every byte of the observer's launch prompt reached Claude's terminal and the prompt sat in its
    composer unsent, while the evidence said `turn_confirmed`. The fake TUI below keeps a composer
    the same way: what it records is what it was *asked to do*, which is the one thing a byte count
    cannot say.
    """

    #: What the dispatcher hands every backend with an agent's prompt; this backend only reads that
    #: one was handed.
    TRANSPORT = object()
    PROMPT = "Read /tmp/SPRINT.md and do its task."

    def setUp(self) -> None:
        super().setUp()
        self.runtime = LocalPtyHeadRuntime(
            self.root,
            head_process_status=head_process_status,
            delivery_grace=TEST_GRACE_SECONDS,
            prompt_quiet=0.5,
            prompt_poll=0.05,
            prompt_first_output=2.0,
            submit_confirm=3.0,
        )
        self.record = self.root / "submitted.jsonl"

    def tui(self, *flags: str) -> str:
        return f"{sys.executable} -u {FAKE_TUI} {self.record} {' '.join(flags)}".strip()

    def submitted(self) -> list[str]:
        if not self.record.exists():
            return []
        return [json.loads(line)["submitted"] for line in self.record.read_text().splitlines() if line]

    def test_a_bare_line_is_typed_into_the_composer_and_never_sent(self) -> None:
        # The defect itself, kept as a fact about a line with no transport: it lands whole, and the
        # agent is asked to do nothing.
        run = self.live_run(command=self.tui())
        time.sleep(0.8)
        receipt = self.runtime.deliver(run, NudgePointer.line(self.PROMPT))
        self.assertEqual(receipt.status, HEAD_OK, receipt.reason)
        time.sleep(1.0)
        self.assertEqual(self.submitted(), [])

    def test_a_prompt_is_sent_and_its_turn_is_confirmed(self) -> None:
        run = self.live_run(command=self.tui())
        receipt = self.runtime.deliver(
            run, NudgePointer.line(self.PROMPT), subject="observer-wake", transport=self.TRANSPORT
        )
        self.assertEqual(receipt.status, HEAD_OK, receipt.reason)
        self.assertEqual([text.strip() for text in self.submitted()], [self.PROMPT])
        evidence = receipt.delivery.evidence
        self.assertTrue(evidence.turn_confirmed)
        self.assertFalse(evidence.payload_left_in_composer)
        self.assertEqual(evidence.submit_count, 1)
        self.assertEqual(evidence.submit_bytes_written, 1)
        self.assertEqual(evidence.stage, "turn_observed")
        accepted = [
            event
            for event in read_events(self.runtime._address(run).journal_path).events
            if event.get("kind") == INPUT_ACCEPTED
        ]
        self.assertEqual(
            [(event["subject"], event["bytes"]) for event in accepted],
            [("observer-wake", len(self.PROMPT) + 1), ("observer-wake:submit", 1)],
            "the line and its Enter are two deliveries, the Enter alone",
        )

    def test_a_launch_prompt_waits_for_the_head_to_settle_and_is_sent(self) -> None:
        receipt = self.bring_up(
            command=self.tui("--banner-delay", "1.0"),
            pointer=NudgePointer.line(self.PROMPT),
            transport=self.TRANSPORT,
            subject="observer-launch",
        )
        self.assertEqual(receipt.status, HEAD_OK, receipt.reason)
        self.assertEqual([text.strip() for text in self.submitted()], [self.PROMPT])
        self.assertTrue(receipt.delivery.evidence.turn_confirmed)
        # A head still working on it is not typed into: the next prompt is refused, not queued.
        busy = self.runtime.deliver(
            receipt.run, NudgePointer.line("too soon"), subject="observer-wake", transport=self.TRANSPORT
        )
        self.assertEqual(busy.status, HEAD_BUSY, busy.reason)
        # And once its turn has ended, the head it raised takes the next prompt the same way.
        self.end_turn(receipt.run)
        again = self.runtime.deliver(
            receipt.run, NudgePointer.line("and again"), subject="observer-wake", transport=self.TRANSPORT
        )
        self.assertEqual(again.status, HEAD_OK, again.reason)
        self.assertEqual([text.strip() for text in self.submitted()], [self.PROMPT, "and again"])

    def test_a_prompt_no_enter_sends_is_not_reported_as_delivered(self) -> None:
        run = self.live_run(command=self.tui("--deaf-enter"))
        receipt = self.runtime.deliver(run, NudgePointer.line(self.PROMPT), transport=self.TRANSPORT)
        self.assertEqual(receipt.status, HEAD_ALIVE, receipt.reason)
        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.reason, DELIVER_NOT_SUBMITTED)
        self.assertTrue(receipt.evidence.payload_left_in_composer)
        self.assertFalse(receipt.evidence.turn_confirmed)
        self.assertEqual(receipt.evidence.submit_count, 2, "one more Enter is given, and no more")
        self.assertEqual(self.submitted(), [])

    def test_a_launch_prompt_no_enter_sends_stops_the_head(self) -> None:
        receipt = self.bring_up(
            command=self.tui("--deaf-enter"),
            pointer=NudgePointer.line(self.PROMPT),
            transport=self.TRANSPORT,
        )
        self.assertFalse(receipt.ok)
        self.assertEqual(receipt.status, HEAD_GONE, receipt.reason)
        self.assertIn(DELIVER_NOT_SUBMITTED, receipt.reason)


class ARetainedHeadIsWokenForItsContinuationTests(LocalPtyRuntimeTestCase):
    """secretary-1702: a red-verdict continuation into a retained worker starts a turn in it.

    A retained worker is suspended with `SIGSTOP` once its round is done, and the continuation
    hands the delivery a transport whose `before_send` sends the `SIGCONT`. This backend read
    nothing of the transport, so the line and both Enters went into the pty of a stopped process
    and every red round ended `prompt_typed_but_no_turn_started`. The fake TUI below is that
    worker: it has finished a turn, then it is stopped the way `retain_worker` stops it.
    """

    FIRST = "Read /tmp/TASK.md and do its task."
    CONTINUATION = (
        "Read /tmp/TASK.md and do its task. Generation 2: use its report command, not an earlier turn's."
    )

    def setUp(self) -> None:
        super().setUp()
        self.runtime = LocalPtyHeadRuntime(
            self.root,
            head_process_status=head_process_status,
            delivery_grace=TEST_GRACE_SECONDS,
            prompt_quiet=0.5,
            prompt_poll=0.05,
            prompt_first_output=2.0,
            submit_confirm=3.0,
        )
        self.record = self.root / "submitted.jsonl"
        self.woken: list[str] = []
        self.accepted_when_woken: list[int] = []

    def accepted(self, run: HeadRun) -> list[dict]:
        """Every payload the head's supervisor wrote into its terminal, off its journal."""
        return [
            event
            for event in read_events(self.runtime._address(run).journal_path).events
            if event.get("kind") == INPUT_ACCEPTED
        ]

    def submitted(self) -> list[str]:
        if not self.record.exists():
            return []
        return [json.loads(line)["submitted"].strip() for line in self.record.read_text().splitlines() if line]

    def retained_head(self) -> HeadRun:
        """A head that has completed one turn and is now suspended, as a retained worker is."""
        run = self.live_run(command=f"{sys.executable} -u {FAKE_TUI} {self.record}")
        first = self.runtime.deliver(
            run, NudgePointer.line(self.FIRST), subject="head-launch", transport=Transport()
        )
        self.assertEqual(first.status, HEAD_OK, first.reason)
        self.end_turn(run)
        self.signal_head(run, signal.SIGSTOP)
        self.addCleanup(self.signal_head, run, signal.SIGCONT)
        self._await(lambda: self.stopped(run), message="the head was never suspended")
        return run

    def signal_head(self, run: HeadRun, number: int) -> None:
        """`CommandHostRuntime._signal_head`: the head's own process group, by its launch identity."""
        status = head_process_status(run.pid_file)
        if status.get("alive"):
            _kill(os.getpgid(int(status["pid"])), number, group=True)

    def stopped(self, run: HeadRun) -> bool:
        return bool(head_process_status(run.pid_file).get("stopped"))

    def resume(self, run: HeadRun):
        """The hook `resume_worker` hands the delivery: it records the call and sends `SIGCONT`."""

        def before_send() -> None:
            self.woken.append(run.run_id)
            self.accepted_when_woken.append(len(self.accepted(run)))
            self.signal_head(run, signal.SIGCONT)

        return Transport(before_send=before_send)

    def test_the_continuation_wakes_the_retained_head_and_starts_a_turn_in_it(self) -> None:
        run = self.retained_head()
        receipt = self.runtime.deliver(
            run,
            NudgePointer.line(self.CONTINUATION),
            subject="worker-continuation",
            transport=self.resume(run),
        )
        self.assertEqual(receipt.status, HEAD_OK, receipt.reason)
        self.assertTrue(receipt.delivery.evidence.turn_confirmed)
        self.assertEqual(receipt.delivery.evidence.submit_count, 1)
        self.assertEqual(self.woken, [run.run_id], "the caller's hook is what wakes the head, once")
        self.assertFalse(self.stopped(run))
        self.assertEqual(self.submitted(), [self.FIRST, self.CONTINUATION])

    def test_a_retained_head_nobody_wakes_is_not_reported_as_delivered(self) -> None:
        # The live failure itself, for a caller that hands no hook: the runtime does not invent a
        # way to resume a head the dispatcher froze, and it does not call the typed line a turn.
        run = self.retained_head()
        receipt = self.runtime.deliver(
            run, NudgePointer.line(self.CONTINUATION), subject="worker-continuation", transport=Transport()
        )
        self.assertEqual(receipt.status, HEAD_ALIVE, receipt.reason)
        self.assertEqual(receipt.reason, DELIVER_NOT_SUBMITTED)
        self.assertEqual(receipt.evidence.submit_count, 2)
        self.assertTrue(self.stopped(run))
        self.assertEqual(self.submitted(), [self.FIRST])

    def test_a_running_head_is_handed_to_the_hook_once_before_the_first_byte(self) -> None:
        # secretary-1719: the hook is the transport's pre-send contract, not a wake for suspended
        # heads only. It also carries a Codex head's provider-source binding, which a running head
        # needs; `SIGCONT` to a running head does nothing.
        run = self.live_run(command=f"{sys.executable} -u {FAKE_TUI} {self.record}")
        receipt = self.runtime.deliver(
            run, NudgePointer.line(self.FIRST), subject="observer-wake", transport=self.resume(run)
        )
        self.assertEqual(receipt.status, HEAD_OK, receipt.reason)
        self.assertEqual(self.woken, [run.run_id], "the hook runs once, and not again for the Enter")
        self.assertEqual(self.accepted_when_woken, [0], "the hook ran before the first byte")
        self.assertEqual(self.submitted(), [self.FIRST])

    def test_a_suspended_head_is_handed_to_the_hook_before_the_first_byte(self) -> None:
        run = self.retained_head()
        accepted = len(self.accepted(run))
        receipt = self.runtime.deliver(
            run, NudgePointer.line(self.CONTINUATION), subject="worker-continuation", transport=self.resume(run)
        )
        self.assertEqual(receipt.status, HEAD_OK, receipt.reason)
        self.assertEqual(self.woken, [run.run_id])
        self.assertEqual(self.accepted_when_woken, [accepted])

    def test_a_running_head_refused_as_busy_is_not_handed_to_the_hook(self) -> None:
        run = self.live_run(command=f"{sys.executable} -u {FAKE_TUI} {self.record}")
        first = self.runtime.deliver(run, NudgePointer.line(self.FIRST), subject="head-launch", transport=Transport())
        self.assertEqual(first.status, HEAD_OK, first.reason)
        busy = self.runtime.deliver(
            run, NudgePointer.line(self.CONTINUATION), subject="observer-wake", transport=self.resume(run)
        )
        self.assertEqual(busy.status, HEAD_BUSY, busy.reason)
        self.assertEqual(self.woken, [])

    def test_a_running_head_refused_as_draining_is_not_handed_to_the_hook(self) -> None:
        run = self.live_run(command=f"{sys.executable} -u {FAKE_TUI} {self.record}")
        self.runtime.request_drain(run, StopInitiator(actor="dispatcher", reason="replacement"))
        receipt = self.runtime.deliver(
            run, NudgePointer.line(self.FIRST), subject="observer-wake", transport=self.resume(run)
        )
        self.assertEqual(receipt.status, HEAD_DRAINING, receipt.reason)
        self.assertEqual(self.woken, [])
        self.assertEqual(self.submitted(), [])

    def test_a_hook_that_raises_hands_back_the_turn_and_writes_nothing(self) -> None:
        # The error handling this backend already had, kept: the exception is the caller's, and
        # the turn the admission granted is released, so the next delivery is not refused as busy.
        run = self.live_run(command=f"{sys.executable} -u {FAKE_TUI} {self.record}")

        def refuse() -> None:
            raise RuntimeError("pre-send refused")

        with self.assertRaisesRegex(RuntimeError, "pre-send refused"):
            self.runtime.deliver(run, NudgePointer.line(self.FIRST), transport=Transport(before_send=refuse))
        self.assertEqual(self.accepted(run), [])
        again = self.runtime.deliver(run, NudgePointer.line(self.FIRST), transport=self.resume(run))
        self.assertEqual(again.status, HEAD_OK, again.reason)

    def test_a_codex_provider_source_is_bound_through_the_hook_before_the_prompt_is_written(self) -> None:
        # secretary-1719: the dispatcher hands a running Codex head `bind_before_delivery` as its
        # hook (`_start_head`, the reviewer continuation, the observer bring-up). On this backend it
        # never ran, so the source stayed unbound until a later lifecycle poll.
        run = self.live_run(command=f"{sys.executable} -u {FAKE_TUI} {self.record}")
        sessions = self.root / "sessions"
        journal = sessions / "2026" / "09" / "24" / "rollout.jsonl"
        journal.parent.mkdir(parents=True)
        journal.write_text(
            json.dumps({"type": "session_meta", "payload": {"session_id": "s-1", "cwd": str(self.workspace.resolve())}})
            + "\n"
            + json.dumps({"type": "thread.started", "thread_id": "parent-1"})
            + "\n",
            encoding="utf-8",
        )
        unbound = run.with_fanout_policy(
            {
                **run.fanout_policy,
                "provider_source": {
                    "version": 1,
                    "kind": "codex_session_event_jsonl",
                    "state": "unbound",
                    **codex_provider_source_descriptor(run),
                    "root": str(sessions),
                    "baseline": [],
                }
            }
        )
        written: list[tuple[str, int]] = []

        def persist(updated: HeadRun) -> None:
            source = updated.fanout_policy.get("provider_source") or {}
            written.append((str(source.get("state")), len(self.accepted(run))))

        ingress = CodexProviderEventIngress(unbound, persist, stop=lambda *_: None, block=lambda _: None)
        receipt = self.runtime.deliver(
            unbound,
            NudgePointer.line(self.FIRST),
            subject="reviewer-launch",
            transport=Transport(before_send=ingress.bind_before_delivery),
        )
        self.assertEqual(receipt.status, HEAD_OK, receipt.reason)
        self.assertIn(("bound", 0), written, "the source was bound before any byte reached the head")
        source = receipt.run.fanout_policy["provider_source"]
        self.assertEqual((source["state"], source["session_id"]), ("bound", "s-1"))
        self.assertEqual(receipt.run.handle, run.handle, "the bound run keeps the address it was delivered at")
        self.assertEqual(self.submitted(), [self.FIRST])

    def test_a_continuation_refused_at_admission_leaves_the_retained_head_suspended(self) -> None:
        # Woken only once the line is admitted: a refused delivery must not turn into a `SIGCONT`
        # the dispatcher then reads as a retained worker that is no longer confirmably suspended.
        run = self.retained_head()
        self.runtime.request_drain(run, StopInitiator(actor="dispatcher", reason="replacement"))
        receipt = self.runtime.deliver(
            run,
            NudgePointer.line(self.CONTINUATION),
            subject="worker-continuation",
            transport=self.resume(run),
        )
        self.assertEqual(receipt.status, HEAD_DRAINING, receipt.reason)
        self.assertEqual(self.woken, [])
        self.assertTrue(self.stopped(run))


@dataclass(frozen=True)
class Transport:
    """The one field of the dispatcher's transport this backend reads."""

    before_send: object = None


if __name__ == "__main__":
    unittest.main()
