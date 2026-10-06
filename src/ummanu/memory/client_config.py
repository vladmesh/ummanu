"""Materialize the operator/automation split for Memory MCP clients.

User sessions start the installation-owned stdio PO bridge.  Dispatcher-launched
roles override that entry point at launch and connect to the HTTP daemon with a
runtime-issued bearer.  These writers own only the named Ummanu MCP entries;
all unrelated user configuration is preserved.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ummanu._fsutil import write_text_atomic
from ummanu.runtime.codex_home import managed_codex_homes

MEMORY_URL = "http://127.0.0.1:8077/mcp"
LEGACY_SERVER = "memory"
PO_SERVER = "po_memory"
_SECTION = re.compile(r"^\s*\[([^]]+)]\s*(?:#.*)?$")


class ClientConfigError(RuntimeError):
    """A user client configuration cannot be reconciled safely."""


@dataclass(frozen=True)
class ClientConfigResult:
    codex_user: bool = False
    claude_user: bool = False
    codex_data_dir: bool = False

    @property
    def changed(self) -> int:
        return sum((self.codex_user, self.claude_user, self.codex_data_dir))


def bridge_executable(product_root: Path) -> Path:
    return product_root / ".venv" / "bin" / "ummanu-memory-po-bridge"


def _bridge_env(data_dir: Path) -> dict[str, str]:
    return {
        "MEMORY_ACCESS_BINDINGS": str(data_dir / "memory" / "access-grants"),
        "UMMANU_MEMORY_URL": MEMORY_URL,
    }


def _codex_section(command: Path, data_dir: Path) -> str:
    env = _bridge_env(data_dir)
    return "\n".join(
        (
            f"[mcp_servers.{PO_SERVER}]",
            f"command = {json.dumps(str(command))}",
            "args = []",
            "env = { "
            + ", ".join(f"{key} = {json.dumps(value)}" for key, value in sorted(env.items()))
            + " }",
            "",
        )
    )


def _reject_symlink(path: Path, label: str) -> None:
    if path.is_symlink():
        raise ClientConfigError(f"{label} config is a symlink: {path}")


def _section_ranges(lines: list[str]) -> list[tuple[str, int, int]]:
    starts: list[tuple[str, int]] = []
    for index, line in enumerate(lines):
        match = _SECTION.match(line)
        if match:
            starts.append((match.group(1), index))
    return [
        (name, start, starts[index + 1][1] if index + 1 < len(starts) else len(lines))
        for index, (name, start) in enumerate(starts)
    ]


def _owned_codex_sections(payload: dict[str, Any]) -> set[str]:
    servers = payload.get("mcp_servers")
    if not isinstance(servers, dict):
        return set()
    owned = {PO_SERVER} if PO_SERVER in servers else set()
    legacy = servers.get(LEGACY_SERVER)
    if isinstance(legacy, dict) and legacy.get("url") == MEMORY_URL:
        owned.add(LEGACY_SERVER)
    return owned


def reconcile_codex(path: Path, command: Path, data_dir: Path, *, dry_run: bool = False) -> bool:
    """Replace only Ummanu's legacy/direct entry with the operator bridge."""
    _reject_symlink(path, "Codex")
    try:
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        payload = tomllib.loads(current) if current.strip() else {}
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ClientConfigError(f"cannot read Codex config {path}: {exc}") from None
    if not isinstance(payload, dict):
        raise ClientConfigError(f"Codex config {path} is not a table")
    owned = _owned_codex_sections(payload)
    lines = current.splitlines(keepends=True)
    remove: set[int] = set()
    for name, start, end in _section_ranges(lines):
        if any(
            name == f"mcp_servers.{server}" or name.startswith(f"mcp_servers.{server}.") for server in owned
        ):
            remove.update(range(start, end))
    retained = "".join(line for index, line in enumerate(lines) if index not in remove).rstrip()
    desired = (retained + ("\n\n" if retained else "") + _codex_section(command, data_dir)).rstrip() + "\n"
    if desired == current:
        return False
    if dry_run:
        return True
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_text_atomic(path, desired)
    except (OSError, RuntimeError) as exc:
        raise ClientConfigError(f"cannot update Codex config {path}: {exc}") from None
    return True


def _claude_bridge(command: Path, data_dir: Path) -> dict[str, Any]:
    return {
        "type": "stdio",
        "command": str(command),
        "args": [],
        "env": _bridge_env(data_dir),
    }


def reconcile_claude(path: Path, command: Path, data_dir: Path, *, dry_run: bool = False) -> bool:
    """Publish one user-scoped bridge entry without replacing other Claude state."""
    _reject_symlink(path, "Claude")
    try:
        payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ClientConfigError(f"cannot read Claude config {path}: {exc}") from None
    if not isinstance(payload, dict):
        raise ClientConfigError(f"Claude config {path} is not an object")
    servers = payload.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ClientConfigError(f"Claude config {path} has non-object mcpServers")
    changed = False
    legacy = servers.get(LEGACY_SERVER)
    if isinstance(legacy, dict) and legacy.get("url") == MEMORY_URL:
        del servers[LEGACY_SERVER]
        changed = True
    desired = _claude_bridge(command, data_dir)
    if servers.get(PO_SERVER) == desired and not changed:
        return False
    servers[PO_SERVER] = desired
    if dry_run:
        return True
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_text_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    except (OSError, RuntimeError) as exc:
        raise ClientConfigError(f"cannot update Claude config {path}: {exc}") from None
    return True


# What seeding writes into a managed CODEX_HOME, copy-once. Never `auth.json`: the login is the PO's.
CODEX_HOME_SEEDED_FILES = ("AGENTS.md", "config.toml")


def packaged_codex_home(product_root: Path) -> Path:
    return product_root / "packaging" / "codex-home"


def seed_codex_home(
    home: Path, source: Path, bridge: tuple[Path, Path] | None, *, dry_run: bool = False
) -> tuple[str, ...]:
    """Copy-once the packaged files `home` lacks; a `config.toml` seeded here gets the bridge entry.

    The one way a managed CODEX_HOME gets its files, whichever of install, the upgrade's `codex-home`
    step or `reconcile_clients` reaches it first: the packaged defaults are never skipped, and with
    `bridge` (the PO-bridge executable and data dir) a seeded config is never left without the
    `po_memory` entry the head's `-c mcp_servers.po_memory.enabled=false` needs. An existing file
    is left as it is. Returns the names written, or that would be under `dry_run`.
    """
    seeded: list[str] = []
    for name in CODEX_HOME_SEEDED_FILES:
        destination = home / name
        if destination.exists():
            continue
        seeded.append(name)
        if dry_run:
            continue
        try:
            contents = (source / name).read_text(encoding="utf-8")
            # The home will hold a login once the PO runs `codex login` into it.
            home.mkdir(mode=0o700, parents=True, exist_ok=True)
            write_text_atomic(destination, contents)
        except (OSError, RuntimeError) as exc:
            raise ClientConfigError(f"cannot seed managed CODEX_HOME {home}: {exc}") from None
        if name == "config.toml" and bridge is not None:
            reconcile_codex(destination, *bridge)
    return tuple(seeded)


def reconciled_codex_homes(data_dir: Path) -> tuple[Path, ...]:
    """The managed CODEX_HOMEs `reconcile_clients` writes the bridge entry into: every one the
    resolver (`codex_preflight.resolve_codex_home`) can select.

    `<data_dir>/codex-home` whenever it exists, whatever it holds: a login alone is enough for the
    resolver to pick it. One that does not exist yet can not be selected, and seeding creates it
    with the entry already in place. The legacy Orca home is no longer one (A20 step 7).
    """
    return tuple(home for home in managed_codex_homes(data_dir) if home.exists())


def reconcile_clients(
    product_root: Path, runtime_home: Path, data_dir: Path, *, dry_run: bool = False
) -> ClientConfigResult:
    command = bridge_executable(product_root)
    if not dry_run and not command.is_file():
        raise ClientConfigError(f"PO bridge executable is missing: {command}")
    data_homes = reconciled_codex_homes(data_dir)
    user_codex = runtime_home / ".codex" / "config.toml"
    claude = runtime_home / ".claude.json"
    return ClientConfigResult(
        codex_user=reconcile_codex(user_codex, command, data_dir, dry_run=dry_run),
        claude_user=reconcile_claude(claude, command, data_dir, dry_run=dry_run),
        codex_data_dir=any(
            [
                _reconcile_codex_home(home, product_root, command, data_dir, dry_run=dry_run)
                for home in data_homes
            ]
        ),
    )


def _reconcile_codex_home(
    home: Path, product_root: Path, command: Path, data_dir: Path, *, dry_run: bool
) -> bool:
    """Seed what the home lacks, then bring its `po_memory` entry current."""
    seeded = seed_codex_home(home, packaged_codex_home(product_root), (command, data_dir), dry_run=dry_run)
    reconciled = reconcile_codex(home / "config.toml", command, data_dir, dry_run=dry_run)
    return bool(seeded) or reconciled
