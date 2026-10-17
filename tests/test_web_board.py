"""The board page through its route: columns, tiles and a board that cannot be read.

Hermetic. The real `ReadLayer` reads a fake board client that answers `board_cards` with the rows a
board store statement returns; which rows the store selects is `tests/test_web_board_store.py`.
"""

from __future__ import annotations

import re
import unittest
from datetime import UTC, datetime
from decimal import Decimal
from html import unescape
from typing import Any

from tests.web_fakes import Recording
from ummanu.board.owner_handover import WAITING_OWNER, WAITING_OWNER_BY, WAITING_OWNER_REASON
from ummanu.web.app import WebApp
from ummanu.webproto.reads import ReadLayer

NOW = 1_800_000_000.0
HOUR = 3600.0


def row(ref: str, state: str, **values: Any) -> dict[str, Any]:
    """One `board_cards` row: a well-formed code card unless `values` says otherwise."""
    return {
        "ref": ref,
        "title": f"title of {ref}",
        "state": state,
        "type": "code",
        "project": "ummanu",
        "sprint": None,
        "moved": Decimal(str(NOW - HOUR)),
        "head_override": None,
        "worker_head": None,
        "review_head_override": None,
        "review_head": None,
        "extensions": {},
        "blocking_comment": None,
        **values,
    }


class BoardClient:
    """The board store's `board_cards`, answering the rows it was given, or failing as told."""

    def __init__(self, rows: list[dict[str, Any]] | None = None, *, failure: Exception | None = None) -> None:
        self.rows = rows or []
        self.failure = failure
        self.asked: list[dict[str, Any]] = []

    def board_cards(self, **arguments: Any) -> list[dict[str, Any]]:
        self.asked.append(arguments)
        if self.failure is not None:
            raise self.failure
        return self.rows


def page(client: Any) -> tuple[int, str]:
    reads = ReadLayer("/nonexistent/instance", board_client=client, clock=lambda: NOW)
    app = WebApp(reads, *(Recording() for _ in range(7)))
    response = app.handle("GET", "/board")
    return response.status, response.body.decode("utf-8")


def columns(html: str) -> list[tuple[str, str, list[str]]]:
    """Each column in page order: its title, its count, and its tiles' markup."""
    found = []
    for section in html.split('<section class="board-column"')[1:]:
        title = re.search(r"<h2>(.*?)</h2>", section).group(1)
        count = re.search(r'<span class="count">(.*?)</span>', section).group(1)
        found.append((title, count, re.findall(r'<li class="tile.*?</li>', section, re.DOTALL)))
    return found


def tile_of(html: str, ref: str) -> str:
    return next(tile for _t, _c, tiles in columns(html) for tile in tiles if f'href="/tasks/{ref}"' in tile)


def text(markup: str) -> str:
    return unescape(re.sub(r"<[^>]+>", " ", markup))


class ColumnTests(unittest.TestCase):
    def test_seven_columns_in_board_order_with_counts_and_the_oldest_move_first(self) -> None:
        client = BoardClient(
            [
                row("u-1", "ready", moved=Decimal(str(NOW - HOUR))),
                row("u-2", "ready", moved=Decimal(str(NOW - 5 * HOUR))),
                row("u-3", "ready", moved=None),
                row("u-4", "done"),
                row("u-5", "issues"),
                row("u-6", "assessment"),
            ]
        )
        status, html = page(client)
        self.assertEqual(status, 200)
        shown = columns(html)
        self.assertEqual(
            [(title, count) for title, count, _tiles in shown],
            [
                ("Issues", "1"),
                ("Ready", "3"),
                ("In progress", "0"),
                ("Validate", "0"),
                ("Assessment", "1"),
                ("Blocked", "0"),
                ("Done", "1"),
            ],
        )
        ready = [re.search(r'href="/tasks/([^"]+)"', tile).group(1) for tile in shown[1][2]]
        self.assertEqual(ready, ["u-3", "u-2", "u-1"])

    def test_the_read_asks_for_done_since_the_window_and_the_header_links_the_board(self) -> None:
        client = BoardClient()
        _status, html = page(client)
        self.assertEqual(client.asked[0]["done_since"], datetime.fromtimestamp(NOW - 48 * HOUR, UTC))
        self.assertIn('<a href="/board" aria-current="page">Board</a>', html)


class TileTests(unittest.TestCase):
    def test_a_tile_names_its_card_title_type_project_sprint_and_age_in_the_column(self) -> None:
        client = BoardClient(
            [
                row("u-1", "ready", sprint="sprint:7", moved=Decimal(str(NOW - 3 * HOUR))),
                row("u-2", "ready", sprint=None, type="research", project="codegen"),
            ]
        )
        _status, html = page(client)
        linked = tile_of(html, "u-1")
        self.assertIn('<a class="ref" href="/tasks/u-1">u-1</a>', linked)
        self.assertIn('<p class="tile-title" title="title of u-1">title of u-1</p>', linked)
        self.assertIn('<a href="/sprints/sprint%3A7">sprint:7</a>', linked)
        self.assertIn(">3h</span>", linked)
        outside = text(tile_of(html, "u-2"))
        self.assertIn("research · codegen · вне спринта", outside)

    def test_the_head_is_the_worker_in_progress_the_reviewer_in_validate_and_none_elsewhere(self) -> None:
        heads = {
            "head_override": "claude-opus-high",
            "worker_head": "codex-terra-high",
            "review_head_override": "codex-review",
            "review_head": "claude-review",
        }
        client = BoardClient(
            [
                row("u-1", "in_progress", **heads),
                row("u-2", "in_progress", worker_head="codex-terra-high"),
                row("u-3", "validate", **heads),
                row("u-4", "validate", review_head="claude-review"),
                row("u-5", "ready", **heads),
                row("u-6", "assessment", **heads),
            ]
        )
        _status, html = page(client)
        shown = {
            ref: re.search(r'<span title="current head">(.*?)</span>', tile_of(html, ref)).group(1)
            for ref in ("u-1", "u-2", "u-3", "u-4", "u-5", "u-6")
        }
        self.assertEqual(
            shown,
            {
                "u-1": "claude-opus-high",
                "u-2": "codex-terra-high",
                "u-3": "codex-review",
                "u-4": "claude-review",
                "u-5": "—",
                "u-6": "—",
            },
        )

    def test_a_blocked_tile_shows_the_first_content_line_of_its_blocking_comment_or_says_none_was_recorded(
        self,
    ) -> None:
        client = BoardClient(
            [
                row("u-1", "blocked", blocking_comment="[dispatcher]\n\n  red gate on   the same SHA \nlog tail"),
                row("u-2", "blocked", blocking_comment=None),
                row("u-3", "blocked", blocking_comment="[observer]\n   \n"),
                row("u-4", "ready", blocking_comment="[dispatcher]\nnot a Blocked card"),
            ]
        )
        _status, html = page(client)
        reason = re.compile(r'<p class="tile-reason">(.*?)</p>')
        self.assertEqual(reason.findall(tile_of(html, "u-1")), ["red gate on the same SHA"])
        self.assertEqual(reason.findall(tile_of(html, "u-2")), ["reason not recorded"])
        self.assertEqual(reason.findall(tile_of(html, "u-3")), ["reason not recorded"])
        self.assertEqual(reason.findall(tile_of(html, "u-4")), [])

    def test_a_card_handed_to_the_owner_has_a_marked_tile_with_a_text_label(self) -> None:
        mark = {
            WAITING_OWNER: "2026-10-16T10:00:00Z",
            WAITING_OWNER_REASON: "choose the deploy window",
            WAITING_OWNER_BY: "po",
        }
        client = BoardClient(
            [row("u-1", "in_progress", extensions={"extra": mark}), row("u-2", "in_progress")]
        )
        _status, html = page(client)
        waiting = tile_of(html, "u-1")
        self.assertTrue(waiting.startswith('<li class="tile tile-owner">'))
        self.assertIn('title="choose the deploy window">waits for the owner</p>', waiting)
        self.assertTrue(tile_of(html, "u-2").startswith('<li class="tile">'))
        self.assertNotIn("waits for the owner", tile_of(html, "u-2"))


class TotalityTests(unittest.TestCase):
    def test_a_failing_board_read_is_a_notice_with_its_reason_not_an_error_status(self) -> None:
        for name, client, said in (
            (
                "the store refuses",
                BoardClient(failure=RuntimeError("the board store did not answer")),
                "the board store did not answer",
            ),
            ("a row in no column", BoardClient([row("u-1", "archived")]), "which no board column shows"),
            ("no store can be built", None, ""),
        ):
            with self.subTest(name):
                status, html = page(client)
                self.assertEqual(status, 200)
                notice = re.search(r'<p class="unavailable"><b>board not readable:</b> (.*?)</p>', html)
                self.assertTrue(notice.group(1).startswith("the board could not be read: "), html)
                self.assertIn(said, unescape(notice.group(1)))
                self.assertNotIn('<section class="board-column"', html)

    def test_hostile_rows_render_a_tile_with_honest_placeholders(self) -> None:
        cases: list[tuple[str, dict[str, Any], list[str]]] = [
            ("missing title", {"title": None}, ["no readable title"]),
            ("non-string title", {"title": 42}, ["no readable title"]),
            ("blank title", {"title": " \n "}, ["no readable title"]),
            (
                "title with HTML and newlines",
                {"title": "<script>alert(1)</script>\nsecond line"},
                ["&lt;script&gt;alert(1)&lt;/script&gt; second line"],
            ),
            ("missing project", {"project": None}, ["project unknown"]),
            ("non-string project", {"project": ["ummanu"]}, ["project unknown"]),
            ("missing type", {"type": None}, ["type unknown"]),
            ("non-string type", {"type": {"code": 1}}, ["type unknown"]),
            ("missing sprint", {"sprint": None}, ["вне спринта"]),
            ("non-string sprint", {"sprint": 1494}, ["sprint unknown"]),
            ("missing date_moved", {"moved": None}, ['<span class="age">unknown</span>']),
            ("date_moved in the future", {"moved": Decimal(str(NOW + HOUR))}, ['<span class="age">unknown</span>']),
            ("date_moved infinite", {"moved": Decimal("Infinity")}, ['<span class="age">unknown</span>']),
            ("date_moved before any datetime", {"moved": Decimal("-1e20")}, ['<span class="age">unknown</span>']),
            ("date_moved not a number", {"moved": "yesterday"}, ['<span class="age">unknown</span>']),
            ("extensions not an object", {"extensions": "not json"}, ['<li class="tile">']),
        ]
        for name, values, expected in cases:
            with self.subTest(name):
                status, html = page(BoardClient([row("u-1", "ready", **values)]))
                self.assertEqual(status, 200)
                tile = tile_of(html, "u-1")
                for fragment in expected:
                    self.assertIn(fragment, tile)
                self.assertNotIn("<script>", html.split("<main>")[1].split("</main>")[0])


if __name__ == "__main__":
    unittest.main()
