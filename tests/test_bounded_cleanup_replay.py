"""The automatic cleanup replay's one elapsed allowance (ummanu-145).

Disposable Git repositories with the owned-cleanup fixture: the production tick's cleanup phase
through its own entry point, real Git children killed at the bound, real lane and journal locks, the
real local-pty runtime's stop against a stalled supervisor, every changed destructive boundary cut
short and recovered by a reloaded owner, rotation under the allowance and a 319-intent journal.
Wall clock is asserted only against the sprint's phase bound; the rest is printed for the report.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import shutil
import socket
import subprocess
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from tests import test_owned_cleanup as owned_fixtures
from tests.production_runtime_fixtures import registered_production_runtime
from ummanu.dispatch import attempt_accounting, cleanup as cleanup_module, production
from ummanu.dispatch.cleanup import (
    ATTEMPT_FLOOR,
    PUBLICATION_GRACE,
    REPLAY_ALLOWANCE,
    RETRY_COOLDOWN,
    CleanupOwner,
)
from ummanu.dispatch.host import CommandHostRuntime
from ummanu.dispatch.types import HostError
from ummanu.runtime.head import HeadRun
from ummanu.runtime.head.local_pty import protocol
from ummanu.runtime.local_pty_head import LocalPtyHeadRuntime

#: The sprint's bound on the cleanup phase.
PHASE_BOUND = 5.0
COUNTS = ("due", "attempted", "deferred", "skipped", "busy", "lost")
git = owned_fixtures.git
journal_bytes = owned_fixtures.journal_bytes


class BoundedCleanupReplayTests(unittest.TestCase):
    def setUp(self):
        self.fixture = owned_fixtures.OwnedCleanupTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.owner = self.fixture.owner
        self.clock = self.fixture.manual_clock()
        fixture = self.fixture

        def stop(run, initiator, **bound):
            """The fixture's tolerant stop, handed the allowance's `remaining` as the runtime is."""
            self.assertEqual(set(bound), {"remaining"})
            fixture.stops.append((run.run_id, run.scope_generation))
            return SimpleNamespace(ok=not fixture.stop_failure, reason="simulated stop failure",
                                   run=run if run.settled else run.finishing(initiator).exited())
        fixture.backend.stop = stop

    # -- helpers -----------------------------------------------------------------------------------

    def replay(self, owner: CleanupOwner | None = None, allowance: float = REPLAY_ALLOWANCE):
        owner = owner or self.owner
        started = time.monotonic()
        result = owner.replay(limit=5, allowance=allowance)
        return result, time.monotonic() - started

    def counts(self, report: dict[str, Any]) -> dict[str, int]:
        return {name: report[name] for name in COUNTS}

    def spend(self):
        """Spend the running automatic replay's allowance now: its next bounded wait or effect
        defers, while the journal publications recording that keep their grace."""
        allowance = cleanup_module._ALLOWANCE.get()
        self.assertIsNotNone(allowance, "only an automatic replay has an allowance")
        allowance.deadline = allowance.clock()

    def spent_at(self, target, attribute, *, after=0):
        """`target.attribute`, spending the allowance as its call number `after + 1` begins."""
        native = getattr(target, attribute)
        calls = []

        def call(*args, **kwargs):
            calls.append(attribute)
            if len(calls) == after + 1:
                self.spend()
            return native(*args, **kwargs)
        return mock.patch.object(target, attribute, side_effect=call), calls

    def git_shim(self, slow_workspace: Path):
        """`git` on PATH that hangs on `status` of one exact workspace (read through the pinned
        descriptor too). The hang is Git's own pid (`exec`), recorded so the test can prove it gone."""
        real = shutil.which("git")
        bin_dir = self.fixture.root / "shim-bin"
        bin_dir.mkdir()
        self.hung = bin_dir / "hung-pids"
        shim = bin_dir / "git"
        shim.write_text("#!/bin/bash\n"
                        f'if [ "$1" = -C ] && [ "$3" = status ] && [ "$(readlink -f "$2")" = "{slow_workspace.resolve()}" ]; '
                        f"then echo $$ >> {self.hung}; exec sleep 60; fi\n"
                        f'exec {real} "$@"\n')
        shim.chmod(0o755)
        return mock.patch.dict(os.environ, {"PATH": f"{bin_dir}:{os.environ['PATH']}"})

    def no_hung_git(self):
        """Every hung Git child was killed and reaped by the replay that started it."""
        pids = [int(line) for line in self.hung.read_text().split()]
        self.assertTrue(pids)
        for pid in pids:
            with self.assertRaises(ProcessLookupError, msg=f"Git child {pid} outlived the replay"):
                os.kill(pid, 0)

    def production_cleanup_phase(self):
        """The production tick's cleanup phase through its own entry point, with the real-mode host,
        its Git children and the tick's telemetry. With no board snapshot the tick ends right after
        the phase, as a failed fence does, and folds its entry."""
        fixture = self.fixture
        host = getattr(self, "host", None)
        if host is None:
            host = self.host = CommandHostRuntime(fixture.catalog, fixture.data, mode="real",
                                                  production_runtime=registered_production_runtime(fixture.root))
        host.cleanup_owner = self.owner
        fixture.runtime.host, fixture.runtime.cleanup, fixture.runtime.owner = host, self.owner, "test-owner"
        fixture.runtime.production_state = SimpleNamespace(save=lambda payload: None)
        payload: dict[str, Any] = {}
        with production.tick_clock(), \
                mock.patch.object(attempt_accounting, "publish_pending_attempt_usage", return_value=[]), \
                mock.patch.object(attempt_accounting, "publish_pending_attempt_outcomes", return_value=[]), \
                mock.patch.object(host, "head_runtime_for", return_value=fixture.backend):
            started = time.monotonic()
            result = production._production_tick_with_snapshot(fixture.runtime, payload, {}, {}, None)
            elapsed = time.monotonic() - started
        self.assertEqual(result["action"], "observer-fence-unavailable")
        entry = payload["tick_telemetry"]["last"]
        # The phase is measured by the tick itself, selection and final publication included.
        self.assertLess(entry["phases"]["cleanup"], PHASE_BOUND * 1000)
        self.assertLess(elapsed, PHASE_BOUND)
        return entry, result, elapsed

    def assert_deferred_then_recovered(self, key: str, boundary: str) -> dict[str, Any]:
        """The cut-short attempt: pending with its reason, cooling for every caller and every reload,
        never a terminal or verified outcome; an hour on, a reloaded owner completes it."""
        fixture = self.fixture
        intent = self.owner.journal.intent(key)
        self.assertEqual(intent["status"], "pending", intent["reason"])
        self.assertIn("allowance exhausted at " + boundary, intent["reason"])
        self.assertEqual(self.owner.last_replay["deferred"], 1, self.owner.last_replay)
        self.assertNotIn("terminal", intent["progress"])
        self.assertFalse(intent["progress"].get("preservation_verified"))
        self.assertEqual(intent["retry"], {"last_attempt_at": self.clock.now,
                                           "next_attempt_at": self.clock.now + RETRY_COOLDOWN})
        self.assertEqual(fixture.task["claim"]["worker"], fixture.record.worker)
        stored = journal_bytes(self.owner.journal)
        self.clock.advance(RETRY_COOLDOWN - 1)
        self.assertEqual(self.replay()[0], [])
        self.assertEqual(self.replay(CleanupOwner(fixture.runtime))[0], [])
        self.assertEqual(CleanupOwner(fixture.runtime).replay_one(key)["status"], "pending")
        self.assertEqual(CleanupOwner(fixture.runtime).replay(limit=5), [])
        self.assertEqual(journal_bytes(self.owner.journal), stored)
        self.clock.advance(1)
        result, _ = self.replay(CleanupOwner(fixture.runtime))
        self.assertEqual([item["status"] for item in result], ["completed"], result and result[0]["reason"])
        self.assertFalse(fixture.workspace.exists())
        self.assertNotIn(str(fixture.workspace), git(fixture.repo, "worktree", "list", "--porcelain"))
        self.assertEqual(git(fixture.repo, "for-each-ref", "refs/heads/pipeline/"), "")
        self.assertIsNone(fixture.task["claim"]["worker"])
        return result[0]

    def due_card(self):
        fixture = self.fixture
        fixture.head()
        fixture.task["claim"]["worker"] = fixture.record.worker
        return fixture.request()

    # -- the production entry point ----------------------------------------------------------------

    def test_production_phase_bounds_a_slow_first_intent_and_carries_the_rest(self):
        fixture = self.fixture
        cards = sorted((fixture.card(f"due-{n}", head=False) for n in range(6)), key=lambda card: card[0])
        slow_key, slow_workspace, _ = cards[0]
        stored = journal_bytes(self.owner.journal)
        with self.git_shim(slow_workspace):
            entry, _, elapsed = self.production_cleanup_phase()
        self.no_hung_git()
        report = entry["cleanup"]
        # One allowance for the phase: the first intent's Git child was killed at it, and no other
        # due intent was admitted after it.
        self.assertLess(elapsed, REPLAY_ALLOWANCE + PUBLICATION_GRACE + 0.5)
        self.assertEqual(self.counts(report), {"due": 6, "attempted": 1, "deferred": 1, "skipped": 5,
                                               "busy": 0, "lost": 0})
        self.assertEqual(report["allowance_ms"], REPLAY_ALLOWANCE * 1000)
        self.assertIn("Git status", report["deferred_at"])
        slow = self.owner.journal.intent(slow_key)
        self.assertEqual(slow["status"], "pending")
        self.assertIn("allowance exhausted at Git status", slow["reason"])
        self.assertEqual(slow["retry"]["next_attempt_at"], self.clock.now + RETRY_COOLDOWN)
        self.assertTrue(slow["progress"]["heads_stopped"])
        self.assertNotIn("removal_started", slow["progress"])
        self.assertTrue(slow_workspace.exists())
        # The skipped intents were never reserved: byte-identical, still due, no cooldown consumed.
        after = journal_bytes(self.owner.journal)
        for key, _, _ in cards[1:]:
            self.assertEqual(after["intents/" + key + ".json"], stored["intents/" + key + ".json"])
        # Deferred writes: the reservation, the heads-stopped checkpoint and the outcome, and the
        # cursor once; no generated digest. The tick's own counters saw the same publications.
        self.assertEqual(report["writes"], {"intent": 3, "meta": 1, "generated": 0, "bytes": report["writes"]["bytes"]})
        self.assertEqual(entry["counters"]["cleanup_intent_writes"], report["writes"]["intent"])
        self.assertEqual(entry["counters"]["cleanup_bytes_written"], report["writes"]["bytes"])
        ticks = [{"elapsed_s": round(elapsed, 3), **report}]
        # Later ticks reach the skipped intents first (the cursor), each within its own allowance; the
        # deferred one is cooling and is not attempted again before its due time.
        refs = {key: self.owner.journal.intent(key)["task"]["ref"] for key, _, _ in cards}
        completed: set[str] = set()
        while len(completed) < 5:
            self.assertLess(len(ticks), 8, ticks)
            self.clock.advance(60)
            entry, _, elapsed = self.production_cleanup_phase()
            ticks.append({"elapsed_s": round(elapsed, 3), **entry["cleanup"]})
            self.assertGreater(entry["cleanup"]["attempted"], 0, entry["cleanup"])
            if len(ticks) == 2:
                self.assertEqual(self.owner.journal.intent(cards[1][0])["status"], "completed")
            completed = {row["ref"] for row in self.owner.journal.summary() if row["status"] == "completed"}
        self.assertEqual(completed, {refs[key] for key, _, _ in cards[1:]})
        self.assertEqual(self.owner.journal.intent(slow_key)["status"], "pending")
        # An hour on, without the hang, it completes from its durable progress.
        self.clock.advance(RETRY_COOLDOWN)
        entry, _, elapsed = self.production_cleanup_phase()
        ticks.append({"elapsed_s": round(elapsed, 3), **entry["cleanup"]})
        self.assertEqual(self.owner.journal.intent(slow_key)["status"], "completed")
        self.assertFalse(slow_workspace.exists())
        print("production cleanup phase ticks: " + json.dumps(ticks, sort_keys=True))

    # -- waits: lanes, the journal lock and a stalled head ------------------------------------------

    def hold(self, enter):
        """Hold a lock from another thread until the test ends."""
        entered, release = threading.Event(), threading.Event()

        def run():
            with enter():
                entered.set()
                release.wait(30)
        thread = threading.Thread(target=run)
        thread.start()

        def released():
            release.set()
            thread.join(5)
        self.addCleanup(released)
        self.assertTrue(entered.wait(5))
        return released

    def test_a_busy_lifecycle_lane_is_skipped_without_a_wait_or_a_cooldown(self):
        fixture = self.fixture
        keys = sorted(fixture.card(f"lane-{n}", head=False)[0] for n in range(3))
        busy = self.owner.journal.intent(keys[0])["task"]["ref"]
        release = self.hold(lambda: cleanup_module.reference_lock(fixture.data, busy, lane="lifecycle"))
        stored = journal_bytes(self.owner.journal)
        result, elapsed = self.replay()
        self.assertLess(elapsed, 2.0)
        self.assertEqual([item["status"] for item in result], ["completed", "completed"])
        self.assertEqual(self.counts(self.owner.last_replay),
                         {"due": 3, "attempted": 2, "deferred": 0, "skipped": 0, "busy": 1, "lost": 0})
        path = "intents/" + keys[0] + ".json"
        self.assertEqual(journal_bytes(self.owner.journal)[path], stored[path])
        release()
        # The cursor passed it; the next invocation wraps around to it.
        self.clock.advance(60)
        self.assertEqual([item["task"]["ref"] for item in self.replay()[0]], [busy])

    def test_a_held_journal_lock_gets_only_the_allowance_and_reserves_nothing(self):
        fixture = self.fixture
        fixture.card("journal-0", head=False)
        self.hold(lambda: cleanup_module.ownership_lock(fixture.data))
        stored = journal_bytes(self.owner.journal)
        result, elapsed = self.replay(allowance=1.0)
        self.assertEqual(result, [])
        self.assertGreaterEqual(elapsed, 1.0)
        self.assertLess(elapsed, 1.0 + PUBLICATION_GRACE + 0.5)
        self.assertIn("cleanup.lock", self.owner.last_replay["deferred_at"])
        self.assertEqual(journal_bytes(self.owner.journal), stored)

    def stalled_supervisor(self, run: HeadRun) -> LocalPtyHeadRuntime:
        """A real local-pty runtime whose supervisor accepts, answers half a frame and stalls, and
        whose head's launch identity stays live until `self.head_alive` is cleared."""
        root = self.fixture.data / "heads"
        run_dir = protocol.run_dir_for(root, run.run_id)
        run_dir.mkdir(parents=True)
        (run_dir / protocol.PID_FILE_NAME).write_text("{}")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(protocol.socket_path_for(run_dir)))
        server.listen()
        stop = threading.Event()

        def serve():
            server.settimeout(0.2)
            while not stop.is_set():
                try:
                    connection, _ = server.accept()
                except TimeoutError:
                    continue
                with connection:
                    connection.recv(65536)
                    connection.sendall(b'{"ok": tr')
                    stop.wait(30)
        thread = threading.Thread(target=serve)
        thread.start()

        def close():
            stop.set()
            thread.join(5)
            server.close()
        self.addCleanup(close)
        self.head_alive = True
        self.close_supervisor = close
        return LocalPtyHeadRuntime(root, head_process_status=lambda *args, **kwargs: {
            "state": "live-match" if self.head_alive else "dead"})

    def test_a_stalled_supervisor_and_a_live_head_get_only_the_allowance(self):
        fixture = self.fixture
        run = fixture.head(generation="")
        fixture.task["claim"]["worker"] = fixture.record.worker
        key = fixture.request()
        fixture.backend = self.stalled_supervisor(run)
        result, elapsed = self.replay(allowance=1.5)
        # Without the allowance this stop could wait its 5 s connect, the framed exchange and its
        # 10 s exit confirmation; with it the whole replay ends within the allowance and its grace.
        self.assertLess(elapsed, 1.5 + PUBLICATION_GRACE + 0.5)
        self.assertEqual([item["status"] for item in result], ["pending"])
        self.assertIn("allowance exhausted at head stop", result[0]["reason"])
        self.assertIn("deadline passed", result[0]["reason"])
        self.assertFalse(result[0]["progress"]["heads_stopped"])
        self.assertTrue(fixture.workspace.exists())
        # The head and its supervisor exit; an hour on, a reloaded owner's stop confirms it and settles.
        self.head_alive = False
        self.close_supervisor()
        self.assertEqual(self.assert_deferred_then_recovered(key, "head stop")["status"], "completed")

    # -- every changed destructive boundary, cut short and recovered --------------------------------

    def test_pre_effect_exhaustion_before_the_head_stop(self):
        key = self.due_card()
        patch, _ = self.spent_at(self.owner, "_scope_fence")
        with patch:
            self.replay()
        intent = self.owner.journal.intent(key)
        self.assertFalse(intent["progress"]["heads_stopped"])
        self.assertEqual(self.fixture.stops, [])
        self.assert_deferred_then_recovered(key, "head stop")

    def test_exhaustion_after_the_stop_before_any_git_effect(self):
        key = self.due_card()
        patch, _ = self.spent_at(self.owner, "_binding")
        with patch:
            self.replay()
        intent = self.owner.journal.intent(key)
        self.assertTrue(intent["progress"]["heads_stopped"])
        self.assertNotIn("removal_started", intent["progress"])
        self.assert_deferred_then_recovered(key, "Git rev-parse")

    def test_ignored_cache_removal_cut_short_resumes_from_a_full_proof(self):
        fixture = self.fixture
        key = self.due_card()
        external = fixture.realistic_venv(fixture.workspace, 8)
        targets = fixture.snapshot(external)
        before = len(fixture.ignored_rows())
        patch, calls = self.spent_at(cleanup_module, "_unlink_confined", after=100)
        with patch:
            self.replay()
        self.assertEqual(len(calls), 256)
        intent = self.owner.journal.intent(key)
        self.assertNotIn("removal_started", intent["progress"])
        self.assertEqual(len(fixture.ignored_rows()), before - 256)
        self.assertIn(str(fixture.workspace), git(fixture.repo, "worktree", "list", "--porcelain"))
        self.assert_deferred_then_recovered(key, "ignored cache removal")
        self.assertEqual(fixture.snapshot(external), targets)

    def test_environment_removal_cut_short_resumes_from_its_retained_identity(self):
        fixture = self.fixture
        namespace = fixture.workspace / ".ummanu-task-env"
        (namespace / "venv").mkdir(parents=True)
        (namespace / "owner.json").write_text("dispatcher-owned")
        for n in range(40):
            (namespace / "venv" / f"file-{n}").write_text("generated")
        (fixture.repo / ".git" / "info" / "exclude").write_text(".ummanu-task-env/\n")

        def ownership(path):
            root = Path(path) / ".ummanu-task-env"
            if not root.exists():
                return "absent"
            if not (root / "owner.json").exists():
                raise HostError("environment owner unavailable")
            return "dispatcher"
        fixture.host._decide_workspace_environment_ownership = ownership
        key = self.due_card()
        patch, _ = self.spent_at(cleanup_module, "_clear_directory", after=1)
        with patch:
            self.replay()
        intent = self.owner.journal.intent(key)
        self.assertTrue(intent["progress"]["environment_removal_started"])
        self.assertEqual(intent["generated_environment"]["owner"], "ummanu-dispatcher")
        self.assertNotIn("removal_started", intent["progress"])
        self.assertTrue((namespace / "venv").is_dir())
        self.assert_deferred_then_recovered(key, "environment namespace removal")

    def test_no_git_removal_starts_in_the_allowances_last_moments(self):
        key = self.due_card()
        patch, calls = self.spent_at(self.owner.journal, "generated_digests", after=2)
        with patch:
            self.replay()
        self.assertEqual(len(calls), 3)
        intent = self.owner.journal.intent(key)
        self.assertNotIn("removal_started", intent["progress"])
        self.assertTrue(self.fixture.workspace.exists())
        self.assert_deferred_then_recovered(key, "Git worktree removal admission")

    def killed_removal(self, effect):
        """The host's Git child for `worktree remove`, killed at the bound after `effect` (once)."""
        fixture = self.fixture
        killed = []

        def capture(args, label, *, timeout=None):
            self.assertIsNotNone(timeout, "an automatic replay's host Git child is bounded")
            self.assertLessEqual(timeout, REPLAY_ALLOWANCE)
            if args[3:5] == ["worktree", "remove"] and not killed:
                killed.append(args)
                effect()
                self.spend()
                raise HostError(label + " failed: timed out")
            return subprocess.run(args, capture_output=True, text=True, check=False, timeout=timeout)
        fixture.host.run_capture = capture
        return killed

    def test_git_removal_killed_before_its_effect_rechecks_identity_and_finishes(self):
        key = self.due_card()
        killed = self.killed_removal(lambda: None)
        self.replay()
        self.assertEqual(len(killed), 1)
        intent = self.owner.journal.intent(key)
        self.assertTrue(intent["progress"]["removal_started"])
        self.assertNotIn("workspace_removed", intent["progress"])
        self.assertTrue(self.fixture.workspace.exists())
        self.assert_deferred_then_recovered(key, "Git worktree")

    def test_git_removal_killed_after_the_directory_finishes_from_the_admitted_registration(self):
        fixture = self.fixture
        key = self.due_card()
        killed = self.killed_removal(lambda: shutil.rmtree(fixture.workspace))
        self.replay()
        self.assertEqual(len(killed), 1)
        intent = self.owner.journal.intent(key)
        self.assertTrue(intent["progress"]["removal_started"])
        self.assertFalse(fixture.workspace.exists())
        self.assertIn(str(fixture.workspace), git(fixture.repo, "worktree", "list", "--porcelain"))
        self.assert_deferred_then_recovered(key, "Git worktree")

    def test_exhaustion_between_removal_and_ref_deletion_keeps_the_ref_and_claim(self):
        fixture = self.fixture
        key = self.due_card()
        patch, calls = self.spent_at(self.owner, "_validate_owner", after=2)
        with patch:
            self.replay()
        self.assertEqual(len(calls), 3)
        intent = self.owner.journal.intent(key)
        self.assertTrue(intent["progress"]["workspace_removed"])
        self.assertTrue(intent["progress"]["ref_started"])
        self.assertNotIn("ref_delete_admitted", intent["progress"])
        self.assertEqual(git(fixture.repo, "rev-parse", "refs/heads/pipeline/sample-1"), fixture.base)
        self.assert_deferred_then_recovered(key, "Git rev-parse")

    def test_exhaustion_never_turns_a_disappeared_workspace_terminal_without_its_witness(self):
        """A dead end needs the retention witness read in that attempt; cut short before it, the
        obligation stays pending with its claim, and only a later full attempt ends it terminally."""
        fixture = self.fixture
        key = self.due_card()
        git(fixture.repo, "worktree", "remove", str(fixture.workspace))
        patch, calls = self.spent_at(self.owner, "_retention_witness")
        with patch:
            result, _ = self.replay()
        self.assertEqual(len(calls), 1)
        self.assertEqual(result[0]["status"], "pending", result[0]["reason"])
        self.assertIn("allowance exhausted at Git for-each-ref", result[0]["reason"])
        self.assertNotIn("terminal", result[0]["progress"])
        self.assertNotIn("claim_settled", result[0]["progress"])
        self.assertNotIn("commit_proof", self.owner.journal.intent(key))
        self.assertEqual(fixture.task["claim"]["worker"], fixture.record.worker)
        self.clock.advance(RETRY_COOLDOWN)
        result, _ = self.replay(CleanupOwner(fixture.runtime))
        fixture.assert_terminal(result[0], "workspace-disappeared")

    # -- rotation, fairness and write counts -----------------------------------------------------------

    def test_bounded_invocations_rotate_through_many_due_intents_a_slow_head_and_fresh_work(self):
        fixture = self.fixture
        elapsed = [0.0]
        fixture.runtime.cleanup_monotonic = lambda: elapsed[0]
        self.owner.monotonic = fixture.runtime.cleanup_monotonic
        cards = sorted((fixture.card(f"fair-{n:02d}") for n in range(12)), key=lambda card: card[0])
        slow = HeadRun.from_json(cards[4][2].worker_head_run).run_id
        attempts: list[str] = []

        def stop(run, initiator, **bound):
            self.assertIn("remaining", bound)
            attempts.append(run.run_id)
            if run.run_id == slow:
                elapsed[0] += bound["remaining"]()  # the runtime waits out all that is left
                return SimpleNamespace(ok=False, reason="head still running", run=run)
            elapsed[0] += 0.9  # an ordinary attempt costs 0.9 s of the allowance
            return SimpleNamespace(ok=True, reason="", run=run if run.settled else run.finishing(initiator).exited())
        fixture.backend.stop = stop
        reports = []
        while len(set(attempts)) < 12:
            self.clock.advance(60)
            before = dict(self.owner.journal.writes)
            self.owner.replay(limit=5, allowance=REPLAY_ALLOWANCE)
            report = self.owner.last_replay
            reports.append(self.counts(report))
            # Never more than the floor admits; a skipped intent is never written.
            self.assertLessEqual(report["attempted"], math.floor((REPLAY_ALLOWANCE - ATTEMPT_FLOOR) / 0.9) + 1)
            self.assertEqual(report["writes"]["meta"], 1 if report["attempted"] else 0)
            self.assertEqual(self.owner.journal.writes["meta"] - before["meta"], report["writes"]["meta"])
            self.assertLessEqual(len(reports), 6)
        # Each due intent once, in rotation order, before any is repeated.
        order = [HeadRun.from_json(card[2].worker_head_run).run_id for card in cards]
        self.assertEqual(attempts, order)
        self.assertEqual(self.owner.journal.intent(cards[4][0])["status"], "pending")
        # Fresh work arrives: the next invocation reaches it, though the slow head is still there.
        key, _, record = fixture.card("fair-fresh")
        self.clock.advance(60)
        self.owner.replay(limit=5, allowance=REPLAY_ALLOWANCE)
        self.assertEqual(attempts[-1], HeadRun.from_json(record.worker_head_run).run_id)
        self.assertEqual(self.owner.journal.intent(key)["status"], "completed")
        # The slow head is attempted once an hour, not once per invocation.
        due = self.owner.journal.intent(cards[4][0])["retry"]["next_attempt_at"]
        while self.clock.now + 60 < due:
            self.clock.advance(60)
            self.owner.replay(limit=5, allowance=REPLAY_ALLOWANCE)
        self.assertEqual(attempts.count(slow), 1)
        self.clock.now = due
        self.owner.replay(limit=5, allowance=REPLAY_ALLOWANCE)
        self.assertEqual(attempts.count(slow), 2)
        print("rotation under the allowance: " + json.dumps(reports))

    def test_representative_journal_selection_and_idle_busy_deferred_writes(self):
        """319 intents shaped like the live journal: selection parses only replaced files, idle
        invocations write nothing, and busy and deferred invocations write what they attempted."""
        fixture = self.fixture
        fixture.head()
        template_key = fixture.request()
        template = self.owner.journal.intent(template_key)
        template["heads"] = [{**template["heads"][0], "run_id": f"run-{n}"} for n in range(12)]
        shapes = (("preserved", "workspace-disappeared", 91), ("completed", None, 116), ("owned", None, 17),
                  ("preserved", None, 94))
        value: dict[str, Any] = {"intents": {}}
        n = 0
        for status, terminal, count in shapes:
            for _ in range(count):
                intent = copy.deepcopy(template)
                intent["task"] = {**intent["task"], "ref": f"clone-{n}", "id": f"task-clone-{n}"}
                intent["status"] = status
                if terminal:
                    intent["progress"]["terminal"] = {"kind": terminal, "reason": "fixture"}
                # Open obligations attempted within the hour: cooling.
                intent["retry"] = {"last_attempt_at": self.clock.now - 60,
                                   "next_attempt_at": self.clock.now + RETRY_COOLDOWN - 60}
                value["intents"][hashlib.sha256(f"clone-{n}".encode()).hexdigest()] = intent
                n += 1
        template["retry"] = {"last_attempt_at": self.clock.now, "next_attempt_at": self.clock.now + RETRY_COOLDOWN}
        value["intents"][template_key] = template
        self.owner.journal.save(value)
        self.assertEqual(len(list((self.owner.journal.path / "intents").glob("*.json"))), 319)
        sizes = sorted(path.stat().st_size for path in (self.owner.journal.path / "intents").glob("*.json"))
        loads = []
        native = cleanup_module.CleanupJournal._load

        def load(path):
            loads.append(path)
            return native(path)
        measured: dict[str, Any] = {"median_intent_bytes": sizes[len(sizes) // 2], "intents": len(sizes)}
        with mock.patch.object(cleanup_module.CleanupJournal, "_load", staticmethod(load)):
            started = time.perf_counter()
            self.owner.journal.read()
            measured["full_read_s"] = round(time.perf_counter() - started, 4)
            owner = CleanupOwner(fixture.runtime)
            # Idle: 90 invocations a second apart, the first one cold (a reload).
            writes = dict(owner.journal.writes)
            idle = []
            for _ in range(90):
                self.clock.advance(1)
                del loads[:]
                result, elapsed = self.replay(owner)
                self.assertEqual(result, [])
                idle.append((elapsed, len(loads)))
            self.assertEqual({name: owner.journal.writes[name] - writes[name] for name in writes},
                             {"intent": 0, "meta": 0, "generated": 0, "bytes": 0})
            self.assertEqual(idle[0][1], 319 + 1)  # every intent once, and the meta
            self.assertTrue(all(count == 1 for _, count in idle[1:]), idle[:3])  # the meta alone
            measured["idle_cold_s"] = round(idle[0][0], 4)
            measured["idle_warm_max_s"] = round(max(elapsed for elapsed, _ in idle[1:]), 4)
            measured["idle_writes_90"] = 0
            # Busy: six real cards fall due; five are attempted, the sixth waits for the next one.
            keys = [fixture.card(f"busy-{index}", head=False)[0] for index in range(6)]
            self.clock.advance(1)
            del loads[:]
            writes = dict(owner.journal.writes)
            result, elapsed = self.replay(owner)
            self.assertTrue(result)
            self.assertEqual([item["status"] for item in result], ["completed"] * owner.last_replay["attempted"])
            measured["busy"] = {"elapsed_s": round(elapsed, 3), **self.counts(owner.last_replay),
                                "writes": owner.last_replay["writes"]}
            self.assertEqual(owner.last_replay["writes"]["meta"], 1)
            self.assertEqual(owner.last_replay["writes"],
                             {name: owner.journal.writes[name] - writes[name] for name in writes})
            # Deferred: the next one, its allowance spent at its scope fence; it fails at its first Git read.
            left = [key for key in keys if owner.journal.intent(key)["status"] != "completed"]
            self.clock.advance(1)
            patch, _ = self.spent_at(owner, "_scope_fence")
            with patch:
                result, elapsed = self.replay(owner)
            self.assertEqual([item["status"] for item in result], ["pending"])
            self.assertEqual(self.counts(owner.last_replay), {"due": len(left), "attempted": 1, "deferred": 1,
                                                              "skipped": len(left) - 1, "busy": 0, "lost": 0})
            # The reservation, the heads-stopped checkpoint, the deferred outcome, and the cursor.
            self.assertEqual({name: owner.last_replay["writes"][name] for name in ("intent", "meta", "generated")},
                             {"intent": 3, "meta": 1, "generated": 0})
            measured["deferred"] = {"elapsed_s": round(elapsed, 3), **self.counts(owner.last_replay),
                                    "writes": owner.last_replay["writes"]}
        print("representative journal: " + json.dumps(measured, sort_keys=True))

    # -- the 20k-entry ignored .venv against the allowance --------------------------------------------

    def test_realistic_venvs_fit_the_allowance_or_defer_with_durable_progress(self):
        fixture = self.fixture
        cards = []
        for name in ("venv-a", "venv-b"):
            key, workspace, _ = fixture.card(name, head=False)
            cards.append((key, workspace, fixture.realistic_venv(workspace, 430)))
        self.assertGreaterEqual(len(fixture.ignored_rows(cards[0][1])), 20000)
        targets = [fixture.snapshot(external) for _, _, external in cards]
        ticks = []
        while any(self.owner.journal.intent(key)["status"] != "completed" for key, _, _ in cards):
            self.assertLess(len(ticks), 8, ticks)
            result, elapsed = self.replay()
            report = self.owner.last_replay
            ticks.append({"elapsed_s": round(elapsed, 3), **self.counts(report), "deferred_at": report["deferred_at"],
                          "statuses": [item["status"] for item in result]})
            # The bound itself, on real Git and a real filesystem of 20000 entries.
            self.assertLess(elapsed, PHASE_BOUND)
            for item in result:
                self.assertIn(item["status"], {"completed", "pending"}, item["reason"])
                if item["status"] == "pending":
                    self.assertIn("allowance exhausted", item["reason"])
            self.clock.advance(RETRY_COOLDOWN if report["deferred"] or not result else 60)
        for (_, workspace, external), before in zip(cards, targets, strict=True):
            self.assertFalse(workspace.exists())
            self.assertEqual(fixture.snapshot(external), before)
        print("realistic venvs under the allowance: " + json.dumps(ticks))

    def test_allowance_floor_admits_no_attempt_it_cannot_start(self):
        """Below `ATTEMPT_FLOOR` nothing is reserved; the due intent keeps its due time."""
        fixture = self.fixture
        key = fixture.card("floor-0", head=False)[0]
        stored = journal_bytes(self.owner.journal)
        result, _ = self.replay(allowance=ATTEMPT_FLOOR / 2)
        self.assertEqual(result, [])
        self.assertEqual(self.counts(self.owner.last_replay),
                         {"due": 1, "attempted": 0, "deferred": 0, "skipped": 1, "busy": 0, "lost": 0})
        self.assertEqual(journal_bytes(self.owner.journal), stored)
        self.assertNotIn("retry", self.owner.journal.intent(key))


if __name__ == "__main__":
    unittest.main()
