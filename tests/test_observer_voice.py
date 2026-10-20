"""The observer speaks in its own name (secretary-1765).

Unit-level: the writers run over a mock board client, so what is proven here is who each write is
admitted as and that a refused one writes nothing. The PostgreSQL path of an observer's Issue, and
the audit record it leaves, is `tests/test_product_issues.py` in the integration-board suite.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from typing import Any, ClassVar
from unittest import mock

from tests.observer_identity import as_observer, unbound_observer
from ummanu import product_issue_commands, sprint_commands, task_commands
from ummanu.board import sprint_dod
from ummanu.board.roles import BOARD_ROLES, Role
from ummanu.cli import build_parser, main
from ummanu.product_issues import ProductIssueStore
from ummanu.runtime import role_env
from ummanu.sprints import SprintWriter
from ummanu.tasks import TaskError, TaskWriter, admit_role, is_significant_observer_event
from ummanu.web.statuses import status_for
from ummanu.webproto import sprint_ops
from ummanu.webproto.errors import IdentityRefused, ValidationRefused

SRC = Path(__file__).resolve().parents[1] / "src" / "ummanu"
SPRINT = "sprint:1465"
OTHER_SPRINT = "sprint:9"
# The client calls that change the board. A refused write makes none of them.
MUTATING = re.compile(r"^(create|update|save|close|move|remove|open|set|assign)")


def role_taking_verbs() -> list[tuple[list[str], argparse.ArgumentParser, tuple[str, ...]]]:
    """Every CLI verb that takes `--role`, found by walking the parser rather than listed by hand."""

    def walk(parser: argparse.ArgumentParser, path: list[str]):
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, sub in action.choices.items():
                    yield from walk(sub, [*path, name])
        role = next((a for a in parser._actions if "--role" in a.option_strings), None)
        if role is not None:
            yield path, parser, tuple(role.choices or ())

    return list(walk(build_parser(), []))


def required_arguments(parser: argparse.ArgumentParser, files: Path) -> list[str]:
    """Placeholder values for every required option of one verb, files included."""
    argv: list[str] = []
    grouped = {
        id(action)
        for group in parser._mutually_exclusive_groups
        if group.required
        for action in group._group_actions[1:]
    }
    for action in parser._actions:
        if not action.option_strings or action.dest in {"role", "actor", "help"} or id(action) in grouped:
            continue
        in_required_group = any(
            group.required and group._group_actions[0] is action for group in parser._mutually_exclusive_groups
        )
        if not (action.required or in_required_group):
            continue
        flag = action.option_strings[0]
        if action.choices:
            value = str(next(iter(action.choices)))
        elif action.type is int:
            value = "1"
        elif action.dest.endswith("_file"):
            path = files / f"{action.dest}.txt"
            # JSON, YAML and prose at once: every body a verb reads before its writer parses.
            path.write_text("{}\n", encoding="utf-8")
            value = str(path)
        else:
            value = "x"
        argv += [flag, value]
    return argv


class AdmitRoleTests(unittest.TestCase):
    def test_the_po_role_under_the_observer_actor_is_refused_before_membership(self) -> None:
        with self.assertRaises(TaskError) as raised:
            admit_role("po", "observer", {Role.PO})
        self.assertEqual(raised.exception.code, "role_masquerade")
        self.assertIn("--role observer", raised.exception.message)
        # Whitespace does not hide it.
        with self.assertRaises(TaskError) as raised:
            admit_role(Role.PO, " observer ", BOARD_ROLES)
        self.assertEqual(raised.exception.code, "role_masquerade")

    def test_every_other_pair_is_decided_by_membership_alone(self) -> None:
        self.assertIs(admit_role("po", "po", {Role.PO}), Role.PO)
        self.assertIs(admit_role("po", "vladmesh-ummanu", {"po"}), Role.PO)
        self.assertIs(admit_role("observer", "observer", {"observer"}), Role.OBSERVER)
        self.assertIs(admit_role("dispatcher", "observer", {Role.DISPATCHER}), Role.DISPATCHER)
        self.assertIs(admit_role("worker", "claude-opus-high", BOARD_ROLES), Role.WORKER)
        with self.assertRaises(TaskError) as raised:
            admit_role("observer", "observer", {"po"})
        self.assertEqual(raised.exception.code, "role_forbidden")

    def test_the_masquerade_is_decided_in_one_place(self) -> None:
        raises = [
            str(path.relative_to(SRC))
            for path in sorted(SRC.rglob("*.py"))
            for _match in re.finditer(r'TaskError\(\s*"role_masquerade"', path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(raises, ["board/task_protocol/rows.py"])


class IdentityRefusalTransportTests(unittest.TestCase):
    """The operation layer carries each identity refusal under the writer's code, and HTTP answers 403."""

    def test_each_writer_code_becomes_its_own_typed_refusal_and_status(self) -> None:
        layer = sprint_ops.SprintOperationLayer("/nonexistent")
        for code in ("role_masquerade", "observer_identity_unbound", "observer_sprint_mismatch"):
            with self.subTest(code=code):
                refused = layer._refusal(TaskError(code, "who is asking", 3), request_id="r")
                self.assertIsInstance(refused, IdentityRefused)
                self.assertEqual(refused.code, code)
                self.assertEqual(status_for(code), 403)
        # `role_forbidden` keeps the answer it had.
        self.assertIsInstance(layer._refusal(TaskError("role_forbidden", "no", 3), request_id="r"), ValidationRefused)


class CliMasqueradeTests(unittest.TestCase):
    """Each role-taking verb, with `--role po --actor observer`, stops in `admit_role`."""

    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.client = mock.MagicMock(name="board")
        self.client._depth = 0
        audit = mock.MagicMock(name="audit")
        audit.committed_event.return_value = None
        audit.pending_event.return_value = None
        audit.event.return_value = None
        self.audit = audit
        report = mock.MagicMock(instance={}, data_dir=self.root / "data")

        def board(*_args: Any, **_kwargs: Any) -> Any:
            return self.client  # one stand-in for every client

        for patcher in (
            mock.patch.object(sprint_commands, "board_client", board),
            mock.patch.object(task_commands, "card_client", board),
            mock.patch.object(product_issue_commands, "board_client", board),
            mock.patch.object(sprint_ops, "board_client", board),
            mock.patch.object(sprint_ops.SprintOperationLayer, "report", return_value=report),
            mock.patch("ummanu.tasks.task_audit_for", return_value=audit),
            mock.patch("ummanu.sprints.task_audit_for", return_value=audit),
            mock.patch("ummanu.product_issues.task_audit_for", return_value=audit),
            mock.patch.dict(os.environ, {"BOARD_ACTOR": ""}),
        ):
            self.enterContext(patcher)

    def run_cli(self, argv: list[str]) -> tuple[int, dict[str, Any]]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        text = (err.getvalue() or out.getvalue()).strip().splitlines()
        return code, json.loads(text[-1]) if text else {}

    def base(self, parser: argparse.ArgumentParser) -> list[str]:
        known = {option for action in parser._actions for option in action.option_strings}
        argv = []
        if "--instance" in known:
            argv += ["--instance", str(self.root / "instance")]
        if "--data-dir" in known:
            argv += ["--data-dir", str(self.root / "data")]
        return argv

    def mutations(self) -> list[str]:
        return [
            str(call.args[0])
            for call in self.client.call.call_args_list
            if call.args and MUTATING.match(str(call.args[0]))
        ]

    def test_every_verb_that_offers_the_po_role_refuses_the_masquerade_through_the_one_check(self) -> None:
        verbs = role_taking_verbs()
        names = {" ".join(path) for path, _parser, _choices in verbs}
        # The walk found the verbs the card names, so an empty walk cannot pass.
        for expected in ("sprint comment", "sprint close", "task move", "task create", "issue create", "issue close"):
            self.assertIn(expected, names)
        reached = []
        for path, parser, choices in verbs:
            if "po" not in choices:
                continue
            with self.subTest(verb=" ".join(path)):
                self.client.reset_mock()
                self.audit.reset_mock()
                argv = [
                    *path,
                    "--role",
                    "po",
                    "--actor",
                    "observer",
                    *self.base(parser),
                    *required_arguments(parser, self.root),
                ]
                code, answer = self.run_cli(argv)
                self.assertEqual(answer.get("error", {}).get("code"), "role_masquerade", (argv, answer))
                self.assertEqual(code, 3)
                self.assertIn("--role observer", answer["error"]["message"])
                self.assertEqual(self.mutations(), [])
                self.audit.stage.assert_not_called()
                self.audit.append.assert_not_called()
                reached.append(" ".join(path))
        self.assertGreaterEqual(len(reached), 20, reached)

    def test_a_verb_that_does_not_offer_the_po_role_cannot_carry_the_masquerade(self) -> None:
        for path, parser, choices in role_taking_verbs():
            if "po" in choices:
                continue
            with self.subTest(verb=" ".join(path)):
                argv = [*path, "--role", "po", "--actor", "observer", *required_arguments(parser, self.root)]
                code, answer = self.run_cli(argv)
                self.assertEqual((code, answer["error"]["code"]), (2, "usage"))

    def test_the_actor_named_by_the_environment_is_refused_the_same_way(self) -> None:
        with mock.patch.dict(os.environ, {"BOARD_ACTOR": "observer"}):
            path, sub, _choices = next(verb for verb in role_taking_verbs() if verb[0] == ["sprint", "comment"])
            code, answer = self.run_cli(
                [*path, "--role", "po", *self.base(sub), *required_arguments(sub, self.root)]
            )
        self.assertEqual((code, answer["error"]["code"]), (3, "role_masquerade"))

    def test_the_observer_and_the_other_roles_are_not_refused_as_a_masquerade(self) -> None:
        # The same verb as the observer reaches its own guard, not the masquerade.
        with unbound_observer():
            code, answer = self.run_cli(
                [
                    "sprint",
                    "comment",
                    "--role",
                    "observer",
                    "--actor",
                    "observer",
                    "--ref",
                    SPRINT,
                    "--data-dir",
                    str(self.root / "data"),
                    "--instance",
                    str(self.root / "instance"),
                    "--body-file",
                    str(self.write("note.md", "hello")),
                ]
            )
        self.assertEqual((code, answer["error"]["code"]), (3, "observer_identity_unbound"))

    def write(self, name: str, text: str) -> Path:
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path


class SprintWriterFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.client = mock.MagicMock(name="board")
        self.client._depth = 1  # inside a transaction already: `_sql_atomic` adds none
        self.writer = SprintWriter(self.client, data_dir=self.root / "data", instance=self.root)
        self.audit = mock.MagicMock(name="audit")
        self.audit.committed_event.return_value = None
        self.audit.pending_event.return_value = None
        self.writer.audit = self.audit

    def denial(self) -> dict[str, Any]:
        (_request_id, event), _ = self.audit.stage.call_args
        self.assertEqual(event["kind"], "sprint_guard_denied")
        return event


class ObserverSprintCommentTests(SprintWriterFixture):
    def test_the_bound_observer_comments_on_its_own_sprint_under_its_own_marker(self) -> None:
        written: list[Any] = []

        def write(kind, role, actor, reference, request_id, payload, mutation):
            mutation({"id": "sprint_postgres_1465"})
            written.append((kind, role, actor, reference))
            return {"event_id": "evt_1"}

        with as_observer(SPRINT), mock.patch.object(self.writer, "_write", side_effect=write):
            self.writer.comment(role="observer", actor="observer", reference=SPRINT, body="hold the release")
        self.assertEqual(written, [("commented", "observer", "observer", SPRINT)])
        self.client.call.assert_called_once_with(
            "createComment", task_id=1465, user_id=0, content="[observer]\nhold the release"
        )
        self.audit.stage.assert_not_called()

    def test_an_unbound_or_foreign_observer_comment_is_refused_and_audited(self) -> None:
        for context, code in (
            (unbound_observer(), "observer_identity_unbound"),
            (as_observer(OTHER_SPRINT), "observer_sprint_mismatch"),
        ):
            with self.subTest(code=code):
                self.audit.reset_mock()
                self.client.reset_mock()
                with context, self.assertRaises(TaskError) as raised:
                    self.writer.comment(
                        role="observer", actor="observer", reference=SPRINT, body="x", request_id="r1"
                    )
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(self.denial()["payload"]["code"], code)
                self.client.call.assert_not_called()

    def test_the_observer_s_own_comment_does_not_wake_it(self) -> None:
        event = {
            "ref": SPRINT,
            "outcome": "success",
            "kind": "commented",
            "actor": {"role": "observer", "id": "observer"},
        }
        self.assertFalse(is_significant_observer_event(event, linked_refs=set(), sprint_ref=SPRINT))
        # The PO's comment on the same sprint still does.
        po = {**event, "actor": {"role": "po", "id": "vladmesh-ummanu"}}
        self.assertTrue(is_significant_observer_event(po, linked_refs=set(), sprint_ref=SPRINT))


class ObserverSprintCloseTests(SprintWriterFixture):
    def test_the_bound_observer_closes_its_own_sprint_with_the_po_close_arguments(self) -> None:
        decisions = {"issues": [{"ref": "issue:1", "verdict": "resolved", "reason": "done"}], "cards": []}
        with as_observer(SPRINT), mock.patch.object(
            self.writer, "_close_atomic", return_value={"closed": True}
        ) as close:
            self.writer.close(
                role="observer",
                actor="observer",
                reference=SPRINT,
                decisions=decisions,
                request_id="close-1",
                reason="goal reached",
                closeout="# closeout",
            )
        close.assert_called_once_with(
            role="observer",
            actor="observer",
            reference=SPRINT,
            decisions=decisions,
            request_id="close-1",
            reason="goal reached",
            closeout="# closeout",
        )
        self.audit.stage.assert_not_called()

    def test_closing_another_sprint_or_an_unbound_close_is_refused_before_anything_is_read(self) -> None:
        for context, code in (
            (unbound_observer(), "observer_identity_unbound"),
            (as_observer(OTHER_SPRINT), "observer_sprint_mismatch"),
        ):
            with self.subTest(code=code):
                self.audit.reset_mock()
                self.client.reset_mock()
                with (
                    context,
                    mock.patch.object(self.writer, "_close_atomic") as close,
                    self.assertRaises(TaskError) as raised,
                ):
                    self.writer.close(role="observer", actor="observer", reference=SPRINT, request_id="close-1")
                self.assertEqual(raised.exception.code, code)
                close.assert_not_called()
                self.assertEqual(self.denial()["payload"]["operation_request_id"], "close-1")

    def test_the_po_close_and_its_masquerade(self) -> None:
        with mock.patch.object(self.writer, "_close_atomic", return_value={}) as close:
            self.writer.close(role="po", actor="vladmesh-ummanu", reference=SPRINT, request_id="c")
            with self.assertRaises(TaskError) as raised:
                self.writer.close(role="po", actor="observer", reference=SPRINT, request_id="c2")
        self.assertEqual(raised.exception.code, "role_masquerade")
        close.assert_called_once()

    def test_the_close_steps_are_written_in_the_closing_caller_s_name(self) -> None:
        """An observer's close archives, disposes and closes issues as the observer, never as the PO."""
        document = {
            "request_id": "close-1",
            "intent": {"role": "observer", "actor": "observer", "reference": SPRINT},
            "event": {"ref": SPRINT, "payload": {}},
        }
        payload: dict[str, Any] = {}
        from ummanu.board.sprint_close import SprintCloseDecisions

        decisions = SprintCloseDecisions.from_document(
            {
                "issues": [{"ref": "issue:1", "verdict": "resolved", "reason": "done"}],
                "cards": [{"ref": "ummanu-2", "verdict": "drop", "reason": "later"}],
            }
        )
        store = mock.MagicMock()
        store.show_issue.return_value = {"closed": False}
        with (
            mock.patch.object(self.writer, "_issue_store", return_value=store),
            mock.patch.object(self.writer, "_close_step_status", return_value="todo"),
            mock.patch.object(self.writer, "_require_close_step_settled"),
            mock.patch.object(self.writer.transactions, "save"),
            mock.patch("ummanu.board.sprint_protocol.lifecycle.TaskWriter") as writer_class,
            mock.patch("ummanu.board.sprint_protocol.lifecycle.TaskReader") as reader_class,
        ):
            reader_class.return_value.show.return_value = {"state": "in_progress"}
            self.writer._close_declared_issues(document, document["event"], payload, decisions)
            targets = mock.MagicMock(remaining_state_map={"ummanu-2": "in_progress"})
            self.writer._dispose_remaining_cards(document, document["event"], payload, decisions, targets)
        close_issue = store.close_issue.call_args.kwargs
        self.assertEqual((close_issue["role"], close_issue["actor"]), ("observer", "observer"))
        self.assertEqual(close_issue["sprint_close"], SPRINT)
        move = writer_class.return_value.move.call_args.kwargs
        self.assertEqual((move["role"], move["actor"], move["sprint_override"]), ("observer", "observer", False))
        archive = writer_class.return_value.archive.call_args.kwargs
        self.assertEqual((archive["role"], archive["sprint_close"]), ("observer", SPRINT))


class CloseDispositionPlanTests(SprintWriterFixture):
    """A close is refused whole, before staging, when its role may not make one of its moves."""

    DECISIONS: ClassVar[dict[str, list[dict[str, str]]]] = {
        "issues": [{"ref": "issue:1", "verdict": "resolved", "reason": "landed"}],
        "cards": [{"ref": "ummanu-3", "verdict": "done", "reason": "reviewed green"}],
    }

    def close(self, role: str, actor: str) -> tuple[Any, mock.MagicMock, mock.MagicMock, mock.MagicMock]:
        """One un-mocked close down to staging, over a sprint with a Done card and one in Assessment."""
        store = mock.MagicMock(name="issues")
        store.show_issue.return_value = {"closed": False, "close_reason": ""}
        cards = [
            {"ref": "ummanu-2", "state": "done", "sprint": SPRINT},
            {"ref": "ummanu-3", "state": "assessment", "sprint": SPRINT},
        ]
        sprint = {
            "id": "sprint_postgres_1465",
            "ref": SPRINT,
            "goal": "g",
            "issues": ["issue:1"],
            "reservations": ["ummanu"],
            "dod": sprint_dod.section(None),
        }
        committed = {"kind": "closed", "ref": SPRINT, "payload": {}}
        with (
            mock.patch.object(self.writer.reader, "show", return_value=sprint),
            mock.patch("ummanu.board.sprint_protocol.lifecycle.TaskReader") as reader_class,
            mock.patch("ummanu.board.sprint_protocol.lifecycle.TaskWriter") as writer_class,
            mock.patch.object(self.writer, "_issue_store", return_value=store),
            mock.patch.object(self.writer.transactions, "existing", return_value=(None, None)),
            mock.patch.object(self.writer.transactions, "begin", return_value=(None, committed)) as begin,
            mock.patch.object(self.writer, "_close_result", return_value={"closed": True}),
            # A refused close drops its request id's staging, of which there is none.
            mock.patch.object(self.writer.transactions, "drop"),
        ):
            reader_class.return_value.list.return_value = cards
            try:
                answer: Any = self.writer.close(
                    role=role, actor=actor, reference=SPRINT, decisions=self.DECISIONS, request_id="close-1"
                )
            except TaskError as exc:
                answer = exc
        return answer, begin, store, writer_class.return_value

    def test_an_observer_close_disposing_an_assessment_card_is_refused_with_nothing_written(self) -> None:
        with as_observer(SPRINT):
            refused, begin, store, writer = self.close("observer", "observer")
        self.assertIsInstance(refused, TaskError)
        self.assertEqual(refused.code, "close_plan_forbidden")
        self.assertIn("ummanu-3 (in assessment, done)", refused.message)
        self.assertIn("task decide", refused.message)
        begin.assert_not_called()
        self.client.sprints.save_close.assert_not_called()
        store.close_issue.assert_not_called()
        writer.archive.assert_not_called()
        writer.move.assert_not_called()
        self.audit.stage.assert_not_called()

    def test_the_same_plan_as_a_po_close_under_the_override_is_staged(self) -> None:
        answer, begin, _store, _writer = self.close("po", "vladmesh-ummanu")
        self.assertEqual(answer, {"closed": True})
        begin.assert_called_once()

    def test_the_refusal_comes_from_the_transition_table_not_from_a_named_column(self) -> None:
        from ummanu.board import card_transitions

        allowed = card_transitions.CARD_TRANSITIONS
        widened = {**allowed, Role.OBSERVER: allowed[Role.PO]}
        with as_observer(SPRINT), mock.patch.dict(card_transitions.CARD_TRANSITIONS, widened):
            answer, begin, _store, _writer = self.close("observer", "observer")
        self.assertEqual(answer, {"closed": True})
        begin.assert_called_once()


class ObserverIssueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.client = mock.MagicMock(name="board")
        self.store = ProductIssueStore(self.client, data_dir=self.root / "data", instance=self.root)
        self.audit = mock.MagicMock(name="audit")
        self.audit.committed_event.return_value = None
        self.audit.pending_event.return_value = None
        self.host = mock.MagicMock(name="host")
        self.host.canon.event.return_value = None
        self.created: list[Any] = []
        self.host.create.side_effect = self.created.append
        for patcher in (
            mock.patch("ummanu.sprints.task_audit_for", return_value=self.audit),
            mock.patch.object(ProductIssueStore, "_host", return_value=self.host),
            mock.patch.object(ProductIssueStore, "_reject_other_pending_reference_operation"),
            mock.patch.object(ProductIssueStore, "_reject_other_pending_typed_operation"),
            mock.patch.object(ProductIssueStore, "show_issue", side_effect=lambda ref: {"ref": ref}),
            mock.patch(
                "ummanu.sprints.SprintReader.show",
                side_effect=lambda ref, **_: {"ref": ref, "product": "ummanu"},
            ),
        ):
            self.enterContext(patcher)

    def create(self, **options: Any) -> dict[str, Any]:
        return self.store.create_issue(
            **{
                "product": "",
                "issue_kind": "bug",
                "priority": "P2",
                "title": "deferred finding",
                "description": "evidence",
                "actor": "observer",
                "role": "observer",
                "request_id": "finding-1",
                **options,
            }
        )

    def test_the_bound_observer_files_an_issue_for_its_sprint_s_product_naming_the_sprint(self) -> None:
        with as_observer(SPRINT):
            self.create()
        (operation,) = self.created
        self.assertEqual(operation.entity.product_ref, "product:ummanu")
        self.assertEqual((operation.actor.role, operation.actor.id), (Role.OBSERVER, "observer"))
        self.assertEqual(operation.related_refs.refs, (SPRINT,))

    def test_an_unbound_observer_files_nothing(self) -> None:
        with unbound_observer(), self.assertRaises(TaskError) as raised:
            self.create()
        self.assertEqual(raised.exception.code, "observer_identity_unbound")
        self.assertEqual(self.created, [])

    def test_another_product_than_the_sprint_s_is_refused(self) -> None:
        with as_observer(SPRINT), self.assertRaises(TaskError) as raised:
            self.create(product="site")
        self.assertEqual(raised.exception.code, "validation")
        self.assertEqual(self.created, [])

    def test_the_observer_may_not_close_reopen_edit_or_reprioritize_an_issue(self) -> None:
        calls = {
            "close": lambda: self.store.close_issue(
                reference="issue:1", reason="resolved", actor="observer", role="observer"
            ),
            "update-priority": lambda: self.store.update_priority(
                reference="issue:1", priority="P0", reason="r", actor="observer", role="observer"
            ),
            "append": lambda: self.store.append_description(
                reference="issue:1", body="b", reason="r", actor="observer", role="observer"
            ),
            "edit title": lambda: self.store.edit_fields(
                reference="issue:1", title="t", reason="r", actor="observer", role="observer"
            ),
            "reopen": lambda: self.store.reopen_issue(
                reference="issue:1", reason="r", actor="observer", role="observer"
            ),
            "product create": lambda: self.store.create_product(
                product_id="p", projects=["ummanu"], title="t", description="", actor="observer", role="observer"
            ),
        }
        with as_observer(SPRINT):
            for name, call in calls.items():
                with self.subTest(verb=name), self.assertRaises(TaskError) as raised:
                    call()
                self.assertEqual(raised.exception.code, "role_forbidden")
        self.host.transition.assert_not_called()
        self.host.replace.assert_not_called()

    def test_the_po_masquerade_files_nothing(self) -> None:
        with self.assertRaises(TaskError) as raised:
            self.create(role="po", product="ummanu")
        self.assertEqual(raised.exception.code, "role_masquerade")
        self.assertEqual(self.created, [])


class ObserverTaskWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.writer = TaskWriter(mock.MagicMock(name="board"), data_dir=tempfile.mkdtemp())
        self.writer.reader = mock.MagicMock()
        self.writer.reader.show.return_value = {
            "ref": "issue:1",
            "record_type": "issue",
            "project": "ummanu",
            "state": "issues",
        }

    def test_the_observer_may_not_promote_or_edit_a_product_issue(self) -> None:
        with as_observer(SPRINT):
            with self.assertRaises(TaskError) as raised:
                self.writer.move(role="observer", actor="observer", reference="issue:1", target="ready", reason="")
            self.assertEqual(raised.exception.code, "role_forbidden")
            with self.assertRaises(TaskError) as raised:
                self.writer.edit(role="observer", actor="observer", reference="issue:1", title="t")
            self.assertEqual(raised.exception.code, "role_forbidden")

    def test_archive_admits_the_observer_only_as_a_step_of_its_sprint_s_close(self) -> None:
        with as_observer(SPRINT):
            with self.assertRaises(TaskError) as raised:
                self.writer.archive(role="observer", actor="observer", reference="ummanu-2", reason="r")
            self.assertEqual(raised.exception.code, "role_forbidden")
        self.writer.audit = mock.MagicMock()
        self.writer.audit.committed_event.return_value = None
        with as_observer(OTHER_SPRINT), self.assertRaises(TaskError) as raised:
            self.writer.archive(
                role="observer", actor="observer", reference="ummanu-2", reason="r", sprint_close=SPRINT
            )
        self.assertEqual(raised.exception.code, "observer_sprint_mismatch")


class IssueCliActorTests(unittest.TestCase):
    def run_cli(self, argv: list[str], env: dict[str, str]) -> tuple[int, dict[str, Any], list[dict]]:
        captured: list[dict[str, Any]] = []

        class Store:
            def create_issue(self, **kwargs: Any) -> dict[str, Any]:
                captured.append(kwargs)
                return {"ref": "issue:x"}

        out, err = io.StringIO(), io.StringIO()
        # The store is a stub, but the command still names an installation: unnamed, it would take
        # the default live root and refuse it where it is absent, as on a CI runner (ummanu-39).
        live_root = tempfile.TemporaryDirectory()
        self.addCleanup(live_root.cleanup)
        with (
            mock.patch.dict(os.environ, {**env, "UMMANU_INSTANCE": live_root.name}),
            mock.patch.object(product_issue_commands, "_store", return_value=Store()),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = main(argv)
        text = (err.getvalue() or out.getvalue()).strip()
        return code, json.loads(text) if text else {}, captured

    ISSUE = ("issue", "create", "--kind", "bug", "--priority", "P2", "--title", "t")

    def test_the_actor_defaults_to_board_actor_and_the_role_travels_with_it(self) -> None:
        code, _answer, captured = self.run_cli([*self.ISSUE, "--role", "observer"], {"BOARD_ACTOR": "observer"})
        self.assertEqual(code, 0)
        self.assertEqual((captured[0]["role"], captured[0]["actor"]), ("observer", "observer"))

    def test_no_actor_at_all_is_refused_rather_than_written_as_po(self) -> None:
        code, answer, captured = self.run_cli([*self.ISSUE, "--role", "po", "--product", "p"], {"BOARD_ACTOR": ""})
        self.assertEqual((code, answer["error"]["code"]), (2, "actor_required"))
        self.assertEqual(captured, [])


class BoardActorEnvTests(unittest.TestCase):
    def env(self, role: str, **base: str) -> dict[str, str]:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "runtime.env"
            env_file.write_text("BOARD_ACTOR=forged\n", encoding="utf-8")
            with mock.patch.object(role_env, "managed_venv_bin", return_value=Path(tmp) / "bin"):
                return role_env.runtime_env(role, base_env={"PATH": "/usr/bin", **base}, env_file=env_file)

    def test_every_board_role_head_names_its_actor(self) -> None:
        for role in ("observer", "steward", "retro"):
            with self.subTest(role=role):
                self.assertEqual(self.env(role)["BOARD_ACTOR"], role)
        # Neither runtime.env nor an ambient value can make the observer another actor.
        self.assertEqual(self.env("observer", BOARD_ACTOR="po")["BOARD_ACTOR"], "observer")

    def test_a_worker_or_reviewer_is_the_head_its_launcher_named(self) -> None:
        for role in ("worker", "reviewer"):
            with self.subTest(role=role):
                self.assertEqual(self.env(role)["BOARD_ACTOR"], role)
                self.assertEqual(self.env(role, BOARD_ACTOR="claude-opus-high")["BOARD_ACTOR"], "claude-opus-high")

    def test_a_role_that_is_not_a_board_role_carries_none(self) -> None:
        for role in ("pipeline", "curator"):
            with self.subTest(role=role):
                self.assertNotIn("BOARD_ACTOR", self.env(role, BOARD_ACTOR="x"))


if __name__ == "__main__":
    unittest.main()
