"""Regression scenarios for the dispatcher vitality path and its recovery decisions."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.dispatcher_fixtures import ensure_attempt
from tests.fakes.dispatcher import (
    FakeCatalog,
    FakeHost,
    FakeSprints,
    dispatcher_seed,
)
from tests.observer_identity import bind_observer
from tests.sql_backend_fixtures import card_store
from ummanu.dispatch import runtime as dispatcher_module
from ummanu.dispatch.head_vitality_episode import VitalityVerdict
from ummanu.dispatch.state import DispatcherRecord, now_rfc3339
from ummanu.tasks import TaskReader, TaskWriter, task_audit_for

CARD_REF = "ummanu-510"


class LegacyPathTests(unittest.TestCase):
    """Shared fixture: one card driven through ``_tick_task`` like the runtime tests do."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.data_dir = Path(self.tmpdir.name) / "data"
        env = mock.patch.dict(os.environ, {"UMMANU_DISPATCHER_BODY_DIR": str(self.data_dir / "bodies")})
        env.start()
        self.addCleanup(env.stop)
        self.board = card_store(self, dispatcher_seed(), instance_dir=self.data_dir)
        self.reader = TaskReader(self.board)
        self.writer = TaskWriter(self.board, data_dir=self.data_dir, workspace=self.data_dir)
        self.catalog = FakeCatalog(instance_dir=self.data_dir)
        self.host = FakeHost(self.data_dir / "workspaces", self.catalog)
        self.host.audit = task_audit_for(self.board)
        self.sprints = FakeSprints()
        self.runtime = dispatcher_module.DispatcherRuntime(
            self.reader,
            self.writer,
            task_audit_for(self.board),
            self.data_dir,
            self.catalog,
            self.host,
            owner="ummanu-pilot",
            sprints=self.sprints,
        )

    # -- fixture plumbing -------------------------------------------------------

    def observed_sprint(self) -> None:
        """Bind the pilot card to an open sprint with a declared observer head."""
        self.board.save_metadata(12, sprint_ref="sprint:1031")
        bind_observer(self, "sprint:1031")
        self.sprints.rows["sprint:1031"] = {
            "ref": "sprint:1031",
            "status": "open",
            "observer": {"kind": "head", "profile": "claude-observer"},
        }
        self.board.add_sprint("sprint:1031", status="open", sprint_reservations='["ummanu"]')

    def start_dispatcher(self) -> None:
        self.observed_sprint()
        self.runtime.production_state.save(
            {
                "version": 1,
                "mode": "production",
                "phase": "production",
                "owner": self.runtime.owner,
                "records": {},
            }
        )

    def tick(self) -> dict:
        from ummanu._fsutil import file_lock

        runtime = self.runtime
        with file_lock(runtime.production_state.tick_lock):
            payload = runtime.production_state.load()
            records = runtime.production_state.records(payload)
            attempt_id = ensure_attempt(payload, CARD_REF, runtime.owner, runtime.owner)
            outcome = runtime._tick_task(self.reader.show(CARD_REF), records, payload, attempt_id)
            runtime.production_state.put_records(payload, records)
            payload["last_tick_at"] = now_rfc3339()
            runtime.production_state.save(payload)
        return outcome

    def record_of(self) -> DispatcherRecord:
        return self.runtime.production_state.records(self.runtime.production_state.load())[CARD_REF]

    def report_done(self) -> None:
        record = self.runtime.production_state.load()["records"][CARD_REF]
        document = (Path(record["workspace"]) / "TASK.md").read_text(encoding="utf-8")
        line = next(l for l in document.splitlines() if "--kind done" in l)
        request_id = line.split("--request-id ", 1)[1].split()[0]
        self.writer.report(
            role="worker",
            actor="worker",
            reference=CARD_REF,
            kind="done",
            body="done",
            request_id=request_id,
        )

    def forget_retention(self) -> None:
        """Drop the card's retention from the record, leaving its worker head identity intact.

        A worker parked by ``retain_worker`` on ``report:done`` is stopped BY this dispatcher, and
        since secretary-1539 the vitality reduction is told so. A head that is stopped without an
        active retention on file -- an adopted or crash-recovered record, an operator's own
        SIGSTOP, a session whose retention was dropped while the head survived -- is the shape the
        fe04011b SIGCONT ladder exists for, and this is how the fixture reaches it.
        """
        payload = self.runtime.production_state.load()
        payload["records"][CARD_REF]["worker_continuation"] = {}
        self.runtime.production_state.save(payload)
        self.assertFalse(self.record_of().worker_continuation.retained)

    def stopped_worker_status(self) -> dict:
        """What the host reports for a worker process the kernel has parked in `T`."""
        return {
            "known": True,
            "live": True,
            "reason": "live",
            "last_activity": time.time(),
            "pid_confirmed": True,
            "idle": False,
            "pid_status": {
                "known": True,
                "alive": True,
                "match": True,
                "state": "live-match",
                "stopped": True,
            },
        }

    def run_worker_to_validate_and_review(self) -> None:
        """Claim, report done, pass the default green gate, start the reviewer."""
        self.tick()
        self.report_done()
        self.assertEqual(self.tick()["to"], "validate")
        self.assertEqual(self.tick()["action"], "review-started")


class IssueB5195041LegacyIdlePathTests(LegacyPathTests):
    """issue:b5195041abbc3ec28243: the idle fence read the screen, not the transcript.

    The original defect -- a pid-confirmed head whose pane reads idle for two
    confirmations past the idle window goes straight to the report prompt and then to a
    stop/respawn with no provider evidence consulted -- is fixed by S1-4: the wait tick's
    decision is the persisted episode's verdict, and an advancing provider cursor keeps
    that verdict at HealthyActive, which refuses every destructive rung. These are REAL
    assertions now.
    """

    def test_a_working_transcript_blocks_the_legacy_idle_respawn(self) -> None:
        """The plan's demand, flipped (S1-4): an advancing provider cursor forbids the respawn.

        The screen reads idle forever while the transcript keeps advancing (the exact
        b5195041 shape: rollout JSONL moves, nothing refreshes the pane). The reduction
        sees Turn=Active on every tick, so no episode ever confirms a stall: the head is
        prompted once by the first quiet crossing and then simply waited on.
        """
        self.start_dispatcher()
        # The retained conversation is available, so the rework resumes the same worker.
        self.host.fail_resume_worker_reason = ""
        self.tick()
        self.report_done()
        self.assertEqual(self.tick()["to"], "validate")
        self.assertEqual(self.tick()["action"], "review-started")
        self.writer.verdict(
            role="reviewer",
            actor="reviewer",
            reference=CARD_REF,
            kind="red",
            body="fix it",
            request_id="review-red",
        )
        parked = self.tick()
        self.assertEqual(parked["to"], "assessment")
        self.writer.decide(
            role="observer",
            actor="observer",
            reference=CARD_REF,
            kind="rework",
            body="decided",
            request_id="decision-rework",
        )
        resumed = self.tick()
        self.assertEqual(resumed["action"], "review-red-reused-worker")

        def idle_episode() -> dict:
            """One pane-idle episode to its acting tick, screen idle throughout."""
            first = self.tick()
            assert first["action"] == "waiting-worker-report"
            # The transcript advances every tick -- the exact thing the pane cannot show.
            self.host.provider_cursor = f"rollout:{time.time()}"
            payload = self.runtime.production_state.load()
            record = payload["records"][CARD_REF]
            episode = record.get("worker_vitality_episode")
            if episode:
                # Age the persisted quiet reference the way an operator clock-rewind
                # would; the transcript itself never stops advancing.
                for name in ("started_at", "updated_at"):
                    if episode.get(name):
                        episode[name] -= 1000
                record["worker_vitality_episode"] = episode
                self.runtime.production_state.save(payload)
            return self.tick()

        status = {
            "known": True,
            "live": True,
            "reason": "live",
            "last_activity": time.time(),
            "pid_confirmed": True,
            "idle": True,
        }
        self.host.worker_status_result = dict(status)
        # A working head is never acted on destructively: with the transcript advancing
        # every tick, no episode ever leaves HealthyActive, so there is not even a
        # prompt -- just waits, forever, on evidence of life.
        first = idle_episode()
        self.assertEqual(first["action"], "waiting-worker-report")
        second = idle_episode()

        # What the plan demands of the switch (S1-4): a working transcript is never
        # respawned -- the advancing cursor keeps every verdict at HealthyActive, so
        # the ladder cannot act no matter how the pane reads or how old the wait clock is.
        self.assertNotEqual(second["action"], "worker-respawned")
        self.assertNotIn("restart_worker", self.host.calls)
        self.assertEqual(self.host.calls.count("prompt_worker_report"), 0)
        record = self.record_of()
        self.assertIsNotNone(record.worker_vitality_episode)
        self.assertIn(
            "advancing@provider_cursor",
            " ".join(record.worker_vitality_episode.basis),
            "the working head's own evidence says its transcript advances",
        )


class IssueFe04011bLegacyGatePendingTests(LegacyPathTests):
    """issue:fe04011b3723df8d5c2c: the gate phase counts hours, not liveness.

    A card waiting in validate whose worker sat in ``T (stopped)`` kept writing
    ``gate-pending status: ok errors: []`` with ``worker_idle_since=0``; only the six-hour
    ``GATE_PENDING_STALL_SECONDS`` ceiling applied. Flipped by S1-5 (SIGCONT / response
    window): the gate-pending tick now runs the same vitality reduction + recovery policy
    for the worker head as the report wait does, so `/proc` state `T` is acted on within
    one tick and the six-hour ceiling remains only the outer bound for the CI rollup.
    """

    def test_a_stopped_worker_ends_the_gate_wait_before_the_ceiling(
        self,
    ) -> None:
        """The plan's demand, flipped (S1-5): `T` inside the gate wait is acted on in one tick.

        The original defect: ``_gate_pending`` read only the clock -- a suspended worker
        behind a pending gate was invisible until six hours passed, exactly the incident.
        The fixture ordering matters (S1-3 review MAJOR 2): the pending gate answers must be
        queued BEFORE the report tick, so the second validate-side tick reaches
        ``_gate_pending`` -- the machinery the incident is about.

        Since secretary-1539 the ladder is scoped to a stop signal this dispatcher does NOT own:
        the card's retention is cleared here (``forget_retention``) so the stopped process is a
        head nobody parked on purpose. That is the whole of the fe04011b protection and it is
        unchanged; the retained case is pinned separately in
        ``Issue02fe04d7RetainedWorkerTests``.
        """
        from ummanu.dispatch.gate import GateResult

        self.start_dispatcher()
        self.host.gate_results = [
            GateResult("pending", "CI still running"),
            GateResult("pending", "CI still running"),
            GateResult("pending", "CI still running"),
        ]
        self.tick()
        self.report_done()
        # The report's own move lands in validate; the gate has not been asked yet.
        self.assertEqual(self.tick()["to"], "validate")

        gated = self.tick()
        self.assertEqual(gated["action"], "gate-pending")
        self.assertEqual(gated["status"], "ok")

        # Meanwhile the worker's process is discovered suspended (its /proc state is `T`).
        stopped_status = {
            "known": True,
            "live": True,
            "reason": "live",
            "last_activity": time.time(),
            "pid_confirmed": True,
            "idle": False,
            "pid_status": {
                "known": True,
                "alive": True,
                "match": True,
                "state": "live-match",
                "stopped": True,
            },
        }
        self.host.worker_status_result = dict(stopped_status)
        # ...and nothing this dispatcher did put it there.
        self.forget_retention()

        # Age the pending window just past one minute: far below the six-hour ceiling.
        payload = self.runtime.production_state.load()
        payload["records"][CARD_REF]["gate_pending_since"] -= 61
        self.runtime.production_state.save(payload)

        noticed = self.tick()

        # What the plan demands: the suspension is seen within a tick, not after 6h --
        # one identity-fenced SIGCONT, rung state on file, nothing stopped.
        self.assertEqual(noticed["action"], "worker-sigcont-sent")
        record = self.record_of()
        episode = record.worker_vitality_episode
        assert episode is not None
        self.assertEqual(episode.verdict, VitalityVerdict.SUSPENDED)
        self.assertEqual(episode.recovery_rung, 3)
        self.assertEqual(record.worker_respawns, 0)
        self.assertNotIn("restart_worker", self.host.calls)
        comments = [
            str(comment.get("body") or "")
            for comment in self.reader.show(CARD_REF)["comments"]
            if isinstance(comment, dict) and "stop signal" in str(comment.get("body") or "")
        ]
        self.assertEqual(len(comments), 1, comments)
        self.assertIn("identity-fenced SIGCONT", comments[0])

    def test_an_expired_response_window_mid_gate_escalates_without_stopping(self) -> None:
        """The second rung works inside the gate wait too: operator, never a stop.

        Same scoping as the rung above (secretary-1539): the ladder is climbed over a head this
        dispatcher is not holding, so the card's retention is cleared before the suspension is
        observed.
        """
        from ummanu.dispatch.gate import GateResult
        from ummanu.dispatch.watchdog import suspension_response_window_seconds

        self.start_dispatcher()
        self.host.gate_results = [
            GateResult("pending", "CI still running"),
            GateResult("pending", "CI still running"),
            GateResult("pending", "CI still running"),
            GateResult("pending", "CI still running"),
        ]
        self.tick()
        self.report_done()
        self.assertEqual(self.tick()["to"], "validate")
        self.tick()  # first pending stamp

        stopped_status = {
            "known": True,
            "live": True,
            "reason": "live",
            "last_activity": time.time(),
            "pid_confirmed": True,
            "idle": False,
            "pid_status": {
                "known": True,
                "alive": True,
                "match": True,
                "state": "live-match",
                "stopped": True,
            },
        }
        self.host.worker_status_result = dict(stopped_status)
        self.forget_retention()

        sent = self.tick()
        self.assertEqual(sent["action"], "worker-sigcont-sent")

        # Age BOTH span stamps past the response window: the head stayed parked.
        payload = self.runtime.production_state.load()
        episode = payload["records"][CARD_REF]["worker_vitality_episode"]
        for name in ("stall_frozen_since", "recovery_span_started_at"):
            if episode.get(name):
                episode[name] -= suspension_response_window_seconds() + 60
        payload["records"][CARD_REF]["worker_vitality_episode"] = episode
        self.runtime.production_state.save(payload)

        escalated = self.tick()

        self.assertEqual(escalated["action"], "worker-suspension-escalated")
        record = self.record_of()
        self.assertEqual(record.worker_respawns, 0)
        # The six-hour gate ceiling was nowhere near elapsed; only the policy spoke.
        self.assertLess(
            time.time() - record.gate_pending_since,
            600,
            "the escalation must come from the response window, not the gate clock",
        )
        self.assertNotIn("restart_worker", self.host.calls)
        self.assertNotIn("stop_head:worker", self.host.calls)
        self.assertNotIn("stop_workspace", self.host.calls)

    def test_a_deterministic_refusal_mid_gate_escalates_fast(self) -> None:
        """The 1194 contract holds while CI is pending: N identical refusals reach a human.

        A reviewer spawn refusing deterministically behind a pending gate must not sit out
        the six-hour rollup ceiling either; three sightings are enough for the policy.
        """
        from ummanu.dispatch.gate import GateResult
        from ummanu.dispatch.head_vitality_policy import (
            DEFAULT_DETERMINISTIC_REFUSAL_LIMIT,
        )

        self.start_dispatcher()
        self.host.gate_results = [GateResult("pending", "CI still running")] * 8
        self.tick()
        self.report_done()
        self.assertEqual(self.tick()["to"], "validate")
        self.tick()  # first pending stamp (no scripted worker status yet: plain wait)

        refusal_status = {
            "known": True,
            "live": True,
            "reason": "live",
            "last_activity": time.time(),
            "pid_confirmed": False,
            "provider_progress": {"state": "unavailable", "reason": "terminal_split_source_not_found"},
        }
        self.host.worker_status_result = dict(refusal_status)
        for _ in range(DEFAULT_DETERMINISTIC_REFUSAL_LIMIT):
            outcome = self.tick()

        self.assertEqual(outcome["action"], "worker-deterministic-refusal-escalated")
        comments = [
            str(comment.get("body") or "")
            for comment in self.reader.show(CARD_REF)["comments"]
            if isinstance(comment, dict) and "authoritative refusal" in str(comment.get("body") or "")
        ]
        self.assertEqual(len(comments), 1, comments)
        record = self.record_of()
        self.assertEqual(record.worker_respawns, 0)
        self.assertNotIn("restart_worker", self.host.calls)


class Issue02fe04d7RetainedWorkerTests(LegacyPathTests):
    """issue:02fe04d7cde3f31e8e56: the watchdog woke the worker the gate had just parked.

    On codegen-orchestrator-1248 (2026-09-02) the card retained its worker at 15:14:53, the
    vitality watchdog SIGCONT'd it at 15:17:32, CI came back red at 15:18:37, and at 15:18:51 the
    red continuation read ``retained worker session is no longer confirmably suspended`` and took
    ``replacement`` instead of ``reuse`` -- losing the provider conversation that wrote the code.
    Retention lasts two to three minutes and CI takes three to four, so the SIGCONT always won
    that race: not a flake, a fact with two owners.

    ``retain_worker``'s SIGSTOP and the watchdog's SIGCONT were reading the same ``/proc`` state
    ``T`` with opposite intentions. The reduction is now TOLD whose stop signal it is, so the
    parked process reduces to ``Retained`` and no rung is ever climbed over it.
    """

    def run_to_gate_pending(self, pending_ticks: int = 3) -> None:
        """Claim, report done (which retains the worker), and stamp a pending gate."""
        from ummanu.dispatch.gate import GateResult

        self.host.fail_resume_worker_reason = ""
        self.start_dispatcher()
        self.host.gate_results = [GateResult("pending", "CI still running") for _ in range(pending_ticks)]
        self.tick()
        self.report_done()
        self.assertEqual(self.tick()["to"], "validate")
        self.assertTrue(
            self.record_of().worker_continuation.retained,
            "report:done retains the worker: that is the retention this card is about",
        )
        self.assertEqual(self.tick()["action"], "gate-pending")

    def sigcont_comments(self) -> list[str]:
        return [
            str(comment.get("body") or "")
            for comment in self.reader.show(CARD_REF)["comments"]
            if isinstance(comment, dict) and "SIGCONT" in str(comment.get("body") or "")
        ]

    def test_a_retained_worker_survives_the_pending_gate_unwoken(self) -> None:
        """Several gate ticks over a deliberately parked worker send no recovery rung at all."""
        self.run_to_gate_pending(pending_ticks=4)
        self.host.worker_status_result = self.stopped_worker_status()

        for tick_number in range(3):
            with self.subTest(gate_tick=tick_number):
                outcome = self.tick()
                self.assertEqual(outcome["action"], "gate-pending")
                self.assertEqual(outcome["status"], "ok")

        record = self.record_of()
        episode = record.worker_vitality_episode
        assert episode is not None
        # Typed, not a boolean special case: head-status and every diagnostic that reads this
        # record see a retention, not a suspension awaiting recovery and not a suspected stall.
        self.assertEqual(episode.verdict, VitalityVerdict.RETAINED)
        self.assertIn("retained@pid_heartbeat", episode.basis)
        self.assertEqual(episode.recovery_rung, 0)
        self.assertEqual(self.sigcont_comments(), [])
        self.assertEqual(record.worker_respawns, 0)
        self.assertNotIn("restart_worker", self.host.calls)
        self.assertNotIn("stop_head:worker", self.host.calls)
        # The retention is still on file and still confirmable: the red verdict can reuse it.
        self.assertTrue(record.worker_continuation.retained)
        self.assertTrue(self.host.worker_retained_alive(record))

    def test_a_red_gate_over_an_unwoken_retention_reuses_the_session(self) -> None:
        """The incident's own outcome, corrected: `reuse`, not `replacement`.

        This test owns the first link of the incident chain -- no SIGCONT is sent, so the
        retention is still confirmable when the red verdict asks. The second link (a retention
        that really did lose its suspension is replaced, once, through a confirmed stop) is
        pinned unchanged by ``test_a_session_that_lost_its_suspension_before_the_red_gate_is
        _replaced_once`` in ``tests/test_dispatcher_launch_intent.py``.
        """
        from ummanu.dispatch.gate import GateResult

        self.run_to_gate_pending(pending_ticks=3)
        self.host.worker_status_result = self.stopped_worker_status()
        self.tick()
        self.tick()

        self.host.gate_results = [GateResult("red", "tests failed", log="boom")]
        red = self.tick()

        self.assertEqual(red["action"], "gate-red-reused-worker")
        self.assertEqual(self.host.calls.count("resume_worker"), 1)
        self.assertEqual(self.host.calls.count("restart_worker"), 0)
        bodies = [
            str(comment.get("body") or "")
            for comment in self.reader.show(CARD_REF)["comments"]
            if isinstance(comment, dict)
        ]
        self.assertTrue(
            any("gate red continuation: reused" in body for body in bodies),
            bodies[-3:],
        )
        self.assertFalse(any("gate red continuation: replacement" in body for body in bodies))
        self.assertEqual(self.sigcont_comments(), [])

    def test_a_retained_worker_that_died_is_still_seen_as_dead(self) -> None:
        """The change suppresses the wake, not the truth: death still outranks the retention."""
        self.run_to_gate_pending(pending_ticks=4)
        self.host.worker_status_result = {
            "known": True,
            "live": False,
            "reason": "gone",
            "last_activity": time.time(),
            "pid_confirmed": False,
            "idle": False,
            "pid_status": {
                "known": True,
                "alive": False,
                "match": False,
                "state": "dead",
                "stopped": False,
            },
        }

        self.tick()

        episode = self.record_of().worker_vitality_episode
        assert episode is not None
        self.assertEqual(episode.verdict, VitalityVerdict.DEAD)
