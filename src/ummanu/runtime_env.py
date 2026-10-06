"""The one validated reader for host ``runtime.env`` configuration."""

from __future__ import annotations

import re
import stat
from pathlib import Path

from ummanu.infra.export_allowlist import is_exported


class RuntimeEnvError(RuntimeError):
    """The host runtime file is unsafe or does not use the supported syntax."""


class RuntimeEnvMissing(RuntimeEnvError):
    """The optional host runtime file is absent."""


_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def parse_env_value(raw: str) -> str:
    """Decode the supported single-line systemd EnvironmentFile value syntax.

    Unquoted interior spaces and # are literal; backslash quotes the next character.
    A fully quoted value follows systemd's single/double-quote rules. No expansion,
    multiline values or quote concatenation is supported. Stored env-file values
    remain serialized text; only runtime consumers decode them.
    """
    if any(
        char in "\x00\n\r"
        or ord(char) == 0xFEFF
        or 0xFDD0 <= ord(char) <= 0xFDEF
        or ord(char) & 0xFFFF in (0xFFFE, 0xFFFF)
        for char in raw
    ):
        raise RuntimeEnvError("value contains unsupported control characters")
    value = raw.lstrip(" \t")
    if not value:
        return ""
    quote = value[0] if value[0] in "'\"" else None
    result: list[str] = []
    significant = 0
    index = 1 if quote else 0
    while index < len(value):
        char = value[index]
        if quote and char == quote:
            if value[index + 1 :].strip(" \t"):
                raise RuntimeEnvError("quoted value must end on the same line")
            return "".join(result)
        if char == "\\" and quote != "'":
            index += 1
            if index == len(value):
                raise RuntimeEnvError("line continuations are unsupported")
            escaped = value[index]
            if quote == '"' and escaped not in '"\\\x60$':
                result.append("\\")
            result.append(escaped)
            significant = len(result)
        else:
            result.append(char)
            if char not in " \t":
                significant = len(result)
        index += 1
    if quote:
        raise RuntimeEnvError("quoted value is not closed")
    return "".join(result[:significant])


def parse_runtime_env(text: str) -> dict[str, str]:
    """Read one assignment per line with systemd's value interpretation.

    Operator files may have comments, blank lines and repeated names (last wins).
    The secret importer separately refuses inputs it cannot reproduce byte for byte.
    """
    values: dict[str, str] = {}
    for number, raw in enumerate(text.split("\n"), 1):
        line = raw.removesuffix("\r").lstrip(" \t")
        if not line or line.startswith(("#", ";")):
            continue
        if "=" not in line or line.startswith("export "):
            raise RuntimeEnvError(f"runtime.env line {number} must use KEY=VALUE syntax")
        key, value = line.split("=", 1)
        key = key.rstrip(" \t")
        if not _ENV_NAME.fullmatch(key):
            raise RuntimeEnvError(f"runtime.env line {number} has an invalid variable name")
        try:
            values[key] = parse_env_value(value)
        except RuntimeEnvError as exc:
            raise RuntimeEnvError(f"runtime.env line {number}: {exc}") from None
    return values


def instance_runtime_env_path(instance_dir: Path, override: str | None = None) -> Path:
    return Path(override).expanduser() if override else instance_dir / "runtime.env"


def read_runtime_env(
    instance_dir: Path,
    override: str | None = None,
    *,
    require_ignored: bool = True,
) -> dict[str, str]:
    """Read the supported ``KEY=VALUE`` dialect, after private-file checks.

    `require_ignored` refuses a file inside the live root at a path the snapshot export allowlist
    matches (`infra.export_allowlist.is_exported`); a file outside the live root is never exported.
    """
    path = instance_runtime_env_path(instance_dir, override)
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        raise RuntimeEnvMissing(
            f"runtime credentials are required: create {path}, chmod 0600, then rerun with --recover"
        ) from None
    except OSError as exc:
        raise RuntimeEnvError(f"runtime.env metadata is unreadable: {path}: {exc}") from None
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise RuntimeEnvError("runtime.env must be a regular file, not a symlink")
    if mode & 0o077:
        raise RuntimeEnvError("runtime.env permissions are too broad; run chmod 0600")
    if require_ignored:
        # Excluded means "not exported": the snapshot export allowlist is the one boundary between
        # the live root and what leaves the host, whether or not the live root is a Git work tree.
        try:
            relative = path.resolve().relative_to(instance_dir.resolve())
        except ValueError:
            relative = None
        if relative is not None and is_exported(relative.as_posix()):
            raise RuntimeEnvError(
                f"runtime.env is at {relative.as_posix()}, a live-root path the snapshot export copies; "
                "move it out of the export allowlist"
            )
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeError):
        raise RuntimeEnvError("runtime.env is unreadable") from None
    return parse_runtime_env(text)
