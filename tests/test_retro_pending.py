"""secretary-1714: retro round-trips the curator's versioned pending record and sets a legacy one aside."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ummanu.automations.agents.curator import harvest
from ummanu.automations.agents.retro import cli
from ummanu.runtime.state import PRECHECK_SKIP, AgentState


def claude(text: str) -> str:
    return json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": text}]}}) + "\n"


def claude_tool() -> str:
    return json.dumps(
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "read"}]}}
    ) + "\n"


class Retention:
    def close_old_done(self):
        return {"closed": []}


class RetroPendingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = AgentState("retro", self.root / "state")
        self.workspace = self.root / "retro"
        self.transcript = self.root / "claude.jsonl"
        self.sessions: list[dict] = []
        patches = [
            mock.patch.object(cli, "STATE", self.state),
            mock.patch.dict("os.environ", {"UMMANU_CURATOR_WORKSPACE": str(self.workspace)}),
            mock.patch.object(cli.discover, "claude_sessions", side_effect=lambda: list(self.sessions)),
            mock.patch.object(cli.discover, "codex_sessions", return_value=[]),
            mock.patch.object(cli.discover, "hermes_sessions", return_value=[]),
            mock.patch.object(cli.discover, "all_memory_files", return_value=[]),
            mock.patch.object(cli.search_log, "tail", return_value=[]),
            mock.patch.object(cli.search_log, "render_markdown", return_value=""),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _session(self, text: str) -> None:
        self.transcript.write_text(text, encoding="utf-8")
        self.sessions = [{"head": "claude", "path": str(self.transcript), "session_id": "c", "cwd": "/project"}]

    def _run(self, fn, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = fn(*args)
        return code, out.getvalue(), err.getvalue()

    def _runs(self) -> list[dict]:
        path = self.state.dir / "runs.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def _write_pending(self, text: str) -> None:
        self.state.ensure_dir()
        self.state.pending_file.write_text(text, encoding="utf-8")

    def test_harvest_writes_a_versioned_record_that_advance_consumes(self) -> None:
        self._session(claude("one") + claude("two"))
        self.assertEqual(self._run(cli.cmd_precheck, Retention())[0], 0)
        self.assertFalse(self.state.pending_file.exists(), "precheck publishes no pending record")

        code, out, _ = self._run(cli.cmd_harvest, True, Retention())
        self.assertEqual(code, 0)
        record = json.loads(self.state.pending_file.read_text(encoding="utf-8"))
        self.assertEqual(record["version"], harvest.PENDING_VERSION)
        self.assertEqual(record["identity"], {"workspace": str(self.workspace.resolve())})
        self.assertEqual(harvest.read_pending(self.state, record["identity"]), record)
        emitted = json.loads(out)["batch"]
        self.assertEqual([t["text"] for t in emitted["sessions"][0]["turns"]], ["one", "two"])

        # A repeated harvest before advance replays the same batch, even after the source grew.
        self.transcript.write_text(claude("one") + claude("two") + claude("three"), encoding="utf-8")
        code, out, _ = self._run(cli.cmd_harvest, True, Retention())
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["batch"]["batch_id"], record["batch_id"])
        # A precheck in the same identity sees the replayable batch as work.
        self.assertEqual(self._run(cli.cmd_precheck, Retention())[0], 0)

        code, out, _ = self._run(cli.cmd_advance)
        self.assertEqual(code, 0, out)
        self.assertFalse(self.state.pending_file.exists())
        self.assertEqual(
            self.state.load_watermark()[str(self.transcript)]["offset"], len(claude("one") + claude("two"))
        )

        # The grown tail is the only new work; after it is advanced nothing is new.
        self.assertEqual(self._run(cli.cmd_harvest, True, Retention())[0], 0)
        self.assertEqual(self._run(cli.cmd_advance)[0], 0)
        self.assertEqual(self._run(cli.cmd_precheck, Retention())[0], PRECHECK_SKIP)
        self.assertEqual((self._runs()[-1]["event"], self._runs()[-1]["result"]), ("precheck", "no-change"))

    def test_a_scan_with_nothing_to_judge_settles_cursors_and_advance_is_a_clean_no_op(self) -> None:
        self._session(claude_tool())
        code, out, _ = self._run(cli.cmd_harvest, True, Retention())
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["batch"]["sessions"], [])
        self.assertFalse(self.state.pending_file.exists())
        self.assertEqual(self.state.load_watermark()[str(self.transcript)]["offset"], len(claude_tool()))
        code, _, err = self._run(cli.cmd_advance)
        self.assertEqual(code, 0)
        self.assertIn("nothing pending", err)
        self.assertEqual(self._run(cli.cmd_precheck, Retention())[0], PRECHECK_SKIP)

    def _legacy(self) -> bytes:
        # The live file's shape: a flat {path: {offset, mtime, size}} map with no version.
        legacy = json.dumps({str(self.transcript): {"offset": 7, "mtime": 1790000000.5, "size": 7}}).encode()
        self.state.ensure_dir()
        self.state.pending_file.write_bytes(legacy)
        return legacy

    def _assert_set_aside(self, legacy: bytes) -> None:
        aside = sorted(self.state.dir.glob("pending.legacy-*.json"))
        self.assertEqual(len(aside), 1)
        self.assertRegex(aside[0].name, r"\Apending\.legacy-\d{8}T\d{6}Z\.json\Z")
        self.assertEqual(aside[0].read_bytes(), legacy)
        warnings = [run for run in self._runs() if run["event"] == "pending-set-aside"]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["level"], "warning")
        self.assertEqual(warnings[0]["file"], str(aside[0]))

    def test_precheck_sets_a_legacy_pending_aside_and_reports_the_change(self) -> None:
        self._session(claude("one"))
        legacy = self._legacy()
        code, _, err = self._run(cli.cmd_precheck, Retention())
        self.assertEqual(code, 0, err)
        self.assertNotIn("Traceback", err)
        self.assertFalse(self.state.pending_file.exists())
        self._assert_set_aside(legacy)
        # The sources were never advanced past, so the next harvest reads them again.
        code, out, _ = self._run(cli.cmd_harvest, True, Retention())
        self.assertEqual(code, 0)
        self.assertEqual([t["text"] for t in json.loads(out)["batch"]["sessions"][0]["turns"]], ["one"])
        self.assertEqual(json.loads(self.state.pending_file.read_text(encoding="utf-8"))["version"], 3)
        self._assert_set_aside(legacy)

    def test_precheck_sets_a_legacy_pending_aside_and_skips_when_nothing_is_new(self) -> None:
        legacy = self._legacy()
        self.assertEqual(self._run(cli.cmd_precheck, Retention())[0], PRECHECK_SKIP)
        self._assert_set_aside(legacy)

    def test_harvest_and_advance_set_a_legacy_pending_aside(self) -> None:
        self._session(claude("one"))
        legacy = self._legacy()
        self.assertEqual(self._run(cli.cmd_harvest, True, Retention())[0], 0)
        self._assert_set_aside(legacy)
        self.assertEqual(json.loads(self.state.pending_file.read_text(encoding="utf-8"))["version"], 3)

        for path in self.state.dir.glob("pending.legacy-*.json"):
            path.unlink()
        (self.state.dir / "runs.jsonl").unlink()
        legacy = self._legacy()
        code, _, err = self._run(cli.cmd_advance)
        self.assertEqual(code, 0, err)
        self._assert_set_aside(legacy)
        self.assertEqual(self.state.load_watermark(), {})

    def test_a_non_legacy_refusal_fails_closed_everywhere_and_is_preserved(self) -> None:
        self._session(claude("one"))
        foreign = harvest.pending_record(
            harvest.harvest(AgentState("probe", self.root / "probe"), {"workspace": "/elsewhere"}),
            {"workspace": "/elsewhere"},
            {str(self.transcript): None},
        )
        cases = {
            "identity": json.dumps(foreign),
            "unreadable": "{not json",
            "invalid batch": json.dumps({**foreign, "identity": {"workspace": str(self.workspace.resolve())}, "batch": []}),
        }
        for reason, text in cases.items():
            with self.subTest(reason=reason):
                self._write_pending(text)
                for name, fn, args in (
                    ("precheck", cli.cmd_precheck, (Retention(),)),
                    ("harvest", cli.cmd_harvest, (True, Retention())),
                    ("advance", cli.cmd_advance, ()),
                ):
                    code, out, err = self._run(fn, *args)
                    self.assertEqual(code, 1, (name, out, err))
                    self.assertIn("retro: curator pending record", err)
                    self.assertNotIn("Traceback", err)
                    self.assertEqual(self.state.pending_file.read_text(encoding="utf-8"), text)
                self.assertEqual(list(self.state.dir.glob("pending.legacy-*")), [])
                self.assertEqual(self.state.load_watermark(), {})
        self.assertIn("pending-refused", [run.get("result") for run in self._runs() if run["event"] == "precheck"])

    def test_a_hostile_v3_batch_is_refused_cleanly_by_every_entry_point(self) -> None:
        identity = {"workspace": str(self.workspace.resolve())}
        turn = {"role": "user", "text": "one", "ts": None}
        session = {"head": "claude", "path": "p", "session_id": "c", "cwd": "/project", "turns": [turn]}
        batches = {
            "sessions missing": {"memory": [{}], "pending": {}},
            "sessions a string": {"sessions": "x", "memory": [], "pending": {}},
            "sessions of non-dicts": {"sessions": ["x", 1], "memory": [], "pending": {}},
            "turns missing": {"sessions": [{"head": "claude"}], "memory": [], "pending": {}},
            "turns a string": {"sessions": [{**session, "turns": "x"}], "memory": [], "pending": {}},
            "turns of non-dicts": {"sessions": [{**session, "turns": ["x"]}], "memory": [], "pending": {}},
            "memory a dict": {"sessions": [session], "memory": {}, "pending": {}},
            "pending a list": {"sessions": [session], "memory": [], "pending": []},
            "batch a list": [session],
            # Well-shaped, but a turn the commands cannot render: the retro backstop refuses it.
            "turn without text": {"sessions": [{**session, "turns": [{"role": "user"}]}], "memory": [], "pending": {}},
        }
        for reason, batch in batches.items():
            with self.subTest(reason=reason):
                record = {
                    "version": harvest.PENDING_VERSION,
                    "identity": identity,
                    "base": {},
                    "selector": harvest.selector(None),
                    "batch": batch,
                    "batch_id": harvest._batch_id(identity, batch, {}, None),
                }
                text = json.dumps(record)
                self._write_pending(text)
                for name, fn, args in (
                    ("precheck", cli.cmd_precheck, (Retention(),)),
                    ("harvest", cli.cmd_harvest, (True, Retention())),
                    ("harvest markdown", cli.cmd_harvest, (False, Retention())),
                    ("advance", cli.cmd_advance, ()),
                ):
                    code, out, err = self._run(fn, *args)
                    self.assertEqual(code, 1, (name, out, err))
                    self.assertIn("retro: curator pending record has an invalid batch", err)
                    self.assertNotIn("Traceback", err)
                    self.assertEqual(self.state.pending_file.read_text(encoding="utf-8"), text)
                self.assertEqual(list(self.state.dir.glob("pending.legacy-*")), [])
                self.assertEqual(self.state.load_watermark(), {})


if __name__ == "__main__":
    unittest.main()
