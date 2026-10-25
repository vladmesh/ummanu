"""Hermetic code stage, live checkpoint migration and release authority regressions."""

from __future__ import annotations

import contextlib
import copy
import unittest
from unittest import mock

from ummanu.board.budget_candidates import is_candidate
from ummanu.board.host import TransitionRequest
from ummanu.board.models import Actor, CardState, EntityKind
from ummanu.board.observer_routing import observer_managed
from ummanu.board.sql_host import SqlBoardHost
from ummanu.dispatch import (
    assessment_decision,
    gate_lifecycle,
    release_lifecycle,
    review_verdict,
    runtime as runtime_module,
)
from ummanu.dispatch.gate import GateResult
from ummanu.dispatch.gate_receipt import AcceptedGreenGate
from ummanu.dispatch.production import _budget_event_type
from ummanu.dispatch.stage_migration import reconcile_validation
from ummanu.dispatch.state import DispatcherRecord
from ummanu.dispatch.types import GateTransportError, HostError
from ummanu.tasks import TaskError, TaskWriter


class CodeStagesTests(unittest.TestCase):
    def setUp(self):
        self.task = {"ref": "sample-1", "state": "ci", "type": "code", "review": "required", "comments": []}
        self.record = DispatcherRecord(
            worker="worker", workspace="/candidate", handle="", head="worker", review_head="reviewer",
            attempt_id="held", state="ci", claimed_at=1, comment_baseline=0, review_baseline=2,
            report_generation=3,
        )
        self.records = {self.task["ref"]: self.record}
        self.payload = {}
        self.runtime = mock.Mock()
        self.runtime.owner = "dispatcher"
        self.runtime.host.mode = "noop"
        self.runtime.host.head_commit.return_value = "a" * 40
        self.runtime.audit.events.return_value = []
        self.runtime.audit.committed_event.return_value = None
        self.runtime._review_launch_recorded.return_value = False
        self.runtime._end_review_pane_confirmed.return_value = None
        self.runtime.reader.show.side_effect = lambda _: copy.deepcopy(self.task)
        self.moves = []

        def move(**args):
            self.moves.append((self.task["state"], args["target"]))
            self.task["state"] = args["target"]

        self.runtime.writer.move.side_effect = move

    def advance(self):
        return runtime_module.DispatcherRuntime._advance_review(
            self.runtime, self.task, self.records, self.payload, "tick"
        )

    def test_pending_and_transport_retry_stay_in_ci_without_a_review_launch(self):
        for answer in (GateResult("pending", "waiting for checks"), GateTransportError("try again")):
            with self.subTest(answer=answer), mock.patch.object(runtime_module, "_advance_review_verdict", return_value=None):
                self.runtime.host.gate_check.side_effect = answer if isinstance(answer, Exception) else None
                self.runtime.host.gate_check.return_value = answer
                result = self.advance()
                self.assertIn(result["action"], {"gate-pending", "gate-transport-retry"})
                self.assertEqual(self.task["state"], "ci")
                self.assertEqual(self.moves, [])

    def test_green_gate_moves_to_review_before_launch_and_hold_recovery(self):
        self.runtime.host.gate_check.return_value = GateResult("green", "green")
        with (
            mock.patch.object(runtime_module, "_advance_review_verdict", return_value=None),
            mock.patch.object(runtime_module, "_start_review", return_value={"action": "provider-hold"}) as launch,
            mock.patch.object(gate_lifecycle, "_validation_ci", return_value="none"),
        ):
            result = self.advance()
        self.assertEqual(result["action"], "provider-hold")
        self.assertEqual(self.moves, [("ci", "review")])
        self.assertEqual(launch.call_args.args[1]["state"], "review")
        self.assertEqual(self.record.state, "review_starting")
        with (
            mock.patch.object(runtime_module, "_advance_review_verdict", return_value=None),
            mock.patch.object(runtime_module, "_recover_review_launch", return_value={"action": "waiting-launch"}) as recover,
        ):
            self.advance()
        recover.assert_called_once()
        self.assertEqual(self.runtime.host.gate_check.call_count, 1)

    def test_skipped_code_reaches_assessment_without_review(self):
        self.task["review"] = "skipped"
        self.runtime.host.gate_check.return_value = GateResult("green", "green")
        with (
            mock.patch.object(runtime_module, "_advance_review_verdict", return_value=None),
            mock.patch.object(gate_lifecycle, "_validation_ci", return_value="none"),
            mock.patch.object(runtime_module, "_park_green_verdict", return_value={"to": "assessment"}) as park,
            mock.patch.object(runtime_module, "_start_review") as launch,
        ):
            self.assertEqual(self.advance()["to"], "assessment")
        self.assertFalse(park.call_args.kwargs["reviewed"])
        launch.assert_not_called()
        self.assertEqual(self.moves, [])

    def test_red_gate_uses_the_existing_worker_transition_with_its_rejection_evidence(self):
        self.runtime.host.gate_check.return_value = GateResult("red", "assertion failed", "failure log")
        with (
            mock.patch.object(runtime_module, "_advance_review_verdict", return_value=None),
            mock.patch.object(gate_lifecycle, "_begin_red_transition", return_value={"action": "rework"}) as rework,
            mock.patch.object(runtime_module, "_start_review") as launch,
        ):
            self.assertEqual(self.advance()["action"], "rework")
        self.assertEqual(rework.call_args.kwargs["phase"], "gate")
        self.assertIn("assertion failed", rework.call_args.kwargs["move_reason"])
        self.assertEqual(self.record.rejected_sha, "a" * 40)
        launch.assert_not_called()

    def test_automatic_green_review_and_skipped_release_audit_assessment_before_merge(self):
        for reviewed in (True, False):
            with self.subTest(reviewed=reviewed):
                self.task.update(state="review" if reviewed else "ci", sprint="")
                self.moves.clear()
                self.record.worker_continuation.clear()
                self.record.gate_state = "green"
                self.runtime.host.complete_green.side_effect = lambda *_: self.assertEqual(self.task["state"], "assessment")

                def terminal(*args, **kwargs):
                    self.runtime.writer.move(target=kwargs["target"])

                with (
                    mock.patch.object(review_verdict.e2e_stage, "run_stage", return_value=None),
                    mock.patch.object(review_verdict, "merge_ready_for_park", return_value=None),
                    mock.patch.object(gate_lifecycle, "_validation_ci", return_value="none"),
                    mock.patch.object(release_lifecycle.attempt_accounting, "terminal_effect", side_effect=terminal),
                    mock.patch.object(review_verdict.attempt_accounting, "record_attempt_usage"),
                ):
                    result = review_verdict.park_green_verdict(
                        self.runtime, self.task, self.record, self.records, self.payload, "tick", reviewed=reviewed
                    )
                self.assertEqual(result["to"], "done")
                self.assertEqual(self.moves, [("review" if reviewed else "ci", "assessment"), ("assessment", "done")])
                self.records[self.task["ref"]] = self.record

    def test_automatic_assessment_recovery_does_not_synthesize_a_decision_or_reviewer(self):
        self.task.update(state="assessment", sprint="", comments=[
            {"marker": "report:done"}, {"marker": "review:green"},
        ])
        self.record.worker_continuation.begin_park("review", 2, "green", "green")
        self.record.worker_continuation.confirm_park()
        with (
            mock.patch.object(assessment_decision, "recorded_decision", return_value=("", "", ())),
            mock.patch.object(release_lifecycle, "release_parked", return_value={"to": "done"}) as release,
        ):
            outcome = assessment_decision.advance_assessment(
                self.runtime, self.task, self.records, self.payload, "tick"
            )
        self.assertEqual(outcome["to"], "done")
        self.assertTrue(release.call_args.kwargs["automatic"])
        self.runtime.writer.decide.assert_not_called()
        self.runtime.host.start_review.assert_not_called()

    def test_live_validate_migration_preserves_the_round_and_is_idempotent(self):
        for state, green, launched, target in (
            ("validate", "", False, "ci"),
            ("validate", "green", False, "ci"),
            ("review_starting", "green", True, "review"),
            ("reviewing", "green", True, "review"),
        ):
            with self.subTest(state=state, green=green):
                self.task["state"] = "validate"
                self.record.state, self.record.gate_state = state, green
                self.runtime._review_launch_recorded.return_value = launched
                before = self.record.to_json()
                self.moves.clear()
                for _ in range(2):
                    reconcile_validation(self.runtime, self.task, self.records, self.payload, "tick")
                self.assertEqual(self.moves, [("validate", target)])
                after = self.record.to_json()
                before.pop("state")
                after.pop("state")
                self.assertEqual(after, before)

    def test_interrupted_board_move_replays_the_same_migration_and_checkpoint(self):
        self.task["state"] = "validate"
        self.record.state = "validate"
        move = self.runtime.writer.move.side_effect
        self.runtime.writer.move.side_effect = HostError("board unavailable")
        with self.assertRaises(HostError):
            reconcile_validation(self.runtime, self.task, self.records, self.payload, "tick")
        request = self.runtime.writer.move.call_args.kwargs["request_id"]
        self.runtime.writer.move.side_effect = move
        reconcile_validation(self.runtime, self.task, self.records, self.payload, "another-tick")
        self.assertEqual(self.runtime.writer.move.call_args.kwargs["request_id"], request)
        self.record.state = "validate"
        reconcile_validation(self.runtime, self.task, self.records, self.payload, "third-tick")
        self.assertEqual(self.record.state, "ci")
        self.assertEqual(self.moves, [("validate", "ci")])

    def test_lost_record_uses_existing_adoption_and_keeps_its_requested_review(self):
        self.task["state"] = "validate"
        self.record.state, self.record.gate_state = "review_starting", "green"
        self.runtime._adopt.return_value = self.record
        self.records.clear()
        reconcile_validation(self.runtime, self.task, self.records, self.payload, "tick")
        self.runtime._adopt.assert_called_once()
        self.assertIs(self.records[self.task["ref"]], self.record)
        self.assertEqual(self.moves, [("validate", "review")])

    def test_unaccepted_review_checkpoint_refuses_migration_without_destroying_it(self):
        self.task["state"] = "validate"
        self.record.state = "reviewing"
        with self.assertRaisesRegex(HostError, "no accepted gate"):
            reconcile_validation(self.runtime, self.task, self.records, self.payload, "tick")
        self.assertEqual(self.moves, [])
        self.assertIs(self.records[self.task["ref"]], self.record)

    def test_non_code_keeps_its_review_choice_without_new_code_stages(self):
        for kind in ("research", "infra"):
            for choice in ("required", "skipped"):
                with self.subTest(kind=kind, choice=choice):
                    self.task.update(type=kind, state="validate", review=choice)
                    with (
                        mock.patch.object(runtime_module, "_advance_review_verdict", return_value=None),
                        mock.patch.object(runtime_module, "_start_review", return_value={"action": "review-started"}) as launch,
                        mock.patch.object(runtime_module, "_park_green_verdict", return_value={"to": "assessment"}) as park,
                    ):
                        self.record.state = "validate"
                        self.advance()
                    self.assertEqual(launch.call_count, int(choice == "required"))
                    self.assertEqual(park.call_count, int(choice == "skipped"))
                    self.assertEqual(self.task["state"], "validate")
        self.runtime.host.gate_check.assert_not_called()


class ReleaseAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.task = {"ref": "sample-1", "type": "code", "state": "assessment", "review": "required", "sprint": ""}
        self.writer = object.__new__(TaskWriter)
        self.writer.client = mock.Mock()
        self.writer.reader = mock.Mock()
        self.writer.reader.show.side_effect = lambda _: copy.deepcopy(self.task)
        self.writer.audit = mock.Mock()
        self.events = [
            {"kind": "card.reported", "record_type": "board.protocol_event", "actor": {"role": "worker"}, "data": {"marker": "report:done"}},
            {"kind": "card.verdict", "record_type": "board.protocol_event", "actor": {"role": "reviewer"}, "data": {"marker": "review:green"}},
        ]
        self.writer.audit.events.side_effect = lambda _: self.events
        self.gate = AcceptedGreenGate.accept({}, current_sha="a" * 40, gate_mode="none", noop=False)

    def admit(self, gate=None, decision=""):
        self.writer.admit_release(role="dispatcher", actor="dispatcher", reference="sample-1", decision=decision,
                                  automatic_release=gate)

    def test_automatic_release_needs_green_policy_report_and_verdict(self):
        self.admit(self.gate)
        for gate in (None, AcceptedGreenGate(None, False), True):
            with self.subTest(gate=gate), self.assertRaises(TaskError):
                self.admit(gate)
        self.events.pop()
        with self.assertRaises(TaskError):
            self.admit(self.gate)
        self.task["review"] = "skipped"
        self.admit(self.gate)
        self.events[0]["data"]["marker"] = "report:partial"
        with self.assertRaises(TaskError):
            self.admit(self.gate)

    def test_actual_kind_observer_and_unknown_context_do_not_get_the_exception(self):
        self.task["type"] = "infra"
        with self.assertRaises(TaskError):
            self.admit(self.gate)
        self.task.update(type="code", sprint="sprint:1")
        with mock.patch("ummanu.sprints.SprintReader") as reader:
            for context in ({"status": "open", "observer": {"kind": "head", "profile": "observer"}},
                            {"status": "open", "observer": None}, {}):
                reader.return_value.show.return_value = context
                with self.subTest(context=context), self.assertRaises(TaskError):
                    self.admit(self.gate)
            reader.return_value.show.side_effect = TaskError("not_found", "sprint missing", 3)
            with self.assertRaises(TaskError):
                self.admit(self.gate)

    def test_no_sprint_explicit_none_and_closed_sprint_preserve_automatic_release(self):
        self.admit(self.gate)
        self.task["sprint"] = "sprint:1"
        with mock.patch("ummanu.sprints.SprintReader") as reader:
            for context in ({"status": "open", "observer": {"kind": "none"}},
                            {"status": "open"}, {"status": "closed"}, {"status": "stopped"}):
                reader.return_value.show.return_value = context
                self.admit(self.gate)

    def test_code_terminal_admission_requires_assessment_and_real_decision_for_managed_cards(self):
        for state in ("ci", "review", "validate"):
            self.task["state"] = state
            with self.subTest(state=state), self.assertRaises(TaskError):
                self.admit(self.gate)
        self.task["state"] = "assessment"
        self.writer._decision_recorded = mock.Mock(return_value=False)
        with self.assertRaises(TaskError):
            self.admit(decision="accept")
        self.writer._decision_recorded.return_value = True
        self.admit(decision="accept")

    def test_refused_terminal_authority_precedes_merge_and_teardown(self):
        runtime = mock.Mock()
        runtime.writer.admit_release.side_effect = TaskError("decision_required", "observer decision required", 3)
        record = DispatcherRecord.from_json({"state": "assessment"})
        record.gate_state = "green"
        runtime.host.mode = "noop"
        with (
            mock.patch.object(gate_lifecycle, "_validation_ci", return_value="none"),
            self.assertRaises(TaskError),
        ):
            release_lifecycle.release_effect(runtime, self.task, record, {"sample-1": record}, {}, "tick",
                                             step="assessment", move_reason="release")
        runtime.host.complete_green.assert_not_called()
        runtime.host.teardown.assert_not_called()

    def test_unreadable_sprint_is_not_no_observer(self):
        read = mock.Mock(side_effect=TaskError("not_found", "missing sprint", 3))
        with self.assertRaises(TaskError):
            observer_managed({"sprint": "sprint:1"}, read)


class OperationalConsumerTests(unittest.TestCase):
    def test_stage_admission_holds_the_capacity_lock_during_the_board_effect(self):
        host = object.__new__(SqlBoardHost)
        host.data_dir = "/unused"
        held = set()

        @contextlib.contextmanager
        def lock(_root, reference, **kwargs):
            held.add(reference)
            try:
                yield
            finally:
                held.remove(reference)

        def transition(*args, **kwargs):
            self.assertIn("capacity", held)
            self.assertIn("sample-1", held)
            return "moved"

        host._transition = transition
        with mock.patch("ummanu.board.sql_host.reference_lock", side_effect=lock):
            for stage in (CardState.CI, CardState.REVIEW):
                operation = TransitionRequest(EntityKind.CARD, "sample-1", stage, Actor("dispatcher", "d"), "stage")
                self.assertEqual(host.transition(operation), "moved")
        self.assertEqual(held, set())

    def test_restart_budget_candidates_retain_new_stages_in_both_event_shapes(self):
        for stage in ("ci", "review"):
            for event in (
                {"kind": "moved", "outcome": "success", "payload": {"from": stage, "to": "ready"}},
                {"kind": "card.returned", "record_type": "board.protocol_event",
                 "transition": {"source": stage, "target": "ready"}, "data": {}},
            ):
                event.update(ref="sample-1", request_id="restart")
                self.assertTrue(is_candidate(event))
                self.assertEqual(_budget_event_type(event), "preempt")
