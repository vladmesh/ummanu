"""DEAD-21's state cut: current typed ownership and immutable historical sidecars.

The backend and preflight are mocked; these cases launch no process or PTY.
Real supervisor, attach and drain contracts remain in runtime-component CI.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ummanu.automations.runtime import dispatch
from ummanu.runtime.head import HeadRun
from ummanu.runtime.head.runtime import HEAD_BUSY, HEAD_OK, HeadReceipt
from ummanu.runtime.state import AgentState


class AutomationStateRetirementTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = AgentState("steward", Path(directory.name) / "steward")
        self.cmd = dispatch.DispatchCommand(
            "/steward", "fixture-command", "resolved", "report-new",
            head_profile={"adapter": "claude", "runtime": "local-pty", "resource": "acct"},
        )

    def _bring_up(self, status: str = HEAD_OK) -> tuple[mock.Mock, list[str]]:
        runtime = mock.Mock()
        order: list[str] = []

        def start(spec, workspace, task_ref, **kwargs):
            run = HeadRun(
                kwargs["run_id"], spec, workspace, task_ref, role=kwargs["role"], handle="current-handle"
            )
            receipt = HeadReceipt(status=status, run=run, reason="fixture")
            runtime.start.return_value = receipt
            return receipt

        runtime.start.side_effect = start
        reports = mock.Mock()

        def taken(cmd, handle):
            run = HeadRun.from_json(self.state.load_head_run())
            self.assertEqual(run.handle, handle)
            self.assertEqual(run.spec.profile_id, "resolved")
            order.append("taken")

        reports.taken.side_effect = taken
        save = self.state.save_head_run
        log = self.state.log_run

        def publish(record):
            save(record)
            order.append("published")

        def logged(*args, **kwargs):
            log(*args, **kwargs)
            order.append("logged")

        historical_paths = {self.state.dir / name for name in ("head_profile.json", "terminal_generation.json")}
        read_bytes, read_text = Path.read_bytes, Path.read_text

        def guarded_read(method):
            def read(path, *args, **kwargs):
                self.assertNotIn(path, historical_paths, "a tick read a retired sidecar")
                return method(path, *args, **kwargs)
            return read

        with (
            mock.patch.object(dispatch, "_ensure_head_ready"),
            mock.patch.object(dispatch, "_local_pty_runtime", return_value=runtime),
            mock.patch.object(dispatch, "_retire_idle_head"),
            mock.patch.object(dispatch, "_memory_bound_launch", side_effect=lambda agent, run, cmd: cmd),
            mock.patch.object(dispatch, "_memory_heartbeat", side_effect=lambda run, cmd: cmd),
            mock.patch.object(dispatch, "new_run_id", return_value="fixture-run"),
            mock.patch.object(dispatch.time, "time", return_value=123.0),
            mock.patch.object(self.state, "save_head_run", side_effect=publish),
            mock.patch.object(self.state, "log_run", side_effect=logged),
            mock.patch.object(Path, "read_bytes", guarded_read(read_bytes)),
            mock.patch.object(Path, "read_text", guarded_read(read_text)),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                dispatch._supervised_bring_up(
                    "steward", "/fixture/workspace", self.state, "tick", self.cmd, reports=reports
                ), 0,
            )
        return runtime, order

    def test_fresh_start_publishes_the_resolved_typed_run_before_report_and_log(self) -> None:
        runtime, order = self._bring_up()
        run = HeadRun.from_json(self.state.load_head_run())
        self.assertEqual(run, runtime.start.return_value.run)
        self.assertEqual(order, ["published", "taken", "logged"])
        self.assertEqual(run.spec.profile_id, "resolved")
        self.assertEqual(run.spec.runtime, "local-pty")
        self.assertEqual(run.spec.resource, "acct")
        self.assertEqual((run.run_id, run.role, run.workspace), ("fixture-run", "steward", "/fixture/workspace"))
        self.assertEqual(run.task_ref, dispatch.TaskRef.standing("steward"))
        self.assertEqual(runtime.start.call_count, 1)
        for name in ("head_profile.json", "terminal_generation.json", "terminal_handle.json"):
            self.assertFalse((self.state.dir / name).exists(), name)

    def test_stale_sidecars_survive_successful_start_and_busy_reuse_byte_for_byte(self) -> None:
        sentinels = {"head_profile.json": b'{"profile":"stale-routed"}\n', "terminal_generation.json": b"invalid old counter\x00"}
        self.state.ensure_dir()
        for name, content in sentinels.items():
            (self.state.dir / name).write_bytes(content)
        self._bring_up()
        before = self.state.head_run_file.read_bytes()
        runtime, order = self._bring_up(HEAD_BUSY)
        self.assertEqual(order, ["logged"])
        self.assertEqual(runtime.start.call_args.kwargs["run_id"], "fixture-run")
        self.assertEqual(self.state.head_run_file.read_bytes(), before)
        for name, content in sentinels.items():
            self.assertEqual((self.state.dir / name).read_bytes(), content)

    def test_historical_terminal_records_fence_tick_and_orphan_settlement_without_reads(self) -> None:
        for content in (b'{"handle":"old","generation":7}\n', b"", b"{not json", b'{"created_at":1.0}'):
            with self.subTest(content=content):
                self.state.ensure_dir()
                self.state.terminal_handle_file.write_bytes(content)
                self.state.head_run_file.write_bytes(b'{"old":"head ownership"}\n')
                self.state.save_active_report("report-old", "old-handle")
                owned = {path: path.read_bytes() for path in (
                    self.state.terminal_handle_file, self.state.head_run_file, self.state.active_report_file
                )}
                board = mock.Mock()
                reports = dispatch._TickReports("steward", self.state, "tick", report_board=board)
                err = io.StringIO()
                with (
                    mock.patch.object(dispatch, "_pipeline_paused", return_value=False),
                    mock.patch.object(dispatch, "_registry_snapshot", return_value=SimpleNamespace(registry=None)),
                    mock.patch.object(dispatch, "_resolve_launch", return_value=mock.sentinel.resolution),
                    mock.patch.object(reports, "command") as command,
                    mock.patch.object(dispatch, "_supervised_bring_up") as bring_up,
                    mock.patch.object(Path, "read_text", side_effect=AssertionError("fence parsed state")),
                    contextlib.redirect_stderr(err),
                ):
                    self.assertEqual(dispatch._tick("steward", None, "/fixture", self.state, "tick", reports), 1)
                    reports._close_an_orphan()
                command.assert_not_called()
                bring_up.assert_not_called()
                self.assertTrue(reports.hold_still)
                # Exercise the independent orphan fence without hold_still or a current run.
                self.state.head_run_file.unlink()
                dispatch._TickReports("steward", self.state, "tick", report_board=board)._close_an_orphan()
                self.state.head_run_file.write_bytes(owned[self.state.head_run_file])
                board.create_report.assert_not_called()
                board.move_report.assert_not_called()
                self.assertIn("terminal_handle.json", err.getvalue())
                event = json.loads((self.state.dir / "runs.jsonl").read_text().splitlines()[-1])
                self.assertEqual((event["action"], event["result"]), (dispatch.SUPERVISED_OWNER_CONFLICT, "error"))
                self.assertIn("terminal_handle.json", event["error"])
                for path, before in owned.items():
                    self.assertEqual(path.read_bytes(), before)
