from __future__ import annotations

import unittest
from itertools import product

from ummanu.board.card_transitions import (
    CARD_TRANSITIONS,
    CardMoveFacts,
    CardMoveReasonRequired,
    CardTransitionForbidden,
    admit_card_move,
    card_transition,
)
from ummanu.board.models import Actor, CardState
from ummanu.board.roles import (
    BOARD_ROLES,
    CREATE_ROLES,
    EDIT_ROLES,
    PROPOSAL_CREATE_ROLES,
    Role,
)
from ummanu.task_commands import Role as CommandRole, _role_choices
from ummanu.tasks import TaskError, TaskWriter


class RoleVocabularyTests(unittest.TestCase):
    def test_role_is_public_string_compatible_closed_vocabulary(self) -> None:
        self.assertIs(CommandRole, Role)
        self.assertIs(Role("worker"), Role.WORKER)
        self.assertEqual(str(Role.WORKER), "worker")
        self.assertEqual(
            {role.value for role in BOARD_ROLES},
            {"po", "dispatcher", "worker", "reviewer", "observer", "steward", "retro"},
        )

    def test_actor_normalizes_strings_to_the_canonical_role(self) -> None:
        actor = Actor("worker", "worker-1", "head-run:1")
        self.assertIs(actor.role, Role.WORKER)
        self.assertEqual(actor.role, "worker")
        with self.assertRaises(ValueError):
            Actor("dispatchre", "bad")

    def test_task_writer_parses_roles_at_its_boundary(self) -> None:
        self.assertIs(TaskWriter._role("worker", BOARD_ROLES), Role.WORKER)
        self.assertIs(TaskWriter._role(Role.PO, CREATE_ROLES), Role.PO)
        with self.assertRaises(TaskError):
            TaskWriter._role("dispatcher", CREATE_ROLES)
        with self.assertRaises(TaskError):
            TaskWriter._role("dispatchre", BOARD_ROLES)

    def test_cli_role_choices_are_projected_from_canonical_subsets(self) -> None:
        self.assertEqual(set(_role_choices(BOARD_ROLES)), {role.value for role in BOARD_ROLES})
        self.assertEqual(set(_role_choices(CREATE_ROLES)), {role.value for role in CREATE_ROLES})
        self.assertEqual(set(_role_choices(EDIT_ROLES)), {role.value for role in EDIT_ROLES})
        self.assertEqual(
            {role.value for role in PROPOSAL_CREATE_ROLES},
            {"worker", "reviewer", "retro", "steward"},
        )

    def test_card_transition_registry_uses_the_canonical_role_type(self) -> None:
        self.assertEqual(set(CARD_TRANSITIONS), BOARD_ROLES)
        by_enum = card_transition(Role.DISPATCHER, CardState.READY, CardState.IN_PROGRESS)
        by_string = card_transition("dispatcher", "ready", "in_progress")
        self.assertEqual(by_enum, by_string)

    def test_unknown_role_is_refused_at_the_transition_boundary(self) -> None:
        with self.assertRaises(CardTransitionForbidden):
            card_transition("dispatchre", CardState.READY, CardState.IN_PROGRESS)


class DispatcherStageTests(unittest.TestCase):
    STAGES = frozenset({CardState.IN_PROGRESS, CardState.VALIDATE, CardState.ASSESSMENT})

    @staticmethod
    def holds(role: Role, source: CardState, target: CardState) -> bool:
        try:
            card_transition(role, source, target)
        except CardTransitionForbidden:
            return False
        return True

    def test_only_the_dispatcher_moves_a_card_into_its_stages_and_blocked_stays_an_emergency_stop(self) -> None:
        emergency_sources = {
            Role.PO: set(CardState) - {CardState.BLOCKED, CardState.DONE},
            Role.OBSERVER: set(CardState) - {CardState.BLOCKED, CardState.DONE, CardState.ASSESSMENT},
            Role.STEWARD: {CardState.READY, CardState.IN_PROGRESS, CardState.VALIDATE, CardState.ASSESSMENT},
        }
        for role, source, target in product(Role, CardState, CardState):
            with self.subTest(role=role.value, source=source.value, target=target.value):
                held = self.holds(role, source, target)
                if target in self.STAGES and role is not Role.DISPATCHER:
                    self.assertFalse(held)
                if target is CardState.BLOCKED and source in emergency_sources.get(role, ()):
                    self.assertTrue(held)

    def test_a_refused_move_into_a_dispatcher_stage_names_the_handle_to_use(self) -> None:
        handles = {
            CardState.ASSESSMENT: ("`task decide`",),
            CardState.BLOCKED: ("return the card to Ready", "`task decide --kind rework`"),
            CardState.READY: ("`task cancel`", "`task supersede`"),
        }
        for role, (source, expected) in product((Role.PO, Role.OBSERVER, Role.STEWARD), handles.items()):
            target = CardState.VALIDATE if source is not CardState.ASSESSMENT else CardState.IN_PROGRESS
            with self.subTest(role=role.value, source=source.value):
                with self.assertRaises(CardTransitionForbidden) as raised:
                    card_transition(role, source, target)
                message = str(raised.exception)
                self.assertIn(f"{role.value} may not move {source.value} to {target.value}", message)
                for handle in expected:
                    self.assertIn(handle, message)

    def test_the_steward_stops_a_card_under_review_only_with_a_reason(self) -> None:
        def facts(reason: str) -> CardMoveFacts:
            return CardMoveFacts(
                reason=reason,
                steward_report=False,
                wait_outcome=lambda: None,
                held_by_wait_card=lambda: False,
                after_merge_hotfix=lambda: False,
            )

        with self.assertRaises(CardMoveReasonRequired):
            admit_card_move(Role.STEWARD, CardState.VALIDATE, CardState.BLOCKED, facts("  "))
        declaration = admit_card_move(Role.STEWARD, CardState.VALIDATE, CardState.BLOCKED, facts("head silent"))
        self.assertEqual((declaration.source, declaration.target), (CardState.VALIDATE, CardState.BLOCKED))


if __name__ == "__main__":
    unittest.main()
