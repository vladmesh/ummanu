"""Whole-board listings over the PostgreSQL board store cost a fixed number of statements.

A listing reads the rows in one statement and then asks `getTaskMetadata` (and, for an export,
`getAllComments`) of every row in one `call_batch`.  `SqlCardClient.call_batch` answers such a
batch set-based, so the statement count of listing every issue, every sprint and every card does
not depend on how many there are.  Two proofs live here:

* the count: the same listing over N and over 10×N records issues the same number of
  statements, counted at `psycopg.Cursor.execute`;
* the parity: what the bulk path answers is exactly what the per-record reads answered before it
  replaced them.  Those reads are kept below, verbatim in their SQL, as the oracle
  (`_PerRecordOracle`); they are no longer product code.

Like the other `*_sql_backend` suites this needs Docker and never skips.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

from tests.sql_backend_fixtures import PostgresBoard
from ummanu.board.backend import record_key
from ummanu.board.sql_cards import SqlCardClient, SqlCardError
from ummanu.board.sql_sprints import sprint_key

BOARD: PostgresBoard


def setUpModule() -> None:
    global BOARD
    for module in ("psycopg", "sqlalchemy", "alembic"):
        __import__(module)
    BOARD = PostgresBoard()


def tearDownModule() -> None:
    BOARD.stop()


_EPOCH = datetime(2026, 9, 1, tzinfo=UTC)


def _at(offset: int) -> datetime:
    return _EPOCH + timedelta(minutes=offset)


def _seed(client: SqlCardClient, n: int) -> None:
    """N issues, N sprints and N cards, each with every child row its metadata reads.

    Child rows are inserted out of their read order on purpose, so the parity test can only pass
    if the bulk path orders each record's children the way the per-record path did.
    """
    q = client._execute
    with client.transaction():
        for project in ("ummanu", "zeta"):
            q("INSERT INTO projects (project_id) VALUES (%s)", (project,))
        for product in ("ummanu", "zeta"):
            q(
                "INSERT INTO products (product_id, board_key, title, description, state, extensions, "
                "created_at, updated_at) VALUES (%s,%s,%s,'',%s,%s::jsonb,%s,%s)",
                (
                    product, record_key("product", product), product.title(),
                    "archived" if product == "zeta" else "active",
                    json.dumps({"extra": {"legacy": "kept", "product_id": "shadowed"}}),
                    _at(0), _at(1),
                ),
            )
            for project in ("zeta", "ummanu"):
                q("INSERT INTO product_projects (product_id, project_id) VALUES (%s,%s)", (product, project))
            q(
                "INSERT INTO product_comments (product_id, marker, body, created_at) VALUES (%s,'po',%s,%s)",
                (product, f"[po]\nabout {product}", _at(2)),
            )
        issues = [f"{index:020x}" for index in range(1, n + 1)]
        for index, issue in enumerate(issues):
            closed = index % 3 == 2
            q(
                "INSERT INTO issues (issue_id, board_key, product_id, title, description, issue_kind, "
                "priority, state, close_reason, extensions, created_at, updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)",
                (
                    issue, record_key("issue", issue), "ummanu" if index % 2 else "zeta",
                    f"Issue {index}", f"body {index}", ("bug", "feature", "question")[index % 3],
                    f"P{index % 4}", "closed" if closed else "open",
                    "resolved" if closed else None,
                    json.dumps({"extra": {"swimlane": "x", "note": str(index), "issue_kind": "no"}}),
                    _at(index), _at(index + 1),
                ),
            )
            # Two comments at the same instant: the id breaks the tie, as before.
            for body in ("second", "first"):
                q(
                    "INSERT INTO issue_comments (issue_id, marker, body, created_at) VALUES (%s,NULL,%s,%s)",
                    (issue, f"{body} on {issue}", _at(10 - index % 2)),
                )
        for number in range(1, n + 1):
            ref = f"sprint:{number}"
            status = ("open", "closed", "stopped")[number % 3]
            q(
                "INSERT INTO sprints (ref, board_key, sprint_number, goal, definition_of_done, product_id, "
                "status, observer, worker_pin, reviewer_pin, source_audit, local_run_exceptions, "
                "created_at, updated_at, closed_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s)",
                (
                    ref, sprint_key(ref), number, f"Goal {number}", f"DoD {number}",
                    "ummanu" if number % 2 else None, status,
                    json.dumps({"profile": "claude-observer", "b": 1}) if number % 2 else None,
                    "codex-high" if number % 2 else None, "claude-opus" if number % 4 == 1 else None,
                    json.dumps({"z": 1, "a": [number]}) if number % 3 == 0 else None,
                    json.dumps([{"project": f"held-{number}", "argv": ["probe", "two words", ""],
                                 "rationale": "owner's exact probe"}] if number % 2 else []),
                    _at(number), _at(number + 1), None if status == "open" else _at(number + 2),
                ),
            )
            # Tied ordinals, as imported Sprints carry them: storage order is the answer's order.
            for ordinal, path in ((1, f"/repo/b{number}"), (0, f"/repo/a{number}"), (1, f"/repo/0{number}")):
                rows = client._query(
                    "INSERT INTO repositories (path) VALUES (%s) RETURNING repository_id", (path,)
                )
                q(
                    "INSERT INTO sprint_repositories (sprint_ref, repository_id, ordinal) VALUES (%s,%s,%s)",
                    (ref, rows[0][0], ordinal),
                )
            if number % 2:
                for ordinal, issue in ((1, issues[-1]), (0, issues[1]), (1, issues[0])):
                    q(
                        "INSERT INTO sprint_issues (sprint_ref, issue_id, ordinal) VALUES (%s,%s,%s)",
                        (ref, issue, ordinal),
                    )
            # One live reservation per project is a store invariant, so each Sprint holds its own.
            q("INSERT INTO projects (project_id) VALUES (%s)", (f"held-{number}",))
            for ordinal, project, reserved in ((1, f"held-{number}", True), (0, "zeta", False)):
                q(
                    "INSERT INTO sprint_projects (sprint_ref, project_id, reserved, reserved_at, "
                    "released_at, ordinal) VALUES (%s,%s,%s,%s,%s,%s)",
                    (ref, project, reserved, _at(0), None if reserved else _at(1), ordinal),
                )
            if number % 2:
                rows = client._query(
                    "INSERT INTO sprint_resumes (sprint_ref, selected_step, selected_why, "
                    "rejected_alternatives, current_task, dod_state, next_safe_step, recorded_at, "
                    "recorded_at_source) VALUES (%s,'step','why','none','task','dod','next',%s,%s) "
                    "RETURNING resume_id",
                    (ref, _at(number), "2026-09-01T00:00:00" if number % 3 == 1 else None),
                )
                q("UPDATE sprints SET resume_id = %s WHERE ref = %s", (rows[0][0], ref))
            request = f"budget-{number}"
            q(
                "INSERT INTO requests (request_id, operation, intent, status, entity_kind, ref, "
                "created_at, settled_at) VALUES (%s,'sprint.budget','{}'::jsonb,'committed','sprint',%s,%s,%s)",
                (request, ref, _at(0), _at(0)),
            )
            events = ["red_review", "blocked", "red_review", "hotfix"][: 1 + number % 4]
            if number % 2 == 0:
                events.append("infrastructure_blocked")
            for event in events:
                q(
                    "INSERT INTO sprint_budget_events (sprint_ref, event_type, charged, reason, "
                    "request_id, occurred_at) VALUES (%s,%s,%s,'r',%s,%s)",
                    (ref, event, event != "infrastructure_blocked", request, _at(number)),
                )
            for body in ("later", "earlier"):
                q(
                    "INSERT INTO sprint_comments (sprint_ref, marker, body, created_at) VALUES (%s,NULL,%s,%s)",
                    (ref, f"{body} {ref}", _at(20 if body == "later" else 5)),
                )
        refs = [f"ummanu-{number}" for number in range(1, n + 1)]
        states = ("ready", "in_progress", "done", "blocked")
        for index, ref in enumerate(refs):
            q(
                "INSERT INTO tasks (task_ref, task_number, project_id, title, description, task_type, "
                "state, archived, position, review, live_impact, claim_worker, slug, retry_same, "
                "quota_snapshot_at, sprint_ref, extensions, created_at, updated_at, date_moved) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)",
                (
                    ref, index + 1, "ummanu", f"Card {index}", "", "research" if index % 3 == 0 else "code",
                    states[index % 4], index % 5 == 4, index, "required" if index % 2 else None,
                    index % 3 == 0, "worker" if index % 2 else None, f"slug-{index}", index % 3,
                    _at(index) if index % 2 else None, "sprint:1" if index % 2 else None,
                    json.dumps({"extra": {"swimlane": "ummanu", "extra": str(index)}}),
                    _at(index), _at(index + 1), _at(index + 2),
                ),
            )
        for index, ref in enumerate(refs):
            for ordinal, head in ((1, "codex-high"), (0, "claude-opus")):
                q(
                    "INSERT INTO task_retry_heads (task_ref, ordinal, head) VALUES (%s,%s,%s)",
                    (ref, ordinal, head),
                )
            for dependency in (refs[-1], "other-9", refs[0]):
                if dependency != ref:
                    q(
                        "INSERT INTO task_dependencies (task_ref, depends_on, depends_on_task) "
                        "VALUES (%s,%s,(SELECT task_ref FROM tasks WHERE task_ref = %s))",
                        (ref, dependency, dependency),
                    )
            if index:
                q(
                    "INSERT INTO task_supersessions (task_ref, supersedes, recorded_at) VALUES (%s,%s,%s)",
                    (ref, refs[index - 1], _at(index)),
                )
            for issue in (issues[-1], issues[0]):
                q("INSERT INTO task_issues (task_ref, issue_id) VALUES (%s,%s) ON CONFLICT DO NOTHING", (ref, issue))
            for body, minute in (("b", 3), ("a", 3), ("c", 1)):
                q(
                    "INSERT INTO task_comments (task_ref, marker, body, created_at) VALUES (%s,NULL,%s,%s)",
                    (ref, f"{body} {ref}", _at(minute)),
                )


class _PerRecordOracle:
    """The per-record reads this card replaced, kept as the parity oracle.

    Each method is the pre-change body of `getTaskMetadata` / `getAllComments` for one record
    kind, with its statements unchanged, issued one record at a time.
    """

    def __init__(self, client: SqlCardClient) -> None:
        self.q = client._query

    def card_metadata(self, key: int) -> dict[str, str]:
        from ummanu.board.sql_cards import _rfc3339, _text

        ref = self.q("SELECT task_ref FROM tasks WHERE board_key = %s", (key,))[0][0]
        values = self.q(
            "SELECT project_id, task_type, claim_worker, slug, base_branch, seed_ref, complexity, "
            "family_preference, head_override, review_head_override, resolved_worker_head, "
            "resolved_review_head, routing_reason, codex_launch_mode, sprint_ref, retry_same, "
            "retry_switch, quota_snapshot_at, extensions, review, live_impact FROM tasks "
            "WHERE task_ref = %s",
            (ref,),
        )[0]
        names = (
            "project", "task_type", "claim", "slug", "base_branch", "seed_ref", "complexity",
            "family_preference", "head", "review_head", "resolved_head", "resolved_review_head",
            "routing_reason", "codex_launch_mode", "sprint_ref",
        )
        meta: dict[str, str] = {}
        for name, value in zip(names, values[: len(names)], strict=True):
            if value is not None and _text(value):
                meta[name] = _text(value)
        for name, value in (("retry_same", values[15]), ("retry_switch", values[16])):
            if value:
                meta[name] = str(value)
        if values[17] is not None:
            meta["quota_snapshot_at"] = _rfc3339(values[17])
        if values[19] is not None:
            meta["review"] = _text(values[19])
        if values[20]:
            meta["live_impact"] = "1"
        heads = [row[0] for row in self.q(
            "SELECT head FROM task_retry_heads WHERE task_ref = %s ORDER BY ordinal", (ref,)
        )]
        if heads:
            meta["retry_heads"] = ",".join(heads)
        blocked = [row[0] for row in self.q(
            "SELECT depends_on FROM task_dependencies WHERE task_ref = %s ORDER BY depends_on", (ref,)
        )]
        if blocked:
            meta["blocked_by"] = ",".join(blocked)
        supersedes = self.q("SELECT supersedes FROM task_supersessions WHERE task_ref = %s", (ref,))
        if supersedes:
            meta["supersedes"] = supersedes[0][0]
        issues = self.q("SELECT issue_id FROM task_issues WHERE task_ref = %s ORDER BY issue_id", (ref,))
        if issues:
            meta["issues"] = ",".join(f"issue:{issue_id}" for (issue_id,) in issues)
        bag = values[18] if isinstance(values[18], dict) else json.loads(values[18] or "{}")
        for name, value in (bag.get("extra") or {}).items():
            if name != "swimlane":
                meta[name] = _text(value)
        # secretary-1678: a `tasks` row states its record type, as `products` and `issues` rows do.
        meta["record_type"] = "task"
        return meta

    def card_comments(self, key: int) -> list[dict[str, Any]]:
        from ummanu.board.sql_cards import _epoch

        ref = self.q("SELECT task_ref FROM tasks WHERE board_key = %s", (key,))[0][0]
        return [
            {"id": identifier, "date_creation": _epoch(created), "comment": body}
            for identifier, body, created in self.q(
                "SELECT comment_id, body, created_at FROM task_comments WHERE task_ref = %s "
                "ORDER BY created_at, comment_id",
                (ref,),
            )
        ]

    def record_metadata(self, kind: str, key: int) -> dict[str, str]:
        from ummanu.board.sql_product_issues import ISSUE_KEYS, PRODUCT_KEYS, _text

        if kind == "product":
            identifier = self.q("SELECT product_id FROM products WHERE board_key = %s", (key,))[0][0]
            rows = self.q("SELECT extensions FROM products WHERE product_id = %s", (identifier,))
            projects = [row[0] for row in self.q(
                "SELECT project_id FROM product_projects WHERE product_id = %s ORDER BY project_id",
                (identifier,),
            )]
            meta = {
                "record_type": "product",
                "product_id": identifier,
                "product_projects": json.dumps(projects, separators=(",", ":")),
            }
            bag = rows[0][0] if isinstance(rows[0][0], dict) else json.loads(rows[0][0] or "{}")
            for name, value in (bag.get("extra") or {}).items():
                if name not in PRODUCT_KEYS:
                    meta[name] = _text(value)
            return meta
        identifier = self.q("SELECT issue_id FROM issues WHERE board_key = %s", (key,))[0][0]
        product_id, issue_kind, priority, close_reason, extensions = self.q(
            "SELECT product_id, issue_kind, priority, close_reason, extensions FROM issues "
            "WHERE issue_id = %s",
            (identifier,),
        )[0]
        meta = {
            "record_type": "issue",
            "issue_product": _text(product_id),
            "issue_kind": _text(issue_kind),
            "issue_priority": _text(priority),
        }
        if close_reason:
            meta["issue_closed_reason"] = _text(close_reason)
        bag = extensions if isinstance(extensions, dict) else json.loads(extensions or "{}")
        for name, value in (bag.get("extra") or {}).items():
            if name not in ISSUE_KEYS:
                meta[name] = _text(value)
        return meta

    def record_comments(self, kind: str, key: int) -> list[dict[str, Any]]:
        from ummanu.board.sql_cards import _epoch

        table, column = ("product_comments", "product_id") if kind == "product" else ("issue_comments", "issue_id")
        identifier = self.q(
            f"SELECT {column} FROM {'products' if kind == 'product' else 'issues'} WHERE board_key = %s",
            (key,),
        )[0][0]
        return [
            {"id": value, "date_creation": _epoch(created), "comment": body}
            for value, body, created in self.q(
                f"SELECT comment_id, body, created_at FROM {table} WHERE {column} = %s "
                "ORDER BY created_at, comment_id",
                (identifier,),
            )
        ]

    def sprint_metadata(self, key: int) -> dict[str, str]:
        from ummanu.board.sql_sprints import _rfc3339

        reference = self.q("SELECT ref FROM sprints WHERE board_key = %s", (key,))[0][0]
        goal, dod, product, status, observer, worker, reviewer, current, source, exceptions = self.q(
            "SELECT goal, definition_of_done, product_id, status, observer, worker_pin, reviewer_pin, "
            "current_task_ref, source_audit, local_run_exceptions FROM sprints WHERE ref = %s", (reference,)
        )[0]
        values: dict[str, str] = {
            "sprint_goal": str(goal), "sprint_definition_of_done": str(dod),
            "sprint_status": str(status), "sprint_current_task": str(current or ""),
        }
        repositories = [r[0] for r in self.q(
            "SELECT r.path FROM sprint_repositories sr JOIN repositories r USING (repository_id) "
            "WHERE sr.sprint_ref = %s ORDER BY sr.ordinal", (reference,)
        )]
        values["sprint_repositories"] = json.dumps(repositories, separators=(",", ":"))
        if product is not None:
            values["sprint_product"] = str(product)
        issues = [f"issue:{r[0]}" for r in self.q(
            "SELECT issue_id FROM sprint_issues WHERE sprint_ref = %s ORDER BY ordinal", (reference,)
        )]
        if product is not None or issues:
            values["sprint_issues"] = json.dumps(issues, separators=(",", ":"))
        projects = [r[0] for r in self.q(
            "SELECT project_id FROM sprint_projects WHERE sprint_ref = %s "
            "AND (%s <> 'open' OR reserved) ORDER BY ordinal, project_id",
            (reference, str(status)),
        )]
        if product is not None or projects:
            values["sprint_reservations"] = json.dumps(projects, separators=(",", ":"))
        if observer is not None:
            values["sprint_observer"] = json.dumps(observer, sort_keys=True, separators=(",", ":"))
        if worker is not None:
            values["sprint_worker"] = str(worker)
        if reviewer is not None:
            values["sprint_reviewer"] = str(reviewer)
        if source is not None:
            values["sprint_source_audit"] = json.dumps(source, sort_keys=True, separators=(",", ":"))
        values["sprint_local_run_exceptions"] = json.dumps(exceptions, sort_keys=True, separators=(",", ":"))
        resume = self.q(
            "SELECT selected_step, selected_why, rejected_alternatives, current_task, dod_state, "
            "next_safe_step, recorded_at, recorded_at_source, po_request FROM sprint_resumes WHERE resume_id = "
            "(SELECT resume_id FROM sprints WHERE ref = %s)", (reference,)
        )
        if resume:
            names = ("selected_step", "selected_why", "rejected_alternatives", "current_task", "dod_state", "next_safe_step")
            document = dict(zip(names, resume[0][:6], strict=True))
            document["recorded_at"] = str(resume[0][7] or _rfc3339(resume[0][6]))
            if resume[0][8] is not None:
                document["po_request"] = resume[0][8]
            values["sprint_resume"] = json.dumps(document, separators=(",", ":"))
        else:
            values["sprint_resume"] = ""
        counts = {str(kind): int(count) for kind, count in self.q(
            "SELECT event_type, count(*) FROM sprint_budget_events WHERE sprint_ref = %s AND charged "
            "GROUP BY event_type", (reference,)
        )}
        values["sprint_budget"] = json.dumps({"by_type": counts}, separators=(",", ":"))
        uncharged = {str(kind): int(count) for kind, count in self.q(
            "SELECT event_type, count(*) FROM sprint_budget_events WHERE sprint_ref = %s AND NOT charged "
            "GROUP BY event_type", (reference,)
        )}
        if uncharged:
            values["sprint_budget_uncharged"] = json.dumps(uncharged, separators=(",", ":"))
        return values

    def sprint_comments(self, key: int) -> list[dict[str, Any]]:
        from ummanu.board.sql_sprints import _epoch

        reference = self.q("SELECT ref FROM sprints WHERE board_key = %s", (key,))[0][0]
        return [
            {"id": identifier, "date_creation": _epoch(created), "comment": body}
            for identifier, body, created in self.q(
                "SELECT comment_id, body, created_at FROM sprint_comments WHERE sprint_ref=%s "
                "ORDER BY created_at, comment_id",
                (reference,),
            )
        ]


def _budget_parsed(meta: dict[str, str]) -> dict[str, Any]:
    """The two budget keys as their JSON value.

    The per-record read filled each count map in `GROUP BY` output order, which PostgreSQL does
    not define, so its key order was never a property of the answer; the bulk read orders by
    event type.  Every other value is compared as the exact string.
    """
    return {
        name: json.loads(value) if name in {"sprint_budget", "sprint_budget_uncharged"} else value
        for name, value in meta.items()
    }


class _Case(unittest.TestCase):
    def client(self, n: int) -> SqlCardClient:
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        client = SqlCardClient(BOARD.fresh_database().for_role("owner"), Path(root.name))
        self.addCleanup(client.close)
        _seed(client, n)
        return client

    @staticmethod
    def keys(client: SqlCardClient) -> dict[str, list[int]]:
        q = client._query
        return {
            "card": [int(k) for (k,) in q("SELECT board_key FROM tasks ORDER BY task_ref")],
            "product": [int(k) for (k,) in q("SELECT board_key FROM products ORDER BY product_id")],
            "issue": [int(k) for (k,) in q("SELECT board_key FROM issues ORDER BY issue_id")],
            "sprint": [int(k) for (k,) in q("SELECT board_key FROM sprints ORDER BY ref")],
        }


class StatementCountTests(_Case):
    """AC1: N and 10×N records cost the same number of statements, for all three listings."""

    N = 4

    @staticmethod
    def count(action: Any) -> tuple[int, Any]:
        import psycopg

        executed: list[str] = []
        original = psycopg.Cursor.execute

        def counting(cursor: Any, query: Any, *args: Any, **kwargs: Any) -> Any:
            executed.append(str(query))
            return original(cursor, query, *args, **kwargs)

        with mock.patch.object(psycopg.Cursor, "execute", counting):
            result = action()
        return len(executed), result

    def listed(self, listing: Any) -> list[tuple[int, int]]:
        """(statements, records) for N and for 10×N, each on a store of its own."""
        measured = []
        for n in (self.N, 10 * self.N):
            client = self.client(n)
            statements, result = self.count(lambda client=client: listing(client))
            measured.append((statements, len(result)))
        return measured

    def assert_bounded(self, measured: list[tuple[int, int]]) -> None:
        (small, small_records), (large, large_records) = measured
        self.assertGreater(large_records, small_records)
        self.assertEqual(small, large, f"statements grew with the records: {measured}")

    def test_listing_all_issues(self) -> None:
        from ummanu.product_issues import ProductIssueStore

        def listing(client: SqlCardClient) -> list[dict[str, Any]]:
            root = Path(client.instance_dir)
            store = ProductIssueStore(client, data_dir=root / "data", instance=root)
            return store.list_issues(include_closed=True)

        self.assert_bounded(self.listed(listing))

    def test_listing_all_sprints(self) -> None:
        from ummanu.sprints import SprintReader

        self.assert_bounded(self.listed(lambda client: SprintReader(client).list(create=False)))
        self.assert_bounded(self.listed(lambda client: SprintReader(client).export()))

    def test_listing_all_tasks(self) -> None:
        from ummanu.tasks import TaskReader

        self.assert_bounded(self.listed(lambda client: TaskReader(client).list()))
        self.assert_bounded(self.listed(lambda client: list(TaskReader(client).restore_snapshot())))

    def test_a_raw_batch_of_every_record_is_bounded(self) -> None:
        def batch(client: SqlCardClient) -> list[Any]:
            keys = [key for group in self.keys(client).values() for key in group]
            return client.call_batch(
                (method, {"task_id": key})
                for key in keys
                for method in ("getTaskMetadata", "getAllComments")
            )

        self.assert_bounded(self.listed(batch))


class BulkParityTests(_Case):
    """AC2: the bulk answer is the per-record answer, record for record and child for child."""

    def setUp(self) -> None:
        self.store = self.client(9)
        self.oracle = _PerRecordOracle(self.store)
        self.all_keys = self.keys(self.store)

    def batch(self, method: str, keys: list[int]) -> list[Any]:
        return self.store.call_batch((method, {"task_id": key}) for key in keys)

    def test_card_metadata_and_comments(self) -> None:
        keys = self.all_keys["card"]
        self.assertEqual(self.batch("getTaskMetadata", keys), [self.oracle.card_metadata(k) for k in keys])
        self.assertEqual(self.batch("getAllComments", keys), [self.oracle.card_comments(k) for k in keys])

    def test_product_and_issue_metadata_and_comments(self) -> None:
        for kind in ("product", "issue"):
            keys = self.all_keys[kind]
            self.assertEqual(
                self.batch("getTaskMetadata", keys), [self.oracle.record_metadata(kind, k) for k in keys]
            )
            self.assertEqual(
                self.batch("getAllComments", keys), [self.oracle.record_comments(kind, k) for k in keys]
            )

    def test_sprint_metadata_and_comments(self) -> None:
        keys = self.all_keys["sprint"]
        self.assertEqual(
            [_budget_parsed(meta) for meta in self.batch("getTaskMetadata", keys)],
            [_budget_parsed(self.oracle.sprint_metadata(k)) for k in keys],
        )
        self.assertEqual(self.batch("getAllComments", keys), [self.oracle.sprint_comments(k) for k in keys])

    def test_a_mixed_batch_answers_in_call_order_and_a_single_call_agrees(self) -> None:
        calls = []
        for kind in ("sprint", "issue", "card", "product", "card"):
            for key in reversed(self.all_keys[kind][:3]):
                calls += [("getAllComments", {"task_id": key}), ("getTaskMetadata", {"task_id": key})]
        calls.append(calls[0])
        answers = self.store.call_batch(calls)
        self.assertEqual(answers, [self.store.call(method, **arguments) for method, arguments in calls])

    def test_a_missing_record_is_refused_as_before(self) -> None:
        present = self.all_keys["card"][0]
        with self.assertRaisesRegex(SqlCardError, "no card carries transport key 1999999"):
            self.batch("getTaskMetadata", [present, 1_999_999])
        issue = record_key("issue", "f" * 20)
        with self.assertRaisesRegex(SqlCardError, f"no issue carries the board key {issue}"):
            self.batch("getAllComments", [issue])
        sprint = sprint_key("sprint:99999")
        with self.assertRaisesRegex(SqlCardError, f"no unique Sprint carries transport key {sprint}"):
            self.batch("getTaskMetadata", [sprint])


if __name__ == "__main__":
    unittest.main()
