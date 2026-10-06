"""A head scope systemd already unloaded is settled by its cgroup, not refused by `systemctl stop`."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ummanu.runtime.head.local_pty.scoped_lifecycle import ScopedHeadLifecycle
from ummanu.runtime.head.memory import MemoryScopeError, scope_unit

LIFECYCLE = "ummanu.runtime.head.local_pty.scoped_lifecycle"


class UnloadedScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch(f"{LIFECYCLE}.CGROUP_ROOT", self.root / "cgroups"))

    def owner(self, run_id: str) -> tuple[ScopedHeadLifecycle, Path, Path]:
        directory = self.root / run_id
        directory.mkdir()
        owner = ScopedHeadLifecycle(run_id, 96)
        owner.persist(directory)
        membership = self.root / "cgroups" / "system.slice" / scope_unit(run_id) / "cgroup.events"
        membership.parent.mkdir(parents=True)
        membership.write_text("populated 1\n")
        return owner, directory, membership

    @staticmethod
    def not_loaded(run_id: str) -> subprocess.CompletedProcess[bytes]:
        unit = scope_unit(run_id)
        return subprocess.CompletedProcess([], 5, stderr=f"Failed to stop {unit}: Unit {unit} not loaded.\n".encode())

    def test_scope_unloaded_before_stop_is_settled_not_refused(self) -> None:
        # The idle head left on its own, systemd unloaded its transient scope, and `systemctl stop`
        # then finds no unit. That is not a refusal: the cgroup proof decides.
        owner, directory, membership = self.owner("unloaded-scope")
        membership.write_text("populated 0\n")
        with mock.patch(f"{LIFECYCLE}.subprocess.run", return_value=self.not_loaded(owner.run_id)):
            owner.stop_and_prove_empty()
        self.assertTrue(ScopedHeadLifecycle.read_owner(directory)["cleanup_complete"])

    def test_scope_not_loaded_with_live_members_still_refuses(self) -> None:
        owner, directory, _ = self.owner("unloaded-but-populated")
        with (
            mock.patch(f"{LIFECYCLE}.subprocess.run", return_value=self.not_loaded(owner.run_id)),
            mock.patch(f"{LIFECYCLE}.time.monotonic", side_effect=[0.0, 11.0]),
            self.assertRaisesRegex(MemoryScopeError, "still has members"),
        ):
            owner.stop_and_prove_empty()
        self.assertFalse(ScopedHeadLifecycle.read_owner(directory)["cleanup_complete"])

    def test_other_stop_failures_still_refuse(self) -> None:
        owner, directory, membership = self.owner("refused")
        membership.write_text("populated 0\n")
        refused = subprocess.CompletedProcess([], 1, stderr=b"Access denied\n")
        with (
            mock.patch(f"{LIFECYCLE}.subprocess.run", return_value=refused),
            self.assertRaisesRegex(MemoryScopeError, "could not stop head scope"),
        ):
            owner.stop_and_prove_empty()
        self.assertFalse(ScopedHeadLifecycle.read_owner(directory)["cleanup_complete"])


if __name__ == "__main__":
    unittest.main()
