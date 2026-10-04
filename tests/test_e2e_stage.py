"""The e2e stage: an adapter-declared workflow run on a code card's candidate, waited for by a wait card.

secretary-1795. The whole dispatcher runs here — `DispatcherRuntime._tick_task` over a real card store
(the disposable PostgreSQL board), with the fake host of the other dispatcher suites — and GitHub is
a fake behind the one host call every gate question goes through (`run_capture`): the repository name,
the `workflow_dispatch` POST, the workflow's run listing, the run the wait card reads, its jobs and its
`--log-failed`. Each test drives the code card and its wait card tick by tick, the way the production
tick interleaves them.

The declaration's own reading and the adapter schema are unit tests in `tests/test_e2e_declaration.py`.
"""

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest import mock

from tests.dispatcher_fixtures import CARD_REF
from tests.e2e_stage_fixtures import (
    BRANCH,
    E2E,
    FIRST_RUN,
    REPO,
    RUN_URL,
    SECOND_SHA,
    SHA,
    E2eStageFixture,
    SimulatedCrash,
)
from tests.integration_setup import require_disposable_board_fixture
from tests.sql_backend_fixtures import PostgresBoard
from ummanu.board.e2e_record import e2e_state
from ummanu.board.owner_events import OwnerEventStore
from ummanu.board.wait_card import wait_spec, wait_state
from ummanu.dispatch import e2e_stage
from ummanu.tasks import TaskWriter


def setUpModule() -> None:
    require_disposable_board_fixture(PostgresBoard.shared)


class E2eStageTests(E2eStageFixture, unittest.TestCase):
    # --- placement ---------------------------------------------------------------------------------

    def test_green_review_dispatches_once_waits_on_a_wait_card_and_parks_with_the_result(self) -> None:
        waiting = self.to_waiting()

        [dispatch] = self.host.dispatches
        self.assertEqual(dispatch["path"], f"repos/{REPO}/actions/workflows/e2e.yml/dispatches")
        self.assertEqual(dispatch["ref"], BRANCH)
        self.assertEqual(dispatch["typed"], {"return_run_details": "true"})
        # Only the declared inputs and the candidate: no dispatch id input is declared, so none is sent.
        self.assertEqual(dispatch["inputs"], {"suite": "mega", "sha": SHA})
        [run] = e2e_state(self.card()).runs
        self.assertEqual((run.sha, run.run_id, run.run_url, run.head_sha), (SHA, FIRST_RUN, RUN_URL, SHA))
        self.assertEqual(self.host.run_listings, 0, "GitHub's answer named the run: no list lookup")
        self.assertEqual((waiting["run"], waiting["wait_card"]), (RUN_URL, run.wait_ref))
        # The wait card is on the board, in the card's sprint, waiting for that run and returning here.
        wait = self.reader.show(run.wait_ref)
        self.assertEqual((wait["type"], wait["state"], wait["sprint"]), ("wait", "ready", "sprint:1031"))
        spec = wait_spec(wait)
        assert spec is not None
        self.assertEqual((spec.target.repo, spec.target.run_id), (REPO, FIRST_RUN))
        self.assertEqual(spec.returns, (f"card:{CARD_REF}",))
        # `task show` of the code card names the run, the wait card and the count. A card of a sprint
        # spends the sprint's e2e budget (secretary-1796): it has no cap of its own.
        view = self.card()["e2e"]
        self.assertEqual((view["runs_dispatched"], view["run_cap"], view["budget"]), (1, None, "sprint:1031"))
        self.assertEqual(view["runs"][0]["wait_card"], run.wait_ref)
        self.assertEqual(view["runs"][0]["state"], "waiting")
        self.assertEqual(view["runs"][0]["run"], RUN_URL)

        # While the run is in flight nothing but the wait card is asked: no gate read, no dispatch.
        gate_reads = len(self.host.gate_calls)
        self.assertEqual(self.tick()["action"], "e2e-waiting")
        self.assertEqual(self.tick_wait()["action"], "wait-waiting")
        self.assertEqual(self.tick()["action"], "e2e-waiting")
        self.assertEqual(len(self.host.gate_calls), gate_reads)

        self.conclude("success")
        self.assertEqual(self.reader.show(run.wait_ref)["state"], "done")
        self.assertTrue(self.comments(f"[wait:target_reached] {run.wait_ref}"))
        parked = self.tick()

        self.assertEqual(parked["to"], "assessment", parked)
        self.assertEqual(self.card()["state"], "assessment")
        self.assertEqual(len(self.host.dispatches), 1)
        self.assertEqual(self.card()["e2e"]["runs"][0]["state"], "success")
        [green] = self.comments("## E2E — green")
        self.assertIn(RUN_URL, green)
        self.assertIn(SHA, green)

        # The release audit on the same SHA: no second run.
        self._decide("release")
        released = self.tick()
        self.assertEqual(released["to"], "done", released)
        self.assertEqual(len(self.host.dispatches), 1)
        self.assertEqual(len(self.wait_cards()), 1)

    def test_review_skipped_runs_the_e2e_after_green_ci_and_then_releases(self) -> None:
        self.arrange(review="skipped", observed=False)
        self._run_worker_to_validate()

        self.assertEqual(self.tick()["action"], "e2e-waiting")
        self.assertEqual(self.host.reviews, [])
        self.assertEqual(len(self.host.dispatches), 1)
        self.conclude("success")
        released = self.tick()

        self.assertEqual(released["to"], "done", released)
        self.assertEqual(self.host.completed, [CARD_REF])
        self.assertEqual(len(self.host.dispatches), 1)

    def test_a_red_review_dispatches_nothing(self) -> None:
        self.arrange()
        self._run_worker_to_validate()
        self.assertEqual(self.tick()["action"], "review-started")
        self._review_red()

        parked = self.tick()

        self.assertEqual(parked["to"], "assessment")
        self.assertEqual(self.host.dispatches, [])
        self.assertNotIn("e2e", self.card())

    def test_a_release_decided_without_a_green_run_waits_for_one_before_the_merge(self) -> None:
        """A card parked by a red review never ran the stage; a release decision runs it, once."""
        self.arrange()
        self._run_worker_to_validate()
        self.assertEqual(self.tick()["action"], "review-started")
        self._review_red()
        self.assertEqual(self.tick()["to"], "assessment")
        self._decide("release")

        waiting = self.tick()
        self.assertEqual((waiting["action"], waiting["step"]), ("e2e-waiting", "assessment"), waiting)
        self.assertEqual(self.tick()["action"], "e2e-waiting")
        self.assertEqual(self.card()["state"], "assessment")
        self.assertEqual(self.host.completed, [])
        self.conclude("success")
        released = self.tick()

        self.assertEqual(released["to"], "done", released)
        self.assertEqual(self.host.completed, [CARD_REF])
        self.assertEqual(len(self.host.dispatches), 1)

    def test_a_project_without_e2e_behaves_as_before(self) -> None:
        self.arrange()
        self.catalog._adapter = {"validation": {"ci": "github", "required_checks": ["test"]}}
        self.to_green_review()

        self.assertEqual(self.tick()["to"], "assessment")
        self.assertEqual(self.host.dispatches, [])
        self.assertEqual(self.wait_cards(), [])

    # --- one SHA, from the gate ------------------------------------------------------------------------

    def test_the_gate_moving_head_at_the_stage_read_dispatches_on_the_new_sha(self) -> None:
        """`_recover_base` merges a newer base at the stage's own gate read: that SHA is the candidate."""
        self.arrange()
        self.to_green_review()
        self.host.move_on_gate = [SECOND_SHA]
        self.host.base_only = {(SHA, SECOND_SHA)}

        waiting = self.tick()

        self.assertEqual((waiting["action"], waiting["sha"]), ("e2e-waiting", SECOND_SHA), waiting)
        [dispatch] = self.host.dispatches
        self.assertEqual(dispatch["inputs"]["sha"], SECOND_SHA)
        [run] = e2e_state(self.card()).runs
        self.assertEqual((run.sha, run.head_sha), (SECOND_SHA, SECOND_SHA))

    def test_a_base_only_move_during_the_wait_carries_the_green_run_and_is_recorded(self) -> None:
        self.to_waiting()
        self.conclude("success")
        self.host.move_on_gate = [SECOND_SHA]
        self.host.base_only = {(SHA, SECOND_SHA)}

        parked = self.tick()

        self.assertEqual(parked["to"], "assessment", parked)
        self.assertEqual(len(self.host.dispatches), 1)
        [run] = e2e_state(self.card()).runs
        self.assertEqual(run.sha, SHA)
        self.assertEqual([item["head_sha"] for item in run.reconciled], [SECOND_SHA])
        self.assertEqual(self.card()["e2e"]["runs"][0]["reconciled_to"], [SECOND_SHA])
        [attested] = self.comments("E2E/base reconciliation")
        self.assertIn(f"e2e-green SHA `{SHA}`; HEAD `{SECOND_SHA}`", attested)

    def test_a_change_to_the_card_s_own_paths_runs_the_stage_again(self) -> None:
        """No review commit to drift from (review skipped), so only the e2e rule decides: it refuses."""
        self.arrange(review="skipped", observed=False)
        self._run_worker_to_validate()
        self.assertEqual(self.tick()["action"], "e2e-waiting")
        self.conclude("success")
        self.host.move_on_gate = [SECOND_SHA]

        again = self.tick()

        self.assertEqual((again["action"], again["sha"]), ("e2e-waiting", SECOND_SHA), again)
        self.assertEqual(self.card()["state"], "validate")
        self.assertEqual(self.host.completed, [])
        self.assertEqual([dispatch["inputs"]["sha"] for dispatch in self.host.dispatches], [SHA, SECOND_SHA])
        self.assertEqual(self.card()["e2e"]["runs_dispatched"], 2)

    def test_the_release_audit_carries_a_green_run_across_a_base_only_move(self) -> None:
        self.to_waiting()
        self.conclude("success")
        self.assertEqual(self.tick()["to"], "assessment")
        self._decide("release")
        self.host.move_on_gate = [SECOND_SHA]
        self.host.base_only = {(SHA, SECOND_SHA)}

        released = self.tick()

        self.assertEqual(released["to"], "done", released)
        self.assertEqual(len(self.host.dispatches), 1)
        [run] = e2e_state(self.card()).runs
        self.assertEqual([item["head_sha"] for item in run.reconciled], [SECOND_SHA])
        attested = [body for body in self.comments("release audit") if "E2E/base reconciliation" in body]
        self.assertEqual(len(attested), 1, self.comments("Mechanical gate attestation"))

    # --- crash and restart ---------------------------------------------------------------------------

    def test_a_crash_after_the_dispatch_finds_the_run_by_sha_branch_event_and_time(self) -> None:
        self.arrange()
        self.to_green_review()
        self.host.dispatch_answer = "crash"
        with self.assertRaises(SimulatedCrash):
            self.tick()
        [intent] = e2e_state(self.card()).runs
        self.assertEqual((intent.sha, intent.run_id, intent.dispatch), (SHA, 0, "intent"))

        self.host.dispatch_answer = "ok"
        recovered = self._runtime()
        # Before the window has settled, one visible match is not attached.
        early = self.tick(recovered)
        self.assertEqual(early["action"], "e2e-identifying", early)
        self.assertIn("recovery_settles_at", early)
        self.assertEqual(self.host.run_listings, 0)
        self.assertEqual(e2e_state(self.card()).runs[0].run_id, 0)

        with self.settled():
            waiting = self.tick(recovered)

        self.assertEqual(waiting["action"], "e2e-waiting", waiting)
        self.assertEqual(len(self.host.dispatches), 1, "recovery never dispatches again")
        self.assertEqual(self.host.run_listings, 1)
        [run] = e2e_state(self.card()).runs
        self.assertEqual((run.dispatch_id, run.run_id, run.head_sha), (intent.dispatch_id, FIRST_RUN, SHA))
        self.assertEqual(run.identified_by, "recovery")
        self.assertIn(f"workflow_dispatch run of e2e.yml on {BRANCH} at {SHA}", run.recovery_rule)
        self.assertEqual(self.card()["e2e"]["runs"][0]["identified_by"], "recovery")
        [said] = self.comments("was identified by recovery, not by GitHub's dispatch answer")
        self.assertIn(RUN_URL, said)
        self.assertTrue(run.wait_ref)

    def test_recovery_ignores_runs_of_another_sha_branch_event_or_time(self) -> None:
        self.arrange()
        self.to_green_review()
        self.host.dispatch_answer = "crash"
        with self.assertRaises(SimulatedCrash):
            self.tick()
        # Before the intent, and others that are not this dispatch: none of them is a match.
        stale = self.host.add_run()
        self.host.runs[stale]["created_at"] = "2026-01-01T00:00:00Z"
        pushed = self.host.add_run()
        self.host.runs[pushed]["event"] = "push"
        other = self.host.add_run()
        self.host.runs[other]["head_branch"] = "main"
        elsewhere = self.host.add_run()
        self.host.runs[elsewhere]["head_sha"] = SECOND_SHA

        with self.settled():
            self.assertEqual(self.tick(self._runtime())["action"], "e2e-waiting")

        [run] = e2e_state(self.card()).runs
        self.assertEqual(run.run_id, FIRST_RUN)
        self.assertEqual(len(self.host.dispatches), 1)

    def test_a_second_run_appearing_during_the_settle_blocks_the_card_and_none_is_guessed(self) -> None:
        self.arrange()
        self.to_green_review()
        self.host.dispatch_answer = "crash"
        with self.assertRaises(SimulatedCrash):
            self.tick()
        self.assertEqual(self.tick(self._runtime())["action"], "e2e-identifying")
        second = self.host.add_run()

        with self.settled():
            blocked = self.tick(self._runtime())

        self.assertBlockedAsInfrastructure(
            blocked, "cannot be told apart", RUN_URL, f"https://github.com/{REPO}/actions/runs/{second}"
        )
        [run] = e2e_state(self.card()).runs
        self.assertEqual(run.run_id, 0)
        self.assertEqual(self.wait_cards(), [])
        self.assertEqual(len(self.host.dispatches), 1)

    def test_a_declared_dispatch_id_input_is_sent_and_recovery_requires_it_in_the_title(self) -> None:
        self.arrange(e2e={**E2E, "dispatch_id_input": "sid"})
        self.to_green_review()
        self.host.dispatch_answer = "crash"
        with self.assertRaises(SimulatedCrash):
            self.tick()
        [intent] = e2e_state(self.card()).runs
        [dispatch] = self.host.dispatches
        self.assertEqual(dispatch["inputs"], {"suite": "mega", "sha": SHA, "sid": intent.dispatch_id})
        self.host.add_run("e2e someone else's dispatch")

        with self.settled():
            self.assertEqual(self.tick(self._runtime())["action"], "e2e-waiting")

        [run] = e2e_state(self.card()).runs
        self.assertEqual(run.run_id, FIRST_RUN)
        self.assertIn(f"its title carrying {intent.dispatch_id}", run.recovery_rule)
        self.assertEqual(len(self.host.dispatches), 1)

    def test_with_a_dispatch_id_input_a_run_without_it_in_the_title_is_never_attached(self) -> None:
        self.arrange(e2e={**E2E, "dispatch_id_input": "sid"})
        self.to_green_review()
        self.host.run_title = "Stand e2e mega"
        self.host.dispatch_answer = "crash"
        with self.assertRaises(SimulatedCrash):
            self.tick()

        with self.settled():
            self.assertEqual(self.tick(self._runtime())["action"], "e2e-identifying")
        self.assertEqual(e2e_state(self.card()).runs[0].run_id, 0)
        with self.settled(minutes=16):
            blocked = self.tick(self._runtime())

        self.assertBlockedAsInfrastructure(blocked, "could not be identified")
        self.assertEqual(e2e_state(self.card()).runs[0].run_id, 0)
        self.assertEqual(self.wait_cards(), [])
        self.assertEqual(len(self.host.dispatches), 1)

    def test_an_answer_without_run_details_is_looked_up(self) -> None:
        self.arrange()
        self.to_green_review()
        self.host.dispatch_answer = "bare"

        self.assertEqual(self.tick()["action"], "e2e-identifying")
        with self.settled():
            self.assertEqual(self.tick()["action"], "e2e-waiting")

        [run] = e2e_state(self.card()).runs
        self.assertEqual((run.run_id, run.dispatch), (FIRST_RUN, "sent"))
        self.assertEqual(self.host.run_listings, 1)

    def test_a_run_on_another_sha_is_not_accepted(self) -> None:
        self.arrange()
        self.to_green_review()
        self.host.run_head_sha = SECOND_SHA

        blocked = self.tick()

        self.assertBlockedAsInfrastructure(blocked, f"ran on `{SECOND_SHA[:12]}`", RUN_URL)
        self.assertEqual(self.wait_cards(), [])

    def test_a_crash_after_the_wait_card_was_created_creates_no_second_one(self) -> None:
        self.arrange()
        self.to_green_review()
        record = TaskWriter.record_e2e_state
        crashed: list[str] = []

        def crash_on_the_wait_ref(writer: TaskWriter, **fields: Any) -> None:
            if not crashed and '"wait_ref":"ummanu-' in fields["state"]:
                crashed.append(fields["state"])
                raise SimulatedCrash("the dispatcher died before recording the wait card")
            record(writer, **fields)

        with (
            mock.patch.object(TaskWriter, "record_e2e_state", crash_on_the_wait_ref),
            self.assertRaises(SimulatedCrash),
        ):
            self.tick()
        self.assertEqual(len(self.wait_cards()), 1)
        self.assertEqual(e2e_state(self.card()).runs[0].wait_ref, "")

        waiting = self.tick(self._runtime())

        self.assertEqual(waiting["action"], "e2e-waiting", waiting)
        [wait] = self.wait_cards()
        self.assertEqual(e2e_state(self.card()).runs[0].wait_ref, wait["ref"])
        self.assertEqual(len(self.host.dispatches), 1)

    def test_a_restart_while_the_run_is_pending_continues_the_card(self) -> None:
        self.to_waiting()
        self.tick_wait()

        restarted = self._runtime()
        self.assertEqual(self.tick(restarted)["action"], "e2e-waiting")
        self.host.run_answer = ("completed", "success")
        self.assertEqual(self.tick_wait(restarted)["action"], "wait-target-reached")
        parked = self.tick(restarted)

        self.assertEqual(parked["to"], "assessment", parked)
        self.assertEqual(len(self.host.dispatches), 1)
        self.assertEqual(len(self.wait_cards()), 1)

    # --- outcomes ------------------------------------------------------------------------------------

    def test_a_failed_run_returns_the_card_to_rework_with_the_evidence_in_task_md(self) -> None:
        self.host.fail_resume_worker_reason = ""
        self.to_waiting()
        self.host.jobs = [
            {"name": "setup", "conclusion": "success", "html_url": RUN_URL, "steps": []},
            {"name": "mega", "conclusion": "failure", "html_url": RUN_URL, "steps": ["Run e2e suite"]},
        ]
        self.conclude("failure")

        reworked = self.tick()

        self.assertEqual(self.card()["state"], "in_progress", reworked)
        document = self._task_document()
        self.assertIn("## Mechanical gate failure to address", document)
        self.assertIn(RUN_URL, document)
        self.assertIn("concluded failure", document)
        self.assertIn("job «mega»", document)
        self.assertIn('step "Run e2e suite"', document)
        self.assertIn("stand 2 never answered its health check", document)
        self.assertEqual(self._pilot_record()["rejected_sha"], SHA)
        self.assertEqual(self.card()["e2e"]["runs"][0]["state"], "failure")
        self.assertEqual(len(self.host.dispatches), 1)

    def test_a_cancelled_run_blocks_the_card_as_infrastructure(self) -> None:
        self.to_waiting()
        wait = self.wait_ref()
        self.conclude("cancelled")

        blocked = self.tick()

        self.assertBlockedAsInfrastructure(blocked, "concluded cancelled", RUN_URL, wait)
        self.assertEqual(self.card()["e2e"]["runs"][0]["state"], "cancelled")

    def test_a_passed_deadline_blocks_the_card(self) -> None:
        self.to_waiting()
        wait = self.wait_ref()
        later = datetime.now(UTC) + timedelta(hours=3)
        with mock.patch("ummanu.dispatch.wait_cards.utcnow", return_value=later):
            self.assertEqual(self.tick_wait()["action"], "wait-ended")
        self.assertEqual(wait_state(self.reader.show(wait)).result["outcome"], "deadline_passed")

        blocked = self.tick()

        self.assertBlockedAsInfrastructure(blocked, "deadline_passed", wait)
        self.assertEqual(self.card()["e2e"]["runs"][0]["state"], "deadline_passed")

    def test_an_unreachable_run_blocks_the_card(self) -> None:
        self.to_waiting()
        wait = self.wait_ref()
        self.host.run_answer = ("http", "gh: Not Found (HTTP 404)")
        self.assertEqual(self.tick_wait()["action"], "wait-ended")

        blocked = self.tick()

        self.assertBlockedAsInfrastructure(blocked, "source_unreachable", wait)

    def test_a_refused_dispatch_blocks_the_card_with_the_reason(self) -> None:
        self.arrange()
        self.to_green_review()
        self.host.dispatch_answer = (
            "http",
            "gh: Workflow does not have 'workflow_dispatch' trigger (HTTP 422)",
        )

        blocked = self.tick()

        self.assertBlockedAsInfrastructure(blocked, "does not have 'workflow_dispatch' trigger", "e2e.yml")
        view = self.card()["e2e"]
        # A run counts when its intent is persisted, whatever GitHub answered (secretary-1796).
        self.assertEqual(view["runs_dispatched"], 1)
        self.assertEqual(view["runs"][0]["state"], "dispatch_refused")
        self.assertEqual(self.wait_cards(), [])

    def test_a_run_never_identified_blocks_the_card_and_is_not_dispatched_again(self) -> None:
        self.arrange()
        self.to_green_review()
        self.host.dispatch_answer = "hidden"
        self.assertEqual(self.tick()["action"], "e2e-identifying")
        self.assertEqual(self.tick()["action"], "e2e-identifying")
        later = datetime.now(UTC) + timedelta(hours=1)
        with mock.patch.object(e2e_stage, "utcnow", return_value=later):
            blocked = self.tick()

        self.assertBlockedAsInfrastructure(
            blocked, "could not be identified", "Nothing was dispatched a second time"
        )
        self.assertEqual(len(self.host.dispatches), 1)
        self.assertEqual(self.wait_cards(), [])

    # --- the per-card cap, outside every sprint ---------------------------------------------------------

    def test_the_fourth_run_of_a_card_outside_every_sprint_is_not_dispatched_and_the_card_is_blocked(self) -> None:
        """Since secretary-1796 the per-card cap binds only a card outside every sprint (a sprint's card
        spends the sprint budget, `tests/test_e2e_budget.py`); with no PO origin it Blocks, and rings."""
        self.arrange(review="skipped", observed=False)
        previous = {
            "runs": [
                {
                    "dispatch_id": f"{CARD_REF}-e2e-{n}-0000000{n}",
                    "sha": sha,
                    "repo": REPO,
                    "branch": BRANCH,
                    "workflow": "e2e.yml",
                    "intent_at": "2026-09-27T10:00:00Z",
                    "dispatch": "sent",
                    "run_id": 8000 + n,
                    "run_url": f"https://github.com/{REPO}/actions/runs/{8000 + n}",
                    "result": {"outcome": "target_reached", "conclusion": "failure", "summary": "red"},
                    # Each was acted on in its own round (the card went to rework, then was reworked).
                    "acted": True,
                }
                for n, sha in ((1, "1" * 40), (2, "2" * 40), (3, SECOND_SHA))
            ]
        }
        self.writer.record_e2e_state(
            role="dispatcher", actor="ummanu-pilot", reference=CARD_REF, state=json.dumps(previous)
        )
        self._run_worker_to_validate()

        blocked = self.tick()

        self.assertBlockedAsInfrastructure(blocked, "e2e run cap reached (3)", taxonomy="other")
        self.assertEqual(blocked["reason"], "e2e run cap reached (3)")
        self.assertEqual(self.host.dispatches, [])
        self.assertEqual(self.card()["e2e"]["runs_dispatched"], 3)
        self.assertEqual(self.card()["e2e"]["run_cap"], 3)
        [bell] = [event for event in OwnerEventStore(self.board.credentials).events() if event.subject_ref == CARD_REF]
        self.assertEqual((bell.kind, bell.event_class), ("e2e_budget_spent", "notice"))
        self.assertFalse(bell.held or bell.pinned)
        self.assertIn("e2e run cap reached (3)", bell.text)


if __name__ == "__main__":
    unittest.main()
