"""secretary-1412: what the head operations share, without a session manager.

The Orca pane versions of `spawn` / `nudge` / `stop` and the fake session host their contract ran
on were removed in secretary-1725; the verbs are now `HeadRuntime`'s, and their contract suite runs
against the local-pty backend in `test_local_pty_head_runtime`. What is pinned here is what every
backend shares:

  * a delivery callback cannot hand back a run that is not the launched one (`post_delivery_run`);
  * a stop records who initiated it, and that initiator survives the record being written and read
    back — which is what "the dispatcher restarted" looks like from inside a run;
  * a head can be pointed at a card, a sprint entity or a role's standing instruction, because the
    observer and the mechanical roles have no card and one must not be invented for them;
  * the package reaches no session manager and spawns no process — asserted by reading its own
    source. That is a source check and it proves only what a source check can.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from tests import source_trees
from ummanu.runtime.head import (
    EXITED,
    HeadNudgeFailed,
    HeadRun,
    HeadSpec,
    StopInitiator,
    TaskRef,
    operations as head_operations,
    post_delivery_run,
)

HEAD_PACKAGE = Path(head_operations.__file__).parent


CODEX = HeadSpec(profile_id="codex-worker", adapter="codex", effort="high", codex_mode="tui")
WORKSPACE = "/tmp/does-not-need-to-exist/ummanu-1412"


class HeadOperationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.task = TaskRef.card("ummanu-1412", document=f"{WORKSPACE}/TASK.md")

    def test_post_delivery_run_refuses_a_foreign_identity(self) -> None:
        before = HeadRun("run-local", CODEX, WORKSPACE, self.task, role="worker")
        foreign = HeadRun("run-foreign", CODEX, WORKSPACE, self.task, role="worker")

        with self.assertRaisesRegex(HeadNudgeFailed, "does not match"):
            post_delivery_run(before, foreign)

    def test_a_run_survives_being_written_down_and_read_back(self) -> None:
        run = (
            HeadRun("run-stopped", CODEX, WORKSPACE, self.task, role="worker")
            .finishing(StopInitiator(actor="watchdog", reason="idle"))
            .exited()
        )

        restored = HeadRun.from_json(run.to_json())

        self.assertEqual(restored, run)
        self.assertEqual(restored.stopped_by.actor, "watchdog")
        self.assertEqual(restored.lifecycle, EXITED)
        self.assertEqual(restored.spec.adapter, "codex")


class TaskPointerTests(unittest.TestCase):
    """A head is pointed at a durable document, and a card is only one kind of one."""

    def test_all_three_kinds_of_task_document_can_carry_a_head(self) -> None:
        for task_ref in (
            TaskRef.card("ummanu-1412", document=f"{WORKSPACE}/TASK.md"),
            TaskRef.sprint("sprint:848"),
            TaskRef.standing("observer", document="/var/lib/ummanu/observer.md"),
        ):
            with self.subTest(kind=task_ref.kind):
                run = HeadRun("run-pointed", CODEX, WORKSPACE, task_ref)
                self.assertEqual(run.task_ref, task_ref)
                self.assertEqual(HeadRun.from_json(run.to_json()).task_ref, task_ref)

    def test_a_sprint_head_needs_no_card_and_no_document_on_disk(self) -> None:
        run = HeadRun("run-sprint", CODEX, WORKSPACE, TaskRef.sprint("sprint:848"))

        self.assertEqual(run.task_ref.kind, "sprint")
        self.assertEqual(run.task_ref.document, "")

    def test_a_pointer_of_no_known_kind_is_refused(self) -> None:
        from ummanu.runtime.head import TaskRefError

        with self.assertRaises(TaskRefError):
            TaskRef(kind="whatever", ref="x")
        with self.assertRaises(TaskRefError):
            TaskRef.card("")



class BackendIndependenceTests(unittest.TestCase):
    """The property that makes the suite above possible, asserted rather than assumed."""

    def test_the_head_package_names_no_session_manager_and_spawns_no_process(self) -> None:
        """Prose may discuss Orca; code may not reach it. The check reads the syntax tree, so a
        docstring naming the session manager it is deliberately independent of does not pass for a
        command that runs it."""
        for source in sorted(HEAD_PACKAGE.glob("*.py")):
            tree = source_trees.parse(source.read_text(encoding="utf-8"))
            docstrings = {
                id(node.value)
                for node in ast.walk(tree)
                if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
            }
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = [alias.name for alias in node.names]
                    names.append(getattr(node, "module", "") or "")
                    for name in names:
                        self.assertNotIn("subprocess", name, f"{source.name} spawns processes of its own")
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if id(node) in docstrings:
                        continue
                    self.assertNotIn(
                        "orca",
                        node.value.lower(),
                        f"{source.name} reaches a session manager by name: {node.value!r}",
                    )
