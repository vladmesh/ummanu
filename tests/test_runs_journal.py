"""runs.jsonl appends: every record lands on its own line, even after a torn write."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ummanu.runtime.state import AgentState


def _records(path: Path) -> list[dict]:
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


class RunsJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = AgentState("curator", state_dir=Path(tmp.name))
        self.runs = self.state.dir / "runs.jsonl"

    def test_a_record_after_a_torn_tail_is_still_readable(self) -> None:
        self.state.log_run("precheck", result="change")
        with self.runs.open("a", encoding="utf-8") as handle:
            handle.write('{"ts": "2026-01-01T00:00:00+00:00", "ev')
        self.state.log_run("advance")
        self.assertEqual([record["event"] for record in _records(self.runs)], ["precheck", "advance"])

    def test_a_short_write_raises_and_the_next_record_survives_it(self) -> None:
        real_write = os.write

        def short(descriptor: int, data: bytes) -> int:
            return real_write(descriptor, data[: len(data) // 2])

        with mock.patch("ummanu.runtime.state.os.write", side_effect=short), self.assertRaises(OSError):
            self.state.log_run("dispatch", action="supervised-started")
        self.state.log_run("advance")
        self.assertEqual([record["event"] for record in _records(self.runs)], ["advance"])

    def test_an_unwritable_journal_raises(self) -> None:
        self.state.dir.mkdir(parents=True, exist_ok=True)
        self.runs.mkdir()
        with self.assertRaises(OSError):
            self.state.log_run("advance")


if __name__ == "__main__":
    unittest.main()
