"""Tests for the upgrade materializer: packaged units, reconcile apply, role worktrees."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import tempfile
import unittest
from dataclasses import fields, replace
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest import mock

from tests.fakes.upgrade import FakeUnitInstaller
from tests.retired_board import STALE_FILE, legacy_runtime_lines, write_stale_leftovers
from ummanu import _proc, installation, state_repo, status, upgrade
from ummanu.board import provision as board_provision
from ummanu.config import DataDirError
from ummanu.head_health import HeadReadiness, resolve_head_chain
from ummanu.head_registry import (
    INSTANCE_ORIGIN,
    PRODUCT_ORIGIN,
    HeadRegistryConfigError,
    assert_snapshot_current,
    canonical_heads,
    canonical_path,
    generated_pair,
    installed_heads,
    load_snapshot,
    product_revision,
    read_source,
)
from ummanu.host import (
    CollectResult,
    HostInventory,
    PlannedResource,
    SystemdLayout,
    build_plan,
    component_enabled,
    load_managed_manifest,
    load_packaged_units,
    manifest_text,
    plan_changes,
    strict_manifest,
)
from ummanu.host_apply import ApplyInputs, apply_host
from ummanu.projects.availability import ProjectAvailability
from ummanu.runtime import heads

UNIT_PREFIX = "ummanu-"

TIMER = """[Unit]
Description=Example timer

[Timer]
OnCalendar=hourly
Unit=ummanu-example.service

[Install]
WantedBy=timers.target
"""

SERVICE = """[Unit]
Description=Example service

[Service]
Type=oneshot
ExecStart=/bin/true
"""


def _resolve_with_red(preferred, red, registry):
    """The head `resolve_head_chain` picks for `preferred` while resource `red` is not launchable."""

    def readiness(pid):
        resource = registry.profile(pid)["resource"]
        return HeadReadiness(resource, "unavailable" if resource == red else "ready", "", 0.0)

    def fallback(pid):
        try:
            return list(registry.profile(pid).get("fallback") or [])
        except heads.HeadRegistryError:
            return None

    return resolve_head_chain(preferred, readiness, fallback).head or None


def write_packaging(root: Path) -> Path:
    packaging = root / "packaging" / "systemd"
    packaging.mkdir(parents=True)
    (packaging / "ummanu-example.service").write_text(SERVICE, encoding="utf-8")
    (packaging / "ummanu-example.timer").write_text(TIMER, encoding="utf-8")
    (packaging / "ummanu-memory.service").write_text(SERVICE, encoding="utf-8")
    # The dispatcher pair is always in the desired plan, so a fixture that omits
    # it would be testing an installation the product cannot actually ship.
    (packaging / "ummanu-dispatcher-production.service").write_text(SERVICE, encoding="utf-8")
    (packaging / "ummanu-dispatcher-production.timer").write_text(TIMER, encoding="utf-8")
    (packaging / "README.md").write_text("not a unit\n", encoding="utf-8")
    return packaging


def instance_config(data_dir: Path, **host: object) -> dict:
    return {
        "version": 1,
        "name": "test",
        "data_dir": str(data_dir),
        "offsite": {"instance_remote": "git@example.invalid:x/y"},
        "host": {"unit_prefix": UNIT_PREFIX, **host},
    }


class PackagedUnitTests(unittest.TestCase):
    def test_orca_runtime_is_not_rendered_or_owned_by_an_upgrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "operator" / ".local" / "bin" / "orca"
            legacy.parent.mkdir(parents=True)
            legacy.write_text("#!/bin/sh\n", encoding="utf-8")
            legacy.chmod(0o755)
            account = SimpleNamespace(pw_dir=str(root / "operator"))
            with mock.patch("ummanu.host_apply.pwd.getpwnam", return_value=account):
                units = upgrade.resolve_packaged(
                    instance_config(root / "data"),
                    instance_path=root / "instance",
                    data_dir=root / "data",
                    runtime_user="operator",
                )

        self.assertNotIn("ummanu-orca.service", {unit.name for unit in units})

    def test_render_is_stable_and_uses_the_installation_layout(self):
        layout = SystemdLayout(
            Path("/opt/ummanu"),
            Path("/srv/secretary-instance"),
            Path("/srv/ummanu-data"),
            "operator",
            Path("/home/operator"),
        )
        first = load_packaged_units(
            upgrade.running_product_root() / "packaging" / "systemd", UNIT_PREFIX, layout
        )
        second = load_packaged_units(
            upgrade.running_product_root() / "packaging" / "systemd", UNIT_PREFIX, layout
        )

        self.assertEqual(
            [(unit.name, unit.content, unit.digest) for unit in first],
            [(unit.name, unit.content, unit.digest) for unit in second],
        )
        rendered = b"\n".join(unit.content for unit in first)
        self.assertIn(b"User=operator", rendered)
        self.assertIn(b"/opt/ummanu", rendered)
        self.assertIn(b"/srv/secretary-instance", rendered)
        self.assertIn(b"/srv/ummanu-data", rendered)
        self.assertNotIn(b"/home/dev", rendered)

    def test_catalogue_reads_component_digest_and_installability(self):
        with tempfile.TemporaryDirectory() as tmp:
            packaging = write_packaging(Path(tmp))
            units = {unit.name: unit for unit in load_packaged_units(packaging, UNIT_PREFIX)}

        self.assertEqual(
            sorted(units),
            [
                "ummanu-dispatcher-production.service",
                "ummanu-dispatcher-production.timer",
                "ummanu-example.service",
                "ummanu-example.timer",
                "ummanu-memory.service",
            ],
        )
        self.assertEqual(units["ummanu-example.timer"].component, "example")
        self.assertTrue(units["ummanu-example.timer"].installable)
        # No [Install] section, so enabling it would fail: it is pulled in by the timer.
        self.assertFalse(units["ummanu-example.service"].installable)
        self.assertNotEqual(units["ummanu-example.timer"].digest, units["ummanu-example.service"].digest)

    def test_a_unit_outside_our_prefix_is_not_ours(self):
        with tempfile.TemporaryDirectory() as tmp:
            packaging = write_packaging(Path(tmp))
            (packaging / "other-thing.timer").write_text(TIMER, encoding="utf-8")
            names = {unit.name for unit in load_packaged_units(packaging, UNIT_PREFIX)}
        self.assertNotIn("other-thing.timer", names)

    def test_component_is_enabled_unless_the_instance_opts_out(self):
        self.assertTrue(component_enabled({}, "curator"))
        self.assertTrue(component_enabled({"components": {"curator": {"reason": "note"}}}, "curator"))
        self.assertFalse(component_enabled({"components": {"curator": {"enabled": False}}}, "curator"))

    def test_disabled_component_leaves_the_desired_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            packaged = load_packaged_units(write_packaging(Path(tmp)), UNIT_PREFIX)
            instance = instance_config(Path(tmp), components={"example": {"enabled": False}})
            names = {r.name for r in build_plan(instance, [], packaged=packaged)}
        self.assertNotIn("ummanu-example.timer", names)
        self.assertIn("ummanu-memory.service", names)

    def test_editing_a_shipped_unit_makes_the_resource_an_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            packaging = write_packaging(Path(tmp))
            instance = instance_config(Path(tmp))
            before = build_plan(instance, [], packaged=load_packaged_units(packaging, UNIT_PREFIX))
            (packaging / "ummanu-example.timer").write_text(
                TIMER.replace("hourly", "daily"), encoding="utf-8"
            )
            after = build_plan(instance, [], packaged=load_packaged_units(packaging, UNIT_PREFIX))
            actual = HostInventory(units={r.name for r in before})
            changes = {c.name: c.action for c in plan_changes(after, actual, before, UNIT_PREFIX)}

        self.assertEqual(changes["ummanu-example.timer"], "update")
        self.assertEqual(changes["ummanu-example.service"], "unchanged")

    def test_dispatcher_units_carry_the_shipped_file_digest(self):
        packaged = load_packaged_units(upgrade.running_product_root() / "packaging" / "systemd", UNIT_PREFIX)
        by_id = {r.logical_id: r for r in build_plan(instance_config(Path("/tmp")), [], packaged=packaged)}
        spec = json.loads(by_id["systemd:dispatcher:production.service"].spec)
        self.assertIn("digest", spec)
        self.assertIn("production-tick", spec["runtime"])

    def test_declared_foreign_unit_is_not_a_conflict(self):
        actual = HostInventory(units={"ummanu-supervisor.timer"})
        conflicts = [c for c in plan_changes([], actual, [], UNIT_PREFIX) if c.action == "conflict"]
        self.assertEqual([c.name for c in conflicts], ["ummanu-supervisor.timer"])

        declared = plan_changes([], actual, [], UNIT_PREFIX, {"ummanu-supervisor.timer"})
        self.assertEqual([c for c in declared if c.action == "conflict"], [])


class ApplyHostTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.packaging = write_packaging(self.root)
        self.data = self.root / "data"
        self.data.mkdir()
        self.manifest = self.data / "host-managed.json"
        self.packaged = load_packaged_units(self.packaging, UNIT_PREFIX)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def inputs(
        self,
        inventory: HostInventory,
        managed=(),
        instance=None,
        bindings=(),
        runtime_user: str | None = None,
    ) -> ApplyInputs:
        return ApplyInputs(
            instance=instance or instance_config(self.data),
            bindings=list(bindings),
            inventory=inventory,
            managed=list(managed),
            manifest_path=self.manifest,
            packaged=self.packaged,
            runtime_user=runtime_user,
        )

    def test_empty_host_is_installed_enabled_and_recorded(self):
        units = FakeUnitInstaller()

        result = apply_host(self.inputs(HostInventory()), units=units)

        self.assertTrue(result.ok, result.errors)
        self.assertIn(("install", "ummanu-example.timer"), units.calls)
        self.assertIn(("enable", "ummanu-example.timer"), units.calls)
        # The service has no [Install]; enabling it would fail, so we never try.
        self.assertNotIn(("enable", "ummanu-example.service"), units.calls)
        self.assertIn(("daemon-reload", ""), units.calls)
        recorded = {r.name for r in strict_manifest(self.manifest)[0]}
        self.assertIn("ummanu-example.timer", recorded)

    def test_root_published_manifest_is_private_to_the_installation_user(self):
        account = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())
        with (
            mock.patch("ummanu.host_apply.os.geteuid", return_value=0),
            mock.patch("ummanu.host_apply.pwd.getpwnam", return_value=account),
        ):
            result = apply_host(
                self.inputs(HostInventory(), runtime_user="operator"),
                units=FakeUnitInstaller(),
            )

        self.assertTrue(result.ok, result.errors)
        self.assertTrue(
            load_managed_manifest(self.manifest)[0], "installation user can read the published manifest"
        )
        info = self.manifest.stat()
        self.assertEqual((info.st_uid, info.st_gid), (account.pw_uid, account.pw_gid))
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)

    def test_unprivileged_reconcile_never_attempts_manifest_ownership_repair(self):
        with (
            mock.patch("ummanu.host_apply.os.geteuid", return_value=1000),
            mock.patch("ummanu.host_apply.pwd.getpwnam") as account,
            mock.patch("ummanu.host_apply.os.chown") as chown,
            mock.patch("ummanu.host_apply.os.chmod") as chmod,
        ):
            result = apply_host(
                self.inputs(HostInventory(), runtime_user="operator"),
                units=FakeUnitInstaller(),
            )

        self.assertTrue(result.ok, result.errors)
        account.assert_not_called()
        chown.assert_not_called()
        chmod.assert_not_called()

    def test_root_repair_hands_an_unchanged_manifest_to_the_installation_user(self):
        desired = build_plan(instance_config(self.data), [], packaged=self.packaged)
        self.manifest.write_text(manifest_text(desired), encoding="utf-8")
        account = SimpleNamespace(pw_uid=1234, pw_gid=5678)
        inventory = HostInventory(
            units={resource.name for resource in desired if resource.kind == "unit"},
            unit_states={unit.name: ("enabled", "active") for unit in self.packaged if unit.installable},
        )
        with (
            mock.patch("ummanu.host_apply.os.geteuid", return_value=0),
            mock.patch("ummanu.host_apply.pwd.getpwnam", return_value=account),
            mock.patch("ummanu.host_apply.os.chown") as chown,
            mock.patch("ummanu.host_apply.os.chmod") as chmod,
        ):
            result = apply_host(
                self.inputs(inventory, managed=desired, runtime_user="operator"),
                units=FakeUnitInstaller(),
            )

        self.assertTrue(result.ok, result.errors)
        self.assertFalse(result.applied)
        chown.assert_called_once_with(self.manifest, 1234, 5678, follow_symlinks=False)
        chmod.assert_called_once_with(self.manifest, 0o600, follow_symlinks=False)

    def test_manifest_write_refuses_untrusted_existing_state(self):
        self.manifest.write_text("not json", encoding="utf-8")
        before = self.manifest.read_bytes()

        result = apply_host(self.inputs(HostInventory()), units=FakeUnitInstaller())

        self.assertFalse(result.ok)
        self.assertIn("managed manifest is not valid JSON", result.errors)
        self.assertEqual(self.manifest.read_bytes(), before)

    def test_second_run_against_the_reconciled_host_changes_nothing(self):
        units = FakeUnitInstaller()
        apply_host(self.inputs(HostInventory()), units=units)
        managed, error = strict_manifest(self.manifest)
        self.assertEqual(error, "")
        installed = HostInventory(units=set(units.files), unit_states=units.unit_states())
        units.calls.clear()

        result = apply_host(self.inputs(installed, managed), units=units)

        self.assertTrue(result.ok)
        self.assertFalse(result.changed)
        self.assertEqual(units.calls, [])
        self.assertEqual({c.action for c in result.changes}, {"unchanged"})

    def test_an_unowned_name_in_our_namespace_aborts_before_any_write(self):
        units = FakeUnitInstaller(present={"ummanu-example.timer": b"hand written"})
        inventory = HostInventory(units={"ummanu-example.timer"})

        result = apply_host(self.inputs(inventory), units=units)

        self.assertFalse(result.ok)
        self.assertEqual([c.name for c in result.conflicts], ["ummanu-example.timer"])
        self.assertEqual(units.calls, [])
        self.assertFalse(self.manifest.exists())
        self.assertEqual(units.files["ummanu-example.timer"], b"hand written")

    def test_a_conflict_anywhere_stops_the_units_that_would_have_been_fine(self):
        units = FakeUnitInstaller()
        inventory = HostInventory(units={"ummanu-legacy.timer"})

        result = apply_host(self.inputs(inventory), units=units)

        self.assertFalse(result.ok)
        self.assertEqual(units.calls, [])

    def test_dry_run_reports_the_same_changes_and_writes_nothing(self):
        units = FakeUnitInstaller()

        preview = apply_host(self.inputs(HostInventory()), units=units, dry_run=True)

        self.assertTrue(preview.ok)
        self.assertEqual({c.action for c in preview.changes}, {"create"})
        self.assertEqual(units.calls, [])
        self.assertFalse(self.manifest.exists())

    def test_a_dropped_component_is_disabled_removed_and_forgotten(self):
        units = FakeUnitInstaller()
        apply_host(self.inputs(HostInventory()), units=units)
        managed, _ = strict_manifest(self.manifest)
        installed = HostInventory(units=set(units.files), unit_states=units.unit_states())
        units.calls.clear()
        shed = instance_config(self.data, components={"example": {"enabled": False}})

        result = apply_host(self.inputs(installed, managed, instance=shed), units=units)

        self.assertTrue(result.ok, result.errors)
        self.assertIn(("disable", "ummanu-example.timer"), units.calls)
        self.assertIn(("remove", "ummanu-example.timer"), units.calls)
        # The service was never enabled (no [Install]), so disabling it would fail.
        self.assertNotIn(("disable", "ummanu-example.service"), units.calls)
        self.assertIn(("remove", "ummanu-example.service"), units.calls)
        recorded = {r.name for r in strict_manifest(self.manifest)[0]}
        self.assertNotIn("ummanu-example.timer", recorded)

    def test_a_failed_install_is_never_recorded_as_managed(self):
        units = FakeUnitInstaller()
        units.fail_on = {"ummanu-example.timer"}

        result = apply_host(self.inputs(HostInventory()), units=units)

        self.assertFalse(result.ok)
        recorded = {r.name for r in strict_manifest(self.manifest)[0]}
        self.assertNotIn("ummanu-example.timer", recorded)

    def test_a_binding_with_a_legacy_orca_binding_registers_nothing(self):
        units = FakeUnitInstaller()
        binding = {"id": "demo", "repo": "/srv/demo", "orca_binding": "demo", "enabled": True}

        with mock.patch("ummanu._proc.run") as run:
            result = apply_host(self.inputs(HostInventory(), bindings=[binding]), units=units)

        self.assertTrue(result.ok, result.errors)
        run.assert_not_called()
        self.assertFalse([change for change in result.changes if change.kind != "unit"])
        recorded = {resource.kind for resource in strict_manifest(self.manifest)[0]}
        self.assertEqual(recorded, {"unit"})

    def test_a_legacy_managed_orca_record_is_kept_and_never_deleted(self):
        """A registration an older reconcile recorded is Orca's own state: left alone, still recorded."""
        units = FakeUnitInstaller()
        apply_host(self.inputs(HostInventory()), units=units)
        managed, _ = strict_manifest(self.manifest)
        spec = '{"binding":"demo","repo":"/srv/demo"}'
        value = json.dumps(["orca:project:demo", "orca", "demo", spec], separators=(",", ":"))
        legacy = PlannedResource(
            "orca:project:demo", "orca", "demo", spec, hashlib.sha256(value.encode()).hexdigest()
        )
        self.manifest.write_text(manifest_text([*managed, legacy]), encoding="utf-8")
        binding = {"id": "demo", "repo": "/srv/demo", "orca_binding": "demo", "enabled": True}

        result = apply_host(
            self.inputs(
                HostInventory(units=set(units.files), unit_states=units.unit_states()),
                [*managed, legacy],
                bindings=[binding],
            ),
            units=units,
        )

        self.assertTrue(result.ok, result.errors)
        self.assertFalse([change for change in result.changes if change.kind == "orca"])
        self.assertIn(legacy, strict_manifest(self.manifest)[0])


class AgentSpecsTests(unittest.TestCase):
    def test_the_product_manifest_locates_the_shipped_role_worktrees_unchanged(self):
        # secretary-1689: the specs are found through `[tool.ummanu] agent-specs` instead of a
        # package name written into `ummanu`. What they materialize must not move by a byte.
        product = upgrade.running_product_root()
        home = Path("/home/owner")
        workspaces = home / "orca" / "workspaces" / "ummanu"
        with mock.patch.dict(os.environ):
            os.environ.pop("TA_WORKSPACES_ROOT", None)
            worktrees = upgrade.desired_role_worktrees(product, home)
        self.assertEqual(
            worktrees, [workspaces / name for name in ("curator", "pipeline", "retro", "steward")]
        )

    def test_the_product_manifest_decides_whether_any_specs_ship(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            product = Path(tmpdir)
            agent = product / "agents" / "curator"
            agent.mkdir(parents=True)
            (agent / "automation.toml").write_text('name = "curator"\nskill = "curate"\n', encoding="utf-8")
            # No manifest, or a manifest that declares none: the product ships no agents.
            self.assertIsNone(upgrade.agents_root(product))
            self.assertEqual(upgrade.desired_role_worktrees(product), [])
            manifest = product / "pyproject.toml"
            manifest.write_text("[project]\nname = 'x'\n", encoding="utf-8")
            self.assertIsNone(upgrade.agents_root(product))

            manifest.write_text('[tool.ummanu]\nagent-specs = "agents"\n', encoding="utf-8")
            self.assertEqual(upgrade.agents_root(product), product / "agents")

            for broken in (
                "[tool.ummanu\n",
                '[tool.ummanu]\nagent-specs = "../x"\n',
                '[tool.ummanu]\nagent-specs = "/abs"\n',
                "[tool.ummanu]\nagent-specs = 3\n",
            ):
                with self.subTest(broken=broken):
                    manifest.write_text(broken, encoding="utf-8")
                    with self.assertRaises(upgrade.AgentSpecsError):
                        upgrade.agents_root(product)
                    context = SimpleNamespace(product_root=product, runtime_home=None)
                    self.assertEqual(upgrade.step_worktrees(context).status, "failed")

    def test_the_upgrade_has_no_orca_automations_step(self):
        # secretary-1706: systemd units are the only schedule owner; upgrade neither creates nor
        # repoints nor deletes Orca automations.
        names = [step.__name__ for step in upgrade.STEPS]
        self.assertFalse([name for name in names if "automation" in name], names)
        self.assertNotIn("automations", {f.name for f in fields(upgrade.UpgradeContext)})


class UpgradeStepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.memory_probe = mock.patch("ummanu.upgrade.probe_memory").start()
        data = tempfile.TemporaryDirectory()
        self.addCleanup(data.cleanup)
        # The dependency and memory steps write their receipts under the data dir.
        self.data_dir = Path(data.name)

    def tearDown(self) -> None:
        self.memory_probe.stop()

    def registry_instance(self, root: Path) -> Path:
        """An instance whose `instance.yaml` names this test's data directory, where the pair goes."""
        (root / "instance.yaml").write_text(
            f"version: 1\nname: upgrade\ndata_dir: {self.data_dir}\n"
            "offsite:\n  instance_remote: git@example.invalid:x/y.git\n",
            encoding="utf-8",
        )
        return root

    def context(self, units: FakeUnitInstaller, **overrides) -> upgrade.UpgradeContext:
        report = _Report()
        report.data_dir = self.data_dir
        base = upgrade.UpgradeContext(
            instance_path=Path("/tmp/instance"),
            product_root=upgrade.running_product_root(),
            base_branch="main",
            dry_run=False,
            units=units,
            report=report,
        )
        return replace(base, **overrides)

    def test_memory_clients_are_materialized_for_the_runtime_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            product_root = Path(tmp)
            (product_root / ".venv").mkdir()
            context = self.context(
                FakeUnitInstaller(),
                product_root=product_root,
                runtime_home=Path("/home/operator"),
                report=SimpleNamespace(data_dir=Path("/srv/ummanu-data")),
            )
            outcome = SimpleNamespace(changed=3)
            with mock.patch("ummanu.upgrade.reconcile_clients", return_value=outcome) as reconcile:
                result = upgrade.step_memory_clients(context)

        self.assertEqual(result.status, "changed")
        reconcile.assert_called_once_with(
            context.product_root,
            Path("/home/operator"),
            Path("/srv/ummanu-data"),
            dry_run=False,
        )

    def test_memory_restarts_when_only_the_code_moved(self):
        units = FakeUnitInstaller(active={"ummanu-memory.service"})

        result = upgrade.step_memory(self.context(units, code_changed=True, runtime_user="memory-runtime"))

        self.assertEqual(result.status, "changed")
        self.assertIn("code or dependencies changed", result.detail)
        self.assertIn(("restart", "ummanu-memory.service"), units.calls)
        self.memory_probe.assert_called_once()
        self.assertEqual(self.memory_probe.call_args.kwargs["runtime_user"], "memory-runtime")

    def test_host_step_reports_a_configured_data_dir_resolution_error(self):
        report = SimpleNamespace(
            data_dir=Path("/tmp/data"),
            instance={"host": {"unit_prefix": UNIT_PREFIX}},
            bindings=[],
            host={"unit_prefix": UNIT_PREFIX},
        )
        with mock.patch(
            "ummanu.upgrade.resolve_packaged",
            side_effect=DataDirError("invalid instance data_dir"),
        ):
            result = upgrade.step_host(self.context(FakeUnitInstaller(), report=report))

        self.assertEqual(result.status, "failed")
        self.assertIn("invalid instance data_dir", result.detail)

    def test_host_step_leaves_a_legacy_orca_registration_alone(self):
        """An unavailable project's legacy Orca record is neither planned, deferred nor deleted."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "data"
            data_dir.mkdir()
            binding = {
                "id": "alpha",
                "repo": "/srv/recovered-alpha",
                "orca_binding": "alpha-repo",
                "enabled": True,
            }
            spec = '{"binding":"alpha-repo","repo":"/srv/alpha"}'
            value = json.dumps(["orca:project:alpha", "orca", "alpha-repo", spec], separators=(",", ":"))
            legacy = PlannedResource(
                "orca:project:alpha", "orca", "alpha-repo", spec, hashlib.sha256(value.encode()).hexdigest()
            )
            manifest = data_dir / "host-managed.json"
            manifest.write_text(manifest_text([legacy]), encoding="utf-8")
            report = SimpleNamespace(
                data_dir=data_dir,
                instance={},
                bindings=[binding],
                host={},
                instance_path=root / "instance" / "instance.yaml",
            )
            source = mock.Mock()
            source.collect.return_value = CollectResult(inventory=HostInventory())
            context = self.context(
                FakeUnitInstaller(),
                instance_path=report.instance_path.parent,
                report=report,
                project_availability=ProjectAvailability(frozenset({"alpha"})),
            )

            with (
                mock.patch("ummanu.upgrade.resolve_packaged", return_value=[]),
                mock.patch("ummanu.upgrade.LiveHostSource", return_value=source),
                mock.patch("ummanu._proc.run") as run,
            ):
                result = upgrade.step_host(context)

            self.assertEqual(result.status, "unchanged", result.detail)
            self.assertNotIn("deferred", result.detail)
            run.assert_not_called()
            self.assertEqual(strict_manifest(manifest)[0], [legacy])

    def test_no_upgrade_step_is_about_a_transport(self):
        names = [step.__name__ for step in upgrade.STEPS]
        self.assertFalse([name for name in names if "transport" in name], names)
        self.assertIn("step_runtime_owner", names)

    def test_runtime_owner_step_neither_reads_nor_reports_stale_transport_leftovers(self):
        """A stale transport file and legacy runtime.env lines are left exactly as found."""
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            subprocess.run(["git", "-C", str(instance), "init", "--quiet"], check=True)
            runtime = instance / "runtime.env"
            body = "OTHER=value\n" + legacy_runtime_lines()
            runtime.write_text(body, encoding="utf-8")
            runtime.chmod(0o600)
            stale = write_stale_leftovers(instance)
            stale.chmod(0o644)  # even a mode the old step refused is no business of this one
            stale_body = stale.read_bytes()
            real_open = Path.open

            def guarded_open(path, *args, **kwargs):
                if Path(path).name == STALE_FILE:
                    raise AssertionError("upgrade read the stale transport file")
                return real_open(path, *args, **kwargs)

            with mock.patch.object(Path, "open", guarded_open):
                result = upgrade.step_runtime_owner(self.context(FakeUnitInstaller(), instance_path=instance))
            self.assertEqual(result.status, "unchanged")
            self.assertNotIn("transport", result.detail)
            self.assertNotIn(STALE_FILE, result.detail)
            self.assertEqual(runtime.read_text(encoding="utf-8"), body)
            self.assertEqual(stale.read_bytes(), stale_body)
            self.assertEqual(stale.stat().st_mode & 0o777, 0o644)

    def test_runtime_owner_step_hands_runtime_files_to_the_runtime_user_and_skips_leftovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            subprocess.run(["git", "-C", str(instance), "init", "--quiet"], check=True)
            (instance / ".gitignore").write_text("runtime.env\n", encoding="utf-8")
            runtime = instance / "runtime.env"
            runtime.write_text("OTHER=value\n", encoding="utf-8")
            runtime.chmod(0o600)
            stale = write_stale_leftovers(instance)
            context = self.context(FakeUnitInstaller(), instance_path=instance, runtime_user="operator")
            account = SimpleNamespace(pw_uid=123, pw_gid=456)

            with (
                mock.patch("ummanu.upgrade.os.geteuid", return_value=0),
                mock.patch("ummanu.upgrade.pwd.getpwnam", return_value=account),
                mock.patch("ummanu.upgrade.os.chown") as chown,
            ):
                result = upgrade.step_runtime_owner(context)

            owned = {Path(call.args[0]) for call in chown.call_args_list}
            self.assertEqual(result.status, "unchanged")
            self.assertIn(runtime, owned)
            self.assertIn(instance / ".gitignore", owned)
            self.assertIn(instance / ".git", owned)
            self.assertNotIn(stale, owned)

    def test_runtime_owner_step_still_fails_on_an_unsafe_runtime_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            subprocess.run(["git", "-C", str(instance), "init", "--quiet"], check=True)
            runtime = instance / "runtime.env"
            runtime.write_text("OTHER=value\n", encoding="utf-8")
            runtime.chmod(0o644)
            insecure = upgrade.step_runtime_owner(self.context(FakeUnitInstaller(), instance_path=instance))
        self.assertEqual(insecure.status, "failed")
        self.assertIn("permissions are too broad", insecure.detail)

    def test_memory_restarts_when_its_unit_file_changed(self):
        units = FakeUnitInstaller(active={"ummanu-memory.service"})

        result = upgrade.step_memory(self.context(units, unit_changed=True))

        self.assertEqual(result.status, "changed")
        self.assertIn(("restart", "ummanu-memory.service"), units.calls)
        self.memory_probe.assert_called_once()

    def test_memory_is_left_alone_when_nothing_moved(self):
        units = FakeUnitInstaller(active={"ummanu-memory.service"})
        # Nothing moved since the restart that wrote the memory process receipt.
        self.assertEqual(upgrade.step_memory(self.context(units, code_changed=True)).status, "changed")
        units.calls.clear()

        result = upgrade.step_memory(self.context(units))

        self.assertEqual(result.status, "unchanged")
        self.assertIn("memory process receipt verified", result.detail)
        self.assertEqual(units.calls, [])

    def test_a_stopped_memory_service_is_started_even_with_no_change(self):
        units = FakeUnitInstaller()

        result = upgrade.step_memory(self.context(units))

        self.assertEqual(result.status, "changed")
        self.assertIn("not active", result.detail)
        self.memory_probe.assert_called_once()

    def test_memory_restart_is_failed_when_the_authenticated_probe_fails(self):
        units = FakeUnitInstaller(active={"ummanu-memory.service"})
        self.memory_probe.side_effect = upgrade.MemoryProbeError("MCP did not return an allowed read")

        result = upgrade.step_memory(self.context(units, code_changed=True))

        self.assertEqual(result.status, "failed")
        self.assertIn("authenticated probe failed", result.detail)
        self.assertIn(("restart", "ummanu-memory.service"), units.calls)

    def test_dry_run_decides_the_restart_without_performing_it(self):
        units = FakeUnitInstaller(active={"ummanu-memory.service"})

        result = upgrade.step_memory(self.context(units, code_changed=True, dry_run=True))

        self.assertEqual(result.status, "changed")
        self.assertEqual(units.calls, [])

    # secretary-756: the two scenarios formerly here (materializing a foreign
    # `ummanu-orca.service` before the ownership migration, and `step_host` failing over
    # an unavailable Orca executable before writing ownership) both depended on the product
    # shipping a `ummanu-orca.*` systemd unit. Orca is host-owned and external
    # (secretary-739/755): packaging/systemd ships no such unit, `resolve_packaged` no longer
    # raises over a missing Orca executable, and `step_host` can no longer materialize or
    # gate on one. Deleted rather than rewritten.

    def test_the_run_stops_at_the_first_failed_step(self):
        calls: list[str] = []

        def ok(context):
            calls.append("ok")
            return upgrade.StepResult("ok", "unchanged")

        def bad(context):
            calls.append("bad")
            return upgrade.StepResult("bad", "failed", "boom")

        result = upgrade.run_steps(self.context(FakeUnitInstaller()), steps=(ok, bad, ok))

        self.assertEqual(calls, ["ok", "bad"])
        self.assertFalse(result.ok)
        self.assertIn("failed", result.render())

    def test_compose_replacement_keeps_owner_mode_and_atomic_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "installation"
            directory.mkdir(mode=0o755)
            compose = directory / "postgres-compose.yml"
            compose.write_text(board_provision.LEGACY_COMPOSE_TEXT, encoding="utf-8")
            compose.chmod(0o600)
            before = compose.stat()
            self.assertTrue(
                board_provision._write_compose(compose, dry_run=False, privileged_argv=lambda argv: argv)
            )
            after = compose.stat()
            self.assertNotEqual(before.st_ino, after.st_ino)
            self.assertEqual((after.st_uid, after.st_gid), (before.st_uid, before.st_gid))
            self.assertEqual(stat.S_IMODE(after.st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o755)
            self.assertEqual(compose.read_text(encoding="utf-8"), board_provision.COMPOSE_TEXT)
            self.assertEqual(list(directory.iterdir()), [compose])

    def test_compose_export_permission_failure_preserves_old_definition(self):
        with tempfile.TemporaryDirectory() as temporary:
            compose = Path(temporary) / "postgres-compose.yml"
            compose.write_text(board_provision.LEGACY_COMPOSE_TEXT, encoding="utf-8")
            compose.chmod(0o600)
            with (
                mock.patch(
                    "ummanu._fsutil._proc.run",
                    side_effect=[subprocess.CompletedProcess([], 1), subprocess.CompletedProcess([], 0)],
                ) as run,
                self.assertRaises(board_provision.BoardStoreError) as raised,
            ):
                board_provision._write_compose(compose, dry_run=False, privileged_argv=lambda argv: argv)
            self.assertIn("stage private file", str(raised.exception))
            self.assertEqual(run.call_count, 2)  # failed install, then cleanup
            self.assertEqual(compose.read_text(encoding="utf-8"), board_provision.LEGACY_COMPOSE_TEXT)
            self.assertEqual(list(compose.parent.iterdir()), [compose])

    def test_compose_failed_rename_removes_staged_file_and_preserves_old_definition(self):
        from ummanu import _proc

        with tempfile.TemporaryDirectory() as temporary:
            compose = Path(temporary) / "postgres-compose.yml"
            compose.write_text(board_provision.LEGACY_COMPOSE_TEXT, encoding="utf-8")
            compose.chmod(0o600)
            real_run = _proc.run

            def fail_rename(argv, **kwargs):
                if argv[0] == "mv":
                    return subprocess.CompletedProcess(argv, 1)
                return real_run(argv, **kwargs)

            with (
                mock.patch("ummanu._fsutil._proc.run", side_effect=fail_rename),
                self.assertRaises(board_provision.BoardStoreError) as raised,
            ):
                board_provision._write_compose(compose, dry_run=False, privileged_argv=lambda argv: argv)
            self.assertIn("replace private file", str(raised.exception))
            self.assertEqual(compose.read_text(encoding="utf-8"), board_provision.LEGACY_COMPOSE_TEXT)
            self.assertEqual(list(compose.parent.iterdir()), [compose])

    def test_provision_exception_renders_failed_step_json_and_stops_before_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            instance = Path(temporary)
            report = SimpleNamespace(ok=True, instance_path=instance / "instance.yaml", data_dir=instance)
            args = SimpleNamespace(
                instance=str(instance),
                product_root=str(upgrade.running_product_root()),
                base_branch="main",
                dry_run=False,
                no_pull=True,
                runtime_user=None,
                host_fixture=None,
                json=True,
            )
            calls: list[str] = []

            def completed(_context):
                calls.append("completed")
                return upgrade.StepResult("dependencies", "changed", "installed")

            def restart(_context):
                calls.append("restart")
                return upgrade.StepResult("web", "changed", "restarted")

            original_run_steps = upgrade.run_steps
            import contextlib
            import io

            output = io.StringIO()
            with (
                mock.patch("ummanu.upgrade.validate_instance", return_value=report),
                mock.patch("ummanu.upgrade.resolve_runtime_owner", return_value=("operator", instance)),
                mock.patch(
                    "ummanu.upgrade.provision_board_store",
                    side_effect=RuntimeError("could not write export file: Permission denied"),
                ),
                mock.patch(
                    "ummanu.upgrade.run_steps",
                    side_effect=lambda context: original_run_steps(
                        context, steps=(completed, upgrade.step_board_store_provision, restart)
                    ),
                ),
                contextlib.redirect_stdout(output),
            ):
                code = upgrade.run_upgrade(args)
            payload = json.loads(output.getvalue())
            self.assertEqual(code, 1)
            self.assertEqual(payload["status"], "failed")
            self.assertEqual(
                [(step["name"], step["status"]) for step in payload["steps"]],
                [("dependencies", "changed"), ("board-store-provision", "failed")],
            )
            self.assertIn("Permission denied", payload["steps"][1]["detail"])
            self.assertNotIn("Traceback", output.getvalue())
            self.assertEqual(calls, ["completed"])

    def test_pulled_code_handoff_preserves_the_upgrade_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            python = root / ".venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("", encoding="utf-8")
            args = SimpleNamespace(
                instance="/srv/instance/instance.yaml",
                base_branch="stable",
                dry_run=False,
                runtime_user="operator",
                host_fixture="/tmp/host-fixture",
                json=True,
            )
            with mock.patch("ummanu.upgrade.os.execve") as execute:
                upgrade._exec_pulled_upgrade(
                    args,
                    root,
                    before="a" * 40,
                    after="b" * 40,
                    changed_paths=("pyproject.toml",),
                )

        executable, argv, environment = execute.call_args.args
        self.assertEqual(executable, python)
        self.assertEqual(argv[:5], [str(python), "-P", "-m", "ummanu", "upgrade"])
        self.assertIn("--no-pull", argv)
        self.assertIn("/srv/instance/instance.yaml", argv)
        self.assertIn("stable", argv)
        self.assertIn("operator", argv)
        self.assertIn("/tmp/host-fixture", argv)
        self.assertIn("--json", argv)
        self.assertEqual(
            json.loads(environment["UMMANU_UPGRADE_HANDOFF"]),
            {
                "before": "a" * 40,
                "after": "b" * 40,
                "changed_paths": ["pyproject.toml"],
            },
        )

    def test_handoff_pull_step_names_revisions_without_pulling_again(self) -> None:
        context = self.context(
            FakeUnitInstaller(),
            pull=False,
            handoff_before="a" * 40,
            handoff_after="b" * 40,
        )
        with mock.patch("ummanu.upgrade.fast_forward") as pull:
            result = upgrade.step_pull(context)

        pull.assert_not_called()
        self.assertEqual(result.status, "changed")
        self.assertIn("aaaaaaaaaaaa -> bbbbbbbbbbbb", result.detail)

    def test_handoff_process_verifies_exact_head_and_runs_the_current_schedule_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            product = root / "product"
            instance = root / "instance"
            product.mkdir()
            instance.mkdir()
            subprocess.run(["git", "init", "--quiet", product], check=True)
            subprocess.run(["git", "-C", product, "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", product, "config", "user.email", "test@example.invalid"], check=True)
            (product / "README").write_text("current\n", encoding="utf-8")
            subprocess.run(["git", "-C", product, "add", "README"], check=True)
            subprocess.run(["git", "-C", product, "commit", "--quiet", "-m", "current"], check=True)
            revision = subprocess.run(
                ["git", "-C", product, "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            report = SimpleNamespace(
                ok=True,
                instance_path=instance / "instance.yaml",
                data_dir=root / "data",
            )
            args = SimpleNamespace(
                instance=str(instance),
                product_root=str(product),
                base_branch="main",
                dry_run=False,
                no_pull=False,
                runtime_user=None,
                host_fixture=None,
                json=False,
            )
            captured = []

            def run_once(context):
                captured.append(context)
                return upgrade.UpgradeResult()

            marker = json.dumps({"before": "a" * 40, "after": revision, "changed_paths": ["pyproject.toml"]})
            with (
                mock.patch.dict(os.environ, {"UMMANU_UPGRADE_HANDOFF": marker}),
                mock.patch("ummanu.upgrade.validate_instance", return_value=report),
                mock.patch("ummanu.upgrade.resolve_runtime_owner", return_value=("operator", root)),
                mock.patch("ummanu.upgrade.run_steps", side_effect=run_once) as steps,
            ):
                self.assertEqual(upgrade.run_upgrade(args), 0)

        steps.assert_called_once()
        self.assertEqual(len(captured), 1)
        self.assertFalse(captured[0].pull)
        self.assertEqual(captured[0].changed_paths, ("pyproject.toml",))

    def test_dependency_provenance_refuses_imports_outside_the_selected_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            python = root / ".venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("", encoding="utf-8")
            evidence = {
                "prefix": str(root / ".venv"),
                "origins": {
                    "ummanu": "/another/checkout/ummanu/__init__.py",
                    "psycopg": str(root / ".venv/lib/python/site-packages/psycopg/__init__.py"),
                    "sqlalchemy": str(root / ".venv/lib/python/site-packages/sqlalchemy/__init__.py"),
                    "alembic": str(root / ".venv/lib/python/site-packages/alembic/__init__.py"),
                },
            }
            context = self.context(FakeUnitInstaller(), product_root=root)
            with mock.patch(
                "ummanu.upgrade._proc.run",
                return_value=subprocess.CompletedProcess([], 0, json.dumps(evidence), ""),
            ):
                result = upgrade.step_dependency_provenance(context)

        self.assertTrue(result.failed)
        self.assertIn("escaped", result.detail)

    def test_changed_pull_hands_off_before_any_import_bound_schedule_step(self) -> None:
        class HandedOff(Exception):
            pass

        report = SimpleNamespace(
            ok=True,
            instance_path=Path("/srv/instance/instance.yaml"),
            data_dir=Path("/srv/data"),
        )
        args = SimpleNamespace(
            instance="/srv/instance",
            product_root="/srv/product",
            base_branch="main",
            dry_run=False,
            no_pull=False,
            runtime_user=None,
            host_fixture=None,
            json=False,
        )

        def pulled(context):
            context.pulled_before = "a" * 40
            context.pulled_after = "b" * 40
            context.changed_paths = ("src/ummanu/new_step.py", "pyproject.toml")
            return upgrade.StepResult("pull", "changed", "aaaaaaaaaaaa -> bbbbbbbbbbbb")

        with (
            mock.patch.dict(os.environ, {"UMMANU_UPGRADE_HANDOFF": ""}),
            mock.patch("ummanu.upgrade.validate_instance", return_value=report),
            mock.patch("ummanu.upgrade.resolve_runtime_owner", return_value=("operator", Path("/srv"))),
            mock.patch("ummanu.upgrade.step_pull", side_effect=pulled),
            mock.patch("ummanu.upgrade._exec_pulled_upgrade", side_effect=HandedOff) as execute,
            mock.patch("ummanu.upgrade.run_steps") as steps,
            self.assertRaises(HandedOff),
        ):
            upgrade.run_upgrade(args)

        steps.assert_not_called()
        self.assertEqual(
            execute.call_args.kwargs["changed_paths"],
            ("src/ummanu/new_step.py", "pyproject.toml"),
        )

    def test_a_dependency_manifest_move_triggers_a_reinstall_decision(self):
        units = FakeUnitInstaller()
        context = self.context(units, changed_paths=("pyproject.toml",), dry_run=True)

        result = upgrade.step_dependencies(context)

        self.assertIn(result.status, {"changed", "skipped"})
        if result.status == "changed":
            self.assertIn("reinstall", result.detail)

    def test_a_code_only_move_leaves_dependencies_alone(self):
        context = self.context(FakeUnitInstaller(), changed_paths=("ummanu/cli.py",), dry_run=True)

        result = upgrade.step_dependencies(context)

        self.assertIn(result.status, {"unchanged", "skipped"})

    @staticmethod
    def _venv(root: Path, direct_url: dict | None, ruff_version: str | None = "0.16.4") -> Path:
        """A product checkout whose .venv holds the product installed the given way."""
        (root / ".venv" / "bin").mkdir(parents=True)
        (root / ".venv" / "bin" / "python").write_text("", encoding="utf-8")
        (root / "pyproject.toml").write_text(
            "[project]\n[project.optional-dependencies]\ndev = ['ruff==0.16.4']\n",
            encoding="utf-8",
        )
        if ruff_version is not None:
            ruff = root / ".venv" / "bin" / "ruff"
            ruff.write_text(f"#!/bin/sh\necho 'ruff {ruff_version}'\n", encoding="utf-8")
            ruff.chmod(0o755)
        dist_info = root / ".venv" / "lib" / "python3.12" / "site-packages" / "ummanu-0.1.0.dist-info"
        dist_info.mkdir(parents=True)
        if direct_url is not None:
            (dist_info / "direct_url.json").write_text(json.dumps(direct_url), encoding="utf-8")
        # The dependency receipt binds the tracked manifests, so the checkout is a Git one.
        (root / ".gitignore").write_text(".venv/\n", encoding="utf-8")
        _commit_all(root, "product")
        return root

    def test_an_editable_install_that_moved_no_manifest_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._venv(Path(tmp), {"url": "file:///product", "dir_info": {"editable": True}})
            context = self.context(FakeUnitInstaller(), product_root=root)
            upgrade._write_dependency_receipt(
                context,
                root / ".venv",
                upgrade._git_tracked_digest(root, upgrade.DEPENDENCY_PATHS),
                ("dev",),
            )

            result = upgrade.step_dependencies(replace(context, dry_run=True))

        self.assertEqual(result.status, "unchanged", result.detail)
        self.assertIn("venv matches checkout (deps sha256 ", result.detail)
        self.assertIn("extras dev)", result.detail)

    def test_an_editable_install_with_missing_pinned_ruff_is_repaired(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._venv(Path(tmp), {"url": "file:///product", "dir_info": {"editable": True}}, None)
            context = self.context(FakeUnitInstaller(), product_root=root, dry_run=True)

            result = upgrade.step_dependencies(context)

        self.assertEqual(result.status, "changed")
        self.assertIn("pinned Ruff 0.16.4 is missing", result.detail)

    def test_an_editable_install_with_the_wrong_ruff_version_is_repaired(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._venv(Path(tmp), {"url": "file:///product", "dir_info": {"editable": True}}, "0.15.0")
            context = self.context(FakeUnitInstaller(), product_root=root, dry_run=True)

            result = upgrade.step_dependencies(context)

        self.assertEqual(result.status, "changed")
        self.assertIn("not 0.16.4", result.detail)

    def test_an_editable_install_with_an_unrunnable_ruff_is_repaired(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._venv(Path(tmp), {"url": "file:///product", "dir_info": {"editable": True}})
            (root / ".venv" / "bin" / "ruff").chmod(0o644)
            context = self.context(FakeUnitInstaller(), product_root=root, dry_run=True)

            result = upgrade.step_dependencies(context)

        self.assertEqual(result.status, "changed")
        self.assertIn("cannot run", result.detail)

    def test_ruff_repair_installs_the_declared_dev_extra(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._venv(Path(tmp), {"url": "file:///product", "dir_info": {"editable": True}}, None)
            context = self.context(FakeUnitInstaller(), product_root=root)
            with mock.patch(
                "ummanu.upgrade._proc.run", return_value=subprocess.CompletedProcess([], 0)
            ) as run:
                result = upgrade.step_dependencies(context)

        self.assertEqual(result.status, "changed")
        self.assertEqual(
            run.call_args.args[0],
            [
                str(root / ".venv" / "bin" / "python"),
                "-m",
                "pip",
                "install",
                "--quiet",
                "-e",
                f"{root}[dev]",
            ],
        )

    def test_a_snapshot_install_is_reinstalled_even_with_no_manifest_move(self):
        """The 2026-08-05 outage: an upgrade step retired legacy runtime.env lines while this venv
        still held a copy of the previous day's reader, and every tick failed for 26h."""
        with tempfile.TemporaryDirectory() as tmp:
            root = self._venv(Path(tmp), {"url": "file:///product", "dir_info": {}})
            context = self.context(FakeUnitInstaller(), product_root=root, dry_run=True)

            result = upgrade.step_dependencies(context)

        self.assertEqual(result.status, "changed")
        self.assertIn("snapshot install", result.detail)

    def test_an_install_that_cannot_prove_it_is_editable_is_treated_as_a_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._venv(Path(tmp), None)
            context = self.context(FakeUnitInstaller(), product_root=root, dry_run=True)

            result = upgrade.step_dependencies(context)

        self.assertEqual(result.status, "changed")
        self.assertIn("snapshot install", result.detail)

    def test_head_registry_step_materializes_the_product_canon_idempotently(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            instance = self.registry_instance(Path(tmpdir))
            context = self.context(FakeUnitInstaller(), instance_path=instance)

            result = upgrade.step_head_registry(context)
            again = upgrade.step_head_registry(context)

            self.assertEqual(result.status, "changed")
            self.assertEqual(again.status, "unchanged")
            self.assertEqual(load_snapshot(instance), canonical_heads(context.product_root, instance))
            self.assertEqual(load_snapshot(instance)["role_defaults"]["new_card"], "claude-opus-medium")

    def test_head_registry_dry_run_reports_drift_without_writing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            instance = self.registry_instance(Path(tmpdir))
            context = self.context(FakeUnitInstaller(), instance_path=instance, dry_run=True)

            result = upgrade.step_head_registry(context)

            self.assertEqual(result.status, "changed")
            self.assertFalse(generated_pair(instance).snapshot.exists())
            self.assertFalse(generated_pair(instance).source.exists())
            self.assertFalse((instance / "heads").exists())

    def test_head_registry_step_pins_the_checkout_the_snapshot_came_from(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            instance = self.registry_instance(Path(tmpdir))
            context = self.context(FakeUnitInstaller(), instance_path=instance)

            upgrade.step_head_registry(context)

            pin = read_source(instance)
            self.assertEqual(pin["product_root"], str(context.product_root.resolve()))
            self.assertEqual(pin["revision"], product_revision(context.product_root))

    def test_root_materialization_hands_the_recovery_pair_to_the_runtime_user(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            instance = self.registry_instance(Path(tmpdir))
            context = self.context(FakeUnitInstaller(), instance_path=instance, runtime_user="operator")
            account = SimpleNamespace(pw_uid=123, pw_gid=456)

            with (
                mock.patch("ummanu.upgrade.os.geteuid", return_value=0),
                mock.patch("ummanu.upgrade.pwd.getpwnam", return_value=account),
                mock.patch("ummanu.upgrade.os.chown") as chown,
            ):
                result = upgrade.step_head_registry(context)

            owned = {Path(call.args[0]) for call in chown.call_args_list}
            self.assertEqual(result.status, "changed")
            self.assertIn(self.data_dir / "heads", owned)
            self.assertIn(self.data_dir / "heads" / "heads.yaml", owned)
            self.assertIn(self.data_dir / "heads" / "source.yaml", owned)

    def test_head_registry_step_repins_a_moved_checkout_without_snapshot_drift(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            instance = self.registry_instance(Path(tmpdir))
            context = self.context(FakeUnitInstaller(), instance_path=instance)
            upgrade.step_head_registry(context)
            snapshot_before = load_snapshot(instance)
            generated_pair(instance).source.write_text(
                "product_root: /somewhere/else\nrevision: deadbeef\n", encoding="utf-8"
            )

            result = upgrade.step_head_registry(context)

            self.assertEqual(result.status, "changed")
            self.assertIn("source.yaml", result.detail)
            self.assertEqual(load_snapshot(instance), snapshot_before)
            self.assertEqual(read_source(instance)["product_root"], str(context.product_root.resolve()))

    def test_upgrade_direct_config_path_renders_the_same_units_as_its_checkout(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            instance = root / "instance"
            instance.mkdir()
            data_dir = root / "data"
            data_dir.mkdir()
            config = instance / "instance.yaml"
            config.write_text(
                "version: 1\nname: upgrade\ndata_dir: "
                + str(data_dir)
                + "\noffsite:\n  instance_remote: git@example.invalid:x/y\nhost:\n  unit_prefix: ummanu-\n",
                encoding="utf-8",
            )
            account = SimpleNamespace(pw_dir="/srv/operator")
            rendered: list[dict[str, bytes]] = []

            def capture(context: upgrade.UpgradeContext) -> upgrade.UpgradeResult:
                packaged = upgrade.resolve_packaged(
                    context.report.instance,
                    context.product_root / "packaging" / "systemd",
                    product_root=context.product_root,
                    instance_path=context.instance_path,
                    data_dir=context.report.data_dir,
                    runtime_user="operator",
                )
                rendered.append({unit.name: unit.content for unit in packaged})
                return upgrade.UpgradeResult()

            with (
                mock.patch.object(upgrade, "run_steps", side_effect=capture),
                mock.patch("ummanu.host_apply.pwd.getpwnam", return_value=account),
            ):
                for value in (instance, config):
                    code = upgrade.run_upgrade(
                        SimpleNamespace(
                            instance=str(value),
                            # The question here is the instance spelling, so the checkout is named
                            # rather than defaulted: the default is a configured path or a home, and
                            # neither has to be a checkout with unit templates in it.
                            product_root=str(upgrade.running_product_root()),
                            base_branch="main",
                            dry_run=True,
                            no_pull=True,
                            host_fixture=None,
                            json=False,
                        )
                    )
                    self.assertEqual(code, 0)

            self.assertEqual(rendered[0], rendered[1])
            self.assertIn(str(instance).encode(), rendered[1]["ummanu-memory.service"])
            self.assertNotIn(str(config).encode(), rendered[1]["ummanu-memory.service"])

    def test_stale_head_snapshot_fails_the_upgrade_verify(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            instance = self.registry_instance(Path(tmpdir))
            stale = generated_pair(instance).snapshot
            stale.parent.mkdir()
            stale.write_text(
                "profiles:\n  codex:\n    model: gpt-5.5\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(HeadRegistryConfigError, "is stale"):
                assert_snapshot_current(instance, upgrade.running_product_root())

    def test_the_shipped_registry_runs_every_role_on_either_subscription(self):
        """The portable default has to bring a clean host up with only one account authed.

        Both directions: an OpenAI-only host runs the role defaults as written, and a Claude-only
        host reaches a green head for every one of them through the fallback chains.
        """
        canon = canonical_heads(upgrade.running_product_root())
        registry = heads.Registry(canon["resources"], canon["profiles"], canon["role_defaults"])
        roles = ("new_card", "reviewer", "observer", "curator", "retro", "steward")

        self.assertEqual(set(canon["resources"]), {"claude-sub", "openai-sub"})
        for role in roles:
            with self.subTest(role=role):
                preferred = registry.role_default(role)
                self.assertIsNotNone(preferred, f"{role} is routed nowhere")
                for red in ("claude-sub", "openai-sub"):
                    resolved = _resolve_with_red(preferred, red, registry)
                    self.assertIsNotNone(resolved, f"{role} has no head with {red} red")
                    self.assertNotEqual(
                        registry.profile(resolved)["resource"],
                        red,
                        f"{role} resolved onto the red resource",
                    )

    def test_the_shipped_registry_keeps_worker_and_reviewer_apart_on_one_subscription(self):
        """secretary-1165: a card whose preferred family is dead is transferred, not collapsed.

        The chains are written by hand, so nothing but a test stops a canon from routing both roles
        onto one head the moment a resource goes red — and the dispatcher refuses to claim that
        card, which turns a transfer into a stall the shipped registry should never cause.
        """
        canon = canonical_heads(upgrade.running_product_root())
        registry = heads.Registry(canon["resources"], canon["profiles"], canon["role_defaults"])

        for red in ("claude-sub", "openai-sub"):
            with self.subTest(red=red):
                worker = _resolve_with_red(registry.role_default("new_card"), red, registry)
                reviewer = _resolve_with_red(registry.role_default("reviewer"), red, registry)
                self.assertIsNotNone(worker)
                self.assertIsNotNone(reviewer)
                self.assertNotEqual(worker, reviewer, "the review would be the worker's own")

    def test_the_shipped_registry_carries_no_installation_account_policy(self):
        """Account policy and model routing are the private canon's, not the product's."""
        canon = canonical_heads(upgrade.running_product_root())

        self.assertEqual(
            [
                name
                for name, resource in canon["resources"].items()
                if resource.get("account") not in {"claude-subscription", "openai-subscription"}
            ],
            [],
        )
        # secretary-1697: the shipped registry is the five pipeline tiers, and the tiers are defined
        # by their model pins; no other model is pinned.
        self.assertEqual(
            sorted({str(profile.get("model", "")) for profile in canon["profiles"].values()}),
            ["gpt-5.6-terra", "gpt-6-sol", "opus"],
        )

    def test_missing_role_worktrees_are_recreated_from_product_head(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            product = root / "product"
            product.mkdir()
            agent = product / "src" / "ummanu" / "automations" / "agents" / "curator"
            agent.mkdir(parents=True)
            (agent / "automation.toml").write_text("name = 'curator'\n", encoding="utf-8")
            (product / "pyproject.toml").write_text(
                '[tool.ummanu]\nagent-specs = "src/ummanu/automations/agents"\n', encoding="utf-8"
            )
            subprocess.run(["git", "init", "-b", "main", str(product)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(product), "config", "user.name", "Test"], check=True)
            subprocess.run(
                ["git", "-C", str(product), "config", "user.email", "test@example.invalid"],
                check=True,
            )
            subprocess.run(["git", "-C", str(product), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(product), "commit", "-m", "product"], check=True, capture_output=True
            )
            subprocess.run(["git", "-C", str(product), "remote", "add", "origin", str(product)], check=True)

            with mock.patch.dict(os.environ, {"TA_WORKSPACES_ROOT": str(root / "workspaces")}):
                result = upgrade.step_worktrees(self.context(FakeUnitInstaller(), product_root=product))
                again = upgrade.step_worktrees(self.context(FakeUnitInstaller(), product_root=product))

            worktree = root / "workspaces" / "ummanu" / "curator"
            self.assertEqual(result.status, "changed")
            self.assertTrue((worktree / ".git").is_file())
            self.assertEqual(again.status, "unchanged")

    def test_root_materialization_assigns_linked_worktree_and_git_admin_to_runtime_user(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            product = root / "product"
            product.mkdir()
            agent = product / "src" / "ummanu" / "automations" / "agents" / "curator"
            agent.mkdir(parents=True)
            (agent / "automation.toml").write_text("name = 'curator'\n", encoding="utf-8")
            (product / "pyproject.toml").write_text(
                '[tool.ummanu]\nagent-specs = "src/ummanu/automations/agents"\n', encoding="utf-8"
            )
            subprocess.run(["git", "init", "-b", "main", str(product)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(product), "config", "user.name", "Test"], check=True)
            subprocess.run(
                ["git", "-C", str(product), "config", "user.email", "test@example.invalid"], check=True
            )
            subprocess.run(["git", "-C", str(product), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(product), "commit", "-m", "product"], check=True, capture_output=True
            )
            subprocess.run(["git", "-C", str(product), "remote", "add", "origin", str(product)], check=True)
            account = SimpleNamespace(pw_uid=123, pw_gid=456)

            with (
                mock.patch.dict(
                    os.environ, {"TA_WORKSPACES_ROOT": str(root / "home" / "orca" / "workspaces")}
                ),
                mock.patch("ummanu.upgrade.os.geteuid", return_value=0),
                mock.patch("ummanu.upgrade.pwd.getpwnam", return_value=account),
                mock.patch("ummanu.upgrade.os.chown") as chown,
            ):
                result = upgrade.step_worktrees(
                    self.context(FakeUnitInstaller(), product_root=product, runtime_user="operator")
                )

            workspace_root = root / "home" / "orca" / "workspaces"
            worktree = workspace_root / "ummanu" / "curator"
            admin = upgrade._worktree_git_dir(worktree)
            owned = {Path(call.args[0]) for call in chown.call_args_list}
            self.assertEqual(result.status, "changed")
            self.assertIn(worktree, owned)
            self.assertIn(worktree.parent, owned)
            self.assertIn(workspace_root, owned)
            self.assertIn(workspace_root.parent, owned)
            self.assertIsNotNone(admin)
            self.assertIn(admin, owned)
            self.assertIn(admin.parent, owned)

    def test_root_ownership_repair_skips_a_hardlinked_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            first = root / "first"
            linked = root / "linked"
            first.write_text("shared\n", encoding="utf-8")
            os.link(first, linked)
            account = SimpleNamespace(pw_uid=123, pw_gid=456)

            with (
                mock.patch("ummanu.upgrade.os.geteuid", return_value=0),
                mock.patch("ummanu.upgrade.pwd.getpwnam", return_value=account),
                mock.patch("ummanu.upgrade.os.chown") as chown,
            ):
                upgrade._set_runtime_owner(root, "operator")

            owned = {Path(call.args[0]) for call in chown.call_args_list}
            self.assertIn(root, owned)
            self.assertNotIn(first, owned)
            self.assertNotIn(linked, owned)


class HeadRegistryPairTests(unittest.TestCase):
    """The generated registry is a pair in the data directory, never committed or pushed (ummanu-26)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.remote = self.root / "instance-remote.git"
        self.instance = self.root / "instance"
        self.data_dir = self.root / "data"
        self.instance.mkdir()
        self._git(self.root, "init", "--quiet", "--bare", "--initial-branch", "main", str(self.remote))
        self._git(self.instance, "init", "--quiet", "--initial-branch", "main")
        self._git(self.instance, "config", "user.name", "test operator")
        self._git(self.instance, "config", "user.email", "test@example.invalid")
        (self.instance / "instance.yaml").write_text(
            f"version: 1\nname: pair\ndata_dir: {self.data_dir}\n"
            f"offsite:\n  instance_remote: {self.remote}\n",
            encoding="utf-8",
        )
        self._git(self.instance, "add", "instance.yaml")
        self._git(self.instance, "commit", "--quiet", "-m", "instance config")
        self._git(self.instance, "remote", "add", "origin", str(self.remote))
        self._git(self.instance, "push", "--quiet", "origin", "main")
        report = _Report()
        report.data_dir = self.data_dir
        self.context = upgrade.UpgradeContext(
            instance_path=self.instance,
            product_root=upgrade.running_product_root(),
            base_branch="main",
            dry_run=False,
            units=FakeUnitInstaller(),
            report=report,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @staticmethod
    def _git(root: Path, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    def test_lifecycle_sets_only_the_local_instance_packing_controls_idempotently(self) -> None:
        first = upgrade.step_instance_packing(self.context)
        second = upgrade.step_instance_packing(self.context)

        self.assertEqual(first.status, "changed")
        self.assertEqual(second.status, "unchanged")
        self.assertEqual(state_repo.packing_controls(self.instance), dict(state_repo.PACKING_CONTROLS))

    def test_the_pair_is_written_to_the_data_directory_and_never_to_git(self):
        head = self._git(self.instance, "rev-parse", "HEAD")
        remote = self._git(self.remote, "rev-parse", "main")
        calls: list[list[str]] = []
        real_run, real_isolated = _proc.run, _proc.run_isolated

        def recording(real):
            def run(argv, *args, **kwargs):
                calls.append([str(part) for part in argv])
                return real(argv, *args, **kwargs)

            return run

        refused = AssertionError("the head-registry step reached the instance repository")
        with (
            mock.patch.object(_proc, "run", side_effect=recording(real_run)),
            mock.patch.object(_proc, "run_isolated", side_effect=recording(real_isolated)),
            mock.patch.object(state_repo, "git", side_effect=refused),
            mock.patch.object(state_repo, "run_git", side_effect=refused),
        ):
            generated = upgrade.step_head_registry(self.context)

        self.assertEqual(generated.status, "changed", generated.detail)
        pair = generated_pair(self.instance)
        self.assertEqual(pair.snapshot, self.data_dir / "heads" / "heads.yaml")
        self.assertTrue(pair.snapshot.is_file())
        self.assertTrue(pair.source.is_file())
        self.assertFalse((self.instance / "heads").exists())
        # No Git call names the live root: the only one is the product checkout's revision.
        self.assertEqual([argv for argv in calls if str(self.instance) in " ".join(argv)], [])
        self.assertEqual(self._git(self.instance, "rev-parse", "HEAD"), head)
        self.assertEqual(self._git(self.remote, "rev-parse", "main"), remote)
        self.assertEqual(self._git(self.instance, "status", "--porcelain", "--untracked-files=all"), "")
        source = read_source(self.instance)
        self.assertIsNotNone(source)
        self.assertEqual(
            source["canonical"],
            str(canonical_path(self.context.product_root, self.instance)[0].resolve()),
        )
        self.assertEqual(source["product_root"], str(self.context.product_root.resolve()))
        self.assertEqual(source["revision"], product_revision(self.context.product_root))

    def test_an_unchanged_pair_is_unchanged(self):
        upgrade.step_head_registry(self.context)

        again = upgrade.step_head_registry(self.context)

        self.assertEqual(again.status, "unchanged", again.detail)

    def test_the_publication_step_and_its_git_contract_are_gone(self):
        self.assertFalse(hasattr(upgrade, "step_publish_head_registry"))
        self.assertNotIn("publication_policy", {field.name for field in fields(upgrade.UpgradeContext)})
        for name in ("HEADS_PATHSPEC", "HEADS_CHECKPOINT_MESSAGE", "RECOVERY_RECONCILIATION_MESSAGE"):
            with self.subTest(name):
                self.assertFalse(hasattr(state_repo, name))

    def test_verify_accepts_the_pair_with_unrelated_instance_dirt(self):
        tracked = self.instance / "projects" / "operator.yaml"
        tracked.parent.mkdir(parents=True)
        tracked.write_text("id: operator\n", encoding="utf-8")
        self._git(self.instance, "add", "projects/operator.yaml")
        self._git(self.instance, "commit", "--quiet", "-m", "operator project")
        foreign = self.instance / "skills" / "operator-overlay.toml"
        foreign.parent.mkdir(parents=True)
        foreign.write_text("[roles]\n", encoding="utf-8")
        generated = upgrade.step_head_registry(self.context)
        self.assertEqual(generated.status, "changed")
        tracked.write_text("id: operator\nname: changed locally\n", encoding="utf-8")

        with (
            mock.patch("ummanu.upgrade.step_host", return_value=upgrade.StepResult("host", "unchanged")),
            mock.patch("ummanu.upgrade.role_skills.audit", return_value={"ok": True}),
        ):
            verified = upgrade.step_verify(self.context)

        self.assertEqual(verified.status, "unchanged", verified.detail)
        self.assertEqual(
            self._git(self.instance, "status", "--porcelain", "--untracked-files=all").splitlines(),
            ["M projects/operator.yaml", "?? skills/operator-overlay.toml"],
        )

    def test_incomplete_or_stale_pair_fails_closed_before_routing(self):
        upgrade.step_head_registry(self.context)
        pair = generated_pair(self.instance)
        pair.source.unlink()
        with self.assertRaisesRegex(HeadRegistryConfigError, "source pin .* is missing"):
            installed_heads(self.instance)

        upgrade.step_head_registry(self.context)
        pair.snapshot.write_text(
            pair.snapshot.read_text(encoding="utf-8") + "# stale\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(HeadRegistryConfigError, "stale or mismatched"):
            installed_heads(self.instance)


class CommandSurfaceTests(unittest.TestCase):
    """The health and materialize commands as an operator and a gate see them."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.instance = self.root / "instance"
        self.instance.mkdir()
        (self.instance / "instance.yaml").write_text(
            "version: 1\nname: apply\ndata_dir: "
            + str(self.root / "data")
            + "\noffsite:\n  instance_remote: git@example.invalid:x/y\n"
            + "host:\n  unit_prefix: ummanu-\n  foreign_units:\n    - ummanu-supervisor.timer\n",
            encoding="utf-8",
        )
        (self.root / "data").mkdir()
        self.fixture = self.root / "host"
        self.fixture.mkdir()
        # A host runs a checkout, and these fixtures run this one. Reconcile and the role-skill
        # audit read the configured product, so an installation that names none has no units and
        # no manifest to compare against.
        env = mock.patch.dict(os.environ, {"UMMANU_REPO": str(upgrade.running_product_root())})
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_cli(self, argv: list[str]) -> tuple[int, str]:
        import contextlib
        import io

        from ummanu.cli import main

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(argv)
        return code, output.getvalue()

    def test_apply_dry_run_shows_the_plan_and_writes_no_manifest(self):
        (self.fixture / "units.txt").write_text("", encoding="utf-8")
        manifest = self.root / "data" / "host-managed.json"

        code, output = self.run_cli(
            [
                "reconcile",
                "apply",
                "--instance",
                str(self.instance),
                "--host-fixture",
                str(self.fixture),
                "--dry-run",
            ]
        )

        self.assertEqual(code, 0, output)
        self.assertIn("create systemd:unit:ummanu-curator.timer", output)
        self.assertFalse(manifest.exists())

    def test_apply_refuses_an_unowned_name_and_names_the_way_out(self):
        (self.fixture / "units.txt").write_text("ummanu-curator.timer\n", encoding="utf-8")

        code, output = self.run_cli(
            [
                "reconcile",
                "apply",
                "--instance",
                str(self.instance),
                "--host-fixture",
                str(self.fixture),
                "--dry-run",
            ]
        )

        self.assertEqual(code, 1, output)
        self.assertIn("ummanu reconcile adopt", output)
        self.assertIn("host.foreign_units", output)

    def test_a_declared_foreign_unit_does_not_block_apply(self):
        (self.fixture / "units.txt").write_text("ummanu-supervisor.timer\n", encoding="utf-8")

        code, output = self.run_cli(
            [
                "reconcile",
                "apply",
                "--instance",
                str(self.instance),
                "--host-fixture",
                str(self.fixture),
                "--dry-run",
            ]
        )

        self.assertEqual(code, 0, output)

    def test_a_unit_is_adopted_only_when_it_matches_the_shipped_file(self):
        unit_dir = self.root / "units"
        unit_dir.mkdir()
        shipped = upgrade.running_product_root() / "packaging" / "systemd" / "ummanu-curator.timer"
        (unit_dir / "ummanu-curator.timer").write_bytes(shipped.read_bytes())
        argv = [
            "reconcile",
            "adopt",
            "--instance",
            str(self.instance),
            "--logical-id",
            "systemd:unit:ummanu-curator.timer",
            "--unit-dir",
            str(unit_dir),
        ]

        code, output = self.run_cli(argv)
        self.assertEqual(code, 0, output)
        self.assertIn("adopt systemd:unit:ummanu-curator.timer", output)

        (unit_dir / "ummanu-curator.timer").write_text("hand written\n", encoding="utf-8")
        code, output = self.run_cli(argv)
        self.assertEqual(code, 2, output)
        self.assertIn("does not match the shipped file", output)

    def test_role_skills_audit_is_available_as_a_health_command(self):
        # Named: with neither `--instance` nor `UMMANU_INSTANCE` the command takes the default live
        # root, and refuses it when absent (ummanu-39), as it is on a CI runner.
        code, output = self.run_cli(["role-skills", "audit", "--instance", str(self.instance)])
        self.assertIn(code, (0, 1))
        self.assertIn("role skills:", output)


class HealthUnitNameTests(unittest.TestCase):
    def test_agents_map_to_the_packaged_units_not_the_retired_ta_names(self):
        from ummanu.automations.runtime import health

        self.assertEqual(health.timer_unit("curator"), "ummanu-curator.timer")
        self.assertEqual(health.timer_unit("steward"), "ummanu-steward.timer")
        # The pipeline's clock is the production dispatcher's timer.
        self.assertEqual(health.timer_unit("pipeline"), "ummanu-dispatcher-production.timer")

    def test_every_checked_unit_is_one_the_product_ships(self):
        from ummanu.automations.__main__ import HEALTH_COMPONENTS
        from ummanu.automations.runtime import health

        shipped = {
            unit.name
            for unit in load_packaged_units(
                upgrade.running_product_root() / "packaging" / "systemd", UNIT_PREFIX
            )
        }
        for agent in HEALTH_COMPONENTS:
            self.assertIn(health.timer_unit(agent), shipped, agent)


class InstanceHeadCanonTests(unittest.TestCase):
    """Which registry an installation materializes from, and what the snapshot says about it.

    Every fixture here is a temporary instance directory, never the host's own: the point is what
    an arbitrary installation gets, and the developing machine's installation owns a canon that
    would answer for it.
    """

    CANON = (
        '[resources.local-sub]\naccount = "local"\nprobe = "true"\n'
        '[profiles.local-head]\nresource = "local-sub"\nadapter = "claude"\nfallback = []\n'
        '[profiles.local-reviewer]\nresource = "local-sub"\nadapter = "claude"\nfallback = []\n'
        '[role_defaults]\nnew_card = "local-head"\nreviewer = "local-reviewer"\n'
        'curator = "local-head"\nretro = "local-head"\nsteward = "local-head"\n'
        'observer = "local-reviewer"\n'
    )

    def instance(self, root: Path, canon: str | None = None) -> Path:
        (root / "instance.yaml").write_text(
            f"version: 1\nname: canon\ndata_dir: {root / 'data'}\n"
            "offsite:\n  instance_remote: git@example.invalid:x/y.git\n",
            encoding="utf-8",
        )
        (root / "heads").mkdir(exist_ok=True)
        if canon is not None:
            (root / "heads" / "heads.toml").write_text(canon, encoding="utf-8")
        return root

    def test_an_installation_that_owns_a_canon_materializes_from_it(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            instance = self.instance(Path(tmpdir), self.CANON)
            product = upgrade.running_product_root()

            path, origin = canonical_path(product, instance)
            heads_data = canonical_heads(product, instance)

            self.assertEqual(path, instance / "heads" / "heads.toml")
            self.assertEqual(origin, INSTANCE_ORIGIN)
            self.assertEqual(heads_data["role_defaults"]["new_card"], "local-head")
            self.assertNotEqual(heads_data, canonical_heads(product))

    def test_an_installation_with_no_canon_stays_runnable_on_the_product_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            instance = self.instance(Path(tmpdir))
            product = upgrade.running_product_root()

            path, origin = canonical_path(product, instance)

            self.assertEqual(
                path,
                product / "src" / "ummanu" / "runtime" / "heads.toml",
            )
            self.assertEqual(origin, PRODUCT_ORIGIN)
            self.assertEqual(canonical_heads(product, instance), canonical_heads(product))

    def test_a_present_but_unusable_canon_fails_by_name_instead_of_falling_back(self):
        product = upgrade.running_product_root()
        for name, build in (
            (
                "malformed",
                lambda root: (root / "heads" / "heads.toml").write_text("nope = [", encoding="utf-8"),
            ),
            ("directory", lambda root: (root / "heads" / "heads.toml").mkdir()),
            ("dangling", lambda root: (root / "heads" / "heads.toml").symlink_to(root / "gone.toml")),
            (
                "unreadable",
                lambda root: (
                    (root / "heads" / "heads.toml").write_text(self.CANON, encoding="utf-8"),
                    (root / "heads" / "heads.toml").chmod(0o000),
                ),
            ),
        ):
            with self.subTest(name), tempfile.TemporaryDirectory() as tmpdir:
                instance = self.instance(Path(tmpdir))
                build(instance)
                self.addCleanup(_restore_mode, instance / "heads" / "heads.toml")

                with self.assertRaises(HeadRegistryConfigError) as caught:
                    canonical_heads(product, instance)

                self.assertIn(str(instance / "heads" / "heads.toml"), str(caught.exception))

    @unittest.skipIf(os.geteuid() == 0, "root traverses a directory with no search bit")
    def test_an_unsearchable_heads_directory_fails_the_step_by_name(self):
        """The probe that decides which canon wins can itself fail on the filesystem.

        `Path.is_file()` does not swallow EACCES, so a `heads/` directory with no search bit used
        to hand `ummanu upgrade` a raw PermissionError. The step catches the bounded config
        error and nothing else, so that crashed the upgrade instead of failing one step by path.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            instance = self.instance(Path(tmpdir), self.CANON)
            owned = instance / "heads" / "heads.toml"
            self.addCleanup(_restore_mode, instance / "heads", mode=0o755)
            (instance / "heads").chmod(0o000)

            with self.assertRaises(HeadRegistryConfigError) as caught:
                canonical_path(upgrade.running_product_root(), instance)
            result = upgrade.step_head_registry(self.context(instance))

            self.assertIn(str(owned), str(caught.exception))
            self.assertEqual(result.status, "failed")
            self.assertIn(str(owned), result.detail)

    def test_a_canon_with_a_malformed_entry_fails_the_upgrade_step_by_name(self):
        """Not just unparseable files: a parsed canon whose entries are the wrong shape.

        The entries are hand-written, so any of them can be a list where a table or a name
        belongs. Every one of those has to come back as the bounded config error naming the file
        — the upgrade step handles that error and nothing else, so a raw AttributeError or
        TypeError would escape the step instead of failing it.
        """
        broken = {
            "list profile": '[resources.local-sub]\naccount = "local"\nprofiles = { local-head = [] }\n',
            "list resource": "resources = { local-sub = [] }\n"
            '[profiles.local-head]\nresource = "local-sub"\nadapter = "claude"\n'
            '[role_defaults]\nnew_card = "local-head"\n',
            "list role default": '[resources.local-sub]\naccount = "local"\n'
            '[profiles.local-head]\nresource = "local-sub"\nadapter = "claude"\n'
            "[role_defaults]\nnew_card = []\n",
            "list fallback entry": '[resources.local-sub]\naccount = "local"\n'
            '[profiles.local-head]\nresource = "local-sub"\n'
            'adapter = "claude"\nfallback = [[]]\n'
            '[role_defaults]\nnew_card = "local-head"\n',
            "list adapter": '[resources.local-sub]\naccount = "local"\n'
            '[profiles.local-head]\nresource = "local-sub"\nadapter = []\n'
            '[role_defaults]\nnew_card = "local-head"\n',
        }
        for name, canon in broken.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmpdir:
                instance = self.instance(Path(tmpdir), canon)
                owned = str(instance / "heads" / "heads.toml")

                with self.assertRaises(HeadRegistryConfigError) as caught:
                    canonical_heads(upgrade.running_product_root(), instance)
                result = upgrade.step_head_registry(self.context(instance))

                self.assertIn(owned, str(caught.exception))
                self.assertEqual(result.status, "failed")
                self.assertIn(owned, result.detail)
                self.assertFalse(generated_pair(instance).snapshot.exists())

    def test_status_reports_a_malformed_installed_snapshot_instead_of_crashing(self):
        """`ummanu status` validates the snapshot on its own, so it meets the same shapes."""
        with tempfile.TemporaryDirectory() as tmpdir:
            instance = self.instance(Path(tmpdir))
            generated_pair(instance).snapshot.parent.mkdir(parents=True)
            generated_pair(instance).snapshot.write_text(
                "resources:\n  local-sub:\n    account: local\n"
                "profiles:\n  local-head: []\n"
                "role_defaults:\n  new_card: local-head\n",
                encoding="utf-8",
            )

            snapshot = str(generated_pair(instance).snapshot)
            with self.assertRaises(HeadRegistryConfigError) as caught:
                installed_heads(instance)
            record = status._head_registry(instance)

            self.assertIn(snapshot, str(caught.exception))
            self.assertIn(snapshot, record["error"])

    def test_the_snapshot_and_pin_name_the_canon_that_actually_won(self):
        for name, canon, origin in (
            ("instance", self.CANON, INSTANCE_ORIGIN),
            ("product", None, PRODUCT_ORIGIN),
        ):
            with self.subTest(name), tempfile.TemporaryDirectory() as tmpdir:
                instance = self.instance(Path(tmpdir), canon)
                context = self.context(instance)
                expected, _ = canonical_path(context.product_root, instance)

                upgrade.step_head_registry(context)

                header = generated_pair(instance).snapshot.read_text(encoding="utf-8").splitlines()[0]
                pin = read_source(instance)
                self.assertIn(str(expected), header)
                self.assertEqual(pin["canonical"], str(expected))
                self.assertEqual(pin["canonical_owner"], origin)
                self.assertEqual(pin["product_root"], str(context.product_root.resolve()))

    def test_a_snapshot_built_from_the_instance_canon_is_not_stale_against_it(self):
        """Verify compares the snapshot with the canon that made it, not with the product's."""
        with tempfile.TemporaryDirectory() as tmpdir:
            instance = self.instance(Path(tmpdir), self.CANON)
            context = self.context(instance)

            upgrade.step_head_registry(context)

            self.assertEqual(
                assert_snapshot_current(instance, context.product_root)["role_defaults"]["new_card"],
                "local-head",
            )

    def context(self, instance: Path) -> upgrade.UpgradeContext:
        report = _Report()
        report.data_dir = instance / "data"
        return upgrade.UpgradeContext(
            instance_path=instance,
            product_root=upgrade.running_product_root(),
            base_branch="main",
            dry_run=False,
            units=FakeUnitInstaller(),
            report=report,
        )


class PipelineStateStepTests(unittest.TestCase):
    """`pipeline-state` restores the untracked run journals a recreated worktree lost (ummanu-1)."""

    RECORDS: ClassVar = [{"event": "claim", "reference": "ummanu-1"}, {"event": "review"}]

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.instance = self.root / "instance"
        self.state_dir = self.root / "home" / "pipeline" / "state" / "pipeline"
        patcher = mock.patch.dict(os.environ, {"TA_PIPELINE_STATE_DIR": str(self.state_dir)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def checkpoint(self, records: list[dict] | None = None) -> None:
        runs = self.instance / "state" / "runs"
        runs.mkdir(parents=True)
        rows = [
            {"source": "runs.jsonl", "line": line, "record": record}
            for line, record in enumerate(self.RECORDS if records is None else records, start=1)
        ]
        (runs / "runs.ndjson").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    def context(self, **overrides) -> upgrade.UpgradeContext:
        base = upgrade.UpgradeContext(
            instance_path=self.instance,
            product_root=upgrade.running_product_root(),
            base_branch="main",
            dry_run=False,
            units=FakeUnitInstaller(),
            report=_Report(),
            runtime_home=self.root / "home",
        )
        return replace(base, **overrides)

    def journal(self) -> list[dict]:
        text = (self.state_dir / "runs.jsonl").read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def test_the_step_runs_right_after_the_role_worktrees(self):
        index = upgrade.STEPS.index(upgrade.step_worktrees)
        self.assertIs(upgrade.STEPS[index + 1], upgrade.step_pipeline_state)

    def test_an_absent_state_dir_is_restored_with_exactly_the_checkpoint_records(self):
        self.checkpoint()

        result = upgrade.step_pipeline_state(self.context())

        self.assertEqual(result.status, "changed", result.detail)
        self.assertIn("restored 2 run record(s)", result.detail)
        self.assertEqual(self.journal(), self.RECORDS)

    def test_a_second_run_is_unchanged(self):
        self.checkpoint()
        upgrade.step_pipeline_state(self.context())
        stamp = (self.state_dir / "runs.jsonl").stat().st_mtime_ns

        result = upgrade.step_pipeline_state(self.context())

        self.assertEqual(result.status, "unchanged", result.detail)
        self.assertEqual((self.state_dir / "runs.jsonl").stat().st_mtime_ns, stamp)

    def test_an_empty_live_journal_receives_the_checkpoint_records(self):
        self.checkpoint()
        self.state_dir.mkdir(parents=True)
        (self.state_dir / "runs.jsonl").write_text("", encoding="utf-8")

        result = upgrade.step_pipeline_state(self.context())

        self.assertEqual(result.status, "changed", result.detail)
        self.assertEqual(self.journal(), self.RECORDS)

    def test_a_live_journal_that_extends_the_checkpoint_is_preserved(self):
        self.checkpoint()
        self.state_dir.mkdir(parents=True)
        live = "".join(json.dumps(record) + "\n" for record in [*self.RECORDS, {"event": "release"}])
        (self.state_dir / "runs.jsonl").write_text(live, encoding="utf-8")

        result = upgrade.step_pipeline_state(self.context())

        self.assertEqual(result.status, "unchanged", result.detail)
        self.assertEqual((self.state_dir / "runs.jsonl").read_text(encoding="utf-8"), live)

    def test_a_diverged_live_journal_is_refused_and_left_byte_for_byte(self):
        self.checkpoint()
        self.state_dir.mkdir(parents=True)
        live = b'{"event": "live"}\n'
        (self.state_dir / "runs.jsonl").write_bytes(live)

        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run):
                result = upgrade.step_pipeline_state(self.context(dry_run=dry_run))

                self.assertEqual(result.status, "failed")
                self.assertIn("does not extend the checkpoint", result.detail)
                self.assertEqual((self.state_dir / "runs.jsonl").read_bytes(), live)

    def test_dry_run_reports_the_restore_and_writes_nothing(self):
        self.checkpoint()

        result = upgrade.step_pipeline_state(self.context(dry_run=True))

        self.assertEqual(result.status, "changed", result.detail)
        self.assertIn("would restore 2 run record(s)", result.detail)
        self.assertFalse(self.state_dir.parent.exists())

    def test_a_checkpoint_without_records_never_creates_an_empty_source(self):
        """An empty directory would make the next export replace the checkpoint's runs with nothing."""
        for name, records in (("no journal", None), ("no records", [])):
            with self.subTest(name):
                if records is not None:
                    self.checkpoint(records)

                result = upgrade.step_pipeline_state(self.context())

                self.assertEqual(result.status, "skipped", result.detail)
                self.assertFalse(self.state_dir.exists())

    def test_recovery_leaves_the_restore_to_its_own_ownership_barrier(self):
        seen: list[tuple] = []

        def run(context, *, steps):
            seen.append(tuple(steps))
            return upgrade.UpgradeResult()

        with (
            mock.patch("ummanu.installation.validate_instance", return_value=SimpleNamespace(ok=True)),
            mock.patch("ummanu.installation.check_product_runtime"),
            mock.patch(
                "ummanu.installation.resolve_runtime_owner", return_value=("operator", self.root / "home")
            ),
            mock.patch("ummanu.installation.run_steps", side_effect=run),
        ):
            installation.materialize_host(self.instance, self.root / "product", before_host=lambda _: None)

        self.assertTrue(seen)
        self.assertFalse(any(upgrade.step_pipeline_state in steps for steps in seen))


def _restore_mode(path: Path, mode: int = 0o644) -> None:
    """Give a deliberately unreadable fixture back its permissions so cleanup can remove it."""
    try:
        path.chmod(mode)
    except OSError:
        pass


def _commit_all(root: Path, message: str) -> None:
    """Commit every file under ``root``, initializing the repository the first time."""
    if not (root / ".git").exists():
        subprocess.run(["git", "-C", str(root), "init", "--quiet", "--initial-branch=main"], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--quiet",
            "--allow-empty",
            "-m",
            message,
        ],
        check=True,
    )


class _Report:
    """The slice of an InstanceReport the host and memory steps read."""

    host: ClassVar = {"unit_prefix": UNIT_PREFIX}
    instance: ClassVar = {
        "host": {"unit_prefix": UNIT_PREFIX},
        "data_dir": "/tmp/does-not-matter",
    }
    data_dir = Path("/tmp/does-not-matter")
    bindings: ClassVar[list] = []


if __name__ == "__main__":
    unittest.main()
