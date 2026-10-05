"""PO head turns through `ummanu.po.runner` with fake `claude`/`codex` against PostgreSQL 16.

The fakes are scripts the runner is pointed at. Each one logs its argv, cwd and stdin, prints an
event stream that carries reasoning and a tool call beside the final answer, and changes behaviour
on words in the owner's message: `SLEEP` keeps the turn running with a child in its process group,
`FAIL` exits non-zero, `SILENT` exits zero without a final answer.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from tests.po_cli_fakes import FAKE_CLAUDE, FAKE_CODEX, SETTLE_SECONDS, eventually, unscoped_test_launch
from tests.sql_backend_fixtures import PostgresBoard
from ummanu.po import store as po_store
from ummanu.po.runner import (
    PoRunner,
    RunnerError,
    claude_resolved_model,
    codex_resolved_model,
    codex_thread_id,
    process_identity,
)
from ummanu.po.store import PoStore, PoStoreError, TurnInProgress

BOARD: PostgresBoard

SECRETS = ("THINKING-SECRET", "TOOL-CALL-SECRET")

# A Claude stand-in that records how the PO workspace's `ummanu` commands resolve inside a turn.
RECORDING_CLAUDE = """#!/bin/sh
cat >/dev/null
{
  echo "python3=$(command -v python3)"
  echo "ummanu=$(command -v ummanu)"
  python3 -P -m ummanu --help >/dev/null 2>&1; echo "module=$?"
  ummanu --help >/dev/null 2>&1; echo "script=$?"
  echo "cwd=$(pwd)"
  echo "home=$HOME"
  echo "mark=$UMMANU_PO_MARK/$TA_PO_MARK"
} > "$FAKE_LOG"
printf '%s\\n' '{"type": "result", "subtype": "success", "is_error": false, "result": "recorded"}'
"""


def setUpModule() -> None:
    global BOARD
    for module in ("psycopg", "sqlalchemy", "alembic"):
        __import__(module)
    BOARD = PostgresBoard()


def tearDownModule() -> None:
    BOARD.stop()


def alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state not in ("Z", "X")


class PoRunnerTests(unittest.TestCase):
    def test_native_feed_metadata_commits_with_claim_and_replay_keeps_original_text(self):
        from ummanu.po.store import RequestConflict

        session = self.runner.create_session("claude", "opus", effort="high")
        metadata = {"source": "dispatcher", "summary": "Which cut?",
                    "sprint_ref": "sprint:50", "comment_position": 50}
        facts = {"card_ref": "ummanu-50", "kind": "decision", "sprint_ref": "sprint:50",
                 "touches_production": None, "input": "card", "deliver_sprint_comments": True}
        turn, created = self.runner.send_request(session.session_id, "Which cut?", "native-input",
                                                  card=facts, note="frozen comments", metadata=metadata)
        self.assertTrue(created)
        self.settle(session.session_id, turn.seq)
        feed = PoStore(self.store.credentials).feed(session.session_id)
        self.assertEqual(feed[0].metadata, metadata)
        self.assertEqual(feed[0].text, "Which cut?\n\nfrozen comments\n")
        self.assertIsNone(feed[1].metadata)
        replay, created = self.runner.send_request(session.session_id, "Which cut?", "native-input",
                                                    card=facts, note="changed", metadata={"source": "web"})
        self.assertFalse(created)
        self.assertEqual(replay.seq, turn.seq)
        self.assertEqual(self.store.feed(session.session_id), feed)
        with self.assertRaises(RequestConflict):
            self.runner.send_request(session.session_id, "Changed", "native-input", card=facts)
        self.assertEqual(self.store.feed(session.session_id), feed)

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.data = self.root / "data"
        (self.data / "po").mkdir(parents=True)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        self.executables = {}
        for name, body in (("claude", FAKE_CLAUDE), ("codex", FAKE_CODEX)):
            path = bin_dir / name
            path.write_text(body, encoding="utf-8")
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
            self.executables[name] = str(path)
        self.log = self.root / "fake.log"
        config = BOARD.fresh_database()
        self.addCleanup(BOARD.drop_database, config.dbname)
        self.store = PoStore(config.for_role("app"))
        self.runner = self.make_runner()
        self.addCleanup(self.stop_everything)

    def make_runner(self) -> PoRunner:
        # A Codex home of the test's own: the fake writes its rollouts there, and the runner reads
        # a turn's model from there, never from the one on this host.
        codex_home = str(self.root / "codex-home")
        return PoRunner(
            self.store,
            self.data,
            executables=self.executables,
            turn_launcher=unscoped_test_launch,
            env={
                **os.environ,
                "FAKE_LOG": str(self.log),
                "CODEX_HOME": codex_home,
                "FAKE_CODEX_HOME": codex_home,
            },
        )

    def stop_everything(self) -> None:
        for turn in self.store.running_turns():
            self.runner.stop(turn.session_id)
        pids = self.log.with_name(self.log.name + ".pids")
        if pids.exists():
            for line in pids.read_text().splitlines():
                for pid in line.split():
                    try:
                        os.kill(int(pid), 9)
                    except (ProcessLookupError, PermissionError):
                        pass

    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def spawned(self, count: int) -> list[tuple[int, int]]:
        pids = self.log.with_name(self.log.name + ".pids")
        eventually(
            lambda: pids.exists() and len(pids.read_text().splitlines()) >= count,
            "the fake never reached its sleeping child",
        )
        return [tuple(int(pid) for pid in line.split()) for line in pids.read_text().splitlines()]

    def settle(self, session_id: str, seq: int) -> po_store.Turn:
        turn = self.runner.wait(session_id, seq, SETTLE_SECONDS)
        self.assertNotEqual(turn.state, po_store.RUNNING)
        return turn

    def feed(self, session_id: str) -> list[tuple[int, str, str]]:
        return [(entry.turn_seq, entry.role, entry.text) for entry in self.store.feed(session_id)]

    def assert_feed_has_no_events(self, session_id: str) -> None:
        for _seq, _role, text in self.feed(session_id):
            for secret in SECRETS:
                self.assertNotIn(secret, text)

    # --- first turn and resume -------------------------------------------------------------

    def test_claude_first_turn_and_resume_use_the_ummanus_session_id(self) -> None:
        session = self.runner.create_session("claude", "opus", "high")
        self.assertEqual(session.cwd, str(self.data / "po"))
        self.assertIsNotNone(session.cli_session_id)
        sid = session.cli_session_id

        first = self.settle(session.session_id, self.runner.send(session.session_id, "remember 42").seq)
        second = self.settle(session.session_id, self.runner.send(session.session_id, "what was it").seq)

        self.assertEqual((first.state, second.state), (po_store.COMPLETED, po_store.COMPLETED))
        calls = self.calls()
        base = [
            "-p",
            "--output-format",
            "json",
            "--model",
            "opus",
            "--effort",
            "high",
            "--dangerously-skip-permissions",
        ]
        self.assertEqual(calls[0]["argv"], [*base, "--session-id", sid])
        self.assertEqual(calls[1]["argv"], [*base, "--resume", sid])
        self.assertEqual({call["cwd"] for call in calls}, {str(self.data / "po")})
        self.assertEqual([call["prompt"] for call in calls], ["remember 42", "what was it"])
        self.assertEqual(
            self.feed(session.session_id),
            [
                (1, "owner", "remember 42"),
                (1, "agent", f"claude --session-id {sid}: remember 42"),
                (2, "owner", "what was it"),
                (2, "agent", f"claude --resume {sid}: what was it"),
            ],
        )
        self.assert_feed_has_no_events(session.session_id)
        stdout = Path(first.stdout_path)
        self.assertEqual(stdout.parent, self.data / "po-runs" / session.session_id)
        self.assertIn("THINKING-SECRET", stdout.read_text())
        self.assertIn('"type": "result"', stdout.read_text())

    def test_codex_first_turn_captures_the_thread_id_and_resume_uses_it(self) -> None:
        session = self.runner.create_session("codex", "gpt-5.5", "high")
        self.assertIsNone(session.cli_session_id)

        first = self.settle(session.session_id, self.runner.send(session.session_id, "remember 42").seq)
        self.assertEqual(self.store.session(session.session_id).cli_session_id, "019a-fake-thread")
        second = self.settle(session.session_id, self.runner.send(session.session_id, "what was it").seq)

        self.assertEqual((first.state, second.state), (po_store.COMPLETED, po_store.COMPLETED))
        runs = self.data / "po-runs" / session.session_id
        options = [
            "--json",
            "-m",
            "gpt-5.5",
            "-c",
            "model_reasoning_effort=high",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
        ]
        calls = self.calls()
        self.assertEqual(
            calls[0]["argv"],
            ["exec", *options, "-o", str(runs / "turn-0001.last-message"), "-C", str(self.data / "po"), "-"],
        )
        self.assertEqual(
            calls[1]["argv"],
            ["exec", "resume", *options, "-o", str(runs / "turn-0002.last-message"), "019a-fake-thread", "-"],
        )
        self.assertEqual({call["cwd"] for call in calls}, {str(self.data / "po")})
        self.assertEqual(
            self.feed(session.session_id),
            [
                (1, "owner", "remember 42"),
                (1, "agent", "codex new 019a-fake-thread: remember 42"),
                (2, "owner", "what was it"),
                (2, "agent", "codex resume 019a-fake-thread: what was it"),
            ],
        )
        self.assert_feed_has_no_events(session.session_id)
        self.assertIn("TOOL-CALL-SECRET", Path(second.stdout_path).read_text())

    # --- one running turn, parallel sessions -------------------------------------------------

    def test_a_second_send_is_refused_while_a_turn_runs_and_two_sessions_run_at_once(self) -> None:
        first = self.runner.create_session("claude", "opus", "high")
        other = self.runner.create_session("codex", "gpt-5.5", "high")
        self.runner.send(first.session_id, "SLEEP one")
        self.runner.send(other.session_id, "SLEEP two")
        self.spawned(2)

        with self.assertRaises(TurnInProgress):
            self.runner.send(first.session_id, "hurry up")

        self.assertEqual([turn.seq for turn in self.store.turns(first.session_id)], [1])
        self.assertEqual(self.feed(first.session_id), [(1, "owner", "SLEEP one")])
        running = self.store.running_turns()
        self.assertEqual(
            sorted(turn.session_id for turn in running), sorted([first.session_id, other.session_id])
        )
        self.assertTrue(all(turn.pid and alive(turn.pid) for turn in running))

    def test_the_database_itself_refuses_a_second_running_turn(self) -> None:
        import psycopg

        session = self.runner.create_session("claude", "opus", "high")
        self.store.begin_turn(session.session_id, "one", lambda seq: self.root / f"{seq}.out")
        with (
            psycopg.connect(self.store.credentials.conninfo()) as connection,
            self.assertRaises(psycopg.errors.UniqueViolation),
        ):
            connection.execute(
                "INSERT INTO po_turns (session_id, seq, started_at, state, stdout_path) "
                "VALUES (%s, 2, now(), 'running', 'x')",
                (session.session_id,),
            )

    # --- stop -------------------------------------------------------------------------------

    def test_stop_kills_the_process_group_and_the_session_resumes(self) -> None:
        session = self.runner.create_session("claude", "opus", "high")
        self.runner.send(session.session_id, "SLEEP please")
        [(leader, child)] = self.spawned(1)

        stopped = self.runner.stop(session.session_id)

        self.assertEqual(stopped.state, po_store.INTERRUPTED)
        self.assertEqual(stopped.reason, "stopped by the owner")
        eventually(lambda: not alive(leader) and not alive(child), "the turn's process group survived stop")
        self.assertEqual(self.store.turn(session.session_id, 1).state, po_store.INTERRUPTED)

        again = self.settle(session.session_id, self.runner.send(session.session_id, "again").seq)

        self.assertEqual(again.state, po_store.COMPLETED)
        # The stopped turn had saved the conversation: `--session-id` is refused, and the same turn
        # goes on with `--resume`.
        self.assertEqual(
            [call["argv"][-2:] for call in self.calls()[-2:]],
            [["--session-id", session.cli_session_id], ["--resume", session.cli_session_id]],
        )
        self.assertEqual([turn.seq for turn in self.store.turns(session.session_id)], [1, 2])
        self.assertEqual(
            self.feed(session.session_id),
            [
                (1, "owner", "SLEEP please"),
                (2, "owner", "again"),
                (2, "agent", f"claude --resume {session.cli_session_id}: again"),
            ],
        )

    def test_a_stopped_first_claude_turn_that_saved_nothing_starts_its_conversation_again(self) -> None:
        session = self.runner.create_session("claude", "opus", "high")
        self.runner.send(session.session_id, "SLEEP NOPERSIST")
        self.spawned(1)
        self.assertEqual(self.runner.stop(session.session_id).state, po_store.INTERRUPTED)

        again = self.settle(session.session_id, self.runner.send(session.session_id, "again").seq)
        then = self.settle(session.session_id, self.runner.send(session.session_id, "and then").seq)

        self.assertEqual((again.state, then.state), (po_store.COMPLETED, po_store.COMPLETED))
        sid = session.cli_session_id
        self.assertEqual(
            [call["argv"][-2:] for call in self.calls()],
            [["--session-id", sid], ["--session-id", sid], ["--resume", sid]],
        )
        self.assertEqual(
            self.feed(session.session_id),
            [
                (1, "owner", "SLEEP NOPERSIST"),
                (2, "owner", "again"),
                (2, "agent", f"claude --session-id {sid}: again"),
                (3, "owner", "and then"),
                (3, "agent", f"claude --resume {sid}: and then"),
            ],
        )

    def test_a_process_that_cannot_be_recorded_is_killed_with_its_group(self) -> None:
        pids = self.log.with_name(self.log.name + ".pids")
        for store_answers in (True, False):
            with self.subTest(store_answers=store_answers):
                if pids.exists():
                    pids.unlink()
                session = self.runner.create_session("codex", "m", "high")
                real_finish = self.store.finish_turn

                def record_fails(*_args, **_kwargs):
                    # Let the child reach its grandchild first, so the kill has a group to reach.
                    self.spawned(1)
                    raise PoStoreError("the board store went away")

                def finish(*args, store_answers=store_answers, real_finish=real_finish, **kwargs):
                    if not store_answers:
                        raise PoStoreError("the board store is still away")
                    return real_finish(*args, **kwargs)

                with (
                    mock.patch.object(self.store, "record_process", side_effect=record_fails),
                    mock.patch.object(self.store, "finish_turn", side_effect=finish),
                    self.assertRaises(PoStoreError),
                ):
                    self.runner.send(session.session_id, "SLEEP forever")

                [(leader, grandchild)] = self.spawned(1)
                eventually(
                    lambda leader=leader, grandchild=grandchild: not alive(leader) and not alive(grandchild),
                    "the unrecorded group survived",
                )
                turn = self.store.turn(session.session_id, 1)
                self.assertIsNone(turn.pid)
                self.assertIsNone(turn.process_identity)
                if store_answers:
                    self.assertEqual(turn.state, po_store.FAILED)
                    self.assertIn("could not be recorded", turn.reason)
                else:
                    self.assertEqual(turn.state, po_store.RUNNING)
                    [recovered] = self.make_runner().recover()
                    self.assertEqual(
                        (recovered.session_id, recovered.state), (session.session_id, po_store.INTERRUPTED)
                    )
                    self.assertNotIn("killed", recovered.reason)
                self.assertEqual(self.feed(session.session_id), [(1, "owner", "SLEEP forever")])
                self.assertEqual(self.store.running_turns(), [])

    def test_a_stopped_codex_first_turn_keeps_its_thread_id_for_resume(self) -> None:
        session = self.runner.create_session("codex", "gpt-5.5", "high")
        self.runner.send(session.session_id, "SLEEP please")
        self.spawned(1)

        self.runner.stop(session.session_id)
        again = self.settle(session.session_id, self.runner.send(session.session_id, "again").seq)

        self.assertEqual(again.state, po_store.COMPLETED)
        self.assertEqual(self.calls()[-1]["argv"][:2], ["exec", "resume"])
        self.assertEqual(self.calls()[-1]["argv"][-2:], ["019a-fake-thread", "-"])

    def test_stop_without_a_running_turn_does_nothing(self) -> None:
        session = self.runner.create_session("claude", "opus", "high")
        self.assertIsNone(self.runner.stop(session.session_id))

    # --- recovery ---------------------------------------------------------------------------

    def test_recover_interrupts_running_turns_and_kills_only_their_own_processes(self) -> None:
        own = self.runner.create_session("claude", "opus", "high")
        reused = self.runner.create_session("codex", "gpt-5.5", "high")
        own_turn = self.store.begin_turn(own.session_id, "left running", lambda seq: self.root / "own.out")
        reused_turn = self.store.begin_turn(
            reused.session_id, "also left", lambda seq: self.root / "reused.out"
        )
        # The process a previous service run started for `own`, still alive after that run ended.
        orphan = subprocess.Popen(["sleep", "300"], start_new_session=True)
        # A stranger that now holds the PID recorded for `reused`: its identity is not the one stored.
        stranger = subprocess.Popen(["sleep", "300"], start_new_session=True)
        for process in (stranger, orphan):
            self.addCleanup(process.wait)
            self.addCleanup(process.kill)
        self.store.record_process(own.session_id, own_turn.seq, orphan.pid, process_identity(orphan.pid))
        self.store.record_process(reused.session_id, reused_turn.seq, stranger.pid, "some-other-boot:12345")

        recovered = self.make_runner().recover()

        self.assertEqual(
            sorted((turn.session_id, turn.state) for turn in recovered),
            sorted([(own.session_id, po_store.INTERRUPTED), (reused.session_id, po_store.INTERRUPTED)]),
        )
        self.assertEqual(orphan.wait(timeout=SETTLE_SECONDS), -9)
        self.assertIsNone(stranger.poll())
        self.assertEqual(self.store.running_turns(), [])
        self.assertIn("its process was killed", self.store.turn(own.session_id, 1).reason)
        self.assertNotIn("killed", self.store.turn(reused.session_id, 1).reason)
        self.assertEqual(self.feed(own.session_id), [(1, "owner", "left running")])
        self.assertEqual(self.feed(reused.session_id), [(1, "owner", "also left")])

        again = self.settle(own.session_id, self.runner.send(own.session_id, "after restart").seq)
        self.assertEqual(again.state, po_store.COMPLETED)

    # --- failure ----------------------------------------------------------------------------

    def test_a_failing_process_fails_the_turn_and_leaves_the_feed_whole(self) -> None:
        for cli, status in (("claude", 3), ("codex", 4)):
            with self.subTest(cli=cli):
                session = self.runner.create_session(cli, "m", "high")
                done = self.settle(session.session_id, self.runner.send(session.session_id, "hello").seq)
                failed = self.settle(session.session_id, self.runner.send(session.session_id, "FAIL now").seq)

                self.assertEqual((done.state, failed.state), (po_store.COMPLETED, po_store.FAILED))
                self.assertIn(f"exited with status {status}", failed.reason)
                self.assertIn(f"boom from fake {cli}", failed.reason)
                self.assertEqual(
                    [(seq, role) for seq, role, _text in self.feed(session.session_id)],
                    [(1, "owner"), (1, "agent"), (2, "owner")],
                )

    def test_a_zero_exit_without_a_final_answer_fails_the_turn(self) -> None:
        for cli in ("claude", "codex"):
            with self.subTest(cli=cli):
                session = self.runner.create_session(cli, "m", "high")
                turn = self.settle(session.session_id, self.runner.send(session.session_id, "SILENT").seq)

                self.assertEqual(turn.state, po_store.FAILED)
                self.assertTrue(turn.reason)
                self.assertEqual(self.feed(session.session_id), [(1, "owner", "SILENT")])

    def test_a_missing_executable_fails_the_turn_with_its_reason(self) -> None:
        self.executables["claude"] = str(self.root / "no-such-claude")
        runner = self.make_runner()
        session = runner.create_session("claude", "opus", "high")

        with self.assertRaises(RuntimeError):
            runner.send(session.session_id, "hello")

        turn = self.store.turn(session.session_id, 1)
        self.assertEqual(turn.state, po_store.FAILED)
        self.assertIn("could not start", turn.reason)
        self.assertEqual(self.feed(session.session_id), [(1, "owner", "hello")])

    # --- environment ------------------------------------------------------------------------

    def test_a_turn_runs_the_ummanu_cli_from_the_product_runtime_unless_env_is_given(self) -> None:
        # A host whose system `python3` and `ummanu` cannot import the product.
        system = self.root / "system-bin"
        system.mkdir()
        for name in ("python3", "ummanu"):
            path = system / name
            path.write_text(
                "#!/bin/sh\necho \"No module named 'referencing'\" >&2\nexit 1\n", encoding="utf-8"
            )
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        recorder = self.root / "bin" / "recording-claude"
        recorder.write_text(RECORDING_CLAUDE, encoding="utf-8")
        recorder.chmod(recorder.stat().st_mode | stat.S_IXUSR)
        service = {
            **os.environ,
            "PATH": f"{system}{os.pathsep}{os.environ.get('PATH', '')}",
            "UMMANU_PO_MARK": "kept",
            "TA_PO_MARK": "kept",
            "FAKE_LOG": str(self.log),
        }
        service.pop("PYTHONPATH", None)

        def turn(runner: PoRunner) -> dict[str, str]:
            session = runner.create_session("claude", "m", "high")
            settled = runner.wait(
                session.session_id, runner.send(session.session_id, "hi").seq, SETTLE_SECONDS
            )
            self.assertEqual(settled.state, po_store.COMPLETED, settled.reason)
            return dict(line.split("=", 1) for line in self.log.read_text(encoding="utf-8").splitlines())

        with mock.patch.dict(os.environ, service, clear=True):
            built = PoRunner(self.store, self.data, executables={"claude": str(recorder)},
                             turn_launcher=unscoped_test_launch)
        seen = turn(built)
        runtime = Path(sys.executable).parent
        self.assertEqual(Path(seen["python3"]), runtime / "python3")
        self.assertEqual(Path(seen["ummanu"]), runtime / "ummanu")
        self.assertEqual((seen["module"], seen["script"]), ("0", "0"))
        self.assertEqual(Path(seen["cwd"]).resolve(), (self.data / "po").resolve())
        self.assertEqual((seen["home"], seen["mark"]), (os.environ.get("HOME", ""), "kept/kept"))

        explicit = PoRunner(self.store, self.data, executables={"claude": str(recorder)}, env=service,
                            turn_launcher=unscoped_test_launch)
        seen = turn(explicit)
        self.assertEqual(Path(seen["python3"]), system / "python3")
        self.assertEqual(seen["module"], "1")

    # --- effort and the resolved model -----------------------------------------------------

    def test_a_claude_effort_is_passed_on_every_turn_and_the_turn_keeps_the_full_model_id(self) -> None:
        session = self.runner.create_session("claude", "opus", "high")
        self.assertEqual(self.store.session(session.session_id).effort, "high")

        first = self.settle(session.session_id, self.runner.send(session.session_id, "one").seq)
        second = self.settle(session.session_id, self.runner.send(session.session_id, "two").seq)

        for call in self.calls():
            argv = call["argv"]
            self.assertEqual(
                argv[argv.index("--model") : argv.index("--model") + 4],
                ["--model", "opus", "--effort", "high"],
            )
        # The session's own model, not the subagent's that `modelUsage` also names.
        self.assertEqual(
            (first.resolved_model, second.resolved_model), ("claude-opus-5-5", "claude-opus-5-5")
        )
        self.assertEqual(self.store.session(session.session_id).resolved_model, "claude-opus-5-5")
        self.assertEqual(self.store.sessions()[0].resolved_model, "claude-opus-5-5")

    def test_a_session_stored_with_default_resumes_with_no_flag_stops_and_closes(self) -> None:
        """A session opened before an effort had to be chosen keeps working as it did."""
        for cli, model in (("claude", "sonnet"), ("codex", "gpt-5.5")):
            with self.subTest(cli=cli):
                # The store still writes the legacy value; only a new session's create refuses it.
                session = self.store.create_session(
                    session_id=str(uuid.uuid4()),
                    cli=cli,
                    model=model,
                    cwd=str(self.runner.workspace),
                    cli_session_id=str(uuid.uuid4()) if cli == "claude" else None,
                )
                self.assertEqual(self.store.session(session.session_id).effort, po_store.DEFAULT_EFFORT)
                before = len(self.calls())
                self.settle(session.session_id, self.runner.send(session.session_id, "one").seq)
                self.settle(session.session_id, self.runner.send(session.session_id, "two").seq)
                self.runner.send(session.session_id, "SLEEP please")
                self.spawned(1 if cli == "claude" else 2)
                self.assertEqual(self.runner.stop(session.session_id).state, po_store.INTERRUPTED)

                argvs = [call["argv"] for call in self.calls()[before:]]
                self.assertEqual(len(argvs), 3)
                for argv in argvs:
                    self.assertNotIn("--effort", argv)
                    self.assertFalse([part for part in argv if "model_reasoning_effort" in part])
                resumed = argvs[1][-2:] if cli == "claude" else argvs[1][:2]
                self.assertEqual(resumed[0], "--resume" if cli == "claude" else "exec")
                closed = self.store.close_session(session.session_id, "owner")
                self.assertEqual(closed.state, po_store.SESSION_CLOSED)

    def test_a_new_session_needs_an_explicit_offered_effort(self) -> None:
        for effort in ("", "  ", "default", "none"):
            with self.subTest(effort=effort), self.assertRaises(RunnerError) as refused:
                self.runner.create_session("claude", "opus", effort)
            self.assertIn("explicit effort", str(refused.exception))
            self.assertIn("high, low, medium, xhigh, max", str(refused.exception))
        with self.assertRaises(RunnerError):
            self.runner.create_session_request("codex", "gpt-5.5", "req-max", "max")
        with self.assertRaises(RunnerError):
            self.runner.create_session("claude", "opus", "max", efforts={"claude": ("high",)})
        self.assertEqual(self.runner.create_session("claude", "opus", " max ").effort, "max")
        self.assertEqual(len(self.store.sessions()), 1)

    def test_a_codex_effort_is_a_config_override_and_the_model_comes_from_the_rollout(self) -> None:
        session = self.runner.create_session("codex", "gpt-5.6-terra", "xhigh")

        first = self.settle(session.session_id, self.runner.send(session.session_id, "one").seq)
        second = self.settle(session.session_id, self.runner.send(session.session_id, "two").seq)

        for call in self.calls():
            argv = call["argv"]
            index = argv.index("-m")
            self.assertEqual(
                argv[index : index + 4], ["-m", "gpt-5.6-terra", "-c", "model_reasoning_effort=xhigh"]
            )
        self.assertEqual(self.calls()[1]["argv"][:2], ["exec", "resume"])
        self.assertEqual((first.resolved_model, second.resolved_model), ("gpt-5.6-terra", "gpt-5.6-terra"))

    def test_a_codex_turn_with_no_rollout_resolves_nothing(self) -> None:
        self.runner.env.pop("FAKE_CODEX_HOME")
        session = self.runner.create_session("codex", "gpt-5.5", "high")

        turn = self.settle(session.session_id, self.runner.send(session.session_id, "one").seq)

        self.assertEqual(turn.state, po_store.COMPLETED)
        self.assertIsNone(turn.resolved_model)
        self.assertIsNone(self.store.session(session.session_id).resolved_model)

    def test_a_sprint_session_request_opens_a_session_the_store_records_and_replays(self) -> None:
        """The resolver's operation passes the real `po_request_operation_in_vocabulary` CHECK."""
        fingerprint = po_store.sprint_session_fingerprint("sprint:7")
        session, created = self.runner.create_session_request(
            "claude", "opus", "resolve-1", "high", operation=po_store.SPRINT_SESSION, fingerprint=fingerprint
        )
        again, created_again = self.runner.create_session_request(
            "claude", "opus", "resolve-1", "high", operation=po_store.SPRINT_SESSION, fingerprint=fingerprint
        )

        self.assertEqual((created, created_again, again.session_id), (True, False, session.session_id))
        self.assertEqual(self.store.session(session.session_id).state, po_store.SESSION_OPEN)
        request = self.store.request("resolve-1")
        self.assertEqual(
            (request.operation, request.fingerprint, request.session_id, request.seq),
            (po_store.SPRINT_SESSION, fingerprint, session.session_id, None),
        )
        with self.assertRaises(po_store.RequestConflict):
            self.runner.create_session_request("claude", "opus", "resolve-1", "high")
        with self.assertRaises(po_store.RequestConflict):
            self.runner.send_request(session.session_id, "hello", "resolve-1")
        with self.assertRaises(po_store.RequestConflict):
            self.runner.create_session_request(
                "claude",
                "opus",
                "resolve-1",
                "high",
                operation=po_store.SPRINT_SESSION,
                fingerprint=po_store.sprint_session_fingerprint("sprint:8"),
            )
        self.assertEqual(len(self.store.sessions()), 1)

    def test_a_create_request_id_is_bound_to_its_effort(self) -> None:
        session, created = self.runner.create_session_request("claude", "opus", "req-1", "high")
        again, created_again = self.runner.create_session_request("claude", "opus", "req-1", "high")
        self.assertEqual((created, created_again, again.session_id), (True, False, session.session_id))
        with self.assertRaises(po_store.RequestConflict):
            self.runner.create_session_request("claude", "opus", "req-1", "low")
        # An id recorded before efforts existed bound (operation, cli, model): `default` keeps that.
        self.assertEqual(
            po_store.session_fingerprint("claude", "opus"),
            po_store._digest([po_store.SESSION_CREATE, "claude", "opus"]),
        )


class ResolvedModelParsingTests(unittest.TestCase):
    def test_claude_names_the_first_model_usage_key_of_the_last_result(self) -> None:
        first = json.dumps({"type": "result", "result": "a", "modelUsage": {"claude-fable-5-1": {}}})
        relaunched = json.dumps(
            {
                "type": "result",
                "result": "b",
                "modelUsage": {"claude-opus-5-5[1m]": {}, "claude-haiku-4-5": {}},
            }
        )
        self.assertEqual(claude_resolved_model(first), "claude-fable-5-1")
        self.assertEqual(claude_resolved_model(first + "\n" + relaunched + "\n"), "claude-opus-5-5[1m]")
        self.assertIsNone(claude_resolved_model(json.dumps({"type": "result", "result": "c"})))
        self.assertIsNone(claude_resolved_model("not json"))

    def test_codex_reads_the_last_turn_context_of_the_threads_rollout(self) -> None:
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        rollout = home / "sessions" / "2026" / "09" / "14" / "rollout-2026-09-14T09-43-36-thread-1.jsonl"
        rollout.parent.mkdir(parents=True)
        rollout.write_text(
            "\n".join(
                json.dumps(record)
                for record in (
                    {"type": "session_meta", "payload": {"id": "thread-1"}},
                    {"type": "turn_context", "payload": {"model": "gpt-5.6-sol", "effort": "low"}},
                    {"type": "turn_context", "payload": {"model": "gpt-5.6-terra", "effort": "high"}},
                )
            )
            + "\n{truncated",
            encoding="utf-8",
        )
        self.assertEqual(codex_resolved_model(home, "thread-1"), "gpt-5.6-terra")
        self.assertIsNone(codex_resolved_model(home, "thread-2"))
        self.assertIsNone(codex_resolved_model(home, None))


class CodexThreadIdTests(unittest.TestCase):
    def test_the_first_thread_id_in_the_stream_is_taken(self) -> None:
        stream = "not json\n" + json.dumps({"type": "x", "payload": {"thread_id": "abc"}}) + "\n"
        self.assertEqual(codex_thread_id(stream), "abc")
        self.assertIsNone(codex_thread_id('{"type": "turn.completed"}\n'))


if __name__ == "__main__":
    unittest.main()
