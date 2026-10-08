"""The automatic cleanup replay's one elapsed allowance (ummanu-145).

Disposable Git repositories with the owned-cleanup fixture: the production tick's cleanup phase
through its own entry point, real Git children killed at the bound, real lane and journal locks, the
real local-pty runtime's stop against a stalled supervisor, every changed destructive boundary cut
short and recovered by a reloaded owner, rotation under the allowance and a 319-intent journal.
Wall clock is asserted only against the sprint's phase bound; the rest is printed for the report.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import inspect
import json
import math
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import psycopg

from tests import test_owned_cleanup as owned_fixtures, test_sql_card_pool as pool_fixtures
from tests.production_runtime_fixtures import registered_production_runtime
from ummanu import _proc
from ummanu.board.sql_cards import SqlCardClient
from ummanu.dispatch import attempt_accounting, cleanup as cleanup_module, production
from ummanu.dispatch.cleanup import (
    ATTEMPT_FLOOR,
    PUBLICATION_RESERVE,
    REPLAY_ALLOWANCE,
    RETRY_COOLDOWN,
    CleanupOwner,
)
from ummanu.dispatch.host import CommandHostRuntime
from ummanu.dispatch.types import HostError
from ummanu.runtime.head import HeadRun
from ummanu.runtime.head.local_pty import protocol
from ummanu.runtime.local_pty_head import LocalPtyHeadRuntime
from ummanu.tasks import TaskError


class Credentials:
    """The driver stand-in's credentials, at a numeric address (no name resolution)."""

    def conninfo(self) -> str:
        return "host=127.0.0.1 port=5432 dbname=board user=app password=secret"


class StallingCursor(pool_fixtures._Cursor):
    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        kind = "settings" if "set_config('statement_timeout'" in sql else "command"
        self.connection._exchange(kind)  # type: ignore[attr-defined]
        return super().execute(sql, params)


class StallingConnection(pool_fixtures._Connection):
    """The driver stand-in whose exchanges wait through the installed psycopg's own
    `Connection.wait` on a real pipe: an armed kind of exchange waits for a reply that never comes,
    as a backend or transport that stops answering does."""

    def __init__(self, number: int, stall: set[str]) -> None:
        super().__init__(number)
        self.read_fd, self.write_fd = os.pipe()
        self.pgconn = SimpleNamespace(socket=self.read_fd, finish=self._finish)
        self.stall = stall
        self.finished = False
        self.wait = types.MethodType(psycopg.Connection.wait, self)

    def _finish(self) -> None:
        self.closed = self.finished = True

    def _exchange(self, kind: str) -> None:
        if kind in self.stall:
            def reply():
                while not ((yield psycopg.waiting.WAIT_R) & psycopg.waiting.READY_R):
                    pass
            self.wait(reply())

    def cursor(self) -> StallingCursor:
        return StallingCursor(self)

    def commit(self) -> None:
        self._exchange("commit")
        super().commit()

    def rollback(self) -> None:
        self._exchange("rollback")
        super().rollback()

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        self._exchange("schema")
        return super().execute(sql, params)


class AnsweringConnection(StallingConnection):
    """A `StallingConnection` whose every exchange waits through the installed psycopg's own
    `Connection.wait` on a pipe that already holds the reply, so each one answers at once."""

    def __init__(self, number: int) -> None:
        super().__init__(number, {"settings", "command", "commit", "rollback", "schema"})
        os.write(self.write_fd, b"x")
        self.answered: list[str] = []

    def _exchange(self, kind: str) -> None:
        super()._exchange(kind)
        self.answered.append(kind)


#: The sprint's bound on the cleanup phase.
PHASE_BOUND = 5.0
COUNTS = ("due", "attempted", "deferred", "skipped", "busy", "lost", "unread")
#: What an invocation may take beyond its deadline: a single indivisible call and the return.
OVERRUN = 0.3
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
        """Spend the running automatic replay's work allowance now: its next bounded wait or effect
        defers, while the publications recording that keep the deadline's reserved tail."""
        allowance = cleanup_module._ALLOWANCE.get()
        self.assertIsNotNone(allowance, "only an automatic replay has an allowance")
        allowance.deadline = allowance.clock() + allowance.reserve

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

    def git_shim(self, script: str):
        """`git` on PATH: `script` (bash, the real argv in "$@") runs first, then the real Git. A
        hang `exec`s in place after recording its pid, so the test can prove that process gone."""
        real = shutil.which("git")
        bin_dir = self.fixture.root / "shim-bin"
        bin_dir.mkdir(exist_ok=True)
        self.hung = bin_dir / "hung-pids"
        shim = bin_dir / "git"
        shim.write_text(f"#!/bin/bash\nHUNG={self.hung}\n{script}\nexec {real} \"$@\"\n")
        shim.chmod(0o755)
        return mock.patch.dict(os.environ, {"PATH": f"{bin_dir}:{os.environ['PATH']}"})

    @staticmethod
    def hang_status_of(workspace: Path) -> str:
        """Hang `git status` of one exact workspace, read through the pinned descriptor too."""
        return (f'if [ "$1" = -C ] && [ "$3" = status ] && [ "$(readlink -f "$2")" = "{workspace.resolve()}" ]; '
                'then echo $$ >> "$HUNG"; exec sleep 60; fi')

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
        with self.git_shim(self.hang_status_of(slow_workspace)):
            entry, _, elapsed = self.production_cleanup_phase()
        self.no_hung_git()
        report = entry["cleanup"]
        # One allowance for the phase: the first intent's Git child was killed at it, and no other
        # due intent was admitted after it.
        self.assertLess(elapsed, REPLAY_ALLOWANCE + OVERRUN)
        # Selection reads only as far as the attempts go: the next due one was read and skipped at
        # the floor, the other four never read, none reserved; the cursor stops before the skipped.
        self.assertEqual(self.counts(report), {"due": 2, "attempted": 1, "deferred": 1, "skipped": 1,
                                               "busy": 0, "lost": 0, "unread": 4})
        # The cursor passes the skipped one too: it is reached again on the next rotation.
        self.assertEqual(self.owner.journal.replay_cursor(), cards[1][0])
        self.assertEqual(report["cursor"], "advanced")
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
                         {"due": 3, "attempted": 2, "deferred": 0, "skipped": 0, "busy": 1, "lost": 0, "unread": 0})
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
        # The journal lock may be awaited into the publication tail, never past the deadline.
        self.assertGreaterEqual(elapsed, 1.0 - 0.05)
        self.assertLess(elapsed, 1.0 + OVERRUN)
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
        result, elapsed = self.replay(allowance=2.0)
        # Without the allowance this stop could wait its 5 s connect, the framed exchange and its
        # 10 s exit confirmation; with it the whole replay ends within its deadline.
        self.assertLess(elapsed, 2.0 + OVERRUN)
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
        # Checked before every entry: the call that spent it was the last one made.
        self.assertEqual(len(calls), 101)
        intent = self.owner.journal.intent(key)
        self.assertNotIn("removal_started", intent["progress"])
        self.assertEqual(len(fixture.ignored_rows()), before - 101)
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

    #: A real Git child of the automatic removal that hangs until the deadline ends its group.
    HANG_REMOVAL = ('if [ "$3" = worktree ] && [ "$4" = remove ]; then {effect}echo $$ >> "$HUNG"; '
                    'exec sleep 60; fi')

    def test_git_removal_ended_before_its_effect_rechecks_identity_and_finishes(self):
        key = self.due_card()
        with self.git_shim(self.HANG_REMOVAL.format(effect="")):
            _, elapsed = self.replay()
        self.assertLess(elapsed, REPLAY_ALLOWANCE + OVERRUN)
        self.no_hung_git()
        intent = self.owner.journal.intent(key)
        self.assertTrue(intent["progress"]["removal_started"])
        self.assertNotIn("workspace_removed", intent["progress"])
        self.assertTrue(self.fixture.workspace.exists())
        self.assert_deferred_then_recovered(key, "Git worktree")

    def test_git_removal_ended_after_the_directory_finishes_from_the_admitted_registration(self):
        fixture = self.fixture
        key = self.due_card()
        # Git's own first effect, then a hang: the deadline ends the group between the two.
        with self.git_shim(self.HANG_REMOVAL.format(effect='rm -rf "$5"; ')):
            _, elapsed = self.replay()
        self.assertLess(elapsed, REPLAY_ALLOWANCE + OVERRUN)
        self.no_hung_git()
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
            self.assertLessEqual(report["attempted"], math.floor((REPLAY_ALLOWANCE - PUBLICATION_RESERVE - ATTEMPT_FLOOR) / 0.9) + 1)
            # The cursor at most once, only with due work observed, exactly when it says it advanced.
            self.assertEqual(report["writes"]["meta"], 1 if report["cursor"] == "advanced" else 0)
            self.assertTrue(report["due"] or report["cursor"] == "unchanged")
            self.assertEqual(self.owner.journal.writes["meta"] - before["meta"], report["writes"]["meta"])
            self.assertLessEqual(len(reports), 6)
        # Each due intent exactly once before any is repeated. A key skipped at the floor is passed
        # by the cursor and reached later in the rotation, so the order is the rotation's, not sorted.
        order = [HeadRun.from_json(card[2].worker_head_run).run_id for card in cards]
        self.assertEqual(len(attempts), len(order))
        self.assertEqual(sorted(attempts), sorted(order))
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
            self.assertEqual(len([key for key in keys if owner.journal.intent(key)["status"] != "completed"]), 1)
            self.clock.advance(1)
            patch, _ = self.spent_at(owner, "_scope_fence")
            with patch:
                result, elapsed = self.replay(owner)
            self.assertEqual([item["status"] for item in result], ["pending"])
            self.assertEqual({name: owner.last_replay[name] for name in ("due", "attempted", "deferred", "skipped")},
                             {"due": 1, "attempted": 1, "deferred": 1, "skipped": 0})
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
        result, _ = self.replay(allowance=PUBLICATION_RESERVE + ATTEMPT_FLOOR / 2)
        self.assertEqual(result, [])
        self.assertEqual(self.counts(self.owner.last_replay),
                         {"due": 1, "attempted": 0, "deferred": 0, "skipped": 1, "busy": 0, "lost": 0, "unread": 0})
        # The intent is byte-identical (no reservation); only the cursor moved past it.
        after = journal_bytes(self.owner.journal)
        self.assertEqual({name: body for name, body in after.items() if name != "meta.json"},
                         {name: body for name, body in stored.items() if name != "meta.json"})
        self.assertNotIn("retry", self.owner.journal.intent(key))
        self.assertEqual(self.owner.journal.replay_cursor(), key)


    # -- the board store client, Git descendants and locks, slow enumeration (rework 3) -------------

    def sql_client(self) -> SqlCardClient:
        """A real `SqlCardClient` over the driver stand-in of `test_sql_card_pool`, as the reader's."""
        self.enterContext(mock.patch("psycopg.connect",
                                     side_effect=lambda *args, **kwargs: pool_fixtures._Connection(1)))
        client = SqlCardClient(Credentials(), self.fixture.data)  # type: ignore[arg-type]
        self.addCleanup(client.close)
        self.fixture.runtime.reader.client = client
        return client

    def test_production_phase_awaits_a_held_board_transaction_only_within_its_deadline(self):
        key = self.fixture.card("turn-1", head=False)[0]
        client = self.sql_client()
        release = self.hold(client.transaction)
        entry, _, elapsed = self.production_cleanup_phase()
        release()
        self.assertLess(elapsed, REPLAY_ALLOWANCE + OVERRUN)
        stored = self.owner.journal.intent(key)
        self.assertEqual(stored["status"], "pending")
        self.assertIn("allowance exhausted at the board store transaction's turn", stored["reason"])
        self.assertEqual(stored["retry"]["next_attempt_at"], self.clock.now + RETRY_COOLDOWN)
        self.assertEqual(entry["cleanup"]["deferred"], 1)
        # The bound ended with the replay; an hour on, the same client completes the intent.
        self.assertIsNone(client._local.remaining)
        self.clock.advance(RETRY_COOLDOWN)
        self.production_cleanup_phase()
        self.assertEqual(self.owner.journal.intent(key)["status"], "completed")

    def reference_hook(self, script: str) -> Path:
        """An ordinary `reference-transaction` hook of the project repository."""
        hook = self.fixture.repo / ".git" / "hooks" / "reference-transaction"
        hook.write_text("#!/bin/bash\n" + script)
        hook.chmod(0o755)
        self.addCleanup(lambda: hook.unlink(missing_ok=True))
        return hook

    def git_locks(self) -> list[str]:
        return sorted(str(path.relative_to(self.fixture.repo)) for path in (self.fixture.repo / ".git").rglob("*.lock"))

    def hanging_hook(self) -> tuple[Path, Path, Path]:
        """At `prepared`: a background writer and a helper holding Git's output pipe, both acting only
        after the deadline (4.2 s), then a hang."""
        root = self.fixture.root
        late, piped, pids = root / "late-effect", root / "piped-effect", root / "hook-pids"
        self.reference_hook(
            'if [ "$1" = prepared ]; then\n'
            f"  (sleep 4.2; printf late > {late}) </dev/null >/dev/null 2>&1 &\n"
            f"  echo $! >> {pids}\n"
            f"  (sleep 4.2; printf late > {piped}) &\n"
            f"  echo $! >> {pids}; echo $$ >> {pids}\n"
            "  exec sleep 8 </dev/null >/dev/null 2>&1\n"
            "fi\n")
        self.addCleanup(self.kill_recorded, pids)
        return late, piped, pids

    @staticmethod
    def kill_recorded(pids: Path) -> None:
        for raw in (pids.read_text().split() if pids.exists() else []):
            try:
                os.kill(int(raw), signal.SIGKILL)
            except ProcessLookupError:
                pass

    def test_a_hanging_reference_hook_ends_with_its_transaction_and_leaves_no_lock(self):
        fixture = self.fixture
        key, _, _ = fixture.card("hook-1", head=False)
        late, piped, pids = self.hanging_hook()
        entry, _, elapsed = self.production_cleanup_phase()
        self.assertLess(elapsed, REPLAY_ALLOWANCE + OVERRUN)
        # Git, the hook and both of its children are gone before the replay returned: Git ended on
        # SIGTERM and removed its own lock files, so nothing is left to write after the lanes go.
        recorded = [int(raw) for raw in pids.read_text().split()]
        self.assertEqual(len(recorded), 3)
        for pid in recorded:
            with self.assertRaises(ProcessLookupError, msg=f"hook process {pid} outlived the replay"):
                os.kill(pid, 0)
        self.assertEqual(self.git_locks(), [])
        with cleanup_module.reference_lock(fixture.data, "hook-1", blocking=False), \
                cleanup_module.reference_lock(fixture.data, "hook-1", lane="lifecycle", blocking=False):
            pass
        time.sleep(4.2)
        self.assertFalse(late.exists())
        self.assertFalse(piped.exists())
        intent = self.owner.journal.intent(key)
        self.assertEqual(intent["status"], "pending")
        self.assertIn("allowance exhausted at Git update-ref (its process group was ended at the bound)",
                      intent["reason"])
        self.assertTrue(intent["progress"]["ref_delete_admitted"])
        self.assertTrue(intent["progress"]["workspace_removed"])
        self.assertEqual(git(fixture.repo, "rev-parse", "refs/heads/pipeline/hook-1"), fixture.base)
        self.assertEqual(entry["cleanup"]["deferred"], 1)
        # The hook still hangs an hour on: the same bounded refusal, with nothing stale behind it.
        self.clock.advance(RETRY_COOLDOWN)
        entry, _, elapsed = self.production_cleanup_phase()
        self.assertLess(elapsed, REPLAY_ALLOWANCE + OVERRUN)
        self.assertEqual(self.owner.journal.intent(key)["status"], "pending")
        self.assertEqual(self.git_locks(), [])
        # Once it answers, a reloaded owner deletes the ref through the same native transaction.
        (fixture.repo / ".git" / "hooks" / "reference-transaction").unlink()
        self.clock.advance(RETRY_COOLDOWN)
        self.owner = CleanupOwner(fixture.runtime)
        self.production_cleanup_phase()
        self.assertEqual(self.owner.journal.intent(key)["status"], "completed")
        self.assertEqual(git(fixture.repo, "for-each-ref", "refs/heads/pipeline/hook-1"), "")
        self.assertEqual(self.git_locks(), [])

    def test_after_an_ended_ref_transaction_a_changed_tip_is_retained(self):
        fixture = self.fixture
        key, _, _ = fixture.card("hook-2", head=False)
        self.hanging_hook()
        self.production_cleanup_phase()
        self.assertTrue(self.owner.journal.intent(key)["progress"]["ref_delete_admitted"])
        (fixture.repo / ".git" / "hooks" / "reference-transaction").unlink()
        moved = git(fixture.repo, "commit-tree", fixture.base + "^{tree}", "-p", fixture.base, "-m", "author")
        git(fixture.repo, "update-ref", "refs/heads/pipeline/hook-2", moved)
        self.clock.advance(RETRY_COOLDOWN)
        self.owner = CleanupOwner(fixture.runtime)
        self.production_cleanup_phase()
        intent = self.owner.journal.intent(key)
        self.assertEqual(intent["status"], "preserved", intent["reason"])
        self.assertIn("candidate ref changed; retained current tip " + moved, intent["reason"])
        self.assertEqual(git(fixture.repo, "rev-parse", "refs/heads/pipeline/hook-2"), moved)
        self.assertEqual(self.git_locks(), [])

    def test_a_foreign_ref_lock_is_named_and_never_removed(self):
        fixture = self.fixture
        key, _, _ = fixture.card("lock-1", head=False)
        for name in ("refs/heads/main.lock", "packed-refs.lock"):
            with self.subTest(lock=name):
                lock = fixture.repo / ".git" / name
                lock.write_text("held by another writer\n")
                identity = lock.stat().st_ino, lock.read_bytes()
                self.production_cleanup_phase()
                intent = self.owner.journal.intent(key)
                self.assertEqual(intent["status"], "pending", intent["reason"])
                self.assertIn("refused a changed or locked tip", intent["reason"])
                self.assertIn(name.rsplit("/", 1)[-1], intent["reason"])
                self.assertEqual((lock.stat().st_ino, lock.read_bytes()), identity)
                self.assertEqual(git(fixture.repo, "rev-parse", "refs/heads/pipeline/lock-1"), fixture.base)
                lock.unlink()
                self.clock.advance(RETRY_COOLDOWN)
        self.production_cleanup_phase()
        self.assertEqual(self.owner.journal.intent(key)["status"], "completed")

    def test_slow_namespace_enumeration_makes_bounded_progress_each_hour(self):
        """20000 exactly owned entries, each enumeration step delayed 0.3 ms (injected latency)."""
        fixture = self.fixture
        key, workspace, _ = fixture.card("namespace-1", head=False)
        namespace = workspace / ".ummanu-task-env"
        namespace.mkdir()
        (namespace / "owner.json").write_text(json.dumps({"owner": "ummanu-dispatcher", "schema_version": 1,
                                                          "workspace": str(workspace)}))
        for n in range(20000):
            (namespace / f"entry-{n}").touch()
        (fixture.repo / ".git" / "info" / "exclude").write_text(".ummanu-task-env/\n")
        native = os.scandir

        class SlowEntries:
            def __init__(self, scan):
                self.scan = scan

            def __enter__(self):
                self.scan.__enter__()
                return self

            def __exit__(self, *args):
                return self.scan.__exit__(*args)

            def __iter__(self):
                for entry in self.scan:
                    time.sleep(0.0003)
                    yield entry

        def scan(path):
            result = native(path)
            if isinstance(path, int) and Path(os.readlink(f"/proc/self/fd/{path}")) == namespace:
                return SlowEntries(result)
            return result
        left, hours = [], []
        with mock.patch.object(os, "scandir", side_effect=scan):
            while self.owner.journal.intent(key)["status"] != "completed":
                self.assertLess(len(hours), 8, hours)
                entry, _, elapsed = self.production_cleanup_phase()
                self.assertLess(elapsed, REPLAY_ALLOWANCE + OVERRUN)
                left.append(len(os.listdir(namespace)) if namespace.exists() else 0)
                hours.append({"elapsed_s": round(elapsed, 3), "left": left[-1],
                              "status": self.owner.journal.intent(key)["status"],
                              "deferred_at": entry["cleanup"].get("deferred_at", "")})
                self.clock.advance(RETRY_COOLDOWN)
        # Every hour removed more of it; none restarted from the whole namespace.
        self.assertGreater(len(hours), 1)
        self.assertEqual(left, sorted(left, reverse=True))
        self.assertEqual(len(set(left)), len(left))
        self.assertFalse(workspace.exists())
        print("slow namespace enumeration, hour by hour: " + json.dumps(hours))

    def test_slow_intent_reads_still_reach_attempts_and_rotate_without_repeating(self):
        """A cold reload each tick (as the oneshot production tick is), each selection read delayed."""
        fixture = self.fixture
        keys = []
        for n in range(30):
            ref = f"legacy-{n:02d}"
            task = {**copy.deepcopy(fixture.task), "id": "task-" + ref, "ref": ref, "closed": True}
            fixture.tasks[ref] = task
            keys.append(self.owner.journal.remember(task, {}, disposition="close"))
        keys.sort()
        native_load = cleanup_module.CleanupJournal._load

        def load(path):
            # Only the selection's own reads are slow; the attempts read as fast as ever.
            if sys._getframe(1).f_code.co_name == "due_keys" and path.parent.name == "intents":
                time.sleep(0.2)
            return native_load(path)
        attempted, ticks = [], []
        with mock.patch.object(cleanup_module.CleanupJournal, "_load", staticmethod(load)):
            while len(attempted) < len(keys):
                self.assertLess(len(ticks), 10, ticks)
                self.clock.advance(60)
                owner = CleanupOwner(fixture.runtime)
                result, elapsed = self.replay(owner)
                self.assertLess(elapsed, REPLAY_ALLOWANCE + OVERRUN)
                self.assertTrue(result, owner.last_replay)
                attempted += [key for key in keys if owner.journal.intent(key).get("retry", {}).get("last_attempt_at")
                              == self.clock.now]
                ticks.append({"elapsed_s": round(elapsed, 3), **self.counts(owner.last_replay)})
        # Each intent once, in rotation order: no tick re-read and re-attempted the same prefix.
        self.assertEqual(attempted, keys)
        print("slow selection reads: " + json.dumps(ticks))

    def test_scope_fence_reads_no_further_run_directory_once_the_deadline_passed(self):
        from ummanu.runtime.head import TaskRef
        from ummanu.runtime.local_pty_head import fence_cleanup_scopes
        root = self.fixture.data / "heads"
        for n in range(50):
            (root / f"run-{n:02d}").mkdir(parents=True)
        left = [4.0]

        def remaining():
            left[0] -= 1
            return left[0]
        with self.assertRaisesRegex(ValueError, "deadline passed"):
            fence_cleanup_scopes(root, "/nowhere", TaskRef.card("x-1"), [], remaining=remaining)
        self.assertEqual(left[0], 0)


    # -- rework 5: driver waits, owner mutex, native enumeration, selection progress -----------------

    def test_an_unanswered_admission_commit_is_ambiguous_and_launches_no_disposal(self):
        fixture = self.fixture
        key, workspace, _ = fixture.card("commit-1", head=False)
        stall: set[str] = set()
        opened: list[StallingConnection] = []

        def connect(*args, **kwargs):
            opened.append(StallingConnection(len(opened), stall))
            return opened[-1]
        self.enterContext(mock.patch("psycopg.connect", side_effect=connect))
        client = SqlCardClient(Credentials(), fixture.data)  # type: ignore[arg-type]
        self.addCleanup(client.close)
        fixture.runtime.reader.client = client
        client._query("SELECT 1")  # a pooled connection, as the dispatcher's is
        stall.add("commit")
        entry, _, elapsed = self.production_cleanup_phase()
        stall.clear()
        self.assertLess(elapsed, REPLAY_ALLOWANCE + OVERRUN)
        intent = self.owner.journal.intent(key)
        self.assertEqual(intent["status"], "pending")
        self.assertIn("its outcome is unknown", intent["reason"])
        # Nothing was disposed from the ambiguous admission, and its connection is never reused.
        self.assertTrue(workspace.exists())
        self.assertNotIn("removal_started", intent["progress"])
        self.assertTrue(opened[0].finished)
        self.assertNotIn(opened[0], client._idle)
        self.assertEqual(entry["cleanup"]["deferred"], 1)
        # An hour on, a reloaded owner reads the board again and completes it on a new connection.
        self.clock.advance(RETRY_COOLDOWN)
        self.owner = CleanupOwner(fixture.runtime)
        self.production_cleanup_phase()
        self.assertEqual(self.owner.journal.intent(key)["status"], "completed")
        self.assertGreater(len(opened), 1)

    def test_another_owner_operation_holds_the_owner_only_within_the_deadline(self):
        fixture = self.fixture
        keys = sorted(fixture.card(f"mutex-{n}", head=False)[0] for n in range(2))
        release = self.hold(lambda: self.owner._operations)
        stored = journal_bytes(self.owner.journal)
        entry, _, elapsed = self.production_cleanup_phase()
        release()
        self.assertLess(elapsed, REPLAY_ALLOWANCE + OVERRUN)
        report = entry["cleanup"]
        self.assertEqual(report["attempted"], 0)
        self.assertIn("cleanup owner operation", report["deferred_at"])
        # Nothing was reserved: the intents are byte-identical and keep their due time.
        after = journal_bytes(self.owner.journal)
        for key in keys:
            self.assertEqual(after["intents/" + key + ".json"], stored["intents/" + key + ".json"])
            self.assertNotIn("retry", self.owner.journal.intent(key))
        # Released, the same owner (nested operations included) completes both.
        self.clock.advance(60)
        self.production_cleanup_phase()
        self.assertEqual([self.owner.journal.intent(key)["status"] for key in keys], ["completed"] * 2)

    def test_a_slow_whole_journal_proof_defers_without_a_terminal_outcome(self):
        """The shared-removal proof of a workspace removed outside cleanup reads the whole journal:
        319 intents, each native enumeration step delayed 17 ms (injected latency)."""
        fixture = self.fixture
        key, workspace, _ = fixture.card("glob-1", head=False)
        template = self.owner.journal.intent(key)
        clones = {}
        for n in range(318):
            clone = copy.deepcopy(template)
            clone["task"] = {**clone["task"], "id": f"clone-id-{n}", "ref": f"clone-ref-{n}"}
            clone["status"] = "completed"
            clones[hashlib.sha256(f"clone-{n}".encode()).hexdigest()] = clone
        self.owner.journal.save({"intents": clones})
        git(fixture.repo, "worktree", "remove", str(workspace))
        intents = self.owner.journal.path / "intents"
        reading = threading.local()
        native_scan, native_read = os.scandir, cleanup_module.CleanupJournal.read
        enumerated: list[str] = []

        class Slow:
            def __init__(self, scan):
                self.scan = scan

            def __enter__(self):
                self.scan.__enter__()
                return self

            def __exit__(self, *args):
                return self.scan.__exit__(*args)

            def __iter__(self):
                for entry in self.scan:
                    time.sleep(0.017)
                    enumerated.append(entry.name)
                    yield entry

        def scan(path):
            result = native_scan(path)
            return Slow(result) if getattr(reading, "on", False) and Path(path) == intents else result

        def read(journal):
            reading.on = True
            try:
                return native_read(journal)
            finally:
                reading.on = False
        with mock.patch.object(os, "scandir", side_effect=scan), \
                mock.patch.object(cleanup_module.CleanupJournal, "read", read):
            _, _, elapsed = self.production_cleanup_phase()
        self.assertLess(elapsed, REPLAY_ALLOWANCE + OVERRUN)
        self.assertGreater(len(enumerated), 0)
        self.assertLess(len(enumerated), 319)  # cut between native entries, never after all of them
        intent = self.owner.journal.intent(key)
        self.assertEqual(intent["status"], "pending")
        self.assertIn("allowance exhausted at cleanup journal read", intent["reason"])
        self.assertNotIn("terminal", intent["progress"])
        # Read whole an hour on, the proof decides: no shared removal, so a terminal outcome.
        self.clock.advance(RETRY_COOLDOWN)
        self.owner = CleanupOwner(fixture.runtime)
        self.production_cleanup_phase()
        fixture.assert_terminal(self.owner.journal.intent(key), "workspace-disappeared")

    def test_a_persistently_slow_first_read_never_starves_later_intents(self):
        """Cold reloads, the first key's eligibility read delayed 2.65 s every time (injected)."""
        fixture = self.fixture
        keys = sorted(fixture.card(f"starve-{n}", head=False)[0] for n in range(6))
        native = cleanup_module.CleanupJournal._load
        slow_reads: list[int] = []

        def load(path):
            if sys._getframe(1).f_code.co_name == "due_keys" and path.name == keys[0] + ".json":
                slow_reads.append(1)
                time.sleep(2.65)
            return native(path)
        ticks = []
        with mock.patch.object(cleanup_module.CleanupJournal, "_load", staticmethod(load)):
            for _ in range(3):
                self.owner = CleanupOwner(fixture.runtime)
                entry, _, elapsed = self.production_cleanup_phase()
                self.assertLess(elapsed, REPLAY_ALLOWANCE + OVERRUN)
                ticks.append({"elapsed_s": round(elapsed, 3), **entry["cleanup"],
                              "at": self.owner.journal.replay_cursor()[:8]})
                self.clock.advance(60)
        # The first invocation passes the slow key without reserving it; the second reaches every
        # later intent; the third comes back round to the slow one.
        self.assertEqual(ticks[0]["skipped"], 1)
        self.assertEqual(ticks[0]["cursor"], "advanced")
        self.assertEqual([self.owner.journal.intent(key)["status"] for key in keys[1:]], ["completed"] * 5)
        self.assertEqual(len(slow_reads), 2)
        self.assertNotIn("retry", self.owner.journal.intent(keys[0]))
        print("slow first read across cold reloads: " + json.dumps(ticks))

    def test_a_cursor_the_deadline_left_unwritten_is_reported(self):
        fixture = self.fixture
        fixture.card("cursor-1", head=False)
        with mock.patch.object(self.owner.journal, "set_replay_cursor",
                               side_effect=cleanup_module.Deferred("publication tail spent")):
            self.replay()
        self.assertEqual(self.owner.last_replay["cursor"], "unwritten")
        self.assertEqual(self.owner.journal.replay_cursor(), "")

    def test_runtime_scope_owners_are_read_no_further_once_the_deadline_passed(self):
        from ummanu.runtime.head.local_pty.scope_inventory import read_runtime_scopes
        data = self.fixture.root / "runtime-data"
        for n in range(50):
            (data / "heads" / f"run-{n:02d}").mkdir(parents=True)
        left = [4.0]

        def remaining():
            left[0] -= 1
            return left[0]
        inventory = read_runtime_scopes(data, {"ummanu-head-x.scope"}, remaining=remaining)
        self.assertIn("deadline passed", inventory.errors["runtime_scopes"])
        self.assertNotIn("ummanu-head-x.scope", inventory.disappeared)  # a partial scan proves no absence


    def test_an_unmigrated_v1_journal_is_left_to_the_ordinary_writers(self):
        legacy = self.owner.journal.legacy
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(json.dumps({"version": 1, "intents": {}, "generated": {}}))
        result, elapsed = self.replay()
        self.assertEqual(result, [])
        self.assertLess(elapsed, 0.5)
        self.assertEqual(self.owner.last_replay["deferred_at"], "v1 cleanup journal migration")
        self.assertTrue(legacy.exists())
        # The first ordinary journal writer migrates it; then the automatic replay proceeds.
        self.assertTrue(self.owner.journal.migrate())
        self.assertEqual(self.replay()[0], [])
        self.assertEqual(self.owner.last_replay["deferred_at"], "")

class SqlDeadlineTests(pool_fixtures.PoolCase):
    """`SqlCardClient.within`: one caller deadline over the pool, connection, turn and statements."""

    pool_size = 2

    def setUp(self) -> None:
        super().setUp()
        # A numeric host: under a deadline every connection attempt the driver would make is
        # enumerated first (`conninfo_attempts`), which resolves names.
        self.client = SqlCardClient(Credentials(), self.client.instance_dir,  # type: ignore[arg-type]
                                    pool_size=self.pool_size, pool_wait_seconds=self.pool_wait_seconds)
        self.bounds: list[tuple[str, tuple[Any, ...]]] = []
        native = pool_fixtures._Cursor.execute

        def execute(cursor, sql, params=()):
            if "set_config('statement_timeout'" in sql:
                self.bounds.append((sql, params))
            return native(cursor, sql, params)
        self.enterContext(mock.patch.object(pool_fixtures._Cursor, "execute", execute))

    @staticmethod
    def deadline(seconds: float):
        end = time.monotonic() + seconds
        return lambda: end - time.monotonic()

    def hold(self, enter) -> threading.Event:
        entered, release = threading.Event(), threading.Event()

        def run():
            with enter():
                entered.set()
                release.wait(10)
        thread = threading.Thread(target=run)
        thread.start()
        self.addCleanup(thread.join, 10)
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(5))
        return release

    def test_every_statement_gets_only_what_is_left_and_none_renews_it(self) -> None:
        self.read()  # an open connection: only statements are measured here
        self.on_execute = lambda sql: time.sleep(0.3) if sql == "SELECT 1" else None
        refused: list[str] = []
        started = time.monotonic()
        with self.client.within(self.deadline(1.0), lambda what: refused.append(what) or RuntimeError(what)), \
                self.assertRaises(RuntimeError):
            for _ in range(10):
                self.read()
        self.assertLess(time.monotonic() - started, 1.0 + 0.3 + 0.1)
        milliseconds = [int(params[0][:-2]) for _, params in self.bounds]
        self.assertGreaterEqual(len(milliseconds), 3)
        self.assertEqual(milliseconds, sorted(milliseconds, reverse=True))
        self.assertTrue(all(value <= 1000 for value in milliseconds))
        # LOCAL to each statement's own transaction, which every session ends.
        self.assertTrue(all(sql.count(", true)") == 2 for sql, _ in self.bounds))
        # The stand-in driver has no server to cancel the last statement at its timeout; the next
        # step that would wait, its connection borrow, is refused.
        self.assertEqual(refused, ["borrowing a board store connection"])
        # The bound ended with the block: the next read sets nothing.
        count = len(self.bounds)
        self.on_execute = None
        self.read()
        self.assertEqual(len(self.bounds), count)

    def test_the_transaction_turn_is_awaited_only_within_the_deadline(self) -> None:
        self.hold(self.client.transaction)
        started = time.monotonic()
        with self.client.within(self.deadline(0.5)), self.assertRaises(TaskError), self.client.transaction():
            pass
        self.assertLess(time.monotonic() - started, 0.5 + 0.2)

    def test_the_pool_wait_is_cut_to_the_deadline(self) -> None:
        def pinned():
            session = self.client._session()
            session.__enter__()
            self.client.connection  # noqa: B018 - pins one of the two connections
            return contextlib.closing(SimpleNamespace(close=lambda: session.__exit__(None, None, None)))
        for _ in range(2):
            self.hold(pinned)
        started = time.monotonic()
        with self.client.within(self.deadline(0.5)), self.assertRaises(TaskError) as raised:
            self.read()
        self.assertEqual(raised.exception.code, "backend_unavailable")
        self.assertLess(time.monotonic() - started, 0.5 + 0.2)

    def test_a_new_connection_and_its_schema_gate_get_only_what_is_left(self) -> None:
        with self.client.within(self.deadline(1.5)), self.assertRaises(TaskError):
            self.read()
        self.assertEqual(self.opened, [])  # libpq counts whole seconds, at least 2: nothing was opened
        options: list[dict[str, Any]] = []
        connect = pool_fixtures._Connection

        def connecting(conninfo, **kwargs):
            options.append(kwargs)
            connection = connect(len(self.opened))
            self.opened.append(connection)
            return connection
        with mock.patch("psycopg.connect", side_effect=connecting), self.client.within(self.deadline(3.5)):
            self.read()
        self.assertEqual(options[0]["connect_timeout"], 3)
        # The schema gate's read ran after its own bound, in a transaction it then ended.
        self.assertTrue(self.opened[0].statements[0].startswith("SELECT set_config('statement_timeout'"))
        self.assertGreaterEqual(self.opened[0].rollbacks, 1)

    def test_a_read_snapshot_still_opens_with_its_isolation_statement(self) -> None:
        self.read()
        connection = self.opened[0]
        before = len(connection.statements)
        with self.client.within(self.deadline(5.0)), self.client.read_snapshot():
            self.client._query("SELECT 1")
        self.assertTrue(connection.statements[before].startswith("SET TRANSACTION ISOLATION LEVEL"))


    def stalled(self, stall: set[str]) -> StallingConnection:
        """The client's one connection, stalling the armed kinds of exchange."""
        connection = StallingConnection(len(self.opened), stall)
        self.opened.append(connection)
        return connection

    def test_every_driver_exchange_waits_only_within_the_deadline(self) -> None:
        def command() -> None:
            self.client._query("SELECT 1")

        def commit() -> None:
            with self.client.transaction():
                self.client._execute("UPDATE tasks SET title = title")

        def rollback() -> None:
            with self.client.transaction():
                self.client._execute("UPDATE tasks SET title = title")
                raise ValueError("the caller's own failure")

        def unpin() -> None:
            # A failed statement leaves its session's connection aborted; the unpin rolls it back.
            with contextlib.suppress(TaskError):
                self.client._query("FAIL")

        cases = {"settings": (command, "settings"), "command": (command, "command"), "commit": (commit, "commit"),
                 "rollback": (rollback, "rollback"), "unpin": (unpin, "rollback")}
        for name, (operation, kind) in cases.items():
            with self.subTest(exchange=name):
                stall: set[str] = set()
                with mock.patch("psycopg.connect", side_effect=lambda *a, stall=stall, **k: self.stalled(stall)):
                    self.client._query("SELECT 1")  # connected and pooled without a deadline
                    connection = self.opened[-1]
                    stall.add(kind)
                    started = time.monotonic()
                    raised = None
                    with self.client.within(self.deadline(0.6)):
                        try:
                            operation()
                        except BaseException as exc:  # noqa: BLE001 - what each case raised is checked below
                            raised = exc
                    elapsed = time.monotonic() - started
                self.assertLess(elapsed, 0.6 + 0.2, name)
                # The exchange's connection was closed and is never handed out again.
                self.assertTrue(connection.finished, name)
                self.assertNotIn(connection, self.client._idle, name)
                if name == "rollback":
                    # The caller's own failure stays what the caller sees.
                    self.assertIsInstance(raised, ValueError)
                elif name != "unpin":
                    self.assertIsInstance(raised, TaskError, name)
                    self.assertIn("outcome is unknown", str(raised))

    def test_a_cold_connection_s_schema_gate_waits_only_within_the_deadline(self) -> None:
        stall = {"schema"}
        with mock.patch("psycopg.connect", side_effect=lambda *a, **k: self.stalled(stall)):
            started = time.monotonic()
            with self.client.within(self.deadline(2.2)), self.assertRaises(TaskError):
                self.client._query("SELECT 1")
            self.assertLess(time.monotonic() - started, 2.2 + 0.2)
        self.assertTrue(self.opened[-1].finished)
        self.assertEqual(self.client._open, 0)

    def test_ordinary_and_bounded_exchanges_answer_through_the_installed_driver_wait(self) -> None:
        """Schema gate, query, commit and rollback, without a deadline and within one, all wait
        through the real `psycopg.Connection.wait`, which the wrapper calls with a `timeout`."""
        def exchanges() -> None:
            self.client._query("SELECT 1")
            with self.client.transaction():
                self.client._execute("UPDATE tasks SET title = title")
            with contextlib.suppress(ValueError), self.client.transaction():
                self.client._execute("UPDATE tasks SET title = title")
                raise ValueError("the caller's own failure")

        opened: list[AnsweringConnection] = []
        with mock.patch("psycopg.connect",
                        side_effect=lambda *a, **k: opened.append(AnsweringConnection(len(opened))) or opened[-1]):
            exchanges()
            ordinary = list(opened[0].answered)
            with self.client.within(self.deadline(3.0)):
                exchanges()
        self.assertEqual(len(opened), 1)
        connection = opened[0]
        self.assertFalse(connection.finished)
        self.assertEqual({"schema", "command", "commit", "rollback"}, set(ordinary))
        self.assertNotIn("settings", ordinary)  # no deadline, no statement bound of ours
        self.assertEqual({"settings", "command", "commit", "rollback"},
                         set(connection.answered[len(ordinary):]))

    def test_without_a_deadline_the_driver_wait_is_its_own(self) -> None:
        calls: list[tuple[Any, ...]] = []
        connection = SimpleNamespace(wait=lambda *args: calls.append(args) or "answer", pgconn=None)
        self.client._bound_waits(connection)
        self.assertEqual(connection.wait("generator"), "answer")
        self.assertEqual(connection.wait("generator", 0.5, 7.0), "answer")
        # Unchanged arguments: the driver's own interval and no timeout of ours.
        self.assertEqual(calls, [("generator", 0.1, None), ("generator", 0.5, 7.0)])


class DriverFloorTests(unittest.TestCase):
    """The declared core driver is one whose `Connection.wait` takes the `timeout` that
    `SqlCardClient._bound_waits` passes on every exchange; 3.3.5 and earlier take only an interval."""

    def test_the_declared_driver_floor_has_the_native_wait_timeout(self) -> None:
        manifest = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
        declared = [item for item in manifest["project"]["dependencies"] if item.startswith("psycopg[")]
        self.assertEqual(declared, ["psycopg[binary]>=3.3.6"])
        installed = tuple(int(part) for part in re.findall(r"\d+", psycopg.__version__)[:3])
        self.assertGreaterEqual(installed, (3, 3, 6))
        self.assertIn("timeout", inspect.signature(psycopg.Connection.wait).parameters)


class IsolatedChildWithinTests(unittest.TestCase):
    """`_proc.run_isolated(within=...)`: run, SIGTERM, SIGKILL, drain and reap inside one deadline."""

    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    @staticmethod
    def deadline(seconds: float):
        end = time.monotonic() + seconds
        return lambda: end - time.monotonic()

    def test_a_running_group_gets_sigterm_first_and_no_member_outlives_the_call(self) -> None:
        term, late = self.root / "term", self.root / "late"
        script = (f'trap "printf term > {term}; exit 0" TERM; (sleep 2; printf late > {late}) & '
                  "sleep 30 & wait")
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired) as raised:
            _proc.run_isolated(["bash", "-c", script], timeout=30, within=self.deadline(1.0))
        self.assertLess(time.monotonic() - started, 1.0 + 0.1)
        self.assertFalse(raised.exception.killed)  # type: ignore[attr-defined]
        self.assertTrue(term.exists())
        time.sleep(2.2)
        self.assertFalse(late.exists())

    def test_a_leader_that_ignores_sigterm_is_killed_within_the_deadline(self) -> None:
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired) as raised:
            _proc.run_isolated(["bash", "-c", 'trap "" TERM; sleep 30'], timeout=30, within=self.deadline(1.0))
        self.assertLess(time.monotonic() - started, 1.0 + 0.1)
        self.assertTrue(raised.exception.killed)  # type: ignore[attr-defined]

    def test_a_helper_that_left_the_group_cannot_hold_the_call_past_the_deadline(self) -> None:
        pid = self.root / "pid"
        script = f"setsid bash -c 'echo $$ > {pid}; exec sleep 5' & sleep 0.2; exit 0"
        started = time.monotonic()
        result = _proc.run_isolated(["bash", "-c", script], timeout=30, within=self.deadline(1.0))
        self.addCleanup(lambda: os.kill(int(pid.read_text()), signal.SIGKILL))
        self.assertEqual(result.returncode, 0)
        self.assertLess(time.monotonic() - started, 1.0 + 0.1)


    def test_the_runtime_provenance_probe_runs_only_within_the_deadline(self) -> None:
        from ummanu.dispatch.runtime_provenance import ProductionRuntime
        interpreter = self.root / "python3"
        interpreter.write_text("#!/bin/bash\nexec sleep 30\n")
        interpreter.chmod(0o755)
        runtime = ProductionRuntime(str(interpreter), str(self.root))
        started = time.monotonic()
        result = runtime.probe(self.deadline(0.8))
        self.assertLess(time.monotonic() - started, 0.8 + 0.1)
        self.assertEqual(result.classification, "interpreter_unavailable")


if __name__ == "__main__":
    unittest.main()
