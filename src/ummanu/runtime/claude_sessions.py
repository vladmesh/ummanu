"""Where Claude Code keeps one workspace's transcripts, spelled the way Claude Code spells it.

Claude Code writes one directory per project under `~/.claude/projects`, named after the absolute
workspace path with every non-alphanumeric character (separators, underscores, dots) replaced by
`-`; `path.replace('/', '-')` is wrong for paths containing other punctuation.
`tests/test_dispatcher_tui.py` checks the rule against the host's real `~/.claude/projects`.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

__all__ = ["claude_project_dir_name", "claude_session_paths"]

# Every non-alphanumeric character, not just the separator (verified against real project dirs).
_NON_PROJECT_CHAR_RE = re.compile(r"[^A-Za-z0-9]")


def claude_project_dir_name(workspace: str) -> str:
    """The `~/.claude/projects` directory name Claude Code gives this workspace."""
    return _NON_PROJECT_CHAR_RE.sub("-", str(Path(workspace).resolve(strict=False)))


def claude_session_paths(workspace: str, *, root: Path) -> Iterator[Path]:
    """Yield this workspace's Claude session logs without scanning the other projects."""
    try:
        yield from (root / claude_project_dir_name(workspace)).glob("*.jsonl")
    except OSError:
        return
