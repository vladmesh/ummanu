"""Independent checkpoint ownership, upgrade readers and packaged cadence without a host."""

from __future__ import annotations

import ast
import contextlib
import fcntl
import inspect
import io
import json
import multiprocessing
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ummanu import checkpoint, cli, data, status
from ummanu._fsutil import try_file_lock, write_json
from ummanu.board.sql_cards import SqlCardClient
from ummanu.checkpoint import CheckpointResult
from ummanu.dispatch import production
from ummanu.host import (
    SHIPPED_PACKAGING_ROOT,
    CollectResult,
    HostInventory,
    SystemdLayout,
    assess_unit_runtime,
    build_doctor_expectations,
    build_plan,
    load_packaged_units,
)
from ummanu.infra import checkpoint_run
from ummanu.tasks import TaskError

NOW = 2_000_000_000.0


def hold_locks(root, channel):
    with contextlib.ExitStack() as stack:
        for name in ("cleanup.lock", "production-tick.lock"):
            handle = stack.enter_context((root / "dispatcher" / name).open("a+"))
            fcntl.flock(handle, fcntl.LOCK_EX)
        channel.send("locked")
        channel.recv()


def concurrent_cut(root, channel):
    def write():
        channel.send("cut")
        channel.recv()
        return CheckpointResult(status="unchanged")

    runtime = SimpleNamespace(data_dir=root, checkpoint=SimpleNamespace(write=write))
    channel.send(checkpoint_run.run_checkpoint(runtime))


class CheckpointRunTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.dispatcher = self.root / "dispatcher"
        self.dispatcher.mkdir()
        self.writer = mock.Mock()
        self.writer.write.return_value = CheckpointResult(status="committed", commit="abc123")
        self.pusher = mock.Mock()
        self.pusher._clock = lambda: NOW
        self.pusher.due.return_value = False
        self.runtime = SimpleNamespace(
            data_dir=self.root, checkpoint=self.writer, checkpoint_push=self.pusher
        )

    def test_tick_bodies_have_no_checkpoint_call_or_phase(self):
        for body in (
            production._production_tick_work,
            production._production_tick_with_snapshot,
            production._frozen_tick_body,
        ):
            source = inspect.getsource(body)
            ast.parse(source)
            self.assertNotIn("_coordinate_checkpoint", source)
            self.assertNotIn('tick_phase("checkpoint")', source)
            self.assertNotIn("_checkpoint_degradation", source)

    def test_cli_owns_state_and_runs_under_foreign_cleanup_and_tick_locks(self):
        parent, child = multiprocessing.get_context("fork").Pipe()
        process = multiprocessing.get_context("fork").Process(target=hold_locks, args=(self.root, child))
        process.start()
        try:
            self.assertTrue(parent.poll(5))
            self.assertEqual(parent.recv(), "locked")
            # These are real locks held by a different process, not mocked contexts.
            for name in ("cleanup.lock", "production-tick.lock"):
                with try_file_lock(self.dispatcher / name) as acquired:
                    self.assertFalse(acquired)
            legacy = {"phase": "production", "checkpoint_push": {"status": "pushed", "attempted_epoch": NOW}}
            write_json(self.dispatcher / "production-state.json", legacy)
            output = io.StringIO()
            with (
                mock.patch("ummanu.dispatch.bootstrap.runtime_from_args", return_value=self.runtime),
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(cli.main(["checkpoint-run", "--instance", str(self.root)]), 0)
            result = json.loads(output.getvalue())
            self.assertEqual(result["checkpoint"]["status"], "committed")
            saved = checkpoint_run.load_checkpoint_state(self.root)
            self.assertEqual(saved["checkpoint"]["commit"], "abc123")
            self.assertEqual(saved["checkpoint_push"], legacy["checkpoint_push"])
            self.assertEqual(json.loads((self.dispatcher / "production-state.json").read_text()), legacy)
            self.writer.write.assert_called_once_with()
            self.assertEqual(
                {path.name for path in self.dispatcher.glob("*.lock")},
                {"checkpoint.lock", "cleanup.lock", "production-tick.lock"},
            )
        finally:
            parent.send("release")
            process.join(5)
            if process.is_alive():
                process.terminate()
                process.join(5)
        self.assertEqual(process.exitcode, 0)

    def test_concurrent_runs_make_one_cut_and_one_skipped_result(self):
        parent, child = multiprocessing.get_context("fork").Pipe()
        process = multiprocessing.get_context("fork").Process(target=concurrent_cut, args=(self.root, child))
        process.start()
        try:
            self.assertTrue(parent.poll(5))
            self.assertEqual(parent.recv(), "cut")
            result = checkpoint_run.run_checkpoint(self.runtime)
            self.assertEqual(result["status"], "skipped")
            self.assertIn("singleton lock", result["reason"])
            self.writer.write.assert_not_called()
            self.assertFalse((self.dispatcher / "checkpoint-state.json").exists())
            parent.send("finish")
            self.assertTrue(parent.poll(5))
            self.assertEqual(parent.recv()["checkpoint"]["status"], "unchanged")
        finally:
            process.join(5)
            if process.is_alive():
                process.terminate()
                process.join(5)
        self.assertEqual(process.exitcode, 0)
        self.assertEqual(checkpoint_run.load_checkpoint_state(self.root)["checkpoint"]["status"], "unchanged")

    def test_legacy_cadence_and_push_window_carry_over_to_first_run(self):
        previous = {
            "checkpoint": {
                "status": "committed",
                "last_success_epoch": NOW - 60,
                "last_success_at": "2033-05-18T03:32:20Z",
            },
            "checkpoint_push": {"status": "pushed", "attempted_epoch": NOW - 60},
        }
        write_json(self.dispatcher / "production-state.json", previous)
        result = checkpoint_run.run_checkpoint(self.runtime)
        self.assertEqual(result["checkpoint"]["status"], "skipped")
        self.assertEqual(result["checkpoint"]["last_success_epoch"], NOW - 60)
        self.pusher.due.assert_called_once_with(previous["checkpoint_push"], now=NOW)
        self.writer.write.assert_not_called()
        self.assertEqual(
            checkpoint_run.load_checkpoint_state(self.root)["checkpoint_push"], previous["checkpoint_push"]
        )

    def test_precadence_legacy_result_requires_fail_closed_first_cut(self):
        write_json(
            self.dispatcher / "production-state.json", {"checkpoint": {"status": "committed", "at": "old"}}
        )
        self.writer.write.return_value = CheckpointResult(status="blocked", reason="audit pending")
        self.pusher.due.return_value = True
        result = checkpoint_run.run_checkpoint(self.runtime)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["checkpoint"]["status"], "blocked")
        self.assertIn("withheld", result["checkpoint_push"]["reason"])
        self.pusher.push.assert_not_called()
        self.writer.write.assert_called_once_with()

    def test_pause_modes_do_not_change_checkpoint_cadence(self):
        for mode in ("freeze", "drain"):
            with self.subTest(mode=mode):
                self.runtime.pause = mock.Mock()
                self.runtime.pause.summary.return_value = {"mode": mode}
                self.pusher._clock = lambda mode=mode: NOW + 300 if mode == "drain" else NOW
                self.assertEqual(
                    checkpoint_run.run_checkpoint(self.runtime)["checkpoint"]["status"], "committed"
                )
                self.runtime.pause.summary.assert_not_called()
        self.assertEqual(self.writer.write.call_count, 2)


class CheckpointReaderTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.root / "dispatcher").mkdir()
        self.report = SimpleNamespace(
            ok=True,
            data_dir=self.root,
            instance_path=self.root / "instance.yaml",
            instance={},
            bindings={},
            name="test",
            projects=[],
        )
        self.enterContext(mock.patch.object(checkpoint.time, "time", return_value=NOW))
        self.enterContext(
            mock.patch.object(checkpoint, "_last_commit", return_value=("new", "2033-05-18T03:33:00Z"))
        )
        self.enterContext(
            mock.patch.object(checkpoint, "_unpushed", return_value=(1, "2033-05-18T03:13:20Z"))
        )
        for name, value in (
            ("build_doctor_expectations", mock.Mock()),
            ("resolve_installed_packaged", None),
            ("_units", []),
            ("_schedules", []),
            ("_head_registry", {"error": "missing"}),
            ("_card_count", 0),
            ("_host_resources", {}),
            ("_memory_status", {}),
            ("store_health", {}),
        ):
            self.enterContext(mock.patch.object(status, name, return_value=value))

    def observe(self):
        runtime = SimpleNamespace(
            data_dir=self.root,
            catalog=SimpleNamespace(instance_dir=self.root),
            production_state=production.ProductionState(self.root),
            pause=mock.Mock(),
            head_health=mock.Mock(),
        )
        return production.production_observe(runtime)["checkpoint"]

    def collect(self):
        return status.collect_status(self.report, offline=True, sprints=False, recovery={"resources": []})[
            "checkpoint"
        ]

    def test_new_and_legacy_records_have_identical_status_doctor_and_findings(self):
        for records in (
            {
                "checkpoint": {
                    "status": "blocked",
                    "reason": "audit pending",
                    "at": "2033-05-18T03:28:20Z",
                    "last_success_epoch": NOW - 1200,
                    "last_success_at": "2033-05-18T03:13:20Z",
                },
                "checkpoint_push": {
                    "status": "failed",
                    "reason": "ssh unavailable",
                    "attempted_epoch": NOW - 1200,
                },
            },
            {
                "checkpoint": {"status": "unchanged", "last_success_epoch": NOW - 60},
                "checkpoint_push": {
                    "status": "diverged",
                    "remote_diverged": True,
                    "reason": "remote diverged",
                },
            },
        ):
            expected = checkpoint.checkpoint_snapshot(
                self.root,
                write_state=records["checkpoint"],
                push_state=records["checkpoint_push"],
                data_dir=self.root,
            )
            rendered = io.StringIO()
            rendered.write("\ncheckpoint freshness: read-only\n")
            for line in checkpoint.render_checkpoint_lines(expected):
                rendered.write(f"  {line}\n")
            outputs = []
            for filename in ("production-state.json", "checkpoint-state.json"):
                with self.subTest(records=records, filename=filename):
                    write_json(self.root / "dispatcher" / filename, records)
                    self.assertEqual(self.collect(), expected)
                    self.assertEqual(self.observe(), expected)
                    text = io.StringIO()
                    with contextlib.redirect_stdout(text):
                        cli.print_checkpoint_status(self.report, findings=[])
                    self.assertEqual(text.getvalue(), rendered.getvalue())
                    outputs.append(
                        (
                            cli.checkpoint_findings(self.report),
                            cli.checkpoint_rpo_findings(self.report),
                            cli.checkpoint_cut_lag_findings(self.report),
                        )
                    )
            self.assertEqual(outputs[0], outputs[1])
            (self.root / "dispatcher" / "checkpoint-state.json").unlink()

    def test_hostile_new_state_never_raises_and_never_revives_legacy_success(self):
        legacy = {
            "checkpoint": {
                "status": "committed",
                "last_success_epoch": NOW - 60,
                "last_success_at": "2033-05-18T03:32:20Z",
            }
        }
        write_json(self.root / "dispatcher" / "production-state.json", legacy)
        path = self.root / "dispatcher" / "checkpoint-state.json"
        for raw in (
            "not JSON",
            "null",
            "[]",
            "42",
            '{"checkpoint": [], "checkpoint_push": "bad"}',
            '{"checkpoint": {"last_success_epoch": Infinity}, "checkpoint_push": {"failures": NaN}}',
            '{"checkpoint": {"last_success_epoch": {}, "status": []}, "checkpoint_push": {"failures": []}}',
        ):
            with self.subTest(raw=raw):
                path.write_text(raw)
                self.assertEqual(self.collect()["last_checkpoint_prepared_epoch"], 0)
                self.observe()
                cli.checkpoint_rpo_findings(self.report)
                cli.checkpoint_cut_lag_findings(self.report)
                cli.checkpoint_findings(self.report)
                with contextlib.redirect_stdout(io.StringIO()):
                    cli.print_checkpoint_status(self.report)
        path.unlink()
        self.assertEqual(self.collect()["last_checkpoint_prepared_epoch"], NOW - 60)
        real_read = Path.read_text

        def unreadable(target, *args, **kwargs):
            if target == path:
                raise PermissionError("unreadable")
            return real_read(target, *args, **kwargs)

        with mock.patch.object(Path, "read_text", unreadable):
            self.assertEqual(checkpoint_run.load_checkpoint_state(self.root), {})
            self.collect()
            self.observe()
            cli.checkpoint_cut_lag_findings(self.report)

    def test_doctor_cut_lag_threshold_is_fifteen_minutes(self):
        for seconds, stale in ((900, False), (901, True), (1200, True)):
            write_json(
                self.root / "dispatcher" / "checkpoint-state.json",
                {"checkpoint": {"last_success_epoch": NOW - seconds}},
            )
            findings = cli.checkpoint_cut_lag_findings(self.report)
            self.assertEqual(bool(findings), stale)
            if stale:
                self.assertEqual(findings[0]["code"], "checkpoint.cut_lag_exceeded")
                self.assertEqual(findings[0]["severity"], "red")


class CheckpointUnitTests(unittest.TestCase):
    def setUp(self):
        self.layout = SystemdLayout(
            Path("/product"), Path("/instance"), Path("/data"), "runtime", Path("/home/runtime")
        )
        self.packaged = load_packaged_units(SHIPPED_PACKAGING_ROOT, "ummanu-", self.layout)
        self.instance = {"host": {"unit_prefix": "ummanu-"}}

    def test_upgrade_and_doctor_include_omitted_checkpoint_component(self):
        plan = build_plan(self.instance, [], packaged=self.packaged)
        names = {resource.name for resource in plan if resource.kind == "unit"}
        self.assertTrue({"ummanu-checkpoint.service", "ummanu-checkpoint.timer"} <= names)
        expected = build_doctor_expectations(self.instance, [], packaged=self.packaged)
        self.assertEqual(expected.unit_runtime["ummanu-checkpoint.service"], (False, False))
        self.assertEqual(expected.unit_runtime["ummanu-checkpoint.timer"], (True, True))
        collected = CollectResult(
            HostInventory(
                units={"ummanu-checkpoint.service"},
                unit_states={"ummanu-checkpoint.service": ("static", "failed")},
            )
        )
        findings = cli.checkpoint_unit_findings(expected, collected)
        self.assertEqual(len(findings), 2)
        self.assertTrue(all(finding["severity"] == "red" for finding in findings))
        self.assertEqual(
            assess_unit_runtime({"ummanu-checkpoint.service": (False, False)}, collected)[0].actual, "failed"
        )
        collected.inventory.units.add("ummanu-checkpoint.timer")
        collected.inventory.unit_states.clear()
        collected.inventory.unit_states.update(
            {
                "ummanu-checkpoint.service": ("static", "inactive"),
                "ummanu-checkpoint.timer": ("enabled", "active"),
            }
        )
        self.assertEqual(cli.checkpoint_unit_findings(expected, collected), [])
        self.instance["host"]["components"] = {"checkpoint": {"enabled": False}}
        self.assertFalse(
            any("checkpoint" in item.name for item in build_plan(self.instance, [], packaged=self.packaged))
        )

    def test_timer_run_budget_and_preserved_window_fit_five_minute_rpo(self):
        units = {unit.name: unit.content.decode() for unit in self.packaged}
        timer = units["ummanu-checkpoint.timer"]
        values = dict(line.split("=", 1) for line in timer.splitlines() if "=" in line)
        interval = int(values["OnUnitActiveSec"])
        expected_run = 50
        self.assertLess(expected_run, interval)
        self.assertLessEqual(interval + expected_run, checkpoint_run.CHECKPOINT_INTERVAL_SECONDS)
        # The coordinator skips until 300s. Pin divisibility as well: 240s alone would make 480s cuts.
        self.assertEqual(checkpoint_run.CHECKPOINT_INTERVAL_SECONDS % interval, 0)
        self.assertLessEqual(int(values["OnBootSec"]) + expected_run, 300)
        self.assertEqual(values["AccuracySec"], "1")
        service = units["ummanu-checkpoint.service"]
        for line in (
            "Type=oneshot",
            "User=runtime",
            "EnvironmentFile=-/instance/runtime.env",
            "Nice=10",
            "IOSchedulingPriority=7",
            "checkpoint-run --instance /instance",
        ):
            self.assertIn(line, service)
        timeout = int(
            next(
                line.split("=", 1)[1] for line in service.splitlines() if line.startswith("TimeoutStartSec=")
            )
        )
        self.assertGreater(timeout, 120 + checkpoint.PUSH_TIMEOUT_SECONDS)

        # Exercise the real coordinator at timer activations, including the writer's runtime.
        with tempfile.TemporaryDirectory() as directory:
            clock = [1000.0]
            completed = []

            def write():
                clock[0] += expected_run
                completed.append(clock[0])
                return CheckpointResult(status="unchanged")

            runtime = SimpleNamespace(
                data_dir=Path(directory), checkpoint=SimpleNamespace(write=write),
                checkpoint_push=SimpleNamespace(_clock=lambda: clock[0], due=lambda state, now: False),
            )
            for activation in range(16):
                clock[0] = 1000.0 + activation * interval
                checkpoint_run.run_checkpoint(runtime)
            self.assertEqual(completed, [1050.0, 1350.0, 1650.0, 1950.0])
            self.assertLessEqual(max(b - a for a, b in zip(completed, completed[1:])), 300)


class BoardCutTests(unittest.TestCase):
    def test_backend_failure_is_an_export_refusal(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            data, "board_client", side_effect=TaskError("backend_error", "board unavailable", 1)
        ), self.assertRaisesRegex(RuntimeError, "ummanu task export failed: board unavailable"):
            data._read_board_cut(Path(directory), Path(directory), None, None)

    def test_projection_reads_cards_audit_and_sprints_in_one_shared_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = mock.Mock(spec=SqlCardClient)
            reader = SimpleNamespace(client=client)
            reading = []
            audit = mock.Mock()

            @contextlib.contextmanager
            def snapshot():
                reading.append("transaction")
                yield
                reading.clear()

            def read(value):
                self.assertEqual(reading, ["transaction"])
                with try_file_lock(root / "dispatcher" / "board-bulk.lock") as exclusive:
                    self.assertFalse(exclusive)
                return value

            client.read_snapshot.side_effect = snapshot
            reader.export = lambda: read([{"reference": "demo-1"}])
            audit.status.side_effect = lambda: read({"ok": True})
            audit.events.side_effect = lambda: read([])
            with (
                mock.patch.object(data, "task_audit_for", return_value=audit),
                mock.patch.object(
                    data,
                    "export_sprint_entities",
                    side_effect=lambda instance, actual: read([{"client": actual}]),
                ),
            ):
                cards, history, sprints = data._read_board_cut(root, root, reader, None)
            self.assertEqual(cards, [{"reference": "demo-1"}])
            self.assertEqual(history, [])
            self.assertIs(sprints[0]["client"], client)
            client.read_snapshot.assert_called_once_with()
            with try_file_lock(root / "dispatcher" / "board-bulk.lock") as acquired:
                self.assertTrue(acquired)


if __name__ == "__main__":
    unittest.main()
