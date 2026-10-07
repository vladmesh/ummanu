"""The one identity convention and the one board client, provable without a database.

The defect this module pins first -- a parser that knew one store word's prefix -- is decidable
from the source, and so is the client's refusal when an installation has no board store.
`tests/test_retired_identity.py` proves the same identities resolve against a real store.
"""

from __future__ import annotations

import json
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest import mock

from tests.retired_board import RETIRED_STORE
from ummanu.board import backend
from ummanu.board.sql_cards import SqlCardClient
from ummanu.tasks import TaskError, project_card_by_reference


class EntityIdentityTests(unittest.TestCase):
    """`<kind>_<store word>_<n>` is minted and read back by one pair of functions."""

    def test_the_identity_is_minted_for_the_store_and_either_kind(self) -> None:
        self.assertEqual(backend.entity_id("task", 468), "task_postgres_468")
        self.assertEqual(backend.entity_id("sprint", 9), "sprint_postgres_9")

    def test_every_minted_identity_reads_back_as_its_number(self) -> None:
        for kind in backend.ENTITY_KINDS:
            with self.subTest(kind=kind):
                self.assertEqual(backend.entity_number(kind, backend.entity_id(kind, 468)), 468)

    def test_an_identity_stored_before_the_cutover_still_reads_back(self) -> None:
        """History carries the store word of the board that minted it; the number is what counts."""
        self.assertEqual(backend.entity_number("task", f"task_{RETIRED_STORE}_12"), 12)
        self.assertEqual(backend.entity_number("sprint", f"sprint_{RETIRED_STORE}_7"), 7)

    def test_an_identity_of_the_other_kind_is_not_this_kind_s_number(self) -> None:
        self.assertIsNone(backend.entity_number("task", f"sprint_{RETIRED_STORE}_9"))
        self.assertIsNone(backend.entity_number("sprint", "task_postgres_468"))

    def test_a_bare_number_is_still_read_and_anything_else_is_not(self) -> None:
        self.assertEqual(backend.entity_number("task", "468"), 468)
        for value in (
            "",
            None,
            "task_postgres_",
            "task_postgres_0",
            "task_postgres_x",
            "task__1",
            "task_Postgres_1",
            "task_post_gres_1",
            "task-postgres-1",
        ):
            with self.subTest(value=value):
                self.assertIsNone(backend.entity_number("task", value))

    def test_minting_refuses_a_kind_outside_the_vocabulary(self) -> None:
        with self.assertRaises(backend.BoardIdentityError):
            backend.entity_id("product", 1)


class SprintReferenceNumberTests(unittest.TestCase):
    def test_only_canonical_ascii_numbered_refs_have_a_number(self) -> None:
        self.assertEqual(backend.sprint_reference_number("sprint:0"), 0)
        self.assertEqual(backend.sprint_reference_number("sprint:1596"), 1596)
        self.assertIsNone(backend.sprint_reference_number("sprint:canary"))
        self.assertIsNone(backend.sprint_reference_number("sprint:١"))

    def test_a_leading_zero_is_refused_instead_of_aliasing_another_ref(self) -> None:
        with self.assertRaisesRegex(backend.BoardIdentityError, "must be canonical"):
            backend.sprint_reference_number("sprint:01")
        with self.assertRaisesRegex(backend.BoardIdentityError, "must be canonical"):
            backend.record_key("sprint", "sprint:01")


class BoardHostIdentityTests(unittest.TestCase):
    """The `report`/`verdict`/`decide` path resolves a card number from any stored identity."""

    def _host(self, identity: str):
        from ummanu.board.sql_host import SqlBoardHost

        host = SqlBoardHost.__new__(SqlBoardHost)
        host.client = object()
        with mock.patch("ummanu.board.sql_host.TaskReader") as reader:
            reader.return_value.show.return_value = {"id": identity, "ref": "ummanu-468"}
            return host._card_task_id("ummanu-468")

    def test_a_card_minted_now_or_before_the_cutover_resolves_to_its_number(self) -> None:
        self.assertEqual(self._host(f"task_{RETIRED_STORE}_468"), 468)
        self.assertEqual(self._host("task_postgres_468"), 468)

    def test_an_identity_outside_the_convention_is_still_refused(self) -> None:
        from ummanu.board.transitions import BoardProtocolError

        for identity in ("sprint_postgres_468", "task-468"):
            with self.subTest(identity=identity), self.assertRaises(BoardProtocolError):
                self._host(identity)


class SprintIdentityTests(unittest.TestCase):
    """`_sprint_number` reads a sprint identity through the same function."""

    def test_a_sprint_minted_now_or_before_the_cutover_resolves_to_its_number(self) -> None:
        from ummanu.sprints import _sprint_number

        self.assertEqual(_sprint_number({"id": f"sprint_{RETIRED_STORE}_9"}), 9)
        self.assertEqual(_sprint_number({"id": "sprint_postgres_9"}), 9)

    def test_a_missing_sprint_is_still_a_named_refusal(self) -> None:
        from ummanu.sprints import _sprint_number

        with self.assertRaises(TaskError) as raised:
            _sprint_number(None)
        self.assertEqual(raised.exception.code, "backend_error")


class BoardClientTests(unittest.TestCase):
    """There is one board client, and nothing in the environment chooses it."""

    def test_every_surface_is_served_by_the_store_and_an_unconfigured_one_refuses_by_name(self) -> None:
        """`BoardStoreError` is a `RuntimeError`; a CLI must not answer with its traceback."""
        for serves in (
            (backend.CARD,),
            (backend.SPRINT,),
            (backend.PRODUCT_ISSUE,),
            (backend.CARD, backend.SPRINT),
        ):
            with self.subTest(serves=serves), self.assertRaises(TaskError) as raised:
                backend.board_client(Path("/nonexistent-instance"), serves=serves)
            self.assertEqual(raised.exception.code, "backend_unavailable")
            self.assertIn("board store", raised.exception.message)

    def test_a_missing_board_store_env_prints_a_named_refusal(self) -> None:
        from ummanu import task_commands

        stderr = StringIO()
        with redirect_stderr(stderr):
            code = task_commands.run_task_command(
                lambda: task_commands.TaskReader(
                    backend.card_client(Path("/nonexistent-instance"))
                ).list()
            )
        document = json.loads(stderr.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(document["error"]["code"], "backend_unavailable")


class DriverRefusalTests(unittest.TestCase):
    """`psycopg`'s exceptions are translated where the adapter raises them, not above it."""

    def test_a_driver_that_is_not_installed_is_a_named_refusal(self) -> None:
        from ummanu.board.sql_cards import _driver_error

        error = _driver_error("open a connection", ModuleNotFoundError("No module named 'psycopg'"))
        self.assertEqual(error.code, "backend_unavailable")
        self.assertIn("driver is not installed", error.message)

    def test_the_client_s_own_refusal_is_already_in_the_dictionary(self) -> None:
        from ummanu.board.sql_cards import SqlCardError

        error = SqlCardError("two cards share one card number")
        self.assertIsInstance(error, TaskError)
        self.assertEqual((error.code, error.exit_code), ("backend_error", 1))



class ArchivedReferenceTests(unittest.TestCase):
    """An archived card's live duplicate is looked for only where one can exist."""

    ARCHIVED: ClassVar[dict[str, object]] = {"id": 7, "reference": "ummanu-7", "is_active": 0}
    LIVE: ClassVar[dict[str, object]] = {"id": 9, "reference": "ummanu-7", "is_active": 1}

    def calls(self, client: object) -> list[str]:
        called: list[str] = []

        def call(method: str, **fields: object) -> object:
            called.append(method)
            return dict(self.ARCHIVED) if method == "getTaskByReference" else [dict(self.LIVE)]

        client.call = call  # type: ignore[attr-defined]
        self.result = project_card_by_reference(client, 1, "ummanu-7")  # type: ignore[arg-type]
        return called

    def test_the_store_answers_an_archived_card_without_listing_the_board(self) -> None:
        # `task_ref` is the store's primary key, so the archived row is the only one.
        self.assertEqual(self.calls(SqlCardClient.__new__(SqlCardClient)), ["getTaskByReference"])
        self.assertEqual(self.result, self.ARCHIVED)

    def test_another_client_still_prefers_a_live_duplicate(self) -> None:
        self.assertEqual(self.calls(SimpleNamespace()), ["getTaskByReference", "getAllTasks"])
        self.assertEqual(self.result, self.LIVE)

if __name__ == "__main__":  # pragma: no cover
    unittest.main()
