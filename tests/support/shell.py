"""Shell tools that execute real commands without consulting a host's startup files."""

from pathlib import Path


def shell_tools(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    bash = root / "bash"
    bash.write_text('#!/bin/sh\nexec /bin/bash --noprofile --norc "$@"\n', encoding="utf-8")
    bash.chmod(0o755)
    return root
