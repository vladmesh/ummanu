"""The scoped head lifecycle across launch, cgroup verification, and exit."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..memory import (
    CGROUP_ROOT,
    MEMORY_LIMIT_REASON,
    MemoryScopeError,
    ScopeEvidence,
    memory_events,
    own_cgroup,
    scope_argv,
    scope_unit,
    supervisor_oom_protected,
)

LAUNCH_BINDING_ENV = "UMMANU_SCOPE_LAUNCH_BINDING"
LAUNCH_FIELDS = ("run_id", "unit", "generation", "role", "task", "workspace",
                 "launch_pid", "launch_identity")


def binding_description(binding: dict[str, Any]) -> str:
    payload = json.dumps(binding, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "ummanu-launch-v1:" + hashlib.sha256(payload.encode("ascii")).hexdigest()


def launch_binding(record: dict[str, Any], directory: Path) -> dict[str, Any]:
    directory = directory.absolute()
    # The caller selected this actual launch root. The reader anchors it under
    # its selected data root; no installation root is guessed from basename.
    return {"version": 1, "directory": str(directory), "root": str(directory.parent),
            **{key: record[key] for key in LAUNCH_FIELDS}}


def native_scope_state(unit: str, *, timeout: float = 10.0) -> dict[str, str]:
    properties = ("Id", "LoadState", "ActiveState", "SubState", "ControlGroup",
                  "InvocationID", "ActiveEnterTimestampMonotonic", "Transient", "BindsTo", "Description")
    result = subprocess.run(
        ["systemctl", "--system", "show", unit, "--property=" + ",".join(properties)],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    if result.returncode or result.stderr.strip():
        raise MemoryScopeError("native runtime scope observation failed; retry systemctl show")
    fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if any(name not in fields for name in properties):
        raise MemoryScopeError("native runtime scope observation is incomplete")
    return fields


#: `systemctl stop` of a unit systemd no longer has loaded: exit status 5 (LSB "program is not
#: installed") with "Unit <name> not loaded." on stderr.
SYSTEMCTL_UNIT_NOT_LOADED = 5


def _unit_not_loaded(returncode: int, detail: str) -> bool:
    """Whether a failed `systemctl stop` only says the unit is already gone."""
    return returncode == SYSTEMCTL_UNIT_NOT_LOADED and "not loaded" in detail


def launch_identity(pid: int) -> str | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except FileNotFoundError:
        return None
    except (OSError, IndexError) as exc:
        raise MemoryScopeError(f"could not inspect scope launcher {pid}: {exc}") from exc
    return None if fields[0] in ("Z", "X") else f"{boot}:{fields[19]}"


def launch_group_present(pid: int, identity: str) -> bool:
    """The launch group cannot be reused without a new leader of its PGID.

    When the leader has died, surviving group members still hold its group number.
    A reboot or a new leader proves that original group ended. Inspect every member
    because a dead leader does not prove its sudo/systemd-run descendants ended.
    """
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    if not identity.startswith(boot + ":"):
        return False
    try:
        leader = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except FileNotFoundError:
        leader = None
    if leader is not None and identity != f"{boot}:{leader[19]}":
        return False
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
        except FileNotFoundError:
            continue
        if fields[2] == str(pid) and fields[0] not in ("Z", "X"):
            return True
    return False


@dataclass
class ScopedHeadLifecycle:
    """The scoped head's launch, cgroup proof, and exit classification across processes."""

    run_id: str
    limit_mib: int
    owner_unit: str = ""
    directory: Path | None = None
    generation: str = field(default_factory=lambda: uuid.uuid4().hex)

    def persist(self, run_dir: Path, *, role: str = "", task: str = "", workspace: str = "", replace_existing: bool = True) -> None:
        """Leave the scope name on disk before any process can create the unit."""
        with self.owner_lock(run_dir):
            path = run_dir / "scope-owner.json"
            if path.exists():
                if not replace_existing:
                    raise MemoryScopeError("a write-ahead scope generation cannot replace an existing owner")
                if not self.read_owner(run_dir)["cleanup_complete"]:
                    raise MemoryScopeError("an unsettled scope owner cannot be replaced")
            self.update_owner(run_dir, {
                "run_id": self.run_id, "unit": scope_unit(self.run_id),
                "generation": self.generation, "launch_allowed": True, "cleanup_complete": False,
                "role": role, "task": task, "workspace": workspace,
            })
        self.directory = run_dir

    @staticmethod
    def from_run_dir(run_dir: Path) -> ScopedHeadLifecycle | None:
        run_dir = run_dir.resolve()
        try:
            record = ScopedHeadLifecycle.read_owner(run_dir)
        except FileNotFoundError:
            return None
        return ScopedHeadLifecycle(record["run_id"], 1, directory=run_dir, generation=record["generation"])

    @staticmethod
    def read_owner(run_dir: Path) -> dict[str, Any]:
        try:
            record = json.loads((run_dir / "scope-owner.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise
        except (OSError, ValueError) as exc:
            raise MemoryScopeError(f"could not read scope owner in {run_dir}: {exc}") from exc
        return ScopedHeadLifecycle.validate_owner(record)

    @staticmethod
    def validate_owner(record: Any) -> dict[str, Any]:
        """Validate the deployed owner format for lifecycle and read-only projections."""
        try:
            run_id = record["run_id"]
            if not isinstance(run_id, str) or not run_id or record["unit"] != scope_unit(run_id):
                raise ValueError("invalid scope identity")
        except (KeyError, TypeError, ValueError) as exc:
            raise MemoryScopeError(f"invalid scope owner: {exc}") from exc
        if not isinstance(record.get("generation"), str) or not record["generation"]:
            raise MemoryScopeError("scope owner has no launch generation")
        if type(record.get("launch_allowed")) is not bool or type(record.get("cleanup_complete")) is not bool:
            raise MemoryScopeError("scope owner has invalid launch or cleanup state")
        if record["cleanup_complete"] and record["launch_allowed"]:
            raise MemoryScopeError("an empty scope owner cannot admit launch work")
        if "launch_pid" in record or "launch_identity" in record:
            identity = record.get("launch_identity")
            if (
                type(record.get("launch_pid")) is not int or record["launch_pid"] <= 0
                or not isinstance(identity, str) or ":" not in identity
                or not identity.rsplit(":", 1)[0] or not identity.rsplit(":", 1)[1].isdecimal()
            ):
                raise MemoryScopeError("scope owner has invalid launcher identity")
        return record

    @contextmanager
    def ownership(self) -> Iterator[dict[str, Any]]:
        """Serialize admission, termination, proof and its consumer for this generation.

        Callers already inside this operation use stop_owned; it never reacquires the
        flock. Lock contention is a retryable refusal, including across processes.
        """
        try:
            if self.directory is None:
                yield {"generation": self.generation, "run_id": self.run_id}
                return
            with self.owner_lock(self.directory):
                record = self.read_owner(self.directory)
                if record["run_id"] != self.run_id or record["generation"] != self.generation:
                    raise MemoryScopeError("scope owner changed; refusing a stale cleanup")
                yield record
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            raise MemoryScopeError(f"could not settle head scope {scope_unit(self.run_id)}: {exc}") from exc

    @staticmethod
    @contextmanager
    def owner_lock(run_dir: Path) -> Iterator[None]:
        with (run_dir / "scope-owner.lock").open("a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise MemoryScopeError("scope ownership is being changed; retry cleanup") from exc
            yield

    @staticmethod
    def update_owner(run_dir: Path, record: dict[str, Any]) -> None:
        path = run_dir / "scope-owner.json"
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(record, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        fd = os.open(run_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def launcher_argv(
        self, supervisor: list[str], *, run_dir: Path, log_path: Path, timeout: float,
        pythonpath: str,
    ) -> list[str]:
        if "--daemonize" in supervisor:
            raise ValueError("a scoped supervisor must remain attached until its head exits")
        return [
            sys.executable, "-P", "-m", "ummanu.runtime.head.local_pty.scope_launcher",
            str(run_dir), str(log_path), str(timeout), self.generation,
            *scope_argv(self.run_id, self.limit_mib, supervisor,
                        pythonpath=pythonpath, owner_unit=self.owner_unit),
        ]

    @staticmethod
    def launch_until_started(arguments: list[str]) -> int:
        """Detach a synchronous systemd-run only after a durable start or a named refusal."""
        from . import protocol
        from .journal import RUN_STARTED, read_events

        run_dir, log_path, timeout = Path(arguments[0]), Path(arguments[1]), float(arguments[2])
        since = len(read_events(run_dir / protocol.JOURNAL_NAME).events)
        # The child cannot invoke systemd until its launch identity is durable. EOF on
        # the barrier (launcher death before release) makes it exit without creating work.
        with ScopedHeadLifecycle.owner_lock(run_dir):
            record = ScopedHeadLifecycle.read_owner(run_dir)
            if not record["launch_allowed"] or record["generation"] != arguments[3]:
                return 1
            if run_dir.absolute() != run_dir.resolve():
                raise MemoryScopeError("scope admission directory is not canonical")
            # The admitted snapshot crosses the gate, rather than being assembled
            # later from a mutable owner. The released caller already supplies [3].
            admitted = {key: record[key] for key in ("run_id", "unit", "generation", "role", "task", "workspace")}
            admitted["directory"] = str(run_dir.absolute())
            read_fd, write_fd = os.pipe()
            try:
                with log_path.open("ab", buffering=0) as log:
                    scope = subprocess.Popen(
                        [sys.executable, "-P", "-m", "ummanu.runtime.head.local_pty.scope_launcher",
                         "--exec-gated", str(read_fd), json.dumps(admitted), *arguments[4:]],
                        stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                        start_new_session=True, close_fds=True, pass_fds=(read_fd,),
                    )
                record["launch_pid"] = scope.pid
                record["launch_identity"] = launch_identity(scope.pid)
                if record["launch_identity"] is None:
                    raise MemoryScopeError("could not establish the scope launch identity")
                ScopedHeadLifecycle.update_owner(run_dir, record)
                os.write(write_fd, b"1")
            finally:
                os.close(read_fd)
                os.close(write_fd)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if any(event.get("kind") == RUN_STARTED for event in read_events(run_dir / protocol.JOURNAL_NAME).events[since:]):
                return 0
            if (run_dir / protocol.STARTUP_ERROR_NAME).exists():
                try:
                    scope.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    scope.terminate()
                    try:
                        scope.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        scope.kill()
                        scope.wait()
                return 0
            status = scope.poll()
            if status is not None:
                return status
            time.sleep(0.05)
        scope.terminate()
        try:
            scope.wait(timeout=5)
        except subprocess.TimeoutExpired:
            scope.kill()
            scope.wait()
        return 1

    @contextmanager
    def attest_launch(self, *, directory: Path, role: str, task: str, workspace: str) -> Iterator[dict[str, Any]]:
        """Bind the sealed admission to this native incarnation before head work."""
        raw = os.environ.pop(LAUNCH_BINDING_ENV, "")
        try:
            binding = json.loads(raw)
            if binding["directory"] != str(directory.absolute()):
                raise MemoryScopeError("sealed launch belongs to a different run directory")
            with self.owner_lock(directory):
                record = self.read_owner(directory)
                if (not record["launch_allowed"] or record["cleanup_complete"]
                        or binding != launch_binding(record, directory)
                        or binding["run_id"] != self.run_id or binding["role"] != role
                        or binding["task"] != task or binding["workspace"] != workspace):
                    raise MemoryScopeError("sealed launch admission does not match the actual supervisor")
                group = own_cgroup()
                state = native_scope_state(record["unit"])
                if (group is None or group != CGROUP_ROOT / "system.slice" / record["unit"]
                        or state["Id"] != record["unit"] or state["LoadState"] != "loaded"
                        or state["Transient"] != "yes" or not state["InvocationID"]
                        or state["ControlGroup"] != f"/system.slice/{record['unit']}"
                        or state["Description"] != binding_description(binding)
                        or launch_identity(record["launch_pid"]) != record["launch_identity"]):
                    raise MemoryScopeError("native scope does not match sealed launch admission")
                ticks = int(record["launch_identity"].rsplit(":", 1)[1])
                if int(state["ActiveEnterTimestampMonotonic"]) < ticks * 1_000_000 // os.sysconf("SC_CLK_TCK"):
                    raise MemoryScopeError("native scope predates the admitted launcher")
                info = group.stat()
                yield {"admitted": binding, "invocation_id": state["InvocationID"],
                       "activation": state["ActiveEnterTimestampMonotonic"],
                       "cgroup": [info.st_dev, info.st_ino]}
        except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
            raise MemoryScopeError("scope launch attestation is unavailable") from exc

    @staticmethod
    def started_or_exited(events: Sequence[dict[str, Any]], since: int) -> tuple[dict[str, Any], bool] | None:
        """A completed short head is still a successful launch with a durable exit to read."""
        from .journal import RUN_EXITED, RUN_STARTED

        fresh = events[since:]
        started = [event for event in fresh if event.get("kind") == RUN_STARTED]
        if not started:
            return None
        record = started[-1]
        exited = any(
            event.get("kind") == RUN_EXITED and int(event.get("seq") or 0) > int(record.get("seq") or 0)
            for event in fresh
        )
        return record, exited

    def cancel_started(self, *, socket_path: Path, journal_path: Path, started_seq: int) -> None:
        """Stop a launched head and prove its whole scope empty before launch fails.

        The heartbeat can lag run.started. In that interval the caller has no handle, but
        the supervisor already owns a live head. Its socket requests a clean stop;
        systemd stops the whole scope even if the head was reaped or the socket failed.
        """
        from .client import SupervisorClient
        from .journal import RUN_EXITED, read_events

        def reaped() -> bool:
            return any(
                event.get("kind") == RUN_EXITED and int(event.get("seq") or 0) > started_seq
                for event in read_events(journal_path).events
            )

        with self.ownership() as record:
            requested = False
            try:
                with SupervisorClient.connect(socket_path, timeout=1.0) as client:
                    client.stop(initiator="startup_failed", signal_name="KILL")
                    requested = True
            except (OSError, RuntimeError):
                pass
            deadline = time.monotonic() + (10.0 if requested else 0.0)
            while time.monotonic() < deadline:
                if reaped():
                    break
                time.sleep(0.05)
            self.stop_owned(record)

    def stop_and_prove_empty(self) -> None:
        """Stop the entire cgroup and keep ownership if empty membership cannot be proved."""
        with self.ownership() as record:
            self.stop_owned(record)

    def stop_owned(self, record: dict[str, Any], *, remaining: Callable[[], float] | None = None) -> None:
        """Terminate and durably prove empty while ownership() remains held.

        A caller's `remaining` cuts each of its waits (launch group, `systemctl stop`, membership)
        to what is left; one it outlives is the same retryable refusal as its own bound.
        """
        def bound(seconds: float, what: str) -> float:
            if remaining is None:
                return seconds
            left = remaining()
            if left <= 0:
                raise MemoryScopeError(f"the caller's deadline passed before {what}")
            return min(seconds, left)

        if self.directory is not None:
            record["launch_allowed"] = False
            record["cleanup_complete"] = False
            self.update_owner(self.directory, record)
            pid = record.get("launch_pid")
            if pid and launch_group_present(pid, record["launch_identity"]):
                # This is the gated launch process, before systemd can create the scope.
                # Its group also covers sudo/systemd-run until registration. Scope
                # membership below covers the payload once systemd has moved it.
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    result = subprocess.run(
                        ["sudo", "-n", "kill", "-KILL", "--", f"-{pid}"],
                        capture_output=True, timeout=bound(5, "the launch group kill"), check=False,
                    )
                    if result.returncode:
                        raise MemoryScopeError("could not terminate the scope launch group")
                deadline = time.monotonic() + bound(5, "the launch group exit")
                while launch_group_present(pid, record["launch_identity"]):
                    if time.monotonic() >= deadline:
                        raise MemoryScopeError("scope launch process has not exited")
                    time.sleep(0.05)
        cgroup = CGROUP_ROOT / "system.slice" / scope_unit(self.run_id)
        try:
            cgroup.stat()
        except FileNotFoundError:
            self._record_empty(record)
            return
        except OSError as exc:
            raise MemoryScopeError(f"could not inspect head scope: {exc}") from exc
        try:
            stopped = subprocess.run(
                ["sudo", "-n", "systemctl", "stop", scope_unit(self.run_id)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                check=False, timeout=bound(15.0, "the scope stop"),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MemoryScopeError(f"could not stop head scope {scope_unit(self.run_id)}: {exc}") from exc
        if stopped.returncode != 0:
            detail = stopped.stderr.decode("utf-8", errors="replace").strip()
            # A transient scope is unloaded as soon as its last process leaves. A head asked to stop
            # through its socket usually takes its supervisor with it, so systemd can unload the
            # scope between the cgroup look above and this stop. That is not a refusal: the unit is
            # already gone, and the membership proof below settles it either way (an absent or
            # unpopulated cgroup is empty; a populated one still refuses).
            if not _unit_not_loaded(stopped.returncode, detail):
                raise MemoryScopeError(f"could not stop head scope {scope_unit(self.run_id)}: {detail}")
        deadline = time.monotonic() + bound(10.0, "the scope membership proof")
        while True:
            try:
                fields = (cgroup / "cgroup.events").read_text(encoding="ascii").splitlines()
            except FileNotFoundError:
                try:
                    cgroup.stat()
                except FileNotFoundError:
                    self._record_empty(record)
                    return  # systemd removed the cgroup after its final member left.
                raise MemoryScopeError(f"head scope {scope_unit(self.run_id)} has no membership evidence")
            except OSError as exc:
                raise MemoryScopeError(f"could not verify empty head scope {scope_unit(self.run_id)}: {exc}") from exc
            if "populated 0" in fields:
                self._record_empty(record)
                return  # cgroup.events counts descendants, including separate process groups.
            if time.monotonic() >= deadline:
                raise MemoryScopeError(f"head scope {scope_unit(self.run_id)} still has members")
            time.sleep(0.05)

    def _record_empty(self, record: dict[str, Any]) -> None:
        if self.directory is None:
            return
        record["cleanup_complete"] = True
        self.update_owner(self.directory, record)

    def verify_self(self) -> ScopeEvidence:
        """Run before `run.started`; a supervisor outside its configured scope refuses launch."""
        cgroup = own_cgroup()
        expected = str(self.limit_mib * 1024 * 1024)
        try:
            actual = (cgroup / "memory.max").read_text(encoding="ascii").strip() if cgroup else ""
            swap_max = (cgroup / "memory.swap.max").read_text(encoding="ascii").strip() if cgroup else ""
            oom_group = (cgroup / "memory.oom.group").read_text(encoding="ascii").strip() if cgroup else ""
        except OSError:
            actual = ""
            swap_max = ""
            oom_group = ""
        before = memory_events(cgroup)
        if (
            cgroup is None or cgroup.name != scope_unit(self.run_id)
            or actual != expected or swap_max != "0" or oom_group != "1" or before is None
            or not supervisor_oom_protected()
        ):
            raise MemoryScopeError(
                f"head scope {scope_unit(self.run_id)} did not materialize "
                f"MemoryMax={expected}, MemorySwapMax=0, memory.oom.group=1 and a protected supervisor"
            )
        return ScopeEvidence(cgroup, before)

    @staticmethod
    def exit_fields(
        status: int, evidence: ScopeEvidence, *, stopping: bool = False,
        oom_victim: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        """Only a kernel kill record for the reserved head PID establishes an OOM."""
        signal_number = os.WTERMSIG(status) if os.WIFSIGNALED(status) else None
        fields: dict[str, Any] = {
            "signal": signal_number,
            "exit_code": os.WEXITSTATUS(status) if os.WIFEXITED(status) else None,
        }
        if not stopping and signal_number == 9 and oom_victim is not None:
            fields["head_loss_reason"] = MEMORY_LIMIT_REASON
            fields["oom_victim"] = oom_victim
        return fields
