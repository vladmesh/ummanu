"""Production timing and its operator readers without a board, host, or subprocess."""

from __future__ import annotations

import contextlib
import functools
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.production_runtime_fixtures import registered_production_runtime
from ummanu import cli, status
from ummanu.dispatch import (
    assessment_decision,
    cleanup as cleanup_module,
    gate_lifecycle,
    production,
    release_lifecycle,
    runtime as runtime_module,
    wait_vitality,
)
from ummanu.dispatch.cleanup import CleanupOwner
from ummanu.dispatch.entrypoint_guard import EntrypointMoved
from ummanu.dispatch.gate import GateResult
from ummanu.dispatch.host import CommandHostRuntime
from ummanu.dispatch.production_checkout import ProductionActivationRefused
from ummanu.dispatch.runtime import DispatcherRuntime
from ummanu.dispatch.state import DispatcherRecord
from ummanu.dispatch.tick_telemetry import (
    CARD_STAGES,
    MAX_DURATION_MS,
    TICK_CARDS_KEPT,
    TICK_COUNTERS,
    TICK_TELEMETRY_RECENT_KEPT,
    reconcile_ms,
    stage_breakdown,
    tick_count,
    tick_counter_values,
    tick_p95_finding,
    tick_stage,
    tick_statistics,
)
from ummanu.dispatch.types import HostError
from ummanu.infra.checkpoint_run import load_checkpoint_state, run_checkpoint
from ummanu.tasks import TaskError

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
        # ummanu-145: the cleanup phase's one automatic replay runs on its elapsed allowance.
        self.runtime.cleanup.replay.assert_called_once_with(limit=5, allowance=production.CLEANUP_REPLAY_ALLOWANCE)

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
                # The seam replaces the whole card advance, so nothing in it is attributed to a stage.
                {"ref": "slow-1", "ms": 300.0, "records": 3, "save_records": 5, "cleanup_intent_writes": 2,
                 "cleanup_bytes_written": 4096, "production_state_saves": 0, "stages": {"unclassified": 300.0}},
                {"ref": "quick-2", "ms": 20.0, "records": 3, "save_records": 0, "cleanup_intent_writes": 0,
                 "cleanup_bytes_written": 0, "production_state_saves": 0, "stages": {"unclassified": 20.0}},
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



class CardStageTests(unittest.TestCase):
    """Where a card's advance time went, through the real advance, wait and flush seams."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.clock = Clock()
        self.reads = 0

        def read():
            self.reads += 1
            return self.clock.now

        self.enterContext(mock.patch.object(production.time, "perf_counter", read))

    def contended(self, ms):
        """The real ownership lock, entered only after `ms` of waiting for it."""
        real = cleanup_module.ownership_lock

        @contextlib.contextmanager
        def lock(data_dir):
            self.clock.now += ms / 1000
            with real(data_dir):
                yield

        return lock

    def wait_runtime(self):
        """A production runtime whose cards wait on their worker through `wait_watchdog`."""
        host = mock.Mock(spec=CommandHostRuntime)
        host.mode = "real"
        host.worker_status.side_effect = self.clock.work(40, {"last_activity": 5.0})
        host.committing.side_effect = lambda flush: contextlib.nullcontext()
        reader = mock.Mock()
        reader.show.side_effect = lambda ref: self.clock.work(20, {"ref": ref, "project": "p"})()
        reader.list.return_value = []
        owner = CleanupOwner(SimpleNamespace(data_dir=self.root))
        self.enterContext(mock.patch.object(owner, "_workspace_identity", side_effect=self.clock.work(25, {})))
        owner.journal = mock.Mock()
        owner.journal.intent_state.return_value = "intent-stat"
        owner.journal.remember.side_effect = self.clock.work(12)
        runtime = SimpleNamespace(
            data_dir=self.root, owner="unit-test", production_state=production.ProductionState(self.root),
            host=host, reader=reader, cleanup=mock.Mock(), pause=mock.Mock(),
        )
        runtime.cleanup.replay.return_value = []
        runtime.cleanup.remember_record = owner.remember_record
        runtime.pause.summary.return_value = {"mode": "running"}
        runtime.save_records = functools.partial(DispatcherRuntime.save_records, runtime)

        def tick_task(task, records, payload, attempt_id):
            self.clock.now += 0.003  # routing no stage names: the measured remainder
            if task["ref"] == "quick-3":
                return {"status": "ok", "pilot_ref": task["ref"]}
            return wait_vitality.wait_watchdog(
                runtime, task, records[task["ref"]], records, payload, attempt_id, kind="worker"
            )

        runtime._tick_task = tick_task
        return runtime

    def test_wait_cards_split_reads_observation_and_nested_flushes_exactly_once(self):
        runtime = self.wait_runtime()

        def record(workspace):
            return SimpleNamespace(workspace=workspace, worker="worker-1", attempt_id="a-1", paused_worker_at=None,
                                   worker_progress_at=0.0, activation_recovery=None,
                                   to_json=lambda: {"attempt_id": "a-1"})

        records = {"slow-1": record("/w/slow-1"), "fail-2": record("/w/fail-2"), "quick-3": record("")}
        tasks = [{"ref": ref, "state": "in_progress"} for ref in records]

        def reduce(runtime, task, record, records, payload, status, **kwargs):
            self.clock.now += 0.010
            runtime.save_records(payload, records)  # the episode it stores: a flush inside vitality

        def decide(runtime, task, *args, **kwargs):
            self.clock.now += 0.008 if task["ref"] == "slow-1" else 0.009
            if task["ref"] == "fail-2":
                raise RuntimeError("decision interrupted")
            return {"status": "ok", "pilot_ref": task["ref"], "action": "waiting-worker-report"}

        write = production.write_json

        def slow_write(path, payload):
            self.clock.now += 0.030
            write(path, payload)

        quiet = ("attempt_accounting.publish_pending_attempt_usage",
                 "attempt_accounting.publish_pending_attempt_outcomes", "reconcile_post_merge_watches",
                 "reconcile_after_merge", "_reconcile_sprint_budget", "reconcile_observers",
                 "reconcile_origin_returns", "_reconcile_production")
        patches = (
            (production, "observer_fence", mock.Mock(return_value={})),
            (production, "auto_resume_expired_freeze", mock.Mock(return_value=None)),
            (production, "_production_tasks", mock.Mock(return_value=tasks)),
            (production, "fenced_task", mock.Mock(return_value=False)),
            (production, "_production_claim_ready", mock.Mock(return_value=None)),
            (production, "write_json", slow_write),
            (runtime.production_state, "records", mock.Mock(return_value=records)),
            (runtime_module, "ownership_lock", self.contended(7)),
            (cleanup_module, "ownership_lock", self.contended(7)),
            (wait_vitality, "_provider_failure_outcome", self.clock.work(5)),
            (wait_vitality, "answer_owed_since_for_wait", mock.Mock(return_value=None)),
            (wait_vitality, "reduce_and_store_vitality_episode", reduce),
            (wait_vitality, "_decide_wait_by_verdict", decide),
        )
        with contextlib.ExitStack() as stack:
            for name in quiet:
                stack.enter_context(mock.patch("ummanu.dispatch.production." + name, return_value=[]))
            for target, name, value in patches:
                stack.enter_context(mock.patch.object(target, name, value))
            result = production.production_tick(runtime)
        self.assertEqual(result["status"], "degraded")
        self.assertEqual([(error["ref"], error["message"]) for error in result["errors"]], [("fail-2", "RuntimeError")])
        state = production.ProductionState(self.root).load()
        last = state["tick_telemetry"]["last"]
        cards = {card["ref"]: card for card in last["cards"]}
        self.assertEqual([card["ref"] for card in last["cards"]], ["slow-1", "fail-2", "quick-3"])
        # The first flush of the tick reads and publishes both workspace cards' cleanup projections;
        # the second finds them unchanged. The episode's flush is inside vitality, counted only as
        # flush parts, and the progress flush outside any other stage likewise.
        self.assertEqual(cards["slow-1"]["stages"], {
            "board_read": 20.0, "observation": 40.0, "provider": 5.0, "vitality": 10.0, "lifecycle": 8.0,
            "flush": 0.0, "flush_card_read": 40.0, "flush_identity": 100.0, "flush_journal": 24.0,
            "flush_lock": 28.0, "flush_write": 60.0, "unclassified": 3.0,
        })
        # Interrupted in its decision: the stages before it and the interrupted one itself are kept.
        self.assertEqual(cards["fail-2"]["stages"], {
            "board_read": 20.0, "observation": 40.0, "provider": 5.0, "vitality": 10.0, "lifecycle": 9.0,
            "flush": 0.0, "flush_identity": 100.0, "flush_lock": 14.0, "flush_write": 60.0, "unclassified": 3.0,
        })
        self.assertEqual(cards["quick-3"]["stages"], {"board_read": 20.0, "unclassified": 3.0})
        self.assertEqual([cards[ref]["ms"] for ref in records], [338.0, 261.0, 23.0])
        for card in last["cards"]:
            self.assertAlmostEqual(sum(card["stages"].values()), card["ms"], delta=0.001 * len(card["stages"]))
        self.assertEqual([cards[ref]["save_records"] for ref in records], [2, 2, 0])
        # The tick's phases and counters keep their meaning: the cards are a breakdown of advance_active.
        self.assertEqual(last["phases"]["advance_active"], 622.0)
        self.assertEqual(last["counters"]["save_records"], 4)
        self.assertNotIn("stages", last["phases"])
        self.assertEqual(set(state["tick_telemetry"]["recent"][0]), {"seq", "at", "status", "healthy",
                                                                     "duration_ms", "phases"})
        for record_json in state["records"].values():
            self.assertEqual(record_json, {"attempt_id": "a-1"})
        shown = status._last_tick(state)
        self.assertEqual(shown["cards"], last["cards"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli._print_tick_measurements({"tick_statistics": tick_statistics(state), "last_tick": shown})
        self.assertIn("last tick card slow-1 stages: board_read 20 ms, observation 40 ms, provider 5 ms, "
                      "vitality 10 ms, lifecycle 8 ms, flush 0 ms, flush_card_read 40 ms, flush_identity 100 ms, "
                      "flush_journal 24 ms, flush_lock 28 ms, flush_write 60 ms, unclassified 3 ms",
                      output.getvalue())

    def test_interrupted_stage_keeps_its_time_and_the_exception(self):
        error = ValueError("flush refused")
        with production.tick_clock():
            with self.assertRaises(ValueError) as caught, production.tick_card("card-1", 1), tick_stage("flush"):
                self.clock.now += 0.004
                with tick_stage("flush_write"):
                    self.clock.now += 0.002
                    raise error
            payload = {}
            production.record_tick_telemetry(payload, {"status": "ok"})
        self.assertIs(caught.exception, error)
        card = payload["tick_telemetry"]["last"]["cards"][0]
        self.assertEqual(card["ms"], 6.0)
        self.assertEqual(card["stages"], {"flush": 4.0, "flush_write": 2.0, "unclassified": 0.0})

    def test_stages_outside_a_card_read_no_clock_and_change_no_phase(self):
        with tick_stage("flush"):
            self.clock.now += 1
        self.assertEqual(self.reads, 0)
        with production.tick_clock():
            with production.tick_phase("reconcile_production"):
                before = self.reads
                with tick_stage("flush"):  # a flush in a tick but outside any card
                    self.clock.now += 0.005
                self.assertEqual(self.reads, before)
            with production.tick_card("card-1", 0), tick_stage("not-a-stage"):
                self.clock.now += 0.002
            payload = {}
            production.record_tick_telemetry(payload, {"status": "ok"})
        last = payload["tick_telemetry"]["last"]
        self.assertEqual(last["phases"], {"reconcile_production": 5.0, "other": 2.0})
        self.assertEqual(last["cards"][0]["stages"], {"unclassified": 2.0})

    def test_rounding_reconciles_with_the_card_within_a_microsecond_per_stage(self):
        under = stage_breakdown({"board_read": 1.0004, "flush": 1.0004, "observation": 1.0004}, 3.001)
        self.assertEqual(under, {"board_read": 1.0, "flush": 1.0, "observation": 1.0, "unclassified": 0.001})
        over = stage_breakdown({"board_read": 1.0006, "flush_write": 1.0006}, 2.001)
        self.assertEqual(over, {"board_read": 1.0, "flush_write": 1.001, "unclassified": 0.0})
        for breakdown, ms in ((under, 3.001), (over, 2.001)):
            self.assertAlmostEqual(sum(breakdown.values()), ms, delta=0.001 * len(breakdown))

    def test_historical_and_malformed_stages_stay_unknown(self):
        cards = [
            {"ref": "old-1", "ms": 10, "records": 1},  # recorded before stages existed
            {"ref": "bad-2", "ms": 10, "records": 1, "stages": "flush"},
            {"ref": "bad-3", "ms": 10, "records": 1,
             "stages": {"flush": -1, "flush_write": True, "unclassified": float("nan"), "secret": 5}},
            {"ref": "mixed-4", "ms": 10, "records": 1,
             "stages": {"board_read": 2.5, "flush": "3", "body": 1, "unclassified": 10**1000}},
        ]
        data = state_with([{"duration_ms": 20}], duration_ms=20, phases={"advance_active": 20}, cards=cards)
        shown = status._last_tick(data)
        self.assertEqual([card.get("stages") for card in shown["cards"]], [None, None, None, {"board_read": 2.5}])
        self.assertEqual([("stages" in card) for card in shown["cards"]], [False, False, False, True])
        json.dumps(shown, allow_nan=False)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli._print_tick_measurements({"tick_statistics": tick_statistics(data), "last_tick": shown})
        text = output.getvalue()
        self.assertIn("last tick card mixed-4 stages: board_read 2 ms", text)
        for ref in ("old-1", "bad-2", "bad-3"):
            self.assertNotIn(f"last tick card {ref} stages", text)

    def test_stage_names_at_every_call_site_are_in_the_vocabulary(self):
        source = Path(production.__file__).parent
        named = set()
        for path in source.glob("*.py"):
            named |= set(re.findall(r'tick_stage(?:_entering)?\("([^"]+)"', path.read_text(encoding="utf-8")))
        self.assertEqual(named - set(CARD_STAGES), set())
        self.assertEqual(set(CARD_STAGES) - named, set())


RELEASE_REF = "sample-158"
RELEASE_SHA = "a" * 40
#: What each leaf of a release costs here, in ms: the ordinary Assessment release adds up to the
#: 15.354 s the production release of 81442 left unclassified, plus 3 ms no stage names.
RELEASE_LEAVES = {
    "merge pr base": 600, "merge pr": 5601, "merge push": 5601, "post-merge fetch": 800,
    "post-merge fast-forward": 1200, "post-merge commit": 500, "post-merge landed commit": 100,
    "runtime:release-before": 150, "runtime:release-after": 150, "gate": 3000, "teardown": 1500, "stop": 300,
}


class _ReleaseHost(CommandHostRuntime):
    """`complete_green`'s merge paths, teardown and gate over leaf commands that only spend time."""

    def __init__(self, root, clock, *, ci):
        catalog = SimpleNamespace(
            adapter=lambda project: {"validation": {"ci": ci}},
            integration_base=lambda project, override: "main",
            binding=lambda project: {"repo": str(root / "project")},
            instance_dir=str(root / "instance"),
            project_default_branch=lambda project: "main",
        )
        super().__init__(catalog, root, mode="real", production_runtime=registered_production_runtime(root))
        self.clock = clock
        self.calls = []
        self.fail = {}

    def leaf(self, label, result=None):
        self.calls.append(label)
        self.clock.now += RELEASE_LEAVES[label] / 1000
        if label in self.fail:
            raise self.fail.pop(label)
        return result

    def _decide_workspace_environment_ownership(self, workspace):
        return "dispatcher"

    def _require_production_runtime(self, boundary, within=None):
        return self.leaf("runtime:" + boundary)

    def _remote_git_checked(self, project, checkout, args, label):
        return self.leaf(label)

    def _run(self, args, label, *, cwd=None):
        stdout = {"merge pr base": "main\n", "post-merge commit": RELEASE_SHA + "\n",
                  "post-merge landed commit": RELEASE_SHA + "\n"}.get(label, "")
        return self.leaf(label, SimpleNamespace(stdout=stdout, stderr="", returncode=0))

    def gate_check(self, task, record):
        return self.leaf("gate", GateResult("green", "all checks passed"))

    def teardown(self, record):
        return self.leaf("teardown", {"workspace": "removed"})

    def stop(self, record):
        self.leaf("stop")


class ReleaseStageTests(unittest.TestCase):
    """A release's cost by effect site, through the real Assessment, release and merge orchestration."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.clock = Clock()
        self.reads = 0

        def read():
            self.reads += 1
            return self.clock.now

        self.enterContext(mock.patch.object(production.time, "perf_counter", read))
        self.host = _ReleaseHost(self.root, self.clock, ci="github")
        self.committed = {}

        def board(ms, result):
            def write(**kwargs):
                self.clock.now += ms / 1000
                if isinstance(result, Exception):
                    raise result
                self.committed[kwargs["request_id"]] = {"event_id": kwargs["request_id"], "ref": "op-1"}
                return result

            return write

        self.board = board
        writer = mock.Mock()
        writer.create.side_effect = board(200, {"task": {"ref": "op-1"}})
        writer.comment.side_effect = board(100, {})
        audit = mock.Mock()
        audit.events.side_effect = self.clock.work(100, [])  # the decision's audit history
        audit.committed_event.side_effect = self.committed.get
        self.runtime = SimpleNamespace(
            data_dir=self.root, owner="unit-test", production_state=production.ProductionState(self.root),
            host=self.host, reader=mock.Mock(), cleanup=mock.Mock(), writer=writer, audit=audit,
        )
        self.runtime.save_records = functools.partial(DispatcherRuntime.save_records, self.runtime)
        self.runtime.reader.show.side_effect = self.clock.work(150, {"ref": RELEASE_REF, "comments": []})
        self.accounting = mock.Mock()
        self.accounting.terminal_effect.side_effect = self.clock.work(900)  # the board move and outcome
        write = production.write_json

        def slow_write(path, payload):
            self.clock.now += 0.050
            write(path, payload)

        for target, name, value in (
            (assessment_decision, "recorded_decision", self.clock.work(250, ("release", "Ship it.", ()))),
            (release_lifecycle, "attempt_accounting", self.accounting),
            (gate_lifecycle, "accept_green_gate", self.clock.work(400)),  # the release gate attestation
            (production, "write_json", slow_write),
        ):
            self.enterContext(mock.patch.object(target, name, value))
        self.record = DispatcherRecord(
            worker="w", workspace=str(self.root / "ws"), handle="", head="codex", review_head="claude",
            attempt_id="attempt-1", comment_baseline=0, review_baseline=0, state="assessment", claimed_at=0.0,
        )
        self.record.worker_continuation.begin_park("review", 0, "review:green", "green")
        self.record.worker_continuation.confirm_park()
        self.records = {RELEASE_REF: self.record}
        self.payload = {}

    def task(self, kind="code"):
        return {"ref": RELEASE_REF, "type": kind, "project": "sample", "sprint": "sprint:1",
                "state": "assessment", "workspace": {}, "comments": []}

    def tick(self, advance):
        """One card of a production tick: 3 ms of routing no stage names, then `advance`."""
        outcome = None
        with production.tick_clock():
            with production.tick_card(RELEASE_REF, 1):
                self.clock.now += 0.003
                outcome = advance()
            telemetry = {}
            production.record_tick_telemetry(telemetry, {"status": "ok"})
        card = telemetry["tick_telemetry"]["last"]["cards"][0]
        self.assertAlmostEqual(sum(card["stages"].values()), card["ms"], delta=0.001 * len(card["stages"]))
        return outcome, card, telemetry

    def assess(self, kind="code"):
        return lambda: assessment_decision.advance_assessment(
            self.runtime, self.task(kind), self.records, self.payload, "attempt-1"
        )

    def test_an_ordinary_release_splits_into_its_effect_sites(self):
        outcome, card, telemetry = self.tick(self.assess())
        self.assertEqual(outcome["to"], "done")
        self.assertEqual(self.host.calls, [
            "gate", "runtime:release-before", "merge pr base", "merge pr", "post-merge fetch",
            "post-merge fast-forward", "runtime:release-after", "post-merge commit", "teardown",
        ])
        self.assertEqual(card["ms"], 15354.0)
        # Before these stages the same release read flush 0, flush_lock 0, flush_write 100 and
        # unclassified 15254: everything but its two flushes was opaque.
        self.assertEqual(card["stages"], {
            "flush": 0.0, "flush_lock": 0.0, "flush_write": 100.0,
            "assessment": 450.0, "release_e2e": 0.0, "release_gate": 3400.0, "release_runtime": 300.0,
            "release_merge": 6201.0, "release_refresh": 2000.0, "release_landed": 500.0,
            "release_teardown": 1500.0, "release_terminal": 900.0, "unclassified": 3.0,
        })
        self.assertEqual(card["save_records"], 2)
        # The tick's own phases and counters are untouched by the card's breakdown.
        last = telemetry["tick_telemetry"]["last"]
        self.assertEqual(last["counters"]["save_records"], 2)
        self.assertNotIn("stages", last.get("phases") or {})
        self.assertEqual(set(telemetry["tick_telemetry"]["recent"][0]),
                         {"seq", "at", "status", "healthy", "duration_ms", "phases"})
        shown = status._last_tick(telemetry)
        self.assertEqual(shown["cards"][0]["stages"], card["stages"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli._print_tick_measurements({"tick_statistics": tick_statistics(telemetry), "last_tick": shown})
        self.assertIn("release_merge 6201 ms, release_refresh 2000 ms, release_landed 500 ms", output.getvalue())

    def test_a_push_release_names_the_push_as_its_merge(self):
        self.host = self.runtime.host = _ReleaseHost(self.root, self.clock, ci="local")
        outcome, card, _ = self.tick(self.assess())
        self.assertEqual(outcome["to"], "done")
        self.assertEqual(self.host.calls[2:6], ["merge push", "post-merge fetch", "post-merge fast-forward",
                                                "runtime:release-after"])
        self.assertEqual({name: card["stages"][name] for name in (
            "release_runtime", "release_merge", "release_refresh", "release_landed")},
            {"release_runtime": 300.0, "release_merge": 5601.0, "release_refresh": 2000.0, "release_landed": 100.0})

    def test_an_interrupted_base_read_keeps_its_time_and_blocks_the_card(self):
        self.host.fail["merge pr base"] = HostError("gh: connection reset")
        outcome, card, _ = self.tick(self.assess())
        self.assertEqual((outcome["status"], outcome["reason"]), ("blocked", "merge failed"))
        self.assertNotIn("merge pr", self.host.calls)
        self.assertEqual(card["stages"], {
            "flush": 0.0, "flush_lock": 0.0, "flush_write": 50.0,
            "assessment": 450.0, "release_e2e": 0.0, "release_gate": 3400.0, "release_runtime": 150.0,
            "release_merge": 600.0, "release_teardown": 300.0, "release_terminal": 900.0, "unclassified": 3.0,
        })
        self.assertEqual(self.accounting.terminal_effect.call_args.kwargs["target"], "blocked")
        self.assertNotIn(RELEASE_REF, self.records)

    def test_a_merged_release_refused_activation_replays_without_a_second_merge(self):
        refusal = EntrypointMoved(target=RELEASE_SHA, package="ummanu", missing="src/ummanu/__main__.py")
        self.host.fail["post-merge fast-forward"] = ProductionActivationRefused(
            refusal, checkout=self.root / "project", old="b" * 40
        )
        # The refusal's comment does not reach the board: the obligation stays with the record.
        self.runtime.writer.comment.side_effect = self.board(100, TaskError("unavailable", "board down", 1))
        outcome, card, _ = self.tick(self.assess())
        self.assertEqual(outcome["action"], "production-activation-recovery-pending")
        self.assertIsNotNone(self.records[RELEASE_REF].activation_recovery)
        self.assertEqual(card["stages"], {
            "flush": 0.0, "flush_lock": 0.0, "flush_write": 50.0,
            "assessment": 450.0, "release_e2e": 0.0, "release_gate": 3400.0, "release_runtime": 150.0,
            "release_merge": 6201.0, "release_refresh": 2000.0, "release_landed": 500.0,
            "release_terminal": 300.0, "unclassified": 3.0,
        })
        self.runtime.writer.comment.side_effect = self.board(100, {})
        outcome, card, _ = self.tick(self.assess())
        self.assertEqual(outcome["status"], "blocked")
        self.assertEqual(self.host.calls.count("merge pr"), 1)
        self.assertEqual(self.runtime.writer.create.call_count, 1)
        self.assertEqual(card["stages"], {
            "flush": 0.0, "flush_lock": 0.0, "flush_write": 100.0,
            "assessment": 450.0, "release_e2e": 0.0, "release_gate": 3400.0,
            "release_teardown": 300.0, "release_terminal": 1000.0, "unclassified": 3.0,
        })

    def test_an_automatic_release_shares_the_effect_sites(self):
        def automatic():
            with tick_stage("lifecycle"):
                return release_lifecycle.release_effect(
                    self.runtime, self.task(), self.record, self.records, self.payload, "attempt-1",
                    step="review", move_reason="review:green",
                )

        outcome, card, _ = self.tick(automatic)
        self.assertEqual(outcome["to"], "done")
        self.assertEqual(card["stages"], {
            "lifecycle": 0.0, "flush": 0.0, "flush_lock": 0.0, "flush_write": 100.0,
            "release_runtime": 300.0, "release_merge": 6201.0, "release_refresh": 2000.0,
            "release_landed": 500.0, "release_teardown": 1500.0, "release_terminal": 900.0,
            "unclassified": 3.0,
        })

    def test_a_release_without_a_candidate_reads_its_completion_evidence(self):
        with mock.patch.object(release_lifecycle, "missing_completion_evidence", return_value=""):
            outcome, card, _ = self.tick(self.assess("infra"))
        self.assertEqual(outcome["to"], "done")
        self.assertEqual(self.host.calls, ["teardown"])
        self.assertEqual(card["stages"], {
            "flush": 0.0, "flush_lock": 0.0, "flush_write": 50.0, "assessment": 450.0,
            "release_evidence": 150.0, "release_teardown": 1500.0, "release_terminal": 900.0,
            "unclassified": 3.0,
        })

    def test_a_release_outside_a_card_reads_no_clock(self):
        outcome = self.assess()()
        self.assertEqual(outcome["to"], "done")
        self.assertEqual(self.reads, 0)
