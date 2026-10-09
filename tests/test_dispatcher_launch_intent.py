"""Durable launch intent for the worker and reviewer heads (secretary-820).

The window these tests hold open is the one between "the host has a head running" and "the
dispatcher has a record that says so". A state write that refuses inside it used to leave a live
head nothing pointed at, and the next tick then read the card as headless and launched a second
one. Every test here drives a real restart path (a claim, a rework, a respawn) with the state
plane failing on one side of the host call or the other, and asks the same two questions: was a
head created that nobody can find, and did the recovery produce a second one.
"""

# ruff: noqa: SIM117

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from tests.dispatcher_fixtures import SupervisedBackend, card_audit, ensure_attempt
from tests.fakes.dispatcher import (
    FakeCatalog,
    FakeHost,
    FakeSprints,
    _configure_production_shaped_codex_relaunch,
    _legacy_unbound_v1_run,
    dispatcher_seed,
)
from tests.fanout_fixtures import accepted_transport_run
from tests.observer_identity import bind_observer
from tests.production_runtime_fixtures import registered_production_runtime
from tests.sql_backend_fixtures import card_store
from ummanu._fsutil import file_lock
from ummanu.dispatch import (
    host as dispatcher_host_module,
    launch as dispatcher_launch,
    worker_continuation as dispatcher_worker_continuation,
)
from ummanu.dispatch.gate import GateResult
from ummanu.dispatch.gate_receipt import GateReceipt, TerminalCheck
from ummanu.dispatch.git_workspace import GitWorkspaceManager
from ummanu.dispatch.heartbeat import heartbeat_identity, run_heartbeat_identity
from ummanu.dispatch.host import CommandHostRuntime, InstanceCatalog
from ummanu.dispatch.launch import LAUNCH_DELIVERY_MAX_ATTEMPTS, launch_intent_liveness
from ummanu.dispatch.production import _budget_event_type
from ummanu.dispatch.runtime import DispatcherRuntime
from ummanu.dispatch.state import (
    DispatcherRecord,
    GatePrAuthorship,
    GatePublishedRef,
    HeadlessRecoveryEpisode,
    LaunchDelivery,
    LaunchIntent,
    PersistedDeliveryEvidence,
    PersistedGatePrAuthorship,
    PersistedGatePublishedRef,
    PersistedGateReceipt,
    PersistedHeadlessRecoveryEpisode,
    PersistedLaunchIntent,
    PersistedRoutingHeadSnapshot,
)
from ummanu.dispatch.tui import (
    DeliveryEvidence,
    claude_project_dir_name,
    provider_progress_for_run,
)
from ummanu.dispatch.types import (
    HeadLaunchAborted,
    HostError,
    LegacyDispatcherRecord,
    ReviewLaunch,
)
from ummanu.dispatch.watchdog import (
    head_process_status,
    initial_output_stall_seconds,
    pid_file_path,
)
from ummanu.dispatch.worker_lifecycle import (
    WorkerContinuation,
    WorkerContinuationStage,
)
from ummanu.dispatch.worker_report import prompt_worker_report as deliver_worker_report_prompt
from ummanu.projects.contract import (
    ContractVerdict,
    ModuleContract,
)
from ummanu.projects.integration_base import resolve_integration_base
from ummanu.routing_journal import RoutingHeadSnapshot, attempts as routing_attempts
from ummanu.runtime.codex_preflight import codex_provider_source_descriptor
from ummanu.runtime.head import (
    HEAD_ALIVE,
    HEAD_GONE,
    HEAD_OK,
    DeliverReceipt,
    HeadCommand,
    StartReceipt,
    StopReceipt,
    operations as head_ops,
)
from ummanu.runtime.head.command import with_pid_heartbeat
from ummanu.runtime.head_runtimes import LOCAL_PTY_RUNTIME
from ummanu.runtime.prompt_document import NUDGE_MAX_BYTES
from ummanu.tasks import TaskReader, TaskWriter, task_audit_for

REF = "ummanu-510"
# Above the default pid_max, so `kill(pid, 0)` raises and the heartbeat reads as a head that died.
DEAD_PID = 999999


def _wait_for_process_stop(pid: int, *, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    status = ""
    while time.monotonic() < deadline:
        try:
            status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
        except FileNotFoundError:
            break
        if "State:\tT" in status:
            return
        time.sleep(0.01)
    raise AssertionError(f"process {pid} did not enter stopped state within {timeout}s: {status!r}")


class _SupervisedBackend:
    """A stand-in for the `local-pty` backend that records what the host asked of it.

    Every head this host raises is held by a supervisor of its own; what these tests assert is what
    the host does around that backend — the heartbeat it binds and confirms, the failure it
    translates, the document it writes before a delivery and the pre-send hook it hands over.
    """

    def __init__(self) -> None:
        self.stops: list[str] = []
        self.deliveries: list[tuple[head_ops.HeadRun, head_ops.NudgePointer, str]] = []
        self.on_deliver: Any = None
        self.on_start: Any = None
        self.start_failure: head_ops.HeadOperationError | None = None

    def start(self, spec, workspace, task_ref, **kwargs: Any) -> StartReceipt:
        if self.on_start is not None:
            return self.on_start(**kwargs)
        run = kwargs["run"]
        failure = self.start_failure
        if failure is not None:
            status = HEAD_ALIVE if isinstance(failure, head_ops.HeadSpawnAborted) else HEAD_GONE
            return StartReceipt(status=status, run=failure.run or run, reason=str(failure), failure=failure)
        return StartReceipt(status=HEAD_OK, run=run.rebound("run:worker", leaf=""))

    def deliver(self, run, pointer, *, transport: Any = None, subject: str = "", **_ignored: Any) -> DeliverReceipt:
        hook = getattr(transport, "before_send", None)
        if hook is not None:
            handed = hook()
            if isinstance(handed, head_ops.HeadRun):
                run = head_ops.post_delivery_run(run, handed)
        self.deliveries.append((run, pointer, subject))
        if self.on_deliver is not None:
            self.on_deliver(run, pointer)
        return DeliverReceipt(status=HEAD_OK, run=run)

    def stop(self, run, initiator, **_ignored: Any) -> StopReceipt:
        self.stops.append(run.run_id)
        return StopReceipt(status=HEAD_OK, run=run.finishing(initiator).exited())


def _transport_only_preflight(
    head: str,
    *,
    role: str,
    workspace: str,
    task_ref: head_ops.TaskRef,
    pid_file: str,
    run_id: str,
) -> head_ops.HeadRun:
    """A captured allow fixture for tests whose subject starts after the policy boundary.

    These contract tests exercise pane/delivery and fenced-stop transport.  The provider policy
    has its own fixtures, so this explicit test double represents the separately attested run the
    transport receives rather than making an absent schema look allowed.
    """
    return accepted_transport_run(
        head,
        role=role,
        workspace=workspace,
        task_ref=task_ref,
        pid_file=pid_file,
        run_id=run_id,
    )


def _document_report_id(workspace: str) -> str:
    """The done-report request id from the `TASK.md` in this checkout.

    A report is attributed to its round through the id the round's command carried, so a test that
    invents one is testing a call no worker makes.
    """
    document = (Path(workspace) / "TASK.md").read_text(encoding="utf-8")
    line = next(line for line in document.splitlines() if "--kind done" in line)
    return line.split("--request-id ", 1)[1].split()[0]


class DispatcherRoutingSnapshotStateTests(unittest.TestCase):
    """A13: routing telemetry is typed in memory without rewriting durable state."""

    @staticmethod
    def record(**changes: Any) -> DispatcherRecord:
        values: dict[str, Any] = {
            "worker": "worker-1",
            "workspace": "/tmp/card",
            "handle": "pane-1",
            "head": "codex",
            "review_head": "claude",
            "attempt_id": "attempt-1",
            "comment_baseline": 0,
            "review_baseline": 0,
            "state": "claimed",
            "claimed_at": 1.0,
        }
        values.update(changes)
        return DispatcherRecord(**values)

    def test_historical_partial_routing_mapping_round_trips_exactly(self) -> None:
        payload = {
            "role": "worker",
            "head": "codex",
            "head_source": "role_default",
            "adapter": "codex",
            "model": "gpt-5.6-terra",
            "model_source": "profile",
            "effort": "high",
        }
        record = self.record(worker_run=payload)

        self.assertIsInstance(record.worker_run, PersistedRoutingHeadSnapshot)
        self.assertIsNotNone(record.worker_run.snapshot)
        self.assertEqual(record.worker_run.snapshot.head, "codex")
        self.assertEqual(record.to_json()["worker_run"], payload)

        restarted = DispatcherRecord.from_json(json.loads(json.dumps(record.to_json())))
        self.assertEqual(restarted.worker_run.to_json(), payload)
        self.assertEqual(restarted.worker_run.snapshot.head, "codex")

    def test_typed_routing_assignment_projects_json_and_empty_clears_it(self) -> None:
        snapshot = RoutingHeadSnapshot(
            role="reviewer",
            head="claude-opus",
            adapter="claude",
            model="opus",
            model_source="profile",
            effort="high",
        )
        record = self.record(review_run=snapshot)

        self.assertIs(record.review_run.snapshot, snapshot)
        self.assertEqual(record.to_json()["review_run"], snapshot.to_json())

        record.review_run = {}
        self.assertIsNone(record.review_run.snapshot)
        self.assertEqual(record.to_json()["review_run"], {})


class DispatcherGateDeliveryStateTests(unittest.TestCase):
    """A13: gate identity and delivery evidence are typed without rewriting durable JSON."""

    @staticmethod
    def record(**changes: Any) -> DispatcherRecord:
        values: dict[str, Any] = {
            "worker": "worker-1",
            "workspace": "/tmp/card",
            "handle": "pane-1",
            "head": "codex",
            "review_head": "claude",
            "attempt_id": "attempt-1",
            "comment_baseline": 0,
            "review_baseline": 0,
            "state": "claimed",
            "claimed_at": 1.0,
        }
        values.update(changes)
        return DispatcherRecord(**values)

    def test_historical_gate_and_delivery_mappings_round_trip_exactly(self) -> None:
        attestation = {"validated_sha": "short", "base_sha": "legacy", "legacy": "keep"}
        authorship = {"number": 17, "digest": "d" * 64, "sent": "s" * 64, "legacy": "keep"}
        published = {"branch": "pipeline/card", "sha": "a" * 40, "legacy": "keep"}
        delivery = {
            "subject": "worker-prompt",
            "stage": "payload_written",
            "turn_confirmed": False,
            "reason": "historical",
            "legacy": "keep",
        }
        record = self.record(
            gate_attestation=attestation,
            gate_pr_authorship=authorship,
            gate_published_ref=published,
            worker_delivery_evidence=delivery,
            review_delivery_evidence=delivery,
        )

        self.assertIsInstance(record.gate_attestation, PersistedGateReceipt)
        self.assertIsNone(record.gate_attestation.receipt)
        self.assertIsInstance(record.gate_pr_authorship, PersistedGatePrAuthorship)
        self.assertEqual(record.gate_pr_authorship.authorship.number, 17)
        self.assertIsInstance(record.gate_published_ref, PersistedGatePublishedRef)
        self.assertEqual(record.gate_published_ref.published_ref.branch, "pipeline/card")
        self.assertIsInstance(record.worker_delivery_evidence, PersistedDeliveryEvidence)
        self.assertEqual(record.worker_delivery_evidence.evidence.reason, "historical")

        durable = record.to_json()
        self.assertEqual(durable["gate_attestation"], attestation)
        self.assertEqual(durable["gate_pr_authorship"], authorship)
        self.assertEqual(durable["gate_published_ref"], published)
        self.assertEqual(durable["worker_delivery_evidence"], delivery)
        self.assertEqual(durable["review_delivery_evidence"], delivery)

        restarted = DispatcherRecord.from_json(json.loads(json.dumps(durable)))
        self.assertEqual(restarted.gate_attestation.to_json(), attestation)
        self.assertEqual(restarted.gate_pr_authorship.to_json(), authorship)
        self.assertEqual(restarted.gate_published_ref.to_json(), published)
        self.assertEqual(restarted.worker_delivery_evidence.to_json(), delivery)
        self.assertEqual(restarted.review_delivery_evidence.to_json(), delivery)

    def test_typed_gate_and_delivery_assignments_project_released_json(self) -> None:
        receipt = GateReceipt(
            validated_sha="a" * 40,
            base_sha="b" * 40,
            gate_mode="local",
            required_checks=(TerminalCheck("unit", "SUCCESS"),),
            completed_at="2026-09-18T00:00:00+00:00",
            command_or_check_set_digest="c" * 64,
        )
        authorship = GatePrAuthorship(number=19, digest="d" * 64, sent="e" * 64)
        published = GatePublishedRef(branch="pipeline/card", sha="f" * 40)
        delivery = DeliveryEvidence(
            handle="pane-1",
            subject="worker-prompt",
            stage="acknowledged",
            turn_confirmed=True,
            reason="",
        )
        record = self.record(
            gate_attestation=receipt,
            gate_pr_authorship=authorship,
            gate_published_ref=published,
            worker_delivery_evidence=delivery,
            review_delivery_evidence=delivery,
        )

        self.assertIs(record.gate_attestation.receipt, receipt)
        self.assertIs(record.gate_pr_authorship.authorship, authorship)
        self.assertIs(record.gate_published_ref.published_ref, published)
        self.assertIs(record.worker_delivery_evidence.evidence, delivery)
        self.assertIs(record.review_delivery_evidence.evidence, delivery)
        self.assertEqual(record.to_json()["gate_attestation"], receipt.as_dict())
        self.assertEqual(record.to_json()["gate_pr_authorship"], authorship.to_json())
        self.assertEqual(record.to_json()["gate_published_ref"], published.to_json())
        self.assertEqual(record.to_json()["worker_delivery_evidence"], delivery.to_json())
        self.assertEqual(record.to_json()["review_delivery_evidence"], delivery.to_json())

        record.gate_attestation = {}
        record.gate_pr_authorship = {}
        record.gate_published_ref = {}
        record.worker_delivery_evidence = {}
        record.review_delivery_evidence = {}
        self.assertIsNone(record.gate_attestation.receipt)
        self.assertIsNone(record.gate_pr_authorship.authorship)
        self.assertIsNone(record.gate_published_ref.published_ref)
        self.assertIsNone(record.worker_delivery_evidence.evidence)
        self.assertIsNone(record.review_delivery_evidence.evidence)


class DispatcherLaunchRecoveryStateTests(unittest.TestCase):
    """A13: launch intent and headless recovery are typed without rewriting durable JSON."""

    @staticmethod
    def record(**changes: Any) -> DispatcherRecord:
        values: dict[str, Any] = {
            "worker": "worker-1",
            "workspace": "/tmp/card",
            "handle": "pane-1",
            "head": "codex",
            "review_head": "claude",
            "attempt_id": "attempt-1",
            "comment_baseline": 0,
            "review_baseline": 0,
            "state": "claimed",
            "claimed_at": 1.0,
        }
        values.update(changes)
        return DispatcherRecord(**values)

    def test_historical_launch_and_headless_mappings_round_trip_exactly(self) -> None:
        launch = {
            "role": "worker",
            "action": "claim",
            "run_id": "legacy-run",
            "delivery": {
                "state": "busy",
                "attempts": 2,
                "next_at": 123.5,
                "legacy": "keep",
            },
            "legacy": "keep",
        }
        headless = {
            "since": 100.25,
            "comment_baseline": 7,
            "record_state": "adopted",
            "handle_known": False,
            "heartbeat": "absent",
            "workspace": "/tmp/card",
            "branch": "pipeline/card",
            "expected_branch": "pipeline/card",
            "dirty": False,
            "candidate_sha": "a" * 40,
            "report_generation": 3,
            "recovery_error": "round_already_answered",
            "legacy": "keep",
        }
        record = self.record(launch_intent=launch, worker_headless=headless)

        self.assertIsInstance(record.launch_intent, PersistedLaunchIntent)
        self.assertEqual(record.launch_intent.intent.role, "worker")
        self.assertEqual(record.launch_intent.intent.delivery.attempts, 2)
        self.assertIsInstance(record.worker_headless, PersistedHeadlessRecoveryEpisode)
        self.assertEqual(record.worker_headless.episode.candidate_sha, "a" * 40)
        self.assertEqual(record.worker_headless.episode.recovery_error, "round_already_answered")

        durable = record.to_json()
        self.assertEqual(durable["launch_intent"], launch)
        self.assertEqual(durable["worker_headless"], headless)

        restarted = DispatcherRecord.from_json(json.loads(json.dumps(durable)))
        self.assertEqual(restarted.launch_intent.to_json(), launch)
        self.assertEqual(restarted.worker_headless.to_json(), headless)
        self.assertEqual(restarted.launch_intent.intent.action, "claim")
        self.assertEqual(restarted.worker_headless.episode.comment_baseline, 7)

    def test_typed_launch_and_headless_assignments_project_released_json(self) -> None:
        evidence = DeliveryEvidence(
            handle="pane-1",
            subject="worker-prompt",
            stage="acknowledged",
            turn_confirmed=True,
        )
        routing = RoutingHeadSnapshot(
            role="worker",
            head="codex",
            adapter="codex",
            model="gpt-5.6-terra",
            model_source="profile",
            effort="high",
        )
        head_run = accepted_transport_run(
            "codex",
            role="worker",
            workspace="/tmp/card",
            task_ref=head_ops.TaskRef.card("ummanu-1", document="/tmp/card/TASK.md"),
            pid_file="/tmp/card.pid",
            run_id="run-1",
        )
        intent = LaunchIntent(
            role="worker",
            action="claim",
            head="codex",
            workspace="/tmp/card",
            pid_file="/tmp/card.pid",
            run_id="run-1",
            task="card:ummanu-1",
            attempt_id="attempt-1",
            round_number=1,
            opens_round=True,
            respawns=0,
            at=123.0,
            routing_run=routing,
            head_run=head_run,
            delivery=LaunchDelivery(
                state="confirmed",
                receipt="accepted",
                evidence=evidence,
            ),
            launched=True,
        )
        episode = HeadlessRecoveryEpisode(
            since=456.0,
            comment_baseline=4,
            record_state="adopted",
            heartbeat="absent",
            workspace="/tmp/card",
            branch="pipeline/card",
            expected_branch="pipeline/card",
            dirty=False,
            candidate_sha="b" * 40,
            report_generation=2,
        )
        record = self.record(launch_intent=intent, worker_headless=episode)

        self.assertEqual(record.launch_intent.intent.role, "worker")
        self.assertEqual(record.launch_intent.intent.routing_run, routing)
        self.assertTrue(record.launch_intent.intent.head_run.same_run(head_run))
        self.assertEqual(record.launch_intent.intent.delivery.evidence.subject, "worker-prompt")
        self.assertEqual(record.worker_headless.episode, episode)
        self.assertEqual(record.to_json()["launch_intent"], intent.to_json())
        self.assertEqual(record.to_json()["worker_headless"], episode.to_json())

        # Legacy call sites still mutate these mapping-compatible wrappers in place. The typed
        # view must track those writes until the last compatibility mutation is removed.
        record.launch_intent["action"] = "worker-respawn"
        record.worker_headless["recovery_error"] = "candidate_unknown"
        self.assertEqual(record.launch_intent.intent.action, "worker-respawn")
        self.assertEqual(record.worker_headless.episode.recovery_error, "candidate_unknown")

        record.launch_intent = {}
        record.worker_headless = {}
        self.assertIsNone(record.launch_intent.intent)
        self.assertIsNone(record.worker_headless.episode)

class LaunchIntentTests(unittest.TestCase):
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
        # The card belongs to a sprint with a concrete observer, so a substantive verdict parks
        # for a decision: these tests drive the rework that decision opens.
        self.sprints = FakeSprints()
        self.sprints.rows["sprint:1031"] = {
            "ref": "sprint:1031",
            "status": "open",
            "observer": {"kind": "head", "profile": "claude-observer"},
        }
        self.board.save_metadata(12, sprint_ref="sprint:1031")
        bind_observer(self, "sprint:1031")
        # And that sprint reserves the card's project, which is what lets its observer decide.
        self.board.add_sprint("sprint:1031", status="open", sprint_reservations='["ummanu"]')
        self.runtime = DispatcherRuntime(
            self.reader,
            self.writer,
            task_audit_for(self.board),
            self.data_dir,
            self.catalog,  # type: ignore[arg-type]
            self.host,  # type: ignore[arg-type]
            owner="ummanu-pilot",
            sprints=self.sprints,
        )
        self.runtime.production_state.save(
            {
                "version": 1,
                "mode": "production",
                "phase": "production",
                "owner": self.runtime.owner,
                "records": {},
            }
        )

    # fixtures ---------------------------------------------------------------

    def tick(self) -> dict:
        """One card through the tick's per-card decision, the way `production_tick` reaches it."""
        with file_lock(self.runtime.production_state.tick_lock):
            payload = self.runtime.production_state.load()
            records = self.runtime.production_state.records(payload)
            attempt_id = ensure_attempt(payload, REF, self.runtime.owner, self.runtime.owner)
            outcome = self.runtime._tick_task(self.reader.show(REF), records, payload, attempt_id)
            self.runtime.production_state.put_records(payload, records)
            self.runtime.production_state.save(payload)
        return outcome

    def record(self) -> DispatcherRecord | None:
        return self.runtime.production_state.records(self.runtime.production_state.load()).get(REF)

    def kill_worker_heartbeat(self, *, path: str = "") -> None:
        """Rewrite the worker's heartbeat with a reaped pid: the head is genuinely gone.

        S1-4: a scripted ``missing-terminal`` answer alone no longer grounds a reclaim,
        because the vitality reduction reads the raw classification too -- and a live
        heartbeat behind a lost pane is not a death. Tests that model a dead head give
        the heartbeat the same evidence. A launch that died before binding its pid file
        passes the intent's path explicitly.
        """
        record = self.record()
        target = path or (record.worker_pid_file if record else "")
        if not target:
            return
        self.host.head_pid = self._dead_pid()
        self.host._write_head_pid(
            "worker",
            REF,
            head_run=record.worker_head_run,
            leaf=record.worker_leaf,
        )

    def kill_review_heartbeat(self) -> None:
        """The reviewer twin of ``kill_worker_heartbeat``."""
        record = self.record()
        if not record or not record.review_pid_file:
            return
        self.host.head_pid = self._dead_pid()
        self.host._write_head_pid(
            "review",
            REF,
            head_run=record.review_head_run,
            leaf=record.review_leaf,
        )

    def _dead_pid(self) -> int:
        """A pid the kernel has already reaped, for heartbeats that name a gone process."""
        proc = subprocess.Popen(["true"])
        proc.wait()
        return proc.pid

    def workspace_of_record(self) -> str:
        record = self.record()
        return record.workspace if record else ""

    def stored_intent(self) -> dict:
        """The intent as it is on disk, which is the only copy a next tick can read."""
        record = self.runtime.production_state.load().get("records", {}).get(REF) or {}
        return dict(record.get("launch_intent") or {})

    def replace_intent_heartbeat_run(self) -> None:
        intent = self.stored_intent()
        path = Path(str(intent["pid_file"]))
        heartbeat = json.loads(path.read_text(encoding="utf-8"))
        heartbeat["run_id"] = "foreign-run"
        path.write_text(json.dumps(heartbeat), encoding="utf-8")

    def fail_launch_intent_save(self):
        """A state plane that refuses exactly the write a launch intent needs, and nothing else.

        Failing every save instead would prove far less: the tick would die on some earlier write
        and never reach the launch at all.
        """
        real = self.runtime.production_state.save

        def save(payload: dict) -> None:
            records = payload.get("records") or {}
            if any(
                (record.get("launch_intent") or {}).get("role")
                for record in records.values()
                if isinstance(record, dict)
            ):
                raise OSError("dispatcher state is not writable")
            real(payload)

        return mock.patch.object(self.runtime.production_state, "save", save)

    @contextlib.contextmanager
    def state_dies_after(self, host_method: str):
        """The tick that started a head does not live to record it.

        Every state write after `host_method` returns refuses, which is what a process killed or a
        data plane lost mid-launch looks like from the record's side.
        """
        real_save = self.runtime.production_state.save
        real_call = getattr(self.host, host_method)
        launched = {"yet": False}

        def save(payload: dict) -> None:
            if launched["yet"]:
                raise OSError("dispatcher state is not writable")
            real_save(payload)

        def call(*args, **kwargs):
            result = real_call(*args, **kwargs)
            launched["yet"] = True
            return result

        with mock.patch.object(self.runtime.production_state, "save", save):
            with mock.patch.object(self.host, host_method, call):
                yield

    @contextlib.contextmanager
    def dies_after_the_intent_is_confirmed(self, host_method: str):
        """The tick lives exactly as far as the write that confirms its launch intent, and no further.

        The narrower twin of `state_dies_after`, and the window the head run has to survive: the
        confirming write is itself durable, so the record on disk already knows a head was launched.
        What used to happen after it — the caller assigning that head's run to the record — is what
        a process killed here never reached, and the next tick then adopted the head with a
        reconstructed identity. Every save after the confirming one refuses, which is what that
        death looks like from the record's side.

        Yields the launched head's run, as the host reported it, so a test can ask whether the head
        the next tick adopts is that same run.
        """
        real_save = self.runtime.production_state.save
        real_call = getattr(self.host, host_method)
        launched: dict[str, Any] = {"yet": False, "saves": 0}
        head_run: dict[str, Any] = {}

        def save(payload: dict) -> None:
            if launched["yet"]:
                launched["saves"] += 1
                if launched["saves"] > 1:
                    raise OSError("dispatcher state is not writable")
            real_save(payload)

        def call(*args, **kwargs):
            result = real_call(*args, **kwargs)
            reported = result.get("head_run") if isinstance(result, dict) else getattr(result, "head_run", {})
            head_run.update(dict(reported or {}))
            launched["yet"] = True
            return result

        with mock.patch.object(self.runtime.production_state, "save", save):
            with mock.patch.object(self.host, host_method, call):
                yield head_run

    def refuse_audit(self, match: str):
        """A journal that refuses exactly the writes whose request id carries `match`.

        The refusal lands on either released staging entry point. Typed events use
        ``claim`` so their request ownership and pre-effect staging are one lock
        hold; generic records continue to use ``stage``.
        """
        real = self.writer.audit.stage

        def stage(request_id: str, event: dict) -> None:
            if match in request_id:
                raise OSError("audit journal is not writable")
            real(request_id, event)

        real_claim = self.writer.audit.claim

        def claim(request_id: str, event: dict, **kwargs: object) -> dict | None:
            if match in request_id:
                raise OSError("audit journal is not writable")
            return real_claim(request_id, event, **kwargs)

        return mock.patch.multiple(self.writer.audit, stage=stage, claim=claim)

    @contextlib.contextmanager
    def audit_dies_after(self, host_method: str):
        """The journal stops accepting writes the moment the head is up.

        The other half of `state_dies_after`: the launch itself succeeded, and what is refused is
        the telemetry the launch path writes after it.
        """
        real_stage = self.writer.audit.stage
        real_append = self.writer.audit.append
        real_call = getattr(self.host, host_method)
        launched = {"yet": False}

        def stage(request_id: str, event: dict) -> None:
            if launched["yet"]:
                raise OSError("audit journal is not writable")
            real_stage(request_id, event)

        def append(request_id: str, event: dict) -> str:
            if launched["yet"]:
                raise OSError("audit journal is not writable")
            return real_append(request_id, event)

        def call(*args, **kwargs):
            result = real_call(*args, **kwargs)
            launched["yet"] = True
            return result

        with mock.patch.object(self.writer.audit, "stage", stage):
            with mock.patch.object(self.writer.audit, "append", append):
                with mock.patch.object(self.host, host_method, call):
                    yield

    def report_done(self) -> None:
        """Report through the done command the checkout holds: that id names the round the
        dispatcher is waiting for (secretary-1063)."""
        self.writer.report(
            role="worker",
            actor="worker",
            reference=REF,
            kind="done",
            body="done",
            request_id=_document_report_id(self.workspace_of_record()),
        )

    def verdict(self, kind: str, body: str, request_id: str) -> None:
        self.writer.verdict(
            role="reviewer",
            actor="reviewer",
            reference=REF,
            kind=kind,
            body=body,
            request_id=request_id,
        )

    def run_to_validate(self) -> None:
        self.tick()
        self.report_done()
        self.assertEqual(self.tick()["to"], "validate")

    def age_intent(self, seconds: float) -> None:
        """Push a stored intent back in time, so its grace window has run out."""
        payload = self.runtime.production_state.load()
        payload["records"][REF]["launch_intent"]["at"] -= seconds
        self.runtime.production_state.save(payload)

    # worker: before the host call -------------------------------------------

    def test_the_worker_launch_intent_is_on_disk_before_the_host_is_called(self) -> None:
        seen: list[dict] = []
        real = self.host.prepare_worker

        def spy(*args, **kwargs):
            seen.append(self.stored_intent())
            return real(*args, **kwargs)

        with mock.patch.object(self.host, "prepare_worker", spy):
            self.tick()

        intent = seen[0]
        self.assertEqual(intent["role"], "worker")
        self.assertEqual(intent["action"], "claim")
        self.assertEqual(intent["head"], "codex")
        self.assertEqual(intent["round"], 1)
        # Workspace and pid file are the head's own, both known before the head exists: without
        # them a tick that dies here could neither find the head nor read its liveness.
        self.assertEqual(intent["workspace"], self.host.restore_workspace({}, f"{REF}-pilot"))
        self.assertEqual(intent["pid_file"], pid_file_path("worker", REF))
        # And it is gone again once the host has answered and the launch is recorded in full.
        self.assertEqual(self.stored_intent(), {})

    def test_state_that_cannot_be_written_launches_no_worker_at_all(self) -> None:
        with self.fail_launch_intent_save():
            outcome = self.tick()

        self.assertEqual(outcome["action"], "worker-launch-intent-unwritable")
        self.assertEqual(outcome["status"], "degraded")
        self.assertEqual(self.host.prepared, [], "no head may exist that no record can find")
        self.assertEqual(self.stored_intent(), {})

        # The card keeps its claim, and the very next tick brings up exactly one head.
        recovered = self.tick()

        self.assertEqual(recovered["step"], "claim")
        self.assertEqual(self.host.prepared, [REF])

    # worker: after the host call --------------------------------------------

    def test_a_worker_launch_that_outlived_its_tick_is_adopted_not_doubled(self) -> None:
        with self.state_dies_after("prepare_worker"), self.assertRaises(OSError):
            self.tick()

        self.assertEqual(self.host.prepared, [REF])
        intent = self.stored_intent()
        self.assertEqual((intent["role"], intent["action"]), ("worker", "claim"))

        adopted = self.tick()

        self.assertEqual(adopted["action"], "worker-launch-adopted")
        self.assertEqual(self.host.prepared, [REF], "the live head must not be launched twice")
        record = self.record()
        assert record is not None
        self.assertEqual(record.state, "claimed")
        self.assertEqual(record.workspace, self.host.restore_workspace({}, f"{REF}-pilot"))
        self.assertEqual(self.stored_intent(), {}, "an adopted intent is spent")

        # And the card carries on from the adopted head instead of being restarted around it.
        self.report_done()
        self.assertEqual(self.tick()["to"], "validate")
        self.assertEqual(self.host.prepared, [REF])

    def test_an_adopted_worker_keeps_the_run_its_bring_up_started(self) -> None:
        """The head run belongs to the launch, not to the tick that survived it (secretary-1414).

        The tick dies in the one window that exists: after the write that confirms the launch
        intent — a durable save, so the record already knows a worker is up — and before anything
        else the record is told about that head. The next tick adopts it, and the run it stops that
        worker by has to be the run `spawn` returned. A reconstructed identity here would mean a
        stop already begun stops being a continuation of itself and its initiator is lost.
        """
        with self.dies_after_the_intent_is_confirmed("prepare_worker") as launched_run:
            with self.assertRaises(OSError):
                self.tick()

        self.assertTrue(launched_run["run_id"], "the fake host reports a run for every launch")
        self.assertEqual(self.stored_intent()["head_run"]["run_id"], launched_run["run_id"])

        adopted = self.tick()

        self.assertEqual(adopted["action"], "worker-launch-adopted")
        record = self.record()
        assert record is not None
        self.assertEqual(record.worker_head_run["run_id"], launched_run["run_id"])
        self.assertEqual(self.stored_intent(), {}, "an adopted intent is spent")

    def test_an_adopted_reviewer_keeps_the_run_its_bring_up_started(self) -> None:
        """The reviewer's half of the same window, and the one the finding was raised on."""
        self.run_to_validate()
        with self.dies_after_the_intent_is_confirmed("start_review") as launched_run:
            with self.assertRaises(OSError):
                self.tick()

        self.assertTrue(launched_run["run_id"], "the fake host reports a run for every launch")
        self.assertEqual(self.stored_intent()["head_run"]["run_id"], launched_run["run_id"])

        adopted = self.tick()

        self.assertEqual(adopted["action"], "review-launch-adopted")
        record = self.record()
        assert record is not None
        self.assertEqual(record.review_head_run["run_id"], launched_run["run_id"])

    def test_an_aborted_reviewer_bring_up_keeps_the_run_of_the_head_it_left(self) -> None:
        """An abort is the case where the pane is live, so what is in it has to stay nameable.

        The reviewer spawned and its worker would not freeze: the bring-up fails with the pane
        open. The run of the head in that pane travels with the failure into the intent, and the
        adoption that finishes the freeze continues it rather than opening a second identity for a
        reviewer this dispatcher did start.
        """
        self.run_to_validate()
        self.host.fail_freeze_worker_reason = "orca refused to close the worker pane"

        self.assertEqual(self.tick()["action"], "review-launch-aborted")
        aborted_run = dict(self.stored_intent()["head_run"])
        self.assertTrue(aborted_run["run_id"])

        self.host.fail_freeze_worker_reason = ""
        adopted = self.tick()

        self.assertEqual(adopted["action"], "review-launch-adopted")
        self.assertEqual(self.host.reviews, [REF], "the live reviewer must not be doubled")
        record = self.record()
        assert record is not None
        self.assertEqual(record.review_head_run["run_id"], aborted_run["run_id"])

    def test_a_worker_intent_whose_head_died_is_relaunched_exactly_once(self) -> None:
        self.host.head_pid = DEAD_PID
        with self.state_dies_after("prepare_worker"), self.assertRaises(OSError):
            self.tick()

        self.host.head_pid = os.getpid()
        relaunched = self.tick()

        self.assertEqual(relaunched["step"], "claim")
        # The lost launch carried no durable leaf, so the heartbeat remains the legacy fallback
        # that stops it before a replacement opens.
        self.assertEqual(
            [call for call in self.host.calls if call in ("prepare_worker", "stop_head:worker")],
            ["prepare_worker", "stop_head:worker", "prepare_worker"],
        )
        self.assertEqual(self.stored_intent(), {})

    def test_a_live_foreign_worker_heartbeat_is_fenced_without_a_stop_or_replacement(self) -> None:
        with self.state_dies_after("prepare_worker"), self.assertRaises(OSError):
            self.tick()
        self.replace_intent_heartbeat_run()

        fenced = self.tick()

        self.assertEqual(fenced["action"], "worker-heartbeat-identity-mismatch")
        self.assertEqual(self.host.calls.count("prepare_worker"), 1)
        self.assertNotIn("stop_head:worker", self.host.calls)
        self.assertNotIn("stop_workspace", self.host.calls)

    def test_an_intent_without_a_heartbeat_waits_out_its_grace_window(self) -> None:
        """A head that has been launched but has not written its pid is not a dead head."""
        self.host.head_pid = None
        with self.state_dies_after("prepare_worker"), self.assertRaises(OSError):
            self.tick()

        pending = self.tick()

        self.assertEqual(pending["action"], "worker-launch-pending")
        self.assertEqual(self.host.prepared, [REF], "a head that is still starting is not replaced")
        self.assertNotIn("stop", self.host.calls)
        self.assertEqual(self.stored_intent()["role"], "worker")

        # Once the window has run out with no heartbeat, the launch counts as one that left
        # nothing running and the ordinary path relaunches.
        self.age_intent(initial_output_stall_seconds() + 60)
        self.host.head_pid = os.getpid()

        self.assertEqual(self.tick()["step"], "claim")
        self.assertEqual(self.host.prepared, [REF, REF])

    # worker: the restart paths, not only the first claim ---------------------

    def test_a_rework_launch_that_outlived_its_tick_is_adopted_not_doubled(self) -> None:
        self.run_to_validate()
        self.tick()  # reviewer up
        self.verdict("red", "needs work", "verdict-red")
        self.tick()  # the verdict parks the card
        self.decide("rework")
        with self.state_dies_after("restart_worker"), self.assertRaises(OSError):
            self.tick()

        self.assertEqual(self.host.calls.count("restart_worker"), 1)
        self.assertEqual(self.stored_intent()["action"], "review-red-rework")

        adopted = self.tick()

        self.assertEqual(adopted["action"], "worker-launch-adopted")
        self.assertEqual(self.host.calls.count("restart_worker"), 1)
        record = self.record()
        assert record is not None
        self.assertEqual(record.state, "claimed")

    def test_legacy_unbound_v1_rework_intent_recovers_one_exact_replacement(self) -> None:
        """The typed source path keeps its preflight run durable across a dead launch tick."""
        self.host.fail_resume_worker_reason = ""
        self.rework_after_red_review()
        self.install_legacy_unbound_v1_worker_source()
        old_run_id = self.record().worker_head_run["run_id"]  # type: ignore[union-attr]
        _configure_production_shaped_codex_relaunch(
            self.host,
            root=self.data_dir / "replacement-sessions",
        )

        with self.state_dies_after("restart_worker"), self.assertRaises(OSError):
            self.tick()

        intent = self.stored_intent()
        self.assertEqual(intent["action"], "review-red-rework")
        self.assertNotEqual(intent["head_run"]["run_id"], old_run_id)
        source = intent["head_run"]["fanout_policy"]["provider_source"]
        self.assertEqual(source["state"], "unbound")
        self.assertEqual(source["baseline"], [])
        self.assertEqual(self.host.calls.count("restart_worker"), 1)
        self.assertEqual(self.host.resumed_continuations, [])

        recovered = self.tick()

        self.assertEqual(recovered["action"], "worker-launch-adopted")
        self.assertEqual(self.host.calls.count("restart_worker"), 1)
        record = self.record()
        assert record is not None
        self.assertEqual(record.worker_head_run["run_id"], intent["head_run"]["run_id"])

    def test_a_gate_red_rework_writes_its_intent_before_the_relaunch(self) -> None:
        self.run_to_validate()
        self.host.gate_results = [GateResult("red", "tests failed", log="boom")]
        seen: list[dict] = []
        real = self.host.restart_worker

        def spy(*args, **kwargs):
            seen.append(self.stored_intent())
            return real(*args, **kwargs)

        with mock.patch.object(self.host, "restart_worker", spy):
            outcome = self.tick()

        self.assertEqual(outcome["action"], "gate-red-rework")
        self.assertEqual(seen[0]["action"], "gate-red-rework")
        self.assertEqual(seen[0]["role"], "worker")

    def test_a_respawn_that_outlived_its_tick_is_adopted_not_doubled(self) -> None:
        self.tick()
        # A worker pane Orca no longer knows about, and a heartbeat that agrees the
        # process is gone: the watchdog reclaims it once.
        self.kill_worker_heartbeat()
        self.host.worker_status_result = {"known": True, "live": False, "reason": "missing-terminal"}
        with self.state_dies_after("restart_worker"), self.assertRaises(OSError):
            self.tick()

        self.assertEqual(self.stored_intent()["action"], "worker-respawn")
        self.assertEqual(self.host.calls.count("restart_worker"), 1)

        # The respawned head is alive: give its heartbeat a live pid (the crash-tick
        # launch inherited the dead one from the scripted death), so the intent's
        # liveness check adopts it instead of stopping a leftover.
        self.host.head_pid = os.getpid()
        self.host._write_head_pid("worker", REF, run_id=self.stored_intent().get("run_id") or "")
        adopted = self.tick()

        self.assertEqual(adopted["action"], "worker-launch-adopted")
        self.assertEqual(self.host.calls.count("restart_worker"), 1)

    def test_a_stale_done_rework_writes_its_intent_before_the_relaunch(self) -> None:
        self.run_to_validate()
        self.tick()
        self.verdict("red", "needs work", "verdict-red")
        self.tick()  # the verdict parks the card
        self.decide("rework")
        self.tick()  # rework head up on the rejected sha
        seen: list[dict] = []
        real = self.host.restart_worker

        def spy(*args, **kwargs):
            seen.append(self.stored_intent())
            return real(*args, **kwargs)

        self.report_done()
        with mock.patch.object(self.host, "restart_worker", spy):
            outcome = self.tick()

        self.assertEqual(outcome["action"], "stale-done-rework")
        self.assertEqual(seen[0]["action"], "stale-done-rework")

    def test_a_stale_done_rework_after_an_unconfirmed_stop_starts_nothing(self) -> None:
        self.run_to_validate()
        self.tick()
        self.verdict("red", "needs work", "verdict-red")
        self.tick()  # the verdict parks the card
        self.decide("rework")
        self.tick()  # rework head up on the rejected sha
        self.report_done()
        self.host.calls.clear()
        self.host.fail_stop_head_reason = "orca terminal stop failed"

        outcome = self.tick()

        self.assertEqual(outcome["action"], "worker-stop-unconfirmed")
        self.assertEqual(outcome["status"], "degraded")
        self.assertEqual(self.host.calls.count("restart_worker"), 0)
        self.assertEqual(self.stored_intent(), {}, "no launch was even fixed on disk")

        # Once the host confirms the stop, the rework relaunch happens exactly once.
        self.host.fail_stop_head_reason = ""

        retried = self.tick()

        self.assertEqual(retried["action"], "stale-done-rework")
        self.assertEqual(self.host.calls.count("restart_worker"), 1)

    # worker: the round a rework launch belongs to ---------------------------

    def decide(self, kind: str, request_id: str = "") -> None:
        """The observer decision that releases a parked card. Nothing acts without one."""
        self.writer.decide(
            role="observer",
            actor="observer",
            reference=REF,
            kind=kind,
            body="observer decision",
            request_id=request_id or f"decision-{kind}",
        )

    def release_after_green_verdict(self) -> dict:
        """Park the green verdict, decide release, and hand back the tick that merged."""
        self.assertEqual(self.tick()["to"], "assessment")
        self.decide("release")
        return self.tick()

    def rework_after_red_review(self) -> None:
        """Bring the card to the point where the next tick relaunches a worker for round 2.

        A red verdict parks the card first, so the rework these tests are about only begins once
        the observer has decided it: the park and the decision are part of getting there now.
        """
        self.run_to_validate()
        self.tick()  # reviewer up
        self.verdict("red", "needs work", "verdict-red")
        self.tick()  # the verdict parks the card in Assessment
        self.decide("rework")

    def install_legacy_unbound_v1_worker_source(self) -> None:
        payload = self.runtime.production_state.load()
        stored = payload["records"][REF]
        stored["worker_head_run"] = _legacy_unbound_v1_run(
            stored["worker_head_run"],
            root=self.data_dir / "codex-sessions",
        )
        self.runtime.production_state.save(payload)
        self.host.provider_progress = lambda _task, record, _kind: provider_progress_for_run(
            head_ops.HeadRun.from_json(record.worker_head_run)
        )

    def test_an_uninterrupted_review_red_rework_opens_the_next_round(self) -> None:
        """The baseline the interrupted rework below has to end up matching."""
        self.rework_after_red_review()

        self.assertEqual(self.tick()["action"], "rework-started")
        record = self.record()
        assert record is not None
        self.assertEqual(record.attempt_round, 2)

    def test_an_adopted_review_red_rework_lands_on_the_round_it_reserved(self) -> None:
        """Recovery resumes the rework's own round, not the one the red verdict closed.

        The round is reserved before the intent goes to disk precisely so a tick that dies between
        the relaunch and its record cannot collapse two rounds, with their routing and their
        verdicts, into one.
        """
        self.rework_after_red_review()
        with self.state_dies_after("restart_worker"), self.assertRaises(OSError):
            self.tick()

        intent = self.stored_intent()
        self.assertEqual((intent["round"], intent["opens_round"]), (2, True))

        adopted = self.tick()

        self.assertEqual(adopted["action"], "worker-launch-adopted")
        record = self.record()
        assert record is not None
        self.assertEqual(record.attempt_round, 2)
        # The previous round's heads go with it: the round records the adopted worker as its own,
        # and the reviewer of the round that was rejected is not carried over.
        self.assertEqual(record.worker_run.get("role"), "worker")
        self.assertEqual(record.review_run, {})

    def test_an_adopted_gate_red_rework_lands_on_the_round_it_reserved(self) -> None:
        self.run_to_validate()
        self.host.gate_results = [GateResult("red", "tests failed", log="boom")]
        with self.state_dies_after("restart_worker"), self.assertRaises(OSError):
            self.tick()

        intent = self.stored_intent()
        self.assertEqual(
            (intent["action"], intent["round"], intent["opens_round"]), ("gate-red-rework", 2, True)
        )

        adopted = self.tick()

        self.assertEqual(adopted["action"], "worker-launch-adopted")
        self.assertEqual(self.host.calls.count("restart_worker"), 1)
        record = self.record()
        assert record is not None
        self.assertEqual(record.attempt_round, 2)

    def test_a_red_delivery_that_outlives_its_tick_replays_without_a_second_worker(self) -> None:
        """The durable resuming state masks the old done report on recovery."""
        self.host.fail_resume_worker_reason = ""
        self.run_to_validate()
        self.host.gate_results = [GateResult("red", "tests failed", log="boom")]

        with self.state_dies_after("resume_worker"), self.assertRaises(OSError):
            self.tick()

        retained = self.record()
        assert retained is not None
        self.assertEqual(retained.worker_continuation.stage, WorkerContinuationStage.DELIVERY_PENDING)
        self.assertEqual(self.host.calls.count("restart_worker"), 0)

        recovered = self.tick()

        self.assertEqual(recovered["action"], "gate-red-reused-worker")
        self.assertEqual(self.host.calls.count("restart_worker"), 0)
        self.assertEqual(self.record().state, "claimed")  # type: ignore[union-attr]

    def test_a_done_report_after_interrupted_red_delivery_is_not_sent_again(self) -> None:
        """The worker may finish before recovery checkpoints its already-delivered prompt."""
        self.host.fail_resume_worker_reason = ""
        self.run_to_validate()
        self.host.gate_results = [GateResult("red", "tests failed", log="boom")]

        with self.state_dies_after("resume_worker"), self.assertRaises(OSError):
            self.tick()

        self.report_done()
        recovered = self.tick()

        self.assertEqual(recovered["action"], "gate-red-reused-worker")
        self.assertEqual(self.host.calls.count("resume_worker"), 1)
        self.assertEqual(self.host.calls.count("restart_worker"), 0)

    def test_an_unnamed_worker_is_swept_by_workspace_before_replacement(self) -> None:
        record = DispatcherRecord(
            worker="worker",
            workspace=str(self.data_dir / "workspace"),
            handle="",
            head="codex",
            review_head="codex-reviewer",
            attempt_id="attempt",
            comment_baseline=0,
            review_baseline=0,
            state="claimed",
            claimed_at=0.0,
        )

        outcome = self.runtime._stop_worker_confirmed(record, REF, step="gate", attempt_id="attempt")

        self.assertIsNone(outcome)
        self.assertIn("stop_workspace", self.host.calls)

    def test_a_legacy_red_delivery_without_a_v1_baseline_blocks_without_waking_it(self) -> None:
        self.host.fail_resume_worker_reason = ""
        self.tick()
        record = self.record()
        assert record is not None
        record.worker_continuation.begin_retention(time.time())
        record.worker_continuation.confirm_validation_move()
        record.worker_continuation.begin_delivery("merge-gate", time.time())
        payload = self.runtime.production_state.load()
        payload["records"][REF] = record.to_json()
        self.runtime.production_state.save(payload)
        recovered = self.tick()

        self.assertEqual(recovered["action"], "merge-gate-red-continuation-liveness-unavailable")
        self.assertNotIn("resume_worker", self.host.calls)
        self.assertEqual(self.reader.show(REF)["state"], "blocked")

    def test_a_legacy_red_review_delivery_without_a_v1_baseline_blocks_safely(self) -> None:
        """A hand-made historical delivery cannot bind an arbitrary current provider source."""
        self.host.fail_resume_worker_reason = ""
        self.tick()
        record = self.record()
        assert record is not None
        record.worker_continuation.begin_retention(time.time())
        record.worker_continuation.confirm_validation_move()
        record.worker_continuation.begin_delivery("review", time.time())
        payload = self.runtime.production_state.load()
        payload["records"][REF] = record.to_json()
        self.runtime.production_state.save(payload)

        recovered = self.tick()

        self.assertEqual(recovered["action"], "review-red-continuation-liveness-unavailable")
        self.assertEqual(self.host.calls.count("restart_worker"), 0)
        self.assertNotIn("resume_worker", self.host.calls)
        self.assertEqual(self.reader.show(REF)["state"], "blocked")

    def test_a_freeze_crash_replays_the_validate_move_without_waking_the_worker(self) -> None:
        self.tick()
        self.report_done()
        real_move = self.writer.move

        def die_before_move(**kwargs):
            if kwargs.get("target") == "validate":
                raise OSError("dispatcher died before board move")
            return real_move(**kwargs)

        with mock.patch.object(self.writer, "move", die_before_move), self.assertRaises(OSError):
            self.tick()

        retained = self.record()
        assert retained is not None
        self.assertEqual(
            retained.worker_continuation.stage,
            WorkerContinuationStage.VALIDATION_MOVE_PENDING,
        )
        self.assertEqual(self.reader.show(REF)["state"], "in_progress")

        recovered = self.tick()

        self.assertEqual(recovered["to"], "validate")
        self.assertEqual(self.host.calls.count("retain_worker"), 1)
        self.assertEqual(self.host.calls.count("restart_worker"), 0)

    def test_a_crash_after_validate_move_keeps_the_worker_frozen_for_review(self) -> None:
        self.tick()
        self.report_done()
        real_save = self.runtime.production_state.save

        def die_after_move(payload: dict) -> None:
            record = payload.get("records", {}).get(REF, {})
            if record.get("state") == "validate":
                raise OSError("dispatcher died after board move")
            real_save(payload)

        with mock.patch.object(self.runtime.production_state, "save", die_after_move):
            with self.assertRaises(OSError):
                self.tick()

        self.assertEqual(self.reader.show(REF)["state"], "validate")
        retained = self.record()
        assert retained is not None
        self.assertEqual(
            retained.worker_continuation.stage,
            WorkerContinuationStage.VALIDATION_MOVE_PENDING,
        )
        self.host.gate_results = [GateResult("green", "passed")]

        recovered = self.tick()

        self.assertEqual(recovered["action"], "review-started")
        # The worker stays suspended for the reviewer instead of being stopped: the checkout is
        # still untouched, and a red verdict has a conversation to hand the findings back to.
        self.assertNotIn("stop_head:worker", self.host.calls)
        self.assertLess(self.host.calls.index("confirm_worker_retained"), len(self.host.calls))

    def fail_the_red_move(self):
        """The red intent reaches the disk and the board move behind it does not.

        A refusing move and a process that dies just before it leave the same thing on disk: an
        open red transition over a card the board still shows in Validate.
        """
        real_move = self.writer.move

        def move(**kwargs):
            if kwargs.get("target") == "in_progress":
                raise OSError("dispatcher died before the red board move")
            return real_move(**kwargs)

        return mock.patch.object(self.writer, "move", move)

    def assert_red_intent_open_in_validate(self, phase: str) -> None:
        """A red transition whose move did not land, over a card the board has not moved.

        The column that card sits in depends on which red opened the transition: a mechanical
        gate opens one in Validate, a rework decision opens one over a card already parked in
        Assessment. Either way the transition is what the record owes and the board has not moved.
        """
        self.assertIn(self.reader.show(REF)["state"], ("validate", "assessment"))
        stranded = self.record()
        assert stranded is not None
        self.assertEqual(stranded.worker_continuation.stage, WorkerContinuationStage.RED_TRANSITION_PENDING)
        self.assertEqual(stranded.worker_continuation.phase, phase)

    def test_a_red_gate_intent_before_the_board_move_outranks_a_fresh_green_gate(self) -> None:
        """The recorded red verdict is not up for re-decision by the next tick's rollup.

        Between the two ticks CI can turn green: the failing job is retried, a flake settles, or the
        rollup simply finishes. Reading the gate again there would start a reviewer over a card that
        already owes its worker a red round, and the red verdict would be gone from the record with
        nothing having delivered it.
        """
        self.host.fail_resume_worker_reason = ""
        self.run_to_validate()
        self.host.gate_results = [GateResult("red", "tests failed", log="boom")]

        with self.fail_the_red_move(), self.assertRaises(OSError):
            self.tick()

        self.assert_red_intent_open_in_validate("gate")
        self.host.gate_results = [GateResult("green", "passed")]

        recovered = self.tick()

        self.assertEqual(recovered["action"], "gate-red-reused-worker")
        self.assertEqual(self.reader.show(REF)["state"], "in_progress")
        self.assertEqual(self.host.calls.count("start_review"), 0)
        self.assertEqual(self.host.calls.count("resume_worker"), 1)
        self.assertEqual(self.host.calls.count("restart_worker"), 0)
        self.assertIn("gate red continuation: reused", self.reader.show(REF)["comments"][-1]["body"])

    def test_a_red_review_intent_before_the_board_move_is_finished_as_that_intent(self) -> None:
        self.host.fail_resume_worker_reason = ""
        self.rework_after_red_review()

        with self.fail_the_red_move(), self.assertRaises(OSError):
            self.tick()

        self.assert_red_intent_open_in_validate("review")

        recovered = self.tick()

        self.assertEqual(recovered["action"], "review-red-reused-worker")
        self.assertEqual(self.reader.show(REF)["state"], "in_progress")
        self.assertEqual(self.host.calls.count("start_review"), 1, "no second reviewer is spawned")
        self.assertEqual(self.host.calls.count("resume_worker"), 1)
        self.assertEqual(self.host.calls.count("restart_worker"), 0)
        self.assertIn("review red continuation: reused", self.reader.show(REF)["comments"][-1]["body"])

    def test_a_red_gate_intent_without_a_session_still_moves_and_replaces_once(self) -> None:
        """Nothing to reuse is not a lesser transition: the replacement is owed just the same."""
        self.without_a_retained_session()
        self.run_to_validate()
        self.host.gate_results = [GateResult("red", "tests failed", log="boom")]

        with self.fail_the_red_move(), self.assertRaises(OSError):
            self.tick()

        self.assert_red_intent_open_in_validate("gate")
        self.assertEqual(self.host.calls.count("restart_worker"), 0)
        self.host.gate_results = [GateResult("green", "passed")]

        recovered = self.tick()

        self.assertEqual(recovered["action"], "gate-red-rework")
        self.assertEqual(self.reader.show(REF)["state"], "in_progress")
        self.assertEqual(self.host.calls.count("start_review"), 0)
        self.assertEqual(self.host.calls.count("restart_worker"), 1)

    def test_a_red_review_intent_without_a_session_still_moves_and_replaces_once(self) -> None:
        self.without_a_retained_session()
        self.rework_after_red_review()

        with self.fail_the_red_move(), self.assertRaises(OSError):
            self.tick()

        self.assert_red_intent_open_in_validate("review")
        self.assertEqual(self.host.calls.count("restart_worker"), 0)

        recovered = self.tick()

        self.assertEqual(recovered["action"], "rework-started")
        self.assertEqual(self.reader.show(REF)["state"], "in_progress")
        self.assertEqual(self.host.calls.count("start_review"), 1)
        self.assertEqual(self.host.calls.count("restart_worker"), 1)

    def die_after_red_move(self):
        """A tick that moves the card back to In progress and never records why.

        The red intent is written before the move, so the only saves this refuses are the delivery
        boundary and everything after it.
        """
        real_save = self.runtime.production_state.save

        def save(payload: dict) -> None:
            if self.reader.show(REF)["state"] == "in_progress":
                raise OSError("dispatcher died after red board move")
            real_save(payload)

        return mock.patch.object(self.runtime.production_state, "save", save)

    def test_a_crash_after_the_red_gate_move_delivers_the_continuation_it_intended(self) -> None:
        """The Validate handoff of the closed round is never replayed by its own done report."""
        self.host.fail_resume_worker_reason = ""
        self.run_to_validate()
        self.host.gate_results = [GateResult("red", "tests failed", log="boom")]

        with self.die_after_red_move(), self.assertRaises(OSError):
            self.tick()

        self.assertEqual(self.reader.show(REF)["state"], "in_progress")
        retained = self.record()
        assert retained is not None
        self.assertEqual(
            retained.worker_continuation.stage,
            WorkerContinuationStage.RED_TRANSITION_PENDING,
        )
        self.assertEqual(retained.worker_continuation.phase, "gate")

        recovered = self.tick()

        self.assertEqual(recovered["action"], "gate-red-reused-worker")
        self.assertEqual(self.reader.show(REF)["state"], "in_progress")
        self.assertEqual(self.host.calls.count("restart_worker"), 0)
        self.assertEqual(self.host.calls.count("resume_worker"), 1)

    def test_a_crash_after_the_red_review_move_delivers_the_continuation_it_intended(self) -> None:
        self.host.fail_resume_worker_reason = ""
        self.run_to_validate()
        self.tick()  # reviewer up
        self.verdict("red", "needs work", "verdict-red")
        self.tick()  # the verdict parks the card
        self.decide("rework")

        with self.die_after_red_move(), self.assertRaises(OSError):
            self.tick()

        retained = self.record()
        assert retained is not None
        self.assertEqual(
            retained.worker_continuation.stage,
            WorkerContinuationStage.RED_TRANSITION_PENDING,
        )
        self.assertEqual(retained.worker_continuation.phase, "review")

        recovered = self.tick()

        self.assertEqual(recovered["action"], "review-red-reused-worker")
        self.assertEqual(self.host.calls.count("restart_worker"), 0)
        self.assertEqual(self.host.calls.count("resume_worker"), 1)

    def die_before_finishing_the_round(self):
        """The delivery is checkpointed and the tick dies before the round it opened is recorded."""

        def die(*_args, **_kwargs):
            raise OSError("dispatcher died after the delivery checkpoint")

        return mock.patch.object(dispatcher_worker_continuation, "_finish_retained_worker_resume", die)

    def test_a_confirmed_gate_red_delivery_is_finished_by_the_next_tick(self) -> None:
        self.host.fail_resume_worker_reason = ""
        self.run_to_validate()
        self.host.gate_results = [GateResult("red", "tests failed", log="boom")]

        with self.die_before_finishing_the_round(), self.assertRaises(OSError):
            self.tick()

        confirmed = self.record()
        assert confirmed is not None
        self.assertEqual(confirmed.worker_continuation.stage, WorkerContinuationStage.DELIVERY_CONFIRMED)
        self.assertEqual(confirmed.attempt_round, 1)

        recovered = self.tick()

        self.assertEqual(recovered["action"], "gate-red-reused-worker")
        self.assertEqual(self.host.calls.count("resume_worker"), 1)
        self.assertEqual(self.host.calls.count("restart_worker"), 0)
        record = self.record()
        assert record is not None
        self.assertEqual(record.attempt_round, 2)
        self.assertEqual(record.worker_continuation.stage, WorkerContinuationStage.NONE)
        self.assertIn("gate red continuation: reused", self.reader.show(REF)["comments"][-1]["body"])

    def test_a_confirmed_review_red_delivery_is_finished_by_the_next_tick(self) -> None:
        self.host.fail_resume_worker_reason = ""
        self.rework_after_red_review()

        with self.die_before_finishing_the_round(), self.assertRaises(OSError):
            self.tick()

        confirmed = self.record()
        assert confirmed is not None
        self.assertEqual(confirmed.worker_continuation.stage, WorkerContinuationStage.DELIVERY_CONFIRMED)

        recovered = self.tick()

        self.assertEqual(recovered["action"], "review-red-reused-worker")
        self.assertEqual(self.host.calls.count("resume_worker"), 1)
        self.assertEqual(self.host.calls.count("restart_worker"), 0)
        record = self.record()
        assert record is not None
        self.assertEqual(record.attempt_round, 2)
        self.assertIn("review red continuation: reused", self.reader.show(REF)["comments"][-1]["body"])

    def test_a_session_that_lost_its_suspension_during_review_is_replaced_once(self) -> None:
        """The suspension confirmed before the reviewer started is not evidence at delivery time."""
        self.host.fail_resume_worker_reason = ""
        self.run_to_validate()
        self.tick()  # reviewer up over a confirmed suspended worker
        self.host.retained_worker_alive = False
        self.verdict("red", "needs work", "verdict-red")
        self.tick()  # the verdict parks the card
        self.decide("rework")

        outcome = self.tick()

        self.assertEqual(outcome["action"], "rework-started")
        self.assertEqual(self.host.calls.count("resume_worker"), 0)
        self.assertEqual(self.host.calls.count("stop_head:worker"), 1)
        self.assertEqual(self.host.calls.count("restart_worker"), 1)
        self.assertIn("review red continuation: replacement", self.reader.show(REF)["comments"][-1]["body"])

    def test_a_session_that_lost_its_suspension_before_the_red_gate_is_replaced_once(self) -> None:
        self.host.fail_resume_worker_reason = ""
        self.run_to_validate()
        self.host.gate_results = [GateResult("red", "tests failed", log="boom")]
        self.host.retained_worker_alive = False

        outcome = self.tick()

        self.assertEqual(outcome["action"], "gate-red-rework")
        self.assertEqual(self.host.calls.count("resume_worker"), 0)
        self.assertEqual(self.host.calls.count("stop_head:worker"), 1)
        self.assertEqual(self.host.calls.count("restart_worker"), 1)
        self.assertIn("gate red continuation: replacement", self.reader.show(REF)["comments"][-1]["body"])

    def lose_the_suspension_after_the_send(self):
        """The dispatcher dies between the send and its checkpoint, and the head wakes meanwhile.

        Terminal recovery and an operator both do this: the record's `delivery_pending` says a
        prompt went out to a session that was suspended one tick ago, which is not the same fact as
        that session being suspended now.
        """
        return self.state_dies_after("resume_worker")

    def test_a_pending_delivery_that_lost_its_suspension_is_replaced_once(self) -> None:
        """Recovery asks the heartbeat again instead of resuming on the dead tick's answer."""
        self.host.fail_resume_worker_reason = ""
        self.run_to_validate()
        self.host.gate_results = [GateResult("red", "tests failed", log="boom")]
        with self.lose_the_suspension_after_the_send(), self.assertRaises(OSError):
            self.tick()
        pending = self.record()
        assert pending is not None
        self.assertEqual(pending.worker_continuation.stage, WorkerContinuationStage.DELIVERY_PENDING)
        confirmations = self.host.calls.count("confirm_worker_retained")
        self.host.retained_worker_alive = False

        recovered = self.tick()

        self.assertEqual(recovered["action"], "gate-red-rework")
        self.assertGreater(
            self.host.calls.count("confirm_worker_retained"),
            confirmations,
            "the suspension is confirmed again at the boundary, not inherited from the dead tick",
        )
        self.assertEqual(self.host.calls.count("resume_worker"), 1, "the woken head is not typed into")
        self.assertEqual(self.host.calls.count("stop_head:worker"), 1)
        self.assertEqual(self.host.calls.count("restart_worker"), 1)
        self.assertIn("gate red continuation: replacement", self.reader.show(REF)["comments"][-1]["body"])

    def test_a_pending_review_delivery_that_lost_its_suspension_is_replaced_once(self) -> None:
        self.host.fail_resume_worker_reason = ""
        self.rework_after_red_review()
        with self.lose_the_suspension_after_the_send(), self.assertRaises(OSError):
            self.tick()
        confirmations = self.host.calls.count("confirm_worker_retained")
        self.host.retained_worker_alive = False

        recovered = self.tick()

        self.assertEqual(recovered["action"], "rework-started")
        self.assertGreater(self.host.calls.count("confirm_worker_retained"), confirmations)
        self.assertEqual(self.host.calls.count("resume_worker"), 1)
        self.assertEqual(self.host.calls.count("stop_head:worker"), 1)
        self.assertEqual(self.host.calls.count("restart_worker"), 1)
        self.assertIn("review red continuation: replacement", self.reader.show(REF)["comments"][-1]["body"])

    def without_a_retained_session(self) -> None:
        """A round whose worker has no conversation to keep: a one-shot head or a lost pane."""
        self.host.fail_retain_worker_reason = "worker session has no addressable pane to retain"

    def test_a_gate_red_crash_without_a_session_replaces_instead_of_replaying_validate(self) -> None:
        """The red intent is durable even when there is nothing to reuse.

        Without it the record would still say Validate, still name the report that closed the round,
        and the next tick would hand that report to the gate a second time while the card sat In
        progress waiting for a worker nobody launched.
        """
        self.without_a_retained_session()
        self.run_to_validate()
        self.host.gate_results = [GateResult("red", "tests failed", log="boom")]

        with self.die_after_red_move(), self.assertRaises(OSError):
            self.tick()

        self.assertEqual(self.reader.show(REF)["state"], "in_progress")
        stranded = self.record()
        assert stranded is not None
        self.assertEqual(stranded.worker_continuation.stage, WorkerContinuationStage.RED_TRANSITION_PENDING)
        self.assertFalse(stranded.worker_continuation.retained)
        self.assertEqual(self.host.calls.count("restart_worker"), 0)

        recovered = self.tick()

        self.assertEqual(recovered["action"], "gate-red-rework")
        self.assertNotIn("to", recovered, "the closed round's report is never a new completion")
        self.assertEqual(self.reader.show(REF)["state"], "in_progress")
        self.assertEqual(self.host.calls.count("restart_worker"), 1)

    def test_a_review_red_crash_without_a_session_replaces_instead_of_replaying_validate(self) -> None:
        self.without_a_retained_session()
        self.rework_after_red_review()

        with self.die_after_red_move(), self.assertRaises(OSError):
            self.tick()

        stranded = self.record()
        assert stranded is not None
        self.assertEqual(stranded.worker_continuation.stage, WorkerContinuationStage.RED_TRANSITION_PENDING)
        self.assertFalse(stranded.worker_continuation.retained)
        self.assertEqual(self.host.calls.count("restart_worker"), 0)

        recovered = self.tick()

        self.assertEqual(recovered["action"], "rework-started")
        self.assertNotIn("to", recovered)
        self.assertEqual(self.reader.show(REF)["state"], "in_progress")
        self.assertEqual(self.host.calls.count("restart_worker"), 1)

    def assert_replacement_still_owed(self) -> None:
        """The board moved for a red verdict and no head was launched: something must still owe one.

        The launch intent was to take the transition over, and the write that would have made that
        handover durable refused. What is left on disk is the only thing a next tick can read.
        """
        self.assertEqual(self.reader.show(REF)["state"], "in_progress")
        self.assertEqual(self.host.calls.count("restart_worker"), 0)
        self.assertEqual(self.stored_intent(), {})
        stranded = self.record()
        assert stranded is not None
        self.assertIn(
            stranded.worker_continuation.stage,
            {
                WorkerContinuationStage.RED_TRANSITION_PENDING,
                WorkerContinuationStage.DELIVERY_PENDING,
            },
        )
        self.assertFalse(stranded.worker_continuation.retained, "the old session was stopped, not kept")

    def assert_one_replacement_on_the_rework_round(self, action: str) -> None:
        recovered = self.tick()

        self.assertEqual(recovered["action"], action)
        self.assertNotIn("to", recovered, "the closed round's report is never a new completion")
        self.assertEqual(self.host.calls.count("restart_worker"), 1)
        record = self.record()
        assert record is not None
        self.assertEqual((record.state, record.attempt_round), ("claimed", 2))
        self.assertIn("red continuation: replacement", self.reader.show(REF)["comments"][-1]["body"])

    def test_a_gate_red_replacement_that_cannot_write_its_intent_keeps_its_transition(self) -> None:
        """A refused handover is not a completed one.

        The intent write is where the red transition is meant to change hands. When it fails the
        transition has gone nowhere, and dropping it from the record would leave the card In
        progress with nothing durable owing it a worker: no rework round, no replacement, and no
        continuation entry on the card.
        """
        self.without_a_retained_session()
        self.run_to_validate()
        self.host.gate_results = [GateResult("red", "tests failed", log="boom")]

        with self.fail_launch_intent_save():
            outcome = self.tick()

        self.assertEqual(outcome["action"], "worker-launch-intent-unwritable")
        self.assert_replacement_still_owed()
        self.assert_one_replacement_on_the_rework_round("gate-red-rework")

    def test_a_review_red_replacement_that_cannot_write_its_intent_keeps_its_transition(self) -> None:
        self.without_a_retained_session()
        self.rework_after_red_review()

        with self.fail_launch_intent_save():
            outcome = self.tick()

        self.assertEqual(outcome["action"], "worker-launch-intent-unwritable")
        self.assert_replacement_still_owed()
        self.assert_one_replacement_on_the_rework_round("rework-started")

    def test_a_refused_gate_continuation_that_cannot_write_its_intent_keeps_its_transition(self) -> None:
        """The same window, entered from the other fallback: the session refused the continuation."""
        self.run_to_validate()
        self.host.gate_results = [GateResult("red", "tests failed", log="boom")]

        with self.fail_launch_intent_save():
            outcome = self.tick()

        self.assertEqual(outcome["action"], "worker-launch-intent-unwritable")
        self.assertEqual(self.host.calls.count("stop_head:worker"), 1)
        self.assert_replacement_still_owed()
        self.assert_one_replacement_on_the_rework_round("gate-red-rework")

    def test_a_refused_review_continuation_that_cannot_write_its_intent_keeps_its_transition(self) -> None:
        self.rework_after_red_review()

        with self.fail_launch_intent_save():
            outcome = self.tick()

        self.assertEqual(outcome["action"], "worker-launch-intent-unwritable")
        self.assert_replacement_still_owed()
        self.assert_one_replacement_on_the_rework_round("rework-started")

    def test_a_dead_rework_intent_relaunches_inside_the_round_it_reserved(self) -> None:
        """The reservation outlives the head the rework never got.

        The round is the one thing the intent knows and the record does not: the record still
        carries the round the red verdict closed. A dead rework head that gave its reservation back
        would put the relaunch, its routing and its next verdict inside the rejected round.
        """
        self.rework_after_red_review()
        self.host.head_pid = DEAD_PID
        with self.state_dies_after("restart_worker"), self.assertRaises(OSError):
            self.tick()

        self.assertEqual((self.stored_intent()["round"], self.stored_intent()["opens_round"]), (2, True))

        # Nothing of that launch is running: the intent's liveness check reads the dead
        # heartbeat at the intent's pid path, stops the leftover, clears the reservation
        # holder and hands the card back to the ordinary path inside the same tick. What that
        # path finds is a card in an active state owing a worker nothing on the record can name
        # -- no pane, no heartbeat, no intent -- and the headless recovery relaunches it on the
        # retained checkout inside the round the rework reserved (secretary-1544).
        #
        # No clock, ladder or scripted terminal status takes part. Before this round the test
        # scripted a `missing-terminal` answer and aged a vitality episode until a confirmed
        # stall respawned the head; that only worked because the fake attached a provider cursor
        # to a shape production probes no provider for, and a record with no head is not
        # something a watchdog can observe at all.
        self.host.head_pid = os.getpid()
        self.kill_worker_heartbeat(path=self.stored_intent().get("pid_file") or "")
        recovered = self.tick()

        self.assertEqual(recovered["action"], "headless-worker-replacement-launched")
        record = self.record()
        assert record is not None
        self.assertEqual(record.attempt_round, 2, "the rework runs in the round it reserved")
        self.assertEqual(self.stored_intent(), {})
        self.assertEqual(self.host.calls.count("restart_worker"), 2, "one head, once the first died")
        rounds = [
            event["payload"]["attempt"]
            for event in self.runtime.audit.events(REF, kind="routing")
            if event["payload"].get("phase") == "worker"
        ]
        self.assertEqual(rounds[-1], 2, "the respawned head is recorded by the rework's round")

    def test_a_respawn_adoption_stays_inside_its_round(self) -> None:
        """A respawn continues the round it interrupted, so its intent reserves nothing."""
        self.tick()
        self.kill_worker_heartbeat()
        self.host.worker_status_result = {"known": True, "live": False, "reason": "missing-terminal"}
        with self.state_dies_after("restart_worker"), self.assertRaises(OSError):
            self.tick()

        self.assertFalse(self.stored_intent()["opens_round"])

        self.tick()

        record = self.record()
        assert record is not None
        self.assertEqual(record.attempt_round, 1)

    # worker: a journal that refuses instead of a state plane ------------------

    def test_an_audit_that_refuses_the_claim_launches_no_worker_at_all(self) -> None:
        with self.refuse_audit("-claim-"), self.assertRaises(OSError):
            self.tick()

        self.assertEqual(self.host.prepared, [], "no head may exist that no record can find")
        self.assertEqual(self.stored_intent(), {})
        self.assertIsNone(self.record())

        recovered = self.tick()

        self.assertEqual(recovered["step"], "claim")
        self.assertEqual(self.host.prepared, [REF], "exactly one head, on the retry")

    def test_an_audit_that_refuses_after_the_claim_leaves_the_head_on_the_record(self) -> None:
        """The record's own save is past by then, so the head is findable either way.

        What the refused write costs is the round's routing event, and that is why the intent is
        not spent yet: the next tick adopts the same head and writes it.
        """
        with self.audit_dies_after("prepare_worker"), self.assertRaises(OSError):
            self.tick()

        self.assertEqual(self.host.prepared, [REF])
        record = self.record()
        assert record is not None
        self.assertEqual(record.state, "claimed")
        self.assertTrue(record.handle, "the launched head must be on the record")
        self.assertEqual(self.stored_intent()["action"], "claim")

        # The next tick reads a card that already has its head, and starts nothing beside it.
        adopted = self.tick()

        self.assertEqual(adopted["action"], "worker-launch-adopted")
        self.assertEqual(self.host.prepared, [REF])
        attempt = self.routing_history()[-1]
        assert attempt.worker is not None
        self.assertEqual(attempt.worker.head, "codex", "the round gets the head that ran it")
        self.assertEqual(self.stored_intent(), {})

    def test_an_audit_that_refuses_after_a_rework_recovers_that_rework_once(self) -> None:
        self.rework_after_red_review()
        with self.audit_dies_after("restart_worker"), self.assertRaises(OSError):
            self.tick()

        self.assertEqual(self.host.calls.count("restart_worker"), 1)
        self.assertEqual(self.stored_intent()["action"], "review-red-rework")

        adopted = self.tick()

        self.assertEqual(adopted["action"], "worker-launch-adopted")
        self.assertEqual(self.host.calls.count("restart_worker"), 1)
        record = self.record()
        assert record is not None
        self.assertEqual((record.state, record.attempt_round), ("claimed", 2))

    # reviewer ---------------------------------------------------------------

    def test_the_review_launch_intent_is_on_disk_before_the_host_is_called(self) -> None:
        self.run_to_validate()
        seen: list[dict] = []
        real = self.host.start_review

        def spy(*args, **kwargs):
            seen.append(self.stored_intent())
            return real(*args, **kwargs)

        with mock.patch.object(self.host, "start_review", spy):
            self.tick()

        intent = seen[0]
        self.assertEqual(intent["role"], "review")
        self.assertEqual(intent["action"], "review-started")
        self.assertEqual(intent["head"], "codex-reviewer")
        self.assertEqual(intent["workspace"], str(self.data_dir / "workspaces" / f"{REF}-pilot"))
        self.assertEqual(self.stored_intent(), {})

    def test_state_that_cannot_be_written_launches_no_reviewer_at_all(self) -> None:
        self.run_to_validate()

        with self.fail_launch_intent_save():
            outcome = self.tick()

        self.assertEqual(outcome["action"], "review-infrastructure-retry")
        self.assertIn("state is not writable", outcome["reason"])
        self.assertEqual(self.host.reviews, [])

        recovered = self.tick()

        self.assertEqual(recovered["step"], "review")
        self.assertEqual(self.host.reviews, [REF], "exactly one reviewer, on the retry")

    def test_a_review_launch_that_outlived_its_tick_is_adopted_not_doubled(self) -> None:
        self.run_to_validate()
        with self.state_dies_after("start_review"), self.assertRaises(OSError):
            self.tick()

        self.assertEqual(self.host.reviews, [REF])
        self.assertEqual(self.stored_intent()["role"], "review")

        adopted = self.tick()

        self.assertEqual(adopted["action"], "review-launch-adopted")
        self.assertEqual(self.host.reviews, [REF], "the live reviewer must not be doubled")
        record = self.record()
        assert record is not None
        self.assertEqual(record.state, "reviewing")
        # The merge gate refuses a verdict it cannot tie to a checkout, so the adopted reviewer
        # gets the commit its launch was pinned at.
        self.assertEqual(record.review_commit, self.host.commit)

        # The verdict of the adopted reviewer lands on the card it was launched for.
        self.verdict("green", "looks good", "verdict-green")
        self.assertEqual(self.release_after_green_verdict()["to"], "done")
        self.assertEqual(self.host.reviews, [REF])

    def test_a_review_intent_whose_head_died_starts_exactly_one_replacement(self) -> None:
        self.run_to_validate()
        self.host.head_pid = DEAD_PID
        with self.state_dies_after("start_review"), self.assertRaises(OSError):
            self.tick()

        self.host.head_pid = os.getpid()
        # The reviewer of the lost tick is gone, so the card is back to needing one.
        self.host.review_status_result = {"known": True, "live": False, "reason": "missing-terminal"}
        restarted = self.tick()

        self.assertEqual(restarted["step"], "review")
        self.assertEqual(self.host.reviews, [REF, REF])
        self.assertEqual(self.stored_intent(), {})

    def test_a_live_foreign_reviewer_heartbeat_is_fenced_without_a_stop_or_replacement(self) -> None:
        self.run_to_validate()
        with self.state_dies_after("start_review"), self.assertRaises(OSError):
            self.tick()
        self.replace_intent_heartbeat_run()

        fenced = self.tick()

        self.assertEqual(fenced["action"], "review-heartbeat-identity-mismatch")
        self.assertEqual(self.host.reviews, [REF])
        self.assertNotIn("stop_review", self.host.calls)

    def test_a_review_intent_without_a_heartbeat_waits_out_its_grace_window(self) -> None:
        self.run_to_validate()
        self.host.head_pid = None
        with self.state_dies_after("start_review"), self.assertRaises(OSError):
            self.tick()

        pending = self.tick()

        self.assertEqual(pending["action"], "review-launch-pending")
        self.assertEqual(self.host.reviews, [REF])

    # reviewer: a journal that refuses instead of a state plane ---------------

    def test_an_audit_that_refuses_the_launch_request_starts_no_reviewer(self) -> None:
        """The launch request comment is the reviewer's own pre-launch journal write."""
        self.run_to_validate()

        with self.refuse_audit("start-intent"), self.assertRaises(OSError):
            self.tick()

        self.assertEqual(self.host.reviews, [])
        self.assertEqual(self.stored_intent(), {})

        recovered = self.tick()

        self.assertEqual(recovered["step"], "review")
        self.assertEqual(self.host.reviews, [REF], "exactly one reviewer, on the retry")

    def test_an_audit_that_refuses_after_the_review_launch_adopts_that_reviewer(self) -> None:
        """The reviewer's routing write is before the record's save, so the intent is what survives."""
        self.run_to_validate()
        with self.audit_dies_after("start_review"), self.assertRaises(OSError):
            self.tick()

        self.assertEqual(self.host.reviews, [REF])
        self.assertEqual(self.stored_intent()["role"], "review")

        adopted = self.tick()

        self.assertEqual(adopted["action"], "review-launch-adopted")
        self.assertEqual(self.host.reviews, [REF], "the live reviewer must not be doubled")
        record = self.record()
        assert record is not None
        self.assertEqual(record.state, "reviewing")

        # And the adopted reviewer's verdict still lands on the card it was launched for.
        self.verdict("green", "looks good", "verdict-green")
        self.assertEqual(self.release_after_green_verdict()["to"], "done")

    # an adopted head belongs to the round's routing history ------------------

    def routing_history(self) -> list:
        return routing_attempts(task_audit_for(self.board).events(REF, kind="routing"))

    def test_an_adopted_worker_is_recorded_as_the_round_that_ran_it(self) -> None:
        """The head an interrupted tick launched is a head that ran, so the round has to name it.

        The tick that started it died before writing its routing event, and nothing after adoption
        writes one either: without this the round's verdict names only the reviewer, and the
        history reads as a round nobody worked.
        """
        with self.state_dies_after("prepare_worker"), self.assertRaises(OSError):
            self.tick()

        self.assertEqual(self.tick()["action"], "worker-launch-adopted")

        attempt = self.routing_history()[-1]
        self.assertEqual(attempt.attempt, 1)
        assert attempt.worker is not None
        # The launch snapshot the interrupted tick fixed on disk, not a fresh read of a registry
        # that may have moved since.
        self.assertEqual((attempt.worker.role, attempt.worker.head), ("worker", "codex"))

        # And the verdict the round ends on carries that worker beside its reviewer.
        self.report_done()
        self.assertEqual(self.tick()["to"], "validate")
        self.assertEqual(self.tick()["action"], "review-started")
        self.verdict("green", "looks good", "verdict-green")
        self.assertEqual(self.release_after_green_verdict()["to"], "done")

        attempt = self.routing_history()[-1]
        assert attempt.worker is not None and attempt.reviewer is not None
        self.assertEqual(attempt.outcome, "green")
        self.assertEqual((attempt.worker.head, attempt.reviewer.head), ("codex", "codex-reviewer"))

    def test_an_adopted_reviewer_is_recorded_as_the_head_that_judged_the_round(self) -> None:
        self.run_to_validate()
        with self.state_dies_after("start_review"), self.assertRaises(OSError):
            self.tick()

        self.assertEqual(self.tick()["action"], "review-launch-adopted")

        attempt = self.routing_history()[-1]
        assert attempt.reviewer is not None
        self.assertEqual((attempt.reviewer.role, attempt.reviewer.head), ("reviewer", "codex-reviewer"))

        self.verdict("green", "looks good", "verdict-green")
        self.assertEqual(self.release_after_green_verdict()["to"], "done")

        attempt = self.routing_history()[-1]
        assert attempt.worker is not None and attempt.reviewer is not None
        self.assertEqual(attempt.outcome, "green")
        self.assertEqual(attempt.reviewer.head, "codex-reviewer")

    def test_a_journal_that_refuses_an_adopted_head_keeps_the_intent_for_the_next_tick(self) -> None:
        """The routing write is the last thing adoption owes that head, and it can refuse.

        Spending the intent on a round whose history is missing would leave the head with no record
        of its launch at all; keeping it costs one more adoption instead.
        """
        with self.state_dies_after("prepare_worker"), self.assertRaises(OSError):
            self.tick()

        with self.refuse_audit("routing-worker"):
            deferred = self.tick()

        self.assertEqual(deferred["action"], "worker-launch-adopt-deferred")
        self.assertEqual(deferred["status"], "degraded")
        self.assertEqual(self.stored_intent()["role"], "worker", "the intent outlives the refusal")
        self.assertEqual(self.host.prepared, [REF])

        adopted = self.tick()

        self.assertEqual(adopted["action"], "worker-launch-adopted")
        self.assertEqual(self.host.prepared, [REF], "the retry adopts, it does not relaunch")
        attempt = self.routing_history()[-1]
        assert attempt.worker is not None
        self.assertEqual(attempt.worker.head, "codex")

    # an adopted head has a lifecycle, not only a pid -------------------------

    def adopt_worker(self) -> None:
        """Leave the card with a live worker head that no pane handle points at."""
        with self.state_dies_after("prepare_worker"), self.assertRaises(OSError):
            self.tick()
        self.assertEqual(self.tick()["action"], "worker-launch-adopted")
        record = self.record()
        assert record is not None
        self.assertEqual(record.handle, "", "an adopted head never had a pane recorded")
        self.assertEqual(record.worker_pid_file, pid_file_path("worker", REF))

    def adopt_reviewer(self) -> None:
        self.run_to_validate()
        with self.state_dies_after("start_review"), self.assertRaises(OSError):
            self.tick()
        self.assertEqual(self.tick()["action"], "review-launch-adopted")
        record = self.record()
        assert record is not None
        self.assertEqual(record.review_handle, "")
        self.assertEqual(record.review_pid_file, pid_file_path("review", REF))

    def head_alive(self, kind: str) -> bool:
        return Path(pid_file_path(kind, REF)).exists()

    def test_an_adopted_worker_is_stopped_before_the_reviewer_takes_the_checkout(self) -> None:
        """The freeze goes by heartbeat when there is no pane to close.

        Without it the adopted worker keeps editing the tree the reviewer was launched to judge,
        and the verdict describes a checkout that no longer exists.
        """
        self.adopt_worker()
        self.report_done()

        self.assertEqual(self.tick()["to"], "validate")
        self.assertEqual(self.tick()["action"], "review-started")

        self.assertIn("stop_head:worker", self.host.calls)
        self.assertFalse(self.head_alive("worker"), "the adopted worker must be stopped")
        record = self.record()
        assert record is not None
        self.assertEqual((record.worker_pid_file, record.handle), ("", ""))

    def test_an_adopted_reviewer_is_stopped_by_the_red_verdict_it_returns(self) -> None:
        self.adopt_reviewer()
        self.verdict("red", "needs work", "verdict-red")
        self.tick()  # the verdict parks the card
        self.decide("rework")

        rework = self.tick()

        self.assertEqual(rework["action"], "rework-started")
        self.assertIn("stop_review", self.host.calls)
        self.assertFalse(self.head_alive("review"), "the adopted reviewer must be stopped")
        self.assertEqual(self.host.calls.count("restart_worker"), 1)
        record = self.record()
        assert record is not None
        self.assertEqual(record.review_pid_file, "")

    def test_an_adopted_reviewer_is_stopped_before_its_watchdog_respawns_it(self) -> None:
        self.adopt_reviewer()
        # The pane inventory cannot see a head that was adopted without a handle, which is exactly
        # the reading that used to respawn a reviewer beside a live one. The adopted head's
        # heartbeat names a gone process, so the reclaim is evidence-backed.
        self.kill_review_heartbeat()
        self.host.review_status_result = {"known": True, "live": False, "reason": "missing-terminal"}

        respawned = self.tick()

        self.assertEqual(respawned["action"], "review-respawned")
        self.assertFalse(
            self.head_alive("review") and self.host.head_pid is None,
            "the adopted reviewer is stopped before its replacement",
        )
        self.assertIn("stop_review", self.host.calls)
        self.assertEqual(self.host.reviews, [REF, REF], "one replacement, not one beside a live head")

    def test_a_reviewer_with_no_heartbeat_is_stopped_through_its_workspace(self) -> None:
        """A raw command override writes no heartbeat, so nothing names that reviewer.

        Past the grace window the launch counts as one that left nothing running, and the
        replacement used to open beside a reviewer that was in fact still up. The workspace is the
        only identity left, so that is what the stop goes through.
        """
        self.run_to_validate()
        self.host.head_pid = None
        with self.state_dies_after("start_review"), self.assertRaises(OSError):
            self.tick()
        self.age_intent(initial_output_stall_seconds() + 60)
        self.host.calls.clear()

        self.host.head_pid = os.getpid()
        self.host.review_status_result = {"known": True, "live": False, "reason": "missing-terminal"}
        restarted = self.tick()

        self.assertEqual(restarted["step"], "review")
        self.assertIn("stop_workspace", self.host.calls)
        self.assertLess(
            self.host.calls.index("stop_workspace"),
            self.host.calls.index("start_review"),
            "whatever the lost launch left is stopped before the replacement opens",
        )
        self.assertEqual(self.host.reviews, [REF, REF])

    # a bring-up that failed with its terminal already open -------------------

    def test_a_worker_bring_up_that_left_a_terminal_keeps_its_intent(self) -> None:
        """`HeadLaunchAborted` is not "no head exists", so the card is neither blocked nor dropped."""
        self.host.fail_prepare_error = HeadLaunchAborted(
            "prompt delivery failed; head terminal stop failed: orca refused",
            handle="term:leftover",
            leaf="leaf:leftover",
            workspace=str(self.data_dir / "workspaces" / f"{REF}-pilot"),
            pid_file=pid_file_path("worker", REF),
        )

        outcome = self.tick()

        self.assertEqual(outcome["action"], "worker-launch-aborted")
        self.assertEqual(outcome["status"], "degraded")
        self.assertEqual(self.reader.show(REF)["state"], "in_progress", "the card is not blocked")
        intent = self.stored_intent()
        self.assertEqual(
            (intent["role"], intent["handle"], intent["leaf"], intent["aborted"]),
            ("worker", "term:leftover", "leaf:leftover", True),
        )

        # And the head that terminal is running is adopted, pane included, rather than doubled.
        self.host.fail_prepare_error = None
        adopted = self.tick()

        self.assertEqual(adopted["action"], "worker-launch-adopted")
        self.assertEqual(self.host.calls.count("prepare_worker"), 1)
        record = self.record()
        assert record is not None
        self.assertEqual(record.handle, "term:leftover")
        self.assertEqual(record.worker_leaf, "leaf:leftover")

    # a live head that never received its pointer ---------------------------

    def test_a_blocked_reviewer_launch_is_not_adopted_because_its_pid_is_alive(self) -> None:
        """`issue:6afc6644`, incident secretary-1527, reproduced end to end.

        The delivery boundary got this right the first time: `readiness_state=blocked`,
        `turn_confirmed=false`, `send_accepted=false`, zero bytes written. The next tick then found
        the retained launch's pid alive and adopted it as `reviewing` anyway, and the system
        reported `waiting-review-verdict` for over an hour against a reviewer that had never
        received the document.

        A live pid, a writable pane and Orca's own `accepted` are not a delivered pointer. So the
        adoption refuses: no worker freeze, no `reviewing`, no routing event, and above all the
        intent is not spent — the reviewer stays recoverable and its pointer stays owed.
        """
        self.run_to_validate()
        self.host.calls.clear()
        evidence = {
            "subject": "reviewer-launch",
            "handle": f"review:{REF}",
            "stage": "none",
            "readiness_before": "blocked",
            "readiness_state": "blocked",
            "reason": "readiness-blocked",
            "turn_confirmed": False,
            "send_accepted": False,
            "bytes_written": 0,
        }

        def blocked_before_send(task: dict, record: DispatcherRecord) -> ReviewLaunch:
            self.host.calls.append("start_review")
            self.host.reviews.append(task["ref"])
            launched = self.host._launched(
                f"review:{task['ref']}",
                record.review_head,
                task,
                "reviewer",
                record.workspace,
                run_id=str(record.launch_intent["run_id"]),
            )
            self.host._write_head_pid("review", task["ref"], head_run=launched.head_run, leaf=launched.leaf)
            raise HeadLaunchAborted(
                "the reviewer pane was held in a dialog and never took the document nudge",
                handle=launched.handle,
                leaf=launched.leaf,
                workspace=record.workspace,
                pid_file=pid_file_path("review", task["ref"]),
                evidence=evidence,
                head_run=dict(launched.head_run),
            )

        with mock.patch.object(self.host, "start_review", side_effect=blocked_before_send):
            self.tick()

        # The refusal travels on the intent, which is the only thing the next tick can read.
        intent = self.stored_intent()
        self.assertEqual(intent["delivery"]["state"], "blocked")
        self.assertEqual(intent["delivery"]["receipt"], "refused")

        # The next tick: the retained launch is alive, and that is exactly what must not authorise
        # a claim. The reviewer delivery retry is the only thing this may turn into.
        still_blocked = HostError("the reviewer pane is still held in a dialog")
        still_blocked.evidence = {**evidence, "reason": "readiness-blocked"}
        self.host.fail_review_delivery_retry_error = still_blocked
        held = self.tick()

        # The pointer is retried over the exact retained reviewer; what it is never turned into is
        # a claim on the strength of the live pid.
        self.assertEqual(held["action"], "review-launch-delivery-unavailable")
        self.assertEqual(held["readiness"], "blocked")
        record = self.record()
        assert record is not None
        self.assertNotEqual(record.state, "reviewing")
        self.assertNotIn("freeze_worker", self.host.calls)
        self.assertEqual(
            sum(1 for attempt in self.routing_history() if attempt.reviewer is not None),
            0,
            "no routing event attributes a round to a reviewer that never got the document",
        )
        self.assertTrue(self.stored_intent(), "the undelivered launch is never spent")
        self.assertEqual(self.host.reviews, [REF], "and no second reviewer was opened beside it")

        # And the card does not report progress: the tick that follows says the same thing.
        following = self.tick()
        self.assertEqual(following["action"], "review-launch-undelivered")
        self.assertEqual(following["readiness"], "blocked")

    def test_a_worker_whose_pointer_stayed_in_the_composer_is_not_a_successful_claim(self) -> None:
        """`issue:2fdac531`, through the launch path and then through the adoption path.

        Ummanu wrote the TASK pointer into the composer and sent Enter three times in 12
        seconds; Orca answered `accepted` with one byte written each time, the cursor never moved,
        and the prompt stayed in the composer. Recovery then adopted the live HeadRun as
        successfully claimed and cleared the launch intent — 80 minutes to a manual Enter.
        """
        left_in_composer = {
            "subject": "worker-launch",
            "handle": "term:leftover",
            "stage": "payload_written",
            "readiness_after": "ready",
            "send_accepted": True,
            "bytes_written": 1,
            "attempts": 3,
            "turn_confirmed": False,
            "payload_left_in_composer": True,
            # The shape the boundary actually emits since secretary-1542: nothing before the write
            # names a startup-held pane, so `pre_delivery_before` is empty and the state is the
            # POST-write observation of the composer holding the payload.
            "pre_delivery_before": "",
            "pre_delivery_after": "starting",
            "reason": "payload-left-in-composer",
        }
        self.host.fail_prepare_error = HeadLaunchAborted(
            "the launch nudge was not confirmed delivered, and the pane may have taken it anyway",
            handle="term:leftover",
            leaf="leaf:leftover",
            workspace=str(self.data_dir / "workspaces" / f"{REF}-pilot"),
            pid_file=pid_file_path("worker", REF),
            evidence=left_in_composer,
        )

        # The launch path: positive evidence that the pointer is still in the composer is a
        # determinate failure, and it is recorded on the intent rather than lost with the tick.
        self.assertEqual(self.tick()["action"], "worker-launch-aborted")
        intent = self.stored_intent()
        self.assertEqual(intent["delivery"]["receipt"], "refused")
        self.assertEqual(intent["delivery"]["evidence"]["payload_left_in_composer"], True)

        # The adoption path: the head is alive and the pane is writable, and neither is delivery.
        self.host.fail_prepare_error = None
        refused = self.tick()

        self.assertEqual(refused["action"], "worker-launch-undelivered")
        self.assertEqual(refused["status"], "degraded")
        record = self.record()
        assert record is not None
        self.assertNotEqual(record.state, "claimed")
        self.assertEqual(
            sum(1 for attempt in self.routing_history() if attempt.worker is not None),
            0,
            "an undelivered head earns no routing record",
        )
        self.assertTrue(self.stored_intent(), "and the intent is not spent")
        self.assertEqual(self.host.calls.count("prepare_worker"), 1, "no second head beside it")

    def test_the_delivery_refusal_runs_before_any_identity_question_is_asked(self) -> None:
        """The composition secretary-1542 and secretary-1544 rest on, proved rather than asserted.

        `_adopt_launch_intent` asks two independent questions: was this head ever delivered to, and
        which head is it. The first must return before the second is asked, because a head that
        never took its pointer is not this card's head however completely it can be identified.
        The two changes therefore compose by ordering and share no predicate.

        The proof is the identity seam itself: `_remember_head_run` is the first thing adoption
        does with a run, and every later identity write (`handle`, `worker_leaf`, the routing
        event, the state) is downstream of it. It is watched here, and an undelivered intent must
        leave it untouched — not merely leave the record looking unchanged.
        """
        self.host.fail_prepare_error = HeadLaunchAborted(
            "the launch nudge was not confirmed delivered, and the pane may have taken it anyway",
            handle="term:leftover",
            leaf="leaf:leftover",
            workspace=str(self.data_dir / "workspaces" / f"{REF}-pilot"),
            pid_file=pid_file_path("worker", REF),
            evidence={
                "subject": "worker-launch",
                "handle": "term:leftover",
                "stage": "payload_written",
                "readiness_after": "ready",
                "send_accepted": True,
                "bytes_written": 1,
                "attempts": 3,
                "turn_confirmed": False,
                "payload_left_in_composer": True,
                "pre_delivery_before": "",
                "pre_delivery_after": "starting",
                "reason": "payload-left-in-composer",
            },
        )
        self.assertEqual(self.tick()["action"], "worker-launch-aborted")
        self.host.fail_prepare_error = None
        # The intent carries a complete identity: a run id, a pane and a leaf. Nothing about the
        # identity question is unanswerable here, so an ordering that asked it first would answer.
        intent = self.stored_intent()
        self.assertTrue(intent["run_id"])
        self.assertEqual(intent["delivery"]["receipt"], "refused")

        identity_calls: list[tuple[str, str]] = []
        real_remember = dispatcher_launch._remember_head_run

        def watched(record, role, head_run):
            identity_calls.append((role, str((head_run or {}).get("run_id") or "")))
            return real_remember(record, role, head_run)

        with mock.patch.object(dispatcher_launch, "_remember_head_run", side_effect=watched):
            refused = self.tick()

        self.assertEqual(refused["action"], "worker-launch-undelivered")
        self.assertEqual(identity_calls, [], "the refusal returned before adoption asked which head this is")
        record = self.record()
        assert record is not None
        self.assertEqual(record.worker_head_run, {}, "no run was promoted onto the record")
        self.assertNotEqual(record.state, "claimed")
        self.assertTrue(self.stored_intent(), "and the intent survives for the next tick")

    def test_a_head_that_never_accepts_its_pointer_is_replaced_within_a_bounded_window(self) -> None:
        """It never sits indefinitely while the card reports progress.

        The refusal is bounded on purpose: past the ceiling the head is stopped through its own
        intent — stopped first, so nothing is ever opened beside it — and the ordinary path makes
        the launch again.
        """
        self.host.fail_prepare_error = HeadLaunchAborted(
            "the launch nudge was not confirmed delivered",
            handle="term:leftover",
            leaf="leaf:leftover",
            workspace=str(self.data_dir / "workspaces" / f"{REF}-pilot"),
            pid_file=pid_file_path("worker", REF),
            evidence={
                "subject": "worker-launch",
                "stage": "none",
                "readiness_state": "blocked",
                "reason": "unknown-dialog",
                "pre_delivery_before": "unknown-dialog",
            },
        )
        self.tick()
        self.host.fail_prepare_error = None

        actions: list[str] = []
        for _ in range(LAUNCH_DELIVERY_MAX_ATTEMPTS + 1):
            payload = self.runtime.production_state.load()
            delivery = payload["records"][REF].get("launch_intent", {}).get("delivery")
            if delivery:
                delivery["next_at"] = 0.0
                self.runtime.production_state.save(payload)
            actions.append(self.tick()["action"])

        self.assertIn("worker-launch-undelivered", actions)
        self.assertIn("worker-launch-undeliverable", actions)
        self.assertLess(
            actions.index("worker-launch-undelivered"), actions.index("worker-launch-undeliverable")
        )
        stopped = [call for call in self.host.calls if call.startswith(("stop_head", "stop_workspace"))]
        self.assertTrue(stopped, "the head that would not take its pointer was stopped")

    def test_a_crash_between_the_modal_and_the_write_resumes_the_same_delivery(self) -> None:
        """The `issue:6afc6644` crash window, pinned: one transaction, one pointer, one head.

        The tick that answered the modal and then died left a live head with its pointer still
        owed. Recovery re-delivers the *same* immutable pointer, at the same path, over the exact
        run the launch recorded — never a rebuilt one, never a second head — and only that
        confirmed receipt spends the intent.
        """
        self.run_to_validate()
        self.host.calls.clear()
        evidence = {
            "subject": "reviewer-launch",
            "handle": f"review:{REF}",
            "stage": "none",
            "readiness_state": "blocked",
            "reason": "pre-delivery-update-modal",
            "modal_resolution": "answered-skip",
            "modal_answers": 1,
            "pre_delivery_before": "update-modal",
        }

        def modal_before_send(task: dict, record: DispatcherRecord) -> ReviewLaunch:
            self.host.calls.append("start_review")
            self.host.reviews.append(task["ref"])
            launched = self.host._launched(
                f"review:{task['ref']}",
                record.review_head,
                task,
                "reviewer",
                record.workspace,
                run_id=str(record.launch_intent["run_id"]),
            )
            self.host._write_head_pid("review", task["ref"], head_run=launched.head_run, leaf=launched.leaf)
            raise HeadLaunchAborted(
                "the tick did not survive the update modal it answered",
                handle=launched.handle,
                leaf=launched.leaf,
                workspace=record.workspace,
                pid_file=pid_file_path("review", task["ref"]),
                evidence=evidence,
                head_run=dict(launched.head_run),
            )

        with mock.patch.object(self.host, "start_review", side_effect=modal_before_send):
            self.tick()

        launched_run = self.stored_intent()["head_run"]
        payload = self.runtime.production_state.load()
        payload["records"][REF]["launch_intent"]["delivery"]["next_at"] = 0.0
        self.runtime.production_state.save(payload)

        resumed = self.tick()

        self.assertEqual(resumed["action"], "review-launch-adopted")
        self.assertEqual(self.host.calls.count("start_review"), 1, "no second reviewer")
        self.assertEqual(self.host.calls.count("nudge_review_delivery"), 1, "and no double prompt")
        self.assertEqual(self.host.review_delivery_retries, [REF])
        record = self.record()
        assert record is not None
        self.assertEqual(record.review_head_run["run_id"], launched_run["run_id"])
        self.assertTrue(record.review_delivery_evidence["turn_confirmed"])
        self.assertEqual(self.stored_intent(), {}, "only a confirmed receipt spends the intent")

    def test_a_reviewer_whose_worker_will_not_freeze_keeps_its_intent(self) -> None:
        """The reviewer pane is up and the worker would not go: neither head may be forgotten."""
        self.run_to_validate()
        self.host.fail_freeze_worker_reason = "orca refused to close the worker pane"

        outcome = self.tick()

        self.assertEqual(outcome["action"], "review-launch-aborted")
        self.assertEqual(self.reader.show(REF)["state"], "validate", "the card is not blocked")
        self.assertIsNotNone(self.record(), "the record is the only pointer to that reviewer")
        intent = self.stored_intent()
        self.assertEqual((intent["role"], intent["aborted"]), ("review", True))
        self.assertTrue(intent["handle"])
        self.assertEqual(intent["leaf"], f"leaf:review:{REF}")

        # Recovery retries the freeze; while it keeps failing, no second reviewer is started.
        stuck = self.tick()

        self.assertEqual(stuck["action"], "worker-stop-unconfirmed")
        self.assertEqual(self.host.reviews, [REF])

        self.host.fail_freeze_worker_reason = ""
        adopted = self.tick()

        self.assertEqual(adopted["action"], "review-launch-adopted")
        self.assertEqual(self.host.reviews, [REF])
        record = self.record()
        assert record is not None
        self.assertEqual((record.handle, record.worker_pid_file), ("", ""))
        self.assertEqual(record.review_leaf, f"leaf:review:{REF}")
        self.assertFalse(self.head_alive("worker"), "the freeze is what recovery had to finish")

    def test_an_aborted_reviewer_launch_keeps_its_delivery_evidence_and_its_intent(self) -> None:
        """The ambiguous reviewer bring-up: its prompt was refused and its pane will not close.

        Both things are true at once and the card owes both answers. The pane may still hold a
        running reviewer, so the launch intent is kept and no second reviewer is opened — that
        still outranks the ordinary infrastructure retry. And the prompt that never landed is the
        card's only account of a pane nothing may touch again, so the evidence is persisted before
        the intent is written, not after some branch remembers to.
        """
        self.run_to_validate()
        evidence = {
            "subject": "reviewer-launch",
            "handle": f"review:{REF}",
            "stage": "payload_written",
            "payload_bytes": 812,
            "payload_sha256": "0f1e2d3c4b5a6978",
            "reason": "payload-left-in-composer",
        }
        self.host.fail_review_error = HeadLaunchAborted(
            "the head pane never took its launch prompt; head terminal stop failed: orca refused",
            handle=f"review:{REF}",
            leaf=f"leaf:review:{REF}",
            workspace=str(self.data_dir / "workspaces" / f"{REF}-pilot"),
            pid_file=pid_file_path("review", REF),
            evidence=evidence,
        )

        outcome = self.tick()

        # The safety behaviour of this branch is unchanged.
        self.assertEqual(outcome["action"], "review-launch-aborted")
        self.assertEqual(self.reader.show(REF)["state"], "validate", "the card is not blocked")
        intent = self.stored_intent()
        self.assertEqual((intent["role"], intent["aborted"]), ("review", True))
        self.assertEqual(intent["handle"], f"review:{REF}")
        record = self.record()
        assert record is not None
        self.assertEqual(record.review_launch_aborts, 1)
        # And the delivery evidence survived the branch that used to drop it.
        self.assertEqual(record.review_delivery_failures, 1)
        self.assertEqual(record.review_delivery_evidence, evidence)
        self.assertEqual(self.host.reviews, [], "no second reviewer was opened")
        # Exactly once: this tick passed the evidence sink, the abort branch and the outward
        # result, and the count is one for one refused prompt.
        self.assertEqual(record.review_delivery_failures, 1)

    def test_a_busy_reviewer_launch_intent_retries_its_document_before_adoption(self) -> None:
        """A live heartbeat after a pre-send timeout is not reviewer launch confirmation.

        This drives the persisted intent through the next dispatcher tick.  The first launch has
        already created the reviewer pane and written its heartbeat, but its production-shaped
        `tui-idle` timeout happened before a terminal send.  Until the durable retry confirms the
        document nudge, recovery must neither freeze the worker nor attribute/clear the reviewer.
        """
        self.run_to_validate()
        self.host.calls.clear()
        evidence = {
            "subject": "reviewer-launch",
            "handle": f"review:{REF}",
            "stage": "none",
            "readiness_before": "busy",
            "readiness_state": "busy",
            "reason": "readiness-busy",
            "transport_error": (
                'orca terminal wait --for tui-idle failed: {"error":{"code":"timeout","message":"timeout"}}'
            ),
        }

        def busy_before_send(task: dict, record: DispatcherRecord) -> ReviewLaunch:
            self.host.calls.append("start_review")
            self.host.reviews.append(task["ref"])
            launched = self.host._launched(
                f"review:{task['ref']}",
                record.review_head,
                task,
                "reviewer",
                record.workspace,
                run_id=str(record.launch_intent["run_id"]),
            )
            self.host._write_head_pid("review", task["ref"], head_run=launched.head_run, leaf=launched.leaf)
            raise HeadLaunchAborted(
                "reviewer document nudge met a busy pane before send",
                handle=launched.handle,
                leaf=launched.leaf,
                workspace=record.workspace,
                pid_file=pid_file_path("review", task["ref"]),
                evidence=evidence,
                head_run=dict(launched.head_run),
            )

        with mock.patch.object(self.host, "start_review", side_effect=busy_before_send):
            held = self.tick()

        self.assertEqual(held["action"], "review-launch-busy")
        intent = self.stored_intent()
        self.assertEqual((intent["role"], intent["delivery"]["state"]), ("review", "busy"))
        self.assertEqual(intent["delivery"]["evidence"], evidence)
        self.assertGreater(intent["delivery"]["next_at"], time.time())
        preserved = {
            key: intent[key] for key in ("workspace", "handle", "leaf", "pid_file", "run_id", "head_run")
        }
        record = self.record()
        assert record is not None
        self.assertEqual(record.review_delivery_evidence, evidence)
        self.assertEqual(record.review_delivery_failures, 0, "busy is not a delivery failure")
        self.assertEqual(record.review_launch_aborts, 0, "busy is not a launch-abort episode")
        self.assertNotIn("freeze_worker", self.host.calls)
        self.assertNotIn("stop_head:worker", self.host.calls)
        self.assertEqual(self.host.reviews, [REF], "the existing reviewer is never replaced")

        waiting = self.tick()

        self.assertEqual(waiting["action"], "review-launch-busy")
        self.assertNotIn("nudge_review_delivery", self.host.calls)
        self.assertNotIn("freeze_worker", self.host.calls)
        self.assertEqual(self.stored_intent()["head_run"], preserved["head_run"])

        payload = self.runtime.production_state.load()
        payload["records"][REF]["launch_intent"]["delivery"]["next_at"] = 0.0
        self.runtime.production_state.save(payload)
        review_routes_before = sum(1 for attempt in self.routing_history() if attempt.reviewer is not None)

        confirmed = self.tick()

        self.assertEqual(confirmed["action"], "review-launch-adopted")
        self.assertEqual(self.host.calls.count("start_review"), 1)
        self.assertEqual(self.host.calls.count("nudge_review_delivery"), 1)
        self.assertEqual(self.host.calls.count("freeze_worker"), 1)
        self.assertEqual(self.host.reviews, [REF], "retry uses the same reviewer run")
        record = self.record()
        assert record is not None
        self.assertEqual(record.state, "reviewing")
        self.assertEqual(record.review_head_run["run_id"], preserved["head_run"]["run_id"])
        self.assertTrue(record.review_delivery_evidence["turn_confirmed"])
        self.assertEqual(self.stored_intent(), {}, "only confirmed delivery spends the intent")
        self.assertEqual(
            sum(1 for attempt in self.routing_history() if attempt.reviewer is not None),
            review_routes_before + 1,
        )

        self.assertEqual(self.tick()["action"], "waiting-review-verdict")
        self.assertEqual(self.host.calls.count("nudge_review_delivery"), 1)
        self.assertEqual(self.host.calls.count("freeze_worker"), 1)

    def test_busy_retry_does_not_adopt_when_payload_remains_in_composer(self) -> None:
        """A started turn is not a document receipt when the composer still holds the payload."""
        self.run_to_validate()
        evidence = {
            "subject": "reviewer-launch",
            "handle": f"review:{REF}",
            "stage": "none",
            "readiness_before": "busy",
            "readiness_state": "busy",
            "reason": "readiness-busy",
        }

        def busy_before_send(task: dict, record: DispatcherRecord) -> ReviewLaunch:
            launched = self.host._launched(
                f"review:{task['ref']}",
                record.review_head,
                task,
                "reviewer",
                record.workspace,
                run_id=str(record.launch_intent["run_id"]),
            )
            self.host._write_head_pid("review", task["ref"], head_run=launched.head_run, leaf=launched.leaf)
            raise HeadLaunchAborted(
                "reviewer document nudge met a busy pane before send",
                handle=launched.handle,
                leaf=launched.leaf,
                workspace=record.workspace,
                pid_file=pid_file_path("review", task["ref"]),
                evidence=evidence,
                head_run=dict(launched.head_run),
            )

        with mock.patch.object(self.host, "start_review", side_effect=busy_before_send):
            self.assertEqual(self.tick()["action"], "review-launch-busy")

        payload = self.runtime.production_state.load()
        payload["records"][REF]["launch_intent"]["delivery"]["next_at"] = 0.0
        self.runtime.production_state.save(payload)
        self.host.review_delivery_retry_evidence = {
            "subject": "reviewer-launch",
            "stage": "acknowledged",
            "turn_confirmed": True,
            "cursor_moved": True,
            "payload_left_in_composer": True,
            "readiness_state": "busy",
        }

        held = self.tick()

        # The pointer was looked for and found in the composer, so the retained delivery is named
        # for what it is rather than for a pane that was busy: this is a determinate refusal.
        self.assertEqual(held["action"], "review-launch-undelivered")
        record = self.record()
        assert record is not None
        self.assertEqual(record.state, "review_starting")
        self.assertEqual(record.launch_intent["delivery"]["state"], "refused")
        self.assertTrue(record.review_delivery_evidence["payload_left_in_composer"])
        self.assertNotIn("freeze_worker", self.host.calls)
        self.assertTrue(self.stored_intent(), "the exact reviewer launch stays recoverable")

    def test_a_failed_review_delivery_counts_once_on_the_ordinary_path(self) -> None:
        """A failure with delivery evidence reaches the recorder once."""
        self.run_to_validate()
        evidence = {
            "subject": "reviewer-launch",
            "handle": f"review:{REF}",
            "stage": "payload_written",
            "payload_bytes": 812,
            "payload_sha256": "0f1e2d3c4b5a6978",
            "reason": "pane-stayed-ready",
        }
        error = HostError("review launch prompt was refused")
        error.evidence = evidence
        self.host.fail_review_error = error

        outcome = self.tick()

        self.assertNotEqual(outcome.get("action"), "review-launch-aborted")
        record = self.record()
        assert record is not None
        self.assertEqual(record.review_delivery_failures, 1)
        self.assertEqual(record.review_delivery_evidence, evidence)

    def test_an_unevidenced_reviewer_bring_up_failure_is_not_a_delivery_failure(self) -> None:
        """A split that would not open is an infrastructure failure and has its own counter.

        The single evidence sink runs for every reviewer-launch exception, so this is where it has
        to stay quiet: counting a bring-up that never reached a prompt as a refused delivery would
        make the card's delivery telemetry say the opposite of what happened.
        """
        self.run_to_validate()
        self.host.fail_review_error = HostError("orca split failed: no pane could be opened")

        self.tick()

        record = self.record()
        assert record is not None
        self.assertEqual(record.review_delivery_failures, 0)
        self.assertEqual(record.review_delivery_evidence, {})

    def test_a_later_reviewer_launch_abort_keeps_a_confirmed_prompt_receipt(self) -> None:
        """Freezing the worker can fail after the reviewer has already accepted its prompt."""
        self.run_to_validate()
        evidence = {
            "subject": "reviewer-launch",
            "handle": f"review:{REF}",
            "stage": "acknowledged",
            "transport_version": "agent-prompt-v2",
            "body_write_accepted": True,
            "submit_write_accepted": True,
            "submit_count": 1,
            "turn_confirmed": True,
        }
        self.host.review_launch_delivery_evidence = evidence
        self.host.fail_freeze_worker_reason = "worker did not stop"

        outcome = self.tick()

        self.assertEqual(outcome["action"], "review-launch-aborted")
        record = self.record()
        assert record is not None
        self.assertEqual(record.review_delivery_evidence, evidence)
        self.assertEqual(record.review_delivery_failures, 0)

    def _abort_review_into_recovery(self) -> None:
        """Leave the card in `review_starting` with its worker retained and its reviewer dead.

        The first review tick brings a reviewer pane up but cannot confirm the worker, so it aborts
        and stores the intent. Killing that reviewer's heartbeat and ageing the intent past its
        grace window is what the incident's unstable reviewer did on its own: every later tick then
        re-enters `start_review` from `review_starting` instead of adopting a live pane.
        """
        self.run_to_validate()
        self.host.fail_freeze_worker_reason = "orca refused to close the worker pane"
        aborted = self.tick()
        self.assertEqual(aborted["action"], "review-launch-aborted")
        self.host.fail_freeze_worker_reason = ""
        Path(pid_file_path("review", REF)).unlink(missing_ok=True)
        self.age_intent(initial_output_stall_seconds() + 60)
        self.host.review_status_result = {"known": True, "live": False, "reason": "missing-terminal"}

    def test_a_reviewer_whose_worker_vanished_launches_review_instead_of_looping(self) -> None:
        """A retained worker that is provably gone leaves nothing to freeze: review goes ahead.

        This is issue:aa9a8ae4. The worker session disappeared while the card waited, so every
        recovery tick used to re-enter `start_review`, fail to confirm a suspension that no longer
        existed, and abort — 113 identical `review-launch-aborted` ticks with no escalation. With
        the vanished session recognised, the reviewer takes the commit the worker left instead.
        """
        self._abort_review_into_recovery()
        # The retained worker's process is now provably gone, not merely unconfirmable.
        self.host.retained_worker_alive = False
        self.host.worker_retained_gone = True

        restarted = self.tick()

        self.assertNotEqual(restarted["action"], "review-launch-aborted", "a vanished worker must not loop")
        self.assertEqual(self.host.reviews, [REF, REF], "the reviewer is relaunched, not aborted")
        self.assertEqual(self.reader.show(REF)["state"], "validate", "the card is not blocked")
        record = self.record()
        assert record is not None
        self.assertEqual(record.state, "reviewing", "the reviewer took the checkout")

    def test_a_red_verdict_after_a_vanished_worker_review_opens_a_replacement(self) -> None:
        """The round kept naming a gone worker; a red verdict resumes nothing and replaces it.

        The vanished worker launched review over the commit it left, but its record still carries
        `retained` and a dead heartbeat. A red verdict must not try to resume that conversation —
        there is none — and must not strand the card either: it opens a fresh worker for round 2.
        """
        self._abort_review_into_recovery()
        self.host.retained_worker_alive = False
        self.host.worker_retained_gone = True
        self.assertEqual(self.tick()["action"], "review-restarted")  # review over the gone worker

        self.host.review_status_result = {"known": True, "live": True, "reason": "live"}
        self.verdict("red", "needs work", "verdict-red-vanished")
        self.tick()  # the verdict parks the card in Assessment
        self.decide("rework")

        outcome = self.tick()

        self.assertEqual(outcome["action"], "rework-started")
        self.assertEqual(self.host.calls.count("resume_worker"), 0, "a gone session is not resumed")
        self.assertEqual(self.host.calls.count("restart_worker"), 1, "a replacement opens instead")
        record = self.record()
        assert record is not None
        self.assertEqual(record.attempt_round, 2)

    def test_a_reviewer_whose_worker_is_only_unconfirmable_still_aborts(self) -> None:
        """The fix is for a *proven* death, never an ambiguous heartbeat.

        A worker that cannot be confirmed suspended but is not confirmably gone either — a pid file
        that was never written, a raw command override — is still a possible second writer, so the
        launch stays on the cautious abort path rather than judging a checkout a live worker may be
        editing.
        """
        self._abort_review_into_recovery()
        # Unconfirmable, but not provably gone: worker_retained_vanished stays False.
        self.host.retained_worker_alive = False
        self.host.worker_retained_gone = False

        stuck = self.tick()

        self.assertEqual(stuck["action"], "review-launch-aborted", "an unproven death is not safe")
        self.assertIsNotNone(self.record(), "the record still points at that launch")

    def test_a_reviewer_launch_stuck_past_the_ceiling_escalates_to_the_operator_once(self) -> None:
        """A launch that keeps aborting past the ceiling leaves one operator note, not a per-tick one."""
        self.run_to_validate()
        self.host.fail_freeze_worker_reason = "orca refused to close the worker pane"
        with mock.patch.dict(os.environ, {"UMMANU_REVIEW_LAUNCH_ABORT_STUCK": "3"}):
            aborts = self.tick()  # first abort, count 1
            self.assertEqual(aborts["action"], "review-launch-aborted")
            for _ in range(4):
                Path(pid_file_path("review", REF)).unlink(missing_ok=True)
                self.age_intent(initial_output_stall_seconds() + 60)
                self.host.review_status_result = {"known": True, "live": False, "reason": "missing-terminal"}
                self.assertEqual(self.tick()["action"], "review-launch-aborted")

        notes = [
            comment
            for comment in self.reader.show(REF)["comments"]
            if "Reviewer launch has aborted" in comment["body"]
        ]
        self.assertEqual(len(notes), 1, "one operator note for the whole stuck episode")
        record = self.record()
        assert record is not None
        self.assertGreaterEqual(record.review_launch_aborts, 3)

    def test_a_reviewer_launch_below_the_ceiling_does_not_escalate(self) -> None:
        """Below the ceiling the abort is still just a degraded tick the steward already carries."""
        self.run_to_validate()
        self.host.fail_freeze_worker_reason = "orca refused to close the worker pane"
        with mock.patch.dict(os.environ, {"UMMANU_REVIEW_LAUNCH_ABORT_STUCK": "5"}):
            self.assertEqual(self.tick()["action"], "review-launch-aborted")

        notes = [
            comment
            for comment in self.reader.show(REF)["comments"]
            if "Reviewer launch has aborted" in comment["body"]
        ]
        self.assertEqual(notes, [], "no operator note for a single abort")

    # A launch takes its leaf from Orca's create/split reply, not a second inventory lookup. -----

    def legacy_pane_lookup_is_unavailable(self):
        """The obsolete handle-to-leaf inventory lookup must not be part of a launch.

        `pane_leaf` models the removed pre-1168 seam. Keeping it unavailable makes the test prove
        that a launch records the leaf handed back by its host result rather than querying Orca
        again by the create-time handle, which may never appear in inventory.
        """
        return mock.patch.object(
            self.host, "pane_leaf", mock.Mock(side_effect=HostError("orca terminal list failed"))
        )

    def test_a_claim_records_its_returned_leaf_without_an_inventory_lookup(self) -> None:
        with self.legacy_pane_lookup_is_unavailable():
            outcome = self.tick()

        self.assertEqual(outcome["step"], "claim")
        self.assertEqual(self.host.prepared, [REF])
        record = self.record()
        assert record is not None
        self.assertEqual(record.worker_leaf, f"leaf:{record.handle}")
        self.assertEqual(self.stored_intent(), {})

    def test_a_rework_records_its_returned_leaf_without_an_inventory_lookup(self) -> None:
        self.rework_after_red_review()

        with self.legacy_pane_lookup_is_unavailable():
            outcome = self.tick()

        self.assertEqual(outcome["action"], "rework-started")
        self.assertEqual(self.host.calls.count("restart_worker"), 1)
        self.assertEqual(self.reader.show(REF)["state"], "in_progress")
        record = self.record()
        assert record is not None
        self.assertEqual((record.state, record.attempt_round), ("claimed", 2))
        self.assertEqual(record.worker_leaf, f"leaf:{record.handle}")

    def test_a_respawn_records_its_returned_leaf_without_an_inventory_lookup(self) -> None:
        self.tick()
        self.kill_worker_heartbeat()
        self.host.worker_status_result = {"known": True, "live": False, "reason": "missing-terminal"}

        with self.legacy_pane_lookup_is_unavailable():
            outcome = self.tick()

        self.assertEqual(outcome["action"], "worker-respawned")
        self.assertEqual(self.host.calls.count("restart_worker"), 1)
        record = self.record()
        assert record is not None
        self.assertTrue(self.head_alive("worker"))
        self.assertEqual(record.worker_leaf, f"leaf:{record.handle}")

    def test_a_gate_red_rework_records_its_returned_leaf_without_an_inventory_lookup(self) -> None:
        self.run_to_validate()
        self.host.gate_results = [GateResult("red", "tests failed", log="boom")]

        with self.legacy_pane_lookup_is_unavailable():
            outcome = self.tick()

        self.assertEqual(outcome["action"], "gate-red-rework")
        self.assertEqual(self.host.calls.count("restart_worker"), 1)
        record = self.record()
        assert record is not None
        self.assertEqual(record.worker_leaf, f"leaf:{record.handle}")

    def test_a_review_bring_up_that_fails_over_its_own_heartbeat_keeps_its_intent(self) -> None:
        """An ordinary failure is only believed while the heartbeat agrees with it.

        A reviewer bring-up that got as far as writing a heartbeat left a process behind, whatever
        the host called the failure. Blocking the card and dropping the record there strands it.
        """
        self.run_to_validate()

        def failing_review(task: dict, record: Any):
            self.host.calls.append("start_review")
            self.host.reviews.append(task["ref"])
            self.host._write_head_pid(
                "review",
                task["ref"],
                run_id=str((record.launch_intent or {}).get("run_id") or ""),
            )
            raise HostError("orca terminal rename failed")

        with mock.patch.object(self.host, "start_review", failing_review):
            outcome = self.tick()

        self.assertEqual(outcome["action"], "review-launch-aborted")
        self.assertEqual(self.reader.show(REF)["state"], "validate", "the card is not blocked")
        self.assertIsNotNone(self.record(), "the record is the only pointer to that reviewer")
        self.assertEqual(self.stored_intent()["role"], "review")
        self.assertTrue(self.head_alive("review"))

        adopted = self.tick()

        self.assertEqual(adopted["action"], "review-launch-adopted")
        self.assertEqual(self.host.reviews, [REF], "no second reviewer beside the live one")

    def test_a_bring_up_that_could_not_hold_its_workspace_leaves_no_intent(self) -> None:
        """The intent names a workspace before the host answers, so the host must land on it.

        A worktree placed somewhere else is refused by `GitWorkspaceManager.create` and reaches the tick as
        an ordinary bring-up failure: nothing is running, so nothing may be adopted against a path
        that would send every later review, stop and teardown to the wrong checkout.
        """
        self.host.fail_prepare_reason = "orca placed the worker workspace at /elsewhere, not /intended"

        outcome = self.tick()

        self.assertEqual(outcome["status"], "blocked")
        self.assertEqual(self.stored_intent(), {})
        self.assertIsNone(self.record())

        self.host.fail_prepare_reason = ""
        self.host.calls.clear()

        # And the retry after the operator requeues it launches exactly one head, on the path the
        # fresh intent names.
        self.writer.move(
            role="po",
            actor="operator",
            reference=REF,
            target="ready",
            reason="requeued",
            request_id="requeue-after-workspace-mismatch",
            sprint_override=True,
            sprint_override_reason="the operator moves a card of a reserved project by hand",
        )
        recovered = self.tick()

        self.assertEqual(recovered["step"], "claim")
        self.assertEqual(self.host.prepared, [REF])
        record = self.record()
        assert record is not None
        self.assertEqual(record.workspace, self.host.restore_workspace({}, f"{REF}-pilot"))

    # a stop the host would not confirm ---------------------------------------

    def test_a_worker_respawn_after_an_unconfirmed_stop_starts_nothing(self) -> None:
        self.tick()
        self.kill_worker_heartbeat()
        self.host.worker_status_result = {"known": True, "live": False, "reason": "missing-terminal"}
        self.host.fail_stop_head_reason = "orca terminal stop failed"

        outcome = self.tick()

        self.assertEqual(outcome["action"], "worker-stop-unconfirmed")
        self.assertEqual(outcome["status"], "degraded")
        self.assertEqual(self.host.calls.count("restart_worker"), 0)
        self.assertEqual(self.stored_intent(), {}, "no launch was even fixed on disk")

        # Once the stop goes through, the respawn happens exactly once.
        self.host.fail_stop_head_reason = ""
        respawned = self.tick()

        self.assertEqual(respawned["action"], "worker-respawned")
        self.assertEqual(self.host.calls.count("restart_worker"), 1)

    def test_a_stop_its_supervisor_will_not_confirm_keeps_the_record(self) -> None:
        """A refused stop is not evidence the head is gone, whatever its heartbeat says."""
        self.tick()
        Path(pid_file_path("worker", REF)).unlink()
        self.kill_worker_heartbeat()
        self.host.worker_status_result = {"known": True, "live": False, "reason": "missing-terminal"}
        real_host = CommandHostRuntime(self.catalog, self.data_dir, mode="real", audit=card_audit(self))  # type: ignore[arg-type]
        SupervisedBackend().install(real_host).stop_refusal = "the supervisor could not be reached"

        with mock.patch.object(self.host, "stop_head", real_host.stop_head):
            outcome = self.tick()

        self.assertEqual(outcome["action"], "worker-stop-unconfirmed")
        self.assertEqual(self.host.calls.count("restart_worker"), 0)
        record = self.record()
        assert record is not None
        self.assertTrue(record.worker_leaf, "the unconfirmed stop must retain the named head")

    def test_a_reviewer_respawn_after_an_unconfirmed_stop_starts_nothing(self) -> None:
        self.run_to_validate()
        self.tick()  # reviewer up
        self.kill_review_heartbeat()
        self.host.review_status_result = {"known": True, "live": False, "reason": "missing-terminal"}
        self.host.fail_stop_review_reason = "orca refused to close the reviewer pane"

        outcome = self.tick()

        self.assertEqual(outcome["action"], "review-stop-unconfirmed")
        self.assertEqual(self.host.reviews, [REF], "no reviewer opens beside one that may be live")

        self.host.fail_stop_review_reason = ""
        respawned = self.tick()

        self.assertEqual(respawned["action"], "review-respawned")
        self.assertEqual(self.host.reviews, [REF, REF])

    def test_a_red_verdict_whose_reviewer_will_not_stop_leaves_the_card_in_validate(self) -> None:
        self.run_to_validate()
        self.tick()
        self.verdict("red", "needs work", "verdict-red")
        self.host.fail_stop_review_reason = "orca refused to close the reviewer pane"

        outcome = self.tick()

        self.assertEqual(outcome["action"], "review-stop-unconfirmed")
        self.assertEqual(self.reader.show(REF)["state"], "validate", "the bounce did not happen")
        self.assertEqual(self.host.calls.count("restart_worker"), 0)

        self.host.fail_stop_review_reason = ""
        self.assertEqual(self.tick()["to"], "assessment")
        self.decide("rework")
        rework = self.tick()

        self.assertEqual(rework["action"], "rework-started")
        self.assertEqual(self.host.calls.count("restart_worker"), 1)

    # liveness ---------------------------------------------------------------

    def test_a_heartbeat_that_never_appears_reads_as_dead_only_past_the_window(self) -> None:
        intent = {"pid_file": str(self.data_dir / "nothing.pid"), "at": 1000.0}

        inside = launch_intent_liveness(intent, now=1000.0 + initial_output_stall_seconds() - 1)
        outside = launch_intent_liveness(intent, now=1000.0 + initial_output_stall_seconds() + 1)

        self.assertEqual((inside["alive"], inside["pid_known"]), (True, False))
        self.assertEqual((outside["alive"], outside["pid_known"]), (False, False))

    def test_recovery_binds_an_empty_leaf_from_the_exact_launch_intent(self) -> None:
        pid_file = self.data_dir / "busy-review.pid"
        identity = heartbeat_identity(run_id="review-run", role="reviewer", task=f"card:{REF}")
        wrapped = with_pid_heartbeat(
            "python3 -c 'import time; time.sleep(5)'",
            str(pid_file),
            identity=identity,
        )
        proc = subprocess.Popen(["/bin/sh", "-lc", wrapped])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.terminate)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not pid_file.exists():
            time.sleep(0.01)
        intent = {
            "pid_file": str(pid_file),
            "at": time.time(),
            "run_id": "review-run",
            "role": "reviewer",
            "task": f"card:{REF}",
            "leaf": "leaf-review",
        }

        liveness = launch_intent_liveness(intent)

        self.assertEqual(liveness, {"alive": True, "pid_known": True})
        self.assertEqual(json.loads(pid_file.read_text(encoding="utf-8"))["leaf"], "leaf-review")

    def test_recovery_does_not_overwrite_a_foreign_nonempty_leaf(self) -> None:
        pid_file = self.data_dir / "foreign-review.pid"
        identity = heartbeat_identity(
            run_id="review-run", role="reviewer", task=f"card:{REF}", leaf="foreign-leaf"
        )
        wrapped = with_pid_heartbeat(
            "python3 -c 'import time; time.sleep(5)'",
            str(pid_file),
            identity=identity,
        )
        proc = subprocess.Popen(["/bin/sh", "-lc", wrapped])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.terminate)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not pid_file.exists():
            time.sleep(0.01)
        intent = {
            "pid_file": str(pid_file),
            "at": time.time(),
            "run_id": "review-run",
            "role": "reviewer",
            "task": f"card:{REF}",
            "leaf": "expected-leaf",
        }

        liveness = launch_intent_liveness(intent)

        self.assertEqual(liveness, {"alive": False, "pid_known": True, "identity_mismatch": True})
        self.assertEqual(json.loads(pid_file.read_text(encoding="utf-8"))["leaf"], "foreign-leaf")


class WorkerWorkspaceBindingTests(unittest.TestCase):
    """Which project a worker checkout may be placed for at all (1066).

    The checkout itself is always the git worktree `GitWorkspaceManager` cuts
    (`tests.test_git_workspace_manager`); what is held here is the refusal before it: a project that
    is unknown, disabled or unavailable reaches no workspace and no head.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.data_dir = Path(self.tmpdir.name)
        self.repo = self.data_dir / "projects" / "codegen_orchestrator"
        self.repo.mkdir(parents=True)
        (self.repo / ".git").mkdir()
        self.host = CommandHostRuntime(  # type: ignore[arg-type]
            self.catalog(),
            self.data_dir,
            mode="real",
            audit=card_audit(self),
            production_runtime=registered_production_runtime(self.data_dir),
        )
        self.host._prepare_workspace_environment = lambda *args, **kwargs: None  # type: ignore[method-assign]
        # This launch-intent fixture deliberately creates no workspace Python environment.
        self.host._install_workspace_test_guards = lambda *args, **kwargs: None  # type: ignore[method-assign]
        self.host._require_workspace_environment = lambda workspace: None  # type: ignore[method-assign]

    def catalog(self):
        test = self

        class Catalog:
            instance_dir = Path("/nonexistent-instance")

            def binding(self, project: str) -> dict[str, Any]:
                return {"repo": str(test.repo)}

            def project_default_branch(self, project: str) -> str:
                return "main"

            def integration_base(self, project: str, override: str | None) -> str:
                return resolve_integration_base(default_branch="main", declared=None, override=override)

            def workspace_seed(self, project: str, task: dict) -> str:
                workspace = task.get("workspace") or {}
                seed = str(workspace.get("seed_ref") or "")
                return seed or self.integration_base(project, workspace.get("base_branch"))

            def broad_check_verdict(self, project: str) -> ContractVerdict:
                # The worker task packet resolves this to print an exact broad-check command
                # (issue:8b39e60e4df361c6138e). Ummanu's adapter declares one, and an adapter
                # that declares none is refused by name now, so a real catalog answers a declared
                # contract here and this stub answers the same shape.
                return ContractVerdict.as_fit(
                    ModuleContract(sys.executable, "ummanu", module="tests.broad"),
                    "ummanu",
                )

            def adapter(self, project: str) -> dict[str, Any]:
                return {}

        return Catalog()

    def test_unavailable_project_is_rejected_before_any_workspace_or_head_activation(self) -> None:
        (self.repo / ".git").rmdir()
        task = {"ref": "ummanu-1", "project": "codegen-orchestrator", "workspace": {}}
        record = SimpleNamespace(workspace=str(self.data_dir / "workspaces" / "existing"), review_head="reviewer")

        with (
            mock.patch.object(self.host, "_run") as run,
            mock.patch.object(self.host, "_launch") as launch,
            mock.patch.object(GitWorkspaceManager, "create") as create,
        ):
            with self.assertRaisesRegex(HostError, "project repo.*unavailable"):
                self.host.prepare_worker(task, "card-1", "worker")
            with self.assertRaisesRegex(HostError, "project repo.*unavailable"):
                self.host.start_review(task, record)

        run.assert_not_called()
        launch.assert_not_called()
        create.assert_not_called()

    def test_project_consumers_distinguish_unknown_disabled_and_unavailable_bindings(self) -> None:
        catalog = object.__new__(InstanceCatalog)
        catalog.registered_bindings = {
            "inventory-only": {
                "id": "inventory-only",
                "repo": str(self.data_dir / "inventory"),
                "enabled": False,
            },
            "unavailable": {
                "id": "unavailable",
                "repo": str(self.data_dir / "missing"),
                "enabled": True,
            },
        }
        catalog.bindings = {"unavailable": catalog.registered_bindings["unavailable"]}
        self.host.catalog = catalog  # type: ignore[assignment]

        with (
            mock.patch.object(self.host, "_run") as run,
            mock.patch.object(self.host, "_launch") as launch,
        ):
            for project, reason in (
                ("unknown", "not registered"),
                ("inventory-only", "registered but not enabled"),
                ("unavailable", "repo.*unavailable"),
            ):
                with self.subTest(project=project), self.assertRaisesRegex(HostError, reason):
                    self.host.prepare_worker(
                        {"ref": "ummanu-1", "project": project, "workspace": {}},
                        "card-1",
                        "worker",
                    )

        run.assert_not_called()
        launch.assert_not_called()

    def test_instance_catalog_uses_filesystem_truth_for_project_availability(self) -> None:
        catalog = object.__new__(InstanceCatalog)
        catalog.bindings = {"codegen-orchestrator": {"id": "codegen-orchestrator", "repo": str(self.repo)}}

        self.assertTrue(catalog.project_availability("codegen-orchestrator").allows("codegen-orchestrator"))
        (self.repo / ".git").rmdir()
        self.assertFalse(catalog.project_availability("codegen-orchestrator").allows("codegen-orchestrator"))


class HostLaunchContourTests(unittest.TestCase):
    """The host half of the contour: what a bring-up promises, and what a stop confirms.

    Everything above this reads the host's answers. These read the answers themselves, because the
    ambiguous outcome only exists down here: a failure raised after the head was started.

    The head's backend is `_SupervisedBackend`, a stand-in for `local-pty` that records what it
    was asked; what is asserted is what this host does around it.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.data_dir = Path(self.tmpdir.name)
        self.host = CommandHostRuntime(  # type: ignore[arg-type]
            FakeCatalog(),
            self.data_dir,
            mode="real",
            audit=card_audit(self),
            production_runtime=registered_production_runtime(self.data_dir),
        )
        self.host._require_workspace_environment = lambda workspace: None  # type: ignore[method-assign]
        self.host.preflight_codex_run = _transport_only_preflight  # type: ignore[method-assign]
        self.backend = _SupervisedBackend()
        self.host._head_runtimes[LOCAL_PTY_RUNTIME] = self.backend

    @staticmethod
    def _reap_head(head: subprocess.Popen) -> None:
        """Kill a stand-in head and collect its exit status.

        `kill` alone leaves a zombie until the process ends, and `Popen.__del__` then reports
        `ResourceWarning: subprocess is still running` (issue:3a06b695f4dc731da91a)."""
        with contextlib.suppress(ProcessLookupError):
            head.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            head.wait(timeout=5)

    def pid_file(self, contents: str | None) -> str:
        path = self.data_dir / "head.pid"
        if contents is not None:
            self._write_test_heartbeat(path, int(contents))
        return str(path)

    @staticmethod
    def _write_test_heartbeat(path: Path, pid: int, *, role: str = "worker") -> None:
        identity = {
            "version": 1,
            "pid": pid,
            "run_id": "host-test-run",
            "role": role,
            "task": f"card:{REF}",
            "leaf": "",
        }
        if pid > 0 and Path(f"/proc/{pid}/stat").exists():
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            identity["boot_id"] = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
            identity["proc_starttime_ticks"] = stat[stat.rfind(")") + 2 :].split()[19]
        else:
            identity["boot_id"] = "dead-process"
            identity["proc_starttime_ticks"] = "0"
        path.write_text(json.dumps(identity), encoding="utf-8")

    def track_worker(self, record: DispatcherRecord) -> DispatcherRecord:
        """Bind a controlled process to the same durable HeadRun the runtime will read."""
        run = head_ops.HeadRun(
            run_id="host-test-run",
            spec=head_ops.HeadSpec(profile_id=record.head or "codex", adapter="codex", runtime=LOCAL_PTY_RUNTIME),
            workspace=record.workspace,
            task_ref=head_ops.TaskRef.card(REF),
            handle=record.handle,
            leaf=record.worker_leaf,
            pid_file=record.worker_pid_file,
        )
        record.worker_head_run = run.to_json()
        raw = json.loads(Path(record.worker_pid_file).read_text(encoding="utf-8"))
        raw.update(
            run_heartbeat_identity(
                record.worker_head_run,
                role="worker",
                task=f"card:{REF}",
                leaf=record.worker_leaf,
            )
        )
        Path(record.worker_pid_file).write_text(json.dumps(raw), encoding="utf-8")
        return record

    def codex_worker_for_delivery(self) -> tuple[subprocess.Popen, DispatcherRecord, head_ops.HeadRun]:
        head = subprocess.Popen(["sleep", "30"])
        self.addCleanup(self._reap_head, head)
        record = DispatcherRecord(
            worker="w1", workspace=str(self.data_dir), handle="term:worker", head="codex",
            review_head="codex-reviewer", attempt_id="a1", comment_baseline=0,
            review_baseline=0, state="claimed", claimed_at=0.0,
            worker_pid_file=self.pid_file(str(head.pid)),
            worker_run={"adapter": "codex", "codex_mode": "tui", "head": "codex"},
            worker_continuation=WorkerContinuation(
                stage=WorkerContinuationStage.DELIVERY_PENDING, phase="gate",
                retained_at=time.time(), sent_at=time.time(),
            ),
        )
        self.track_worker(record)
        run = head_ops.HeadRun(
            run_id="host-test-run",
            spec=head_ops.HeadSpec(
                profile_id="codex", adapter="codex", model="gpt-5.6-terra", runtime=LOCAL_PTY_RUNTIME
            ),
            workspace=record.workspace, task_ref=head_ops.TaskRef.card(REF), role="worker",
            handle=record.handle, pid_file=record.worker_pid_file,
        )
        attested = accepted_transport_run(
            "codex", role="worker", workspace=record.workspace, task_ref=run.task_ref,
            pid_file=record.worker_pid_file, run_id=run.run_id,
        )
        sessions = self.data_dir / "sessions"
        sessions.mkdir(exist_ok=True)
        (sessions / "rollout.jsonl").write_text(
            json.dumps({"type": "session_meta", "payload": {"session_id": "s-1", "cwd": str(self.data_dir.resolve())}})
            + "\n" + json.dumps({"type": "thread.started", "thread_id": "parent-1"}) + "\n",
            encoding="utf-8",
        )
        run = run.with_fanout_policy({**attested.fanout_policy, "provider_source": {
            "version": 1, "kind": "codex_session_event_jsonl", "state": "unbound",
            **codex_provider_source_descriptor(run), "root": str(sessions), "baseline": [],
        }})
        record.worker_head_run = run.to_json()
        return head, record, run

    def test_codex_worker_continuations_and_report_bind_before_delivery(self) -> None:
        for stopped, report in ((True, False), (False, False), (False, True)):
            with self.subTest(stopped=stopped, report=report):
                head, record, run = self.codex_worker_for_delivery()
                events: list[str] = []
                self.host.configure_codex_provider_ingress(
                    run, persist=lambda updated, target=record: setattr(target, "worker_head_run", updated.to_json()),
                    stop=lambda *_: None, block=lambda *_: None,
                )
                ingress = self.host._codex_provider_ingresses[run.run_id]
                bind = ingress.bind_before_delivery

                def record_bind(log: list[str] = events, hook: Any = bind) -> head_ops.HeadRun:
                    log.append("bind")
                    return hook()

                self.backend.on_deliver = lambda *_, log=events: log.append("send")
                real_signal = self.host._signal_head

                def signal_then_record(*args: Any, log: list[str] = events, signal_hook: Any = real_signal, **kwargs: Any) -> None:
                    log.append("SIGCONT")
                    signal_hook(*args, **kwargs)

                if stopped:
                    os.kill(head.pid, signal.SIGSTOP)
                    _wait_for_process_stop(head.pid)
                with (
                    mock.patch.object(self.host, "_signal_head", signal_then_record),
                    mock.patch.object(ingress, "bind_before_delivery", record_bind),
                    mock.patch.object(self.host, "_worker_task_doc", return_value="# Rework\n"),
                    mock.patch.object(self.host.catalog, "integration_base", return_value="main", create=True),
                    mock.patch("ummanu.dispatch.host._provider_turn_started", return_value=False),
                ):
                    if report:
                        self.host.prompt_worker_report({"ref": REF}, record)
                    else:
                        self.host.resume_worker({"ref": REF, "project": "ummanu", "workspace": {}}, record)
                self.assertEqual(events, (["SIGCONT"] if stopped else []) + ["bind", "send"])
                self.assertEqual(record.worker_head_run["fanout_policy"]["provider_source"]["state"], "bound")
                self.assertTrue(record.worker_delivery_evidence["provider_bound"])
                self.assertEqual(record.worker_delivery_evidence["provider_source_state"], "bound")

    def test_codex_bind_failure_and_unbound_source_still_deliver(self) -> None:
        for raises in (True, False):
            with self.subTest(raises=raises):
                _head, record, run = self.codex_worker_for_delivery()

                def bind(*, fail: bool = raises, current: head_ops.HeadRun = run) -> head_ops.HeadRun:
                    if fail:
                        raise RuntimeError("source unavailable")
                    return current

                self.host._codex_provider_ingresses[run.run_id] = SimpleNamespace(  # type: ignore[assignment]
                    run=run, bind_before_delivery=bind
                )
                self.host.prompt_worker_report({"ref": REF}, record)
                self.assertFalse(record.worker_delivery_evidence["provider_bound"])
                self.assertEqual(record.worker_delivery_evidence["provider_source_state"], "unbound")

    def test_missing_codex_ingress_is_installed_from_the_durable_run(self) -> None:
        _head, record, run = self.codex_worker_for_delivery()
        records = {REF: record}
        saves: list[str] = []
        comments: list[str] = []
        runtime = SimpleNamespace(
            host=self.host, save_records=lambda *_: saves.append("persist"),
            writer=SimpleNamespace(comment=lambda **kwargs: comments.append(kwargs["body"])),
            owner="ummanu-dispatcher",
        )
        runtime.bind_codex_provider_ingress = lambda *args, **kwargs: DispatcherRuntime.bind_codex_provider_ingress(
            runtime, *args, **kwargs
        )
        self.assertNotIn(run.run_id, self.host._codex_provider_ingresses)
        outcome, _reason = deliver_worker_report_prompt(
            runtime, {"ref": REF}, record, records, {}, "a1", trigger="quiet"  # type: ignore[arg-type]
        )
        self.assertIsNotNone(outcome)
        self.assertTrue(saves)
        self.assertTrue(record.worker_delivery_evidence["provider_bound"])
        self.assertEqual(record.worker_head_run["fanout_policy"]["provider_source"]["state"], "bound")
        self.assertIn("Provider bound: True; source state: bound", comments[0])

    # a failure raised after the head was started ---------------------------

    def launch_worker(self):
        with mock.patch.dict(os.environ, {"UMMANU_DISPATCHER_BODY_DIR": str(self.data_dir)}):
            with mock.patch.object(self.host.catalog, "prepare_head_workspace", lambda *a, **k: None, create=True):
                with mock.patch.object(self.host.catalog, "head_launch", lambda *a, **k: HeadCommand("run-worker", prompt_after_start=True), create=True):
                    with mock.patch.object(self.host, "_launched", lambda *a, **k: "launched"):
                        return self.host._launch(
                            str(self.data_dir),
                            "title",
                            "codex",
                            "TASK.md",
                            role="worker",
                            env_name="UMMANU_UNSET_COMMAND",
                            task={"ref": REF, "project": "ummanu"},
                        )

    def started_head(self, handle: str = "run:worker") -> head_ops.HeadRun:
        return head_ops.HeadRun(
            run_id="host-test-run",
            spec=head_ops.HeadSpec(profile_id="codex", adapter="codex", runtime=LOCAL_PTY_RUNTIME),
            workspace=str(self.data_dir),
            task_ref=head_ops.TaskRef.card(REF),
            role="worker",
            handle=handle,
        )

    def test_a_delivery_failure_over_a_head_that_stays_up_aborts_the_launch(self) -> None:
        """The whole point of `HeadLaunchAborted`: the caller keeps its intent instead of blocking."""
        run = self.started_head()
        self.backend.start_failure = head_ops.HeadSpawnAborted("the head never took the prompt", run=run)

        with self.assertRaises(HeadLaunchAborted) as caught:
            self.launch_worker()

        self.assertEqual(caught.exception.handle, "run:worker")
        self.assertEqual(caught.exception.workspace, str(self.data_dir))

    def test_a_delivery_failure_whose_head_goes_stays_an_ordinary_failure(self) -> None:
        """The other half: nothing is left running, so the caller may block the card as before."""
        self.backend.start_failure = head_ops.HeadSpawnFailed("the head never took the prompt")

        with self.assertRaises(HostError) as caught:
            self.launch_worker()

        self.assertNotIsInstance(caught.exception, HeadLaunchAborted)
        self.assertIn("never took the prompt", str(caught.exception))


    # a stop that is not confirmed -------------------------------------------

    def test_a_head_that_ignores_every_signal_is_not_reported_as_stopped(self) -> None:
        pid_file = self.pid_file(str(os.getpid()))

        with mock.patch.object(self.host, "_signal_head", lambda *a, **k: None):
            with mock.patch.object(dispatcher_host_module, "HEAD_STOP_GRACE_SECONDS", 0.05):
                with self.assertRaisesRegex(HostError, "still running after stop"):
                    self.host._confirm_head_process_gone(pid_file)

    def test_a_live_foreign_heartbeat_is_never_signalled(self) -> None:
        pid_file = self.pid_file(str(os.getpid()))
        expected = heartbeat_identity(run_id="different-run", role="worker", task=f"card:{REF}")

        with mock.patch.object(self.host, "_signal_head") as signal_head:
            with self.assertRaisesRegex(HostError, "mismatching launch identity"):
                self.host._confirm_head_process_gone(pid_file, expected=expected)

        signal_head.assert_not_called()

    def test_a_head_its_supervisor_let_go_of_is_stopped_through_its_heartbeat(self) -> None:
        """The shape every adopted head has: no handle, only a pid. A workspace stop that the
        backend answered is still confirmed against the heartbeat, which ends the process."""
        head = subprocess.Popen(["sleep", "30"])
        self.addCleanup(self._reap_head, head)
        record = DispatcherRecord(
            worker="w1",
            workspace=str(self.data_dir),
            handle="",
            head="codex",
            review_head="codex-reviewer",
            attempt_id="a1",
            comment_baseline=0,
            review_baseline=0,
            state="claimed",
            claimed_at=0.0,
            worker_pid_file=self.pid_file(str(head.pid)),
        )
        self.track_worker(record)

        self.host.stop_workspace(record)

        self.assertEqual(self.backend.stops, ["host-test-run"])
        self.assertIsNotNone(head.poll(), "the head must actually be gone")
        self.assertFalse(Path(record.worker_pid_file).exists())

    def test_a_stopped_head_is_woken_before_its_graceful_stop(self) -> None:
        """SIGTERM is pending while stopped, so handoff must SIGCONT first."""
        head = subprocess.Popen(["sleep", "30"])
        self.addCleanup(self._reap_head, head)
        record = DispatcherRecord(
            worker="w1",
            workspace=str(self.data_dir),
            handle="",
            head="codex",
            review_head="codex-reviewer",
            attempt_id="a1",
            comment_baseline=0,
            review_baseline=0,
            state="claimed",
            claimed_at=0.0,
            worker_pid_file=self.pid_file(str(head.pid)),
            worker_continuation=WorkerContinuation(
                stage=WorkerContinuationStage.RETAINED,
                retained_at=time.time(),
            ),
        )
        self.track_worker(record)
        os.kill(head.pid, signal.SIGSTOP)

        self.host.stop_workspace(record)

        self.assertIsNotNone(head.poll(), "a retained head must exit without SIGKILL grace")

    def test_retention_stops_the_head_process_group(self) -> None:
        """A helper started by a worker is frozen with the worker, not left writing alone."""
        child_file = self.data_dir / "child.pid"
        head = subprocess.Popen(
            [
                "setsid",
                "sh",
                "-c",
                f"sleep 30 & echo $! > {child_file}; wait",
            ]
        )

        def reap_group() -> None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(head.pid), signal.SIGKILL)
            with contextlib.suppress(subprocess.TimeoutExpired):
                head.wait(timeout=1)

        self.addCleanup(reap_group)
        for _ in range(50):
            if child_file.exists():
                break
            time.sleep(0.01)
        child = int(child_file.read_text(encoding="utf-8"))
        self.addCleanup(lambda: os.kill(child, signal.SIGKILL))
        record = DispatcherRecord(
            worker="w1",
            workspace=str(self.data_dir),
            handle="term:worker",
            head="codex",
            review_head="codex-reviewer",
            attempt_id="a1",
            comment_baseline=0,
            review_baseline=0,
            state="claimed",
            claimed_at=0.0,
            worker_pid_file=self.pid_file(str(head.pid)),
            worker_run={"adapter": "codex", "codex_mode": "tui", "head": "codex"},
        )
        self.track_worker(record)

        self.host.retain_worker(record)

        status = ""
        for _ in range(50):
            status = Path(f"/proc/{child}/status").read_text(encoding="utf-8")
            if "State:\tT" in status:
                break
            time.sleep(0.01)
        self.assertIn("State:\tT", status)

    def test_claude_retained_worker_rewrites_task_before_delivering_rework(self) -> None:
        """Claude's interactive pane is reusable just like Codex TUI."""
        head = subprocess.Popen(["sleep", "30"])
        self.addCleanup(self._reap_head, head)
        record = DispatcherRecord(
            worker="w1",
            workspace=str(self.data_dir),
            handle="term:worker",
            head="claude-opus",
            review_head="codex-reviewer",
            attempt_id="a1",
            comment_baseline=0,
            review_baseline=3,
            report_generation=3,
            state="claimed",
            claimed_at=0.0,
            worker_pid_file=self.pid_file(str(head.pid)),
            worker_run={"adapter": "claude", "head": "claude-opus"},
            worker_continuation=WorkerContinuation(
                stage=WorkerContinuationStage.DELIVERY_PENDING,
                phase="gate",
                retained_at=time.time(),
                sent_at=time.time(),
            ),
        )
        self.track_worker(record)
        os.kill(head.pid, signal.SIGSTOP)
        _wait_for_process_stop(head.pid)
        task_at_delivery: list[str] = []
        prompt_at_delivery: list[str] = []

        def delivered(_run: head_ops.HeadRun, pointer: head_ops.NudgePointer) -> None:
            task_at_delivery.append((self.data_dir / "TASK.md").read_text())
            prompt_at_delivery.append(pointer.text)

        self.backend.on_deliver = delivered

        self.host.resume_worker({"ref": REF, "project": "ummanu", "workspace": {}}, record)

        self.assertIn("worker-report-done-ummanu-510-3", task_at_delivery[0])
        # The document the worker is sent back to and the prompt that wakes it name one round.
        self.assertIn("Generation 3", prompt_at_delivery[0])
        self.assertIn("not an earlier turn's", prompt_at_delivery[0])
        # secretary-1413: what the head actually receives is the pointer — the document's own
        # absolute path — and not the round, whose text stays in the file.
        self.assertIn(str(self.data_dir / "TASK.md"), prompt_at_delivery[0])
        self.assertLessEqual(len(prompt_at_delivery[0].encode("utf-8")), NUDGE_MAX_BYTES)
        self.assertNotIn("Reviewer findings", prompt_at_delivery[0])
        self.assertLess(
            len(prompt_at_delivery[0].encode("utf-8")),
            len(task_at_delivery[0].encode("utf-8")),
            "the round is in the document, not in the prompt",
        )
        # The suspended head was woken by the delivery's own pre-send step, not before it.
        self.assertFalse(head_process_status(record.worker_pid_file).get("stopped"))

    def test_a_running_retained_claude_replays_delivery_after_a_crash_after_readiness(self) -> None:
        """SIGCONT after a ready probe is still not a delivered continuation.

        The first call models a dispatcher dying after the readiness boundary but before the
        send. The recovery call sees a
        running but idle provider, waits for its TUI to settle, sends the prompt and confirms the
        turn rather than treating process liveness as delivery evidence.
        """
        head = subprocess.Popen(["sleep", "30"])
        self.addCleanup(self._reap_head, head)
        record = DispatcherRecord(
            worker="w1",
            workspace=str(self.data_dir),
            handle="term:worker",
            head="claude-opus",
            review_head="codex-reviewer",
            attempt_id="a1",
            comment_baseline=0,
            review_baseline=3,
            state="claimed",
            claimed_at=0.0,
            worker_pid_file=self.pid_file(str(head.pid)),
            worker_run={"adapter": "claude", "head": "claude-opus"},
            worker_continuation=WorkerContinuation(
                stage=WorkerContinuationStage.DELIVERY_PENDING,
                phase="gate",
                retained_at=time.time(),
                sent_at=time.time(),
            ),
        )
        self.track_worker(record)
        os.kill(head.pid, signal.SIGSTOP)
        _wait_for_process_stop(head.pid)
        real_signal = self.host._signal_head

        class DispatcherDied(BaseException):
            pass

        def die_after_continuing(pid_file: str, signal_number: int, **kwargs: Any) -> None:
            real_signal(pid_file, signal_number, **kwargs)
            raise DispatcherDied()

        with mock.patch.object(self.host, "_signal_head", die_after_continuing):
            with self.assertRaises(DispatcherDied):
                self.host.resume_worker({"ref": REF, "project": "ummanu", "workspace": {}}, record)
        self.assertEqual(self.backend.deliveries, [], "the first attempt died before its send")

        with mock.patch("ummanu.dispatch.tui.latest_claude_user_turn_for", return_value=None):
            self.host.resume_worker({"ref": REF, "project": "ummanu", "workspace": {}}, record)

        self.assertFalse(head_process_status(record.worker_pid_file).get("stopped"))
        self.assertEqual(len(self.backend.deliveries), 1, "a running head is not proof of delivery")

    def test_a_running_retained_claude_recovers_from_its_durable_user_turn(self) -> None:
        """A Claude JSONL user record proves delivery after a crash without terminal guessing."""
        head = subprocess.Popen(["sleep", "30"])
        self.addCleanup(self._reap_head, head)
        sent_at = time.time() - 1
        record = DispatcherRecord(
            worker="w1",
            workspace=str(self.data_dir),
            handle="term:worker",
            head="claude-opus",
            review_head="codex-reviewer",
            attempt_id="a1",
            comment_baseline=0,
            review_baseline=3,
            state="claimed",
            claimed_at=0.0,
            worker_pid_file=self.pid_file(str(head.pid)),
            worker_run={"adapter": "claude", "head": "claude-opus"},
            worker_continuation=WorkerContinuation(
                stage=WorkerContinuationStage.DELIVERY_PENDING,
                phase="gate",
                retained_at=sent_at,
                sent_at=sent_at,
            ),
        )
        self.track_worker(record)
        projects = self.data_dir / "claude-projects"
        session = projects / claude_project_dir_name(str(self.data_dir)) / "session.jsonl"
        session.parent.mkdir(parents=True)
        session.write_text(
            json.dumps({"type": "user", "timestamp": "2099-01-02T03:04:05Z"}) + "\n",
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {"UMMANU_CLAUDE_PROJECTS": str(projects)}):
            self.host.resume_worker({"ref": REF, "project": "ummanu", "workspace": {}}, record)

        self.assertEqual(self.backend.deliveries, [])

    def test_a_confirmed_retained_continuation_is_not_delivered_twice_on_recovery(self) -> None:
        """A crash after checkpointing delivery must leave the active worker alone."""
        head = subprocess.Popen(["sleep", "30"])
        self.addCleanup(self._reap_head, head)
        record = DispatcherRecord(
            worker="w1",
            workspace=str(self.data_dir),
            handle="term:worker",
            head="claude-opus",
            review_head="codex-reviewer",
            attempt_id="a1",
            comment_baseline=0,
            review_baseline=3,
            state="claimed",
            claimed_at=0.0,
            worker_pid_file=self.pid_file(str(head.pid)),
            worker_run={"adapter": "claude", "head": "claude-opus"},
            worker_continuation=WorkerContinuation(
                stage=WorkerContinuationStage.DELIVERY_CONFIRMED,
                phase="gate",
                retained_at=time.time(),
                sent_at=time.time(),
            ),
        )
        self.track_worker(record)

        self.host.resume_worker({"ref": REF, "project": "ummanu", "workspace": {}}, record)

        self.assertEqual(self.backend.deliveries, [])

    def test_dead_or_missing_retained_worker_refuses_continuation(self) -> None:
        record = DispatcherRecord(
            worker="w1",
            workspace=str(self.data_dir / "missing"),
            handle="term:worker",
            head="claude-opus",
            review_head="codex-reviewer",
            attempt_id="a1",
            comment_baseline=0,
            review_baseline=0,
            state="claimed",
            claimed_at=0.0,
            worker_pid_file=self.pid_file(str(DEAD_PID)),
            worker_run={"adapter": "claude"},
            worker_continuation=WorkerContinuation(
                stage=WorkerContinuationStage.DELIVERY_PENDING,
                phase="gate",
                retained_at=time.time(),
                sent_at=time.time(),
            ),
        )

        with self.assertRaisesRegex(HostError, "session exited"):
            self.host.resume_worker({"ref": REF, "project": "ummanu", "workspace": {}}, record)

    def test_a_stopped_retained_worker_with_no_workspace_refuses_continuation(self) -> None:
        head = subprocess.Popen(["sleep", "30"])
        self.addCleanup(self._reap_head, head)
        record = DispatcherRecord(
            worker="w1",
            workspace=str(self.data_dir / "missing"),
            handle="term:worker",
            head="claude-opus",
            review_head="codex-reviewer",
            attempt_id="a1",
            comment_baseline=0,
            review_baseline=0,
            state="claimed",
            claimed_at=0.0,
            worker_pid_file=self.pid_file(str(head.pid)),
            worker_run={"adapter": "claude"},
            worker_continuation=WorkerContinuation(
                stage=WorkerContinuationStage.DELIVERY_PENDING,
                phase="gate",
                retained_at=time.time(),
                sent_at=time.time(),
            ),
        )
        self.track_worker(record)
        os.kill(head.pid, signal.SIGSTOP)
        _wait_for_process_stop(head.pid)

        with self.assertRaisesRegex(HostError, "workspace is missing"):
            self.host.resume_worker({"ref": REF, "project": "ummanu", "workspace": {}}, record)

    def test_a_head_nothing_names_cannot_be_reported_as_stopped(self) -> None:
        record = DispatcherRecord(
            worker="w1",
            workspace=str(self.data_dir),
            handle="",
            head="codex",
            review_head="codex-reviewer",
            attempt_id="a1",
            comment_baseline=0,
            review_baseline=0,
            state="claimed",
            claimed_at=0.0,
        )

        # secretary-1722: a record that names no run of its worker was written before runs were
        # recorded, which is a legacy record; nothing is stopped and nothing reports a stop.
        with self.assertRaisesRegex(LegacyDispatcherRecord, "is a legacy Orca record"):
            self.host.stop_head(record, "worker")
        self.assertEqual(self.backend.stops, [])


class WorkerLifecycleRunTests(unittest.TestCase):
    """Which run a stop of this card's worker acts on, read from the record alone."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.data_dir = Path(self.tmpdir.name)
        self.host = CommandHostRuntime(FakeCatalog(), self.data_dir, mode="real", audit=card_audit(self))  # type: ignore[arg-type]

    def record(self, run: head_ops.HeadRun) -> DispatcherRecord:
        record = DispatcherRecord(
            worker="w1",
            workspace=str(self.data_dir),
            handle="run:1",
            head="codex",
            review_head="codex-reviewer",
            attempt_id="a1",
            comment_baseline=0,
            review_baseline=0,
            state="claimed",
            claimed_at=0.0,
        )
        record.worker_head_run = run.to_json()
        return record

    def test_a_confirmed_stop_is_kept_over_a_record_that_still_names_a_head(self) -> None:
        """An `exited` run is finished with, and its receipt is the truthful record. A record that
        still names a head afterwards gets no fresh identity: one minted here was never launched,
        named the worker id as its card and poisoned Done cleanup (secretary-1918). The stop of a
        settled run re-reads its pid file instead (`_confirm_settled_head`)."""
        exited = (
            head_ops.HeadRun(
                run_id="run-1",
                spec=head_ops.HeadSpec(profile_id="codex", adapter="codex", runtime=LOCAL_PTY_RUNTIME),
                workspace=str(self.data_dir),
                task_ref=head_ops.TaskRef.card(REF),
                role="worker",
            )
            .finishing(head_ops.StopInitiator(actor="operator"))
            .exited()
        )
        record = self.record(exited)
        record.handle = "run:2"

        run = self.host.worker_lifecycle_run(record)

        self.assertEqual(run.run_id, exited.run_id)
        self.assertEqual(run.lifecycle, "exited")
        self.assertEqual(run.task_ref, head_ops.TaskRef.card(REF))
        self.assertEqual(run.spec.runtime, LOCAL_PTY_RUNTIME)


class ProductionLaunchIntentTests(unittest.TestCase):
    """The same contour under the production tick, where records outlive one card's cycle.

    Two things only exist here: the reconciliation pass, which removes the records of cards the
    board has taken out of the active cycle, and the pipeline freeze. Both used to walk past a head
    that had no pane handle — the shape every head adopted from a launch intent has.
    """

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
        # The card belongs to a sprint with a concrete observer, so a substantive verdict parks
        # for a decision: these tests drive the rework that decision opens.
        self.sprints = FakeSprints()
        self.sprints.rows["sprint:1031"] = {
            "ref": "sprint:1031",
            "status": "open",
            "observer": {"kind": "head", "profile": "claude-observer"},
        }
        self.board.save_metadata(12, sprint_ref="sprint:1031")
        bind_observer(self, "sprint:1031")
        # And that sprint reserves the card's project, which is what lets its observer decide.
        self.board.add_sprint("sprint:1031", status="open", sprint_reservations='["ummanu"]')
        self.runtime = DispatcherRuntime(
            self.reader,
            self.writer,
            task_audit_for(self.board),
            self.data_dir,
            self.catalog,  # type: ignore[arg-type]
            self.host,  # type: ignore[arg-type]
            owner="ummanu-pilot",
            sprints=self.sprints,
        )

    # fixtures ---------------------------------------------------------------

    def tick(self) -> dict:
        return self.runtime.production_tick()

    def actions(self, result: dict) -> list[dict]:
        return [action for action in result.get("actions") or []]

    def records(self) -> dict:
        payload = self.runtime.production_state.load()
        return payload.get("records") or {}

    def workspace_of_record(self) -> str:
        return str((self.records().get(REF) or {}).get("workspace") or "")

    def stored_intent(self) -> dict:
        return dict((self.records().get(REF) or {}).get("launch_intent") or {})

    @contextlib.contextmanager
    def state_dies_after(self, host_method: str):
        real_save = self.runtime.production_state.save
        real_call = getattr(self.host, host_method)
        launched = {"yet": False}

        def save(payload: dict) -> None:
            if launched["yet"]:
                raise OSError("production state is not writable")
            real_save(payload)

        def call(*args, **kwargs):
            result = real_call(*args, **kwargs)
            launched["yet"] = True
            return result

        with mock.patch.object(self.runtime.production_state, "save", save):
            with mock.patch.object(self.host, host_method, call):
                yield

    def leave_a_post_launch_intent(self) -> None:
        """Claim the card and lose the tick right after the worker head came up."""
        with self.state_dies_after("prepare_worker"), self.assertRaises(OSError):
            self.tick()
        self.assertEqual(self.host.prepared, [REF])
        self.assertEqual(self.stored_intent().get("role"), "worker")

    def leave_a_post_launch_review_intent(self) -> None:
        """Take the card to validate and lose the tick right after the reviewer pane came up."""
        self.tick()
        self.report_done()
        self.tick()
        with self.state_dies_after("start_review"), self.assertRaises(OSError):
            self.tick()
        self.assertEqual(self.host.reviews, [REF])
        self.assertEqual(self.stored_intent().get("role"), "review")

    def leave_a_post_launch_rework_intent(self) -> None:
        """Lose the tick right after a red verdict's rework head came up, round 2 reserved."""
        self.leave_a_post_launch_review_intent()
        self.tick()  # the reviewer of the lost tick is adopted
        self.writer.verdict(
            role="reviewer",
            actor="reviewer",
            reference=REF,
            kind="red",
            body="needs work",
            request_id="verdict-red",
        )
        self.tick()  # the verdict parks the card
        self.writer.decide(
            role="observer",
            actor="observer",
            reference=REF,
            kind="rework",
            body="observer decision",
            request_id="decision-rework",
        )
        with self.state_dies_after("restart_worker"), self.assertRaises(OSError):
            self.tick()
        intent = self.stored_intent()
        self.assertEqual(
            (intent["action"], intent["round"], intent["opens_round"]), ("review-red-rework", 2, True)
        )

    def report_done(self) -> None:
        """Report through the done command the checkout holds: that id names the round the
        dispatcher is waiting for (secretary-1063)."""
        self.writer.report(
            role="worker",
            actor="worker",
            reference=REF,
            kind="done",
            body="done",
            request_id=_document_report_id(self.workspace_of_record()),
        )

    def move_card(self, target: str, reason: str, request_id: str) -> None:
        # The card's sprint reserves its project, so an operator move is a recorded override.
        self.writer.move(
            role="po",
            actor="operator",
            reference=REF,
            target=target,
            reason=reason,
            request_id=request_id,
            sprint_override=True,
            sprint_override_reason="the operator moves a card of a reserved project by hand",
        )

    def head_alive(self, kind: str) -> bool:
        return Path(pid_file_path(kind, REF)).exists()

    # reconciliation ---------------------------------------------------------

    def test_a_card_moved_out_of_the_cycle_takes_its_unresolved_head_with_it(self) -> None:
        """The record is the only pointer to that head, so it cannot be dropped over one.

        `_tick_task` never sees this card again — the board has taken it out of the active cycle —
        so reconciliation is the last chance to settle the launch. Removing the record first would
        strand a live worker in the workspace, and a requeue would put a second one in beside it.
        """
        self.leave_a_post_launch_intent()
        self.move_card("blocked", "PO parked it mid-launch", "move-to-blocked")
        self.host.calls.clear()

        result = self.tick()

        reconciled = [a for a in self.actions(result) if a["step"] == "production-reconcile"]
        self.assertEqual([a["action"] for a in reconciled], ["record-removed"])
        self.assertEqual(reconciled[0]["stopped_launch"], "claim")
        self.assertIn("stop_head:worker", self.host.calls)
        self.assertNotIn("stop_workspace", self.host.calls)
        self.assertFalse(self.head_alive("worker"), "the head of the unresolved launch is gone")
        self.assertNotIn(REF, self.records())

        # And the requeue that follows starts one head, not one beside a survivor. The neighbour
        # card is parked first: one code task per project may be active, and it took the slot the
        # moment this card left the cycle.
        self.writer.move(
            role="po",
            actor="operator",
            reference="ummanu-511",
            target="issues",
            reason="park the neighbour",
            request_id="park-neighbor",
            sprint_override=True,
            sprint_override_reason="the operator moves a card of a reserved project by hand",
        )
        self.move_card("ready", "back to the queue", "move-back-to-ready")
        for _ in range(3):
            self.tick()

        self.assertEqual(
            self.host.prepared.count(REF), 2, "one head for the first claim, one for the requeue"
        )

    def test_ready_card_settles_its_unresolved_launch_before_reclaim(self) -> None:
        self.leave_a_post_launch_intent()
        self.writer.move(
            role="po",
            actor="operator",
            reference="ummanu-511",
            target="issues",
            reason="make the requeue claimable",
            sprint_override=True,
            sprint_override_reason="the operator moves a card of a reserved project by hand",
            request_id="park-neighbor-for-ready-intent",
        )
        self.move_card("ready", "requeue during bring-up", "ready-with-live-intent")
        self.host.calls.clear()

        self.tick()

        self.assertEqual(self.reader.show(REF)["state"], "in_progress")
        self.assertIn("stop_head:worker", self.host.calls)
        self.assertNotIn("stop_workspace", self.host.calls)
        self.assertEqual(self.host.prepared.count(REF), 2)
        self.assertEqual(self.stored_intent(), {})

    def test_workspace_only_adopted_record_is_stopped_before_removal(self) -> None:
        self.tick()
        payload = self.runtime.production_state.load()
        record = payload["records"][REF]
        record["handle"] = ""
        record["worker_leaf"] = ""
        record["worker_pid_file"] = ""
        record["review_handle"] = ""
        record["review_leaf"] = ""
        record["review_pid_file"] = ""
        self.runtime.production_state.save(payload)
        self.move_card("blocked", "park the adopted card", "park-workspace-only-record")
        self.host.calls.clear()

        result = self.tick()

        actions = [a for a in self.actions(result) if a["step"] == "production-reconcile"]
        self.assertEqual([a["action"] for a in actions], ["record-removed"])
        self.assertIn("stop_workspace", self.host.calls)
        self.assertNotIn(REF, self.records())

    def test_settled_ready_workspace_is_not_stopped_again_on_later_ticks(self) -> None:
        self.tick()
        payload = self.runtime.production_state.load()
        record = payload["records"][REF]
        record["handle"] = ""
        record["worker_leaf"] = ""
        record["worker_pid_file"] = ""
        record["review_handle"] = ""
        record["review_leaf"] = ""
        record["review_pid_file"] = ""
        self.runtime.production_state.save(payload)
        self.move_card("ready", "park behind the other ready card", "park-workspace-record")
        self.runtime.pause.save({"mode": "drain"})
        self.host.calls.clear()

        self.tick()
        self.tick()

        self.assertEqual(self.host.calls.count("stop_workspace"), 1)
        record = self.records()[REF]
        self.assertTrue(record["workspace_settled"])
        restored = DispatcherRecord.from_json(record)
        self.assertFalse(restored.owns_head())
        self.assertFalse(restored.needs_settling())

    def test_role_identity_without_workspace_is_stopped_before_removal(self) -> None:
        self.tick()
        payload = self.runtime.production_state.load()
        record = payload["records"][REF]
        record["workspace"] = ""
        record["handle"] = ""
        record["worker_leaf"] = ""
        record["worker_pid_file"] = ""
        record["review_handle"] = ""
        record["review_leaf"] = ""
        record["review_pid_file"] = pid_file_path("review", REF)
        self.runtime.production_state.save(payload)
        self.host._write_head_pid("review", REF)
        self.move_card("blocked", "park the identity-only record", "park-identity-only-record")
        self.host.calls.clear()

        result = self.tick()

        actions = [a for a in self.actions(result) if a["step"] == "production-reconcile"]
        self.assertEqual([a["action"] for a in actions], ["record-removed"])
        self.assertIn("stop_head:review", self.host.calls)
        self.assertNotIn(REF, self.records())

    def test_a_stop_the_host_refuses_keeps_the_record_and_its_intent(self) -> None:
        self.leave_a_post_launch_intent()
        self.move_card("blocked", "PO parked it mid-launch", "move-to-blocked")
        self.host.fail_stop_head_reason = "orca terminal close failed"

        result = self.tick()

        reconciled = [a for a in self.actions(result) if a["step"] == "production-reconcile"]
        self.assertEqual([a["action"] for a in reconciled], ["launch-intent-stop-unconfirmed"])
        self.assertEqual(reconciled[0]["status"], "degraded")
        self.assertEqual(self.stored_intent().get("role"), "worker", "the pointer survives")

        # The next tick retries the same stop, and only then lets the record go.
        self.host.fail_stop_head_reason = ""

        retried = self.tick()

        actions = [a["action"] for a in self.actions(retried) if a["step"] == "production-reconcile"]
        self.assertEqual(actions, ["record-removed"])
        self.assertNotIn(REF, self.records())

    # a claim that moved under a launch nothing has resolved ------------------

    def claim_moved_to(self, worker: str) -> None:
        """Someone else's claim on the card this record was launched for."""
        self.board.save_metadata(12, claim=worker)

    def test_a_claim_that_moved_under_a_live_launch_stops_its_head_first(self) -> None:
        """The mismatch drops the record, and the intent on it is the only pointer to that head.

        It runs ahead of `_tick_task`, so nothing else will settle the launch: blocking the card
        and removing the record over a live worker leaves it in the checkout, and the requeue that
        follows opens a second one beside it.
        """
        self.leave_a_post_launch_intent()
        self.claim_moved_to("someone-else")
        self.host.calls.clear()

        result = self.tick()

        mismatch = [a for a in self.actions(result) if a.get("step") == "production-recovery"]
        self.assertEqual([a["status"] for a in mismatch], ["blocked"])
        self.assertIn("stop_head:worker", self.host.calls)
        self.assertNotIn("stop_workspace", self.host.calls)
        self.assertFalse(self.head_alive("worker"), "the head of the unresolved launch is gone")
        self.assertNotIn(REF, self.records())
        self.assertEqual(self.reader.show(REF)["state"], "blocked")
        blocked = [
            event
            for event in self.runtime.audit.events(REF)
            if event.get("transition", {}).get("target") == "blocked"
        ][-1]
        self.assertEqual(blocked["data"]["terminal_taxonomy"]["blocked_reason"], "other")
        self.assertEqual(_budget_event_type(blocked), "blocked")
        self.assertIsInstance(blocked["data"].get("attempt_outcome_owed"), dict)

    def test_a_mismatch_over_a_head_that_will_not_stop_keeps_the_record(self) -> None:
        self.leave_a_post_launch_intent()
        self.claim_moved_to("someone-else")
        self.host.fail_stop_head_reason = "orca terminal close failed"

        result = self.tick()

        mismatch = [a for a in self.actions(result) if a.get("step") == "production-recovery"]
        self.assertEqual([a["action"] for a in mismatch], ["launch-intent-stop-unconfirmed"])
        self.assertEqual(mismatch[0]["status"], "degraded")
        self.assertEqual(self.stored_intent().get("role"), "worker", "the pointer survives")
        self.assertTrue(self.head_alive("worker"))
        self.assertEqual(
            self.reader.show(REF)["state"], "in_progress", "the card is not blocked over a live head"
        )

        # Once the host confirms the stop, the mismatch is resolved the ordinary way.
        self.host.fail_stop_head_reason = ""

        retried = self.tick()

        mismatch = [a for a in self.actions(retried) if a.get("step") == "production-recovery"]
        self.assertEqual([a["status"] for a in mismatch], ["blocked"])
        self.assertFalse(self.head_alive("worker"))
        self.assertNotIn(REF, self.records())

    # freeze and resume ------------------------------------------------------

    def test_a_freeze_stops_an_adopted_worker_and_resume_brings_back_one_head(self) -> None:
        self.leave_a_post_launch_intent()
        self.assertEqual(
            [a["action"] for a in self.actions(self.tick()) if a.get("pilot_ref") == REF],
            ["worker-launch-adopted"],
        )

        paused = self.runtime.pause_pipeline(mode="freeze", actor="operator", reason="maintenance")

        self.assertEqual(paused["stopped_worker"], [REF])
        self.assertFalse(self.head_alive("worker"), "a freeze must reach a head with no handle")

        self.runtime.resume_pipeline(actor="operator")

        self.assertEqual(self.host.calls.count("restart_worker"), 1)
        self.assertTrue((self.records()[REF] or {})["handle"])

    def test_a_freeze_stops_the_worker_of_a_launch_nothing_has_resolved_yet(self) -> None:
        """Between the host call and the record's save the intent is the only pointer to that head.

        A freeze that looked only at the handle and the stored pid file would find neither, write an
        empty `stopped_worker`, and declare the pipeline stopped over a worker still editing the
        checkout.
        """
        self.leave_a_post_launch_intent()
        self.assertFalse((self.records()[REF] or {})["handle"], "no handle was ever recorded")
        self.assertTrue(self.head_alive("worker"))

        paused = self.runtime.pause_pipeline(mode="freeze", actor="operator", reason="maintenance")

        self.assertEqual(paused["stopped_worker"], [REF])
        self.assertFalse(self.head_alive("worker"), "the head of the unresolved launch is stopped")
        self.assertEqual(self.stored_intent(), {}, "a confirmed stop spends the intent")

        # And the resume puts back one head, from the record the freeze left behind.
        self.runtime.resume_pipeline(actor="operator")

        self.assertEqual(self.host.calls.count("restart_worker"), 1)
        self.assertEqual(self.host.calls.count("prepare_worker"), 1, "the claim is not redone")

    def test_a_freeze_that_cannot_stop_an_unresolved_launch_keeps_its_intent(self) -> None:
        self.leave_a_post_launch_intent()
        self.host.fail_stop_head_reason = "orca terminal close failed"

        paused = self.runtime.pause_pipeline(mode="freeze", actor="operator", reason="maintenance")

        self.assertEqual(paused["stopped_worker"], [], "an unconfirmed stop is not a stop")
        self.assertTrue(self.head_alive("worker"))
        self.assertEqual(self.stored_intent().get("role"), "worker", "the only pointer survives")

        # The resume launches nothing beside it, and the tick's own recovery still owns that head.
        self.runtime.resume_pipeline(actor="operator")
        self.host.fail_stop_head_reason = ""

        self.assertEqual(self.host.calls.count("restart_worker"), 0)
        self.assertEqual(
            [a["action"] for a in self.actions(self.tick()) if a.get("pilot_ref") == REF],
            ["worker-launch-adopted"],
        )
        self.assertEqual(self.host.prepared, [REF], "still exactly one head")

    def test_a_freeze_stops_the_reviewer_of_a_launch_nothing_has_resolved_yet(self) -> None:
        """The reviewer's window is wider: neither handle nor pid file is on the record yet."""
        self.leave_a_post_launch_review_intent()
        record = self.records()[REF] or {}
        self.assertEqual((record["review_handle"], record["review_pid_file"]), ("", ""))

        paused = self.runtime.pause_pipeline(mode="freeze", actor="operator", reason="maintenance")

        self.assertEqual(paused["stopped_reviewer"], [REF])
        self.assertFalse(self.head_alive("review"), "the reviewer of the lost tick is stopped")
        self.assertEqual(self.stored_intent(), {})

        self.runtime.resume_pipeline(actor="operator")

        self.assertEqual(self.host.reviews, [REF, REF], "one reviewer at a time, one after resume")

    def test_a_freeze_that_cannot_stop_an_unresolved_reviewer_keeps_its_intent(self) -> None:
        self.leave_a_post_launch_review_intent()
        # The reviewer of that launch wrote its heartbeat, so the intent's identity is its own pane
        # and the stop goes through the reviewer's lifecycle rather than the whole workspace.
        self.host.fail_stop_review_reason = "orca terminal stop failed"

        paused = self.runtime.pause_pipeline(mode="freeze", actor="operator", reason="maintenance")

        self.assertEqual(paused["stopped_reviewer"], [])
        self.assertTrue(self.head_alive("review"))
        self.assertEqual(self.stored_intent().get("role"), "review")

        self.runtime.resume_pipeline(actor="operator")

        self.assertEqual(self.host.reviews, [REF], "no second reviewer over a head still running")

    def test_a_freeze_that_stops_a_rework_launch_keeps_the_round_it_reserved(self) -> None:
        self.leave_a_post_launch_rework_intent()

        paused = self.runtime.pause_pipeline(mode="freeze", actor="operator", reason="maintenance")

        self.assertEqual(paused["stopped_worker"], [REF])
        self.assertEqual((self.records()[REF] or {})["attempt_round"], 2)

        self.runtime.resume_pipeline(actor="operator")

        self.assertEqual((self.records()[REF] or {})["attempt_round"], 2)

    def test_a_freeze_that_cannot_stop_an_adopted_worker_does_not_relaunch_it(self) -> None:
        self.leave_a_post_launch_intent()
        self.tick()
        self.host.fail_stop_head_reason = "orca terminal close failed"

        paused = self.runtime.pause_pipeline(mode="freeze", actor="operator", reason="maintenance")

        self.assertEqual(paused["stopped_worker"], [], "an unconfirmed stop is not a stop")
        self.assertTrue(self.head_alive("worker"))

        self.runtime.resume_pipeline(actor="operator")

        self.assertEqual(self.host.calls.count("restart_worker"), 0)


if __name__ == "__main__":
    unittest.main()
