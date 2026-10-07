"""Production timing and its operator readers without a board, host, or subprocess."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ummanu import cli, status
from ummanu.dispatch import production
from ummanu.dispatch.host import CommandHostRuntime
from ummanu.dispatch.tick_telemetry import (
    MAX_DURATION_MS,
    TICK_TELEMETRY_RECENT_KEPT,
    tick_p95_finding,
    tick_statistics,
)


class Clock:
    def __init__(self):
        self.now = 0.0

    def read(self):
        return self.now

    def work(self, ms, result=None, error=None):
        def run(*args, **kwargs):
            self.now += ms / 1000
            if error:
                raise error
            return result

        return run


def state_with(recent, **last):
    return {"tick_telemetry": {"recent": recent, "last": last}}


class TickMeasurementTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.clock = Clock()
        self.enterContext(mock.patch.object(production.time, "perf_counter", self.clock.read))
        self.host = mock.Mock(spec=CommandHostRuntime)
        self.host.mode = "real"
        self.host.committing.side_effect = lambda flush: contextlib.nullcontext()
        self.runtime = SimpleNamespace(
            data_dir=self.root,
            owner="unit-test",
            production_state=production.ProductionState(self.root),
            host=self.host,
            reader=mock.Mock(),
            cleanup=mock.Mock(),
            pause=mock.Mock(),
        )
        self.runtime.reader.list.side_effect = self.clock.work(50, [])
        self.runtime.cleanup.replay.side_effect = self.clock.work(30, [])
        self.runtime.pause.summary.return_value = {"mode": "running"}
        seams = {
            "attempt_accounting.publish_pending_attempt_usage": (10, []),
            "attempt_accounting.publish_pending_attempt_outcomes": (20, []),
            "observer_fence": (40, {}),
            "_reconcile_production": (60, []),
            "_advance_active": (70, ([], [], {})),
            "reconcile_post_merge_watches": (80, []),
            "reconcile_after_merge": (90, []),
            "_reconcile_sprint_budget": (100, []),
            "reconcile_observers": (110, []),
            "reconcile_origin_returns": (130, []),
            "_coordinate_checkpoint": (120, (None, None)),
            "retry_pending_observer_stops": (140, []),
            "auto_resume_expired_freeze": (0, None),
        }
        for name, (ms, result) in seams.items():
            self.enterContext(
                mock.patch("ummanu.dispatch.production." + name, side_effect=self.clock.work(ms, result))
            )
        self.save = self.enterContext(
            mock.patch.object(self.runtime.production_state, "save", wraps=self.runtime.production_state.save)
        )

    def last(self):
        telemetry = production.ProductionState(self.root).load()["tick_telemetry"]
        self.assertEqual(len(telemetry["recent"]), 1)
        last = telemetry["last"]
        self.assertAlmostEqual(sum(last["phases"].values()), last["duration_ms"], places=3)
        self.assertEqual(telemetry["recent"][0]["phases"], last["phases"])
        return last

    def test_working_tick_exclusive_phases_and_no_extra_work(self):
        production.production_tick(self.runtime)
        last = self.last()
        self.assertEqual(last["status"], "ok")
        self.assertEqual(
            last["phases"],
            {
                "snapshot": 50.0,
                "reconcile": 170.0,
                "cleanup": 30.0,
                "after-merge": 170.0,
                "launches": 110.0,
                "checkpoint": 120.0,
                "other": 260.0,
            },
        )
        self.assertEqual(self.runtime.reader.list.call_count, 1)
        self.save.assert_called_once()
        self.runtime.cleanup.replay.assert_called_once_with(limit=5)

    def test_failed_tick_keeps_interrupted_and_completed_phases(self):
        with (
            mock.patch.object(
                production,
                "_reconcile_production",
                side_effect=self.clock.work(60, error=RuntimeError("failed")),
            ),
            self.assertRaisesRegex(RuntimeError, "failed"),
        ):
            production.production_tick(self.runtime)
        last = self.last()
        self.assertEqual(last["status"], "failed")
        self.assertEqual(
            last["phases"],
            {
                "snapshot": 50.0,
                "reconcile": 100.0,
                "cleanup": 30.0,
                "other": 30.0,
            },
        )
        self.save.assert_called_once()

    def test_fence_refusal_keeps_partial_phases(self):
        with mock.patch.object(
            production, "observer_fence", side_effect=self.clock.work(40, error=RuntimeError("fence"))
        ):
            result = production.production_tick(self.runtime)
        self.assertEqual(result["status"], "critical")
        self.assertEqual(self.last()["phases"], {"snapshot": 50.0, "reconcile": 40.0, "cleanup": 30.0, "other": 30.0})
        self.runtime.reader.list.assert_called_once()
        self.save.assert_called_once()

    def test_frozen_tick_records_checkpoint_without_board_reads(self):
        self.runtime.pause.summary.return_value = {"mode": "freeze"}
        production.production_tick(self.runtime)
        self.assertEqual(self.last()["phases"], {"checkpoint": 120.0, "other": 140.0})
        self.assertEqual(self.last()["status"], "skipped")
        self.runtime.reader.list.assert_not_called()
        self.save.assert_called_once()

    def test_probe_does_not_save_or_append(self):
        self.runtime.pause.summary.return_value = {"mode": "freeze"}
        before = self.runtime.production_state.load()
        production.production_probe(self.runtime)
        self.assertEqual(before, self.runtime.production_state.load())
        self.save.assert_not_called()

    def test_ring_truncation_restart_and_size(self):
        payload = {}
        names = ("snapshot", "reconcile", "after-merge", "cleanup", "launches", "checkpoint")
        # Long numeric representations and the longest recorded terminal status, pretty printed
        # exactly like production-state.json. Measure only the bytes added by the ring.
        for _ in range(105):
            with production.tick_clock():
                for name in names:
                    with production.tick_phase(name):
                        self.clock.now += 123456.789123
                production.record_tick_telemetry(payload, {"status": "degraded"})
        state = production.ProductionState(self.root)
        state.save(payload)
        restarted = production.ProductionState(self.root).load()
        recent = restarted["tick_telemetry"]["recent"]
        self.assertEqual(len(recent), TICK_TELEMETRY_RECENT_KEPT)
        self.assertEqual([row["seq"] for row in recent], list(range(6, 106)))
        encoded = json.dumps(restarted, indent=2, sort_keys=True).encode()
        del restarted["tick_telemetry"]["recent"]
        without_ring = json.dumps(restarted, indent=2, sort_keys=True).encode()
        self.assertLessEqual(len(encoded) - len(without_ring), 50_000)

    def test_clock_context_resets_and_repeated_phases_accumulate(self):
        with production.tick_clock():
            with production.tick_phase("snapshot"):
                self.clock.now += 1
            with production.tick_phase("snapshot"):
                self.clock.now += 2
            payload = {}
            production.record_tick_telemetry(payload, {"status": "ok"})
        self.assertEqual(payload["tick_telemetry"]["last"]["phases"], {"snapshot": 3000.0, "other": 0.0})
        production.record_tick_telemetry(payload, {"status": "ok"})
        self.assertIsNone(payload["tick_telemetry"]["last"]["duration_ms"])
        self.assertEqual(payload["tick_telemetry"]["last"]["phases"], {})


class TickReaderTests(unittest.TestCase):
    def test_nearest_rank_and_last_100_entries(self):
        values = [{"duration_ms": value} for value in range(1, 101)]
        self.assertEqual(
            tick_statistics(state_with(values)),
            {
                "sample_count": 100,
                "p50_duration_ms": 50.0,
                "p95_duration_ms": 95.0,
            },
        )
        self.assertEqual(
            tick_statistics(state_with([{"duration_ms": MAX_DURATION_MS}] * 30 + values)),
            tick_statistics(state_with(values)),
        )

    def test_doctor_boundaries(self):
        for count, duration, red in (
            (19, 300001, False),
            (20, 300000, False),
            (20, 300001, True),
            (20, 299999, False),
            (0, 300001, False),
        ):
            with self.subTest(count=count, duration=duration):
                finding = tick_p95_finding(state_with([{"duration_ms": duration}] * count))
                self.assertEqual(finding is not None, red)
                if finding:
                    self.assertEqual(finding["code"], "dispatcher_tick_p95_slow")
                    self.assertEqual(finding["severity"], "red")
                    for text in ("p95", "20 samples", "300000 ms", "300001 ms"):
                        self.assertIn(text, finding["message"])

    def test_hostile_values_are_total_for_status_and_doctor(self):
        invalid = [None, -1, "300001", float("nan"), float("inf"), 10**1000, True, {}, []]
        valid = [{"duration_ms": 300001}] * 20
        for value in invalid:
            with self.subTest(value=repr(value)[:50]):
                production_state = state_with(
                    valid + [{"duration_ms": value}], duration_ms=value, phases={"good": 1, "bad": value}
                )
                self.assertEqual(tick_statistics(production_state)["sample_count"], 20)
                self.assertIsNotNone(tick_p95_finding(production_state))
                last = status._last_tick(production_state)
                self.assertIsNone(last["duration_ms"])
                self.assertEqual(last["phases"], {"good": 1.0})
                json.dumps(last, allow_nan=False)
        for recent in (None, {}, "bad", 100, [], [None, [], "bad", {}, {"duration_ms": None}]):
            with self.subTest(recent=recent):
                data = state_with(recent, duration_ms=12, phases="bad")
                self.assertEqual(tick_statistics(data)["sample_count"], 0)
                self.assertIsNone(tick_p95_finding(data))
                self.assertIsNone(status._last_tick(data)["phases"])
        for data in ({}, {"tick_telemetry": None}, {"tick_telemetry": {}}):
            self.assertIsNone(tick_statistics(data)["p95_duration_ms"])
            self.assertIsNone(tick_p95_finding(data))
            self.assertIsNone(status._last_tick(data))

    def test_status_collection_and_text_json_rendering(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            root = Path(directory)
            report = SimpleNamespace(
                ok=True,
                data_dir=root,
                instance_path=root / "instance.yaml",
                instance={},
                bindings={},
                name="test",
                projects=[],
            )
            stack.enter_context(
                mock.patch.object(status, "build_doctor_expectations", return_value=mock.Mock())
            )
            stack.enter_context(mock.patch.object(status, "resolve_installed_packaged", return_value=None))
            for name, value in (
                ("_units", []),
                ("_schedules", []),
                ("_head_registry", {"error": "missing"}),
                ("_card_count", 0),
                ("_host_resources", {}),
                ("_memory_status", {"fact_count": None}),
            ):
                stack.enter_context(mock.patch.object(status, name, return_value=value))
            stack.enter_context(mock.patch.object(status, "store_health", return_value={}))
            stack.enter_context(mock.patch.object(cli, "validate_instance", return_value=report))
            stack.enter_context(
                mock.patch.object(cli.interactive_workspace, "status_line", return_value="workspace")
            )
            for available in (False, True):
                if available:
                    production.ProductionState(root).save(
                        state_with(
                            [{"duration_ms": 100}, {"duration_ms": 200}],
                            duration_ms=200,
                            phases={"snapshot": 20, "other": 180},
                        )
                    )
                snapshot = status.collect_status(
                    report, offline=True, sprints=False, recovery={"resources": []}
                )
                with mock.patch.object(cli, "collect_status", return_value=snapshot):
                    for json_output in (True, False):
                        args = SimpleNamespace(
                            instance=str(report.instance_path),
                            host_fixture=None,
                            offline=True,
                            json=json_output,
                        )
                        output = io.StringIO()
                        with contextlib.redirect_stdout(output):
                            self.assertEqual(cli.run_status(args), 0)
                        if json_output:
                            dispatcher = json.loads(output.getvalue())["dispatcher"]
                            self.assertEqual(
                                dispatcher["tick_statistics"]["sample_count"], 2 if available else 0
                            )
                            self.assertEqual(
                                dispatcher["tick_statistics"]["p95_duration_ms"], 200 if available else None
                            )
                            if available:
                                self.assertEqual(
                                    dispatcher["last_tick"]["phases"], {"snapshot": 20, "other": 180}
                                )
                        elif available:
                            self.assertIn("2 samples, p50 100 ms, p95 200 ms", output.getvalue())
                            self.assertIn("snapshot 20 ms", output.getvalue())
                        else:
                            self.assertIn("tick durations: unavailable", output.getvalue())
                            self.assertIn("last tick phases: unavailable", output.getvalue())

    def test_doctor_inspection_includes_stable_red_finding_offline(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            root = Path(directory)
            report = SimpleNamespace(data_dir=root, instance_path=root / "instance.yaml", instance={})
            production.ProductionState(root).save(state_with([{"duration_ms": 300001}] * 20))
            for name in (
                "_restore_findings",
                "automation_busy_findings",
                "doctor_live_root_findings",
                "checkpoint_rpo_findings",
                "snapshot_foreign_commit_findings",
                "checkpoint_findings",
                "secret_store_findings",
                "fallback_errors",
                "_recovery_findings",
                "_board_schema_findings",
            ):
                stack.enter_context(mock.patch.object(cli, name, return_value=[]))
            stack.enter_context(
                mock.patch.object(cli, "production_runtime_provenance_finding", return_value=None)
            )
            stack.enter_context(
                mock.patch.object(cli, "collect_recovery_inventory", return_value={"resources": []})
            )
            stack.enter_context(
                mock.patch.object(cli, "_codex_home_status", return_value={"login_missing": False})
            )
            stack.enter_context(mock.patch.object(cli, "board_schema_inspection", return_value={}))
            args = SimpleNamespace(offline=True, dry_run=True, strict=False)
            findings = cli.collect_doctor_inspection(report, args).findings
            self.assertEqual(findings, [tick_p95_finding(production.ProductionState(root).load())])
