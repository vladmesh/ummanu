"""A wait-watchdog respawn tells the successor which command was interrupted (secretary-1692).

The successor's TASK.md carries one factual line taken from the stopped run's own vitality episode
when that episode kept a child reading, and nothing new when it did not.
"""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from tests.dispatcher_fixtures import DispatcherRuntimeFixture
from ummanu.runtime.head.children import read_head_children


def _stop_group(head: subprocess.Popen) -> None:
    """Kill a stand-in head's whole process group, then reap the head (secretary-1695)."""
    try:
        os.killpg(head.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    head.wait(timeout=10)


class RespawnNamesTheInterruptedCommandTests(DispatcherRuntimeFixture, unittest.TestCase):
    """The successor's TASK.md carries the one factual line, and only when there is one."""

    def _stall_to_the_respawn(self, *, command: str) -> dict:
        self._open_the_second_round()
        self._head_at_its_prompt()
        self.tick()
        self._rewind_idle()
        self.tick()  # the round's one prompt
        self._rewind_idle()
        if command:
            payload = self.runtime.production_state.load()
            episode = payload["records"]["ummanu-510"]["worker_vitality_episode"]
            episode["last_child_key"] = "4321.99"
            episode["last_child_command"] = command
            episode["last_child_output"] = "/tmp/shards.log"
            self.runtime.production_state.save(payload)
        outcome = self.tick()
        self.assertEqual(outcome["action"], "worker-respawned")
        return outcome

    def _task_doc(self) -> str:
        payload = self.runtime.production_state.load()
        workspace = payload["records"]["ummanu-510"]["workspace"]
        return (Path(workspace) / "TASK.md").read_text(encoding="utf-8")

    def test_the_successor_is_told_which_command_was_interrupted(self) -> None:
        self._stall_to_the_respawn(command="timeout 580 python -m pytest tests/integration -k shard1")
        document = self._task_doc()
        self.assertIn("## Interrupted command", document)
        self.assertIn(
            "The previous head was stopped while running: timeout 580 python -m pytest "
            "tests/integration -k shard1 (its output was redirected to /tmp/shards.log)",
            document,
        )
        # Transient: nothing about it lands on the durable record.
        payload = self.runtime.production_state.load()
        self.assertNotIn("respawn_interrupted_command", payload["records"]["ummanu-510"])

    def _respawn_over_a_real_sleep_child(self, stdout: Any) -> str:
        """The wait tick's own child readings of a live ``sleep 3600`` (never above the noise
        floor) drive the respawn; answers the successor's TASK.md. ``stdout`` is the child's, set
        explicitly so the runner's own stdout never becomes its output file (secretary-1694)."""
        if not Path("/proc/self/stat").exists():
            self.skipTest("needs Linux /proc")
        head = subprocess.Popen(
            ["sh", "-c", "sleep 3600; true"],
            stdout=stdout,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.addCleanup(_stop_group, head)
        deadline = time.time() + 10
        while time.time() < deadline and not read_head_children(head.pid).get("descendants"):
            time.sleep(0.05)
        real_status = self.host.worker_status

        def with_children(task, record):
            status = real_status(task, record)
            status["child_activity"] = read_head_children(head.pid)
            return status

        self.host.worker_status = with_children  # type: ignore[method-assign]
        self._stall_to_the_respawn(command="")
        return self._task_doc()

    def test_a_real_quiet_sleep_child_is_named_to_the_successor(self) -> None:
        """Round 2 reproduction, stdout discarded: the command alone, nothing injected."""
        document = self._respawn_over_a_real_sleep_child(subprocess.DEVNULL)
        self.assertIn("The previous head was stopped while running: sleep 3600\n", document)
        self.assertNotIn("redirected to", document)

    def test_a_real_sleep_childs_output_file_is_named_to_the_successor(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        log = Path(tmp.name) / "sleep.log"
        with log.open("w", encoding="utf-8") as stdout:
            document = self._respawn_over_a_real_sleep_child(stdout)
        self.assertIn(
            f"The previous head was stopped while running: sleep 3600 (its output was redirected to {log})",
            document,
        )

    def test_no_child_reading_means_nothing_new(self) -> None:
        self._stall_to_the_respawn(command="")
        self.assertNotIn("Interrupted command", self._task_doc())
        self.assertNotIn("stopped while running", self._task_doc())


if __name__ == "__main__":
    unittest.main()
