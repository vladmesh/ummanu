"""PO codex turns on the installation's managed CODEX_HOME, and a session moved to the other family.

A real `PoRunner` and `PoService` run the fake CLIs of `tests.po_cli_fakes` against the in-memory PO
store; owner events land in the in-memory store of `tests.owner_event_fakes`.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.owner_event_fakes import FakeOwnerEvents
from tests.po_cli_fakes import FAKE_CLAUDE, FAKE_CODEX, SETTLE_SECONDS, unscoped_test_launch
from tests.po_fake_store import FakeBoard, FakePoStore
from ummanu.board.owner_events import NEEDS_OWNER, NOTICE, PO_SESSION_MOVED, PO_SESSION_MOVED_ACCOUNT
from ummanu.po import store as po_store
from ummanu.po.runner import PoRunner, RunnerError
from ummanu.po.service import PoService

MODELS = {"claude": ("opus",), "codex": ("gpt-6-sol",)}
FINAL_401 = (
    "unexpected status 401 Unauthorized: Missing bearer or basic authentication in header, "
    "url: https://api.openai.com/v1/responses"
)


class PoCodexHomeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="po-")))
        self.data = self.root / "data"
        (self.data / "po").mkdir(parents=True)
        self.home = self.data / "codex-home"
        self.home.mkdir()
        executables = {}
        for name, body in (("claude", FAKE_CLAUDE), ("codex", FAKE_CODEX)):
            path = self.root / "bin" / name
            path.parent.mkdir(exist_ok=True)
            path.write_text(body, encoding="utf-8")
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
            executables[name] = str(path)
        self.log = self.root / "fake.log"
        # The resolver's env rung would win over the data dir; the PO service runs without it.
        self.enterContext(mock.patch.dict(os.environ))
        os.environ.pop("UMMANU_CODEX_HOME", None)
        self.runner = PoRunner(
            FakePoStore(FakeBoard()),
            self.data,
            executables=executables,
            turn_launcher=unscoped_test_launch,
            env={
                **os.environ,
                "FAKE_LOG": str(self.log),
                "CODEX_HOME": str(self.root / "ambient-codex"),
                "FAKE_CODEX_HOME": str(self.home),
            },
        )
        self.events = FakeOwnerEvents()
        self.service = PoService(self.runner, data_dir=self.data, models=MODELS, owner_events=self.events)
        self.addCleanup(self.stop_everything)

    def stop_everything(self) -> None:
        for live in list(self.runner._live.values()):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(live.process.pid, signal.SIGKILL)

    def log_in(self) -> None:
        (self.home / "auth.json").write_text('{"tokens": "fixture"}\n', encoding="utf-8")

    def run_turn(self, session_id: str, text: str) -> po_store.Turn:
        turn = self.runner.send(session_id, text)
        return self.runner.wait(session_id, turn.seq, SETTLE_SECONDS)

    def calls(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def health(self) -> dict:
        return json.loads((self.data / "dispatcher" / "resource_health.json").read_text(encoding="utf-8"))

    def test_a_codex_turn_runs_on_the_managed_home_and_reads_its_model_there(self) -> None:
        self.log_in()
        session = self.runner.create_session("codex", "gpt-6-sol", "high")

        turn = self.run_turn(session.session_id, "hello")

        self.assertEqual(turn.state, po_store.COMPLETED, turn.reason)
        self.assertEqual([call["codex_home"] for call in self.calls()], [str(self.home)])
        self.assertEqual(turn.resolved_model, "gpt-6-sol")

    def test_a_codex_session_without_a_login_is_refused_at_create(self) -> None:
        with self.assertRaises(RunnerError) as raised:
            self.runner.create_session("codex", "gpt-6-sol", "high")

        self.assertIn(f"CODEX_HOME={self.home} codex login", str(raised.exception))
        self.assertEqual(self.runner.store.sessions(), [])
        self.assertEqual(self.runner.create_session("claude", "opus", "high").cli, "claude")

    def refuse_codex(self, *lines: str) -> str:
        self.log_in()
        self.log.with_name(self.log.name + ".refuse-codex").write_text("\n".join(lines) + "\n", encoding="utf-8")
        session = self.runner.create_session("codex", "gpt-6-sol", "high")
        turn = self.run_turn(session.session_id, "hello")
        self.assertEqual(turn.state, po_store.COMPLETED, turn.reason)
        moved = self.runner.store.session(session.session_id)
        self.assertEqual((moved.cli, moved.model), ("claude", "opus"))
        return session.session_id

    def test_a_401_moves_the_session_rings_the_owner_and_records_the_final_error(self) -> None:
        session_id = self.refuse_codex(f"Reconnecting... 2/5 ({FINAL_401})", f"Reconnecting... 5/5 ({FINAL_401})",
                                       FINAL_401)

        [event] = self.events.rows.values()
        self.assertEqual(
            (event.kind, event.event_class, event.subject_ref, event.dedup_key),
            (PO_SESSION_MOVED_ACCOUNT, NEEDS_OWNER, f"po-session:{session_id}",
             f"{PO_SESSION_MOVED_ACCOUNT}:{session_id}:1"),
        )
        self.assertIn("turn 1: picked codex/gpt-6-sol, now running claude/opus", event.text)
        self.assertIn("(auth)", event.text)
        recorded = self.health()["openai-sub"]
        self.assertEqual(recorded["status"], "unauthenticated")
        self.assertIn("401 Unauthorized: Missing bearer", recorded["reason"])
        self.assertNotIn("Reconnecting", recorded["reason"])

    def test_a_server_error_move_is_a_notice(self) -> None:
        session_id = self.refuse_codex("unexpected status 503 Service Unavailable")

        [event] = self.events.rows.values()
        self.assertEqual(
            (event.kind, event.event_class, event.dedup_key),
            (PO_SESSION_MOVED, NOTICE, f"{PO_SESSION_MOVED}:{session_id}:1"),
        )
        self.assertIn("(server)", event.text)
        self.assertEqual(self.health()["openai-sub"]["status"], "unavailable")

    def test_the_last_claude_error_result_is_the_one_recorded(self) -> None:
        self.log_in()
        refusal = self.log.with_name(self.log.name + ".refuse-claude")
        retry, final = "API Error: 503 Service Unavailable", "API Error: 401 invalid token \u00b7 Please run /login"
        for lines, status, summary, other in (
            ((retry, final), "unauthenticated", "401 invalid token", "503"),
            ((final, retry), "unavailable", "503 Service Unavailable", "401"),
        ):
            with self.subTest(final=lines[-1]):
                refusal.write_text("\n".join(lines) + "\n", encoding="utf-8")
                session = self.runner.create_session("claude", "opus", "high")
                turn = self.run_turn(session.session_id, "hello")
                self.assertEqual(turn.state, po_store.COMPLETED, turn.reason)
                self.assertEqual(self.runner.store.session(session.session_id).cli, "codex")
                recorded = self.health()["claude-sub"]
                self.assertEqual(recorded["status"], status)
                self.assertIn(summary, recorded["reason"])
                self.assertNotIn(other, recorded["reason"])


if __name__ == "__main__":
    unittest.main()
