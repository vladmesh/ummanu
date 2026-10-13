from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support.fakes.sprint_reader import ReadOnlySprintClient
from ummanu.board.models import SprintState
from ummanu.board.sprint_read import (
    BUDGET_EVENT_TYPES,
    SprintReadMetadata,
    SprintResume,
)
from ummanu.kernel.errors import TaskError
from ummanu.sprints import SPRINT_BOARD_NAME, SprintReader


class SprintReadOnlyTests(unittest.TestCase):
    def test_defaults_and_explicit_false_keep_missing_board_reads_empty(self) -> None:
        self.assertIs(inspect.signature(SprintReader.list).parameters["create"].default, False)
        for board in (None, False, {}, {"id": "invalid"}, []):
            with self.subTest(board=board):
                client = ReadOnlySprintClient(board=board)
                self.assertEqual(SprintReader(client).list(create=False), [])
                with self.assertRaises(TaskError) as raised:
                    SprintReader(client).show("sprint:missing")
                self.assertEqual((raised.exception.code, raised.exception.exit_code), ("not_found", 2))
                self.assertEqual(client.writes, [])
                self.assertEqual(len(client.calls), 2)

    def test_default_list_never_initializes_a_missing_board(self) -> None:
        client = ReadOnlySprintClient()
        reader = SprintReader(client)
        self.assertEqual(client.calls, [])
        self.assertEqual(reader.list(), [])
        self.assertEqual(client.calls, [("getProjectByName", {"name": SPRINT_BOARD_NAME})])
        self.assertEqual(client.writes, [])

    def test_lookup_failure_propagates_without_initialization_or_empty_success(self) -> None:
        for operation in (lambda r: r.list(), lambda r: r.show("sprint:1")):
            client = ReadOnlySprintClient()
            failure = TaskError("backend_error", "lookup failed", 1)
            with mock.patch.object(client, "call", side_effect=failure), self.assertRaises(TaskError) as raised:
                operation(SprintReader(client))
            self.assertIs(raised.exception, failure)
            self.assertEqual(client.writes, [])

    def test_existing_empty_board_and_missing_reference_keep_released_errors(self) -> None:
        client = ReadOnlySprintClient(board={"id": 2})
        reader = SprintReader(client)
        self.assertEqual(reader.list(), [])
        with self.assertRaises(TaskError) as raised:
            reader.show("sprint:missing")
        self.assertEqual((raised.exception.code, raised.exception.exit_code), ("not_found", 2))
        self.assertEqual([name for name, _ in client.calls],
                         ["getProjectByName", "getAllTasks", "getProjectByName", "getTaskByReference"])
        self.assertEqual(client.batches, [[]])
        self.assertEqual(client.writes, [])

    def test_explicit_initialization_still_calls_create_once(self) -> None:
        client = ReadOnlySprintClient()
        original = client.call

        def initialize(method, **params):
            if method == "createProject":
                client.calls.append((method, params))
                client.board = {"id": 2}
                return 2
            return original(method, **params)

        with mock.patch.object(client, "call", side_effect=initialize):
            reader = SprintReader(client)
            self.assertEqual(reader.list(create=True), [])
            self.assertEqual(reader.list(create=True), [])
        self.assertEqual([name for name, _ in client.calls].count("createProject"), 1)
        self.assertEqual(client.calls[1], ("createProject", {"name": SPRINT_BOARD_NAME}))

    def test_existing_rows_keep_order_batch_metadata_comments_and_default_flags(self) -> None:
        client = ReadOnlySprintClient(board={"id": 2}, rows=[
            {"id": 4, "reference": "sprint:4"}, {"id": 3, "reference": "sprint:3"},
        ])
        client.metadata = {4: {"sprint_status": "closed"}, 3: {"sprint_goal": "read goal"}}
        client.comments = [{"date_creation": 0, "comment": "read comment"}]
        with tempfile.TemporaryDirectory() as directory:
            guard = Path(directory) / "sprints/active-repositories.json"
            guard.parent.mkdir()
            guard.write_bytes(b'{"version":2,"projects":{}}\n')
            before = {str(p.relative_to(directory)): p.read_bytes() for p in Path(directory).rglob("*") if p.is_file()}
            reader = SprintReader(client, data_dir=directory)
            listed = reader.list()
            self.assertEqual([s["ref"] for s in listed], ["sprint:4", "sprint:3"])
            self.assertEqual(client.batches, [[("getTaskMetadata", {"task_id": 4}),
                                              ("getTaskMetadata", {"task_id": 3})]])
            self.assertNotIn("getAllComments", [name for name, _ in client.calls])
            self.assertNotIn("resume_freshness", listed[0])
            with mock.patch("ummanu.sprints.TaskReader") as task_reader:
                task_reader.return_value.list.return_value = [{"ref": "ummanu-1", "sprint": "sprint:3"}]
                shown = reader.show("sprint:3")
                task_reader.assert_called_once_with(client)
                task_reader.return_value.list.assert_called_once_with(sprint="sprint:3")
            self.assertEqual(shown["cards"], [{"ref": "ummanu-1", "sprint": "sprint:3"}])
            # Released show includes fetched comments when cards are requested, even though
            # include_comments defaults to False. This cut preserves that output.
            self.assertEqual(shown["comments"], [{"created_at": None, "body": "read comment"}])
            self.assertIn("resume_freshness", shown)
            self.assertEqual(list(shown), list(listed[1])[:-1] + ["comments", "resume", "cards", "resume_freshness"])
            detail = reader.show("sprint:3", include_cards=False, include_comments=True, include_resume_freshness=False)
            self.assertEqual(detail["comments"], [{"created_at": None, "body": "read comment"}])
            self.assertNotIn("cards", detail)
            self.assertNotIn("resume_freshness", detail)
            after = {str(p.relative_to(directory)): p.read_bytes() for p in Path(directory).rglob("*") if p.is_file()}
            self.assertEqual(after, before)
        self.assertEqual(client.writes, [])

    def test_malformed_rows_metadata_and_comments_keep_error_classification(self) -> None:
        for method, value, operation, code in (
            ("getAllTasks", {}, lambda r: r.list(), None),  # Released falsy response is an empty list.
            ("getAllTasks", "invalid", lambda r: r.list(), "backend_error"),
            ("getTaskByReference", "invalid", lambda r: r.show("sprint:3"), "not_found"),
            ("getTaskMetadata", [], lambda r: r.show("sprint:3", include_cards=False), "backend_error"),
            ("getAllComments", ["invalid"], lambda r: r.show("sprint:3", include_comments=True), "backend_error"),
        ):
            with self.subTest(method=method, value=value):
                client = ReadOnlySprintClient(board={"id": 2}, rows=[{"id": 3, "reference": "sprint:3"}])
                original = client.call
                with mock.patch.object(client, "call", side_effect=lambda verb, value=value, method=method, original=original, **params: value if verb == method else original(verb, **params)):
                    if code is None:
                        self.assertEqual(operation(SprintReader(client)), [])
                    else:
                        with self.assertRaises(TaskError) as raised:
                            operation(SprintReader(client))
                        self.assertEqual(raised.exception.code, code)
                self.assertEqual(client.writes, [])

    def test_freshness_uses_only_the_supplied_sprint_and_linked_card_audit_slice(self) -> None:
        client = ReadOnlySprintClient(board={"id": 2}, rows=[{"id": 3, "reference": "sprint:3"}])
        client.metadata[3] = {"sprint_resume": json.dumps({
            "selected_step": "continue", "selected_why": "safe", "rejected_alternatives": "none",
            "current_task": "ummanu-1", "dod_state": "pending", "next_safe_step": "review",
            "recorded_at": "2026-10-01T00:00:00Z",
        })}
        audit = mock.Mock()
        audit.events.return_value = [
            {"ref": "sprint:3", "kind": "commented", "outcome": "success", "actor": {"role": "po"},
             "occurred_at": "2026-10-01T00:06:00Z"},
            {"ref": "sprint:other", "kind": "commented", "outcome": "success", "actor": {"role": "po"},
             "occurred_at": "2026-10-01T01:00:00Z"},
        ]
        reader = SprintReader(client, data_dir="unused-read-only-path")
        with (mock.patch("ummanu.sprints.TaskReader") as task_reader,
              mock.patch.object(reader.audit, "events", side_effect=AssertionError("unrelated audit source"))):
            task_reader.return_value.list.return_value = [{"ref": "ummanu-1", "sprint": "sprint:3"}]
            shown = reader.show("sprint:3", audit=audit)
        audit.events.assert_called_once_with({"sprint:3", "ummanu-1"})
        self.assertEqual(shown["resume_freshness"], {"fresh": False, "error": "resume_stale",
            "recorded_at": "2026-10-01T00:00:00Z", "last_event_at": "2026-10-01T00:06:00Z",
            "lag_seconds": 360, "threshold_seconds": 300})
        self.assertEqual(client.writes, [])

    def test_default_show_never_initializes_a_missing_board(self) -> None:
        client = ReadOnlySprintClient()
        reader = SprintReader(client)
        with self.assertRaises(TaskError) as refused:
            reader.show("sprint:1")
        self.assertEqual((refused.exception.code, refused.exception.message, refused.exception.exit_code),
                         ("not_found", "sprint was not found", 2))
        self.assertEqual(client.calls, [("getProjectByName", {"name": SPRINT_BOARD_NAME})])
        self.assertEqual(client.writes, [])


class SprintReadModelTests(unittest.TestCase):
    def test_compound_metadata_is_typed_once_and_renders_the_released_shape(self) -> None:
        resume = {
            "selected_step": " next ",
            "selected_why": " because ",
            "rejected_alternatives": " other ",
            "current_task": " task:1 ",
            "dod_state": " pending ",
            "next_safe_step": " continue ",
            "recorded_at": "2026-09-16T12:00:00Z",
        }
        meta = {
            "sprint_repositories": json.dumps([" /repo/a ", "/repo/a", "/repo/b", ""]),
            "sprint_status": "closed",
            "sprint_budget": json.dumps({"by_type": {"red_review": "2", "blocked": -4, "hotfix": "bad"}}),
            "sprint_budget_uncharged": json.dumps({"infrastructure_blocked": "3"}),
            "sprint_resume": json.dumps(resume),
            "sprint_source_audit": json.dumps(
                {
                    "created_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-01-02T00:00:00Z",
                    "board": "old",
                    "ignored": "not part of the contract",
                }
            ),
        }

        read = SprintReadMetadata.from_legacy(meta, thresholds={"signal": 2, "hard": 5})

        self.assertEqual(read.repositories, ("/repo/a", "/repo/b"))
        self.assertIs(read.state, SprintState.CLOSED)
        self.assertEqual(read.budget.total, 2)
        self.assertEqual(
            read.budget.by_type,
            {event: (2 if event == "red_review" else 0) for event in BUDGET_EVENT_TYPES},
        )
        self.assertEqual(read.budget.uncharged, {"infrastructure_blocked": 3})
        self.assertTrue(read.budget.signal_reached)
        self.assertFalse(read.budget.hard_reached)
        self.assertEqual(
            read.resume.to_document() if read.resume else None,
            {
                **{key: value.strip() for key, value in resume.items() if key != "recorded_at"},
                "recorded_at": resume["recorded_at"],
            },
        )
        self.assertEqual(
            read.source_audit.to_document() if read.source_audit else None,
            {
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-02T00:00:00Z",
                "board": "old",
            },
        )

    def test_invalid_legacy_values_keep_the_old_safe_defaults(self) -> None:
        read = SprintReadMetadata.from_legacy(
            {
                "sprint_repositories": "not-json",
                "sprint_status": "future-state",
                "sprint_budget": "{",
                "sprint_budget_uncharged": "{",
                "sprint_resume": json.dumps({"selected_step": "only one field"}),
                "sprint_source_audit": json.dumps({"other": "ignored"}),
            }
        )

        self.assertEqual(read.repositories, ())
        self.assertIs(read.state, SprintState.OPEN)
        self.assertEqual(read.budget.total, 0)
        self.assertEqual(read.budget.thresholds, {"signal": 3, "hard": 6})
        self.assertFalse(read.budget.signal_reached)
        self.assertFalse(read.budget.hard_reached)
        self.assertIsNone(read.resume)
        self.assertIsNone(read.source_audit)

    def test_empty_budget_threshold_override_keeps_legacy_defaults(self) -> None:
        read = SprintReadMetadata.from_legacy({}, thresholds={})

        self.assertEqual(read.budget.thresholds, {"signal": 3, "hard": 6})
        self.assertFalse(read.budget.signal_reached)
        self.assertFalse(read.budget.hard_reached)

    def test_required_resume_reports_the_same_missing_field_contract(self) -> None:
        with self.assertRaisesRegex(ValueError, "resume entry is missing required fields"):
            SprintResume.from_legacy({"selected_step": "x"}, required=True)
        with self.assertRaisesRegex(ValueError, "resume entry must be a JSON object"):
            SprintResume.from_legacy("not-json", required=True)


if __name__ == "__main__":
    unittest.main()
