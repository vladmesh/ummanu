"""Deterministic procfs disappearance and fail-closed scoped cleanup observations."""

from __future__ import annotations

import errno
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ummanu.runtime.head.local_pty.scoped_lifecycle import (
    ScopedHeadLifecycle,
    launch_group_present,
    launch_identity,
)
from ummanu.runtime.head.memory import MemoryScopeError, scope_unit

LIFECYCLE = "ummanu.runtime.head.local_pty.scoped_lifecycle"
BOOT = Path("/proc/sys/kernel/random/boot_id")
LEADER = Path("/proc/123/stat")
MEMBER = Path("/proc/456/stat")
SURVIVOR = Path("/proc/789/stat")
DISAPPEARANCES = (errno.ENOENT, errno.ESRCH)


def stat(group: int = 123, state: str = "S", ticks: int = 42) -> str:
    fields = [state, "1", str(group), *(["0"] * 16), str(ticks)]
    return "123 (fixture with ) parentheses) " + " ".join(fields)


class ProcFixture:
    read_text = staticmethod(Path.read_text)

    def observe(self, reads: dict[Path, str | OSError], entries: tuple[Path, ...] = ()) -> list[Path]:
        """Only process observations are fake; owner persistence uses real temporary files."""
        original = self.read_text
        seen = []

        def read(path: Path, *args, **kwargs):
            if path == BOOT or path in reads:
                seen.append(path)
                value = reads.get(path, "boot")
                if isinstance(value, OSError):
                    raise value
                return value
            if path.parts[:2] == ("/", "proc"):
                raise AssertionError(f"unexpected process observation: {path}")
            return original(path, *args, **kwargs)

        self.enterContext(mock.patch.object(Path, "read_text", read))
        self.enterContext(
            mock.patch.object(
                Path,
                "iterdir",
                lambda path: (
                    iter(entries) if path == Path("/proc") else self.fail(f"unexpected enumeration: {path}")
                ),
            )
        )
        return seen


class ProcessPresenceTests(ProcFixture, unittest.TestCase):
    def test_enumerated_member_esrch_is_absent(self) -> None:
        # OSError's precise errno constructor produces the actual ProcessLookupError.
        vanished = OSError(errno.ESRCH, "No such process", str(MEMBER))
        self.assertIsInstance(vanished, ProcessLookupError)
        seen = self.observe({LEADER: stat(state="Z"), MEMBER: vanished}, (MEMBER.parent,))
        self.assertFalse(launch_group_present(123, "boot:42"))
        self.assertEqual(seen, [BOOT, LEADER, MEMBER])

    def test_absent_leader_still_scans_a_live_descendant(self) -> None:
        for code in DISAPPEARANCES:
            with self.subTest(errno=code):
                seen = self.observe({LEADER: OSError(code, "vanished"), MEMBER: stat()}, (MEMBER.parent,))
                self.assertTrue(launch_group_present(123, "boot:42"))
                self.assertIn(MEMBER, seen)

    def test_vanished_member_does_not_hide_a_later_survivor(self) -> None:
        for code in DISAPPEARANCES:
            with self.subTest(errno=code):
                seen = self.observe(
                    {LEADER: stat(state="Z"), MEMBER: OSError(code, "vanished"), SURVIVOR: stat()},
                    (MEMBER.parent, SURVIVOR.parent),
                )
                self.assertTrue(launch_group_present(123, "boot:42"))
                self.assertEqual(seen[-2:], [MEMBER, SURVIVOR])

    def test_launch_identity_disappearance_is_absent(self) -> None:
        for code in DISAPPEARANCES:
            with self.subTest(errno=code):
                seen = self.observe({LEADER: OSError(code, "vanished")})
                self.assertIsNone(launch_identity(123))
                self.assertEqual(seen, [LEADER])

    def test_boot_mismatch_and_reused_leader_do_not_scan(self) -> None:
        seen = self.observe({LEADER: stat(ticks=43), MEMBER: stat()}, (MEMBER.parent,))
        self.assertFalse(launch_group_present(123, "previous-boot:42"))
        self.assertEqual(seen, [BOOT])
        self.assertFalse(launch_group_present(123, "boot:42"))
        self.assertEqual(seen, [BOOT, BOOT, LEADER])

    def test_dead_and_foreign_members_are_absent(self) -> None:
        self.observe(
            {LEADER: stat(state="Z"), MEMBER: stat(state="X"), SURVIVOR: stat(group=999)},
            (Path("/proc/sys"), LEADER.parent, MEMBER.parent, SURVIVOR.parent),
        )
        self.assertFalse(launch_group_present(123, "boot:42"))
        self.assertIsNone(launch_identity(123))

    def test_live_identity_and_group_are_present(self) -> None:
        self.observe({LEADER: stat()}, (LEADER.parent,))
        self.assertEqual(launch_identity(123), "boot:42")
        self.assertTrue(launch_group_present(123, "boot:42"))

    def test_process_read_errors_are_not_absence(self) -> None:
        for code in (errno.EACCES, errno.EIO):
            for path in (LEADER, MEMBER):
                with self.subTest(errno=code, path=path):
                    error = OSError(code, "unreadable")
                    self.observe({LEADER: stat(state="Z"), path: error}, (MEMBER.parent,))
                    with self.assertRaises(OSError) as caught:
                        launch_group_present(123, "boot:42")
                    self.assertIs(caught.exception, error)
                    if path == LEADER:
                        with self.assertRaises(MemoryScopeError) as wrapped:
                            launch_identity(123)
                        self.assertIs(wrapped.exception.__cause__, error)

    def test_boot_read_policy_is_unchanged(self) -> None:
        for code in (*DISAPPEARANCES, errno.EACCES, errno.EIO):
            with self.subTest(errno=code):
                error = OSError(code, "boot unavailable")
                self.observe({LEADER: stat(), BOOT: error})
                with self.assertRaises(OSError) as caught:
                    launch_group_present(123, "boot:42")
                self.assertIs(caught.exception, error)
                if code == errno.ENOENT:
                    self.assertIsNone(launch_identity(123))
                else:
                    with self.assertRaises(MemoryScopeError) as wrapped:
                        launch_identity(123)
                    self.assertIs(wrapped.exception.__cause__, error)


class CleanupPresenceTests(ProcFixture, unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch(f"{LIFECYCLE}.CGROUP_ROOT", self.root / "cgroups"))
        self.owner = ScopedHeadLifecycle("proc-race", 96)
        self.owner.persist(self.root)
        record = ScopedHeadLifecycle.read_owner(self.root)
        record.update(launch_pid=123, launch_identity="boot:42")
        ScopedHeadLifecycle.update_owner(self.root, record)
        self.membership = (
            self.root / "cgroups" / "system.slice" / scope_unit(self.owner.run_id) / "cgroup.events"
        )
        self.membership.parent.mkdir(parents=True)
        self.membership.write_text("populated 0\n")
        self.kill = self.enterContext(mock.patch(f"{LIFECYCLE}.os.killpg"))
        self.backend = self.enterContext(
            mock.patch(f"{LIFECYCLE}.subprocess.run", side_effect=self.stop_backend)
        )
        self.sleep = self.enterContext(
            mock.patch(f"{LIFECYCLE}.time.sleep", side_effect=AssertionError("real wait"))
        )

    def stop_backend(self, *args, **kwargs):
        record = ScopedHeadLifecycle.read_owner(self.root)
        self.assertFalse(record["launch_allowed"])
        self.assertFalse(record["cleanup_complete"])
        return subprocess.CompletedProcess([], 0, stderr=b"")

    def test_empty_cleanup_settles_after_disappearance_and_membership_proof(self) -> None:
        for code in DISAPPEARANCES:
            with self.subTest(errno=code):
                seen = self.observe(
                    {LEADER: OSError(code, "vanished"), MEMBER: OSError(code, "vanished")}, (MEMBER.parent,)
                )
                self.owner.stop_and_prove_empty()
                record = ScopedHeadLifecycle.read_owner(self.root)
                self.assertFalse(record["launch_allowed"])
                self.assertTrue(record["cleanup_complete"])
                self.assertEqual(seen, [BOOT, LEADER, MEMBER])
        self.assertEqual(self.backend.call_count, 2)
        self.kill.assert_not_called()

    def test_live_descendant_refuses_cleanup_before_cgroup_success(self) -> None:
        self.observe(
            {
                LEADER: OSError(errno.ESRCH, "vanished"),
                MEMBER: OSError(errno.ESRCH, "vanished"),
                SURVIVOR: stat(),
            },
            (MEMBER.parent, SURVIVOR.parent),
        )
        with (
            mock.patch(f"{LIFECYCLE}.time.monotonic", side_effect=[0.0, 6.0]),
            self.assertRaisesRegex(MemoryScopeError, "launch process has not exited"),
        ):
            self.owner.stop_and_prove_empty()
        self.kill.assert_called_once_with(123, 9)
        self.backend.assert_not_called()
        self.assertFalse(ScopedHeadLifecycle.read_owner(self.root)["cleanup_complete"])

    def test_unreadable_process_evidence_refuses_cleanup(self) -> None:
        for code in (errno.EACCES, errno.EIO):
            with self.subTest(errno=code):
                self.observe(
                    {LEADER: OSError(errno.ESRCH, "vanished"), MEMBER: OSError(code, "unreadable")},
                    (MEMBER.parent,),
                )
                with self.assertRaises(MemoryScopeError) as caught:
                    self.owner.stop_and_prove_empty()
                self.assertEqual(caught.exception.__cause__.errno, code)
                record = ScopedHeadLifecycle.read_owner(self.root)
                self.assertFalse(record["cleanup_complete"])
                self.assertFalse(record["launch_allowed"])
        self.kill.assert_not_called()
        self.backend.assert_not_called()

    def test_populated_cgroup_refuses_cleanup_after_process_disappearance(self) -> None:
        self.membership.write_text("populated 1\n")
        self.observe(
            {LEADER: OSError(errno.ESRCH, "vanished"), MEMBER: OSError(errno.ESRCH, "vanished")},
            (MEMBER.parent,),
        )
        with (
            mock.patch(f"{LIFECYCLE}.time.monotonic", side_effect=[0.0, 11.0]),
            self.assertRaisesRegex(MemoryScopeError, "still has members"),
        ):
            self.owner.stop_and_prove_empty()
        self.backend.assert_called_once()
        self.kill.assert_not_called()
        self.assertFalse(ScopedHeadLifecycle.read_owner(self.root)["cleanup_complete"])
