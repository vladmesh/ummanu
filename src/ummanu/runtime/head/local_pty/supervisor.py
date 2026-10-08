"""The process that owns one head: its pty, its socket, its journal and its ending.

The supervisor is the answer to a question the sprint's first card exists to settle — can a
process the product starts itself outlive the dispatcher tick that started it and stay
addressable afterwards. It does four things and refuses to do a fifth:

  * it **starts the head on a pty of its own**, in a new session with the pty as its controlling
    terminal. A signal sent to the dispatcher's process group therefore cannot reach it, and an
    interactive adapter gets the terminal it expects, including `SIGWINCH` when the size changes.
    The terminal is sized and taken out of canonical mode before the head exists, so the head never
    observes a half-configured one;
  * it **holds the head's addressable surface**: a Unix socket at a predictable path, owner-only,
    with bounded input, bounded output and bounded attach. Every bound refuses by name, and no
    request on that socket ever waits for the head: a delivery is admitted or refused on the spot
    and then written by this loop, so a head that has stopped reading its terminal changes what
    `status` says about the delivery and changes nothing about how long anybody is answered in;
  * it **narrates the run** into the versioned append-only journal beside the socket;
  * it **reaps the head**, tells its own death apart from the head's, and writes `run.exited` with
    the exit code or the signal. When the head is gone the socket goes with it: nothing holds an
    address for a process that no longer exists;
  * it does **not** implement `HeadRuntime`. It is a substrate, and the six verbs are a separate
    piece of work built on the surface above.

The head's identity is not this process's business either. The head's command is wrapped by
`with_pid_heartbeat` here and nowhere else — a caller hands this process the bare head command — so
the record is written by the head's own process and is the same launch identity
`ummanu.dispatch.watchdog` already reads. It goes under `head.pid` in the run directory, or at
the `pid_file` the launcher designated when the launcher reads the head's liveness at a path of its
own (the dispatcher's watchdog heartbeat, secretary-1698).
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import pty
import re
import select
import selectors
import signal
import socket
import struct
import sys
import termios
import time
import traceback
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

from ..command import with_pid_heartbeat
from ..memory import OOM_STREAM_ENV, MemoryScopeError, ScopeEvidence, read_oom_victim
from . import protocol
from .journal import (
    DRAIN_REQUESTED,
    INPUT_ACCEPTED,
    PROVIDER_PROGRESSED,
    RUN_EXITED,
    RUN_STARTED,
    RUN_STOPPING,
    SCOPE_BOUND,
    TURN_FINISHED,
    TURN_STARTED,
    JournalWriter,
)
from .scoped_lifecycle import ScopedHeadLifecycle
from .screen import ScreenModel

#: A turn is over when the head has said nothing for this long. The substrate cannot see a
#: provider's end-of-turn marker, so it records the observable fact: the head went quiet.
TURN_QUIET_SECONDS = 2.0
#: Output is considered for progress at this interval; repeated screen content is folded.
PROGRESS_COALESCE_SECONDS = 0.5
#: Maximum distinct normalized lines remembered during one turn.
PROGRESS_SEEN_LINES_MAX = 4096
#: How long a stopping head is given before the signal is escalated.
STOP_GRACE_SECONDS = 5.0
#: Loop resolution: bounds how late a quiet turn or a stop deadline is noticed.
LOOP_TICK_SECONDS = 0.1
#: How long to keep flushing last frames to attached clients before closing the socket.
FAREWELL_SECONDS = 0.5
_READ_CHUNK = 65536
#: Indices into the list `termios.tcgetattr` returns.
_IFLAG = 0
_LFLAG = 3
_CC = 6

EXIT_OK = 0
EXIT_STARTUP_FAILED = 2
EXIT_ALREADY_RUNNING = 3
#: A run that was up and then lost its supervisor; distinct from a startup failure.
EXIT_RUN_FAILED = 4

START_ALREADY_RUNNING = "already_running"
START_FAILED = "startup_failed"
RUN_FAILED = "run_failed"

_SPINNER = re.compile(r"[\u2700-\u27bf\u2800-\u28ff\u25a0-\u25ff\u2022\u00b7]|(?<!\w)\*(?!\w)")
_DIGITS = re.compile(r"\d+")


def _progress_lines(lines: Iterable[str]) -> set[str]:
    """Normalize visible screen lines, never unplaced fragments of the PTY byte stream."""
    return {line for raw in lines if (line := " ".join(_DIGITS.sub("#", _SPINNER.sub("", raw)).split()))}


class SupervisorStartupError(RuntimeError):
    """The supervisor could not take ownership of this run, and took none of it."""

    def __init__(self, reason: str, detail: str, *, exit_code: int = EXIT_STARTUP_FAILED) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.exit_code = exit_code


class _Client:
    """One caller on the socket, and everything the supervisor owes it or withholds from it."""

    __slots__ = ("conn", "inbox", "pending", "attached", "dropped", "closing", "overflowed")

    def __init__(self, conn: socket.socket) -> None:
        self.conn = conn
        self.inbox = bytearray()
        self.pending = bytearray()
        self.attached = False
        self.dropped = 0
        self.closing = False
        self.overflowed = False


class _Delivery:
    """One admitted payload on its way to the head's terminal.

    Outlives the admitting request: the loop writes bytes as the head takes them, and progress is
    state reported by `status` and journaled at the end, never a caller held on the wire.
    """

    __slots__ = ("id", "payload", "subject", "written", "deadline", "state", "why", "seconds")

    def __init__(self, identifier: int, payload: bytes, subject: str, seconds: float) -> None:
        self.id = identifier
        self.payload = payload
        self.subject = subject
        self.written = 0
        self.seconds = seconds
        self.deadline = time.monotonic() + seconds
        self.state = protocol.DELIVERY_IN_FLIGHT
        self.why = ""

    @property
    def size(self) -> int:
        return len(self.payload)

    @property
    def in_flight(self) -> bool:
        return self.state == protocol.DELIVERY_IN_FLIGHT

    def view(self) -> dict[str, Any]:
        """What a reader is told about this delivery in any state."""
        return {
            "id": self.id,
            "state": self.state,
            "size_bytes": self.size,
            "written_bytes": self.written,
            "complete": self.state == protocol.DELIVERY_COMPLETE,
            "subject": self.subject,
            "timeout_seconds": self.seconds,
            "detail": protocol.delivery_detail(self.state, self.size, self.written, self.why, self.seconds),
        }


class Supervisor:
    """One run's owner. Constructed only in the process that will be the supervisor."""

    def __init__(
        self,
        *,
        run_dir: Path,
        run_id: str,
        role: str,
        task: str,
        command: str,
        rows: int = 24,
        cols: int = 80,
        term: str = "xterm-256color",
        quiet_seconds: float = TURN_QUIET_SECONDS,
        delivery_seconds: float = protocol.INPUT_DELIVERY_SECONDS,
        pid_file: str | os.PathLike[str] = "",
        memory_limit_mib: int | None = None,
    ) -> None:
        self.run_dir = Path(run_dir)
        self.run_id = run_id
        self.role = role
        self.task = task
        self.command = command
        self.rows = max(1, int(rows))
        self.cols = max(1, int(cols))
        self.term = term
        self.quiet_seconds = float(quiet_seconds)
        self.delivery_seconds = float(delivery_seconds)

        self.socket_path = protocol.socket_path_for(self.run_dir)
        self.journal_path = self.run_dir / protocol.JOURNAL_NAME
        self.pid_file = Path(pid_file) if pid_file else self.run_dir / protocol.PID_FILE_NAME
        self.memory_limit_mib = memory_limit_mib

        self._lock_fd = -1
        self._listener: socket.socket | None = None
        self._selector = selectors.DefaultSelector()
        self._clients: dict[socket.socket, _Client] = {}
        self._journal: JournalWriter | None = None

        self._master = -1
        self._head_pid = 0
        self._head_status: int | None = None
        self._memory_lifecycle = ScopedHeadLifecycle(run_id, memory_limit_mib) if memory_limit_mib is not None else None
        self._memory_evidence: ScopeEvidence | None = None
        self._oom_victim: dict[str, int] | None = None
        self._oom_stream = -1

        self._output = bytearray()
        self._output_dropped = 0
        self._output_total = 0
        self._screen = ScreenModel(self.rows, self.cols)

        self._turn_open = False
        self._turn_id = 0
        self._turn_bytes = 0
        self._last_output_at = 0.0
        # Monotonic: when the head last printed, or when this supervisor began if it has not yet.
        # A turn's start is not output, so this is kept apart from `_last_output_at`.
        self._printed_mono = time.monotonic()
        self._progress_bytes = 0
        self._progress_at = 0.0
        self._progress_window_bytes = 0
        self._progress_seen: dict[bytes, None] = {}
        self._progress_visible: set[bytes] = set()
        self._folded_windows = 0

        self._delivery: _Delivery | None = None
        self._delivery_seq = 0

        self.started = False
        self._draining = False
        self._stopping = False
        self._stop_deadline = 0.0
        self._signalled = 0
        self._wakeup_read = -1
        self._wakeup_write = -1

    # -- startup ---------------------------------------------------------------------------

    def claim(self) -> None:
        """Take exclusive ownership of the run directory, or refuse without touching anything.

        A live supervisor holds the lock, so a second start is refused rather than binding a second
        socket and head under the same run id. Only with the lock held is a leftover socket file
        treated as debris and removed.
        """
        self.run_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.run_dir, 0o700)
        lock_path = self.run_dir / protocol.SUPERVISOR_LOCK_NAME
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        os.set_inheritable(fd, False)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise SupervisorStartupError(
                START_ALREADY_RUNNING,
                f"another supervisor already owns {self.run_dir} ({exc.strerror})",
                exit_code=EXIT_ALREADY_RUNNING,
            ) from exc
        self._lock_fd = fd
        os.write(fd, f"{os.getpid()}\n".encode())
        self._refuse_a_second_head()
        self._bind()

    def _refuse_a_second_head(self) -> None:
        """Refuse to start beside a still-live head of this run.

        The lock proves no other supervisor owns the run, not that no head is alive: a `SIGKILL`ed
        supervisor leaves an orphaned head. Reads the head's launch identity (as the watchdog does)
        and refuses only on the full identity triple, so a recycled pid cannot fence a run out.
        """
        alive = _live_head(self.pid_file, self.run_id)
        if alive:
            raise SupervisorStartupError(
                START_ALREADY_RUNNING,
                f"head {alive} of run {self.run_id} is still alive; refusing to start a second "
                f"head for the same run",
                exit_code=EXIT_ALREADY_RUNNING,
            )

    def _bind(self) -> None:
        if self.socket_path.exists():
            if self._socket_answers():
                raise SupervisorStartupError(
                    START_ALREADY_RUNNING,
                    f"{self.socket_path} still answers; refusing to start a second head",
                    exit_code=EXIT_ALREADY_RUNNING,
                )
            self.socket_path.unlink()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        previous = os.umask(0o077)
        try:
            listener.bind(str(self.socket_path))
        finally:
            os.umask(previous)
        os.chmod(self.socket_path, 0o600)
        listener.listen(protocol.CONNECTION_MAX_CLIENTS + 4)
        listener.setblocking(False)
        self._listener = listener

    def _socket_answers(self) -> bool:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.5)
        try:
            probe.connect(str(self.socket_path))
        except OSError:
            return False
        finally:
            probe.close()
        return True

    def _head_argv(self) -> list[str]:
        identity = {"run_id": self.run_id, "role": self.role, "task": self.task}
        wrapped = with_pid_heartbeat(
            self.command, str(self.pid_file), identity=identity,
            in_process=self._memory_lifecycle is not None,
        )
        return ["/bin/sh", "-c", wrapped]

    def start_head(self) -> int:
        """Fork the head onto its own pty: new session, controlling terminal, launch identity.

        `pty.fork` written out so the terminal's size and line discipline are set on the slave
        before the fork: the head never sees a 0x0 size or the default discipline. Everything this
        process holds is close-on-exec, so the head's `exec` drops the socket, lock and journal.
        """
        argv = self._head_argv()
        environment = dict(os.environ)
        environment["TERM"] = self.term
        master, slave = pty.openpty()
        self._prepare_terminal(slave)
        ready_read, ready_write = os.pipe2(os.O_CLOEXEC) if self._memory_lifecycle is not None else (-1, -1)
        go_read, go_write = os.pipe2(os.O_CLOEXEC) if self._memory_lifecycle is not None else (-1, -1)
        pid = os.fork()
        if pid == 0:  # pragma: no cover - the child never returns to the test process
            try:
                if self._memory_lifecycle is not None:
                    os.close(self._oom_stream)
                    environment.pop(OOM_STREAM_ENV, None)
                    os.close(ready_read)
                    os.close(go_write)
                    # Reserve the new PID while still OOM-protected; the parent drops earlier kernel
                    # records before allowing this incarnation to execute.
                    os.write(ready_write, b"R")
                    if os.read(go_read, 1) != b"1":
                        os._exit(127)
                    os.close(go_read)
                    # Only the supervisor is OOM-protected; head descendants get the ordinary score
                    # and are included in a group OOM kill.
                    Path("/proc/self/oom_score_adj").write_text("0\n", encoding="ascii")
                    os.write(ready_write, b"1")
                    os.close(ready_write)
                os.close(master)
                os.setsid()
                fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
                for target in (0, 1, 2):
                    os.dup2(slave, target)
                if slave > 2:
                    os.close(slave)
                signal.set_wakeup_fd(-1)
                for number in (signal.SIGINT, signal.SIGTERM, signal.SIGWINCH, signal.SIGHUP):
                    signal.signal(number, signal.SIG_DFL)
                os.execvpe(argv[0], argv, environment)
            except BaseException:  # noqa: BLE001 - a forked child must exit on every failure
                os._exit(127)
        os.close(slave)
        self._head_pid = pid
        self._master = master
        os.set_blocking(master, False)
        os.set_inheritable(master, False)
        if ready_read >= 0:
            os.close(ready_write)
            os.close(go_read)
            try:
                readable, _, _ = select.select([ready_read], [], [], 5.0)
                if not readable or os.read(ready_read, 1) != b"R":
                    raise SupervisorStartupError("memory_scope_unavailable", "head launch barrier failed")
                os.lseek(self._oom_stream, 0, os.SEEK_END)
                os.write(go_write, b"1")
                readable, _, _ = select.select([ready_read], [], [], 5.0)
                if not readable or os.read(ready_read, 1) != b"1":
                    raise SupervisorStartupError(
                        "memory_scope_unavailable", "the head could not clear inherited OOM protection"
                    )
            finally:
                os.close(ready_read)
                os.close(go_write)
        return pid

    def _prepare_terminal(self, slave: int) -> None:
        """Size the pty and set the line discipline the head inherits, before the head runs.

        Canonical mode caps a line at 4095 bytes and silently discards the rest, which would make
        the declared 64 KiB input limit false. Non-canonical mode gives `EAGAIN` back-pressure, so
        any payload up to the limit arrives whole. Settings:

          * off: `ICANON`, all echo flags, `IEXTEN` (they buffer, cap and re-emit input);
          * off: `IXON` (it would eat `0x11`/`0x13`, and a stray `0x13` freezes the head's output);
          * on: `ICRNL`, the only input translation left (CR reaches the head as newline);
          * on: `ISIG` (`^C` still interrupts) and `OPOST` (output line endings).

        An adapter may set its own mode afterwards; this is only the inherited default.
        """
        packed = struct.pack("HHHH", self.rows, self.cols, 0, 0)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, packed)
        attributes = termios.tcgetattr(slave)
        attributes[_IFLAG] &= ~termios.IXON
        attributes[_LFLAG] &= ~(
            termios.ICANON | termios.ECHO | termios.ECHOE | termios.ECHOK | termios.ECHONL | termios.IEXTEN
        )
        # With ICANON off a read returns as soon as one byte is there, and never waits on a timer.
        attributes[_CC][termios.VMIN] = 1
        attributes[_CC][termios.VTIME] = 0
        termios.tcsetattr(slave, termios.TCSANOW, attributes)

    def set_winsize(self, rows: int, cols: int) -> None:
        """Set the pty's size. The kernel delivers `SIGWINCH` to the head from here."""
        self.rows = max(1, int(rows))
        self.cols = max(1, int(cols))
        self._screen.resize(self.rows, self.cols)
        if self._master < 0:
            return
        packed = struct.pack("HHHH", self.rows, self.cols, 0, 0)
        fcntl.ioctl(self._master, termios.TIOCSWINSZ, packed)

    # -- the loop --------------------------------------------------------------------------

    def run(self) -> int:
        """Own the head until it ends, and return the exit code.

        Everything after `claim` is under one `finally`, so a failed bring-up releases the socket
        and lock and leaves a named refusal rather than unanswering debris.
        """
        try:
            try:
                self._begin()
            except BaseException:
                # A failed bring-up must end the forked head: closing a pty only requests SIGHUP.
                self._abandon_head()
                raise
            while self._head_status is None:
                for key, mask in self._selector.select(LOOP_TICK_SECONDS):
                    self._dispatch(key, mask)
                self._tick()
            return self._finish()
        finally:
            self._shutdown()

    def _begin(self) -> None:
        """Bring the head up and record it, in the order a run-directory reader needs."""
        self._journal = JournalWriter(self.journal_path, self.run_id).open()
        self._install_signals()
        self._prepare_memory_scope()
        if self._memory_lifecycle is not None:
            try:
                with self._memory_lifecycle.attest_launch(directory=self.run_dir, role=self.role,
                                                         task=self.task, workspace=os.getcwd()) as binding:
                    self._append(SCOPE_BOUND, binding=binding)
                    descriptor = os.open(self.run_dir, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                    self.start_head()
            except MemoryScopeError as exc:
                raise SupervisorStartupError("memory_scope_unavailable", str(exc)) from exc
        else:
            self.start_head()
        (self.run_dir / protocol.SUPERVISOR_PID_NAME).write_text(f"{os.getpid()}\n", "utf-8")
        self._append(
            RUN_STARTED,
            head_pid=self._head_pid,
            supervisor_pid=os.getpid(),
            command=self.command,
            rows=self.rows,
            cols=self.cols,
            role=self.role,
            task=self.task,
            pid_file=str(self.pid_file),
            socket_path=str(self.socket_path),
            input_limit_bytes=protocol.INPUT_MAX_BYTES,
            output_buffer_bytes=protocol.OUTPUT_BUFFER_BYTES,
            attach_limit=protocol.ATTACH_MAX_CLIENTS,
        )
        self.started = True
        assert self._listener is not None
        self._selector.register(self._listener, selectors.EVENT_READ, "listener")
        self._selector.register(self._master, selectors.EVENT_READ, "master")
        self._selector.register(self._wakeup_read, selectors.EVENT_READ, "wakeup")

    def _prepare_memory_scope(self) -> None:
        """Refuse a scoped launch until the limit is observable on this supervisor itself."""
        if self._memory_lifecycle is None:
            return
        self._memory_evidence = None
        try:
            self._memory_evidence = self._memory_lifecycle.verify_self()
            try:
                self._oom_stream = int(os.environ.pop(OOM_STREAM_ENV))
                os.fstat(self._oom_stream)
                os.set_inheritable(self._oom_stream, False)
            except (KeyError, ValueError, OSError) as exc:
                raise MemoryScopeError("kernel OOM victim stream is unavailable") from exc
        except MemoryScopeError as exc:
            raise SupervisorStartupError("memory_scope_unavailable", str(exc)) from exc

    def _abandon_head(self) -> None:
        """End a head forked by a failed `_begin`: SIGTERM the group, grace, SIGKILL, then reap.

        No loop exists to escalate through; reaping here avoids leaving a zombie after the refusal.
        """
        if self._head_pid <= 0 or self._head_status is not None:
            return
        self._signal_head(signal.SIGTERM)
        deadline = time.monotonic() + STOP_GRACE_SECONDS
        while time.monotonic() < deadline:
            self._reap()
            if self._head_status is not None:
                return
            time.sleep(LOOP_TICK_SECONDS)
        self._signal_head(signal.SIGKILL)
        try:
            _pid, status = os.waitpid(self._head_pid, 0)
        except (ChildProcessError, OSError):
            self._head_status = 0
            return
        self._head_status = status

    def _dispatch(self, key: selectors.SelectorKey, mask: int) -> None:
        data = key.data
        if data == "listener":
            self._accept()
        elif data == "master":
            if mask & selectors.EVENT_READ:
                self._read_head()
            if mask & selectors.EVENT_WRITE:
                # The terminal has room for more of the admitted payload. Reading first means a head
                # blocked writing its own output cannot deadlock against a pending payload.
                self._pump_delivery()
        elif data == "wakeup":
            try:
                os.read(self._wakeup_read, 4096)
            except OSError:
                pass
        else:
            client = data
            if mask & selectors.EVENT_WRITE:
                self._flush(client)
            if mask & selectors.EVENT_READ:
                self._read_client(client)

    def _tick(self) -> None:
        now = time.time()
        if self._signalled and not self._stopping:
            self._begin_stop(f"signal:{self._signalled}", signal.SIGTERM)
        if self._turn_open and self._last_output_at and now - self._last_output_at >= self.quiet_seconds:
            self._flush_progress()
            self._append(
                TURN_FINISHED,
                turn=self._turn_id,
                reason="quiet",
                quiet_seconds=self.quiet_seconds,
                output_bytes=self._turn_bytes,
                folded_windows=self._folded_windows,
            )
            self._turn_open = False
        elif (
            self._turn_open
            and self._progress_window_bytes
            and now - self._progress_at >= PROGRESS_COALESCE_SECONDS
        ):
            self._flush_progress()
        # A terminal that never becomes writable raises no event, so the tick enforces the bound.
        self._expire_delivery()
        if self._stopping and self._stop_deadline and now >= self._stop_deadline:
            self._stop_deadline = 0.0
            self._signal_head(signal.SIGKILL)
        self._reap()

    def _reap(self) -> None:
        if self._head_pid <= 0 or self._head_status is not None:
            return
        try:
            if self._memory_evidence is not None:
                exited = os.waitid(os.P_PID, self._head_pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                if exited is None:
                    return
                # Reserve the zombie's PID until the kernel kill evidence is consumed.
                self._oom_victim = read_oom_victim(self._oom_stream, self._head_pid)
            pid, status = os.waitpid(self._head_pid, os.WNOHANG)
        except ChildProcessError:
            self._head_status = 0
            return
        if pid == self._head_pid:
            self._head_status = status

    # -- the head's pty --------------------------------------------------------------------

    def _read_head(self) -> None:
        while True:
            try:
                chunk = os.read(self._master, _READ_CHUNK)
            except BlockingIOError:
                return
            except OSError as exc:
                # A pty master reads EIO once the last slave end is gone (usually the head's exit).
                if exc.errno in (errno.EIO, errno.EBADF):
                    self._master_closed()
                    return
                raise
            if not chunk:
                self._master_closed()
                return
            self._record_output(chunk)

    def _master_closed(self) -> None:
        try:
            self._selector.unregister(self._master)
        except (KeyError, ValueError):
            pass
        self._finish_delivery(
            protocol.DELIVERY_FAILED, "the head's terminal was closed before the delivery finished"
        )
        # EIO means every slave end is closed, usually but not provably the head's exit (a head may
        # close its terminal and keep running), so the exit itself comes from `waitpid`.
        self._reap()

    def _record_output(self, chunk: bytes) -> None:
        self._screen.feed(chunk)
        self._output_total += len(chunk)
        self._printed_mono = time.monotonic()
        self._output += chunk
        if len(self._output) > protocol.OUTPUT_BUFFER_BYTES:
            excess = len(self._output) - protocol.OUTPUT_BUFFER_BYTES
            del self._output[:excess]
            self._output_dropped += excess
        self._last_output_at = time.time()
        if self._turn_open:
            self._turn_bytes += len(chunk)
            self._progress_bytes += len(chunk)
            self._progress_window_bytes += len(chunk)
            if not self._progress_at:
                self._progress_at = self._last_output_at
        for client in list(self._clients.values()):
            if client.attached:
                self._push_output(client, chunk)

    def _flush_progress(self) -> None:
        if not self._progress_window_bytes:
            return
        lines = _progress_lines(self._screen.lines())
        self._progress_window_bytes = 0
        self._progress_at = 0.0
        visible = {hashlib.blake2b(line.encode("utf-8"), digest_size=16).digest() for line in lines}
        # A screen may show more lines than the history cap; an evicted but still-visible line
        # must not be rediscovered on every spinner redraw.
        new = any(
            digest not in self._progress_seen and digest not in self._progress_visible for digest in visible
        )
        self._progress_visible = visible
        for digest in sorted(visible):
            if digest not in self._progress_seen:
                if len(self._progress_seen) >= PROGRESS_SEEN_LINES_MAX:
                    self._progress_seen.pop(next(iter(self._progress_seen)))
                self._progress_seen[digest] = None
        if not new:
            self._folded_windows += 1
            return
        self._append(
            PROVIDER_PROGRESSED,
            turn=self._turn_id,
            output_bytes=self._progress_bytes,
            total_output_bytes=self._output_total,
            dropped_bytes=self._output_dropped,
            folded_windows=self._folded_windows,
        )
        self._progress_bytes = 0
        self._folded_windows = 0

    # -- delivery: admitted by the socket, written by the loop -----------------------------

    def _admit(self, payload: bytes, subject: str) -> _Delivery:
        """Admit one payload and hand it to the loop."""
        self._delivery_seq += 1
        delivery = _Delivery(self._delivery_seq, payload, subject, self.delivery_seconds)
        self._delivery = delivery
        self._arm_delivery()
        return delivery

    def _arm_delivery(self) -> None:
        """Wake the loop when the head's terminal has room (pty master writable), not on a timer."""
        if self._master < 0:
            return
        try:
            self._selector.modify(self._master, selectors.EVENT_READ | selectors.EVENT_WRITE, "master")
        except (KeyError, ValueError):
            pass

    def _disarm_delivery(self) -> None:
        if self._master < 0:
            return
        try:
            self._selector.modify(self._master, selectors.EVENT_READ, "master")
        except (KeyError, ValueError):
            pass

    def _pump_delivery(self) -> None:
        """Write as much of the admitted payload as the terminal takes now, stopping at `EAGAIN`.

        Never waits for the head: a slow or stopped reader costs one non-blocking write per wake-up.
        """
        delivery = self._delivery
        if delivery is None or not delivery.in_flight:
            return
        if self._head_status is not None:
            self._finish_delivery(protocol.DELIVERY_FAILED, "the head exited")
            return
        while delivery.written < delivery.size:
            chunk = delivery.payload[delivery.written : delivery.written + _READ_CHUNK]
            try:
                delivery.written += os.write(self._master, chunk)
            except BlockingIOError:
                break
            except OSError as exc:
                self._finish_delivery(
                    protocol.DELIVERY_FAILED,
                    f"the head's terminal refused the write ({exc.strerror})",
                )
                return
        if delivery.written >= delivery.size:
            self._finish_delivery(protocol.DELIVERY_COMPLETE, "")
            return
        self._expire_delivery()

    def _expire_delivery(self) -> None:
        """Abandon a payload the head has not taken within the delivery bound, recording a stall."""
        delivery = self._delivery
        if delivery is None or not delivery.in_flight:
            return
        if self._head_status is not None:
            # A pty master stays writable after the head is gone, so the tick notices this.
            self._finish_delivery(protocol.DELIVERY_FAILED, "the head exited")
            return
        if time.monotonic() >= delivery.deadline:
            self._finish_delivery(protocol.DELIVERY_STALLED, "the head stopped reading its terminal")

    def _finish_delivery(self, state: str, why: str) -> None:
        """Close a delivery out and journal what actually reached the head's terminal.

        The only writer of `input.accepted`; `bytes` is what the kernel took. A delivery with zero
        bytes landed is still recorded but opens no turn. Retry after a stall (which leaves an
        irrevocable prefix) is the backend's `deliver` concern, not the substrate's.
        """
        delivery = self._delivery
        if delivery is None or not delivery.in_flight:
            return
        delivery.state = state
        delivery.why = why
        self._disarm_delivery()
        self._append(
            INPUT_ACCEPTED,
            bytes=delivery.written,
            offered_bytes=delivery.size,
            complete=state == protocol.DELIVERY_COMPLETE,
            delivery=delivery.id,
            state=state,
            subject=delivery.subject,
            detail=protocol.delivery_detail(state, delivery.size, delivery.written, why, delivery.seconds),
        )
        if delivery.written and not self._turn_open:
            self._turn_id += 1
            self._turn_open = True
            self._turn_bytes = 0
            self._last_output_at = time.time()
            self._progress_bytes = 0
            self._progress_window_bytes = 0
            self._progress_at = 0.0
            self._progress_seen.clear()
            self._progress_visible.clear()
            self._folded_windows = 0
            self._append(TURN_STARTED, turn=self._turn_id, subject=delivery.subject)

    def _delivery_view(self) -> dict[str, Any] | None:
        return self._delivery.view() if self._delivery is not None else None

    def _signal_head(self, number: int) -> None:
        if self._head_pid <= 0:
            return
        try:
            # The head leads its own session and process group, so its pid names its group.
            os.killpg(self._head_pid, number)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(self._head_pid, number)
            except OSError:
                pass

    # -- the socket ------------------------------------------------------------------------

    def _accept(self) -> None:
        assert self._listener is not None
        while True:
            try:
                conn, _ = self._listener.accept()
            except BlockingIOError:
                return
            except OSError:
                return
            if len(self._clients) >= protocol.CONNECTION_MAX_CLIENTS:
                self._refuse_connection(conn)
                continue
            conn.setblocking(False)
            client = _Client(conn)
            self._clients[conn] = client
            self._selector.register(conn, selectors.EVENT_READ, client)

    def _refuse_connection(self, conn: socket.socket) -> None:
        """Refuse a caller over the connection limit with a frame, then close."""
        try:
            conn.settimeout(0.2)
            conn.sendall(protocol.encode_frame(protocol.connection_refusal(len(self._clients))))
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _read_client(self, client: _Client) -> None:
        try:
            data = client.conn.recv(_READ_CHUNK)
        except BlockingIOError:
            return
        except OSError:
            self._close_client(client)
            return
        if not data:
            self._close_client(client)
            return
        client.inbox += data
        while True:
            index = client.inbox.find(b"\n")
            if index < 0:
                if len(client.inbox) > protocol.FRAME_MAX_BYTES:
                    self._send(
                        client,
                        {
                            "ok": False,
                            "error": protocol.ERROR_FRAME_TOO_LARGE,
                            "limit_bytes": protocol.FRAME_MAX_BYTES,
                            "size_bytes": len(client.inbox),
                            "detail": (
                                f"an unterminated request of at least {len(client.inbox)} bytes "
                                f"exceeds the {protocol.FRAME_MAX_BYTES}-byte frame limit"
                            ),
                        },
                    )
                    client.inbox.clear()
                    client.closing = True
                return
            line = bytes(client.inbox[:index])
            del client.inbox[: index + 1]
            self._handle(client, line)
            if client.closing:
                return

    def _handle(self, client: _Client, line: bytes) -> None:
        """Answer one request from state this process already holds.

        No handler asks the head anything, so a caller waits at most one loop pass.
        """
        try:
            request = protocol.decode_frame(line)
        except protocol.ProtocolError as exc:
            # Malformed bytes have no id, so the refusal carries none (uncorrelated).
            self._send(client, {"ok": False, "error": protocol.ERROR_MALFORMED, "detail": str(exc)})
            return
        request_id = request.get(protocol.REQUEST_ID)
        op = str(request.get("op") or "")
        handler = {
            protocol.OP_STATUS: self._op_status,
            protocol.OP_INPUT: self._op_input,
            protocol.OP_OUTPUT: self._op_output,
            protocol.OP_ATTACH: self._op_attach,
            protocol.OP_RESIZE: self._op_resize,
            protocol.OP_DRAIN: self._op_drain,
            protocol.OP_STOP: self._op_stop,
        }.get(op)
        if handler is None:
            self._answer(
                client,
                request_id,
                {
                    "ok": False,
                    "error": protocol.ERROR_UNKNOWN_OP,
                    "detail": f"unknown op {op!r} (known: {', '.join(protocol.OPS)})",
                },
            )
            return
        try:
            self._answer(client, request_id, handler(client, request))
        except protocol.ProtocolError as exc:
            self._answer(
                client,
                request_id,
                {"ok": False, "error": protocol.ERROR_MALFORMED, "detail": str(exc)},
            )

    def _answer(self, client: _Client, request_id: Any, payload: dict[str, Any]) -> None:
        """Send one answer carrying the request's id, so a stale answer cannot pass as a fresh one."""
        if isinstance(request_id, (str, int)) and not isinstance(request_id, bool):
            payload = {**payload, protocol.REQUEST_ID: request_id}
        self._send(client, payload)

    def _op_status(self, client: _Client, request: dict[str, Any]) -> dict[str, Any]:
        del client, request
        return {
            "ok": True,
            "run_id": self.run_id,
            "role": self.role,
            "task": self.task,
            "head_pid": self._head_pid,
            "supervisor_pid": os.getpid(),
            "alive": self._head_status is None,
            "draining": self._draining,
            "stopping": self._stopping,
            "turn_open": self._turn_open,
            "turn": self._turn_id,
            "delivery": self._delivery_view(),
            "rows": self.rows,
            "cols": self.cols,
            "journal_seq": self._journal.seq if self._journal else 0,
            "output_bytes": self._output_total,
            # How long the head has printed nothing, since its last output or, if it has printed
            # nothing yet, since this supervisor began: one status answers "is it settled".
            "output_idle_seconds": round(max(0.0, time.monotonic() - self._printed_mono), 3),
            "dropped_bytes": self._output_dropped,
            "attached": sum(1 for other in self._clients.values() if other.attached),
            "attach_limit": protocol.ATTACH_MAX_CLIENTS,
            "connections": len(self._clients),
            "connection_limit": protocol.CONNECTION_MAX_CLIENTS,
            "input_limit_bytes": protocol.INPUT_MAX_BYTES,
            "output_buffer_bytes": protocol.OUTPUT_BUFFER_BYTES,
        }

    def _op_input(self, client: _Client, request: dict[str, Any]) -> dict[str, Any]:
        """Admit one payload or refuse it by name, within this tick.

        Decided from held state only: head gone, admission closed, a delivery in flight, or over
        the limit. `ok` means accepted; what landed is in `status`'s `delivery` and the journal's
        `input.accepted`.
        """
        del client
        payload = protocol.decode_payload(request.get("data"))
        size = len(payload)
        if size > protocol.INPUT_MAX_BYTES:
            return protocol.input_refusal(size)
        if self._head_status is not None:
            return {"ok": False, "error": protocol.ERROR_HEAD_GONE, "detail": "the head has exited"}
        if self._draining:
            return {
                "ok": False,
                "error": protocol.ERROR_DRAINING,
                "detail": "this head's admission is closed; it takes no further input",
            }
        if self._delivery is not None and self._delivery.in_flight:
            return protocol.in_flight_refusal(self._delivery.view())
        delivery = self._admit(payload, str(request.get("subject") or ""))
        return {
            "ok": True,
            "accepted": True,
            "accepted_bytes": size,
            "delivery": delivery.view(),
            "turn": self._turn_id,
        }

    def _op_output(self, client: _Client, request: dict[str, Any]) -> dict[str, Any]:
        del client
        try:
            limit = int(request.get("max_bytes") or protocol.OUTPUT_BUFFER_BYTES)
        except (TypeError, ValueError) as exc:
            raise protocol.ProtocolError("max_bytes is a number") from exc
        limit = max(0, min(limit, protocol.OUTPUT_BUFFER_BYTES))
        tail = bytes(self._output[-limit:]) if limit else b""
        return {
            "ok": True,
            "data": protocol.encode_payload(tail),
            "bytes": len(tail),
            "total_bytes": self._output_total,
            "dropped_bytes": self._output_dropped + max(0, len(self._output) - len(tail)),
            "truncated": self._output_total > len(tail),
            "alive": self._head_status is None,
        }

    def _op_attach(self, client: _Client, request: dict[str, Any]) -> dict[str, Any]:
        del request
        if client.attached:
            return {"ok": True, "attached": True, "already": True}
        attached = sum(1 for other in self._clients.values() if other.attached)
        if attached >= protocol.ATTACH_MAX_CLIENTS:
            return {
                "ok": False,
                "error": protocol.ERROR_ATTACH_LIMIT,
                "limit": protocol.ATTACH_MAX_CLIENTS,
                "attached": attached,
                "detail": (
                    f"{attached} callers already hold this head's stream, which is the "
                    f"{protocol.ATTACH_MAX_CLIENTS}-attachment limit"
                ),
            }
        client.attached = True
        return {
            "ok": True,
            "attached": True,
            "data": protocol.encode_payload(bytes(self._output)),
            "dropped_bytes": self._output_dropped,
            "total_bytes": self._output_total,
        }

    def _op_resize(self, client: _Client, request: dict[str, Any]) -> dict[str, Any]:
        del client
        try:
            rows = int(request["rows"])
            cols = int(request["cols"])
        except (KeyError, TypeError, ValueError) as exc:
            raise protocol.ProtocolError("a resize names rows and cols") from exc
        if self._head_status is not None:
            return {"ok": False, "error": protocol.ERROR_HEAD_GONE, "detail": "the head has exited"}
        self.set_winsize(rows, cols)
        return {"ok": True, "rows": self.rows, "cols": self.cols}

    def _op_drain(self, client: _Client, request: dict[str, Any]) -> dict[str, Any]:
        del client
        initiator = str(request.get("initiator") or "client")
        if not self._draining:
            self._draining = True
            self._append(DRAIN_REQUESTED, initiator=initiator, turn_open=self._turn_open)
        return {"ok": True, "draining": True, "turn_open": self._turn_open}

    def _op_stop(self, client: _Client, request: dict[str, Any]) -> dict[str, Any]:
        del client
        initiator = str(request.get("initiator") or "client")
        name = str(request.get("signal") or "TERM").upper()
        number = (
            getattr(signal, f"SIG{name}", None) if not name.startswith("SIG") else getattr(signal, name, None)
        )
        if not isinstance(number, signal.Signals):
            raise protocol.ProtocolError(f"unknown signal {name!r}")
        if self._head_status is not None:
            return {"ok": False, "error": protocol.ERROR_HEAD_GONE, "detail": "the head has exited"}
        self._begin_stop(initiator, number)
        return {"ok": True, "stopping": True, "signal": int(number)}

    def _begin_stop(self, initiator: str, number: int) -> None:
        if self._stopping:
            return
        self._stopping = True
        if not self._draining:
            self._draining = True
            self._append(DRAIN_REQUESTED, initiator=initiator, turn_open=self._turn_open)
        self._append(RUN_STOPPING, initiator=initiator, signal=int(number), turn_open=self._turn_open)
        self._signal_head(number)
        self._stop_deadline = time.time() + STOP_GRACE_SECONDS

    # -- client plumbing -------------------------------------------------------------------

    def _send(self, client: _Client, payload: dict[str, Any]) -> None:
        client.pending += protocol.encode_frame(payload)
        self._flush(client)

    def _push_output(self, client: _Client, chunk: bytes) -> None:
        """Push a chunk to an attached client, or drop and count it when the client is backed up.

        Keeps a slow reader from growing the supervisor; the drop count is sent as its own event.
        """
        if len(client.pending) + len(chunk) > protocol.OUTPUT_BUFFER_BYTES:
            client.dropped += len(chunk)
            client.overflowed = True
            return
        self._announce_dropped(client)
        client.pending += protocol.encode_frame(
            {"event": protocol.EVENT_OUTPUT, "data": protocol.encode_payload(chunk)}
        )
        self._flush(client)

    def _announce_dropped(self, client: _Client) -> None:
        """Send a pending drop count before anything else.

        Also called at stream end, so an overflow on the last chunk is not lost.
        """
        if not client.overflowed:
            return
        client.pending += protocol.encode_frame({"event": protocol.EVENT_DROPPED, "bytes": client.dropped})
        client.overflowed = False

    def _flush(self, client: _Client) -> None:
        while client.pending:
            try:
                sent = client.conn.send(bytes(client.pending[:_READ_CHUNK]))
            except BlockingIOError:
                break
            except OSError:
                self._close_client(client)
                return
            del client.pending[:sent]
        if client.closing and not client.pending:
            self._close_client(client)
            return
        events = selectors.EVENT_READ | (selectors.EVENT_WRITE if client.pending else 0)
        try:
            self._selector.modify(client.conn, events, client)
        except (KeyError, ValueError):
            pass

    def _close_client(self, client: _Client) -> None:
        """Detach a caller; this is not an event in the head's life."""
        try:
            self._selector.unregister(client.conn)
        except (KeyError, ValueError):
            pass
        self._clients.pop(client.conn, None)
        try:
            client.conn.close()
        except OSError:
            pass

    # -- ending ----------------------------------------------------------------------------

    def _finish(self) -> int:
        status = int(self._head_status or 0)
        self._finish_delivery(protocol.DELIVERY_FAILED, "the head exited before the delivery finished")
        self._flush_progress()
        if self._turn_open:
            self._append(
                TURN_FINISHED,
                turn=self._turn_id,
                reason="head_exited",
                output_bytes=self._turn_bytes,
                folded_windows=self._folded_windows,
            )
            self._turn_open = False
        exited: dict[str, Any] = {
            "head_pid": self._head_pid,
            "output_bytes": self._output_total,
            "dropped_bytes": self._output_dropped,
            "stopping": self._stopping,
        }
        if self._memory_lifecycle is not None and self._memory_evidence is not None:
            exited.update(self._memory_lifecycle.exit_fields(
                status, self._memory_evidence, stopping=self._stopping,
                oom_victim=self._oom_victim,
            ))
        else:
            exited["signal"] = os.WTERMSIG(status) if os.WIFSIGNALED(status) else None
            exited["exit_code"] = os.WEXITSTATUS(status) if os.WIFEXITED(status) else None
        record = self._append(RUN_EXITED, **exited)
        for client in list(self._clients.values()):
            if client.attached:
                self._announce_dropped(client)
                client.pending += protocol.encode_frame({"event": protocol.EVENT_EXITED, "record": record})
            self._flush(client)
        deadline = time.time() + FAREWELL_SECONDS
        while time.time() < deadline and any(c.pending for c in self._clients.values()):
            for _key, _mask in self._selector.select(0.05):
                pass
            for client in list(self._clients.values()):
                self._flush(client)
        return EXIT_OK

    def _shutdown(self) -> None:
        """Release everything, leaving nothing addressable behind."""
        if self._oom_stream >= 0:
            os.close(self._oom_stream)
            self._oom_stream = -1
        if self._listener is not None:
            try:
                self._selector.unregister(self._listener)
            except (KeyError, ValueError):
                pass
            self._listener.close()
            self._listener = None
        for client in list(self._clients.values()):
            self._close_client(client)
        self.socket_path.unlink(missing_ok=True)
        (self.run_dir / protocol.SUPERVISOR_PID_NAME).unlink(missing_ok=True)
        if self._master >= 0:
            try:
                os.close(self._master)
            except OSError:
                pass
            self._master = -1
        if self._journal is not None:
            self._journal.close()
            self._journal = None
        self._selector.close()
        if self._lock_fd >= 0:
            os.close(self._lock_fd)
            self._lock_fd = -1

    def _append(self, kind: str, **fields: Any) -> dict[str, Any]:
        assert self._journal is not None
        return self._journal.append(kind, **fields)

    def _install_signals(self) -> None:
        read_fd, write_fd = os.pipe()
        os.set_blocking(read_fd, False)
        os.set_blocking(write_fd, False)
        os.set_inheritable(read_fd, False)
        os.set_inheritable(write_fd, False)
        self._wakeup_read, self._wakeup_write = read_fd, write_fd
        signal.set_wakeup_fd(write_fd)

        def remember(number: int, _frame: Any) -> None:
            self._signalled = number

        for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(number, remember)


def _live_head(pid_file: Path, run_id: str) -> int:
    """The pid of this run's head when still running, else 0.

    The watchdog's test: pid, boot id and process start ticks must all agree (reboot, pid reuse).
    """
    try:
        record = json.loads(pid_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if not isinstance(record, dict) or str(record.get("run_id") or "") != run_id:
        return 0
    try:
        pid = int(cast(Any, record.get("pid")))
    except (TypeError, ValueError):
        return 0
    if pid <= 0:
        return 0
    try:
        os.kill(pid, 0)
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return 0
    close = stat.rfind(")")
    fields = stat[close + 2 :].split()
    if close < 0 or len(fields) <= 19:
        return 0
    if fields[0] == "Z":
        return 0
    if str(record.get("boot_id") or "") != boot_id:
        return 0
    if str(record.get("proc_starttime_ticks") or "") != fields[19]:
        return 0
    return pid


def failure_of(started: bool) -> tuple[str, str, int]:
    """The failure file, reason and exit code: `startup.error` before the run was up, else
    `supervisor.error`.
    """
    if started:
        return protocol.SUPERVISOR_ERROR_NAME, RUN_FAILED, EXIT_RUN_FAILED
    return protocol.STARTUP_ERROR_NAME, START_FAILED, EXIT_STARTUP_FAILED


def _write_failure(run_dir: Path, name: str, reason: str, detail: str) -> None:
    """Leave the reason in the run directory, so a launcher learns it rather than timing out."""
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / name).write_text(
            json.dumps({"reason": reason, "detail": detail}, sort_keys=True), encoding="utf-8"
        )
    except OSError:
        pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="local-pty-supervisor", description=__doc__)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--command", required=True)
    parser.add_argument("--cwd", default="")
    parser.add_argument("--rows", type=int, default=24)
    parser.add_argument("--cols", type=int, default=80)
    parser.add_argument("--term", default="xterm-256color")
    parser.add_argument("--quiet-seconds", type=float, default=TURN_QUIET_SECONDS)
    parser.add_argument("--delivery-seconds", type=float, default=protocol.INPUT_DELIVERY_SECONDS)
    parser.add_argument("--memory-limit-mib", type=int, default=None)
    parser.add_argument(
        "--pid-file",
        default="",
        help="where the head writes its launch identity; the run directory's head.pid when omitted",
    )
    parser.add_argument(
        "--daemonize",
        action="store_true",
        help=(
            "fork once and let the launching process reap the intermediate immediately, so the "
            "supervisor is never a child of the tick that started it"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    run_dir = Path(args.run_dir)
    if run_dir.exists():
        (run_dir / protocol.STARTUP_ERROR_NAME).unlink(missing_ok=True)
        (run_dir / protocol.SUPERVISOR_ERROR_NAME).unlink(missing_ok=True)
    if args.daemonize and os.fork() != 0:
        # The intermediate exits at once and its parent reaps it; the supervisor is reparented to
        # init, addressable through its socket and pid file.
        os._exit(EXIT_OK)
    if args.cwd:
        os.chdir(args.cwd)
    supervisor = Supervisor(
        run_dir=run_dir,
        run_id=args.run_id,
        role=args.role,
        task=args.task,
        command=args.command,
        rows=args.rows,
        cols=args.cols,
        term=args.term,
        quiet_seconds=args.quiet_seconds,
        delivery_seconds=args.delivery_seconds,
        pid_file=args.pid_file,
        memory_limit_mib=args.memory_limit_mib,
    )
    try:
        supervisor.claim()
    except (SupervisorStartupError, OSError) as exc:
        reason = getattr(exc, "reason", START_FAILED)
        _write_failure(run_dir, protocol.STARTUP_ERROR_NAME, reason, str(exc))
        print(f"supervisor startup refused ({reason}): {exc}", file=sys.stderr)
        return getattr(exc, "exit_code", EXIT_STARTUP_FAILED)
    try:
        return supervisor.run()
    except Exception as exc:  # noqa: BLE001 - the launcher is owed the reason, whatever it is
        name, reason, code = failure_of(supervisor.started)
        if isinstance(exc, SupervisorStartupError):
            reason = exc.reason
        where = "after the run was up" if supervisor.started else "on the way up"
        _write_failure(run_dir, name, reason, f"the supervisor failed {where}: {exc!r}")
        traceback.print_exc()
        return code


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
