"""DEC-01 ownership, exact source preservation and operative patch/binding contracts."""

from __future__ import annotations

import ast
import contextlib
import copy
import inspect
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests import config_dependencies
from tests.po_handover_fakes import MemoryAudit
from tests.support import task_protocol
from ummanu import tasks
from ummanu.board.task_protocol import dispatcher, recovery, rows, sprint_guard
from ummanu.kernel.errors import TaskError

ROOT = Path(__file__).resolve().parents[1]
CAPTURE = json.loads((ROOT / "docs/task-protocol-inventory.json").read_text())


class TaskProtocolSourceTests(unittest.TestCase):
    def test_each_task_file_meets_the_size_contract_and_the_old_file_did_not(self) -> None:
        self.assertEqual(CAPTURE["baseline_lines"], 5820)
        self.assertEqual(task_protocol.file_limits({"baseline": "\n" * CAPTURE["baseline_lines"]}),
                         ["baseline: 5820 > 3000"])
        sources = task_protocol.read_sources(ROOT, CAPTURE["files"])
        paths = {str(path.relative_to(ROOT)) for path in (ROOT / "src/ummanu/board/task_protocol").rglob("*.py")}
        self.assertEqual(paths | {"src/ummanu/tasks.py"}, set(sources))
        self.assertEqual(task_protocol.file_limits(sources), [])
        self.assertEqual(task_protocol.file_limits({"boundary": "\n" * 3000}), [])
        self.assertTrue(task_protocol.file_limits({"too_large": "\n" * 3001}))

    def test_all_methods_helpers_and_constants_have_one_owner_and_exact_original_ast(self) -> None:
        actual = task_protocol.inventory(task_protocol.read_sources(ROOT, CAPTURE["files"]))
        expected = CAPTURE["definitions"]
        self.assertEqual(set(actual), set(expected))
        for name, record in expected.items():
            with self.subTest(name=name):
                path, node = actual[name]
                self.assertEqual(path, record["owner"])
                self.assertEqual(task_protocol.digest(node), record["baseline_ast_sha256"])
        self.assertEqual(CAPTURE["ast_deltas"], [])

    def test_guard_refuses_a_second_implementation(self) -> None:
        with self.assertRaisesRegex(AssertionError, "duplicate task implementation: TaskWriter._write"):
            task_protocol.inventory({"core": "class TaskWriter:\n def _write(self): pass\n",
                                     "recovery": "class RecoveryMixin:\n def _write(self): pass\n"})
        with self.assertRaisesRegex(AssertionError, "duplicate task implementation: TaskWriter._write"):
            task_protocol.inventory({"core": "class TaskWriter:\n def _write(self): pass\n def _write(self): pass\n"})

    def test_lower_owners_never_import_the_public_facade(self) -> None:
        for path, source in task_protocol.read_sources(ROOT, CAPTURE["files"]).items():
            if path == "src/ummanu/tasks.py":
                continue
            name = path.removeprefix("src/").removesuffix(".py").replace("/", ".")
            if name.endswith(".__init__"):
                name = name.removesuffix(".__init__")
            targets = config_dependencies.dependencies(name, Path(path), source, strict_dynamic=False)
            self.assertNotIn("ummanu.tasks", targets, path)
        tree = ast.parse((ROOT / "src/ummanu/board/task_protocol/__init__.py").read_text())
        self.assertEqual(len(tree.body), 1)
        self.assertIsInstance(tree.body[0], ast.Expr)

    def test_reverse_import_guard_sees_nested_relative_alias_and_string_forms(self) -> None:
        for source in (
            "def f():\n from ...tasks import TaskWriter\n",
            "from ... import tasks as facade\nx = facade.TaskWriter\n",
            "import ummanu.tasks as facade\nx = facade.TaskWriter\n",
            "from importlib import import_module as load\nx = load('ummanu.tasks')\n",
            "PATCH = 'ummanu.tasks._task_number'\n",
        ):
            with self.subTest(source=source):
                targets = config_dependencies.dependencies(
                    "ummanu.board.task_protocol.rows", Path("src/ummanu/board/task_protocol/rows.py"),
                    source, strict_dynamic=False,
                )
                self.assertTrue(any(t == "ummanu.tasks" or t.startswith("ummanu.tasks.") for t in targets))


class TaskProtocolBindingTests(unittest.TestCase):
    def test_released_error_aliases_and_method_descriptors_have_one_identity(self) -> None:
        self.assertIs(tasks.TaskError, TaskError)
        for name in ("_CommittedWriteError", "ArtifactOwnershipTaskError", "SprintReservationUnverifiable"):
            self.assertIs(getattr(tasks, name), getattr(rows, name))
        for name in ("_transactional", "_mutation", "_check_retention_record", "_role"):
            self.assertIs(inspect.getattr_static(tasks.TaskWriter, name),
                          inspect.getattr_static(recovery.RecoveryMixin, name))
        self.assertIsInstance(inspect.getattr_static(tasks.TaskWriter, "_transactional"), property)
        self.assertIsInstance(inspect.getattr_static(tasks.TaskWriter, "_role"), staticmethod)
        self.assertTrue(hasattr(tasks.TaskWriter._mutation, "__wrapped__"))

    def test_dispatcher_uses_self_patches_and_the_actual_helper_owner(self) -> None:
        writer = object.__new__(tasks.TaskWriter)
        writer.reader = mock.Mock(show=mock.Mock(return_value={"type": "wait", "id": "fixture"}))
        writer.client = mock.Mock()
        with (mock.patch.object(writer, "_mutation", return_value=contextlib.nullcontext()) as mutation,
              mock.patch.object(dispatcher, "_task_number", return_value=73) as number):
            writer.record_wait_state(role="dispatcher", actor="dispatcher", reference="p-1", state="{\"v\":1}")
        mutation.assert_called_once_with()
        number.assert_called_once_with({"type": "wait", "id": "fixture"})
        writer.client.call.assert_called_once_with("saveTaskMetadata", task_id=73, values={"wait_state": '{"v":1}'})

    def test_sprint_unknown_session_refuses_before_any_effect_through_self(self) -> None:
        writer = object.__new__(tasks.TaskWriter)
        writer.client = mock.Mock()
        with (mock.patch.object(writer, "_po_session_state", return_value="") as read,
              self.assertRaises(TaskError) as raised):
            writer._refuse_unknown_po_sessions(["missing"])
        read.assert_called_once_with("missing")
        self.assertEqual((raised.exception.code, raised.exception.exit_code), ("validation", 2))
        self.assertEqual(writer.client.mock_calls, [])
        self.assertIs(tasks.TaskWriter._refuse_unknown_po_sessions, sprint_guard.SprintGuardMixin._refuse_unknown_po_sessions)

    def test_recovery_classifies_rollback_and_ambiguity_using_the_shared_property(self) -> None:
        writer = object.__new__(tasks.TaskWriter)
        writer.client = SimpleNamespace()
        ambiguous = writer._post_effect_refusal("fixture")
        self.assertEqual((ambiguous.code, ambiguous.exit_code), ("audit_pending", 4))
        writer.client.transaction = lambda: contextlib.nullcontext()
        rolled_back = writer._post_effect_refusal("fixture")
        self.assertEqual((rolled_back.code, rolled_back.exit_code), ("backend_error", 1))
        self.assertIn("nothing was written", rolled_back.message)


class TaskProtocolAuditTests(unittest.TestCase):
    """Representative legacy audit shape/order and faults over the extracted engine.

    Expected bytes are a source-derived baseline capture, not a baseline suite run.
    These in-memory cases supplement the retained PostgreSQL fault/recovery CI suites.
    """

    def setUp(self) -> None:
        self.writer = object.__new__(tasks.TaskWriter)
        self.card = {"id": 7, "ref": "p-1", "audit": {"updated_at": "fixed"}}
        self.writer.reader = mock.Mock(show=mock.Mock(side_effect=lambda ref: copy.deepcopy(self.card)))
        self.writer.audit = MemoryAudit()
        self.enterContext(mock.patch.object(self.writer, "_mutation", return_value=contextlib.nullcontext()))
        self.enterContext(mock.patch.object(recovery, "_now", return_value="2026-10-13T00:00:00Z"))
        self.enterContext(mock.patch.object(recovery.uuid, "uuid4", return_value=SimpleNamespace(hex="fixed")))

    def write(self, mutation, *, body="digest"):
        return self.writer._write("commented", "worker", "worker-1", "p-1", "r-1",
                                  {"body_sha256": body}, mutation, identity={"body_sha256": body})

    def test_event_bytes_key_order_and_same_request_replay_without_a_second_effect(self) -> None:
        effect = mock.Mock()
        self.assertFalse(self.write(effect)["replayed"])
        event = self.writer.audit.committed_event("r-1")
        self.assertEqual(json.dumps(event, separators=(",", ":")),
                         '{"event_id":"evt_fixed","schema_version":1,"occurred_at":"2026-10-13T00:00:00Z",'
                         '"actor":{"role":"worker","id":"worker-1"},"kind":"commented","outcome":"success",'
                         '"task_id":7,"ref":"p-1","backend":{"kind":"postgres","task_id":7,"revision":"updated_at:fixed"},'
                         '"request_id":"r-1","payload":{"body_sha256":"digest"}}')
        self.assertTrue(self.write(effect)["replayed"])
        effect.assert_called_once_with(self.card)
        self.writer.reader.show.reset_mock()
        with self.assertRaises(TaskError) as raised:
            self.write(effect, body="other")
        self.assertEqual((raised.exception.code, raised.exception.exit_code), ("validation", 2))
        self.writer.reader.show.assert_not_called()
        self.assertEqual(effect.call_count, 1)

    def test_stage_effect_read_stage_commit_order_and_discard_on_uncommitted_failure(self) -> None:
        calls = []
        audit = self.writer.audit
        for name in ("stage", "append", "discard"):
            original = getattr(audit, name)
            def recorded(*args, name=name, original=original):
                calls.append(name)
                return original(*args)
            self.enterContext(mock.patch.object(audit, name, side_effect=recorded))
        self.writer.reader.show.side_effect = lambda ref: (calls.append("read"), copy.deepcopy(self.card))[1]
        self.write(lambda card: calls.append("effect"))
        self.assertEqual(calls, ["read", "stage", "effect", "read", "stage", "append"])
        audit.committed.clear()
        calls.clear()
        with self.assertRaisesRegex(ValueError, "before effect"):
            self.write(mock.Mock(side_effect=ValueError("before effect")))
        self.assertEqual(calls, ["read", "stage", "discard"])
        self.assertEqual((audit.pending, audit.committed), ({}, {}))

    def test_committed_failure_retains_pending_record_with_no_commit_or_discard(self) -> None:
        with (mock.patch.object(self.writer.audit, "discard", wraps=self.writer.audit.discard) as discard,
              self.assertRaises(TaskError) as raised):
            self.write(mock.Mock(side_effect=rows._CommittedWriteError()))
        self.assertEqual((raised.exception.code, raised.exception.exit_code), ("audit_pending", 4))
        discard.assert_not_called()
        self.assertEqual(list(self.writer.audit.pending), ["r-1"])
        self.assertEqual(self.writer.audit.committed, {})
