"""Shared state locations that must be stable across checkouts."""

from __future__ import annotations

import os
from pathlib import Path

from ummanu.runtime.paths import env_value

WORKSPACES_ROOT = Path(env_value("UMMANU_WORKSPACES_ROOT", nonempty=True) or Path.home() / "orca" / "workspaces").resolve()
AGENTS_PROJECT = os.environ.get("UMMANU_ORCA_PROJECT", "ummanu")


def resolve_pipeline_state_dir(workspaces_root: Path | None = None) -> Path:
    """State dir owned by the live pipeline dispatcher worktree.

    `UMMANU_PIPELINE_STATE_DIR` is a test and host-layout override. Without it, the dispatcher state
    lives in the reserved ummanu/pipeline worktree under the shared Orca workspaces root.
    """
    override = env_value("UMMANU_PIPELINE_STATE_DIR", nonempty=True)
    if override:
        return Path(override)
    root = WORKSPACES_ROOT if workspaces_root is None else Path(workspaces_root)
    return root / AGENTS_PROJECT / "pipeline" / "state" / "pipeline"
