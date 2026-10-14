"""CON-03: terminal Blocked outcomes fence only their own scopes for one tick."""

from types import SimpleNamespace
from unittest import TestCase, mock

from ummanu.dispatch import production
from ummanu.tasks import TaskError


class ProductionBlockedFenceTests(TestCase):
    def test_terminal_destination_fences_independently_of_action_status(self):
        card = {"ref": "alpha-1", "sprint": "sprint:1", "project": "alpha"}
        for outcome, blocked in (
            ({"status": "ok", "to": "blocked"}, True),
            ({"status": "degraded", "to": "blocked"}, True),
            ({"status": "unexpected", "to": "blocked"}, True),
            ({"to": "blocked"}, True),
            ({"status": "blocked"}, True),
            ({"status": "ok", "to": "validate"}, False),
            ({"status": "waiting"}, False),
            ({"status": "degraded"}, False),
        ):
            with self.subTest(outcome=outcome):
                original = dict(outcome)
                with mock.patch.object(production, "_production_tick_active", return_value=outcome):
                    outcomes, errors, scopes = production._advance_active(
                        SimpleNamespace(), {}, {}, [card]
                    )
                self.assertIs(outcomes[0], outcome)
                self.assertEqual(outcome, original)
                self.assertEqual(errors, [])
                self.assertEqual(scopes, {
                    "sprints": {"sprint:1"} if blocked else set(),
                    "projects": {"alpha"} if blocked else set(),
                })

    def test_scopes_reach_claim_collaborator_and_expire_on_next_tick(self):
        active = {"ref": "alpha-1", "sprint": "sprint:1", "project": "alpha"}
        ready = [
            {"ref": "beta-1", "sprint": "sprint:1", "project": "beta"},
            {"ref": "alpha-2", "sprint": "sprint:2", "project": "alpha"},
            {"ref": "alpha-3", "sprint": "", "project": "alpha"},
            {"ref": "gamma-1", "sprint": "sprint:3", "project": "gamma"},
        ]
        expected_skips = [
            {"ref": "beta-1", "reason": "this sprint has a card blocked in this cycle"},
            {"ref": "alpha-2", "reason": "this project has a card blocked in this cycle"},
            {"ref": "alpha-3", "reason": "this project has a card blocked in this cycle"},
        ]
        for terminal in ({"status": "degraded", "to": "blocked"}, {"to": "blocked"}):
            with self.subTest(outcome=terminal):
                runtime = SimpleNamespace(sprints=mock.Mock())
                runtime.sprints.show.return_value = {"status": "open"}
                records, payload = {}, {}
                with (
                    mock.patch.object(production, "_production_tick_active", return_value=terminal),
                    mock.patch.object(production, "_production_tasks",
                                      side_effect=lambda _runtime, states: ready if states == {"ready"} else []),
                    mock.patch.object(production, "pending_wait_blockers", return_value=[]),
                    mock.patch.object(production, "new_attempt_id", return_value="attempt"),
                    mock.patch.object(production, "claim_ready_task",
                                      return_value={"status": "ok", "ref": "gamma-1"}) as claim,
                ):
                    _, errors, scopes = production._advance_active(runtime, records, payload, [active])
                    self.assertEqual(errors, [])
                    result = production._production_claim_ready(runtime, records, payload, blocked_scopes=scopes)
                    self.assertEqual(result["skipped_ready"], expected_skips)
                    claim.assert_called_once_with(runtime, ready[3], records, payload, "attempt",
                                                  resume_workspace=False, fresh_attempt=True)
                    runtime.sprints.show.assert_called_once_with("sprint:3", include_cards=False)
                    self.assertEqual(payload, {})
                    self.assertEqual(records, {})

                    # After the blocked card leaves Active, the next tick builds fresh scopes.
                    _, errors, next_scopes = production._advance_active(runtime, records, payload, [])
                    self.assertEqual(errors, [])
                    self.assertEqual(next_scopes, {"sprints": set(), "projects": set()})
                    self.assertEqual(scopes, {"sprints": {"sprint:1"}, "projects": {"alpha"}})
                    for candidate in ready[:3]:
                        claim.reset_mock()
                        claim.return_value = {"status": "ok", "ref": candidate["ref"]}
                        with mock.patch.object(production, "_production_tasks",
                                               side_effect=lambda _runtime, states, card=candidate: [card]
                                               if states == {"ready"} else []):
                            result = production._production_claim_ready(
                                runtime, records, payload, blocked_scopes=next_scopes
                            )
                        self.assertNotIn("skipped_ready", result)
                        claim.assert_called_once_with(runtime, candidate, records, payload, "attempt",
                                                      resume_workspace=False, fresh_attempt=True)

    def test_missing_scopes_never_create_empty_tokens_or_global_fence(self):
        for card, expected in (
            ({"ref": "alpha-1", "project": "alpha"}, {"sprints": set(), "projects": {"alpha"}}),
            ({"ref": "alpha-1", "sprint": "sprint:1"}, {"sprints": {"sprint:1"}, "projects": set()}),
            ({"ref": "alpha-1", "sprint": "", "project": ""}, {"sprints": set(), "projects": set()}),
        ):
            with (
                self.subTest(card=card),
                mock.patch.object(production, "_production_tick_active", return_value={"to": "blocked"}),
                mock.patch.object(production, "_production_tasks", return_value=[{"ref": "other-1"}]),
                mock.patch.object(production, "pending_wait_blockers", return_value=[]),
                mock.patch.object(production, "claim_ready_task", return_value={"status": "ok"}) as claim,
            ):
                runtime = SimpleNamespace()
                _, errors, scopes = production._advance_active(runtime, {}, {}, [card])
                self.assertEqual(errors, [])
                self.assertEqual(scopes, expected)
                result = production._production_claim_ready(runtime, {}, {}, blocked_scopes=scopes)
                claim.assert_called_once()
                self.assertNotIn("skipped_ready", result)

    def test_step_errors_still_accumulate_without_rewriting_outcomes(self):
        cards = [{"ref": "alpha-1"}, {"ref": "alpha-2"}, {"ref": "alpha-3"}]
        outcome = {"to": "blocked"}
        with mock.patch.object(production, "_production_tick_active", side_effect=[
            TaskError("unavailable", "board unavailable", 1), ValueError("bad step"), outcome,
        ]):
            outcomes, errors, scopes = production._advance_active(SimpleNamespace(), {}, {}, cards)
        self.assertIs(outcomes[0], outcome)
        self.assertEqual(errors[0], {"ref": "alpha-1", "code": "unavailable", "message": "board unavailable"})
        self.assertEqual(errors[1], {"ref": "alpha-2", "code": "unexpected_error", "message": "ValueError"})
        self.assertEqual(scopes, {"sprints": set(), "projects": set()})
