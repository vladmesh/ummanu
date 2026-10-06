"""Tick-level decision tests for the wait tick's verdict table (card S1-4).

The card requires the wait-tick decision to be tested per verdict, parametrised over the
whole ladder, because the decision -- not the guard alone -- is what an operator sees
tick after tick. Each case drives the same scenario to a known verdict and asserts the
tick outcome; the Suspended cases additionally pin the arm's own contract: exactly one
comment per freeze span (the request-id is derived from the frozen span, so a re-suspension
with a new span writes again, while every tick inside one span replays the same id),
never a signal, never a destructive host call, and no renewal of the outer wait clock.
"""

from __future__ import annotations

import time
import unittest
from typing import ClassVar
from unittest import mock

from tests.dispatcher_fixtures import CARD_REF, RUNNING_STATUS, STOPPED_STATUS, DispatcherRuntimeFixture
from ummanu.dispatch import wait_vitality as wait_vitality_module
from ummanu.dispatch.types import HostError
from ummanu.dispatch.watchdog import idle_stall_seconds, stall_seconds
from ummanu.dispatch.worker_lifecycle import head_run_binding


def _suspension_comments(case) -> list[str]:
    """The durable suspension comments on the pilot card, as bodies."""
    return [
        str(comment.get("body") or "")
        for comment in case.reader.show(CARD_REF)["comments"]
        if isinstance(comment, dict) and "stop signal" in str(comment.get("body") or "")
    ]


class SuspendedWaitTickDecisionTests(DispatcherRuntimeFixture, unittest.TestCase):
    """The Suspended arm of ``_decide_wait_by_verdict``, through the S1-5 policy.

    The card's own contract, per freeze span: exactly one SIGCONT (the request-id is
    derived from the span stamp, so a re-suspension with a new span fires again while
    every tick inside one span replays the same id), one comment per policy action, never
    a signal beyond SIGCONT, and no renewal of the outer wait clock.
    """

    def setUp(self) -> None:
        super().setUp()
        self.start_dispatcher()
        self.tick()  # claim + launch; the worker heartbeat binds a live pid
        self.host.worker_status_result = dict(STOPPED_STATUS)

    def test_a_suspended_head_gets_one_sigcont_one_comment_and_no_stop(self) -> None:
        """Observe -> decide: one identity-fenced SIGCONT names the span; nothing is stopped."""
        first = self.tick()
        self.assertEqual(first["action"], "worker-sigcont-sent")
        episode = self._pilot_record()["worker_vitality_episode"]
        self.assertEqual(episode["verdict"], "suspended")
        # Rung 3 = response window running after the send; the span key is stamped.
        self.assertEqual(episode["recovery_rung"], 3)
        self.assertEqual(episode["recovery_span_started_at"], episode["stall_frozen_since"])

        decided = self.tick()

        # Inside the window the policy observes: same comment count, no second signal.
        self.assertEqual(decided["action"], "worker-suspension-observed")
        comments = _suspension_comments(self)
        self.assertEqual(len(comments), 1, comments)
        self.assertIn("parked on a stop signal", comments[0])
        self.assertIn("identity-fenced SIGCONT", comments[0])
        record = self._pilot_record()
        self.assertEqual(record["worker_respawns"], 0)
        self.assertNotIn("restart_worker", self.host.calls)
        signals = [call for call in self.host.calls if "signal" in call.lower()]
        self.assertEqual(signals, [])

    def test_the_suspension_comment_does_not_renew_the_outer_wait_clock(self) -> None:
        """A suspended head must not look like fresh progress: the window stays put."""
        before = self._pilot_record()["worker_waiting_since"]
        self.tick()
        self.tick()
        self.assertEqual(
            self._pilot_record()["worker_waiting_since"],
            before,
            "renewing the wait window would hide the stall behind a fresh clock",
        )

    def test_inside_one_freeze_span_the_comment_is_written_once(self) -> None:
        """Every tick in one span re-derives the same request-id: the board sees one."""
        for _ in range(4):
            self.tick()
        self.assertEqual(len(_suspension_comments(self)), 1)

    def test_a_re_suspension_with_a_new_freeze_span_writes_again(self) -> None:
        """Thaw and re-freeze: the new span's key differs, so the operator hears again."""
        self.tick()
        self.tick()
        self.assertEqual(len(_suspension_comments(self)), 1)

        self.host.worker_status_result = dict(RUNNING_STATUS)
        thawed = self.tick()
        self.assertEqual(thawed["action"], "waiting-worker-report")
        episode = self._pilot_record()["worker_vitality_episode"]
        self.assertEqual(episode["verdict"], "healthy_quiet")
        self.assertEqual(episode["stall_frozen_since"], 0.0)
        # The recovered span cleared the policy ladder with it: a future suspension
        # starts fresh instead of inheriting this span's rungs.
        self.assertEqual(episode["recovery_rung"], 0)
        self.assertEqual(episode["recovery_span_started_at"], 0.0)
        self.assertEqual(len(_suspension_comments(self)), 1, "a thaw writes nothing")

        # Re-freeze inside a different wall-clock second so the span (and its key) moves.
        time.sleep(1.1)
        self.host.worker_status_result = dict(STOPPED_STATUS)
        for _ in range(3):
            self.tick()
        self.assertGreaterEqual(
            len(_suspension_comments(self)),
            2,
            "the second freeze span must reach the operator as its own comment",
        )

    def test_recovery_clears_the_rung_on_the_right_role_only(self) -> None:
        """The rung reset lands on the suspended role's own episode slot -- never across.

        Card S1-6 regression: ``_run_recovery_policy`` used to persist the reset with a
        hardcoded ``kind="worker"``, so a RECOVERED review head left its cleared episode
        (bound to the review run's run_id) parked on ``worker_vitality_episode`` while
        ``review_vitality_episode.recovery_rung`` stayed stale. A foreign-run episode in
        the worker slot is fail-safe (the destructive guard refuses it as FOREIGN_RUN)
        but wrong: the review ladder never reset, and the worker slot held another
        run's state.
        """
        # Worker first, so both roles hold episodes and neither slot starts empty.
        self.host.worker_status_result = dict(STOPPED_STATUS)
        self.tick()
        worker_suspended = self._pilot_record()["worker_vitality_episode"]
        self.assertEqual(worker_suspended["verdict"], "suspended")
        self.assertGreater(worker_suspended["recovery_rung"], 0)

        # The worker resumes: the WORKER mirror of the fix -- its own slot clears.
        self.host.worker_status_result = dict(RUNNING_STATUS)
        worker_resumed = self.tick()

        self.assertEqual(worker_resumed["action"], "waiting-worker-report")
        worker_cleared = self._pilot_record()["worker_vitality_episode"]
        self.assertEqual(worker_cleared["verdict"], "healthy_quiet")
        self.assertEqual(worker_cleared["recovery_rung"], 0)
        self.assertEqual(
            worker_cleared["run_id"],
            worker_suspended["run_id"],
            "the same worker episode is updated in place",
        )

        self._run_worker_to_validate()
        self.assertEqual(self.tick()["action"], "review-started")
        self.host.review_status_result = dict(STOPPED_STATUS)
        self.tick()
        review_suspended = self._pilot_record()["review_vitality_episode"]
        worker_before = self._pilot_record()["worker_vitality_episode"]
        self.assertEqual(review_suspended["verdict"], "suspended")
        self.assertGreater(review_suspended["recovery_rung"], 0)

        # The review head resumes: its own slot must be the one that clears.
        self.host.review_status_result = dict(RUNNING_STATUS)
        resumed = self.tick()

        self.assertEqual(resumed["action"], "waiting-review-verdict")
        record = self._pilot_record()
        review_after = record["review_vitality_episode"]
        worker_after = record["worker_vitality_episode"]
        self.assertEqual(review_after["recovery_rung"], 0)
        self.assertEqual(review_after["recovery_span_started_at"], 0.0)
        self.assertEqual(
            review_after["run_id"],
            review_suspended["run_id"],
            "the same review episode is updated, not replaced by a foreign one",
        )
        self.assertEqual(
            worker_after,
            worker_before,
            "a review recovery must not touch the worker's episode slot",
        )

    def test_a_suspended_review_head_gets_the_same_arm(self) -> None:
        """The review twin decides identically: comment once, no stop, no replacement."""
        self._run_worker_to_validate()
        self.assertEqual(self.tick()["action"], "review-started")
        self.host.review_status_result = dict(STOPPED_STATUS)

        self.tick()
        decided = self.tick()

        # The review twin runs the identical policy: one SIGCONT, then in-window observes.
        self.assertEqual(decided["action"], "review-suspension-observed")
        comments = [
            str(comment.get("body") or "")
            for comment in self.reader.show(CARD_REF)["comments"]
            if isinstance(comment, dict) and "Vitality (review)" in str(comment.get("body") or "")
        ]
        self.assertTrue(any("stop signal" in body for body in comments), comments)
        self.assertNotIn("stop_review", self.host.calls)


class WaitTickVerdictTableTests(DispatcherRuntimeFixture, unittest.TestCase):
    """One scenario per remaining verdict: the outcome the tick hands back.

    The ConfirmedStall/Dead arms are covered extensively by the runtime suite (prompt /
    respawn / reclaim) and the guard call-site tests; this table pins the non-destructive
    rows so that disabling any single arm fails here by name.
    """

    def setUp(self) -> None:
        super().setUp()
        self.start_dispatcher()
        self.tick()

    def _decide_with(self, status: dict) -> dict:
        self.host.worker_status_result = dict(status)
        self.tick()  # the reduction observes and stores the verdict
        return self.tick()  # the decision tick

    def _advancing_status(self) -> dict:
        """A live head whose provider cursor advanced since the previous snapshot.

        The cursor evidence must carry this run's HeadRun binding (the reducer refuses a
        cursor naming another run), and the cursor value itself must differ from the
        previous snapshot's for the source to read Advancing rather than Quiet.
        """
        record = self._pilot_record()
        run_id, fingerprint = head_run_binding(record["worker_head_run"])
        status = dict(RUNNING_STATUS)
        status["provider_progress"] = {
            "state": "observed",
            "admission": "accepted",
            "source": "fake-bound-session",
            "source_fingerprint": "f" * 32,
            "cursor": f"rollout:{time.time()}",
            "head_run_id": run_id,
            "head_run_fingerprint": fingerprint,
        }
        return status

    def test_healthy_active_waits_without_touching_anything(self) -> None:
        self.host.worker_status_result = self._advancing_status()
        self.tick()  # first observation: stores the cursor, still Quiet
        self.host.worker_status_result = self._advancing_status()  # cursor moves again
        outcome = self.tick()  # Advancing
        self.assertEqual(outcome["action"], "waiting-worker-report")
        self.assertEqual(outcome["status"], "ok")
        episode = self._pilot_record()["worker_vitality_episode"]
        self.assertEqual(episode["verdict"], "healthy_active")

    def test_healthy_quiet_renews_the_window_and_waits(self) -> None:
        outcome = self._decide_with(dict(RUNNING_STATUS))
        self.assertEqual(outcome["action"], "waiting-worker-report")
        episode = self._pilot_record()["worker_vitality_episode"]
        self.assertEqual(episode["verdict"], "healthy_quiet")
        self.assertEqual(self._pilot_record()["worker_respawns"], 0)

    def test_unverifiable_waits_instead_of_guessing(self) -> None:
        status = {
            "known": False,
            "live": True,
            "reason": "runtime-unavailable",
        }
        outcome = self._decide_with(status)
        self.assertIn(
            outcome["action"],
            ("worker-runtime-unavailable", "waiting-worker-report"),
        )
        self.assertEqual(self._pilot_record()["worker_respawns"], 0)

    def test_suspected_stall_nudges_exactly_once_then_degrades(self) -> None:
        self._decide_with(dict(RUNNING_STATUS))  # baseline quiet below every threshold
        # Age the persisted quiet past suspect_after but short of confirm_after.
        self._age_vitality_quiet("worker", 500.0)
        first = self.tick()
        # Assert the verdict instead of skipping (S1-4 review follow-up): a fixture that
        # cannot reach suspected_stall is a broken test, not an inapplicable one.
        episode = self._pilot_record()["worker_vitality_episode"]
        self.assertEqual(
            episode["verdict"],
            "suspected_stall",
            f"the fixture aged quiet to {episode['verdict']} instead of suspected_stall",
        )
        self.assertEqual(first["action"], "worker-stall-suspected")
        self.assertEqual(first["status"], "degraded")
        prompts = self.host.calls.count("prompt_worker_report")
        again = self.tick()
        self.assertEqual(again["action"], "worker-stall-suspected")
        self.assertEqual(
            self.host.calls.count("prompt_worker_report"),
            prompts,
            "a suspicion spends at most one nudge per round generation",
        )
        self.assertEqual(self._pilot_record()["worker_respawns"], 0)

    def test_disabling_the_suspended_arm_is_caught_by_this_table(self) -> None:
        """Mutation check: with the arm disabled the suspended head must not be destroyed.

        The guard would refuse a disabled arm's drift into the ceiling path -- but the
        refusal is itself observable. This test pins the arm positively: the outcome is
        the policy's in-window observation, not a guard refusal and not a respawn.
        """
        outcome = self._decide_with(dict(STOPPED_STATUS))
        self.assertEqual(outcome["action"], "worker-suspension-observed")
        self.assertNotIn("guard-refused", str(outcome.get("action")))
        # And the arm really produced the SIGCONT through the real code path.
        self.assertEqual(len(_suspension_comments(self)), 1)


class UnobservableWaitEscalationTests(DispatcherRuntimeFixture, unittest.TestCase):
    """The no-episode fallback's bound: operator escalation, never replacement.

    An unobservable head (heartbeat unreadable, provider dark, nothing on file) is a run
    nobody can prove dead, so the guard refuses its destruction forever. The wait is
    still bounded: once the outer ceiling elapses, ``_escalate_unobservable_wait`` writes
    one durable comment per wait cycle and hands back a degraded outcome. These tests
    catch a mutation that deletes that bound (the tick would return to a silent ``ok``
    wait) and one that turns it destructive.
    """

    def setUp(self) -> None:
        super().setUp()
        self.start_dispatcher()
        self.tick()  # claim + launch

    def _unobservable_status(self) -> None:
        """Make the runtime inventory fail: the watchdog sees no sources at all.

        The fake derives pid/provider evidence for any scripted dict (that realism is
        what other tests rely on), so an unobservable head is modelled the way production
        produces one -- ``worker_status`` raising, which ``_wait_watchdog`` catches into a
        source-less status and the reduction into no episode.
        """
        self.host.worker_status_error = HostError("orca terminal list failed")

    def test_ceiling_elapsed_escalates_to_the_operator_without_touching_the_head(self) -> None:
        from ummanu.dispatch.watchdog import stall_seconds

        self.host.worker_status_result = self._unobservable_status()
        self.tick()  # stamps the fresh waiting window
        payload = self.runtime.production_state.load()
        record_payload = payload["records"][CARD_REF]
        record_payload["worker_waiting_since"] -= stall_seconds("worker") + 60
        self.runtime.production_state.save(payload)

        outcome = self.tick()

        self.assertEqual(outcome["action"], "worker-unobserved-wait-escalated")
        self.assertEqual(outcome["status"], "degraded")
        comments = [
            str(comment.get("body") or "")
            for comment in self.reader.show(CARD_REF)["comments"]
            if isinstance(comment, dict) and "NOT stopped or replaced" in str(comment.get("body") or "")
        ]
        self.assertEqual(
            len(comments),
            1,
            "exactly one escalation per unobserved wait cycle",
        )
        self.assertNotIn("restart_worker", self.host.calls)
        self.assertNotIn("stop_head:worker", self.host.calls)

    def test_below_the_ceiling_an_unobservable_head_is_a_plain_wait(self) -> None:
        self.host.worker_status_result = self._unobservable_status()
        first = self.tick()
        second = self.tick()
        for outcome in (first, second):
            self.assertEqual(outcome["action"], "worker-runtime-unavailable") if outcome.get(
                "reason"
            ) else None
            self.assertNotEqual(
                outcome.get("action"),
                "worker-unobserved-wait-escalated",
                "escalation must not fire before the ceiling",
            )

    def test_disabling_the_escalation_is_caught(self) -> None:
        """Mutation check: deleting the bound returns an unobservable head to silence."""
        from ummanu.dispatch.watchdog import stall_seconds

        self.host.worker_status_result = self._unobservable_status()
        self.tick()
        payload = self.runtime.production_state.load()
        record_payload = payload["records"][CARD_REF]
        record_payload["worker_waiting_since"] -= stall_seconds("worker") + 60
        self.runtime.production_state.save(payload)

        with (
            mock.patch.object(
                wait_vitality_module,
                "_escalate_unobservable_wait",
                side_effect=AssertionError("the escalation arm was disabled"),
            ),
            self.assertRaises(AssertionError),
        ):
            self.tick()

    def test_a_turning_the_escalation_destructive_is_caught(self) -> None:
        """The escalation may not grow a stop: any host call fails this test."""
        from ummanu.dispatch.watchdog import stall_seconds

        self.host.worker_status_result = self._unobservable_status()
        self.tick()
        payload = self.runtime.production_state.load()
        record_payload = payload["records"][CARD_REF]
        record_payload["worker_waiting_since"] -= stall_seconds("worker") + 60
        self.runtime.production_state.save(payload)

        calls_before = list(self.host.calls)
        self.tick()
        new_calls = [call for call in self.host.calls[len(calls_before) :]]
        self.assertEqual(
            [call for call in new_calls if "restart" in call or "stop" in call],
            [],
            f"the unobservable escalation signalled or replaced something: {new_calls}",
        )


class VitalityVerdictCommentIdempotencyTests(DispatcherRuntimeFixture, unittest.TestCase):
    """The verdict comment's body must be a function of exactly what its request id names.

    secretary-1477. The id keys the transition pair alone -- deliberately, so a flapping
    verdict cannot mint a fresh key every tick -- while the board's identity for a comment
    is the digest of its body. Quoting the reduction's live ``basis`` (``quiet:<n>s@...``)
    in that body therefore made the SECOND visit to one pair a different payload under the
    same key, which the write path answers with
    ``validation: request id belongs to another operation or payload``. That exception
    leaves ``_reduce_and_store_vitality_episode`` before ``_decide_wait_by_verdict`` ever
    runs, so the card loses its whole per-card advance for the tick.

    These tests drive the real writer (no stub, no patched ``TaskWriter``): the second visit
    to a pair has to be an honest idempotent replay.
    """

    def setUp(self) -> None:
        super().setUp()
        self.start_dispatcher()
        self.tick()  # claim + launch: the worker heartbeat binds a live pid

    def _cursor_status(self, cursor: str) -> dict:
        """A live head whose provider cursor reads exactly ``cursor``.

        Repeating a cursor is what makes the source read Quiet rather than Advancing, which
        is how these tests flap one head between the two healthy verdicts on demand.
        """
        record = self._pilot_record()
        run_id, fingerprint = head_run_binding(record["worker_head_run"])
        status = dict(RUNNING_STATUS)
        status["provider_progress"] = {
            "state": "observed",
            "admission": "accepted",
            "source": "fake-bound-session",
            "source_fingerprint": "f" * 32,
            "cursor": cursor,
            "head_run_id": run_id,
            "head_run_fingerprint": fingerprint,
        }
        return status

    def _age_progress_reference(self, seconds: float) -> None:
        """Move the episode's quiet reference back, so the next quiet reduction measures more.

        The live measurement this card is about is ``now - last_progress_at``; two ticks in one
        test process are milliseconds apart, so without this the two visits to a pair would
        agree by accident and prove nothing.
        """
        payload = self.runtime.production_state.load()
        episode = payload["records"][CARD_REF]["worker_vitality_episode"]
        for name in ("started_at", "last_progress_at"):
            if episode.get(name):
                episode[name] -= seconds
        self.runtime.production_state.save(payload)

    def _flap_twice_through_one_pair(self) -> tuple[list, list]:
        """Visit ``healthy_active -> healthy_quiet`` twice with different quiet measurements.

        Hands back (every comment call the ticks made, the two calls that share the repeated
        pair's request id). The ticks run through the real ``TaskWriter``; the spy wraps it
        rather than replacing it, so the idempotency journal is exercised exactly as in
        production.
        """
        with mock.patch.object(
            self.writer,
            "comment",
            wraps=self.writer.comment,
        ) as spy:
            self.host.worker_status_result = self._cursor_status("rollout:1")
            self.tick()  # first observation: the cursor is stored, nothing advanced yet
            self.host.worker_status_result = self._cursor_status("rollout:2")
            self.tick()  # the cursor moved: healthy_active
            self.host.worker_status_result = self._cursor_status("rollout:2")
            self.tick()  # the same cursor: healthy_quiet, quiet measured from the move
            self.host.worker_status_result = self._cursor_status("rollout:3")
            self.tick()  # advancing again: back to healthy_active
            self._age_progress_reference(41)
            self.host.worker_status_result = self._cursor_status("rollout:3")
            # The second visit to healthy_active -> healthy_quiet, with a quiet span that is
            # 41s longer than the first visit's. Before the fix this raised.
            self.tick()
        calls = list(spy.call_args_list)
        repeated = [
            call
            for call in calls
            if str(call.kwargs.get("request_id") or "").endswith(
                "worker-vitality-verdict-" + CARD_REF + "-healthy_active--healthy_quiet"
            )
        ]
        return calls, repeated

    def test_one_transition_pair_writes_one_body_byte_for_byte(self) -> None:
        """Acceptance 1: the same pair in a later tick produces an identical body."""
        _, repeated = self._flap_twice_through_one_pair()

        self.assertEqual(
            len(repeated),
            2,
            "the flap must visit healthy_active -> healthy_quiet twice for this to prove anything",
        )
        first, second = (str(call.kwargs["body"]) for call in repeated)
        self.assertEqual(first, second)
        self.assertNotIn("quiet:", first, "a live measurement in the body is what broke the key")

    def test_the_repeat_is_an_idempotent_replay_not_a_validation_refusal(self) -> None:
        """Acceptance 2: the real write path answers the repeat without raising.

        Nothing is stubbed here -- the fixture's own ``TaskWriter`` and audit journal decide.
        A refusal would surface as ``TaskError('validation', ...)`` escaping ``self.tick()``.
        """
        self._flap_twice_through_one_pair()

        bodies = [
            str(comment.get("body") or "")
            for comment in self.reader.show(CARD_REF)["comments"]
            if "Vitality (worker):" in str(comment.get("body") or "")
        ]
        # Three distinct pairs were visited (none->quiet, quiet->active, active->quiet) and the
        # fourth and fifth ticks revisited two of them: the board holds one comment per pair.
        self.assertEqual(len(bodies), 3, bodies)

    def test_every_repeated_key_carries_the_same_payload(self) -> None:
        """Acceptance 3 (anti-flood): the key is unchanged, so the pair set still bounds the
        stream -- and no key on this path is ever re-used for a different payload."""
        calls, _ = self._flap_twice_through_one_pair()

        by_request: dict[str, set[str]] = {}
        for call in calls:
            request_id = str(call.kwargs.get("request_id") or "")
            if "-vitality-verdict-" not in request_id:
                continue
            by_request.setdefault(request_id, set()).add(str(call.kwargs.get("body") or ""))
        self.assertTrue(by_request)
        for request_id, seen in by_request.items():
            self.assertEqual(len(seen), 1, f"{request_id} was written with two payloads: {seen}")
        self.assertEqual(
            len(by_request),
            3,
            "five ticks may only ever name the pairs they actually visited",
        )


class Ummanu1517WaitTickTests(DispatcherRuntimeFixture, unittest.TestCase):
    """End to end, through the real wait tick: the shape `issue:7bff833fef6d9d9b404d` froze in.

    A Codex worker head whose provider source has no bound v1 baseline (so the cursor answers
    unavailable), a live PID, an idle pane, and no file or event progress. Before secretary-1543
    the reduction returned ``healthy_quiet`` with the freeze reason on every tick for as long as
    the PID lived: 65+ minutes claimed, no wake, no replacement, no terminal outcome.
    """

    #: The dark window and the quiet window both have to elapse before anything is earned.
    DARK_CEILING = 600.0

    def setUp(self) -> None:
        super().setUp()
        # An addressable head: the wake this card is about is a real report prompt typed into a
        # live conversation, not the degraded fallback an unaddressable fixture head produces.
        self.host.fail_resume_worker_reason = ""
        self.start_dispatcher()
        self.tick()  # claim + launch; the worker heartbeat binds a live pid
        self._codex_head_with_no_provider_baseline()

    def _codex_head_with_no_provider_baseline(self) -> None:
        self.host.worker_status_result = {
            "known": True,
            "live": True,
            "reason": "live",
            "pid_confirmed": True,
            # The pane answers idle throughout -- which, per secretary-1542's measurement against
            # real Codex, does not distinguish a starting head from a settled one.
            "idle": True,
            "provider_progress": {
                "state": "unavailable",
                "reason": "codex provider source has no bound v1 baseline for this HeadRun",
            },
        }

    def _age(self, seconds: float) -> None:
        """Age the episode's quiet reference AND its dark stamp, as wall-clock would."""
        payload = self.runtime.production_state.load()
        record = payload["records"][CARD_REF]
        episode = record.get("worker_vitality_episode")
        assert episode is not None
        for name in ("started_at", "quiet_since", "updated_at", "last_progress_at", "suspected_since"):
            if episode.get(name):
                episode[name] -= seconds
        episode["unavailable_since"] = {
            name: stamp - seconds for name, stamp in (episode.get("unavailable_since") or {}).items()
        }
        record["worker_vitality_episode"] = episode
        self.runtime.production_state.save(payload)

    def test_the_frozen_head_is_woken_instead_of_sitting_claimed(self) -> None:
        frozen = self.tick()
        self.assertEqual(frozen["action"], "waiting-worker-report")
        episode = self._pilot_record()["worker_vitality_episode"]
        self.assertEqual(episode["verdict"], "healthy_quiet")
        self.assertIn("provider_cursor", episode["unavailable_since"])

        # Ten minutes later -- past the dark ceiling and past `suspect_after`.
        self._age(self.DARK_CEILING + 60.0)
        woken = self.tick()

        self.assertEqual(woken["action"], "worker-report-prompted")
        self.assertEqual(woken["status"], "degraded")
        episode = self._pilot_record()["worker_vitality_episode"]
        # The prompt restarts the quiet clock on purpose, so the persisted verdict after the wake
        # is healthy again; what matters is that the wake happened, and why it happened.
        self.assertIn("suspects a stall", woken["reason"])
        self.assertIn("provider_cursor has been dark for", woken["reason"])
        # Nothing was stopped or replaced to get there.
        self.assertEqual(self._pilot_record()["worker_respawns"], 0)
        self.assertNotIn("restart_worker", self.host.calls)

    def test_the_head_is_never_destroyed_on_the_dark_channel_alone(self) -> None:
        """Past confirmation the guard still holds the destructive rung behind the ceiling."""
        self.tick()
        self._age(self.DARK_CEILING + 60.0)
        self.assertEqual(self.tick()["action"], "worker-report-prompted")

        # The nudge is spent for this generation; the head stays dark and quiet.
        outcomes = []
        for _ in range(3):
            self._age(20 * 60.0)
            outcomes.append(self.tick())

        episode = self._pilot_record()["worker_vitality_episode"]
        self.assertEqual(episode["verdict"], "confirmed_stall")
        # Every tick still says something: the card is never silently claimed with nothing
        # running. And nothing destructive fired -- the confirmation rests on the pid alone,
        # so the guard holds it behind the role's outer ceiling.
        for outcome in outcomes:
            self.assertEqual(outcome["status"], "degraded", outcome)
        self.assertEqual(self._pilot_record()["worker_respawns"], 0)
        self.assertNotIn("restart_worker", self.host.calls)
        self.assertIn(
            "guard-refused",
            " ".join(str(outcome.get("action") or "") for outcome in outcomes),
        )

    def test_a_retained_head_in_the_same_shape_is_left_alone(self) -> None:
        """A deliberately parked worker is exempt from every rung this card can reach."""
        payload = self.runtime.production_state.load()
        record = payload["records"][CARD_REF]
        record["worker_continuation"] = {
            "stage": "retained",
            "phase": "validate",
            "retained_at": time.time(),
            "session_held": True,
        }
        self.runtime.production_state.save(payload)
        self.host.worker_status_result["pid_status"] = {
            "known": True,
            "alive": True,
            "match": True,
            "state": "live-match",
            "stopped": True,
        }

        self.tick()
        self._age(90 * 60.0)
        outcome = self.tick()

        episode = self._pilot_record()["worker_vitality_episode"]
        self.assertEqual(episode["verdict"], "retained")
        self.assertEqual(episode["recovery_rung"], 0)
        self.assertNotEqual(outcome.get("action"), "worker-report-prompted")
        self.assertEqual(self._pilot_record()["worker_respawns"], 0)
        self.assertNotIn("restart_worker", self.host.calls)


class RejectedReportAnswerOwedTests(DispatcherRuntimeFixture, unittest.TestCase):
    """secretary-1543: a bounced done report arms an explicit signal, not a ceiling."""

    def _record_field(self) -> float:
        return float(self._pilot_record().get("worker_answer_owed_since") or 0.0)

    def test_a_bounced_done_report_arms_the_signal_and_an_accepted_one_disarms_it(self) -> None:
        from tests.fakes.dispatcher import GateResult

        self.start_dispatcher()
        self.host.gate_results = [GateResult("red", "local validation failed", "assert False")]
        self._run_worker_to_validate()
        self.assertEqual(self.tick()["action"], "gate-red-rework")
        self.assertEqual(self._record_field(), 0.0)

        # The same SHA reported done again: the dispatcher bounces it back to rework.
        self.writer.report(
            role="worker",
            actor="worker",
            reference=CARD_REF,
            kind="done",
            body="nothing changed",
            request_id=self._worker_report_request_id(),
        )
        self.assertEqual(self.tick()["action"], "stale-done-rework")

        owed = self._record_field()
        self.assertGreater(owed, 0.0, "the head now owes the dispatcher an answer")

        # Real new work, accepted: whatever the head owed, it has answered.
        self.host.commit = "b" * 40
        self.writer.report(
            role="worker",
            actor="worker",
            reference=CARD_REF,
            kind="done",
            body="reworked",
            request_id=self._worker_report_request_id(),
        )
        self.assertEqual(self.tick()["to"], "validate")
        self.assertEqual(self._record_field(), 0.0)

    def test_the_owed_answer_reaches_the_reduction_as_a_declared_input(self) -> None:
        """The wiring, not the arithmetic: the reducer's own tests own the conclusion."""
        self.start_dispatcher()
        self.tick()
        payload = self.runtime.production_state.load()
        payload["records"][CARD_REF]["worker_answer_owed_since"] = time.time() - 30.0
        self.runtime.production_state.save(payload)
        seen: list[float] = []
        real = _reduce_vitality_under_test()

        def spy(previous, snapshots, now, thresholds, *, retained=False, answer_owed_since=0.0, **declared):
            seen.append(answer_owed_since)
            return real(
                previous,
                snapshots,
                now,
                thresholds,
                retained=retained,
                answer_owed_since=answer_owed_since,
                **declared,
            )

        with mock.patch("ummanu.dispatch.wait_vitality._reduce_vitality", spy):
            self._head_at_its_prompt()
            self.tick()

        self.assertTrue(seen)
        self.assertGreater(seen[0], 0.0)


def _reduce_vitality_under_test():
    from ummanu.dispatch.head_vitality_episode import reduce_vitality

    return reduce_vitality



class ProviderLessStatusShapesTests(DispatcherRuntimeFixture, unittest.TestCase):
    """The live-heartbeat status shape that carries no provider channel at all.

    `command_terminal_status` probes the provider only for a supervised head, so a legacy record's
    exact live heartbeat carries a `pid_status` and *no* `provider_progress` key. (Until
    secretary-1723 a second such shape existed, a matched Orca pane that was not connected; the
    pane inventory is gone, and with it that shape.) A head whose progress channel went dark is
    precisely the head the vitality ladder must not kill, so the shape is pinned here as the
    production function actually produces it -- not as a fixture wishes it -- and then driven
    through the reduction and the guard.

    Before secretary-1543's round 2 an absent provider channel left `unavailable_since` empty
    while the cursor from the tick that did answer stayed on file, so the episode read as
    "witnessed and not dark": the freeze window was skipped, the guard counted one channel as two
    and a live head was respawnable fifteen minutes after its last provider advance.
    """

    LIVE_MATCH: ClassVar[dict] = {
        "known": True,
        "alive": True,
        "match": True,
        "state": "live-match",
        "stopped": False,
    }

    def setUp(self) -> None:
        super().setUp()
        # Addressable, so the conversational rung this card protects is a real report prompt.
        self.host.fail_resume_worker_reason = ""
        self.start_dispatcher()
        self.tick()  # claim + launch; the worker heartbeat binds a live pid

    def _live_record(self):
        payload = self.runtime.production_state.load()
        return self.runtime.production_state.records(payload)[CARD_REF]

    def _production_status(self) -> dict:
        """What the real `command_terminal_status` returns for a legacy record's live heartbeat.

        Only the /proc heartbeat probe is stubbed. Everything that decides the shape is the
        production function.
        """
        from ummanu.dispatch import review as dispatcher_review

        record = self._live_record()
        # These are the shapes of a head that was an Orca pane, which only a legacy record still
        # names (secretary-1722); a supervised head's pid shape carries its provider cursor.
        record.worker_head_run = {**record.worker_head_run, "head_runtime": "orca-legacy"}
        host = mock.Mock(mode="orca")
        with mock.patch.object(
            dispatcher_review, "_head_run_process_status", return_value=dict(self.LIVE_MATCH)
        ):
            return dispatcher_review.command_terminal_status(
                host, self.reader.show(CARD_REF), record, kind="worker"
            )

    def _witness_the_provider(self) -> None:
        """Ticks on which the provider answers and advances, as the real incident's head did.

        Two cursors, so the episode carries a real ``last_progress_at`` as well as an evidence
        cursor: that is the state in which the old nudge bought no grace at all, and the state
        the reviewer's reproduction started from.
        """
        for cursor in ("cursor-a", "cursor-b"):
            record = self._live_record()
            self.host.worker_status_result = {
                "known": True,
                "live": True,
                "reason": "live",
                "pid_confirmed": True,
                "pid_status": dict(self.LIVE_MATCH),
                "provider_progress": self._bound_provider_progress(record, cursor),
            }
            self.tick()
        episode = self._pilot_record()["worker_vitality_episode"]
        self.assertIn("provider_cursor", episode["evidence_cursors"])
        self.assertGreater(episode["last_progress_at"], 0.0)
        self.assertEqual(episode["unavailable_since"], {})

    def _lose_the_pane(self) -> None:
        """The provider stops answering for this head: one tick on the provider-less shape."""
        self.host.worker_status_result = self._production_status()
        self.tick()
        episode = self._pilot_record()["worker_vitality_episode"]
        self.assertIn("provider_cursor", episode["unavailable_since"])

    def _age(self, seconds: float) -> None:
        """Age every clock this episode reads, as wall-clock would."""
        payload = self.runtime.production_state.load()
        record = payload["records"][CARD_REF]
        episode = record.get("worker_vitality_episode")
        assert episode is not None
        for name in ("started_at", "quiet_since", "updated_at", "last_progress_at", "suspected_since"):
            if episode.get(name):
                episode[name] -= seconds
        episode["unavailable_since"] = {
            name: stamp - seconds for name, stamp in (episode.get("unavailable_since") or {}).items()
        }
        record["worker_vitality_episode"] = episode
        self.runtime.production_state.save(payload)

    def test_the_shape_carries_a_heartbeat_and_no_provider_channel(self) -> None:
        """Pinned from the production function: this is the wiring the reduction really sees."""
        lost_pane = self._production_status()
        self.assertEqual(lost_pane["reason"], "pid")
        self.assertTrue(lost_pane["pid_confirmed"])
        self.assertIn("pid_status", lost_pane)
        self.assertNotIn("provider_progress", lost_pane)

    def test_an_absent_provider_channel_is_recorded_dark_not_answering(self) -> None:
        """The repair itself: darkness is read from the absence of an answer."""
        self._witness_the_provider()
        self.host.worker_status_result = self._production_status()
        self.tick()

        episode = self._pilot_record()["worker_vitality_episode"]
        self.assertIn("provider_cursor", episode["unavailable_since"])
        self.assertIn("absent@provider_cursor", episode["basis"])
        # Inside the window it is still healthy, and the reason says which source is
        # dark and for how long -- not "running with no progress evidence".
        self.assertEqual(episode["verdict"], "healthy_quiet")
        self.assertIn("provider_cursor", episode["reason"])

    def test_a_live_head_with_no_pane_binding_is_not_respawned_before_the_outer_ceiling(self) -> None:
        """The blocker, end to end: the reduction may confirm, the guard may not destroy.

        The sequence the reviewer recorded -- provider answers, then fifteen minutes of heartbeat
        alone -- now spends the conversational rung and then sits behind the role's own six-hour
        ceiling instead of stopping a head whose pane the inventory merely lost.
        """
        self._witness_the_provider()
        self._lose_the_pane()

        # Past the dark ceiling and past `suspect_after`: the one nudge, and nothing destructive.
        self._age(2 * idle_stall_seconds() + 60.0)
        woken = self.tick()
        self.assertEqual(woken["action"], "worker-report-prompted")
        self.assertIn("provider_cursor has been dark for", woken["reason"])

        # The nudge is spent for this generation; the head stays live, quiet and unreadable.
        outcomes = []
        for _ in range(3):
            self._age(20 * 60.0)
            outcomes.append(self.tick())

        episode = self._pilot_record()["worker_vitality_episode"]
        self.assertEqual(episode["verdict"], "confirmed_stall")
        for outcome in outcomes:
            self.assertEqual(outcome["status"], "degraded", outcome)
        self.assertIn(
            "guard-refused",
            " ".join(str(outcome.get("action") or "") for outcome in outcomes),
        )
        self.assertEqual(self._pilot_record()["worker_respawns"], 0)
        self.assertNotIn("restart_worker", self.host.calls)

    def test_a_retained_head_in_the_provider_less_shape_is_still_exempt(self) -> None:
        """The absent channel may not wake a head the dispatcher itself parked."""
        self._witness_the_provider()
        status = self._production_status()
        status["pid_status"] = {**self.LIVE_MATCH, "stopped": True}
        payload = self.runtime.production_state.load()
        payload["records"][CARD_REF]["worker_continuation"] = {
            "stage": "retained",
            "phase": "validate",
            "retained_at": time.time(),
            "session_held": True,
        }
        self.runtime.production_state.save(payload)
        self.host.worker_status_result = status

        self.tick()
        self._age(90 * 60.0)
        outcome = self.tick()

        episode = self._pilot_record()["worker_vitality_episode"]
        self.assertEqual(episode["verdict"], "retained")
        self.assertNotEqual(outcome.get("action"), "worker-report-prompted")
        self.assertEqual(self._pilot_record()["worker_respawns"], 0)
        self.assertNotIn("restart_worker", self.host.calls)

    def test_the_nudge_buys_the_head_real_grace_before_the_next_rung(self) -> None:
        """The rung, not a formality: the tick after a prompt may not re-confirm at once.

        The nudge used to rewrite ``started_at`` only, while the reducer measures quiet from the
        later of the last progress and the last restart, so an episode that had ever seen the
        provider advance was re-confirmed immediately -- removing the one conversational rung
        that stands between a quiet head and a respawn.
        """
        self._witness_the_provider()
        self._lose_the_pane()
        self._age(2 * idle_stall_seconds() + 60.0)
        self.assertEqual(self.tick()["action"], "worker-report-prompted")
        episode = self._pilot_record()["worker_vitality_episode"]
        self.assertGreater(episode["quiet_since"], 0.0)

        after = self.tick()

        self.assertEqual(after["action"], "waiting-worker-report")
        self.assertEqual(self._pilot_record()["worker_vitality_episode"]["verdict"], "healthy_quiet")
        self.assertEqual(self._pilot_record()["worker_respawns"], 0)

    def test_past_the_outer_ceiling_the_same_head_is_recovered(self) -> None:
        """Bounded, not merely held: the hold is the role's ceiling, and it does elapse."""
        self._witness_the_provider()
        self._lose_the_pane()
        self._age(2 * idle_stall_seconds() + 60.0)
        self.assertEqual(self.tick()["action"], "worker-report-prompted")
        self._age(stall_seconds("worker") + 600.0)

        outcome = self.tick()

        self.assertEqual(outcome["action"], "worker-respawned")
        self.assertEqual(self._pilot_record()["worker_respawns"], 1)


class IdleTurnOwedAnswerWaitTickTests(DispatcherRuntimeFixture, unittest.TestCase):
    """secretary-1739: the wait tick declares the owed answer and hands the journal cursor back.

    The reducer's own tests (``tests/test_local_pty_journal_turn.py``) own the idle-turn
    arithmetic; this pins the wiring: the worker's wait tick declares ``worker_started_at``, the
    journal reading reaches the reduction, and the gate path's call declares nothing.
    """

    LIVE_MATCH: ClassVar[dict] = {
        "known": True,
        "alive": True,
        "match": True,
        "state": "live-match",
        "stopped": False,
    }

    def test_the_owed_answer_is_the_phase_start_or_a_bounced_report(self) -> None:
        from ummanu.dispatch.state import DispatcherRecord

        record = DispatcherRecord(
            worker="w",
            workspace="/tmp/w",
            handle="",
            head="h",
            review_head="h",
            attempt_id="a",
            comment_baseline=0,
            review_baseline=0,
            state="in_progress",
            claimed_at=0.0,
        )
        record.worker_started_at = 100.0
        self.assertEqual(wait_vitality_module.answer_owed_since_for_wait(record, "worker"), 100.0)
        record.worker_answer_owed_since = 150.0
        self.assertEqual(wait_vitality_module.answer_owed_since_for_wait(record, "worker"), 150.0)
        record.review_started_at = 300.0
        self.assertEqual(wait_vitality_module.answer_owed_since_for_wait(record, "review"), 300.0)

    def test_the_wait_tick_confirms_an_idle_worker_and_the_gate_path_does_not(self) -> None:
        from ummanu.dispatch.head_vitality import SnapshotSource
        from ummanu.dispatch.head_vitality_episode import (
            DEFAULT_VITALITY_THRESHOLDS,
            VitalityVerdict,
            reduce_vitality,
        )

        self.start_dispatcher()
        self.tick()
        payload = self.runtime.production_state.load()
        record = payload["records"][CARD_REF]
        run_id = record["worker_head_run"]["run_id"]
        asked = time.time() - DEFAULT_VITALITY_THRESHOLDS.idle_turn_confirm_after - 120.0
        record["worker_started_at"] = asked
        self.runtime.production_state.save(payload)
        self.host.worker_status_result = {
            "known": True,
            "live": True,
            "reason": "pid",
            "pid_confirmed": True,
            "pid_status": dict(self.LIVE_MATCH),
            "supervisor_journal": {
                "state": "observed",
                "run_id": run_id,
                "turn": "idle",
                "turn_since": asked + 30.0,
                "progress_seq": 12,
            },
        }
        seen: list[float] = []
        adapters: list[str] = []

        def spy(previous, snapshots, now, thresholds, *, retained=False, answer_owed_since=0.0, adapter=""):
            seen.append(answer_owed_since)
            adapters.append(adapter)
            return reduce_vitality(
                previous,
                snapshots,
                now,
                thresholds,
                retained=retained,
                answer_owed_since=answer_owed_since,
                adapter=adapter,
            )

        with mock.patch("ummanu.dispatch.wait_vitality._reduce_vitality", spy):
            self.tick()
        self.assertEqual(seen[0], asked)
        # The fixture's worker runs on the codex adapter: a head the idle-turn premise holds for.
        self.assertEqual(adapters[0], "codex")
        stored = self.runtime.production_state.records(self.runtime.production_state.load())[CARD_REF]
        episode = stored.worker_vitality_episode
        self.assertIs(episode.verdict, VitalityVerdict.CONFIRMED_STALL)
        self.assertEqual(episode.idle_turn_since, asked + 30.0)
        self.assertIn(SnapshotSource.SUPERVISOR_JOURNAL.value, episode.evidence_cursors)

        # The same observation reduced by the gate path's call, which declares nothing.
        payload = self.runtime.production_state.load()
        records = self.runtime.production_state.records(payload)
        target = records[CARD_REF]
        target.worker_vitality_episode = None
        gate_episode = wait_vitality_module.reduce_and_store_vitality_episode(
            self.runtime,
            {"ref": CARD_REF},
            target,
            records,
            payload,
            dict(self.host.worker_status_result),
            kind="worker",
            now=time.time(),
        )
        self.assertEqual(gate_episode.idle_turn_since, 0.0)
        self.assertIsNot(gate_episode.verdict, VitalityVerdict.CONFIRMED_STALL)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
