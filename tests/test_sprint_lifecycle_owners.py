"""Sprint ownership, inherited patch lookup and shared SQL transaction boundaries."""

from __future__ import annotations

import ast
import contextlib
import inspect
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.support import sprint_lifecycle_owners as owners
from ummanu import sprints, tasks
from ummanu.board.sprint_protocol import lifecycle, reservations, shared
from ummanu.kernel.errors import TaskError

ROOT = Path(__file__).resolve().parents[1]


class SprintLifecycleOwnersTests(unittest.TestCase):
    def test_released_close_and_reopen_result_bytes_and_key_order(self):
        # Expected fields come from the released result contract, not a separately executed
        # before-run. PostgreSQL request replay and once-only effects remain CI probes.
        writer = object.__new__(sprints.SprintWriter)
        writer.data_dir = Path("unused-golden-path")
        writer.reader = mock.Mock()
        writer.reader.show.return_value = {"ref": "sprint:1", "status": "closed"}
        event = {"ref": "sprint:1", "event_id": "evt1", "payload": {
            "decisions": {"issues": [], "cards": []}, "archived_tasks": ["ummanu-1"],
            "remaining_tasks": ["ummanu-2"], "reason": "finished",
        }}
        with mock.patch.object(lifecycle, "CleanupJournal") as cleanup:
            cleanup.return_value.summary.return_value = {"pending": 0}
            closed = writer._close_result(event)
        self.assertEqual(list(closed), ["action", "sprint", "event_id", "archived_tasks", "remaining_tasks",
            "issue_decisions", "closed_issues", "card_dispositions", "disposed_tasks", "reason", "closeout",
            "definition_of_done", "cleanup"])
        self.assertEqual(json.dumps({key: value for key, value in closed.items() if key != "definition_of_done"}),
            '{"action": "closed", "sprint": {"ref": "sprint:1", "status": "closed"}, "event_id": "evt1", '
            '"archived_tasks": ["ummanu-1"], "remaining_tasks": ["ummanu-2"], "issue_decisions": [], '
            '"closed_issues": [], "card_dispositions": [], "disposed_tasks": [], "reason": "finished", '
            '"closeout": null, "cleanup": {"pending": 0}}')
        self.assertIs(closed["definition_of_done"]["satisfied"], False)
        writer.reader.show.return_value = {"ref": "sprint:1", "status": "open"}
        reopened = writer._committed_result(shared.SPRINT_REOPENED, event)
        self.assertEqual(json.dumps(reopened),
            '{"action": "reopened", "sprint": {"ref": "sprint:1", "status": "open"}, "event_id": "evt1"}')

    def test_current_owners_have_one_implementation_and_fit_the_limit(self):
        sources = owners.sources(ROOT)
        inventory = owners.inventory(sources)
        self.assertEqual(owners.file_limits(sources), [])
        self.assertEqual(inventory["SprintLifecycleMixin"][0], "board/sprint_protocol/lifecycle")
        self.assertEqual(inventory["_sql_atomic"][0], "board/sprint_protocol/shared")
        self.assertEqual(inventory["sprint_admission_lock"][0], "board/sprint_protocol/reservations")
        for owner in owners.OWNERS[1:]:
            self.assertEqual(owners.facade_dependencies(owner, sources[owner]), set())
        methods = [node for node in inventory["SprintLifecycleMixin"][1].body if isinstance(node, ast.FunctionDef)]
        self.assertEqual(len(methods), 29)
        self.assertEqual(len([n for n in inventory["SprintWriter"][1].body if isinstance(n, ast.FunctionDef)]) + len(methods), 96)
        self.assertEqual(len([n for n in inventory["SprintReader"][1].body if isinstance(n, ast.FunctionDef)]), 13)
        self.assertEqual(len([n for n in inventory["_AuditOnce"][1].body if isinstance(n, ast.FunctionDef)]), 2)
        self.assertFalse(any(isinstance(n, ast.Name) and n.id in {"SprintWriter", "SprintReader", "super"}
                             for method in methods for n in ast.walk(method)))
        self.assertFalse(any(isinstance(n, ast.Attribute) and n.attr.startswith("__") and not n.attr.endswith("__")
                             for method in methods for n in ast.walk(method)))

    def test_guards_reject_duplicate_methods_oversize_and_reverse_import_variants(self):
        with self.assertRaisesRegex(AssertionError, "duplicate sprint implementation"):
            owners.inventory({"a": "def close(): pass", "b": "def close(): pass"})
        with self.assertRaisesRegex(AssertionError, "duplicate writer method"):
            owners.inventory({"a": "class SprintWriter:\n def close(self): pass", "b": "class SprintLifecycleMixin:\n def close(self): pass"})
        self.assertEqual(owners.file_limits({"released-size": "\n" * 3619, "boundary": "\n" * 3000}), ["released-size"])
        for source in (
            "def f():\n from ummanu.sprints import SprintWriter\n",
            "from ... import sprints as facade\nx = facade.SprintWriter\n",
            "import ummanu.sprints as facade\nx = facade.SprintWriter\n",
            "from importlib import import_module as load\nx = load('...sprints', 'ummanu.board.sprint_protocol')\n",
            "PATCH = 'ummanu.sprints.TaskWriter'\n",
        ):
            with self.subTest(source=source):
                self.assertTrue(owners.facade_dependencies("board/sprint_protocol/lifecycle", source))

    def test_public_identity_mro_signatures_and_self_patches(self):
        self.assertEqual(sprints.SprintWriter.__mro__, (sprints.SprintWriter, lifecycle.SprintLifecycleMixin, object))
        self.assertIs(lifecycle.TaskReader, tasks.TaskReader)
        self.assertIs(lifecycle.TaskWriter, tasks.TaskWriter)
        self.assertIs(sprints.TaskReader, tasks.TaskReader)
        self.assertIs(sprints.update_active_sprint_projects, reservations.update_active_sprint_projects)
        self.assertIs(lifecycle.sprint_admission_lock, reservations.sprint_admission_lock)
        self.assertIs(sprints.SPRINT_CLOSED, shared.SPRINT_CLOSED)
        self.assertIs(sprints.SPRINT_CLOSEOUT, shared.SPRINT_CLOSEOUT)
        self.assertEqual(list(inspect.signature(sprints.SprintWriter.close).parameters),
                         ["self", "role", "actor", "reference", "decisions", "request_id", "reason", "closeout"])
        writer = object.__new__(sprints.SprintWriter)
        writer.client = mock.Mock()
        writer.data_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with (mock.patch.object(sprints.SprintWriter, "_role"),
              mock.patch.object(writer, "_guard_observer_identity"),
              mock.patch.object(sprints.SprintWriter, "_close_atomic", return_value={"event_id": "evt1"}) as step,
              mock.patch.object(lifecycle.owner_events, "record") as event):
            self.assertEqual(writer.close(role="po", actor="po", reference="sprint:1", request_id="r"), {"event_id": "evt1"})
            step.assert_called_once_with(role="po", actor="po", reference="sprint:1", decisions=None, request_id="r", reason="", closeout="")
            self.assertEqual(event.call_args.args[3], "sprint_closed:sprint:1:evt1")
        self.assertEqual(shared._close_step_request_id("r", "issue", "issue:1"),
                         "r:sprint-close-issue:740a35d7c6631c13a85845da9a134397c3878f38118bed1c5bf5d3cf5a285668")


class SprintSqlBoundaryTests(unittest.TestCase):
    def writer(self, depth=0):
        order = []

        @contextlib.contextmanager
        def transaction():
            order.append("begin")
            try:
                yield
            except Exception:
                order.append("rollback")
                raise
            else:
                order.append("commit")

        writer = SimpleNamespace(client=SimpleNamespace(_depth=depth, transaction=transaction,
            _execute=lambda statement, params: order.append((statement, params))),
            transactions=SimpleNamespace(drop=lambda request: order.append(("drop", request))))
        return writer, order

    def test_complete_transaction_lock_and_nested_fastpath_preserve_annotations(self):
        for depth in (0, 1):
            writer, order = self.writer(depth)

            def operation(self, *, request_id: str, order=order) -> dict[str, object]:
                order.append("operation")
                return {"request_id": request_id}

            wrapped = shared._sql_atomic(operation)
            self.assertEqual(wrapped(writer, request_id="r"), {"request_id": "r"})
            self.assertEqual(order, ["operation"] if depth else ["begin", ("SELECT pg_advisory_xact_lock(%s)", (1600,)), "operation", "commit"])
            self.assertEqual(inspect.signature(wrapped), inspect.signature(operation))
            self.assertEqual(wrapped.__annotations__, operation.__annotations__)

    def test_rollback_precedes_drop_and_error_mapping(self):
        for failure, expected in (
            (TaskError("audit_pending", "pending", 4), ("backend_error", "PostgreSQL Sprint transaction rolled back", 1)),
            (TaskError("validation", "invalid", 2), ("validation", "invalid", 2)),
            (ValueError("bad step"), ("backend_error", "PostgreSQL Sprint transaction rolled back: bad step", 1)),
        ):
            writer, order = self.writer()

            def operation(self, *, request_id, failure=failure):
                raise failure

            with self.assertRaises(TaskError) as raised:
                shared._sql_atomic(operation)(writer, request_id="r")
            self.assertEqual((raised.exception.code, raised.exception.message, raised.exception.exit_code), expected)
            self.assertEqual(order[-2:], ["rollback", ("drop", "r")])
            if isinstance(failure, TaskError) and failure.code == "validation":
                self.assertIs(raised.exception, failure)
