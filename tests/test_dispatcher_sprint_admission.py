"""Admission of cards linked to no sprint on a project an open sprint reserves (secretary-1641).

The PO may cut a card of any kind outside a sprint; whether it runs is decided here, before the
claim. A `code` card on a reserved project is refused and blocked with a typed reason, with no
workspace, head or round; `research` and `infra` are admitted. An index that cannot be verified
refuses `code` with its own reason and still admits the others.
"""

from __future__ import annotations

import unittest
from unittest import mock

from tests.dispatcher_fixtures import CARD_REF, DispatcherRuntimeFixture
from tests.integration_setup import require_disposable_board_fixture
from tests.sql_backend_fixtures import PostgresBoard
from ummanu.board.sql_cards import SPRINT_BOARD_ID
from ummanu.dispatch.claim import (
    SPRINT_RESERVATION_BLOCKED_ACTION,
    SPRINT_RESERVATION_RESERVED,
    SPRINT_RESERVATION_UNVERIFIABLE,
)
from ummanu.dispatch.launch import CAUSE_ADMISSION_REFUSED, FAILURE_CLASS_ADMISSION
from ummanu.dispatch.production import _reconcile_sprint_budget
from ummanu.dispatch.state import CLAIM_SKIP_SPRINT_RESERVATION_UNVERIFIABLE, is_claim_skip
from ummanu.sprints import SprintReader
from ummanu.tasks import TaskError, task_audit_for

SPRINT = "sprint:1031"
SPRINT_BOARD = SPRINT_BOARD_ID


def setUpModule() -> None:
    """Confirm this CI shard can build its disposable board seam before tests run."""
    require_disposable_board_fixture(PostgresBoard.shared)


class OutOfSprintAdmissionTests(DispatcherRuntimeFixture, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        # An open sprint that reserves the pilot's project, as `start_dispatcher` declares it.
        self.start_dispatcher()

    def _out_of_sprint(self, kind: str = "code") -> None:
        """The pilot as the PO cuts it outside the sprint: no sprint link, of the given kind."""
        self.board.save_metadata(12, {"sprint_ref": ""})
        self.board.save_metadata(12, task_type=kind)
        self.board.save_metadata(12, review="required" if kind == "code" else "skipped")

    def _sprint_metadata(self) -> dict:
        return self.board.sprint_metadata(SPRINT)

    def _blocked_transitions(self) -> list[dict]:
        return [
            event
            for event in task_audit_for(self.board).events()
            if event.get("record_type") == "board.protocol_event"
            and (event.get("transition") or {}).get("target") == "blocked"
            and str(event.get("ref") or "").endswith(CARD_REF)
        ]

    def _unreadable_sprint_board(self):
        """The sprint board cannot be listed, and the index has never been written."""
        index = self.data_dir / "sprints" / "active-repositories.json"
        index.unlink(missing_ok=True)
        original = self.board.call

        def call(method: str, **params: object) -> object:
            if method == "getAllTasks" and int(params.get("project_id") or 0) == SPRINT_BOARD:
                raise TaskError("backend_unavailable", "sprint board is unavailable", 1)
            return original(method, **params)

        return mock.patch.object(self.board, "call", side_effect=call)

    def _assert_nothing_spent(self) -> None:
        self.assertEqual(self.host.calls, [], "the host was never asked for anything")
        self.assertEqual(self.host.prepared, [], "no workspace")
        self.assertEqual(self.runtime.production_state.load()["records"], {}, "no dispatcher round")
        self.assertFalse((self.data_dir / "workspaces").exists())

    def test_a_code_card_outside_the_sprint_on_its_project_is_refused_before_any_work(self) -> None:
        self._out_of_sprint("code")

        blocked = self.tick()

        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["step"], "sprint-reservation-refused")
        self.assertEqual(blocked["failure_class"], FAILURE_CLASS_ADMISSION)
        self.assertEqual(blocked["failure_cause"], CAUSE_ADMISSION_REFUSED)
        self.assertEqual(blocked["sprint_reservation"]["refusal"], SPRINT_RESERVATION_RESERVED)
        self.assertEqual(blocked["sprint_reservation"]["project"], "ummanu")
        self.assertEqual(blocked["sprint_reservation"]["sprints"], [SPRINT])
        task = self.reader.show(CARD_REF)
        self.assertEqual(task["state"], "blocked")
        reason = task["comments"][-1]["body"]
        self.assertTrue(reason.endswith(blocked["failure_reason"]), "the card and the tick say the same")
        self.assertIn(f"refusal={SPRINT_RESERVATION_RESERVED}", reason)
        self.assertIn("project=ummanu", reason)
        self.assertIn(SPRINT, reason)
        self.assertIn("after that sprint closes", reason)
        self.assertNotIn("host_unavailable", reason)
        self.assertNotIn(blocked["attempt_id"], reason)
        self.assertIsNone(task["claim"]["worker"], "the refusal releases the claim it took")
        # The typed action is in the transition the block wrote.
        (transition,) = self._blocked_transitions()
        self.assertIn(f"-{SPRINT_RESERVATION_BLOCKED_ACTION}-", transition["request_id"])
        self._assert_nothing_spent()

        # Not retried: the next tick leaves the card where a person has to move it.
        again = self.tick()

        self.assertEqual(again["action"], "terminal-state")
        self.assertEqual(self.reader.show(CARD_REF)["state"], "blocked")
        self.assertEqual(len(self._blocked_transitions()), 1, "one block, not one per tick")
        self._assert_nothing_spent()

    def test_the_refusal_is_not_charged_to_any_sprint_budget(self) -> None:
        self._out_of_sprint("code")
        before = SprintReader(self.board).show(SPRINT, include_cards=False)["budget"]  # type: ignore[arg-type]

        self.tick()
        charged = _reconcile_sprint_budget(self.runtime)

        self.assertEqual(len(self._blocked_transitions()), 1)
        self.assertEqual([row for row in charged if row.get("step") == "sprint-budget"], [])
        after = SprintReader(self.board).show(SPRINT, include_cards=False)["budget"]  # type: ignore[arg-type]
        self.assertEqual(after, before)
        self.assertEqual(after["total"], 0)

    def test_research_and_infra_outside_the_sprint_on_its_project_are_claimed(self) -> None:
        for index, kind in enumerate(("research", "infra")):
            with self.subTest(kind=kind):
                if index:
                    self.tearDown()
                    self.setUp()
                self._out_of_sprint(kind)

                claimed = self.tick()

                self.assertEqual(claimed["step"], "claim")
                self.assertEqual(self.reader.show(CARD_REF)["state"], "in_progress")
                self.assertEqual(self.host.prepared, [CARD_REF])
                self.assertEqual(self._blocked_transitions(), [])

    def test_a_code_card_of_the_sprint_is_claimed(self) -> None:
        self.assertEqual(self.reader.show(CARD_REF)["sprint"], SPRINT)

        claimed = self.tick()

        self.assertEqual(claimed["step"], "claim")
        self.assertEqual(self.reader.show(CARD_REF)["state"], "in_progress")

    def test_a_code_card_outside_every_sprint_on_an_unreserved_project_is_claimed(self) -> None:
        self._out_of_sprint("code")
        self.board.save_sprint_metadata(SPRINT, sprint_reservations='["other"]')

        claimed = self.tick()

        self.assertEqual(claimed["step"], "claim")
        self.assertEqual(self.reader.show(CARD_REF)["state"], "in_progress")

    def test_the_refused_card_moved_back_to_ready_after_the_sprint_closes_is_claimed(self) -> None:
        self._out_of_sprint("code")
        self.assertEqual(self.tick()["step"], "sprint-reservation-refused")

        self.board.save_sprint_metadata(SPRINT, sprint_status="closed")
        self.sprints.rows[SPRINT]["status"] = "closed"
        moved = self.writer.move(
            role="po", actor="operator", reference=CARD_REF, target="ready", reason="the sprint closed"
        )
        self.assertEqual(moved["task"]["state"], "ready")

        claimed = self.tick()

        self.assertEqual(claimed["step"], "claim")
        self.assertEqual(self.reader.show(CARD_REF)["state"], "in_progress")
        self.assertEqual(self.host.prepared, [CARD_REF])

    def test_the_refused_card_is_archived_by_the_po(self) -> None:
        self._out_of_sprint("code")
        self.assertEqual(self.tick()["step"], "sprint-reservation-refused")

        archived = self.writer.archive(role="po", actor="operator", reference=CARD_REF, reason="not needed")

        self.assertTrue(archived["task"]["closed"])

    def test_an_unverifiable_index_refuses_code_by_its_own_reason(self) -> None:
        """Refused without a block: the Blocked move would meet the same index at the write guard."""
        self._out_of_sprint("code")

        with self._unreadable_sprint_board():
            refused = self.tick()

        self.assertEqual(refused["status"], "skipped")
        self.assertEqual(refused["action"], CLAIM_SKIP_SPRINT_RESERVATION_UNVERIFIABLE)
        self.assertTrue(is_claim_skip(refused), "the production pass moves on to the next Ready card")
        self.assertEqual(refused["sprint_reservation"]["refusal"], SPRINT_RESERVATION_UNVERIFIABLE)
        self.assertEqual(refused["sprint_reservation"]["project"], "ummanu")
        self.assertIn("could not be verified", refused["reason"])
        self.assertEqual(self.reader.show(CARD_REF)["state"], "ready", "not claimed")
        self.assertEqual(self._blocked_transitions(), [])
        self._assert_nothing_spent()

        # Once the index answers again, the card is decided on what it says.
        blocked = self.tick()

        self.assertEqual(blocked["step"], "sprint-reservation-refused")
        self.assertEqual(blocked["sprint_reservation"]["refusal"], SPRINT_RESERVATION_RESERVED)

    def test_an_unverifiable_index_still_admits_research_and_infra(self) -> None:
        for index, kind in enumerate(("research", "infra")):
            with self.subTest(kind=kind):
                if index:
                    self.tearDown()
                    self.setUp()
                self._out_of_sprint(kind)

                with self._unreadable_sprint_board():
                    claimed = self.tick()

                self.assertEqual(claimed["step"], "claim")
                self.assertEqual(self.reader.show(CARD_REF)["state"], "in_progress")


if __name__ == "__main__":
    unittest.main()
