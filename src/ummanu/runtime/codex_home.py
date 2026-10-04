"""The installation's data dir, for the CODEX_HOME resolver that cannot read it itself.

`codex_preflight.resolve_codex_home` takes `<data_dir>/codex-home` when it holds a login, but that
module imports nothing else of `ummanu`, so it knows the data dir only when it is named or in
`UMMANU_DATA_DIR`. This module is the other half: it reads the data dir of the selected
installation, and binds it into `UMMANU_DATA_DIR` for the processes that launch Codex heads, so
the command a head is launched with and the preflight that writes its trust resolve one home.
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
    """`UMMANU_DATA_DIR`, else the `data_dir` of an explicitly selected `UMMANU_INSTANCE`.

    None with neither, or with an instance whose data dir cannot be resolved. The default instance
    path is never read: a checkout on a host with an installation must not pick up its login.
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
    """The CODEX_HOME this installation's heads run with as a path, or None with no login there.

    `data_dir` is the one the caller serves; unnamed, it is `selected_data_dir()`. A reader of the
    heads' Codex account (the dashboard's usage bar) takes its login and rollouts from here, not
    from `~/.codex`, whose login no head refreshes.
    """
    target = Path(data_dir).expanduser() if data_dir is not None else selected_data_dir()
    try:
        return Path(resolve_codex_home({}, data_dir=target).path)
    except CodexHomeLoginMissing:
        return None


def managed_codex_homes(data_dir: Path | None) -> tuple[Path, ...]:
    """Every CODEX_HOME an installation manages, whether or not it exists yet or holds a login.

    `<data_dir>/codex-home` when a data dir is named, and nothing otherwise. Seeding and the
    Memory-client reconcile take their homes from here, so neither can leave one out. The legacy
    Orca home is not managed since A20 step 7 (secretary-1723).
    """
    # Only a named data dir: `data_dir_codex_home(None)` would fall back to this process's own
    # `UMMANU_DATA_DIR`, which is not necessarily the installation being provisioned.
    data_home = data_dir_codex_home(data_dir) if data_dir is not None else None
    return () if data_home is None else (data_home,)


# Read-only, and only here: the `sessions/` of the legacy Orca home. The curator
# (`automations.agents.curator.discover.codex_sessions`) still has sessions there it has not
# ingested -- on 2026-09-24 its watermark named 2749 of the 3217 rollouts and not the other 468,
# written up to 08:38Z that day. Remove this once the curator watermark names every file under it.
_LEGACY_SESSIONS = Path(".config") / "orca" / "codex-runtime-home" / "home" / "sessions"


def session_roots() -> list[Path]:
    """Every `sessions/` a reader of this installation's Codex rollouts has to scan.

    The home a launch would resolve to now comes first (when there is one), then the data-dir home
    and the legacy Orca home whenever their `sessions/` exists, each directory once (symlinks
    resolved). No head runs on the legacy home any more; it is read, never written or resolved to,
    for the sessions the curator has not ingested yet (`_LEGACY_SESSIONS`).
    """
    candidates: list[Path] = []
    try:
        candidates.append(Path(installation_codex_home().path) / "sessions")
    except CodexHomeLoginMissing:
        # No home a head could run with: a reader still scans whatever sessions exist.
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
    """Name the installation's data dir in `UMMANU_DATA_DIR` for the duration of the block.

    `data_dir` is the one the caller already serves; unnamed, it is `selected_data_dir()`. A value
    already in the environment is the operator's and is left as it is. The previous environment is
    restored on exit, so an in-process caller (a test, a CLI invoked twice) keeps no trace.
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
