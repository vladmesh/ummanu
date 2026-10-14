"""Pure SQL projections through their adapters, with independent expected board values."""

from __future__ import annotations

import ast
import json
import sys
import unittest
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from ummanu.board import sql_audit, sql_cards, sql_product_issues, sql_rows, sql_sprints
from ummanu.board.backend import record_key
from ummanu.board.sql_cards import SqlCardClient, SqlCardError
from ummanu.board.sql_product_issues import ProductIssueRecords
from ummanu.board.sql_sprints import SqlSprintRecords

CREATED = datetime(1970, 1, 1, 2, 0, 1, 900000, tzinfo=timezone(timedelta(hours=2)))
UPDATED = datetime(1970, 1, 1, 0, 0, 4, 800000, tzinfo=UTC)
KEYS = ["id", "reference", "title", "description", "column_id", "position", "swimlane_id",
        "date_creation", "date_modification", "is_active"]


class SqlRowsTests(unittest.TestCase):
    def setUp(self) -> None:
        # Construction would open a connection. These methods need only the supplied values.
        self.client = object.__new__(SqlCardClient)
        self.client._lane_id = mock.Mock(return_value=7)
        self.client._query = mock.Mock()
        self.client._commit_unless_nested = mock.Mock()
        self.client._staged = mock.Mock(return_value={})
        self.records = ProductIssueRecords(self.client)
        self.sprints = SqlSprintRecords(self.client)

    def check_row(self, actual, identity, reference, title, description, column, lane,
                  updated="4", active=1, position=0, moved=None) -> None:
        expected = dict(zip(KEYS, [identity, reference, title, description, column, position,
                                   lane, "1", updated, active], strict=True))
        order = KEYS.copy()
        if moved is not None:
            expected["date_moved"] = moved
            order.insert(-1, "date_moved")
        self.assertEqual(actual, expected)
        self.assertEqual(list(actual), order)

    def card_values(self, **changes):
        values = {"ref": "ummanu-1", "key": 1, "title": None, "description": None, "state": "ready",
                  "archived": False, "position": 3, "extensions": {"extra": {"swimlane": "lane"}},
                  "created": CREATED, "updated": UPDATED, "moved": None}
        values.update(changes)
        return tuple(values.values())

    def test_task_coercions_move_optionality_and_active_policy(self) -> None:
        for moved, expected in ((None, None), (UPDATED, "4")):
            with self.subTest(moved=moved):
                row = self.client._row(self.card_values(moved=moved, archived=True))
                self.check_row(row, 1, "ummanu-1", "", "", 2, 7, active=0,
                               position=3, moved=expected)
        self.assertEqual(self.client._lane_id.call_args_list, [mock.call("lane")] * 2)
        self.client._query.assert_not_called()

    def test_task_invalid_key_and_state_refuse_before_lane_lookup(self) -> None:
        for changes, message in (
            ({"key": "bad"}, "card ummanu-1 carries malformed transport key 'bad'"),
            ({"state": "alien"}, "card ummanu-1 is in state 'alien', which no Pipeline board column shows"),
        ):
            with self.subTest(changes=changes), self.assertRaises(SqlCardError) as error:
                self.client._row(self.card_values(**changes))
            self.assertEqual(str(error.exception), message)
        self.client._lane_id.assert_not_called()
        self.client._query.assert_not_called()

    def test_persisted_sprint_str_none_and_staged_raw_values(self) -> None:
        self.check_row(self.sprints._row((None, "9", None, CREATED, UPDATED)),
                       9, "None", "None", "", 1, 0)
        staged = {"reference": "sprint:9", "title": None, "description": None,
                  "created_at": CREATED}
        self.check_row(self.sprints._staged_row("9", staged), "9", "sprint:9", None, None,
                       1, 0, updated="1")
        del staged["description"]
        self.assertEqual(self.sprints._staged_row(9, staged)["description"], "")
        self.client._lane_id.assert_not_called()
        self.client._query.assert_not_called()

    def test_product_issue_and_staged_lookup_order_and_distinct_states(self) -> None:
        events = []

        def call(method, **params):
            events.append((method, params))
            return {"id": "1"} if method == "getProjectByName" else [{"id": "8", "title": "Issues"}]

        self.client.call = mock.Mock(side_effect=call)
        self.client._lane_id.side_effect = lambda lane: events.append(("lane", lane)) or 7
        for state in ("archived", "closed", "open"):
            product = self.records._product_row(("p", "8", None, None, state, CREATED, UPDATED))
            issue = self.records._issue_row(("i", "9", "p", None, None, state, CREATED, UPDATED))
            self.check_row(product, 8, "product:p", "", "", 8, 7, active=int(state != "archived"))
            self.check_row(issue, 9, "issue:i", "", "", 8, 7, active=int(state != "closed"))
        staged = {10: {"reference": "issue:staged", "title": None, "description": None,
                       "lane": "raw", "created_at": CREATED}}
        self.client._staged.return_value = staged
        self.check_row(self.records._staged_row(10), 10, "issue:staged", None, None, 8, 7, updated="1")
        self.assertEqual(events, [item for lane in ["p"] * 6 + ["raw"] for item in (
            ("getProjectByName", {"name": "Pipeline"}), ("getColumns", {"project_id": 1}), ("lane", lane))])

    def test_product_column_refusal_does_not_resolve_lane(self) -> None:
        for columns in ([], [{"id": 1, "title": "Issues"}] * 2):
            self.client.call = mock.Mock(side_effect=[{"id": 1}, columns])
            with self.assertRaisesRegex(SqlCardError, "exactly one Issues column"):
                self.records._product_row(("p", 8, "", "", "active", CREATED, UPDATED))
        self.client._lane_id.assert_not_called()

    def test_comment_projections_keep_raw_values_group_order_and_empty_keys(self) -> None:
        body, identifier = object(), object()
        expected = [{"id": identifier, "date_creation": "1", "comment": body},
                    {"id": "2", "date_creation": "1", "comment": "second"}]
        self.client._query.side_effect = [[(1, "ummanu-1"), (2, "ummanu-2")],
                                         [("ummanu-1", identifier, body, CREATED),
                                          ("ummanu-1", "2", "second", CREATED)]]
        card = self.client._card_comments([1, 2])
        self.sprints._references = mock.Mock(return_value={1: ("sprint:1",), 2: ("sprint:2",)})
        self.client._query.side_effect = [[("sprint:1", identifier, body, CREATED),
                                          ("sprint:1", "2", "second", CREATED)]]
        sprint = self.sprints.comments_of([1, 2])
        self.records._by_kind = mock.Mock(return_value=([3], {"product": [1, 2]}))
        self.records._stored = mock.Mock(return_value={1: ("p",), 2: ("q",)})
        self.client._query.side_effect = [[("p", identifier, body, CREATED), ("p", "2", "second", CREATED)]]
        product = self.records.comments_of([1, 2, 3])
        for result in (card, sprint, product):
            self.assertEqual(result[1], expected)
            self.assertEqual(result[2], [])
            self.assertIs(result[1][0]["id"], identifier)
            self.assertIs(result[1][0]["comment"], body)
            self.assertEqual(list(result[1][0]), ["id", "date_creation", "comment"])
        self.assertEqual(product[3], [])

    def test_marker_edges_and_all_three_insertion_paths(self) -> None:
        self.client._ref_of = mock.Mock(return_value="ummanu-1")
        self.sprints._reference = mock.Mock(return_value="sprint:1")
        self.sprints._request = mock.Mock(return_value=("request", {"actor": {"role": "po", "id": "owner"}}))
        self.records.identifier_for = mock.Mock(return_value="p")
        key = record_key("product", "p")
        for content, marker in (("", None), ("[]", ""), ("[po]\r\nbody", "po"),
                                (" [po]", None), ("[po] ", None), ("[[x]]\n[y]", "[x]"),
                                ("\n[po]", None), ("[ a ]", " a "), ("no marker", None)):
            with self.subTest(content=content):
                self.assertEqual(sql_rows.comment_marker(content), marker)
                for module, create in (
                    (sql_cards, lambda content=content: self.client._rpc_createComment(task_id=1, content=content)),
                    (sql_sprints, lambda content=content: self.sprints.create_comment(1, content)),
                    (sql_product_issues, lambda content=content: self.records.create_comment(key, content)),
                ):
                    self.client._query.side_effect = None
                    self.client._query.return_value = [(11,)]
                    with mock.patch.object(module, "_now", return_value=CREATED) as clock:
                        self.assertEqual(create(), 11)
                    params = self.client._query.call_args.args[1]
                    self.assertEqual(params[:3], ("sprint:1" if module is sql_sprints else
                                                 "p" if module is sql_product_issues else "ummanu-1",
                                                 marker, content))
                    self.assertEqual(params[-1], CREATED)
                    clock.assert_called_once_with()
                    if module is sql_sprints:
                        self.assertEqual(params[3:6], ("po", "owner", "request"))

    def test_task_supplied_comment_time_refuses_before_insert(self) -> None:
        self.client._ref_of = mock.Mock(return_value="ummanu-1")
        with (mock.patch.object(sql_cards, "_now", return_value=CREATED),
              self.assertRaisesRegex(SqlCardError, "which is not a time")):
            self.client._rpc_createComment(task_id=1, content="[]", created_at="invalid")
        self.client._query.assert_not_called()

    def test_document_identity_fallback_scalars_and_visible_errors(self) -> None:
        class Document(dict):
            pass

        for decode in (sql_rows.json_document, sql_audit.SqlTaskAudit._document):
            for value in ({"extra": {"unknown": "keep"}}, Document(nested=[])):
                self.assertIs(decode(value), value)
            for value in (None, "", False, 0, [], b""):
                self.assertEqual(decode(value), {})
            for text, expected in (("null", None), ("[]", []), ("1", 1), ('"text"', "text"),
                                   ('{"extra":{"spelling":"kept"}}', {"extra": {"spelling": "kept"}})):
                self.assertEqual(decode(text), expected)
            with self.assertRaises(json.JSONDecodeError):
                decode("invalid")
            with self.assertRaises(TypeError):
                decode([1])
        for value, error in (("null", AttributeError), ("[]", AttributeError), ("invalid", json.JSONDecodeError)):
            with self.assertRaises(error):
                self.client._row(self.card_values(extensions=value))

    def test_strict_sprint_request_and_timestamp_policies_remain_separate(self) -> None:
        document = {"actor": {}}
        self.client._query.return_value = [("request", document)]
        self.assertIs(self.sprints._request("sprint:1")[1], document)
        for value, error in (("", json.JSONDecodeError), (None, TypeError)):
            self.client._query.return_value = [("request", value)]
            with self.assertRaises(error):
                self.sprints._request("sprint:1")
        self.assertEqual(sql_cards._rfc3339(CREATED), "1970-01-01T00:00:01Z")
        self.assertEqual(sql_sprints._rfc3339(CREATED), "1970-01-01T00:00:01.900000Z")
        self.assertEqual(sql_rows.epoch(None), "")
        self.assertEqual(sql_rows.epoch(datetime(1969, 12, 31, 23, 59, 59, 100000, tzinfo=UTC)), "0")

    def test_extensions_decode_at_each_metadata_path_without_changing_spelling(self) -> None:
        bag = {"extra": {"unknown": {"nested": 1}, "empty": None, "record_type": "shadow"}}
        for stored_bag in (bag, json.dumps(bag)):
            values = [None] * 22
            values[0:3] = ["ummanu-1", "ummanu", "code"]
            values[19] = stored_bag
            self.client._query.side_effect = [[(1, *values)], [], [], [], []]
            meta = self.client._card_metadata([1])[1]
            self.assertEqual(meta["unknown"], "{'nested': 1}")
            self.assertEqual(meta["empty"], "")
            self.assertEqual(meta["record_type"], "task")
            self.records._by_kind = mock.Mock(return_value=([], {"product": [8], "issue": [9]}))
            self.records._stored = mock.Mock(side_effect=[{8: ("p", stored_bag)},
                                                         {9: ("i", "p", "bug", "P2", None, stored_bag)}])
            self.client._query.side_effect = [[]]
            metadata = self.records.metadata_of([8, 9])
            for key, kind in ((8, "product"), (9, "issue")):
                self.assertEqual(metadata[key]["unknown"], "{'nested': 1}")
                self.assertEqual(metadata[key]["empty"], "")
                # Issue's known keys differ from Product's, including record_type in both.
                self.assertEqual(metadata[key]["record_type"], kind)

    def test_shared_leaf_ownership_and_adapter_delegation(self) -> None:
        leaf = ast.parse(Path(sql_rows.__file__).read_text())
        for node in ast.walk(leaf):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module]
                self.assertTrue(all(name.split(".")[0] in sys.stdlib_module_names for name in names))
        projections = {"board_row", "comment_row", "comment_marker", "json_document"}
        for function in leaf.body:
            if isinstance(function, ast.FunctionDef) and function.name in projections:
                for call in (node for node in ast.walk(function) if isinstance(node, ast.Call)):
                    self.assertIn(ast.unparse(call.func), {"epoch", "isinstance", "json.loads",
                                                         "content.splitlines", "first.startswith", "first.endswith"})
        targets = {
            sql_cards: {"_row": "board_row", "_card_comments": "comment_row", "_rpc_createComment": "comment_marker",
                        "_card_metadata": "json_document"},
            sql_sprints: {"_row": "board_row", "_staged_row": "board_row", "comments_of": "comment_row",
                          "create_comment": "comment_marker"},
            sql_product_issues: {"_product_row": "board_row", "_issue_row": "board_row", "_staged_row": "board_row",
                                 "comments_of": "comment_row", "create_comment": "comment_marker", "metadata_of": "json_document"},
            sql_audit: {"_document": "json_document"},
        }
        for module, owners in targets.items():
            tree = ast.parse(Path(module.__file__).read_text())
            functions = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
            for owner, helper in owners.items():
                with self.subTest(module=module.__name__, owner=owner):
                    function = functions[owner]
                    self.assertTrue(any(isinstance(node, ast.Call) and ast.unparse(node.func) == helper
                                        for node in ast.walk(function)))
            for node in ast.walk(tree):
                if isinstance(node, ast.Dict):
                    keys = {key.value for key in node.keys if isinstance(key, ast.Constant)}
                    self.assertFalse({"reference", "column_id", "date_creation"} <= keys)
                    self.assertFalse({"id", "date_creation", "comment"} <= keys)
