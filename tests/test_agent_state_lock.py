"""AgentState.lock(): a dead holder's lock is reclaimed, a live holder's is refused."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from ummanu.runtime import state as state_module
from ummanu.runtime.state import AgentState

SRC = Path(__file__).resolve().parents[1] / "src"

# A child that takes the lock, announces it, and holds it until killed or `hold` seconds pass.
HOLDER = """
import sys, time
from pathlib import Path
from ummanu.runtime.state import AgentState
state_dir, ready, go, hold = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]), float(sys.argv[4])
while go.name != "-" and not go.exists():
    time.sleep(0.01)
with AgentState("curator", state_dir=Path(state_dir)).lock():
    ready.write_text("held")
    print("won", flush=True)
    time.sleep(hold)
"""


def _dead_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def _events(state_dir: Path, name: str) -> list[dict]:
    runs = state_dir / "runs.jsonl"
    if not runs.exists():
        return []
    records = [json.loads(line) for line in runs.read_text(encoding="utf-8").splitlines()]
    return [record for record in records if record["event"] == name]


class AgentStateLockTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.state_dir = self.root / "curator"
        self.state = AgentState("curator", state_dir=self.state_dir)
        self.state.ensure_dir()
        self.env = dict(os.environ, UMMANU_STATE=str(self.root), PYTHONPATH=str(SRC))
        self.children: list[subprocess.Popen] = []
        self.addCleanup(self._reap)

    def _reap(self) -> None:
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.communicate()

    def _sleeper(self) -> subprocess.Popen:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.children.append(child)
        return child

    def _holder(self, ready: Path, go: str = "-", hold: float = 60) -> subprocess.Popen:
        child = subprocess.Popen(
            [sys.executable, "-c", HOLDER, str(self.state_dir), str(ready), go, str(hold)],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.children.append(child)
        return child

    def _wait_for(self, path: Path) -> None:
        deadline = time.monotonic() + 20
        while not path.exists():
            self.assertLess(time.monotonic(), deadline, f"{path} never appeared")
            time.sleep(0.01)

    def _run_body(self) -> list[str]:
        ran: list[str] = []
        with self.state.lock():
            ran.append("body")
            record = json.loads(self.state.lockfile.read_text(encoding="utf-8"))
            self.assertEqual(record["pid"], os.getpid())
        return ran

    def _assert_refused(self, holder: int) -> None:
        with self.assertRaises(SystemExit) as caught, self.state.lock():
            self.fail("body must not run under a live holder")
        self.assertEqual(
            str(caught.exception.code),
            f"curator: another run holds the lock ({self.state.lockfile}, pid {holder})",
        )
        self.assertEqual(_events(self.state_dir, "lock-refused")[-1]["holder_pid"], holder)
        self.assertTrue(self.state.lockfile.exists())

    def test_normal_exit_removes_the_lock_file(self) -> None:
        self.assertEqual(self._run_body(), ["body"])
        self.assertFalse(self.state.lockfile.exists())
        self.assertEqual(_events(self.state_dir, "lock-reclaimed"), [])

    def test_dead_legacy_bare_pid_is_reclaimed(self) -> None:
        stale = _dead_pid()
        self.state.lockfile.write_text(str(stale), encoding="utf-8")
        self.assertEqual(self._run_body(), ["body"])
        (event,) = _events(self.state_dir, "lock-reclaimed")
        self.assertEqual(event["stale_pid"], stale)
        self.assertEqual(event["reclaimer_pid"], os.getpid())
        self.assertIn("lock_age_s", event)
        self.assertFalse(self.state.lockfile.exists())

    def test_dead_new_form_record_is_reclaimed(self) -> None:
        stale = _dead_pid()
        self.state.lockfile.write_text(json.dumps({"pid": stale, "start": 12345, "source": "x"}), encoding="utf-8")
        self.assertEqual(self._run_body(), ["body"])
        (event,) = _events(self.state_dir, "lock-reclaimed")
        self.assertEqual((event["stale_pid"], event["recorded_start"]), (stale, 12345))

    def test_live_holder_is_refused(self) -> None:
        ready = self.root / "ready"
        holder = self._holder(ready)
        self._wait_for(ready)
        self._assert_refused(holder.pid)

    def test_live_legacy_bare_pid_is_refused(self) -> None:
        sleeper = self._sleeper()
        self.state.lockfile.write_text(str(sleeper.pid), encoding="utf-8")
        self._assert_refused(sleeper.pid)

    def test_reused_pid_with_other_start_time_is_reclaimed(self) -> None:
        sleeper = self._sleeper()
        start = state_module._process_start(sleeper.pid)
        self.assertIsNotNone(start)
        assert start is not None
        self.state.lockfile.write_text(json.dumps({"pid": sleeper.pid, "start": start - 1}), encoding="utf-8")
        self.assertEqual(self._run_body(), ["body"])
        self.assertEqual(_events(self.state_dir, "lock-reclaimed")[0]["stale_pid"], sleeper.pid)

    def test_matching_start_time_is_refused(self) -> None:
        sleeper = self._sleeper()
        start = state_module._process_start(sleeper.pid)
        self.state.lockfile.write_text(json.dumps({"pid": sleeper.pid, "start": start}), encoding="utf-8")
        self._assert_refused(sleeper.pid)

    def test_legacy_pid_started_after_the_lock_was_written_is_reclaimed(self) -> None:
        sleeper = self._sleeper()
        self.state.lockfile.write_text(str(sleeper.pid), encoding="utf-8")
        hour_ago = time.time() - 3600
        os.utime(self.state.lockfile, (hour_ago, hour_ago))
        self.assertEqual(self._run_body(), ["body"])
        self.assertEqual(_events(self.state_dir, "lock-reclaimed")[0]["stale_pid"], sleeper.pid)

    def test_sigkilled_holder_does_not_stop_the_next_lock(self) -> None:
        ready = self.root / "ready"
        holder = self._holder(ready)
        self._wait_for(ready)
        holder.send_signal(signal.SIGKILL)
        holder.wait()
        self.assertTrue(self.state.lockfile.exists())
        self.assertEqual(self._run_body(), ["body"])
        self.assertEqual(_events(self.state_dir, "lock-reclaimed")[0]["stale_pid"], holder.pid)

    def test_two_concurrent_reclaimers_of_one_stale_lock_exactly_one_wins(self) -> None:
        self.state.lockfile.write_text(str(_dead_pid()), encoding="utf-8")
        go = self.root / "go"
        contenders = [self._holder(self.root / f"ready-{index}", str(go), hold=3) for index in range(2)]
        go.write_text("go")
        outcomes = [child.communicate(timeout=30) for child in contenders]
        codes = sorted(child.returncode for child in contenders)
        self.assertEqual(codes, [0, 1], outcomes)
        self.assertEqual(sum("won" in stdout for stdout, _ in outcomes), 1)
        self.assertEqual(sum("another run holds the lock" in stderr for _, stderr in outcomes), 1)
        self.assertEqual(len(_events(self.state_dir, "lock-reclaimed")), 1)
        self.assertFalse(self.state.lockfile.exists())


if __name__ == "__main__":
    unittest.main()
