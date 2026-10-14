"""The board layer's shared row helpers and the clean-up that put them in one place (ummanu-237).

No server: the SQL clients run over `_Recording`, a card client that records each statement it
would send and answers from a small table, so what is proven here is what the code sends and
refuses before any SQL.  That the rewritten statements are valid PostgreSQL, and do what the
row-by-row ones did, is `tests/test_board_bulk_writes_backend.py`.
"""

from __future__ import annotations

import importlib
import inspect
import json
import unittest
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from tests.fakes.board import FakeBoardHost
from ummanu.board import backend, sql_rows
from ummanu.board.events import event_id, render_marker_comment
from ummanu.board.extension_bag import bag_json, card_bag
from ummanu.board.host import Create, MarkerComment, TransitionRequest
from ummanu.board.models import (
    Actor,
    Card,
    CardState,
    EntityKind,
    Event,
    EventKind,
    Product,
    RelatedRefs,
    Sprint,
)
from ummanu.board.owner_handover import WAITING_OWNER, WAITING_OWNER_BY, WAITING_OWNER_REASON, waiting_owner
from ummanu.board.sql_cards import SqlCardClient, SqlCardError, _timestamp
from ummanu.board.sql_host import SqlBoardHost
from ummanu.board.sql_product_issues import ProductIssueRecords
from ummanu.board.sql_sprints import SqlSprintRecords, sprint_key
from ummanu.board.wait_card import parse_utc

Answer = Callable[[str, tuple[Any, ...]], list[tuple[Any, ...]]]

#: One moment in every spelling the board has written it.
MOMENT = datetime(2026, 10, 12, 20, 20, 42, 123456, tzinfo=UTC)
SPELLINGS = (
    "2026-10-12T20:20:42.123456+00:00",  # `datetime.isoformat()`, the old e2e charge writer
    "2026-10-12T20:20:42.123456Z",  # Sprints and events, to the microsecond
    "2026-10-12T20:20:42+00:00",  # an `isoformat()` of a truncated moment
    "2026-10-12T20:20:42Z",  # cards, audit, tasks: the one written form
    "2026-10-12T20:20:42z",
)


class _Recording(SqlCardClient):
    """The card client with no driver: each statement is recorded, and `answer` replies to reads."""

    def __init__(self, answer: Answer | None = None) -> None:
        self.statements: list[tuple[str, tuple[Any, ...]]] = []
        self.answer: Answer = answer or (lambda sql, params: [(1,)])
        self.records = ProductIssueRecords(self)
        self.sprints = SqlSprintRecords(self)

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        self.statements.append((sql, params))
        return self.answer(sql, params)

    def _execute(self, sql: str, params: tuple[Any, ...] = (), *, bounded: bool = True) -> int:
        self.statements.append((sql, params))
        return 1

    def _ref_of(self, task_id: Any) -> str:
        return "ummanu-1"

    def _commit_unless_nested(self) -> None:
        return None

    def _staged(self, kind: str) -> dict[int, dict[str, Any]]:
        return {}


class TimeFormatTests(unittest.TestCase):
    """CON-08: one written form, and readers that take every form the board ever wrote."""

    def test_the_one_writer_spells_seconds_with_a_z(self) -> None:
        self.assertEqual(sql_rows.rfc3339(MOMENT), "2026-10-12T20:20:42Z")
        self.assertEqual(sql_rows.rfc3339(MOMENT, microseconds=True), "2026-10-12T20:20:42.123456Z")
        east = MOMENT.astimezone(timezone(timedelta(hours=5)))
        self.assertEqual(sql_rows.rfc3339(east), "2026-10-12T20:20:42Z")
        self.assertEqual(sql_rows.rfc3339(None), "")

    def test_every_old_and_new_spelling_reads_back_as_the_same_moment(self) -> None:
        for text in SPELLINGS:
            with self.subTest(text=text):
                expected = MOMENT if "123456" in text else MOMENT.replace(microsecond=0)
                self.assertEqual(sql_rows.parse_rfc3339(text), expected)
                self.assertEqual(_timestamp(text), expected)
                self.assertEqual(parse_utc(text, "--wait-until"), expected)
        self.assertIsNone(sql_rows.parse_rfc3339("yesterday"))
        self.assertIsNone(sql_rows.parse_rfc3339(""))
        self.assertEqual(_timestamp("2026-10-12T20:20:42"), MOMENT.replace(microsecond=0))

    def test_a_written_time_round_trips_through_the_readers(self) -> None:
        for microseconds in (False, True):
            written = sql_rows.rfc3339(MOMENT, microseconds=microseconds)
            self.assertEqual(
                sql_rows.parse_rfc3339(written), MOMENT if microseconds else MOMENT.replace(microsecond=0)
            )
            self.assertEqual(
                sql_rows.rfc3339(sql_rows.parse_rfc3339(written), microseconds=microseconds), written
            )

    def test_an_event_record_reads_its_occurred_at_in_the_old_spelling(self) -> None:
        event = Event(
            "board-event-1",
            EventKind.ENTITY_CREATED,
            EntityKind.CARD,
            "ummanu-1",
            Actor("worker", "worker-1"),
            "created",
            MOMENT,
        )
        record = event.to_record("request-1")
        self.assertEqual(record["occurred_at"], "2026-10-12T20:20:42.123456Z")
        for text in SPELLINGS[:2]:
            with self.subTest(text=text):
                self.assertEqual(Event.from_record({**record, "occurred_at": text}), event)

    def test_a_waiting_owner_mark_reads_in_either_spelling(self) -> None:
        for since in SPELLINGS:
            card = {
                "extensions": {
                    "extra": {WAITING_OWNER: since, WAITING_OWNER_REASON: "why", WAITING_OWNER_BY: "po"}
                }
            }
            with self.subTest(since=since):
                self.assertEqual(waiting_owner(card), {"since": since, "reason": "why", "by": "po"})

    def test_an_e2e_charge_keeps_microseconds_and_reads_either_spelling(self) -> None:
        from ummanu import tasks

        written = tasks._charge_time()
        self.assertRegex(written, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z$")

        def answer(sql: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
            if sql.startswith("UPDATE sprints SET e2e_used"):
                return [(3, 1)]
            if sql.startswith("SELECT e2e_budget"):
                return [(3, 1, [])]
            return []

        for at in (SPELLINGS[0], SPELLINGS[1]):
            client = _Recording(answer)
            with self.subTest(at=at):
                self.assertTrue(
                    client.sprints.charge_e2e("sprint:1", task_ref="ummanu-1", dispatch_id="d-1", at=at)[
                        "charged"
                    ]
                )
                [charged_at] = [
                    params[3]
                    for sql, params in client.statements
                    if sql.startswith("INSERT INTO sprint_e2e_charges")
                ]
                self.assertEqual(charged_at, MOMENT)

    def test_a_sprint_charge_is_rendered_to_the_microsecond(self) -> None:
        """Truncated, two charges in one second would export and restore in dispatch-id order."""
        client = _Recording(lambda sql, params: [("sprint:1", "d-1", "ummanu-1", MOMENT)])
        charges = client.sprints._e2e_charges(["sprint:1"])
        self.assertEqual(charges["sprint:1"][0]["at"], "2026-10-12T20:20:42.123456Z")


class RowHelperTests(unittest.TestCase):
    """DUP-B1: one leaf for the helpers, and no lazy import of them left in the SQL modules."""

    def test_grouped_and_epoch(self) -> None:
        self.assertEqual(sql_rows.grouped([(1, "a"), (2, "b"), (1, "c")]), {1: ["a", "c"], 2: ["b"]})
        self.assertEqual(sql_rows.grouped([(1, "a", 2)]), {1: [("a", 2)]})
        self.assertEqual(sql_rows.epoch(MOMENT), str(int(MOMENT.timestamp())))
        self.assertEqual(sql_rows.epoch(None), "")

    def test_the_sql_modules_import_the_leaf_and_nothing_lazily(self) -> None:
        from ummanu.board import sql_audit, sql_cards, sql_product_issues, sql_sprints

        for module in (sql_cards, sql_sprints, sql_product_issues):
            with self.subTest(module=module.__name__):
                self.assertIs(module._grouped, sql_rows.grouped)
                self.assertIs(module._epoch, sql_rows.epoch)
                self.assertIs(module._now, sql_rows.now)
                self.assertNotIn("from ummanu.board.sql_cards import _", inspect.getsource(module))
        self.assertIs(sql_audit._now, sql_rows.now)
        leaf = inspect.getsource(sql_rows)
        self.assertNotIn("import ummanu", leaf)
        self.assertNotIn("from ummanu", leaf)


class CardClientRefusalTests(unittest.TestCase):
    """BUG-24, board: three refusals named before any SQL rather than a crash or a wrong one."""

    def test_a_comment_with_an_unreadable_time_is_refused_before_the_insert(self) -> None:
        client = _Recording()
        with self.assertRaisesRegex(SqlCardError, "created_at 'not a time'"):
            client._rpc_createComment(task_id=1, content="hello", created_at="not a time")
        self.assertEqual(client.statements, [])

    def test_a_comment_takes_either_spelling_of_its_time(self) -> None:
        for text in SPELLINGS[:2]:
            client = _Recording()
            client._rpc_createComment(task_id=1, content="hello", created_at=text)
            [(_, params)] = client.statements
            self.assertEqual(params[3], MOMENT)

    def test_an_unknown_column_is_named(self) -> None:
        client = _Recording()
        with self.assertRaisesRegex(SqlCardError, "no column 99"):
            client._rpc_moveTaskPosition(project_id=1, task_id=1, column_id=99, position=1)
        with self.assertRaisesRegex(SqlCardError, "no column 0"):
            client._rpc_createTask(project_id=1, title="t", column_id=0, reference="ummanu-7")
        self.assertEqual(client.statements, [])

    def test_a_stored_state_no_column_shows_is_named(self) -> None:
        client = _Recording()
        row = ("ummanu-1", 1, "t", "", "archived_somehow", False, 1, {}, MOMENT, MOMENT, None)
        with self.assertRaisesRegex(SqlCardError, "ummanu-1 is in state 'archived_somehow'"):
            client._row(row)

    def test_remove_task_removes_only_a_sprint(self) -> None:
        client = _Recording()
        for key, named in (
            (5, "not card key 5"),
            (backend.record_key("issue", "abc"), f"not issue key {backend.record_key('issue', 'abc')}"),
            (backend.record_key("product", "ummanu"), "not product key"),
        ):
            with (
                self.subTest(key=key),
                self.assertRaisesRegex(SqlCardError, f"removeTask removes only a Sprint, {named}"),
            ):
                client._rpc_removeTask(task_id=key)
        self.assertEqual(client.statements, [])

    def test_remove_task_still_removes_a_sprint(self) -> None:
        client = _Recording(lambda sql, params: [("sprint:4",)])
        self.assertTrue(client._rpc_removeTask(task_id=sprint_key("sprint:4")))
        self.assertEqual(client.statements[-1], ("DELETE FROM sprints WHERE ref = %s", ("sprint:4",)))


class EventIdGoldenTests(unittest.TestCase):
    """DUP-B2: one event-id function, and the ids recorded events already carry stay the same.

    Every literal below was computed on `main` at 8c1ea4cd, before the five copies became one.
    """

    ACTOR = Actor("worker", "worker-1417", "head-run:1417")
    RELATED = RelatedRefs(("product:ummanu", "issue:1"))

    def test_the_function_itself(self) -> None:
        self.assertEqual(
            event_id({"request_id": "r-1", "kind": "card.started", "data": {"b": 1, "a": [1, "x"]}}),
            "board-event-4dc812a9c58f2cceab9f8f7311345f1e",
        )

    def test_the_fake_host(self) -> None:
        created = FakeBoardHost().create(
            Create(
                Card("ummanu-1417", "Protocol seam", CardState.READY, sprint_ref="sprint:943"),
                self.ACTOR,
                "accepted into the sprint",
                request_id="golden-create",
            )
        )
        self.assertEqual(created.event.event_id, "board-event-4365c84a955856a62c2dba81ac86c5ac")

    def test_the_sql_host_builders(self) -> None:
        card = Card("ummanu-1417", "Protocol seam", CardState.IN_PROGRESS)
        transition = TransitionRequest(
            EntityKind.CARD,
            card.ref,
            CardState.VALIDATE,
            self.ACTOR,
            "handed to review",
            self.RELATED,
            "golden-transition",
        )
        marker = MarkerComment(
            card.ref,
            EventKind.CARD_REPORTED,
            self.ACTOR,
            "all green",
            {"marker": "report:done", "body": "all green", "status": "done"},
            self.RELATED,
            "golden-marker",
        )
        ids = {
            "sprint": SqlBoardHost._sprint_event(
                EventKind.SPRINT_CLOSED,
                Sprint("sprint:943", "Ship the seam"),
                self.ACTOR,
                "Sprint closed",
                self.RELATED,
                "golden-sprint",
                source="open",
                target="closed",
            ).event_id,
            "entity": SqlBoardHost._entity_event(
                None,
                EventKind.ENTITY_CREATED,
                Product("product:ummanu", "Ummanu"),
                self.ACTOR,
                "created",  # type: ignore[arg-type]
                self.RELATED,
                "golden-product",
            ).event_id,
            "transition": SqlBoardHost._event(
                card, EventKind.CARD_SUBMITTED, transition, self.RELATED, "golden-transition"
            ).event_id,
            "marker": SqlBoardHost._marker_event(
                card, marker, self.RELATED, "golden-marker", marker_occurrence=1
            ).event_id,
        }
        self.assertEqual(
            ids,
            {
                "sprint": "board-event-c3386aaf18a7d77561635238ddead1e3",
                "entity": "board-event-3c68873c858e6ddc7cbf821f485d8092",
                "transition": "board-event-ad96436c41ad595a45ca491395f13165",
                "marker": "board-event-567a35596b65630da97689daeec9080c",
            },
        )


class MarkerValidationTests(unittest.TestCase):
    """DUP-B5: the marker payload is checked once, when the Event is made."""

    @staticmethod
    def event(**data: Any) -> Event:
        body = str(data.get("body", "the body"))
        return Event(
            "board-event-1",
            EventKind.CARD_REPORTED,
            EntityKind.CARD,
            "ummanu-1",
            Actor("worker", "worker-1"),
            body,
            MOMENT,
            data={"body": body, **data},
        )

    def test_an_invalid_marker_is_refused_at_creation(self) -> None:
        for data, message in (
            ({"marker": "report:done", "status": "blocked"}, "unsupported marker payload"),
            ({"marker": "report:blocked", "status": "blocked"}, "supported classification"),
            (
                {"marker": "report:done", "status": "done", "classification": "external_fact"},
                "no classification",
            ),
            ({"marker": "", "status": "done"}, "non-empty marker and body"),
        ):
            with self.subTest(data=data), self.assertRaisesRegex(ValueError, message):
                self.event(**data)
        with self.assertRaisesRegex(ValueError, "reason must match its body"):
            Event(
                "board-event-1",
                EventKind.CARD_VERDICTED,
                EntityKind.CARD,
                "ummanu-1",
                Actor("reviewer", "r-1"),
                "other",
                MOMENT,
                data={"marker": "review:green", "body": "body", "status": "green"},
            )

    def test_a_valid_marker_renders_its_grammar(self) -> None:
        self.assertEqual(
            render_marker_comment(self.event(marker="report:done", status="done")), "[report:done]\nthe body"
        )
        self.assertEqual(
            render_marker_comment(
                self.event(marker="report:blocked", status="blocked", classification="external_fact")
            ),
            "[report:blocked]\nclassification: external_fact\n\nthe body",
        )

    def test_a_non_marker_event_is_not_rendered(self) -> None:
        event = Event(
            "board-event-1",
            EventKind.CARD_STARTED,
            EntityKind.CARD,
            "ummanu-1",
            Actor("worker", "w"),
            "go",
            MOMENT,
        )
        with self.assertRaisesRegex(ValueError, "not a Card marker occurrence"):
            render_marker_comment(event)


class ExtensionBagTests(unittest.TestCase):
    """DUP-B4: the bag and its JSON fields are read in one place."""

    def test_card_bag_and_bag_json(self) -> None:
        card = {"extensions": {"extra": {"spec": '{"a": 1}', "object": {"b": 2}, "broken": "{"}}}
        self.assertEqual(card_bag(card)["spec"], '{"a": 1}')
        self.assertEqual(bag_json(card, "spec"), {"a": 1})
        self.assertEqual(bag_json(card, "object"), {"b": 2})
        self.assertIsNone(bag_json(card, "broken"))
        self.assertIsNone(bag_json(card, "absent"))
        for malformed in ({}, {"extensions": "x"}, {"extensions": {"extra": []}}):
            self.assertEqual(card_bag(malformed), {})


class FrozenRevisionKeyTests(unittest.TestCase):
    """0004 keeps its own copy of the key formula it backfilled with."""

    def test_0004_no_longer_imports_the_live_function(self) -> None:
        revision = importlib.import_module("ummanu.board.migrations.versions.0004_product_issue_sql")
        self.assertIsNot(revision.record_key, backend.record_key)
        self.assertNotIn("from ummanu.board.backend import", inspect.getsource(revision))
        self.assertIn("frozen copy of record_key as of", inspect.getsource(revision))

    def test_0004_frozen_copy_agrees_with_the_live_function(self) -> None:
        revision = importlib.import_module("ummanu.board.migrations.versions.0004_product_issue_sql")
        identifiers = (
            "ummanu",
            "f" * 20,
            "0001",
            " padded ",
            "ü-ñ",
            "sprint:0",
            "sprint:1596",
            "sprint:canary",
            "sprint:١",
        )
        for kind in backend.RECORD_KINDS:
            for identifier in identifiers:
                with self.subTest(kind=kind, identifier=identifier):
                    self.assertEqual(
                        revision.record_key(kind, identifier), backend.record_key(kind, identifier)
                    )
        for kind, identifier in (
            ("card", "x"),
            ("product", "  "),
            ("sprint", "sprint:500000000"),
            ("sprint", "sprint:01"),
        ):
            with self.subTest(kind=kind, identifier=identifier):
                with self.assertRaises(ValueError):
                    revision.record_key(kind, identifier)
                with self.assertRaises(backend.BoardIdentityError):
                    backend.record_key(kind, identifier)


class StatementCountTests(unittest.TestCase):
    """INEF-16: a list written into a satellite table costs the same statements at any length."""

    def counted(self, write: Callable[[_Recording, int], Any], answer: Answer | None = None) -> list[int]:
        counts = []
        for n in (3, 9):
            client = _Recording(answer)
            write(client, n)
            counts.append(len(client.statements))
        return counts

    def assert_constant(self, counts: list[int], expected: int) -> None:
        self.assertEqual(counts, [expected, expected], "statements grew with the list")

    def test_card_metadata_links_and_bag_removals(self) -> None:
        def write(client: _Recording, n: int) -> None:
            client._rpc_saveTaskMetadata(
                task_id=1,
                values={
                    "retry_heads": ",".join(f"head-{i}" for i in range(n)),
                    "blocked_by": ",".join(f"ummanu-{i + 2}" for i in range(n)),
                    "issues": ",".join(f"issue:{i}" for i in range(n)),
                    **{f"note-{i}": "" for i in range(n)},
                },
            )

        # DELETE + INSERT for each of the three links, and one bag removal for every dropped key.
        self.assert_constant(self.counted(write), 7)

    def test_sprint_relations(self) -> None:
        def write(client: _Recording, n: int) -> None:
            client.sprints._replace_relations(
                "sprint:1",
                {
                    "sprint_repositories": json.dumps([f"/repo/{i}" for i in range(n)]),
                    "sprint_issues": json.dumps([f"issue:{i}" for i in range(n)]),
                    "sprint_reservations": json.dumps([f"project-{i}" for i in range(n)]),
                },
            )

        # Repositories: DELETE + one upsert-and-link; issues: DELETE + INSERT; reservations:
        # release, status read, the project rows and the reservation upsert.
        self.assert_constant(self.counted(write, lambda sql, params: [("open",)]), 8)

    def test_a_repeated_reservation_keeps_its_last_place(self) -> None:
        client = _Recording(lambda sql, params: [("open",)])
        client.sprints._replace_relations("sprint:1", {"sprint_reservations": json.dumps(["a", "b", "a"])})
        [params] = [
            params for sql, params in client.statements if sql.startswith("INSERT INTO sprint_projects")
        ]
        self.assertEqual((params[4], params[5]), (["a", "b"], [2, 1]))

    def test_restored_budget_events(self) -> None:
        def answer(sql: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
            if "FROM requests" in sql:
                return [("restore-1", {})] if params[-1] == "restored" else []
            return []

        def write(client: _Recording, n: int) -> None:
            client.sprints._apply(
                "sprint:1",
                {
                    "sprint_budget": json.dumps({"by_type": {"red_review": n, "blocked": 1}}),
                    "sprint_budget_uncharged": json.dumps({"infrastructure_blocked": n}),
                },
            )

        # The two request reads, the DELETE and one INSERT of every restored occurrence.
        self.assert_constant(self.counted(write, answer), 4)

    def test_restored_budget_events_keep_their_listed_order(self) -> None:
        client = _Recording(
            lambda sql, params: (
                [("restore-1", {})] if "FROM requests" in sql and params[-1] == "restored" else []
            )
        )
        client.sprints._apply(
            "sprint:1",
            {
                "sprint_budget": json.dumps({"by_type": {"red_review": 2, "blocked": 1}}),
                "sprint_budget_uncharged": json.dumps({"infrastructure_blocked": 1}),
            },
        )
        [params] = [
            params for sql, params in client.statements if sql.startswith("INSERT INTO sprint_budget_events")
        ]
        self.assertEqual(
            list(zip(params[3], params[4], params[5], strict=True)),
            [
                ("red_review", True, "restored occurrence 1"),
                ("red_review", True, "restored occurrence 2"),
                ("blocked", True, "restored occurrence 1"),
                ("infrastructure_blocked", False, "restored occurrence 1"),
            ],
        )

    def test_product_projects(self) -> None:
        def write(client: _Recording, n: int) -> None:
            client.records._write_projects("ummanu", json.dumps([f"project-{i}" for i in range(n)]))

        self.assert_constant(self.counted(write), 3)


if __name__ == "__main__":
    unittest.main()
