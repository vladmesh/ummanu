"""Admission and retry decisions without a backend or worker process."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any
from unittest import mock

from ummanu.board.audit_contract import PROTOCOL_EVENT_RECORD_TYPE
from ummanu.board.models import EventKind
from ummanu.dispatch import claim, production
from ummanu.dispatch.host import _blocked_actions_and_their_infrastructure_twins
from ummanu.dispatch.state import DispatcherRecord, attempt_request_id, is_claim_skip
from ummanu.dispatch.watchdog import wait_cycle_token
from ummanu.tasks import TaskError


class ClaimBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.task = {"ref": "sample-1", "type": "code", "state": "ready", "comments": []}
        self.runtime = mock.Mock()
        self.runtime.owner = "dispatcher"
        self.runtime.reader.show.return_value = self.task
        self.runtime._journal_round.return_value = 0
        self.runtime._end_review_pane_confirmed.return_value = None
        self.runtime._stop_worker_confirmed.return_value = None
        self.runtime.audit.events.return_value = []
        self.runtime.audit.committed_event.side_effect = lambda request_id: next(
            (event for event in self.runtime.audit.events.return_value if event["request_id"] == request_id),
            None,
        )
        self.records = {}
        self.payload = {}
        choice = SimpleNamespace(resolved=True, head="head", preferred="head", substituted=False)
        for name, value in (
            ("resolve_head", choice), ("_failover_collapse", None),
            ("_sprint_admission_refusal", None), ("live_root_project_refusal", ""),
            ("_broad_check_contract_verdict", None), ("_comment_head_failover", None),
        ):
            patcher = mock.patch.object(claim, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def event(request_id, kind, *, role="dispatcher", source="", target="", data=None, event_id=""):
        event: dict[str, Any] = {
            "record_type": PROTOCOL_EVENT_RECORD_TYPE,
            "request_id": request_id,
            "event_id": event_id or f"evt-{request_id}",
            "kind": kind.value,
            "ref": "sample-1",
            "actor": {"role": role, "id": role},
            "data": data or {},
        }
        if target:
            event["transition"] = {"source": source, "target": target}
        return event

    def block(self, action, suffix="", *, attempt="old-attempt", source="in_progress", role="dispatcher"):
        return self.event(
            attempt_request_id(attempt, action, "sample-1", suffix), EventKind.CARD_BLOCKED,
            role=role, source=source, target="blocked",
        )

    def claimed(self, attempt="old-attempt"):
        return self.event(
            attempt_request_id(attempt, "claim", "sample-1"), EventKind.CARD_STARTED,
            source="ready", target="in_progress",
        )

    def report(self, kind, generation, attempt="old-attempt"):
        return self.event(
            attempt_request_id(attempt, f"worker-report-{kind}", "sample-1", str(generation)),
            EventKind.CARD_REPORTED, role="worker",
        )

    def rework_round(self, body="Narrow the reader to the declared states.", attempt="old-attempt"):
        """A parked review, the observer's rework decision on it, and the round it opened."""
        park = self.event(
            attempt_request_id(attempt, "review-park", "sample-1"), EventKind.CARD_ASSESSED,
            source="validate", target="assessment", event_id="evt-park",
        )
        decided = self.event(
            "observer-rework", EventKind.CARD_DECIDED, role="observer", event_id="evt-decision",
            data={"decision": "rework", "body": body, "assessment_visit": "evt-park"},
        )
        moved = self.event(
            attempt_request_id(attempt, "review-red", "sample-1", "4"), EventKind.CARD_REWORKED,
            source="assessment", target="in_progress",
        )
        return [park, decided, moved]

    def prepare(self, **kwargs):
        return claim._prepare_claim(
            self.runtime, self.task, self.records, self.payload, "old-attempt", **kwargs
        )

    def test_every_dispatcher_block_of_a_claimed_round_requires_its_workspace_and_a_new_attempt(self):
        cycle = wait_cycle_token(DispatcherRecord.from_json({"comment_baseline": 7, "worker_respawns": 1, "review_respawns": 2}))
        cases = [
            *[(action, suffix, "in_progress", claim.STAGE_WORKER)
              for action in _blocked_actions_and_their_infrastructure_twins(
                  "worker-wait-stall", "worker-respawn-blocked", "bringup-blocked", "worker-blocked")
              for suffix in ("", cycle)],
            ("stale-done-blocked", "2", "in_progress", claim.STAGE_WORKER),
            ("stale-done-infrastructure-blocked", "2", "in_progress", claim.STAGE_WORKER),
            ("headless-recovery-refused", "episode-1", "in_progress", claim.STAGE_WORKER),
            ("gate-infrastructure-reruns-exhausted", "2", "validate", claim.STAGE_GATE),
            ("merge-gate-infrastructure-reruns-exhausted", "2", "validate", claim.STAGE_GATE),
            ("merge-gate-pending-stall", "", "assessment", claim.STAGE_GATE),
            ("initial-gate-receipt-blocked", "", "validate", claim.STAGE_GATE),
            ("rejected-candidate-blocked", "", "validate", claim.STAGE_GATE),
            ("red-review-ceiling", "", "validate", claim.STAGE_REVIEW),
            ("red-review-repeated-blockers", "", "validate", claim.STAGE_REVIEW),
            ("review-infrastructure-blocked", cycle, "validate", claim.STAGE_REVIEW),
            ("review-wait-stall", cycle, "validate", claim.STAGE_REVIEW),
            ("assessment-reslice", "", "assessment", claim.STAGE_REVIEW),
        ]
        for action, suffix, source, stage in cases:
            with self.subTest(action=action, suffix=suffix):
                self.records.clear()
                self.payload.clear()
                self.runtime.audit.events.return_value = [self.claimed(), self.block(action, suffix, source=source)]
                self.assertEqual(claim.resume_point(self.runtime, self.task).stage, stage)
                prepared = self.prepare()
                self.assertIsInstance(prepared, claim.ClaimHandoff)
                self.assertTrue(prepared.require_existing_workspace)
                self.assertNotEqual(prepared.record.attempt_id, "old-attempt")
                self.assertEqual(self.payload["attempts"][0]["attempt_id"], prepared.record.attempt_id)
        self.runtime.audit.committed_event.assert_not_called()

    def test_released_generic_move_to_blocked_is_a_workspace_retry(self):
        event = {"kind": "moved", "ref": "sample-1", "outcome": "success",
                 "actor": {"role": "dispatcher", "id": "dispatcher"},
                 "payload": {"from": "in_progress", "to": "blocked"},
                 "request_id": attempt_request_id("old-attempt", "worker-blocked", "sample-1")}
        self.runtime.audit.events.return_value = [event]
        prepared = self.prepare()
        self.assertTrue(prepared.require_existing_workspace)
        self.assertNotEqual(prepared.record.attempt_id, "old-attempt")

    def test_a_block_of_an_earlier_attempt_is_resumed_from_any_claim_path(self):
        self.runtime.audit.events.return_value = [self.block("worker-blocked", attempt="another-attempt")]
        prepared = self.prepare(fresh_attempt=True)
        self.assertTrue(prepared.require_existing_workspace)
        self.assertEqual(prepared.record.attempt_id, "old-attempt")

    def test_a_round_opened_since_or_a_block_by_hand_is_no_interrupted_round(self):
        for events in (
            [self.block("worker-blocked"), self.claimed("next-attempt")],
            [self.claimed(), self.block("worker-blocked", role="po")],
        ):
            with self.subTest(request=[event["request_id"] for event in events]):
                self.records.clear()
                self.runtime.audit.events.return_value = events
                self.assertEqual(claim.resume_point(self.runtime, self.task), claim.ResumePoint())
                prepared = self.prepare()
                self.assertEqual(prepared.record.attempt_id, "old-attempt")
                self.assertFalse(prepared.require_existing_workspace)
                self.assertEqual(prepared.record.report_generation, 1)

    def test_admission_refusals_open_new_attempt_without_requiring_a_checkout(self):
        for action in _blocked_actions_and_their_infrastructure_twins(
            "sprint-reservation-blocked", "live-root-project-blocked",
            "contract-preflight-blocked", "git-access-preflight-blocked", "codex-fanout-blocked",
        ):
            with self.subTest(action=action):
                self.records.clear()
                self.runtime.audit.events.return_value = [self.claimed(), self.block(action)]
                self.assertEqual(claim.resume_point(self.runtime, self.task).stage, claim.STAGE_ADMISSION)
                prepared = self.prepare()
                self.assertNotEqual(prepared.record.attempt_id, "old-attempt")
                self.assertFalse(prepared.require_existing_workspace)

    def test_resumed_round_carries_its_decision_generation_and_a_baseline_past_old_verdicts(self):
        self.task["comments"] = [{"marker": "review:red", "body": "[review:red]\nold"}] * 3
        self.runtime.audit.events.return_value = [
            self.claimed(), self.report("done", 1), *self.rework_round(),
            self.report("blocked-external_fact", 2), self.block("worker-blocked"),
        ]
        point = claim.resume_point(self.runtime, self.task)
        self.assertEqual(
            (point.stage, point.decision_id, point.decision, point.generation),
            (claim.STAGE_WORKER, "evt-decision", "Narrow the reader to the declared states.", 2),
        )
        record = self.prepare(fresh_attempt=True).record
        self.assertEqual(record.report_generation, 3)
        self.assertEqual(record.report_decision, "Narrow the reader to the declared states.")
        self.assertEqual(record.report_decision_id, "evt-decision")
        self.assertEqual(record.review_baseline, 3)

    def test_a_round_without_a_decision_resumes_without_one(self):
        bounce = self.event(
            attempt_request_id("old-attempt", "stale-done-rework", "sample-1", "1"), EventKind.CARD_MOVED,
        )
        for events in (
            [*self.rework_round(), bounce, self.block("stale-done-blocked", "2")],
            [*self.rework_round(), self.block("assessment-reslice", source="assessment")],
            [*self.rework_round(), self.claimed("next-attempt"), self.block("worker-blocked", attempt="next-attempt")],
        ):
            with self.subTest(request=events[-1]["request_id"]):
                self.runtime.audit.events.return_value = events
                point = claim.resume_point(self.runtime, self.task)
                self.assertTrue(point.require_existing_workspace)
                self.assertEqual((point.decision_id, point.decision), ("", ""))

    def test_production_claim_reads_the_audit_once_and_resumes_a_ceiling_block(self):
        def tasks(_runtime, states):
            return [self.task] if states == {"ready"} else []

        self.runtime.audit.events.return_value = [self.claimed(), self.block("red-review-ceiling", source="validate")]
        with (
            mock.patch.object(production, "_production_tasks", side_effect=tasks),
            mock.patch.object(production, "pending_wait_blockers", return_value=[]),
            mock.patch.object(claim, "launch_worker_after_claim", return_value={"status": "ok"}) as launch,
        ):
            production._production_claim_ready(self.runtime, self.records, self.payload)
        self.runtime.audit.events.assert_called_once_with("sample-1")
        self.runtime.audit.committed_event.assert_not_called()
        self.runtime.writer.claim.assert_called_once()
        self.assertTrue(launch.call_args.kwargs["require_existing_workspace"])

    def test_audit_error_precedes_replacement_and_claim(self):
        self.runtime.audit.events.side_effect = TaskError("backend_error", "audit unavailable", 1)
        self.runtime.audit.committed_event.side_effect = self.runtime.audit.events.side_effect
        with self.assertRaisesRegex(TaskError, "audit unavailable"):
            self.prepare()
        self.runtime.writer.claim.assert_not_called()
        self.runtime._stop_worker_confirmed.assert_not_called()
        self.assertEqual(self.payload, {})
        self.assertEqual(self.records, {})

    def test_unconfirmed_stop_is_a_claim_skip_that_keeps_each_old_identity_and_creates_no_attempt(self):
        for role in ("worker", "review"):
            with self.subTest(role=role):
                old = DispatcherRecord.from_json({
                    "attempt_id": "old-attempt", "workspace": "/retained", "handle": "old-worker",
                    "review_handle": "old-review" if role == "review" else "",
                    "worker_leaf": "worker-leaf", "review_leaf": "review-leaf" if role == "review" else "",
                    f"{role}_head_run": {"run_id": f"old-{role}-run"},
                })
                self.records["sample-1"] = old
                refused = {"status": "degraded", "step": "claim", "action": f"{role}-stop-unconfirmed",
                           "reason": "the previous head could not be confirmed stopped: fixture"}
                self.runtime._stop_worker_confirmed.return_value = refused if role == "worker" else None
                self.runtime._end_review_pane_confirmed.return_value = refused if role == "review" else None
                identity = old.to_json()
                skipped = self.prepare()
                self.assertTrue(is_claim_skip(skipped), skipped)
                self.assertEqual(
                    skipped["reason"],
                    f"{role} run old-{role}-run: the previous head could not be confirmed stopped: fixture",
                )
                self.assertEqual(old.to_json(), identity)
                self.assertIs(self.records["sample-1"], old)
                self.assertEqual(self.payload, {})
                self.runtime.writer.claim.assert_not_called()

    def test_ready_record_is_replaced_only_after_both_confirmed_stops(self):
        old = DispatcherRecord.from_json({"workspace": "/retained", "handle": "worker", "review_handle": "review"})
        self.records["sample-1"] = old
        order = []
        self.runtime._end_review_pane_confirmed.side_effect = lambda *a, **k: order.append("review")
        self.runtime._stop_worker_confirmed.side_effect = lambda *a, **k: order.append("worker")
        self.runtime.writer.claim.side_effect = lambda **k: order.append("claim")
        prepared = self.prepare()
        self.assertEqual(order, ["review", "worker", "claim"])
        self.assertNotEqual(prepared.record.attempt_id, "old-attempt")
        self.assertIs(self.records["sample-1"], prepared.record)

    def test_committed_claim_without_record_keeps_identity_for_writer_verification(self):
        self.runtime.audit.events.return_value = [self.claimed()]
        with mock.patch.object(claim, "launch_worker_after_claim", return_value={"status": "blocked"}) as launch:
            claim.claim_ready_task(self.runtime, self.task, self.records, self.payload, "old-attempt")
        self.assertEqual(
            self.runtime.writer.claim.call_args.kwargs["request_id"],
            attempt_request_id("old-attempt", "claim", "sample-1"),
        )
        self.assertEqual(launch.call_args.args[2].attempt_id, "old-attempt")
        self.assertEqual(self.payload, {})

    def test_repeated_review_block_requeue_claims_a_new_attempt_on_the_retained_workspace(self):
        self.runtime.audit.events.return_value = [self.block("red-review-repeated-blockers", source="validate")]
        with mock.patch.object(claim, "launch_worker_after_claim", return_value={"status": "ok"}) as launch:
            outcome = claim.claim_ready_task(
                self.runtime, self.task, self.records, self.payload, "old-attempt"
            )
        self.assertEqual(outcome["status"], "ok")
        record = self.records["sample-1"]
        self.assertNotEqual(record.attempt_id, "old-attempt")
        self.assertEqual(
            self.runtime.writer.claim.call_args.kwargs["request_id"],
            attempt_request_id(record.attempt_id, "claim", "sample-1"),
        )
        self.assertEqual(self.payload["attempts"][0]["attempt_id"], record.attempt_id)
        launch.assert_called_once()
        self.assertTrue(launch.call_args.kwargs["require_existing_workspace"])
