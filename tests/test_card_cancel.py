"""`task cancel` and `task supersede` of a non-Done card through the public writer seam, with the
dispatcher's runtime."""

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


class CancelFixture(DispatcherRuntimeFixture):
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

    def records(self) -> dict:
        return self.runtime.production_state.load().get("records") or {}


class CardCancelTests(CancelFixture, unittest.TestCase):
    def cancel(self, request_id: str, *, role: str = "po", actor: str = "operator") -> dict:
        return self.writer.cancel(
            role=role, actor=actor, reference=CARD_REF, reason=REASON, request_id=request_id,
            runtime=lambda: self.runtime,
        )

    def cancelled_event(self) -> dict:
        [event] = [event for event in self.writer.audit.events(CARD_REF) if event.get("kind") == "cancelled"]
        return event

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

    def test_ci_and_review_withdrawal_passes_through_blocked_before_archive(self) -> None:
        from ummanu.board.audit_contract import card_transition_of

        for stage in ("ci", "review"):
            with self.subTest(stage=stage):
                self.board.move(12, stage)
                self.board._execute("UPDATE tasks SET archived = false WHERE task_ref = %s", (CARD_REF,))
                self.board._commit_unless_nested()
                request = f"cancel-{stage}"
                self.cancel(request)
                self.cancel(request)
                card = self.reader.show(CARD_REF)
                self.assertEqual((card["state"], card["closed"]), ("blocked", True))
                transitions = [card_transition_of(event) for event in self.writer.audit.events(CARD_REF)
                               if event.get("request_id") == f"{request}-withdrawal"]
                self.assertEqual(transitions, [(stage, "blocked")])

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


SUCCESSOR = "ummanu-511"


class CardSupersedeTests(CancelFixture, unittest.TestCase):
    """OLD is the pilot card, NEW its Ready neighbour; dependents are added per test."""

    def supersede(
        self, request_id: str, *, old: str = CARD_REF, new: str = SUCCESSOR, role: str = "po", actor: str = "operator"
    ) -> dict:
        return self.writer.supersede(
            role=role, actor=actor, reference=old, successor=new, reason=REASON, request_id=request_id,
            runtime=lambda: self.runtime,
        )

    def card(self, key: int, reference: str, *, state: str = "ready", closed: bool = False, **metadata: str) -> str:
        self.board.add_card(key, reference, state=state, closed=closed, metadata={"task_type": "code", **metadata})
        return reference

    def dependents(self) -> tuple[str, str, str]:
        """Two open dependents of OLD, one beside another blocker, and a Done one."""
        self.card(19, "ummanu-519")
        return (
            self.card(20, "ummanu-520", blocked_by=CARD_REF),
            self.card(21, "ummanu-521", blocked_by=f"ummanu-519,{CARD_REF}"),
            self.card(22, "ummanu-522", state="done", blocked_by=CARD_REF),
        )

    def blocked_by(self, reference: str) -> str:
        """The card's `blocked_by` as the board reads it back, its refs sorted."""
        return self.reader.show(reference)["blocked_by"]

    def superseded_events(self) -> list[dict]:
        return [event for event in self.writer.audit.events(CARD_REF) if event.get("kind") == "superseded"]

    def test_two_dependents_are_repointed_old_archived_and_the_supersession_recorded_and_a_repeat_replays(
        self,
    ) -> None:
        first, second, done = self.dependents()

        result = self.supersede("supersede-two")
        again = self.supersede("supersede-two")

        self.assertEqual(self.blocked_by(first), SUCCESSOR)
        self.assertEqual(self.blocked_by(second), f"{SUCCESSOR},ummanu-519")
        self.assertEqual(self.blocked_by(done), CARD_REF)
        old = self.reader.show(CARD_REF)
        self.assertTrue(old["closed"])
        self.assertIn(f"[superseded by {SUCCESSOR}]\n{REASON}", [comment["body"] for comment in old["comments"]])
        self.assertEqual(self.reader.show(SUCCESSOR)["workspace"]["supersedes"], CARD_REF)
        [event] = self.superseded_events()
        self.assertEqual(event["payload"]["successor"], SUCCESSOR)
        self.assertEqual(
            event["payload"]["repointed"], {first: SUCCESSOR, second: f"{SUCCESSOR},ummanu-519"}
        )
        self.assertEqual((result["repointed"], result["replayed"]), ([first, second], False))
        self.assertEqual((again["event_id"], again["replayed"]), (result["event_id"], True))
        [intent] = CleanupJournal(self.data_dir).summary()
        self.assertEqual((intent["ref"], intent["disposition"]), (CARD_REF, "archive"))

    def test_an_in_progress_old_card_has_its_heads_stopped_and_record_dropped_as_cancel_does(self) -> None:
        self.start_dispatcher()
        self.tick()
        record = self.records()[CARD_REF]
        heartbeat = Path(pid_file_path("worker", CARD_REF))
        self.assertTrue(heartbeat.exists())
        dependent = self.card(20, "ummanu-520", blocked_by=CARD_REF)

        result = self.supersede("supersede-running")

        self.assertIn("stop_head:worker", self.host.calls)
        self.assertFalse(heartbeat.exists())
        self.assertIn(f"worker run {record['worker_head_run']['run_id']}", result["stopped"])
        self.assertTrue(result["record_removed"])
        self.assertNotIn(CARD_REF, self.records())
        [event] = self.superseded_events()
        self.assertEqual(event["payload"]["from"], "in_progress")
        self.assertEqual(self.blocked_by(dependent), SUCCESSOR)
        card = self.reader.show(CARD_REF)
        self.assertEqual((card["closed"], card["claim"]["worker"]), (True, None))

    def test_each_refusal_comes_before_any_effect(self) -> None:
        first, _second, _done = self.dependents()
        cases = (
            ("missing successor", {"new": "ummanu-599"}, "not_found"),
            ("successor is the card itself", {"new": CARD_REF}, "validation"),
            ("archived successor", {"new": self.card(30, "ummanu-530", closed=True)}, "validation"),
            ("Done successor", {"new": self.card(31, "ummanu-531", state="done")}, "validation"),
            ("successor blocked_by the card", {"new": self.card(32, "ummanu-532", blocked_by=CARD_REF)},
             "dependency_cycle"),
            ("successor waiting for a dependent", {"new": self.card(33, "ummanu-533", blocked_by=first)},
             "dependency_cycle"),
            ("Done card", {"old": self.card(34, "ummanu-534", state="done")}, "transition_forbidden"),
            ("archived card", {"old": self.card(35, "ummanu-535", closed=True)}, "transition_forbidden"),
        )
        refused = {}
        for name, refs, code in cases:
            with self.subTest(name), self.assertRaises(TaskError) as raised:
                self.supersede(f"supersede-{len(refused)}", **refs)
            self.assertEqual(raised.exception.code, code, raised.exception.message)
            refused[name] = raised.exception.message

        self.assertIn(f"ummanu-533 -> {first} -> ummanu-533", refused["successor waiting for a dependent"])
        self.assertFalse(self.reader.show(CARD_REF)["closed"])
        self.assertEqual(self.blocked_by(first), CARD_REF)
        self.assertIsNone(self.reader.show(SUCCESSOR)["workspace"]["supersedes"])
        self.assertEqual(self.superseded_events(), [])

    def test_the_observer_supersedes_only_by_a_card_of_its_own_sprint(self) -> None:
        self.observed_sprint()
        with self.assertRaises(TaskError) as raised:
            self.supersede("supersede-observer-other", role="observer", actor="observer")
        self.assertEqual(raised.exception.code, "role_forbidden")
        self.assertFalse(self.reader.show(CARD_REF)["closed"])

        self.board.save_metadata(13, sprint_ref="sprint:1031")
        self.supersede("supersede-observer", role="observer", actor="observer")

        self.assertTrue(self.reader.show(CARD_REF)["closed"])
        [event] = self.superseded_events()
        self.assertEqual(event["actor"], {"role": "observer", "id": "observer"})

    def test_a_held_card_lock_refuses_naming_the_holder_and_repoints_nothing(self) -> None:
        first, second, _done = self.dependents()
        release = self.hold(reference_lock(self.data_dir, CARD_REF))
        with mock.patch.object(card_cancel, "CANCEL_ALLOWANCE", 1.0), self.assertRaises(TaskError) as raised:
            self.supersede("supersede-held")
        release()

        self.assertEqual(raised.exception.code, "card_busy")
        self.assertIn(f"is held by pid {os.getpid()}", raised.exception.message)
        self.assertEqual((self.blocked_by(first), self.blocked_by(second)), (CARD_REF, f"{CARD_REF},ummanu-519"))
        self.assertFalse(self.reader.show(CARD_REF)["closed"])
        self.assertIsNone(self.reader.show(SUCCESSOR)["workspace"]["supersedes"])

        self.assertEqual(self.supersede("supersede-held")["repointed"], [first, second])


if __name__ == "__main__":
    unittest.main()
