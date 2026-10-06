"""Resolve the selected installation's data dir for `codex_preflight`, which imports nothing else.

`bound_data_dir` puts it in `UMMANU_DATA_DIR` so a head's launch command and its preflight resolve
one CODEX_HOME (`<data_dir>/codex-home`).
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from ummanu.runtime.codex_preflight import (
    CodexHome,
    CodexHomeLoginMissing,
    data_dir_codex_home,
    resolve_codex_home,
)

DATA_DIR_ENV = "UMMANU_DATA_DIR"
INSTANCE_ENV = "UMMANU_INSTANCE"


def selected_data_dir() -> Path | None:
    """`UMMANU_DATA_DIR`, else the `data_dir` of an explicitly selected `UMMANU_INSTANCE`, else None.

    The default instance path is never read: a checkout must not pick up a host installation's login.
    """
    configured = os.environ.get(DATA_DIR_ENV)
    if configured:
        return Path(configured).expanduser()
    instance = os.environ.get(INSTANCE_ENV)
    if not instance:
        return None
    from ummanu.config import DataDirError, instance_data_dir

    try:
        return instance_data_dir(Path(instance))
    except (DataDirError, OSError):
        return None


def installation_codex_home(profile: Mapping[str, Any] | None = None) -> CodexHome:
    """The CODEX_HOME this process's installation launches Codex heads with.

    Raises `CodexHomeLoginMissing` when there is none (`resolve_codex_home`).
    """
    return resolve_codex_home(profile or {}, data_dir=selected_data_dir())


def installation_codex_dir(data_dir: str | os.PathLike[str] | None = None) -> Path | None:
    """The heads' CODEX_HOME path for `data_dir` (default `selected_data_dir()`), or None without a login.

    Readers of the heads' Codex account use this, not `~/.codex`, whose login no head refreshes.
    """
    target = Path(data_dir).expanduser() if data_dir is not None else selected_data_dir()
    try:
        return Path(resolve_codex_home({}, data_dir=target).path)
    except CodexHomeLoginMissing:
        return None


def managed_codex_homes(data_dir: Path | None) -> tuple[Path, ...]:
    """Every CODEX_HOME an installation manages (exists or not): `<data_dir>/codex-home`, if named.

    Seeding and the Memory-client reconcile both take homes from here.
    """
    # `data_dir_codex_home(None)` would fall back to this process's `UMMANU_DATA_DIR`, which may not
    # be the installation being provisioned.
    data_home = data_dir_codex_home(data_dir) if data_dir is not None else None
    return () if data_home is None else (data_home,)


# Read-only: the legacy Orca home's `sessions/`, kept until the curator
# (`automations.agents.curator.discover.codex_sessions`) has ingested every rollout under it.
_LEGACY_SESSIONS = Path(".config") / "orca" / "codex-runtime-home" / "home" / "sessions"


def session_roots() -> list[Path]:
    """Every `sessions/` a reader of this installation's Codex rollouts has to scan.

    The resolved launch home first, then the data-dir home and the legacy Orca home when present,
    each once (symlinks resolved). The legacy home is only read (`_LEGACY_SESSIONS`).
    """
    candidates: list[Path] = []
    try:
        candidates.append(Path(installation_codex_home().path) / "sessions")
    except CodexHomeLoginMissing:
        # No launchable home: still scan whatever sessions exist.
        pass
    data_home = data_dir_codex_home(selected_data_dir())
    if data_home is not None:
        candidates.append(data_home / "sessions")
    candidates.append(Path.home() / _LEGACY_SESSIONS)
    roots: list[Path] = []
    seen: set[Path] = set()
    for index, root in enumerate(candidates):
        if index and not root.is_dir():
            continue
        key = root.resolve(strict=False)
        if key in seen:
            continue
        seen.add(key)
        roots.append(root)
    return roots


@contextlib.contextmanager
def bound_data_dir(data_dir: str | os.PathLike[str] | None = None) -> Iterator[None]:
    """Set `UMMANU_DATA_DIR` for the block (default `selected_data_dir()`); restored on exit.

    An operator-set value is left untouched.
    """
    if os.environ.get(DATA_DIR_ENV):
        yield
        return
    target = Path(data_dir).expanduser() if data_dir is not None else selected_data_dir()
    if target is None:
        yield
        return
    os.environ[DATA_DIR_ENV] = str(target)
    try:
        yield
    finally:
        os.environ.pop(DATA_DIR_ENV, None)
