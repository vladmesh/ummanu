"""The durable receipt that makes ``upgrade web unchanged`` a proved statement."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.fakes.upgrade import FakeUnitInstaller
from ummanu.backup_policy import FULL_POLICY, should_skip_data_entry
from ummanu.host_apply import HostCommandError, SystemdUnitInstaller, UnitProcessIdentity
from ummanu.installation.upgrade import (
    host as upgrade_host,
    services as upgrade_services,
    types as upgrade_types,
    workspaces as upgrade_workspaces,
)

WEB_UNIT = "ummanu-web.service"
UNIT_TEXT = b"[Service]\nExecStart=/x --host 127.0.0.1 --port 8787\n"


class WebProcessReceiptTests(unittest.TestCase):
    """Each case has its own checkout, instance, data root and fake service generation."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.product = self.root / "product"
        self.instance = self.root / "instance"
        self.data = self.root / "data"
        self._write_product()
        self._git("init", "--initial-branch=main", "--quiet")
        self._git("add", "-A")
        self._git(
            "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--quiet", "-m", "A"
        )
        self.units = FakeUnitInstaller(present={WEB_UNIT: UNIT_TEXT}, active={WEB_UNIT})
        self.report = SimpleNamespace(
            host={"unit_prefix": "ummanu-"},
            instance={"host": {"unit_prefix": "ummanu-"}, "data_dir": str(self.data)},
            data_dir=self.data,
            bindings=[],
        )
        self.context = upgrade_types.UpgradeContext(
            instance_path=self.instance,
            product_root=self.product,
            base_branch="main",
            dry_run=False,
            units=self.units,
            pull=False,
            report=self.report,
        )
        self.probe = self.enterContext(mock.patch.object(upgrade_services, "probe_web", return_value=200))

    def _write_product(self) -> None:
        for relative, text in {
            "src/ummanu/__init__.py": "",
            "src/ummanu/app.py": "VERSION = 'A'\n",
            "src/ummanu/schemas/contract.json": '{"type":"object"}\n',
            "src/ummanu/automations/__init__.py": "",
            "pyproject.toml": "[project]\nname = 'ummanu'\n",
            "packaging/systemd/ummanu-web.service": UNIT_TEXT.decode(),
            "packaging/systemd/ummanu-web-front.service": "[Unit]\nPartOf=ummanu-web.service\n",
        }.items():
            path = self.product / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def _git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=self.product,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def _advance_checkout(self, relative: str = "src/ummanu/app.py") -> None:
        path = self.product / relative
        path.write_text("VERSION = 'B'\n", encoding="utf-8")
        self._git("add", "-A")
        self._git(
            "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--quiet", "-m", "B"
        )

    def receipt_path(self) -> Path:
        return upgrade_services.web_process_receipt_path(self.context)

    def establish(self) -> None:
        result = upgrade_services.step_web(self.context)
        self.assertEqual(result.status, "changed", result.detail)
        self.assertTrue(self.receipt_path().is_file())

    def test_advanced_checkout_with_no_pull_restarts_instead_of_claiming_the_old_process_is_current(
        self,
    ) -> None:
        self.establish()
        old_identity = self.units.process_identity(WEB_UNIT)
        self._advance_checkout()  # Dispatcher release moved this editable checkout, not this invocation.

        result = upgrade_services.step_web(self.context)

        self.assertEqual(result.status, "changed", result.detail)
        self.assertIn("receipt inputs do not match", result.detail)
        self.assertNotIn("already runs this checkout", result.detail)
        self.assertEqual(self.units.calls, [("restart", WEB_UNIT), ("restart", WEB_UNIT)])
        self.assertNotEqual(self.units.process_identity(WEB_UNIT), old_identity)
        self.assertEqual(self.probe.call_count, 2)

    def test_first_active_upgrade_writes_a_receipt_and_the_exact_repeat_is_a_real_no_op(self) -> None:
        self.establish()
        receipt = self.receipt_path()
        self.assertEqual(stat_mode(receipt), 0o600)
        self.units.calls.clear()
        self.probe.reset_mock()

        result = upgrade_services.step_web(self.context)

        self.assertEqual(result.status, "unchanged", result.detail)
        self.assertIn("web process receipt verified", result.detail)
        self.assertIn("product revision", result.detail)
        self.assertEqual(self.units.calls, [])
        self.probe.assert_not_called()

    def test_malformed_or_partial_receipts_fail_closed(self) -> None:
        for body in ("{", '{"version":1}'):
            with self.subTest(body=body):
                self.receipt_path().parent.mkdir(parents=True, exist_ok=True)
                self.receipt_path().write_text(body, encoding="utf-8")
                self.units.calls.clear()

                result = upgrade_services.step_web(self.context)

                self.assertEqual(result.status, "changed", result.detail)
                self.assertIn("web process receipt is malformed", result.detail)
                self.assertEqual(self.units.calls, [("restart", WEB_UNIT)])
                self.receipt_path().unlink()

    def test_checkout_and_materialized_input_mismatches_restart(self) -> None:
        self.establish()
        self._advance_checkout()

        checkout = upgrade_services.step_web(self.context)
        self.assertEqual(checkout.status, "changed", checkout.detail)
        self.assertIn("receipt inputs do not match", checkout.detail)

        self.units.files[WEB_UNIT] = UNIT_TEXT.replace(b"8787", b"8899")
        unit = upgrade_services.step_web(self.context)
        self.assertEqual(unit.status, "changed", unit.detail)
        self.assertIn("receipt inputs do not match", unit.detail)

        # The installed snapshot the receipt binds: the generated pair in the data directory.
        heads = self.data / "heads" / "heads.yaml"
        heads.parent.mkdir(parents=True, exist_ok=True)
        heads.write_text("profiles: {}\n", encoding="utf-8")
        registry = upgrade_services.step_web(self.context)
        self.assertEqual(registry.status, "changed", registry.detail)
        self.assertIn("receipt inputs do not match", registry.detail)

    def test_reused_pid_with_a_different_start_identity_cannot_authorize_unchanged(self) -> None:
        self.establish()
        prior = self.units.process_identity(WEB_UNIT)
        assert prior is not None
        self.units.identities[WEB_UNIT] = UnitProcessIdentity(
            prior.pid, prior.start_ticks + 1, prior.invocation_id
        )

        result = upgrade_services.step_web(self.context)

        self.assertEqual(result.status, "changed", result.detail)
        self.assertIn("different process generation", result.detail)
        self.assertEqual(self.units.calls, [("restart", WEB_UNIT), ("restart", WEB_UNIT)])

    def test_restart_or_probe_failure_does_not_replace_the_old_receipt(self) -> None:
        self.establish()
        before = self.receipt_path().read_bytes()
        self._advance_checkout()
        self.units.restart = lambda name: (_ for _ in ()).throw(HostCommandError(f"restart {name}: exited 1"))

        restart = upgrade_services.step_web(self.context)

        self.assertEqual(restart.status, "failed")
        self.assertEqual(self.receipt_path().read_bytes(), before)

        self.units.restart = FakeUnitInstaller.restart.__get__(self.units, FakeUnitInstaller)
        self.probe.side_effect = upgrade_services.WebProbeError("no 200")
        probe = upgrade_services.step_web(self.context)

        self.assertEqual(probe.status, "failed")
        self.assertEqual(self.receipt_path().read_bytes(), before)

    def test_dry_run_reports_missing_process_evidence_without_writing_or_restarting(self) -> None:
        dry = replace(self.context, dry_run=True)

        result = upgrade_services.step_web(dry)

        self.assertEqual(result.status, "changed", result.detail)
        self.assertIn("would restart", result.detail)
        self.assertIn("web process receipt is missing", result.detail)
        self.assertFalse(self.receipt_path().exists())
        self.assertEqual(self.units.calls, [])
        self.probe.assert_not_called()

    def test_inactive_or_uninstalled_web_units_do_not_consult_or_create_receipts(self) -> None:
        for units, expected in (
            (FakeUnitInstaller(), "not installed"),
            (FakeUnitInstaller(present={WEB_UNIT: UNIT_TEXT}), "not active"),
        ):
            with self.subTest(expected=expected):
                context = replace(self.context, units=units)
                result = upgrade_services.step_web(context)
                self.assertEqual(result.status, "skipped")
                self.assertIn(expected, result.detail)
                self.assertFalse(upgrade_services.web_process_receipt_path(context).exists())

    def test_receipt_write_is_atomic_and_a_write_failure_leaves_no_partial_evidence(self) -> None:
        with mock.patch.object(upgrade_services.process_receipts.os, "replace", side_effect=OSError("disk full")) as replace:
            result = upgrade_services.step_web(self.context)

        replace.assert_called_once()
        self.assertEqual(result.status, "failed", result.detail)
        self.assertFalse(self.receipt_path().exists())
        self.assertEqual(list(self.receipt_path().parent.glob("*.tmp")), [])

    def test_receipt_is_explicitly_excluded_from_backup(self) -> None:
        self.assertTrue(should_skip_data_entry(Path("web/process-receipt.json"), policy=FULL_POLICY))

    def test_verify_refuses_an_active_web_process_without_valid_receipt_evidence(self) -> None:
        with (
            mock.patch.object(upgrade_services, "step_host", return_value=upgrade_types.StepResult("host", "unchanged")),
            mock.patch.object(upgrade_workspaces.role_skills, "audit", return_value={"ok": True}),
            mock.patch.object(upgrade_services, "assert_snapshot_current"),
            mock.patch.object(upgrade_services, "installed_heads"),
            mock.patch.object(upgrade_host.state_repo, "status", return_value=""),
        ):
            result = upgrade_services.step_verify(self.context)

        self.assertEqual(result.status, "failed", result.detail)
        self.assertIn("active web process receipt is not current", result.detail)


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


class SystemdProcessIdentityTests(unittest.TestCase):
    """The production seam joins systemd's generation to a non-wall-clock kernel identity."""

    def test_native_observation_uses_pid_start_ticks_and_systemd_invocation(self) -> None:
        shown = subprocess.CompletedProcess(
            ["systemctl"], 0, stdout="MainPID=4321\nInvocationID=0123456789abcdef\n", stderr=""
        )
        installer = SystemdUnitInstaller(sudo=False)
        with (
            mock.patch("ummanu.host_apply._proc.run", return_value=shown) as run,
            mock.patch("ummanu.host_apply._process_start_ticks", return_value=987654),
        ):
            identity = installer.process_identity(WEB_UNIT)

        self.assertEqual(identity, UnitProcessIdentity(4321, 987654, "0123456789abcdef"))
        self.assertEqual(run.call_count, 2)
        self.assertIn("--property=MainPID", run.call_args.args[0])
        self.assertIn("--property=InvocationID", run.call_args.args[0])

    def test_native_observation_refuses_a_generation_that_changes_while_it_is_read(self) -> None:
        first = subprocess.CompletedProcess(
            ["systemctl"], 0, stdout="MainPID=4321\nInvocationID=one\n", stderr=""
        )
        second = subprocess.CompletedProcess(
            ["systemctl"], 0, stdout="MainPID=4321\nInvocationID=two\n", stderr=""
        )
        installer = SystemdUnitInstaller(sudo=False)
        with (
            mock.patch("ummanu.host_apply._proc.run", side_effect=(first, second)),
            mock.patch("ummanu.host_apply._process_start_ticks", return_value=987654),
        ):
            identity = installer.process_identity(WEB_UNIT)

        self.assertIsNone(identity)
