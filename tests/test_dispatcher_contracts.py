"""Contract tests binding the dispatcher test doubles to the real runtime classes.

The dispatcher suite drives `DispatcherRuntime` against `FakeHost`/`FakeCatalog` and a real card
store (tests/test_dispatcher.py). Those doubles stand in for `CommandHostRuntime` and
`InstanceCatalog`. When a real class grew a method (`teardown`, `gate_check`) or changed a return
shape, the double had to be patched by hand, and every time it lagged the suite stayed green while
production broke. These tests make that drift a build failure.

Two layers:

surface  — the set of host/catalog attributes `DispatcherRuntime` and its helpers actually touch is
           discovered from the source (AST, not a hand-kept list), then required to exist on both
           the real class and the double with a call-compatible signature.
behaviour — the real host is run in `mode="noop"`, which needs no orca/git/gh, and its results are
           compared to the double's for the same call. That pins return shapes (the keys of
           `prepare_worker`, the `GateResult` of `gate_check`) rather than just method names.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import os
import pwd
import re
import subprocess
import sys
import tempfile
import textwrap
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest import mock

from tests.dispatcher_fixtures import card_audit
from tests.fakes.dispatcher import FakeCatalog, FakeHost
from tests.fanout_fixtures import accepted_transport_run
from tests.production_runtime_fixtures import registered_production_runtime
from tests.retired_board import legacy_runtime_lines
from tests.support.managed_venv import guarded_product_env, managed_product_root
from ummanu import upgrade
from ummanu.automations.runtime import dispatch
from ummanu.dispatch import (
    attempt_accounting as dispatcher_attempt_accounting,
    claim as dispatcher_claim,
    gate_lifecycle as dispatcher_gate_lifecycle,
    host as dispatcher_host_module,
    launcher as dispatcher_launcher,
    observer as dispatcher_observer,
    production as dispatcher_production,
    release_lifecycle as dispatcher_release_lifecycle,
    review as dispatcher_review,
    runtime as dispatcher_module,
    wait_vitality as dispatcher_wait_vitality,
    worker_continuation as dispatcher_worker_continuation,
    worker_launch as dispatcher_worker_launch,
    worker_report as dispatcher_worker_report,
)
from ummanu.dispatch.gate import GateResult
from ummanu.dispatch.git_workspace import GitWorkspaceManager
from ummanu.dispatch.host import CommandHostRuntime, InstanceCatalog
from ummanu.dispatch.runtime import DispatcherRuntime
from ummanu.dispatch.state import DispatcherRecord
from ummanu.dispatch.types import DispatcherError, HostError, LegacyDispatcherRecord
from ummanu.head_registry import (
    canonical_heads,
    generated_pair,
    installed_heads,
    materialize_snapshot,
    record_source,
    snapshot_header,
)
from ummanu.host import SHIPPED_PACKAGING_ROOT, SystemdLayout, render_systemd_unit
from ummanu.host_apply import resolve_packaged
from ummanu.runtime import heads, role_env
from ummanu.runtime.head import (
    HEAD_ALIVE,
    HEAD_OK,
    STANDING_BINDING,
    HeadCommandError,
    HeadRun,
    HeadSpec,
    HeadSpecError,
    StopReceipt,
    TaskRef,
    render_head_command,
    wrap_role_command,
)
from ummanu.runtime.head_runtimes import (
    DEFAULT_HEAD_RUNTIME,
    HEAD_RUNTIMES,
    LOCAL_PTY_RUNTIME,
    ORCA_LEGACY_RUNTIME,
    RECORD_RUNTIME_WHEN_ABSENT,
)
from ummanu.runtime.local_pty_head import LocalPtyHeadRuntime
from ummanu.runtime.role_env import observer_binding

# Modules that reach through a runtime into the host/catalog collaborators.
_RUNTIME_MODULES = (
    dispatcher_module,
    dispatcher_attempt_accounting,
    dispatcher_claim,
    dispatcher_gate_lifecycle,
    dispatcher_production,
    dispatcher_release_lifecycle,
    dispatcher_review,
    dispatcher_observer,
    dispatcher_worker_launch,
    dispatcher_worker_continuation,
    dispatcher_worker_report,
    dispatcher_wait_vitality,
)

# Attribute owners as they are spelled at the call sites: `self.host` inside DispatcherRuntime,
# `runtime.host` / `host.` inside the extracted helper modules.
_RUNTIME_HOLDERS = ("self", "runtime")


def _runtime_trees() -> list[ast.AST]:
    """Sources that drive a runtime's collaborators. dispatch/runtime.py is narrowed to the
    `DispatcherRuntime` class: `CommandHostRuntime` also holds a `self.catalog`, but that is the
    host's own use of the real catalog, not the surface the doubles have to cover."""
    trees: list[ast.AST] = []
    for module in _RUNTIME_MODULES:
        tree = ast.parse(Path(inspect.getsourcefile(module)).read_text(encoding="utf-8"))
        if module is dispatcher_module:
            trees.extend(
                node
                for node in tree.body
                if isinstance(node, ast.ClassDef) and node.name == "DispatcherRuntime"
            )
            continue
        trees.append(tree)
    return trees


def _used_attributes(collaborator: str) -> set[str]:
    """Every attribute the dispatcher sources read off `<runtime>.<collaborator>`."""
    used: set[str] = set()
    for tree in _runtime_trees():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Attribute):
                continue
            inner = node.value
            if inner.attr != collaborator:
                continue
            if isinstance(inner.value, ast.Name) and inner.value.id in _RUNTIME_HOLDERS:
                used.add(node.attr)
    return used


def _declares(cls, name: str) -> bool:
    """Plain data attributes (`catalog.instance_dir`) are part of the surface too.

    They are assigned in `__init__` rather than declared on the class, so `hasattr`
    alone would read them as missing on the real class and as a drift on the double.
    """
    if hasattr(cls, name):
        return True
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(cls)))
    except (OSError, TypeError, SyntaxError):
        return False
    return any(
        isinstance(node, ast.Attribute)
        and node.attr == name
        and isinstance(node.ctx, ast.Store)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
        for node in ast.walk(tree)
    )


def _signature(func) -> list[tuple[str, str, bool]]:
    """(name, kind, has-default) per parameter, ignoring `self` and annotations."""
    parameters = list(inspect.signature(func).parameters.values())
    if parameters and parameters[0].name == "self":
        parameters = parameters[1:]
    return [(p.name, str(p.kind), p.default is not inspect.Parameter.empty) for p in parameters]


class HostSurfaceContractTests(unittest.TestCase):
    """`FakeHost` must accept every call the runtime makes on the real host."""

    def test_runtime_host_calls_are_discovered(self) -> None:
        # Guard the AST scan itself: a rename of the holder attribute would otherwise silently
        # reduce the contract to the empty set and pass vacuously.
        used = _used_attributes("host")
        self.assertIn("gate_check", used)
        self.assertIn("teardown", used)
        self.assertIn("prepare_worker", used)
        self.assertIn("mode", used)

    def test_fake_host_covers_the_real_host_surface(self) -> None:
        for name in sorted(_used_attributes("host")):
            with self.subTest(method=name):
                self.assertTrue(
                    _declares(CommandHostRuntime, name),
                    f"DispatcherRuntime calls host.{name}, missing on CommandHostRuntime",
                )
                self.assertTrue(
                    _declares(FakeHost, name),
                    f"DispatcherRuntime calls host.{name}, missing on FakeHost",
                )
                real = getattr(CommandHostRuntime, name, None)
                fake = getattr(FakeHost, name, None)
                self.assertEqual(callable(real), callable(fake), f"FakeHost.{name} kind drifted")
                if callable(real):
                    self.assertEqual(
                        _signature(real),
                        _signature(fake),
                        f"FakeHost.{name} signature drifted from CommandHostRuntime.{name}",
                    )

    def test_fake_catalog_covers_the_real_catalog_surface(self) -> None:
        # The gate reads the catalog through the host, so its calls count as runtime usage too.
        used = _used_attributes("catalog") | {
            "adapter",
            "binding",
            "integration_base",
            "project_default_branch",
            "workspace_seed",
        }
        for name in sorted(used):
            with self.subTest(method=name):
                self.assertTrue(
                    _declares(InstanceCatalog, name),
                    f"the dispatcher calls catalog.{name}, missing on InstanceCatalog",
                )
                self.assertTrue(
                    _declares(FakeCatalog, name),
                    f"the dispatcher calls catalog.{name}, missing on FakeCatalog",
                )
                if not callable(getattr(InstanceCatalog, name, None)):
                    continue
                self.assertEqual(
                    _signature(getattr(InstanceCatalog, name)),
                    _signature(getattr(FakeCatalog, name)),
                    f"FakeCatalog.{name} signature drifted from InstanceCatalog.{name}",
                )

    def test_host_internal_catalog_calls_exist_on_the_real_catalog(self) -> None:
        """`CommandHostRuntime` drives the catalog itself when launching a head. No double covers
        that path (the launcher tests use the real classes), but a rename must still fail here."""
        tree = ast.parse(Path(inspect.getsourcefile(dispatcher_host_module)).read_text(encoding="utf-8"))
        host_class = next(
            node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "CommandHostRuntime"
        )
        used = {
            node.attr
            for node in ast.walk(host_class)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "catalog"
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "self"
        }
        self.assertIn("head_launch", used)
        for name in sorted(used):
            with self.subTest(method=name):
                self.assertTrue(hasattr(InstanceCatalog, name), f"catalog.{name} is gone")

    def test_runtime_collaborators_are_named_host_and_catalog(self) -> None:
        # The scan keys off those attribute names; a rename must break here, loudly.
        parameters = inspect.signature(DispatcherRuntime.__init__).parameters
        self.assertIn("host", parameters)
        self.assertIn("catalog", parameters)


class HostBehaviourContractTests(unittest.TestCase):
    """Same call, real host in noop mode vs FakeHost: the results must have the same shape."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.shared_body_dir = Path(os.environ.get("UMMANU_DISPATCHER_BODY_DIR", "/tmp"))
        self.shared_body_files = self._body_files(self.shared_body_dir)
        env = mock.patch.dict(
            os.environ,
            {"UMMANU_DISPATCHER_BODY_DIR": str(self.root / "bodies")},
        )
        env.start()
        self.addCleanup(env.stop)
        self.real = CommandHostRuntime(FakeCatalog(), self.root / "data", mode="noop", audit=card_audit(self))  # type: ignore[arg-type]
        # These calls assert host return-shape parity after the pre-pane boundary.  The explicit
        # accepted run keeps the test from claiming a missing provider schema is launchable.
        self.real.preflight_codex_run = accepted_transport_run  # type: ignore[method-assign]
        self.fake = FakeHost(self.root / "fake")
        self.fake.audit = self.real.audit
        (self.root / "fake").mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.assertEqual(
            self._body_files(self.shared_body_dir),
            self.shared_body_files,
            "the noop host contract test changed the shared dispatcher body directory",
        )

    @staticmethod
    def _body_files(root: Path) -> dict[str, bytes]:
        """Snapshot only dispatcher-managed files, including ones that existed before this test."""
        return {
            path.name: path.read_bytes()
            for pattern in ("ummanu-*.md", "ummanu-*-pid-*.pid")
            for path in root.glob(pattern)
            if path.is_file()
        }

    def _task(self) -> dict:
        return {
            "ref": "ummanu-635",
            "project": "ummanu",
            "description": "contract",
            "workspace": {"base_branch": "main"},
            "routing": {},
        }

    def _record(self, workspace: str) -> DispatcherRecord:
        return DispatcherRecord(
            worker="ummanu-635-worker",
            workspace=workspace,
            handle="term:1",
            head="codex",
            review_head="codex-reviewer",
            attempt_id="attempt-1",
            comment_baseline=0,
            review_baseline=0,
            state="claimed",
            claimed_at=0.0,
        )

    def test_prepare_worker_returns_the_same_keys(self) -> None:
        task = self._task()
        real = self.real.prepare_worker(task, "w1", "codex", attempt_id="attempt-1")
        fake = self.fake.prepare_worker(task, "w1", "codex", attempt_id="attempt-1")
        self.assertEqual(sorted(real), sorted(fake))
        for key in real:
            self.assertIsInstance(fake[key], type(real[key]), f"prepare_worker[{key!r}] type drift")

    def test_prepare_observer_returns_the_same_keys(self) -> None:
        sprint = {"ref": "sprint:1", "goal": "g", "definition_of_done": "d", "repositories": []}
        real = self.real.prepare_observer(sprint, "codex-observer", prompt="# Sprint sprint:1\n")
        fake = self.fake.prepare_observer(sprint, "codex-observer", prompt="# Sprint sprint:1\n")
        self.assertEqual(sorted(real), sorted(fake))
        for key in real:
            self.assertIsInstance(fake[key], type(real[key]), f"prepare_observer[{key!r}] type drift")
        # Both write the sprint document the head opens at the workspace root.
        for result in (real, fake):
            self.assertTrue((Path(result["workspace"]) / "SPRINT.md").is_file())

    def test_gate_check_returns_a_gate_result(self) -> None:
        task = self._task()
        record = self._record(str(self.root / "fake" / "w1"))
        real = self.real.gate_check(task, record)
        fake = self.fake.gate_check(task, record)
        self.assertIsInstance(real, GateResult)
        self.assertIsInstance(fake, GateResult)
        self.assertEqual(real.status, "green")
        self.assertEqual(fake.status, "green")

    def test_restore_workspace_returns_a_path_string(self) -> None:
        task = self._task()
        self.assertIsInstance(self.real.restore_workspace(task, "w1"), str)
        self.assertIsInstance(self.fake.restore_workspace(task, "w1"), str)

    def test_void_methods_stay_void(self) -> None:
        task = self._task()
        record = self._record(str(self.root / "fake" / "w1"))
        self.assertIsNone(self.real.verify_worker_result(task, record))
        self.assertIsNone(self.fake.verify_worker_result(task, record))
        self.assertIsNone(self.real.complete_green(task, record))
        self.assertIsNone(self.fake.complete_green(task, record))
        self.assertIsNone(self.real.stop(record))
        self.assertIsNone(self.fake.stop(record))
        self.assertIsNone(self.real.teardown(record))
        self.assertIsNone(self.fake.teardown(record))

    def _supervised_record(self) -> DispatcherRecord:
        """A card in its git workspace whose worker is a recorded `local-pty` head."""
        workspace = self.root / "data" / "workspaces" / "ummanu" / "ummanu-635-worker"
        record = self._record(str(workspace))
        record.worker_head_run = HeadRun(
            run_id="run-w",
            spec=HeadSpec(profile_id="codex", adapter="codex", runtime=LOCAL_PTY_RUNTIME),
            workspace=str(workspace),
            task_ref=TaskRef.card("ummanu-635"),
            role="worker",
        ).to_json()
        return record

    def test_teardown_requires_a_durable_cleanup_owner(self) -> None:
        real = CommandHostRuntime(
            FakeCatalog(), self.root / "data", mode="real", production_runtime=registered_production_runtime(self.root)
        )
        backend = _RecordingBackend(LOCAL_PTY_RUNTIME)
        real._head_runtimes[LOCAL_PTY_RUNTIME] = backend
        record = self._supervised_record()
        with (
            mock.patch.object(GitWorkspaceManager, "teardown") as removal,
            self.assertRaisesRegex(HostError, "no durable cleanup owner"),
        ):
            real.teardown(record)
        self.assertEqual(backend.calls, [])
        removal.assert_not_called()
        self.fake.teardown(record)
        self.assertEqual(self.fake.stopped, [record.worker])
        self.assertEqual(self.fake.torn_down, [record.worker])

    def test_teardown_returns_the_owners_actual_pending_receipt(self) -> None:
        real = CommandHostRuntime(
            FakeCatalog(), self.root / "data", mode="real", production_runtime=registered_production_runtime(self.root)
        )
        record = self._supervised_record()
        task = {"ref": "ummanu-635"}
        cleanup = mock.Mock(return_value={"status": "pending", "reason": "scope stop failed", "progress": {}})
        real.cleanup_owner = SimpleNamespace(runtime=SimpleNamespace(reader=SimpleNamespace(show=lambda ref: task)),
                                             cleanup=cleanup)
        with mock.patch.object(GitWorkspaceManager, "teardown") as removal:
            result = real.teardown(record)
        cleanup.assert_called_once_with(task, record, "done")
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["reason"], "scope stop failed")
        removal.assert_not_called()


class RuntimeWiringContractTests(unittest.TestCase):
    def test_production_unit_puts_local_bin_on_path(self) -> None:
        """The dispatcher shells out to `orca` by bare name. systemd starts with a minimal PATH, so
        the unit must carry one that contains the install dir, or every host call fails at runtime
        while the whole unit test suite stays green."""
        product_root = Path(__file__).resolve().parents[1]
        unit = product_root / "packaging" / "systemd" / "ummanu-dispatcher-production.service"
        rendered = render_systemd_unit(
            unit.read_bytes(),
            SystemdLayout(
                product_root,
                Path("/srv/secretary-instance"),
                Path("/srv/ummanu-data"),
                "operator",
                Path("/home/operator"),
            ),
        )
        lines = [
            line.split("=", 1)[1]
            for line in rendered.decode("utf-8").splitlines()
            if line.startswith("Environment=PATH=")
        ]
        self.assertEqual(len(lines), 1, "the production unit must declare exactly one PATH")
        self.assertIn("/home/operator/.local/bin", lines[0].split("PATH=", 1)[1])

    def test_production_unit_executes_the_stdlib_preflight_before_the_console_entry_point(self) -> None:
        """A broken editable install must be diagnosed before ``ummanu`` can be imported."""
        product_root = Path("/srv/ummanu")
        rendered = render_systemd_unit(
            (SHIPPED_PACKAGING_ROOT / "ummanu-dispatcher-production.service").read_bytes(),
            SystemdLayout(
                product_root,
                Path("/srv/secretary-instance"),
                Path("/srv/ummanu-data"),
                "operator",
                Path("/home/operator"),
            ),
        ).decode("utf-8")
        exec_start = next(line for line in rendered.splitlines() if line.startswith("ExecStart="))
        expected_prefix = (
            "ExecStart=/srv/ummanu/.venv/bin/python3 -I "
            "/srv/ummanu/src/ummanu/dispatch/runtime_preflight.py "
            "--product-root /srv/ummanu --interpreter /srv/ummanu/.venv/bin/python3 "
            "--data-dir /srv/ummanu-data -- "
        )
        self.assertTrue(exec_start.startswith(expected_prefix), exec_start)
        self.assertEqual(
            exec_start[len(expected_prefix) :],
            "/srv/ummanu/.venv/bin/ummanu dispatcher production-tick "
            "--instance /srv/secretary-instance",
        )
        self.assertNotIn("PYTHONPATH", exec_start)


class HeadRegistrySourceContractTests(unittest.TestCase):
    """The live catalog reads the installation's registry, never the checkout it was imported from.

    The production unit starts the dispatcher out of the working checkout, so comparing the live
    registry against that checkout made an unmerged `heads.toml` commit stop every tick.
    """

    def instance(self, root: Path, snapshot: str) -> Path:
        (root / "instance.yaml").write_text(
            "version: 1\nname: contract\ndata_dir: "
            + str(root / "data")
            + "\noffsite:\n  instance_remote: git@example.invalid:x/y.git\n"
            + "host:\n  unit_prefix: ummanu-\n",
            encoding="utf-8",
        )
        canonical = root / "heads" / "heads.toml"
        rendered = snapshot_header(canonical) + snapshot
        pair = generated_pair(root)
        pair.snapshot.parent.mkdir(parents=True)
        pair.snapshot.write_text(rendered, encoding="utf-8")
        pair.source.write_text(
            "canonical: " + str(canonical) + "\n"
            "canonical_owner: instance\n"
            "product_root: /fixture/product\n"
            "revision: fixture\n"
            "snapshot_sha256: " + hashlib.sha256(rendered.encode("utf-8")).hexdigest() + "\n",
            encoding="utf-8",
        )
        return root

    def snapshot(self, *, role_default: str = "installed-head") -> str:
        return (
            "resources:\n  installed-resource:\n    account: installed-account\n"
            "profiles:\n  installed-head:\n    resource: installed-resource\n    adapter: claude\n"
            f"role_defaults:\n  new_card: {role_default}\n"
        )

    def test_catalog_runs_off_the_installation_snapshot_not_the_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            instance = self.instance(Path(tmpdir), self.snapshot())

            catalog = InstanceCatalog(instance)

            # Deliberately a registry the product canon does not contain: had the catalog compared
            # the snapshot against this checkout, construction would have raised `invalid_heads`.
            self.assertEqual(catalog.head_profile("installed-head")["adapter"], "claude")
            self.assertEqual(catalog.worker_head({}), "installed-head")

    def test_a_broken_installation_snapshot_still_stops_the_tick_by_name(self) -> None:
        broken = {
            "missing table": "profiles:\n  installed-head:\n    adapter: claude\n",
            "unknown resource": (
                "resources: {}\nprofiles:\n  installed-head:\n    resource: gone\n    adapter: claude\n"
                "role_defaults:\n  new_card: installed-head\n"
            ),
            "unrouted role": self.snapshot(role_default="not-a-head"),
        }
        for name, snapshot in broken.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmpdir:
                instance = self.instance(Path(tmpdir), snapshot)

                with self.assertRaises(DispatcherError) as caught:
                    InstanceCatalog(instance)

                self.assertEqual(caught.exception.code, "invalid_heads")
                self.assertIn("heads.yaml", str(caught.exception))


class RoleRoutingGenerationTests(unittest.TestCase):
    """One registry generation routes all six roles, whichever process is asking.

    The dispatcher routes worker, reviewer and observer off the installation snapshot; the
    background agents' driver resolves curator, retro and steward through the pipeline registry.
    Those used to be two different files — the checkout's `heads.toml` and the installation's
    `heads.yaml` — so a role could be routed by one generation and launched by another.
    """

    CANON = (
        '[resources.owned-sub]\naccount = "owned"\nprobe = "true"\n'
        '[profiles.owned-worker]\nresource = "owned-sub"\nadapter = "claude"\nfallback = []\n'
        '[profiles.owned-reviewer]\nresource = "owned-sub"\nadapter = "claude"\nfallback = []\n'
        '[profiles.owned-observer]\nresource = "owned-sub"\nadapter = "claude"\nfallback = []\n'
        '[profiles.owned-watcher]\nresource = "owned-sub"\nadapter = "claude"\nfallback = []\n'
        '[role_defaults]\nnew_card = "owned-worker"\nreviewer = "owned-reviewer"\n'
        'observer = "owned-observer"\ncurator = "owned-watcher"\nretro = "owned-watcher"\n'
        'steward = "owned-watcher"\n'
    )

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.instance = Path(tmp.name)
        (self.instance / "instance.yaml").write_text(
            "version: 1\nname: routing\ndata_dir: "
            + str(self.instance / "data")
            + "\noffsite:\n  instance_remote: git@example.invalid:x/y.git\n"
            + "host:\n  unit_prefix: ummanu-\n",
            encoding="utf-8",
        )
        (self.instance / "heads").mkdir()
        heads._load_registry.cache_clear()
        self.addCleanup(heads._load_registry.cache_clear)

    def materialize(self, canon: str | None) -> None:
        if canon is not None:
            (self.instance / "heads" / "heads.toml").write_text(canon, encoding="utf-8")
        materialize_snapshot(self.instance, upgrade.running_product_root())
        record_source(self.instance, upgrade.running_product_root())

    def test_the_installations_own_canon_routes_every_role(self) -> None:
        self.materialize(self.CANON)
        catalog = InstanceCatalog(self.instance)

        with mock.patch.dict(os.environ, {"UMMANU_INSTANCE": str(self.instance)}):
            self.assertEqual(heads.registry_path(), self.instance / "data" / "heads" / "heads.yaml")
            self.assertEqual(catalog.worker_head({}), "owned-worker")
            self.assertEqual(catalog.review_head({}), "owned-reviewer")
            self.assertEqual(catalog.observer_head(), "owned-observer")
            for agent in ("curator", "retro", "steward"):
                with self.subTest(agent=agent):
                    spec = {"head": "product-static-head"}
                    self.assertEqual(dispatch._preferred_head(agent, spec), "owned-watcher")

    def test_an_installation_with_no_canon_runs_the_product_defaults(self) -> None:
        self.materialize(None)
        product = canonical_heads(upgrade.running_product_root())["role_defaults"]
        catalog = InstanceCatalog(self.instance)

        with mock.patch.dict(os.environ, {"UMMANU_INSTANCE": str(self.instance)}):
            self.assertEqual(catalog.worker_head({}), product["new_card"])
            self.assertEqual(catalog.review_head({}), product["reviewer"])
            self.assertEqual(catalog.observer_head(), product["observer"])
            for agent in ("curator", "retro", "steward"):
                with self.subTest(agent=agent):
                    self.assertEqual(dispatch._preferred_head(agent, {}), product[agent])

    def test_a_checkout_with_no_installation_reads_the_shipped_registry(self) -> None:
        """No `UMMANU_INSTANCE` means no installation to read — never the host's own."""
        env = {key: value for key, value in os.environ.items() if key != "UMMANU_INSTANCE"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(heads.registry_path(), heads.HEADS_TOML)

    def test_claude_effort_is_validated_and_rendered_by_shared_launcher(self) -> None:
        resources = {"claude-sub": {"account": "subscription"}}
        profiles = {
            "opus-medium": {
                "resource": "claude-sub",
                "adapter": "claude",
                "model": "opus",
                "effort": "medium",
                "fallback": [],
            }
        }
        heads.validate_registry(resources, profiles)
        registry = heads.Registry(resources, profiles)

        rendered = render_head_command(
            registry.profile("opus-medium"),
            role="reviewer",
            prompt="review",
            binding=STANDING_BINDING,
        )

        self.assertIn("--model opus --effort medium", rendered.command)
        # A claude command seeds its own prompt, so there is nothing left to deliver into it.
        self.assertFalse(rendered.prompt_after_start)
        with self.assertRaisesRegex(heads.HeadRegistryError, "unknown claude effort"):
            heads.validate_registry(
                resources,
                {"bad": {**profiles["opus-medium"], "effort": "unbounded"}},
            )

    def test_a_selected_installation_without_a_usable_snapshot_fails_by_its_path(self) -> None:
        """The product default is for no installation at all, not for a broken one.

        Falling back here would route a packaged role off whichever checkout the process happens
        to run from — the mutable file the snapshot exists to keep out of a live tick — and would
        abandon the instance the operator selected without saying so.
        """
        for name, build in (
            ("missing", lambda snapshot: None),
            ("directory", lambda snapshot: snapshot.mkdir()),
            ("dangling", lambda snapshot: snapshot.symlink_to(snapshot.parent / "gone.yaml")),
        ):
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                instance = Path(tmp)
                (instance / "instance.yaml").write_text(
                    f"version: 1\nname: broken\ndata_dir: {instance / 'data'}\n"
                    "offsite:\n  instance_remote: git@example.invalid:x/y.git\n",
                    encoding="utf-8",
                )
                (instance / "heads").mkdir()
                (instance / "heads" / "heads.toml").write_text(self.CANON, encoding="utf-8")
                # Where the pair now lives; the live root has no legacy copy to fall back to.
                snapshot = instance / "data" / "heads" / "heads.yaml"
                snapshot.parent.mkdir(parents=True)
                build(snapshot)

                with mock.patch.dict(os.environ, {"UMMANU_INSTANCE": str(instance)}):
                    heads._load_registry.cache_clear()
                    self.assertEqual(heads.registry_path(), snapshot)
                    with self.assertRaises(heads.HeadRegistryError) as caught:
                        heads.load_registry()

                self.assertIn(str(snapshot), str(caught.exception))

    def test_a_selected_installation_with_no_data_directory_fails_instead_of_the_default(self) -> None:
        """An instance whose `instance.yaml` names no usable data directory has no pair to find."""
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            (instance / "instance.yaml").write_text("version: 1\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"UMMANU_INSTANCE": str(instance)}):
                heads._load_registry.cache_clear()
                with self.assertRaisesRegex(heads.HeadRegistryError, "cannot locate the head registry"):
                    heads.registry_path()


class PackagedRoleUnitInstanceTests(unittest.TestCase):
    """Every packaged role process resolves the instance its unit was rendered for.

    A role that is handed no `UMMANU_INSTANCE` falls back to the default installation path, so
    on a host with a real installation a unit rendered for another instance would quietly route
    off the default live root's heads instead of its own.
    """

    UNITS = (
        "ummanu-dispatcher-production.service",
        "ummanu-curator.service",
        "ummanu-retro.service",
        "ummanu-steward.service",
        "ummanu-steward-deep-sweep.service",
    )

    # The layout resolves a home directory through `pwd`, so the account has to exist wherever
    # the suite runs. The invoking account is the one guaranteed to.
    RUNTIME_USER = pwd.getpwuid(os.getuid()).pw_name

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        # Deliberately not the default instance path: the assertion below is only worth anything
        # if the wrong answer is a different directory.
        self.instance = self.root / "other-instance"
        (self.instance / "heads").mkdir(parents=True)
        (self.instance / "heads" / "heads.toml").write_text(
            RoleRoutingGenerationTests.CANON, encoding="utf-8"
        )
        (self.instance / "instance.yaml").write_text(
            f"version: 1\nname: other\ndata_dir: {self.root / 'data'}\n"
            "offsite:\n  instance_remote: git@example.invalid:x/y.git\n",
            encoding="utf-8",
        )
        (self.instance / "runtime.env").write_text(legacy_runtime_lines(), encoding="utf-8")
        materialize_snapshot(self.instance, upgrade.running_product_root())
        record_source(self.instance, upgrade.running_product_root())
        self.layout = SystemdLayout(
            product_root=self.root / "product",
            instance_path=self.instance,
            data_dir=self.root / "data",
            runtime_user=self.RUNTIME_USER,
            runtime_home=self.root / "home",
        )
        heads._load_registry.cache_clear()
        self.addCleanup(heads._load_registry.cache_clear)

    def unit_env(self, name: str) -> dict[str, str]:
        rendered = render_systemd_unit((SHIPPED_PACKAGING_ROOT / name).read_bytes(), self.layout).decode()
        env = {}
        for line in rendered.splitlines():
            if line.startswith("Environment="):
                key, value = line[len("Environment=") :].split("=", 1)
                env[key] = value
        return env

    def test_every_role_unit_carries_the_rendered_instance_path(self) -> None:
        for name in self.UNITS:
            with self.subTest(unit=name):
                self.assertEqual(self.unit_env(name)["UMMANU_INSTANCE"], str(self.instance))

    def test_the_units_apply_would_install_carry_it_too(self) -> None:
        """Through `resolve_packaged`, the compile step `reconcile apply` actually installs from."""
        packaged = resolve_packaged(
            {"host": {"unit_prefix": "ummanu-"}, "data_dir": str(self.root / "data")},
            SHIPPED_PACKAGING_ROOT,
            product_root=self.root / "product",
            instance_path=self.instance,
            data_dir=self.root / "data",
            runtime_user=self.RUNTIME_USER,
        )
        compiled = {unit.name: unit.content for unit in packaged}

        self.assertLessEqual(set(self.UNITS), set(compiled))
        for name in self.UNITS:
            with self.subTest(unit=name):
                self.assertIn(f"Environment=UMMANU_INSTANCE={self.instance}".encode(), compiled[name])

    def test_the_role_process_resolves_that_instances_registry(self) -> None:
        """Through role_env, the way the unit actually starts the process."""
        for name, role in (
            ("ummanu-curator.service", "curator"),
            ("ummanu-retro.service", "retro"),
            ("ummanu-steward.service", "steward"),
            ("ummanu-steward-deep-sweep.service", "steward"),
            ("ummanu-dispatcher-production.service", "pipeline"),
        ):
            with self.subTest(unit=name):
                env = role_env.runtime_env(
                    role,
                    base_env=self.unit_env(name),
                    env_file=self.instance / "runtime.env",
                )

                with mock.patch.dict(os.environ, env, clear=True):
                    heads._load_registry.cache_clear()
                    self.assertEqual(heads.registry_path(), self.root / "data" / "heads" / "heads.yaml")
                    self.assertEqual(heads.default_head(), "owned-worker")
                    self.assertEqual(heads.reviewer_head(), "owned-reviewer")

    def test_every_role_unit_names_the_runtime_env_file_of_its_own_instance(self) -> None:
        """A role that reloads runtime.env has to reload the selected installation's copy.

        `EnvironmentFile=` alone only seeds the unit's own process. Both role-env modules resolve
        the file again for the process they launch, from a name that defaults to the default
        installation, so the unit has to export it.
        """
        expected = str(self.instance / "runtime.env")
        for name in self.UNITS:
            with self.subTest(unit=name):
                env = self.unit_env(name)
                if name == "ummanu-dispatcher-production.service":
                    self.assertEqual(env["UMMANU_RUNTIME_ENV_FILE"], expected)
                self.assertEqual(env["TA_RUNTIME_ENV_FILE"], expected)

    def test_the_runtime_env_file_cannot_hand_a_role_another_instance(self) -> None:
        """The launcher's binding wins over the file's own line, for a head and a standing role."""
        self.decoy_runtime_env()
        for role in ("worker", "steward"):
            with self.subTest(role):
                base = self.unit_env("ummanu-dispatcher-production.service")
                env = role_env.runtime_env(role, base_env=base, env_file=self.instance / "runtime.env")

                self.assertEqual(env["UMMANU_INSTANCE"], str(self.instance))

    def test_every_background_unit_carries_the_product_root_it_was_rendered_for(self) -> None:
        """An upgrade from an alternate checkout renders these units against that checkout.

        Nothing else tells a launched process which product to import: the gate resolves
        ``$HOME/ummanu`` when neither name is set, which on an upgraded host is the checkout
        the installation was moved off. Every one of these services launches something further —
        heads through the dispatcher, task commands and curator memory writes through the deferred
        product lookup — so the binding has to be on the unit, not only on the dispatcher.
        """
        for name in self.UNITS:
            with self.subTest(unit=name):
                env = self.unit_env(name)

                self.assertEqual(env["UMMANU_REPO"], str(self.root / "product"))
                # The gate-launched services also import from it directly; the dispatcher runs the
                # checkout's own venv entry point and needs no PYTHONPATH of its own.
                self.assertEqual(
                    env.get("TA_RUNTIME_PYTHONPATH", str(self.root / "product")),
                    str(self.root / "product"),
                )

    def test_rendered_gate_units_name_the_same_checkout_the_gate_resolves(self) -> None:
        """A materialized alternate checkout cannot bind a role unit to one root and its gate to another."""
        for name in (
            "ummanu-curator.service",
            "ummanu-retro.service",
            "ummanu-steward.service",
            "ummanu-steward-deep-sweep.service",
        ):
            with self.subTest(unit=name):
                rendered = render_systemd_unit(
                    (SHIPPED_PACKAGING_ROOT / name).read_bytes(), self.layout
                ).decode()
                self.assertIn(f"Environment=TA_RUNTIME_PYTHONPATH={self.root / 'product'}", rendered)
                self.assertIn(f"Environment=UMMANU_REPO={self.root / 'product'}", rendered)
                self.assertIn(f"ExecStart={self.root / 'product'}/scripts/ummanu-agent-gate.sh", rendered)

    def test_the_launched_head_command_names_that_checkout_rather_than_a_home(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                **self.unit_env("ummanu-dispatcher-production.service"),
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": str(self.root / "home"),
            },
            clear=True,
        ):
            command = wrap_role_command("worker", "true")

        self.assertIn(f"UMMANU_REPO={self.root / 'product'}", command)
        self.assertIn(f"PYTHONPATH={self.root / 'product'}", command)
        self.assertNotIn("$HOME/ummanu", command)

    def test_a_dispatcher_launched_head_imports_the_checkout_the_unit_named(self) -> None:
        """End to end, in a terminal that has no ``UMMANU_REPO`` of its own.

        The rendered checkout has to be the real one here, because the wrapper runs
        ``ummanu.runtime.role_env`` out of it; the layout points at it the way an alternate upgrade
        would, and the launching shell is given a home where no checkout exists at all.
        """
        product_env = guarded_product_env(self.root)
        product = Path(product_env["UMMANU_REPO"])
        layout = SystemdLayout(
            product_root=product,
            instance_path=self.instance,
            data_dir=self.root / "data",
            runtime_user=self.RUNTIME_USER,
            runtime_home=self.root / "home",
        )
        rendered = render_systemd_unit(
            (SHIPPED_PACKAGING_ROOT / "ummanu-dispatcher-production.service").read_bytes(),
            layout,
        ).decode()
        unit_env = dict(
            line[len("Environment=") :].split("=", 1)
            for line in rendered.splitlines()
            if line.startswith("Environment=")
        )
        with mock.patch.dict(
            os.environ, {**unit_env, "PATH": product_env["PATH"]}, clear=True
        ):
            workspace_python = self.root / ".ummanu-task-env" / "venv" / "bin" / "python3"
            workspace_python.parent.mkdir(parents=True)
            workspace_python.symlink_to(sys.executable)
            command = wrap_role_command("worker", "printenv UMMANU_REPO", workspace=str(self.root))

        result = subprocess.run(
            ["/bin/sh", "-c", command],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
            env={
                "PATH": product_env["PATH"],
                "HOME": str(self.root / "home"),
            },
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(product), result.stderr)
        self.assertFalse((self.root / "docker-calls").exists(), "the checkout probe must not call native Docker")

    def test_a_dispatcher_launched_head_resolves_the_selected_instance(self) -> None:
        """End to end through the rendered command, the way a head actually starts.

        Orca creates the head's terminal, so it inherits nothing the dispatcher unit exported:
        the binding has to travel inside the command string. The subprocess here is given an
        environment with no `UMMANU_*` in it at all, which is what the reviewer's reproduction
        found routing heads back to `/home/dev/secretary-instance`.
        """
        self.decoy_runtime_env()
        bound = self.unit_env("ummanu-dispatcher-production.service")
        product_env = guarded_product_env(self.root)
        bound.update(product_env)
        with mock.patch.dict(os.environ, bound, clear=True):
            workspace_python = self.root / ".ummanu-task-env" / "venv" / "bin" / "python3"
            workspace_python.parent.mkdir(parents=True)
            workspace_python.symlink_to(sys.executable)
            command = wrap_role_command("worker", "printenv UMMANU_INSTANCE", workspace=str(self.root))

        # The role wrapper starts in a worktree.  A package there must not shadow the selected
        # control plane merely because Python's default path would put the cwd first.
        shadow = self.root / "ummanu"
        shadow.mkdir()
        (shadow / "__init__.py").write_text("raise RuntimeError('shadow package imported')\n")

        result = subprocess.run(
            ["/bin/sh", "-c", command],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=self.root,
            env={
                "PATH": product_env["PATH"],
                "HOME": str(self.root),
                "UMMANU_REPO": product_env["UMMANU_REPO"],
            },
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(self.instance), result.stderr)
        self.assertFalse((self.root / "docker-calls").exists(), "the instance probe must not call native Docker")

    def decoy_runtime_env(self) -> None:
        """The selected instance's runtime.env, carrying the default installation's own name.

        A copied or inherited `UMMANU_INSTANCE` line is exactly how a non-default installation
        loses its heads, so the fixture keeps one in the file the roles read.
        """
        (self.instance / "runtime.env").write_text(
            legacy_runtime_lines() + "UMMANU_INSTANCE=/home/dev/secretary-instance\n"
            "UMMANU_OBSERVER_SPRINT=sprint:somebody-else\n"
            "UMMANU_OBSERVER_GENERATION=forged\n",
            encoding="utf-8",
        )

    def test_an_observer_head_carries_the_sprint_the_launcher_bound_it_to(self) -> None:
        """End to end, against a `runtime.env` claiming another sprint.

        The identity travels in the command the launcher renders, and `runtime_env` treats it the
        way it treats the installation: a file inside the installation may not rename the caller,
        or a copied line would let a head sign for a sprint it was never launched for.
        """
        self.decoy_runtime_env()
        identity = observer_binding("sprint:1126", "abc123def456")
        bound = self.unit_env("ummanu-dispatcher-production.service")
        # An observer head starts only under the product's managed venv.
        product = managed_product_root(self.root)
        bound["UMMANU_REPO"] = str(product)
        with mock.patch.dict(os.environ, bound, clear=True):
            command = wrap_role_command(
                "observer",
                "printenv UMMANU_OBSERVER_SPRINT; printenv UMMANU_OBSERVER_GENERATION",
                identity=identity,
            )

        result = subprocess.run(
            ["/bin/sh", "-c", command],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": str(self.root),
                "UMMANU_REPO": str(product),
            },
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), ["sprint:1126", "abc123def456"], result.stderr)

    def test_a_worker_head_is_never_given_an_observer_binding(self) -> None:
        """The binding is one role's, and a launcher asking for it elsewhere is a defect."""
        with self.assertRaisesRegex(HeadCommandError, "UMMANU_OBSERVER_SPRINT"):
            wrap_role_command(
                "worker",
                "true",
                identity=observer_binding("sprint:1126", "abc123def456"),
            )


class CodexIsInteractiveOnlyTests(unittest.TestCase):
    """Every Codex head the product can launch is one interactive session (secretary-1173)."""

    RESOURCES: ClassVar = {"openai-sub": {"account": "openai-subscription", "probe": "true"}}

    def test_the_portable_registry_states_the_mode_on_every_codex_profile(self) -> None:
        """Stated, not defaulted: the generated installation snapshot is a copy of these tables,
        so an operator reading the snapshot sees the mode rather than having to know the default."""
        canon = canonical_heads(upgrade.running_product_root())

        codex = {
            pid: profile for pid, profile in canon["profiles"].items() if profile.get("adapter") == "codex"
        }
        self.assertTrue(codex, "the portable registry is expected to ship Codex heads")
        for pid, profile in codex.items():
            with self.subTest(profile=pid):
                self.assertEqual(profile.get("codex_mode"), "tui")

    def test_the_generated_installation_registry_carries_the_mode_too(self) -> None:
        """What a live tick actually reads is the snapshot, not this checkout's canon."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        instance = Path(tmp.name)
        (instance / "instance.yaml").write_text(
            "version: 1\nname: modes\ndata_dir: " + str(instance / "data") + "\n"
            "offsite:\n  instance_remote: git@example.invalid:x/y.git\n",
            encoding="utf-8",
        )
        (instance / "heads").mkdir()

        materialize_snapshot(instance, upgrade.running_product_root())
        record_source(instance, upgrade.running_product_root())
        installed = installed_heads(instance)

        codex = {
            pid: profile
            for pid, profile in installed["profiles"].items()
            if profile.get("adapter") == "codex"
        }
        self.assertTrue(codex)
        for pid, profile in codex.items():
            with self.subTest(profile=pid):
                self.assertEqual(profile.get("codex_mode"), "tui")

    def test_every_role_default_and_fallback_target_is_reachable_and_interactive(self) -> None:
        """Role defaults and fallback chains reach nothing that would launch another way."""
        canon = canonical_heads(upgrade.running_product_root())
        profiles = canon["profiles"]

        reachable = set(canon["role_defaults"].values())
        for profile in profiles.values():
            reachable.update(profile.get("fallback") or [])
        for pid in sorted(reachable):
            with self.subTest(profile=pid):
                profile = profiles[pid]
                if profile.get("adapter") == "codex":
                    self.assertEqual(profile.get("codex_mode"), "tui")

    def test_a_missing_mode_resolves_to_the_interactive_one(self) -> None:
        profiles = {"bare": {"resource": "openai-sub", "adapter": "codex", "fallback": []}}
        heads.validate_registry(self.RESOURCES, profiles)
        registry = heads.Registry(self.RESOURCES, profiles)

        rendered = render_head_command(
            registry.profile("bare"),
            role="worker",
            prompt="do the card",
            workspace="/tmp/ws",
            binding=STANDING_BINDING,
        )

        self.assertTrue(rendered.prompt_after_start)
        self.assertNotIn("codex exec", rendered.command)
        self.assertIn("codex --dangerously-bypass-approvals-and-sandbox", rendered.command)
        self.assertNotIn("do the card", rendered.command)
        self.assertEqual(registry.profile("bare").get("codex_mode"), None)

    def test_a_registry_that_still_pins_exec_is_refused(self) -> None:
        """Fail closed rather than launch: there is no renderer left for what it asks for."""
        with self.assertRaisesRegex(heads.HeadRegistryError, "unknown codex launch mode 'exec'"):
            heads.validate_registry(
                self.RESOURCES,
                {
                    "old": {
                        "resource": "openai-sub",
                        "adapter": "codex",
                        "codex_mode": "exec",
                        "fallback": [],
                    }
                },
            )

    def test_no_role_can_be_rendered_a_codex_exec_command(self) -> None:
        """Sweep the shipped registry: every profile, every role that launches one."""
        canon = canonical_heads(upgrade.running_product_root())
        registry = heads.Registry(canon["resources"], canon["profiles"], canon["role_defaults"])

        for pid in registry.known():
            for role in ("worker", "reviewer", "observer", "curator", "retro", "steward"):
                with self.subTest(profile=pid, role=role):
                    rendered = render_head_command(
                        registry.profile(registry.resolve(pid)),
                        role=role,
                        prompt="skill",
                        workspace="/tmp/ws",
                        binding=STANDING_BINDING,
                    )
                    self.assertNotIn("codex exec", rendered.command)

    def test_every_role_that_launches_a_codex_head_prepares_its_workspace(self) -> None:
        """The trust preflight follows the adapter, not the role.

        Until secretary-1173 the dispatcher ran it for the observer alone, on the reasoning that
        worker and reviewer workspaces hang off repositories the runtime already trusts. That is a
        property of a host that has been running codex heads, not of the product: on a clean host
        every one of these roles brings up a TUI that will sit on the dialog instead of taking its
        prompt. Swept over the shipped registry so a new codex role default cannot quietly miss it.
        """
        canon = canonical_heads(upgrade.running_product_root())
        codex_heads = sorted(
            pid for pid, profile in canon["profiles"].items() if profile.get("adapter") == "codex"
        )
        self.assertTrue(codex_heads)

        for role in ("worker", "reviewer", "observer", "curator", "retro", "steward"):
            for head in codex_heads:
                with self.subTest(role=role, head=head):
                    tmp = tempfile.TemporaryDirectory()
                    self.addCleanup(tmp.cleanup)
                    home = Path(tmp.name) / "codex-home"
                    workspace = Path(tmp.name) / "ws"
                    workspace.mkdir()
                    catalog = object.__new__(InstanceCatalog)
                    catalog._heads = canon  # type: ignore[attr-defined]

                    with mock.patch.dict(os.environ, {"TA_CODEX_HOME": str(home)}):
                        catalog.prepare_head_workspace(head, str(workspace), role=role)

                    trusted = (home / "config.toml").read_text(encoding="utf-8")
                    self.assertIn(str(workspace.resolve()), trusted)
                    self.assertIn('trust_level = "trusted"', trusted)

    def test_the_preflight_is_shared_with_the_automations_launcher(self) -> None:
        """One implementation reachable from both sides, and the dependency direction that forces
        where it lives: the runtime module imports nothing else of `ummanu`."""
        from ummanu.automations.runtime import dispatch as ta_dispatch
        from ummanu.runtime import codex_preflight

        self.assertIs(
            dispatcher_launcher._preflight_codex_workspace, codex_preflight.ensure_codex_workspace_trusted
        )
        self.assertIs(ta_dispatch.preflight_codex_launch, codex_preflight.preflight_codex_launch)
        source = Path(codex_preflight.__file__).read_text(encoding="utf-8")
        self.assertNotIn("import ummanu", source)
        self.assertNotIn("from ummanu", source)

    def test_an_ordinary_id_resolves_to_itself(self) -> None:
        """Whatever family an id names, the registry that defines it is the one answer."""
        profiles = {
            "claude-opus-medium": {"resource": "openai-sub", "adapter": "claude", "fallback": []},
            "codex-terra-high": {"resource": "openai-sub", "adapter": "codex", "fallback": []},
            "hermes": {"resource": "openai-sub", "adapter": "hermes", "fallback": []},
        }
        registry = heads.Registry(self.RESOURCES, profiles)

        for pid in profiles:
            with self.subTest(head=pid):
                self.assertEqual(registry.resolve(pid), pid)

    def test_a_retired_id_fails_closed_by_name(self) -> None:
        """secretary-1697: no alias table stands a retired id in for a profile of today's registry.

        A card override or an agent spec written before the installation renamed its profiles is
        refused under its own name, even when a profile of the same family and effort exists.
        """
        profiles = {
            "codex-terra-high": {
                "resource": "openai-sub",
                "adapter": "codex",
                "effort": "high",
                "fallback": [],
            },
            "codex-sol-medium": {"resource": "openai-sub", "adapter": "codex", "fallback": []},
        }
        registry = heads.Registry(self.RESOURCES, profiles)

        for retired in ("codex", "codex-high", "codex-reviewer", "codex-terra", "claude-opus", ""):
            with (
                self.subTest(head=retired),
                self.assertRaisesRegex(heads.HeadRegistryError, f"unknown head {retired!r}"),
            ):
                registry.resolve(retired)

    def test_a_persisted_record_carrying_a_retired_id_still_loads(self) -> None:
        """Read paths take any string; only launch resolution is strict."""
        worker_run = {
            "role": "worker",
            "head": "codex-high",
            "head_source": "role_default",
            "adapter": "codex",
            "model": "gpt-5.6-terra",
            "model_source": "profile",
            "effort": "high",
        }
        record = DispatcherRecord(
            worker="worker-1",
            workspace="/tmp/card",
            handle="pane-1",
            head="codex-high",
            review_head="codex-reviewer",
            attempt_id="attempt-1",
            comment_baseline=0,
            review_baseline=0,
            state="claimed",
            claimed_at=1.0,
            worker_run=worker_run,
        )

        loaded = DispatcherRecord.from_json(record.to_json())

        self.assertEqual((loaded.head, loaded.review_head), ("codex-high", "codex-reviewer"))
        self.assertEqual(loaded.worker_run.snapshot.head, "codex-high")
        self.assertEqual(loaded.worker_run.to_json(), worker_run)

        catalog = object.__new__(InstanceCatalog)
        catalog._heads = {  # type: ignore[attr-defined]
            "resources": self.RESOURCES,
            "profiles": {
                "codex-terra-high": {"resource": "openai-sub", "adapter": "codex", "fallback": []},
            },
            "role_defaults": {"new_card": "codex-terra-high", "reviewer": "codex-terra-high"},
        }
        task = {"routing": {"resolved_worker_head": "codex-high"}}
        with self.assertRaisesRegex(HostError, "'codex-high'"):
            catalog.claimed_worker_head(task)

    def test_role_defaults_are_the_only_default_heads(self) -> None:
        """No product-chosen id stands in for a role the registry routes nowhere."""
        catalog = object.__new__(InstanceCatalog)
        catalog._heads = {  # type: ignore[attr-defined]
            "resources": self.RESOURCES,
            "profiles": {
                "codex-terra-high": {"resource": "openai-sub", "adapter": "codex", "fallback": []},
            },
            "role_defaults": {},
        }

        for role, launch in (
            ("new_card", lambda: catalog.worker_head({})),
            ("reviewer", lambda: catalog.review_head({})),
            ("observer", catalog.observer_head),
        ):
            with self.subTest(role=role), self.assertRaisesRegex(HostError, rf"role_defaults\.{role}\b"):
                launch()
        registry = heads.Registry(self.RESOURCES, catalog._heads["profiles"])  # type: ignore[attr-defined]
        with self.assertRaisesRegex(heads.HeadRegistryError, r"role_defaults\.new_card"):
            heads.default_head(registry)
        with mock.patch.dict(os.environ, {}, clear=False) as env:
            env.pop("TA_REVIEWER_HEAD", None)
            with self.assertRaisesRegex(heads.HeadRegistryError, r"role_defaults\.reviewer"):
                heads.reviewer_head(registry)


class ShippedRegistryTiersTests(unittest.TestCase):
    """secretary-1697: the portable registry names the five pipeline tiers and nothing retired."""

    TIERS: ClassVar = {
        "codex-terra-high": ("codex", "gpt-5.6-terra", "high"),
        "codex-sol-medium": ("codex", "gpt-6-sol", "medium"),
        "codex-sol-high": ("codex", "gpt-6-sol", "high"),
        "claude-opus-medium": ("claude", "opus", "medium"),
        "claude-opus-high": ("claude", "opus", "high"),
    }
    RETIRED: ClassVar = (
        "codex", "codex-tui", "codex-high", "codex-extra", "codex-reviewer", "codex-observer",
        "codex-curator", "codex-steward", "codex-retro", "codex-sol", "codex-terra", "codex-luna",
        "codex-5-4", "codex-mini", "codex-spark", "claude-default", "claude-sonnet", "claude-opus",
        "claude-observer", "claude-fable",
    )
    # `<family>-<model>-<effort>`; a `*-local-pty` runtime variant and the `hermes` fallback family
    # are the only ids allowed another shape.
    PROFILE_ID = re.compile(r"^(claude|codex)-[a-z0-9.]+(-[a-z0-9.]+)*-(low|medium|high|xhigh|max|extra)$")

    @classmethod
    def id_is_well_formed(cls, pid: str, profile: dict) -> bool:
        if pid.endswith("-local-pty") or profile.get("adapter") == "hermes":
            return True
        return bool(cls.PROFILE_ID.match(pid)) and pid.split("-", 1)[0] == profile.get("adapter")

    def test_the_shipped_registry_is_exactly_the_five_tiers(self) -> None:
        canon = canonical_heads(upgrade.running_product_root())
        profiles = canon["profiles"]

        self.assertEqual(sorted(profiles), sorted(self.TIERS))
        for pid, (adapter, model, effort) in self.TIERS.items():
            with self.subTest(profile=pid):
                self.assertEqual(
                    (profiles[pid]["adapter"], profiles[pid]["model"], profiles[pid]["effort"]),
                    (adapter, model, effort),
                )
        for retired in self.RETIRED:
            self.assertNotIn(retired, profiles)

    def test_every_shipped_id_follows_the_naming_rule(self) -> None:
        canon = canonical_heads(upgrade.running_product_root())
        for pid, profile in canon["profiles"].items():
            with self.subTest(profile=pid):
                self.assertTrue(self.id_is_well_formed(pid, profile), pid)

    def test_the_naming_rule_admits_only_the_allowed_variants(self) -> None:
        cases = {
            "codex-terra-high": ({"adapter": "codex"}, True),
            "claude-opus-medium": ({"adapter": "claude"}, True),
            "claude-steward-local-pty": ({"adapter": "claude", "runtime": "local-pty"}, True),
            "hermes": ({"adapter": "hermes"}, True),
            "codex": ({"adapter": "codex"}, False),
            "codex-high": ({"adapter": "codex"}, False),
            "claude-default": ({"adapter": "claude"}, False),
            "claude-opus": ({"adapter": "claude"}, False),
            "codex-sol-medium": ({"adapter": "claude"}, False),
        }
        for pid, (profile, expected) in cases.items():
            with self.subTest(profile=pid):
                self.assertEqual(self.id_is_well_formed(pid, profile), expected)

    def test_role_defaults_route_to_tiers_with_a_cross_family_reviewer(self) -> None:
        canon = canonical_heads(upgrade.running_product_root())
        role_defaults = canon["role_defaults"]
        profiles = canon["profiles"]

        for role in ("new_card", "reviewer", "observer", "curator", "retro", "steward"):
            with self.subTest(role=role):
                self.assertIn(role_defaults[role], self.TIERS)
        self.assertNotEqual(
            profiles[role_defaults["reviewer"]]["adapter"], profiles[role_defaults["new_card"]]["adapter"]
        )

    def test_shipped_automation_specs_name_shipped_tiers(self) -> None:
        agents = upgrade.agents_root(Path(upgrade.running_product_root()))
        assert agents is not None
        specs = sorted(agents.glob("*/automation.toml"))
        self.assertTrue(specs, agents)
        for spec in specs:
            head = tomllib.loads(spec.read_text(encoding="utf-8")).get("head")
            if head is None:
                continue
            with self.subTest(agent=spec.parent.name):
                self.assertIn(head, self.TIERS)


class PerProfileRuntimeTests(unittest.TestCase):
    """secretary-1467: which backend a head is held by is the profile's own answer.

    The value has to survive four boundaries to be worth anything, and each one is a test here:
    the registry that names it, the `HeadSpec` that carries it, the durable run record a later
    tick reads it back from, and the published `heads.yaml` a live installation actually runs off.
    """

    RESOURCES: ClassVar = {"acct": {"account": "acct", "probe": "true"}}

    def _profiles(self, **profile: object) -> dict:
        return {"head": {"resource": "acct", **profile}}

    # -- criterion 1: the key, and what its absence means --------------------------------------

    def test_a_profile_may_name_local_pty_and_naming_none_is_the_same(self) -> None:
        """secretary-1718: a profile's absent key is `local-pty`; secretary-1722: `orca-legacy` is refused."""
        with self.assertRaisesRegex(heads.HeadRegistryError, "unknown runtime 'orca-legacy'.*drop the key"):
            heads.validate_registry(self.RESOURCES, self._profiles(adapter="claude", runtime=ORCA_LEGACY_RUNTIME))
        for named, expected in (
            (LOCAL_PTY_RUNTIME, LOCAL_PTY_RUNTIME),
            (None, LOCAL_PTY_RUNTIME),
        ):
            with self.subTest(runtime=named):
                profile = {"adapter": "claude"}
                if named is not None:
                    profile["runtime"] = named
                profiles = self._profiles(**profile)
                heads.validate_registry(self.RESOURCES, profiles)

                spec = HeadSpec.from_profile("head", profiles["head"])

                self.assertEqual(spec.runtime, expected)
        self.assertEqual(DEFAULT_HEAD_RUNTIME, LOCAL_PTY_RUNTIME)
        self.assertEqual(
            RECORD_RUNTIME_WHEN_ABSENT, ORCA_LEGACY_RUNTIME, "a record's absence must not change hands"
        )

    def test_the_shipped_registry_names_no_runtime_but_local_pty(self) -> None:
        """secretary-1722 (A20 step 2): every shipped tier is a `local-pty` head."""
        shipped = heads.load_registry(heads.HEADS_TOML)

        self.assertTrue(shipped.profiles)
        for profile_id, profile in shipped.profiles.items():
            with self.subTest(profile=profile_id):
                self.assertEqual(profile.get("runtime", DEFAULT_HEAD_RUNTIME), LOCAL_PTY_RUNTIME)
                self.assertEqual(HeadSpec.from_profile(profile_id, profile).runtime, LOCAL_PTY_RUNTIME)

    def test_a_spec_built_by_hand_is_read_by_the_record_rule(self) -> None:
        """Every hand-built spec is a head rebuilt from a record that never named a backend."""
        self.assertEqual(HeadSpec(profile_id="head", adapter="unknown").runtime, ORCA_LEGACY_RUNTIME)

    # -- criterion 2: one refusal, at both readers of a registry -------------------------------

    def test_an_unknown_runtime_is_refused_by_name_at_the_table_and_at_the_one_profile(self) -> None:
        """The same rule refuses it, whether a whole table is loaded or one head is raised."""
        profiles = self._profiles(adapter="claude", runtime="kubernetes")

        with self.assertRaisesRegex(heads.HeadRegistryError, "unknown runtime 'kubernetes'"):
            heads.validate_registry(self.RESOURCES, profiles)
        with self.assertRaisesRegex(HeadSpecError, "unknown runtime 'kubernetes'"):
            HeadSpec.from_profile("head", profiles["head"])

    def test_a_runtime_that_is_not_a_name_is_refused_rather_than_raising_past_the_caller(self) -> None:
        profiles = self._profiles(adapter="claude", runtime=["local-pty"])

        with self.assertRaisesRegex(heads.HeadRegistryError, "runtime must be a name"):
            heads.validate_registry(self.RESOURCES, profiles)

    # -- criterion 3: the runtime is independent of the adapter --------------------------------

    def test_every_adapter_may_stand_on_every_runtime(self) -> None:
        """`runtime` says what holds a head; `adapter` says what the head is. Orthogonal."""
        for adapter in ("claude", "codex", "hermes"):
            for runtime in HEAD_RUNTIMES:
                with self.subTest(adapter=adapter, runtime=runtime):
                    profiles = self._profiles(adapter=adapter, runtime=runtime)
                    heads.validate_registry(self.RESOURCES, profiles)

                    spec = HeadSpec.from_profile("head", profiles["head"])

                    self.assertEqual((spec.adapter, spec.runtime), (adapter, runtime))

    # -- criterion 4: it reaches the place the backend is built --------------------------------

    def test_the_run_record_carries_the_runtime_a_head_was_raised_on(self) -> None:
        """A later tick reads the head back from disk and must reach it through its own backend."""
        run = HeadRun(
            run_id="run-1",
            spec=HeadSpec(profile_id="head", adapter="claude", runtime=LOCAL_PTY_RUNTIME),
            workspace="/tmp/ws",
            task_ref=TaskRef.card("card-1"),
            role="worker",
        )

        recovered = HeadRun.from_json(run.to_json())

        self.assertEqual(recovered.spec.runtime, LOCAL_PTY_RUNTIME)

    def test_recording_the_runtime_left_every_head_s_launch_identity_where_it_was(self) -> None:
        """Why it is written beside the spec block and not inside it.

        The spec block is hashed whole as a head's launch identity in two places — the worker
        lifecycle's `head_run_binding` and the Codex provider source's own fingerprint. A value
        added inside it changes the identity of every head already running, which at the next
        upgrade turns every persisted provider source into a foreign one and relaunches the heads
        reading them. So the two fingerprints have to be indifferent to it, and this is that.
        """
        from ummanu.dispatch.worker_lifecycle import head_run_binding
        from ummanu.runtime import codex_preflight

        def run_on(runtime: str) -> HeadRun:
            return HeadRun(
                run_id="run-1",
                spec=HeadSpec(profile_id="head", adapter="codex", codex_mode="tui", runtime=runtime),
                workspace="/tmp/ws",
                task_ref=TaskRef.card("card-1"),
                role="worker",
            )

        legacy, supervised = run_on(ORCA_LEGACY_RUNTIME), run_on(LOCAL_PTY_RUNTIME)

        self.assertNotIn("runtime", legacy.to_json()["spec"])
        self.assertEqual(head_run_binding(legacy.to_json()), head_run_binding(supervised.to_json()))
        self.assertEqual(
            codex_preflight.codex_provider_source_descriptor(legacy)["head_run_fingerprint"],
            codex_preflight.codex_provider_source_descriptor(supervised)["head_run_fingerprint"],
        )

    def test_a_record_written_before_this_key_existed_is_a_legacy_head(self) -> None:
        payload = {"profile_id": "head", "adapter": "codex"}

        recovered = HeadRun.from_json(
            {
                "run_id": "run-1",
                "spec": payload,
                "workspace": "/tmp/ws",
                "task_ref": {"kind": "card", "ref": "card-1"},
                "role": "worker",
            }
        )

        self.assertEqual(recovered.spec.runtime, ORCA_LEGACY_RUNTIME)

    def test_the_dispatcher_builds_the_backend_the_head_named_and_no_other(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        host = CommandHostRuntime(FakeCatalog(), Path(tmp.name), mode="noop")

        legacy = HeadSpec(profile_id="head", adapter="claude")
        supervised = HeadSpec(profile_id="head", adapter="claude", runtime=LOCAL_PTY_RUNTIME)

        # secretary-1722: a legacy record is given no backend, and never the supervised one.
        for subject in (legacy, ORCA_LEGACY_RUNTIME, None):
            with self.subTest(subject=subject), self.assertRaisesRegex(HostError, "legacy Orca record"):
                host.head_runtime_for(subject)
        self.assertIsInstance(host.head_runtime_for(supervised), LocalPtyHeadRuntime)
        self.assertIs(
            host.head_runtime_for(supervised),
            host.head_runtime_for(supervised),
            "a rebuilt runtime would forget the turns it handed out",
        )

    def test_a_runtime_no_validated_registry_could_produce_fails_closed(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        host = CommandHostRuntime(FakeCatalog(), Path(tmp.name), mode="noop")

        with self.assertRaisesRegex(HostError, "unknown head runtime"):
            host.head_runtime_for(HeadSpec(profile_id="head", adapter="claude", runtime="podman"))

    # -- criterion 5: it survives publication ---------------------------------------------------

    def test_the_value_survives_the_snapshot_a_live_tick_reads(self) -> None:
        """Materialized, not just parsed: `installed_heads` is what the dispatcher runs off."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        product = Path(tmp.name) / "product"
        canon = product / "src" / "ummanu" / "runtime"
        canon.mkdir(parents=True)
        (canon / "heads.toml").write_text(
            '[resources.acct]\naccount = "acct"\n\n'
            '[profiles.supervised]\nresource = "acct"\nadapter = "claude"\n'
            'runtime = "local-pty"\n\n'
            '[profiles.legacy]\nresource = "acct"\nadapter = "claude"\n\n'
            '[role_defaults]\nnew_card = "supervised"\n',
            encoding="utf-8",
        )
        instance = Path(tmp.name) / "instance"
        (instance / "heads").mkdir(parents=True)
        (instance / "instance.yaml").write_text(
            f"version: 1\nname: runtimes\ndata_dir: {instance / 'data'}\n"
            "offsite:\n  instance_remote: git@example.invalid:x/y.git\n",
            encoding="utf-8",
        )

        materialize_snapshot(instance, product)
        record_source(instance, product)
        installed = installed_heads(instance)

        self.assertEqual(installed["profiles"]["supervised"].get("runtime"), LOCAL_PTY_RUNTIME)
        self.assertNotIn("runtime", installed["profiles"]["legacy"])
        published = heads.load_registry(generated_pair(instance).snapshot)
        self.assertEqual(
            HeadSpec.from_profile("supervised", published.profile("supervised")).runtime,
            LOCAL_PTY_RUNTIME,
        )

    # -- criterion 6: nothing the product ships moves ------------------------------------------

    def test_every_profile_the_product_ships_is_on_the_one_backend(self) -> None:
        canon = canonical_heads(upgrade.running_product_root())

        for pid, profile in canon["profiles"].items():
            with self.subTest(profile=pid):
                self.assertEqual(profile.get("runtime", DEFAULT_HEAD_RUNTIME), LOCAL_PTY_RUNTIME)


class _RecordingBackend:
    """A head runtime that answers the verb workspace cleanup uses and remembers the asking."""

    def __init__(self, name: str, *, refuses: bool = False, raises: bool = False) -> None:
        self.name = name
        self.refuses = refuses
        self.raises = raises
        self.calls: list[tuple[str, str]] = []

    def stop(self, run: HeadRun, initiator, **ignored) -> StopReceipt:
        del ignored
        self.calls.append(("stop", run.run_id))
        if self.raises:
            raise HostError("the supervisor is unreachable")
        if self.refuses:
            return StopReceipt(status=HEAD_ALIVE, run=run, reason="the head's process outlived the stop")
        return StopReceipt(status=HEAD_OK, run=run.finishing(initiator).exited())


class WorkspaceCleanupChoosesTheBackendTheHeadIsHeldByTests(unittest.TestCase):
    """secretary-1467: workspace-scoped cleanup selects from the run, like every per-head verb.

    Per-head operations already resolved the backend from the head they act on. Workspace cleanup
    did not: it made one unconditional Orca by-worktree call, which addresses no supervised head at
    all, and `stop` absorbed the refusal so the caller went on to remove the worktree with the head
    still running — after which the card's next attempt raises a second head beside the first.

    Every site that picks a backend here picks it from the durable run the record names:
    `stop_workspace` from `worker_head_run` and `review_head_run`, `_stop_observer_terminals` from
    the observer record's own `head_run`. A workspace that names no run has no head to stop, and a
    record carrying a legacy run is refused before any head is stopped (secretary-1722).
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        env = mock.patch.dict(os.environ, {"UMMANU_DISPATCHER_WORKSPACES_ROOT": str(self.root / "orca")})
        env.start()
        self.addCleanup(env.stop)
        self.host = CommandHostRuntime(  # type: ignore[arg-type]
            FakeCatalog(), self.root / "data", mode="real", production_runtime=registered_production_runtime(self.root)
        )
        self.supervised = _RecordingBackend(LOCAL_PTY_RUNTIME)
        self.host._head_runtimes[LOCAL_PTY_RUNTIME] = self.supervised
        self.workspace = self.root / "data" / "workspaces" / "ummanu" / "ummanu-1467-worker"
        self.removed: list[str] = []
        removal = mock.patch.object(GitWorkspaceManager, "teardown", side_effect=self.removed.append)
        removal.start()
        self.addCleanup(removal.stop)

    def _run(self, role: str, runtime: str, run_id: str) -> dict:
        return HeadRun(
            run_id=run_id,
            spec=HeadSpec(profile_id="head", adapter="claude", runtime=runtime),
            workspace=str(self.workspace),
            task_ref=TaskRef.card("ummanu-1467"),
            role=role,
        ).to_json()

    def _record(self, **fields) -> DispatcherRecord:
        return DispatcherRecord(
            worker="ummanu-1467-worker",
            workspace=str(self.workspace),
            handle="term:1",
            head="claude",
            review_head="claude-reviewer",
            attempt_id="attempt-1",
            comment_baseline=0,
            review_baseline=0,
            state="claimed",
            claimed_at=0.0,
            **fields,
        )

    def test_a_supervised_worker_is_stopped_through_its_own_backend(self) -> None:
        record = self._record(worker_head_run=self._run("worker", LOCAL_PTY_RUNTIME, "run-w"))

        self.host.stop_workspace(record)

        self.assertEqual(self.supervised.calls, [("stop", "run-w")])

    def test_a_supervised_reviewer_is_stopped_through_its_own_backend(self) -> None:
        record = self._record(review_head_run=self._run("reviewer", LOCAL_PTY_RUNTIME, "run-r"))

        self.host.stop_workspace(record)

        self.assertEqual(self.supervised.calls, [("stop", "run-r")])

    def test_a_supervised_observer_is_stopped_through_its_own_backend(self) -> None:
        self.host._stop_observer_terminals(
            str(self.workspace),
            run=self._run("observer", LOCAL_PTY_RUNTIME, "run-o"),
            role="observer",
        )

        self.assertEqual(self.supervised.calls, [("stop", "run-o")])

    def test_a_record_carrying_any_legacy_run_is_refused_before_any_head_is_stopped(self) -> None:
        """One supervised, one legacy: nothing is stopped by run and nothing is torn down by worktree."""
        for worker, reviewer in (
            (LOCAL_PTY_RUNTIME, ORCA_LEGACY_RUNTIME),
            (ORCA_LEGACY_RUNTIME, ORCA_LEGACY_RUNTIME),
        ):
            with self.subTest(worker=worker, reviewer=reviewer):
                record = self._record(
                    worker_head_run=self._run("worker", worker, "run-w"),
                    review_head_run=self._run("reviewer", reviewer, "run-r"),
                )

                for verb in (self.host.stop_workspace, self.host.teardown):
                    with self.assertRaisesRegex(
                        LegacyDispatcherRecord, "head run run-(w|r) is a legacy record"
                    ):
                        verb(record)
                self.assertIsNone(self.host.stop(record), "the best-effort stop still absorbs it")

        self.assertEqual(self.supervised.calls, [])
        self.assertEqual(self.removed, [])

    def test_a_record_whose_runs_predate_the_runtime_key_loads_as_legacy_and_is_refused(self) -> None:
        """secretary-1718: the profile default moved to `local-pty`, and a durable record did not.

        Today's record shape, with the `head_runtime` its runs carry stripped out — which is what
        every record written before the key existed looks like — reads back as Orca heads, and is
        refused rather than stopped through a backend it never lived on (secretary-1722).
        """
        current = self._record(
            worker_head_run=self._run("worker", LOCAL_PTY_RUNTIME, "run-w"),
            review_head_run=self._run("reviewer", LOCAL_PTY_RUNTIME, "run-r"),
        ).to_json()
        for role in ("worker_head_run", "review_head_run"):
            self.assertIn("head_runtime", current[role], "the fixture is not today's record shape")
            del current[role]["head_runtime"]

        record = DispatcherRecord.from_json(json.loads(json.dumps(current)))

        for persisted in (record.worker_head_run, record.review_head_run):
            self.assertIsNotNone(persisted.run)
            self.assertEqual(persisted.run.spec.runtime, ORCA_LEGACY_RUNTIME)
        with self.assertRaises(LegacyDispatcherRecord):
            self.host.stop_workspace(record)
        self.assertEqual(self.supervised.calls, [])

    def test_a_record_in_an_orca_worktree_is_refused_even_with_supervised_runs(self) -> None:
        orca = self.root / "orca" / "sample_orca" / "ummanu-1467-worker"
        record = self._record(worker_head_run=self._run("worker", LOCAL_PTY_RUNTIME, "run-w"))
        record.workspace = str(orca)

        for verb in (self.host.stop_workspace, self.host.teardown):
            with self.assertRaisesRegex(LegacyDispatcherRecord, "is an Orca worktree"):
                verb(record)
        self.assertEqual(self.supervised.calls, [])
        self.assertEqual(self.removed, [])

    def test_a_workspace_that_names_no_run_has_no_head_to_stop(self) -> None:
        """A bring-up that never got as far as a durable run raised nothing to stop."""
        self.host.stop_workspace(self._record())

        self.assertEqual(self.supervised.calls, [])

    def test_a_head_that_would_not_stop_reaches_the_caller(self) -> None:
        self.supervised.refuses = True
        record = self._record(worker_head_run=self._run("worker", LOCAL_PTY_RUNTIME, "run-w"))

        with self.assertRaisesRegex(HostError, "was not stopped"):
            self.host.stop_workspace(record)

    def test_a_teardown_does_not_remove_the_worktree_under_a_head_it_could_not_stop(self) -> None:
        """The defect this card's own wiring opened: `stop` absorbs a refusal, and a removal made
        on the strength of an absorbed refusal orphans a live head."""
        self.supervised.refuses = True
        record = self._record(worker_head_run=self._run("worker", LOCAL_PTY_RUNTIME, "run-w"))

        with self.assertRaisesRegex(HostError, "no durable cleanup owner"):
            self.host.teardown(record)

        self.assertEqual(self.removed, [], "the worktree was pulled out from under a live head")

    def test_confirmed_heads_alone_do_not_authorize_path_only_teardown(self) -> None:
        record = self._record(worker_head_run=self._run("worker", LOCAL_PTY_RUNTIME, "run-w"))

        with self.assertRaisesRegex(HostError, "no durable cleanup owner"):
            self.host.teardown(record)
        self.assertEqual(self.removed, [])


if __name__ == "__main__":
    unittest.main()
