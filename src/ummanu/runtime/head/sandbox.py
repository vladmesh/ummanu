"""Confine one head's process tree to its workspace: Landlock for files, a connect() broker for sockets.

Run by absolute path under `-I` in front of the adapter command, outside the head and before it. The
launcher stays the parent: the child gets no_new_privs, a Landlock ruleset (read and execute beneath
the read roots, full access beneath the write roots, nothing else, and no ptrace of processes outside
the ruleset) and a seccomp filter that hands every connect() to the launcher. The launcher allows a
Unix socket beneath a write root, loopback TCP to the board, the memory service or a listener inside
the sandbox, and any other network address. A kernel without Landlock or seccomp user notification
refuses the launch; nothing ever runs unconfined.

What a head reads: the system, its toolchain, the product checkout, its workspace and the git store
the workspace belongs to, the installation's configuration, and the roots its profile names. What
it writes: the workspace, its own Claude config dir and temp dir, the cargo caches, its transcript
directory and the board's lock files. The rest of the home (other projects, agent profiles, other
transcripts, installation state) is refused by the kernel.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import errno
import fcntl
import hashlib
import ipaddress
import json
import os
import platform
import select
import shutil
import signal
import socket
import stat
import struct
import sys
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

# Executed by absolute path under -I: only this product's source is importable.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from ummanu.runtime.procfs import proc_stat_fields

# Landlock filesystem rights (uapi/linux/landlock.h) and the ABI that introduced each.
FS_EXECUTE = 1 << 0
FS_WRITE_FILE = 1 << 1
FS_READ_FILE = 1 << 2
FS_READ_DIR = 1 << 3
FS_REFER = 1 << 13
FS_TRUNCATE = 1 << 14
FS_IOCTL_DEV = 1 << 15
_FS_ABI1 = (1 << 13) - 1
_FS_BY_ABI = {1: _FS_ABI1, 2: _FS_ABI1 | FS_REFER, 3: _FS_ABI1 | FS_REFER | FS_TRUNCATE}
_FS_ABI5 = _FS_BY_ABI[3] | FS_IOCTL_DEV
FILE_RIGHTS = FS_EXECUTE | FS_WRITE_FILE | FS_READ_FILE | FS_TRUNCATE | FS_IOCTL_DEV
READ_RIGHTS = FS_EXECUTE | FS_READ_FILE | FS_READ_DIR
DEVICE_RIGHTS = FS_READ_FILE | FS_WRITE_FILE | FS_READ_DIR | FS_TRUNCATE | FS_IOCTL_DEV
PROC_RIGHTS = FS_READ_FILE | FS_WRITE_FILE | FS_READ_DIR
# ABI 6 scoping: no signals to, and no abstract Unix sockets of, processes outside the sandbox.
SCOPE_ABSTRACT_UNIX = 1 << 0
SCOPE_SIGNAL = 1 << 1
RULE_PATH_BENEATH = 1
CREATE_RULESET_VERSION = 1 << 0

PR_SET_NO_NEW_PRIVS = 38
PR_SET_CHILD_SUBREAPER = 36
PR_SET_PDEATHSIG = 1
SECCOMP_SET_MODE_FILTER = 1
SECCOMP_GET_NOTIF_SIZES = 3
SECCOMP_FILTER_FLAG_NEW_LISTENER = 1 << 3
SECCOMP_RET_ALLOW = 0x7FFF0000
SECCOMP_RET_ERRNO = 0x00050000
SECCOMP_RET_USER_NOTIF = 0x7FC00000
SECCOMP_USER_NOTIF_FLAG_CONTINUE = 1
NOTIF_RECV = 0xC0502100
NOTIF_SEND = 0xC0182101
NOTIF_ID_VALID = 0x40082102

_ARCHES = {
    # audit arch, landlock_create_ruleset, seccomp, connect, io_uring_setup
    "x86_64": (0xC000003E, 444, 317, 42, 425),
    "aarch64": (0xC00000B7, 444, 277, 203, 425),
}
X32_SYSCALL_BIT = 0x40000000

SYSTEM_READ = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32", "/etc", "/opt",
               "/run/systemd/resolve", "/sys")
HOME_READ = (".local/bin", ".local/share/claude", ".rustup", ".cargo/bin", ".cargo/env",
             ".cargo/config.toml", ".local/state/ummanu/head-config")
HOME_WRITE = (".cargo/registry", ".cargo/git")
INSTANCE_READ = ("instance.yaml", "board-store.env", "heads", "projects", "skills", "adapters",
                 "CONTEXT.md", "README.md")
# The board CLI's lock files and journals beneath the data directory (made before the ruleset).
DATA_WRITE_FILES = ("board/.create.lock", "board/.audit.lock", "dispatcher/board-bulk.lock",
                    "dispatcher/cleanup.lock")
DATA_WRITE_DIRS = ("board/assessment-decisions", "board/sql-sprint-locks", "board/product-issue-transactions",
                   "dispatcher/effects", "dispatcher/admission", "dispatcher/lifecycle")
DATA_READ = ("dispatcher/production-state.json", "dispatcher/cleanup")
# Unix sockets outside the write roots a head may still reach: name resolution only.
ALLOWED_SOCKETS = ("/run/systemd/resolve/io.systemd.Resolve",)
# The memory service every head's MCP config names.
MEMORY_PORT = 8077
# Variables that would point a head at the owner's account, agent or bus.
STRIPPED_ENV = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "SSH_AUTH_SOCK",
                "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR", "GPG_AGENT_INFO")
CLAUDE_SETTINGS = {"skipDangerousModePermissionPrompt": True, "theme": "dark",
                   "env": {"DISABLE_AUTOUPDATER": "1"}}
# The sandbox's own state, beside the head git config the role env already keeps there.
STATE_DIR = Path(".local") / "state" / "ummanu" / "head-sandbox"
# Short, so a test that binds a Unix socket under TMPDIR stays within the 108-byte path limit.
TMP_ROOT = Path("/tmp")


class SandboxError(RuntimeError):
    """The sandbox cannot be built as asked; the head must not start without it."""


@dataclass(frozen=True)
class Policy:
    read: tuple[Path, ...]
    write: tuple[Path, ...]
    devices: tuple[Path, ...] = (Path("/dev"),)
    proc: tuple[Path, ...] = (Path("/proc"),)
    loopback_ports: frozenset[int] = frozenset()
    sockets: tuple[Path, ...] = field(default=tuple(Path(p) for p in ALLOWED_SOCKETS))

    def socket_allowed(self, path: Path) -> bool:
        return path in self.sockets or any(path.is_relative_to(root) for root in self.write)

    def refuse_home_roots(self, home: Path) -> None:
        """A root that is the home or holds it would open every file the sandbox exists to hide."""
        for root in (*self.read, *self.write):
            if home.is_relative_to(root):
                raise SandboxError(f"{root} holds the home directory {home}; refusing to expose it")


def git_paths(workspace: Path) -> tuple[list[Path], list[Path]]:
    """Read and write roots for the repository a workspace is a linked worktree of.

    The shared git dir stays read-only apart from objects, refs, logs and this worktree's own admin
    directory, so a head can commit but cannot plant config or hooks the host later runs. The main
    checkout's working tree is not a root: the head has its own.
    """
    marker = workspace / ".git"
    if not marker.is_file():
        return [], []
    text = marker.read_text(encoding="utf-8").strip()
    if not text.startswith("gitdir:"):
        raise SandboxError(f"{marker} is not a gitdir pointer")
    gitdir = (workspace / text.removeprefix("gitdir:").strip()).resolve()
    common_file = gitdir / "commondir"
    common = (gitdir / common_file.read_text(encoding="utf-8").strip()).resolve() if common_file.is_file() else gitdir
    read = [common]
    write = [gitdir, *(common / name for name in ("objects", "refs", "logs", "FETCH_HEAD"))]
    return read, write


def board_port(instance: Path) -> int | None:
    """The board store's port when it is served on loopback, from the instance's `board-store.env`."""
    path = instance / "board-store.env"
    if not path.is_file():
        return None
    values = dict(
        line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines() if "=" in line
    )
    host = values.get("UMMANU_DB_HOST", "").strip()
    port = values.get("UMMANU_DB_PORT", "").strip()
    if not port.isdigit() or not _loopback_host(host):
        return None
    return int(port)


def _loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def claude_project_dir_name(workspace: Path) -> str:
    """The `projects/` directory name Claude Code gives a workspace (every non-alphanumeric -> '-')."""
    return "".join(ch if ch.isascii() and ch.isalnum() else "-" for ch in str(workspace))


def build_policy(
    workspace: Path,
    *,
    home: Path,
    environ: Mapping[str, str],
    product_root: Path,
    private: Sequence[Path] = (),
    data_dir: Path | None = None,
    extra_read: Sequence[Path] = (),
    loopback_ports: Iterable[int] = (),
) -> Policy:
    """The roots one head may read and write; missing paths are dropped, nothing else is reachable.

    `private` are the sandbox's own writable directories (its Claude config dir and temp dir).
    """
    read = [Path(p) for p in SYSTEM_READ]
    read += [home / p for p in HOME_READ]
    read += [product_root, *extra_read]
    write = [workspace, *private, *(home / p for p in HOME_WRITE)]
    write.append(home / ".claude" / "projects" / claude_project_dir_name(workspace))
    write.append(Path("/dev/shm"))
    repo_read, repo_write = git_paths(workspace)
    read += repo_read
    write += repo_write
    ports = {MEMORY_PORT, *loopback_ports}
    instance = environ.get("UMMANU_INSTANCE", "").strip()
    if instance:
        read += [Path(instance) / name for name in INSTANCE_READ]
        port = board_port(Path(instance))
        if port is not None:
            ports.add(port)
    if data_dir is not None:
        read += [data_dir / name for name in DATA_READ]
        write += [data_dir / name for name in (*DATA_WRITE_FILES, *DATA_WRITE_DIRS)]
    policy = Policy(
        read=tuple(dict.fromkeys(p for p in read if p.exists())),
        write=tuple(dict.fromkeys(p for p in write if p.exists())),
        loopback_ports=frozenset(ports),
    )
    policy.refuse_home_roots(home)
    return policy


def prepare_data_locks(data_dir: Path) -> None:
    """Create the board's lock files and directories the confined head cannot create itself."""
    for name in DATA_WRITE_DIRS:
        (data_dir / name).mkdir(parents=True, exist_ok=True)
    for name in DATA_WRITE_FILES:
        path = data_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileExistsError):
            os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644))


def installation_data_dir(instance: str) -> Path | None:
    """The data directory the installation's `instance.yaml` names, read by the product's own parser."""
    if not instance:
        return None
    from ummanu.config import instance_data_dir

    return instance_data_dir(Path(instance))


def private_tmp(workspace: Path) -> Path:
    """This workspace's own temp dir, `/tmp/ummanu-sandbox-<uid>/<digest>`, owned by us and 0700."""
    parent = TMP_ROOT / f"ummanu-sandbox-{os.getuid()}"
    tmp = parent / hashlib.sha256(str(workspace).encode()).hexdigest()[:16]
    for path in (parent, tmp):
        with contextlib.suppress(FileExistsError):
            path.mkdir(mode=0o700)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise SandboxError(f"{path} is not a private directory of this user")
    return tmp


def role_skills(product_root: Path, instance: str) -> set[str]:
    """Skill names the product and installation manifests declare; personal skills stay out."""
    names: set[str] = set()
    manifests = [product_root / "skills" / "manifest.toml"]
    if instance:
        manifests.append(Path(instance) / "skills" / "manifest.toml")
    for manifest in manifests:
        try:
            roles = tomllib.loads(manifest.read_text(encoding="utf-8")).get("roles", {})
        except (OSError, tomllib.TOMLDecodeError):
            continue
        for role in roles.values():
            names.update(str(skill).rsplit("/", 1)[-1] for skill in role.get("skills", ()))
    return names


def prepare_claude_home(
    claude_home: Path, workspace: Path, *, home: Path, skills: Sequence[Path]
) -> None:
    """A Claude config dir holding only this sandbox's state: settings, trust, global rules, skills.

    Its `projects/<workspace>` links to the same directory under `~/.claude/projects`, so the
    product's transcript readers find the session where they always look.
    """
    claude_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write_json(claude_home / "settings.json", CLAUDE_SETTINGS)
    state_path = claude_home / ".claude.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    state.update({"hasCompletedOnboarding": True, "theme": state.get("theme") or "dark",
                  "bypassPermissionsModeAccepted": True})
    state.setdefault("projects", {}).setdefault(str(workspace), {})["hasTrustDialogAccepted"] = True
    _write_json(state_path, state)
    rules = home / ".claude" / "CLAUDE.md"
    if rules.is_file():
        shutil.copyfile(rules, claude_home / "CLAUDE.md")
    skills_dir = claude_home / "skills"
    skills_dir.mkdir(exist_ok=True)
    for source in skills:
        _link(skills_dir / source.name, source)
    name = claude_project_dir_name(workspace)
    transcripts = home / ".claude" / "projects" / name
    transcripts.mkdir(mode=0o700, parents=True, exist_ok=True)
    (claude_home / "projects").mkdir(exist_ok=True)
    _link(claude_home / "projects" / name, transcripts)


def _write_json(path: Path, value: object) -> None:
    text = json.dumps(value, indent=2, sort_keys=True) + "\n"
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _link(link: Path, target: Path) -> None:
    if link.is_symlink() and Path(os.readlink(link)) == target:
        return
    if link.is_symlink():
        link.unlink()
    link.symlink_to(target)


# --- kernel interfaces -------------------------------------------------------------------------

_libc = ctypes.CDLL(None, use_errno=True)
_libc.syscall.restype = ctypes.c_long


def _arch() -> tuple[int, int, int, int, int]:
    machine = platform.machine()
    if machine not in _ARCHES:
        raise SandboxError(f"no sandbox support for the {machine} architecture")
    return _ARCHES[machine]


def _syscall(number: int, *args: object) -> int:
    result = _libc.syscall(number, *args)
    if result < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return result


def landlock_abi() -> int:
    """The running kernel's Landlock ABI version, 0 when Landlock is unavailable."""
    try:
        return _syscall(_arch()[1], None, ctypes.c_size_t(0), ctypes.c_uint32(CREATE_RULESET_VERSION))
    except OSError:
        return 0


def _handled_fs(abi: int) -> int:
    return _FS_ABI5 if abi >= 5 else _FS_BY_ABI[min(abi, 3)]


def apply_landlock(policy: Policy) -> None:
    """Restrict this process (and its future children) to the policy; irreversible."""
    abi = landlock_abi()
    if abi < 1:
        raise SandboxError("the kernel offers no Landlock")
    create = _arch()[1]
    handled = _handled_fs(abi)
    if abi >= 6:
        attr = struct.pack("<QQQ", handled, 0, SCOPE_ABSTRACT_UNIX | SCOPE_SIGNAL)
    elif abi >= 4:
        attr = struct.pack("<QQ", handled, 0)
    else:
        attr = struct.pack("<Q", handled)
    buffer = ctypes.create_string_buffer(attr, len(attr))
    ruleset = _syscall(create, buffer, ctypes.c_size_t(len(attr)), ctypes.c_uint32(0))
    try:
        for paths, rights in ((policy.read, READ_RIGHTS), (policy.write, handled),
                              (policy.devices, DEVICE_RIGHTS), (policy.proc, PROC_RIGHTS)):
            for path in paths:
                _add_rule(create + 1, ruleset, path, rights & handled)
        _syscall(create + 2, ctypes.c_int(ruleset), ctypes.c_uint32(0))
    finally:
        os.close(ruleset)


def _add_rule(add_rule: int, ruleset: int, path: Path, rights: int) -> None:
    fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
    try:
        if not os.path.isdir(f"/proc/self/fd/{fd}"):
            rights &= FILE_RIGHTS
        attr = struct.pack("<Qi", rights, fd)
        buffer = ctypes.create_string_buffer(attr, len(attr))
        _syscall(add_rule, ctypes.c_int(ruleset), ctypes.c_int(RULE_PATH_BENEATH), buffer, ctypes.c_uint32(0))
    finally:
        os.close(fd)


class _Filter(ctypes.Structure):
    _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte), ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint32)]


class _Program(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(_Filter))]


def _connect_filter() -> list[tuple[int, int, int, int]]:
    """cBPF: foreign ABIs refused, connect() to the broker, io_uring refused, the rest allowed."""
    audit_arch, _, _, connect, io_uring_setup = _arch()
    load, jeq, jge, ret = 0x20, 0x15, 0x35, 0x06
    return [
        (load, 0, 0, 4),
        (jeq, 0, 7, audit_arch),
        (load, 0, 0, 0),
        (jge, 5, 0, X32_SYSCALL_BIT),
        (jeq, 3, 0, connect),
        (jeq, 1, 0, io_uring_setup),
        (ret, 0, 0, SECCOMP_RET_ALLOW),
        (ret, 0, 0, SECCOMP_RET_ERRNO | errno.ENOSYS),
        (ret, 0, 0, SECCOMP_RET_USER_NOTIF),
        (ret, 0, 0, SECCOMP_RET_ERRNO | errno.EPERM),
    ]


def install_connect_filter() -> int:
    """Install the filter on this process and return its notification listener."""
    program = _connect_filter()
    filters = (_Filter * len(program))(*(_Filter(*item) for item in program))
    fprog = _Program(len(program), filters)
    return _syscall(_arch()[2], ctypes.c_uint(SECCOMP_SET_MODE_FILTER),
                    ctypes.c_uint(SECCOMP_FILTER_FLAG_NEW_LISTENER), ctypes.byref(fprog))


def _notif_sizes() -> tuple[int, int]:
    sizes = ctypes.create_string_buffer(6)
    _syscall(_arch()[2], ctypes.c_uint(SECCOMP_GET_NOTIF_SIZES), ctypes.c_uint(0), sizes)
    notif, response, _ = struct.unpack("<HHH", sizes.raw)
    return notif, response


# --- connect() broker -------------------------------------------------------------------------


class Broker:
    """Answers the sandbox's connect() calls; denies whatever it cannot read or prove allowed."""

    def __init__(self, listener: int, policy: Policy, root_pid: int) -> None:
        self.listener = listener
        self.policy = policy
        self.root_pid = root_pid
        self.notif_size, self.response_size = _notif_sizes()

    def serve_one(self) -> None:
        buffer = bytearray(self.notif_size)
        try:
            fcntl.ioctl(self.listener, NOTIF_RECV, buffer, True)
        except OSError as exc:
            if exc.errno in (errno.ENOENT, errno.EINTR):
                return
            raise
        notif_id, pid = struct.unpack_from("<QI", buffer, 0)
        args = struct.unpack_from("<6Q", buffer, 32)
        try:
            allowed = self.decide(pid, args[1], args[2], notif_id)
        except OSError:
            allowed = False
        response = bytearray(self.response_size)
        if allowed:
            struct.pack_into("<QqiI", response, 0, notif_id, 0, 0, SECCOMP_USER_NOTIF_FLAG_CONTINUE)
        else:
            struct.pack_into("<QqiI", response, 0, notif_id, 0, -errno.EACCES, 0)
        try:
            fcntl.ioctl(self.listener, NOTIF_SEND, response, True)
        except OSError as exc:
            if exc.errno not in (errno.ENOENT, errno.EINTR):
                raise

    def decide(self, pid: int, address: int, length: int, notif_id: int) -> bool:
        length = min(int(length), 128)
        if length < 2:
            return True
        with open(f"/proc/{pid}/mem", "rb", buffering=0) as memory:
            memory.seek(address)
            raw = memory.read(length)
        cwd = os.readlink(f"/proc/{pid}/cwd")
        # The pid may have been reused between receipt and read; the id check pins it.
        fcntl.ioctl(self.listener, NOTIF_ID_VALID, struct.pack("<Q", notif_id))
        return self.permits(raw, cwd)

    def permits(self, raw: bytes, cwd: str) -> bool:
        family = struct.unpack_from("<H", raw, 0)[0]
        if family == socket.AF_UNIX:
            path = raw[2:].split(b"\0", 1)[0]
            if not path:
                return False  # abstract namespace
            target = Path(os.path.realpath(os.path.join(cwd, os.fsdecode(path))))
            return self.policy.socket_allowed(target)
        if family == socket.AF_INET and len(raw) >= 8:
            port = struct.unpack_from(">H", raw, 2)[0]
            host = ipaddress.IPv4Address(raw[4:8])
            return not (host.is_loopback or host.is_unspecified) or self.loopback_allowed(port)
        if family == socket.AF_INET6 and len(raw) >= 24:
            port = struct.unpack_from(">H", raw, 2)[0]
            host6 = ipaddress.IPv6Address(raw[8:24])
            mapped = host6.ipv4_mapped
            local = host6.is_loopback or host6.is_unspecified or bool(mapped and (mapped.is_loopback or mapped.is_unspecified))
            return not local or self.loopback_allowed(port)
        return True

    def loopback_allowed(self, port: int) -> bool:
        return port in self.policy.loopback_ports or port in sandbox_listening_ports(self.root_pid)


def sandbox_listening_ports(root_pid: int) -> set[int]:
    """TCP ports a process under `root_pid` listens on: a head may reach its own test servers."""
    inodes: dict[str, int] = {}
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(table).read_text(encoding="ascii").splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) > 9 and fields[3] == "0A":
                inodes[fields[9]] = int(fields[1].rsplit(":", 1)[1], 16)
    if not inodes:
        return set()
    ports: set[int] = set()
    for pid in descendants(root_pid):
        try:
            for fd in os.listdir(f"/proc/{pid}/fd"):
                link = os.readlink(f"/proc/{pid}/fd/{fd}")
                if link.startswith("socket:[") and link[8:-1] in inodes:
                    ports.add(inodes[link[8:-1]])
        except OSError:
            continue
    return ports


def descendants(root_pid: int) -> set[int]:
    parents: dict[int, int] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            fields = proc_stat_fields(Path(f"/proc/{entry}/stat").read_text(encoding="ascii"))
        except (OSError, ValueError):
            continue
        parents[int(entry)] = int(fields[1])
    found = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, parent in parents.items():
            if parent in found and pid not in found:
                found.add(pid)
                changed = True
    return found


# --- launch -----------------------------------------------------------------------------------


def child_environment(environ: Mapping[str, str], *, claude_home: Path | None, tmp: Path) -> dict[str, str]:
    env = {key: value for key, value in environ.items() if key not in STRIPPED_ENV}
    env["TMPDIR"] = str(tmp)
    env["XDG_CACHE_HOME"] = str(tmp / "cache")
    # `~/.gitconfig` is out of reach and git refuses an unreadable one; role env names its own.
    env.setdefault("GIT_CONFIG_GLOBAL", os.devnull)
    if claude_home is not None:
        env["CLAUDE_CONFIG_DIR"] = str(claude_home)
    return env


def run(command: Sequence[str], policy: Policy, env: Mapping[str, str]) -> int:
    """Fork the confined child, broker its connects, and return its exit status as a shell would."""
    if landlock_abi() < 1:
        raise SandboxError("the kernel offers no Landlock")
    _notif_sizes()
    parent_end, child_end = socket.socketpair()
    _prctl(PR_SET_CHILD_SUBREAPER, 1)
    pid = os.fork()
    if pid == 0:  # pragma: no cover - replaced by exec in the child
        parent_end.close()
        try:
            # A launcher killed outright takes its head with it instead of leaving it unbrokered.
            _prctl(PR_SET_PDEATHSIG, signal.SIGTERM)
            _prctl(PR_SET_NO_NEW_PRIVS, 1)
            apply_landlock(policy)
            listener = install_connect_filter()
            socket.send_fds(child_end, [b"L"], [listener])
            os.close(listener)
            child_end.close()
            os.execvpe(command[0], list(command), dict(env))
        except BaseException as exc:  # noqa: BLE001 - the child must never fall back to running unconfined
            print(f"head sandbox refused: {exc}", file=sys.stderr)
            os._exit(126)
    child_end.close()
    for name in ("SIGINT", "SIGQUIT", "SIGTSTP", "SIGTTIN", "SIGTTOU"):
        signal.signal(getattr(signal, name), signal.SIG_IGN)
    for name in ("SIGTERM", "SIGHUP"):
        signal.signal(getattr(signal, name), lambda number, _frame: _forward(pid, number))
    _, fds, _, _ = socket.recv_fds(parent_end, 1, 1)
    parent_end.close()
    if not fds:
        return _wait(pid)
    broker = Broker(fds[0], policy, pid)
    poller = select.poll()
    poller.register(broker.listener, select.POLLIN)
    while True:
        status = _reap(pid)
        if status is not None:
            return status
        for _, events in poller.poll(500):
            if events & select.POLLIN:
                broker.serve_one()


def _forward(pid: int, number: int) -> None:
    try:
        os.kill(pid, number)
    except ProcessLookupError:
        pass


def _reap(main: int) -> int | None:
    while True:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return 1
        if pid == 0:
            return None
        if pid == main:
            return os.waitstatus_to_exitcode(status) if os.WIFEXITED(status) else 128 + os.WTERMSIG(status)


def _wait(pid: int) -> int:
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status) if os.WIFEXITED(status) else 128 + os.WTERMSIG(status)


def _prctl(option: int, value: int) -> None:
    if _libc.prctl(option, ctypes.c_ulong(value), ctypes.c_ulong(0), ctypes.c_ulong(0), ctypes.c_ulong(0)) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="ummanu-head-sandbox")
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--product-root", required=True, type=Path)
    parser.add_argument("--state", type=Path, help="the sandbox's own state directory")
    parser.add_argument("--claude", action="store_true", help="give Claude Code its own config dir")
    parser.add_argument("--read", action="append", default=[], type=Path)
    parser.add_argument("--loopback-port", action="append", default=[], type=int)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(list(argv))
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    if not args.command:
        parser.error("a command to run is required after --")
    for path in (args.workspace, args.product_root, *([args.state] if args.state else []), *args.read):
        if not path.is_absolute():
            parser.error(f"{path} must be absolute")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    home = Path(os.environ.get("HOME") or Path.home())
    instance = os.environ.get("UMMANU_INSTANCE", "").strip()
    try:
        workspace = args.workspace.resolve(strict=True)
        product_root = args.product_root.resolve(strict=True)
        state = args.state or home / STATE_DIR
        tmp = private_tmp(workspace)
        private = [tmp]
        claude_home = None
        if args.claude:
            claude_home = state / "claude"
            skills = sorted(
                (home / ".claude" / "skills" / name).resolve()
                for name in role_skills(product_root, instance)
                if (home / ".claude" / "skills" / name).is_dir()
            )
            prepare_claude_home(claude_home, workspace, home=home, skills=skills)
            private.append(claude_home)
            args.read += skills
        data_dir = installation_data_dir(instance)
        if data_dir is not None:
            prepare_data_locks(data_dir)
        policy = build_policy(
            workspace, home=home, environ=os.environ, product_root=product_root, private=private,
            data_dir=data_dir, extra_read=args.read, loopback_ports=args.loopback_port,
        )
        env = child_environment(os.environ, claude_home=claude_home, tmp=tmp)
        return run(args.command, policy, env)
    except Exception as exc:  # noqa: BLE001 - every failure refuses the launch, never runs unconfined
        print(f"head sandbox refused: {exc}", file=sys.stderr)
        return 126


if __name__ == "__main__":  # pragma: no cover - exercised as a separate process
    raise SystemExit(main())
