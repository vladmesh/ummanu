"""Launcher and socket client for supervised heads.

`spawn_head` starts an intermediate in a new session that forks the supervisor and exits; the
launcher reaps it at once, so the supervisor is never the launcher's child and survives the
dispatcher tick (even a killed process group). Readiness is read from the run directory: the
socket answers, the journal has `run.started`, and the head wrote its launch identity.
`startup.error` marks a refusal on the way up; `supervisor.error` a failure after the run was up.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

from ..memory import MemoryScopeError
from . import protocol
from .journal import RUN_STARTED, JournalReadResult, read_events
from .scoped_lifecycle import ScopedHeadLifecycle

SUPERVISOR_MODULE = "ummanu.runtime.head.local_pty.supervisor"
SCOPE_LAUNCHER_MODULE = "ummanu.runtime.head.local_pty.scope_launcher"
#: How long `spawn_head` waits for the run directory to say the head is up.
SPAWN_TIMEOUT_SECONDS = 20.0
_POLL_SECONDS = 0.02


class LocalPtyError(RuntimeError):
    """Something about a supervised head could not be done."""


class LocalPtySpawnError(LocalPtyError):
    """A head did not come up, and the reason the run directory gave for it."""

    def __init__(self, reason: str, detail: str, *, cleanup_complete: bool = True, scope_generation: str = "") -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.cleanup_complete = cleanup_complete
        self.scope_generation = scope_generation


@dataclass(frozen=True)
class HeadHandle:
    """Everything a caller needs to reach a head it started, all derived from the run directory."""

    run_dir: Path
    run_id: str
    role: str
    task: str
    socket_path: Path
    journal_path: Path
    pid_file: Path
    supervisor_pid: int
    head_pid: int
    scope_generation: str = ""

    def connect(self, timeout: float = 5.0) -> SupervisorClient:
        return SupervisorClient.connect(self.socket_path, timeout=timeout)

    def events(self) -> JournalReadResult:
        """Read the journal from outside the supervisor, alive or dead."""
        return read_events(self.journal_path)

    def identity(self) -> dict[str, Any]:
        """The head's own launch-identity record, as `with_pid_heartbeat` wrote it."""
        try:
            record = json.loads(self.pid_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return record if isinstance(record, dict) else {}


def _supervisor_environment(extra: Mapping[str, str] | None) -> dict[str, str]:
    """Environment for the supervisor, with this checkout's root first on `PYTHONPATH`.

    An ambient `PYTHONPATH` naming another installation must not make the supervisor run its code.
    """
    environment = dict(os.environ)
    environment.update(extra or {})
    root = str(Path(__file__).resolve().parents[4])
    parts = [part for part in environment.get("PYTHONPATH", "").split(os.pathsep) if part]
    environment["PYTHONPATH"] = os.pathsep.join([root, *[p for p in parts if p != root]])
    return environment


def spawn_head(
    *,
    root: str | os.PathLike[str],
    run_id: str,
    role: str,
    task: str,
    command: str,
    cwd: str | os.PathLike[str] = "",
    rows: int = 24,
    cols: int = 80,
    term: str = "xterm-256color",
    quiet_seconds: float | None = None,
    delivery_seconds: float | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float = SPAWN_TIMEOUT_SECONDS,
    pid_file: str | os.PathLike[str] = "",
    memory_limit_mib: int | None = None,
    owner_unit: str = "",
    scope_generation: str = "",
    launch_admission: Callable[[], contextlib.AbstractContextManager[Any]] | None = None,
) -> HeadHandle:
    """Bring one head up under a supervisor that outlives this process; wait until it answers.

    `pid_file` overrides where the head writes its launch identity (default: run dir `head.pid`);
    the supervisor remains its only writer.
    """
    run_dir = protocol.run_dir_for(root, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(run_dir, 0o700)
    socket_path = protocol.socket_path_for(run_dir)
    error_paths = (
        run_dir / protocol.STARTUP_ERROR_NAME,
        run_dir / protocol.SUPERVISOR_ERROR_NAME,
    )
    for error_path in error_paths:
        error_path.unlink(missing_ok=True)
    journal_path = run_dir / protocol.JOURNAL_NAME
    already = len(read_events(journal_path).events)

    argv = [
        sys.executable,
        "-P",
        "-m",
        SUPERVISOR_MODULE,
        "--run-dir",
        str(run_dir),
        "--run-id",
        run_id,
        "--role",
        role,
        "--task",
        task,
        "--command",
        command,
        "--rows",
        str(rows),
        "--cols",
        str(cols),
        "--term",
        term,
    ]
    if memory_limit_mib is None:
        argv.append("--daemonize")
    if cwd:
        argv += ["--cwd", str(Path(cwd).resolve())]
    if quiet_seconds is not None:
        argv += ["--quiet-seconds", str(quiet_seconds)]
    if delivery_seconds is not None:
        argv += ["--delivery-seconds", str(delivery_seconds)]
    identity_file = Path(pid_file).absolute() if pid_file else run_dir / protocol.PID_FILE_NAME
    if pid_file:
        argv += ["--pid-file", str(identity_file)]
    if memory_limit_mib is not None:
        argv += ["--memory-limit-mib", str(memory_limit_mib)]
    log_path = run_dir / protocol.SUPERVISOR_LOG_NAME
    launch_env = _supervisor_environment(env)
    lifecycle = ScopedHeadLifecycle(run_id, memory_limit_mib, owner_unit, run_dir) if memory_limit_mib is not None else None
    if lifecycle is not None:
        if scope_generation:
            lifecycle.generation = scope_generation
        previous = None
        try:
            previous = ScopedHeadLifecycle.from_run_dir(run_dir)
            if previous is not None:
                if scope_generation:
                    raise MemoryScopeError("a write-ahead scope generation cannot replace an existing owner")
                previous.stop_and_prove_empty()
            lifecycle.persist(run_dir, role=role, task=task, workspace=str(Path(cwd or os.getcwd()).resolve()),
                              replace_existing=not bool(scope_generation))
            descriptor = os.open(run_dir.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except MemoryScopeError as exc:
            raise LocalPtySpawnError("cleanup_failed", str(exc), cleanup_complete=False,
                                     scope_generation=scope_generation or (previous.generation if previous is not None else lifecycle.generation)) from exc
        argv = lifecycle.launcher_argv(
            argv, run_dir=run_dir, log_path=log_path, timeout=timeout,
            pythonpath=launch_env["PYTHONPATH"],
        )
    intermediate = None
    try:
        # Board ownership is checked at the launch syscall, after scope/setup work.
        # Release its per-card fence before waiting for readiness or delivering a prompt.
        with (
            launch_admission() if launch_admission is not None else contextlib.nullcontext()
        ), open(log_path, "ab", buffering=0) as log:
            intermediate = subprocess.Popen(
                argv,
                cwd=str(cwd) if cwd else None,
                env=launch_env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
                close_fds=True,
            )
        # Reap the launcher before cleanup, so it cannot register a late scope afterwards.
        status = intermediate.wait()
    except (OSError, subprocess.SubprocessError) as exc:
        error = LocalPtySpawnError("scope_failed", f"head scope launcher failed: {exc}")
        raise _after_failed_launch(error, lifecycle, journal_path, socket_path, already) from exc
    except Exception as exc:  # admission can fail before or after the launch syscall
        if intermediate is not None:
            intermediate.wait()
            # A failed SQL commit after Popen cannot attest absence, even if the
            # supervisor has not yet written its heartbeat. Retain exact intent.
            raise LocalPtySpawnError("admission_failed", str(exc), cleanup_complete=False,
                                     scope_generation=scope_generation) from exc
        if lifecycle is not None:
            error = _after_failed_launch(LocalPtySpawnError("admission_refused", str(exc)),
                                        lifecycle, journal_path, socket_path, already)
            if not error.cleanup_complete:
                raise error from exc
        raise
    if memory_limit_mib is not None and status != 0:
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-2048:]
        error = LocalPtySpawnError("scope_failed", f"head scope did not start (exit {status}): {tail}")
        raise _after_failed_launch(error, lifecycle, journal_path, socket_path, already)

    deadline = time.monotonic() + timeout
    while True:
        for error_path in error_paths:
            failure = _startup_error(error_path)
            if failure is not None:
                error = LocalPtySpawnError(
                    str(failure.get("reason") or "startup_failed"),
                    str(failure.get("detail") or ""),
                )
                raise _after_failed_launch(error, lifecycle, journal_path, socket_path, already)
        result = read_events(journal_path)
        observed = lifecycle.started_or_exited(result.events, already) if lifecycle else None
        started = [event for event in result.events[already:] if event.get("kind") == RUN_STARTED]
        record = observed[0] if observed else (started[-1] if started else None)
        exited = observed[1] if observed else False
        if (
            record is not None
            and _identity_written(identity_file, run_id)
            and (exited or (socket_path.exists() and _answers(socket_path)))
        ):
            return HeadHandle(
                run_dir=run_dir,
                run_id=run_id,
                role=role,
                task=task,
                socket_path=socket_path,
                journal_path=journal_path,
                pid_file=identity_file,
                supervisor_pid=int(record.get("supervisor_pid") or 0),
                head_pid=int(record.get("head_pid") or 0),
                scope_generation=lifecycle.generation if lifecycle is not None else "",
            )
        if time.monotonic() >= deadline:
            tail = ""
            try:
                tail = log_path.read_text(encoding="utf-8", errors="replace")[-2048:]
            except OSError:
                pass
            error = LocalPtySpawnError(
                "timeout",
                f"the supervisor for {run_id} did not answer within {timeout:g}s "
                f"(intermediate exit {status}); log tail: {tail!r}",
            )
            raise _after_failed_launch(error, lifecycle, journal_path, socket_path, already)
        time.sleep(_POLL_SECONDS)


def _after_failed_launch(
    error: LocalPtySpawnError, lifecycle: ScopedHeadLifecycle | None,
    journal_path: Path, socket_path: Path, since: int,
) -> LocalPtySpawnError:
    if lifecycle is None:
        return error
    observed = lifecycle.started_or_exited(read_events(journal_path).events, since)
    try:
        if observed is not None and not observed[1]:
            lifecycle.cancel_started(
                socket_path=socket_path, journal_path=journal_path,
                started_seq=int(observed[0].get("seq") or 0),
            )
        else:
            lifecycle.stop_and_prove_empty()
    except MemoryScopeError as exc:
        return LocalPtySpawnError(
            "cleanup_failed", f"{error.detail}; {exc}", cleanup_complete=False,
            scope_generation=lifecycle.generation,
        )
    return error


def _identity_written(pid_file: Path, run_id: str) -> bool:
    """Whether the head has written its launch identity naming this run.

    The heartbeat is written by the head's shell before `exec`, so `run.started` can land first;
    a handle is only returned once the record exists.
    """
    try:
        record = json.loads(pid_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(record, dict) and str(record.get("run_id") or "") == run_id


def _startup_error(path: Path) -> dict[str, Any] | None:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _answers(socket_path: Path) -> bool:
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    try:
        probe.connect(str(socket_path))
    except OSError:
        return False
    finally:
        probe.close()
    return True


class SupervisorClient:
    """One synchronous connection to one supervisor; requests answered in order, attach pushes after.

    The `HeadRuntime` backend turns these answers into receipts.
    """

    def __init__(self, conn: socket.socket) -> None:
        self._conn = conn
        self._inbox = bytearray()
        self._request_seq = 0
        #: Answers to questions this client stopped waiting for, discarded rather than returned.
        self.stale_frames = 0
        self.attached = False

    @classmethod
    def connect(cls, socket_path: str | os.PathLike[str], *, timeout: float = 5.0) -> SupervisorClient:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(timeout)
        try:
            conn.connect(str(socket_path))
        except OSError as exc:
            conn.close()
            raise LocalPtyError(f"no supervisor answers at {socket_path}: {exc}") from exc
        return cls(conn)

    def close(self) -> None:
        try:
            self._conn.close()
        except OSError:
            pass

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- frames ----------------------------------------------------------------------------

    def _next_frame(self) -> dict[str, Any]:
        while True:
            index = self._inbox.find(b"\n")
            if index >= 0:
                line = bytes(self._inbox[:index])
                del self._inbox[: index + 1]
                return protocol.decode_frame(line)
            chunk = self._conn.recv(65536)
            if not chunk:
                raise LocalPtyError("the supervisor closed the connection")
            self._inbox += chunk

    def _refusal_already_sent(self) -> dict[str, Any] | None:
        """A frame queued before this connection could be written to, or `None`.

        Used only when a request could not be sent. Stream events and id-carrying frames (stale
        answers to abandoned requests) are skipped; only an uncorrelated frame, such as a
        connection-limit refusal or a malformed-bytes refusal, is returned.
        """
        try:
            while True:
                frame = self._next_frame()
                if "event" in frame:
                    continue
                if frame.get(protocol.REQUEST_ID) is not None:
                    self.stale_frames += 1
                    continue
                return frame
        except (LocalPtyError, OSError):
            return None

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send one request and return its answer, matched by id.

        Pushed events are skipped (they belong to `next_event`), uncorrelated frames are returned,
        and answers to earlier abandoned requests are discarded and counted in `stale_frames`, so
        a caller timeout never leaves the connection one answer out of step.
        """
        self._request_seq += 1
        request_id = self._request_seq
        try:
            self._conn.sendall(protocol.encode_frame({**payload, protocol.REQUEST_ID: request_id}))
        except OSError:
            # At the connection bound the supervisor writes a refusal and closes before any request,
            # so this write can fail with `EPIPE` while the refusal waits in the receive queue.
            # Read it rather than raising out of a verb for a live head.
            refusal = self._refusal_already_sent()
            if refusal is None:
                raise
            return refusal
        while True:
            frame = self._next_frame()
            if "event" in frame:
                continue
            answered = frame.get(protocol.REQUEST_ID)
            if answered == request_id:
                return frame
            if answered is None:
                # Uncorrelated (connection refused, or bytes too malformed to carry an id).
                return frame
            self.stale_frames += 1

    # -- verbs -----------------------------------------------------------------------------

    def set_timeout(self, timeout: float) -> None:
        """Set the per-request answer timeout for this connection.

        The connect bound suits reaching a silent supervisor, not watching a slow operation; a
        caller that knows the substrate's bound for what it watches sets it from that.
        """
        self._conn.settimeout(timeout)

    def status(self) -> dict[str, Any]:
        return self.request({"op": protocol.OP_STATUS})

    def send_input(self, data: bytes | str, *, subject: str = "") -> dict[str, Any]:
        """Offer one bounded payload for the head's pty. Oversize is refused, never truncated.

        The answer is about admission, within one supervisor tick: `ok` carries the `delivery`
        (id, size, state). Refusals: over the limit (with limit and size), admission closed, head
        gone, or another delivery in flight. What landed is in `status()["delivery"]` and the
        journal's `input.accepted`; `wait_for_delivery` polls for it.
        """
        payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        return self.request(
            {"op": protocol.OP_INPUT, "data": protocol.encode_payload(payload), "subject": subject}
        )

    def wait_for_delivery(
        self,
        delivery_id: int | None = None,
        *,
        timeout: float = protocol.INPUT_DELIVERY_SECONDS + 5.0,
        poll: float = 0.02,
    ) -> dict[str, Any]:
        """Poll `status` until the delivery leaves `in_flight`, and return it.

        The wait is caller-side and abandonable; every delivery ends as complete, stalled or
        failed, so this returns.
        """
        deadline = time.monotonic() + timeout
        while True:
            delivery = self.status().get("delivery")
            if (isinstance(delivery, dict) and (delivery_id is None or delivery.get("id") == delivery_id)
                    and delivery.get("state") != protocol.DELIVERY_IN_FLIGHT):
                return delivery
            if time.monotonic() >= deadline:
                raise LocalPtyError(
                    f"delivery {delivery_id} was still in flight after {timeout:g}s: {delivery}"
                )
            time.sleep(poll)

    def read_output(self, max_bytes: int | None = None) -> dict[str, Any]:
        request: dict[str, Any] = {"op": protocol.OP_OUTPUT}
        if max_bytes is not None:
            request["max_bytes"] = int(max_bytes)
        answer = self.request(request)
        if answer.get("ok"):
            answer["bytes_data"] = protocol.decode_payload(answer.get("data") or "")
        return answer

    def resize(self, rows: int, cols: int) -> dict[str, Any]:
        return self.request({"op": protocol.OP_RESIZE, "rows": int(rows), "cols": int(cols)})

    def drain(self, initiator: str = "client") -> dict[str, Any]:
        return self.request({"op": protocol.OP_DRAIN, "initiator": initiator})

    def stop(self, initiator: str = "client", signal_name: str = "TERM") -> dict[str, Any]:
        return self.request({"op": protocol.OP_STOP, "initiator": initiator, "signal": signal_name})

    def attach(self) -> dict[str, Any]:
        answer = self.request({"op": protocol.OP_ATTACH})
        if answer.get("ok"):
            self.attached = True
            answer["bytes_data"] = protocol.decode_payload(answer.get("data") or "")
        return answer

    def next_event(self, timeout: float | None = None) -> dict[str, Any] | None:
        """The next pushed frame, or `None` when none arrived within `timeout` or the stream ended."""
        if timeout is not None:
            self._conn.settimeout(timeout)
        while True:
            try:
                frame = self._next_frame()
            except TimeoutError:
                return None
            except (LocalPtyError, OSError):
                return None
            if "event" not in frame:
                continue
            if frame.get("data") is not None:
                frame["bytes_data"] = protocol.decode_payload(frame["data"])
            return frame

    def stream(self, *, timeout: float | None = None) -> Iterator[dict[str, Any]]:
        """Yield pushed events for an attached client until the connection ends or the head exits.

        Detaching is closing the connection; it does nothing to the head.
        """
        while True:
            frame = self.next_event(timeout)
            if frame is None:
                return
            yield frame
            if frame.get("event") == protocol.EVENT_EXITED:
                return
