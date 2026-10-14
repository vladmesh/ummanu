"""The `po-token` install/upgrade step: `DATA_DIR/po-web-token` created once, 0600, never rewritten."""

from __future__ import annotations

import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ummanu import upgrade
from ummanu.installation.upgrade import types as upgrade_types, workspaces as upgrade_workspaces
from ummanu.po import token as po_token


class PoTokenStepTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.path = self.data / "po-web-token"

    def run_step(self, *, dry_run: bool = False, runtime_user: str | None = None) -> upgrade_types.StepResult:
        context = upgrade_types.UpgradeContext(
            instance_path=self.root / "instance",
            product_root=self.root / "product",
            base_branch="main",
            dry_run=dry_run,
            units=None,
            report=SimpleNamespace(data_dir=self.data),
            runtime_user=runtime_user,
        )
        return upgrade_workspaces.step_po_token(context)

    def test_the_step_creates_a_0600_token_and_a_second_run_changes_nothing(self) -> None:
        first = self.run_step()

        self.assertEqual((first.name, first.status), ("po-token", "changed"), first.detail)
        self.assertEqual(po_token.token_path(self.data), self.path)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        token = po_token.read_token(self.data)
        self.assertGreaterEqual(len(token), 43)
        before = (self.path.read_bytes(), self.path.stat().st_mtime_ns, self.path.stat().st_ino)

        second = self.run_step()

        self.assertEqual(second.status, "unchanged", second.detail)
        self.assertEqual(
            (self.path.read_bytes(), self.path.stat().st_mtime_ns, self.path.stat().st_ino), before
        )
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_a_dry_run_says_it_would_create_and_creates_nothing(self) -> None:
        result = self.run_step(dry_run=True)
        self.assertEqual(result.status, "changed")
        self.assertIn("would create", result.detail)
        self.assertFalse(self.path.exists())

    def test_an_existing_token_is_never_rewritten(self) -> None:
        self.path.write_text("chosen-by-the-owner\n", encoding="utf-8")
        self.path.chmod(0o600)

        result = self.run_step()

        self.assertEqual(result.status, "unchanged")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "chosen-by-the-owner\n")

    def test_a_symlink_in_its_place_is_not_written_through(self) -> None:
        target = self.root / "elsewhere"
        target.write_text("not a token\n", encoding="utf-8")
        self.path.symlink_to(target)

        self.assertEqual(self.run_step().status, "unchanged")
        self.assertEqual(target.read_text(encoding="utf-8"), "not a token\n")
        with self.assertRaises(po_token.TokenError):
            po_token.read_token(self.data)

    def test_rotation_is_deleting_the_file_and_running_the_step_again(self) -> None:
        self.run_step()
        old = po_token.read_token(self.data)
        self.path.unlink()

        self.assertEqual(self.run_step().status, "changed")
        self.assertNotEqual(po_token.read_token(self.data), old)

    def test_a_root_invoker_hands_the_token_to_the_runtime_user(self) -> None:
        account = SimpleNamespace(pw_uid=4321, pw_gid=4321)
        with (
            mock.patch('ummanu.installation.upgrade.workspaces.os.geteuid', return_value=0),
            mock.patch('ummanu.installation.upgrade.workspaces.pwd.getpwnam', return_value=account),
            mock.patch('ummanu.installation.upgrade.workspaces.os.chown') as chown,
        ):
            result = self.run_step(runtime_user="po-runtime")

        self.assertFalse(result.failed, result.detail)
        chown.assert_called_once_with(self.path, 4321, 4321, follow_symlinks=False)

    def test_the_step_runs_after_the_po_workspace_is_handed_over(self) -> None:
        names = [step.__name__ for step in upgrade.STEPS]
        self.assertEqual(names.index("step_po_token"), names.index("step_po_workspace_owner") + 1)

    def test_the_token_lives_outside_the_po_workspace(self) -> None:
        from ummanu.po.workspace import workspace_dir

        self.assertNotIn(workspace_dir(self.data), po_token.token_path(self.data).parents)


if __name__ == "__main__":
    unittest.main()
