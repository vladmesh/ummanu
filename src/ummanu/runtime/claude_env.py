"""Pre-answer Claude Code's first-run prompts before a headless driver spawns a head.

Folder trust and the onboarding theme picker both block a fresh `claude` on stdin, so the head
never starts its work and is never detected. Both are flags in `~/.claude.json`.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


class ClaudeConfigError(RuntimeError):
    """~/.claude.json (or its override) is unreadable or unwritable."""


def _load(config: Path) -> dict:
    try:
        return json.loads(config.read_text(encoding="utf-8")) if config.is_file() else {}
    except (OSError, json.JSONDecodeError) as e:
        raise ClaudeConfigError(f"cannot read {config}: {e}") from e


def _save(config: Path, data: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(config.parent), prefix=f".{config.name}.")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, config)


def ensure_trust(config: Path, workspace: str) -> None:
    """Mark `workspace` trusted so Claude Code skips the folder-trust dialog for it."""
    d = _load(config)
    entry = d.setdefault("projects", {}).setdefault(str(workspace), {})
    if entry.get("hasTrustDialogAccepted") is True:
        return
    entry["hasTrustDialogAccepted"] = True
    _save(config, d)


def ensure_theme(config: Path, default: str = "dark") -> None:
    """Pre-set the global theme so a fresh `claude` process skips the onboarding picker.

    Global, not per-workspace: the picker is gated on the top-level `theme` key, unrelated to
    `~/.claude/settings.json`'s own theme setting. Leaves an existing value untouched.
    """
    d = _load(config)
    if d.get("theme"):
        return
    d["theme"] = default
    _save(config, d)
