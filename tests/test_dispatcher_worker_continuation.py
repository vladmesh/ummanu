"""Ownership, durable ordering and replay contracts for retained/red continuation."""

from __future__ import annotations

import ast
import copy
from dataclasses import replace
import inspect
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest import mock

from ummanu.board import Actor, BoardEventPending, Card, CardState, EntityKind, SqlBoardHost, TransitionRequest
from ummanu.board.fake import MemoryAudit
from ummanu.dispatch import attempt_accounting
from ummanu.dispatch import worker_continuation as continuation_module
from ummanu.dispatch.state import DispatcherRecord, PersistedGateReceipt
from ummanu.dispatch.worker_lifecycle import (
    BUSY_RETRY_INITIAL_SECONDS,
    CONTINUATION_NO_PROGRESS_BUSY_ATTEMPTS,
    ContinuationRecoveryRung,
    WorkerContinuationLiveness,
    WorkerContinuationStage,
)


class WorkerContinuationBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.task = {"ref": "sample-1", "type": "code", "comments": []}
        self.record = DispatcherRecord(
            worker="worker-1",
            workspace="/unused",
            handle="worker-1",
            head="codex",
            review_head="claude",
            attempt_id="held-attempt",
            comment_baseline=0,
            review_baseline=0,
            state="validate",
            claimed_at=1.0,
            report_generation=3,
            attempt_round=1,
        )
        self.record.worker_head_run = {
            "run_id": "retained-run",
            "workspace": "/unused",
            "task_ref": {"kind": "card", "ref": "sample-1"},
            "role": "worker",
            "spec": {"profile_id": "codex", "adapter": "codex"},
        }
        self.records = {"sample-1": self.record}
        self.payload = {}
        self.runtime = mock.Mock()
        self.runtime.owner = "dispatcher"
        self.runtime.reader.show.return_value = self.task
        self.runtime._stop_worker_confirmed.return_value = None
        self.runtime.open_worker_round.side_effect = self.open_round
        self.runtime.host.provider_progress.side_effect = self.provider_evidence
        self.accounting = mock.Mock()
        self.accounting_patcher = mock.patch.object(
            continuation_module, "attempt_accounting", self.accounting
        )
        self.accounting_patcher.start()
        self.addCleanup(self.accounting_patcher.stop)

    def open_round(self, record, *, round_number):
        record.attempt_round = round_number

    def provider_evidence(self, *_args, cursor="cursor-1"):
        liveness = self.record.worker_continuation_liveness
        return {
            "state": "observed",
            "admission": "accepted",
            "head_run_id": liveness.head_run_id,
            "head_run_fingerprint": liveness.head_run_fingerprint,
            "source": "codex-session",
            "source_fingerprint": "a" * 32,
            "cursor": cursor,
        }

    def retain(self):
        self.record.worker_continuation.begin_retention(1.0)
        self.record.worker_continuation.confirm_validation_move()

    def pending(self):
        self.retain()
        self.record.worker_continuation.begin_delivery("gate", 2.0)
        self.record.worker_continuation_liveness = WorkerContinuationLiveness.begin(
            self.record.worker_head_run
        )
        self.record.worker_continuation_liveness.observe_provider(
            self.provider_evidence(), 2.0, head_run=self.record.worker_head_run
        )

    def begin(self):
        return continuation_module.begin_red_transition(
            self.runtime,
            self.task,
            self.record,
            self.records,
            self.payload,
            "tick-attempt",
            phase="gate",
            move_reason="gate is red",
            verdict_outcome="gate_red",
        )

    def complete(self):
        return continuation_module.complete_red_transition(
            self.runtime,
            self.task,
            self.record,
            self.records,
            self.payload,
            "tick-attempt",
            ref="sample-1",
        )

    def recover(self, marker=None):
        return continuation_module.recover_worker_continuation(
            self.runtime,
            self.task,
            self.record,
            self.records,
            self.payload,
            "tick-attempt",
            marker=marker,
        )

    def deliver(self):
        return continuation_module._deliver_red_continuation(
            self.runtime, self.task, self.record, self.records, self.payload, "tick-attempt", phase="gate"
        )

    def restart(self):
        return continuation_module._restart_red_worker(
            self.runtime,
            self.task,
            self.record,
            self.records,
            self.payload,
            "tick-attempt",
            phase="gate",
            continuation_reason="session unavailable",
        )

    def test_no_pending_delivery_leaves_report_and_wait_to_their_owners(self) -> None:
        self.assertIsNone(self.recover("report:done"))
        self.assertEqual(self.runtime.mock_calls, [])

    def test_recovered_observer_rework_keeps_frozen_reason_and_worker_instruction(self):
        self.runtime.audit.events.return_value = []
        body = "Repair the canonical defect and preserve its evidence."
        self.record.worker_continuation.begin_red_transition(
            "review", 0, f"Observer decision: rework. {body}", "red", "rework",
            reserved_generation=4, decision_body=body,
            decision_protocol_prerequisites=("repair-contract",))
        # A released pending transition may still carry the redundant move body.
        self.record = DispatcherRecord.from_json(self.record.to_json())
        self.records["sample-1"] = self.record
        with mock.patch.object(continuation_module, "_deliver_red_continuation", return_value={"action": "delivered"}):
            self.assertEqual(self.complete()["action"], "delivered")
        move = self.accounting.terminal_effect.call_args.kwargs
        self.assertEqual(move["decision"], "rework")
        self.assertEqual(move["reason"], f"Observer decision: rework. {body}")
        self.assertEqual(self.record.report_decision, body)
        self.assertEqual(self.record.report_protocol_prerequisites, ("repair-contract",))

    def test_new_observer_rework_freezes_pointer_before_move(self):
        from ummanu.dispatch import assessment_decision

        self.runtime.audit.events.return_value = []
        body = "Repair the canonical defect and preserve its evidence."
        frozen = []
        self.runtime.save_records.side_effect = lambda *_: frozen.append(self.record.to_json())
        with mock.patch.object(continuation_module, "_deliver_red_continuation", return_value={}):
            assessment_decision.rework_parked(
                self.runtime, self.task, self.record, self.records, self.payload, "tick",
                reason=body, protocol_prerequisites=("repair-contract",))
        reason = frozen[0]["worker_continuation"]["move_reason"]
        self.assertIn("[decision:rework]", reason)
        self.assertNotIn(body, reason)
        self.assertEqual(self.accounting.terminal_effect.call_args.kwargs["reason"], reason)
        # A retry cannot replace the pointer from its frozen intent with a newer audit read.
        self.runtime.audit.events.side_effect = AssertionError("re-read frozen decision")
        with mock.patch.object(continuation_module, "_deliver_red_continuation", return_value={}):
            self.complete()
        self.assertEqual(self.accounting.terminal_effect.call_args.kwargs["reason"], reason)
        self.assertEqual(self.record.report_decision, body)

    def test_release_and_reslice_routing_keep_only_the_canonical_pointer(self):
        from ummanu.dispatch import assessment_decision, release_lifecycle

        self.runtime.audit.events.return_value = []
        self.task["type"] = "research"
        body = "Canonical observer reasoning that remains on the card."
        with (mock.patch.object(release_lifecycle, "transfer_research_report", return_value=None),
              mock.patch.object(release_lifecycle, "release_effect", return_value={"action": "released"}) as effect):
            release_lifecycle.release_parked(self.runtime, self.task, self.record, self.records,
                                             self.payload, "tick", reason=body)
        reason = effect.call_args.kwargs["move_reason"]
        self.assertIn("[decision:release]", reason)
        self.assertNotIn(body, reason)
        with mock.patch.object(assessment_decision, "attempt_accounting", self.accounting):
            assessment_decision.reslice_parked(self.runtime, self.task, self.record, self.records,
                                               self.payload, "tick", reason=body)
        move = self.accounting.terminal_effect.call_args.kwargs
        self.assertEqual((move["decision"], move["target"]), ("reslice", "blocked"))
        self.assertIn("[decision:reslice]", move["reason"])
        self.assertNotIn(body, move["reason"])

    def test_failed_red_intent_save_prevents_board_and_host_effects(self) -> None:
        self.runtime.save_records.side_effect = OSError("state unavailable")
        with self.assertRaisesRegex(OSError, "state unavailable"):
            self.begin()
        self.assertTrue(self.record.worker_continuation.red_transition_pending)
        self.assertEqual(self.record.worker_continuation.reserved_generation, 4)
        self.accounting.terminal_effect.assert_not_called()
        self.runtime.host.resume_worker.assert_not_called()
        self.runtime._stop_worker_confirmed.assert_not_called()

    def test_red_move_replay_keeps_its_request_and_reserved_generation(self) -> None:
        self.record.gate_attestation = PersistedGateReceipt({"previous_round": "receipt"})
        snapshots = []
        self.runtime.save_records.side_effect = lambda *_: snapshots.append(
            (self.record.worker_continuation.stage, self.record.report_generation)
        )
        with (
            mock.patch.object(
                continuation_module, "_deliver_red_continuation", side_effect=OSError("died after move")
            ),
            self.assertRaisesRegex(OSError, "died after move"),
        ):
            self.begin()
        self.assertEqual(snapshots[0], (WorkerContinuationStage.RED_TRANSITION_PENDING, 3))
        self.assertIsInstance(self.record.gate_attestation, PersistedGateReceipt)
        self.assertEqual(self.record.gate_attestation.to_json(), {})
        first_request = self.accounting.terminal_effect.call_args.kwargs["request_id"]
        self.assertEqual(self.record.report_generation, 4)
        with mock.patch.object(continuation_module, "_deliver_red_continuation", return_value={"ok": True}):
            self.assertEqual(self.complete(), {"ok": True})
        self.assertEqual(self.accounting.terminal_effect.call_args.kwargs["request_id"], first_request)
        self.assertEqual(self.record.report_generation, 4)
        self.assertEqual(self.record.worker_continuation.reserved_generation, 4)

    def test_failed_delivery_boundary_save_does_not_wake_retained_worker(self) -> None:
        self.retain()
        self.runtime.save_records.side_effect = OSError("delivery intent unavailable")
        with self.assertRaisesRegex(OSError, "delivery intent unavailable"):
            self.deliver()
        self.assertTrue(self.record.worker_continuation.delivery_pending)
        self.runtime.host.confirm_worker_retained.assert_called_once()
        self.runtime.host.resume_worker.assert_not_called()

    def test_delivery_is_saved_before_wake_and_confirmation_before_opening_round(self) -> None:
        self.retain()
        events = []
        self.runtime.save_records.side_effect = lambda *_: events.append(
            ("save", self.record.worker_continuation.stage)
        )
        self.runtime.host.resume_worker.side_effect = lambda *_: events.append(
            ("wake", self.record.worker_continuation.stage)
        )
        result = self.deliver()
        self.assertEqual(result["action"], "gate-red-reused-worker")
        self.assertEqual(
            events[:3],
            [
                ("save", WorkerContinuationStage.DELIVERY_PENDING),
                ("wake", WorkerContinuationStage.DELIVERY_PENDING),
                ("save", WorkerContinuationStage.DELIVERY_CONFIRMED),
            ],
        )
        self.assertEqual(self.record.attempt_round, 2)
        self.runtime.open_worker_round.assert_called_once_with(self.record, round_number=2)
        self.runtime._stop_worker_confirmed.assert_not_called()

    def test_report_proves_delivery_and_confirmed_replay_never_resends(self) -> None:
        for marker in ("report:done", "report:blocked"):
            with self.subTest(marker=marker):
                self.setUp()
                self.pending()
                with (
                    mock.patch.object(
                        continuation_module,
                        "_finish_retained_worker_resume",
                        side_effect=OSError("round write died"),
                    ),
                    self.assertRaisesRegex(OSError, "round write died"),
                ):
                    self.recover(marker)
                self.assertTrue(self.record.worker_continuation.delivery_confirmed)
                self.runtime.save_records.assert_called_once()
                self.assertEqual(self.recover(marker)["action"], "gate-red-reused-worker")
                self.runtime.open_worker_round.assert_called_once_with(self.record, round_number=2)
                self.runtime.host.resume_worker.assert_not_called()
                self.runtime.host.provider_progress.assert_not_called()

    def test_unconfirmed_stop_forbids_replacement(self) -> None:
        refused = {"status": "degraded", "action": "head-stop-unconfirmed"}
        self.runtime._stop_worker_confirmed.return_value = refused
        with mock.patch.object(continuation_module, "_write_worker_relaunch_intent") as intent:
            self.assertIs(self.restart(), refused)
        intent.assert_not_called()
        self.runtime.host.restart_worker.assert_not_called()
        self.runtime.open_worker_round.assert_not_called()

    def test_failed_relaunch_intent_restores_transition_debt(self) -> None:
        self.record.worker_continuation.begin_red_transition(
            "gate", 0, "red", "gate_red", reserved_generation=4
        )
        held = copy.deepcopy(self.record.worker_continuation)
        with (
            mock.patch.object(
                continuation_module, "_write_worker_relaunch_intent", return_value="state unavailable"
            ),
            mock.patch.object(continuation_module, "_bring_up_worker_head") as bring_up,
        ):
            result = self.restart()
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(self.record.worker_continuation, held)
        bring_up.assert_not_called()
        self.runtime.open_worker_round.assert_not_called()

    def test_fresh_provider_progress_outranks_busy_backoff_without_replacement(self) -> None:
        self.pending()
        self.record.worker_continuation.busy_next_at = 1000.0
        self.record.worker_continuation_liveness.busy_attempts = 2
        self.runtime.host.provider_progress.side_effect = lambda *_: self.provider_evidence(cursor="cursor-2")
        held_run = copy.deepcopy(self.record.worker_head_run)
        with mock.patch.object(continuation_module.time, "time", return_value=100.0):
            result = self.recover()
        self.assertEqual(result["action"], "gate-red-worker-busy")
        self.assertEqual(self.record.worker_continuation.busy_next_at, 100.0 + BUSY_RETRY_INITIAL_SECONDS)
        self.assertEqual(self.record.worker_continuation_liveness.busy_attempts, 0)
        self.assertEqual(self.record.worker_head_run, held_run)
        self.runtime.host.resume_worker.assert_not_called()
        self.runtime._stop_worker_confirmed.assert_not_called()

    def test_not_due_busy_delivery_does_not_touch_the_pane(self) -> None:
        self.pending()
        self.record.worker_continuation.busy_next_at = 200.0
        with mock.patch.object(continuation_module.time, "time", return_value=100.0):
            self.assertEqual(self.recover()["action"], "gate-red-worker-busy")
        self.runtime.host.resume_worker.assert_not_called()
        self.runtime.host.confirm_worker_retained.assert_not_called()
        self.runtime._stop_worker_confirmed.assert_not_called()

    def test_identity_mismatch_blocks_without_rebinding_or_recovering_head(self) -> None:
        self.pending()
        held_run = copy.deepcopy(self.record.worker_head_run)
        self.runtime.host.provider_progress.side_effect = None
        self.runtime.host.provider_progress.return_value = {
            "state": "identity_mismatch",
            "reason": "other head",
        }
        self.assertEqual(self.recover()["action"], "gate-red-continuation-liveness-unavailable")
        self.assertEqual(self.record.worker_head_run, held_run)
        self.assertTrue(self.record.worker_continuation_liveness.source_rejected)
        self.runtime.host.resume_worker.assert_not_called()
        self.runtime.host.safe_recover_worker_continuation.assert_not_called()
        self.runtime._stop_worker_confirmed.assert_not_called()

    def test_recovery_response_window_precedes_any_pane_interaction(self) -> None:
        self.pending()
        self.record.worker_continuation_liveness.safe_recovery_response_window(90.0, 30.0)
        with mock.patch.object(continuation_module.time, "time", return_value=100.0):
            self.assertEqual(self.recover()["action"], "gate-red-worker-recovery-window")
        self.runtime.host.resume_worker.assert_not_called()
        self.runtime.host.confirm_worker_retained.assert_not_called()

    def stall(self):
        self.pending()
        self.record.worker_continuation_liveness.observe_provider(
            self.provider_evidence(), 3.0, head_run=self.record.worker_head_run
        )
        self.record.worker_continuation_liveness.busy_attempts = CONTINUATION_NO_PROGRESS_BUSY_ATTEMPTS

    def advance_stalled(self):
        return continuation_module._advance_no_progress_continuation(
            self.runtime, self.task, self.record, self.records, self.payload, "tick-attempt", phase="gate"
        )

    def test_failed_safe_recovery_intent_save_never_calls_capability(self) -> None:
        self.stall()
        self.runtime.save_records.side_effect = OSError("recovery intent unavailable")
        with self.assertRaisesRegex(OSError, "recovery intent unavailable"):
            self.advance_stalled()
        self.assertEqual(
            self.record.worker_continuation_liveness.recovery_rung,
            ContinuationRecoveryRung.SAFE_RECOVERY_PENDING,
        )
        self.runtime.host.safe_recover_worker_continuation.assert_not_called()
        self.runtime._stop_worker_confirmed.assert_not_called()

    def test_replayed_safe_recovery_intent_is_not_spent_twice(self) -> None:
        self.stall()
        self.record.worker_continuation_liveness.begin_safe_recovery(3.0)
        refused = {"status": "degraded", "action": "head-stop-unconfirmed"}
        self.runtime._stop_worker_confirmed.return_value = refused
        self.assertIs(self.advance_stalled(), refused)
        self.runtime.host.safe_recover_worker_continuation.assert_not_called()
        self.assertEqual(self.record.worker_continuation_liveness.terminal_outcome, "replacement")
        self.assertEqual(self.record.worker_continuation_liveness.recovery_attempts, 1)


class ContinuationOwnershipTests(unittest.TestCase):
    def test_implementation_is_owned_by_the_package_not_runtime_callbacks(self) -> None:
        root = Path(__file__).resolve().parents[1]
        runtime_source = (root / "src/ummanu/dispatch/runtime.py").read_text(encoding="utf-8")
        runtime_tree = ast.parse(runtime_source)
        runtime = next(
            n for n in runtime_tree.body if isinstance(n, ast.ClassDef) and n.name == "DispatcherRuntime"
        )
        module_tree = ast.parse(inspect.getsource(continuation_module))
        helpers = {
            "_begin_red_transition",
            "_complete_red_transition",
            "_deliver_red_continuation",
            "_observe_retained_continuation_progress",
            "_block_unadmitted_continuation_liveness",
            "_continuation_recovery_window",
            "_advance_no_progress_continuation",
            "_finish_retained_worker_resume",
            "_restart_red_worker",
            "_record_worker_continuation",
        }
        runtime_names = {n.name for n in runtime.body if isinstance(n, ast.FunctionDef)}
        self.assertFalse(helpers & runtime_names)
        callbacks = {
            n.attr
            for n in ast.walk(module_tree)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "runtime"
        }
        self.assertFalse(helpers & callbacks)
        for helper in (
            "_retained_worker_busy_deferred",
            "_retained_worker_recovery_window",
            "_continuation_no_progress_evidence",
        ):
            self.assertNotIn(f"def {helper}(", runtime_source)
        owned = {n.name for n in module_tree.body if isinstance(n, ast.FunctionDef)}
        self.assertTrue(
            {"begin_red_transition", "complete_red_transition", "recover_worker_continuation"} <= owned
        )
        self.assertNotIn("ummanu.dispatch.runtime", inspect.getsource(continuation_module))
        advance = next(
            n for n in runtime.body if isinstance(n, ast.FunctionDef) and n.name == "_advance_worker"
        )
        advance_source = ast.get_source_segment(runtime_source, advance)
        names = [
            "_complete_red_transition(",
            "_worker_report_marker(",
            "_recover_worker_continuation(",
            "_handle_worker_report(",
            "_wait_watchdog(self, ",
        ]
        positions = [advance_source.index(name) for name in names]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("if continuation.delivery_pending:", advance_source)


class DecisionNativeReplayTests(unittest.TestCase):
    """Exercise the native host's strict replay seam without opening a SQL store."""

    def setUp(self):
        self.enterContext(mock.patch.object(attempt_accounting, "_attempt_outcome_obligation", return_value=None))
        self.reset_native()

    def reset_native(self):
        self.audit = MemoryAudit()
        self.host = SqlBoardHost(mock.sentinel.client, data_dir="/unused", audit=self.audit)
        self.card = Card("sample-1", "Decision replay", CardState.ASSESSMENT)
        self.host.read = mock.Mock(side_effect=lambda *_: self.card)
        self.host._move_card = mock.Mock(side_effect=self.move_card)
        self.runtime = SimpleNamespace(
            owner="dispatcher", writer=SimpleNamespace(board_host=self.host, move=self.move))
        self.record = DispatcherRecord(
            worker="worker", workspace="/unused", handle="worker", head="codex",
            review_head="claude", comment_baseline=0, review_baseline=0,
            state="assessment", claimed_at=1.0, attempt_id="held-attempt")

    def move_card(self, _card, target):
        self.card = replace(self.card, state=target)

    def move(self, **kwargs):
        result = self.host.transition(TransitionRequest(
            EntityKind.CARD, kwargs["reference"], CardState(kwargs["target"]),
            Actor(kwargs["role"], kwargs["actor"]), kwargs["reason"],
            request_id=kwargs["request_id"], data={"terminal_taxonomy": kwargs["terminal_taxonomy"]}))
        return {"event_id": result.event.event_id}

    def effect(self, decision, reason, request_id="decision-move"):
        target = "done" if decision == "release" else "blocked"
        return attempt_accounting.terminal_effect(
            self.runtime, {"ref": "sample-1"}, self.record, target=target,
            reason=reason, request_id=request_id, terminal_state=target,
            disposition=decision, decision=decision)

    def test_release_and_reslice_replay_owned_full_reason_and_new_pointer(self):
        for decision in ("release", "reslice"):
            for original in ("Observer decision: " + decision + ". Full released reason.",
                             "Observer decision: " + decision + ". See canonical-event."):
                for committed in (False, True):
                    with self.subTest(decision=decision, original=original, committed=committed):
                        self.reset_native()
                        # The native host stages its own intent and performs the move; a failed
                        # journal append leaves that exact owned record for recovery.
                        with mock.patch.object(self.audit, "append", side_effect=OSError("lost commit")):
                            with self.assertRaises(BoardEventPending):
                                self.effect(decision, original)
                        owned = self.host.canon.event("decision-move")
                        if committed:
                            self.host.canon.commit("decision-move", owned)
                        self.effect(decision, "Observer decision: " + decision + ". See newer-event.")
                        self.assertEqual(self.host.canon.committed("decision-move"), owned)
                        self.assertEqual(self.host._move_card.call_count, 1)
                        self.assertEqual(self.audit.status(), {"ok": True, "pending": 0})

    def test_released_continuation_replays_native_move_after_lost_state_save(self):
        body = "Repair the canonical defect and preserve its evidence."
        original = f"Observer decision: rework. {body}"
        self.record.worker_continuation.begin_red_transition(
            "review", 0, original, "red", "rework", reserved_generation=2,
            decision_body=body, decision_protocol_prerequisites=("repair-contract",))
        frozen = self.record.to_json()
        self.runtime.reader = SimpleNamespace(show=lambda _: {"ref": "sample-1", "comments": []})
        self.runtime.save_records = mock.Mock(side_effect=OSError("lost state save"))
        records = {"sample-1": self.record}
        with self.assertRaisesRegex(OSError, "lost state save"):
            continuation_module.complete_red_transition(
                self.runtime, {"ref": "sample-1"}, self.record, records, {}, "tick", ref="sample-1")
        events = self.host.canon.events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].reason, original)
        self.record = DispatcherRecord.from_json(frozen)
        records["sample-1"] = self.record
        self.runtime.save_records.side_effect = None
        with mock.patch.object(continuation_module, "_deliver_red_continuation", return_value={}):
            continuation_module.complete_red_transition(
                self.runtime, {"ref": "sample-1"}, self.record, records, {}, "new-tick", ref="sample-1")
        self.assertEqual(self.host.canon.events(), events)
        self.assertEqual(self.host._move_card.call_count, 1)
        self.assertEqual(self.record.report_decision, body)
        self.assertEqual(self.record.report_protocol_prerequisites, ("repair-contract",))
        self.assertEqual(self.record.report_generation, 2)

    def test_mismatched_native_identity_and_data_still_refuse(self):
        self.effect("release", "original pointer")
        owned = self.host.canon.committed("decision-move")
        for field, value in (("ref", "other-1"), ("actor", Actor("dispatcher", "other")),
                             ("target_state", "blocked"), ("data", {"foreign": True})):
            with self.subTest(field=field):
                foreign = replace(owned, **{field: value})
                with mock.patch.object(self.host.canon, "event", return_value=foreign):
                    with self.assertRaisesRegex(ValueError, "another operation or payload"):
                        self.effect("release", "new pointer")
                self.assertEqual(self.host.canon.committed("decision-move"), owned)
                self.assertEqual(self.host._move_card.call_count, 1)


if __name__ == "__main__":
    unittest.main()
