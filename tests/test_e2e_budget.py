"""The e2e run budget: a sprint's budget, a decision card when it is spent, raised only on the owner's word.

secretary-1796. The dispatcher runs as in `tests/test_e2e_stage.py` (the real tick over a disposable
PostgreSQL board, GitHub faked behind `run_capture`), and the budget is the real `sprints` row: every
charge is the conditional UPDATE of `TaskWriter.record_e2e_intent`. The fixture card (`secretary-510`)
goes through the whole tick; a second card of the same sprint reaches the stage through
`e2e_stage.run_stage` directly, as `park_green_verdict` calls it, with a green gate on the SHA given.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import threading
import unittest
from typing import Any
from unittest import mock

from tests.dispatcher_fixtures import CARD_REF
from tests.e2e_stage_fixtures import REPO, SHA, E2eStageFixture, SimulatedCrash
from tests.fakes.sprints import SprintFixture
from tests.integration_setup import require_disposable_board_fixture
from tests.sql_backend_fixtures import CardStoreClient, PostgresBoard
from ummanu.board import po_origin as origin_field
from ummanu.board.e2e_record import e2e_state
from ummanu.board.owner_events import OwnerEventStore
from ummanu.cli import main
from ummanu.dispatch import e2e_stage
from ummanu.dispatch.gate import GateResult
from ummanu.dispatch.gate_receipt import mint_gate_receipt
from ummanu.dispatch.runtime import DispatcherRuntime
from ummanu.dispatch.state import DispatcherRecord
from ummanu.sprint_observer import head_choice
from ummanu.sprints import SprintReader, SprintWriter
from ummanu.tasks import TaskError, TaskReader, TaskWriter, next_project_reference
from ummanu.webproto.sprint_reads import _sprint_value

SPRINT = "sprint:1031"
OTHER = "ummanu-520"
THIRD = "ummanu-521"
DECISION_BODY = "## Decision\n\n{}\n\n## How to verify\n\n`ummanu sprint show --ref sprint:1031`\n"


def setUpModule() -> None:
    require_disposable_board_fixture(PostgresBoard.shared)


def _sha(digit: str) -> str:
    return digit * 40


class BudgetStageFixture(E2eStageFixture):
    """The e2e stage over the sprint's real budget, with a second card of the sprint beside the pilot."""

    def add_code_card(self, ref: str, *, sprint: str = SPRINT, state: str = "ready") -> None:
        self.board.add_card(
            self.board.next_key(),
            ref,
            # Ready by default: the pilot holds the project's one claim, and the stage reads no column.
            state=state,
            metadata={"task_type": "code", "review": "skipped", **({"sprint_ref": sprint} if sprint else {})},
        )

    def stage(self, ref: str, sha: str, *, runtime: DispatcherRuntime | None = None) -> dict[str, Any]:
        """One e2e stage pass for a card other than the pilot, green on `sha`."""
        receipt = mint_gate_receipt(
            validated_sha=sha,
            base_sha="b" * 40,
            gate_mode="github",
            required_checks=[{"name": "test", "conclusion": "SUCCESS", "url": ""}],
            check_set_identity='{"required":["test"]}',
        )
        record = DispatcherRecord(
            worker="worker",
            workspace=str(self.data_dir / "workspaces" / ref),
            handle="",
            head="",
            review_head="",
            attempt_id=f"attempt-{ref}",
            comment_baseline=0,
            review_baseline=0,
            state="validate",
            claimed_at=0.0,
        )
        runtime = runtime or self.runtime
        self.host.run_head_sha = sha
        try:
            return e2e_stage.run_stage(
                runtime,
                runtime.reader.show(ref),
                record,
                {},
                {},
                record.attempt_id,
                step="review",
                gate=lambda: (None, GateResult("green", f"CI green @ {sha[:12]}", attestation=receipt)),
            )
        finally:
            self.host.run_head_sha = ""

    def settle_runs(self, ref: str) -> None:
        """Every run of the card concluded red and was acted on, as a rework round leaves it."""
        state = e2e_state(self.reader.show(ref))
        for run in state.runs:
            run.result = {"outcome": "target_reached", "conclusion": "failure", "summary": "red"}
            run.acted = True
        self.writer.record_e2e_state(role="dispatcher", actor="ummanu-pilot", reference=ref, state=state.text())

    def spend_on_other(self, *digits: str) -> None:
        """The other card dispatches one run per SHA, each concluded and acted on before the next."""
        for digit in digits:
            self.assertEqual(self.stage(OTHER, _sha(digit))["action"], "e2e-waiting")
            self.settle_runs(OTHER)

    def budget(self) -> dict[str, Any]:
        return self.reader.sprint_e2e_budget(SPRINT) or {}

    def decisions(self) -> list[dict[str, Any]]:
        return [card for card in self.reader.list() if card.get("type") == "decision"]

    def spent_with_a_pending_decision(self) -> str:
        """Three runs across two cards, then the pilot's fourth is refused with one decision card, which a
        card arriving later joins."""
        self.arrange()
        self.add_code_card(OTHER)
        self.add_code_card(THIRD)
        self.spend_on_other("1", "2")
        self.assertEqual(self.stage(THIRD, _sha("3"))["action"], "e2e-waiting")
        self.assertEqual((self.budget()["used"], self.budget()["budget"]), (3, 3))
        self.to_green_review()

        spent = self.tick()

        self.assertEqual(spent.get("action"), "e2e-budget-waiting", spent)
        [decision] = self.decisions()
        self.assertEqual(spent["decision"], decision["ref"])
        # Another card reaching the stage while the budget is spent joins the same decision.
        self.settle_runs(OTHER)
        joined = self.stage(OTHER, _sha("4"))
        self.assertEqual((joined["action"], joined["decision"]), ("e2e-budget-waiting", decision["ref"]), joined)
        return str(decision["ref"])

    def hand_to_owner(self, decision: str) -> None:
        """The dispatcher submitted the decision to the PO, and the PO handed it to the owner."""
        self.board.move(self.board.key_of(decision), "in_progress")
        self.writer.handover(
            role="po",
            actor="po",
            reference=decision,
            to="owner",
            reason="more e2e runs cost money: the owner decides",
            request_id=f"handover-{decision}",
        )

    def owner_says(self, reference: str, body: str, request_id: str) -> str:
        return str(
            self.writer.comment(role="owner", actor="owner", reference=reference, body=body, request_id=request_id)[
                "event_id"
            ]
        )

    def sprint_writer(self) -> SprintWriter:
        return SprintWriter(self.board, data_dir=self.data_dir)  # type: ignore[arg-type]


class SprintBudgetStageTests(BudgetStageFixture, unittest.TestCase):
    def record_standing(self, identifier="stop-1", kind="e2e_refusal", value="no_more_e2e"):
        return self.sprint_writer().record_owner_decisions(
            role="po", actor="po", reference=SPRINT, request_id=identifier,
            entries=[{"id": identifier, "scope": "sprint", "kind": kind, "value": value,
                      "quotation": "No more e2e in this sprint. Keep the paid runs."}],
        )

    def test_owner_session_grant_requires_no_owner_comment_or_budget_card(self) -> None:
        self.arrange()
        self.record_standing("grant-session", "e2e_grant", 2)
        self.record_standing("grant-session", "e2e_grant", 2)
        self.assertEqual(self.budget()["budget"], 5)
        self.assertEqual(self.decisions(), [])
        self.assertEqual(self.writer.audit.events(SPRINT, kind="e2e_budget_raised"), [])
        self.to_green_review()
        self.assertEqual(self.tick()["action"], "e2e-waiting")

    def test_standing_refusal_with_room_and_later_card_blocks_without_dispatch_card_or_bell(self) -> None:
        self.arrange()
        self.record_standing()
        from ummanu.sprints import SprintReader
        view = SprintReader(self.board, data_dir=self.data_dir).show(SPRINT)
        self.assertEqual((view["e2e"]["budget"], view["e2e"]["used"]), (3, 0))
        self.assertIn("no more e2e", view["e2e"]["summary"])
        self.assertIn(f"{SPRINT}/stop-1", view["e2e"]["summary"])
        self.to_green_review()
        self.assertEqual(self.tick()["status"], "blocked")
        self.add_code_card(OTHER, state="validate")
        self.assertEqual(self.stage(OTHER, _sha("2"))["status"], "blocked")
        self.assertEqual(self.host.dispatches, [])
        self.assertEqual(self.decisions(), [])
        self.assertEqual(self.budget()["used"], 0)
        for ref in (CARD_REF, OTHER):
            shown = self.reader.show(ref)
            self.assertEqual(shown["e2e"]["declined_by"], f"{SPRINT}/stop-1")
            moved = [event for event in self.writer.audit.events(ref) if event.get("transition", {}).get("target") == "blocked"][-1]
            self.assertIn("No more e2e in this sprint.", moved["reason"])
        self.assertEqual([event for event in OwnerEventStore(self.board.credentials).events() if event.event_class == "needs_owner"], [])

    def test_spent_standing_refusal_creates_no_budget_card(self) -> None:
        self.arrange()
        self.add_code_card(OTHER)
        self.spend_on_other("1", "2", "3")
        self.record_standing()
        self.to_green_review()
        self.assertEqual(self.tick()["status"], "blocked")
        self.assertEqual(len(self.host.dispatches), 3)
        self.assertEqual(self.decisions(), [])
        self.assertEqual([event for event in OwnerEventStore(self.board.credentials).events() if event.event_class == "needs_owner"], [])

    def test_pending_budget_wait_consumes_standing_refusal_and_preserves_paid_history(self) -> None:
        decision = self.spent_with_a_pending_decision()
        # run_stage is reached after gate validation; the pilot is already there through tick.
        self.board.move(self.board.key_of(OTHER), "validate")
        self.record_standing()
        self.assertEqual(self.tick()["status"], "blocked")
        self.assertEqual(self.stage(OTHER, _sha("4"))["status"], "blocked")
        # The pending budget decision already occupies the next project reference.
        late = next_project_reference(self.board, 1, "ummanu")
        self.add_code_card(late, state="validate")
        self.assertEqual(self.stage(late, _sha("5"))["status"], "blocked")
        self.assertEqual([card["ref"] for card in self.decisions()], [decision])
        self.assertEqual(self.budget()["used"], 3)
        self.assertEqual(len(self.host.dispatches), 3)
        self.assertEqual([event for event in OwnerEventStore(self.board.credentials).events() if event.event_class == "needs_owner"], [])

    def test_paid_run_keeps_running_after_a_standing_refusal(self) -> None:
        self.arrange()
        self.to_green_review()
        self.assertEqual(self.tick()["action"], "e2e-waiting")
        history = self.budget()["charges"]
        self.record_standing()
        self.assertEqual(self.tick()["action"], "e2e-waiting")
        self.assertEqual(self.budget()["charges"], history)
        self.assertEqual(len(self.host.dispatches), 1)
        self.assertEqual(self.decisions(), [])

    def test_an_unrelated_later_block_clears_the_known_refusal_marker(self) -> None:
        self.arrange()
        self.add_code_card(OTHER, state="validate")
        self.record_standing()
        self.assertEqual(self.stage(OTHER, _sha("1"))["status"], "blocked")
        self.writer.move(
            role="po", actor="po", reference=OTHER, target="ready", reason="a later plan",
            sprint_override=True, sprint_override_reason="fixture later plan", request_id="new-plan",
        )
        self.writer.move(
            role="po", actor="po", reference=OTHER, target="blocked", reason="needs a fresh decision",
            sprint_override=True, sprint_override_reason="fixture unrelated block", request_id="unrelated-block",
        )
        self.assertIsNone(e2e_state(self.reader.show(OTHER)).budget_decline)
        self.assertTrue([event for event in OwnerEventStore(self.board.credentials).events() if event.subject_ref == OTHER and event.event_class == "notice"])

    def test_the_fourth_run_of_a_sprint_is_not_dispatched_and_cuts_exactly_one_decision(self) -> None:
        decision = self.spent_with_a_pending_decision()

        self.assertEqual(len(self.host.dispatches), 3, "runs 1-2 by one card, 3 by another, no fourth")
        self.assertEqual(len(self.decisions()), 1, "the second card joined; no second decision")
        shown = self.reader.show(decision)
        self.assertEqual((shown["type"], shown["sprint"], shown["state"]), ("decision", SPRINT, "ready"))
        # The card, the runs spent with their links and results, and the question.
        body = shown["description"]
        self.assertIn(f"- {CARD_REF} waits", body)
        self.assertIn("The e2e run budget of sprint:1031 is spent: 3 of 3 runs.", body)
        self.assertIn(f"https://github.com/{REPO}/actions/runs/", body)
        self.assertIn("(failure: red)", body)
        self.assertIn("Raise the e2e budget of sprint:1031 by N runs, or no?", body)
        self.assertIn("task handover", body)
        self.assertIn("sprint e2e-budget --ref sprint:1031 --role po --authorized-by <event id>", body)
        # The two exact answer lines the owner is asked for.
        self.assertIn("\n    e2e budget: raise <N>\n    e2e budget: no\n", body)
        [joined] = [c["body"] for c in shown["comments"] if OTHER in c["body"]]
        self.assertIn("joins this decision", joined)
        # The pilot is not Blocked: it waits where it is, with the mark on `task show`.
        card = self.card()
        self.assertEqual(card["state"], "validate")
        self.assertEqual(card["e2e"]["mark"], f"e2e: budget spent, waiting on {decision}")
        self.assertEqual(e2e_state(self.reader.show(OTHER)).budget_wait.decision, decision)
        # Re-checked each tick without reading the gate, and nothing is cut or dispatched again.
        gate_reads = len(self.host.gate_calls)
        again = self.tick()
        self.assertEqual((again["action"], again["decision"]), ("e2e-budget-waiting", decision))
        self.assertEqual(self.stage(OTHER, _sha("4"))["action"], "e2e-budget-waiting")
        self.assertEqual(len(self.host.gate_calls), gate_reads)
        self.assertEqual((len(self.host.dispatches), len(self.decisions())), (3, 1))
        self.assertEqual(len([c for c in self.reader.show(decision)["comments"] if "joins" in c["body"]]), 1)
        # The sprint says what was spent, and by whom.
        shown_sprint = SprintReader(self.board, data_dir=self.data_dir).show(SPRINT)  # type: ignore[arg-type]
        self.assertEqual(shown_sprint["e2e"]["summary"], "e2e: 3 of 3")
        self.assertEqual(shown_sprint["e2e"]["cards"], [OTHER, THIRD])

    def test_a_raise_on_the_owner_s_answer_lets_the_waiting_cards_dispatch_next_tick(self) -> None:
        decision = self.spent_with_a_pending_decision()
        self.hand_to_owner(decision)
        answer = self.owner_says(decision, "Fine, two more.\n  E2E Budget: Raise 2  \nNo more after that.", "owner-raise")

        raised = self.sprint_writer().raise_e2e_budget(
            role="po", actor="po", reference=SPRINT, add=2, authorized_by=answer
        )

        self.assertEqual(raised["action"], "e2e_budget_raised")
        self.assertEqual((self.budget()["used"], self.budget()["budget"]), (3, 5))
        [event] = self.writer.audit.events(SPRINT, kind="e2e_budget_raised")
        self.assertEqual(event["payload"], {"add": 2, "authorized_by": answer, "decision": decision})
        self.assertEqual(event["actor"], {"role": "po", "id": "po"})
        waiting = self.tick()
        self.assertEqual((waiting["action"], waiting["sha"]), ("e2e-waiting", SHA), waiting)
        self.assertEqual(self.stage(OTHER, _sha("4"))["action"], "e2e-waiting")
        self.assertEqual(len(self.host.dispatches), 5)
        self.assertEqual((self.budget()["used"], self.budget()["budget"]), (5, 5))
        self.assertNotIn("mark", self.card()["e2e"])
        # The same answer raises once: a repeat is the same raise, another request id is refused.
        again = self.sprint_writer().raise_e2e_budget(
            role="po", actor="po", reference=SPRINT, add=2, authorized_by=answer
        )
        self.assertEqual(again["event_id"], raised["event_id"])
        with self.assertRaises(TaskError) as refused:
            self.sprint_writer().raise_e2e_budget(
                role="po", actor="po", reference=SPRINT, add=2, authorized_by=answer, request_id="another"
            )
        self.assertEqual(refused.exception.code, "authorization_refused")
        self.assertEqual(self.budget()["budget"], 5)

    def test_a_raise_the_owner_did_not_authorize_is_refused_and_writes_nothing(self) -> None:
        decision = self.spent_with_a_pending_decision()
        early = self.owner_says(decision, "e2e budget: raise 1", "owner-before-handover")
        self.hand_to_owner(decision)
        answer = self.owner_says(decision, "e2e budget: raise 1", "owner-raise")
        po_comment = str(
            self.writer.comment(
                role="po", actor="po", reference=decision, body="e2e budget: raise 1", request_id="po-says"
            )["event_id"]
        )
        elsewhere = self.owner_says(CARD_REF, "e2e budget: raise 1", "owner-elsewhere")
        refusals = {
            "no authorization": ("po", ""),
            "an unknown event": ("po", "evt_nothing"),
            "a PO comment on the decision": ("po", po_comment),
            "an owner comment on another card": ("po", elsewhere),
            "an owner comment before the handover": ("po", early),
            "the observer": ("observer", answer),
            "the dispatcher": ("dispatcher", answer),
            "a worker": ("worker", answer),
        }
        for label, (role, authorized_by) in refusals.items():
            with self.subTest(label), self.assertRaises(TaskError) as refused:
                self.sprint_writer().raise_e2e_budget(
                    role=role, actor=role, reference=SPRINT, add=1, authorized_by=authorized_by
                )
            self.assertIn(refused.exception.code, {"authorization_refused", "role_forbidden", "role_masquerade"})

        self.assertEqual((self.budget()["used"], self.budget()["budget"]), (3, 3))
        self.assertEqual(self.writer.audit.events(SPRINT, kind="e2e_budget_raised"), [])
        self.assertEqual(self.tick()["action"], "e2e-budget-waiting")
        # Another sprint's decision does not authorize this one either.
        with self.assertRaises(TaskError) as refused:
            self.sprint_writer().raise_e2e_budget(
                role="po", actor="po", reference="sprint:1469", add=1, authorized_by=answer
            )
        self.assertIn(refused.exception.code, {"authorization_refused", "not_found"})

    def test_the_raise_is_the_owner_s_recorded_number_and_nothing_else(self) -> None:
        decision = self.spent_with_a_pending_decision()
        self.hand_to_owner(decision)
        refused_answers = {
            "no": "e2e budget: no",
            "unparseable": "raise by 2, I suppose",
            "two raise lines": "e2e budget: raise 1\ne2e budget: raise 2",
            "both forms": "e2e budget: raise 1\ne2e budget: no",
            "not a positive N": "e2e budget: raise 0",
            "not the whole line": "ok, e2e budget: raise 2 then",
        }
        for label, body in refused_answers.items():
            said = self.owner_says(decision, body, f"owner-{label.replace(' ', '-')}")
            with self.subTest(label), self.assertRaises(TaskError) as refused:
                self.sprint_writer().raise_e2e_budget(role="po", actor="po", reference=SPRINT, authorized_by=said)
            self.assertEqual(refused.exception.code, "authorization_refused")
        answer = self.owner_says(decision, "e2e budget: raise 2", "owner-two")
        # An --add other than the owner's N is refused, and writes nothing.
        with self.assertRaises(TaskError) as refused:
            self.sprint_writer().raise_e2e_budget(
                role="po", actor="po", reference=SPRINT, add=100, authorized_by=answer
            )
        self.assertEqual(refused.exception.code, "authorization_refused")
        self.assertEqual(self.budget()["budget"], 3)
        self.assertEqual(self.writer.audit.events(SPRINT, kind="e2e_budget_raised"), [])

        # With no --add, N is the owner's.
        self.sprint_writer().raise_e2e_budget(role="po", actor="po", reference=SPRINT, authorized_by=answer)

        self.assertEqual(self.budget()["budget"], 5)
        [event] = self.writer.audit.events(SPRINT, kind="e2e_budget_raised")
        self.assertEqual(event["payload"], {"add": 2, "authorized_by": answer, "decision": decision})

    def test_the_decision_done_without_a_raise_sends_the_waiting_cards_to_blocked(self) -> None:
        decision = self.spent_with_a_pending_decision()
        self.hand_to_owner(decision)
        self.owner_says(decision, "e2e budget: no\nThe stands budget is gone for this month.", "owner-no")
        self.writer.complete(
            role="po",
            actor="po",
            reference=decision,
            kind="decision",
            body=DECISION_BODY.format("The owner said no: no more e2e runs for sprint:1031 this month."),
            request_id="po-complete",
        )

        blocked = self.tick()

        self.assertEqual(blocked["status"], "blocked", blocked)
        self.assertEqual(blocked["reason"], "e2e budget not raised")
        card = self.card()
        self.assertEqual(card["state"], "blocked")
        event = self.blocked_transition()
        self.assertIn("no more e2e runs for sprint:1031 this month", str(event.get("reason")))
        self.assertIn(f"the decision {decision} was completed without a raise", str(event.get("reason")))
        self.assertEqual(event["data"]["terminal_taxonomy"]["blocked_reason"], "other")
        self.assertNotIn("gate-red", str(event.get("request_id")), "not charged as a code defect")
        self.assertNotIn("mark", card.get("e2e") or {})
        self.assertEqual(len(self.host.dispatches), 3)

    def test_a_crash_between_the_charge_and_the_post_is_recovered_and_not_charged_again(self) -> None:
        self.arrange()
        self.to_green_review()
        self.host.dispatch_answer = "crash"
        with self.assertRaises(SimulatedCrash):
            self.tick()
        [intent] = e2e_state(self.card()).runs
        charged = self.budget()
        self.assertEqual(charged["used"], 1)
        self.assertEqual([c["dispatch_id"] for c in charged["charges"]], [intent.dispatch_id])

        self.host.dispatch_answer = "ok"
        with self.settled():
            waiting = self.tick(self._runtime())

        self.assertEqual(waiting["action"], "e2e-waiting", waiting)
        [run] = e2e_state(self.card()).runs
        self.assertEqual((run.dispatch_id, run.identified_by), (intent.dispatch_id, "recovery"))
        self.assertEqual(len(self.host.dispatches), 1)
        after = self.budget()
        self.assertEqual((after["used"], len(after["charges"])), (1, 1))

    def test_two_cards_racing_for_the_last_run_give_one_dispatch(self) -> None:
        self.arrange()
        self.add_code_card(OTHER)
        self.add_code_card(THIRD)
        self.spend_on_other("1", "2")
        second = CardStoreClient(self.board.credentials, self.data_dir)
        self.addCleanup(second.close)
        racer = DispatcherRuntime(
            TaskReader(second),  # type: ignore[arg-type]
            TaskWriter(second, data_dir=self.data_dir, workspace=self.data_dir),  # type: ignore[arg-type]
            TaskWriter(second, data_dir=self.data_dir, workspace=self.data_dir).audit,  # type: ignore[arg-type]
            self.data_dir,
            self.catalog,  # type: ignore[arg-type]
            self.host,  # type: ignore[arg-type]
            owner="ummanu-pilot",
            sprints=self.sprints,
        )
        # Both passes decide to dispatch before either charges: the charge alone decides the race.
        barrier = threading.Barrier(2, timeout=30)
        charge = TaskWriter.record_e2e_intent

        def at_the_same_time(writer: TaskWriter, **fields: Any) -> dict[str, Any]:
            barrier.wait()
            return charge(writer, **fields)

        outcomes: dict[str, Any] = {}

        def race(ref: str, runtime: DispatcherRuntime, sha: str) -> None:
            try:
                outcomes[ref] = self.stage(ref, sha, runtime=runtime)["action"]
            except Exception as exc:  # noqa: BLE001 - reported below
                outcomes[ref] = exc

        with mock.patch.object(TaskWriter, "record_e2e_intent", at_the_same_time):
            threads = [
                threading.Thread(target=race, args=(OTHER, self.runtime, _sha("3"))),
                threading.Thread(target=race, args=(THIRD, racer, _sha("3"))),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(60)

        self.assertEqual(sorted(outcomes.values()), ["e2e-budget-waiting", "e2e-waiting"], outcomes)
        self.assertEqual(len(self.host.dispatches), 3)
        self.assertEqual((self.budget()["used"], len(self.budget()["charges"])), (3, 3))
        dispatched = [ref for ref, action in outcomes.items() if action == "e2e-waiting"]
        [winner] = dispatched
        runs = {ref: len(e2e_state(self.reader.show(ref)).runs) for ref in (OTHER, THIRD)}
        self.assertEqual(runs[THIRD], 1 if winner == THIRD else 0)
        self.assertEqual(runs[OTHER], 3 if winner == OTHER else 2)


class OutOfSprintCapTests(BudgetStageFixture, unittest.TestCase):
    def spent_cap(self, *, origin: bool) -> None:
        self.arrange(review="skipped", observed=False)
        if origin:
            self.board.save_metadata(12, {"po_origin": origin_field.origin_text("po-session-7", "po-request-7")})
        state = {
            "runs": [
                {
                    "dispatch_id": f"{CARD_REF}-e2e-{n}-0000000{n}",
                    "sha": _sha(str(n)),
                    "repo": REPO,
                    "branch": f"pipeline/{CARD_REF}",
                    "workflow": "e2e.yml",
                    "intent_at": "2026-09-27T10:00:00Z",
                    "dispatch": "sent",
                    "run_id": 8000 + n,
                    "run_url": f"https://github.com/{REPO}/actions/runs/{8000 + n}",
                    "result": {"outcome": "target_reached", "conclusion": "failure", "summary": "red"},
                    "acted": True,
                }
                for n in (1, 2, 3)
            ]
        }
        self.writer.record_e2e_state(
            role="dispatcher", actor="ummanu-pilot", reference=CARD_REF, state=json.dumps(state)
        )
        self._run_worker_to_validate()

    def test_a_card_with_a_po_origin_gets_a_decision_card_with_that_origin(self) -> None:
        self.spent_cap(origin=True)

        waiting = self.tick()

        self.assertEqual(waiting["action"], "e2e-budget-waiting", waiting)
        [decision] = self.decisions()
        shown = self.reader.show(decision["ref"])
        self.assertEqual(origin_field.po_origin(shown), {"session": "po-session-7", "request": "po-request-7"})
        self.assertFalse(shown.get("sprint"))
        self.assertIn(f"task e2e-budget --ref {CARD_REF} --role po", shown["description"])
        self.assertIn(f"https://github.com/{REPO}/actions/runs/8001", shown["description"])
        self.assertEqual(self.card()["state"], "validate")
        self.assertEqual(self.card()["e2e"]["mark"], f"e2e: budget spent, waiting on {decision['ref']}")
        self.assertEqual(self.host.dispatches, [])

        # The owner's answer raises the card's own cap, and the stage dispatches on the next tick.
        self.hand_to_owner(decision["ref"])
        answer = self.owner_says(decision["ref"], "e2e budget: raise 1", "owner-cap")
        with self.assertRaises(TaskError):
            self.writer.raise_e2e_cap(role="observer", actor="observer", reference=CARD_REF, add=1, authorized_by=answer)
        with self.assertRaises(TaskError) as refused:
            self.writer.raise_e2e_cap(role="po", actor="po", reference=CARD_REF, add=5, authorized_by=answer)
        self.assertEqual(refused.exception.code, "authorization_refused")
        self.assertEqual(self.card()["e2e"]["run_cap"], 3)
        # No --add: the owner's N.
        self.writer.raise_e2e_cap(role="po", actor="po", reference=CARD_REF, authorized_by=answer)
        [event] = self.writer.audit.events(CARD_REF, kind="e2e_cap_raised")
        self.assertEqual((event["payload"]["authorized_by"], event["payload"]["add"]), (answer, 1))
        self.assertEqual(self.card()["e2e"]["run_cap"], 4)

        self.assertEqual(self.tick()["action"], "e2e-waiting")
        self.assertEqual(len(self.host.dispatches), 1)

    def test_refused_dispatches_count_and_the_fourth_entry_cuts_the_decision_without_a_post(self) -> None:
        """Out of a sprint a run counts when its intent is persisted, exactly as in a sprint: refused too."""
        self.arrange(review="skipped", observed=False)
        # In Validate, where a card reaching the stage is (the pilot is never claimed in this test).
        self.add_code_card(OTHER, sprint="", state="validate")
        self.board.save_metadata(
            self.board.key_of(OTHER), {"po_origin": origin_field.origin_text("po-session-7", "po-request-7")}
        )
        self.host.dispatch_answer = ("http", "gh: Workflow does not have 'workflow_dispatch' trigger (HTTP 422)")
        for digit in ("1", "2", "3"):
            self.assertEqual(self.stage(OTHER, _sha(digit))["status"], "blocked")
            # Unblocked and back at the stage, as a card brought back for another round is.
            self.board.move(self.board.key_of(OTHER), "validate")
        view = self.reader.show(OTHER)["e2e"]
        self.assertEqual((view["runs_dispatched"], view["run_cap"]), (3, 3))
        self.assertEqual(len(self.host.dispatches), 3)

        waiting = self.stage(OTHER, _sha("4"))

        self.assertEqual(waiting["action"], "e2e-budget-waiting", waiting)
        self.assertEqual(len(self.host.dispatches), 3, "the fourth entry does not POST")
        [decision] = self.decisions()
        self.assertEqual(
            origin_field.po_origin(self.reader.show(decision["ref"])),
            {"session": "po-session-7", "request": "po-request-7"},
        )
        self.assertEqual(len(e2e_state(self.reader.show(OTHER)).runs), 3)

    def test_a_card_with_no_origin_has_one_assigned_po_decision_without_spending(self) -> None:
        self.spent_cap(origin=False)
        waiting = self.tick()
        self.assertEqual(waiting["action"], "e2e-budget-waiting")
        [decision] = self.decisions()
        shown = self.reader.show(decision["ref"])
        self.assertIsNone(origin_field.po_origin(shown))
        self.assertEqual(shown["po_execution"]["purpose"], "e2e_budget")
        self.assertEqual(shown["po_execution"]["sources"], [CARD_REF])
        self.assertEqual(self.card()["e2e"]["waiting_on"], decision["ref"])
        self.assertEqual(self.tick()["action"], "e2e-budget-waiting")
        self.assertEqual(len(self.decisions()), 1)
        self.assertEqual(len(e2e_state(self.card()).runs), 3)
        self.assertEqual(self.host.dispatches, [])
        self.assertFalse(any(e.event_class == "needs_owner" for e in OwnerEventStore(self.board.credentials).events()))


class SprintBudgetEntityTests(SprintFixture):
    """`sprint create --e2e-budget`, and `sprint show` / `sprint status` rendering what is spent."""

    def _create(self, **kwargs: Any) -> dict[str, Any]:
        for field, value in (
            ("role", "po"),
            ("actor", "operator"),
            ("product", "ummanu"),
            ("issues", ["issue:open"]),
            ("projects", ["ummanu"]),
            ("observer", head_choice("codex-observer")),
        ):
            kwargs.setdefault(field, value)
        return self.writer.create(**kwargs)

    def cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with (
            self.board_injected(),
            mock.patch.dict(os.environ, {"BOARD_ACTOR": "po"}),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = main([*argv, "--instance", str(self.instance), "--data-dir", self.tmp.name])
        return code, out.getvalue(), err.getvalue()

    def test_create_defaults_to_a_budget_of_3(self) -> None:
        self._create(goal="default", reference="sprint:7")
        self.assertEqual(self.sprint("sprint:7")["e2e"]["summary"], "e2e: 0 of 3")
        self.assertEqual(self.client._query("SELECT e2e_budget, e2e_used FROM sprints"), [(3, 0)])

    def test_create_takes_an_explicit_budget_and_refuses_a_negative_one(self) -> None:
        with self.assertRaises(TaskError) as refused:
            self._create(goal="negative", reference="sprint:9", e2e_budget=-1, request_id="negative")
        self.assertEqual(refused.exception.code, "validation")
        self._create(goal="explicit", reference="sprint:8", e2e_budget=5, request_id="explicit")
        self.assertEqual(self.client._query("SELECT ref, e2e_budget, e2e_used FROM sprints"), [("sprint:8", 5, 0)])

    def test_the_cli_takes_the_budget_and_show_and_status_render_used_of_budget(self) -> None:
        code, _output, errors = self.cli(
            "sprint", "create", "--role", "po", "--goal", "bad", "--product", "ummanu", "--issue",
            "issue:open", "--project", "ummanu", "--observer", "codex-observer", "--e2e-budget", "-2",
        )
        self.assertEqual(code, 2, "argparse refuses a negative budget")
        self.assertIn("0 or more runs", errors)
        code, _output, errors = self.cli(
            "sprint", "create", "--role", "po", "--goal", "cli", "--product", "ummanu", "--issue",
            "issue:open", "--project", "ummanu", "--observer", "codex-observer", "--ref", "sprint:12",
            "--e2e-budget", "2",
        )
        self.assertEqual(code, 0, errors)
        self.assertEqual(self.sprint("sprint:12")["e2e"]["summary"], "e2e: 0 of 2")
        # Two runs charged by two cards; the third finds nothing left.
        for ref, dispatch in (("ummanu-90", "d-1"), ("ummanu-91", "d-2"), ("ummanu-91", "d-3")):
            with self.client.transaction():
                answer = self.client.call(
                    "chargeSprintE2e", sprint_ref="sprint:12", task_ref=ref, dispatch_id=dispatch,
                    at="2026-09-27T10:00:0" + dispatch[-1] + "Z",
                )
            self.assertEqual(answer["charged"], dispatch != "d-3")

        code, output, errors = self.cli("sprint", "show", "--ref", "sprint:12")
        self.assertEqual(code, 0, errors)
        shown = json.loads(output)["e2e"]
        self.assertEqual((shown["summary"], shown["cards"]), ("e2e: 2 of 2", ["ummanu-90", "ummanu-91"]))
        self.assertEqual([c["dispatch_id"] for c in shown["charges"]], ["d-1", "d-2"])
        status = SprintReader(self.client, data_dir=self.tmp.name).status("sprint:12")  # type: ignore[arg-type]
        self.assertEqual(status["e2e"]["summary"], "e2e: 2 of 2")
        # `sprint status` prints the watched sprint's value, and the value carries the same block.
        self.assertEqual(_sprint_value(self.sprint("sprint:12"))["e2e"]["summary"], "e2e: 2 of 2")

    def test_the_raise_verb_refuses_a_po_call_without_the_owner_s_authorization(self) -> None:
        self._create(goal="raise", reference="sprint:13")
        code, _output, errors = self.cli(
            "sprint", "e2e-budget", "--ref", "sprint:13", "--role", "po", "--authorized-by", "evt_x"
        )
        self.assertNotEqual(code, 0)
        self.assertIn("authorization_refused", errors)
        code, _output, errors = self.cli(
            "sprint", "e2e-budget", "--ref", "sprint:13", "--role", "observer", "--add", "1", "--authorized-by", "evt_x"
        )
        self.assertNotEqual(code, 0)
        self.assertEqual(self.sprint("sprint:13")["e2e"]["summary"], "e2e: 0 of 3")


if __name__ == "__main__":
    unittest.main()
