"""Disposable Git repositories and simulated scope owners, including public maintenance."""

from __future__ import annotations

import argparse
import contextlib
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from tests.fakes.cleanup import HourlyClock, ManualClock
from tests.fakes.dispatcher import FakeCatalog, FakeHost
from tests.production_runtime_fixtures import registered_production_runtime
from ummanu.broad_check import run_broad_check
from ummanu.cli import build_parser, run_residue_maintenance
from ummanu.dispatch.cleanup import (
    RETRY_COOLDOWN,
    CleanupJournal,
    CleanupOwner,
    UnknownProject,
    ownership_lock,
)
from ummanu.dispatch.host import CommandHostRuntime
from ummanu.dispatch.observer import ObserverRecord
from ummanu.dispatch.production import ProductionState, _reconcile_production
from ummanu.dispatch.runtime import DispatcherRuntime
from ummanu.dispatch.state import DispatcherRecord
from ummanu.dispatch.types import HostError
from ummanu.infra import git_worktree
from ummanu.observer_root import observer_root_repo
from ummanu.runtime.head import HeadRun, HeadSpec, StopInitiator, TaskRef
from ummanu.runtime.head_runtimes import LOCAL_PTY_RUNTIME
from ummanu.runtime.role_env import workspace_tool_cache_env


def journal_bytes(journal: CleanupJournal) -> dict[str, bytes]:
    """Every stored journal file, so an assertion of "nothing written" covers all of them."""
    files = sorted(journal.path.rglob("*")) if journal.path.exists() else []
    stored = {str(file.relative_to(journal.path)): file.read_bytes() for file in files if file.is_file()}
    if journal.legacy.exists():
        stored["<v1>"] = journal.legacy.read_bytes()
    return stored


def obligations(journal: CleanupJournal) -> dict[str, Any]:
    """Every stored file, each intent without its own retry schedule (ummanu-132).

    A later attempt records when it ran; everything the obligation proves must stay byte-identical.
    """
    stored: dict[str, Any] = {}
    for name, body in journal_bytes(journal).items():
        if name.startswith("intents/"):
            intent = json.loads(body)
            intent.pop("retry", None)
            stored[name] = intent
        else:
            stored[name] = body
    return stored


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


class OwnedCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "--quiet", "--initial-branch=main")
        git(self.repo, "config", "user.name", "Cleanup Test")
        git(self.repo, "config", "user.email", "cleanup@example.invalid")
        (self.repo / "file").write_text("base\n")
        (self.repo / ".gitignore").write_text("ignored\nTASK.md\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "--quiet", "-m", "base")
        self.base = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/origin/main", self.base)
        self.task = {"id": "task-1", "ref": "sample-1", "project": "sample", "sprint": "sprint:1",
                     "state": "done", "closed": False, "claim": {"worker": None, "claimed_at": None},
                     "workspace": {"base_branch": None}}
        self.tasks = {self.task["ref"]: self.task}
        self.workspace = self.data / "workspaces" / "sample" / "sample-1-worker"
        self.workspace.parent.mkdir(parents=True)
        git(self.repo, "worktree", "add", "-b", "pipeline/sample-1", str(self.workspace), "main")
        self.record = DispatcherRecord(worker="sample-1-worker", workspace=str(self.workspace), handle="",
                                       head="test", review_head="test", attempt_id="attempt-1",
                                       comment_baseline=0, review_baseline=0, state="assessment", claimed_at=1)
        self.stops = []
        self.stop_failure = False
        self.backend = SimpleNamespace(stop=self.stop)
        self.catalog = SimpleNamespace(bindings={"sample": {"repo": str(self.repo), "default_branch": "main"}})
        self.catalog.binding = lambda project: self.catalog.bindings[project]
        self.host = SimpleNamespace(head_runtime_for=lambda run: self.backend,
                                    _decide_workspace_environment_ownership=lambda path: "absent")
        self.runtime = SimpleNamespace(data_dir=self.data, catalog=self.catalog, host=self.host,
                                       reader=SimpleNamespace(show=lambda ref: copy.deepcopy(self.tasks[ref])),
                                       writer=SimpleNamespace(settle_cleanup_claim=self.settle_claim),
                                       audit=SimpleNamespace(events=lambda ref: [{"kind": "claimed"}]),
                                       cleanup_clock=HourlyClock())
        self.owner = CleanupOwner(self.runtime)

    def stop(self, run, initiator):
        self.stops.append((run.run_id, run.scope_generation))
        return SimpleNamespace(ok=not self.stop_failure, reason="simulated stop failure",
                               run=run.finishing(initiator).exited())

    def settle_claim(self, task, worker):
        self.assertEqual(task["claim"]["worker"], worker)
        self.tasks[task["ref"]]["claim"] = {"worker": None, "claimed_at": None}

    def head(self, role="worker", generation="generation-1"):
        run = HeadRun(run_id="run-" + role, spec=HeadSpec(profile_id="test", adapter="unknown", runtime=LOCAL_PTY_RUNTIME),
                      workspace=str(self.workspace), task_ref=TaskRef.card(self.task["ref"]),
                      role=role, scope_generation=generation)
        setattr(self.record, "worker_head_run" if role == "worker" else "review_head_run", run.to_json())
        return run

    def request(self, disposition="done"):
        self.owner.remember(self.task, self.record)
        return self.owner.journal.request(self.task, disposition, self.record.to_json())

    def state(self, records):
        path = self.data / "dispatcher" / "production-state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"records": records}))

    def observer(self, *, committed=False):
        repo = observer_root_repo(self.data)
        repo.mkdir(parents=True)
        git(repo, "init", "--quiet", "--initial-branch=observers")
        git(repo, "config", "user.name", "Cleanup Test")
        git(repo, "config", "user.email", "cleanup@example.invalid")
        git(repo, "commit", "--quiet", "--allow-empty", "-m", "root")
        path = self.data / "workspaces" / "observers" / "sprint-1"
        path.parent.mkdir(parents=True)
        git(repo, "worktree", "add", "--detach", str(path), "HEAD")
        if committed:
            (path / "NOTES.md").write_text("retained user notes\n")
            git(path, "add", "NOTES.md")
            git(path, "commit", "--quiet", "-m", "user notes")
        self.host.observer_workspace = lambda ref: str(path)
        self.runtime.sprints = SimpleNamespace(show=lambda *a, **k: {
            "id": "sprint-1", "ref": "sprint:1", "status": "closed"})
        run = HeadRun(run_id="observer-run", spec=HeadSpec(
            profile_id="test", adapter="unknown", runtime=LOCAL_PTY_RUNTIME),
            workspace=str(path), task_ref=TaskRef.sprint("sprint:1"), role="observer")
        return repo, path, ObserverRecord(sprint="sprint:1", generation="observer-gen",
                                         workspace=str(path), head_possible=True, head_run=run.to_json())

    def residue_command(self, *, replay=False, project=None, targets=(), digests=(), expected_exit=0):
        self.runtime.cleanup = self.owner
        args = argparse.Namespace(instance="unused", residue_replay=replay, residue_inventory=not replay,
                                  project=project, target=list(targets), manifest=list(digests))
        with mock.patch("ummanu.dispatch.bootstrap.runtime_from_args", return_value=self.runtime), mock.patch("builtins.print") as output:
            self.assertEqual(run_residue_maintenance(args), expected_exit)
        return json.loads(output.call_args.args[0])

    def maintenance(self, *, expected_exit=0, project="sample"):
        """The operator procedure: read the project's manifest, then replay its open targets exactly."""
        inventory = self.owner.inventory(project=project)
        chosen = [entry for entry in inventory["manifest"]
                  if entry["outcome"] != "completed" and (len(entry["target"]) == 64 or entry["outcome"] == "eligible")]
        if not chosen:
            return {**inventory, "replay": []}
        return self.residue_command(replay=True, project=project, targets=[e["target"] for e in chosen],
                                    digests=[e["digest"] for e in chosen], expected_exit=expected_exit)

    def interrupt_git_directory_removal(self):
        self.task["claim"]["worker"] = self.record.worker
        key = self.request()
        def interrupted(args, **kwargs):
            if args[3:5] == ["worktree", "remove"]:
                self.assertTrue(self.owner.journal.read()["intents"][key]["progress"]["removal_started"])
                shutil.rmtree(self.workspace)  # Simulate Git's first effect in this disposable repo.
                raise KeyboardInterrupt("Git interrupted before admin removal")
            return native(args, **kwargs)
        native = subprocess.run
        with mock.patch("ummanu.infra.git_worktree.subprocess.run", side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
            self.owner.replay_one(key)
        self.assertFalse(self.workspace.exists())
        self.assertIn(str(self.workspace), git(self.repo, "worktree", "list", "--porcelain"))
        return key

    def test_detached_unpublished_observer_commit_keeps_clean_checkout_and_notes(self):
        repo, path, observer = self.observer(committed=True)
        tip = git(path, "rev-parse", "HEAD")
        self.assertEqual(git(repo, "for-each-ref", "--contains=" + tip), "")
        self.assertEqual(git(path, "status", "--porcelain"), "")
        result = self.owner.cleanup_observer(observer)
        self.assertEqual(result["status"], "preserved", result["reason"])
        self.assertTrue(result["progress"]["preservation_verified"])
        self.assertEqual((path / "NOTES.md").read_text(), "retained user notes\n")
        self.assertEqual(git(path, "rev-parse", "HEAD"), tip)
        self.assertIn(str(path), git(repo, "worktree", "list", "--porcelain"))

    def test_detached_published_observer_commit_has_retaining_ref_after_removal(self):
        repo, path, observer = self.observer(committed=True)
        tip = git(path, "rev-parse", "HEAD")
        git(repo, "update-ref", "refs/remotes/origin/notes", tip)
        result = self.owner.cleanup_observer(observer)
        self.assertEqual(result["status"], "completed", result["reason"])
        self.assertFalse(path.exists())
        self.assertNotIn(str(path), git(repo, "worktree", "list", "--porcelain"))
        self.assertEqual(git(repo, "rev-parse", "refs/remotes/origin/notes"), tip)
        self.assertEqual(result["commit_proof"]["refs"], ["refs/remotes/origin/notes"])

    def test_detached_observer_local_user_ref_is_retention_without_publication(self):
        repo, path, observer = self.observer(committed=True)
        tip = git(path, "rev-parse", "HEAD")
        git(repo, "update-ref", "refs/heads/user-notes", tip)
        result = self.owner.cleanup_observer(observer)
        self.assertEqual(result["status"], "preserved", result["reason"])
        self.assertTrue(path.exists())
        self.assertEqual(git(repo, "rev-parse", "refs/heads/user-notes"), tip)

    def test_observer_existing_empty_root_branch_retains_disposable_head(self):
        repo, path, observer = self.observer()
        tip = git(repo, "rev-parse", "refs/heads/observers")
        result = self.owner.cleanup_observer(observer)
        self.assertEqual(result["status"], "completed", result["reason"])
        self.assertFalse(path.exists())
        self.assertEqual(git(repo, "rev-parse", "refs/heads/observers"), tip)
        self.assertEqual(result["commit_proof"]["publication"], "owned observer root")

    def test_observer_changed_root_branch_does_not_adopt_unpublished_user_commit(self):
        repo, path, observer = self.observer(committed=True)
        tip = git(path, "rev-parse", "HEAD")
        git(repo, "update-ref", "refs/heads/observers", tip)
        result = self.owner.cleanup_observer(observer)
        self.assertEqual(result["status"], "preserved", result["reason"])
        self.assertTrue(path.exists())

    def test_detached_observer_without_existing_root_ref_is_preserved(self):
        repo, path, observer = self.observer()
        git(repo, "update-ref", "-d", "refs/heads/observers")
        result = self.owner.cleanup_observer(observer)
        self.assertEqual(result["status"], "preserved", result["reason"])
        self.assertTrue(path.exists())

    def test_public_maintenance_cannot_readopt_changed_recorded_ref(self):
        key = self.request("archive")
        self.task["closed"] = True
        remove = self.owner._remove_workspace
        def interrupted(intent, repo):
            remove(intent, repo)
            raise KeyboardInterrupt("crash before ref settlement")
        with mock.patch.object(self.owner, "_remove_workspace", side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
            self.owner.replay_one(key)
        (self.repo / "file").write_text("replacement published work\n")
        git(self.repo, "commit", "--quiet", "-am", "replacement")
        newer = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/origin/main", newer)
        git(self.repo, "update-ref", "refs/heads/pipeline/sample-1", newer)
        for _ in range(2):
            result = self.maintenance()
            self.assertEqual(git(self.repo, "rev-parse", "refs/heads/pipeline/sample-1"), newer)
            self.assertEqual([r["status"] for r in result["replay"]], ["preserved"])
            self.assertIn("recorded ownership conflicts", result["residue"][0]["reason"])
            provenance = result["residue"][0]["recorded_owners"]
            self.assertEqual(provenance[0]["attempt_id"], "attempt-1")
            self.assertEqual(provenance[0]["identity"]["tip"], self.base)
        self.assertEqual(list(self.owner.journal.read()["intents"]), [key])

    def test_public_maintenance_keeps_replacement_after_unadmitted_disappearance(self):
        key = self.request("archive")
        self.task["closed"] = True
        git(self.repo, "worktree", "remove", str(self.workspace))
        (self.repo / "file").write_text("replacement\n")
        git(self.repo, "commit", "--quiet", "-am", "replacement")
        tip = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/origin/main", tip)
        git(self.repo, "update-ref", "refs/heads/pipeline/sample-1", tip)
        # ummanu-132: a workspace gone without removal evidence ends as terminal retention instead
        # of a perpetual pending refusal; the replacement ref and the recorded owner stay as they were.
        result = self.maintenance()
        self.assertIn("recorded ownership conflicts", result["residue"][0]["reason"])
        self.assertEqual([r["status"] for r in result["replay"]], ["preserved"])
        self.assertEqual(result["intents"][0]["terminal"], "workspace-disappeared")
        self.assertFalse(result["intents"][0]["progress"]["preservation_verified"])
        self.assertNotIn("workspace_removed", result["intents"][0]["progress"])
        self.assertEqual(git(self.repo, "rev-parse", "refs/heads/pipeline/sample-1"), tip)
        self.assertEqual(list(self.owner.journal.read()["intents"]), [key])

    def test_public_maintenance_does_not_replace_unproven_recorded_attempt(self):
        key = self.owner.journal.remember(self.task, self.record.to_json(), disposition="archive")
        self.task["closed"] = True
        git(self.repo, "worktree", "remove", str(self.workspace))
        result = self.maintenance()
        self.assertIn("recorded ownership conflicts", result["residue"][0]["reason"])
        self.assertEqual([r["status"] for r in result["replay"]], ["preserved"])
        self.assertEqual(git(self.repo, "rev-parse", "refs/heads/pipeline/sample-1"), self.base)
        self.assertEqual(list(self.owner.journal.read()["intents"]), [key])

    def test_public_maintenance_reuses_exact_pending_owner_and_retries(self):
        self.head(generation="")
        key = self.request("archive")
        self.task["closed"] = True
        with mock.patch("ummanu.dispatch.cleanup.git_worktree.remove", return_value=False):
            result = self.maintenance(expected_exit=1)
        self.assertEqual([r["status"] for r in result["replay"]], ["pending"])
        self.assertEqual(result["residue"][0]["cleanup_ids"], [key])
        result = self.maintenance()
        self.assertEqual([r["status"] for r in result["replay"]], ["completed"])
        self.assertEqual(list(self.owner.journal.read()["intents"]), [key])
        self.assertFalse(self.workspace.exists())
        self.assertEqual(self.maintenance()["replay"], [])

    def test_public_maintenance_reuses_owned_attempt_before_archival(self):
        self.head()
        key = self.owner.remember(self.task, self.record)
        self.task["closed"] = True
        self.assertEqual([r["status"] for r in self.maintenance()["replay"]], ["completed"])
        self.assertEqual(list(self.owner.journal.read()["intents"]), [key])

    def assert_observer_waits_for_failure(self, failure):
        self.head(generation="")
        self.task["claim"]["worker"] = self.record.worker
        key = self.request("close")
        _, path, observer = self.observer()
        with failure:
            result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "pending", result["reason"])
        self.assertTrue(result["progress"]["heads_stopped"])
        result = self.owner.cleanup_observer(observer)
        self.assertEqual(result["status"], "pending", result["reason"])
        self.assertTrue(path.exists())
        # The exact observer head stops now; only its workspace and completion wait for the card.
        self.assertIn(("observer-run", ""), self.stops)
        self.assertTrue(result["progress"]["heads_stopped"])
        self.assertEqual(result["progress"]["awaits_cards"], [key])
        self.owner.replay()
        card = self.owner.journal.read()["intents"][key]
        self.assertEqual(card["status"], "completed", card["reason"])
        result = self.owner.cleanup_observer(observer)
        self.assertEqual(result["status"], "completed", result["reason"])
        self.assertFalse(path.exists())
        self.assertIn(("observer-run", ""), self.stops)

    def test_observer_waits_for_refused_card_removal_then_replay(self):
        self.assert_observer_waits_for_failure(mock.patch(
            "ummanu.dispatch.cleanup.git_worktree.remove", return_value=False))

    def test_observer_waits_for_unreadable_card_git_then_replay(self):
        self.assert_observer_waits_for_failure(mock.patch.object(
            self.owner, "_dirty", side_effect=HostError("Git evidence unreadable")))

    def test_observer_waits_for_failed_card_ref_settlement_then_replay(self):
        self.assert_observer_waits_for_failure(mock.patch.object(
            self.owner, "_delete_branch", side_effect=HostError("ref transaction refused")))

    def test_observer_waits_for_failed_card_claim_settlement_then_replay(self):
        self.assert_observer_waits_for_failure(mock.patch.object(
            self.runtime.writer, "settle_cleanup_claim", side_effect=HostError("claim write refused")))

    def test_observer_waits_for_unverified_preservation_despite_settled_heads(self):
        key = self.owner.journal.remember(self.task, self.record.to_json(), disposition="close")
        result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "preserved")
        self.assertTrue(result["progress"]["heads_stopped"])
        self.assertTrue(result["progress"]["claim_settled"])
        self.assertFalse(result["progress"]["preservation_verified"])
        _, path, observer = self.observer()
        result = self.owner.cleanup_observer(observer)
        self.assertEqual(result["status"], "pending")
        self.assertTrue(path.exists())
        self.assertEqual(self.stops, [("observer-run", "")])
        self.assertEqual(result["progress"]["awaits_cards"], [key])

    def test_observer_can_follow_verified_dirty_card_preservation(self):
        self.head()
        self.task["claim"]["worker"] = self.record.worker
        (self.workspace / "notes").write_text("user notes")
        key = self.request("close")
        result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "preserved")
        self.assertTrue(result["progress"]["preservation_verified"])
        _, path, observer = self.observer()
        self.assertEqual(self.owner.cleanup_observer(observer)["status"], "completed")
        self.assertFalse(path.exists())
        self.assertEqual((self.workspace / "notes").read_text(), "user notes")
        self.assertIsNone(self.task["claim"]["worker"])

    def test_partial_git_directory_before_admin_removal_recovers_and_repeats(self):
        key = self.interrupt_git_directory_removal()
        result = CleanupOwner(self.runtime).replay_one(key)
        self.assertEqual(result["status"], "completed", result["reason"])
        self.assertTrue(result["progress"]["workspace_removed"])
        self.assertNotIn(str(self.workspace), git(self.repo, "worktree", "list", "--porcelain"))
        self.assertEqual(git(self.repo, "for-each-ref", "refs/heads/pipeline/"), "")
        self.assertIsNone(self.task["claim"]["worker"])
        self.assertEqual(CleanupOwner(self.runtime).replay_one(key), result)

    def test_missing_registered_directory_without_admission_is_not_adopted(self):
        self.task["claim"]["worker"] = self.record.worker
        key = self.request()
        shutil.rmtree(self.workspace)
        # ummanu-132: this was a pending refusal on every replay; it is now a terminal retention of
        # the exact registration. Still never adopted: no removal, no prune, no ref deletion.
        for _ in range(2):
            result = self.owner.replay_one(key)
            self.assertEqual(result["status"], "preserved", result["reason"])
            self.assertEqual(result["progress"]["terminal"]["kind"], "registration-without-directory")
            self.assertIn("no admitted removal proof", result["reason"])
            self.assertFalse(result["progress"].get("removal_started"))
            self.assertFalse(result["progress"]["preservation_verified"])
            self.assertNotIn("workspace_removed", result["progress"])
            self.assertNotIn("ref_removed", result["progress"])
        self.assertIn(str(self.workspace), git(self.repo, "worktree", "list", "--porcelain"))
        self.assertEqual(git(self.repo, "rev-parse", "refs/heads/pipeline/sample-1"), self.base)
        # The heads were stopped by the exact proof path first, so the Done card's claim is settled.
        self.assertTrue(result["progress"]["heads_stopped"])
        self.assertTrue(result["progress"]["claim_settled"])
        self.assertIsNone(self.task["claim"]["worker"])

    def test_partial_removal_rejects_substituted_admin_identity(self):
        key = self.interrupt_git_directory_removal()
        admin = Path(self.owner.journal.read()["intents"][key]["identity"]["admin"])
        old = admin.with_name(admin.name + "-original")
        admin.rename(old)
        shutil.copytree(old, admin)
        result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "pending", result["reason"])
        self.assertIn("admin identity changed", result["reason"])
        self.assertTrue(admin.exists())
        self.assertFalse(result["progress"].get("workspace_removed"))
        self.assertEqual(self.task["claim"]["worker"], self.record.worker)

    def test_partial_removal_rejects_changed_admin_path_mapping(self):
        key = self.interrupt_git_directory_removal()
        admin = Path(self.owner.journal.read()["intents"][key]["identity"]["admin"])
        (admin / "gitdir").write_text(str(self.root / "foreign" / ".git") + "\n")
        result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "pending", result["reason"])
        self.assertTrue(admin.exists())
        self.assertFalse(result["progress"].get("workspace_removed"))
        self.assertEqual(git(self.repo, "rev-parse", "refs/heads/pipeline/sample-1"), self.base)

    def test_partial_removal_rejects_changed_admin_head_and_retains_claim(self):
        key = self.interrupt_git_directory_removal()
        admin = Path(self.owner.journal.read()["intents"][key]["identity"]["admin"])
        (admin / "HEAD").write_text(self.base + "\n")
        result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "pending", result["reason"])
        self.assertIn("registration changed", result["reason"])
        self.assertTrue(admin.exists())
        self.assertEqual(self.task["claim"]["worker"], self.record.worker)

    def test_partial_removal_changed_ref_retains_replacement_and_claim(self):
        key = self.interrupt_git_directory_removal()
        (self.repo / "file").write_text("replacement work\n")
        git(self.repo, "commit", "--quiet", "-am", "replacement")
        tip = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/origin/main", tip)
        git(self.repo, "update-ref", "refs/heads/pipeline/sample-1", tip)
        result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "pending", result["reason"])
        self.assertIn("HEAD changed", result["reason"])
        self.assertEqual(git(self.repo, "rev-parse", "refs/heads/pipeline/sample-1"), tip)
        self.assertIn(str(self.workspace), git(self.repo, "worktree", "list", "--porcelain"))
        self.assertEqual(self.task["claim"]["worker"], self.record.worker)

    def test_partial_removal_revalidates_at_shared_native_effect(self):
        key = self.interrupt_git_directory_removal()
        admin = Path(self.owner.journal.read()["intents"][key]["identity"]["admin"])
        calls = []
        def capture(args, label):
            calls.append(args)
            if args[3:5] == ["worktree", "list"]:
                (admin / "HEAD").write_text(self.base + "\n")
            return subprocess.run(args, capture_output=True, text=True, check=False)
        self.host.run_capture = capture
        result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "pending", result["reason"])
        self.assertIn("registration changed", result["reason"])
        self.assertFalse(any(args[3:5] == ["worktree", "remove"] for args in calls))
        self.assertTrue(admin.exists())
        self.assertEqual(self.task["claim"]["worker"], self.record.worker)

    def test_partial_removal_unreadable_registration_retries_without_settlement(self):
        key = self.interrupt_git_directory_removal()
        with mock.patch("ummanu.dispatch.cleanup._registered", side_effect=HostError("registration unreadable")):
            result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "pending", result["reason"])
        self.assertEqual(self.task["claim"]["worker"], self.record.worker)
        self.assertEqual(self.owner.replay()[0]["status"], "completed")

    def test_shared_primitive_missing_directory_needs_owner_proof(self):
        self.interrupt_git_directory_removal()
        def run(args, cwd):
            return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False)
        self.assertFalse(git_worktree.remove(run, self.repo, self.workspace))
        self.assertIn(str(self.workspace), git(self.repo, "worktree", "list", "--porcelain"))

    def test_partial_removal_rejects_replacement_workspace_before_native_effect(self):
        key = self.interrupt_git_directory_removal()
        self.workspace.mkdir()
        (self.workspace / "NOTES.md").write_text("replacement user notes")
        result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "pending", result["reason"])
        self.assertEqual((self.workspace / "NOTES.md").read_text(), "replacement user notes")
        self.assertEqual(self.task["claim"]["worker"], self.record.worker)

    def test_partial_removal_rejects_symlink_admin_substitution(self):
        key = self.interrupt_git_directory_removal()
        admin = Path(self.owner.journal.read()["intents"][key]["identity"]["admin"])
        original = self.root / "retained-admin"
        admin.rename(original)
        admin.symlink_to(original, target_is_directory=True)
        result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "pending", result["reason"])
        self.assertIn("substituted", result["reason"])
        self.assertTrue(admin.is_symlink())
        self.assertTrue(original.exists())
        self.assertEqual(self.task["claim"]["worker"], self.record.worker)

    def test_partial_removal_lost_publication_stays_pending_with_registration(self):
        key = self.interrupt_git_directory_removal()
        git(self.repo, "update-ref", "-d", "refs/remotes/origin/main")
        result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "pending", result["reason"])
        self.assertFalse(result["progress"].get("preservation_verified"))
        self.assertIn(str(self.workspace), git(self.repo, "worktree", "list", "--porcelain"))
        self.assertEqual(self.task["claim"]["worker"], self.record.worker)
        git(self.repo, "update-ref", "refs/remotes/origin/main", self.base)
        self.assertEqual(self.owner.replay_one(key)["status"], "completed")

    def test_done_removes_merged_workspace_and_exact_local_branch(self):
        self.head()
        self.head("reviewer", "review-generation")
        result = self.owner.cleanup(self.task, self.record, "done")
        self.assertEqual(result["status"], "completed", result["reason"])
        self.assertFalse(self.workspace.exists())
        self.assertEqual(len(self.stops), 2)
        self.assertEqual(git(self.repo, "for-each-ref", "--format=%(refname)", "refs/heads/pipeline/"), "")
        self.assertEqual(git(self.repo, "rev-parse", "refs/remotes/origin/main"), self.base)
        self.assertTrue(result["progress"]["claim_settled"])

    def test_archive_request_survives_absent_active_record(self):
        self.head()
        self.owner.remember(self.task, self.record)
        self.task.update(closed=True, state="blocked")
        key = self.owner.journal.request(self.task, "archive")
        result = CleanupOwner(self.runtime).replay_one(key)
        self.assertEqual(result["status"], "completed", result["reason"])
        self.assertFalse(self.workspace.exists())

    def test_close_routes_to_same_owner_and_exposes_progress(self):
        self.owner.remember(self.task, self.record)
        self.task["closed"] = True
        self.owner.journal.request(self.task, "close")
        self.assertEqual(self.owner.replay()[0]["status"], "completed")
        self.assertEqual(self.owner.journal.summary(sprint="sprint:1")[0]["disposition"], "close")

    def test_crash_after_git_removal_before_ref_or_claim_settlement(self):
        self.task["claim"]["worker"] = self.record.worker
        key = self.request()
        remove = self.owner._remove_workspace
        def interrupted(intent, repo):
            remove(intent, repo)
            raise KeyboardInterrupt("crash after successful Git removal")
        with mock.patch.object(self.owner, "_remove_workspace", side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
            self.owner.replay_one(key)
        self.assertFalse(self.workspace.exists())
        result = CleanupOwner(self.runtime).replay_one(key)
        self.assertEqual(result["status"], "completed", result["reason"])
        self.assertIsNone(self.task["claim"]["worker"])

    def test_crash_after_exact_ref_deletion_replays_its_admitted_proof(self):
        key = self.request()
        delete = self.owner._delete_branch
        def interrupted(intent, repo, base):
            delete(intent, repo, base)
            raise KeyboardInterrupt("crash after successful ref transaction")
        with mock.patch.object(self.owner, "_delete_branch", side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
            self.owner.replay_one(key)
        self.assertEqual(CleanupOwner(self.runtime).replay_one(key)["status"], "completed")

    def test_prior_attempts_reusing_the_same_workspace_reconcile_after_later_cleanup(self):
        self.owner.remember(self.task, self.record)
        self.record.attempt_id = "attempt-2"
        self.assertEqual(self.owner.cleanup(self.task, self.record, "done")["status"], "completed")
        self.task["closed"] = True
        self.owner.journal.request(self.task, "close")
        results = self.owner.replay()
        self.assertEqual([item["status"] for item in results], ["completed"])

    def test_stop_failure_is_durable_and_automatically_retryable(self):
        self.head()
        self.stop_failure = True
        key = self.request()
        self.assertEqual(self.owner.replay_one(key)["status"], "pending")
        self.assertTrue(self.workspace.exists())
        self.stop_failure = False
        self.assertEqual(CleanupOwner(self.runtime).replay()[0]["status"], "completed")

    def test_removal_failure_is_not_a_clean_receipt(self):
        key = self.request()
        with mock.patch("ummanu.dispatch.cleanup.git_worktree.remove", return_value=False):
            result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "pending")
        self.assertTrue(self.workspace.exists())
        self.assertEqual(self.owner.replay()[0]["status"], "completed")

    def test_tracked_untracked_and_ignored_work_are_preserved(self):
        for name in ("file", "untracked", "ignored"):
            with self.subTest(name=name):
                path = self.workspace / name
                path.write_text("author work")
                result = self.owner.cleanup(self.task, self.record, "done")
                self.assertEqual(result["status"], "preserved", result["reason"])
                self.assertTrue(path.exists())
                if name == "file":
                    git(self.workspace, "restore", "file")
                else:
                    path.unlink()

    def test_generated_prompt_requires_exact_bytes(self):
        path = self.workspace / "TASK.md"
        path.write_text("generated")
        self.owner.journal.generated(path, "generated")
        path.write_text("user notes")
        key = self.request()
        self.assertEqual(self.owner.replay_one(key)["status"], "preserved")
        path.write_text("generated")
        self.assertEqual(self.owner.replay_one(key)["status"], "completed")

    def test_interrupted_owned_environment_deletion_retains_its_exact_proof(self):
        namespace = self.workspace / ".ummanu-task-env"
        namespace.mkdir()
        (namespace / "owner.json").write_text("dispatcher-owned")
        (namespace / "venv-file").write_text("generated")
        (self.repo / ".git" / "info" / "exclude").write_text(".ummanu-task-env/\n")
        def ownership(path):
            root = Path(path) / ".ummanu-task-env"
            if not root.exists():
                return "absent"
            if not (root / "owner.json").exists():
                raise HostError("environment owner unavailable")
            return "dispatcher"
        self.host._decide_workspace_environment_ownership = ownership
        key = self.request()
        def interrupted(path):
            (Path(path) / "owner.json").unlink()
            raise KeyboardInterrupt("interrupted environment removal")
        with mock.patch("ummanu.dispatch.cleanup.shutil.rmtree", side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
            self.owner.replay_one(key)
        result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "completed", result["reason"])
        self.assertFalse(self.workspace.exists())

    def pipeline_generated_workspace(self):
        """A merged, published Done workspace holding only what Ummanu's own pipeline wrote.

        Every artifact comes from its real producer: the environment claim, the install record,
        the prompt writer, a Python import and the broad-check writer under the head's cache env.
        """
        (self.workspace / "module.py").write_text("VALUE = 1\n")
        (self.workspace / "src" / "sample").mkdir(parents=True)
        (self.workspace / "src" / "sample" / "__init__.py").write_text("VALUE = 1\n")
        git(self.workspace, "add", "module.py", "src/sample/__init__.py")
        git(self.workspace, "commit", "--quiet", "-m", "work")
        tip = git(self.workspace, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/heads/main", tip)
        git(self.repo, "update-ref", "refs/remotes/origin/main", tip)
        host = CommandHostRuntime(SimpleNamespace(), self.data, mode="real",
                                  production_runtime=SimpleNamespace(interpreter=sys.executable))
        host._prepare_workspace_environment(str(self.workspace))
        namespace = host._workspace_environment(self.workspace).parent
        self.host._decide_workspace_environment_ownership = host._decide_workspace_environment_ownership
        before = host._workspace_extra_files(self.workspace)
        metadata = self.workspace / "src" / "sample.egg-info"
        metadata.mkdir(parents=True)
        (metadata / "PKG-INFO").write_text("Metadata-Version: 2.1\nName: sample\n")
        (metadata / "SOURCES.txt").write_text("module.py\n")
        host._record_install_output(self.workspace, before)
        host._write_prompt(self.workspace / "TASK.md", "# Task sample-1\n")
        caches = workspace_tool_cache_env(self.workspace)
        subprocess.run([sys.executable, "-c", "import module"], cwd=self.workspace, check=True,
                       env={**os.environ, **caches})
        code, _ = run_broad_check("true", root=self.workspace, stream=StringIO(),
                                  env={**os.environ, **caches})
        self.assertEqual(code, 0)
        # secretary-1922: a test's child of the workspace venv, given an environment built from scratch.
        subprocess.run([str(host._workspace_python(self.workspace)), "-c", "import sample"],
                       cwd=self.workspace, check=True, env={"PYTHONPATH": str(self.workspace / "src")})
        self.assertTrue(any((namespace / "pycache").rglob("module*.pyc")))
        self.assertTrue(any((namespace / "pycache").rglob("sample/__init__*.pyc")))
        self.assertTrue(any((namespace / "checks").glob("broad-*.json")))
        return namespace, metadata

    def test_pipeline_generated_artifacts_prove_a_done_workspace_clean(self):
        """secretary-1920: owned caches, recorded install output and the prompt are not author work.

        secretary-1922: that includes the bytecode of a clean-env child of the workspace venv.
        """
        self.pipeline_generated_workspace()
        key = self.request()
        result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "completed", result["reason"])
        self.assertFalse(self.workspace.exists())
        self.assertNotIn(str(self.workspace), git(self.repo, "worktree", "list", "--porcelain"))
        self.assertEqual(git(self.repo, "for-each-ref", "--format=%(refname)", "refs/heads/pipeline/"), "")

    def test_retention_floor_survives_pipeline_generated_artifacts(self):
        """secretary-1920: each piece of real work still keeps the workspace, named in the reason."""
        namespace, metadata = self.pipeline_generated_workspace()
        with (self.repo / ".git" / "info" / "exclude").open("a") as exclude:
            exclude.write("__pycache__/\n")
        key = self.request()
        ignored = self.workspace / "ignored"
        untracked = self.workspace / "notes.txt"
        egg = metadata / "SOURCES.txt"
        original = egg.read_text()
        owner_file = namespace / "owner.json"
        claim = owner_file.read_text()
        ownership = self.host._decide_workspace_environment_ownership

        def outside_cache():
            # ummanu-132: an ignored `__pycache__` of a Done card is a disposable cache now; any
            # other ignored file outside the owned namespace is still author work.
            ignored.write_text("author data\n")
            return ignored.unlink

        def untracked_file():
            untracked.write_text("author notes\n")
            return untracked.unlink

        def modified_install_output():
            egg.write_text("rewritten by a head\n")
            return lambda: egg.write_text(original)

        def unowned_namespace():
            # The host's ownership answer is not the dispatcher's: every namespace row is work.
            self.host._decide_workspace_environment_ownership = lambda path: "absent"
            return lambda: setattr(self.host, "_decide_workspace_environment_ownership", ownership)

        cases = (("!! ignored", outside_cache), ("?? notes.txt", untracked_file),
                 ("src/sample.egg-info/SOURCES.txt", modified_install_output),
                 ("!! .ummanu-task-env/", unowned_namespace))
        for named, introduce in cases:
            with self.subTest(named=named):
                restore = introduce()
                result = self.owner.replay_one(key)
                self.assertEqual(result["status"], "preserved", result["reason"])
                self.assertIn("dirty tracked, untracked or ignored work", result["reason"])
                self.assertIn(named, result["reason"])
                self.assertTrue(self.workspace.exists())
                restore()
        # A claim naming another workspace is refused before any effect, as before.
        owner_file.write_text(claim.replace(str(self.workspace), str(self.workspace) + "-other"))
        result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "pending", result["reason"])
        self.assertIn(".ummanu-task-env", result["reason"])
        self.assertTrue(self.workspace.exists())
        owner_file.write_text(claim)
        # Each refusal above was the only obstacle: restored, the same intent completes.
        self.assertEqual(self.owner.replay_one(key)["status"], "completed")
        self.assertFalse(self.workspace.exists())

    def test_unpublished_commits_preserve_workspace_and_ref(self):
        (self.workspace / "file").write_text("unpublished")
        git(self.workspace, "commit", "-am", "unpublished")
        tip = git(self.workspace, "rev-parse", "HEAD")
        result = self.owner.cleanup(self.task, self.record, "done")
        self.assertEqual(result["status"], "preserved")
        self.assertTrue(self.workspace.exists())
        self.assertEqual(git(self.repo, "rev-parse", "pipeline/sample-1"), tip)

    def test_published_unmerged_ref_is_retained_at_exact_tip(self):
        (self.workspace / "file").write_text("candidate")
        git(self.workspace, "commit", "-am", "candidate")
        tip = git(self.workspace, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/origin/pipeline/sample-1", tip)
        result = self.owner.cleanup(self.task, self.record, "done")
        self.assertEqual(result["status"], "preserved", result["reason"])
        self.assertFalse(self.workspace.exists())
        self.assertEqual(git(self.repo, "rev-parse", "pipeline/sample-1"), tip)

    def test_changed_head_and_branch_ref_are_preserved(self):
        key = self.request()
        (self.workspace / "file").write_text("changed")
        git(self.workspace, "commit", "-am", "changed")
        result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "pending")
        self.assertTrue(self.workspace.exists())

    def test_foreign_claim_and_replacement_precede_every_effect(self):
        self.head()
        key = self.request()
        self.task["claim"]["worker"] = "new-worker"
        self.assertEqual(self.owner.replay_one(key)["status"], "pending")
        self.assertEqual(self.stops, [])
        self.task["claim"]["worker"] = None
        replacement = self.record.to_json()
        replacement["attempt_id"] = "new-attempt"
        self.state({self.task["ref"]: replacement})
        self.assertEqual(self.owner.replay_one(key)["status"], "pending")
        self.assertEqual(self.stops, [])
        self.assertTrue(self.workspace.exists())

    def test_replaced_directory_and_symlink_are_refused(self):
        key = self.request()
        saved = self.workspace.with_name("retained")
        self.workspace.rename(saved)
        self.workspace.symlink_to(saved, target_is_directory=True)
        self.assertEqual(self.owner.replay_one(key)["status"], "pending")
        self.assertTrue(saved.exists())

    def test_registration_substitution_is_refused(self):
        key = self.request()
        (self.workspace / ".git").write_text("gitdir: " + str(self.repo / ".git") + "\n")
        self.assertEqual(self.owner.replay_one(key)["status"], "pending")
        self.assertTrue(self.workspace.exists())

    def test_shared_git_removal_refuses_foreign_registration_and_ignored_files(self):
        from ummanu.infra.git_worktree import remove
        def capture(args, cwd):
            return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False)
        (self.workspace / "ignored").write_text("author work")
        self.assertFalse(remove(capture, self.repo, self.workspace))
        (self.workspace / "ignored").unlink()
        (self.workspace / ".git").write_text("gitdir: " + str(self.repo / ".git") + "\n")
        self.assertFalse(remove(capture, self.repo, self.workspace))
        self.assertTrue(self.workspace.exists())

    def test_deployed_unscoped_head_uses_its_runtime_stop_and_identity_fence(self):
        self.head(generation="")
        seen = []
        self.host._guard_head_run = lambda run, role, **kwargs: seen.append(run.run_id)
        result = self.owner.cleanup(self.task, self.record, "done")
        self.assertEqual(result["status"], "completed", result["reason"])
        self.assertEqual(seen, ["run-worker"])
        self.assertEqual(self.stops, [("run-worker", "")])

    def test_foreign_workspace_and_missing_proof_are_reported(self):
        self.record.workspace = str(self.repo)
        result = self.owner.cleanup(self.task, self.record, "done")
        self.assertEqual(result["status"], "preserved")
        self.assertTrue(self.repo.exists())

    def test_missing_git_proof_still_settles_the_exact_recorded_head(self):
        self.head()
        self.record.workspace = str(self.repo)
        self.record.worker_head_run["workspace"] = str(self.repo)
        result = self.owner.cleanup(self.task, self.record, "inactive")
        self.assertEqual(result["status"], "preserved", result["reason"])
        self.assertTrue(result["progress"]["heads_stopped"])
        self.assertEqual(self.stops, [("run-worker", "generation-1")])
        self.assertTrue(self.repo.exists())

    def test_unscoped_stop_receipt_survives_crash_and_replay_without_a_second_stop(self):
        run = self.head(generation="")
        def stop(run, initiator):
            self.stops.append((run.run_id, run.scope_generation))
            return SimpleNamespace(ok=True, reason="", run=run.finishing(initiator).exited())
        self.backend.stop = stop
        key = self.request()
        with mock.patch.object(self.owner, "_remove_workspace", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.owner.replay_one(key)
        saved = self.owner.journal.read()["intents"][key]
        self.assertTrue(HeadRun.from_json(saved["heads"][-1]).settled)
        self.assertEqual(CleanupOwner(self.runtime).replay_one(key)["status"], "completed")
        self.assertEqual(self.stops, [(run.run_id, "")])

    def test_a_stop_receipt_for_another_generation_never_authorizes_git_effects(self):
        from dataclasses import replace
        run = self.head()
        settled = replace(run.finishing(StopInitiator(actor="test")).exited(), scope_generation="replacement")
        self.backend.stop = lambda *args: SimpleNamespace(ok=True, reason="", run=settled)
        result = self.owner.cleanup(self.task, self.record, "done")
        self.assertEqual(result["status"], "pending")
        self.assertIn("does not settle", result["reason"])
        self.assertTrue(self.workspace.exists())

    def test_a_bare_ok_stop_without_a_settled_run_is_pending(self):
        self.head()
        self.backend.stop = lambda *args: SimpleNamespace(ok=True)
        result = self.owner.cleanup(self.task, self.record, "done")
        self.assertEqual(result["status"], "pending")
        self.assertFalse(result["progress"].get("heads_stopped", False))
        self.assertTrue(self.workspace.exists())

    def test_a_settled_scoped_run_still_reaches_the_runtime_empty_proof_on_replay(self):
        self.head()
        key = self.request()
        with mock.patch.object(self.owner, "_remove_workspace", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.owner.replay_one(key)
        self.stop_failure = True
        result = CleanupOwner(self.runtime).replay_one(key)
        self.assertEqual(result["status"], "pending")
        self.assertFalse(result["progress"]["heads_stopped"])
        self.assertIn("has not verified", self.owner.journal.admission_refusal(self.task["ref"]))
        self.assertEqual(self.stops, [("run-worker", "generation-1"), ("run-worker", "generation-1")])
        self.assertTrue(self.workspace.exists())

    def test_unreadable_journal_never_adopts_residue(self):
        key = self.request()
        (self.owner.journal.path / "intents" / (key + ".json")).write_text("unreadable")
        with self.assertRaises(HostError):
            self.owner.replay()
        self.assertTrue(self.workspace.exists())

    def test_inventory_and_maintenance_replay_archived_branch_only_for_both_projects(self):
        git(self.repo, "worktree", "remove", str(self.workspace))
        second = self.root / "instance"
        git(self.root, "clone", "--quiet", str(self.repo), str(second))
        git(second, "branch", "pipeline/instance-2")
        self.catalog.bindings["instance"] = {"repo": str(second), "default_branch": "main"}
        self.tasks["instance-2"] = {**self.task, "id": "task-2", "ref": "instance-2", "project": "instance", "closed": True}
        self.task["closed"] = True
        inventory = self.owner.inventory()
        self.assertEqual(len(inventory["residue"]), 2)
        self.assertFalse(self.owner.journal.path.exists())
        rendered = self.residue_command()
        self.assertEqual(len(rendered["residue"]), 2)
        self.assertEqual([e["outcome"] for e in rendered["manifest"]], ["eligible", "eligible"])
        self.assertFalse(self.owner.journal.path.exists(), "public inventory must remain read-only")
        # No global batch: each project is replayed on its own, by its exact targets.
        for project in ("instance", "sample"):
            rendered = self.maintenance(project=project)
            self.assertEqual([x["status"] for x in rendered["replay"]], ["completed"])
        self.assertEqual(git(second, "for-each-ref", "--format=%(refname)", "refs/heads/pipeline/"), "")
        self.assertEqual(git(self.repo, "for-each-ref", "--format=%(refname)", "refs/heads/pipeline/"), "")

    def test_old_archived_worktree_without_runtime_proof_is_preserved(self):
        self.task["closed"] = True
        result = self.owner.inventory()
        self.assertIn("historical worktree", result["residue"][0]["reason"])
        self.assertTrue(self.workspace.exists())
        self.assertEqual(self.owner.replay(), [])

    def test_newer_scope_owner_is_fenced_before_any_stop(self):
        from ummanu.runtime.head.local_pty.scoped_lifecycle import ScopedHeadLifecycle
        self.head()
        key = self.request()
        root = self.root / "heads"
        run_dir = root / "other-run"
        run_dir.mkdir(parents=True)
        ScopedHeadLifecycle("other-run", 128, generation="new-generation").persist(
            run_dir, role="worker", task="card:sample-1", workspace=str(self.workspace))
        self.host._local_pty_root = lambda: root
        self.host.fence_cleanup_scopes = lambda *args: CommandHostRuntime.fence_cleanup_scopes(self.host, *args)
        result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "pending")
        self.assertIn("newer scope", result["reason"])
        self.assertEqual(self.stops, [])
        self.assertTrue(self.workspace.exists())

    def test_foreign_reviewer_heartbeat_refuses_before_worker_stop(self):
        self.head()
        self.head("reviewer")
        def guard(run, role, **kwargs):
            if role == "reviewer":
                raise HostError("reviewer heartbeat has mismatching identity")
        self.host._guard_head_run = guard
        result = self.owner.cleanup(self.task, self.record, "done")
        self.assertEqual(result["status"], "pending")
        self.assertEqual(self.stops, [])
        self.assertTrue(self.workspace.exists())

    def test_unreadable_scope_evidence_is_pending(self):
        key = self.request()
        root = self.root / "heads"
        run_dir = root / "unknown"
        run_dir.mkdir(parents=True)
        (run_dir / "scope-owner.json").write_text("unreadable")
        self.host._local_pty_root = lambda: root
        self.host.fence_cleanup_scopes = lambda *args: CommandHostRuntime.fence_cleanup_scopes(self.host, *args)
        self.assertEqual(self.owner.replay_one(key)["status"], "pending")
        self.assertTrue(self.workspace.exists())

    def test_scope_binding_and_path_substitution_refuse_before_stop(self):
        from ummanu.runtime.head.local_pty.scoped_lifecycle import ScopedHeadLifecycle
        run = self.head()
        root = self.root / "heads"
        directory = root / run.run_id
        directory.mkdir(parents=True)
        owner = ScopedHeadLifecycle(run.run_id, 128, generation=run.scope_generation)
        owner.persist(directory, role="worker", task="card:foreign", workspace=str(self.workspace))
        self.host._local_pty_root = lambda: root
        self.host.fence_cleanup_scopes = lambda *args: CommandHostRuntime.fence_cleanup_scopes(self.host, *args)
        key = self.request()
        result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "pending")
        self.assertIn("binding differs", result["reason"])
        evidence = owner.read_owner(directory)
        evidence["task"] = "card:sample-1"
        owner.update_owner(directory, evidence)
        owner_path = directory / "scope-owner.json"
        owner_path.rename(directory / "borrowed.json")
        owner_path.symlink_to(directory / "borrowed.json")
        result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "pending")
        self.assertEqual(self.stops, [])
        self.assertTrue(self.workspace.exists())

    def test_unknown_terminal_scope_needs_supported_native_disappearance_proof(self):
        from ummanu.runtime.head.local_pty.scoped_lifecycle import ScopedHeadLifecycle
        root = self.root / "heads"
        directory = root / "old-run"
        directory.mkdir(parents=True)
        ScopedHeadLifecycle("old-run", 128, generation="old").persist(
            directory, role="worker", task="card:sample-1", workspace=str(self.workspace))
        record = ScopedHeadLifecycle.read_owner(directory)
        record.update(launch_allowed=False, cleanup_complete=True)
        ScopedHeadLifecycle.update_owner(directory, record)
        self.host._local_pty_root = lambda: root
        self.host.fence_cleanup_scopes = lambda *args: CommandHostRuntime.fence_cleanup_scopes(self.host, *args)
        key = self.request()
        with mock.patch("ummanu.runtime.local_pty_head.runtime_scope_inventory", return_value=SimpleNamespace(
                errors={}, disappeared=set())):
            result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "pending")
        self.assertTrue(self.workspace.exists())
        with mock.patch("ummanu.runtime.local_pty_head.runtime_scope_inventory", return_value=SimpleNamespace(
                errors={}, disappeared={record["unit"]})):
            result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "completed", result["reason"])

    def test_replacement_admission_refuses_unsettled_old_heads(self):
        self.head()
        self.stop_failure = True
        self.owner.cleanup(self.task, self.record, "done")
        self.assertIn("has not verified head settlement", self.owner.journal.admission_refusal(self.task["ref"]))
        self.stop_failure = False
        self.owner.replay()
        self.assertEqual(self.owner.journal.admission_refusal(self.task["ref"]), "")

    def flush(self, records=None, events=None):
        """The dispatcher's real record flush, in real host mode, over this owner."""
        runtime = SimpleNamespace(host=CommandHostRuntime(FakeCatalog(), self.data, mode="real"),
                                  data_dir=self.data, cleanup=self.owner, reader=self.runtime.reader,
                                  production_state=ProductionState(self.data))
        if events is not None:
            save = runtime.production_state.save
            runtime.production_state.save = lambda payload: (events.append("state"), save(payload))
        DispatcherRuntime.save_records(runtime, {}, records or {self.task["ref"]: self.record})

    def journal_io(self):
        """Every journal read and write path, and the card read, as one recording."""
        calls = []
        stack = contextlib.ExitStack()
        for name in ("read", "load_intent", "save", "_replace", "remember"):
            original = getattr(CleanupJournal, name)
            stack.enter_context(mock.patch.object(
                CleanupJournal, name, autospec=True,
                side_effect=lambda *args, _name=name, _original=original, **kwargs: (
                    calls.append(_name), _original(*args, **kwargs))[1]))
        show = self.runtime.reader.show
        self.runtime.reader.show = lambda ref: (calls.append("card"), show(ref))[1]
        stack.callback(setattr, self.runtime.reader, "show", show)
        return stack, calls

    def test_unchanged_cleanup_projection_flush_does_no_journal_io(self):
        self.head()
        self.flush()
        key = self.owner._remembered[self.task["ref"]]["key"]
        stored = journal_bytes(self.owner.journal)
        stack, calls = self.journal_io()
        with stack:
            self.flush()
            # Telemetry only: progress stamps, other subsystems' evidence and the provider cursor.
            self.record.worker_progress_at = 1234.5
            self.record.gate_attestation = {"evidence": "changed"}
            run = dict(self.record.worker_head_run)
            run["fanout_policy"] = {**run["fanout_policy"], "provider_source": {
                "version": 1, "kind": "codex_session_event_jsonl", "state": "unbound", "root": "/codex/sessions",
                "baseline": [f"/codex/sessions/rollout-{n}.jsonl" for n in range(500)]}}
            self.record.worker_head_run = run
            self.flush()
        self.assertEqual(calls, [])
        self.assertEqual(journal_bytes(self.owner.journal), stored)
        self.assertEqual(self.owner.journal.intent(key)["record"]["worker_head_run"]["lifecycle"], "spawned")

    def test_lifecycle_change_is_journaled_before_the_state_save(self):
        run = self.head()
        self.flush()
        events = []
        replace = CleanupJournal._replace
        with mock.patch.object(CleanupJournal, "_replace", autospec=True,
                               side_effect=lambda journal, *args: (events.append("journal"),
                                                                   replace(journal, *args))[1]):
            self.record.worker_head_run = run.finishing(StopInitiator(actor="test")).exited().to_json()
            self.flush(events=events)
        self.assertEqual(events, ["journal", "state"])
        key = self.owner._remembered[self.task["ref"]]["key"]
        self.assertEqual(self.owner.journal.intent(key)["heads"][0]["lifecycle"], "exited")

    def test_new_attempt_and_concurrent_mutations_are_not_hidden_by_the_cache(self):
        self.head()
        self.flush()
        first = self.owner._remembered[self.task["ref"]]["key"]
        # Another producer replaces the stored intent with an older record of the same attempt.
        other = CleanupJournal(self.data)
        stale = other.intent(first)
        stale["record"] = {**stale["record"], "handle": "stale-handle"}
        other.save({"intents": {first: stale}})
        stack, calls = self.journal_io()
        with stack:
            self.flush()
        self.assertIn("load_intent", calls)
        self.assertNotEqual(other.intent(first)["record"].get("handle"), "stale-handle")
        # A concurrent settlement request is re-read and left as it is: nothing to write.
        other.request(self.task, "done", self.record.to_json())
        stack, calls = self.journal_io()
        with stack:
            self.flush()
        self.assertIn("load_intent", calls)
        self.assertNotIn("_replace", calls)
        self.assertEqual(other.intent(first)["disposition"], "done")
        # A new attempt is a new obligation: the card is read again and its own intent written.
        self.record.attempt_id = "attempt-2"
        stack, calls = self.journal_io()
        with stack:
            self.flush()
        self.assertIn("card", calls)
        second = self.owner._remembered[self.task["ref"]]["key"]
        self.assertNotEqual(second, first)
        self.assertEqual(other.intent(second)["record"]["attempt_id"], "attempt-2")

    def test_residue_views_answer_the_same_from_the_v1_journal_and_after_its_migration(self):
        self.head()
        self.task["claim"]["worker"] = self.record.worker
        key = self.request()
        inventory = self.owner.inventory(project="sample")
        rendered = self.residue_command(project="sample", expected_exit=1)
        summary = self.owner.journal.summary(sprint="sprint:1")
        refusal = self.owner.journal.admission_refusal(self.task["ref"])
        value = self.owner.journal.read()
        # The same obligations as the released single document, before its one migration.
        shutil.rmtree(self.owner.journal.path)
        self.owner.journal.legacy.write_text(json.dumps(
            {"version": 1, "intents": value["intents"], "generated": value["generated"],
             "replay_cursor": value["replay_cursor"]}, sort_keys=True))
        self.assertEqual(self.owner.inventory(project="sample"), inventory)
        self.assertEqual(self.residue_command(project="sample", expected_exit=1), rendered)
        self.assertEqual(self.owner.journal.summary(sprint="sprint:1"), summary)
        self.assertEqual(self.owner.journal.admission_refusal(self.task["ref"]), refusal)
        self.assertFalse(self.owner.journal.path.exists(), "residue views stay read-only")
        entry = self.entry(inventory, key)
        result = self.owner.replay_targets("sample", [(key, entry["digest"])])
        self.assertEqual(result[0]["status"], "completed", result[0]["reason"])
        self.assertTrue(self.owner.journal.path.is_dir())
        self.assertTrue(self.owner.journal.archive.exists())
        self.assertFalse(self.owner.journal.legacy.exists())

    def test_production_probe_cannot_replay_or_capture_durable_cleanup(self):
        from ummanu.dispatch.production import ProbeAbort, _probe_runtime
        self.request()
        before = journal_bytes(self.owner.journal)
        self.runtime.cleanup = self.owner
        self.runtime.production_state = SimpleNamespace()
        self.runtime.po = SimpleNamespace()
        probe = _probe_runtime(self.runtime)
        with self.assertRaises(ProbeAbort):
            probe.cleanup.replay()
        with self.assertRaises(ProbeAbort):
            probe.cleanup.cleanup(self.task, self.record, "inactive")
        with self.assertRaises(ProbeAbort):
            probe.cleanup.remember_record(self.task["ref"], self.record, lambda: self.task)
        self.assertEqual(journal_bytes(self.owner.journal), before)
        self.assertTrue(self.workspace.exists())

    def test_bounded_replay_rotates_past_persistent_failures(self):
        first = self.request()
        value = self.owner.journal.read()
        other = copy.deepcopy(value["intents"][first])
        second = "0" * 64 if first != "0" * 64 else "f" * 64
        value["intents"][second] = other
        self.owner.journal.save(value)
        with mock.patch.object(self.owner, "replay_one", return_value=other) as replay:
            self.owner.replay(limit=1)
            self.owner.replay(limit=1)
        self.assertEqual(set(call.args[0] for call in replay.call_args_list), {first, second})

    def test_tip_change_between_merge_proof_and_actual_deletion_is_fenced(self):
        key = self.request()
        original = self.owner._published
        calls = 0
        def change(repo, tip):
            nonlocal calls
            calls += 1
            if calls == 2:
                replacement = git(self.repo, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "replacement")
                git(self.repo, "update-ref", "refs/heads/pipeline/sample-1", replacement)
            return original(repo, tip)
        with mock.patch.object(self.owner, "_published", side_effect=change):
            result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "pending")
        self.assertNotEqual(git(self.repo, "rev-parse", "pipeline/sample-1"), self.base)

    def test_claim_admission_and_cleanup_share_one_critical_section(self):
        entered = threading.Event()
        release = threading.Event()
        def competing():
            with ownership_lock(self.data):
                entered.set()
            release.set()
        with ownership_lock(self.data):
            thread = threading.Thread(target=competing)
            thread.start()
            self.assertFalse(entered.wait(.05))
            self.assertEqual(self.owner.cleanup(self.task, self.record, "done")["status"], "completed")
        thread.join(2)
        self.assertTrue(release.is_set())

    def test_public_observer_stop_retains_unreadable_registration_and_retries(self):
        host = CommandHostRuntime(
            FakeCatalog(), self.data, mode="real",
            production_runtime=registered_production_runtime(self.root),
        )
        self.runtime.host = host
        self.runtime.sprints = SimpleNamespace(show=lambda *a, **k: {
            "id": "sprint-1", "ref": "sprint:1", "status": "closed"})
        host.cleanup_owner = self.owner
        path = Path(host.observer_workspace("sprint:1"))
        host._create_git_observer_workspace(path)
        run = HeadRun(run_id="observer-run", spec=HeadSpec(
            profile_id="test", adapter="unknown", runtime=LOCAL_PTY_RUNTIME),
            workspace=str(path), task_ref=TaskRef.sprint("sprint:1"), role="observer")
        observer = ObserverRecord(sprint="sprint:1", workspace=str(path), head_possible=True,
                                  head_run=run.to_json())
        self.backend.forget_head = mock.Mock()
        with mock.patch.object(host, "head_runtime_for", return_value=self.backend):
            self.stop_failure = True
            with self.assertRaisesRegex(HostError, "stop pending"):
                host.stop_observer(observer)
            self.stop_failure = False
            with (mock.patch("ummanu.dispatch.cleanup._registered",
                             side_effect=HostError("worktree registrations are unreadable")),
                  self.assertRaisesRegex(HostError, "unreadable")):
                host.stop_observer(observer)
            intent = next(iter(self.owner.journal.read()["intents"].values()))
            self.assertEqual(intent["status"], "pending")
            self.assertTrue(intent["progress"]["heads_stopped"])
            self.assertTrue(path.is_dir())
            self.assertTrue(host._git_observer_worktree_listed(str(path)))
            self.backend.forget_head.assert_not_called()
            host.stop_observer(observer)
        self.assertFalse(path.exists())
        self.assertFalse(host._git_observer_worktree_listed(str(path)))
        self.assertEqual(self.owner.journal.summary()[0]["status"], "completed")
        self.assertEqual(len(self.stops), 2, "retry reuses the second attempt's settled receipt")
        self.backend.forget_head.assert_called_once_with(run.run_id)

    def test_real_host_inactive_reconciliation_retains_failed_cleanup_and_retries(self):
        host = CommandHostRuntime(
            self.catalog, self.data, mode="real",
            production_runtime=registered_production_runtime(self.root),
        )
        self.runtime.host = host
        self.runtime.cleanup = self.owner
        host.cleanup_owner = self.owner
        self.head(generation="")
        records = {self.task["ref"]: self.record}
        with mock.patch.object(host, "head_runtime_for", return_value=self.backend):
            self.stop_failure = True
            refused = _reconcile_production(self.runtime, records, {}, set())
            self.assertEqual(refused[0]["status"], "pending")
            self.assertIn(self.task["ref"], records)
            self.assertTrue(self.workspace.is_dir())
            self.stop_failure = False
            retried = _reconcile_production(self.runtime, records, {}, set())
        self.assertEqual(retried[0]["status"], "completed")
        self.assertEqual(retried[1]["action"], "record-removed")
        self.assertEqual(records, {})
        self.assertFalse(self.workspace.exists())
        self.assertEqual(self.owner.journal.summary()[0]["status"], "completed")

    def test_recording_host_uses_inactive_head_lifecycle_without_claiming_git_ownership(self):
        host = FakeHost(self.data / "recording-workspaces")
        self.runtime.host = host
        self.runtime.cleanup = self.owner
        records = {self.task["ref"]: self.record}
        with mock.patch.object(self.owner, "cleanup") as cleanup:
            outcome = _reconcile_production(self.runtime, records, {}, set())
        cleanup.assert_not_called()
        self.assertEqual(host.calls, ["stop_workspace", "stop"])
        self.assertEqual(outcome[0]["action"], "record-removed")
        self.assertEqual(records, {})
        self.assertTrue(self.workspace.is_dir())
        self.assertFalse(self.owner.journal.path.exists())

    def test_observer_closed_handoff_waits_for_cards_and_preserves_user_work(self):
        self.request("close")
        repo = observer_root_repo(self.data)
        repo.mkdir(parents=True)
        git(repo, "init", "--quiet", "--initial-branch=observers")
        git(repo, "config", "user.name", "Test")
        git(repo, "config", "user.email", "test@example.invalid")
        git(repo, "commit", "--quiet", "--allow-empty", "-m", "root")
        path = self.data / "workspaces" / "observers" / "sprint-1"
        path.parent.mkdir(parents=True)
        git(repo, "worktree", "add", "--detach", str(path), "HEAD")
        (path / "NOTES.md").write_text("user notes")
        self.host.observer_workspace = lambda ref: str(path)
        self.runtime.sprints = SimpleNamespace(show=lambda *a, **k: {"id": "sprint-1", "ref": "sprint:1", "status": "closed"})
        observer = ObserverRecord(sprint="sprint:1", generation="observer-gen", workspace=str(path))
        state = self.data / "dispatcher" / "production-state.json"
        state.write_text(json.dumps({"records": {}, "observers": {"sprint:1": observer.to_json()}}))
        handoff = self.owner.journal.observer_handoff({"id": "sprint-1", "ref": "sprint:1"})
        self.assertIsNotNone(handoff)
        self.assertTrue(path.exists(), "close handoff must not execute the observer cleanup")
        result = self.owner.cleanup_observer(observer)
        self.assertEqual(result["status"], "pending")
        self.assertIn("waits for card cleanup", result["reason"])
        self.owner.replay()
        result = self.owner.cleanup_observer(observer)
        self.assertEqual(result["status"], "preserved", result["reason"])
        self.assertTrue((path / "NOTES.md").exists())

    # Close ownership and observer exit (secretary-1917).

    def preserved_done_intent(self):
        """The 1904 shape: an exact done attempt verified-preserved for dirty work."""
        self.head()
        self.task["claim"]["worker"] = self.record.worker
        (self.workspace / "notes").write_text("user notes")
        result = self.owner.cleanup(self.task, self.record, "done")
        self.assertEqual(result["status"], "preserved", result["reason"])
        self.assertTrue(result["progress"]["preservation_verified"])
        self.task["closed"] = True
        return next(iter(self.owner.journal.read()["intents"]))

    def legacy_close_intent(self):
        """The shape the old request staged with no record: no attempt, identity or head."""
        return self.owner.journal.remember(self.task, {}, disposition="close")

    def test_close_reuses_verified_preserved_done_intent(self):
        key = self.preserved_done_intent()
        before = copy.deepcopy(self.owner.journal.read()["intents"][key])
        self.assertEqual(self.owner.journal.request(self.task, "close"), key)
        intents = self.owner.journal.read()["intents"]
        self.assertEqual(list(intents), [key])
        self.assertEqual(intents[key], before)
        _, path, observer = self.observer()
        result = self.owner.cleanup_observer(observer)
        self.assertEqual(result["status"], "completed", result["reason"])
        self.assertFalse(path.exists())

    def test_close_and_archive_do_not_reopen_a_completed_intent(self):
        result = self.owner.cleanup(self.task, self.record, "done")
        self.assertEqual(result["status"], "completed", result["reason"])
        key = next(iter(self.owner.journal.read()["intents"]))
        self.task["closed"] = True
        for disposition in ("close", "archive"):
            self.assertEqual(self.owner.journal.request(self.task, disposition), key)
        intents = self.owner.journal.read()["intents"]
        self.assertEqual(list(intents), [key])
        self.assertEqual((intents[key]["status"], intents[key]["disposition"]), ("completed", "done"))
        self.assertEqual(self.owner.replay(), [])

    def test_request_stages_new_intent_only_without_any_intent(self):
        self.task["closed"] = True
        key = self.owner.journal.request(self.task, "close")
        self.assertEqual(list(self.owner.journal.read()["intents"]), [key])
        self.assertEqual(self.owner.journal.request(self.task, "archive"), key)
        self.assertEqual(list(self.owner.journal.read()["intents"]), [key])

    def test_existing_empty_duplicate_converges_to_its_attempt_owner(self):
        owner = self.preserved_done_intent()
        duplicate = self.legacy_close_intent()
        stops = list(self.stops)
        for _ in range(2):
            result = self.owner.replay_one(duplicate)
            expected = self.owner.journal.read()["intents"][owner]
            self.assertEqual((result["status"], result["reason"]), (expected["status"], expected["reason"]))
            self.assertEqual(result["progress"]["settled_by"], [owner])
            self.assertTrue(result["progress"]["preservation_verified"])
            self.assertTrue(result["progress"]["heads_stopped"])
            self.assertTrue(result["progress"]["claim_settled"])
        self.assertEqual(self.stops, stops, "a follower performs no effect of its own")
        self.assertEqual((self.workspace / "notes").read_text(), "user notes")
        _, path, observer = self.observer()
        self.assertEqual(self.owner.cleanup_observer(observer)["status"], "completed")
        self.assertFalse(path.exists())

    def test_empty_duplicate_follows_a_pending_owner_until_it_settles(self):
        self.task["claim"]["worker"] = self.record.worker
        owner = self.request("close")
        self.task["closed"] = True
        duplicate = self.legacy_close_intent()
        with mock.patch("ummanu.dispatch.cleanup.git_worktree.remove", return_value=False):
            self.owner.replay_one(owner)
        result = self.owner.replay_one(duplicate)
        self.assertEqual(result["status"], "pending")
        self.assertIn("follows attempt owner " + owner, result["reason"])
        self.owner.replay_one(owner)
        result = self.owner.replay_one(duplicate)
        self.assertEqual(result["status"], "completed", result["reason"])
        self.assertEqual(self.owner.replay(), [])

    def operation_record(self):
        git(self.repo, "worktree", "remove", str(self.workspace))
        git(self.repo, "branch", "-D", "pipeline/sample-1")
        return DispatcherRecord(worker="sample-1-operation", workspace="", handle="", head="", review_head="",
                                attempt_id="attempt-op", comment_baseline=0, review_baseline=0,
                                state="assessment", claimed_at=1)

    def test_operation_card_attempt_without_workspace_completes(self):
        record = self.operation_record()
        self.task.update(closed=True, claim={"worker": record.worker, "claimed_at": None})
        self.state({self.task["ref"]: record.to_json()})
        result = self.owner.cleanup(self.task, record, "inactive")
        self.assertEqual(result["status"], "pending")
        self.assertIn("awaits release of current record", result["reason"])
        self.assertTrue(result["progress"]["heads_stopped"])
        self.state({})
        result = self.owner.replay()[0]
        self.assertEqual(result["status"], "completed", result["reason"])
        self.assertIsNone(self.task["claim"]["worker"])
        self.assertEqual(self.stops, [])

    def test_operation_card_attempt_with_pipeline_ref_is_preserved_naming_it(self):
        record = self.operation_record()
        git(self.repo, "branch", "pipeline/sample-1")
        self.task["closed"] = True
        result = self.owner.cleanup(self.task, record, "inactive")
        self.assertEqual(result["status"], "preserved", result["reason"])
        self.assertIn("refs/heads/pipeline/sample-1 exists at " + self.base, result["reason"])
        self.assertTrue(result["progress"]["preservation_verified"])
        self.assertEqual(git(self.repo, "rev-parse", "refs/heads/pipeline/sample-1"), self.base)

    def test_attempt_without_workspace_but_with_a_head_field_is_not_completed(self):
        record = self.operation_record()
        record.worker_pid_file = "/nonexistent/pid"
        self.task["closed"] = True
        result = self.owner.cleanup(self.task, record, "inactive")
        self.assertEqual(result["status"], "pending")
        self.assertIn("head ownership is missing", result["reason"])
        record.worker_pid_file = ""
        record.review_head = "reviewer-profile"
        record.attempt_id = "attempt-op-2"
        result = self.owner.cleanup(self.task, record, "inactive")
        self.assertEqual(result["status"], "preserved")
        self.assertIn("missing exact workspace/attempt ownership proof", result["reason"])
        self.assertFalse(result["progress"]["preservation_verified"])

    def test_legacy_empty_intent_is_verified_preserved_without_effects(self):
        self.task["closed"] = True
        key = self.legacy_close_intent()
        result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "preserved", result["reason"])
        self.assertIn("no attempt ownership was recorded", result["reason"])
        self.assertIn("left to the inventory", result["reason"])
        self.assertTrue(result["progress"]["preservation_verified"])
        self.assertTrue(result["progress"]["heads_stopped"])
        self.assertEqual(result["heads"], [])
        self.assertEqual(self.stops, [])
        self.assertTrue(self.workspace.is_dir())
        self.assertIn(str(self.workspace), git(self.repo, "worktree", "list", "--porcelain"))
        self.assertEqual(git(self.repo, "rev-parse", "refs/heads/pipeline/sample-1"), self.base)
        before = obligations(self.owner.journal)
        self.owner.replay_one(key)
        self.assertEqual(obligations(self.owner.journal), before)
        _, _path, observer = self.observer()
        self.assertEqual(self.owner.cleanup_observer(observer)["status"], "completed")

    def test_legacy_empty_intent_waits_for_a_terminal_card(self):
        key = self.legacy_close_intent()
        self.task["state"] = "blocked"
        result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "pending")
        self.assertIn("closed or Done card", result["reason"])

    def test_legacy_empty_intent_live_pid_file_keeps_it_pending(self):
        self.task["closed"] = True
        key = self.legacy_close_intent()
        pid = self.root / "worker.pid"
        pid.write_text("1")
        with mock.patch("ummanu.dispatch.watchdog.pid_file_path", return_value=str(pid)), \
                mock.patch("ummanu.runtime.head.identity.head_process_status", return_value={"state": "alive"}):
            result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "pending")
        self.assertIn("pid file " + str(pid) + " names a live or unknown process", result["reason"])
        self.assertFalse(result["progress"]["heads_stopped"])

    def test_legacy_empty_intent_current_record_keeps_it_pending(self):
        self.task["closed"] = True
        key = self.legacy_close_intent()
        self.state({self.task["ref"]: self.record.to_json()})
        result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "pending")
        self.assertIn("another active owner", result["reason"])
        other = {**self.record.to_json(), "worker": "other-worker", "attempt_id": "other"}
        self.state({"other-1": other})
        result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "pending")
        self.assertIn("awaits release of current record other-1", result["reason"])
        self.assertTrue(self.workspace.is_dir())

    def test_legacy_empty_intent_foreign_claim_keeps_it_pending(self):
        self.task.update(closed=True, claim={"worker": "foreign-worker", "claimed_at": None})
        key = self.legacy_close_intent()
        result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "pending")
        self.assertIn("claim is foreign", result["reason"])
        self.assertEqual(self.task["claim"]["worker"], "foreign-worker")

    def test_public_observer_stop_exits_while_card_cleanup_is_pending(self):
        host = CommandHostRuntime(
            FakeCatalog(), self.data, mode="real",
            production_runtime=registered_production_runtime(self.root),
        )
        self.runtime.host = host
        self.runtime.sprints = SimpleNamespace(show=lambda *a, **k: {
            "id": "sprint-1", "ref": "sprint:1", "status": "closed"})
        host.cleanup_owner = self.owner
        self.task["claim"]["worker"] = self.record.worker
        card = self.request("close")
        self.task["closed"] = True
        with mock.patch("ummanu.dispatch.cleanup.git_worktree.remove", return_value=False):
            self.assertEqual(self.owner.replay_one(card)["status"], "pending")
        path = Path(host.observer_workspace("sprint:1"))
        host._create_git_observer_workspace(path)
        run = HeadRun(run_id="observer-run", spec=HeadSpec(
            profile_id="test", adapter="unknown", runtime=LOCAL_PTY_RUNTIME),
            workspace=str(path), task_ref=TaskRef.sprint("sprint:1"), role="observer")
        observer = ObserverRecord(sprint="sprint:1", workspace=str(path), head_possible=True,
                                  head_run=run.to_json())
        self.backend.forget_head = mock.Mock()
        with mock.patch.object(host, "head_runtime_for", return_value=self.backend):
            host.stop_observer(observer)
            self.backend.forget_head.assert_called_once_with(run.run_id)
            self.assertIn(("observer-run", ""), self.stops)
            key = next(k for k, i in self.owner.journal.read()["intents"].items() if i["task"].get("kind") == "observer")
            intent = self.owner.journal.read()["intents"][key]
            self.assertEqual(intent["status"], "pending", "completion is not published early")
            self.assertEqual(intent["progress"]["awaits_cards"], [card])
            self.assertIn(card, intent["reason"])
            self.assertTrue(path.is_dir())
            self.assertEqual(self.owner.replay_one(key)["status"], "pending")
            self.assertTrue(path.is_dir())
            self.assertEqual(self.owner.replay_one(card)["status"], "completed")
            result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "completed", result["reason"])
        self.assertNotIn("awaits_cards", result["progress"])
        self.assertFalse(path.exists())

    def test_observer_stop_failure_still_raises_while_cards_wait(self):
        self.task["claim"]["worker"] = self.record.worker
        self.request("close")
        _, _path, observer = self.observer()
        self.stop_failure = True
        result = self.owner.cleanup_observer(observer)
        self.assertEqual(result["status"], "pending")
        self.assertFalse(result["progress"]["heads_stopped"])
        self.assertNotIn("awaits_cards", result["progress"])

    def observer_key(self, record):
        import hashlib
        return hashlib.sha256(("sprint:1:" + record.generation + ":" + str(record.launches)).encode()).hexdigest()

    def replaced_observers(self):
        _repo, path, first = self.observer()
        first.launches, first.launched_at = 1, 100.0
        second_run = HeadRun(run_id="observer-run-2", spec=HeadSpec(
            profile_id="test", adapter="unknown", runtime=LOCAL_PTY_RUNTIME),
            workspace=str(path), task_ref=TaskRef.sprint("sprint:1"), role="observer")
        second = ObserverRecord(sprint="sprint:1", generation="observer-gen", launches=2, launched_at=200.0,
                                workspace=str(path), head_possible=True, head_run=second_run.to_json())
        state = self.data / "dispatcher" / "production-state.json"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({"records": {}, "observers": {"sprint:1": second.to_json()}}))
        fenced = []
        guarded = []
        self.host.fence_cleanup_scopes = lambda workspace, task, runs, recorded_only=False: fenced.append(
            ([r.run_id for r in runs], recorded_only))
        self.host._guard_head_run = lambda run, role, **kwargs: guarded.append(run.run_id)
        return path, first, second, fenced, guarded

    def test_predecessor_observer_intent_cannot_stop_the_replacement(self):
        path, first, _, fenced, guarded = self.replaced_observers()
        result = self.owner.cleanup_observer(first)
        self.assertEqual(result["status"], "preserved", result["reason"])
        self.assertIn("replaced by observer-gen:2", result["reason"])
        self.assertTrue(result["progress"]["preservation_verified"])
        self.assertTrue(result["progress"]["heads_stopped"])
        self.assertEqual(self.stops, [("observer-run", "")])
        self.assertEqual((fenced, guarded), ([(["observer-run"], True)], []))
        self.assertIsNone(self.owner.journal.read()["intents"][self.observer_key(first)]["identity"])
        self.assertTrue(path.is_dir())

    def test_replacement_observer_intent_cannot_stop_the_predecessor(self):
        path, _first, second, fenced, _ = self.replaced_observers()
        result = self.owner.cleanup_observer(second)
        self.assertEqual(result["status"], "completed", result["reason"])
        self.assertEqual(self.stops, [("observer-run-2", "")])
        self.assertEqual(fenced, [(["observer-run-2"], False)])
        self.assertFalse(path.exists())

    def test_replaced_launch_with_settled_run_settles_without_touching_successor(self):
        """The de9a574d shape: launch 1 already stopped, launch 2 current and live."""
        path, first, second, fenced, guarded = self.replaced_observers()
        run = HeadRun.from_json(first.head_run)
        first.head_run = run.finishing(StopInitiator(actor="test")).exited().to_json()
        task = {"id": "sprint-1", "ref": "sprint:1", "sprint": "sprint:1", "project": "observers",
                "kind": "observer", "claim": {}}
        raw = first.to_json()
        raw.update(attempt_id="observer-gen:1", worker="observer-gen")
        key = self.owner.journal.remember(task, raw, disposition="observer-stop")
        handoff = self.owner.journal.remember(task, {**second.to_json(), "attempt_id": "observer-gen:2",
                                                     "worker": "observer-gen"}, disposition="observer-close")
        for _ in range(2):
            result = self.owner.replay_one(key)
            self.assertEqual(result["status"], "preserved", result["reason"])
            self.assertIn("handed to the successor", result["reason"])
        self.assertEqual(self.stops, [])
        self.assertEqual((fenced, guarded), ([(["observer-run"], True)] * 2, []))
        self.assertTrue(path.is_dir())
        # With the current record gone, the later recorded launch is still the successor.
        (self.data / "dispatcher" / "production-state.json").write_text(json.dumps({"records": {}}))
        result = self.owner.replay_one(key)
        self.assertIn("replaced by observer-gen:2", result["reason"])
        self.assertTrue(path.is_dir())
        self.assertEqual(self.owner.journal.read()["intents"][handoff]["status"], "pending")

    def test_heads_list_is_bounded_over_replays_and_remembers(self):
        from dataclasses import replace
        self.head()
        (self.workspace / "notes").write_text("user notes")
        counter = iter(range(1000))
        def stop(run, initiator):
            self.stops.append((run.run_id, run.scope_generation))
            # The runtime re-proves a settled scoped run; each receipt differs in a non-key field.
            settled = run if run.settled else run.finishing(initiator).exited()
            return SimpleNamespace(ok=True, reason="", run=replace(settled, handle="h" + str(next(counter))))
        self.backend.stop = stop
        for index in range(5):
            self.record.worker_head_run["handle"] = "view-" + str(index)
            self.owner.remember(self.task, self.record)
        key = self.request("done")
        for _ in range(10):
            self.assertEqual(self.owner.replay_one(key)["status"], "preserved")
        heads = self.owner.journal.read()["intents"][key]["heads"]
        self.assertEqual(len(heads), 1)
        self.assertEqual(heads[0]["lifecycle"], "exited")
        self.assertEqual(len(self.stops), 10)

    def test_remembering_the_same_intent_again_does_not_rewrite_the_journal(self):
        journal = CleanupJournal(Path(self.enterContext(tempfile.TemporaryDirectory())))
        task = {"id": 7, "ref": "ummanu-7", "project": "ummanu"}
        record = {"attempt_id": "attempt-1", "worker_head_run": {"run_id": "run-1", "lifecycle": "running"}}
        value = journal.read()
        key, changed = journal.remember_into(value, task, record)
        self.assertTrue(changed)
        self.assertEqual(journal.remember_into(value, task, copy.deepcopy(record)), (key, False))
        journal.remember(task, record)
        with mock.patch.object(journal, "save", wraps=journal.save) as save:
            self.assertEqual(journal.remember(task, copy.deepcopy(record)), key)
            save.assert_not_called()
            moved = {**record, "worker_head_run": {"run_id": "run-1", "lifecycle": "exited"}}
            self.assertEqual(journal.remember_into(journal.read(), task, moved), (key, True))
            journal.remember(task, moved)
            save.assert_called_once()
        self.assertEqual(journal.read()["intents"][key]["heads"][0]["lifecycle"], "exited")

    def test_oversized_heads_list_compacts_on_next_checkpoint(self):
        worker = self.head().to_json()
        reviewer = self.head("reviewer", "review-generation").to_json()
        key = self.request()
        value = self.owner.journal.read()
        exited = HeadRun.from_json(worker).finishing(StopInitiator(actor="test")).exited().to_json()
        value["intents"][key]["heads"] = ([{**worker, "handle": str(n)} for n in range(200)] + [exited]
                                          + [{**worker, "handle": "stale"}] + [reviewer] * 100)
        self.owner.journal.save(value)
        heads = self.owner.journal.read()["intents"][key]["heads"]
        self.assertEqual([(h["run_id"], h["lifecycle"]) for h in heads],
                         [("run-worker", "exited"), ("run-reviewer", reviewer["lifecycle"])])

    def test_crash_after_stop_before_journal_save_retries_to_same_content(self):
        self.head()
        def stop(run, initiator):
            self.stops.append((run.run_id, run.scope_generation))
            return SimpleNamespace(ok=True, reason="", run=run if run.settled else run.finishing(initiator).exited())
        self.backend.stop = stop
        key = self.request()
        stop = self.owner._stop
        def interrupted(intent, **kwargs):
            stop(intent, **kwargs)
            raise KeyboardInterrupt("crash after stop before saving heads_stopped")
        with mock.patch.object(self.owner, "_stop", side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
            self.owner.replay_one(key)
        saved = self.owner.journal.read()["intents"][key]
        self.assertFalse(saved["progress"]["heads_stopped"])
        self.assertTrue(self.workspace.exists())
        result = CleanupOwner(self.runtime).replay_one(key)
        self.assertEqual(result["status"], "completed", result["reason"])
        self.assertEqual(len(result["heads"]), 1)
        self.assertFalse(self.workspace.exists())

    def test_replaying_a_preserved_intent_twice_keeps_the_journal_identical(self):
        key = self.preserved_done_intent()
        self.owner.replay_one(key)
        before = obligations(self.owner.journal)
        self.owner.replay_one(key)
        self.assertEqual(obligations(self.owner.journal), before)

    def scoped_predecessor(self, *, role="observer", task="sprint:1", workspace=None, completed=True):
        """Launch 1 as a scoped run under a real local-PTY runtime root, replaced by launch 2."""
        from dataclasses import replace

        from ummanu.runtime.head.identity import head_process_status
        from ummanu.runtime.head.local_pty.scoped_lifecycle import ScopedHeadLifecycle
        from ummanu.runtime.local_pty_head import LocalPtyHeadRuntime
        path, first, _second, _, _ = self.replaced_observers()
        heartbeat = self.root / "observer.pid"
        old = replace(HeadRun.from_json(first.head_run), scope_generation="old-scope", pid_file=str(heartbeat))
        first.head_run = old.finishing(StopInitiator(actor="test")).exited().to_json()
        first.pid_file = str(heartbeat)
        root = self.root / "heads"
        directory = root / old.run_id
        directory.mkdir(parents=True)
        scope = ScopedHeadLifecycle(old.run_id, 128, generation=old.scope_generation)
        scope.persist(directory, role=role, task=task, workspace=workspace or str(path))
        if completed:
            evidence = scope.read_owner(directory)
            evidence.update(launch_allowed=False, cleanup_complete=True)
            scope.update_owner(directory, evidence)
        read = []
        def identity(pid_file, **kwargs):
            read.append(pid_file)
            return head_process_status(pid_file, **kwargs)
        backend = LocalPtyHeadRuntime(root, head_process_status=identity, stop_timeout=0)
        self.host.head_runtime_for = lambda run: backend
        self.host._local_pty_root = lambda: root
        self.host.fence_cleanup_scopes = lambda *args, **kwargs: CommandHostRuntime.fence_cleanup_scopes(
            self.host, *args, **kwargs)
        return path, first, heartbeat, backend, read

    def test_replaced_observer_conflicting_scope_owner_refuses_before_any_stop_effect(self):
        from ummanu.runtime.head.local_pty.scoped_lifecycle import ScopedHeadLifecycle
        path, first, _, backend, _ = self.scoped_predecessor(
            role="worker", task="card:foreign", workspace="/foreign/workspace", completed=False)
        with mock.patch.object(backend, "_ask_to_stop") as ask, \
                mock.patch.object(ScopedHeadLifecycle, "stop_owned") as native:
            result = self.owner.cleanup_observer(first)
        self.assertEqual(result["status"], "pending", result["reason"])
        self.assertIn("binding differs from its recorded head", result["reason"])
        self.assertFalse(result["progress"]["heads_stopped"])
        self.assertEqual((ask.call_count, native.call_count), (0, 0))
        self.assertTrue(path.is_dir())

    def test_scoped_predecessor_settles_by_its_own_scope_not_the_shared_heartbeat(self):
        from ummanu.runtime.head.identity import head_process_status, publish_heartbeat
        from ummanu.runtime.head.local_pty.scoped_lifecycle import ScopedHeadLifecycle
        from ummanu.runtime.local_pty_head import MemoryScopeError
        path, first, heartbeat, _, read = self.scoped_predecessor()
        publish_heartbeat(str(heartbeat), {"run_id": "observer-run-2", "role": "observer", "task": "sprint:1"})
        with mock.patch.object(ScopedHeadLifecycle, "stop_owned",
                               side_effect=MemoryScopeError("scope still has descendants")) as native:
            result = self.owner.cleanup_observer(first)
        self.assertEqual(result["status"], "pending", result["reason"])
        self.assertIn("scope still has descendants", result["reason"])
        self.assertEqual(native.call_count, 1, "a retained exit receipt is not fresh scoped proof")
        with mock.patch.object(ScopedHeadLifecycle, "stop_owned") as native:
            result = self.owner.cleanup_observer(first)
        self.assertEqual(result["status"], "preserved", result["reason"])
        self.assertIn("replaced by observer-gen:2", result["reason"])
        self.assertTrue(result["progress"]["preservation_verified"])
        self.assertEqual(native.call_count, 1)
        self.assertNotIn(str(heartbeat), read)
        self.assertEqual(head_process_status(str(heartbeat))["record"]["run_id"], "observer-run-2")
        heads = result["heads"]
        self.assertEqual([(h["run_id"], h["pid_file"]) for h in heads], [("observer-run", str(heartbeat))])
        self.assertTrue(path.is_dir())

    def test_predecessor_cleanup_observer_never_reads_the_successor_workspace(self):
        from ummanu.dispatch.cleanup import _identity
        path, first, _, _, _ = self.replaced_observers()
        exists = Path.exists
        touched = []
        def watched(self_path, *args, **kwargs):
            touched.append(str(self_path))
            return exists(self_path, *args, **kwargs)
        with mock.patch("ummanu.dispatch.cleanup._identity", wraps=_identity) as identity, \
                mock.patch.object(Path, "exists", autospec=True, side_effect=watched):
            result = self.owner.cleanup_observer(first)
        self.assertEqual(result["status"], "preserved", result["reason"])
        self.assertEqual(identity.call_count, 0)
        self.assertNotIn(str(path), touched)
        self.assertIsNone(result["identity"])


    # secretary-1918: a re-stop of a settled reviewer once minted and committed a run it never
    # launched, naming the worker id as its card and the shared review pid file.
    def settled_reviewer(self, number):
        return HeadRun(run_id=f"run-reviewer-{number}", spec=HeadSpec(
            profile_id="test", adapter="unknown", runtime=LOCAL_PTY_RUNTIME),
            workspace=str(self.workspace), task_ref=TaskRef.card(self.task["ref"]), role="reviewer",
            scope_generation=f"run-reviewer-{number}", pid_file=self.record.review_pid_file,
        ).finishing(StopInitiator(actor="review-verdict")).exited()

    def placeholder(self, **fields):
        values = {"run_id": "placeholder-run", "spec": HeadSpec(
            profile_id="test", adapter="unknown", runtime=LOCAL_PTY_RUNTIME),
            "workspace": str(self.workspace), "task_ref": TaskRef.card(self.record.worker),
            "pid_file": self.record.review_pid_file}
        values.update(fields)
        return HeadRun(**values).finishing(StopInitiator(actor="review-verdict")).exited()

    def review_heartbeat(self, run_id, *, live=False):
        from ummanu.runtime.head.identity import publish_heartbeat
        identity = {"run_id": run_id, "role": "reviewer", "task": "card:" + self.task["ref"]}
        if live:
            publish_heartbeat(self.record.review_pid_file, identity)
            return
        process = subprocess.Popen(["sleep", "30"])
        try:
            publish_heartbeat(self.record.review_pid_file, identity, pid=process.pid)
        finally:
            process.kill()
            process.wait()

    def native_scoped_heads(self, *runs):
        """Each scoped run under a real local-PTY root, stopped by the real runtime and owner reader.

        The worker's owner is live, the reviewers' terminal. Only the supervisor's stop request
        and the scope's native termination are simulated; each asked stop lands in `self.stops`.
        """
        from ummanu.runtime.head.identity import head_process_status
        from ummanu.runtime.head.local_pty.scoped_lifecycle import ScopedHeadLifecycle
        from ummanu.runtime.local_pty_head import LocalPtyHeadRuntime
        root = self.data / "heads"
        for run in runs:
            directory = root / run.run_id
            directory.mkdir(parents=True, exist_ok=True)
            scope = ScopedHeadLifecycle(run.run_id, 128, generation=run.scope_generation)
            scope.persist(directory, role=run.role, task="card:" + run.task_ref.ref, workspace=run.workspace)
            if run.settled:
                evidence = scope.read_owner(directory)
                evidence.update(launch_allowed=False, cleanup_complete=True)
                scope.update_owner(directory, evidence)
        backend = LocalPtyHeadRuntime(root, head_process_status=head_process_status, stop_timeout=0)
        def ask(address, initiator, signal_name):
            self.stops.append(address.run_dir.name)
            return {"ok": True}
        for patcher in (mock.patch.object(backend, "_ask_to_stop", side_effect=ask),
                        mock.patch.object(ScopedHeadLifecycle, "stop_owned", autospec=True,
                                          side_effect=lambda owner, record: record.update(
                                              launch_allowed=False, cleanup_complete=True))):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.host.head_runtime_for = lambda run: backend
        self.host._local_pty_root = lambda: root
        self.host._guard_head_run = lambda run, role, **kwargs: CommandHostRuntime._guard_head_run(
            self.host, run, role, **kwargs)
        self.host.fence_cleanup_scopes = lambda *args, **kwargs: CommandHostRuntime.fence_cleanup_scopes(
            self.host, *args, **kwargs)
        return root

    def placeholder_intent(self, placeholder=None):
        """The live secretary-1917 shape: a live worker, two reviewer generations sharing one
        review pid file that names the dead round 2, and the placeholder between them."""
        self.task["claim"]["worker"] = self.record.worker
        self.record.worker_pid_file = str(self.root / "worker.pid")
        self.record.review_pid_file = str(self.root / "review.pid")
        self.record.review_handle = "review-handle"
        worker = replace(self.head(), pid_file=self.record.worker_pid_file)
        self.record.worker_head_run = worker.to_json()
        self.native_scoped_heads(worker, self.settled_reviewer(1), self.settled_reviewer(2))
        self.record.review_head_run = self.settled_reviewer(1).to_json()
        self.owner.remember(self.task, self.record)
        self.record.review_head_run = (placeholder or self.placeholder()).to_json()
        self.owner.remember(self.task, self.record)
        self.record.review_head_run = self.settled_reviewer(2).to_json()
        self.review_heartbeat("run-reviewer-2")
        return self.request()

    def test_settled_reviewer_placeholder_converges_through_replay(self):
        key = self.placeholder_intent()
        self.assertIn("placeholder-run", [h["run_id"] for h in self.owner.journal.read()["intents"][key]["heads"]])
        result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "completed", result["reason"])
        self.assertTrue(result["progress"]["heads_stopped"])
        # Both reviewer generations were addressed by their own run directories, though the shared
        # review pid file names only round 2. The exited placeholder was skipped, never stopped.
        self.assertEqual(sorted(self.stops), ["run-reviewer-1", "run-reviewer-2", "run-worker"])
        heads = {h["run_id"]: h for h in result["heads"]}
        self.assertEqual(heads["placeholder-run"]["lifecycle"], "exited")
        self.assertEqual(heads["run-worker"]["lifecycle"], "exited")
        self.assertEqual({heads[name]["pid_file"] for name in ("run-reviewer-1", "run-reviewer-2")},
                         {self.record.review_pid_file})
        self.assertEqual(heads["run-worker"]["pid_file"], self.record.worker_pid_file)
        self.assertFalse(self.workspace.exists())
        self.assertEqual(git(self.repo, "for-each-ref", "--format=%(refname)", "refs/heads/pipeline/"), "")
        self.assertIsNone(self.task["claim"]["worker"])
        journal = self.owner.journal.read()
        self.owner.replay_one(key)
        self.assertEqual(self.owner.journal.read(), journal)

    def test_settled_reviewer_placeholder_crash_after_stop_retries_to_same_result(self):
        key = self.placeholder_intent()
        stop = self.owner._stop
        def interrupted(intent, **kwargs):
            stop(intent, **kwargs)
            raise KeyboardInterrupt("crash after stop before saving heads_stopped")
        with mock.patch.object(self.owner, "_stop", side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
            self.owner.replay_one(key)
        self.assertFalse(self.owner.journal.read()["intents"][key]["progress"]["heads_stopped"])
        self.assertTrue(self.workspace.exists())
        result = CleanupOwner(self.runtime).replay_one(key)
        self.assertEqual(result["status"], "completed", result["reason"])
        self.assertEqual(sorted(h["run_id"] for h in result["heads"]),
                         ["placeholder-run", "run-reviewer-1", "run-reviewer-2", "run-worker"])
        self.assertFalse(self.workspace.exists())

    def assert_placeholder_refused(self, key):
        result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "pending")
        self.assertIn("another workspace or card", result["reason"])
        self.assertFalse(result["progress"]["heads_stopped"])
        self.assertEqual(self.stops, [])
        self.assertTrue(self.workspace.exists())

    def test_settled_reviewer_placeholder_with_a_foreign_card_still_refuses(self):
        self.assert_placeholder_refused(self.placeholder_intent(
            self.placeholder(task_ref=TaskRef.card("other-1-worker"))))

    def test_settled_reviewer_placeholder_in_another_workspace_still_refuses(self):
        self.assert_placeholder_refused(self.placeholder_intent(self.placeholder(workspace=str(self.repo))))

    def test_settled_reviewer_placeholder_with_a_run_directory_still_refuses(self):
        (self.data / "heads" / "placeholder-run").mkdir(parents=True)
        self.assert_placeholder_refused(self.placeholder_intent())

    def test_settled_reviewer_placeholder_named_by_a_live_pid_file_still_refuses(self):
        key = self.placeholder_intent()
        self.review_heartbeat("placeholder-run", live=True)
        self.assert_placeholder_refused(key)

    def test_exited_entry_with_a_scope_generation_still_refuses(self):
        self.assert_placeholder_refused(self.placeholder_intent(self.placeholder(scope_generation="placeholder-run")))

    def test_exited_entry_with_a_role_still_refuses(self):
        self.assert_placeholder_refused(self.placeholder_intent(self.placeholder(role="reviewer")))

    def test_scoped_generation_whose_own_heartbeat_names_another_run_still_refuses(self):
        from ummanu.runtime.head.identity import publish_heartbeat
        key = self.placeholder_intent()
        process = subprocess.Popen(["sleep", "30"])
        try:
            publish_heartbeat(str(self.data / "heads" / "run-reviewer-1" / "head.pid"),
                              {"run_id": "someone-else", "role": "reviewer", "task": "card:sample-1"},
                              pid=process.pid)
        finally:
            process.kill()
            process.wait()
        result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "pending")
        self.assertIn("does not match the scoped stop", result["reason"])
        self.assertFalse(result["progress"]["heads_stopped"])
        self.assertNotIn("run-reviewer-1", self.stops)
        self.assertTrue(self.workspace.exists())

    def test_scoped_generation_whose_own_heartbeat_is_live_and_foreign_refuses_before_any_stop(self):
        from ummanu.runtime.head.identity import publish_heartbeat
        key = self.placeholder_intent()
        publish_heartbeat(str(self.data / "heads" / "run-reviewer-1" / "head.pid"),
                          {"run_id": "someone-else", "role": "reviewer", "task": "card:sample-1"})
        result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "pending")
        self.assertIn("mismatching launch identity", result["reason"])
        self.assertFalse(result["progress"]["heads_stopped"])
        self.assertEqual(self.stops, [])
        self.assertTrue(self.workspace.exists())


    # secretary-1921: project- and target-scoped residue replay with a read-only effect manifest.

    def second_project(self):
        """A second registered binding with its own merged branch-only residue and a pending intent."""
        second = self.root / "instance"
        git(self.root, "clone", "--quiet", str(self.repo), str(second))
        git(second, "branch", "pipeline/instance-2")
        git(second, "update-ref", "refs/remotes/origin/main", self.base)
        self.catalog.bindings["instance"] = {"repo": str(second), "default_branch": "main"}
        other = {**self.task, "id": "task-2", "ref": "instance-2", "project": "instance", "closed": True}
        self.tasks["instance-2"] = other
        key = self.owner.journal.remember(other, {"attempt_id": "instance-attempt", "worker": "", "workspace": ""},
                                          disposition="archive")
        return second, key

    def branch_only(self, events):
        git(self.repo, "worktree", "remove", str(self.workspace))
        self.task["closed"] = True
        self.runtime.audit = SimpleNamespace(events=lambda ref: copy.deepcopy(events))
        return "refs/heads/pipeline/sample-1@" + self.base

    def entry(self, inventory, target):
        return next(entry for entry in inventory["manifest"] if entry["target"] == target)

    def recorded_reads(self):
        """Every Git repository and audit ref the cleanup owner reads while the context is open."""
        calls, refs = [], []
        native = subprocess.run
        def run(args, *rest, **kwargs):
            if args[:2] == ["git", "-C"]:
                calls.append(str(args[2]))
            return native(args, *rest, **kwargs)
        audit = self.runtime.audit
        self.runtime.audit = SimpleNamespace(events=lambda ref: refs.append(ref) or audit.events(ref))
        self.addCleanup(setattr, self.runtime, "audit", audit)
        return calls, refs, mock.patch("ummanu.dispatch.cleanup.subprocess.run", side_effect=run)

    def test_project_inventory_and_replay_never_read_or_write_the_other_binding(self):
        second, foreign = self.second_project()
        target = self.branch_only([{"kind": "card.started", "actor": {"role": "dispatcher", "id": "ummanu-production"}}])
        before = self.owner.journal.read()["intents"][foreign]
        calls, refs, patch = self.recorded_reads()
        with patch:
            inventory = self.owner.inventory(project="sample")
            self.assertEqual({row["project"] for row in inventory["residue"]}, {"sample"})
            self.assertEqual([item["ref"] for item in inventory["intents"]], [])
            self.assertEqual([entry["target"] for entry in inventory["manifest"]], [target])
            digest = self.entry(inventory, target)["digest"]
            result = self.owner.replay_targets("sample", [(target, digest)])
        self.assertEqual([item["status"] for item in result], ["completed"])
        self.assertTrue(calls)
        self.assertFalse([path for path in calls if path.startswith(str(second))], calls)
        self.assertNotIn("instance-2", refs)
        value = self.owner.journal.read()
        self.assertEqual(value["intents"][foreign], before)
        self.assertEqual({intent["task"]["project"] for intent in value["intents"].values()}, {"sample", "instance"})
        self.assertEqual(git(second, "for-each-ref", "--format=%(refname)", "refs/heads/pipeline/"),
                         "refs/heads/pipeline/instance-2")

    def test_unknown_project_is_refused_before_any_read(self):
        self.request()
        with mock.patch("ummanu.dispatch.cleanup.subprocess.run", side_effect=AssertionError("Git read")), \
                mock.patch.object(CleanupJournal, "read", side_effect=AssertionError("journal read")):
            with self.assertRaises(UnknownProject):
                self.owner.inventory(project="unknown")
            with self.assertRaises(UnknownProject):
                self.owner.replay_targets("unknown", [("0" * 64, "0" * 64)])
            result = self.residue_command(project="unknown", expected_exit=2)
        self.assertEqual(result["status"], "refused")
        self.assertIn("not registered: unknown", result["error"])

    def test_plain_inventory_reads_every_project_and_writes_nothing(self):
        second, foreign = self.second_project()
        key = self.request()
        value = self.owner.journal.read()
        value["replay_cursor"] = key
        self.owner.journal.save(value)
        before = journal_bytes(self.owner.journal)
        inventory = self.owner.inventory()
        rendered = self.residue_command(expected_exit=1)  # The requested intent is still pending.
        self.assertEqual({row["project"] for row in rendered["residue"]}, {"sample", "instance"})
        self.assertLessEqual({key, foreign}, {entry["target"] for entry in inventory["manifest"]})
        self.assertEqual(journal_bytes(self.owner.journal), before)
        self.assertEqual(self.stops, [])
        self.assertTrue(self.workspace.exists())
        self.assertEqual(git(second, "for-each-ref", "--format=%(refname)", "refs/heads/pipeline/"),
                         "refs/heads/pipeline/instance-2")

    def test_manifest_plans_every_admitted_effect_in_order_without_performing_one(self):
        self.head()
        self.task["claim"]["worker"] = self.record.worker
        key = self.request()
        before = journal_bytes(self.owner.journal)
        entry = self.entry(self.owner.inventory(project="sample"), key)
        self.assertEqual(entry["outcome"], "eligible", entry["reason"])
        self.assertEqual([effect["effect"] for effect in entry["effects"]],
                         ["stop-head", "remove-worktree", "delete-ref", "settle-claim"])
        stop, removal, deletion, claim = entry["effects"]
        self.assertEqual(stop["run_id"], "run-worker")
        self.assertEqual((removal["path"], removal["dirty"]), (str(self.workspace), "clean"))
        self.assertEqual(removal["identity"]["inode"], self.workspace.stat().st_ino)
        self.assertEqual(deletion, {"effect": "delete-ref", "ref": "refs/heads/pipeline/sample-1", "tip": self.base,
                                    "base": "refs/heads/main", "base_tip": self.base,
                                    "merged": True, "published": True})
        self.assertEqual(claim, {"effect": "settle-claim", "worker": self.record.worker, "board_write": True})
        # No effect and no journal write: the effects above would all be admitted by a replay.
        self.assertEqual(journal_bytes(self.owner.journal), before)
        self.assertEqual(self.stops, [])
        self.assertTrue(self.workspace.exists())
        self.assertEqual(git(self.repo, "rev-parse", "refs/heads/pipeline/sample-1"), self.base)
        self.assertEqual(self.tasks["sample-1"]["claim"]["worker"], self.record.worker)
        self.assertEqual(self.entry(self.owner.inventory(project="sample"), key)["digest"], entry["digest"])
        result = self.owner.replay_targets("sample", [(key, entry["digest"])])
        self.assertEqual([(item["status"], item["replayed"]) for item in result], [("completed", True)])
        self.assertFalse(self.workspace.exists())

    def test_replay_touches_only_its_named_target(self):
        _, foreign = self.second_project()
        key = self.request()
        value = self.owner.journal.read()
        other = "0" * 64 if key != "0" * 64 else "f" * 64
        value["intents"][other] = copy.deepcopy(value["intents"][key])
        value["intents"][other]["status"] = "pending"
        value["replay_cursor"] = other
        self.owner.journal.save(value)
        untouched = json.dumps({k: v for k, v in value.items() if k != "intents"}, sort_keys=True)
        others = {name: json.dumps(value["intents"][name], sort_keys=True) for name in (other, foreign)}
        digest = self.entry(self.owner.inventory(project="sample"), key)["digest"]
        # The other pending intent keeps the command pending; it is reported, never replayed.
        result = self.residue_command(replay=True, project="sample", targets=[key], digests=[digest], expected_exit=1)
        self.assertEqual([(item["target"], item["status"]) for item in result["replay"]], [(key, "completed")])
        value = self.owner.journal.read()
        self.assertEqual(json.dumps({k: v for k, v in value.items() if k != "intents"}, sort_keys=True), untouched)
        self.assertEqual({name: json.dumps(value["intents"][name], sort_keys=True) for name in others}, others)

    def test_replay_refuses_changed_unknown_and_foreign_targets_before_any_effect(self):
        _, foreign = self.second_project()
        self.head()
        key = self.request()
        before = journal_bytes(self.owner.journal)
        foreign_ref = "refs/heads/pipeline/instance-2@" + self.base
        result = self.owner.replay_targets("sample", [(key, "0" * 64), ("f" * 64, "0" * 64),
                                                      (foreign, "0" * 64), (foreign_ref, "0" * 64)])
        self.assertEqual([item["status"] for item in result], ["refused"] * 4)
        self.assertIn("digest differs", result[0]["reason"])
        self.assertTrue(all("unknown target" in item["reason"] for item in result[1:]))
        self.assertFalse(any(item["replayed"] for item in result))
        self.assertEqual(journal_bytes(self.owner.journal), before)
        self.assertEqual(self.stops, [])
        self.assertTrue(self.workspace.exists())
        # A digest read before the evidence changed no longer admits the target.
        digest = self.entry(self.owner.inventory(project="sample"), key)["digest"]
        (self.workspace / "notes").write_text("new author work\n")
        result = self.owner.replay_targets("sample", [(key, digest)])
        self.assertEqual(result[0]["status"], "refused")
        self.assertEqual(journal_bytes(self.owner.journal), before)
        with self.assertRaises(HostError):
            self.owner.replay_targets("sample", [(str(index), "0" * 64) for index in range(21)])
        with self.assertRaises(HostError):
            self.owner.replay_targets("sample", [(key, digest), (key, digest)])

    def test_global_replay_batch_no_longer_exists(self):
        self.request()
        before = journal_bytes(self.owner.journal)
        with mock.patch("ummanu.dispatch.bootstrap.runtime_from_args", side_effect=AssertionError("runtime")):
            for kwargs in ({}, {"project": "sample"}, {"targets": ["x"], "digests": ["y"]},
                           {"project": "sample", "targets": ["x"]}):
                args = argparse.Namespace(instance="unused", residue_replay=True, residue_inventory=False,
                                          project=kwargs.get("project"), target=kwargs.get("targets", []),
                                          manifest=kwargs.get("digests", []))
                with mock.patch("builtins.print") as output:
                    self.assertEqual(run_residue_maintenance(args), 2)
                self.assertEqual(json.loads(output.call_args.args[0])["status"], "refused")
        self.assertEqual(journal_bytes(self.owner.journal), before)
        self.assertTrue(self.workspace.exists())
        with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
            build_parser().parse_args(["instance-maintenance", "--residue-replay", "--limit", "20"])

    def test_dispatcher_card_started_proves_branch_only_ownership(self):
        target = self.branch_only([{"kind": "card.started", "actor": {"role": "dispatcher", "id": "ummanu-production"}}])
        inventory = self.owner.inventory(project="sample")
        self.assertEqual(inventory["residue"][0]["reason"], "owned branch-only residue; eligible for exact-tip replay")
        entry = self.entry(inventory, target)
        self.assertEqual(entry["outcome"], "eligible", entry["reason"])
        self.assertEqual([effect["effect"] for effect in entry["effects"]], ["delete-ref", "settle-claim"])
        self.assertFalse(self.owner.journal.path.exists())
        result = self.residue_command(replay=True, project="sample", targets=[target], digests=[entry["digest"]])
        self.assertEqual([item["status"] for item in result["replay"]], ["completed"])
        self.assertEqual(git(self.repo, "for-each-ref", "--format=%(refname)", "refs/heads/pipeline/"), "")

    def test_other_role_card_started_does_not_prove_ownership(self):
        target = self.branch_only([{"kind": "card.started", "actor": {"role": "po", "id": "po"}},
                                   {"kind": "card.moved", "actor": {"role": "dispatcher", "id": "ummanu-production"}}])
        entry = self.entry(self.owner.inventory(project="sample"), target)
        self.assertEqual((entry["outcome"], entry["reason"]), ("preserved", "card/project or audited claim proof missing"))
        result = self.owner.replay_targets("sample", [(target, entry["digest"])])
        self.assertEqual((result[0]["status"], result[0]["replayed"]), ("preserved", False))
        self.assertFalse(self.owner.journal.path.exists())
        self.assertEqual(git(self.repo, "rev-parse", "refs/heads/pipeline/sample-1"), self.base)

    def test_card_of_another_project_does_not_prove_ownership(self):
        target = self.branch_only([{"kind": "card.started", "actor": {"role": "dispatcher", "id": "ummanu-production"}}])
        self.task["project"] = "instance"
        entry = self.entry(self.owner.inventory(project="sample"), target)
        self.assertEqual((entry["outcome"], entry["reason"]),
                         ("preserved", "foreign or project-mismatch residue: card sample-1 belongs to project instance"))

    # Review round 1 of secretary-1921: the reviewer's reproductions, now asserting the corrected behavior.

    def test_branch_adoption_refuses_a_tip_absent_from_the_manifest(self):
        target = self.branch_only([{"kind": "card.started", "actor": {"role": "dispatcher"}}])
        entry = self.entry(self.owner.inventory(project="sample"), target)
        git(self.repo, "commit", "--quiet", "--allow-empty", "-m", "new integration")
        newer = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/heads/main", self.base)
        adopt = self.owner._adopt_branch
        def concurrent_ref_change(task, repo, ref, tip, **kwargs):
            # Another Git process advances the candidate and main after the digest matched.
            git(repo, "update-ref", "refs/heads/main", newer)
            git(repo, "update-ref", "refs/remotes/origin/main", newer)
            git(repo, "update-ref", ref, newer)
            return adopt(task, repo, ref, tip, **kwargs)
        with mock.patch.object(self.owner, "_adopt_branch", side_effect=concurrent_ref_change):
            result = self.owner.replay_targets("sample", [(target, entry["digest"])])
        self.assertEqual((result[0]["status"], result[0]["replayed"]), ("refused", False))
        self.assertIn("changed since the manifest was read", result[0]["reason"])
        self.assertFalse(self.owner.journal.path.exists(), "nothing is journaled for a changed target")
        self.assertEqual(git(self.repo, "rev-parse", "refs/heads/pipeline/sample-1"), newer)

    def test_ref_transaction_refuses_a_base_advanced_after_adoption(self):
        target = self.branch_only([{"kind": "card.started", "actor": {"role": "dispatcher"}}])
        entry = self.entry(self.owner.inventory(project="sample"), target)
        git(self.repo, "commit", "--quiet", "--allow-empty", "-m", "new integration")
        newer = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/heads/main", self.base)
        replay = self.owner.replay_one
        def advance_then_replay(key):
            git(self.repo, "update-ref", "refs/heads/main", newer)
            return replay(key)
        with mock.patch.object(self.owner, "replay_one", side_effect=advance_then_replay):
            result = self.owner.replay_targets("sample", [(target, entry["digest"])])
        self.assertEqual(result[0]["status"], "pending")
        self.assertIn("differs from the reviewed manifest", result[0]["reason"])
        self.assertEqual(git(self.repo, "rev-parse", "refs/heads/pipeline/sample-1"), self.base)

    def test_targeted_replay_keeps_an_unselected_foreign_intent_byte_identical(self):
        _, foreign = self.second_project()
        run = self.head().to_json()
        key = self.request()
        value = self.owner.journal.read()
        value["intents"][foreign]["heads"] = [run, copy.deepcopy(run)]
        # A stored intent file written before its next checkpoint (heads not yet compacted).
        stored = self.owner.journal.path / "intents" / (foreign + ".json")
        stored.write_text(json.dumps(value["intents"][foreign], sort_keys=True))
        before = stored.read_bytes()
        entry = self.entry(self.owner.inventory(project="sample"), key)
        result = self.owner.replay_targets("sample", [(key, entry["digest"])])
        self.assertEqual(result[0]["status"], "completed", result[0]["reason"])
        self.assertEqual(stored.read_bytes(), before)
        self.assertEqual(len(json.loads(stored.read_bytes())["heads"]), 2)

    def test_inventory_leaves_the_real_git_index_unrefreshed(self):
        key = self.request()
        index = Path(git(self.workspace, "rev-parse", "--absolute-git-dir")) / "index"
        before = index.read_bytes()
        file = self.workspace / "file"
        stat = file.stat()
        os.utime(file, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2000000000))
        entry = self.entry(self.owner.inventory(project="sample"), key)
        self.assertEqual(entry["outcome"], "eligible", entry["reason"])
        self.assertEqual(index.read_bytes(), before)

    def test_project_inventory_never_reads_a_foreign_cards_audit(self):
        self.second_project()
        git(self.repo, "branch", "pipeline/instance-2")
        _, refs, patch = self.recorded_reads()
        with patch:
            inventory = self.owner.inventory(project="sample")
        row = next(row for row in inventory["residue"] if row.get("ref") == "refs/heads/pipeline/instance-2")
        self.assertEqual(row["reason"], "foreign or project-mismatch residue: card instance-2 belongs to project instance")
        self.assertNotIn("instance-2", refs)
        entry = self.entry(inventory, "refs/heads/pipeline/instance-2@" + self.base)
        self.assertEqual(entry["outcome"], "preserved")

    def test_conflicting_foreign_owner_gets_a_scoped_refusal_entry(self):
        _, foreign = self.second_project()
        from ummanu.dispatch.cleanup import _identity
        value = self.owner.journal.read()
        value["intents"][foreign]["identity"] = _identity(self.repo, "", "pipeline/sample-1")
        self.owner.journal.save(value)
        before = journal_bytes(self.owner.journal)
        inventory = self.owner.inventory(project="sample")
        row = next(row for row in inventory["residue"] if row.get("ref") == "refs/heads/pipeline/sample-1")
        self.assertIn("recorded ownership conflicts", row["reason"])
        self.assertEqual(row["targets"], [])
        target = "refs/heads/pipeline/sample-1@" + self.base
        self.assertEqual([entry["target"] for entry in inventory["manifest"]], [target])
        entry = self.entry(inventory, target)
        self.assertEqual((entry["outcome"], entry["reason"]), ("preserved", row["reason"]))
        self.assertNotIn(foreign, json.dumps(inventory["manifest"]))
        result = self.owner.replay_targets("sample", [(target, entry["digest"]), (foreign, entry["digest"])])
        self.assertEqual([(item["status"], item["replayed"]) for item in result],
                         [("preserved", False), ("refused", False)])
        self.assertEqual(journal_bytes(self.owner.journal), before)
        self.assertTrue(self.workspace.exists())

    def test_ref_with_a_worktree_is_preserved_despite_dispatcher_card_started(self):
        self.task["closed"] = True
        self.runtime.audit = SimpleNamespace(events=lambda ref: [
            {"kind": "card.started", "actor": {"role": "dispatcher", "id": "ummanu-production"}}])
        target = "refs/heads/pipeline/sample-1@" + self.base
        entry = self.entry(self.owner.inventory(project="sample"), target)
        self.assertEqual(entry["outcome"], "preserved")
        self.assertIn("historical worktree", entry["reason"])
        self.assertEqual(self.owner.replay_targets("sample", [(target, entry["digest"])])[0]["replayed"], False)
        self.assertTrue(self.workspace.exists())

    def test_unmerged_or_unpublished_branch_only_ref_is_retained(self):
        target = self.branch_only([{"kind": "card.started", "actor": {"role": "dispatcher", "id": "ummanu-production"}}])
        git(self.repo, "checkout", "--quiet", "pipeline/sample-1")
        (self.repo / "file").write_text("candidate\n")
        git(self.repo, "commit", "--quiet", "-am", "candidate")
        tip = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "checkout", "--quiet", "main")
        git(self.repo, "update-ref", "refs/remotes/origin/candidate", tip)
        target = "refs/heads/pipeline/sample-1@" + tip
        entry = self.entry(self.owner.inventory(project="sample"), target)
        self.assertEqual((entry["outcome"], entry["reason"]), ("preserved", "unmerged candidate ref retained at " + tip))
        git(self.repo, "update-ref", "-d", "refs/remotes/origin/candidate")
        git(self.repo, "merge", "--quiet", "--ff-only", "pipeline/sample-1")
        entry = self.entry(self.owner.inventory(project="sample"), target)
        self.assertEqual(entry["outcome"], "preserved")
        self.assertIn("unpublished commits", entry["reason"])
        result = self.owner.replay_targets("sample", [(target, entry["digest"])])
        self.assertEqual((result[0]["status"], result[0]["replayed"]), ("preserved", False))
        self.assertIn("unpublished commits", result[0]["reason"])
        self.assertEqual(git(self.repo, "rev-parse", "refs/heads/pipeline/sample-1"), tip)
        self.assertFalse(self.owner.journal.path.exists())

    def test_dirty_work_is_itemized_in_manifest_and_replay_result(self):
        self.head()
        key = self.request()
        (self.workspace / "ignored").write_text("ignored author work\n")
        entry = self.entry(self.owner.inventory(project="sample"), key)
        self.assertEqual(entry["outcome"], "preserved")
        self.assertIn("dirty tracked, untracked or ignored work: !! ignored", entry["reason"])
        self.assertEqual([effect["effect"] for effect in entry["effects"]], ["stop-head", "settle-claim"])
        result = self.owner.replay_targets("sample", [(key, entry["digest"])])
        self.assertEqual(result[0]["status"], "preserved")
        self.assertIn("!! ignored", result[0]["reason"])
        self.assertTrue((self.workspace / "ignored").exists())
        self.assertEqual(git(self.repo, "rev-parse", "refs/heads/pipeline/sample-1"), self.base)

    def test_owned_attempt_of_a_terminal_card_plans_its_settlement_request_first(self):
        self.head()
        key = self.owner.remember(self.task, self.record)
        before = journal_bytes(self.owner.journal)
        self.task["state"] = "in_progress"
        entry = self.entry(self.owner.inventory(project="sample"), key)
        self.assertEqual((entry["outcome"], entry["reason"]), ("preserved", "card is still active"))
        self.assertEqual(self.owner.replay_targets("sample", [(key, entry["digest"])])[0]["replayed"], False)
        self.task["state"] = "done"
        self.task["closed"] = True
        entry = self.entry(self.owner.inventory(project="sample"), key)
        self.assertEqual(entry["effects"][0], {"effect": "request-settlement", "disposition": "archive"})
        self.assertEqual(journal_bytes(self.owner.journal), before)
        result = self.owner.replay_targets("sample", [(key, entry["digest"])])
        self.assertEqual(result[0]["status"], "completed", result[0]["reason"])
        self.assertEqual(self.owner.journal.read()["intents"][key]["disposition"], "archive")

    # -- ummanu-132: terminal dead ends, the hourly retry policy and disposable Done caches ---------

    def manual_clock(self):
        """Pin the clock of this owner and of every owner built later from the same runtime."""
        clock = ManualClock()
        self.runtime.cleanup_clock = clock
        self.owner.clock = clock
        self.tolerant_stop()
        return clock

    def tolerant_stop(self):
        """A stop that, like the runtime's, settles an already exited run again without minting one."""
        def stop(run, initiator):
            self.stops.append((run.run_id, run.scope_generation))
            return SimpleNamespace(ok=not self.stop_failure, reason="simulated stop failure",
                                   run=run if run.settled else run.finishing(initiator).exited())
        self.backend.stop = stop

    def intent_files(self):
        return {name: body for name, body in journal_bytes(self.owner.journal).items()
                if name.startswith("intents/")}

    def card(self, ref, *, project="sample", repo=None, head=True):
        """Another Done card of `project` with its own exact workspace, branch, head and request."""
        task = {**copy.deepcopy(self.task), "id": "task-" + ref, "ref": ref, "project": project}
        self.tasks[ref] = task
        workspace = self.data / "workspaces" / project / (ref + "-worker")
        workspace.parent.mkdir(parents=True, exist_ok=True)
        git(repo or self.repo, "worktree", "add", "--quiet", "-b", "pipeline/" + ref, str(workspace), "main")
        record = DispatcherRecord(worker=ref + "-worker", workspace=str(workspace), handle="", head="test",
                                  review_head="test", attempt_id="attempt-" + ref, comment_baseline=0,
                                  review_baseline=0, state="assessment", claimed_at=1)
        if head:
            record.worker_head_run = HeadRun(
                run_id="run-" + ref, spec=HeadSpec(profile_id="test", adapter="unknown", runtime=LOCAL_PTY_RUNTIME),
                workspace=str(workspace), task_ref=TaskRef.card(ref), role="worker",
                scope_generation="generation-1").to_json()
        task["claim"] = {"worker": record.worker, "claimed_at": None}
        self.owner.remember(task, record)
        key = self.owner.journal.request(task, "done", record.to_json())
        return key, workspace, record

    def assert_terminal(self, result, kind):
        self.assertEqual(result["status"], "preserved", result["reason"])
        self.assertEqual(result["progress"]["terminal"]["kind"], kind)
        self.assertEqual(result["progress"]["terminal"]["reason"], result["reason"])
        # A dead end is neither a verified retention nor a removal: no such flag is set for it.
        self.assertFalse(result["progress"]["preservation_verified"])
        for flag in ("workspace_removed", "ref_started", "ref_removed", "removal_started", "ref_delete_admitted"):
            self.assertNotIn(flag, result["progress"])
        self.assertTrue(result["progress"]["heads_stopped"])
        self.assertTrue(result["progress"]["claim_settled"])

    def test_disappeared_workspace_ends_terminal_and_no_later_replay_touches_it(self):
        clock = self.manual_clock()
        self.head()
        self.task["claim"]["worker"] = self.record.worker
        key = self.request()
        git(self.repo, "worktree", "remove", str(self.workspace))  # Gone outside the owner: no proof.
        result = self.owner.replay_one(key)
        self.assert_terminal(result, "workspace-disappeared")
        self.assertIn("without removal evidence", result["reason"])
        self.assertEqual(self.stops, [("run-worker", "generation-1")])
        self.assertIsNone(self.task["claim"]["worker"])
        # The ref, the recorded identity and the heads are retained exactly.
        self.assertEqual(git(self.repo, "rev-parse", "refs/heads/pipeline/sample-1"), self.base)
        stored = self.owner.journal.intent(key)
        self.assertEqual(stored["identity"]["workspace"], str(self.workspace))
        self.assertEqual([head["run_id"] for head in stored["heads"]], ["run-worker"])
        # Readers see the honest outcome: summary, manifest, residue, launch admission.
        row = self.owner.journal.summary()[0]
        self.assertEqual((row["status"], row["terminal"], row["next_attempt_at"]),
                         ("preserved", "workspace-disappeared", None))
        before = self.intent_files()
        inventory = self.owner.inventory(project="sample")
        entry = self.entry(inventory, key)
        self.assertEqual((entry["outcome"], entry["effects"]), ("preserved", []))
        self.assertIn("without removal evidence", entry["reason"])
        self.assertEqual(inventory["residue"][0]["recorded_owners"][0]["status"], "preserved")
        self.assertEqual(self.owner.journal.admission_refusal("sample-1"), "")
        # Neither automatic replay, a restart, a targeted replay nor a close acts on it again.
        calls, _refs, reads = self.recorded_reads()
        clock.advance(10 * RETRY_COOLDOWN)
        with reads:
            self.assertEqual(self.owner.replay(), [])
            self.assertEqual(CleanupOwner(self.runtime).replay_one(key)["status"], "preserved")
            self.assertEqual(self.owner.cleanup(self.task, self.record, "done")["status"], "preserved")
        self.assertEqual(calls, [])
        self.assertEqual(self.stops, [("run-worker", "generation-1")])
        self.assertEqual(self.intent_files(), before)
        replayed = self.maintenance()["replay"]
        self.assertEqual([(r["replayed"], r["status"]) for r in replayed], [(False, "preserved")])
        self.assertIn("terminal workspace-disappeared", replayed[0]["reason"])
        self.assertEqual(self.intent_files(), before)
        # The observer's closeout no longer waits forever on this dead end.
        _, path, observer = self.observer()
        self.assertEqual(self.owner.cleanup_observer(observer)["status"], "completed")
        self.assertFalse(path.exists())

    def test_unregistered_project_ends_terminal_without_reading_any_repository(self):
        self.manual_clock()
        self.head()
        self.task["claim"]["worker"] = self.record.worker
        key = self.request()
        # The follower: a duplicate empty-attempt obligation of the same card.
        follower = self.owner.journal.remember(self.task, {}, disposition="close")
        del self.catalog.bindings["sample"]
        calls, _refs, reads = self.recorded_reads()
        with reads:
            result = self.owner.replay_one(key)
        self.assert_terminal(result, "project-unregistered")
        self.assertEqual(calls, [], "no repository is searched for or read")
        self.assertTrue(self.workspace.is_dir())
        self.assertIn(str(self.workspace), git(self.repo, "worktree", "list", "--porcelain"))
        self.assertEqual(git(self.repo, "rev-parse", "refs/heads/pipeline/sample-1"), self.base)
        followed = self.owner.replay_one(follower)
        self.assertEqual(followed["status"], "preserved", followed["reason"])
        self.assertEqual(followed["progress"]["terminal"], {"kind": "follows", "owners": [key],
                                                            "reason": followed["reason"]})
        self.assertFalse(followed["progress"]["preservation_verified"])
        self.owner.clock.advance(5 * RETRY_COOLDOWN)
        before = journal_bytes(self.owner.journal)
        self.assertEqual(self.owner.replay(), [])
        self.assertEqual(journal_bytes(self.owner.journal), before)

    def test_registered_but_disabled_or_unreadable_catalog_stays_retryable(self):
        self.manual_clock()
        self.head()
        key = self.request()
        binding = self.catalog.bindings.pop("sample")
        self.catalog.registered_bindings = {"sample": binding}  # registered, not enabled
        def disabled(project):
            raise HostError(f"project {project!r} is registered but not enabled for workloads")
        self.catalog.binding = disabled
        result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "pending", result["reason"])
        self.assertNotIn("terminal", result["progress"])
        self.assertTrue(self.workspace.is_dir())
        # A catalog without a readable registration table is the binding's own refusal too.
        self.owner.clock.advance(RETRY_COOLDOWN)
        self.catalog.registered_bindings = None
        self.catalog.bindings = None
        result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "pending", result["reason"])
        self.assertNotIn("terminal", result["progress"])
        self.assertEqual(self.owner.journal.summary()[0]["next_attempt_at"], self.owner.clock.now + RETRY_COOLDOWN)

    def test_terminal_classification_never_overrides_a_fence(self):
        """Each refusal keeps the disappeared workspace's obligation pending, its claim and ref intact."""
        clock = self.manual_clock()
        self.head()
        self.task["claim"]["worker"] = self.record.worker
        key = self.request()
        git(self.repo, "worktree", "remove", str(self.workspace))
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "data").write_text("foreign bytes\n")

        def live_head():
            self.stop_failure = True
            return lambda: setattr(self, "stop_failure", False)

        def foreign_claim():
            self.tasks["sample-1"]["claim"] = {"worker": "other-worker", "claimed_at": 7}
            return lambda: self.tasks["sample-1"].update(claim={"worker": self.record.worker, "claimed_at": None})

        def active_owner():
            self.state({"sample-1": {"attempt_id": "attempt-2", "worker": "sample-1-worker",
                                     "workspace": str(self.workspace)}})
            return lambda: (self.data / "dispatcher" / "production-state.json").unlink()

        def active_card():
            self.tasks["sample-1"]["state"] = "in_progress"
            return lambda: self.tasks["sample-1"].update(state="done")

        def symlinked_workspace():
            self.workspace.symlink_to(outside)
            return self.workspace.unlink

        def unreadable_registry():
            patch = mock.patch("ummanu.dispatch.cleanup._registered",
                               side_effect=HostError("cleanup worktree registrations are unreadable"))
            patch.start()
            return patch.stop

        def retained_admin():
            admin = Path(self.owner.journal.intent(key)["identity"]["admin"])
            admin.mkdir(parents=True)
            return lambda: shutil.rmtree(admin)

        cases = (("live or unknown head", live_head), ("foreign claim", foreign_claim),
                 ("another active owner", active_owner), ("card still admitted", active_card),
                 ("substituted workspace", symlinked_workspace), ("unreadable registry", unreadable_registry),
                 ("admin entry reappeared", retained_admin))
        for named, introduce in cases:
            with self.subTest(named=named):
                restore = introduce()
                try:
                    result = self.owner.replay_one(key)
                finally:
                    restore()
                self.assertEqual(result["status"], "pending", result["reason"])
                self.assertNotIn("terminal", result["progress"])
                self.assertFalse(result["progress"].get("claim_settled"))
                self.assertEqual(self.tasks["sample-1"]["claim"]["worker"], self.record.worker)
                self.assertEqual(git(self.repo, "rev-parse", "refs/heads/pipeline/sample-1"), self.base)
                self.assertEqual((outside / "data").read_text(), "foreign bytes\n")
                # A retryable refusal is never lost: it is due again after one cooldown.
                self.assertEqual(self.owner.journal.summary()[0]["next_attempt_at"], clock.now + RETRY_COOLDOWN)
                clock.advance(RETRY_COOLDOWN)
        result = self.owner.replay_one(key)
        self.assert_terminal(result, "workspace-disappeared")

    def test_missing_directory_with_a_substituted_admin_stays_a_refusal(self):
        self.manual_clock()
        self.task["claim"]["worker"] = self.record.worker
        key = self.request()
        shutil.rmtree(self.workspace)
        admin = Path(self.owner.journal.intent(key)["identity"]["admin"])
        old = admin.with_name(admin.name + "-original")
        admin.rename(old)
        shutil.copytree(old, admin)
        result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "pending", result["reason"])
        self.assertIn("admin identity changed", result["reason"])
        self.assertNotIn("terminal", result["progress"])
        self.assertEqual(self.task["claim"]["worker"], self.record.worker)
        self.assertTrue(admin.exists() and old.exists())

    def test_retry_waits_one_cooldown_across_restart_request_and_crash(self):
        clock = self.manual_clock()
        self.head()
        self.stop_failure = True
        key = self.request()
        first = self.owner.replay_one(key)
        self.assertEqual(first["status"], "pending")
        self.assertEqual(first["retry"], {"last_attempt_at": clock.now, "next_attempt_at": clock.now + RETRY_COOLDOWN})
        self.assertEqual(len(self.stops), 1)
        stored = journal_bytes(self.owner.journal)
        # 3599 s later: no effect, no write, no slot; nor through a restart, a repeated request or a
        # teardown/close caller, all of which only reach replay_one.
        clock.advance(RETRY_COOLDOWN - 1)
        writes = dict(self.owner.journal.writes)
        self.assertEqual(self.owner.replay(), [])
        self.assertEqual(CleanupOwner(self.runtime).replay_one(key)["status"], "pending")
        self.assertEqual(self.owner.journal.request(self.task, "done", self.record.to_json()), key)
        self.assertEqual(self.owner.cleanup(self.task, self.record, "done")["retry"], first["retry"])
        self.assertEqual(self.owner.journal.request(self.task, "close"), key)
        self.assertEqual(len(self.stops), 1)
        self.assertEqual(self.owner.journal.writes, writes)
        self.assertEqual({k: v for k, v in journal_bytes(self.owner.journal).items() if k != "meta.json"},
                         {k: v for k, v in stored.items() if k != "meta.json"})
        # 3600 s: due, through a fresh owner (a restart reads the stored due time).
        clock.advance(1)
        self.assertEqual(len(CleanupOwner(self.runtime).replay()), 1)
        self.assertEqual(len(self.stops), 2)
        # A crash after the reservation keeps the reservation: the next attempt is a cooldown later.
        clock.advance(RETRY_COOLDOWN)
        with mock.patch.object(self.owner, "_stop", side_effect=KeyboardInterrupt), \
                self.assertRaises(KeyboardInterrupt):
            self.owner.replay_one(key)
        reserved = self.owner.journal.intent(key)["retry"]
        self.assertEqual(reserved["last_attempt_at"], clock.now)
        clock.advance(10)
        restarted = CleanupOwner(self.runtime)
        self.assertEqual(restarted.replay(), [])
        self.assertEqual(len(self.stops), 2)
        clock.advance(RETRY_COOLDOWN - 10)
        self.stop_failure = False
        self.assertEqual(restarted.replay()[0]["status"], "completed")
        self.assertEqual(len(self.stops), 3)
        # A completed intent is never due again.
        clock.advance(RETRY_COOLDOWN)
        self.assertEqual(restarted.replay(), [])

    def test_a_new_attempt_is_its_own_key_with_its_own_first_attempt(self):
        clock = self.manual_clock()
        self.head()
        self.stop_failure = True
        old = self.request()
        self.owner.replay_one(old)
        record = replace(self.record, attempt_id="attempt-2")
        self.owner.remember(self.task, record)
        new = self.owner.journal.request(self.task, "done", record.to_json())
        self.assertNotEqual(new, old)
        clock.advance(60)
        self.assertEqual([item["record"]["attempt_id"] for item in self.owner.replay()], ["attempt-2"])
        self.assertEqual(len(self.stops), 2)
        self.assertEqual(self.owner.journal.intent(old)["retry"]["last_attempt_at"], clock.now - 60)

    def test_concurrent_owners_reserve_one_attempt(self):
        self.manual_clock()
        self.head()
        key = self.request()
        barrier = threading.Barrier(4)
        reserved = []
        def reserve():
            journal = CleanupJournal(self.data)
            barrier.wait()
            reserved.append(journal.reserve_attempt(key, self.owner.clock.now))
        threads = [threading.Thread(target=reserve) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual(sum(item is not None for item in reserved), 1)
        # An owner racing a running attempt finds it reserved and acts on nothing.
        self.owner.clock.advance(RETRY_COOLDOWN)
        entered, release = threading.Event(), threading.Event()
        stop = self.backend.stop
        def slow(run, initiator):
            entered.set()
            self.assertTrue(release.wait(5))
            return stop(run, initiator)
        self.backend.stop = slow
        first = threading.Thread(target=self.owner.replay_one, args=(key,))
        first.start()
        self.assertTrue(entered.wait(5))
        results = []
        second = threading.Thread(target=lambda: results.append(CleanupOwner(self.runtime).replay_one(key)))
        second.start()
        release.set()
        first.join(10)
        second.join(10)
        self.assertEqual(self.stops, [("run-worker", "generation-1")])
        self.assertEqual(results[0]["status"], "completed")

    def test_hostile_or_backwards_due_times_are_attempted_once_and_reanchored(self):
        clock = self.manual_clock()
        self.head()
        self.stop_failure = True
        key = self.request()
        for hostile in ({"next_attempt_at": 1e300}, {"next_attempt_at": "soon"}, {"next_attempt_at": True},
                        {"next_attempt_at": float("nan")}, "garbage", {"last_attempt_at": clock.now}):
            with self.subTest(hostile=hostile):
                intent = self.owner.journal.intent(key)
                intent["retry"] = hostile
                self.owner.journal.commit_intent(key, intent)
                stops = len(self.stops)
                self.owner.replay(limit=1)
                self.assertEqual(len(self.stops), stops + 1)
                self.assertEqual(self.owner.journal.intent(key)["retry"],
                                 {"last_attempt_at": clock.now, "next_attempt_at": clock.now + RETRY_COOLDOWN})
                # Re-anchored: no write storm at the same clock.
                writes = dict(self.owner.journal.writes)
                self.assertEqual(self.owner.replay(), [])
                self.assertEqual(self.owner.journal.writes, writes)
                clock.advance(1)
        # The clock moves back two hours: a due time more than one cooldown ahead is distrusted.
        clock.advance(-2 * RETRY_COOLDOWN)
        stops = len(self.stops)
        self.owner.replay()
        self.assertEqual(len(self.stops), stops + 1)
        clock.advance(10)
        self.assertEqual(self.owner.replay(), [])
        self.assertEqual(len(self.stops), stops + 1)

    def test_cooling_intents_take_no_replay_slot(self):
        clock = self.manual_clock()
        keys = []
        for n in range(101):
            ref = f"legacy-{n:03d}"
            task = {**copy.deepcopy(self.task), "id": "task-" + ref, "ref": ref, "closed": True}
            self.tasks[ref] = task
            keys.append(self.owner.journal.remember(task, {}, disposition="close"))
        due = set(sorted(keys)[40:42])
        for key in keys:
            if key not in due:
                intent = self.owner.journal.intent(key)
                intent["retry"] = {"last_attempt_at": clock.now - 60, "next_attempt_at": clock.now + RETRY_COOLDOWN - 60}
                self.owner.journal.commit_intent(key, intent)
        before = self.intent_files()
        replayed = []
        replay = self.owner._replay
        def spy(value, key):
            replayed.append(key)
            return replay(value, key)
        writes = dict(self.owner.journal.writes)
        with mock.patch.object(self.owner, "_replay", side_effect=spy):
            results = self.owner.replay(limit=5)
            self.assertEqual(self.owner.replay(limit=5), [])
        self.assertEqual(set(replayed), due)
        self.assertEqual([item["status"] for item in results], ["preserved", "preserved"])
        changed = {name for name, body in self.intent_files().items() if before[name] != body}
        self.assertEqual(changed, {f"intents/{key}.json" for key in due})
        # Each due intent: its reservation, its heads-stopped checkpoint and its outcome; the cursor once.
        self.assertEqual(self.owner.journal.writes["intent"] - writes["intent"], 6)
        self.assertEqual(self.owner.journal.writes["meta"] - writes["meta"], 1)

    def ignored_caches(self, workspace=None):
        """Git-ignored Python caches in the exact workspace; returns their paths and an outside target."""
        workspace = workspace or self.workspace
        with (self.repo / ".git" / "info" / "exclude").open("a") as exclude:
            exclude.write(".pytest_cache/\n.venv/\nsrc/**/__pycache__/\n")
        outside = self.root / "outside-python"
        outside.write_bytes(b"interpreter bytes\n")
        files = [workspace / ".pytest_cache" / "v" / "cache" / "lastfailed",
                 workspace / "src" / "pkg" / "__pycache__" / "mod.cpython-312.pyc",
                 workspace / "src" / "pkg" / "deep" / "__pycache__" / "x.pyc",
                 workspace / ".venv" / "lib" / "site.py"]
        for file in files:
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(b"cache")
        (workspace / ".venv" / "bin").mkdir()
        (workspace / ".venv" / "bin" / "python").symlink_to(outside)
        return files, outside

    def test_done_workspace_with_ignored_python_caches_is_removed_without_force(self):
        self.head()
        self.task["claim"]["worker"] = self.record.worker
        key = self.request()
        files, outside = self.ignored_caches()
        status = git(self.workspace, "status", "--porcelain", "--ignored", "--untracked-files=all")
        self.assertTrue(all(line.startswith("!! ") for line in status.splitlines()), status)
        # Planning reads the same predicate and writes nothing, in the journal or the workspace.
        before = journal_bytes(self.owner.journal)
        entry = self.entry(self.owner.inventory(project="sample"), key)
        self.assertEqual(entry["outcome"], "eligible", entry["reason"])
        removal = next(effect for effect in entry["effects"] if effect["effect"] == "remove-worktree")
        self.assertEqual((removal["dirty"], removal["ignored_caches"]), ("clean", 5))
        self.assertEqual(journal_bytes(self.owner.journal), before)
        self.assertTrue(all(file.exists() for file in files))
        git_calls = []
        native = git_worktree.remove
        def remove(run, repo, path, **kwargs):
            git_calls.append(path)
            return native(run, repo, path, **kwargs)
        with mock.patch("ummanu.dispatch.cleanup.git_worktree.remove", side_effect=remove), \
                mock.patch("ummanu.dispatch.cleanup.shutil.rmtree") as rmtree:
            result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "completed", result["reason"])
        rmtree.assert_not_called()
        self.assertEqual(git_calls, [self.workspace])
        self.assertFalse(self.workspace.exists())
        self.assertNotIn(str(self.workspace), git(self.repo, "worktree", "list", "--porcelain"))
        self.assertEqual(result["commit_proof"]["publication"], "remote-tracking")
        self.assertEqual(outside.read_bytes(), b"interpreter bytes\n")

    def test_only_done_ignored_caches_are_disposable(self):
        self.tolerant_stop()
        self.head()
        self.task["claim"]["worker"] = self.record.worker
        key = self.request()
        files, outside = self.ignored_caches()
        foreign = self.root / "foreign"
        (foreign / "__pycache__").mkdir(parents=True)
        (foreign / "__pycache__" / "x.pyc").write_bytes(b"foreign")
        (foreign / "data").write_bytes(b"foreign data")
        tracked = self.workspace / "src" / "tracked" / "__pycache__" / "kept.pyc"

        def tracked_cache():
            tracked.parent.mkdir(parents=True)
            tracked.write_bytes(b"tracked")
            git(self.workspace, "add", "-f", str(tracked))
            git(self.workspace, "commit", "--quiet", "-m", "tracked cache")
            git(self.repo, "update-ref", "refs/remotes/origin/main", git(self.workspace, "rev-parse", "HEAD"))
            tip = git(self.workspace, "rev-parse", "HEAD")
            intent = self.owner.journal.intent(key)
            intent["identity"]["tip"] = tip
            self.owner.journal.save({"intents": {key: intent}})
            tracked.write_bytes(b"changed tracked cache")
            return lambda: None

        def unignored_cache():
            (self.workspace / "other" / "__pycache__").mkdir(parents=True)
            (self.workspace / "other" / "__pycache__" / "x.pyc").write_bytes(b"x")
            return lambda: shutil.rmtree(self.workspace / "other")

        def arbitrary_ignored():
            (self.workspace / "ignored").write_text("secret\n")
            return (self.workspace / "ignored").unlink

        def author_work():
            (self.workspace / "notes.txt").write_text("author\n")
            return (self.workspace / "notes.txt").unlink

        def symlinked_cache():
            shutil.rmtree(self.workspace / ".pytest_cache")
            (self.workspace / ".pytest_cache").symlink_to(foreign, target_is_directory=True)
            def restore():
                (self.workspace / ".pytest_cache").unlink()
                files[0].parent.mkdir(parents=True)
                files[0].write_bytes(b"cache")
            return restore

        def symlinked_parent():
            (self.workspace / "src" / "link").symlink_to(foreign, target_is_directory=True)
            return (self.workspace / "src" / "link").unlink

        def closed_not_done():
            self.tasks["sample-1"].update(state="blocked", closed=True)
            return lambda: self.tasks["sample-1"].update(state="done", closed=False)

        cases = (("unignored cache name", unignored_cache, "?? other/__pycache__/x.pyc"),
                 ("arbitrary ignored file", arbitrary_ignored, "!! ignored"),
                 ("author work", author_work, "?? notes.txt"),
                 ("symlinked cache", symlinked_cache, ".pytest_cache"),
                 ("symlinked parent", symlinked_parent, "src/link"),
                 ("closed but not Done", closed_not_done, "!! .venv/bin/python"),
                 ("tracked cache name", tracked_cache, "src/tracked/__pycache__/kept.pyc"))
        for named, introduce, row in cases:
            with self.subTest(named=named):
                restore = introduce()
                try:
                    result = self.owner.replay_one(key)
                finally:
                    restore()
                self.assertEqual(result["status"], "preserved", result["reason"])
                self.assertIn("dirty tracked, untracked or ignored work", result["reason"])
                self.assertIn(row, result["reason"])
                self.assertTrue(all(file.exists() for file in files), "no cache entry is deleted while dirty")
                self.assertEqual((foreign / "__pycache__" / "x.pyc").read_bytes(), b"foreign")
                self.assertEqual((foreign / "data").read_bytes(), b"foreign data")
                self.assertEqual(outside.read_bytes(), b"interpreter bytes\n")
        # An active card is refused before any proof: nothing at all is read as disposable.
        self.tasks["sample-1"]["state"] = "in_progress"
        result = self.owner.replay_one(key)
        self.assertEqual(result["status"], "pending", result["reason"])
        self.assertTrue(all(file.exists() for file in files))

    def test_observer_ignored_caches_remain_work(self):
        repo, path, observer = self.observer()
        with (repo / ".git" / "info" / "exclude").open("a") as exclude:
            exclude.write("__pycache__/\n")
        (path / "__pycache__").mkdir()
        (path / "__pycache__" / "x.pyc").write_bytes(b"x")
        result = self.owner.cleanup_observer(observer)
        self.assertEqual(result["status"], "preserved", result["reason"])
        self.assertIn("!! __pycache__/x.pyc", result["reason"])
        self.assertTrue((path / "__pycache__" / "x.pyc").exists())

    def test_live_shaped_backlog_settles_dead_ends_and_retries_only_real_refusals(self):
        """72 disappeared, 12 unregistered, 7 missing-proof and 8 retryable obligations (fixture only)."""
        clock = self.manual_clock()
        gone = self.root / "gone-repo"
        git(self.root, "clone", "--quiet", str(self.repo), str(gone))
        git(gone, "update-ref", "refs/remotes/origin/main", self.base)
        self.catalog.bindings["gone"] = {"repo": str(gone), "default_branch": "main"}
        live = set()
        stop = self.backend.stop
        def backend_stop(run, initiator):
            self.stops.append((run.run_id, run.scope_generation))
            if run.run_id in live:
                return SimpleNamespace(ok=False, reason="head still running", run=run)
            return SimpleNamespace(ok=True, reason="", run=run.finishing(initiator).exited())
        self.backend.stop = backend_stop
        self.addCleanup(setattr, self.backend, "stop", stop)
        kinds = {}
        for n in range(72):
            key, workspace, _ = self.card(f"gone-{n:02d}")
            git(self.repo, "worktree", "remove", str(workspace))
            kinds[key] = "workspace-disappeared"
        for n in range(12):
            key, _, _ = self.card(f"unreg-{n:02d}", project="gone", repo=gone)
            kinds[key] = "project-unregistered"
        for n in range(7):
            key, workspace, _ = self.card(f"missing-{n:02d}")
            shutil.rmtree(workspace)
            kinds[key] = "registration-without-directory"
        for n in range(8):
            key, _, record = self.card(f"live-{n:02d}")
            live.add(HeadRun.from_json(record.worker_head_run).run_id)
            kinds[key] = ""
        del self.catalog.bindings["gone"]
        writes = dict(self.owner.journal.writes)
        ticks = 0
        while True:
            clock.advance(15)
            if not self.owner.replay(limit=5):
                break
            ticks += 1
        first_pass = {name: self.owner.journal.writes[name] - writes[name] for name in writes}
        summary = {row["id"]: row for row in self.owner.journal.summary()}
        self.assertEqual({key: summary[key]["terminal"] for key in kinds}, kinds)
        statuses = {}
        for row in summary.values():
            statuses[(row["status"], row["terminal"])] = statuses.get((row["status"], row["terminal"]), 0) + 1
        self.assertEqual(statuses, {("preserved", "workspace-disappeared"): 72,
                                    ("preserved", "project-unregistered"): 12,
                                    ("preserved", "registration-without-directory"): 7,
                                    ("pending", ""): 8})
        self.assertLessEqual(sum(row["status"] == "pending" for row in summary.values()), 10)
        self.assertEqual(ticks, 20)
        self.assertEqual(len(self.stops), 99)
        # A dead end: reservation, stop receipt, heads-stopped checkpoint, outcome (91 x 4); a refused
        # stop: reservation and outcome (8 x 2). The cursor once per tick, the generated map never.
        self.assertEqual((first_pass["intent"], first_pass["meta"], first_pass["generated"]),
                         (91 * 4 + 8 * 2, ticks, 0))
        # Later ticks within the hour: no effect, no Git read, no write.
        writes = dict(self.owner.journal.writes)
        calls, _refs, reads = self.recorded_reads()
        with reads:
            for _ in range(20):
                clock.advance(15)
                self.assertEqual(self.owner.replay(limit=5), [])
        self.assertEqual(calls, [])
        self.assertEqual(self.owner.journal.writes, writes)
        self.assertEqual(len(self.stops), 99)
        # An hour on, only the real refusals are attempted again.
        clock.advance(RETRY_COOLDOWN)
        retried = [item["task"]["ref"] for _ in range(3) for item in self.owner.replay(limit=5)]
        self.assertEqual(sorted(retried), sorted(f"live-{n:02d}" for n in range(8)))
        self.assertEqual(len(self.stops), 107)
        for path in self.owner.journal.path.rglob("*.json"):
            self.assertLessEqual(path.stat().st_size, 1_000_000)
        print(f"backlog fixture: {statuses}; first pass {ticks} ticks, writes {first_pass}")


class SettledHeadStopTests(unittest.TestCase):
    """secretary-1918: stopping an already-settled head keeps its receipt and mints nothing."""

    def setUp(self):
        from tests.dispatcher_fixtures import RecordingReviewHost
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        self.host = RecordingReviewHost(self.root)
        self.record = DispatcherRecord(worker="sample-1-worker", workspace=str(self.workspace), handle="",
                                       head="codex", review_head="codex-reviewer", attempt_id="attempt-1",
                                       comment_baseline=0, review_baseline=0, state="reviewing", claimed_at=0.0)
        self.committed = []
        self.host.commit_state = lambda: self.committed.extend(
            run for run in (self.record.worker_head_run, self.record.review_head_run) if run)

    def stored_run(self, run_id, role, profile):
        from tests.dispatcher_fixtures import supervised_run
        return supervised_run(run_id, role=role, profile=profile, workspace=str(self.workspace),
                              task_ref=TaskRef.card("sample-1"), handle="run:" + run_id)

    def assert_only_card_runs_committed(self):
        self.assertTrue(self.committed)
        self.assertEqual({run["task_ref"]["ref"] for run in self.committed}, {"sample-1"})

    def test_review_round_settled_then_stopped_again_then_round_two_stopped(self):
        self.record.review_handle = "run:review-1"
        self.record.review_head_run = self.stored_run("review-1", "reviewer", "codex-reviewer")
        self.host.stop_review(self.record, "review-verdict")
        settled = copy.deepcopy(self.record.review_head_run)
        self.assertEqual(settled["lifecycle"], "exited")

        self.host.stop_head(self.record, "review", "done")
        self.assertEqual(self.record.review_head_run, settled)
        self.assertEqual(self.host.backend.stops, [("review-1", "review-verdict")])

        self.record.review_handle = "run:review-2"
        self.record.review_head_run = self.stored_run("review-2", "reviewer", "codex-reviewer")
        self.host.stop_review(self.record, "review-verdict")
        self.assertEqual(self.host.backend.stops, [("review-1", "review-verdict"), ("review-2", "review-verdict")])
        self.assertEqual((self.record.review_head_run["run_id"], self.record.review_head_run["lifecycle"]),
                         ("review-2", "exited"))
        self.assert_only_card_runs_committed()

    def test_worker_settled_then_stopped_again_then_next_run_stopped(self):
        self.record.handle = "run:worker-1"
        self.record.worker_head_run = self.stored_run("worker-1", "worker", "codex")
        self.host.stop_head(self.record, "worker", "review-freeze")
        settled = copy.deepcopy(self.record.worker_head_run)
        self.assertEqual(settled["lifecycle"], "exited")

        self.host.stop_head(self.record, "worker", "done")
        self.assertEqual(self.record.worker_head_run, settled)
        self.assertEqual(self.host.backend.stops, [("worker-1", "review-freeze")])

        self.record.handle = "run:worker-2"
        self.record.worker_head_run = self.stored_run("worker-2", "worker", "codex")
        self.host.stop_head(self.record, "worker", "done")
        self.assertEqual(self.host.backend.stops, [("worker-1", "review-freeze"), ("worker-2", "done")])
        self.assertEqual((self.record.worker_head_run["run_id"], self.record.worker_head_run["lifecycle"]),
                         ("worker-2", "exited"))
        self.assert_only_card_runs_committed()

    def test_settled_reviewer_restop_still_fences_a_live_foreign_pid_file(self):
        from ummanu.runtime.head.identity import publish_heartbeat
        self.record.review_handle = "run:review-1"
        self.record.review_pid_file = str(self.root / "review.pid")
        self.record.review_head_run = self.stored_run("review-1", "reviewer", "codex-reviewer")
        self.host.stop_review(self.record, "review-verdict")
        settled = copy.deepcopy(self.record.review_head_run)
        publish_heartbeat(self.record.review_pid_file,
                          {"run_id": "someone-else", "role": "reviewer", "task": "card:sample-1"})
        with self.assertRaises(HostError):
            self.host.stop_head(self.record, "review", "done")
        self.assertEqual(self.record.review_head_run, settled)
        self.assertEqual(self.host.backend.stops, [("review-1", "review-verdict")])

if __name__ == "__main__":
    unittest.main()
