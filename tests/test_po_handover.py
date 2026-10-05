"""A decision/operation card handed to the owner, the owner's answer, and the sprint reading `waiting`.

Unit-level (secretary-1761), on the fakes of cards 1 to 3. `task handover` and `task comment --role
owner` run `TaskWriter` over a mock client whose writes land on one in-memory card, with an in-memory
audit that keeps the claim rules of `SqlTaskAudit`. The dispatcher side runs the real
`advance_po_card` with a real `PoService` over its socket (`tests.test_po_cards.DispatcherFixture`).
The PostgreSQL paths (the handover and the completion each in one transaction) are covered by the
integration-board suite (`tests/test_tasks.py`, `tests/test_web_sprint_protocol.py`).
"""

from __future__ import annotations

import contextlib
import copy
import io
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from tests.po_card_fakes import DECISION_BODY, REF, SPRINT, DispatcherFixture, card
from tests.po_fake_store import FakePoStore
from tests.po_handover_fakes import (
    REASON,
    SINCE,
    HandedOverFixture,
    MemoryAudit,
    OneCardClient,
    decision_card,
)
from ummanu.board.owner_events import ReadRefused
from ummanu.board.owner_handover import (
    HANDED_TO_OWNER,
    MARK_KEYS,
    OWNER_ANSWER,
    attention_record,
    mark_values,
    owner_answer_event_ids,
    owner_comments_since_handover,
    render_handover_comment,
    waiting_owner,
)
from ummanu.board.po_execution import assignment, create_assignment
from ummanu.cli import main
from ummanu.dispatch.po_cards import (
    PO_BLOCKED_ACTION,
    ServicePoChannel,
    advance_po_card,
    complete_command,
    handover_command,
    owner_answer_request_id,
)
from ummanu.dispatch.state import attempt_request_id
from ummanu.po import store as po_store
from ummanu.po.queue import PoQueue
from ummanu.po.sprints import SprintRecord
from ummanu.tasks import TaskError, TaskReader, TaskWriter
from ummanu.web import pages
from ummanu.webproto import sources
from ummanu.webproto.reads import _card_value
from ummanu.webproto.section import Reading, SourceSet, render
from ummanu.webproto.sprint_reads import (
    SECTIONS,
    SOURCE_CARDS,
    SOURCE_LIVENESS,
    SOURCE_SPRINTS,
    WAITING_BLOCKED,
    WAITING_WAITING,
    _Production,
)


class AssignedOwnerEpisodeTests(HandedOverFixture):
    def submitted_card(self, description="WAIT-GATE"):
        self.start()
        task = card(description=description)
        task["sprint"] = ""
        task["extensions"] = {"extra": {"po_execution": create_assignment(
            "dispatcher-e2e-cap-source-3", "e2e_budget", ["source"])}}
        runtime = self.runtime(task, comments=[])
        runtime.po.successor_choice = lambda _closed: ("claude", "opus", "high")
        self.claim(runtime)
        return runtime, self.record().po_submission.session_id

    def test_two_explicit_handovers_answer_restart_and_done_use_current_episode(self):
        runtime, session = self.submitted_card()
        self.hand_over("Which route?")
        self.assertEqual(self.tick(runtime)["action"], "po-card-waiting-owner")
        self.owner_says("Use the existing route.", "evt-assigned-answer-1")
        self.assertEqual(self.tick(runtime)["action"], "po-owner-answer-submitted")
        # The current handover wins while the previous turn is still running.
        self.hand_over("Which remaining route?")
        self.records.clear()
        self.assertEqual(self.tick(runtime)["action"], "po-card-waiting-owner")
        self.owner_says("Use the same authority.", "evt-assigned-answer-2")
        self.assertEqual(self.tick(runtime)["action"], "po-owner-answer-submitted")
        self.cards.complete_as_po("decision", DECISION_BODY)
        self.assertEqual(self.tick(runtime)["action"], "po-card-closed")
        self.gate.touch()
        self.settled(session, 3)
        self.assertEqual(len(FakePoStore(self.board).turns(session)), 3)
        self.assertEqual(assignment(self.cards.card).executor, session)
        self.assertNotIn("po_origin", self.cards.card["extensions"]["extra"])
        self.assertNotIn("owner_escalation", self.cards.card["extensions"]["extra"])

    def test_done_or_new_handover_during_assigned_turn_read_wins(self):
        for change in ("done", "handover"):
            with self.subTest(change=change):
                fixture = AssignedOwnerEpisodeTests()
                fixture.setUp()
                try:
                    runtime, session = fixture.submitted_card()
                    fixture.gate.touch()
                    fixture.settled(session, 1)
                    turn_read = runtime.po.turn
                    def raced(*args, fixture=fixture, turn_read=turn_read, change=change):
                        result = turn_read(*args)
                        if change == "done":
                            fixture.cards.complete_as_po("decision", DECISION_BODY)
                        else:
                            fixture.hand_over("Which route?")
                        return result
                    with mock.patch.object(runtime.po, "turn", side_effect=raced):
                        outcome = fixture.tick(runtime)
                    self.assertEqual(outcome["action"], "po-card-closed" if change == "done" else "po-card-waiting-owner")
                    self.assertFalse(any(e["kind"] == "move" and e["to"] == "blocked" for e in fixture.cards.log))
                    self.assertNotIn("owner_escalation", fixture.cards.card["extensions"]["extra"])
                finally:
                    fixture.doCleanups()


class WriterFixture(unittest.TestCase):
    def setUp(self) -> None:
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.card = decision_card()
        self.client = OneCardClient(self.card, tmp)
        self.writer = TaskWriter(self.client, data_dir=tmp)  # type: ignore[arg-type]
        self.writer.audit = MemoryAudit()
        self.writer.reader = mock.Mock(show=lambda reference: copy.deepcopy(self.card))
        patcher = mock.patch("ummanu.tasks._task_number", return_value=1900)
        patcher.start()
        self.addCleanup(patcher.stop)

    def hand_over(self, **fields: Any) -> dict[str, Any]:
        call = {"role": "po", "actor": "po", "reference": REF, "to": "owner", "reason": REASON,
                "request_id": "handover-1", **fields}
        return self.writer.handover(**call)


class HandoverTests(WriterFixture):
    def test_it_marks_the_card_writes_the_comment_and_the_audit_fact_and_leaves_it_in_progress(self) -> None:
        answer = self.hand_over()

        self.assertEqual((answer["action"], answer["replayed"]), (HANDED_TO_OWNER, False))
        self.assertEqual(self.card["state"], "in_progress")
        mark = waiting_owner(self.card)
        self.assertEqual((mark["reason"], mark["by"]), (REASON, "po"))
        datetime.fromisoformat(mark["since"])
        [comment] = self.card["comments"]
        self.assertEqual(comment["body"], "[po]\n" + render_handover_comment(REASON))
        self.assertEqual(comment["body"].splitlines()[1], "[handover:owner]")
        [event] = self.writer.audit.events(REF)
        self.assertEqual((event["kind"], event["ref"], event["request_id"]), (HANDED_TO_OWNER, REF, "handover-1"))
        self.assertEqual(event["actor"], {"role": "po", "id": "po"})
        self.assertEqual(
            {key: event["payload"][key] for key in ("to", "kind", "sprint", "waiting_owner")},
            {"to": "owner", "kind": "decision", "sprint": SPRINT, "waiting_owner": mark["since"]},
        )
        # The mark is written only through the three validated fields, and never by a transition.
        [(_method, params)] = [write for write in self.client.writes if write[0] == "saveTaskMetadata"]
        self.assertEqual(set(params["values"]), {*MARK_KEYS, "owner_answer", "owner_escalation"})

    def test_a_repeat_under_the_same_id_writes_nothing_and_another_reason_is_refused(self) -> None:
        first = self.hand_over()
        writes = list(self.client.writes)

        again = self.hand_over()

        self.assertEqual((again["replayed"], again["event_id"]), (True, first["event_id"]))
        self.assertEqual(self.client.writes, writes)
        with self.assertRaises(TaskError) as raised:
            self.hand_over(reason="Something else entirely.")
        self.assertEqual(raised.exception.code, "validation")
        self.assertEqual(self.client.writes, writes)

    def test_each_refusal_writes_nothing(self) -> None:
        for fields, code in (
            ({"role": "observer"}, "role_forbidden"),
            ({"role": "worker"}, "role_forbidden"),
            ({"to": "observer"}, "validation"),
            ({"reason": "   "}, "validation"),
        ):
            with self.subTest(fields=fields), self.assertRaises(TaskError) as raised:
                self.hand_over(request_id=f"refused-{code}-{len(fields)}", **fields)
            self.assertEqual(raised.exception.code, code)
        for document, code in (
            (decision_card(kind="code"), "validation"),
            (decision_card(kind="research"), "validation"),
            (decision_card(state="ready"), "transition_forbidden"),
            (decision_card(state="blocked", kind="operation"), "transition_forbidden"),
            (decision_card(state="done"), "transition_forbidden"),
        ):
            self.card.clear()
            self.card.update(document)
            with self.subTest(kind=document["type"], state=document["state"]), self.assertRaises(TaskError) as raised:
                self.hand_over(request_id=f"refused-{document['type']}-{document['state']}")
            self.assertEqual(raised.exception.code, code)
        self.assertEqual((self.client.writes, self.writer.audit.events()), ([], []))

    def test_a_card_already_handed_over_is_refused_with_its_mark(self) -> None:
        self.hand_over()
        writes = list(self.client.writes)
        with self.assertRaises(TaskError) as raised:
            self.hand_over(request_id="handover-2", reason="A second reason.")
        self.assertEqual(raised.exception.code, "already_handed_over")
        self.assertIn(REASON, raised.exception.message)
        self.assertEqual(self.client.writes, writes)
        self.assertEqual(len(self.writer.audit.events()), 1)

    def test_the_mark_is_visible_in_task_show_and_list_and_on_the_card_page(self) -> None:
        reader = TaskReader(mock.Mock())
        columns, swimlanes = {3: "In progress"}, {}
        row = {"id": 1900, "reference": REF, "title": "The decision", "column_id": 3, "is_active": 1}
        meta = {"task_type": "decision", "sprint_ref": SPRINT, **mark_values(SINCE, REASON, "po")}

        shown = reader._normalize(row, columns, swimlanes, meta, comments=[])
        plain = reader._normalize(row, columns, swimlanes, {"task_type": "decision"}, comments=[])

        self.assertEqual(shown["waiting_owner"], {"since": SINCE, "reason": REASON, "by": "po"})
        self.assertNotIn("waiting_owner", plain)
        value = _card_value(shown)
        self.assertEqual(value["waiting_owner"]["reason"], REASON)
        self.assertIsNone(_card_value(plain)["waiting_owner"])
        html = pages.task(
            {"ref": REF, "card": {"source": None, "value": value}, "project": {}, "events": {}, "agents": {}},
            runs={},
        )
        self.assertIn("waiting for the owner", html)
        self.assertIn("handed to the owner", html)
        self.assertIn("Pay the relay provider", html)

    def test_a_partial_or_malformed_mark_is_no_mark(self) -> None:
        for bag in (
            {"waiting_owner": SINCE},
            {**mark_values(SINCE, REASON, "po"), "waiting_owner_by": ""},
            mark_values("yesterday", REASON, "po"),
        ):
            with self.subTest(bag=bag):
                self.assertIsNone(waiting_owner({"extensions": {"extra": bag}}))


class OwnerCommentTests(WriterFixture):
    def test_both_answer_channels_settle_attention_before_card_completion(self) -> None:
        for channel in ("comment", "conversation"):
            with self.subTest(channel=channel):
                self.card.clear()
                self.card.update(decision_card())
                self.writer.audit = MemoryAudit()
                handover = self.hand_over(request_id=f"handover-{channel}", po_session="po-origin")
                event = self.client.owner_events.of_kind("card_handed_to_owner")[-1]
                self.assertTrue(event.unread)
                quotation = " Yes, use the company card.\n"
                if channel == "comment":
                    result = self.writer.comment(role="owner", actor="owner", reference=REF,
                        body=quotation, request_id=f"answer-{channel}")
                else:
                    result = self.writer.record_owner_answer(role="po", actor="po", reference=REF,
                        handover_event=handover["event_id"], quotation=quotation, request_id=f"answer-{channel}")
                self.assertIsNone(waiting_owner(self.card))
                self.assertEqual(self.card["state"], "in_progress")
                answer = attention_record(self.card, OWNER_ANSWER)
                self.assertEqual(answer["quotation"], quotation)
                self.assertEqual(answer["handover_event"], handover["event_id"])
                self.assertEqual(answer["event_id"], result["event_id"])
                self.assertEqual(answer["po_session"], "po-origin")
                self.assertFalse(self.client.owner_events.rows[event.id].unread)
                self.assertFalse(self.client.owner_events.mark_read(event.id).held)

    def test_answer_replay_and_earlier_comments_cannot_answer_a_later_epoch(self) -> None:
        self.writer.comment(role="owner", actor="owner", reference=REF, body="Earlier approval.", request_id="old-comment")
        first = self.hand_over()
        self.assertIsNotNone(waiting_owner(self.card))
        fields = dict(role="po", actor="po", reference=REF, handover_event=first["event_id"],
                      quotation="Approved.", request_id="answer-1")
        result = self.writer.record_owner_answer(**fields)
        self.assertTrue(self.writer.record_owner_answer(**fields)["replayed"])
        self.assertEqual(attention_record(self.card, OWNER_ANSWER)["event_id"], result["event_id"])
        second = self.hand_over(request_id="handover-2", reason="Which monthly plan?")
        self.assertIsNone(attention_record(self.card, OWNER_ANSWER))
        self.assertTrue(self.writer.record_owner_answer(**fields)["replayed"])
        self.assertIsNotNone(waiting_owner(self.card))
        with self.assertRaises(TaskError):
            self.writer.record_owner_answer(**{**fields, "request_id": "stale-answer"})
        with self.assertRaises(TaskError):
            self.writer.record_owner_answer(**{**fields, "role": "observer", "request_id": "bad-role"})
        with self.assertRaises(TaskError):
            self.writer.record_owner_answer(**{**fields, "quotation": " ", "handover_event": second["event_id"], "request_id": "blank"})
        self.assertIsNotNone(waiting_owner(self.card))

    def test_released_owner_comment_recovery_uses_actual_quotation_and_epoch(self) -> None:
        self.hand_over()
        quote = "Approved in the released writer."
        self.card["comments"].append({"marker": "owner", "body": "[owner]\n" + quote})
        import hashlib
        self.writer.audit.committed["released-comment"] = {
            "ref": REF, "event_id": "released-event", "kind": "commented", "occurred_at": SINCE,
            "payload": {"marker": "owner", "body_sha256": hashlib.sha256(quote.encode()).hexdigest()}}
        answer = self.writer.accept_owner_comment(actor="dispatcher", reference=REF, event_id="released-event")
        self.assertFalse(answer["replayed"])
        self.assertIsNone(waiting_owner(self.card))
        self.assertEqual(attention_record(self.card, OWNER_ANSWER)["quotation"], quote)
        self.assertTrue(self.writer.accept_owner_comment(actor="dispatcher", reference=REF, event_id="released-event")["replayed"])

    def test_the_owner_comments_on_any_card_as_the_owner(self) -> None:
        for document in (decision_card(), decision_card(kind="code", state="done")):
            self.card.clear()
            self.card.update(document)
            with self.subTest(kind=document["type"]):
                answer = self.writer.comment(
                    role="owner", actor="somebody", reference=REF, body="Yes, use the company card.",
                    request_id=f"owner-{document['type']}",
                )
                self.assertEqual(answer["action"], "commented")
                self.assertEqual(self.card["comments"][-1]["body"], "[owner]\nYes, use the company card.")
                self.assertEqual(self.card["comments"][-1]["marker"], "owner")
                event = self.writer.audit.committed_event(f"owner-{document['type']}")
                self.assertEqual((event["actor"], event["payload"]["marker"]), ({"role": "owner", "id": "owner"}, "owner"))

    def test_released_blank_owner_comment_does_not_end_the_owner_turn(self) -> None:
        import hashlib
        self.hand_over()
        self.card["comments"].append({"marker": "owner", "body": "[owner]\n "})
        self.writer.audit.committed["released-comment"] = {
            "ref": REF, "event_id": "released-blank", "kind": "commented", "occurred_at": SINCE,
            "payload": {"marker": "owner", "body_sha256": hashlib.sha256(b" ").hexdigest()}}
        with self.assertRaises(TaskError):
            self.writer.accept_owner_comment(actor="dispatcher", reference=REF, event_id="released-blank")
        self.assertIsNotNone(waiting_owner(self.card))
        self.assertIsNone(attention_record(self.card, OWNER_ANSWER))

    def test_blank_owner_comment_does_not_prevent_a_native_conversation_answer(self) -> None:
        handover = self.hand_over()
        self.writer.comment(role="owner", actor="owner", reference=REF, body=" ", request_id="blank-comment")
        self.assertIsNotNone(waiting_owner(self.card))
        self.writer.record_owner_answer(role="po", actor="po", reference=REF,
            handover_event=handover["event_id"], quotation="Use existing refusal-1.", request_id="real-answer")
        self.assertIsNone(waiting_owner(self.card))
        self.assertEqual(attention_record(self.card, OWNER_ANSWER)["quotation"], "Use existing refusal-1.")

    def test_the_owner_role_is_a_comment_role_only(self) -> None:
        for call in (
            lambda: self.writer.move(role="owner", actor="owner", reference=REF, target="done", reason="x"),
            lambda: self.writer.handover(role="owner", actor="owner", reference=REF, to="owner", reason="x"),
        ):
            with self.assertRaises(TaskError) as raised:
                call()
            self.assertEqual(raised.exception.code, "role_forbidden")

    def test_the_cli_takes_owner_on_comment_only_and_passes_handover_through(self) -> None:
        writer = mock.Mock()
        writer.return_value.comment.return_value = {"action": "commented"}
        writer.return_value.handover.return_value = {"action": HANDED_TO_OWNER}
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch("ummanu.task_commands.TaskWriter", writer),
            mock.patch("ummanu.task_commands.card_client"),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            body = f"{tmp}/body.md"
            with open(body, "w", encoding="utf-8") as handle:
                handle.write(REASON)
            base = ["--instance", tmp, "--data-dir", tmp]
            codes = [
                main(["task", "comment", "--ref", REF, "--role", "owner", "--body-file", body, *base]),
                main(["task", "report", "--ref", REF, "--role", "owner", "--kind", "done", "--body-file", body, *base]),
                main(["task", "handover", "--ref", REF, "--role", "po", "--to", "owner", "--reason", REASON,
                      "--request-id", "h-1", *base]),
                main(["task", "handover", "--ref", REF, "--role", "po", "--to", "owner", "--reason-file", body, *base]),
                main(["task", "handover", "--ref", REF, "--role", "observer", "--to", "owner", "--reason", "x", *base]),
                main(["task", "handover", "--ref", REF, "--role", "po", "--to", "owner", *base]),
            ]
        self.assertEqual(codes, [0, 2, 0, 0, 2, 2])
        self.assertEqual(writer.return_value.comment.call_args.kwargs["role"], "owner")
        first, second = (call.kwargs for call in writer.return_value.handover.call_args_list)
        self.assertEqual(
            (first["reference"], first["to"], first["reason"], first["request_id"], first["role"]),
            (REF, "owner", REASON, "h-1", "po"),
        )
        self.assertEqual((second["reason"], second["request_id"]), (REASON, None))

    def test_cli_records_conversation_quotation_as_po_for_the_named_epoch(self) -> None:
        writer = mock.Mock()
        writer.return_value.record_owner_answer.return_value = {"action": "owner_answer_recorded"}
        with (tempfile.TemporaryDirectory() as tmp,
              mock.patch("ummanu.task_commands.TaskWriter", writer),
              mock.patch("ummanu.task_commands.card_client"),
              contextlib.redirect_stdout(io.StringIO())):
            body = Path(tmp) / "answer.md"
            body.write_text("Use standing decision refusal-1.\n", encoding="utf-8")
            code = main(["task", "record-owner-answer", "--ref", REF, "--role", "po",
                         "--handover-event", "epoch-1", "--body-file", str(body),
                         "--request-id", "answer-1", "--instance", tmp, "--data-dir", tmp])
        self.assertEqual(code, 0)
        call = writer.return_value.record_owner_answer.call_args.kwargs
        self.assertEqual((call["role"], call["reference"], call["handover_event"], call["quotation"], call["request_id"]),
                         ("po", REF, "epoch-1", "Use standing decision refusal-1.\n", "answer-1"))


class EscalationTests(WriterFixture):
    def runtime(self, state: str | None = None) -> Any:
        self.writer.audit.committed["claim-episode"] = {"ref": REF, "kind": "card.started",
            "event_id": "claim-epoch", "occurred_at": SINCE}
        po = mock.Mock()
        po.request.return_value = None if state is None else SimpleNamespace(session_id="session", seq=1)
        po.refused.return_value = None
        po.queued.return_value = None
        po.turn.return_value = SimpleNamespace(state=state, session_id="session", seq=1)
        return SimpleNamespace(reader=self.writer.reader, writer=self.writer, audit=self.writer.audit,
                               po=po, owner="dispatcher", save_records=lambda *_: None)

    def advance(self, runtime: Any, seconds: int) -> None:
        from ummanu.dispatch.po_cards import _episode_outcome
        start = datetime.fromisoformat(SINCE).timestamp()
        with mock.patch("ummanu.dispatch.po_cards.time.time", return_value=start + seconds):
            # These tests isolate the escalation writer; settled-turn transitions are exercised
            # through the full dispatcher and real SQL writer below and in test_sprint_attention.
            with mock.patch("ummanu.dispatch.po_cards._block", return_value={}):
                _episode_outcome(runtime, self.card, SimpleNamespace(attempt_id="attempt",
                    po_submission=SimpleNamespace(submit_request_id="submit-episode")), {}, {})

    def test_queued_deadline_is_2959_then_3000_and_restart_replay_deduplicates(self) -> None:
        runtime = self.runtime()
        self.advance(runtime, 1799)
        self.assertEqual(self.client.owner_events.kinds(), [])
        self.advance(runtime, 1800)
        [event] = self.client.owner_events.of_kind("po_card_escalated")
        self.assertEqual(event.event_class, "needs_owner")
        self.assertIn("30 minutes", event.text)
        self.assertTrue(self.client.owner_events.events()[0].held)
        with self.assertRaises(ReadRefused):
            self.client.owner_events.mark_read(event.id)
        self.advance(self.runtime(), 1900)
        self.assertEqual(len(self.client.owner_events.of_kind("po_card_escalated")), 1)

    def test_execution_failure_escalates_immediately_but_owner_interruption_does_not(self) -> None:
        self.advance(self.runtime("interrupted"), 1900)
        self.assertEqual(self.client.owner_events.kinds(), [])
        self.advance(self.runtime("completed"), 1900)
        self.assertEqual(self.client.owner_events.kinds(), [])
        self.advance(self.runtime("failed"), 1)
        [event] = self.client.owner_events.of_kind("po_card_escalated")
        self.assertIn("execution failed", event.text)

    def test_handover_replaces_escalation_and_answer_replaces_handover(self) -> None:
        self.advance(self.runtime(), 1800)
        escalation = self.client.owner_events.of_kind("po_card_escalated")[0]
        first = self.hand_over()
        self.assertFalse(self.client.owner_events.rows[escalation.id].unread)
        self.writer.record_owner_answer(role="po", actor="po", reference=REF,
            handover_event=first["event_id"], quotation="Approved.", request_id="answer")
        self.assertIsNone(waiting_owner(self.card))
        self.assertEqual(self.client.owner_events.snapshot()["human_waits"], [])

    def test_completion_between_clock_read_and_atomic_escalation_wins(self) -> None:
        runtime = self.runtime()
        escalate = self.writer.escalate_po_card
        def completed(**fields):
            self.card["state"] = "done"
            return escalate(**fields)
        with mock.patch.object(self.writer, "escalate_po_card", side_effect=completed):
            self.advance(runtime, 1800)
        self.assertEqual(self.client.owner_events.kinds(), [])


class CompletionClearsTheMarkTests(WriterFixture):
    """`task complete` takes the mark off inside the transition's own transaction (its `finish`)."""

    def setUp(self) -> None:
        super().setUp()
        self.events: dict[str, Any] = {}
        self.writer._typed_event = lambda request_id: self.events.get(request_id)  # type: ignore[method-assign]

        def transition(**fields: Any) -> Any:
            fields["finish"](None)
            self.card["state"] = fields["target"].value
            self.events[fields["request_id"]] = SimpleNamespace(
                ref=fields["reference"], reason=fields["reason"], source_state="in_progress"
            )
            return SimpleNamespace(event=SimpleNamespace(event_id="evt-done"))

        self.writer._transition_card = transition  # type: ignore[method-assign]

    def complete(self) -> dict[str, Any]:
        return self.writer.complete(
            role="po", actor="po", reference=REF, kind="decision", body=DECISION_BODY, request_id="complete-1"
        )

    def test_completion_clears_the_mark_with_the_done(self) -> None:
        self.hand_over()
        self.assertIsNotNone(waiting_owner(self.card))

        self.complete()

        self.assertEqual(self.card["state"], "done")
        self.assertIsNone(waiting_owner(self.card))
        self.assertFalse(set(MARK_KEYS) & set(self.card["extensions"]["extra"]))

    def test_an_unmarked_completion_writes_no_mark_fields(self) -> None:
        self.complete()
        [values] = [params["values"] for method, params in self.client.writes if method == "saveTaskMetadata"]
        self.assertFalse(set(MARK_KEYS) & set(values))

    def test_any_move_out_of_in_progress_takes_the_mark_off(self) -> None:
        self.hand_over()
        writer = self.writer
        writer._reset_transition_metadata(copy.deepcopy(self.card), source="in_progress", target="blocked")
        self.assertIsNone(waiting_owner(self.card))


class OwnerAnswerDispatchTests(HandedOverFixture):
    def test_lost_accepted_answer_submission_is_recovered_under_the_same_id(self) -> None:
        runtime, session = self.submitted_card()
        self.hand_over()
        self.owner_says("Approved.", "evt-owner-lost")
        self.assertEqual(self.tick(runtime)["action"], "po-owner-answer-submitted")
        self.settled(session, 3)
        stable_id = self.record().po_submission.owner_request_id
        with (mock.patch.object(runtime.po, "request", return_value=None),
              mock.patch.object(runtime.po, "queued", return_value=None),
              mock.patch.object(runtime.po, "submit", wraps=runtime.po.submit) as submit):
            self.assertEqual(self.tick(runtime)["action"], "po-owner-answer-submitted")
        self.assertEqual(submit.call_args.kwargs["request_id"], stable_id)
        self.assertEqual(len(FakePoStore(self.board).turns(session)), 3)
        self.assertEqual(self.tick(runtime)["action"], PO_BLOCKED_ACTION)
        self.assertIsNone(waiting_owner(self.cards.card))

    """The dispatcher waits on a handed-over card, and hands each owner answer to the PO once."""

    def test_a_settled_turn_with_the_card_handed_over_waits_instead_of_blocking(self) -> None:
        runtime, _session = self.submitted_card()
        before = runtime.reader.show(REF)  # the tick's read, taken before the PO's handover landed
        self.hand_over()

        outcome = advance_po_card(runtime, before, self.records, self.payload, self.attempt)

        self.assertEqual((outcome["status"], outcome["action"]), ("ok", "po-card-waiting-owner"))
        self.assertIn(REASON, outcome["reason"])
        self.assertEqual(self.cards.card["state"], "in_progress")
        self.assertIn(REF, self.records)
        self.assertEqual([event["kind"] for event in self.cards.log], ["claimed", HANDED_TO_OWNER])
        # And it keeps waiting, tick after tick, with nothing submitted.
        self.assertEqual(self.tick(runtime)["action"], "po-card-waiting-owner")
        self.assertEqual(self.record().po_submission.owner_request_id, "")

    def test_the_card_input_quotes_the_handover_command_with_its_derived_id(self) -> None:
        self.submitted_card()
        submission = self.record().po_submission
        self.assertEqual(submission.handover_request_id, attempt_request_id(self.attempt, "po-handover", REF))
        self.assertIn(handover_command(REF, submission.handover_request_id), submission.text)
        self.assertEqual(
            handover_command(REF, "h-1"),
            f"python3 -P -m ummanu task handover --ref {REF} --role po --to owner --reason-file <file> "
            "--request-id h-1",
        )
        self.assertIn("unless you handed it to the owner in this turn", submission.text)

    def test_the_same_settled_turn_without_the_mark_blocks(self) -> None:
        runtime, session = self.submitted_card()

        outcome = self.tick(runtime)

        self.assertEqual((outcome["status"], outcome["action"]), ("blocked", PO_BLOCKED_ACTION))
        self.assertEqual(outcome["reason"], f"PO turn {session}/2 ended completed without completing the card")

    def test_each_owner_comment_becomes_one_follow_up_to_the_same_session(self) -> None:
        runtime, session = self.submitted_card()
        self.hand_over()
        self.owner_says("Use the company card, the budget allows it.", "evt-owner-1")

        submitted = self.tick(runtime)

        expected_id = owner_answer_request_id(REF, "evt-owner-1")
        self.assertEqual((submitted["action"], submitted["po_request_id"]), ("po-owner-answer-submitted", expected_id))
        self.assertEqual(expected_id, f"dispatcher-po-owner-answer-{REF}-evt-owner-1")
        self.assertEqual(self.settled(session, 3).state, po_store.COMPLETED)
        request = FakePoStore(self.board).request(expected_id)
        self.assertEqual((request.session_id, request.seq), (session, 3))
        prompt = self.calls()[-1]["prompt"]
        submission = self.record().po_submission
        self.assertEqual(prompt, submission.owner_text)
        for expected in (REF, "decision", REASON, "Use the company card, the budget allows it.",
                         complete_command(REF, "decision", submission.complete_request_id)):
            self.assertIn(expected, prompt)
        self.assertNotIn("[handover:owner]", prompt)

        # Before the dispatcher reads the ended turn, the PO hands over a new question.
        # The current board epoch wins over the previous turn's missing response.
        self.hand_over(reason="Which monthly plan?")
        self.owner_says("And cap it at the monthly plan.", "evt-owner-2")
        self.assertEqual(self.tick(runtime)["action"], "po-owner-answer-submitted")
        self.settled(session, 4)
        prompt = self.calls()[-1]["prompt"]
        self.assertNotIn("Use the company card", prompt)
        self.assertIn("And cap it", prompt)
        self.assertEqual(len(FakePoStore(self.board).turns(session)), 4)

        # The PO completes the card: the record closes.
        self.cards.complete_as_po("decision", DECISION_BODY)
        self.assertEqual(self.tick(runtime)["action"], "po-card-closed")
        self.assertNotIn(REF, self.records)

    def test_a_repeat_with_a_lost_answer_or_a_lost_record_never_duplicates_the_follow_up(self) -> None:
        runtime, session = self.submitted_card()
        self.hand_over()
        self.owner_says("Approved.", "evt-owner-1")
        real_submit = runtime.po.submit
        seen: list[str] = []

        def loses_the_answer(**fields: Any) -> dict[str, Any]:
            seen.append(fields["request_id"])
            real_submit(**fields)
            if len(seen) == 1:
                from ummanu.po.client import OutcomeUnknown

                raise OutcomeUnknown("no answer from the PO service: connection reset")
            return {}

        with mock.patch.object(runtime.po, "submit", side_effect=loses_the_answer):
            self.assertEqual(self.tick(runtime)["action"], "po-service-unanswered")
            self.settled(session, 3)
            self.assertEqual(self.tick(runtime)["action"], PO_BLOCKED_ACTION)
        self.settled(session, 3)
        self.records.clear()  # the dispatcher's state file is lost with its record

        rebuilt = [self.tick(runtime)["action"], self.tick(runtime)["action"], self.tick(runtime)["action"]]

        self.assertEqual(rebuilt, ["po-card-closed"] * 3)
        self.assertEqual(seen, [owner_answer_request_id(REF, "evt-owner-1")])
        self.assertEqual(len(FakePoStore(self.board).turns(session)), 3)

    def test_completed_followup_after_lost_record_blocks_once_and_preserves_answer(self) -> None:
        runtime, session = self.submitted_card()
        self.hand_over()
        self.owner_says("Approved.", "evt-owner-completed")
        self.tick(runtime)
        self.settled(session, 3)
        answer = attention_record(self.cards.card, OWNER_ANSWER)
        self.records.clear()
        with mock.patch.object(runtime.po, "submit", wraps=runtime.po.submit) as submit:
            outcome = self.tick(runtime)
            self.assertEqual(outcome["action"], PO_BLOCKED_ACTION)
            self.assertIn(f"{session}/3 ended completed without completing", outcome["reason"])
            self.assertEqual([self.tick(runtime)["action"] for _ in range(3)], ["po-card-closed"] * 3)
            submit.assert_not_called()
        self.assertEqual(attention_record(self.cards.card, OWNER_ANSWER), answer)
        self.assertIsNone(waiting_owner(self.cards.card))
        self.assertIsNone(attention_record(self.cards.card, "owner_escalation"))
        self.assertEqual(sum(e["kind"] == "move" and e["to"] == "blocked" for e in self.cards.log), 1)
        self.assertEqual(len(FakePoStore(self.board).turns(session)), 3)

    def test_done_or_new_handover_during_followup_turn_read_wins(self) -> None:
        for change in ("done", "handover"):
            with self.subTest(change=change):
                # A separate card fixture for each race, including an actual completed follow-up.
                fixture = HandedOverFixture()
                fixture.setUp()
                try:
                    runtime, session = fixture.submitted_card()
                    fixture.hand_over()
                    fixture.owner_says("Approved.", "evt-owner-race")
                    fixture.tick(runtime)
                    fixture.settled(session, 3)
                    turn_read = runtime.po.turn
                    def raced(*args, change=change, fixture=fixture, turn_read=turn_read):
                        result = turn_read(*args)
                        if change == "done":
                            fixture.cards.complete_as_po("decision", DECISION_BODY)
                        else:
                            fixture.hand_over("Which monthly plan?")
                        return result
                    with mock.patch.object(runtime.po, "turn", side_effect=raced):
                        outcome = fixture.tick(runtime)
                    self.assertEqual(outcome["action"], "po-card-closed" if change == "done" else "po-card-waiting-owner")
                    self.assertFalse(any(e["kind"] == "move" and e["to"] == "blocked" for e in fixture.cards.log))
                finally:
                    fixture.doCleanups()

    def test_unavailable_submission_uses_durable_answer_deadline_and_stable_retry(self) -> None:
        from ummanu.po.client import OutcomeUnknown, ServiceUnavailable
        runtime, session = self.submitted_card()
        self.hand_over()
        self.owner_says("Approved.", "evt-owner-unavailable")
        answer = attention_record(self.cards.card, OWNER_ANSWER)
        start = datetime.fromisoformat(answer["at"]).timestamp()
        stable_id = owner_answer_request_id(REF, answer["event_id"])
        with mock.patch.object(runtime.po, "submit", side_effect=[ServiceUnavailable("offline"), ServiceUnavailable("offline"),
                                                          OutcomeUnknown("lost reply"), OutcomeUnknown("lost reply")]) as submit:
            for seconds in (1799, 1799, 1800, 1900):
                with mock.patch("ummanu.dispatch.po_cards.time.time", return_value=start + seconds):
                    self.assertEqual(self.tick(runtime)["action"], "po-service-unanswered")
                escalation = attention_record(self.cards.card, "owner_escalation")
                self.assertEqual(escalation is not None, seconds >= 1800)
                if escalation:
                    self.assertEqual(escalation["episode"], answer["event_id"])
                self.records.clear()
            self.assertEqual([c.kwargs["request_id"] for c in submit.call_args_list], [stable_id] * 4)
        self.assertEqual(self.tick(runtime)["action"], "po-owner-answer-submitted")
        self.settled(session, 3)
        self.assertEqual(self.tick(runtime)["action"], PO_BLOCKED_ACTION)
        self.assertEqual(attention_record(self.cards.card, OWNER_ANSWER), answer)

    def test_queued_and_failed_followup_share_the_durable_answer_episode(self) -> None:
        runtime, _session = self.submitted_card()
        self.hand_over()
        self.owner_says("Approved.", "evt-owner-queued")
        answer = attention_record(self.cards.card, OWNER_ANSWER)
        start = datetime.fromisoformat(answer["at"]).timestamp()
        with mock.patch.object(runtime.po, "request", return_value=SimpleNamespace(session_id="followup", seq=None)):
            for seconds in (1799, 1800):
                with mock.patch("ummanu.dispatch.po_cards.time.time", return_value=start + seconds):
                    self.assertEqual(self.tick(runtime)["action"], "po-card-owner-answered")
                self.assertEqual(attention_record(self.cards.card, "owner_escalation") is not None, seconds == 1800)
        self.cards.card["extensions"]["extra"].pop("owner_escalation", None)
        with mock.patch.object(runtime.po, "request", return_value=SimpleNamespace(session_id="followup", seq=4)), mock.patch.object(
                runtime.po, "turn", return_value=SimpleNamespace(session_id="followup", seq=4, state="failed")), mock.patch(
                "ummanu.dispatch.po_cards.time.time", return_value=start + 1):
            self.assertEqual(self.tick(runtime)["action"], "po-card-owner-answer-turn-ended")
        escalation = attention_record(self.cards.card, "owner_escalation")
        self.assertEqual(escalation["episode"], answer["event_id"])
        self.assertIn("execution failed", escalation["reason"])

    def test_permanent_submission_refusal_blocks_and_retains_settled_answer(self) -> None:
        from ummanu.po.client import ServiceRefused
        runtime, _session = self.submitted_card()
        self.hand_over()
        self.owner_says("Approved.", "evt-owner-refused")
        answer = attention_record(self.cards.card, OWNER_ANSWER)
        with mock.patch.object(runtime.po, "submit", side_effect=ServiceRefused("refused", "permanent refusal")):
            self.assertEqual(self.tick(runtime)["action"], PO_BLOCKED_ACTION)
        self.assertEqual(attention_record(self.cards.card, OWNER_ANSWER), answer)
        self.assertIsNone(attention_record(self.cards.card, "owner_escalation"))

    def test_completion_or_handover_during_permanent_submit_refusal_wins(self) -> None:
        from ummanu.po.client import ServiceRefused
        for change in ("done", "handover"):
            with self.subTest(change=change):
                fixture = HandedOverFixture()
                fixture.setUp()
                try:
                    runtime, _session = fixture.submitted_card()
                    fixture.hand_over()
                    fixture.owner_says("Approved.", "evt-owner-refusal-race")
                    def refused(*, change=change, fixture=fixture, **_fields):
                        if change == "done":
                            fixture.cards.complete_as_po("decision", DECISION_BODY)
                        else:
                            fixture.hand_over("Which monthly plan?")
                        raise ServiceRefused("refused", "permanent refusal")
                    with mock.patch.object(runtime.po, "submit", side_effect=refused):
                        outcome = fixture.tick(runtime)
                    self.assertEqual(outcome["action"], "po-card-closed" if change == "done" else "po-card-waiting-owner")
                    self.assertFalse(any(e["kind"] == "move" and e["to"] == "blocked" for e in fixture.cards.log))
                finally:
                    fixture.doCleanups()

    def test_closed_session_refuses_followup_through_the_existing_blocked_route(self) -> None:
        from ummanu.po.store import SessionClosed
        runtime, _session = self.submitted_card()
        self.hand_over()
        self.owner_says("Approved.", "evt-owner-closed-session")
        answer = attention_record(self.cards.card, OWNER_ANSWER)
        with mock.patch.object(runtime.po, "submit", side_effect=SessionClosed("closed session")):
            outcome = self.tick(runtime)
        self.assertEqual(outcome["action"], PO_BLOCKED_ACTION)
        self.assertIn("closed session", outcome["reason"])
        self.assertEqual(attention_record(self.cards.card, OWNER_ANSWER), answer)
        self.assertIsNone(attention_record(self.cards.card, "owner_escalation"))

    def test_conflicting_retry_with_unavailable_store_remains_degraded(self) -> None:
        from ummanu.po.store import PoStoreError, RequestConflict
        runtime, _session = self.submitted_card()
        self.hand_over()
        self.owner_says("Approved.", "evt-owner-conflict")
        with mock.patch.object(runtime.po, "request", side_effect=[None, PoStoreError("unavailable")]), mock.patch.object(
                runtime.po, "submit", side_effect=RequestConflict("conflicting retry")):
            outcome = self.tick(runtime)
        self.assertEqual((outcome["status"], outcome["action"]), ("degraded", "po-store-unanswered"))
        self.assertEqual(self.cards.card["state"], "in_progress")
        self.assertIsNone(attention_record(self.cards.card, "owner_escalation"))

    def test_interrupted_followup_and_unavailable_store_do_not_claim_execution_failure(self) -> None:
        from ummanu.po.store import PoStoreError
        runtime, session = self.submitted_card()
        self.hand_over()
        self.owner_says("Approved.", "evt-owner-stop")
        self.tick(runtime)
        self.settled(session, 3)
        answer = attention_record(self.cards.card, OWNER_ANSWER)
        start = datetime.fromisoformat(answer["at"]).timestamp()
        with mock.patch.object(runtime.po, "turn", return_value=SimpleNamespace(state="interrupted")), mock.patch(
                "ummanu.dispatch.po_cards.time.time", return_value=start + 7200):
            self.assertEqual(self.tick(runtime)["action"], "po-card-owner-answer-turn-ended")
        with mock.patch.object(runtime.po, "turn", side_effect=PoStoreError("unavailable")):
            outcome = self.tick(runtime)
            self.assertEqual((outcome["status"], outcome["action"]), ("degraded", "po-store-unanswered"))
        self.assertEqual(self.cards.card["state"], "in_progress")
        self.assertIsNone(attention_record(self.cards.card, "owner_escalation"))

    def test_comments_before_the_handover_and_from_other_roles_are_not_the_owners_answer(self) -> None:
        comments = [
            {"marker": "owner", "body": "[owner]\nAn old remark."},
            {"marker": "po", "body": "[po]\n" + render_handover_comment(REASON)},
            {"marker": "observer", "body": "[observer]\nNoted."},
            {"marker": "owner", "body": "[owner]\nThe answer.", "created_at": SINCE},
        ]
        self.assertEqual(owner_comments_since_handover(comments), [{"created_at": SINCE, "body": "The answer."}])
        events = [
            {"kind": "commented", "event_id": "e1", "payload": {"marker": "owner"}},
            {"kind": HANDED_TO_OWNER, "event_id": "e2"},
            {"kind": "commented", "event_id": "e3", "payload": {"marker": "po"}},
            {"kind": "commented", "event_id": "e4", "payload": {"marker": "owner"}},
        ]
        self.assertEqual(owner_answer_event_ids(events), ["e4"])


class SetAsideInputTests(DispatcherFixture):
    """5a: an input the PO service set aside in `po-queue/refused/` Blocks the card, with its reason."""

    def test_a_set_aside_input_blocks_the_card_with_the_services_reason(self) -> None:
        self.start()
        runtime = self.runtime(card())
        queue = PoQueue(self.data)

        class SetsItAside(ServicePoChannel):
            def submit(self, **fields: Any) -> dict[str, Any]:
                item = queue.put(source="dispatcher", **{k: fields[k] for k in ("session_id", "text", "request_id")})
                queue.refuse(item, "PO session s-1 is closed; open a new session to continue")
                return {"session_id": fields["session_id"], "queued": True, "seq": None}

        channel = SetsItAside(self.data, None)
        channel._store = FakePoStore(self.board)
        channel._client = runtime.po._service()
        runtime.po = channel

        submitted = self.claim(runtime)
        blocked = self.tick(runtime)

        self.assertEqual(submitted["action"], "po-card-submitted")
        self.assertEqual((blocked["status"], blocked["action"]), ("blocked", PO_BLOCKED_ACTION))
        self.assertEqual(
            blocked["reason"],
            "the PO service set the card's input aside and will not run it: "
            "PO session s-1 is closed; open a new session to continue",
        )
        self.assertEqual(self.cards.card["state"], "blocked")


class SetAsideOwnerAnswerTests(HandedOverFixture):
    """The same for the follow-up: an owner answer the service set aside Blocks the card, with its reason."""

    def test_a_set_aside_follow_up_blocks_the_card(self) -> None:
        runtime, session = self.submitted_card()
        self.hand_over()
        self.owner_says("Approved.", "evt-owner-1")
        self.assertEqual(self.tick(runtime)["action"], "po-owner-answer-submitted")
        self.settled(session, 3)
        request_id = owner_answer_request_id(REF, "evt-owner-1")
        with (
            mock.patch.object(runtime.po, "request", return_value=None),
            mock.patch.object(runtime.po, "refused", return_value={"reason": "PO session closed"}) as refused,
        ):
            blocked = self.tick(runtime)
        refused.assert_called_once_with(request_id)
        self.assertEqual((blocked["status"], blocked["action"]), ("blocked", PO_BLOCKED_ACTION))
        self.assertEqual(
            blocked["reason"], "the PO service set the owner's answer aside and will not run it: PO session closed"
        )


class RebuiltRecordTests(DispatcherFixture):
    """5b: a rebuilt record whose input is still queued for the same session counts it as submitted."""

    def test_a_rebuilt_record_with_its_input_still_queued_is_submitted_not_blocked(self) -> None:
        service = self.start()
        session = self.session(service)
        self.po_sprints.records[SPRINT] = SprintRecord(SPRINT, "open", session)
        service.submit(session_id=session, text="GATE the owner's long question", request_id="owner-1")
        self.reached_gate(session, 1)
        runtime = self.runtime(card(), comments=["The sprint opened."])
        self.claim(runtime)
        submit_id = self.record().po_submission.submit_request_id
        self.assertIsNotNone(PoQueue(self.data).find(submit_id))

        self.records.clear()
        runtime.sprints.comments.append("A comment that landed after the submit.")
        rebuilt = self.tick(runtime)

        self.assertEqual((rebuilt["status"], rebuilt["action"]), ("ok", "po-card-queued"))
        self.assertEqual(self.cards.card["state"], "in_progress")
        self.assertEqual([item.request_id for item in PoQueue(self.data).pending(session)], [submit_id])
        self.assertEqual(self.tick(runtime)["action"], "po-card-queued")
        self.gate.touch()
        self.settled(session, 2)


class SprintWaitingTests(unittest.TestCase):
    """4: a sprint whose current card is an open decision/operation card reads `waiting`, with a pointer."""

    def waiting(self, current: dict[str, Any] | None, records: dict[str, Any] | None = None) -> dict[str, Any]:
        now = 1_000_000.0
        row = {"ref": SPRINT, "status": "open", "current_task": REF if current is not None else None}
        read = SourceSet(
            [
                Reading(SOURCE_SPRINTS, sources.available(now), (row, row)),
                Reading(SOURCE_CARDS, sources.available(now), {SPRINT: [current] if current else []}),
                Reading(SOURCE_LIVENESS, sources.available(now), _Production({"records": records or {}}, {})),
            ]
        )
        return render(SECTIONS.waiting(read))

    def test_with_the_po_and_handed_to_the_owner(self) -> None:
        # The dispatcher holds its record for the card: a head-less card is still not `working`.
        records = {REF: {"state": "po_submitted"}}
        for kind in ("decision", "operation"):
            with self.subTest(kind=kind):
                with_po = self.waiting(card(kind, state="in_progress"), records)
                self.assertEqual(
                    (with_po["state"], with_po["reason"], with_po["card"]),
                    (WAITING_WAITING, f"{REF} ({kind}) is with the PO", REF),
                )
                handed = card(kind, state="in_progress")
                handed["extensions"] = {"extra": mark_values(SINCE, REASON, "po")}
                with_owner = self.waiting(handed, records)
                self.assertEqual(
                    (with_owner["state"], with_owner["reason"], with_owner["card"]),
                    (WAITING_WAITING, f"{REF} ({kind}) is handed to the owner: {REASON}", REF),
                )
                self.assertEqual(with_owner["source"]["name"], SOURCE_CARDS)

    def test_other_cards_and_columns_keep_their_reading(self) -> None:
        blocked = self.waiting(card(state="blocked"), {})
        self.assertEqual((blocked["state"], blocked["card"]), (WAITING_BLOCKED, REF))
        code = self.waiting({**card(state="in_progress"), "type": "code"}, {REF: {"state": "claimed"}})
        self.assertEqual((code["state"], code["card"]), ("working", REF))
        nothing = self.waiting(None)
        self.assertEqual((nothing["state"], nothing["card"]), (WAITING_WAITING, None))

    def test_the_dashboard_row_says_it_with_the_card(self) -> None:
        item = {
            "ref": SPRINT,
            "goal": "g",
            "waiting": {"state": "waiting", "reason": f"{REF} (decision) is handed to the owner: {REASON}", "card": REF},
        }
        html = pages._compact_sprint_card(item)
        self.assertIn("is handed to the owner: Pay the relay provider", html)
        self.assertIn(f'href="/tasks/{REF}"', html)


if __name__ == "__main__":
    unittest.main()
