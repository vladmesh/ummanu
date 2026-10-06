from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

from tests.fakes.sprints import sprint_store, status_seed
from ummanu import state_repo
from ummanu.board.sql_audit import SqlTaskAudit
from ummanu.cli import main
from ummanu.config import validate, validate_instance
from ummanu.head_registry import (
    materialize_snapshot,
    product_revision,
    record_source,
)
from ummanu.host import (
    CollectResult,
    HostInventory,
    build_doctor_expectations,
    packaging_root,
)
from ummanu.host_apply import resolve_packaged
from ummanu.secret_store import initialize_store, set_secret
from ummanu.sprints import SprintWriter
from ummanu.status import collect_status


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


def _init_instance_repo(instance_dir: Path, instance_yaml: str) -> None:
    instance_dir.mkdir(parents=True, exist_ok=True)
    (instance_dir / "instance.yaml").write_text(instance_yaml, encoding="utf-8")
    _git(instance_dir, "init", "--quiet", "--initial-branch", "main")
    _git(instance_dir, "config", "user.name", "operator")
    _git(instance_dir, "config", "user.email", "operator@example.invalid")
    _git(instance_dir, "config", "commit.gpgsign", "false")
    for key, value in state_repo.PACKING_CONTROLS:
        _git(instance_dir, "config", "--local", key, value)
    _git(instance_dir, "add", "instance.yaml")
    _git(instance_dir, "commit", "--quiet", "-m", "config")


class StatusCliTests(unittest.TestCase):
    # status and doctor resolve the packaged Orca runtime. tests/__init__.py
    # routes that discovery to the repo fixture for every test by default, so
    # this class no longer needs its own patch (secretary-705, secretary-738,
    # secretary-748).

    def test_status_json_has_the_documented_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            (data_dir / "dispatcher").mkdir(parents=True)
            (data_dir / "memory").mkdir()
            (data_dir / "memory" / "manifest.json").write_text(
                json.dumps({"journal": {"fact_count": 2}}), encoding="utf-8"
            )
            (data_dir / "memory" / "index.sqlite").write_text("index", encoding="utf-8")
            (data_dir / "board").mkdir()
            (data_dir / "board" / "cards.ndjson").write_text('{"ref":"ummanu-727"}\n', encoding="utf-8")
            (data_dir / "dispatcher" / "pause.json").write_text(
                json.dumps({"mode": "freeze", "actor": "ummanu", "since": "2999-01-01T00:00:00Z"}),
                encoding="utf-8",
            )
            (data_dir / "dispatcher" / "production-state.json").write_text(
                json.dumps(
                    {
                        "phase": "production",
                        "last_tick_finished_at": "2026-07-26T00:00:00Z",
                        "tick_telemetry": {
                            "tick_seq": 12,
                            "last": {
                                "seq": 12,
                                "at": "2026-07-26T00:00:00Z",
                                "status": "ok",
                                "step": "production-tick",
                                "healthy": True,
                                "reason": "",
                                "actions": 2,
                                "error_count": 0,
                                "degraded_count": 0,
                                "duration_ms": 4321.5,
                            },
                        },
                        "records": {
                            "ummanu-727": {
                                "attempt_id": "a1",
                                "head": "codex",
                                "workspace": "/work",
                                "worker_progress_at": 1,
                                "worker_respawns": 1,
                                "paused_worker_at": 1,
                            }
                        },
                        "controlled_divergences": [
                            {
                                "id": "div_open0000000000",
                                "at": "2026-07-25T00:00:00Z",
                                "pilot_ref": "ummanu-730",
                                "step": "production-recovery",
                                "reason": "active_claim_mismatch",
                                "status": "open",
                            },
                            {
                                "id": "div_closed00000000",
                                "at": "2026-07-01T00:00:00Z",
                                "pilot_ref": "ummanu-716",
                                "step": "production-recovery",
                                "reason": "active_claim_mismatch",
                                "status": "closed",
                                "closed_at": "2026-07-02T00:00:00Z",
                                "closed_reason": "card left the active dispatcher cycle (state=issues)",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            instance = root / "instance.yaml"
            instance.write_text(
                "version: 1\nname: test\n"
                f"data_dir: {data_dir}\n"
                "offsite:\n  instance_remote: git@example.invalid:x/y.git\n"
                "heads:\n  - role: worker\n    model: codex\n"
                "host:\n  projects_root: /projects\n  unit_prefix: ummanu-\n"
                "  orca_repos:\n    - demo\n",
                encoding="utf-8",
            )
            projects = root / "projects"
            projects.mkdir()
            (projects / "demo.yaml").write_text(
                "id: demo\nrepo: /projects/demo\nenabled: true\nadapter: demo\ndefault_branch: main\n",
                encoding="utf-8",
            )
            fixture = root / "host"
            fixture.mkdir()
            report = validate_instance(instance)
            expected = build_doctor_expectations(
                report.instance,
                report.bindings,
                packaged=resolve_packaged(
                    report.instance,
                    instance_path=root,
                    data_dir=report.data_dir,
                ),
            )
            (fixture / "units.txt").write_text("\n".join(sorted(expected.units)), encoding="utf-8")
            (fixture / "projects.txt").write_text("/projects/demo\n", encoding="utf-8")
            (fixture / "unit-states.txt").write_text(
                "\n".join(f"{name} enabled active" for name in sorted(expected.units)), encoding="utf-8"
            )
            output = io.StringIO()
            checkpoint = {
                "last_commit": "abc123",
                "last_commit_at": "2026-07-25T12:00:00Z",
                "last_push_at": "2026-07-25T11:50:00Z",
                "last_push_commit": "old123",
                "lag_commits": 9,
                "lag_minutes": 7,
                "push_status": "pending",
                "push_reason": "",
                "push_failures": 0,
                "remote_diverged": False,
                "blocked_reason": "",
                "checkpoint_status": "committed",
                "checkpoint_duration_ms": 2100.0,
            }
            with (
                contextlib.redirect_stdout(output),
                mock.patch("ummanu.status.checkpoint_snapshot", return_value=checkpoint),
            ):
                code = main(["status", "--json", "--host-fixture", str(fixture), "--instance", str(instance)])

        self.assertEqual(code, 0, output.getvalue())
        payload = json.loads(output.getvalue())
        self.assertEqual(validate(payload, "status", "status.json"), [])
        self.assertEqual(payload["dispatcher"]["active_attempts"][0]["head"], "codex")
        self.assertEqual(payload["dispatcher"]["active_attempts"][0]["watchdogs"]["worker"]["respawns"], 1)
        self.assertTrue(payload["dispatcher"]["active_attempts"][0]["paused"]["worker"])
        self.assertTrue(payload["host"]["units"])
        self.assertTrue(payload["host"]["schedules"])
        self.assertEqual(payload["host"]["units"][0]["enabled"], "enabled")
        self.assertEqual(payload["installation"]["projects"], 1)
        self.assertEqual(payload["installation"]["heads"][0]["role"], "worker")
        self.assertEqual(payload["installation"]["cards"]["total"], 1)
        self.assertIsNotNone(payload["host"]["resources"]["disk_free_bytes"])
        self.assertEqual(len(payload["host"]["resources"]["load_average"]), 3)
        self.assertEqual(payload["checkpoint"]["last_commit"], "abc123")
        self.assertEqual(payload["checkpoint"]["lag_commits"], 9)
        self.assertTrue(payload["dispatcher"]["pause"]["paused"])
        self.assertEqual(payload["dispatcher"]["pause"]["mode"], "freeze")
        self.assertEqual(payload["dispatcher"]["pause"]["actor"], "ummanu")
        self.assertEqual(payload["dispatcher"]["pause"]["auto_resume"]["reason"], "fresh")
        self.assertEqual(payload["memory"]["fact_count"], 2)
        self.assertIsNotNone(payload["memory"]["last_reindex_at"])
        self.assertEqual(payload["dispatcher"]["divergences"]["open_count"], 1)
        self.assertEqual(payload["dispatcher"]["divergences"]["total_count"], 2)
        self.assertEqual(payload["dispatcher"]["divergences"]["open"][0]["pilot_ref"], "ummanu-730")
        self.assertIsNotNone(payload["dispatcher"]["reconciliation"]["last_tick_finished_at"])
        self.assertEqual(payload["dispatcher"]["reconciliation"]["records_tracked"], 1)
        # This fixture's state predates the reconciliation pass (no "last_reconciled_at" key was
        # written): a pre-deployment host must read as "unknown", not borrow the pre-existing
        # "last_tick_finished_at" as if it were reconciliation evidence.
        self.assertIsNone(payload["dispatcher"]["reconciliation"]["last_reconciled_at"])
        # A20 step 9 (secretary-1726): status no longer reports a host-owned Orca runtime.
        self.assertNotIn("external_runtime", payload["host"])
        # What the last tick cost, beside the facts already recorded for it (secretary-1649): the
        # dispatcher has always written this entry and status carried nothing from it.
        last_tick = payload["dispatcher"]["last_tick"]
        self.assertEqual(last_tick["duration_ms"], 4321.5)
        self.assertEqual(last_tick["seq"], 12)
        self.assertEqual(last_tick["status"], "ok")
        self.assertEqual(last_tick["at"], "2026-07-26T00:00:00Z")
        self.assertTrue(last_tick["healthy"])
        self.assertEqual(last_tick["actions"], 2)
        self.assertEqual(payload["checkpoint"]["checkpoint_duration_ms"], 2100.0)

    def test_status_human_output_and_live_watchdog_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            (data_dir / "dispatcher").mkdir(parents=True)
            (data_dir / "dispatcher" / "production-state.json").write_text(
                json.dumps(
                    {
                        "records": {
                            "ummanu-727": {"head": "codex", "worker_progress_at": 1, "worker_respawns": 2}
                        }
                    }
                ),
                encoding="utf-8",
            )
            instance = root / "instance.yaml"
            instance.write_text(
                "version: 1\nname: test\ndata_dir: " + str(data_dir) + "\n"
                "offsite:\n  instance_remote: git@example.invalid:x/y.git\n"
                "host:\n  unit_prefix: ummanu-\n",
                encoding="utf-8",
            )
            output = io.StringIO()
            inventory = HostInventory(units=set(), unit_states={})
            panel = {"known": True, "live": True, "reason": "pane-active"}
            with (
                contextlib.redirect_stdout(output),
                mock.patch("ummanu.status.LiveHostSource.collect", return_value=CollectResult(inventory)),
                mock.patch("ummanu.status.command_terminal_status", return_value=panel),
                mock.patch("ummanu.status.checkpoint_snapshot", return_value={"lag_minutes": 4}),
            ):
                code = main(["status", "--instance", str(instance)])
            report = validate_instance(instance)
            with (
                mock.patch("ummanu.status.LiveHostSource.collect", return_value=CollectResult(inventory)),
                mock.patch("ummanu.status.command_terminal_status", return_value=panel),
                mock.patch("ummanu.status.checkpoint_snapshot", return_value={"lag_minutes": 4}),
            ):
                from ummanu.status import collect_status

                snapshot = collect_status(report)

        self.assertEqual(code, 0)
        self.assertIn("Ummanu status:", output.getvalue())
        self.assertTrue(snapshot["dispatcher"]["active_attempts"][0]["watchdogs"]["worker"]["panel"]["live"])

    def test_status_json_includes_stopped_sprint_and_its_frozen_resume(self):
        """A stopped sprint reports the freshness of its own record, not of later events."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            (data_dir / "dispatcher").mkdir(parents=True)
            (data_dir / "dispatcher" / "production-state.json").write_text(
                json.dumps({"records": {}}), encoding="utf-8"
            )
            instance = root / "instance.yaml"
            instance.write_text(
                "version: 1\nname: test\n"
                f"data_dir: {data_dir}\n"
                "offsite:\n  instance_remote: git@example.invalid:x/y.git\n"
                "host:\n  unit_prefix: ummanu-\n"
                "sprint_budget:\n  signal: 20\n  hard: 40\n",
                encoding="utf-8",
            )
            board = sprint_store(self, status_seed())
            board.add_sprint(
                "sprint:1",
                status="stopped",
                sprint_resume=json.dumps(
                    {
                        "selected_step": "fix",
                        "selected_why": "blocked",
                        "rejected_alternatives": "wait",
                        "current_task": "ummanu-510",
                        "dod_state": "pending",
                        "next_safe_step": "test",
                        "recorded_at": "2020-01-01T00:00:00Z",
                    }
                ),
            )
            # The store keeps a budget as the charges that make it up, so it is arranged through
            # the verbatim restore a checkpoint recovery uses.
            SprintWriter(board, data_dir=data_dir).restore(
                reference="sprint:1",
                values={"sprint_budget": json.dumps({"by_type": {"blocked": 2}})},
                request_id="fixture-budget",
            )
            board.save_metadata(12, sprint_ref="sprint:1")
            SqlTaskAudit(board).append(
                "later-card-event",
                {
                    "event_id": "evt_later_card_event",
                    "request_id": "later-card-event",
                    "ref": "ummanu-510",
                    "kind": "moved",
                    "outcome": "success",
                    "payload": {"to": "assessment"},
                    "occurred_at": "2026-07-27T00:00:00Z",
                },
            )
            output = io.StringIO()
            report = validate_instance(instance)
            with (
                contextlib.redirect_stdout(output),
                mock.patch(
                    "ummanu.status.checkpoint_snapshot",
                    return_value={
                        "last_commit": None,
                        "lag_minutes": None,
                        "remote_diverged": False,
                        "blocked_reason": None,
                        "checkpoint_duration_ms": 0.0,
                    },
                ),
            ):
                from ummanu.status import collect_status

                snapshot = collect_status(report, offline=True, sprint_client=board)

        self.assertEqual(validate(snapshot, "status", "status.json"), [])
        sprint = snapshot["installation"]["sprints"]["items"][0]
        self.assertEqual(sprint["status"], "stopped")
        self.assertEqual(sprint["stop_reason"], "budget_hard_limit")
        self.assertEqual(sprint["budget"]["by_type"]["blocked"], 2)
        self.assertEqual(sprint["budget"]["thresholds"], {"signal": 20, "hard": 40})
        # The linked card moved after the sprint stopped.  The sprint takes no further work, so
        # its resume cannot go stale: freshness comes from the record the stop froze.
        self.assertTrue(sprint["resume_freshness"]["fresh"])
        self.assertIsNone(sprint["resume_freshness"]["error"])
        self.assertEqual(sprint["resume_freshness"]["recorded_at"], "2020-01-01T00:00:00Z")
        self.assertIsNone(sprint["resume_freshness"]["last_event_at"])

    def test_status_json_reads_the_task_audit_once_however_many_sprints_exist(self):
        """`ummanu status --json` costs one audit traversal, not one per sprint."""

        def snapshot(sprint_count: int, *, open_first: bool = True) -> tuple[dict, int]:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                data_dir = root / "data"
                (data_dir / "dispatcher").mkdir(parents=True)
                (data_dir / "dispatcher" / "production-state.json").write_text(
                    json.dumps({"records": {}}), encoding="utf-8"
                )
                instance = root / "instance.yaml"
                instance.write_text(
                    "version: 1\nname: test\n"
                    f"data_dir: {data_dir}\n"
                    "offsite:\n  instance_remote: git@example.invalid:x/y.git\n"
                    "host:\n  unit_prefix: ummanu-\n",
                    encoding="utf-8",
                )
                board = sprint_store(self, status_seed())
                for index in range(sprint_count):
                    board.add_sprint(
                        f"sprint:{index}",
                        status="open" if index == 0 and open_first else "closed",
                        sprint_resume=json.dumps(
                            {
                                "selected_step": "fix",
                                "selected_why": "blocked",
                                "rejected_alternatives": "wait",
                                "current_task": "ummanu-510",
                                "dod_state": "pending",
                                "next_safe_step": "test",
                                "recorded_at": "2020-01-01T00:00:00Z",
                            }
                        ),
                    )
                board.save_metadata(12, sprint_ref="sprint:0")
                SqlTaskAudit(board).append(
                    "later-card-event",
                    {
                        "event_id": "evt_later_card_event",
                        "request_id": "later-card-event",
                        "ref": "ummanu-510",
                        "kind": "moved",
                        "outcome": "success",
                        "payload": {"to": "assessment"},
                        "occurred_at": "2026-07-27T00:00:00Z",
                    },
                )
                report = validate_instance(instance)
                traversals = 0
                original = SqlTaskAudit.events

                def counting(audit, *args, **kwargs):
                    nonlocal traversals
                    traversals += 1
                    return original(audit, *args, **kwargs)

                with (
                    mock.patch(
                        "ummanu.status.checkpoint_snapshot",
                        return_value={
                            "last_commit": None,
                            "lag_minutes": None,
                            "remote_diverged": False,
                            "blocked_reason": None,
                            "checkpoint_duration_ms": 0.0,
                        },
                    ),
                    mock.patch.object(SqlTaskAudit, "events", counting),
                ):
                    collected = collect_status(report, offline=True, sprint_client=board)
                return collected, traversals

        one, one_traversals = snapshot(1)
        many, many_traversals = snapshot(4)
        terminal, terminal_traversals = snapshot(4, open_first=False)

        self.assertEqual(len(one["installation"]["sprints"]["items"]), 1)
        self.assertEqual(len(many["installation"]["sprints"]["items"]), 4)
        self.assertEqual(one_traversals, 1)
        self.assertEqual(many_traversals, 1)
        # An installation whose sprints have all finished reads the journal not at all.
        self.assertEqual(len(terminal["installation"]["sprints"]["items"]), 4)
        self.assertEqual(terminal_traversals, 0)
        for item in terminal["installation"]["sprints"]["items"]:
            self.assertTrue(item["resume_freshness"]["fresh"])
            self.assertIsNone(item["resume_freshness"]["last_event_at"])
        for items in (one["installation"]["sprints"]["items"], many["installation"]["sprints"]["items"]):
            # The open sprint still reflects the significant later linked-card event.
            open_sprint = next(item for item in items if item["status"] == "open")
            self.assertEqual(open_sprint["resume_freshness"]["error"], "resume_stale")
            self.assertEqual(open_sprint["resume_freshness"]["last_event_at"], "2026-07-27T00:00:00Z")

    def test_invalid_status_json_uses_the_documented_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp) / "instance.yaml"
            instance.write_text("not: an-instance\n", encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["status", "--json", "--instance", str(instance)])
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(validate(payload, "status", "status.json"), [])

    def test_doctor_json_has_structured_findings(self):
        root = Path(__file__).resolve().parents[1]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["doctor", "--offline", "--json", "--instance", str(root / "examples" / "instance")])
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0, payload)
        self.assertIn("findings", payload)
        self.assertIn("status", payload)

    def test_doctor_json_reports_the_same_missing_host_resource_as_doctor(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp)
            text_output = io.StringIO()
            json_output = io.StringIO()
            arguments = [
                "doctor",
                "--host-fixture",
                str(fixture),
                "--instance",
                str(root / "examples" / "instance"),
            ]
            with contextlib.redirect_stdout(text_output):
                text_code = main(arguments)
            with contextlib.redirect_stdout(json_output):
                code = main(["doctor", "--json", *arguments[1:]])
        payload = json.loads(json_output.getvalue())
        self.assertEqual(text_code, 1, text_output.getvalue())
        self.assertIn("missing-on-host:", text_output.getvalue())
        self.assertEqual(code, 1, payload)
        self.assertFalse(payload["ok"])
        self.assertTrue(any(finding["code"] == "missing_on_host" for finding in payload["findings"]))

    def test_doctor_json_reports_unit_runtime_findings(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp)
            report = validate_instance(root / "examples" / "instance")
            expected = build_doctor_expectations(
                report.instance,
                report.bindings,
                packaged=resolve_packaged(
                    report.instance,
                    instance_path=root / "examples" / "instance",
                    data_dir=report.data_dir,
                ),
            )
            (fixture / "units.txt").write_text("\n".join(sorted(expected.units)), encoding="utf-8")
            (fixture / "unit-states.txt").write_text(
                "\n".join(f"{name} disabled inactive" for name in sorted(expected.units)), encoding="utf-8"
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(
                    [
                        "doctor",
                        "--json",
                        "--host-fixture",
                        str(fixture),
                        "--instance",
                        str(root / "examples" / "instance"),
                    ]
                )
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 1, payload)
        self.assertTrue(any(finding["code"] == "unit_runtime" for finding in payload["findings"]))

    def test_status_json_reports_oneshot_state_and_no_orca_runtime(self):
        # secretary-755: a completed one-shot dispatcher unit must carry real, non-null evidence —
        # not the "unprobed" (None, None) an operator cannot distinguish from "we never looked".
        # A20 step 9 (secretary-1726): Orca's server and its X display, stopped and disabled on
        # this host, are neither expected nor reported.
        root = Path(__file__).resolve().parents[1]
        # The example installation runs this checkout: status compares a host against the units of
        # the product the installation is configured with, not the module's own directory.
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict(os.environ, {"UMMANU_REPO": str(root)}),
        ):
            fixture = Path(tmp)
            report = validate_instance(root / "examples" / "instance")
            expected = build_doctor_expectations(
                report.instance,
                report.bindings,
                packaged=resolve_packaged(
                    report.instance,
                    packaging_root(root),
                    product_root=root,
                    instance_path=root / "examples" / "instance",
                    data_dir=report.data_dir,
                ),
            )
            oneshot = next(
                name
                for name, (need_enabled, need_active) in expected.unit_runtime.items()
                if not need_enabled and not need_active
            )
            (fixture / "units.txt").write_text("\n".join(sorted(expected.units)), encoding="utf-8")
            (fixture / "unit-states.txt").write_text(
                "\n".join(
                    [
                        *(f"{name} enabled active" for name in sorted(expected.units) if name != oneshot),
                        f"{oneshot} static inactive",
                        "orca-server.service disabled inactive",
                        "xvfb.service disabled inactive",
                    ]
                ),
                encoding="utf-8",
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(
                    [
                        "status",
                        "--json",
                        "--host-fixture",
                        str(fixture),
                        "--instance",
                        str(root / "examples" / "instance"),
                    ]
                )
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0, payload)
        oneshot_row = next(row for row in payload["host"]["units"] if row["name"] == oneshot)
        self.assertEqual(oneshot_row["active"], "inactive")
        self.assertEqual(oneshot_row["enabled"], "static")
        self.assertNotIn("external_runtime", payload["host"])
        self.assertNotIn("orca-server", output.getvalue())
        self.assertNotIn("xvfb", output.getvalue())

    def test_status_and_doctor_ignore_stopped_and_absent_orca_units(self):
        # A20 step 9 (secretary-1726): neither status nor doctor expects or prints Orca's server
        # or its X display. Stopped and disabled, or never installed ("not-found", as systemd 255
        # reports it), the unit section and its runtime findings stay green.
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp)
            report = validate_instance(root / "examples" / "instance")
            expected = build_doctor_expectations(
                report.instance,
                report.bindings,
                packaged=resolve_packaged(
                    report.instance,
                    instance_path=root / "examples" / "instance",
                    data_dir=report.data_dir,
                ),
            )
            (fixture / "units.txt").write_text("\n".join(sorted(expected.units)), encoding="utf-8")
            (fixture / "unit-states.txt").write_text(
                "\n".join(
                    [
                        *(f"{name} enabled active" for name in sorted(expected.units)),
                        "orca-server.service disabled inactive",
                        "xvfb.service not-found inactive",
                    ]
                ),
                encoding="utf-8",
            )
            json_output = io.StringIO()
            with contextlib.redirect_stdout(json_output):
                json_code = main(
                    [
                        "status",
                        "--json",
                        "--host-fixture",
                        str(fixture),
                        "--instance",
                        str(root / "examples" / "instance"),
                    ]
                )
            text_output = io.StringIO()
            with contextlib.redirect_stdout(text_output):
                text_code = main(
                    [
                        "doctor",
                        "--host-fixture",
                        str(fixture),
                        "--instance",
                        str(root / "examples" / "instance"),
                    ]
                )
        payload = json.loads(json_output.getvalue())
        self.assertEqual(json_code, 0, payload)
        self.assertEqual(text_code, 1, text_output.getvalue())
        self.assertNotIn("external_runtime", payload["host"])
        self.assertFalse([row for row in payload["host"]["units"] if row.get("active") != "active"])
        # examples/instance's fixture host is otherwise incomplete (missing project checkout),
        # same as test_doctor_json_reports_the_same_missing_host_resource_as_doctor; the unit
        # section is what this scenario pins: matched, no drift, no runtime findings.
        text = text_output.getvalue()
        for foreign in ("orca-server", "xvfb", "Orca runtime"):
            self.assertNotIn(foreign, json_output.getvalue())
            self.assertNotIn(foreign, text)
        self.assertNotIn("unit runtime findings", text)
        units_section = text.split("\nunits:\n", 1)[1].splitlines()[:3]
        self.assertEqual(
            [line.strip() for line in units_section[1:]],
            ["missing-on-host: (none)", "unmanaged-on-host: (none)"],
            text,
        )

    def test_doctor_json_reports_an_unresolved_divergence_even_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            (data_dir / "dispatcher").mkdir(parents=True)
            (data_dir / "dispatcher" / "pilot-state.json").write_text(
                json.dumps({"phase": "cutover_committed", "legacy_decommissioned": True}), encoding="utf-8"
            )
            (data_dir / "dispatcher" / "production-state.json").write_text(
                json.dumps(
                    {
                        "phase": "production",
                        "owner": "ummanu-production",
                        "records": {},
                        "controlled_divergences": [
                            {
                                "id": "div_open0000000001",
                                "pilot_ref": "ummanu-730",
                                "step": "production-recovery",
                                "reason": "active_claim_mismatch",
                                "status": "open",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            instance = Path(tmp) / "instance.yaml"
            instance.write_text(
                "version: 1\nname: test\n"
                f"data_dir: {data_dir}\n"
                "offsite:\n  instance_remote: git@example.invalid:x/y.git\n",
                encoding="utf-8",
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["doctor", "--offline", "--json", "--instance", str(instance)])
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 1, payload)
        dispatcher_findings = [f for f in payload["findings"] if f["code"] == "dispatcher"]
        self.assertTrue(
            any(
                "unresolved controlled divergence" in f["message"] and "ummanu-730" in f["message"]
                for f in dispatcher_findings
            ),
            dispatcher_findings,
        )


class HeadRegistrySourceTests(unittest.TestCase):
    """The checkout the live head registry came from is readable outside the code."""

    def _instance(self, root: Path) -> Path:
        instance = root / "instance.yaml"
        instance.write_text(
            "version: 1\nname: test\n"
            f"data_dir: {root / 'data'}\n"
            "offsite:\n  instance_remote: git@example.invalid:x/y.git\n",
            encoding="utf-8",
        )
        return instance

    def test_status_reports_the_pinned_canon_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = self._instance(root)
            product_root = Path(__file__).resolve().parents[1]
            materialize_snapshot(root, product_root)
            record_source(root, product_root)
            report = validate_instance(instance)

            snapshot = collect_status(report, offline=True)

        registry = snapshot["installation"]["head_registry"]
        self.assertEqual(validate(snapshot, "status", "status.json"), [])
        self.assertIsNone(registry["error"])
        self.assertEqual(registry["product_root"], str(product_root))
        self.assertEqual(registry["revision"], product_revision(product_root))
        # The generated pair's home is the data directory, and there is no legacy source to name.
        self.assertEqual(registry["snapshot"], str(root / "data" / "heads" / "heads.yaml"))
        self.assertNotIn("legacy_source", registry)
        self.assertEqual(registry["canonical_owner"], "product")
        self.assertEqual(
            registry["canonical"],
            str(product_root / "src" / "ummanu" / "runtime" / "heads.toml"),
        )

    def test_status_credits_the_installation_for_a_registry_it_owns(self):
        """The product revision alone would name the wrong file for an installation-owned canon."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = self._instance(root)
            (root / "heads").mkdir()
            (root / "heads" / "heads.toml").write_text(
                '[resources.own]\naccount = "own"\n'
                '[profiles.own-head]\nresource = "own"\nadapter = "claude"\n'
                '[role_defaults]\nnew_card = "own-head"\n',
                encoding="utf-8",
            )
            product_root = Path(__file__).resolve().parents[1]
            materialize_snapshot(root, product_root)
            record_source(root, product_root)
            report = validate_instance(instance)

            snapshot = collect_status(report, offline=True)
            # The snapshot is still what status validates, and it is the one that canon produced.
            materialized = (root / "data" / "heads" / "heads.yaml").read_text(encoding="utf-8")
            canonical = str(root / "heads" / "heads.toml")

        registry = snapshot["installation"]["head_registry"]
        self.assertEqual(validate(snapshot, "status", "status.json"), [])
        self.assertIsNone(registry["error"])
        self.assertEqual(registry["canonical_owner"], "instance")
        self.assertEqual(registry["canonical"], canonical)
        self.assertIn("own-head", materialized)

    def test_status_names_an_installation_with_no_recorded_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = self._instance(root)
            materialize_snapshot(root, Path(__file__).resolve().parents[1])
            report = validate_instance(instance)

            snapshot = collect_status(report, offline=True)

        registry = snapshot["installation"]["head_registry"]
        self.assertIsNone(registry["product_root"])
        self.assertIn("ummanu upgrade", registry["error"])

    def test_status_names_a_broken_installation_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = self._instance(root)
            (root / "data" / "heads").mkdir(parents=True)
            (root / "data" / "heads" / "heads.yaml").write_text("profiles: {}\n", encoding="utf-8")
            report = validate_instance(instance)

            snapshot = collect_status(report, offline=True)

        self.assertIn("[resources] table", snapshot["installation"]["head_registry"]["error"])

    def test_status_never_reads_a_live_root_pair(self):
        """ummanu-39 removed the deploy-skew fallback: a live root's pair is not read, and the error
        names the data directory's pair and `ummanu upgrade` (was: the live root's pair was read and
        named as `legacy_source`)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = self._instance(root)
            product_root = Path(__file__).resolve().parents[1]
            materialize_snapshot(root, product_root)
            record_source(root, product_root)
            (root / "heads").mkdir()
            for name in ("heads.yaml", "source.yaml"):
                (root / "data" / "heads" / name).rename(root / "heads" / name)
            report = validate_instance(instance)

            snapshot = collect_status(report, offline=True)

        registry = snapshot["installation"]["head_registry"]
        self.assertEqual(validate(snapshot, "status", "status.json"), [])
        self.assertNotIn("legacy_source", registry)
        self.assertEqual(registry["snapshot"], str(root / "data" / "heads" / "heads.yaml"))
        self.assertIn(str(root / "data" / "heads" / "heads.yaml"), registry["error"])
        self.assertIn("ummanu upgrade", registry["error"])


class SecretStoreObservabilityTests(unittest.TestCase):
    """`status`/`doctor` surface the store's health without ever showing a value."""

    def _instance_yaml(self, data_dir: Path) -> str:
        return (
            "version: 1\nname: test\n"
            f"data_dir: {data_dir}\n"
            "offsite:\n  instance_remote: git@example.invalid:x/y.git\n"
        )

    def test_status_json_reports_an_initialized_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance_dir = root / "instance"
            data_dir = root / "data"
            _init_instance_repo(instance_dir, self._instance_yaml(data_dir))
            initialize_store(instance_dir, phrase=" ".join(str(n) for n in range(16)), actor="tester")
            set_secret(
                instance_dir,
                secret_id="service.api-token",
                value=b"super-secret-token-value",
                scope="installation",
                purpose="board api",
                environment="EXAMPLE_API_TOKEN",
                materialize={"target": "runtime-env"},
                actor="tester",
            )

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(
                    [
                        "status",
                        "--json",
                        "--offline",
                        "--instance",
                        str(instance_dir / "instance.yaml"),
                    ]
                )

        self.assertEqual(code, 0, output.getvalue())
        payload = json.loads(output.getvalue())
        self.assertEqual(validate(payload, "status", "status.json"), [])
        store = payload["secret_store"]
        self.assertTrue(store["initialized"])
        self.assertEqual(store["secret_count"], 1)
        self.assertIsNotNone(store["last_modified_at"])
        self.assertEqual(store["installation_key"], {"present": True, "usable": True})
        self.assertEqual(store["materialize"], [{"target": "runtime-env", "path": None, "count": 1}])
        # The point of the section: never a value, never the recovery phrase.
        self.assertNotIn("super-secret-token-value", output.getvalue())

    def test_status_json_reports_an_absent_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance_dir = root / "instance"
            data_dir = root / "data"
            _init_instance_repo(instance_dir, self._instance_yaml(data_dir))

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(
                    [
                        "status",
                        "--json",
                        "--offline",
                        "--instance",
                        str(instance_dir / "instance.yaml"),
                    ]
                )

        self.assertEqual(code, 0, output.getvalue())
        payload = json.loads(output.getvalue())
        self.assertEqual(validate(payload, "status", "status.json"), [])
        self.assertEqual(
            payload["secret_store"],
            {
                "initialized": False,
                "secret_count": 0,
                "last_modified_at": None,
                "installation_key": {"present": False, "usable": None},
                "materialize": [],
            },
        )

    def assert_only_the_work_tree_finding(self, code: int, output: str) -> None:
        """`_init_instance_repo` makes the live root a Git work tree, which doctor reds since ummanu-39
        (`live_root.git_work_tree`); green apart from that one finding."""
        self.assertEqual(code, 1, output)
        self.assertEqual(output.count("live_root."), 1, output)
        self.assertIn("live_root.git_work_tree: ", output)
        self.assertIn("status: findings", output)

    def test_doctor_stays_green_when_there_is_no_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance_dir = root / "instance"
            data_dir = root / "data"
            _init_instance_repo(instance_dir, self._instance_yaml(data_dir))

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(
                    [
                        "doctor",
                        "--dry-run",
                        "--offline",
                        "--instance",
                        str(instance_dir / "instance.yaml"),
                    ]
                )

        self.assert_only_the_work_tree_finding(code, output.getvalue())
        self.assertNotIn("secret store findings", output.getvalue())

    def test_doctor_stays_green_when_the_store_is_healthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance_dir = root / "instance"
            data_dir = root / "data"
            _init_instance_repo(instance_dir, self._instance_yaml(data_dir))
            initialize_store(instance_dir, phrase=" ".join(str(n) for n in range(16)), actor="tester")
            set_secret(
                instance_dir,
                secret_id="service.api-token",
                value=b"token-value",
                scope="installation",
                purpose="board api",
                actor="tester",
            )

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(
                    [
                        "doctor",
                        "--dry-run",
                        "--offline",
                        "--instance",
                        str(instance_dir / "instance.yaml"),
                    ]
                )

        self.assert_only_the_work_tree_finding(code, output.getvalue())
        self.assertNotIn("secret store findings", output.getvalue())

    def test_doctor_reports_a_broken_store_as_a_finding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance_dir = root / "instance"
            data_dir = root / "data"
            _init_instance_repo(instance_dir, self._instance_yaml(data_dir))
            initialize_store(instance_dir, phrase=" ".join(str(n) for n in range(16)), actor="tester")
            set_secret(
                instance_dir,
                secret_id="service.api-token",
                value=b"token-value",
                scope="installation",
                purpose="board api",
                actor="tester",
            )
            (instance_dir / "secrets" / "installation.key").unlink()

            text_output = io.StringIO()
            with contextlib.redirect_stdout(text_output):
                text_code = main(
                    [
                        "doctor",
                        "--dry-run",
                        "--offline",
                        "--instance",
                        str(instance_dir / "instance.yaml"),
                    ]
                )
            json_output = io.StringIO()
            with contextlib.redirect_stdout(json_output):
                json_code = main(
                    [
                        "doctor",
                        "--dry-run",
                        "--offline",
                        "--json",
                        "--instance",
                        str(instance_dir / "instance.yaml"),
                    ]
                )

        self.assertEqual(text_code, 1, text_output.getvalue())
        self.assertIn("secret store findings:", text_output.getvalue())
        self.assertIn("installation key is missing or unusable", text_output.getvalue())
        payload = json.loads(json_output.getvalue())
        self.assertEqual(json_code, 1, payload)
        self.assertTrue(any(f["code"] == "secret_store" for f in payload["findings"]))

    def test_doctor_does_not_leak_key_material_from_a_corrupted_version_field(self):
        """A key-params file whose `version` field holds the raw installation
        key (e.g. from tampering, or a botched manual edit) must not have that
        value echoed back by `doctor`, in either the text or JSON report."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance_dir = root / "instance"
            data_dir = root / "data"
            _init_instance_repo(instance_dir, self._instance_yaml(data_dir))
            initialize_store(instance_dir, phrase=" ".join(str(n) for n in range(16)), actor="tester")
            set_secret(
                instance_dir,
                secret_id="service.api-token",
                value=b"token-value",
                scope="installation",
                purpose="board api",
                actor="tester",
            )
            raw_key = (instance_dir / "secrets" / "installation.key").read_text(encoding="utf-8").strip()
            params_path = instance_dir / "secrets" / "installation-key.json"
            params = json.loads(params_path.read_text(encoding="utf-8"))
            params["version"] = raw_key
            params_path.write_text(json.dumps(params), encoding="utf-8")

            text_output = io.StringIO()
            with contextlib.redirect_stdout(text_output):
                text_code = main(
                    [
                        "doctor",
                        "--dry-run",
                        "--offline",
                        "--instance",
                        str(instance_dir / "instance.yaml"),
                    ]
                )
            json_output = io.StringIO()
            with contextlib.redirect_stdout(json_output):
                json_code = main(
                    [
                        "doctor",
                        "--dry-run",
                        "--offline",
                        "--json",
                        "--instance",
                        str(instance_dir / "instance.yaml"),
                    ]
                )

        self.assertEqual(text_code, 1, text_output.getvalue())
        self.assertNotIn(raw_key, text_output.getvalue())
        self.assertIn("installation key is missing or unusable", text_output.getvalue())
        self.assertEqual(json_code, 1, json_output.getvalue())
        self.assertNotIn(raw_key, json_output.getvalue())

    def test_doctor_reports_a_malformed_catalog_as_a_finding_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance_dir = root / "instance"
            data_dir = root / "data"
            _init_instance_repo(instance_dir, self._instance_yaml(data_dir))
            initialize_store(instance_dir, phrase=" ".join(str(n) for n in range(16)), actor="tester")
            (instance_dir / "secrets" / "catalog.yaml").write_text("bad: catalog\n", encoding="utf-8")
            (instance_dir / "secrets" / "installation-key.json").write_text("{}", encoding="utf-8")

            text_output = io.StringIO()
            with contextlib.redirect_stdout(text_output):
                text_code = main(
                    [
                        "doctor",
                        "--dry-run",
                        "--offline",
                        "--instance",
                        str(instance_dir / "instance.yaml"),
                    ]
                )
            json_output = io.StringIO()
            with contextlib.redirect_stdout(json_output):
                json_code = main(
                    [
                        "doctor",
                        "--dry-run",
                        "--offline",
                        "--json",
                        "--instance",
                        str(instance_dir / "instance.yaml"),
                    ]
                )
            status_output = io.StringIO()
            with contextlib.redirect_stdout(status_output):
                status_code = main(
                    [
                        "status",
                        "--json",
                        "--offline",
                        "--instance",
                        str(instance_dir / "instance.yaml"),
                    ]
                )

        self.assertEqual(text_code, 1, text_output.getvalue())
        self.assertIn("secret store findings:", text_output.getvalue())
        payload = json.loads(json_output.getvalue())
        self.assertEqual(json_code, 1, payload)
        self.assertTrue(any(f["code"] == "secret_store" for f in payload["findings"]))
        self.assertEqual(status_code, 0, status_output.getvalue())


class HeadlessWorkStatusTests(unittest.TestCase):
    """An In progress column is not evidence of active work (secretary-1544).

    A card whose worker the dispatcher cannot name reads as degraded in both projections an
    operator has: the per-card attempt row, and the sprint summary that counts it.
    """

    EPISODE: ClassVar[dict] = {
        "since": 1_700_000_000.0,
        "record_state": "adopted",
        "handle_known": False,
        "heartbeat": "absent",
        "workspace": "/work/ummanu-1232",
        "branch": "pipeline/codegen-orchestrator-1232",
        "expected_branch": "pipeline/codegen-orchestrator-1232",
        "dirty": False,
        "candidate_sha": "6cc7ca0c8cdf0719629e1e01bb5c72614983d7ef",
        "report_generation": 1,
        "recovery_error": "round_already_answered",
    }

    def test_the_attempt_row_says_headless_with_the_evidence_a_recovery_holds(self) -> None:
        from ummanu.status import _attempts

        rows = _attempts(
            {"records": {"codegen-orchestrator-1232": {"state": "adopted", "worker_headless": self.EPISODE}}},
            probe_panels=False,
        )

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertTrue(row["degraded"])
        headless = row["headless"]
        self.assertEqual(headless["state"], "adopted")
        self.assertFalse(headless["handle_known"])
        self.assertEqual(headless["heartbeat"], "absent")
        self.assertEqual(headless["workspace"], "/work/ummanu-1232")
        self.assertEqual(headless["branch"], "pipeline/codegen-orchestrator-1232")
        self.assertEqual(headless["candidate_sha"], "6cc7ca0c8cdf0719629e1e01bb5c72614983d7ef")
        self.assertFalse(headless["dirty"])
        self.assertEqual(headless["recovery_error"], "round_already_answered")
        self.assertGreater(headless["waiting_seconds"], 0)

    def test_a_card_with_a_worker_is_not_degraded(self) -> None:
        from ummanu.status import _attempts

        rows = _attempts(
            {"records": {"ummanu-1": {"state": "claimed", "handle": "term:1"}}}, probe_panels=False
        )

        self.assertIsNone(rows[0]["headless"])
        self.assertFalse(rows[0]["degraded"])

    def test_the_sprint_summary_names_only_its_own_degraded_cards(self) -> None:
        from ummanu.sprints import SprintReader

        sprint = {
            "ref": "sprint:1416",
            "goal": "g",
            "status": "open",
            "current_task": None,
            "budget": {},
            "resume_freshness": {},
            "cards": [
                {"ref": "codegen-orchestrator-1232", "state": "in_progress"},
                {"ref": "codegen-orchestrator-1233", "state": "in_progress"},
            ],
        }
        headless = {
            "codegen-orchestrator-1232": dict(self.EPISODE),
            # A headless card belonging to some other sprint is not this sprint's degradation.
            "ummanu-9999": dict(self.EPISODE),
        }

        summary = SprintReader._status(  # type: ignore[misc]
            SprintReader.__new__(SprintReader), sprint, None, headless
        )

        self.assertEqual(list(summary["degraded_cards"]), ["codegen-orchestrator-1232"])
        self.assertEqual(
            summary["degraded_cards"]["codegen-orchestrator-1232"]["candidate_sha"],
            "6cc7ca0c8cdf0719629e1e01bb5c72614983d7ef",
        )
