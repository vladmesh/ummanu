"""`task cancel` of a non-Done card through the public writer seam, with the dispatcher's runtime."""

from __future__ import annotations

import os
import threading
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest import mock

from tests.dispatcher_fixtures import CARD_REF, DispatcherRuntimeFixture
from tests.observer_identity import as_observer
from ummanu.dispatch import card_cancel
from ummanu.dispatch.cleanup_journal import CleanupJournal
from ummanu.dispatch.ownership import ownership_lock, reference_lock
from ummanu.dispatch.watchdog import pid_file_path
from ummanu.tasks import TaskError

REASON = "The plan changed."


class CardCancelTests(DispatcherRuntimeFixture, unittest.TestCase):
    def hold(self, lock) -> Callable[[], None]:
        """Hold `lock` on another thread; the returned call releases it."""
        held, release = threading.Event(), threading.Event()

        def hold() -> None:
            with lock:
                held.set()
                release.wait(10)

        holder = threading.Thread(target=hold)
        holder.start()
        self.addCleanup(holder.join)
        self.addCleanup(release.set)
        self.assertTrue(held.wait(5))

        def released() -> None:
            release.set()
            holder.join()

        return released

    def cancel(self, request_id: str, *, role: str = "po", actor: str = "operator") -> dict:
        return self.writer.cancel(
            role=role, actor=actor, reference=CARD_REF, reason=REASON, request_id=request_id,
            runtime=lambda: self.runtime,
        )

    def cancelled_event(self) -> dict:
        [event] = [event for event in self.writer.audit.events(CARD_REF) if event.get("kind") == "cancelled"]
        return event

    def records(self) -> dict:
        return self.runtime.production_state.load().get("records") or {}

    def test_a_ready_card_is_archived_with_its_cancel_audit_and_a_repeat_replays(self) -> None:
        first = self.cancel("cancel-ready")
        again = self.cancel("cancel-ready")

        card = self.reader.show(CARD_REF)
        self.assertEqual((card["state"], card["closed"]), ("ready", True))
        self.assertIn(f"[cancel]\n{REASON}", [comment["body"] for comment in card["comments"]])
        event = self.cancelled_event()
        self.assertEqual(event["actor"], {"role": "po", "id": "operator"})
        self.assertEqual((event["payload"]["reason"], event["payload"]["from"]), (REASON, "ready"))
        self.assertEqual((first["replayed"], again["replayed"]), (False, True))
        self.assertEqual(again["event_id"], first["event_id"])
        [intent] = CleanupJournal(self.data_dir).summary()
        self.assertEqual((intent["ref"], intent["disposition"]), (CARD_REF, "archive"))

    def test_an_in_progress_card_has_its_heads_stopped_and_record_dropped_and_no_tick_takes_it_back(self) -> None:
        self.start_dispatcher()
        self.tick()
        record = self.records()[CARD_REF]
        heartbeat = Path(pid_file_path("worker", CARD_REF))
        self.assertTrue(heartbeat.exists())

        result = self.cancel("cancel-running")

        self.assertIn("stop_head:worker", self.host.calls)
        self.assertFalse(heartbeat.exists())
        self.assertIn(f"worker run {record['worker_head_run']['run_id']}", result["stopped"])
        self.assertTrue(result["record_removed"])
        self.assertNotIn(CARD_REF, self.records())
        self.assertEqual(self.cancelled_event()["payload"]["from"], "in_progress")
        launches = self.host.prepared.count(CARD_REF)

        self.runtime.production_tick()

        card = self.reader.show(CARD_REF)
        self.assertEqual((card["closed"], card["claim"]["worker"]), (True, None))
        self.assertNotIn(CARD_REF, self.records())
        self.assertEqual(self.host.prepared.count(CARD_REF), launches)

    def test_a_blocked_card_with_a_stale_claim_is_released_and_archived_where_archive_refuses(self) -> None:
        self.board.move(12, "blocked")
        self.board.save_metadata(12, claim="ummanu-510-pilot")
        with self.assertRaisesRegex(TaskError, "active claim.*task cancel"):
            self.writer.archive(role="po", actor="operator", reference=CARD_REF, reason=REASON)

        self.cancel("cancel-blocked")

        card = self.reader.show(CARD_REF)
        self.assertEqual((card["state"], card["closed"], card["claim"]["worker"]), ("blocked", True, None))
        self.assertEqual(self.cancelled_event()["payload"]["claim"], "ummanu-510-pilot")

    def test_an_assessment_card_is_archived(self) -> None:
        self.board.move(12, "assessment")

        self.cancel("cancel-assessment")

        self.assertTrue(self.reader.show(CARD_REF)["closed"])
        self.assertEqual(self.cancelled_event()["payload"]["from"], "assessment")

    def test_the_observer_cancels_only_a_card_of_its_own_sprint_and_other_roles_none(self) -> None:
        self.observed_sprint()
        for role, sprint, code in (("observer", "sprint:other", "observer_sprint_mismatch"),
                                   ("steward", "sprint:1031", "role_forbidden")):
            with self.subTest(role=role), as_observer(sprint), self.assertRaises(TaskError) as raised:
                self.cancel(f"cancel-{role}", role=role, actor=role)
            self.assertEqual(raised.exception.code, code)
        self.assertFalse(self.reader.show(CARD_REF)["closed"])

        self.cancel("cancel-observer", role="observer", actor="observer")

        self.assertTrue(self.reader.show(CARD_REF)["closed"])
        self.assertEqual(self.cancelled_event()["actor"], {"role": "observer", "id": "observer"})

    def test_a_held_card_lock_is_named_in_the_refusal_and_nothing_is_cancelled(self) -> None:
        release = self.hold(reference_lock(self.data_dir, CARD_REF))
        with mock.patch.object(card_cancel, "CANCEL_ALLOWANCE", 1.0), self.assertRaises(TaskError) as raised:
            self.cancel("cancel-held")
        release()

        self.assertEqual(raised.exception.code, "card_busy")
        self.assertIn(f"is held by pid {os.getpid()}", raised.exception.message)
        self.assertIn("nothing was archived", raised.exception.message)
        self.assertFalse(self.reader.show(CARD_REF)["closed"])
        self.assertTrue(self.cancel("cancel-held")["task"]["closed"])

    def test_a_lock_held_after_the_archive_commits_is_reported_as_pending_and_a_repeat_finishes(self) -> None:
        self.runtime.production_state.save(
            {"version": 1, "phase": "production", "owner": self.runtime.owner,
             "records": {CARD_REF: {"worker": "ummanu-510-pilot", "attempt_id": "attempt-settled"}}}
        )
        release = self.hold(ownership_lock(self.data_dir))
        with mock.patch.object(card_cancel, "CANCEL_ALLOWANCE", 2.0), self.assertRaises(TaskError) as raised:
            self.cancel("cancel-pending")
        release()

        self.assertEqual(raised.exception.code, "card_busy")
        self.assertIn(f"{CARD_REF} is archived (event ", raised.exception.message)
        self.assertIn(f"is held by pid {os.getpid()}", raised.exception.message)
        self.assertIn("repeat the same request id", raised.exception.message)
        self.assertNotIn("nothing was archived", raised.exception.message)
        self.assertTrue(self.reader.show(CARD_REF)["closed"])
        self.assertIn(CARD_REF, self.records())

        again = self.cancel("cancel-pending")

        self.assertEqual((again["replayed"], again["record_removed"]), (True, True))
        self.assertNotIn(CARD_REF, self.records())


if __name__ == "__main__":
    unittest.main()
