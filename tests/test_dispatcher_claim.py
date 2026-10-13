"""Admission and retry decisions without a backend or worker process."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest import mock

from ummanu.board.models import Actor, EntityKind, Event, EventKind
from ummanu.dispatch import claim, production
from ummanu.dispatch.host import _blocked_actions_and_their_infrastructure_twins
from ummanu.dispatch.state import DispatcherRecord, attempt_request_id
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
        self.runtime.audit.committed_event.return_value = None
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

    def block(self, action, suffix="", *, attempt="old-attempt", ref="sample-1"):
        request_id = attempt_request_id(attempt, action, ref, suffix)
        return Event(
            "evt_block", EventKind.CARD_BLOCKED, EntityKind.CARD, ref,
            Actor("dispatcher", "dispatcher"), "terminal refusal", datetime(2026, 10, 13, tzinfo=UTC),
            source_state="in_progress", target_state="blocked",
        ).to_record(request_id)

    def prepare(self, **kwargs):
        return claim._prepare_claim(
            self.runtime, self.task, self.records, self.payload, "old-attempt", **kwargs
        )

    def test_cycle_and_historical_terminal_ids_choose_existing_workspace_and_new_attempt(self):
        cycle = wait_cycle_token(DispatcherRecord.from_json({"comment_baseline": 7, "worker_respawns": 1, "review_respawns": 2}))
        for action in _blocked_actions_and_their_infrastructure_twins(
            "worker-wait-stall", "review-wait-stall", "worker-respawn-blocked", "review-blocked"
        ):
            for suffix in ("", cycle):
                with self.subTest(action=action, suffix=suffix):
                    self.records.clear()
                    self.payload.clear()
                    self.runtime.audit.events.return_value = [self.block(action, suffix)]
                    prepared = self.prepare()
                    self.assertIsInstance(prepared, claim.ClaimHandoff)
                    self.assertTrue(prepared.require_existing_workspace)
                    self.assertNotEqual(prepared.record.attempt_id, "old-attempt")
                    self.assertEqual(self.payload["attempts"][0]["attempt_id"], prepared.record.attempt_id)
        self.runtime.audit.events.assert_called_with("sample-1", kind="card.blocked")
        self.runtime.audit.committed_event.assert_not_called()

    def test_other_attempt_and_nonterminal_request_do_not_authorize_a_retry(self):
        for event in (
            self.block("review-blocked", "7-1-2", attempt="another-attempt"),
            self.block("review-blocked-extra", "7-1-2"),
            self.block("claim"),
        ):
            with self.subTest(request=event["request_id"]):
                self.records.clear()
                self.runtime.audit.events.return_value = [event]
                prepared = self.prepare()
                self.assertEqual(prepared.record.attempt_id, "old-attempt")
                self.assertFalse(prepared.require_existing_workspace)

    def test_admission_refusals_open_new_attempt_without_requiring_a_checkout(self):
        for action in _blocked_actions_and_their_infrastructure_twins(
            "sprint-reservation-blocked", "live-root-project-blocked",
            "contract-preflight-blocked", "git-access-preflight-blocked",
        ):
            with self.subTest(action=action):
                self.records.clear()
                self.runtime.audit.events.return_value = [self.block(action)]
                prepared = self.prepare()
                self.assertNotEqual(prepared.record.attempt_id, "old-attempt")
                self.assertFalse(prepared.require_existing_workspace)

    def test_resume_has_no_terminal_lookup_and_requires_the_preserved_workspace(self):
        prepared = self.prepare(resume_workspace=True)
        self.assertTrue(prepared.require_existing_workspace)
        self.assertNotEqual(prepared.record.attempt_id, "old-attempt")
        self.runtime.audit.events.assert_not_called()
        self.runtime.audit.committed_event.assert_not_called()

    def test_fresh_production_claim_has_no_terminal_lookup(self):
        def tasks(_runtime, states):
            return [self.task] if states == {"ready"} else []

        with (
            mock.patch.object(production, "_production_tasks", side_effect=tasks),
            mock.patch.object(production, "pending_wait_blockers", return_value=[]),
            mock.patch.object(claim, "launch_worker_after_claim", return_value={"status": "ok"}) as launch,
        ):
            production._production_claim_ready(self.runtime, self.records, self.payload)
        self.runtime.audit.committed_event.assert_not_called()
        self.runtime.audit.events.assert_not_called()
        self.runtime.writer.claim.assert_called_once()
        self.assertFalse(launch.call_args.kwargs["require_existing_workspace"])

    def test_audit_error_precedes_replacement_and_claim(self):
        self.runtime.audit.events.side_effect = TaskError("backend_error", "audit unavailable", 1)
        with self.assertRaisesRegex(TaskError, "audit unavailable"):
            self.prepare()
        self.runtime.writer.claim.assert_not_called()
        self.runtime._stop_worker_confirmed.assert_not_called()
        self.assertEqual(self.payload, {})
        self.assertEqual(self.records, {})

    def test_unconfirmed_stop_keeps_each_old_identity_and_creates_no_attempt(self):
        for role in ("worker", "review"):
            with self.subTest(role=role):
                old = DispatcherRecord.from_json({
                    "attempt_id": "old-attempt", "workspace": "/retained", "handle": "old-worker",
                    "review_handle": "old-review" if role == "review" else "",
                    "worker_leaf": "worker-leaf", "review_leaf": "review-leaf" if role == "review" else "",
                })
                self.records["sample-1"] = old
                refused = {"status": "degraded", "action": f"{role}-stop-unconfirmed"}
                self.runtime._stop_worker_confirmed.return_value = refused if role == "worker" else None
                self.runtime._end_review_pane_confirmed.return_value = refused if role == "review" else None
                identity = old.to_json()
                self.assertEqual(self.prepare(), refused)
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
        self.runtime.audit.committed_event.return_value = {"request_id": "committed-claim"}
        with mock.patch.object(claim, "launch_worker_after_claim", return_value={"status": "blocked"}) as launch:
            claim.claim_ready_task(self.runtime, self.task, self.records, self.payload, "old-attempt")
        self.assertEqual(
            self.runtime.writer.claim.call_args.kwargs["request_id"],
            attempt_request_id("old-attempt", "claim", "sample-1"),
        )
        self.assertEqual(launch.call_args.args[2].attempt_id, "old-attempt")
        self.assertEqual(self.payload, {})
