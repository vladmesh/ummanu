"""The transport-independent read layer: cursors, the five agent states, and degraded sources.

Every test here is hermetic in the strong sense the card asks for: no live Orca, no network, no
live board, no real worker. The board is a throwaway card store (`tests/sql_backend_fixtures.py`),
installation health is a value the test
supplies, and the agents are heartbeat records written into a temporary directory -- which is
exactly the point, because a dashboard that could only be tested against production would be
tested against production.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import source_trees
from tests.fakes.dispatcher import dispatcher_seed
from tests.sql_backend_fixtures import card_store, terminate_session
from ummanu.cli import main
from ummanu.config import validate
from ummanu.runtime.head.identity import publish_heartbeat
from ummanu.tasks import TaskError, task_audit_for
from ummanu.webproto import agents as agent_reads
from ummanu.webproto.cursor import Cursor
from ummanu.webproto.errors import InvalidCursor, TaskNotFound
from ummanu.webproto.reads import ReadLayer

REPO_ROOT = Path(__file__).resolve().parents[1]

HEALTH = {"schema_version": 1, "installation": {"name": "test"}}


def _instance(root: Path, *, projects: tuple[str, ...] = ("ummanu",)) -> Path:
    """A validating instance whose data plane is under ``root``."""
    instance_dir = root / "instance"
    (instance_dir / "projects").mkdir(parents=True)
    data_dir = root / "data"
    (data_dir / "dispatcher").mkdir(parents=True)
    (data_dir / "board").mkdir(parents=True)
    (instance_dir / "instance.yaml").write_text(
        "version: 1\nname: test\n"
        f"data_dir: {data_dir}\n"
        "offsite:\n  instance_remote: git@example.invalid:x/y.git\n",
        encoding="utf-8",
    )
    for project in projects:
        (instance_dir / "projects" / f"{project}.yaml").write_text(
            f"id: {project}\nrepo: /projects/{project}\nenabled: true\n"
            f"adapter: {project}\ndefault_branch: main\n",
            encoding="utf-8",
        )
    return instance_dir


def _journal(board, records: list[dict]) -> None:
    """Commit records to the card audit (`requests`), in order, as their writers commit them."""
    audit = task_audit_for(board)
    for record in records:
        audit.append(record["request_id"], record)


def _event(ref: str, kind: str, *, ordinal: int) -> dict:
    return {
        "schema_version": 2,
        "record_type": "board.protocol_event",
        "request_id": f"req-{ref}-{ordinal}",
        "event_id": f"evt-{ref}-{ordinal}",
        "kind": kind,
        "subject": {"kind": "card", "ref": ref},
        "ref": ref,
        "occurred_at": "2026-09-06T00:00:00Z",
        "actor": {"role": "dispatcher", "id": "ummanu-production"},
        "reason": f"event {ordinal}",
        "related_refs": [],
        "data": {},
    }


def _routing(ref: str, attempt: int, phase: str, heads: list[dict], *, ordinal: int) -> dict:
    """One routing telemetry record as `TaskWriter.routing` commits it: a generic audit record."""
    return {
        "event_id": f"evt-{ref}-{ordinal}",
        "schema_version": 1,
        "kind": "routing",
        "occurred_at": "2026-09-06T00:00:00Z",
        "outcome": "success",
        "actor": {"role": "dispatcher", "id": "ummanu-production"},
        "ref": ref,
        "request_id": f"req-{ref}-{ordinal}",
        "payload": {
            "attempt": attempt,
            "attempt_id": f"attempt-{attempt}",
            "phase": phase,
            "outcome": "",
            "heads": heads,
        },
    }


def _usage(ref: str, *, ordinal: int, attempt: int, **resolved: object) -> dict:
    """One committed `attempt.usage` occurrence of a worker phase that named its models."""
    empty = dict.fromkeys(("input", "cache_input", "cache_read_input", "output", "reasoning"))
    record = _event(ref, "attempt.usage", ordinal=ordinal)
    record["data"] = {
        "attempt": attempt,
        "attempt_id": f"attempt-{attempt}",
        "phase": "worker",
        "role": "worker",
        "report_generation": 1,
        "head": "claude-opus",
        "adapter": "claude",
        "model": "opus",
        "model_source": "profile",
        "session_id": "session-1",
        "session_id_reason": "",
        "launch_id": "run-1",
        "outcome": "usage_absent",
        "detail": "",
        "source_kind": "claude_session_jsonl",
        "records": 0,
        "skipped_records": 0,
        "tokens": dict(empty),
        "session_totals": dict(empty),
        "phase_baseline": dict(empty),
        **resolved,
    }
    return record


def _production(data_dir: Path, records: dict) -> None:
    (data_dir / "dispatcher" / "production-state.json").write_text(
        json.dumps({"phase": "production", "records": records}), encoding="utf-8"
    )


def _dead_pid() -> int:
    """A pid that names no process, so a heartbeat pointing at it classifies as dead.

    Searched downwards from the pid ceiling because those numbers are the last the kernel hands
    out, so the answer stays dead for the length of a test.
    """
    ceiling = int(Path("/proc/sys/kernel/pid_max").read_text(encoding="utf-8").strip())
    for candidate in range(ceiling - 1, 1, -1):
        try:
            os.kill(candidate, 0)
        except ProcessLookupError:
            return candidate
        except PermissionError:
            continue
    raise unittest.SkipTest("this host has no free pid to prove a dead heartbeat with")


class ReadLayerFixture(unittest.TestCase):
    """One instance, one fake board, and a layer built over both."""

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.instance = _instance(self.tmp)
        self.data_dir = self.tmp / "data"
        self.board = card_store(self, dispatcher_seed(), instance_dir=self.data_dir)
        _production(self.data_dir, {})

    def layer(self, **kwargs) -> ReadLayer:
        options = {
            "board_client": self.board,
            "status_reader": lambda: HEALTH,
            "clock": lambda: 1788652800.0,
        }
        options.update(kwargs)
        return ReadLayer(self.instance, **options)


class CursorTests(ReadLayerFixture):
    def test_paging_a_cursor_reads_every_event_exactly_once(self) -> None:
        _journal(
            self.board,
            [_event("ummanu-1", "card.started", ordinal=index) for index in range(5)],
        )
        # Another card's events are interleaved: a position must survive them without shifting.
        _journal(self.board, [_event("ummanu-2", "card.started", ordinal=99)])
        _journal(
            self.board,
            [_event("ummanu-1", "card.reported", ordinal=index) for index in range(5, 9)],
        )
        layer = self.layer()
        seen: list[str] = []
        cursor = None
        for _ in range(20):
            page = layer.task_events("ummanu-1", cursor, limit=2)
            seen.extend(item["event_id"] for item in page["items"])
            cursor = page["next_cursor"]
            if not page["items"]:
                break
        self.assertEqual(seen, [f"evt-ummanu-1-{index}" for index in range(9)])
        self.assertEqual(len(seen), len(set(seen)))

    def test_the_same_cursor_twice_is_the_same_page(self) -> None:
        _journal(self.board, [_event("ummanu-1", "card.started", ordinal=i) for i in range(6)])
        layer = self.layer()
        first = layer.task_events("ummanu-1", None, limit=3)
        again = layer.task_events("ummanu-1", None, limit=3)
        self.assertEqual(first["items"], again["items"])
        resumed = layer.task_events("ummanu-1", first["next_cursor"], limit=3)
        repeated = layer.task_events("ummanu-1", first["next_cursor"], limit=3)
        self.assertEqual(resumed["items"], repeated["items"])
        self.assertEqual(
            [item["event_id"] for item in resumed["items"]],
            [f"evt-ummanu-1-{index}" for index in (3, 4, 5)],
        )

    def test_a_cursor_issued_before_new_events_returns_exactly_those_events(self) -> None:
        _journal(self.board, [_event("ummanu-1", "card.started", ordinal=0)])
        layer = self.layer()
        page = layer.task_events("ummanu-1", None, limit=50)
        self.assertEqual(len(page["items"]), 1)
        self.assertFalse(page["has_more"])
        cursor = page["next_cursor"]
        # Nothing new yet: the same cursor is an empty page, not a repeat and not an error.
        self.assertEqual(layer.task_events("ummanu-1", cursor, limit=50)["items"], [])
        _journal(
            self.board,
            [
                _event("ummanu-2", "card.started", ordinal=7),
                _event("ummanu-1", "card.moved", ordinal=1),
            ],
        )
        after = layer.task_events("ummanu-1", cursor, limit=50)
        self.assertEqual([item["event_id"] for item in after["items"]], ["evt-ummanu-1-1"])

    def test_a_cursor_from_another_card_is_refused_by_name(self) -> None:
        _journal(self.board, [_event("ummanu-1", "card.started", ordinal=0)])
        layer = self.layer()
        foreign = layer.task_events("ummanu-1", None, limit=1)["next_cursor"]
        with self.assertRaises(InvalidCursor) as refused:
            layer.task_events("ummanu-2", foreign, limit=1)
        self.assertIn("ummanu-1", str(refused.exception))
        with self.assertRaises(InvalidCursor):
            layer.task_events("ummanu-1", "not-a-cursor", limit=1)

    def test_a_cursor_past_the_end_of_the_journal_is_refused_not_reset(self) -> None:
        _journal(self.board, [_event("ummanu-1", "card.started", ordinal=0)])
        beyond = Cursor(ref="ummanu-1", offset=10_000_000).encode()
        with self.assertRaises(InvalidCursor):
            self.layer().task_events("ummanu-1", beyond, limit=1)

    def test_an_unreadable_audit_is_a_source_fact_and_keeps_the_cursor(self) -> None:
        _journal(self.board, [_event("ummanu-1", "card.started", ordinal=0)])
        layer = self.layer()
        cursor = layer.task_events("ummanu-1", None, limit=1)["next_cursor"]
        refusal = TaskError("backend_unavailable", "the board store could not answer a read", 1)
        with mock.patch.object(self.board, "_query", side_effect=refusal):
            page = layer.task_events("ummanu-1", cursor, limit=1)
        self.assertEqual(page["source"]["state"], "unavailable")
        self.assertIn("could not be read", page["source"]["reason"])
        self.assertEqual(page["items"], [])
        self.assertEqual(page["next_cursor"], cursor)

    def test_generic_audit_records_are_events_too(self) -> None:
        """A history with the transitions in it and the creations missing would not be a history."""
        _journal(
            self.board,
            [
                {
                    "schema_version": 1,
                    "event_id": "evt-generic",
                    "kind": "created",
                    "outcome": "success",
                    "ref": "ummanu-1",
                    "request_id": "req-generic",
                    "occurred_at": "2026-09-06T00:00:00Z",
                    "actor": {"role": "observer", "id": "observer"},
                    "payload": {"project": "ummanu"},
                },
                _event("ummanu-1", "card.started", ordinal=0),
            ],
        )
        items = self.layer().task_events("ummanu-1", None, limit=10)["items"]
        self.assertEqual([item["typed"] for item in items], [False, True])
        self.assertEqual(items[0]["outcome"], "success")
        self.assertEqual(items[0]["data"], {"project": "ummanu"})
        self.assertEqual(items[1]["reason"], "event 0")


class AgentStateTests(ReadLayerFixture):
    """The four states of criterion 3, told apart, and none of them read from a window."""

    def _record(self, ref: str, **overrides) -> dict:
        record = {
            "attempt_id": "attempt-1",
            "head": "codex-worker",
            "state": "validate",
            "workspace": str(self.tmp / "workspace"),
            "worker_pid_file": str(self.tmp / f"{ref}.pid"),
            "worker_head_run": {"run_id": f"run-{ref}", "lifecycle": "working"},
        }
        record.update(overrides)
        return record

    def _states(self, records: dict) -> dict[str, dict]:
        _production(self.data_dir, records)
        snapshot = self.layer().system_snapshot()
        self.assertEqual(snapshot["agents"]["source"]["state"], "available")
        return {row["ref"]: row for row in snapshot["agents"]["items"]}

    def test_a_live_matching_process_is_running(self) -> None:
        pid_file = str(self.tmp / "live.pid")
        publish_heartbeat(pid_file, {"run_id": "run-live", "role": "worker", "task": "card:ummanu-1"})
        rows = self._states(
            {
                "ummanu-1": self._record(
                    "ummanu-1",
                    worker_pid_file=pid_file,
                    worker_head_run={
                        "run_id": "run-live",
                        "lifecycle": "working",
                        "task_ref": {"kind": "card", "ref": "ummanu-1"},
                    },
                )
            }
        )
        self.assertEqual(rows["ummanu-1"]["state"], "running")
        self.assertEqual(rows["ummanu-1"]["evidence"]["heartbeat_state"], "live-match")

    def test_a_confirmed_stop_is_finished_and_a_missing_process_is_a_failure(self) -> None:
        dead = self.tmp / "dead.pid"
        dead.write_text(
            json.dumps(
                {
                    "version": 1,
                    "pid": _dead_pid(),
                    "boot_id": "boot",
                    "proc_starttime_ticks": "42",
                    "run_id": "run-ummanu-2",
                    "role": "worker",
                    "task": "ummanu-2",
                }
            ),
            encoding="utf-8",
        )
        rows = self._states(
            {
                # A run whose stop the dispatcher confirmed: an ending, whatever the heartbeat says.
                "ummanu-1": self._record(
                    "ummanu-1",
                    worker_head_run={"run_id": "run-1", "lifecycle": "exited"},
                ),
                # A run that still expects a process, and there is none.
                "ummanu-2": self._record("ummanu-2", worker_pid_file=str(dead)),
            }
        )
        self.assertEqual(rows["ummanu-1"]["state"], "finished")
        self.assertEqual(rows["ummanu-2"]["state"], "process_failed")
        self.assertIn("expects a process", rows["ummanu-2"]["reason"])

    def test_an_unreadable_heartbeat_is_a_source_failure_not_a_dead_head(self) -> None:
        broken = self.tmp / "broken.pid"
        broken.write_text("{not json", encoding="utf-8")
        rows = self._states({"ummanu-1": self._record("ummanu-1", worker_pid_file=str(broken))})
        self.assertEqual(rows["ummanu-1"]["state"], "source_unavailable")
        self.assertIn("could not be read", rows["ummanu-1"]["reason"])

    def test_no_evidence_yet_is_unknown_rather_than_absent(self) -> None:
        rows = self._states(
            {
                "ummanu-1": self._record("ummanu-1"),
                "ummanu-2": self._record("ummanu-2", worker_head_run={}),
            }
        )
        self.assertEqual(rows["ummanu-1"]["state"], "unknown")
        self.assertIn("has not published a launch heartbeat", rows["ummanu-1"]["reason"])
        self.assertEqual(rows["ummanu-2"]["state"], "unknown")
        self.assertIn("no durable run", rows["ummanu-2"]["reason"])

    def test_a_pane_is_never_evidence_that_an_agent_is_alive(self) -> None:
        """The card's own invariant: a window that exists says nothing about a process."""
        rows = self._states(
            {
                "ummanu-1": self._record(
                    "ummanu-1",
                    handle="pane-1",
                    worker_leaf="leaf-1",
                    review_handle="pane-2",
                    review_leaf="leaf-2",
                    review_head="claude-reviewer",
                    review_head_run={"run_id": "run-review", "lifecycle": "working"},
                )
            }
        )
        self.assertEqual(rows["ummanu-1"]["state"], "unknown")
        self.assertNotIn("running", {rows["ummanu-1"]["state"]})

    def test_both_roles_are_reported_and_named(self) -> None:
        _production(
            self.data_dir,
            {
                "ummanu-1": self._record(
                    "ummanu-1",
                    review_pid_file=str(self.tmp / "review.pid"),
                    review_head="claude-reviewer",
                    review_head_run={"run_id": "run-review", "lifecycle": "working"},
                )
            },
        )
        rows = self.layer().system_snapshot()["agents"]["items"]
        self.assertEqual([row["role"] for row in rows], ["worker", "reviewer"])
        self.assertEqual(rows[1]["head"], "claude-reviewer")


class DegradedSourceTests(ReadLayerFixture):
    def test_an_unreadable_dispatcher_state_does_not_drop_the_system_snapshot(self) -> None:
        (self.data_dir / "dispatcher" / "production-state.json").unlink()
        snapshot = self.layer().system_snapshot()
        self.assertEqual(snapshot["agents"]["source"]["state"], "unavailable")
        self.assertIn("production state could not be read", snapshot["agents"]["source"]["reason"])
        self.assertEqual(snapshot["agents"]["items"], [])
        # Everything whose source did answer is still there.
        self.assertEqual(snapshot["projects"]["source"]["state"], "available")
        self.assertEqual([project["id"] for project in snapshot["projects"]["items"]], ["ummanu"])
        self.assertEqual(snapshot["tasks"]["source"]["state"], "available")
        self.assertEqual(snapshot["installation"]["health"]["source"]["state"], "available")

    def test_a_board_that_will_not_answer_is_reported_with_its_reason(self) -> None:
        class RefusingBoard:
            instance_dir = Path("/nonexistent")

            def call(self, method, **params):
                from ummanu.tasks import TaskError

                raise TaskError("backend_unavailable", "board store is not answering", 1)

            def call_batch(self, calls):
                return []

        snapshot = self.layer(board_client=RefusingBoard()).system_snapshot()
        self.assertEqual(snapshot["tasks"]["source"]["state"], "unavailable")
        self.assertIn("board store is not answering", snapshot["tasks"]["source"]["reason"])
        self.assertEqual(snapshot["tasks"]["items"], [])
        self.assertEqual(snapshot["agents"]["source"]["state"], "available")

    def test_a_restarted_board_store_is_read_again_by_the_same_long_lived_layer(self) -> None:
        # The web service keeps one layer, and so one board client, for its whole life.
        layer = self.layer()
        before = layer.system_snapshot()["tasks"]
        self.assertEqual(before["source"]["state"], "available")
        terminate_session(self.board)

        # The pool finds the terminated connection hung up and never hands it out (§5.6).
        recovered = layer.system_snapshot()["tasks"]
        self.assertEqual(recovered["source"]["state"], "available")
        self.assertEqual(recovered["items"], before["items"])

    def test_health_that_cannot_be_collected_carries_a_reason_and_the_age_of_what_is_left(
        self,
    ) -> None:
        def refuse():
            raise OSError("the host inventory timed out")

        state = self.data_dir / "dispatcher" / "production-state.json"
        os.utime(state, (1788652800.0 - 600, 1788652800.0 - 600))
        health = self.layer(status_reader=refuse).system_snapshot()["installation"]["health"]
        self.assertEqual(health["source"]["state"], "unavailable")
        self.assertIn("the host inventory timed out", health["source"]["reason"])
        self.assertIsNone(health["status"])
        self.assertEqual(health["source"]["data_age_seconds"], 600.0)
        self.assertEqual(health["source"]["observed_at"], "2026-09-05T23:50:00Z")

    def test_an_invalid_instance_is_a_typed_refusal_not_a_snapshot(self) -> None:
        (self.instance / "instance.yaml").write_text("version: 1\n", encoding="utf-8")
        from ummanu.webproto.errors import InstallationUnavailable

        with self.assertRaises(InstallationUnavailable):
            self.layer().system_snapshot()


class TaskSnapshotTests(ReadLayerFixture):
    def _card(self) -> None:
        self.board.replace_comments(12, [
            {"date_creation": 1, "comment": "[report:done]\nthe work is done"},
            {"date_creation": 2, "comment": "[review:green]\nno blockers"},
            {"date_creation": 3, "comment": "[decision:release]\nship it"},
        ])

    def test_a_task_snapshot_carries_state_project_events_and_result(self) -> None:
        self._card()
        _journal(self.board, [_event("ummanu-510", "card.started", ordinal=0)])
        _production(
            self.data_dir,
            {
                "ummanu-510": {
                    "attempt_id": "attempt-1",
                    "state": "validate",
                    "head": "codex-worker",
                    "gate_state": "green",
                    "workspace": str(self.tmp / "workspace"),
                    "worker_pid_file": str(self.tmp / "worker.pid"),
                    "worker_head_run": {"run_id": "run-1", "lifecycle": "working"},
                }
            },
        )
        snapshot = self.layer().task_snapshot("ummanu-510")
        self.assertEqual(snapshot["card"]["value"]["state"], "ready")
        self.assertEqual(
            snapshot["project"],
            {
                "id": "ummanu",
                "registered": True,
                "binding": {
                    "repo": "/projects/ummanu",
                    "adapter": "ummanu",
                    "default_branch": "main",
                    "enabled": True,
                },
            },
        )
        self.assertEqual(snapshot["attempt"]["value"]["gate_state"], "green")
        self.assertEqual([row["role"] for row in snapshot["agents"]["items"]], ["worker"])
        self.assertEqual(snapshot["agents"]["items"][0]["project"], "ummanu")
        self.assertEqual(snapshot["work"]["worker_report"]["value"], "done")
        self.assertEqual(snapshot["work"]["review_verdict"]["value"], "green")
        self.assertEqual(snapshot["work"]["decision"]["value"], "release")
        self.assertEqual(snapshot["work"]["outcome"]["kind"], "decision")
        self.assertFalse(snapshot["work"]["outcome"]["terminal"])
        self.assertEqual(len(snapshot["events"]["items"]), 1)
        # The tail's cursor continues at the end of the journal, so a client polling with it is
        # never handed an event it was just shown.
        self.assertEqual(
            self.layer().task_events("ummanu-510", snapshot["events"]["next_cursor"])["items"],
            [],
        )

    def test_heads_say_what_each_run_was_launched_with_and_what_actually_ran(self) -> None:
        """One row per run: the configured alias beside the resolved model, joined by launch id."""
        first = {
            "role": "worker",
            "head": "claude-opus",
            "adapter": "claude",
            "model": "opus",
            "model_source": "profile",
            "effort": "medium",
            "launch_id": "run-0",
        }
        worker = {**first, "effort": "high", "launch_id": "run-1"}
        reviewer = {
            "role": "reviewer",
            "head": "codex",
            "adapter": "codex",
            "model": "gpt-5.6-terra",
            "model_source": "profile",
            "effort": "xhigh",
            "launch_id": "run-2",
        }
        _journal(
            self.board,
            [
                _routing("ummanu-510", 1, "worker", [first], ordinal=1),
                # The first run's occurrence names another model; it stays on that run's row.
                _usage(
                    "ummanu-510",
                    ordinal=2,
                    attempt=1,
                    launch_id="run-0",
                    resolved_model="claude-fable-5-1",
                    resolved_models=["claude-fable-5-1"],
                    resolved_effort="medium",
                ),
                _routing("ummanu-510", 2, "worker", [worker], ordinal=3),
                _usage(
                    "ummanu-510",
                    ordinal=4,
                    attempt=2,
                    launch_id="run-1",
                    resolved_model="claude-opus-5-5",
                    resolved_models=["claude-haiku-4-5", "claude-opus-5-5"],
                    resolved_effort="high",
                ),
                _routing("ummanu-510", 2, "review", [reviewer], ordinal=5),
            ],
        )
        _production(
            self.data_dir,
            {
                "ummanu-510": {
                    "attempt_id": "attempt-2",
                    "state": "validate",
                    "head": "claude-opus",
                    "worker_pid_file": str(self.tmp / "worker.pid"),
                    "worker_head_run": {"run_id": "run-1", "lifecycle": "working"},
                    "worker_run": worker,
                }
            },
        )

        snapshot = self.layer().task_snapshot("ummanu-510")

        self.assertEqual(validate(snapshot, "web-read", "task"), [])
        self.assertEqual(snapshot["heads"]["source"]["state"], "available")
        rows = {row["run_id"]: row for row in snapshot["heads"]["items"]}
        self.assertEqual(set(rows), {"run-0", "run-1", "run-2"})
        self.assertEqual(
            {key: rows["run-1"][key] for key in ("role", "attempt", "head", "adapter", "model", "model_source", "effort")},
            {
                "role": "worker",
                "attempt": 2,
                "head": "claude-opus",
                "adapter": "claude",
                "model": "opus",
                "model_source": "profile",
                "effort": "high",
            },
        )
        self.assertEqual(
            {key: rows["run-1"][key] for key in ("resolved_model", "resolved_models", "resolved_effort", "resolved_report_generation")},
            {
                "resolved_model": "claude-opus-5-5",
                "resolved_models": ["claude-haiku-4-5", "claude-opus-5-5"],
                "resolved_effort": "high",
                "resolved_report_generation": 1,
            },
        )
        self.assertTrue(rows["run-1"]["current"])
        # The earlier run keeps what it resolved; nothing of it leaks onto the later run.
        self.assertEqual((rows["run-0"]["attempt"], rows["run-0"]["resolved_model"]), (1, "claude-fable-5-1"))
        self.assertFalse(rows["run-0"]["current"])
        # The reviewer launched and has not finished a phase yet: configured, nothing resolved.
        self.assertEqual(rows["run-2"]["effort"], "xhigh")
        self.assertIsNone(rows["run-2"]["resolved_model"])
        self.assertEqual(rows["run-2"]["resolved_models"], [])

    def test_an_occurrence_written_before_the_resolved_fields_resolves_nothing(self) -> None:
        worker = {
            "role": "worker",
            "head": "codex",
            "adapter": "codex",
            "model": "gpt-5.6-terra",
            "launch_id": "run-1",
        }
        _journal(
            self.board,
            [
                _routing("ummanu-510", 1, "worker", [worker], ordinal=1),
                _usage("ummanu-510", ordinal=2, attempt=1, launch_id="run-1"),
            ],
        )

        (row,) = self.layer().task_snapshot("ummanu-510")["heads"]["items"]

        self.assertEqual(
            (row["model"], row["resolved_model"], row["resolved_effort"]), ("gpt-5.6-terra", None, None)
        )
        self.assertEqual(row["resolved_report_generation"], 1)

    def test_a_blocked_report_keeps_its_classification(self) -> None:
        self.board.replace_comments(12, [
            {
                "date_creation": 1,
                "comment": "[report:blocked]\nclassification: external_fact\n\nthe dependency is down",
            }
        ])
        work = self.layer().task_snapshot("ummanu-510")["work"]
        self.assertEqual(work["worker_report"]["classification"], "external_fact")
        self.assertEqual(
            work["outcome"],
            {"kind": "report", "value": "blocked", "at": work["worker_report"]["at"], "terminal": False},
        )

    def test_an_unknown_reference_is_a_typed_not_found(self) -> None:
        with self.assertRaises(TaskNotFound):
            self.layer().task_snapshot("ummanu-does-not-exist")

    def test_a_task_snapshot_survives_a_board_that_will_not_answer(self) -> None:
        """The card read is refused and the audit of the same store still answers."""
        _journal(self.board, [_event("ummanu-1", "card.started", ordinal=0)])
        refusal = TaskError("backend_error", "the board refused the read request", 1)
        with mock.patch.object(self.board, "call", side_effect=refusal):
            snapshot = self.layer().task_snapshot("ummanu-1")
        self.assertEqual(snapshot["card"]["source"]["state"], "unavailable")
        self.assertIsNone(snapshot["card"]["value"])
        self.assertEqual(snapshot["events"]["source"]["state"], "available")
        self.assertEqual(len(snapshot["events"]["items"]), 1)


class SchemaTests(ReadLayerFixture):
    def test_every_document_validates_against_the_published_schema(self) -> None:
        self.board.replace_comments(12, [{"date_creation": 1, "comment": "[report:done]\ndone"}])
        _journal(self.board, [_event("ummanu-510", "card.started", ordinal=0)])
        _production(
            self.data_dir,
            {
                "ummanu-510": {
                    "attempt_id": "attempt-1",
                    "state": "validate",
                    "worker_pid_file": str(self.tmp / "worker.pid"),
                    "worker_head_run": {"run_id": "run-1", "lifecycle": "working"},
                }
            },
        )
        layer = self.layer()
        for document in (
            layer.system_snapshot(),
            layer.task_snapshot("ummanu-510"),
            layer.task_events("ummanu-510", None, limit=10),
        ):
            with self.subTest(kind=document["kind"]):
                self.assertEqual(validate(document, "web-read", document["kind"]), [])
                json.dumps(document)


class TransportIndependenceTests(unittest.TestCase):
    """Criterion 4, as a check rather than a promise."""

    FORBIDDEN = frozenset(
        """
        http httpx requests urllib urllib3 socket socketserver ssl asyncio aiohttp flask fastapi
        starlette uvicorn django jinja2 tornado werkzeug wsgiref html cgi bottle sanic quart
        ummanu.dispatch.review ummanu.dispatch.head_status ummanu.runtime.pane_host
        """.split()
    )

    def _imports(self) -> list[tuple[str, str]]:
        import ast

        found: list[tuple[str, str]] = []
        for path in sorted((REPO_ROOT / "src" / "ummanu" / "webproto").glob("*.py")):
            tree = source_trees.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    found.extend((path.name, alias.name) for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    found.append((path.name, node.module))
        return found

    def test_the_layer_imports_no_transport(self) -> None:
        offenders = [
            f"{module}: {imported}"
            for module, imported in self._imports()
            if imported.split(".")[0] in self.FORBIDDEN or imported in self.FORBIDDEN
        ]
        self.assertEqual(offenders, [])

    def test_only_the_command_module_knows_a_caller_exists(self) -> None:
        """argparse, stdout and exit statuses are the CLI's, not the layer's."""
        offenders = [
            f"{module}: {imported}"
            for module, imported in self._imports()
            if module != "commands.py" and imported.split(".")[0] in {"argparse", "sys"}
        ]
        self.assertEqual(offenders, [])

    def test_the_liveness_invariant_is_stated_on_every_agent_row(self) -> None:
        self.assertIn("pane", agent_reads.LIVENESS_INVARIANT)
        self.assertEqual(len(agent_reads.AGENT_STATES), 5)


class WebReadCommandTests(ReadLayerFixture):
    def _run(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main([*argv, "--instance", str(self.instance)])
        return code, out.getvalue(), err.getvalue()

    def test_the_three_commands_print_their_snapshots(self) -> None:
        _journal(self.board, [_event("ummanu-510", "card.started", ordinal=0)])
        with mock.patch(
            "ummanu.webproto.commands.ReadLayer",
            lambda instance, **kwargs: self.layer(),
        ):
            code, out, _ = self._run("web-read", "system", "--json")
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["kind"], "system")

            code, out, _ = self._run("web-read", "task", "--ref", "ummanu-510", "--json")
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["ref"], "ummanu-510")

            code, out, _ = self._run("web-read", "events", "--ref", "ummanu-510")
            self.assertEqual(code, 0)
            self.assertIn("next cursor:", out)

    def test_a_refused_read_exits_on_its_code_and_prints_the_error(self) -> None:
        with mock.patch(
            "ummanu.webproto.commands.ReadLayer",
            lambda instance, **kwargs: self.layer(),
        ):
            code, _, err = self._run(
                "web-read", "events", "--ref", "ummanu-1", "--cursor", "nonsense", "--json"
            )
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error"]["code"], "validation")


if __name__ == "__main__":
    unittest.main()
