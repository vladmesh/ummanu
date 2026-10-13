"""Frozen cwd mapping consumed by curator rebind after the released sprint:1475 move.

Persisted pending identities, watermark paths and Claude transcript keys may still name these
cwds. Keep pair order and key encoding: rebind uses the first whole-component match. This data
is historical recovery policy, not an installation mover or a general rename table.
"""

from __future__ import annotations

import re
from pathlib import Path

Prefixes = list[tuple[str, str]]

# Released source names, scoped to the curator's persisted cwd identities.
_OLD_PRODUCT = "secretary"
_OLD_DATA = "secretary-data"
_ROLES = ("curator", "pipeline", "retro", "steward")


def claude_key(path: Path) -> str:
    """The `~/.claude/projects` directory name Claude Code gives a cwd (every non-alphanumeric -> '-')."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def swap_prefix(value: str, prefixes: Prefixes) -> str:
    """`value` with the first matching path prefix replaced; a prefix matches whole components only,
    so a product cwd never matches an instance-repository cwd."""
    for old, new in prefixes:
        if value == old or value.startswith(old + "/"):
            return new + value[len(old) :]
    return value


def claude_move_paths(home: Path, sprint: str) -> list[tuple[Path, Path]]:
    """The released cwd pairs, including the caller-selected observer sprint when nonempty."""
    data_old, data_new = home / _OLD_DATA, home / "ummanu-data"
    pairs = [
        (data_old / "po", data_new / "po"),
        (data_old / "po" / "handoffs", data_new / "po" / "handoffs"),
        (
            data_old / "dispatcher" / "observer-root" / "observers",
            data_new / "dispatcher" / "observer-root" / "observers",
        ),
        (home / _OLD_PRODUCT, home / "ummanu"),
    ]
    if sprint:
        slug = sprint.replace(":", "-")
        pairs.append(
            (data_old / "workspaces" / "observers" / slug, data_new / "workspaces" / "observers" / slug)
        )
    workspaces = home / "orca" / "workspaces"
    pairs += [(workspaces / _OLD_PRODUCT / role, workspaces / "ummanu" / role) for role in _ROLES]
    return pairs
