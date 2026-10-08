"""The local-pty substrate, exercised against real processes, real ptys and a real socket.

Nothing here is faked. The card this suite belongs to exists to settle one architectural
uncertainty — whether a process the product starts itself can outlive the dispatcher tick that
started it and stay addressable — and a fake supervisor would settle nothing. So every test starts
a real supervisor, which forks a real child onto a real pty, and every test takes back what it
started, on success and on failure alike.
"""

from __future__ import annotations

import ast
import json
import os
import re
import select
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from ummanu.dispatch.watchdog import (
    HEARTBEAT_DEAD,
    HEARTBEAT_LIVE_MATCH,
    head_process_status,
)
from ummanu.runtime.head.local_pty import journal as journal_module
from ummanu.runtime.head.local_pty import protocol
from ummanu.runtime.head.local_pty import supervisor as supervisor_module
from ummanu.runtime.head.local_pty.client import (
    HeadHandle,
    LocalPtySpawnError,
    SupervisorClient,
    spawn_head,
)
from ummanu.runtime.head.local_pty.journal import (
    DRAIN_REQUESTED,
    EVENT_KINDS,
    INPUT_ACCEPTED,
    JOURNAL_SCHEMA_VERSION,
    JOURNAL_TAIL_BYTES,
    PROVIDER_PROGRESSED,
    RUN_EXITED,
    RUN_STARTED,
    RUN_STOPPING,
    TURN_FINISHED,
    TURN_STARTED,
    JournalError,
    JournalWriter,
    read_events,
    read_tail,
)
from ummanu.runtime.head.memory import scope_unit
from ummanu.runtime.head.local_pty.scoped_lifecycle import ScopedHeadLifecycle

REPO = Path(__file__).resolve().parents[1]
CHILD = REPO / "tests" / "fixtures" / "local_pty_child.py"
LAUNCHER = REPO / "tests" / "fixtures" / "local_pty_launcher.py"
SLOW_READER = REPO / "tests" / "fixtures" / "local_pty_slow_reader.py"
CHILD_COMMAND = f"{sys.executable} -u {CHILD}"
# These are test-only bounds.  The shipped values belong to
# ShippedRuntimeDeadlineContractTests; expiry behaviour must not make ordinary runtime feedback
# wait for them.
TEST_DELIVERY_SECONDS = 1.0
TEST_DELIVERY_WAIT_SECONDS = 3.0
TEST_RESPONSE_SECONDS = 0.5
TEST_BYSTANDER_DELIVERY_SECONDS = 3.0


def _identity_of(pid: int, run_id: str) -> dict[str, object]:
    """A launch-identity record for a process that really is running, in the shape the head writes."""
    return {
        "version": 1,
        "pid": pid,
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip(),
        "proc_starttime_ticks": _proc_field(pid, 22),
        "run_id": run_id,
        "role": "worker",
        "task": "ummanu-1463",
    }


def _proc_field(pid: int, index: int) -> str:
    """One field of `/proc/<pid>/stat`, counting the way the kernel numbers them (1-based)."""
    stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    close = stat.rfind(")")
    fields = stat[close + 2 :].split()
    return fields[index - 3]


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        return "Z" not in _proc_field(pid, 3)
    except (OSError, IndexError):
        return False


def _kill(pid: int, number: int = signal.SIGKILL, *, group: bool = False) -> None:
    if pid <= 0:
        return
    try:
        os.killpg(pid, number) if group else os.kill(pid, number)
    except OSError:
        pass


class LocalPtySubstrateTests(unittest.TestCase):
    """Every test owns its processes and its files, and gives all of them back."""

    def setUp(self) -> None:
        # /tmp rather than the workspace: a Unix socket address is bounded at ~100 bytes and a
        # workspace path plus a run id does not fit inside it.
        self.root = Path(tempfile.mkdtemp(prefix="local-pty-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self._started: list[HeadHandle] = []
        self.addCleanup(self._reap_everything)

    def _reap_everything(self) -> None:
        for handle in self._started:
            owner = ScopedHeadLifecycle.from_run_dir(handle.run_dir)
            if owner is not None:
                owner.stop_and_prove_empty()
            _kill(handle.head_pid, group=True)
            _kill(handle.head_pid)
            _kill(handle.supervisor_pid)
        for handle in self._started:
            self._await(lambda handle=handle: not _alive(handle.head_pid), timeout=5.0, soft=True)

    def _start(self, *, run_id: str = "run", command: str = CHILD_COMMAND, **options) -> HeadHandle:
        handle = spawn_head(
            root=self.root,
            run_id=run_id,
            role="worker",
            task="ummanu-1463",
            command=command,
            quiet_seconds=options.pop("quiet_seconds", 0.4),
            **options,
        )
        self._started.append(handle)
        return handle

    @unittest.skipUnless(os.environ.get("GITHUB_ACTIONS") == "true", "transient system scope proof runs in CI")
    def test_tiny_scope_records_a_real_memory_limit_kill(self) -> None:
        # Touch each page: a zero-filled bytearray may stay lazily backed and never count
        # against the scope's MemoryMax.
        pressure = (
            "import subprocess, sys, threading, time; sys.stdin.readline(); "
            "threading.Thread(target=time.sleep, args=(5,), daemon=True).start(); "
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)']); "
            "data = bytearray(192 * 1024 * 1024); "
            "data[::4096] = b'x' * (len(data) // 4096); time.sleep(5)"
        )
        command = (
            f"{shlex.quote(sys.executable)} -u -c "
            + shlex.quote(pressure)
        )
        handle = self._start(run_id="tiny-memory-limit", command=command, memory_limit_mib=96)
        unified = next(
            line[3:] for line in Path(f"/proc/{handle.head_pid}/cgroup").read_text().splitlines()
            if line.startswith("0::")
        )
        cgroup = Path("/sys/fs/cgroup") / unified.lstrip("/")
        self.assertEqual(cgroup.name, scope_unit(handle.run_id))
        self.assertEqual((cgroup / "memory.max").read_text().strip(), str(96 * 1024 * 1024))
        self.assertEqual((cgroup / "memory.swap.max").read_text().strip(), "0")
        self.assertTrue(self._client(handle).send_input("go\n")["ok"])
        self._await(
            lambda: bool(handle.events().of_kind(RUN_EXITED)), timeout=20.0,
            message="the over-limit head did not exit",
        )
        exited = handle.events().of_kind(RUN_EXITED)[-1]
        self.assertEqual(exited.get("signal"), signal.SIGKILL, exited)
        self.assertEqual(exited.get("head_loss_reason"), "memory_limit")
        self.assertTrue(scope_unit(handle.run_id).startswith("ummanu-head-"))

    def _client(self, handle: HeadHandle) -> SupervisorClient:
        client = handle.connect()
        self.addCleanup(client.close)
        return client

    def _await(self, predicate, *, timeout: float = 5.0, message: str = "", soft: bool = False) -> bool:
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

    def _await_output(self, client: SupervisorClient, marker: bytes, *, timeout: float = 8.0) -> bytes:
        seen = b""

        def arrived() -> bool:
            nonlocal seen
            seen = client.read_output()["bytes_data"]
            return marker in seen

        self._await(arrived, timeout=timeout, message=f"{marker!r} never appeared in {seen[-400:]!r}")
        return seen

    def test_status_says_how_long_the_head_has_printed_nothing(self) -> None:
        """ummanu-140: one status answers whether a head is settled, so a handoff need not watch it.

        The count restarts at the head's output, not at a turn's start: `turn.started` is the
        supervisor's bookkeeping, and only what the head printed says it is not quiet.
        """
        handle = self._start(run_id="idle-seconds")
        client = self._client(handle)
        self.assertTrue(client.send_input("hello\n")["ok"])
        self._await_output(client, b"ECHO hello")
        self._await(lambda: client.status()["output_idle_seconds"] >= 0.5, message="the idle count never grew")
        quiet = client.status()["output_idle_seconds"]
        self.assertTrue(client.send_input("again\n")["ok"])
        self._await_output(client, b"ECHO again")
        self.assertLess(client.status()["output_idle_seconds"], quiet)

    # -- the screen the provider-failure reader sees (secretary-1799) ------------------------

    def test_the_backend_renders_the_heads_screen_for_the_provider_failure_reader(self) -> None:
        """`head_run_screen_lines` reads a live head's screen through its supervisor, read-only."""
        from ummanu.runtime.local_pty_head import head_run_screen_lines
        from ummanu.runtime.provider_errors import screen_turn_failure

        error = "  \u23bf  API Error: 401 invalid token \u00b7 Please run /login"
        handle = self._start(
            run_id="screen",
            command=f"{sys.executable} -u -c \"print({error!r}); print('> '); import time; time.sleep(30)\"",
        )
        self._await_output(self._client(handle), b"Please run /login")

        screen = head_run_screen_lines(self.root, "screen")

        self.assertEqual(screen["state"], "observed")
        self.assertTrue(any("API Error: 401" in line for line in screen["lines"]), screen["lines"])
        found = screen_turn_failure(screen["lines"])
        assert found is not None
        self.assertEqual((found.status, found.source), (401, "pty-screen"))
        self.assertTrue(_alive(handle.head_pid), "reading the screen must not disturb the head")
        self.assertEqual(head_run_screen_lines(self.root, "no-such-run")["state"], "unavailable")

    # -- ownership of the process ----------------------------------------------------------

    def test_the_head_gets_its_own_session_and_the_launchers_group_cannot_signal_it(self) -> None:
        launcher, handle = self._launch_from_another_group(run_id="own-session", linger=30.0)
        self.addCleanup(_kill, launcher.pid, signal.SIGKILL, group=True)

        head_pid = handle.head_pid
        self.assertEqual(_proc_field(head_pid, 6), str(head_pid), "the head leads its own session")
        self.assertEqual(_proc_field(head_pid, 5), str(head_pid), "the head leads its own group")
        self.assertNotEqual(_proc_field(head_pid, 6), _proc_field(launcher.pid, 6))
        self.assertNotEqual(_proc_field(head_pid, 5), _proc_field(launcher.pid, 5))
        self.assertNotEqual(_proc_field(handle.supervisor_pid, 5), _proc_field(launcher.pid, 5))

        os.killpg(launcher.pid, signal.SIGTERM)
        launcher.wait(timeout=10)
        time.sleep(0.4)
        self.assertTrue(_alive(head_pid), "a signal to the launcher's group reached the head")
        self.assertTrue(_alive(handle.supervisor_pid))
        self.assertTrue(self._client(handle).status()["alive"])

    def test_the_supervisor_outlives_the_process_that_started_it(self) -> None:
        launcher, handle = self._launch_from_another_group(run_id="outlives", linger=0.0)
        launcher.wait(timeout=15)
        self.assertFalse(_alive(launcher.pid), "the launcher is gone")

        self.assertTrue(_alive(handle.head_pid), "the head died with its launcher")
        self.assertTrue(_alive(handle.supervisor_pid), "the supervisor died with its launcher")
        self.assertNotIn("Z", _proc_field(handle.supervisor_pid, 3), "the supervisor is a zombie")
        self.assertNotEqual(
            _proc_field(handle.supervisor_pid, 4),
            str(launcher.pid),
            "the supervisor is still parented to the launcher that exited",
        )
        client = self._client(handle)
        status = client.status()
        self.assertTrue(status["alive"])
        self.assertEqual(status["head_pid"], handle.head_pid)
        self.assertTrue(client.send_input("hello\n")["ok"], "the head is no longer addressable")
        self.assertIn(b"ECHO hello", self._await_output(client, b"ECHO hello"))

    def _launch_from_another_group(self, *, run_id: str, linger: float):
        """Start a head from a process that is neither this one nor in this one's group."""
        handle_file = self.root / f"{run_id}-handle.json"
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.pathsep.join(
            [str(REPO / "src"), environment.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)
        launcher = subprocess.Popen(
            [
                sys.executable,
                "-P",
                str(LAUNCHER),
                "--root",
                str(self.root),
                "--run-id",
                run_id,
                "--command",
                CHILD_COMMAND,
                "--handle-file",
                str(handle_file),
                "--linger",
                str(linger),
            ],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.addCleanup(launcher.wait)
        self.addCleanup(launcher.stdout.close)
        self.addCleanup(_kill, launcher.pid, signal.SIGKILL)
        self._await(handle_file.exists, timeout=30.0, message="the launcher never published a handle")
        record = json.loads(handle_file.read_text(encoding="utf-8"))
        handle = HeadHandle(
            run_dir=Path(record["run_dir"]),
            run_id=record["run_id"],
            role=record["role"],
            task=record["task"],
            socket_path=Path(record["socket_path"]),
            journal_path=Path(record["journal_path"]),
            pid_file=Path(record["pid_file"]),
            supervisor_pid=int(record["supervisor_pid"]),
            head_pid=int(record["head_pid"]),
        )
        self._started.append(handle)
        return launcher, handle

    # -- the terminal ----------------------------------------------------------------------

    def test_the_head_runs_on_a_pty_that_can_be_sized_and_resized(self) -> None:
        # A size neither the kernel's default (0x0) nor a conventional one (24x80) could be
        # mistaken for: the head reads it once, at once, so a size set after the fork would be a
        # race this assertion loses. It is set on the pty before the head exists.
        handle = self._start(run_id="terminal", rows=37, cols=113)
        client = self._client(handle)
        self.assertIn(b"SIZE 37x113", self._await_output(client, b"SIZE 37x113"))

        resized = client.resize(41, 132)
        self.assertEqual(
            {key: resized[key] for key in ("ok", "rows", "cols")},
            {"ok": True, "rows": 41, "cols": 132},
        )
        # Every answer carries the id of the request it answers; nothing else is added to this one.
        self.assertEqual(set(resized) - {protocol.REQUEST_ID}, {"ok", "rows", "cols"})
        self.assertIn(b"WINCH 41x132", self._await_output(client, b"WINCH 41x132"))

    # -- bounded input ---------------------------------------------------------------------

    def test_an_oversized_input_is_refused_by_name_rather_than_truncated(self) -> None:
        handle = self._start(run_id="input-limit")
        client = self._client(handle)
        self._await_output(client, b"SIZE ")

        payload = b"z" * (protocol.INPUT_MAX_BYTES + 1)
        answer = client.send_input(payload)
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["error"], protocol.ERROR_INPUT_TOO_LARGE)
        self.assertEqual(answer["limit_bytes"], protocol.INPUT_MAX_BYTES)
        self.assertEqual(answer["size_bytes"], len(payload))
        self.assertIn(str(protocol.INPUT_MAX_BYTES), answer["detail"])
        self.assertIn(str(len(payload)), answer["detail"])

        # Nothing of the refused payload reached the head, in whole or in part.
        self.assertTrue(client.send_input("hello\n")["ok"])
        seen = self._await_output(client, b"ECHO hello")
        self.assertNotIn(b"zzzz", seen)
        self.assertEqual(handle.events().of_kind(INPUT_ACCEPTED)[0]["bytes"], 6)

    def test_the_input_limit_is_far_above_the_legacy_path_that_lost_a_nudge(self) -> None:
        # issue:d9d049eaad39d02bbb1e — a continuation nudge did not fit in the legacy 256-byte cap
        # and was silently truncated. A limit is only a real answer to that if a continuation
        # payload fits inside it with room to spare, and only if the payload *arrives whole*: the
        # head is asked how many bytes it read, because the loss this is about is a silent one and
        # the prefix of a truncated delivery looks exactly like a whole one.
        self.assertGreaterEqual(protocol.INPUT_MAX_BYTES, 64 * 1024)
        self.assertGreater(protocol.FRAME_MAX_BYTES, protocol.INPUT_MAX_BYTES)
        handle = self._start(run_id="big-input")
        client = self._client(handle)
        self._await_output(client, b"SIZE ")
        self.assertEqual(self._bulk(handle, client, 8192, index=0), 8193)

    def test_a_delivery_across_the_line_disciplines_edge_arrives_whole(self) -> None:
        """The 4096-byte edge a canonical line discipline drops the far side of, in silence.

        These sizes are the defect this substrate must not have: `N_TTY` in canonical mode caps a
        line at 4095 bytes and discards the rest without blocking, erroring or telling anyone. The
        head reports the length it read, so a truncation is a failed equality rather than a passing
        prefix match.
        """
        handle = self._start(run_id="edge-input")
        client = self._client(handle)
        self._await_output(client, b"SIZE ")
        for index, size in enumerate((4094, 4095, 4096, 4097, 8000)):
            with self.subTest(size=size):
                self.assertEqual(self._bulk(handle, client, size, index=index), size + 1)

    def test_a_delivery_at_the_declared_limit_arrives_whole_and_one_over_is_refused(self) -> None:
        handle = self._start(run_id="limit-input")
        client = self._client(handle)
        self._await_output(client, b"SIZE ")
        # One byte short of the limit plus the newline is exactly the limit: the largest delivery
        # the supervisor says it takes, taken whole.
        self.assertEqual(
            self._bulk(handle, client, protocol.INPUT_MAX_BYTES - 1, index=0),
            protocol.INPUT_MAX_BYTES,
        )
        answer = client.send_input(b"z" * (protocol.INPUT_MAX_BYTES + 1))
        self.assertEqual(answer["error"], protocol.ERROR_INPUT_TOO_LARGE)
        accepted = handle.events().of_kind(INPUT_ACCEPTED)
        self.assertEqual(len(accepted), 1, "the refused delivery was written down as an arrival")
        self.assertEqual(accepted[0]["bytes"], protocol.INPUT_MAX_BYTES)

    def _bulk(self, handle: HeadHandle, client: SupervisorClient, size: int, *, index: int) -> int:
        """Send a payload of exactly `size` bytes and return the length the head says it read.

        The `ok` here is an admission, not an arrival: the supervisor took the payload on within its
        own tick and is writing it. What actually reached the terminal is asked for afterwards —
        which is the same number, and the test says so — because that is where the substrate keeps
        it now.
        """
        payload = ("bulk " + "a" * (size - 5)).encode("ascii")
        self.assertEqual(len(payload), size)
        answer = client.send_input(payload + b"\n")
        self.assertTrue(answer["ok"], answer)
        self.assertTrue(answer["accepted"], answer)
        self.assertEqual(answer["accepted_bytes"], size + 1)
        self.assertEqual(answer["delivery"]["state"], protocol.DELIVERY_IN_FLIGHT, answer)
        delivered = client.wait_for_delivery(answer["delivery"]["id"], timeout=30.0)
        self.assertEqual(delivered["state"], protocol.DELIVERY_COMPLETE, delivered)
        self.assertEqual(delivered["written_bytes"], size + 1, "the supervisor wrote less than it took")
        reports: list[bytes] = []

        def reported() -> bool:
            nonlocal reports
            reports = re.findall(rb"BULK (\d+)", client.read_output()["bytes_data"])
            return len(reports) > index

        self._await(reported, timeout=20.0, message=f"the head never reported a {size}-byte delivery")
        # The journal counts what was written, not what was offered.
        accepted = handle.events().of_kind(INPUT_ACCEPTED)
        self.assertEqual(accepted[index]["bytes"], size + 1)
        self.assertEqual(accepted[index]["offered_bytes"], size + 1)
        self.assertTrue(accepted[index]["complete"])
        return int(reports[index])

    # -- a delivery is admitted, and never something a caller waits through --------------

    def _slow_head(
        self,
        run_id: str,
        *,
        chunk: int,
        pause: float,
        delivery_seconds: float,
        total: int = 0,
    ) -> HeadHandle:
        command = f"{sys.executable} -u {SLOW_READER} --chunk {chunk} --pause {pause}"
        if total:
            command += f" --total {total}"
        return self._start(
            run_id=run_id,
            command=command,
            delivery_seconds=delivery_seconds,
        )

    def _prompt(self, call, *, within: float, what: str):
        """Call something on the socket and fail if it did not answer well inside `within`."""
        started = time.monotonic()
        answer = call()
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, within, f"{what} took {elapsed:.2f}s, over {within:g}s")
        return answer

    def test_a_head_that_never_reads_keeps_answering_and_makes_its_stall_observable(self) -> None:
        """The head stops reading its terminal, and nothing about the socket slows down.

        A pty holds a few kilobytes, so a 64 KiB payload to a head that never reads cannot finish:
        it is carried for the supervisor's delivery bound and then abandoned. What the caller must
        never get out of that is a wait. It gets an admission inside a tick, an answer to every
        question it asks while the delivery is stuck — *its own* answer, with `alive` in it, about
        a head that really is alive — and the stall itself as state, at the moment it happens.
        """
        handle = self._slow_head(
            "stalled",
            chunk=4096,
            pause=600.0,
            delivery_seconds=TEST_DELIVERY_SECONDS,
        )
        client = self._client(handle)
        self._await_output(client, b"UP")

        payload = b"z" * protocol.INPUT_MAX_BYTES
        answer = self._prompt(
            lambda: client.send_input(payload, subject="nudge"),
            within=TEST_RESPONSE_SECONDS,
            what="send_input",
        )
        self.assertTrue(answer["ok"], answer)
        self.assertTrue(answer["accepted"], answer)
        self.assertEqual(answer["accepted_bytes"], len(payload))
        delivery_id = answer["delivery"]["id"]
        self.assertEqual(answer["delivery"]["state"], protocol.DELIVERY_IN_FLIGHT)

        # The injected substrate bound is enough to make the delivery expire.  This connection
        # must still answer throughout it, and every answer must be the answer that was asked.
        deadline = time.monotonic() + TEST_DELIVERY_WAIT_SECONDS
        seen_in_flight = False
        while time.monotonic() < deadline:
            status = self._prompt(
                client.status,
                within=TEST_RESPONSE_SECONDS,
                what="status during a stuck delivery",
            )
            self.assertTrue(status["ok"], status)
            self.assertIn("alive", status)
            self.assertTrue(status["alive"], "a live head was reported dead")
            self.assertEqual(status["delivery"]["id"], delivery_id)
            if status["delivery"]["state"] == protocol.DELIVERY_IN_FLIGHT:
                seen_in_flight = True
                time.sleep(0.1)
                continue
            break
        else:  # pragma: no cover - only reached if the delivery never leaves the in-flight state
            self.fail("the delivery never left the in-flight state")
        self.assertTrue(seen_in_flight, "the delivery finished before it could be observed running")
        self.assertEqual(client.stale_frames, 0, "a frame answered the wrong request")

        stalled = client.status()["delivery"]
        self.assertEqual(stalled["state"], protocol.DELIVERY_STALLED)
        self.assertFalse(stalled["complete"])
        self.assertGreater(stalled["written_bytes"], 0)
        self.assertLess(stalled["written_bytes"], len(payload))
        self.assertIn(str(stalled["written_bytes"]), stalled["detail"])
        self.assertIn(str(len(payload)), stalled["detail"])

        # The journal says the same thing, and counts only what the terminal took.
        accepted = handle.events().of_kind(INPUT_ACCEPTED)[-1]
        self.assertEqual(accepted["bytes"], stalled["written_bytes"], "the journal overstated it")
        self.assertEqual(accepted["offered_bytes"], len(payload))
        self.assertFalse(accepted["complete"])
        self.assertEqual(accepted["state"], protocol.DELIVERY_STALLED)

        # The head is untouched by the stall, and admission is open again afterwards.
        self.assertTrue(_alive(handle.head_pid))
        self.assertTrue(client.status()["alive"])
        self.assertTrue(client.send_input(b"small\n")["ok"])

    def test_a_delivery_a_head_takes_slowly_still_succeeds_and_says_so_as_itself(self) -> None:
        """The worst of the three defects: a delivery that entirely succeeds.

        The head reads 8 KiB at a time with a pause between reads, so 40000 bytes — well inside the
        declared limit, nothing pathological — take seconds to land. The caller is told the truth
        at every point: admitted at once, in flight while it is, complete when it is, with the
        payload's own size as the count. Not an exception, and never a stale frame in place of the
        next answer.
        """
        handle = self._slow_head(
            "slow-reader",
            chunk=8192,
            pause=0.12,
            total=40000,
            delivery_seconds=1.5,
        )
        client = self._client(handle)
        self._await_output(client, b"UP")

        payload = b"s" * 40000
        answer = self._prompt(
            lambda: client.send_input(payload), within=TEST_RESPONSE_SECONDS, what="send_input"
        )
        self.assertTrue(answer["ok"], answer)
        delivery_id = answer["delivery"]["id"]

        status = self._prompt(client.status, within=TEST_RESPONSE_SECONDS, what="status mid-delivery")
        self.assertTrue(status["alive"], "a live head was reported dead")
        self.assertEqual(status["delivery"]["id"], delivery_id)

        delivered = client.wait_for_delivery(delivery_id, timeout=TEST_DELIVERY_WAIT_SECONDS)
        self.assertEqual(delivered["state"], protocol.DELIVERY_COMPLETE, delivered)
        self.assertTrue(delivered["complete"])
        self.assertEqual(delivered["written_bytes"], len(payload))
        self.assertEqual(client.stale_frames, 0, "a frame answered the wrong request")

        accepted = handle.events().of_kind(INPUT_ACCEPTED)[-1]
        self.assertEqual(accepted["bytes"], len(payload))
        self.assertEqual(accepted["offered_bytes"], len(payload))
        self.assertTrue(accepted["complete"])
        # The head, not the supervisor, confirms it: every byte was read off the terminal.
        self._await_output(client, b"READ 40000", timeout=TEST_DELIVERY_WAIT_SECONDS)

    def test_a_bystander_is_answered_and_the_loop_keeps_ticking_during_a_delivery(self) -> None:
        """A second caller who had nothing to do with the delivery, and the tick behind it.

        A single-threaded loop that waits for the head inside a request handler stops serving
        everyone, not only the caller that delivered: a bystander's `status` slides, and so do the
        loop's own duties — the quiet-turn check, the reap, the stop escalation. So the bystander
        asks repeatedly while a 64 KiB payload is stuck, and then asks the supervisor to stop the
        head, which is a thing only a loop that is still ticking can do.
        """
        handle = self._slow_head(
            "bystander",
            chunk=4096,
            pause=600.0,
            delivery_seconds=TEST_BYSTANDER_DELIVERY_SECONDS,
        )
        deliverer = self._client(handle)
        bystander = self._client(handle)
        self._await_output(deliverer, b"UP")

        accepted = deliverer.send_input(b"z" * protocol.INPUT_MAX_BYTES)
        self.assertTrue(accepted["ok"], accepted)
        self.assertEqual(accepted["delivery"]["state"], protocol.DELIVERY_IN_FLIGHT)

        for _ in range(10):
            status = self._prompt(
                bystander.status,
                within=TEST_RESPONSE_SECONDS,
                what="a bystander's status during a delivery",
            )
            self.assertTrue(status["ok"], status)
            self.assertTrue(status["alive"])
            self.assertEqual(status["delivery"]["state"], protocol.DELIVERY_IN_FLIGHT)
            time.sleep(0.1)
        self.assertEqual(bystander.stale_frames, 0)

        # The delivering connection is not out of step either: its next question is its own.
        own = self._prompt(
            deliverer.status,
            within=TEST_RESPONSE_SECONDS,
            what="the deliverer's next status",
        )
        self.assertTrue(own["alive"])
        self.assertEqual(own["delivery"]["state"], protocol.DELIVERY_IN_FLIGHT)
        self.assertEqual(deliverer.stale_frames, 0)

        # And the loop is still doing its own work: a stop, asked for by the bystander mid-delivery.
        stop = self._prompt(
            lambda: bystander.stop(initiator="test"),
            within=TEST_RESPONSE_SECONDS,
            what="stop mid-delivery",
        )
        self.assertTrue(stop["ok"], stop)
        self._await(
            lambda: not _alive(handle.head_pid),
            timeout=10.0,
            message="a stop asked for during a delivery never reached the head",
        )

    def test_a_second_delivery_while_one_is_in_flight_is_refused_by_name(self) -> None:
        """One payload at a time, and the refusal says which one holds the floor.

        Two payloads in flight would interleave on the one terminal, and neither caller could be
        told what the head received. Refusing is immediate, like every other answer here.
        """
        handle = self._slow_head(
            "one-at-a-time",
            chunk=4096,
            pause=600.0,
            delivery_seconds=TEST_DELIVERY_SECONDS,
        )
        client = self._client(handle)
        self._await_output(client, b"UP")

        first = client.send_input(b"z" * protocol.INPUT_MAX_BYTES)
        self.assertTrue(first["ok"], first)
        second = self._prompt(
            lambda: client.send_input(b"second\n"),
            within=TEST_RESPONSE_SECONDS,
            what="a second delivery",
        )
        self.assertFalse(second["ok"], second)
        self.assertEqual(second["error"], protocol.ERROR_INPUT_IN_FLIGHT)
        self.assertEqual(second["delivery"]["id"], first["delivery"]["id"])
        self.assertIn(str(second["delivery"]["size_bytes"]), second["detail"])
        # The refused payload is not an event: nothing about the head changed.
        self.assertEqual(handle.events().of_kind(INPUT_ACCEPTED), ())

    def test_an_answer_carries_the_id_of_the_question_and_a_stale_frame_is_discarded(self) -> None:
        """The recovery a client owes itself when it stops waiting for an answer.

        Frames are otherwise told apart only by their order, so a caller that abandoned one — a
        timeout, an interrupt — would find it sitting there as the answer to its *next* question,
        and stay one answer out of step for good. Here a request is deliberately sent and its
        answer left unread, exactly as an abandoned one would be, and the next call still gets its
        own answer — with the discard counted rather than silent.
        """
        handle = self._start(run_id="correlated")
        client = self._client(handle)

        first = client.status()
        self.assertIn(protocol.REQUEST_ID, first)
        second = client.status()
        self.assertNotEqual(second[protocol.REQUEST_ID], first[protocol.REQUEST_ID])

        # A question asked and never listened for: its answer is now waiting in the socket.
        # Reaching past the client's own API on purpose: the point is a frame it never reads.
        client._conn.sendall(
            protocol.encode_frame({"op": protocol.OP_RESIZE, "rows": 11, "cols": 22, "id": "orphan"})
        )
        self._await(
            lambda: client.status()["rows"] == 11,
            timeout=5.0,
            message="the abandoned request was never carried out",
        )
        self.assertGreaterEqual(client.stale_frames, 1, "the orphaned answer was not discarded")
        # Not one answer out of step: this is a status, and it says so.
        answer = client.status()
        self.assertTrue(answer["ok"])
        self.assertEqual(answer["run_id"], handle.run_id)
        self.assertIn("alive", answer)

    def test_a_frame_without_an_end_is_refused_with_its_limit_and_its_size(self) -> None:
        handle = self._start(run_id="frame-limit")
        # A foreign client, spoken to the socket directly: the bound belongs to the supervisor, not
        # to the convenience of this repository's own client class.
        raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(raw.close)
        raw.settimeout(5.0)
        raw.connect(str(handle.socket_path))
        raw.sendall(b"{" + b"a" * (protocol.FRAME_MAX_BYTES + 16))
        answer = json.loads(raw.recv(65536).split(b"\n")[0])
        self.assertEqual(answer["error"], protocol.ERROR_FRAME_TOO_LARGE)
        self.assertEqual(answer["limit_bytes"], protocol.FRAME_MAX_BYTES)
        self.assertGreater(answer["size_bytes"], protocol.FRAME_MAX_BYTES)
        # The head is untouched by a client that misbehaved.
        self.assertTrue(_alive(handle.head_pid))
        self.assertTrue(self._client(handle).status()["alive"])

    # -- bounded output --------------------------------------------------------------------

    def test_output_is_bounded_and_the_reader_is_told_what_was_dropped(self) -> None:
        handle = self._start(run_id="output-bound")
        client = self._client(handle)
        self._await_output(client, b"SIZE ")
        lines = protocol.OUTPUT_BUFFER_BYTES // 1000 + 200
        self.assertTrue(client.send_input(f"spew {lines}\n")["ok"])
        self._await_output(client, b"SPEWDONE", timeout=30.0)

        answer = client.read_output()
        self.assertLessEqual(answer["bytes"], protocol.OUTPUT_BUFFER_BYTES)
        self.assertTrue(answer["truncated"])
        self.assertGreater(answer["dropped_bytes"], 0)
        self.assertGreater(answer["total_bytes"], answer["bytes"])
        tail = answer["bytes_data"]
        self.assertIn(b"SPEWDONE", tail, "the reader did not get the freshest tail")
        self.assertNotIn(b"SIZE 24x80", tail, "the oldest output survived a bounded buffer")

    # -- bounded attach --------------------------------------------------------------------

    def test_attach_is_bounded_and_detaching_neither_kills_the_head_nor_loses_its_output(self) -> None:
        handle = self._start(run_id="attach")
        commander = self._client(handle)
        self._await_output(commander, b"SIZE ")

        attached = []
        for _ in range(protocol.ATTACH_MAX_CLIENTS):
            client = handle.connect()
            self.addCleanup(client.close)
            answer = client.attach()
            self.assertTrue(answer["ok"], answer)
            attached.append(client)

        surplus = handle.connect()
        self.addCleanup(surplus.close)
        refusal = surplus.attach()
        self.assertFalse(refusal["ok"])
        self.assertEqual(refusal["error"], protocol.ERROR_ATTACH_LIMIT)
        self.assertEqual(refusal["limit"], protocol.ATTACH_MAX_CLIENTS)
        self.assertEqual(refusal["attached"], protocol.ATTACH_MAX_CLIENTS)

        self.assertTrue(commander.send_input("first\n")["ok"])
        for client in attached:
            self._await_event(client, b"ECHO first")

        # A caller leaving is not an event in the head's life.
        for client in attached[1:]:
            client.close()
        self._await(
            lambda: commander.status()["attached"] == 1,
            message="the supervisor never noticed the detachments",
        )
        self.assertTrue(_alive(handle.head_pid), "detaching killed the head")
        self.assertTrue(commander.send_input("second\n")["ok"])
        self._await_event(attached[0], b"ECHO second")
        self.assertIn(b"ECHO second", self._await_output(commander, b"ECHO second"))

        # The attach slots the departed clients held are given back, not leaked.
        self.assertTrue(surplus.attach()["ok"])

    def test_the_last_thing_dropped_is_still_reported_before_the_stream_ends(self) -> None:
        """A count of dropped output must not be lost because the overflow was the final chunk.

        The notice used to travel with the next chunk that fit, so a client that overflowed at the
        very end of a run was told nothing: it saw the stream end with no sign that part of it was
        missing. Here the head's last act is to exit without a word, so there is no next chunk.

        What is asserted is the property itself, as an account that has to balance: every byte the
        head wrote after the attach either arrived as output or is inside the last `dropped` count
        the stream carried before `exited`. Which frame carries that count is timing: on a slow
        host the last line of the spew can be read on its own and still fit, and then the count
        rides with it instead of with the exit (secretary-1703, runtime-component on bf5a655). An
        unreported final loss leaves the account short either way, so every frame from the attach
        to the exit is kept, the ones read while the stream drains included.
        """
        handle = self._start(run_id="dropped-tail")
        commander = self._client(handle)
        self._await_output(commander, b"SIZE ")
        watcher = handle.connect()
        self.addCleanup(watcher.close)
        attached = watcher.attach()
        self.assertTrue(attached["ok"])

        # Far more than the supervisor will hold for one client, none of it read while it arrives.
        self.assertTrue(commander.send_input(f"spew {protocol.OUTPUT_BUFFER_BYTES * 8 // 1000}\n")["ok"])
        self._await_output(commander, b"SPEWDONE", timeout=60.0)
        frames = []
        while (event := watcher.next_event(0.4)) is not None:
            frames.append(event)

        self.assertTrue(commander.send_input("die 5\n")["ok"])
        while True:
            event = watcher.next_event(5.0)
            if event is None:
                break
            frames.append(event)
            if event.get("event") == protocol.EVENT_EXITED:
                break
        kinds = [frame.get("event") for frame in frames]
        self.assertEqual(kinds[-1], protocol.EVENT_EXITED, kinds[-5:])
        self.assertEqual(frames[-1]["record"]["exit_code"], 5)
        self.assertIn(protocol.EVENT_DROPPED, kinds, "the loss was never reported")
        dropped = [frame for frame in frames if frame.get("event") == protocol.EVENT_DROPPED][-1]
        self.assertGreater(dropped["bytes"], 0)

        received = sum(
            len(protocol.decode_payload(frame.get("data") or ""))
            for frame in frames
            if frame.get("event") == protocol.EVENT_OUTPUT
        )
        written_since_attach = frames[-1]["record"]["output_bytes"] - attached["total_bytes"]
        self.assertEqual(
            received + dropped["bytes"],
            written_since_attach,
            "the stream ended with part of the loss unreported",
        )

    def test_a_refusal_written_before_the_connection_closed_is_not_lost_to_the_write_that_failed(
        self,
    ) -> None:
        """The connection bound answers before it is asked, and the answer must survive.

        A caller the supervisor will not hold is told so and let go immediately, so the refusal is
        already in the caller's receive queue when the caller writes its first request — and that
        write fails, because the peer is gone. The kernel keeps the queued bytes readable anyway,
        which is what makes the refusal recoverable; a client that let the failed write win would
        turn a live head at a bound into an exception raised out of a verb, which is the "alive
        looks dead" collapse in another costume.

        A real listening socket rather than the supervisor, because what is being pinned is the
        order — answered, closed, only then written to — and against a real supervisor that order
        is a race this test would lose most of the time.
        """
        directory = Path(tempfile.mkdtemp(prefix="lp-refusal-"))
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(str(directory / "socket"))
        listener.listen(1)

        client = SupervisorClient.connect(directory / "socket", timeout=5.0)
        self.addCleanup(client.close)
        conn, _ = listener.accept()
        conn.sendall(protocol.encode_frame(protocol.connection_refusal(8)))
        conn.close()
        self._await(
            lambda: bool(select.select([client._conn], [], [], 0)[0]),
            message="the refusal never reached the caller",
        )

        answer = client.attach()

        self.assertFalse(answer["ok"])
        self.assertEqual(answer["error"], protocol.ERROR_CONNECTION_LIMIT)
        self.assertEqual(answer["limit"], protocol.CONNECTION_MAX_CLIENTS)

    def test_an_answer_to_an_abandoned_request_is_not_read_as_this_request_s_refusal(self) -> None:
        """The queue read after a failed write is subject to the id, like every other read.

        The recovery above reads whatever the supervisor said before it let go. What it must not
        do is hand back an answer that names a *different* question: an id on a frame says which
        request it answers, and the request that just failed to be sent is not that one. Without
        the check, a client that had abandoned an earlier request would take that stale answer as
        this one's news and stay one answer out of step for the rest of its life — the exact
        desynchronisation the id exists to prevent, arriving through the one path that reads the
        queue without having asked anything.
        """
        directory = Path(tempfile.mkdtemp(prefix="lp-stale-"))
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(str(directory / "socket"))
        listener.listen(1)

        client = SupervisorClient.connect(directory / "socket", timeout=5.0)
        self.addCleanup(client.close)
        conn, _ = listener.accept()
        # An answer to a question this client stopped waiting for, and then the news that is
        # really its own, in that order.
        conn.sendall(protocol.encode_frame({"ok": True, "alive": False, protocol.REQUEST_ID: 1}))
        conn.sendall(protocol.encode_frame(protocol.connection_refusal(8)))
        conn.close()
        self._await(
            lambda: bool(select.select([client._conn], [], [], 0)[0]),
            message="the frames never reached the caller",
        )

        answer = client.status()

        self.assertEqual(answer["error"], protocol.ERROR_CONNECTION_LIMIT, "a stale answer won")
        self.assertNotIn("alive", answer, "the abandoned request's answer became this one's")
        self.assertEqual(client.stale_frames, 1, "the stale frame was not counted as one")

    def test_connections_are_bounded_as_well_as_attachments(self) -> None:
        handle = self._start(run_id="connections")
        held = []
        for _ in range(protocol.CONNECTION_MAX_CLIENTS):
            client = handle.connect()
            self.addCleanup(client.close)
            # The answer is what proves the supervisor has taken the connection, not just the
            # kernel's backlog.
            self.assertTrue(client.status()["ok"])
            held.append(client)
        self.assertEqual(held[-1].status()["connections"], protocol.CONNECTION_MAX_CLIENTS)

        surplus = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(surplus.close)
        surplus.settimeout(5.0)
        surplus.connect(str(handle.socket_path))
        answer = json.loads(surplus.recv(65536).split(b"\n")[0])
        self.assertEqual(answer["error"], protocol.ERROR_CONNECTION_LIMIT)
        self.assertEqual(answer["limit"], protocol.CONNECTION_MAX_CLIENTS)
        self.assertEqual(answer["connections"], protocol.CONNECTION_MAX_CLIENTS)
        self.assertEqual(surplus.recv(65536), b"", "a refused caller was left holding a connection")

        # A refusal is not a wound: the head is untouched and a slot given back is usable.
        self.assertTrue(_alive(handle.head_pid))
        held.pop().close()
        self._await(
            lambda: held[-1].status()["connections"] == protocol.CONNECTION_MAX_CLIENTS - 1,
            message="the supervisor never noticed a caller leaving",
        )
        replacement = handle.connect()
        self.addCleanup(replacement.close)
        self.assertTrue(replacement.status()["ok"])

    def _await_event(self, client: SupervisorClient, marker: bytes, *, timeout: float = 8.0) -> None:
        seen = bytearray()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            event = client.next_event(0.5)
            if event is None:
                continue
            seen += event.get("bytes_data") or b""
            if marker in seen:
                return
        self.fail(f"{marker!r} never arrived on the attached stream; saw {bytes(seen[-300:])!r}")

    # -- the socket itself -----------------------------------------------------------------

    def test_the_socket_is_owner_only_at_a_predictable_path(self) -> None:
        handle = self._start(run_id="socket")
        self.assertEqual(handle.socket_path, self.root / "socket" / protocol.SOCKET_NAME)
        self.assertEqual(handle.socket_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(handle.run_dir.stat().st_mode & 0o777, 0o700)

    def test_a_failure_after_the_lock_is_named_rather_than_waited_out(self) -> None:
        """A supervisor that dies on the way up leaves a reason, not a socket file and a timeout.

        The run directory is arranged so that the supervisor fails *after* it has taken the lock
        and bound the socket: a directory where its own pid file goes. What a launcher must get
        back is the named failure, quickly, and a run directory with no address left in it.
        """
        run_dir = protocol.run_dir_for(self.root, "half-up")
        run_dir.mkdir(parents=True)
        (run_dir / protocol.SUPERVISOR_PID_NAME).mkdir()
        started = time.monotonic()
        with self.assertRaises(LocalPtySpawnError) as caught:
            spawn_head(
                root=self.root,
                run_id="half-up",
                role="worker",
                task="ummanu-1463",
                command=CHILD_COMMAND,
                timeout=20.0,
            )
        self.assertEqual(caught.exception.reason, "startup_failed")
        self.assertLess(time.monotonic() - started, 15.0, "the launcher waited out its timeout instead")
        self.assertFalse((run_dir / protocol.SOCKET_NAME).exists(), "an address outlived the run")

    def test_a_failure_once_the_run_is_up_is_not_called_a_startup_failure(self) -> None:
        """A head that worked for an hour and then lost its supervisor did not fail to start.

        The two failures are different facts about a run directory and are named differently, so a
        reader is never told a run never started when what happened is that it ended badly. The
        supervisor knows which it is from whether `run.started` was ever written.
        """
        self.assertEqual(
            supervisor_module.failure_of(False)[:2],
            (protocol.STARTUP_ERROR_NAME, supervisor_module.START_FAILED),
        )
        self.assertEqual(
            supervisor_module.failure_of(True)[:2],
            (protocol.SUPERVISOR_ERROR_NAME, supervisor_module.RUN_FAILED),
        )
        self.assertNotEqual(protocol.STARTUP_ERROR_NAME, protocol.SUPERVISOR_ERROR_NAME)
        self.assertNotEqual(supervisor_module.failure_of(True)[2], supervisor_module.failure_of(False)[2])

        # And a run that really is up is on the `started` side of that choice: it has said so in
        # the journal, and it has left neither failure file behind.
        handle = self._start(run_id="named-failure")
        self.assertTrue(handle.events().of_kind(RUN_STARTED))
        self.assertFalse((handle.run_dir / protocol.STARTUP_ERROR_NAME).exists())
        self.assertFalse((handle.run_dir / protocol.SUPERVISOR_ERROR_NAME).exists())

    def test_a_second_start_over_a_live_run_is_refused_rather_than_doubling_the_head(self) -> None:
        handle = self._start(run_id="double")
        with self.assertRaises(LocalPtySpawnError) as caught:
            spawn_head(
                root=self.root,
                run_id="double",
                role="worker",
                task="ummanu-1463",
                command=CHILD_COMMAND,
                timeout=10.0,
            )
        self.assertEqual(caught.exception.reason, "already_running")
        self.assertEqual(len(handle.events().of_kind(RUN_STARTED)), 1, "a second head came up for one run")
        self.assertTrue(_alive(handle.head_pid))

    def test_a_supervisor_killed_outright_hangs_its_head_up_and_leaves_only_debris(self) -> None:
        handle = self._start(run_id="orphan")
        client = self._client(handle)
        self._await_output(client, b"SIZE ")
        client.close()

        _kill(handle.supervisor_pid, signal.SIGKILL)
        self._await(lambda: not _alive(handle.supervisor_pid), message="the supervisor survived")
        self.assertTrue(handle.socket_path.exists(), "the debris this test is about is missing")
        # Closing the last master end of a pty hangs the terminal up, and the kernel sends SIGHUP
        # to the head's foreground group. So a killed supervisor does not leave a head running
        # behind it: the ownership is real in both directions.
        self._await(
            lambda: not _alive(handle.head_pid),
            timeout=10.0,
            message="the head survived its supervisor being killed",
        )

        restarted = self._start(run_id="orphan")
        self.assertNotEqual(restarted.head_pid, handle.head_pid)
        self.assertEqual(restarted.socket_path.stat().st_mode & 0o777, 0o600)
        events = restarted.events()
        self.assertTrue(events.ordered)
        self.assertEqual(len(events.of_kind(RUN_STARTED)), 2, "the journal restarted its sequence")

    def test_a_head_of_this_run_that_is_still_alive_refuses_a_second_one(self) -> None:
        """The guard that makes a restart over debris safe, with a real live process behind it.

        Modelled rather than orphaned on purpose: a head that survives its supervisor is exactly
        what the pty hangup above prevents, so the only deterministic way to hold the guard is to
        hand it a launch identity that names a process which really is alive — this one.
        """
        run_dir = protocol.run_dir_for(self.root, "still-alive")
        run_dir.mkdir(parents=True)
        (run_dir / protocol.PID_FILE_NAME).write_text(
            json.dumps(_identity_of(os.getpid(), "still-alive")), encoding="utf-8"
        )
        with self.assertRaises(LocalPtySpawnError) as caught:
            spawn_head(
                root=self.root,
                run_id="still-alive",
                role="worker",
                task="ummanu-1463",
                command=CHILD_COMMAND,
                timeout=10.0,
            )
        self.assertEqual(caught.exception.reason, "already_running")
        self.assertIn(str(os.getpid()), caught.exception.detail)
        self.assertFalse((run_dir / protocol.SOCKET_NAME).exists(), "the refusal bound a socket")
        self.assertEqual(read_events(run_dir / protocol.JOURNAL_NAME).events, ())

    # -- the journal -----------------------------------------------------------------------

    def test_the_journal_is_versioned_sequenced_and_readable_by_another_process(self) -> None:
        handle = self._start(run_id="journal")
        client = self._client(handle)
        self._await_output(client, b"SIZE ")
        self.assertTrue(client.send_input("hello\n", subject="a nudge")["ok"])
        self._await_output(client, b"ECHO hello")
        self._await(
            lambda: bool(handle.events().of_kind(TURN_FINISHED)),
            message="a quiet head never closed its turn",
        )
        # Read from a process that is not the supervisor, while the supervisor is still alive.
        reader = subprocess.run(
            [
                sys.executable,
                "-P",
                "-c",
                "import sys;sys.path.insert(0, sys.argv[1]);"
                "from ummanu.runtime.head.local_pty.journal import read_events;"
                "import json;result=read_events(sys.argv[2]);"
                "print(json.dumps({'kinds': list(result.kinds), 'ordered': result.ordered}))",
                str(REPO / "src"),
                str(handle.journal_path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        outside = json.loads(reader.stdout)
        self.assertTrue(outside["ordered"])
        self.assertEqual(
            outside["kinds"][:5],
            [RUN_STARTED, INPUT_ACCEPTED, TURN_STARTED, PROVIDER_PROGRESSED, TURN_FINISHED],
        )

        client.drain("observer")
        client.stop("observer")
        self._await(lambda: bool(handle.events().of_kind(RUN_EXITED)), message="no run.exited was written")
        result = handle.events()
        self.assertTrue(result.ordered)
        self.assertFalse(result.truncated_tail)
        self.assertEqual(result.malformed, 0)
        self.assertEqual([event["seq"] for event in result.events], list(range(1, len(result.events) + 1)))
        for event in result.events:
            self.assertEqual(event["schema_version"], JOURNAL_SCHEMA_VERSION)
            self.assertEqual(event["run_id"], "journal")
            self.assertIn(event["kind"], EVENT_KINDS)
        self.assertEqual(result.kinds[-3:], (DRAIN_REQUESTED, RUN_STOPPING, RUN_EXITED))

    def test_a_journal_torn_by_sigkill_stays_readable_and_ordered(self) -> None:
        handle = self._start(run_id="torn")
        client = self._client(handle)
        self._await_output(client, b"SIZE ")
        self.assertTrue(client.send_input("hello\n")["ok"])
        self._await_output(client, b"ECHO hello")

        _kill(handle.supervisor_pid, signal.SIGKILL)
        self._await(lambda: not _alive(handle.supervisor_pid), message="the supervisor survived")
        intact = read_events(handle.journal_path)
        self.assertTrue(intact.ordered)
        self.assertFalse(intact.truncated_tail)
        self.assertGreaterEqual(len(intact.events), 3)

        # A kill *during* a write leaves a record with no newline. Modelling it here is the only
        # way to make the timing deterministic; the property under test is the reader's.
        with open(handle.journal_path, "ab") as journal:
            journal.write(b'{"schema_version":1,"seq":99,"run_id":"torn","kind":"run.exi')
        torn = read_events(handle.journal_path)
        self.assertTrue(torn.truncated_tail)
        self.assertEqual(torn.events, intact.events)
        self.assertTrue(torn.ordered)
        self.assertEqual(torn.malformed, 0)

    def test_the_journal_refuses_a_kind_it_does_not_define(self) -> None:
        path = self.root / "hand-written.jsonl"
        with JournalWriter(path, "hand") as journal:
            journal.append(RUN_STARTED, head_pid=1)
            with self.assertRaises(JournalError):
                journal.append("provider.invented", detail="not a kind")
            with self.assertRaises(JournalError):
                journal.append(RUN_STARTED, seq=4)
        self.assertEqual(read_events(path).kinds, (RUN_STARTED,))

    def test_a_bounded_read_of_a_journal_reads_its_end_and_says_that_it_did(self) -> None:
        """secretary-1479: the cost of asking a journal what a head is doing is a named number.

        A supervised role's run directory is reused and its journal is append-only across every
        incarnation, so a reader that opens the whole file pays a cost that grows with the head's
        history. `read_tail` is bounded by `JOURNAL_TAIL_BYTES`, drops the record the bound cut in
        half rather than counting it as malformed, and says `partial_head` so that a reader knows
        the difference between "this journal has no drain in it" and "the window I read has none".
        """
        path = self.root / "long.jsonl"
        with JournalWriter(path, "long") as journal:
            journal.append(RUN_STARTED, head_pid=1)
            journal.append(DRAIN_REQUESTED, initiator="an incarnation that is over")
            while path.stat().st_size <= JOURNAL_TAIL_BYTES * 2:
                journal.append(PROVIDER_PROGRESSED, turn=1, output_bytes=64)
            journal.append(TURN_FINISHED, turn=1, reason="quiet", output_bytes=64)

        whole = read_events(path)
        tail = read_tail(path)

        self.assertGreater(path.stat().st_size, JOURNAL_TAIL_BYTES)
        self.assertFalse(whole.partial_head, "the unbounded read is the whole file")
        self.assertEqual(whole.kinds[0], RUN_STARTED)
        self.assertTrue(tail.partial_head, "a bounded read that began inside the file says so")
        self.assertEqual(tail.malformed, 0, "the record the bound cut is dropped, not counted")
        self.assertTrue(tail.ordered)
        self.assertEqual(tail.kinds[-1], TURN_FINISHED, "the end of the journal is what it reads")
        self.assertNotIn(RUN_STARTED, tail.kinds, "a bounded read is bounded")
        self.assertLess(len(tail.events), len(whole.events))
        self.assertLessEqual(
            sum(len(json.dumps(event, sort_keys=True, separators=(",", ":"))) + 1 for event in tail.events),
            JOURNAL_TAIL_BYTES,
            "the bound is the number it is declared to be",
        )
        self.assertEqual(
            [event["seq"] for event in whole.events][-len(tail.events) :],
            [event["seq"] for event in tail.events],
            "the window is the end of the same sequence, not a re-numbering of it",
        )
        with self.assertRaises(JournalError):
            read_tail(path, max_bytes=0)

    def test_a_bounded_read_stays_bounded_while_the_journal_is_being_appended_to(self) -> None:
        """secretary-1479: the bound is on the bytes read, and a live writer cannot lift it.

        The reader this bound exists for reads a journal its supervisor is still writing, so the
        end of the file it measured is not the end of the file it reads from: a read issued for
        "everything from here" reads through whatever landed in between, and a writer that keeps
        going makes that arbitrarily larger than the number the bound is named for. Counting the
        records that survived the parse cannot see this — they are the same records either way —
        so what is counted here is what `read` actually handed back.

        The append is made from inside the reader's own `seek`, which is the moment the race
        happens at: the size has been taken and the read has not been issued.
        """
        path = self.root / "appended.jsonl"
        with JournalWriter(path, "appended") as journal:
            journal.append(RUN_STARTED, head_pid=1)
            while path.stat().st_size <= JOURNAL_TAIL_BYTES * 2:
                journal.append(PROVIDER_PROGRESSED, turn=1, output_bytes=64)
            appended_from = int(path.stat().st_size)
            while path.stat().st_size <= appended_from + JOURNAL_TAIL_BYTES * 3:
                journal.append(PROVIDER_PROGRESSED, turn=2, output_bytes=64)
            latecomers = path.read_bytes()[appended_from:]
        truncate_to = appended_from
        with open(path, "r+b") as handle:
            handle.truncate(truncate_to)

        real_open = open
        read_bytes: list[int] = []

        class _AppendingHandle:
            """A file that grows by `latecomers` between the size probe and the read."""

            def __init__(self, inner) -> None:
                self._inner = inner
                self._appended = False

            def seek(self, *args: int) -> int:
                position = self._inner.seek(*args)
                if not self._appended and args[:2] != (0, os.SEEK_END):
                    self._appended = True
                    with real_open(path, "ab") as sink:
                        sink.write(latecomers)
                return position

            def read(self, *args: int) -> bytes:
                data = self._inner.read(*args)
                read_bytes.append(len(data))
                return data

            def __enter__(self) -> _AppendingHandle:
                return self

            def __exit__(self, *exc_info: object) -> None:
                self._inner.close()

        def opening(*args: object, **kwargs: object) -> _AppendingHandle:
            return _AppendingHandle(real_open(*args, **kwargs))

        with mock.patch.object(journal_module, "open", opening, create=True):
            result = read_tail(path)

        self.assertGreater(len(latecomers), JOURNAL_TAIL_BYTES, "the append is too small to show it")
        self.assertGreater(path.stat().st_size, truncate_to, "the append never happened")
        self.assertTrue(read_bytes, "the read was never made")
        self.assertLessEqual(
            sum(read_bytes),
            JOURNAL_TAIL_BYTES,
            "a writer appending during the read lifted the bound the read is named for",
        )
        self.assertTrue(result.partial_head, "this is still a bounded read of a long journal")
        self.assertTrue(result.ordered)

    # -- identity --------------------------------------------------------------------------

    def test_the_head_carries_the_existing_launch_identity_and_the_watchdog_reads_it(self) -> None:
        handle = self._start(run_id="identity")
        record = handle.identity()
        self.assertEqual(record["pid"], handle.head_pid, "the identity belongs to the head")
        for name in ("pid", "boot_id", "proc_starttime_ticks", "run_id", "role", "task"):
            self.assertTrue(str(record.get(name) or ""), f"{name} is missing from the identity")
        self.assertEqual(record["run_id"], "identity")
        self.assertEqual(record["role"], "worker")
        self.assertEqual(record["task"], "ummanu-1463")

        expected = {"run_id": "identity", "role": "worker", "task": "ummanu-1463"}
        status = head_process_status(str(handle.pid_file), expected=expected)
        self.assertEqual(status["state"], HEARTBEAT_LIVE_MATCH, status)
        self.assertEqual(status["pid"], handle.head_pid)

        foreign = head_process_status(str(handle.pid_file), expected={**expected, "run_id": "somebody-else"})
        self.assertNotEqual(foreign["state"], HEARTBEAT_LIVE_MATCH)

        client = self._client(handle)
        client.stop("observer")
        self._await(lambda: not _alive(handle.head_pid), message="the head never stopped")
        # Retain the successful native observation: a fresh read can be inconclusive during
        # exit/reaping, even after the watchdog has classified the head as dead.
        def watchdog_observes_dead() -> bool:
            nonlocal status
            status = head_process_status(str(handle.pid_file), expected=expected)
            return status["state"] == HEARTBEAT_DEAD

        self._await(
            watchdog_observes_dead,
            message="the watchdog never classified the stopped head as dead",
        )
        self.assertEqual(status["state"], HEARTBEAT_DEAD, status)
        self.assertTrue(status["known"], status)
        self.assertFalse(status["alive"], status)
        self.assertFalse(status["match"], status)
        self.assertEqual(status["pid"], handle.head_pid, status)

    # -- endings ---------------------------------------------------------------------------

    def test_a_heads_own_exit_is_recorded_with_its_code_and_frees_the_socket(self) -> None:
        handle = self._start(run_id="exit-code")
        client = self._client(handle)
        self._await_output(client, b"SIZE ")
        self.assertTrue(client.send_input("exit 7\n")["ok"])

        self._await(
            lambda: bool(handle.events().of_kind(RUN_EXITED)),
            timeout=15.0,
            message="the supervisor never recorded the head's exit",
        )
        exited = handle.events().of_kind(RUN_EXITED)[-1]
        self.assertEqual(exited["exit_code"], 7)
        self.assertIsNone(exited["signal"])
        self.assertFalse(exited["stopping"], "an exit of the head's own is not a stop")
        self.assertEqual(exited["head_pid"], handle.head_pid)

        self._await(
            lambda: not _alive(handle.supervisor_pid),
            timeout=15.0,
            message="the supervisor held the socket after its head died",
        )
        self.assertFalse(handle.socket_path.exists(), "a dead head left an address behind")
        self.assertFalse((handle.run_dir / protocol.SUPERVISOR_PID_NAME).exists())

    def test_a_stop_is_recorded_as_a_signal_and_the_head_is_not_left_behind(self) -> None:
        handle = self._start(
            run_id="stopped",
            command=f"{sys.executable} -u -c "
            "'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_DFL);"
            'print("UP",flush=True);time.sleep(600)\'',
        )
        client = self._client(handle)
        self._await_output(client, b"UP")
        self.assertTrue(client.drain("observer")["ok"])
        self.assertTrue(client.stop("observer")["ok"])

        self._await(
            lambda: bool(handle.events().of_kind(RUN_EXITED)),
            timeout=15.0,
            message="a stopped head was never reaped",
        )
        exited = handle.events().of_kind(RUN_EXITED)[-1]
        self.assertEqual(exited["signal"], int(signal.SIGTERM))
        self.assertIsNone(exited["exit_code"])
        self.assertTrue(exited["stopping"])
        self.assertEqual(handle.events().of_kind(RUN_STOPPING)[-1]["initiator"], "observer")
        self._await(lambda: not _alive(handle.head_pid), message="the stopped head survived")
        self._await(lambda: not _alive(handle.supervisor_pid), message="the supervisor survived")


    def test_spinner_redraws_fold_but_new_lines_progress_and_quiet_finishes(self) -> None:
        command = f"{sys.executable} -u {REPO / 'tests' / 'fixtures' / 'local_pty_spinner.py'}"
        handle = self._start(run_id="spinner", command=command, quiet_seconds=2.0)
        client = self._client(handle)
        self.assertTrue(client.send_input("begin\n")["ok"])
        self._await_output(client, b"14763 tokens", timeout=10.0)
        time.sleep(0.6)  # let the last spinner window flush before the child's new text
        before_new_text = len(handle.events().of_kind(PROVIDER_PROGRESSED))
        self._await(lambda: bool(handle.events().of_kind(RUN_EXITED)), timeout=15.0, message="spinner did not exit")
        events = handle.events()
        progress = events.of_kind(PROVIDER_PROGRESSED)
        self.assertLessEqual(len(progress), 5, "spinner redraws grew the journal")
        self.assertGreater(len(progress), before_new_text, "new text after the spinner did not progress")
        self.assertTrue(any(event.get("folded_windows", 0) > 0 for event in progress))
        self.assertEqual(len(events.of_kind(TURN_FINISHED)), 1)
        self.assertEqual(events.of_kind(TURN_FINISHED)[0]["reason"], "quiet")
        self.assertGreater(events.of_kind(TURN_FINISHED)[0]["output_bytes"], 400 * 20)


class SubstrateIsNotWiredInTests(unittest.TestCase):
    """This package is a substrate. Only the runtime backend and PO runner consume it.

    The dispatcher reaches it through `runtime.local_pty_head`; the PO service reaches it through
    `po.runner`. Both use the scoped lifecycle's supervisor, journal and cgroup contract. An import
    anywhere else creates a third lifecycle entry path, which this guard refuses.
    """

    def test_only_the_scoped_head_consumers_reach_for_it(self) -> None:
        package = REPO / "src" / "ummanu" / "runtime" / "head" / "local_pty"
        backend = REPO / "src" / "ummanu" / "runtime" / "local_pty_head.py"
        po_runner = REPO / "src" / "ummanu" / "po" / "runner.py"
        substrate = "ummanu.runtime.head.local_pty"
        offenders = []
        for path in (REPO / "src").rglob("*.py"):
            if package in path.parents or path in (backend, po_runner):
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                    names = [node.module]
                if any(name == substrate or name.startswith(substrate + ".") for name in names):
                    offenders.append(str(path.relative_to(REPO)))
        self.assertEqual(offenders, [], "the substrate is reached outside its runtime and PO consumers")

    def test_the_substrate_implements_none_of_the_six_verbs_as_a_boundary(self) -> None:
        """Prose about `HeadRuntime` is fine; an implementation of it is what this card excludes."""
        package = REPO / "src" / "ummanu" / "runtime" / "head" / "local_pty"
        verbs = {"start", "deliver", "observe", "request_drain", "attach"}
        for path in sorted(package.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    self.assertNotIn(
                        "runtime",
                        (node.module or "").split("."),
                        f"{path.name} imports the boundary this card does not implement",
                    )
                if isinstance(node, ast.ClassDef):
                    methods = {
                        child.name
                        for child in node.body
                        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                    }
                    self.assertFalse(
                        verbs.issubset(methods),
                        f"{path.name}:{node.name} wears the six verbs already",
                    )


if __name__ == "__main__":
    unittest.main()
