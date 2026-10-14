from __future__ import annotations

import unittest

from ummanu.board.card_transitions import (
    CARD_TRANSITIONS,
    CardTransitionForbidden,
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


if __name__ == "__main__":
    unittest.main()
