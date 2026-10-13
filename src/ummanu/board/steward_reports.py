"""Canonical Ummanu adapter for the steward's report-card lifecycle."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from ummanu.kernel.errors import TaskError
from ummanu.tasks import TaskReader, TaskWriter


class StewardReportBoard:
    """A small structural adapter for a dispatch-owned report-board port.

    The port itself belongs to the triggered-agent runtime, which keeps that
    scheduler independent of Ummanu.  This implementation intentionally
    imports no runtime protocol: matching its three methods is enough and keeps
    the dependency directed from the product domain to the adapter.
    """

    def __init__(
        self,
        reader: TaskReader | None = None,
        writer: TaskWriter | None = None,
        *,
        actor: str = "triggered-agent-dispatch",
        board_factory: Callable[[], tuple[TaskReader, TaskWriter]] | None = None,
    ) -> None:
        if (reader is None) != (writer is None):
            raise ValueError("StewardReportBoard needs both reader and writer")
        if reader is None and board_factory is None:
            raise ValueError("StewardReportBoard needs a board or board_factory")
        if reader is not None and board_factory is not None:
            raise ValueError("StewardReportBoard cannot combine a board and board_factory")
        self._reader = reader
        self._writer = writer
        self._board_factory = board_factory
        self.actor = actor

    def _ensure_board(self) -> tuple[TaskReader, TaskWriter]:
        if self._reader is None or self._writer is None:
            assert self._board_factory is not None
            self._reader, self._writer = self._board_factory()
        return self._reader, self._writer

    @property
    def reader(self) -> TaskReader:
        return self._ensure_board()[0]

    @property
    def writer(self) -> TaskWriter:
        return self._ensure_board()[1]

    def create_report(self, *, project: str, title: str, slug: str) -> str:
        result = self.writer.create_steward_report(
            actor=self.actor,
            project=project,
            title=title,
            slug=slug,
        )
        return str(result["task"]["ref"])

    def in_progress_reports(self, *, project: str) -> list[dict[str, Any]]:
        return self.reader.steward_reports_in_progress(project)

    def move_report(self, *, reference: str, target: Literal["done", "blocked"], reason: str) -> None:
        self.writer.move(
            role="steward",
            actor=self.actor,
            reference=reference,
            target=target,
            reason=reason,
        )


class StewardSignalBoard:
    """Ummanu-owned implementation of the steward's structural read port."""

    def __init__(
        self,
        reader: TaskReader | None = None,
        *,
        reader_factory: Callable[[], TaskReader] | None = None,
        error_mapper: Callable[[TaskError], None] | None = None,
    ) -> None:
        if reader is None and reader_factory is None:
            raise ValueError("StewardSignalBoard needs a reader or reader_factory")
        if reader is not None and reader_factory is not None:
            raise ValueError("StewardSignalBoard cannot combine a reader and reader_factory")
        self._reader = reader
        self._reader_factory = reader_factory
        self._error_mapper = error_mapper

    @property
    def reader(self) -> TaskReader:
        if self._reader is None:
            assert self._reader_factory is not None
            self._reader = self._reader_factory()
        return self._reader

    def active_cards(
        self, *, states: set[str] | None = None, project: str | None = None
    ) -> list[dict[str, Any]]:
        try:
            return self.reader.steward_signal_cards(states=states, project=project)
        except TaskError as exc:
            if self._error_mapper is not None:
                self._error_mapper(exc)
            raise
