"""Contract coverage for the launch-bound Codex provider-event journal ingress."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.dispatcher_fixtures import CARD_REF, DispatcherRuntimeFixture
from tests.fakes.dispatcher import FakeCatalog
from tests.fanout_fixtures import accepted_transport_run
from tests.production_runtime_fixtures import registered_production_runtime
from ummanu.codex_provider_events import (
    CodexProviderEventIngress,
)
from ummanu.dispatch import launch as dispatcher_launch
from ummanu.dispatch import observer as dispatcher_observer
from ummanu.dispatch import review as dispatcher_review
from ummanu.dispatch import worker_continuation
from ummanu.dispatch.cleanup import CleanupJournal, CleanupOwner
from ummanu.dispatch.host import CommandHostRuntime
from ummanu.dispatch.launch import (
    REVIEW_ROLE,
    WORKER_ROLE,
    confirm_launch_intent,
    merge_launch_head_run,
    resolve_launch_intent,
    write_launch_intent,
)
from ummanu.dispatch.observer import (
    OBSERVER_ROLE,
    ObserverRecord,
    _bind_codex_provider_ingress,
)
from ummanu.dispatch.observer import (
    _adopt_launch_intent as adopt_observer_launch_intent,
)
from ummanu.dispatch.observer import (
    _write_launch_intent as write_observer_launch_intent,
)
from ummanu.dispatch.production import ProductionState
from ummanu.dispatch.provider_failure import provider_failure_for_persisted_run, provider_failure_for_run
from ummanu.dispatch.runtime import DispatcherRuntime
from ummanu.dispatch.state import DispatcherRecord
from ummanu.dispatch.tui import provider_progress_for_persisted_run, provider_progress_for_run
from ummanu.dispatch.types import HostError
from ummanu.dispatch.worker_launch import bring_up_worker_head
from ummanu.dispatch.worker_lifecycle import WorkerContinuationLiveness
from ummanu.head_health import HeadReadiness
from ummanu.projects.contract import (
    ContractVerdict,
    ModuleContract,
)
from ummanu.projects.integration_base import resolve_integration_base
from ummanu.runtime import codex_preflight
from ummanu.runtime.head import (
    HEAD_OK,
    DeliverReceipt,
    HeadCommand,
    HeadRun,
    HeadSpec,
    StartReceipt,
    TaskRef,
)
from ummanu.runtime.head import operations as head_ops
from ummanu.runtime.head_run_binding import head_run_binding
from ummanu.runtime.head_runtimes import LOCAL_PTY_RUNTIME


class CodexProviderEventIngressTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.binary = self.root / "codex"
        self.binary.write_text("#!/bin/sh\nprintf 'codex 9.9.9\\n'\n", encoding="utf-8")
        self.binary.chmod(0o755)
        self.sessions = self.root / "sessions"
        self.source = self.sessions / "2026" / "08" / "13" / "run.jsonl"
        self.source.parent.mkdir(parents=True)
        self.written: list[HeadRun] = []
        self.stops: list[tuple[str, str]] = []
        self.blocks: list[dict] = []

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _attested_run(
        self,
        *,
        run_id: str = "run-1",
        role: str = "worker",
        spec: HeadSpec | None = None,
    ) -> tuple[HeadRun, dict[str, object]]:
        run = HeadRun(
            run_id=run_id,
            spec=spec or HeadSpec(profile_id="codex-extra", adapter="codex", model="gpt-5.6-terra"),
            workspace=str(self.workspace),
            task_ref=TaskRef.card("secretary-1428", document=str(self.workspace / "TASK.md")),
            role=role,
        )
        tools: list[dict] = []
        attestation = {
            "version": codex_preflight.FANOUT_ATTESTATION_VERSION,
            "role": run.role,
            "model": run.spec.model,
            "binary_digest": codex_preflight._file_digest(self.binary),
            "cli_version": "codex 9.9.9",
            "tools": tools,
            "tool_schema_digest": codex_preflight._json_digest(tools),
            "provider_schema_verdict": codex_preflight.FANOUT_SCHEMA_ALLOWED,
        }
        allowed = codex_preflight.attest_codex_fanout(
            {}, run, schema_attestation=attestation, binary_path=str(self.binary)
        )
        return allowed, attestation

    def _run(self) -> HeadRun:
        allowed, _attestation = self._attested_run()
        return allowed.with_fanout_policy(
            {
                **allowed.fanout_policy,
                "provider_source": {
                    "version": 1,
                    "kind": "codex_session_event_jsonl",
                    "state": "unbound",
                    **codex_preflight.codex_provider_source_descriptor(allowed),
                    "root": str(self.sessions),
                    "baseline": [],
                },
            }
        )

    def _preflight_run(
        self,
        *,
        run_id: str = "run-1",
        role: str = "worker",
        spec: HeadSpec | None = None,
    ) -> HeadRun:
        """The production preflight path, before the provider creates its new journal."""
        run, attestation = self._attested_run(run_id=run_id, role=role, spec=spec)
        return codex_preflight.preflight_codex_launch(
            {"codex_home": str(self.root)},
            str(self.workspace),
            run,
            schema_attestation=attestation,
            binary_path=str(self.binary),
            config=self.root / "config.toml",
        )

    def _write_source(self, *events: object, source: Path | None = None) -> None:
        records = [
            {"type": "session_meta", "payload": {"session_id": "session-1", "cwd": str(self.workspace)}},
            {"type": "thread.started", "thread_id": "parent-1"},
            *events,
        ]
        self._write_records(*records, source=source)

    def _write_records(self, *records: object, source: Path | None = None) -> None:
        target = source or self.source
        target.write_text(
            "\n".join(value if isinstance(value, str) else json.dumps(value) for value in records) + "\n",
            encoding="utf-8",
        )

    def _append_records(self, *records: object, source: Path | None = None) -> None:
        target = source or self.source
        body = target.read_text(encoding="utf-8")
        target.write_text(
            body
            + "\n".join(value if isinstance(value, str) else json.dumps(value) for value in records)
            + "\n",
            encoding="utf-8",
        )

    def _ingress(self, run: HeadRun | None = None) -> CodexProviderEventIngress:
        return CodexProviderEventIngress(
            run or self._run(),
            self.written.append,
            stop=lambda current, reason: self.stops.append((current.run_id, reason)),
            block=self.blocks.append,
        )

    def test_binds_new_session_and_cursor_before_any_provider_event(self) -> None:
        self._write_source()
        ingress = self._ingress()

        ingress.bind_before_delivery()

        bound = self.written[-1]
        source = bound.fanout_policy["provider_source"]
        self.assertEqual(source["state"], "bound")
        self.assertEqual(source["session_id"], "session-1")
        self.assertEqual(source["parent_thread_id"], "parent-1")
        self.assertEqual(self.written[0].fanout_policy["provider_source"]["cursor"]["line"], 0)
        self.assertEqual(source["initial_range"]["first"]["line"], 1)
        self.assertEqual(source["initial_range"]["root"]["line"], 2)
        self.assertEqual(source["initial_range"]["last"]["line"], 2)
        self.assertEqual(source["cursor"]["line"], 2)
        self.assertEqual(self.stops, [])

    def test_a_journal_written_after_the_launch_attempt_binds_on_the_next_poll(self) -> None:
        """sprint:1407: a pane whose session appeared late left the source unbound for its life."""
        ingress = self._ingress()

        # The launch attempt runs before the pane has written anything, so it selects nothing.
        ingress.bind_before_delivery()
        self.assertEqual(ingress.source["state"], "unbound")
        self.assertEqual(ingress.run.fanout_policy["state"], "unknown")

        self._write_source()
        ingress.poll()

        source = ingress.source
        self.assertEqual(source["state"], "bound")
        self.assertEqual(source["session_id"], "session-1")
        self.assertEqual(source["parent_thread_id"], "parent-1")
        # The late binding still scans the whole selected journal rather than skipping the tail
        # the pane wrote while the source was unbound.
        self.assertEqual(source["initial_range"]["first"]["line"], 1)
        self.assertEqual(source["cursor"]["line"], 2)
        self.assertEqual(self.stops, [])
        self.assertEqual(self.blocks, [])

    def test_a_late_poll_still_refuses_an_ambiguous_or_foreign_journal(self) -> None:
        """Retrying the binding does not relax any axis a journal is claimed by."""
        second = self.sessions / "2026" / "08" / "13" / "other.jsonl"
        self._write_source()
        self._write_records(
            {"type": "session_meta", "payload": {"session_id": "session-2", "cwd": str(self.workspace)}},
            {"type": "thread.started", "thread_id": "parent-2"},
            source=second,
        )
        ingress = self._ingress()

        ingress.poll()

        self.assertEqual(ingress.source["state"], "unbound")
        self.assertEqual(ingress.run.fanout_policy["state"], "unknown")

        # A journal from another workspace is not the missing single candidate either.
        second.unlink()
        foreign = self.sessions / "2026" / "08" / "13" / "foreign.jsonl"
        self._write_records(
            {"type": "session_meta", "payload": {"session_id": "session-3", "cwd": str(self.root)}},
            {"type": "thread.started", "thread_id": "parent-3"},
            source=foreign,
        )
        ingress.poll()

        self.assertEqual(ingress.source["state"], "bound")
        self.assertEqual(ingress.source["session_id"], "session-1")
        self.assertEqual(self.stops, [])

    def test_binds_codex_0147_session_meta_without_thread_started(self) -> None:
        """The live TUI journal uses session_meta as its root identity anchor."""
        for identifier in ("id", "session_id"):
            with self.subTest(identifier=identifier):
                self._write_records(
                    {
                        "type": "session_meta",
                        "payload": {identifier: "session-1", "cwd": str(self.workspace)},
                    },
                    {
                        "type": "event_msg",
                        "payload": {"type": "task_started", "thread_id": "session-1"},
                    },
                    {
                        "type": "event_msg",
                        "payload": {"type": "task_complete", "thread_id": "session-1"},
                    },
                )
                ingress = self._ingress()

                ingress.bind_before_delivery()

                source = self.written[-1].fanout_policy["provider_source"]
                self.assertEqual(source["state"], "bound")
                self.assertEqual(source["session_id"], "session-1")
                self.assertEqual(source["parent_thread_id"], "session-1")
                self.assertEqual(source["initial_range"]["root"]["line"], 1)
                self.assertEqual(source["cursor"]["line"], 3)
                persisted = HeadRun.from_json(self.written[-1].to_json())
                writes_after_bind = len(self.written)
                recovered = self._ingress(persisted)
                recovered.poll()
                self.assertEqual(len(self.written), writes_after_bind)
                self.assertEqual(
                    recovered.run.fanout_policy["provider_source"],
                    persisted.fanout_policy["provider_source"],
                )

    def test_preflight_descriptor_survives_real_bind_and_admits_new_progress(self) -> None:
        """The launch source reaches liveness through binding without losing its HeadRun fence."""
        preflight = self._preflight_run()
        original = dict(preflight.fanout_policy["provider_source"])
        self._write_source()
        ingress = self._ingress(preflight)

        ingress.bind_before_delivery()

        bound = ingress.run
        source = bound.fanout_policy["provider_source"]
        for field in ("run_id", "head_run_fingerprint", "workspace", "role", "task_ref", "root", "baseline"):
            self.assertEqual(source[field], original[field])
        baseline = provider_progress_for_run(bound)
        self.assertEqual(baseline["state"], "observed")
        self.assertEqual(baseline["admission"], "accepted")
        liveness = WorkerContinuationLiveness.begin(bound.to_json())
        self.assertEqual(liveness.observe_provider(baseline, 10.0, head_run=bound.to_json()), "baseline")

        self._append_records({"type": "turn.completed", "thread_id": "parent-1"})
        ingress.poll()
        progressed_run = ingress.run
        progress = provider_progress_for_run(progressed_run)

        self.assertTrue(progressed_run.same_run(preflight))
        self.assertNotEqual(progress["cursor"], baseline["cursor"])
        self.assertEqual(progress["state"], "observed")
        self.assertEqual(
            liveness.observe_provider(
                progress,
                20.0,
                head_run=progressed_run.to_json(),
            ),
            "progressed",
        )
        self.assertEqual(liveness.busy_attempts, 0)

    def _profile_bound_run(self, limit: int | None = None) -> HeadRun:
        profile = {
            "adapter": "codex",
            "model": "gpt-6.1-sol",
            "effort": "high",
            "resource": "openai-sub",
            "codex_mode": "tui",
            "runtime": "local-pty",
        }
        if limit is not None:
            profile["memory_limit_mib"] = limit
        spec = HeadSpec.from_profile("codex-sol61-high", profile)
        preflight = self._preflight_run(spec=spec)
        self._write_source()
        ingress = self._ingress(preflight)
        ingress.bind_before_delivery()
        self.assertEqual(ingress.source["state"], "bound")
        self.assertEqual(
            ingress.source["head_run_fingerprint"],
            preflight.fanout_policy["provider_source"]["head_run_fingerprint"],
        )
        return ingress.run

    def test_deployed_v1_digest_is_fixed_when_persisted_spec_grows(self) -> None:
        # Golden digest from the deployed seven-field producer, before memory limits.
        spec = HeadSpec.from_profile(
            "codex-sol61-high",
            {
                "adapter": "codex",
                "model": "gpt-6.1-sol",
                "effort": "high",
                "resource": "openai-sub",
                "codex_mode": "tui",
                "fallback": ["claude-opus-high"],
                "memory_limit_mib": 12288,
            },
        )
        run = HeadRun(
            run_id="deployed-v1",
            spec=spec,
            workspace="/workspace",
            task_ref=TaskRef.card("secretary-1883"),
            role="worker",
        )
        for limit in (None, 8192, 12288):
            with self.subTest(limit=limit):
                current = replace(run, spec=replace(spec, memory_limit_mib=limit))
                descriptor = codex_preflight.codex_provider_source_descriptor(current)
                self.assertEqual(descriptor["head_run_fingerprint"], "e7fd0d34702c7a2374dafd04a7543d68")
                self.assertEqual(
                    head_run_binding(current.to_json()), ("deployed-v1", descriptor["head_run_fingerprint"])
                )

    def test_profile_memory_and_generation_conflicts_remain_lifecycle_fenced(self) -> None:
        bound = replace(self._profile_bound_run(12288), scope_generation="original")
        for foreign in (
            replace(bound, spec=replace(bound.spec, memory_limit_mib=8192)),
            replace(bound, scope_generation="replacement"),
        ):
            # A journal cannot attest scope ownership; the authoritative handoff must reject it.
            self.assertEqual(head_run_binding(foreign.to_json()), head_run_binding(bound.to_json()))
            with self.assertRaisesRegex(HostError, "identity mismatch"):
                merge_launch_head_run(bound.to_json(), foreign.to_json())

    def test_serialized_spec_digest_is_not_rebound_into_a_deployed_v1_episode(self) -> None:
        bound = self._profile_bound_run(12288)
        liveness = WorkerContinuationLiveness.begin(bound.to_json())
        old_stable = {
            key: bound.to_json()[key] for key in ("run_id", "workspace", "task_ref", "role", "spec")
        }
        old_digest = hashlib.sha256(
            json.dumps(old_stable, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        ).hexdigest()[:32]
        self.assertNotEqual(old_digest, liveness.head_run_fingerprint)
        legacy = WorkerContinuationLiveness.from_json(
            {**liveness.to_json(), "head_run_fingerprint": old_digest}
        )
        self.assertEqual(
            legacy.observe_provider(provider_progress_for_run(bound), 10.0, head_run=bound.to_json()),
            "unknown",
        )
        self.assertEqual(legacy.head_run_fingerprint, old_digest)
        self.assertTrue(legacy.source_rejected)
        self.assertFalse(legacy.admitted)

    def test_profile_limits_survive_preflight_ingress_persistence_and_both_readers(self) -> None:
        for limit, expected in ((None, 8192), (12288, 12288)):
            with self.subTest(limit=limit):
                self.source = self.source.with_name(f"profile-{expected}.jsonl")
                bound = self._profile_bound_run(limit)
                self.assertEqual(bound.spec.memory_limit_mib, expected)
                # These are address/lifecycle updates after spawn, not a new provider launch.
                addressed = replace(
                    bound,
                    handle="head.sock",
                    leaf="pane-1",
                    pid_file="head.pid",
                    lifecycle="working",
                    scope_generation="owned-generation",
                )
                persisted = json.loads(json.dumps(addressed.to_json()))
                restored = HeadRun.from_json(persisted)
                progress = provider_progress_for_run(restored)
                self.assertEqual(progress["state"], "observed")
                self.assertEqual(progress, provider_progress_for_persisted_run(persisted))
                self.assertEqual(
                    progress["head_run_fingerprint"],
                    bound.fanout_policy["provider_source"]["head_run_fingerprint"],
                )
                self.assertEqual(provider_failure_for_run(restored)["state"], "none")
                self._append_records(
                    {"type": "event_msg", "payload": {"type": "task_started"}},
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "error": {"message": "unexpected status 503 Service Unavailable"},
                        },
                    },
                )
                self.assertNotEqual(provider_progress_for_run(restored)["cursor"], progress["cursor"])
                failure = provider_failure_for_persisted_run(persisted)
                self.assertEqual(failure["state"], "failed")
                self.assertEqual(failure["run_id"], bound.run_id)
                self.assertEqual(failure["error"]["status"], 503)

    def test_profile_bound_source_follows_retained_red_gate_delivery(self) -> None:
        bound = self._profile_bound_run(12288)
        record = DispatcherRecord(
            worker="worker-1",
            workspace=str(self.workspace),
            handle="head.sock",
            head=bound.spec.profile_id,
            review_head="claude",
            attempt_id="held-attempt",
            comment_baseline=0,
            review_baseline=0,
            state="validate",
            claimed_at=1.0,
            report_generation=1,
            attempt_round=1,
        )
        record.worker_head_run = json.loads(json.dumps(bound.to_json()))
        record.worker_continuation.begin_retention(1.0)
        record.worker_continuation.confirm_validation_move()
        task = {"ref": "secretary-1428", "type": "code", "comments": []}
        runtime = mock.Mock()
        runtime.reader.show.return_value = task
        host = object.__new__(CommandHostRuntime)
        runtime.host.provider_progress.side_effect = lambda *args: host.provider_progress(*args)
        events = []
        runtime.save_records.side_effect = lambda *_: events.append("save")
        runtime.host.resume_worker.side_effect = lambda *_: events.append("resume")
        with mock.patch.object(worker_continuation, "attempt_accounting") as accounting:
            result = worker_continuation._deliver_red_continuation(
                runtime,
                task,
                record,
                {task["ref"]: record},
                {},
                "tick-attempt",
                phase="gate",
            )
        self.assertEqual(result["action"], "gate-red-reused-worker")
        self.assertTrue(record.worker_continuation_liveness.admitted)
        self.assertEqual(record.worker_continuation_liveness.busy_attempts, 0)
        self.assertLess(events.index("save"), events.index("resume"))
        runtime.host.confirm_worker_retained.assert_called_once_with(record)
        runtime.host.resume_worker.assert_called_once_with(task, record)
        runtime.open_worker_round.assert_called_once()
        runtime._stop_worker_confirmed.assert_not_called()
        runtime.host.safe_recover_worker_continuation.assert_not_called()
        accounting.terminal_effect.assert_not_called()

    def test_profile_source_rejects_foreign_launch_facts_and_malformed_descriptors(self) -> None:
        bound = self._profile_bound_run(12288)
        foreign_runs = [
            replace(bound, run_id="foreign"),
            replace(bound, workspace=str(self.root)),
            replace(bound, role="reviewer"),
            replace(bound, task_ref=TaskRef.card("foreign")),
        ]
        for field, value in (
            ("profile_id", "foreign"),
            ("adapter", "claude"),
            ("model", "foreign"),
            ("effort", "low"),
            ("resource", "foreign"),
            ("codex_mode", "foreign"),
            ("fallback", ("foreign",)),
        ):
            foreign_runs.append(replace(bound, spec=replace(bound.spec, **{field: value})))
        source = bound.fanout_policy["provider_source"]
        for field, value in (
            ("version", 2),
            ("run_id", []),
            ("task_ref", None),
            ("head_run_fingerprint", ""),
            ("session_id", "foreign"),
        ):
            foreign_runs.append(
                bound.with_fanout_policy({**bound.fanout_policy, "provider_source": {**source, field: value}})
            )
        for index, foreign in enumerate(foreign_runs):
            with self.subTest(index=index):
                self.assertIn(
                    provider_progress_for_run(foreign)["state"], ("identity_mismatch", "unavailable")
                )
                self.assertEqual(provider_failure_for_run(foreign)["state"], "unavailable")
                liveness = WorkerContinuationLiveness.begin(bound.to_json())
                self.assertNotEqual(
                    liveness.observe_provider(
                        provider_progress_for_run(foreign), 10.0, head_run=bound.to_json()
                    ),
                    "baseline",
                )
                self.assertFalse(liveness.admitted)

    def test_profile_source_rejects_wrong_journal_root_and_initial_range(self) -> None:
        bound = self._profile_bound_run(12288)
        source = bound.fanout_policy["provider_source"]
        other = self.source.with_name("foreign.jsonl")
        self._write_records(
            {"type": "session_meta", "payload": {"session_id": "foreign", "cwd": str(self.workspace)}},
            source=other,
        )
        wrong_range = copy.deepcopy(source["initial_range"])
        wrong_range["digest"] = "0" * 64
        for field, value in (
            ("root", str(self.workspace)),
            ("path", str(other)),
            ("initial_range", wrong_range),
        ):
            with self.subTest(field=field):
                damaged = bound.with_fanout_policy(
                    {**bound.fanout_policy, "provider_source": {**source, field: value}}
                )
                self.assertNotEqual(provider_progress_for_run(damaged)["state"], "observed")
                self.assertEqual(provider_failure_for_run(damaged)["state"], "unavailable")

    def test_real_bound_sources_refresh_the_shared_worker_and_reviewer_progress_seam(self) -> None:
        worker_preflight = self._preflight_run()
        self._write_source()
        worker_ingress = self._ingress(worker_preflight)
        worker_ingress.bind_before_delivery()

        review_source = self.source.with_name("review.jsonl")
        review_preflight = self._preflight_run(run_id="review-run", role="reviewer")
        self._write_source(source=review_source)
        review_ingress = self._ingress(review_preflight)
        review_ingress.bind_before_delivery()

        record = DispatcherRecord(
            worker="secretary-1428-worker",
            workspace=str(self.workspace),
            handle="term-worker",
            head="codex-extra",
            review_head="codex-extra",
            attempt_id="attempt-1",
            comment_baseline=0,
            review_baseline=0,
            state="reviewing",
            claimed_at=0.0,
        )
        record.worker_head_run = worker_ingress.run.to_json()
        record.review_head_run = review_ingress.run.to_json()
        host = object.__new__(CommandHostRuntime)
        task = {"ref": "secretary-1428"}

        worker_baseline = CommandHostRuntime.provider_progress(host, task, record, "worker")
        reviewer_progress = CommandHostRuntime.provider_progress(host, task, record, "review")
        self.assertEqual(worker_baseline["state"], "observed")
        self.assertEqual(worker_baseline["admission"], "accepted")
        self.assertEqual(reviewer_progress["state"], "observed")
        self.assertEqual(reviewer_progress["admission"], "accepted")

        liveness = WorkerContinuationLiveness.begin(record.worker_head_run)
        self.assertEqual(
            liveness.observe_provider(
                worker_baseline,
                10.0,
                head_run=record.worker_head_run,
            ),
            "baseline",
        )
        self._append_records({"type": "turn.completed", "thread_id": "parent-1"})
        worker_ingress.poll()
        record.worker_head_run = worker_ingress.run.to_json()
        worker_progress = CommandHostRuntime.provider_progress(host, task, record, "worker")

        self.assertTrue(worker_ingress.run.same_run(worker_preflight))
        self.assertNotEqual(worker_progress["cursor"], worker_baseline["cursor"])
        self.assertEqual(worker_progress["admission"], "accepted")
        self.assertEqual(
            liveness.observe_provider(
                worker_progress,
                20.0,
                head_run=record.worker_head_run,
            ),
            "progressed",
        )

    def test_incomplete_or_foreign_bound_descriptor_cannot_admit_progress(self) -> None:
        self._write_source()
        ingress = self._ingress()
        ingress.bind_before_delivery()
        bound = ingress.run
        source = dict(bound.fanout_policy["provider_source"])
        for field, value in (("run_id", ""), ("workspace", "/foreign")):
            with self.subTest(field=field):
                damaged = bound.with_fanout_policy(
                    {
                        **bound.fanout_policy,
                        "provider_source": {**source, field: value},
                    }
                )
                progress = provider_progress_for_run(damaged)
                self.assertEqual(progress["state"], "identity_mismatch")
                self.assertNotEqual(progress.get("admission"), "accepted")

    def test_launch_intent_handoff_keeps_the_real_bound_source_for_worker_and_reviewer(self) -> None:
        """The later launcher result may add pane/lifecycle facts but cannot restore `unbound`."""

        class Runtime:
            def save_records(self, _payload, _records) -> None:
                return None

        for intent_role, run_role in ((WORKER_ROLE, "worker"), (REVIEW_ROLE, "reviewer")):
            with self.subTest(role=intent_role):
                self.source.unlink(missing_ok=True)
                preflight = self._preflight_run(run_id=f"{intent_role}-run", role=run_role)
                self._write_source()
                ingress = self._ingress(preflight)
                ingress.bind_before_delivery()
                bound = ingress.run
                stale = preflight.rebound(f"term-{intent_role}", leaf=f"leaf-{intent_role}").working()
                record = DispatcherRecord(
                    worker="worker-1",
                    workspace=str(self.workspace),
                    handle="",
                    head="codex-extra",
                    review_head="codex-extra",
                    attempt_id="attempt-1",
                    comment_baseline=0,
                    review_baseline=0,
                    state="claimed",
                    claimed_at=0.0,
                )
                record.launch_intent = {"role": intent_role, "head_run": bound.to_json()}
                if intent_role == REVIEW_ROLE:
                    record.review_head_run = bound.to_json()
                else:
                    record.worker_head_run = bound.to_json()
                records = {"secretary-1428": record}

                confirm_launch_intent(
                    Runtime(),
                    {},
                    records,
                    "secretary-1428",
                    record,
                    handle=stale.handle,
                    leaf=stale.leaf,
                    head_run=stale.to_json(),
                )

                stored = record.review_head_run if intent_role == REVIEW_ROLE else record.worker_head_run
                source = stored["fanout_policy"]["provider_source"]
                self.assertEqual(source["state"], "bound")
                self.assertEqual(source["session_id"], "session-1")
                self.assertEqual(stored["lifecycle"], "working")
                self.assertEqual(stored["handle"], stale.handle)
                self.assertEqual(record.launch_intent["head_run"], stored)

    def test_launch_intent_handoff_refuses_a_conflicting_bound_source(self) -> None:
        class Runtime:
            def save_records(self, _payload, _records) -> None:
                return None

        preflight = self._preflight_run()
        self._write_source()
        ingress = self._ingress(preflight)
        ingress.bind_before_delivery()
        bound = ingress.run
        conflicting_source = {
            **bound.fanout_policy["provider_source"],
            "session_id": "foreign-session",
        }
        conflicting = bound.with_fanout_policy(
            {
                **bound.fanout_policy,
                "provider_source": conflicting_source,
            }
        )
        record = DispatcherRecord(
            worker="worker-1",
            workspace=str(self.workspace),
            handle="",
            head="codex-extra",
            review_head="codex-extra",
            attempt_id="attempt-1",
            comment_baseline=0,
            review_baseline=0,
            state="claimed",
            claimed_at=0.0,
        )
        record.launch_intent = {"role": WORKER_ROLE, "head_run": bound.to_json()}
        record.worker_head_run = bound.to_json()
        before = record.launch_intent["head_run"]

        with self.assertRaisesRegex(HostError, "bound provider sources conflict"):
            confirm_launch_intent(
                Runtime(),
                {},
                {"secretary-1428": record},
                "secretary-1428",
                record,
                head_run=conflicting.to_json(),
            )

        self.assertEqual(record.launch_intent["head_run"], before)

    def test_observer_writer_keeps_the_bound_delivery_handoff_over_a_stale_launch_copy(self) -> None:
        class State:
            def save(self, _payload) -> None:
                return None

        class Host:
            def configure_codex_provider_ingress(self, _run, *, persist, stop, block) -> None:
                self.persist = persist

        class Runtime:
            def __init__(self) -> None:
                self.host = Host()
                self.production_state = State()

        self.source.unlink(missing_ok=True)
        preflight = self._preflight_run(run_id="observer-run", role="observer")
        self._write_source()
        ingress = self._ingress(preflight)
        ingress.bind_before_delivery()
        bound = ingress.run
        stale = preflight.rebound("term-observer", leaf="leaf-observer")
        record = ObserverRecord(
            sprint="sprint:1",
            workspace=str(self.workspace),
            head="codex-extra",
            head_run=bound.to_json(),
        )
        runtime = Runtime()
        observers = {"sprint:1": record}

        _bind_codex_provider_ingress(runtime, {}, observers, "sprint:1", record)
        runtime.host.persist(stale)

        source = record.head_run["fanout_policy"]["provider_source"]
        self.assertEqual(source["state"], "bound")
        self.assertEqual(source["session_id"], "session-1")
        self.assertEqual(record.handle, "term-observer")
        self.assertEqual(observers["sprint:1"].head_run, record.head_run)

    def test_collaboration_call_is_persisted_as_advisory_telemetry(self) -> None:
        self._write_source()
        ingress = self._ingress()
        ingress.bind_before_delivery()
        self._write_source(
            {
                "type": "item.completed",
                "item": {"type": "collab_tool_call", "tool": "spawn_agent", "sender_thread_id": "parent-1"},
            }
        )

        ingress.poll()

        recorded = self.written[-1]
        event = recorded.fanout_policy["events"][-1]
        self.assertEqual(event["type"], "collaboration_call")
        raw_line = self.source.read_text(encoding="utf-8").splitlines()[-1]
        self.assertEqual(event["raw_event_digest"], hashlib.sha256(raw_line.encode("utf-8")).hexdigest())
        self.assertEqual(recorded.fanout_policy["terminal_state"], "violation")
        self.assertEqual(self.stops, [])
        self.assertEqual(self.blocks, [])

    def test_retained_tui_tool_only_envelope_reaches_the_same_recorder(self) -> None:
        self._write_source()
        ingress = self._ingress()
        ingress.bind_before_delivery()
        self._write_source(
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "thread_id": "parent-1",
                    "item": {
                        "type": "CollabAgentToolCall",
                        "tool": "wait",
                        "sender_thread_id": "parent-1",
                        "receiver_thread_ids": [],
                    },
                },
            }
        )

        ingress.poll()

        event = self.written[-1].fanout_policy["events"][-1]
        self.assertEqual(event["type"], "collaboration_call")
        self.assertEqual(event["parent_thread_id"], "parent-1")
        self.assertEqual(event["tool_name"], "wait")
        self.assertEqual(self.stops, [])
        self.assertEqual(self.blocks, [])

    def test_child_edge_unknown_thread_and_malformed_line_are_typed(self) -> None:
        cases = (
            (
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "item_completed",
                        "thread_id": "parent-1",
                        "item": {
                            "type": "CollabAgentToolCall",
                            "tool": "spawn_agent",
                            "sender_thread_id": "parent-1",
                            "receiver_thread_ids": ["child-1"],
                        },
                    },
                },
                "child_thread_edge",
                "violation",
            ),
            ({"type": "thread.started", "thread_id": "foreign-thread"}, "unknown_thread_edge", "unknown"),
            ("{not-json", "unparseable_provider_event", "unknown"),
        )
        for event, expected_type, expected_state in cases:
            with self.subTest(expected_type=expected_type):
                self.written.clear()
                self.stops.clear()
                self.blocks.clear()
                self._write_source()
                ingress = self._ingress()
                ingress.bind_before_delivery()
                self._write_source(event)

                ingress.poll()

                self.assertEqual(self.written[-1].fanout_policy["events"][-1]["type"], expected_type)
                self.assertEqual(self.written[-1].fanout_policy["terminal_state"], expected_state)
                self.assertEqual(self.stops, [])
                self.assertEqual(self.blocks, [])

    def test_recovery_cursor_mismatch_is_non_fatal_diagnostic(self) -> None:
        self._write_source()
        ingress = self._ingress()
        ingress.bind_before_delivery()
        # The durable parent cursor no longer names the same raw line.  It is not a reason to
        # redirect the record to some later session or regenerate an identity.
        self.source.write_text(
            json.dumps(
                {"type": "session_meta", "payload": {"session_id": "session-1", "cwd": str(self.workspace)}}
            )
            + "\n"
            + json.dumps({"type": "thread.started", "thread_id": "changed-parent"})
            + "\n",
            encoding="utf-8",
        )

        ingress.poll()

        self.assertEqual(self.stops, [])
        self.assertEqual(self.blocks, [])
        self.assertEqual(ingress.run.fanout_policy["terminal_state"], "unknown")

    def test_clean_ordinary_prebind_lines_are_durably_advanced_before_delivery(self) -> None:
        self._write_source(
            {"type": "event_msg", "payload": {"type": "agent_message", "thread_id": "parent-1"}},
            {"type": "turn.completed", "thread_id": "parent-1"},
        )
        ingress = self._ingress()

        ingress.bind_before_delivery()

        source = self.written[-1].fanout_policy["provider_source"]
        self.assertEqual(source["cursor"]["line"], 4)
        self.assertEqual(self.written[-1].fanout_policy["events"], [])
        self.assertEqual(self.stops, [])
        self.assertEqual(self.blocks, [])

    def test_malformed_post_root_prebind_line_is_advisory_telemetry(self) -> None:
        self._write_source("{not-json")
        ingress = self._ingress()

        ingress.bind_before_delivery()

        self.assertEqual(self.written[0].fanout_policy["provider_source"]["cursor"]["line"], 0)
        event = self.written[-1].fanout_policy["events"][-1]
        self.assertEqual(event["type"], "unparseable_provider_event")
        self.assertEqual(event["source_sequence"], 3)
        self.assertEqual(self.stops, [])
        self.assertEqual(self.blocks, [])

    def test_tui_policy_event_in_post_root_prebind_tail_is_advisory(self) -> None:
        self._write_source(
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "thread_id": "parent-1",
                    "item": {
                        "type": "CollabAgentToolCall",
                        "tool": "spawn_agent",
                        "sender_thread_id": "parent-1",
                        "receiver_thread_ids": ["child-1"],
                    },
                },
            }
        )
        ingress = self._ingress()

        ingress.bind_before_delivery()

        self.assertEqual(self.written[0].fanout_policy["provider_source"]["cursor"]["line"], 0)
        event = self.written[-1].fanout_policy["events"][-1]
        self.assertEqual(event["type"], "child_thread_edge")
        self.assertEqual(event["child_thread_id"], "child-1")
        self.assertEqual(self.stops, [])
        self.assertEqual(self.blocks, [])

    def test_unrecognised_collaboration_shape_is_unknown_not_a_clean_cursor_advance(self) -> None:
        self._write_source(
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "thread_id": "parent-1",
                    "item": {"type": "CollabAgentStatus", "sender_thread_id": "parent-1"},
                },
            }
        )
        ingress = self._ingress()

        ingress.bind_before_delivery()

        self.assertEqual(self.written[-1].fanout_policy["events"][-1]["type"], "unknown_thread_edge")
        self.assertEqual(self.written[-1].fanout_policy["terminal_state"], "unknown")

    def test_malformed_pre_root_line_is_recorded_from_the_selected_source_range(self) -> None:
        self._write_records(
            {"type": "session_meta", "payload": {"session_id": "session-1", "cwd": str(self.workspace)}},
            "{partially-written",
            {"type": "thread.started", "thread_id": "parent-1"},
        )
        ingress = self._ingress()

        ingress.bind_before_delivery()

        selected = self.written[0].fanout_policy["provider_source"]
        self.assertEqual(selected["cursor"]["line"], 0)
        self.assertEqual(selected["initial_range"]["first"]["line"], 1)
        self.assertEqual(selected["initial_range"]["root"]["line"], 3)
        self.assertEqual(selected["initial_range"]["last"]["line"], 3)
        event = self.written[-1].fanout_policy["events"][-1]
        self.assertEqual(event["type"], "unparseable_provider_event")
        self.assertEqual(event["source_sequence"], 2)
        self.assertEqual(self.stops, [])
        self.assertEqual(self.blocks, [])

    def test_clean_recognised_pre_root_records_cross_the_root_in_one_scanner(self) -> None:
        self._write_records(
            {"type": "session_meta", "payload": {"session_id": "session-1", "cwd": str(self.workspace)}},
            {"type": "event_msg", "payload": {"type": "agent_message"}},
            {"type": "thread.started", "thread_id": "parent-1"},
            {"type": "turn.completed", "thread_id": "parent-1"},
        )
        ingress = self._ingress()

        ingress.bind_before_delivery()

        self.assertEqual(
            [run.fanout_policy["provider_source"]["cursor"]["line"] for run in self.written],
            # The binding, then one advisory cursor write for the whole scan (ummanu-131).
            [0, 4],
        )
        self.assertEqual(self.written[-1].fanout_policy["events"], [])
        self.assertEqual(self.stops, [])
        self.assertEqual(self.blocks, [])

    def test_policy_pre_root_record_is_telemetry_after_source_selection(self) -> None:
        self._write_records(
            {"type": "session_meta", "payload": {"session_id": "session-1", "cwd": str(self.workspace)}},
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "CollabAgentToolCall",
                        "tool": "spawn_agent",
                        "sender_thread_id": "parent-1",
                        "receiver_thread_ids": ["child-1"],
                    },
                },
            },
            {"type": "thread.started", "thread_id": "parent-1"},
        )
        ingress = self._ingress()

        ingress.bind_before_delivery()

        selected = self.written[0].fanout_policy["provider_source"]
        self.assertEqual(selected["initial_range"]["root"]["line"], 3)
        event = self.written[-1].fanout_policy["events"][-1]
        self.assertEqual(event["type"], "child_thread_edge")
        self.assertEqual(event["source_sequence"], 2)
        self.assertEqual(event["child_thread_id"], "child-1")
        self.assertEqual(self.stops, [])
        self.assertEqual(self.blocks, [])

    def test_recovery_reuses_the_persisted_full_range_before_later_events(self) -> None:
        self._write_records(
            {"type": "session_meta", "payload": {"session_id": "session-1", "cwd": str(self.workspace)}},
            {"type": "event_msg", "payload": {"type": "agent_message"}},
            {"type": "thread.started", "thread_id": "parent-1"},
            {"type": "turn.completed", "thread_id": "parent-1"},
        )
        ingress = self._ingress()
        ingress.bind_before_delivery()
        persisted = HeadRun.from_json(self.written[-1].to_json())
        writes_after_bind = len(self.written)
        recovered = self._ingress(persisted)

        recovered.poll()

        self.assertEqual(len(self.written), writes_after_bind)
        self._append_records("{not-json")
        recovered.poll()

        event = self.written[-1].fanout_policy["events"][-1]
        self.assertEqual(event["type"], "unparseable_provider_event")
        self.assertEqual(event["source_sequence"], 5)
        self.assertEqual(self.stops, [])
        self.assertEqual(self.blocks, [])

    def test_recovery_fences_a_changed_record_inside_the_persisted_initial_range(self) -> None:
        self._write_records(
            {"type": "session_meta", "payload": {"session_id": "session-1", "cwd": str(self.workspace)}},
            {"type": "event_msg", "payload": {"type": "agent_message"}},
            {"type": "thread.started", "thread_id": "parent-1"},
            {"type": "turn.completed", "thread_id": "parent-1"},
        )
        ingress = self._ingress()
        ingress.bind_before_delivery()
        persisted = HeadRun.from_json(self.written[-1].to_json())
        self._write_records(
            {"type": "session_meta", "payload": {"session_id": "session-1", "cwd": str(self.workspace)}},
            {"type": "event_msg", "payload": {"type": "agent_message_rewritten"}},
            {"type": "thread.started", "thread_id": "parent-1"},
            {"type": "turn.completed", "thread_id": "parent-1"},
        )

        recovered = self._ingress(persisted)
        recovered.poll()

        self.assertEqual(self.stops, [])
        self.assertEqual(self.blocks, [])
        self.assertEqual(recovered.run.fanout_policy["terminal_state"], "unknown")

    def test_prebind_cursor_persistence_failure_is_non_fatal(self) -> None:
        self._write_source({"type": "turn.completed", "thread_id": "parent-1"})

        def fail_after_binding(run: HeadRun) -> None:
            self.written.append(run)
            if len(self.written) > 1:
                raise OSError("disk full")

        ingress = CodexProviderEventIngress(
            self._run(),
            fail_after_binding,
            stop=lambda current, reason: self.stops.append((current.run_id, reason)),
            block=self.blocks.append,
        )

        ingress.bind_before_delivery()

        self.assertEqual(self.written[0].fanout_policy["provider_source"]["state"], "bound")
        self.assertEqual(self.written[0].fanout_policy["provider_source"]["cursor"]["line"], 0)
        self.assertEqual(self.stops, [])
        self.assertEqual(self.blocks, [])

    def test_failed_durable_event_write_is_non_fatal(self) -> None:
        self._write_source()
        ingress = self._ingress()
        ingress.bind_before_delivery()
        self._write_source(
            {
                "type": "item.completed",
                "item": {"type": "collab_tool_call", "tool": "spawn_agent", "sender_thread_id": "parent-1"},
            }
        )

        def fail(_run: HeadRun) -> None:
            raise OSError("disk full")

        ingress.persist = fail
        ingress.poll()

        self.assertEqual(self.stops, [])
        self.assertEqual(self.blocks, [])
        self.assertEqual(ingress.run.fanout_policy["events"], [])


class PreparedProviderSourceLaunchHandoffTests(unittest.TestCase):
    """One launch preflights twice; only one of the two descriptors may reach the handoff.

    A bring-up is attested once to fix its intent on disk and once inside the host call that opens
    the pane. Both attestations enumerate the Codex session root, so any head that opened a session
    in between makes the second baseline legitimately differ from the first. `secretary-1445` hit
    exactly that on an approved rework: its relaunch never reached a worker because the two unbound
    descriptors for its one `HeadRun` were refused as a provider-source conflict.
    """

    class Catalog:
        def __init__(self, home: Path) -> None:
            self.home = home

        def head_profile(self, _head: str) -> dict[str, str]:
            return {
                "adapter": "codex",
                "model": "gpt-5.6-terra",
                "effort": "medium",
                "codex_mode": "tui",
                "codex_home": str(self.home),
            }

    class Runtime:
        def save_records(self, _payload: dict, _records: dict) -> None:
            return None

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.sessions = self.root / "sessions" / "2026" / "08" / "15"
        self.sessions.mkdir(parents=True)
        self.host = CommandHostRuntime(self.Catalog(self.root), self.root / "data", mode="noop")  # type: ignore[arg-type]
        self.persisted: list[HeadRun] = []

    @property
    def _launch_identity(self) -> dict[str, object]:
        return {
            "role": WORKER_ROLE,
            "workspace": str(self.workspace),
            "task_ref": TaskRef.card("secretary-1445", document=str(self.workspace / "TASK.md")),
            "pid_file": str(self.root / "worker.pid"),
            "run_id": "rework-run-1",
        }

    def _prepare_intent_run(self) -> HeadRun:
        """The pre-pane attestation `write_launch_intent` fixes on disk, with its ingress bound."""
        run = self.host.preflight_codex_run("codex-extra", **self._launch_identity)  # type: ignore[arg-type]
        self._install_ingress(run)
        return run

    def _install_ingress(self, run: HeadRun) -> None:
        self.host.configure_codex_provider_ingress(
            run,
            persist=self.persisted.append,
            stop=lambda _run, _reason: None,
            block=lambda _evidence: None,
        )

    def _open_another_session(self, name: str = "another-head.jsonl") -> Path:
        """A session journal a different head opened after the intent was written."""
        path = self.sessions / name
        path.write_text(
            json.dumps({"type": "session_meta", "payload": {"session_id": "other", "cwd": "/elsewhere"}})
            + "\n",
            encoding="utf-8",
        )
        return path

    def _record(self, prepared: HeadRun) -> DispatcherRecord:
        record = DispatcherRecord(
            worker="worker-1",
            workspace=str(self.workspace),
            handle="",
            head="codex-extra",
            review_head="codex-extra",
            attempt_id="attempt-1",
            comment_baseline=0,
            review_baseline=0,
            state="claimed",
            claimed_at=0.0,
        )
        record.launch_intent = {
            "role": WORKER_ROLE,
            "head_run": prepared.to_json(),
            "run_id": prepared.run_id,
        }
        record.worker_head_run = prepared.to_json()
        return record

    def _confirm(self, record: DispatcherRecord, launched: HeadRun) -> dict:
        confirm_launch_intent(
            self.Runtime(),
            {},
            {"secretary-1445": record},
            "secretary-1445",
            record,
            handle=launched.handle,
            leaf=launched.leaf,
            head_run=launched.to_json(),
        )
        return record.worker_head_run

    @staticmethod
    def _source(run: HeadRun | dict) -> dict:
        policy = run["fanout_policy"] if isinstance(run, dict) else run.fanout_policy
        return dict(policy["provider_source"])

    def test_a_rework_relaunch_keeps_the_source_its_intent_was_prepared_with(self) -> None:
        prepared = self._prepare_intent_run()
        record = self._record(prepared)
        self._open_another_session()

        launched = (
            self.host._preflight_launch_run(
                "codex-extra",
                **self._launch_identity,  # type: ignore[arg-type]
            )
            .rebound("pane-1", leaf="leaf-1")
            .working()
        )
        stored = self._confirm(record, launched)

        source = self._source(stored)
        self.assertEqual(source, self._source(prepared))
        self.assertEqual(source["state"], "unbound")
        self.assertEqual(stored["run_id"], prepared.run_id, "a retained run is never re-minted")
        self.assertEqual(stored["handle"], "pane-1")
        self.assertEqual(stored["lifecycle"], "working")
        self.assertEqual(record.launch_intent["head_run"], stored)

    def test_the_worker_bring_up_itself_hands_on_the_prepared_source(self) -> None:
        """Which host call routes through the repair, read off the launch that opens the pane."""
        reference = "secretary-1445"
        pid_file = dispatcher_launch.launch_pid_file(WORKER_ROLE, reference)
        identity = {
            **self._launch_identity,
            "pid_file": pid_file,
            "run_id": "rework-run-2",
        }
        prepared = self.host.preflight_codex_run("codex-extra", **identity)  # type: ignore[arg-type]
        self._install_ingress(prepared)
        self._open_another_session()
        (self.workspace / "TASK.md").write_text("rework instruction\n", encoding="utf-8")

        launched = self.host._launch(
            str(self.workspace),
            f"{reference} worker rework",
            "codex-extra",
            "TASK.md",
            role=WORKER_ROLE,
            env_name="UMMANU_DISPATCHER_WORKER_COMMAND",
            prompt_document=str(self.workspace / "TASK.md"),
            task={"ref": reference},
            heartbeat_run_id="rework-run-2",
        )

        self.assertEqual(self._source(launched.head_run), self._source(prepared))

    def test_an_observer_bring_up_still_enumerates_its_own_source(self) -> None:
        """The observer keeps its own preflight: this repair is the worker/reviewer handoff only."""
        sprint = "sprint:1089"
        workspace = Path(self.host.observer_workspace(sprint))
        prepared = self.host.preflight_codex_run(
            "codex-extra",
            role=OBSERVER_ROLE,
            workspace=str(workspace),
            task_ref=TaskRef.sprint(sprint),
            pid_file=dispatcher_observer.observer_pid_file(sprint),
            run_id="observer-run-1",
        )
        self._install_ingress(prepared)
        opened = self._open_another_session()

        launched = self.host.prepare_observer(
            {"ref": sprint},
            "codex-extra",
            prompt="observe",
            heartbeat_run_id="observer-run-1",
        )

        source = self._source(launched["head_run"])
        self.assertEqual(source["baseline"], [str(opened)])
        self.assertNotEqual(source, self._source(prepared))
        self.assertEqual(self._source(prepared)["baseline"], [])

    def test_the_second_attestations_own_baseline_is_still_refused_as_a_conflict(self) -> None:
        """The fence the repair works within: nothing admits a divergent unbound descriptor."""
        prepared = self._prepare_intent_run()
        record = self._record(prepared)
        self._open_another_session()

        unprepared = self.host.preflight_codex_run("codex-extra", **self._launch_identity)  # type: ignore[arg-type]
        self.assertNotEqual(
            self._source(unprepared)["baseline"],
            self._source(prepared)["baseline"],
        )

        with self.assertRaisesRegex(HostError, "preflight provider source conflicts"):
            self._confirm(record, unprepared.rebound("pane-1", leaf="leaf-1").working())

        self.assertEqual(record.worker_head_run, prepared.to_json())

    def _bind_this_runs_session(self, prepared: HeadRun) -> tuple[HeadRun, Path]:
        """Let the pane's own journal appear and bind it through this run's ingress."""
        journal = self.sessions / "worker.jsonl"
        journal.write_text(
            "\n".join(
                json.dumps(value)
                for value in (
                    {
                        "type": "session_meta",
                        "payload": {"session_id": "session-1", "cwd": str(self.workspace)},
                    },
                    {"type": "thread.started", "thread_id": "parent-1"},
                )
            )
            + "\n",
            encoding="utf-8",
        )
        ingress = self.host._codex_provider_ingresses[prepared.run_id]
        ingress.bind_before_delivery()
        return ingress.run, journal

    def test_a_source_already_bound_to_its_session_is_not_replaced_by_a_new_attestation(self) -> None:
        prepared = self._prepare_intent_run()
        record = self._record(prepared)
        bound_run, journal = self._bind_this_runs_session(prepared)
        record.worker_head_run = bound_run.to_json()
        record.launch_intent["head_run"] = bound_run.to_json()
        self._open_another_session()

        launched = (
            self.host._preflight_launch_run(
                "codex-extra",
                **self._launch_identity,  # type: ignore[arg-type]
            )
            .rebound("pane-1", leaf="leaf-1")
            .working()
        )

        self.assertEqual(self._source(launched), self._source(bound_run))
        stored = self._confirm(record, launched)
        source = self._source(stored)
        self.assertEqual(source["state"], "bound")
        self.assertEqual(source["session_id"], "session-1")
        self.assertEqual(source["path"], str(journal))
        self.assertEqual(source["cursor"], self._source(bound_run)["cursor"])

    def test_a_prepared_source_from_another_session_root_is_never_retained(self) -> None:
        prepared = self._prepare_intent_run()
        foreign_root = self.root / "other-home" / "sessions"
        foreign_root.mkdir(parents=True)
        foreign = prepared.with_fanout_policy(
            {
                **prepared.fanout_policy,
                "provider_source": {**self._source(prepared), "root": str(foreign_root)},
            }
        )
        self._install_ingress(foreign)
        record = self._record(foreign)
        self._open_another_session()

        launched = self.host._preflight_launch_run(
            "codex-extra",
            **self._launch_identity,  # type: ignore[arg-type]
        )

        self.assertEqual(self._source(launched)["root"], str(self.root / "sessions"))
        with self.assertRaisesRegex(HostError, "preflight provider source conflicts"):
            self._confirm(record, launched.rebound("pane-1", leaf="leaf-1").working())

    def test_a_prepared_run_naming_another_head_is_never_retained(self) -> None:
        prepared = self._prepare_intent_run()
        baseline = self._source(prepared)["baseline"]
        self._open_another_session()
        foreign_ref = TaskRef.card("secretary-1444", document=str(self.workspace / "TASK.md"))
        for field, value in (
            ("workspace", str(self.root / "other-workspace")),
            ("role", REVIEW_ROLE),
            ("task_ref", foreign_ref),
            ("pid_file", str(self.root / "review.pid")),
            ("spec", HeadSpec(profile_id="codex-other", adapter="codex", model="gpt-5.6-terra")),
        ):
            with self.subTest(field=field):
                self._install_ingress(
                    prepared.__class__(
                        **{
                            **{
                                name: getattr(prepared, name)
                                for name in (
                                    "run_id",
                                    "spec",
                                    "workspace",
                                    "task_ref",
                                    "role",
                                    "pid_file",
                                    "fanout_policy",
                                )
                            },
                            field: value,
                        }
                    )
                )

                launched = self.host._preflight_launch_run(
                    "codex-extra",
                    **self._launch_identity,  # type: ignore[arg-type]
                )

                self.assertNotEqual(self._source(launched)["baseline"], baseline)

    def test_two_bound_writers_disagreeing_at_one_cursor_line_stay_refused(self) -> None:
        prepared = self._prepare_intent_run()
        bound_run, _journal = self._bind_this_runs_session(prepared)
        source = self._source(bound_run)
        conflicting = bound_run.with_fanout_policy(
            {
                **bound_run.fanout_policy,
                "provider_source": {
                    **source,
                    # A well-formed digest for the same line: the fence is the disagreement itself,
                    # not a malformed record.
                    "cursor": {"line": source["cursor"]["line"], "digest": "f" * 64},
                },
            }
        )

        with self.assertRaisesRegex(HostError, "cursor conflicts"):
            dispatcher_launch.merge_launch_head_run(bound_run.to_json(), conflicting.to_json())


class ProductionPostDeliveryHandoffContractTests(unittest.TestCase):
    """Drive the dispatcher routes that own the post-delivery write, not their merge helpers."""

    class SupervisedHeads:
        """The `local-pty` backend's delivery contour, as far as this contract reaches.

        A prompt is put in front of a head once it is ready — the moment its provider journal
        appears — and the dispatcher's pre-send hook runs after admission and before the first byte,
        exactly where `LocalPtyHeadRuntime._before_send` runs it. The run that hook binds is merged
        into the receipt the way that backend merges it.
        """

        def __init__(self, on_ready) -> None:
            self.on_ready = on_ready
            self.sent: list[object] = []

        def _delivered(self, run: HeadRun, pointer, transport) -> HeadRun:
            self.on_ready()
            hook = getattr(transport, "before_send", None)
            handed = hook() if hook is not None else None
            if isinstance(handed, HeadRun):
                run = head_ops.post_delivery_run(run, handed)
            self.sent.append(pointer)
            return run

        def start(self, spec, workspace, task_ref, *, run, pointer=None, transport=None, **_ignored):
            live = run.rebound(f"run:{run.run_id}", leaf=f"leaf:{run.run_id}")
            if pointer is None or transport is None:
                return StartReceipt(status=HEAD_OK, run=live)
            return StartReceipt(status=HEAD_OK, run=self._delivered(live, pointer, transport).working())

        def deliver(self, run, pointer, *, transport=None, subject="", **_ignored):
            return DeliverReceipt(status=HEAD_OK, run=self._delivered(run, pointer, transport))

    class Catalog:
        def __init__(self, fixture: ProductionPostDeliveryHandoffContractTests) -> None:
            self.fixture = fixture

        def head_profile(self, _head: str) -> dict[str, str]:
            return {
                "adapter": "codex",
                "model": "gpt-5.6-terra",
                "effort": "medium",
                "codex_mode": "tui",
                "codex_home": str(self.fixture.root),
                "runtime": LOCAL_PTY_RUNTIME,
            }

        def head_launch(self, _head: str, _prompt_file: str, **_kwargs) -> HeadCommand:
            return HeadCommand("run-codex", prompt_after_start=True, adapter="codex")

        def project_default_branch(self, project: str) -> str:
            return "main"

        def binding(self, project: str) -> dict[str, str]:
            return {"repo": str(self.fixture.repo)}

        def integration_base(self, project: str, override: str | None) -> str:
            return resolve_integration_base(default_branch="main", declared=None, override=override)

        def workspace_seed(self, project: str, task: dict) -> str:
            workspace = task.get("workspace") or {}
            seed = str(workspace.get("seed_ref") or "")
            return seed or self.integration_base(project, workspace.get("base_branch"))

        def broad_check_verdict(self, _project: str) -> ContractVerdict:
            # The worker task packet resolves the project's broad-check contract to print an exact
            # command (issue:8b39e60e4df361c6138e). Ummanu's own adapter declares one, and an
            # adapter that declares none is refused by name now, so a `fit` here is a declared
            # contract; this stub answers the same shape a real catalog does.
            return ContractVerdict.as_fit(
                ModuleContract(sys.executable, "ummanu", module="tests.broad"),
                "ummanu",
            )

        def head_run(self, task: dict, *, role: str, head: str, workspace: str, **_kwargs) -> HeadRun:
            return HeadRun(
                run_id=f"routing-{role}",
                spec=HeadSpec(profile_id=head, adapter="codex", model="gpt-5.6-terra"),
                workspace=workspace,
                task_ref=TaskRef.card(str(task["ref"])),
                role=role,
            )

        def observer_run(self, head: str, *, workspace: str):
            return HeadRun(
                run_id="observer-routing",
                spec=HeadSpec(profile_id=head, adapter="codex", model="gpt-5.6-terra"),
                workspace=workspace,
                task_ref=TaskRef.sprint("sprint:contract"),
                role=OBSERVER_ROLE,
            )

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        workspace_python = self.workspace / ".ummanu-task-env" / "venv" / "bin" / "python3"
        workspace_python.parent.mkdir(parents=True)
        workspace_python.symlink_to(sys.executable)
        (self.workspace / ".ummanu-task-env" / "owner.json").write_text(
            json.dumps(
                {
                    "owner": "ummanu-dispatcher",
                    "schema_version": 1,
                    "workspace": str(self.workspace.resolve()),
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        (self.workspace / ".ummanu-task-env" / "ready").write_text("ready\n", encoding="utf-8")
        self.repo = self.root / "repo"
        (self.repo / ".git").mkdir(parents=True)
        self.binary = self.root / "codex"
        self.binary.write_text("#!/bin/sh\nprintf 'codex 9.9.9\\n'\n", encoding="utf-8")
        self.binary.chmod(0o755)
        self.sessions = self.root / "sessions"
        self.source = self.sessions / "2026" / "08" / "13" / "run.jsonl"
        self.source.parent.mkdir(parents=True)
        self.snapshots: list[dict[str, dict]] = []
        self.moves: list[dict] = []
        self.source_emitted = False
        self.source_events: list[object] = []
        self.session = self.SupervisedHeads(self._emit_source)
        self.host = CommandHostRuntime(  # type: ignore[arg-type]
            self.Catalog(self),
            self.root / "data",
            mode="real",
            production_runtime=registered_production_runtime(self.root),
            # The card has no history yet: what its TASK.md is rendered against is an audit that
            # holds none. The audit itself is not what this contract is about.
            audit=SimpleNamespace(events=lambda *_args, **_kwargs: []),
        )
        self.host.preflight_codex_run = self._real_preflight  # type: ignore[method-assign]
        self.host._run = self._run  # type: ignore[method-assign]
        self.host._head_runtimes[LOCAL_PTY_RUNTIME] = self.session
        # The observer's git worktree is not what this contract is about.
        self.host._create_git_observer_workspace = lambda workspace: (  # type: ignore[method-assign]
            workspace.mkdir(parents=True, exist_ok=True) or workspace
        )
        self.env = mock.patch.dict(
            os.environ,
            {
                "UMMANU_DISPATCHER_BODY_DIR": str(self.root / "bodies"),
                "UMMANU_DISPATCHER_PROMPT_DIR": str(self.root / "prompts"),
                "UMMANU_DISPATCHER_WORKSPACES_ROOT": str(self.root / "workspaces"),
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def _run(self, args: list[str], _label: str, **_kwargs) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args, 0, stdout="contract-sha\n", stderr="")

    def _real_preflight(
        self,
        head: str,
        *,
        role: str,
        workspace: str,
        task_ref: TaskRef,
        pid_file: str,
        run_id: str,
    ) -> HeadRun:
        run = HeadRun(
            run_id=run_id,
            spec=HeadSpec(profile_id=head, adapter="codex", model="gpt-5.6-terra", runtime=LOCAL_PTY_RUNTIME),
            workspace=workspace,
            task_ref=task_ref,
            role=role,
            pid_file=pid_file,
        )
        # The production path has no independently captured provider schema.  Advisory fan-out
        # telemetry must therefore carry ``schema_absent`` through all three real launch routes.
        return codex_preflight.preflight_codex_launch(
            {"codex_home": str(self.root)},
            workspace,
            run,
            binary_path=str(self.binary),
            config=self.root / "config.toml",
        )

    def _emit_source(self) -> None:
        if self.source_emitted:
            return
        self.source_emitted = True
        self._write_records(
            {"type": "session_meta", "payload": {"session_id": "session-1", "cwd": self.source_workspace}},
            {"type": "thread.started", "thread_id": "parent-1"},
            *self.source_events,
        )

    @staticmethod
    def _fail_recorder(recorder: codex_preflight.CodexProviderEventRecorder, *_args, **_kwargs):
        raise codex_preflight.CodexFanoutRecordingError(
            "recorder storage unavailable",
            run=recorder.run,
            event={},
        )

    def _add_recording_failure_event(self) -> None:
        self.source_events = [
            {
                "type": "event_msg",
                "payload": {
                    "type": "collab_tool_call",
                    "item": {
                        "type": "collab_tool_call",
                        "tool": "spawn_agent",
                        "sender_thread_id": "parent-1",
                        "receiver_thread_ids": [],
                    },
                },
            }
        ]

    def _write_records(self, *records: object) -> None:
        self.source.write_text(
            "\n".join(value if isinstance(value, str) else json.dumps(value) for value in records) + "\n",
            encoding="utf-8",
        )

    def _append_records(self, *records: object) -> None:
        self.source.write_text(
            self.source.read_text(encoding="utf-8")
            + "\n".join(value if isinstance(value, str) else json.dumps(value) for value in records)
            + "\n",
            encoding="utf-8",
        )

    @property
    def source_workspace(self) -> str:
        return str(getattr(self, "_source_workspace", self.workspace))

    def _arm_source(self, workspace: str) -> None:
        self._source_workspace = workspace
        self.source_emitted = False
        self.source.unlink(missing_ok=True)

    def _runtime(self):
        runtime = object.__new__(DispatcherRuntime)
        runtime.host = self.host
        runtime.owner = "contract-dispatcher"
        runtime.writer = SimpleNamespace(move=lambda **kwargs: self.moves.append(dict(kwargs)))
        runtime.head_readiness = lambda _head: HeadReadiness("test", "ready", "", 0.0)
        runtime.record_worker_routing = lambda *_args: None
        runtime.record_review_routing = lambda *_args: None
        runtime.open_worker_round = lambda *_args, **_kwargs: None

        def save(_payload: dict, records: dict[str, DispatcherRecord]) -> None:
            self.snapshots.append({ref: copy.deepcopy(record.to_json()) for ref, record in records.items()})

        runtime.save_records = save
        return runtime

    @staticmethod
    def _record(workspace: str) -> DispatcherRecord:
        return DispatcherRecord(
            worker="contract-worker",
            workspace=workspace,
            handle="",
            head="codex-contract",
            review_head="codex-contract",
            attempt_id="attempt-contract",
            comment_baseline=0,
            review_baseline=0,
            state="claimed",
            claimed_at=0.0,
        )

    def _assert_bound(self, run: dict, *, role: str) -> None:
        self.assertEqual(run["fanout_policy"]["state"], codex_preflight.FANOUT_SCHEMA_ABSENT)
        source = run["fanout_policy"]["provider_source"]
        self.assertEqual(source["state"], "bound")
        self.assertEqual(source["session_id"], "session-1")
        self.assertEqual(source["role"], role)
        self.assertEqual(source["path"], str(self.source))
        self.assertEqual(source["run_id"], run["run_id"])
        self.assertEqual(source["task_ref"], run["task_ref"])

    def test_worker_route_persists_the_callback_run_before_confirm_then_adopts_and_refreshes(self) -> None:
        runtime = self._runtime()
        task = {"ref": "secretary-1428", "project": "ummanu", "workspace": {"base_branch": "main"}}
        record = self._record(str(self.workspace))
        records = {task["ref"]: record}
        payload: dict = {}
        self._arm_source(record.workspace)
        self._add_recording_failure_event()

        self.assertIsNone(
            write_launch_intent(
                runtime,
                payload,
                records,
                task["ref"],
                record,
                role=WORKER_ROLE,
                action="rework",
                head=record.head,
                workspace=record.workspace,
            )
        )
        with mock.patch.object(
            codex_preflight.CodexProviderEventRecorder,
            "record",
            new=self._fail_recorder,
        ):
            launched, failure = bring_up_worker_head(
                runtime,
                task,
                record,
                records,
                payload,
                record.attempt_id,
                step="advance",
                stage="rework",
                blocked_reason="contract",
                blocked_action="contract-worker-blocked",
            )

        self.assertIsNone(failure)
        assert launched is not None
        self._assert_bound(record.worker_head_run, role="worker")
        self.assertRegex(
            record.worker_head_run["fanout_policy"]["prompt_identity"]["version"],
            r"^sha256:[0-9a-f]{64}$",
        )
        self.assertEqual(record.worker_head_run["fanout_policy"]["events"], [])
        self.assertEqual(record.worker_head_run, record.launch_intent["head_run"])
        callback_snapshot = next(
            snapshot[task["ref"]]
            for snapshot in self.snapshots
            if snapshot[task["ref"]]["worker_head_run"]
            .get("fanout_policy", {})
            .get("provider_source", {})
            .get("state")
            == "bound"
            and not snapshot[task["ref"]]["launch_intent"].get("launched")
        )
        self.assertEqual(callback_snapshot["launch_intent"]["head_run"], callback_snapshot["worker_head_run"])

        # A competing, incomplete, or foreign writer reaches the configured production ingress and
        # is fenced without changing the retained descriptor.
        ingress = self.host._codex_provider_ingress(HeadRun.from_json(record.worker_head_run))
        assert ingress is not None
        before = copy.deepcopy(record.worker_head_run)
        source = dict(before["fanout_policy"]["provider_source"])
        foreign = HeadRun.from_json(before).with_fanout_policy(
            {
                **before["fanout_policy"],
                "provider_source": {**source, "session_id": "foreign-session"},
            }
        )
        with self.assertRaisesRegex(HostError, "bound provider sources conflict"):
            ingress.commit_run(foreign)
        self.assertEqual(record.worker_head_run, before)
        incomplete = HeadRun.from_json(before).with_fanout_policy(
            {
                **before["fanout_policy"],
                "provider_source": {key: value for key, value in source.items() if key != "session_id"},
            }
        )
        ingress.commit_run(incomplete)
        self.assertEqual(record.worker_head_run, before, "an incomplete writer cannot clobber a binding")
        record.worker_head_run = incomplete.to_json()
        self.assertIn(
            self.host.provider_progress(task, record, "worker")["state"],
            {"unavailable", "identity_mismatch"},
        )
        record.worker_head_run = before

        baseline = self.host.provider_progress(task, record, "worker")
        self._append_records({"type": "turn.completed", "thread_id": "parent-1"})
        self.assertIsNone(
            runtime.poll_codex_provider_ingress(
                record,
                records,
                payload,
                reference=task["ref"],
            )
        )
        progress = self.host.provider_progress(task, record, "worker")
        self.assertEqual(progress["admission"], "accepted")
        self.assertNotEqual(progress["cursor"], baseline["cursor"])
        with mock.patch.object(
            dispatcher_review,
            "_head_run_process_status",
            return_value={"known": True, "match": True, "state": "live-match", "stopped": False},
        ):
            status = dispatcher_review.command_terminal_status(self.host, task, record, kind="worker")
        # A supervised head has no pane: its liveness is its pid and this exact-run cursor.
        self.assertEqual(status["reason"], "pid")
        self.assertEqual(status["provider_progress"]["cursor"], progress["cursor"])
        self.assertEqual(self.moves, [])
        self.assertEqual(len(self.session.sent), 1, "progress did not nudge or replace the worker")

        # This is the crash boundary after confirmation: adoption reads the exact source already
        # committed to the launch intent rather than reconstructing an unbound local copy.
        with mock.patch.object(
            dispatcher_launch,
            "head_process_status",
            return_value={"known": True, "match": True},
        ):
            adopted = resolve_launch_intent(runtime, task, records, payload)
        self.assertEqual(adopted["action"], "worker-launch-adopted")
        self._assert_bound(record.worker_head_run, role="worker")

    def test_reviewer_route_uses_the_external_document_identity_and_adopts_the_bound_run(self) -> None:
        runtime = self._runtime()
        task = {"ref": "secretary-1428", "project": "ummanu", "workspace": {"base_branch": "main"}}
        record = self._record(str(self.workspace))
        record.state = "review_starting"
        records = {task["ref"]: record}
        payload: dict = {}
        self._arm_source(record.workspace)
        self._add_recording_failure_event()
        calls = [0]

        def crash_after_confirm(*_args) -> None:
            calls[0] += 1
            if calls[0] == 1:
                raise OSError("dispatcher crashed after reviewer confirmation")

        runtime.record_review_routing = crash_after_confirm
        with (
            mock.patch.object(
                codex_preflight.CodexProviderEventRecorder,
                "record",
                new=self._fail_recorder,
            ),
            self.assertRaisesRegex(OSError, "crashed after reviewer confirmation"),
        ):
            dispatcher_review.start_review(
                runtime,
                task,
                records,
                record,
                record.attempt_id,
                action="review-started",
                payload=payload,
            )

        self._assert_bound(record.review_head_run, role="reviewer")
        self.assertRegex(
            record.review_head_run["fanout_policy"]["prompt_identity"]["version"],
            r"^sha256:[0-9a-f]{64}$",
        )
        self.assertEqual(record.review_head_run["fanout_policy"]["events"], [])
        self.assertEqual(record.review_head_run, record.launch_intent["head_run"])
        self.assertEqual(
            record.review_head_run["task_ref"]["document"],
            str(self.host._prompt_document_path(REVIEW_ROLE, task["ref"], record.review_baseline)),
        )
        with mock.patch.object(
            dispatcher_launch,
            "head_process_status",
            return_value={"known": True, "match": True},
        ):
            adopted = resolve_launch_intent(runtime, task, records, payload)
        self.assertEqual(adopted["action"], "review-launch-adopted")
        self._assert_bound(record.review_head_run, role="reviewer")

    def test_observer_prepare_callback_persists_the_bound_run_for_watchdog_adoption(self) -> None:
        runtime = SimpleNamespace()
        runtime.host = self.host
        runtime.owner = "contract-dispatcher"
        runtime.production_state = SimpleNamespace(save=lambda _payload: None)
        observers: dict[str, ObserverRecord] = {}
        payload: dict = {}
        record = ObserverRecord(sprint="sprint:contract")

        self.assertIsNone(
            write_observer_launch_intent(
                runtime,
                payload,
                observers,
                record.sprint,
                record,
                "codex-contract",
                1,
            )
        )
        self._arm_source(record.workspace)
        self._add_recording_failure_event()
        _bind_codex_provider_ingress(runtime, payload, observers, record.sprint, record)
        with mock.patch.object(
            codex_preflight.CodexProviderEventRecorder,
            "record",
            new=self._fail_recorder,
        ):
            launched = self.host.prepare_observer(
                {"ref": record.sprint},
                "codex-contract",
                prompt="# Sprint\n",
                heartbeat_run_id=str(record.head_run["run_id"]),
            )

        self.assertEqual(record.head_run, launched["head_run"])
        self._assert_bound(record.head_run, role=OBSERVER_ROLE)
        self.assertEqual(record.head_run["fanout_policy"]["events"], [])
        self.assertEqual(record.pending_launch, 1, "the watchdog sees the crash-era intent")
        # The bring-up's own launch prompt reaches the same backend as everything else since
        # secretary-1461, so what adoption must not do is measured from where the launch left it.
        sent_by_the_launch = len(self.session.sent)
        with mock.patch.object(
            dispatcher_observer,
            "observer_alive",
            return_value={"alive": True, "pid_known": True},
        ):
            adopted = adopt_observer_launch_intent(
                runtime,
                payload,
                observers,
                record.sprint,
                record,
            )
        self.assertEqual(adopted["action"], "observer-adopted")
        self._assert_bound(observers[record.sprint].head_run, role=OBSERVER_ROLE)
        self.assertEqual(
            len(self.session.sent),
            sent_by_the_launch,
            "watchdog adoption did not redeliver or replace",
        )


class ProviderScanWriteStormTests(unittest.TestCase):
    """One scan of ordinary rollout lines costs a constant number of durable writes (ummanu-131).

    The real record flush of a real-mode runtime is used: every provider persist reaches
    `DispatcherRuntime.save_records`, the cleanup journal and the production state.
    """

    LINES = 1000

    def setUp(self) -> None:
        self.helper = CodexProviderEventIngressTests("test_binds_new_session_and_cursor_before_any_provider_event")
        self.helper.setUp()
        self.addCleanup(self.helper.tearDown)
        self.data = self.helper.root / "data"
        runtime = object.__new__(DispatcherRuntime)
        runtime.data_dir = self.data
        runtime.catalog = FakeCatalog()
        runtime.host = CommandHostRuntime(runtime.catalog, self.data, mode="real")
        runtime.reader = SimpleNamespace(show=lambda ref: {"id": 1, "ref": ref, "project": "sample", "claim": {}})
        runtime.production_state = ProductionState(self.data)
        runtime.cleanup = CleanupOwner(runtime)
        self.runtime = runtime
        self.record = DispatcherRecord(
            worker="sample-1-worker",
            workspace=str(self.helper.workspace),
            handle="",
            head="codex-extra",
            review_head="claude",
            attempt_id="attempt-1",
            comment_baseline=0,
            review_baseline=0,
            state="claimed",
            claimed_at=1.0,
        )
        self.record.worker_head_run = json.loads(json.dumps(self.helper._run().to_json()))
        self.records = {"sample-1": self.record}
        self.cleanup_replacements: list[str] = []
        replace = os.replace

        def counted(source, target, *args, **kwargs):
            if str(target).startswith(str(self.data / "dispatcher" / "cleanup")):
                self.cleanup_replacements.append(str(target))
            return replace(source, target, *args, **kwargs)

        self.enterContext(mock.patch("ummanu.dispatch.cleanup.os.replace", side_effect=counted))
        self.saves = self.enterContext(
            mock.patch.object(runtime.production_state, "save", wraps=runtime.production_state.save)
        )

    def _ingress(self, run: HeadRun | None = None, persist=None) -> CodexProviderEventIngress:
        captured = {}
        with mock.patch.object(
            self.runtime.host,
            "configure_codex_provider_ingress",
            side_effect=lambda _run, *, persist, stop, block: captured.update(persist=persist),
        ):
            self.runtime.bind_codex_provider_ingress(
                self.record, self.records, {}, role=WORKER_ROLE, reference="sample-1"
            )
        return CodexProviderEventIngress(
            run or HeadRun.from_json(self.record.worker_head_run),
            persist or captured["persist"],
            stop=lambda current, reason: self.helper.stops.append((current.run_id, reason)),
            block=self.helper.blocks.append,
        )

    def _ordinary(self, count: int) -> list[dict]:
        return [
            {"type": "event_msg", "payload": {"type": "agent_message", "thread_id": "parent-1", "n": index}}
            for index in range(count)
        ]

    def _collaboration(self) -> dict:
        return {
            "type": "item.completed",
            "item": {"type": "collab_tool_call", "tool": "spawn_agent", "sender_thread_id": "parent-1"},
        }

    def _cursor(self) -> int:
        return self.record.worker_head_run["fanout_policy"]["provider_source"]["cursor"]["line"]

    def test_a_thousand_ordinary_lines_cost_a_constant_number_of_writes(self) -> None:
        self.helper._write_source(*self._ordinary(self.LINES))
        ingress = self._ingress()

        ingress.poll()

        # The binding, then one cursor write for the whole scan.
        self.assertEqual(self._cursor(), self.LINES + 2)
        self.assertLessEqual(len(self.cleanup_replacements), 2, self.cleanup_replacements)
        self.assertEqual(self.saves.call_count, 2)
        self.assertEqual(ingress.run.fanout_policy["events"], [])

        # A second scan of as many new lines moves only the advisory cursor: one state save, and the
        # cleanup projection is unchanged, so the journal is neither read nor written.
        self.helper._append_records(*self._ordinary(self.LINES))
        self.cleanup_replacements.clear()
        with mock.patch.object(CleanupJournal, "load_intent", wraps=self.runtime.cleanup.journal.load_intent) as reads:
            ingress.poll()
        self.assertEqual(self._cursor(), 2 * self.LINES + 2)
        self.assertEqual(self.cleanup_replacements, [])
        reads.assert_not_called()
        self.assertEqual(self.saves.call_count, 3)
        for file in (self.data / "dispatcher" / "cleanup").rglob("*.json"):
            self.assertLessEqual(file.stat().st_size, 1_000_000)

    def test_meaningful_event_is_durable_at_its_own_line_inside_a_batched_scan(self) -> None:
        self.helper._write_source(*self._ordinary(500), self._collaboration(), *self._ordinary(500))
        written: list[HeadRun] = []
        ingress = self._ingress()
        persist = ingress.persist

        def recorded(run: HeadRun) -> None:
            persist(run)
            written.append(run)

        ingress.persist = recorded
        ingress.poll()

        event_line = 2 + 500 + 1
        cursors = [run.fanout_policy["provider_source"]["cursor"]["line"] for run in written]
        # Binding, the event at its own cursor (never ahead of it), then the end of the scan.
        self.assertEqual(cursors, [0, event_line, 2 + 1001])
        self.assertEqual(written[1].fanout_policy["events"][0]["source_sequence"], event_line)
        self.assertEqual(len(ingress.run.fanout_policy["events"]), 1)
        self.assertEqual(self.saves.call_count, 3)
        self.assertEqual((self.helper.stops, self.helper.blocks), ([], []))

    def test_crash_before_the_scan_cursor_write_rescans_without_duplicate_events(self) -> None:
        self.helper._write_source(*self._ordinary(300), self._collaboration(), *self._ordinary(300))
        durable: list[dict] = []
        ingress = self._ingress()
        persist = ingress.persist

        def crash_at_scan_end(run: HeadRun) -> None:
            if run.fanout_policy["provider_source"]["cursor"]["line"] == 2 + 601:
                raise KeyboardInterrupt("process died before the advisory cursor write")
            persist(run)
            durable.append(json.loads(json.dumps(run.to_json())))

        ingress.persist = crash_at_scan_end
        with self.assertRaises(KeyboardInterrupt):
            ingress.poll()
        # The restarted dispatcher resumes from what was durable: the event at its own line.
        restored = HeadRun.from_json(durable[-1])
        self.assertEqual(restored.fanout_policy["provider_source"]["cursor"]["line"], 2 + 301)
        self.assertEqual(self.record.worker_head_run["fanout_policy"]["provider_source"]["cursor"]["line"], 2 + 301)
        restarted = self._ingress(restored)
        restarted.poll()
        self.assertEqual(len(restarted.run.fanout_policy["events"]), 1)
        self.assertEqual(restarted.run.fanout_policy["provider_source"]["cursor"]["line"], 2 + 601)
        self.assertEqual((self.helper.stops, self.helper.blocks), ([], []))

    def test_failed_scan_cursor_write_keeps_the_durable_cursor_and_retries_next_poll(self) -> None:
        self.helper._write_source(*self._ordinary(200))
        ingress = self._ingress()
        ingress.poll()
        self.helper._append_records(*self._ordinary(200))
        persist = ingress.persist

        def fail(_run: HeadRun) -> None:
            raise OSError("disk full")

        ingress.persist = fail
        ingress.poll()
        self.assertEqual(self._cursor(), 202)
        self.assertEqual(ingress.run.fanout_policy["provider_source"]["cursor"]["line"], 202)
        ingress.persist = persist
        ingress.poll()
        self.assertEqual(self._cursor(), 402)


if __name__ == "__main__":
    unittest.main()
