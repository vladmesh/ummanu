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
    TICK_CARDS_KEPT,
    TICK_COUNTERS,
    TICK_TELEMETRY_RECENT_KEPT,
    reconcile_ms,
    tick_count,
    tick_counter_values,
    tick_p95_finding,
    tick_statistics,
)
from ummanu.infra.checkpoint_run import load_checkpoint_state, run_checkpoint

# The real pass; the fixture below replaces it with a timed seam.
ADVANCE_ACTIVE = production._advance_active


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
                "fence": 40.0,
                "reconcile_production": 60.0,
                "advance_active": 70.0,
                "cleanup": 30.0,
                "after-merge": 170.0,
                "launches": 110.0,
                "other": 260.0,
            },
        )
        self.assertEqual(reconcile_ms(last["phases"]), 170.0)
        self.assertEqual(self.runtime.reader.list.call_count, 1)
        self.save.assert_called_once()
        self.runtime.cleanup.replay.assert_called_once_with(limit=5)

    def test_checkpoint_failure_does_not_degrade_the_tick_or_enter_its_phases(self):
        self.runtime.checkpoint = mock.Mock()
        self.runtime.checkpoint.write.return_value.to_json.return_value = {
            "status": "blocked", "reason": "audit pending",
        }
        result = run_checkpoint(self.runtime)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["checkpoint"]["reason"], "audit pending")
        self.runtime.checkpoint.write.assert_called_once_with()
        tick = production.production_tick(self.runtime)
        self.assertEqual(tick["status"], "ok")
        self.assertNotIn("checkpoint", tick)
        self.assertNotIn("checkpoint", self.last()["phases"])
        self.assertTrue(self.last()["healthy"])
        self.assertEqual(load_checkpoint_state(self.root)["checkpoint"]["reason"], "audit pending")
        self.runtime.checkpoint.write.assert_called_once_with()

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
                "fence": 40.0,
                "reconcile_production": 60.0,
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
        self.assertEqual(self.last()["phases"], {"snapshot": 50.0, "fence": 40.0, "cleanup": 30.0, "other": 30.0})
        self.runtime.reader.list.assert_called_once()
        self.save.assert_called_once()

    def test_frozen_tick_records_its_work_without_board_reads(self):
        self.runtime.pause.summary.return_value = {"mode": "freeze"}
        production.production_tick(self.runtime)
        self.assertEqual(self.last()["phases"], {"other": 140.0})
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
        # Every phase a tick can record (ummanu-131 split reconcile into three), each at its maximum.
        names = ("snapshot", "fence", "reconcile_production", "advance_active", "after-merge", "cleanup",
                 "launches")
        # Long numeric representations and the longest recorded terminal status, pretty printed
        # exactly like production-state.json. Measure only the bytes added by the ring.
        for _ in range(105):
            with production.tick_clock():
                for name in names:
                    with production.tick_phase(name):
                        self.clock.now += 123456.789123
                        # Per-card details and every counter at their maximum: kept off the ring.
                        for card in range(TICK_CARDS_KEPT + 5):
                            with production.tick_card("x" * 200 + str(card), 2**40):
                                for counter in TICK_COUNTERS:
                                    tick_count(counter, 2**40)
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

    def state_writes(self):
        """An independent count of state publications that completed, at the file writer itself."""
        written = []
        write = production.write_json

        def counted(path, payload):
            write(path, payload)
            written.append(path)

        self.enterContext(mock.patch.object(production, "write_json", side_effect=counted))
        return written

    def test_advance_records_each_card_and_the_ticks_actual_writes(self):
        # Three dispatcher records go into every advance; one card flushes them five times, one never.
        writes = {"slow-1": (300, 5, 2, 4096), "quick-2": (20, 0, 0, 0)}
        records = {ref: SimpleNamespace(to_json=dict) for ref in ("slow-1", "quick-2", "held-3")}
        published = self.state_writes()

        def advance(runtime, task, records, payload):
            ms, flushes, intents, written = writes[task["ref"]]
            self.clock.now += ms / 1000
            tick_count("save_records", flushes)
            tick_count("cleanup_intent_writes", intents)
            tick_count("cleanup_bytes_written", written)
            return {"ref": task["ref"], "status": "ok"}

        tasks = [{"ref": "quick-2", "state": "in_progress"}, {"ref": "slow-1", "state": "in_progress"}]
        with (
            mock.patch.object(production, "_advance_active", side_effect=ADVANCE_ACTIVE),
            mock.patch.object(production, "_production_tick_active", side_effect=advance),
            mock.patch.object(production, "_production_tasks", return_value=tasks),
            mock.patch.object(production, "fenced_task", return_value=False),
            mock.patch.object(self.runtime.production_state, "records", return_value=records),
        ):
            production.production_tick(self.runtime)
        last = self.last()
        self.assertEqual(last["phases"]["advance_active"], 320.0)
        self.assertEqual(reconcile_ms(last["phases"]), 40.0 + 60.0 + 320.0)
        self.assertEqual(
            last["cards"],
            [
                {"ref": "slow-1", "ms": 300.0, "records": 3, "save_records": 5, "cleanup_intent_writes": 2,
                 "cleanup_bytes_written": 4096, "production_state_saves": 0},
                {"ref": "quick-2", "ms": 20.0, "records": 3, "save_records": 0, "cleanup_intent_writes": 0,
                 "cleanup_bytes_written": 0, "production_state_saves": 0},
            ],
        )
        self.assertEqual(
            last["counters"],
            {"save_records": 5, "cleanup_intent_writes": 2, "cleanup_bytes_written": 4096,
             "production_state_saves": len(published)},
        )
        self.assertEqual(len(published), 1)
        self.assertNotIn("counters", production.ProductionState(self.root).load()["tick_telemetry"]["recent"][0])
        shown = status._last_tick(production.ProductionState(self.root).load())
        self.assertEqual(shown["reconcile_ms"], 420.0)
        self.assertEqual(shown["counters"], last["counters"])
        self.assertEqual(shown["cards"], last["cards"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli._print_tick_measurements({"tick_statistics": tick_statistics({}), "last_tick": shown})
        self.assertIn("last tick card slow-1: 300 ms, records 3, flushes 5,", output.getvalue())
        self.assertIn("last tick card quick-2: 20 ms, records 3, flushes 0,", output.getvalue())

    def test_terminal_save_only_tick_counts_its_own_save(self):
        published = self.state_writes()
        production.production_tick(self.runtime)
        self.assertEqual(len(published), 1)
        self.assertEqual(
            self.last()["counters"],
            {"save_records": 0, "cleanup_intent_writes": 0, "cleanup_bytes_written": 0, "production_state_saves": 1},
        )

    def test_failed_tick_counts_every_completed_publication_once(self):
        published = self.state_writes()

        def fail(runtime, records, payload, *args, **kwargs):
            runtime.production_state.save(payload)  # a mid-tick flush that completed
            tick_count("cleanup_intent_writes")
            tick_count("cleanup_bytes_written", 10)
            raise RuntimeError("failed")

        with (
            mock.patch.object(production, "_reconcile_production", side_effect=fail),
            self.assertRaisesRegex(RuntimeError, "failed"),
        ):
            production.production_tick(self.runtime)
        last = self.last()
        self.assertEqual(last["status"], "failed")
        # The mid-tick flush and the recovery record's own save: both completed, each counted once.
        self.assertEqual(len(published), 2)
        self.assertEqual(last["counters"]["production_state_saves"], 2)
        self.assertEqual(last["counters"]["cleanup_intent_writes"], 1)
        self.assertEqual(last["counters"]["cleanup_bytes_written"], 10)
        self.assertEqual(last["cards"], [])

    def test_failed_terminal_save_is_not_counted_and_its_recovery_is(self):
        published = []
        write = production.write_json
        failures = [OSError("disk full")]

        def flaky(path, payload):
            if failures:
                raise failures.pop()
            write(path, payload)
            published.append(path)

        with (
            mock.patch.object(production, "write_json", side_effect=flaky),
            self.assertRaisesRegex(OSError, "disk full"),
        ):
            production.production_tick(self.runtime)
        last = self.last()
        self.assertEqual(last["status"], "failed")
        self.assertEqual(len(published), 1)
        self.assertEqual(last["counters"]["production_state_saves"], 1)

    def test_counters_and_cards_outside_a_tick_are_inert(self):
        tick_count("save_records")
        with production.tick_card("outside", 3):
            self.clock.now += 1
        self.assertIsNone(tick_counter_values())
        production.ProductionState(self.root).save({})
        payload = {}
        production.record_tick_telemetry(payload, {"status": "ok"})
        self.assertIsNone(payload["tick_telemetry"]["last"]["counters"])
        self.assertEqual(payload["tick_telemetry"]["last"]["cards"], [])

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
                production_state["tick_telemetry"]["last"].update(
                    counters={"save_records": value, "cleanup_intent_writes": 3},
                    cards=[value, {"ref": value, "ms": 1, "records": 2},
                           {"ref": "card-1", "ms": value, "records": value, "save_records": value}],
                )
                last = status._last_tick(production_state)
                self.assertIsNone(last["duration_ms"])
                self.assertEqual(last["phases"], {"good": 1.0})
                self.assertIsNone(last["reconcile_ms"])
                self.assertEqual(last["counters"], {"cleanup_intent_writes": 3})
                strings = [{"ref": value, "ms": 1.0, "records": 2}] if isinstance(value, str) else []
                self.assertEqual(last["cards"], [*strings, {"ref": "card-1", "ms": None, "records": None}])
                json.dumps(last, allow_nan=False)
        for recent in (None, {}, "bad", 100, [], [None, [], "bad", {}, {"duration_ms": None}]):
            with self.subTest(recent=recent):
                data = state_with(recent, duration_ms=12, phases="bad")
                self.assertEqual(tick_statistics(data)["sample_count"], 0)
                self.assertIsNone(tick_p95_finding(data))
                self.assertIsNone(status._last_tick(data)["phases"])
                self.assertIsNone(status._last_tick(data)["counters"])
                self.assertIsNone(status._last_tick(data)["cards"])
        for data in ({}, {"tick_telemetry": None}, {"tick_telemetry": {}}):
            self.assertIsNone(tick_statistics(data)["p95_duration_ms"])
            self.assertIsNone(tick_p95_finding(data))
            self.assertIsNone(status._last_tick(data))

    def test_status_text_shows_reconcile_counters_and_cards(self):
        counters = {"save_records": 4, "cleanup_intent_writes": 2, "cleanup_bytes_written": 4096,
                    "production_state_saves": 4}
        data = state_with(
            [{"duration_ms": 500}],
            duration_ms=500,
            phases={"fence": 40, "reconcile_production": 60, "advance_active": 320, "other": 80},
            counters=counters,
            cards=[{"ref": "slow-1", "ms": 300, "records": 3, **counters}],
        )
        last = status._last_tick(data)
        self.assertEqual(last["reconcile_ms"], 420.0)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli._print_tick_measurements({"tick_statistics": tick_statistics(data), "last_tick": last})
        text = output.getvalue()
        self.assertIn("fence 40 ms, reconcile_production 60 ms, advance_active 320 ms", text)
        self.assertIn("last tick reconcile: 420 ms", text)
        self.assertIn("last tick writes: save_records 4, cleanup_intent_writes 2, cleanup_bytes_written 4096, "
                      "production_state_saves 4", text)
        self.assertIn("last tick card slow-1: 300 ms, records 3, flushes 4, cleanup intents 2 (4096 bytes), "
                      "state saves 4", text)
        # A tick recorded before these fields existed prints what it has and nothing invented.
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli._print_tick_measurements({"tick_statistics": tick_statistics(data),
                                          "last_tick": status._last_tick(state_with([], phases={"reconcile": 5}))})
        self.assertIn("last tick reconcile: 5 ms", output.getvalue())
        self.assertNotIn("last tick writes", output.getvalue())

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
