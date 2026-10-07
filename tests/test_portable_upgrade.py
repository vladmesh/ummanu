"""Upgrading an installation from a product checkout that is not the running one.

Every fixture here is built from nothing: a second product checkout with its own skill manifest,
head canon and unit templates, a home directory this test owns, and an instance that has never been
upgraded. Nothing under the developing machine's home or inside the checkout that runs these tests
may reach the result, because the whole point of the materializer is that another checkout can
install a host without the running one having a say.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.fakes.upgrade import FakeUnitInstaller
from tests.retired_board import (
    LEGACY_ENV,
    LEGACY_VALUES,
    STALE_FILE,
    STATUS_SECTION,
    legacy_runtime_lines,
    write_stale_leftovers,
)
from ummanu import cli as cli_module, installation, role_skills, upgrade
from ummanu.cli import main as cli_main
from ummanu.config import validate_instance
from ummanu.head_registry import (
    INSTANCE_ORIGIN,
    PRODUCT_ORIGIN,
    canonical_heads,
    canonical_path,
    generated_pair,
    read_source,
)
from ummanu.host import SHIPPED_PACKAGING_ROOT, LiveHostSource
from ummanu.host_apply import HostCommandError, SystemdUnitInstaller, resolve_packaged, strict_manifest
from ummanu.projects.availability import ProjectAvailability
from ummanu.runtime import heads as shipped_heads

UNIT_PREFIX = "ummanu-"
# Read at import, before any fixture patches the home or the account database: these are the two
# places a portable run is not allowed to reach.
LIVE_HOME = str(Path.home())
RUNNING_CHECKOUT = str(Path(upgrade.__file__).resolve().parents[1])

# A canon small enough to read and complete enough to validate. The two fixtures below differ in
# which file carries it, which is the whole question the head-registry step answers.
PRODUCT_CANON = """
[resources.portable-sub]
account = "portable"
probe = "true"

[profiles.portable-head]
resource = "portable-sub"
adapter = "claude"
fallback = []

[profiles.portable-reviewer]
resource = "portable-sub"
adapter = "claude"
fallback = []

[role_defaults]
new_card = "portable-head"
reviewer = "portable-reviewer"
curator = "portable-head"
retro = "portable-head"
steward = "portable-head"
observer = "portable-reviewer"
"""

INSTANCE_CANON = PRODUCT_CANON.replace("portable", "owned")

SERVICE = """[Unit]
Description=Portable {component}

[Service]
Type=simple
User={{{{UMMANU_RUNTIME_USER}}}}
WorkingDirectory={{{{UMMANU_PRODUCT_ROOT}}}}
Environment=UMMANU_INSTANCE={{{{UMMANU_INSTANCE_PATH}}}}
Environment=UMMANU_DATA_DIR={{{{UMMANU_DATA_DIR}}}}
ExecStart={{{{UMMANU_RUNTIME_HOME}}}}/.local/bin/ummanu-{component}

[Install]
WantedBy=default.target
"""

TIMER = """[Unit]
Description=Portable {component} timer

[Timer]
OnCalendar=hourly
Unit=ummanu-{component}.service

[Install]
WantedBy=timers.target
"""

MANIFEST = """
[roles.ummanu]
skills = ["portable-skill"]

[targets.codex-portable]
shell = "codex"
root = "~/shells/codex/skills"
roles = ["ummanu"]

[targets.claude-portable]
shell = "claude"
root = "~/shells/claude/skills"
roles = ["ummanu"]
"""

OVERLAY = """
[roles.ummanu]
skills = ["owned-skill"]
"""


class RecordingUnits(FakeUnitInstaller):
    """A systemd double that keeps the fixture host's inventory honest.

    `verify` re-plans against the host the run just wrote, so a fixture whose unit list never
    moves would report every install the run performed as still pending.
    """

    def __init__(self, fixture: Path) -> None:
        super().__init__()
        self.fixture = fixture
        self._publish()

    def _publish(self) -> None:
        (self.fixture / "units.txt").write_text(
            "".join(f"{name}\n" for name in sorted(self.files)), encoding="utf-8"
        )
        (self.fixture / "unit-states.txt").write_text(
            "".join(
                f"{name} {enabled} {active}\n"
                for name, (enabled, active) in sorted(self.unit_states().items())
            ),
            encoding="utf-8",
        )

    def install(self, unit) -> None:
        super().install(unit)
        self._publish()

    def remove(self, name: str) -> None:
        super().remove(name)
        self._publish()

    def enable(self, name: str) -> None:
        super().enable(name)
        self._publish()

    def disable(self, name: str) -> None:
        super().disable(name)
        self._publish()

    def start(self, name: str) -> None:
        super().start(name)
        self._publish()

    def restart(self, name: str) -> None:
        super().restart(name)
        self._publish()


class PortableFixture(unittest.TestCase):
    """A clean installation materialized entirely from a checkout this test wrote."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        # Two homes, because they are two accounts. `home` belongs to the installation being
        # materialized; `invoker_home` is whoever typed the command — an operator repairing
        # somebody else's installation, or root after a recovery. Everything an upgrade writes
        # belongs in the first, and a step that reads the process environment lands in the second,
        # where these tests can see it.
        self.home = self.root / "home"
        self.invoker_home = self.root / "invoker"
        self.home.mkdir()
        self.invoker_home.mkdir()
        self.product = self.root / "product"
        self.instance = self.root / "instance"
        self.data = self.root / "data"
        self.host_fixture = self.root / "host"
        for path in (self.instance, self.data, self.host_fixture):
            path.mkdir()
        self.units = RecordingUnits(self.host_fixture)
        self.write_product()
        self.write_instance()
        self._initialize_instance_repo()
        # A fully replaced environment: an inherited UMMANU_INSTANCE, TA_* or runtime variable would
        # point some part of the run back at the live installation, which is exactly the failure
        # this fixture exists to rule out.
        env = mock.patch.dict(
            os.environ,
            {"HOME": str(self.invoker_home), "PATH": os.environ.get("PATH", "")},
            clear=True,
        )
        env.start()
        self.addCleanup(env.stop)
        account = SimpleNamespace(pw_dir=str(self.home), pw_name="operator")
        for target, kwargs in (
            ("ummanu.host_apply.pwd.getpwnam", {"return_value": account}),
            ("ummanu.host_apply.pwd.getpwuid", {"return_value": account}),
        ):
            patch = mock.patch(target, **kwargs)
            patch.start()
            self.addCleanup(patch.stop)

    # --- fixture construction -------------------------------------------------------------

    def write_product(self) -> None:
        manifest = self.product / "skills" / "manifest.toml"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(MANIFEST, encoding="utf-8")
        skill = manifest.parent / "roles" / "ummanu" / "portable-skill"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# portable-skill\n", encoding="utf-8")
        (skill / "portable-skill.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        canon = self.product / "src" / "ummanu" / "runtime" / "heads.toml"
        canon.parent.mkdir(parents=True)
        canon.write_text(PRODUCT_CANON, encoding="utf-8")
        packaging = self.product / "packaging" / "systemd"
        packaging.mkdir(parents=True)
        for component in ("memory", "dispatcher-production"):
            (packaging / f"ummanu-{component}.service").write_text(
                SERVICE.format(component=component), encoding="utf-8"
            )
        (packaging / "ummanu-dispatcher-production.timer").write_text(
            TIMER.format(component="dispatcher-production"), encoding="utf-8"
        )
        # A portable product checkout is now also the strict source of the
        # active product memory pack. Keep this fixture self-contained rather
        # than letting an upgrade read the checkout running the test.
        memory_pack = self.product / "packaging" / "memory" / "product-ummanu"
        memory_pack.mkdir(parents=True)
        pack_fact = b"---\nsource: product:ummanu\n---\nportable product fact\n"
        (memory_pack / "portable.md").write_bytes(pack_fact)
        (memory_pack / "manifest.yaml").write_text(
            "\n".join(
                (
                    "schema: 1",
                    "product: ummanu",
                    "namespace: product:ummanu",
                    "status: active",
                    "ownership: shipped",
                    "fact_format: markdown-frontmatter-v1",
                    "reconciliation:",
                    "  identity: id",
                    "  digest: sha256",
                    "  manifest_is_complete: true",
                    "  absent_id: delete",
                    "  unchanged_digest: retain_embedding",
                    "overlay_policy:",
                    "  local_overlay_allowed: true",
                    "  shipped_id_collision: reject",
                    "facts:",
                    "  - id: portable",
                    "    path: portable.md",
                    f"    sha256: {hashlib.sha256(pack_fact).hexdigest()}",
                    "",
                )
            ),
            encoding="utf-8",
        )
        # The non-secret codex runtime files an install seeds into the owner's managed CODEX_HOME.
        # A checkout without them is not one an install can materialize, so the fixture ships them.
        codex_home = self.product / "packaging" / "codex-home"
        codex_home.mkdir(parents=True)
        (codex_home / "AGENTS.md").write_text("# portable\n", encoding="utf-8")
        (codex_home / "config.toml").write_text("[portable]\n", encoding="utf-8")
        # The shared part of the interactive head's persona, composed into `<data>/interactive`.
        interactive = self.product / "packaging" / "interactive-workspace"
        interactive.mkdir(parents=True)
        (interactive / "AGENTS.md").write_text("# portable interactive head\n", encoding="utf-8")
        # An installed product is a Git checkout: `dependencies` and `memory` bind their receipts to
        # its revision and tracked inputs, so a fixture that is not one could never be current.
        for command in (
            ["git", "-C", str(self.product), "init", "--quiet", "--initial-branch", "main"],
            ["git", "-C", str(self.product), "add", "-A"],
            [
                "git",
                "-C",
                str(self.product),
                "-c",
                "user.name=portable operator",
                "-c",
                "user.email=portable@example.invalid",
                "commit",
                "--quiet",
                "-m",
                "product",
            ],
        ):
            subprocess.run(command, check=True, capture_output=True)

    def write_instance(self) -> None:
        (self.instance / "instance.yaml").write_text(
            "version: 1\nname: portable\ndata_dir: "
            + str(self.data)
            + "\noffsite:\n  instance_remote: git@example.invalid:x/y\n"
            + f"host:\n  unit_prefix: {UNIT_PREFIX}\n",
            encoding="utf-8",
        )

    def _initialize_instance_repo(self) -> None:
        """Give the materializer the private checkpoint it requires in production."""
        remote = self.root / "instance-remote.git"
        for command in (
            ["git", "init", "--quiet", "--bare", "--initial-branch", "main", str(remote)],
            ["git", "-C", str(self.instance), "init", "--quiet", "--initial-branch", "main"],
            ["git", "-C", str(self.instance), "config", "user.name", "portable operator"],
            ["git", "-C", str(self.instance), "config", "user.email", "portable@example.invalid"],
            ["git", "-C", str(self.instance), "add", "instance.yaml"],
            ["git", "-C", str(self.instance), "commit", "--quiet", "-m", "instance config"],
            ["git", "-C", str(self.instance), "remote", "add", "origin", str(remote)],
            ["git", "-C", str(self.instance), "push", "--quiet", "-u", "origin", "main"],
        ):
            subprocess.run(command, check=True)

    def own_a_canon(self) -> Path:
        canon = self.instance / "heads" / "heads.toml"
        canon.parent.mkdir(parents=True, exist_ok=True)
        canon.write_text(INSTANCE_CANON, encoding="utf-8")
        return canon

    def own_a_skill(self) -> Path:
        overlay = self.instance / "skills" / "manifest.toml"
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_text(OVERLAY, encoding="utf-8")
        skill = overlay.parent / "roles" / "ummanu" / "owned-skill"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# owned-skill\n", encoding="utf-8")
        return overlay

    # --- driving the materializer ---------------------------------------------------------

    def context(self, **overrides) -> upgrade.UpgradeContext:
        report = validate_instance(self.instance)
        self.assertTrue(report.ok, report.errors)
        base = upgrade.UpgradeContext(
            instance_path=self.instance,
            product_root=self.product,
            base_branch="main",
            dry_run=False,
            units=self.units,
            host_fixture=self.host_fixture,
            pull=False,
            report=report,
            runtime_user="operator",
            runtime_home=self.home,
        )
        return base if not overrides else upgrade.replace(base, **overrides)

    def run_upgrade(self, **overrides) -> upgrade.UpgradeResult:
        # This fixture replaces systemd and the host inventory; it intentionally has no MCP daemon.
        # The dedicated memory health tests cover the authenticated service boundary.
        with mock.patch.object(upgrade, "probe_memory"):
            return upgrade.run_steps(self.context(**overrides))

    def run_upgrade_command(self, **overrides) -> int:
        """The public entry point, with only the host-touching clients replaced by doubles.

        Everything an upgrade decides about which account it is materializing for is left to the
        command itself: it is the caller that has no `runtime_user` to pass, and the reason the
        argument exists is that the command has to work it out.
        """
        args = SimpleNamespace(
            instance=str(self.instance),
            product_root=str(self.product),
            base_branch="main",
            dry_run=False,
            no_pull=True,
            host_fixture=str(self.host_fixture),
            json=False,
        )
        for key, value in overrides.items():
            setattr(args, key, value)
        with (
            mock.patch.object(upgrade, "SystemdUnitInstaller", return_value=self.units),
            mock.patch.object(upgrade, "probe_memory"),
        ):
            return upgrade.run_upgrade(args)

    def run_cli(self, argv: list[str]) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli_main(argv)
        return code, output.getvalue()

    def run_json_cli(self, argv: list[str]) -> tuple[int, dict]:
        code, output = self.run_cli(argv)
        return code, json.loads(output)

    #: The fixture's live root is the private repository's work tree (`_initialize_instance_repo`),
    #: the pre-cutover shape doctor reds since ummanu-39. A doctor run that found nothing else exits 1
    #: with that one finding where it used to exit 0 with none.
    WORK_TREE = "live_root.git_work_tree"

    def assert_only_the_work_tree_finding(self, code: int, report: dict) -> None:
        self.assertEqual(code, 1, report)
        self.assertEqual([finding["code"] for finding in report["findings"]], [self.WORK_TREE], report)

    def assert_only_the_work_tree_line(self, code: int, text: str) -> None:
        self.assertEqual(code, 1, text)
        self.assertEqual(text.count("live_root."), 1, text)
        self.assertIn(f"{self.WORK_TREE}: ", text)
        self.assertIn("status: findings", text)

    def statuses(self, result: upgrade.UpgradeResult) -> dict[str, str]:
        return {step.name: step.status for step in result.steps}

    def shell_skill(self, shell: str, skill: str, home: Path | None = None) -> Path:
        return (home or self.home) / "shells" / shell / "skills" / skill / "SKILL.md"

    def assert_invoker_home_untouched(self) -> None:
        self.assertEqual(sorted(path.name for path in self.invoker_home.iterdir()), [])

    def assert_hermetic(self, text: str) -> None:
        """Nothing in a result may name the developing machine's home or this checkout."""
        for foreign in (RUNNING_CHECKOUT, LIVE_HOME):
            self.assertNotIn(foreign, text)


class PackagedRuntimeParityTests(PortableFixture):
    """Real upgrade/doctor consumers, using the full shipped catalogue on an isolated host."""

    def setUp(self) -> None:
        super().setUp()
        shutil.copytree(SHIPPED_PACKAGING_ROOT, self.product / "packaging" / "systemd", dirs_exist_ok=True)
        context = self.context()
        prepared = upgrade.run_steps(
            context,
            (
                upgrade.step_role_skills,
                upgrade.step_head_registry,
                upgrade.step_instance_packing,
            ),
        )
        self.assertTrue(prepared.ok, prepared.render())
        self.assertFalse(upgrade.step_host(context).failed)
        self.units.calls.clear()

    def verify(self, **overrides):
        # Process receipts have their own fixture coverage. Keep those consumers running here,
        # with their evidence provided by the fake host, while exercising real host verification.
        with (
            mock.patch.object(
                upgrade, "_receipt_evidence", return_value=(True, "web receipt current")
            ) as web,
            mock.patch.object(
                upgrade, "_po_receipt_evidence", return_value=(True, "PO receipt current")
            ) as po,
        ):
            result = upgrade.step_verify(self.context(**overrides))
        if not result.failed:
            web.assert_called_once()
            po.assert_called_once()
        return result

    def doctor_inventory(self):
        return cli_module.collect_host_inventory(
            self.context().report, SimpleNamespace(host_fixture=str(self.host_fixture))
        )

    def test_full_catalog_rendering_ownership_and_completed_oneshots(self):
        packaged = resolve_packaged(
            self.context().report.instance,
            self.product / "packaging" / "systemd",
            product_root=self.product,
            instance_path=self.instance,
            data_dir=self.data,
            runtime_user="operator",
        )
        self.assertEqual(len(packaged), 20)
        from ummanu.infra.doctor_record import TIMEOUT_SECONDS

        service = self.units.files["ummanu-doctor.service"].decode()
        timer = self.units.files["ummanu-doctor.timer"].decode()
        self.assertIn(f"ExecStart={self.product}/.venv/bin/ummanu doctor-record --instance {self.instance} --data-dir {self.data}", service)
        self.assertIn(f"EnvironmentFile=-{self.instance}/runtime.env", service)
        self.assertIn("User=operator", service)
        outer = int(next(line.split("=", 1)[1] for line in service.splitlines() if line.startswith("TimeoutStartSec=")))
        self.assertGreater(outer, TIMEOUT_SECONDS)
        self.assertIn("KillMode=control-group", service)
        self.assertIn("OnUnitInactiveSec=60s", timer)
        self.assertIn("Unit=ummanu-doctor.service", timer)
        self.assertEqual(self.units.files, {unit.name: unit.content for unit in packaged})
        managed, error = strict_manifest(self.data / "host-managed.json")
        self.assertEqual(error, "")
        self.assertEqual({resource.name for resource in managed}, set(self.units.files))
        self.assertEqual(self.units.enabled, {unit.name for unit in packaged if unit.installable})
        for unit in packaged:
            self.assertNotIn(b"{{UMMANU_", unit.content)
            if unit.name.endswith(".service"):
                self.assertIn(b"User=operator", unit.content)
            if unit.oneshot and not unit.installable:
                self.assertNotIn(unit.name, self.units.active)
        for component in ("steward", "retro", "steward-deep-sweep", "doctor", "checkpoint"):
            self.assertIn(f"ummanu-{component}.timer", self.units.enabled)
            self.assertIn(
                f"Unit=ummanu-{component}.service".encode(),
                self.units.files[f"ummanu-{component}.timer"],
            )
        self.assertFalse(self.verify().failed)
        expected, collected, diffs = self.doctor_inventory()
        self.assertEqual(cli_module._unit_runtime_findings(expected, collected), [])
        self.assertEqual(diffs["units"].missing_on_host, [])
        self.assertEqual(upgrade.step_host(self.context()).status, "unchanged")
        self.assertEqual(self.units.calls, [])

    def test_inactive_and_disabled_timers_fail_both_consumers_and_reconcile_unchanged_bytes(self):
        manifest = (self.data / "host-managed.json").read_bytes()
        before = dict(self.units.files)
        for component in ("steward", "retro", "steward-deep-sweep", "doctor", "checkpoint"):
            for enabled, action in ((True, "start"), (False, "enable")):
                name = f"ummanu-{component}.timer"
                with self.subTest(name=name, enabled=enabled):
                    self.units.active.discard(name)
                    if not enabled:
                        self.units.enabled.discard(name)
                    self.units._publish()
                    result = self.verify()
                    self.assertTrue(result.failed, result.detail)
                    self.assertIn(f"{name}: expected active, got inactive", result.detail)
                    expected, collected, _ = self.doctor_inventory()
                    self.assertIn(
                        f"{name}: expected active, got inactive",
                        cli_module._unit_runtime_findings(expected, collected),
                    )
                    if not enabled:
                        self.assertIn(f"{name}: expected enabled, got disabled", result.detail)
                    preview = upgrade.step_host(self.context(dry_run=True))
                    self.assertEqual(preview.status, "changed", preview.detail)
                    self.assertIn(f"{action} {name}", preview.detail)
                    self.assertEqual(self.units.calls, [])
                    self.assertEqual((self.data / "host-managed.json").read_bytes(), manifest)
                    repaired = upgrade.step_host(self.context())
                    self.assertEqual(repaired.status, "changed", repaired.detail)
                    self.assertEqual(self.units.calls, [(action, name)])
                    self.assertEqual(self.units.files, before)
                    self.assertEqual((self.data / "host-managed.json").read_bytes(), manifest)
                    self.assertFalse(self.verify().failed)
                    self.units.calls.clear()

    def test_missing_catalog_units_independently_fail_verify_and_doctor(self):
        before = dict(self.units.files)
        for name in before:
            with self.subTest(name=name):
                self.units.files.pop(name)
                self.units._publish()
                result = self.verify()
                self.assertTrue(result.failed, result.detail)
                self.assertIn(f"create {name}", result.detail)
                expected, collected, diffs = self.doctor_inventory()
                self.assertEqual(diffs["units"].missing_on_host, [name])
                self.assertEqual(cli_module._unit_runtime_findings(expected, collected), [])
                self.assertEqual(self.units.calls, [])
                self.units.files[name] = before[name]
                self.units._publish()

    def test_existing_materializer_updates_steward_and_retro_templates_and_layout(self):
        for component in ("steward", "retro", "doctor"):
            name = f"ummanu-{component}.service"
            template = self.product / "packaging" / "systemd" / name
            template.write_bytes(template.read_bytes() + b"\n# changed catalogue input\n")
            result = upgrade.step_host(self.context())
            self.assertEqual(result.status, "changed", result.detail)
            self.assertIn(f"update {name}", result.detail)
            self.assertIn(("install", name), self.units.calls)
            self.assertNotIn(("enable", name), self.units.calls)
            self.units.calls.clear()
        other_home = self.root / "other-owner-home"
        with mock.patch(
            "ummanu.host_apply.pwd.getpwnam", return_value=SimpleNamespace(pw_dir=str(other_home))
        ):
            result = upgrade.step_host(self.context())
        self.assertEqual(result.status, "changed", result.detail)
        for component in ("steward", "retro", "doctor"):
            self.assertIn(str(other_home).encode(), self.units.files[f"ummanu-{component}.service"])
        managed, error = strict_manifest(self.data / "host-managed.json")
        self.assertEqual(error, "")
        self.assertEqual({resource.name for resource in managed}, set(self.units.files))

    def test_opted_out_and_foreign_units_are_excluded_from_materialization_and_assessment(self):
        # A foreign declaration relinquishes a real owned unit through the supported plan boundary.
        config = self.instance / "instance.yaml"
        config.write_text(
            config.read_text()
            + "  components:\n    retro: {enabled: false}\n"
            + "  foreign_units: [ummanu-steward.service, ummanu-steward.timer]\n"
        )
        self.units.active.discard("ummanu-steward.timer")
        self.units.enabled.discard("ummanu-steward.timer")
        self.units._publish()
        foreign_before = {name: content for name, content in self.units.files.items() if "steward." in name}
        result = upgrade.step_host(self.context())
        self.assertFalse(result.failed, result.detail)
        self.assertFalse(any("steward." in name for _, name in self.units.calls))
        self.assertFalse(any("retro." in name for name in self.units.files))
        self.assertEqual({name: self.units.files[name] for name in foreign_before}, foreign_before)
        expected, collected, diffs = self.doctor_inventory()
        self.assertNotIn("ummanu-steward.timer", expected.unit_runtime)
        self.assertNotIn("ummanu-retro.timer", expected.unit_runtime)
        self.assertEqual(cli_module._unit_runtime_findings(expected, collected), [])
        self.assertEqual(diffs["units"].unmanaged_on_host, [])
        self.assertFalse(self.verify().failed)

    def test_failed_runtime_repair_is_failed_and_verify_still_reads_inactive(self):
        name = "ummanu-steward.timer"
        self.units.active.discard(name)
        self.units.fail_on.add(name)
        self.units._publish()
        result = upgrade.step_host(self.context())
        self.assertTrue(result.failed, result.detail)
        self.assertIn("start", result.detail)
        self.assertTrue(self.verify().failed)

    def test_doctor_component_opt_out_and_foreign_pair_use_existing_ownership_rules(self):
        config = self.instance / "instance.yaml"
        original = config.read_text()
        pair = {"ummanu-doctor.service", "ummanu-doctor.timer"}
        before = {name: self.units.files[name] for name in pair}
        config.write_text(original + "  foreign_units: [ummanu-doctor.service, ummanu-doctor.timer]\n")
        self.units.active.discard("ummanu-doctor.timer")
        self.units._publish()
        result = upgrade.step_host(self.context())
        self.assertFalse(result.failed, result.detail)
        self.assertFalse(any(name in pair for _, name in self.units.calls))
        self.assertEqual({name: self.units.files[name] for name in pair}, before)
        expected, collected, diffs = self.doctor_inventory()
        self.assertNotIn("ummanu-doctor.timer", expected.unit_runtime)
        self.assertEqual(cli_module._unit_runtime_findings(expected, collected), [])
        self.assertEqual(diffs["units"].unmanaged_on_host, [])
        self.assertFalse(self.verify().failed)
        # A disabled component in a fresh install owns/renders neither unit.
        config.write_text(original + "  components:\n    doctor: {enabled: false}\n")
        result = upgrade.step_host(self.context())
        self.assertFalse(result.failed, result.detail)
        self.assertTrue(pair.isdisjoint(self.units.files))
        self.assertFalse(self.verify().failed)

    def test_required_long_running_service_is_repaired_and_verified(self):
        name = "ummanu-memory.service"
        self.units.active.discard(name)
        self.units._publish()
        result = self.verify()
        self.assertTrue(result.failed, result.detail)
        self.assertIn(f"{name}: expected active, got inactive", result.detail)
        expected, collected, _ = self.doctor_inventory()
        self.assertIn(
            f"{name}: expected active, got inactive", cli_module._unit_runtime_findings(expected, collected)
        )
        repaired = upgrade.step_host(self.context())
        self.assertFalse(repaired.failed, repaired.detail)
        self.assertEqual(self.units.calls, [("start", name)])
        self.assertFalse(self.verify().failed)

    def test_process_reconciliation_respects_foreign_and_disabled_components(self):
        config = self.instance / "instance.yaml"
        config.write_text(
            config.read_text()
            + "  components:\n    memory: {enabled: false}\n"
            + "  foreign_units: [ummanu-web.service, ummanu-po.service]\n"
        )
        context = self.context()
        host = upgrade.step_host(context)
        self.assertFalse(host.failed, host.detail)
        self.units.calls.clear()
        with (
            mock.patch.object(upgrade, "_receipt_evidence") as web,
            mock.patch.object(upgrade, "_po_receipt_evidence") as po,
            mock.patch.object(self.units, "is_active", side_effect=AssertionError("excluded process probe")),
        ):
            for step in (upgrade.step_memory, upgrade.step_web, upgrade.step_po):
                result = step(context)
                self.assertEqual(result.status, "skipped", result.detail)
            verified = upgrade.step_verify(context)
            self.assertFalse(verified.failed, verified.detail)
            web.assert_not_called()
            po.assert_not_called()
        self.assertEqual(self.units.calls, [])

    def test_runtime_state_absent_is_unavailable_without_live_fixture_probes(self):
        (self.host_fixture / "unit-states.txt").unlink()
        with mock.patch("ummanu.infra.systemd._proc.run", side_effect=AssertionError("live probe")):
            result = self.verify()
            expected, collected, _ = self.doctor_inventory()
            findings = cli_module._unit_runtime_findings(expected, collected)
        self.assertTrue(result.failed, result.detail)
        self.assertIn("runtime status unavailable", result.detail)
        self.assertTrue(findings)
        self.assertTrue(all("unavailable" in finding for finding in findings))
        self.assertEqual(self.units.calls, [])

    def systemctl_reply(self, argv, **kwargs):
        """Native systemctl boundary backed by the same isolated installation as the file fixture."""
        if argv[0] != "systemctl":
            return self.real_run(argv, **kwargs)
        self.assertEqual(argv[1], "--system")
        self.assertEqual(kwargs["timeout"], 10)
        self.systemctl_calls.append(argv)
        verb, name = argv[2], argv[-1]
        if self.bus_error and (self.error_probe == "list-unit-files" or verb == self.error_probe):
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr=self.bus_error)
        if verb == "list-unit-files":
            output = "".join(f"{unit} enabled enabled\n" for unit in sorted(self.units.files))
            return subprocess.CompletedProcess(argv, 0, stdout=output, stderr="")
        if verb == "list-units":
            output = "".join(
                f"{unit} loaded {active} running Disposable fixture\n"
                for unit, (_, active) in sorted(self.units.unit_states().items())
            )
            return subprocess.CompletedProcess(argv, 0, stdout=output, stderr="")
        if verb == "show":
            return subprocess.CompletedProcess(argv, 0, stdout="n/a\n", stderr="")
        enabled, active = self.units.unit_states()[name]
        state = enabled if verb == "is-enabled" else active
        code = 0 if state in {"active", "enabled"} else (3 if verb == "is-active" else 1)
        return subprocess.CompletedProcess(argv, code, stdout=state + "\n", stderr="")

    def native_boundary(self, error="", probe="list-unit-files"):
        from ummanu import _proc

        self.real_run = _proc.run
        self.systemctl_calls = []
        self.bus_error = error
        self.error_probe = probe
        return mock.patch("ummanu.infra.systemd._proc.run", side_effect=self.systemctl_reply)

    def test_successful_native_enumeration_with_bus_and_connect_foreign_names(self):
        args = [
            "doctor",
            "--dry-run",
            "--instance",
            str(self.instance),
            "--host-fixture",
            str(self.host_fixture),
        ]
        baseline_code, baseline = self.run_json_cli([*args, "--json"])
        self.assertEqual(baseline_code, 1, baseline)
        self.assertEqual(
            {finding["code"] for finding in baseline["findings"]},
            {"production_runtime_provenance", "dispatcher", self.WORK_TREE},
        )
        foreign = {"ummanu-bus-forwarder.service", "ummanu-connect-forwarder.service"}
        config = self.instance / "instance.yaml"
        config.write_text(config.read_text() + f"  foreign_units: {sorted(foreign)}\n")
        manifest = (self.data / "host-managed.json").read_bytes()
        files = dict(self.units.files)
        # Native enumeration reads only this temporary root, never the live manager's units.
        unit_dir = self.root / "systemd-root" / "etc" / "systemd" / "system"
        unit_dir.mkdir(parents=True)
        for name, content in files.items():
            (unit_dir / name).write_bytes(content)
        for name in foreign:
            (unit_dir / name).write_text("[Service]\nExecStart=/bin/true\n")
        foreign_files = {name: (unit_dir / name).read_bytes() for name in foreign}
        native = subprocess.run(
            [
                "systemctl",
                "--system",
                f"--root={self.root / 'systemd-root'}",
                "list-unit-files",
                "--no-legend",
                "ummanu-*",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        self.assertEqual(native.stderr, "")
        self.assertTrue(foreign <= {line.split()[0] for line in native.stdout.splitlines()})

        def reply(argv, **kwargs):
            if argv[:3] == ["systemctl", "--system", "list-unit-files"]:
                return subprocess.CompletedProcess(argv, native.returncode, native.stdout, native.stderr)
            return self.systemctl_reply(argv, **kwargs)

        with (
            self.native_boundary(),
            mock.patch("ummanu.infra.systemd._proc.run", side_effect=reply),
            mock.patch.object(cli_module, "FixtureHostSource", return_value=LiveHostSource("operator")),
        ):
            result = self.verify(host_fixture=None)
            self.assertEqual(result.status, "unchanged", result.detail)
            text_code, output = self.run_cli(args)
            json_code, payload = self.run_json_cli([*args, "--json"])
        self.assertEqual(text_code, baseline_code, output)
        self.assertEqual(json_code, baseline_code, payload)
        self.assertEqual(payload["findings"], baseline["findings"])
        self.assertNotIn("unavailable: system manager/bus", output)
        self.assertEqual(self.units.files, files)
        self.assertEqual({name: (unit_dir / name).read_bytes() for name in foreign}, foreign_files)
        self.assertEqual((self.data / "host-managed.json").read_bytes(), manifest)
        self.assertEqual(self.units.calls, [])

    def test_previously_owned_foreign_dispatcher_is_unchanged_in_all_consumers(self):
        pair = {"ummanu-dispatcher-production.service", "ummanu-dispatcher-production.timer"}
        managed, error = strict_manifest(self.data / "host-managed.json")
        self.assertEqual(error, "")
        self.assertTrue(pair <= {resource.name for resource in managed})
        self.assertEqual(len(self.units.files), 20)
        files = dict(self.units.files)
        manifest = (self.data / "host-managed.json").read_bytes()
        state = self.data / "dispatcher" / "production-state.json"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({"owner": "operator", "phase": "ready"}))
        args = [
            "doctor",
            "--dry-run",
            "--instance",
            str(self.instance),
            "--host-fixture",
            str(self.host_fixture),
        ]
        baseline_code, baseline = self.run_json_cli([*args, "--json"])
        self.assertEqual(baseline_code, 1, baseline)
        self.assertEqual(
            {finding["code"] for finding in baseline["findings"]},
            {"production_runtime_provenance", "dispatcher", self.WORK_TREE},
        )
        self.assertFalse(any("managed unit mismatch" in str(finding) for finding in baseline["findings"]))
        config = self.instance / "instance.yaml"
        config.write_text(config.read_text() + f"  foreign_units: {sorted(pair)}\n")
        self.assertEqual(upgrade.step_host(self.context()).status, "unchanged")
        result = self.verify()
        self.assertEqual(result.status, "unchanged", result.detail)
        text_code, output = self.run_cli(args)
        json_code, payload = self.run_json_cli([*args, "--json"])
        self.assertEqual(text_code, baseline_code, output)
        self.assertEqual(json_code, baseline_code, payload)
        self.assertEqual(payload["findings"], baseline["findings"])
        self.assertNotIn("managed unit mismatch", output)
        self.assertEqual(self.units.files, files)
        self.assertEqual((self.data / "host-managed.json").read_bytes(), manifest)
        self.assertEqual(self.units.calls, [])

    def test_failed_connection_diagnostic_on_stdout_is_unavailable_in_all_consumers(self):
        def reply(argv, **kwargs):
            if argv[:3] == ["systemctl", "--system", "list-unit-files"]:
                return subprocess.CompletedProcess(
                    argv, 1, "Failed to connect to bus: No such file or directory\n", ""
                )
            return self.systemctl_reply(argv, **kwargs)

        with (
            self.native_boundary(),
            mock.patch("ummanu.infra.systemd._proc.run", side_effect=reply),
            mock.patch.object(cli_module, "FixtureHostSource", return_value=LiveHostSource("operator")),
        ):
            result = self.verify(host_fixture=None)
            self.assertTrue(result.failed, result.detail)
            self.assertIn("system manager/bus unavailable: manager/bus connection failed", result.detail)
            args = [
                "doctor",
                "--dry-run",
                "--instance",
                str(self.instance),
                "--host-fixture",
                str(self.host_fixture),
            ]
            text_code, output = self.run_cli(args)
            json_code, payload = self.run_json_cli([*args, "--json"])
        self.assertEqual(text_code, 2, output)
        self.assertEqual(json_code, 2, payload)
        self.assertIn(
            "units:\n  unavailable: system manager/bus unavailable: manager/bus connection failed", output
        )
        self.assertIn(
            {
                "code": "host_inventory_unavailable",
                "kind": "units",
                "message": "system manager/bus unavailable: manager/bus connection failed",
            },
            payload["findings"],
        )
        self.assertFalse(
            any(finding["code"] in {"unit_runtime", "missing_on_host"} for finding in payload["findings"])
        )
        self.assertEqual(self.units.calls, [])

    def test_excluded_schema_valid_symlink_loop_preserves_upgrade_project_policy(self):
        loop = self.root / "unavailable-checkout"
        loop.symlink_to(loop)
        project = self.instance / "projects" / "unavailable.yaml"
        project.parent.mkdir(parents=True)
        project.write_text(
            f"id: unavailable\nrepo: {loop}\nenabled: true\norca_binding: unavailable\n"
            "adapter: unavailable\ndefault_branch: main\n"
        )
        context = self.context(host_fixture=None)
        availability = ProjectAvailability.inspect(context.report.bindings)
        self.assertEqual(availability.unavailable, frozenset({"unavailable"}))
        context.project_availability = availability
        manifest = (self.data / "host-managed.json").read_bytes()
        files = dict(self.units.files)
        with self.native_boundary():
            host = upgrade.step_host(context)
            self.assertEqual(host.status, "unchanged", host.detail)
            result = self.verify(host_fixture=None, project_availability=availability)
            self.assertEqual(result.status, "unchanged", result.detail)
        self.assertEqual(self.units.files, files)
        self.assertEqual((self.data / "host-managed.json").read_bytes(), manifest)
        self.assertEqual(self.units.calls, [])
        # Unit assessment is still required when the unavailable checkout is excluded.
        self.units.active.discard("ummanu-steward.timer")
        with self.native_boundary():
            result = self.verify(host_fixture=None, project_availability=availability)
        self.assertTrue(result.failed, result.detail)
        self.assertIn("ummanu-steward.timer: expected active, got inactive", result.detail)
        self.assertEqual(self.units.calls, [])

    def test_included_project_symlink_loop_remains_unavailable_in_upgrade(self):
        # Upgrade's existing project policy inspects names relative to cwd before projects_root.
        loop = self.root / "included-checkout"
        loop.symlink_to(loop)
        project = self.instance / "projects" / "included.yaml"
        project.parent.mkdir(parents=True)
        project.write_text(
            f"id: included\nrepo: {loop}\nenabled: true\norca_binding: included\n"
            "adapter: included\ndefault_branch: main\n"
        )
        previous_cwd = Path.cwd()
        try:
            os.chdir(self.root)
            with self.native_boundary():
                host = upgrade.step_host(self.context(host_fixture=None))
                result = self.verify(host_fixture=None)
        finally:
            os.chdir(previous_cwd)
        for step in (host, result):
            self.assertTrue(step.failed, step.detail)
            self.assertIn("projects: expected project checkout path could not be inspected", step.detail)
        self.assertEqual(self.units.calls, [])

    def test_root_and_different_shell_identity_observe_the_same_owner_and_system_manager(self):
        for shell_user, effective_uid in (("root", 0), ("different-shell-user", 2000)):
            with (
                self.subTest(shell_user=shell_user),
                mock.patch.dict(os.environ, {"USER": shell_user}),
                mock.patch("ummanu.host_apply.os.geteuid", return_value=effective_uid),
                self.native_boundary(),
            ):
                context = self.context(host_fixture=None)
                captured = []
                real_source = LiveHostSource

                def source(runtime_user, captured=captured, real_source=real_source):
                    captured.append(runtime_user)
                    return real_source(runtime_user)

                with (
                    mock.patch.object(upgrade, "LiveHostSource", side_effect=source),
                    mock.patch.object(cli_module, "LiveHostSource", side_effect=source),
                ):
                    result = self.verify(host_fixture=None)
                    expected, collected, diffs = cli_module.collect_host_inventory(
                        context.report, SimpleNamespace(host_fixture=None)
                    )
                self.assertFalse(result.failed, result.detail)
                self.assertEqual(captured, ["operator", "operator"])
                self.assertEqual(collected.errors, {})
                self.assertEqual(diffs["units"].missing_on_host, [])
                self.assertEqual(cli_module._unit_runtime_findings(expected, collected), [])
                installer = SystemdUnitInstaller(sudo=False, runtime_user="operator")
                self.assertTrue(installer.is_active("ummanu-steward.timer"))
                self.assertEqual(installer.observation.runtime_user, "operator")
                self.assertTrue(self.systemctl_calls)
                self.assertTrue(all("--user" not in argv for argv in self.systemctl_calls))

    def test_native_inactive_exit_is_a_finding_in_verify_and_text_json_doctor(self):
        name = "ummanu-steward.timer"
        self.units.active.discard(name)
        self.units._publish()
        with self.native_boundary():
            result = upgrade.step_verify(self.context(host_fixture=None))
            self.assertTrue(result.failed, result.detail)
            self.assertIn(f"{name}: expected active, got inactive", result.detail)
            self.assertFalse(SystemdUnitInstaller(sudo=False).is_active(name))
            with mock.patch.object(cli_module, "FixtureHostSource", return_value=LiveHostSource("operator")):
                text_code, output = self.run_cli(
                    [
                        "doctor",
                        "--dry-run",
                        "--instance",
                        str(self.instance),
                        "--host-fixture",
                        str(self.host_fixture),
                    ]
                )
                json_code, payload = self.run_json_cli(
                    [
                        "doctor",
                        "--dry-run",
                        "--instance",
                        str(self.instance),
                        "--host-fixture",
                        str(self.host_fixture),
                        "--json",
                    ]
                )
        self.assertEqual(text_code, 1, output)
        self.assertEqual(json_code, 1, payload)
        message = f"{name}: expected active, got inactive"
        self.assertIn(message, output)
        self.assertIn({"code": "unit_runtime", "message": message}, payload["findings"])
        self.assertFalse(
            any(finding["code"] == "host_inventory_unavailable" for finding in payload["findings"])
        )

    def test_unavailable_manager_and_historical_user_bus_error_fail_all_observation_consumers(self):
        for diagnostic in (
            "Failed to connect to bus: No such file or directory",
            "Failed to connect to user bus: $DBUS_SESSION_BUS_ADDRESS and $XDG_RUNTIME_DIR not defined",
        ):
            for probe in ("list-unit-files", "is-active"):
                with (
                    self.subTest(diagnostic=diagnostic, probe=probe),
                    self.native_boundary(diagnostic, probe),
                ):
                    result = upgrade.step_verify(self.context(host_fixture=None))
                    self.assertTrue(result.failed, result.detail)
                    self.assertIn(
                        "system manager/bus unavailable: manager/bus connection failed", result.detail
                    )
                    self.assertNotIn("expected active", result.detail)
                    installer = SystemdUnitInstaller(sudo=False, runtime_user="operator")
                    with self.assertRaisesRegex(HostCommandError, "manager/bus connection failed"):
                        installer.is_active("ummanu-steward.timer")
                    with mock.patch.object(
                        cli_module, "FixtureHostSource", return_value=LiveHostSource("operator")
                    ):
                        text_code, output = self.run_cli(
                            [
                                "doctor",
                                "--dry-run",
                                "--instance",
                                str(self.instance),
                                "--host-fixture",
                                str(self.host_fixture),
                            ]
                        )
                        json_code, payload = self.run_json_cli(
                            [
                                "doctor",
                                "--dry-run",
                                "--instance",
                                str(self.instance),
                                "--host-fixture",
                                str(self.host_fixture),
                                "--json",
                            ]
                        )
                    self.assertEqual(text_code, 2, output)
                    self.assertEqual(json_code, 2, payload)
                    self.assertIn("manager/bus connection failed", output)
                    unavailable = [
                        finding
                        for finding in payload["findings"]
                        if finding["code"] == "host_inventory_unavailable"
                    ]
                    self.assertEqual(len(unavailable), 1, payload)
                    self.assertIn("manager/bus connection failed", unavailable[0]["message"])
                    self.assertFalse(
                        any(
                            finding["code"] in {"missing_on_host", "unit_runtime"}
                            for finding in payload["findings"]
                        )
                    )
                    self.assertEqual(self.units.calls, [])


class StaleTransportLeftoverTests(PortableFixture):
    """An older installation still carries the retired transport's file and runtime.env lines.

    Upgrade and doctor neither read nor report them: they are files and variables nothing names.
    """

    def setUp(self) -> None:
        super().setUp()
        (self.instance / ".gitignore").write_text("/runtime.env\n/" + STALE_FILE + "\n", encoding="utf-8")
        self.runtime = self.instance / "runtime.env"
        self.runtime_body = "OTHER=value\n" + legacy_runtime_lines()
        self.runtime.write_text(self.runtime_body, encoding="utf-8")
        self.runtime.chmod(0o600)
        self.stale = write_stale_leftovers(self.instance)
        self.stale_body = self.stale.read_bytes()

    def assert_nothing_names_the_transport(self, text: str) -> None:
        self.assertNotIn(STALE_FILE, text)
        self.assertNotIn("board transport", text.lower())
        self.assertNotIn(STATUS_SECTION, text)
        for name, value in zip(LEGACY_ENV, LEGACY_VALUES):
            self.assertNotIn(name, text)
            self.assertNotIn(value, text)

    def test_upgrade_and_doctor_neither_read_nor_report_the_leftovers(self) -> None:
        real_open = Path.open

        def guarded_open(path, *args, **kwargs):
            if Path(path).name == STALE_FILE:
                raise AssertionError(f"{path} was opened")
            return real_open(path, *args, **kwargs)

        with mock.patch.object(Path, "open", guarded_open):
            result = self.run_upgrade()
            instance = ["--instance", str(self.instance), "--offline", "--json"]
            code, report = self.run_json_cli(["doctor", *instance])
            status_code, status_text = self.run_cli(["status", *instance])

        self.assertTrue(result.ok, result.render())
        self.assertFalse([step.name for step in result.steps if "transport" in step.name])
        self.assert_nothing_names_the_transport(result.render())
        self.assert_only_the_work_tree_finding(code, report)
        self.assertNotIn(STATUS_SECTION, report["status"])
        self.assert_nothing_names_the_transport(json.dumps(report))
        self.assertEqual(status_code, 0, status_text)
        self.assertNotIn(STATUS_SECTION, json.loads(status_text))
        self.assert_nothing_names_the_transport(status_text)
        self.assertEqual(self.stale.read_bytes(), self.stale_body)
        self.assertEqual(self.runtime.read_text(encoding="utf-8"), self.runtime_body)


class PortableInstallationTests(PortableFixture):
    """An installation with no overlays at all: everything comes from the named checkout."""

    def test_an_installation_with_no_overlays_materializes_from_the_named_checkout(self) -> None:
        result = self.run_upgrade()

        self.assertTrue(result.ok, result.render())
        self.assertEqual(self.statuses(result)["role-skills"], "changed")
        self.assertTrue(self.shell_skill("codex", "portable-skill").is_file())
        self.assertTrue(self.shell_skill("claude", "portable-skill").is_file())
        self.assertEqual(
            (self.home / "bin" / "portable-skill").resolve(),
            self.product / "skills" / "roles" / "ummanu" / "portable-skill" / "portable-skill.sh",
        )
        # The running checkout's own manifest targets `~/.claude/skills` and `~/.hermes/...`. It
        # was never named here, so a step that read it would leave those directories behind in
        # this fixture's home, and nothing else in the run would say so.
        self.assertEqual(sorted(path.name for path in self.home.iterdir()), ["bin", "shells"])
        self.assert_invoker_home_untouched()
        self.assert_hermetic(result.render())

    def test_the_head_canon_falls_back_to_the_named_checkout_not_the_running_one(self) -> None:
        canonical, origin = canonical_path(self.product, self.instance)
        result = self.run_upgrade()

        pin = read_source(self.instance)
        self.assertEqual(
            canonical,
            self.product / "src" / "ummanu" / "runtime" / "heads.toml",
        )
        self.assertEqual(origin, PRODUCT_ORIGIN)
        self.assertTrue(result.ok, result.render())
        self.assertIn("portable-head", generated_pair(self.instance).snapshot.read_text(encoding="utf-8"))
        self.assertEqual(pin["canonical"], str(canonical))
        self.assertEqual(pin["canonical_owner"], PRODUCT_ORIGIN)
        self.assertEqual(pin["product_root"], str(self.product))

    def test_the_units_are_rendered_from_the_named_checkout_and_the_installation_user(self) -> None:
        context = self.context()

        with mock.patch.object(upgrade, "probe_memory"):
            result = upgrade.run_steps(context)
        rendered = b"\n".join(
            unit.content
            for unit in upgrade.resolve_packaged(
                context.report.instance,
                self.product / "packaging" / "systemd",
                product_root=self.product,
                instance_path=self.instance,
                data_dir=context.report.data_dir,
                runtime_user="operator",
            )
        )

        self.assertTrue(result.ok, result.render())
        self.assertIn(f"WorkingDirectory={self.product}".encode(), rendered)
        self.assertIn(f"UMMANU_INSTANCE={self.instance}".encode(), rendered)
        self.assertIn(f"ExecStart={self.home}/.local/bin".encode(), rendered)
        self.assertIn("User=operator", rendered.decode())
        self.assert_hermetic(rendered.decode())

    def test_a_dry_run_upgrade_never_asks_orca_about_automations(self) -> None:
        """secretary-1706: systemd units are the only schedule owner of the background roles.

        The product ships a background agent's spec, which is exactly what the retired automations
        step turned into `orca automations` calls; the upgrade must now issue none of them.
        """
        agent = self.product / "src" / "ummanu" / "automations" / "agents" / "curator"
        agent.mkdir(parents=True, exist_ok=True)
        (agent / "automation.toml").write_text('name = "curator"\nskill = "/curate"\n', encoding="utf-8")
        (self.product / "pyproject.toml").write_text(
            '[tool.ummanu]\nagent-specs = "src/ummanu/automations/agents"\n', encoding="utf-8"
        )
        argvs: list[list[str]] = []
        real_popen_init = subprocess.Popen.__init__

        def recording_init(popen, args, *rest, **kwargs):
            argvs.append([str(arg) for arg in args] if isinstance(args, (list, tuple)) else [str(args)])
            return real_popen_init(popen, args, *rest, **kwargs)

        with (
            mock.patch.object(subprocess.Popen, "__init__", recording_init),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            code = self.run_upgrade_command(dry_run=True)

        self.assertEqual(code, 0, output.getvalue())
        self.assertTrue(argvs, "the recorder saw no subprocess at all")
        self.assertFalse([argv for argv in argvs if "automations" in argv], argvs)
        self.assertNotIn("automations", output.getvalue())

    def test_a_second_run_against_the_installation_it_just_wrote_changes_nothing(self) -> None:
        first = self.run_upgrade()
        second = self.run_upgrade()

        self.assertTrue(first.ok, first.render())
        self.assertTrue(second.ok, second.render())
        self.assertTrue(first.changed)
        self.assertEqual(
            [step.name for step in second.steps if step.status == "changed"], [], second.render()
        )

    def test_offline_doctor_reads_the_fresh_installation_without_the_live_host(self) -> None:
        self.run_upgrade()

        code, report = self.run_json_cli(["doctor", "--instance", str(self.instance), "--offline", "--json"])

        registry = report["status"]["installation"]["head_registry"]
        self.assert_only_the_work_tree_finding(code, report)
        self.assertEqual(report["status"]["installation"]["instance"], str(self.instance / "instance.yaml"))
        self.assertEqual(registry["snapshot"], str(generated_pair(self.instance).snapshot))
        self.assertEqual(registry["snapshot"], str(self.data / "heads" / "heads.yaml"))
        self.assertNotIn("legacy_source", registry)
        self.assertEqual(registry["product_root"], str(self.product))
        self.assertEqual(registry["canonical_owner"], PRODUCT_ORIGIN)
        self.assertFalse(registry["error"], registry)

    def test_offline_doctor_plans_against_the_units_of_the_installations_own_checkout(self) -> None:
        """The pin says which checkout this host runs, and the units come from there.

        The running checkout ships a much larger unit catalogue under the same prefix — curator,
        retro, steward and the rest — so a doctor that read its own `packaging/systemd` would
        report those as missing on a host that was never installed from it.
        """
        self.run_upgrade()

        code, report = self.run_json_cli(["doctor", "--instance", str(self.instance), "--offline", "--json"])

        self.assert_only_the_work_tree_finding(code, report)
        self.assertEqual(
            sorted(unit["name"] for unit in report["status"]["host"]["units"]),
            [
                f"{UNIT_PREFIX}dispatcher-production.service",
                f"{UNIT_PREFIX}dispatcher-production.timer",
                f"{UNIT_PREFIX}memory.service",
            ],
        )
        self.assert_hermetic(json.dumps(report))

    def test_offline_doctor_reads_the_pinned_checkout_over_a_configured_one(self) -> None:
        """A decoy in the environment must not outrank what the installation was installed from."""
        self.run_upgrade()
        decoy = self.root / "decoy"
        (decoy / "packaging" / "systemd").mkdir(parents=True)

        with mock.patch.dict(os.environ, {"UMMANU_REPO": str(decoy)}):
            code, report = self.run_json_cli(
                ["doctor", "--instance", str(self.instance), "--offline", "--json"]
            )

        self.assert_only_the_work_tree_finding(code, report)
        self.assertTrue(report["status"]["host"]["units"], report["status"]["host"])

    def test_the_upgrade_command_installs_the_checkout_it_was_pointed_at(self) -> None:
        """`--product-root` has to reach the steps, or the skills come from the running module."""
        seen: list[upgrade.UpgradeContext] = []

        with mock.patch.object(
            upgrade, "run_steps", side_effect=lambda context: seen.append(context) or upgrade.UpgradeResult()
        ):
            code = upgrade.run_upgrade(
                SimpleNamespace(
                    instance=str(self.instance),
                    product_root=str(self.product),
                    base_branch="main",
                    dry_run=True,
                    no_pull=True,
                    host_fixture=str(self.host_fixture),
                    json=False,
                )
            )

        self.assertEqual(code, 0)
        self.assertEqual(seen[0].product_root, self.product)
        self.assertEqual(
            upgrade._role_skills_manifest(seen[0]),
            role_skills.product_manifest_path(self.product),
        )

    def test_the_role_skill_audit_reads_the_named_checkout_rather_than_the_running_one(self) -> None:
        """The running checkout ships neither this manifest nor this skill, so a stale read shows."""
        manifest = role_skills.product_manifest_path(self.product)

        audit = role_skills.audit(instance_path=self.instance, product_manifest=manifest)

        self.assertEqual([source["path"] for source in audit["manifests"]], [str(manifest)])
        self.assertEqual(sorted({item["skill"] for item in audit["missing"]}), ["portable-skill"])
        self.assertNotEqual(manifest, role_skills.manifest_path())

    def test_the_command_line_delivers_the_named_checkouts_skills(self) -> None:
        """A hand-run sync has no installation owner to resolve and uses the caller's own home."""
        code, output = self.run_cli(
            [
                "role-skills",
                "sync",
                "--instance",
                str(self.instance),
                "--product-root",
                str(self.product),
            ]
        )

        self.assertTrue(self.shell_skill("codex", "portable-skill", self.invoker_home).is_file())
        self.assertEqual(code, 0, output)
        self.assert_hermetic(output)


class CodexHomeMigrationTests(PortableFixture):
    """secretary-1710: upgrade seeds `<data_dir>/codex-home`; doctor names the home heads launch with."""

    def test_upgrade_seeds_the_data_dir_home_copy_once_without_a_login(self) -> None:
        first = self.run_upgrade()
        second = self.run_upgrade()

        self.assertTrue(first.ok, first.render())
        self.assertEqual(self.statuses(first)["codex-home"], "changed", first.render())
        self.assertEqual(self.statuses(second)["codex-home"], "unchanged", second.render())
        data_home = self.data / "codex-home"
        self.assertEqual((data_home / "AGENTS.md").read_text(encoding="utf-8"), "# portable\n")
        self.assertFalse((data_home / "auth.json").exists())
        # Upgrade never touched the legacy home and still does not; that stays install's.
        self.assertFalse((self.home / ".config").exists())
        self.assert_invoker_home_untouched()

    def test_doctor_reports_a_missing_login_red_with_the_fix_and_then_the_active_home(self) -> None:
        """A20 step 7 (secretary-1723): no legacy fallback, so no login is a red finding naming the fix."""
        canon = self.product / "src" / "ummanu" / "runtime" / "heads.toml"
        canon.write_text(
            PRODUCT_CANON
            + '\n[profiles.portable-codex]\nresource = "portable-sub"\nadapter = "codex"\nfallback = []\n',
            encoding="utf-8",
        )
        self.run_upgrade()
        instance = ["doctor", "--instance", str(self.instance), "--offline"]
        data_home = self.data / "codex-home"
        fix = (
            f"no Codex login for this installation: log in under {data_home} "
            f"(`CODEX_HOME={data_home} codex login`), or copy an auth.json there"
        )

        code, text = self.run_cli(instance)
        json_code, report = self.run_json_cli([*instance, "--json"])

        self.assertEqual(code, 1, text)
        self.assertIn(f"error: codex home: {fix}", text)
        self.assertNotIn("legacy", text)
        self.assertEqual(json_code, 1, report)
        self.assertEqual(
            report["codex_home"],
            {
                "path": None,
                "kind": "",
                "data_dir_home": str(data_home),
                "login_missing": fix,
                "codex_required": True,
            },
        )
        self.assertIn({"code": "codex_home_login_missing", "message": fix}, report["findings"])

        (data_home / "auth.json").write_text('{"tokens": "fixture"}\n', encoding="utf-8")
        code, text = self.run_cli(instance)
        json_code, report = self.run_json_cli([*instance, "--json"])

        self.assert_only_the_work_tree_line(code, text)
        self.assertIn(f"codex home: {data_home} (data-dir home)", text)
        self.assertNotIn("error: codex home", text)
        self.assert_only_the_work_tree_finding(json_code, report)
        self.assertEqual(report["codex_home"]["kind"], "data-dir")
        self.assertEqual(report["codex_home"]["login_missing"], "")

    def test_an_installation_without_a_codex_profile_is_not_red_for_a_missing_login(self) -> None:
        self.run_upgrade()
        code, text = self.run_cli(["doctor", "--instance", str(self.instance), "--offline"])

        self.assert_only_the_work_tree_line(code, text)
        self.assertIn("codex home: none, and no installed profile runs Codex", text)


class InstallationOwnerTests(PortableFixture):
    """Whose home an upgrade materializes into, when that is not the caller's.

    The reproduction is a repair: root, or an operator on the same box, runs `ummanu upgrade`
    against an installation owned by somebody else. Every home-relative path the run writes has to
    be the owner's, because the units the same run renders name the owner's home and nothing else
    will go looking in `/root` for the skills they were supposed to find.
    """

    def test_the_upgrade_command_resolves_the_installation_owner_from_the_instance(self) -> None:
        seen: list[upgrade.UpgradeContext] = []

        with mock.patch.object(
            upgrade, "run_steps", side_effect=lambda context: seen.append(context) or upgrade.UpgradeResult()
        ):
            code = self.run_upgrade_command(dry_run=True)

        self.assertEqual(code, 0)
        self.assertEqual(seen[0].runtime_user, "operator")
        self.assertEqual(seen[0].runtime_home, self.home)
        self.assertNotEqual(seen[0].runtime_home, self.invoker_home)

    def test_nonroot_dry_run_reports_an_unreadable_managed_manifest(self) -> None:
        with (
            mock.patch.object(upgrade.os, "geteuid", return_value=1000),
            mock.patch.object(
                upgrade, "strict_manifest", return_value=([], "managed manifest is unreadable")
            ),
        ):
            code, output = self.capture(lambda: self.run_upgrade_command(dry_run=True))

        self.assertEqual(code, 1)
        self.assertIn("failed    host: managed manifest is unreadable", output)

    def test_an_explicit_runtime_user_wins_over_the_directory_owner(self) -> None:
        seen: list[upgrade.UpgradeContext] = []

        with (
            mock.patch.object(
                upgrade,
                "run_steps",
                side_effect=lambda context: seen.append(context) or upgrade.UpgradeResult(),
            ),
            mock.patch("ummanu.host_apply.pwd.getpwuid", side_effect=AssertionError("owner probed")),
        ):
            code = self.run_upgrade_command(dry_run=True, runtime_user="named")

        self.assertEqual(code, 0)
        self.assertEqual(seen[0].runtime_user, "named")
        self.assertEqual(seen[0].runtime_home, self.home)

    def test_the_command_delivers_skills_and_entry_points_under_the_owners_home(self) -> None:
        code = self.run_upgrade_command()

        self.assertEqual(code, 0)
        self.assertTrue(self.shell_skill("codex", "portable-skill").is_file())
        self.assertTrue(self.shell_skill("claude", "portable-skill").is_file())
        self.assertEqual(
            (self.home / "bin" / "portable-skill").resolve(),
            self.product / "skills" / "roles" / "ummanu" / "portable-skill" / "portable-skill.sh",
        )
        self.assert_invoker_home_untouched()

    def test_role_worktrees_belong_to_the_owner(self) -> None:
        """Not written here; decided from a home, and it must be the owner's."""
        agent = self.product / "src" / "ummanu" / "automations" / "agents" / "curator"
        agent.mkdir(parents=True, exist_ok=True)
        (agent / "automation.toml").write_text('name = "curator"\nskill = "curate"\n', encoding="utf-8")
        (self.product / "pyproject.toml").write_text(
            '[tool.ummanu]\nagent-specs = "src/ummanu/automations/agents"\n', encoding="utf-8"
        )

        worktrees = upgrade.desired_role_worktrees(self.product, self.home)

        self.assertEqual(worktrees, [self.home / "orca" / "workspaces" / "ummanu" / "curator"])

    def test_a_configured_workspaces_root_still_wins_over_the_owners_home(self) -> None:
        elsewhere = self.root / "elsewhere"

        with mock.patch.dict(os.environ, {"TA_WORKSPACES_ROOT": str(elsewhere)}):
            roots = upgrade.workspaces_root(self.home)

        self.assertEqual(roots, elsewhere)

    def test_a_configured_bin_dir_still_wins_over_the_owners_home(self) -> None:
        elsewhere = self.root / "elsewhere-bin"

        with mock.patch.dict(os.environ, {role_skills.BIN_DIR_ENV: str(elsewhere)}):
            self.assertEqual(role_skills.bin_dir(self.home), elsewhere)
        self.assertEqual(role_skills.bin_dir(self.home), self.home / "bin")

    def test_an_installation_owned_by_a_missing_account_is_refused_before_any_write(self) -> None:
        with mock.patch("ummanu.host_apply.pwd.getpwnam", side_effect=KeyError("operator")):
            code, output = self.capture(lambda: self.run_upgrade_command())

        self.assertEqual(code, 2)
        self.assertIn("operator", output)
        self.assertFalse((self.home / "shells").exists())
        self.assert_invoker_home_untouched()

    def capture(self, call) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = call()
        return code, output.getvalue()


class ProductRootDefaultTests(PortableFixture):
    """Which checkout install and upgrade materialize when the operator names none.

    A candidate checkout is a normal place to run the command from — that is what upgrading to it
    looks like before it is installed. If the running module decided, the answer would be the
    working directory of whoever typed the command rather than anything the host configured.
    """

    def test_the_configured_checkout_wins_over_the_one_running_the_command(self) -> None:
        with mock.patch.dict(os.environ, {"UMMANU_REPO": str(self.product)}):
            self.assertEqual(upgrade.default_product_root(), self.product)
            self.assertEqual(
                installation._product_root(SimpleNamespace(product_root=None)),
                self.product.resolve(),
            )

    def test_with_nothing_configured_the_default_hangs_off_the_running_users_home(self) -> None:
        self.assertEqual(upgrade.default_product_root(), self.invoker_home / "ummanu")
        self.assertNotEqual(str(upgrade.default_product_root()), RUNNING_CHECKOUT)

    def test_the_default_role_skill_manifest_is_the_configured_checkouts(self) -> None:
        """`role-skills` without `--product-root` is the path the reviewer's finding walks.

        The manifest of the module running the command is never the answer: a candidate checkout
        auditing itself would report the host in sync with a registry it does not run.
        """
        with mock.patch.dict(os.environ, {"UMMANU_REPO": str(self.product)}):
            resolved = role_skills.manifest_path()

        self.assertEqual(resolved, role_skills.product_manifest_path(self.product))
        self.assertNotEqual(resolved, role_skills.MANIFEST)

    def test_a_named_manifest_and_a_named_root_both_outrank_the_configured_checkout(self) -> None:
        named = self.root / "named" / "manifest.toml"
        explicit = self.root / "explicit" / "manifest.toml"
        env = {"UMMANU_REPO": str(self.product), role_skills.MANIFEST_ENV: str(named)}

        with mock.patch.dict(os.environ, env):
            self.assertEqual(role_skills.manifest_path(), named)
            self.assertEqual(role_skills.manifest_path(explicit), explicit)

    def test_an_audit_with_no_named_checkout_reads_the_configured_ones_skills(self) -> None:
        """End to end through the CLI, which is where the default is actually taken."""
        with mock.patch.dict(os.environ, {"UMMANU_REPO": str(self.product)}):
            code, report = self.run_json_cli(
                ["role-skills", "audit", "--instance", str(self.instance), "--json"]
            )

        self.assertEqual(code, 0, report)
        self.assertEqual(
            [source["path"] for source in report["manifests"]],
            [str(role_skills.product_manifest_path(self.product))],
        )
        self.assertEqual(sorted({item["skill"] for item in report["missing"]}), ["portable-skill"])

    def test_an_explicitly_named_checkout_still_wins_over_the_configured_one(self) -> None:
        decoy = self.root / "decoy"
        with mock.patch.dict(os.environ, {"UMMANU_REPO": str(decoy)}):
            seen: list[upgrade.UpgradeContext] = []
            with mock.patch.object(
                upgrade,
                "run_steps",
                side_effect=lambda context: seen.append(context) or upgrade.UpgradeResult(),
            ):
                code = self.run_upgrade_command(dry_run=True)

        self.assertEqual(code, 0)
        self.assertEqual(seen[0].product_root, self.product)


class InstallationOwnedLayersTests(PortableFixture):
    """The same alternate checkout, over an installation that owns heads and skills of its own."""

    def setUp(self) -> None:
        super().setUp()
        self.canon = self.own_a_canon()
        self.overlay = self.own_a_skill()
        # These are installation configuration, not upgrade output.  A recovered
        # installation gets them from its private repository, so make the fixture
        # start in the same clean state rather than asking upgrade to clean up
        # operator-owned untracked files.
        for command in (
            ["git", "-C", str(self.instance), "add", "--", "heads/heads.toml", "skills"],
            ["git", "-C", str(self.instance), "commit", "--quiet", "-m", "instance head and skill config"],
            ["git", "-C", str(self.instance), "push", "--quiet", "origin", "main"],
        ):
            subprocess.run(command, check=True)

    def test_an_upgrade_from_another_checkout_keeps_both_skill_layers(self) -> None:
        result = self.run_upgrade()

        audit = role_skills.audit(
            instance_path=self.instance,
            product_manifest=role_skills.product_manifest_path(self.product),
        )
        self.assertTrue(result.ok, result.render())
        self.assertTrue(self.shell_skill("codex", "portable-skill").is_file())
        self.assertTrue(self.shell_skill("codex", "owned-skill").is_file())
        self.assertEqual(
            [(source["origin"], source["path"]) for source in audit["manifests"]],
            [
                (PRODUCT_ORIGIN, str(role_skills.product_manifest_path(self.product))),
                (INSTANCE_ORIGIN, str(self.overlay)),
            ],
        )

    def test_the_installations_own_canon_wins_over_the_named_checkouts_default(self) -> None:
        result = self.run_upgrade()

        pin = read_source(self.instance)
        self.assertTrue(result.ok, result.render())
        self.assertEqual(pin["canonical"], str(self.canon))
        self.assertEqual(pin["canonical_owner"], INSTANCE_ORIGIN)
        self.assertEqual(pin["product_root"], str(self.product))
        self.assertIn("owned-head", generated_pair(self.instance).snapshot.read_text(encoding="utf-8"))

    def test_the_head_canon_survives_a_second_upgrade_unchanged(self) -> None:
        self.run_upgrade()
        second = self.run_upgrade()

        self.assertTrue(second.ok, second.render())
        self.assertEqual(self.statuses(second)["head-registry"], "unchanged")
        self.assertEqual(self.canon.read_text(encoding="utf-8"), INSTANCE_CANON)


class RefusedBeforeAnyWriteTests(PortableFixture):
    """A registry the operator has to fix stops the run before the first materializing write."""

    def wrote_nothing(self) -> None:
        self.assertFalse(generated_pair(self.instance).snapshot.exists(), "a head snapshot was written")
        self.assertFalse(generated_pair(self.instance).source.exists(), "a pin was written")
        self.assertFalse((self.instance / "heads" / "source.yaml").exists(), "a live-root pin was written")
        self.assertFalse((self.home / "shells").exists(), "a skill was delivered")
        self.assertFalse((self.home / "bin").exists(), "an entry point was linked")
        self.assertFalse((self.data / "host-managed.json").exists(), "the host manifest was written")

    def assert_refused(self, result: upgrade.UpgradeResult, named: Path) -> None:
        failed = [step for step in result.steps if step.failed]
        self.assertEqual([step.name for step in failed], ["registries"], result.render())
        self.assertIn(str(named), failed[0].detail)
        self.assertLessEqual(len(failed[0].detail.splitlines()), 2, failed[0].detail)
        self.wrote_nothing()

    def test_a_malformed_product_manifest_is_named_before_anything_is_materialized(self) -> None:
        manifest = role_skills.product_manifest_path(self.product)
        manifest.write_text("[roles.ummanu\n", encoding="utf-8")

        self.assert_refused(self.run_upgrade(), manifest)

    def test_a_malformed_instance_overlay_is_named_before_anything_is_materialized(self) -> None:
        overlay = self.own_a_skill()
        overlay.write_text("[roles.ummanu]\nskills = [1]\n", encoding="utf-8")

        self.assert_refused(self.run_upgrade(), overlay)

    def test_an_overlay_that_is_a_directory_is_not_a_portable_installation(self) -> None:
        overlay = self.instance / "skills" / "manifest.toml"
        overlay.mkdir(parents=True)

        self.assert_refused(self.run_upgrade(), overlay)

    def test_a_dangling_overlay_link_is_refused_rather_than_read_past(self) -> None:
        overlay = self.instance / "skills" / "manifest.toml"
        overlay.parent.mkdir(parents=True)
        overlay.symlink_to(self.instance / "never-checked-out.toml")

        self.assert_refused(self.run_upgrade(), overlay)

    def test_a_skill_the_named_checkout_does_not_ship_stops_the_delivery(self) -> None:
        """A manifest can be readable and still name a skill that is not beside it.

        Readable is not deliverable, and the difference has to be found in the same step as a
        syntax error: the head snapshot is written two steps before the skills are.
        """
        source = self.product / "skills" / "roles" / "ummanu" / "portable-skill"
        (source / "SKILL.md").unlink()

        self.assert_refused(self.run_upgrade(), source / "SKILL.md")
        self.assertFalse((self.home / "shells").exists())

    def test_an_entry_point_the_registry_does_not_own_is_refused_before_the_snapshot(self) -> None:
        """A command that cannot be linked is a registry fault, not work the sync can do.

        `sync` refuses this one, but by then the snapshot is written. The command bin is also the
        one part of the plan that lives outside the shells, so nothing earlier would have touched
        it and noticed.
        """
        occupied = self.home / "bin" / "portable-skill"
        occupied.parent.mkdir(parents=True)
        occupied.write_text("#!/bin/sh\necho the operator's own\n", encoding="utf-8")

        result = self.run_upgrade()

        failed = [step for step in result.steps if step.failed]
        self.assertEqual([step.name for step in failed], ["registries"], result.render())
        self.assertIn(str(occupied), failed[0].detail)
        self.assertEqual(occupied.read_text(encoding="utf-8"), "#!/bin/sh\necho the operator's own\n")
        self.assertFalse((self.home / "shells").exists())

    def test_a_bad_registry_stops_the_run_before_the_checkout_is_reinstalled(self) -> None:
        """`pip install -e` writes into the version being installed, so it is a materializing step.

        A pulled checkout with a virtualenv and a moved dependency manifest reinstalls itself. Doing
        that and only then refusing the manifest leaves the host part-way onto a version it never
        finished installing, which is the state the `registries` step exists to make impossible.
        """
        venv_python = self.product / ".venv" / "bin" / "python"
        venv_python.parent.mkdir(parents=True)
        marker = self.root / "pip-ran"
        venv_python.write_text(f"#!/bin/sh\nprintf '' > {marker}\nexit 0\n", encoding="utf-8")
        venv_python.chmod(0o755)
        manifest = role_skills.product_manifest_path(self.product)
        manifest.write_text("[roles.ummanu\n", encoding="utf-8")

        result = self.run_upgrade(changed_paths=("pyproject.toml",))

        self.assertEqual([step.name for step in result.steps], ["pull", "registries"], result.render())
        self.assertFalse(marker.exists(), "the checkout was reinstalled before the refusal")
        self.wrote_nothing()

    def test_the_registries_step_runs_before_every_step_that_writes(self) -> None:
        names = [step.__name__ for step in upgrade.STEPS]

        self.assertEqual(names[:2], ["step_pull", "step_registries"])

    def test_a_malformed_instance_canon_is_named_before_the_snapshot_is_written(self) -> None:
        canon = self.own_a_canon()
        canon.write_text("nope = [", encoding="utf-8")

        self.assert_refused(self.run_upgrade(), canon)

    def test_a_dangling_instance_canon_is_named_before_the_snapshot_is_written(self) -> None:
        canon = self.instance / "heads" / "heads.toml"
        canon.parent.mkdir(parents=True)
        canon.symlink_to(self.instance / "heads" / "gone.toml")

        self.assert_refused(self.run_upgrade(), canon)

    def test_a_canon_that_is_a_directory_is_named_before_the_snapshot_is_written(self) -> None:
        canon = self.instance / "heads" / "heads.toml"
        canon.mkdir(parents=True)

        self.assert_refused(self.run_upgrade(), canon)


class ShippedRegistryHomeTests(unittest.TestCase):
    """The product's portable registry ships beside `ummanu.runtime.heads`, and both canons load."""

    ROOT = Path(__file__).resolve().parents[1]

    def test_the_product_fallback_is_the_registry_the_runtime_ships(self) -> None:
        path, owner = canonical_path(self.ROOT)
        self.assertEqual(
            (path, owner), (self.ROOT / "src" / "ummanu" / "runtime" / "heads.toml", PRODUCT_ORIGIN)
        )
        self.assertEqual(path.resolve(), shipped_heads.HEADS_TOML.resolve())
        self.assertTrue(canonical_heads(self.ROOT)["profiles"])

    def test_an_instance_owned_canon_still_wins_and_loads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            owned = instance / "heads" / "heads.toml"
            owned.parent.mkdir()
            owned.write_text(shipped_heads.HEADS_TOML.read_text(encoding="utf-8"), encoding="utf-8")

            self.assertEqual(canonical_path(self.ROOT, instance), (owned, INSTANCE_ORIGIN))
            self.assertEqual(canonical_heads(self.ROOT, instance), canonical_heads(self.ROOT))


if __name__ == "__main__":
    unittest.main()
