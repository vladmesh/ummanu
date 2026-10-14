"""Sprint reservation index and admission locks shared by protocol writers."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ummanu._fsutil import file_lock
from ummanu.board.sprint_admission import SprintAdmission, SprintReservationIndex
from ummanu.board.tick_snapshot import select_sprints
from ummanu.kernel.errors import TaskError

_GUARD_INDEX = "sprints/active-repositories.json"
# Version 1 indexes are rebuilt: guards key by project, not repository path.
_GUARD_INDEX_VERSION = 2
_ADMISSION_LOCK = "sprints/admission.lock"


def active_sprint_projects(data_dir: str | Path) -> dict[str, list[str]]:
    """Return the local index of projects reserved by open sprints."""
    index = _read_guard_index(Path(data_dir) / _GUARD_INDEX)
    return index.to_projects_document() if index is not None else {}


def _read_guard_index(path: Path) -> SprintReservationIndex | None:
    """Return the typed index, or None when it is absent, unreadable or of an older version."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return SprintReservationIndex.from_document(raw, version=_GUARD_INDEX_VERSION)


def sprint_guard_index_initialized(data_dir: str | Path) -> bool:
    return _read_guard_index(Path(data_dir) / _GUARD_INDEX) is not None


def require_active_sprint_projects(data_dir: str | Path) -> dict[str, list[str]]:
    """Read the reserved-project index without confusing an unreadable index with an
    empty one. Unlike admission's missing-index view, release-report reads refuse
    unreadable/invalid data and name its file.
    """
    index = _read_guard_index(Path(data_dir) / _GUARD_INDEX)
    if index is None:
        raise TaskError(
            "backend_error",
            f"the reserved-project index {_GUARD_INDEX} is missing, unreadable or of another version",
            1,
        )
    return index.to_projects_document()


def refresh_active_sprint_projects(data_dir: str | Path, reader: Any) -> None:
    """Seed the index from the live board without racing a sprint mutation."""
    with _sprint_guard_index_lock(data_dir):
        _replace_active_sprint_projects(data_dir, select_sprints(reader, statuses={"open"}, create=False))


def _replace_active_sprint_projects(data_dir: str | Path, sprints: list[dict[str, Any]]) -> None:
    admissions = [SprintAdmission.from_document(sprint) for sprint in sprints]
    _write_guard_index(data_dir, SprintReservationIndex.from_sprints(admissions))


def update_active_sprint_projects(data_dir: str | Path, sprint: SprintAdmission | dict[str, Any]) -> None:
    """Update one sprint's entries in the local reserved-project index."""
    with _sprint_guard_index_lock(data_dir):
        path = Path(data_dir) / _GUARD_INDEX
        index = _read_guard_index(path)
        if index is None and path.exists():
            # Rebuild stale index key spaces from the board.
            path.unlink()
            return
        admission = sprint if isinstance(sprint, SprintAdmission) else SprintAdmission.from_document(sprint)
        index = (index or SprintReservationIndex()).without_sprint(admission.ref)
        if admission.is_open:
            index = index.with_sprint(admission)
        _write_guard_index(data_dir, index)


@contextmanager
def _sprint_guard_index_lock(data_dir: str | Path):
    with file_lock((Path(data_dir) / _GUARD_INDEX).with_suffix(".lock")):
        yield


@contextmanager
def sprint_admission_lock(data_dir: str | Path):
    """Serialize every admission of an open sprint on this installation. The rules for
    opening a sprint are reads of live state, so two writers that both check before
    either writes would each see no open sprint and both create one.
    """
    with file_lock(Path(data_dir) / _ADMISSION_LOCK):
        yield


def _write_guard_index(data_dir: str | Path, index: SprintReservationIndex) -> None:
    path = Path(data_dir) / _GUARD_INDEX
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            index.to_document(version=_GUARD_INDEX_VERSION),
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    temporary.replace(path)
