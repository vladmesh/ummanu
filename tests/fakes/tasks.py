"""Starting boards for the card store, and the sprint stand-in the card create path needs.

Cards have one implementation, PostgreSQL, so there is no in-memory card board here. A `CardSeed`
is only seed input: `tests.sql_backend_fixtures.card_store` writes it into a real store through the
product's own client. The rows keep the legacy board shape the importer reads (`column_id`,
`swimlane_id`, `date_creation`), because that is what `seed_client` takes; a card keeps its row id
as its `board_key`, so `task_postgres_12` is the first card of every seed below.
"""

from __future__ import annotations

import contextlib
from typing import Any, ClassVar
from unittest import mock

from tests.observer_identity import as_observer
from ummanu.board import sprint_dod

CARD_STATES = ("issues", "ready", "in_progress", "validate", "assessment", "blocked", "done")


@contextlib.contextmanager
def open_sprint(ref: str = "sprint:test", project: str = "ummanu"):
    """Stand in for the open sprint every Ready card needs.

    These tests are about the create and audit path; the sprint link is a precondition of a
    create on the board, and the guard behind it is covered in tests/test_sprints.py.

    The caller is bound to the same sprint, because the observer creating a card linked to it is
    that sprint's own head; an unbound caller is refused before the create path is reached.
    """
    sprint = {"ref": ref, "status": "open", "repositories": [project], "reservations": [project],
              "dod": sprint_dod.section(None)}
    with mock.patch("ummanu.sprints.SprintReader.show", return_value=sprint), as_observer(ref):
        yield ref


# The sprint the assessment fixture's card belongs to.
SPRINT = "sprint:1031"

#: The seven columns, in the numbering the seed rows below use.
SEED_COLUMNS = [
    {"id": 1, "title": "Issues"},
    {"id": 2, "title": "Ready"},
    {"id": 3, "title": "In progress"},
    {"id": 4, "title": "Validate"},
    {"id": 7, "title": "Assessment"},
    {"id": 5, "title": "Blocked"},
    {"id": 6, "title": "Done"},
]
#: The column id of each state in `SEED_COLUMNS`.
SEED_COLUMN = {
    "issues": 1,
    "ready": 2,
    "in_progress": 3,
    "validate": 4,
    "assessment": 7,
    "blocked": 5,
    "done": 6,
}


class CardSeed:
    """A starting board: card rows, their metadata and their comments, keyed by row id."""

    columns: ClassVar[list[dict[str, Any]]] = SEED_COLUMNS
    lanes: list[dict[str, Any]] = [{"id": 4, "name": "Ummanu"}]  # noqa: RUF012 - copied per seed

    def __init__(
        self,
        tasks: list[dict[str, Any]] | None = None,
        metadata: dict[int, dict[str, Any]] | None = None,
        comments: dict[int, list[dict[str, Any]]] | None = None,
        *,
        next_key: int | None = None,
    ) -> None:
        self.lanes = [dict(lane) for lane in type(self).lanes]
        self.tasks = list(tasks or [])
        self.metadata = dict(metadata or {})
        self.comments = dict(comments or {})
        #: The `board_key` the store hands the next created card, when the test cares.
        self.next_key = next_key


def _two_cards() -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]]]:
    tasks: list[dict[str, Any]] = [
        {
            "id": 12,
            "reference": "ummanu-468",
            "title": "Readonly task protocol",
            "description": "",
            "column_id": 2,
            "position": "3",
            "swimlane_id": 4,
            "date_creation": "1720000000",
            "date_modification": "1720000010",
        },
        {"id": 13, "reference": "old-1", "title": "Old", "column_id": 1, "position": "bad"},
    ]
    metadata: dict[int, dict[str, Any]] = {
        12: {
            "project": "ummanu",
            "task_type": "code",
            "claim": "codex-terra",
            "head": "codex-terra",
            "retry_same": "2",
            "retry_switch": "bad",
            "retry_heads": "codex-terra,claude-opus",
            "steward_report": "1",
            "codex_launch_mode": "tui",
        },
        13: {},
    }
    return tasks, metadata


def reader_seed() -> CardSeed:
    """`secretary-468` in Ready and `old-1` in Issues, each carrying one `report:done` comment."""
    tasks, metadata = _two_cards()
    done = {"date_creation": 1720000020, "comment": "[report:done]\nReady for review"}
    return CardSeed(tasks, metadata, {12: [dict(done)], 13: [dict(done)]})


def writer_seed() -> CardSeed:
    """The same two cards with no comments: the board a writer case starts from."""
    tasks, metadata = _two_cards()
    return CardSeed(tasks, metadata)


def empty_seed() -> CardSeed:
    """No cards and no lanes at all; the first card created on it is `task_postgres_12`."""
    seed = CardSeed(next_key=12)
    seed.lanes = []
    return seed
