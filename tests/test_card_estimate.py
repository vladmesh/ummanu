"""The observer's estimate on a card: refused before the board is read, passed through by the CLI.

`TaskWriter.create` runs over a mock client, so a refusal is proven by the client never being called.
The stored value, `task show` and the export/restore round trip run on the real store in
`tests/test_tasks.py`.
"""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from ummanu.board.estimate import DEFAULT_ESTIMATE_MINUTES, estimate_view
from ummanu.cli import main
from ummanu.tasks import TaskError, TaskWriter

SPRINT = "sprint:1"


class CreateRefusalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.client = mock.Mock(instance_dir=self.tmp)
        self.writer = TaskWriter(self.client, data_dir=self.tmp)

    def create(self, kind: str, estimate: str, **fields: Any) -> dict:
        return self.writer.create(
            role="observer", actor="observer", project="ummanu", task_type=kind, title="T", sprint=SPRINT,
            estimate_minutes=estimate, **fields,
        )

    def test_a_value_that_is_not_a_positive_whole_number_is_refused_unread(self) -> None:
        for kind in ("code", "research", "infra"):
            for value in ("0", "-5", "1.5", "90m", "abc", "+30"):
                with (
                    self.subTest(kind=kind, value=value),
                    self.assertRaisesRegex(TaskError, "positive whole number of minutes") as raised,
                ):
                    self.create(kind, value)
                self.assertEqual(raised.exception.code, "validation")
        self.assertEqual(self.client.mock_calls, [])

    def test_the_flag_on_a_card_no_worker_executes_is_refused_unread(self) -> None:
        for kind, fields in (
            ("decision", {}),
            ("operation", {"touches_production": "none"}),
            ("wait", {"wait": {"until": "2030-01-01T00:00:00Z", "deadline": "2030-01-02T00:00:00Z"}}),
        ):
            with (
                self.subTest(kind=kind),
                self.assertRaisesRegex(TaskError, f"a {kind} card takes none") as raised,
            ):
                self.create(kind, "30", **fields)
            self.assertEqual(raised.exception.code, "validation")
        self.assertEqual(self.client.mock_calls, [])

    def test_a_positive_whole_number_passes_to_the_board(self) -> None:
        # Past the estimate check the create reads the sprint: the refusal is not ours.
        with (
            mock.patch("ummanu.sprints.SprintReader.show", side_effect=RuntimeError("the board")),
            self.assertRaisesRegex(RuntimeError, "the board"),
        ):
            self.create("code", " 45 ")

    def test_the_cli_passes_the_flag_through(self) -> None:
        registry = Path(self.tmp) / "projects"
        registry.mkdir()
        (registry / "ummanu.yaml").write_text("id: ummanu\nenabled: true\n", encoding="utf-8")
        writer = mock.Mock()
        writer.return_value.create.return_value = {"action": "created"}
        with (
            mock.patch("ummanu.tasks.TaskWriter", writer),
            mock.patch("ummanu.task_commands.card_client"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            code = main(
                ["task", "create", "--role", "observer", "--instance", self.tmp, "--data-dir", self.tmp,
                 "--project", "ummanu", "--type", "research", "--title", "T", "--sprint", SPRINT,
                 "--estimate-minutes", "45"]
            )
        self.assertEqual(code, 0)
        self.assertEqual(writer.return_value.create.call_args.kwargs["estimate_minutes"], "45")


class EstimateViewTests(unittest.TestCase):
    def test_a_worker_card_reads_its_value_or_the_default_and_other_kinds_read_none(self) -> None:
        def card(kind: str, **bag: str) -> dict:
            return {"type": kind, "extensions": {"extra": bag}}

        default = {"minutes": DEFAULT_ESTIMATE_MINUTES, "source": "default"}
        self.assertEqual(estimate_view(card("code", estimate_minutes="45")), {"minutes": 45, "source": "card"})
        self.assertEqual(estimate_view(card("infra")), default)
        self.assertEqual(estimate_view({"type": "research"}), default)
        for malformed in ("0", "-1", "x"):
            with self.subTest(malformed=malformed):
                self.assertEqual(estimate_view(card("code", estimate_minutes=malformed)), default)
        for kind in ("decision", "operation", "wait"):
            with self.subTest(kind=kind):
                self.assertIsNone(estimate_view(card(kind, estimate_minutes="45")))


if __name__ == "__main__":
    unittest.main()
