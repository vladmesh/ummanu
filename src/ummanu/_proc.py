"""One thin, explicit gateway to ordinary child processes."""

from __future__ import annotations

import os
import select
import selectors
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import IO, TextIO, cast


def run(
    argv: Sequence[str],
    *,
    input: str | bytes | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
    cwd: str | Path | None = None,
    check: bool = False,
    text: bool = True,
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        argv,
        input=input,
        env=env,
        timeout=timeout,
        cwd=cwd,
        check=check,
        capture_output=True,
        text=text,
    )


def run_isolated(
    argv: Sequence[str],
    *,
    input: str | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
    cwd: str | Path | None = None,
    pass_fds: Sequence[int] = (),
    within: Callable[[], float] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a child in its own process group; when it returns, normally or by timeout, nothing it
    started is left in that group.

    This is the one place that enforces that for everything a production tick runs and waits on.
    The dispatcher unit sets ``KillMode=process`` so the local-pty heads a tick launches outlive it
    (secretary-1699), so the unit's control-group kill no longer sweeps what a child left behind: a
    gate's or adapter's background process under ``bash -lc``, a provider CLI under a probe shell,
    Git's ``git-remote-https`` helper. On every path (success, non-zero exit, timeout, exception)
    whatever remains of the child's group is killed once the leader has exited or timed out, with
    the same bounded, EPERM-safe reap.

    The group is killed while its leader is still unreaped: the leader's exit is observed with
    ``WNOWAIT`` (a pidfd, or ``waitid``), so the zombie keeps the group id from being recycled
    until the kill has landed, and only then is it reaped.

    A command that deliberately leaves its group (``setsid``, a daemon) is outside this contract:
    it is not swept, and it can hold the reap open for at most the grace period.

    ``within`` is what is left of a caller's own deadline. Everything the call does then fits in
    it, with no grace of its own: the child runs until ``_TERMINATE_SECONDS`` of it are left (or
    ``timeout``, if sooner); a child still running is asked to end with ``SIGTERM`` to its whole
    group, which lets Git remove its own lock files, for at most that tail; then the group is
    killed, and the drain and the reap get only what is left. A timed-out call's
    ``TimeoutExpired`` says in ``killed`` whether the leader outlived ``SIGTERM``.
    """
    process = subprocess.Popen(
        argv,
        env=env,
        cwd=cwd,
        text=True,
        stdin=subprocess.PIPE if input is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        pass_fds=tuple(pass_fds),
    )
    pump = _Pump(process, input)
    grace = None if within is None else lambda: max(0.0, within())
    killed = False
    try:
        run_for = timeout
        if within is not None:
            run_for = max(0.0, within() - _TERMINATE_SECONDS)
            run_for = run_for if timeout is None else min(timeout, run_for)
        exited = pump.until_leader_exits(run_for)
        if not exited and within is not None:
            _signal_group(process, signal.SIGTERM)
            killed = not pump.until_leader_exits(min(_TERMINATE_SECONDS, max(0.0, within())))
    except BaseException:
        _end_group(process, pump, grace)
        raise
    stdout, stderr = _end_group(process, pump, grace)
    if not exited:
        assert run_for is not None  # only a timeout ends the pump before the leader exits
        expired = subprocess.TimeoutExpired(argv, run_for, output=stdout, stderr=stderr)
        expired.killed = killed  # type: ignore[attr-defined]
        raise expired
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


# How long the output of a killed group may keep flowing, and then how long its leader may take to
# die. Only a descendant that left the group (its own `setsid`) or one this process may not signal
# can still hold the pipes open after the kill; waiting on it would turn a bounded child into an
# unbounded wait.
_REAP_GRACE_SECONDS = 5.0

# Without a pidfd, how often the leader is checked for an exit while its output is read.
_LEADER_POLL_SECONDS = 0.05

# Inside a caller's deadline, the tail of it a child still running gets to end itself on SIGTERM.
_TERMINATE_SECONDS = 0.25


def _signal_group(process: subprocess.Popen[str], signum: int) -> None:
    """`signum` to the child's whole group, or to the leader alone when no member may be signalled."""
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        pass
    except PermissionError:
        try:
            process.send_signal(signum)
        except (ProcessLookupError, PermissionError):
            pass


def _end_group(process: subprocess.Popen[str], pump: _Pump,
               grace: Callable[[], float] | None = None) -> tuple[str, str]:
    """Kill what remains of the child's process group, drain its output, then reap its leader.

    ``killpg`` signals every member this process may signal and fails with EPERM only when it may
    signal none of them, e.g. a non-root caller whose group holds only a setuid ``sudo`` and the
    other-uid command under it. Then the leader alone is tried, and whatever cannot be signalled is
    left running rather than raised or waited on without bound. A root caller (the only one that
    crosses identity through ``runuser``, which keeps its child in the group) may signal everyone.
    An already empty group (ESRCH) is the ordinary end of a command that left nothing behind.
    """
    _signal_group(process, signal.SIGKILL)
    try:
        pump.drain(_REAP_GRACE_SECONDS if grace is None else grace())
    finally:
        pump.close()
    try:
        process.wait(timeout=_REAP_GRACE_SECONDS if grace is None else grace())
    except subprocess.TimeoutExpired:
        pass
    return pump.text()


class _Pump:
    """Feed a child's stdin and collect its output while watching for its leader to exit, without
    reaping it: ``communicate`` would reap the leader and free its pid as a group id."""

    def __init__(self, process: subprocess.Popen[str], input: str | None) -> None:
        self.process = process
        self.selector = selectors.DefaultSelector()
        assert process.stdout is not None and process.stderr is not None
        self.stdout_fd, self.stderr_fd = process.stdout.fileno(), process.stderr.fileno()
        self.chunks: dict[int, list[bytes]] = {self.stdout_fd: [], self.stderr_fd: []}
        for pipe in (process.stdout, process.stderr):
            self.selector.register(pipe, selectors.EVENT_READ)
        self.pending = memoryview(b"")
        if process.stdin is not None:
            stdin = cast(TextIO, process.stdin)
            self.pending = memoryview((input or "").encode(stdin.encoding, stdin.errors or "strict"))
            if self.pending:
                self.selector.register(process.stdin, selectors.EVENT_WRITE)
            else:
                _close_quietly(process.stdin)
        self.pidfd = _open_pidfd(process.pid)
        if self.pidfd is not None:
            self.selector.register(self.pidfd, selectors.EVENT_READ)

    def until_leader_exits(self, timeout: float | None) -> bool:
        """Pump until the leader has exited (True) or the timeout passed (False)."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if self.pidfd is None and _exited_unreaped(self.process.pid):
                return True
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return False
            wait = remaining
            if self.pidfd is None:
                wait = _LEADER_POLL_SECONDS if remaining is None else min(remaining, _LEADER_POLL_SECONDS)
            for key, _events in self.selector.select(wait):
                if key.fileobj == self.pidfd:
                    return True
                self._service(key)

    def drain(self, grace: float) -> None:
        """Read what the killed group left in the pipes, for at most ``grace`` seconds."""
        if self.pidfd is not None:
            self.selector.unregister(self.pidfd)
        deadline = time.monotonic() + grace
        while self.selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            for key, _events in self.selector.select(remaining):
                self._service(key)

    def close(self) -> None:
        self.selector.close()
        if self.pidfd is not None:
            os.close(self.pidfd)
            self.pidfd = None
        for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
            if pipe is not None:
                _close_quietly(pipe)

    def text(self) -> tuple[str, str]:
        stdout, stderr = self.process.stdout, self.process.stderr
        assert stdout is not None and stderr is not None
        return _decode(stdout, self.chunks[self.stdout_fd]), _decode(stderr, self.chunks[self.stderr_fd])

    def _service(self, key: selectors.SelectorKey) -> None:
        if key.fileobj is self.process.stdin:
            try:
                written = os.write(key.fd, self.pending[:_PIPE_CHUNK])
            except BrokenPipeError:
                written = len(self.pending)
            self.pending = self.pending[written:]
            if not self.pending:
                self.selector.unregister(key.fileobj)
                _close_quietly(self.process.stdin)
            return
        data = os.read(key.fd, 32768)
        if data:
            self.chunks[key.fd].append(data)
        else:
            self.selector.unregister(key.fileobj)


_PIPE_CHUNK = getattr(select, "PIPE_BUF", 512)


def _open_pidfd(pid: int) -> int | None:
    try:
        return os.pidfd_open(pid)
    except (AttributeError, OSError):
        return None


def _exited_unreaped(pid: int) -> bool:
    try:
        return os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
    except ChildProcessError:
        return True


def _decode(pipe: IO[str], chunks: list[bytes]) -> str:
    """The text ``communicate`` would have returned: decoded, universal newlines."""
    stream = cast(TextIO, pipe)
    text = b"".join(chunks).decode(stream.encoding, stream.errors or "strict")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _close_quietly(pipe: IO[str]) -> None:
    try:
        pipe.close()
    except OSError:
        pass
