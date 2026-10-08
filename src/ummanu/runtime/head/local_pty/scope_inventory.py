"""Read-only projection of canonical scope owners, fenced to a native scope incarnation.

Creates no files and takes only shared locks on existing owner locks; a retained owner alone does
not prove a systemd unit still belongs to that launch. See docs/HEAD_RUNTIME.md "Runtime scopes in
host reconciliation".
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
import subprocess
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..memory import CGROUP_ROOT, MemoryScopeError
from . import protocol
from .journal import RUN_STARTED, SCOPE_BOUND, read_events
from .scoped_lifecycle import ScopedHeadLifecycle, binding_description, launch_binding, native_scope_state


@dataclass(frozen=True)
class RuntimeScopeInventory:
    data_dir: Path
    observed: frozenset[str]
    scopes: dict[str, dict[str, Any]] = field(default_factory=dict)
    disappeared: frozenset[str] = frozenset()
    errors: dict[str, str] = field(default_factory=dict)

    def revalidate(self) -> RuntimeScopeInventory:
        """Refresh before apply; an old snapshot never blesses a replacement."""
        fresh = read_runtime_scopes(self.data_dir, set(self.observed))
        errors = dict(fresh.errors)
        for unit, scope in self.scopes.items():
            current = fresh.scopes.get(unit)
            if unit not in fresh.disappeared and (
                current is None or current["identity"] != scope["identity"]
            ):
                errors[unit] = "runtime scope identity changed since inventory; retry inspection"
        return RuntimeScopeInventory(fresh.data_dir, fresh.observed, fresh.scopes,
                                     fresh.disappeared, errors)


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _open(stack: ExitStack, name: str | Path, *, parent: int | None = None,
          directory: bool = False, uid: int | None = None) -> int:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK |
                 (os.O_DIRECTORY if directory else 0), dir_fd=parent)
    stack.callback(os.close, fd)
    info = os.fstat(fd)
    if (not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1)
            or uid is not None and info.st_uid != uid or info.st_mode & 0o022):
        raise MemoryScopeError("runtime ownership path is not private to the installation owner")
    return fd


def _directory(stack: ExitStack, path: Path) -> int:
    """Anchor every path component, refusing symlinks instead of resolving them."""
    if not path.is_absolute():
        raise MemoryScopeError("runtime inventory needs an absolute selected data root")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    stack.callback(os.close, fd)
    for part in path.parts[1:]:
        if part in (".", ".."):
            raise MemoryScopeError("runtime ownership path escapes the selected data root")
        # Shared ancestors such as /tmp need not belong to the runtime account.
        fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        stack.callback(os.close, fd)
    return fd


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise MemoryScopeError("runtime ownership JSON has duplicate fields")
        result[key] = value
    return result


def _json(fd: int) -> Any:
    with os.fdopen(os.dup(fd), "r", encoding="utf-8") as stream:
        return json.load(stream, object_pairs_hook=_unique)


def _unit_state(unit: str) -> dict[str, str]:
    return native_scope_state(unit)


def _names(root: int, remaining: Callable[[], float] | None) -> Iterator[str]:
    """`os.listdir(root)` as the native iterator yields it, a caller's deadline checked between
    entries: a scan it cuts short raises, so absence is never read from a partial listing."""
    if remaining is None:
        yield from os.listdir(root)
        return
    with os.scandir(root) as scan:
        for entry in scan:
            if remaining() <= 0:
                raise MemoryScopeError("the caller's deadline passed while runtime scope owners were read")
            yield entry.name


def _unit_state_within(remaining: Callable[[], float]) -> Callable[[str], dict[str, str]]:
    """`_unit_state` with each `systemctl show` cut to what is left of a caller's deadline."""
    def state(unit: str) -> dict[str, str]:
        left = remaining()
        if left <= 0:
            raise MemoryScopeError("the caller's deadline passed before native scope observation")
        return native_scope_state(unit, timeout=min(10.0, left))
    return state


def _absent(state: dict[str, str]) -> bool:
    return (state["LoadState"] == "not-found" and state["ActiveState"] == "inactive"
            and not state["ControlGroup"])


def _membership(group: Path) -> tuple[tuple[int, int], bool]:
    info = group.stat()
    if group.is_symlink() or group.resolve() != group:
        raise MemoryScopeError("runtime cgroup path was substituted")
    fields = dict(line.split() for line in (group / "cgroup.events").read_text().splitlines())
    if fields.get("populated") not in {"0", "1"}:
        raise MemoryScopeError("runtime scope has no recursive membership evidence")
    return _identity(info), fields["populated"] == "1"


def _journal_proof(stack: ExitStack, fd: int, uid: int, record: dict[str, Any],
                   state: dict[str, str], directory: Path, native: tuple[int, int]) -> None:
    """One launch-time attestation, independent of the mutable owner or live PIDs."""
    journal = _open(stack, protocol.JOURNAL_NAME, parent=fd, uid=uid)
    with os.fdopen(os.dup(journal), "r", encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                raw = json.loads(line, object_pairs_hook=_unique)
                if (not isinstance(raw, dict) or type(raw.get("schema_version")) is not int
                        or type(raw.get("seq")) is not int):
                    raise MemoryScopeError("runtime journal ownership evidence has invalid field types")
    events = read_events(Path(f"/proc/self/fd/{journal}"))
    if events.malformed or not events.ordered or events.truncated_tail:
        raise MemoryScopeError("runtime journal ownership evidence is damaged")
    bindings = events.of_kind(SCOPE_BOUND)
    if not bindings:
        raise MemoryScopeError("runtime scope lacks launch-time generation/workspace attestation; "
                               "retry after normal lifecycle settlement or supported handoff; do not adopt or backfill")
    current = bindings[-1]
    proof = current.get("binding")
    admitted = launch_binding(record, directory)
    if (current.get("run_id") != record["run_id"] or not isinstance(proof, dict)
            or set(proof) != {"admitted", "invocation_id", "activation", "cgroup"}
            or not isinstance(proof.get("admitted"), dict)
            or proof.get("admitted") != admitted
            or proof.get("invocation_id") != state["InvocationID"]
            or proof.get("activation") != state["ActiveEnterTimestampMonotonic"]
            or proof.get("cgroup") != list(native)
            or not all(type(value) is int for value in proof["cgroup"])
            or state.get("Description") != binding_description(admitted)
            or state.get("Description") != binding_description(proof["admitted"])
            or sum(event.get("binding", {}).get("invocation_id") == state["InvocationID"]
                   for event in bindings if isinstance(event.get("binding"), dict)) != 1):
        raise MemoryScopeError("runtime launch attestation does not match canonical ownership or native incarnation")
    boot, ticks = record["launch_identity"].rsplit(":", 1)
    hz = os.sysconf("SC_CLK_TCK")
    activation = int(state["ActiveEnterTimestampMonotonic"])
    launch_time = int(ticks) * 1_000_000 // hz
    if (boot != Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            or activation < launch_time):
        raise MemoryScopeError("runtime launch attestation belongs to a different boot or activation")
    starts = tuple(event for event in events.of_kind(RUN_STARTED) if event["seq"] > current["seq"])
    if not starts:
        return  # The fsynced pre-head binding also covers crash before run.started.
    if len(starts) != 1:
        raise MemoryScopeError("multiple starts borrow one scope launch attestation; retry lifecycle recovery")
    started = starts[-1]
    if started.get("socket_path") != str(directory / protocol.SOCKET_NAME):
        raise MemoryScopeError("runtime journal belongs to a different canonical data root")
    for key in ("run_id", "role", "task"):
        if started.get(key) != record[key]:
            raise MemoryScopeError("runtime journal does not match canonical ownership")
    # The journal names the deployed heartbeat location, which can be outside
    # the run directory. Anchor and validate that pointer as well.
    heartbeat_path = Path(started.get("pid_file", ""))
    heartbeat_parent = _directory(stack, heartbeat_path.parent)
    heartbeat = _json(_open(stack, heartbeat_path.name, parent=heartbeat_parent, uid=uid))
    if not isinstance(heartbeat, dict):
        raise MemoryScopeError("runtime heartbeat ownership evidence is not an object")
    for key in ("run_id", "role", "task"):
        if heartbeat.get(key) != record[key]:
            raise MemoryScopeError("runtime heartbeat does not match canonical ownership")
    current_boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    if (type(heartbeat.get("version")) is not int or heartbeat.get("version") != 1
            or type(heartbeat.get("pid")) is not int or heartbeat["pid"] <= 0
            or heartbeat.get("pid") != started.get("head_pid")
            or boot != current_boot or heartbeat.get("boot_id") != boot):
        raise MemoryScopeError("runtime launch belongs to a different native identity or boot")
    head_time = int(heartbeat["proc_starttime_ticks"]) * 1_000_000 // hz
    # /proc birth time is rounded down to a kernel tick. The exact native
    # incarnation is already independently fenced by InvocationID and inode.
    if not launch_time <= activation < head_time + (1_000_000 + hz - 1) // hz:
        raise MemoryScopeError("native scope incarnation is outside the recorded launch; possible unit reuse")


def read_runtime_scopes(data_dir: Path, observed: set[str], *,
                        remaining: Callable[[], float] | None = None) -> RuntimeScopeInventory:
    """Project all runtime roots for this installation, without following PO pointers."""
    unit_state = _unit_state if remaining is None else _unit_state_within(remaining)
    original = frozenset(observed)
    wanted = frozenset(name for name in original if name.endswith(".scope"))
    scopes: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    disappeared: set[str] = set()
    owners: dict[str, tuple[Path, dict[str, Any]]] = {}
    if not wanted:
        return RuntimeScopeInventory(data_dir, original)
    try:
        with ExitStack() as stack:
            data_fd = _directory(stack, data_dir)
            uid = os.fstat(data_fd).st_uid
            if os.fstat(data_fd).st_mode & 0o022:
                raise MemoryScopeError("selected runtime data root is writable by another owner")
            for relative in (Path("heads"), Path("po-heads"), Path("webproto/heads")):
                try:
                    root = _directory(stack, data_dir / relative)
                except FileNotFoundError:
                    continue
                root_info = os.fstat(root)
                if root_info.st_uid != uid or root_info.st_mode & 0o022:
                    raise MemoryScopeError("canonical runtime root is not private to this installation owner")
                for name in _names(root, remaining):
                    info = os.stat(name, dir_fd=root, follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode):
                        raise MemoryScopeError("canonical runtime directory is a symlink")
                    if not stat.S_ISDIR(info.st_mode):
                        continue
                    with ExitStack() as owner_stack:
                        directory = data_dir / relative / name
                        fd = _open(owner_stack, name, parent=root, directory=True, uid=uid)
                        try:
                            owner_fd = _open(owner_stack, "scope-owner.json", parent=fd, uid=uid)
                        except FileNotFoundError:
                            continue  # genuinely deployed unscoped records are not scope owners
                        record = ScopedHeadLifecycle.validate_owner(_json(owner_fd))
                        if protocol.run_dir_for(directory.parent, record["run_id"]) != directory:
                            raise MemoryScopeError("canonical owner directory does not match its run identity")
                        unit = record["unit"]
                        if unit in owners:
                            raise MemoryScopeError("duplicate canonical runtime scope owners")
                        owners[unit] = directory, record
                        if unit not in wanted:
                            continue
                        lock = _open(owner_stack, "scope-owner.lock", parent=fd, uid=uid)
                        fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                        # Admission/cleanup writers cannot change the owner while this
                        # observation is made. Check substitutions which ignore the lock.
                        locked_record = ScopedHeadLifecycle.read_owner(Path(f"/proc/self/fd/{fd}"))
                        if locked_record != record:
                            raise MemoryScopeError("runtime owner changed before its observation lock")
                        state = unit_state(unit)
                        if _absent(state):
                            if unit_state(unit) != state or ScopedHeadLifecycle.read_owner(directory) != record:
                                raise MemoryScopeError("runtime scope changed during disappearance observation; retry inspection")
                            disappeared.add(unit)
                            continue
                        if (state["Id"] != unit or state["LoadState"] != "loaded"
                                or state["Transient"] != "yes" or not state["InvocationID"]
                                or state["ControlGroup"] != f"/system.slice/{unit}"):
                            raise MemoryScopeError("unit is not the canonical native transient scope")
                        if any(not isinstance(record.get(key), str) or not record[key]
                               for key in ("role", "task", "workspace")):
                            raise MemoryScopeError("runtime owner has incomplete role/task/workspace identity")
                        if not Path(record["workspace"]).is_absolute() or "launch_pid" not in record:
                            raise MemoryScopeError("runtime owner has no native generation launch proof")
                        group = CGROUP_ROOT / "system.slice" / unit
                        native, populated = _membership(group)
                        if record["cleanup_complete"] and populated:
                            raise MemoryScopeError("a populated runtime scope falsely claims completed cleanup")
                        _journal_proof(owner_stack, fd, uid, record, state, directory, native)
                        after = unit_state(unit)
                        if _absent(after):
                            disappeared.add(unit)
                            continue
                        native_after, populated = _membership(group)
                        if record["cleanup_complete"] and populated:
                            raise MemoryScopeError("a completed runtime scope gained members during inspection")
                        _journal_proof(owner_stack, fd, uid, record, after, directory, native_after)
                        if (after != state or native_after != native
                                or ScopedHeadLifecycle.read_owner(directory) != record
                                or _identity(os.fstat(_directory(owner_stack, directory))) != _identity(os.fstat(fd))
                                or _identity((directory / "scope-owner.json").lstat()) != _identity(os.fstat(owner_fd))
                                or _identity(os.fstat(_directory(owner_stack, data_dir))) != _identity(os.fstat(data_fd))):
                            raise MemoryScopeError("runtime ownership or native scope changed during inspection")
                        scopes[unit] = {
                            "run_id": record["run_id"], "generation": record["generation"],
                            "role": record["role"], "task": record["task"], "workspace": record["workspace"],
                            "launch_allowed": record["launch_allowed"], "cleanup_complete": record["cleanup_complete"],
                            "populated": populated, "binds_to": state.get("BindsTo", "").split(),
                            "active": state["ActiveState"], "owner": str(directory / "scope-owner.json"),
                        "identity": (record["run_id"], record["generation"], record["role"], record["task"],
                                         record["workspace"], record["launch_pid"], record["launch_identity"],
                                         _identity(os.fstat(fd)), native, state["InvocationID"],
                                         tuple(state.get("BindsTo", "").split())),
                        }
            # Missing ownership cannot establish absence. Refresh every observed
            # ownerless name too, including a name omitted by an earlier snapshot.
            for unit in wanted - owners.keys():
                state = unit_state(unit)
                after = unit_state(unit)
                if after != state:
                    raise MemoryScopeError("ownerless runtime unit changed during inspection; retry observation")
                if _absent(after):
                    disappeared.add(unit)
    except (OSError, ValueError, KeyError, IndexError, TypeError, RuntimeError, subprocess.SubprocessError) as exc:
        # No partial success from an ambiguous ownership inventory. Diagnostics
        # never include command lines, environment, journal payloads or outcomes.
        scopes.clear()
        errors["runtime_scopes"] = str(exc) if isinstance(exc, MemoryScopeError) else (
            f"{type(exc).__name__} reading canonical runtime ownership or native membership; "
            "check selected data-root permissions and systemd access, then retry inspection")
    return RuntimeScopeInventory(data_dir, original, scopes, frozenset(disappeared), errors)
