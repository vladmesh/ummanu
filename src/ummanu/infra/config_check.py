"""`ummanu config check`: the live root's own check, with no Git.

It replaces the instance repository's test suite (docs/OPERATIONS.md, "Changing installation
config"). Two checks run over the live root:

- schema validation, `config.validate_instance`, the same read the dispatcher makes every tick,
  and the cross-family fallback rule (`config.fallback_errors`, ummanu-108): every head profile
  and PO session can fall over to the other subscription family;
- the old-name guard (`infra.old_name_guard`, docs/RENAME.md §T5) over the files the next exporter
  cut copies. A file outside the export allowlist (`runtime.env`, `board-store.env`,
  `secrets/installation.key`, generated or local state) is never opened.

Each finding is one line; any finding fails the check.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from ummanu.checkpoint import CheckpointBlocked, exported_files
from ummanu.config import fallback_errors, validate_instance
from ummanu.infra.old_name_guard import live_root_violations, text_of


@dataclass
class ConfigCheck:
    live_root: Path
    findings: list[str] = field(default_factory=list)
    checked_files: int = 0

    @property
    def ok(self) -> bool:
        return not self.findings


def check_live_root(instance: Path) -> ConfigCheck:
    """Both checks over the live root `instance` (a directory or its `instance.yaml`)."""
    report = validate_instance(instance)
    result = ConfigCheck(report.instance_path.parent)
    result.findings += [_one_line(f"schema: {error}") for error in report.errors]
    result.findings += [
        _one_line(f"fallback: {error}") for error in fallback_errors(result.live_root, report.instance)
    ]
    try:
        names = exported_files(result.live_root)
    except CheckpointBlocked as exc:
        result.findings.append(_one_line(f"export: {exc}"))
        return result
    for name in names:
        try:
            data = _read_regular(result.live_root, name)
        except OSError as exc:
            result.findings.append(_one_line(f"{name}: unreadable: {exc.strerror or exc}"))
            continue
        result.checked_files += 1
        result.findings += [_one_line(line) for line in live_root_violations(name, text_of(data))]
    return result


def _read_regular(root: Path, name: str) -> bytes:
    """The bytes of the regular file `name`, never through a symlink swapped in at the leaf."""
    descriptor = os.open(root / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(0, "not a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            return handle.read()
    finally:
        os.close(descriptor)


def _one_line(text: str) -> str:
    return " ".join(text.split())
