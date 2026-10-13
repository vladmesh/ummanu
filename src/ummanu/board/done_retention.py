"""Canonical, retry-safe cleanup of old active Done execution cards."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from ummanu.task_errors import TaskError
from ummanu.tasks import TaskReader, TaskWriter

DONE_RETENTION_DAYS = 5


def close_old_done(
    reader: TaskReader,
    writer: TaskWriter,
    *,
    now: float | None = None,
    retention_days: int = DONE_RETENTION_DAYS,
) -> dict[str, Any]:
    """Return the legacy-shaped Done cleanup result through canonical ports.

    The comparison intentionally remains strict: a card exactly at the retention
    threshold stays open until a later run.
    """
    now_value = float(time.time() if now is None else now)
    cutoff = now_value - retention_days * 86400
    closed: list[str] = []
    for candidate in sorted(reader.done_retention_candidates(), key=lambda item: str(item["reference"])):
        reference = str(candidate["reference"])
        moved_at = candidate.get("date_moved")
        if not isinstance(moved_at, int) or moved_at <= 0 or moved_at >= cutoff:
            continue
        result = writer.retire_done(
            reference=reference,
            expected_date_moved=moved_at,
            cutoff=cutoff,
            retention_days=retention_days,
        )
        if result.get("retired"):
            closed.append(reference)
    return {
        "action": "closed-old-done",
        "closed": closed,
        "closed_count": len(closed),
        "retention_days": retention_days,
    }


class DoneRetentionBoard:
    """Lazy canonical implementation of retro's narrow Done-retention port."""

    def __init__(
        self,
        *,
        board_factory: Callable[[], tuple[TaskReader, TaskWriter]],
        error_mapper: Callable[[TaskError], None] | None = None,
    ) -> None:
        self._board_factory = board_factory
        self._error_mapper = error_mapper
        self._reader: TaskReader | None = None
        self._writer: TaskWriter | None = None

    def _ensure_board(self) -> tuple[TaskReader, TaskWriter]:
        if self._reader is None or self._writer is None:
            self._reader, self._writer = self._board_factory()
        return self._reader, self._writer

    def close_old_done(self) -> dict[str, Any]:
        try:
            reader, writer = self._ensure_board()
            return close_old_done(reader, writer)
        except TaskError as exc:
            if self._error_mapper is not None:
                self._error_mapper(exc)
            raise
