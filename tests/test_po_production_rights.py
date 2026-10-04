"""Production rights: an operation card names its production, and the PO decides what its sprint does not allow.

Unit-level (secretary-1764, secretary-1769), on the fakes of cards 1 to 4. The card side runs
`TaskWriter.create` over a mock client, every refusal decided before the board is read. The rule runs in
a real `PoService` over its socket, reached by the real `advance_po_card`, with the in-memory PO store,
the fake CLI, the fake sprint port and the one-card board of `tests.po_card_fakes`. The rule refuses
nothing: every operation becomes a PO turn whose input carries the service's rights section, and the
three paths are an allowed production, one the PO allows with `sprint allow-production`, and one the PO
hands to the owner. `SprintWriter.allow_production` runs over a mock board client here; its PostgreSQL
path and audit event are covered by the integration-board suite (`tests/test_sprint_po_channel_backend.py`),
and so are create and the stored field (`tests/test_tasks.py`).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tests.po_card_fakes import OPERATION_BODY, REF, SPRINT, DispatcherFixture, card
from tests.po_fake_store import FakePoStore
from tests.po_handover_fakes import HandedOverFixture
from ummanu import sprint_commands
from ummanu.board.owner_handover import HANDED_TO_OWNER, waiting_owner
from ummanu.board.production_rights import (
    CARD_INPUT,
    OWNER_ANSWER_INPUT,
    RIGHTS_HEADING,
    allow_production_command,
    card_facts,
    facts_problem,
    rights_line,
    rights_note,
    touches_production,
)
from ummanu.cli import main
from ummanu.dispatch.po_cards import (
    ServicePoChannel,
    complete_command,
    handover_command,
    owner_answer_request_id,
)
from ummanu.dispatch.state import DispatcherRecord
from ummanu.po import store as po_store
from ummanu.po.client import OutcomeUnknown
from ummanu.po.queue import PoQueue
from ummanu.po.sprints import BoardSprintSessions, SprintRecord
from ummanu.sprints import ALLOWED_PRODUCTIONS_FIELD, SprintWriter
from ummanu.tasks import TaskError, TaskReader, TaskWriter, admit_role
from ummanu.web import pages
from ummanu.webproto.reads import _card_value

NOT_ALLOWED = f"touches production relay; sprint {SPRINT} allows []"
DECIDE = "This is not a refusal: decide it under the owner's standing rule."
ALLOWS_IT = "The sprint allows it: run the operation with no further confirmation"


class CreateValidationTests(unittest.TestCase):
    """`--touches-production`: required on an operation, refused elsewhere, an unknown project refused."""

    def setUp(self) -> None:
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        registry = Path(self.tmp) / "projects"
        registry.mkdir()
        (registry / "ummanu.yaml").write_text("id: ummanu\n", encoding="utf-8")
        self.client = mock.Mock(instance_dir=self.tmp)
        self.writer = TaskWriter(self.client, data_dir=self.tmp)

    def create(self, kind: str, **fields: Any) -> dict:
        return self.writer.create(
            role="observer", actor="observer", project="ummanu", task_type=kind, title="T", sprint=SPRINT,
            **fields,
        )

    def test_each_refusal_is_decided_before_the_board_is_read(self) -> None:
        for kind, fields, reason in (
            ("operation", {}, "an operation card needs --touches-production <project>|none"),
            ("operation", {"touches_production": "relay"}, "unknown registered project: relay"),
            ("operation", {"touches_production": "../relay"}, "unknown registered project: ../relay"),
            ("decision", {"touches_production": "none"}, "a decision card takes none"),
            ("code", {"touches_production": "ummanu"}, "a code card takes none"),
            ("research", {"touches_production": "none"}, "a research card takes none"),
        ):
            with self.subTest(kind=kind, fields=fields), self.assertRaisesRegex(TaskError, reason) as raised:
                self.create(kind, **fields)
            self.assertEqual(raised.exception.code, "validation")
        self.assertEqual(self.client.mock_calls, [])

    def test_without_a_project_registry_a_named_production_is_refused(self) -> None:
        (Path(self.tmp) / "projects" / "ummanu.yaml").unlink()
        (Path(self.tmp) / "projects").rmdir()
        with self.assertRaisesRegex(TaskError, "project registry is unavailable"):
            self.create("operation", touches_production="ummanu")
        self.assertEqual(self.client.mock_calls, [])

    def test_a_registered_project_and_none_pass_to_the_board(self) -> None:
        for value in ("ummanu", "none"):
            # Past the production check the create reads the sprint: the refusal is not ours.
            with (
                self.subTest(value=value),
                mock.patch("ummanu.sprints.SprintReader.show", side_effect=RuntimeError("the board")),
                self.assertRaisesRegex(RuntimeError, "the board"),
            ):
                self.create("operation", touches_production=value)

    def test_the_cli_passes_the_flag_through(self) -> None:
        writer = mock.Mock()
        writer.return_value.create.return_value = {"action": "created"}
        with (
            mock.patch("ummanu.task_commands.TaskWriter", writer),
            mock.patch("ummanu.task_commands.card_client"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            code = main(
                ["task", "create", "--role", "observer", "--instance", self.tmp, "--data-dir", self.tmp,
                 "--project", "ummanu", "--type", "operation", "--title", "T", "--sprint", SPRINT,
                 "--touches-production", "ummanu"]
            )
        self.assertEqual(code, 0)
        self.assertEqual(writer.return_value.create.call_args.kwargs["touches_production"], "ummanu")

    def test_the_value_is_read_from_the_bag_and_shown_by_task_show_and_the_card_page(self) -> None:
        reader = TaskReader(mock.Mock())
        row = {"id": 1900, "reference": REF, "title": "Rotate the key", "column_id": 3, "is_active": 1}
        meta = {"task_type": "operation", "sprint_ref": SPRINT, "touches_production": "ummanu"}

        shown = reader._normalize(row, {3: "Ready"}, {}, meta, comments=[])
        plain = reader._normalize(row, {3: "Ready"}, {}, {"task_type": "decision"}, comments=[])

        self.assertEqual(shown["touches_production"], "ummanu")
        self.assertEqual(shown["extensions"]["extra"]["touches_production"], "ummanu")
        self.assertNotIn("touches_production", plain)
        value = _card_value(shown)
        self.assertEqual(value["touches_production"], "ummanu")
        self.assertIsNone(_card_value(plain)["touches_production"])
        html = pages.task(
            {"ref": REF, "card": {"source": None, "value": value}, "project": {}, "events": {}, "agents": {}},
            runs={},
        )
        self.assertIn("touches production", html)

    def test_a_malformed_value_is_no_value(self) -> None:
        for value in ("", "  ", "../etc", 7, None):
            with self.subTest(value=value):
                self.assertIsNone(touches_production({"extensions": {"extra": {"touches_production": value}}}))
        self.assertIsNone(touches_production({}))




class CardFactsTests(unittest.TestCase):
    def test_what_is_missing_or_malformed_is_named(self) -> None:
        good = card_facts(card_ref=REF, kind="operation", touches_production="relay", sprint_ref=SPRINT)
        self.assertEqual(facts_problem(good), "")
        self.assertEqual(
            card_facts(card_ref=REF, kind="decision", touches_production="relay", sprint_ref=SPRINT)[
                "touches_production"
            ],
            None,
        )
        for card_value, problem in (
            (None, "carries no card facts"),
            ({**good, "card_ref": ""}, "name no card_ref"),
            ({**good, "sprint_ref": None}, "name no sprint_ref"),
            ({**good, "kind": "code"}, "kind 'code'"),
            ({**good, "input": "other"}, "input 'other'"),
            ({**good, "touches_production": None}, f"operation card {REF} names no production"),
            ({**good, "kind": "decision"}, "a decision card names no production"),
        ):
            with self.subTest(card=card_value):
                self.assertIn(problem, facts_problem(card_value))
        # The owner's answer is not checked again, so it needs no production.
        self.assertEqual(facts_problem({**good, "touches_production": None, "input": OWNER_ANSWER_INPUT}), "")

    def test_the_rights_line_and_the_note(self) -> None:
        self.assertEqual(rights_line("relay", SPRINT, ()), NOT_ALLOWED)
        self.assertEqual(
            rights_line("relay", SPRINT, ("ummanu", "site")),
            f"touches production relay; sprint {SPRINT} allows [ummanu, site]",
        )
        allowed = rights_note("relay", SPRINT, ["relay"], request_id="s-1:allow-production")
        self.assertIn(f"touches production relay; sprint {SPRINT} allows [relay]", allowed)
        self.assertIn(ALLOWS_IT, allowed)
        self.assertNotIn("allow-production", allowed)
        none = rights_note("none", SPRINT, None, request_id="s-1:allow-production")
        self.assertIn("touches production none: the sprint allows it", none)
        decide = rights_note("relay", SPRINT, [], request_id="s-1:allow-production")
        for expected in (
            RIGHTS_HEADING,
            NOT_ALLOWED,
            DECIDE,
            "Production of ummanu is allowed by default, because it is the development server",
            "any other production only as agreed at sprint planning",
            "task handover --to owner",
        ):
            self.assertIn(expected, decide)
        self.assertIn(allow_production_command(SPRINT, "relay", "s-1:allow-production"), decide)
        self.assertEqual(
            allow_production_command(SPRINT, "relay", "s-1:allow-production"),
            f"python3 -P -m ummanu sprint allow-production --ref {SPRINT} --role po --project relay "
            "--reason '<text>' --request-id s-1:allow-production",
        )


REASON = "ummanu is the development server; relay was agreed at planning"


class RuleFixture(DispatcherFixture):
    def allow(self, *productions: str, session: str | None = None) -> None:
        self.po_sprints.records[SPRINT] = SprintRecord(SPRINT, "open", session, productions)

    def operation(self, production: str | None, **fields: Any) -> dict[str, Any]:
        return card("operation", production=production, description="Rotate the relay key.", **fields)

    def owner_says(self, text: str, event_id: str) -> None:
        HandedOverFixture.owner_says(self, text, event_id)

    def handovers(self) -> list[dict[str, Any]]:
        return [event for event in self.cards.log if event["kind"] == HANDED_TO_OWNER]

    def allow_as_po(self, project: str, request_id: str, *, actor: str = "po", role: str = "po") -> tuple[int, str]:
        """`sprint allow-production` as a PO turn runs it: the actor from `BOARD_ACTOR`, the fake sprint writer."""
        err = io.StringIO()
        with (
            mock.patch.object(sprint_commands, "SprintWriter", lambda *_args, **_fields: self.po_sprints),
            mock.patch.object(sprint_commands, "board_client"),
            mock.patch.dict(os.environ, {"BOARD_ACTOR": actor}),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(err),
        ):
            code = main(
                ["sprint", "allow-production", "--ref", SPRINT, "--role", role, "--project", project,
                 "--reason", REASON, "--request-id", request_id, "--instance", str(self.root),
                 "--data-dir", str(self.data)]
            )
        return code, err.getvalue()

    def card_turn(self, runtime: Any) -> tuple[Any, str]:
        """Claim the card and let its turn settle: the submission and the prompt the PO read."""
        self.assertEqual(self.claim(runtime)["action"], "po-card-submitted")
        submission = self.record().po_submission
        self.settled(submission.session_id, 2)
        prompt = self.calls()[-1]["prompt"]
        self.assertTrue(prompt.startswith(submission.text.rstrip()), "the card's text comes first")
        return submission, prompt


class AllowedTests(RuleFixture):
    """Path (a): the sprint allows it, so it is queued and runs with no confirmation and no handover."""

    def test_an_allowed_production_is_queued_and_runs_and_the_facts_bind_the_id(self) -> None:
        self.start()
        self.allow("relay")
        runtime = self.runtime(self.operation("relay"))

        submission, prompt = self.card_turn(runtime)

        facts = {"card_ref": REF, "kind": "operation", "touches_production": "relay", "sprint_ref": SPRINT,
                 "input": CARD_INPUT}
        self.assertEqual(submission.card, facts)
        session = submission.session_id
        self.assertEqual(self.settled(session, 2).state, po_store.COMPLETED)
        request = FakePoStore(self.board).request(submission.submit_request_id)
        # The id binds the dispatcher's text and facts, not the service's note after it.
        self.assertEqual(request.fingerprint, po_store.send_fingerprint(session, submission.text, facts))
        self.assertNotEqual(request.fingerprint, po_store.send_fingerprint(session, submission.text))
        self.assertIn("Touches production: relay.", prompt)
        self.assertIn(f"touches production relay; sprint {SPRINT} allows [relay]", prompt)
        self.assertIn(ALLOWS_IT, prompt)
        self.assertNotIn("allow-production", prompt)
        # The feed shows the PO's input as the PO read it.
        [entry] = [e for e in FakePoStore(self.board).feed(session) if e.turn_seq == 2 and e.role == po_store.OWNER]
        self.assertEqual(entry.text, prompt)
        self.assertEqual(self.handovers(), [])
        self.cards.complete_as_po("operation", OPERATION_BODY)
        self.assertEqual(self.tick(runtime)["action"], "po-card-closed")

    def test_none_is_queued_whatever_the_sprint_allows(self) -> None:
        self.start()
        runtime = self.runtime(self.operation("none"))

        submission, prompt = self.card_turn(runtime)

        self.assertEqual(submission.card["touches_production"], "none")
        self.assertIn("Touches production: none. Touch no production", prompt)
        self.assertIn("touches production none: the sprint allows it", prompt)
        self.assertEqual(self.handovers(), [])

    def test_a_decision_carries_the_same_facts_with_no_production_and_no_note(self) -> None:
        self.start()
        runtime = self.runtime(card())

        submission, prompt = self.card_turn(runtime)

        self.assertEqual(
            submission.card,
            {"card_ref": REF, "kind": "decision", "touches_production": None, "sprint_ref": SPRINT,
             "input": CARD_INPUT},
        )
        self.assertNotIn(RIGHTS_HEADING, prompt)
        self.assertEqual(prompt, submission.text)
        self.assertEqual(self.handovers(), [])

    def test_the_facts_survive_the_dispatcher_record(self) -> None:
        self.start()
        runtime = self.runtime(self.operation("none"))
        self.claim(runtime)
        record = self.record()
        loaded = DispatcherRecord.from_json(record.to_json())
        self.assertEqual(loaded.po_submission, record.po_submission)
        # A record written while the service could still hand a card over loads without that field.
        self.assertNotIn("handed_over", record.to_json()["po_submission"])
        legacy = record.to_json()
        legacy["po_submission"]["handed_over"] = True
        self.assertEqual(DispatcherRecord.from_json(legacy).po_submission, record.po_submission)


class PoDecidesTests(RuleFixture):
    """A production the sprint does not allow is a normal PO turn: never refused, never handed over by the service."""

    def test_b_the_po_allows_it_with_the_command_and_runs_it(self) -> None:
        self.start()
        runtime = self.runtime(self.operation("relay"))

        submission, prompt = self.card_turn(runtime)

        allow_id = f"{submission.submit_request_id}:allow-production"
        for expected in (
            "Touches production: relay. Whether the sprint allows it is in the PO service's production rights",
            RIGHTS_HEADING,
            NOT_ALLOWED,
            DECIDE,
            "Production of ummanu is allowed by default",
            allow_production_command(SPRINT, "relay", allow_id),
            "task handover --to owner",
            handover_command(REF, submission.handover_request_id),
        ):
            self.assertIn(expected, prompt)
        self.assertIsNone(waiting_owner(self.cards.card))
        self.assertEqual(self.handovers(), [])
        self.assertEqual(self.cards.card["state"], "in_progress")

        # The PO, inside that turn: records the allowance, runs the operation, completes the card.
        self.assertEqual(self.allow_as_po("relay", allow_id), (0, ""))
        self.cards.complete_as_po("operation", OPERATION_BODY)

        self.assertEqual(self.tick(runtime)["action"], "po-card-closed")
        self.assertEqual(self.po_sprints.records[SPRINT].allowed_productions, ("relay",))
        [event] = self.po_sprints.events
        self.assertEqual(
            (event["kind"], event["request_id"], event["actor"], event["payload"]),
            ("production_allowed", allow_id, {"role": "po", "id": "po"}, {"project": "relay", "reason": REASON}),
        )
        self.assertEqual(self.handovers(), [])
        # A repeat of the command writes nothing new.
        self.assertEqual(self.allow_as_po("relay", allow_id), (0, ""))
        self.assertEqual(len(self.po_sprints.events), 1)

    def test_c_the_po_hands_it_over_the_owner_answers_and_the_po_completes(self) -> None:
        self.start()
        runtime = self.runtime(self.operation("relay"))

        submission, prompt = self.card_turn(runtime)
        self.assertIn(DECIDE, prompt)
        # The PO may not decide it, so it hands the card over inside its turn.
        why = "relay is not ummanu and was not agreed at planning: may I rotate its key?"
        self.cards.hand_over_as_po(why, request_id=submission.handover_request_id)

        self.assertEqual(self.tick(runtime)["action"], "po-card-waiting-owner")
        self.owner_says("Go ahead on relay, once.", "evt-owner-1")
        answered = self.tick(runtime)

        request_id = owner_answer_request_id(REF, "evt-owner-1")
        self.assertEqual((answered["action"], answered["po_request_id"]), ("po-owner-answer-submitted", request_id))
        session = submission.session_id
        self.assertEqual(self.settled(session, 3).state, po_store.COMPLETED)
        submission = self.record().po_submission
        request = FakePoStore(self.board).request(request_id)
        facts = {**submission.card, "input": OWNER_ANSWER_INPUT}
        self.assertEqual(request.fingerprint, po_store.send_fingerprint(session, submission.owner_text, facts))
        answer = self.calls()[-1]["prompt"]
        # The answer is delivered as quoted, without changing the sprint's recorded authority.
        self.assertEqual(answer, submission.owner_text)
        self.assertNotIn(RIGHTS_HEADING, answer)
        self.assertNotIn("checked the sprint allows it", answer)
        for expected in (
            "which you handed to the owner",
            "## Why you handed it to the owner",
            why,
            "Go ahead on relay, once.",
            "Touches production: relay. Follow the quoted owner answer",
            "Recording this answer applies no grant",
            complete_command(REF, "operation", submission.complete_request_id),
        ):
            self.assertIn(expected, answer)
        self.assertEqual(self.po_sprints.records[SPRINT].allowed_productions, ())
        self.assertEqual([event["actor"] for event in self.handovers()], ["po"])

        self.cards.complete_as_po("operation", OPERATION_BODY)
        self.assertIsNone(waiting_owner(self.cards.card))
        self.assertEqual(self.tick(runtime)["action"], "po-card-closed")
        self.assertNotIn(REF, self.records)

    def test_a_sprint_opened_before_the_field_sends_its_operations_to_the_po(self) -> None:
        """An empty `allowed_productions` (every sprint opened before 0016) is a PO turn, never the owner."""
        self.start()
        self.assertEqual(self.po_sprints.records[SPRINT].allowed_productions, ())
        runtime = self.runtime(self.operation("ummanu"))

        submission, prompt = self.card_turn(runtime)

        self.assertIn(f"touches production ummanu; sprint {SPRINT} allows []", prompt)
        self.assertIn("Production of ummanu is allowed by default", prompt)
        self.assertEqual(self.settled(submission.session_id, 2).state, po_store.COMPLETED)
        self.assertIsNone(waiting_owner(self.cards.card))
        self.assertEqual(self.handovers(), [])
        self.assertEqual([event["kind"] for event in self.cards.log], ["claimed"])

    def test_a_repeat_of_the_same_submit_queues_once_whatever_the_sprint_allows_by_then(self) -> None:
        self.start()
        runtime = self.runtime(self.operation("relay"))
        real = runtime.po
        answers: list[dict[str, Any]] = []
        sprints = self.po_sprints
        allow = self.allow

        class LosesTheFirstAnswer:
            def submit(self, **fields: Any) -> dict[str, Any]:
                answers.append(real.submit(**fields))
                if len(answers) == 1:
                    # The sprint changes before the repeat: the note is the service's, not the id's.
                    allow("relay", session=sprints.records[SPRINT].po_session)
                    raise OutcomeUnknown("no answer from the PO service: connection reset")
                return answers[-1]

            def __getattr__(self, name: str) -> Any:
                return getattr(real, name)

        runtime.po = LosesTheFirstAnswer()

        first = self.claim(runtime)
        second = self.tick(runtime)
        self.records.clear()  # and the dispatcher's record is lost: rebuilt from the claim, same ids
        third = self.tick(runtime)

        self.assertEqual(
            [first["action"], second["action"], third["action"]],
            ["po-service-unanswered", "po-card-submitted", "po-card-submitted"],
        )
        self.assertEqual([answer["repeated"] for answer in answers], [False, True, True])
        submission = self.record().po_submission
        self.settled(submission.session_id, 2)
        self.assertEqual(len(FakePoStore(self.board).turns(submission.session_id)), 2)
        # The one turn carries the note it was queued with.
        self.assertIn(NOT_ALLOWED, self.calls()[-1]["prompt"])
        self.assertEqual(self.handovers(), [])


class NeverExecutedTests(RuleFixture):
    def test_a_sprint_that_cannot_be_read_is_never_executed(self) -> None:
        self.start()
        self.allow("relay")
        runtime = self.runtime(self.operation("relay"))
        sprints = self.po_sprints

        class FailsTheSprintReadAtSubmit(ServicePoChannel):
            def submit(self, **fields: Any) -> dict[str, Any]:
                sprints.fail["sprint"] = 1
                return super().submit(**fields)

        channel = FailsTheSprintReadAtSubmit(self.data, None)
        channel._store = FakePoStore(self.board)
        runtime.po = channel

        outcomes = [self.claim(runtime), self.tick(runtime)]

        for outcome in outcomes:
            self.assertEqual((outcome["status"], outcome["action"]), ("degraded", "po-service-unanswered"))
        self.assertIn(f"sprint {SPRINT} cannot be read for its allowed productions", outcomes[-1]["reason"])
        submission = self.record().po_submission
        self.assertFalse(submission.submitted)
        self.assertEqual(self.cards.card["state"], "in_progress")
        self.assertIsNone(PoQueue(self.data).find(submission.submit_request_id))
        self.assertIsNone(FakePoStore(self.board).request(submission.submit_request_id))
        self.assertEqual(self.handovers(), [])
        # Once the sprint reads again, the same request id is evaluated and runs.
        runtime.po = ServicePoChannel(self.data, None)
        runtime.po._store = FakePoStore(self.board)
        self.assertEqual(self.tick(runtime)["action"], "po-card-submitted")
        self.settled(submission.session_id, 2)

    def test_an_operation_that_names_no_production_is_never_executed(self) -> None:
        self.start()
        self.allow("relay")
        runtime = self.runtime(self.operation(None))

        outcomes = [self.claim(runtime), self.tick(runtime)]

        for outcome in outcomes:
            self.assertEqual(outcome["action"], "po-service-unanswered")
        self.assertIn(f"operation card {REF} names no production it touches", outcomes[-1]["reason"])
        submission = self.record().po_submission
        self.assertIsNone(PoQueue(self.data).find(submission.submit_request_id))
        self.assertIsNone(FakePoStore(self.board).request(submission.submit_request_id))
        self.assertEqual(self.handovers(), [])

    def test_card_facts_come_from_the_dispatcher_only_and_it_always_sends_them(self) -> None:
        service = self.start(listen=False)
        session = self.session(service)
        text = "Rotate the relay key."
        facts = card_facts(card_ref=REF, kind="operation", touches_production="none", sprint_ref=SPRINT)
        web = service.handle(
            {"op": "submit", "session_id": session, "text": text, "request_id": "w-1", "source": "web", "card": facts}
        )
        bare = service.handle(
            {"op": "submit", "session_id": session, "text": text, "request_id": "d-1", "source": "dispatcher"}
        )
        self.assertEqual((web["error"]["code"], web["error"].get("nothing_written")), ("validation", True))
        self.assertEqual(bare["error"]["code"], "unavailable")
        self.assertIn("carries no card facts", bare["error"]["message"])
        self.assertEqual(PoQueue(self.data).pending(), [])

    def test_the_service_answers_a_not_allowed_production_as_queued_never_handed_over(self) -> None:
        service = self.start(listen=False)
        session = self.session(service)
        facts = card_facts(card_ref=REF, kind="operation", touches_production="relay", sprint_ref=SPRINT)
        answer = service.handle(
            {"op": "submit", "session_id": session, "text": "Rotate it.", "request_id": "d-2",
             "source": "dispatcher", "card": facts}
        )
        self.assertNotIn("error", answer)
        self.assertNotIn("handed_over", answer)
        self.settled(session, 1)
        [entry] = [e for e in FakePoStore(self.board).feed(session) if e.role == po_store.OWNER]
        self.assertTrue(entry.text.startswith("Rotate it.\n\n" + RIGHTS_HEADING))
        self.assertIn(NOT_ALLOWED, entry.text)


class AllowProductionWriterTests(unittest.TestCase):
    """`SprintWriter.allow_production` over a mock board: the rules decided before and around its one write."""

    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.instance = self.root / "instance"
        (self.instance / "projects").mkdir(parents=True)
        for project in ("ummanu", "relay"):
            (self.instance / "projects" / f"{project}.yaml").write_text(f"id: {project}\n", encoding="utf-8")
        self.client = mock.MagicMock()
        self.writer = SprintWriter(self.client, data_dir=self.root / "data", instance=self.instance)
        self.sprint = {"id": "sprint_postgres_1", "ref": SPRINT, "status": "open", "allowed_productions": ["ummanu"]}
        self.writer.reader = mock.Mock()
        self.writer.reader.show.side_effect = lambda _ref, **_: dict(self.sprint)
        self.writer.audit = mock.Mock()
        self.writer.transactions = mock.Mock()
        self.writer.audit.committed_event.return_value = None
        self.writer.audit.pending_event.return_value = None
        self.writer.audit.append.return_value = "evt-1"

    def allow(self, project: str = "relay", **fields: Any) -> dict[str, Any]:
        call = {"role": "po", "actor": "po", "reference": SPRINT, "project": project, "reason": REASON,
                "request_id": "allow-1", **fields}
        return self.writer.allow_production(**call)

    def writes(self) -> list[Any]:
        return [c for c in self.client.call.call_args_list if c.args and c.args[0] == "saveTaskMetadata"]

    def test_it_appends_the_project_and_records_who_and_why(self) -> None:
        answer = self.allow()

        self.assertEqual((answer["action"], answer["event_id"]), ("production_allowed", "evt-1"))
        [write] = self.writes()
        self.assertEqual(json.loads(write.kwargs["values"][ALLOWED_PRODUCTIONS_FIELD]), ["ummanu", "relay"])
        self.assertEqual(set(write.kwargs["values"]), {ALLOWED_PRODUCTIONS_FIELD})
        request_id, event = self.writer.audit.append.call_args.args
        self.assertEqual(request_id, "allow-1")
        self.assertEqual(
            (event["kind"], event["ref"], event["actor"], event["payload"]),
            ("production_allowed", SPRINT, {"role": "po", "id": "po"}, {"project": "relay", "reason": REASON}),
        )

    def test_a_project_already_allowed_writes_nothing(self) -> None:
        answer = self.allow("ummanu")

        self.assertEqual((answer["action"], answer["event_id"]), ("already_allowed", None))
        self.assertEqual(self.writes(), [])
        self.writer.audit.stage.assert_not_called()
        self.writer.audit.append.assert_not_called()

    def test_a_repeat_of_the_request_id_answers_the_same_write(self) -> None:
        committed = {"kind": "production_allowed", "ref": SPRINT, "request_id": "allow-1",
                     "payload": {"project": "relay", "reason": REASON}}
        self.writer.audit.committed_event.return_value = committed
        self.sprint["allowed_productions"] = ["ummanu", "relay"]

        answer = self.allow()

        self.assertEqual((answer["action"], answer["event_id"]), ("production_allowed", "evt-1"))
        self.assertEqual(self.writes(), [])
        self.writer.audit.stage.assert_not_called()
        with self.assertRaises(TaskError) as raised:
            self.allow("ummanu")
        self.assertEqual(raised.exception.code, "validation")
        self.assertIn("already belongs to another sprint write", raised.exception.message)

    def test_refusals_write_nothing(self) -> None:
        for fields, code in (
            ({"role": "observer", "actor": "observer"}, "role_forbidden"),
            ({"role": "steward", "actor": "steward"}, "role_forbidden"),
            ({"role": "po", "actor": "observer"}, "role_masquerade"),
            ({"project": "elsewhere"}, "validation"),
            ({"project": " "}, "validation"),
            ({"reason": " "}, "validation"),
        ):
            with self.subTest(fields=fields), self.assertRaises(TaskError) as raised:
                self.allow(**fields)
            self.assertEqual(raised.exception.code, code)
        self.writer.reader.show.assert_not_called()
        for status in ("closed", "stopped"):
            self.sprint["status"] = status
            with self.subTest(status=status), self.assertRaises(TaskError) as raised:
                self.allow()
            self.assertEqual((raised.exception.code, raised.exception.exit_code), ("closed", 3))
        self.assertEqual(self.writes(), [])
        self.writer.audit.stage.assert_not_called()


class AllowProductionCommandTests(unittest.TestCase):
    """`sprint allow-production`: arguments down to the writer, the actor from `BOARD_ACTOR`."""

    def run_command(self, *extra: str, actor: str | None = "po") -> tuple[int, dict[str, Any], str]:
        captured: dict[str, Any] = {}

        class Writer:
            def allow_production(self, **fields: Any) -> dict[str, Any]:
                captured.update(fields)
                admit_role(fields["role"], fields["actor"], {"po"})
                return {"action": "production_allowed"}

        environ = {key: value for key, value in os.environ.items() if key != "BOARD_ACTOR"}
        if actor is not None:
            environ["BOARD_ACTOR"] = actor
        err = io.StringIO()
        with (
            mock.patch.object(sprint_commands, "SprintWriter", lambda *_args, **_fields: Writer()),
            mock.patch.object(sprint_commands, "board_client"),
            mock.patch.dict(os.environ, environ, clear=True),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(err),
        ):
            code = main(
                ["sprint", "allow-production", "--ref", SPRINT, "--project", "relay", "--reason", REASON,
                 "--instance", "/nowhere", "--data-dir", "/nowhere", *extra]
            )
        return code, captured, err.getvalue()

    def test_the_arguments_and_the_actor_reach_the_writer(self) -> None:
        code, captured, _ = self.run_command("--role", "po", "--request-id", "a-1")
        self.assertEqual(code, 0)
        self.assertEqual(
            captured,
            {"role": "po", "actor": "po", "reference": SPRINT, "project": "relay", "reason": REASON,
             "request_id": "a-1"},
        )

    def test_the_observer_is_refused_by_the_writer_and_its_masquerade_too(self) -> None:
        code, _, err = self.run_command("--role", "observer", actor="observer")
        self.assertEqual((code, json.loads(err)["error"]["code"]), (3, "role_forbidden"))
        code, _, err = self.run_command("--role", "po", actor="observer")
        self.assertEqual((code, json.loads(err)["error"]["code"]), (3, "role_masquerade"))


class SprintRecordTests(unittest.TestCase):
    def test_the_sprint_record_carries_its_allowed_productions(self) -> None:
        document = {"ref": SPRINT, "status": "open", "po_session": None, "allowed_productions": ["relay"]}
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch("ummanu.sprints.SprintReader.show", return_value=document),
            mock.patch("ummanu.board.backend.board_client"),
        ):
            record = BoardSprintSessions(tmp, tmp).sprint(SPRINT)
        self.assertEqual(record, SprintRecord(SPRINT, "open", None, ("relay",)))
        self.assertFalse(hasattr(BoardSprintSessions, "hand_over"))


if __name__ == "__main__":
    unittest.main()
