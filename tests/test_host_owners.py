"""Dispatcher host owner, patch, refusal and complete packet contracts."""

from __future__ import annotations

import ast
import inspect
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.support import host_owners
from ummanu.dispatch import host
from ummanu.dispatch.host_runtime import catalog, contracts, lifecycle, observer_io, packets, workspace
from ummanu.dispatch.types import DispatcherError, HostError
from ummanu.runtime.head import HeadRun, HeadSpec, TaskRef
from ummanu.runtime.prompt_document import PromptDocumentError

ROOT = Path(__file__).resolve().parents[1]


class HostOwnershipTests(unittest.TestCase):
    def test_file_limits_unique_owners_and_a_plain_package(self):
        sources = host_owners.sources(ROOT)
        self.assertEqual(host_owners.file_limits(sources), [])
        self.assertEqual(host_owners.file_limits({"boundary": "\n" * 3000}), [])
        self.assertEqual(host_owners.file_limits({"baseline": "\n" * 5142}), ["baseline: 5142 > 3000"])
        definitions = host_owners.inventory(sources)
        for name, (path, node) in definitions.items():
            if name.startswith("CommandHostRuntime."):
                method = name.split(".", 1)[1]
                self.assertTrue(callable(getattr(host.CommandHostRuntime, method)) or
                                isinstance(inspect.getattr_static(host.CommandHostRuntime, method), property))
            if path != "src/ummanu/dispatch/host.py":
                self.assertEqual(host_owners.reverse_imports(path, sources[path]), set(), path)
        tree = ast.parse(sources["src/ummanu/dispatch/host_runtime/__init__.py"])
        self.assertEqual(len(tree.body), 1)
        self.assertIsInstance(tree.body[0], ast.Expr)

    def test_duplicate_and_reverse_import_guards_detect_planted_cases(self):
        for sources in (
            {"core": "class CommandHostRuntime:\n def stop(self): pass\n",
             "owner": "class HeadLifecycleMixin:\n def stop(self): pass\n"},
            {"owner": "class ObserverHostMixin:\n def observer_status(self): pass\n def observer_status(self): pass\n"},
        ):
            with self.assertRaisesRegex(AssertionError, "duplicate host implementation"):
                host_owners.inventory(sources)
        for source in (
            "def f():\n from ..host import CommandHostRuntime\n",
            "from .. import host as facade\nx = facade.CommandHostRuntime\n",
            "import ummanu.dispatch.host as facade\nx = facade.CommandHostRuntime\n",
            "from importlib import import_module as load\nx = load('ummanu.dispatch.host')\n",
            "PATCH = 'ummanu.dispatch.host._launch_command'\n",
        ):
            with self.subTest(source=source):
                self.assertTrue(host_owners.reverse_imports("src/ummanu/dispatch/host_runtime/packets.py", source))

    def test_public_identities_descriptors_mro_and_instance_lookup(self):
        self.assertIs(host.InstanceCatalog, catalog.InstanceCatalog)
        self.assertIs(host.HostError, catalog.HostError)
        self.assertIs(host._durable_head_run, contracts._durable_head_run)
        self.assertIs(host._body_file_path, packets._body_file_path)
        declared = {}
        for cls in host.CommandHostRuntime.__mro__[:-1]:
            for name, member in vars(cls).items():
                if callable(member) or isinstance(member, (property, staticmethod, classmethod)):
                    self.assertNotIn(name, declared, (name, cls, declared.get(name)))
                    declared[name] = cls
        for name, owner, kind in (
            ("_git_observer_root", observer_io.ObserverHostMixin, property),
            ("_workspace_environment", workspace.WorkspaceEnvironmentMixin, staticmethod),
            ("_workspace_environment_owner", workspace.WorkspaceEnvironmentMixin, classmethod),
            ("_head_status", lifecycle.HeadLifecycleMixin, staticmethod),
        ):
            member = inspect.getattr_static(host.CommandHostRuntime, name)
            self.assertIs(member, inspect.getattr_static(owner, name))
            self.assertIsInstance(member, kind)
        self.assertTrue(hasattr(host.CommandHostRuntime.prepare_observer, "__wrapped__"))
        instance = host.CommandHostRuntime(SimpleNamespace(), ROOT, mode="noop")
        with mock.patch.object(instance, "_local_run_policy", return_value=(None, True)) as read:
            self.assertEqual(instance.local_run_snapshot_for_round(
                {"ref": "p-1", "project": "p"}, "worker", 1, {})["unavailable"], True)
        read.assert_called_once()


class HostOwnerBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.host = host.CommandHostRuntime(SimpleNamespace(), self.root, mode="real")

    def test_catalog_validation_and_repository_comparison_use_the_owning_globals(self):
        with (mock.patch.object(catalog, "validate_instance", return_value=SimpleNamespace(ok=False)) as validate,
              self.assertRaises(DispatcherError) as raised):
            host.InstanceCatalog(self.root / "instance.yaml")
        self.assertEqual(raised.exception.code, "invalid_instance")
        validate.assert_called_once_with(self.root / "instance.yaml")
        config = SimpleNamespace(instance_dir=self.root, binding=lambda project: {"repo": str(self.root / "other")})
        with mock.patch.object(catalog, "_same_repo", return_value=True) as same:
            self.assertIn("live root", catalog.live_root_project_refusal(config, "p"))
        same.assert_called_once_with(self.root / "other", self.root)

    def test_workspace_owner_write_fault_keeps_error_classification_and_self_lookup(self):
        with (mock.patch.object(self.host, "_decide_workspace_environment_ownership", return_value="absent") as decide,
              mock.patch.object(self.host, "_exclude_workspace_environment") as exclude,
              mock.patch.object(workspace, "write_text_atomic", side_effect=RuntimeError("owner sentinel")) as write,
              self.assertRaisesRegex(HostError, "owner sentinel")):
            self.host._claim_workspace_environment(str(self.root))
        decide.assert_called_once_with(str(self.root))
        exclude.assert_called_once_with(self.root)
        self.assertEqual(write.call_count, 1)
        self.assertFalse(self.host._workspace_environment_owner(self.root).exists())

    def test_lifecycle_status_dispatches_to_each_actual_owner_with_distinct_sentinels(self):
        with (mock.patch.object(lifecycle, "_head_run_process_status", return_value={"sentinel": "run"}) as exact,
              mock.patch.object(lifecycle, "_head_process_status", return_value={"sentinel": "legacy"}) as legacy):
            self.assertEqual(self.host._head_status("pid", run={"fixture": True}, role="worker"), {"sentinel": "run"})
            self.assertEqual(self.host._head_status("pid", expected={"run_id": "fixed"}), {"sentinel": "legacy"})
        exact.assert_called_once_with("pid", run={"fixture": True}, role="worker", task="", leaf="")
        legacy.assert_called_once_with("pid", expected={"run_id": "fixed"})

    def test_prompt_write_refusal_precedes_launch_and_clear_order(self):
        fixture_host, task, record = host_owners.packet_fixture(self.root)
        fixture_host.mode = "real"
        Path(record.workspace).mkdir()
        calls = []
        with (mock.patch.object(fixture_host, "_require_project_available"),
              mock.patch.object(fixture_host, "_refuse_legacy_record"),
              mock.patch.object(fixture_host, "_require_worker_admission", side_effect=lambda *a: calls.append("admission")),
              mock.patch.object(fixture_host, "_prepare_workspace_environment", side_effect=lambda *a, **k: calls.append("prepare")),
              mock.patch.object(fixture_host, "_require_workspace_environment", side_effect=lambda *a: calls.append("environment")),
              mock.patch.object(fixture_host, "_review_prompt", return_value="complete review"),
              mock.patch.object(fixture_host, "_clear_body_file", side_effect=lambda *a: calls.append("clear")),
              mock.patch.object(fixture_host, "_launch") as launch,
              mock.patch.object(packets, "_write_prompt_document", side_effect=PromptDocumentError("write sentinel")) as write,
              self.assertRaisesRegex(HostError, "write sentinel")):
            fixture_host.start_review(task, record)
        self.assertEqual(calls, ["admission", "prepare", "environment", "clear"])
        write.assert_called_once()
        self.assertEqual(write.call_args.args[1], "complete review")
        launch.assert_not_called()

    def test_observer_worktree_refusal_reads_the_owning_git_global_and_preserves_placement(self):
        placement = self.root / "observer"
        with (mock.patch.object(self.host, "_git_observer_worktree_listed", return_value=False) as listed,
              mock.patch.object(self.host, "_observer_repo", return_value=self.root),
              mock.patch.object(observer_io.git_worktree, "add", return_value=SimpleNamespace(
                  returncode=42, stderr="observer sentinel", stdout="")) as add,
              mock.patch.object(self.host, "teardown") as teardown,
              self.assertRaisesRegex(HostError, "observer sentinel; residue preserved")):
            self.host._create_git_observer_workspace(placement)
        listed.assert_called_once_with(str(placement))
        add.assert_called_once_with(self.host._observer_git, self.root, placement, observer_io.OBSERVER_REPO_BRANCH)
        teardown.assert_not_called()

    def test_observer_failure_refuses_foreign_run_before_reading_the_owner_global(self):
        run = HeadRun(run_id="observer-run", spec=HeadSpec(profile_id="observer", adapter="claude", runtime="local-pty"),
                      workspace=str(self.root), role="observer", task_ref=TaskRef.sprint("sprint:1490"))
        record = SimpleNamespace(head_run=run.to_json(), workspace=str(self.root), sprint="sprint:1490")
        with (mock.patch.object(observer_io, "_provider_failure_for_persisted_run", return_value={"sentinel": "observer"}) as read,
              mock.patch.object(self.host, "_local_pty_root", return_value="fixture-root")):
            self.assertEqual(self.host.observer_provider_failure(record), {"sentinel": "observer"})
            read.assert_called_once_with(record.head_run, local_pty_root="fixture-root")
            read.reset_mock()
            record.sprint = "sprint:other"
            self.assertEqual(self.host.observer_provider_failure(record)["state"], "identity_mismatch")
            read.assert_not_called()

    def test_shared_launch_wrapper_keeps_duck_type_writer_and_owning_global_lookup(self):
        with mock.patch.object(contracts, "_with_pid_heartbeat", return_value="wrapper sentinel") as wrap:
            self.assertEqual(contracts._launch_command(SimpleNamespace(), "command", "pid", {"run_id": "r"}),
                             "wrapper sentinel")
            wrap.assert_called_once_with("command", "pid", identity={"run_id": "r"})
            wrap.reset_mock()
            self.assertEqual(host._launch_command(SimpleNamespace(writes_launch_identity=True),
                                                  "command", "pid", {"run_id": "r"}), "command")
            wrap.assert_not_called()

    def test_complete_worker_and_reviewer_documents_match_captured_bytes(self):
        rendered = host_owners.render_packets(self.root)
        artifact = ROOT / "tests/fixtures/host-packets.json"
        if os.environ.get("UMMANU_REGENERATE_HOST_GOLDENS") == "1":
            artifact.write_text(json.dumps(rendered, indent=2, ensure_ascii=False) + "\n")
        self.assertEqual(rendered, json.loads(artifact.read_text()))
