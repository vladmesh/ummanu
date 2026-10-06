"""Carry the prepared environment on sealed stdin, never privileged argv or disk.

The gated launcher is the sole producer. sudo and a synchronous system scope preserve
stdin; the trusted bootstrap leaves it unopened until setpriv has dropped privileges.
The runtime reader consumes it once and replaces stdin with /dev/null. The kernel
owns cleanup on exec refusal, cancellation and crashes, including before admission.
See docs/HEAD_SCOPES.md "Prepared environment across privilege acquisition".
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

MAX_ENVIRONMENT_BYTES = 1024 * 1024
BOOTSTRAP = Path(__file__).resolve().with_name("scope_bootstrap.py")
BOOTSTRAP_ENVIRONMENT = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}
_SEALS = fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE


class EnvironmentTransferError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("scoped environment transfer refused")


def _valid(environment: object) -> bool:
    return isinstance(environment, dict) and all(
        isinstance(key, str) and key and "=" not in key and "\0" not in key
        and isinstance(value, str) and "\0" not in value
        for key, value in environment.items()
    )


def environment_descriptor(environment: Mapping[str, str]) -> int:
    """Return an immutable anonymous descriptor; no recoverable secret artifact."""
    snapshot = dict(environment)
    if not _valid(snapshot):
        raise EnvironmentTransferError()
    payload = json.dumps(snapshot, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    if len(payload) > MAX_ENVIRONMENT_BYTES:
        raise EnvironmentTransferError()
    fd = os.memfd_create("ummanu-scope-environment", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(os.dup(fd), "wb") as stream:
            stream.write(payload)
        os.lseek(fd, 0, os.SEEK_SET)
        fcntl.fcntl(fd, fcntl.F_ADD_SEALS, _SEALS)
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_environment(fd: int = 0) -> dict[str, str]:
    """Bounded reader, only after the native runtime uid/gid/groups drop."""
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or not 0 < info.st_size <= MAX_ENVIRONMENT_BYTES
            or fcntl.fcntl(fd, fcntl.F_GET_SEALS) != _SEALS
            or os.lseek(fd, 0, os.SEEK_CUR) != 0
        ):
            raise EnvironmentTransferError()
        payload = bytearray()
        while len(payload) < info.st_size:
            chunk = os.read(fd, info.st_size - len(payload))
            if not chunk:
                raise EnvironmentTransferError()
            payload.extend(chunk)
        environment = json.loads(payload)
        if not _valid(environment):
            raise EnvironmentTransferError()
        return environment
    except (OSError, ValueError, TypeError):
        raise EnvironmentTransferError() from None


def privileged_argv(arguments: list[str]) -> list[str]:
    """Rebuild the privileged argv from the shared argv contract.

    Also accepts the older producer form (sudo -E and an `env PYTHONPATH=` prefix); neither
    controls privileged execution. Interpreter, bootstrap and identity come from this launcher;
    scope properties and head argv keep the caller's contract. No environment value grants admission.
    """
    args = list(arguments)
    if args[:4] == ["sudo", "-n", "-E", "systemd-run"]:
        args.pop(2)
    if args[:3] != ["sudo", "-n", "systemd-run"]:
        raise EnvironmentTransferError()
    try:
        divider = args.index("--")
        payload = args[divider + 1:]
        if payload[:1] == ["env"]:
            if len(payload) < 2 or not payload[1].startswith("PYTHONPATH="):
                raise EnvironmentTransferError()
            payload = payload[2:]
        if payload[1:4] == ["-P", "-m", "ummanu.runtime.head.local_pty.scope_bootstrap"]:
            payload = payload[4:]
        elif payload[1:3] == ["-I", str(BOOTSTRAP)]:
            payload = payload[3:]
        else:
            raise EnvironmentTransferError()
        groups = os.getgroups()
        identity = [f"--reuid={os.getuid()}", f"--regid={os.getgid()}",
                    f"--groups={','.join(map(str, groups))}" if groups else "--clear-groups"]
        if payload[:4] != [*identity, "--"] or not payload[4:]:
            raise EnvironmentTransferError()
        return ["/usr/bin/sudo", "-n", "/usr/bin/systemd-run", *args[3:divider], "--",
                sys.executable, "-I", str(BOOTSTRAP), *payload]
    except (ValueError, IndexError):
        raise EnvironmentTransferError() from None


def exec_scope(arguments: list[str], *, binding: dict[str, Any] | None = None) -> None:
    from .scoped_lifecycle import LAUNCH_BINDING_ENV, binding_description

    argv = privileged_argv(arguments)
    environment = dict(os.environ)
    environment.pop(LAUNCH_BINDING_ENV, None)
    if binding is not None:
        divider = argv.index("--")
        if any("Description=" in arg or arg.startswith("--description") for arg in argv[:divider]):
            raise EnvironmentTransferError()
        argv.insert(divider, "--description=" + binding_description(binding))
        environment[LAUNCH_BINDING_ENV] = json.dumps(binding)
    fd = environment_descriptor(environment)
    try:
        os.dup2(fd, 0, inheritable=True)
    finally:
        os.close(fd)
    # No caller Python/loader/home/PATH setting is active in privileged bootstrap.
    os.execve(argv[0], argv, BOOTSTRAP_ENVIRONMENT)
