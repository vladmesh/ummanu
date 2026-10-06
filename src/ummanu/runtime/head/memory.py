"""Per-head systemd scope limit and cgroup v2 OOM evidence."""

from __future__ import annotations

import hashlib
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MEMORY_LIMIT_MIB = 8192
MEMORY_LIMIT_REASON = "memory_limit"
CGROUP_ROOT = Path("/sys/fs/cgroup")


class MemoryScopeError(RuntimeError):
    """A head's own system scope did not materialize its memory ceiling."""


@dataclass(frozen=True)
class ScopeEvidence:
    cgroup: Path
    before: dict[str, int]


def memory_limit_mib(value: object, profile_id: str) -> int:
    """Accept a positive integer MiB value; bool is not an integer configuration value."""
    if type(value) is not int or not 1 <= value <= 1048576:
        raise ValueError(f"profile {profile_id!r} memory_limit_mib must be an integer from 1 to 1048576")
    return value


def scope_unit(run_id: str) -> str:
    # The hash also keeps arbitrary run ids out of a unit name.
    return f"ummanu-head-{hashlib.sha256(run_id.encode()).hexdigest()[:24]}.scope"


def scope_argv(
    run_id: str, limit_mib: int, command: list[str], *, pythonpath: str = "",
    owner_unit: str = "",
) -> list[str]:
    """Register a system scope, then run its payload as the original runtime user."""
    groups = os.getgroups()
    group_option = f"--groups={','.join(str(group) for group in groups)}" if groups else "--clear-groups"
    return [
        "sudo", "-n", "systemd-run", "--system", "--scope", "--quiet",
        "--unit", scope_unit(run_id),
        f"--property=MemoryMax={limit_mib * 1024 * 1024}",
        "--property=MemorySwapMax=0", "--property=Delegate=yes",
        *([f"--property=BindsTo={owner_unit}", f"--property=After={owner_unit}"] if owner_unit else []),
        "--",
        sys.executable, "-I", str(Path(__file__).resolve().parent / "local_pty" / "scope_bootstrap.py"),
        f"--reuid={os.getuid()}", f"--regid={os.getgid()}", group_option, "--",
        *command,
    ]


def own_cgroup() -> Path | None:
    """Return this process's unified cgroup, only if it is beneath the cgroup mount."""
    try:
        lines = Path("/proc/self/cgroup").read_text(encoding="ascii").splitlines()
    except OSError:
        return None
    for line in lines:
        if line.startswith("0::"):
            path = (CGROUP_ROOT / line[3:].lstrip("/")).resolve()
            if path.is_relative_to(CGROUP_ROOT):
                return path
    return None


def memory_events(cgroup: Path | None) -> dict[str, int] | None:
    if cgroup is None:
        return None
    try:
        fields = (cgroup / "memory.events.local").read_text(encoding="ascii").splitlines()
        parsed = {parts[0]: int(parts[1]) for line in fields if len(parts := line.split()) == 2}
        return {key: parsed[key] for key in ("max", "oom_kill", "oom_group_kill")}
    except (OSError, KeyError, ValueError):
        return None


def supervisor_oom_protected() -> bool:
    try:
        return Path("/proc/self/oom_score_adj").read_text(encoding="ascii").strip() == "-1000"
    except OSError:
        return False


OOM_STREAM_ENV = "UMMANU_OOM_STREAM_FD"


def open_oom_stream() -> int:
    """Bootstrap opens the kernel producer before fork, with no historical records.

    Only the trusted supervisor receives this read-only descriptor. It is closed in the
    head child before exec. Failure is a launch refusal, not a silently unobservable OOM.
    """
    fd = os.open("/dev/kmsg", os.O_RDONLY | os.O_NONBLOCK)
    try:
        os.lseek(fd, 0, os.SEEK_END)
        os.set_inheritable(fd, True)
    except BaseException:
        os.close(fd)
        raise
    return fd


def read_oom_victim(fd: int, head_pid: int) -> dict[str, int] | None:
    """Consume kernel kill records while waitid(WNOWAIT) reserves this child's PID.

    The kernel prints the record before the victim can exit, so it is readable at waitid return.
    Only a kernel-facility "Killed process <head_pid>" record counts; counters, selection summaries
    and oom_reaper lines do not. Overflow or an unreadable stream invalidates the observation. See
    docs/HEAD_SCOPES.md "Causal OOM producer and consumer".
    """
    victim = None
    for _ in range(16384):  # bounded even if the kernel is continuously logging
        try:
            record = os.read(fd, 8192).decode("utf-8", errors="replace")
        except BlockingIOError:
            return victim
        except OSError:
            return None
        header, separator, message = record.partition(";")
        match = re.match(r"Memory cgroup out of memory: Killed process (\d+) ", message)
        if separator and match and int(match[1]) == head_pid:
            try:
                priority, sequence, timestamp, *_ = header.split(",")
                if int(priority) >= 8:  # userspace kmsg injection has a non-kernel facility
                    continue
                victim = {"pid": head_pid, "kernel_seq": int(sequence), "kernel_usec": int(timestamp)}
            except ValueError:
                return None
    return None
