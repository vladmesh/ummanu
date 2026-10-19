"""A Ready card whose old reviewer has ended is claimed afresh; one whose stop is unproven is skipped."""

from __future__ import annotations

import unittest
from dataclasses import replace
from unittest import mock

from tests.dispatcher_fixtures import CARD_REF, DispatcherRuntimeFixture
from ummanu.dispatch.types import HostError
from ummanu.runtime.head import HeadRun, HeadSpec, StopInitiator, TaskRef
from ummanu.runtime.head.identity import head_process_status
from ummanu.runtime.head.local_pty import protocol
from ummanu.runtime.head.local_pty.journal import RUN_EXITED, RUN_STARTED, JournalWriter
from ummanu.runtime.head_runtimes import LOCAL_PTY_RUNTIME
from ummanu.runtime.local_pty_head import LocalPtyHeadRuntime

NEIGHBOR_REF = "ummanu-511"
REVIEWER_RUN = "dead-reviewer"


class DeadReviewerReleaseTests(DispatcherRuntimeFixture, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.heads = self.data_dir / "heads"
        self.enterContext(mock.patch(
            "ummanu.runtime.head.local_pty.scoped_lifecycle.CGROUP_ROOT", self.data_dir / "cgroups"))
        self.backend = LocalPtyHeadRuntime(self.heads, head_process_status=head_process_status, stop_timeout=0)
        stop_head = self.host.stop_head

        def stop_head_routed(record, kind, initiator="dispatcher"):
            if kind == "review":
                self.stop_reviewer(record, initiator)
                return
            stop_head(record, kind, initiator)

        self.host.stop_head = stop_head_routed
        self.host.stop_review = self.stop_reviewer

    def stop_reviewer(self, record, initiator) -> None:
        """The host's reviewer stop: the runtime's receipt decides, as `stop_review_head` does."""
        run = replace(HeadRun.from_json(record.review_head_run), pid_file=record.review_pid_file)
        receipt = self.backend.stop(run, StopInitiator(actor=initiator))
        if not receipt.ok:
            raise HostError(receipt.reason)
        record.review_head_run = receipt.run.to_json()

    def leave_ownerless_reviewer(self, *journal: str) -> str:
        """Claim the card, then leave its record in `review_starting` with a reviewer that lost its owner."""
        self.runtime.production_tick()
        directory = protocol.run_dir_for(self.heads, REVIEWER_RUN)
        directory.mkdir(parents=True)
        with JournalWriter(directory / protocol.JOURNAL_NAME, REVIEWER_RUN) as writer:
            for kind in journal:
                writer.append(kind, head_pid=4242)
        payload = self.runtime.production_state.load()
        record = payload["records"][CARD_REF]
        run = HeadRun(run_id=REVIEWER_RUN, spec=HeadSpec(profile_id="test", adapter="unknown", runtime=LOCAL_PTY_RUNTIME),
                      workspace=record["workspace"], task_ref=TaskRef.card(CARD_REF), role="reviewer",
                      scope_generation="lost-generation")
        record.update(state="review_starting", review_head_run=run.to_json(), review_infra_failures=10,
                      review_pid_file=str(self.data_dir / "review-pid" / f"{CARD_REF}.pid"))
        self.runtime.production_state.save(payload)
        for target in ("blocked", "ready"):
            self.writer.move(role="po", actor="operator", reference=CARD_REF, target=target,
                             reason="return the card", request_id=f"return-{target}")
        return str(record["attempt_id"])

    def test_ready_card_with_an_exited_ownerless_reviewer_is_claimed_with_a_fresh_attempt(self) -> None:
        old_attempt = self.leave_ownerless_reviewer(RUN_STARTED, RUN_EXITED)

        result = self.runtime.production_tick()

        claims = [action for action in result["actions"] if action.get("step") == "claim"]
        self.assertEqual([(action["status"], action["pilot_ref"]) for action in claims], [("ok", CARD_REF)], result)
        record = self.runtime.production_state.load()["records"][CARD_REF]
        self.assertNotEqual(record["attempt_id"], old_attempt)
        self.assertNotEqual(record["state"], "review_starting")
        self.assertEqual(record["review_infra_failures"], 0)
        self.assertNotEqual(record["review_head_run"].get("run_id"), REVIEWER_RUN)
        self.assertEqual(self.reader.show(CARD_REF)["state"], "in_progress")

    def test_ready_card_behind_an_unconfirmed_stop_is_claimed_in_the_same_pass(self) -> None:
        old_attempt = self.leave_ownerless_reviewer(RUN_STARTED)

        result = self.runtime.production_tick()

        claims = [action for action in result["actions"] if action.get("step") == "claim"]
        self.assertEqual([(action["status"], action["pilot_ref"]) for action in claims],
                         [("ok", NEIGHBOR_REF)], result)
        skip = next(skip for skip in claims[0]["skipped_ready"] if skip["ref"] == CARD_REF)
        self.assertIn(f"review run {REVIEWER_RUN}", skip["reason"])
        self.assertIn("lost its owner", skip["reason"])
        payload = self.runtime.production_state.load()
        self.assertEqual(payload["ready_skips"][CARD_REF]["reason"], skip["reason"])
        self.assertEqual(payload["records"][CARD_REF]["attempt_id"], old_attempt)
        self.assertEqual(self.reader.show(CARD_REF)["state"], "ready")
        self.assertEqual(self.reader.show(NEIGHBOR_REF)["state"], "in_progress")


if __name__ == "__main__":
    unittest.main()
