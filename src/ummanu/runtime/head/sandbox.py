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
directory, the board's lock files and what its role needs (a worker or reviewer: its own card's task
documents and report/verdict bodies; an observer: the sprint reservation index). The rest of the home (other projects, agent profiles, other
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
import re
import select
import shutil
import signal
import socket
import stat
import struct
import sys
import tempfile
import threading
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
NOTIF_RECV = 0xC0502100
NOTIF_SEND = 0xC0182101
NOTIF_ID_VALID = 0x40082102

MSG_FASTOPEN = 0x20000000


@dataclass(frozen=True)
class _Abi:
    audit: int
    landlock_create_ruleset: int
    seccomp: int
    connect: int
    sendto: int
    sendmsg: int
    sendmmsg: int
    io_uring_setup: int
    pidfd_getfd: int


_ARCHES = {
    "x86_64": _Abi(0xC000003E, 444, 317, 42, 44, 46, 307, 425, 438),
    "aarch64": _Abi(0xC00000B7, 444, 277, 203, 206, 211, 269, 425, 438),
}
X32_SYSCALL_BIT = 0x40000000

SYSTEM_READ = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32", "/etc", "/opt",
               "/run/systemd/resolve", "/sys")
HOME_READ = (".local/bin", ".local/share/claude", ".rustup", ".cargo/bin", ".cargo/env",
             ".cargo/config.toml", ".local/state/ummanu/head-config", ".bashrc", ".profile")
HOME_WRITE = (".cargo/registry", ".cargo/git")
INSTANCE_READ = ("instance.yaml", "board-store.env", "heads", "projects", "skills", "adapters",
                 "CONTEXT.md", "README.md")
# The board CLI's lock files and journals beneath the data directory (made before the ruleset).
DATA_WRITE_FILES = ("board/.create.lock", "board/.audit.lock", "dispatcher/board-bulk.lock",
                    "dispatcher/cleanup.lock")
DATA_WRITE_DIRS = ("board/assessment-decisions", "board/sql-sprint-locks", "board/product-issue-transactions",
                   "dispatcher/effects", "dispatcher/admission", "dispatcher/lifecycle")
# Per role, from the role env the trusted launch command bound (`BOARD_ROLE`, `UMMANU_HEAD_CARD`): a
# worker or reviewer reads its own card's task documents and writes its own card's report/verdict
# bodies (`dispatch.host_runtime.packets`), never another card's. Every sprint role reads the sprint
# reservation index and takes its locks (the sprint guard of `task move`/`comment` and `sprint resume`);
# `sprints/` itself stays read-only, so a head cannot plant a link there that an unconfined writer of
# the index would follow, and its own index refresh is skipped (`board.sprint_protocol.reservations`).
ROLE_ENV = "BOARD_ROLE"
CARD_ENV = "UMMANU_HEAD_CARD"
CARD_ROLES = ("worker", "reviewer")
CARD_READ = "artifacts/prompts"
CARD_WRITE = "artifacts/bodies"
SPRINT_ROLES = ("worker", "reviewer", "observer")
SPRINT_READ = "sprints"
SPRINT_LOCKS = ("sprints/active-repositories.lock", "sprints/admission.lock")
# The dispatcher's overrides of the artifacts roots (`dispatch.host_runtime.packets`).
BODY_DIR_ENV = "UMMANU_DISPATCHER_BODY_DIR"
PROMPT_DIR_ENV = "UMMANU_DISPATCHER_PROMPT_DIR"
# A rule binds the inode, and the dispatcher replaces its state files atomically, so its directory is
# the read root: `ummanu sprint status` and `task show` read the production state from there.
DATA_READ = ("dispatcher", "data-manifest.json")
# Unix sockets outside the write roots a head may still reach: name resolution only.
ALLOWED_SOCKETS = ("/run/systemd/resolve/io.systemd.Resolve",)
# The memory service every head's MCP config names.
MEMORY_PORT = 8077
# A static binary resolves names through the loopback stub resolver rather than the varlink socket.
DNS_PORT = 53
# Addresses that are this host's or its neighbours' (containers, LAN): reachable only on allowed ports.
SHARED_ADDRESS_SPACE = ipaddress.IPv4Network("100.64.0.0/10")
# Variables that would point a head at the owner's account, agent or bus.
STRIPPED_ENV = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "SSH_AUTH_SOCK",
                "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR", "GPG_AGENT_INFO")
CLAUDE_SETTINGS = {"skipDangerousModePermissionPrompt": True, "theme": "dark",
                   "env": {"DISABLE_AUTOUPDATER": "1"}}
# Marks the confined child; must equal `ummanu.runtime.redact.HEAD_SANDBOX_ENV` (this launcher runs
# with `-I` and imports nothing from the product).
HEAD_SANDBOX_ENV = "UMMANU_HEAD_SANDBOX"
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


def trusted_git_store(workspace: Path, *, data_dir: Path | None, instance: str) -> Path | None:
    """The git dir a dispatcher workspace was cut from, named by the installation, never by the workspace.

    `<data>/workspaces/<project>/<card>` belongs to the `repo` of `<instance>/projects/<project>.yaml`
    and `<data>/workspaces/observers/<sprint>` to the dispatcher's observer repository. The head can
    rewrite its own `.git` pointer and worktree admin files, so they only select a worktree of this
    store; any other workspace gets no git roots.
    """
    if data_dir is None or workspace.parent.parent != data_dir / "workspaces":
        return None
    group = workspace.parent.name
    if group == "observers":
        return (data_dir / "dispatcher" / "observer-root" / "observers" / ".git").resolve()
    if not instance:
        return None
    import yaml

    try:
        record = yaml.safe_load((Path(instance) / "projects" / f"{group}.yaml").read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    repo = record.get("repo") if isinstance(record, dict) else None
    return (Path(str(repo)).expanduser() / ".git").resolve() if repo else None


def git_paths(workspace: Path, store: Path | None) -> tuple[list[Path], list[Path]]:
    """Read and write roots for a linked worktree of `store`.

    The store stays read-only apart from objects, refs, logs and this worktree's own admin directory,
    so a head can commit but cannot plant config or hooks the host later runs. The main checkout's
    working tree is not a root: the head has its own. A `.git` pointer that leaves the store's
    `worktrees/` gets nothing.
    """
    marker = workspace / ".git"
    if store is None or marker.is_symlink() or not marker.is_file():
        return [], []
    text = marker.read_text(encoding="utf-8").strip()
    if not text.startswith("gitdir:"):
        return [], []
    gitdir = (workspace / text.removeprefix("gitdir:").strip()).resolve()
    if gitdir.parent != store / "worktrees" or not store.is_dir():
        return [], []
    return [store], [gitdir, *(store / name for name in ("objects", "refs", "logs", "FETCH_HEAD"))]


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
    git_store: Path | None = None,
    extra_read: Sequence[Path] = (),
    loopback_ports: Iterable[int] = (),
) -> Policy:
    """The roots one head may read and write; missing paths are dropped, nothing else is reachable.

    `private` are the sandbox's own writable directories (its Claude config dir and temp dir) and its
    role's (`role_paths`).
    """
    read = [Path(p) for p in SYSTEM_READ]
    read += [home / p for p in HOME_READ]
    read += [product_root, *extra_read]
    write = [workspace, *private, *(home / p for p in HOME_WRITE)]
    write.append(home / ".claude" / "projects" / claude_project_dir_name(workspace))
    write.append(Path("/dev/shm"))
    repo_read, repo_write = git_paths(workspace, git_store)
    read += repo_read
    write += repo_write
    ports = {MEMORY_PORT, DNS_PORT, *loopback_ports}
    instance = environ.get("UMMANU_INSTANCE", "").strip()
    if instance:
        read += [Path(instance) / name for name in INSTANCE_READ]
        port = board_port(Path(instance))
        if port is not None:
            ports.add(port)
    if data_dir is not None:
        read += [data_dir / name for name in DATA_READ]
        write += [data_dir / name for name in (*DATA_WRITE_FILES, *DATA_WRITE_DIRS)]
    # Landlock opens each root through its symlinks; the guard below must judge the same directory.
    policy = Policy(
        read=tuple(dict.fromkeys(p.resolve() for p in read if p.exists())),
        write=tuple(dict.fromkeys(p.resolve() for p in write if p.exists())),
        loopback_ports=frozenset(ports),
    )
    policy.refuse_home_roots(home.resolve())
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


def card_token(reference: str) -> str:
    """A card's directory name under the artifacts roots (`dispatch.state.request_token`)."""
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", reference).strip("-") or "empty"


def role_paths(data_dir: Path, environ: Mapping[str, str]) -> tuple[list[Path], list[Path]]:
    """Extra read and write roots of this head's role, made private (0700) before the ruleset."""
    role = environ.get(ROLE_ENV, "").strip()
    card = environ.get(CARD_ENV, "").strip()
    read: list[Path] = []
    write: list[Path] = []
    if role in CARD_ROLES and card:
        token = card_token(card)
        if token in (".", ".."):
            raise SandboxError(f"card {card!r} names no directory of its own")
        prompt_root = Path(environ.get(PROMPT_DIR_ENV, "").strip() or data_dir / CARD_READ).expanduser()
        body_root = Path(environ.get(BODY_DIR_ENV, "").strip().rstrip("/") or data_dir / CARD_WRITE).expanduser()
        if not (prompt_root.is_absolute() and body_root.is_absolute()):
            raise SandboxError("the dispatcher's prompt and body directories must be absolute")
        prompts, bodies = prompt_root / token, body_root / token
        for path in (prompts.parent, prompts, bodies.parent, bodies):
            _owned_private_dir(path)
        read.append(prompts)
        write.append(bodies)
    if role in SPRINT_ROLES:
        (data_dir / SPRINT_READ).mkdir(parents=True, exist_ok=True)
        read.append(data_dir / SPRINT_READ)
        for name in SPRINT_LOCKS:
            write.append(_owned_lock_file(data_dir / name))
    return read, write


def _owned_lock_file(path: Path) -> Path:
    """`path` as a regular file of this user, created if missing; a link or anything else refuses."""
    with contextlib.suppress(FileExistsError):
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644))
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise SandboxError(f"{path} is not a regular file of this user")
    return path


def _owned_private_dir(path: Path) -> None:
    """`path` as a 0700 directory of this user; anything else there refuses the launch."""
    with contextlib.suppress(FileExistsError):
        path.mkdir(mode=0o700, parents=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise SandboxError(f"{path} is not a directory of this user")
    if info.st_mode & 0o077:
        os.chmod(path, 0o700)


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
    """A Claude config dir holding only this workspace's state: settings, trust, global rules, skills.

    One per workspace, so heads never share history or plant settings for each other. The head may
    write this directory, so every entry the launcher (unconfined) touches is checked or replaced, never
    followed: files are written to a fresh temp name and renamed over, a planted symlink or file
    where a directory belongs is removed, and `skills/` is rebuilt. Its `projects/<workspace>` links to
    the same directory under `~/.claude/projects`, so the product's transcript readers find the
    session where they always look.
    """
    _private_dir(claude_home)
    _write_private(claude_home, "settings.json", _json(CLAUDE_SETTINGS))
    state = _read_json(claude_home / ".claude.json")
    state.update({"hasCompletedOnboarding": True, "theme": state.get("theme") or "dark",
                  "bypassPermissionsModeAccepted": True})
    projects = state.get("projects") if isinstance(state.get("projects"), dict) else {}
    projects.setdefault(str(workspace), {})["hasTrustDialogAccepted"] = True
    state["projects"] = projects
    _write_private(claude_home, ".claude.json", _json(state))
    rules = home / ".claude" / "CLAUDE.md"
    if rules.is_file():
        _write_private(claude_home, "CLAUDE.md", rules.read_text(encoding="utf-8"))
    skills_dir = claude_home / "skills"
    _remove(skills_dir)
    skills_dir.mkdir(mode=0o700)
    for source in skills:
        (skills_dir / source.name).symlink_to(source)
    name = claude_project_dir_name(workspace)
    transcripts = home / ".claude" / "projects" / name
    transcripts.mkdir(mode=0o700, parents=True, exist_ok=True)
    links = claude_home / "projects"
    _private_dir(links)
    link = links / name
    if not (link.is_symlink() and Path(os.readlink(link)) == transcripts):
        _remove(link)
    if not link.is_symlink():
        link.symlink_to(transcripts)


def _json(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def _read_json(path: Path) -> dict:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return {}
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        return {}
    try:
        with os.fdopen(fd, encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _private_dir(path: Path) -> None:
    """`path` as a real directory of this user: anything else there is removed first."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        path.mkdir(mode=0o700, parents=True)
        return
    if stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid():
        return
    _remove(path)
    path.mkdir(mode=0o700)


def _remove(path: Path) -> None:
    """Remove a file, symlink or directory tree without following any link."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISDIR(info.st_mode):
        shutil.rmtree(path)
    else:
        path.unlink()


def _write_private(directory: Path, name: str, text: str) -> None:
    """Replace `directory/name` by a fresh 0600 file: a link planted at either name is never followed."""
    fd, temporary = tempfile.mkstemp(prefix=f".{name}.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary, directory / name)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


# --- kernel interfaces -------------------------------------------------------------------------

_libc = ctypes.CDLL(None, use_errno=True)
_libc.syscall.restype = ctypes.c_long
_libc.connect.restype = ctypes.c_int


def _arch() -> _Abi:
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
        return _syscall(_arch().landlock_create_ruleset, None, ctypes.c_size_t(0), ctypes.c_uint32(CREATE_RULESET_VERSION))
    except OSError:
        return 0


def _handled_fs(abi: int) -> int:
    return _FS_ABI5 if abi >= 5 else _FS_BY_ABI[min(abi, 3)]


def apply_landlock(policy: Policy) -> None:
    """Restrict this process (and its future children) to the policy; irreversible."""
    abi = landlock_abi()
    if abi < 1:
        raise SandboxError("the kernel offers no Landlock")
    create = _arch().landlock_create_ruleset
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
    """cBPF: foreign ABIs and io_uring refused, connect() to the broker, TCP Fast Open sends refused.

    A `sendto`/`sendmsg` with `MSG_FASTOPEN` opens a TCP connection without `connect()`; it gets
    EOPNOTSUPP, the answer of a kernel without Fast Open, so callers fall back to `connect()`.
    """
    abi = _arch()
    program: list[tuple[str, int, str] | tuple[str, int] | str] = [
        ("ld", 4),
        ("jne", abi.audit, "foreign"),
        ("ld", 0),
        ("jge", X32_SYSCALL_BIT, "foreign"),
        ("jeq", abi.connect, "notify"),
        ("jeq", abi.io_uring_setup, "unsupported"),
        ("jeq", abi.sendto, "flags3"),
        ("jeq", abi.sendmmsg, "flags3"),
        ("jeq", abi.sendmsg, "flags2"),
        ("ret", SECCOMP_RET_ALLOW),
        "flags3",
        ("ld", 16 + 8 * 3),
        ("jset", MSG_FASTOPEN, "fastopen"),
        ("ret", SECCOMP_RET_ALLOW),
        "flags2",
        ("ld", 16 + 8 * 2),
        ("jset", MSG_FASTOPEN, "fastopen"),
        ("ret", SECCOMP_RET_ALLOW),
        "notify",
        ("ret", SECCOMP_RET_USER_NOTIF),
        "fastopen",
        ("ret", SECCOMP_RET_ERRNO | errno.EOPNOTSUPP),
        "unsupported",
        ("ret", SECCOMP_RET_ERRNO | errno.ENOSYS),
        "foreign",
        ("ret", SECCOMP_RET_ERRNO | errno.EPERM),
    ]
    return _assemble(program)


def _assemble(program: Sequence[tuple[str, int, str] | tuple[str, int] | str]) -> list[tuple[int, int, int, int]]:
    """Labels to cBPF: `jeq`/`jge`/`jset` jump to the label when true, `jne` when false."""
    labels: dict[str, int] = {}
    for item in program:
        if isinstance(item, str):
            labels[item] = sum(1 for entry in program[: program.index(item)] if not isinstance(entry, str))
    codes = {"ld": 0x20, "ret": 0x06, "jeq": 0x15, "jne": 0x15, "jge": 0x35, "jset": 0x45}
    out: list[tuple[int, int, int, int]] = []
    for item in program:
        if isinstance(item, str):
            continue
        op, value = item[0], item[1]
        if len(item) == 2:
            out.append((codes[op], 0, 0, value))
            continue
        offset = labels[str(item[2])] - len(out) - 1
        jt, jf = (0, offset) if op == "jne" else (offset, 0)
        out.append((codes[op], jt, jf, value))
    return out


def install_connect_filter() -> int:
    """Install the filter on this process and return its notification listener."""
    program = _connect_filter()
    filters = (_Filter * len(program))(*(_Filter(*item) for item in program))
    fprog = _Program(len(program), filters)
    return _syscall(_arch().seccomp, ctypes.c_uint(SECCOMP_SET_MODE_FILTER),
                    ctypes.c_uint(SECCOMP_FILTER_FLAG_NEW_LISTENER), ctypes.byref(fprog))


def _notif_sizes() -> tuple[int, int]:
    sizes = ctypes.create_string_buffer(6)
    _syscall(_arch().seccomp, ctypes.c_uint(SECCOMP_GET_NOTIF_SIZES), ctypes.c_uint(0), sizes)
    notif, response, _ = struct.unpack("<HHH", sizes.raw)
    return notif, response


# --- connect() broker -------------------------------------------------------------------------


class Broker:
    """Makes the sandbox's connect() calls itself, from its own copy of the address it approved.

    The kernel never re-reads the head's memory after the check (no `SECCOMP_USER_NOTIF_FLAG_CONTINUE`):
    the broker takes a duplicate of the head's socket (`pidfd_getfd`, which shares the open file) and
    connects it, reaching a Unix socket through an `O_PATH` handle of the file it checked. Each call
    runs on its own thread, so one slow connect blocks only the head thread that made it. Anything the
    broker cannot read, prove allowed or perform is refused with EACCES.
    """

    def __init__(self, listener: int, policy: Policy, root_pid: int) -> None:
        self.listener = listener
        self.policy = policy
        self.root_pid = root_pid
        self.local = local_addresses()
        self.notif_size, self.response_size = _notif_sizes()

    def serve_one(self) -> None:
        buffer = bytearray(self.notif_size)
        try:
            fcntl.ioctl(self.listener, NOTIF_RECV, buffer, True)
        except OSError:
            return  # the caller left, or a signal interrupted the wait; the head keeps running
        notif_id, pid = struct.unpack_from("<QI", buffer, 0)
        args = struct.unpack_from("<6Q", buffer, 32)
        threading.Thread(target=self._answer, args=(notif_id, pid, args), daemon=True).start()

    def _answer(self, notif_id: int, pid: int, args: Sequence[int]) -> None:
        try:
            error = self.handle(notif_id, pid, args[0], args[1], args[2])
        except Exception:  # noqa: BLE001 - whatever goes wrong is a refusal, never a crash
            error = errno.EACCES
        response = bytearray(self.response_size)
        struct.pack_into("<QqiI", response, 0, notif_id, 0, -error if error else 0, 0)
        with contextlib.suppress(OSError):
            fcntl.ioctl(self.listener, NOTIF_SEND, response, True)

    def handle(self, notif_id: int, pid: int, fd: int, address: int, length: int) -> int:
        """Connect the head's socket `fd` to its address if allowed; the errno to answer with, 0 on success."""
        length = min(int(length), 128)
        with open(f"/proc/{pid}/mem", "rb", buffering=0) as memory:
            memory.seek(address)
            raw = memory.read(length)
        cwd = os.readlink(f"/proc/{pid}/cwd")
        # The pid may have been reused between receipt and these reads; the id check pins it.
        fcntl.ioctl(self.listener, NOTIF_ID_VALID, struct.pack("<Q", notif_id))
        family = struct.unpack_from("<H", raw, 0)[0] if len(raw) >= 2 else socket.AF_UNSPEC
        if family != socket.AF_UNIX:
            if not self.permits(raw, cwd):
                return errno.EACCES
            return self.connect_as(notif_id, pid, int(fd), raw)
        path = raw[2:].split(b"\0", 1)[0]
        if not path:
            return errno.EACCES  # abstract namespace
        target = os.path.realpath(os.path.join(cwd, os.fsdecode(path)))
        if not self.policy.socket_allowed(Path(target)):
            return errno.EACCES
        handle = os.open(target, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            # What was opened, not what the path names now: a swapped directory cannot redirect it.
            opened = Path(os.readlink(f"/proc/self/fd/{handle}"))
            if not stat.S_ISSOCK(os.fstat(handle).st_mode) or not self.policy.socket_allowed(opened):
                return errno.EACCES
            via = os.fsencode(f"/proc/self/fd/{handle}")
            return self.connect_as(notif_id, pid, int(fd), struct.pack("<H", socket.AF_UNIX) + via + b"\0")
        finally:
            os.close(handle)

    def connect_as(self, notif_id: int, pid: int, fd: int, raw: bytes) -> int:
        # The notification names the calling thread; a pidfd names its process, which owns the fds.
        pidfd = os.pidfd_open(thread_group(pid))
        try:
            duplicate = _syscall(_arch().pidfd_getfd, ctypes.c_int(pidfd), ctypes.c_int(fd), ctypes.c_uint(0))
        finally:
            os.close(pidfd)
        try:
            fcntl.ioctl(self.listener, NOTIF_ID_VALID, struct.pack("<Q", notif_id))
            buffer = ctypes.create_string_buffer(raw, len(raw))
            if _libc.connect(ctypes.c_int(duplicate), buffer, ctypes.c_uint(len(raw))) == 0:
                return 0
            return ctypes.get_errno() or errno.EACCES
        finally:
            os.close(duplicate)

    def permits(self, raw: bytes, cwd: str) -> bool:
        """Whether a non-Unix address may be reached: loopback and this host's own addresses only on
        an allowed port, any other network address, any other family (it cannot reach this host)."""
        del cwd
        family = struct.unpack_from("<H", raw, 0)[0] if len(raw) >= 2 else socket.AF_UNSPEC
        if family == socket.AF_INET:
            if len(raw) < 8:
                return False
            port = struct.unpack_from(">H", raw, 2)[0]
            return not self.is_local(ipaddress.IPv4Address(raw[4:8])) or self.loopback_allowed(port)
        if family == socket.AF_INET6:
            if len(raw) < 24:
                return False
            port = struct.unpack_from(">H", raw, 2)[0]
            return not self.is_local(ipaddress.IPv6Address(raw[8:24])) or self.loopback_allowed(port)
        return family != socket.AF_UNIX

    def is_local(self, host: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        """Loopback, this host's addresses and private ranges, where containers and the LAN live."""
        if isinstance(host, ipaddress.IPv6Address) and host.ipv4_mapped is not None:
            host = host.ipv4_mapped
        if isinstance(host, ipaddress.IPv4Address) and host in SHARED_ADDRESS_SPACE:
            return True
        return host.is_loopback or host.is_unspecified or host.is_private or host.is_link_local or (
            host in self.local
        )

    def loopback_allowed(self, port: int) -> bool:
        return port in self.policy.loopback_ports or port in sandbox_listening_ports(self.root_pid)


def thread_group(tid: int) -> int:
    """The process a thread belongs to (`Tgid` in its status)."""
    for line in Path(f"/proc/{tid}/status").read_text(encoding="ascii").splitlines():
        if line.startswith("Tgid:"):
            return int(line.split()[1])
    raise OSError(errno.ESRCH, f"thread {tid} has no thread group")


def local_addresses() -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """This host's own addresses: a service bound to all interfaces answers on them as on loopback."""
    found: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    with contextlib.suppress(OSError):
        lines = Path("/proc/net/fib_trie").read_text(encoding="ascii").splitlines()
        for previous, line in zip(lines, lines[1:], strict=False):
            if "/32 host LOCAL" in line:
                with contextlib.suppress(ValueError):
                    found.add(ipaddress.IPv4Address(previous.split()[-1]))
    with contextlib.suppress(OSError):
        for line in Path("/proc/net/if_inet6").read_text(encoding="ascii").splitlines():
            with contextlib.suppress(ValueError, IndexError):
                found.add(ipaddress.IPv6Address(int(line.split()[0], 16)))
    return found


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
    # The board CLI then scrubs with what the head can read rather than failing on the refused store.
    env[HEAD_SANDBOX_ENV] = "1"
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
            claude_home = state / "claude" / hashlib.sha256(str(workspace).encode()).hexdigest()[:16]
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
            data_dir = data_dir.resolve()
            prepare_data_locks(data_dir)
            role_read, role_write = role_paths(data_dir, os.environ)
            args.read += role_read
            private += role_write
        policy = build_policy(
            workspace, home=home, environ=os.environ, product_root=product_root, private=private,
            data_dir=data_dir, git_store=trusted_git_store(workspace, data_dir=data_dir, instance=instance),
            extra_read=args.read, loopback_ports=args.loopback_port,
        )
        env = child_environment(os.environ, claude_home=claude_home, tmp=tmp)
        return run(args.command, policy, env)
    except Exception as exc:  # noqa: BLE001 - every failure refuses the launch, never runs unconfined
        print(f"head sandbox refused: {exc}", file=sys.stderr)
        return 126


if __name__ == "__main__":  # pragma: no cover - exercised as a separate process
    raise SystemExit(main())
