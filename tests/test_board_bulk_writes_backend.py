"""The set-based board writes of ummanu-237 against a real PostgreSQL store.

`tests/test_board_rows.py` proves, without a server, what these writes send; this suite proves the
statements are valid and leave the rows the row-by-row writes left, and that a list three times
longer costs the same statements, counted at `psycopg.Cursor.execute`:

* a card's `retry_heads`, `blocked_by` and `issues` links, and the bag keys a save drops;
* a Sprint's repositories, issues and project reservations, and a restored budget history;
* a Product's project set;
* a card comment whose time is unreadable, refused by name before the NOT NULL column sees it.

Like the other `*_backend` suites this needs Docker and never skips.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest import mock

from tests.sql_backend_fixtures import PostgresBoard
from ummanu.board.backend import record_key
from ummanu.board.sql_cards import SqlCardClient, SqlCardError
from ummanu.board.sql_sprints import sprint_key

_AT = datetime(2026, 10, 1, tzinfo=UTC)


class _Case(unittest.TestCase):
    board: PostgresBoard

    @classmethod
    def setUpClass(cls) -> None:
        cls.board = PostgresBoard.shared()

    def client(self) -> SqlCardClient:
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        client = SqlCardClient(self.board.fresh_database().for_role("owner"), Path(root.name))
        self.addCleanup(client.close)
        q = client._execute
        with client.transaction():
            q("INSERT INTO projects (project_id) VALUES ('ummanu')")
            q(
                "INSERT INTO products (product_id, board_key, title, description, state, extensions, "
                "created_at, updated_at) VALUES ('ummanu', %s, 'Ummanu', '', 'active', '{}'::jsonb, %s, %s)",
                (record_key("product", "ummanu"), _AT, _AT),
            )
            for issue in ("i1", "i2", "i3"):
                q(
                    "INSERT INTO issues (issue_id, board_key, product_id, title, description, issue_kind, "
                    "priority, state, extensions, created_at, updated_at) "
                    "VALUES (%s, %s, 'ummanu', %s, '', 'bug', 'P1', 'open', '{}'::jsonb, %s, %s)",
                    (issue, record_key("issue", issue), issue, _AT, _AT),
                )
            q(
                "INSERT INTO sprints (ref, board_key, sprint_number, goal, definition_of_done, status, "
                "created_at, updated_at) VALUES ('sprint:1', %s, 1, 'Goal', 'DoD', 'open', %s, %s)",
                (sprint_key("sprint:1"), _AT, _AT),
            )
            for number in range(1, 10):
                q(
                    "INSERT INTO tasks (task_ref, task_number, project_id, title, description, state, "
                    "archived, position, extensions, created_at, updated_at) "
                    "VALUES (%s, %s, 'ummanu', %s, '', 'ready', false, %s, '{}'::jsonb, %s, %s)",
                    (f"ummanu-{number}", number, f"Card {number}", number, _AT, _AT),
                )
        return client

    @staticmethod
    def card_key(client: SqlCardClient, ref: str) -> int:
        return int(client._query("SELECT board_key FROM tasks WHERE task_ref = %s", (ref,))[0][0])

    @staticmethod
    def count(action: Callable[[], Any]) -> int:
        import psycopg

        executed: list[str] = []
        original = psycopg.Cursor.execute

        def counting(cursor: Any, query: Any, *args: Any, **kwargs: Any) -> Any:
            executed.append(str(query))
            return original(cursor, query, *args, **kwargs)

        with mock.patch.object(psycopg.Cursor, "execute", counting):
            action()
        return len(executed)

    def assert_constant(self, write: Callable[[SqlCardClient, int], Any]) -> None:
        counts = []
        for n in (2, 6):
            client = self.client()
            counts.append(self.count(lambda client=client, n=n: write(client, n)))
        self.assertEqual(counts[0], counts[1], f"statements grew with the list: {counts}")


class CardLinkWriteTests(_Case):
    def test_links_and_bag_removals_leave_the_rows_the_row_by_row_writes_left(self) -> None:
        client = self.client()
        key = self.card_key(client, "ummanu-1")
        client.call("saveTaskMetadata", task_id=key, values={"note": "kept", "gone": "x", "also": "y"})
        client.call("saveTaskMetadata", task_id=key, values={
            "retry_heads": " codex-high, ,claude-opus ",
            "blocked_by": "ummanu-3,other-9,ummanu-1,ummanu-2",
            "issues": "issue:i2,issue:missing,i1",
            "gone": "",
            "also": "",
        })
        q = client._query
        self.assertEqual(
            q("SELECT ordinal, head FROM task_retry_heads WHERE task_ref = 'ummanu-1' ORDER BY ordinal"),
            [(0, "codex-high"), (1, "claude-opus")],
        )
        self.assertEqual(
            sorted(q("SELECT depends_on, depends_on_task FROM task_dependencies WHERE task_ref = 'ummanu-1'")),
            [("other-9", None), ("ummanu-2", "ummanu-2"), ("ummanu-3", "ummanu-3")],
        )
        self.assertEqual(
            sorted(q("SELECT issue_id FROM task_issues WHERE task_ref = 'ummanu-1'")), [("i1",), ("i2",)]
        )
        [(bag,)] = q("SELECT extensions->'extra' FROM tasks WHERE task_ref = 'ummanu-1'")
        self.assertEqual(bag, {"note": "kept"})
        meta = client.call("getTaskMetadata", task_id=key)
        self.assertEqual(meta["retry_heads"], "codex-high,claude-opus")

    def test_the_statement_count_does_not_grow_with_the_lists(self) -> None:
        def write(client: SqlCardClient, n: int) -> None:
            client.call("saveTaskMetadata", task_id=self.card_key(client, "ummanu-1"), values={
                "retry_heads": ",".join(f"head-{i}" for i in range(n)),
                "blocked_by": ",".join(f"ummanu-{i + 2}" for i in range(n)),
                # An issue the store lacks is skipped by the same INSERT, so the list can grow freely.
                "issues": ",".join(f"issue:x{i}" for i in range(n)),
                **{f"note-{i}": "" for i in range(n)},
            })

        self.assert_constant(write)

    def test_a_comment_with_an_unreadable_time_is_refused_by_name(self) -> None:
        client = self.client()
        key = self.card_key(client, "ummanu-1")
        with self.assertRaisesRegex(SqlCardError, "created_at 'soon'"):
            client.call("createComment", task_id=key, content="hello", created_at="soon")
        self.assertEqual(client._query("SELECT count(*) FROM task_comments"), [(0,)])
        client.call("createComment", task_id=key, content="old", created_at="2026-10-01T00:00:00.5+00:00")
        client.call("createComment", task_id=key, content="new", created_at="2026-10-01T00:00:01Z")
        self.assertEqual(
            client._query("SELECT body, created_at FROM task_comments ORDER BY created_at"),
            [("old", _AT.replace(microsecond=500000)), ("new", _AT.replace(second=1))],
        )


class SprintRelationWriteTests(_Case):
    def test_relations_keep_their_order_and_a_known_repository(self) -> None:
        client = self.client()
        known = client._query("INSERT INTO repositories (path) VALUES ('/r/a') RETURNING repository_id")[0][0]
        client.call("saveTaskMetadata", task_id=sprint_key("sprint:1"), values={
            "sprint_repositories": json.dumps(["/r/b", "/r/a", "/r/c"]),
            "sprint_issues": json.dumps(["issue:i2", "i1"]),
            "sprint_reservations": json.dumps(["ummanu", "fresh", "ummanu"]),
        })
        q = client._query
        self.assertEqual(
            q("SELECT r.path, sr.ordinal FROM sprint_repositories sr JOIN repositories r USING (repository_id) "
              "WHERE sr.sprint_ref = 'sprint:1' ORDER BY sr.ordinal"),
            [("/r/b", 0), ("/r/a", 1), ("/r/c", 2)],
        )
        self.assertEqual(q("SELECT repository_id FROM repositories WHERE path = '/r/a'"), [(known,)])
        self.assertEqual(
            q("SELECT issue_id, ordinal FROM sprint_issues WHERE sprint_ref = 'sprint:1' ORDER BY ordinal"),
            [("i2", 0), ("i1", 1)],
        )
        self.assertEqual(
            q("SELECT project_id, ordinal, reserved, released_at IS NULL FROM sprint_projects "
              "WHERE sprint_ref = 'sprint:1' ORDER BY ordinal"),
            [("fresh", 1, True, True), ("ummanu", 2, True, True)],
        )
        self.assertEqual(q("SELECT count(*) FROM projects WHERE project_id = 'fresh'"), [(1,)])

    def test_a_restored_budget_history_keeps_its_listed_order(self) -> None:
        client = self.client()
        client._execute(
            "INSERT INTO requests (request_id, operation, intent, status, entity_kind, ref, created_at) "
            "VALUES ('restore-1', 'restored', '{}'::jsonb, 'staged', 'sprint', 'sprint:1', %s)",
            (_AT,),
        )
        client.call("saveTaskMetadata", task_id=sprint_key("sprint:1"), values={
            "sprint_budget": json.dumps({"by_type": {"red_review": 2, "blocked": 1}}),
            "sprint_budget_uncharged": json.dumps({"infrastructure_blocked": 1}),
        })
        self.assertEqual(
            client._query(
                "SELECT event_type, charged, reason, request_id FROM sprint_budget_events "
                "WHERE sprint_ref = 'sprint:1' ORDER BY budget_event_id"
            ),
            [
                ("red_review", True, "restored occurrence 1", "restore-1"),
                ("red_review", True, "restored occurrence 2", "restore-1"),
                ("blocked", True, "restored occurrence 1", "restore-1"),
                ("infrastructure_blocked", False, "restored occurrence 1", "restore-1"),
            ],
        )

    def test_the_statement_count_does_not_grow_with_the_lists(self) -> None:
        def write(client: SqlCardClient, n: int) -> None:
            client._execute(
                "INSERT INTO requests (request_id, operation, intent, status, entity_kind, ref, created_at) "
                "VALUES ('restore-1', 'restored', '{}'::jsonb, 'staged', 'sprint', 'sprint:1', %s)",
                (_AT,),
            )
            client.call("saveTaskMetadata", task_id=sprint_key("sprint:1"), values={
                "sprint_repositories": json.dumps([f"/r/{i}" for i in range(n)]),
                "sprint_issues": json.dumps(["issue:i1", "issue:i2", "issue:i3"][: min(n, 3)]),
                "sprint_reservations": json.dumps([f"project-{i}" for i in range(n)]),
                "sprint_budget": json.dumps({"by_type": {"red_review": n}}),
                "sprint_budget_uncharged": json.dumps({"infrastructure_blocked": n}),
            })

        self.assert_constant(write)


class ProductProjectWriteTests(_Case):
    def test_a_project_set_is_written_whole(self) -> None:
        client = self.client()
        key = record_key("product", "ummanu")
        client.call("saveTaskMetadata", task_id=key, values={"product_projects": json.dumps(["zeta", "ummanu", "zeta"])})
        q = client._query
        self.assertEqual(
            q("SELECT project_id FROM product_projects WHERE product_id = 'ummanu' ORDER BY project_id"),
            [("ummanu",), ("zeta",)],
        )
        self.assertEqual(q("SELECT count(*) FROM projects WHERE project_id = 'zeta'"), [(1,)])
        client.call("saveTaskMetadata", task_id=key, values={"product_projects": "[]"})
        self.assertEqual(q("SELECT count(*) FROM product_projects WHERE product_id = 'ummanu'"), [(0,)])

    def test_the_statement_count_does_not_grow_with_the_set(self) -> None:
        def write(client: SqlCardClient, n: int) -> None:
            client.call("saveTaskMetadata", task_id=record_key("product", "ummanu"),
                        values={"product_projects": json.dumps([f"project-{i}" for i in range(n)])})

        self.assert_constant(write)


if __name__ == "__main__":
    unittest.main()
