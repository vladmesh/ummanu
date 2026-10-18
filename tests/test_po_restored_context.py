"""Restored turns use durable dialogue and one proven replacement native context."""

import copy
import json
import os
import stat
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from tests.po_checkpoint_fixtures import po_records
from tests.po_fake_store import FakeBoard, FakePoStore
from ummanu.board.po_restore import normalized_po_records, prepare_po_restore
from ummanu.board.sql_rows import parse_rfc3339
from ummanu.po.restore_context import RestoreContextError
from ummanu.po.runner import PoRunner, RunnerError
from ummanu.po.store import (
    COMPLETED,
    FAILED,
    INTERRUPTED,
    RUNNING,
    FeedEntry,
    PoRequest,
    PoStoreError,
    Session,
    Turn,
)


def imported(row, kind):
    return kind(**{key: parse_rfc3339(value) if key.endswith("_at") and value is not None else value
                   for key, value in row.items()})


class FakeProcess:
    pid = 999999999

    def __init__(self, code=0, hold=None):
        self.code = code
        self.hold = hold

    def wait(self, timeout=None):
        if self.hold is not None and not self.hold.wait(timeout or 2):
            raise RunnerError("fake supervisor lost")
        return self.code


class RestoredContextTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.calls = []
        self.code = 0
        self.output = None
        self.stderr = ""
        self.hold = None

    def setup_provider(self, cli="claude", feed_count=10, prepare=True):
        self.data = self.root / cli
        self.data.mkdir()
        (self.data / "po").mkdir()
        self.records = po_records(feed_count=feed_count)
        self.records["po_sessions"][-1]["cli"] = cli
        self.plan = prepare_po_restore(self.records, self.data,
                                       interrupted_at=datetime(2026, 10, 1, tzinfo=UTC))
        normalized = normalized_po_records(self.records, self.plan)
        self.board = FakeBoard()
        self.board.sessions = {row["session_id"]: imported(row, Session) for row in normalized["po_sessions"]}
        self.board.turns = {(row["session_id"], row["seq"]): imported(row, Turn)
                            for row in normalized["po_turns"]}
        self.board.feed = [imported(row, FeedEntry) for row in normalized["po_feed"]]
        self.board.requests = {row["request_id"]: imported(row, PoRequest) for row in normalized["po_requests"]}
        self.store = FakePoStore(self.board)
        self.path = self.data / ".po-restore" / "contexts.json"
        self.runner = self.new_runner()
        if prepare:
            self.runner.prepare_restored_context(self.records, self.plan)

    def new_runner(self):
        runner = PoRunner(FakePoStore(self.board), self.data, head_specs={}, env={}, turn_launcher=self.launch)
        self.enterContext(mock.patch.object(runner, "codex_home", return_value=self.root))
        return runner

    def launch(self, session, seq, argv, files, environment, spec):
        prompt = files.prompt.read_text()
        transcripts = {path: path.read_text() for path in (self.data / "po" / ".restored-context").glob("*.txt")}
        self.calls.append({"cli": session.cli, "seq": seq, "argv": argv, "prompt": prompt,
                           "transcripts": transcripts, "environment": environment})
        output = self.output
        if output is None:
            output = ({"type": "result", "result": "fresh answer", "session_id": session.cli_session_id}
                      if session.cli == "claude" else {"type": "thread.started", "thread_id": "new-thread"})
        files.stdout.write_text(json.dumps(output) + "\n")
        files.stderr.write_text(self.stderr)
        files.last_message.write_text("fresh answer")
        return FakeProcess(self.code, self.hold)

    def send(self, runner=None, text="current-owner-input", request="new-request"):
        runner = runner or self.runner
        turn, created = runner.send_request("open", text, request)
        return runner.wait("open", turn.seq, 2), created

    def assert_no_new_launch(self, runner=None, text="next", request="next-request"):
        count = len(self.calls)
        with self.assertRaises((RestoreContextError, RunnerError)):
            self.send(runner, text, request)
        self.assertEqual(len(self.calls), count)

    def test_both_providers_seed_full_history_once_then_resume_after_restart_and_replay(self):
        for cli in ("claude", "codex"):
            with self.subTest(cli=cli):
                self.setup_provider(cli, feed_count=80)
                captured = copy.deepcopy(self.board.turns)
                source_feed = copy.deepcopy(self.board.feed)
                self.assertEqual(self.runner.recover(rerun=True), [])
                replay, created = self.runner.send_request("open", self.records["po_feed"][0]["text"], "send")
                self.assertFalse(created)
                self.assertEqual(replay.seq, 1)
                before = len(self.calls)
                first, created = self.send()
                self.assertTrue(created)
                self.assertEqual(first.state, COMPLETED, first.reason)
                call = self.calls[before]
                self.assertEqual(call["prompt"].count("current-owner-input"), 1)
                self.assertIn("unknown completion and external effects", call["prompt"])
                self.assertIn("--- current input ---", call["prompt"])
                self.assertLess(len(call["prompt"].encode()), 25_000)
                [(path, transcript)] = call["transcripts"].items()
                self.assertIn(str(path), call["prompt"])
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
                dialogue = [json.loads(line) for line in transcript.splitlines() if line.startswith('{"entry_id"')]
                self.assertEqual([row["text"] for row in dialogue], [row.text for row in source_feed])
                self.assertEqual([row["entry_id"] for row in dialogue], [row.entry_id for row in source_feed])
                self.assertIn('"state": "interrupted"', transcript)
                self.assertIn("no automatic retry", transcript)
                argv = call["argv"]
                if cli == "claude":
                    self.assertEqual(argv[-2:], ["--session-id", dict(self.plan.native_bindings)["open"]])
                else:
                    self.assertEqual(argv[1], "exec")
                    self.assertIn("-C", argv)
                    self.assertEqual(self.store.session("open").cli_session_id, "new-thread")
                restart = self.new_runner()
                saved = self.path.read_bytes()
                with mock.patch("uuid.uuid4", side_effect=AssertionError("retry generated native id")):
                    restart.prepare_restored_context(self.records, self.plan)
                self.assertEqual(self.path.read_bytes(), saved)
                same, created = self.send(restart)
                self.assertFalse(created)
                self.assertEqual(same, first)
                self.assertEqual(len(self.calls), before + 1)
                second, _ = self.send(restart, "follow-up", "second-request")
                self.assertEqual(second.state, COMPLETED)
                self.assertEqual(self.calls[-1]["prompt"], "follow-up")
                if cli == "claude":
                    self.assertEqual(self.calls[-1]["argv"][-2:], ["--resume", dict(self.plan.native_bindings)["open"]])
                else:
                    self.assertEqual(self.calls[-1]["argv"][1:3], ["exec", "resume"])
                    self.assertEqual(self.calls[-1]["argv"][-2:], ["new-thread", "-"])
                for key, turn in captured.items():
                    self.assertEqual(self.board.turns[key], turn)
                self.assertEqual(self.board.feed[:len(source_feed)], source_feed)
                self.assertEqual(call["environment"]["UMMANU_PO_REQUEST"], "new-request")

    def test_small_history_is_inline_and_preparation_is_idempotent_without_launch(self):
        self.setup_provider(feed_count=2)
        before = self.path.stat()
        saved = self.path.read_bytes()
        with mock.patch("uuid.uuid4", side_effect=AssertionError("generated native id")):
            self.runner.prepare_restored_context(self.records, self.plan)
        self.assertEqual(self.calls, [])
        self.assertEqual((self.path.stat().st_ino, self.path.stat().st_mtime_ns), (before.st_ino, before.st_mtime_ns))
        self.assertEqual(self.path.read_bytes(), saved)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        turn, _ = self.send()
        self.assertEqual(turn.state, COMPLETED)
        self.assertIn(json.dumps(self.records["po_feed"][0]["text"], ensure_ascii=False), self.calls[0]["prompt"])

    def test_missing_preparation_refuses_imported_session_before_spawn(self):
        self.setup_provider(prepare=False)
        self.assert_no_new_launch()
        self.assertEqual(self.store.turn("open", 5).state, FAILED)

    def test_foreign_source_target_plan_binding_and_live_import_are_refused(self):
        self.setup_provider(prepare=False)
        changed = copy.deepcopy(self.records)
        changed["po_feed"][0]["text"] += "different"
        for records, plan in (
            (changed, self.plan),
            (self.records, replace(self.plan, data_dir=str(self.root))),
            (self.records, replace(self.plan, native_bindings=(*self.plan.native_bindings[:-1],
                                  ("open", "11111111-1111-4111-8111-111111111111")))),
        ):
            with self.subTest(source=records is changed), self.assertRaises((ValueError, RestoreContextError)):
                self.runner.prepare_restored_context(records, plan)
            self.assertFalse(self.path.exists())
        live = self.board.sessions["open"]
        for alteration in ({"cli_session_id": "foreign"}, {"cwd": "/foreign"}, {"title": "not imported"}):
            self.board.sessions["open"] = replace(live, **alteration)
            with self.assertRaises(RestoreContextError):
                self.runner.prepare_restored_context(self.records, self.plan)
            self.assertFalse(self.path.exists())
        self.board.sessions["open"] = live
        request = self.board.requests["send"]
        self.board.requests["send"] = replace(request, fingerprint="0" * 64)
        with self.assertRaises(RestoreContextError):
            self.runner.prepare_restored_context(self.records, self.plan)
        self.board.requests["send"] = request
        self.board.feed[0] = replace(self.board.feed[0], text="foreign history")
        with self.assertRaises(RestoreContextError):
            self.runner.prepare_restored_context(self.records, self.plan)
        self.assertEqual(self.calls, [])

    def test_corrupt_context_is_never_overwritten_or_treated_as_normal(self):
        self.setup_provider()
        saved = self.path.read_bytes()
        valid = json.loads(saved)
        invalid = [b"null", b"[]", b"{", b"\xff", b'{"version":1,"version":1}',
                   json.dumps({**valid, "version": True}).encode(),
                   json.dumps({**valid, "plan_sha256": "foreign"}).encode()]
        for field, value in (("boundary", True), ("attempt", 5), ("phase", "established"),
                             ("native_id", "foreign"), ("history_sha256", "z" * 64)):
            changed = copy.deepcopy(valid)
            changed["sessions"]["open"][field] = value
            invalid.append(json.dumps(changed).encode())
        for index, payload in enumerate(invalid):
            self.path.write_bytes(payload)
            with self.subTest(index=index):
                with self.assertRaises(RestoreContextError):
                    self.runner.prepare_restored_context(self.records, self.plan)
                self.assert_no_new_launch(text=f"input-{index}", request=f"request-{index}")
                self.assertEqual(self.path.read_bytes(), payload)

    def test_required_history_read_failure_and_changed_history_prevent_launch(self):
        self.setup_provider()
        with (
            mock.patch.object(self.runner.store, "feed", side_effect=PoStoreError("history unavailable")),
            self.assertRaisesRegex(RestoreContextError, "PoStoreError"),
        ):
            self.send()
        self.assertEqual(self.calls, [])
        self.board.feed[0] = replace(self.board.feed[0], text="changed source")
        self.assert_no_new_launch(text="retry", request="retry")

    def test_transcript_read_error_and_unsafe_paths_prevent_launch(self):
        self.setup_provider()
        original = os.open

        def denied(path, *args, **kwargs):
            if str(path).endswith(".txt"):
                raise PermissionError("transcript unavailable")
            return original(path, *args, **kwargs)

        directory = self.data / "po" / ".restored-context"
        directory.mkdir(mode=0o700)
        write = Path.write_text

        def prompt_failure(path, *args, **kwargs):
            if path.suffix == ".prompt":
                raise OSError("prompt unavailable")
            return write(path, *args, **kwargs)

        with mock.patch.object(Path, "write_text", prompt_failure), self.assertRaises(OSError):
            self.send()
        self.assertEqual(self.calls, [])
        transcript = next(directory.glob("*.txt"))
        next_path = transcript.with_name(transcript.name.replace("-5.txt", "-6.txt"))
        next_path.write_bytes(transcript.read_bytes())
        next_path.chmod(0o600)
        with mock.patch("os.open", side_effect=denied):
            self.assert_no_new_launch(text="retry", request="retry")
        directory.chmod(0o755)
        self.assert_no_new_launch(text="unsafe", request="unsafe")

    def test_failed_or_missing_native_evidence_blocks_reseed_and_provider_fallback(self):
        for cli in ("claude", "codex"):
            with self.subTest(cli=cli):
                self.setup_provider(cli)
                self.output = {"type": "result", "result": "answer without native evidence"}
                self.runner.fallback_choice = mock.Mock(return_value=("codex", "other", "high"))
                first, _ = self.send()
                self.assertEqual(first.state, INTERRUPTED)
                self.assertIn("unconfirmed", first.reason)
                with self.assertRaisesRegex(RestoreContextError, "unconfirmed"):
                    self.new_runner().prepare_restored_context(self.records, self.plan)
                self.assert_no_new_launch(self.new_runner())
                self.runner.fallback_choice.assert_not_called()
                self.output = None

    def test_session_collision_and_provider_refusal_never_relaunch_restored_turn(self):
        self.setup_provider()
        self.code = 1
        self.stderr = "session is already in use; rate limit exceeded"
        self.output = {"type": "result", "is_error": True, "result": "rate limit exceeded"}
        self.runner.fallback_choice = mock.Mock(return_value=("codex", "other", "high"))
        first, _ = self.send()
        self.assertEqual(first.state, FAILED)
        self.assertEqual(len(self.calls), 1)
        self.runner.fallback_choice.assert_not_called()
        self.assertEqual(self.store.session("open").cli, "claude")
        self.assert_no_new_launch(self.new_runner())

    def test_stop_during_establishment_blocks_even_with_native_output(self):
        self.setup_provider("codex")
        self.hold = threading.Event()
        self.addCleanup(self.hold.set)
        turn, _ = self.runner.send_request("open", "stop this", "stop-request")
        finish = self.runner.store.finish_turn

        def stop_then_release(*args, **kwargs):
            result = finish(*args, **kwargs)
            self.hold.set()
            return result

        with mock.patch.object(self.runner.store, "finish_turn", side_effect=stop_then_release):
            stopped = self.runner.stop("open")
        self.runner.wait("open", turn.seq, 2)
        self.assertIsNotNone(stopped)
        self.assertEqual(self.store.turn("open", turn.seq).state, INTERRUPTED)
        self.assert_no_new_launch(self.new_runner())

    def test_restart_with_unsettled_establishment_interrupts_without_rerun(self):
        self.setup_provider()
        with (
            mock.patch.object(self.runner.store, "complete_turn", side_effect=PoStoreError("store lost")),
            mock.patch.object(self.runner.store, "finish_turn", side_effect=PoStoreError("store lost")),
        ):
            first, _ = self.send()
        self.assertEqual(first.state, RUNNING)
        restart = self.new_runner()
        [recovered] = restart.recover(rerun=True)
        self.assertEqual(recovered.state, INTERRUPTED)
        self.assertIn("no automatic retry", recovered.reason)
        self.assertEqual(len(self.calls), 1)
        replay, created = self.send(restart)
        self.assertFalse(created)
        self.assertEqual(replay.state, INTERRUPTED)
        self.assert_no_new_launch(restart)

    def test_completion_without_context_publication_stays_blocked_after_restart(self):
        self.setup_provider()
        from ummanu._fsutil import write_bytes_atomic

        def interrupted(path, payload, **kwargs):
            if path == self.path and b'"phase": "established"' in payload:
                raise RuntimeError("publication interrupted")
            return write_bytes_atomic(path, payload, **kwargs)

        with mock.patch("ummanu.po.restore_context.write_bytes_atomic", side_effect=interrupted):
            first, _ = self.send()
        self.assertEqual(first.state, COMPLETED)
        self.assert_no_new_launch(self.new_runner())

    def test_restart_after_establishment_interrupts_input_and_keeps_confirmed_context(self):
        self.setup_provider("codex")
        first, _ = self.send()
        self.assertEqual(first.state, COMPLETED)
        with (
            mock.patch.object(self.runner.store, "complete_turn", side_effect=PoStoreError("store lost")),
            mock.patch.object(self.runner.store, "finish_turn", side_effect=PoStoreError("store lost")),
        ):
            second, _ = self.send(text="uncertain follow-up", request="second-request")
        self.assertEqual(second.state, RUNNING)
        restart = self.new_runner()
        [recovered] = restart.recover(rerun=True)
        self.assertEqual(recovered.state, INTERRUPTED)
        self.assertIn("completion and external effects are unknown", recovered.reason)
        count = len(self.calls)
        replay, created = self.send(restart, text="uncertain follow-up", request="second-request")
        self.assertFalse(created)
        self.assertEqual(replay.state, INTERRUPTED)
        self.assertEqual(len(self.calls), count)
        third, _ = self.send(restart, text="new decision", request="third-request")
        self.assertEqual(third.state, COMPLETED)
        self.assertEqual(self.calls[-1]["argv"][1:3], ["exec", "resume"])
        self.assertEqual(self.calls[-1]["argv"][-2:], ["new-thread", "-"])
        self.assertEqual(self.calls[-1]["prompt"], "new decision")

    def test_launch_marker_publication_failure_cannot_spawn_or_reseed(self):
        self.setup_provider()
        original = os.replace

        def interrupted(source, destination):
            original(source, destination)
            if destination == self.path:
                raise OSError("rename acknowledgement lost")

        with mock.patch("os.replace", side_effect=interrupted):
            self.assert_no_new_launch()
        self.assert_no_new_launch(self.new_runner(), text="retry", request="retry")

    def test_preparation_publication_interruption_reloads_without_replacement(self):
        self.setup_provider(prepare=False)
        original = os.replace

        def interrupted(source, destination):
            original(source, destination)
            if destination == self.path:
                raise OSError("rename acknowledgement lost")

        with mock.patch("os.replace", side_effect=interrupted), self.assertRaises(RestoreContextError):
            self.runner.prepare_restored_context(self.records, self.plan)
        before = self.path.read_bytes()
        with mock.patch("uuid.uuid4", side_effect=AssertionError("regenerated binding")):
            self.new_runner().prepare_restored_context(self.records, self.plan)
        self.assertEqual(self.path.read_bytes(), before)
        first, _ = self.send()
        self.assertEqual(first.state, COMPLETED)

    def test_captured_turn_with_running_state_cannot_be_recovered_or_launched(self):
        self.setup_provider()
        captured = replace(self.board.turns[("open", 4)], state=RUNNING, finished_at=None)
        self.board.turns[("open", 4)] = captured
        self.assertEqual(self.new_runner().recover(rerun=True), [])
        self.assertEqual(self.board.turns[("open", 4)], captured)
        self.assertEqual(self.calls, [])

    def test_changed_live_native_binding_refuses_even_after_establishment(self):
        self.setup_provider("codex")
        first, _ = self.send()
        self.assertEqual(first.state, COMPLETED)
        self.board.sessions["open"] = replace(self.board.sessions["open"], cli_session_id="foreign-thread")
        self.assert_no_new_launch(self.new_runner())

    def test_established_marker_requires_a_completed_establishment_turn(self):
        self.setup_provider()
        first, _ = self.send()
        self.assertEqual(first.state, COMPLETED)
        self.board.turns[("open", first.seq)] = replace(first, state=INTERRUPTED, reason="uncertain")
        restart = self.new_runner()
        with self.assertRaises(RestoreContextError):
            restart.prepare_restored_context(self.records, self.plan)
        self.assert_no_new_launch(restart)

    def test_missing_context_after_completion_cannot_regenerate_a_prepared_boundary(self):
        self.setup_provider()
        first, _ = self.send()
        self.assertEqual(first.state, COMPLETED)
        self.path.unlink()
        restart = self.new_runner()
        with self.assertRaises(RestoreContextError):
            restart.prepare_restored_context(self.records, self.plan)
        self.assert_no_new_launch(restart)

    def test_context_symlink_hardlink_and_permissions_refuse_without_replacement(self):
        self.setup_provider()
        before = self.path.read_bytes()
        retained = self.data / "retained"
        self.path.rename(retained)
        self.path.symlink_to(retained)
        with self.assertRaises(RestoreContextError):
            self.runner.prepare_restored_context(self.records, self.plan)
        self.assertTrue(self.path.is_symlink())
        self.path.unlink()
        os.link(retained, self.path)
        with self.assertRaises(RestoreContextError):
            self.runner.prepare_restored_context(self.records, self.plan)
        self.path.unlink()
        retained.rename(self.path)
        for path, unsafe, safe in ((self.path, 0o644, 0o600), (self.path.parent, 0o755, 0o700)):
            path.chmod(unsafe)
            with self.assertRaises(RestoreContextError):
                self.runner.prepare_restored_context(self.records, self.plan)
            path.chmod(safe)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.calls, [])

    def test_non_restored_sessions_keep_completed_history_resume_heuristic(self):
        self.setup_provider()
        session = self.runner.create_session("claude", "model", "high")
        first = self.runner.send(session.session_id, "ordinary first")
        self.assertEqual(self.runner.wait(session.session_id, first.seq, 2).state, COMPLETED)
        restart = self.new_runner()
        second = restart.send(session.session_id, "ordinary second")
        self.assertEqual(restart.wait(session.session_id, second.seq, 2).state, COMPLETED)
        self.assertEqual(self.calls[-1]["argv"][-2:], ["--resume", session.cli_session_id])
        self.assertEqual(self.calls[-1]["prompt"], "ordinary second")
