"""Pure observation seams shared by wait, gate and legacy operator snapshots."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from ummanu.dispatch import gate_lifecycle, head_status, wait_vitality
from ummanu.dispatch.head_vitality import (
    ProgressState,
    SnapshotSource,
    SourceAvailability,
    snapshots_from_status,
)
from ummanu.dispatch.head_vitality_episode import VitalityEpisode, previous_snapshot_cursors
from ummanu.dispatch.state import DispatcherRecord


def record_for(kind: str = "worker", run_id: str = "current") -> DispatcherRecord:
    record = DispatcherRecord(
        worker="worker", workspace="", handle="pane", head="codex",
        review_head="reviewer", attempt_id="attempt", comment_baseline=0,
        review_baseline=0, state="reviewing", claimed_at=0,
    )
    setattr(record, f"{kind}_head_run", {"run_id": run_id})
    return record


def runtime_for(status: object) -> SimpleNamespace:
    return SimpleNamespace(
        host=SimpleNamespace(mode="real", worker_status=mock.Mock(return_value=status)),
        owner="operator", writer=SimpleNamespace(comment=mock.Mock()), save_records=mock.Mock(),
    )


class ObservationConsumersTests(unittest.TestCase):
    def test_child_and_journal_only_advancement_reaches_gate_and_wait_reducer(self) -> None:
        for status, source in (
            ({"child_activity": {"state": "observed", "uptime_ticks": 200,
                                  "total_cpu_ms": 1_000,
                                  "descendants": [{"pid": 12, "start": 10, "cpu_ms": 1_000}]}},
             SnapshotSource.EXECUTION_CHILD),
            ({"supervisor_journal": {"state": "observed", "run_id": "current",
                                      "turn": "active", "turn_since": 50.0, "progress_seq": 2}},
             SnapshotSource.SUPERVISOR_JOURNAL),
        ):
            for consumer in ("gate", "wait"):
                with self.subTest(source=source, consumer=consumer):
                    record = record_for()
                    record.worker_vitality_episode = VitalityEpisode(
                        run_id="current", evidence_cursors={"execution_child": "c2:64:0:0;",
                                                          "supervisor_journal": "j1:1"},
                    )
                    runtime = runtime_for(status)
                    with mock.patch.object(wait_vitality, "_reduce_vitality", wraps=wait_vitality._reduce_vitality) as reduce:
                        episode = self.consume(consumer, runtime, record, status)
                    self.assertIsNotNone(episode)
                    snapshot, = reduce.call_args.args[1]
                    self.assertIs(snapshot.source, source)
                    self.assertIs(snapshot.availability, SourceAvailability.AVAILABLE)
                    self.assertIs(snapshot.progress, ProgressState.ADVANCING)
                    if source is SnapshotSource.EXECUTION_CHILD:
                        self.assertEqual(episode.child_progress_at, 100.0)
                        self.assertEqual(episode.last_progress_at, 0.0)
                    else:
                        self.assertEqual(episode.last_progress_at, 100.0)
                    runtime.save_records.assert_called_once()

    def test_gate_and_wait_observe_each_supported_source(self) -> None:
        for status, source in (
            ({"pid_status": {}}, SnapshotSource.PID_HEARTBEAT),
            ({"provider_progress": {}}, SnapshotSource.PROVIDER_CURSOR),
            ({"idle": None}, SnapshotSource.PANE_ADVISORY),
            ({"child_activity": {}}, SnapshotSource.EXECUTION_CHILD),
            ({"supervisor_journal": None}, SnapshotSource.SUPERVISOR_JOURNAL),
            ({"supervisor_journal": "broken"}, SnapshotSource.SUPERVISOR_JOURNAL),
        ):
            for consumer in ("gate", "wait"):
                with self.subTest(status=status, consumer=consumer):
                    record = record_for()
                    runtime = runtime_for(status)
                    with mock.patch.object(wait_vitality, "_reduce_vitality", wraps=wait_vitality._reduce_vitality) as reduce:
                        episode = self.consume(consumer, runtime, record, status)
                    self.assertIsNotNone(episode)
                    snapshots = reduce.call_args.args[1]
                    self.assertEqual([item.source for item in snapshots], [source])
                    if source is SnapshotSource.SUPERVISOR_JOURNAL:
                        self.assertIs(snapshots[0].availability, SourceAvailability.UNAVAILABLE)
                        self.assertIs(snapshots[0].progress, ProgressState.UNKNOWN)
                        self.assertEqual(episode.last_progress_at, 0.0)
                    runtime.save_records.assert_called_once()

    @staticmethod
    def consume(consumer: str, runtime: SimpleNamespace, record: DispatcherRecord, status: object):
        task = {"ref": "example-1"}
        records = {task["ref"]: record}
        if consumer == "gate":
            with mock.patch.object(gate_lifecycle.time, "time", return_value=100.0):
                return gate_lifecycle._worker_vitality_for_gate(runtime, task, record, records, {})
        return wait_vitality.reduce_and_store_vitality_episode(
            runtime, task, record, records, {}, status, kind="worker", now=100.0,
        )

    def test_unobserved_tick_keeps_bound_or_foreign_episode_without_effects(self) -> None:
        for status in (None, [], "broken", {}, {"reason": "unavailable"},
                       {"pid_status": None, "provider_progress": [], "child_activity": "broken"}):
            for previous_id in ("current", "foreign"):
                for consumer in ("gate", "wait"):
                    with self.subTest(status=status, previous_id=previous_id, consumer=consumer):
                        record = record_for()
                        previous = VitalityEpisode(run_id=previous_id)
                        record.worker_vitality_episode = previous
                        runtime = runtime_for(status)
                        with mock.patch.object(wait_vitality, "_reduce_vitality") as reduce:
                            self.assertIsNone(self.consume(consumer, runtime, record, status))
                        self.assertIs(record.worker_vitality_episode, previous)
                        reduce.assert_not_called()
                        runtime.save_records.assert_not_called()
                        runtime.writer.comment.assert_not_called()

    def test_missing_wait_run_identity_clears_stale_episode_before_observation_check(self) -> None:
        for kind in ("worker", "review"):
            for status in ({}, None, {"supervisor_journal": None}):
                with self.subTest(kind=kind, status=status):
                    record = record_for(kind, "")
                    setattr(record, f"{kind}_vitality_episode", VitalityEpisode(run_id="foreign"))
                    runtime = runtime_for(status)
                    with mock.patch.object(wait_vitality, "_reduce_vitality") as reduce:
                        episode = wait_vitality.reduce_and_store_vitality_episode(
                            runtime, {"ref": "example-1"}, record, {}, {}, status,
                            kind=kind, now=100.0,
                        )
                    self.assertIsNone(episode)
                    self.assertIsNone(getattr(record, f"{kind}_vitality_episode"))
                    reduce.assert_not_called()
                    runtime.save_records.assert_not_called()
                    runtime.writer.comment.assert_not_called()

    def test_gate_noop_or_failed_probe_never_reduces(self) -> None:
        for mode, error in (("noop", None), ("real", RuntimeError("blind probe"))):
            with self.subTest(mode=mode):
                runtime = runtime_for({"supervisor_journal": None})
                runtime.host.mode = mode
                runtime.host.worker_status.side_effect = error
                with mock.patch.object(gate_lifecycle, "_reduce_and_store_vitality_episode") as reduce:
                    self.assertIsNone(self.consume("gate", runtime, record_for(), {}))
                reduce.assert_not_called()


class PreviousSnapshotConsumersTests(unittest.TestCase):
    def test_empty_run_cannot_supply_any_previous_cursor_or_child_key(self) -> None:
        previous = VitalityEpisode(
            run_id="current", evidence_cursors={"provider_cursor": "before", "execution_child": "child",
                                              "supervisor_journal": "journal"}, last_child_key="m:child",
        )
        self.assertEqual(previous_snapshot_cursors(previous, ""), {
            "previous_cursor": "", "previous_child_cursor": "", "previous_child_key": "",
            "previous_journal_cursor": "",
        })

    def test_all_baselines_are_bound_to_nonempty_current_run_for_both_roles(self) -> None:
        for kind in ("worker", "review"):
            for previous_id in ("current", "foreign", None):
                for consumer in ("wait", "head-status"):
                    with self.subTest(kind=kind, previous_id=previous_id, consumer=consumer):
                        record = record_for(kind)
                        previous = VitalityEpisode(
                            run_id=previous_id,
                            evidence_cursors={
                                "provider_cursor": "provider-before", "execution_child": "child-before",
                                "supervisor_journal": "journal-before",
                            }, last_child_key="m:child",
                        ) if previous_id is not None else None
                        setattr(record, f"{kind}_vitality_episode", previous)
                        runtime = runtime_for({"supervisor_journal": None})
                        module = wait_vitality if consumer == "wait" else head_status
                        seam = "_snapshots_from_status" if consumer == "wait" else "snapshots_from_status"
                        with mock.patch.object(module, seam, wraps=snapshots_from_status) as snapshots:
                            if consumer == "wait":
                                wait_vitality.reduce_and_store_vitality_episode(
                                    runtime, {"ref": "example-1"}, record, {}, {},
                                    {"supervisor_journal": None}, kind=kind, now=100.0,
                                )
                            else:
                                with mock.patch.object(head_status, "_terminal_status", return_value=({"supervisor_journal": None}, "")):
                                    row = head_status._legacy_row(
                                        runtime.host, "example-1", record, None,
                                        kind=kind, role=kind, observed_at=100.0,
                                    )
                                self.assertEqual([item["source"] for item in row["evidence"]],
                                                 ["pid_heartbeat", "provider_cursor"])
                                self.assertIs(getattr(record, f"{kind}_vitality_episode"), previous)
                                runtime.save_records.assert_not_called()
                                runtime.writer.comment.assert_not_called()
                                runtime.host.worker_status.assert_not_called()
                        expected = {
                            "previous_cursor": "provider-before", "previous_child_cursor": "child-before",
                            "previous_child_key": "m:child", "previous_journal_cursor": "journal-before",
                        }
                        for name, cursor in expected.items():
                            self.assertEqual(snapshots.call_args.kwargs.get(name, ""),
                                             cursor if previous_id == "current" else "", name)

    def test_missing_operator_run_identity_never_reads_or_converts(self) -> None:
        for kind in ("worker", "review"):
            record = record_for(kind, "")
            previous = VitalityEpisode(run_id="foreign")
            setattr(record, f"{kind}_vitality_episode", previous)
            with (
                mock.patch.object(head_status, "_terminal_status") as status,
                mock.patch.object(head_status, "snapshots_from_status") as snapshots,
            ):
                row = head_status._legacy_row(
                    SimpleNamespace(), "example-1", record, None, kind=kind, role=kind, observed_at=100.0,
                )
            self.assertIsNone(row["episode"])
            self.assertIs(getattr(record, f"{kind}_vitality_episode"), previous)
            status.assert_not_called()
            snapshots.assert_not_called()
