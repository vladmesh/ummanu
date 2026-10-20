"""A card returned from Blocked at the gate or review stage continues there on an unchanged candidate."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.dispatcher_fixtures import CARD_REF, DispatcherRuntimeFixture
from tests.support.completion_receipt import SHA, TREE, declared_receipt
from ummanu.broad_check import admission_snapshot, admitted_check
from ummanu.dispatch import wait_vitality
from ummanu.dispatch.gate import GateResult
from ummanu.dispatch.helpers import _round_report_ids, _round_worker_check
from ummanu.dispatch.types import HostError
from ummanu.tasks import task_audit_for


class StageResumeTests(DispatcherRuntimeFixture, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        _, path, receipt = declared_receipt(root / "candidate", root / "instance")
        # Every done report of these tests is admitted with a green receipt for the checkout's HEAD.
        evidence = admission_snapshot(receipt, candidate_sha=SHA, tree_sha=TREE, path=path)
        patcher = mock.patch("ummanu.check_commands.completion_check", return_value=evidence)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.host.commit = SHA
        self.start_dispatcher()

    def _record(self) -> dict:
        return self.runtime.production_state.load()["records"][CARD_REF]

    def _return_to_ready(self, request_id: str) -> None:
        self.writer.move(
            role="po", actor="operator", reference=CARD_REF, target="ready",
            reason="the outage is over", sprint_override=True,
            sprint_override_reason="the PO returns the card from Blocked", request_id=request_id,
        )

    def _review_stall_blocked(self) -> str:
        """Run the card to a live reviewer on a green gate, then the review wait watchdog Blocks it."""
        self._run_worker_to_validate()
        self.assertEqual(self.tick()["action"], "review-started")
        attempt = self._record()["attempt_id"]
        payload = self.runtime.production_state.load()
        records = self.runtime.production_state.records(payload)
        record = records[CARD_REF]
        stalled = wait_vitality._escalate_wait(
            self.runtime, self.reader.show(CARD_REF), record, records, payload, record.attempt_id,
            kind="review", stall=1, trigger="confirmed stall",
        )
        self.assertEqual(stalled["status"], "blocked", stalled)
        self.assertEqual(self.reader.show(CARD_REF)["state"], "blocked")
        return attempt

    def _worker_launches(self) -> int:
        return self.host.calls.count("prepare_worker") + self.host.calls.count("restart_worker")

    def test_a_review_stall_returned_to_ready_launches_the_reviewer_on_the_same_sha(self) -> None:
        first_attempt = self._review_stall_blocked()
        workers, reviews, gates = self._worker_launches(), len(self.host.reviews), len(self.host.gate_calls)
        self._return_to_ready("return-review-stall")

        resumed = self.tick()

        self.assertEqual((resumed["action"], resumed["to"]), ("stage-resumed", "validate"), resumed)
        self.assertEqual(self.reader.show(CARD_REF)["state"], "validate")
        record = self._record()
        self.assertNotEqual(record["attempt_id"], first_attempt)
        self.assertEqual(record["report_generation"], 1)
        admission = admitted_check(_round_worker_check(
            self.runtime.audit, CARD_REF,
            _round_report_ids(record["workspace"], record["attempt_id"], CARD_REF, record["report_generation"]),
        ))
        self.assertIsNotNone(admission)
        self.assertEqual(admission["candidate_sha"], SHA)

        launched = self.tick()

        self.assertEqual(launched["action"], "review-started", launched)
        self.assertEqual(len(self.host.gate_calls), gates + 1)
        self.assertEqual(len(self.host.reviews), reviews + 1)
        self.assertEqual(self._record()["review_commit"], SHA)
        self.assertEqual(self._worker_launches(), workers)

    def test_a_tick_lost_before_the_validate_move_finishes_the_move_without_a_worker(self) -> None:
        self._review_stall_blocked()
        workers = self._worker_launches()
        self._return_to_ready("return-before-a-lost-tick")
        move = self.writer.move

        def lost_tick(**kwargs):
            if kwargs.get("target") == "validate":
                raise OSError("the tick died")
            return move(**kwargs)

        with mock.patch.object(self.writer, "move", side_effect=lost_tick), self.assertRaises(OSError):
            self.tick()
        self.assertEqual(self.reader.show(CARD_REF)["state"], "in_progress")

        finished = self.tick()

        self.assertEqual((finished["action"], finished["to"]), ("stage-resumed", "validate"), finished)
        self.assertEqual(self.reader.show(CARD_REF)["state"], "validate")
        self.assertEqual(self._worker_launches(), workers)

    def test_a_gate_infrastructure_block_returned_to_ready_reruns_the_gate_on_the_same_sha(self) -> None:
        infrastructure = GateResult(
            "red", "CI red: job «build» failed", "Getting action download info: HTTP 502",
            failure_class="infrastructure", failure_reason="action-download-http-5xx",
            failed_run_id="999", failed_run_repo="example-org/sample",
        )
        self.host.gate_results = [infrastructure] * 3
        self._run_worker_to_validate()
        self.assertEqual(self.tick()["action"], "gate-infrastructure-rerun")
        self.assertEqual(self.tick()["action"], "gate-infrastructure-rerun")
        self.assertEqual(self.tick()["action"], "gate-infrastructure-reruns-exhausted")
        workers, gates = self._worker_launches(), len(self.host.gate_calls)
        self._return_to_ready("return-gate-infrastructure")

        self.assertEqual(self.tick()["action"], "stage-resumed")
        rerun = self.tick()

        self.assertEqual(rerun["action"], "review-started", rerun)
        self.assertEqual(len(self.host.gate_calls), gates + 1)
        self.assertEqual(self._record()["review_commit"], SHA)
        self.assertEqual(self._worker_launches(), workers)

    def test_a_new_commit_in_the_checkout_starts_a_worker_round(self) -> None:
        self._review_stall_blocked()
        workers, reviews = self._worker_launches(), len(self.host.reviews)
        self.host.commit = "d" * 40
        self._return_to_ready("return-after-a-new-commit")

        restarted = self.tick()

        self.assertEqual(restarted["status"], "ok", restarted)
        self.assertNotEqual(restarted.get("action"), "stage-resumed")
        self.assertEqual(self.reader.show(CARD_REF)["state"], "in_progress")
        self.assertEqual(self._worker_launches(), workers + 1)
        self.assertTrue(self.host.prepare_requires_existing[-1])
        self.assertEqual(self._record()["report_generation"], 2)
        self.assertEqual(len(self.host.reviews), reviews)

    def test_a_held_candidate_in_the_rejected_ledger_is_blocked_before_gate_and_review(self) -> None:
        self._run_worker_to_validate()
        self.assertEqual(self.tick()["action"], "review-started")
        verdict_request = self._review_verdict_request_id("red")
        self._review_red(verdict_request)
        self.assertEqual(self.tick()["to"], "assessment")
        verdict = next(
            event for event in task_audit_for(self.board).events() if event["request_id"] == verdict_request
        )
        # The observer accepts over the red verdict and the release's merge gate cannot be read.
        self._decide("accept")
        self.host.gate_results = [HostError("gate backend failed")]
        self.assertEqual(self.tick()["reason"], "release failed")
        self.assertEqual(self.reader.show(CARD_REF)["state"], "blocked")
        self._return_to_ready("return-rejected-candidate")
        self.assertEqual(self.tick()["action"], "stage-resumed")
        workers, reviews, gates = self._worker_launches(), len(self.host.reviews), len(self.host.gate_calls)

        refused = self.tick()

        self.assertEqual(refused["reason"], "rejected candidate", refused)
        self.assertEqual(self.reader.show(CARD_REF)["state"], "blocked")
        self.assertIn(f"the red review verdict {verdict['event_id']}", self.reader.show(CARD_REF)["comments"][-1]["body"])
        self.assertEqual(len(self.host.gate_calls), gates)
        self.assertEqual(len(self.host.reviews), reviews)
        self.assertEqual(self._worker_launches(), workers)


if __name__ == "__main__":
    unittest.main()
