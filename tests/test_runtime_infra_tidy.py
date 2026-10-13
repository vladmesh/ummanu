"""ummanu-241: the runtime, backup and infra defects of the audit (BUG-24) and the helpers their
duplicates now share, each pinned by the scenario that broke it."""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ummanu import _fsutil, checkpoint, state_repo, task_restore
from ummanu.automations.runtime import dispatch
from ummanu.dispatch.host import HostError, _body_file_path, _private_body_dir
from ummanu.infra import snapshot_tree
from ummanu.memory import access
from ummanu.runtime.head.local_pty.journal import RUN_STOPPING, TURN_FINISHED
from ummanu.runtime.head.local_pty.supervisor import TURN_QUIET_SECONDS, Supervisor

OID = "a" * 40


class _Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class SupervisorClockTests(unittest.TestCase):
    """The supervisor times quiet windows and its stop grace on the monotonic clock: a wall-clock
    step (NTP, a suspended VM) neither closes a turn nor kills a head inside its grace."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.wall = _Clock(1_000_000.0)
        self.mono = _Clock(500.0)
        for name, clock in (("time.time", self.wall), ("time.monotonic", self.mono)):
            patcher = mock.patch(name, side_effect=clock)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.supervisor = Supervisor(
            run_dir=Path(directory.name), run_id="run-1", role="worker", task="card:ummanu-241", command="true"
        )
        self.addCleanup(self.supervisor._selector.close)
        self.kinds: list[str] = []
        self.supervisor._journal = SimpleNamespace(  # type: ignore[assignment]
            append=lambda kind, **fields: self.kinds.append(kind) or {"kind": kind, **fields}
        )

    def _open_turn_with_output(self) -> None:
        self.supervisor._turn_open = True
        self.supervisor._record_output(b"working\r\n")

    def test_a_wall_clock_jump_does_not_close_an_open_turn(self) -> None:
        self._open_turn_with_output()

        self.wall.now += 3600.0
        self.supervisor._tick()

        self.assertTrue(self.supervisor._turn_open)
        self.assertNotIn(TURN_FINISHED, self.kinds)

    def test_monotonic_quiet_still_closes_the_turn(self) -> None:
        self._open_turn_with_output()

        self.mono.now += TURN_QUIET_SECONDS + 0.1
        self.supervisor._tick()

        self.assertFalse(self.supervisor._turn_open)
        self.assertIn(TURN_FINISHED, self.kinds)

    def test_a_wall_clock_jump_does_not_cut_the_stop_grace_short(self) -> None:
        signals: list[int] = []
        self.supervisor._signal_head = signals.append  # type: ignore[method-assign]
        self.supervisor._begin_stop("test", signal.SIGTERM)
        self.assertIn(RUN_STOPPING, self.kinds)

        self.wall.now += 3600.0
        self.supervisor._tick()
        self.assertEqual(signals, [signal.SIGTERM])

        self.mono.now += 3600.0
        self.supervisor._tick()
        self.assertEqual(signals, [signal.SIGTERM, signal.SIGKILL])


class BodyFileDirectoryTests(unittest.TestCase):
    """Report and verdict bodies leave the shared `/tmp`, where any account could plant the
    predictable `ummanu-<kind>-<ref>-<round>.md` first, for a directory only this user enters."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        environment = mock.patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("UMMANU_DISPATCHER_BODY_DIR", None)

    def test_the_default_body_file_is_under_the_data_dir_not_tmp(self) -> None:
        path = _body_file_path(self.root, "report", "ummanu-241", 1)

        self.assertEqual(path, f"{self.root}/artifacts/bodies/ummanu-report-ummanu-241-1.md")

    def test_the_override_still_names_the_directory(self) -> None:
        os.environ["UMMANU_DISPATCHER_BODY_DIR"] = str(self.root / "bodies") + "/"

        path = _body_file_path(Path("/unused"), "verdict", "ummanu-241", 3)

        self.assertEqual(path, f"{self.root}/bodies/ummanu-verdict-ummanu-241-3.md")

    def test_a_new_directory_is_created_private(self) -> None:
        bodies = self.root / "artifacts" / "bodies"

        _private_body_dir(bodies)

        self.assertEqual(stat.S_IMODE(bodies.stat().st_mode), 0o700)

    def test_an_existing_open_directory_this_user_owns_is_tightened(self) -> None:
        bodies = self.root / "bodies"
        bodies.mkdir(mode=0o777)
        bodies.chmod(0o777)

        _private_body_dir(bodies)

        self.assertEqual(stat.S_IMODE(bodies.stat().st_mode), 0o700)

    def test_a_directory_another_user_owns_is_refused(self) -> None:
        bodies = self.root / "bodies"
        bodies.mkdir(mode=0o755)

        with (
            mock.patch("os.geteuid", return_value=os.geteuid() + 1),
            self.assertRaisesRegex(HostError, "not a directory this user owns"),
        ):
            _private_body_dir(bodies)
        self.assertEqual(stat.S_IMODE(bodies.stat().st_mode), 0o755)

    def test_a_symlink_in_place_of_the_directory_is_refused(self) -> None:
        target = self.root / "elsewhere"
        target.mkdir()
        (self.root / "bodies").symlink_to(target, target_is_directory=True)

        with self.assertRaisesRegex(HostError, "not a directory this user owns"):
            _private_body_dir(self.root / "bodies")


class GrantWriteDescriptorTests(unittest.TestCase):
    """`memory/access._write_json` closed its descriptor twice when a write failed after `fdopen`:
    once through the handle, then `os.close(fd)` on a number another thread may own by then."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.closed: list[int] = []
        real_close = os.close

        def close(descriptor: int) -> None:
            self.closed.append(descriptor)
            real_close(descriptor)

        patcher = mock.patch("os.close", side_effect=close)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_failed_write_after_fdopen_leaves_the_close_to_the_handle(self) -> None:
        with (
            mock.patch("json.dump", side_effect=ValueError("not serialisable")),
            self.assertRaisesRegex(ValueError, "not serialisable"),
        ):
            access._write_json(self.root / "grant.json", {"grant_id": "g"})

        self.assertEqual(self.closed, [])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_a_failed_fdopen_closes_the_descriptor_exactly_once(self) -> None:
        with (
            mock.patch("os.fdopen", side_effect=OSError("no handle")),
            self.assertRaisesRegex(OSError, "no handle"),
        ):
            access._write_json(self.root / "grant.json", {"grant_id": "g"})

        self.assertEqual(len(self.closed), 1)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_a_successful_write_is_private_and_complete(self) -> None:
        target = self.root / "grant.json"

        access._write_json(target, {"grant_id": "g"})

        self.assertEqual(target.read_text(encoding="utf-8"), '{"grant_id":"g"}')
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)


def _batch(stdout: bytes, *, returncode: int = 0, stderr: bytes = b"") -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(["git", "cat-file", "--batch"], returncode, stdout, stderr)


class SnapshotBlobReaderTests(unittest.TestCase):
    """`checkpoint._SnapshotAudit._blobs` and `snapshot_tree._blobs` are one reader. The checkpoint
    copy did `payload.index(b"\\n")` and raised `ValueError` on output cut short."""

    TRUNCATED = (
        ("no output", b""),
        ("header without its newline", f"{OID} blob 5".encode()),
        ("body shorter than announced", f"{OID} blob 10\nabc".encode()),
    )

    def _audit(self) -> checkpoint._SnapshotAudit:
        return checkpoint._SnapshotAudit(checkpoint._PublishedRepo(Path("/nonexistent/snapshot.git"), True))

    def test_truncated_output_is_a_typed_refusal_for_both_callers(self) -> None:
        readers = (
            ("checkpoint", lambda: self._audit()._blobs([OID]), state_repo.StateRepoError),
            ("snapshot_tree", lambda: snapshot_tree._blobs(Path("/nonexistent/snapshot.git"), [OID]),
             snapshot_tree.SnapshotTreeError),
        )
        for name, read, error in readers:
            for case, stdout in self.TRUNCATED:
                with (
                    self.subTest(reader=name, case=case),
                    mock.patch("ummanu.state_repo.run_git_bytes", return_value=_batch(stdout)),
                    self.assertRaisesRegex(error, "snapshot blobs failed"),
                ):
                    read()

    def test_a_complete_batch_returns_each_blob_byte_for_byte(self) -> None:
        other = "b" * 40
        stdout = f"{OID} blob 3\nab\n\n{other} blob 0\n\n".encode()
        with mock.patch("ummanu.state_repo.run_git_bytes", return_value=_batch(stdout)) as run:
            blobs = self._audit()._blobs([OID, other])

        self.assertEqual(blobs, {OID: b"ab\n", other: b""})
        self.assertEqual(run.call_args.args[1], ["--git-dir", "/nonexistent/snapshot.git", "cat-file", "--batch"])

    def test_a_missing_object_and_a_failed_git_keep_their_texts(self) -> None:
        with (
            mock.patch("ummanu.state_repo.run_git_bytes", return_value=_batch(f"{OID} missing\n".encode())),
            self.assertRaisesRegex(state_repo.StateRepoError, f"^snapshot blobs failed: {OID} is not a blob$"),
        ):
            self._audit()._blobs([OID])
        failed = _batch(b"", returncode=128, stderr=b"warning: x\nfatal: not a git repository\n")
        with (
            mock.patch("ummanu.state_repo.run_git_bytes", return_value=failed),
            self.assertRaisesRegex(
                snapshot_tree.SnapshotTreeError, "^snapshot blobs failed: fatal: not a git repository$"
            ),
        ):
            snapshot_tree._blobs(Path("/nonexistent/snapshot.git"), [OID])


class GitFailureWrapperTests(unittest.TestCase):
    """One last-line rule and one checked runner, parameterised by the caller's error class."""

    @staticmethod
    def _result(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["git"], returncode, stdout, stderr)

    def test_the_detail_is_the_last_stderr_line_else_stdout_else_the_fallback(self) -> None:
        cases = [
            (self._result(1, "out\n", "hint: a\nfatal: b\n"), {}, "fatal: b"),
            (self._result(1, "one\ntwo\n", ""), {}, "two"),
            (self._result(1, "one\n", ""), {"stdout": False}, "git error"),
            (self._result(3), {"fallback": "exited 3"}, "exited 3"),
            (subprocess.CompletedProcess(["git"], 1, b"", b"x\nfatal: bytes\n"), {}, "fatal: bytes"),
        ]
        for result, options, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(state_repo.git_failure_detail(result, **options), expected)

    def test_a_failed_command_raises_the_callers_class_with_the_same_text(self) -> None:
        failed = self._result(128, "", "fatal: bad revision\n")
        for error in (state_repo.StateRepoError, checkpoint.CheckpointBlocked, snapshot_tree.SnapshotTreeError):
            with (
                self.subTest(error=error.__name__),
                mock.patch("ummanu.state_repo.run_git", return_value=failed),
                self.assertRaisesRegex(error, "^snapshot tip failed: fatal: bad revision$"),
            ):
                state_repo.git(Path("/repo"), ["rev-parse", "HEAD"], label="snapshot tip", error=error)

    def test_a_runner_failure_keeps_its_text_under_the_callers_class(self) -> None:
        refusal = state_repo.StateRepoError("snapshot tip failed: timed out")
        with (
            mock.patch("ummanu.state_repo.run_git", side_effect=refusal),
            self.assertRaisesRegex(checkpoint.CheckpointBlocked, "^snapshot tip failed: timed out$"),
        ):
            state_repo.run_git_checked(Path("/repo"), ["rev-parse"], label="snapshot tip", error=checkpoint.CheckpointBlocked)

    def test_a_success_returns_the_whole_result(self) -> None:
        with mock.patch("ummanu.state_repo.run_git", return_value=self._result(0, "abc\n")):
            result = state_repo.run_git_checked(Path("/repo"), ["rev-parse"], label="tip")
        self.assertEqual(result.stdout, "abc\n")


class _Board:
    def __init__(self, refusal: Exception | None = None) -> None:
        self.moves: list[tuple[str, str, str]] = []
        self.refusal = refusal

    def move_report(self, *, reference: str, target: str, reason: str) -> None:
        if self.refusal is not None:
            raise self.refusal
        self.moves.append((reference, target, reason))


class StewardReportMoveTests(unittest.TestCase):
    """The four report movers of `automations/runtime/dispatch` are `_move_report`; these are the
    log lines and card moves each of them wrote before, byte for byte."""

    REF = "ummanu-report-1"

    def setUp(self) -> None:
        self.state = mock.Mock()
        self.state.load_active_report.return_value = {"reference": self.REF}
        self.cmd = dispatch.DispatchCommand("/steward", "", None, self.REF)

    def _logs(self) -> list[dict[str, object]]:
        return [entry.kwargs for entry in self.state.log_run.call_args_list]

    def test_each_settlement_moves_the_card_and_logs_its_action(self) -> None:
        board = _Board()
        ticks = dispatch._TickReports("steward", self.state, "tick", report_board=board)
        failure = RuntimeError("bring-up refused")
        ticks.undispatched(self.cmd, "not dispatched")
        ticks.failed(self.cmd, failure)
        ticks.preflight_failed(self.cmd, RuntimeError("no workspace"))
        ticks._close_an_orphan()

        self.assertEqual(
            board.moves,
            [
                (self.REF, "done", "not dispatched"),
                (
                    self.REF,
                    "done",
                    (
                        "steward dispatch failed before the head accepted the report-card run.\n\n"
                        "failure: bring-up refused"
                    ),
                ),
                (
                    self.REF,
                    "blocked",
                    (
                        "steward dispatch could not prepare the head workspace, so no head was started and "
                        "no sweep ran.\n\nfailure: no workspace"
                    ),
                ),
            ],
        )
        self.assertEqual(
            self._logs(),
            [
                {"action": "dispatch-release", "result": "done", "reference": self.REF},
                {"action": "dispatch-recovery", "result": "done", "reference": self.REF},
                {"action": "dispatch-preflight", "result": "blocked", "reference": self.REF, "error": "no workspace"},
            ],
        )
        self.assertEqual(self.state.clear_active_report.call_count, 3)

    def test_a_standing_report_is_settled_from_the_role_state(self) -> None:
        board = _Board()
        self.state.load_head_run.return_value = None
        self.state.terminal_handle_file.exists.return_value = False

        dispatch._TickReports("steward", self.state, "tick", report_board=board)._close_an_orphan()

        self.assertEqual([move[:2] for move in board.moves], [(self.REF, "done")])
        self.assertEqual(self._logs(), [{"action": "owner-report-release", "result": "done", "reference": self.REF}])

    def test_a_failed_move_is_logged_never_raised(self) -> None:
        ticks = dispatch._TickReports("steward", self.state, "tick", report_board=_Board(OSError("board down")))
        ticks.undispatched(self.cmd, "not dispatched")
        ticks.preflight_failed(self.cmd, RuntimeError("no workspace"))
        dispatch._TickReports("steward", self.state, "tick").failed(self.cmd, RuntimeError("x"))

        self.assertEqual(
            self._logs(),
            [
                {"action": "dispatch-release", "result": "failed", "reference": self.REF, "error": "board down"},
                {"action": "dispatch-preflight", "result": "failed", "reference": self.REF,
                 "error": "no workspace (escalation failed: board down)"},
                {"action": "dispatch-recovery", "result": "failed", "reference": self.REF,
                 "error": "steward report board must be supplied by the composition root"},
            ],
        )

    def test_no_card_means_nothing_is_touched(self) -> None:
        ticks = dispatch._TickReports("steward", self.state, "tick", report_board=_Board())
        ticks.undispatched(dispatch.DispatchCommand("/steward", "", None, None), "not dispatched")
        self.state.load_active_report.return_value = None
        self.state.load_head_run.return_value = None
        self.state.terminal_handle_file.exists.return_value = False
        ticks._close_an_orphan()

        self.state.clear_active_report.assert_not_called()
        self.state.log_run.assert_not_called()


class SharedHelperTests(unittest.TestCase):
    def test_fsync_directory_syncs_the_directory_and_closes_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            synced: list[int] = []
            real_fsync = os.fsync

            def fsync(descriptor: int) -> None:
                synced.append(descriptor)
                self.assertTrue(stat.S_ISDIR(os.fstat(descriptor).st_mode))
                real_fsync(descriptor)

            with mock.patch("os.fsync", side_effect=fsync):
                _fsutil.fsync_directory(Path(directory))

            self.assertEqual(len(synced), 1)
            with self.assertRaises(OSError):
                os.fstat(synced[0])

    def test_one_restored_identity_reader_refuses_with_the_callers_class(self) -> None:
        class Refusal(RuntimeError):
            pass

        self.assertEqual(task_restore.restored_entity_number("task", "task_postgres_7"), 7)
        with self.assertRaisesRegex(Refusal, "restored row carries no usable sprint identity: 'sprint_7x'"):
            task_restore.restored_entity_number("sprint", "sprint_7x", error=Refusal)
        with self.assertRaisesRegex(RuntimeError, "restored row carries no usable task identity"):
            task_restore.restored_entity_number("task", None)


if __name__ == "__main__":
    unittest.main()
