"""Card role aliases cross real dispatcher boundaries with mock runtime effects."""

from __future__ import annotations

import dataclasses
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ummanu.dispatch import heartbeat, host as host_module, launch, provider_failure
from ummanu.dispatch.card_roles import card_kind, head_role
from ummanu.dispatch.host import CommandHostRuntime
from ummanu.dispatch.review import retry_busy_reviewer_launch_delivery
from ummanu.dispatch.state import DispatcherRecord
from ummanu.dispatch.types import HostError
from ummanu.dispatch.watchdog import pid_file_path
from ummanu.runtime.codex_preflight import CodexFanoutPolicyError
from ummanu.runtime.head import HeadCommand, HeadRun, HeadSpec, TaskRef


class CardRoleBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.document = self.root / "REVIEW.md"
        self.document.write_text("review this candidate\n", encoding="utf-8")
        self.task = {"ref": "ummanu-253", "project": "ummanu", "sprint": "1490"}
        self.catalog = mock.Mock()
        self.catalog.head_launch.return_value = HeadCommand("role-env-command", prompt_after_start=True)
        self.host = CommandHostRuntime(self.catalog, self.root, mode="real")
        self.spec = HeadSpec(profile_id="head", adapter="hermes", runtime="local-pty")

    def run_for(self, role: str = "reviewer", **changes) -> HeadRun:
        return dataclasses.replace(HeadRun(
            run_id="run", spec=self.spec, role=role, workspace=str(self.root),
            task_ref=TaskRef.card(self.task["ref"], document=str(self.document)),
            pid_file=pid_file_path("review", self.task["ref"]), handle="pane", leaf="leaf",
        ), **changes)

    def record_for(self, **changes) -> DispatcherRecord:
        values = {"worker": "worker", "handle": "", "workspace": str(self.root), "head": "head",
                  "review_head": "head", "attempt_id": "attempt", "comment_baseline": 0,
                  "review_baseline": 0, "state": "claimed", "claimed_at": 0}
        return DispatcherRecord(**(values | changes))

    def test_launch_aliases_bind_preflight_runtime_heartbeat_and_role_env(self) -> None:
        for alias, kind, role in (("review", "review", "reviewer"),
                                  ("reviewer", "review", "reviewer"),
                                  ("worker", "worker", "worker")):
            with self.subTest(alias=alias):
                backend = mock.Mock(writes_launch_identity=True)

                def preflight(_head, **binding):
                    return HeadRun(spec=self.spec, **binding)

                def start(_spec, _workspace, _task_ref, **binding):
                    return SimpleNamespace(ok=True, run=binding["run"], delivery=None, fallback_reason="")

                backend.start.side_effect = start
                policy = {"version": 1, "fixture": "round-policy"}
                with (
                    mock.patch.dict(os.environ, {"UMMANU_DISPATCHER_BODY_DIR": str(self.root)}, clear=True),
                    mock.patch.object(self.host, "_require_production_runtime") as production,
                    mock.patch.object(self.host, "_require_workspace_environment") as environment,
                    mock.patch.object(self.host, "_preflight_launch_run", side_effect=preflight) as attest,
                    mock.patch.object(self.host, "_head_spec", return_value=self.spec),
                    mock.patch.object(self.host, "head_runtime_for", return_value=backend),
                    mock.patch.object(self.host, "_codex_provider_ingress", return_value=None),
                    mock.patch.object(self.host, "_head_transport"),
                    mock.patch.object(host_module.memory_access, "issue_grant",
                                      return_value=SimpleNamespace(launch_identity={"token": "fixture"})) as grant,
                    mock.patch.object(host_module, "_launch_command", return_value="wrapped") as wrap,
                    mock.patch.object(host_module, "_bind_head_heartbeat") as bind,
                    mock.patch.object(host_module, "_clear_head_heartbeat") as clear,
                ):
                    result = self.host._launch(
                        str(self.root), "title", "head", "prompt.md", role=alias,
                        env_name="UNSET_OVERRIDE", task=self.task, prompt_document=str(self.document),
                        launch_prompt="read REVIEW.md", heartbeat_run_id="run", local_run_policy=(policy, True),
                    )
                    expected_path = pid_file_path(kind, self.task["ref"])
                production.assert_called_once()
                environment.assert_called_once_with(str(self.root))
                self.assertEqual(attest.call_args.kwargs["pid_file"], expected_path)
                self.assertEqual(attest.call_args.kwargs["role"], role)
                started = backend.start.call_args.kwargs
                self.assertEqual((started["role"], started["run_id"], started["pid_file"]),
                                 (role, "run", expected_path))
                self.assertEqual(started["run"].task_ref, TaskRef.card(self.task["ref"], document=str(self.document)))
                self.assertEqual(started["run"].fanout_policy["prompt_identity"]["version"],
                                 "sha256:" + hashlib.sha256(self.document.read_bytes()).hexdigest())
                self.assertEqual(self.catalog.head_launch.call_args.kwargs["role"], role)
                self.assertIn("local_run_policy", self.catalog.head_launch.call_args.kwargs)
                self.assertEqual(self.catalog.head_launch.call_args.kwargs["identity"], {"BOARD_ACTOR": "head"})
                grant.assert_called_once()
                self.assertEqual(wrap.call_args.args[3],
                                 {"run_id": "run", "role": role, "task": "card:ummanu-253", "leaf": ""})
                bind.assert_called_once_with(expected_path, expected=wrap.call_args.args[3], leaf="")
                clear.assert_not_called()
                self.assertEqual(result.head_run["role"], role)

    def test_alias_field_clear_and_forget_preserve_worker_identity(self) -> None:
        for alias in ("review", "reviewer"):
            with self.subTest(alias=alias):
                record = self.record_for(handle="worker-pane", worker_leaf="worker-leaf",
                                          worker_pid_file="worker-pid", review_handle="review-pane",
                                          review_leaf="review-leaf", review_pid_file="old-review-pid")
                record.worker_continuation.session_held = True
                worker_before = record.worker_continuation.to_json()
                record.launch_intent = {"role": alias, "pid_file": "review-pid"}
                launch.clear_launch_intent(record)
                self.assertEqual(record.review_pid_file, "review-pid")
                self.assertEqual(launch.role_field(alias, "head_run"), "review_head_run")
                self.assertEqual(launch.role_label(alias), "reviewer")
                launch.forget_role_head(record, alias)
                self.assertEqual((record.review_handle, record.review_leaf, record.review_pid_file), ("", "", ""))
                self.assertEqual((record.handle, record.worker_leaf, record.worker_pid_file),
                                 ("worker-pane", "worker-leaf", "worker-pid"))
                self.assertEqual(record.worker_continuation.to_json(), worker_before)

    def test_intent_producer_keeps_canonical_role_round_run_and_policy(self) -> None:
        for alias in ("review", "reviewer", "worker"):
            with self.subTest(alias=alias):
                kind = "worker" if alias == "worker" else "review"
                role = "worker" if alias == "worker" else "reviewer"
                record = self.record_for(workspace=str(self.root), attempt_id="attempt", attempt_round=2,
                                          review_baseline=7, report_generation=3, review_respawns=4)
                runtime = mock.Mock()
                runtime.host.preflight_codex_run.side_effect = lambda _head, **binding: HeadRun(spec=self.spec, **binding)
                runtime.host.local_run_snapshot_for_round.return_value = {"fixture": "policy"}
                error = launch.write_launch_intent(
                    runtime, {}, {}, self.task["ref"], record, role=alias, action="start",
                    head="head", workspace=str(self.root), document=str(self.document), round_number=5, task=self.task,
                )
                self.assertIsNone(error)
                intent = launch.launch_intent(record)
                self.assertEqual((intent["role"], intent["attempt_id"], intent["round"]),
                                 (kind, "attempt", 5))
                self.assertEqual(intent["pid_file"], pid_file_path(kind, self.task["ref"]))
                self.assertEqual(intent["respawns"], 0 if kind == "worker" else 4)
                stored = getattr(record, f"{kind}_head_run")
                self.assertEqual((stored["role"], stored["run_id"]), (role, intent["run_id"]))
                self.assertEqual(heartbeat.intent_heartbeat_identity(intent)["role"], role)
                policy_args = runtime.host.local_run_snapshot_for_round.call_args.args
                self.assertEqual(policy_args[1:3], (kind, 3 if kind == "worker" else 7))
                launch.confirm_launch_intent(runtime, {}, {}, self.task["ref"], record,
                                             head_run=stored, handle="pane", leaf="leaf")
                if kind == "review":
                    pending = launch.launch_intent(record)
                    pending["delivery"] = {"state": "busy", "attempts": 1}
                    record.launch_intent = pending
                    runtime.host.nudge_review_delivery.return_value = {
                        "head_run": stored, "delivery_evidence": {"stage": "turn", "turn_confirmed": True},
                    }
                    self.assertIsNone(retry_busy_reviewer_launch_delivery(
                        runtime, self.task, {}, {}, record, pending, "review"))
                    retried = launch.launch_intent(record)
                    for field in ("role", "run_id", "attempt_id", "round", "head_run", "task"):
                        self.assertEqual(retried[field], pending[field])
                    self.assertFalse(launch.undelivered_launch_delivery(retried))
                    record.review_commit = "candidate"
                    outcome = launch._adopt_launch_intent(
                        runtime, self.task, {}, {}, record, retried, "review", "review")
                    self.assertEqual(outcome["action"], "review-launch-adopted")
                    self.assertEqual(record.review_head_run, stored)
                    self.assertEqual(record.review_local_run_snapshot, {"fixture": "policy"})
                    self.assertEqual((record.attempt_round, record.review_baseline), (2, 7))
                    runtime.record_worker_routing.assert_not_called()
                else:
                    launch.clear_launch_intent(record)
                self.assertEqual(getattr(record, f"{kind}_pid_file"), intent["pid_file"])

    def test_provider_consumers_refuse_foreign_persisted_binding(self) -> None:
        for consumer, seam in ((self.host.provider_progress, "_provider_progress_for_run"),
                               (self.host.provider_failure, "_provider_failure_for_persisted_run")):
            with mock.patch.object(host_module, seam, return_value={"state": "fixture"}) as read:
                for run in (self.run_for("worker"), self.run_for("review"),
                            self.run_for(workspace="foreign"), self.run_for(task_ref=TaskRef.card("other"))):
                    record = self.record_for(review_head_run=run.to_json())
                    self.assertEqual(consumer(self.task, record, "review")["state"], "identity_mismatch")
                read.assert_not_called()
                record.review_head_run = self.run_for().to_json()
                self.assertEqual(consumer(self.task, record, "review")["state"], "fixture")
                read.assert_called_once()

    def test_other_role_domains_never_select_worker_fields(self) -> None:
        for role in ("observer", "po", "steward", "unknown", ""):
            self.assertEqual(head_role(role), role)
            self.assertEqual(heartbeat.heartbeat_role(role), role)
            with self.assertRaises(ValueError):
                card_kind(role)
            with self.assertRaises(ValueError):
                launch.role_field(role, "head_run")

    def test_noop_and_refused_preflight_still_cross_launch_guards(self) -> None:
        for alias in ("review", "reviewer", "worker"):
            role = head_role(alias)
            with (
                mock.patch.object(self.host, "_require_production_runtime") as production,
                mock.patch.object(self.host, "_require_workspace_environment") as environment,
                mock.patch.object(self.host, "_preflight_launch_run",
                                  side_effect=lambda _head, **binding: HeadRun(spec=self.spec, **binding)) as preflight,
                mock.patch.object(self.host, "head_runtime_for") as backend,
            ):
                self.host.mode = "noop"
                self.host._launch(str(self.root), "title", "head", "prompt.md", role=alias,
                                  env_name="UNSET", task=self.task, prompt_document=str(self.document))
                self.assertEqual(preflight.call_args.kwargs["role"], role)
                backend.assert_not_called()
                production.assert_called_once()
                environment.assert_called_once()
                self.host.mode = "real"
                preflight.side_effect = CodexFanoutPolicyError("refused", run=self.run_for(role))
                with self.assertRaisesRegex(HostError, "refused"):
                    self.host._launch(str(self.root), "title", "head", "prompt.md", role=alias,
                                      env_name="UNSET", task=self.task)
                backend.assert_not_called()

    def test_normalization_grants_no_foreign_run_or_stop_authority(self) -> None:
        run = self.run_for()
        for foreign in (self.run_for("worker"), self.run_for("review"), self.run_for(run_id="foreign"),
                        self.run_for(workspace="foreign"), self.run_for(task_ref=TaskRef.card("foreign"))):
            with self.assertRaisesRegex(HostError, "identity mismatch"):
                launch._merge_launch_head_runs(run, foreign)
        record = self.record_for(review_head_run=run.to_json())
        runtime = mock.Mock()
        runtime.host.provider_failure.return_value = {"state": "failed", "run_id": "foreign"}
        with mock.patch.object(provider_failure, "_fall_back") as fallback:
            self.assertIsNone(provider_failure.provider_failure_outcome(
                runtime, self.task, record, {}, {}, "attempt", kind="review"))
            fallback.assert_not_called()
        intent = {"role": "review", "pid_file": run.pid_file, "run_id": "run", "task": "card:ummanu-253"}
        record.launch_intent = intent
        with mock.patch.object(launch, "head_process_status", return_value={"state": "identity-mismatch"}) as status:
            self.assertTrue(launch.launch_intent_liveness(intent)["identity_mismatch"])
            self.assertIn("mismatching", launch.stop_launch_intent(runtime, record, intent, "review"))
            self.assertEqual(status.call_args.kwargs["expected"], heartbeat.intent_heartbeat_identity(intent))
        runtime.host.stop_review.assert_not_called()
        self.assertTrue(launch.launch_intent(record))

    def test_stop_refusal_keeps_intent_and_both_role_identities(self) -> None:
        record = self.record_for(handle="worker-pane", review_handle="review-pane")
        intent = {"role": "review", "pid_file": "review-pid", "run_id": "run",
                  "head_run": self.run_for().to_json()}
        record.launch_intent = intent
        runtime = mock.Mock()
        runtime.host.stop_review.side_effect = HostError("stop unconfirmed")
        with mock.patch.object(launch, "head_process_status", return_value={"known": True, "alive": True}):
            self.assertIn("stop unconfirmed", launch.stop_launch_intent(runtime, record, intent, "review"))
        self.assertTrue(launch.launch_intent(record))
        self.assertEqual((record.handle, record.review_handle), ("worker-pane", "review-pane"))
        runtime.host.stop_worker_head.assert_not_called()
        runtime.host.stop_review.side_effect = None
        with mock.patch.object(launch, "head_process_status", return_value={"known": True, "alive": True}):
            self.assertIsNone(launch.stop_launch_intent(runtime, record, intent, "review"))
        self.assertFalse(launch.launch_intent(record))
        self.assertEqual((record.handle, record.review_handle, record.review_pid_file), ("worker-pane", "", ""))


if __name__ == "__main__":
    unittest.main()
