"""The interactive head's permanent workspace (`<data_dir>/interactive`), the cwd of `ummanu shell`.

Upgrade and recover materialize it like the PO workspace (`ummanu.po.workspace`): an `AGENTS.md`
composed of the shipped shared part plus the live root's personal part, and a one-line `CLAUDE.md`
pointing at it. The persona is written only here, never to other workspaces or the owner's home.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ummanu._fsutil import write_bytes_atomic

# The one name of the workspace under the data directory.
WORKSPACE_NAME = "interactive"
# The shared part, relative to the product checkout being installed.
SHARED_SOURCE_RELATIVE = Path("packaging") / "interactive-workspace" / "AGENTS.md"
# The personal part, relative to the live root. `persona/**` is in the snapshot allowlist.
PERSONAL_SOURCE_RELATIVE = Path("persona") / "AGENTS.md"
AGENTS_FILE = "AGENTS.md"
CLAUDE_FILE = "CLAUDE.md"
# Which sources the workspace was composed from, for doctor and status.
SOURCES_FILE = ".sources.json"

CLAUDE_POINTER = b"@AGENTS.md\n"
# Between the two parts. The personal part follows it byte for byte.
SEPARATOR = (
    b"\n---\n\n"
    b"<!-- ummanu: the personal part follows, byte for byte from persona/AGENTS.md in the live root."
    b" Edit it there; upgrade rewrites this file. -->\n\n"
)
DIGEST_LENGTH = 12


class WorkspaceError(RuntimeError):
    """The interactive workspace cannot be materialized."""


@dataclass(frozen=True)
class WorkspaceResult:
    path: Path
    # Workspace-relative names that were (or, on a dry run, would be) written.
    changed: tuple[str, ...]
    shared: str
    # None when the live root has no personal part.
    personal: str | None


def workspace_dir(data_dir: Path | str) -> Path:
    return Path(data_dir) / WORKSPACE_NAME


def shared_source(product_root: Path) -> Path:
    return product_root / SHARED_SOURCE_RELATIVE


def personal_source(live_root: Path) -> Path:
    return live_root / PERSONAL_SOURCE_RELATIVE


def digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()[:DIGEST_LENGTH]


def compose_agents(shared: bytes, personal: bytes | None) -> bytes:
    """`AGENTS.md`: the shared part, then the separator and the personal part when there is one."""
    if personal is None:
        return shared
    if not shared.endswith(b"\n"):
        shared += b"\n"
    return shared + SEPARATOR + personal


def _read_personal(path: Path) -> bytes | None:
    if not path.exists() and not path.is_symlink():
        return None
    try:
        return path.read_bytes()
    except OSError as exc:
        raise WorkspaceError(f"cannot read the personal part {path}: {exc}") from None


def _replace_owned(path: Path, payload: bytes, *, dry_run: bool) -> bool:
    """Write a product-owned file unless it already holds exactly these bytes."""
    try:
        if path.is_file() and not path.is_symlink() and path.read_bytes() == payload:
            return False
        if not dry_run:
            write_bytes_atomic(path, payload)
    except (OSError, RuntimeError) as exc:
        raise WorkspaceError(f"cannot write {path}: {exc}") from None
    return True


def materialize(
    product_root: Path, live_root: Path, data_dir: Path, *, dry_run: bool = False
) -> WorkspaceResult:
    """Bring the workspace to the composition of the two current sources.

    Upgrade and recover both call this; recover's live root is the one extracted from the snapshot.
    """
    source = shared_source(product_root)
    try:
        shared = source.read_bytes()
    except OSError as exc:
        raise WorkspaceError(f"cannot read the packaged shared part {source}: {exc}") from None
    personal = _read_personal(personal_source(live_root))
    agents = compose_agents(shared, personal)
    sources = {
        "agents": digest(agents),
        "personal": digest(personal) if personal is not None else None,
        "shared": digest(shared),
    }
    stamp = (json.dumps(sources, indent=2, sort_keys=True) + "\n").encode("utf-8")

    workspace = workspace_dir(data_dir)
    changed: list[str] = []
    for name, payload in ((AGENTS_FILE, agents), (CLAUDE_FILE, CLAUDE_POINTER), (SOURCES_FILE, stamp)):
        if _replace_owned(workspace / name, payload, dry_run=dry_run):
            changed.append(name)
    return WorkspaceResult(workspace, tuple(changed), sources["shared"], sources["personal"])


def describe(data_dir: Path | str) -> dict[str, Any]:
    """What doctor and status report about the workspace; reads only."""
    workspace = workspace_dir(data_dir)
    state: dict[str, Any] = {
        "path": str(workspace),
        "present": False,
        "shared": None,
        "personal": None,
        "drifted": False,
        "error": None,
    }
    try:
        agents = (workspace / AGENTS_FILE).read_bytes()
    except FileNotFoundError:
        return state
    except OSError as exc:
        state["error"] = f"cannot read {AGENTS_FILE}: {exc}"
        return state
    state["present"] = True
    try:
        sources = json.loads((workspace / SOURCES_FILE).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        state["error"] = f"no readable {SOURCES_FILE}: {exc}"
        return state
    if not isinstance(sources, dict):
        state["error"] = f"{SOURCES_FILE} is not an object"
        return state
    shared, personal = sources.get("shared"), sources.get("personal")
    state["shared"] = shared if isinstance(shared, str) else None
    state["personal"] = personal if isinstance(personal, str) else None
    state["drifted"] = sources.get("agents") != digest(agents)
    return state


def status_line(state: dict[str, Any]) -> str:
    """The one doctor/status line: `interactive workspace: <path> (shared <sha>, personal <sha|absent>)`."""
    prefix = f"interactive workspace: {state['path']}"
    if not state["present"]:
        detail = f"error: {state['error']}" if state["error"] else "absent; run `ummanu upgrade`"
        return f"{prefix} ({detail})"
    if state["error"]:
        return f"{prefix} ({state['error']}; run `ummanu upgrade`)"
    line = f"{prefix} (shared {state['shared'] or 'unknown'}, personal {state['personal'] or 'absent'})"
    if state["drifted"]:
        line += f"; {AGENTS_FILE} differs from its sources, run `ummanu upgrade`"
    return line
