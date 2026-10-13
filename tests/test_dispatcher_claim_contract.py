"""Real audit/writer evidence for claim admission and terminal request replay, CI only."""

from __future__ import annotations

import contextlib
import json
import unittest
from unittest import mock

from tests.dispatcher_fixtures import CARD_REF, DispatcherRuntimeFixture
from ummanu.board.models import Event
from ummanu.dispatch import claim, production, review, wait_vitality
from ummanu.dispatch.launch import infrastructure_action
from ummanu.dispatch.state import attempt_request_id
from ummanu.dispatch.types import HostError
from ummanu.dispatch.watchdog import wait_cycle_token


class ClaimAuditContractTests(DispatcherRuntimeFixture, unittest.TestCase):
    def move_ready(self):
        self.writer.move(
            role="po", actor="operator", reference=CARD_REF, target="ready", reason="retry",
            sprint_override=True, sprint_override_reason="operator-approved retry",
            request_id="fixture-requeue",
        )

    def cycle_retry(self, action, *, infrastructure=False, historical=False):
        payload = self.runtime.production_state.load()
        records = {}
        first = claim.claim_ready_task(
            self.runtime, self.reader.show(CARD_REF), records, payload, "old-attempt"
        )
        self.assertEqual(first["status"], "ok")
        record = records[CARD_REF]
        workspace = record.workspace
        record.comment_baseline = 7
        record.worker_respawns = 1
        record.review_respawns = 2
        suffix = "" if historical else wait_cycle_token(record)
        token = infrastructure_action(action) if infrastructure else action
        request_id = attempt_request_id(record.attempt_id, token, CARD_REF, suffix)
        cause = "host_unavailable" if infrastructure else "workspace_contract"
        task = self.reader.show(CARD_REF)
        # These producers previously wrote unsuffixed IDs. Exercise their historical output
        # through the same terminal writer, rather than planting an event-shaped fixture.
        with contextlib.ExitStack() as stack:
            if historical:
                module = review if action == "review-blocked" else wait_vitality
                stack.enter_context(mock.patch.object(module, "_wait_cycle_token", return_value=""))
            if action.endswith("wait-stall"):
                result = wait_vitality._escalate_wait(
                    self.runtime, task, record, records, payload, record.attempt_id,
                    kind=action.split("-")[0], stall=1, trigger="confirmed stall",
                )
            elif action == "worker-respawn-blocked":
                self.host.fail_restart_error = HostError("respawn refused", bring_up_cause=cause)
                result = wait_vitality._respawn_wait(
                    self.runtime, task, record, records, payload, record.attempt_id,
                    kind="worker", now=1.0, trigger="confirmed dead head",
                )
                self.host.fail_restart_error = None
            else:
                record.review_infra_failures = 100
                result = review.review_infrastructure_failure(
                    self.runtime, task, records, record, record.attempt_id, payload=payload,
                    reason="review refused", outcome_reason="host review failed",
                    exc=HostError("review refused", bring_up_cause=cause),
                )
        self.assertTrue(result.get("to") == "blocked" or result["status"] == "blocked", result)
        event = self.runtime.audit.committed_event(request_id)
        self.assertIsNotNone(event)
        self.assertEqual(event["transition"]["target"], "blocked")
        before = self.runtime.audit.events(CARD_REF)
        replay = self.writer.move(
            role="dispatcher", actor=self.runtime.owner, reference=CARD_REF, target="blocked",
            reason=event["reason"], request_id=request_id,
            outcome_owed=event["data"].get("attempt_outcome_owed"),
            terminal_taxonomy=event["data"].get("terminal_taxonomy"),
        )
        self.assertTrue(replay["replayed"])
        self.assertEqual(self.runtime.audit.events(CARD_REF), before)
        self.move_ready()
        retried = claim.claim_ready_task(
            self.runtime, self.reader.show(CARD_REF), records, payload, record.attempt_id
        )
        self.assertEqual(retried["status"], "ok", retried)
        self.assertNotEqual(retried["attempt_id"], record.attempt_id)
        self.assertTrue(self.host.prepare_requires_existing[-1])
        self.assertEqual(records[CARD_REF].workspace, workspace)
        self.assertEqual(self.reader.show(CARD_REF)["state"], "in_progress")
        self.assertEqual(len(payload["attempts"]), 1)
        self.assertIsNotNone(self.runtime.audit.committed_event(
            attempt_request_id(retried["attempt_id"], "claim", CARD_REF)
        ))

    def test_worker_wait_cycle_retry(self):
        self.cycle_retry("worker-wait-stall")

    def test_review_wait_cycle_retry(self):
        self.cycle_retry("review-wait-stall")

    def test_worker_respawn_cycle_retry(self):
        self.cycle_retry("worker-respawn-blocked")

    def test_worker_respawn_infrastructure_cycle_retry(self):
        self.cycle_retry("worker-respawn-blocked", infrastructure=True)

    def test_review_blocked_cycle_retry(self):
        self.cycle_retry("review-blocked")

    def test_review_blocked_infrastructure_cycle_retry(self):
        self.cycle_retry("review-blocked", infrastructure=True)

    def test_worker_wait_historical_retry(self):
        self.cycle_retry("worker-wait-stall", historical=True)

    def test_review_wait_historical_retry(self):
        self.cycle_retry("review-wait-stall", historical=True)

    def test_worker_respawn_historical_retry(self):
        self.cycle_retry("worker-respawn-blocked", historical=True)

    def test_worker_respawn_infrastructure_historical_retry(self):
        self.cycle_retry("worker-respawn-blocked", infrastructure=True, historical=True)

    def test_review_blocked_historical_retry(self):
        self.cycle_retry("review-blocked", historical=True)

    def test_review_blocked_infrastructure_historical_retry(self):
        self.cycle_retry("review-blocked", infrastructure=True, historical=True)

    def test_fresh_production_claim_counts_writer_reads_and_protocol_events(self):
        audit = self.runtime.audit
        before = audit.events(CARD_REF)
        with (
            mock.patch.object(audit, "committed_event", wraps=audit.committed_event) as committed,
            mock.patch.object(audit, "pending_event", wraps=audit.pending_event) as pending,
            mock.patch.object(audit, "events", wraps=audit.events) as history,
        ):
            result = production._production_claim_ready(self.runtime, {}, self.runtime.production_state.load())
        request_id = attempt_request_id(result["attempt_id"], "claim", CARD_REF)
        preflight = [call for call in committed.call_args_list
                     if "blocked" in call.args[0] or "wait-stall" in call.args[0] or "pending-stall" in call.args[0]]
        writer = [call for call in committed.call_args_list if call.args[0] == request_id]
        writer_pending = [call for call in pending.call_args_list if call.args[0] == request_id]
        events = [event for event in audit.events(CARD_REF) if event not in before]
        protocols = [event for event in events if event.get("record_type") == Event.RECORD_TYPE]
        counts = {
            "committed": committed.call_count, "pending": pending.call_count,
            "history": history.call_count, "total": committed.call_count + pending.call_count + history.call_count,
            "terminal_preflight": len(preflight), "claim_writer_committed": len(writer),
            "claim_writer_pending": len(writer_pending), "claim_writer_total": len(writer) + len(writer_pending),
            "protocol_events": len(protocols), "all_events": len(events),
        }
        print("CLAIM_COUNTS " + json.dumps(counts, sort_keys=True), flush=True)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(counts, {
            "committed": 22, "pending": 19, "history": 5, "total": 46,
            "terminal_preflight": 0, "claim_writer_committed": 8,
            "claim_writer_pending": 7, "claim_writer_total": 15,
            "protocol_events": 1, "all_events": 4,
        })
        self.assertEqual(len([event for event in protocols if event["kind"] == "card.started"]), 1)

    def test_committed_claim_without_record_still_reports_live_divergence(self):
        payload = self.runtime.production_state.load()
        records = {}
        claim.claim_ready_task(self.runtime, self.reader.show(CARD_REF), records, payload, "old-attempt")
        self.runtime._stop_worker_confirmed(records[CARD_REF], CARD_REF, step="claim", attempt_id="old-attempt")
        self.move_ready()
        prepared = len(self.host.prepared)
        result = claim.claim_ready_task(self.runtime, self.reader.show(CARD_REF), {}, payload, "old-attempt")
        self.assertEqual(result["reason"], "claim live board mismatch")
        self.assertEqual(len(self.host.prepared), prepared)
        self.assertEqual(self.reader.show(CARD_REF)["state"], "ready")

    def test_admission_refused_without_workspace_returns_through_fresh_bringup(self):
        payload = self.runtime.production_state.load()
        records = {}
        with mock.patch.object(claim, "live_root_project_refusal", return_value="live root cannot be claimed"):
            refused = claim.claim_ready_task(
                self.runtime, self.reader.show(CARD_REF), records, payload, "old-attempt"
            )
        self.assertEqual(refused["status"], "blocked")
        self.assertEqual(records, {})
        self.assertEqual(self.host.prepared, [])
        self.move_ready()
        retried = claim.claim_ready_task(self.runtime, self.reader.show(CARD_REF), records, payload, "old-attempt")
        self.assertEqual(retried["status"], "ok")
        self.assertNotEqual(retried["attempt_id"], "old-attempt")
        self.assertFalse(self.host.prepare_requires_existing[-1])
        self.assertEqual(self.host.prepared, [CARD_REF])
