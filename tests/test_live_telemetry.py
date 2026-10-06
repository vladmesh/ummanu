"""Background-role telemetry read from the live data plane (secretary-833).

The incident: `python3 -m triggered_agents health` was RED with `no runs.jsonl yet` for curator,
steward and retro while all three were ticking fine — it looked for their state inside each
agent's git worktree, and the units write it under the installation's data dir. The pipeline line
and the steward's pipeline signal had the same shape of defect one layer down: both read the
pipeline worktree's `state/pipeline/runs.jsonl`, which the production dispatcher does not write,
so a failing production tick was invisible to both.

These tests pin all three ends: what the dispatcher records, what the readers make of it, and that
neither reader can be answered with a stale or missing source as if it were a healthy one.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from tests.fakes.dispatcher import FakeCatalog, FakeHost, dispatcher_seed
from tests.retired_board import legacy_runtime_lines
from tests.sql_backend_fixtures import card_store
from ummanu import host
from ummanu.automations.agents.steward import cli as steward_cli
from ummanu.automations.agents.steward import signals as steward_signals
from ummanu.automations.runtime import health, production_telemetry
from ummanu.board.backend import CARD, SPRINT
from ummanu.cli import build_parser
from ummanu.dispatch.bootstrap import default_data_dir, runtime_from_args
from ummanu.dispatch.observer import (
    STATE_PAUSE_STOP_PENDING,
    ObserverRecord,
    put_observers,
)
from ummanu.dispatch.production import (
    TICK_TELEMETRY_DEGRADATIONS_KEPT,
    TICK_TELEMETRY_ERRORS_KEPT,
    TICK_TELEMETRY_UNHEALTHY_KEPT,
    record_tick_telemetry,
)
from ummanu.dispatch.runtime import DispatcherRuntime
from ummanu.dispatch.watchdog import idle_stall_seconds
from ummanu.head_health import HeadHealth
from ummanu.head_registry import materialize_snapshot, record_source
from ummanu.runtime import role_env
from ummanu.runtime.state import PRECHECK_SKIP, AgentState
from ummanu.tasks import TaskError, TaskReader, TaskWriter, task_audit_for


class EmptyStewardReader:
    def active_cards(self, *, states=None, project=None):
        return []


EMPTY_STEWARD_READER = EmptyStewardReader()


def _ts(minutes_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat()


def _instance(root: Path, data_dir: Path) -> Path:
    """A valid instance dir whose data_dir is `data_dir`, as the dispatcher unit is started with.

    The packaged unit passes `--instance` and no `--data-dir`, so this file is the only thing that
    binds the dispatcher to a data plane — and therefore the only thing that can bind a reader in
    another process to the same one.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "instance.yaml").write_text(
        "version: 1\n"
        "name: telemetry-test\n"
        f"data_dir: {data_dir}\n"
        "offsite:\n"
        "  instance_remote: https://example.invalid/instance.git\n",
        encoding="utf-8",
    )
    return root


def _telemetry_state(path: Path, telemetry: dict | None, **payload) -> None:
    body = {"version": 1, "mode": "production", "phase": "production", **payload}
    if telemetry is not None:
        body["tick_telemetry"] = telemetry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body), encoding="utf-8")


def _tick(seq: int, *, healthy: bool, at: str | None = None, **fields) -> dict:
    return {
        "seq": seq,
        "at": at or _ts(1),
        "status": "ok" if healthy else "degraded",
        "step": "production-tick",
        "healthy": healthy,
        "reason": "",
        "actions": 0,
        "error_count": 0,
        "errors": [],
        **fields,
    }


class ProductionTickTelemetryTests(unittest.TestCase):
    """What the production dispatcher durably records about how its ticks ended."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.data_dir = Path(self.tmpdir.name)
        env = mock.patch.dict(
            os.environ,
            {
                "UMMANU_LEGACY_PAUSE_FILE": str(self.data_dir / "legacy-pause.json"),
                "UMMANU_DISPATCHER_BODY_DIR": str(self.data_dir / "bodies"),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        self.board = card_store(self, dispatcher_seed(), instance_dir=self.data_dir)
        self.reader = TaskReader(self.board)  # type: ignore[arg-type]
        self.writer = TaskWriter(self.board, data_dir=self.data_dir, workspace=self.data_dir)  # type: ignore[arg-type]
        self.catalog = FakeCatalog(instance_dir=self.data_dir)
        self.host = FakeHost(self.data_dir / "workspaces", self.catalog)
        self.host.audit = task_audit_for(self.board)
        self.runtime = DispatcherRuntime(
            self.reader,
            self.writer,
            task_audit_for(self.board),
            self.data_dir,
            self.catalog,  # type: ignore[arg-type]
            self.host,  # type: ignore[arg-type]
            owner="ummanu-pilot",
        )

    def read_through_the_agent_reader(self) -> production_telemetry.TickTelemetry:
        """The record as the health line and the steward actually see it.

        Reading it back through `ummanu.automations` rather than out of the payload is the point:
        writer and reader live in different processes on the host, and the file is the only
        contract between them.
        """
        with mock.patch.dict(
            os.environ,
            {"TA_PRODUCTION_STATE": str(self.runtime.production_state.path)},
        ):
            return production_telemetry.read()

    def test_healthy_tick_records_its_outcome_and_freshness(self) -> None:
        result = self.runtime.production_tick()
        self.assertEqual(result["status"], "ok")

        telemetry = self.read_through_the_agent_reader()

        self.assertTrue(telemetry.available)
        self.assertEqual(telemetry.tick_seq, 1)
        self.assertTrue(telemetry.last["healthy"])
        self.assertEqual(telemetry.last["status"], "ok")
        self.assertEqual(telemetry.last["step"], "production-tick")
        self.assertEqual(telemetry.last_healthy_at, telemetry.last["at"])
        self.assertEqual(telemetry.unhealthy, ())
        self.assertEqual(telemetry.unhealthy_total, 0)
        self.assertTrue(telemetry.generation)

        # The generation names this telemetry history and never moves while it continues: it is
        # what tells a reader's watermark that the counter it holds is still the same count.
        self.runtime.production_tick()
        self.assertEqual(self.read_through_the_agent_reader().generation, telemetry.generation)

    def test_every_terminal_tick_records_how_long_it_took(self) -> None:
        """The cost of a tick, beside the outcome it reached, for every outcome that is recorded.

        Added by secretary-1649, which is about making the pipeline's own performance measurable
        before anything is optimised: `record_tick_telemetry` already carried how a tick ended and
        said nothing about how long it ran, so a dispatcher minute that grew from one second to
        twenty left no trace anybody could read. The three cases below are the three terminal
        records `production_tick` can make — the working tick, the frozen one, and the one that
        died on an exception — and each of them has to carry its own number.
        """
        self.runtime.production_tick()
        healthy = self.read_through_the_agent_reader().last
        self.assertEqual(healthy["status"], "ok")
        self.assertIsInstance(healthy["duration_ms"], float)
        self.assertGreater(healthy["duration_ms"], 0.0)
        # A tick over a fake board is quick, but it is not free and it is not an hour.
        self.assertLess(healthy["duration_ms"], 60_000.0)

        self.runtime.pause_pipeline(mode="freeze", actor="operator", reason="host maintenance")
        self.runtime.production_tick()
        frozen = self.read_through_the_agent_reader().last
        self.assertEqual(frozen["status"], "skipped")
        self.assertGreater(frozen["duration_ms"], 0.0)
        self.runtime.resume_pipeline(actor="operator")

        with (
            mock.patch(
                "ummanu.dispatch.production._reconcile_production",
                side_effect=RuntimeError("host is gone"),
            ),
            self.assertRaises(RuntimeError),
        ):
            self.runtime.production_tick()
        failed = self.read_through_the_agent_reader().last
        self.assertEqual(failed["status"], "failed")
        self.assertGreater(failed["duration_ms"], 0.0)

    def test_a_record_folded_in_outside_a_tick_reports_no_duration_rather_than_zero(self) -> None:
        """There is no tick to time, and nought milliseconds would be a measurement nobody made."""
        payload: dict = {}
        record_tick_telemetry(payload, {"status": "ok", "step": "production-tick"})

        self.assertIsNone(payload["tick_telemetry"]["last"]["duration_ms"])

    def test_a_rebuilt_state_file_starts_a_new_telemetry_generation(self) -> None:
        self.runtime.production_tick()
        first = self.read_through_the_agent_reader().generation

        # The state file is replaced: a restore, a rebuilt installation. Its counters start again,
        # and may well land on numbers a reader's watermark already holds.
        self.runtime.production_state.path.unlink()
        self.runtime.production_tick()

        self.assertNotEqual(self.read_through_the_agent_reader().generation, first)

    def test_degraded_tick_records_the_diagnostic_and_leaves_the_healthy_stamp_behind(self) -> None:
        self.runtime.production_tick()
        healthy_at = self.read_through_the_agent_reader().last_healthy_at

        with mock.patch.object(self.runtime, "_tick_task", side_effect=RuntimeError("board is gone")):
            result = self.runtime.production_tick()
        self.assertEqual(result["status"], "degraded")

        telemetry = self.read_through_the_agent_reader()
        self.assertFalse(telemetry.last["healthy"])
        self.assertEqual(telemetry.last["status"], "degraded")
        self.assertEqual(telemetry.last["error_count"], 1)
        self.assertEqual(telemetry.last["errors"][0]["code"], "unexpected_error")
        self.assertEqual(telemetry.unhealthy_total, 1)
        self.assertEqual(telemetry.unhealthy[-1]["seq"], telemetry.last["seq"])
        # The healthy stamp is not advanced by a failing tick, and not erased by it either: it is
        # the freshness evidence, not a verdict on the current tick.
        self.assertEqual(telemetry.last_healthy_at, healthy_at)

    def degraded_reconcile(self, reason: str = "the head of an unresolved launch could not be stopped"):
        """A reconciliation pass that returns a degraded outcome and raises nothing.

        The real shape of `launch-intent-stop-unconfirmed` (_reconcile_production): the operation
        failed, the outcome says so, and `errors` stays empty — the tick that reported itself `ok`
        over exactly this is the round-3 blocker.
        """
        return mock.patch(
            "ummanu.dispatch.production._reconcile_production",
            return_value=[
                {
                    "status": "degraded",
                    "step": "production-reconcile",
                    "ref": "ummanu-900",
                    "action": "launch-intent-stop-unconfirmed",
                    "reason": reason,
                }
            ],
        )

    def test_a_degraded_action_makes_the_tick_degraded_and_is_never_recorded_healthy(self) -> None:
        self.runtime.production_tick()
        healthy_at = self.read_through_the_agent_reader().last_healthy_at

        with self.degraded_reconcile():
            result = self.runtime.production_tick()

        self.assertEqual(result["status"], "degraded")
        telemetry = self.read_through_the_agent_reader()
        self.assertFalse(telemetry.last["healthy"])
        self.assertEqual(telemetry.last["error_count"], 0)
        self.assertEqual(telemetry.last["degraded_count"], 1)
        self.assertEqual(
            telemetry.last["degradations"][0],
            {
                "ref": "ummanu-900",
                "step": "production-reconcile",
                "status": "degraded",
                "action": "launch-intent-stop-unconfirmed",
                "reason": "the head of an unresolved launch could not be stopped",
            },
        )
        self.assertEqual(telemetry.unhealthy_total, 1)
        self.assertEqual(telemetry.last_healthy_at, healthy_at)

        with mock.patch.dict(os.environ, {"TA_PRODUCTION_STATE": str(self.runtime.production_state.path)}):
            problems, _ = health._pipeline_status()
        self.assertTrue(any("last tick unhealthy" in problem for problem in problems))
        self.assertTrue(any("launch-intent-stop-unconfirmed" in problem for problem in problems))

    def test_a_degraded_action_reaches_the_steward_once_and_survives_a_healthy_tick(self) -> None:
        """End to end, on the real state file: dispatcher → durable record → steward gate.

        The intervening healthy tick is the point. Dedup is keyed on the incident counters, so an
        ordinary production tick between the failure and the steward's look must not consume the
        incident — it only adds the recovery that closed it — and `advance` — the steward having
        actually looked — is the only thing that does.
        """
        state = AgentState("steward", state_dir=self.data_dir / "steward")
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                mock.patch.dict(os.environ, {"TA_PRODUCTION_STATE": str(self.runtime.production_state.path)})
            )
            stack.enter_context(mock.patch.object(steward_signals, "STATE", state))
            stack.enter_context(mock.patch.object(steward_cli, "STATE", state))
            stack.enter_context(
                mock.patch.object(steward_signals, "resolve_reader", return_value=EMPTY_STEWARD_READER)
            )
            stack.enter_context(
                mock.patch.object(steward_signals, "_workspaces_root", return_value=self.data_dir / "no-workspaces")
            )
            stack.enter_context(
                mock.patch.dict(
                    os.environ,
                    {"TA_PRODUCTION_RESOURCE_HEALTH": str(self.data_dir / "no-resource-health.json")},
                )
            )
            stack.enter_context(mock.patch("sys.stdout", new=io.StringIO()))
            stack.enter_context(mock.patch("sys.stderr", new=io.StringIO()))

            self.runtime.production_tick()
            self.assertEqual(steward_cli.cmd_precheck(), PRECHECK_SKIP)

            with self.degraded_reconcile():
                self.runtime.production_tick()

            self.assertEqual(steward_cli.cmd_precheck(), 0)
            hits = steward_signals.scan()["signals"]["pipeline_ticks"]
            self.assertEqual([hit["event"] for hit in hits], ["pipeline-tick-unhealthy"])
            self.assertEqual(hits[0]["degradations"][0]["action"], "launch-intent-stop-unconfirmed")

            self.runtime.production_tick()
            self.assertEqual(steward_cli.cmd_precheck(), 0)
            after = steward_signals.scan()["signals"]["pipeline_ticks"]
            self.assertEqual(
                [hit["event"] for hit in after], ["pipeline-tick-unhealthy", "pipeline-tick-recovered"]
            )
            self.assertEqual({hit["incident"] for hit in after}, {hits[0]["incident"]})

            self.assertEqual(steward_cli.cmd_scan(True), 0)
            self.assertEqual(steward_cli.cmd_advance(), 0)
            self.assertEqual(steward_cli.cmd_precheck(), PRECHECK_SKIP)

    def test_an_idle_worker_bounce_is_a_degraded_tick(self) -> None:
        """secretary-1063: a head that is alive, idle and has delivered nothing for the round the
        dispatcher is waiting on is the pipeline failing to move a card. The bounce that retries it
        has to reach the operator as degradation, or the telemetry reads green right up to the
        Blocked and the one signal before it is lost.

        S1-4: the idle fence is gone; the same story is told by the verdict ladder -- a
        confirmed stall prompts first (degraded), then reclaims (degraded)."""
        ref = "ummanu-510"
        self.runtime.production_tick()  # claims the card and launches its worker
        self.host.worker_status_result = {
            "known": True,
            "live": True,
            "reason": "live",
            "last_activity": time.time(),
            "pid_confirmed": True,
            "idle": True,
        }
        self.runtime.production_tick()  # baseline: a quiet head below every threshold
        self._age_worker_episode(seconds=idle_stall_seconds() + 60)
        pending = self.runtime.production_tick()  # quiet past suspect: one nudge

        self.assertEqual(pending["status"], "degraded")
        self.assertEqual(
            [action["action"] for action in pending["actions"] if action.get("pilot_ref") == ref],
            ["worker-stall-suspected"],
        )
        self._age_worker_episode(seconds=idle_stall_seconds() * 4)
        result = self.runtime.production_tick()  # quiet past confirm: reclaimed

        self.assertEqual(result["status"], "degraded")
        self.assertEqual(
            [action["action"] for action in result["actions"] if action.get("pilot_ref") == ref],
            ["worker-respawned"],
        )
        telemetry = self.read_through_the_agent_reader()
        self.assertFalse(telemetry.last["healthy"])
        self.assertEqual(telemetry.last["degraded_count"], 1)
        degradation = telemetry.last["degradations"][0]
        self.assertEqual(
            (degradation["ref"], degradation["step"], degradation["action"]),
            (ref, "advance", "worker-respawned"),
        )
        self.assertIn("generation 1", degradation["reason"])

        with mock.patch.dict(os.environ, {"TA_PRODUCTION_STATE": str(self.runtime.production_state.path)}):
            problems, _ = health._pipeline_status()
        self.assertTrue(any("worker-respawned" in problem for problem in problems))

    def _age_worker_episode(self, seconds: float) -> None:
        """Move the persisted episode's quiet reference back, as operator clock-rewind."""
        payload = self.runtime.production_state.load()
        episode = dict(payload["records"]["ummanu-510"]["worker_vitality_episode"] or {})
        for name in ("started_at", "updated_at"):
            if episode.get(name):
                episode[name] -= seconds
        payload["records"]["ummanu-510"]["worker_vitality_episode"] = episode
        self.runtime.production_state.save(payload)

    def test_a_blocked_card_is_the_dispatcher_working_not_a_degraded_tick(self) -> None:
        """A card parked in Blocked keeps the tick healthy, on purpose.

        The board carries the reason and the steward reports it as a `new_blocked` signal; the
        tick's exit code is the production unit's result, so a correctly blocked card must not
        fail the unit or redden the pipeline line. Only an operation the dispatcher could not
        finish does that.
        """
        with mock.patch(
            "ummanu.dispatch.production._reconcile_production",
            return_value=[
                {
                    "status": "blocked",
                    "step": "production-recovery",
                    "ref": "ummanu-901",
                    "reason": "active task claim no longer matches production record",
                }
            ],
        ):
            result = self.runtime.production_tick()

        self.assertEqual(result["status"], "ok")
        telemetry = self.read_through_the_agent_reader()
        self.assertTrue(telemetry.last["healthy"])
        self.assertEqual(telemetry.last["degraded_count"], 0)
        self.assertEqual(telemetry.unhealthy_total, 0)

    def test_a_tick_that_dies_before_its_own_save_is_recorded_as_a_failure(self) -> None:
        """The board outage case: the first board read raises and the tick never finishes.

        Nothing inside the tick catches it, so without a record of its own the previous healthy
        tick would answer for the pipeline for the whole freshness window while no card moves.
        """
        self.runtime.production_tick()
        healthy = self.read_through_the_agent_reader()

        with (
            mock.patch(
                "ummanu.dispatch.production._production_tasks",
                side_effect=TaskError("backend_unavailable", "board is down", 1),
            ),
            self.assertRaises(TaskError),
        ):
            self.runtime.production_tick()

        telemetry = self.read_through_the_agent_reader()
        self.assertFalse(telemetry.last["healthy"])
        self.assertEqual(telemetry.last["status"], "failed")
        self.assertEqual(telemetry.last["errors"][0]["code"], "backend_unavailable")
        self.assertEqual(telemetry.unhealthy_total, 1)
        self.assertEqual(telemetry.unhealthy[-1]["seq"], telemetry.last["seq"])
        self.assertEqual(telemetry.last_healthy_at, healthy.last_healthy_at)
        # ...and it opens the incident, carrying the code that tells a board outage from a bug.
        self.assertEqual(telemetry.incident_total, 1)
        self.assertEqual(telemetry.incident["opened"]["errors"][0]["code"], "backend_unavailable")
        self.assertEqual(telemetry.recovery, {})

        with mock.patch.dict(os.environ, {"TA_PRODUCTION_STATE": str(self.runtime.production_state.path)}):
            problems, _ = health._pipeline_status()
        self.assertTrue(any("last tick unhealthy" in problem for problem in problems))
        self.assertTrue(any("backend_unavailable" in problem for problem in problems))

    def test_a_board_outage_is_one_incident_and_one_recovery_end_to_end(self) -> None:
        """The whole path on the real state file, without touching the live board (secretary-839).

        A board outage fails every tick for as long as it lasts and then stops failing. What has to
        reach the steward is one incident with its cause, one fact that it is over, and a fresh
        incident for the next independent failure — not a per-tick stream with no ending.
        """
        state = AgentState("steward", state_dir=self.data_dir / "steward")
        outage = mock.patch(
            "ummanu.dispatch.production._production_tasks",
            side_effect=TaskError("backend_unavailable", "board is down", 1),
        )
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                mock.patch.dict(os.environ, {"TA_PRODUCTION_STATE": str(self.runtime.production_state.path)})
            )
            stack.enter_context(mock.patch.object(steward_signals, "STATE", state))
            stack.enter_context(mock.patch.object(steward_cli, "STATE", state))
            stack.enter_context(
                mock.patch.object(steward_signals, "resolve_reader", return_value=EMPTY_STEWARD_READER)
            )
            stack.enter_context(
                mock.patch.object(steward_signals, "_workspaces_root", return_value=self.data_dir / "no-workspaces")
            )
            stack.enter_context(
                mock.patch.dict(
                    os.environ,
                    {"TA_PRODUCTION_RESOURCE_HEALTH": str(self.data_dir / "no-resource-health.json")},
                )
            )
            stack.enter_context(mock.patch("sys.stdout", new=io.StringIO()))
            stack.enter_context(mock.patch("sys.stderr", new=io.StringIO()))

            self.runtime.production_tick()
            self.assertEqual(steward_cli.cmd_precheck(), PRECHECK_SKIP)

            # The outage: two ticks die before their own save, on the very first board read.
            for _ in range(2):
                with outage, self.assertRaises(TaskError):
                    self.runtime.production_tick()

            self.assertEqual(steward_cli.cmd_precheck(), 0)
            hits = steward_signals.scan()["signals"]["pipeline_ticks"]
            self.assertEqual([hit["event"] for hit in hits], ["pipeline-tick-unhealthy"])
            self.assertEqual(hits[0]["errors"][0]["code"], "backend_unavailable")
            self.assertEqual(hits[0]["unhealthy_ticks"], 2)
            incident = hits[0]["incident"]
            # A repeated precheck before `advance` sees the same one incident, not another.
            self.assertEqual(steward_cli.cmd_precheck(), 0)
            self.assertEqual(
                [hit["incident"] for hit in steward_signals.scan()["signals"]["pipeline_ticks"]], [incident]
            )
            self.assertEqual(steward_cli.cmd_scan(True), 0)
            self.assertEqual(steward_cli.cmd_advance(), 0)
            self.assertEqual(steward_cli.cmd_precheck(), PRECHECK_SKIP)

            # The board comes back. One recovery, tied to the incident and its cause.
            self.runtime.production_tick()
            self.assertEqual(steward_cli.cmd_precheck(), 0)
            recovered = steward_signals.scan()["signals"]["pipeline_ticks"]
            self.assertEqual([hit["event"] for hit in recovered], ["pipeline-tick-recovered"])
            self.assertEqual(recovered[0]["incident"], incident)
            self.assertEqual(recovered[0]["unhealthy_ticks"], 2)
            self.assertIn("backend_unavailable", recovered[0]["cause"])
            self.assertEqual(steward_cli.cmd_scan(True), 0)
            self.assertEqual(steward_cli.cmd_advance(), 0)

            # Healthy ticks after it repeat nothing, and health is green on the same records.
            self.runtime.production_tick()
            self.assertEqual(steward_cli.cmd_precheck(), PRECHECK_SKIP)
            problems, _ = health._pipeline_status()
            self.assertEqual(problems, [])

            # A later independent failure is a new incident, and wakes the steward once.
            with outage, self.assertRaises(TaskError):
                self.runtime.production_tick()
            self.assertEqual(steward_cli.cmd_precheck(), 0)
            reopened = steward_signals.scan()["signals"]["pipeline_ticks"]
            self.assertEqual([hit["event"] for hit in reopened], ["pipeline-tick-unhealthy"])
            self.assertNotEqual(reopened[0]["incident"], incident)

    def test_a_failed_tick_records_telemetry_without_persisting_its_half_done_work(self) -> None:
        """Only the record of the failure lands: the payload it died holding is half applied."""
        self.runtime.production_tick()
        before = json.loads(self.runtime.production_state.path.read_text(encoding="utf-8"))

        with (
            mock.patch(
                "ummanu.dispatch.production._reconcile_production",
                side_effect=RuntimeError("host is gone"),
            ),
            self.assertRaises(RuntimeError),
        ):
            self.runtime.production_tick()

        after = json.loads(self.runtime.production_state.path.read_text(encoding="utf-8"))
        self.assertEqual(
            {key: value for key, value in after.items() if key != "tick_telemetry"},
            {key: value for key, value in before.items() if key != "tick_telemetry"},
        )
        self.assertEqual(self.read_through_the_agent_reader().last["status"], "failed")

    def test_telemetry_is_found_through_the_instance_the_unit_passes(self) -> None:
        """No TA_PRODUCTION_STATE, no UMMANU_DATA_DIR: only `--instance`, as the unit runs it."""
        instance = _instance(self.data_dir / "instance", self.data_dir)
        self.runtime.production_tick()

        with mock.patch.dict(os.environ, {"UMMANU_INSTANCE": str(instance)}, clear=False):
            os.environ.pop("TA_PRODUCTION_STATE", None)
            os.environ.pop("UMMANU_DATA_DIR", None)
            telemetry = production_telemetry.read()

        self.assertEqual(telemetry.path, self.runtime.production_state.path)
        self.assertTrue(telemetry.available)
        self.assertTrue(telemetry.last["healthy"])

    def test_frozen_tick_is_recorded_as_a_healthy_terminal_tick(self) -> None:
        self.runtime.pause_pipeline(mode="freeze", actor="operator", reason="host maintenance")

        result = self.runtime.production_tick()
        self.assertEqual(result["status"], "skipped")

        telemetry = self.read_through_the_agent_reader()
        self.assertTrue(telemetry.last["healthy"])
        self.assertEqual(telemetry.last["reason"], "pipeline is frozen by pause")
        self.assertTrue(telemetry.last_healthy_at)

    def plant_pending_observer_stop(self, ref: str = "sprint:1") -> None:
        """A head the freeze asked the host to close and the host refused, left on the books.

        The shape `_mark_stop_pending` leaves behind: the handle is still ours, so the frozen tick
        retries the stop instead of walking away from a live terminal.
        """
        payload = self.runtime.production_state.load()
        put_observers(
            payload,
            {
                ref: ObserverRecord(
                    sprint=ref,
                    head="observer",
                    workspace=str(self.data_dir / "workspaces" / "observer"),
                    handle=f"observer:{ref}",
                    launches=1,
                    head_possible=True,
                    state=STATE_PAUSE_STOP_PENDING,
                    stopped_reason="pipeline frozen: host maintenance",
                )
            },
        )
        self.runtime.production_state.save(payload)

    def test_a_freeze_that_cannot_stop_a_head_is_not_a_healthy_terminal_tick(self) -> None:
        """The frozen tick's own retry is an action like any other, and a refused one reddens it.

        A freeze is a deliberate stop and records healthy — but not while it is sitting on a head
        the host would not close. That tick used to be stored with the plain `skipped` status, so
        health read OK, the steward got no signal, and the one place the failure was written down
        was a field nobody reads (secretary-833 review, round 4).
        """
        self.runtime.pause_pipeline(mode="freeze", actor="operator", reason="host maintenance")
        self.plant_pending_observer_stop()
        self.host.fail_stop_observer_reason = "orca refused to close the pane"

        result = self.runtime.production_tick()

        self.assertEqual(result["status"], "degraded")
        self.assertEqual([row["action"] for row in result["observer_stops"]], ["observer-stop-failed"])

        telemetry = self.read_through_the_agent_reader()
        self.assertFalse(telemetry.last["healthy"])
        self.assertEqual(telemetry.last["degraded_count"], 1)
        degradation = telemetry.last["degradations"][0]
        self.assertEqual(degradation["action"], "observer-stop-failed")
        self.assertEqual(degradation["ref"], "sprint:1")
        self.assertIn("host maintenance", degradation["reason"])
        self.assertEqual(telemetry.unhealthy_total, 1)
        self.assertFalse(telemetry.last_healthy_at)

        with mock.patch.dict(os.environ, {"TA_PRODUCTION_STATE": str(self.runtime.production_state.path)}):
            problems, _ = health._pipeline_status()
        self.assertTrue(any("last tick unhealthy" in problem for problem in problems))
        self.assertTrue(any("observer-stop-failed" in problem for problem in problems))

        # ...and the steward hears about it, on the same records.
        state = AgentState("steward", state_dir=self.data_dir / "steward")
        state.save_watermark(
            dict(
                steward_signals._empty_watermark(),
                pipeline_incident_total=0,
                pipeline_recovery_total=0,
                pipeline_telemetry_generation=telemetry.generation,
            )
        )
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                mock.patch.dict(os.environ, {"TA_PRODUCTION_STATE": str(self.runtime.production_state.path)})
            )
            stack.enter_context(mock.patch.object(steward_signals, "STATE", state))
            hits, _ = steward_signals._pipeline_tick_signals(state.load_watermark())
        self.assertEqual([hit["event"] for hit in hits], ["pipeline-tick-unhealthy"])
        self.assertEqual(hits[0]["degradations"][0]["action"], "observer-stop-failed")

    def test_a_freeze_that_stops_its_pending_head_stays_healthy(self) -> None:
        """The other half of the rule: the retry that worked leaves the freeze a healthy tick."""
        self.runtime.pause_pipeline(mode="freeze", actor="operator", reason="host maintenance")
        self.plant_pending_observer_stop()

        result = self.runtime.production_tick()

        self.assertEqual(result["status"], "skipped")
        self.assertEqual([row["action"] for row in result["observer_stops"]], ["observer-stopped-by-pause"])
        telemetry = self.read_through_the_agent_reader()
        self.assertTrue(telemetry.last["healthy"])
        self.assertEqual(telemetry.unhealthy_total, 0)

    def test_guard_blocked_tick_writes_nothing(self) -> None:
        """A dispatcher that is not allowed to touch its own state must not touch it here either.

        The evidence of a blocked dispatcher is that it stops producing healthy ticks; writing
        across the ownership fence to say so would be worse than the silence.
        """
        self.runtime.production_state.save(
            {
                "version": 1,
                "mode": "production",
                "phase": "production",
                "owner": "another-dispatcher",
                "records": {},
            }
        )

        result = self.runtime.production_tick()

        self.assertEqual(result["status"], "blocked")
        self.assertFalse(self.read_through_the_agent_reader().available)

    def test_unhealthy_ring_is_bounded_while_the_counter_keeps_growing(self) -> None:
        payload: dict = {}
        for _ in range(TICK_TELEMETRY_UNHEALTHY_KEPT + 5):
            record_tick_telemetry(
                payload,
                {
                    "status": "degraded",
                    "step": "production-tick",
                    "errors": [{"ref": "x", "code": "boom", "message": ""}],
                },
            )

        telemetry = payload["tick_telemetry"]
        self.assertEqual(len(telemetry["unhealthy"]), TICK_TELEMETRY_UNHEALTHY_KEPT)
        self.assertEqual(telemetry["unhealthy_total"], TICK_TELEMETRY_UNHEALTHY_KEPT + 5)
        self.assertEqual(telemetry["unhealthy"][-1]["seq"], TICK_TELEMETRY_UNHEALTHY_KEPT + 5)

    def test_errors_recorded_per_tick_are_capped_but_counted_in_full(self) -> None:
        payload: dict = {}
        errors = [{"ref": f"ref-{i}", "code": "boom", "message": "m"} for i in range(9)]

        record_tick_telemetry(payload, {"status": "degraded", "step": "production-tick", "errors": errors})

        entry = payload["tick_telemetry"]["last"]
        self.assertEqual(entry["error_count"], 9)
        self.assertLess(len(entry["errors"]), 9)
        self.assertIn("+", production_telemetry.describe(entry))

    def test_degraded_actions_recorded_per_tick_are_capped_but_counted_in_full(self) -> None:
        payload: dict = {}
        actions = [
            {
                "status": "degraded",
                "step": "production-reconcile",
                "ref": f"ref-{i}",
                "action": "launch-intent-stop-unconfirmed",
            }
            for i in range(9)
        ]

        record_tick_telemetry(payload, {"status": "ok", "step": "production-tick", "actions": actions})

        entry = payload["tick_telemetry"]["last"]
        # A result built as `ok` over degraded actions is still recorded unhealthy: the entry is
        # the durable evidence, and it must not be able to disagree with what happened.
        self.assertFalse(entry["healthy"])
        self.assertEqual(entry["degraded_count"], 9)
        self.assertEqual(len(entry["degradations"]), TICK_TELEMETRY_DEGRADATIONS_KEPT)
        self.assertIn("+4 more degraded action(s)", production_telemetry.describe(entry))


class ProductionStatePathTests(unittest.TestCase):
    """Where the reader looks for the dispatcher's records, on a host that is not the default one."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.data_dir = self.root / "srv" / "ummanu-data"
        self.instance = _instance(self.root / "instance", self.data_dir)
        env = mock.patch.dict(os.environ, {"HOME": str(self.root / "home")})
        env.start()
        self.addCleanup(env.stop)
        for name in (
            "TA_PRODUCTION_STATE",
            "UMMANU_DATA_DIR",
            "UMMANU_INSTANCE",
            "TA_RUNTIME_ENV_FILE",
        ):
            os.environ.pop(name, None)

    def test_instance_data_dir_is_the_dispatchers_own(self) -> None:
        os.environ["UMMANU_INSTANCE"] = str(self.instance)

        # default_data_dir is what `ummanu dispatcher production-tick --instance ...` resolves
        # with no --data-dir: the reader must land on the same directory, not on a home default.
        self.assertEqual(production_telemetry.data_dir(), default_data_dir(self.instance))
        self.assertEqual(
            production_telemetry.state_path(),
            self.data_dir / "dispatcher" / "production-state.json",
        )

    def test_a_relative_instance_data_dir_resolves_against_the_instance(self) -> None:
        (self.instance / "instance.yaml").write_text(
            "version: 1\nname: telemetry-test\ndata_dir: ./data\n"
            "offsite:\n  instance_remote: https://example.invalid/instance.git\n",
            encoding="utf-8",
        )
        os.environ["UMMANU_INSTANCE"] = str(self.instance)

        self.assertEqual(production_telemetry.data_dir(), self.instance / "data")

    def test_explicit_env_overrides_win_in_the_dispatchers_order(self) -> None:
        os.environ["UMMANU_INSTANCE"] = str(self.instance)
        os.environ["UMMANU_DATA_DIR"] = str(self.root / "elsewhere")
        self.assertEqual(production_telemetry.data_dir(), self.root / "elsewhere")

        os.environ["TA_PRODUCTION_STATE"] = str(self.root / "state.json")
        self.assertEqual(production_telemetry.state_path(), self.root / "state.json")

    def test_an_unusable_instance_falls_back_to_the_home_default(self) -> None:
        """A broken or absent instance file must not crash the command that reports host trouble."""
        os.environ["UMMANU_INSTANCE"] = str(self.root / "missing")
        self.assertEqual(production_telemetry.data_dir(), self.root / "home" / "ummanu-data")

        (self.instance / "instance.yaml").write_text("data_dir: [not, a, path\n", encoding="utf-8")
        os.environ["UMMANU_INSTANCE"] = str(self.instance)
        self.assertEqual(production_telemetry.data_dir(), self.root / "home" / "ummanu-data")

    def test_a_missing_record_names_the_instance_resolved_path(self) -> None:
        os.environ["UMMANU_INSTANCE"] = str(self.instance)

        telemetry = production_telemetry.read()

        self.assertEqual(telemetry.unavailable, "production-state-missing")
        self.assertEqual(telemetry.path, self.data_dir / "dispatcher" / "production-state.json")

    def test_the_role_units_env_file_path_carries_the_instance(self) -> None:
        """A role process handed only TA_RUNTIME_ENV_FILE still lands on the dispatcher's file."""
        os.environ["TA_RUNTIME_ENV_FILE"] = str(self.instance / "runtime.env")

        self.assertEqual(production_telemetry.data_dir(), self.data_dir)

        # An explicit instance still wins over the env-file's directory.
        other = _instance(self.root / "other-instance", self.root / "other-data")
        os.environ["UMMANU_INSTANCE"] = str(other)
        self.assertEqual(production_telemetry.data_dir(), self.root / "other-data")


class EnvDataDirConflictTests(unittest.TestCase):
    """UMMANU_DATA_DIR disagreeing with the instance must move writer and readers together.

    An installation (or a drop-in) can put UMMANU_DATA_DIR in runtime.env, which the dispatcher
    unit imports. If only the readers honored it, health and `steward scan` would report on a file
    nobody writes and call the silence healthy — the same blindness this card exists to end
    (secretary-833 review, round 3). So the rule is one rule: the dispatcher parser defaults
    --data-dir to that variable, exactly as `ummanu task` does.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.env_data = self.root / "env-data"
        self.instance = _instance(self.root / "instance", self.root / "instance-data")
        # A real instance the dispatcher will accept, so the writer path is resolved by the same
        # code the unit runs, not by a stub standing in for it.
        materialize_snapshot(self.instance, Path(__file__).resolve().parents[1])
        record_source(self.instance, Path(__file__).resolve().parents[1])
        env = mock.patch.dict(
            os.environ,
            {"UMMANU_INSTANCE": str(self.instance), "UMMANU_DATA_DIR": str(self.env_data)},
        )
        env.start()
        self.addCleanup(env.stop)
        for name in ("TA_PRODUCTION_STATE", "TA_RUNTIME_ENV_FILE"):
            os.environ.pop(name, None)
        self.state = AgentState("steward", state_dir=self.root / "steward")
        state = mock.patch.object(steward_signals, "STATE", self.state)
        state.start()
        self.addCleanup(state.stop)

    def writer_state_path(self) -> Path:
        """Where the packaged unit's own command line lands, parsed by the real CLI parser."""
        args = build_parser().parse_args(["dispatcher", "production-tick", "--instance", str(self.instance)])
        with mock.patch("ummanu.dispatch.bootstrap.board_client") as selected:
            runtime = runtime_from_args(
                args.instance,
                args.data_dir,
                host_mode="noop",
                owner="ummanu-production",
            )
        selected.assert_called_once_with(self.instance, serves=(CARD, SPRINT))
        return runtime.production_state.path

    def test_writer_health_and_steward_all_land_on_the_env_data_plane(self) -> None:
        writer = self.writer_state_path()
        self.assertEqual(writer, self.env_data / "dispatcher" / "production-state.json")
        self.assertEqual(production_telemetry.state_path(), writer)

        # A degraded tick written where the dispatcher writes must reach both readers. Left in the
        # instance's data dir it would be invisible to them, and health would stay green.
        payload: dict = {}
        record_tick_telemetry(
            payload,
            {
                "status": "degraded",
                "step": "production-tick",
                "errors": [{"ref": "ummanu-1", "code": "boom", "message": "host is gone"}],
            },
        )
        _telemetry_state(writer, payload["tick_telemetry"])

        problems, detail = health._pipeline_status()
        self.assertTrue(problems)
        self.assertIn("boom", " ".join(problems))
        self.assertIn("last tick", detail)

        hits, pending = steward_signals._pipeline_tick_signals(
            dict(steward_signals._empty_watermark(), pipeline_incident_total=0, pipeline_recovery_total=0)
        )
        self.assertEqual([hit["event"] for hit in hits], ["pipeline-tick-unhealthy"])
        self.assertEqual(pending["pipeline_incident_total"], 1)

    def test_a_record_left_on_the_instance_data_plane_is_not_read_as_healthy(self) -> None:
        """Proof the two directories really are different: the readers do not silently find it."""
        _telemetry_state(
            self.root / "instance-data" / "dispatcher" / "production-state.json",
            {
                "tick_seq": 3,
                "last": _tick(3, healthy=True),
                "last_healthy_at": _ts(1),
                "unhealthy": [],
                "unhealthy_total": 0,
            },
        )

        self.assertEqual(production_telemetry.read().unavailable, "production-state-missing")
        problems, _ = health._pipeline_status()
        self.assertTrue(problems)


class PackagedStewardUnitEnvTests(unittest.TestCase):
    """The env the packaged steward units actually give the process that reads production state.

    Setting UMMANU_INSTANCE in a test proves the reader, not the installation: the steward runs
    with whatever its rendered unit exports, through role_env's allowlist. So this renders the
    shipped templates and builds the role env exactly that way (secretary-833 review, round 2).
    """

    UNITS = ("ummanu-steward.service", "ummanu-steward-deep-sweep.service")

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.data_dir = self.root / "srv" / "ummanu-data"
        self.instance = _instance(self.root / "instance", self.data_dir)
        # The live runtime.env carries leftover board lines and no instance path — the case the
        # reviewer found: role_env has nothing to forward unless the unit itself exports it.
        (self.instance / "runtime.env").write_text(legacy_runtime_lines(), encoding="utf-8")
        self.layout = host.SystemdLayout(
            product_root=self.root / "product",
            instance_path=self.instance,
            data_dir=self.data_dir,
            runtime_user="dev",
            runtime_home=self.root / "home",
        )

    def unit_env(self, name: str) -> dict[str, str]:
        rendered = host.render_systemd_unit(
            (host.SHIPPED_PACKAGING_ROOT / name).read_bytes(), self.layout
        ).decode()
        env = {}
        for line in rendered.splitlines():
            if line.startswith("Environment="):
                key, value = line[len("Environment=") :].split("=", 1)
                env[key] = value
        return env

    def test_the_steward_process_resolves_the_installations_production_state(self) -> None:
        for name in self.UNITS:
            with self.subTest(unit=name):
                env = role_env.runtime_env(
                    "steward",
                    base_env=self.unit_env(name),
                    env_file=self.instance / "runtime.env",
                )

                self.assertEqual(env["UMMANU_INSTANCE"], str(self.instance))
                with mock.patch.dict(os.environ, env, clear=True):
                    self.assertEqual(
                        production_telemetry.state_path(),
                        self.data_dir / "dispatcher" / "production-state.json",
                    )

    def test_a_data_dir_in_runtime_env_reaches_the_steward_process_too(self) -> None:
        """The dispatcher unit imports runtime.env wholesale; the steward gets it through role_env.

        Setting UMMANU_DATA_DIR inside the reader's own process would prove nothing: on the live
        host the variable arrives from that file, and the role allowlist decides whether it
        survives. Stripped, the steward resolves the instance's data_dir while the dispatcher
        writes to the env one, and every production signal it reads comes off a file nobody writes
        (secretary-833 review, round 3).
        """
        env_data = self.root / "env-data"
        (self.instance / "runtime.env").write_text(
            legacy_runtime_lines() + f"UMMANU_DATA_DIR={env_data}\n",
            encoding="utf-8",
        )
        # What the dispatcher unit's own EnvironmentFile gives its process, resolved by the real
        # CLI parser: the writer side of the same binding.
        writer_env = role_env.load_env_file(self.instance / "runtime.env")
        with mock.patch.dict(os.environ, writer_env, clear=False):
            writer_args = build_parser().parse_args(
                ["dispatcher", "production-tick", "--instance", str(self.instance)]
            )
        self.assertEqual(writer_args.data_dir, str(env_data))

        for name in self.UNITS:
            with self.subTest(unit=name):
                env = role_env.runtime_env(
                    "steward",
                    base_env=self.unit_env(name),
                    env_file=self.instance / "runtime.env",
                )

                self.assertEqual(env["UMMANU_DATA_DIR"], str(env_data))
                with mock.patch.dict(os.environ, env, clear=True):
                    self.assertEqual(
                        production_telemetry.state_path(),
                        env_data / "dispatcher" / "production-state.json",
                    )
                    self.assertEqual(
                        production_telemetry.resource_health_path(),
                        env_data / "dispatcher" / "resource_health.json",
                    )


class StewardResourceSignalTests(unittest.TestCase):
    """`resource_flip` reads the cache the running production dispatcher writes.

    The dispatcher probes resources through `ummanu.head_health.HeadHealth`, which caches under
    `<data_dir>/dispatcher/resource_health.json`. The pipeline worktree holds a same-named file the
    legacy agent path wrote; reading that one leaves the steward blind to a flip the current
    dispatcher just saw (secretary-833 review, round 3).
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.data_dir = self.root / "srv" / "ummanu-data"
        self.instance = _instance(self.root / "instance", self.data_dir)
        env = mock.patch.dict(os.environ, {"UMMANU_INSTANCE": str(self.instance)})
        env.start()
        self.addCleanup(env.stop)
        for name in (
            "TA_PRODUCTION_RESOURCE_HEALTH",
            "UMMANU_DATA_DIR",
            "TA_RUNTIME_ENV_FILE",
            "TA_PIPELINE_STATE_DIR",
        ):
            os.environ.pop(name, None)
        workspaces = mock.patch.object(steward_signals, "_workspaces_root", return_value=self.root / "workspaces")
        workspaces.start()
        self.addCleanup(workspaces.stop)

    def write_production(self, statuses: dict[str, str]) -> None:
        path = HeadHealth(None, self.data_dir).path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    rid: {
                        "resource": rid,
                        "status": status,
                        "reason": "probe",
                        "checked_at": 1.0,
                        "cached": False,
                    }
                    for rid, status in statuses.items()
                }
            ),
            encoding="utf-8",
        )

    def write_legacy_worktree(self, statuses: dict[str, str]) -> None:
        """The pipeline worktree copy, which must not answer for the production dispatcher."""
        path = (
            self.root
            / "workspaces"
            / "ummanu"
            / "pipeline"
            / "state"
            / "pipeline"
            / "resource_health.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({rid: {"status": status} for rid, status in statuses.items()}), encoding="utf-8"
        )

    def test_the_reader_lands_where_the_dispatcher_caches(self) -> None:
        self.assertEqual(production_telemetry.resource_health_path(), HeadHealth(None, self.data_dir).path)

    def test_a_production_only_flip_is_reported(self) -> None:
        self.write_production({"claude": "unauthenticated"})
        self.write_legacy_worktree({"claude": "ready"})  # stale worktree copy, still green

        changed, current = steward_signals._resource_signals({"resource_status": {"claude": "ready"}})

        self.assertEqual(changed, {"claude": "unauthenticated"})
        self.assertEqual(current, {"claude": "unauthenticated"})

    def test_a_stale_worktree_copy_cannot_flip_the_production_status(self) -> None:
        self.write_production({"claude": "ready"})
        self.write_legacy_worktree({"claude": "unavailable"})

        changed, current = steward_signals._resource_signals({"resource_status": {"claude": "ready"}})

        self.assertEqual(changed, {})
        self.assertEqual(current, {"claude": "ready"})

    def test_a_first_seen_resource_is_a_baseline_not_a_flip(self) -> None:
        self.write_production({"claude": "unavailable"})

        changed, current = steward_signals._resource_signals({"resource_status": {}})

        self.assertEqual(changed, {})
        self.assertEqual(current, {"claude": "unavailable"})

    def test_a_missing_or_broken_cache_keeps_the_previous_baseline(self) -> None:
        """Never reset to {}: that would erase the flip the next real read would have reported."""
        mark = {"resource_status": {"claude": "unavailable"}}

        self.assertEqual(steward_signals._resource_signals(mark), ({}, mark["resource_status"]))

        path = HeadHealth(None, self.data_dir).path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        self.assertEqual(steward_signals._resource_signals(mark), ({}, mark["resource_status"]))

        path.write_text(json.dumps({"claude": {"reason": "no status key"}}), encoding="utf-8")
        self.assertEqual(steward_signals._resource_signals(mark), ({}, mark["resource_status"]))

    def test_an_explicit_override_wins_like_the_tick_record(self) -> None:
        elsewhere = self.root / "elsewhere.json"
        with mock.patch.dict(os.environ, {"TA_PRODUCTION_RESOURCE_HEALTH": str(elsewhere)}):
            self.assertEqual(production_telemetry.resource_health_path(), elsewhere)


class HealthAgentStateTests(unittest.TestCase):
    """The health line for curator/steward/retro reads the state root the units actually write."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.state_root = Path(self.tmpdir.name) / "automation-state"
        patcher = mock.patch("ummanu.runtime.state.STATE_ROOT", self.state_root)
        patcher.start()
        self.addCleanup(patcher.stop)
        timer = mock.patch.object(health, "_timer_active", return_value=True)
        timer.start()
        self.addCleanup(timer.stop)

    def write_runs(self, agent: str, records: list[dict]) -> None:
        state = AgentState(agent)
        state.ensure_dir()
        (state.dir / "runs.jsonl").write_text(
            "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
        )

    def test_runs_are_read_from_the_unit_state_root_not_the_worktree(self) -> None:
        self.write_runs("steward", [{"ts": _ts(5), "event": "precheck", "result": "no-change"}])

        problems, detail = health._runs_status("steward")

        self.assertEqual(problems, [])
        self.assertIn("last tick", detail)

    def test_state_root_override_is_honored(self) -> None:
        # TA_STATE is the same knob the units would use if they set one; health must follow it
        # rather than resolving a path of its own.
        elsewhere = Path(self.tmpdir.name) / "elsewhere"
        with mock.patch("ummanu.runtime.state.STATE_ROOT", elsewhere):
            state = AgentState("curator")
            state.ensure_dir()
            (state.dir / "runs.jsonl").write_text(
                json.dumps({"ts": _ts(5), "event": "dispatch"}) + "\n", encoding="utf-8"
            )
            problems, _ = health._runs_status("curator")

        self.assertEqual(problems, [])

    def test_missing_runs_is_still_reported(self) -> None:
        problems, _ = health._runs_status("steward")

        self.assertEqual(problems, ["no runs.jsonl yet"])

    def test_stale_healthy_tick_is_red(self) -> None:
        self.write_runs("steward", [{"ts": _ts(60 * 24), "event": "precheck", "result": "no-change"}])

        problems, _ = health._runs_status("steward")

        self.assertTrue(problems)
        self.assertIn("last healthy tick", problems[0])

    def test_a_fresh_error_does_not_pass_as_a_healthy_tick(self) -> None:
        self.write_runs(
            "steward",
            [
                {"ts": _ts(60 * 24), "event": "precheck", "result": "no-change"},
                {"ts": _ts(1), "event": "precheck", "result": "error"},
            ],
        )

        problems, _ = health._runs_status("steward")

        self.assertTrue(problems)
        self.assertIn("last healthy tick", problems[0])


class HealthExpectedStateTests(unittest.TestCase):
    """Scheduled-role intent comes from the process's installation, not this checkout."""

    ROLES = (
        ("curator", "curator"),
        ("retro", "retro"),
        ("steward", "steward"),
        ("pipeline", "dispatcher-production"),
    )

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.instance = self.root / "instance"
        self.product_root = Path(__file__).resolve().parents[1]
        instance = mock.patch.object(
            production_telemetry, "instance_file", return_value=self.instance / "instance.yaml"
        )
        instance.start()
        self.addCleanup(instance.stop)
        product = mock.patch.dict(os.environ, {"UMMANU_REPO": str(self.product_root)})
        product.start()
        self.addCleanup(product.stop)

    def write_instance(self, components: dict[str, bool] | None = None) -> None:
        rows = [
            "version: 1",
            "name: health-state-test",
            f"data_dir: {self.root / 'data'}",
            "offsite:",
            "  instance_remote: https://example.invalid/instance.git",
            "host:",
            "  unit_prefix: installed-",
        ]
        if components:
            rows.append("  components:")
            for component, enabled in components.items():
                rows.extend((f"    {component}:", f"      enabled: {'true' if enabled else 'false'}"))
        self.instance.mkdir(parents=True, exist_ok=True)
        (self.instance / "instance.yaml").write_text("\n".join(rows) + "\n", encoding="utf-8")

    def check_output(self, agents: tuple[str, ...]) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = health.check(agents)
        return code, output.getvalue()

    def test_shipped_defaults_enable_each_scheduled_role(self) -> None:
        self.write_instance()

        expectations, error = health._role_expectations(tuple(role for role, _ in self.ROLES))

        self.assertIsNone(error)
        assert expectations is not None
        for role, component in self.ROLES:
            with self.subTest(role=role):
                expectation = expectations[role]
                self.assertTrue(expectation.enabled)
                self.assertEqual(expectation.component, component)
                self.assertEqual(expectation.unit_prefix, "installed-")
        self.assertEqual(expectations["retro"].max_age_s, 26 * 3600)
        self.assertEqual(expectations["pipeline"].max_age_s, 900)

    def test_component_overrides_win_over_shipped_defaults(self) -> None:
        for role, component in self.ROLES:
            with self.subTest(role=role):
                self.write_instance({component: False})

                expectations, error = health._role_expectations((role,))

                self.assertIsNone(error)
                assert expectations is not None
                self.assertFalse(expectations[role].enabled)

    def test_enabled_role_with_an_inactive_timer_is_red(self) -> None:
        self.write_instance()
        for role, _ in self.ROLES:
            with (
                self.subTest(role=role),
                mock.patch.object(health, "_timer_active", return_value=False),
                mock.patch.object(health, "_runs_status", return_value=([], "fresh")),
                mock.patch.object(health, "_pipeline_status", return_value=([], "fresh")),
            ):
                code, output = self.check_output((role,))

                self.assertEqual(code, 1)
                self.assertIn(f"RED {role}: installed-", output)
                self.assertIn("not active", output)

    def test_disabled_role_is_neutral_without_timer_or_telemetry_reads(self) -> None:
        for role, component in self.ROLES:
            with self.subTest(role=role):
                self.write_instance({component: False})
                with (
                    mock.patch.object(health, "_timer_active", side_effect=AssertionError),
                    mock.patch.object(health, "_runs_status", side_effect=AssertionError),
                    mock.patch.object(health, "_pipeline_status", side_effect=AssertionError),
                ):
                    code, output = self.check_output((role,))

                self.assertEqual(code, 0)
                self.assertEqual(
                    output,
                    f"DISABLED {role}: {component} disabled by installation configuration\n",
                )

    def test_disabling_steward_deep_sweep_does_not_disable_steward_schedule(self) -> None:
        self.write_instance({"steward-deep-sweep": False})
        with (
            mock.patch.object(health, "_timer_active", return_value=False),
            mock.patch.object(health, "_runs_status", return_value=([], "fresh")),
        ):
            code, output = self.check_output(("steward",))

        self.assertEqual(code, 1)
        self.assertIn("RED steward: installed-steward.timer not active", output)
        self.assertNotIn("DISABLED", output)

    def test_unreadable_installation_configuration_is_an_error(self) -> None:
        code, output = self.check_output(("curator",))

        self.assertEqual(code, 1)
        self.assertIn("ERROR curator: effective installation configuration unavailable", output)


class HealthPipelineLineTests(unittest.TestCase):
    """The pipeline line comes from production dispatcher telemetry, and never flatters it."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.path = Path(self.tmpdir.name) / "dispatcher" / "production-state.json"
        env = mock.patch.dict(os.environ, {"TA_PRODUCTION_STATE": str(self.path)})
        env.start()
        self.addCleanup(env.stop)

    def test_fresh_healthy_tick_is_green(self) -> None:
        _telemetry_state(
            self.path,
            {
                "tick_seq": 7,
                "last": _tick(7, healthy=True),
                "last_healthy_at": _ts(1),
                "unhealthy": [],
                "unhealthy_total": 0,
            },
        )

        problems, detail = health._pipeline_status()

        self.assertEqual(problems, [])
        self.assertIn("last healthy", detail)

    def test_fresh_degraded_tick_is_red_even_with_a_recent_healthy_one(self) -> None:
        _telemetry_state(
            self.path,
            {
                "tick_seq": 8,
                "last": _tick(
                    8,
                    healthy=False,
                    reason="",
                    error_count=1,
                    errors=[{"ref": "ummanu-1", "code": "backend_unavailable", "message": "x"}],
                ),
                "last_healthy_at": _ts(2),
                "unhealthy": [],
                "unhealthy_total": 1,
            },
        )

        problems, _ = health._pipeline_status()

        self.assertTrue(problems)
        self.assertIn("last tick unhealthy", problems[0])
        self.assertIn("backend_unavailable", problems[0])

    def test_a_tick_red_only_from_an_action_names_the_degradation(self) -> None:
        """No caught error at all: the line has to say which operation could not finish."""
        _telemetry_state(
            self.path,
            {
                "tick_seq": 9,
                "last": _tick(
                    9,
                    healthy=False,
                    degraded_count=1,
                    degradations=[
                        {
                            "ref": "ummanu-900",
                            "step": "production-reconcile",
                            "status": "degraded",
                            "action": "launch-intent-stop-unconfirmed",
                            "reason": "the head could not be stopped",
                        }
                    ],
                ),
                "last_healthy_at": _ts(2),
                "unhealthy": [],
                "unhealthy_total": 1,
            },
        )

        problems, _ = health._pipeline_status()

        self.assertIn("last tick unhealthy", problems[0])
        self.assertIn("ummanu-900 launch-intent-stop-unconfirmed", problems[0])
        self.assertIn("the head could not be stopped", problems[0])

    def test_stale_healthy_tick_is_red(self) -> None:
        old = _ts(60 * 6)
        _telemetry_state(
            self.path,
            {
                "tick_seq": 3,
                "last": _tick(3, healthy=True, at=old),
                "last_healthy_at": old,
                "unhealthy": [],
                "unhealthy_total": 0,
            },
        )

        problems, _ = health._pipeline_status()

        self.assertTrue(any("last healthy tick" in problem for problem in problems))

    def test_missing_state_file_is_red_and_names_the_path(self) -> None:
        problems, _ = health._pipeline_status()

        self.assertEqual(len(problems), 1)
        self.assertIn("production-state-missing", problems[0])
        self.assertIn(str(self.path), problems[0])

    def test_state_without_telemetry_is_red_not_silently_green(self) -> None:
        _telemetry_state(self.path, None, last_tick_at=_ts(1))

        problems, _ = health._pipeline_status()

        self.assertEqual(len(problems), 1)
        self.assertIn("tick-telemetry-missing", problems[0])

    def test_unreadable_state_file_is_red(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not json", encoding="utf-8")

        problems, _ = health._pipeline_status()

        self.assertIn("production-state-unreadable", problems[0])


class StewardPipelineSignalTests(unittest.TestCase):
    """`steward scan`'s pipeline signal, against the production dispatcher's own records."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.path = Path(self.tmpdir.name) / "dispatcher" / "production-state.json"
        env = mock.patch.dict(os.environ, {"TA_PRODUCTION_STATE": str(self.path)})
        env.start()
        self.addCleanup(env.stop)
        self.state = AgentState("steward", state_dir=Path(self.tmpdir.name) / "steward")
        state = mock.patch.object(steward_signals, "STATE", self.state)
        state.start()
        self.addCleanup(state.stop)

    def write(self, ticks: str, *, generation: str = "gen-a", payload: dict | None = None) -> dict:
        """Fold a run of production ticks through the REAL writer and land it on disk.

        `ticks` is one character per terminal tick, `.` healthy and `x` unhealthy, in order. Going
        through `record_tick_telemetry` rather than hand-building the record is the point of these
        tests: incident dedup is a contract between two processes, and a hand-written state file
        could pin the reader against a shape the dispatcher never writes.

        Returns the payload so a test can keep ticking the same history forward.
        """
        body = {} if payload is None else payload
        for tick in ticks:
            record_tick_telemetry(
                body,
                {
                    "status": "ok" if tick == "." else "degraded",
                    "step": "production-tick",
                    "errors": []
                    if tick == "."
                    else [{"ref": "", "code": "backend_unavailable", "message": "TaskError"}],
                },
            )
        telemetry = dict(body["tick_telemetry"])
        telemetry["generation"] = generation
        body["tick_telemetry"] = telemetry
        _telemetry_state(self.path, telemetry)
        return body

    def baseline(self, **fields) -> dict:
        """A watermark that has already taken a baseline of the current history."""
        return {
            **steward_signals._empty_watermark(),
            "pipeline_incident_total": 0,
            "pipeline_recovery_total": 0,
            "pipeline_telemetry_generation": "gen-a",
            **fields,
        }

    @contextlib.contextmanager
    def steward_cli_env(self):
        """The steward's own commands with every non-pipeline signal source quiet, so what the
        gate does with the pipeline record is the only thing under test."""
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(steward_cli, "STATE", self.state))
            stack.enter_context(
                mock.patch.object(steward_signals, "resolve_reader", return_value=EMPTY_STEWARD_READER)
            )
            stack.enter_context(
                mock.patch.object(
                    steward_signals,
                    "_workspaces_root",
                    return_value=Path(self.tmpdir.name) / "no-workspaces",
                )
            )
            stack.enter_context(
                mock.patch.dict(
                    os.environ,
                    {
                        "TA_PRODUCTION_RESOURCE_HEALTH": str(
                            Path(self.tmpdir.name) / "no-resource-health.json"
                        )
                    },
                )
            )
            stack.enter_context(mock.patch("sys.stdout", new=io.StringIO()))
            stack.enter_context(mock.patch("sys.stderr", new=io.StringIO()))
            yield

    def steward_runs(self) -> list[dict]:
        path = self.state.dir / "runs.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def test_first_scan_takes_a_baseline_instead_of_replaying_the_record(self) -> None:
        self.write("xxx")

        hits, pending = steward_signals._pipeline_tick_signals(steward_signals._empty_watermark())

        self.assertEqual(hits, [])
        self.assertEqual(pending["pipeline_incident_total"], 1)
        self.assertEqual(pending["pipeline_recovery_total"], 0)
        # The baseline is only meaningful next to the history it came from, so it is carried too.
        self.assertEqual(pending["pipeline_telemetry_generation"], "gen-a")

    def test_one_outage_is_one_incident_however_many_ticks_it_fails(self) -> None:
        """The board-outage shape: every tick fails while it lasts, and it is one thing to look
        at. Repeated scans before `advance` keep reporting the same incident, and further failing
        ticks of the same outage neither open a second one nor re-arm the first."""
        mark = self.baseline()
        payload = self.write("x")

        hits, pending = steward_signals._pipeline_tick_signals(mark)

        self.assertEqual([hit["event"] for hit in hits], ["pipeline-tick-unhealthy"])
        incident = hits[0]["incident"]
        self.assertTrue(incident)
        self.assertEqual(hits[0]["errors"][0]["code"], "backend_unavailable")
        self.assertEqual(hits[0]["unhealthy_ticks"], 1)
        self.assertEqual(hits[0]["incidents"], 1)
        self.assertEqual(pending["pipeline_incident_total"], 1)

        # The outage goes on. A repeated precheck/scan, and every further failing tick, describe
        # the same open incident — not a new one.
        for ticks in ("", "x", "xx"):
            payload = self.write(ticks, payload=payload)
            again, pending = steward_signals._pipeline_tick_signals(mark)
            self.assertEqual([hit["event"] for hit in again], ["pipeline-tick-unhealthy"])
            self.assertEqual(again[0]["incident"], incident)
            self.assertEqual(pending["pipeline_incident_total"], 1)

        # ...and once the steward has actually looked, the same incident is silent.
        self.assertEqual(steward_signals._pipeline_tick_signals(dict(mark, **pending))[0], [])

    def test_unhealthy_hit_summarizes_every_readable_retained_diagnostic_class(self) -> None:
        """The newest tick must not obscure older retained failures of the same incident.

        Diagnostics are bounded per tick. The sixth error below existed (``error_count`` says so)
        but was not retained, so the reader may neither invent its class nor attach its ref to a
        visible class.
        """
        payload: dict = {}
        record_tick_telemetry(
            payload,
            {
                "status": "degraded",
                "step": "production-tick",
                "actions": [
                    {"ref": "ummanu-1", "step": "claim", "action": "start", "status": "failed"},
                    {"ref": "ummanu-2", "step": "claim", "action": "start", "status": "failed"},
                    {"ref": "ummanu-1", "step": "claim", "action": "start", "status": "degraded"},
                ],
                "errors": [
                    {"ref": "ummanu-1", "code": "backend_unavailable"},
                    {"ref": "ummanu-2", "code": "backend_unavailable"},
                    {"ref": "ummanu-1", "code": "access_denied"},
                    {},
                    {"ref": "ummanu-4", "code": "read_timeout"},
                    {"ref": "ummanu-unseen", "code": "unretained_error"},
                ],
            },
        )
        record_tick_telemetry(
            payload,
            {
                "status": "degraded",
                "step": "production-tick",
                "actions": [
                    {"ref": "sprint-9", "step": "observer", "action": "stop", "status": "failed"},
                ],
                "errors": [{"ref": "sprint-9", "code": "later_error"}],
            },
        )
        telemetry = payload["tick_telemetry"]
        telemetry["generation"] = "gen-a"
        self.assertEqual(telemetry["unhealthy"][-1]["errors"][0]["code"], "later_error")
        self.assertEqual(telemetry["unhealthy"][0]["error_count"], TICK_TELEMETRY_ERRORS_KEPT + 1)
        self.assertEqual(len(telemetry["unhealthy"][0]["errors"]), TICK_TELEMETRY_ERRORS_KEPT)
        # A reader has to tolerate hand-edited/old optional diagnostic items alongside valid ones.
        telemetry["unhealthy"][0]["degradations"].extend([None, {"step": "claim", "action": 7}])
        telemetry["unhealthy"][0]["errors"].extend([None, {"code": 7, "ref": "ignored"}])
        _telemetry_state(self.path, telemetry)

        hits, _ = steward_signals._pipeline_tick_signals(self.baseline())

        self.assertEqual([hit["event"] for hit in hits], ["pipeline-tick-unhealthy"])
        self.assertEqual(
            hits[0]["retained_window"],
            {
                "degradations": [
                    {
                        "step": "claim",
                        "action": "start",
                        "count": 3,
                        "refs": ["ummanu-1", "ummanu-2"],
                    },
                    {"step": "observer", "action": "stop", "count": 1, "refs": ["sprint-9"]},
                ],
                "errors": [
                    {"code": "access_denied", "count": 1, "refs": ["ummanu-1"]},
                    {
                        "code": "backend_unavailable",
                        "count": 2,
                        "refs": ["ummanu-1", "ummanu-2"],
                    },
                    {"code": "later_error", "count": 1, "refs": ["sprint-9"]},
                    {"code": "read_timeout", "count": 1, "refs": ["ummanu-4"]},
                ],
            },
        )

    def test_the_first_healthy_tick_reports_one_recovery_and_no_more(self) -> None:
        mark = self.baseline()
        payload = self.write("xx")
        opened_hits, pending = steward_signals._pipeline_tick_signals(mark)
        opened = opened_hits[0]
        mark = dict(mark, **pending)  # the head looked at the failure and advanced

        payload = self.write(".", payload=payload)
        hits, pending = steward_signals._pipeline_tick_signals(mark)

        self.assertEqual([hit["event"] for hit in hits], ["pipeline-tick-recovered"])
        recovered = hits[0]
        # The recovery names the incident it closes, its cause and how the pipeline came back.
        self.assertEqual(recovered["incident"], opened["incident"])
        self.assertEqual(recovered["opened_at"], opened["ts"])
        self.assertEqual(recovered["unhealthy_ticks"], 2)
        self.assertEqual(recovered["recovered_status"], "ok")
        self.assertIn("backend_unavailable", recovered["cause"])
        self.assertEqual(pending["pipeline_recovery_total"], 1)

        # Before `advance` the same recovery is still the batch, and further healthy ticks do not
        # repeat it afterwards.
        self.assertEqual(
            [hit["incident"] for hit in steward_signals._pipeline_tick_signals(mark)[0]],
            [recovered["incident"]],
        )
        mark = dict(mark, **pending)
        payload = self.write("..", payload=payload)
        self.assertEqual(steward_signals._pipeline_tick_signals(mark)[0], [])

    def test_a_failure_after_recovery_opens_a_new_incident(self) -> None:
        mark = self.baseline()
        payload = self.write("x.")
        mark = dict(mark, **steward_signals._pipeline_tick_signals(mark)[1])
        first = payload["tick_telemetry"]["recovery"]["id"]

        payload = self.write("xx", payload=payload)
        hits, pending = steward_signals._pipeline_tick_signals(mark)

        self.assertEqual([hit["event"] for hit in hits], ["pipeline-tick-unhealthy"])
        self.assertNotEqual(hits[0]["incident"], first)
        self.assertEqual(pending["pipeline_incident_total"], 2)

    def test_an_incident_that_opened_and_closed_between_two_scans_reports_both(self) -> None:
        """A steward that only heard the recovery would be told something ended without ever being
        told what broke, so the failure is reported first even though it is already over."""
        mark = self.baseline()
        self.write("x.")

        hits, _ = steward_signals._pipeline_tick_signals(mark)

        self.assertEqual(
            [hit["event"] for hit in hits], ["pipeline-tick-unhealthy", "pipeline-tick-recovered"]
        )
        self.assertEqual(hits[0]["incident"], hits[1]["incident"])
        self.assertEqual(hits[0]["errors"][0]["code"], "backend_unavailable")

    def test_a_replaced_state_file_reports_the_reset_and_the_incident_again(self) -> None:
        mark = self.baseline(pipeline_incident_total=9, pipeline_recovery_total=9)
        self.write("x")

        hits, pending = steward_signals._pipeline_tick_signals(mark)

        self.assertEqual(hits[0]["event"], "pipeline-telemetry-reset")
        self.assertEqual([hit["event"] for hit in hits[1:]], ["pipeline-tick-unhealthy"])
        self.assertEqual(pending["pipeline_incident_total"], 1)

    def test_a_replaced_state_with_the_same_counter_is_still_a_reset(self) -> None:
        """The counter alone cannot see a state file swap that lands on the same number.

        A restore or a rebuilt installation starts its own history, and its first incident meets a
        watermark that already says 1. Without the generation the steward reports nothing and
        everything the new history records is deduped away forever (secretary-833 review, round 4).
        """
        mark = self.baseline(pipeline_incident_total=1, pipeline_recovery_total=0)
        self.write("x", generation="gen-b")

        hits, pending = steward_signals._pipeline_tick_signals(mark)

        self.assertEqual(hits[0]["event"], "pipeline-telemetry-reset")
        self.assertEqual(hits[0]["seen_generation"], "gen-a")
        self.assertEqual([hit["event"] for hit in hits[1:]], ["pipeline-tick-unhealthy"])
        self.assertEqual(pending["pipeline_telemetry_generation"], "gen-b")
        self.assertIn("pipeline-telemetry-reset", [run["event"] for run in self.steward_runs()])

        # The same history on the next scan is not a reset again: only a change is.
        self.assertEqual(steward_signals._pipeline_tick_signals(dict(mark, **pending))[0], [])

    def test_telemetry_without_a_generation_does_not_reset_every_scan(self) -> None:
        """A host whose dispatcher predates the stamp writes no generation. That is "cannot tell",
        not "the state changed" — reading it as a change would report a reset every hour."""
        mark = self.baseline(pipeline_incident_total=1, pipeline_recovery_total=0)
        self.write("x", generation="")

        hits, pending = steward_signals._pipeline_tick_signals(mark)

        self.assertEqual(hits, [])
        self.assertEqual(pending["pipeline_telemetry_generation"], "")

    def test_missing_telemetry_wakes_the_head_and_is_logged_durably(self) -> None:
        mark = self.baseline(pipeline_incident_total=2, pipeline_recovery_total=1)

        hits, pending = steward_signals._pipeline_tick_signals(mark)

        self.assertEqual([hit["event"] for hit in hits], ["production-state-missing"])
        self.assertEqual(hits[0]["level"], "warn")
        # The watermark does not move over a source that could not be read.
        self.assertEqual(
            pending,
            {
                "pipeline_incident_total": 2,
                "pipeline_recovery_total": 1,
                "pipeline_telemetry_generation": "gen-a",
            },
        )
        self.assertEqual([run["event"] for run in self.steward_runs()], ["production-state-missing"])

    def test_a_quiet_precheck_keeps_the_baseline_so_a_later_failure_still_fires(self) -> None:
        """The real lifecycle: precheck skips quiet hours, and `advance` only ever runs after a
        dispatched head. If the baseline the first quiet scan took never reached disk, the next
        failure would be read as another first-ever scan and suppressed, forever (round-2 finding).
        """
        with self.steward_cli_env():
            payload = self.write(".")
            self.assertEqual(steward_cli.cmd_precheck(), PRECHECK_SKIP)
            self.assertEqual(self.state.load_watermark()["pipeline_incident_total"], 0)

            # A production tick fails. The steward is woken by it, and stays woken until it has
            # actually looked: a repeated precheck before `advance` must not consume the signal.
            payload = self.write("x", payload=payload)
            self.assertEqual(steward_cli.cmd_precheck(), 0)
            self.assertEqual(steward_cli.cmd_precheck(), 0)
            hits = steward_signals.scan()["signals"]["pipeline_ticks"]
            self.assertEqual([hit["event"] for hit in hits], ["pipeline-tick-unhealthy"])

            # ...and once the head has looked, the same incident is silent — while the recovery
            # that ends it, and a later independent failure, each wake the steward once more.
            self.assertEqual(steward_cli.cmd_scan(True), 0)
            self.assertEqual(steward_cli.cmd_advance(), 0)
            self.assertEqual(steward_cli.cmd_precheck(), PRECHECK_SKIP)

            payload = self.write(".", payload=payload)
            self.assertEqual(steward_cli.cmd_precheck(), 0)
            recovered = steward_signals.scan()["signals"]["pipeline_ticks"]
            self.assertEqual([hit["event"] for hit in recovered], ["pipeline-tick-recovered"])
            self.assertEqual(steward_cli.cmd_scan(True), 0)
            self.assertEqual(steward_cli.cmd_advance(), 0)
            self.assertEqual(steward_cli.cmd_precheck(), PRECHECK_SKIP)

            payload = self.write("x", payload=payload)
            self.assertEqual(steward_cli.cmd_precheck(), 0)
            reopened = steward_signals.scan()["signals"]["pipeline_ticks"]
            self.assertEqual([hit["event"] for hit in reopened], ["pipeline-tick-unhealthy"])
            self.assertNotEqual(reopened[0]["incident"], recovered[0]["incident"])

            # The health line reads the same records, and the synthetic recovery leaves it green.
            self.write(".", payload=payload)
            problems, detail = health._pipeline_status()
            self.assertEqual(problems, [])
            self.assertIn("last healthy", detail)

    def test_the_baseline_write_never_overwrites_a_watermark_or_invents_one(self) -> None:
        self.write("x.")
        self.state.save_watermark(
            dict(
                steward_signals._empty_watermark(),
                pipeline_incident_total=2,
                pipeline_recovery_total=2,
                notified_blocked=["ref-1"],
            )
        )

        steward_signals.ensure_pipeline_baseline(
            {
                "pending": {
                    "pipeline_incident_total": 4,
                    "pipeline_recovery_total": 4,
                    "pipeline_telemetry_generation": "gen-a",
                }
            }
        )

        mark = self.state.load_watermark()
        self.assertEqual(mark["pipeline_incident_total"], 2)
        self.assertEqual(mark["notified_blocked"], ["ref-1"])

        # A baseline that is taken carries the recovery counter and the history it was read from,
        # or the next scan would replay a recovery, or fail to tell a replaced state from this one.
        self.state.watermark_file.unlink()
        steward_signals.ensure_pipeline_baseline(
            {
                "pending": {
                    "pipeline_incident_total": 4,
                    "pipeline_recovery_total": 3,
                    "pipeline_telemetry_generation": "gen-a",
                }
            }
        )
        mark = self.state.load_watermark()
        self.assertEqual(mark["pipeline_recovery_total"], 3)
        self.assertEqual(mark["pipeline_telemetry_generation"], "gen-a")

        # Telemetry that could not be read measured nothing, so it leaves no baseline behind.
        self.state.watermark_file.unlink()
        steward_signals.ensure_pipeline_baseline({"pending": {"pipeline_incident_total": None}})
        self.assertIsNone(self.state.load_watermark().get("pipeline_incident_total"))

    def test_a_hit_is_a_signal_and_renders(self) -> None:
        batch = {
            "signals": {
                "pipeline_ticks": [
                    {
                        "event": "pipeline-tick-unhealthy",
                        "ts": _ts(1),
                        "retained_window": {"degradations": [], "errors": []},
                    }
                ],
                "new_blocked": [],
                "stale": [],
                "resource_flip": {},
                "new_orphan_workspaces": [],
            }
        }

        self.assertTrue(steward_signals.has_signal(batch))
        rendered = steward_signals.render_markdown(batch)
        self.assertIn("production dispatcher", rendered)
        self.assertIn("retained_window", rendered)


_LONG_AGO = 1_600_000_000  # a card date_moved far beyond any stale threshold


class StewardStaleColumnsTests(unittest.TestCase):
    """secretary-1025: Assessment is an active column for the stale detector.

    Nothing else watches it. A card in Assessment has no head and no gate running, so no
    watchdog can time it out; if the observer never comes back to decide, the stale signal is
    the only thing that notices.
    """

    def test_an_assessment_card_past_the_threshold_is_reported_stale(self) -> None:
        looked_at: list[str] = []

        def list_cards(column: str | None = None, **_kwargs: object) -> list[dict]:
            looked_at.append(str(column))
            if column != "Assessment":
                return []
            return [{"reference": "ummanu-1025", "date_moved": _LONG_AGO}]

        class Reader:
            def active_cards(self, *, states=None, project=None):
                return [
                    {"reference": row["reference"], "column": column, "date_moved": row["date_moved"]}
                    for column in steward_signals.STALE_COLUMNS
                    for row in list_cards(column)
                ]

        hits, notified = steward_signals._stale_signals({"notified_stale": {}}, Reader())

        self.assertEqual(hits, [{"reference": "ummanu-1025", "column": "Assessment", "since": _LONG_AGO}])
        self.assertEqual(notified, {"ummanu-1025": _LONG_AGO})
        self.assertIn("Assessment", looked_at)
        # Issues and Done stay out: an untriaged proposal and a finished card may sit forever.
        self.assertNotIn("Issues", looked_at)
        self.assertNotIn("Done", looked_at)

    def test_a_stale_assessment_card_fires_only_once_per_dwell(self) -> None:
        class Reader:
            def active_cards(self, *, states=None, project=None):
                return [{"reference": "ummanu-1025", "column": "Assessment", "date_moved": _LONG_AGO}]

        hits, notified = steward_signals._stale_signals(
            {"notified_stale": {"ummanu-1025": _LONG_AGO}}, Reader()
        )

        self.assertEqual(hits, [])
        self.assertEqual(notified, {"ummanu-1025": _LONG_AGO})


class StewardSignalPortTests(unittest.TestCase):
    class Reader:
        def __init__(self) -> None:
            self.calls: list[tuple[set[str] | None, str | None]] = []

        def active_cards(self, *, states=None, project=None):
            self.calls.append((states, project))
            cards = [
                {
                    "reference": "ummanu-1",
                    "state": "blocked",
                    "column": "Blocked",
                    "project": "ummanu",
                    "date_moved": _LONG_AGO,
                    "steward_report": "",
                },
                {
                    "reference": "ummanu-2",
                    "state": "ready",
                    "column": "Ready",
                    "project": "ummanu",
                    "date_moved": _LONG_AGO,
                    "steward_report": "",
                },
                {
                    "reference": "ummanu-3",
                    "state": "blocked",
                    "column": "Blocked",
                    "project": "ummanu",
                    "date_moved": _LONG_AGO,
                    "steward_report": "1",
                },
            ]
            return [
                card
                for card in cards
                if (states is None or card["state"] in states)
                and (project is None or card["project"] == project)
            ]

    def test_injected_reader_covers_signal_reads_and_advance_without_legacy_ops(self) -> None:
        reader = self.Reader()
        with tempfile.TemporaryDirectory() as tmp:
            state = AgentState("steward", state_dir=Path(tmp) / "state")
            root = Path(tmp) / "workspaces"
            workspace = root / "ummanu"
            active_worker = workspace / "ummanu-2-live"
            active_reviewer = workspace / "review-ummanu-1-live"
            orphan = workspace / "ummanu-999-orphan"
            active_worker.mkdir(parents=True)
            active_reviewer.mkdir()
            orphan.mkdir()
            (root / "observers" / "sprint-1").mkdir(parents=True)
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(steward_signals, "STATE", state))
                stack.enter_context(mock.patch.object(steward_cli, "STATE", state))
                stack.enter_context(mock.patch.object(steward_signals, "_workspaces_root", return_value=root))
                stack.enter_context(
                    mock.patch.object(
                        steward_signals,
                        "_pipeline_tick_signals",
                        return_value=([], steward_signals._empty_watermark()),
                    )
                )
                stack.enter_context(
                    mock.patch.object(steward_signals, "_resource_signals", return_value=({}, {}))
                )
                batch = steward_signals.scan(reader)
                self.assertEqual(batch["signals"]["new_blocked"], ["ummanu-1"])
                self.assertIn(
                    {"reference": "ummanu-2", "column": "Ready", "since": _LONG_AGO},
                    batch["signals"]["stale"],
                )
                self.assertEqual(batch["signals"]["new_orphan_workspaces"], [str(orphan)])
                state.ensure_dir()
                state.pending_file.write_text(json.dumps({"notified_blocked": []}), encoding="utf-8")
                self.assertEqual(steward_cli.cmd_advance(reader), 0)
            self.assertEqual(state.load_watermark()["notified_blocked"], ["ummanu-1"])
        self.assertIn((None, "ummanu"), reader.calls)


if __name__ == "__main__":
    unittest.main()
