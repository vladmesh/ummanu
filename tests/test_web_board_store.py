"""Which cards the board read selects from a real card store, and in how many statements.

The store is a throwaway PostgreSQL (`tests/sql_backend_fixtures.py`); the read is
`ReadLayer.board_view` over the product's own card client.
"""

from __future__ import annotations

import tempfile
import unittest
from typing import Any

from tests.sql_backend_fixtures import CardStoreClient, card_store
from ummanu.webproto.reads import ReadLayer

NOW = 1_800_000_000
HOUR = 3600
DAY = 24 * HOUR


class StatementCountingStore(CardStoreClient):
    """The card store client, counting the statements its reads issue."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.statements = 0

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        self.statements += 1
        return super()._query(sql, params)


class BoardStoreReadTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.board = card_store(self, instance_dir=scratch.name, client_class=StatementCountingStore)
        self.reads = ReadLayer(scratch.name, board_client=self.board, clock=lambda: float(NOW))
        self.keys = iter(range(100, 1000))

    def card(self, reference: str, state: str, *, moved: int | None, sprint: str | None = None,
             archived: bool = False, project: str = "ummanu", task_type: str | None = "code") -> int:
        key = next(self.keys)
        metadata = {**({"task_type": task_type} if task_type else {}), **({"sprint_ref": sprint} if sprint else {})}
        self.board.add_card(
            key, reference, project=project, state=state, moved=moved, closed=archived, metadata=metadata
        )
        return key

    def columns(self, **filters: str) -> list[dict[str, Any]]:
        document = self.reads.board_view(**filters)
        self.assertEqual(document["source"]["state"], "available", document["source"])
        return document["columns"]

    def refs(self, **filters: str) -> set[str]:
        return {tile["ref"] for column in self.columns(**filters) for tile in column["cards"]}

    def shown(self) -> dict[str, list[str]]:
        return {column["state"]: [tile["ref"] for tile in column["cards"]] for column in self.columns()}

    def test_backlog_issue_and_product_rows_are_never_on_the_board(self) -> None:
        self.board.add_record(
            "product:ummanu",
            "Ummanu",
            {"record_type": "product", "product_id": "ummanu", "product_projects": '["ummanu"]'},
        )
        self.board.add_record(
            "issue:abc",
            "An issue",
            {"record_type": "issue", "issue_product": "ummanu", "issue_kind": "bug", "issue_priority": "P2"},
        )
        self.card("ummanu-1", "issues", moved=NOW - HOUR)
        self.assertEqual(self.shown()["issues"], ["ummanu-1"])

    def test_done_holds_the_last_two_days_and_every_done_card_of_an_open_sprint(self) -> None:
        self.board.ensure_sprint("sprint:1", status="open")
        self.board.ensure_sprint("sprint:2", status="closed")
        self.card("ummanu-1", "done", moved=NOW - HOUR)
        self.card("ummanu-2", "done", moved=NOW - 3 * DAY)
        self.card("ummanu-3", "done", moved=NOW - 10 * DAY, sprint="sprint:1", archived=True)
        self.card("ummanu-4", "done", moved=NOW - 10 * DAY, sprint="sprint:2", archived=True)
        self.card("ummanu-5", "done", moved=None, sprint="sprint:1")
        self.card("ummanu-6", "ready", moved=NOW - HOUR, archived=True)
        self.card("ummanu-7", "ready", moved=NOW - 30 * DAY)
        shown = self.shown()
        self.assertEqual(shown["done"], ["ummanu-5", "ummanu-3", "ummanu-1"])
        self.assertEqual(shown["ready"], ["ummanu-7"])

    def test_an_untyped_card_is_never_on_the_board(self) -> None:
        self.card("ummanu-1", "ready", moved=NOW - HOUR)
        self.card("ummanu-2", "ready", moved=NOW - HOUR, task_type=None)
        self.assertEqual(self.refs(), {"ummanu-1"})

    def test_project_and_sprint_narrow_the_window_and_together_intersect(self) -> None:
        self.board.ensure_sprint("sprint:1", status="open")
        self.board.ensure_sprint("sprint:2", status="closed")
        self.card("ummanu-1", "ready", moved=NOW - HOUR, sprint="sprint:1")
        self.card("ummanu-2", "in_progress", moved=NOW - HOUR)
        self.card("codegen-1", "done", moved=NOW - 10 * DAY, sprint="sprint:1", project="codegen")
        self.card("codegen-2", "done", moved=NOW - 3 * DAY, project="codegen")
        self.card("codegen-3", "done", moved=NOW - 10 * DAY, sprint="sprint:2", project="codegen", archived=True)
        self.card("codegen-4", "blocked", moved=NOW - HOUR, project="codegen")
        cases = [
            ({}, {"ummanu-1", "ummanu-2", "codegen-1", "codegen-4"}),
            ({"project": "codegen"}, {"codegen-1", "codegen-4"}),
            ({"sprint": "sprint:1"}, {"ummanu-1", "codegen-1"}),
            ({"sprint": "none"}, {"ummanu-2", "codegen-4"}),
            ({"sprint": "sprint:2"}, set()),
            ({"project": "codegen", "sprint": "sprint:1"}, {"codegen-1"}),
            ({"project": "codegen", "sprint": "none"}, {"codegen-4"}),
            ({"project": "ummanu", "sprint": "sprint:404"}, set()),
            ({"project": "no-such-project"}, set()),
        ]
        for filters, expected in cases:
            with self.subTest(**filters):
                self.board.statements = 0
                self.assertEqual(self.refs(**filters), expected)
                self.assertEqual(self.board.statements, 1)

    def test_the_filters_offer_the_open_sprints_newest_first(self) -> None:
        self.board.ensure_sprint("sprint:9", status="open")
        self.board.ensure_sprint("sprint:10", status="open")
        self.board.ensure_sprint("sprint:11", status="closed")
        sprints = self.reads.board_filters()["sprints"]
        self.assertEqual(sprints["source"]["state"], "available", sprints["source"])
        self.assertEqual(sprints["items"], ["sprint:10", "sprint:9"])

    def test_a_blocked_card_carries_the_latest_comment_a_move_into_blocked_wrote_since_it_entered(self) -> None:
        moved = NOW - HOUR
        blocked = self.card("ummanu-1", "blocked", moved=moved)
        self.board.add_comment(blocked, "[dispatcher]\nVitality (review): healthy", created=moved - 60)
        self.board.add_comment(blocked, "[report:blocked]\nclassification: external_fact\n\nthe kit", created=moved - 30)
        self.board.add_comment(blocked, "[dispatcher]\nwait watchdog: the review head stalled", created=moved)
        self.board.add_comment(blocked, "[worker]\na worker note after the move", created=moved + 60)
        silent = self.card("ummanu-2", "blocked", moved=moved)
        self.board.add_comment(silent, "[observer]\nan older move's reason", created=moved - 600)
        tiles = {
            tile["ref"]: tile["blocked_reason"]
            for column in self.columns()
            for tile in column["cards"]
        }
        self.assertEqual(
            tiles, {"ummanu-1": "wait watchdog: the review head stalled", "ummanu-2": None}
        )

    def test_the_read_is_one_statement_whatever_the_card_count(self) -> None:
        self.board.ensure_sprint("sprint:1", status="open")
        added = 0
        for count in (3, 30):
            while added < count:
                added += 1
                key = self.card(f"ummanu-{added}", "blocked" if added % 2 else "done", moved=NOW - added,
                                sprint="sprint:1")
                self.board.add_comment(key, "[observer]\nheld", created=NOW)
            self.board.statements = 0
            columns = self.columns()
            self.assertEqual(self.board.statements, 1, count)
            self.assertEqual(sum(column["count"] for column in columns), count)


if __name__ == "__main__":
    unittest.main()
